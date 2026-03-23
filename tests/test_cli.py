"""Tests for the CLI entry point."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
from click.testing import CliRunner

from kedo.cli import main
from kedo.stages.base import StageResult, StageStatus


def _make_context(failed=False):
    status = StageStatus.FAILED if failed else StageStatus.SUCCESS
    return {
        "requirement": "test req",
        "_results": [StageResult(stage_name="s", status=status, message="m")],
    }


def test_cli_version():
    runner = CliRunner()
    result = runner.invoke(main, ["--version"])
    assert result.exit_code == 0
    assert "0.1.0" in result.output


def test_cli_run_success():
    runner = CliRunner()
    with patch("kedo.cli._build_pipeline") as mock_build:
        mock_pipeline = MagicMock()
        mock_pipeline.run.return_value = _make_context(failed=False)
        mock_build.return_value = mock_pipeline
        result = runner.invoke(main, ["run", "build a hello world app"])
    assert result.exit_code == 0


def test_cli_run_failure_exits_nonzero():
    runner = CliRunner()
    with patch("kedo.cli._build_pipeline") as mock_build:
        mock_pipeline = MagicMock()
        mock_pipeline.run.return_value = _make_context(failed=True)
        mock_build.return_value = mock_pipeline
        result = runner.invoke(main, ["run", "build something"])
    assert result.exit_code != 0


def test_cli_passes_flags_to_config():
    runner = CliRunner()
    captured_config = {}

    def fake_build(config):
        captured_config.update(config)
        mock_pipeline = MagicMock()
        mock_pipeline.run.return_value = _make_context()
        return mock_pipeline

    with patch("kedo.cli._build_pipeline", side_effect=fake_build):
        runner.invoke(
            main,
            ["run", "--auto-approve", "--no-manifest", "--model", "gpt-3.5-turbo",
             "--output-dir", "/tmp/out", "req"],
        )
    assert captured_config.get("auto_approve") is True
    assert captured_config.get("generate_manifest") is False
    assert captured_config.get("model") == "gpt-3.5-turbo"
    assert captured_config.get("output_dir") == "/tmp/out"
