"""
Permission Manager — 权限分层系统

借鉴 Claude Code 的权限模型，支持三级权限模式:
- interactive: 每次写操作前需确认
- auto: 自动执行，仅危险操作需确认
- sandbox: 完全沙箱，禁止所有写操作

路径作用域: 所有文件操作限制在 project_root 内
危险命令检测: 增强版命令安全检查
"""
from __future__ import annotations

import logging
import re
from enum import Enum
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)


class PermissionMode(str, Enum):
    INTERACTIVE = "interactive"   # 每次操作前确认
    AUTO = "auto"                 # 自动执行，仅危险操作确认
    SANDBOX = "sandbox"           # 完全沙箱，禁止所有写操作


# 写工具列表
WRITE_TOOLS = {"shell_execute", "file_write", "code_generate", "git"}

# 危险命令模式（在 auto 模式下也需确认）
DANGEROUS_PATTERNS = [
    r"rm\s+-rf\s+/",
    r"rm\s+-rf\s+\*",
    r"mkfs\b",
    r"dd\s+if=",
    r"chmod\s+-R\s+777\s+/",
    r":\(\)\{.*\}",                # fork bomb
    r"git\s+push\s+.*--force",
    r"git\s+reset\s+--hard",
    r"drop\s+database",
    r"drop\s+table",
    r"truncate\s+table",
    r">\s*/dev/sd",
    r"curl\s+.*\|\s*sh",
    r"wget\s+.*\|\s*sh",
]

# Git 的写操作 action
GIT_WRITE_ACTIONS = {"add", "commit", "branch", "stash", "reset", "checkout", "push"}


class PermissionManager:
    """
    权限管理器

    用法:
        pm = PermissionManager(mode=PermissionMode.AUTO, project_root="/path/to/project")

        # 检查工具权限
        await pm.check("file_write", file_path="/path/to/project/src/main.py")  # OK
        await pm.check("file_write", file_path="/etc/passwd")  # PermissionError

        # 在 sandbox 模式下
        await pm.check("shell_execute", command="ls")  # PermissionError
    """

    def __init__(
        self,
        mode: PermissionMode = PermissionMode.AUTO,
        project_root: str = ".",
        confirm_callback=None,
    ):
        self.mode = mode
        self.project_root = Path(project_root).resolve()
        # 可选的确认回调（interactive 模式下使用）
        self._confirm_callback = confirm_callback
        # 已批准的操作缓存（避免重复确认同类操作）
        self._approved_operations: set[str] = set()

    async def check(self, tool_name: str, **kwargs) -> bool:
        """
        检查操作权限

        Args:
            tool_name: 工具名称
            **kwargs: 工具参数

        Returns:
            True 表示允许执行

        Raises:
            PermissionError: 权限不足时抛出
        """
        # 1. 路径作用域检查
        self._check_path_scope(tool_name, kwargs)

        # 2. 模式检查
        if self.mode == PermissionMode.SANDBOX:
            if tool_name in WRITE_TOOLS:
                # Git 的只读操作在沙箱中也允许
                if tool_name == "git":
                    action = kwargs.get("action", "")
                    if action not in GIT_WRITE_ACTIONS:
                        return True
                raise PermissionError(
                    f"Sandbox mode: write operation '{tool_name}' is not allowed"
                )

        # 3. 危险命令检测
        if tool_name == "shell_execute":
            command = kwargs.get("command", "")
            danger = self._detect_dangerous_command(command)
            if danger:
                if self.mode == PermissionMode.AUTO:
                    raise PermissionError(
                        f"Dangerous command blocked in auto mode: {danger}"
                    )
                elif self.mode == PermissionMode.INTERACTIVE:
                    approved = await self._request_confirmation(
                        f"Dangerous command detected: {danger}\nCommand: {command}"
                    )
                    if not approved:
                        raise PermissionError(
                            f"User denied dangerous command: {command}"
                        )

        # 4. Interactive 模式: 写操作需确认
        if self.mode == PermissionMode.INTERACTIVE and tool_name in WRITE_TOOLS:
            # Git 只读操作不需要确认
            if tool_name == "git":
                action = kwargs.get("action", "")
                if action not in GIT_WRITE_ACTIONS:
                    return True

            op_key = f"{tool_name}:{self._op_fingerprint(kwargs)}"
            if op_key not in self._approved_operations:
                desc = self._describe_operation(tool_name, kwargs)
                approved = await self._request_confirmation(desc)
                if not approved:
                    raise PermissionError(f"User denied: {desc}")
                self._approved_operations.add(op_key)

        return True

    def _check_path_scope(self, tool_name: str, kwargs: dict):
        """确保文件操作在项目根目录内"""
        path_keys = ["file_path", "directory", "project_path", "working_dir"]

        for key in path_keys:
            if key not in kwargs:
                continue
            target = Path(kwargs[key]).resolve()
            if not self._is_within_project(target):
                raise PermissionError(
                    f"Path '{target}' is outside project root '{self.project_root}'"
                )

    def _is_within_project(self, path: Path) -> bool:
        """检查路径是否在项目根目录内"""
        try:
            path.relative_to(self.project_root)
            return True
        except ValueError:
            return False

    def _detect_dangerous_command(self, command: str) -> Optional[str]:
        """检测危险命令模式"""
        cmd_lower = command.lower().strip()
        for pattern in DANGEROUS_PATTERNS:
            if re.search(pattern, cmd_lower):
                return f"Matches dangerous pattern: {pattern}"
        return None

    async def _request_confirmation(self, description: str) -> bool:
        """请求用户确认（interactive 模式）"""
        if self._confirm_callback:
            return await self._confirm_callback(description)
        # 无回调时默认拒绝
        logger.warning(f"Permission request (no callback, auto-denied): {description}")
        return False

    def _op_fingerprint(self, kwargs: dict) -> str:
        """生成操作指纹用于去重"""
        parts = []
        for key in sorted(kwargs.keys()):
            val = str(kwargs[key])[:50]
            parts.append(f"{key}={val}")
        return "|".join(parts)

    def _describe_operation(self, tool_name: str, kwargs: dict) -> str:
        """生成人类可读的操作描述"""
        if tool_name == "file_write":
            return f"Write to file: {kwargs.get('file_path', '?')}"
        elif tool_name == "code_generate":
            return f"Generate code: {kwargs.get('instruction', '?')[:80]}"
        elif tool_name == "shell_execute":
            return f"Execute command: {kwargs.get('command', '?')}"
        elif tool_name == "git":
            return f"Git {kwargs.get('action', '?')}: {kwargs.get('args', '')}"
        return f"{tool_name}: {kwargs}"
