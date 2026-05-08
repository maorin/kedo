# ReactAgent vs Claude Code Harness — 两种 Agent Runtime 对比

> **状态**：草稿，2026-05-08 立项。一篇横向对照文章，不是 spec。
> **关联文档**：
> - `multi-agent-architecture.md` — 多 Agent 拓扑选型，本文论述的演化方向之一
> - `agent-workflow-hybrid.md` — 自主 Agent ↔ Workflow 的混合，本文引用其分类法
> - `kedo-as-skill-and-skill-host.md` — kedo 暴露给 harness 调用的探讨稿，本文末尾的演化方向

## TL;DR

两者都是 "agent runtime"，但解决的是不同问题：

| | 定位 | 用户接触面 | 主要负载 |
|---|---|---|---|
| **kedo ReactAgent** | 嵌入式开发**自主流水线**，分钟到小时级长任务 | dashboard (HTTP) + REPL | "把需求做成 .nro / commit candidate" |
| **Claude Code harness** | 通用**交互式编程伙伴**，对话式 | 终端 / IDE 插件 / Web | "理解我说的，帮我改这块代码" |

**ReactAgent 像外包专精车间**：你给需求单，等成品；**harness 像贴身助理**：边走边对话，每步要你点头。

两者长期会互相借鉴。kedo `multi-agent-architecture.md` 列的 "Sub-agent as Tool" 拓扑，本质上就是 harness 的 `Agent` 工具范式；harness 的 skill 系统则是 kedo 想做但还没做的扩展机制。

---

## 1. 9 维总览

| 维度 | ReactAgent (kedo) | Claude Code harness |
|---|---|---|
| **核心循环** | LLM-driven ReAct (Think → Act → Observe)，单 LLM 多 turn 自跑 | 对话式 turn-by-turn，每 turn 一个 LLM 响应 |
| **工具来源** | 启动时静态注册（`ToolRegistry`），**26** 个 tool 全部在 `tools/*.py` | 内置 + Skill (文件系统) + MCP server + sub-agent；动态扩展 |
| **状态持久化** | `.kedo/state/<task_id>` checkpoint 跨 session 续跑（含 messages / plan / charter） | conversation transcript + memory files (`CLAUDE.md`、`~/.claude/.../memory/`) |
| **权限** | `PermissionManager` + Tier 0-3 + dashboard 弹窗 + `profile_guard` | 三档 mode (default / plan / yolo) + 工具级 allow/deny + hooks (PreToolUse 等) |
| **失败处理** | `pause_for_human` / `propose_alternatives` / `propose_charter_change` 工具化 escalate | 直接说"卡住了，怎么办"等用户回 |
| **多 Agent** | Reviewer 二审（可选开启，方案 C / Actor-Critic）+ 未来 sub-agent | `Agent` 工具内嵌子 agent + 子 agent 类型 (general / Explore / Plan / 自定义) |
| **跨平台** | Web Dashboard + REPL，依赖后端服务 (FastAPI) | CLI / IDE 插件 / Desktop / Web，无后端，二进制即跑 |
| **域适配** | 单一域：Switch homebrew / 嵌入式（charter 强约束） | 全语言全栈，无 charter 概念 |
| **失控防护** | `profile_guard` (charter frozen 拒写) + `reject_tracker` 升级 + `role_swap` | hooks PreToolUse 拦截 + plan mode + sandbox |

---

## 2. 关键差异展开

### 2.1 自主性程度根本不同

**Harness 设计假设**：每 turn 用户都在场。LLM 跑工具调用 → 出结果 → 等下一句话。哪怕开 plan mode 或长时间多 turn，本质上是**等用户继续输入**。

**ReactAgent 设计假设**：用户给完需求就走。LLM 在 ReAct 循环里自己决策、自己出错自己 retry / pause / propose alternatives，分钟到小时级**自跑**。要么完成、要么 escalate，**不会**"等下一句话"。

→ 这驱动完全不同的失败处理：

- harness 不需要 `pause_for_human` 工具（用户本来就在），LLM 直接说"卡住了"就行
- ReactAgent 必须有 `pause_for_human` / `propose_alternatives` / `propose_charter_change` 这一组**显式 escalate** 工具，告诉系统"我搞不定，叫人 + 这个 task 进 paused 状态等用户回 dashboard"

→ 也驱动了完全不同的 prompt 风格：

- harness LLM 答用户问题就行
- ReactAgent LLM 必须每步**说服自己**继续：reasoning + tool_call + observation 三段式，里面有大量"自我审视"

### 2.2 工具系统的开放性反过来

**Harness 工具**：内置（Bash/Read/Edit/Write/Grep/Glob/WebFetch/WebSearch/Agent）+ Skill (文件系统插件) + MCP (RPC server) + sub-agent，**任意添加，运行时动态加载**。skill 写个 markdown + 几条 instruction 就能扩展能力。

**ReactAgent 工具**：启动时**静态注册**到 `ToolRegistry`，**26 个全部 hardcoded** 在 `tools/*.py`。要新增能力得：

1. 写一个继承 `BaseTool` 的子类
2. 在 `api/server.py:create_app` 里 `tool_registry.register(MyTool(...))`
3. 重启服务

→ 反差很大。这是 kedo 在 `kedo-as-skill-and-skill-host.md` 探讨稿里 B 想法（kedo 内部消费第三方 skill 包）的动机。

→ 但 kedo 的 charter 强约束让"任意外部工具"风险大于收益 —— 任意 skill 可能违反 charter（比如改 frozen profile）。要做必须有签名 / 沙箱 / 工具调用前 charter 校验机制。**先做不做得了暂时不知道**，路线图里排在 Browser Bridge M3/M4 之后。

### 2.3 上下文管理思路相反

**Harness**：接近 "**信任 LLM + 用户主动管理**"。超长会话靠 LLM 自动压缩 + 用户主动 `/clear`；memory 是用户**显式记录**的小条目（这条 conversation 就在按这个范式跑）。

**ReactAgent**：是 "**系统主动管理**"。

- `core/memory.py` 维护 `max_context_chars=120_000` 阈值
- `react_agent.py` 有收敛检测（同 tool + 相似 fingerprint 连续 3 次自动 pause）
- `checkpoint.messages` 用于跨 session 还原对话历史
- Long-horizon memory 是已知 gap（见 `long-horizon-memory.md`）

→ 这是因为 ReactAgent 跑长任务，**没人在边上 `/clear`**，靠系统自治才能不爆 context。但当前自治还不够（30+ turn 后早期 charter / 决策被稀释），是 roadmap 里"长 horizon 摘要器"的事。

### 2.4 权限模型 surface 不一样

**Harness 权限**核心矛盾：**用户在场但不想被打断**。

- plan mode（只读思考）
- yolo mode（一次性放行所有）
- hooks（机器拦截）
- settings.json（持久 allow list）

→ 都是**减少用户被打扰频率**的手段。

**ReactAgent 权限**核心矛盾：**用户不在场但又得防 agent 干蠢事**。

- `PermissionManager` Tier 0-3 自动分级
- dashboard 弹窗（async 通知）
- `profile_guard`（charter frozen 拒改 Makefile / CMakeLists 关键字段）
- `reject_tracker`（commit_candidate 多次被 Reviewer 拒后强制 escalate）

→ 都是**自动决策 + 不能时叫人**的手段。

→ Browser Bridge 把这点暴露得最清楚：场景 A（agent 操作用户主浏览器）默认 T2 强制确认；场景 C（agent 查资料）独立 profile 不污染用户登录态。harness 不会有这种区分，因为 harness 不会代用户操作真实账号。

### 2.5 持久化形态根本不同

**Harness persistence**：conversation transcript（每条消息都在）+ memory files（手写的 markdown 条目）+ CLAUDE.md（项目说明）。重启后**重读 transcript** 进入上下文。

**ReactAgent persistence**：

- `StateManager` 的 `<task_id>_checkpoint.json`（含 plan / messages / charter / state）
- 类数据库的 `task_index.json`
- 候选版本 git tag / branch
- Charter 文件 + version_manager

→ 重启后能**任选一个 task 续跑**或回到任意候选版本。

→ 一句话：**harness 是会话历史恢复，ReactAgent 是任务状态机恢复**。前者线性、后者多 task 并存。

### 2.6 多 Agent 拓扑差距

**Harness 的 sub-agent**：`Agent` 工具是"派一个子 agent 干个事"，主要用来**保护主上下文**（搜索、研究类任务）。子 agent 类型有 general / Explore / Plan / code-reviewer / 自定义。轻量、isolation 强（默认 worktree 选项）。

**ReactAgent 当前的多 agent**：是 "**Producer + 独立 Reviewer**"（方案 C，配置开关）—— Reviewer 二审拒绝 `commit_candidate` 时升级、可选 swap 角色再试一次。`multi-agent-architecture.md` 里讨论了 4 种拓扑（Sub-agent as Tool / Orchestrator-Worker / Actor-Critic / Blackboard），ReactAgent 走的是 **Actor-Critic 简化版**。

→ harness 的 sub-agent 模式（Sub-agent as Tool）是 kedo 多 agent 路线图里的**下一阶段候选**（roadmap.md "多 Agent 协同" workstream），方向上是 ReactAgent 想借鉴的。

---

## 3. ReactAgent 能借鉴 harness 的

1. **Skill 风格扩展**（已在探讨稿） —— 让用户写个 markdown + 工具描述就能新增能力，而不是改 `tools/*.py`。门槛降低 + 解耦域知识。但 kedo charter 约束让安全门槛比 harness 高，落地需要签名 / 沙箱机制。
2. **Hook 机制** —— `PreToolUse` / `PostToolUse` 拦截能让 charter 治理更优雅。现在 `profile_guard` 是工具内部硬编码，加 hook 就能解耦。
3. **`Agent` 工具范式** —— 把多 agent 协同从 hardcoded "Reviewer + Producer" 抽象成"派子 agent 干这事"，更通用。和 `multi-agent-architecture.md` Sub-agent as Tool 路径一致。
4. **Plan Mode 明确语义** —— ReactAgent 现在没有"只读规划"模式，LLM 想到什么直接干；如果有 plan → approve → exec 流程，charter drift 风险更可控。和 `agent-workflow-hybrid.md` 推的 ③ Plan-as-Contract 演进方向一致。

## 4. harness 不能解决的、kedo 必须自己做的

1. **领域 charter 治理** —— harness 没有"项目不变约束"概念，它信任用户实时决策。kedo 跑长任务必须有 frozen charter 守底线（profile_guard / commit_candidate gate / propose_charter_change 流程），不能简单复用 harness 模式。
2. **跨 session 任务续跑** —— harness 一次 conversation 完事，重开是新 conversation。kedo 任务跨天跨周续跑，得有 task_id 状态机 + checkpoint.messages 还原。
3. **Self-eval 物理对抗** —— harness 默认信 LLM 自评（用户能看到所以 OK）；kedo 用户不在场，必须 Reviewer 二审破 confirmation bias（详见 `self-evaluation.md`）。
4. **建-测-评-上线全流程编排** —— harness 是单 turn 工具调用粒度，kedo 是 build → host_test → emulator_test → coredump → reviewer → commit_candidate 这种 pipeline 状态机。React 循环里这条状态机靠 plan_tool + evaluate_tool + commit_candidate_tool + auto_fix 串起来，是领域知识。

## 5. 演化方向

两者长期不会合并，但会**互相靠拢**到某个范式：

```
harness                                        ReactAgent
(通用 + 交互)                                  (专精 + 自主)
    │                                              │
    │  借鉴 charter / 自评对抗                       │  借鉴 skill / hook / sub-agent
    ▼                                              ▼
   harness 跑长任务时也想要"可验证 plan"            ReactAgent 想要"用户写个 skill 就能扩展"
                       │                                    │
                       └────── 收敛点 ──────────────────────┘
                  Hierarchical Agent + Plan-as-Contract
                  (multi-agent-architecture.md Phase 3-4)
```

→ kedo 路线图里 "**Skill 暴露**" workstream（`kedo-as-skill-and-skill-host.md`）的最终形态：

> **kedo 整体打包成一个 harness 可调的"嵌入式开发 skill"** —— 用户在 Claude Code 终端说"帮我把这个 Switch homebrew 的 audio 修一下"，harness 把任务通过 skill 协议转给 kedo，kedo 跑完返回 commit_id 和 build artifact，harness 接着用 Edit/Read 工具帮用户做后续修改。

这是 ReactAgent 域知识 + harness 用户体验的最佳组合。

---

## 6. 一句话总结

**harness 是通用 agent runtime，ReactAgent 是垂直域 agent runtime**。

- 前者把**灵活性**给 ecosystem（skill / MCP / hook），靠用户在场补决策
- 后者把**约束**给 charter / state / reviewer，靠系统自治保正确

两条路线都对，看你的 agent 是要**辅助用户思考**还是**替代用户执行**。kedo 是后者，所以现在长这样。

---

## 不在本文范围

- **AutoGPT / BabyAGI 等其他 agent runtime 对比** —— 它们和 ReactAgent 同向但更早期，charter 治理 / Reviewer / 持久化都做得不如 kedo，比较意义不大
- **Cursor / Cline / Aider 等 IDE-AI 对比** —— 它们和 harness 同向，工具系统更受限（依赖 IDE API），不如 harness 通用
- **MCP 协议本身** —— harness 通过 MCP 接外部 tool 是另一个话题，本文只对比 runtime 形态
