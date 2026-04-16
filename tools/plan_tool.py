"""
规划工具 — 封装 Planner 的任务拆解能力，供 ReactAgent 调用
"""
from __future__ import annotations

import json
import logging

from tools.base import BaseTool, ToolParameter, ToolResult

logger = logging.getLogger(__name__)


class PlanTool(BaseTool):
    """
    调用 Planner 将需求拆解为子任务列表。

    ReactAgent 面对复杂开发需求时可调用此工具生成结构化计划，
    然后按计划逐步执行 code_generate / build / test 等工具。
    """

    def __init__(self, planner=None, memory=None):
        self._planner = planner
        self._memory = memory

    @property
    def name(self) -> str:
        return "plan_development"

    @property
    def description(self) -> str:
        return (
            "Break down a complex development requirement into an ordered list of subtasks. "
            "Use this for large tasks that need multiple files, build steps, or testing. "
            "Returns a JSON plan with subtasks (title, description, step_type). "
            "After getting the plan, execute each subtask using other tools."
        )

    @property
    def parameters(self) -> list[ToolParameter]:
        return [
            ToolParameter("requirement", "string", "The development requirement to plan"),
            ToolParameter("project_path", "string", "Project root directory"),
        ]

    @property
    def is_read_only(self) -> bool:
        return True

    async def execute(self, requirement: str, project_path: str = ".") -> ToolResult:
        if not self._planner:
            return ToolResult(success=False, error="Planner not initialized")

        try:
            # 收集项目上下文
            import os
            from pathlib import Path

            project_context = {}
            p = Path(project_path)
            if p.exists():
                files = []
                for root, dirs, filenames in os.walk(project_path):
                    dirs[:] = [d for d in dirs if not d.startswith(".") and d not in (
                        "node_modules", "__pycache__", "build", "dist", ".git", ".kedo"
                    )]
                    for f in filenames:
                        if not f.startswith("."):
                            rel = os.path.relpath(os.path.join(root, f), project_path)
                            files.append(rel)
                project_context["files"] = files[:50]
                project_context["file_count"] = len(files)

                # 项目状态
                project_state = {
                    "has_source_code": any(f.endswith((".cpp", ".c", ".py", ".js", ".go", ".rs")) for f in files),
                    "has_docs": any("docs/" in f for f in files),
                    "has_makefile": (p / "Makefile").exists(),
                    "has_cmake": (p / "CMakeLists.txt").exists(),
                }
                project_context["project_state"] = project_state

            # 调用 Planner
            task_id = "plan_tool"
            plan = await self._planner.create_plan(task_id, requirement, project_context)

            # 格式化输出
            subtasks = []
            for s in plan.subtasks:
                subtasks.append({
                    "id": s.id,
                    "title": s.title,
                    "description": s.description[:200],
                    "step_type": s.step_type.value if hasattr(s.step_type, 'value') else str(s.step_type),
                })

            output = json.dumps(subtasks, ensure_ascii=False, indent=2)
            return ToolResult(
                success=True,
                output=f"Plan generated: {len(subtasks)} subtasks\n{output}",
                data={"subtask_count": len(subtasks), "subtasks": subtasks},
            )
        except Exception as e:
            logger.error(f"PlanTool failed: {e}", exc_info=True)
            return ToolResult(success=False, error=f"Planning failed: {e}")
