# 双 Agent 对抗架构（方案 C / Actor-Critic）

> **状态**：2026-04-25 已落地，可通过配置开关。  
> **设计渊源**：`docs/deep-dives/multi-agent-architecture.md` "方案 B vs 方案 C：详细对比"。  
> **本文档定位**：面向当前代码状态的实施 reference。设计动因看 deep-dive。

## TL;DR

- 在保留单 ReactAgent 流水线的前提下，新增**独立 Reviewer Agent**。
- Reviewer 跑在**另一个 LLM provider**（默认 DeepSeek，可换 Claude/Kimi/OpenAI）——物理破 Self-eval drift。
- 两处关卡接入：`evaluate` 工具被调时 → Reviewer 打分；`commit_candidate` 工具创建候选前 → Reviewer 强制过审。
- 一行配置 `reviewer_provider: none` 关掉，整套退回单 Agent 行为，零迁移成本。

## 角色与拓扑

```
                ┌─────────────────────────────────────────────┐
                │          Producer (ReactAgent)              │
                │   主 LLM = Kimi-code / Claude / Kimi / ...  │
                │                                             │
                │   ReAct loop: LLM → tool → observe → ...    │
                │   工具集：15 个（file_*/shell/build/test/   │
                │           code_generate/auto_fix/git/       │
                │           plan_development/respond/         │
                │           propose_alternatives/             │
                │           pause_for_human/                  │
                │           ★ evaluate / ★ commit_candidate)  │
                └─────────────┬───────────────────────────────┘
                              │ 在以下两个工具调用时跨过去
                              │
                  ┌───────────▼─────────────┐
                  │   ★ evaluate (adhoc)    │
                  │   ★ commit_candidate    │
                  │     (pre-commit gate)   │
                  └───────────┬─────────────┘
                              │ 调
                              ▼
                ┌─────────────────────────────────────────────┐
                │           Reviewer Agent                    │
                │   独立 LLM = DeepSeek / Claude / ...        │
                │                                             │
                │   持久化对象（跨关卡累积 _history）          │
                │   只读产物：requirement + 磁盘代码 +        │
                │              build/test 输出                │
                │   返回 ReviewResult{approve, score, ...}    │
                │   无工具集 — 不写文件、不调 shell           │
                └─────────────────────────────────────────────┘
```

**关键**：两 Agent **不**互相调工具、**不**通过消息总线通信。Producer 主 loop 不变，只是某些工具内部把"自评"换成"调 Reviewer"。所以现有 ReactAgent / convergence detection / checkpoint / dashboard / WebSocket 全部不动。

## 核心组件

### Producer = 现 ReactAgent

无改动。只是它能调用的两个工具 (`evaluate` / `commit_candidate`) 行为变了。

代码：`core/react_agent.py`

### Reviewer Agent

代码：`core/reviewer.py`

| 属性 | 说明 |
|---|---|
| `_llm` | 独立 LLM client（与 Producer 不同 provider） |
| `_inner` | 包了一个 `Evaluator(llm_client=独立LLM, system_prompt=REVIEWER_SYSTEM_PROMPT)`，复用静态检查 + merge 逻辑 |
| `_history` | `list[ReviewResult]`，最近 10 条判决，跨关卡累积；`_history_prefix_for_prompt()` 把上次意见拼入 parent_section 给当前 review 做背景 |
| `_min_score` | approve 阈值（默认 70，可比 `min_eval_score` 更严） |

公共接口：

```python
async def review(
    requirement: str,            # 当前 scope 的需求（评分依据）
    changed_files: list[CodeChange],
    project_path: str,
    stage: str = "pre_commit",   # build_ok / test_ok / pre_commit / adhoc
    test_results: TestResult | None = None,
    parent_goal: str = "",
) -> ReviewResult
```

`ReviewResult` 含：

| 字段 | 说明 |
|---|---|
| `approve: bool` | `score >= min_score` |
| `score: float` | 4 维加权总分 0-100 |
| `stage: str` | 关卡名，写日志用 |
| `comments: str` | 一行摘要：`[stage=...] score=... (req_match=..., ...) \| Missed: ... \| Risks: ...` |
| `dimensions / requirements_met / requirements_missed / risks / suggestions` | 同 `EvalReport` |
| `reviewer_model / review_id` | `rev-xxxxxx`，写进 candidate 元信息 |

**Reviewer 的 system prompt** 与 Evaluator 不同（`REVIEWER_SYSTEM_PROMPT`）：

> You are an **independent code reviewer** on a 2-agent system. Another agent (the "Producer") wrote this code. **You did NOT write it.** Your job is to catch problems the Producer missed — confirmation bias on its own work is your target.
> ... Be honest, not agreeable. The Producer is another LLM; it cannot be offended.

### 工具改造

`tools/evaluate_tool.py` (`EvaluateTool`)
- 构造时同时收 `evaluator`（旧）+ `reviewer`（新）
- `execute()` 双路径：
  - Reviewer 激活 → `reviewer.review(stage="adhoc")` → 输出以 `Reviewer[provider/model] score: ...` 开头
  - 否则 → 旧 `evaluator.evaluate()` → 输出以 `Evaluation score: ...` 开头（单 Agent 行为）
- `data.source` 字段标 `"reviewer"` / `"evaluator"`，便于 dashboard/审计区分

`tools/commit_candidate_tool.py` (`CommitCandidateTool`)
- 构造时收 `reviewer` + `pre_commit_gate: bool`（默认 True）
- `execute()` 在调 `VersionManager.create_candidate` **之前** 强制走 Reviewer：
  - approve → 继续创建候选；候选元信息 `ai_confidence` 用 Reviewer 分数，`data` 含 `reviewer_review_id / reviewer_score / reviewer_provider / reviewer_model`
  - reject → `ToolResult(success=False, error="Reviewer REJECTED this candidate ... iterate first.")`，Producer 必须继续修代码再试，不能 retry 同代码

### `_normalize_changed_files`（容错入口）

`tools/evaluate_tool.py` 内的归一化函数。LLM 实测会用 6 种格式传 `changed_files`：
1. 规范 `[{file_path, action}]`
2. `["a.c", "b.c"]`（list of str）
3. `{"a.c": "modify", "b.c": "create"}`（dict 作 action）
4. `{"src/main.c": "主程序"}`（dict 作描述 — **生产环境踩到过，全静默丢弃 → DeepSeek 审零代码 → 0 分**）
5. `{"a.c": {"action": "modify"}}`（嵌套）
6. `{"files": [...]}`（包一层）

归一化全部转成规范 list，非规范格式打 warning 提醒（`commit_candidate_tool` 也复用同一函数）。

## 关卡流程

### Gate 1：`evaluate` 工具（adhoc）

Producer 在写完代码、build/test 都过后主动调，用来"自己看分数决定要不要再迭代一次"。

```
Producer ReAct loop
  ↓
LLM 决定调 evaluate(requirement, changed_files=[...])
  ↓
EvaluateTool.execute()
  ├─ _load_code_changes  → _normalize_changed_files  → 读磁盘补 content
  ├─ Reviewer.review(stage="adhoc")
  │     ├─ static checks (syntax / lint / dangerous patterns)
  │     ├─ DeepSeek chat (REVIEWER_SYSTEM_PROMPT + scoped requirement + code + static)
  │     ├─ 解析 JSON → EvalReport
  │     └─ 包成 ReviewResult，approve = score >= min_score，push _history
  └─ 返回 ToolResult(success=approve, output="Reviewer[...] score: ...")
  ↓
Producer 看到分数，决定 respond / propose_alternatives / 继续修
```

### Gate 2：`commit_candidate` 工具（pre-commit gate）

Producer 觉得"这一版本可以固化为候选"时调，最严的一道闸。

```
Producer 调 commit_candidate(title, requirement, changed_files=[...])
  ↓
CommitCandidateTool.execute()
  ├─ 若 reviewer 激活 + pre_commit_gate=True：
  │     Reviewer.review(stage="pre_commit")
  │     ├─ approve=False → return FAIL("Reviewer REJECTED ... iterate first")
  │     │                  Producer 不能用同代码再 commit；必须先改
  │     └─ approve=True → 继续
  ├─ VersionManager.create_candidate(ai_confidence=reviewer_score/100, ...)
  └─ 返回 success，候选 panel 显示 "Reviewer approval: 75/100 (DeepseekClient/...) [rev-xxxxxx]"
```

### Stage 字段语义

`stage` 只影响日志/comments 的标签和 `_history` 排序，**不影响打分权重**：

| stage | 触发位置 | 用法 |
|---|---|---|
| `adhoc` | EvaluateTool 调用 | LLM 主动想看分 |
| `pre_commit` | CommitCandidateTool 调用 | 候选写入前的最后审查 |
| `build_ok` / `test_ok` | 暂未启用 | 预留给 build/test 关卡自动调 Reviewer |

## 配置

`config.yaml`（仓库根 + `~/.config/kedo/config.yaml` 用户态合并；用户态优先）：

```yaml
# Producer (主 LLM) — 不变
llm_provider: "kimi-code"
model: "kimi-k2.5"
kimi_api_key: ""

# 方案 C — Reviewer
reviewer_provider: "deepseek"     # none | anthropic | openai | kimi | kimi-code | deepseek | ollama | mock
reviewer_model: ""                # 留空用 provider 默认（deepseek → deepseek-v4-pro）
reviewer_api_key: "sk-..."        # 优先于环境变量；留空回退到对应 *_API_KEY 环境变量
reviewer_base_url: ""             # 可选覆盖
reviewer_min_score: 70            # approve 阈值（可严于 min_eval_score）
reviewer_pre_commit_gate: true    # commit_candidate 前是否过 Reviewer
```

支持的 provider 同主 LLM。常见组合：

| Producer | Reviewer | 说明 |
|---|---|---|
| kimi-code | deepseek | 当前默认实战组合 |
| kimi-code | anthropic | Claude 做 reviewer，最严但最贵 |
| anthropic | deepseek | 反过来，DeepSeek 性价比高 |
| anthropic | kimi | Kimi 反过来审 Claude |
| 任意 | none | 关闭，回退单 Agent |

**约束**：Producer 与 Reviewer 用同 provider 同 key 没意义（破不了 bias）；代码不阻止但请勿这么配。

## 启用 / 关闭

### 启用（在 192.168.1.8 上）
1. 编辑 `~/.config/kedo/config.yaml` 加 `reviewer_provider` + `reviewer_api_key`
2. 重启 kedo（无热重载，Reviewer 只在 `create_app` 时构造）
3. `curl http://localhost:8000/api/llm/status` 确认返回 `"reviewer":{"active":true,...}`
4. Dashboard 顶栏会变成 `🛡 双Agent: Producer=... | Reviewer=...`

### 关闭（rollback）
1. `reviewer_provider: "none"` 
2. 重启
3. EvaluateTool / CommitCandidateTool 完全走旧路径，dashboard 顶栏回到 `LLM: provider/model`
4. 单 Agent 行为完全等价于改造前

### 故障降级（Reviewer LLM 调用失败）

`reviewer.review()` 内部 `try/except` 包了 `_inner.evaluate()`：

- LLM 抛异常 → `logger.warning("Reviewer LLM call failed ...")` + 返回 `ReviewResult(approve=False, score=0.0, comments="Reviewer 调用失败...")`
- Producer 看到 score=0 + REJECT，自然继续迭代或 escalate（pause_for_human / propose_alternatives）
- 不会让整个任务挂掉

## 状态可见性

| 入口 | 显示 |
|---|---|
| `GET /api/llm/status` | 顶层 `provider/model/project_path` + `reviewer.{active, provider, model, min_score}` |
| Dashboard 顶栏 | 单 Agent: `LLM: kimi-code/kimi-k2.5`<br>双 Agent: `🛡 双Agent: Producer=kimi-code/kimi-k2.5 │ Reviewer=deepseek/deepseek-v4-pro`，hover 显示完整 tooltip |
| 工具输出 (`evaluate`) | `Reviewer[DeepseekClient/deepseek-v4-pro] score: 52.5/100 (REJECT min=70)` 开头 |
| 工具输出 (`commit_candidate` 通过) | candidate 输出含 `Reviewer approval: 75.0/100 (DeepseekClient/...) [rev-3d9214]` |
| 工具输出 (`commit_candidate` 拒绝) | `Reviewer REJECTED this candidate (score X.X < 70)... iterate first.` |
| `data.source` (evaluate 工具 data 字段) | `"reviewer"` 或 `"evaluator"` |
| `data.review_id` | `rev-xxxxxx` 唯一标识，可与 Reviewer 内部 `_history` 对照 |

## 已知 quirk 与缓解

### LLM 把 `changed_files` 传成 dict 而不是 list（已修）
- 现象：所有文件被静默丢弃 → Reviewer 审零代码 → 0 分；用户看到"评估全 0"误以为 Reviewer 没工作
- 缓解：`_normalize_changed_files` 兼容 6 种 LLM 常见格式 + 非规范打 warning
- 文件：`tools/evaluate_tool.py:24-100` / `tools/commit_candidate_tool.py:14`（import）

### DeepSeek-v4-pro 有 `reasoning_content` 思考链
- 现象：响应 JSON 同时含 `content`（正文）和 `reasoning_content`（思考）；OpenAI SDK 走 `content`
- 后果：当前正常，正文里就是规范 JSON
- 备用方案：若哪天 DeepSeek 把答案塞到 `reasoning_content`（参考 Kimi quirk），需要类似 `KimiClient` 的 reasoning 兜底——目前没碰到

### Reviewer 与 Evaluator 同名 / 路由层 hot-swap 不影响 Reviewer
- `/llm/switch` 路由的 `_evaluator._llm = new_client` 只换**外层** Evaluator（fallback 用）的 LLM
- Reviewer 的内层 Evaluator 是独立实例（`reviewer._inner`），不会被切换
- 这是有意的：用户切 Producer LLM 不影响 Reviewer

### Reviewer 启动时机
- Reviewer 只在 `api/server.py:create_app()` 构造一次。运行时改 config 不会重建
- 切 Reviewer provider/key 必须重启 kedo

## 与单 Agent 模式的等价性

`reviewer_provider: none` 时：
- `reviewer = None`（`api/server.py` 里的判断）
- EvaluateTool / CommitCandidateTool 看到 `self._reviewer is None` → 全部走旧路径
- 行为与方案 C 落地前**完全等价**
- 无任何性能 / 行为差异

这是设计上的硬约束：所有改动都是"加路径"而非"改路径"，老路径一字不改。

## 后续演进触发条件

按 `project_multi_agent_next.md` memory 记录的判定：

| 触发条件 | 演进方向 |
|---|---|
| Producer context 经常 > 40k、Kimi `reasoning_content` fallback 频繁 | 方案 B Phase 2（独立 Planner Agent，Worker context 收窄） |
| 需多 task 并发 / 多用户 | 方案 B Phase 3（Orchestrator + 多 Worker） |
| Reviewer 太严或太宽（实战收集 1-2 月数据） | 调 `reviewer_min_score` / 切 provider，不动架构 |
| Self-eval drift 继续显现 | 检查是否 Producer / Reviewer 真用了不同 provider；考虑加 multi-critic（不同 prompt 跑两个 Reviewer 取一致） |

方案 C 不锁定演进——Reviewer 类是个干净的可组合单元，未来作为方案 B 里的 Reviewer Worker 节点直接搬过去即可。

## 关键代码位置速查

| 关注点 | 文件 |
|---|---|
| Reviewer 类、ReviewResult、prompt | `core/reviewer.py` |
| Evaluator 系统提示可被覆盖（reviewer 用） | `core/evaluator.py:79`（`self._system_prompt = config.get("eval_system_prompt") or EVAL_SYSTEM_PROMPT`） |
| Evaluate 工具双路径 | `tools/evaluate_tool.py` |
| commit_candidate pre-commit gate | `tools/commit_candidate_tool.py` |
| changed_files 归一化 | `tools/evaluate_tool.py` 中 `_normalize_changed_files()` |
| Reviewer 构造与注入 | `api/server.py:create_app()`（搜 `reviewer = ` / `create_reviewer_llm_client`） |
| `/api/llm/status` 返回 reviewer 字段 | `api/routes.py:get_llm_status()` |
| Dashboard 顶栏双 Agent 渲染 | `dashboard/index.html` 中 `fetchLLMStatus()` |
| `/login` 增加 deepseek 选项 | `cli/repl.py` providers 列表 |
| DeepseekClient | `api/server.py:DeepseekClient` |
