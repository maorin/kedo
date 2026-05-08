# kedo Roadmap

> **状态**：滚动更新的工作文档，不是承诺。最后更新 **2026-05-08**。
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
1. Browser Bridge **M3**（Agent 写控制 + Tier-2 权限）
2. Browser Bridge **M4**（隔离 profile + `browser_research` 高层工具）
3. 主 dashboard 整合 Inbox 工作流（避免独立 URL）
4. 探索 **kedo 作为 skill** 暴露（让 Claude Code / Cursor 远程调用）

---

## Workstream 总览

| Workstream | 现状 | 下一步 | 关联设计 |
|---|---|---|---|
| 主 Agent Loop | ReactAgent 单轨 ✅ | Hierarchical Agent (③→④) | `deep-dives/agent-workflow-hybrid.md` |
| 多 Agent 协同 | Reviewer 二审 ✅ | Sub-agent as Tool / Orchestrator-Worker | `deep-dives/multi-agent-architecture.md` |
| Browser Bridge | M1+M2 ✅ | M3 写控制 + M4 隔离 profile | `deep-dives/browser-bridge-design.md` |
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

### M3 — Agent 写控制 + Tier-2 权限（⏳ 下一步）

**范围：**
- 新工具：`browser_click`、`browser_type`、`browser_submit`、`browser_scroll`
- Tier 权限模型落地（Tier 0/1/2/3 详见 design §8）
- T2 写操作触发 dashboard 弹窗确认（用户可"信任此域名 30 分钟"）
- 域名白名单文件 `~/.config/kedo/browser_allowlist.txt`
- 审计日志 `~/.kedo/browser-audit.jsonl`
- 硬规则：`type=password` / `autocomplete~="cc-"` 永远拒绝（已在 M2 query 层暴露 `is_password_field`，M3 强制执行）

**风险点：**
- Selector 漂移 → 三元定位 + matched_strategy 反馈给 LLM 学习
- 静默写操作误用用户登录态（GitHub / Slack） → T2 默认每次确认是底线

**估时：** 5-7 天

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
Week 1-2:   Browser Bridge M3 (写控制 + Tier-2 权限)
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
