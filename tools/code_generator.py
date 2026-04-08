"""
代码生成工具 — 调用 LLM 生成/修改代码
"""
from __future__ import annotations

import logging
from pathlib import Path

from tools.base import BaseTool, ToolParameter, ToolResult

logger = logging.getLogger(__name__)


class CodeGeneratorTool(BaseTool):
    """LLM 驱动的代码生成和修改工具"""

    def __init__(self, llm_client, config: dict = None, on_token=None):
        self._llm = llm_client
        self._config = config or {}
        self._on_token = on_token  # async callback(token: str) for streaming progress

    @property
    def name(self) -> str:
        return "code_generate"

    @property
    def description(self) -> str:
        return "Generate or modify code files based on natural language instructions. Can create new files, edit existing ones, or perform batch refactoring."

    @property
    def parameters(self) -> list[ToolParameter]:
        return [
            ToolParameter("instruction", "string", "What code to generate or modify"),
            ToolParameter("file_path", "string", "Target file path"),
            ToolParameter("existing_content", "string", "Current file content (for modifications)", required=False),
            ToolParameter("context_files", "object", "Related file contents for context", required=False),
        ]

    async def execute(
        self,
        instruction: str,
        file_path: str,
        existing_content: str = "",
        context_files: dict[str, str] = None,
    ) -> ToolResult:
        """生成或修改代码"""
        # 构建 prompt
        doc_lang = self._config.get("doc_language", "en")
        lang_hint = ""
        if doc_lang == "zh":
            lang_hint = " For markdown/documentation files (.md), write ALL content in Chinese (中文). For code files, write comments in Chinese."
        elif doc_lang and doc_lang != "en":
            lang_hint = f" For markdown/documentation files (.md), write ALL content in language '{doc_lang}'. For code files, write comments in that language."

        messages = [
            {
                "role": "system",
                "content": (
                    "You are an expert code generator. Generate clean, well-documented code "
                    "following best practices. Output ONLY the code, no explanations."
                    + lang_hint
                ),
            }
        ]

        # 添加上下文文件
        if context_files:
            context_str = "\n\n".join(
                f"=== {path} ===\n{content}" for path, content in context_files.items()
            )
            messages.append({
                "role": "user",
                "content": f"Related files for context:\n{context_str}",
            })

        # 构建主指令
        if existing_content:
            prompt = (
                f"Modify the following file: {file_path}\n\n"
                f"Current content:\n```\n{existing_content}\n```\n\n"
                f"Instruction: {instruction}\n\n"
                f"Output the complete modified file content."
            )
        else:
            prompt = (
                f"Create a new file: {file_path}\n\n"
                f"Instruction: {instruction}\n\n"
                f"Output the complete file content."
            )

        messages.append({"role": "user", "content": prompt})

        try:
            # 使用流式调用（如果可用）
            if self._on_token and hasattr(self._llm, 'stream_chat'):
                chunks = []
                async for token in self._llm.stream_chat(messages):
                    chunks.append(token)
                    await self._on_token(token)
                response = "".join(chunks)
            else:
                response = await self._llm.chat(messages)
            generated_code = self._extract_code(response)

            # 写入文件 — 确保路径正确解析
            target = Path(file_path)
            if not target.is_absolute():
                target = (Path.cwd() / target).resolve()
            else:
                target = target.resolve()
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(generated_code)

            # 生成 diff
            diff = self._generate_diff(existing_content, generated_code, file_path)

            return ToolResult(
                success=True,
                output=f"Code {'modified' if existing_content else 'generated'}: {file_path}",
                data={
                    "file_path": file_path,
                    "action": "modify" if existing_content else "create",
                    "diff": diff,
                    "content": generated_code,
                    "lines": len(generated_code.splitlines()),
                },
            )
        except Exception as e:
            return ToolResult(success=False, error=f"Code generation failed: {e}")

    def _extract_code(self, response: str) -> str:
        """从 LLM 响应中提取代码块"""
        # 尝试提取 markdown 代码块
        if "```" in response:
            blocks = response.split("```")
            if len(blocks) >= 3:
                code = blocks[1]
                # 移除语言标识 (如 ```python)
                first_newline = code.find("\n")
                if first_newline != -1:
                    code = code[first_newline + 1:]
                return code.strip()
        return response.strip()

    def _generate_diff(self, old: str, new: str, path: str) -> str:
        """生成 unified diff"""
        import difflib
        old_lines = old.splitlines(keepends=True) if old else []
        new_lines = new.splitlines(keepends=True)
        diff = difflib.unified_diff(
            old_lines, new_lines,
            fromfile=f"a/{path}", tofile=f"b/{path}",
        )
        return "".join(diff)
