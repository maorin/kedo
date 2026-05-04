"""
编译工具 — 封装 ProjectProfileManager 的编译逻辑

T1（虚拟测试三层方案 Phase A）：
- profile.strict_warnings 注入 CFLAGS/CXXFLAGS/RUSTFLAGS（不改 build.command）
- stderr 里的 warning: 行 surface 给 LLM，不只 error
"""
from __future__ import annotations

import logging
import os
import re
import subprocess
from pathlib import Path

from tools.base import BaseTool, ToolParameter, ToolResult

logger = logging.getLogger(__name__)

# warning 行匹配：gcc/clang/cmake/Rust 通用
# gcc/clang:  path/to/file.c:42:5: warning: <msg>
# rust:       warning: <msg>
# cmake:      CMake Warning at <file>:<line> ...
WARNING_RE = re.compile(
    r"^(.+?:\d+(?::\d+)?:\s*warning:\s*.+|warning:\s*.+|CMake Warning.+)$",
    re.IGNORECASE,
)
MAX_WARNING_LINES = 30


class BuildTool(BaseTool):
    """执行项目编译，自动探测/使用 project profile"""

    def __init__(self, profile_manager=None, llm_client=None):
        self._profile_manager = profile_manager
        self._llm_client = llm_client

    @property
    def name(self) -> str:
        return "build"

    @property
    def description(self) -> str:
        return (
            "Build/compile the project. Automatically detects build system "
            "(CMake, Make, npm, cargo, etc.) and runs the appropriate command. "
            "Call this after writing or modifying code to verify it compiles."
        )

    @property
    def parameters(self) -> list[ToolParameter]:
        return [
            ToolParameter("project_path", "string", "Project root directory path"),
        ]

    async def execute(self, project_path: str) -> ToolResult:
        project_path = os.path.abspath(project_path)

        # 获取 build command + strict_warnings env
        build_command = None
        env_overrides: dict[str, str] = {}
        if self._profile_manager:
            try:
                profile = await self._profile_manager.ensure(
                    project_path, self._llm_client
                )
                if profile:
                    self._profile_manager.apply_required_env(profile)
                    build_command = profile.get("build", {}).get("command", "")
                    build_command = os.path.expandvars(build_command)
                    env_overrides = self._build_strict_env(profile)
            except Exception as e:
                logger.warning(f"Profile ensure failed: {e}")

        if not build_command:
            # 自动探测
            if (Path(project_path) / "CMakeLists.txt").exists():
                build_command = "cmake -B build -S . && cmake --build build --parallel"
            elif (Path(project_path) / "Makefile").exists():
                build_command = "make"
            elif (Path(project_path) / "package.json").exists():
                build_command = "npm run build"
            elif (Path(project_path) / "Cargo.toml").exists():
                build_command = "cargo build"
            else:
                return ToolResult(
                    success=False,
                    error="No build system detected (CMakeLists.txt, Makefile, package.json, Cargo.toml)",
                )

        # 拼合环境变量：os.environ + profile.required_env (已 apply) + strict_warnings
        run_env = os.environ.copy()
        if env_overrides:
            for k, v in env_overrides.items():
                # 已存在的 CFLAGS/CXXFLAGS：保留用户值，把 strict flags 追加在后面
                if k in ("CFLAGS", "CXXFLAGS", "RUSTFLAGS") and run_env.get(k):
                    run_env[k] = run_env[k] + " " + v
                else:
                    run_env[k] = v

        try:
            result = subprocess.run(
                build_command,
                shell=True,
                cwd=project_path,
                capture_output=True,
                text=True,
                timeout=300,
                env=run_env,
            )
            output = result.stdout[-3000:] if len(result.stdout) > 3000 else result.stdout
            stderr = result.stderr[-3000:] if len(result.stderr) > 3000 else result.stderr
            warnings = self._extract_warnings(result.stdout, result.stderr)
            warning_section = self._format_warnings(warnings) if warnings else ""

            if result.returncode == 0:
                output_text = f"Build succeeded.\n{output}"
                if warning_section:
                    output_text += f"\n{warning_section}"
                return ToolResult(
                    success=True,
                    output=output_text,
                    data={
                        "command": build_command,
                        "returncode": 0,
                        "warnings": warnings[:MAX_WARNING_LINES],
                        "warning_count": len(warnings),
                        "strict_env": list(env_overrides.keys()),
                    },
                )
            else:
                # 记录失败
                if self._profile_manager:
                    try:
                        self._profile_manager.mark_failure(project_path, stderr)
                    except Exception:
                        pass
                error_text = f"Build failed (exit {result.returncode}):\n{stderr}"
                if warning_section:
                    error_text += f"\n{warning_section}"
                return ToolResult(
                    success=False,
                    output=output,
                    error=error_text,
                    data={
                        "command": build_command,
                        "returncode": result.returncode,
                        "warnings": warnings[:MAX_WARNING_LINES],
                        "warning_count": len(warnings),
                        "strict_env": list(env_overrides.keys()),
                    },
                )
        except subprocess.TimeoutExpired:
            return ToolResult(success=False, error="Build timed out (300s)")
        except Exception as e:
            return ToolResult(success=False, error=f"Build error: {e}")

    # ----------------------------------------------------------
    # T1 helpers
    # ----------------------------------------------------------

    @staticmethod
    def _build_strict_env(profile: dict) -> dict[str, str]:
        """
        从 profile.strict_warnings 构造 env overrides。
        返回 {} 表示未启用。
        """
        sw = profile.get("strict_warnings") or {}
        if not sw or not sw.get("enabled"):
            return {}

        env: dict[str, str] = {}
        cflags = sw.get("cflags") or []
        cxxflags = sw.get("cxxflags") or []
        rustflags = sw.get("rustflags") or []
        if cflags:
            env["CFLAGS"] = " ".join(cflags)
        if cxxflags:
            env["CXXFLAGS"] = " ".join(cxxflags)
        if rustflags:
            env["RUSTFLAGS"] = " ".join(rustflags)
        for k, v in (sw.get("extra_env") or {}).items():
            if isinstance(k, str) and isinstance(v, str):
                env[k] = v
        return env

    @staticmethod
    def _extract_warnings(stdout: str, stderr: str) -> list[str]:
        """从 build 输出里抠出 warning 行。"""
        seen: set[str] = set()
        results: list[str] = []
        for chunk in (stdout, stderr):
            if not chunk:
                continue
            for line in chunk.splitlines():
                line = line.rstrip()
                if not line:
                    continue
                if WARNING_RE.match(line):
                    # 去重（同一 warning 在 stdout/stderr 都出现的情况）
                    if line not in seen:
                        seen.add(line)
                        results.append(line)
        return results

    @staticmethod
    def _format_warnings(warnings: list[str]) -> str:
        """把 warning 列表格式化成 LLM 友好的段落。"""
        n = len(warnings)
        head = warnings[:MAX_WARNING_LINES]
        body = "\n".join(head)
        suffix = f"\n... and {n - MAX_WARNING_LINES} more" if n > MAX_WARNING_LINES else ""
        return f"--- Warnings ({n}) ---\n{body}{suffix}"
