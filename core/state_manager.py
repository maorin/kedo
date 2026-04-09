"""
状态管理器 — 管理任务状态、持久化检查点、支持暂停恢复
"""
from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Optional

from api.schemas import (
    AgentCheckpoint,
    AgentEvent,
    EventType,
    TaskStatus,
    TaskStatusResponse,
)

logger = logging.getLogger(__name__)


class EventBus:
    """发布/订阅事件总线 — 用于实时状态推送"""

    def __init__(self):
        self._subscribers: dict[str, list[Callable]] = {}
        self._history: list[AgentEvent] = []

    def subscribe(self, event_type: str, callback: Callable):
        if event_type not in self._subscribers:
            self._subscribers[event_type] = []
        self._subscribers[event_type].append(callback)

    def unsubscribe(self, event_type: str, callback: Callable):
        if event_type in self._subscribers:
            self._subscribers[event_type].remove(callback)

    async def publish(self, event: AgentEvent):
        self._history.append(event)
        event_type = event.event_type.value

        # 通知特定事件的订阅者
        for callback in self._subscribers.get(event_type, []):
            try:
                if asyncio.iscoroutinefunction(callback):
                    await callback(event)
                else:
                    callback(event)
            except Exception as e:
                logger.error(f"Event callback error: {e}")

        # 通知通配符订阅者
        for callback in self._subscribers.get("*", []):
            try:
                if asyncio.iscoroutinefunction(callback):
                    await callback(event)
                else:
                    callback(event)
            except Exception as e:
                logger.error(f"Event callback error: {e}")

    def get_history(self, task_id: Optional[str] = None, limit: int = 100) -> list[AgentEvent]:
        events = self._history
        if task_id:
            events = [e for e in events if e.task_id == task_id]
        return events[-limit:]


class StateManager:
    """
    核心状态管理器

    职责:
    - 管理所有任务的状态
    - 持久化 / 恢复检查点 (用于暂停/恢复)
    - 发布状态变更事件
    - 提供任务查询接口
    """

    def __init__(self, storage_dir: str = ".kedo/state"):
        self.storage_path = Path(storage_dir)
        self.storage_path.mkdir(parents=True, exist_ok=True)
        self.event_bus = EventBus()
        self._tasks: dict[str, dict[str, Any]] = {}
        self._checkpoints: dict[str, AgentCheckpoint] = {}
        self._logs: dict[str, list[str]] = {}
        self._pause_events: dict[str, asyncio.Event] = {}

        # 启动时从磁盘加载历史任务索引
        self._load_task_index()

    # ----------------------------------------------------------
    # 任务生命周期
    # ----------------------------------------------------------

    async def create_task(self, task_id: str, description: str, config: dict = None):
        """创建新任务"""
        self._tasks[task_id] = {
            "task_id": task_id,
            "description": description,
            "status": TaskStatus.PENDING,
            "config": config or {},
            "created_at": datetime.utcnow().isoformat(),
            "current_step": None,
            "progress_percent": 0.0,
        }
        self._logs[task_id] = []
        self._pause_events[task_id] = asyncio.Event()
        self._pause_events[task_id].set()  # 初始为未暂停

        await self.event_bus.publish(AgentEvent(
            event_type=EventType.TASK_CREATED,
            task_id=task_id,
            data={"description": description},
        ))
        self._append_log(task_id, f"Task created: {description}")
        self._persist_task_index()

    async def update_status(self, task_id: str, status: TaskStatus, **kwargs):
        """更新任务状态（状态相同时仅更新附加字段，不发布冗余事件）"""
        if task_id not in self._tasks:
            raise ValueError(f"Task {task_id} not found")

        old_status = self._tasks[task_id]["status"]
        self._tasks[task_id]["status"] = status
        self._tasks[task_id].update(kwargs)

        # 如果状态没有实际变化，仅更新附加字段（如 progress_percent），不发布事件
        if old_status == status and not kwargs.get("_force_event"):
            return

        await self.event_bus.publish(AgentEvent(
            event_type=EventType.TASK_STATUS_CHANGED,
            task_id=task_id,
            data={"old_status": old_status.value, "new_status": status.value, **kwargs},
        ))
        self._append_log(task_id, f"Status: {old_status.value} → {status.value}")
        self._persist_task_index()

    # ----------------------------------------------------------
    # 暂停 / 恢复
    # ----------------------------------------------------------

    async def pause_task(self, task_id: str):
        """暂停任务执行"""
        if task_id in self._pause_events:
            self._pause_events[task_id].clear()  # 阻塞等待
            await self.update_status(task_id, TaskStatus.PAUSED)
            await self.event_bus.publish(AgentEvent(
                event_type=EventType.AGENT_PAUSED,
                task_id=task_id,
            ))
            self._append_log(task_id, "Agent paused by user")

    async def resume_task(self, task_id: str):
        """恢复任务执行"""
        if task_id in self._pause_events:
            self._pause_events[task_id].set()  # 解除阻塞
            await self.update_status(task_id, TaskStatus.IN_PROGRESS)
            await self.event_bus.publish(AgentEvent(
                event_type=EventType.AGENT_RESUMED,
                task_id=task_id,
            ))
            self._append_log(task_id, "Agent resumed")

    async def wait_if_paused(self, task_id: str):
        """Agent Loop 中调用 — 如果被暂停则阻塞等待"""
        if task_id in self._pause_events:
            await self._pause_events[task_id].wait()

    # ----------------------------------------------------------
    # 检查点 (Checkpoint)
    # ----------------------------------------------------------

    async def save_checkpoint(self, checkpoint: AgentCheckpoint):
        """保存检查点 — 用于恢复"""
        self._checkpoints[checkpoint.task_id] = checkpoint

        # 持久化到磁盘
        cp_path = self.storage_path / f"{checkpoint.task_id}_checkpoint.json"
        cp_path.write_text(checkpoint.model_dump_json(indent=2), encoding="utf-8")
        self._append_log(checkpoint.task_id, f"Checkpoint saved at step {checkpoint.current_step_index}")

    async def load_checkpoint(self, task_id: str) -> Optional[AgentCheckpoint]:
        """加载检查点"""
        if task_id in self._checkpoints:
            return self._checkpoints[task_id]

        cp_path = self.storage_path / f"{task_id}_checkpoint.json"
        if cp_path.exists():
            data = json.loads(cp_path.read_text(encoding="utf-8"))
            cp = AgentCheckpoint(**data)
            self._checkpoints[task_id] = cp
            return cp
        return None

    # ----------------------------------------------------------
    # 查询接口
    # ----------------------------------------------------------

    def get_task_status(self, task_id: str) -> Optional[TaskStatusResponse]:
        """获取任务状态详情"""
        if task_id not in self._tasks:
            return None
        task = self._tasks[task_id]
        checkpoint = self._checkpoints.get(task_id)
        return TaskStatusResponse(
            task_id=task_id,
            status=task["status"],
            current_step=task.get("current_step"),
            progress_percent=task.get("progress_percent", 0),
            plan=checkpoint.plan if checkpoint else None,
            code_changes=checkpoint.code_changes if checkpoint else [],
            test_results=checkpoint.test_results if checkpoint else None,
            eval_report=checkpoint.eval_report if checkpoint else None,
            logs=self._logs.get(task_id, [])[-50:],
        )

    def list_tasks(self) -> list[dict]:
        """返回序列化友好的任务列表"""
        result = []
        for t in self._tasks.values():
            task_copy = dict(t)
            # 确保 status 是字符串
            if hasattr(task_copy.get("status"), "value"):
                task_copy["status"] = task_copy["status"].value
            result.append(task_copy)
        return result

    # ----------------------------------------------------------
    # 日志
    # ----------------------------------------------------------

    # ----------------------------------------------------------
    # 任务索引持久化 (跨会话)
    # ----------------------------------------------------------

    def _persist_task_index(self):
        """将任务列表持久化到磁盘，供下次启动时恢复"""
        index_path = self.storage_path / "task_index.json"
        entries = []
        for t in self._tasks.values():
            status = t["status"]
            entries.append({
                "task_id": t["task_id"],
                "description": t["description"],
                "status": status.value if hasattr(status, "value") else str(status),
                "created_at": t.get("created_at", ""),
                "current_step": t.get("current_step"),
                "progress_percent": t.get("progress_percent", 0),
            })
        index_path.write_text(json.dumps(entries, ensure_ascii=False, indent=2), encoding="utf-8")

    def _load_task_index(self):
        """启动时从磁盘加载历史任务索引（仅恢复元数据，不恢复运行状态）"""
        index_path = self.storage_path / "task_index.json"
        if not index_path.exists():
            return
        try:
            entries = json.loads(index_path.read_text(encoding="utf-8"))
            for entry in entries:
                task_id = entry["task_id"]
                if task_id in self._tasks:
                    continue  # 已存在则跳过
                # 将保存时的运行中状态标记为中断
                saved_status = entry.get("status", "pending")
                if saved_status in ("in_progress", "planning"):
                    restored_status = TaskStatus.FAILED  # 运行中被中断视为失败
                else:
                    restored_status = TaskStatus(saved_status) if saved_status in TaskStatus.__members__.values() else TaskStatus.PENDING
                # 尝试通过枚举值匹配
                try:
                    restored_status = TaskStatus(saved_status) if saved_status not in ("in_progress", "planning") else TaskStatus.FAILED
                except ValueError:
                    restored_status = TaskStatus.FAILED

                self._tasks[task_id] = {
                    "task_id": task_id,
                    "description": entry.get("description", ""),
                    "status": restored_status,
                    "created_at": entry.get("created_at", ""),
                    "current_step": entry.get("current_step"),
                    "progress_percent": entry.get("progress_percent", 0),
                    "config": {},
                    "_restored": True,  # 标记为从磁盘恢复的
                }
                self._logs[task_id] = []
            logger.info(f"Loaded {len(entries)} historical tasks from disk")
        except Exception as e:
            logger.warning(f"Failed to load task index: {e}")

    def find_resumable_tasks(self) -> list[dict]:
        """查找可续接的历史任务（有 checkpoint 且非完成状态的任务）"""
        resumable = []
        for t in self._tasks.values():
            status = t["status"]
            status_val = status.value if hasattr(status, "value") else str(status)
            # 完成的任务不可续接，其余有 checkpoint 的都可以
            if status_val in ("completed",):
                continue
            # 检查是否有 checkpoint 文件
            cp_path = self.storage_path / f"{t['task_id']}_checkpoint.json"
            if cp_path.exists():
                resumable.append({
                    "task_id": t["task_id"],
                    "description": t["description"],
                    "status": status_val,
                    "created_at": t.get("created_at", ""),
                    "current_step": t.get("current_step"),
                    "progress_percent": t.get("progress_percent", 0),
                })
        # 排序：进度高的优先，进度相同按时间倒序。进度 0% 的 failed 任务排最后。
        resumable.sort(key=lambda x: (
            x.get("progress_percent", 0) > 0,   # 有进度的排前面
            x.get("progress_percent", 0),         # 进度高的优先
            x.get("created_at", ""),              # 同进度按时间倒序
        ), reverse=True)
        return resumable

    def find_matching_task(self, description: str) -> Optional[str]:
        """根据描述关键词匹配最相关的可续接任务"""
        import re
        # 提取关键词（去掉常见停用词）
        stop_words = {"的", "了", "在", "是", "我", "有", "和", "就", "不", "人", "都",
                      "一", "上", "也", "很", "到", "说", "要", "去", "你", "会", "着",
                      "没有", "看", "好", "自己", "这", "他", "她", "吧", "被", "比",
                      "还", "等", "当", "又", "能", "做", "再", "之前", "按", "继续",
                      "之前的", "上次", "接着", "重新", "按之前", "写一个", "一个",
                      "the", "a", "an", "is", "are", "was", "were", "be", "been",
                      "continue", "resume", "previous", "last", "before"}
        # 中文按字符分词 + 英文按空格分词
        words = set(re.findall(r'[\u4e00-\u9fff]+|[a-zA-Z]+', description.lower()))
        keywords = words - stop_words

        if not keywords:
            return None

        resumable = self.find_resumable_tasks()
        best_match = None
        best_score = 0

        for task in resumable:
            task_words = set(re.findall(r'[\u4e00-\u9fff]+|[a-zA-Z]+', task["description"].lower()))
            # 计算关键词重叠
            overlap = len(keywords & task_words)
            # 子串匹配加分
            for kw in keywords:
                if len(kw) >= 2 and kw in task["description"].lower():
                    overlap += 1
            if overlap > best_score:
                best_score = overlap
                best_match = task["task_id"]

        return best_match if best_score > 0 else None

    # ----------------------------------------------------------
    # 日志
    # ----------------------------------------------------------

    def _append_log(self, task_id: str, message: str):
        ts = datetime.utcnow().strftime("%H:%M:%S")
        log_line = f"[{ts}] {message}"
        if task_id not in self._logs:
            self._logs[task_id] = []
        self._logs[task_id].append(log_line)
