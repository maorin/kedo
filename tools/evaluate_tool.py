"""
EvaluateTool — 多维度代码质量评分工具

方案 C 后行为：
- 若 Reviewer Agent 激活（reviewer_provider != none）：调 reviewer.review()，独立 LLM 打分
- 否则：调原 core.evaluator.Evaluator，用 Producer 同一 LLM 自评（单 Agent 行为）

LLM 在 build/test 都通过、准备 respond 之前可主动调，拿到 4 维度评分（需求匹配/代码质量/
测试覆盖/安全）。低分时可结合 propose_alternatives 让用户决定是否迭代。
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Optional

from api.schemas import CodeChange
from tools.base import BaseTool, ToolParameter, ToolResult

logger = logging.getLogger(__name__)


def _normalize_changed_files(entries) -> list[dict]:
    """把 LLM 传入的 changed_files 归一化成 [{file_path, action}]。

    实测 LLM 会用多种不等价格式传参（即便 description 里写了 list 也常传 dict）：
      1. [{"file_path": "a.c", "action": "modify"}]  ← 规范
      2. ["a.c", "b.c"]                              ← 纯字符串列表
      3. {"a.c": "modify", "b.c": "create"}          ← dict，value 是 action
      4. {"a.c": "主程序", "b.c": "NFS 客户端"}       ← dict，value 是描述（最常见踩坑）
      5. {"files": [...]}                            ← 包一层
      6. None / 空                                   ← 跳过

    归一化策略：
      - 1 原样通过
      - 2 每项当路径，action 默认 "modify"
      - 3/4 dict 视为 {path: 任意}，路径是 key，value 当 action 或描述（LLM 可能都会出）
          若 value 形如 "modify"/"create"/"delete" 则作 action，否则默认 "modify"
      - 5 展开 "files" 键
      - 其他退化场景在 logger.warning 里抛出，调用方可据此通知 LLM 改格式

    一个 action 白名单用于 3/4 判定。
    """
    ACTION_WHITELIST = {"create", "modify", "delete", "update", "new", "add"}

    if not entries:
        return []

    # 情况 5：{"files": [...]}
    if isinstance(entries, dict) and len(entries) == 1 and "files" in entries:
        entries = entries["files"]

    def _to_entry(fp: str, action_hint: str = "") -> dict:
        fp = (fp or "").strip()
        if not fp:
            return {}
        raw = (action_hint or "").strip().lower()
        action = raw if raw in ACTION_WHITELIST else "modify"
        return {"file_path": fp, "action": action}

    normalized: list[dict] = []

    if isinstance(entries, list):
        for item in entries:
            if isinstance(item, dict):
                fp = item.get("file_path") or item.get("path") or item.get("file") or ""
                action = item.get("action") or ""
                e = _to_entry(fp, action)
                if e:
                    normalized.append(e)
            elif isinstance(item, str):
                e = _to_entry(item)
                if e:
                    normalized.append(e)
            else:
                logger.warning(f"changed_files: unsupported list entry type {type(item).__name__}, skip")
    elif isinstance(entries, dict):
        for k, v in entries.items():
            # 如果 value 本身是完整 dict（形如 {"action": ...}），把它 merge
            if isinstance(v, dict):
                fp = v.get("file_path") or k
                action = v.get("action") or ""
                e = _to_entry(fp, action)
            else:
                # v 可能是 action 字符串（"modify"）也可能是描述（"主程序"），
                # 用白名单兜底：不在白名单就默认 "modify"
                e = _to_entry(k, str(v) if v else "")
            if e:
                normalized.append(e)
        if normalized:
            logger.warning(
                f"changed_files: LLM 传的是 dict 格式（{len(normalized)} 项），"
                f"已兼容归一化为 list；规范格式是 list[{{file_path, action}}]。"
            )
    else:
        logger.warning(
            f"changed_files: 不支持的类型 {type(entries).__name__}，跳过全部条目"
        )

    return normalized


def _load_code_changes(
    entries, project_path: str
) -> list[CodeChange]:
    """从 LLM 提供的 changed_files（任意合理格式）构建 CodeChange，并附上磁盘内容。"""
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
            logger.debug(f"EvaluateTool: failed to read {fp}: {e}")
        out.append(CodeChange(
            file_path=fp, action=action, diff="", content=content,
        ))
    return out


class EvaluateTool(BaseTool):
    def __init__(self, evaluator=None, reviewer=None, min_score: int = 70):
        self._evaluator = evaluator
        self._reviewer = reviewer  # 方案 C：独立 Reviewer Agent（可为 None）
        self._min_score = min_score

    @property
    def name(self) -> str:
        return "evaluate"

    @property
    def description(self) -> str:
        reviewer_note = ""
        if self._reviewer is not None and self._reviewer.is_active:
            reviewer_note = (
                f" An independent Reviewer Agent (provider={self._reviewer.provider_name}, "
                f"model={self._reviewer.model_name}) scores the code — "
                f"not the LLM that wrote it, so the score cannot be inflated by self-bias."
            )
        return (
            "Evaluate the current code changes across 4 dimensions: requirement_match (0.35), "
            "code_quality (0.25), test_coverage (0.20), security (0.20). Returns a weighted "
            "score 0-100 plus per-dimension details, requirements_met / missed, risks, "
            "suggestions. Call this AFTER build succeeds and BEFORE you respond to the user, "
            "so you can decide whether the implementation is good enough or needs another "
            "iteration. Score < 70 = consider propose_alternatives or another fix round."
            + reviewer_note
        )

    @property
    def parameters(self) -> list[ToolParameter]:
        return [
            ToolParameter("requirement", "string",
                          "The user requirement to evaluate against (the original task description)"),
            ToolParameter("changed_files", "object",
                          "List of {file_path, action} dicts describing what files you changed/created. "
                          "action is 'create' or 'modify'.",
                          required=False),
            ToolParameter("project_path", "string",
                          "Project root (auto-injected by ReactAgent)",
                          required=False),
        ]

    @property
    def is_read_only(self) -> bool:
        return True  # 只读评估，不写文件

    async def execute(
        self,
        requirement: str,
        changed_files: Optional[list] = None,
        project_path: str = ".",
    ) -> ToolResult:
        code_changes = _load_code_changes(changed_files, project_path)

        # 路径 1：Reviewer Agent 激活 —— 独立 LLM 打分（方案 C 的主路径）
        if self._reviewer is not None and self._reviewer.is_active:
            return await self._run_with_reviewer(requirement, code_changes, project_path)

        # 路径 2：回退 —— 用 Producer 同 LLM 的 Evaluator（单 Agent 行为，Self-eval drift 在）
        if not self._evaluator:
            return ToolResult(success=False, error="Evaluator not wired (server.py)")
        return await self._run_with_evaluator(requirement, code_changes, project_path)

    # --------------------------------------------------------
    # Reviewer Agent 路径（方案 C）
    # --------------------------------------------------------

    async def _run_with_reviewer(
        self, requirement: str, code_changes: list[CodeChange], project_path: str
    ) -> ToolResult:
        try:
            result = await self._reviewer.review(
                requirement=requirement,
                changed_files=code_changes,
                project_path=project_path,
                stage="adhoc",
            )
        except Exception as e:
            return ToolResult(success=False, error=f"reviewer.review failed: {e}")

        dim_lines = [
            f"  - {d['name']}: {d['score']:.0f} — {(d.get('details') or '')[:120]}"
            for d in (result.dimensions or [])
        ]
        missed = result.requirements_missed or []
        risks = result.risks or []
        suggestions = result.suggestions or []

        output = (
            f"Reviewer[{self._reviewer.provider_name}/{self._reviewer.model_name}] "
            f"score: {result.score:.1f}/100 "
            f"({'PASS' if result.approve else 'REJECT'} min={self._reviewer.min_score})\n"
            f"review_id: {result.review_id}  stage: {result.stage}\n\n"
            f"Dimensions:\n" + ("\n".join(dim_lines) if dim_lines else "  (none)") + "\n\n"
            f"Requirements missed ({len(missed)}):\n"
            + ("\n".join(f"  - {m}" for m in missed[:5]) if missed else "  (none)")
            + "\n\nRisks:\n"
            + ("\n".join(f"  - {r}" for r in risks[:5]) if risks else "  (none)")
            + "\n\nSuggestions:\n"
            + ("\n".join(f"  - {s}" for s in suggestions[:5]) if suggestions else "  (none)")
        )
        return ToolResult(
            success=result.approve,
            output=output,
            data={
                "score": result.score,
                "passed": result.approve,
                "missed": missed,
                "risks": risks,
                "suggestions": suggestions,
                "min_score": self._reviewer.min_score,
                "review_id": result.review_id,
                "reviewer_provider": self._reviewer.provider_name,
                "reviewer_model": self._reviewer.model_name,
                "source": "reviewer",
            },
            error=None if result.approve else (
                f"Reviewer rejected: score {result.score:.1f} < {self._reviewer.min_score}"
            ),
        )

    # --------------------------------------------------------
    # 回退路径（单 Agent Evaluator）
    # --------------------------------------------------------

    async def _run_with_evaluator(
        self, requirement: str, code_changes: list[CodeChange], project_path: str
    ) -> ToolResult:
        try:
            report = await self._evaluator.evaluate(
                original_requirement=requirement,
                code_changes=code_changes,
                test_results=None,
                project_path=project_path,
            )
        except Exception as e:
            return ToolResult(success=False, error=f"evaluate failed: {e}")

        # EvalReport 的加权总分字段名为 score（不是 weighted_score）
        score = float(getattr(report, "score", 0) or 0)
        passed = score >= self._min_score

        dim_lines = []
        for d in (report.dimensions or []):
            name = d.name if hasattr(d, "name") else d.get("name", "")
            sc = d.score if hasattr(d, "score") else d.get("score", 0)
            details = d.details if hasattr(d, "details") else d.get("details", "")
            dim_lines.append(f"  - {name}: {sc} — {details[:120]}")

        missed = report.requirements_missed or []
        risks = report.risks or []
        suggestions = report.suggestions or []

        output = (
            f"Evaluation score: {score:.1f}/100 ({'PASS' if passed else 'BELOW THRESHOLD'} "
            f"min={self._min_score})\n\n"
            f"Dimensions:\n" + "\n".join(dim_lines) + "\n\n"
            f"Requirements missed ({len(missed)}):\n"
            + ("\n".join(f"  - {m}" for m in missed[:5]) if missed else "  (none)")
            + "\n\nRisks:\n"
            + ("\n".join(f"  - {r}" for r in risks[:5]) if risks else "  (none)")
            + "\n\nSuggestions:\n"
            + ("\n".join(f"  - {s}" for s in suggestions[:5]) if suggestions else "  (none)")
        )

        return ToolResult(
            success=passed,
            output=output,
            data={
                "score": score,
                "passed": passed,
                "missed": missed,
                "risks": risks,
                "suggestions": suggestions,
                "min_score": self._min_score,
                "source": "evaluator",
            },
            error=None if passed else f"Score {score:.1f} below threshold {self._min_score}",
        )
