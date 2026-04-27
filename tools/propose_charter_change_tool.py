"""
ProposeCharterChangeTool — frozen charter 的合法变更通道

charter `mutable: false` 时 ProfileGuard 拒所有违约 file_write，Producer 想真正
改 charter 必须走这个工具：
  1. LLM 调本工具描述要改的字段 + 理由
  2. 工具发 dashboard 事件（复用 DISCUSSION_STARTED / DISCUSSION_PROPOSALS）
  3. 阻塞等用户在 dashboard 点 approve / reject（routes.py POST
     /api/charter/propose-change/{task_id}/decide 推到 _CHARTER_QUEUES）
  4. approve → 把新字段值写回 charter 文件 + bump last_changed
  5. reject → 返回 ToolResult(success=False, error=...) 让 LLM 换思路

跟 propose_alternatives 的关系：UX 一致（dashboard 弹个待审 panel），但用独立
queue 避免跟 alternatives 串。
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any, Optional

from tools.base import BaseTool, ToolParameter, ToolResult
from core.project_charter import Charter

logger = logging.getLogger(__name__)


# 模块级 queue 注册表：每个 task 一个 Queue（routes 端点写入）
_CHARTER_QUEUES: dict[str, asyncio.Queue] = {}


def get_charter_queue(task_id: str) -> asyncio.Queue:
    """routes.py 调用，拿（或创建）对应 task 的 charter-change queue"""
    if task_id not in _CHARTER_QUEUES:
        _CHARTER_QUEUES[task_id] = asyncio.Queue()
    return _CHARTER_QUEUES[task_id]


def _set_nested(d: dict, dotted_path: str, value: Any) -> None:
    """按 'build.system' / 'artifact.target_name' 这种点分路径写入嵌套 dict。"""
    keys = dotted_path.split(".")
    cur = d
    for k in keys[:-1]:
        nxt = cur.get(k)
        if not isinstance(nxt, dict):
            nxt = {}
            cur[k] = nxt
        cur = nxt
    cur[keys[-1]] = value


def _get_nested(d: dict, dotted_path: str) -> Any:
    cur = d
    for k in dotted_path.split("."):
        if not isinstance(cur, dict):
            return None
        cur = cur.get(k)
    return cur


_ALLOWED_FIELDS = {
    "mutable",
    "project_kind",
    "build.system",
    "build.must_have_files",
    "build.forbidden_files",
    "build.command",
    "artifact.target_name",
    "artifact.output_path",
    "deploy.command",
    "coding_conventions",
    "forbidden_actions",
}


class ProposeCharterChangeTool(BaseTool):
    """LLM 工具入口。"""

    def __init__(
        self,
        state_manager=None,
        event_bus=None,
        timeout_s: int = 1800,
    ):
        # state_manager / event_bus 复用现有 dashboard 通道
        self._state = state_manager
        self._event_bus = event_bus
        # project_path / task_id 由 ReactAgent._execute_tool 强制注入（见 react_agent.py:896-899）
        self._timeout_s = timeout_s

    @property
    def name(self) -> str:
        return "propose_charter_change"

    @property
    def description(self) -> str:
        return (
            "Propose a change to .kedo/project_charter.md (the project's binding contract). "
            "Use this when you believe the charter is wrong and a violation you'd otherwise "
            "commit is actually justified — e.g. switching build_system because the current "
            "one is fundamentally inadequate. BLOCKS until user approves/rejects on the "
            "dashboard. Allowed fields: build.system, build.forbidden_files, "
            "build.must_have_files, build.command, artifact.target_name, "
            "artifact.output_path, deploy.command, coding_conventions, forbidden_actions, "
            "project_kind, mutable. Do NOT use this to bypass charter — only when the charter "
            "itself needs revision and you have a concrete reason."
        )

    @property
    def parameters(self) -> list[ToolParameter]:
        return [
            ToolParameter(
                "field_path", "string",
                "Dotted path of the charter field to change "
                "(e.g. 'build.system', 'artifact.target_name', 'build.forbidden_files').",
            ),
            ToolParameter(
                "new_value", "string",
                "New value as a string. For list fields (forbidden_files, coding_conventions) "
                "pass a JSON-encoded list, e.g. '[\"Makefile\", \"GNUmakefile\"]'.",
            ),
            ToolParameter(
                "reason", "string",
                "Why this charter change is justified. The user reads this on the dashboard "
                "to approve/reject. Be specific: what's blocked currently, what changes after.",
            ),
            ToolParameter(
                "task_id", "string",
                "Active task id (auto-injected by ReactAgent).",
                required=False,
            ),
            ToolParameter(
                "project_path", "string",
                "Project root path (auto-injected by ReactAgent).",
                required=False,
            ),
        ]

    @property
    def is_read_only(self) -> bool:
        return False  # approve 后会写文件

    async def execute(
        self,
        field_path: str,
        new_value: str,
        reason: str,
        task_id: str = "",
        project_path: str = "",
    ) -> ToolResult:
        if not task_id:
            return ToolResult(success=False, error="task_id required (auto-injection failed)")
        if not project_path:
            return ToolResult(
                success=False,
                error="project_path required (auto-injection failed). "
                      "Cannot locate .kedo/project_charter.md.",
            )
        if not field_path or field_path not in _ALLOWED_FIELDS:
            return ToolResult(
                success=False,
                error=(
                    f"Field '{field_path}' is not in the allowed charter-change list. "
                    f"Allowed: {sorted(_ALLOWED_FIELDS)}"
                ),
            )
        if not reason or len(reason.strip()) < 8:
            return ToolResult(
                success=False,
                error="reason is required and must be specific (≥8 chars). "
                      "Tell the user WHY this charter change is justified.",
            )

        charter = Charter.load(project_path)
        if charter is None:
            return ToolResult(
                success=False,
                error=(
                    f"No charter at {project_path}/.kedo/project_charter.md. "
                    f"Charter must exist before proposing changes — ask the user to create one."
                ),
            )

        # 解析 new_value：list/bool 字段先尝试 JSON 解码
        parsed_value: Any = new_value
        s = (new_value or "").strip()
        if field_path in ("build.must_have_files", "build.forbidden_files",
                          "coding_conventions", "forbidden_actions"):
            import json
            try:
                parsed_value = json.loads(s)
                if not isinstance(parsed_value, list):
                    raise ValueError("must be JSON list")
                parsed_value = [str(x) for x in parsed_value]
            except Exception as e:
                return ToolResult(
                    success=False,
                    error=f"Field {field_path} requires JSON list, got: {s!r} ({e})",
                )
        elif field_path == "mutable":
            if s.lower() in ("true", "1", "yes"):
                parsed_value = True
            elif s.lower() in ("false", "0", "no"):
                parsed_value = False
            else:
                return ToolResult(
                    success=False,
                    error=f"Field 'mutable' requires bool-ish value, got: {s!r}",
                )

        old_value = _get_nested({
            "schema_version": charter.schema_version,
            "mutable": charter.mutable,
            "project_kind": charter.project_kind,
            "build": charter.build,
            "artifact": charter.artifact,
            "deploy": charter.deploy,
            "coding_conventions": charter.coding_conventions,
            "forbidden_actions": charter.forbidden_actions,
        }, field_path)

        if old_value == parsed_value:
            return ToolResult(
                success=False,
                error=f"No-op: charter.{field_path} is already {old_value!r}.",
            )

        # 构造 dashboard 显示用的 diff
        proposal = {
            "kind": "charter_change",
            "field_path": field_path,
            "old_value": old_value,
            "new_value": parsed_value,
            "reason": reason.strip(),
            "frozen": charter.frozen,
        }

        situation_summary = (
            f"Producer 想改 charter 字段 `{field_path}`：\n"
            f"  旧值: {old_value!r}\n  新值: {parsed_value!r}\n"
            f"理由: {reason.strip()}"
        )

        # 复用 DISCUSSION_* 事件 → dashboard 现有 discussion 面板能渲染（kind 字段区分）
        if self._event_bus:
            try:
                from api.schemas import AgentEvent, EventType
                from datetime import datetime, timezone
                base_ts = datetime.now(timezone.utc)
                await self._event_bus.publish(AgentEvent(
                    event_type=EventType.DISCUSSION_STARTED,
                    task_id=task_id,
                    timestamp=base_ts,
                    data={"summary": situation_summary, "trigger": "charter_change"},
                ))
                await self._event_bus.publish(AgentEvent(
                    event_type=EventType.DISCUSSION_PROPOSALS,
                    task_id=task_id,
                    timestamp=base_ts,
                    data={
                        "kind": "charter_change",
                        "proposal": proposal,
                        # 兼容 dashboard alternatives UI：把 approve/reject 包装成 2 选项
                        "proposals": [
                            {"id": "approve",
                             "title": f"Approve charter.{field_path} → {parsed_value!r}",
                             "description": reason.strip()},
                            {"id": "reject",
                             "title": "Reject — keep current charter",
                             "description": "Producer must find another path."},
                        ],
                    },
                ))
            except Exception as e:
                logger.warning(f"propose_charter_change: event emit failed: {e}")

        # 也写到 state_manager（让 REST /api/charter 能看到 pending change）
        if self._state is not None:
            try:
                # 简单 key 协议：state.set_discussion 已有，复用之带 kind 区分
                self._state.set_discussion(task_id, situation_summary, [proposal])
            except Exception as e:
                logger.debug(f"propose_charter_change: set_discussion failed: {e}")

        logger.info(
            f"propose_charter_change waiting on user input for task {task_id} "
            f"(field={field_path}, timeout={self._timeout_s}s)"
        )

        queue = get_charter_queue(task_id)
        try:
            payload = await asyncio.wait_for(queue.get(), timeout=self._timeout_s)
        except asyncio.TimeoutError:
            if self._state is not None:
                try:
                    self._state.clear_discussion(task_id)
                except Exception:
                    pass
            return ToolResult(
                success=False,
                error=(
                    f"propose_charter_change timed out after {self._timeout_s}s. "
                    f"User did not approve/reject on dashboard. Treat as REJECT — "
                    f"do not change charter; either pick a different approach or "
                    f"call pause_for_human."
                ),
            )
        finally:
            if self._state is not None:
                try:
                    self._state.clear_discussion(task_id)
                except Exception:
                    pass

        # routes 推 dict {action: 'approve'|'reject', additional_input?: str}
        action = (payload.get("action") or payload.get("proposal_id") or "").lower()
        user_note = payload.get("human_input") or payload.get("note") or ""

        if action != "approve":
            return ToolResult(
                success=False,
                error=(
                    f"User REJECTED charter change (field={field_path}). "
                    f"User note: {user_note or '(none)'}. "
                    f"You MUST find another way to satisfy the requirement that does "
                    f"NOT violate the existing charter."
                ),
            )

        # approve → 写回 charter
        try:
            _set_nested({
                "build": charter.build,
                "artifact": charter.artifact,
                "deploy": charter.deploy,
            }, field_path, parsed_value) if "." in field_path else None
            # top-level fields
            if field_path == "mutable":
                charter.mutable = bool(parsed_value)
            elif field_path == "project_kind":
                charter.project_kind = str(parsed_value)
            elif field_path == "coding_conventions":
                charter.coding_conventions = list(parsed_value)
            elif field_path == "forbidden_actions":
                charter.forbidden_actions = list(parsed_value)
            charter.save(new_reason=reason.strip())
        except Exception as e:
            logger.exception("Charter save failed after approval")
            return ToolResult(
                success=False,
                error=f"Charter approve succeeded on UI but save failed: {e}. "
                      f"Charter file may be in inconsistent state — escalate.",
            )

        logger.info(
            f"Charter changed: {field_path} {old_value!r} → {parsed_value!r} "
            f"(approved by user for task {task_id})"
        )
        return ToolResult(
            success=True,
            output=(
                f"Charter change APPROVED.\n"
                f"  field: {field_path}\n"
                f"  old:   {old_value!r}\n"
                f"  new:   {parsed_value!r}\n"
                f"  user note: {user_note or '(none)'}\n\n"
                f"You may now proceed with edits that align with the new charter."
            ),
            data={
                "field_path": field_path,
                "old_value": old_value,
                "new_value": parsed_value,
                "user_note": user_note,
            },
        )
