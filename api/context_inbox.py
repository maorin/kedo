"""
Context Inbox — 用户从浏览器插件喂过来的网页落点

设计参考: docs/deep-dives/browser-bridge-design.md §6
存储: ~/.config/kedo/inbox/items.jsonl + screenshots/
"""
from __future__ import annotations

import asyncio
import base64
import json
import logging
import secrets
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)


@dataclass
class InboxItem:
    id: str
    received_at: str                       # ISO 8601
    source: str = "browser_inject"
    url: Optional[str] = None
    title: Optional[str] = None
    content: str = ""                      # Readability textContent
    excerpt: Optional[str] = None
    selection: Optional[str] = None
    screenshot_path: Optional[str] = None
    user_note: Optional[str] = None
    used_in_tasks: list[str] = field(default_factory=list)


class ContextInbox:
    """Per-user inbox of browser-injected pages, persisted as JSONL."""

    def __init__(self, base_dir: Path | str | None = None):
        if base_dir is None:
            base_dir = Path.home() / ".config" / "kedo" / "inbox"
        self.base_dir = Path(base_dir)
        self.items_file = self.base_dir / "items.jsonl"
        self.screenshots_dir = self.base_dir / "screenshots"
        self.base_dir.mkdir(parents=True, exist_ok=True)
        self.screenshots_dir.mkdir(parents=True, exist_ok=True)
        self._lock = asyncio.Lock()

    async def add(self, payload: dict) -> InboxItem:
        item_id = secrets.token_urlsafe(8)
        screenshot_path = self._save_screenshot(item_id, payload.get("screenshot_data_url"))

        item = InboxItem(
            id=item_id,
            received_at=datetime.now(timezone.utc).isoformat(),
            source="browser_inject",
            url=payload.get("url"),
            title=payload.get("title"),
            content=payload.get("dom_text") or "",
            excerpt=payload.get("excerpt"),
            selection=payload.get("selection"),
            screenshot_path=screenshot_path,
            user_note=payload.get("user_note"),
        )

        async with self._lock:
            with self.items_file.open("a", encoding="utf-8") as f:
                f.write(json.dumps(asdict(item), ensure_ascii=False) + "\n")

        logger.info(f"context_inbox: added {item_id} ({item.url})")
        return item

    async def list(self, limit: int = 100) -> list[InboxItem]:
        if not self.items_file.exists():
            return []
        items: list[InboxItem] = []
        async with self._lock:
            with self.items_file.open("r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        items.append(InboxItem(**json.loads(line)))
                    except Exception as exc:
                        logger.warning(f"context_inbox: skip bad line: {exc}")
        items.sort(key=lambda x: x.received_at, reverse=True)
        return items[:limit]

    async def get(self, item_id: str) -> Optional[InboxItem]:
        for item in await self.list(limit=10_000):
            if item.id == item_id:
                return item
        return None

    async def delete(self, item_id: str) -> bool:
        items = await self.list(limit=10_000)
        kept = [it for it in items if it.id != item_id]
        if len(kept) == len(items):
            return False
        async with self._lock:
            with self.items_file.open("w", encoding="utf-8") as f:
                for it in kept:
                    f.write(json.dumps(asdict(it), ensure_ascii=False) + "\n")
        for ext in ("png", "jpg", "jpeg", "webp"):
            p = self.screenshots_dir / f"{item_id}.{ext}"
            if p.exists():
                p.unlink()
        logger.info(f"context_inbox: deleted {item_id}")
        return True

    async def attach_to_task(self, item_ids: list[str], task_id: str) -> list[InboxItem]:
        items = await self.list(limit=10_000)
        attached: list[InboxItem] = []
        for it in items:
            if it.id in item_ids and task_id not in it.used_in_tasks:
                it.used_in_tasks.append(task_id)
                attached.append(it)
        async with self._lock:
            with self.items_file.open("w", encoding="utf-8") as f:
                for it in items:
                    f.write(json.dumps(asdict(it), ensure_ascii=False) + "\n")
        return attached

    def _save_screenshot(self, item_id: str, data_url: Optional[str]) -> Optional[str]:
        if not data_url or not data_url.startswith("data:image/"):
            return None
        try:
            header, b64 = data_url.split(",", 1)
            ext = "png" if "png" in header else "jpg"
            path = self.screenshots_dir / f"{item_id}.{ext}"
            path.write_bytes(base64.b64decode(b64))
            return str(path)
        except Exception as exc:
            logger.warning(f"context_inbox: screenshot save failed: {exc}")
            return None
