# kedo Roadmap

> **状态**：滚动更新的工作文档，不是承诺。最后更新 **2026-05-09**（M3 + 双 Agent 实战完成，进 M3.5）。
>
> 本文是"现在做什么、下一步做什么、为什么"的清单。架构层面的"为什么"详见 `deep-dives/`，里程碑实现细节看对应 design 文档与 commit。
>
> 维护原则：每个里程碑落地或调整时，本文同步更新；新增 workstream 在表里加一行，detail 段在下面追写。

---

## TL;DR — 当前形势

**主线**已稳定的能力：
- ReactAgent 单轨架构（替代旧刚性流水线）
- Reviewer Agent + commit candidate gate（双 Agent 对抗）
- 虚拟测试 T1/T2 + 真机 coredump 抓取（T3 模拟器搁置）
- Browser Bridge M1/M2（用户喂网页 + Agent 只读控制浏览器）
- LLM 多提供商热切换（Kimi / Anthropic / DeepSeek / OpenAI / Ollama / mock）

**下一周期**重点（按优先级）：
1. ✅ Browser Bridge **M3** + 双 Agent 实战通了（2026-05-09）
2. ⏳ Browser Bridge **M3.5**（测试用例自动执行）— 用 ReactAgent prompt 驱动跑 14 模块 TC，验证可行性后决定要不要做专用工具
3. ⏳ Browser Bridge **M4**（隔离 profile + `browser_research` 高层工具）
4. 主 dashboard 整合 Inbox 工作流（避免独立 URL）
5. 探索 **kedo 作为 skill** 暴露（让 Claude Code / Cursor 远程调用）

---

## Workstream 总览

| Workstream | 现状 | 下一步 | 关联设计 |
|---|---|---|---|
| 主 Agent Loop | ReactAgent 单轨 ✅ | Hierarchical Agent (③→④) | `deep-dives/agent-workflow-hybrid.md` |
| 多 Agent 协同 | Reviewer 二审 ✅ | Sub-agent as Tool / Orchestrator-Worker | `deep-dives/multi-agent-architecture.md` |
| Browser Bridge | M1+M2+M3 + 双 Agent 实战 ✅ | M3.5 测试执行 → M4 隔离 profile | `deep-dives/browser-bridge-design.md` |
| Virtual Test | T1/T2 ✅、T3→真机 coredump ✅ | 提取 charter 测试用例自动化 | `virtual-test-strategy.md` |
| Charter 治理 | propose_charter_change 工具 ✅ | charter diff UI、版本回退 | (待写 deep-dive) |
| 上下文管理 | memory + react_agent 收敛检测 ✅ | long-horizon 摘要器 | `deep-dives/context-management.md`、`long-horizon-memory.md` |
| Self-evaluation | EvaluateTool + Reviewer ✅ | 多维度对照 dimension drift | `deep-dives/self-evaluation.md` |
| 工具脆弱性 | profile_guard + auto_fix ✅ | tool 调用幂等性 + retry 策略 | `deep-dives/tool-fragility.md` |
| LLM Provider | Kimi/Claude/DeepSeek/OpenAI ✅ | quirk-mitigation 抽象层 | `llm-providers.md` |
| **Skill 暴露** | 探讨稿 | MCP / output protocol | `deep-dives/kedo-as-skill-and-skill-host.md` |

---

## Browser Bridge — 详细路线

完整设计：`deep-dives/browser-bridge-design.md`。协议契约：[`kedo-browser-bridge/PROTOCOL.md`](https://github.com/maorin/kedo-browser-bridge/blob/main/PROTOCOL.md)。

### M1 — 通道 + Inbox（✅ 完成 2026-05-08）

- WS 端点 `/api/ws/browser`、token 鉴权（`~/.config/kedo/browser_token`）
- ContextInbox 持久化 `~/.config/kedo/inbox/`
- 用户喂网页 → Inbox 卡片 → Start task 自动 attach 引用块
- 实战验证：1.8 上 google.com → inbox.html 全链路通

### M2 — Agent 只读控制（✅ 完成 2026-05-08）

- 6 个 ReactAgent 工具：`browser_list_tabs / navigate / screenshot / extract / query / wait_for`
- 协议升级到 1.1（向后兼容 1.0）
- Selector 三元定位（CSS + text + aria_label）
- 实战验证：1.8 上 ReactAgent 调 list_tabs 列出用户浏览器 tab

### M3 — Agent 写控制 + Tier-2 权限（✅ 完成 2026-05-09，含双 Agent 二审）

**已交付：**
- 5 个新工具：`browser_click` / `browser_type` / `browser_submit` / `browser_scroll` + 辅助 `browser_get_active_tab`
- Tier 模型 (`core/browser_permissions.py`)：T0 read 自动 / T1 nav 白名单 / T2 write 30min 信任窗口 / T3 高危拒
- T2 触发 dashboard 弹窗（4 选项：Deny / Allow once / Allow 30min / Trust this domain）
- 持久化白名单 `~/.config/kedo/browser_permissions.json`（默认含 localhost / 127.0.0.1 / *.devkitpro.org）
- 审计日志 `~/.kedo/browser-audit.jsonl`（每次决策一行 JSON）
- 硬规则：协议 (chrome:// / file://) / 密码字段 / 信用卡字段 client-side 强制拒
- 协议升 1.2，向后兼容 1.0/1.1
- 插件 v0.3.0

**实战验证（2026-05-09 在 10.168.2.4）：**
- ✅ 浏览器写控制全链路：navigate www.google.com → 弹窗 → Trust this domain → screenshot 出图 → audit log
- ✅ 双 Agent 二审：commit_candidate → DeepSeek (deepseek-v4-pro) score=91.5 / 92.0 / 96.2 多次通过
- ✅ 自动生成 14 模块测试用例文档（76KB）
- ✅ 期间修了 4 个 bug：tab_id=0 / openai 必需 dep / api_key leak / logger INFO（commits `7dedae4` `78711ad` `2619e5e` `ee84d3e`）

### M3.5 — 测试用例自动执行（⏳ 进行中 2026-05-09）

**用例：** 把 M3 写出的测试用例 markdown 让 ReactAgent **自动执行** —— 不只生成文档而是真跑测试。

**已具备能力：** browser_navigate / click / type / query / screenshot / wait_for + Reviewer 二审。

**实战发现的限制：**
- 撞 M3 密码字段硬规则 → TC-LOGIN-* 类用例本质上不能自动跑（设计如此），**需跳过**或走 M5 credentials vault
- 收敛检测误伤探索类任务（同 selector 反复 ELEMENT_NOT_FOUND 触发 stop）—— 见 `deep-dives/tool-fragility.md` 后续
- doc-only 任务里 commit_candidate 跳过 → Reviewer 不参与质量把关
- LLM "走捷径"倾向：浏览少 + 经验补内容多，框架对、细节虚

**短期补的能力（无重大架构改动，可现做）：**
- 测试用例 markdown 解析器（TC-XX-NNN 步骤数组化）
- 断言机制（"预期结果"自动验证）
- per-case pass/fail 报告 + 失败截图
- 一个 case 失败不影响下一个的 isolation 机制

**两条路：**
- **路径 A**：纯 ReactAgent prompt 驱动（task 描述里说"按 TC 一条条跑"）。最快验证，不写代码。
- **路径 B**：加专用 `run_test_cases(md_file, tc_id)` 工具。300 行 Python，1-2 天，更稳。

**当前进度：** 路径 A 在试，task `08079a68` plan_development 已生成 6 子任务，正在读 14 个 md。

**估时：** 路径 A 验证 1-2 天；如果走路径 B 加 2-3 天

### M4 — 隔离 profile + `browser_research`（⏳）

**范围：**
- `core/browser_profile.py` 启动 headed Chrome 实例（独立 user-data-dir）
- 同插件 role=agent 模式（启动时通过 EXTENSION_DIR/kedo-config.json 指定）
- 高层工具 `browser_research(query, max_pages=3)` 内部展开为多步 navigate + extract
- BrowserBridge 区分 user_session / agent_session，工具 `prefer_role` 自动路由
- 空闲 30 分钟自动关闭隔离 chrome

**用例：** ReactAgent build 失败 → 自调 `browser_research("libnx audoutInitialize -19")` → 从 stackoverflow / wiki 取 3 篇相关问答 → 改对代码

**估时：** 5-7 天

### M5 — 跨浏览器 + 录制回放（探索）

- Firefox / Edge 兼容（MV3 大体一致，alarms / sidepanel API 略有差异）
- 操作录制 → 回放成 charter 测试用例
- 上 Chrome Web Store（自用阶段不需要，M3 后再评估）

---

## 主 Dashboard 整合 Inbox（M2.5）

**问题：** 当前 Inbox 在独立 URL `/dashboard/inbox.html`，用户得记一个额外路径；主 dashboard 只在 header 显示徽章 + 链接。

**目标：**
- 主 dashboard 增加"Inbox"侧边面板或 view-switcher 选项
- 选中条目可直接 drag 到 task description 框
- Inbox 条目和正在跑的 task 关联可视化（哪个 task 用了哪些条目）

**估时：** 2-3 天，可与 M3/M4 并行

---

## Skill 暴露（探索阶段）

**详见：** `deep-dives/kedo-as-skill-and-skill-host.md`

三个相关想法：
- **A** kedo 对外暴露 skill（让 Claude Code / Cursor 远程调用）
- **B** kedo 内部消费第三方 skill 包（替代/补充内置 `tools/*.py`）
- **C** A 实现后，调用方反过来读 kedo 源码、提 patch（协同进化）

**当前状态：** 仅讨论稿。实施前需先决定：
- 协议（MCP / Anthropic Output Protocol / 自定义 HTTP）
- 暴露粒度（黑盒 task / 白盒原子能力 / 两层都给）
- 状态语义（同步 / 异步 + task_id 轮询）

**优先级：** Browser Bridge M3/M4 之后再认真投入。

---

## 长期 / 探索

不在近期 sprint 里，但是值得持续关注：

| 主题 | 备注 |
|---|---|
| Hierarchical Agent (Plan-as-Contract → Hierarchical) | 单 ReactAgent 工具数已 26 个，prompt 膨胀风险；按子角色拆分 sub-agent |
| 长 horizon 摘要器 | 30+ turn 后早期 charter / 决策被稀释，需主动摘要 + 重注入 |
| 工具调用幂等 + retry | tool failures 当前靠 LLM 自己重试；可加幂等键 + bounded retry |
| Charter diff UI + 回退 | 当前 charter 修改通过 propose_charter_change 工具，但版本管理薄弱 |
| Quirk-mitigation 抽象层 | 现 Kimi/Claude/DeepSeek 各自 fallback 散落代码，可抽象成 strategy |

---

## 时间线 (近 30 天估计)

```
✅ Done (2026-05-08~09):  M1 (通道) + M2 (只读) + M3 (写+权限+双Agent) + 14 模块测试文档自动生成
Week 1:     Browser Bridge M3.5 (测试执行) — 路径 A (ReactAgent prompt) 验证 → 必要时路径 B 加 run_test_cases 工具
Week 2:     Browser Bridge M3 (写控制 + Tier-2 权限) [legacy entry, ignore]
Week 3:     Browser Bridge M4 (隔离 profile + browser_research)
Week 4:     主 dashboard 整合 Inbox + 收尾打磨
            或：进入 Skill 暴露 PoC
```

并行：
- Long-horizon 摘要器（持续小修补）
- Charter UI（按需）

---

## 不做 / 暂缓清单

明确先不做的：

- **Browser Bridge T3 高危**（任意 JS 执行 / 跨 origin / 文件上传） —— 设计已禁，配置默认 OFF，不暴露给 LLM
- **Chrome Web Store 上架** —— 自用阶段 Load unpacked 即可
- **Switch 模拟器集成（T3 原计划）** —— 已搁置，改为真机 coredump 抓取替代
- **AgentLoop 旧实现迁移** —— P3-M3 已退役到 `core/_legacy/`，4-6 周后再评估真删

---

## 维护说明

- 完成里程碑：把对应 ⏳ 改 ✅、加 commit hash + 日期、补"实战验证"一句
- 发现新工作：在合适 workstream 表行下追加；如果是新 workstream，加表头新行 + 下面 detail 段
- 优先级换序：在 TL;DR 重排，并在变更原因写一句话
- 每次更新顶部"最后更新"日期跟着改

写过的、放弃的、转向的，都留下来——roadmap 的价值之一是看到决策轨迹。
