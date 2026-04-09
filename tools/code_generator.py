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
        """生成或修改代码。对 .md 文档使用分段生成避免截断。"""
        try:
            target = Path(file_path)
            is_doc = target.suffix.lower() in (".md", ".txt", ".rst")

            if is_doc and not existing_content:
                # ★ 文档类文件：分段生成（每个章节一次 LLM 调用）
                generated_code = await self._generate_doc_by_sections(
                    instruction, file_path, context_files
                )
            else:
                # 代码文件 / 修改模式：一次性生成
                messages = self._build_messages(instruction, file_path, existing_content, context_files)
                response = await self._call_llm(messages)
                generated_code = self._extract_code(response)

            # 写入文件
            if not target.is_absolute():
                target = (Path.cwd() / target).resolve()
            else:
                target = target.resolve()
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(generated_code, encoding="utf-8")

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

    def _build_messages(self, instruction, file_path, existing_content, context_files):
        """构建 LLM 消息"""
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

        if context_files:
            context_str = "\n\n".join(
                f"=== {path} ===\n{content}" for path, content in context_files.items()
            )
            messages.append({
                "role": "user",
                "content": f"Related files for context:\n{context_str}",
            })

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
        return messages

    async def _call_llm(self, messages):
        """调用 LLM（优先流式）"""
        if self._on_token and hasattr(self._llm, 'stream_chat'):
            chunks = []
            async for token in self._llm.stream_chat(messages):
                chunks.append(token)
                await self._on_token(token)
            return "".join(chunks)
        else:
            return await self._llm.chat(messages)

    async def _generate_doc_by_sections(
        self, instruction: str, file_path: str, context_files: dict = None
    ) -> str:
        """
        分段生成文档：先生成大纲，再逐章节生成内容，最后拼接。
        每个章节独立调用 LLM，避免单次输出过长被截断。
        """
        doc_lang = self._config.get("doc_language", "en")
        lang_hint = "所有内容使用中文。" if doc_lang == "zh" else ""

        # ---- Step 1: 生成大纲（章节标题列表）----
        outline_messages = [
            {
                "role": "system",
                "content": (
                    "你是一个文档架构师。根据指令生成文档的章节大纲。"
                    "只输出 markdown 标题列表（## 开头），每行一个章节标题，不要写内容。"
                ),
            },
            {
                "role": "user",
                "content": (
                    f"为文件 {file_path} 生成章节大纲。\n\n"
                    f"指令: {instruction}\n\n"
                    f"只输出 ## 标题列表，每行一个，不要写内容。例如:\n"
                    f"## 概述\n## 架构设计\n## 部署步骤\n..."
                ),
            }
        ]

        if self._on_token:
            await self._on_token(f"\n[分段生成] 正在生成大纲...\n")

        outline_response = await self._call_llm(outline_messages)
        outline_text = self._extract_code(outline_response)

        # 解析章节标题
        sections = []
        for line in outline_text.split("\n"):
            stripped = line.strip()
            if stripped.startswith("## "):
                sections.append(stripped[3:].strip())
            elif stripped.startswith("# ") and not sections:
                # 文档标题（一级标题），不算章节
                pass

        if not sections:
            # 大纲生成失败，回退到一次性生成
            logger.warning(f"Failed to generate outline for {file_path}, falling back to single-shot")
            messages = self._build_messages(instruction, file_path, "", context_files)
            response = await self._call_llm(messages)
            return self._extract_code(response)

        logger.info(f"Document outline for {file_path}: {len(sections)} sections: {sections}")
        if self._on_token:
            await self._on_token(f"[分段生成] 大纲 {len(sections)} 章节: {', '.join(sections)}\n")

        # ---- Step 2: 逐章节生成内容 ----
        # 先生成文档标题和开头
        file_name = Path(file_path).stem.replace("-", " ").replace("_", " ").title()
        doc_parts = [f"# {file_name}\n"]

        context_hint = ""
        if context_files:
            context_hint = "参考文件:\n" + "\n".join(f"- {p}" for p in context_files.keys()) + "\n\n"

        for i, section_title in enumerate(sections):
            if self._on_token:
                await self._on_token(f"\n[分段生成] ({i+1}/{len(sections)}) {section_title}...\n")

            section_messages = [
                {
                    "role": "system",
                    "content": (
                        "你是一个技术文档撰写专家。根据指令为文档的某个章节生成详细内容。"
                        "只输出该章节的内容（包括 ## 标题），不要输出其他章节。"
                        "内容必须详实、有实质，不能只写标题或占位符。"
                        f"{lang_hint}"
                    ),
                },
                {
                    "role": "user",
                    "content": (
                        f"文件: {file_path}\n"
                        f"项目指令: {instruction}\n"
                        f"{context_hint}"
                        f"完整大纲: {', '.join(sections)}\n\n"
                        f"请只生成以下章节的完整内容:\n\n"
                        f"## {section_title}\n\n"
                        f"要求：内容详实（至少 200 字），包含具体的技术细节、步骤或说明。"
                        f"如需图表请使用 Mermaid 语法。"
                    ),
                }
            ]

            try:
                section_response = await self._call_llm(section_messages)
                section_content = self._extract_code(section_response).strip()

                # 确保章节以 ## 标题开头
                if not section_content.startswith("##"):
                    section_content = f"## {section_title}\n\n{section_content}"

                doc_parts.append(section_content)
                logger.info(f"Section '{section_title}' generated: {len(section_content)} chars")

            except Exception as e:
                logger.warning(f"Failed to generate section '{section_title}': {e}")
                doc_parts.append(f"## {section_title}\n\n> 此章节生成失败，待补充。\n")

        # ---- Step 3: 拼接 ----
        full_doc = "\n\n".join(doc_parts) + "\n"
        logger.info(f"Document {file_path} complete: {len(sections)} sections, {len(full_doc)} chars")

        if self._on_token:
            await self._on_token(f"\n[分段生成] 完成! {len(sections)} 章节, {len(full_doc)} 字符\n")

        return full_doc

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
