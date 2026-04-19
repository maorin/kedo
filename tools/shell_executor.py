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

# 危险命令黑名单（子串匹配，命令小写后比对）
BLOCKED_COMMANDS = {
    "rm -rf /", "rm -rf /*", "mkfs", "dd if=", ":(){:|:&};:",
    "chmod -R 777 /", "wget", "curl -o",
}

# 提权命令前缀 — 任何在命令开头或 shell 链路中的提权调用都拦截
# 用 token 匹配（前面是行首/空格/分号/管道/&&/||），避免误伤含 "sudo" 字串的合法路径
PRIVILEGE_TOKENS = ("sudo", "su", "pkexec", "doas")

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
            # 屏蔽任何交互式密码 / 确认提示：
            # - stdin=DEVNULL：sudo/ssh/passwd 等读 tty 时直接 EOF 失败
            # - SUDO_ASKPASS=/bin/false + DISPLAY="" + SSH_ASKPASS=/bin/false：
            #   截断 sudo / ssh 通过 askpass 程序绕过 tty 弹窗
            # - GIT_TERMINAL_PROMPT=0 / GIT_ASKPASS=/bin/echo：阻止 git http 凭证弹窗
            child_env = {
                **os.environ,
                "PYTHONDONTWRITEBYTECODE": "1",
                "SUDO_ASKPASS": "/bin/false",
                "SSH_ASKPASS": "/bin/false",
                "DISPLAY": "",
                "GIT_TERMINAL_PROMPT": "0",
                "GIT_ASKPASS": "/bin/echo",
                "DEBIAN_FRONTEND": "noninteractive",
            }
            proc = await asyncio.create_subprocess_shell(
                command,
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=cwd,
                env=child_env,
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
        """安全检查 — 阻止危险命令 + 任何提权调用"""
        cmd_lower = command.lower().strip()

        # 检查黑名单
        for blocked in BLOCKED_COMMANDS:
            if blocked in cmd_lower:
                return f"Command contains blocked pattern: {blocked}"

        # 提权检查：sudo/su/pkexec/doas 出现在命令开头或 shell 链分隔符之后
        # 拆词时把分隔符 ; & | 都视为分界
        import re
        tokens = re.split(r"[\s;&|()`]+", cmd_lower)
        for tok in tokens:
            if tok in PRIVILEGE_TOKENS:
                return (
                    f"Command requires privilege escalation ({tok}); "
                    f"refused to avoid prompting for password. "
                    f"Install/configure tooling out-of-band, then retry without {tok}."
                )

        return None
