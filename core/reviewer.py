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

Evaluate the code changes against the Scoped Requirement across these 5 dimensions:

1. **requirement_match** (weight 0.30): Does the code fulfill the Scoped Requirement?
2. **code_quality** (weight 0.20): Clean, readable, well-structured, following best practices?
3. **test_coverage** (weight 0.15): Adequate tests? Edge cases covered?
4. **security** (weight 0.15): Vulnerabilities, injection risks, unsafe patterns?
5. **deliverable_completeness** (weight 0.20): **Can the user run the deliverable end-to-end with ONLY what this candidate provides?**

The 5th dimension is critical — it catches scope-handoff abuse where the Producer produces
client-side code but tells the user "you also need to start a Python HTTP server / database /
service on your machine first" without providing it. Specifically deduct points for:

- Code references an external service (HTTP server, database, RPC endpoint, file share)
  but no script / config / Dockerfile / setup instructions is included in changed_files.
- Commit message / summary contains phrases like "you need to", "start the server",
  "run python -m ...", "set up X first" — and that X is NOT in changed_files.
- Charter declares an `external_services` entry with `provider: task` but the corresponding
  files are not present in changed_files.
- README / usage section in changed files describes a multi-step setup the user must do
  manually, with no automation provided (e.g., a docker-compose or a shell script).

Score deliverable_completeness ≤50 if any of the above hits. Score ≥80 only if a fresh user
can clone + build + run the candidate without any "you-also-need-to" hand-off step.

You will also receive static check results (syntax / lint / dangerous patterns) — factor
these into your scoring.

**Be honest, not agreeable.** The Producer is another LLM; it cannot be offended.
- If the code misses a requirement, say so in `requirements_missed`.
- If test coverage is 0 and the scoped requirement expects tests, `test_coverage` stays ≤40.
- If static checks flagged high-severity patterns, `security` stays ≤50 until cleaned.
- If deliverable handoff is incomplete, list the missing piece in `requirements_missed`
  prefixed with `[deliverable]`.

Output JSON:
{
  "dimensions": [
    {"name": "requirement_match", "score": <0-100>, "details": "..."},
    {"name": "code_quality", "score": <0-100>, "details": "..."},
    {"name": "test_coverage", "score": <0-100>, "details": "..."},
    {"name": "security", "score": <0-100>, "details": "..."},
    {"name": "deliverable_completeness", "score": <0-100>, "details": "..."}
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


# Reviewer 独立维度配置 — 比 Evaluator 多 deliverable_completeness 这一维, 权重重排.
# 通过 inner_config["eval_dimensions"] 注入 Evaluator (Evaluator 的 DEFAULT_DIMENSIONS 不动).
REVIEWER_DIMENSIONS = [
    {"name": "requirement_match",        "weight": 0.30, "label": "需求匹配"},
    {"name": "code_quality",             "weight": 0.20, "label": "代码质量"},
    {"name": "test_coverage",            "weight": 0.15, "label": "测试覆盖"},
    {"name": "security",                 "weight": 0.15, "label": "安全性"},
    {"name": "deliverable_completeness", "weight": 0.20, "label": "交付完整性"},
]


REVIEWER_STUCK_BUILD_SYSTEM_PROMPT = """You are an **independent senior engineer** acting
as advisor on a 2-agent system. The other agent (Producer) is **stuck on a build that
keeps failing the same way**. You are NOT scoring its work — you are giving it directions.

Your goals, in order:
1. **Diagnose** the actual root cause from the build errors and the working-tree state
   (don't just paraphrase the stderr — say *what's wrong structurally*).
2. **Tell the Producer what to STOP doing.** It has been retrying; if it's been changing
   the same files in circles, name them.
3. **Tell it the next concrete action.** Be specific: a file:line edit, a single shell
   command, a rollback (`git checkout -- <path>`), or escalate (call `pause_for_human`).
4. If it keeps drifting (e.g. introducing new build systems, rewriting profile commands,
   renaming targets), call that out explicitly and tell it to revert.

Be terse. Output 4–8 lines, no bullet headers, no JSON. Address the Producer in 2nd
person ("You should…", "Stop changing X…"). Do NOT pretend things are fine — if the
build is broken because of structural damage from earlier turns, say so.
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

        # 内部 Evaluator 只承担静态检查 + merge，LLM 走 reviewer 独立 client.
        # 注入 REVIEWER_DIMENSIONS 让 Reviewer 比 Evaluator 多 deliverable_completeness 这一维.
        inner_config = {
            **self._config,
            "eval_system_prompt": REVIEWER_SYSTEM_PROMPT,
            "eval_dimensions": REVIEWER_DIMENSIONS,
        }
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
        # 方案 C：charter 存在时拼入。Reviewer 看 diff 评分时照 charter 来检查
        if project_path:
            try:
                from core.project_charter import Charter
                charter = Charter.load(project_path)
                if charter is not None:
                    charter_block = (
                        charter.summarize_for_prompt()
                        + "\n\nWhen the diff violates this charter, name the violation as "
                        "`charter:<field>` in `requirements_missed` and keep the score below "
                        "the approve threshold."
                    )
                    effective_parent = (
                        charter_block
                        + ("\n\n" + effective_parent if effective_parent else "")
                    )
            except Exception as e:
                logger.debug(f"Reviewer.review: charter inject skipped: {e}")

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
    # 卡死 build 时的指令性反馈（不打分，纯文本建议）
    # ----------------------------------------------------------

    async def advise_on_stuck_build(
        self,
        task_description: str,
        build_command: str,
        recent_errors: list[str],
        working_tree_summary: str = "",
        recent_actions: list[str] = None,
        project_path: str = "",
    ) -> str:
        """Producer 的 build 工具连续失败 N 次后调用，让 Reviewer 给一段指令性反馈。

        与 review() 不同，这里不打分、不写历史、不走 evaluator —— 直接调独立 LLM 拿
        一段建议文本。失败时返回一段降级提示，不抛异常（调用方拿到啥就回灌啥给 Producer）。
        """
        recent_actions = recent_actions or []
        # 最近 3 条错误就够，避免单次 prompt 太大
        errors_block = "\n\n---\n\n".join(
            f"[Build attempt {-len(recent_errors) + i + 1}]\n{e[-1200:]}"
            for i, e in enumerate(recent_errors[-3:])
        ) or "(no recent errors captured)"

        actions_block = (
            "\n".join(f"- {a}" for a in recent_actions[-8:])
            if recent_actions else "(not provided)"
        )

        user_msg = (
            f"Producer task:\n{task_description.strip()[:1500]}\n\n"
            f"Current build command:\n  {build_command or '(unknown)'}\n\n"
            f"Recent build errors (oldest first):\n{errors_block}\n\n"
            f"Working tree (relevant build/profile files):\n{working_tree_summary or '(not summarized)'}\n\n"
            f"Producer's recent actions:\n{actions_block}\n\n"
            f"Give the Producer a short directive. Diagnose, tell it what to stop, "
            f"and name the next concrete step."
        )

        # 方案 C：charter 存在时把摘要拼入 Reviewer system prompt，反馈时引用 charter:<field>
        system_prompt = REVIEWER_STUCK_BUILD_SYSTEM_PROMPT
        if project_path:
            try:
                from core.project_charter import Charter
                charter = Charter.load(project_path)
                if charter is not None:
                    system_prompt = (
                        system_prompt
                        + "\n\n"
                        + charter.summarize_for_prompt()
                        + "\n\n**When citing violations, use `charter:<field>` notation "
                        "(e.g. 'charter:build.forbidden_files', 'charter:artifact.target_name') "
                        "so the Producer can map your feedback to the contract directly.**"
                    )
            except Exception as e:
                logger.debug(f"Reviewer.advise: charter inject skipped: {e}")

        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_msg},
        ]

        if not self._llm:
            return (
                "[Reviewer disabled] No independent reviewer is configured. "
                "Suggest you call `pause_for_human` with a summary of what you've tried "
                "and what the build error says."
            )
        try:
            advice = await self._llm.chat(messages)
        except Exception as e:
            logger.warning(f"Reviewer.advise_on_stuck_build failed ({self.provider_name}): {e}")
            return (
                f"[Reviewer unavailable: {e}] Cannot get an independent opinion this turn. "
                f"Recommend you stop iterating, run `pause_for_human` with the latest stderr, "
                f"and let a human redirect."
            )
        text = (advice or "").strip()
        if not text:
            return (
                "[Reviewer returned empty] The independent reviewer had nothing to say. "
                "Treat this as a signal that the situation is unusual — escalate via "
                "`pause_for_human` rather than retrying the same approach."
            )
        logger.info(
            f"Reviewer[{self.provider_name}/{self.model_name}] stuck-build advice "
            f"({len(text)} chars) for task fragment: {task_description[:60]!r}"
        )
        return text

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
