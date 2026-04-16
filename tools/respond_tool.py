"""
回复工具 — Agent 调用此工具表示已完成任务，向用户输出最终回复
"""
from __future__ import annotations

from tools.base import BaseTool, ToolParameter, ToolResult


class RespondTool(BaseTool):
    """Agent 用来向用户发送最终回复的工具"""

    @property
    def name(self) -> str:
        return "respond"

    @property
    def description(self) -> str:
        return (
            "Send a final response to the user. Use this when you have completed "
            "the user's request and want to deliver the result or answer. "
            "After calling this tool, the conversation turn ends."
        )

    @property
    def parameters(self) -> list[ToolParameter]:
        return [
            ToolParameter("message", "string", "The response message to send to the user"),
        ]

    @property
    def is_read_only(self) -> bool:
        return True

    async def execute(self, message: str) -> ToolResult:
        return ToolResult(
            success=True,
            output=message,
            data={"type": "final_response", "message": message},
        )
