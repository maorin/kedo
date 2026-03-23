"""Tests for the individual pipeline stages."""

from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import pytest

from kedo.llm.client import LLMMessage, LLMResponse
from kedo.stages.analysis import AnalysisStage
from kedo.stages.base import StageStatus
from kedo.stages.codegen import CodegenStage
from kedo.stages.decomposition import DecompositionStage
from kedo.stages.deployment import DeploymentStage
from kedo.stages.evaluation import EvaluationStage
from kedo.stages.review import ReviewStage
from kedo.stages.testing import TestingStage


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_llm_response(payload) -> LLMResponse:
    """Return a mock LLMResponse whose *content* is the JSON-serialised payload."""
    return LLMResponse(
        content=json.dumps(payload, ensure_ascii=False),
        model="gpt-4o",
        usage={},
    )


def _patch_llm(stage, payload):
    """Patch the stage's internal LLM client to return *payload*."""
    stage._llm = MagicMock()
    stage._llm.chat.return_value = _make_llm_response(payload)


# ---------------------------------------------------------------------------
# AnalysisStage
# ---------------------------------------------------------------------------


def test_analysis_stage_success():
    stage = AnalysisStage()
    analysis_payload = {
        "background": "Build a to-do API.",
        "goals": ["CRUD operations"],
        "scope": ["REST endpoints"],
        "constraints": [],
        "acceptance_criteria": ["All endpoints return JSON"],
    }
    _patch_llm(stage, analysis_payload)
    ctx = {"requirement": "Build a to-do list REST API"}
    result = stage.run(ctx)
    assert result.status == StageStatus.SUCCESS
    assert ctx["analysis"] == analysis_payload


def test_analysis_stage_no_requirement():
    stage = AnalysisStage()
    result = stage.run({})
    assert result.status == StageStatus.FAILED


def test_analysis_stage_handles_invalid_json():
    stage = AnalysisStage()
    stage._llm = MagicMock()
    stage._llm.chat.return_value = LLMResponse(
        content="not valid json", model="gpt-4o", usage={}
    )
    ctx = {"requirement": "something"}
    result = stage.run(ctx)
    assert result.status == StageStatus.SUCCESS
    assert "raw" in ctx["analysis"]


# ---------------------------------------------------------------------------
# DecompositionStage
# ---------------------------------------------------------------------------


def test_decomposition_stage_success():
    stage = DecompositionStage()
    tasks_payload = [
        {"id": 1, "title": "Set up project", "description": "Init repo", "effort": "small", "dependencies": []},
        {"id": 2, "title": "Implement API", "description": "Build endpoints", "effort": "large", "dependencies": [1]},
    ]
    _patch_llm(stage, tasks_payload)
    ctx = {"analysis": {"goals": ["Build API"]}}
    result = stage.run(ctx)
    assert result.status == StageStatus.SUCCESS
    assert len(ctx["tasks"]) == 2


def test_decomposition_skips_without_analysis():
    stage = DecompositionStage()
    assert stage.should_skip({}) is True
    assert stage.should_skip({"analysis": {}}) is False


# ---------------------------------------------------------------------------
# CodegenStage
# ---------------------------------------------------------------------------


def test_codegen_stage_success():
    stage = CodegenStage()
    artifact_payload = {
        "file_path": "app.py",
        "language": "python",
        "code": "print('hello')",
        "notes": "Simple entry point",
    }
    _patch_llm(stage, artifact_payload)
    ctx = {
        "tasks": [{"id": 1, "title": "T1", "description": "D1"}],
        "analysis": {},
    }
    result = stage.run(ctx)
    assert result.status == StageStatus.SUCCESS
    assert ctx["generated_code"][0]["file_path"] == "app.py"
    assert ctx["generated_code"][0]["task_id"] == 1


def test_codegen_skips_without_tasks():
    stage = CodegenStage()
    assert stage.should_skip({}) is True


# ---------------------------------------------------------------------------
# TestingStage
# ---------------------------------------------------------------------------


def test_testing_stage_success():
    stage = TestingStage()
    test_payload = {
        "test_file_path": "test_app.py",
        "language": "python",
        "test_code": "def test_hello(): pass",
        "test_cases": ["test_hello"],
    }
    _patch_llm(stage, test_payload)
    ctx = {
        "generated_code": [{"file_path": "app.py", "code": "print('hi')", "task_id": 1}]
    }
    result = stage.run(ctx)
    assert result.status == StageStatus.SUCCESS
    assert ctx["test_artifacts"][0]["test_file_path"] == "test_app.py"


def test_testing_skips_without_generated_code():
    stage = TestingStage()
    assert stage.should_skip({}) is True


# ---------------------------------------------------------------------------
# EvaluationStage
# ---------------------------------------------------------------------------


def test_evaluation_stage_success():
    stage = EvaluationStage()
    eval_payload = {
        "score": 85,
        "coverage_score": 90,
        "code_quality_score": 80,
        "test_quality_score": 85,
        "strengths": ["Good structure"],
        "weaknesses": [],
        "suggestions": [],
        "ready_for_review": True,
    }
    _patch_llm(stage, eval_payload)
    ctx = {"generated_code": [{"file_path": "app.py", "code": "print('hi')"}], "test_artifacts": []}
    result = stage.run(ctx)
    assert result.status == StageStatus.SUCCESS
    assert ctx["evaluation"]["score"] == 85
    assert "85/100" in result.message


def test_evaluation_skips_without_generated_code():
    stage = EvaluationStage()
    assert stage.should_skip({}) is True


# ---------------------------------------------------------------------------
# ReviewStage
# ---------------------------------------------------------------------------


def test_review_auto_approve():
    stage = ReviewStage({"auto_approve": True})
    ctx = {"evaluation": {"ready_for_review": True, "score": 90, "weaknesses": []}}
    result = stage.run(ctx)
    assert result.status == StageStatus.SUCCESS
    assert ctx["review_approved"] is True


def test_review_not_ready_skips():
    stage = ReviewStage()
    assert stage.should_skip({"evaluation": {"ready_for_review": False}}) is True
    assert stage.should_skip({"evaluation": {"ready_for_review": True}}) is False


def test_review_interactive_rejected(monkeypatch):
    stage = ReviewStage()
    monkeypatch.setattr("kedo.stages.review._prompt_user", lambda _: "n")
    ctx = {
        "evaluation": {"ready_for_review": True, "score": 50, "weaknesses": ["poor tests"]},
        "generated_code": [],
    }
    result = stage.run(ctx)
    assert result.status == StageStatus.FAILED
    assert ctx["review_approved"] is False


def test_review_interactive_approved(monkeypatch):
    stage = ReviewStage()
    monkeypatch.setattr("kedo.stages.review._prompt_user", lambda _: "y")
    ctx = {
        "evaluation": {"ready_for_review": True, "score": 95, "weaknesses": []},
        "generated_code": [],
    }
    result = stage.run(ctx)
    assert result.status == StageStatus.SUCCESS
    assert ctx["review_approved"] is True


# ---------------------------------------------------------------------------
# DeploymentStage
# ---------------------------------------------------------------------------


def test_deployment_writes_files(tmp_path):
    stage = DeploymentStage({"output_dir": str(tmp_path), "generate_manifest": False})
    ctx = {
        "generated_code": [{"file_path": "src/app.py", "code": "print('hello')", "task_id": 1}],
        "test_artifacts": [{"test_file_path": "tests/test_app.py", "test_code": "def test_x(): pass", "task_id": 1}],
    }
    result = stage.run(ctx)
    assert result.status == StageStatus.SUCCESS
    assert (tmp_path / "src" / "app.py").read_text() == "print('hello')"
    assert (tmp_path / "tests" / "test_app.py").read_text() == "def test_x(): pass"


def test_deployment_generates_manifest(tmp_path):
    stage = DeploymentStage({"output_dir": str(tmp_path), "generate_manifest": True})
    manifest_payload = {"file_path": "Dockerfile", "content": "FROM python:3.11\n", "notes": "build it"}
    _patch_llm(stage, manifest_payload)
    ctx = {
        "generated_code": [{"file_path": "app.py", "code": "print('hi')", "task_id": 1}],
        "test_artifacts": [],
    }
    result = stage.run(ctx)
    assert result.status == StageStatus.SUCCESS
    assert (tmp_path / "Dockerfile").read_text() == "FROM python:3.11\n"


def test_deployment_skips_when_review_rejected():
    stage = DeploymentStage()
    assert stage.should_skip({"review_approved": False}) is True
    assert stage.should_skip({"review_approved": True}) is False
    assert stage.should_skip({}) is False
