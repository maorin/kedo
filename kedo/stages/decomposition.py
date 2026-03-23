"""任务拆解 — Task-decomposition stage.

Takes the structured requirements analysis from the previous stage and asks
the LLM to break the work into an ordered list of development tasks, each
with a title, description, estimated effort, and dependencies.
"""

from __future__ import annotations

import json
from typing import Any, Dict

from kedo.llm.client import LLMClient, LLMMessage
from kedo.stages.base import Stage, StageResult, StageStatus

_SYSTEM_PROMPT = """\
You are a senior engineering lead skilled at breaking software requirements
into concrete, actionable development tasks.

Given a requirements analysis (JSON), return ONLY a valid JSON array (no
markdown fences) where every element has:
  "id"           – sequential integer starting from 1
  "title"        – short task title (string)
  "description"  – detailed description of what needs to be done (string)
  "effort"       – estimated effort: "small" | "medium" | "large"
  "dependencies" – list of task ids (integers) this task depends on

Order tasks so that they can be executed in sequence with dependencies
resolved.  Respond in the same language as the analysis.
"""


class DecompositionStage(Stage):
    """Breaks requirements into an ordered list of development tasks."""

    name = "任务拆解"

    def __init__(self, config: Dict[str, Any] | None = None) -> None:
        super().__init__(config)
        self._llm = LLMClient(
            model=self.config.get("model", "gpt-4o"),
            api_key=self.config.get("api_key"),
            base_url=self.config.get("base_url"),
        )

    def should_skip(self, context: Dict[str, Any]) -> bool:
        return "analysis" not in context

    def run(self, context: Dict[str, Any]) -> StageResult:
        analysis = context.get("analysis", {})

        messages = [
            LLMMessage(role="system", content=_SYSTEM_PROMPT),
            LLMMessage(
                role="user",
                content=f"Requirements analysis:\n{json.dumps(analysis, ensure_ascii=False, indent=2)}",
            ),
        ]
        response = self._llm.chat(messages)

        try:
            tasks = json.loads(response.content)
            if not isinstance(tasks, list):
                tasks = [tasks]
        except json.JSONDecodeError:
            tasks = [{"id": 1, "title": "Implement requirement", "raw": response.content}]

        context["tasks"] = tasks
        return StageResult(
            stage_name=self.name,
            status=StageStatus.SUCCESS,
            data={"tasks": tasks},
            message=f"Decomposed into {len(tasks)} task(s).",
        )
