# 架构

kedo 是 **单 Agent 架构**：唯一的 `ReactAgent` 通过 15 个工具完成所有任务，没有 if/else 特判的流水线。

## ReactAgent：LLM 驱动的 ReAct 循环

```
用户输入 → Agent 思考(LLM) → 选择工具 → 执行 → 观察结果 → 再思考 → ... → 回复用户
```

每一步"做什么"都由 LLM 自主决定，不是预先规划的刚性流程。

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

## 项目结构

```
kedo/
├── kedo.py                    CLI 入口
├── cli/
│   ├── repl.py                交互式 REPL + DECSTBM 底栏 + /verbose 切换
│   └── theme.py               终端主题
├── core/
│   ├── react_agent.py         LLM 驱动的 ReAct 主循环 + 文本模式解析器
│   │                          + 收敛检测 + 任务链继承 + resume_from_checkpoint
│   ├── agent_loop.py          22 行 deprecated shim（DeprecationWarning + re-export）
│   ├── _legacy/
│   │   └── agent_loop.py      旧 Agent 流水线（4-6 周后真删）
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
