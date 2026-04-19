"""
CommitCandidateTool — 把 VersionManager.create_candidate 包装为 ReactAgent 工具

build/test/evaluate 都过后，LLM 调此工具把当前实现固化为一个候选版本。
触发 Git tag/branch（如果配置开启）+ 写入 candidate 列表，dashboard 候选 panel
展示，用户可在多个候选间比较 / 选择部署。
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Optional

from api.schemas import CodeChange
from tools.base import BaseTool, ToolParameter, ToolResult

logger = logging.getLogger(__name__)


class CommitCandidateTool(BaseTool):
    def __init__(self, version_manager=None):
        self._vm = version_manager

    @property
    def name(self) -> str:
        return "commit_candidate"

    @property
    def description(self) -> str:
        return (
            "Snapshot the current implementation as a candidate version. Call this AFTER "
            "build + (optional test) + evaluate all pass, BEFORE you respond to the user. "
            "Creates a version_id, optionally a Git tag/branch, and shows the candidate in "
            "the dashboard's candidates panel for the user to deploy/compare. "
            "Don't call this if anything is broken — only commit working candidates."
        )

    @property
    def parameters(self) -> list[ToolParameter]:
        return [
            ToolParameter("title", "string",
                          "Short title for this candidate (e.g., 'first working build')"),
            ToolParameter("summary", "string",
                          "What this candidate achieves and any caveats (3-5 sentences)",
                          required=False),
            ToolParameter("changed_files", "object",
                          "List of {file_path, action} dicts of files in this candidate",
                          required=False),
            ToolParameter("eval_score", "integer",
                          "Score from previous evaluate call (0-100), if you ran evaluate",
                          required=False),
            ToolParameter("project_path", "string",
                          "Project root (auto-injected by ReactAgent)",
                          required=False),
            ToolParameter("task_id", "string",
                          "Active task id (auto-injected by ReactAgent)",
                          required=False),
        ]

    @property
    def is_read_only(self) -> bool:
        return False  # 写 git tag / candidate 列表

    async def execute(
        self,
        title: str,
        summary: str = "",
        changed_files: Optional[list] = None,
        eval_score: Optional[int] = None,
        project_path: str = ".",
        task_id: str = "",
    ) -> ToolResult:
        if not self._vm:
            return ToolResult(success=False, error="VersionManager not wired (server.py)")
        if not task_id:
            return ToolResult(success=False, error="task_id required (auto-injection failed)")

        # 简易把 LLM 给的 file 列表转 CodeChange（不读内容，只记元数据）
        code_changes: list[CodeChange] = []
        for entry in (changed_files or []):
            if not isinstance(entry, dict):
                continue
            fp = entry.get("file_path") or ""
            if not fp:
                continue
            code_changes.append(CodeChange(
                file_path=fp,
                action=entry.get("action") or "modify",
                diff="",
                content=None,
            ))

        try:
            confidence = (eval_score or 0) / 100.0 if eval_score else 0.0
            candidate = await self._vm.create_candidate(
                task_id=task_id,
                code_changes=code_changes,
                test_results=None,
                eval_report=None,
                build_success=True,  # LLM 必须在 build 通过后才调本工具
                ai_confidence=confidence,
                ai_summary=summary or title,
            )
        except Exception as e:
            return ToolResult(success=False, error=f"create_candidate failed: {e}")

        version_id = getattr(candidate, "version_id", "") or (candidate.get("version_id", "") if isinstance(candidate, dict) else "")
        version_number = getattr(candidate, "version_number", "?") if not isinstance(candidate, dict) else candidate.get("version_number", "?")

        return ToolResult(
            success=True,
            output=(
                f"Candidate v{version_number} committed: {title}\n"
                f"version_id: {version_id}\n"
                f"summary: {summary[:200] if summary else '(none)'}\n"
                f"Visible in dashboard candidates panel."
            ),
            data={
                "version_id": version_id,
                "version_number": version_number,
                "title": title,
            },
        )
