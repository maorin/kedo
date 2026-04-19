# kedo 如何应对 Planning Instability（规划不稳定）

## 问题

**Planning Instability**：LLM 生成的 plan 在三个维度上不稳定——

- **generation variance**：同一 requirement 两次生成的 plan 结构/粒度可能差异巨大
- **execution drift**：LLM 拆了 plan 但**不按 plan 跑**，执行途中自行换路线，plan 变成废话
- **no-update-after-progress**：plan 写完就"冻结"，执行中的事实（某步失败 / 发现新约束）不会反馈回 plan

在 kedo 里这几种不稳定会咬人：子任务 panel 显示 12 个 subtask 但 LLM 跑到第 3 个就 respond；plan 里说"用 libnfs"实际代码全是 stub；跑同一需求两次生成完全不同的 plan 数量。

## 答：4 类机制 + 重要 gap

### 1. 规划端的结构约束（Planner prompt）

`core/planner.py` 的 `PLAN_SYSTEM_PROMPT` ~600 行，把"该怎么规划"编码进 system prompt：

| 约束 | 强度 |
|---|---|
| 五步流程（需求/SDD/代码/测试/评估）模板 | 强：prompt 里给 JSON schema + 示例 |
| `≥1 个真代码 subtask` | 强：`_validate_subtask_quality` 结构后校验 |
| `文档 subtask ≤4` | 强：同上 |
| `subtasks 总数 ≤12` | 强：同上（针对 Claude 文档堆砌 quirk） |
| 子任务 step_type 必须在枚举内 | 强：schema 层校验 |
| 子任务粒度（"generate main.c" 而非 "write code"） | 弱：仅 prompt 引导，无校验 |

这些约束把 variance 压在一个狭窄区间 — 不同 LLM、不同 run 生成的 plan 结构上趋同。

### 2. 持久化（可见性 + 可追溯）

`tools/plan_tool.py:execute` 成功后把 plan 写到 `.kedo/state/{task_id}_checkpoint.json` 的 `plan` 字段：

- Dashboard 右侧子任务 panel 实时渲染，用户**看得到 LLM 规划了什么**
- `_save_checkpoint` 每 5 turn 保存时 preserve plan（不会被清）
- `_gather_prior_task_context`（M1）查最近 failed/paused task 时会拉它的 plan subtask title 链注入新 task system prompt

### 3. 执行端的"柔性参考"模式

**关键设计决策**：kedo 的 plan **不是执行脚本，是 LLM 的思考辅助**。ReactAgent 不强制按 plan 跑——这是故意的：

- 真实开发中很多决策要在**看见项目实际状态后才能定**，预先硬规划死板
- LLM 调完 `plan_development` 后，真正执行靠 LLM 在 ReAct 循环里**自己决定下一个工具调用**
- Plan 只是"我打算这么干" 的快照，不是"我必须这么干"

这天然**牺牲了 execution-fidelity**（plan 说要做的事可能不做），换来了灵活性。

### 4. 失败回退的替代机制

当 plan 跑不通时 kedo 有别的兜底：

| 回退机制 | 触发 |
|---|---|
| `auto_fix` 工具 | build/test 失败时不改 plan，直接修代码 |
| 收敛检测 | 同错连续 3 次自动 pause，不再试任何 plan 里的东西 |
| `propose_alternatives` | LLM 识别出需要换技术路线时让用户拍板 |
| 任务链继承 | 本 task 的 plan 没跑完 fail 了，下个 task 拿到上次 plan 摘要继续 |

## 诚实的 gap

| 问题 | 状态 |
|---|---|
| plan diff（同一 task 两次 plan 质量对比） | ❌ 未实现 |
| subtask 状态回写（completed/failed 反映到 plan.subtasks[i].status） | ⚠ schema 支持（`SubTask.status` 字段存在），但 ReactAgent 不写——执行时只往 messages 加 tool_result，不回写 plan |
| 多次调 `plan_development` 的处理 | ⚠ 最新 plan 覆盖旧 plan，历次 plan 不保留——LLM 多次 replan 没历史对比 |
| plan 随进度动态 update | ❌ plan 是"静态快照"，执行中发现的新约束（某库不可用）不会进 plan |
| plan 和实际执行 divergence 检测 | ❌ LLM 说要做的 12 件事，实际可能只做了 3 件就 respond，框架无报警 |

## 实战案例

**switchvideo 2026-04-19**：LLM 初始 `plan_development` 生成的 plan 里包含"实现 libnfs 连接模块"subtask。执行中 `dkp-pacman -Sp switch-libnfs` 失败（提权被拦+包不可得）。LLM 自行决定"用 dirent.h 本地 stub 替代 libnfs"，**没有 replan**。最终任务标 completed，dashboard 子任务 panel 仍显示"libnfs 连接模块" ✓ — 视觉上 plan 完成了，实际实现和 plan 脱节。

这正是 Planning Instability 的典型：**plan 成了装饰，execution 自走流**。

## 现状为何还 work

- 结构约束 + 持久化已经把 **80% 变异** 压在可控范围（generation variance 小）
- "柔性参考"设计让**简单任务**的 plan drift 无害（LLM 自己调整更敏捷）
- 客观回路（build/test）拦下**绝大多数**靠"按 plan 假装做了" 的失真（做没做要过编译）
- Dashboard 子任务 panel 让用户**肉眼**看 plan 和实际代码产出是否匹配——这是当前最有效的"人工对账"

## 下一步候选（未决，按 ROI 排序）

1. **subtask 状态回写**（~2h）：ReactAgent 在 `code_generate` / `build` 等工具结束时自动把对应 subtask 状态更新到 checkpoint.plan.subtasks[i].status；dashboard 子任务 panel 能看到"做了/没做/失败"
2. **plan-execution divergence 报警**（~2h）：`respond` 工具调用时对比 plan.subtasks 里 status 仍是 pending 的，在最终汇报中强制列出"plan 里这几项没做的原因"
3. **多 plan 对比**（~3h）：同一 task 多次调 plan_development 保留历史版本，让 LLM 看到"上次你拆成 12 步，这次只拆 6 步，哪个更对?"
4. **plan 动态 update 工具**（~4h）：新增 `update_plan` 工具让 LLM 显式 "由于 libnfs 不可用，放弃该 subtask，新增 local stub subtask"——不改就覆盖，改就 diff

1 和 2 合起来 ~4h，直接解决"子任务显示和实际脱节"这个视觉体验最差的点。
