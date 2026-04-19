"""
EvaluateTool — 把 core.evaluator.Evaluator 的多维度打分包装为 ReactAgent 工具

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


class EvaluateTool(BaseTool):
    def __init__(self, evaluator=None, min_score: int = 70):
        self._evaluator = evaluator
        self._min_score = min_score

    @property
    def name(self) -> str:
        return "evaluate"

    @property
    def description(self) -> str:
        return (
            "Evaluate the current code changes across 4 dimensions: requirement_match (0.35), "
            "code_quality (0.25), test_coverage (0.20), security (0.20). Returns a weighted "
            "score 0-100 plus per-dimension details, requirements_met / missed, risks, "
            "suggestions. Call this AFTER build succeeds and BEFORE you respond to the user, "
            "so you can decide whether the implementation is good enough or needs another "
            "iteration. Score < 70 = consider propose_alternatives or another fix round."
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
        if not self._evaluator:
            return ToolResult(success=False, error="Evaluator not wired (server.py)")

        # 把 LLM 提供的简易 changed_files 列表转为 CodeChange 对象，附上磁盘上的实际内容
        code_changes: list[CodeChange] = []
        for entry in (changed_files or []):
            if not isinstance(entry, dict):
                continue
            fp = entry.get("file_path") or ""
            action = entry.get("action") or "modify"
            if not fp:
                continue
            content = ""
            try:
                p = Path(fp)
                if not p.is_absolute():
                    p = Path(project_path) / fp
                if p.is_file():
                    content = p.read_text(encoding="utf-8", errors="replace")
            except Exception as e:
                logger.debug(f"EvaluateTool: failed to read {fp}: {e}")
            code_changes.append(CodeChange(
                file_path=fp, action=action, diff="", content=content,
            ))

        try:
            report = await self._evaluator.evaluate(
                original_requirement=requirement,
                code_changes=code_changes,
                test_results=None,
                project_path=project_path,
            )
        except Exception as e:
            return ToolResult(success=False, error=f"evaluate failed: {e}")

        # 格式化输出
        score = report.weighted_score if hasattr(report, "weighted_score") else 0
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
            },
            error=None if passed else f"Score {score:.1f} below threshold {self._min_score}",
        )
