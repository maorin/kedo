"""
Browser permission policy — Tier 0-3 + allowlist + 30min trust window.

Design: docs/deep-dives/browser-bridge-design.md §8.

Tier model:
- T0 read     : list_tabs / query / extract / screenshot / wait_for / get_active_tab → auto-allow
- T1 navigate : navigate / scroll → allowlist match auto; else ask user (option to add)
- T2 write    : click / type / submit → trust window or per-call confirm
- T3 high     : arbitrary JS / file upload / clipboard write → hardcoded DENY

Hard rules (cannot be bypassed by any allowlist):
- chrome:// / file:// / chrome-extension:// schemes → deny
- input type=password / autocomplete~="cc-" → deny (also enforced client-side)
- cross-origin iframe traversal → not crossed (client-side enforcement)
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Awaitable, Callable, Literal, Optional
from urllib.parse import urlparse

logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)


TIER_OF_ACTION = {
    # T0 read-only — auto-allow
    "list_tabs": 0,
    "query": 0,
    "extract": 0,
    "screenshot": 0,
    "wait_for": 0,
    "get_active_tab": 0,
    # T1 navigation — allowlist
    "navigate": 1,
    "scroll": 1,
    # T2 write — trust window or ask
    "click": 2,
    "type": 2,
    "submit": 2,
    # T3 high — denied
    "execute_script": 3,
    "upload_file": 3,
    "clipboard_write": 3,
}

BLOCKED_SCHEMES = ("chrome://", "chrome-extension://", "file://", "devtools://")
TRUST_WINDOW_S = 30 * 60  # 30 minutes
ASK_TIMEOUT_S = 120.0


DecisionKind = Literal[
    "allow_implicit",   # T0 or already-trusted, no user prompt
    "allow_once",       # user picked "Allow once"
    "allow_30min",      # user picked "Trust 30 min"
    "trust_persist",    # user picked "Trust permanently" (added to allowlist)
    "deny",
    "timeout",          # user did not respond within ASK_TIMEOUT_S
]


@dataclass
class Decision:
    kind: DecisionKind
    reason: str = ""

    @property
    def allowed(self) -> bool:
        return self.kind in ("allow_implicit", "allow_once", "allow_30min", "trust_persist")


@dataclass
class _State:
    permanent_allowlist: set[str] = field(default_factory=set)
    trust_window: dict[str, float] = field(default_factory=dict)  # domain → expires_at_unix


def _domain_of(url: str) -> str:
    if not url:
        return ""
    try:
        return (urlparse(url).hostname or "").lower()
    except Exception:
        return ""


def _matches_allowlist(domain: str, allowlist: set[str]) -> bool:
    """`github.com` exact match, `*.devkitpro.org` matches both `devkitpro.org` and any subdomain."""
    if not domain:
        return False
    if domain in allowlist:
        return True
    for pattern in allowlist:
        if pattern.startswith("*."):
            base = pattern[2:]
            if domain == base or domain.endswith("." + base):
                return True
    return False


_DEFAULT_ALLOWLIST = [
    # 本地服务
    "localhost",
    "127.0.0.1",
    # 嵌入式开发文档
    "*.devkitpro.org",
    # browser_research 常用搜索引擎和参考站点 — 预填减少首次研究时反复弹窗
    "duckduckgo.com",
    "*.duckduckgo.com",
    "github.com",
    "stackoverflow.com",
    "*.stackoverflow.com",
    "*.wikipedia.org",
    "*.mozilla.org",
]


class BrowserPermissionPolicy:
    """Stateful permission gate for browser bridge actions."""

    def __init__(
        self,
        state_path: Path | str | None = None,
        audit_log_path: Path | str | None = None,
        broadcast_request: Optional[Callable[[dict], Awaitable[None]]] = None,
        ask_timeout_s: float = ASK_TIMEOUT_S,
    ):
        if state_path is None:
            state_path = Path.home() / ".config" / "kedo" / "browser_permissions.json"
        if audit_log_path is None:
            audit_log_path = Path.home() / ".kedo" / "browser-audit.jsonl"

        self._state_path = Path(state_path)
        self._audit_log_path = Path(audit_log_path)
        self._broadcast = broadcast_request
        self._ask_timeout_s = ask_timeout_s

        self._state = _State()
        self._pending: dict[str, asyncio.Future] = {}
        self._lock = asyncio.Lock()

        self._state_path.parent.mkdir(parents=True, exist_ok=True)
        self._audit_log_path.parent.mkdir(parents=True, exist_ok=True)
        self._load_state()

    # ---------- public API ----------

    def tier_of(self, action: str) -> int:
        return TIER_OF_ACTION.get(action, 3)

    async def check(
        self,
        action: str,
        url: str,
        params: dict | None = None,
        task_id: str | None = None,
    ) -> Decision:
        """Decide whether `action` on `url` may proceed. Awaits user if needed."""
        domain = _domain_of(url)

        # Hard rules first (cannot be bypassed by any allowlist)
        for scheme in BLOCKED_SCHEMES:
            if (url or "").startswith(scheme):
                d = Decision(kind="deny", reason=f"PROTOCOL_BLOCKED: {scheme}")
                self._audit(action, domain, url, d, task_id, params)
                return d

        tier = self.tier_of(action)

        if tier == 0:
            d = Decision(kind="allow_implicit", reason="T0")
            self._audit(action, domain, url, d, task_id, params)
            return d

        if tier == 3:
            d = Decision(kind="deny", reason=f"TIER_3_BLOCKED: {action}")
            self._audit(action, domain, url, d, task_id, params)
            return d

        # T1 / T2 — check trust state, else ask
        if domain and _matches_allowlist(domain, self._state.permanent_allowlist):
            d = Decision(kind="allow_implicit", reason="allowlist")
            self._audit(action, domain, url, d, task_id, params)
            return d

        if domain and self._is_trust_window_active(domain):
            d = Decision(kind="allow_implicit", reason="trust_window")
            self._audit(action, domain, url, d, task_id, params)
            return d

        # Otherwise ask the user via dashboard event
        d = await self._ask(action, domain, url, tier, task_id, params)
        self._audit(action, domain, url, d, task_id, params)
        await self._apply_decision(d, domain)
        return d

    def resolve(self, request_id: str, decision_kind: str) -> bool:
        """Called by REST endpoint when user clicks dashboard button."""
        fut = self._pending.pop(request_id, None)
        if fut is None or fut.done():
            return False
        if decision_kind not in (
            "allow_once", "allow_30min", "trust_persist", "deny",
        ):
            decision_kind = "deny"
        fut.set_result(Decision(kind=decision_kind, reason="user_resolved"))
        return True

    def status(self) -> dict[str, Any]:
        return {
            "permanent_allowlist": sorted(self._state.permanent_allowlist),
            "active_trust_window": [
                {"domain": d, "expires_in_s": round(exp - time.time(), 1)}
                for d, exp in self._state.trust_window.items()
                if exp > time.time()
            ],
            "pending_requests": len(self._pending),
        }

    # ---------- internals ----------

    def _is_trust_window_active(self, domain: str) -> bool:
        exp = self._state.trust_window.get(domain)
        if exp is None:
            return False
        if exp <= time.time():
            self._state.trust_window.pop(domain, None)
            return False
        return True

    async def _ask(
        self,
        action: str,
        domain: str,
        url: str,
        tier: int,
        task_id: str | None,
        params: dict | None,
    ) -> Decision:
        request_id = uuid.uuid4().hex
        fut: asyncio.Future = asyncio.get_event_loop().create_future()
        self._pending[request_id] = fut

        if self._broadcast is None:
            logger.warning(
                "browser_permissions: no dashboard broadcast configured — auto-deny "
                f"action={action} domain={domain}"
            )
            return Decision(kind="deny", reason="no_dashboard")

        await self._broadcast({
            "type": "browser_permission_request",
            "data": {
                "request_id": request_id,
                "action": action,
                "domain": domain,
                "url": url,
                "tier": tier,
                "task_id": task_id,
                "params_summary": _summarize_params(params or {}),
            },
        })
        logger.info(
            f"browser_permissions: awaiting user decision request_id={request_id} "
            f"action={action} domain={domain} tier={tier}"
        )

        try:
            return await asyncio.wait_for(fut, self._ask_timeout_s)
        except asyncio.TimeoutError:
            self._pending.pop(request_id, None)
            return Decision(kind="timeout", reason=f"no_response_in_{int(self._ask_timeout_s)}s")

    async def _apply_decision(self, d: Decision, domain: str) -> None:
        if not domain:
            return
        async with self._lock:
            if d.kind == "allow_30min":
                self._state.trust_window[domain] = time.time() + TRUST_WINDOW_S
            elif d.kind == "trust_persist":
                self._state.permanent_allowlist.add(domain)
            self._save_state()

    def _audit(
        self,
        action: str,
        domain: str,
        url: str,
        decision: Decision,
        task_id: str | None,
        params: dict | None,
    ) -> None:
        try:
            entry = {
                "ts": time.strftime("%Y-%m-%dT%H:%M:%S%z", time.gmtime()),
                "action": action,
                "domain": domain,
                "url": url,
                "decision_kind": decision.kind,
                "reason": decision.reason,
                "task_id": task_id,
                "params_summary": _summarize_params(params or {}),
            }
            with self._audit_log_path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(entry, ensure_ascii=False) + "\n")
        except Exception as exc:
            logger.warning(f"browser_permissions: audit log write failed: {exc}")

    def _load_state(self) -> None:
        if not self._state_path.exists():
            self._state.permanent_allowlist = set(_DEFAULT_ALLOWLIST)
            self._save_state()
            return
        try:
            data = json.loads(self._state_path.read_text(encoding="utf-8"))
            self._state.permanent_allowlist = set(data.get("permanent_allowlist") or _DEFAULT_ALLOWLIST)
            tw = data.get("trust_window") or {}
            now = time.time()
            self._state.trust_window = {
                d: float(exp) for d, exp in tw.items()
                if isinstance(exp, (int, float)) and float(exp) > now
            }
        except Exception as exc:
            logger.warning(f"browser_permissions: state load failed: {exc}; using defaults")
            self._state.permanent_allowlist = set(_DEFAULT_ALLOWLIST)
            self._state.trust_window = {}

    def _save_state(self) -> None:
        try:
            data = {
                "permanent_allowlist": sorted(self._state.permanent_allowlist),
                "trust_window": self._state.trust_window,
            }
            self._state_path.write_text(
                json.dumps(data, indent=2, ensure_ascii=False),
                encoding="utf-8",
            )
        except Exception as exc:
            logger.warning(f"browser_permissions: state save failed: {exc}")


def _summarize_params(params: dict) -> dict:
    """Strip large fields (like full text inputs) from dashboard payload."""
    out = {}
    for k, v in params.items():
        if k in ("value", "text"):
            s = str(v) if v is not None else ""
            out[k] = (s[:80] + "…") if len(s) > 80 else s
        elif k in ("selector", "text_match", "aria_label", "tab_id", "url"):
            out[k] = v
    return out
