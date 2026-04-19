# kedo 如何解决"上下文焦虑"

## 问题

ReactAgent 跑长任务（30+ turns）、处理大项目（每次工具输出动辄上万字符）、跨 task 续接——任何一个维度都可能把 LLM 的 context window 撑爆。kedo 用什么策略应对？哪里还没做到位？

## 答：5 层策略 + 重要 gap + 为何现状还 work

### 实际在用的 5 层策略

```
           Strategy                        配置                 作用域
┌────────────────────────────────────────────────────────────────────────┐
│ 1. 工具输出截断                 max_tool_output_chars=4000    单 tool 调用 │
│    _truncate() 首尾各一半                                              │
├────────────────────────────────────────────────────────────────────────┤
│ 2. 单次 LLM 请求 token 上限    max_tokens=8192                一次 LLM 调用 │
│    KimiClient / AnthropicClient                                        │
├────────────────────────────────────────────────────────────────────────┤
│ 3. Checkpoint 持久化            每 5 turn 保存 messages       单个 task 跨 turn │
│    AgentCheckpoint(messages=...)  resume 恢复                          │
├────────────────────────────────────────────────────────────────────────┤
│ 4. 任务链上下文继承             _gather_prior_task_context    跨 task   │
│    新 task 注入上任务 plan 摘要                                         │
├────────────────────────────────────────────────────────────────────────┤
│ 5. 增量上下文加载               tools 只读相关文件            工具内部    │
│    auto_fix 只拉 Makefile/CMake/profile + hint file，每 8KB 截         │
│    prior_attempts 只保留最近 10 条                                     │
└────────────────────────────────────────────────────────────────────────┘
```

### 每一层分别在防什么

1. **`_truncate(text, 4000)`**（`core/react_agent.py:396`）：工具返回的 output（比如 build stderr 10 万字符）追加到 messages 前压缩为前 2K + `[... N chars truncated ...]` + 后 2K。防止单次 stderr 撑爆一整个 context。

2. **`max_tokens=8192`**（`api/server.py` 各 LLM Client）：LLM 生成上限，防止 LLM 自己吐太长。Kimi 尤其要这个——以前不传默认很小，经常截断；太大又触发 reasoning-only quirk，8192 是平衡点。

3. **Checkpoint** 每 5 turn 写一次（`_save_checkpoint`），含完整 messages 历史 + plan + project_path + description。**关键设计**：resume 时恢复的是原始 messages，不是摘要——保真度高但可能大。

4. **任务链继承**（M1 新加，`_gather_prior_task_context`）：你开新 task 时，ReactAgent 查同项目最近 failed/paused 的 task，把它的 plan subtask 标题链 + 卡点 + 失败摘要塞进新 task 的 system prompt（约 300-500 字）。避免"每个新 task 从零白板探索同一片雷区"。

5. **auto_fix 的上下文采集**（`tools/auto_fix_tool.py:_gather_context`）：只拉项目根的 build manifests + LLM 指定的 target file，每文件 8KB 上限；prior_attempts 只注入**最近 3 条**作为 negative examples。不试图把整个项目塞给 LLM。

### 诚实的 gap：kedo 当前没做的事

| 问题 | 状态 |
|---|---|
| `AgentMemory` 里写了一套 LLM-driven 历史压缩（COMPRESS_THRESHOLD=0.7 触发摘要） | ❌ ReactAgent **实际没用**：`self.memory` 只被调 `snapshot()` 存 checkpoint，`get_context_window()` 从没被调用 |
| 动态 context budget 分配（按剩余额度决定给 LLM 喂多少） | ❌ 全靠 `max_tool_output_chars` 等硬编码常量 |
| Sliding window（老 messages 滑出去） | ❌ ReactAgent 的 messages list 只增不减，直到 max_turns 终止 |
| 根据 LLM 报的 token usage 反馈调整 | ❌ 没读 usage 字段 |

### 为什么现状还 work

1. **max_turns=50 + 收敛检测** → 一个 task 里消息堆积有上限（通常 < 20 turn 就 respond 或 pause）
2. **长输出的元凶是 build stderr** — 已被 `_truncate` 砍到 4K
3. **Kimi 8192 max_tokens** 让 LLM 生成撑不出太长的回复
4. 遇到真压爆时 Kimi 会 reasoning-only fallback——**虽然是 quirk 但意外地限制了 output size**

### 真正卡住的场景

若 task 跑 30+ turn 且每轮都有中等大小工具输出（2-3K），messages 累计 60-90K tokens，就会有压力：

- Kimi 8192 `max_tokens` 仍 OK（请求体 input 不限）
- 但 Kimi reasoning-only 概率会上升（长 context → 模型偏向 reason）
- 我们的 reasoning_content fallback 顶住，但内容质量会退化

**未修复的隐患**：若任务逼近 context 上限（Kimi K2.5 约 128K tokens），会直接 HTTP 400 `context_length_exceeded`。kedo 没 catch 这个错，会以 retry 3 次后标 failed。

### 下一步候选（未决）

把 `AgentMemory.get_context_window()` 真接进 ReactAgent 的 `_loop`——每轮 LLM 调用前过一遍，超 70% 容量就调 LLM 把前面 N-10 条消息压缩成摘要。这是**现成代码**，只差布线：

- 修改点：`core/react_agent.py:_loop` 调 LLM 前插 `messages = await self.memory.get_context_window_for(messages, self.llm)`
- 工作量：~2h，风险：中（压缩质量依赖 LLM，弱模型可能摘丢关键决策）
- 回报：把理论上的 "30+ turn 会爆" 变成 "30+ turn 会降质但不爆"

暂未列入 backlog——当前实战还没撞到这个上限，属于"明确但非紧迫"的改进。
