"""Pipeline orchestrator.

The :class:`Pipeline` executes a list of :class:`~kedo.stages.base.Stage`
objects in order, passing a shared *context* dictionary between them.  It
uses the ``rich`` library to display live progress in the terminal.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from kedo.stages.base import Stage, StageResult, StageStatus


class Pipeline:
    """Orchestrates the full development pipeline.

    Parameters
    ----------
    stages:
        Ordered list of :class:`Stage` instances to execute.
    console:
        Optional *rich* ``Console`` for pretty output.  When *None* the
        pipeline falls back to plain ``print`` statements.
    """

    def __init__(
        self,
        stages: List[Stage],
        console: Optional[Any] = None,
    ) -> None:
        self.stages = stages
        self._console = console

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def run(self, requirement: str) -> Dict[str, Any]:
        """Run the full pipeline for *requirement*.

        Returns the accumulated *context* dictionary which contains the
        outputs of every stage.
        """
        context: Dict[str, Any] = {"requirement": requirement}
        results: List[StageResult] = []

        self._print_header(requirement)

        for stage in self.stages:
            if stage.should_skip(context):
                result = StageResult(
                    stage_name=stage.name,
                    status=StageStatus.SKIPPED,
                    message="Stage skipped.",
                )
                results.append(result)
                self._print_result(result)
                continue

            self._print_running(stage.name)
            try:
                result = stage.run(context)
            except (KeyboardInterrupt, SystemExit):
                raise
            except Exception as exc:  # noqa: BLE001
                result = StageResult(
                    stage_name=stage.name,
                    status=StageStatus.FAILED,
                    message=f"Unhandled exception: {exc}",
                )

            results.append(result)
            self._print_result(result)

            if result.failed:
                self._print_aborted(stage.name)
                break

        context["_results"] = results
        self._print_summary(results)
        return context

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _print(self, text: str, style: str = "") -> None:
        if self._console is not None:
            self._console.print(text, style=style)
        else:
            print(text)

    def _print_header(self, requirement: str) -> None:
        self._print(f"\n🚀  kedo pipeline starting…")
        self._print(f"    Requirement: {requirement[:120]}")

    def _print_running(self, name: str) -> None:
        self._print(f"\n⏳  [{name}] running…")

    def _print_result(self, result: StageResult) -> None:
        icons = {
            StageStatus.SUCCESS: "✅",
            StageStatus.SKIPPED: "⏭️ ",
            StageStatus.FAILED: "❌",
        }
        icon = icons.get(result.status, "ℹ️ ")
        self._print(f"{icon}  [{result.stage_name}] {result.message}")

    def _print_aborted(self, name: str) -> None:
        self._print(f"\n🛑  Pipeline aborted at stage '{name}'.")

    def _print_summary(self, results: List[StageResult]) -> None:
        self._print(f"\n{'─' * 50}")
        self._print("📋  Pipeline summary:")
        for r in results:
            self._print(f"    {r.stage_name:16s}  {r.status.value}")
        self._print(f"{'─' * 50}\n")
