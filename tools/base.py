"""
Tool 基类 — 可插拔工具系统的基础接口

所有工具 (代码生成、测试运行、Shell 执行等) 都继承此基类。
Tool Router 通过注册的工具列表动态调用。
"""
from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Optional

logger = logging.getLogger(__name__)


@dataclass
class ToolParameter:
    """工具参数定义"""
    name: str
    type: str  # "string", "integer", "boolean", "object"
    description: str
    required: bool = True
    default: Any = None


@dataclass
class ToolResult:
    """工具执行结果"""
    success: bool
    output: str = ""
    data: dict[str, Any] = field(default_factory=dict)
    error: Optional[str] = None
    duration_ms: float = 0


class BaseTool(ABC):
    """所有工具的抽象基类"""

    @property
    @abstractmethod
    def name(self) -> str:
        """工具名称 (唯一标识)"""
        ...

    @property
    @abstractmethod
    def description(self) -> str:
        """工具描述 (供 LLM 理解何时使用此工具)"""
        ...

    @property
    def parameters(self) -> list[ToolParameter]:
        """工具参数定义"""
        return []

    @property
    def is_read_only(self) -> bool:
        """只读工具可并发执行，写工具必须串行"""
        return False

    @abstractmethod
    async def execute(self, **kwargs) -> ToolResult:
        """执行工具"""
        ...

    def to_schema(self) -> dict:
        """导出为 LLM function calling 格式"""
        properties = {}
        required = []
        for param in self.parameters:
            properties[param.name] = {
                "type": param.type,
                "description": param.description,
            }
            if param.required:
                required.append(param.name)

        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": {
                    "type": "object",
                    "properties": properties,
                    "required": required,
                },
            },
        }


class ToolRegistry:
    """工具注册表 — 管理所有可用工具"""

    def __init__(self, permission_manager=None):
        self._tools: dict[str, BaseTool] = {}
        self.permission_manager = permission_manager

    def register(self, tool: BaseTool):
        """注册工具"""
        self._tools[tool.name] = tool
        logger.info(f"Tool registered: {tool.name}")

    def get(self, name: str) -> Optional[BaseTool]:
        return self._tools.get(name)

    def list_tools(self) -> list[BaseTool]:
        return list(self._tools.values())

    def get_schemas(self) -> list[dict]:
        """获取所有工具的 schema (用于 LLM function calling)"""
        return [tool.to_schema() for tool in self._tools.values()]

    async def execute(self, tool_name: str, **kwargs) -> ToolResult:
        """执行指定工具（带权限检查）"""
        tool = self._tools.get(tool_name)
        if not tool:
            return ToolResult(success=False, error=f"Tool '{tool_name}' not found")

        # 权限检查
        if self.permission_manager:
            try:
                await self.permission_manager.check(tool_name, **kwargs)
            except PermissionError as e:
                logger.warning(f"Permission denied for {tool_name}: {e}")
                return ToolResult(success=False, error=str(e))

        import time
        start = time.monotonic()
        try:
            result = await tool.execute(**kwargs)
            result.duration_ms = (time.monotonic() - start) * 1000
            return result
        except Exception as e:
            logger.error(f"Tool {tool_name} execution failed: {e}")
            return ToolResult(
                success=False,
                error=str(e),
                duration_ms=(time.monotonic() - start) * 1000,
            )
