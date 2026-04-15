# kedo — AI 全流程自动化开发工具

从需求到部署的全流程自动化开发工具。输入自然语言需求，kedo 自动完成需求分析、架构设计、代码生成、构建、测试、评估，直到部署上线。支持智能续接、增量开发、自动闭环修复、平台感知代码生成。

## 核心特性

- **全流程自动化**：需求 → 设计 → 代码生成 → 构建 → 测试 → 评估 → 部署
- **智能续接**：输入"继续"自动识别上次进度，扫描项目现状，只补缺失功能
- **闭环修复**：评估不通过时自动分析原因 → 生成修复方案 → 重新规划执行（最多 5 轮）
- **LLM 驱动自动修复**：编译/测试失败时 AI 分析 stderr 并修复代码，支持结构化错误解析、增量修复、历次失败回溯、重复错误早停
- **Bug Fix 快速通道**：用户报告运行时 bug（"会退出"、"应该是"等）自动跳过 planner 五步流程，直接读源码 → LLM 诊断 → 应用 patch → BUILD 验证
- **平台感知代码生成**：自动扫描目标平台库/头文件，注入平台知识和 CMakeLists 模板，消除 LLM 库名幻觉
- **项目 Profile 系统**：LLM 自动生成项目构建档案（build/test/deploy 命令），支持跨 session 缓存、失败历史跟踪、自动重生成、变更白名单保护
- **多维度评估**：需求匹配 / 代码质量 / 测试覆盖 / 安全性四维度加权评分，交叉编译项目自动跳过测试维度
- **产品需求智能总结**：从所有任务对话用 LLM 提炼当前产品需求，生成可复用的提示词
- **Web Dashboard**：工作台 + 文件浏览 + 代码预览 + 部署引导 + 讨论面板

## 实战验证

kedo 已在真实项目 **switchvideo**（Nintendo Switch NFS 视频播放器）上验证：

| 阶段 | 内容 | 状态 |
|------|------|------|
| Hello World | libnx console 输出 + nxlink 部署到 Switch 真机 | kedo 独立完成 |
| UI 设计 | 4 屏界面设计文档（线框图 + 状态机 + 配色方案） | kedo 独立完成 |
| HTTP 连通 | Switch libcurl → 局域网 HTTP 服务器 → 读取文件 | kedo + 人工调试 |
| 视频播放 | SDL2 渲染 + 服务端 ffmpeg 实时转码 + 音视频同步 | 人工完成 |

> **当前能力边界**：kedo 在熟悉平台上能独立完成全流程（Python/Node.js 项目）。对交叉编译平台（如 Switch devkitPro），经过三轮改进（G1-G6 全部修复）后已能独立完成增量开发（如新增页面），但复杂的从零构建仍可能需要人工辅助。

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
kedo> 写一个在 Switch 上运行的视频播放器，能连接 NFS 共享存储播放视频
```

kedo 自动执行全流程：规划 → 代码生成 → 构建 → 测试 → 评估 → 部署。

### 4. 续接开发

```
kedo> 继续
```

kedo 扫描项目现状 → 加载历史评估 → 生成增量计划 → 只做缺失部分。

## Web Dashboard

启动后访问 `http://localhost:8000`：

### 工作台视图
- **左侧**：任务列表（创建/暂停/恢复/停止）
- **中间**：控制台实时输出 + 日志 + 讨论面板
- **右侧**：子任务计划（进度条 + 步骤状态）
- **底部**：新建任务输入栏

### 任务暂停与恢复
当 kedo 遇到无法自动修复的错误时：
- 右侧面板顶部显示**橙色暂停 banner**
- 展示：失败步骤 + 完整错误信息 + 修复建议
- 提供**文本输入框**写入建议，点"恢复"→ kedo 带着你的指导重新规划
- 无建议直接点"恢复"→ 从暂停点继续

### 产品需求总结
讨论面板自动汇总所有任务对话，LLM 生成：
- 功能需求清单（✅ 已完成 / 🔧 进行中 / ❌ 失败）
- 产品当前状态概要
- 可复用的需求提示词
- 下一步改进方向

任务完成/失败时自动刷新，也可手动点"刷新总结"。

### 其他视图
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
- **失败历史跟踪**：`fail_count` + `total_regens` + `prior_attempts`，LLM 重生成时看到历次失败的 build_command 和 stderr 作为 negative examples
- **重生成上限**：`MAX_PROFILE_REGENS=3`，超过后停止自动重生成，交给人工
- **human_verified 保护**：标记后 auto_fix 不可修改，防止 LLM "简化"掉关键构建参数
- **环境变量探测**：`apply_required_env` 在构建前自动探测并设置 DEVKITPRO 等变量

## Bug Fix 快速通道

当用户提交的需求看起来是**运行时 bug 报告**（包含"会退出"、"应该是"、"不对"、"崩溃"、"bug"、"修复"、"instead of"、"should be" 等关键词）时，kedo 自动走 Bug Fix 快速通道，**跳过 planner 五步流程**：

```
用户: "长按R 会退出 应该是一直快进"
  │
  ├─ 1. _is_bug_report() 关键词识别
  ├─ 2. _find_bug_related_files() 定位相关源文件（小项目全读，大项目按 top-N 文件大小）
  ├─ 3. LLM 诊断（debugger 模式 prompt + 完整源码 + 平台知识）
  ├─ 4. 应用 patch（复用 auto_fix 的白名单 + 保护逻辑）
  └─ 5. BUILD 验证
```

与完整 pipeline 的对比：

| | 完整 pipeline | Bug Fix 通道 |
|---|---|---|
| 步骤 | planning → code_generate → build → test → evaluate | LLM 诊断 → patch → build |
| 源码输入 | 前 40 行摘要 | 全文完整内容 |
| 耗时 | ~15 分钟+ | ~6 分钟 |
| 回退策略 | — | LLM 判定无法单文件修 or 解析失败 → 回退正常流程 |

**适用场景**：已有项目中的运行时行为修复（UI、逻辑、控制流）。
**不适用**：新功能开发、架构重构、多文件级别的修改。

## 自动修复系统

构建/测试失败时，kedo 的 auto_fix 三层防御：

| 层级 | 机制 | 说明 |
|------|------|------|
| Prompt 引导 | `human_verified` 的 profile 在修复上下文中标 `[READ-ONLY]` | 引导 LLM 不碰已验证配置 |
| 写入拦截 | 写文件前检查 `human_verified=true` | 阻止 LLM 修改已验证的 profile |
| 变更验证 | 写入后检查关键字段（TOOLCHAIN_FILE / test.strategy） | 回归时自动 revert |

auto_fix 流程：
1. 结构化解析 stderr（cmake/gcc/ld 错误分类），聚焦第一个错误
2. 收集失败上下文：相关项目文件 + 历次失败记录（prior_attempts）
3. LLM 诊断根因 + 输出修复 patch（完整文件内容），历次失败作为 negative examples 避免重复犯错
4. Profile 变更白名单验证：auto_fix 只能修改 build/notes 字段，其他字段自动 revert
5. 应用 patch → 重试构建（增量修复：每次只修一个错误）
6. 重复错误早停：连续两次 stderr 指纹相似 → 判定 LLM 无法修复 → 升级到人工

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
| GET | `/api/product-summary` | 产品需求智能总结（LLM 生成） |
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

## LLM 适配

### 支持的提供商

| 提供商 | 配置值 | 默认模型 | API Key 环境变量 | 说明 |
|--------|--------|----------|------------------|------|
| Kimi Code | `kimi-code` | `kimi-k2.5` | `KIMI_API_KEY` | 编程专用端点（推荐） |
| Kimi | `kimi` | `kimi-k2.5` | `KIMI_API_KEY` | 通用 Moonshot 端点 |
| Anthropic | `anthropic` | `claude-sonnet-4-6` | `ANTHROPIC_API_KEY` | Claude 系列 |
| OpenAI | `openai` | `gpt-4` | `OPENAI_API_KEY` | GPT 系列 |
| Ollama | `ollama` | `codellama` | 无需 | 本地模型 |
| Mock | `mock` | — | 无需 | 模拟模式，用于测试/演示 |

### 适配架构

所有提供商实现统一的 `BaseLLMClient` 接口：

```python
class BaseLLMClient:
    async def chat(messages: list[dict]) -> str          # 非流式
    async def stream_chat(messages: list[dict]) -> str   # 流式（逐 token yield）
```

消息格式统一为 `[{"role": "system/user/assistant", "content": "..."}]`。各提供商差异在客户端内部处理（如 Anthropic 需要分离 system 消息）。

### 对接新 LLM 的步骤

1. 在 `api/server.py` 中新建 `XxxClient(BaseLLMClient)` 类，实现 `chat()` 和可选的 `stream_chat()`
2. 在 `create_llm_client()` 工厂函数中添加 `elif provider == "xxx":` 分支
3. 在 `kedo.yaml` 中配置 `llm_provider: "xxx"` 和对应的 `model`

### Prompt 模板与 LLM 适配要点

kedo 有 4 个核心 prompt 模板，对接新 LLM 时需确保其能正确遵循这些结构化输出要求：

| 模块 | Prompt 位置 | 输出格式 | 适配注意 |
|------|-------------|----------|----------|
| Planner | `core/planner.py` L34 | JSON（subtask 列表） | ~600 行 system prompt，含五步流程定义 + 文档模板，需要 LLM 有强指令遵循能力 |
| Evaluator | `core/evaluator.py` L33 | JSON（四维度评分） | 需要 LLM 严格按 schema 输出，弱模型易漏字段或分数格式错 |
| Code Generator | `tools/code_generator.py` L120 | 纯代码（无 markdown 包裹） | 动态注入平台知识 + CMakeLists 模板，prompt 较长（~2K token） |
| Auto Fix | `core/agent_loop.py` L1154 | JSON（diagnosis + patch） | 需要 LLM 输出完整文件内容而非 diff，弱模型可能输出截断或混入注释 |

**已知的 LLM 兼容性差异**：
- **Kimi K2.5**（当前主力）：指令遵循强，JSON 输出稳定，但偶尔幻觉不存在的库名（已通过 G1 平台扫描缓解）
- **Anthropic Claude**：system prompt 需从 messages 分离单独传入（客户端已处理），JSON 遵循能力强
- **OpenAI GPT-4**：兼容但未深度测试，code_generator 的 "纯代码无 markdown" 要求可能需要额外 prompt 强调

### Claude (Anthropic) 对接

默认模型 `claude-sonnet-4-6`。`AnthropicClient` 提供两级校验和错误归类：

- `validate_key_format(key)` — 本地只检查 `sk-ant-` 前缀 + 长度
- `validate()` — 实网 ping（`max_tokens=1`）确认 key + 模型可用
- 异常按 401 / 403 / 404 / 429 / 5xx / 网络分类返回中文可读消息

#### 录入 key 的三种方式

1. **REPL `/login`**：交互选 `1` Claude → 输 key → 自动做格式+连通性校验 → 成功则热切换 + 持久化到 `~/.config/kedo/config.yaml`
2. **环境变量**：`export ANTHROPIC_API_KEY=sk-ant-...` + `export KEDO_PROVIDER=anthropic`
3. **HTTP API** (运行时热切换)：
   ```bash
   # 只校验不切换
   curl -XPOST http://host:8000/api/llm/validate \
     -H 'content-type: application/json' \
     -d '{"provider":"claude","api_key":"sk-ant-..."}'

   # 切换 + 默认持久化（传 "persist": false 可以只内存切换不落盘）
   curl -XPOST http://host:8000/api/llm/switch \
     -H 'content-type: application/json' \
     -d '{"provider":"claude","api_key":"sk-ant-...","model":"claude-sonnet-4-6"}'
   ```

#### 配置持久化

`/llm/switch` 成功后会把 `llm_provider / model / anthropic_api_key / kimi_*` **合并**写入 `~/.config/kedo/config.yaml`（权限 0600，保留文件里其他键）。下次 `kedo <project>` 启动时直接复用，不会回退到旧 provider。

### 问答/闲聊快速通道

输入被识别为闲聊或元信息问询（如"你是什么模型？"、"你能做什么？"）时，`_is_chat_query` 会让 agent loop **跳过 planner/evaluator 流水线**，直接把问题交给底层 LLM 流式回答。判定规则：

- 命中身份/模型/能力类关键词，**或** 短输入 (≤30 字) 且以问号结尾
- 同时不能出现开发动词（`实现/添加/构建/implement/...`）或 bug 关键词（`bug/崩溃/报错/...`），否则让 bug-fix 或正常流水线接管

目的是避免把一句问话当成开发需求拆成 build/test/evaluate 子任务、还被 evaluator 按 code review 打 0 分进迭代循环。
- **Ollama 本地模型**：受模型能力限制，复杂的 planner prompt 可能无法正确遵循，建议仅用于简单项目
- **对接其他 LLM 时**：重点验证 (1) JSON 结构化输出是否稳定 (2) 长 system prompt 是否被截断 (3) "输出纯代码" 指令是否被遵循

## 项目结构

```
kedo/                          18,750 行
├── kedo.py                    CLI 入口
├── cli/
│   ├── repl.py                交互式 REPL（1,146 行）
│   └── theme.py               终端主题
├── core/
│   ├── agent_loop.py          Agent 主循环 + 自动修复 + 智能续接（3,054 行）
│   ├── planner.py             任务规划器（848 行）
│   ├── evaluator.py           多维度质量评估（508 行）
│   ├── project_profile.py     项目 Profile 管理（642 行）
│   ├── platform_knowledge.py  平台知识 + CMakeLists 模板（Switch 等）
│   ├── state_manager.py       状态持久化（378 行）
│   ├── version_manager.py     候选版本管理（344 行）
│   └── memory.py              上下文记忆（321 行）
├── api/
│   ├── server.py              FastAPI 应用
│   ├── routes.py              REST API + 产品需求总结（2,072 行）
│   ├── schemas.py             数据模型（364 行）
│   └── websocket.py           WebSocket 推送
├── tools/
│   ├── code_generator.py      代码生成 + 校验（486 行）
│   ├── file_tool.py           文件操作
│   ├── shell_executor.py      Shell 执行（沙箱模式）
│   └── test_runner.py         测试运行
└── dashboard/
    └── index.html             Web Dashboard（4,776 行）
```

## 已知局限与改进历程

kedo 在交叉编译平台上曾暴露 6 个核心能力差距，经三轮改进已全部修复：

| 编号 | 问题 | 修复方案 | 状态 |
|------|------|----------|------|
| G1 | LLM 幻觉不存在的库名 | `scan_platform_hints()` 扫描真实文件系统注入 prompt | 已修复 |
| G2 | 不会迭代调试 build 错误 | 结构化 build error 解析 + 增量修复循环 | 已修复 |
| G3 | auto_fix 可能越修越坏 | Profile 变更白名单（只允许改 build/notes） | 已修复 |
| G4 | 不了解目标平台构建规范 | `platform_knowledge.py` 平台开发规范注入 | 已修复 |
| G5 | CMakeLists 生成质量差 | 按项目类型提供已验证的 CMakeLists 模板 | 已修复 |
| G6 | 生成非代码文件 | 二进制文件走 ImageMagick/ffmpeg 生成 | 已修复 |

### 待办

- **auto_fix 历次失败回溯**：auto_fix prompt 注入 prior_attempts，让 LLM 看到历次修改避免重复犯错（已实现，待实战验证）
- **escalation 信息密度**：Dashboard 暂停 banner 中展示更完整的上下文（auto_fix 历次 diff + stderr 全文）

## 许可证

MIT License
