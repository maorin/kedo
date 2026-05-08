"""
Browser tools — M2 read-only Chrome control via Browser Bridge.

ReactAgent uses these to inspect (and navigate) the user's browser. All
require an active `kedo-browser-bridge` plugin session; without one, calls
return `bridge: no_browser_session(role=user)`.

Read-only flagged tools (is_read_only=True) may run concurrently in the
ToolRegistry execution path. browser_navigate is *not* read-only because
it changes the active tab's URL.
"""
from __future__ import annotations

import logging
from typing import Optional

from tools.base import BaseTool, ToolParameter, ToolResult

logger = logging.getLogger(__name__)


class _BrowserToolBase(BaseTool):
    """Common plumbing — call browser_bridge.send_command()."""

    def __init__(self, bridge):
        self._bridge = bridge

    @property
    def is_read_only(self) -> bool:
        return True

    async def _send(
        self,
        action: str,
        params: dict,
        prefer_role: str = "user",
        timeout: float = 35.0,
    ) -> ToolResult:
        try:
            resp = await self._bridge.send_command(
                action, params, prefer_role=prefer_role, timeout=timeout
            )
        except Exception as exc:
            return ToolResult(success=False, error=f"bridge: {exc}")
        if resp.get("success"):
            data = resp.get("data") or {}
            return ToolResult(success=True, output=self._summarize(data), data=data)
        err = resp.get("error") or {}
        return ToolResult(
            success=False,
            error=f"{err.get('code', 'INTERNAL')}: {err.get('message', '')}",
        )

    def _summarize(self, data: dict) -> str:
        return ""


class BrowserListTabsTool(_BrowserToolBase):
    @property
    def name(self) -> str:
        return "browser_list_tabs"

    @property
    def description(self) -> str:
        return (
            "List all tabs currently open in the user's browser. "
            "Returns a list of {id, url, title, active, status} per tab."
        )

    @property
    def parameters(self) -> list[ToolParameter]:
        return []

    async def execute(self, **kwargs) -> ToolResult:
        return await self._send("list_tabs", {})

    def _summarize(self, data: dict) -> str:
        tabs = data.get("tabs", [])
        lines = [f"{len(tabs)} tab(s):"]
        for t in tabs[:20]:
            star = "*" if t.get("active") else " "
            lines.append(f" {star} [{t.get('id')}] {t.get('title','')} — {t.get('url','')}")
        return "\n".join(lines)


class BrowserNavigateTool(_BrowserToolBase):
    @property
    def name(self) -> str:
        return "browser_navigate"

    @property
    def description(self) -> str:
        return (
            "Navigate a tab to the given URL and wait for the page to finish loading. "
            "Use new_tab=true to open in a fresh tab; otherwise updates the active tab "
            "(or the tab specified by tab_id)."
        )

    @property
    def parameters(self) -> list[ToolParameter]:
        return [
            ToolParameter(name="url", type="string", description="Target URL (http/https only)"),
            ToolParameter(
                name="tab_id", type="integer",
                description="Optional: tab to navigate (defaults to active tab)",
                required=False,
            ),
            ToolParameter(
                name="new_tab", type="boolean",
                description="Open in a new tab instead of replacing current",
                required=False, default=False,
            ),
        ]

    @property
    def is_read_only(self) -> bool:
        return False  # navigation mutates browser state

    async def execute(
        self,
        url: str,
        tab_id: Optional[int] = None,
        new_tab: bool = False,
        **_,
    ) -> ToolResult:
        params: dict = {"url": url, "new_tab": bool(new_tab)}
        if tab_id is not None:
            params["tab_id"] = tab_id
        return await self._send("navigate", params, timeout=40.0)

    def _summarize(self, data: dict) -> str:
        return f"navigated tab {data.get('tab_id')} → {data.get('url')} ({data.get('title','')})"


class BrowserScreenshotTool(_BrowserToolBase):
    @property
    def name(self) -> str:
        return "browser_screenshot"

    @property
    def description(self) -> str:
        return (
            "Capture the visible viewport of a tab as a PNG. "
            "Returns a data URL (data:image/png;base64,...). Only request when you "
            "actually need pixels — base64 PNGs are large."
        )

    @property
    def parameters(self) -> list[ToolParameter]:
        return [
            ToolParameter(
                name="tab_id", type="integer",
                description="Optional tab id (defaults to active tab)",
                required=False,
            ),
        ]

    async def execute(self, tab_id: Optional[int] = None, **_) -> ToolResult:
        params: dict = {}
        if tab_id is not None:
            params["tab_id"] = tab_id
        return await self._send("screenshot", params, timeout=15.0)

    def _summarize(self, data: dict) -> str:
        url = data.get("data_url", "") or ""
        return f"screenshot of tab {data.get('tab_id')} (data url len={len(url)})"


class BrowserExtractTool(_BrowserToolBase):
    @property
    def name(self) -> str:
        return "browser_extract"

    @property
    def description(self) -> str:
        return (
            "Extract the readable main content of a page using Mozilla Readability. "
            "Returns title, text_content (plain text), excerpt, and any user selection."
        )

    @property
    def parameters(self) -> list[ToolParameter]:
        return [
            ToolParameter(
                name="tab_id", type="integer",
                description="Optional tab id (defaults to active tab)",
                required=False,
            ),
        ]

    async def execute(self, tab_id: Optional[int] = None, **_) -> ToolResult:
        params: dict = {}
        if tab_id is not None:
            params["tab_id"] = tab_id
        return await self._send("extract", params)

    def _summarize(self, data: dict) -> str:
        title = data.get("title", "") or ""
        text = data.get("text_content", "") or ""
        return f"{title} — {len(text)} chars"


class BrowserQueryTool(_BrowserToolBase):
    @property
    def name(self) -> str:
        return "browser_query"

    @property
    def description(self) -> str:
        return (
            "Query DOM elements in a tab. Provide ANY of: selector (CSS), text_match "
            "(substring of element textContent), or aria_label. Multiple are OR-ed and "
            "deduped. Returns up to `limit` element descriptors with rect, text, role, "
            "aria_label, href, visible, matched_strategy, and is_password_field "
            "(agent must never interact with credential fields)."
        )

    @property
    def parameters(self) -> list[ToolParameter]:
        return [
            ToolParameter(name="selector", type="string", description="CSS selector", required=False),
            ToolParameter(name="text_match", type="string", description="Substring of element text", required=False),
            ToolParameter(name="aria_label", type="string", description="Exact aria-label match", required=False),
            ToolParameter(name="tab_id", type="integer", description="Optional tab id", required=False),
            ToolParameter(name="limit", type="integer", description="Max results (default 20)", required=False, default=20),
        ]

    async def execute(self, **kwargs) -> ToolResult:
        keep = {"selector", "text_match", "aria_label", "tab_id", "limit"}
        params: dict = {k: v for k, v in kwargs.items() if k in keep and v is not None}
        if not any(params.get(k) for k in ("selector", "text_match", "aria_label")):
            return ToolResult(
                success=False,
                error="at least one of selector / text_match / aria_label required",
            )
        return await self._send("query", params)

    def _summarize(self, data: dict) -> str:
        ms = data.get("matches", [])
        lines = [f"{data.get('total', len(ms))} match(es), showing {len(ms)}:"]
        for m in ms[:5]:
            txt = (m.get("text") or "")[:80]
            lines.append(f" - [{m.get('matched_strategy')}] <{m.get('tag')}> {txt}")
        return "\n".join(lines)


class BrowserWaitForTool(_BrowserToolBase):
    @property
    def name(self) -> str:
        return "browser_wait_for"

    @property
    def description(self) -> str:
        return (
            "Wait until a DOM element appears (or, with vanish=true, disappears). "
            "Same selector / text_match / aria_label triple as browser_query. "
            "Polls every 200ms up to timeout_ms (default 30000, max 60000)."
        )

    @property
    def parameters(self) -> list[ToolParameter]:
        return [
            ToolParameter(name="selector", type="string", description="CSS selector", required=False),
            ToolParameter(name="text_match", type="string", description="Substring of element text", required=False),
            ToolParameter(name="aria_label", type="string", description="Exact aria-label match", required=False),
            ToolParameter(
                name="vanish", type="boolean",
                description="Wait until the element disappears instead of appears",
                required=False, default=False,
            ),
            ToolParameter(
                name="timeout_ms", type="integer",
                description="Max wait in ms (capped at 60000)",
                required=False, default=30000,
            ),
            ToolParameter(name="tab_id", type="integer", description="Optional tab id", required=False),
        ]

    async def execute(self, **kwargs) -> ToolResult:
        keep = {"selector", "text_match", "aria_label", "vanish", "timeout_ms", "tab_id"}
        params: dict = {k: v for k, v in kwargs.items() if k in keep and v is not None}
        if not any(params.get(k) for k in ("selector", "text_match", "aria_label")):
            return ToolResult(
                success=False,
                error="at least one of selector / text_match / aria_label required",
            )
        timeout = min(int(params.get("timeout_ms") or 30000), 60000)
        return await self._send("wait_for", params, timeout=(timeout / 1000.0) + 5)

    def _summarize(self, data: dict) -> str:
        if data.get("error"):
            return f"wait_for: {data['error']}"
        return (
            f"wait_for: found={data.get('found')} "
            f"elapsed={data.get('elapsed_ms')}ms count={data.get('count')}"
        )
