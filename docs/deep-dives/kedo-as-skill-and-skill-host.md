# kedo 作为 Skill 暴露 + 调用 Skill + 协同进化 — 三个想法的探讨稿

> **状态**：草稿，2026-05-07 立项。三个相关想法的脑暴起点，**不是实施 reference**。
> **关联文档**：
> - `agent-workflow-hybrid.md` — 自主 Agent 与 Workflow 的混合
> - `multi-agent-architecture.md` — 多 Agent 拓扑
> - `tool-fragility.md` — 工具脆弱性

## TL;DR

三个**渐进**想法：

1. **kedo 对外暴露 skill**（output protocol）：让其他 agent / 编辑器（如 Claude Code、Cursor）能像调用工具一样把任务交给 kedo。kedo 退化成"嵌入式开发自动化引擎"，前端不绑死自家 dashboard / REPL。
2. **kedo 内部调用 skill**（input protocol）：kedo 的 ReactAgent 能消费第三方 skill 包作为新工具，而不是只会调内置 `tools/*.py`。
3. **协同进化**（feedback loop）：A 实现后，调用方（如 Claude Code）反过来读 kedo 自己的源码、定位 kedo 的能力缺口、提 patch ——kedo 通过"被调用"暴露不足，然后让调用方帮自己长能力。**这是 A 的副产品，但风险/治理模型完全不同，必须单独讨论。**

A、B 互不冲突；C 必须建立在 A 之上。讨论时应分开评估。

---

## 想法 A：kedo 对外暴露 skill

### 用户场景

- **Claude Code 调 kedo**：用户在 Claude Code 里写 React 代码，遇到一个嵌入式 / Switch homebrew 的小活，让 Claude Code 把这块工作"分包"给 kedo。Claude Code 通过 skill 协议触发 kedo，等 kedo 跑完返回结果（.nro 路径 / commit_id / crash_site 报告）。
- **Cursor / 其他 IDE**：同上，把 kedo 当成"专精于 Switch homebrew + 真机 coredump 抓取 + Charter 治理"的领域工具。
- **CI 集成**：CI pipeline 直接 `curl kedo skill endpoint` 触发一次 build + reviewer 评审 + 返回 candidate。

### 核心问题

| 维度 | 问题 |
|---|---|
| **协议** | 用 Anthropic Claude Code skill 规范？MCP？OpenAI function-call？还是先支 HTTP REST 再渐次包 SDK？ |
| **什么粒度暴露** | 暴露"创建 task 跑全流程"（黑盒），还是暴露"build / fetch_crash / reviewer 评审"（白盒原子能力）？还是两层都给？ |
| **状态/会话语义** | 调用方期望"同步等结果"还是"异步轮询"？kedo 的 task 平均跑分钟级，HTTP 长连接不现实；要不要返回 task_id 让调用方 poll？ |
| **dashboard 在哪** | kedo 跑任务时人工干预（charter change、coredump pick）的弹窗在哪个端处理？kedo 自家 dashboard 还是调用方 IDE？ |
| **认证 / 多租户** | 单机用还是网络服务？多个客户端同时跑会不会抢 LLM key / 项目目录？ |
| **边界** | kedo 假设独占某个 `project_path`、自家 `.kedo/` 目录写状态——多个调用方同时进同一项目怎么办？ |

### 与现状的关系

kedo 已经有 `POST /tasks` 创建任务、`/api/coredump/...`、`/api/charter/...` 等 HTTP 端点。**事实上"REST 调用方"已经能触发 kedo**——只是没有 skill 标准化包装、没有调用方友好的 schema 文档、dashboard 阻塞性人工干预没有"远程代理"通道。

最小可行版（MVP）：把现有 HTTP 端点按 skill 规范打个壳（如 Claude Code Skill manifest + script wrapper），调用方直接拿 skill 包就能 `kedo:build_switch_project requirement="..."`。

### 待回答的设计问题

- [ ] 选哪个 skill 协议作为首发？（Claude Code skill 规范 vs MCP server 模式 vs 自定义 + 适配层）
- [ ] 是否需要"远程模式"——kedo daemon 跑一台 server，多个客户端连？
- [ ] 调用方需要"流式事件"（看 LLM token / 工具进展）还是只要最终结果？事件 schema 怎么设计跨工具通用？
- [ ] dashboard 那种"等用户填 ftpd-pro IP"的阻塞，怎么映射到调用方？是 skill 调用反向阻塞调用方，还是 kedo 单独弹个 web 等用户、调用方异步轮询？
- [ ] 安全 / 权限边界——调用方能不能让 kedo 跑任意 shell 命令？charter 在 skill 模式下怎么生效？

---

## 想法 B：kedo 内部调用 skill

### 用户场景

- 用户从 Claude Code skill marketplace 拿一个"OCR PDF"的 skill，让 kedo 用它解析项目里的 datasheet（嵌入式开发常需要查 chip datasheet）。
- 团队内部把"部署到 Switch"的 nxlink 流程封装成 skill，几个项目共享。
- 用户在 kedo 里 `/skill add <package>`，下一次 task 跑时 ReactAgent 在工具列表里看到这个 skill，按需调用。

### 核心问题

| 维度 | 问题 |
|---|---|
| **加载机制** | 启动时全加载 vs 项目级（charter 里声明）vs 任务级（用户 prompt 里声明）？前者污染所有 task 的工具列表，后两者更精确 |
| **执行隔离** | skill 一般是脚本/容器，kedo 在哪个进程跑它？子进程 + sandbox？容器？host fork？kedo 自家的 ShellTool 已经有沙盒化（stdin DEVNULL / 拦提权），skill runner 沿用还是另起一套？ |
| **schema 转换** | skill 的描述格式（Claude Code skill = SKILL.md frontmatter + script、MCP = tool descriptor JSON）怎么转成 ReactAgent 用的 `tools/base.py:ToolParameter` schema？ |
| **结果消费** | skill 的输出多半是文本/JSON。kedo ReactAgent 习惯结构化 ToolResult（success/output/data），需要 adapter 把 skill stdout 拼成 ToolResult |
| **成本控制** | LLM 看到的工具列表越长，每轮 prompt 越大。50+ skill 加载会显著推高 token 成本——要不要 lazy-discover（先描述、用时再加载）？ |
| **trust model** | 用户安装一个 skill 等于授权它在 kedo 进程上下文跑代码。要不要权限分级？签名校验？运行时审计？ |

### 与现状的关系

kedo 内部工具系统（`tools/base.py:BaseTool`、`tool_registry`）已经有"动态注册"的雏形——`api/server.py:create_app` 里 `tool_registry.register(...)` 一连串调用就是。把 skill 当成"另一种 BaseTool 子类"是合理切入点：写一个 `SkillAdapterTool`，构造时吃 skill 描述，`execute()` 走子进程。

最小可行版：先支持单一 skill 协议（譬如 Claude Code skill），定义 SkillAdapter 子类，提供 `kedo skill install <path>` 命令把 skill 写进 charter 或 project profile，启动时自动注册到 tool_registry。

### 待回答的设计问题

- [ ] 哪个 skill 协议先落地？（Claude Code skill vs MCP tool descriptor vs 兼容多种）
- [ ] skill 在 charter 里声明（项目级），还是用户全局配置（账号级）？还是任务级（每条 task 描述时附带）？
- [ ] skill 的 stdout/stderr 怎么转 ToolResult？要不要约定 skill 输出 JSON 的格式（如 `{"success": bool, "data": {...}, "log": "..."}`）？
- [ ] skill 出错时是直接 ToolResult.error 还是 escalate 到 reviewer / pause_for_human？
- [ ] skill 能不能也是 LLM-driven 的（调用 skill 的内部又是另一个 LLM agent）？嵌套深度上限？

---

---

## 想法 C：协同进化 — Claude Code 帮 kedo 长能力

### 用户场景

- 用户在 Claude Code 里调 kedo 跑 switchvideo 任务，kedo 在某个工具上卡死（比如 `fetch_crash_report` 选错了 log）。Claude Code 看到 kedo 的失败响应 + Claude Code 又能直接读 `tools/fetch_crash_report_tool.py` 的源码 → Claude Code 写一个 patch 改 kedo 自己 → kedo 装上 patch 后重试，一次过。
- 用户在 Claude Code 里说"kedo 缺 Yocto 项目类型支持，帮它加"。Claude Code 把 kedo 当成"可被改造的目标项目"，自然走它擅长的 src-edit / test-runner 路径，给 kedo 添新的 ProjectProfile 类型 + 平台 hints。
- **本质：kedo 既是 IDE 的工具，也是 IDE 的"被开发对象"**。两个角色同时存在。

### 为什么这是 A 的副产品

A 让外部 agent 能调 kedo。如果"调用方有能力 read/write kedo 自己的源码"（Claude Code 在 kedo 仓库里启动时天然就有），那么调用方在每次调 kedo 时**两个角色叠加**：

- 调用 kedo 完成正在做的任务（业务用户）
- 同时观察 kedo 的失败模式 / 能力空缺，决定要不要改 kedo（kedo 维护者）

A 提供了"调用 kedo"的通道；C 利用的是"调用方碰巧也有 kedo 源码访问权"这个事实——不需要新协议，只是工作流上的额外约定。

### 与现状的关系

kedo 项目本身就在被 Claude Code 维护（这次会话即如此）。C 把这个"暗工作流"显式化：

- **现在**：用户找 Claude Code，描述 kedo 在哪个 task 失败，Claude Code 改 kedo，用户手动 scp + 重启 daemon 重跑
- **C 想要**：kedo 调用失败时，调用方（Claude Code）**直接拿到结构化失败语境**（哪个工具、什么参数、stderr）→ 自动诊断 → 拉 kedo 源码 → 提 patch（默认进 PR / 待审）→ 用户审一下 → kedo 自动重启自洽

中间最大的工程跳跃在**第二步**："结构化失败语境怎么传达"，以及**第四步**："kedo 自己的 reviewer / charter 怎么治理 kedo 自己的代码改动"。

### 核心问题

| 维度 | 问题 |
|---|---|
| **失败语境的颗粒度** | 调用方收到 `ToolResult(success=False, error="...")` 是否够？还是要把 task 全部 message history、相关源文件路径、现场 git diff 也打包送过去？数据量 vs 调试有效性的取舍 |
| **patch 应用模型** | (a) 直接落 working tree（暴力）；(b) 自动开 branch + PR + 等用户 review；(c) 进 charter 的"propose_patch"流程，让 kedo 自己的 reviewer agent 评估外来 patch；(d) 只生成 patch 文件让用户手动 apply。**c 是最 self-consistent 的设计**——把 kedo 治理 switchvideo 那套 charter+reviewer 反过来用在 kedo 自身上 |
| **递归深度** | Claude Code 改完 kedo 的 patch 跑了 → 还是失败 → Claude Code 再读源码再改 → 无限循环？需要"single-task patch budget"或人工 checkpoint |
| **trust** | 一个 Claude Code 实例（甚至跨用户的 Claude Code 实例）能在 kedo 仓库自由 commit 是否合理？最小权限：(a) 只能在 sandbox branch；(b) 必须经 GitHub PR；(c) merge 必须人工 |
| **kedo 自家 charter 写谁** | kedo 仓库要不要有 charter，约束"哪些文件外来 patch 不许动"（如 charter / reviewer / state_manager 这些治理核心）？ |
| **回滚** | patch 应用后 kedo 失败更严重怎么办？daemon 启动检查 + 自动 git revert 上一次"协同进化"commit？ |

### 与"想法 A"的协议层耦合

如果走 c 方案（kedo 把外来 patch 当 PR 走 reviewer + charter）：

- A 的协议层不仅要返回 task 结果，还要支持 **"channel 反向"**——调用方推 patch 给 kedo
- 这等于 kedo 的 skill manifest 里多一个 endpoint：`POST /api/coevolve/propose_patch`，接调用方给的 unified diff + 失败语境
- patch endpoint 的处理流程：
  1. 写到 sandbox branch
  2. 拿 patch + 当前失败语境喂 kedo 的 reviewer
  3. reviewer pass → 自动跑 tests / smoke / 重启 daemon 验证
  4. validation pass → 升级到 main（或开 PR 等用户）
- 这是把方案 C（双 Agent 对抗）从"业务代码评审"扩展到"kedo 自身代码评审"

### 风险与边界

最大的风险**不是技术**，是**反馈环路失控**：

- Claude Code 不擅长 kedo 业务但擅长改 Python，可能反复"治标"——见到 KeyError 就 catch 而不修上游问题，把 kedo 越改越脆弱
- kedo 的"自我治理"如果靠 kedo 自己的 reviewer，那是同一颗 LLM 同一份训练数据——可能盖章通过有问题的 patch
- **缓解**：C 必须强制要求"非同 provider 的 reviewer"对外来 patch 评审；这天然契合方案 C 现有"Producer/Reviewer 不同 provider"原则

不应该做的：

- ❌ **kedo 自己改自己同时还在跑 task**——必须把"协同进化"作为 daemon 重启时机的特殊 phase，不能 hot-patch 运行中的 ReactAgent
- ❌ **绕过 GitHub 直接 commit 到 main**——即便所有 reviewer 都 pass，最终的 main commit 也应保留人工 hook
- ❌ **把 kedo 的核心治理代码（charter / reviewer / state_manager）开放给协同进化**——这些必须有 charter:forbidden_files 守住

### 待回答的设计问题

- [ ] "结构化失败语境"的 schema：失败时调用方拿到的最小 + 最大信息分别是什么？
- [ ] patch 治理的归属——走 GitHub PR（沿用现有 ultrareview 工作流）还是 kedo 内部 reviewer 评估？两者并行可不可以（先 reviewer 后 PR）？
- [ ] 协同进化的"调用方"是谁——只允许 Claude Code，还是任何走 A 协议的客户端？前者更安全后者更通用
- [ ] kedo 仓库本身需要哪些 charter 字段（特别是 forbidden_files）？
- [ ] 失败 → 协同改 → 重试这条循环的 budget 怎么定？(time / patch count / token)

---

## A、B、C 之间的关系

**A 与 B 共享**："工具协议适配层"

- A 是"把内部 BaseTool 暴露成外部协议（skill / MCP）"
- B 是"把外部协议（skill / MCP）适配成内部 BaseTool"

如果先把"协议↔BaseTool"的双向 adapter 做好，A 和 B 都是 thin wrapper。这暗示**应该先讨论协议选型**，再决定先做 A 还是 B。

**C 依赖 A**：C 是 A 的 use case 增强，不是独立通道。但 C 引入的"反向 patch 通道"+ "kedo 治理 kedo 自己代码"两个新轴，需要 A 的协议层从一开始就预留 patch endpoint 的扩展点。

```
       想法 A (output protocol)
       ┌──────────────────────────┐
       │  外部 → 调 kedo 跑业务   │
       └────────┬─────────────────┘
                │
                │ A 实现后才能谈 C
                ▼
       想法 C (feedback loop)
       ┌──────────────────────────┐
       │ 外部 → 借调用语境改 kedo │
       └──────────────────────────┘

       想法 B (input protocol)        ← 与 A/C 正交
       ┌──────────────────────────┐
       │ kedo → 调外部 skill      │
       └──────────────────────────┘
```

候选协议横向对比（待补全）：

| 协议 | 客户端生态 | 服务端生态 | kedo 适配难度 | 备注 |
|---|---|---|---|---|
| **MCP (Model Context Protocol)** | Anthropic SDK / Cursor / Continue 等 | 开放，多个实现 | 中——需要长连接 | 标准化最完整 |
| **Claude Code Skill** | Claude Code | 自定义 | 低——文本协议 | 但客户端只有 Claude Code |
| **OpenAI function-call** | OpenAI / 兼容 SDK | 自定义 | 极低——HTTP+JSON | 单方向（function 是给 LLM 看的，不暴露给其他 agent） |
| **自定义 REST + OpenAPI** | 任何 HTTP 客户端 | kedo 自家 | 已部分实现 | 缺 skill 概念上的"自描述能力" |

---

## 下一步建议

- [ ] **第一步**：选定"协议适配层"——一份 spec 决定 A 与 B 共享什么。建议先 deep-dive MCP，因为它是双向（kedo 既能当 server 也能当 client），一举两得。
- [ ] **第二步（A 方向）**：把现有 `POST /tasks` + 关键 GET 端点按选定协议包一层 manifest，做一个 hello-world skill 让 Claude Code 调通
- [ ] **第二步（B 方向）**：写 `tools/skill_adapter.py`，吃一份 SKILL.md 风格的描述，实现 `BaseTool` 接口；`kedo skill install` 命令
- [ ] **第三步（C 方向，紧跟 A）**：在 A 的 manifest 里预留 `propose_patch` endpoint 的扩展点；写 kedo 自家的 charter（先把 forbidden_files 列好）；定义"结构化失败语境" schema
- [ ] **第四步**：在 switchvideo 项目里做一个真用例——A 方向：让 Claude Code 调 kedo 的 build_switch_project skill；B 方向：让 kedo 调一个 "switch-toolchain-info" skill；C 方向：故意让 kedo 在某个工具上失败，看 Claude Code 能否自动诊断 + 提 patch + 走 reviewer

## 暂时不做的事（避免提前优化）

- ❌ 不要先做远程多租户 / SaaS 化——单机 + 单用户的 skill 暴露已经覆盖 80% 实战需求
- ❌ 不要在协议未选定前写大量 adapter——会浪费在错的协议上
- ❌ 不要把 charter / reviewer 也做成"skill 可注入"——这是 kedo 核心治理层，不应被外部 skill 替换
- ❌ 不要为了"想法 A 兼容性"放弃 dashboard——kedo 的 dashboard 是核心 UX 资产，应当**追加**支持远程调用，而非替换为外部 IDE 渲染
- ❌ **C 方向**：不要让协同进化跳过人工 hook 直接 merge 到 main——即便 reviewer 都过，最后一道闸必须留给人
- ❌ **C 方向**：不要 hot-patch 运行中的 kedo daemon——patch 必须落到磁盘 + 重启 daemon 才生效
