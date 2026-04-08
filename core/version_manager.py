"""
VersionManager — 候选版本管理器

在 AI 开发过程中，Agent 会在特定时刻创建"候选版本"（Candidate）。
每个候选版本是代码在某一时刻的可测试快照。
人工审查门依赖这些候选版本进行选择和审批。

核心流程:
                                ┌─────────────────────┐
  Agent 开发中 ────────────────►│  创建候选版本 (v1)    │
  (编码/编译/测试通过)           │  status: created     │
                                └──────┬──────────────┘
                                       │
                              AI 评估认为可测试
                                       │
                                       ▼
                                ┌─────────────────────┐
                                │  AI 推荐 (v1)        │
                                │  status: recommended │◄─── Dashboard 显示推荐标记
                                └──────┬──────────────┘
                                       │
                              用户选择此版本测试
                                       │
                                       ▼
                                ┌─────────────────────┐
                                │  人工测试中 (v1)      │
                                │  status: testing     │◄─── Agent 可继续开发 v2
                                └──────┬──────────────┘
                                       │
                              ┌────────┴────────┐
                              │                 │
                              ▼                 ▼
                     ┌──────────────┐  ┌──────────────┐
                     │  Approved ✅  │  │  Rejected ❌  │
                     │  → 进入部署   │  │  → 反馈给Agent│
                     └──────────────┘  └──────────────┘

关键设计:
- Agent 在开发过程中可以创建多个候选版本 (v1, v2, v3...)
- Agent 继续开发不会阻塞，人工测试和开发可以并行
- 人工只对"AI 推荐"的版本进行测试
- 每个版本包含: 代码快照、Git commit、测试结果、AI 评估报告
"""
from __future__ import annotations

import logging
from datetime import datetime
from typing import Optional

from api.schemas import (
    AgentEvent,
    CandidateStatus,
    CandidateVersion,
    CodeChange,
    EvalReport,
    EventType,
    TestResult,
)

logger = logging.getLogger(__name__)


class VersionManager:
    """
    候选版本管理器

    职责:
    - 创建候选版本快照
    - 管理版本状态流转
    - 提供版本查询、比较接口
    - 与 Git 集成创建 tag/branch
    """

    def __init__(self, event_bus=None, git_tool=None):
        self._event_bus = event_bus
        self._git = git_tool

        # task_id → [CandidateVersion] (按版本号排序)
        self._candidates: dict[str, list[CandidateVersion]] = {}

    # ----------------------------------------------------------
    # 创建候选版本
    # ----------------------------------------------------------

    async def create_candidate(
        self,
        task_id: str,
        code_changes: list[CodeChange],
        test_results: Optional[TestResult] = None,
        eval_report: Optional[EvalReport] = None,
        build_success: bool = False,
        ai_confidence: float = 0.0,
        ai_summary: str = "",
        project_path: str = ".",
    ) -> CandidateVersion:
        """
        创建一个新的候选版本

        通常由 Agent Loop 在以下时刻调用:
        - 编译 + 测试通过后
        - 评估分数达标后
        - 完成一轮完整的 code → build → test 循环后
        """
        if task_id not in self._candidates:
            self._candidates[task_id] = []

        version_number = len(self._candidates[task_id]) + 1

        # 创建 Git commit/tag (如果有 git tool)
        git_hash = ""
        git_branch = ""
        if self._git:
            try:
                commit_result = await self._git.execute(
                    action="add_commit",
                    project_path=project_path,
                    args=f"-am 'candidate-v{version_number}: {ai_summary[:50]}'",
                )
                if commit_result.success:
                    hash_result = await self._git.execute(
                        action="log", project_path=project_path,
                        args="--format=%H -1",
                    )
                    git_hash = hash_result.output.strip() if hash_result.success else ""

                    # 创建 tag
                    await self._git.execute(
                        action="tag", project_path=project_path,
                        args=f"candidate-v{version_number}",
                    )

                branch_result = await self._git.execute(
                    action="branch", project_path=project_path,
                    args="--show-current",
                )
                git_branch = branch_result.output.strip() if branch_result.success else ""
            except Exception as e:
                logger.warning(f"Git operations failed: {e}")

        # 计算变更统计
        lines_added = 0
        lines_removed = 0
        for change in code_changes:
            for line in (change.diff or "").split("\n"):
                if line.startswith("+") and not line.startswith("+++"):
                    lines_added += 1
                elif line.startswith("-") and not line.startswith("---"):
                    lines_removed += 1

        candidate = CandidateVersion(
            task_id=task_id,
            version_number=version_number,
            status=CandidateStatus.CREATED,
            ai_confidence=ai_confidence,
            ai_summary=ai_summary,
            testable=ai_confidence >= 60,  # AI 信心 >= 60 视为可测试
            git_commit_hash=git_hash,
            git_branch=git_branch,
            code_changes=code_changes,
            files_changed=len(code_changes),
            lines_added=lines_added,
            lines_removed=lines_removed,
            test_results=test_results,
            eval_report=eval_report,
            build_success=build_success,
        )

        self._candidates[task_id].append(candidate)

        # 发布事件
        await self._emit(task_id, EventType.CANDIDATE_CREATED, {
            "version_id": candidate.version_id,
            "version_number": version_number,
            "ai_confidence": ai_confidence,
            "testable": candidate.testable,
        })

        logger.info(
            f"Candidate v{version_number} created for task {task_id} "
            f"(confidence={ai_confidence}, testable={candidate.testable})"
        )

        # 如果 AI 认为可测试，自动推荐
        if candidate.testable:
            await self.recommend_candidate(task_id, candidate.version_id, ai_summary)

        return candidate

    # ----------------------------------------------------------
    # 版本状态流转
    # ----------------------------------------------------------

    async def recommend_candidate(
        self,
        task_id: str,
        version_id: str,
        recommendation: str = "",
    ):
        """AI 推荐此版本可供人工测试"""
        candidate = self._find(task_id, version_id)
        if not candidate:
            return

        candidate.status = CandidateStatus.AI_RECOMMENDED
        candidate.ai_recommendation = recommendation
        candidate.recommended_at = datetime.utcnow()

        await self._emit(task_id, EventType.CANDIDATE_RECOMMENDED, {
            "version_id": version_id,
            "version_number": candidate.version_number,
            "ai_confidence": candidate.ai_confidence,
            "ai_summary": candidate.ai_summary,
            "ai_recommendation": recommendation,
            "test_passed": candidate.test_results.passed if candidate.test_results else 0,
            "test_total": candidate.test_results.total if candidate.test_results else 0,
        })

        logger.info(f"Candidate v{candidate.version_number} recommended for task {task_id}")

    async def select_for_testing(self, task_id: str, version_id: str) -> Optional[CandidateVersion]:
        """用户选择一个候选版本进行人工测试"""
        candidate = self._find(task_id, version_id)
        if not candidate:
            return None

        candidate.status = CandidateStatus.HUMAN_TESTING

        await self._emit(task_id, EventType.CANDIDATE_SELECTED, {
            "version_id": version_id,
            "version_number": candidate.version_number,
        })

        logger.info(f"Candidate v{candidate.version_number} selected for testing")
        return candidate

    async def approve_candidate(
        self,
        task_id: str,
        version_id: str,
        feedback: str = "",
        test_notes: str = "",
    ) -> Optional[CandidateVersion]:
        """人工审批通过"""
        candidate = self._find(task_id, version_id)
        if not candidate:
            return None

        candidate.status = CandidateStatus.APPROVED
        candidate.human_feedback = feedback
        candidate.human_test_notes = test_notes
        candidate.reviewed_at = datetime.utcnow()

        # 将其他版本标记为 superseded
        for c in self._candidates.get(task_id, []):
            if c.version_id != version_id and c.status not in (
                CandidateStatus.APPROVED, CandidateStatus.REJECTED
            ):
                c.status = CandidateStatus.SUPERSEDED

        await self._emit(task_id, EventType.CANDIDATE_APPROVED, {
            "version_id": version_id,
            "version_number": candidate.version_number,
            "feedback": feedback,
        })

        return candidate

    async def reject_candidate(
        self,
        task_id: str,
        version_id: str,
        feedback: str = "",
        test_notes: str = "",
    ) -> Optional[CandidateVersion]:
        """人工驳回"""
        candidate = self._find(task_id, version_id)
        if not candidate:
            return None

        candidate.status = CandidateStatus.REJECTED
        candidate.human_feedback = feedback
        candidate.human_test_notes = test_notes
        candidate.reviewed_at = datetime.utcnow()

        await self._emit(task_id, EventType.CANDIDATE_REJECTED, {
            "version_id": version_id,
            "version_number": candidate.version_number,
            "feedback": feedback,
        })

        return candidate

    # ----------------------------------------------------------
    # 查询接口
    # ----------------------------------------------------------

    def get_candidates(self, task_id: str) -> list[CandidateVersion]:
        """获取任务的所有候选版本"""
        return self._candidates.get(task_id, [])

    def get_latest(self, task_id: str) -> Optional[CandidateVersion]:
        """获取最新候选版本"""
        candidates = self._candidates.get(task_id, [])
        return candidates[-1] if candidates else None

    def get_recommended(self, task_id: str) -> Optional[CandidateVersion]:
        """获取 AI 推荐的最新候选版本"""
        candidates = self._candidates.get(task_id, [])
        for c in reversed(candidates):
            if c.status == CandidateStatus.AI_RECOMMENDED:
                return c
        return None

    def get_approved(self, task_id: str) -> Optional[CandidateVersion]:
        """获取已审批通过的版本"""
        for c in self._candidates.get(task_id, []):
            if c.status == CandidateStatus.APPROVED:
                return c
        return None

    def get_testing(self, task_id: str) -> Optional[CandidateVersion]:
        """获取正在人工测试的版本"""
        for c in self._candidates.get(task_id, []):
            if c.status == CandidateStatus.HUMAN_TESTING:
                return c
        return None

    # ----------------------------------------------------------
    # 内部方法
    # ----------------------------------------------------------

    def _find(self, task_id: str, version_id: str) -> Optional[CandidateVersion]:
        for c in self._candidates.get(task_id, []):
            if c.version_id == version_id:
                return c
        return None

    async def _emit(self, task_id: str, event_type: EventType, data: dict):
        if self._event_bus:
            await self._event_bus.publish(AgentEvent(
                event_type=event_type,
                task_id=task_id,
                data=data,
            ))
