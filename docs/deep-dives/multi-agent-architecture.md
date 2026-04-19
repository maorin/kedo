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

## 多 Agent 架构设计

下面是 kedo 可以演进的一个**具体**多 Agent 架构，不是空想 framework。

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

## 下一步候选（未决）

| 优先级 | 任务 | 工作量 | 前置 |
|---|---|---|---|
| **P0**（ROI 最高） | Phase 1：Reviewer 跨 provider | ~1 周 | 无 |
| P1 | ai_confidence 校准统计（和 Reviewer 配合用） | ~2h | Phase 1 |
| P1 | 单 Agent 内的 context / 约束 reminder 改进 | ~5h | 无（可先做） |
| P2 | Phase 2：Planner 独立 context | ~1-2 周 | Phase 1 实战验证后 |
| P3 | Phase 3：Orchestrator + 多 Worker | ~2-3 周 | Phase 2 成功 |
| P3 | Phase 4：Memory Agent | ~1 周 | Phase 3 基础设施就绪 |

## 一句话总结

> **多 Agent 是手段不是目的**。kedo 的单 Agent + 15 工具 + P1-M3 护栏已经覆盖 80% 的可靠性问题；多 Agent 的真正价值在于破那些**物理上同一个 LLM 做不到**的事（跨 provider judge、并行执行、专家分工）。分 4 阶段渐进迁移，Phase 1 Reviewer 跨 provider 是必做、Phase 3-4 是 aspirational。
