# LLM 适配

## 支持的提供商

| 提供商 | 配置值 | 默认模型 | API Key 环境变量 | 说明 |
|--------|--------|----------|------------------|------|
| Kimi Code | `kimi-code` | `kimi-k2.5` | `KIMI_API_KEY` | 编程专用端点（推荐） |
| Kimi | `kimi` | `kimi-k2.5` | `KIMI_API_KEY` | 通用 Moonshot 端点 |
| Anthropic | `anthropic` | `claude-sonnet-4-6` | `ANTHROPIC_API_KEY` | Claude 系列 |
| OpenAI | `openai` | `gpt-4` | `OPENAI_API_KEY` | GPT 系列 |
| Ollama | `ollama` | `codellama` | 无需 | 本地模型 |
| Mock | `mock` | — | 无需 | 模拟模式，用于测试/演示 |

## 适配架构

所有提供商实现统一的 `BaseLLMClient` 接口：

```python
class BaseLLMClient:
    async def chat(messages: list[dict]) -> str          # 非流式
    async def stream_chat(messages: list[dict]) -> str   # 流式（逐 token yield）
```

消息格式统一为 `[{"role": "system/user/assistant", "content": "..."}]`。各提供商差异在客户端内部处理（如 Anthropic 需要分离 system 消息）。

## 对接新 LLM 的步骤

1. 在 `api/server.py` 中新建 `XxxClient(BaseLLMClient)` 类，实现 `chat()` 和可选的 `stream_chat()`
2. 在 `create_llm_client()` 工厂函数中添加 `elif provider == "xxx":` 分支
3. 在 `kedo.yaml` 中配置 `llm_provider: "xxx"` 和对应的 `model`

## Prompt 模板与 LLM 适配要点

kedo 有 4 个核心 prompt 模板，对接新 LLM 时需确保其能正确遵循这些结构化输出要求：

| 模块 | Prompt 位置 | 输出格式 | 适配注意 |
|------|-------------|----------|----------|
| Planner | `core/planner.py` | JSON（subtask 列表） | ~600 行 system prompt，含五步流程定义 + 文档模板，需要 LLM 有强指令遵循能力 |
| Evaluator | `core/evaluator.py` | JSON（四维度评分） | 需要 LLM 严格按 schema 输出，弱模型易漏字段或分数格式错 |
| Code Generator | `tools/code_generator.py` | 纯代码（无 markdown 包裹） | 动态注入平台知识 + CMakeLists 模板，prompt 较长（~2K token） |
| Auto Fix | `tools/auto_fix_tool.py` | JSON（diagnosis + patch） | 需要 LLM 输出完整文件内容而非 diff，弱模型可能输出截断或混入注释 |

**已知的 LLM 兼容性差异**：
- **Kimi K2.5**（当前主力）：指令遵循强，JSON 输出稳定，但偶尔幻觉不存在的库名（已通过 G1 平台扫描缓解）
- **Anthropic Claude**：system prompt 需从 messages 分离单独传入（客户端已处理），JSON 遵循能力强
- **OpenAI GPT-4**：兼容但未深度测试，code_generator 的 "纯代码无 markdown" 要求可能需要额外 prompt 强调
- **Ollama 本地模型**：受模型能力限制，复杂的 planner prompt 可能无法正确遵循，建议仅用于简单项目
- **对接其他 LLM 时**：重点验证 (1) JSON 结构化输出是否稳定 (2) 长 system prompt 是否被截断 (3) "输出纯代码" 指令是否被遵循

## Claude (Anthropic) 对接

默认模型 `claude-sonnet-4-6`。`AnthropicClient` 提供两级校验和错误归类：

- `validate_key_format(key)` — 本地只检查 `sk-ant-` 前缀 + 长度
- `validate()` — 实网 ping（`max_tokens=1`）确认 key + 模型可用
- 异常按 401 / 403 / 404 / 429 / 5xx / 网络分类返回中文可读消息

### 录入 key 的三种方式

1. **REPL `/login`**：交互选 `1` Claude → 输 key → 自动做格式+连通性校验 → 成功则热切换 + 持久化到 `~/.config/kedo/config.yaml`
2. **环境变量**：`export ANTHROPIC_API_KEY=sk-ant-...` + `export KEDO_PROVIDER=anthropic`
3. **HTTP API** (运行时热切换)：
   ```bash
   # 只校验不切换
   curl -XPOST http://host:8000/api/llm/validate \
     -H 'content-type: application/json' \
     -d '{"provider":"claude","api_key":"sk-ant-..."}'

   # 切换 + 默认持久化（传 "persist": false 可以只内存切换不落盘）
   curl -XPOST http://host:8000/api/llm/switch \
     -H 'content-type: application/json' \
     -d '{"provider":"claude","api_key":"sk-ant-...","model":"claude-sonnet-4-6"}'
   ```

### 配置持久化

`/llm/switch` 成功后会把 `llm_provider / model / anthropic_api_key / kimi_*` **合并**写入 `~/.config/kedo/config.yaml`（权限 0600，保留文件里其他键）。下次 `kedo <project>` 启动时直接复用，不会回退到旧 provider。

## 问答/闲聊快速通道

输入被识别为闲聊或元信息问询（如"你是什么模型？"、"你能做什么？"）时，REPL 端 `_is_chat_query` 会直接走 `POST /api/chat` 端点**跳过 task 创建**，直接把问题交给底层 LLM 流式回答。判定规则：

- 命中身份/模型/能力类关键词，**或** 短输入 (≤30 字) 且以问号结尾
- 同时不能出现开发动词（`实现/添加/构建/implement/...`）或 bug 关键词（`bug/崩溃/报错/...`），否则让 ReactAgent 走完整 ReAct 循环接管

目的是避免把一句问话当成开发需求拆成 build/test/evaluate 子任务、还被 evaluator 按 code review 打 0 分进迭代循环。
