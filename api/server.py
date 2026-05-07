"""
FastAPI 服务主入口

启动 API 服务器，初始化所有组件，注册路由
"""
from __future__ import annotations

import logging

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

from api.routes import router, set_dependencies
from api.websocket import ws_manager
# P3-M3: AgentLoop 已退役，导入移到 _legacy；当前 server 不再使用
# from core.agent_loop import AgentLoop  # noqa
from core.evaluator import Evaluator
from core.memory import AgentMemory
from core.planner import Planner
from core.react_agent import ReactAgent
from core.reject_tracker import RejectTracker
from core.reviewer import Reviewer
from core.role_swap import RoleSwapManager
from core.state_manager import StateManager
from tools.base import ToolRegistry
from tools.build_tool import BuildTool
from tools.code_generator import CodeGeneratorTool
from tools.plan_tool import PlanTool
from tools.pause_tool import PauseForHumanTool
from tools.auto_fix_tool import AutoFixTool
from tools.evaluate_tool import EvaluateTool
from tools.commit_candidate_tool import CommitCandidateTool
from tools.propose_alternatives_tool import ProposeAlternativesTool
from tools.propose_charter_change_tool import ProposeCharterChangeTool
from tools.fetch_crash_report_tool import FetchCrashReportTool
from tools.file_tool import FileReadTool, FileSearchTool, FileWriteTool
from tools.git_tool import GitTool
from tools.respond_tool import RespondTool
from tools.emulator_test import EmulatorTestTool
from tools.host_test import HostTestTool
from tools.shell_executor import ShellExecutorTool
from tools.static_check import StaticCheckTool
from tools.test_runner import TestRunnerTool

logger = logging.getLogger(__name__)


def create_app(config: dict = None) -> FastAPI:
    """
    创建并配置 FastAPI 应用

    Args:
        config: 配置字典，包含 LLM API key、项目路径等
    """
    config = config or {}

    app = FastAPI(
        title="kedo",
        description="AI-powered development assistant with full pipeline automation",
        version="0.1.0",
    )

    # CORS (允许 Dashboard 跨域访问)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    # ----------------------------------------------------------
    # 初始化组件
    # ----------------------------------------------------------

    # LLM 客户端 (需要替换为实际的 LLM client)
    llm_client = create_llm_client(config)

    # 状态管理器
    state_manager = StateManager(
        storage_dir=config.get("storage_dir", ".kedo/state")
    )

    # 记忆管理器
    memory = AgentMemory(
        max_context_chars=config.get("max_context_chars", 120_000)
    )

    # ProjectProfile manager + 写拦截 guard（共享给 BuildTool / FileWriteTool / CodeGeneratorTool）
    from core.project_profile import ProjectProfileManager
    from tools.profile_guard import ProfileGuard
    profile_manager = ProjectProfileManager()
    profile_guard = ProfileGuard(
        profile_manager=profile_manager,
        project_path=config.get("project_path", "."),
    )

    # 工具注册
    shell = ShellExecutorTool(
        working_dir=config.get("project_path", "."),
        sandbox_mode=config.get("sandbox_mode", True),
        profile_manager=profile_manager,
    )
    tool_registry = ToolRegistry()
    tool_registry.register(CodeGeneratorTool(llm_client, config=config, profile_guard=profile_guard))
    tool_registry.register(shell)
    tool_registry.register(TestRunnerTool(shell))
    tool_registry.register(GitTool(shell))
    tool_registry.register(FileReadTool())
    tool_registry.register(FileWriteTool(profile_guard=profile_guard))
    tool_registry.register(FileSearchTool())
    tool_registry.register(RespondTool())
    tool_registry.register(BuildTool(profile_manager=profile_manager, llm_client=llm_client))
    tool_registry.register(StaticCheckTool(profile_manager=profile_manager))
    tool_registry.register(HostTestTool(profile_manager=profile_manager, llm_client=llm_client))
    emulator_tool = EmulatorTestTool(profile_manager=profile_manager, llm_client=llm_client)
    tool_registry.register(emulator_tool)

    # Planner & Evaluator
    planner = Planner(llm_client, memory, config=config)
    evaluator = Evaluator(llm_client, memory, config=config)

    # Reviewer — 方案 C（双 Agent 对抗）的独立审查 Agent
    # reviewer_provider=none → reviewer_llm=None → Reviewer 不激活，回退到单 Agent 行为
    reviewer_llm = create_reviewer_llm_client(config)
    reviewer = (
        Reviewer(
            llm_client=reviewer_llm,
            memory=memory,
            config=config,
            min_score=config.get("reviewer_min_score", config.get("min_eval_score", 70)),
        )
        if reviewer_llm is not None
        else None
    )
    if reviewer is not None:
        logger.info(
            f"Reviewer Agent active: provider={reviewer.provider_name} "
            f"model={reviewer.model_name} min_score={reviewer.min_score}"
        )
    else:
        logger.info("Reviewer Agent disabled (reviewer_provider=none)")

    # PlanTool 依赖 planner，必须在 planner 创建后注册
    tool_registry.register(PlanTool(planner=planner, memory=memory, state_manager=state_manager))
    tool_registry.register(PauseForHumanTool(state_manager=state_manager, event_bus=state_manager.event_bus))
    tool_registry.register(AutoFixTool(llm_client=llm_client, profile_manager=profile_manager, profile_guard=profile_guard))
    tool_registry.register(EvaluateTool(
        evaluator=evaluator,
        reviewer=reviewer,
        min_score=config.get("min_eval_score", 70),
    ))
    tool_registry.register(ProposeAlternativesTool(
        state_manager=state_manager,
        event_bus=state_manager.event_bus,
        # 默认 600s（10 分钟）— 实战发现 1800s 太长，常常用户没注意 dashboard 提示就被
        # 30 分钟超时 + 3 连失败拖死整个 task。配置兼容旧字段 discussion_timeout_s。
        timeout_s=config.get("propose_alternatives_timeout_s",
                             config.get("discussion_timeout_s", 600)),
    ))
    # 方案 C：charter 变更提案工具（frozen charter 的合法变更通道）
    tool_registry.register(ProposeCharterChangeTool(
        state_manager=state_manager,
        event_bus=state_manager.event_bus,
        timeout_s=config.get("propose_charter_change_timeout_s", 1800),
    ))
    # 真机 coredump 拉取 + 解析（T3 模拟器搁置后的实用替代）
    tool_registry.register(FetchCrashReportTool(
        state_manager=state_manager,
        event_bus=state_manager.event_bus,
        profile_manager=profile_manager,
        timeout_s=config.get("fetch_crash_report_timeout_s", 1800),
    ))
    # commit_candidate 需要 version_manager — 必须在 version_manager 创建后再注册（下面）

    # Version Manager (候选版本管理)
    from core.version_manager import VersionManager
    version_manager = VersionManager(
        event_bus=state_manager.event_bus,
        git_tool=GitTool(shell) if config.get("git_enabled", True) else None,
    )
    # 升级护栏共享对象：commit_candidate 多次被 Reviewer 拒后强制 Producer
    # 升级（pause_for_human / propose_alternatives），不让它绕过 respond 假装完成
    reject_tracker = RejectTracker(
        escalation_threshold=config.get("commit_reject_escalation_threshold", 3),
    )

    # 角色对换管理器：到阈值时优先 swap LLM 角色再试一次（默认关闭）
    # bind 在 react_agent 创建后调用
    role_swap = RoleSwapManager(
        enabled=config.get("enable_swap_on_reject", False) and reviewer is not None,
    )

    # commit_candidate 工具（依赖 version_manager + 可选 reviewer pre-commit gate + reject_tracker + role_swap + emulator gate）
    tool_registry.register(CommitCandidateTool(
        version_manager=version_manager,
        reviewer=reviewer,
        pre_commit_gate=config.get("reviewer_pre_commit_gate", True),
        reject_tracker=reject_tracker,
        role_swap=role_swap,
        emulator_tool=emulator_tool,  # T3 — profile.emulator.enabled 控制是否实际跑
    ))

    # ReactAgent (LLM 驱动的 ReAct Agent — P3 后唯一主路径)
    react_agent = ReactAgent(
        llm_client=llm_client,
        tool_registry=tool_registry,
        state=state_manager,
        memory=memory,
        config=config,
        reject_tracker=reject_tracker,
        role_swap=role_swap,
        reviewer=reviewer,
    )

    # role_swap 需要 react_agent + reviewer 都创建好后才能 bind
    role_swap.bind(react_agent=react_agent, reviewer=reviewer)
    if role_swap.enabled:
        logger.info(
            f"RoleSwap active: producer↔reviewer swap on commit_candidate reject "
            f"threshold={reject_tracker.escalation_threshold}"
        )

    # 注册 WebSocket 事件处理
    state_manager.event_bus.subscribe("*", ws_manager.handle_event)

    # 注入依赖到路由（含 create_llm_client 引用，避免循环导入）
    # P3-M3: agent_loop=None 表示退役；planner/evaluator/version_manager 单独传入
    # 让 routes 不再通过 _agent_loop.X 访问这些组件
    set_dependencies(
        agent_loop=None,
        state_manager=state_manager,
        create_llm_client_fn=create_llm_client,
        project_path=config.get("project_path", "."),
        react_agent=react_agent,
        version_manager=version_manager,
        planner=planner,
        evaluator=evaluator,
        reviewer=reviewer,
        role_swap=role_swap,
    )

    # 注册路由 — 必须在 StaticFiles 之前
    app.include_router(router, prefix="/api")

    # 静态文件 (Dashboard) — 使用包相对路径，避免 CWD 不同时找不到
    from pathlib import Path as _Path
    _package_dir = _Path(__file__).resolve().parent.parent  # kedo/
    _dashboard_dir = _package_dir / "dashboard"
    try:
        if _dashboard_dir.is_dir():
            app.mount("/dashboard", StaticFiles(directory=str(_dashboard_dir), html=True), name="dashboard")
        else:
            logger.warning(f"Dashboard directory not found at {_dashboard_dir}, skipping")
    except Exception:
        logger.warning("Dashboard static files mount failed, skipping")

    # 禁用 dashboard 静态资源的浏览器缓存：开发期改了 HTML/JS 后无需手动 Cmd+Shift+R
    @app.middleware("http")
    async def _no_cache_dashboard(request, call_next):
        response = await call_next(request)
        path = request.url.path
        if path == "/" or path.startswith("/dashboard"):
            response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
            response.headers["Pragma"] = "no-cache"
            response.headers["Expires"] = "0"
        return response

    # 根路由重定向到 Dashboard
    from fastapi.responses import RedirectResponse

    @app.get("/")
    async def root():
        return RedirectResponse(url="/dashboard")

    # ----------------------------------------------------------
    # 启动/关闭事件
    # ----------------------------------------------------------

    @app.on_event("startup")
    async def startup():
        logger.info("kedo starting up...")

    @app.on_event("shutdown")
    async def shutdown():
        logger.info("kedo shutting down...")

    return app


def create_llm_client(config: dict):
    """
    创建 LLM 客户端

    支持多种 LLM 后端 (优先级: 环境变量 > 配置文件):
    - anthropic: Anthropic Claude API (需要 ANTHROPIC_API_KEY)
    - openai: OpenAI API (需要 OPENAI_API_KEY)
    - ollama: 本地模型
    - mock: 模拟 LLM，无需 API Key，用于测试/演示

    strict_mode: 如果为 True，缺少 API Key 时直接报错而非回退到 Mock
    """
    import os

    provider = config.get("llm_provider", "anthropic")
    strict_mode = config.get("strict_mode", False)

    def _handle_missing_key(provider_name: str, env_var: str, help_url: str = ""):
        """处理 API Key 缺失：strict_mode 下报错，否则回退到 Mock 并标记"""
        if strict_mode:
            raise RuntimeError(
                f"[strict_mode] {provider_name} 需要 API Key，但未找到 {env_var}。\n"
                f"请设置环境变量 {env_var} 或在 config.yaml 中配置。\n"
                + (f"获取 Key: {help_url}" if help_url else "")
            )
        logger.warning(f"No {env_var} found, falling back to Mock LLM")
        logger.warning(f"Set env var {env_var} or edit config.yaml to use real {provider_name}")
        # ★ 标记回退状态，供 REPL 检测
        config["_mock_fallback"] = True
        config["_mock_fallback_reason"] = f"未找到 {env_var}，已自动回退到 Mock 模式"
        config["_intended_provider"] = provider_name
        return MockLLMClient()

    if provider == "mock":
        logger.info("Using Mock LLM (no API key required)")
        config["_mock_fallback"] = False  # 用户主动选择 mock，不算回退
        return MockLLMClient()

    elif provider == "anthropic":
        api_key = os.environ.get("ANTHROPIC_API_KEY") or config.get("anthropic_api_key", "")
        if not api_key:
            return _handle_missing_key("Claude (Anthropic)", "ANTHROPIC_API_KEY", "https://console.anthropic.com")
        config["_mock_fallback"] = False
        return AnthropicClient(
            api_key=api_key,
            model=config.get("model", DEFAULT_ANTHROPIC_MODEL),
        )

    elif provider == "openai":
        api_key = os.environ.get("OPENAI_API_KEY") or config.get("openai_api_key", "")
        if not api_key:
            return _handle_missing_key("OpenAI", "OPENAI_API_KEY")
        config["_mock_fallback"] = False
        return OpenAIClient(
            api_key=api_key,
            model=config.get("model", "gpt-4"),
        )

    elif provider == "kimi-code":
        # Kimi Code 2.5 — 编程专用端点
        api_key = os.environ.get("KIMI_API_KEY") or os.environ.get("MOONSHOT_API_KEY") or config.get("kimi_api_key", "")
        if not api_key:
            return _handle_missing_key("Kimi Code 2.5", "KIMI_API_KEY", "https://platform.moonshot.ai")
        config["_mock_fallback"] = False
        # ★ 自动纠正模型名：如果 model 不是 kimi 系列（比如误配了 claude/gpt），用默认值
        model = config.get("model", "kimi-k2.5")
        if not model.startswith("kimi") and not model.startswith("moonshot"):
            logger.warning(f"模型 '{model}' 不兼容 kimi-code，自动切换为 kimi-k2.5")
            model = "kimi-k2.5"
        return KimiClient(
            api_key=api_key,
            model=model,
            base_url=config.get("kimi_base_url", "https://api.kimi.com/coding/v1"),
        )

    elif provider == "kimi":
        # Kimi K2.5 — 通用 Moonshot 端点
        api_key = os.environ.get("KIMI_API_KEY") or os.environ.get("MOONSHOT_API_KEY") or config.get("kimi_api_key", "")
        if not api_key:
            return _handle_missing_key("Kimi K2.5", "KIMI_API_KEY", "https://platform.moonshot.ai")
        config["_mock_fallback"] = False
        # ★ 自动纠正模型名
        model = config.get("model", "kimi-k2.5")
        if not model.startswith("kimi") and not model.startswith("moonshot"):
            logger.warning(f"模型 '{model}' 不兼容 kimi，自动切换为 kimi-k2.5")
            model = "kimi-k2.5"
        return KimiClient(
            api_key=api_key,
            model=model,
            base_url=config.get("kimi_base_url", "https://api.moonshot.ai/v1"),
        )

    elif provider == "deepseek":
        # DeepSeek — OpenAI 兼容端点
        api_key = os.environ.get("DEEPSEEK_API_KEY") or config.get("deepseek_api_key", "")
        if not api_key:
            return _handle_missing_key("DeepSeek", "DEEPSEEK_API_KEY", "https://platform.deepseek.com")
        config["_mock_fallback"] = False
        return DeepseekClient(
            api_key=api_key,
            model=config.get("model", DEFAULT_DEEPSEEK_MODEL),
            base_url=config.get("deepseek_base_url", ""),
        )

    elif provider == "ollama":
        config["_mock_fallback"] = False
        return OllamaClient(
            base_url=config.get("ollama_url", "http://localhost:11434"),
            model=config.get("model", "codellama"),
        )

    else:
        raise ValueError(f"Unknown LLM provider: {provider}")


def create_reviewer_llm_client(config: dict):
    """
    为 Reviewer Agent 构造独立 LLM client（方案 C）。

    优先级：reviewer_api_key > 对应环境变量 > 主 LLM 的 api_key 配置
    返回 None 表示 Reviewer 不激活（reviewer_provider=none 或 key 缺失）。

    注意：Reviewer 故意不回退到 Mock —— 缺 key 时 reviewer=None，上层保持单 Agent 行为，
    而不是悄悄用 Mock 给不真实的分数。
    """
    import os

    provider = (config.get("reviewer_provider") or "none").strip().lower()
    if provider in ("", "none", "off", "disabled", "false"):
        return None

    model = (config.get("reviewer_model") or "").strip()
    reviewer_key = (config.get("reviewer_api_key") or "").strip()
    base_url = (config.get("reviewer_base_url") or "").strip()

    if provider == "mock":
        logger.info("Reviewer using Mock LLM (reviewer_provider=mock)")
        return MockLLMClient()

    if provider == "anthropic":
        api_key = (
            reviewer_key
            or os.environ.get("ANTHROPIC_API_KEY")
            or config.get("anthropic_api_key", "")
        )
        if not api_key:
            logger.warning("reviewer_provider=anthropic but no API key; Reviewer disabled")
            return None
        return AnthropicClient(
            api_key=api_key,
            model=model or DEFAULT_ANTHROPIC_MODEL,
        )

    if provider == "openai":
        api_key = (
            reviewer_key
            or os.environ.get("OPENAI_API_KEY")
            or config.get("openai_api_key", "")
        )
        if not api_key:
            logger.warning("reviewer_provider=openai but no API key; Reviewer disabled")
            return None
        return OpenAIClient(api_key=api_key, model=model or "gpt-4")

    if provider in ("kimi", "kimi-code"):
        api_key = (
            reviewer_key
            or os.environ.get("KIMI_API_KEY")
            or os.environ.get("MOONSHOT_API_KEY")
            or config.get("kimi_api_key", "")
        )
        if not api_key:
            logger.warning(f"reviewer_provider={provider} but no API key; Reviewer disabled")
            return None
        default_url = (
            "https://api.kimi.com/coding/v1" if provider == "kimi-code"
            else "https://api.moonshot.ai/v1"
        )
        mdl = model or "kimi-k2.5"
        if not mdl.startswith("kimi") and not mdl.startswith("moonshot"):
            logger.warning(
                f"reviewer_model '{mdl}' 与 {provider} 不兼容，自动切换为 kimi-k2.5"
            )
            mdl = "kimi-k2.5"
        return KimiClient(api_key=api_key, model=mdl, base_url=base_url or default_url)

    if provider == "deepseek":
        api_key = (
            reviewer_key
            or os.environ.get("DEEPSEEK_API_KEY")
            or config.get("deepseek_api_key", "")
        )
        if not api_key:
            logger.warning("reviewer_provider=deepseek but no API key; Reviewer disabled")
            return None
        return DeepseekClient(
            api_key=api_key,
            model=model or DEFAULT_DEEPSEEK_MODEL,
            base_url=base_url,
        )

    if provider == "ollama":
        return OllamaClient(
            base_url=base_url or config.get("ollama_url", "http://localhost:11434"),
            model=model or "codellama",
        )

    logger.warning(f"Unknown reviewer_provider '{provider}', Reviewer disabled")
    return None


# ============================================================
# LLM Client 接口 (可替换为实际实现)
# ============================================================

class BaseLLMClient:
    async def chat(self, messages: list[dict]) -> str:
        raise NotImplementedError

    async def stream_chat(self, messages: list[dict]):
        """流式输出 — 逐 token yield str，默认回退到非流式"""
        result = await self.chat(messages)
        yield result

    async def chat_with_tools(self, messages: list[dict], tools: list[dict]):
        """带 function calling 的聊天，返回 LLMResponse"""
        from api.schemas import LLMResponse
        # 默认回退：不支持工具调用时，走普通 chat
        result = await self.chat(messages)
        return LLMResponse(content=result, tool_calls=[])


class MockLLMClient(BaseLLMClient):
    """
    模拟 LLM 客户端 — 无需 API Key，用于测试和演示

    根据 prompt 内容智能生成合理的模拟响应，
    可以跑通完整的 Agent Loop 流程
    """

    async def chat(self, messages: list[dict]) -> str:
        import asyncio
        import json

        # 模拟一点延迟，更真实
        await asyncio.sleep(0.5)

        last_msg = messages[-1]["content"] if messages else ""
        all_text = " ".join(m["content"] for m in messages).lower()

        # 根据上下文返回不同的模拟响应
        # ★ 注意：匹配顺序很关键，更具体的 pattern 必须放前面

        # 1) 评估阶段 — 必须在 plan 之前（因为 plan 的 system prompt 里也包含 "evaluate" 等通用词）
        #    通过检测 evaluator 特有的关键词来精确匹配
        if "evaluate these changes" in all_text or ("score" in all_text and "code changes" in all_text):
            return json.dumps({
                "score": 85,
                "requirements_met": ["Core functionality implemented", "Code is readable"],
                "requirements_missed": [],
                "risks": ["No error handling for edge cases"],
                "suggestions": ["Add input validation", "Add more test cases"],
            })

        # 2) 计划阶段 — Planner 的 system prompt 包含 "execution plan"
        elif "execution plan" in all_text or "generate the execution plan" in all_text or "break it down" in all_text:
            return json.dumps([
                {
                    "title": "Analyze Requirements",
                    "description": "Understand the requirements and identify key components",
                    "step_type": "code_generate",
                },
                {
                    "title": "Generate Code",
                    "description": "Write the core implementation code",
                    "step_type": "code_generate",
                },
                {
                    "title": "Build & Lint",
                    "description": "python -c \"print('Build: OK')\"",
                    "step_type": "build",
                },
                {
                    "title": "Run Tests",
                    "description": "Execute test suite",
                    "step_type": "test",
                },
                {
                    "title": "Quality Review",
                    "description": "Evaluate code quality",
                    "step_type": "evaluate",
                },
                {
                    "title": "Human Review",
                    "description": "Review and approve changes",
                    "step_type": "review",
                },
            ])

        # 3) 代码生成 — CodeGenerator 的 prompt 包含 "create a new file" 或 "modify"
        elif "create a new file" in all_text or "modify the following file" in all_text or "output only the code" in all_text:
            if "hello" in all_text:
                return '''def hello_world():
    """Say hello to the world!"""
    print("Hello, World!")

if __name__ == "__main__":
    hello_world()'''
            return '''# Auto-generated code by kedo (Mock Mode)
def main():
    """Main entry point."""
    print("Feature implemented successfully!")
    return True

if __name__ == "__main__":
    main()'''

        # 3b) 评估 fallback — 宽松匹配
        elif "evaluate" in all_text or "score" in all_text:
            return json.dumps({
                "score": 85,
                "requirements_met": ["Core functionality implemented", "Code is readable"],
                "requirements_missed": [],
                "risks": ["No error handling for edge cases"],
                "suggestions": ["Add input validation", "Add more test cases"],
            })

        # 4) 通用回复
        else:
            return "Task completed successfully. [Mock LLM Response]"

    async def stream_chat(self, messages: list[dict]):
        """模拟流式输出 — 逐字符 yield"""
        import asyncio
        result = await self.chat(messages)
        for char in result:
            yield char
            await asyncio.sleep(0.01)


class OpenAIClient(BaseLLMClient):
    # 错误标签 —— 子类可覆盖，让异常信息指向正确 provider
    PROVIDER_LABEL = "OpenAI"

    # 超时配置 — 实战发现 default httpx timeout 对 deepseek-v4-pro 这类 reasoning 模型
    # 不够：复杂 prompt 时 reasoning 阶段 stream 长时间无字节，触发 idle timeout（约
    # 17-20s 一致失败）。给 5 分钟 read 兜底，足以容纳大 reasoning。
    REQUEST_TIMEOUT = 300.0  # 秒

    def __init__(self, api_key: str, model: str = "gpt-4", base_url: str = ""):
        self.api_key = api_key
        self.model = model
        self.base_url = base_url or ""

    def _client(self):
        import openai
        kwargs = {
            "api_key": self.api_key,
            "timeout": self.REQUEST_TIMEOUT,
            "max_retries": 0,  # ReactAgent 自己有 3 次重试，避免 SDK 层再叠加
        }
        if self.base_url:
            kwargs["base_url"] = self.base_url
        return openai.AsyncOpenAI(**kwargs)

    async def chat(self, messages: list[dict]) -> str:
        try:
            client = self._client()
            response = await client.chat.completions.create(
                model=self.model,
                messages=messages,
                temperature=0.1,
            )
            return response.choices[0].message.content
        except Exception as e:
            raise RuntimeError(f"{self.PROVIDER_LABEL} API error: {e}")

    async def stream_chat(self, messages: list[dict]):
        # ★ 双层兜底：stream 任何失败都回落到非 stream chat()，避免一时网络抖动
        # 或 reasoning idle timeout 让整轮重试全挂
        emitted_any = False
        try:
            client = self._client()
            stream = await client.chat.completions.create(
                model=self.model,
                messages=messages,
                temperature=0.1,
                stream=True,
            )
            async for chunk in stream:
                delta = chunk.choices[0].delta
                if delta.content:
                    emitted_any = True
                    yield delta.content
            if emitted_any:
                return  # stream 成功
        except Exception as e:
            logger.warning(
                f"{self.PROVIDER_LABEL} stream failed ({e}), falling back to non-stream chat"
            )
            try:
                fallback = await self.chat(messages)
                if fallback:
                    yield fallback
                return
            except Exception as e2:
                raise RuntimeError(f"{self.PROVIDER_LABEL} API stream error: {e}")

        # stream 跑完但没发任何 chunk（reasoning-only 兜底场景）→ 也走非 stream
        if not emitted_any:
            logger.warning(
                f"{self.PROVIDER_LABEL} stream returned 0 chunks, falling back to non-stream chat "
                f"(likely reasoning_content was generated but content stayed empty)"
            )
            try:
                fallback = await self.chat(messages)
                if fallback:
                    yield fallback
            except Exception as e:
                raise RuntimeError(f"{self.PROVIDER_LABEL} API stream error: {e}")

    async def chat_with_tools(self, messages: list[dict], tools: list[dict]):
        from api.schemas import LLMResponse, ToolCallData
        try:
            client = self._client()
            kwargs = {"model": self.model, "messages": messages, "temperature": 0.1}
            if tools:
                kwargs["tools"] = tools
            response = await client.chat.completions.create(**kwargs)
            msg = response.choices[0].message
            tool_calls = []
            if msg.tool_calls:
                import json as _json
                for tc in msg.tool_calls:
                    args = tc.function.arguments
                    if isinstance(args, str):
                        args = _json.loads(args)
                    tool_calls.append(ToolCallData(
                        id=tc.id, name=tc.function.name, arguments=args
                    ))
            # ★ DeepSeek-v4-pro thinking mode：reasoning_content 必须传回下一轮
            #   pydantic v2 BaseModel 默认 model_extra 持有未声明字段
            reasoning = getattr(msg, "reasoning_content", None)
            if not reasoning and hasattr(msg, "model_extra") and msg.model_extra:
                reasoning = msg.model_extra.get("reasoning_content")
            return LLMResponse(
                content=msg.content,
                tool_calls=tool_calls,
                reasoning_content=reasoning,
            )
        except Exception as e:
            raise RuntimeError(f"{self.PROVIDER_LABEL} API error: {e}")


DEFAULT_DEEPSEEK_MODEL = "deepseek-v4-pro"
DEEPSEEK_BASE_URL = "https://api.deepseek.com/v1"


class DeepseekClient(OpenAIClient):
    """
    DeepSeek API 客户端。

    DeepSeek 的 API 完全兼容 OpenAI Chat Completions 协议（含 stream 与 tool_calls），
    所以直接继承 OpenAIClient，把 base_url 指向 https://api.deepseek.com/v1。

    申请 Key: https://platform.deepseek.com
    环境变量: DEEPSEEK_API_KEY
    默认模型: deepseek-v4-pro （可通过 config.model / config.reviewer_model 覆盖）
    """

    PROVIDER_LABEL = "DeepSeek"

    def __init__(
        self,
        api_key: str,
        model: str = DEFAULT_DEEPSEEK_MODEL,
        base_url: str = "",
    ):
        super().__init__(
            api_key=api_key,
            model=model or DEFAULT_DEEPSEEK_MODEL,
            base_url=base_url or DEEPSEEK_BASE_URL,
        )

    @staticmethod
    def validate_key_format(api_key: str) -> tuple[bool, str]:
        if not api_key:
            return False, "API Key 为空"
        key = api_key.strip()
        if not key.startswith("sk-"):
            return False, "格式错误: DeepSeek API Key 通常以 'sk-' 开头"
        if len(key) < 20:
            return False, "长度异常: DeepSeek API Key 过短"
        return True, ""

    async def validate(self) -> tuple[bool, str]:
        ok, msg = self.validate_key_format(self.api_key)
        if not ok:
            return False, msg
        try:
            await self.chat([{"role": "user", "content": "ping"}])
            return True, "ok"
        except Exception as e:
            return False, str(e)


DEFAULT_ANTHROPIC_MODEL = "claude-sonnet-4-6"


def _format_anthropic_error(e: Exception, stage: str = "API") -> RuntimeError:
    """将 anthropic SDK 异常归类为易读的 RuntimeError"""
    try:
        import anthropic
    except ImportError:
        return RuntimeError(f"Anthropic {stage} error: {e}")

    if isinstance(e, anthropic.AuthenticationError):
        return RuntimeError("Anthropic 鉴权失败 (401): API Key 无效或已过期，请在 /login 重新录入")
    if isinstance(e, anthropic.PermissionDeniedError):
        return RuntimeError("Anthropic 权限不足 (403): 该 Key 无权访问指定模型或资源")
    if isinstance(e, anthropic.NotFoundError):
        return RuntimeError(f"Anthropic 模型不存在 (404): {e}")
    if isinstance(e, anthropic.RateLimitError):
        return RuntimeError("Anthropic 限流 (429): 请求过于频繁或已达配额上限，稍后重试")
    if isinstance(e, anthropic.BadRequestError):
        return RuntimeError(f"Anthropic 请求错误 (400): {e}")
    # overloaded / 5xx
    if isinstance(e, anthropic.APIStatusError):
        return RuntimeError(f"Anthropic 服务异常 ({getattr(e, 'status_code', '5xx')}): {e}")
    if isinstance(e, anthropic.APIConnectionError):
        return RuntimeError(f"Anthropic 网络错误: {e}")
    return RuntimeError(f"Anthropic {stage} error: {e}")


class AnthropicClient(BaseLLMClient):
    def __init__(self, api_key: str, model: str = DEFAULT_ANTHROPIC_MODEL, max_tokens: int = 8192):
        self.api_key = api_key
        self.model = model
        self.max_tokens = max_tokens

    @staticmethod
    def validate_key_format(api_key: str) -> tuple[bool, str]:
        """本地格式校验 (不发网络请求)"""
        if not api_key:
            return False, "API Key 为空"
        key = api_key.strip()
        if not key.startswith("sk-ant-"):
            return False, "格式错误: Claude API Key 必须以 'sk-ant-' 开头"
        if len(key) < 40:
            return False, "长度异常: Claude API Key 过短"
        return True, ""

    async def validate(self) -> tuple[bool, str]:
        """实网校验: 发送一次最小请求确认 Key / 模型可用"""
        ok, msg = self.validate_key_format(self.api_key)
        if not ok:
            return False, msg
        try:
            import anthropic
            client = anthropic.AsyncAnthropic(api_key=self.api_key)
            await client.messages.create(
                model=self.model,
                max_tokens=1,
                messages=[{"role": "user", "content": "ping"}],
            )
            return True, "ok"
        except Exception as e:
            return False, str(_format_anthropic_error(e, "validate"))

    async def chat(self, messages: list[dict]) -> str:
        try:
            import anthropic
            client = anthropic.AsyncAnthropic(api_key=self.api_key)
            system = "\n".join(m["content"] for m in messages if m["role"] == "system")
            user_messages = [m for m in messages if m["role"] != "system"]
            response = await client.messages.create(
                model=self.model,
                max_tokens=self.max_tokens,
                system=system,
                messages=user_messages,
            )
            return response.content[0].text
        except Exception as e:
            raise _format_anthropic_error(e, "chat")

    async def stream_chat(self, messages: list[dict]):
        try:
            import anthropic
            client = anthropic.AsyncAnthropic(api_key=self.api_key)
            system = "\n".join(m["content"] for m in messages if m["role"] == "system")
            user_messages = [m for m in messages if m["role"] != "system"]
            async with client.messages.stream(
                model=self.model,
                max_tokens=self.max_tokens,
                system=system,
                messages=user_messages,
            ) as stream:
                async for text in stream.text_stream:
                    yield text
        except Exception as e:
            raise _format_anthropic_error(e, "stream")

    async def chat_with_tools(self, messages: list[dict], tools: list[dict]):
        from api.schemas import LLMResponse, ToolCallData
        try:
            import anthropic
            client = anthropic.AsyncAnthropic(api_key=self.api_key)
            system = "\n".join(m["content"] for m in messages if m["role"] == "system")
            user_messages = self._convert_messages_for_anthropic(
                [m for m in messages if m["role"] != "system"]
            )
            # 转换 OpenAI tool schema → Anthropic tool schema
            anthropic_tools = []
            for t in (tools or []):
                func = t.get("function", {})
                anthropic_tools.append({
                    "name": func["name"],
                    "description": func.get("description", ""),
                    "input_schema": func.get("parameters", {"type": "object", "properties": {}}),
                })
            kwargs = {
                "model": self.model,
                "max_tokens": self.max_tokens,
                "system": system,
                "messages": user_messages,
            }
            if anthropic_tools:
                kwargs["tools"] = anthropic_tools
            response = await client.messages.create(**kwargs)
            content_text = ""
            tool_calls = []
            for block in response.content:
                if block.type == "text":
                    content_text += block.text
                elif block.type == "tool_use":
                    tool_calls.append(ToolCallData(
                        id=block.id, name=block.name, arguments=block.input
                    ))
            return LLMResponse(
                content=content_text or None,
                tool_calls=tool_calls,
            )
        except Exception as e:
            raise _format_anthropic_error(e, "chat_with_tools")

    @staticmethod
    def _convert_messages_for_anthropic(messages: list[dict]) -> list[dict]:
        """将含有 tool 角色的消息转为 Anthropic 格式"""
        result = []
        for m in messages:
            role = m.get("role")
            if role == "tool":
                # Anthropic 用 tool_result 块
                result.append({
                    "role": "user",
                    "content": [{
                        "type": "tool_result",
                        "tool_use_id": m.get("tool_call_id", ""),
                        "content": m.get("content", ""),
                    }],
                })
            elif role == "assistant" and m.get("tool_calls"):
                # 转换 assistant 的 tool_calls 为 Anthropic 格式
                content_blocks = []
                if m.get("content"):
                    content_blocks.append({"type": "text", "text": m["content"]})
                for tc in m["tool_calls"]:
                    content_blocks.append({
                        "type": "tool_use",
                        "id": tc["id"] if isinstance(tc, dict) else tc.id,
                        "name": tc["name"] if isinstance(tc, dict) else tc.name,
                        "input": tc["arguments"] if isinstance(tc, dict) else tc.arguments,
                    })
                result.append({"role": "assistant", "content": content_blocks})
            else:
                result.append(m)
        return result


class KimiClient(BaseLLMClient):
    """
    Moonshot Kimi K2.5 客户端

    兼容 OpenAI Chat Completions API 格式
    申请 Key: https://platform.moonshot.ai/

    使用流式响应 (stream=True) 避免长时间等待导致读超时。
    """

    # 超时配置（秒）
    CONNECT_TIMEOUT = 30       # 连接超时
    READ_TIMEOUT = 300         # 读超时（流式模式下每个 chunk 间的超时）
    WRITE_TIMEOUT = 30         # 写超时
    MAX_RETRIES = 2            # 最大重试次数

    def __init__(self, api_key: str, model: str = "kimi-k2.5",
                 base_url: str = "https://api.moonshot.ai/v1",
                 max_tokens: int = 8192):
        self.api_key = api_key
        self.model = model
        self.base_url = base_url
        self.max_tokens = max_tokens

    async def chat(self, messages: list[dict]) -> str:
        import httpx

        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
            "User-Agent": "claude-code/0.1.0",
        }

        timeout = httpx.Timeout(
            connect=self.CONNECT_TIMEOUT,
            read=self.READ_TIMEOUT,
            write=self.WRITE_TIMEOUT,
            pool=self.CONNECT_TIMEOUT,
        )

        last_error = None
        for attempt in range(1, self.MAX_RETRIES + 1):
            try:
                return await self._stream_chat(headers, messages, timeout)
            except httpx.TimeoutException as e:
                last_error = e
                logger.warning(f"Kimi API 超时 (attempt {attempt}/{self.MAX_RETRIES}): {e}")
                if attempt < self.MAX_RETRIES:
                    import asyncio
                    await asyncio.sleep(2 * attempt)  # 指数退避
                    continue
                raise RuntimeError(
                    f"Kimi API 请求超时 ({self.READ_TIMEOUT}s)，已重试 {self.MAX_RETRIES} 次。\n"
                    f"可能原因：1) 网络不稳定 2) 请求内容过大 3) Kimi 服务繁忙\n"
                    f"建议：稍后重试，或尝试简化提示词"
                )
            except httpx.HTTPStatusError as e:
                status = e.response.status_code
                detail = ""
                try:
                    body = e.response.json()
                    detail = body.get("error", {}).get("message", "") if isinstance(body.get("error"), dict) else str(body.get("error", ""))
                except Exception:
                    pass
                if status == 401:
                    raise RuntimeError(f"Kimi API Key 无效或已过期 (endpoint: {self.base_url})，请用 /login 重新输入。{f' ({detail})' if detail else ''}")
                elif status == 429:
                    # 频率限制可以重试
                    last_error = e
                    if attempt < self.MAX_RETRIES:
                        import asyncio
                        await asyncio.sleep(3 * attempt)
                        continue
                    raise RuntimeError(f"Kimi API 请求频率超限，已重试 {self.MAX_RETRIES} 次。{f' ({detail})' if detail else ''}")
                elif status == 400:
                    raise RuntimeError(f"Kimi API 请求参数错误: {detail or e}")
                else:
                    raise RuntimeError(f"Kimi API 错误 (HTTP {status}): {detail or e}")
            except RuntimeError:
                raise
            except Exception as e:
                raise RuntimeError(f"Kimi API error: {e}")

        raise RuntimeError(f"Kimi API 未知错误: {last_error}")

    async def stream_chat(self, messages: list[dict]):
        """流式输出 — 逐 token yield，复用 SSE 解析逻辑。

        Kimi reasoning-only 兜底: 若整个流没出过 content 但有 reasoning_content，
        在流结束时一次性 yield 拼接后的 reasoning（里面通常含 ```tool_call``` 块）。
        """
        import httpx
        import json as _json

        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
            "User-Agent": "claude-code/0.1.0",
        }
        timeout = httpx.Timeout(
            connect=self.CONNECT_TIMEOUT,
            read=self.READ_TIMEOUT,
            write=self.WRITE_TIMEOUT,
            pool=self.CONNECT_TIMEOUT,
        )

        emitted_any = False
        reasoning_buf: list[str] = []

        async with httpx.AsyncClient(timeout=timeout) as client:
            async with client.stream(
                "POST",
                f"{self.base_url}/chat/completions",
                headers=headers,
                json={
                    "model": self.model,
                    "messages": messages,
                    "temperature": 0.1,
                    "max_tokens": self.max_tokens,
                    "stream": True,
                },
            ) as response:
                response.raise_for_status()
                async for line in response.aiter_lines():
                    line = line.strip()
                    if not line:
                        continue
                    chunk = None
                    if line.startswith("data:"):
                        data_str = line[5:].strip()
                        if data_str == "[DONE]":
                            break
                        try:
                            chunk = _json.loads(data_str)
                        except (ValueError, IndexError, KeyError):
                            continue
                    elif line.startswith("{"):
                        try:
                            chunk = _json.loads(line)
                        except (ValueError, IndexError, KeyError):
                            continue
                    if chunk is None:
                        continue
                    text = self._extract_chunk_content(chunk)
                    if text:
                        emitted_any = True
                        yield text
                        continue
                    r = self._extract_chunk_reasoning(chunk)
                    if r:
                        reasoning_buf.append(r)

        if not emitted_any and reasoning_buf:
            full_reasoning = "".join(reasoning_buf)
            logger.warning(
                f"Stream emitted no content but {len(full_reasoning)} chars of reasoning_content; "
                f"yielding reasoning as fallback (Kimi reasoning-only quirk)."
            )
            yield full_reasoning

    async def _stream_chat(
        self, headers: dict, messages: list[dict], timeout
    ) -> str:
        """
        使用流式响应 (stream=True) 接收 LLM 输出。

        流式模式下 read_timeout 是每个 chunk 之间的间隔超时，
        而非整体响应超时，因此不会因为生成时间长而超时。

        兼容两种 SSE 格式：
        - 标准 OpenAI: data: {"choices":[{"delta":{"content":"..."}}]}
        - Kimi Code:   可能直接返回 content 字段或使用 message 而非 delta
        """
        import httpx
        import json as _json

        content_parts: list[str] = []
        reasoning_parts: list[str] = []
        raw_lines_sample = []  # 保留前几行用于调试

        async with httpx.AsyncClient(timeout=timeout) as client:
            async with client.stream(
                "POST",
                f"{self.base_url}/chat/completions",
                headers=headers,
                json={
                    "model": self.model,
                    "messages": messages,
                    "temperature": 0.1,
                    "max_tokens": self.max_tokens,
                    "stream": True,
                },
            ) as response:
                response.raise_for_status()
                async for line in response.aiter_lines():
                    line = line.strip()
                    if not line:
                        continue

                    # 记录前 5 行用于调试
                    if len(raw_lines_sample) < 5:
                        raw_lines_sample.append(line[:200])

                    chunk = None
                    if line.startswith("data:"):
                        data_str = line[5:].strip()
                        if data_str == "[DONE]":
                            break
                        try:
                            chunk = _json.loads(data_str)
                        except (ValueError, IndexError, KeyError) as e:
                            logger.debug(f"Skip unparsable SSE chunk: {data_str[:100]}... ({e})")
                            continue
                    elif line.startswith("{"):
                        try:
                            chunk = _json.loads(line)
                        except (ValueError, IndexError, KeyError):
                            continue
                    if chunk is None:
                        continue
                    text = self._extract_chunk_content(chunk)
                    if text:
                        content_parts.append(text)
                        continue
                    r = self._extract_chunk_reasoning(chunk)
                    if r:
                        reasoning_parts.append(r)

        full_content = "".join(content_parts)
        if full_content:
            return full_content

        # content 全空 → 先看 reasoning_content 兜底（Kimi reasoning-only 输出）
        if reasoning_parts:
            full_reasoning = "".join(reasoning_parts)
            logger.warning(
                f"Stream content empty but got {len(full_reasoning)} chars of reasoning_content. "
                f"Using reasoning as fallback (Kimi reasoning-only quirk)."
            )
            return full_reasoning

        # 流真的什么都没出 → 非流式回退
        logger.warning(
            f"Stream returned empty content (no reasoning either). Raw lines sample: {raw_lines_sample}. "
            f"Falling back to non-stream request."
        )
        return await self._non_stream_chat(headers, messages, timeout)

    @staticmethod
    def _extract_chunk_reasoning(chunk: dict) -> str:
        """提取 reasoning_content（思考链）。仅在 content 全空时作为兜底使用。"""
        choices = chunk.get("choices", [])
        if not choices:
            return ""
        delta = choices[0].get("delta", {})
        if delta and delta.get("reasoning_content"):
            return delta["reasoning_content"]
        message = choices[0].get("message", {})
        if message and message.get("reasoning_content"):
            return message["reasoning_content"]
        return ""

    @staticmethod
    def _extract_chunk_content(chunk: dict) -> str:
        """
        从 SSE chunk 中提取文本内容。

        兼容多种格式：
        - OpenAI 标准: choices[0].delta.content
        - 某些 API:   choices[0].message.content
        - Kimi Code 2.5: choices[0].delta.reasoning_content (思考) + delta.content (正文)
          reasoning_content 是模型的思考过程，跳过；只提取 content 正文。
        """
        choices = chunk.get("choices", [])
        if not choices:
            return ""
        choice = choices[0]
        # 标准 delta 格式
        delta = choice.get("delta", {})
        if delta:
            # Kimi Code 2.5: 跳过 reasoning_content（思考过程），只取 content
            content = delta.get("content")
            if content:
                return content
            # 如果只有 reasoning_content 而没有 content，跳过（返回空）
            if "reasoning_content" in delta:
                return ""
        # message 格式（某些非标准实现）
        message = choice.get("message", {})
        if message and "content" in message:
            return message["content"] or ""
        # text 格式
        if "text" in choice:
            return choice["text"] or ""
        return ""

    async def _non_stream_chat(
        self, headers: dict, messages: list[dict], timeout
    ) -> str:
        """
        非流式回退：当流式响应解析为空时使用。

        采用 120s 读超时（不再 *2 = 600s 死等），避免 Kimi reasoning-only quirk
        把整个 ReAct loop 卡死。content 空时回落到 reasoning_content。
        """
        import httpx

        fallback_timeout = httpx.Timeout(
            connect=self.CONNECT_TIMEOUT,
            read=120,
            write=self.WRITE_TIMEOUT,
            pool=self.CONNECT_TIMEOUT,
        )

        async with httpx.AsyncClient(timeout=fallback_timeout) as client:
            response = await client.post(
                f"{self.base_url}/chat/completions",
                headers=headers,
                json={
                    "model": self.model,
                    "messages": messages,
                    "temperature": 0.1,
                    "max_tokens": self.max_tokens,
                },
            )
            response.raise_for_status()
            data = response.json()
            message = data.get("choices", [{}])[0].get("message", {}) or {}
            content = message.get("content") or ""
            if content:
                return content
            # content 空 → 回落到 reasoning_content（同样可能含 ```tool_call``` 块）
            reasoning = message.get("reasoning_content") or ""
            if reasoning:
                logger.warning(
                    f"Non-stream content empty but got {len(reasoning)} chars of reasoning_content; "
                    f"using as fallback."
                )
                return reasoning
            logger.error(f"Non-stream returned empty (no content / no reasoning). Response: {str(data)[:500]}")
            raise RuntimeError(
                "Kimi API 返回空响应（流式和非流式均为空）。\n"
                "可能原因：1) API Key 权限不足 2) 模型暂时不可用 3) 请求被服务端过滤\n"
                "建议：检查 API Key 是否有效，或稍后重试"
            )

    async def chat_with_tools(self, messages: list[dict], tools: list[dict]):
        """Kimi OpenAI 兼容 function calling"""
        from api.schemas import LLMResponse, ToolCallData
        import httpx
        import json as _json

        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }
        timeout = httpx.Timeout(
            connect=self.CONNECT_TIMEOUT,
            read=self.READ_TIMEOUT * 2,  # 非流式需要更长超时
            write=self.WRITE_TIMEOUT,
            pool=self.CONNECT_TIMEOUT,
        )
        body = {
            "model": self.model,
            "messages": messages,
            "temperature": 0.1,
            "max_tokens": self.max_tokens,
        }
        if tools:
            body["tools"] = tools

        async with httpx.AsyncClient(timeout=timeout) as client:
            response = await client.post(
                f"{self.base_url}/chat/completions",
                headers=headers,
                json=body,
            )
            response.raise_for_status()
            data = response.json()

        msg = data.get("choices", [{}])[0].get("message", {})
        content = msg.get("content")
        tool_calls = []
        for tc in (msg.get("tool_calls") or []):
            func = tc.get("function", {})
            args = func.get("arguments", "{}")
            if isinstance(args, str):
                try:
                    args = _json.loads(args)
                except (ValueError, TypeError):
                    args = {}
            tool_calls.append(ToolCallData(
                id=tc.get("id", ""),
                name=func.get("name", ""),
                arguments=args,
            ))
        return LLMResponse(content=content, tool_calls=tool_calls)


class OllamaClient(BaseLLMClient):
    def __init__(self, base_url: str = "http://localhost:11434", model: str = "codellama"):
        self.base_url = base_url
        self.model = model

    async def chat(self, messages: list[dict]) -> str:
        try:
            import httpx
            async with httpx.AsyncClient() as client:
                response = await client.post(
                    f"{self.base_url}/api/chat",
                    json={"model": self.model, "messages": messages, "stream": False},
                    timeout=120,
                )
                return response.json()["message"]["content"]
        except Exception as e:
            raise RuntimeError(f"Ollama API error: {e}")

    async def stream_chat(self, messages: list[dict]):
        try:
            import httpx
            async with httpx.AsyncClient(timeout=120) as client:
                async with client.stream(
                    "POST",
                    f"{self.base_url}/api/chat",
                    json={"model": self.model, "messages": messages, "stream": True},
                ) as response:
                    async for line in response.aiter_lines():
                        if not line.strip():
                            continue
                        try:
                            import json as _json
                            data = _json.loads(line)
                            content = data.get("message", {}).get("content", "")
                            if content:
                                yield content
                            if data.get("done"):
                                break
                        except (ValueError, KeyError):
                            continue
        except Exception as e:
            raise RuntimeError(f"Ollama API stream error: {e}")
