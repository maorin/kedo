"""
文档模板 — 固化的文档结构定义

定义 kedo 五步流程中四个文档目录的标准模板结构，
供 Planner 在生成计划时直接引用，确保每次生成的文档
都遵循统一的结构规范。

四个文档目录：
  docs/requirement/ — 需求文档（从用户提示词分析生成）
  docs/sdd/        — SDD 设计文档（架构、API、数据库、模块设计）
  docs/deploy/     — 部署文档（部署方案、环境配置）
  docs/test/       — 测试文档（测试计划、用例、自动化方案）
"""

from __future__ import annotations

# ============================================================
# 需求文档模板
# ============================================================

REQUIREMENT_TEMPLATES = {
    "requirement/requirement.md": {
        "title": "需求概述文档",
        "description": "从用户提示词中提取并结构化需求，生成完整的需求分析文档",
        "outline": """# 需求文档

## 1. 项目概述
- 项目名称
- 项目目标（从用户提示词提取核心目标）
- 项目背景与动机

## 2. 功能需求
### 2.1 核心功能列表
（列出从提示词中分析出的所有功能需求，使用表格：编号、功能名、描述、优先级）

### 2.2 功能详细描述
（逐个功能展开描述：输入、输出、处理逻辑、约束条件）

## 3. 非功能需求
### 3.1 性能需求
### 3.2 安全需求
### 3.3 可用性需求
### 3.4 兼容性需求

## 4. 约束与假设
- 技术约束
- 业务约束
- 前置假设

## 5. 术语表
（项目相关术语定义）
""",
    },
    "requirement/user-stories.md": {
        "title": "用户故事文档",
        "description": "将需求拆解为标准用户故事格式，含验收标准",
        "outline": """# 用户故事

（按功能模块分组，每个用户故事遵循以下格式）

## [模块名称]

### US-XXX：[故事标题]

**作为** [角色]
**我希望** [功能]
**以便于** [价值]

**验收标准**：

- [具体可验证的条件1]
- [具体可验证的条件2]
- [边界条件和异常情况]

（为每个核心功能生成对应的用户故事，编号连续）
""",
    },
}


# ============================================================
# SDD 设计文档模板
# ============================================================

SDD_TEMPLATES = {
    "sdd/architecture.md": {
        "title": "系统架构设计",
        "description": "系统整体架构设计，包含技术栈选型、架构图、服务划分、分层结构",
        "outline": """# 系统架构设计

## 1. 系统概述
（项目整体技术架构描述、设计理念）

## 2. 技术栈
（使用表格列出：层次、技术选型、版本、说明）

## 3. 架构图
```mermaid
graph TB
    （系统整体架构图，包含客户端、入口层、服务层、数据层）
```

## 4. 服务划分
### 4.x [服务名称]
- 职责
- 端口
- 数据表
- 关键接口

## 5. 分层架构
```mermaid
graph LR
    （每个服务内部的分层结构：Router → Service → Repository → DB）
```

| 层次 | 职责 | 对应文件 |
|------|------|---------|

## 6. 通信机制
### 6.1 同步通信
### 6.2 认证传播

## 7. 目录结构
```
（项目文件目录树）
```
""",
    },
    "sdd/api-design.md": {
        "title": "API 设计文档",
        "description": "RESTful API 接口详细规范，包含路径、方法、请求/响应格式和错误码",
        "outline": """# API 设计文档

## 1. 概述
- 基础路径
- 数据格式
- 认证方式
- API 文档地址

## 2. 认证
### 2.1 获取 Token
（请求体 + 响应示例）
### 2.2 认证头格式

## 3. [模块] 接口
### 3.x [接口名称]
```
[METHOD] /api/v1/[path]
Authorization: Bearer <token>
```
（请求参数表格：字段、类型、必填、默认值、说明）
（响应示例 JSON）

## N. 数据模型
### N.1 [ModelName]Response
（完整 JSON 响应示例）

## N+1. 错误码规范
| HTTP 状态码 | 含义 | 场景 |
|------------|------|------|

**错误响应格式**：
```json
{"detail": "错误信息"}
```

## N+2. 接口流程图
```mermaid
sequenceDiagram
    （完整的请求 → 认证 → 业务处理 → 响应流程）
```
""",
    },
    "sdd/database-design.md": {
        "title": "数据库设计文档",
        "description": "数据库架构设计，包含 ER 图、表结构、索引策略、约束关系",
        "outline": """# 数据库设计文档

## 1. 概述
（数据库选型、ORM 框架、版本信息）

## 2. ER 图
```mermaid
erDiagram
    （实体关系图，标注 PK/FK/UK、字段类型、关系说明）
```

## 3. 表结构
### 3.x [表名] 表
| 字段 | 类型 | 约束 | 说明 |
|------|------|------|------|
（每个表的完整字段定义）

## 4. 索引策略
| 表 | 索引 | 类型 | 用途 |
|----|------|------|------|

## 5. 状态枚举
```mermaid
stateDiagram-v2
    （状态流转图）
```

## 6. 外键与级联
（外键关系及级联删除/更新策略）

## 7. 连接配置
（开发/生产环境数据库连接配置示例）

## 8. 数据库安全
（访问控制、敏感数据处理策略）
""",
    },
    "sdd/module-design.md": {
        "title": "模块设计文档",
        "description": "模块拆分及内部设计，包含模块关系图、核心流程、业务规则",
        "outline": """# 模块设计文档

## 1. 模块总览
```mermaid
graph TD
    （模块依赖关系图，包含路由层、业务层、数据层、横切关注）
```

## 2. [模块名称] 模块
### 2.1 职责
（模块核心职责列表）

### 2.2 核心流程
```mermaid
sequenceDiagram
    （模块核心业务流程时序图）
```

### 2.3 业务规则
（数据校验规则、状态转换规则、安全规则等）

（为每个核心模块重复以上结构）

## N. 数据库连接模块
### N.1 连接配置
### N.2 会话管理
""",
    },
}


# ============================================================
# 部署文档模板
# ============================================================

DEPLOY_TEMPLATES = {
    "deploy/deployment.md": {
        "title": "部署方案文档",
        "description": "完整的部署方案，包含部署架构、环境配置、部署流程、监控方案",
        "outline": """# 部署方案文档

## 1. 部署概述
- 部署目标环境
- 部署方式（容器化 / 传统部署）
- 高可用策略

## 2. 部署架构
```mermaid
graph TB
    （部署拓扑图：负载均衡、应用服务器、数据库、缓存等组件关系）
```

## 3. 环境配置
### 3.1 开发环境
### 3.2 测试环境
### 3.3 生产环境

## 4. Docker 配置
### 4.1 Dockerfile
（完整 Dockerfile 内容及注释说明）
### 4.2 docker-compose.yml
（完整编排配置及服务说明）

## 5. 环境变量
| 变量名 | 说明 | 默认值 | 必填 |
|--------|------|--------|------|

## 6. 部署流程
### 6.1 首次部署步骤
### 6.2 更新部署步骤
### 6.3 回滚流程

## 7. CI/CD 流水线
（自动构建、测试、部署的流水线配置）

## 8. 监控与日志
### 8.1 健康检查
### 8.2 日志收集
### 8.3 告警策略

## 9. 安全加固
- 网络策略
- 密钥管理
- HTTPS 配置
""",
    },
}


# ============================================================
# 测试文档模板
# ============================================================

TEST_TEMPLATES = {
    "test/test-plan.md": {
        "title": "测试计划",
        "description": "整体测试策略和计划，包含测试范围、方法、环境、时间表",
        "outline": """# 测试计划

## 1. 测试概述
- 测试目标
- 测试范围
- 测试策略（单元测试、集成测试、端到端测试的比例）

## 2. 测试环境
| 项目 | 配置 |
|------|------|
（操作系统、数据库、依赖版本等）

## 3. 测试分类
### 3.1 单元测试
- 覆盖目标
- 测试框架
- 覆盖率要求

### 3.2 集成测试
- 测试范围（API 接口测试、数据库集成测试）
- 测试数据策略

### 3.3 端到端测试
- 关键业务流程覆盖

## 4. 质量指标
| 指标 | 目标值 |
|------|--------|
（代码覆盖率、通过率、性能指标等）

## 5. 风险与对策
| 风险 | 影响 | 对策 |
|------|------|------|
""",
    },
    "test/test-cases.md": {
        "title": "测试用例文档",
        "description": "详细的测试用例，按模块分组，包含正常流程和异常流程",
        "outline": """# 测试用例

## 1. [模块名称]

### TC-XXX：[测试用例名称]
- **前置条件**：
- **测试步骤**：
  1. [步骤1]
  2. [步骤2]
- **预期结果**：
- **优先级**：高/中/低

（按模块分组，每个模块包含：正常流程用例 + 异常流程用例 + 边界条件用例）

（为每个 API 接口生成对应的测试用例，覆盖：
  - 正常请求（200/201/204）
  - 参数校验失败（400/422）
  - 认证失败（401）
  - 权限不足（403）
  - 资源不存在（404）
  - 数据冲突（409）
）
""",
    },
    "test/automation.md": {
        "title": "自动化测试方案",
        "description": "自动化测试框架选型、结构设计、CI集成方案",
        "outline": """# 自动化测试方案

## 1. 框架选型
| 测试类型 | 框架 | 说明 |
|---------|------|------|
（单元测试框架、API 测试框架、性能测试工具等）

## 2. 项目结构
```
tests/
├── unit/           # 单元测试
├── integration/    # 集成测试
├── e2e/            # 端到端测试
├── fixtures/       # 测试夹具
├── conftest.py     # pytest 配置
└── README.md
```

## 3. 测试代码示例
### 3.1 单元测试示例
```python
（关键模块的单元测试代码示例）
```

### 3.2 API 集成测试示例
```python
（API 端到端测试代码示例，使用 TestClient）
```

## 4. 测试数据管理
- 测试数据库策略（内存数据库 / 测试专用实例）
- Fixture 设计
- 数据清理策略

## 5. CI 集成
```yaml
（CI 流水线配置示例，如 GitHub Actions）
```

## 6. 覆盖率报告
- 覆盖率工具配置
- 报告生成与查看方式
- 覆盖率门限设置
""",
    },
}


# ============================================================
# 汇总所有模板
# ============================================================

ALL_DOC_TEMPLATES = {
    **REQUIREMENT_TEMPLATES,
    **SDD_TEMPLATES,
    **DEPLOY_TEMPLATES,
    **TEST_TEMPLATES,
}

# 按目录分组的模板（供 planner 按阶段引用）
DOC_CATEGORIES = {
    "requirement": {
        "label": "需求文档",
        "description": "从用户提示词中分析提取，生成结构化需求文档",
        "templates": REQUIREMENT_TEMPLATES,
    },
    "sdd": {
        "label": "SDD 设计文档",
        "description": "软件设计文档，包含架构、API、数据库、模块设计",
        "templates": SDD_TEMPLATES,
    },
    "deploy": {
        "label": "部署文档",
        "description": "部署方案、环境配置、CI/CD 流水线",
        "templates": DEPLOY_TEMPLATES,
    },
    "test": {
        "label": "测试文档",
        "description": "测试计划、测试用例、自动化测试方案",
        "templates": TEST_TEMPLATES,
    },
}


def get_all_doc_paths() -> list[str]:
    """获取所有文档模板的文件路径列表"""
    return list(ALL_DOC_TEMPLATES.keys())


def get_doc_generation_prompt(category: str) -> str:
    """
    获取某个文档分类的生成指令（供 planner 注入到 subtask description 中）

    Args:
        category: 文档分类 key (requirement / sdd / deploy / test)

    Returns:
        包含该分类下所有文档模板的详细生成指令
    """
    cat = DOC_CATEGORIES.get(category)
    if not cat:
        return ""

    parts = [f"=== {cat['label']} ===", f"说明: {cat['description']}", ""]

    for path, tmpl in cat["templates"].items():
        parts.append(f"### 文件: docs/{path}")
        parts.append(f"标题: {tmpl['title']}")
        parts.append(f"要求: {tmpl['description']}")
        parts.append(f"文档大纲:\n{tmpl['outline']}")
        parts.append("")

    return "\n".join(parts)


def get_compact_template_prompt() -> str:
    """
    获取精简版文档模板提示词（用于 system prompt）

    只列出文件路径和标题，不包含完整大纲，
    减小 system prompt 体积，避免 LLM API 超时。
    完整大纲在 _ensure_doc_steps() 中注入到 subtask description。
    """
    lines = []
    for cat_key, cat in DOC_CATEGORIES.items():
        lines.append(f"[{cat['label']}] {cat['description']}")
        for path, tmpl in cat["templates"].items():
            lines.append(f"  - docs/{path} — {tmpl['title']}：{tmpl['description']}")
    return "\n".join(lines)


def get_full_template_prompt() -> str:
    """
    获取完整的文档模板提示词（包含全部 4 个分类的详细大纲）

    用于注入到 subtask description 中，确保文档内容结构完整。
    注意：不要用于 system prompt（体积太大会导致 API 超时）。
    """
    sections = []
    for cat_key, cat in DOC_CATEGORIES.items():
        sections.append(get_doc_generation_prompt(cat_key))
    return "\n".join(sections)
