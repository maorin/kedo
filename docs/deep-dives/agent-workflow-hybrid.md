# 自主 Agent 与 LLM Workflow 的 Hybrid 化

> **状态**:讨论稿。2026-04-27 立项,基于 switchvideo 6075f8ec / b9f19e61 两次实战漂移事件。
> **本文档定位**:架构方向探讨,不是实施 reference。下次会话基于此文展开切片设计。
> **关联文档**:
> - `multi-agent-architecture.md` — 多 Agent 拓扑(协作 vs 对抗)的横向对比
> - `dual-agent-architecture.md` — 已落地的方案 C 实施 reference
> - `planning-instability.md` — Plan 漂移的现象学

## TL;DR

- 自主 Agent 与 LLM Workflow **不是 either/or 选择**,而是**分层 hybrid**——看每一层用哪个。
- kedo 已经走过一轮:**workflow backbone (旧 `agent_loop.py`) → agent backbone + guardrails (现在的 `react_agent.py` + charter/guard/reviewer)**。这是从光谱左端跳到右端的过程。
- 实战痛点(switchvideo)证明:**纯 agent 的 backbone 在 50+ turn 长任务里仍然漂**——LLM 注意力分散导致跑偏 plan、错诊断方向。
- 推荐下一步:**③ Plan-as-Contract + ④ Hierarchical Agent 的渐进混合**,把 plan 升格为 frozen 契约,把 reviewer 升格为有 readonly 工具的诊断师,长远引入 Architect 角色避免诊断方向单点漂移。

## 1. 为什么讨论这个

switchvideo 项目从 2026-04-19 立项到 2026-04-26,kedo 跑了 20+ 个 task,产生了三类反复发生的失败模式:

1. **诊断方向单点漂移**——4 连 task (915eb677 / 6075f8ec / b9f19e61 / d05947e9) 都把 Switch 错误码 2168-0002 诊断为"栈空间不足 + NPDM 服务权限缺失",反复在这两个错的方向修。**真因在 main.cpp 前 30 行**(`consoleInit` 与 `framebufferCreate` 共抢 default nwindow),从未被任何一次诊断触及。
2. **Plan 漂移**——`plan_development` 出了 plan 之后,LLM 跑 50+ turn 后会遗忘当前 subtask,跳去做无关的事(b9f19e61 把 main.cpp 改成 32 行字体绘制 stub)。
3. **结构性破坏**——LLM 在没有契约约束时引入双 build system、改飞 profile.build.command、CMakeLists target rename 不同步部署命令(已被方案 C 的 charter 拦下)。

这三类失败的共同根源:**纯自主 agent 的"短期注意力 + 长期遗忘"特性**,在长任务上被反复利用又反复掉坑。

## 2. 两端光谱的本质区别

```
                                                     长任务漂移
        ┌──────────────────────────────────────────────────►
        │
   纯 LLM workflow (pipeline)        纯自主 agent (ReAct)
   ────────────────────────         ──────────────────────
   预定义 step 序列                  LLM 自主决策下一步
   每个 step 是黑盒                  无预设流程,看上下文
   step 间硬性约束                   靠 LLM 自己判断
        │                                       │
   优:可预测/可观测/cost 可控       优:灵活/能处理未预料情况
   缺:刚性/新需求要改 workflow      缺:漂移/难收敛/cost 不可控
```

这两端在 kedo 都跑过。

## 3. kedo 自己的演进路径

```
2025  旧 core/agent_loop.py (3406 行)
       └─ 刚性 workflow:plan → code → build → test → eval → commit → deploy
       └─ 闲聊/bugfix 靠 if/else 特判
       └─ Pain:每加一类需求改 workflow,workflow 变成 spaghetti

2026-04-16  core/react_agent.py (~400 行) 上线 (M1)
       └─ ReAct loop,LLM 自主决策工具调用
       └─ 工具集 15 个,LLM 自由组合
       └─ Win:hello-world 全流程跑通,小改动迭代快

2026-04-19  ReactAgent 单轨 (M3)
       └─ AgentLoop → core/_legacy/,完全切走

2026-04-25 ~ 04-27  + 方案 C(charter / guard / reviewer)
       └─ Agent backbone 不变,在 hook 上加 deterministic 闸门
       └─ 拦双 build system / target rename / forbidden_files
       └─ build 失败 ≥3 次自动调 reviewer 给指令性反馈
       └─ Pain:闸门救不了"plan 漂移"和"诊断方向单点漂移"
```

**当前位置:Agent as backbone, Workflow as guardrails**(下表 ②)。

## 4. 四种典型结合方式

| 方案 | 形态 | 强项 | 弱项 | 实战代表 |
|---|---|---|---|---|
| ① **Workflow as backbone, Agent in cells** | 流水线骨架(plan→code→build→test→eval→commit→deploy),每个 cell 内部用 ReAct | 可预测、易插桩、token 可控 | 新需求要改 workflow,闲聊/bugfix 也走全流程,僵 | 旧 kedo agent_loop / GitHub Copilot Workspace |
| ② **Agent as backbone, Workflow as guardrails** | 主体 ReAct,在关键 hook 插确定性闸门(charter / profile_guard / commit gate / reviewer) | 灵活,长任务不死,工具组合自由 | 长 task 容易漂离 plan,仍依赖闸门兜底 | **kedo 现在** / Claude Code 的 hooks 模式 |
| ③ **Plan-and-Execute with checkpoint** | 先 plan 出 DAG → agent 执行每节点 → 每步回 plan 看进度调整 | 既灵活又有锚 | plan 也是 LLM 出的,plan 错了下游照样错 | LangGraph plan-execute / Devin 早期 |
| ④ **Hierarchical agent** | 上层 strategic (Architect) → 中层 tactical (Coder) → 底层 deterministic check | 对应人类 PM/工程师/QA 分工,可观测性好 | 多 agent 协议要设计,成本翻 N 倍 | AutoGen / OpenDevin |

## 5. 当前位置(②)的固有 limit

`react_agent.py` 主循环本质上是 `while turn < max_turns: LLM(messages + tools) → execute → append`。在 50+ turn 长任务里:

- **早期 plan 在 messages 里被稀释**——50 turn 后 plan_development 工具调用产生的 plan output 被 truncate 截掉或被新 tool 调用挤出 context attention focus
- **诊断方向锁定**——一旦 LLM 第 5 turn 决定"是栈不够",后续 45 turn 全部 confirmation bias 围绕这条假设
- **Reviewer 只看 diff/build 结果**——拦不住"诊断方向选错"这种**元层面**问题。它能看到"build 又失败了",但不能反思"为什么我们一直在改栈大小"

charter / profile_guard 是结构性约束(拦双 build system 这类),但**plan 和诊断方向是过程性的**,没法用 charter 约束。

## 6. 推荐的演进方向(渐进 ③ → ④)

按落地代价排序,每一步都是独立可发布的最小切片。

### Step 1: Plan-as-Contract(③ 半步)

把 `plan_development` 的输出从"LLM 自言自语的建议"升格为**reviewer 守的契约**。

具体改动:

- Plan 出来后写盘到 `.kedo/state/<task_id>_plan.json`,frozen 状态
- 每次 file_write / build / shell_execute 前,reviewer 看一眼"这次 turn 是否在执行当前 subtask"
- 漂离当前 subtask 的关键操作 → reviewer 拒,退回让 LLM 显式选 subtask 或 `propose_plan_change`
- LLM 想改 plan 必须 `propose_plan_change(subtask_id, new_description, reason)`,跟 charter 一样阻塞等用户

**对 switchvideo 的修复力**:b9f19e61 那次 LLM 把 main.cpp 改成字体绘制 stub 的 file_write,会被 reviewer 识别为"当前 subtask 是栈/NPDM 修复,这次写入跟字体绘制无关",拒。

**改造大小**:估 2 天,跟 charter 改造大小相当。

### Step 2: Reviewer with readonly tools(④ 半步)

当前 Reviewer 只能看 (diff + build_stderr),给它加 readonly 工具:

- `file_read` (任意文件)
- `shell_execute_readonly` (`grep` / `ls` / `head` / `git log` 等,白名单)
- `web_fetch` (查文档)

让 reviewer 在 advise/review 时能**主动诊断**而不只是评分。

**对 switchvideo 的修复力**:reviewer 看到 build 一直失败/反复改栈,会主动去读 main.cpp 前 50 行+查 libnx consoleInit 文档,识别出 console/framebuffer 资源冲突,直接给 Producer "停修栈,看 main.cpp:195 + main.cpp:217 的资源冲突" 的 actionable 反馈。

**改造大小**:估 1.5 天。Reviewer 拓扑要变(从单轮 LLM 调用变成自己也是 ReAct loop),token 成本会涨。

### Step 3: Architect agent(真正的 ④)

引入轻量"Architect" agent,只在 task 启动 / 严重卡死时介入:

- task 启动时,Architect 读 user requirement → 输出"这次任务该走哪条诊断/实施路径",对 Producer 是 plan 的上游约束
- Producer 卡死(reviewer 介入 ≥2 次仍无进展)→ Architect 重新评估方向,可以推翻当前 plan
- Architect 跟 Reviewer 跑独立 LLM(再加一道 self-eval drift 防护)

**对 switchvideo 的修复力**:第一次 task 启动时,Architect 看到"Switch 启动直接 2168-0002" 会输出诊断清单(crt0/console/资源/链接/NPDM 全套排查路径),Producer 不会一头扎进"栈不够"这条死胡同。

**改造大小**:估 3-4 天。需要新协议:Architect ↔ Producer ↔ Reviewer 三方消息流。

## 7. Tradeoff

往 ③ ④ 走的代价:

- **token 成本**:Plan-as-Contract 每次 file_write 多一次 reviewer 调用;Reviewer-with-tools 每次诊断展开多轮 ReAct;Architect 每个 task 多一段 strategic LLM。整体 cost 估算 1.5×~3×。
- **延迟**:每个 file_write 多 1-2 秒 reviewer 检查(本地缓存可缓解)
- **复杂度**:多 agent 协议设计,事件流变密,dashboard 要扩展显示三方对话

但 switchvideo 这次浪费的不是 token 而是**用户耐心 + working tree 整洁度**:

- 3 天 4 个 failed task,每个跑到 max_turns 才死
- 项目 working tree 被改成 CMakeLists / Makefile / npdm.json / 双 build 目录的混乱状态
- 真 bug (main.cpp 30 行) 一直没人看见

如果 ③ 半步落地能让 switchvideo 这种长 task 的成功率从 30% 提到 70%,那 1.5× cost 完全值得。

## 8. 开放问题(留给下次讨论)

1. **Plan-as-Contract 的颗粒度怎么定?** 每次 file_write 都过 reviewer 太重,只在 build 触发前过 reviewer 又太松。是否按"修改文件类别(profile / source / build / docs)" 分级?
2. **Reviewer-with-tools 的 readonly shell 边界?** `grep` / `ls` / `head` 是 readonly 没争议,但 `git log -p` 可能展开成几 MB,容易撑爆 reviewer context。要不要单独的 budget?
3. **Architect 该不该有 plan 改写权?** 如果有,Producer 跑到一半 plan 被换会很迷惑(messages 里 plan 跟现实不一致)。如果没有,Architect 介入只能给"建议",Producer 可能不听。
4. **失败 escalation 路径**:Reviewer 拒 Producer 3 次 → Architect 介入 → Architect 也无方向时 → 走 `pause_for_human`?这条链路上每一步的超时和 retry 怎么定?
5. **观测性**:三方对话怎么呈现给用户?dashboard 一个总 timeline 还是三个分支视图?
6. **跟 ② 的兼容性**:charter / profile_guard / dual-reviewer 这套已经落地的"workflow as guardrails"模块,在 ③ ④ 引入后是不是有重叠或冗余?能不能让 charter 直接由 Architect 维护?
7. **退出机制**:如果实战发现 ③ 让简单任务变重(一个 typo 修复也要走 plan-as-contract),怎么 fast path?
