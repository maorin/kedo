"""
run_test_cases — 自动执行 markdown 写的测试用例（M3.5 v1）

设计文档: docs/deep-dives/m3.5-test-execution-design.md

v1 选项（与设计文档一致）：
- 步骤→动作映射：纯 regex（不靠 LLM 翻译，保证确定性）
- 断言精度：粗略（步骤无错即 PASS，不验证"预期结果"原文）
- 集成方式：Python 直调 bridge，绕过 ReactAgent；event_bus 推进度
- v1 范围：单文件全部 case 或单 case

未来 v2 改 M3 prompt 让 LLM 同时输出 TC.json（结构化），那时本工具
直接读 json 无需 regex。
"""
from __future__ import annotations

import asyncio
import base64
import json
import logging
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

from tools.base import BaseTool, ToolParameter, ToolResult

logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)


# ============================================================
# Markdown TC parser
# ============================================================

TC_HEADER_RE = re.compile(r"^##\s+(TC-[\w-]+-\d+)\s+(.+?)\s*$", re.MULTILINE)
TABLE_ROW_RE = re.compile(r"\|\s*\**\s*(.+?)\s*\**\s*\|\s*(.+?)\s*\|")
NUMBER_PREFIX_RE = re.compile(r"^\s*\d+[.．]\s*")


@dataclass
class TestCase:
    id: str
    title: str
    priority: str = ""
    preconditions: str = ""
    steps: list[str] = None
    expected: str = ""

    def __post_init__(self):
        if self.steps is None:
            self.steps = []


def parse_tc_markdown(content: str) -> list[TestCase]:
    """Parse a TC.md file into structured TestCase objects.

    Expects sections like:
      ## TC-DM-001 桌面列表加载
      | 项目 | 内容 |
      |---|---|
      | 优先级 | P0 |
      | 前置条件 | ... |
      | 测试步骤 | 1. 点击... <br>2. 输入... |
      | 预期结果 | ... |
    """
    tcs: list[TestCase] = []
    headers = list(TC_HEADER_RE.finditer(content))
    for i, m in enumerate(headers):
        tc_id, title = m.group(1), m.group(2).strip()
        start = m.end()
        end = headers[i + 1].start() if i + 1 < len(headers) else len(content)
        section = content[start:end]

        priority = preconditions = steps_raw = expected = ""
        for row in TABLE_ROW_RE.finditer(section):
            label = re.sub(r"\*+", "", row.group(1)).strip()
            value = row.group(2).strip()
            if "优先级" in label:
                priority = value
            elif "前置条件" in label:
                preconditions = value
            elif "测试步骤" in label:
                steps_raw = value
            elif "预期结果" in label:
                expected = value

        # Split steps on <br> / newline; strip leading "1." prefix
        steps: list[str] = []
        if steps_raw:
            for part in re.split(r"<br\s*/?>|\n", steps_raw):
                s = NUMBER_PREFIX_RE.sub("", part.strip())
                if s:
                    steps.append(s)

        tcs.append(TestCase(
            id=tc_id, title=title, priority=priority,
            preconditions=preconditions, steps=steps, expected=expected,
        ))
    return tcs


# ============================================================
# Step → browser action mapping (regex-based, 纯确定性)
# ============================================================

# Each entry: (regex pattern, builder function)
# Builder returns (action_name, params_dict). First match wins.
def _build_step_mappings():
    """Build the regex → action mapping list. Function form because lambdas
    capturing regex groups are easier to read this way."""

    def click_by_text(text: str) -> tuple[str, dict]:
        return ("click", {"text_match": text.strip()})

    def type_value(value: str, field: str | None = None) -> tuple[str, dict]:
        params: dict = {"value": value}
        if field:
            params["aria_label"] = field.strip()
        return ("type", params)

    mappings: list = [
        # 截图 — 最早匹配，因为"截图"经常和其他动作一起出现在步骤里
        (re.compile(r"^\s*(?:截图|截取屏幕|screenshot)\s*$"),
         lambda m: ("screenshot", {})),

        # 点击 (X 描述) 【按钮文字】 — 允许 "点击" 和 "【" 之间 0-30 字描述（如"左侧菜单"、"页面右上角"）
        (re.compile(r"(?:点击|单击|按)[^【\n]{0,30}【(.+?)】"),
         lambda m: click_by_text(m.group(1))),

        # 点击 "X" / 点击 'X' / 点击 [X]
        (re.compile(r"""(?:点击|单击|按)[^"'"`「『\[\n]{0,30}["'"`「『](.+?)["'"`」』]"""),
         lambda m: click_by_text(m.group(1))),
        (re.compile(r"(?:点击|单击|按)[^\[\n]{0,30}\[(.+?)\]"),
         lambda m: click_by_text(m.group(1))),

        # 在【X】输入框 输入 "value"
        (re.compile(r"""(?:在|到)?\s*【(.+?)】\s*(?:输入框|栏|字段)?\s*(?:中)?\s*输入\s*["'"`「『](.+?)["'"`」』]"""),
         lambda m: type_value(m.group(2), m.group(1))),

        # 输入用户名 "admin" — 在 "输入" 和引号之间最多 20 字描述
        (re.compile(r"""输入[^"'"`「『\n]{0,20}["'"`「『](.+?)["'"`」』]"""),
         lambda m: type_value(m.group(1))),

        # 在 X 输入框 输入 value (no quotes around value)
        (re.compile(r"在\s*(.+?)\s*(?:输入框|栏)\s*(?:中)?\s*输入\s*(.+?)\s*$"),
         lambda m: type_value(m.group(2).strip(), m.group(1).strip())),

        # 导航至 / 访问 / 打开 https://...
        (re.compile(r"(?:导航|跳转|访问|打开|前往)\s*(?:至|到)?\s*(https?://\S+)"),
         lambda m: ("navigate", {"url": m.group(1)})),

        # 等待 X 出现 / 等待加载 / 等待页面加载
        (re.compile(r"""等待\s*.{0,5}\s*["'"`「『](.+?)["'"`」』]"""),
         lambda m: ("wait_for", {"text_match": m.group(1), "timeout_ms": 5000})),
        (re.compile(r"等待.+?(?:加载完成|页面加载|出现)"),
         lambda m: ("wait_for", {"timeout_ms": 3000, "selector": "body"})),

        # 滚动 到底/底部 / 到顶/顶部
        (re.compile(r"滚动\s*.{0,5}\s*(?:到底|至底|底部|最下)"),
         lambda m: ("scroll", {"direction": "bottom"})),
        (re.compile(r"滚动\s*.{0,5}\s*(?:到顶|至顶|顶部|最上)"),
         lambda m: ("scroll", {"direction": "top"})),
        (re.compile(r"(?:向下|往下)\s*滚动|滚动\s*(?:向下|往下)"),
         lambda m: ("scroll", {"direction": "down", "amount": 600})),
        (re.compile(r"(?:向上|往上)\s*滚动|滚动\s*(?:向上|往上)"),
         lambda m: ("scroll", {"direction": "up", "amount": 600})),

        # 提交表单
        (re.compile(r"(?:提交|submit)\s*(?:表单|form)?"),
         lambda m: ("submit", {})),
    ]
    return mappings


_STEP_MAPPINGS = _build_step_mappings()


def map_step(text: str) -> Optional[tuple[str, dict]]:
    """Try to map a natural-language step to a (action, params) tuple.
    Returns None if no pattern matched. Caller treats None as
    UNSUPPORTED_STEP → fail the TC at this step.
    """
    text = (text or "").strip()
    for pattern, builder in _STEP_MAPPINGS:
        m = pattern.search(text)
        if m:
            try:
                return builder(m)
            except Exception as exc:
                logger.warning(f"map_step: builder failed for {text!r}: {exc}")
                return None
    return None


# ============================================================
# Tool implementation
# ============================================================

class RunTestCasesTool(BaseTool):
    """Run TC.md cases automatically via Browser Bridge.

    Pure Python orchestrator — does NOT go through ReactAgent. The LLM only
    triggers this tool once with the md_file; the tool runs all cases
    sequentially via bridge.send_command. Progress events go to event_bus
    for dashboard visibility.
    """

    def __init__(self, bridge, profile_manager=None, event_bus=None):
        self._bridge = bridge
        self._profile = profile_manager
        self._event_bus = event_bus

    @property
    def name(self) -> str:
        return "run_test_cases"

    @property
    def description(self) -> str:
        return (
            "Auto-execute test cases from a markdown file (TC-XX-NNN sections). "
            "Parses each TC's '测试步骤' table row, maps natural-language steps "
            "to browser_* actions via regex, runs sequentially via Browser Bridge, "
            "and writes a PASS/FAIL report. TC-LOGIN-* are auto-skipped (password "
            "fields hard-blocked by M3). Unrecognized steps mark the TC as "
            "FAILED with UNSUPPORTED_STEP. Recommended when test docs exist; for "
            "writing TC docs, use the agent itself + browser_query + file_write."
        )

    @property
    def parameters(self) -> list[ToolParameter]:
        return [
            ToolParameter(name="md_file", type="string",
                          description="Absolute path to a TC.md file"),
            ToolParameter(name="tc_id", type="string",
                          description="'all' (default), exact 'TC-XX-NNN', or 'TC-XX-*' wildcard",
                          required=False, default="all"),
            ToolParameter(name="base_url", type="string",
                          description="If set, navigate here before each TC (state reset)",
                          required=False),
            ToolParameter(name="skip_password_cases", type="boolean",
                          description="Skip TC-LOGIN-* cases (default true)",
                          required=False, default=True),
            ToolParameter(name="output_dir", type="string",
                          description="Report output dir (default <md_file>/../results/)",
                          required=False),
        ]

    @property
    def is_read_only(self) -> bool:
        # Tests CAN trigger T2 click/type which mutate page state, but they
        # don't create candidates / mutate kedo's own state, so this is
        # effectively read-only from kedo's perspective.
        return True

    async def execute(
        self,
        md_file: str,
        tc_id: str = "all",
        base_url: Optional[str] = None,
        skip_password_cases: bool = True,
        output_dir: Optional[str] = None,
        **_,
    ) -> ToolResult:
        md_path = Path(md_file).expanduser().resolve()
        if not md_path.is_file():
            return ToolResult(success=False, error=f"md file not found: {md_path}")

        try:
            content = md_path.read_text(encoding="utf-8")
        except Exception as exc:
            return ToolResult(success=False, error=f"read md failed: {exc}")

        all_tcs = parse_tc_markdown(content)
        if not all_tcs:
            return ToolResult(
                success=False,
                error=f"no TC sections found in {md_path}; expected '## TC-XX-NNN ...' headers",
            )

        tcs = _filter_tcs(all_tcs, tc_id)
        if not tcs:
            return ToolResult(
                success=False,
                error=f"no TCs match tc_id={tc_id!r}; found {len(all_tcs)} total in file",
            )

        # Prefer isolated agent profile; fall back to user session (same
        # strategy as browser_research).
        prefer_role = "user"
        if self._profile is not None:
            try:
                await self._profile.ensure_running(timeout=15.0)
                prefer_role = "agent"
            except Exception as exc:
                logger.info(
                    f"run_test_cases: isolated profile unavailable ({exc}); "
                    f"falling back to user session"
                )

        out_dir = Path(output_dir).expanduser() if output_dir else md_path.parent / "results"
        screenshots_dir = out_dir / "screenshots"
        out_dir.mkdir(parents=True, exist_ok=True)
        screenshots_dir.mkdir(parents=True, exist_ok=True)

        # Run cases
        start_ts = time.monotonic()
        results: list[dict] = []
        for tc in tcs:
            await self._emit_event("test_case_started",
                                   {"tc_id": tc.id, "title": tc.title})
            result = await self._run_one_tc(
                tc, base_url, prefer_role, skip_password_cases, screenshots_dir,
            )
            results.append(result)
            await self._emit_event("test_case_completed", {
                "tc_id": tc.id,
                "status": result["status"],
                "duration_ms": result.get("duration_ms"),
                "error": result.get("error"),
            })

        duration_ms = int((time.monotonic() - start_ts) * 1000)
        summary = _build_summary(results, duration_ms)

        # Write reports
        md_report_path = out_dir / f"{md_path.stem}-results.md"
        json_report_path = out_dir / f"{md_path.stem}-results.json"
        md_report_path.write_text(
            _render_md_report(md_path.name, summary, results),
            encoding="utf-8",
        )
        json_report_path.write_text(
            json.dumps({"source": str(md_path), "summary": summary,
                        "results": results}, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

        return ToolResult(
            success=True,
            output=_short_summary_text(md_path.name, summary, prefer_role),
            data={
                "summary": summary,
                "results": results,
                "report_md_path": str(md_report_path),
                "report_json_path": str(json_report_path),
                "session_role": prefer_role,
            },
        )

    async def _run_one_tc(
        self, tc: TestCase, base_url: Optional[str], prefer_role: str,
        skip_password_cases: bool, screenshots_dir: Path,
    ) -> dict:
        if skip_password_cases and tc.id.upper().startswith("TC-LOGIN-"):
            return {
                "tc_id": tc.id, "title": tc.title, "status": "skipped",
                "reason": "password_field_blocked (M3 hard rule); set skip_password_cases=false to override",
            }

        start = time.monotonic()

        if base_url:
            resp = await self._send("navigate", {"url": base_url}, prefer_role, timeout=30)
            if not resp.get("success"):
                return _fail_result(tc, 0, f"base_url navigate failed: {_err_str(resp)}",
                                    screenshot=None, step_text=f"navigate {base_url}",
                                    duration=int((time.monotonic() - start) * 1000))
            # Brief settle
            await asyncio.sleep(0.5)

        for step_idx, step_text in enumerate(tc.steps, 1):
            mapped = map_step(step_text)
            if mapped is None:
                screenshot = await self._capture_screenshot(tc.id, prefer_role, screenshots_dir)
                return _fail_result(tc, step_idx, f"UNSUPPORTED_STEP: {step_text}",
                                    screenshot=screenshot, step_text=step_text,
                                    duration=int((time.monotonic() - start) * 1000))

            action, params = mapped
            try:
                resp = await self._send(action, params, prefer_role, timeout=15)
            except Exception as exc:
                screenshot = await self._capture_screenshot(tc.id, prefer_role, screenshots_dir)
                return _fail_result(tc, step_idx, f"bridge error: {exc}",
                                    screenshot=screenshot, step_text=step_text,
                                    action=action, params=params,
                                    duration=int((time.monotonic() - start) * 1000))

            if not resp.get("success"):
                screenshot = await self._capture_screenshot(tc.id, prefer_role, screenshots_dir)
                return _fail_result(tc, step_idx, _err_str(resp),
                                    screenshot=screenshot, step_text=step_text,
                                    action=action, params=params,
                                    duration=int((time.monotonic() - start) * 1000))

        return {
            "tc_id": tc.id, "title": tc.title, "status": "passed",
            "steps_executed": len(tc.steps),
            "duration_ms": int((time.monotonic() - start) * 1000),
        }

    async def _send(self, action: str, params: dict, prefer_role: str, timeout: float = 15) -> dict:
        try:
            return await self._bridge.send_command(action, params, prefer_role=prefer_role, timeout=timeout)
        except Exception as exc:
            return {"success": False, "error": {"code": "BRIDGE_ERROR", "message": str(exc)}}

    async def _capture_screenshot(self, tc_id: str, prefer_role: str, dest_dir: Path) -> Optional[str]:
        """Take a screenshot for failure forensics. Returns file path or None."""
        try:
            resp = await self._bridge.send_command("screenshot", {}, prefer_role=prefer_role, timeout=10)
        except Exception as exc:
            logger.warning(f"screenshot for {tc_id} failed: {exc}")
            return None
        if not resp.get("success"):
            return None
        data_url = (resp.get("data") or {}).get("data_url", "")
        if not data_url.startswith("data:image/"):
            return None
        try:
            _, b64 = data_url.split(",", 1)
            ext = "png" if "png" in data_url[:30] else "jpg"
            path = dest_dir / f"{tc_id}.{ext}"
            path.write_bytes(base64.b64decode(b64))
            return str(path)
        except Exception as exc:
            logger.warning(f"screenshot save failed for {tc_id}: {exc}")
            return None

    async def _emit_event(self, kind: str, payload: dict) -> None:
        if self._event_bus is None:
            return
        try:
            # Use a generic emit; if event_bus has a different API, adapt
            if hasattr(self._event_bus, "publish"):
                await self._event_bus.publish("test_run", {"kind": kind, **payload})
            elif hasattr(self._event_bus, "emit"):
                await self._event_bus.emit("test_run", {"kind": kind, **payload})
        except Exception:
            pass  # event delivery best-effort


# ============================================================
# Helpers
# ============================================================

def _filter_tcs(tcs: list[TestCase], tc_id: str) -> list[TestCase]:
    if tc_id == "all":
        return tcs
    if tc_id.endswith("*"):
        prefix = tc_id[:-1]
        return [t for t in tcs if t.id.startswith(prefix)]
    return [t for t in tcs if t.id == tc_id]


def _err_str(resp: dict) -> str:
    err = resp.get("error") or {}
    return f"{err.get('code', 'INTERNAL')}: {err.get('message', '')}"


def _fail_result(tc: TestCase, step_idx: int, error: str, *,
                 screenshot: Optional[str] = None, step_text: str = "",
                 action: Optional[str] = None, params: Optional[dict] = None,
                 duration: int = 0) -> dict:
    out = {
        "tc_id": tc.id, "title": tc.title, "status": "failed",
        "failed_step": step_idx, "error": error,
        "duration_ms": duration,
    }
    if step_text:
        out["step_text"] = step_text
    if action:
        out["action"] = action
    if params:
        out["params"] = params
    if screenshot:
        out["screenshot"] = screenshot
    return out


def _build_summary(results: list[dict], total_duration_ms: int) -> dict:
    counts = {"total": len(results), "passed": 0, "failed": 0, "skipped": 0}
    for r in results:
        s = r.get("status", "")
        if s in counts:
            counts[s] += 1
    counts["duration_ms"] = total_duration_ms
    return counts


def _short_summary_text(filename: str, summary: dict, prefer_role: str) -> str:
    return (
        f"run_test_cases({filename}) via {prefer_role} session: "
        f"{summary['passed']} passed, {summary['failed']} failed, "
        f"{summary['skipped']} skipped (total {summary['total']}, "
        f"{summary['duration_ms']}ms)"
    )


def _render_md_report(source_name: str, summary: dict, results: list[dict]) -> str:
    """Markdown report — overview table + per-TC details."""
    lines = [
        f"# Test Run Report — {source_name}",
        "",
        f"> Generated by kedo M3.5 run_test_cases",
        f"> Total: **{summary['total']}**  ·  Passed: **{summary['passed']}** ✅  "
        f"·  Failed: **{summary['failed']}** ❌  ·  Skipped: **{summary['skipped']}** ⏭  "
        f"·  Duration: {summary['duration_ms']}ms",
        "",
        "## Overview",
        "",
        "| TC ID | Status | Duration | Notes |",
        "|---|---|---|---|",
    ]
    for r in results:
        status_icon = {"passed": "✅", "failed": "❌", "skipped": "⏭"}.get(r["status"], "❓")
        notes_parts = []
        if r["status"] == "failed":
            notes_parts.append(f"step {r.get('failed_step', '?')}: {r.get('error', '')[:80]}")
        elif r["status"] == "skipped":
            notes_parts.append(r.get("reason", ""))
        notes = " · ".join(notes_parts)
        dur = r.get("duration_ms", "")
        dur_s = f"{dur}ms" if isinstance(dur, int) else ""
        lines.append(f"| {r['tc_id']} | {status_icon} {r['status']} | {dur_s} | {notes} |")

    fails = [r for r in results if r["status"] == "failed"]
    if fails:
        lines += ["", "## Failure Details", ""]
        for r in fails:
            lines.append(f"### {r['tc_id']} — {r.get('title','')}")
            lines.append("")
            lines.append(f"- **Failed at step**: {r.get('failed_step', '?')}")
            if r.get("step_text"):
                lines.append(f"- **Step text**: `{r['step_text']}`")
            if r.get("action"):
                lines.append(f"- **Mapped action**: `{r['action']}({r.get('params', {})})`")
            lines.append(f"- **Error**: `{r.get('error', '')}`")
            if r.get("screenshot"):
                lines.append(f"- **Screenshot**: `{r['screenshot']}`")
            lines.append("")
    return "\n".join(lines) + "\n"
