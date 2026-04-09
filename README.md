# kedo — AI 全流程自动化开发工具

从需求到部署的全流程自动化开发工具。输入自然语言需求，kedo 自动完成需求分析、架构设计、代码生成、构建、测试、评估，直到部署上线。支持智能续接、增量开发、自动闭环修复。

## 核心特性

- **五步开发流程**：需求 → 设计文档 → 代码生成 → 测试评估 → 部署
- **智能续接**：输入"继续"自动识别上次进度，扫描项目现状，只补缺失功能
- **闭环修复**：评估不通过时自动分析失败原因 → 生成修复方案 → 重新规划执行（最多 5 轮）
- **自动修复**：编译/测试失败时 AI 自动分析错误并修复，最多重试 3 次
- **不完整文档检测**：续接时自动检测被截断的文档并重新生成
- **目录结构规范**：强制使用 `src/` + `build/` 标准目录，续接时自动重构不规范的目录
- **Web Dashboard**：实时流程图、VS Code 风格文件树、代码预览、部署环境引导

## 安装

```bash
git clone <repo-url>
cd kedo

# 创建虚拟环境
python -m venv .venv
source .venv/bin/activate

# 安装依赖
pip install -r requirements.txt

# 安装为命令行工具
pip install -e .
```

## 快速开始

### 1. 配置 LLM

```bash
# Claude (推荐)
export ANTHROPIC_API_KEY="sk-ant-..."

# 或 Kimi Code
export KIMI_API_KEY="sk-..."

# 或无 Key 体验 (Mock 模式)
kedo --provider mock
```

### 2. 启动

```bash
# REPL 模式（交互式）
cd my-project
kedo

# Server 模式（仅 Web Dashboard）
kedo server

# 指定端口和提供商
kedo --port 9000 --provider anthropic
```

### 3. 输入需求

```
kedo> 写一个在 Switch 上运行的视频播放器，能连接 NFS 共享存储播放视频
```

kedo 自动执行全流程。你可以随时 `/pause` 暂停查看进度。

### 4. 续接开发

```
kedo> 继续
```

kedo 会：
1. 扫描磁盘上已有的文件和代码
2. 加载上次评估结果（哪些功能缺失）
3. 检测不完整的文档和不规范的目录
4. 生成增量计划，只做缺失的部分
5. 执行计划

## 标准项目目录结构

kedo 生成的项目强制使用以下目录结构：

```
项目根目录/
├── src/              ← 所有源代码（不是 source/、lib/、app/）
├── tests/            ← 测试代码
├── build/            ← 构建产物输出（.nro、.exe 等，不在根目录）
├── docs/             ← 文档
│   ├── requirement/  ← 需求文档
│   │   ├── requirement.md
│   │   └── user-stories.md
│   ├── sdd/          ← 设计文档
│   │   ├── architecture.md
│   │   ├── api-design.md
│   │   ├── database-design.md
│   │   └── module-design.md
│   ├── deploy/       ← 部署文档
│   │   └── deployment.md
│   └── test/         ← 测试文档
│       ├── test-plan.md
│       ├── test-cases.md
│       └── automation.md
├── config/           ← 配置文件（可选）
├── Makefile / CMakeLists.txt
├── Dockerfile / docker-compose.yml
└── README.md
```

续接时，如果检测到不规范的目录（如 `source/` 而非 `src/`），会自动加入重构步骤。

## REPL 命令

| 命令 | 说明 |
|------|------|
| `/help` | 显示帮助 |
| `/status` | 查看当前任务状态 |
| `/flow` | 显示流程图 |
| `/pause` | 暂停当前任务 |
| `/resume` | 恢复暂停的任务 |
| `/continue` | 从检查点续接历史任务 |
| `/candidates` | 查看候选版本 |
| `/discuss` | 参与闭环讨论 |
| `/history` | 查看迭代历史 |
| `/login` | 切换 LLM 提供商 |
| `/web` | 打开 Dashboard |
| `/config` | 查看配置 |
| `/quit` | 退出 |

自然语言输入中包含"继续""接着""上次"等关键词时，自动检测续接意图。

## Web Dashboard

启动后访问 `http://localhost:8000`，提供：

- **Pipeline 视图**：实时流程图 + 任务列表 + 控制台/日志
- **文档视图**：浏览和编辑项目文档，Mermaid 图表渲染
- **代码视图**：VS Code 风格目录树 + 代码预览，显示项目根路径
- **打包视图**：构建产物列表 + 候选版本 + 打包监控统计
- **部署视图**：部署环境列表 + 分步准备指南 + 环境检测 + 部署记录
- **测试视图**：测试结果 + 覆盖率

Dashboard 支持：
- 输入"继续"时弹出续接确认弹窗（智能续接）
- 任务列表按最新排序，可滚动
- 代码文件树折叠/展开

## 智能续接机制

当用户输入包含续接关键词（继续、接着、上次等）时：

```
用户输入 "继续"
  ↓
① 查询 /tasks/resumable 找到有 checkpoint 的历史任务
② 弹出确认弹窗，显示任务 ID、描述、进度
③ 扫描项目现状：
   - 磁盘上有哪些源码文件（提取摘要）
   - 哪些文档不完整（截断、过短、空章节）
   - 目录结构是否规范（source/ → src/）
④ 加载上次评估报告：已满足/缺失的需求
⑤ Planner 生成增量计划（独立 prompt，不继承固化模板）：
   - 目录重构（如有）
   - 重新生成不完整文档
   - 补充缺失功能代码
   - build → test → evaluate
⑥ 执行增量计划
```

## 部署环境检测

部署页面自动检测：

- Docker / Docker Compose
- 编译工具链（Make、devkitPro 等）
- 项目文件（Makefile、源代码、构建产物）
- 部署目标（根据项目类型自动识别：Switch、Docker、Node.js 等）

缺失的依赖会显示安装提示，人工步骤标记为黄色。

## API 接口

| 方法 | 路径 | 说明 |
|------|------|------|
| POST | `/api/tasks` | 创建新任务 |
| GET | `/api/tasks` | 列出所有任务 |
| GET | `/api/tasks/resumable` | 列出可续接的历史任务 |
| GET | `/api/tasks/{id}` | 获取任务详情 |
| POST | `/api/tasks/{id}/resume-checkpoint` | 智能续接（从检查点恢复） |
| POST | `/api/tasks/{id}/pause` | 暂停任务 |
| POST | `/api/tasks/{id}/resume` | 恢复暂停的任务 |
| GET | `/api/tasks/{id}/candidates` | 获取候选版本 |
| GET | `/api/tasks/{id}/discussion` | 获取讨论状态 |
| POST | `/api/tasks/{id}/discussion/input` | 提交讨论意见 |
| GET | `/api/code/status` | 代码监控（含磁盘文件扫描） |
| GET | `/api/code/file` | 读取代码文件内容 |
| GET | `/api/build/status` | 打包监控（含构建产物 + 候选版本） |
| GET | `/api/deploy/guide` | 部署指南（从部署文档解析） |
| GET | `/api/deploy/environment` | 部署环境检测 |
| GET | `/api/deploy/status` | 部署状态 |
| GET | `/api/docs` | 文档目录树 |
| GET | `/api/docs/file` | 读取文档内容 |
| WS | `/api/ws` | WebSocket 实时事件流 |

## 配置

kedo 按以下优先级查找配置：

1. `kedo.yaml` / `kedo.yml`（当前目录）
2. `.kedo.yaml`（当前目录）
3. `~/.config/kedo/config.yaml`（全局）
4. 环境变量（最高优先级）

```yaml
# kedo.yaml
llm_provider: "anthropic"         # anthropic / kimi / kimi-code / openai / ollama / mock
model: "claude-sonnet-4-20250514"
max_retries: 3                    # 子任务最大重试次数
auto_fix: true                    # 自动修复
min_eval_score: 70                # 最低评估通过分数
max_iterations: 5                 # 最大闭环迭代次数
auto_discussion: true             # AI 自动选择修复方案
doc_language: "zh"                # 文档语言
sandbox_mode: true                # Shell 沙箱
host: "0.0.0.0"
port: 8000
```

| 环境变量 | 说明 |
|----------|------|
| `ANTHROPIC_API_KEY` | Claude API Key |
| `KIMI_API_KEY` | Kimi API Key |
| `OPENAI_API_KEY` | OpenAI API Key |
| `KEDO_PORT` | 服务端口 |
| `KEDO_HOST` | 绑定地址 |
| `KEDO_PROVIDER` | LLM 提供商 |

## 支持的 LLM

| 提供商 | 配置值 | 说明 |
|--------|--------|------|
| Anthropic | `anthropic` | Claude 系列 |
| Kimi Code | `kimi-code` | Kimi K2.5 编程专用 |
| Kimi | `kimi` | Kimi K2.5 通用 |
| OpenAI | `openai` | GPT 系列 |
| Ollama | `ollama` | 本地模型 |
| Mock | `mock` | 模拟模式，用于测试 |

## 项目结构

```
kedo/
├── kedo.py                 # CLI 入口
├── cli/
│   ├── repl.py             # 交互式 REPL（含续接检测）
│   └── theme.py            # 终端主题
├── core/
│   ├── agent_loop.py       # Agent 主循环（智能续接 + 闭环）
│   ├── planner.py          # 任务规划器（新建 + 续接两套 prompt）
│   ├── evaluator.py        # 质量评估器
│   ├── state_manager.py    # 状态管理（持久化任务索引）
│   ├── version_manager.py  # 候选版本管理
│   └── memory.py           # 上下文记忆
├── api/
│   ├── server.py           # FastAPI 应用
│   ├── routes.py           # REST API（含环境检测、部署指南）
│   ├── schemas.py          # 数据模型
│   └── websocket.py        # WebSocket 推送
├── tools/
│   ├── code_generator.py   # 代码生成
│   ├── file_tool.py        # 文件操作
│   ├── shell_executor.py   # Shell 执行
│   └── test_runner.py      # 测试运行
└── dashboard/
    └── index.html          # Web Dashboard
```

## 许可证

MIT License
