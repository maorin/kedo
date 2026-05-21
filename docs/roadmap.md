# kedo Roadmap

> **状态**：滚动更新的工作文档，不是承诺。最后更新 **2026-05-21**（本地 ds4 provider 基础落地）。
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
2. ⏸ Browser Bridge **M3.5**（测试用例自动执行）— 暂搁置，重启时直接做路径 B `run_test_cases` 专用工具
3. 🔄 Browser Bridge **M4** 局部完成（基础设施齐 + browser_research 路径 A 实战通了；真隔离 chrome 启动 Linux Google Chrome 阻塞，待真需求来时迁 Playwright）
4. ✅ 对接本地 **ds4** 推理引擎（DeepSeek V4 Flash）作为新 LLM provider — 基础落地 2026-05-21，待 switchvideo 实战验证长 context
5. 主 dashboard 整合 Inbox 工作流（避免独立 URL）
6. 探索 **kedo 作为 skill** 暴露（让 Claude Code / Cursor 远程调用）

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
| LLM Provider | Kimi/Claude/DeepSeek/OpenAI/**ds4** ✅ | switchvideo 实战验证 ds4 长 context + quirk-mitigation 抽象层 | `llm-providers.md` |
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

### M3.5 — 测试用例自动执行（⏸ 暂搁置，设计文档已落地）

**详细设计：** [`deep-dives/m3.5-test-execution-design.md`](deep-dives/m3.5-test-execution-design.md)（2026-05-11 写完）— 5 个设计点 + v1/v2/v3 演化路径 + 4 个待决定点。

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

**当前进度（2026-05-10 暂搁置）：** 路径 A 试到一半（task `08079a68`）。撞到的具体情况：
- agent 14 个 md 全读取后 plan_development 出 6 子任务，但具体执行步骤进度慢、context 很快塞满
- 14 模块 × 平均 5 TC ≈ 70 个 TC，单 task 跑完成本太高
- 密码硬规则 + 收敛检测误伤 + LLM 走捷径多重叠加，纯 prompt 驱动不稳

**重启时建议直接走路径 B**：写专用 `run_test_cases(md_file, tc_id="all")` 工具：
- markdown TC 章节解析 (regex `^## TC-[A-Z]+-\d+`)
- 步骤 → 浏览器动作映射（点击【X】按钮 → click text_match=X / 输入"Y" → type value=Y）
- 预期结果 → query 验证 + 截图比对
- per-case PASS/FAIL + screenshot on fail + 汇总 results.json
- isolation：一 TC 失败不影响下一个（重置到登录态 dashboard）

工作量 1-2 天，重启时再开。

**估时：** 路径 B 重启时 1-2 天

### M4 — 隔离 profile + `browser_research`（🔄 局部完成 2026-05-10）

**已交付（基础设施）：**
- `core/browser_profile.py` `IsolatedBrowserProfile` 完整实现：spawn chrome + 独立 user-data-dir + 写 kedo-config.json + 30min 闲置自动关 + 孤儿 chrome 清理
- 双 token：`~/.config/kedo/browser_token` (user) + `~/.config/kedo/browser_token_agent` (agent)
- BrowserBridge `_role_for_token` server-authoritative role 判定
- 插件 v0.4.1：getConfig 双路径（kedo-config.json fallback chrome.storage.local）+ cs_loaded SW 唤醒
- 协议升 1.3
- `browser_research(query, max_pages, search_engine)` 高层工具
- 默认 allowlist 预填 duckduckgo / github / stackoverflow / wikipedia / mozilla 减少弹窗

**实战遇阻（Linux Google Chrome 137+）：**
- `--load-extension` 被 Chrome policy 静默忽略（"is not allowed in Google Chrome, ignoring."）
- `--disable-extensions-except` 配对也被拒
- 没有任何命令行 flag 能从 Google Chrome 借出 dev mode
- → 隔离 chrome 启动后插件不加载，agent ws session 永远连不上

**当前实战路径（路径 A 自动 fallback）：**
- `BrowserResearchTool` 优先尝试 isolated profile，失败时**自动降级 prefer_role="user"**
- 走主浏览器 session + M3 Tier-1 权限层（首次访问域名要点 Trust）
- audit log 全程记录、allowlist 持久化
- 实测在 192.168.1.162 跑通 `browser_research("libnx audoutInitialize -19 error")`

**真正"隔离 chrome 启动"待解（任一即可）：**
- 装 Chromium / Brave / Edge 二进制（无 Google policy 限制），改 `_find_chrome_binary` 优先级
- 装 Chrome enterprise policy（需 sudo 写 `/etc/opt/chrome/policies/managed/*.json`）
- 迁 Playwright（自带 chromium 无限制，~1 天重构 + 150MB 依赖）

**当前结论：**
- M4 设计的"isolation"对 read-only research 不是硬需求 → 路径 A 已够用
- 真正硬需"isolation"的场景（自动化测试 / 专用账号代理）尚未上路线图
- 那些场景上来时再认真投资 Playwright 或 Brave，**而不是**继续在 Google Chrome 上抠 flag

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

## 本地 ds4 Provider 对接（✅ 基础落地 2026-05-21）

**项目：** [antirez/ds4](https://github.com/antirez/ds4) — DwarfStar 4，专为 DeepSeek V4 Flash 写的本地推理引擎，本地路径 `/Users/maojj/project/ds4`。

**已交付：**
- `api/server.py` 加 `Ds4Client(OpenAIClient)`：固定 api_key="local"、默认 base_url `http://127.0.0.1:8001/v1`、默认 model `deepseek-v4-flash`、`validate()` 走 `GET /v1/models` 探活并校验 model id 在列
- `create_llm_client` + `create_reviewer_llm_client` 两个 factory 加 ds4 分支
- `/llm/switch` + `/llm/validate` + `_persist_llm_config` + `switch_reviewer` 加 ds4 分支（持久化 `ds4_base_url`，需要重启的还是 reviewer）
- CLI `/login` 主流程 + reviewer 子流程加 ds4 选项（跳过 API Key 输入，改问 base_url）
- `kedo --provider` 帮助文本加 ds4

**端到端实测（M3 Max 128GB / ds4-server --port 8001 + 32K context）：**
- 启动后 `validate()` 通；`chat([{user:'回一个字'}])` → `'ping'`
- `chat_with_tools` 跑一次 `get_weather('上海')`：拿到 `ToolCallData(name='get_weather', arguments={'city':'上海'})` + `reasoning_content="用户想知道..."`
- 现有 `OpenAIClient.chat_with_tools` 的 `reasoning_content` 透传逻辑（commit history 中 deepseek-v4-pro 那条）直接复用

**待做：**
- 在 switchvideo 实战里跑一轮 ReactAgent，看 1M context + 落盘 KV cache（`--kv-disk-dir`）能不能撑长任务
- 启动 ds4-server 时加 `--ctx 1048576 --kv-disk-dir ~/.cache/ds4-kv` 验证长 context 在 kedo 流程里的实际表现
- ds4-server 用全局 lockfile（`ds4` 和 `ds4-server` 同名进程互斥），实战里要注意冲突

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
⏸ Parked (2026-05-10):    M3.5 (测试执行) — 路径 A 实战不稳，待重启时走路径 B (run_test_cases 工具)
Week 1-2:   M4 (隔离 profile + browser_research) 或 主 dashboard 整合 Inbox（看哪个更紧）
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
