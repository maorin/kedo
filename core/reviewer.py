"""
Reviewer — 方案 C（双 Agent 对抗 / Actor-Critic）的独立审查 Agent

为什么是 Agent 而不是工具内临时 LLM client：
- **持久化对象**：跨关卡点累积判决历史，支持"增量 review"（上次审过的代码这次只看 diff）
- **独立 LLM provider**：物理破 Self-eval drift（Producer 与 Reviewer 不可同模型）
- **只读产物**：Reviewer 不写文件、不调工具，只看 [requirement + plan + 磁盘上的代码 + build/test 输出]

与 core.evaluator.Evaluator 的分工：
- Evaluator 负责静态检查 + LLM 打分 + merge 为 EvalReport —— 一切逻辑完整
- Reviewer 内部复用 Evaluator（借静态检查 + merge），**把 LLM 换成独立 provider**
- Reviewer 在 Evaluator 之上加：
    * 独立 system prompt（"你没写这段代码，你是独立裁判，不要向 Producer 让步"）
    * stage 感知（build_ok / test_ok / pre_commit）
    * 历史累积（跨关卡连续性）
    * approve 决定（ReviewResult vs 裸 EvalReport）

Rollback：配置 reviewer_provider: none → 上层不构造 Reviewer，EvaluateTool 与
CommitCandidateTool 完全走旧单 Agent 路径。
"""
from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass, field
from typing import Optional

from api.schemas import CodeChange, TestResult
from core.evaluator import Evaluator

logger = logging.getLogger(__name__)


REVIEWER_SYSTEM_PROMPT = """You are an **independent code reviewer** on a 2-agent system.
Another agent (the "Producer") wrote this code. **You did NOT write it.** Your job is
to catch problems the Producer missed — confirmation bias on its own work is your target.

CRITICAL SCOPING RULE:
You will be given a "Scoped Requirement" — the SPECIFIC sub-task whose output you must score.
You may also be given a "Parent Goal" — the wider project goal for context only.
Score `requirement_match` SOLELY against the Scoped Requirement. Do NOT penalize for things
that belong to the Parent Goal but are out of scope for this sub-task. If the Parent Goal
mentions X but the Scoped Requirement does not, X being absent is NOT a missed requirement.

Evaluate the code changes against the Scoped Requirement across these 4 dimensions:

1. **requirement_match** (weight 0.35): Does the code fulfill the Scoped Requirement?
2. **code_quality** (weight 0.25): Clean, readable, well-structured, following best practices?
3. **test_coverage** (weight 0.20): Adequate tests? Edge cases covered?
4. **security** (weight 0.20): Vulnerabilities, injection risks, unsafe patterns?

You will also receive static check results (syntax / lint / dangerous patterns) — factor
these into your scoring.

**Be honest, not agreeable.** The Producer is another LLM; it cannot be offended.
- If the code misses a requirement, say so in `requirements_missed`.
- If test coverage is 0 and the scoped requirement expects tests, `test_coverage` stays ≤40.
- If static checks flagged high-severity patterns, `security` stays ≤50 until cleaned.

Output JSON:
{
  "dimensions": [
    {"name": "requirement_match", "score": <0-100>, "details": "..."},
    {"name": "code_quality", "score": <0-100>, "details": "..."},
    {"name": "test_coverage", "score": <0-100>, "details": "..."},
    {"name": "security", "score": <0-100>, "details": "..."}
  ],
  "requirements_met": ["..."],
  "requirements_missed": ["..."],
  "risks": ["..."],
  "suggestions": ["..."]
}

Scoring guide per dimension:
- 90-100: Excellent
- 70-89: Good, minor issues
- 50-69: Acceptable, needs improvement
- Below 50: Needs significant rework
"""


@dataclass
class ReviewResult:
    """Reviewer 单次判决结果 — Producer 据此决定继续或重做"""
    approve: bool
    score: float
    stage: str
    comments: str
    dimensions: list[dict] = field(default_factory=list)
    requirements_met: list[str] = field(default_factory=list)
    requirements_missed: list[str] = field(default_factory=list)
    risks: list[str] = field(default_factory=list)
    suggestions: list[str] = field(default_factory=list)
    static_checks: dict = field(default_factory=dict)
    reviewer_model: str = ""
    review_id: str = field(default_factory=lambda: f"rev-{uuid.uuid4().hex[:6]}")


class Reviewer:
    """
    独立审查 Agent。

    典型用法（由 EvaluateTool / CommitCandidateTool 调用）：
        result = await reviewer.review(
            requirement="实现 libnfs 挂载逻辑",
            changed_files=[CodeChange(...)],
            project_path="/home/.../switchvideo",
            stage="pre_commit",
            test_results=None,
            parent_goal="NFS 视频播放器完整版",
        )
        if not result.approve:
            # Producer 重做或走 propose_alternatives / pause_for_human
    """

    def __init__(
        self,
        llm_client,
        memory,
        config: Optional[dict] = None,
        min_score: int = 70,
        max_history: int = 10,
    ):
        self._llm = llm_client
        self._memory = memory
        self._config = dict(config or {})
        self._min_score = min_score
        self._history: list[ReviewResult] = []
        self._max_history = max_history

        # 内部 Evaluator 只承担静态检查 + merge，LLM 走 reviewer 独立 client
        inner_config = {**self._config, "eval_system_prompt": REVIEWER_SYSTEM_PROMPT}
        self._inner = Evaluator(
            llm_client=llm_client,
            memory=memory,
            config=inner_config,
        )

    # ----------------------------------------------------------
    # 属性
    # ----------------------------------------------------------

    @property
    def is_active(self) -> bool:
        return self._llm is not None

    @property
    def model_name(self) -> str:
        return getattr(self._llm, "model", "unknown")

    @property
    def provider_name(self) -> str:
        return type(self._llm).__name__ if self._llm else "none"

    @property
    def min_score(self) -> int:
        return self._min_score

    # ----------------------------------------------------------
    # 主接口
    # ----------------------------------------------------------

    async def review(
        self,
        requirement: str,
        changed_files: list[CodeChange],
        project_path: str,
        stage: str = "pre_commit",
        test_results: Optional[TestResult] = None,
        parent_goal: str = "",
        test_strategy: str = "auto",
    ) -> ReviewResult:
        """
        对当前产物做一次独立判决。

        stage: "build_ok" / "test_ok" / "pre_commit" / "adhoc"
            只影响 comments 里的标签和历史记录，不影响打分权重。
        """
        # 历史判决作为背景拼入 parent_section（不改评分依据，只给连续性）
        history_note = self._history_prefix_for_prompt()
        effective_parent = parent_goal.strip() if parent_goal else ""
        if history_note:
            effective_parent = (
                history_note
                + ("\n\n" + effective_parent if effective_parent else "")
            )

        try:
            report = await self._inner.evaluate(
                original_requirement=requirement,
                code_changes=changed_files,
                test_results=test_results,
                project_path=project_path,
                parent_goal=effective_parent,
                test_strategy=test_strategy,
            )
        except Exception as e:
            logger.warning(f"Reviewer LLM call failed ({self.provider_name}): {e}")
            return ReviewResult(
                approve=False,
                score=0.0,
                stage=stage,
                comments=f"Reviewer 调用失败（{self.provider_name}）: {e}",
                reviewer_model=self.model_name,
            )

        score = float(report.score or 0)
        approve = score >= self._min_score
        comments = self._format_summary(report, stage)

        result = ReviewResult(
            approve=approve,
            score=score,
            stage=stage,
            comments=comments,
            dimensions=[
                {"name": d.name, "score": d.score, "details": d.details}
                for d in (report.dimensions or [])
            ],
            requirements_met=list(report.requirements_met or []),
            requirements_missed=list(report.requirements_missed or []),
            risks=list(report.risks or []),
            suggestions=list(report.suggestions or []),
            static_checks=report.static_checks or {},
            reviewer_model=self.model_name,
        )
        self._push_history(result)
        logger.info(
            f"Reviewer[{self.provider_name}/{self.model_name}] review {result.review_id} "
            f"stage={stage} score={score:.1f} approve={approve}"
        )
        return result

    # ----------------------------------------------------------
    # 历史累积
    # ----------------------------------------------------------

    def history(self) -> list[ReviewResult]:
        return list(self._history)

    def reset_history(self) -> None:
        """新 task 开始时调用，清空跨关卡累积。"""
        self._history = []

    def _push_history(self, result: ReviewResult) -> None:
        self._history.append(result)
        if len(self._history) > self._max_history:
            self._history = self._history[-self._max_history:]

    def _history_prefix_for_prompt(self) -> str:
        """把最近几次判决压成一段注释，让 Reviewer 知道自己之前给过什么意见。"""
        if not self._history:
            return ""
        recent = self._history[-3:]
        lines = ["Prior reviews of this task (background, do NOT rescore these):"]
        for r in recent:
            miss = "; ".join(r.requirements_missed[:3]) if r.requirements_missed else "(none)"
            lines.append(
                f"- {r.review_id} [{r.stage}] score={r.score:.0f} "
                f"approve={r.approve} missed={miss}"
            )
        return "\n".join(lines)

    # ----------------------------------------------------------
    # 格式化
    # ----------------------------------------------------------

    @staticmethod
    def _format_summary(report, stage: str) -> str:
        dims = ", ".join(
            f"{d.name}={d.score:.0f}" for d in (report.dimensions or [])
        )
        parts = [f"[stage={stage}] score={report.score:.1f}/100 ({dims})"]
        miss = (report.requirements_missed or [])[:3]
        risks = (report.risks or [])[:3]
        if miss:
            parts.append("Missed: " + "; ".join(miss))
        if risks:
            parts.append("Risks: " + "; ".join(risks))
        if not (miss or risks):
            parts.append("No blocking findings.")
        return " | ".join(parts)
