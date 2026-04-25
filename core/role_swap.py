"""
RoleSwapManager — Producer ↔ Reviewer 角色对换状态机

为什么需要：
2026-04-25 实战中观察到 Producer 反复被 Reviewer 拒后无路可走（直接 respond 又被
RejectTracker 屏蔽）。增加一道"自动救援"：3 次拒后让原 Reviewer 的 LLM 上位当
Producer 再试一次，原 Producer 的 LLM 下来当 Reviewer 审。这是真正的"换思路"——
不只是换 prompt，是换另一个 LLM 的训练分布上手。

状态机：
    NORMAL ──(commit_candidate 拒到阈值，can_swap)──→ SWAPPED
    SWAPPED ──(commit_candidate approve)──→ NORMAL
    SWAPPED ──(再次拒到阈值)──→ 反弹给 RejectTracker，强制 pause_for_human
                                  → on_escalate 后由 ReactAgent 调 restore() → NORMAL

关键不变量：
- swap 后写代码的（ReactAgent.llm）和审的（Reviewer._llm）始终是不同 LLM 实例
- 因此方案 C 的 Self-eval drift 屏障**保留**，只是双方角色交换
- 任何升级人工的退出路径都伴随 restore，让用户/dashboard 看到一致的"原始配置"

使用方式（server.py 接线时）：
    role_swap = RoleSwapManager(enabled=cfg.get("enable_swap_on_reject", False))
    # ... create react_agent 和 reviewer ...
    role_swap.bind(react_agent=react_agent, reviewer=reviewer)
    # CommitCandidateTool / ReactAgent 都拿这个对象引用
"""
from __future__ import annotations

import logging
import threading
from typing import Optional

logger = logging.getLogger(__name__)


class RoleSwapManager:
    """跨组件共享的 Producer ↔ Reviewer 角色对换管理器。线程安全。"""

    NORMAL = "normal"
    SWAPPED = "swapped"

    def __init__(self, enabled: bool = False):
        self._enabled_flag = bool(enabled)
        self._ra = None       # ReactAgent
        self._reviewer = None # Reviewer
        self._state: dict[str, str] = {}
        self._snapshot: dict[str, dict] = {}
        self._lock = threading.Lock()

    # ----------------------------------------------------------
    # 接线
    # ----------------------------------------------------------

    def bind(self, react_agent, reviewer) -> None:
        """server.py 在 react_agent 和 reviewer 都创建好后调一次。"""
        self._ra = react_agent
        self._reviewer = reviewer

    @property
    def enabled(self) -> bool:
        """是否启用 swap 机制。需要 enable_swap_on_reject=True 且 react_agent + reviewer 都已绑定。"""
        return self._enabled_flag and self._ra is not None and self._reviewer is not None

    # ----------------------------------------------------------
    # 状态查询
    # ----------------------------------------------------------

    def state_for(self, task_id: str) -> str:
        if not task_id:
            return self.NORMAL
        with self._lock:
            return self._state.get(task_id, self.NORMAL)

    def is_swapped(self, task_id: str) -> bool:
        return self.state_for(task_id) == self.SWAPPED

    def can_swap(self, task_id: str) -> bool:
        """是否允许首次 swap：启用了 + 当前是 NORMAL"""
        return self.enabled and self.state_for(task_id) == self.NORMAL

    # ----------------------------------------------------------
    # swap / restore
    # ----------------------------------------------------------

    def do_swap(
        self,
        task_id: str,
        *,
        missed: Optional[list] = None,
        risks: Optional[list] = None,
    ) -> str:
        """
        执行 Producer ↔ Reviewer 对换。
        返回供调用方追加到 LLM 上下文的 swap 公告文本（空串表示未执行）。

        一旦 swap：
        - ReactAgent.llm 切到原 Reviewer 的 LLM 客户端
        - Reviewer._llm + Reviewer._inner._llm 切到原 Producer 的 LLM
        - Reviewer._history 临时清空（新 reviewer 不继承前任偏见）
        """
        if not self.can_swap(task_id):
            return ""

        with self._lock:
            ra, rv = self._ra, self._reviewer
            if ra is None or rv is None:
                return ""

            prev_producer = type(ra.llm).__name__
            prev_reviewer = type(rv._llm).__name__

            # 备份
            snap = {
                "ra_llm": ra.llm,
                "rv_llm": rv._llm,
                "rv_history": list(rv._history),
                "rv_inner_llm": rv._inner._llm,
            }
            self._snapshot[task_id] = snap

            # swap
            ra.llm = snap["rv_llm"]
            rv._llm = snap["ra_llm"]
            rv._inner._llm = snap["ra_llm"]
            rv._history = []

            # 清掉 ReactAgent 的 function-calling 探测结果 — 新 LLM 可能能力不同
            # 这样下次调用会重新探测一次而不是延续旧客户端的判断
            ra._function_calling_available = None

            self._state[task_id] = self.SWAPPED

        new_producer = type(self._ra.llm).__name__
        new_reviewer = type(self._reviewer._llm).__name__
        logger.info(
            f"RoleSwap[{task_id}] SWAPPED: "
            f"Producer {prev_producer}→{new_producer}, "
            f"Reviewer {prev_reviewer}→{new_reviewer}"
        )

        miss_summary = "; ".join((missed or [])[:3]) or "(none reported)"
        risk_summary = "; ".join((risks or [])[:3]) or "(none reported)"
        return (
            f"\n\n=== 🔄 ROLE SWAP EXECUTED ===\n"
            f"The previous Producer ({prev_producer}) was rejected 3 consecutive times "
            f"by the Reviewer. To break the deadlock, roles have been swapped:\n"
            f"  Producer: {prev_producer} → {new_producer}\n"
            f"  Reviewer: {prev_reviewer} → {new_reviewer}\n\n"
            f"You ({new_producer}) are now the Producer. Address the unresolved findings:\n"
            f"  Missed requirements: {miss_summary}\n"
            f"  Risks: {risk_summary}\n\n"
            f"If you also exhaust 3 commit attempts without approval, the system will "
            f"escalate to human (force pause_for_human). After approval the original "
            f"roles will restore.\n"
            f"=================================="
        )

    def restore(self, task_id: str) -> str:
        """commit_candidate approve 或升级人工时调用，恢复原始 LLM 配置。"""
        if not self.is_swapped(task_id):
            return ""

        with self._lock:
            ra, rv = self._ra, self._reviewer
            snap = self._snapshot.pop(task_id, None)
            self._state.pop(task_id, None)
            if not snap or ra is None or rv is None:
                return ""

            prev_producer = type(ra.llm).__name__

            ra.llm = snap["ra_llm"]
            rv._llm = snap["rv_llm"]
            rv._inner._llm = snap["rv_inner_llm"]
            rv._history = snap["rv_history"]
            ra._function_calling_available = None  # 重新探测

        new_producer = type(self._ra.llm).__name__
        new_reviewer = type(self._reviewer._llm).__name__
        logger.info(
            f"RoleSwap[{task_id}] RESTORED: Producer {prev_producer}→{new_producer}"
        )
        return (
            f"\n\n=== 🔁 ROLE RESTORED ===\n"
            f"Producer: {new_producer}, Reviewer: {new_reviewer}.\n"
            f"=========================="
        )

    def reset(self, task_id: str) -> None:
        """新 task 起点 / 强制清理。"""
        if self.is_swapped(task_id):
            self.restore(task_id)
        with self._lock:
            self._state.pop(task_id, None)
            self._snapshot.pop(task_id, None)
