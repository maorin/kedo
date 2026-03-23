"""部署上线 — Deployment stage.

Writes every generated code artifact to disk (under *output_dir*) and
optionally generates a deployment manifest (e.g., a ``Dockerfile`` or a
shell deploy script) via the LLM.

Configuration keys (all optional):
  output_dir   – directory to write files into (default: ``"output"``)
  generate_manifest – bool, whether to ask the LLM for a deploy script
                       (default: True)
  model / api_key / base_url – passed through to :class:`LLMClient`
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Dict, List

from kedo.llm.client import LLMClient, LLMMessage
from kedo.stages.base import Stage, StageResult, StageStatus

_MANIFEST_PROMPT = """\
You are a DevOps engineer.  Given the generated code artifacts (JSON list),
produce a minimal but complete deployment manifest.

Return ONLY a valid JSON object (no markdown fences) with:
  "file_path"  – relative path for the manifest file (e.g. "Dockerfile",
                 "deploy.sh", "docker-compose.yml")
  "content"    – full text content of the manifest (string)
  "notes"      – brief deployment instructions (string)
"""


class DeploymentStage(Stage):
    """Writes artifacts to disk and produces a deployment manifest."""

    name = "部署上线"

    def __init__(self, config: Dict[str, Any] | None = None) -> None:
        super().__init__(config)
        self._llm = LLMClient(
            model=self.config.get("model", "gpt-4o"),
            api_key=self.config.get("api_key"),
            base_url=self.config.get("base_url"),
        )

    def should_skip(self, context: Dict[str, Any]) -> bool:
        # Only deploy when the review was explicitly approved (or not present)
        return context.get("review_approved") is False

    def run(self, context: Dict[str, Any]) -> StageResult:
        output_dir = Path(self.config.get("output_dir", "output"))
        artifacts: List[Dict[str, Any]] = context.get("generated_code", [])
        test_artifacts: List[Dict[str, Any]] = context.get("test_artifacts", [])
        written_files: List[str] = []

        # Write source files ---------------------------------------------------
        for artifact in artifacts:
            file_path = artifact.get("file_path")
            code = artifact.get("code", "")
            if file_path and code:
                dest = output_dir / file_path
                dest.parent.mkdir(parents=True, exist_ok=True)
                dest.write_text(code, encoding="utf-8")
                written_files.append(str(dest))

        # Write test files -----------------------------------------------------
        for test_artifact in test_artifacts:
            file_path = test_artifact.get("test_file_path")
            code = test_artifact.get("test_code", "")
            if file_path and code:
                dest = output_dir / file_path
                dest.parent.mkdir(parents=True, exist_ok=True)
                dest.write_text(code, encoding="utf-8")
                written_files.append(str(dest))

        # Generate deployment manifest -----------------------------------------
        manifest_info: Dict[str, Any] = {}
        if self.config.get("generate_manifest", True) and artifacts:
            messages = [
                LLMMessage(role="system", content=_MANIFEST_PROMPT),
                LLMMessage(
                    role="user",
                    content=f"Artifacts:\n{json.dumps(artifacts, ensure_ascii=False, indent=2)}",
                ),
            ]
            response = self._llm.chat(messages)
            try:
                manifest_info = json.loads(response.content)
            except json.JSONDecodeError:
                manifest_info = {"file_path": "deploy.sh", "content": response.content}

            manifest_path = manifest_info.get("file_path")
            manifest_content = manifest_info.get("content", "")
            if manifest_path and manifest_content:
                dest = output_dir / manifest_path
                dest.parent.mkdir(parents=True, exist_ok=True)
                dest.write_text(manifest_content, encoding="utf-8")
                written_files.append(str(dest))

        context["deployment"] = {
            "output_dir": str(output_dir),
            "written_files": written_files,
            "manifest": manifest_info,
        }

        return StageResult(
            stage_name=self.name,
            status=StageStatus.SUCCESS,
            data={"written_files": written_files, "manifest": manifest_info},
            message=(
                f"Deployed {len(written_files)} file(s) to '{output_dir}'. "
                + (f"Manifest: {manifest_info.get('file_path', '')}" if manifest_info else "")
            ),
        )
