"""
安全 Shell 执行工具 — 在沙箱中运行 Shell 命令
"""
from __future__ import annotations

import asyncio
import logging
import os
from typing import Optional

from tools.base import BaseTool, ToolParameter, ToolResult

logger = logging.getLogger(__name__)

# 危险命令黑名单
BLOCKED_COMMANDS = {
    "rm -rf /", "rm -rf /*", "mkfs", "dd if=", ":(){:|:&};:",
    "chmod -R 777 /", "wget", "curl -o",
}

# 允许的命令白名单前缀
ALLOWED_PREFIXES = {
    "python", "pip", "npm", "node", "npx", "yarn",
    "git", "cat", "ls", "find", "grep", "echo", "mkdir",
    "cp", "mv", "touch", "head", "tail", "wc", "diff",
    "pytest", "jest", "cargo", "go", "make", "cmake",
    "rustc", "gcc", "javac", "tsc", "eslint", "prettier",
}


class ShellExecutorTool(BaseTool):
    """安全的 Shell 命令执行工具"""

    def __init__(
        self,
        working_dir: str = ".",
        timeout: int = 120,
        sandbox_mode: bool = True,
    ):
        self.working_dir = working_dir
        self.timeout = timeout
        self.sandbox_mode = sandbox_mode

    @property
    def name(self) -> str:
        return "shell_execute"

    @property
    def description(self) -> str:
        return "Execute shell commands safely in a sandboxed environment. Used for building, testing, running scripts, and file operations."

    @property
    def parameters(self) -> list[ToolParameter]:
        return [
            ToolParameter("command", "string", "Shell command to execute"),
            ToolParameter("working_dir", "string", "Working directory (optional)", required=False),
            ToolParameter("timeout", "integer", "Timeout in seconds (optional)", required=False),
        ]

    async def execute(
        self,
        command: str,
        working_dir: Optional[str] = None,
        timeout: Optional[int] = None,
    ) -> ToolResult:
        cwd = working_dir or self.working_dir
        timeout = timeout or self.timeout

        # 安全检查
        if self.sandbox_mode:
            safety_check = self._check_safety(command)
            if safety_check:
                return ToolResult(success=False, error=f"Blocked: {safety_check}")

        logger.info(f"Executing: {command} (cwd={cwd}, timeout={timeout}s)")

        try:
            proc = await asyncio.create_subprocess_shell(
                command,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=cwd,
                env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
            )
            stdout, stderr = await asyncio.wait_for(
                proc.communicate(), timeout=timeout
            )

            stdout_str = stdout.decode("utf-8", errors="replace").strip()
            stderr_str = stderr.decode("utf-8", errors="replace").strip()
            success = proc.returncode == 0

            output_parts = []
            if stdout_str:
                output_parts.append(stdout_str)
            if stderr_str:
                output_parts.append(f"[stderr]\n{stderr_str}")

            return ToolResult(
                success=success,
                output="\n".join(output_parts) or "(no output)",
                data={
                    "return_code": proc.returncode,
                    "stdout": stdout_str,
                    "stderr": stderr_str,
                },
                error=stderr_str if not success else None,
            )

        except asyncio.TimeoutError:
            return ToolResult(
                success=False,
                error=f"Command timed out after {timeout}s: {command}",
            )
        except Exception as e:
            return ToolResult(success=False, error=f"Execution error: {e}")

    def _check_safety(self, command: str) -> Optional[str]:
        """安全检查 — 阻止危险命令"""
        cmd_lower = command.lower().strip()

        # 检查黑名单
        for blocked in BLOCKED_COMMANDS:
            if blocked in cmd_lower:
                return f"Command contains blocked pattern: {blocked}"

        # 检查白名单 (可选的严格模式)
        # first_word = cmd_lower.split()[0] if cmd_lower else ""
        # if first_word not in ALLOWED_PREFIXES:
        #     return f"Command '{first_word}' not in allowed list"

        return None
