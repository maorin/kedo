"""
静态检查工具 — T1 编译期严格化的补充

(虚拟测试三层方案 Phase A)

按项目类型自动选 checker：
  - C/C++:   clang-tidy（首选）/ cppcheck（兜底）
  - Python:  pyright（首选）/ mypy（兜底）
  - Rust:    cargo clippy
  - JS/TS:   eslint / tsc --noEmit

LLM 不需要 build 也能调，专门定位"声明缺失 / 签名漂移 / 未初始化 / 类型错"。
"""
from __future__ import annotations

import logging
import os
import re
import shutil
import subprocess
from pathlib import Path
from typing import Optional

from tools.base import BaseTool, ToolParameter, ToolResult

logger = logging.getLogger(__name__)


# 输出截断上限（避免淹没 LLM context）
MAX_OUTPUT_CHARS = 6000


class StaticCheckTool(BaseTool):
    """静态检查工具 — clang-tidy / pyright / cppcheck / eslint wrapper"""

    def __init__(self, profile_manager=None):
        self._profile_manager = profile_manager

    @property
    def name(self) -> str:
        return "static_check"

    @property
    def description(self) -> str:
        return (
            "Run static analysis on source files (clang-tidy/pyright/cppcheck/eslint). "
            "Catches declaration mismatches, type errors, uninitialized variables, "
            "and other bugs without running the build. "
            "Use this BEFORE 'build' for fast feedback on individual files."
        )

    @property
    def parameters(self) -> list[ToolParameter]:
        return [
            ToolParameter("project_path", "string", "Project root directory"),
            ToolParameter(
                "files",
                "string",
                "Comma-separated file paths (relative to project_path) to check, "
                "or empty to auto-detect changed source files.",
                required=False,
            ),
            ToolParameter(
                "checker",
                "string",
                "Override checker: clang-tidy|cppcheck|pyright|mypy|clippy|eslint|tsc|auto",
                required=False,
            ),
        ]

    async def execute(
        self,
        project_path: str,
        files: Optional[str] = None,
        checker: str = "auto",
    ) -> ToolResult:
        project_path = os.path.abspath(project_path)
        proj = Path(project_path)
        if not proj.is_dir():
            return ToolResult(success=False, error=f"Project path not found: {project_path}")

        target_files = self._resolve_files(proj, files)
        chosen = self._pick_checker(proj, target_files, checker)
        if chosen is None:
            return ToolResult(
                success=False,
                error=(
                    "No suitable static checker available. "
                    "Tried: clang-tidy, cppcheck, pyright, mypy, cargo, eslint, tsc. "
                    "Install one of them or specify `checker` explicitly."
                ),
            )

        checker_name, cmd = chosen
        logger.info(f"static_check[{checker_name}]: {cmd}")
        try:
            result = subprocess.run(
                cmd,
                shell=True,
                cwd=project_path,
                capture_output=True,
                text=True,
                timeout=120,
            )
        except subprocess.TimeoutExpired:
            return ToolResult(
                success=False,
                error=f"static_check[{checker_name}] timed out (120s)",
            )
        except Exception as e:
            return ToolResult(success=False, error=f"static_check error: {e}")

        stdout = result.stdout or ""
        stderr = result.stderr or ""
        combined = (stdout + ("\n" + stderr if stderr else "")).strip()
        truncated = combined[-MAX_OUTPUT_CHARS:] if len(combined) > MAX_OUTPUT_CHARS else combined

        # 按 returncode 区分：0 = 全过；非 0 = 有发现 / 工具错
        # 但 returncode 不一定可靠（pyright 没 issue 也可能 != 0），所以 success 改为
        # "工具能跑起来"，让 LLM 看 output 自己判断
        issues = self._count_issues(checker_name, combined)
        ok = result.returncode == 0 and issues == 0

        header = f"[{checker_name}] returncode={result.returncode}, issues_found≈{issues}"
        if ok:
            output_text = f"{header}\n(no issues)"
        else:
            output_text = f"{header}\n{truncated}"

        return ToolResult(
            success=ok,
            output=output_text,
            data={
                "checker": checker_name,
                "command": cmd,
                "returncode": result.returncode,
                "issues": issues,
            },
            error=None if ok else f"{checker_name} reported {issues} issue(s)",
        )

    # ----------------------------------------------------------
    # Helpers
    # ----------------------------------------------------------

    @staticmethod
    def _resolve_files(proj: Path, files_arg: Optional[str]) -> list[str]:
        if not files_arg:
            return []
        return [f.strip() for f in files_arg.split(",") if f.strip()]

    def _pick_checker(
        self,
        proj: Path,
        target_files: list[str],
        checker: str,
    ) -> Optional[tuple[str, str]]:
        """
        返回 (checker_name, full_shell_command) 或 None。
        """
        # 项目类型探测
        has_c = any(proj.glob("*.c")) or any(proj.glob("source/*.c"))
        has_cpp = any(proj.glob("*.cpp")) or any(proj.glob("source/*.cpp")) or any(proj.glob("*.cc"))
        has_cmake = (proj / "CMakeLists.txt").exists()
        has_make = (proj / "Makefile").exists()
        has_py = (proj / "pyproject.toml").exists() or any(proj.glob("*.py"))
        has_rust = (proj / "Cargo.toml").exists()
        has_pkg = (proj / "package.json").exists()
        has_ts = (proj / "tsconfig.json").exists()

        # 显式选 checker
        if checker and checker != "auto":
            return self._build_command(checker, proj, target_files)

        # 自动选：按目标文件后缀优先
        if target_files:
            exts = {Path(f).suffix.lower() for f in target_files}
            if exts & {".c", ".cpp", ".cc", ".h", ".hpp"}:
                return self._first_available(["clang-tidy", "cppcheck"], proj, target_files)
            if exts & {".py"}:
                return self._first_available(["pyright", "mypy"], proj, target_files)
            if exts & {".rs"}:
                return self._first_available(["clippy"], proj, target_files)
            if exts & {".ts", ".tsx", ".js", ".jsx"}:
                return self._first_available(["eslint", "tsc"], proj, target_files)

        # 没指定文件：按项目类型
        if has_c or has_cpp or has_cmake or has_make:
            return self._first_available(["clang-tidy", "cppcheck"], proj, target_files)
        if has_rust:
            return self._first_available(["clippy"], proj, target_files)
        if has_py:
            return self._first_available(["pyright", "mypy"], proj, target_files)
        if has_pkg or has_ts:
            return self._first_available(["eslint", "tsc"], proj, target_files)
        return None

    def _first_available(
        self,
        names: list[str],
        proj: Path,
        target_files: list[str],
    ) -> Optional[tuple[str, str]]:
        for name in names:
            built = self._build_command(name, proj, target_files)
            if built is not None:
                return built
        return None

    @staticmethod
    def _build_command(
        name: str,
        proj: Path,
        target_files: list[str],
    ) -> Optional[tuple[str, str]]:
        """
        如果 checker binary 存在 → 返回 (name, cmd)；否则 None。
        所有命令都用 `--quiet`/输出到 stdout 的形式，避免污染太多噪音。
        """
        files_arg = " ".join(target_files) if target_files else ""

        if name == "clang-tidy":
            if not shutil.which("clang-tidy"):
                return None
            target = files_arg or _glob_sources(proj, [".c", ".cpp", ".cc"], cap=20)
            if not target:
                return None
            # -p build → 用 compile_commands.json（如果有），没有也能跑；--quiet 减少 banner
            cmd = f"clang-tidy --quiet -p build {target}"
            return ("clang-tidy", cmd)

        if name == "cppcheck":
            if not shutil.which("cppcheck"):
                return None
            target = files_arg or _glob_sources(proj, [".c", ".cpp", ".cc"], cap=50) or "."
            cmd = f"cppcheck --enable=warning,style,performance --inline-suppr --quiet {target}"
            return ("cppcheck", cmd)

        if name == "pyright":
            if not shutil.which("pyright"):
                return None
            target = files_arg or "."
            cmd = f"pyright --outputjson {target} 2>/dev/null || pyright {target}"
            return ("pyright", cmd)

        if name == "mypy":
            if not shutil.which("mypy"):
                return None
            target = files_arg or "."
            cmd = f"mypy --no-error-summary {target}"
            return ("mypy", cmd)

        if name == "clippy":
            if not shutil.which("cargo"):
                return None
            cmd = "cargo clippy --quiet --no-deps -- -W clippy::all"
            return ("clippy", cmd)

        if name == "eslint":
            if not shutil.which("eslint") and not (proj / "node_modules" / ".bin" / "eslint").exists():
                return None
            bin_path = "./node_modules/.bin/eslint" if (proj / "node_modules" / ".bin" / "eslint").exists() else "eslint"
            target = files_arg or "."
            cmd = f"{bin_path} {target}"
            return ("eslint", cmd)

        if name == "tsc":
            if not shutil.which("tsc") and not (proj / "node_modules" / ".bin" / "tsc").exists():
                return None
            bin_path = "./node_modules/.bin/tsc" if (proj / "node_modules" / ".bin" / "tsc").exists() else "tsc"
            cmd = f"{bin_path} --noEmit"
            return ("tsc", cmd)

        return None

    @staticmethod
    def _count_issues(checker: str, output: str) -> int:
        """粗略估算 issue 数：每行一条。"""
        if not output:
            return 0
        if checker in ("clang-tidy", "cppcheck"):
            return len(re.findall(r":\d+:\d*:?\s*(warning|error|note):", output))
        if checker == "pyright":
            m = re.search(r"(\d+)\s+errors?,\s+(\d+)\s+warnings?", output)
            if m:
                return int(m.group(1)) + int(m.group(2))
        if checker == "mypy":
            return output.count(": error:") + output.count(": note:")
        if checker == "clippy":
            return output.count("warning:") + output.count("error:")
        if checker in ("eslint", "tsc"):
            return len(re.findall(r":\s*(error|warning)\s+", output))
        # 兜底
        return output.count("warning") + output.count("error")


def _glob_sources(proj: Path, exts: list[str], cap: int = 20) -> str:
    """收集项目内的源文件路径（相对路径），最多 cap 个。"""
    found: list[str] = []
    for ext in exts:
        for f in proj.rglob(f"*{ext}"):
            if "build" in f.parts or "node_modules" in f.parts or ".kedo" in f.parts:
                continue
            try:
                rel = f.relative_to(proj)
            except ValueError:
                continue
            found.append(str(rel))
            if len(found) >= cap:
                break
        if len(found) >= cap:
            break
    return " ".join(found)
