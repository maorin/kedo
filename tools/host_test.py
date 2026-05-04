"""
T2 宿主机 mock + ASAN 测试工具（虚拟测试三层方案 Phase B）

思路：
  - 在 host (Linux/Mac) 上用 mock 桩编译 + 运行业务代码的子集，开 ASAN/UBSAN
  - 抓 null deref / buffer overflow / use-after-free / 未初始化读
  - 跨编译类项目（Switch/embedded）build 通过后自动跑一次，10s 内反馈给 LLM
  - 反馈格式：ASAN 报告里的 file:line:col → LLM 下一轮 prompt

由 profile.host_test 驱动：
  {
    "enabled": true,
    "mock_dir": "tests/host_mock",
    "build_command": "gcc -fsanitize=address,undefined ...",
    "run_command": "./host_test",
    "expected_exit_code": 0,
    "timeout_s": 30,
    "auto_run_after_build": true
  }
"""
from __future__ import annotations

import logging
import os
import re
import subprocess
from pathlib import Path
from typing import Optional

from tools.base import BaseTool, ToolParameter, ToolResult

logger = logging.getLogger(__name__)


# ASAN/UBSAN 报告关键标记
SANITIZER_HEADER_RE = re.compile(
    r"==\d+==(ERROR|WARNING):\s+(AddressSanitizer|UndefinedBehaviorSanitizer|LeakSanitizer)"
)
# stack trace 行：    #0 0x... in foo /path:42
STACK_FRAME_RE = re.compile(
    r"^\s*#\d+\s+0x[0-9a-fA-F]+\s+in\s+\S+\s+(\S+:\d+(?::\d+)?)"
)
# 简短诊断："SUMMARY: AddressSanitizer: heap-buffer-overflow ..."
SUMMARY_RE = re.compile(r"^SUMMARY:\s+(\S+Sanitizer):\s+(.+)$", re.MULTILINE)

MAX_OUTPUT_CHARS = 6000
DEFAULT_TIMEOUT_S = 30


class HostTestTool(BaseTool):
    """T2 宿主机 mock 测试 — 编译 mock 变体 + ASAN 跑一遍"""

    def __init__(self, profile_manager=None, llm_client=None):
        self._profile_manager = profile_manager
        self._llm_client = llm_client

    @property
    def name(self) -> str:
        return "host_test"

    @property
    def description(self) -> str:
        return (
            "Run host-side mock test with AddressSanitizer/UBSAN. "
            "Compiles a mock variant of the project on the host (Linux/Mac) using "
            "stubbed platform APIs, then runs it under sanitizers to catch null "
            "dereferences, buffer overflows, use-after-free, and uninitialized reads. "
            "For cross-compiled projects (Switch/embedded), this is the fastest way "
            "to catch memory bugs without flashing real hardware. "
            "Configured via profile.host_test."
        )

    @property
    def parameters(self) -> list[ToolParameter]:
        return [
            ToolParameter("project_path", "string", "Project root directory"),
        ]

    async def execute(self, project_path: str) -> ToolResult:
        project_path = os.path.abspath(project_path)
        if not Path(project_path).is_dir():
            return ToolResult(success=False, error=f"Project path not found: {project_path}")

        # 读 profile.host_test
        host_cfg: dict = {}
        if self._profile_manager:
            try:
                profile = await self._profile_manager.ensure(
                    project_path, self._llm_client
                )
                if profile:
                    host_cfg = profile.get("host_test") or {}
            except Exception as e:
                logger.warning(f"host_test: profile ensure failed: {e}")

        if not host_cfg.get("enabled"):
            return ToolResult(
                success=True,  # 未启用 ≠ 失败
                output="host_test: not enabled in profile.host_test (skipped).",
                data={"skipped": True, "reason": "not_enabled"},
            )

        build_cmd = host_cfg.get("build_command") or ""
        run_cmd = host_cfg.get("run_command") or ""
        if not build_cmd or not run_cmd:
            return ToolResult(
                success=False,
                error=(
                    "host_test: profile.host_test missing build_command or run_command. "
                    "Set them in .kedo/project_profile.json before running."
                ),
            )

        mock_dir = host_cfg.get("mock_dir") or "."
        cwd = str((Path(project_path) / mock_dir).resolve())
        if not Path(cwd).is_dir():
            return ToolResult(
                success=False,
                error=f"host_test: mock_dir not found: {cwd}",
            )

        timeout = int(host_cfg.get("timeout_s") or DEFAULT_TIMEOUT_S)
        expected_exit = int(host_cfg.get("expected_exit_code") if host_cfg.get("expected_exit_code") is not None else 0)

        # 准备 ASAN 友好的 env：
        #   - ASAN_OPTIONS: 关闭 leak detector（启动子进程时常误报）+ abort_on_error
        #   - UBSAN_OPTIONS: print_stacktrace=1 给 LLM 行号
        #   - LSAN_OPTIONS: detect_leaks=0
        run_env = os.environ.copy()
        run_env.setdefault("ASAN_OPTIONS", "abort_on_error=0:halt_on_error=0:detect_leaks=0:print_full_thread_history=0")
        run_env.setdefault("UBSAN_OPTIONS", "print_stacktrace=1:halt_on_error=0")
        run_env.setdefault("LSAN_OPTIONS", "detect_leaks=0")

        # 1) 编译 mock 变体
        try:
            build_res = subprocess.run(
                build_cmd,
                shell=True,
                cwd=cwd,
                capture_output=True,
                text=True,
                timeout=timeout,
                env=run_env,
            )
        except subprocess.TimeoutExpired:
            return ToolResult(
                success=False,
                error=f"host_test: build timed out ({timeout}s) | command: {build_cmd}",
            )
        except Exception as e:
            return ToolResult(success=False, error=f"host_test: build error: {e}")

        if build_res.returncode != 0:
            stderr = (build_res.stderr or "")[-MAX_OUTPUT_CHARS:]
            stdout = (build_res.stdout or "")[-1000:]
            return ToolResult(
                success=False,
                error=(
                    f"host_test: mock build failed (exit {build_res.returncode}).\n"
                    f"--- stderr ---\n{stderr}\n--- stdout ---\n{stdout}"
                ),
                data={"phase": "build", "returncode": build_res.returncode},
            )

        # 2) 运行
        try:
            run_res = subprocess.run(
                run_cmd,
                shell=True,
                cwd=cwd,
                capture_output=True,
                text=True,
                timeout=timeout,
                env=run_env,
            )
        except subprocess.TimeoutExpired:
            return ToolResult(
                success=False,
                error=(
                    f"host_test: run timed out ({timeout}s) | command: {run_cmd}\n"
                    f"This usually means an infinite loop or deadlock in the code."
                ),
                data={"phase": "run", "timeout": True},
            )
        except Exception as e:
            return ToolResult(success=False, error=f"host_test: run error: {e}")

        stdout = run_res.stdout or ""
        stderr = run_res.stderr or ""
        report = self._extract_sanitizer_report(stdout, stderr)
        ok_exit = run_res.returncode == expected_exit
        ok = ok_exit and not report["fatal"]

        # 输出格式：先一句结论，然后 sanitizer 报告（如有），最后保留尾部 stdout/stderr
        head_lines = [
            f"host_test: returncode={run_res.returncode} (expected={expected_exit})",
        ]
        if report["fatal"]:
            head_lines.append(
                f"!! Sanitizer report: {report['summary']} "
                f"(at {report['first_frame'] or 'unknown'})"
            )
        if report["frames"]:
            head_lines.append("--- Sanitizer frames (top 10) ---")
            head_lines.extend(report["frames"][:10])

        tail = (stderr or stdout)[-2000:]
        output_text = "\n".join(head_lines) + ("\n--- tail ---\n" + tail if tail.strip() else "")
        output_text = output_text[-MAX_OUTPUT_CHARS:]

        return ToolResult(
            success=ok,
            output=output_text,
            data={
                "phase": "run",
                "returncode": run_res.returncode,
                "expected_exit_code": expected_exit,
                "sanitizer_summary": report["summary"],
                "sanitizer_frames": report["frames"][:10],
                "fatal": report["fatal"],
            },
            error=None if ok else self._compose_error(run_res.returncode, expected_exit, report),
        )

    @staticmethod
    def _extract_sanitizer_report(stdout: str, stderr: str) -> dict:
        """
        从 ASAN/UBSAN 输出抠出关键诊断。
        返回:
          {
            "fatal": bool,        # 是否检测到 sanitizer 报错
            "summary": str,       # 简短描述（heap-buffer-overflow on address 0x...）
            "frames": list[str],  # file:line 列表
            "first_frame": str|None,
          }
        """
        combined = (stderr or "") + "\n" + (stdout or "")
        fatal = bool(SANITIZER_HEADER_RE.search(combined))

        summary = ""
        m = SUMMARY_RE.search(combined)
        if m:
            summary = f"{m.group(1)}: {m.group(2).strip()}"

        frames: list[str] = []
        for line in combined.splitlines():
            mf = STACK_FRAME_RE.match(line)
            if mf:
                frames.append(line.strip())

        first_frame = frames[0] if frames else None
        return {
            "fatal": fatal,
            "summary": summary,
            "frames": frames,
            "first_frame": first_frame,
        }

    @staticmethod
    def _compose_error(returncode: int, expected: int, report: dict) -> str:
        if report["fatal"]:
            return (
                f"host_test FAILED — sanitizer detected: {report['summary'] or 'unknown'} "
                f"(at {report['first_frame'] or 'unknown frame'})"
            )
        if returncode != expected:
            return f"host_test FAILED — exit {returncode} (expected {expected})"
        return "host_test FAILED"
