"""
Git 操作工具 — 版本控制操作
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Optional

from tools.base import BaseTool, ToolParameter, ToolResult
from tools.shell_executor import ShellExecutorTool

logger = logging.getLogger(__name__)


class GitTool(BaseTool):
    """Git 版本控制工具"""

    # 只读的 git action（可并发执行）
    READ_ONLY_ACTIONS = {"status", "diff", "diff_staged", "log"}

    def __init__(self, shell: ShellExecutorTool):
        self._shell = shell

    @property
    def name(self) -> str:
        return "git"

    @property
    def description(self) -> str:
        return "Perform Git operations: status, diff, add, commit, branch, log."

    @property
    def is_read_only(self) -> bool:
        # 默认标记为非只读；ToolOrchestrator 会根据具体 action 判断
        return False

    @property
    def parameters(self) -> list[ToolParameter]:
        return [
            ToolParameter("action", "string", "Git action: status/diff/add/commit/branch/log/stash/reset"),
            ToolParameter("project_path", "string", "Repository path"),
            ToolParameter("args", "string", "Additional arguments", required=False),
        ]

    async def execute(
        self,
        action: str,
        project_path: str,
        args: str = "",
    ) -> ToolResult:
        # 项目非 git 仓库时立刻返回 actionable 错误，避免 LLM 反复试失败触发 convergence
        # （实战观察：Producer 想 git log 找回失落代码，3 次 'fatal: 不是 git 仓库' 直接早停）
        if action != "init":
            git_dir = Path(project_path) / ".git"
            if not git_dir.exists():
                return ToolResult(
                    success=False,
                    error=(
                        f"Not a git repository at {project_path} (.git missing). "
                        f"Cannot use `git`. Try file_read / shell_execute instead, or run "
                        f"`git init` once if version control is needed."
                    ),
                    data={"action": action, "reason": "not_a_git_repo"},
                )

        command_map = {
            "status": "git status --short",
            "diff": "git diff",
            "diff_staged": "git diff --staged",
            "add": f"git add {args or '.'}",
            "commit": f'git commit -m "{args}"' if args else 'git commit -m "Auto-commit by AI Assistant"',
            "log": "git log --oneline -20",
            "branch": f"git branch {args}",
            "stash": f"git stash {args}",
            "reset": f"git reset {args}",
            "checkout": f"git checkout {args}",
            "init": f"git init {args}".strip(),
        }

        cmd = command_map.get(action)
        if not cmd:
            return ToolResult(success=False, error=f"Unknown git action: {action}")

        # 安全检查: 阻止 force push
        if "push" in action and "--force" in args:
            return ToolResult(success=False, error="Force push is blocked for safety")

        result = await self._shell.execute(command=cmd, working_dir=project_path)

        return ToolResult(
            success=result.success,
            output=result.output,
            data={"action": action, "command": cmd},
            error=result.error,
        )

    async def create_snapshot(self, project_path: str, message: str) -> ToolResult:
        """创建代码快照 (stash + commit)"""
        await self.execute("add", project_path, ".")
        return await self.execute("commit", project_path, f"[snapshot] {message}")

    async def get_diff_summary(self, project_path: str) -> str:
        """获取变更摘要"""
        status = await self.execute("status", project_path)
        diff = await self.execute("diff", project_path)
        return f"Status:\n{status.output}\n\nDiff:\n{diff.output}"
