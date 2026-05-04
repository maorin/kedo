"""
T3 真模拟器测试工具（虚拟测试三层方案 Phase C）

只在 commit_candidate 关卡跑一次，不参与 ReAct 主循环（启动 5-30s + 跑 30s + 退出
≈ 40-90s/次，会拖死反馈速度）。专门抓 T1/T2 抓不到的"平台特定 svcBreak / 服务交互
/ GPU 真渲染"类 bug。

由 profile.emulator 驱动：
  {
    "enabled": true,
    "command_template": "xvfb-run -a ryujinx --headless {artifact}",
    "timeout_s": 90,
    "success_patterns": ["main loop entered"],
    "crash_patterns": ["svcBreak", "Result code 0x[0-9a-f]+", "panic"],
    "required": false   # true → emulator 失败阻塞 commit_candidate；false → 仅 warning
  }

emulator binary 不存在时优雅降级：required=true 才 fail，否则 success+skipped。
"""
from __future__ import annotations

import logging
import os
import re
import shlex
import subprocess
from pathlib import Path
from typing import Optional

from tools.base import BaseTool, ToolParameter, ToolResult

logger = logging.getLogger(__name__)


DEFAULT_TIMEOUT_S = 90
MAX_OUTPUT_CHARS = 6000


class EmulatorTestTool(BaseTool):
    """T3 真模拟器测试 — Ryubing/QEMU/AVD 等 headless 启动 + 模式匹配"""

    def __init__(self, profile_manager=None, llm_client=None):
        self._profile_manager = profile_manager
        self._llm_client = llm_client

    @property
    def name(self) -> str:
        return "emulator_test"

    @property
    def description(self) -> str:
        return (
            "Run the build artifact in a real platform emulator (Ryubing for Switch, "
            "QEMU for embedded ARM, Android AVD, etc.) and check for crash signatures. "
            "Slow (40-90s/run) — typically called once before commit_candidate, NOT "
            "in the ReAct loop. Configured via profile.emulator. "
            "If profile.emulator.enabled is false or the emulator binary is missing, "
            "this tool returns success+skipped (unless profile.emulator.required=true)."
        )

    @property
    def parameters(self) -> list[ToolParameter]:
        return [
            ToolParameter("project_path", "string", "Project root directory"),
            ToolParameter(
                "artifact_path",
                "string",
                "Path to the build artifact (e.g., build/foo.nro). Auto-detected "
                "from profile.build.artifacts glob if omitted.",
                required=False,
            ),
        ]

    async def execute(
        self,
        project_path: str,
        artifact_path: Optional[str] = None,
    ) -> ToolResult:
        project_path = os.path.abspath(project_path)
        if not Path(project_path).is_dir():
            return ToolResult(success=False, error=f"Project path not found: {project_path}")

        # 读 profile.emulator + 找 artifact
        em_cfg: dict = {}
        artifacts_glob: list[str] = []
        if self._profile_manager:
            try:
                profile = await self._profile_manager.ensure(
                    project_path, self._llm_client
                )
                if profile:
                    em_cfg = profile.get("emulator") or {}
                    artifacts_glob = ((profile.get("build") or {}).get("artifacts") or [])
            except Exception as e:
                logger.warning(f"emulator_test: profile ensure failed: {e}")

        required = bool(em_cfg.get("required"))

        if not em_cfg.get("enabled"):
            return ToolResult(
                success=True,
                output="emulator_test: not enabled in profile.emulator (skipped).",
                data={"skipped": True, "reason": "not_enabled"},
            )

        template = em_cfg.get("command_template") or ""
        if not template:
            return ToolResult(
                success=not required,
                output="emulator_test: profile.emulator.command_template empty (skipped).",
                data={"skipped": True, "reason": "no_command_template"},
                error="profile.emulator.command_template is empty" if required else None,
            )

        # 解析 artifact 路径
        if not artifact_path:
            artifact_path = self._resolve_artifact(project_path, artifacts_glob)
        if not artifact_path:
            return ToolResult(
                success=not required,
                output=(
                    "emulator_test: no artifact found "
                    f"(profile.build.artifacts={artifacts_glob}). Skipped."
                ),
                data={"skipped": True, "reason": "no_artifact"},
                error="No build artifact found to run in emulator" if required else None,
            )

        # 校验 emulator binary 存在（取 command 第一段，去掉 xvfb-run 等 wrapper）
        cmd = template.replace("{artifact}", shlex.quote(artifact_path))
        cmd = os.path.expandvars(cmd)
        if not self._emulator_binary_exists(cmd):
            msg = (
                f"emulator_test: emulator binary not found in PATH. Command: {cmd}\n"
                f"Install the emulator (Ryubing/QEMU/AVD) on this host first."
            )
            return ToolResult(
                success=not required,
                output=msg,
                data={"skipped": True, "reason": "binary_missing", "command": cmd},
                error=msg if required else None,
            )

        timeout = int(em_cfg.get("timeout_s") or DEFAULT_TIMEOUT_S)
        success_pats = self._compile_patterns(em_cfg.get("success_patterns") or [])
        crash_pats = self._compile_patterns(em_cfg.get("crash_patterns") or [])

        logger.info(f"emulator_test: running '{cmd}' (timeout {timeout}s)")
        try:
            result = subprocess.run(
                cmd,
                shell=True,
                cwd=project_path,
                capture_output=True,
                text=True,
                timeout=timeout,
            )
            timed_out = False
            stdout = result.stdout or ""
            stderr = result.stderr or ""
            returncode = result.returncode
        except subprocess.TimeoutExpired as e:
            timed_out = True
            stdout = (e.stdout or b"").decode("utf-8", errors="replace") if isinstance(e.stdout, bytes) else (e.stdout or "")
            stderr = (e.stderr or b"").decode("utf-8", errors="replace") if isinstance(e.stderr, bytes) else (e.stderr or "")
            returncode = -1
        except Exception as e:
            return ToolResult(success=False, error=f"emulator_test error: {e}")

        combined = stdout + "\n" + stderr
        crash_hits = self._scan_patterns(combined, crash_pats)
        success_hits = self._scan_patterns(combined, success_pats)

        # 判定逻辑：
        #   - crash 命中 → fail
        #   - success_patterns 非空且未命中 → fail（启动了但没到目标状态）
        #   - success_patterns 空 → 只看 crash + returncode
        ok = not crash_hits
        if success_pats and not success_hits:
            ok = False
        # timeout 本身不是失败：emulator 经常需要外部 kill；timeout=ok 当作"跑满了
        # 没崩"。除非用户在 success_patterns 里写了启动标记。
        if timed_out and (success_pats and not success_hits):
            ok = False

        head = [
            f"emulator_test: returncode={returncode} timeout={'yes' if timed_out else 'no'} "
            f"crashes={len(crash_hits)} success_marks={len(success_hits)}"
        ]
        if crash_hits:
            head.append("--- Crash signatures hit ---")
            head.extend(f"  {h}" for h in crash_hits[:5])
        if success_hits:
            head.append("--- Success markers hit ---")
            head.extend(f"  {h}" for h in success_hits[:3])
        tail = combined[-2000:]
        output_text = "\n".join(head) + ("\n--- output tail ---\n" + tail if tail.strip() else "")
        output_text = output_text[-MAX_OUTPUT_CHARS:]

        err_msg = None
        if not ok:
            if crash_hits:
                err_msg = f"emulator FAILED — crash detected: {crash_hits[0]}"
            elif success_pats and not success_hits:
                err_msg = (
                    f"emulator FAILED — none of expected success_patterns hit "
                    f"({success_pats[0].pattern[:60]} ...). The artifact ran but never "
                    f"reached the expected state."
                )

        return ToolResult(
            success=ok,
            output=output_text,
            data={
                "command": cmd,
                "artifact_path": artifact_path,
                "returncode": returncode,
                "timed_out": timed_out,
                "crash_hits": crash_hits[:5],
                "success_hits": success_hits[:3],
                "required": required,
            },
            error=err_msg,
        )

    # ----------------------------------------------------------
    # Helpers
    # ----------------------------------------------------------

    @staticmethod
    def _resolve_artifact(project_path: str, globs: list[str]) -> Optional[str]:
        """从 profile.build.artifacts glob 列表里找一个真实文件。"""
        proj = Path(project_path)
        for pat in globs:
            pat = os.path.expandvars(pat)
            for hit in sorted(proj.glob(pat)):
                if hit.is_file():
                    return str(hit)
        return None

    @staticmethod
    def _emulator_binary_exists(cmd: str) -> bool:
        """
        粗略校验：取 command 第一段（剥离 xvfb-run/sudo 等常见 wrapper），
        看是否在 PATH 或绝对路径存在。失败时返回 False，让上层走"跳过"路径。
        """
        try:
            tokens = shlex.split(cmd)
        except Exception:
            return True  # 不可解析就别拦，让 subprocess 自己报错
        if not tokens:
            return False
        # 跳过 wrapper
        wrappers = {"xvfb-run", "sudo", "env", "nohup", "timeout"}
        idx = 0
        while idx < len(tokens) and tokens[idx] in wrappers:
            idx += 1
            # wrapper 通常带 flag，跳过 -x 形式
            while idx < len(tokens) and tokens[idx].startswith("-"):
                idx += 1
                if idx < len(tokens) and tokens[idx - 1] in ("-a", "-s"):
                    # xvfb-run -s "..." 形式：把下一个值也跳掉
                    idx += 1
        if idx >= len(tokens):
            return False
        binary = tokens[idx]
        if "/" in binary:
            return Path(binary).exists()
        # PATH 查找
        from shutil import which
        return which(binary) is not None

    @staticmethod
    def _compile_patterns(patterns: list[str]) -> list[re.Pattern]:
        out: list[re.Pattern] = []
        for p in patterns:
            if not isinstance(p, str) or not p:
                continue
            try:
                out.append(re.compile(p, re.IGNORECASE))
            except re.error:
                # 当字面量处理
                out.append(re.compile(re.escape(p), re.IGNORECASE))
        return out

    @staticmethod
    def _scan_patterns(text: str, patterns: list[re.Pattern]) -> list[str]:
        if not patterns or not text:
            return []
        hits: list[str] = []
        for line in text.splitlines():
            for pat in patterns:
                m = pat.search(line)
                if m:
                    hits.append(line.strip()[:200])
                    break
        return hits
