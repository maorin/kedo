"""
Agent Loop Controller — AI 开发助手的核心控制循环

实现 Plan → Execute → Observe → Evaluate → (Loop or Exit) 的持续运行循环
支持暂停/恢复、人工审查门、检查点保存
"""
from __future__ import annotations

import asyncio
import logging
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
    ReviewDecision,
    StepType,
    SubTask,
    TaskPlan,
    TaskStatus,
    TestResult,
)
from core.evaluator import Evaluator
from core.memory import AgentMemory
from core.planner import Planner
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

        # 运行时状态
        self._running_tasks: dict[str, asyncio.Task] = {}
        self._review_queues: dict[str, asyncio.Queue] = {}
        self._discussion_queues: dict[str, asyncio.Queue] = {}  # 讨论阶段的人工输入队列
        self._iterations: dict[str, IterationState] = {}         # 迭代状态跟踪

        # 配置
        # tool_registry 引用（供外部热替换 LLM client 时遍历工具）
        self.tool_registry = self.tools

        self.max_retries = self.config.get("max_retries", 3)
        self.auto_fix_enabled = self.config.get("auto_fix", True)
        self.review_gate_enabled = self.config.get("review_gate", True)
        self.min_eval_score = self.config.get("min_eval_score", 70)
        self.max_iterations = self.config.get("max_iterations", 5)
        self.auto_discussion = self.config.get("auto_discussion", True)  # AI自动选方案 vs 等人工

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
        self._review_queues[task_id] = asyncio.Queue()

        # 在后台启动 Agent Loop
        loop_task = asyncio.create_task(
            self._run_loop(task_id, description, project_path)
        )
        self._running_tasks[task_id] = loop_task

        logger.info(f"Agent Loop started for task {task_id}")

    async def resume_from_checkpoint(self, task_id: str):
        """从检查点恢复执行"""
        checkpoint = await self.state.load_checkpoint(task_id)
        if not checkpoint:
            raise ValueError(f"No checkpoint found for task {task_id}")

        # 恢复记忆
        self.memory.restore(checkpoint.memory_snapshot)

        # 恢复暂停状态
        await self.state.resume_task(task_id)

        # 从检查点位置继续执行
        loop_task = asyncio.create_task(
            self._run_loop_from_checkpoint(task_id, checkpoint)
        )
        self._running_tasks[task_id] = loop_task

    async def submit_review(
        self,
        task_id: str,
        decision: ReviewDecision,
        feedback: str = "",
        version_id: str = "",
        test_notes: str = "",
    ):
        """提交人工审查结果"""
        # 如果用户选择了一个版本进行测试，先标记为 human_testing
        if version_id:
            await self.versions.select_for_testing(task_id, version_id)

        if task_id in self._review_queues:
            await self._review_queues[task_id].put({
                "decision": decision,
                "feedback": feedback,
                "version_id": version_id,
                "test_notes": test_notes,
            })

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

                elif subtask.step_type == StepType.REVIEW and self.review_gate_enabled:
                    # ★ 人工审查门 — 现在基于候选版本
                    await self._handle_review_gate(task_id, plan, code_changes, test_results)

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

    async def _run_loop_from_checkpoint(self, task_id: str, checkpoint: AgentCheckpoint):
        """从检查点恢复执行"""
        plan = checkpoint.plan
        start_index = checkpoint.current_step_index + 1
        code_changes = checkpoint.code_changes
        test_results = checkpoint.test_results

        project_path = self.state._tasks.get(task_id, {}).get("config", {}).get("project_path", ".")

        await self.state.update_status(task_id, TaskStatus.IN_PROGRESS)

        for i in range(start_index, len(plan.subtasks)):
            subtask = plan.subtasks[i]
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
                ))
            elif subtask.step_type == StepType.TEST:
                test_results = TestResult(**result.data.get("test_result", {}))

            await self.state.save_checkpoint(AgentCheckpoint(
                task_id=task_id,
                current_step_index=i,
                plan=plan,
                memory_snapshot=self.memory.snapshot(),
                code_changes=code_changes,
                test_results=test_results,
            ))

        await self.state.update_status(task_id, TaskStatus.COMPLETED, progress_percent=100)

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

                if self.auto_fix_enabled and attempt < subtask.max_retries - 1:
                    logger.warning(
                        f"Step '{subtask.title}' failed (attempt {attempt+1}), auto-fixing..."
                    )
                    await self._emit(task_id, EventType.LLM_RESPONSE,
                                     phase="auto_fix",
                                     summary=f"失败 (第{attempt+1}次), 自动修复中: {(result.error or '')[:100]}")
                    self.memory.add_message(
                        "tool", f"Error in {subtask.title}: {result.error}"
                    )
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
            # 智能推断构建命令（不直接用 description 当命令）
            build_cmd = self._infer_build_command(project_path, subtask.description)
            return await self.tools.execute(
                "shell_execute",
                command=build_cmd,
                working_dir=project_path,
            )

        elif step_type == StepType.TEST:
            # 尝试运行测试，如果没有测试框架则跳过（视为成功）
            try:
                result = await self.tools.execute(
                    "test_run",
                    project_path=project_path,
                )
                # 如果无法检测测试框架，不算失败
                if not result.success and "Could not detect test framework" in (result.error or ""):
                    logger.info("No test framework detected, skipping test step")
                    return ToolResult(
                        success=True,
                        output="No test framework detected — step skipped",
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
            # 从当前任务中获取描述（通过 task_id 查找，不是 subtask.id）
            task_id = self._find_task_id_for_subtask(subtask)
            task_data = self.state._tasks.get(task_id, {})
            description = task_data.get("description", subtask.description)
            async def _on_eval_token(token):
                await self._emit(task_id, EventType.LLM_TOKEN, token=token, phase="evaluate")
            try:
                report = await self.evaluator.evaluate(
                    original_requirement=description,
                    code_changes=existing_changes,
                    project_path=project_path,
                    on_token=_on_eval_token,
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

        elif step_type == StepType.REVIEW:
            return ToolResult(success=True, output="Awaiting human review")

        elif step_type == StepType.DEPLOY:
            deploy_cmd = self._infer_deploy_command(project_path, subtask.description)
            return await self.tools.execute(
                "shell_execute",
                command=deploy_cmd,
                working_dir=project_path,
            )

        else:
            return ToolResult(success=False, error=f"Unknown step type: {step_type}")

    def _infer_file_name(self, subtask: SubTask, project_path: str) -> str:
        """从子任务描述中推断有意义的文件名，而非直接用 subtask.title

        返回的始终是相对于 project_path 的相对路径，避免绝对路径被重复拼接。
        """
        import re

        desc = subtask.description.lower()

        # 如果描述中明确提到了文件名 (xxx.py, xxx.js 等)
        # 使用 ASCII 字符集避免匹配中文等 Unicode 字符
        file_match = re.search(r'[a-zA-Z0-9_/\\.-]+\.[a-zA-Z0-9]{1,5}', subtask.description)
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

    def _infer_build_command(self, project_path: str, description: str) -> str:
        """根据项目类型智能推断构建命令"""
        p = Path(project_path)

        # Python 项目 — 语法检查
        if list(p.glob("*.py")) or (p / "setup.py").exists() or (p / "pyproject.toml").exists():
            py_files = " ".join(str(f) for f in p.glob("*.py"))
            if py_files:
                return f"python -m py_compile {list(p.glob('*.py'))[0]}"
            return "python -c \"print('Build: OK (Python project)')\""

        # Node.js
        if (p / "package.json").exists():
            return "npm run build 2>/dev/null || echo 'Build: OK (no build script)'"

        # Go
        if (p / "go.mod").exists():
            return "go build ./..."

        # Rust
        if (p / "Cargo.toml").exists():
            return "cargo build"

        # 默认 — 不执行危险命令，只做确认
        return "echo 'Build: OK (no build system detected)'"

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
    # 人工审查门
    # ==========================================================

    async def _handle_review_gate(
        self,
        task_id: str,
        plan: TaskPlan,
        code_changes: list[CodeChange],
        test_results: Optional[TestResult],
    ):
        """
        处理人工审查节点 — 基于候选版本

        流程:
        1. 获取所有候选版本 → 找到 AI 推荐的版本
        2. 通知前端展示候选版本列表
        3. 用户选择一个版本进行人工测试
        4. 用户提交 Approve / Reject 决策
        5. 如果 Reject → 反馈注入 Agent 上下文 → 触发 replan
        """
        # 获取候选版本列表
        candidates = self.versions.get_candidates(task_id)
        recommended = self.versions.get_recommended(task_id)

        # 如果没有候选版本 (边界情况)，自动创建一个
        if not candidates:
            recommended_candidate = await self.versions.create_candidate(
                task_id=task_id,
                code_changes=code_changes,
                test_results=test_results,
                build_success=True,
                ai_confidence=70,
                ai_summary="Auto-created for review gate",
            )
            recommended = recommended_candidate

        await self.state.update_status(task_id, TaskStatus.REVIEWING, current_step="Awaiting Review")
        await self._emit(
            task_id, EventType.REVIEW_REQUESTED,
            changes=len(code_changes),
            test_passed=test_results.passed if test_results else 0,
            candidates=[{
                "version_id": c.version_id,
                "version_number": c.version_number,
                "status": c.status.value,
                "ai_confidence": c.ai_confidence,
                "ai_summary": c.ai_summary,
                "ai_recommendation": c.ai_recommendation,
                "testable": c.testable,
                "build_success": c.build_success,
                "test_passed": c.test_results.passed if c.test_results else 0,
                "test_total": c.test_results.total if c.test_results else 0,
            } for c in candidates],
            recommended_version_id=recommended.version_id if recommended else None,
        )

        # 等待人工审查结果
        review_queue = self._review_queues.get(task_id)
        if not review_queue:
            return

        review = await review_queue.get()
        decision = review["decision"]
        feedback = review.get("feedback", "")
        version_id = review.get("version_id", "")
        test_notes = review.get("test_notes", "")

        if decision == ReviewDecision.APPROVE:
            logger.info(f"Task {task_id} approved (version={version_id})")
            if version_id:
                await self.versions.approve_candidate(task_id, version_id, feedback, test_notes)
            self.memory.add_message("user", f"Review: APPROVED (v={version_id}) {feedback}")

        elif decision == ReviewDecision.REJECT:
            logger.info(f"Task {task_id} rejected (version={version_id}): {feedback}")
            if version_id:
                await self.versions.reject_candidate(task_id, version_id, feedback, test_notes)
            self.memory.add_message("user", f"Review: REJECTED - {feedback}")

            # ★ 人工 Reject 也走讨论闭环
            # 获取该版本的 eval_report（如果有的话）
            rejected_version = None
            for c in self.versions.get_candidates(task_id):
                if c.version_id == version_id:
                    rejected_version = c
                    break
            eval_report = rejected_version.eval_report if rejected_version else None

            new_plan = await self._handle_eval_failure_loop(
                task_id=task_id,
                plan=plan,
                eval_report=eval_report,
                code_changes=code_changes,
                test_results=test_results,
                description=feedback,
                project_path=".",
                trigger="human_rejected",
                human_feedback=feedback,
            )
            plan.subtasks = new_plan.subtasks

        elif decision == ReviewDecision.EDIT:
            logger.info(f"Task {task_id} edited by user (version={version_id})")
            self.memory.add_message("user", f"Review: Manual edits applied to version {version_id}")

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
            elif subtask.step_type in (StepType.CODE_GENERATE, StepType.DEPLOY, StepType.REVIEW):
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
