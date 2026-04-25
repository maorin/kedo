"""
文件操作工具 — 读写文件、目录操作、文件搜索
"""
from __future__ import annotations

import fnmatch
import logging
import os
from pathlib import Path
from typing import Optional

from tools.base import BaseTool, ToolParameter, ToolResult

logger = logging.getLogger(__name__)


class FileReadTool(BaseTool):
    @property
    def name(self) -> str:
        return "file_read"

    @property
    def description(self) -> str:
        return (
            "Read file contents from the project. "
            "Optional `limit` (max lines, 0=all) and `offset` (skip first N lines, 0=start). "
            "Useful for large files: read with offset+limit in chunks."
        )

    @property
    def is_read_only(self) -> bool:
        return True

    @property
    def parameters(self) -> list[ToolParameter]:
        return [
            ToolParameter("file_path", "string", "Path to the file to read"),
            ToolParameter("limit", "integer", "Max number of lines to return (0 = all)", required=False),
            ToolParameter("offset", "integer", "Skip this many lines before returning (0 = start of file)", required=False),
        ]

    async def execute(self, file_path: str, limit: int = 0, offset: int = 0, **_extra) -> ToolResult:
        # **_extra 兜底吞掉 LLM 偶发传的未知参数（如某些模型会传 cache_control / encoding 等），
        # 避免 0ms TypeError 失败触发 convergence detection
        try:
            p = Path(file_path)
            if not p.exists():
                return ToolResult(success=False, error=f"File not found: {file_path}")
            content = p.read_text(encoding="utf-8", errors="replace")
            total_lines = content.count("\n") + (1 if content and not content.endswith("\n") else 0)

            # 应用 offset/limit 切片（仅在显式传入时生效，默认全文）
            try:
                limit_n = int(limit) if limit else 0
                offset_n = int(offset) if offset else 0
            except (TypeError, ValueError):
                limit_n = 0
                offset_n = 0

            sliced_note = ""
            if limit_n > 0 or offset_n > 0:
                lines = content.splitlines(keepends=True)
                start = max(0, offset_n)
                end = (start + limit_n) if limit_n > 0 else len(lines)
                lines = lines[start:end]
                content = "".join(lines)
                sliced_note = (
                    f" [sliced: lines {start + 1}..{start + len(lines)} of {total_lines}]"
                )

            return ToolResult(
                success=True,
                output=content + (sliced_note if sliced_note else ""),
                data={
                    "file_path": file_path,
                    "lines_returned": content.count("\n") + (1 if content and not content.endswith("\n") else 0),
                    "total_lines": total_lines,
                    "size": len(content),
                    "offset": offset_n,
                    "limit": limit_n,
                },
            )
        except Exception as e:
            return ToolResult(success=False, error=str(e))


class FileWriteTool(BaseTool):
    def __init__(self, profile_guard=None):
        self._guard = profile_guard

    @property
    def name(self) -> str:
        return "file_write"

    @property
    def description(self) -> str:
        return "Write or create a file with given content."

    @property
    def parameters(self) -> list[ToolParameter]:
        return [
            ToolParameter("file_path", "string", "Path to the file"),
            ToolParameter("content", "string", "Content to write"),
        ]

    async def execute(self, file_path: str, content: str) -> ToolResult:
        try:
            p = Path(file_path)
            # 安全检查：确保解析后的路径不会在当前目录下创建绝对路径结构
            # 例如防止 "/Users/maojj/project/foo" 被当成相对路径写入当前目录
            resolved = p.resolve()
            cwd = Path.cwd().resolve()
            if not p.is_absolute():
                # 相对路径：确保在 cwd 下
                resolved = (cwd / p).resolve()
            p = resolved

            # ProfileGuard：拦 human_verified profile 覆盖 + 拦 Makefile/CMake 关键 target 丢失
            if self._guard:
                violation = self._guard.check(str(p), content)
                if violation:
                    logger.warning(f"FileWriteTool blocked: {violation[:120]}")
                    return ToolResult(success=False, error=violation)

            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(content, encoding="utf-8")
            return ToolResult(
                success=True,
                output=f"Written {len(content)} bytes to {p}",
                data={"file_path": str(p), "size": len(content)},
            )
        except Exception as e:
            return ToolResult(success=False, error=str(e))


class FileSearchTool(BaseTool):
    @property
    def name(self) -> str:
        return "file_search"

    @property
    def description(self) -> str:
        return "Search for files by name pattern or content (grep)."

    @property
    def is_read_only(self) -> bool:
        return True

    @property
    def parameters(self) -> list[ToolParameter]:
        return [
            ToolParameter("directory", "string", "Directory to search in"),
            ToolParameter("pattern", "string", "Filename glob pattern (e.g., '*.py')"),
            ToolParameter("content_pattern", "string", "Regex pattern to search in file contents", required=False),
        ]

    async def execute(
        self,
        directory: str,
        pattern: str = "*",
        content_pattern: Optional[str] = None,
    ) -> ToolResult:
        try:
            matches = []
            root = Path(directory)
            for dirpath, dirnames, filenames in os.walk(root):
                # 跳过隐藏目录和常见忽略目录
                dirnames[:] = [
                    d for d in dirnames
                    if not d.startswith(".") and d not in {"node_modules", "__pycache__", "venv", ".git"}
                ]
                for filename in filenames:
                    if fnmatch.fnmatch(filename, pattern):
                        full_path = os.path.join(dirpath, filename)
                        matches.append(full_path)

            # 如果需要内容搜索
            if content_pattern:
                import re
                regex = re.compile(content_pattern)
                filtered = []
                for fpath in matches:
                    try:
                        content = Path(fpath).read_text(encoding="utf-8", errors="replace")
                        if regex.search(content):
                            filtered.append(fpath)
                    except Exception:
                        pass
                matches = filtered

            return ToolResult(
                success=True,
                output="\n".join(matches[:100]),
                data={"count": len(matches), "files": matches[:100]},
            )
        except Exception as e:
            return ToolResult(success=False, error=str(e))
