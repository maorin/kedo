"""Base classes for pipeline stages."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, Optional


class StageStatus(Enum):
    """Execution status of a pipeline stage."""

    PENDING = "pending"
    RUNNING = "running"
    SUCCESS = "success"
    SKIPPED = "skipped"
    FAILED = "failed"


@dataclass
class StageResult:
    """Holds the output produced by a pipeline stage."""

    stage_name: str
    status: StageStatus
    data: Dict[str, Any] = field(default_factory=dict)
    message: str = ""

    # Convenience helpers --------------------------------------------------

    @property
    def succeeded(self) -> bool:
        return self.status == StageStatus.SUCCESS

    @property
    def failed(self) -> bool:
        return self.status == StageStatus.FAILED

    @property
    def skipped(self) -> bool:
        return self.status == StageStatus.SKIPPED


class Stage(ABC):
    """Abstract base class for all pipeline stages.

    Subclasses must implement :meth:`run`.  The optional :meth:`should_skip`
    hook lets a stage opt out of execution based on the accumulated *context*.
    """

    #: Human-readable name shown in the CLI progress display.
    name: str = ""

    def __init__(self, config: Optional[Dict[str, Any]] = None) -> None:
        self.config: Dict[str, Any] = config or {}

    def should_skip(self, context: Dict[str, Any]) -> bool:  # noqa: ARG002
        """Return *True* to skip this stage.  Default: never skip."""
        return False

    @abstractmethod
    def run(self, context: Dict[str, Any]) -> StageResult:
        """Execute the stage and return a :class:`StageResult`.

        Parameters
        ----------
        context:
            Mutable dictionary that accumulates results from earlier stages.
            Each stage is expected to add its own key(s) to *context* so that
            downstream stages can consume them.
        """
