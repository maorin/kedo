"""
Coredump parser — Atmosphere crash_reports/*.log → structured crash site.

T3 真模拟器搁置后真机崩溃定位的实用替代。流程：
  1. parse_crash_log(text)           → 提取 result/type/fault/PC/LR/stack frames
  2. resolve_with_addr2line(frames)  → 偏移翻译成 function/file:line + inline 链
  3. format_for_llm(...)             → 给 LLM 看的精简文本

只解决 Switch homebrew 这条路径（aarch64-none-elf-addr2line）。其他平台后接。

调用约定：所有函数都是纯函数 / 子进程调用，没有事件 / 网络副作用。
"""
from __future__ import annotations

import logging
import os
import re
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)


# ============================================================
# 数据模型
# ============================================================

@dataclass
class StackFrame:
    """单个栈帧（addr2line 之前可能有多个 inline，解完后展开）"""
    role: str           # "PC" | "LR" | "RA0" | "RA1" | ...
    raw_addr: str       # 完整虚拟地址，如 "00000058973d8e34"
    module: str         # "switchvideo"（来自 `(switchvideo + 0xXXXX)`）
    offset: str         # "0x8bbe34"
    # addr2line 结果（多行表示 inline 链；index 0 是最深的 inline 函数）
    resolved: list[dict] = field(default_factory=list)
    # 每个 dict: {"function": str, "file": str, "line": int}


@dataclass
class CrashReport:
    """解析后的 crash log。各字段为 None 表示原文里没找到对应行。"""
    result_code: Optional[str] = None     # "0x4A8 (2168-0002)"
    exception_type: Optional[str] = None  # "Data Abort"
    fault_address: Optional[str] = None   # "0x0"
    process_name: Optional[str] = None    # "hbloader"（注意不是业务 .nro 名）
    program_id: Optional[str] = None      # "010000000000100d"
    thread_id: Optional[str] = None
    # 栈帧（含 PC + LR + ReturnAddress[N]，按照源 log 顺序）
    frames: list[StackFrame] = field(default_factory=list)
    # 寄存器（X0-X28 + FP/SP），保留原样
    registers: dict[str, str] = field(default_factory=dict)
    # 主模块名（从栈帧里提取频次最高的那个，用作 .elf 推导）
    primary_module: Optional[str] = None


# ============================================================
# 解析
# ============================================================

# 匹配 "(switchvideo + 0x4e1a8)" 这种模块偏移注解
_MODULE_OFFSET_RE = re.compile(r"\(([A-Za-z_][\w\-.]*)\s*\+\s*(0x[0-9a-fA-F]+)\)")

# 匹配单字段行 "Result:    0x4A8 (2168-0002)"，捕获冒号后内容（去前后空白）
_FIELD_RE = re.compile(r"^\s*([A-Za-z][A-Za-z\s]*?):\s+(.+?)\s*$")

# 寄存器行 "        X[00]:    0x..." / "        PC:    0x..." / "        LR:    0x..."
_REG_X_RE = re.compile(r"^\s+X\[(\d+)\]:\s+([0-9a-fA-FxX]+)")
_REG_NAMED_RE = re.compile(r"^\s+(PC|LR|SP|FP):\s+([0-9a-fA-FxX]+)")

# 栈帧 "        ReturnAddress[00]:    00000058... (switchvideo + 0xXXX)"
_STACK_RE = re.compile(r"^\s+ReturnAddress\[(\d+)\]:\s+([0-9a-fA-F]+)\s*(\(.*\))?")


def parse_crash_log(text: str) -> CrashReport:
    """解析 Atmosphere crash report 文本，返回结构化数据。

    text 里行/字段缺失时尽力解析，不报错（returns 部分填充的 CrashReport）。
    """
    rep = CrashReport()
    lines = text.splitlines()
    section = None  # 当前 section header（"Process Info" / "Exception Info" / "Registers" / "Stack Trace"）

    for raw in lines:
        line = raw.rstrip()

        # section header（任意缩进，行内只有 "Xxx:" 格式 — Atmosphere log 里
        # Registers / Stack Trace 是嵌在 Crashed Thread Info 下的 4-space 子 section）
        stripped = line.strip()
        if stripped.endswith(":") and stripped.count(":") == 1:
            header = stripped[:-1].strip()
            if header in ("Process Info", "Exception Info", "Crashed Thread Info",
                          "Registers", "Stack Trace", "Stack Dump", "TLS Dump",
                          "Module Info", "Thread Report"):
                # Module Info / Thread Report 之后是各线程的重复数据，直接停。
                # 我们只要"Crashed Thread"那一份。
                if header in ("Module Info", "Thread Report"):
                    break
                section = header
                continue

        # 解析单字段行（顶层）
        if section in (None, "Process Info", "Exception Info",
                       "Crashed Thread Info"):
            m = _FIELD_RE.match(line)
            if m:
                key = m.group(1).strip()
                val = m.group(2).strip()
                if key == "Result":
                    rep.result_code = val
                elif key in ("Type",):
                    rep.exception_type = val
                elif key == "Fault Address":
                    rep.fault_address = val
                elif key == "Process Name":
                    rep.process_name = val
                elif key == "Program ID":
                    rep.program_id = val
                elif key == "Thread ID":
                    rep.thread_id = val

        # 寄存器（同一 Registers 块下，PC/LR 可能带模块偏移）
        if section in ("Registers",):
            mx = _REG_X_RE.match(line)
            if mx:
                rep.registers[f"X{int(mx.group(1)):02d}"] = mx.group(2)
                continue
            mn = _REG_NAMED_RE.match(line)
            if mn:
                name = mn.group(1)
                rep.registers[name] = mn.group(2)
                # PC / LR 同时记到 frames
                if name in ("PC", "LR"):
                    mod_match = _MODULE_OFFSET_RE.search(line)
                    if mod_match:
                        rep.frames.append(StackFrame(
                            role=name,
                            raw_addr=mn.group(2),
                            module=mod_match.group(1),
                            offset=mod_match.group(2),
                        ))
                continue

        # Stack Trace 区
        if section == "Stack Trace":
            ms = _STACK_RE.match(line)
            if ms:
                idx = int(ms.group(1))
                addr = ms.group(2)
                mod_match = _MODULE_OFFSET_RE.search(line)
                if mod_match:
                    rep.frames.append(StackFrame(
                        role=f"RA{idx}",
                        raw_addr=addr,
                        module=mod_match.group(1),
                        offset=mod_match.group(2),
                    ))

    # 推主模块（频次最高的）
    if rep.frames:
        from collections import Counter
        c = Counter(f.module for f in rep.frames)
        rep.primary_module = c.most_common(1)[0][0]

    return rep


# ============================================================
# addr2line 解析
# ============================================================

def find_addr2line() -> Optional[str]:
    """定位 aarch64-none-elf-addr2line。优先 $DEVKITPRO，再 PATH。返回 None 表示找不到。"""
    devkit = os.environ.get("DEVKITPRO", "/opt/devkitpro")
    candidate = Path(devkit) / "devkitA64" / "bin" / "aarch64-none-elf-addr2line"
    if candidate.exists():
        return str(candidate)
    p = shutil.which("aarch64-none-elf-addr2line")
    return p


def resolve_with_addr2line(
    frames: list[StackFrame],
    elf_path: str,
    primary_module: Optional[str] = None,
    addr2line_bin: Optional[str] = None,
) -> list[StackFrame]:
    """用 addr2line 把每个 StackFrame.offset 翻译成 function/file:line。

    只解析 module == primary_module 的帧（其它模块比如 hbloader / Atmosphere 自己的栈
    不在我们 .elf 里，addr2line 解出来是 ??:?）。

    返回 frames 同一对象（in-place 填 resolved），方便链式调用。
    """
    addr2line = addr2line_bin or find_addr2line()
    if not addr2line:
        logger.warning("addr2line not found — skip symbol resolution")
        return frames

    if not Path(elf_path).is_file():
        logger.warning(f"elf not found: {elf_path}")
        return frames

    # 一次性传所有偏移，addr2line -i 输出 inline 链每帧多行：
    # function_name
    # file:line
    # 多个 inline 时反复，最后一行是空行分隔（实测无空行，靠数 frames 数量分组）

    target_frames = [f for f in frames if not primary_module or f.module == primary_module]
    if not target_frames:
        return frames

    offsets = [f.offset for f in target_frames]
    cmd = [addr2line, "-e", elf_path, "-f", "-C", "-i"] + offsets

    try:
        proc = subprocess.run(
            cmd, capture_output=True, text=True, timeout=20,
        )
    except (subprocess.TimeoutExpired, FileNotFoundError) as e:
        logger.warning(f"addr2line failed: {e}")
        return frames

    if proc.returncode != 0:
        logger.warning(f"addr2line returncode={proc.returncode}: {proc.stderr[:200]}")
        return frames

    # addr2line -i 的输出：每个输入偏移对应 N 行（N=inline 链长度），无显式分隔符。
    # 我们没法 100% 知道 N，但可以靠"function 行 + file:line 行"的成对模式推：
    #   line[0] = func_name (可能是 "??")
    #   line[1] = file:line (可能是 "??:?" 或 "??:0")
    # 然后下一对是同一 PC 的 inliner，直到遇到下一个 PC（实测无标识符）。
    # 简化策略：因为我们传 N 个 offsets，把 stdout 按"两行一组"切，按顺序分给每个 frame。
    # 当 inline 多帧时，组数会比 N 多 — 我们用 -i 时 addr2line 没给分组，干脆禁用 -i。
    #
    # 改方案：单独跑 -i 拿不到分组；改成不传 -i，保证一个偏移一对（func + file:line）。
    # 如果将来需要 inline 链，单独再跑 -i mode 单独偏移。
    # 这里简化：同一 offset 跑两次，第二次带 -i 拿 inline 链。

    pairs = proc.stdout.strip().splitlines()
    # 没 -i 模式：N 个 offset 应该输出 2N 行（func + loc）。
    # 但 -i 已经传了… 重新跑一次不带 -i 拿到稳定 2N 行布局。
    cmd_noinline = [addr2line, "-e", elf_path, "-f", "-C"] + offsets
    try:
        proc2 = subprocess.run(
            cmd_noinline, capture_output=True, text=True, timeout=20,
        )
    except Exception as e:
        logger.warning(f"addr2line (no-i) failed: {e}")
        return frames
    if proc2.returncode != 0:
        return frames

    pairs2 = proc2.stdout.strip().splitlines()
    if len(pairs2) < 2 * len(target_frames):
        logger.warning(
            f"addr2line output line count mismatch: "
            f"got {len(pairs2)} for {len(target_frames)} offsets"
        )

    for i, frame in enumerate(target_frames):
        try:
            func = pairs2[2 * i].strip()
            loc = pairs2[2 * i + 1].strip()
        except IndexError:
            break
        file_part, _, line_part = loc.rpartition(":")
        try:
            line_num = int(line_part) if line_part.isdigit() else None
        except ValueError:
            line_num = None
        frame.resolved.append({
            "function": func or "??",
            "file": file_part or "??",
            "line": line_num,
        })

    # 第二次跑带 -i 单独取 inline 链（可选；只对 PC 帧做，省 token）
    pc_frames = [f for f in target_frames if f.role == "PC"]
    for pcf in pc_frames:
        try:
            proc3 = subprocess.run(
                [addr2line, "-e", elf_path, "-f", "-C", "-i", pcf.offset],
                capture_output=True, text=True, timeout=10,
            )
            inline_lines = proc3.stdout.strip().splitlines()
            chain = []
            for j in range(0, len(inline_lines), 2):
                if j + 1 >= len(inline_lines):
                    break
                f = inline_lines[j].strip()
                loc = inline_lines[j + 1].strip()
                file_part, _, line_part = loc.rpartition(":")
                try:
                    ln = int(line_part) if line_part.isdigit() else None
                except ValueError:
                    ln = None
                chain.append({"function": f, "file": file_part, "line": ln})
            if len(chain) > 1:
                # PC 的 resolved 替换为完整 inline 链（最深在前）
                pcf.resolved = chain
        except Exception as e:
            logger.debug(f"PC inline expansion failed: {e}")

    return frames


# ============================================================
# 格式化输出
# ============================================================

def find_business_frame(rep: CrashReport, project_path: str) -> Optional[StackFrame]:
    """找第一个 file 在 project_path 下的栈帧 — 这通常就是业务崩点。
    libnx 内部帧的 file 是 /home/fincs/git/... 不会匹配。
    """
    abs_proj = os.path.abspath(project_path).rstrip("/")
    for f in rep.frames:
        for r in f.resolved:
            file = r.get("file") or ""
            if file.startswith(abs_proj + "/") or file.startswith(abs_proj + "\\"):
                return f
    return None


def format_for_llm(rep: CrashReport, project_path: str = "") -> str:
    """给 ReactAgent / Reviewer 看的简短文本。<= ~1.5KB。"""
    out = []
    out.append("=== Switch Crash Report ===")
    if rep.result_code:
        out.append(f"Result:    {rep.result_code}")
    if rep.exception_type:
        out.append(f"Type:      {rep.exception_type}")
    if rep.fault_address and rep.fault_address.lower() not in ("0x0", "0000000000000000", ""):
        out.append(f"Fault Addr:{rep.fault_address}")
    elif rep.fault_address:
        out.append(f"Fault Addr:0x0  (NULL pointer dereference)")
    if rep.process_name:
        out.append(f"Process:   {rep.process_name}  (homebrew loader; module={rep.primary_module})")

    biz = find_business_frame(rep, project_path) if project_path else None
    if biz and biz.resolved:
        r0 = biz.resolved[0]
        out.append("")
        out.append(f"→ Crash site: {r0.get('function')} at {r0.get('file')}:{r0.get('line')}")
        if len(biz.resolved) > 1:
            chain = " → ".join(
                f"{x.get('function')}:{x.get('line')}" for x in biz.resolved
            )
            out.append(f"  inline chain: {chain}")

    out.append("")
    out.append("Stack (deepest first):")
    seen = 0
    for f in rep.frames:
        if not f.resolved:
            continue
        r0 = f.resolved[0]
        func = r0.get("function") or "??"
        file = r0.get("file") or "??"
        line = r0.get("line")
        loc = f"{file}:{line}" if line is not None else file
        out.append(f"  [{f.role:>3}] {func:30s}  {loc}  ({f.module}+{f.offset})")
        seen += 1
        if seen >= 8:
            out.append(f"  ... (+{len(rep.frames) - seen} more frames)")
            break

    if not seen:
        out.append("  (no symbols resolved — addr2line unavailable or .elf missing)")

    return "\n".join(out)


# ============================================================
# 顶层便利函数
# ============================================================

def parse_and_resolve(
    log_text: str,
    elf_path: str,
    project_path: str = "",
) -> CrashReport:
    """一站式：解析 + addr2line。失败时仍返回部分结果。"""
    rep = parse_crash_log(log_text)
    if rep.frames:
        resolve_with_addr2line(rep.frames, elf_path, primary_module=rep.primary_module)
    return rep


def parse_log_file(
    log_path: str,
    elf_path: str,
    project_path: str = "",
) -> CrashReport:
    """从 .log 文件路径直接解析 + 解析符号。"""
    with open(log_path, "r", encoding="utf-8", errors="replace") as f:
        text = f.read()
    return parse_and_resolve(text, elf_path, project_path)
