# kedo — AI 全流程自动化开发工具

从需求到部署的全流程自动化开发工具。输入自然语言需求，kedo 自动完成需求分析、架构设计、代码生成、构建、测试、评估，直到部署上线。支持智能续接、增量开发、自动闭环修复。

## 核��特性

- **全流程自动化**：需求 → 设计 → 代码生成 → 构建 → 测试 → 评估 → 部署
- **智能续接**：输入"继续"自动识别上次进度，扫描项目现状，只补缺失功能
- **闭环修复**：评估不通过时自动分析原因 → 生成修复方案 → 重新规划执行（最多 5 轮）
- **LLM 驱动自动修复**：编译/测试失败时 AI 分析 stderr 并修复��码，支持重复错误早停
- **项目 Profile 系统**：LLM 自���生��项���构建档案（build/test/deploy 命令），支持��� session 缓存、失败历��跟踪、自动重生成
- **多维���评估**：需求匹配 / 代码质量 / 测试覆盖 / 安全性四维度加权评分，交叉编译项目自��跳过测试维度
- **产品需求智能总结**：从所有���务对话��� LLM 提炼当���产品需求，生成可复用的提示词
- **Web Dashboard**：工作台 + 文件浏览 + 代码预览 + 部署引导 + 讨论面板

## 实战验证

kedo 已在真实项目 **switchvideo**（Nintendo Switch NFS 视频播放器）上验证：

| 阶段 | 内容 | 状态 |
|------|------|------|
| Hello World | libnx console 输出 + nxlink 部���到 Switch 真机 | kedo 独立完成 |
| UI 设计 | 4 屏界面设计文档（线框图 + 状态机 + 配色方案） | kedo 独立完成 |
| HTTP 连通 | Switch libcurl → 局域网 HTTP 服务器 → 读取文件 | kedo + 人工调试 |
| 视频播放 | SDL2 渲染 + 服务端 ffmpeg 实时转码 + 音视频同步 | 人工完成 |

> **当前能力边界**：kedo 在熟悉平台上能独立完成全流程（Python/Node.js 项目）。对不熟悉的交叉编译平台（如 Switch devkitPro），LLM 会幻觉不存在的库名，需要人工辅助调试。改进��划见 [能力��距分析](#已知局限)。

## 安装

```bash
git clone https://github.com/maorin/kedo.git
cd kedo
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
pip install -e .
```

## 快速开始

### 1. 配置 LLM

```bash
# Kimi (推荐，性价比高)
export KIMI_API_KEY="sk-..."

# 或 Claude
export ANTHROPIC_API_KEY="sk-ant-..."

# 或无 Key 体验
kedo --provider mock
```

### 2. 启动

```bash
# REPL 模式（交互式）
cd my-project
kedo

# Server 模式（Web Dashboard）
kedo /path/to/project server

# 指定端口和提供商
kedo --port 9000 --provider kimi
```

### 3. 输入需求

```
kedo> 写一个在 Switch 上运行的视频播放器，能连接 NFS 共享存储���放视频
```

kedo 自动执行全流程：规划 → 代码生成 → 构建 → 测试 → 评估 → 部署。

### 4. 续接开发

```
kedo> 继续
```

kedo 扫描项目现状 → 加载历史评估 → 生成增量计划 → 只做缺失部��。

## Web Dashboard

启动后访问 `http://localhost:8000`：

### 工作台视图
- **左侧**：��务列表（创��/暂停/恢复/停止）
- **中间**：控制台实���输出 + 日志 + 讨论面板
- **右侧**：子任务计划（进度条 + 步骤状态）
- **底部**：新建任务输入���

### 任务暂停与恢复
当 kedo 遇到无法自��修复的错误时：
- 右侧面板顶部显示**橙色暂停 banner**
- 展示：失败步骤 + 完整错误信息 + 修复建议
- 提供**文本输入框**��写入���议��点"恢复"→ kedo 带着你的指导重新规划
- 无建议直接点"恢复"→ 从暂停点继续

### 产品需求总结
讨论面板自动汇总所有任���对话，LLM 生成：
- 功能需求清单（✅ 已完成 / 🔧 进行中 / ❌ 失败）
- 产品当前状态概要
- 可复用的需求提示词
- 下一步改进方向

任��完成/失败时自动刷新，也可���动点"刷新总结"。

### 其��视图
- **文档**：浏览和编辑项目文档，Mermaid 图表渲染
- **代码**：VS Code 风格目录树 + 代码预览
- **打包**：构建产物 + 候选版本
- **部署**：部署环境检测 + 分步准备指南
- **测试**：测试结果 + 覆盖率

## 项目 Profile 系统

kedo 为每个项目自动生成 `.kedo/project_profile.json`：

```json
{
  "type": "switch_homebrew",
  "build": {
    "command": "cmake -B build -S . -DCMAKE_TOOLCHAIN_FILE=... && cmake --build build",
    "artifacts": ["build/*.nro"]
  },
  "test": {
    "strategy": "skip",
    "reason": "Cross-compiled, cannot execute on host"
  },
  "deploy": {
    "method": "nxlink",
    "command": "nxlink -a <ip> build/switchvideo.nro"
  },
  "required_env": [
    {"name": "DEVKITPRO", "search_paths": ["/opt/devkitpro"], "verify_file": "cmake/Switch.cmake"}
  ]
}
```

特性：
- **LLM 自动生成**：首次构建时读取 Makefile/CMakeLists/package.json 等推断
- **失��历史跟踪**：`fail_count` + `total_regens` + `prior_attempts`，LLM 重生成时看到历次失败的 build_command 和 stderr 作为 negative examples
- **重生成上限**：`MAX_PROFILE_REGENS=3`，超过后停止自动重生成，交给人工
- **human_verified 保护**：标记后 auto_fix 不可��改，防止 LLM "简化"掉关键���建���数
- **环境变量探测**：`apply_required_env` 在构建前自动探测并设置 DEVKITPRO 等变量

## 自动修复系统

构建/测试失败时，kedo 的 auto_fix 三层防御：

| 层级 | 机制 | 说明 |
|------|------|------|
| Prompt 引导 | `human_verified` 的 profile 在修复上下文中标 `[READ-ONLY]` | 引导 LLM 不碰已验证配置 |
| 写入拦截 | 写文件前检查 `human_verified=true` | 阻止 LLM 修改��验证的 profile |
| 变更验证 | 写入后检查关键字段（TOOLCHAIN_FILE / test.strategy） | 回归时自动 revert |

auto_fix 流程：
1. 收集失败 stderr + 相关项目文件���profile���CMakeLists、源码）
2. LLM 诊断根因 + 输出修复 patch（完���文件��容）
3. 验��� patch 不会回归关键���置
4. 应用 patch → 重试构建
5. 重复错��早停：连��两次 stderr 指纹相似 → 判定 LLM 无法修复 → 升级到人工

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
| `/login` | 切�� LLM 提供商 |
| `/web` | 打开 Dashboard |
| `/config` | 查看配置 |
| `/quit` | 退出 |

## API 接口

| 方法 | 路径 | 说明 |
|------|------|------|
| POST | `/api/tasks` | 创建新任务 |
| GET | `/api/tasks` | 列出所有任务 |
| GET | `/api/tasks/resumable` | 列出可续接的历史任务 |
| GET | `/api/tasks/{id}` | 获取任务详情（含 plan + eval_report） |
| POST | `/api/tasks/{id}/resume-checkpoint` | 智能续接（带 additional_context） |
| POST | `/api/tasks/{id}/pause` | 暂停任务 |
| POST | `/api/tasks/{id}/resume` | 恢复暂停的任务 |
| GET | `/api/tasks/{id}/candidates` | 获取候选版本 |
| GET | `/api/tasks/{id}/discussion` | 获取讨论状态 |
| POST | `/api/tasks/{id}/discussion/input` | 提交讨论意见 |
| GET | `/api/product-summary` | 产品需��智能总结（LLM 生成） |
| GET | `/api/code/status` | 代码监控 |
| GET | `/api/code/file` | 读取代码文件 |
| GET | `/api/build/status` | 构建状态 |
| GET | `/api/deploy/guide` | 部署指南 |
| GET | `/api/deploy/environment` | 环境检测 |
| WS | `/api/ws` | WebSocket 实时事件流 |

## 配置

kedo 按以下优先级查找配置：

1. `kedo.yaml` / `kedo.yml`（项目目录）
2. `.kedo.yaml`（项目目录）
3. `~/.config/kedo/config.yaml`（全局）
4. 环境变量（最高优先级）

```yaml
# kedo.yaml
llm_provider: "kimi"              # anthropic / kimi / kimi-code / openai / ollama / mock
model: "kimi-k2.5"
max_retries: 3                    # 子任务最大重试次数
auto_fix: true                    # LLM 自动修复
min_eval_score: 70                # 最低评估通过分数
max_iterations: 5                 # 最大闭环迭代次数
max_profile_regens: 3             # Profile 最大重生成次数
auto_discussion: true             # AI 自动选择修复方案
doc_language: "zh"                # 文档语言
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
| Kimi Code | `kimi-code` | Kimi K2.5 编程专用（推荐） |
| Kimi | `kimi` | Kimi K2.5 通用 |
| Anthropic | `anthropic` | Claude 系列 |
| OpenAI | `openai` | GPT 系列 |
| Ollama | `ollama` | 本地模型 |
| Mock | `mock` | 模拟模式，用于测试 |

## 项目结构

```
kedo/                          17,289 行
├── kedo.py                    CLI 入口
├── cli/
│   ├── repl.py                交互式 REPL（1,146 行）
│   └── theme.py               终端主题
├── core/
│   ├── agent_loop.py          Agent 主循环 + 自��修�� + 智能续接（2,697 行）
│   ├── planner.py             任务规划器（838 行）
│   ├── evaluator.py           多维度质量评估（508 行）
│   ├── project_profile.py     项目 Profile 管理（543 行）
│   ├── state_manager.py       状态持久化（378 行）
│   ├── version_manager.py     候选版本管理（344 行）
│   └── memory.py              上下文记忆（321 行）
├── api/
│   ├── server.py              FastAPI 应用
│   ├── routes.py              REST API + 产品需求总结（2,072 行）
│   ├── schemas.py             数据模型（364 行）
│   └── websocket.py           WebSocket 推送
├── tools/
│   ├── code_generator.py      代码生成 + 校验（388 行）
│   ├── file_tool.py           文件操作
│   ├── shell_executor.py      Shell 执行（沙箱模式）
│   └── test_runner.py         测试运行
└── dashboard/
    └── index.html             Web Dashboard（4,776 行）
```

## 已知局限

kedo 在交叉编译等不熟悉的平台上存在以下能力差距（[详细分析](docs/kedo-gaps.md)）：

| 编号 | 问题 | 改进方向 |
|------|------|----------|
| G1 | LLM 幻觉不存在的库名 | 自动扫描目标平台 portlibs 注入 prompt |
| G2 | 不会迭代调试 build 错误 | code_generate 后 dry-run cmake 预检 |
| G3 | auto_fix 可能越修越坏 | 三层防御（已实现） |
| G4 | 不了解目标平台构建规范 | 平台知识文件系统 |
| G5 | CMakeLists 生成质量差 | 平��级 CMakeLists 模板 |
| G6 | 生成非代码文件 | 二进制文件走系���工具 |

## 许可证

MIT License
