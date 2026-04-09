"""
Planner — LLM 驱动的任务拆解与计划生成

将自然语言需求分解为可执行的有序子任务列表。
遵循固化的五步开发流程：

  ① 需求提出 → ② SDD 文档生成 → ③ 代码生成 → ④ 部署 → ⑤ 测试

测试不通过时回退到 ② SDD 文档修改，形成闭环。
文档按 4 个目录生成，模板结构固化在 doc_templates.py 中。
"""
from __future__ import annotations

import json
import logging
from typing import Any, Optional

from api.schemas import StepType, SubTask, TaskPlan
from core.memory import AgentMemory
from core.doc_templates import (
    DOC_CATEGORIES,
    ALL_DOC_TEMPLATES,
    get_doc_generation_prompt,
    get_compact_template_prompt,
    get_full_template_prompt,
)

logger = logging.getLogger(__name__)

# ============================================================
# System Prompt — 五步流程 + 固化文档模板
# ============================================================

PLAN_SYSTEM_PROMPT = """You are a software development planner following kedo's **five-step development pipeline**.

=== KEDO 五步开发流程 ===

  ① 需求提出（Requirement）  — 分析用户提示词，生成结构化需求文档
  ② SDD 文档生成（Design）   — 生成架构、API、数据库、模块设计文档
  ③ 代码生成（Coding）       — 根据设计文档生成代码
  ④ 部署（Deploy）           — 生成部署配置和文档
  ⑤ 测试（Testing）          — 生成测试文档，运行测试

如果测试不通过 → 回退到步骤 ② 修改 SDD 文档，重新进入流程。

=== 任务拆解规则 ===

Each subtask must have:
- title: short descriptive name
- description: detailed what to do (be very specific about file paths, content, and structure)
- step_type: one of [plan, code_generate, build, test, evaluate, deploy]
- dependencies: list of subtask IDs this depends on

step_type guidelines:
- "plan": ONLY for high-level architecture decisions that don't produce files (max 1 per plan)
- "code_generate": for creating or modifying ANY file (code, config, docs, markdown, etc.)
- "build": for compilation, dependency install, or docker build
- "test": for running tests or validation
- "evaluate": for quality assessment
- "deploy": for deployment actions

=== 固化文档结构（必须严格遵循） ===

你在生成计划时，**必须**按照以下 4 个文档目录生成对应文档，每个文档的结构已固化：

{doc_templates}

=== 计划生成步骤 ===

你生成的 subtask 列表**必须**按以下顺序包含：

**步骤 ① 需求文档（docs/requirement/）**
  - subtask_0: 创建 docs/requirement/requirement.md（需求概述）
  - subtask_1: 创建 docs/requirement/user-stories.md（用户故事）

**步骤 ② SDD 设计文档（docs/sdd/）**
  - subtask_2: 创建 docs/sdd/architecture.md（系统架构设计）
  - subtask_3: 创建 docs/sdd/api-design.md（API 设计）
  - subtask_4: 创建 docs/sdd/database-design.md（数据库设计）
  - subtask_5: 创建 docs/sdd/module-design.md（模块设计）

**步骤 ③ 代码生成**
  - subtask_6+: 根据设计文档生成所有代码文件（数量由需求决定）
  - 包含：项目入口、数据模型、路由、业务逻辑、配置文件、Dockerfile、docker-compose.yml、README.md 等

**步骤 ④ 部署文档（docs/deploy/）**
  - subtask_N: 创建 docs/deploy/deployment.md（部署方案）

**步骤 ⑤ 测试文档 + 验证（docs/test/）**
  - subtask_N+1: 创建 docs/test/test-plan.md（测试计划）
  - subtask_N+2: 创建 docs/test/test-cases.md（测试用例）
  - subtask_N+3: 创建 docs/test/automation.md（自动化测试方案）
  - subtask_N+4: build（构建项目）
  - subtask_N+5: test（运行测试）
  - subtask_N+6: evaluate（质量评估）

=== 重要规则 ===

1. **文档先行**：步骤①②的文档必须在步骤③代码生成之前完成
2. **固化结构**：每个文档必须严格按照上面定义的大纲结构生成，不可省略或修改章节
3. **内容详实**：文档内容必须根据用户的提示词需求充分展开，不能只写标题
4. **Mermaid 图表**：架构图、ER 图、时序图、状态图必须使用 mermaid 语法
5. **代码依赖设计**：步骤③的代码生成必须引用步骤②的设计文档内容
6. **部署文档**：必须包含 Docker 配置、环境变量、部署流程
7. **测试完整**：测试文档必须覆盖所有 API 接口的正常和异常用例
8. **文件路径明确**：每个 code_generate subtask 的 description 中必须指定完整文件路径
9. 对于每个文档 subtask，description 中必须包含该文档的完整大纲结构（从上面的固化模板复制）

Output a JSON array of subtasks.

{language_instruction}
"""


# 语言配置映射
DOC_LANGUAGE_INSTRUCTIONS = {
    "zh": "IMPORTANT: All documents (docs/**/*.md) and README.md MUST be written in Chinese (中文). Code comments should also be in Chinese. Subtask titles and descriptions in the JSON can remain in English.",
    "en": "",  # 默认英文，无需额外指令
    "ja": "IMPORTANT: All documents (docs/**/*.md) and README.md MUST be written in Japanese (日本語). Code comments should also be in Japanese.",
    "ko": "IMPORTANT: All documents (docs/**/*.md) and README.md MUST be written in Korean (한국어). Code comments should also be in Korean.",
}


class Planner:
    """
    任务计划生成器

    使用 LLM 将高层需求拆解为可执行的开发步骤。
    遵循 kedo 五步流程，文档模板已固化。
    """

    def __init__(self, llm_client, memory: AgentMemory, config: dict = None):
        self._llm = llm_client
        self._memory = memory
        self._config = config or {}

    def _get_system_prompt(self) -> str:
        """获取带语言配置和文档模板的 system prompt"""
        # 语言指令
        doc_lang = self._config.get("doc_language", "en")
        lang_instruction = DOC_LANGUAGE_INSTRUCTIONS.get(doc_lang, "")
        if doc_lang not in DOC_LANGUAGE_INSTRUCTIONS and doc_lang != "en":
            lang_instruction = (
                f"IMPORTANT: All documents (docs/**/*.md) and README.md MUST be "
                f"written in language code '{doc_lang}'. Code comments should also "
                f"be in this language."
            )

        # 注入精简版文档模板（避免 system prompt 体积过大导致超时）
        doc_templates = get_compact_template_prompt()

        prompt = PLAN_SYSTEM_PROMPT.replace("{language_instruction}", lang_instruction)
        prompt = prompt.replace("{doc_templates}", doc_templates)
        return prompt

    async def create_plan(
        self,
        task_id: str,
        description: str,
        project_context: Optional[dict] = None,
        on_token=None,
    ) -> TaskPlan:
        """
        根据需求描述生成执行计划

        遵循五步流程：需求 → SDD → 代码 → 部署 → 测试

        Args:
            task_id: 任务 ID
            description: 自然语言需求描述
            project_context: 项目上下文 (文件结构、已有代码等)
        """
        messages = [
            {"role": "system", "content": self._get_system_prompt()},
        ]

        # 添加项目上下文
        if project_context:
            context_str = json.dumps(project_context, indent=2, ensure_ascii=False)
            messages.append({
                "role": "user",
                "content": f"Project context:\n{context_str}",
            })

        # 添加相关经验
        experiences = self._memory.get_relevant_experience(description)
        if experiences:
            exp_str = "\n".join(
                f"- {e['summary']}: {', '.join(e['learnings'])}"
                for e in experiences
            )
            messages.append({
                "role": "user",
                "content": f"Relevant past experiences:\n{exp_str}",
            })

        messages.append({
            "role": "user",
            "content": (
                f"Task: {description}\n\n"
                f"请严格按照 kedo 五步流程生成计划，文档结构按照固化模板生成。\n"
                f"Generate the execution plan as JSON."
            ),
        })

        # 调用 LLM（优先流式，支持 token 回调；失败时回退到非流式）
        if on_token and hasattr(self._llm, 'stream_chat'):
            try:
                chunks = []
                async for token in self._llm.stream_chat(messages):
                    chunks.append(token)
                    await on_token(token)
                response = "".join(chunks)
            except Exception as e:
                logger.warning(f"Stream call failed ({e}), falling back to non-stream")
                response = await self._llm.chat(messages)
        else:
            response = await self._llm.chat(messages)
        subtasks = self._parse_plan(response)

        # 验证计划是否包含必要的文档步骤
        subtasks = self._ensure_doc_steps(subtasks, description)

        plan = TaskPlan(
            task_id=task_id,
            subtasks=subtasks,
        )

        # 记录到记忆
        self._memory.add_message("assistant", f"Plan created with {len(subtasks)} steps")

        logger.info(f"Plan created for task {task_id}: {len(subtasks)} subtasks")
        return plan

    def _ensure_doc_steps(
        self, subtasks: list[SubTask], description: str
    ) -> list[SubTask]:
        """
        确保计划中包含所有必要的文档生成步骤

        如果 LLM 遗漏了某些文档，这里会补充上去。
        这是"固化"逻辑的核心保障——即使 LLM 不听话，也能兜底。
        """
        # 收集已有文档步骤的文件路径
        existing_paths = set()
        for st in subtasks:
            desc_lower = st.description.lower()
            for doc_path in ALL_DOC_TEMPLATES:
                if doc_path in desc_lower or f"docs/{doc_path}" in desc_lower:
                    existing_paths.add(doc_path)

        # 检查缺失的必要文档
        missing_docs = []
        for doc_path, tmpl in ALL_DOC_TEMPLATES.items():
            if doc_path not in existing_paths:
                missing_docs.append((doc_path, tmpl))

        if not missing_docs:
            return subtasks

        logger.warning(
            f"LLM plan missing {len(missing_docs)} doc(s), injecting: "
            f"{[d[0] for d in missing_docs]}"
        )

        # 按类别确定插入位置
        # requirement → 插入到最前面
        # sdd → 插入到 requirement 之后
        # deploy → 插入到代码生成之后
        # test → 插入到 deploy 之后

        category_order = {"requirement": 0, "sdd": 1, "deploy": 2, "test": 3}
        missing_docs.sort(
            key=lambda x: category_order.get(x[0].split("/")[0], 99)
        )

        # 找到插入位置：在第一个非 code_generate 或 build 步骤之前
        insert_indices = {
            "requirement": 0,
            "sdd": 0,
            "deploy": 0,
            "test": 0,
        }

        # 扫描现有步骤确定各类别的合理插入位置
        for i, st in enumerate(subtasks):
            desc_lower = st.description.lower()
            if "requirement/" in desc_lower:
                insert_indices["sdd"] = max(insert_indices["sdd"], i + 1)
                insert_indices["deploy"] = max(insert_indices["deploy"], i + 1)
                insert_indices["test"] = max(insert_indices["test"], i + 1)
            elif "sdd/" in desc_lower:
                insert_indices["deploy"] = max(insert_indices["deploy"], i + 1)
                insert_indices["test"] = max(insert_indices["test"], i + 1)
            elif st.step_type == StepType.CODE_GENERATE and "docs/" not in desc_lower:
                insert_indices["deploy"] = max(insert_indices["deploy"], i + 1)
                insert_indices["test"] = max(insert_indices["test"], i + 1)

        # 插入缺失的文档步骤
        injected = []
        for doc_path, tmpl in missing_docs:
            category = doc_path.split("/")[0]
            new_subtask = SubTask(
                title=f"生成{tmpl['title']}",
                description=(
                    f"Create file docs/{doc_path}\n\n"
                    f"{tmpl['description']}\n\n"
                    f"文档必须按照以下大纲结构生成：\n"
                    f"{tmpl['outline']}"
                ),
                step_type=StepType.CODE_GENERATE,
                dependencies=[],
            )
            injected.append((category, new_subtask))

        # 按类别分组插入
        result = list(subtasks)
        offset = 0
        for category, new_st in injected:
            idx = insert_indices.get(category, 0) + offset
            result.insert(idx, new_st)
            offset += 1

        # 重新分配 ID
        for i, st in enumerate(result):
            st.id = f"subtask_{i}"

        return result

    async def replan(
        self,
        task_id: str,
        current_plan: TaskPlan,
        feedback: str,
        failed_step: Optional[SubTask] = None,
    ) -> TaskPlan:
        """
        根据反馈重新生成计划（测试失败时回退到 SDD 文档修改）

        五步流程闭环：测试不通过 → 回到步骤② → 修改 SDD → 重新代码生成 → 重新测试

        Args:
            task_id: 任务 ID
            current_plan: 当前计划
            feedback: 人工反馈或失败原因
            failed_step: 失败的步骤
        """
        messages = [
            {"role": "system", "content": self._get_system_prompt()},
            {
                "role": "user",
                "content": (
                    f"The current plan needs revision.\n\n"
                    f"Current plan: {json.dumps([s.model_dump() for s in current_plan.subtasks], indent=2)}\n\n"
                    f"Feedback: {feedback}\n"
                    + (f"Failed step: {failed_step.title} - {failed_step.result}\n" if failed_step else "")
                    + "\n按照 kedo 五步流程，测试失败应回退到步骤②（SDD 文档修改），"
                    + "然后重新进入代码生成 → 部署 → 测试流程。\n"
                    + "只需要重新生成从步骤②开始需要修改的部分，不需要重新生成全部文档。\n"
                    + "\nGenerate a revised plan as JSON."
                ),
            },
        ]

        response = await self._llm.chat(messages)
        subtasks = self._parse_plan(response)

        return TaskPlan(task_id=task_id, subtasks=subtasks)

    def _parse_plan(self, response: str) -> list[SubTask]:
        """解析 LLM 返回的计划 JSON"""
        # 提取 JSON
        try:
            # 尝试从 markdown 代码块中提取
            if "```" in response:
                json_str = response.split("```")[1]
                if json_str.startswith("json"):
                    json_str = json_str[4:]
                json_str = json_str.strip()
            else:
                # 尝试直接解析
                start = response.find("[")
                end = response.rfind("]") + 1
                json_str = response[start:end]

            raw_tasks = json.loads(json_str)
        except (json.JSONDecodeError, IndexError) as e:
            logger.error(f"Failed to parse plan JSON: {e}")
            # Fallback: 使用固化的基本文档计划
            return self._create_fallback_plan()

        # 转换为 SubTask 对象
        subtasks = []
        for i, raw in enumerate(raw_tasks):
            subtask = SubTask(
                id=f"subtask_{i}",
                title=raw.get("title", f"Step {i+1}"),
                description=raw.get("description", ""),
                step_type=StepType(raw.get("step_type", "code_generate")),
                dependencies=raw.get("dependencies", []),
            )
            subtasks.append(subtask)

        return subtasks

    def _create_fallback_plan(self) -> list[SubTask]:
        """
        兜底计划 — 当 LLM 返回解析失败时，
        直接使用固化模板创建包含全部文档步骤的基本计划
        """
        subtasks = []
        idx = 0

        # 步骤① 需求文档
        for doc_path, tmpl in DOC_CATEGORIES["requirement"]["templates"].items():
            subtasks.append(SubTask(
                id=f"subtask_{idx}",
                title=f"生成{tmpl['title']}",
                description=(
                    f"Create file docs/{doc_path}\n\n"
                    f"{tmpl['description']}\n\n"
                    f"文档大纲：\n{tmpl['outline']}"
                ),
                step_type=StepType.CODE_GENERATE,
                dependencies=[f"subtask_{i}" for i in range(idx)] if idx > 0 else [],
            ))
            idx += 1

        # 步骤② SDD 设计文档
        sdd_start = idx
        for doc_path, tmpl in DOC_CATEGORIES["sdd"]["templates"].items():
            subtasks.append(SubTask(
                id=f"subtask_{idx}",
                title=f"生成{tmpl['title']}",
                description=(
                    f"Create file docs/{doc_path}\n\n"
                    f"{tmpl['description']}\n\n"
                    f"文档大纲：\n{tmpl['outline']}"
                ),
                step_type=StepType.CODE_GENERATE,
                dependencies=[f"subtask_{sdd_start - 1}"],  # 依赖需求文档
            ))
            idx += 1

        # 步骤③ 代码生成（占位，由 LLM 根据需求决定具体内容）
        subtasks.append(SubTask(
            id=f"subtask_{idx}",
            title="Generate project code",
            description="Based on the SDD design documents, generate all project source code files.",
            step_type=StepType.CODE_GENERATE,
            dependencies=[f"subtask_{idx - 1}"],
        ))
        code_idx = idx
        idx += 1

        # 步骤④ 部署文档
        for doc_path, tmpl in DOC_CATEGORIES["deploy"]["templates"].items():
            subtasks.append(SubTask(
                id=f"subtask_{idx}",
                title=f"生成{tmpl['title']}",
                description=(
                    f"Create file docs/{doc_path}\n\n"
                    f"{tmpl['description']}\n\n"
                    f"文档大纲：\n{tmpl['outline']}"
                ),
                step_type=StepType.CODE_GENERATE,
                dependencies=[f"subtask_{code_idx}"],
            ))
            idx += 1

        # 步骤⑤ 测试文档
        for doc_path, tmpl in DOC_CATEGORIES["test"]["templates"].items():
            subtasks.append(SubTask(
                id=f"subtask_{idx}",
                title=f"生成{tmpl['title']}",
                description=(
                    f"Create file docs/{doc_path}\n\n"
                    f"{tmpl['description']}\n\n"
                    f"文档大纲：\n{tmpl['outline']}"
                ),
                step_type=StepType.CODE_GENERATE,
                dependencies=[f"subtask_{idx - 1}"] if idx > code_idx + 1 else [f"subtask_{code_idx}"],
            ))
            idx += 1

        # 验证步骤
        subtasks.append(SubTask(
            id=f"subtask_{idx}",
            title="Build project",
            description="Build the project",
            step_type=StepType.BUILD,
            dependencies=[f"subtask_{idx - 1}"],
        ))
        idx += 1

        subtasks.append(SubTask(
            id=f"subtask_{idx}",
            title="Run tests",
            description="Run tests",
            step_type=StepType.TEST,
            dependencies=[f"subtask_{idx - 1}"],
        ))
        idx += 1

        subtasks.append(SubTask(
            id=f"subtask_{idx}",
            title="Evaluate quality",
            description="Evaluate code quality",
            step_type=StepType.EVALUATE,
            dependencies=[f"subtask_{idx - 1}"],
        ))
        idx += 1

        return subtasks
