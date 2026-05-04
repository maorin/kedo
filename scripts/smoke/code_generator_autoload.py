"""
冒烟 P0 修复:
  case A: 已有文件 + LLM 没传 ec → 自动磁盘加载, prompt 含 Current content, 不被覆盖
  case B: 已有文件 + LLM 传了截断 ec → 信任 LLM (旧行为不变)
  case C: 新文件 + LLM 没传 ec → 不读盘, 走零上下文创建
  case D: 已有文件 + LLM 没传 ec + 文件超大 → 不读盘 + WARN
  case E: 已有文件 + 自动加载 + LLM 输出片段 → shrink 防御拒写
  case F: .md 已存在修改 → 单次生成不走分段
  case G: .md 不存在创建 → 走分段
"""
import asyncio, sys, tempfile
from pathlib import Path
sys.path.insert(0, '.')
from tools.code_generator import CodeGeneratorTool, MAX_AUTO_LOAD_CHARS

class CapturingLLM:
    """记录每次调用收到的 prompt + 可控返回内容"""
    def __init__(self, returns: list):
        self.returns = list(returns)  # FIFO
        self.calls = []  # [(messages, returned)]
    async def chat(self, messages):
        out = self.returns.pop(0)
        self.calls.append({"messages": messages, "returned": out})
        return out

async def main():
    failed = 0

    # ─── case A: 已有 .c 文件, LLM 不传 ec → 应自动加载 ───────────
    tmp = Path(tempfile.mkdtemp())
    target = tmp / "video_player.c"
    original = "/* original */\n" + "\n".join(f"int line_{i}(void){{return {i};}}" for i in range(50))
    target.write_text(original)
    # mock LLM 输出长度合理的"修改后全文"
    new_full = original.replace("line_0", "line_renamed")
    llm = CapturingLLM(returns=[f"```c\n{new_full}\n```"])
    tool = CodeGeneratorTool(llm, config={})
    res = await tool.execute(instruction="rename line_0 to line_renamed", file_path=str(target))
    last = llm.calls[0]["messages"][-1]["content"]
    if "Current content:" in last and original.split('\n')[1] in last:
        print("[A] PASS — auto-load worked, prompt contains existing content")
    else:
        print("[A] FAIL — prompt missing Current content"); failed += 1
    if res.success and len(target.read_text()) > 1000:
        print("[A] PASS — file not truncated, success=True")
    else:
        print(f"[A] FAIL — success={res.success}, file size={len(target.read_text())}"); failed += 1

    # ─── case B: LLM 传了截断 ec, 工具应该不再覆盖 ────────────────
    target.write_text(original)
    truncated_ec = original[:200]  # LLM 传的是截断版
    llm = CapturingLLM(returns=[f"```c\n{truncated_ec}\nadded_line\n```"])
    tool = CodeGeneratorTool(llm, config={})
    res = await tool.execute(
        instruction="add line",
        file_path=str(target),
        existing_content=truncated_ec,  # LLM 显式传截断
    )
    last = llm.calls[0]["messages"][-1]["content"]
    # 期望: prompt 用的是 LLM 传的 truncated 版, 不是磁盘上的 (信任 LLM)
    if truncated_ec[:100] in last:
        print("[B] PASS — LLM-provided existing_content used (not disk)")
    else:
        print("[B] FAIL — prompt didn't use LLM-provided ec"); failed += 1

    # ─── case C: 新文件, LLM 不传 ec → 不读盘 (旧创建路径) ────────
    target_new = tmp / "brand_new.c"
    llm = CapturingLLM(returns=["```c\nint main(void){return 0;}\n```"])
    tool = CodeGeneratorTool(llm, config={})
    res = await tool.execute(instruction="hello main", file_path=str(target_new))
    last = llm.calls[0]["messages"][-1]["content"]
    if "Create a new file" in last and "Current content:" not in last:
        print("[C] PASS — new file path: no Current content injected")
    else:
        print("[C] FAIL — new file path malformed"); failed += 1

    # ─── case D: 文件超大 (>80k) → 不读盘 ────────────────────────
    target_huge = tmp / "huge.c"
    huge = "x" * (MAX_AUTO_LOAD_CHARS + 100)
    target_huge.write_text(huge)
    llm = CapturingLLM(returns=["```c\nint main(void){return 0;}\n```"])
    tool = CodeGeneratorTool(llm, config={})
    res = await tool.execute(instruction="rewrite", file_path=str(target_huge))
    last = llm.calls[0]["messages"][-1]["content"]
    if "Current content:" not in last:
        print("[D] PASS — huge file not auto-loaded (>MAX_AUTO_LOAD_CHARS)")
    else:
        print("[D] FAIL — huge file was auto-loaded"); failed += 1

    # ─── case E: shrink 防御 ────────────────────────────────────
    target.write_text(original)  # 1500+ 字符
    # mock LLM 偷懒输出 30 字符片段
    llm = CapturingLLM(returns=["```c\nint x = 0;\n```"])
    tool = CodeGeneratorTool(llm, config={})
    res = await tool.execute(instruction="remove unused", file_path=str(target))
    if not res.success and "shrink_defense" in (res.data or {}).get("rejected_reason", ""):
        print("[E] PASS — shrink defense rejected the truncated output")
    else:
        print(f"[E] FAIL — shrink defense missed; success={res.success} data={res.data}"); failed += 1
    # 重要: 文件未被破坏
    if target.read_text() == original:
        print("[E] PASS — file content preserved")
    else:
        print("[E] FAIL — file was overwritten despite shrink rejection"); failed += 1

    # ─── case F: 修改已有 .md → 走单次, 不分段 ──────────────────
    target_md = tmp / "doc.md"
    target_md.write_text("# Old\n\n## Section\n\nContent")
    llm = CapturingLLM(returns=["```markdown\n# Updated\n\n## Section\n\nNew content\n```"])
    tool = CodeGeneratorTool(llm, config={})
    res = await tool.execute(instruction="update title", file_path=str(target_md))
    # 单次生成走 _build_messages 路径, llm.calls 应该只有 1 次
    if len(llm.calls) == 1:
        print("[F] PASS — modified .md uses single-pass (not section-by-section)")
    else:
        print(f"[F] FAIL — .md modify went section-by-section ({len(llm.calls)} calls)"); failed += 1

    # ─── case G: 创建新 .md → 分段 (outline + 各章) ──────────────
    target_md_new = tmp / "new_doc.md"
    # 分段路径: 1 次 outline + N 次 sections; 这里给 outline + 2 sections 的返回值
    llm = CapturingLLM(returns=[
        "## 概述\n## 用法\n",          # outline
        "## 概述\n这是概述\n",          # section 1
        "## 用法\n这是用法\n",          # section 2
    ])
    tool = CodeGeneratorTool(llm, config={"doc_language": "zh"})
    res = await tool.execute(instruction="写个文档", file_path=str(target_md_new))
    if len(llm.calls) >= 2:
        print(f"[G] PASS — new .md goes through section path ({len(llm.calls)} calls)")
    else:
        print(f"[G] FAIL — new .md should be section-by-section, got {len(llm.calls)} calls"); failed += 1

    print()
    print(f"=== SMOKE DONE: {'ALL PASS' if failed == 0 else f'{failed} FAILURES'} ===")
    return 0 if failed == 0 else 1

sys.exit(asyncio.run(main()))
