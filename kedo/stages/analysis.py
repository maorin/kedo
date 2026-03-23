"""需求分析 — Requirements-analysis stage.

Sends the raw user requirement to the LLM and asks it to produce a structured
analysis containing: background, goals, scope, constraints, and acceptance
criteria.
"""

from __future__ import annotations

import json
from typing import Any, Dict

from kedo.llm.client import LLMClient, LLMMessage
from kedo.stages.base import Stage, StageResult, StageStatus

_SYSTEM_PROMPT = """\
You are a senior software architect and requirements analyst.
Analyse the user's natural-language requirement and return ONLY a valid JSON
object (no markdown fences) with exactly these keys:
  "background"   – 1-3 sentences of context
  "goals"        – list of specific goals (strings)
  "scope"        – what is in scope (list of strings)
  "constraints"  – technical or non-technical constraints (list of strings)
  "acceptance_criteria" – measurable criteria for completion (list of strings)
Respond in the same language as the user's requirement.
"""


class AnalysisStage(Stage):
    """Performs LLM-powered requirements analysis."""

    name = "需求分析"

    def __init__(self, config: Dict[str, Any] | None = None) -> None:
        super().__init__(config)
        self._llm = LLMClient(
            model=self.config.get("model", "gpt-4o"),
            api_key=self.config.get("api_key"),
            base_url=self.config.get("base_url"),
        )

    def run(self, context: Dict[str, Any]) -> StageResult:
        requirement = context.get("requirement", "")
        if not requirement:
            return StageResult(
                stage_name=self.name,
                status=StageStatus.FAILED,
                message="No requirement found in context.",
            )

        messages = [
            LLMMessage(role="system", content=_SYSTEM_PROMPT),
            LLMMessage(role="user", content=requirement),
        ]
        response = self._llm.chat(messages)

        try:
            analysis = json.loads(response.content)
        except json.JSONDecodeError:
            # Fall back: wrap the raw text so downstream stages still work
            analysis = {"raw": response.content}

        context["analysis"] = analysis
        return StageResult(
            stage_name=self.name,
            status=StageStatus.SUCCESS,
            data={"analysis": analysis},
            message="Requirements analysis completed.",
        )
