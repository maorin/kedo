"""
Browser Bridge — WebSocket 网关 for kedo-browser-bridge Chrome 插件

设计参考: docs/deep-dives/browser-bridge-design.md §4.2
协议契约: kedo-browser-bridge/PROTOCOL.md (1.0)
"""
from __future__ import annotations

import asyncio
import logging
import secrets
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Awaitable, Callable, Literal, Optional

from fastapi import WebSocket
from starlette.websockets import WebSocketDisconnect, WebSocketState

logger = logging.getLogger(__name__)

PROTOCOL_VERSION = "1.0"
SERVER_CAPABILITIES = ["context_inbox", "permission_v1"]
HELLO_TIMEOUT_S = 5.0
TOKEN_PATH = Path.home() / ".config" / "kedo" / "browser_token"


def get_or_create_browser_token() -> str:
    """Return the long-lived browser-bridge token; create + persist if missing."""
    if TOKEN_PATH.exists():
        token = TOKEN_PATH.read_text(encoding="utf-8").strip()
        if token:
            return token

    TOKEN_PATH.parent.mkdir(parents=True, exist_ok=True)
    token = secrets.token_urlsafe(24)
    TOKEN_PATH.write_text(token, encoding="utf-8")
    try:
        TOKEN_PATH.chmod(0o600)
    except OSError:
        pass
    logger.info(f"browser_bridge: created token at {TOKEN_PATH}")
    return token


@dataclass
class BrowserSession:
    session_id: str
    role: Literal["user", "agent"]
    ws: WebSocket
    last_heartbeat: float = field(default_factory=time.monotonic)
    client_version: str = ""
    protocol_versions: list[str] = field(default_factory=list)


class BrowserBridge:
    """WebSocket bridge between kedo backend and one or more bridge clients."""

    def __init__(
        self,
        token: str,
        inbox,
        on_inbox_event: Optional[Callable[[dict], Awaitable[None]]] = None,
    ):
        self._token = token
        self._inbox = inbox
        self._on_inbox_event = on_inbox_event
        self._sessions: dict[str, BrowserSession] = {}
        self._pending: dict[str, asyncio.Future] = {}

    async def handle_ws(self, ws: WebSocket, query_token: Optional[str]) -> None:
        # Token may arrive in the query string (preferred) or in the hello frame.
        # Reject early if neither matches; the hello frame will revalidate.
        if query_token is not None and query_token != self._token:
            await ws.close(code=4001, reason="bad_token")
            logger.warning("browser_bridge: rejected ws (query token mismatch)")
            return

        await ws.accept()
        session: Optional[BrowserSession] = None
        try:
            session = await self._handshake(ws)
            if session is None:
                return
            await self._run_session(session)
        except WebSocketDisconnect:
            pass
        except Exception as exc:
            logger.exception(f"browser_bridge: session error: {exc}")
        finally:
            if session is not None:
                self._sessions.pop(session.session_id, None)
                logger.info(f"browser_bridge: session {session.session_id} closed")
            if ws.client_state != WebSocketState.DISCONNECTED:
                try:
                    await ws.close()
                except Exception:
                    pass

    async def _handshake(self, ws: WebSocket) -> Optional[BrowserSession]:
        try:
            hello = await asyncio.wait_for(ws.receive_json(), timeout=HELLO_TIMEOUT_S)
        except (asyncio.TimeoutError, Exception) as exc:
            logger.warning(f"browser_bridge: no hello frame ({exc})")
            await ws.close(code=4002, reason="no_hello")
            return None

        if hello.get("type") != "hello":
            await ws.close(code=4002, reason="protocol_violation")
            return None

        protocol_versions = hello.get("protocol_versions") or []
        if PROTOCOL_VERSION not in protocol_versions:
            await ws.send_json({"type": "hello_nack", "reason": "version_mismatch"})
            await ws.close(code=4002, reason="version_mismatch")
            return None

        if hello.get("token") != self._token:
            await ws.send_json({"type": "hello_nack", "reason": "bad_token"})
            await ws.close(code=4001, reason="bad_token")
            return None

        role_hint = hello.get("role_hint", "user")
        role: Literal["user", "agent"] = "agent" if role_hint == "agent" else "user"

        session_id = uuid.uuid4().hex
        session = BrowserSession(
            session_id=session_id,
            role=role,
            ws=ws,
            client_version=hello.get("client_version", ""),
            protocol_versions=protocol_versions,
        )
        self._sessions[session_id] = session

        await ws.send_json({
            "type": "hello_ack",
            "session_id": session_id,
            "negotiated_protocol": PROTOCOL_VERSION,
            "role": role,
            "server_capabilities": SERVER_CAPABILITIES,
        })
        logger.info(
            f"browser_bridge: session {session_id} role={role} "
            f"client={hello.get('client')} v{hello.get('client_version')}"
        )
        return session

    async def _run_session(self, session: BrowserSession) -> None:
        while True:
            msg = await session.ws.receive_json()
            session.last_heartbeat = time.monotonic()
            await self._dispatch(session, msg)

    async def _dispatch(self, session: BrowserSession, msg: dict) -> None:
        mtype = msg.get("type")

        if mtype == "heartbeat":
            return

        if mtype == "result":
            req_id = msg.get("id")
            fut = self._pending.pop(req_id, None) if req_id else None
            if fut is not None and not fut.done():
                fut.set_result(msg)
            return

        if mtype == "user_inject":
            await self._handle_user_inject(session, msg.get("payload") or {})
            return

        if mtype == "permission_response":
            req_id = msg.get("id")
            fut = self._pending.pop(req_id, None) if req_id else None
            if fut is not None and not fut.done():
                fut.set_result(msg)
            return

        logger.debug(f"browser_bridge: ignored message type={mtype}")

    async def _handle_user_inject(self, session: BrowserSession, payload: dict) -> None:
        try:
            item = await self._inbox.add(payload)
        except Exception as exc:
            logger.error(f"browser_bridge: inbox add failed: {exc}")
            await session.ws.send_json({
                "type": "ack",
                "kind": "user_inject_failed",
                "error": str(exc),
            })
            return

        await session.ws.send_json({
            "type": "ack",
            "kind": "user_inject_received",
            "inbox_item_id": item.id,
        })
        if self._on_inbox_event is not None:
            await self._on_inbox_event({
                "kind": "inbox_item_added",
                "item_id": item.id,
                "title": item.title,
                "url": item.url,
            })

    async def send_command(
        self,
        action: str,
        params: dict[str, Any],
        prefer_role: Literal["user", "agent"] = "user",
        timeout: float = 30.0,
    ) -> dict:
        """M2+ — agent tools call this to run something in the browser. Reserved in M1."""
        session = self._pick_session(prefer_role)
        if session is None:
            raise RuntimeError(f"no_browser_session(role={prefer_role})")

        request_id = uuid.uuid4().hex
        fut: asyncio.Future = asyncio.get_event_loop().create_future()
        self._pending[request_id] = fut
        await session.ws.send_json({
            "type": "command",
            "id": request_id,
            "action": action,
            "params": params,
        })
        try:
            return await asyncio.wait_for(fut, timeout)
        finally:
            self._pending.pop(request_id, None)

    def _pick_session(self, prefer_role: str) -> Optional[BrowserSession]:
        for s in self._sessions.values():
            if s.role == prefer_role:
                return s
        return next(iter(self._sessions.values()), None)

    def status(self) -> dict[str, Any]:
        return {
            "protocol_version": PROTOCOL_VERSION,
            "connected_sessions": [
                {
                    "session_id": s.session_id,
                    "role": s.role,
                    "client_version": s.client_version,
                    "idle_seconds": round(time.monotonic() - s.last_heartbeat, 1),
                }
                for s in self._sessions.values()
            ],
        }
