"""
测试运行工具 — 运行测试套件并收集结果
"""
from __future__ import annotations

import json
import logging
import re
from pathlib import Path
from typing import Optional

from api.schemas import TestResult
from tools.base import BaseTool, ToolParameter, ToolResult
from tools.shell_executor import ShellExecutorTool

logger = logging.getLogger(__name__)


class TestRunnerTool(BaseTool):
    """自动化测试运行工具"""

    def __init__(self, shell: ShellExecutorTool):
        self._shell = shell

    @property
    def name(self) -> str:
        return "test_run"

    @property
    def description(self) -> str:
        return "Run test suites (pytest, jest, etc.), collect results, and analyze coverage."

    @property
    def parameters(self) -> list[ToolParameter]:
        return [
            ToolParameter("test_command", "string", "Test command to run (e.g., 'pytest tests/')", required=False),
            ToolParameter("project_path", "string", "Project root path"),
            ToolParameter("coverage", "boolean", "Enable coverage analysis", required=False),
        ]

    async def execute(
        self,
        project_path: str,
        test_command: Optional[str] = None,
        coverage: bool = True,
    ) -> ToolResult:
        # 自动检测测试框架
        if not test_command:
            test_command = await self._detect_test_framework(project_path)

        if not test_command:
            return ToolResult(
                success=False,
                error="Could not detect test framework. Please specify test_command.",
            )

        # 添加覆盖率选项
        if coverage:
            test_command = self._add_coverage_flag(test_command)

        logger.info(f"Running tests: {test_command}")
        result = await self._shell.execute(command=test_command, working_dir=project_path)

        # 解析测试结果
        test_result = self._parse_results(result.output, result.data.get("stderr", ""))

        return ToolResult(
            success=result.success and test_result.failed == 0,
            output=result.output,
            data={
                "test_result": test_result.model_dump(),
                "raw_output": result.output,
            },
            error=result.error if not result.success else None,
        )

    async def _detect_test_framework(self, project_path: str) -> Optional[str]:
        """自动检测项目使用的测试框架"""
        p = Path(project_path)

        # Python: pytest
        if (p / "pytest.ini").exists() or (p / "pyproject.toml").exists() or (p / "tests").exists():
            return "python -m pytest tests/ -v"

        # Node.js: jest / vitest
        pkg_json = p / "package.json"
        if pkg_json.exists():
            try:
                pkg = json.loads(pkg_json.read_text(encoding="utf-8"))
                scripts = pkg.get("scripts", {})
                if "test" in scripts:
                    return "npm test"
            except Exception:
                pass

        # Go
        if (p / "go.mod").exists():
            return "go test ./... -v"

        # Rust
        if (p / "Cargo.toml").exists():
            return "cargo test"

        return None

    def _add_coverage_flag(self, command: str) -> str:
        """为测试命令添加覆盖率标记"""
        if "pytest" in command and "--cov" not in command:
            return command + " --cov --cov-report=term-missing"
        return command

    def _parse_results(self, stdout: str, stderr: str) -> TestResult:
        """解析测试输出为结构化结果"""
        combined = stdout + "\n" + stderr
        result = TestResult()

        # pytest 格式: "5 passed, 2 failed, 1 skipped"
        pytest_match = re.search(
            r"(\d+) passed(?:, (\d+) failed)?(?:, (\d+) skipped)?",
            combined,
        )
        if pytest_match:
            result.passed = int(pytest_match.group(1) or 0)
            result.failed = int(pytest_match.group(2) or 0)
            result.skipped = int(pytest_match.group(3) or 0)
            result.total = result.passed + result.failed + result.skipped

        # 覆盖率: "TOTAL ... 85%"
        cov_match = re.search(r"TOTAL\s+\d+\s+\d+\s+(\d+)%", combined)
        if cov_match:
            result.coverage_percent = float(cov_match.group(1))

        # 收集失败详情
        fail_blocks = re.findall(r"FAILED (.+?) - (.+?)(?:\n|$)", combined)
        for test_name, reason in fail_blocks:
            result.failures.append({"test": test_name, "reason": reason})

        return result
