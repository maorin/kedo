"""
kedo CLI — 交互式 REPL (Read-Eval-Print Loop)

类似 Claude Code 的命令行交互体验：
- 输入自然语言需求，Agent 自动开发
- 实时显示流程状态（流程图节点高亮）
- 内置命令：/status, /pause, /resume, /candidates, /discuss, /history, /web, /help
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
import threading
import time
import webbrowser
from typing import Optional

from cli.theme import (
    C, BRAND, ACCENT, SUCCESS, WARN, ERROR, INFO, MUTED, HIGHLIGHT,
    LOGO, PROMPT, banner, status_badge, step_line, progress_bar,
    table_row, divider, flow_node, status_bar, status_line,
)


class KedoREPL:
    """
    kedo 交互式命令行界面

    启动时:
    1. 启动 FastAPI server (后台线程)
    2. 打开 WebSocket 连接接收实时事件
    3. 进入 REPL 循环等待用户输入
    """

    def __init__(self, config: dict = None, project_path: str = ".", connect_url: Optional[str] = None):
        self.config = config or {}
        self.project_path = os.path.abspath(project_path)
        # connect_url: 非 None 时走 thin-client 模式（连已有 server，不自起 uvicorn）
        # None 时走 embedded 模式（自己起 uvicorn，沿用旧行为）
        self.connect_url = (connect_url or "").rstrip("/") or None
        if self.connect_url:
            from urllib.parse import urlparse
            u = urlparse(self.connect_url)
            self.host = u.hostname or "127.0.0.1"
            self.port = u.port or (443 if u.scheme == "https" else 80)
            self._scheme = u.scheme or "http"
        else:
            self.port = self.config.get("port", 8000)
            self.host = self.config.get("host", "127.0.0.1")
            self._scheme = "http"

        # 运行时状态
        self.current_task_id: Optional[str] = None
        self.server_ready = threading.Event()
        self.running = True

        # 流程图状态
        self.flow_state = {
            "需求输入": "pending",
            "计划生成": "pending",
            "代码生成": "pending",
            "编译检查": "pending",
            "自动测试": "pending",
            "质量评估": "pending",
            "候选版本": "pending",
            "自动部署": "pending",
            "线上监控": "pending",
        }

        self._event_log: list[dict] = []
        # 控制台输出详细度: True = 不截断（看到完整 args/output/summary）；False = 旧的简短模式
        self._verbose: bool = True

        # 状态栏数据
        self._sb = {
            "task_id": "",
            "status": "",
            "step": "",
            "progress": 0,
            "provider": "",
            "model": "",
            "iteration": 0,
            "max_iterations": 5,
        }
        self._stdout_lock = threading.Lock()  # 保护所有 stdout 写操作

    # ─── 启动流程 ────────────────────────────────────────────

    def start(self):
        """启动 kedo"""
        print(LOGO)
        print(f"  {MUTED}项目路径: {HIGHLIGHT}{self.project_path}{C.RESET}")
        display_host = "127.0.0.1" if self.host in ("0.0.0.0", "::") else self.host
        print(f"  {MUTED}Dashboard: {ACCENT}{self._scheme}://{display_host}:{self.port}{C.RESET}")

        if self.connect_url:
            # thin-client：复用已有 server，不自启 uvicorn
            print(f"  {MUTED}模式: {ACCENT}thin-client{MUTED}（连接已有 server，REPL 退出不影响 task）{C.RESET}")
            self.server_ready.set()  # 让等就绪的逻辑直接通过
        else:
            # embedded：自己起 uvicorn（旧行为）
            self._start_server_thread()

        # 获取 LLM 提供商状态 (填充 _sb)
        self._fetch_provider_status()

        # 显示 LLM 状态
        provider = self._sb.get("provider", "")
        model = self._sb.get("model", "")
        if getattr(self, "_is_mock_fallback", False):
            reason = getattr(self, "_mock_fallback_reason", "")
            print(f"\n  {ERROR}{C.BOLD}⚠  警告: 当前运行在 Mock 模式 (模拟){C.RESET}")
            print(f"  {ERROR}   原因: {reason}{C.RESET}")
            print(f"  {WARN}   Mock 模式下所有 LLM 响应均为固定模板{C.RESET}")
            print(f"  {ACCENT}   → 使用 {HIGHLIGHT}/login{ACCENT} 命令配置 AI 提供商{C.RESET}\n")
        elif provider == "mock":
            print(f"  {WARN}LLM: Mock 模式 (演示/测试){C.RESET}")
        elif provider and provider != "unknown":
            print(f"  {SUCCESS}LLM: {HIGHLIGHT}{provider}{SUCCESS}  模型: {HIGHLIGHT}{model}{C.RESET}")

        print(f"\n  {MUTED}输入需求开始开发，或输入 {HIGHLIGHT}/help{MUTED} 查看命令{C.RESET}")
        print(divider())
        print()

        # 启动事件监听
        self._start_event_listener()

        # 启动底栏分屏（DECSTBM 滚动区域 + 底部固定状态栏）
        self._setup_split_screen()

        # 进入 REPL
        try:
            self._repl_loop()
        except KeyboardInterrupt:
            self._shutdown()

    def _start_server_thread(self):
        """在后台线程启动 FastAPI server"""
        def run_server():
            import uvicorn
            from api.server import create_app

            self.config["project_path"] = self.project_path
            app = create_app(self.config)

            # 保存 app 引用供 REPL 使用
            self._app = app

            config = uvicorn.Config(
                app, host=self.host, port=self.port,
                log_level="warning",  # 减少 uvicorn 日志噪音
            )
            server = uvicorn.Server(config)
            self._uvicorn_server = server

            # 标记服务器已就绪
            original_startup = server.startup

            async def patched_startup(sockets=None):
                await original_startup(sockets)
                self.server_ready.set()

            server.startup = patched_startup

            asyncio.run(server.serve())

        t = threading.Thread(target=run_server, daemon=True)
        t.start()

        # 等待服务器启动
        print(f"  {MUTED}启动服务...{C.RESET}", end="", flush=True)
        if self.server_ready.wait(timeout=10):
            print(f"\r  {SUCCESS}✓ 服务已启动{C.RESET}          ")
        else:
            print(f"\r  {WARN}⚠ 服务启动中...{C.RESET}        ")

    def _start_event_listener(self):
        """在后台线程监听 WebSocket 事件"""
        def listen():
            asyncio.run(self._ws_listener())

        t = threading.Thread(target=listen, daemon=True)
        t.start()

    def _fetch_provider_status(self):
        """查询 LLM 提供商状态，填充 _sb 数据 (不打印，由 start() 显示)"""
        llm_status = self._api_get("/llm/status")
        provider = llm_status.get("provider", "unknown") if llm_status else "unknown"
        model = llm_status.get("model", "unknown") if llm_status else "unknown"
        self._sb["provider"] = provider
        self._sb["model"] = model
        # mock 回退标记也保存方便 banner 展示
        self._is_mock_fallback = self.config.get("_mock_fallback", False)
        self._mock_fallback_reason = self.config.get("_mock_fallback_reason", "")

    async def _ws_listener(self):
        """WebSocket 事件监听 — 实时更新终端显示"""
        import websockets

        await asyncio.sleep(1)  # 等服务器就绪

        ws_scheme = "wss" if self._scheme == "https" else "ws"
        uri = f"{ws_scheme}://{self._api_host}:{self.port}/api/ws"
        retry = 0
        while self.running and retry < 5:
            try:
                async with websockets.connect(uri) as ws:
                    retry = 0
                    async for msg in ws:
                        try:
                            event = json.loads(msg)
                            self._handle_event(event)
                        except json.JSONDecodeError:
                            pass
            except Exception:
                retry += 1
                await asyncio.sleep(2)

    def _handle_event(self, event: dict):
        """处理实时事件 — 更新终端 UI"""
        etype = event.get("type", "")
        data = event.get("data", {})
        task_id = event.get("task_id", "")

        self._event_log.append(event)

        # 更新流程图状态
        step_map = {
            "step_started": {
                "Planning": ("计划生成", "running"),
                "Analyze Requirements": ("代码生成", "running"),
                "Generate Code": ("代码生成", "running"),
                "Build & Lint": ("编译检查", "running"),
                "Run Tests": ("自动测试", "running"),
                "Quality Review": ("质量评估", "running"),
            },
            "step_completed": {
                "Planning": ("计划生成", "success"),
                "Analyze Requirements": ("代码生成", "success"),
                "Generate Code": ("代码生成", "success"),
                "Build & Lint": ("编译检查", "success"),
                "Run Tests": ("自动测试", "success"),
                "Quality Review": ("质量评估", "success"),
            },
            "step_failed": {
                "Build & Lint": ("编译检查", "failed"),
                "Run Tests": ("自动测试", "failed"),
                "Quality Review": ("质量评估", "failed"),
            },
        }

        if etype in step_map:
            step_name = data.get("step", "")
            for key, (node, status) in step_map[etype].items():
                if key.lower() in step_name.lower():
                    self.flow_state[node] = status
                    break

        # ★ 更新状态栏数据
        if task_id and not self._sb["task_id"]:
            self._sb["task_id"] = task_id

        # 特殊事件处理
        if etype == "task_created":
            self.flow_state["需求输入"] = "success"
            self._sb["task_id"] = task_id
            self._sb["status"] = "pending"
            self._print_event(f"{SUCCESS}✓ 任务已创建{C.RESET} [{task_id}]  {MUTED}实时状态将在此显示{C.RESET}")

        elif etype == "llm_request":
            phase = data.get("phase", "")
            prompt = data.get("prompt_summary", "")
            model = data.get("model", "")
            turn = data.get("turn", "")
            phase_label = {
                "planning": "📋 规划",
                "code_generate": "💻 代码生成",
                "evaluate": "📊 质量评估",
                "agent": "🤖 Agent",
            }.get(phase, phase)
            turn_hint = f" Turn {turn}" if turn else ""
            self._print_event(f"{BRAND}⬆ LLM 请求{C.RESET} [{phase_label}{turn_hint}]  {MUTED}model={model}{C.RESET}")
            if prompt:
                self._print_event(f"  {MUTED}prompt: {prompt}{C.RESET}")

        elif etype == "llm_response":
            phase = data.get("phase", "")
            summary = data.get("summary", "")
            turn = data.get("turn", "")
            tool_count = data.get("tool_count", 0)
            phase_label = {
                "planning": "📋 规划",
                "code_generate": "💻 代码生成",
                "evaluate": "📊 质量评估",
                "auto_fix": "🔧 自动修复",
                "agent": "🤖 Agent",
                "respond": "💬 回复",
            }.get(phase, phase)
            turn_hint = f" Turn {turn}" if turn else ""
            tools_hint = f" ({tool_count} tools)" if tool_count else ""
            self._print_event(f"{ACCENT}⬇ LLM 响应{C.RESET} [{phase_label}{turn_hint}{tools_hint}]")
            if summary:
                lines = summary.split("\n")
                show_lines = lines if self._verbose else lines[:5]
                for line in show_lines:
                    text_line = line if self._verbose else line[:150]
                    self._print_event(f"  {MUTED}{text_line}{C.RESET}")
                if not self._verbose and len(lines) > 5:
                    self._print_event(f"  {MUTED}... ({len(lines) - 5} more lines, /verbose on 查看完整){C.RESET}")

        elif etype == "llm_token":
            # 流式 token — 实时输出，不换行
            token = data.get("token", "")
            if token:
                print(f"{MUTED}{token}{C.RESET}", end="", flush=True)
                # 遇到换行时标记（下一个非 token 事件打印前会自动换行）
                if token.endswith("\n"):
                    self._streaming_newline = False
                else:
                    self._streaming_newline = True

        elif etype == "tool_execute":
            # 兼容旧格式(step, tool_type, description) 和新格式(tool, args_summary)
            tool_name = data.get("tool") or data.get("step", "")
            tool_type = data.get("tool_type", tool_name)
            desc = data.get("args_summary") or data.get("description", "")
            attempt = data.get("attempt", 1)
            mx = data.get("max_retries", 1)
            retry_hint = f" (尝试 {attempt}/{mx})" if mx > 1 and attempt > 1 else ""
            # ★ 更新状态栏 step
            if tool_name:
                self._sb["step"] = tool_name
            tool_icon = {
                "code_generate": "💻",
                "build": "🔨",
                "test": "🧪",
                "evaluate": "📊",
                "deploy": "🚀",
                "file_read": "📖",
                "file_write": "✏️",
                "file_search": "🔍",
                "shell_execute": "🖥️",
                "respond": "💬",
                "git": "📦",
            }.get(tool_type, "⚙")
            self._print_event(f"{INFO}{tool_icon} 执行: {tool_name}{retry_hint}{C.RESET}")
            if desc:
                text = desc if self._verbose else desc[:120]
                for line in text.split("\n"):
                    self._print_event(f"  {MUTED}{line}{C.RESET}")

        elif etype == "step_started":
            step = data.get("step", "")
            stype = data.get("type", "")
            # ★ 更新状态栏
            if step:
                self._sb["step"] = step
            if step.lower() == "planning":
                self._sb["status"] = "planning"
                self._print_event(f"{BRAND}▶ 开始规划...{C.RESET}")

        elif etype == "step_completed":
            step = data.get("step", "")
            output = data.get("output", "") or data.get("output_preview", "")
            # 美化 "tool:file_read" → "file_read"
            display_step = step.replace("tool:", "") if step.startswith("tool:") else step
            self._print_event(f"{SUCCESS}✓ {display_step} 完成{C.RESET}")
            if output:
                lines = output.split("\n")
                show_lines = lines if self._verbose else lines[:3]
                for line in show_lines:
                    self._print_event(f"  {MUTED}{line}{C.RESET}")
                if not self._verbose and len(lines) > 3:
                    self._print_event(f"  {MUTED}... ({len(lines) - 3} more lines, /verbose on 查看完整){C.RESET}")

        elif etype == "step_failed":
            step = data.get("step", "")
            error = data.get("error", "")
            display_step = step.replace("tool:", "") if step.startswith("tool:") else step
            self._print_event(f"{ERROR}✗ {display_step} 失败{C.RESET}")
            if error:
                text = error if self._verbose else error[:150]
                for line in text.split("\n"):
                    self._print_event(f"  {ERROR}{line}{C.RESET}")

        elif etype == "candidate_created":
            self.flow_state["候选版本"] = "success"
            conf = data.get("ai_confidence", 0)
            self._print_event(f"{SUCCESS}📦 候选版本已创建{C.RESET} confidence={conf}")

        elif etype == "discussion_started":
            iteration = data.get("iteration", 1)
            trigger = data.get("trigger", "")
            self._print_event(f"{ERROR}🔄 闭环迭代 #{iteration}{C.RESET} — 触发: {trigger}")

        elif etype == "discussion_proposals":
            n = len(data.get("proposals", []))
            self._print_event(f"{ACCENT}💬 生成 {n} 个方案{C.RESET} — 输入 /discuss 参与讨论")

        elif etype == "replan_completed":
            n = data.get("subtask_count", 0)
            self._print_event(f"{BRAND}📝 重新规划完成{C.RESET} — {n} 个子任务")

        elif etype == "iteration_updated":
            it = data.get("iteration", 1)
            mx = data.get("max_iterations", 5)
            # ★ 更新状态栏
            self._sb["iteration"] = it
            self._sb["max_iterations"] = mx
            if data.get("forced_pause"):
                self._print_event(f"{ERROR}⚠ 达到最大迭代次数 ({mx}){C.RESET} — 已暂停等待人工介入")

        elif etype == "task_status_changed":
            new_status = data.get("new_status", data.get("status", ""))
            old_status = data.get("old_status", "")
            current_step = data.get("current_step", "")
            progress = data.get("progress_percent", self._sb["progress"])

            # ★ 更新状态栏
            self._sb["status"] = new_status
            if current_step:
                self._sb["step"] = current_step
            if progress is not None:
                self._sb["progress"] = progress

            status_label = {
                "planning": "规划中",
                "in_progress": "执行中",
                "paused": "已暂停",
                "completed": "已完成",
                "failed": "失败",
            }.get(new_status, new_status)
            if new_status == "completed":
                self._sb["progress"] = 100
                self._sb["step"] = ""
                self._print_event(f"{SUCCESS}{C.BOLD}✓ 任务完成！{C.RESET}")
            elif new_status == "failed":
                self._print_event(f"{ERROR}✗ 任务失败{C.RESET}")
            elif old_status and new_status:
                self._print_event(f"{INFO}状态: {old_status} → {status_label}{C.RESET}")

    # ─── 内联状态栏 (无 scroll region) ──────────────────────

    def _get_status_text(self) -> str:
        """生成当前状态行文本"""
        return status_line(**self._sb)

    # ─── 底栏分屏 (DECSTBM) ─────────────────────────────────────

    def _setup_split_screen(self):
        """初始化滚动区域 1..H-1 + 在第 H 行画固定状态栏。

        终端必须是 tty；否则跳过（避免在管道/重定向场景里写转义符）。
        """
        if not sys.stdout.isatty():
            self._split_screen_active = False
            return
        import shutil
        size = shutil.get_terminal_size((80, 24))
        self._term_height = size.lines
        self._split_screen_active = True
        h = self._term_height
        with self._stdout_lock:
            # 滚动区域 = 第 1 行 ~ 第 H-1 行（cursor 会被 DECSTBM 自动跳到 (1,1)）
            sys.stdout.write(f"\033[1;{h - 1}r")
            # 写底栏到第 H 行
            sys.stdout.write(f"\033[{h};1H\033[2K{self._get_status_text()}")
            # cursor 落到滚动区底部 (H-1)，让后续 prompt/事件从底部出现
            # 否则 cursor 还在 (1,1)，input(PROMPT) 会覆盖 banner 顶端
            sys.stdout.write(f"\033[{h - 1};1H")
            sys.stdout.flush()
        # 监听窗口 resize
        try:
            import signal
            signal.signal(signal.SIGWINCH, self._on_terminal_resize)
        except Exception:
            pass

    def _on_terminal_resize(self, *_):
        """SIGWINCH: 重新计算高度 + 重设滚动区域 + 重画状态栏"""
        if not getattr(self, "_split_screen_active", False):
            return
        import shutil
        size = shutil.get_terminal_size((80, 24))
        self._term_height = size.lines
        h = self._term_height
        with self._stdout_lock:
            sys.stdout.write(f"\033[1;{h - 1}r")
            sys.stdout.write(f"\0337\033[{h};1H\033[2K{self._get_status_text()}\0338")
            sys.stdout.flush()

    def _redraw_status_bar(self):
        """只重绘底部状态栏，不影响滚动区或当前 cursor 位置"""
        if not getattr(self, "_split_screen_active", False):
            return
        h = self._term_height
        # \0337 保存光标 → 跳底行 → 清行 → 写状态栏 → \0338 恢复光标
        sys.stdout.write(f"\0337\033[{h};1H\033[2K{self._get_status_text()}\0338")
        sys.stdout.flush()

    def _teardown_split_screen(self):
        """退出时还原滚动区域为整屏 + 清状态栏"""
        if not getattr(self, "_split_screen_active", False):
            return
        h = getattr(self, "_term_height", 24)
        with self._stdout_lock:
            # 清掉底栏，重置滚动区域
            sys.stdout.write(f"\0337\033[{h};1H\033[2K\0338\033[r")
            sys.stdout.flush()
        self._split_screen_active = False

    # ─── stdout 写入 ──────────────────────────────────────

    def _safe_print(self, text: str):
        """线程安全地打印一行 (主线程用)。底栏会在事件之后自动刷新。"""
        with self._stdout_lock:
            sys.stdout.write(f"{text}\n")
            sys.stdout.flush()
        self._redraw_status_bar()

    def _print_event(self, text: str):
        """
        线程安全地打印事件 (后台 WebSocket 线程调用)。

        滚动区域 (1..H-1) 内自然滚动；底栏 (H) 通过 _redraw_status_bar 单独更新，
        不再每次事件都嵌一行状态栏。
        """
        with self._stdout_lock:
            buf = []
            # 如果上一个事件是流式 token（未换行），先换行
            if getattr(self, "_streaming_newline", False):
                buf.append("\n")
                self._streaming_newline = False
            buf.append(f"  {text}\n")
            sys.stdout.write("".join(buf))
            sys.stdout.flush()
        # 数据可能变化（progress/step/status），底栏刷新
        self._redraw_status_bar()

    # ─── REPL 主循环 ────────────────────────────────────────────

    def _repl_loop(self):
        """
        交互式命令循环

        布局（DECSTBM 分屏后）:
            [滚动区: 1..H-1]
              ...事件流持续滚动...
              kedo ❯ █                               ← prompt 在滚动区底部
            [固定底栏: 第 H 行]
              ⚡ kimi-code/kimi-k2.5 │ Task:xxx │ 执行中 ...

        底栏由 _redraw_status_bar 自动维护，prompt 不再手动嵌状态行。
        """
        import readline  # noqa: F401  启用行编辑和历史

        while self.running:
            try:
                user_input = input(PROMPT).strip()
            except EOFError:
                self._shutdown()
                break
            except KeyboardInterrupt:
                # Ctrl-C: 有活跃 task 就停 task（不退 REPL）；没有就提示一下
                # ESC 单键不可靠（readline 吞 meta 前缀），Ctrl-C 是 CLI 通用"停止"语义
                print()  # 换行避免 ^C 顶在 prompt 上
                if self.current_task_id:
                    self._cmd_stop()
                else:
                    print(f"  {MUTED}（无活跃任务，再按 Ctrl-D 或输入 /exit 退出）{C.RESET}")
                continue
            # 用户按 enter 后 cursor 可能短暂越过滚动区域，立刻回写底栏
            self._redraw_status_bar()

            if not user_input:
                continue

            # 处理命令
            if user_input.startswith("/"):
                self._handle_command(user_input)
            else:
                # 检测续接意图
                if self._detect_continuation(user_input):
                    matched_task = self._find_resumable_task(user_input)
                    if matched_task:
                        self._prompt_and_resume(matched_task, user_input)
                        continue
                # 闲聊检测 → 轻量问答（不创建 task、不显示流程图）
                if self._is_chat_query(user_input):
                    self._quick_chat(user_input)
                    continue
                # 自然语言输入 → 创建开发任务
                self._create_task(user_input)

    def _handle_command(self, cmd: str):
        """处理 / 命令"""
        parts = cmd.split(maxsplit=1)
        command = parts[0].lower()
        args = parts[1] if len(parts) > 1 else ""

        commands = {
            "/help": self._cmd_help,
            "/h": self._cmd_help,
            "/status": self._cmd_status,
            "/s": self._cmd_status,
            "/flow": self._cmd_flow,
            "/f": self._cmd_flow,
            "/pause": self._cmd_pause,
            "/resume": self._cmd_resume,
            "/stop": self._cmd_stop,
            "/k": self._cmd_stop,
            "/candidates": self._cmd_candidates,
            "/c": self._cmd_candidates,
            "/discuss": self._cmd_discuss,
            "/d": self._cmd_discuss,
            "/history": self._cmd_history,
            "/web": self._cmd_web,
            "/w": self._cmd_web,
            "/login": self._cmd_login,
            "/l": self._cmd_login,
            "/config": self._cmd_config,
            "/continue": self._cmd_continue,
            "/cont": self._cmd_continue,
            "/clear": self._cmd_clear,
            "/verbose": self._cmd_verbose,
            "/v": self._cmd_verbose,
            "/quit": self._cmd_quit,
            "/q": self._cmd_quit,
            "/exit": self._cmd_quit,
        }

        handler = commands.get(command)
        if handler:
            handler(args) if args else handler()
        else:
            print(f"  {ERROR}未知命令: {command}{C.RESET}  (输入 /help 查看所有命令)")

    # ─── 命令实现 ────────────────────────────────────────────

    def _cmd_help(self, _=""):
        print()
        print(banner("kedo 命令帮助"))
        print()
        cmds = [
            ("/help, /h", "显示帮助"),
            ("/login, /l", "登录/切换 LLM 提供商 (Claude / Kimi)"),
            ("/status, /s", "查看当前任务状态"),
            ("/flow, /f", "显示流程图（实时状态）"),
            ("/pause", "暂停当前任务"),
            ("/resume", "恢复暂停的任务"),
            ("/stop, /k", "停止当前任务（同 Ctrl-C）"),
            ("/continue, /cont", "从检查点续接历史任务"),
            ("/candidates, /c", "列出所有候选版本"),
            ("/discuss, /d", "参与闭环讨论（选择方案）"),
            ("/history", "查看迭代历史"),
            ("/web, /w", "在浏览器中打开 Dashboard"),
            ("/config", "查看当前配置"),
            ("/clear", "清屏"),
            ("/verbose, /v [on|off]", "切换控制台详细模式（默认 on，显示完整 args/output）"),
            ("/quit, /q", "退出"),
        ]
        for cmd, desc in cmds:
            print(f"  {ACCENT}{cmd:<24}{C.RESET} {desc}")
        print()
        print(f"  {MUTED}直接输入自然语言即可创建开发任务（含\"继续\"等关键词时自动检测续接）{C.RESET}")
        print()

    def _cmd_status(self, _=""):
        """显示任务状态"""
        data = self._api_get(f"/tasks/{self.current_task_id}") if self.current_task_id else None
        print()
        if not data or "detail" in data:
            print(f"  {MUTED}暂无活跃任务。输入需求描述创建新任务。{C.RESET}")
            print()
            return

        status = data.get("status", "unknown")
        color = {"in_progress": BRAND, "completed": SUCCESS, "paused": WARN, "failed": ERROR}.get(status, MUTED)

        print(f"  {HIGHLIGHT}任务{C.RESET} {self.current_task_id}  {status_badge(status, color)}")
        print(table_row("当前步骤", data.get("current_step", "—")))
        print(table_row("进度", f"{data.get('progress_percent', 0):.0f}%"))
        print(table_row("代码变更", f"{len(data.get('code_changes', []))} 个文件"))
        tr = data.get("test_results")
        if tr:
            print(table_row("测试结果", f"{tr.get('passed', 0)}/{tr.get('total', 0)} 通过  覆盖率: {tr.get('coverage_percent', 0)}%"))
        print()

    def _cmd_flow(self, _=""):
        """打印当前流程图状态"""
        self._print_flow()

    def _print_flow(self):
        """渲染 ASCII 流程图"""
        print()
        print(f"  {HIGHLIGHT}{C.BOLD}运行流程{C.RESET}")
        print(divider("─", 44))

        nodes = [
            ("📋", "需求输入"),
            ("🎯", "计划生成"),
            ("💻", "代码生成"),
            ("🔨", "编译检查"),
            ("🧪", "自动测试"),
            ("📊", "质量评估"),
            ("📦", "候选版本"),
            ("🚀", "自动部署"),
            ("📡", "线上监控"),
        ]

        for i, (icon, name) in enumerate(nodes):
            status = self.flow_state.get(name, "pending")
            print(flow_node(icon, name, status))
            if i < len(nodes) - 1:
                # 决策点的特殊箭头
                if name in ("编译检查", "自动测试"):
                    s = self.flow_state.get(name, "pending")
                    if s == "failed":
                        print(f"  {ERROR}  ╰──▶ 否 → 回到 代码生成{C.RESET}")
                    else:
                        print(f"  {MUTED}  │{C.RESET}")
                elif name == "质量评估":
                    s = self.flow_state.get(name, "pending")
                    if s == "failed":
                        print(f"  {ERROR}  ╰──▶ 否 → 闭环讨论 → 重新规划 → 回到 计划生成{C.RESET}")
                    else:
                        print(f"  {MUTED}  │{C.RESET}")
                else:
                    print(f"  {MUTED}  │{C.RESET}")

        print(divider("─", 44))
        print()

    def _cmd_pause(self, _=""):
        if not self.current_task_id:
            print(f"  {MUTED}暂无活跃任务{C.RESET}")
            return
        self._api_post(f"/tasks/{self.current_task_id}/pause")
        print(f"  {WARN}⏸ 任务已暂停{C.RESET}")

    def _cmd_resume(self, _=""):
        if not self.current_task_id:
            print(f"  {MUTED}暂无活跃任务{C.RESET}")
            return
        self._api_post(f"/tasks/{self.current_task_id}/resume")
        print(f"  {SUCCESS}▶ 任务已恢复{C.RESET}")

    def _cmd_stop(self, _=""):
        """停止当前任务（用户主动取消）。Ctrl-C 也走这条路径。"""
        if not self.current_task_id:
            print(f"  {MUTED}暂无活跃任务{C.RESET}")
            return
        tid = self.current_task_id
        try:
            resp = self._api_post(f"/tasks/{tid}/stop", {"reason": "Cancelled by user (REPL)"})
        except Exception as e:
            print(f"  {ERROR}停止请求失败: {e}{C.RESET}")
            return
        if resp and resp.get("force_cancelled"):
            print(f"  {WARN}⛔ 任务 {tid[:8]} 已强制停止（5s 内未优雅退出）{C.RESET}")
        elif resp and resp.get("handled"):
            print(f"  {WARN}⛔ 任务 {tid[:8]} 已停止{C.RESET}")
        else:
            reason = (resp or {}).get("reason", "已是终态")
            print(f"  {MUTED}任务 {tid[:8]} 未停止（{reason}）{C.RESET}")

    def _cmd_candidates(self, _=""):
        """查看候选版本"""
        if not self.current_task_id:
            print(f"  {MUTED}暂无活跃任务{C.RESET}")
            return
        data = self._api_get(f"/tasks/{self.current_task_id}/candidates")
        if not data:
            print(f"  {MUTED}暂无候选版本{C.RESET}")
            return

        candidates = data.get("candidates", [])
        rec_id = data.get("recommended_version_id", "")

        print()
        print(f"  {HIGHLIGHT}{C.BOLD}候选版本列表{C.RESET}  ({len(candidates)} 个)")
        print(divider())

        for c in candidates:
            vid = c.get("version_id", "?")
            vn = c.get("version_number", 0)
            conf = c.get("ai_confidence", 0)
            status = c.get("status", "created")
            summary = c.get("ai_summary", "")
            is_rec = vid == rec_id

            color = SUCCESS if conf >= 70 else WARN if conf >= 50 else ERROR
            rec_tag = f" {SUCCESS}⭐ AI推荐{C.RESET}" if is_rec else ""

            print(f"  {color}{C.BOLD}v{vn}{C.RESET} [{vid}]  信心分: {color}{conf}{C.RESET}  状态: {status}{rec_tag}")
            if summary:
                print(f"    {MUTED}{summary}{C.RESET}")

        print()

    def _cmd_discuss(self, _=""):
        """参与闭环讨论"""
        # current_task_id 为空时 fallback 查最近 in_progress / paused 的 task
        # （用户通过 dashboard/API 提交的 task，REPL 不会自动设 current_task_id）
        task_id = self.current_task_id
        if not task_id:
            tasks = self._api_get("/tasks") or []
            active = [t for t in tasks if t.get("status") in ("in_progress", "paused", "planning")]
            if active:
                # 取时间最近的
                active.sort(key=lambda t: t.get("created_at", ""), reverse=True)
                task_id = active[0].get("task_id", "")
                print(f"  {MUTED}未选中任务，自动切到最近活跃任务 {task_id}{C.RESET}")
            else:
                print(f"  {MUTED}暂无活跃任务{C.RESET}")
                return

        data = self._api_get(f"/tasks/{task_id}/discussion")
        if not data or not data.get("has_discussion", True) or data.get("has_discussion") is False:
            print(f"  {MUTED}当前没有进行中的讨论{C.RESET}")
            return

        print()
        print(banner("闭环讨论", ACCENT))
        print()

        # summary（新 propose_alternatives 带）
        summary = data.get("summary", "")
        if summary:
            print(f"  {MUTED}{summary[:500]}{C.RESET}\n")

        # 显示问题（旧 AgentLoop 结构）
        issues = data.get("issues", [])
        if issues:
            print(f"  {ERROR}{C.BOLD}发现 {len(issues)} 个问题:{C.RESET}")
            for i, issue in enumerate(issues, 1):
                sev = issue.get("severity", "medium")
                color = ERROR if sev in ("critical", "high") else WARN if sev == "medium" else MUTED
                print(f"    {color}{i}. [{sev}] {issue.get('category', '')}: {issue.get('description', '')}{C.RESET}")
            print()

        # 显示方案（兼容 propose_alternatives 的 {id,title,description,pros,cons}
        # 和旧 AgentLoop 的 {proposal_id,title,description,ai_recommended,...}）
        proposals = data.get("proposals", [])
        if proposals:
            print(f"  {ACCENT}{C.BOLD}可选方案:{C.RESET}")
            for i, p in enumerate(proposals, 1):
                rec = f" {SUCCESS}⭐推荐{C.RESET}" if p.get("ai_recommended") else ""
                print(f"    {ACCENT}{i}. {p.get('title', '')}{C.RESET}{rec}")
                desc = p.get("description", "")
                if desc:
                    print(f"       {MUTED}{desc}{C.RESET}")
                pros = p.get("pros", "")
                cons = p.get("cons", "")
                if pros:
                    print(f"       {SUCCESS}优势: {pros}{C.RESET}")
                if cons:
                    print(f"       {WARN}劣势: {cons}{C.RESET}")
                if p.get("estimated_effort") or p.get("risk_level"):
                    print(f"       工作量: {p.get('estimated_effort', '?')}  风险: {p.get('risk_level', '?')}")
            print()

            # 让用户选择
            try:
                choice = input(f"  {ACCENT}选择方案 (编号, 或直接回车选AI推荐): {C.RESET}").strip()
                human_input = input(f"  {ACCENT}追加意见 (可选, 直接回车跳过): {C.RESET}").strip()

                # 选编号 → 取对应 proposal id；空回车 → 默认第一个（"AI 推荐"）
                if choice.isdigit() and 1 <= int(choice) <= len(proposals):
                    sel = proposals[int(choice) - 1]
                else:
                    sel = proposals[0]
                proposal_id = sel.get("id") or sel.get("proposal_id", "")

                self._api_post(f"/tasks/{task_id}/discussion/input", {
                    "proposal_id": proposal_id,
                    "human_input": human_input,
                })
                print(f"  {SUCCESS}✓ 已提交（选了：{sel.get('title', proposal_id)}）{C.RESET}")
            except (KeyboardInterrupt, EOFError):
                print(f"\n  {MUTED}已取消{C.RESET}")

        print()

    def _cmd_history(self, _=""):
        """查看迭代历史"""
        if not self.current_task_id:
            print(f"  {MUTED}暂无活跃任务{C.RESET}")
            return

        data = self._api_get(f"/tasks/{self.current_task_id}/iterations")
        if not data:
            print(f"  {MUTED}暂无迭代记录{C.RESET}")
            return

        print()
        print(f"  {HIGHLIGHT}{C.BOLD}迭代历史{C.RESET}  当前: #{data.get('current_iteration', 1)}/{data.get('max_iterations', 5)}")
        print(divider())

        for d in data.get("discussions", []):
            it = d.get("iteration", 1)
            trigger = d.get("trigger", "?")
            status = d.get("status", "?")
            color = SUCCESS if status == "resolved" else WARN
            print(f"  {color}迭代 #{it}{C.RESET}  触发: {trigger}  状态: {status}  问题: {d.get('issue_count', 0)}  方案: {d.get('proposal_count', 0)}")

        print()

    def _cmd_login(self, _=""):
        """交互式登录/切换 LLM 提供商"""
        import getpass

        print()
        print(banner("选择 AI 提供商", ACCENT))
        print()

        # 先查询当前状态
        current = self._api_get("/llm/status")
        if current:
            cur_provider = current.get("provider", "unknown")
            cur_model = current.get("model", "unknown")
            print(f"  {MUTED}当前提供商: {HIGHLIGHT}{cur_provider}{MUTED}  模型: {HIGHLIGHT}{cur_model}{C.RESET}")
            print()

        providers = [
            ("1", "claude", "Claude (Anthropic)", "claude-sonnet-4-6", "ANTHROPIC_API_KEY"),
            ("2", "kimi-code", "Kimi Code 2.5 (编程专用)", "kimi-k2.5", "KIMI_API_KEY"),
            ("3", "kimi", "Kimi K2.5 (通用)", "kimi-k2.5", "MOONSHOT_API_KEY"),
            ("4", "deepseek", "DeepSeek v4-pro", "deepseek-v4-pro", "DEEPSEEK_API_KEY"),
            ("5", "mock", "Mock 模式 (无需 Key)", "mock", None),
        ]

        for num, _, label, model, _ in providers:
            print(f"  {ACCENT}{C.BOLD}{num}{C.RESET}  {label}  {MUTED}({model}){C.RESET}")
        print()

        try:
            choice = input(f"  {ACCENT}选择提供商 [1/2/3/4/5]: {C.RESET}").strip()
        except (KeyboardInterrupt, EOFError):
            print(f"\n  {MUTED}已取消{C.RESET}\n")
            return

        # 匹配选择
        selected = None
        for num, provider_id, label, model, env_key in providers:
            if choice == num or choice.lower() == provider_id:
                selected = (provider_id, label, model, env_key)
                break

        if not selected:
            print(f"  {ERROR}无效选择{C.RESET}\n")
            return

        provider_id, label, default_model, env_key = selected

        # Mock 模式无需 key
        if provider_id == "mock":
            result = self._api_post("/llm/switch", {"provider": "mock"})
            if result and result.get("success"):
                print(f"  {SUCCESS}✓ 已切换到 Mock 模式{C.RESET}\n")
                self.config["_mock_fallback"] = False  # 主动选择 mock 不算回退
            else:
                print(f"  {ERROR}✗ 切换失败: {result.get('error', '未知错误')}{C.RESET}\n")
            return

        # 需要 API Key 的提供商
        print()
        import os
        existing_key = os.environ.get(env_key, "")
        if existing_key:
            masked = existing_key[:8] + "..." + existing_key[-4:] if len(existing_key) > 12 else "***"
            print(f"  {MUTED}检测到已有 Key: {masked}{C.RESET}")
            try:
                use_existing = input(f"  {ACCENT}使用已有 Key? [Y/n]: {C.RESET}").strip().lower()
            except (KeyboardInterrupt, EOFError):
                print(f"\n  {MUTED}已取消{C.RESET}\n")
                return
            if use_existing in ("", "y", "yes"):
                api_key = existing_key
            else:
                try:
                    api_key = getpass.getpass(f"  {ACCENT}输入 API Key: {C.RESET}")
                except (KeyboardInterrupt, EOFError):
                    print(f"\n  {MUTED}已取消{C.RESET}\n")
                    return
        else:
            if provider_id == "claude":
                print(f"  {MUTED}获取 Key: https://console.anthropic.com{C.RESET}")
            elif provider_id == "kimi-code":
                print(f"  {MUTED}获取 Key: https://kimi.com (Kimi Code 订阅){C.RESET}")
            elif provider_id == "kimi":
                print(f"  {MUTED}获取 Key: https://platform.moonshot.ai{C.RESET}")
            elif provider_id == "deepseek":
                print(f"  {MUTED}获取 Key: https://platform.deepseek.com{C.RESET}")
            try:
                api_key = getpass.getpass(f"  {ACCENT}输入 API Key: {C.RESET}")
            except (KeyboardInterrupt, EOFError):
                print(f"\n  {MUTED}已取消{C.RESET}\n")
                return

        if not api_key:
            print(f"  {ERROR}未输入 API Key，已取消{C.RESET}\n")
            return

        # 可选自定义模型
        try:
            custom_model = input(f"  {ACCENT}模型 (回车使用默认 {default_model}): {C.RESET}").strip()
        except (KeyboardInterrupt, EOFError):
            custom_model = ""
        model = custom_model if custom_model else default_model

        # 发起切换请求
        print(f"\n  {BRAND}⟳ 正在切换到 {label}...{C.RESET}")
        result = self._api_post("/llm/switch", {
            "provider": provider_id,
            "api_key": api_key,
            "model": model,
        })

        if result and result.get("success"):
            print(f"  {SUCCESS}✓ {result.get('message', '切换成功')}{C.RESET}")
            # 同步状态栏（_sb）和 config：让下一次 draw 立即反映新的 provider/model
            new_provider = result.get("provider", provider_id)
            new_model = result.get("model", model)
            self._sb["provider"] = new_provider
            self._sb["model"] = new_model
            self.config["llm_provider"] = new_provider
            self.config["model"] = new_model
            # ★ 清除 mock 回退标记
            self.config["_mock_fallback"] = False
            self.config.pop("_mock_fallback_reason", None)
            self.config.pop("_intended_provider", None)
        else:
            err = result.get("error", "未知错误") if result else "服务无响应"
            print(f"  {ERROR}✗ 切换失败: {err}{C.RESET}")
        print()

    def _cmd_web(self, _=""):
        url = f"{self._scheme}://{self._api_host}:{self.port}"
        print(f"  {ACCENT}打开 Dashboard: {url}{C.RESET}")
        webbrowser.open(url)

    def _cmd_config(self, _=""):
        print()
        print(f"  {HIGHLIGHT}{C.BOLD}当前配置{C.RESET}")
        print(divider())
        print(table_row("项目路径", self.project_path))
        print(table_row("服务地址", f"{self.host}:{self.port}"))

        # 实时查询当前 LLM 状态
        llm_status = self._api_get("/llm/status")
        if llm_status:
            print(table_row("LLM 提供商", llm_status.get("provider", "unknown")))
            print(table_row("模型", llm_status.get("model", "unknown")))
        else:
            print(table_row("LLM 提供商", self.config.get("llm_provider", "anthropic")))
            print(table_row("模型", self.config.get("model", "claude-sonnet-4-6")))

        print(table_row("最大迭代", str(self.config.get("max_iterations", 5))))
        print(table_row("评估阈值", str(self.config.get("min_eval_score", 70))))
        print(f"  {MUTED}使用 /login 切换 LLM 提供商{C.RESET}")
        print()

    def _cmd_clear(self, _=""):
        os.system("clear" if os.name != "nt" else "cls")

    def _cmd_verbose(self, args: str = ""):
        """切换控制台详细模式：on=显示完整 args/output（默认）, off=精简截断"""
        arg = (args or "").strip().lower()
        if arg in ("on", "1", "true", "yes", "y"):
            self._verbose = True
        elif arg in ("off", "0", "false", "no", "n"):
            self._verbose = False
        elif arg in ("toggle", ""):
            self._verbose = not self._verbose
        else:
            self._safe_print(f"  {ERROR}用法: /verbose [on|off|toggle]{C.RESET}")
            return
        state = "on (完整输出)" if self._verbose else "off (精简截断)"
        self._safe_print(f"  {SUCCESS}/verbose → {state}{C.RESET}")

    def _cmd_quit(self, _=""):
        self._shutdown()

    # ─── 续接检测 & 恢复 ──────────────────────────────────────

    # 续接意图关键词（中英文）
    _CONTINUE_KEYWORDS = [
        "继续", "接着", "上次", "之前的", "按之前", "接着做", "继续做",
        "重新来", "再试", "上一个", "刚才的", "恢复之前",
        "continue", "resume", "previous", "last time", "pick up",
    ]

    def _detect_continuation(self, text: str) -> bool:
        """检测用户输入是否包含续接意图"""
        text_lower = text.lower()
        return any(kw in text_lower for kw in self._CONTINUE_KEYWORDS)

    def _find_resumable_task(self, description: str) -> Optional[dict]:
        """查找与描述最匹配的可续接任务"""
        # 先尝试关键词匹配
        data = self._api_get("/tasks/resumable")
        if not data or not isinstance(data, list) or len(data) == 0:
            return None

        # 提取用户输入中的关键词做匹配
        import re
        stop_words = {"的", "了", "在", "是", "我", "有", "和", "就", "不", "都",
                      "一", "上", "也", "很", "到", "说", "要", "去", "你", "会",
                      "吧", "被", "还", "等", "能", "做", "再", "之前", "按", "继续",
                      "接着", "重新", "上次", "写一个", "一个",
                      "continue", "resume", "previous", "last", "before"}
        words = set(re.findall(r'[\u4e00-\u9fff]+|[a-zA-Z]+', description.lower()))
        keywords = words - stop_words

        best_match = None
        best_score = 0

        for task in data:
            task_words = set(re.findall(r'[\u4e00-\u9fff]+|[a-zA-Z]+', task["description"].lower()))
            overlap = len(keywords & task_words)
            for kw in keywords:
                if len(kw) >= 2 and kw in task["description"].lower():
                    overlap += 1
            if overlap > best_score:
                best_score = overlap
                best_match = task

        # 如果没有关键词匹配，返回最近的可续接任务
        if best_match is None and len(data) > 0:
            best_match = data[0]  # 已按时间倒序

        return best_match

    def _prompt_and_resume(self, task: dict, user_input: str):
        """提示用户确认并恢复历史任务"""
        task_id = task["task_id"]
        desc = task["description"]
        status = task.get("status", "?")
        progress = task.get("progress_percent", 0)

        print()
        self._safe_print(f"  {ACCENT}检测到续接意图，找到历史任务:{C.RESET}")
        self._safe_print(f"  {HIGHLIGHT}[{task_id}]{C.RESET} {desc}")
        self._safe_print(f"  {MUTED}状态: {status}  进度: {progress:.0f}%{C.RESET}")
        print()

        try:
            confirm = input(f"  {ACCENT}是否续接该任务? [Y/n]: {C.RESET}").strip().lower()
        except (KeyboardInterrupt, EOFError):
            self._safe_print(f"\n  {MUTED}已取消{C.RESET}")
            return

        if confirm in ("n", "no"):
            self._safe_print(f"  {MUTED}已跳过续接，创建新任务...{C.RESET}")
            self._create_task(user_input)
            return

        self._resume_from_checkpoint(task_id, user_input)

    def _resume_from_checkpoint(self, task_id: str, additional_context: str = ""):
        """通过 API 触发 checkpoint 恢复"""
        self._safe_print(f"  {BRAND}⟳ 正在从检查点恢复任务 [{task_id}]...{C.RESET}")

        # 重置 UI 状态
        self.current_task_id = task_id
        self._sb["task_id"] = task_id
        self._sb["status"] = "resuming"
        self._sb["step"] = "恢复中"
        self._sb["progress"] = 0
        self._sb["iteration"] = 0

        # 重置流程图
        for key in self.flow_state:
            self.flow_state[key] = "pending"
        self.flow_state["需求输入"] = "success"

        data = self._api_post(f"/tasks/{task_id}/resume-checkpoint", {
            "additional_context": additional_context,
        })

        if data and data.get("task_id"):
            resumed_step = data.get("resumed_from_step", 0)
            total = data.get("total_steps", 0)
            self._safe_print(f"  {SUCCESS}✓ 已恢复{C.RESET}  从步骤 {resumed_step}/{total} 继续")
            self._sb["status"] = "in_progress"
        else:
            err = data.get("error", "未知错误") if data else "服务无响应"
            self._safe_print(f"  {ERROR}✗ 恢复失败: {err}{C.RESET}")
            self._sb["status"] = "failed"

    def _cmd_continue(self, arg=""):
        """从检查点续接历史任务"""
        # 如果指定了 task_id
        if arg.strip():
            task_id = arg.strip()
            self._resume_from_checkpoint(task_id)
            return

        # 否则列出可续接的任务供选择
        data = self._api_get("/tasks/resumable")
        if not data or not isinstance(data, list) or len(data) == 0:
            self._safe_print(f"  {MUTED}没有找到可续接的历史任务{C.RESET}")
            self._safe_print(f"  {MUTED}(需要有 checkpoint 的失败/暂停任务){C.RESET}")
            return

        print()
        self._safe_print(f"  {HIGHLIGHT}{C.BOLD}可续接的历史任务{C.RESET}  ({len(data)} 个)")
        print(divider())

        for i, task in enumerate(data[:10], 1):
            status = task.get("status", "?")
            progress = task.get("progress_percent", 0)
            color = WARN if status == "paused" else ERROR
            desc = task.get("description", "")
            if len(desc) > 60:
                desc = desc[:57] + "..."
            self._safe_print(f"  {ACCENT}{C.BOLD}{i}{C.RESET}  [{task['task_id']}]  {color}{status}{C.RESET}  {progress:.0f}%  {desc}")

        print()
        try:
            choice = input(f"  {ACCENT}选择任务 (编号或 task_id, 直接回车选最近的): {C.RESET}").strip()
        except (KeyboardInterrupt, EOFError):
            self._safe_print(f"\n  {MUTED}已取消{C.RESET}")
            return

        # 解析选择
        if not choice:
            selected = data[0]
        elif choice.isdigit() and 1 <= int(choice) <= len(data):
            selected = data[int(choice) - 1]
        else:
            # 当作 task_id
            self._resume_from_checkpoint(choice)
            return

        # 可选补充说明
        try:
            ctx = input(f"  {ACCENT}补充说明 (可选, 直接回车跳过): {C.RESET}").strip()
        except (KeyboardInterrupt, EOFError):
            ctx = ""

        self._resume_from_checkpoint(selected["task_id"], ctx)

    # ─── 闲聊快速通道（不创建 task）─────────────────────────

    # 复用 agent_loop 的闲聊检测关键词
    _CHAT_KEYWORDS = [
        "你是谁", "你是什么", "你叫", "你用", "你现在", "你能",
        "什么模型", "哪个模型", "模型版本", "model version",
        "who are you", "what model", "which model",
        "你好", "hello", "hi ", "hey ", "嗨", "哈喽",
        "谢谢", "thanks", "thank you",
    ]
    _CODE_VERBS = [
        "实现", "添加", "增加", "改造", "重构", "优化代码", "接入", "生成",
        "写一个", "写个", "写段", "封装", "开发", "构建", "编译", "部署",
        "implement", "add ", "refactor", "build ", "compile", "deploy",
        "integrate", "create a", "write a", "write the",
    ]
    _BUG_KEYWORDS = [
        "应该是", "不应该", "会退出", "有问题", "有bug", "bug", "fix",
        "修复", "修一下", "修改", "不对", "不正确", "出错", "报错",
    ]

    def _is_chat_query(self, description: str) -> bool:
        """判断是否是纯闲聊/问答（不需要走规划→执行→评估流水线）"""
        desc = description.strip()
        low = desc.lower()
        if any(v in low for v in self._CODE_VERBS):
            return False
        if any(kw in low for kw in self._BUG_KEYWORDS):
            return False
        if any(kw in low for kw in self._CHAT_KEYWORDS):
            return True
        if len(desc) <= 30 and (desc.endswith("?") or desc.endswith("？")):
            return True
        return False

    def _quick_chat(self, message: str):
        """轻量闲聊：调 /api/chat，直接打印回复，不创建 task"""
        self._safe_print(f"  {MUTED}思考中...{C.RESET}")
        data = self._api_post("/chat", {"message": message})
        if data and "answer" in data:
            # 清除"思考中..."那一行，打印回复
            answer = data["answer"]
            self._safe_print(f"  {ACCENT}{answer}{C.RESET}")
        else:
            error = (data or {}).get("detail", "调用失败")
            self._safe_print(f"  {ERROR}✗ {error}{C.RESET}")

    # ─── 任务创建 ────────────────────────────────────────────

    def _create_task(self, description: str):
        """创建新的开发任务"""

        # ★ Mock 回退拦截：提醒用户当前不在真实模式
        if self.config.get("_mock_fallback"):
            self._safe_print(f"  {ERROR}{C.BOLD}⚠ 当前处于 Mock 模式（自动回退），LLM 响应均为固定模板！{C.RESET}")
            self._safe_print(f"  {WARN}  如需真实 AI 生成代码，请先使用 {HIGHLIGHT}/login{WARN} 配置 API Key{C.RESET}")
            try:
                confirm = input(f"  {ACCENT}仍要在 Mock 模式下继续? [y/N]: {C.RESET}").strip().lower()
                if confirm not in ("y", "yes"):
                    self._safe_print(f"  {MUTED}已取消。请使用 /login 配置 LLM 提供商后重试。{C.RESET}")
                    return
            except (KeyboardInterrupt, EOFError):
                self._safe_print(f"  {MUTED}已取消{C.RESET}")
                return

        self._safe_print(f"  {BRAND}⟳ 创建任务...{C.RESET}")

        # 重置流程图
        for key in self.flow_state:
            self.flow_state[key] = "pending"
        self.flow_state["需求输入"] = "running"

        # 重置状态栏
        self._sb["task_id"] = ""
        self._sb["status"] = "pending"
        self._sb["step"] = "创建任务"
        self._sb["progress"] = 0
        self._sb["iteration"] = 0

        data = self._api_post("/tasks", {
            "description": description,
            "project_path": self.project_path,
        })

        if data and "task_id" in data:
            self.current_task_id = data["task_id"]
            # task_created 事件会通过 WebSocket → _handle_event → _print_event 显示
            # 这里只设置状态，不重复打印
            self.flow_state["需求输入"] = "success"
        else:
            self.flow_state["需求输入"] = "failed"
            self._safe_print(f"  {ERROR}✗ 创建失败{C.RESET}")

    # ─── HTTP 辅助 ────────────────────────────────────────────

    @property
    def _api_host(self) -> str:
        """API 请求地址: 0.0.0.0 是绑定地址，实际请求用 127.0.0.1"""
        return "127.0.0.1" if self.host in ("0.0.0.0", "::") else self.host

    def _api_get(self, path: str) -> Optional[dict]:
        try:
            import urllib.request
            url = f"{self._scheme}://{self._api_host}:{self.port}/api{path}"
            req = urllib.request.Request(url)
            with urllib.request.urlopen(req, timeout=5) as resp:
                return json.loads(resp.read())
        except Exception:
            return None

    def _api_post(self, path: str, body: dict = None) -> Optional[dict]:
        try:
            import urllib.request
            import urllib.error
            url = f"{self._scheme}://{self._api_host}:{self.port}/api{path}"
            data = json.dumps(body or {}).encode()
            req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"}, method="POST")
            with urllib.request.urlopen(req, timeout=10) as resp:
                return json.loads(resp.read())
        except urllib.error.HTTPError as e:
            # 读取服务端返回的错误详情
            try:
                err_body = json.loads(e.read())
                return {"success": False, "error": f"HTTP {e.code}: {err_body.get('detail', err_body)}"}
            except Exception:
                return {"success": False, "error": f"HTTP {e.code}: {e.reason}"}
        except urllib.error.URLError as e:
            return {"success": False, "error": f"连接失败: {e.reason}"}
        except Exception as e:
            return {"success": False, "error": str(e)}

    # ─── 退出 ────────────────────────────────────────────

    def _shutdown(self):
        self.running = False
        # 还原终端滚动区域 + 清底栏，避免 ANSI 状态遗留到 shell
        try:
            self._teardown_split_screen()
        except Exception:
            pass
        print(f"\n  {MUTED}再见! 👋{C.RESET}\n")
        sys.exit(0)
