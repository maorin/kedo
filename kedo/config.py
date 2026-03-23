"""Configuration loader.

kedo looks for a configuration file in the following order:
  1. Path passed explicitly (``--config`` CLI flag).
  2. ``kedo.yaml`` / ``kedo.yml`` in the current working directory.
  3. ``~/.kedo/config.yaml``.

If no file is found, built-in defaults are used.

Example ``kedo.yaml``::

    model: gpt-4o
    api_key: sk-...          # or set OPENAI_API_KEY env var
    base_url: ~              # optional custom base URL
    output_dir: output
    auto_approve: false
    generate_manifest: true
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Dict, Optional

try:
    import yaml  # type: ignore
    _HAS_YAML = True
except ImportError:  # pragma: no cover
    _HAS_YAML = False

_DEFAULTS: Dict[str, Any] = {
    "model": "gpt-4o",
    "api_key": None,
    "base_url": None,
    "output_dir": "output",
    "auto_approve": False,
    "generate_manifest": True,
}

_SEARCH_PATHS = [
    Path("kedo.yaml"),
    Path("kedo.yml"),
    Path.home() / ".kedo" / "config.yaml",
]


def load_config(path: Optional[str] = None) -> Dict[str, Any]:
    """Load and return the merged configuration dictionary.

    Environment variables ``OPENAI_API_KEY`` and ``OPENAI_BASE_URL`` are
    applied on top of the file values (if set).
    """
    config = dict(_DEFAULTS)

    # Determine which file to read
    file_path: Optional[Path] = None
    if path:
        file_path = Path(path)
    else:
        for candidate in _SEARCH_PATHS:
            if candidate.exists():
                file_path = candidate
                break

    if file_path:
        if not file_path.exists():
            if path:
                # The caller explicitly named a file — treat a missing file as an error.
                raise FileNotFoundError(f"kedo config file not found: {file_path}")
            # Auto-discovered candidate that has since disappeared; skip it.
            file_path = None

    if file_path:
        if not _HAS_YAML:  # pragma: no cover
            raise RuntimeError(
                "PyYAML is required to load a config file. "
                "Install it with: pip install pyyaml"
            )
        with open(file_path, encoding="utf-8") as fh:
            file_config = yaml.safe_load(fh) or {}
        config.update(file_config)

    # Environment-variable overrides
    if os.environ.get("OPENAI_API_KEY"):
        config["api_key"] = os.environ["OPENAI_API_KEY"]
    if os.environ.get("OPENAI_BASE_URL"):
        config["base_url"] = os.environ["OPENAI_BASE_URL"]

    return config
