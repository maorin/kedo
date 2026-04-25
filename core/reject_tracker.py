"""
RejectTracker — Reviewer 拒绝累计 + escalation 状态

为什么需要：
2026-04-25 实战中观察到 Producer 在 commit_candidate 连续被 Reviewer 拒 6+ 次后，
选择"绕过 Reviewer 直接 respond 用户"作为逃生路径，导致任务被标 COMPLETED 但实际
未解决 Reviewer 指出的关键缺陷（如 NFS 是 stub、audio 截断、测试覆盖 5 分）。

护栏逻辑（双层）：
  A. CommitCandidateTool: 同 task_id 累计 reject 次数；到阈值时 ToolResult 的
     error/output 改为强制建议（"必须先 pause_for_human / propose_alternatives，
     不要直接 respond"）。这是 LLM 看到的软提示。
  B. ReactAgent: 在 LLM 想调 respond 前查 should_block_respond；为 True 时把
     respond 调用合成失败，让 LLM 必须先调 pause_for_human / propose_alternatives
     才能解锁 respond。这是物理硬约束，LLM 即便忽略 A 的提示也躲不掉。

设计为单实例跨 task 共用：__init__ 一次，server.py 注入到两个组件。
按 task_id 分桶；新 task 开始时 ReactAgent 调 reset(task_id) 清掉历史。
"""
from __future__ import annotations

import logging
import threading

logger = logging.getLogger(__name__)


# 配合 ReactAgent 识别"算 escalation"的工具名
ESCALATION_TOOL_NAMES = frozenset({"pause_for_human", "propose_alternatives"})


class RejectTracker:
    """跨组件共享的 Reviewer 拒绝累计 + escalation 状态。线程安全。"""

    def __init__(self, escalation_threshold: int = 3):
        self.escalation_threshold = max(1, int(escalation_threshold))
        self._counts: dict[str, int] = {}
        self._unhandled: dict[str, bool] = {}
        self._lock = threading.Lock()

    # ----------------------------------------------------------
    # CommitCandidateTool 入口
    # ----------------------------------------------------------

    def on_reject(self, task_id: str) -> int:
        """记一次拒绝；返回当前 task 的累计拒次。task_id 空则忽略。"""
        if not task_id:
            return 0
        with self._lock:
            n = self._counts.get(task_id, 0) + 1
            self._counts[task_id] = n
            self._unhandled[task_id] = True
            logger.info(
                f"RejectTracker[{task_id}] reject count → {n} "
                f"(threshold={self.escalation_threshold}, blocking respond={n >= self.escalation_threshold})"
            )
            return n

    def on_approve(self, task_id: str) -> None:
        """commit 通过 → 清掉拒绝累计。"""
        if not task_id:
            return
        with self._lock:
            had = self._counts.pop(task_id, None)
            self._unhandled.pop(task_id, None)
            if had:
                logger.info(f"RejectTracker[{task_id}] approve → reset (was {had} rejects)")

    # ----------------------------------------------------------
    # ReactAgent 入口
    # ----------------------------------------------------------

    def on_escalate(self, task_id: str) -> None:
        """Producer 调了 pause_for_human / propose_alternatives → 解锁 respond
        （但不清空累计计数 — 下一次 reject 仍 +1 落进同一桶）"""
        if not task_id:
            return
        with self._lock:
            had = self._unhandled.pop(task_id, None)
            if had:
                logger.info(f"RejectTracker[{task_id}] escalation acknowledged → respond unblocked")

    def reject_count(self, task_id: str) -> int:
        if not task_id:
            return 0
        with self._lock:
            return self._counts.get(task_id, 0)

    def has_unhandled_reject(self, task_id: str) -> bool:
        if not task_id:
            return False
        with self._lock:
            return self._unhandled.get(task_id, False)

    def should_block_respond(self, task_id: str) -> bool:
        """True ⇒ ReactAgent 应屏蔽 respond，强制先升级。"""
        if not task_id:
            return False
        with self._lock:
            return (
                self._counts.get(task_id, 0) >= self.escalation_threshold
                and self._unhandled.get(task_id, False)
            )

    def reset(self, task_id: str) -> None:
        """新 task / 强制清。"""
        if not task_id:
            return
        with self._lock:
            self._counts.pop(task_id, None)
            self._unhandled.pop(task_id, None)
