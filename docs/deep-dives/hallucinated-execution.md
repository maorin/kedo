# kedo 如何应对 Hallucinated Execution（幻觉执行）

## 问题

**Hallucinated Execution**：LLM 声称做了某件事但**实际从没发生**。典型症状：

- "我已经修改了 main.c 第 42 行" — 但 `file_write` 工具从没调用过
- "build 成功，产物在 build/" — 但 `build` 工具没被调、目录也不存在
- "我读过这个文件" — `file_read` 报 not found，但 LLM 下一轮 reason as if content known
- LLM 在 reasoning_content 里"幻想"跑了某个 shell 命令
- LLM 改了不相关的变量名却声称"修复了 ffmpeg API 废弃警告"
- 文本 ReAct 模式：LLM 写了 ` ```tool_call ` 块但 fence 没闭合，**框架没解析到**，LLM 下轮以为调了

和 [self-evaluation](self-evaluation.md) 的区别：self-eval 是"做了但打分失真"，hallucinated 是"压根没做却声称做了"。

## 答：5 类机制 + 重要 gap

### 1. Tool registry 把"动作"收口到真调用

ReactAgent 的核心设计：**LLM 说的话不算数，只有 tool_call 才产生副作用**。

- LLM 在 content 里写 `"我已修改 main.c"` → 对磁盘零影响
- LLM 必须输出结构化 `tool_call` 块让 ReactAgent 解析 → 进 `tools.execute(name, **args)` → 工具真执行
- 工具返回 `ToolResult` 作为 `role="tool"` 消息写回 messages

**这是最根本的一层**：把"幻想" 和"现实" 物理隔离。文件没被 `file_write` 触碰就是没改。

### 2. 幻觉工具名的捕获

LLM 可能编造工具：`"我调用 magical_fix 工具修复了问题"`。

- `tools/base.py:115-119`: `ToolRegistry.execute` 先查 `_tools` 字典，没找到返 `ToolResult(success=False, error=f"Tool '{tool_name}' not found")`
- `core/react_agent.py:751`: `_execute_tool` 也有一道 `Unknown tool: {name}` 兜底

LLM 下一轮看到"Unknown tool"就知道换真实工具名。

### 3. 文本 ReAct 的解析兜底

Kimi Code 端点不支持 function calling，走文本 ReAct。这里有个阴险的幻觉陷阱：LLM **真的输出了 tool_call 块，但格式有瑕疵框架解析不到**，LLM 下一轮以为自己调了工具实际没调。

`core/react_agent.py:_parse_text_tool_calls` 兼容三类格式异常（M1 修复）：

| 异常 | 兼容 |
|---|---|
| 未闭合 ` ``` `（输出截断或模型遗漏） | 正则 `(?:\n```\|\Z)` 兜底到文末 |
| 同一块塞多个 JSON 对象 | 按花括号平衡扫描所有顶层 `{...}` |
| content 全空只有 reasoning_content | KimiClient 自动回落 reasoning_content 当 content |

**更严格的兜底**（M2 新加）：LLM 输出**非空 content 但没有任何 tool_call** 时，回灌 "请用 respond 工具明确收尾或继续调工具"。防"LLM 以为汇报完了，框架不知道要不要结束"。

### 4. 工具结果以"客观事实"格式回写

工具返回后，ReactAgent 把结果用 `role="tool"` 追加到 messages：

```python
messages.append({
    "role": "tool",
    "tool_call_id": tc.id,
    "content": truncated_output,  # 截断到 4000 chars
})
```

LLM 下轮看到这条消息，是**真实工具输出**，不是 LLM 自己想象的。想否认只能主动编造"忽略这个 result"——而多数 LLM 不会这么做。

### 5. 客观回路防"幻想成功"

最终层防御：**build / test 的 exit code 不能 fake**。

- LLM 声称 "build 成功" 只是 assistant content 里的一句话
- 真 build 成功必须是 `build` 工具的 `ToolResult(success=True, data={return_code: 0})`
- 再退一步：用户在 dashboard 看项目目录有没有 .nro/.exe/.elf 产物，LLM 再怎么幻想也变不出文件

收敛检测 + 强制 respond 收尾 + ProfileGuard 从不同角度锁住"LLM 说的 ≠ 实际发生的"这条缝。

## 诚实的 gap

| 问题 | 状态 |
|---|---|
| LLM 可以 pretend 读过不存在的文件 | ⚠ `file_read` 返 `File not found` 后，LLM 下轮仍然可以 "根据 main.c 的内容…" 瞎编——没有机制强制 LLM"你没读到就不许引用" |
| stderr 误读 → 错误修复 | ⚠ LLM 看错行号，改了不相关代码，build 再失败但故事听起来合理 |
| 工具返回的 error 被"合理化"成功 | ⚠ auto_fix 返 `unfixable` 时 LLM 可能还是说"已处理"——用户不盯日志发现不了 |
| respond 里的汇报和实际产物不一致 | ❌ LLM 可以 respond "已生成 5 个文件"，即使只生成了 2 个；ReactAgent 不校对 |
| 幻想命令执行输出 | ⚠ LLM 可以在 assistant content 里写"`$ make` 输出了 Success"——这是纯文本，framework 区分不了"引用工具历史" vs "编造工具历史" |

## 现状为何还 work

- **最多的幻觉对应的是零副作用**：LLM 在 content 里幻想跑了 `make`，但磁盘没动，`build` 工具客观数据不匹配 → 下一轮 `build` 真调后失败 → LLM 被现实纠正
- **ReAct 循环的短反馈**：幻觉最多撑过一轮，下一轮某个真实工具调用就会把幻觉暴露
- **Prose 结尾 retry** 强制 LLM 显式调 respond，收口"没调工具就说完了"这类隐形幻觉
- **文本解析器对未闭合 fence 的兜底**（M1）直接消除了"LLM 以为调了但框架没接" 的陷阱

## 实战案例

switchvideo 2026-04-16（M1 之前）：
- Kimi 在文本 ReAct 模式下输出：
  ```
  好的，我来查看项目结构。
  \`\`\`tool_call
  {"name": "file_read", ...}
  {"name": "file_read", ...}
  ```
- 注意 **只有一个开 fence，没有闭合 ` ``` `**（Kimi 输出截断了）
- 旧版 `_parse_text_tool_calls` 正则强制要求闭合 fence → 0 tool_calls
- ReactAgent 把整段当 final answer 返回 → task 被标 completed
- LLM 以为自己读了 2 个文件，**实际一个都没读**；用户看到"任务完成"但项目文件没动

M1 修复后：正则 `(?:\n```\|\Z)` 兜底到文末，**未闭合 fence 也能解析出 tool_calls**，幻觉陷阱堵死。

## 下一步候选（未决，按 ROI 排序）

1. **file_read 失败后 LLM 禁引用**（~2h）：维护本 task 内成功读过的文件集合，若 LLM 的下一条 content 里**引用了某文件但该文件没成功 read 过**，注入警告"你声称引用 X.c，但本 session 没成功 file_read 过"
2. **respond 内容事实校对**（~3h）：respond 工具 execute 时抓取用户需求 + 本 task 写过的文件列表，给 LLM "自你开始到现在真的写了这几个文件：[A, B, C]；请核对你的汇报是否准确"
3. **幻想输出检测**（~4h）：在 content 里出现 ` ```bash ` 代码块并夹带 `$ command` + 伪输出时，给 LLM warning "这看起来是幻想的命令输出，请用 shell_execute 真跑"
4. **语义级 diff 验证**（难，预留）：LLM 说"修改了 X 功能"，自动 diff 实际文件修改 → 如果 diff 内容和声称不匹配（只改了变量名但声称"修了 bug"）→ escalate

1 和 2 都在 3h 内能做出明显效果，第 3 条偏启发式不总准，第 4 条涉及代码语义理解，LLM 能力瓶颈。
