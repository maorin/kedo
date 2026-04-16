"""
编译工具 — 封装 ProjectProfileManager 的编译逻辑
"""
from __future__ import annotations

import logging
import os
import subprocess
from pathlib import Path

from tools.base import BaseTool, ToolParameter, ToolResult

logger = logging.getLogger(__name__)


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

        # 获取 build command
        build_command = None
        if self._profile_manager:
            try:
                profile = await self._profile_manager.ensure(
                    project_path, self._llm_client
                )
                if profile:
                    self._profile_manager.apply_required_env(profile)
                    build_command = profile.get("build", {}).get("command", "")
                    build_command = os.path.expandvars(build_command)
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

        try:
            result = subprocess.run(
                build_command,
                shell=True,
                cwd=project_path,
                capture_output=True,
                text=True,
                timeout=300,
            )
            output = result.stdout[-3000:] if len(result.stdout) > 3000 else result.stdout
            stderr = result.stderr[-3000:] if len(result.stderr) > 3000 else result.stderr
            if result.returncode == 0:
                return ToolResult(
                    success=True,
                    output=f"Build succeeded.\n{output}",
                    data={"command": build_command, "returncode": 0},
                )
            else:
                # 记录失败
                if self._profile_manager:
                    try:
                        self._profile_manager.mark_failure(project_path, stderr)
                    except Exception:
                        pass
                return ToolResult(
                    success=False,
                    output=output,
                    error=f"Build failed (exit {result.returncode}):\n{stderr}",
                    data={"command": build_command, "returncode": result.returncode},
                )
        except subprocess.TimeoutExpired:
            return ToolResult(success=False, error="Build timed out (300s)")
        except Exception as e:
            return ToolResult(success=False, error=f"Build error: {e}")
