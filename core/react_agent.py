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

**For larger development tasks**, follow this recommended workflow:
  1. **Understand** — Read existing code and project structure
  2. **Design** — Think about the approach (for complex tasks, outline a plan)
  3. **Implement** — Write/modify code files
  4. **Build** — Compile and check for errors
  5. **Test** — Run tests if applicable
  6. **Verify** — Review the result quality

You decide which steps to follow based on the task complexity. Skip steps that \
aren't needed.

## Important Rules

1. Always use the `respond` tool to deliver your final answer to the user
2. Read files before modifying them — understand first, then change
3. After writing code, try to build to catch errors early
4. If a build fails, read the error, fix the code, and try again
5. Be concise in tool arguments — don't repeat the entire file when editing
6. Use Chinese (中文) when responding to Chinese input
7. Never include API keys or secrets in code files

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
    ):
        self.llm = llm_client
        self.tools = tool_registry
        self.state = state
        self.memory = memory
        self.config = config or {}

        # 可配置参数
        self.max_turns: int = self.config.get("max_agent_turns", 50)
        self.max_consecutive_errors: int = 3
        self.max_tool_output_chars: int = 4000

        # function calling 是否可用（首次调用时探测）
        self._function_calling_available: Optional[bool] = None

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
        asyncio.create_task(self._run_safe(task_id, description, project_path))

    async def _run_safe(self, task_id: str, description: str, project_path: str):
        """安全包装：捕获所有异常，确保任务状态更新"""
        try:
            result = await self.run(task_id, description, project_path)
            logger.info(f"Task {task_id} completed: {result[:100] if result else '(empty)'}...")
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
        await self.state.update_status(
            task_id, TaskStatus.IN_PROGRESS, current_step="Thinking"
        )
        await self._emit(task_id, EventType.STEP_STARTED, step="agent_loop")

        # 构建 system prompt
        project_context = await self._gather_project_context(project_path)
        system_prompt = self._build_system_prompt(project_context)

        # 初始消息
        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": description},
        ]

        # 运行 ReAct 循环
        result = await self._loop(task_id, messages, project_path)

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
            await self._emit(task_id, EventType.STEP_COMPLETED, step="agent_loop", output=result[:200] if result else "")

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

            # 更新进度
            progress = min(turn / self.max_turns * 100, 95)
            await self.state.update_status(
                task_id, TaskStatus.IN_PROGRESS,
                progress_percent=progress,
                current_step=f"Turn {turn + 1}",
            )

            # 调用 LLM
            response = await self._call_llm(task_id, messages, tool_schemas, turn)

            # 推送 LLM 思考内容（让 REPL/Dashboard 显示）
            if response.content:
                # 逐行推送，模拟流式效果
                for line in response.content.split("\n"):
                    if line.strip():
                        await self._emit(task_id, EventType.LLM_TOKEN, token=line + "\n", phase="agent")

            # Case 1: 无工具调用 → LLM 直接回复 → 完成
            if not response.tool_calls:
                return response.content or "(empty response)"

            # Case 2: 有工具调用 → 执行
            # 先把 assistant 消息追加到历史
            assistant_msg = self._format_assistant_message(response)
            messages.append(assistant_msg)

            for tc in response.tool_calls:
                # respond 工具特殊处理 → 直接结束
                if tc.name == "respond":
                    final_msg = tc.arguments.get("message", "")
                    await self._emit(
                        task_id, EventType.LLM_RESPONSE,
                        phase="respond", summary=final_msg[:200],
                    )
                    # 推送最终回复内容
                    await self._emit(task_id, EventType.LLM_TOKEN, token=final_msg + "\n", phase="respond")
                    return final_msg

                # 执行工具
                result = await self._execute_tool(task_id, tc, project_path)
                tool_output = result.output if result.success else (result.error or "Unknown error")

                # 追加 tool result 到消息历史
                truncated_output = self._truncate(tool_output, self.max_tool_output_chars)
                messages.append({
                    "role": "tool",
                    "tool_call_id": tc.id,
                    "content": truncated_output,
                })

                # 推送工具结果摘要到前端
                output_preview = tool_output[:300]
                await self._emit(
                    task_id, EventType.LLM_TOKEN,
                    token=f"[{tc.name} → {'OK' if result.success else 'FAIL'}] {output_preview}\n",
                    phase="tool_result",
                )

                # 错误计数
                if result.success:
                    consecutive_errors = 0
                else:
                    consecutive_errors += 1

            # 连续错误上限
            if consecutive_errors >= self.max_consecutive_errors:
                error_msg = f"Stopping: {consecutive_errors} consecutive tool failures."
                await self._emit(task_id, EventType.STEP_FAILED, error=error_msg)
                return error_msg

            # 定期 checkpoint
            if turn > 0 and turn % 5 == 0:
                await self._save_checkpoint(task_id, messages)

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

        # 发射响应事件
        summary = ""
        if response.content:
            summary = response.content[:150]
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
## Tool Call Format

Call tools with ```tool_call``` blocks. One tool per block:

```tool_call
{"name": "file_read", "arguments": {"file_path": "src/main.cpp"}}
```

Final answer to user — MUST use respond tool:
```tool_call
{"name": "respond", "arguments": {"message": "回复内容"}}
```

IMPORTANT: You MUST output ```tool_call``` blocks to act. Do NOT just describe what to do.""")
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

        # 带重试的 LLM 调用（网络偶发错误）
        last_error = None
        for attempt in range(3):
            try:
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
        """从 LLM 文本输出中解析 ```tool_call``` 块"""
        import re
        tool_calls = []
        # 匹配 ```tool_call ... ``` 块
        pattern = r'```tool_call\s*\n(.*?)\n```'
        matches = re.findall(pattern, text, re.DOTALL)
        for i, match in enumerate(matches):
            try:
                data = json.loads(match.strip())
                name = data.get("name", "")
                arguments = data.get("arguments", {})
                if isinstance(arguments, str):
                    arguments = json.loads(arguments)
                tool_calls.append(ToolCallData(
                    id=f"text_tc_{i}",
                    name=name,
                    arguments=arguments,
                ))
            except (json.JSONDecodeError, KeyError, TypeError) as e:
                logger.warning(f"Failed to parse tool_call block: {match[:100]}... ({e})")
                continue
        return tool_calls

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

        await self._emit(
            task_id, EventType.TOOL_EXECUTE,
            tool=name, args_summary=str(args)[:200],
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

            # 注入 project_path（如果工具需要但参数中没有）
            if "project_path" in param_names and "project_path" not in args:
                args["project_path"] = project_path

            # 解析 file_path: 如果是相对路径，拼上 project_path
            if "file_path" in args and not os.path.isabs(args["file_path"]):
                args["file_path"] = os.path.join(project_path, args["file_path"])

            logger.info(f"Tool {name} args: {str(args)[:300]}")
            result = await self.tools.execute(name, **args)
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
                output_preview=(result.output or "")[:200],
            )
        else:
            await self._emit(
                task_id, EventType.STEP_FAILED,
                step=f"tool:{name}", error=(result.error or "")[:300],
            )

        return result

    # ==========================================================
    # System Prompt 构建
    # ==========================================================

    def _build_system_prompt(self, project_context: dict) -> str:
        model_name = getattr(self.llm, "model", "unknown")

        # 格式化项目上下文
        ctx_parts = []
        if project_context.get("file_count"):
            ctx_parts.append(f"Project has {project_context['file_count']} files.")
        if project_context.get("file_tree"):
            ctx_parts.append(f"File structure:\n```\n{project_context['file_tree']}\n```")
        if project_context.get("build_system"):
            ctx_parts.append(f"Build system: {project_context['build_system']}")

        context_str = ""
        if ctx_parts:
            context_str = "## Project Context\n\n" + "\n".join(ctx_parts)

        return SYSTEM_PROMPT_TEMPLATE.format(
            model_name=model_name,
            project_context=context_str,
        )

    async def _gather_project_context(self, project_path: str) -> dict:
        """收集项目基本信息"""
        ctx: dict[str, Any] = {}
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

    async def _save_checkpoint(self, task_id: str, messages: list[dict]):
        """保存 checkpoint（消息历史）"""
        try:
            # 简化 checkpoint: 只保存消息历史
            from api.schemas import AgentCheckpoint
            checkpoint = AgentCheckpoint(
                task_id=task_id,
                current_step_index=len(messages),
                plan=None,
                memory_snapshot=self.memory.snapshot() if self.memory else {},
                code_changes=[],
            )
            # 额外保存消息历史
            checkpoint_data = checkpoint.model_dump() if hasattr(checkpoint, "model_dump") else {}
            checkpoint_data["messages"] = messages
            await self.state.save_checkpoint(checkpoint)
        except Exception as e:
            logger.warning(f"Checkpoint save failed: {e}")

    # ==========================================================
    # 辅助方法
    # ==========================================================

    @staticmethod
    def _format_assistant_message(response: LLMResponse) -> dict:
        """将 LLMResponse 转为 OpenAI 格式的 assistant 消息"""
        msg: dict[str, Any] = {"role": "assistant"}
        if response.content:
            msg["content"] = response.content
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
    def _truncate(text: str, max_chars: int) -> str:
        if len(text) <= max_chars:
            return text
        half = max_chars // 2
        return text[:half] + f"\n... ({len(text) - max_chars} chars truncated) ...\n" + text[-half:]

    def _get_model_name(self) -> str:
        return getattr(self.llm, "model", "unknown")
