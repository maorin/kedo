# Browser Bridge — kedo 浏览器插件技术方案

> **状态**：草稿，2026-05-08 立项。讨论稿性质，分期落地，**不是最终 spec**。
> **关联文档**：
> - `agent-workflow-hybrid.md` — 自主 Agent 与 Workflow 的混合（浏览器工具属于自主 Agent 能力扩展）
> - `tool-fragility.md` — 工具脆弱性（浏览器工具会引入新的 selector 漂移、登录态、网络超时等脆弱点）
> - `multi-agent-architecture.md` — Agent 控制浏览器是否需要专门的 BrowserAgent 子角色，留作 Phase 后讨论

## TL;DR

给 kedo 加一个浏览器插件 + 后端 Bridge，让 ReactAgent 能：

1. **A. 操作用户浏览器** — 自动化测试 / 办公自动化（点击、输入、抽 DOM、截图）
2. **B. 接收用户喂网页** — 用户在任意页面一键发"上下文条目"到 kedo（**不直接创 task，进上下文盒子**）
3. **C. 自己开浏览器查资料** — 走**独立 profile**（与用户主浏览器登录态隔离），只读

明确**不做**的事：反爬绕过、密码字段读取、隐藏页面执行、跨站请求伪造、剪贴板劫持。所有写操作必须在 dashboard 可见，并可被随时打断。

### 三个已确认的设计决定（2026-05-08）

| # | 决定 | 影响 |
|---|---|---|
| 1 | **插件单独 repo** | 协议跨 repo 必须稳定；版本兼容矩阵需管理；联调需要"kedo 后端运行 + 插件 load unpacked"双方在场 |
| 2 | **用户喂网页进"上下文盒子"，不直接创 task** | 后端新增 `ContextInbox` 概念；dashboard 新增 inbox 面板；用户从 inbox 组合条目再起 task |
| 3 | **Agent 查资料用独立 profile** | 后端能够启动 headed Chrome 实例（独立 user-data-dir）+ 同一插件；BrowserBridge 区分 `user_session` vs `agent_session`；用户登录态零污染 |

---

## 1. 目标与边界

### 1.1 用户场景

| 场景 | 描述 | 触发方 | 浏览器 session |
|---|---|---|---|
| A1 | Agent 验证生成的 web UI / dashboard | ReactAgent | 用户主浏览器 |
| A2 | Agent 跑 E2E 测试（点击/输入/断言） | ReactAgent | 用户主浏览器 |
| A3 | Agent 做办公自动化（填表、上传、提交工单） | ReactAgent | 用户主浏览器 |
| B  | 用户把当前网页喂给 kedo 当上下文（一键 → 进盒子） | 用户 | 用户主浏览器 |
| C  | Agent 自己查资料（搜文档、读 issue、看 stackoverflow） | ReactAgent | 独立隔离 profile |

A 和 B 共用**用户主浏览器**（用户登录着 GitHub、Slack 等，便利但敏感）；C 必须用**独立 profile**避免污染用户登录态、避免 agent 误操作影响用户的真实账号。

### 1.2 不做清单（硬边界）

- **不读密码字段**：`<input type="password">` / `autocomplete~="cc-"` 永远 skip
- **不动 chrome:// / file://**：协议级别拒绝
- **不执行任意 JS**：通过 `chrome.scripting` 注入任意代码这条路默认 OFF，只在配置文件里开关，不暴露给 LLM 工具调用
- **不穿透 cross-origin iframe**：避免被嵌入的第三方页面引诱
- **不静默写**：所有 T2 写操作（click/type）默认 dashboard 弹窗确认（用户可"信任此域名 30 分钟"豁免）
- **不做反爬反 bot 绕过**：CAPTCHA 命中就停下来叫人

呼应 `feedback_shell_tool_security` 的精神 —— 暴露给 LLM 的高权限工具一律最小权限 + 显式提权 + 可审计。

---

## 2. 整体架构

```
                        ┌─────────────────── 用户主浏览器 ───────────────────┐
                        │  Chrome MV3 插件                                    │
                        │  ├ service worker  ─── WS user_session ───┐        │
                        │  ├ content script  (DOM 操作)              │        │
                        │  └ popup/sidepanel ("Send to kedo")        │        │
                        └────────────────────────────────────────────┼────────┘
                                                                     │
                                                                     ↓
                ┌──────────────── kedo 后端 ──────────────────┐
                │  api/browser_bridge.py                       │
                │  ├ session manager(user_session/agent_session)│
                │  ├ command queue + Future 配对                │
                │  ├ ContextInbox(用户喂网页落点)               │
                │  └ permission gate(Tier 0/1/2/3)              │
                │            ↑↓ 工具调用                        │
                │  ReactAgent + browser_* 工具集                │
                └──────────────────────────────────────────────┘
                                       ↑
                                       │ WS agent_session
                                       │
                ┌──────────── kedo 启动的隔离 Chrome ──────────┐
                │  独立 user-data-dir (~/.kedo/browser-profile) │
                │  装同一个插件,headless=false                  │
                │  专跑场景 C(agent 查资料)                     │
                └──────────────────────────────────────────────┘
```

**关键复用：** WebSocket 走 `api/websocket.py` 同一 FastAPI 实例（新增 `/ws/browser`）；权限走 `core/permissions.py` PermissionManager；事件流走 dashboard 现有 events 通道。

---

## 3. Repo 拆分策略

### 3.1 仓库布局

```
kedo (现有 repo)
├── api/browser_bridge.py        # WebSocket 接入 + session 管理
├── api/context_inbox.py         # 上下文盒子（B 场景落点）
├── tools/browser_*.py           # ReactAgent 浏览器工具（7-9 个）
├── core/browser_profile.py      # 启动/管理隔离 Chrome 实例（C 场景）
└── docs/deep-dives/browser-bridge-design.md  # 本文档

kedo-browser-bridge (新 repo)
├── manifest.json                # MV3 manifest
├── src/
│   ├── service_worker.ts       # WS 长连接 + 心跳 + 命令分发
│   ├── content_script.ts       # DOM 操作执行端
│   ├── popup/                  # "Send to kedo" UI
│   ├── sidepanel/              # 当前 agent 活动 + 暂停按钮
│   └── lib/
│       ├── selector.ts         # ARIA + 文本双重定位
│       └── readability.ts      # Mozilla Readability fork
├── PROTOCOL.md                  # 与 kedo 后端的协议契约（版本化）
└── package.json
```

### 3.2 跨 repo 协议管理

**协议契约**写在 `kedo-browser-bridge/PROTOCOL.md`，在 kedo repo 软链接 / git submodule 或干脆在 kedo 测试里复制一份做兼容性测试。

**版本号策略：**
- 协议有独立 `PROTOCOL_VERSION`（如 `1.0`），插件和后端分别报告自己支持的版本
- 握手时双方协商：插件连接发 `{ "type": "hello", "client_version": "1.2", "supported_protocols": ["1.0", "1.1"] }`，后端回 `{ "type": "hello_ack", "negotiated_protocol": "1.1" }`
- **兼容矩阵**写在 `PROTOCOL.md` 顶部表格里，每次破坏性改动 bump major

**跨 repo CI 联调：**
- kedo-browser-bridge CI 跑单元测试 + 启 mock kedo 后端跑端到端
- kedo CI 跑后端单测；联调测试单独 workflow 拉两个 repo
- 发布时插件商店版本和 kedo backend 版本不必同步，但 PROTOCOL_VERSION 必须匹配

### 3.3 联调流程（开发期）

1. `git clone kedo-browser-bridge && pnpm install && pnpm dev`（webpack watch 输出 `dist/`）
2. Chrome → 扩展程序 → 开发者模式 → "加载已解压的扩展程序" → 选 `dist/`
3. `kedo` 后端正常启动；插件 popup 输入 token 连接
4. 改插件代码热重载；改后端代码 FastAPI 自动 reload；二者只通过 WS 协议通信，无源码耦合

---

## 4. 组件分解

### 4.1 浏览器插件（kedo-browser-bridge repo）

**Manifest V3 关键 permission：**

```json
{
  "manifest_version": 3,
  "permissions": ["tabs", "activeTab", "scripting", "storage", "alarms"],
  "host_permissions": ["<all_urls>"],
  "background": { "service_worker": "service_worker.js" },
  "content_scripts": [{ "matches": ["<all_urls>"], "js": ["content_script.js"] }],
  "action": { "default_popup": "popup.html" },
  "side_panel": { "default_path": "sidepanel.html" }
}
```

**关键技术点：**
- **Service worker 保活**：MV3 30 秒闲置即 sleep。用 `chrome.alarms.create("ping", { periodInMinutes: 0.4 })`（24 秒）触发 WS ping 重唤醒。断线指数退避重连（1s → 2s → 4s → 30s 上限）。
- **content script 与 service worker 通信**：所有 tab 共用一条 WS（service worker 持有），content script 通过 `chrome.runtime.sendMessage` 转发结果，避免 N tab × N 连接。
- **截图大小控制**：`chrome.tabs.captureVisibleTab` 默认 PNG 可能 >1MB。>200KB 时落到 `chrome.storage.local`（或本地文件需要 file system access API），WS 只回路径/句柄，agent 拿到后再按需 fetch。
- **Selector 三元组**：每次 query/click 同时接 `selector` (CSS) + `text` (innerText 子串) + `aria_label`，命中任一返回，并在 result 里报 `matched_strategy: "selector" | "text" | "aria"`。
- **Token 鉴权**：popup 首次连接需粘贴 token（kedo 启动时写到 `~/.config/kedo/browser_token`），存 `chrome.storage.local` 后续自动用。

### 4.2 kedo 后端 — BrowserBridge

新建 `api/browser_bridge.py`（约 250 行）：

```python
class BrowserSession:
    session_id: str
    role: Literal["user", "agent"]   # 用户主浏览器 / 隔离 profile
    ws: WebSocket
    last_heartbeat: float
    capabilities: dict               # 协议版本、可用 API、当前 tab 列表 cache

class BrowserBridge:
    def __init__(self):
        self._sessions: dict[str, BrowserSession] = {}
        self._pending: dict[str, asyncio.Future] = {}    # request_id → Future
        self._inbox: ContextInbox = ContextInbox()       # B 场景落点

    async def send_command(
        self,
        action: str,
        params: dict,
        prefer_role: Literal["user", "agent"] = "user",
        timeout: float = 30.0,
    ) -> dict:
        """Agent 工具侧调用。优先选 prefer_role 的 session。"""
        session = self._pick_session(prefer_role)
        if not session:
            raise NoBrowserSessionError(prefer_role)
        request_id = uuid4().hex
        fut = asyncio.get_event_loop().create_future()
        self._pending[request_id] = fut
        await session.ws.send_json({
            "type": "command",
            "id": request_id,
            "action": action,
            "params": params,
        })
        try:
            return await asyncio.wait_for(fut, timeout)
        finally:
            self._pending.pop(request_id, None)

    async def on_message(self, ws, raw: dict):
        """处理插件推上来的 hello / result / user_inject / heartbeat"""
        ...
```

**新建 `core/browser_profile.py`（约 150 行）— 启动/管理隔离 Chrome：**

```python
class IsolatedBrowserProfile:
    """Agent 查资料专用 Chrome 实例"""

    PROFILE_DIR = Path.home() / ".kedo" / "browser-profile"
    EXTENSION_DIR = Path.home() / ".kedo" / "browser-extension-pack"

    async def start(self) -> BrowserSession:
        """启动 headed Chrome,加载插件,等 ws 连接到 BrowserBridge"""
        cmd = [
            self._chrome_binary(),
            f"--user-data-dir={self.PROFILE_DIR}",
            f"--load-extension={self.EXTENSION_DIR}",
            "--no-first-run",
            "--no-default-browser-check",
            "--disable-background-timer-throttling",
            "about:blank",
        ]
        self._proc = await asyncio.create_subprocess_exec(*cmd, ...)
        # 插件启动后会自动连 ws,带 role=agent 标记
        return await self._wait_for_session(role="agent", timeout=15)
```

**插件区分 role 的方式：** 启动隔离实例时通过命令行参数注入 token + role hint（写到 EXTENSION_DIR 下一个 `kedo-config.json`），插件首次握手时读出来上报 `role: "agent"`；用户主浏览器装的插件没这个文件，默认 `role: "user"`。

### 4.3 ReactAgent 工具集（kedo repo）

按"读 → 写 → 高层"分档增加 7-9 个工具到 `tools/`：

| 工具 | 档位 | role 偏好 | 说明 |
|---|---|---|---|
| `browser_list_tabs` | T0 读 | user | 列已打开 tab |
| `browser_navigate` | T1 导航 | user / agent | 打开/切换 URL，agent 模式下默认走 isolated |
| `browser_query` | T0 读 | user / agent | CSS/XPath/aria 查 DOM，返结构化数组 |
| `browser_extract` | T0 读 | user / agent | Readability 抽正文 + 链接 + 截图路径 |
| `browser_screenshot` | T0 读 | user / agent | 截当前 tab，存 `.kedo/cache/screenshots/` |
| `browser_click` | T2 写 | user | 点击元素，三元组定位 |
| `browser_type` | T2 写 | user | 输入文本，密码字段自动跳过 |
| `browser_wait_for` | T1 | user / agent | 等元素出现/消失，超时 30s |
| `browser_research` | T0 高层 | **agent** | 给 query → isolated profile 内部 navigate + extract，封装常用研究入口 |

每个工具继承 `BaseTool`，工具内部 `await bridge.send_command(action, params, prefer_role=...)`。注册位置：`api/server.py` 在 ToolRegistry 创建处加 `registry.register(BrowserNavigateTool(bridge))` 等。

`browser_research` 是高层封装，内部展开成多个低层调用 + 结果合并，专用 isolated session：

```python
async def run(self, query: str, max_pages: int = 3) -> ToolResult:
    profile = await self._profile_manager.get_or_start()
    await self._bridge.send_command("navigate",
        {"url": f"https://duckduckgo.com/?q={quote(query)}"}, prefer_role="agent")
    results = await self._bridge.send_command("query",
        {"selector": ".result__a", "limit": max_pages}, prefer_role="agent")
    pages = []
    for r in results["data"]["matches"][:max_pages]:
        await self._bridge.send_command("navigate", {"url": r["href"]}, prefer_role="agent")
        extracted = await self._bridge.send_command("extract", {}, prefer_role="agent")
        pages.append({"url": r["href"], "title": extracted["title"], "text": extracted["text"]})
    return ToolResult(success=True, output=..., data={"pages": pages})
```

---

## 5. 通信协议（PROTOCOL.md 摘要）

WS 帧统一 JSON。当前协议版本：`1.0`。

### 5.1 握手

```json
// 插件 → 后端
{ "type": "hello", "client": "kedo-browser-bridge",
  "client_version": "1.0.0", "protocol_versions": ["1.0"],
  "role_hint": "user",
  "token": "..." }

// 后端 → 插件
{ "type": "hello_ack", "session_id": "...", "negotiated_protocol": "1.0",
  "role": "user",
  "server_capabilities": ["context_inbox", "permission_v1"] }
```

### 5.2 命令-结果（agent → 浏览器）

```json
// server → client
{ "type": "command", "id": "uuid",
  "action": "click",
  "params": { "tab_id": 12,
              "selector": "button[aria-label='Save']",
              "text_match": "保存",
              "aria_label": "Save" } }

// client → server
{ "type": "result", "id": "uuid", "success": true,
  "data": { "matched_strategy": "aria",
            "matched_text": "保存",
            "screenshot_path": "/Users/.../shot.png" } }

// 失败
{ "type": "result", "id": "uuid", "success": false,
  "error": { "code": "ELEMENT_NOT_FOUND", "message": "..." } }
```

### 5.3 用户喂网页（B 场景，无对应 command）

```json
// client → server，落到 ContextInbox
{ "type": "user_inject", "payload": {
    "url": "https://github.com/...",
    "title": "...",
    "dom_text": "正文 markdown 化后的版本",
    "selection": "用户选中的片段（可空）",
    "screenshot_path": "/tmp/.../shot.png",
    "user_note": "可选用户备注"
}}

// server → client，确认入盒
{ "type": "ack", "kind": "user_inject_received",
  "inbox_item_id": "..." }
```

### 5.4 心跳与权限确认

```json
// 双向心跳
{ "type": "heartbeat", "ts": 1234567890 }

// T2 写操作前，后端可主动询问插件展示确认 UI
{ "type": "permission_request", "id": "...",
  "action": "click", "domain": "example.com",
  "tier": 2 }
{ "type": "permission_response", "id": "...",
  "decision": "allow_once" | "allow_30min" | "deny" }
```

权限确认也可以走 dashboard 弹窗（已有事件系统），插件侧的 permission_request 是**备份通道**（dashboard 没开时使用）。

### 5.5 错误码约定

| code | 含义 | agent 处理建议 |
|---|---|---|
| `ELEMENT_NOT_FOUND` | 三元组都没命中 | 试 wait_for 后重试，或 extract 整页让 LLM 重新选 |
| `PERMISSION_DENIED` | 用户拒绝或域名黑名单 | 调 `pause_for_human` 暂停 |
| `NAVIGATION_TIMEOUT` | 页面加载 30s 没好 | 截图 + extract 看到底加载到哪了 |
| `PASSWORD_FIELD_BLOCKED` | type 命中密码字段 | 永远不重试，必须人工 |
| `NO_AGENT_SESSION` | isolated profile 没启动 | 调 `core.browser_profile.start()` 后重试 |

---

## 6. ContextInbox（响应"上下文盒子"决策）

### 6.1 数据模型

```python
@dataclass
class InboxItem:
    id: str
    received_at: datetime
    source: Literal["browser_inject", "manual_paste"]
    url: Optional[str]
    title: Optional[str]
    content: str                    # 正文（markdown 化）
    selection: Optional[str]
    screenshot_path: Optional[str]
    user_note: Optional[str]
    used_in_tasks: list[str] = []   # 被哪些 task 引用过

class ContextInbox:
    async def add(item: InboxItem) -> str: ...
    async def list(limit: int = 50) -> list[InboxItem]: ...
    async def get(item_id: str) -> InboxItem: ...
    async def delete(item_id: str) -> None: ...
    async def attach_to_task(item_ids: list[str], task_id: str) -> None: ...
```

落地存 `~/.kedo/inbox.jsonl`（追加 + 查询时全读，量小阶段足够；后期可换 SQLite）。

### 6.2 REST 接口

```
POST   /context-inbox            # 内部用，由 BrowserBridge 调
GET    /context-inbox            # dashboard 列表
GET    /context-inbox/{id}       # 详情
DELETE /context-inbox/{id}       # 用户清理
POST   /tasks                    # body 加 inbox_item_ids: [...] 字段
                                 # → ReactAgent 启动时把这些 item 拼进 system prompt
POST   /tasks/{id}/attach-context # 给已存在 task 追加 inbox 内容
```

### 6.3 Dashboard 上的样子

```
┌─ Context Inbox (3) ──────────────────── ✕ Clear all ─┐
│ ☐ [GitHub] devkitpro/libnx#420 — audio init returns -19  │
│   "I get -19 when calling audoutInitialize after..."   │
│   2 minutes ago · 12KB · screenshot                    │
│   [ View ] [ Delete ] [ Attach to active task ]        │
│ ☐ [DevkitPro Wiki] Audio HOWTO                          │
│   ...                                                  │
│ ☐ [Stack Overflow] How to debug Switch crash...         │
│   ...                                                  │
└──────────────────────────────────────────────────────┘
[ ☑ Select all ]   [ Start new task with selected (2) ▼ ]
```

用户工作流：
1. 浏览 GitHub issue → 点插件 popup "Send to kedo" → item 1 入盒
2. 浏览 wiki → 同上 → item 2 入盒
3. 进 dashboard → 全选 → "Start new task" → 输入需求"修复这个 audio init -19 错误，参考 wiki 提到的初始化顺序"
4. ReactAgent 收到 task，system prompt 已注入 item 1 + item 2 全文

**关键：**inbox **不会**自动触发 task，避免用户随手发到一半就被 agent 跑起来；用户必须显式选中 + 起 task。

---

## 7. 关键流程时序

### 7.1 场景 A — Agent 验证生成的 dashboard

```
ReactAgent ──(1) browser_navigate(localhost:8765)
   ↓ tool registry
BrowserBridge ──(2) WS command{action:navigate} → user_session
   ↓
插件 service worker ──(3) chrome.tabs.update → content script load
   ↓
content script ──(4) result{success:true, current_url:"..."} → service worker → WS
   ↓
BrowserBridge ──(5) Future.set_result → tool 返回
   ↓
ReactAgent ──(6) browser_screenshot()
   ↓ ... 同上回流
   ↓
ReactAgent ──(7) LLM 看截图判断"task 卡片是否符合 charter 预期"
   ↓
不符合 → 调 auto_fix_tool → 修代码 → 重 build → 回到 (1)
```

### 7.2 场景 B — 用户喂网页（走盒子）

```
用户 ──(1) 在 GitHub issue 页点插件 popup "Send to kedo"
   ↓
插件 popup ──(2) chrome.runtime.sendMessage{collect_page} → service worker
   ↓
service worker ──(3) 注入 content_script.collect 抓正文
   ↓
content script ──(4) Readability 抽正文 + 截图 → 回 service worker
   ↓
service worker ──(5) WS user_inject{...} → BrowserBridge
   ↓
BrowserBridge ──(6) ContextInbox.add() + dashboard event
   ↓
Dashboard ──(7) inbox 面板显示新条目（不创 task!）
   ↓ 用户后续...
用户 ──(8) 在 dashboard 选条目 → "Start new task" → 输入需求 → POST /tasks
   ↓
ReactAgent 启动，system prompt 注入 inbox 内容
```

### 7.3 场景 C — Agent 查资料（独立 profile）

```
ReactAgent ──(1) browser_research(query="libnx audoutInitialize -19 error")
   ↓
BrowserResearchTool 内部:
   ├─ (2) IsolatedBrowserProfile.get_or_start()
   │      ├ 第一次:启动 chrome --user-data-dir=~/.kedo/browser-profile ...
   │      ├ 等插件以 role=agent 连接 BrowserBridge
   │      └ 返回 agent_session
   ├─ (3) bridge.send_command("navigate", url=duckduckgo, prefer_role="agent")
   ├─ (4) bridge.send_command("query", selector=".result__a", prefer_role="agent")
   ├─ (5) for top 3 link: navigate + extract → 收正文
   └─ (6) 拼接 + return ToolResult(pages=[{url, title, text}])
      ↓
ReactAgent ──(7) 把 pages 当作 observation 喂回 LLM
```

**第一次启动 isolated profile** 会有 5-15 秒延迟（chrome 冷启 + 插件加载 + ws 握手），后续保持运行复用。空闲 30 分钟自动关闭。

---

## 8. 安全 / 权限模型

### 8.1 Tier 分级

| Tier | 操作 | 默认策略 | 触发确认 |
|---|---|---|---|
| 0 读 | list_tabs / query / extract / screenshot / get_url | 自动放行 | 无 |
| 1 导航 | navigate / scroll / wait_for | 域名白名单内自动；白名单外问一次记忆 | dashboard 弹窗 |
| 2 写 | click / type / submit | 默认每次确认；用户可"信任此域名 30 分钟" | dashboard 弹窗 + 插件 sidepanel |
| 3 高危 | 任意 JS 执行 / 文件上传 / 剪贴板写 / 跨 origin | 默认 OFF（配置开关，不暴露 LLM 工具） | 无（直接拒） |

**硬规则**（任何 Tier 都不能突破）：
- `chrome://` `file://` `chrome-extension://` 全禁
- `<input type="password">` / `autocomplete~="cc-"` skip 不报错
- cross-origin iframe 不穿透
- CAPTCHA 检测命中 → 立即调 `pause_for_human`

### 8.2 域名白名单

写在 `~/.config/kedo/browser_allowlist.txt`：

```
# kedo 默认白名单
localhost
127.0.0.1
github.com
*.devkitpro.org
duckduckgo.com
stackoverflow.com
# 用户手动追加
example.com
```

`*.foo.com` 支持通配。命中白名单 → T1 自动放行；不命中 → 弹窗"将 example.org 加入白名单？"，记到文件。

### 8.3 与现有 PermissionManager 集成

`core/permissions.py` 现有 `check(tool_name, **kwargs)` 接口。新增：

```python
class BrowserPermissionPolicy:
    def __init__(self, allowlist_path, default_tier_policy):
        ...

    async def check(self, action: str, params: dict, role: str) -> Decision:
        tier = self._tier_of(action)
        domain = self._extract_domain(params.get("url") or params.get("tab_url"))
        # role=="agent" 走更严格策略（非用户授权情形）
        ...
```

PermissionManager 启动时把这个 policy 注册到 `ToolRegistry.permission_manager`，所有 `browser_*` 工具调用前自动过 check。

### 8.4 审计日志

每个浏览器操作（无论 Tier）都落 `~/.kedo/browser-audit.jsonl`：

```json
{"ts":"2026-05-08T14:23:11Z","session":"user","action":"click",
 "url":"https://github.com/...","selector":"...","decision":"allow_30min",
 "result":"success","triggered_by":"task_42"}
```

dashboard 提供 `/audit/browser` 视图供事后回看。

---

## 9. MVP 路线

| 期 | 范围 | 工作量 | 验证标准 |
|---|---|---|---|
| **M1 — 通道 + 上下文盒子** | 单独 repo 创建；MV3 manifest + service worker + popup "Send to kedo"；后端 `/ws/browser` + `ContextInbox` + dashboard inbox 面板；不实现任何 agent 控制 | 3-5 天 | 用户在 devkitpro wiki 点 popup → dashboard 立刻收到正文 + 截图卡片 → 用户能从 inbox 起新 task 并把内容传进 system prompt |
| **M2 — Agent 只读控制** | T0 工具（list_tabs/query/extract/screenshot）+ T1 navigate；session role 区分；ReactAgent 注册 | 5-7 天 | 在 dashboard 让 agent "看一下当前激活 tab 上有什么"，agent 调 list_tabs + extract 正确返回 |
| **M3 — Agent 写 + 权限** | T2 click/type + 三元组 selector + dashboard 确认 UI + Tier 策略 + 审计日志 | 5-7 天 | 让 agent 在自己的 dashboard 上点一个按钮（自闭环），权限弹窗按预期触发；密码字段被拒 |
| **M4 — 隔离 profile + research** | `core/browser_profile.py` 启动 isolated chrome；同插件 role=agent 模式；`browser_research` 高层工具 | 5-7 天 | agent build 失败时自调 research，能从 stackoverflow 取回 3 篇相关问答并改对代码 |
| **M5（可选）— 跨浏览器 + 录制回放** | Firefox / Edge MV3 兼容；操作录制 → 回放成 charter 测试用例 | 后续 | — |

每期独立可发布（插件 v0.1 / v0.2 / ... 配套 kedo backend 协议版本）。

---

## 10. 风险与权衡

### 10.1 已识别的风险

| 风险 | 影响 | 缓解 |
|---|---|---|
| **MV3 service worker 不稳** | 长任务途中 ws 断 | alarms 24s ping + 命令幂等（同 request_id 重发不重复执行）+ ReactAgent 检测 `NO_AGENT_SESSION` 时重启 isolated profile |
| **Selector 漂移** | 网站改版工具失效 | 三元组定位（selector + text + aria）；result 回 `matched_strategy` 让 LLM 学会哪种最稳；charter 里记录关键页面的优先 strategy |
| **截图带宽爆 WS** | 大图卡死连接 | >200KB 落本地路径；vision 模式按需 fetch |
| **用户登录态滥用** | agent 误用主浏览器登录的 GitHub 发评论 / 改 PR | 场景 A 默认 T2 强制确认；场景 C 强制走 isolated profile（与决定 #3 一致）；密码字段硬规则拒绝 |
| **CAPTCHA / Cloudflare** | research 被拦 | 检测到立即 pause_for_human，不做绕过 |
| **协议跨 repo 漂移** | 插件版本和 backend 版本不匹配 | 握手协商 + 版本不兼容时插件 popup 显示警告 + dashboard 显示告警 banner |
| **隔离 profile 占资源** | chrome 多开吃内存 | 空闲 30 分钟自动 close；用户可在 dashboard 手动关 |
| **Chrome Web Store 上架审核** | 自动化插件可能被拒 | 自用阶段走 "Load unpacked"（developer mode），无需上架；上架问题晚于 M3 再考虑 |

### 10.2 关键权衡点（已选择）

- **单 repo vs 多 repo（已选多 repo）**：换来发布解耦、协议契约显式化；代价是联调多一步。决定 #1。
- **直接创 task vs 上下文盒子（已选盒子）**：换来用户控制感、避免误触发；代价是多一个 dashboard 面板。决定 #2。
- **共享 profile vs 隔离 profile（已选隔离）**：换来 agent 操作不污染用户登录态；代价是要管理额外 chrome 实例 + 启动延迟。决定 #3。

### 10.3 仍待决定（不影响 M1）

- **协议封装层**：纯 JSON-WS 还是上 protobuf / msgpack？M1 用 JSON 够用，M2/M3 量起来再评估。
- **selector 学习机制**：是否在 charter 里持久化"这个页面用 aria 最稳"，让后续调用直接走对的 strategy？M3 后再做。
- **多 user_session 并发**：用户开两个浏览器都装插件会怎样？M1 假定单 user_session（后开的踢前面的），M3 后再支持。
- **Firefox / Edge 支持**：MV3 大体兼容但 alarms / sidepanel 等 API 有差异。M5 再处理。
- **是否需要专门的 BrowserAgent 子角色？** —— 当前 ReactAgent 持 15 个工具再加 9 个浏览器工具，可能 prompt 会膨胀。是否拆出一个"浏览器子 agent"由 ReactAgent 委派？属于 `multi-agent-architecture.md` 范畴，留 Phase 后讨论。

---

## 11. 决策日志

| 日期 | 决策 | 理由 |
|---|---|---|
| 2026-05-08 | 立项写本文档 | 用户提出"像 Claude 一样浏览器插件控制浏览功能" |
| 2026-05-08 | 通道选 WebSocket | 复用 `api/websocket.py`；双向实时；最简单 |
| 2026-05-08 | 三大场景 A/B/C 全做 | 用户明确所有三种交互模式都要 |
| 2026-05-08 | 边界：不做 hack 行为 | 用户原话"不需要 hack 行为"；写进硬规则 |
| 2026-05-08 | 插件单独 repo（决定 #1） | 解耦发布、明确协议契约 |
| 2026-05-08 | 用户喂网页进上下文盒子（决定 #2） | 避免误触发；用户保留控制权 |
| 2026-05-08 | Agent 查资料用独立 profile（决定 #3） | 不污染用户主浏览器登录态 |

---

## 12. 下一步

进入 M1，开始：
1. 在 GitHub 创建 `kedo-browser-bridge` repo，写好 `PROTOCOL.md` 1.0 草稿
2. kedo repo 实现 `api/browser_bridge.py` + `api/context_inbox.py` + `/ws/browser` 路由
3. 插件实现 manifest + service worker + popup（"Send to kedo"）
4. dashboard 加 inbox 面板（HTML + WS 事件订阅）
5. 端到端验证标准：用户在 GitHub issue 页一键 → kedo dashboard 显示 → 用户从 inbox 起 task → ReactAgent 收到正确上下文

M1 不动 ReactAgent 工具集，先确保通道 + 盒子工作。
