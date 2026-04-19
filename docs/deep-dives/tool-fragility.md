# kedo 如何应对 Tool Fragility（工具依赖脆弱）

## 问题

**Tool Fragility**：Agent 把所有"动作"都交给工具，工具链的可靠性就变成系统上限。kedo 的工具会在几种典型方式崩：

- **外部依赖缺失**：`build` 依赖 `$DEVKITPRO` 环境变量、make / cmake 在 PATH 里
- **路径/权限错**：`file_write` 写只读目录、`shell_execute` cwd 不存在
- **LLM 传错参数**：`file_path` 写成绝对路径但基准是相对、工具 required 参数漏传
- **工具间隐式依赖**：LLM 先 `build` 再 `code_generate`（应该反过来）、先 `test_run` 但项目根本没编
- **第三方命令自身崩**：`gcc` OOM、`cmake` 卡死、`git` 遇到 lock file
- **超时配置不合适**：默认 120s `shell_execute` timeout 对交叉编译全量 rebuild 不够

## 答：5 类机制 + 重要 gap

### 1. 每工具 try/except 边界

`tools/base.py:ToolRegistry.execute` 在最外层包裹所有工具调用：

```python
try:
    result = await tool.execute(**kwargs)
except Exception as e:
    return ToolResult(success=False, error=str(e))
```

**保证**：工具内部任何异常不会传到 ReactAgent loop，全部转成 `ToolResult(success=False)` 消息进 LLM context。LLM 看到 error 字段可以自主决定换思路或换工具。

### 2. 环境依赖预检 + 自动探测

`core/project_profile.py:apply_required_env` 在 build 前自动检查 `required_env` 条目：

```json
"required_env": [
  {"name": "DEVKITPRO", "search_paths": ["/opt/devkitpro", ...], "verify_file": "cmake/Switch.cmake"}
]
```

- 环境变量缺失时从 `search_paths` 猜
- 猜到的目录用 `verify_file` 确认真是 devkitPro（不是空目录）
- 成功后 export 到 subprocess 环境，避免 `build` 工具报"`$(DEVKITPRO)` undefined"这种低级错

`scan_platform_hints` 扫真实 lib/include 文件系统，把可用的 `-lXXX` 写到 profile，避免 LLM 幻觉"用 `-lnonexistent`"（G1）。

### 3. 结构性输入校验

| 工具 | 校验 |
|---|---|
| `file_write` / `code_generate` | ProfileGuard 拦 human_verified profile 覆盖 + Makefile/CMake 关键 target 丢失 |
| `shell_execute` | 提权拦截 + DEVNULL stdin + askpass 屏蔽 |
| `plan_development` | `_validate_subtask_quality` 校验 subtask 数量/类型分布 |
| 所有工具 | `task_id` / `project_path` 强制覆盖 LLM 传值（防 LLM 编路径） |

### 4. 失败回路

| 失败类型 | kedo 的回路 |
|---|---|
| 单次工具失败 | `ToolResult(error=...)` 进 messages，LLM 看到后下一轮自主修复 |
| 同错反复失败 | 收敛检测 3 次自动 pause |
| LLM 叫了不存在的工具 | `ToolRegistry.execute` 返 `Tool '{name}' not found`（`tools/base.py:119`），LLM 下轮换工具 |
| 工具参数漏传（required） | BaseTool 执行时 Python 抛 TypeError → try/except 转 error → LLM 补参数 |

### 5. Timeout 与资源护栏

- `shell_execute` 默认 120s，LLM 可在 args 里覆盖（`timeout: 600`）
- `ProjectProfileManager.MAX_PROFILE_REGENS=3` 防 LLM 反复 regen profile
- `max_tool_output_chars=4000` 截断超长 stdout，避免 OOM 级别的 messages 膨胀
- `subprocess stdin=DEVNULL` 防交互式命令占 tty 永不返回

## 诚实的 gap

| 问题 | 状态 |
|---|---|
| 工具间依赖不建模 | ❌ LLM 可先 `build` 后 `code_generate`，工具不会拒 |
| 启动时工具 health check（make/cmake/git 可用性） | ❌ 未实现；出错只能运行时撞 |
| shell_execute timeout 大小自适应 | ❌ 120s 默认太紧，大项目要 LLM 每次手动传 |
| 第三方命令 OOM / 资源限制 | ❌ 不设 ulimit，大 project 可能拖死机器 |
| 工具 required 参数的 schema 强校验 | ⚠ Python 层面抛 TypeError 当做 error 返回；但 LLM 看到的错误信息不如"`parameter 'X' is required but missing`"友好 |
| 并行工具调用 | ❌ ReactAgent 串行执行所有 tool_calls，LLM 想并发跑两个 `file_read` 也只能等 |

## 现状为何还 work

- **最大依赖 `$DEVKITPRO`** 已被 `required_env` + `apply_required_env` 自动探测覆盖
- **ProfileGuard** 替 LLM 挡下"写坏 Makefile" 这个工具副作用最大的场景
- **`Unknown tool`** 返回让 LLM 的幻觉工具名无害化
- **shell 沙箱** 把 tty 劫持这个最棘手的工具副作用堵死了
- LLM 是**有弹性的**：单次工具失败 → 看 error → 换参数/换工具，能自我恢复的场景远多于不能

## 实战案例

switchvideo 首次 run 遇到的工具依赖问题链（都在 M1-M3 加固前）：
- `dkp-pacman -Sp switch-libnfs` 失败（工具没装 + 提权被拦）→ LLM 换 `pacman -Sp` 又失败 → 换 `curl` 下载又失败 → 放弃并用 local stub
- build 编译器报 `struct SwsContext` 需要 struct 前缀（ffmpeg API 漂移）→ LLM 靠 stderr 错误提示 3 轮修完
- Makefile 重写丢了 `switch_rules` include（M1 之后 ProfileGuard 会拦）

所有这些都没让 task 彻底卡死，说明**工具失败 + LLM 弹性恢复** 是可跑的路径。

## 下一步候选（未决，按 ROI 排序）

1. **启动时工具 health check**（~2h）：`kedo` 启动时跑 `make --version` / `cmake --version` / `git --version`，缺的在 banner 警告，比运行中才撞墙体验好
2. **工具间依赖声明**（~3h）：每个工具加 `requires_before: list[str]` 字段（`build` requires `code_generate`），ReactAgent 在调工具前校验序列
3. **timeout 自适应**（~1h）：`shell_execute` 从 project_profile 读 `default_timeout`，大项目 profile 里写 600s，LLM 不用每次手动传
4. **并行工具执行**（~4h）：对 `is_read_only=True` 的工具允许并发（多个 `file_read` 同时跑）

前 3 条都是工程细节，第 4 条真正提速但要小心 race condition。
