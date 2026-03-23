"""Tests for configuration loading."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from kedo.config import load_config, _DEFAULTS


def test_load_config_defaults():
    config = load_config()
    for key, value in _DEFAULTS.items():
        assert config[key] == value


def test_load_config_from_file(tmp_path):
    config_file = tmp_path / "kedo.yaml"
    config_file.write_text("model: gpt-3.5-turbo\nauto_approve: true\n", encoding="utf-8")
    config = load_config(str(config_file))
    assert config["model"] == "gpt-3.5-turbo"
    assert config["auto_approve"] is True
    # Unspecified keys should still have defaults
    assert config["output_dir"] == _DEFAULTS["output_dir"]


def test_load_config_env_overrides(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "test-key-123")
    monkeypatch.setenv("OPENAI_BASE_URL", "https://custom.endpoint/v1")
    config = load_config()
    assert config["api_key"] == "test-key-123"
    assert config["base_url"] == "https://custom.endpoint/v1"


def test_load_config_missing_file():
    with pytest.raises(FileNotFoundError):
        # Pass a path that doesn't exist; load_config should propagate the error
        # from yaml.safe_load / open.
        load_config("/nonexistent/path/kedo.yaml")
