"""Tests for the pipeline orchestrator."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from kedo.pipeline import Pipeline
from kedo.stages.base import Stage, StageResult, StageStatus


class _SuccessStage(Stage):
    name = "success"

    def run(self, context):
        context["success_ran"] = True
        return StageResult(stage_name=self.name, status=StageStatus.SUCCESS, message="ok")


class _FailStage(Stage):
    name = "fail"

    def run(self, context):
        return StageResult(stage_name=self.name, status=StageStatus.FAILED, message="boom")


class _SkippableStage(Stage):
    name = "skippable"

    def should_skip(self, context):
        return context.get("skip_me", False)

    def run(self, context):
        context["skippable_ran"] = True
        return StageResult(stage_name=self.name, status=StageStatus.SUCCESS)


class _RaisingStage(Stage):
    name = "raiser"

    def run(self, context):
        raise ValueError("unexpected error")


def test_pipeline_runs_all_stages():
    pipeline = Pipeline(stages=[_SuccessStage(), _SkippableStage()])
    ctx = pipeline.run("build a hello-world app")
    assert ctx["success_ran"] is True
    assert ctx["skippable_ran"] is True
    results = ctx["_results"]
    assert all(r.status == StageStatus.SUCCESS for r in results)


def test_pipeline_stops_on_failure():
    second = _SuccessStage()
    second.name = "second"
    pipeline = Pipeline(stages=[_FailStage(), second])
    ctx = pipeline.run("some requirement")
    results = ctx["_results"]
    # Only the failing stage result should be present; pipeline aborted
    assert len(results) == 1
    assert results[0].status == StageStatus.FAILED
    assert "success_ran" not in ctx


def test_pipeline_skips_stage():
    pipeline = Pipeline(stages=[_SkippableStage()])
    ctx = pipeline.run("req")
    ctx["skip_me"] = True  # set after run — just verify skip logic works
    # In the run above skip_me wasn't set, so the stage ran
    assert ctx.get("skippable_ran") is True


def test_pipeline_skip_respected():
    stage = _SkippableStage()
    pipeline = Pipeline(stages=[stage])
    # Inject skip_me into the context *before* run by pre-seeding through a
    # preceding stage that sets it.

    class _Setter(Stage):
        name = "setter"

        def run(self, c):
            c["skip_me"] = True
            return StageResult(stage_name=self.name, status=StageStatus.SUCCESS)

    pipeline2 = Pipeline(stages=[_Setter(), stage])
    ctx = pipeline2.run("req")
    results = ctx["_results"]
    assert results[1].status == StageStatus.SKIPPED
    assert "skippable_ran" not in ctx


def test_pipeline_catches_exceptions():
    pipeline = Pipeline(stages=[_RaisingStage()])
    ctx = pipeline.run("req")
    results = ctx["_results"]
    assert results[0].status == StageStatus.FAILED
    assert "unexpected error" in results[0].message


def test_pipeline_passes_requirement_in_context():
    pipeline = Pipeline(stages=[_SuccessStage()])
    ctx = pipeline.run("my requirement")
    assert ctx["requirement"] == "my requirement"
