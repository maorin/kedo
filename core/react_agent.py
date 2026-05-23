"""
ReactAgent — LLM 驱动的 ReAct Agent

取代刚性流水线 agent_loop.py，用 Think→Act→Observe 循环
让 LLM 自主决定每一步做什么。

工作流:
    用户输入 → Agent 思考(LLM) → 选择工具 → 执行 → 观察结果 → ... → respond
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from pathlib import Path
from typing import Any, Optional

from api.schemas import (
    EventType,
    LLMResponse,
    TaskStatus,
    ToolCallData,
)
from core.memory import AgentMemory
from core.state_manager import StateManager
from tools.base import ToolRegistry, ToolResult

logger = logging.getLogger(__name__)


# ============================================================
# System Prompt
# ============================================================

SYSTEM_PROMPT_TEMPLATE = """\
You are **kedo** — an AI-powered automated development assistant.
You are NOT Qwen, ChatGPT, or any other model's persona. You are kedo.
Current LLM engine: {model_name}.

## Your Capabilities

You have access to tools for reading/writing files, executing shell commands, \
searching the codebase, generating code, building projects, running tests, and more.

## How to Work

**For simple questions or chat**: respond directly using the `respond` tool. No need \
for file operations or planning.

**For small code changes** (bug fixes, minor edits): read the relevant files, make \
the change, build to verify, then respond with what you did.

**For larger development tasks** (new features, multi-file changes, full projects):
  1. Call `plan_development` to break the requirement into subtasks
  2. For each subtask, use `code_generate` to write code, or `file_write` for config files
  3. Call `build` to compile and check for errors
  4. If build fails, read the error, fix the code, build again
  5. Use `respond` to report what was done

**For "compile" / "build" / "打包" requests**: just call the `build` tool directly.

You decide which approach based on complexity. Simple → direct tools. Complex → plan first.

## Important Rules

1. Always use the `respond` tool to deliver your final answer to the user
2. Read files before modifying them — understand first, then change
3. After writing code, try to build to catch errors early
4. If a build fails, read the error, fix the code, and try again
5. Be concise in tool arguments — don't repeat the entire file when editing
6. Use Chinese (中文) when responding to Chinese input
7. Never include API keys or secrets in code files
8. **Emit at most 5 tool_call blocks per response.** Batches larger than that \
get truncated. If you need more operations, do them in separate turns.

## Scope Awareness — 遇到以下"超纲" 情况立刻停手并用 `propose_alternatives`：

- 需要**交叉编译一个第三方 C 库**（如 libnfs/openssl 到 Switch/ARM）—— \
这超出单 agent 能力范围，靠 `shell_execute` 一路硬试会失败 N 次并污染项目
- 需要 **sudo / 修改系统路径** 才能继续（shell sandbox 已经挡了）
- 需要**在项目根 git clone 第三方仓库**（已经挡了，会收到 refused 错误）
- 需要**访问外部网络资源**（下载/curl）才能修复当前问题

这些场景请调用 `propose_alternatives`，给用户 2-3 个选项让其决定：
  (a) 使用 **local stub** 替代第三方依赖
  (b) 请用户在系统层面预置好依赖（如装 switch-libnfs 包）后重试
  (c) 换一条技术路线（如 SMB 替代 NFS、服务器转码替代客户端解码）

不要自己硬试 — 硬试会烧 LLM 额度、污染项目、最终仍然失败。

{project_context}
"""


class ReactAgent:
    """
    LLM 驱动的 ReAct Agent

    ReAct 循环: LLM → tool_call → execute → observe → LLM → ...
    直到 LLM 返回纯文本（无工具调用）或调用 respond 工具。
    """

    def __init__(
        self,
        llm_client,
        tool_registry: ToolRegistry,
        state: StateManager,
        memory: AgentMemory,
        config: dict = None,
        reject_tracker=None,
        role_swap=None,
        reviewer=None,
    ):
        self.llm = llm_client
        self.tools = tool_registry
        self.state = state
        self.memory = memory
        self.config = config or {}
        # 跨工具/Agent 共享的 reject 计数器；屏蔽 respond 直到 Producer 升级
        self._reject_tracker = reject_tracker
        # Producer ↔ Reviewer 角色对换管理器（可为 None）
        self._role_swap = role_swap
        # 独立审查 Reviewer（方案 C）— 主循环里 build 失败 N 连击时调
        # advise_on_stuck_build 拿指令性反馈回灌给 Producer。可为 None。
        self._reviewer = reviewer

        # 可配置参数
        self.max_turns: int = self.config.get("max_agent_turns", 50)
        self.max_consecutive_errors: int = 3
        # 12000 字符约 300-400 行 C 代码；4000 太小，源文件读取一直被中间截断
        self.max_tool_output_chars: int = self.config.get("max_tool_output_chars", 12000)
        # Fix 2: 单次 LLM 响应里最多允许的 tool_call 数量（防"一次塞 155 个"风暴）
        self.max_tool_calls_per_turn: int = self.config.get("max_tool_calls_per_turn", 10)
        # 收敛检测：同 (tool_name, error_fingerprint) 在最近 K 次失败中重复 N 次 → escalate
        self.convergence_window: int = self.config.get("convergence_window", 4)
        self.convergence_threshold: int = self.config.get("convergence_threshold", 3)

        # function calling 是否可用（首次调用时探测）
        self._function_calling_available: Optional[bool] = None
        # 当前任务 ID（供 _text_react_call 内推送事件用）
        self._current_task_id: Optional[str] = None
        # 收敛检测历史：每条工具失败追加 (tool_name, error_fingerprint)
        self._failure_fingerprints: list[tuple[str, str]] = []
        # build 失败连击 → Reviewer 介入：阈值默认 3，介入后 reset 给 Producer 再试一轮
        self.build_stuck_threshold: int = self.config.get("build_stuck_review_threshold", 3)
        self._build_failure_count: int = 0
        self._build_recent_errors: list[str] = []
        self._build_last_command: str = ""
        self._reviewer_interventions: int = 0
        self.max_reviewer_interventions: int = self.config.get(
            "max_reviewer_build_interventions", 2
        )

        # 兼容旧接口: agent_loop 暴露了 planner._llm 供路由层使用
        # 这里提供一个简单的兼容属性
        self._planner_compat = type("_Compat", (), {"_llm": llm_client})()

    @property
    def planner(self):
        """兼容旧代码中 _agent_loop.planner._llm 的访问"""
        return self._planner_compat

    # ==========================================================
    # 公开接口
    # ==========================================================

    async def start_task(
        self,
        task_id: str,
        description: str,
        project_path: str,
    ):
        """创建任务并启动 Agent 循环（异步后台运行）"""
        await self.state.create_task(task_id, description, self.config)
        handle = asyncio.create_task(self._run_safe(task_id, description, project_path))
        # 注册给 StateManager，让 cancel_task 能强杀
        self.state.register_task_handle(task_id, handle)

    async def resume_from_checkpoint(self, task_id: str, additional_context: str = ""):
        """
        从 checkpoint 恢复 ReAct 循环。
        - 加载 checkpoint.messages 还原对话历史
        - additional_context 作为新 user 消息追加（用户给出的指导）
        - 异步起新 _loop 继续推进

        前置：checkpoint 必须包含 messages（M2 起 _save_checkpoint 已保存）。
        """
        checkpoint = await self.state.load_checkpoint(task_id)
        if not checkpoint:
            raise ValueError(f"No checkpoint found for task {task_id}")
        if not checkpoint.messages:
            raise ValueError(
                f"Checkpoint for task {task_id} has no messages history "
                f"(was saved by old AgentLoop or pre-M2 ReactAgent). Cannot resume."
            )

        messages = list(checkpoint.messages)
        if additional_context:
            messages.append({
                "role": "user",
                "content": f"[续接补充] {additional_context}",
            })

        project_path = checkpoint.project_path or "."
        description = checkpoint.description or ""

        # 状态：unpause + 标 in_progress（防止 wait_if_paused 卡住）
        if task_id not in self.state._pause_events:
            self.state._pause_events[task_id] = asyncio.Event()
        self.state._pause_events[task_id].set()
        await self.state.update_status(
            task_id, TaskStatus.IN_PROGRESS, current_step="Resuming"
        )
        await self._emit(task_id, EventType.STEP_STARTED, step="agent_loop_resume")

        # 重置 ReactAgent per-task 状态
        self._current_task_id = task_id
        self._failure_fingerprints = []
        self._prose_retry_done = False

        # resume 重起前清旧 cancel_flag，避免一上来就退
        self.state.clear_cancel_flag(task_id)

        # 异步起 loop + 注册 handle 给 StateManager
        handle = asyncio.create_task(self._resume_run_safe(task_id, messages, project_path, description))
        self.state.register_task_handle(task_id, handle)
        logger.info(
            f"ReactAgent resumed task {task_id}: {len(messages)} messages restored"
            + (f", additional_context={additional_context[:80]}" if additional_context else "")
        )

    async def _resume_run_safe(self, task_id: str, messages: list[dict], project_path: str, description: str):
        """resume 路径的安全包装，与 _run_safe 对称"""
        try:
            result = await self._loop(task_id, messages, project_path)
            # 用户主动停止：状态由 cancel_task 已设置 FAILED + Cancelled，这里别覆盖
            if self.state.is_cancelled(task_id) or result == "Cancelled by user":
                return
            if result.startswith(("(LLM error", "Stopping:", "Reached maximum")):
                await self.state.update_status(task_id, TaskStatus.FAILED, current_step=result[:100])
                await self._emit(task_id, EventType.STEP_FAILED, step="agent_loop_resume", error=result[:200])
            else:
                await self.state.update_status(task_id, TaskStatus.COMPLETED, progress_percent=100, current_step="Completed")
                await self._emit(task_id, EventType.STEP_COMPLETED, step="agent_loop_resume", output=result[:200] if result else "")
        except asyncio.CancelledError:
            logger.info(f"Resume task {task_id} hard-cancelled (asyncio.CancelledError)")
            # 状态由 cancel_task 已设置；不再覆盖
            raise
        except Exception as e:
            logger.error(f"Resume task {task_id} failed: {e}", exc_info=True)
            await self.state.update_status(task_id, TaskStatus.FAILED, current_step=f"Resume error: {str(e)[:100]}")
            await self._emit(task_id, EventType.STEP_FAILED, error=str(e))

    async def _run_safe(self, task_id: str, description: str, project_path: str):
        """安全包装：捕获所有异常，确保任务状态更新"""
        try:
            result = await self.run(task_id, description, project_path)
            logger.info(f"Task {task_id} completed: {result[:100] if result else '(empty)'}...")
        except asyncio.CancelledError:
            logger.info(f"Task {task_id} hard-cancelled (asyncio.CancelledError)")
            # 状态由 cancel_task 已设置；不再覆盖
            raise
        except Exception as e:
            logger.error(f"Task {task_id} failed with exception: {e}", exc_info=True)
            await self.state.update_status(
                task_id, TaskStatus.FAILED, current_step=f"Error: {str(e)[:100]}"
            )
            await self._emit(task_id, EventType.STEP_FAILED, error=str(e))

    async def run(
        self,
        task_id: str,
        description: str,
        project_path: str,
    ) -> str:
        """主入口: 构建上下文 → 启动 ReAct 循环"""
        self._current_task_id = task_id
        # 重置收敛检测历史（每个新 task 都从空开始）
        self._failure_fingerprints = []
        # 重置 prose 收尾重试标志
        self._prose_retry_done = False
        await self.state.update_status(
            task_id, TaskStatus.IN_PROGRESS, current_step="Thinking"
        )
        await self._emit(task_id, EventType.STEP_STARTED, step="agent_loop")

        # 构建 system prompt
        project_context = await self._gather_project_context(project_path)
        system_prompt = self._build_system_prompt(project_context)

        # 任务链上下文继承：检测同项目最近 failed/paused 的 task，注入摘要
        prior_context = await self._gather_prior_task_context(task_id, project_path)
        if prior_context:
            system_prompt = system_prompt + "\n\n" + prior_context

        # 初始消息
        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": description},
        ]

        # 运行 ReAct 循环
        result = await self._loop(task_id, messages, project_path)

        # 用户主动停止：cancel_task 已经设置好状态，别覆盖（也别走 STEP_COMPLETED 路径）
        if self.state.is_cancelled(task_id) or result == "Cancelled by user":
            return result

        # 判断结果：LLM error / max turns = 失败，其他 = 成功
        if result.startswith("(LLM error") or result.startswith("Stopping:") or result.startswith("Reached maximum"):
            await self.state.update_status(
                task_id, TaskStatus.FAILED, current_step=result[:100]
            )
            await self._emit(task_id, EventType.STEP_FAILED, step="agent_loop", error=result[:200])
        else:
            await self.state.update_status(
                task_id, TaskStatus.COMPLETED, progress_percent=100, current_step="Completed"
            )
            await self._emit(task_id, EventType.STEP_COMPLETED, step="agent_loop", output=result[:4000] if result else "")

        return result

    # ==========================================================
    # ReAct 核心循环
    # ==========================================================

    async def _loop(
        self,
        task_id: str,
        messages: list[dict],
        project_path: str,
    ) -> str:
        """
        ReAct 循环:
        1. 调用 LLM (带工具定义)
        2. 如果 LLM 返回工具调用 → 执行 → 追加结果 → 回到 1
        3. 如果 LLM 返回纯文本 → 结束
        4. 如果 respond 工具被调用 → 提取消息 → 结束
        """
        tool_schemas = self.tools.get_schemas()
        consecutive_errors = 0

        for turn in range(self.max_turns):
            # 检查暂停
            await self.state.wait_if_paused(task_id)

            # 检查取消（pause 唤醒后第一时间看，命中即优雅退出）
            if self.state.is_cancelled(task_id):
                logger.info(f"ReactAgent[{task_id}] cancellation flag detected, exiting loop")
                await self._emit(
                    task_id, EventType.STEP_FAILED,
                    step="cancelled",
                    error="Cancelled by user",
                )
                return "Cancelled by user"

            # 更新进度
            progress = min(turn / self.max_turns * 100, 95)
            await self.state.update_status(
                task_id, TaskStatus.IN_PROGRESS,
                progress_percent=progress,
                current_step=f"Turn {turn + 1}",
            )

            # 调用 LLM
            response = await self._call_llm(task_id, messages, tool_schemas, turn)

            # Case 1: 无工具调用
            if not response.tool_calls:
                text = response.content or ""
                # 子情况 1a: 文本里出现了 ```tool_call 但解析为 0
                # → 不是 LLM 真的想结束，而是 fence 未闭合 / JSON 坏掉 / 块内塞多个对象失败
                # 把半截 assistant 消息留住，回灌纠错指令再循环
                if "```tool_call" in text and not self._function_calling_available:
                    logger.warning("Detected malformed tool_call fence; instructing LLM to retry.")
                    messages.append({"role": "assistant", "content": text})
                    messages.append({
                        "role": "user",
                        "content": (
                            "[System] 上一条消息里检测到 ```tool_call``` 块，但格式不合法"
                            "（fence 未闭合、JSON 解析失败或同一块内塞了多个对象）。\n"
                            "请重新输出，遵守以下约束：\n"
                            "1) 每个工具调用必须是独立的 ```tool_call``` 起、```闭合 的代码块；\n"
                            "2) 一个块只放一个 JSON 对象，形如 {\"name\": \"...\", \"arguments\": {...}}；\n"
                            "3) 多个工具请分多个块输出；\n"
                            "4) 如果你想结束并直接回复用户，请用 respond 工具。"
                        ),
                    })
                    consecutive_errors += 1
                    if consecutive_errors >= self.max_consecutive_errors:
                        error_msg = (
                            f"Stopping: {consecutive_errors} consecutive malformed tool_call outputs."
                        )
                        await self._emit(task_id, EventType.STEP_FAILED, error=error_msg)
                        return error_msg
                    continue
                # 子情况 1b: 内容为空 + 无 tool_call → 不是 LLM 主动结束，是上游异常/截断
                # 不能当成 success，回灌一条 user 提示重发；累计到 max_consecutive_errors 后标 failed
                if not text.strip():
                    logger.warning("LLM returned empty content with no tool_calls; treating as transient error and retrying.")
                    messages.append({
                        "role": "user",
                        "content": (
                            "[System] 上一次模型回复为空，且没有工具调用。这通常意味着请求被截断或上游异常。\n"
                            "请重新输出：要么用 ```tool_call``` 调用一个工具继续推进，"
                            "要么用 respond 工具给出最终回复。"
                        ),
                    })
                    consecutive_errors += 1
                    if consecutive_errors >= self.max_consecutive_errors:
                        error_msg = (
                            f"Stopping: {consecutive_errors} consecutive empty LLM responses."
                        )
                        await self._emit(task_id, EventType.STEP_FAILED, error=error_msg)
                        return error_msg
                    continue
                # 子情况 1c: 有内容、无 tool_call
                # 这是 Kimi prose 结尾 quirk 的高发区——LLM 用"编译成功 🎉 做了 5 件事"
                # 替代正经调 respond 工具，加上 reasoning_content 截断，常常断在句中。
                # 第一次出现回灌 prompt 强制走 respond；再次出现才接受为真的 final answer。
                if not getattr(self, "_prose_retry_done", False):
                    self._prose_retry_done = True
                    logger.warning("Non-empty content without tool_call; instructing LLM to use respond tool to confirm finality.")
                    messages.append({"role": "assistant", "content": text})
                    messages.append({
                        "role": "user",
                        "content": (
                            "[System] 你刚才输出了文本但没用任何工具。这种 prose 结尾在系统里"
                            "无法区分「你真的完成了」还是「你被截断了」。\n\n"
                            "请二选一：\n"
                            "1) 任务真的完成了 → 调用 respond 工具，message 字段写最终汇报；\n"
                            "2) 还有事要做 → 继续调相应工具（build / file_read / auto_fix / ...）。\n\n"
                            "下一条回复必须包含一个 tool_call 块。"
                        ),
                    })
                    continue
                # 已经回灌过一次还是 prose 结尾 → 接受为最终答案（避免无限循环）
                logger.info("Accepting prose ending after one retry.")
                self._prose_retry_done = False  # 为下个 task 重置
                return text

            # Case 2: 有工具调用 → 执行
            # Fix 2: 单次响应 tool_call 数量上限 — 防 LLM 塞 155 个 shell_execute 那种风暴
            if len(response.tool_calls) > self.max_tool_calls_per_turn:
                logger.warning(
                    f"LLM returned {len(response.tool_calls)} tool_calls in one response; "
                    f"truncating to {self.max_tool_calls_per_turn}"
                )
                truncated_calls = response.tool_calls[:self.max_tool_calls_per_turn]
                response.tool_calls = truncated_calls
            # 先把 assistant 消息追加到历史
            assistant_msg = self._format_assistant_message(response)
            messages.append(assistant_msg)

            # 标记是否因工具早停需要跳出 turn 循环
            early_stop_reason: Optional[str] = None

            for tc in response.tool_calls:
                # respond 工具特殊处理 → 直接结束
                if tc.name == "respond":
                    # 升级护栏：commit_candidate 多次被 Reviewer 拒后，禁止 Producer
                    # 用 respond 当逃生路径直接报告完成。必须先 pause_for_human /
                    # propose_alternatives 升级，才能解锁 respond。
                    if (
                        self._reject_tracker is not None
                        and self._reject_tracker.should_block_respond(task_id)
                    ):
                        n = self._reject_tracker.reject_count(task_id)
                        thr = self._reject_tracker.escalation_threshold
                        block_msg = (
                            f"`respond` is BLOCKED: Reviewer has rejected commit_candidate "
                            f"{n} times (threshold {thr}) and the findings remain unresolved.\n"
                            f"You MUST call ONE of these tools first, then you can call respond:\n"
                            f"  - `pause_for_human` — ask user for guidance / new approach\n"
                            f"  - `propose_alternatives` — offer 2-3 concrete paths for user to pick\n"
                            f"Do NOT declare the task done while critical Reviewer findings "
                            f"(missed requirements / risks) are still on the table."
                        )
                        logger.warning(
                            f"ReactAgent[{task_id}] blocked respond — {n} rejects pending escalation"
                        )
                        await self._emit(
                            task_id, EventType.STEP_FAILED,
                            step="respond_blocked",
                            error=f"respond blocked: {n}/{thr} rejects pending escalation",
                        )
                        messages.append({
                            "role": "tool",
                            "tool_call_id": tc.id,
                            "content": block_msg,
                        })
                        consecutive_errors += 1
                        if consecutive_errors >= self.max_consecutive_errors:
                            early_stop_reason = (
                                f"Stopping: {consecutive_errors} consecutive failures "
                                f"(respond blocked, no escalation called)"
                            )
                            break
                        continue  # 处理下一个 tool_call

                    final_msg = tc.arguments.get("message", "")
                    await self._emit(
                        task_id, EventType.LLM_RESPONSE,
                        phase="respond", summary=final_msg[:4000],
                    )
                    # 推送最终回复内容
                    await self._emit(task_id, EventType.LLM_TOKEN, token=final_msg + "\n", phase="respond")
                    return final_msg

                # 执行工具
                result = await self._execute_tool(task_id, tc, project_path)

                # 升级解锁：成功调了 pause_for_human / propose_alternatives 后清 unhandled flag
                if result.success and self._reject_tracker is not None:
                    from core.reject_tracker import ESCALATION_TOOL_NAMES
                    if tc.name in ESCALATION_TOOL_NAMES:
                        self._reject_tracker.on_escalate(task_id)
                        # 同时还原 role swap：用户人工介入 → 回到原始 LLM 配置
                        if (
                            self._role_swap is not None
                            and self._role_swap.is_swapped(task_id)
                        ):
                            self._role_swap.restore(task_id)
                tool_output = result.output if result.success else (result.error or "Unknown error")

                # 追加 tool result 到消息历史
                truncated_output = self._truncate(tool_output, self.max_tool_output_chars)
                messages.append({
                    "role": "tool",
                    "tool_call_id": tc.id,
                    "content": truncated_output,
                })

                # 错误计数 + 收敛检测指纹
                if result.success:
                    consecutive_errors = 0
                    if tc.name == "build":
                        # build 通过 → 清掉之前积累的"卡死 build"状态
                        self._build_failure_count = 0
                        self._build_recent_errors.clear()
                        self._reviewer_interventions = 0
                        # T2 (虚拟测试 Phase B): build 通过后自动跑一次 host_test
                        # 仅当 profile.host_test.enabled=true && auto_run_after_build=true
                        await self._maybe_auto_run_host_test(
                            task_id=task_id,
                            messages=messages,
                            project_path=project_path,
                        )
                else:
                    # 方案 C：build 失败连击 → Reviewer 介入给指令性反馈。必须在
                    # consecutive_errors++ 之前，介入成功就 reset 错误计数让 Producer
                    # 拿建议有空间继续。
                    if tc.name == "build":
                        self._record_build_failure(result)
                        intervened = await self._maybe_invoke_reviewer_for_build(
                            task_id=task_id,
                            messages=messages,
                            project_path=project_path,
                        )
                        if intervened:
                            consecutive_errors = 0
                            # 跳过本次的 fingerprint 累积 + 早停 / 收敛检测
                            continue
                    consecutive_errors += 1
                    # Fix 4: 空 error 用 [empty] 占位，让 convergence 能识别"反复返空"
                    fp = self._error_fingerprint(result.error or "") or "[empty]"
                    self._failure_fingerprints.append((tc.name, fp))
                    # 只保留最近 N 条避免无限增长
                    if len(self._failure_fingerprints) > self.convergence_window * 2:
                        self._failure_fingerprints = self._failure_fingerprints[-self.convergence_window * 2:]
                    # Fix 1: tool_call 循环内失败早停 — 达阈值立刻跳出，不跑完全部
                    if consecutive_errors >= self.max_consecutive_errors:
                        early_stop_reason = f"Stopping: {consecutive_errors} consecutive tool failures."
                        logger.warning(f"Early stop: {early_stop_reason} (after {tc.name})")
                        break
                    # Fix 4 收敛检测也提前触发
                    if self._convergence_detected():
                        stuck_tool, stuck_fp = self._failure_fingerprints[-1]
                        early_stop_reason = f"Stopping: convergence detected on {stuck_tool}"
                        logger.warning(f"Early stop: {early_stop_reason}")
                        break

            # 如果在 tool_call 循环里早停，跳出 turn 循环
            if early_stop_reason:
                await self._emit(task_id, EventType.STEP_FAILED, error=early_stop_reason)
                return early_stop_reason

            # 连续错误上限
            if consecutive_errors >= self.max_consecutive_errors:
                error_msg = f"Stopping: {consecutive_errors} consecutive tool failures."
                await self._emit(task_id, EventType.STEP_FAILED, error=error_msg)
                return error_msg

            # 收敛检测：同 (tool, similar_fp) 在最近窗口里重复 N 次 → 自动 escalate 给人工
            if self._convergence_detected():
                stuck_tool, stuck_fp = self._failure_fingerprints[-1]
                msg = (
                    f"Convergence detected: {stuck_tool} 连续失败且错误指纹相似 "
                    f"({self.convergence_threshold} 次以上)，疑似进入死循环。"
                )
                logger.warning(msg)
                await self._emit(
                    task_id, EventType.STEP_FAILED,
                    step="agent_loop",
                    error=msg,
                    escalation="paused_for_human",
                    suggestion=(
                        f"Agent 反复尝试 {stuck_tool} 没解决同一个错误。建议：检查最后一次错误信息，"
                        f"提供新的修复方向（换库 / 改架构 / 简化需求），然后 resume 任务。"
                    ),
                )
                await self.state.pause_task(task_id)
                # 用 "Stopping:" 前缀让 run() 标 failed
                return f"Stopping: convergence detected on {stuck_tool} ({self.convergence_threshold}+ similar failures)"

            # 定期 checkpoint
            if turn > 0 and turn % 5 == 0:
                # 从 messages 提取原始 user 描述（messages[1] 通常是首条 user）
                desc = ""
                for m in messages[:3]:
                    if m.get("role") == "user":
                        desc = (m.get("content") or "")[:1000]
                        break
                await self._save_checkpoint(task_id, messages, project_path=project_path, description=desc)

        return "Reached maximum turns limit."

    # ==========================================================
    # LLM 调用
    # ==========================================================

    async def _call_llm(
        self,
        task_id: str,
        messages: list[dict],
        tools: list[dict],
        turn: int,
    ) -> LLMResponse:
        """单次 LLM 调用，发射事件。自动探测并回退到文本 ReAct。"""
        model_name = getattr(self.llm, "model", "unknown")

        await self._emit(
            task_id, EventType.LLM_REQUEST,
            phase="agent", turn=turn + 1,
            prompt_summary=f"Turn {turn + 1}",
            model=model_name,
        )

        t0 = time.monotonic()

        # 策略选择: native function calling vs text ReAct
        if self._function_calling_available is None:
            # 首次调用: 尝试 native function calling
            try:
                response = await self.llm.chat_with_tools(messages, tools)
                self._function_calling_available = True
                logger.info("Function calling available, using native mode")
            except Exception as e:
                err_str = str(e)
                if "403" in err_str or "Forbidden" in err_str:
                    # 403 = 端点不支持 function calling，永久切换到文本 ReAct
                    logger.warning(f"Function calling not supported (403), switching to text ReAct permanently")
                    self._function_calling_available = False
                    response = await self._text_react_call(messages, tools)
                else:
                    # 网络错误等临时问题，不标记，直接用文本 ReAct 本次
                    logger.warning(f"Function calling probe failed ({e}), trying text ReAct this turn")
                    response = await self._text_react_call(messages, tools)
        elif self._function_calling_available:
            try:
                response = await self.llm.chat_with_tools(messages, tools)
            except Exception as e:
                logger.warning(f"Function calling failed ({e}), falling back to text ReAct")
                response = await self._text_react_call(messages, tools)
        else:
            response = await self._text_react_call(messages, tools)

        elapsed_ms = (time.monotonic() - t0) * 1000

        # 发射响应事件 — summary 不再源头截断，REPL 按 verbose 决定显示
        summary = response.content or ""
        if response.tool_calls:
            tool_names = ", ".join(tc.name for tc in response.tool_calls)
            summary = f"[tools: {tool_names}] {summary}"

        await self._emit(
            task_id, EventType.LLM_RESPONSE,
            phase="agent", turn=turn + 1,
            summary=summary,
            elapsed_ms=round(elapsed_ms),
            tool_count=len(response.tool_calls),
        )

        return response

    # ==========================================================
    # 文本 ReAct 模式 (function calling 不可用时的回退)
    # ==========================================================

    def _build_text_react_tools_prompt(self, tools: list[dict]) -> str:
        """将工具 schema 转为精简文本描述（控制 token 开销）"""
        lines = ["## Tools\n"]
        for t in tools:
            func = t.get("function", {})
            name = func.get("name", "")
            desc = func.get("description", "")[:80]
            params = func.get("parameters", {}).get("properties", {})
            param_list = ", ".join(f'{k}: {v.get("type","str")}' for k, v in params.items())
            lines.append(f"- **{name}**({param_list}) — {desc}")

        lines.append("""
## Tool Call Format — STRICT

Each tool call MUST be its own fenced block, opened with ```tool_call and closed with ```.
Exactly ONE JSON object per block. Multiple tools = multiple blocks.

✅ Correct (two reads = two blocks, both closed):
```tool_call
{"name": "file_read", "arguments": {"file_path": "src/main.cpp"}}
```
```tool_call
{"name": "file_read", "arguments": {"file_path": "CMakeLists.txt"}}
```

❌ Wrong — two JSON in one block:
```tool_call
{"name": "file_read", "arguments": {"file_path": "a"}}
{"name": "file_read", "arguments": {"file_path": "b"}}
```

❌ Wrong — fence not closed (parser will reject and force a retry).

Final answer to user — MUST use respond tool, also in its own closed block:
```tool_call
{"name": "respond", "arguments": {"message": "回复内容"}}
```

IMPORTANT: You MUST output properly closed ```tool_call``` blocks to act. Do NOT just describe what to do in prose.""")
        return "\n".join(lines)

    async def _text_react_call(
        self, messages: list[dict], tools: list[dict]
    ) -> LLMResponse:
        """文本 ReAct: 通过 prompt 注入工具描述，解析 LLM 文本中的工具调用"""
        # 在 system prompt 后追加工具描述
        tools_prompt = self._build_text_react_tools_prompt(tools)
        enhanced_messages = list(messages)

        # 找到 system message 并追加工具描述
        for i, m in enumerate(enhanced_messages):
            if m["role"] == "system":
                enhanced_messages[i] = {
                    "role": "system",
                    "content": m["content"] + "\n\n" + tools_prompt,
                }
                break
        else:
            # 没有 system message，加到开头
            enhanced_messages.insert(0, {"role": "system", "content": tools_prompt})

        # 将 tool result 消息转为 user 消息（普通 chat 不支持 tool 角色）
        converted = []
        for m in enhanced_messages:
            if m.get("role") == "tool":
                converted.append({
                    "role": "user",
                    "content": f"[Tool Result] {m.get('content', '')}",
                })
            elif m.get("role") == "assistant" and m.get("tool_calls"):
                # 把 assistant 的 tool_calls 转为文本
                tc_text = ""
                if m.get("content"):
                    tc_text = m["content"] + "\n"
                for tc in m["tool_calls"]:
                    func = tc.get("function", tc)
                    name = func.get("name", "")
                    args = func.get("arguments", "{}")
                    if isinstance(args, str):
                        tc_text += f"\n```tool_call\n{{\"name\": \"{name}\", \"arguments\": {args}}}\n```\n"
                    else:
                        tc_text += f"\n```tool_call\n{json.dumps({'name': name, 'arguments': args}, ensure_ascii=False)}\n```\n"
                converted.append({"role": "assistant", "content": tc_text})
            else:
                converted.append(m)

        # 带重试的 LLM 调用 — 优先流式（可以逐 token 推送事件）
        last_error = None
        for attempt in range(3):
            try:
                if hasattr(self.llm, 'stream_chat'):
                    chunks = []
                    async for token in self.llm.stream_chat(converted):
                        chunks.append(token)
                        # 逐 token 推送到前端（需要 task_id，从闭包外传入）
                        if self._current_task_id:
                            await self._emit(self._current_task_id, EventType.LLM_TOKEN, token=token, phase="agent")
                    text = "".join(chunks)
                else:
                    text = await self.llm.chat(converted)
                break
            except Exception as e:
                last_error = e
                logger.warning(f"Text ReAct LLM call attempt {attempt + 1}/3 failed: {e}")
                if attempt < 2:
                    await asyncio.sleep(2 * (attempt + 1))
        else:
            return LLMResponse(content=f"(LLM error after 3 retries: {last_error})", tool_calls=[])

        logger.warning(f"Text ReAct LLM response (first 500 chars): {text[:500]}")

        # 解析文本中的 tool_call 块
        tool_calls = self._parse_text_tool_calls(text)

        if tool_calls:
            # 有工具调用: 去掉 content 中的 tool_call 块（保留其他文本作为思考过程）
            import re
            clean_content = re.sub(r'```tool_call\s*\n.*?\n```', '', text, flags=re.DOTALL).strip()
            return LLMResponse(content=clean_content or None, tool_calls=tool_calls)
        else:
            # 无工具调用: 纯文本回复
            return LLMResponse(content=text, tool_calls=[])

    @staticmethod
    def _parse_text_tool_calls(text: str) -> list[ToolCallData]:
        """从 LLM 文本输出中解析 ```tool_call``` 块。

        兼容三种异常：
        1. 未闭合 fence（输出被截断或模型遗漏 closing ```）— 用 \\Z 兜底到文末
        2. 同一 fence 内塞多个 JSON 对象（违反 one-tool-per-block 约定）— 按花括号平衡扫描所有顶层 {…}
        3. arguments 字段被序列化为字符串 — 二次 json.loads
        """
        import re
        tool_calls: list[ToolCallData] = []
        pattern = r'```tool_call\s*\n(.*?)(?:\n```|\Z)'
        matches = re.findall(pattern, text, re.DOTALL)

        idx = 0
        for block in matches:
            for obj_text in ReactAgent._scan_json_objects(block):
                try:
                    data = json.loads(obj_text)
                except json.JSONDecodeError as e:
                    logger.warning(f"Failed to parse tool_call json: {obj_text[:120]}... ({e})")
                    continue
                if not isinstance(data, dict):
                    continue
                name = data.get("name", "")
                if not name:
                    continue
                arguments = data.get("arguments", {})
                if not arguments:
                    arguments = {k: v for k, v in data.items() if k != "name"}
                if isinstance(arguments, str):
                    try:
                        arguments = json.loads(arguments)
                    except json.JSONDecodeError:
                        arguments = {"_raw": arguments}
                tool_calls.append(ToolCallData(
                    id=f"text_tc_{idx}",
                    name=name,
                    arguments=arguments,
                ))
                idx += 1
        return tool_calls

    @staticmethod
    def _scan_json_objects(text: str):
        """扫描文本中所有顶层 {...} JSON 对象，处理嵌套花括号与字符串内的引号。"""
        depth = 0
        start = -1
        in_str = False
        escape = False
        for i, ch in enumerate(text):
            if escape:
                escape = False
                continue
            if in_str:
                if ch == '\\':
                    escape = True
                elif ch == '"':
                    in_str = False
                continue
            if ch == '"':
                in_str = True
                continue
            if ch == '{':
                if depth == 0:
                    start = i
                depth += 1
            elif ch == '}':
                if depth > 0:
                    depth -= 1
                    if depth == 0 and start >= 0:
                        yield text[start:i + 1]
                        start = -1

    # ==========================================================
    # 工具执行
    # ==========================================================

    async def _execute_tool(
        self,
        task_id: str,
        tool_call: ToolCallData,
        project_path: str,
    ) -> ToolResult:
        """执行单个工具调用"""
        name = tool_call.name
        args = dict(tool_call.arguments)

        # 完整 args（REPL 端按 verbose 决定截断）
        await self._emit(
            task_id, EventType.TOOL_EXECUTE,
            tool=name, args_summary=str(args),
        )
        await self.state.update_status(
            task_id, TaskStatus.IN_PROGRESS,
            current_step=f"Tool: {name}",
        )

        t0 = time.monotonic()
        try:
            tool = self.tools.get(name)
            if not tool:
                return ToolResult(success=False, error=f"Unknown tool: {name}")

            param_names = [p.name for p in tool.parameters]

            # 强制注入 project_path / task_id —— 不允许 LLM 自行覆盖
            # （之前 LLM 给 plan_development 传 task_id="switch-nfs-player"，
            #  导致 plan 被存到错误 checkpoint，dashboard 永远拿不到。）
            if "project_path" in param_names:
                args["project_path"] = project_path
            if "task_id" in param_names:
                args["task_id"] = task_id

            # 解析 file_path: 如果是相对路径，拼上 project_path
            if "file_path" in args and not os.path.isabs(args["file_path"]):
                args["file_path"] = os.path.join(project_path, args["file_path"])

            # 串接 code_generate 这类内部调 LLM 的工具：把 token 流转发到 LLM_TOKEN 事件
            prev_on_token = None
            restore_on_token = False
            if hasattr(tool, "_on_token"):
                prev_on_token = getattr(tool, "_on_token", None)
                async def _emit_codegen_token(tok: str):
                    await self._emit(task_id, EventType.LLM_TOKEN, token=tok, phase="code_generate")
                tool._on_token = _emit_codegen_token
                restore_on_token = True
                # 同时发一个 LLM_REQUEST 事件让控制台知道开始
                await self._emit(
                    task_id, EventType.LLM_REQUEST,
                    phase="code_generate",
                    prompt_summary=f"{name}: {str(args)[:200]}",
                    model=getattr(self.llm, "model", "unknown"),
                )

            logger.info(f"Tool {name} args: {str(args)[:300]}")
            try:
                result = await self.tools.execute(name, **args)
            finally:
                if restore_on_token:
                    tool._on_token = prev_on_token
        except Exception as e:
            result = ToolResult(success=False, error=str(e))

        elapsed_ms = (time.monotonic() - t0) * 1000

        if result.success:
            logger.info(f"Tool {name}: OK ({elapsed_ms:.0f}ms)")
        else:
            logger.warning(f"Tool {name}: FAIL ({elapsed_ms:.0f}ms) error={result.error}")

        if result.success:
            await self._emit(
                task_id, EventType.STEP_COMPLETED,
                step=f"tool:{name}", elapsed_ms=round(elapsed_ms),
                output_preview=(result.output or ""),
            )
        else:
            await self._emit(
                task_id, EventType.STEP_FAILED,
                step=f"tool:{name}", error=(result.error or ""),
            )

        return result

    # ==========================================================
    # System Prompt 构建
    # ==========================================================

    def _build_system_prompt(self, project_context: dict) -> str:
        model_name = getattr(self.llm, "model", "unknown")

        # 格式化项目上下文
        ctx_parts = []
        if project_context.get("project_path"):
            ctx_parts.append(f"**Project root path: `{project_context['project_path']}`**")
            ctx_parts.append(f"All file operations MUST use this path as the base directory.")
        if project_context.get("file_count"):
            ctx_parts.append(f"Project has {project_context['file_count']} files.")
        if project_context.get("file_tree"):
            ctx_parts.append(f"File structure:\n```\n{project_context['file_tree']}\n```")
        if project_context.get("build_system"):
            ctx_parts.append(f"Build system: {project_context['build_system']}")

        context_str = ""
        if ctx_parts:
            context_str = "## Project Context\n\n" + "\n".join(ctx_parts)

        prompt = SYSTEM_PROMPT_TEMPLATE.format(
            model_name=model_name,
            project_context=context_str,
        )

        # 方案 C：charter 存在时把摘要拼入 system prompt（Producer 视角）
        project_path = project_context.get("project_path")
        if project_path:
            try:
                from core.project_charter import Charter
                charter = Charter.load(project_path)
                if charter is not None:
                    prompt = prompt + "\n\n" + charter.summarize_for_prompt()
            except Exception as e:
                logger.debug(f"ReactAgent: charter load skipped: {e}")
        return prompt

    async def _gather_prior_task_context(self, current_task_id: str, project_path: str) -> str:
        """
        看同项目最近的 failed/paused task，把它的 plan 摘要 + 最后 error fingerprint
        + 已尝试修法注入新 task system prompt，避免 LLM 从零探索同一片雷区。

        返回空串表示没相关历史可继承。
        """
        try:
            tasks = self.state.list_tasks() or []
        except Exception:
            return ""

        # 同项目过滤：暂时按 description 同根目录或 status 不为 completed 都纳入候选
        # 实际项目隔离需要 state 层支持 project_path 字段（M2 阶段加），现在按时间倒序拿最近 3 个
        prior_failed = []
        for t in tasks:
            tid = t.get("task_id")
            if not tid or tid == current_task_id:
                continue
            status = t.get("status", "")
            if status in ("failed", "paused"):
                prior_failed.append(t)

        if not prior_failed:
            return ""

        # 取时间最近的一条（list_tasks 顺序未保证，按 created_at 排）
        prior_failed.sort(key=lambda t: t.get("created_at", ""), reverse=True)
        recent = prior_failed[0]
        recent_id = recent.get("task_id", "")
        recent_step = recent.get("current_step", "")
        recent_desc = (recent.get("description", "") or "")[:300]

        # 尝试拉它的 checkpoint 看 plan + 失败信息
        plan_summary = ""
        try:
            cp = await self.state.load_checkpoint(recent_id)
            if cp and cp.plan and cp.plan.subtasks:
                titles = [s.title for s in cp.plan.subtasks[:8]]
                plan_summary = "尝试过的步骤: " + " → ".join(titles)
        except Exception as e:
            logger.debug(f"prior task checkpoint load failed: {e}")

        return (
            f"## 任务链上下文（同项目最近的失败任务）\n\n"
            f"上一个 task `{recent_id}` ({recent.get('status')}): {recent_desc}\n"
            f"- 卡在: {recent_step[:120]}\n"
            f"- {plan_summary}\n\n"
            f"**注意**：避免重复上次已经证伪的修法。若错误信息一致，先用 `pause_for_human` "
            f"询问用户思路，而不是反复尝试同一类修复。"
        )

    async def _gather_project_context(self, project_path: str) -> dict:
        """收集项目基本信息"""
        ctx: dict[str, Any] = {"project_path": os.path.abspath(project_path)}
        p = Path(project_path)

        if not p.exists():
            return ctx

        # 文件计数和树
        try:
            files = []
            for root, dirs, filenames in os.walk(project_path):
                # 跳过隐藏目录和常见排除项
                dirs[:] = [d for d in dirs if not d.startswith(".") and d not in (
                    "node_modules", "__pycache__", "build", "dist", ".git", ".kedo", "venv", ".venv"
                )]
                rel_root = os.path.relpath(root, project_path)
                for f in filenames:
                    if not f.startswith("."):
                        rel_path = os.path.join(rel_root, f) if rel_root != "." else f
                        files.append(rel_path)

            ctx["file_count"] = len(files)
            # 只显示前 50 个文件
            tree_lines = files[:50]
            if len(files) > 50:
                tree_lines.append(f"... and {len(files) - 50} more files")
            ctx["file_tree"] = "\n".join(tree_lines)
        except Exception as e:
            logger.warning(f"Failed to scan project: {e}")

        # 检测构建系统
        if (p / "CMakeLists.txt").exists():
            ctx["build_system"] = "CMake"
        elif (p / "Makefile").exists():
            ctx["build_system"] = "Make"
        elif (p / "package.json").exists():
            ctx["build_system"] = "npm/Node.js"
        elif (p / "Cargo.toml").exists():
            ctx["build_system"] = "Cargo/Rust"
        elif (p / "setup.py").exists() or (p / "pyproject.toml").exists():
            ctx["build_system"] = "Python"

        return ctx

    # ==========================================================
    # 事件发射
    # ==========================================================

    async def _emit(self, task_id: str, event_type: EventType, **data):
        """发射事件到 EventBus（供 REPL/Dashboard 显示）"""
        from api.schemas import EventType as ET
        from datetime import datetime, timezone

        event_data = {"task_id": task_id, **data}
        try:
            # 使用 state_manager 的 event_bus
            if hasattr(self.state, "event_bus"):
                from api.schemas import AgentEvent
                event = AgentEvent(
                    event_type=event_type,
                    task_id=task_id,
                    timestamp=datetime.now(timezone.utc),
                    data=data,
                )
                await self.state.event_bus.publish(event)
        except Exception as e:
            logger.debug(f"Event emission failed: {e}")

    # ==========================================================
    # Checkpoint
    # ==========================================================

    async def _save_checkpoint(self, task_id: str, messages: list[dict], project_path: str = "", description: str = ""):
        """保存 checkpoint（含完整 messages 历史，供 resume_from_checkpoint 直接还原）"""
        try:
            from api.schemas import AgentCheckpoint
            existing = await self.state.load_checkpoint(task_id)
            checkpoint = AgentCheckpoint(
                task_id=task_id,
                current_step_index=len(messages),
                plan=existing.plan if existing else None,
                memory_snapshot=self.memory.snapshot() if self.memory else {},
                code_changes=existing.code_changes if existing else [],
                test_results=existing.test_results if existing else None,
                eval_report=existing.eval_report if existing else None,
                messages=messages,
                project_path=project_path or (existing.project_path if existing else ""),
                description=description or (existing.description if existing else ""),
            )
            await self.state.save_checkpoint(checkpoint)
        except Exception as e:
            logger.warning(f"Checkpoint save failed: {e}")

    # ==========================================================
    # 辅助方法
    # ==========================================================

    @staticmethod
    def _format_assistant_message(response: LLMResponse) -> dict:
        """将 LLMResponse 转为 OpenAI 格式的 assistant 消息

        含 reasoning_content（DeepSeek-v4-pro / Kimi reasoner 的 thinking trace）时
        透传给下一轮 messages —— 这是 thinking-mode 协议硬要求，否则 provider 返 400。
        OpenAI SDK 1.x 接受 dict 形式 messages 时透传未知字段。
        """
        msg: dict[str, Any] = {"role": "assistant"}
        if response.content:
            msg["content"] = response.content
        if response.reasoning_content:
            msg["reasoning_content"] = response.reasoning_content
        if response.tool_calls:
            msg["tool_calls"] = [
                {
                    "id": tc.id,
                    "type": "function",
                    "function": {
                        "name": tc.name,
                        "arguments": json.dumps(tc.arguments, ensure_ascii=False),
                    },
                }
                for tc in response.tool_calls
            ]
        return msg

    @staticmethod
    def _error_fingerprint(error: str) -> str:
        """归一化 error 文本成指纹，用于收敛检测（去行号/绝对路径/地址/空白）。"""
        if not error:
            return ""
        import re as _re
        s = error
        s = _re.sub(r"\x1b\[[0-9;]*[A-Za-z]", "", s)        # ANSI
        s = _re.sub(r":\d+:\d+", ":N:N", s)                  # file:line:col
        s = _re.sub(r":\d+:", ":N:", s)
        s = _re.sub(r"line \d+", "line N", s)
        s = _re.sub(r"0x[0-9a-fA-F]+", "0xADDR", s)
        s = _re.sub(r"/tmp/[A-Za-z0-9_./-]+", "/tmp/X", s)
        s = _re.sub(r"\s+", " ", s).strip()
        if len(s) > 1500:
            s = s[:750] + " ... " + s[-750:]
        return s

    def _convergence_detected(self) -> bool:
        """最近 convergence_window 条失败里，convergence_threshold 条以上是同 tool + 相似指纹"""
        window = self._failure_fingerprints[-self.convergence_window:]
        if len(window) < self.convergence_threshold:
            return False
        last_tool, last_fp = window[-1]
        if not last_fp:
            return False
        from difflib import SequenceMatcher
        similar = 0
        for tool, fp in window:
            if tool != last_tool or not fp:
                continue
            if fp == last_fp or SequenceMatcher(None, fp, last_fp).ratio() >= 0.85:
                similar += 1
        return similar >= self.convergence_threshold

    # ----------------------------------------------------------
    # 方案 C：build 卡死时 Reviewer 介入
    # ----------------------------------------------------------

    def _record_build_failure(self, result) -> None:
        """累积 build 失败状态供 Reviewer 介入时查看。"""
        self._build_failure_count += 1
        cmd = ""
        if isinstance(getattr(result, "data", None), dict):
            cmd = str(result.data.get("command", "") or "")
        if cmd:
            self._build_last_command = cmd
        err = result.error or result.output or ""
        if err:
            self._build_recent_errors.append(err)
            if len(self._build_recent_errors) > 5:
                self._build_recent_errors = self._build_recent_errors[-5:]

    async def _maybe_invoke_reviewer_for_build(
        self,
        task_id: str,
        messages: list[dict],
        project_path: str,
    ) -> bool:
        """build 失败 ≥ build_stuck_threshold 时调 Reviewer，把指令性反馈回灌到 messages。
        返回 True 表示介入了（调用方应 reset consecutive_errors 让 Producer 拿建议再试）。
        介入次数有上限，超过则不再介入，把空间让给收敛检测自然 escalate。"""
        if self._reviewer is None or not getattr(self._reviewer, "is_active", False):
            return False
        if self._build_failure_count < self.build_stuck_threshold:
            return False
        if self._reviewer_interventions >= self.max_reviewer_interventions:
            logger.info(
                f"Reviewer build-advice cap reached ({self._reviewer_interventions}); "
                f"letting convergence detector escalate instead."
            )
            return False

        # 提取任务描述（messages[1] 通常是首条 user）
        task_desc = ""
        for m in messages[:4]:
            if m.get("role") == "user":
                c = m.get("content")
                if isinstance(c, str) and c.strip():
                    task_desc = c.strip()
                    break

        # 提取 Producer 最近 8 个 tool_call 摘要
        recent_actions: list[str] = []
        for m in messages[-30:]:
            if m.get("role") != "assistant":
                continue
            for tc in (m.get("tool_calls") or []):
                fn = (tc.get("function") or {}).get("name") or tc.get("name") or "?"
                args = (tc.get("function") or {}).get("arguments") or tc.get("arguments") or ""
                if isinstance(args, dict):
                    args = str(args)
                recent_actions.append(f"{fn} {str(args)[:120]}")
        recent_actions = recent_actions[-8:]

        wt_summary = self._build_working_tree_summary(project_path)

        try:
            advice = await self._reviewer.advise_on_stuck_build(
                task_description=task_desc or "(task description not found in messages)",
                build_command=self._build_last_command,
                recent_errors=list(self._build_recent_errors),
                working_tree_summary=wt_summary,
                recent_actions=recent_actions,
                project_path=project_path,
            )
        except Exception as e:
            logger.warning(f"Reviewer.advise_on_stuck_build raised unexpectedly: {e}")
            return False

        # 用 user 角色回灌：function-calling 模式下 mid-conversation 的 system msg
        # 模型不一定尊重，user 角色最稳
        feedback_block = (
            f"[Independent Reviewer feedback — build has failed "
            f"{self._build_failure_count} times in a row]\n\n"
            f"{advice}\n\n"
            f"(This is from a separate model acting as reviewer. Treat it as a directive, "
            f"not a suggestion. If it tells you to revert or escalate, do that next.)"
        )
        messages.append({"role": "user", "content": feedback_block})

        self._reviewer_interventions += 1
        prev_count = self._build_failure_count
        # 介入后 reset 计数，给 Producer 一轮空间执行建议
        self._build_failure_count = 0

        await self._emit(
            task_id, EventType.STEP_COMPLETED,
            step="reviewer_build_advice",
            intervention=self._reviewer_interventions,
            failures_before_advice=prev_count,
            advice_preview=advice[:200],
        )
        logger.info(
            f"Reviewer intervened for stuck build "
            f"(intervention #{self._reviewer_interventions}, after {prev_count} failures)"
        )
        return True

    async def _maybe_auto_run_host_test(
        self,
        task_id: str,
        messages: list[dict],
        project_path: str,
    ) -> None:
        """T2 (虚拟测试 Phase B) — build 通过后自动跑 host_test。

        触发条件：profile.host_test.enabled 且 auto_run_after_build。
        结果以 user 角色追加到 messages，让 LLM 能看到（function calling 模式下
        system message 不一定被尊重）。失败不阻塞主循环，让 Producer 自己决定
        如何处理。
        """
        host_tool = self.tools.get("host_test")
        if host_tool is None:
            return

        from pathlib import Path
        import json as _json
        try:
            pf_path = Path(project_path) / ".kedo" / "project_profile.json"
            if not pf_path.exists():
                return
            profile = _json.loads(pf_path.read_text(encoding="utf-8", errors="replace"))
        except Exception:
            return

        host_cfg = profile.get("host_test") or {}
        if not host_cfg.get("enabled"):
            return
        if not host_cfg.get("auto_run_after_build", True):
            return

        try:
            await self._emit(
                task_id, EventType.STEP_STARTED,
                step="host_test_auto",
                summary="T2: running host mock + ASAN after build",
            )
            ht_result = await host_tool.execute(project_path=project_path)
        except Exception as e:
            logger.warning(f"host_test auto-run failed: {e}")
            return

        # 结果回灌 — user 角色更稳定（kimi-code 的 system 注入有时会被忽略）
        marker = "T2 host_test (auto):" if ht_result.success else "T2 host_test (auto) FAILED:"
        body = ht_result.output if ht_result.success else (ht_result.error or "(no error message)")
        body = body[-3000:]
        messages.append({
            "role": "user",
            "content": f"[{marker}]\n{body}",
        })
        await self._emit(
            task_id,
            EventType.STEP_COMPLETED if ht_result.success else EventType.STEP_FAILED,
            step="host_test_auto",
            output=(body[:300] if ht_result.success else None),
            error=(body[:300] if not ht_result.success else None),
        )

    def _build_working_tree_summary(self, project_path: str) -> str:
        """枚举项目根的 build/profile 文件，给 Reviewer 一份 working tree 简报。"""
        from pathlib import Path
        try:
            root = Path(project_path)
            if not root.is_dir():
                return f"(project_path {project_path} not a directory)"
        except Exception as e:
            return f"(cannot inspect project_path: {e})"

        watched = [
            "CMakeLists.txt", "Makefile", "GNUmakefile", "makefile",
            "package.json", "Cargo.toml", "build.gradle", "pyproject.toml",
            "setup.py", "configure", "configure.ac",
            "npdm.json",
        ]
        present: list[str] = []
        for n in watched:
            try:
                if (root / n).exists():
                    present.append(n)
            except Exception:
                continue
        # build 输出目录
        for d in ("build", "out", "dist"):
            try:
                if (root / d).is_dir():
                    present.append(f"{d}/ (dir)")
            except Exception:
                continue

        # profile.build / deploy.command（直接读 .kedo/project_profile.json，避免依赖 mgr）
        profile_block = ""
        try:
            import json as _json
            pf_path = root / ".kedo" / "project_profile.json"
            if pf_path.exists():
                pf = _json.loads(pf_path.read_text(encoding="utf-8", errors="replace"))
                build_cmd = (pf.get("build") or {}).get("command", "") or "(empty)"
                deploy_cmd = (pf.get("deploy") or {}).get("command", "") or "(none)"
                hv = pf.get("human_verified", False)
                profile_block = (
                    f"\nprofile.build.command: {build_cmd!r}\n"
                    f"profile.deploy.command: {deploy_cmd!r}\n"
                    f"profile.human_verified: {hv}"
                )
        except Exception as e:
            profile_block = f"\n(profile read failed: {e})"

        files_block = ", ".join(present) if present else "(no recognized build files at root)"
        return f"Project root: {project_path}\nBuild-related files at root: {files_block}{profile_block}"

    @staticmethod
    def _truncate(text: str, max_chars: int) -> str:
        if len(text) <= max_chars:
            return text
        half = max_chars // 2
        return text[:half] + f"\n... ({len(text) - max_chars} chars truncated) ...\n" + text[-half:]

    def _get_model_name(self) -> str:
        return getattr(self.llm, "model", "unknown")
