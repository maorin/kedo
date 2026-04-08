"""
Memory & Context — Agent 的上下文记忆管理

维护对话历史、代码上下文、执行结果等信息
支持跨任务共享上下文和上下文窗口管理

P1 增强:
- 分层压缩: system prompt → 工作记忆 → 最近 N 条消息 → 历史摘要
- LLM 摘要: 超过阈值时用 LLM 将老消息压缩为摘要
- 工具输出截断: 工具返回的大段输出在进入记忆前预截断
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Optional

logger = logging.getLogger(__name__)


@dataclass
class MemoryEntry:
    role: str  # "system", "user", "assistant", "tool"
    content: str
    metadata: dict[str, Any] = field(default_factory=dict)
    timestamp: datetime = field(default_factory=datetime.utcnow)


class AgentMemory:
    """
    Agent 记忆管理器

    - 短期记忆: 当前任务的对话历史和执行结果
    - 长期记忆: 项目代码索引、之前任务的经验
    - 工作记忆: 当前步骤的上下文 (相关文件、diff 等)

    P1 增强: 分层压缩策略
    ┌─────────────────────────┐
    │ System Prompt (不压缩)    │
    ├─────────────────────────┤
    │ 工作记忆 (不压缩)         │
    ├─────────────────────────┤
    │ 最近 N 条消息 (不压缩)    │  ← RECENT_KEEP
    ├─────────────────────────┤
    │ 历史摘要 (LLM 压缩)       │  ← 早期消息压缩为摘要
    └─────────────────────────┘
    """

    RECENT_KEEP = 10            # 最近 N 条非 system 消息保持原样
    COMPRESS_THRESHOLD = 0.7    # 超过 context 容量的 70% 时触发压缩
    MAX_TOOL_OUTPUT = 2000      # 工具输出最大字符数

    def __init__(self, max_short_term: int = 100, max_context_chars: int = 120_000):
        self.max_short_term = max_short_term
        self.max_context_chars = max_context_chars

        # 短期记忆 — 当前任务
        self._short_term: list[MemoryEntry] = []

        # 工作记忆 — 当前步骤的 context
        self._working: dict[str, str] = {}

        # 长期记忆 — 项目知识 (文件索引、经验等)
        self._long_term: dict[str, Any] = {}

        # 代码上下文缓存
        self._file_cache: dict[str, str] = {}

        # 历史摘要 (压缩后的早期消息)
        self._history_summary: str = ""

    # ----------------------------------------------------------
    # 短期记忆 (对话 & 执行历史)
    # ----------------------------------------------------------

    def add_message(self, role: str, content: str, **metadata):
        entry = MemoryEntry(role=role, content=content, metadata=metadata)
        self._short_term.append(entry)
        # 超出限制时移除最早的记录 (保留 system 消息)
        while len(self._short_term) > self.max_short_term:
            for i, e in enumerate(self._short_term):
                if e.role != "system":
                    self._short_term.pop(i)
                    break
            else:
                break

    def add_tool_result(self, tool_name: str, output: str):
        """记录工具结果，超长自动截断"""
        if len(output) > self.MAX_TOOL_OUTPUT:
            output = (
                output[:self.MAX_TOOL_OUTPUT]
                + f"\n... (truncated, total {len(output)} chars)"
            )
        self.add_message("tool", f"[{tool_name}] {output}")

    def get_messages(self, limit: Optional[int] = None) -> list[dict]:
        """获取消息历史 (用于 LLM 调用)"""
        entries = self._short_term[-limit:] if limit else self._short_term
        return [{"role": e.role, "content": e.content} for e in entries]

    def _calculate_total_chars(self) -> int:
        """计算当前所有记忆的总字符数"""
        total = sum(len(e.content) for e in self._short_term)
        total += sum(len(v) for v in self._working.values())
        total += len(self._history_summary)
        return total

    async def get_context_window(self, llm_client=None) -> list[dict]:
        """
        构建发送给 LLM 的 context window
        包含: system prompt + 历史摘要 + 工作记忆 + 短期记忆 (裁剪到 token 限制内)

        Args:
            llm_client: 可选的 LLM 客户端，用于在压缩时生成摘要。
                        如果不传，压缩时回退到简单截断。
        """
        total_chars = self._calculate_total_chars()

        if total_chars > self.max_context_chars * self.COMPRESS_THRESHOLD:
            if llm_client:
                await self._compress_history(llm_client)
            else:
                self._truncate_old_messages()

        return self._build_context()

    def _build_context(self) -> list[dict]:
        """构建最终的 context window 消息列表"""
        messages = []

        # 1) System prompt
        system_parts = [e for e in self._short_term if e.role == "system"]
        for e in system_parts:
            messages.append({"role": "system", "content": e.content})

        # 2) 历史摘要 (压缩后的早期对话)
        if self._history_summary:
            messages.append({
                "role": "system",
                "content": f"[History Summary]\n{self._history_summary}",
            })

        # 3) 工作记忆 (当前相关文件内容)
        if self._working:
            working_content = "\n\n".join(
                f"=== {k} ===\n{v}" for k, v in self._working.items()
            )
            messages.append({
                "role": "system",
                "content": f"[Current Context]\n{working_content}",
            })

        # 4) 最近的短期记忆 (不压缩)
        non_system = [e for e in self._short_term if e.role != "system"]
        total_chars = sum(len(m["content"]) for m in messages)
        for entry in non_system:
            if total_chars + len(entry.content) > self.max_context_chars:
                break
            messages.append({"role": entry.role, "content": entry.content})
            total_chars += len(entry.content)

        return messages

    async def _compress_history(self, llm_client):
        """用 LLM 将老消息压缩为摘要"""
        non_system = [m for m in self._short_term if m.role != "system"]

        if len(non_system) <= self.RECENT_KEEP:
            return  # 没有足够的老消息需要压缩

        # 分出要压缩的消息（排除最近 N 条）
        old_messages = non_system[:-self.RECENT_KEEP]
        recent_messages = non_system[-self.RECENT_KEEP:]

        if not old_messages:
            return

        # 调用 LLM 生成摘要
        conversation_text = "\n".join(
            f"[{m.role}] {m.content[:500]}" for m in old_messages
        )
        summary_prompt = (
            "Summarize the following conversation history concisely. "
            "Keep: key decisions, errors encountered, file changes made, "
            "important context. Be concise but preserve critical details.\n\n"
            + conversation_text
        )

        try:
            summary = await llm_client.chat([
                {"role": "user", "content": summary_prompt}
            ])
            logger.info(
                f"Compressed {len(old_messages)} messages into summary "
                f"({len(conversation_text)} → {len(summary)} chars)"
            )
        except Exception as e:
            logger.warning(f"LLM compression failed: {e}, falling back to truncation")
            self._truncate_old_messages()
            return

        # 更新历史摘要
        if self._history_summary:
            self._history_summary = f"{self._history_summary}\n\n{summary}"
        else:
            self._history_summary = summary

        # 替换短期记忆：保留 system 消息 + 最近 N 条
        system_messages = [m for m in self._short_term if m.role == "system"]
        self._short_term = system_messages + list(recent_messages)

    def _truncate_old_messages(self):
        """回退方案：无 LLM 时简单截断老消息"""
        non_system = [m for m in self._short_term if m.role != "system"]

        if len(non_system) <= self.RECENT_KEEP:
            return

        # 保留最近 N 条 + system 消息
        recent_messages = non_system[-self.RECENT_KEEP:]
        old_messages = non_system[:-self.RECENT_KEEP]

        # 生成简单摘要（非 LLM）
        simple_summary = (
            f"[Auto-truncated {len(old_messages)} older messages. "
            f"Roles: {', '.join(set(m.role for m in old_messages))}]"
        )

        if self._history_summary:
            self._history_summary = f"{self._history_summary}\n{simple_summary}"
        else:
            self._history_summary = simple_summary

        system_messages = [m for m in self._short_term if m.role == "system"]
        self._short_term = system_messages + list(recent_messages)

    # ----------------------------------------------------------
    # 工作记忆 (当前步骤上下文)
    # ----------------------------------------------------------

    def set_working_context(self, key: str, content: str):
        """设置工作记忆 (如当前正在编辑的文件内容)"""
        self._working[key] = content

    def clear_working_context(self):
        self._working.clear()

    def get_working_context(self) -> dict[str, str]:
        return dict(self._working)

    # ----------------------------------------------------------
    # 长期记忆 (项目知识)
    # ----------------------------------------------------------

    def update_project_index(self, file_tree: dict):
        """更新项目文件索引"""
        self._long_term["file_tree"] = file_tree

    def add_experience(self, task_summary: str, learnings: list[str]):
        """记录任务经验"""
        if "experiences" not in self._long_term:
            self._long_term["experiences"] = []
        self._long_term["experiences"].append({
            "summary": task_summary,
            "learnings": learnings,
            "timestamp": datetime.utcnow().isoformat(),
        })

    def get_relevant_experience(self, query: str) -> list[dict]:
        """获取相关经验 (简单关键词匹配, 后续可升级为向量检索)"""
        experiences = self._long_term.get("experiences", [])
        keywords = set(query.lower().split())
        scored = []
        for exp in experiences:
            text = (exp["summary"] + " ".join(exp["learnings"])).lower()
            score = sum(1 for kw in keywords if kw in text)
            if score > 0:
                scored.append((score, exp))
        scored.sort(key=lambda x: x[0], reverse=True)
        return [exp for _, exp in scored[:5]]

    # ----------------------------------------------------------
    # 文件缓存
    # ----------------------------------------------------------

    def cache_file(self, path: str, content: str):
        self._file_cache[path] = content

    def get_cached_file(self, path: str) -> Optional[str]:
        return self._file_cache.get(path)

    def invalidate_file(self, path: str):
        self._file_cache.pop(path, None)

    # ----------------------------------------------------------
    # 序列化 (用于 Checkpoint)
    # ----------------------------------------------------------

    def snapshot(self) -> dict:
        """导出记忆快照"""
        return {
            "short_term": [
                {"role": e.role, "content": e.content, "metadata": e.metadata}
                for e in self._short_term
            ],
            "working": dict(self._working),
            "long_term": self._long_term,
            "history_summary": self._history_summary,
        }

    def restore(self, snapshot: dict):
        """从快照恢复"""
        self._short_term = [
            MemoryEntry(role=e["role"], content=e["content"], metadata=e.get("metadata", {}))
            for e in snapshot.get("short_term", [])
        ]
        self._working = snapshot.get("working", {})
        self._long_term = snapshot.get("long_term", {})
        self._history_summary = snapshot.get("history_summary", "")
