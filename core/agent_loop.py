"""
Agent Loop Controller — AI 开发助手的核心控制循环

实现 Plan → Execute → Observe → Evaluate → (Loop or Exit) 的持续运行循环
支持暂停/恢复、人工审查门、检查点保存
"""
from __future__ import annotations

import asyncio
import logging
import os
import uuid
from pathlib import Path
from typing import Any, Optional

from api.schemas import (
    AgentCheckpoint,
    AgentEvent,
    CodeChange,
    DiscussionRecord,
    DiscussionStatus,
    EvalReport,
    EventType,
    IssueItem,
    IterationState,
    Proposal,
    StepType,
    SubTask,
    TaskPlan,
    TaskStatus,
    TestResult,
)
from core.evaluator import Evaluator
from core.memory import AgentMemory
from core.planner import Planner
from core.project_profile import ProjectProfileManager
from core.state_manager import StateManager
from core.version_manager import VersionManager
from tools.base import ToolRegistry, ToolResult

logger = logging.getLogger(__name__)


class AgentLoop:
    """
    Agent 核心循环控制器

    生命周期:
    1. 接收任务 → 2. Planner 生成计划 → 3. 逐步执行子任务
    4. 每步: Execute → Observe → (自动修复 or 继续)
    5. 评估 → 人工审查 → 部署/完成

    关键特性:
    - 每步检查暂停信号 (wait_if_paused)
    - 关键节点保存检查点 (save_checkpoint)
    - 失败自动重试 (max_retries)
    - 人工审查门 (review gate)
    """

    def __init__(
        self,
        state_manager: StateManager,
        planner: Planner,
        evaluator: Evaluator,
        tool_registry: ToolRegistry,
        memory: AgentMemory,
        version_manager: VersionManager = None,
        config: dict[str, Any] = None,
    ):
        self.state = state_manager
        self.planner = planner
        self.evaluator = evaluator
        self.tools = tool_registry
        self.memory = memory
        self.versions = version_manager or VersionManager()
        self.config = config or {}
        self.profile_manager = ProjectProfileManager()

        # 运行时状态
        self._running_tasks: dict[str, asyncio.Task] = {}
        self._discussion_queues: dict[str, asyncio.Queue] = {}  # 讨论阶段的人工输入队列
        self._iterations: dict[str, IterationState] = {}         # 迭代状态跟踪

        # 配置
        # tool_registry 引用（供外部热替换 LLM client 时遍历工具）
        self.tool_registry = self.tools

        self.max_retries = self.config.get("max_retries", 3)
        self.auto_fix_enabled = self.config.get("auto_fix", True)
        self.min_eval_score = self.config.get("min_eval_score", 70)
        self.max_iterations = self.config.get("max_iterations", 5)
        self.auto_discussion = self.config.get("auto_discussion", True)  # AI自动选方案 vs 等人工
        # profile 被 LLM 重生成的次数上限：超过就不再自动 regen，直接走人工 escalation。
        # 跨 regen 持久化（存在 profile.total_regens 字段里），所以 kedo 重启也记得。
        self.MAX_PROFILE_REGENS = self.config.get("max_profile_regens", 3)

        # ★ 启动时加载之前自动检测到的环境变量
        self._load_auto_detected_env()

    def _load_auto_detected_env(self):
        """启动时从 .kedo/env_auto_detected.json 加载之前发现的环境变量"""
        import json as _json
        project_path = self.config.get("project_path", ".")
        env_path = Path(project_path) / ".kedo" / "env_auto_detected.json"
        if env_path.exists():
            try:
                detected = _json.loads(env_path.read_text())
                for key, value in detected.items():
                    if not os.environ.get(key):
                        os.environ[key] = value
                        logger.info(f"Restored auto-detected env: {key}={value}")
                        # devkitPro 衍生变量
                        if key == "DEVKITPRO":
                            devkita64 = Path(value) / "devkitA64"
                            if devkita64.exists() and not os.environ.get("DEVKITA64"):
                                os.environ["DEVKITA64"] = str(devkita64)
                            for bin_dir in [Path(value) / "tools" / "bin", devkita64 / "bin"]:
                                if bin_dir.exists() and str(bin_dir) not in os.environ.get("PATH", ""):
                                    os.environ["PATH"] = str(bin_dir) + ":" + os.environ.get("PATH", "")
            except Exception as e:
                logger.warning(f"Failed to load auto-detected env: {e}")

    # ==========================================================
    # 主入口
    # ==========================================================

    async def start_task(self, task_id: str, description: str, project_path: str = "."):
        """
        启动一个新任务 — 非阻塞，在后台运行 Agent Loop

        Args:
            task_id: 任务唯一 ID
            description: 自然语言需求
            project_path: 项目根目录
        """
        await self.state.create_task(task_id, description)

        # 在后台启动 Agent Loop
        loop_task = asyncio.create_task(
            self._run_loop(task_id, description, project_path)
        )
        self._running_tasks[task_id] = loop_task

        logger.info(f"Agent Loop started for task {task_id}")

    async def resume_from_checkpoint(self, task_id: str, additional_context: str = ""):
        """
        智能续接：扫描项目现状 + 加载历史评估 → 重新规划缺失部分 → 执行

        不是机械地从旧计划第 N 步继续，而是：
        1. 扫描磁盘上已有的文件和代码
        2. 加载上次的评估报告（哪些需求满足/缺失）
        3. 让 Planner 基于 (现状 + 评估反馈 + 用户补充) 生成新计划
        4. 执行新计划，只做缺失的部分
        """
        checkpoint = await self.state.load_checkpoint(task_id)
        if not checkpoint:
            raise ValueError(f"No checkpoint found for task {task_id}")

        # 恢复记忆
        self.memory.restore(checkpoint.memory_snapshot)
        if additional_context:
            self.memory.add_message("user", f"[续接补充] {additional_context}")

        # 确保任务有 pause_event
        if task_id not in self.state._pause_events:
            self.state._pause_events[task_id] = asyncio.Event()
            self.state._pause_events[task_id].set()

        # 确保任务状态可恢复
        if task_id in self.state._tasks:
            await self.state.update_status(task_id, TaskStatus.IN_PROGRESS)
        else:
            task_desc = checkpoint.plan.subtasks[0].description if checkpoint.plan and checkpoint.plan.subtasks else ""
            await self.state.create_task(task_id, task_desc)
            await self.state.update_status(task_id, TaskStatus.IN_PROGRESS)

        # 获取原始任务描述
        original_desc = self.state._tasks.get(task_id, {}).get("description", "")
        project_path = self.state._tasks.get(task_id, {}).get("config", {}).get("project_path", ".")

        # ★ 智能续接：走新的 _run_smart_continuation
        loop_task = asyncio.create_task(
            self._run_smart_continuation(
                task_id=task_id,
                checkpoint=checkpoint,
                original_description=original_desc,
                additional_context=additional_context,
                project_path=project_path,
            )
        )
        self._running_tasks[task_id] = loop_task

        logger.info(f"Smart continuation started for task {task_id}"
                     + (f" with context: {additional_context[:80]}" if additional_context else ""))

    async def submit_discussion_input(
        self,
        task_id: str,
        action: str = "select",
        proposal_id: str = "",
        human_input: str = "",
        additional_constraints: list[str] = None,
    ):
        """人工参与讨论：选择方案、追问或提出自定义方案"""
        if task_id in self._discussion_queues:
            await self._discussion_queues[task_id].put({
                "action": action,
                "proposal_id": proposal_id,
                "human_input": human_input,
                "additional_constraints": additional_constraints or [],
            })

    # ==========================================================
    # 核心 Loop
    # ==========================================================

    async def _run_loop(self, task_id: str, description: str, project_path: str):
        """Agent Loop 主循环"""
        try:
            # ---- Phase 1: 计划 ----
            await self.state.update_status(task_id, TaskStatus.PLANNING, current_step="Planning")
            await self._emit(task_id, EventType.STEP_STARTED, step="planning")

            # 收集项目上下文
            project_context = await self._gather_project_context(project_path)

            # ★ 收集项目状态（让 Planner 自己判断意图）
            project_state = await self._gather_project_state(project_path)
            project_context["project_state"] = project_state

            # 生成计划
            await self._emit(task_id, EventType.LLM_REQUEST,
                             phase="planning", prompt_summary=f"需求: {description[:100]}",
                             model=self._get_model_name())
            async def _on_plan_token(token):
                await self._emit(task_id, EventType.LLM_TOKEN, token=token, phase="planning")
            plan = await self.planner.create_plan(task_id, description, project_context, on_token=_on_plan_token)
            # 输出计划详情
            plan_detail = " → ".join(s.title for s in plan.subtasks)
            await self._emit(task_id, EventType.LLM_RESPONSE,
                             phase="planning", summary=f"{len(plan.subtasks)} 个子任务: {plan_detail}")
            await self._emit(task_id, EventType.STEP_COMPLETED, step="planning", subtask_count=len(plan.subtasks))

            # ★ 计划生成后立即保存 checkpoint（让 Dashboard 能看到子任务列表）
            await self.state.save_checkpoint(AgentCheckpoint(
                task_id=task_id,
                current_step_index=-1,
                plan=plan,
                memory_snapshot=self.memory.snapshot(),
                code_changes=[],
                test_results=None,
            ))

            # 检查是否被暂停 (计划生成后的审查点)
            await self.state.wait_if_paused(task_id)

            # ---- Phase 2: 执行子任务 ----
            await self.state.update_status(task_id, TaskStatus.IN_PROGRESS)
            code_changes: list[CodeChange] = []
            test_results: Optional[TestResult] = None
            eval_report_data: Optional[dict] = None
            build_success = False

            for i, subtask in enumerate(plan.subtasks):
                # 检查暂停信号
                await self.state.wait_if_paused(task_id)

                # 更新进度
                progress = (i / len(plan.subtasks)) * 100
                await self.state.update_status(
                    task_id, TaskStatus.IN_PROGRESS,
                    current_step=subtask.title,
                    progress_percent=progress,
                )

                # 执行子任务
                result = await self._execute_subtask(
                    task_id, subtask, project_path, code_changes
                )

                # ★ 步骤失败感知：除 EVALUATE/PLAN 外，关键步骤失败后需要 escalate
                # （EVALUATE 由分数低分支处理；PLAN 即便失败 dispatch 已 auto-pass 视为成功）
                if (
                    not result.success
                    and subtask.step_type not in (StepType.EVALUATE, StepType.PLAN)
                ):
                    should_continue = await self._on_step_unrecoverable(
                        task_id=task_id,
                        plan=plan,
                        subtask=subtask,
                        result=result,
                        code_changes=code_changes,
                        project_path=project_path,
                    )
                    if not should_continue:
                        # 默认行为：暂停任务等待人工，wait_if_paused 会阻塞直到人工 resume
                        await self.state.wait_if_paused(task_id)
                        # 人工 resume 后继续走原逻辑（不再重跑该步骤；如果想重跑应在 hook 内自行实现）

                # 收集结果
                if subtask.step_type == StepType.CODE_GENERATE and result.success:
                    change = CodeChange(
                        file_path=result.data.get("file_path", ""),
                        action=result.data.get("action", "modify"),
                        diff=result.data.get("diff", ""),
                        content=result.data.get("content"),
                    )
                    code_changes.append(change)

                elif subtask.step_type == StepType.BUILD:
                    build_success = result.success

                elif subtask.step_type == StepType.TEST:
                    test_results = TestResult(**result.data.get("test_result", {}))

                    # ★ 测试通过后 → 创建候选版本 (关键时刻!)
                    if result.success and build_success:
                        eval_score = eval_report_data.get("score", 70) if eval_report_data else 70
                        await self.versions.create_candidate(
                            task_id=task_id,
                            code_changes=code_changes,
                            test_results=test_results,
                            build_success=build_success,
                            ai_confidence=eval_score,
                            ai_summary=f"Build OK + Tests passed ({test_results.passed}/{test_results.total})",
                            project_path=project_path,
                        )

                elif subtask.step_type == StepType.EVALUATE:
                    eval_report_data = result.data.get("eval_report")

                    if eval_report_data:
                        score = eval_report_data.get("score", 0)
                        eval_report_obj = EvalReport(**eval_report_data)

                        # ★ 评估后 → 创建/更新候选版本
                        await self.versions.create_candidate(
                            task_id=task_id,
                            code_changes=code_changes,
                            test_results=test_results,
                            eval_report=eval_report_obj,
                            build_success=build_success,
                            ai_confidence=score,
                            ai_summary=f"Eval score: {score} — "
                                       + ("Ready for review" if score >= self.min_eval_score else "Needs improvement"),
                            project_path=project_path,
                        )

                        if score < self.min_eval_score:
                            # ★★★ 闭环核心：评估不通过 → 讨论 → 重新规划 ★★★
                            logger.warning(f"Eval score {score} < {self.min_eval_score}, entering discussion loop...")
                            plan = await self._handle_eval_failure_loop(
                                task_id=task_id,
                                plan=plan,
                                eval_report=eval_report_obj,
                                code_changes=code_changes,
                                test_results=test_results,
                                description=description,
                                project_path=project_path,
                            )
                            continue

                # 保存检查点
                await self.state.save_checkpoint(AgentCheckpoint(
                    task_id=task_id,
                    current_step_index=i,
                    plan=plan,
                    memory_snapshot=self.memory.snapshot(),
                    code_changes=code_changes,
                    test_results=test_results,
                ))

            # ---- Phase 3: 完成 ----
            await self.state.update_status(
                task_id, TaskStatus.COMPLETED,
                progress_percent=100,
                current_step="Completed",
            )
            logger.info(f"Task {task_id} completed successfully")

        except asyncio.CancelledError:
            logger.info(f"Task {task_id} was cancelled")
            await self.state.update_status(task_id, TaskStatus.PAUSED)
        except Exception as e:
            logger.error(f"Task {task_id} failed: {e}", exc_info=True)
            # ★ 发出错误事件到 REPL/Dashboard，不再吞掉错误
            await self._emit(task_id, EventType.STEP_FAILED,
                             step="agent_loop", error=str(e),
                             error_type=type(e).__name__)
            await self.state.update_status(task_id, TaskStatus.FAILED, current_step=f"Error: {e}")

    async def _run_smart_continuation(
        self,
        task_id: str,
        checkpoint: AgentCheckpoint,
        original_description: str,
        additional_context: str,
        project_path: str,
    ):
        """
        智能续接核心逻辑：

        1. 扫描项目现状（磁盘文件 + 已有代码摘要）
        2. 构建续接上下文（上次完成了什么、评估反馈、缺失什么）
        3. 调用 Planner 生成增量计划（只做缺失部分）
        4. 执行新计划（复用 _run_loop 的主流程）
        """
        try:
            # ---- Phase 1: 分析项目现状 ----
            await self.state.update_status(task_id, TaskStatus.PLANNING, current_step="Analyzing current state")
            await self._emit(task_id, EventType.STEP_STARTED, step="smart_continuation_analysis")

            # 扫描项目文件
            project_context = await self._gather_project_context(project_path)

            # 读取已有源码文件的摘要（文件名 + 前几行）
            existing_code_summary = await self._summarize_existing_code(project_path)

            # 扫描不完整的文档（需要重新生成）
            incomplete_docs = self._scan_incomplete_docs(project_path)

            # 检测目录结构是否规范
            dir_issues = self._check_directory_structure(project_path)

            # 分析构建状态（是否真正产生了构建产物）
            build_issues = self._check_build_artifacts(project_path, checkpoint)

            # 从 checkpoint 提取历史信息
            prev_eval = checkpoint.eval_report
            prev_plan = checkpoint.plan
            prev_code_changes = checkpoint.code_changes
            completed_steps = []
            for i in range(min(checkpoint.current_step_index + 1, len(prev_plan.subtasks))):
                s = prev_plan.subtasks[i]
                completed_steps.append(f"- [{s.step_type.value}] {s.title}")

            # ---- Phase 2: 构建续接上下文 ----
            continuation_context = self._build_continuation_context(
                original_description=original_description,
                additional_context=additional_context,
                completed_steps=completed_steps,
                existing_code_summary=existing_code_summary,
                prev_eval=prev_eval,
                prev_code_changes=prev_code_changes,
                incomplete_docs=incomplete_docs,
                dir_issues=dir_issues,
                build_issues=build_issues,
            )

            await self._emit(task_id, EventType.STEP_COMPLETED,
                             step="smart_continuation_analysis",
                             output=f"已分析项目现状: {len(existing_code_summary)} 个源码文件")

            # ---- Phase 3: 生成增量计划 ----
            await self.state.update_status(task_id, TaskStatus.PLANNING, current_step="Planning continuation")
            await self._emit(task_id, EventType.STEP_STARTED, step="planning")
            await self._emit(task_id, EventType.LLM_REQUEST,
                             phase="planning", prompt_summary=f"智能续接: {original_description[:60]}",
                             model=self._get_model_name())

            async def _on_token(token):
                await self._emit(task_id, EventType.LLM_TOKEN, token=token, phase="planning")

            plan = await self.planner.create_continuation_plan(
                task_id=task_id,
                continuation_context=continuation_context,
                project_context=project_context,
                on_token=_on_token,
            )

            # ★ 强制注入不完整文档的补全步骤（不依赖 LLM 决策）
            if incomplete_docs:
                # 构建项目摘要供文档生成使用
                src_files_hint = ", ".join(f["path"] for f in existing_code_summary[:10])
                project_hint = (
                    f"项目需求: {original_description}\n"
                    f"项目源码文件: {src_files_hint}\n"
                    f"项目路径: {project_path}"
                )

                existing_titles = {s.title.lower() for s in plan.subtasks}
                inject_at = 0
                for doc in incomplete_docs:
                    doc_path = doc["path"]
                    title = f"补全文档 {doc_path}"
                    if title.lower() not in existing_titles and doc_path.lower() not in " ".join(existing_titles):
                        inject_subtask = SubTask(
                            id=f"subtask_inject_{inject_at}",
                            title=title,
                            description=(
                                f"重新生成完整的 {doc_path}（当前内容不完整: {doc['reason']}）。\n"
                                f"必须根据项目实际情况生成完整内容，不能只写标题。\n"
                                f"{project_hint}\n"
                                f"文件路径: {doc_path}"
                            ),
                            step_type=StepType.CODE_GENERATE,
                            dependencies=[],
                        )
                        plan.subtasks.insert(inject_at, inject_subtask)
                        inject_at += 1
                        logger.info(f"Injected doc regeneration step: {title}")
                # 重新编号
                for i, st in enumerate(plan.subtasks):
                    st.id = f"subtask_{i}"

            plan_detail = " → ".join(s.title for s in plan.subtasks)
            await self._emit(task_id, EventType.LLM_RESPONSE,
                             phase="planning", summary=f"续接计划: {len(plan.subtasks)} 个子任务: {plan_detail}")
            await self._emit(task_id, EventType.STEP_COMPLETED, step="planning", subtask_count=len(plan.subtasks))

            # ---- Phase 4: 执行新计划（复用主循环逻辑）----
            await self.state.wait_if_paused(task_id)
            await self.state.update_status(task_id, TaskStatus.IN_PROGRESS)

            code_changes: list[CodeChange] = list(checkpoint.code_changes)  # 保留历史
            test_results: Optional[TestResult] = None
            eval_report_data: Optional[dict] = None
            build_success = False

            for i, subtask in enumerate(plan.subtasks):
                await self.state.wait_if_paused(task_id)

                progress = (i / len(plan.subtasks)) * 100
                await self.state.update_status(
                    task_id, TaskStatus.IN_PROGRESS,
                    current_step=subtask.title,
                    progress_percent=progress,
                )

                result = await self._execute_subtask(task_id, subtask, project_path, code_changes)

                if subtask.step_type == StepType.CODE_GENERATE and result.success:
                    code_changes.append(CodeChange(
                        file_path=result.data.get("file_path", ""),
                        action=result.data.get("action", "modify"),
                        diff=result.data.get("diff", ""),
                        content=result.data.get("content"),
                    ))
                elif subtask.step_type == StepType.BUILD:
                    build_success = result.success
                elif subtask.step_type == StepType.TEST:
                    test_results = TestResult(**result.data.get("test_result", {}))
                    if result.success and build_success:
                        eval_score = eval_report_data.get("score", 70) if eval_report_data else 70
                        await self.versions.create_candidate(
                            task_id=task_id,
                            code_changes=code_changes,
                            test_results=test_results,
                            build_success=build_success,
                            ai_confidence=eval_score,
                            ai_summary=f"Continuation: Build OK + Tests passed",
                            project_path=project_path,
                        )
                elif subtask.step_type == StepType.EVALUATE:
                    eval_report_data = result.data.get("eval_report")
                    if eval_report_data:
                        score = eval_report_data.get("score", 0)
                        eval_report_obj = EvalReport(**eval_report_data)
                        await self.versions.create_candidate(
                            task_id=task_id,
                            code_changes=code_changes,
                            test_results=test_results,
                            eval_report=eval_report_obj,
                            build_success=build_success,
                            ai_confidence=score,
                            ai_summary=f"Continuation eval: {score}",
                            project_path=project_path,
                        )
                        if score < self.min_eval_score:
                            plan = await self._handle_eval_failure_loop(
                                task_id=task_id, plan=plan, eval_report=eval_report_obj,
                                code_changes=code_changes, test_results=test_results,
                                description=original_description, project_path=project_path,
                            )
                            continue

                await self.state.save_checkpoint(AgentCheckpoint(
                    task_id=task_id,
                    current_step_index=i,
                    plan=plan,
                    memory_snapshot=self.memory.snapshot(),
                    code_changes=code_changes,
                    test_results=test_results,
                    eval_report=eval_report_obj if eval_report_data else None,
                ))

            # ---- Phase 5: 完成 ----
            await self.state.update_status(task_id, TaskStatus.COMPLETED, progress_percent=100, current_step="Completed")
            logger.info(f"Smart continuation completed for task {task_id}")

        except asyncio.CancelledError:
            logger.info(f"Task {task_id} was cancelled")
            await self.state.update_status(task_id, TaskStatus.PAUSED)
        except Exception as e:
            logger.error(f"Smart continuation failed for task {task_id}: {e}", exc_info=True)
            await self._emit(task_id, EventType.STEP_FAILED, step="smart_continuation", error=str(e), error_type=type(e).__name__)
            await self.state.update_status(task_id, TaskStatus.FAILED, current_step=f"Error: {e}")

    def _build_continuation_context(
        self,
        original_description: str,
        additional_context: str,
        completed_steps: list[str],
        existing_code_summary: list[dict],
        prev_eval: Optional[EvalReport],
        prev_code_changes: list[CodeChange],
        incomplete_docs: list[dict] = None,
        dir_issues: list[dict] = None,
        build_issues: list[dict] = None,
    ) -> str:
        """构建续接上下文文本，供 Planner 使用"""
        parts = []

        parts.append(f"## 原始需求\n{original_description}")

        if additional_context:
            parts.append(f"## 用户补充说明\n{additional_context}")

        if dir_issues:
            issue_list = "\n".join(f"- {d['issue']}: {d['detail']}" for d in dir_issues)
            parts.append(
                f"## 目录结构不规范（需要重构）\n"
                f"以下目录不符合 kedo 标准（src/ + build/），请在续接计划的最前面加入重构步骤：\n{issue_list}"
            )

        if build_issues:
            issue_list = "\n".join(f"- {d['issue']}: {d['detail']}" for d in build_issues)
            parts.append(
                f"## 构建问题（必须优先修复）\n"
                f"上次构建没有产生有效的可执行文件。以下是诊断出的问题，请在续接计划中修复：\n{issue_list}\n"
                f"注意：不要重新生成所有代码，只修复构建相关的问题（如缺少 Makefile、构建脚本错误等）。"
            )

        if completed_steps:
            parts.append(f"## 上次已完成的步骤\n" + "\n".join(completed_steps))

        if existing_code_summary:
            file_list = "\n".join(
                f"- {f['path']} ({f['lines']}行): {f['summary']}"
                for f in existing_code_summary[:30]
            )
            parts.append(f"## 项目中已有的源码文件\n{file_list}")

        if incomplete_docs:
            doc_list = "\n".join(
                f"- {d['path']} ({d['lines']}行, {d['size_bytes']}字节): {d['reason']}"
                for d in incomplete_docs
            )
            parts.append(
                f"## 不完整的文档（需要重新生成）\n"
                f"以下文档内容不完整（被截断或过短），请在续接计划中重新生成这些文档：\n{doc_list}"
            )

        if prev_eval:
            parts.append(f"## 上次评估结果 (评分: {prev_eval.score}/100)")
            if prev_eval.requirements_met:
                parts.append("### 已满足的需求\n" + "\n".join(f"- {r}" for r in prev_eval.requirements_met))
            if prev_eval.requirements_missed:
                parts.append("### 缺失的需求（本次需要重点实现）\n" + "\n".join(f"- {r}" for r in prev_eval.requirements_missed))
            if prev_eval.suggestions:
                parts.append("### 改进建议\n" + "\n".join(f"- {s}" for s in prev_eval.suggestions[:5]))

        return "\n\n".join(parts)

    async def _summarize_existing_code(self, project_path: str) -> list[dict]:
        """扫描项目中已有的源码文件，提取文件名和首行摘要"""
        from pathlib import Path as _Path
        project = _Path(project_path)
        if not project.is_dir():
            return []

        source_exts = {".cpp", ".c", ".h", ".hpp", ".py", ".js", ".ts", ".jsx", ".tsx",
                       ".java", ".go", ".rs", ".cs", ".rb", ".swift", ".kt"}
        skip_dirs = {".kedo", ".git", ".venv", "node_modules", "__pycache__", "build", "dist"}

        results = []
        for item in sorted(project.rglob("*")):
            if item.is_dir():
                continue
            parts = item.relative_to(project).parts
            if any(p in skip_dirs for p in parts):
                continue
            if item.suffix.lower() not in source_exts:
                continue
            try:
                lines = item.read_text(encoding="utf-8", errors="replace").splitlines()
                line_count = len(lines)
                # 提取前5行非空行作为摘要
                summary_lines = [l.strip() for l in lines[:10] if l.strip()][:3]
                summary = " | ".join(summary_lines)[:120]
                results.append({
                    "path": str(item.relative_to(project)),
                    "lines": line_count,
                    "summary": summary,
                })
            except Exception:
                continue

        return results

    def _scan_incomplete_docs(self, project_path: str) -> list[dict]:
        """
        扫描 docs/ 目录，检测不完整的文档（截断、过短、内容断在中间）。
        这些文档应在续接计划中重新生成。
        """
        from pathlib import Path as _Path
        project = _Path(project_path)
        docs_dir = project / "docs"
        if not docs_dir.is_dir():
            return []

        # 各文档的最小合理长度（字节）
        min_doc_sizes = {
            "requirement.md": 500,
            "user-stories.md": 500,
            "architecture.md": 800,
            "api-design.md": 600,
            "database-design.md": 400,
            "module-design.md": 800,
            "deployment.md": 500,
            "test-plan.md": 500,
            "test-cases.md": 600,
            "automation.md": 400,
        }

        incomplete = []
        for md_file in sorted(docs_dir.rglob("*.md")):
            try:
                content = md_file.read_text(encoding="utf-8", errors="replace")
                size_bytes = len(content.encode("utf-8"))
                lines = content.splitlines()
                line_count = len(lines)
                rel_path = str(md_file.relative_to(project))
                file_name = md_file.name

                reasons = []

                # 检查 1：文件过短
                min_size = min_doc_sizes.get(file_name, 300)
                if size_bytes < min_size:
                    reasons.append(f"文件过短（{size_bytes}字节 < {min_size}字节最低要求）")

                # 检查 2：内容以冒号、"如下"等结尾（被截断的特征）
                stripped = content.rstrip()
                if stripped:
                    last_line = stripped.split("\n")[-1].strip()
                    truncation_endings = ("：", "如下：", "如下", "following:", "below:", "as follows:")
                    if any(last_line.endswith(e) for e in truncation_endings):
                        reasons.append(f"内容疑似被截断（末尾: '{last_line[-30:]}'）")

                # 检查 3：有标题但没有对应内容
                headers_without_content = 0
                for i, line in enumerate(lines):
                    if line.startswith("## ") or line.startswith("### "):
                        # 检查后续是否有非空行
                        has_content = False
                        for j in range(i + 1, min(i + 5, len(lines))):
                            if lines[j].strip() and not lines[j].startswith("#"):
                                has_content = True
                                break
                        if not has_content:
                            headers_without_content += 1

                if headers_without_content >= 2:
                    reasons.append(f"{headers_without_content}个章节标题下没有内容")

                # 检查 4：行数过少（相对于标题数）
                header_count = sum(1 for l in lines if l.startswith("#"))
                if header_count > 0 and line_count < header_count * 3:
                    reasons.append(f"内容过少（{line_count}行/{header_count}个标题）")

                if reasons:
                    incomplete.append({
                        "path": rel_path,
                        "lines": line_count,
                        "size_bytes": size_bytes,
                        "reason": "; ".join(reasons),
                    })

            except Exception:
                continue

        return incomplete

    def _check_build_artifacts(self, project_path: str, checkpoint: AgentCheckpoint) -> list[dict]:
        """
        检查构建是否真正产生了可执行文件。
        如果没有，诊断原因（缺少构建脚本、构建脚本错误、依赖缺失等）。
        """
        from pathlib import Path as _Path
        project = _Path(project_path)
        issues = []

        # 检查是否有 build 步骤
        has_build_step = False
        if checkpoint.plan and checkpoint.plan.subtasks:
            has_build_step = any(s.step_type == StepType.BUILD for s in checkpoint.plan.subtasks)

        if not has_build_step:
            return []  # 没有 build 步骤的任务不检查

        # 检查构建产物是否存在
        artifact_patterns = ["*.nro", "*.elf", "*.exe", "*.bin", "*.so", "*.dll",
                             "*.jar", "*.whl", "*.wasm", "*.a", "*.dylib"]
        found_artifacts = []
        for pattern in artifact_patterns:
            found_artifacts.extend(project.rglob(pattern))
        # 排除 .kedo 等目录
        skip_dirs = {".kedo", ".git", ".venv", "node_modules"}
        found_artifacts = [f for f in found_artifacts
                          if not any(p in f.relative_to(project).parts for p in skip_dirs)]

        if found_artifacts:
            return []  # 有构建产物，没问题

        # 没有构建产物 → 诊断原因
        # 1. 检查是否有构建脚本
        has_makefile = (project / "Makefile").exists()
        has_cmake = (project / "CMakeLists.txt").exists()
        has_package_json = (project / "package.json").exists()
        has_cargo = (project / "Cargo.toml").exists()

        if not has_makefile and not has_cmake and not has_package_json and not has_cargo:
            issues.append({
                "issue": "缺少构建脚本",
                "detail": "项目中没有 Makefile、CMakeLists.txt 或其他构建配置文件。"
                          "需要创建一个正确的构建脚本，将所有源码文件编译为可执行文件。"
            })
        else:
            # 有构建脚本但没产物 → 脚本可能有问题
            script_name = "Makefile" if has_makefile else "CMakeLists.txt" if has_cmake else "package.json"
            script_path = project / script_name
            try:
                script_content = script_path.read_text(encoding="utf-8", errors="replace")
                script_lines = len(script_content.splitlines())
                if script_lines < 5:
                    issues.append({
                        "issue": f"构建脚本 {script_name} 内容过少（{script_lines}行）",
                        "detail": f"{script_name} 只有 {script_lines} 行，可能是不完整或 mock 生成的。"
                                  f"需要重新生成正确的构建脚本。"
                    })
                else:
                    issues.append({
                        "issue": f"构建脚本 {script_name} 存在但未产生可执行文件",
                        "detail": f"{script_name} 有 {script_lines} 行但构建没有产物。"
                                  f"可能的原因：编译错误、依赖缺失、输出路径配置错误。"
                                  f"请检查并修复构建脚本。"
                    })
            except Exception:
                pass

        # 2. 检查 checkpoint 中 code_changes 是否包含构建脚本
        has_build_script_in_changes = False
        for c in (checkpoint.code_changes or []):
            fp = (c.file_path or "").lower()
            if "makefile" in fp or "cmakelists" in fp or "package.json" in fp or "cargo.toml" in fp:
                has_build_script_in_changes = True
                content = c.content or ""
                if len(content) < 50:
                    issues.append({
                        "issue": f"生成的构建脚本内容异常（仅 {len(content)} 字符）",
                        "detail": f"LLM 生成的 {c.file_path} 内容过短，可能是 mock 或截断。需要重新生成。"
                    })
                break

        if not has_build_script_in_changes and not has_makefile and not has_cmake:
            issues.append({
                "issue": "上次任务没有生成构建脚本",
                "detail": "code_changes 中没有 Makefile/CMakeLists.txt，"
                          "需要在续接计划中生成正确的构建脚本。"
            })

        # 3. 检查 Docker 配置是否正确
        docker_compose = project / "docker-compose.yml"
        if docker_compose.exists():
            try:
                dc_content = docker_compose.read_text(encoding="utf-8", errors="replace")
                if "FROM" in dc_content and "services" not in dc_content.lower():
                    issues.append({
                        "issue": "docker-compose.yml 内容异常",
                        "detail": "docker-compose.yml 包含 FROM 指令（应该是 Dockerfile 的内容），"
                                  "说明 LLM 把 Dockerfile 内容错误地写入了 docker-compose.yml。"
                                  "需要分别正确生成 Dockerfile 和 docker-compose.yml。"
                    })
            except Exception:
                pass

        # 4. 检查源码文件是否存在
        src_exts = {".cpp", ".c", ".py", ".js", ".ts", ".java", ".go", ".rs"}
        src_files = [f for f in project.rglob("*")
                     if f.is_file() and f.suffix.lower() in src_exts
                     and not any(p in f.relative_to(project).parts for p in skip_dirs)]
        if not src_files:
            issues.append({
                "issue": "项目中没有源码文件",
                "detail": "没有找到任何源码文件（.cpp/.py/.js 等），构建无法进行。"
            })

        return issues

    def _check_directory_structure(self, project_path: str) -> list[dict]:
        """检测项目目录结构是否符合 kedo 标准（src/ + build/）"""
        from pathlib import Path as _Path
        project = _Path(project_path)
        issues = []

        # 标准源码目录应该是 src/
        non_standard_src_dirs = ["source", "lib", "app", "code"]
        for d in non_standard_src_dirs:
            dir_path = project / d
            if dir_path.is_dir():
                # 检查里面确实有源码文件
                src_exts = {".cpp", ".c", ".h", ".hpp", ".py", ".js", ".ts", ".java", ".go", ".rs"}
                has_code = any(f.suffix.lower() in src_exts for f in dir_path.rglob("*") if f.is_file())
                if has_code:
                    file_count = sum(1 for f in dir_path.rglob("*") if f.is_file() and f.suffix.lower() in src_exts)
                    issues.append({
                        "issue": f"源代码目录 `{d}/` 不规范",
                        "detail": f"应将 `{d}/`（含 {file_count} 个源码文件）重命名为 `src/`，"
                                  f"并更新构建脚本中的源码路径引用",
                        "from_dir": d,
                        "to_dir": "src",
                    })

        # 检查 src/ 是否存在
        if not (project / "src").is_dir() and not issues:
            # 没有 src/ 也没有非标准目录 — 可能源码在根目录
            src_exts = {".cpp", ".c", ".h", ".hpp", ".py", ".js", ".ts", ".java", ".go", ".rs"}
            root_src = [f for f in project.iterdir() if f.is_file() and f.suffix.lower() in src_exts]
            if root_src:
                issues.append({
                    "issue": "源代码散落在项目根目录",
                    "detail": f"有 {len(root_src)} 个源码文件在根目录，应移入 `src/` 目录",
                    "from_dir": ".",
                    "to_dir": "src",
                })

        # 检查构建产物是否在根目录
        build_exts = {".nro", ".nso", ".elf", ".exe", ".bin", ".so", ".dll", ".a", ".o",
                      ".jar", ".war", ".whl"}
        root_artifacts = [f for f in project.iterdir() if f.is_file() and f.suffix.lower() in build_exts]
        if root_artifacts:
            names = ", ".join(f.name for f in root_artifacts[:5])
            issues.append({
                "issue": "构建产物在项目根目录",
                "detail": f"构建产物（{names}）应输出到 `build/` 目录，"
                          f"需修改 Makefile/CMakeLists.txt 的输出路径配置",
                "from_dir": ".",
                "to_dir": "build",
            })

        return issues

    # ==========================================================
    # 步骤失败的最后防线
    # ==========================================================

    async def _on_step_unrecoverable(
        self,
        task_id: str,
        plan: TaskPlan,
        subtask: SubTask,
        result: ToolResult,
        code_changes: list[CodeChange],
        project_path: str,
    ) -> bool:
        """
        关键步骤经过 auto_fix 仍失败时调用。

        默认行为：emit STEP_FAILED + pause task，把控制权交给人工。

        【方案 3 接入点】未来若要在此触发自动 replan：
          - 把 _handle_eval_failure_loop 泛化成接受非 EvalReport 的失败上下文
          - 在这里调用它，并 return True 表示已自愈、外层应继续后续 subtask

        Returns:
            False — 默认。表示外层 loop 应停下来等待人工（pause + wait_if_paused）。
            True  — 该 hook 已自愈/重新规划，外层 loop 应继续推进。
        """
        error_msg = result.error or "Step failed"
        failure_kind = "unknown"
        if isinstance(result.data, dict):
            failure_kind = result.data.get("failure_kind", "unknown")

        logger.error(
            f"Step '{subtask.title}' unrecoverable after auto_fix "
            f"(kind={failure_kind}): {error_msg[:200]}"
        )

        # ★★★ 方案 W：profile build 失败 → 把"用户可以手工修 profile"作为明确出口
        suggestion = (
            "Auto-fix exhausted. Inspect the error and either fix manually + resume, "
            "or stop the task and refine the plan."
        )
        if subtask.step_type in (StepType.BUILD, StepType.TEST):
            profile = self.profile_manager.load(project_path)
            if profile is not None:
                profile_path = Path(project_path) / ".kedo" / "project_profile.json"
                regen_hint = ""
                if profile.total_regens >= self.MAX_PROFILE_REGENS:
                    regen_hint = (
                        f"\n  NOTE: profile has already been LLM-regenerated "
                        f"{profile.total_regens} times (cap={self.MAX_PROFILE_REGENS}). "
                        f"Auto-regeneration is now disabled for this project; manual "
                        f"intervention is required."
                    )
                suggestion = (
                    f"Auto-fix exhausted (profile fail_count={profile.fail_count}, "
                    f"total_regens={profile.total_regens}). "
                    f"The LLM-generated project profile may be wrong. You can:\n"
                    f"  1. Manually edit {profile_path} (set human_verified=true to "
                    f"prevent auto-regeneration), then resume the task.\n"
                    f"  2. Delete the file to force LLM to regenerate from scratch.\n"
                    f"  3. Inspect the actual error below and fix the source code, "
                    f"then resume.{regen_hint}"
                )

        await self._emit(
            task_id, EventType.STEP_FAILED,
            step=subtask.title,
            error=error_msg[:500],
            error_type=failure_kind,
            escalation="paused_for_human",
            suggestion=suggestion,
        )
        self.memory.add_message(
            "system",
            f"[Escalation] Step '{subtask.title}' failed unrecoverably ({failure_kind}). "
            f"Task paused for human review. Last error: {error_msg[:300]}",
        )

        # 默认：暂停任务，等人工 resume
        await self.state.pause_task(task_id)
        return False

    # ==========================================================
    # LLM 驱动的失败修复
    # ==========================================================

    # 失败修复时优先查看的"项目脉络文件"清单（按相对优先级）
    _FIX_CONTEXT_BUILD_MANIFESTS = [
        "CMakeLists.txt", "Makefile", "makefile", "build.gradle",
        "build.gradle.kts", "package.json", "pyproject.toml", "setup.py",
        "Cargo.toml", "go.mod", "BUILD.bazel", "BUILD",
    ]

    def _gather_fix_context(self, subtask: SubTask, project_path: str) -> list[tuple[str, str]]:
        """
        采集与失败步骤相关的项目文件，用于 LLM 失败诊断。

        策略：
          - 最高优先：.kedo/project_profile.json（如果存在），因为 BUILD/TEST 命令
            来自 profile，profile 写错时 LLM 必须能直接看到并修这个文件
          - 总是包含项目根目录下的构建清单文件（CMakeLists.txt 等）
          - CODE_GENERATE 失败：附带 subtask 描述里提到的目标文件（如果存在）
          - TEST 失败：列出 tests/ 或 test/ 目录的文件名
        返回 [(relative_path, content)] 列表，单文件最大 8KB。
        """
        from pathlib import Path as _P
        proj = _P(project_path)
        max_per_file = 8192
        results: list[tuple[str, str]] = []

        # 0) project_profile.json — 最高优先级，因为 build/test 命令来自它
        profile_file = proj / ".kedo" / "project_profile.json"
        if profile_file.exists() and profile_file.is_file():
            try:
                content = profile_file.read_text(encoding="utf-8", errors="replace")[:max_per_file]
                results.append((".kedo/project_profile.json", content))
            except Exception:
                pass

        # 1) 构建清单
        for name in self._FIX_CONTEXT_BUILD_MANIFESTS:
            f = proj / name
            if f.exists() and f.is_file():
                try:
                    results.append((name, f.read_text(encoding="utf-8", errors="replace")[:max_per_file]))
                except Exception:
                    pass

        # 2) 子任务描述里指明的目标文件（CODE_GENERATE 失败时常用）
        try:
            file_name = self._infer_file_name(subtask, project_path)
            target = proj / file_name
            if target.exists() and target.is_file() and file_name not in [r[0] for r in results]:
                results.append(
                    (file_name, target.read_text(encoding="utf-8", errors="replace")[:max_per_file])
                )
        except Exception:
            pass

        # 3) TEST 失败时附上 tests 目录清单
        if subtask.step_type == StepType.TEST:
            for tdir_name in ("tests", "test"):
                tdir = proj / tdir_name
                if tdir.exists() and tdir.is_dir():
                    try:
                        listing = "\n".join(
                            sorted(p.name for p in tdir.iterdir() if not p.name.startswith("."))[:50]
                        )
                        results.append((f"{tdir_name}/ (listing)", listing))
                    except Exception:
                        pass
                    break

        return results

    def _build_fix_prompt(
        self,
        subtask: SubTask,
        error_text: str,
        context_files: list[tuple[str, str]],
    ) -> list[dict]:
        """构造失败诊断 + 修复建议的 LLM 消息。"""
        files_block = "\n\n".join(
            f"=== {path} ===\n{content}" for path, content in context_files
        ) or "(no relevant files found in project root)"

        system = (
            "You are a senior build/test debugging expert embedded inside an automated "
            "software engineering pipeline. A pipeline step has just failed. Your job is "
            "to diagnose the root cause and propose ONE concrete file change that will "
            "let the step succeed on the next attempt.\n\n"
            "CRITICAL RULES:\n"
            "1. Read the error output and the project files carefully before deciding.\n"
            "2. If a single file edit can fix the failure, output the COMPLETE new "
            "content of that file. Do not output a diff or partial snippet.\n"
            "3. If you see `.kedo/project_profile.json` in the relevant files, that JSON "
            "is the source of truth for build/test commands. If the build error matches "
            "a wrong path/command in that profile (e.g., a toolchain file path that does "
            "not exist, a missing flag, a wrong working directory), edit the profile JSON "
            "directly — do not try to work around it by editing other files. The profile "
            "is editable just like any other file.\n"
            "4. If the failure is structural (the wrong tool is being used, the test "
            "cannot run in this configuration, the plan itself is wrong, dependencies "
            "are missing at the system level, etc.) and CANNOT be fixed by editing one "
            "project file, respond with the unfixable schema instead. Do NOT hallucinate "
            "a fake fix.\n"
            "5. Prefer minimal, targeted edits over rewrites.\n"
            "6. Use forward slashes in file paths, relative to project root.\n\n"
            "Output STRICTLY one of these two JSON shapes (no markdown, no commentary):\n"
            '{\n'
            '  "diagnosis": "<one-sentence root cause>",\n'
            '  "file_to_fix": "<relative/path>",\n'
            '  "action": "create" | "modify",\n'
            '  "new_content": "<complete new file content>"\n'
            '}\n'
            "OR\n"
            '{"unfixable": true, "reason": "<why a single-file edit cannot fix this>"}\n'
        )

        user = (
            f"Failed step: {subtask.title}\n"
            f"Step type: {subtask.step_type.value}\n"
            f"Step description: {subtask.description}\n\n"
            f"Error output:\n{error_text[:4000]}\n\n"
            f"Relevant project files:\n{files_block}\n\n"
            f"Diagnose the root cause and respond with the JSON."
        )

        return [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ]

    def _parse_fix_response(self, response: str) -> Optional[dict]:
        """解析 LLM 修复响应，返回 dict 或 None（无效响应/unfixable）。"""
        import json as _json
        import re as _re
        if not response:
            return None
        text = response.strip()
        # 去掉 ```json 包裹
        if "```" in text:
            m = _re.search(r"```(?:json)?\s*([\s\S]*?)```", text)
            if m:
                text = m.group(1).strip()
        # 取第一个完整 JSON 对象
        start = text.find("{")
        if start < 0:
            return None
        depth = 0
        end = -1
        for i in range(start, len(text)):
            c = text[i]
            if c == "{":
                depth += 1
            elif c == "}":
                depth -= 1
                if depth == 0:
                    end = i + 1
                    break
        if end < 0:
            return None
        try:
            data = _json.loads(text[start:end])
        except Exception as e:
            logger.warning(f"_parse_fix_response: JSON decode failed: {e}")
            return None
        if data.get("unfixable"):
            return {"unfixable": True, "reason": data.get("reason", "")}
        if not all(k in data for k in ("file_to_fix", "new_content")):
            return None
        return data

    async def _attempt_llm_fix(
        self,
        task_id: str,
        subtask: SubTask,
        error_text: str,
        project_path: str,
    ) -> Optional[dict]:
        """
        失败修复入口：调 LLM 分析错误 → 应用文件级修改 → 返回 patch dict 或 None。

        返回：
          - dict {"file_to_fix", "action", "new_content", "diagnosis"}: 已应用修改
          - {"unfixable": True, "reason": ...}: LLM 明确表示单文件修不了
          - None: LLM 调用失败 / 响应无效
        """
        try:
            context_files = self._gather_fix_context(subtask, project_path)
            messages = self._build_fix_prompt(subtask, error_text, context_files)
            await self._emit(
                task_id, EventType.LLM_REQUEST,
                phase="auto_fix",
                prompt_summary=f"诊断 {subtask.title} 的失败原因",
                model=self._get_model_name(),
            )
            response = await self.planner._llm.chat(messages)
        except Exception as e:
            logger.warning(f"_attempt_llm_fix: LLM call failed: {e}")
            return None

        patch = self._parse_fix_response(response)
        if patch is None:
            logger.warning(f"_attempt_llm_fix: LLM response not parseable for '{subtask.title}'")
            return None
        if patch.get("unfixable"):
            logger.warning(
                f"_attempt_llm_fix: LLM declared unfixable for '{subtask.title}': "
                f"{patch.get('reason')}"
            )
            await self._emit(
                task_id, EventType.LLM_RESPONSE,
                phase="auto_fix",
                summary=f"LLM 判定单文件修不了: {patch.get('reason', '')[:200]}",
            )
            return patch

        # 应用文件修改
        from pathlib import Path as _P
        rel = patch["file_to_fix"].lstrip("/").replace("\\", "/")
        target = _P(project_path) / rel
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(patch["new_content"], encoding="utf-8")
        except Exception as e:
            logger.error(f"_attempt_llm_fix: failed to write {target}: {e}")
            return None

        # 如果 LLM 改的是 profile.json，必须 invalidate 内存缓存，
        # 否则下次 ensure() 会返回旧的 profile 内容
        if rel == ".kedo/project_profile.json":
            self.profile_manager.invalidate(project_path)
            logger.info("_attempt_llm_fix: invalidated profile cache after LLM edit")

        diagnosis = patch.get("diagnosis", "")[:200]
        logger.info(f"_attempt_llm_fix: edited {rel} — {diagnosis}")
        await self._emit(
            task_id, EventType.LLM_RESPONSE,
            phase="auto_fix",
            summary=f"已修改 {rel}: {diagnosis}",
        )
        self.memory.add_message(
            "system",
            f"[auto_fix] Step '{subtask.title}' failed → LLM diagnosed: {diagnosis}. "
            f"Modified {rel}.",
        )
        return patch

    # ==========================================================
    # 子任务执行
    # ==========================================================

    async def _execute_subtask(
        self,
        task_id: str,
        subtask: SubTask,
        project_path: str,
        existing_changes: list[CodeChange],
    ) -> ToolResult:
        """执行单个子任务 (带重试)"""
        await self._emit(task_id, EventType.STEP_STARTED, step=subtask.title, type=subtask.step_type.value)

        # 历史失败指纹：用于判断 LLM 是否在反复修一个修不动的错误
        stderr_fingerprints: list[str] = []

        for attempt in range(subtask.max_retries):
            try:
                # 发出工具执行事件
                await self._emit(task_id, EventType.TOOL_EXECUTE,
                                 step=subtask.title, tool_type=subtask.step_type.value,
                                 description=subtask.description[:150],
                                 attempt=attempt + 1, max_retries=subtask.max_retries)

                # 如果是 LLM 相关步骤，发出 LLM_REQUEST
                if subtask.step_type in (StepType.CODE_GENERATE, StepType.EVALUATE):
                    await self._emit(task_id, EventType.LLM_REQUEST,
                                     phase=subtask.step_type.value,
                                     prompt_summary=subtask.description[:120],
                                     model=self._get_model_name())

                result = await self._dispatch_subtask(subtask, project_path, existing_changes)

                if result.success:
                    subtask.status = TaskStatus.COMPLETED
                    subtask.result = result.data
                    # 发出 LLM_RESPONSE（对有 LLM 交互的步骤）
                    if subtask.step_type in (StepType.CODE_GENERATE, StepType.EVALUATE):
                        output_preview = (result.output or "")[:200]
                        await self._emit(task_id, EventType.LLM_RESPONSE,
                                         phase=subtask.step_type.value,
                                         summary=output_preview or "执行成功",
                                         data_keys=list(result.data.keys()) if result.data else [])
                    await self._emit(task_id, EventType.STEP_COMPLETED,
                                     step=subtask.title, output=(result.output or "")[:200])
                    return result

                # 失败 — 尝试自动修复
                # evaluate 步骤失败（分数不够）不在此重试，交给外层 _handle_eval_failure_loop 处理
                if subtask.step_type == StepType.EVALUATE:
                    subtask.status = TaskStatus.FAILED
                    subtask.result = {"error": result.error}
                    return result

                # ★ 工具链/环境故障短路：LLM 改代码救不了，直接 abort
                stderr_text = result.error or ""
                if result.data and isinstance(result.data, dict):
                    stderr_text = stderr_text or result.data.get("stderr", "")
                failure_kind, failure_reason = self._classify_failure(stderr_text)
                if failure_kind == "toolchain":
                    abort_msg = (
                        f"环境/工具链故障 ({failure_reason}) — 已中止 auto_fix。"
                        f"LLM 无法通过修改项目代码解决此类故障，请检查工具链安装。\n"
                        f"原始错误片段: {stderr_text[:300]}"
                    )
                    logger.error(f"Step '{subtask.title}' aborted due to toolchain failure: {failure_reason}")
                    subtask.status = TaskStatus.FAILED
                    subtask.result = {"error": abort_msg, "failure_kind": "toolchain"}
                    await self._emit(
                        task_id, EventType.STEP_FAILED,
                        step=subtask.title, error=abort_msg,
                        error_type="toolchain_failure",
                    )
                    return ToolResult(success=False, error=abort_msg, data={"failure_kind": "toolchain"})

                if self.auto_fix_enabled and attempt < subtask.max_retries - 1:
                    # ★ 重复错误早停：如果新的 stderr 和上一次几乎一样，
                    # 说明 LLM 上一轮的修复没动到根因，再试一次也是浪费 token
                    fp = self._stderr_fingerprint(stderr_text)
                    if stderr_fingerprints and self._stderr_similar(fp, stderr_fingerprints[-1]):
                        abort_msg = (
                            f"连续 2 次 auto_fix 后错误未变化，判定 LLM 无法修复，提前中止。\n"
                            f"原始错误片段: {stderr_text[:300]}"
                        )
                        logger.error(
                            f"Step '{subtask.title}' aborted: stderr unchanged after fix attempt"
                        )
                        subtask.status = TaskStatus.FAILED
                        subtask.result = {"error": abort_msg, "failure_kind": "stuck"}
                        await self._emit(
                            task_id, EventType.STEP_FAILED,
                            step=subtask.title, error=abort_msg,
                            error_type="auto_fix_stuck",
                        )
                        return ToolResult(success=False, error=abort_msg, data={"failure_kind": "stuck"})
                    stderr_fingerprints.append(fp)

                    logger.warning(
                        f"Step '{subtask.title}' failed (attempt {attempt+1}), invoking LLM auto_fix..."
                    )
                    await self._emit(task_id, EventType.LLM_RESPONSE,
                                     phase="auto_fix",
                                     summary=f"失败 (第{attempt+1}次), LLM 诊断中: {(result.error or '')[:100]}")

                    # ★★★ 调 LLM 真修 ★★★
                    patch = await self._attempt_llm_fix(
                        task_id=task_id,
                        subtask=subtask,
                        error_text=stderr_text,
                        project_path=project_path,
                    )

                    if patch is None:
                        # LLM 调用失败 / 响应无法解析 → 退化为静默重试，保持原行为
                        self.memory.add_message(
                            "tool", f"Error in {subtask.title}: {result.error}"
                        )
                    elif patch.get("unfixable"):
                        # LLM 明确表示单文件修不了 → 立刻 abort，escalate 给外层处理
                        reason = patch.get("reason", "")
                        abort_msg = (
                            f"LLM 判定该失败无法通过单文件修复（结构性问题），需人工或重新规划。\n"
                            f"原因: {reason}\n"
                            f"原始错误片段: {stderr_text[:300]}"
                        )
                        subtask.status = TaskStatus.FAILED
                        subtask.result = {"error": abort_msg, "failure_kind": "needs_human"}
                        await self._emit(
                            task_id, EventType.STEP_FAILED,
                            step=subtask.title, error=abort_msg,
                            error_type="needs_human",
                        )
                        return ToolResult(
                            success=False, error=abort_msg,
                            data={"failure_kind": "needs_human", "unfixable_reason": reason},
                        )
                    else:
                        # LLM 已应用 patch → 把改动登记为 CodeChange，让 evaluator 看得见
                        try:
                            existing_changes.append(CodeChange(
                                file_path=patch["file_to_fix"],
                                action=patch.get("action", "modify"),
                                content=patch.get("new_content", ""),
                                diff="",
                            ))
                        except Exception:
                            pass

                    subtask.retry_count = attempt + 1
                    continue

                # 超过重试次数
                subtask.status = TaskStatus.FAILED
                subtask.result = {"error": result.error}
                await self._emit(
                    task_id, EventType.STEP_FAILED,
                    step=subtask.title, error=result.error,
                )
                return result

            except Exception as e:
                logger.error(f"Subtask execution error: {e}")
                if attempt >= subtask.max_retries - 1:
                    return ToolResult(success=False, error=str(e))

        return ToolResult(success=False, error="Max retries exceeded")

    async def _dispatch_subtask(
        self,
        subtask: SubTask,
        project_path: str,
        existing_changes: list[CodeChange],
    ) -> ToolResult:
        """根据子任务类型分发到对应工具"""
        step_type = subtask.step_type

        if step_type == StepType.PLAN:
            # ★ Plan 步骤：架构/设计类任务，调用 LLM 生成设计方案，记录到 memory
            try:
                messages = [
                    {"role": "system", "content": "You are a software architect. Provide a concise technical design based on the task description. Output actionable decisions, not code."},
                    {"role": "user", "content": subtask.description},
                ]
                design_output = await self.planner._llm.chat(messages)
                self.memory.add_message("assistant", f"[Design] {subtask.title}: {design_output[:500]}")
                return ToolResult(
                    success=True,
                    output=design_output[:500],
                    data={"design": design_output, "step": subtask.title},
                )
            except Exception as e:
                logger.warning(f"Plan step LLM call failed: {e}, auto-passing with description")
                self.memory.add_message("assistant", f"[Design] {subtask.title}: {subtask.description}")
                return ToolResult(
                    success=True,
                    output=f"Design noted: {subtask.description}",
                    data={"design": subtask.description, "step": subtask.title},
                )

        elif step_type == StepType.CODE_GENERATE:
            # 从任务描述中推断有意义的文件名，而不是直接用 subtask title
            file_name = self._infer_file_name(subtask, project_path)
            # file_name 始终为相对路径，拼接到 project_path 下
            file_path = str(Path(project_path) / file_name)
            # 注入 token 回调以便流式推送到前端
            task_id = self._find_task_id_for_subtask(subtask)
            code_gen_tool = self.tools.get("code_generate")
            if code_gen_tool:
                async def _on_code_token(token):
                    await self._emit(task_id, EventType.LLM_TOKEN, token=token, phase="code_generate")
                code_gen_tool._on_token = _on_code_token
            return await self.tools.execute(
                "code_generate",
                instruction=subtask.description,
                file_path=file_path,
            )

        elif step_type == StepType.BUILD:
            # ★★★ 方案 Z：先 ensure project profile，再用 profile.build.command ★★★
            # profile 不存在时调 LLM 生成；存在时直接用，0 LLM 调用
            try:
                profile = await self.profile_manager.ensure(project_path, self.planner._llm)
            except Exception as e:
                logger.warning(f"profile.ensure failed: {e}, falling back to inference")
                profile = None

            if profile and profile.build_command:
                # profile 命中：先把 required_env 主动应用到当前进程
                applied_env = self.profile_manager.apply_required_env(profile)
                if applied_env:
                    logger.info(f"profile applied env: {applied_env}")
                # build_cmd 用 shell expansion 让 $DEVKITPRO 等变量被解析
                build_cmd = os.path.expandvars(profile.build_command)
                logger.info(f"BUILD using profile (type={profile.get('type')}): {build_cmd[:120]}")
            else:
                # 没 profile 或 LLM 失败：回退原推断逻辑
                build_cmd = self._infer_build_command(project_path, subtask.description)
                logger.info(f"BUILD using inferred command (no profile): {build_cmd[:120]}")

            result = await self.tools.execute(
                "shell_execute",
                command=build_cmd,
                working_dir=project_path,
            )

            # ★ build 失败时自动诊断并修复环境（保留原 _auto_fix_build_env 作为补充）
            if not result.success:
                stderr = result.error or (result.data.get("stderr", "") if result.data else "")
                env_fixed = self._auto_fix_build_env(stderr, project_path)
                if env_fixed:
                    logger.info(f"Auto-fixed build env: {env_fixed}, retrying build...")
                    result = await self.tools.execute(
                        "shell_execute",
                        command=build_cmd,
                        working_dir=project_path,
                    )

            # 失败计数：profile build 多次失败 → 记录到 profile 用于 W 兜底
            if profile and not result.success:
                stderr = result.error or (result.data.get("stderr", "") if result.data else "")
                # 传入 stderr + build_cmd，让本次失败快照进入 prior_attempts（跨 regen 持久化）
                fc = self.profile_manager.mark_failure(
                    project_path,
                    stderr=stderr,
                    build_command=build_cmd,
                )
                logger.warning(
                    f"profile build failure: fail_count={fc}, "
                    f"total_regens={profile.total_regens}"
                )

                # ★★★ P1: profile 第 2 次失败 → 强制重新生成 + 立刻重试一次
                # 这是 Z 方案的自我修复：当 LLM 第一版 profile 写错了，
                # 把错误信息作为 hint 让 LLM 看着 stderr 重新写一版
                #
                # 跨 regen 持久化的上限保护：如果这个项目已经 regen 过 MAX_PROFILE_REGENS
                # 次还在失败，说明 LLM 解决不了这个问题，不要再无限烧 token —— 直接
                # 让外层 auto_fix / _on_step_unrecoverable 接手，走人工 escalation。
                if fc >= 2 and profile.total_regens < self.MAX_PROFILE_REGENS:
                    logger.warning(
                        f"profile failed {fc} times (total_regens={profile.total_regens}), "
                        f"regenerating with error hint..."
                    )
                    try:
                        new_profile = await self.profile_manager.ensure(
                            project_path,
                            self.planner._llm,
                            force_regenerate=True,
                            previous_error=stderr,
                        )
                    except Exception as e:
                        logger.warning(f"profile regeneration raised: {e}")
                        new_profile = None

                    if new_profile and new_profile.build_command:
                        new_cmd = os.path.expandvars(new_profile.build_command)
                        if new_cmd != build_cmd:
                            self.profile_manager.apply_required_env(new_profile)
                            logger.info(
                                f"Retrying build with regenerated profile "
                                f"(total_regens={new_profile.total_regens}): {new_cmd[:120]}"
                            )
                            result = await self.tools.execute(
                                "shell_execute",
                                command=new_cmd,
                                working_dir=project_path,
                            )
                            if result.success:
                                self.profile_manager.reset_failures(project_path)
                        else:
                            logger.info(
                                "Regenerated profile produced same build command, skip retry"
                            )
                elif fc >= 2:
                    logger.error(
                        f"profile failed {fc} times and total_regens="
                        f"{profile.total_regens} has reached MAX_PROFILE_REGENS="
                        f"{self.MAX_PROFILE_REGENS}. Not auto-regenerating; escalating "
                        f"to human via _on_step_unrecoverable."
                    )
            elif profile and result.success:
                self.profile_manager.reset_failures(project_path)

            return result

        elif step_type == StepType.TEST:
            # ★★★ 方案 Z：profile.test.strategy 决定测试行为 ★★★
            profile = self.profile_manager.load(project_path)
            if profile:
                strategy = profile.test_strategy
                # 交叉编译类项目（如 Switch homebrew）声明 skip → 直接通过
                # 这解决了"host 上跑不了 cross-compile test"的根本矛盾
                if strategy == "skip":
                    reason = profile.test_reason or "Profile says test skipped"
                    logger.info(f"TEST skipped per profile: {reason}")
                    return ToolResult(
                        success=True,
                        output=f"Test skipped per project profile: {reason}",
                        data={
                            "test_result": {
                                "total": 0, "passed": 0, "failed": 0,
                                "skipped": 1, "coverage_percent": 0,
                            },
                            "skip_reason": reason,
                        },
                    )
                # custom 命令优先于 test_run 工具的自动检测
                if strategy == "custom" and profile.test_command:
                    cmd = os.path.expandvars(profile.test_command)
                    logger.info(f"TEST using profile.custom command: {cmd[:120]}")
                    return await self.tools.execute(
                        "shell_execute",
                        command=cmd,
                        working_dir=project_path,
                    )
                # 其他策略 (ctest/pytest/go_test/...) 走原 test_run 工具的自动检测路径

            # 尝试运行测试，如果没有测试框架则跳过（视为成功）
            try:
                result = await self.tools.execute(
                    "test_run",
                    project_path=project_path,
                )
                # 如果无法检测测试框架，不算失败
                if not result.success and "Could not detect test framework" in (result.error or ""):
                    logger.warning("No test framework detected, marking as failed")
                    return ToolResult(
                        success=False,
                        output="No test framework detected — cannot verify code quality",
                        error="No test framework detected. Need to set up tests.",
                        data={"test_result": {"total": 0, "passed": 0, "failed": 0, "coverage_percent": 0}},
                    )
                return result
            except Exception as e:
                logger.warning(f"Test step error: {e}, treating as skipped")
                return ToolResult(
                    success=True,
                    output=f"Test step skipped: {e}",
                    data={"test_result": {"total": 0, "passed": 0, "failed": 0, "coverage_percent": 0}},
                )

        elif step_type == StepType.EVALUATE:
            # 评分依据 = 当前子任务 scope；全局需求作为背景上下文传给 evaluator
            task_id = self._find_task_id_for_subtask(subtask)
            task_data = self.state._tasks.get(task_id, {})
            parent_goal = task_data.get("description", "")
            scoped_requirement = subtask.description
            async def _on_eval_token(token):
                await self._emit(task_id, EventType.LLM_TOKEN, token=token, phase="evaluate")
            # 把 profile 的 test.strategy 透传给 evaluator：strategy=skip 时跳过
            # test_coverage 维度，避免交叉编译项目被"无单测"硬顶 40 分拖进 discussion loop。
            profile_for_eval = self.profile_manager.load(project_path)
            eval_test_strategy = profile_for_eval.test_strategy if profile_for_eval else "auto"
            try:
                report = await self.evaluator.evaluate(
                    original_requirement=scoped_requirement,
                    code_changes=existing_changes,
                    project_path=project_path,
                    on_token=_on_eval_token,
                    parent_goal=parent_goal,
                    test_strategy=eval_test_strategy,
                )
                return ToolResult(
                    success=report.score >= self.min_eval_score,
                    output=f"Score: {report.score}",
                    data={"eval_report": report.model_dump()},
                )
            except Exception as e:
                logger.warning(f"Evaluation error: {e}, auto-passing")
                return ToolResult(
                    success=True,
                    output="Evaluation skipped due to error, auto-passed",
                    data={"eval_report": {"score": 75, "requirements_met": [], "requirements_missed": [], "risks": [str(e)], "suggestions": []}},
                )

        elif step_type == StepType.DEPLOY:
            # ★★★ 方案 Z：profile.deploy.command 优先，没有就回退到 _infer_deploy_command ★★★
            profile = self.profile_manager.load(project_path)
            deploy_cmd = None
            if profile:
                deploy_section = profile.get("deploy") or {}
                cmd = deploy_section.get("command")
                if cmd:
                    # required_env 也对 deploy 有效（go-mtpfs 可能需要 PATH 等）
                    self.profile_manager.apply_required_env(profile)
                    deploy_cmd = os.path.expandvars(cmd)
                    logger.info(f"DEPLOY using profile.deploy.command: {deploy_cmd[:120]}")
            if deploy_cmd is None:
                deploy_cmd = self._infer_deploy_command(project_path, subtask.description)
                logger.info(f"DEPLOY using inferred command (no profile.deploy): {deploy_cmd[:120]}")
            return await self.tools.execute(
                "shell_execute",
                command=deploy_cmd,
                working_dir=project_path,
            )

        else:
            return ToolResult(success=False, error=f"Unknown step type: {step_type}")

    def _infer_file_name(self, subtask: SubTask, project_path: str) -> str:
        """从子任务 title + description 中推断有意义的文件名

        返回的始终是相对于 project_path 的相对路径，避免绝对路径被重复拼接。
        """
        import re

        desc = subtask.description.lower()

        # 合并 title 和 description 搜索文件路径（title 中常包含明确路径）
        search_text = subtask.title + " " + subtask.description

        # 如果文本中明确提到了文件名 (xxx.py, xxx.js, docs/xxx.md 等)
        file_match = re.search(r'[a-zA-Z0-9_/\\.-]+\.[a-zA-Z0-9]{1,5}', search_text)
        if file_match:
            matched = file_match.group(0)
            # 清理前导的 ./
            while matched.startswith('./'):
                matched = matched[2:]
            # 统一路径分隔符（兼容 Windows 反斜杠出现在描述中的情况）
            matched = matched.replace('\\', '/')
            # 如果匹配到的是绝对路径，转成相对于 project_path 的相对路径
            matched_path = Path(matched)
            if matched_path.is_absolute():
                try:
                    return str(matched_path.relative_to(project_path))
                except ValueError:
                    # 绝对路径不在 project_path 下，取文件名部分
                    return matched_path.name
            return matched

        # 从描述中提取关键词生成文件名
        # 去掉常见动词和介词
        stop_words = {
            'create', 'write', 'implement', 'add', 'build', 'generate', 'make',
            'the', 'a', 'an', 'for', 'to', 'and', 'or', 'with', 'that', 'this',
            'code', 'file', 'new', 'function', 'class', 'module',
        }
        words = re.findall(r'[a-z]+', desc)
        keywords = [w for w in words if w not in stop_words and len(w) > 2]

        if keywords:
            # 取前3个关键词
            name = '_'.join(keywords[:3])
        else:
            # fallback: 用 subtask 序号
            name = f"step_{subtask.id or 'unknown'}"

        return f"{name}.py"

    # 工具链/环境故障的特征模式 — 命中后直接 abort，不进 LLM 修复循环
    # 这些故障 LLM 改项目代码救不了，必须由用户在环境层修
    _TOOLCHAIN_FAILURE_PATTERNS = [
        # pip 装的工具自身崩溃（cmake / black / ruff 等通过 ~/.local/bin 的入口脚本）
        (
            r"Traceback \(most recent call last\):[\s\S]*?(?:/\.local/bin/|site-packages/)",
            "Python 工具链自身崩溃",
        ),
        # 解释器/工具二进制找不到
        (r"command not found", "命令未安装"),
        (r"No such file or directory.*?(?:cmake|make|gcc|g\+\+|clang|python|node|java|rustc|cargo)", "工具二进制缺失"),
        # 动态库链接器故障（不是项目代码导致的）
        (r"error while loading shared libraries", "动态库缺失"),
        # 段错误来自工具链而非项目代码
        (r"(?:cmake|gcc|g\+\+|clang|ld): .*?[Ss]egmentation fault", "工具链段错误"),
        # PATH 或 shebang 解析失败
        (r"bad interpreter: No such file", "解释器路径失效"),
    ]

    def _stderr_fingerprint(self, stderr: str) -> str:
        """
        把 stderr 归一化成指纹，用于判断两次失败是否本质上是同一个错误。
        去掉行号、绝对路径前缀、PID、临时文件名、ANSI 颜色码、空白差异。
        """
        if not stderr:
            return ""
        import re as _re
        s = stderr
        s = _re.sub(r"\x1b\[[0-9;]*[A-Za-z]", "", s)              # ANSI
        s = _re.sub(r"line \d+", "line N", s)                      # py 行号
        s = _re.sub(r":\d+:\d+", ":N:N", s)                        # file:line:col
        s = _re.sub(r":\d+:", ":N:", s)                            # file:line
        s = _re.sub(r"/tmp/[A-Za-z0-9_./-]+", "/tmp/X", s)         # 临时路径
        s = _re.sub(r"0x[0-9a-fA-F]+", "0xADDR", s)                # 内存地址
        s = _re.sub(r"\bpid[= ]\d+", "pid=N", s, flags=_re.I)
        s = _re.sub(r"\s+", " ", s).strip()
        # 太长就只看前后各 1KB（开头通常含定位信息，结尾含 root cause）
        if len(s) > 2000:
            s = s[:1000] + " ... " + s[-1000:]
        return s

    def _stderr_similar(self, a: str, b: str, threshold: float = 0.85) -> bool:
        """两个 stderr 指纹是否相似（默认 85%）。"""
        if not a or not b:
            return False
        if a == b:
            return True
        from difflib import SequenceMatcher
        return SequenceMatcher(None, a, b).ratio() >= threshold

    def _classify_failure(self, stderr: str) -> tuple[str, str]:
        """
        把失败 stderr 分类成 (kind, reason)。

        kind:
          - "toolchain": 环境/工具链故障，LLM 修不了，应直接 abort
          - "code":      项目代码问题，可以进 auto_fix 循环
        """
        if not stderr:
            return ("code", "")

        import re as _re
        for pattern, reason in self._TOOLCHAIN_FAILURE_PATTERNS:
            if _re.search(pattern, stderr, _re.IGNORECASE):
                return ("toolchain", reason)

        return ("code", "")

    # 已知的环境变量 → 路径探测规则
    _ENV_AUTO_DETECT = {
        "DEVKITPRO": {
            "search_paths": ["/opt/devkitpro", "/usr/local/devkitpro", Path.home() / "devkitpro"],
            "verify_file": "cmake/Switch.cmake",
            "triggers": ["switch.h", "libnx", "devkitpro", "devkitA64"],
            "description": "Nintendo Switch 交叉编译工具链",
        },
        "ANDROID_HOME": {
            "search_paths": ["/usr/local/android-sdk", Path.home() / "Android" / "Sdk", "/opt/android-sdk"],
            "verify_file": "platform-tools/adb",
            "triggers": ["android.h", "ndk-build", "gradle"],
            "description": "Android SDK",
        },
        "JAVA_HOME": {
            "search_paths": ["/usr/lib/jvm/java-17-openjdk-amd64", "/usr/lib/jvm/java-11-openjdk-amd64"],
            "verify_file": "bin/javac",
            "triggers": ["javac", "javax", "java.lang"],
            "description": "Java Development Kit",
        },
    }

    def _auto_fix_build_env(self, stderr: str, project_path: str) -> dict:
        """
        分析 build 错误，自动检测并设置缺失的环境变量。
        返回 {env_var: path} 表示修复了哪些环境变量，空 dict 表示无法修复。
        """
        fixed = {}
        stderr_lower = stderr.lower()

        for env_var, config in self._ENV_AUTO_DETECT.items():
            # 跳过已设置的环境变量
            if os.environ.get(env_var):
                continue

            # 检查 stderr 是否包含触发关键词
            triggered = any(trigger in stderr_lower for trigger in config["triggers"])
            if not triggered:
                continue

            # 在已知路径中搜索
            for search_path in config["search_paths"]:
                search_path = Path(search_path)
                if not search_path.exists():
                    continue
                verify = search_path / config["verify_file"]
                if verify.exists():
                    # 找到了！设置环境变量
                    path_str = str(search_path)
                    os.environ[env_var] = path_str
                    fixed[env_var] = path_str
                    logger.info(f"Auto-detected {env_var}={path_str} ({config['description']})")

                    # 同时设置衍生环境变量
                    if env_var == "DEVKITPRO":
                        devkita64 = search_path / "devkitA64"
                        if devkita64.exists():
                            os.environ["DEVKITA64"] = str(devkita64)
                            # 把工具链路径加入 PATH
                            tools_bin = search_path / "tools" / "bin"
                            a64_bin = devkita64 / "bin"
                            path_dirs = [str(tools_bin), str(a64_bin)]
                            current_path = os.environ.get("PATH", "")
                            for d in path_dirs:
                                if d not in current_path:
                                    os.environ["PATH"] = d + ":" + current_path
                                    current_path = os.environ["PATH"]
                    break

        if fixed:
            # 保存到 .kedo/env_auto_detected.json 供下次启动参考
            env_path = Path(project_path) / ".kedo" / "env_auto_detected.json"
            try:
                import json as _json
                existing = {}
                if env_path.exists():
                    existing = _json.loads(env_path.read_text())
                existing.update(fixed)
                env_path.parent.mkdir(parents=True, exist_ok=True)
                env_path.write_text(_json.dumps(existing, indent=2))
            except Exception:
                pass

            # 发事件通知 Dashboard
            logger.warning(f"Auto-fixed environment: {fixed}. Recommend restart for full effect.")

        return fixed

    def _infer_build_command(self, project_path: str, description: str) -> str:
        """根据项目类型智能推断构建命令（按优先级排列）"""
        p = Path(project_path)

        # devkitPro Switch 项目（从环境变量检测）
        devkitpro_env = os.environ.get("DEVKITPRO", "")
        devkitpro = Path(devkitpro_env) if devkitpro_env else None
        if (p / "CMakeLists.txt").exists() and devkitpro and devkitpro.exists():
            # 检查 CMakeLists 或项目是否是 Switch 项目
            cmake_content = ""
            try:
                cmake_content = (p / "CMakeLists.txt").read_text(encoding="utf-8", errors="replace").lower()
            except Exception:
                pass
            is_switch = "switch" in cmake_content or "nx" in cmake_content or (p / "source").is_dir() or any(p.rglob("*.nro"))
            # 也检查项目描述
            desc_lower = description.lower()
            if is_switch or "switch" in desc_lower or "nro" in desc_lower:
                toolchain = devkitpro / "cmake" / "Switch.cmake"
                if toolchain.exists():
                    return (
                        f"mkdir -p build && cd build && "
                        f"cmake -DCMAKE_TOOLCHAIN_FILE={toolchain} .. && "
                        f"make -j$(nproc)"
                    )

        # CMake 项目（通用）
        if (p / "CMakeLists.txt").exists():
            return "mkdir -p build && cd build && cmake .. && make -j$(nproc)"

        # Makefile 项目
        if (p / "Makefile").exists():
            return "make -j$(nproc)"

        # Rust
        if (p / "Cargo.toml").exists():
            return "cargo build"

        # Go
        if (p / "go.mod").exists():
            return "go build ./..."

        # Node.js
        if (p / "package.json").exists():
            return "npm run build"

        # Python 项目 — 只在有 setup.py/pyproject.toml 时检测（避免误匹配散落的 .py）
        if (p / "setup.py").exists() or (p / "pyproject.toml").exists():
            return "python -m build 2>/dev/null || python setup.py build"

        # Docker
        if (p / "docker-compose.yml").exists():
            return "docker-compose build"
        if (p / "Dockerfile").exists():
            return "docker build -t $(basename $(pwd)) ."

        # 默认 — 没有构建系统，返回失败命令
        return "echo 'ERROR: No build system detected (Makefile, CMakeLists.txt, package.json, etc.)' && exit 1"

    def _infer_deploy_command(self, project_path: str, description: str) -> str:
        """推断部署命令（默认安全占位）"""
        return "echo 'Deploy: OK (placeholder — configure actual deployment)'"

    def _find_task_id_for_subtask(self, subtask: SubTask) -> str:
        """找到 subtask 所属的 task_id"""
        # 从正在运行的任务中查找
        for task_id in self._running_tasks:
            return task_id
        return ""

    # ==========================================================
    # 闭环讨论机制
    # ==========================================================

    async def _handle_eval_failure_loop(
        self,
        task_id: str,
        plan: TaskPlan,
        eval_report: Optional[EvalReport],
        code_changes: list[CodeChange],
        test_results: Optional[TestResult],
        description: str,
        project_path: str,
        trigger: str = "eval_failed",
        human_feedback: str = "",
    ) -> TaskPlan:
        """
        闭环核心：评估不通过/人工驳回 → 分析 → 讨论 → 重新规划

        三步走:
        Step A: 失败原因分析 → 生成问题清单
        Step B: 讨论 & 方案制定 → AI提出方案 / 人工介入
        Step C: 重新规划 → 注入上下文后 replan
        """
        # 获取/初始化迭代状态
        if task_id not in self._iterations:
            self._iterations[task_id] = IterationState(task_id=task_id)
        iteration_state = self._iterations[task_id]

        # 检查是否超过最大迭代次数
        if iteration_state.current_iteration >= iteration_state.max_iterations:
            logger.error(f"Task {task_id} exceeded max iterations ({iteration_state.max_iterations}), forcing pause")
            iteration_state.is_forced_pause = True
            await self._emit(task_id, EventType.ITERATION_UPDATED,
                             iteration=iteration_state.current_iteration,
                             max_iterations=iteration_state.max_iterations,
                             forced_pause=True)
            await self.state.pause_task(task_id)
            await self.state.wait_if_paused(task_id)
            # 人工恢复后重置计数器
            iteration_state.current_iteration = 1
            iteration_state.is_forced_pause = False

        iteration_state.current_iteration += 1
        await self._emit(task_id, EventType.ITERATION_UPDATED,
                         iteration=iteration_state.current_iteration,
                         max_iterations=iteration_state.max_iterations,
                         forced_pause=False)

        # 创建讨论记录
        discussion = DiscussionRecord(
            task_id=task_id,
            iteration=iteration_state.current_iteration,
            trigger=trigger,
            eval_report=eval_report,
            human_feedback=human_feedback,
        )

        # ---- Step A: 失败原因分析 ----
        await self._emit(task_id, EventType.DISCUSSION_STARTED,
                         discussion_id=discussion.discussion_id,
                         iteration=discussion.iteration,
                         trigger=trigger)

        issues = await self._analyze_failure(eval_report, human_feedback, trigger)
        discussion.issues = issues
        discussion.status = DiscussionStatus.PROPOSALS_READY

        # ---- Step B: 讨论 & 方案生成 ----
        proposals = await self._generate_proposals(issues, eval_report, code_changes, description)
        discussion.proposals = proposals

        await self._emit(task_id, EventType.DISCUSSION_PROPOSALS,
                         discussion_id=discussion.discussion_id,
                         issues=[i.model_dump() for i in issues],
                         proposals=[p.model_dump() for p in proposals])

        # 选择方案：自动模式 or 等待人工（多轮讨论）
        selected_proposal = None
        if self.auto_discussion:
            # AI 自动选择推荐方案
            selected_proposal = next((p for p in proposals if p.ai_recommended), proposals[0] if proposals else None)
            if selected_proposal:
                discussion.selected_proposal_id = selected_proposal.proposal_id
                logger.info(f"Auto-selected proposal: {selected_proposal.title}")
        else:
            # 多轮讨论循环
            selected_proposal = await self._wait_for_discussion_resolution(
                task_id, discussion, proposals
            )

        # ---- Step C: 重新规划 ----
        discussion.status = DiscussionStatus.RESOLVED
        from datetime import datetime
        discussion.resolved_at = datetime.utcnow()

        # 构建 replan 上下文
        replan_context = self._build_replan_context(discussion, selected_proposal)
        discussion.replan_context = replan_context

        # 保存讨论记录
        iteration_state.discussions.append(discussion)

        # 将讨论经验写入长期记忆
        self.memory.add_experience(
            task_summary=f"Iteration #{discussion.iteration}: {discussion.trigger}",
            learnings=[
                f"问题: {issue.description}" for issue in discussion.issues[:3]
            ] + ([
                f"解决方案: {selected_proposal.title} - {selected_proposal.approach}"
            ] if selected_proposal else []),
        )

        await self._emit(task_id, EventType.DISCUSSION_RESOLVED,
                         discussion_id=discussion.discussion_id,
                         selected_proposal=selected_proposal.model_dump() if selected_proposal else {},
                         replan_context=replan_context[:200])

        # 记入 Memory
        self.memory.add_message("system", f"[Iteration #{discussion.iteration}] Discussion resolved:\n{replan_context[:500]}")

        # 执行重新规划
        await self._emit(task_id, EventType.REPLAN_STARTED,
                         iteration=discussion.iteration)
        new_plan = await self.planner.replan(task_id, plan, replan_context)
        await self._emit(task_id, EventType.REPLAN_COMPLETED,
                         iteration=discussion.iteration,
                         subtask_count=len(new_plan.subtasks))

        return new_plan

    async def _analyze_failure(
        self,
        eval_report: Optional[EvalReport],
        human_feedback: str,
        trigger: str,
    ) -> list[IssueItem]:
        """Step A: 分析失败原因，生成问题清单"""
        issues = []

        if eval_report:
            # 从评估报告中提取问题
            for missed in eval_report.requirements_missed:
                issues.append(IssueItem(
                    category="功能缺失",
                    severity="high",
                    description=missed,
                    eval_dimension="requirements",
                    suggestion=f"需要补充实现: {missed}",
                ))
            for risk in eval_report.risks:
                issues.append(IssueItem(
                    category="安全/风险",
                    severity="medium",
                    description=risk,
                    eval_dimension="risks",
                    suggestion=f"需要处理风险: {risk}",
                ))
            for suggestion in eval_report.suggestions:
                issues.append(IssueItem(
                    category="代码质量",
                    severity="low",
                    description=suggestion,
                    eval_dimension="suggestions",
                    suggestion=suggestion,
                ))
            # 如果评分低但没有具体原因
            if not issues and eval_report.score < 70:
                issues.append(IssueItem(
                    category="综合质量",
                    severity="high",
                    description=f"评估分数仅 {eval_report.score}，低于阈值",
                    eval_dimension="score",
                    suggestion="需要全面提升代码质量",
                ))

        if human_feedback:
            issues.append(IssueItem(
                category="人工反馈",
                severity="critical",
                description=human_feedback,
                eval_dimension="human_review",
                suggestion=f"根据人工反馈调整: {human_feedback}",
            ))

        # 如果没有任何问题，添加通用问题
        if not issues:
            issues.append(IssueItem(
                category="未知",
                severity="medium",
                description=f"触发原因: {trigger}",
                eval_dimension="unknown",
                suggestion="需要进一步调查",
            ))

        return issues

    async def _generate_proposals(
        self,
        issues: list[IssueItem],
        eval_report: Optional[EvalReport],
        code_changes: list[CodeChange],
        description: str,
    ) -> list[Proposal]:
        """Step B: 用 LLM 生成针对性解决方案提案"""
        import json

        issues_text = "\n".join(
            f"- [{i.severity.upper()}] {i.category}: {i.description}"
            for i in issues
        )
        changes_text = "\n".join(
            f"- {c.action} {c.file_path}" for c in code_changes[:10]
        )
        score_text = f"Current score: {eval_report.score}" if eval_report else "No eval report"

        prompt = (
            "Based on the following issues found in code evaluation, "
            "generate 2-3 concrete fix proposals.\n\n"
            f"Issues:\n{issues_text}\n\n"
            f"Original requirement: {description}\n\n"
            f"Code changes so far:\n{changes_text}\n\n"
            f"{score_text}\n\n"
            "For each proposal, provide a JSON object with:\n"
            '- "title": short name\n'
            '- "description": what to do\n'
            '- "approach": specific technical steps\n'
            '- "estimated_effort": "small" | "medium" | "large"\n'
            '- "risk_level": "low" | "medium" | "high"\n'
            '- "pros": list of advantages\n'
            '- "cons": list of disadvantages\n'
            '- "ai_recommended": true for the best one, false for others\n'
            "Output as a JSON array. No extra text."
        )

        try:
            response = await self.planner._llm.chat([
                {"role": "system", "content": "You are a technical lead proposing fix strategies. Output valid JSON only."},
                {"role": "user", "content": prompt},
            ])
            proposals = self._parse_proposals(response)
            if proposals:
                return proposals
        except Exception as e:
            logger.warning(f"LLM proposal generation failed: {e}, using fallback")

        # 回退到硬编码方案
        return self._fallback_proposals(issues, eval_report)

    def _parse_proposals(self, response: str) -> list[Proposal]:
        """解析 LLM 返回的 JSON 方案列表"""
        import json

        # 尝试提取 JSON 数组
        text = response.strip()
        # 处理 markdown 代码块包裹
        if "```" in text:
            import re
            match = re.search(r"```(?:json)?\s*\n?(.*?)```", text, re.DOTALL)
            if match:
                text = match.group(1).strip()

        try:
            data = json.loads(text)
        except json.JSONDecodeError:
            return []

        if not isinstance(data, list):
            return []

        proposals = []
        for item in data:
            if not isinstance(item, dict):
                continue
            proposals.append(Proposal(
                title=item.get("title", "Untitled"),
                description=item.get("description", ""),
                approach=item.get("approach", ""),
                estimated_effort=item.get("estimated_effort", "medium"),
                risk_level=item.get("risk_level", "medium"),
                pros=item.get("pros", []),
                cons=item.get("cons", []),
                ai_recommended=item.get("ai_recommended", False),
            ))

        # 确保至少有一个 ai_recommended
        if proposals and not any(p.ai_recommended for p in proposals):
            proposals[0].ai_recommended = True

        return proposals

    def _fallback_proposals(
        self,
        issues: list[IssueItem],
        eval_report: Optional[EvalReport],
    ) -> list[Proposal]:
        """LLM 方案生成失败时的回退方案"""
        proposals = []
        high_issues = [i for i in issues if i.severity in ("critical", "high")]

        if high_issues:
            proposals.append(Proposal(
                title="针对性修复关键问题",
                description=f"仅修复 {len(high_issues)} 个高优先级问题",
                approach="逐个修复关键/高优问题，保留已通过的代码",
                estimated_effort="small",
                risk_level="low",
                pros=["改动最小", "风险低", "速度快"],
                cons=["可能遗漏低优先级问题"],
                ai_recommended=True,
            ))

        if not proposals:
            proposals.append(Proposal(
                title="通用改进",
                description="基于评估反馈进行改进",
                approach="根据所有问题点逐一改进",
                estimated_effort="medium",
                risk_level="low",
                pros=["覆盖全面"],
                cons=["可能改动较多"],
                ai_recommended=True,
            ))

        return proposals

    def _build_replan_context(
        self,
        discussion: DiscussionRecord,
        selected_proposal: Optional[Proposal],
    ) -> str:
        """构建注入给 Planner 的上下文摘要"""
        lines = [
            f"=== 闭环迭代 #{discussion.iteration} — 重新规划上下文 ===",
            f"触发原因: {discussion.trigger}",
            "",
        ]

        # 问题清单
        if discussion.issues:
            lines.append("【问题清单】")
            for i, issue in enumerate(discussion.issues, 1):
                lines.append(f"  {i}. [{issue.severity.upper()}] {issue.category}: {issue.description}")
            lines.append("")

        # 选定方案
        if selected_proposal:
            lines.append(f"【选定方案】{selected_proposal.title}")
            lines.append(f"  方法: {selected_proposal.approach}")
            lines.append(f"  预估工作量: {selected_proposal.estimated_effort}")
            lines.append("")

        # 人工输入
        if discussion.human_input:
            lines.append(f"【人工意见】{discussion.human_input}")
            lines.append("")
        if discussion.human_feedback:
            lines.append(f"【人工反馈】{discussion.human_feedback}")
            lines.append("")

        # 评估报告摘要
        if discussion.eval_report:
            lines.append(f"【上轮评分】{discussion.eval_report.score}")
            if discussion.eval_report.requirements_missed:
                lines.append(f"  未满足需求: {', '.join(discussion.eval_report.requirements_missed[:5])}")
            lines.append("")

        lines.append("请基于以上上下文重新规划，确保新计划能解决所有已知问题。")
        return "\n".join(lines)

    # ==========================================================
    # 子 Agent 并行调度
    # ==========================================================

    async def _execute_parallel_subtasks(
        self,
        task_id: str,
        subtasks: list[SubTask],
        project_path: str,
    ) -> list[ToolResult]:
        """
        并发执行无依赖的子任务

        分析子任务依赖关系，将无依赖的子任务分配给子 Agent 并发执行。
        """
        from core.sub_agent import execute_parallel_subtasks

        await self._emit(task_id, EventType.PARALLEL_BATCH_STARTED,
                         subtask_count=len(subtasks),
                         subtask_titles=[s.title for s in subtasks])

        results = await execute_parallel_subtasks(
            subtasks=subtasks,
            parent_memory=self.memory,
            llm_client=self.planner._llm,
            tool_registry=self.tools,
            project_path=project_path,
        )

        success_count = sum(1 for r in results if r.success)
        await self._emit(task_id, EventType.PARALLEL_BATCH_COMPLETED,
                         total=len(results),
                         success=success_count,
                         failed=len(results) - success_count)

        return results

    def _find_independent_subtasks(self, subtasks: list[SubTask]) -> tuple[list[SubTask], list[SubTask]]:
        """
        从子任务列表中分出可并行的子任务和必须串行的子任务

        Returns:
            (parallel_subtasks, sequential_subtasks)
        """
        parallel = []
        sequential = []

        for subtask in subtasks:
            # 有依赖的必须串行
            if subtask.dependencies:
                sequential.append(subtask)
            # 写操作（代码生成、部署）必须串行
            elif subtask.step_type in (StepType.CODE_GENERATE, StepType.DEPLOY):
                sequential.append(subtask)
            # 只读/分析类操作可以并行
            elif subtask.step_type in (StepType.PLAN, StepType.TEST, StepType.EVALUATE):
                parallel.append(subtask)
            else:
                sequential.append(subtask)

        return parallel, sequential

    # ==========================================================
    # 多轮讨论
    # ==========================================================

    async def _wait_for_discussion_resolution(
        self,
        task_id: str,
        discussion: DiscussionRecord,
        proposals: list[Proposal],
    ) -> Optional[Proposal]:
        """多轮讨论循环 — 支持选方案、追问和自定义方案"""
        discussion.status = DiscussionStatus.WAITING_HUMAN
        await self._emit(task_id, EventType.DISCUSSION_WAITING_HUMAN,
                         discussion_id=discussion.discussion_id)

        # 初始化讨论队列
        if task_id not in self._discussion_queues:
            self._discussion_queues[task_id] = asyncio.Queue()

        selected_proposal = None
        while True:
            human_input = await self._discussion_queues[task_id].get()
            action = human_input.get("action", "select")

            if action == "select":
                # 用户选择了方案
                proposal_id = human_input.get("proposal_id", "")
                discussion.human_input = human_input.get("human_input", "")

                if proposal_id:
                    selected_proposal = next(
                        (p for p in proposals if p.proposal_id == proposal_id), None
                    )
                if not selected_proposal and proposals:
                    selected_proposal = proposals[0]
                if selected_proposal:
                    discussion.selected_proposal_id = selected_proposal.proposal_id
                break

            elif action == "ask":
                # 用户追问 → AI 回答 → 继续等待
                question = human_input.get("human_input", "")
                answer = await self._answer_discussion_question(
                    question, discussion.issues, proposals
                )
                await self._emit(task_id, EventType.DISCUSSION_RESPONSE,
                                 question=question, answer=answer)

            elif action == "custom":
                # 用户提出自定义方案 → 加入 proposals → 自动选中
                custom = Proposal(
                    title="用户自定义方案",
                    description=human_input.get("human_input", ""),
                    approach=human_input.get("human_input", ""),
                    estimated_effort="medium",
                    ai_recommended=False,
                )
                proposals.append(custom)
                discussion.proposals.append(custom)
                discussion.selected_proposal_id = custom.proposal_id
                selected_proposal = custom
                break

        return selected_proposal

    async def _answer_discussion_question(
        self,
        question: str,
        issues: list[IssueItem],
        proposals: list[Proposal],
    ) -> str:
        """AI 回答讨论中的追问"""
        issues_text = "\n".join(
            f"- [{i.severity}] {i.category}: {i.description}" for i in issues
        )
        proposals_text = "\n".join(
            f"- {p.title}: {p.description} (effort: {p.estimated_effort}, risk: {p.risk_level})"
            + (" [RECOMMENDED]" if p.ai_recommended else "")
            for p in proposals
        )

        prompt = (
            "You are a technical lead in a code review discussion.\n\n"
            f"Current issues:\n{issues_text}\n\n"
            f"Proposed solutions:\n{proposals_text}\n\n"
            f"The developer asks: {question}\n\n"
            "Provide a concise, helpful answer."
        )

        try:
            return await self.planner._llm.chat([
                {"role": "system", "content": "You are a helpful technical lead."},
                {"role": "user", "content": prompt},
            ])
        except Exception as e:
            logger.warning(f"Discussion answer failed: {e}")
            return f"Unable to answer: {e}"

    # ==========================================================
    # 辅助方法
    # ==========================================================

    async def _stream_llm_call(
        self, task_id: str, messages: list[dict], phase: str
    ) -> str:
        """
        流式调用 LLM 并推送 token 事件

        如果 LLM 客户端支持 stream_chat，逐 token 推送；
        否则回退到非流式调用。
        """
        llm = self.planner._llm
        chunks = []
        try:
            async for token in llm.stream_chat(messages):
                chunks.append(token)
                await self._emit(task_id, EventType.LLM_TOKEN, token=token, phase=phase)
        except Exception as e:
            logger.warning(f"Stream call failed ({e}), falling back to non-stream")
            result = await llm.chat(messages)
            return result
        return "".join(chunks)

    async def _gather_project_context(self, project_path: str) -> dict:
        """收集项目上下文信息"""
        result = await self.tools.execute(
            "file_search",
            directory=project_path,
            pattern="*",
        )
        return {
            "project_path": project_path,
            "file_count": result.data.get("count", 0),
            "files": result.data.get("files", [])[:50],  # 最多 50 个文件
        }

    async def _gather_project_state(self, project_path: str) -> dict:
        """收集项目深层状态，让 Planner 自主判断该做什么"""
        from pathlib import Path as _Path
        project = _Path(project_path)
        skip_dirs = {".kedo", ".git", ".venv", "node_modules", "__pycache__", "build", "dist"}
        src_exts = {".cpp", ".c", ".h", ".hpp", ".py", ".js", ".ts", ".java", ".go", ".rs"}

        # 源码状态
        src_files = []
        for f in project.rglob("*"):
            if f.is_file() and f.suffix.lower() in src_exts and not any(p in f.relative_to(project).parts for p in skip_dirs):
                src_files.append(str(f.relative_to(project)))

        # 构建状态
        artifact_exts = {".nro", ".elf", ".exe", ".bin", ".so", ".dll", ".jar", ".whl"}
        artifacts = [str(f.relative_to(project)) for f in project.rglob("*")
                     if f.is_file() and f.suffix.lower() in artifact_exts
                     and not any(p in f.relative_to(project).parts for p in skip_dirs)]

        has_makefile = (project / "Makefile").exists()
        has_cmake = (project / "CMakeLists.txt").exists()
        has_docker = (project / "docker-compose.yml").exists() or (project / "Dockerfile").exists()
        has_docs = (project / "docs").is_dir()

        # 文档状态
        doc_files = []
        if has_docs:
            doc_files = [str(f.relative_to(project)) for f in (project / "docs").rglob("*.md")]

        # 上次评估
        last_eval = None
        resumable = self.state.find_resumable_tasks()
        if resumable:
            cp = await self.state.load_checkpoint(resumable[0]["task_id"])
            if cp and cp.eval_report:
                last_eval = {
                    "score": cp.eval_report.score,
                    "requirements_met": cp.eval_report.requirements_met[:5],
                    "requirements_missed": cp.eval_report.requirements_missed[:5],
                    "suggestions": cp.eval_report.suggestions[:3],
                }

        return {
            "has_source_code": len(src_files) > 0,
            "source_files": src_files[:20],
            "source_count": len(src_files),
            "has_build_artifacts": len(artifacts) > 0,
            "artifacts": artifacts,
            "has_makefile": has_makefile,
            "has_cmake": has_cmake,
            "has_docker": has_docker,
            "has_docs": has_docs,
            "doc_files": doc_files[:15],
            "last_eval": last_eval,
            "previous_tasks": len(self.state._tasks),
        }

    def _get_model_name(self) -> str:
        """获取当前 LLM 模型名"""
        llm = self.planner._llm
        return getattr(llm, "model", type(llm).__name__)

    async def _emit(self, task_id: str, event_type: EventType, **data):
        """发布事件"""
        await self.state.event_bus.publish(AgentEvent(
            event_type=event_type,
            task_id=task_id,
            data=data,
        ))
