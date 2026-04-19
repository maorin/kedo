"""
PauseForHumanTool — Agent 自评 "我搞不定了" 时主动调用，触发暂停 + 等待人工建议
"""
from __future__ import annotations

import logging

from tools.base import BaseTool, ToolParameter, ToolResult

logger = logging.getLogger(__name__)


class PauseForHumanTool(BaseTool):
    """
    把 escalation 主动权交给 LLM。

    使用场景：
      - 同一个错误已经修过 N 次还在复现
      - 错误是环境/依赖问题，单文件改不动
      - LLM 想在两条技术路线之间让用户拍板

    行为：
      - 发 STEP_FAILED 事件，escalation=paused_for_human + suggestion 写到事件 data
      - 调 state.pause_task → ReactAgent 主循环 wait_if_paused 处会卡住等 resume
      - 用户在 dashboard / API 提供 additional_context 后 resume，messages 末尾追加 user 消息继续 ReAct
    """

    def __init__(self, state_manager=None, event_bus=None):
        self._state = state_manager
        self._event_bus = event_bus

    @property
    def name(self) -> str:
        return "pause_for_human"

    @property
    def description(self) -> str:
        return (
            "Pause the task and ask a human for guidance. Use this when you genuinely "
            "cannot make progress: the same error keeps recurring after multiple fix "
            "attempts, the failure is structural (missing system package, wrong toolchain, "
            "ambiguous requirement), or you need the user to choose between two valid "
            "technical approaches. The dashboard will show a pause banner with your "
            "summary + suggestion. Do NOT use this for normal failures that you can "
            "iterate on yourself."
        )

    @property
    def parameters(self) -> list[ToolParameter]:
        return [
            ToolParameter(
                "summary", "string",
                "One-paragraph summary of where you are stuck (what you tried, what's failing).",
            ),
            ToolParameter(
                "suggestion", "string",
                "Concrete options for the user to consider, e.g. 'install package X / switch to library Y / clarify requirement Z'.",
            ),
            ToolParameter(
                "task_id", "string",
                "Active task id (auto-injected by ReactAgent)",
                required=False,
            ),
        ]

    @property
    def is_read_only(self) -> bool:
        return True  # 不写文件，安全可重入

    async def execute(
        self,
        summary: str,
        suggestion: str = "",
        task_id: str = "",
    ) -> ToolResult:
        if not task_id or not self._state:
            return ToolResult(
                success=False,
                error="pause_for_human requires task_id + state_manager wiring",
            )

        # 发暂停事件 — dashboard 监听到后渲染 banner
        if self._event_bus:
            try:
                from api.schemas import AgentEvent, EventType
                from datetime import datetime, timezone
                event = AgentEvent(
                    event_type=EventType.STEP_FAILED,
                    task_id=task_id,
                    timestamp=datetime.now(timezone.utc),
                    data={
                        "step": "agent_self_pause",
                        "error": summary,
                        "escalation": "paused_for_human",
                        "suggestion": suggestion,
                    },
                )
                await self._event_bus.publish(event)
            except Exception as e:
                logger.warning(f"pause_for_human: event emit failed: {e}")

        await self._state.pause_task(task_id)
        logger.warning(f"Task {task_id} paused by agent self-assessment: {summary[:100]}")

        return ToolResult(
            success=True,
            output=(
                f"Task paused for human review.\n"
                f"Summary: {summary}\n"
                f"Suggestion: {suggestion or '(none)'}\n"
                f"Waiting for user to provide additional context via /resume or dashboard."
            ),
            data={"paused": True, "summary": summary, "suggestion": suggestion},
        )
