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

    def __init__(self, config: dict = None, project_path: str = "."):
        self.config = config or {}
        self.project_path = os.path.abspath(project_path)
        self.port = self.config.get("port", 8000)
        self.host = self.config.get("host", "127.0.0.1")

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
            "人工审查": "pending",
            "自动部署": "pending",
            "线上监控": "pending",
        }

        self._event_log: list[dict] = []

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
        print(f"  {MUTED}Dashboard: {ACCENT}http://{display_host}:{self.port}{C.RESET}")

        # 启动服务器
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

        uri = f"ws://{self.host}:{self.port}/api/ws"
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
                "Human Review": ("人工审查", "running"),
            },
            "step_completed": {
                "Planning": ("计划生成", "success"),
                "Analyze Requirements": ("代码生成", "success"),
                "Generate Code": ("代码生成", "success"),
                "Build & Lint": ("编译检查", "success"),
                "Run Tests": ("自动测试", "success"),
                "Quality Review": ("质量评估", "success"),
                "Human Review": ("人工审查", "success"),
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
            phase_label = {
                "planning": "📋 规划",
                "code_generate": "💻 代码生成",
                "evaluate": "📊 质量评估",
            }.get(phase, phase)
            self._print_event(f"{BRAND}⬆ LLM 请求{C.RESET} [{phase_label}]  {MUTED}model={model}{C.RESET}")
            if prompt:
                self._print_event(f"  {MUTED}prompt: {prompt}{C.RESET}")

        elif etype == "llm_response":
            phase = data.get("phase", "")
            summary = data.get("summary", "")
            phase_label = {
                "planning": "📋 规划",
                "code_generate": "💻 代码生成",
                "evaluate": "📊 质量评估",
                "auto_fix": "🔧 自动修复",
            }.get(phase, phase)
            self._print_event(f"{ACCENT}⬇ LLM 响应{C.RESET} [{phase_label}]")
            if summary:
                # 多行响应缩进展示
                for line in summary.split("\n")[:5]:
                    self._print_event(f"  {MUTED}{line}{C.RESET}")

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
            step = data.get("step", "")
            tool_type = data.get("tool_type", "")
            desc = data.get("description", "")
            attempt = data.get("attempt", 1)
            mx = data.get("max_retries", 1)
            retry_hint = f" (尝试 {attempt}/{mx})" if mx > 1 and attempt > 1 else ""
            # ★ 更新状态栏 step
            if step:
                self._sb["step"] = step
            tool_icon = {
                "code_generate": "💻",
                "build": "🔨",
                "test": "🧪",
                "evaluate": "📊",
                "review": "👤",
                "deploy": "🚀",
            }.get(tool_type, "⚙")
            self._print_event(f"{INFO}{tool_icon} 执行: {step}{retry_hint}{C.RESET}")
            if desc:
                self._print_event(f"  {MUTED}{desc[:100]}{C.RESET}")

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
            output = data.get("output", "")
            self._print_event(f"{SUCCESS}✓ {step} 完成{C.RESET}")
            if output:
                for line in output.split("\n")[:3]:
                    self._print_event(f"  {MUTED}{line}{C.RESET}")

        elif etype == "step_failed":
            step = data.get("step", "")
            error = data.get("error", "")
            self._print_event(f"{ERROR}✗ {step} 失败{C.RESET}")
            if error:
                self._print_event(f"  {ERROR}{error[:150]}{C.RESET}")

        elif etype == "candidate_created":
            self.flow_state["候选版本"] = "success"
            conf = data.get("ai_confidence", 0)
            self._print_event(f"{SUCCESS}📦 候选版本已创建{C.RESET} confidence={conf}")

        elif etype == "review_requested":
            self.flow_state["人工审查"] = "active"
            self._print_event(f"{WARN}⏸ 等待人工审查{C.RESET} — 输入 /review 查看候选版本")

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
                "reviewing": "审查中",
                "completed": "已完成",
                "failed": "失败",
            }.get(new_status, new_status)
            if new_status == "completed":
                self._sb["progress"] = 100
                self._sb["step"] = ""
                self._print_event(f"{SUCCESS}{C.BOLD}✓ 任务完成！{C.RESET}")
                self._print_flow()
            elif new_status == "failed":
                self._print_event(f"{ERROR}✗ 任务失败{C.RESET}")
            elif old_status and new_status:
                self._print_event(f"{INFO}状态: {old_status} → {status_label}{C.RESET}")

    # ─── 内联状态栏 (无 scroll region) ──────────────────────

    def _get_status_text(self) -> str:
        """生成当前状态行文本"""
        return status_line(**self._sb)

    def _safe_print(self, text: str):
        """线程安全地打印一行 (主线程用)"""
        with self._stdout_lock:
            sys.stdout.write(f"{text}\n")
            sys.stdout.flush()

    def _print_event(self, text: str):
        """
        线程安全地打印事件 (后台 WebSocket 线程调用)。

        终端布局 (状态行可见时):
            ...上面的输出...
            ⚡ kimi-code/kimi-k2.5 │ 空闲          ← 状态行 (上一行)
            kedo ❯ █                               ← prompt  (当前行)

        操作:
            1. 清除 prompt 行
            2. 如果状态行可见，上移清除状态行
            3. 打印事件
            4. 打印新的状态行
            5. 打印 prompt
        """
        with self._stdout_lock:
            buf = []
            # 如果上一个事件是流式 token（未换行），先换行
            if getattr(self, "_streaming_newline", False):
                buf.append("\n")
                self._streaming_newline = False
            buf.append("\r\033[2K")          # 清除 prompt 行
            if getattr(self, "_status_line_visible", False):
                buf.append("\033[A\033[2K")  # 上移并清除状态行
            buf.append(f"  {text}\n")        # 打印事件
            buf.append(f"{self._get_status_text()}\n")  # 新状态行
            buf.append(PROMPT)               # prompt
            sys.stdout.write("".join(buf))
            sys.stdout.flush()
            self._status_line_visible = True

    # ─── REPL 主循环 ────────────────────────────────────────────

    def _repl_loop(self):
        """
        交互式命令循环

        布局:
            [事件输出正常滚动]
            ⚡ kimi-code/kimi-k2.5 │ 空闲          ← 状态行
            kedo ❯ █                               ← prompt

        _print_event 会清除状态行+prompt，打印事件后重绘。
        """
        import readline  # 启用行编辑和历史

        # 标记: 状态行是否已输出 (用于 _print_event 判断是否需要上移清除)
        self._status_line_visible = False

        while self.running:
            try:
                # 打印状态行，再显示 prompt
                sl = self._get_status_text()
                sys.stdout.write(f"{sl}\n")
                sys.stdout.flush()
                self._status_line_visible = True

                user_input = input(PROMPT).strip()
            except EOFError:
                self._shutdown()
                break

            # 用户按回车后，状态行已随内容滚上去，标记不可见
            self._status_line_visible = False

            if not user_input:
                continue

            # 处理命令
            if user_input.startswith("/"):
                self._handle_command(user_input)
            else:
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
            "/review": self._cmd_review,
            "/r": self._cmd_review,
            "/approve": lambda a="": self._cmd_review_action("approve", a),
            "/reject": lambda a="": self._cmd_review_action("reject", a),
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
            "/clear": self._cmd_clear,
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
            ("/resume", "恢复执行"),
            ("/review, /r", "查看待审查的候选版本"),
            ("/approve [反馈]", "批准当前候选版本"),
            ("/reject [反馈]", "驳回并给出反馈"),
            ("/candidates, /c", "列出所有候选版本"),
            ("/discuss, /d", "参与闭环讨论（选择方案）"),
            ("/history", "查看迭代历史"),
            ("/web, /w", "在浏览器中打开 Dashboard"),
            ("/config", "查看当前配置"),
            ("/clear", "清屏"),
            ("/quit, /q", "退出"),
        ]
        for cmd, desc in cmds:
            print(f"  {ACCENT}{cmd:<24}{C.RESET} {desc}")
        print()
        print(f"  {MUTED}直接输入自然语言即可创建开发任务{C.RESET}")
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
        color = {"in_progress": BRAND, "completed": SUCCESS, "paused": WARN, "failed": ERROR, "reviewing": ACCENT}.get(status, MUTED)

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
            ("👤", "人工审查"),
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
                elif name == "人工审查":
                    # 不打印箭头 — 有审查分支在上面显示
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

    def _cmd_review(self, _=""):
        """查看待审查候选版本"""
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
        print(f"  {MUTED}使用 /approve 或 /reject [反馈] 提交审查{C.RESET}")
        print()

    def _cmd_review_action(self, decision: str, feedback: str = ""):
        if not self.current_task_id:
            print(f"  {MUTED}暂无活跃任务{C.RESET}")
            return

        # 获取推荐版本
        data = self._api_get(f"/tasks/{self.current_task_id}/candidates")
        version_id = ""
        if data:
            version_id = data.get("recommended_version_id", "")
            if not version_id:
                candidates = data.get("candidates", [])
                if candidates:
                    version_id = candidates[-1].get("version_id", "")

        self._api_post(f"/tasks/{self.current_task_id}/review", {
            "decision": decision,
            "version_id": version_id,
            "feedback": feedback,
            "test_notes": feedback,
        })

        if decision == "approve":
            print(f"  {SUCCESS}✓ 已批准{C.RESET}  版本: {version_id}")
        else:
            print(f"  {ERROR}✗ 已驳回{C.RESET}  版本: {version_id}  反馈: {feedback}")

    def _cmd_candidates(self, _=""):
        self._cmd_review()

    def _cmd_discuss(self, _=""):
        """参与闭环讨论"""
        if not self.current_task_id:
            print(f"  {MUTED}暂无活跃任务{C.RESET}")
            return

        data = self._api_get(f"/tasks/{self.current_task_id}/discussion")
        if not data or not data.get("has_discussion", True) or data.get("has_discussion") is False:
            print(f"  {MUTED}当前没有进行中的讨论{C.RESET}")
            return

        print()
        print(banner("闭环讨论", ACCENT))
        print()

        # 显示问题
        issues = data.get("issues", [])
        if issues:
            print(f"  {ERROR}{C.BOLD}发现 {len(issues)} 个问题:{C.RESET}")
            for i, issue in enumerate(issues, 1):
                sev = issue.get("severity", "medium")
                color = ERROR if sev in ("critical", "high") else WARN if sev == "medium" else MUTED
                print(f"    {color}{i}. [{sev}] {issue.get('category', '')}: {issue.get('description', '')}{C.RESET}")
            print()

        # 显示方案
        proposals = data.get("proposals", [])
        if proposals:
            print(f"  {ACCENT}{C.BOLD}可选方案:{C.RESET}")
            for i, p in enumerate(proposals, 1):
                rec = f" {SUCCESS}⭐推荐{C.RESET}" if p.get("ai_recommended") else ""
                print(f"    {ACCENT}{i}. {p.get('title', '')}{C.RESET}{rec}")
                print(f"       {MUTED}{p.get('description', '')}{C.RESET}")
                print(f"       工作量: {p.get('estimated_effort', '?')}  风险: {p.get('risk_level', '?')}")
            print()

            # 让用户选择
            try:
                choice = input(f"  {ACCENT}选择方案 (编号, 或直接回车选AI推荐): {C.RESET}").strip()
                human_input = input(f"  {ACCENT}追加意见 (可选, 直接回车跳过): {C.RESET}").strip()

                proposal_id = ""
                if choice.isdigit() and 1 <= int(choice) <= len(proposals):
                    proposal_id = proposals[int(choice) - 1].get("proposal_id", "")

                self._api_post(f"/tasks/{self.current_task_id}/discussion/input", {
                    "proposal_id": proposal_id,
                    "human_input": human_input,
                })
                print(f"  {SUCCESS}✓ 已提交讨论意见{C.RESET}")
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
            ("1", "claude", "Claude (Anthropic)", "claude-sonnet-4-20250514", "ANTHROPIC_API_KEY"),
            ("2", "kimi-code", "Kimi Code 2.5 (编程专用)", "kimi-k2.5", "KIMI_API_KEY"),
            ("3", "kimi", "Kimi K2.5 (通用)", "kimi-k2.5", "MOONSHOT_API_KEY"),
            ("4", "mock", "Mock 模式 (无需 Key)", "mock", None),
        ]

        for num, _, label, model, _ in providers:
            print(f"  {ACCENT}{C.BOLD}{num}{C.RESET}  {label}  {MUTED}({model}){C.RESET}")
        print()

        try:
            choice = input(f"  {ACCENT}选择提供商 [1/2/3/4]: {C.RESET}").strip()
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
            # ★ 清除 mock 回退标记
            self.config["_mock_fallback"] = False
            self.config.pop("_mock_fallback_reason", None)
            self.config.pop("_intended_provider", None)
        else:
            err = result.get("error", "未知错误") if result else "服务无响应"
            print(f"  {ERROR}✗ 切换失败: {err}{C.RESET}")
        print()

    def _cmd_web(self, _=""):
        url = f"http://{self.host}:{self.port}"
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
            print(table_row("模型", self.config.get("model", "claude-sonnet-4-20250514")))

        print(table_row("最大迭代", str(self.config.get("max_iterations", 5))))
        print(table_row("评估阈值", str(self.config.get("min_eval_score", 70))))
        print(f"  {MUTED}使用 /login 切换 LLM 提供商{C.RESET}")
        print()

    def _cmd_clear(self, _=""):
        os.system("clear" if os.name != "nt" else "cls")

    def _cmd_quit(self, _=""):
        self._shutdown()

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
            url = f"http://{self._api_host}:{self.port}/api{path}"
            req = urllib.request.Request(url)
            with urllib.request.urlopen(req, timeout=5) as resp:
                return json.loads(resp.read())
        except Exception:
            return None

    def _api_post(self, path: str, body: dict = None) -> Optional[dict]:
        try:
            import urllib.request
            import urllib.error
            url = f"http://{self._api_host}:{self.port}/api{path}"
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
        print(f"\n  {MUTED}再见! 👋{C.RESET}\n")
        sys.exit(0)
