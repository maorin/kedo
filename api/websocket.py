"""
WebSocket 管理器 — 实时状态推送到 Dashboard

通过 WebSocket 双向通信，将 Agent 的状态变更、日志、进度
实时推送到前端 Dashboard
"""
from __future__ import annotations

import json
import logging
from typing import Any

from fastapi import WebSocket
from starlette.websockets import WebSocketState

from api.schemas import AgentEvent

logger = logging.getLogger(__name__)


class ConnectionManager:
    """WebSocket 连接管理器"""

    def __init__(self):
        self._connections: list[WebSocket] = []
        self._task_subscriptions: dict[str, list[WebSocket]] = {}

    async def connect(self, websocket: WebSocket, task_id: str = None):
        """接受新的 WebSocket 连接"""
        await websocket.accept()
        self._connections.append(websocket)

        if task_id:
            if task_id not in self._task_subscriptions:
                self._task_subscriptions[task_id] = []
            self._task_subscriptions[task_id].append(websocket)

        logger.info(f"WebSocket connected (task={task_id}), total={len(self._connections)}")

    async def disconnect(self, websocket: WebSocket):
        """断开连接"""
        if websocket in self._connections:
            self._connections.remove(websocket)

        for task_id, sockets in self._task_subscriptions.items():
            if websocket in sockets:
                sockets.remove(websocket)

        logger.info(f"WebSocket disconnected, total={len(self._connections)}")

    async def broadcast(self, message: dict[str, Any]):
        """广播消息到所有连接"""
        dead = []
        for ws in self._connections:
            try:
                if ws.client_state == WebSocketState.CONNECTED:
                    await ws.send_json(message)
            except Exception:
                dead.append(ws)

        for ws in dead:
            await self.disconnect(ws)

    async def send_to_task(self, task_id: str, message: dict[str, Any]):
        """发送消息到订阅了特定任务的连接"""
        sockets = self._task_subscriptions.get(task_id, [])
        dead = []
        for ws in sockets:
            try:
                if ws.client_state == WebSocketState.CONNECTED:
                    await ws.send_json(message)
            except Exception:
                dead.append(ws)

        for ws in dead:
            await self.disconnect(ws)

    async def handle_event(self, event: AgentEvent):
        """
        事件处理器 — 注册到 EventBus

        将 Agent 事件转换为 WebSocket 消息并推送
        去重: 已订阅特定任务的连接不会重复收到广播消息
        """
        message = {
            "type": event.event_type.value,
            "task_id": event.task_id,
            "timestamp": event.timestamp.isoformat(),
            "data": event.data,
        }

        # 收集已经发送过的连接 (去重)
        sent: set[int] = set()

        # 1) 发送到订阅了该任务的连接
        task_sockets = self._task_subscriptions.get(event.task_id, [])
        dead = []
        for ws in task_sockets:
            try:
                if ws.client_state == WebSocketState.CONNECTED:
                    await ws.send_json(message)
                    sent.add(id(ws))
            except Exception:
                dead.append(ws)
        for ws in dead:
            await self.disconnect(ws)

        # 2) 广播到其他连接 (跳过已经发送过的)
        dead = []
        for ws in self._connections:
            if id(ws) in sent:
                continue  # 已发过，跳过
            try:
                if ws.client_state == WebSocketState.CONNECTED:
                    await ws.send_json(message)
            except Exception:
                dead.append(ws)
        for ws in dead:
            await self.disconnect(ws)


# 全局连接管理器实例
ws_manager = ConnectionManager()
