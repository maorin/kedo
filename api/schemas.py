"""
Pydantic 数据模型 — 定义所有 API 请求/响应 和核心数据结构
"""
from __future__ import annotations

import uuid
from datetime import datetime
from enum import Enum
from typing import Any, Optional

from pydantic import BaseModel, Field


# ============================================================
# 枚举类型
# ============================================================

class TaskStatus(str, Enum):
    PENDING = "pending"
    PLANNING = "planning"
    IN_PROGRESS = "in_progress"
    PAUSED = "paused"
    REVIEWING = "reviewing"
    APPROVED = "approved"
    REJECTED = "rejected"
    DEPLOYING = "deploying"
    COMPLETED = "completed"
    FAILED = "failed"


class StepType(str, Enum):
    PLAN = "plan"
    CODE_GENERATE = "code_generate"
    BUILD = "build"
    TEST = "test"
    EVALUATE = "evaluate"
    REVIEW = "review"
    DEPLOY = "deploy"
    MONITOR = "monitor"


class CandidateStatus(str, Enum):
    """候选版本状态"""
    CREATED = "created"             # 刚创建
    AI_RECOMMENDED = "ai_recommended"  # AI 推荐可以人工测试
    HUMAN_TESTING = "human_testing"    # 人工正在测试
    APPROVED = "approved"              # 人工审批通过
    REJECTED = "rejected"              # 人工驳回
    SUPERSEDED = "superseded"          # 被更新版本取代


class DiscussionStatus(str, Enum):
    """讨论阶段状态"""
    ANALYZING = "analyzing"           # AI 正在分析失败原因
    PROPOSALS_READY = "proposals_ready"  # 方案已生成，等待选择
    WAITING_HUMAN = "waiting_human"    # 等待人工介入讨论
    RESOLVED = "resolved"              # 讨论结束，方案已确认


class EventType(str, Enum):
    TASK_CREATED = "task_created"
    TASK_STATUS_CHANGED = "task_status_changed"
    STEP_STARTED = "step_started"
    STEP_COMPLETED = "step_completed"
    STEP_FAILED = "step_failed"
    AGENT_PAUSED = "agent_paused"
    AGENT_RESUMED = "agent_resumed"
    REVIEW_REQUESTED = "review_requested"
    CANDIDATE_CREATED = "candidate_created"
    CANDIDATE_RECOMMENDED = "candidate_recommended"
    CANDIDATE_SELECTED = "candidate_selected"
    CANDIDATE_APPROVED = "candidate_approved"
    CANDIDATE_REJECTED = "candidate_rejected"
    # 讨论 & 闭环事件
    DISCUSSION_STARTED = "discussion_started"
    DISCUSSION_PROPOSALS = "discussion_proposals"
    DISCUSSION_WAITING_HUMAN = "discussion_waiting_human"
    DISCUSSION_RESOLVED = "discussion_resolved"
    REPLAN_STARTED = "replan_started"
    REPLAN_COMPLETED = "replan_completed"
    ITERATION_UPDATED = "iteration_updated"
    # LLM 交互事件
    LLM_REQUEST = "llm_request"
    LLM_RESPONSE = "llm_response"
    LLM_TOKEN = "llm_token"
    TOOL_EXECUTE = "tool_execute"
    LOG_MESSAGE = "log_message"
    METRIC_UPDATE = "metric_update"
    # 讨论多轮对话事件
    DISCUSSION_RESPONSE = "discussion_response"
    # 子 Agent 事件
    SUB_AGENT_STARTED = "sub_agent_started"
    SUB_AGENT_COMPLETED = "sub_agent_completed"
    PARALLEL_BATCH_STARTED = "parallel_batch_started"
    PARALLEL_BATCH_COMPLETED = "parallel_batch_completed"


class ReviewDecision(str, Enum):
    APPROVE = "approve"
    REJECT = "reject"
    EDIT = "edit"


# ============================================================
# 核心模型
# ============================================================

class SubTask(BaseModel):
    """拆解后的子任务"""
    id: str = Field(default_factory=lambda: str(uuid.uuid4())[:8])
    title: str
    description: str = ""
    step_type: StepType
    status: TaskStatus = TaskStatus.PENDING
    dependencies: list[str] = Field(default_factory=list)
    result: Optional[dict[str, Any]] = None
    retry_count: int = 0
    max_retries: int = 3


class TaskPlan(BaseModel):
    """Agent 生成的执行计划"""
    task_id: str
    subtasks: list[SubTask] = Field(default_factory=list)
    created_at: datetime = Field(default_factory=datetime.utcnow)
    estimated_duration_minutes: Optional[int] = None


class CodeChange(BaseModel):
    """代码变更记录"""
    file_path: str
    action: str  # "create", "modify", "delete"
    diff: str = ""
    content: Optional[str] = None


class TestResult(BaseModel):
    """测试结果"""
    total: int = 0
    passed: int = 0
    failed: int = 0
    skipped: int = 0
    coverage_percent: float = 0.0
    failures: list[dict[str, str]] = Field(default_factory=list)
    duration_seconds: float = 0.0


class EvalDimension(BaseModel):
    """单维度评估结果"""
    name: str           # "requirement_match", "code_quality", "test_coverage", "security"
    score: float        # 0-100
    weight: float       # 权重 (所有维度权重之和 = 1.0)
    details: str = ""   # 评分理由


class EvalReport(BaseModel):
    """质量评估报告（多维度）"""
    score: float = 0.0  # 加权总分 0-100
    dimensions: list[EvalDimension] = Field(default_factory=list)
    requirements_met: list[str] = Field(default_factory=list)
    requirements_missed: list[str] = Field(default_factory=list)
    risks: list[str] = Field(default_factory=list)
    suggestions: list[str] = Field(default_factory=list)
    static_checks: dict[str, Any] = Field(default_factory=dict)


class CandidateVersion(BaseModel):
    """
    候选版本 — AI 在开发过程中创建的"可测试状态快照"

    当 AI 认为当前代码处于一个可供人工测试的状态时，
    会自动创建一个 CandidateVersion。人工审查门依赖这些候选版本。

    生命周期:
    created → ai_recommended → (人工选择) → human_testing → approved / rejected
    """
    version_id: str = Field(default_factory=lambda: f"v-{uuid.uuid4().hex[:6]}")
    task_id: str
    version_number: int = 1                          # 递增的版本号 (v1, v2, v3...)
    status: CandidateStatus = CandidateStatus.CREATED

    # AI 评估信息
    ai_confidence: float = 0.0                       # AI 对此版本的信心分 (0-100)
    ai_summary: str = ""                             # AI 对此版本的简短说明
    ai_recommendation: str = ""                      # AI 推荐理由 / 测试建议
    testable: bool = False                           # AI 是否认为可以人工测试

    # 代码状态快照
    git_commit_hash: str = ""                        # Git commit SHA
    git_branch: str = ""                             # Git 分支名
    code_changes: list[CodeChange] = Field(default_factory=list)
    files_changed: int = 0
    lines_added: int = 0
    lines_removed: int = 0

    # 自动化测试结果
    test_results: Optional[TestResult] = None
    eval_report: Optional[EvalReport] = None
    build_success: bool = False

    # 人工反馈
    human_feedback: str = ""
    human_test_notes: str = ""

    # 时间戳
    created_at: datetime = Field(default_factory=datetime.utcnow)
    recommended_at: Optional[datetime] = None
    reviewed_at: Optional[datetime] = None


class IssueItem(BaseModel):
    """评估失败时的单个问题项"""
    category: str = ""         # "功能缺失", "代码质量", "安全性", "性能", "测试覆盖"
    severity: str = "medium"   # "critical", "high", "medium", "low"
    description: str = ""
    eval_dimension: str = ""   # 对应 EvalReport 中的哪个维度
    suggestion: str = ""       # AI 的初步修复建议


class Proposal(BaseModel):
    """讨论阶段的解决方案提案"""
    proposal_id: str = Field(default_factory=lambda: f"p-{uuid.uuid4().hex[:6]}")
    title: str = ""
    description: str = ""
    approach: str = ""                       # 具体的技术方案
    estimated_effort: str = ""               # "small", "medium", "large"
    risk_level: str = "medium"               # "low", "medium", "high"
    pros: list[str] = Field(default_factory=list)
    cons: list[str] = Field(default_factory=list)
    ai_recommended: bool = False             # AI 是否推荐这个方案


class DiscussionRecord(BaseModel):
    """
    讨论记录 — 评估不通过或人工 Reject 时产生

    完整闭环：
    1. 评估不通过 / 人工 Reject → 创建 DiscussionRecord
    2. AI 分析失败原因 → 填充 issues
    3. AI 提出多个方案 → 填充 proposals
    4. (可选) 暂停等待人工讨论 → human_input
    5. 确认方案 → selected_proposal_id
    6. 注入 Replan 上下文 → resolved
    """
    discussion_id: str = Field(default_factory=lambda: f"disc-{uuid.uuid4().hex[:6]}")
    task_id: str
    iteration: int = 1                       # 当前是第几轮迭代
    trigger: str = ""                        # "eval_failed" | "human_rejected"
    status: DiscussionStatus = DiscussionStatus.ANALYZING

    # 问题分析
    issues: list[IssueItem] = Field(default_factory=list)
    eval_report: Optional[EvalReport] = None  # 触发此讨论的评估报告
    human_feedback: str = ""                  # 人工 Reject 时的反馈

    # 方案提案
    proposals: list[Proposal] = Field(default_factory=list)
    selected_proposal_id: str = ""            # 被选中的方案 ID
    human_input: str = ""                     # 人工讨论时追加的意见

    # 结论
    replan_context: str = ""                  # 注入给 Planner 的上下文摘要
    resolved_at: Optional[datetime] = None

    # 时间戳
    created_at: datetime = Field(default_factory=datetime.utcnow)


class IterationState(BaseModel):
    """迭代计数器 — 跟踪闭环次数，防止无限循环"""
    task_id: str
    current_iteration: int = 1
    max_iterations: int = 5
    discussions: list[DiscussionRecord] = Field(default_factory=list)
    is_forced_pause: bool = False             # 超过最大迭代时强制暂停


class AgentCheckpoint(BaseModel):
    """Agent 状态检查点 — 用于暂停/恢复"""
    task_id: str
    current_step_index: int
    plan: TaskPlan
    memory_snapshot: dict[str, Any] = Field(default_factory=dict)
    code_changes: list[CodeChange] = Field(default_factory=list)
    test_results: Optional[TestResult] = None
    eval_report: Optional[EvalReport] = None
    created_at: datetime = Field(default_factory=datetime.utcnow)


# ============================================================
# API 请求 / 响应模型
# ============================================================

class CreateTaskRequest(BaseModel):
    """创建任务请求"""
    description: str
    project_path: str = "."
    config: dict[str, Any] = Field(default_factory=dict)


class CreateTaskResponse(BaseModel):
    task_id: str
    status: TaskStatus
    message: str


class TaskStatusResponse(BaseModel):
    task_id: str
    status: TaskStatus
    current_step: Optional[str] = None
    progress_percent: float = 0.0
    plan: Optional[TaskPlan] = None
    code_changes: list[CodeChange] = Field(default_factory=list)
    test_results: Optional[TestResult] = None
    eval_report: Optional[EvalReport] = None
    logs: list[str] = Field(default_factory=list)


class ReviewRequest(BaseModel):
    """人工审查决策"""
    decision: ReviewDecision
    version_id: str = ""              # 审查的候选版本 ID
    feedback: str = ""
    test_notes: str = ""              # 人工测试记录
    edits: list[CodeChange] = Field(default_factory=list)


class SelectCandidateRequest(BaseModel):
    """选择候选版本进行人工测试"""
    version_id: str


class CandidateListResponse(BaseModel):
    """候选版本列表响应"""
    task_id: str
    candidates: list[CandidateVersion] = Field(default_factory=list)
    recommended_version_id: Optional[str] = None    # AI 推荐的版本


class DiscussionInputRequest(BaseModel):
    """人工参与讨论的请求"""
    action: str = "select"         # "select" 选方案 | "ask" 追问 | "custom" 自定义方案
    proposal_id: str = ""          # 选择的方案 ID（为空时AI自动选择）
    human_input: str = ""          # 人工追加的意见或修复思路
    additional_constraints: list[str] = Field(default_factory=list)  # 追加的需求约束


class DiscussionResponse(BaseModel):
    """讨论状态响应"""
    discussion_id: str
    task_id: str
    iteration: int
    status: DiscussionStatus
    issues: list[IssueItem] = Field(default_factory=list)
    proposals: list[Proposal] = Field(default_factory=list)
    selected_proposal_id: str = ""


class PauseResumeResponse(BaseModel):
    task_id: str
    status: TaskStatus
    message: str


class AgentEvent(BaseModel):
    """实时推送事件"""
    event_type: EventType
    task_id: str
    timestamp: datetime = Field(default_factory=datetime.utcnow)
    data: dict[str, Any] = Field(default_factory=dict)
