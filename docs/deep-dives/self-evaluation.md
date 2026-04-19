# kedo 如何解决 Self-evaluation Drift（自评失真）

## 问题

**Self-evaluation Drift**：LLM 既是 executor 又是 judge，天然有 confirmation bias——它对自己生成的代码/决策会系统性偏乐观，对失败有下意识的合理化。具体在 kedo 体现为：

- **"我完成了"但没调 respond**（Kimi prose ending：`"编译成功 🎉 做了 5 件事..."` → 框架标 completed，实际可能还有未检 issue）
- **"我修好了"但 patch 逻辑错**（auto_fix 说诊断对了，patch 其实没修到真错误）
- **"这个方案 80 分"但实际漏洞百出**（Evaluator 给自己生成的代码打高分）
- **"这次不一样能修"**（LLM 连续试 3 次同样的修法都失败仍自信）
- **`ai_confidence: 0.9`** 但代码跑起来功能不对（commit_candidate 的 confidence 字段完全靠 LLM 自觉）

kedo 在这几个场景下到底怎么防失真？

## 答：防御纵深的 5 类机制 + 重要 gap

### 1. 客观校验替 LLM 判断（硬事实回路）

最可靠的一层 — **不依赖 LLM 说什么**，直接看事实：

| 机制 | 位置 | 检查什么 |
|---|---|---|
| build exit_code | `tools/build_tool.py` | 编译器返回 0 才算成功，LLM 说"build OK"不算 |
| test_run 结果 | `tools/test_runner.py` | pytest/go test 等的 exit code + pass/fail count |
| ProfileGuard 结构检查 | `tools/profile_guard.py` | Makefile 的 `all:` target 是否丢失、CMake 关键 call 是否还在——正则算法判断，非 LLM |
| 收敛检测 fingerprint | `core/react_agent.py:_error_fingerprint` | 归一化后 SequenceMatcher 相似度 ≥ 0.85 算同一错，算法判断 |
| 提权命令黑名单 | `tools/shell_executor.py` | token 切分匹配 `sudo/su/pkexec/doas`，拒绝 |

**这一层永远信得过**。LLM 再会自我吹嘘也骗不过 exit_code。

### 2. 强制再验证（LLM 说完了不算，必须过回路）

LLM 汇报成功后立刻跑客观校验，让自评落到事实上：

| 场景 | 强制回路 |
|---|---|
| `auto_fix` 返回 `success=True + diagnosis=...` | ReactAgent 在工具结果上**不做奖赏判断**，LLM 下一步自主决定再 `build` — 不过 build 就没证据说修好了 |
| prose 结尾（LLM 文本汇报但没调 respond） | 回灌 user "请用 respond 或继续工具"，强制 LLM 第二轮显式表态 |
| `evaluate` 打分 | 工具内部**从磁盘读真实文件内容**传给 reviewer prompt，不相信 LLM 传进来的 code_changes 描述 |
| `commit_candidate` | 工具签名强制要求 `build_success: true`（但这条目前还靠 LLM 自律，见 gap） |

### 3. 负样本记忆（防 LLM 忘记自己试过什么）

LLM 天然"短记忆 + 乐观"，容易反复试同样的修法还觉得"这次不一样"。kedo 把历次失败强塞进 prompt：

| 机制 | 位置 | 内容 |
|---|---|---|
| `prior_attempts` 注入 | `tools/auto_fix_tool.py:execute` 构 prompt 时 | 最近 3 次的 `build_command + stderr_excerpt + patched_file`，标为 "PREVIOUS FAILED ATTEMPTS (avoid repeating)" |
| 任务链上下文继承 | `core/react_agent.py:_gather_prior_task_context` | 上个 failed task 的 plan subtask 链 + 卡点，注入新 task system prompt |
| 收敛检测 fingerprint 库 | `core/react_agent.py:_failure_fingerprints` | 本 task 内所有 `(tool_name, error_fingerprint)`，同对重复 3 次就强制 pause，不听 LLM |

### 4. 多角色 prompt 隔离（表面上的 checks & balances）

同一个 LLM 被分成多个角色调用，每个角色 prompt 完全不同，**至少减少同一上下文里的 confirmation bias**：

| 角色 | system prompt 主干 | 任务 |
|---|---|---|
| Code Generator | "expert code generator" | 生成代码 |
| Evaluator | "senior code reviewer performing multi-dimensional evaluation" | 对同一代码**反向**挑刺 |
| Auto Fix | "senior build/test debugging expert" | 诊断 stderr 找 root cause |
| Planner | "software architect, five-step process" | 拆解任务 |

Evaluator 的 prompt 明确列 4 维度 + 评分标准 + 返 JSON，逼 LLM 换"审查者"视角。**不完全根治 bias，但比单 agent 全程乐观好**。

### 5. 人工兜底（LLM 真撞墙时换真 judge）

LLM 判断不了或陷死循环时，显式把方向盘交给人类：

| 工具 | 触发 | 行为 |
|---|---|---|
| `pause_for_human` | LLM 自评"我搞不定" 主动调 | 发 escalation 事件 + pause task + dashboard banner 等用户建议 |
| `propose_alternatives` | LLM 识别出 ≥2 条可行路线（libnfs vs SMB） | 结构化 2-3 个 option，dashboard 等用户选 |
| 收敛检测自动触发 | 算法检测到死循环（不靠 LLM 自觉） | 强制 `pause_for_human`，**即便 LLM 还在说"这次不一样"** |

## 诚实的 gap：kedo 当前没做的事

| 问题 | 状态 |
|---|---|
| 同一 LLM 自评结构性 bias | ❌ Evaluator 和 Code Generator 本质还是同一个模型，只是 prompt 换了视角 |
| 跨模型 judge（Kimi 写，Claude 审） | ❌ 未实现。`api/server.py` 只有一个活跃 LLM 实例 |
| `ai_confidence` 字段校准 | ❌ LLM 自己填，没有事后"过去 ai_confidence 0.9 的 candidate 实际通过率是多少"的统计 |
| `commit_candidate` 前置事实检查 | ❌ 工具签名要求 `build_success=True`，但没在工具内部重跑一次 build 验证，靠 LLM 自律 |
| 人类真实运行回路 | ❌ build 成功 ≠ 代码跑对。.nro 编出来不代表在 Switch 上真播得了视频 |
| `ai_summary` 事后矫正 | ❌ LLM 自己写的 "summary"，跑失败了也没机制回头改正 |

## 为什么现状还 work

- 最硬的 bias bypass 是 **build exit_code** — 这一关无法 fake，撑住了 80% 的 Self-evaluation Drift 场景
- **auto_fix 的 re-build loop 天然反自评**：LLM 说"修好了"不算，编译器说算
- **收敛检测** 在 LLM 陷入"这次不一样" 时**直接切断**，不听 LLM 解释
- **人工兜底** 捕捉最后 5% — 用户一眼就能看出 "build 成功但代码是 stub" 这类失真
- ProfileGuard 的**结构检查**专门拦"LLM 以为自己改好了但实际破坏了关键 target" 这类盲点

## 实战中 Self-evaluation Drift 真发生过的案例

**switchvideo 2026-04-19 13:51 事件**：
- LLM 用 prose 汇报："编译成功 🎉 做了 5 件事..."
- Checkpoint 没查 build 产物存不存在
- ReactAgent 直接标 completed 100%
- 用户去看项目目录：.nro **确实存在**，build 真的成功了 ✅
- 但 `nfs_client.c` 被 LLM 改成**本地文件系统 stub**，根本没接真 NFS
- LLM 的"成功"是编译层面的成功，**功能层面 silently degraded**，LLM 汇报时也没明说

这就是 **Self-evaluation Drift 最棘手的形态**：LLM 没撒谎（build 真过了），但它选择了个用户没要求的简化路径（stub NFS）还当成"完成" — 功能预期和实现偏离，客观校验回路（只测 build）覆盖不到这一层。

## 下一步候选（未决）

按 ROI 排序：

1. **commit_candidate 前置事实检查**（~1h，小，高回报）：
   - 工具内部在记录 candidate 前自动跑一次 build + 若 profile.test.strategy != skip 跑一次 test
   - 结果硬注入 candidate record，LLM 传的 `build_success` 只作对比
   - 预期效果：防止 LLM 在没跑 build 的情况下直接 commit "我觉得应该过了" 的版本

2. **跨模型 judge**（~3h，中，高回报）：
   - 配置里允许独立的 `evaluator_llm_provider`
   - Evaluator 默认用和主 LLM 不同的提供商（主 Kimi → eval 用 Claude）
   - 分歧超过 20 分触发 `propose_alternatives` 让用户拍板
   - 预期效果：破同模型 bias，catch 那种"主 LLM 和 evaluator 对自己一见钟情"的场景

3. **`ai_confidence` 校准统计**（~2h，中，中回报）：
   - 事后统计 `ai_confidence` 和 `candidate_success rate` 的相关性
   - LLM 过度自信时（ai_confidence=0.9 但实际通过率 0.3）扣权重再呈现给用户
   - 需要先跑一段时间积累数据才有效

4. **语义级回路（最难）**：自动从 requirement 里提关键 assertion（"连 NFS"、"播放视频"）→ 生成验证 test → 跑 → 不通过触发换方案。预期需要 1-2 周 + 模型能力本身瓶颈。

前 3 条都是工程问题，没哲学障碍；第 4 条涉及需求→验证转换的 LLM 能力本身。
