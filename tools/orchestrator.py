"""
Tool Orchestrator — 工具编排器，读写分离并发优化

借鉴 Claude Code 的 toolOrchestration 设计:
- 只读工具可并发执行 (file_read, file_search, git status/log/diff)
- 写工具必须串行执行 (code_generate, file_write, shell_execute, test_run)
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any

from tools.base import BaseTool, ToolRegistry, ToolResult

logger = logging.getLogger(__name__)

# Git 的只读 action 集合（用于细粒度判断）
GIT_READ_ONLY_ACTIONS = {"status", "diff", "diff_staged", "log"}


class ToolOrchestrator:
    """
    工具编排器 — 读写分离，并发优化

    用法:
        orchestrator = ToolOrchestrator(registry)

        # 批量执行：只读工具并发，写工具串行
        results = await orchestrator.execute_batch([
            ("file_read", {"file_path": "a.py"}),
            ("file_read", {"file_path": "b.py"}),
            ("file_write", {"file_path": "c.py", "content": "..."}),
        ])

        # 单工具执行（带权限检查预留接口）
        result = await orchestrator.execute("file_read", file_path="a.py")
    """

    def __init__(self, registry: ToolRegistry, permission_manager=None):
        self.registry = registry
        self.permission_manager = permission_manager

    def _is_read_only(self, tool_name: str, kwargs: dict) -> bool:
        """判断工具调用是否为只读操作"""
        # Git 工具需要根据 action 细粒度判断（不依赖注册状态）
        if tool_name == "git":
            action = kwargs.get("action", "")
            return action in GIT_READ_ONLY_ACTIONS

        tool = self.registry.get(tool_name)
        if not tool:
            return False

        return tool.is_read_only

    async def execute(self, tool_name: str, **kwargs) -> ToolResult:
        """执行单个工具（带权限检查）"""
        # 权限检查
        if self.permission_manager:
            try:
                allowed = await self.permission_manager.check(tool_name, **kwargs)
                if not allowed:
                    return ToolResult(
                        success=False,
                        error=f"Permission denied for tool '{tool_name}'",
                    )
            except PermissionError as e:
                return ToolResult(success=False, error=str(e))

        return await self.registry.execute(tool_name, **kwargs)

    async def execute_batch(
        self, tasks: list[tuple[str, dict]]
    ) -> list[ToolResult]:
        """
        批量执行工具 — 只读并发，写串行

        Args:
            tasks: [(tool_name, kwargs), ...] 列表

        Returns:
            与 tasks 对应顺序的 ToolResult 列表
        """
        if not tasks:
            return []

        # 分类：只读 vs 写
        read_only_indices: list[int] = []
        write_indices: list[int] = []

        for i, (tool_name, kwargs) in enumerate(tasks):
            if self._is_read_only(tool_name, kwargs):
                read_only_indices.append(i)
            else:
                write_indices.append(i)

        # 结果容器
        results: list[ToolResult | None] = [None] * len(tasks)

        # 1. 只读工具并发执行
        if read_only_indices:
            read_tasks = [
                self.execute(tasks[i][0], **tasks[i][1])
                for i in read_only_indices
            ]
            read_results = await asyncio.gather(*read_tasks, return_exceptions=True)

            for idx, result in zip(read_only_indices, read_results):
                if isinstance(result, Exception):
                    results[idx] = ToolResult(
                        success=False, error=f"Concurrent execution error: {result}"
                    )
                else:
                    results[idx] = result

            logger.info(
                f"Batch: {len(read_only_indices)} read-only tools executed concurrently"
            )

        # 2. 写工具串行执行
        for idx in write_indices:
            tool_name, kwargs = tasks[idx]
            results[idx] = await self.execute(tool_name, **kwargs)

        if write_indices:
            logger.info(
                f"Batch: {len(write_indices)} write tools executed sequentially"
            )

        return results  # type: ignore[return-value]
