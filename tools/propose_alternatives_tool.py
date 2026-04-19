"""
ProposeAlternativesTool — 结构化"换思路 vs 你拍板"工具

跟 pause_for_human 的区别：
  - pause_for_human：开放式 escalation（"我搞不定，给方向"）
  - propose_alternatives：结构化（"我识别出 N 条可行路线，请你选一条"）

LLM 调用后：
  1. 发 DISCUSSION_STARTED + DISCUSSION_PROPOSALS 事件 → dashboard discussion 面板
  2. 在 task_id 对应的 asyncio.Queue 上阻塞等待 user 选择
  3. 用户选完通过 routes.py POST /tasks/{id}/discussion/input 推到 queue
  4. 工具拿到选择 → 写入 ToolResult 返回 LLM 继续
"""
from __future__ import annotations

import asyncio
import logging
from typing import Optional

from tools.base import BaseTool, ToolParameter, ToolResult

logger = logging.getLogger(__name__)


# 模块级 queue 注册表：每个 task 一个 Queue（routes.py submit_discussion_input 会写入）
_DISCUSSION_QUEUES: dict[str, asyncio.Queue] = {}


def get_discussion_queue(task_id: str) -> asyncio.Queue:
    """routes.py 调，拿（或创建）对应 task 的 discussion queue"""
    if task_id not in _DISCUSSION_QUEUES:
        _DISCUSSION_QUEUES[task_id] = asyncio.Queue()
    return _DISCUSSION_QUEUES[task_id]


class ProposeAlternativesTool(BaseTool):
    def __init__(self, state_manager=None, event_bus=None, timeout_s: int = 1800):
        self._state = state_manager
        self._event_bus = event_bus
        self._timeout_s = timeout_s  # 默认 30 分钟，超时返回 timeout

    @property
    def name(self) -> str:
        return "propose_alternatives"

    @property
    def description(self) -> str:
        return (
            "Present 2-3 concrete alternative approaches to the user and wait for their "
            "choice. Use this when you've identified MULTIPLE viable technical paths and "
            "want the user to decide (e.g., 'use libnfs vs SMB', 'keep dependency vs "
            "simplify'). More structured than pause_for_human (which is open-ended). "
            "BLOCKS until user responds via dashboard or times out (30 min default). "
            "Returns the user's choice as the option's 'id' field, plus any free-form "
            "additional input."
        )

    @property
    def parameters(self) -> list[ToolParameter]:
        return [
            ToolParameter("situation_summary", "string",
                          "1-paragraph summary of why the user needs to choose"),
            ToolParameter("options", "object",
                          "List of {id, title, description, pros, cons} dicts (2-3 items recommended)"),
            ToolParameter("task_id", "string",
                          "Active task id (auto-injected by ReactAgent)",
                          required=False),
        ]

    @property
    def is_read_only(self) -> bool:
        return True

    async def execute(
        self,
        situation_summary: str,
        options: Optional[list] = None,
        task_id: str = "",
    ) -> ToolResult:
        if not task_id:
            return ToolResult(success=False, error="task_id required (auto-injection failed)")
        # ★ 兼容 LLM 传 dict 格式 {"a": "<full text>", "b": ...} —— 转 list
        # 不少 LLM 在 schema type=object 时倾向 dict，规整成 [{"id":k, "description":v}]
        if isinstance(options, dict):
            options = [
                {"id": k, "title": str(v).split("。")[0][:80] if v else k.upper(),
                 "description": str(v) if v else ""}
                for k, v in options.items()
            ]

        if not options or len(options) < 2:
            return ToolResult(
                success=False,
                error="propose_alternatives requires at least 2 options. "
                      "If you only have one path, just continue; if you're stuck, use pause_for_human.",
            )

        # 标准化 options（容错）
        normalized = []
        for i, opt in enumerate(options):
            if not isinstance(opt, dict):
                # 纯字符串选项 — 把字符串当 description，前 80 字当 title
                s = str(opt)
                normalized.append({
                    "id": f"opt_{i}",
                    "title": s.split("。")[0][:80] if s else f"Option {i+1}",
                    "description": s,
                    "pros": "", "cons": "",
                })
                continue
            normalized.append({
                "id": opt.get("id") or f"opt_{i}",
                "title": opt.get("title") or f"Option {i+1}",
                "description": opt.get("description") or "",
                "pros": opt.get("pros") or "",
                "cons": opt.get("cons") or "",
            })

        # 发事件 → dashboard 渲染 discussion 面板
        if self._event_bus:
            try:
                from api.schemas import AgentEvent, EventType
                from datetime import datetime, timezone
                base_ts = datetime.now(timezone.utc)
                await self._event_bus.publish(AgentEvent(
                    event_type=EventType.DISCUSSION_STARTED,
                    task_id=task_id,
                    timestamp=base_ts,
                    data={"summary": situation_summary, "trigger": "agent_propose"},
                ))
                await self._event_bus.publish(AgentEvent(
                    event_type=EventType.DISCUSSION_PROPOSALS,
                    task_id=task_id,
                    timestamp=base_ts,
                    data={"proposals": normalized},
                ))
            except Exception as e:
                logger.warning(f"propose_alternatives: event emit failed: {e}")

        # ★ 把 proposals 存到 state_manager，让 /tasks/{id}/discussion REST 端点能读到
        # （事件流 DISCUSSION_PROPOSALS 只推给 WebSocket，REPL /discuss 走 REST 查询读不到）
        if self._state is not None:
            try:
                self._state.set_discussion(task_id, situation_summary, normalized)
            except Exception as e:
                logger.warning(f"propose_alternatives: set_discussion failed: {e}")

        logger.info(f"propose_alternatives waiting on user input for task {task_id} (timeout={self._timeout_s}s)")

        # 阻塞等用户回应
        queue = get_discussion_queue(task_id)
        try:
            payload = await asyncio.wait_for(queue.get(), timeout=self._timeout_s)
        except asyncio.TimeoutError:
            # 清掉 pending discussion 让 /discuss 不再显示僵尸提案
            if self._state is not None:
                try:
                    self._state.clear_discussion(task_id)
                except Exception:
                    pass
            return ToolResult(
                success=False,
                error=f"propose_alternatives timed out after {self._timeout_s}s waiting for user choice",
            )
        finally:
            # user 响应后也清掉
            if self._state is not None:
                try:
                    self._state.clear_discussion(task_id)
                except Exception:
                    pass

        # routes.py submit_discussion_input 推 dict {action, proposal_id, human_input, additional_constraints}
        choice_id = payload.get("proposal_id") or ""
        human_input = payload.get("human_input") or ""
        constraints = payload.get("additional_constraints") or []

        chosen = next((o for o in normalized if o["id"] == choice_id), None)
        # ★ choice_id 空或无匹配时默认选第一个（"AI 推荐" 语义）
        if chosen is None:
            chosen = normalized[0]
            choice_id = chosen["id"]
            logger.info(f"propose_alternatives: empty/unknown choice, defaulting to first option {choice_id}")
        chosen_title = chosen["title"]

        return ToolResult(
            success=True,
            output=(
                f"User chose: {chosen_title}\n"
                f"id: {choice_id}\n"
                f"additional input: {human_input or '(none)'}\n"
                f"extra constraints: {', '.join(constraints) if constraints else '(none)'}"
            ),
            data={
                "chosen_id": choice_id,
                "chosen": chosen,
                "human_input": human_input,
                "constraints": constraints,
            },
        )
