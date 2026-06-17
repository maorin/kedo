"""
Skill 消费工具（Skill 双向 · 方向 1）

- skill_list：列出已安装 skill 的名字 + 描述（系统 prompt 已注入目录，本工具供 agent 复查）
- skill_read：读取某个 skill 的完整 SKILL.md 正文 + 随包文件清单 + 目录路径，
  之后 agent 按指令用 file_read 读 references、用 shell_execute 跑 scripts。

skill 由 SkillLoader（core/skill_loader.py）从 ~/.config/kedo/skills/ 加载。
工具本身不执行任何脚本——只读指令；执行交给现有 shell_execute / file_* / git / browser_*。
"""
from __future__ import annotations

from tools.base import BaseTool, ToolParameter, ToolResult


class SkillListTool(BaseTool):
    def __init__(self, loader):
        self._loader = loader

    @property
    def name(self) -> str:
        return "skill_list"

    @property
    def description(self) -> str:
        return (
            "列出当前已安装的 skill（可复用流程指令包）及其描述。"
            "当你想确认有哪些 skill 可用、或要按 skill 执行某流程前，先调它。"
        )

    @property
    def is_read_only(self) -> bool:
        return True

    async def execute(self, **kwargs) -> ToolResult:
        skills = self._loader.list_skills()
        if not skills:
            return ToolResult(success=True, output="（没有已安装的 skill）", data={"skills": []})
        lines = []
        for sk in skills:
            lines.append(f"- {sk.name}: {sk.description or '(无描述)'}")
        return ToolResult(
            success=True,
            output="\n".join(lines),
            data={"skills": [sk.to_public() for sk in skills]},
        )


class SkillReadTool(BaseTool):
    def __init__(self, loader):
        self._loader = loader

    @property
    def name(self) -> str:
        return "skill_read"

    @property
    def description(self) -> str:
        return (
            "读取一个 skill 的完整指令（SKILL.md 正文）+ 随包文件清单 + 目录绝对路径。"
            "拿到后请严格按 SKILL.md 执行：用 file_read 读其中提到的 references/scripts，"
            "用 shell_execute 运行脚本（如需在远程机器执行，按指令用 ssh 包装命令）。"
        )

    @property
    def parameters(self) -> list[ToolParameter]:
        return [
            ToolParameter("name", "string", "要读取的 skill 名字（来自 skill_list / Available Skills）", required=True),
        ]

    @property
    def is_read_only(self) -> bool:
        return True

    async def execute(self, **kwargs) -> ToolResult:
        name = (kwargs.get("name") or "").strip()
        if not name:
            return ToolResult(success=False, output="", error="缺少参数 name")
        sk = self._loader.get(name)
        if sk is None:
            avail = ", ".join(s.name for s in self._loader.list_skills()) or "(无)"
            return ToolResult(
                success=False,
                output="",
                error=f"未找到 skill '{name}'。已安装: {avail}",
            )
        files_block = "\n".join(f"  - {f}" for f in sk.files) or "  （无随包文件）"
        output = (
            f"# Skill: {sk.name}\n"
            f"描述: {sk.description}\n"
            f"目录: {sk.dir}\n"
            f"随包文件（用 file_read 读、shell_execute 跑）:\n{files_block}\n\n"
            f"--- SKILL.md 指令正文 ---\n{sk.body}"
        )
        return ToolResult(
            success=True,
            output=output,
            data=sk.to_public(with_body=True),
        )
