"""代码生成 — Code-generation stage.

For every task produced by the decomposition stage, asks the LLM to generate
the implementation code together with a suggested file path.
"""

from __future__ import annotations

import json
from typing import Any, Dict, List

from kedo.llm.client import LLMClient, LLMMessage
from kedo.stages.base import Stage, StageResult, StageStatus

_SYSTEM_PROMPT = """\
You are an expert software engineer.
Given a development task (JSON) and the overall requirements analysis (JSON),
generate the implementation.

Return ONLY a valid JSON object (no markdown fences) with:
  "file_path"  – suggested relative file path for this code (string)
  "language"   – programming language (string)
  "code"       – complete, runnable source code (string)
  "notes"      – brief implementation notes (string)

Write clean, well-documented, production-ready code.
"""


def _generate_for_task(
    llm: LLMClient,
    task: Dict[str, Any],
    analysis: Dict[str, Any],
) -> Dict[str, Any]:
    messages = [
        LLMMessage(role="system", content=_SYSTEM_PROMPT),
        LLMMessage(
            role="user",
            content=(
                f"Task:\n{json.dumps(task, ensure_ascii=False, indent=2)}\n\n"
                f"Requirements analysis:\n{json.dumps(analysis, ensure_ascii=False, indent=2)}"
            ),
        ),
    ]
    response = llm.chat(messages)
    try:
        result = json.loads(response.content)
    except json.JSONDecodeError:
        result = {"file_path": f"task_{task.get('id', 0)}.py", "code": response.content}
    result["task_id"] = task.get("id")
    return result


class CodegenStage(Stage):
    """Generates implementation code for every decomposed task."""

    name = "代码生成"

    def __init__(self, config: Dict[str, Any] | None = None) -> None:
        super().__init__(config)
        self._llm = LLMClient(
            model=self.config.get("model", "gpt-4o"),
            api_key=self.config.get("api_key"),
            base_url=self.config.get("base_url"),
        )

    def should_skip(self, context: Dict[str, Any]) -> bool:
        return "tasks" not in context

    def run(self, context: Dict[str, Any]) -> StageResult:
        tasks: List[Dict[str, Any]] = context.get("tasks", [])
        analysis = context.get("analysis", {})

        generated: List[Dict[str, Any]] = []
        for task in tasks:
            artifact = _generate_for_task(self._llm, task, analysis)
            generated.append(artifact)

        context["generated_code"] = generated
        return StageResult(
            stage_name=self.name,
            status=StageStatus.SUCCESS,
            data={"generated_code": generated},
            message=f"Generated code for {len(generated)} task(s).",
        )
