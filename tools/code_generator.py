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

    # 二进制文件后缀 → 用系统工具生成，不走 LLM
    _BINARY_EXTENSIONS = {
        ".jpg", ".jpeg", ".png", ".bmp", ".gif", ".ico", ".webp",
        ".wav", ".mp3", ".ogg", ".flac",
        ".bin", ".dat", ".nro", ".elf", ".o", ".a", ".so", ".dylib",
    }

    async def execute(
        self,
        instruction: str,
        file_path: str,
        existing_content: str = "",
        context_files: dict[str, str] = None,
        platform_constraints: str = "",
    ) -> ToolResult:
        """生成或修改代码。对 .md 文档使用分段生成避免截断。"""
        try:
            target = Path(file_path)

            # ★ G6 修复：二进制文件不走 LLM，用系统工具生成
            if target.suffix.lower() in self._BINARY_EXTENSIONS:
                return await self._generate_binary(instruction, file_path, target)

            is_doc = target.suffix.lower() in (".md", ".txt", ".rst")

            if is_doc and not existing_content:
                # ★ 文档类文件：分段生成（每个章节一次 LLM 调用）
                generated_code = await self._generate_doc_by_sections(
                    instruction, file_path, context_files
                )
            else:
                # 代码文件 / 修改模式：一次性生成
                messages = self._build_messages(
                    instruction, file_path, existing_content, context_files,
                    platform_constraints=platform_constraints,
                )
                response = await self._call_llm(messages)
                generated_code = self._extract_code(response)

            # ★ 验证生成的代码质量（关键文件）
            generated_code = await self._validate_and_retry(
                generated_code, instruction, file_path, existing_content, context_files, target
            )

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

    def _build_messages(self, instruction, file_path, existing_content, context_files,
                        platform_constraints: str = ""):
        """构建 LLM 消息"""
        doc_lang = self._config.get("doc_language", "en")
        lang_hint = ""
        if doc_lang == "zh":
            lang_hint = " For markdown/documentation files (.md), write ALL content in Chinese (中文). For code files, write comments in Chinese."
        elif doc_lang and doc_lang != "en":
            lang_hint = f" For markdown/documentation files (.md), write ALL content in language '{doc_lang}'. For code files, write comments in that language."

        system_content = (
            "You are an expert code generator. Generate clean, well-documented code "
            "following best practices. Output ONLY the code, no explanations."
            + lang_hint
        )

        if platform_constraints:
            system_content += "\n\n" + platform_constraints

        messages = [
            {
                "role": "system",
                "content": system_content,
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

    # 关键文件的验证规则
    _VALIDATION_RULES = {
        "CMakeLists.txt": {
            "required": ["cmake_minimum_required", "project(", "add_executable"],
            # CMake 用 # 做注释，绝对不能把 # / ## 列入 forbidden_prefix。
            # 这里只过滤明确的 markdown/mermaid 残留。
            "forbidden_prefix": ["flowchart", "```"],
            "min_lines": 10,
            "description": "CMake 构建脚本",
        },
        "Makefile": {
            "required": [":", "\t"],  # Makefile 必须有 target: 和 tab 缩进
            # 同理：Makefile 用 # 做注释，不能 forbid。
            "forbidden_prefix": ["flowchart", "```"],
            "min_lines": 5,
            "description": "Make 构建脚本",
        },
        "docker-compose.yml": {
            "required": ["services:", "version:"],
            "forbidden_prefix": ["FROM "],  # Dockerfile 内容不能出现在 docker-compose 里
            "min_lines": 5,
            "description": "Docker Compose 配置",
        },
        "Dockerfile": {
            "required": ["FROM"],
            "forbidden_prefix": ["services:", "version:"],
            "min_lines": 3,
            "description": "Dockerfile",
        },
    }

    async def _validate_and_retry(
        self, content: str, instruction: str, file_path: str,
        existing_content: str, context_files: dict, target: Path,
        max_retries: int = 2,
    ) -> str:
        """
        验证生成的关键文件是否有效。
        如果不合格（如 CMakeLists.txt 是 Markdown 而非 CMake），自动重新生成。
        """
        file_name = Path(file_path).name
        rules = self._VALIDATION_RULES.get(file_name)
        if not rules:
            return content  # 非关键文件不验证

        for attempt in range(max_retries):
            issues = self._check_content(content, rules)
            if not issues:
                return content  # 验证通过

            logger.warning(f"Validation failed for {file_name} (attempt {attempt + 1}): {issues}")
            if self._on_token:
                await self._on_token(f"\n[验证失败] {file_name}: {'; '.join(issues)}，重新生成...\n")

            # 重新生成，在指令中加入验证失败的原因
            fix_instruction = (
                f"{instruction}\n\n"
                f"重要：上次生成的 {file_name} 有以下问题：\n"
                + "\n".join(f"- {i}" for i in issues) + "\n\n"
                f"请生成正确的 {rules['description']}，不要输出 Markdown 文档格式。\n"
                f"只输出纯代码内容，不要任何 markdown 标题或说明文字。"
            )
            messages = self._build_messages(fix_instruction, file_path, existing_content, context_files)
            response = await self._call_llm(messages)
            content = self._extract_code(response)

        # 最后一次验证
        issues = self._check_content(content, rules)
        if issues:
            logger.error(f"Validation still failing for {file_name} after {max_retries} retries: {issues}")
        return content

    def _check_content(self, content: str, rules: dict) -> list[str]:
        """检查内容是否符合规则，返回问题列表"""
        issues = []
        lines = content.strip().splitlines()

        # 检查最少行数
        min_lines = rules.get("min_lines", 0)
        if len(lines) < min_lines:
            issues.append(f"内容过短（{len(lines)}行 < {min_lines}行）")

        # 检查必须包含的关键字
        content_lower = content.lower()
        for kw in rules.get("required", []):
            if kw.lower() not in content_lower:
                issues.append(f"缺少必要内容: '{kw}'")

        # 检查不应出现的前缀（说明是 Markdown 而非代码）
        first_lines = lines[:5] if lines else []
        for prefix in rules.get("forbidden_prefix", []):
            for line in first_lines:
                if line.strip().startswith(prefix):
                    issues.append(f"文件开头包含非法内容: '{line.strip()[:40]}'（这不是有效的代码文件）")
                    break

        return issues

    async def _generate_binary(self, instruction: str, file_path: str, target: Path) -> ToolResult:
        """
        G6 修复：二进制文件（图片、音频等）用系统工具生成，不让 LLM 写文件内容。
        LLM 只负责生成工具命令，由 shell 执行。
        """
        import asyncio

        if not target.is_absolute():
            target = (Path.cwd() / target).resolve()
        else:
            target = target.resolve()
        target.parent.mkdir(parents=True, exist_ok=True)

        ext = target.suffix.lower()

        # 图片类：用 ImageMagick convert 或 ffmpeg 生成
        if ext in (".jpg", ".jpeg", ".png", ".bmp", ".gif", ".ico", ".webp"):
            # 从 instruction 提取尺寸，默认 256x256
            import re
            size_match = re.search(r'(\d{2,4})\s*[xX×]\s*(\d{2,4})', instruction)
            w, h = (size_match.group(1), size_match.group(2)) if size_match else ("256", "256")

            # 从 instruction 提取颜色，默认纯色
            color = "gray"
            for c in ("red", "blue", "green", "black", "white", "gray", "orange", "purple"):
                if c in instruction.lower():
                    color = c
                    break

            # 优先 ImageMagick，退而用 ffmpeg
            cmd_magick = f'convert -size {w}x{h} xc:{color} "{target}"'
            cmd_ffmpeg = f'ffmpeg -y -f lavfi -i color=c={color}:s={w}x{h}:d=1 -frames:v 1 "{target}"'

            for cmd in (cmd_magick, cmd_ffmpeg):
                try:
                    proc = await asyncio.create_subprocess_shell(
                        cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
                    )
                    _, stderr = await asyncio.wait_for(proc.communicate(), timeout=30)
                    if proc.returncode == 0 and target.exists():
                        return ToolResult(
                            success=True,
                            output=f"Binary file generated: {file_path} ({target.stat().st_size} bytes)",
                            data={"file_path": file_path, "action": "create", "binary": True},
                        )
                except Exception:
                    continue

            # 两种工具都失败：创建最小占位文件并警告
            target.write_bytes(b'\x00')
            return ToolResult(
                success=True,
                output=f"Placeholder created for {file_path} (ImageMagick/ffmpeg not available, needs manual replacement)",
                data={"file_path": file_path, "action": "create", "binary": True, "placeholder": True},
            )

        # 其他二进制类型：跳过生成，返回提示
        return ToolResult(
            success=True,
            output=f"Skipped binary file {file_path} (cannot be generated by LLM, needs manual creation or build step)",
            data={"file_path": file_path, "action": "skip", "binary": True},
        )

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
