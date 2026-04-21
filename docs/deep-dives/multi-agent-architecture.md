# 从单 Agent 到多 Agent：kedo 的架构演进设计

> 这一篇和前 6 篇 deep-dive 性质不同：前面是"现状诊断"，这一篇是"设计未来"。

## 问题：为什么要考虑多 Agent

看完前 6 篇核心问题答疑会发现一个模式——**单 Agent 架构的结构性痛点**：

| 问题 | 单 Agent 的结构性根因 |
|---|---|
| [Context Anxiety](context-management.md) | 一个 context window 塞下 plan + 历史工具调用 + 代码片段 + 错误累积 |
| [Self-evaluation Drift](self-evaluation.md) | 同一个 LLM 写代码又打分，confirmation bias 无解 |
| [Planning Instability](planning-instability.md) | 同一 LLM 拆 plan 又执行，execution drift 无人监督 |
| [Long-horizon Memory Loss](long-horizon-memory.md) | 单一 attention 权重分布被工具 result 稀释 |
| [Hallucinated Execution](hallucinated-execution.md) | 短 ReAct 反馈对单轮幻觉有效，但跨阶段 "整体汇报失真" 仍在 |

**这些不是可以在单 Agent 内修完的工程问题**。kedo 通过收敛检测 / ProfileGuard / 客观回路把 80% 压住了，剩下 20% 是**单 Agent 的天花板**——同一 LLM 承担 planner + executor + reviewer 三个角色的 role conflict。

多 Agent 的核心价值：**把 role conflict 物理拆开**。planner 拆完 plan 就别再"看代码调工具"；reviewer 不参与 code_generate 就没有"护我自己生的蛋" 的 bias。

## 单 Agent 现状分析

当前 `ReactAgent` 是一个"全能 worker"：

```
┌──────────────────────────────────────────────────────────┐
│                    ReactAgent (单 Agent)                   │
│                                                          │
│    user request → messages → LLM → tool_call → result    │
│                       ↑                           │      │
│                       └───── loop ──────────────┘      │
│                                                          │
│  Tools (15):                                             │
│    file_* / shell_execute / build / code_generate /      │
│    test_run / git / plan_development / auto_fix /        │
│    evaluate / commit_candidate / propose_alternatives /  │
│    pause_for_human / respond                             │
└──────────────────────────────────────────────────────────┘
```

**一切都在一个 LLM 的 context 里跑**：

- 一条 messages 列表装 plan + 所有工具调用历史 + 所有工具返回 + 所有 reasoning
- 一个 system prompt 同时是"你是规划者"、"你是程序员"、"你是代码审查者"
- 一套 evaluator 工具调同一个 LLM 给自己的代码打分

### 做得对的地方（为什么当前能 work）

- **Tool-level role hint**：每个工具 system prompt 是独立的（Planner prompt / Evaluator prompt / Code Generator prompt 各不同），在单一 messages 之外叠加"角色视角切换"
- **客观回路**：build/test exit_code 作为 ground truth
- **强制回验**：auto_fix 后 re-build、prose 收尾 retry、收敛检测

### 做不到的事（天花板）

- **并行**：工具调用全串行，LLM 思考也串行
- **专业化**：code_generate 想用 Claude（强代码）但 evaluate 想用 GPT-4（第三方 judge）—— 不支持
- **独立审查**：evaluator 本质还是同 LLM + 换 prompt，不是真独立 reviewer
- **上下文隔离**：planner 也能看到所有工具调用历史（其实不需要），context 被迅速污染
- **专长路由**：简单 bug fix 调用 15 工具能力过剩，复杂多文件重构 15 工具又不够结构

## 架构模式设计方案对比

多 Agent 不是单一架构而是一个光谱。本节把可选模式并列比较，下一节（"方案 B 详细设计"）是其中一个方案的具体展开。

### 光谱：5 种典型模式

刚性程度 2 > 3 > 4 > 5，Agent 自主性 2 < 3 < 4 < 5。

| # | 模式 | 结构 | 代表 |
|---|---|---|---|
| 1 | 单 Agent + 多工具 | 一个 LLM loop，工具有独立 prompt | kedo 现状、Cursor agent |
| 2 | Pipeline（刚性流水线） | Planner → Coder → Tester → Reviewer 顺序交接 | kedo 旧 AgentLoop、传统 RAG pipeline |
| 3 | Sub-agent as Tool | 主 Agent 把 worker agent 当工具调 | Claude Code、OpenAI Assistants |
| 4 | Orchestrator-Worker（层级） | "领导"Agent 分派给专家 Agent，独立 context | Anthropic multi-agent research |
| 5 | Peer / Swarm（对等协作） | 多 Agent 平等通过共享通道/群聊协商 | AutoGen group chat、CAMEL |

kedo 现状是 1，旧 AgentLoop 是 2（已弃用），本文档默认演进方向是 4。

### 4 个候选方案

#### 方案 A — Sub-agent as Tool（最轻量）

主 ReactAgent 不变，把 Reviewer / Planner 包装成**特殊工具**。调用时工具内部起独立 LLM client + 独立 messages，返回结果后并入主 context 作为 tool_result。

- **本质**：调用栈上的 agent，没有真正的"同时存在"
- **工程量**：~1-2 周
- **像什么**：Claude Code 的 Task 工具、OpenAI Assistants 的 sub-assistant
- **破的 bias**：Self-eval drift（Reviewer 独立 provider 即可）+ Planner context 污染

#### 方案 B — Orchestrator-Worker（本文档原版设计）

顶层 Orchestrator 决定路由、分派 subtask 给 Worker/Planner/Reviewer，各 Agent 有独立 messages、独立 LLM、独立 checkpoint。详见下一节。

- **本质**：真正的并发 + 角色隔离 + 独立失败域
- **工程量**：~4-6 周（含消息协议、checkpoint 拆分、dashboard 多 agent 可视化）

#### 方案 C — 双 Agent 对抗（Actor-Critic）

只分两个 Agent：生产者（现 ReactAgent）+ 审查者。每次 build/test 成功后，Reviewer 强制过一道（独立 LLM、独立 context）。不引入 Orchestrator、不拆 Planner。

- **本质**：Phase 1 Reviewer 跨 provider 的"硬化版"——从工具升级为独立 Agent，有自己的判决 loop
- **工程量**：~1 周
- **破的 bias**：Self-eval drift（足以）

#### 方案 D — Blackboard + 任务队列

不画 Agent 角色，只定义任务类型 → LLM 池。文件系统 + `.kedo/state/` 当 blackboard，任一 Agent pull 任务、写结果。

- **本质**：最解耦、最像微服务
- **工程量**：~3-4 周
- **代价**：失去 ReAct 的"LLM 自主决定下一步"的灵活性；需要任务调度器

### 优缺点矩阵

#### 优点（按"真实度"排序）

| 收益 | 哪些方案提供 | 真实度 |
|---|---|---|
| Self-eval drift 破解（唯一物理可解） | B、C | ★★★★★ 实打实，单 Agent 内做不到 |
| context 隔离（planner 不被工具历史污染） | B、D | ★★★★ 显著；单 Agent 内靠剪裁只能缓解一半 |
| 专业化 LLM 选配（code/judge 不同 provider） | A、B、C | ★★★★ 纯配置收益 |
| 并行执行 subtask | B、D | ★★★ kedo 实际可并行的 subtask 稀少（code→build→test 强依赖），价值常被夸大 |
| 失败域隔离（worker 挂了 orchestrator 恢复） | B、D | ★★★ 有价值，单 Agent checkpoint 已覆盖 70% |
| 独立压测新 LLM | A、B、C、D | ★★ 有但非核心诉求 |

#### 缺点（容易被低估的）

| 代价 | 严重度 | 说明 |
|---|---|---|
| Agent 间 state divergence | ★★★★★ | "我以为 main.c 长这样"——filesystem blackboard 也救不了，Agent messages 缓存里还有上次读的旧内容。单 Agent 内天然无此问题 |
| Convergence detection 变难 | ★★★★ | M1 的"同工具相似 fingerprint 3 次 pause"是在单一 loop 内统计的；跨 Agent 需要重写（Orchestrator 反复分派给同一 Worker 的识别） |
| Checkpoint 语义爆炸 | ★★★★ | 谁的 checkpoint？Orchestrator 分派记录 + Worker 内部 messages + Reviewer 判决——三份状态同步是 Phase 3 最大设计负担 |
| 调试可观测性坍塌 | ★★★★ | 单 Agent 一条 journal；多 Agent 需要 trace id + 时间线合并。distributed tracing 搭起来前，bug 定位时间 ×3 |
| Token 成本 1.5-2.5× | ★★★ | Agent 间摘要结构化重复；Orchestrator 本身也消耗 LLM |
| Framework over-engineering 诱惑 | ★★★ | 一旦引入消息协议、Agent registry、dispatch，就会想把所有东西"架构化"。kedo 现有 ~400 行 ReactAgent + 15 工具的简洁是资产 |
| 用户心智负担 | ★★ | dashboard 从"一 task 一 loop"变成"一 task 多 agent 时间线" |

### 4 个反直觉点

1. **Phase 3 并行收益被夸大**
   code_gen → build → test 有强依赖，真正可并行的只有"探索多方案"（propose_alternatives）。这个场景单 Agent 串行两次也 OK。并行不应作为选方案 B 的主理由。

2. **方案 A (Sub-agent as Tool) 是 kedo 可能最合适的起点**
   - 保留 ReactAgent 作为唯一决策中心
   - Reviewer / Planner 作为工具被调——内部起独立 LLM client + messages
   - 不需要消息协议、不需要 dispatch、不需要新 checkpoint 语义
   - 仍然破了 Self-eval drift
   - 等价于 Phase 1 + Phase 2 的轻量融合，工程量远小于方案 B

3. **Orchestrator-Worker 真正价值是"可替换"不是"更聪明"**
   Worker 挂了换实现、Reviewer 换 provider、Planner 从 Opus 降到 Sonnet——这些**运维灵活性**才是方案 B 主要价值。如果 kedo 不需要这种灵活性（个人工具），B 的成本 > 收益。

4. **单 Agent 的瓶颈不是"能力"是 role conflict**
   多 Agent 价值不在"更多脑子"，在把"我写的代码我评分"这种结构性 bias 拆掉。这是方案 C 就能做到的事，不一定要上 B。

### 场景推荐

| 场景 | 推荐方案 |
|---|---|
| kedo 保持个人工具 / 小团队 | **A（Sub-agent as Tool）或 C（双 Agent）**——成本低、收益真 |
| kedo 走向多用户 / 多并发 task | B（Orchestrator-Worker）值得投入 |
| 已实证 Self-eval drift 严重但不想大改 | C（最小改动破 bias） |
| 想实验 agent-agent 协作本身 | B 或 D |
| 仍是小规模 | 不上 D（过度设计） |

### 与迁移路径的映射

| 选定方案 | 对应迁移路径 |
|---|---|
| 方案 A | Phase 1 + Phase 2 合并为一步（工具形态而非独立 Agent） |
| 方案 B | Phase 1 → Phase 2 → Phase 3 → Phase 4 完整走完 |
| 方案 C | 只做 Phase 1 的加强版（Reviewer 升格为独立 Agent 而非工具内独立 client） |
| 方案 D | 跳过 Phase 1-3 直接重构为 blackboard（不推荐作为起点） |

## 蜂群（Swarm）技术专题

光谱里的第 5 种模式（Peer/Swarm）需要单独展开——"蜂群"在多 Agent 领域是被滥用的词（从 AutoGen 群聊到 6 Agent 对话瀑布都被叫 swarm），对 kedo 场景的判断需要比其它方案更细致。

### 定义：蜂群 ≠ 多 Agent

- **层级（Orchestrator-Worker）**：有明确分派者，Worker 之间不直接通信，只和 Boss 通信（star topology）
- **蜂群（Swarm / Peer）**：**无固定领导**，Agent 之间直接通信或通过共享通道广播，协调行为**涌现**而非预设

核心区分特征：
1. peer-to-peer 通信（非 star topology）
2. 决策去中心化（谁下一个说话由机制决定，不是老板点名）
3. 行为涌现（没有全局 plan，结果由交互产生）
4. 角色作为 prompt 而非调用目标（CEO / 架构师 / 程序员是角色扮演，不是"调用 CEO Agent"）

### 4 种典型蜂群形态

#### 1. Group Chat（群聊模式）

所有 Agent 在同一消息通道里，每轮由 **speaker selection** 机制挑发言者。

- **代表**：AutoGen `GroupChat`
- **speaker selection 三种策略**：
  - **round-robin**（轮流）—— 简单但死板
  - **LLM-pick-next**（让 LLM 看历史决定下一个该谁说）—— 灵活但有 bias（LLM 倾向挑话多的）
  - **manual**（人挑）—— 退化成人工调度
- **终止**：max_round 或特殊 token（"TERMINATE"）

#### 2. Role-Playing Debate（角色对抗）

固定角色对，轮流发言。典型是双 Agent：user proxy 出题、assistant 解答，或 defender vs challenger 互怼。

- **代表**：CAMEL、Constitutional AI 的 critic-defender
- **机制**：一方提问/挑战，另一方回答/防守，直到 task instruction 达成
- **为什么 work**：对抗产生信息，单 Agent 同时自我批评效果远不如双 Agent 真互喷

#### 3. Waterfall Role（瀑布角色）

借用软件工程 SOP：ProductManager → Architect → Engineer → QA，每角色是 Agent，按流程交接。

- **代表**：MetaGPT、ChatDev
- **争议**：本质是 Pipeline + 角色扮演，**不是真蜂群**；被叫 swarm 是市场话术
- **与 kedo 旧 AgentLoop 的关系**：同一类架构

#### 4. Handoff-based（交接式）

Agent 之间无共享消息通道，**主动 hand off** 控制权给下一个 Agent。当前 Agent 说"交给 BillingAgent 吧"，框架切换。

- **代表**：OpenAI Swarm (2024) —— 最轻量的蜂群框架
- **机制**：每 Agent 有 `functions=[transfer_to_X]`，调用即切换
- **本质**：把"分派"从 Orchestrator 改成 Agent 自主决定
- **好处**：代码极简（几百行框架）；**坏处**：agent 间容易"踢皮球"

### 关键技术机制

| 机制 | 作用 | 常见失败模式 |
|---|---|---|
| **Speaker Selection** | 决定下一个发言者 | LLM-pick-next 有 confirmation bias（挑同意自己的） |
| **Termination Detection** | 何时停 | 绝大多数蜂群靠 max_round 硬截断，不是真收敛 |
| **Shared Memory** | Agent 间共享什么 | 全广播 → O(N²) token 成本；私聊 → 信息孤岛 |
| **Role Prompting** | 角色差异化 | 多轮后角色漂移（Engineer 开始指点需求） |
| **Consensus / Voting** | 多 Agent 投票 | 多数 ≠ 正确；同模型多 Agent 意见高度相关 |
| **Debate Rounds** | 对抗轮次 | 越多越贵；3 轮后边际收益递减 |

### 代表系统一览

| 系统 | 类型 | 亮点 | 坑 |
|---|---|---|---|
| **AutoGen** (Microsoft) | Group Chat | speaker_selection 可配、UserProxy 人在回路 | token 爆炸、speaker 选择难调 |
| **CAMEL** | Role-Playing 双 Agent | 理论清晰、paper 被引高 | 实用案例少 |
| **MetaGPT** | Waterfall Role | 模拟软件公司 SOP、生成完整项目 | 本质 pipeline、不适应迭代 |
| **ChatDev** | Chat Chain | 多阶段 multi-agent 瀑布 | 对简单任务严重过度设计 |
| **OpenAI Swarm** | Handoff | 框架极简、教育友好 | 官方标 "experimental"，非生产框架 |
| **CrewAI** | Role + Task | 工程化好、上手快 | 仍是 pipeline flavor |

### Work vs Not Work

**Work 的场景**
- 开放式创造：brainstorming、写作、角色扮演剧本、架构设计讨论
- 需要对抗性验证：代码审查、debate、adversarial testing
- 模拟人类组织：PM + 设计师 + 工程师的 SOP 很难用单 Agent 模拟
- 无客观评价函数：艺术、UX、策略

**Not Work 的场景**
- 有 ground truth 的 pipeline：code → build → test 有明确成功信号，蜂群扯皮浪费 token
- 预算敏感：蜂群 token 消耗常是单 Agent 的 3-10 倍
- 需要 debug：3 个 Agent 的消息交错到一条 timeline 上，bug 基本没法定位
- task 目标明确：单 Agent + ReAct 足够，蜂群只是演剧

### 学术 vs 生产的割裂

Paper 里蜂群在 HumanEval 等 benchmark 上比单 Agent 高 5-10%。但**每条 query 的 token 成本也高 5-10×**。这个 ratio 在 paper 里很少被突出。**生产环境里 cost-adjusted performance 常常持平甚至倒退**。

### 对 kedo 的启示（诚实版）

**主架构不上蜂群**。理由：
- kedo 核心路径 code_gen → build → test → evaluate 是明确 pipeline
- 蜂群的扯皮成本（1.5-2.5× → 3-10×）在 Kimi token 敏感场景不可接受
- state divergence 在蜂群里更严重（N 个 Agent 各自缓存文件内容）
- 调试复杂度：dashboard 目前是单 task 一条线，蜂群意味着大改

**但 3 个局部可借用的蜂群 idea**：

1. **Debate 式 Reviewer**（方案 C 的蜂群增强版）
   当 Reviewer 打分 < `min_eval_score` 且 Worker 不同意时，**第三 Agent 作裁判**。这是"2 Agent 分歧 → 召唤裁判"的极小蜂群，只在分歧时触发，非常态。

2. **Multi-critic on low confidence**
   `ai_confidence < 0.6` 的 commit 候选，**并行多 Reviewer**（不同 prompt、不同 provider），一致 → 过；不一致 → `pause_for_human`。"市场投票"式蜂群的局部应用。

3. **Brainstorm on propose_alternatives**
   LLM 纠结选 libnfs 还是 SMB 时，2-3 Agent 各自扮演一方案的"辩护律师"，写 pro/con 给用户选择。role-debate 的单次应用，不是持续蜂群。

**结论**：蜂群在 kedo 里的角色是**"特殊情况触发的局部工具"，不是主架构**。主架构仍应是方案 A 或方案 B。

## 方案 B vs 方案 C：详细对比（含 kedo 选型推荐）

上文"4 候选方案"是高层概览。这一节把最值得认真考虑的两个——**B (Orchestrator-Worker)** 和 **C (双 Agent 对抗 / Actor-Critic)**——按同一套维度并列对比，附 kedo 当前阶段的选择推荐。

### 结构本质

**C 的拓扑 — 线性 + 关卡**

```
Producer (现 ReactAgent) ─┬─> build/test ok? ─> Reviewer ─┬─> pass ─> commit
                          │                                │
                          └── reject/rework <──────────────┘
```

Producer 主导 loop，Reviewer 只在**关卡点**（build 成功 / test 通过 / commit_candidate 前）被"拉进来审一道"。结构几乎不变。

**B 的拓扑 — 星形 + 多节点**

```
             Orchestrator
           ╱      │      ╲
      Planner   Worker   Reviewer
           ╲      │      ╱
             Blackboard
```

Orchestrator 是协调中心，各 Agent 平行接活回结果。结构是重画的。

### 9 维度对比

#### 1. Agent 组成

| 维度 | C | B |
|---|---|---|
| Agent 数 | 2 | 4–5 |
| 顶层决策 | Producer 自己 | Orchestrator |
| Plan | Producer（plan_development 工具） | 独立 Planner |
| Execute | Producer | Worker（context 更窄） |
| Review | 独立 Reviewer | 独立 Reviewer |
| Route/协调 | 不需要 | Orchestrator |
| Memory | 单 context + Reviewer 自 context | 可选 Memory Agent |

#### 2. Context 结构

- **C**：2 份 context，完全解耦。Producer 保持现状完整 messages；Reviewer 只看 [需求 + plan 最终版 + 产物路径 + build/test 输出]
- **B**：5 份 context，需同步（Orchestrator / Planner / Worker / Reviewer / Memory 各自独立）

#### 3. 通信机制

| 维度 | C | B |
|---|---|---|
| 调用形式 | 同步 `Reviewer.review()` | Orchestrator ↔ Agent 异步消息 |
| 协议 | 一个 dataclass `ReviewResult{approve, score, comments}` | 消息 schema（from/to/intent/payload/blackboard_ref） |
| 并发 | 无 | Worker 可并发（kedo 强依赖限制收益） |
| 通信次数 | ~1–3 次 | ~10–20 次 |

#### 4. Checkpoint & 失败恢复

| 维度 | C | B |
|---|---|---|
| Checkpoint 所有者 | Producer 一份 | 多份（Orchestrator + 每 Agent） |
| Reviewer 挂了 | 重试或降级为 Producer 自评，**主 loop 不影响** | 同理但恢复路径更长 |
| Worker 挂了 | 不存在 | Orchestrator 重分派 / 换 Worker |
| Orchestrator 挂了 | N/A | 谁管 Orchestrator 是开放问题 |
| Resume-checkpoint 语义 | 不变 | 重写 |

#### 5. Token / 延迟成本（每 task 粗估，N = 主 loop 轮数）

| 维度 | 单 Agent 基准 | C | B |
|---|---|---|---|
| 主 loop tokens | N × 8k | N × 8k（不变） | N × 6k（context 收窄） |
| 协调 overhead | 0 | +18k（Reviewer 1–3 次） | +110k（Orchestrator + Planner + Reviewer + Memory） |
| 相对单 Agent | 1× | **1.2–1.3×** | **1.8–2.5×** |
| 墙钟延迟 | 串行 | +20–30% | +10–40% |

#### 6. 对 kedo deep-dive 6 大问题的缓解

| 问题 | C | B | 备注 |
|---|---|---|---|
| Self-eval drift | ★★★★★ | ★★★★★ | C 已足够 |
| Context anxiety | ★★ | ★★★★ | B 显著更强 |
| Planning instability | ★★ | ★★★ | B 拆了 Planner |
| Hallucinated execution | ★★ | ★★ | ReAct 本身已抑制 |
| Long-horizon memory | ★ | ★★★ | 需 Memory Agent |
| Role conflict | ★★★★ | ★★★★★ | 都破，B 更彻底 |

#### 7. 工程改动量

- **C（~1 周）**：新 `Reviewer` Agent 类（~200 行）；`EvaluateTool` / `CommitCandidateTool` 把"调同 LLM"换成"调 Reviewer.review()"；配置项 `reviewer_provider / reviewer_model / reviewer_api_key`。**现有 15 工具、checkpoint、dashboard、routes 全不变**。
- **B（~4–6 周）**：Agent 基类 + 4 个 Agent（~2000 行）；消息协议 schema；dispatch 机制；per-agent checkpoint；`POST /tasks` 改入口到 Orchestrator；dashboard 多 Agent 时间线可视化；Worker context 窄化；跨 Agent convergence detection 重实现；resume-checkpoint 语义重写。

#### 8. Risk surface

| 风险 | C | B |
|---|---|---|
| Reviewer 过严 / 过宽 | 有（可 threshold 调） | 有（同） |
| State divergence | 几乎无（Reviewer 只读产物） | 显著（多 Worker 视角分歧） |
| Convergence 失效 | 继承现有 M1 机制 | 需重建跨 Agent convergence |
| Dashboard 不一致 | 无风险（不改） | 中风险（新引入） |
| 迁移 bug | 低 | 高 |

#### 9. 可试错性 / Rollback

| 维度 | C | B |
|---|---|---|
| A/B 测量 | 易（同批 task 开/关 Reviewer 对比） | 难（架构已换，回不去） |
| Feature flag 关闭 | 一行配置（`reviewer_provider: none`） | 整套基础设施已引入 |
| 回退到单 Agent | 瞬时 | 需撤大量代码 |

### 4 个反直觉点

1. **B 的"并行"收益在 kedo 几乎不存在**
   code_gen → build → test 有强依赖，没 subtask 能并发。真正要并行需"探索多方案"（libnfs vs SMB 同跑）——这个场景**单 Agent 串行两次也 OK**。**不应以并行为主理由选 B**。

2. **C 的 Reviewer Agent vs Phase 1 "工具内独立 LLM client" 的差别**
   表面看一样——都是独立 LLM、独立 context。区别：
   - **Phase 1 工具版**：每次 evaluate 重建 LLM client，**无跨调用状态**
   - **方案 C Agent 版**：Reviewer 是持久化对象，**跨关卡点累积判决历史**（"这次 build 的代码我上次 review 过，只看增量"）、有自己的 convergence tracking
   长任务 + 多轮改代码场景差异显著。

3. **B 的 Worker context 收窄收益被低估**
   现 ReactAgent 长 task 时 messages 可膨胀到 40–60k，Kimi 的长 context quirk（reasoning-only fallback、未闭合 fence）发生率上升。Worker 只看 ~8k subtask 时这类 quirk **显著下降**。C 拿不到这个红利。

4. **C → B 演进的前置条件**
   C 跑一段时间后若发现"Self-eval drift 破了但仍因 context anxiety 翻车"——B 的 Worker 收窄价值凸显。**未实证就上 B 是"为架构而架构"**。

### 两个具体场景走查

**场景 1：switchvideo NFS 视频播放器完整 run**
- **C**：~12 Producer 轮 + 3 Reviewer 介入（build ok 审 #1 / test ok 审 #2 / commit 前审 #3）→ done
- **B**：Orchestrator → Planner 5-step plan → 多 Worker 逐 step 执行 → Reviewer 审总体 → Orchestrator commit。多 ~5 次 Agent 间调用（+40k tokens），并发收益此场景 ≈ 0

**场景 2：简单 bug fix（改一个 if 判断）**
- **C**：Producer 2–3 轮 + Reviewer 审 1 次
- **B**：Orchestrator 路由判断（+1–2 LLM 调用）→ Worker → Reviewer → Orchestrator commit。**overhead 不成比例**

### 决策矩阵：什么情况选哪个

| 情况 | 推荐 |
|---|---|
| kedo 当前 = 个人工具 / 单用户 / pipeline 任务主导 | **C** |
| 已实证 Self-eval drift 是主要瓶颈 | **C**（一周出结果） |
| 长任务里 Producer context 经常超 40k、Kimi quirk 频繁触发 | **B**（Worker context 收窄有不可替代价值） |
| 需要真正的多 task 并发（libnfs / SMB 同跑） | **B** |
| 产品化：多用户 + 多并发 + dashboard 丰富可视化 | **B** |
| 只有一周投入 | **C** |

### 推荐（kedo 当前阶段）：**先做 C，实证满足度后再决定是否演进到 B**

理由：
1. kedo 当前最大 pain 是 **Self-eval drift + 长 context Kimi quirk**。C 直接解决第一个，第二个**单 Agent 内剪裁也能缓解一半**。
2. C 的 ROI（1 周 / Self-eval drift 完全破解）碾压其它方案。
3. C **不锁定未来演进**——跑 1–2 个月后若瓶颈变成"Producer context 仍爆"，再从 C 演进到 B，Reviewer Agent 组件可直接复用。
4. B 当前"并行"和"多用户"需求尚不存在；为假想需求做 4–6 周基础设施不合算。

**一句话**：C 是"最小破 Self-eval drift 手段"，B 是"kedo 产品化基础设施"。阶段不对就上 B 会给自己制造问题。

## 方案 B 详细设计：Orchestrator-Worker

下面是方案 B 的**具体**形态，不是空想 framework。

### 架构图

```
                        ┌─────────────────┐
                        │  User Request   │
                        └────────┬────────┘
                                 │
                    ┌────────────▼────────────┐
                    │   Orchestrator Agent    │◀──── (routing + coordination)
                    │   (decides who + when)  │      context: 小，仅任务元信息
                    └─────┬──────┬──────┬─────┘
                          │      │      │
          ┌───────────────┘      │      └────────────────┐
          │                      │                       │
    ┌─────▼──────┐         ┌─────▼──────┐          ┌─────▼──────┐
    │  Planner   │         │   Worker   │          │  Reviewer  │
    │  Agent     │         │   Agent    │          │   Agent    │
    │            │         │            │          │            │
    │ plan_dev   │         │ code_gen   │          │ evaluate   │
    │ propose_*  │         │ build      │          │ commit_*   │
    │            │         │ test_run   │          │ (不同 LLM   │
    │ 角色:      │         │ auto_fix   │          │  provider) │
    │ 拆解架构   │         │ file_*     │          │ 角色: 独立 │
    │ 换思路     │         │ shell_exec │          │ code       │
    │            │         │            │          │ reviewer   │
    │ context:   │         │ context:   │          │ context:   │
    │ 只看需求 + │         │ 只看自己   │          │ 只看 plan+ │
    │ 平台 hints │         │ 的任务 +   │          │ 产物+需求  │
    │            │         │ 工具历史   │          │ (只读)     │
    └────────────┘         └──────┬─────┘          └────────────┘
                                  │
                           ┌──────▼──────────────┐
                           │  Shared Blackboard   │
                           │  (文件系统 + .kedo/  │
                           │   state + Profile)   │
                           └──────────────────────┘
                                  ▲
                           ┌──────┴──────┐
                           │ Memory Agent │
                           │ (压缩 / 回忆) │
                           │ context: 大, │
                           │ 只看消息摘要 │
                           └─────────────┘
```

### 4 个 Agent 的职责划分

| Agent | 可用工具 | Context 内容 | LLM 建议 |
|---|---|---|---|
| **Orchestrator** | `dispatch_to_X` / `pause_for_human` / `respond` | 任务元信息 + 各 Agent 摘要，不看工具调用历史 | 便宜快速（Haiku / kimi-k2.5） |
| **Planner** | `plan_development` / `propose_alternatives` / `scan_platform` | 需求 + 平台 hints + prior task 摘要，不看工具历史 | 强推理（Claude Opus / GPT-4） |
| **Worker** | `code_generate` / `file_*` / `shell_execute` / `build` / `test_run` / `auto_fix` / `git` | 当前 subtask 的目标 + 本 subtask 的工具历史，不看其它 subtask | 强代码（Claude Sonnet / Kimi Code） |
| **Reviewer** | `evaluate` / `commit_candidate` | plan 最终版 + 实际产物（代码文件）+ build/test 结果，只读 | **第三方 provider**（主 Kimi → Reviewer Claude；主 Claude → Reviewer Kimi） |
| **Memory** | `summarize` / `recall` | 全局消息历史 + checkpoint 索引 | 便宜（Haiku） |

### 核心协议

**Shared Blackboard**：文件系统 + `.kedo/state/` 是天然共享的。Agent 间不传递代码文件内容，只传路径；都从磁盘读。**避免 agent 间的"我以为 main.c 长这样" 分歧**。

**消息协议**（受 Google A2A / Anthropic MCP 启发，但简化）：

```json
{
  "from": "orchestrator",
  "to": "worker",
  "task_id": "ac2b9390",
  "subtask_id": "st_3",
  "intent": "execute_subtask",
  "payload": {
    "subtask": {"title": "generate main.c", "spec": "..."},
    "constraints": ["必须用 libnfs", "不许用 sudo"],
    "deadline_turns": 10
  },
  "blackboard_ref": "/home/.../switchvideo"
}
```

Orchestrator 发消息 → Worker 起自己的 ReactAgent 实例（独立 LLM context）→ 完成后返 `{status, artifacts: [file_paths], summary}` → Orchestrator 决定下一步。

### 为什么 Reviewer 一定要**不同 LLM provider**

Reviewer 用同一 LLM 的结果：实证上给自己代码打分平均 +15-20 分偏高（Kimi 对自己 ~85、Claude 独立审 ~70）。**只有跨 provider 才真正破 confirmation bias**。

kedo 当前 LLM 配置只有一个活跃 client (`_react_agent.llm`)，要支持这个就要新配置：

```yaml
llm_provider: "kimi"         # 主 LLM
reviewer_provider: "claude"  # Reviewer 用不同 provider
reviewer_model: "claude-sonnet-4-6"
reviewer_api_key: "sk-ant-..."
```

运行时 Reviewer Agent 用独立 LLM 实例。

## 从单 Agent 到多 Agent 的迁移路径

**关键原则**：不大爆炸式重写。分 4 个可验证阶段，每个阶段结束都能实战跑。

### Phase 1 — 抽离 Reviewer（~1 周）

最小爆炸半径，收益最大：

- 新增配置 `reviewer_provider / reviewer_model / reviewer_api_key`
- `EvaluateTool` / `CommitCandidateTool` 两个工具在执行时**创建独立 LLM client**（不是用 ReactAgent 主 LLM）
- 单元测试 + switchvideo 实战跑一次 eval，观察分数差异

这一步几乎不碰架构，只改两个工具的 LLM 来源。**低风险、高价值**（破 Self-evaluation Drift）。

### Phase 2 — 抽离 Planner（~1-2 周）

`PlanTool` 从"调同 LLM 换 prompt"升级为"起独立 Planner Agent 进程/协程"：

- Planner Agent 有自己的 messages 列表（不复用主 ReactAgent 的）
- Planner 只看需求 + 平台 hints + prior task 摘要
- Planner 可配独立 LLM provider（可能用更强的推理模型）
- 输出 plan → 写 checkpoint → 主 ReactAgent 读 checkpoint 执行

这一步开始真正"分家"。好处：planner 的思考不占主 ReactAgent context。

### Phase 3 — Orchestrator + 多 Worker（~2-3 周）

真正的多 Agent：

- 新增 `OrchestratorAgent` 作为 routes.py POST /tasks 的入口
- Orchestrator 决定：这是闲聊？直接答；这是 bug fix？起一个 Worker；这是复杂开发？先 Planner 再 Worker
- Worker 是现在的 ReactAgent 换个名字，但 context 更窄（只给 subtask 相关）
- 多 Worker 并行跑（独立 subtask 可以并发）

这一步架构变化最大，需要：
- 新的消息协议定义
- Agent 间 ID 路由
- Dashboard 多 task / 多 agent 的可视化
- 失败回滚（某 Worker 挂了，Orchestrator 怎么恢复）

### Phase 4 — Memory Agent（~1 周）

独立的 Memory Agent 负责：
- 压缩老 messages 为摘要
- 任务链上下文生成
- 跨 task 检索

之前提过 `AgentMemory.get_context_window()` 未接入——这一阶段把它做成独立 Agent 而不是 library call。好处：摘要有自己的 LLM budget + 独立 prompt。

## 工程复杂度分析

多 Agent 不是免费的。诚实账本：

### 成本

| 维度 | 单 Agent 成本 | 多 Agent 成本 |
|---|---|---|
| 每 task LLM 调用次数 | N（一个 Agent N 轮） | 1.5-2.5N（多 Agent 间协调有冗余） |
| 每 task token 消耗 | 单 context 无重复 | Agent 间传递摘要 + 独立 system prompt 有 ~30% overhead |
| 每 task 墙钟时间 | 串行 | 并行部分 -20-40%，协调部分 +10% |
| 调试复杂度 | 单一日志 | 多 Agent trace，需 distributed tracing 基础设施 |
| 状态一致性风险 | 无（单 context） | Agent 间"我以为的项目状态" 分歧 |

### 收益

| 维度 | 收益估计 |
|---|---|
| Self-evaluation Drift 缓解 | 高：跨 provider reviewer 直接破 bias |
| Planning Instability 缓解 | 中：planner 独立 context 但 execution drift 仍在 worker 里 |
| Context Anxiety 缓解 | 高：每 Agent 只看自己的 context，窄且聚焦 |
| Long-horizon Memory Loss 缓解 | 中：Memory Agent 专职压缩，但 Agent 间状态同步仍是问题 |
| 开发速度 | **短期负**（要搭基础设施）、**长期正**（新 LLM 接入变容易、agent 独立测） |

## 决策矩阵：什么情况下开启多 Agent

这是一个可选演进，不是必须。参考决策：

| 场景 | 推荐架构 |
|---|---|
| 单用户、小项目、kedo 作为个人工具 | **保持单 Agent**（现状够用，复杂度不值） |
| 多用户、大项目、需要同时跑多 task | 考虑 **Phase 3 Orchestrator**（多 Worker 并行） |
| Self-evaluation Drift 已被实测发现严重 | 立刻做 **Phase 1 Reviewer 跨 provider**（1 周内收益） |
| LLM provider 成本敏感（想便宜 Agent 跑路由） | 考虑 **Phase 4 Memory Agent**（Haiku 跑摘要省钱） |
| 想研究 agent-to-agent 协作本身 | 全 Phase 1-4 走完 |

## 现实建议

kedo 的 deep-dive 已经揭示：**80% 的问题在单 Agent 内能缓解** (M1-M3 已做)。多 Agent 不是解决最后 20% 的唯一出路——更便宜的选项包括：

- "commit_candidate 前置事实检查"（~1h，单 Agent 内）
- "核心约束 reminder 注入"（~2h，单 Agent 内）
- "接入 AgentMemory.get_context_window"（~2h，单 Agent 内）

**Phase 1 Reviewer 跨 provider** 是多 Agent 演进里 ROI 最高的第一步（~1 周），因为它是**唯一真正破 Self-evaluation Drift 的手段**，单 Agent 内做不到。

Phase 2-4 值得做的条件：
- Phase 1 实战确认了 reviewer 独立的价值
- kedo 使用场景扩大到多用户 / 多并发 task
- 有时间做基础设施（distributed tracing、消息协议、dashboard 多 Agent 可视化）

## 下一步计划（已决：方案 C 先行）

**2026-04-21 决策**：先实现方案 C（双 Agent 对抗 / Actor-Critic），实证 Self-eval drift 破解效果后再决定是否演进到方案 B。详见上文"方案 B vs 方案 C：详细对比"。

| 优先级 | 任务 | 工作量 | 前置 |
|---|---|---|---|
| **P0**（进行中） | **方案 C：Reviewer 独立 Agent**（持久化对象、跨关卡累积判决、独立 LLM provider） | ~1 周 | 无 |
| P1 | ai_confidence 校准统计（和 Reviewer 配合） | ~2h | 方案 C |
| P1 | 单 Agent 内的 context / 约束 reminder 改进（与 C 正交、可并行） | ~5h | 无 |
| P2 | 评估是否演进到方案 B（触发条件：C 跑 1–2 个月后 Producer context 仍经常 >40k，或需多 task 并发 / 多用户） | 评估 | 方案 C 实战 1–2 个月 |
| P3 | 方案 B Phase 2：Planner 独立 context | ~1-2 周 | P2 判定需演进 |
| P3 | 方案 B Phase 3：Orchestrator + 多 Worker | ~2-3 周 | Phase 2 成功 |
| P3 | 方案 B Phase 4：Memory Agent | ~1 周 | Phase 3 基础设施就绪 |

## 一句话总结

> **多 Agent 是手段不是目的**。kedo 的单 Agent + 15 工具 + P1-M3 护栏已经覆盖 80% 的可靠性问题；多 Agent 的真正价值在于破那些**物理上同一个 LLM 做不到**的事（跨 provider judge、并行执行、专家分工）。分 4 阶段渐进迁移，Phase 1 Reviewer 跨 provider 是必做、Phase 3-4 是 aspirational。
