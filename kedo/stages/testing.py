"""测试 — Testing stage.

Asks the LLM to produce unit tests for every generated code artifact, then
reports a test plan that a human or CI system can execute.
"""

from __future__ import annotations

import json
from typing import Any, Dict, List

from kedo.llm.client import LLMClient, LLMMessage
from kedo.stages.base import Stage, StageResult, StageStatus

_SYSTEM_PROMPT = """\
You are a senior QA engineer and test automation specialist.
Given a source file (JSON with 'file_path', 'language', 'code', and optional
'notes'), write comprehensive unit tests for it.

Return ONLY a valid JSON object (no markdown fences) with:
  "test_file_path" – suggested relative path for the test file (string)
  "language"       – programming language of the tests (string)
  "test_code"      – complete test source code (string)
  "test_cases"     – list of test case titles (strings) covered by the tests

Use the standard test framework for the language (pytest for Python, Jest for
JavaScript/TypeScript, JUnit for Java, etc.).
"""


class TestingStage(Stage):
    """Generates unit tests for every code artifact."""

    name = "测试"

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
        artifacts: List[Dict[str, Any]] = context.get("generated_code", [])

        test_artifacts: List[Dict[str, Any]] = []
        for artifact in artifacts:
            messages = [
                LLMMessage(role="system", content=_SYSTEM_PROMPT),
                LLMMessage(
                    role="user",
                    content=f"Source file:\n{json.dumps(artifact, ensure_ascii=False, indent=2)}",
                ),
            ]
            response = self._llm.chat(messages)
            try:
                test_artifact = json.loads(response.content)
            except json.JSONDecodeError:
                test_artifact = {
                    "test_file_path": f"test_{artifact.get('file_path', 'unknown')}",
                    "test_code": response.content,
                }
            test_artifact["task_id"] = artifact.get("task_id")
            test_artifacts.append(test_artifact)

        context["test_artifacts"] = test_artifacts
        total_cases = sum(len(t.get("test_cases", [])) for t in test_artifacts)
        return StageResult(
            stage_name=self.name,
            status=StageStatus.SUCCESS,
            data={"test_artifacts": test_artifacts},
            message=f"Generated {len(test_artifacts)} test file(s) covering {total_cases} test case(s).",
        )
