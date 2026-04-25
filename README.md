# kedo — AI 全流程自动化开发工具

从需求到部署的全流程自动化开发工具。输入自然语言需求，kedo 自动完成需求分析、架构设计、代码生成、构建、测试、评估，直到部署上线。

## 核心特性

- **ReactAgent 主循环**：唯一的 LLM 驱动 ReAct Agent（Think→Act→Observe），通过 15 个工具完成所有任务；简单问题直接回答，复杂任务自动规划→编码→编译→测试
- **可选双 Agent 对抗（方案 C / Actor-Critic）**：开启后由独立 Reviewer Agent（不同 LLM provider）在 `evaluate` / `commit_candidate` 关卡强制过审，物理破 Self-eval drift；一行配置 `reviewer_provider` 启用 / 关闭，零迁移成本。详见 [`docs/dual-agent-architecture.md`](docs/dual-agent-architecture.md)
- **多 LLM 支持**：Kimi K2.5 / Kimi Code 2.5、Claude（Anthropic）、DeepSeek v4-pro、OpenAI、Ollama，运行时 `/login` 热切换；Producer 与 Reviewer 可选不同 provider
- **Function Calling + 文本 ReAct 双模式**：原生支持 OpenAI/Anthropic function calling；对不支持的端点（Kimi Code）自动回退文本 ReAct
- **LLM 驱动自动修复**：编译/测试失败时 AI 分析 stderr 并修复代码，ProfileGuard 拦掉破坏性补丁，收敛检测防死循环
- **智能续接**：输入"继续"自动识别上次进度；新 task 启动时继承最近失败 task 的上下文
- **平台感知代码生成**：自动扫描目标平台库/头文件，注入平台知识和 CMakeLists 模板
- **项目 Profile 系统**：LLM 自动生成构建档案（build/test/deploy 命令），跨 session 缓存、失败历史跟踪；`shell_execute` 也会复用 profile 注入 `DEVKITPRO` 等环境变量
- **API Key 安全 + Shell 沙箱**：密钥存储在 `~/.config/kedo/config.yaml`（权限 0600），`shell_execute` 拦提权（sudo/su/pkexec）+ DEVNULL stdin 防 tty 劫持 + 退出码非 0 时把 stdout 尾部回填到 error，避免"空 error 误以为崩了"
- **Web Dashboard**：工作台 + 文件浏览 + 代码预览 + 部署引导 + 实时事件流；顶栏显示当前 LLM 与 Reviewer 状态

## 实战验证

kedo 已在真实项目 **switchvideo**（Nintendo Switch NFS 视频播放器）上验证：

| 阶段 | 内容 | 状态 |
|------|------|------|
| Hello World | libnx console 输出 + nxlink 部署到 Switch 真机 | kedo 独立完成 |
| UI 设计 | 4 屏界面设计文档（线框图 + 状态机 + 配色方案） | kedo 独立完成 |
| HTTP 连通 | Switch libcurl → 局域网 HTTP 服务器 → 读取文件 | kedo + 人工调试 |
| 视频播放 | SDL2 渲染 + 服务端 ffmpeg 实时转码 + 音视频同步 | 人工完成 |
| 双 Agent 评审 | Producer (Kimi-code) + Reviewer (DeepSeek-v4-pro) 跨 provider 评审 | 已落地，实战中 |

> **当前能力边界**：kedo 在熟悉平台上能独立完成全流程（Python/Node.js 项目）。交叉编译平台（如 Switch devkitPro）经过 G1-G6 修复 + P3 单 Agent 迁移后能独立完成增量开发；从零构建复杂项目仍可能需要人工辅助。开启双 Agent 后，Reviewer 会拦掉"自评 OK 但实际有缺陷"的候选版本（典型：本地 stub 冒充真 NFS、测试覆盖为 0），让 Producer 强制再迭代。

## 安装

```bash
git clone https://github.com/maorin/kedo.git
cd kedo
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
pip install -e .
```

> 想用 OpenAI / DeepSeek 走 OpenAI 兼容端点：`pip install openai`（requirements.txt 标为可选）

## 快速开始

### 1. 配置 LLM

```bash
# Kimi (推荐，性价比高)
export KIMI_API_KEY="sk-..."

# 或 Claude
export ANTHROPIC_API_KEY="sk-ant-..."

# 或 DeepSeek
export DEEPSEEK_API_KEY="sk-..."

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
kedo> 写一个在 Switch 上运行的视频播放器，能连接 NFS 共享存储播放视频
```

kedo 自动执行全流程：规划 → 代码生成 → 构建 → 测试 → 评估 → 部署。

### 4. 续接开发

```
kedo> 继续
```

kedo 扫描项目现状 → 加载历史评估 → 生成增量计划 → 只做缺失部分。

### 5. 启用双 Agent 对抗（可选）

编辑 `~/.config/kedo/config.yaml`：

```yaml
# Producer 不变
llm_provider: "kimi-code"
kimi_api_key: "sk-..."

# Reviewer (建议挑不同 provider，否则破不了 Self-eval drift)
reviewer_provider: "deepseek"     # none(默认) | anthropic | openai | kimi | kimi-code | deepseek
reviewer_api_key: "sk-..."
reviewer_min_score: 70
reviewer_pre_commit_gate: true    # commit_candidate 前强制 Reviewer 过审
```

重启 kedo 后 `/api/llm/status` 会返回 `"reviewer":{"active":true,...}`，Dashboard 顶栏变成 `🛡 双Agent: Producer=kimi-code/... | Reviewer=deepseek/...`。任何时候 `reviewer_provider: "none"` 即回到单 Agent 行为。

## 一图流：架构概览

```
用户输入
   ↓
Producer = ReactAgent (LLM-driven ReAct loop)
   ↓                                       ↑
 选择工具 → 执行 → 观察结果 → 再思考 ────────┘
   │
   ├─ file_read / file_write / file_search
   ├─ shell_execute (沙箱 + 注入 profile required_env)
   ├─ build / test_run / git
   ├─ code_generate (+ ProfileGuard)
   ├─ plan_development (拆解需求 → checkpoint)
   ├─ auto_fix (诊断 stderr → 单轮补丁)
   ├─ ★ evaluate ─────────────┐
   ├─ ★ commit_candidate ──────┤  双 Agent 模式下跨过去
   ├─ propose_alternatives     │
   ├─ pause_for_human          │
   └─ respond                  ▼
                       ┌─────────────────┐
                       │ Reviewer Agent  │  独立 LLM provider
                       │ (DeepSeek/...)  │  独立 context
                       │                 │  跨关卡累积判决
                       │ → ReviewResult  │  approve/score/comments
                       └─────────────────┘
```

每一步"做什么"由 LLM 自主决定，不是预先规划的刚性流程。Reviewer 关卡只在双 Agent 模式开启时介入，单 Agent 默认下是直通的。

## 文档导航

| 场景 | 文档 |
|---|---|
| 我想懂 kedo 的架构/原理/安全机制 | [docs/architecture.md](docs/architecture.md) |
| 我想用双 Agent 对抗模式（方案 C） | [docs/dual-agent-architecture.md](docs/dual-agent-architecture.md) |
| 我要查 REPL 命令 / API / 配置 / Dashboard 用法 | [docs/reference.md](docs/reference.md) |
| 我要对接新 LLM / 配置多 LLM 切换 | [docs/llm-providers.md](docs/llm-providers.md) |
| 我想了解 kedo 的改进历程和已知局限 | [docs/changelog.md](docs/changelog.md) |
| 我想深入理解 kedo 的设计权衡和未解问题 | [docs/deep-dives/](docs/deep-dives/) |

## 许可证

MIT License
