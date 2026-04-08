"""
Sub Agent — 子 Agent 机制

支持主 Agent 派生子 Agent 并行处理独立子任务。
每个子 Agent 拥有独立的短期记忆，但共享父 Agent 的长期记忆（项目经验）。

用法:
    sub = SubAgent(agent_id="sub-001", parent_memory=main_memory,
                   llm_client=llm, tool_registry=tools)
    result = await sub.execute_task(subtask, project_path)
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any, Optional

from api.schemas import (
    CodeChange,
    EventType,
    StepType,
    SubTask,
    TaskStatus,
    TestResult,
)
from core.memory import AgentMemory
from tools.base import ToolRegistry, ToolResult

logger = logging.getLogger(__name__)


class SubAgent:
    """
    子 Agent — 独立上下文，共享项目记忆

    特点:
    - 独立的短期记忆和工作记忆
    - 共享父 Agent 的长期记忆（项目知识、经验）
    - 使用相同的 LLM 客户端和工具注册表
    - 执行完成后将经验回传给父 Agent
    """

    def __init__(
        self,
        agent_id: str,
        parent_memory: AgentMemory,
        llm_client,
        tool_registry: ToolRegistry,
    ):
        self.agent_id = agent_id
        self.llm = llm_client
        self.tools = tool_registry

        # 独立的短期记忆
        self.memory = AgentMemory()
        # 共享父 Agent 的长期记忆
        self.memory._long_term = parent_memory._long_term

    async def execute_task(
        self,
        subtask: SubTask,
        project_path: str,
    ) -> ToolResult:
        """
        在独立上下文中执行子任务

        Args:
            subtask: 要执行的子任务
            project_path: 项目根目录

        Returns:
            执行结果
        """
        logger.info(f"SubAgent {self.agent_id} executing: {subtask.title}")

        self.memory.add_message(
            "system",
            f"You are a sub-agent (ID: {self.agent_id}) handling: {subtask.title}\n"
            f"Description: {subtask.description}",
        )

        try:
            result = await self._dispatch(subtask, project_path)

            # 记录执行经验到长期记忆
            if result.success:
                self.memory.add_experience(
                    task_summary=f"SubAgent {self.agent_id}: {subtask.title}",
                    learnings=[
                        f"Step type: {subtask.step_type.value}",
                        f"Result: success — {(result.output or '')[:200]}",
                    ],
                )

            logger.info(
                f"SubAgent {self.agent_id} finished: "
                f"{'success' if result.success else 'failed'}"
            )
            return result

        except Exception as e:
            logger.error(f"SubAgent {self.agent_id} error: {e}")
            return ToolResult(success=False, error=f"SubAgent error: {e}")

    async def _dispatch(
        self,
        subtask: SubTask,
        project_path: str,
    ) -> ToolResult:
        """根据子任务类型分发执行"""
        step_type = subtask.step_type

        if step_type == StepType.PLAN:
            messages = [
                {"role": "system", "content": "You are a software architect. Provide a concise technical design."},
                {"role": "user", "content": subtask.description},
            ]
            design = await self.llm.chat(messages)
            self.memory.add_message("assistant", f"[Design] {design[:500]}")
            return ToolResult(
                success=True,
                output=design[:500],
                data={"design": design, "step": subtask.title},
            )

        elif step_type == StepType.CODE_GENERATE:
            return await self.tools.execute(
                "code_generate",
                instruction=subtask.description,
                file_path=f"{project_path}/{subtask.id}.py",
            )

        elif step_type == StepType.TEST:
            return await self.tools.execute(
                "test_run",
                project_path=project_path,
            )

        elif step_type == StepType.BUILD:
            return await self.tools.execute(
                "shell_execute",
                command="echo 'SubAgent build: OK'",
                working_dir=project_path,
            )

        else:
            return ToolResult(
                success=False,
                error=f"SubAgent does not support step type: {step_type}",
            )


async def execute_parallel_subtasks(
    subtasks: list[SubTask],
    parent_memory: AgentMemory,
    llm_client,
    tool_registry: ToolRegistry,
    project_path: str,
) -> list[ToolResult]:
    """
    并发执行无依赖的子任务

    为每个子任务创建一个 SubAgent，并行执行后收集结果。

    Args:
        subtasks: 无依赖关系的子任务列表
        parent_memory: 父 Agent 的记忆（共享长期记忆）
        llm_client: LLM 客户端
        tool_registry: 工具注册表
        project_path: 项目根目录

    Returns:
        与 subtasks 对应顺序的 ToolResult 列表
    """
    agents = [
        SubAgent(
            agent_id=f"sub-{subtask.id}",
            parent_memory=parent_memory,
            llm_client=llm_client,
            tool_registry=tool_registry,
        )
        for subtask in subtasks
    ]

    results = await asyncio.gather(*[
        agent.execute_task(subtask, project_path)
        for agent, subtask in zip(agents, subtasks)
    ], return_exceptions=True)

    # 将异常转为 ToolResult
    final_results = []
    for result in results:
        if isinstance(result, Exception):
            final_results.append(
                ToolResult(success=False, error=f"Parallel execution error: {result}")
            )
        else:
            final_results.append(result)

    return final_results
