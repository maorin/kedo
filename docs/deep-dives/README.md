# Deep Dives — kedo 核心问题答疑

这里收集对 kedo 架构的深度讨论：每一篇都是一个"为什么要这么做 / 现在怎么做 / 还差什么"的坦诚分析。和 `../architecture.md` 的区别在于：那是"kedo 现在是什么样"，这里是"为什么是这样 + 哪里还没做到位"。

目标读者：想**真正理解** kedo 决策权衡、而不仅仅是会用的人（新贡献者、深度用户、以及 3 个月后需要回忆"当初为啥这么定"的作者自己）。

## 目录

| 问题 | 文档 |
|---|---|
| 1️⃣ Context Anxiety（上下文焦虑）— 长会话/大项目里怎么不撑爆 context window？ | [context-management.md](context-management.md) |
| 2️⃣ Self-evaluation Drift（自评失真）— LLM 既当 executor 又当 judge，怎么防 confirmation bias？ | [self-evaluation.md](self-evaluation.md) |
| 3️⃣ Planning Instability（规划不稳定）— plan 生成 variance、execution drift、plan 不随进度更新 | [planning-instability.md](planning-instability.md) |
| 4️⃣ Tool Fragility（工具依赖脆弱）— 外部依赖缺失、参数错、工具间隐式依赖 | [tool-fragility.md](tool-fragility.md) |
| 5️⃣ Long-horizon Memory Loss（长任务遗忘）— 30+ turn 后早期约束/决策被稀释 | [long-horizon-memory.md](long-horizon-memory.md) |
| 6️⃣ Hallucinated Execution（幻觉执行）— LLM 声称做了实际没发生 | [hallucinated-execution.md](hallucinated-execution.md) |

## 架构演进设计

| 主题 | 文档 |
|---|---|
| 🛠 从单 Agent 到多 Agent — 4 候选方案对比（Sub-agent as Tool / Orchestrator-Worker / Actor-Critic / Blackboard）+ 蜂群（Swarm）技术专题 + 优缺点矩阵 + 4 Phase 迁移路径 | [multi-agent-architecture.md](multi-agent-architecture.md) |

> 欢迎增补。新问题模板：**问题本身 → 实际做了什么（对着代码引用）→ 诚实的 gap → 为何现状还 work → 下一步候选**。
