"""
CommitCandidateTool — 固化当前实现为一个候选版本

方案 C 后行为：
- 若 Reviewer 激活 且 pre_commit_gate=True：create_candidate 之前强制过 Reviewer 一道
  判决拒绝则直接返回失败，Producer 必须重做（Self-eval drift 的最后一道物理闸）
- 否则：跳过 Reviewer，直接写入候选版本（原单 Agent 行为）

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
from tools.evaluate_tool import _normalize_changed_files

logger = logging.getLogger(__name__)


class CommitCandidateTool(BaseTool):
    def __init__(
        self,
        version_manager=None,
        reviewer=None,
        pre_commit_gate: bool = True,
        reject_tracker=None,
    ):
        self._vm = version_manager
        self._reviewer = reviewer          # 方案 C：独立 Reviewer Agent（可为 None）
        self._pre_commit_gate = pre_commit_gate
        # 跨工具/Agent 共享的 reject 计数器，触发 respond 屏蔽护栏（可为 None）
        self._reject_tracker = reject_tracker

    @property
    def name(self) -> str:
        return "commit_candidate"

    @property
    def description(self) -> str:
        reviewer_note = ""
        if self._reviewer is not None and self._reviewer.is_active and self._pre_commit_gate:
            reviewer_note = (
                f" **Pre-commit gate active**: an independent Reviewer Agent "
                f"(provider={self._reviewer.provider_name}) will score the code BEFORE "
                f"the candidate is created. If the Reviewer rejects (score < "
                f"{self._reviewer.min_score}), this tool returns failure and you must "
                f"iterate — do NOT retry with the same code. Pass `requirement` so the "
                f"Reviewer knows what to score against."
            )
        return (
            "Snapshot the current implementation as a candidate version. Call this AFTER "
            "build + (optional test) + evaluate all pass, BEFORE you respond to the user. "
            "Creates a version_id, optionally a Git tag/branch, and shows the candidate in "
            "the dashboard's candidates panel for the user to deploy/compare. "
            "Don't call this if anything is broken — only commit working candidates."
            + reviewer_note
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
            ToolParameter("requirement", "string",
                          "The user's original requirement — Reviewer scores `requirement_match` against this. "
                          "Pass the same text you passed to `evaluate`.",
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
        requirement: str = "",
        project_path: str = ".",
        task_id: str = "",
    ) -> ToolResult:
        if not self._vm:
            return ToolResult(success=False, error="VersionManager not wired (server.py)")
        if not task_id:
            return ToolResult(success=False, error="task_id required (auto-injection failed)")

        # 简易把 LLM 给的 file 列表转 CodeChange（候选版本本身只记元数据）
        # 归一化兼容 dict / list-of-str / 规范 list-of-dict 多种 LLM 传参格式。
        code_changes: list[CodeChange] = [
            CodeChange(
                file_path=e["file_path"],
                action=e["action"],
                diff="",
                content=None,
            )
            for e in _normalize_changed_files(changed_files)
        ]

        # --- Pre-commit Reviewer 闸 ---
        # 方案 C 的关键动作：把"谁写代码谁打分"物理拆开。Producer 只能尝试 commit，
        # Reviewer（独立 LLM）说 approve 才能写入。拒绝则返回带意见的失败，Producer 必须迭代。
        reviewer_meta: dict = {}
        if (
            self._reviewer is not None
            and self._reviewer.is_active
            and self._pre_commit_gate
        ):
            review_changes = self._load_changes_with_content(changed_files, project_path)
            effective_requirement = requirement or summary or title
            try:
                review = await self._reviewer.review(
                    requirement=effective_requirement,
                    changed_files=review_changes,
                    project_path=project_path,
                    stage="pre_commit",
                )
            except Exception as e:
                logger.warning(f"Reviewer pre-commit call failed: {e}")
                return ToolResult(
                    success=False,
                    error=(
                        f"Pre-commit Reviewer 调用失败: {e}. "
                        "修复后再调 commit_candidate，或先用 pause_for_human 求助。"
                    ),
                )

            reviewer_meta = {
                "reviewer_review_id": review.review_id,
                "reviewer_score": review.score,
                "reviewer_model": review.reviewer_model,
                "reviewer_provider": self._reviewer.provider_name,
            }

            if not review.approve:
                miss_preview = "; ".join(review.requirements_missed[:3]) or "(none)"
                risk_preview = "; ".join(review.risks[:3]) or "(none)"
                sugg_preview = "; ".join(review.suggestions[:3]) or "(none)"

                # 累计 reject + 触发升级建议
                reject_n = 0
                threshold = 0
                escalation_required = False
                if self._reject_tracker is not None:
                    reject_n = self._reject_tracker.on_reject(task_id)
                    threshold = self._reject_tracker.escalation_threshold
                    escalation_required = reject_n >= threshold

                if escalation_required:
                    # 软提示：到阈值，明确告诉 LLM 不要再走 respond 逃生
                    error_msg = (
                        f"Reviewer REJECTED ({reject_n}/{threshold} consecutive rejects, "
                        f"escalation required). Score {review.score:.1f} < "
                        f"{self._reviewer.min_score}. Review {review.review_id}.\n"
                        f"⚠ DO NOT call `respond` to declare task done — Reviewer's findings "
                        f"are unresolved and respond is now BLOCKED until you escalate.\n"
                        f"You MUST call one of:\n"
                        f"  - `pause_for_human` — ask the user for guidance / new approach\n"
                        f"  - `propose_alternatives` — offer the user 2-3 concrete paths to choose from"
                    )
                    extra_output = (
                        f"\n\n⚠ ESCALATION REQUIRED ({reject_n}/{threshold} consecutive rejects)\n"
                        f"`respond` is BLOCKED until you call `pause_for_human` or "
                        f"`propose_alternatives`. Don't try to declare success — "
                        f"the Reviewer's findings above must be acknowledged to the user."
                    )
                else:
                    error_msg = (
                        f"Reviewer REJECTED this candidate (score {review.score:.1f} < "
                        f"{self._reviewer.min_score}). Do NOT retry commit with the same code — "
                        f"iterate first. Review {review.review_id}. "
                        f"({reject_n}/{threshold} rejects so far)" if threshold else
                        f"Reviewer REJECTED this candidate (score {review.score:.1f} < "
                        f"{self._reviewer.min_score}). Do NOT retry commit with the same code — "
                        f"iterate first. Review {review.review_id}."
                    )
                    extra_output = ""

                return ToolResult(
                    success=False,
                    error=error_msg,
                    output=(
                        f"Reviewer[{self._reviewer.provider_name}/{review.reviewer_model}] "
                        f"rejected pre-commit gate.\n"
                        f"score: {review.score:.1f}/100  stage: {review.stage}\n"
                        f"missed: {miss_preview}\n"
                        f"risks: {risk_preview}\n"
                        f"suggestions: {sugg_preview}\n"
                        f"Next: address the findings above, re-run build/test, then try "
                        f"commit_candidate again. If you cannot resolve, use "
                        f"propose_alternatives or pause_for_human."
                        f"{extra_output}"
                    ),
                    data={
                        **reviewer_meta,
                        "approved": False,
                        "missed": review.requirements_missed,
                        "risks": review.risks,
                        "suggestions": review.suggestions,
                        "reject_count": reject_n,
                        "escalation_threshold": threshold,
                        "escalation_required": escalation_required,
                    },
                )

            # approve → 清掉这个 task 的 reject 累计
            if self._reject_tracker is not None:
                self._reject_tracker.on_approve(task_id)

        # --- Reviewer 通过（或未激活） → 实际创建候选版本 ---
        try:
            # 方案 C 后：ai_confidence 优先用 reviewer 的独立打分；没有 reviewer 时用 Producer 自评
            if reviewer_meta.get("reviewer_score") is not None:
                confidence = float(reviewer_meta["reviewer_score"]) / 100.0
            elif eval_score:
                confidence = eval_score / 100.0
            else:
                confidence = 0.0
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

        reviewer_line = ""
        if reviewer_meta:
            reviewer_line = (
                f"\nReviewer approval: {reviewer_meta['reviewer_score']:.1f}/100 "
                f"({reviewer_meta['reviewer_provider']}/{reviewer_meta['reviewer_model']}) "
                f"[{reviewer_meta['reviewer_review_id']}]"
            )

        return ToolResult(
            success=True,
            output=(
                f"Candidate v{version_number} committed: {title}\n"
                f"version_id: {version_id}\n"
                f"summary: {summary[:200] if summary else '(none)'}"
                f"{reviewer_line}\n"
                f"Visible in dashboard candidates panel."
            ),
            data={
                "version_id": version_id,
                "version_number": version_number,
                "title": title,
                **reviewer_meta,
            },
        )

    @staticmethod
    def _load_changes_with_content(
        entries, project_path: str
    ) -> list[CodeChange]:
        """Pre-commit review 需要文件实际内容；从磁盘补齐 content。接受任意 LLM 传参格式。"""
        out: list[CodeChange] = []
        for entry in _normalize_changed_files(entries):
            fp = entry["file_path"]
            action = entry["action"]
            content = ""
            try:
                p = Path(fp)
                if not p.is_absolute():
                    p = Path(project_path) / fp
                if p.is_file():
                    content = p.read_text(encoding="utf-8", errors="replace")
            except Exception as e:
                logger.debug(f"CommitCandidateTool: failed to read {fp}: {e}")
            out.append(CodeChange(
                file_path=fp, action=action, diff="", content=content,
            ))
        return out
