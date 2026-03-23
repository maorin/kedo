"""人工审查 — Human-review stage.

Presents the generated code, tests, and evaluation report to the user and
asks for approval before proceeding to deployment.  When running in
non-interactive mode (``auto_approve=True`` in config) the stage is
automatically approved.
"""

from __future__ import annotations

import sys
from typing import Any, Dict

from kedo.stages.base import Stage, StageResult, StageStatus


class ReviewStage(Stage):
    """Pauses the pipeline for human review and approval."""

    name = "人工审查"

    def should_skip(self, context: Dict[str, Any]) -> bool:
        # Skip if the evaluation explicitly said it's not ready for review
        evaluation = context.get("evaluation", {})
        return not evaluation.get("ready_for_review", True)

    def run(self, context: Dict[str, Any]) -> StageResult:
        auto_approve: bool = self.config.get("auto_approve", False)

        if auto_approve:
            context["review_approved"] = True
            return StageResult(
                stage_name=self.name,
                status=StageStatus.SUCCESS,
                data={"approved": True},
                message="Auto-approved (non-interactive mode).",
            )

        # Interactive approval -------------------------------------------------
        evaluation = context.get("evaluation", {})
        score = evaluation.get("score", "N/A")
        weaknesses = evaluation.get("weaknesses", [])

        print(f"\n{'=' * 60}")
        print("人工审查 — Human Review")
        print(f"{'=' * 60}")
        print(f"Overall score : {score}/100")
        if weaknesses:
            print("Issues found  :")
            for w in weaknesses:
                print(f"  • {w}")

        generated = context.get("generated_code", [])
        for artifact in generated:
            print(f"\n── {artifact.get('file_path', 'unknown')} ──")
            print(artifact.get("code", ""))

        print(f"\n{'=' * 60}")
        answer = _prompt_user("Approve and proceed to deployment? [y/N] ")
        approved = answer.strip().lower() in {"y", "yes"}
        context["review_approved"] = approved

        if not approved:
            return StageResult(
                stage_name=self.name,
                status=StageStatus.FAILED,
                data={"approved": False},
                message="Review rejected by user. Pipeline halted.",
            )
        return StageResult(
            stage_name=self.name,
            status=StageStatus.SUCCESS,
            data={"approved": True},
            message="Review approved by user.",
        )


def _prompt_user(prompt: str) -> str:
    """Prompt the user for input, returning empty string if stdin is not a tty."""
    if sys.stdin.isatty():
        return input(prompt)
    return ""
