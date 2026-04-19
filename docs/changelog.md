# 改进历程

## G1-G6 交叉编译能力差距（2026 年第一轮实战修复）

kedo 在交叉编译平台上曾暴露 6 个核心能力差距，经三轮改进已全部修复：

| 编号 | 问题 | 修复方案 | 状态 |
|------|------|----------|------|
| G1 | LLM 幻觉不存在的库名 | `scan_platform_hints()` 扫描真实文件系统注入 prompt | 已修复 |
| G2 | 不会迭代调试 build 错误 | 结构化 build error 解析 + 增量修复循环 | 已修复 |
| G3 | auto_fix 可能越修越坏 | Profile 变更白名单（只允许改 build/notes） | 已修复 |
| G4 | 不了解目标平台构建规范 | `platform_knowledge.py` 平台开发规范注入 | 已修复 |
| G5 | CMakeLists 生成质量差 | 按项目类型提供已验证的 CMakeLists 模板 | 已修复 |
| G6 | 生成非代码文件 | 二进制文件走 ImageMagick/ffmpeg 生成 | 已修复 |

## P3 单 Agent 架构迁移（2026-04 已完成）

旧版本 kedo 同时运行 `AgentLoop`（3406 行刚性流水线）和 `ReactAgent`（LLM 驱动 ReAct 循环）双轨：reasoning_content fallback 救了 ReactAgent 却毒到 AgentLoop 的 JSON parser；`_on_step_unrecoverable(error_text=...)` typo 只在 AgentLoop 侧。

P3 三个里程碑统一到 ReactAgent 单轨：

- **M1 — ReactAgent 加固**：auto_fix / profile_guard / 收敛检测 / 任务链上下文继承 / pause_for_human 工具化
- **M2 — 统一到 ReactAgent**：resume_from_checkpoint + evaluate / commit_candidate / propose_alternatives 工具化 + Kimi prose 收尾 retry
- **M3 — 退役 AgentLoop**：移到 `core/_legacy/` + 22 行 deprecated shim；server.py 不再实例化；routes 通过单独注入的 `version_manager` / `planner` / `evaluator` 访问，不再走 `_agent_loop.X`

后续 LLM quirk 或工具改进只需在 ReactAgent 一处装。AgentLoop shim 计划 4-6 周后实战确认无回归再从 `_legacy/` 真删。

## 待办

- **任务链上下文按 project_path 严格隔离**：当前 `_gather_prior_task_context` 取最近的 failed task，未按项目路径过滤（未来 state_manager 加 project_path 字段后再收紧）
- **escalation 信息密度**：Dashboard 暂停 banner 中展示更完整的上下文（auto_fix 历次 diff + stderr 全文）
- **真删 `core/_legacy/agent_loop.py`**：4-6 周实战无回归后
