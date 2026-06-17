"""
prompt_toolkit-based REPL for kedo (Claude Code-style UI).

布局:
    [滚动区: 事件 / LLM 输出 / 命令回显]

    ▎ > what should I do next?_
       / commands · ⌃c stop · ⌃d quit              ⚡ kimi-code/kimi-k2.5  ⏳ task...

特性:
    - 输入框带 ▎ 装饰符 + bottom_toolbar 显示 LLM/任务状态
    - 事件流 patch_stdout 在 prompt 上方平滑滚动, 不刷屏
    - / 自动补全 (slash 命令)
    - ESC / Ctrl-C: 有活跃 task 停 task, 否则清空当前缓冲
    - Ctrl-D: 退出 REPL
    - 不可用时优雅 fallback 到 v1 REPL (kedo.py 控制)
"""
from __future__ import annotations

import asyncio
import logging
import os
import sys
import threading
from typing import Optional

from prompt_toolkit import PromptSession
from prompt_toolkit.application import get_app
from prompt_toolkit.completion import Completer, Completion
from prompt_toolkit.formatted_text import FormattedText
from prompt_toolkit.history import InMemoryHistory
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.patch_stdout import patch_stdout
from prompt_toolkit.styles import Style

from cli.repl import KedoREPL
from cli.theme import (
    ACCENT,
    BRAND,
    C,
    ERROR,
    HIGHLIGHT,
    LOGO,
    MUTED,
    SUCCESS,
    WARN,
    divider,
)

logger = logging.getLogger(__name__)


# ============================================================
# Slash 命令补全
# ============================================================

# 命令名 + 简短说明（display_meta 用）— 与 cli/repl.py 的 _handle_command 表保持同步
_SLASH_CMDS: list[tuple[str, str]] = [
    ("/help", "命令帮助"),
    ("/status", "查看任务状态"),
    ("/flow", "流程图（实时）"),
    ("/login", "切换 LLM 提供商"),
    ("/pause", "暂停当前任务"),
    ("/resume", "恢复已暂停任务"),
    ("/stop", "停止当前任务（同 Ctrl-C / ESC）"),
    ("/candidates", "查看候选版本"),
    ("/discuss", "参与闭环讨论 / 选择方案"),
    ("/history", "迭代历史"),
    ("/continue", "续接历史任务"),
    ("/loop", "自动循环：定时/自定步重跑任务"),
    ("/skill", "技能包：安装/列出/查看 Agent Skill"),
    ("/web", "在浏览器打开 Dashboard"),
    ("/config", "查看当前配置"),
    ("/clear", "清屏"),
    ("/verbose", "切换详细输出"),
    ("/quit", "退出"),
]


class _SlashCompleter(Completer):
    """只在第一个字符是 '/' 时给出补全；其它输入不打扰用户"""

    def get_completions(self, document, complete_event):
        text = document.text_before_cursor
        if not text or not text.startswith("/"):
            return
        prefix = text.lower()
        for cmd, desc in _SLASH_CMDS:
            if cmd.startswith(prefix):
                yield Completion(
                    cmd,
                    start_position=-len(text),
                    display=cmd,
                    display_meta=desc,
                )


# ============================================================
# 视觉样式
# ============================================================

_KEDO_STYLE = Style.from_dict({
    # 输入框装饰
    "prompt.bar": "fg:#5cf bold",
    "prompt.gt": "fg:#888",
    # 底栏左侧（快捷键 hint）
    "toolbar.hint": "fg:#666",
    "toolbar.sep": "fg:#444",
    # 底栏右侧（LLM / task 状态）
    "toolbar.brand": "fg:#5cf",
    "toolbar.task": "fg:#f59e0b",
    "toolbar.ok": "fg:#22c55e",
    "toolbar.err": "fg:#ef4444",
    "toolbar.muted": "fg:#666",
    # completion 菜单
    "completion-menu.completion": "bg:#1a1f33 fg:#d0d0d0",
    "completion-menu.completion.current": "bg:#0066cc fg:#ffffff",
    "completion-menu.meta.completion": "bg:#1a1f33 fg:#888888",
    "completion-menu.meta.completion.current": "bg:#0066cc fg:#cccccc",
})


# ============================================================
# v2 REPL
# ============================================================

class KedoREPLv2(KedoREPL):
    """
    prompt_toolkit 驱动的 REPL，复用父类的命令处理 / 事件流 / API 客户端。
    与 v1 的关键差异：
      - 弃用 DECSTBM 分屏，改 prompt_toolkit 内置布局
      - 输入用 PromptSession.prompt_async（async）
      - bottom_toolbar 替代手画 ANSI 状态栏
      - 事件打印走 patch_stdout，自动出现在 prompt 上方
    """

    def start(self):
        """复刻 v1 start() 的初始化顺序，最后跑 async REPL"""
        # 1) Banner
        print(LOGO)
        print(f"  {MUTED}项目路径: {HIGHLIGHT}{self.project_path}{C.RESET}")
        display_host = "127.0.0.1" if self.host in ("0.0.0.0", "::") else self.host
        print(f"  {MUTED}Dashboard: {ACCENT}{self._scheme}://{display_host}:{self.port}{C.RESET}")

        # 2) 起 server (embedded) 或标记 thin-client 已就绪
        if self.connect_url:
            print(f"  {MUTED}模式: {ACCENT}thin-client{MUTED}（连接已有 server，REPL 退出不影响 task）{C.RESET}")
            self.server_ready.set()
        else:
            self._start_server_thread()

        # 3) 拉 LLM 状态填充 _sb（底栏依赖）
        self._fetch_provider_status()

        # 4) 显示 LLM 状态
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

        print(f"\n  {MUTED}输入需求开始开发，或输入 {HIGHLIGHT}/help{MUTED} 查看命令；按 ESC 或 Ctrl-C 停止任务{C.RESET}")
        print(divider())
        print()

        # 5) 起事件监听（后台 WebSocket 线程，会调 self._print_event）
        self._start_event_listener()

        # 6) 跑 async prompt loop
        try:
            asyncio.run(self._async_repl())
        except (KeyboardInterrupt, EOFError):
            pass
        finally:
            self._shutdown()

    # ─── 主循环 ─────────────────────────────────────────────

    async def _async_repl(self):
        history = InMemoryHistory()
        kb = self._build_keybindings()

        session: PromptSession = PromptSession(
            history=history,
            completer=_SlashCompleter(),
            complete_while_typing=True,
            key_bindings=kb,
            bottom_toolbar=self._render_toolbar,
            style=_KEDO_STYLE,
            multiline=False,
            mouse_support=False,
        )
        self._pt_session = session

        prompt_msg = FormattedText([
            ("class:prompt.bar", "▎"),
            ("class:prompt.gt", " > "),
        ])

        with patch_stdout(raw=True):
            while self.running:
                try:
                    text = await session.prompt_async(prompt_msg)
                except (EOFError, KeyboardInterrupt):
                    break
                except Exception as e:
                    logger.exception("REPL prompt error: %s", e)
                    continue
                text = (text or "").strip()
                if not text:
                    continue

                if text.startswith("/"):
                    self._handle_command(text)
                else:
                    if self._detect_continuation(text):
                        matched_task = self._find_resumable_task(text)
                        if matched_task:
                            self._prompt_and_resume(matched_task, text)
                            continue
                    if self._is_chat_query(text):
                        self._quick_chat(text)
                        continue
                    self._create_task(text)

    # ─── Key bindings ─────────────────────────────────────────

    def _build_keybindings(self) -> KeyBindings:
        kb = KeyBindings()

        @kb.add("c-c")
        def _(event):
            """Ctrl-C: 有活跃 task 就停 task；否则清空当前缓冲"""
            buf = event.current_buffer
            if self.current_task_id:
                # patch_stdout 接管了 stdout，_cmd_stop 内的 print 会出现在上方
                self._cmd_stop()
                buf.reset()
            else:
                buf.reset()

        @kb.add("escape", eager=True)
        def _(event):
            """ESC: 与 Ctrl-C 行为一致；eager=True 防止被当 meta 前缀吞掉"""
            buf = event.current_buffer
            if self.current_task_id:
                self._cmd_stop()
                buf.reset()
            else:
                # 没有 task 时 ESC 仅清缓冲，不退出 REPL（避免误操作）
                buf.reset()

        @kb.add("c-d")
        def _(event):
            """Ctrl-D: 退出 REPL（仅在缓冲为空时）"""
            if not event.current_buffer.text:
                event.app.exit(exception=EOFError())

        return kb

    # ─── Bottom toolbar ───────────────────────────────────────

    def _render_toolbar(self):
        """每次重绘底栏由 prompt_toolkit 自动调"""
        sb = self._sb or {}
        provider = sb.get("provider") or "?"
        model = sb.get("model") or "?"
        task_id = sb.get("task_id") or ""
        status = sb.get("status") or ""
        progress = sb.get("progress") or 0

        left = [
            ("class:toolbar.hint", "  /"),
            ("class:toolbar.muted", " 命令  "),
            ("class:toolbar.sep", "·"),
            ("class:toolbar.muted", "  ⌃c / ESC 停止  "),
            ("class:toolbar.sep", "·"),
            ("class:toolbar.muted", "  ⌃d 退出"),
        ]

        right = []
        if task_id:
            stat_color = "class:toolbar.task"
            if status == "completed":
                stat_color = "class:toolbar.ok"
            elif status == "failed":
                stat_color = "class:toolbar.err"
            right.append((stat_color,
                          f"  ⏳ {task_id[:8]} {status} {int(progress)}%"))
        right.append(("class:toolbar.brand", f"  ⚡ {provider}/{model}  "))

        # 用空白把右半边推到行尾
        # FormattedText 不直接支持对齐，简化方案：左 + 任意空白 + 右
        # prompt_toolkit 会按窗口宽度截断 → 可接受
        sep = [("class:toolbar.muted", "    ")]
        return FormattedText(left + sep + right)

    # ─── 重写父类的渲染方法（避免 v1 的 DECSTBM 抖动）─────────

    def _setup_split_screen(self):
        """v2 不用 DECSTBM"""
        self._split_screen_active = False

    def _teardown_split_screen(self):
        return

    def _redraw_status_bar(self):
        """让 prompt_toolkit 自己重绘底栏"""
        try:
            app = get_app()
            if app and not app.is_done:
                app.invalidate()
        except Exception:
            pass  # prompt 还没起来或已退出

    def _safe_print(self, text: str):
        """主线程打印 — patch_stdout 已接管 stdout，直接 print 即可"""
        with self._stdout_lock:
            sys.stdout.write(f"{text}\n")
            sys.stdout.flush()
        self._redraw_status_bar()

    def _print_event(self, text: str):
        """后台 WebSocket 线程打印事件 — patch_stdout 会把它放到 prompt 上方"""
        with self._stdout_lock:
            buf = []
            if getattr(self, "_streaming_newline", False):
                buf.append("\n")
                self._streaming_newline = False
            buf.append(f"  {text}\n")
            sys.stdout.write("".join(buf))
            sys.stdout.flush()
        self._redraw_status_bar()


# ============================================================
# 工具函数: 决定走 v1 还是 v2
# ============================================================

def use_v2() -> bool:
    """
    什么时候走 v2:
      - stdin/stdout 是 tty
      - prompt_toolkit 可导入（启动时已做）
      - KEDO_LEGACY_REPL 未设
    """
    if os.environ.get("KEDO_LEGACY_REPL"):
        return False
    if not (sys.stdin.isatty() and sys.stdout.isatty()):
        return False
    return True
