# 使用参考

## Web Dashboard

启动后访问 `http://localhost:8000`：

### 工作台视图
- **左侧**：任务列表（创建/暂停/恢复/停止）
- **中间**：控制台实时输出 + 日志 + 讨论面板
- **右侧**：子任务计划（进度条 + 步骤状态）
- **底部**：新建任务输入栏

### 任务暂停与恢复
当 kedo 遇到无法自动修复的错误时：
- 右侧面板顶部显示**橙色暂停 banner**
- 展示：失败步骤 + 完整错误信息 + 修复建议
- 提供**文本输入框**写入建议，点"恢复"→ kedo 带着你的指导重新规划
- 无建议直接点"恢复"→ 从暂停点继续

### 产品需求总结
讨论面板自动汇总所有任务对话，LLM 生成：
- 功能需求清单（✅ 已完成 / 🔧 进行中 / ❌ 失败）
- 产品当前状态概要
- 可复用的需求提示词
- 下一步改进方向

任务完成/失败时自动刷新，也可手动点"刷新总结"。

### 其他视图
- **文档**：浏览和编辑项目文档，Mermaid 图表渲染
- **代码**：VS Code 风格目录树 + 代码预览
- **打包**：构建产物 + 候选版本
- **部署**：部署环境检测 + 分步准备指南
- **测试**：测试结果 + 覆盖率

## REPL 命令

| 命令 | 说明 |
|------|------|
| `/help` | 显示帮助 |
| `/status` | 查看当前任务状态 |
| `/flow` | 显示流程图 |
| `/pause` | 暂停当前任务 |
| `/resume` | 恢复暂停的任务 |
| `/continue` | 从检查点续接历史任务 |
| `/candidates` | 查看候选版本 |
| `/discuss` | 参与闭环讨论 |
| `/history` | 查看迭代历史 |
| `/login` | 切换 LLM 提供商 |
| `/web` | 打开 Dashboard |
| `/config` | 查看配置 |
| `/verbose [on\|off\|toggle]` | 控制台详细模式开关（默认 on，显示完整 args/output/summary 不截断） |
| `/quit` | 退出 |

### 控制台底栏（固定状态栏）

REPL 启动时用 ANSI DECSTBM 把终端切成两个区域：

- **滚动区**（第 1 行 ~ H-1 行）：事件流持续滚动（LLM 请求/响应、工具执行、step 完成/失败）
- **固定底栏**（第 H 行）：永远钉在屏幕最底部，显示 `provider/model │ Task:xxx │ 状态 │ 当前 step │ 进度条`

事件流不会再穿插状态栏，按 `Ctrl+L` 或事件刷屏都不会冲掉底栏。窗口 resize（SIGWINCH）会自动重设滚动区域。退出时 `\033[r` 恢复整屏滚动，shell prompt 不会被污染。

## API 接口

| 方法 | 路径 | 说明 |
|------|------|------|
| POST | `/api/tasks` | 创建新任务 |
| GET | `/api/tasks` | 列出所有任务 |
| GET | `/api/tasks/resumable` | 列出可续接的历史任务 |
| GET | `/api/tasks/{id}` | 获取任务详情（含 plan + eval_report） |
| POST | `/api/tasks/{id}/resume-checkpoint` | 智能续接（带 additional_context） |
| POST | `/api/tasks/{id}/pause` | 暂停任务 |
| POST | `/api/tasks/{id}/resume` | 恢复暂停的任务 |
| GET | `/api/tasks/{id}/candidates` | 获取候选版本 |
| GET | `/api/tasks/{id}/discussion` | 获取讨论状态 |
| POST | `/api/tasks/{id}/discussion/input` | 提交讨论意见 |
| GET | `/api/product-summary` | 产品需求智能总结（LLM 生成） |
| GET | `/api/code/status` | 代码监控 |
| GET | `/api/code/file` | 读取代码文件 |
| GET | `/api/build/status` | 构建状态 |
| GET | `/api/deploy/guide` | 部署指南 |
| GET | `/api/deploy/environment` | 环境检测 |
| WS | `/api/ws` | WebSocket 实时事件流 |

## 配置

kedo 按以下优先级查找配置：

1. `kedo.yaml` / `kedo.yml`（项目目录）
2. `.kedo.yaml`（项目目录）
3. `~/.config/kedo/config.yaml`（全局）
4. 环境变量（最高优先级）

```yaml
# kedo.yaml
llm_provider: "kimi"              # anthropic / kimi / kimi-code / openai / ollama / mock
model: "kimi-k2.5"
max_retries: 3                    # 子任务最大重试次数
auto_fix: true                    # LLM 自动修复
min_eval_score: 70                # 最低评估通过分数
max_iterations: 5                 # 最大闭环迭代次数
max_profile_regens: 3             # Profile 最大重生成次数
auto_discussion: true             # AI 自动选择修复方案
doc_language: "zh"                # 文档语言
host: "0.0.0.0"
port: 8000
```

| 环境变量 | 说明 |
|----------|------|
| `ANTHROPIC_API_KEY` | Claude API Key |
| `KIMI_API_KEY` | Kimi API Key |
| `OPENAI_API_KEY` | OpenAI API Key |
| `KEDO_PORT` | 服务端口 |
| `KEDO_HOST` | 绑定地址 |
| `KEDO_PROVIDER` | LLM 提供商 |
