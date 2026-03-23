"""评估 — Evaluation stage.

Asks the LLM to evaluate the generated code and tests against the original
requirements, producing a scored report with strengths, weaknesses, and
improvement suggestions.
"""

from __future__ import annotations

import json
from typing import Any, Dict

from kedo.llm.client import LLMClient, LLMMessage
from kedo.stages.base import Stage, StageResult, StageStatus

_SYSTEM_PROMPT = """\
You are a principal engineer conducting a technical review.
Given the requirements analysis, the generated code artifacts, and the test
artifacts (all JSON), evaluate the overall implementation quality.

Return ONLY a valid JSON object (no markdown fences) with:
  "score"            – integer 0-100 representing overall quality
  "coverage_score"   – integer 0-100: how completely requirements are covered
  "code_quality_score" – integer 0-100: code quality assessment
  "test_quality_score" – integer 0-100: test coverage and quality
  "strengths"        – list of positive observations (strings)
  "weaknesses"       – list of identified issues (strings)
  "suggestions"      – list of concrete improvement actions (strings)
  "ready_for_review" – boolean: whether the code is ready for human review

Respond in the same language as the requirements.
"""


class EvaluationStage(Stage):
    """Evaluates implementation quality against requirements."""

    name = "评估"

    def __init__(self, config: Dict[str, Any] | None = None) -> None:
        super().__init__(config)
        self._llm = LLMClient(
            model=self.config.get("model", "gpt-4o"),
            api_key=self.config.get("api_key"),
            base_url=self.config.get("base_url"),
        )

    def should_skip(self, context: Dict[str, Any]) -> bool:
        return "generated_code" not in context

    def run(self, context: Dict[str, Any]) -> StageResult:
        payload = {
            "analysis": context.get("analysis", {}),
            "generated_code": context.get("generated_code", []),
            "test_artifacts": context.get("test_artifacts", []),
        }

        messages = [
            LLMMessage(role="system", content=_SYSTEM_PROMPT),
            LLMMessage(
                role="user",
                content=json.dumps(payload, ensure_ascii=False, indent=2),
            ),
        ]
        response = self._llm.chat(messages)

        try:
            evaluation = json.loads(response.content)
        except json.JSONDecodeError:
            evaluation = {"raw": response.content, "ready_for_review": True}

        context["evaluation"] = evaluation
        score = evaluation.get("score", "N/A")
        ready = evaluation.get("ready_for_review", True)
        return StageResult(
            stage_name=self.name,
            status=StageStatus.SUCCESS,
            data={"evaluation": evaluation},
            message=f"Evaluation score: {score}/100. Ready for review: {ready}.",
        )
