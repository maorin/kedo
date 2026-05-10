"""
Browser tools — Chrome control via Browser Bridge.

ReactAgent uses these to inspect, navigate, and (M3) interact with the
user's browser. All require an active `kedo-browser-bridge` plugin session;
without one, calls return `bridge: no_browser_session(role=user)`.

Tier model (see core/browser_permissions.py):
- T0 read     : list_tabs / query / extract / screenshot / wait_for / get_active_tab
- T1 navigate : navigate / scroll
- T2 write    : click / type / submit (always require permission check)

Read-only flagged tools (is_read_only=True) may run concurrently in the
ToolRegistry execution path. T1/T2 tools are not read-only.
"""
from __future__ import annotations

import logging
from typing import Optional

from tools.base import BaseTool, ToolParameter, ToolResult

logger = logging.getLogger(__name__)


class _BrowserToolBase(BaseTool):
    """Common plumbing — call browser_bridge.send_command() with optional Tier check."""

    def __init__(self, bridge, policy=None):
        self._bridge = bridge
        self._policy = policy  # core.browser_permissions.BrowserPermissionPolicy or None

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
        """Direct send — for T0 actions that bypass policy."""
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

    async def _send_with_permission(
        self,
        action: str,
        params: dict,
        all_kwargs: dict,
        prefer_role: str = "user",
        timeout: float = 35.0,
        task_id: Optional[str] = None,
    ) -> ToolResult:
        """T1/T2 send — first resolve target URL, ask policy, then send if allowed."""
        if self._policy is None:
            # No policy configured = M2 fallback, allow all (test mode).
            return await self._send(action, params, prefer_role=prefer_role, timeout=timeout)

        url = await self._resolve_target_url(all_kwargs)
        decision = await self._policy.check(
            action=action, url=url, params=all_kwargs, task_id=task_id,
        )
        if not decision.allowed:
            return ToolResult(
                success=False,
                error=f"PERMISSION_DENIED: {decision.kind} ({decision.reason})",
            )
        return await self._send(action, params, prefer_role=prefer_role, timeout=timeout)

    async def _resolve_target_url(self, kwargs: dict) -> str:
        """For navigate, return the destination URL. Otherwise ask the active tab."""
        # navigate: target URL is in kwargs
        if "url" in kwargs and kwargs["url"]:
            return str(kwargs["url"])
        # everything else: query the bridge for current active tab URL
        tab_id = kwargs.get("tab_id")
        params: dict = {}
        if isinstance(tab_id, int) and tab_id > 0:
            params["tab_id"] = tab_id
        try:
            resp = await self._bridge.send_command(
                "get_active_tab", params, prefer_role="user", timeout=5,
            )
            if resp.get("success"):
                return str((resp.get("data") or {}).get("url") or "")
        except Exception as exc:
            logger.warning(f"_resolve_target_url failed: {exc}")
        return ""

    def _summarize(self, data: dict) -> str:
        return ""


class BrowserGetActiveTabTool(_BrowserToolBase):
    @property
    def name(self) -> str:
        return "browser_get_active_tab"

    @property
    def description(self) -> str:
        return (
            "Return {tab_id, window_id, url, title, status} of the user's active tab. "
            "Use to learn the current page before deciding to click / type."
        )

    @property
    def parameters(self) -> list[ToolParameter]:
        return [
            ToolParameter(name="tab_id", type="integer",
                          description="Optional tab id; defaults to active tab",
                          required=False),
        ]

    async def execute(self, tab_id: Optional[int] = None, **_) -> ToolResult:
        params: dict = {}
        if isinstance(tab_id, int) and tab_id > 0:
            params["tab_id"] = tab_id
        return await self._send("get_active_tab", params, timeout=5)

    def _summarize(self, data: dict) -> str:
        return f"tab {data.get('tab_id')} — {data.get('title','')} ({data.get('url','')})"


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
        if isinstance(tab_id, int) and tab_id > 0:
            params["tab_id"] = tab_id
        all_kwargs = {"url": url, "tab_id": tab_id, "new_tab": new_tab}
        return await self._send_with_permission(
            "navigate", params, all_kwargs, timeout=40.0,
        )

    def _summarize(self, data: dict) -> str:
        return f"navigated tab {data.get('tab_id')} → {data.get('url')} ({data.get('title','')})"


class BrowserClickTool(_BrowserToolBase):
    @property
    def name(self) -> str:
        return "browser_click"

    @property
    def description(self) -> str:
        return (
            "Click a DOM element on the active tab. Provide ANY of: selector (CSS), "
            "text_match (substring of element textContent), or aria_label. The plugin "
            "uses the same triple-strategy as browser_query and clicks the first match. "
            "This is a Tier-2 (write) action — first-time per domain triggers a dashboard "
            "permission prompt; subsequent calls within 30 min trust window are auto-allowed. "
            "Password fields and credit-card inputs are always rejected client-side."
        )

    @property
    def parameters(self) -> list[ToolParameter]:
        return [
            ToolParameter(name="selector", type="string", description="CSS selector", required=False),
            ToolParameter(name="text_match", type="string", description="Substring of element text", required=False),
            ToolParameter(name="aria_label", type="string", description="Exact aria-label match", required=False),
            ToolParameter(name="tab_id", type="integer", description="Optional tab id", required=False),
        ]

    @property
    def is_read_only(self) -> bool:
        return False

    async def execute(self, **kwargs) -> ToolResult:
        keep = {"selector", "text_match", "aria_label", "tab_id"}
        params = {k: v for k, v in kwargs.items() if k in keep and v is not None}
        if not any(params.get(k) for k in ("selector", "text_match", "aria_label")):
            return ToolResult(
                success=False,
                error="at least one of selector / text_match / aria_label required",
            )
        return await self._send_with_permission("click", params, kwargs)

    def _summarize(self, data: dict) -> str:
        return (
            f"clicked: strategy={data.get('matched_strategy','?')} "
            f"text='{(data.get('text') or '')[:60]}' tag={data.get('tag','?')}"
        )


class BrowserTypeTool(_BrowserToolBase):
    @property
    def name(self) -> str:
        return "browser_type"

    @property
    def description(self) -> str:
        return (
            "Type text into a focused or selected input element. Provide selector OR "
            "aria_label to identify the input. The plugin focuses, clears, and types "
            "the value (events: input + change). Tier-2 write action — see browser_click "
            "for permission flow. Password fields (type=password) and credit-card "
            "autocomplete inputs are HARD-BLOCKED client-side regardless of permission."
        )

    @property
    def parameters(self) -> list[ToolParameter]:
        return [
            ToolParameter(name="value", type="string", description="Text to type"),
            ToolParameter(name="selector", type="string", description="CSS selector", required=False),
            ToolParameter(name="aria_label", type="string", description="Exact aria-label match", required=False),
            ToolParameter(name="clear_first", type="boolean",
                          description="Clear input before typing (default true)",
                          required=False, default=True),
            ToolParameter(name="press_enter", type="boolean",
                          description="Press Enter after typing",
                          required=False, default=False),
            ToolParameter(name="tab_id", type="integer", description="Optional tab id", required=False),
        ]

    @property
    def is_read_only(self) -> bool:
        return False

    async def execute(self, value: str, **kwargs) -> ToolResult:
        keep = {"selector", "aria_label", "clear_first", "press_enter", "tab_id"}
        params: dict = {"value": value}
        for k in keep:
            v = kwargs.get(k)
            if v is not None:
                params[k] = v
        if not (params.get("selector") or params.get("aria_label")):
            return ToolResult(
                success=False,
                error="selector or aria_label required",
            )
        return await self._send_with_permission(
            "type", params, {**kwargs, "value": value},
        )

    def _summarize(self, data: dict) -> str:
        return f"typed into {data.get('tag','?')} (was_password={data.get('was_password', False)})"


class BrowserSubmitTool(_BrowserToolBase):
    @property
    def name(self) -> str:
        return "browser_submit"

    @property
    def description(self) -> str:
        return (
            "Submit a form. Provide selector for the <form> element, or omit to submit "
            "the form containing the focused element. Equivalent to pressing Enter on "
            "an input. Tier-2 write action."
        )

    @property
    def parameters(self) -> list[ToolParameter]:
        return [
            ToolParameter(name="selector", type="string", description="CSS selector for form (optional)", required=False),
            ToolParameter(name="tab_id", type="integer", description="Optional tab id", required=False),
        ]

    @property
    def is_read_only(self) -> bool:
        return False

    async def execute(self, **kwargs) -> ToolResult:
        keep = {"selector", "tab_id"}
        params = {k: v for k, v in kwargs.items() if k in keep and v is not None}
        return await self._send_with_permission("submit", params, kwargs)

    def _summarize(self, data: dict) -> str:
        return f"submitted form (action={data.get('form_action','')})"


class BrowserScrollTool(_BrowserToolBase):
    @property
    def name(self) -> str:
        return "browser_scroll"

    @property
    def description(self) -> str:
        return (
            "Scroll the active tab. Use direction='up'/'down' for viewport-relative "
            "scroll, or selector to scroll a specific element into view. Tier-1 "
            "navigation action — first-time per domain may prompt."
        )

    @property
    def parameters(self) -> list[ToolParameter]:
        return [
            ToolParameter(name="direction", type="string",
                          description="'up' / 'down' / 'top' / 'bottom'",
                          required=False),
            ToolParameter(name="selector", type="string",
                          description="CSS selector to scroll into view",
                          required=False),
            ToolParameter(name="amount", type="integer",
                          description="Pixels to scroll (default 600)",
                          required=False, default=600),
            ToolParameter(name="tab_id", type="integer", description="Optional tab id", required=False),
        ]

    @property
    def is_read_only(self) -> bool:
        return False

    async def execute(self, **kwargs) -> ToolResult:
        keep = {"direction", "selector", "amount", "tab_id"}
        params = {k: v for k, v in kwargs.items() if k in keep and v is not None}
        if not (params.get("direction") or params.get("selector")):
            return ToolResult(
                success=False,
                error="direction or selector required",
            )
        return await self._send_with_permission("scroll", params, kwargs)

    def _summarize(self, data: dict) -> str:
        return (
            f"scrolled: direction={data.get('direction','?')} "
            f"y={data.get('scroll_y','?')}"
        )


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
        if isinstance(tab_id, int) and tab_id > 0:
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
        if isinstance(tab_id, int) and tab_id > 0:
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


class BrowserResearchTool(_BrowserToolBase):
    """High-level research tool — agent's own isolated chrome (M4).

    Workflow:
    1. Ensure isolated agent profile is alive (lazy-spawns kedo-launched chrome
       with --user-data-dir + bundled extension that reports role=agent).
    2. Search the query on a search engine (DuckDuckGo by default — no captcha,
       no login wall, friendly to scraping).
    3. Take top N result links, navigate each, run Readability extraction.
    4. Return aggregated {title, url, text} list.

    Why this, not a search API: kedo agent often hits build/lib problems where
    the answer is buried in stackoverflow / GitHub issues / project docs — not
    indexed cleanly by any single API. A real browser sees the actual page,
    handles JS-rendered content, follows redirects, etc.
    """

    def __init__(self, bridge, profile_manager, policy=None):
        super().__init__(bridge, policy=policy)
        self._profile = profile_manager  # core.browser_profile.IsolatedBrowserProfile

    @property
    def name(self) -> str:
        return "browser_research"

    @property
    def description(self) -> str:
        return (
            "Search the web on the agent's own isolated Chrome profile and return "
            "extracted text from the top N result pages. Use when build/test fails "
            "and you need real documentation — not stackoverflow API, not anthropic "
            "search, but actual page content. Runs in a sandboxed Chrome separate from "
            "the user's main browser, so cookies/login state are never touched. "
            "The isolated profile auto-launches on first call and auto-closes after "
            "30 min idle. Returns a list of {url, title, text, length}."
        )

    @property
    def parameters(self) -> list[ToolParameter]:
        return [
            ToolParameter(name="query", type="string", description="Search query"),
            ToolParameter(
                name="max_pages", type="integer",
                description="Max result pages to extract (default 3, max 5)",
                required=False, default=3,
            ),
            ToolParameter(
                name="search_engine", type="string",
                description="'duckduckgo' (default) / 'google' / 'bing'",
                required=False, default="duckduckgo",
            ),
        ]

    @property
    def is_read_only(self) -> bool:
        # Spawning isolated chrome is a side effect, but no user state is mutated.
        return True

    async def execute(
        self,
        query: str,
        max_pages: int = 3,
        search_engine: str = "duckduckgo",
        **_,
    ) -> ToolResult:
        if not query or not str(query).strip():
            return ToolResult(success=False, error="query required")

        # Try isolated agent profile first (M4 design: don't pollute user login state).
        # Fall back to user session if isolation is unavailable (e.g. Linux Google
        # Chrome 137+ blocks --load-extension, sudo policy not configured, etc.).
        # Read-only research doesn't strictly need isolation — domains hit are public
        # search engines + docs sites; the M3 Tier-1 permission layer (allowlist /
        # 30min trust window) protects user from agent doing surprising navigations.
        prefer_role = "user"
        used_isolation = False
        if self._profile is not None:
            try:
                await self._profile.ensure_running(timeout=15.0)
                prefer_role = "agent"
                used_isolation = True
            except Exception as exc:
                logger.info(
                    f"browser_research: isolated agent profile unavailable ({exc}); "
                    f"falling back to user session (read-only research is OK without "
                    f"isolation; M3 permission layer still gates non-allowlist domains)"
                )

        max_pages = max(1, min(int(max_pages or 3), 5))
        search_url, result_selector = self._search_url(search_engine, query)

        # 1) Navigate to search results
        nav = await self._send_command("navigate", {"url": search_url}, prefer_role=prefer_role, timeout=30)
        if not nav.success:
            return nav
        # Brief wait for results JS to render
        await self._send_command(
            "wait_for",
            {"selector": result_selector, "timeout_ms": 8000},
            prefer_role=prefer_role, timeout=10,
        )

        # 2) Pull result links
        q = await self._send_command(
            "query",
            {"selector": result_selector, "limit": max_pages * 2},
            prefer_role=prefer_role, timeout=10,
        )
        if not q.success:
            return q

        matches = (q.data or {}).get("matches", []) if q.data else []
        # Filter: must have href, prefer external (not search engine itself)
        links: list[str] = []
        seen = set()
        for m in matches:
            href = m.get("href")
            if not href or href in seen:
                continue
            if any(domain in href for domain in ("duckduckgo.com", "google.com/search", "bing.com/search")):
                continue
            seen.add(href)
            links.append(href)
            if len(links) >= max_pages:
                break

        if not links:
            return ToolResult(
                success=False,
                error=f"no result links found on {search_engine} for query={query!r}",
                data={"search_url": search_url, "raw_matches": matches[:5]},
            )

        # 3) Navigate + extract each
        pages: list[dict] = []
        for url in links:
            nav = await self._send_command("navigate", {"url": url}, prefer_role=prefer_role, timeout=30)
            if not nav.success:
                pages.append({"url": url, "error": nav.error})
                continue
            ext = await self._send_command("extract", {}, prefer_role=prefer_role, timeout=10)
            if ext.success and ext.data:
                pages.append({
                    "url": url,
                    "title": ext.data.get("title", ""),
                    "text": (ext.data.get("text_content") or "")[:8000],
                    "length": ext.data.get("length", 0),
                })
            else:
                pages.append({"url": url, "error": ext.error or "extract failed"})

        if used_isolation and self._profile is not None:
            self._profile.mark_activity()

        return ToolResult(
            success=True,
            output=self._summarize_pages(pages, query, used_isolation),
            data={
                "query": query,
                "search_engine": search_engine,
                "session_role": prefer_role,
                "isolation_used": used_isolation,
                "pages": pages,
            },
        )

    async def _send_command(
        self, action: str, params: dict, prefer_role: str = "agent", timeout: float = 30,
    ):
        try:
            resp = await self._bridge.send_command(
                action, params, prefer_role=prefer_role, timeout=timeout,
            )
        except Exception as exc:
            return ToolResult(success=False, error=f"bridge: {exc}")
        if resp.get("success"):
            return ToolResult(success=True, data=resp.get("data") or {})
        err = resp.get("error") or {}
        return ToolResult(
            success=False,
            error=f"{err.get('code', 'INTERNAL')}: {err.get('message', '')}",
        )

    @staticmethod
    def _search_url(engine: str, query: str) -> tuple[str, str]:
        """Return (search URL, CSS selector that matches result links)."""
        from urllib.parse import quote
        e = (engine or "duckduckgo").lower()
        if e == "google":
            return f"https://www.google.com/search?q={quote(query)}", "h3 a, .yuRUbf a"
        if e == "bing":
            return f"https://www.bing.com/search?q={quote(query)}", "li.b_algo h2 a"
        # default: duckduckgo
        return f"https://duckduckgo.com/?q={quote(query)}", "a.result__a, h2 a"

    @staticmethod
    def _summarize_pages(pages: list[dict], query: str, isolation_used: bool = False) -> str:
        ok = sum(1 for p in pages if "error" not in p)
        mode = "isolated agent profile" if isolation_used else "user browser session"
        lines = [f"research[{query!r}] via {mode}: {ok}/{len(pages)} pages extracted"]
        for p in pages:
            if "error" in p:
                lines.append(f" ✗ {p['url']} — {p['error']}")
            else:
                lines.append(f" ✓ {p.get('title','(no title)')[:60]}  ({p.get('length',0)} chars)")
                lines.append(f"   {p['url']}")
        return "\n".join(lines)
