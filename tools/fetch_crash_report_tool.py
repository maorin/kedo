"""
FetchCrashReportTool — 从 Switch 拉 Atmosphere crash_reports 并 addr2line 解析

T3 真模拟器搁置后真机崩溃定位的实用替代。流程：

  1. LLM 调本工具（build 通过但实机有问题时主动调）
  2. 检查 .kedo/crash_reports/ 已有 .log（snapshot 一份本地集合 S0）
  3. 读 .kedo/device.json 拿默认 IP/port/user/pass（有则填进 dashboard 卡片）
  4. emit DISCUSSION_STARTED + DISCUSSION_PROPOSALS（kind=coredump_fetch）→ 阻塞等用户 ack
  5. 用户 dashboard "准备好"按钮 + 填写连接信息 → POST /api/coredump/{task_id}/ack
  6. 用 ftplib 列远端 /atmosphere/crash_reports/，下载所有 *.log（dedupe by name）
  7. S1 = 当前本地 .log 集合；新增 = sorted(S1 - S0, reverse=True)
  8. 取最新（字典序最大）那个 → coredump_parser.parse_log_file 解析 + addr2line
  9. emit COREDUMP_ANALYZED 让 dashboard 显示崩点
 10. 返回 ToolResult.data = {crash_site, frames, ...} 让 LLM 知道下一步去改哪行

时间错乱处理：Switch 时间是 2024，文件名前缀是 unix epoch 但单调递增，所以**字典序排序**
即等同时间排序（前缀长度固定 11 位）。

约定：
  - .kedo/crash_reports/ 目录累积保留所有，不自动清
  - 本地已有同名文件直接跳过下载（FTP 协议无 mtime 可信对比）
  - "记住连接信息"勾选时写 .kedo/device.json，下次预填
"""
from __future__ import annotations

import asyncio
import ftplib
import json
import logging
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from tools.base import BaseTool, ToolParameter, ToolResult
from core.coredump_parser import (
    parse_log_file,
    format_for_llm,
    find_addr2line,
    find_business_frame,
)

logger = logging.getLogger(__name__)


# 模块级 queue 注册表（一个 task 一个 queue）
_COREDUMP_QUEUES: dict[str, asyncio.Queue] = {}
_COREDUMP_PENDING: dict[str, dict] = {}  # task_id → 当前等待中的 request payload（dashboard 启动拉历史用）


def get_coredump_queue(task_id: str) -> asyncio.Queue:
    if task_id not in _COREDUMP_QUEUES:
        _COREDUMP_QUEUES[task_id] = asyncio.Queue()
    return _COREDUMP_QUEUES[task_id]


def list_pending_requests() -> dict[str, dict]:
    """dashboard 启动时调 GET /api/coredump/pending 拉当前等待。"""
    return dict(_COREDUMP_PENDING)


def _device_json_path(project_path: str) -> Path:
    return Path(project_path) / ".kedo" / "device.json"


def _load_device_defaults(project_path: str) -> dict:
    p = _device_json_path(project_path)
    if not p.is_file():
        return {}
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception as e:
        logger.warning(f"device.json parse failed: {e}")
        return {}


def _save_device_defaults(project_path: str, info: dict) -> None:
    p = _device_json_path(project_path)
    p.parent.mkdir(parents=True, exist_ok=True)
    safe = {
        "switch_ip": info.get("switch_ip", ""),
        "ftp_port": int(info.get("ftp_port", 5000)),
        "ftp_user": info.get("ftp_user", ""),
        # 注意：密码也写文件 — ftpd-pro 一般无密码或弱密码，权限设 0600
        "ftp_pass": info.get("ftp_pass", ""),
        "remote_path": info.get("remote_path", "/atmosphere/crash_reports/"),
        "saved_at": datetime.now(timezone.utc).isoformat(),
    }
    p.write_text(json.dumps(safe, indent=2, ensure_ascii=False), encoding="utf-8")
    try:
        p.chmod(0o600)
    except Exception:
        pass


def _local_logs(project_path: str) -> set[str]:
    d = Path(project_path) / ".kedo" / "crash_reports"
    if not d.is_dir():
        return set()
    return {p.name for p in d.iterdir() if p.suffix == ".log"}


def _ftp_fetch_logs(
    host: str, port: int, user: str, password: str,
    remote_path: str, local_dir: Path,
    timeout_s: int = 30,
) -> tuple[list[str], list[str]]:
    """连 FTP 拉所有 *.log。返回 (downloaded_filenames, error_messages)。
    本地已存在同名文件跳过（按名 dedupe）。"""
    local_dir.mkdir(parents=True, exist_ok=True)
    downloaded: list[str] = []
    errors: list[str] = []
    ftp = ftplib.FTP()
    try:
        ftp.connect(host=host, port=port, timeout=timeout_s)
        ftp.login(user or "anonymous", password or "")
    except Exception as e:
        return ([], [f"connect/login failed: {e}"])

    try:
        try:
            ftp.cwd(remote_path)
        except ftplib.error_perm as e:
            return ([], [f"cwd {remote_path}: {e}"])

        names: list[str] = []
        try:
            names = ftp.nlst()
        except ftplib.error_perm as e:
            return ([], [f"nlst {remote_path}: {e}"])

        log_names = sorted([n for n in names if n.endswith(".log")])
        existing = {p.name for p in local_dir.iterdir() if p.is_file()}
        for fn in log_names:
            if fn in existing:
                continue
            local_path = local_dir / fn
            try:
                with open(local_path, "wb") as fh:
                    ftp.retrbinary(f"RETR {fn}", fh.write)
                downloaded.append(fn)
            except Exception as e:
                errors.append(f"download {fn}: {e}")
                # 残余文件清掉避免下次误以为已下载
                try:
                    local_path.unlink(missing_ok=True)
                except Exception:
                    pass
    finally:
        try:
            ftp.quit()
        except Exception:
            try:
                ftp.close()
            except Exception:
                pass

    return downloaded, errors


def _guess_elf_path(project_path: str, profile: Optional[dict] = None) -> Optional[str]:
    """从 profile.artifact / build/<target>.elf 推 .elf 路径。"""
    if profile:
        art = (profile.get("artifact") or {})
        target = art.get("target_name")
        if target:
            for cand in (
                Path(project_path) / "build" / f"{target}.elf",
                Path(project_path) / "build" / target / f"{target}.elf",
            ):
                if cand.is_file():
                    return str(cand)
    # 兜底：扫 build/*.elf
    build_dir = Path(project_path) / "build"
    if build_dir.is_dir():
        elfs = list(build_dir.glob("*.elf"))
        if elfs:
            elfs.sort(key=lambda p: p.stat().st_mtime, reverse=True)
            return str(elfs[0])
    return None


class FetchCrashReportTool(BaseTool):
    """LLM 工具：拉 Switch 真机崩溃日志 + addr2line 解析。"""

    def __init__(
        self,
        state_manager=None,
        event_bus=None,
        profile_manager=None,
        timeout_s: int = 1800,
    ):
        self._state = state_manager
        self._event_bus = event_bus
        self._profile_manager = profile_manager
        self._timeout_s = timeout_s

    @property
    def name(self) -> str:
        return "fetch_crash_report"

    @property
    def description(self) -> str:
        return (
            "Pull Atmosphere crash_reports from a Switch via FTP and resolve them "
            "with addr2line to function/file:line. Use this when build passed but "
            "the .nro crashes on real hardware (e.g. screen shows 2168-XXXX, app "
            "exits silently, or you suspect runtime svcBreak / Data Abort). "
            "BLOCKS until the user starts ftpd-pro on Switch and confirms on the "
            "dashboard. Returns the most recent crash's structured stack: "
            "{result_code, exception_type, fault_address, crash_site, frames}. "
            "Use the data to locate the offending source line and propose a fix."
        )

    @property
    def parameters(self) -> list[ToolParameter]:
        return [
            ToolParameter(
                "reason", "string",
                "Brief reason for fetching crash reports (shown to user on the "
                "dashboard so they understand why you need them now). Example: "
                "'build passed but Switch shows 2168-0002 fatal — need crash log "
                "to locate the failing call'.",
            ),
            ToolParameter(
                "force_refetch", "boolean",
                "If true, ALWAYS re-fetch from Switch even if local .kedo/crash_reports/ "
                "already has logs. Default false (use cached if any).",
                required=False,
                default=False,
            ),
            ToolParameter(
                "task_id", "string",
                "Active task id (auto-injected).",
                required=False,
            ),
            ToolParameter(
                "project_path", "string",
                "Project root (auto-injected).",
                required=False,
            ),
        ]

    @property
    def is_read_only(self) -> bool:
        return False  # 写入 .kedo/crash_reports/ + 可能写 device.json

    async def execute(
        self,
        reason: str = "",
        force_refetch: bool = False,
        task_id: str = "",
        project_path: str = "",
    ) -> ToolResult:
        if not task_id:
            return ToolResult(success=False, error="task_id required (auto-injection failed)")
        if not project_path:
            return ToolResult(success=False, error="project_path required (auto-injection failed)")

        local_dir = Path(project_path) / ".kedo" / "crash_reports"
        s0 = _local_logs(project_path)

        # Fast path：force_refetch=False 且本地已有 .log → 直接解最新那个，不弹卡片
        if not force_refetch and s0:
            return await self._analyze_local(project_path, task_id, source="cached")

        # 否则走 dashboard ack + ftp 拉取
        defaults = _load_device_defaults(project_path)
        # 默认 remote_path 从 charter 的 forbidden_patterns 里没法推，硬编码
        if "remote_path" not in defaults:
            defaults["remote_path"] = "/atmosphere/crash_reports/"
        if "ftp_port" not in defaults:
            defaults["ftp_port"] = 5000

        await self._publish_request_event(
            task_id=task_id,
            project_path=project_path,
            reason=reason,
            defaults=defaults,
            local_count=len(s0),
        )

        queue = get_coredump_queue(task_id)
        try:
            payload = await asyncio.wait_for(queue.get(), timeout=self._timeout_s)
        except asyncio.TimeoutError:
            self._clear_pending(task_id)
            return ToolResult(
                success=False,
                error=(
                    f"fetch_crash_report timed out after {self._timeout_s}s. "
                    f"User did not confirm on dashboard. Either retry later or "
                    f"call pause_for_human."
                ),
            )
        finally:
            self._clear_pending(task_id)

        action = (payload.get("action") or "").lower()
        if action != "ready":
            return ToolResult(
                success=False,
                error=(
                    f"User cancelled crash report fetch. "
                    f"User note: {payload.get('human_input') or '(none)'}. "
                    f"Either propose a different debugging path or call pause_for_human."
                ),
            )

        # 收用户填的连接信息
        switch_ip = (payload.get("switch_ip") or defaults.get("switch_ip") or "").strip()
        ftp_port = int(payload.get("ftp_port") or defaults.get("ftp_port") or 5000)
        ftp_user = (payload.get("ftp_user") or defaults.get("ftp_user") or "").strip()
        ftp_pass = payload.get("ftp_pass") or defaults.get("ftp_pass") or ""
        remote_path = (payload.get("remote_path") or defaults.get("remote_path")
                       or "/atmosphere/crash_reports/").strip()
        remember = bool(payload.get("remember"))

        if not switch_ip:
            return ToolResult(
                success=False,
                error="Switch IP not provided. Re-run fetch_crash_report after the user enters IP.",
            )

        # 拉
        downloaded, errs = await asyncio.get_event_loop().run_in_executor(
            None, _ftp_fetch_logs,
            switch_ip, ftp_port, ftp_user, ftp_pass, remote_path, local_dir, 30,
        )

        if errs and not downloaded:
            # 全失败 — 不保存默认值；让 LLM 知道连不上
            return ToolResult(
                success=False,
                error=(
                    f"FTP fetch failed: {'; '.join(errs)}. "
                    f"Check that ftpd-pro is running on Switch at {switch_ip}:{ftp_port}, "
                    f"and that {remote_path} exists. Then retry fetch_crash_report."
                ),
                data={"errors": errs},
            )

        # 部分/全部成功 — 写默认值（用户勾选时）
        if remember:
            try:
                _save_device_defaults(project_path, {
                    "switch_ip": switch_ip,
                    "ftp_port": ftp_port,
                    "ftp_user": ftp_user,
                    "ftp_pass": ftp_pass,
                    "remote_path": remote_path,
                })
            except Exception as e:
                logger.warning(f"save device.json failed: {e}")

        s1 = _local_logs(project_path)
        new_logs = sorted(s1 - s0, reverse=True)  # 字典序最大 = 最新（epoch 单调递增）

        if not new_logs:
            return ToolResult(
                success=False,
                error=(
                    f"FTP succeeded but no new crash reports found "
                    f"(remote had {len(downloaded)} files, all already local). "
                    f"Either Switch hasn't crashed since last fetch, or the .nro "
                    f"didn't crash this run. Try running the .nro on Switch first, "
                    f"then call fetch_crash_report again."
                ),
                data={"downloaded": downloaded, "new_count": 0},
            )

        latest = new_logs[0]
        return await self._analyze_local(
            project_path, task_id, latest_filename=latest, source="fetched",
            extra_data={"downloaded": downloaded, "new_logs": new_logs, "errors": errs},
        )

    # ------------------------------------------------------------------
    # 内部
    # ------------------------------------------------------------------

    async def _analyze_local(
        self,
        project_path: str,
        task_id: str,
        latest_filename: Optional[str] = None,
        source: str = "cached",
        extra_data: Optional[dict] = None,
    ) -> ToolResult:
        local_dir = Path(project_path) / ".kedo" / "crash_reports"
        if not latest_filename:
            logs = sorted(_local_logs(project_path), reverse=True)
            if not logs:
                return ToolResult(
                    success=False,
                    error="No crash reports in .kedo/crash_reports/ yet — "
                          "call fetch_crash_report with force_refetch=true.",
                )
            latest_filename = logs[0]

        log_path = local_dir / latest_filename

        # 推 .elf — 只读已存在的 profile，不调 LLM 生成（addr2line 阶段不该触发 profile 重生成）
        profile = None
        if self._profile_manager:
            try:
                profile = self._profile_manager.load(project_path)
            except Exception as e:
                logger.debug(f"profile load failed: {e}")
        elf = _guess_elf_path(project_path, profile)
        if not elf:
            return ToolResult(
                success=False,
                error=(
                    f"Found crash log {latest_filename} but cannot locate .elf for "
                    f"symbol resolution. Looked in {project_path}/build/. Build the "
                    f"project first (with debug symbols) then retry."
                ),
                data={"crash_log": str(log_path)},
            )

        if not find_addr2line():
            return ToolResult(
                success=False,
                error=(
                    "aarch64-none-elf-addr2line not found. Set $DEVKITPRO or install "
                    "devkitA64 toolchain. Crash log was downloaded but cannot be resolved."
                ),
                data={"crash_log": str(log_path), "elf": elf},
            )

        try:
            rep = parse_log_file(str(log_path), elf, project_path=project_path)
        except Exception as e:
            return ToolResult(
                success=False,
                error=f"Crash log parse failed: {e}",
                data={"crash_log": str(log_path)},
            )

        formatted = format_for_llm(rep, project_path=project_path)
        # 抽 crash_site（最深的、文件在 project_path 下的 frame）
        biz = find_business_frame(rep, project_path)
        crash_site = None
        if biz and biz.resolved:
            r0 = biz.resolved[0]
            crash_site = {
                "function": r0.get("function"),
                "file": r0.get("file"),
                "line": r0.get("line"),
            }

        # emit COREDUMP_ANALYZED 让 dashboard 显示
        if self._event_bus:
            try:
                from api.schemas import AgentEvent, EventType
                await self._event_bus.publish(AgentEvent(
                    event_type=EventType.LOG_MESSAGE,  # 沿用 LOG_MESSAGE，dashboard 按 phase 过滤
                    task_id=task_id,
                    timestamp=datetime.now(timezone.utc),
                    data={
                        "phase": "coredump_analyzed",
                        "crash_log": str(log_path),
                        "result_code": rep.result_code,
                        "exception_type": rep.exception_type,
                        "crash_site": crash_site,
                        "summary": formatted,
                    },
                ))
            except Exception as e:
                logger.debug(f"emit coredump_analyzed failed: {e}")

        data = {
            "source": source,
            "crash_log": str(log_path),
            "elf_path": elf,
            "result_code": rep.result_code,
            "exception_type": rep.exception_type,
            "fault_address": rep.fault_address,
            "process_name": rep.process_name,
            "primary_module": rep.primary_module,
            "crash_site": crash_site,
            "frames": [
                {
                    "role": f.role,
                    "module": f.module,
                    "offset": f.offset,
                    "resolved": f.resolved,
                }
                for f in rep.frames
            ],
        }
        if extra_data:
            data.update(extra_data)

        return ToolResult(
            success=True,
            output=formatted,
            data=data,
        )

    async def _publish_request_event(
        self,
        task_id: str,
        project_path: str,
        reason: str,
        defaults: dict,
        local_count: int,
    ) -> None:
        # 复用 DISCUSSION_* 事件（dashboard 已有渲染逻辑），靠 kind=coredump_fetch 区分
        request_payload = {
            "kind": "coredump_fetch",
            "reason": reason,
            "defaults": {
                "switch_ip": defaults.get("switch_ip", ""),
                "ftp_port": int(defaults.get("ftp_port", 5000)),
                "ftp_user": defaults.get("ftp_user", ""),
                "ftp_pass": defaults.get("ftp_pass", ""),
                "remote_path": defaults.get("remote_path", "/atmosphere/crash_reports/"),
            },
            "has_remembered": bool(defaults.get("switch_ip")),
            "local_log_count": local_count,
        }
        _COREDUMP_PENDING[task_id] = request_payload

        if not self._event_bus:
            return
        try:
            from api.schemas import AgentEvent, EventType
            base_ts = datetime.now(timezone.utc)
            summary = (
                f"Producer 想拉 Switch crash_reports 看实机崩溃。\n理由: "
                f"{reason or '(未填)'}\n"
                f"请在 Switch 上启动 ftpd-pro，确认连接信息后点'准备好'。"
            )
            await self._event_bus.publish(AgentEvent(
                event_type=EventType.DISCUSSION_STARTED,
                task_id=task_id,
                timestamp=base_ts,
                data={"summary": summary, "trigger": "coredump_fetch"},
            ))
            await self._event_bus.publish(AgentEvent(
                event_type=EventType.DISCUSSION_PROPOSALS,
                task_id=task_id,
                timestamp=base_ts,
                data={
                    "kind": "coredump_fetch",
                    "request": request_payload,
                    # 让旧 dashboard 至少有兜底渲染
                    "proposals": [
                        {"id": "ready",
                         "title": "准备好，从 Switch 拉取 crash_reports",
                         "description": "确认 ftpd-pro 已启动 + 填好连接信息后点这个"},
                        {"id": "cancel",
                         "title": "取消",
                         "description": "本次不拉取，让 LLM 走别的路"},
                    ],
                },
            ))
        except Exception as e:
            logger.warning(f"emit coredump_fetch_request failed: {e}")

    def _clear_pending(self, task_id: str) -> None:
        _COREDUMP_PENDING.pop(task_id, None)
        if self._state is not None:
            try:
                self._state.clear_discussion(task_id)
            except Exception:
                pass
