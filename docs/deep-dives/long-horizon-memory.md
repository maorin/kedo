# kedo 如何应对 Long-horizon Memory Loss（长任务遗忘）

## 问题

**Long-horizon Memory Loss**：LLM 在 30+ turn 的任务里会系统性忘记早期信息：

- 用户开头说"必须用 libnfs"，中段 build 失败后 LLM 自己换成"本地 stub" 不记得约束
- `plan_development` 拆解过 12 个 subtask，跑到第 7 个时 LLM 忘了剩下 5 个
- System prompt 写了"不许用 sudo"，30 轮后 LLM 还是跑 `sudo apt install`
- 需求里的关键术语（"NFS 视频播放器"）在多轮工具调用的 result 冲刷后变成"视频播放器"
- `prior_attempts` 最近 10 条之外的失败记忆被淘汰

和 [context-management.md](context-management.md) 的区别：那篇是"context window 会撑爆"（物理限制），这篇是"即使没撑爆，attention 也会稀释关键信息"（语义限制）。

## 答：5 类机制 + 重要 gap

### 1. System prompt 始终在头部（最强 attention 位）

ReactAgent 的 messages 结构：

```
[0] system: 项目上下文 + 工具规范 + 任务链继承 + user 需求摘要
[1] user:   原始需求
[2..N]      工具调用 + 结果交替
```

`messages[0]` 永远是 system prompt，不会被新消息挤掉（`_truncate` 对 system role 有保护）。关键**约束和项目规则**（禁止 sudo、工具使用模板、平台知识）写在这。

### 2. 任务链跨 session 继承

`_gather_prior_task_context`（M1 新加，`core/react_agent.py:_gather_prior_task_context`）：新 task 启动时查同项目最近 failed/paused 的 task，提取以下要素塞进新 task 的 system prompt 末尾：

- 上个 task 的 plan subtask 标题链（"需求分析 → SDD → code_generate main.c → build → ..."）
- 卡点位置（current_step）
- 描述摘要（前 300 字）

**目的**：跨 session 长任务被切分成多个 task 时，关键上下文不丢。你提交"修复这个问题再编译"时，LLM 拿到的不是空白上下文。

### 3. Checkpoint messages 全量保存 + resume

`AgentCheckpoint.messages`（M2 新加字段）保存完整 messages 历史（不压缩）。`resume_from_checkpoint` 恢复时直接还原——**保真度优先于精简**。理论上：即使你明天再开 kedo，上个 paused task 的对话历史一字不差。

### 4. prior_attempts 跨 session 持久化

`core/project_profile.py:ProjectProfile.prior_attempts` 保留最近 **10 条** 的 build/auto_fix 失败快照（`build_command + stderr_excerpt + patched_file`）：

- 写入：auto_fix 每次补丁后追加
- 读取：auto_fix 下次调用注入最近 3 条作为 negative examples
- 持久化：落盘在 `.kedo/project_profile.json`，下次 kedo 启动仍在

**超出 10 条的老记录被淘汰**——这是记忆的代价，老失败的教训会"真忘"。

### 5. ReAct 循环自然记录

LLM 每次调工具，`tool_call + tool_result` 这对消息就进 messages，LLM 下轮看到"我试过 X 得到 Y"——这是**最自然的短期记忆**。关键代码：

```python
# react_agent.py:_loop
messages.append(assistant_msg)              # LLM 说要干啥
messages.append({"role": "tool", ...})      # 工具返回了啥
```

**短期**记忆在 ReactAgent 层自动做。**长期**记忆（跨 task / 跨 kedo restart）靠 checkpoint + prior_attempts + profile。

## 诚实的 gap

| 问题 | 状态 |
|---|---|
| 用户原始需求在长任务中被稀释 | ⚠ 需求只在 `messages[1]`，第 30 轮时它前后都是工具输出，attention 权重下降 |
| 关键约束 reminder 机制 | ❌ 没有"每 N turn 把核心约束重放一次" |
| AgentMemory 的 LLM 压缩 | ❌ 代码存在但 ReactAgent 没调，见 context-management.md |
| prior_attempts 淘汰策略 | ⚠ 简单 FIFO（最近 10 条），没按"重要性"评分；很早的关键失败会被刷掉 |
| plan subtask 状态不回写 | ❌ LLM 忘了还有哪些 subtask 没做，plan 也不告诉他（见 planning-instability.md） |
| 语义级"核心约束"标注 | ❌ 用户说"必须用 libnfs" 跟"顺便加个 logo" 在 messages 里权重一样 |

## 现状为何还 work

- **实战任务多数 < 20 turn**：收敛检测 3 次 fail 就 pause，prose 结尾强制 respond，不会无限延长
- **build exit_code 是客观锚点**：每次 build 把 LLM 从"漂移的解释"拉回"能不能跑"的硬事实
- **System prompt 有相对稳定的 attention 权重**（语言模型对开头+结尾都有偏好），关键项目规则写那里基本抓得住
- **任务链继承** 把"上次到哪了"直接塞进新 task 的 system，绕过长记忆需求

## 实战案例

**switchvideo 2026-04-19 13:45**：
- Turn 1 system prompt 含"项目目标：Nintendo Switch NFS 视频播放器（连接 NFS 共享存储播放视频）"
- 经历 3 次 build fail、2 次 auto_fix、Kimi reasoning-only fallback 3 次
- Turn 25+ 时 LLM 决定把 `nfs_client.c` 改成 **dirent.h + sys/stat.h 本地文件系统 stub**
- Turn 30 LLM 汇报"build 成功🎉"，task 标 completed

LLM 早期清楚任务是"NFS"，后期面对 libnfs 找不到的压力下，**悄悄把约束从"NFS"降级为"能播放视频的播放器"**——核心约束被稀释了，用户在 dashboard 上看到"completed"以为完了，实际功能层面缩水。

这是长任务遗忘叠加 [Self-evaluation Drift](self-evaluation.md) 的合谋。

## 下一步候选（未决，按 ROI 排序）

1. **核心约束 reminder 注入**（~2h）：检测 turn ≥ N 时自动在下一轮 user 消息里追加 "Reminder: 原始需求核心约束是：[关键词摘要]"。关键词从 description 里抽取（简单版：前 100 字 + 要求/必须/一定 等提示词前后句）
2. **接入 AgentMemory.get_context_window**（~2h）：70% 容量时调 LLM 压缩早期非关键消息为摘要，保留 system + 最近 10 条 + 摘要（context-management.md 里也提过）
3. **重要性排序 prior_attempts**（~1h）：保留标签（如"涉及 libnfs 的失败"）而不是 FIFO，关键失败长期保留
4. **plan subtask 进度回写** + 每轮显示"剩余 subtask"（~2h，和 planning-instability.md 重合）

前 2 条独立做都有效，组合起来可以把 30+ turn 任务的"遗忘率"压下去。
