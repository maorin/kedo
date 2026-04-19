# kedo — AI 全流程自动化开发工具

从需求到部署的全流程自动化开发工具。输入自然语言需求，kedo 自动完成需求分析、架构设计、代码生成、构建、测试、评估，直到部署上线。

## 核心特性

- **ReAct Agent 架构**：LLM 驱动的 Think→Act→Observe 循环，自主决策每一步做什么。简单问题直接回答，复杂任务自动规划→编码→编译→测试
- **Function Calling + 文本 ReAct 双模式**：原生支持 OpenAI/Anthropic function calling；对不支持 function calling 的端点（如 Kimi Code）自动切换到文本 ReAct 模式
- **工具驱动**：Agent 通过工具操作项目——读写文件、执行 Shell、编译构建、搜索代码、Git 操作，所有动作可追踪
- **智能续接**：输入"继续"自动识别上次进度，扫描项目现状，只补缺失功能
- **LLM 驱动自动修复**：编译/测试失败时 AI 分析 stderr 并修复代码，支持多轮重试和错误恢复
- **平台感知代码生成**：自动扫描目标平台库/头文件，注入平台知识和 CMakeLists 模板
- **项目 Profile 系统**：LLM 自动生成项目构建档案（build/test/deploy 命令），支持跨 session 缓存、失败历史跟踪
- **多 LLM 支持**：Kimi K2.5（推荐）、Claude、OpenAI、Ollama，运行时 `/login` 热切换
- **API Key 安全**：密钥存储在 `~/.config/kedo/config.yaml`（权限 0600），不进入项目代码
- **Web Dashboard**：工作台 + 文件浏览 + 代码预览 + 部署引导 + 实时事件流

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

## 架构：ReactAgent

kedo 的核心是 **ReactAgent** — 一个 LLM 驱动的 ReAct（Reasoning + Acting）循环：

```
用户输入 → Agent 思考(LLM) → 选择工具 → 执行 → 观察结果 → 再思考 → ... → 回复用户
```

### Agent 可用工具（15 个）

| 工具 | 说明 |
|------|------|
| `file_read` | 读取项目文件 |
| `file_write` | 创建/修改文件（受 ProfileGuard 拦截） |
| `file_search` | 搜索文件 |
| `shell_execute` | 执行 Shell 命令（沙箱：拦提权 + DEVNULL stdin） |
| `build` | 编译项目（自动探测构建系统） |
| `code_generate` | LLM 驱动的代码生成（受 ProfileGuard 拦截） |
| `test_run` | 运行测试 |
| `git` | Git 操作 |
| `plan_development` | LLM 调 Planner 拆解需求 → 写 plan checkpoint |
| `auto_fix` | LLM-driven 单轮修复：诊断 stderr → patch → 写文件 |
| `evaluate` | 4 维度代码质量打分（需求/质量/测试/安全） |
| `commit_candidate` | 固化候选版本 + Git tag + dashboard 候选 panel |
| `propose_alternatives` | 结构化"换思路 vs 你拍板"，阻塞等 dashboard 选择 |
| `pause_for_human` | LLM 自评搞不定 → 暂停 + dashboard banner 等用户建议 |
| `respond` | 向用户发送最终回复（必须用以正式收尾） |

### 双模式 LLM 调用

- **Native Function Calling**：OpenAI / Claude 等支持 `tools` 参数的 API
- **文本 ReAct Fallback**：不支持 function calling 的端点（如 Kimi Code 403），自动在 prompt 中注入工具描述，LLM 用 ` ```tool_call` ` 块输出调用，Agent 解析执行

### 配置分层

优先级：环境变量 > 用户配置 (`~/.config/kedo/config.yaml`) > 项目配置 (`config.yaml`)

API Key 通过 `/login` 命令设置，存储在用户目录（权限 0600），不进入项目代码。

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

## 自动修复系统（auto_fix 工具）

P3 后 auto_fix 是 ReactAgent 可调的工具（`tools/auto_fix_tool.py`），不再是 AgentLoop 的内嵌循环。LLM 模式：build fail → call `auto_fix(failed_step, error_text)` → 收 diagnosis + patched file → 再 build → ...

三层防御：

| 层级 | 机制 | 说明 |
|------|------|------|
| Prompt 引导 | `human_verified` profile 在修复上下文中标 `[READ-ONLY]` | 引导 LLM 不碰已验证配置 |
| 写入拦截 | `ProfileGuard.check()` 写文件前 hook | 拦掉 human_verified profile 覆盖 + Makefile/CMake 关键 target 丢失 |
| 收敛检测 | 同 tool + 相似 fingerprint 连续 3 次 | 自动 emit paused_for_human + state.pause_task → 升级到人工 |

auto_fix 工具流程（每次调用做一轮）：
1. 收集相关项目文件（Makefile / CMakeLists / 目标源文件 / .kedo/project_profile.json）
2. 注入 prior_attempts 作为 negative examples，告诉 LLM 哪些修法已经试过且无效
3. LLM 输出 JSON 补丁（`{file_to_fix, action, new_content, diagnosis}` 或 `{unfixable, reason}`）
4. ProfileGuard 检查 → 通过则写文件，不通过返错给 LLM 让它换思路
5. 写入 prior_attempts（patched_file + diagnosis），让下次 auto_fix 看到

ReactAgent 决定循环：根据 auto_fix 返回的 success/diagnosis，LLM 自主决定再 build 一次还是放弃用 `pause_for_human`/`propose_alternatives`。

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
| `/verbose [on\|off\|toggle]` | 控制台详细模式开关（默认 on，显示完整 args/output/summary 不截断） |
| `/quit` | 退出 |

### 控制台底栏（固定状态栏）

REPL 启动时用 ANSI DECSTBM 把终端切成两个区域：

- **滚动区**（第 1 行 ~ H-1 行）：事件流持续滚动（LLM 请求/响应、工具执行、step 完成/失败）
- **固定底栏**（第 H 行）：永远钉在屏幕最底部，显示 `provider/model │ Task:xxx │ 状态 │ 当前 step │ 进度条`

事件流不会再穿插状态栏，按 `Ctrl+L` 或事件刷屏都不会冲掉底栏。窗口 resize（SIGWINCH）会自动重设滚动区域。退出时 `\033[r` 恢复整屏滚动，shell prompt 不会被污染。

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
kedo/
├── kedo.py                    CLI 入口
├── cli/
│   ├── repl.py                交互式 REPL + DECSTBM 底栏 + /verbose 切换（1,308 行）
│   └── theme.py               终端主题
├── core/
│   ├── react_agent.py         LLM 驱动的 ReAct 主循环 + 文本模式解析器
│   │                          + 收敛检测 + 任务链继承 + resume_from_checkpoint
│   ├── agent_loop.py          22 行 deprecated shim（DeprecationWarning + re-export）
│   ├── _legacy/
│   │   └── agent_loop.py      旧 Agent 流水线 3406 行（4-6 周后真删）
│   ├── planner.py             任务规划器
│   ├── evaluator.py           多维度质量评估（被 EvaluateTool 包装）
│   ├── project_profile.py     项目 Profile 管理 + ProfileGuard 写拦截
│   ├── platform_knowledge.py  平台知识 + CMakeLists 模板（Switch 等）
│   ├── state_manager.py       状态持久化
│   ├── version_manager.py     候选版本管理（被 CommitCandidateTool 包装）
│   └── memory.py              上下文记忆
├── api/
│   ├── server.py              FastAPI + LLM 客户端 + 工具注册 + dashboard 缓存
│   ├── routes.py              REST API + set_dependencies 接收 react_agent /
│   │                          version_manager / planner / evaluator 单例
│   ├── schemas.py             数据模型（AgentCheckpoint 含 messages 历史）
│   └── websocket.py           WebSocket 推送
├── tools/                     ReactAgent 的 15 个工具
│   ├── code_generator.py      code_generate（+ on_token 回调流式 + ProfileGuard 拦截）
│   ├── shell_executor.py      shell_execute（沙箱：拦提权 + DEVNULL stdin）
│   ├── file_tool.py           file_read/write/search（+ ProfileGuard 拦 write）
│   ├── build_tool.py          build（封装 ProjectProfileManager）
│   ├── test_runner.py         test_run
│   ├── git_tool.py            git
│   ├── respond_tool.py        respond（最终回复）
│   ├── plan_tool.py           plan_development（拆解 + 写 checkpoint）
│   ├── auto_fix_tool.py       auto_fix（LLM 单轮诊断 + patch + 受 ProfileGuard 保护）
│   ├── pause_tool.py          pause_for_human（开放式 escalate）
│   ├── propose_alternatives_tool.py  propose_alternatives（结构化"换思路"）
│   ├── evaluate_tool.py       evaluate（4 维度打分）
│   ├── commit_candidate_tool.py      commit_candidate（固化候选）
│   └── profile_guard.py       ProfileGuard 写拦截器（共享给 file/codegen/auto_fix）
└── dashboard/
    └── index.html             Web Dashboard（含 cache-busting middleware）
```

## 安全护栏（Hardening）

### Shell 工具沙箱
`shell_execute` 工具默认三层隔离，防止 LLM 命令劫持开发者的终端：

- **`stdin=DEVNULL`**：subprocess 不继承 kedo 的 tty，任何 `read 0` 操作（sudo/passwd/ssh 密码提示、git 凭证提示）立即 EOF 失败
- **提权命令拦截**：黑名单 `sudo / su / pkexec / doas`，按 token 切分，命令任意位置（行首、`&&` 后、`;` 后、管道后）都拦
- **askpass 通道屏蔽**：注入 `SUDO_ASKPASS=/bin/false`、`SSH_ASKPASS=/bin/false`、`DISPLAY=""`、`GIT_TERMINAL_PROMPT=0`、`DEBIAN_FRONTEND=noninteractive`，断绝 sudo/ssh/apt 通过外部程序绕过 tty 弹窗

LLM 想跑提权命令时直接拿到 `Command requires privilege escalation (sudo); refused...` 错误，可换思路继续推进；不会再发生 kedo 控制台弹"[sudo] password:"的情况。

### Dashboard 缓存控制
FastAPI 中间件给 `/` 和 `/dashboard/*` 所有响应注入 `Cache-Control: no-store, no-cache, must-revalidate, max-age=0`。改了 HTML/JS 后**普通刷新（F5 / Cmd+R）**就拿最新版，不再需要强制刷新（Cmd+Shift+R）。

### ReAct 文本模式解析器健壮性
对接 Kimi Code（不支持 function calling）等端点时走文本 ReAct 模式，解析器兼容三类常见异常：

| 异常 | 兼容方式 |
|---|---|
| LLM 漏写闭合 ` ``` `（输出被截断或模型遗漏） | 正则 `(?:\n```\|\Z)` 兜底到文末 |
| 同一 ` ```tool_call ` 块塞多个 JSON 对象（违反 one-tool-per-block 约定） | 按花括号平衡扫描 block 内所有顶层 `{...}` |
| 模型返回纯 reasoning_content 没 content（Kimi reasoning-only quirk） | KimiClient stream/non-stream 都 fallback 到 reasoning_content；空响应触发重试，连续 3 次空才标 failed |

### 工具参数注入
ReactAgent 自动注入的运行时参数（`task_id`、`project_path`）**强制覆盖** LLM 自己传的值。原因：LLM 看到 schema 里有这些参数会自己编一个看似合理的值（如 `task_id="switch-nfs-player"`），导致 PlanTool 把 plan 写到错误的 checkpoint，dashboard 永远看不到子任务列表。

### 收敛检测（防 LLM 死循环）
ReactAgent 每次工具失败记录 `(tool_name, error_fingerprint)`；最近窗口（默认 4 次）内同 tool + 相似指纹（85% SequenceMatcher 阈值）≥3 次 → 自动 emit `paused_for_human` + 调 `state.pause_task` + 标 failed。fingerprint 归一化 ANSI / 行号 / 绝对路径 / 内存地址 / tmp 路径，跨多次构建仍能识别"同一个错"。

### 任务链上下文继承
新 task 启动时 ReactAgent 检查同项目最近 failed/paused 的 task，把它的 plan 子任务标题链 + 卡点 + 描述摘要注入新 task 的 system prompt，避免 LLM 从零探索同一片雷区（你提交"修复这个问题再编译"时拿到的不是空白上下文）。

### 强制 respond 收尾
LLM 输出非空 prose 但没调任何工具时（典型 Kimi prose 结尾 quirk："编译成功🎉做了 5 件事..."），ReactAgent 第一次回灌 user "请用 respond 工具明确收尾或继续调工具" 让 LLM 重出一轮；第二次出现才接受为 final answer。避免 reasoning_content 截断的半成品被误判为任务完成。

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

### P3 单 Agent 架构迁移（已完成）

旧版本 kedo 同时运行 `AgentLoop`（3406 行刚性流水线）和 `ReactAgent`（LLM 驱动 ReAct 循环）双轨：reasoning_content fallback 救了 ReactAgent 却毒到 AgentLoop 的 JSON parser；`_on_step_unrecoverable(error_text=...)` typo 只在 AgentLoop 侧。

P3 三个里程碑统一到 ReactAgent 单轨：
- **M1**：ReactAgent 加固（auto_fix / profile_guard / 收敛检测 / 任务链上下文继承 / pause_for_human 工具化）
- **M2**：resume_from_checkpoint + evaluate / commit_candidate / propose_alternatives 工具化 + Kimi prose 收尾 retry
- **M3**：AgentLoop 移到 `core/_legacy/` + 22 行 deprecated shim；server.py 不再实例化；routes 通过单独注入的 `version_manager` / `planner` / `evaluator` 访问，不再走 `_agent_loop.X`

后续 LLM quirk 或工具改进只需在 ReactAgent 一处装。AgentLoop shim 计划 4-6 周后实战确认无回归再从 `_legacy/` 真删。

### 待办

- **任务链上下文按 project_path 严格隔离**：当前 `_gather_prior_task_context` 取最近的 failed task，未按项目路径过滤（未来 state_manager 加 project_path 字段后再收紧）
- **escalation 信息密度**：Dashboard 暂停 banner 中展示更完整的上下文（auto_fix 历次 diff + stderr 全文）
- **真删 `core/_legacy/agent_loop.py`**：4-6 周实战无回归后

## 许可证

MIT License
