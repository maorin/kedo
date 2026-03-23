# kedo
kedo — AI 开发助手

## 介绍

**kedo** 是一个从需求到部署的全流程自动化开发工具。输入一段自然语言需求，kedo 自动完成以下流程：

| 阶段 | 描述 |
|------|------|
| 需求分析 | 解析需求，输出背景、目标、范围、约束和验收标准 |
| 任务拆解 | 将需求分解为有序的开发任务列表 |
| 代码生成 | 为每个任务生成可运行的代码 |
| 测试     | 为生成的代码生成单元测试 |
| 评估     | 综合评估实现质量（0–100 分） |
| 人工审查 | 展示代码和评估报告，由人工决策是否部署 |
| 部署上线 | 将生成的代码和测试写入磁盘，并生成部署清单 |

---

## 安装

```bash
pip install kedo
```

或从源码安装：

```bash
git clone https://github.com/maorin/kedo.git
cd kedo
pip install -e ".[dev]"
```

---

## 快速开始

### 前置条件

设置 OpenAI API Key：

```bash
export OPENAI_API_KEY=sk-...
```

### 运行流程

```bash
# 运行完整流程（交互式人工审查）
kedo run "构建一个管理待办事项的 REST API"

# 使用自动审批（跳过人工确认）
kedo run --auto-approve "构建一个用户登录注册系统"

# 指定输出目录和模型
kedo run --output-dir ./my-project --model gpt-4o "构建一个博客系统"

# 不生成部署清单
kedo run --no-manifest "构建一个计算器 CLI 工具"

# 查看帮助
kedo --help
kedo run --help
```

---

## 配置文件

在项目根目录创建 `kedo.yaml`（可选）：

```yaml
model: gpt-4o                   # 使用的 LLM 模型
api_key: sk-...                 # OpenAI API Key（建议用环境变量）
base_url: ~                     # 自定义 API 地址（可选）
output_dir: output              # 生成文件的输出目录
auto_approve: false             # 是否跳过人工审查
generate_manifest: true         # 是否生成部署清单（Dockerfile 等）
```

配置优先级：CLI 参数 > 配置文件 > 环境变量 > 默认值。

---

## 项目结构

```
kedo/
├── kedo/
│   ├── cli.py              # CLI 入口
│   ├── pipeline.py         # 流程编排器
│   ├── config.py           # 配置加载
│   ├── llm/
│   │   └── client.py       # LLM 客户端封装
│   └── stages/
│       ├── base.py         # Stage 基类
│       ├── analysis.py     # 需求分析
│       ├── decomposition.py# 任务拆解
│       ├── codegen.py      # 代码生成
│       ├── testing.py      # 测试生成
│       ├── evaluation.py   # 评估
│       ├── review.py       # 人工审查
│       └── deployment.py   # 部署上线
└── tests/
    ├── test_pipeline.py
    ├── test_stages.py
    ├── test_config.py
    └── test_cli.py
```

---

## 开发

```bash
# 安装开发依赖
pip install -e ".[dev]"

# 运行测试
python -m pytest tests/ -v
```
