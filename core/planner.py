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

根据项目现状决定生成哪些步骤：

**如果项目为空（无源码、无文档）→ 执行完整五步流程：**
  ① docs/requirement/requirement.md + user-stories.md
  ② docs/sdd/architecture.md + api-design.md + database-design.md + module-design.md
  ③ 所有代码文件
  ④ docs/deploy/deployment.md
  ⑤ docs/test/ 文档 + build + test + evaluate

**如果项目已有代码和文档 → 只做缺失/需要修复的部分：**
  - 已有的文档：跳过，不重新生成
  - 已有的代码文件：跳过，除非需要修改
  - 缺少的构建脚本：生成 Makefile/CMakeLists.txt
  - 缺少的功能：只生成该功能的代码
  - 无论什么情况，末尾都必须有 build + test + evaluate

**绝对禁止在已有项目上重新生成需求文档和设计文档。**

=== 标准项目目录结构（必须遵守） ===

所有代码和构建产物必须使用以下标准目录结构：

```
项目根目录/
├── src/              ← 所有源代码（不要用 source/、lib/、app/ 等）
├── tests/            ← 测试代码
├── build/            ← 构建产物输出目录（.nro、.exe、.bin 等）
├── docs/             ← 文档（requirement/、sdd/、deploy/、test/）
├── config/           ← 配置文件（可选，简单项目可放根目录）
├── Makefile / CMakeLists.txt  ← 构建脚本（输出目录必须指向 build/）
├── Dockerfile / docker-compose.yml  ← 容器化构建（可选）
└── README.md
```

**强制规则：**
- 源代码目录必须是 `src/`（不是 source/、lib/、app/）
- 构建产物必须输出到 `build/` 目录（不要输出到项目根目录）
- 构建脚本（Makefile/CMakeLists.txt）中必须配置 `BUILD_DIR=build` 或等价设置

=== 重要规则 ===

1. **项目现状优先**：如果项目上下文显示已有文件，禁止重新生成这些文件
2. **最小化原则**：只生成用户需求中缺失的部分，不做多余的工作
3. **文档先行**：新项目时，步骤①②必须在步骤③之前；已有项目不需要重新生成文档
4. **固化结构**：新建文档时必须按照上面定义的大纲结构生成
5. **内容详实**：文档内容必须根据用户的提示词需求充分展开，不能只写标题
6. **Mermaid 图表**：架构图、ER 图、时序图、状态图必须使用 mermaid 语法
7. **文件路径明确**：每个 code_generate subtask 的 description 中必须指定完整文件路径
8. **目录规范**：源代码路径必须用 `src/`，构建产物必须输出到 `build/`
9. **末尾必有**：无论什么情况，计划末尾必须包含 build + test + evaluate 三个步骤

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

    def _get_incremental_system_prompt(self, project_state: dict) -> str:
        """已有项目的增量规划 prompt — 不包含固化文档步骤，只聚焦缺失部分"""
        doc_lang = self._config.get("doc_language", "zh")

        # 构建项目现状描述
        state_lines = []
        if project_state.get("has_source_code"):
            state_lines.append(f"已有 {project_state.get('source_count', 0)} 个源码文件: {', '.join(project_state.get('source_files', [])[:10])}")
        if project_state.get("has_docs"):
            state_lines.append(f"已有文档: {', '.join(project_state.get('doc_files', [])[:8])}")
        if project_state.get("has_build_artifacts"):
            state_lines.append(f"已有构建产物: {', '.join(project_state.get('artifacts', []))}")
        else:
            state_lines.append("没有构建产物（可执行文件）")
        if project_state.get("has_makefile") or project_state.get("has_cmake"):
            state_lines.append("有构建脚本")
        else:
            state_lines.append("没有构建脚本（Makefile/CMakeLists.txt）")
        last_eval = project_state.get("last_eval")
        if last_eval:
            state_lines.append(f"上次评估: {last_eval.get('score', 0)}/100")
            missed = last_eval.get("requirements_missed", [])
            if missed:
                state_lines.append(f"缺失需求: {', '.join(missed[:5])}")

        state_text = "\n".join(f"- {s}" for s in state_lines)

        return f"""你是 kedo 的增量规划器。这是一个**已有内容的项目**，不是从零开始。

## 项目现状
{state_text}

## 核心规则（必须严格遵守）

1. **绝对禁止重新生成已有的文档和代码** — docs/ 下的文档已存在，不要再生成
2. **只做用户要求的事** — 分析用户的提示词，只做他要求的
3. **如果用户提到构建/打包问题** — 只生成或修复 Makefile/CMakeLists.txt，不要动其他文件
4. **如果用户提到某个功能缺失** — 只补充该功能的代码
5. **如果用户没有明确说要做什么** — 根据"缺失需求"和"没有构建产物"来判断该做什么
6. **计划精简** — 通常 3-8 个步骤，绝不超过 10 步
7. **末尾必须有** build + test + evaluate

## 输出格式

JSON 数组，每个元素:
- title: 简短描述
- description: 详细说明（包含文件路径）
- step_type: "code_generate" | "build" | "test" | "evaluate"
- dependencies: 依赖的 subtask id 列表（字符串数组如 ["subtask_0"]）

## 标准目录结构

源代码: src/，构建产物: build/，文档: docs/

## 禁止事项

- ❌ 不要生成 docs/requirement/ 下的任何文件
- ❌ 不要生成 docs/sdd/ 下的任何文件
- ❌ 不要生成 docs/deploy/ 下的任何文件
- ❌ 不要生成 docs/test/ 下的任何文件
- ❌ 不要重写已有的源码文件（除非用户明确要求修改）
- ❌ 不要生成超过 10 个步骤的计划

文档语言: {doc_lang}
"""

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
        # ★ 根据项目状态选择系统 prompt
        project_state = project_context.get("project_state", {}) if project_context else {}
        is_existing_project = project_state.get("has_source_code") or project_state.get("has_docs")

        if is_existing_project:
            # 已有项目 → 使用续接模式的 prompt（不包含固化文档步骤）
            system_prompt = self._get_incremental_system_prompt(project_state)
        else:
            # 空项目 → 使用完整五步流程 prompt
            system_prompt = self._get_system_prompt()

        messages = [
            {"role": "system", "content": system_prompt},
        ]

        # 添加项目上下文
        if project_context:
            # 去掉 project_state 避免重复（已注入到系统 prompt）
            ctx = {k: v for k, v in project_context.items() if k != "project_state"}
            context_str = json.dumps(ctx, indent=2, ensure_ascii=False)
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

    async def create_continuation_plan(
        self,
        task_id: str,
        continuation_context: str,
        project_context: Optional[dict] = None,
        on_token=None,
    ) -> TaskPlan:
        """
        智能续接计划：基于项目现状和历史评估生成增量计划

        使用独立的系统 prompt（不继承固化文档模板），只聚焦缺失功能。
        """
        system_prompt = f"""你是 kedo 的续接规划器。用户已经有一个半完成的项目，你的任务是生成**增量计划**。

## 核心原则（必须严格遵守）

1. **不要重新生成完整的已有文件** — 除非续接上下文明确标记为"不完整的文档"
2. **重新生成不完整的文档** — 如果续接上下文中列出了"不完整的文档"，必须重新生成这些文档的完整内容
3. **只做缺失的工作** — 根据续接上下文中的"缺失的需求"列表，只生成实现这些功能的步骤
4. **在现有代码基础上修改/扩展** — 新代码应该 import/引用已有的模块，不要重写
5. **计划要精简** — 通常 5-15 个步骤，不应超过 20 步

## 输出格式

返回 JSON 数组，每个元素：
- title: 简短描述
- description: 详细说明（包含完整文件路径、具体要修改/新增什么内容）
- step_type: "code_generate" | "build" | "test" | "evaluate"
- dependencies: 依赖的 subtask id 列表

## 标准目录结构

源代码必须在 `src/`，构建产物输出到 `build/`：
```
项目根目录/
├── src/          ← 所有源代码
├── tests/        ← 测试代码
├── build/        ← 构建产物
├── docs/         ← 文档
├── Makefile / CMakeLists.txt
└── README.md
```

如果续接上下文中标记了"目录结构不规范"，必须在计划最前面加入重构步骤。

## 计划结构

0. **目录结构重构**（如果有不规范的目录）— step_type: "code_generate"
1. **重新生成不完整的文档**（如果有）— step_type: "code_generate"
2. **修改已有代码文件**（补充缺失功能的实现）— step_type: "code_generate"
3. **新增代码文件**（如果需要全新模块，放在 `src/`）— step_type: "code_generate"
4. **更新构建文件**（输出目录指向 `build/`）— step_type: "code_generate"
5. **build** — step_type: "build"
6. **test** — step_type: "test"
7. **evaluate** — step_type: "evaluate"

## 禁止事项

- ❌ 不要重新生成内容完整的文档（只重新生成被标记为"不完整"的）
- ❌ 不要重新生成内容完整的代码文件的全部内容，只修改需要补充的部分
- ❌ 不要把源代码放在 `source/`、`lib/`、`app/` 等非标准目录
- ❌ 不要把构建产物输出到项目根目录

文档语言: {self._config.get("doc_language", "zh")}
"""

        messages = [
            {"role": "system", "content": system_prompt},
        ]

        if project_context:
            context_str = json.dumps(project_context, indent=2, ensure_ascii=False)
            messages.append({
                "role": "user",
                "content": f"项目当前磁盘文件:\n{context_str}",
            })

        messages.append({
            "role": "user",
            "content": (
                f"{continuation_context}\n\n"
                f"---\n\n"
                f"请根据以上信息，生成增量续接计划。\n"
                f"只包含缺失功能的代码修改/新增 + build + test + evaluate。\n"
                f"不要生成任何文档步骤。不要重写已有代码文件。\n"
                f"返回 JSON 数组。"
            ),
        })

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

        # 确保末尾有 build/test/evaluate 步骤
        step_types = [s.step_type for s in subtasks]
        last_code_id = f"subtask_{len(subtasks) - 1}"
        if StepType.BUILD not in step_types:
            subtasks.append(SubTask(
                id=f"subtask_{len(subtasks)}",
                title="Build Project",
                description="Build the project to verify compilation",
                step_type=StepType.BUILD,
                dependencies=[last_code_id],
            ))
        if StepType.TEST not in step_types:
            subtasks.append(SubTask(
                id=f"subtask_{len(subtasks)}",
                title="Run Tests",
                description="Run test suite to validate functionality",
                step_type=StepType.TEST,
                dependencies=[f"subtask_{len(subtasks) - 1}"],
            ))
        if StepType.EVALUATE not in step_types:
            subtasks.append(SubTask(
                id=f"subtask_{len(subtasks)}",
                title="Quality Evaluation",
                description="Evaluate code quality and requirement coverage",
                step_type=StepType.EVALUATE,
                dependencies=[f"subtask_{len(subtasks) - 1}"],
            ))

        for i, st in enumerate(subtasks):
            st.id = f"subtask_{i}"

        return TaskPlan(task_id=task_id, subtasks=subtasks)

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
            # dependencies 可能是整数或字符串，统一转为字符串
            raw_deps = raw.get("dependencies", [])
            deps = [str(d) if not isinstance(d, str) else d for d in raw_deps]
            # 如果 LLM 返回纯数字（如 [1, 2]），转为 subtask_N 格式
            deps = [f"subtask_{d}" if d.isdigit() else d for d in deps]

            subtask = SubTask(
                id=f"subtask_{i}",
                title=raw.get("title", f"Step {i+1}"),
                description=raw.get("description", ""),
                step_type=StepType(raw.get("step_type", "code_generate")),
                dependencies=deps,
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
