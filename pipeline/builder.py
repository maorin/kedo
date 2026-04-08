"""
Builder — 构建编译 Pipeline

管理项目的构建流程：编译、类型检查、Lint
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Optional

from tools.shell_executor import ShellExecutorTool

logger = logging.getLogger(__name__)


@dataclass
class BuildResult:
    success: bool
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    duration_seconds: float = 0


class Builder:
    """项目构建器"""

    def __init__(self, shell: ShellExecutorTool):
        self._shell = shell

    async def build(self, project_path: str, build_command: Optional[str] = None) -> BuildResult:
        """执行构建"""
        if not build_command:
            build_command = await self._detect_build_command(project_path)

        if not build_command:
            return BuildResult(success=True, warnings=["No build command detected, skipping"])

        logger.info(f"Building: {build_command}")
        result = await self._shell.execute(command=build_command, working_dir=project_path)

        errors = self._parse_errors(result.output + "\n" + result.data.get("stderr", ""))
        warnings = self._parse_warnings(result.output)

        return BuildResult(
            success=result.success,
            errors=errors,
            warnings=warnings,
            duration_seconds=result.duration_ms / 1000,
        )

    async def lint(self, project_path: str) -> BuildResult:
        """运行 Lint 检查"""
        lint_commands = await self._detect_lint_commands(project_path)
        all_errors = []
        all_warnings = []

        for cmd in lint_commands:
            result = await self._shell.execute(command=cmd, working_dir=project_path)
            all_errors.extend(self._parse_errors(result.output))
            all_warnings.extend(self._parse_warnings(result.output))

        return BuildResult(
            success=len(all_errors) == 0,
            errors=all_errors,
            warnings=all_warnings,
        )

    async def type_check(self, project_path: str) -> BuildResult:
        """运行类型检查"""
        from pathlib import Path
        p = Path(project_path)

        commands = []
        if (p / "tsconfig.json").exists():
            commands.append("npx tsc --noEmit")
        if (p / "pyproject.toml").exists() or (p / "mypy.ini").exists():
            commands.append("python -m mypy .")

        all_errors = []
        for cmd in commands:
            result = await self._shell.execute(command=cmd, working_dir=project_path)
            if not result.success:
                all_errors.extend(self._parse_errors(result.output))

        return BuildResult(success=len(all_errors) == 0, errors=all_errors)

    async def _detect_build_command(self, project_path: str) -> Optional[str]:
        """自动检测构建命令"""
        from pathlib import Path
        p = Path(project_path)

        if (p / "package.json").exists():
            return "npm run build"
        if (p / "Makefile").exists():
            return "make"
        if (p / "Cargo.toml").exists():
            return "cargo build"
        if (p / "go.mod").exists():
            return "go build ./..."
        if (p / "setup.py").exists() or (p / "pyproject.toml").exists():
            return "python -m py_compile $(find . -name '*.py' -not -path './.venv/*')"
        return None

    async def _detect_lint_commands(self, project_path: str) -> list[str]:
        from pathlib import Path
        p = Path(project_path)
        commands = []

        if (p / ".eslintrc.js").exists() or (p / ".eslintrc.json").exists():
            commands.append("npx eslint . --max-warnings=0")
        if (p / "pyproject.toml").exists():
            commands.append("python -m ruff check .")
        if (p / ".flake8").exists():
            commands.append("python -m flake8 .")

        return commands

    def _parse_errors(self, output: str) -> list[str]:
        """从输出中提取错误信息"""
        errors = []
        for line in output.splitlines():
            lower = line.lower()
            if "error" in lower and not lower.startswith("warning"):
                errors.append(line.strip())
        return errors[:20]

    def _parse_warnings(self, output: str) -> list[str]:
        warnings = []
        for line in output.splitlines():
            if "warning" in line.lower():
                warnings.append(line.strip())
        return warnings[:20]
