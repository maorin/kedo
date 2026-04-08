"""
kedo CLI — 终端主题 & 样式定义

统一的颜色方案和 UI 组件，保持终端输出美观一致
"""
from __future__ import annotations


# ─── ANSI Color Codes ────────────────────────────────────────────
class C:
    """ANSI 颜色常量"""
    RESET = "\033[0m"
    BOLD = "\033[1m"
    DIM = "\033[2m"
    ITALIC = "\033[3m"
    UNDERLINE = "\033[4m"

    # 基础色
    BLACK = "\033[30m"
    RED = "\033[31m"
    GREEN = "\033[32m"
    YELLOW = "\033[33m"
    BLUE = "\033[34m"
    MAGENTA = "\033[35m"
    CYAN = "\033[36m"
    WHITE = "\033[37m"

    # 亮色
    GRAY = "\033[90m"
    BRED = "\033[91m"
    BGREEN = "\033[92m"
    BYELLOW = "\033[93m"
    BBLUE = "\033[94m"
    BMAGENTA = "\033[95m"
    BCYAN = "\033[96m"
    BWHITE = "\033[97m"

    # 背景
    BG_RED = "\033[41m"
    BG_GREEN = "\033[42m"
    BG_YELLOW = "\033[43m"
    BG_BLUE = "\033[44m"
    BG_MAGENTA = "\033[45m"
    BG_CYAN = "\033[46m"


# ─── 语义色 ────────────────────────────────────────────
BRAND = C.BBLUE
ACCENT = C.BCYAN
SUCCESS = C.BGREEN
WARN = C.BYELLOW
ERROR = C.BRED
INFO = C.GRAY
MUTED = C.GRAY
HIGHLIGHT = C.BWHITE


# ─── UI 组件 ────────────────────────────────────────────
LOGO = f"""{BRAND}{C.BOLD}
  ██╗  ██╗███████╗██████╗  ██████╗
  ██║ ██╔╝██╔════╝██╔══██╗██╔═══██╗
  █████╔╝ █████╗  ██║  ██║██║   ██║
  ██╔═██╗ ██╔══╝  ██║  ██║██║   ██║
  ██║  ██╗███████╗██████╔╝╚██████╔╝
  ╚═╝  ╚═╝╚══════╝╚═════╝  ╚═════╝
{C.RESET}{MUTED}  AI Development Assistant v0.1.0{C.RESET}
"""

PROMPT = f"{BRAND}{C.BOLD}kedo{C.RESET}{MUTED} ❯ {C.RESET}"
PROMPT_CONT = f"{MUTED}  ... {C.RESET}"


def banner(text: str, color: str = BRAND) -> str:
    width = max(len(text) + 4, 40)
    line = "─" * width
    return f"{color}╭{line}╮\n│  {C.BOLD}{text}{C.RESET}{color}{' ' * (width - len(text) - 2)}│\n╰{line}╯{C.RESET}"


def status_badge(label: str, color: str) -> str:
    return f"{color}{C.BOLD} {label} {C.RESET}"


def step_line(icon: str, text: str, detail: str = "", color: str = INFO) -> str:
    out = f"  {icon}  {color}{text}{C.RESET}"
    if detail:
        out += f"  {MUTED}{detail}{C.RESET}"
    return out


def progress_bar(current: int, total: int, width: int = 30) -> str:
    if total == 0:
        return f"{MUTED}[{'─' * width}] 0%{C.RESET}"
    pct = current / total
    filled = int(width * pct)
    bar = f"{SUCCESS}{'█' * filled}{MUTED}{'░' * (width - filled)}{C.RESET}"
    return f"  [{bar}] {SUCCESS}{int(pct * 100)}%{C.RESET}"


def table_row(label: str, value: str, label_width: int = 16) -> str:
    return f"  {MUTED}{label:<{label_width}}{C.RESET} {value}"


def divider(char: str = "─", width: int = 50) -> str:
    return f"{MUTED}{char * width}{C.RESET}"


def status_bar(
    task_id: str = "",
    status: str = "",
    step: str = "",
    progress: float = 0,
    provider: str = "",
    model: str = "",
    iteration: int = 0,
    max_iterations: int = 5,
    width: int = 0,
) -> str:
    """
    渲染持久状态栏 — 显示在事件输出上方

    ┌─ kedo ─────────────────────────────────────────────────────┐
    │ ⚡ kimi-code/kimi-k2.5 │ Task: abc123 │ 规划中 │ ▰▰▰▱▱ 60% │
    └────────────────────────────────────────────────────────────┘
    """
    import shutil
    term_width = width if width > 0 else shutil.get_terminal_size((80, 24)).columns
    content_width = term_width - 4  # 左右边框各 2 字符

    # 状态颜色
    status_colors = {
        "planning": (BRAND, "规划中"),
        "in_progress": (ACCENT, "执行中"),
        "paused": (WARN, "已暂停"),
        "reviewing": (C.BYELLOW, "审查中"),
        "completed": (SUCCESS, "已完成"),
        "failed": (ERROR, "失败"),
    }
    s_color, s_label = status_colors.get(status, (MUTED, status or "空闲"))

    # 迷你进度条 (10格)
    bar_width = 10
    filled = int(bar_width * progress / 100) if progress > 0 else 0
    bar = f"{SUCCESS}{'▰' * filled}{MUTED}{'▱' * (bar_width - filled)}{C.RESET}"
    pct_str = f"{int(progress)}%"

    # 组装各段
    segments = []

    # Provider
    if provider and provider != "unknown":
        segments.append(f"{C.BOLD}⚡{C.RESET}{MUTED}{provider}{C.RESET}")
        if model and model != "unknown":
            segments[-1] = f"{C.BOLD}⚡{C.RESET}{MUTED}{provider}/{model}{C.RESET}"

    # Task
    if task_id:
        segments.append(f"{HIGHLIGHT}Task:{C.RESET}{MUTED}{task_id}{C.RESET}")

    # Status
    if status:
        segments.append(f"{s_color}{C.BOLD}{s_label}{C.RESET}")

    # Step (截断)
    if step and status not in ("completed", "failed", ""):
        display_step = step if len(step) <= 20 else step[:18] + ".."
        segments.append(f"{MUTED}{display_step}{C.RESET}")

    # Progress bar
    if task_id and progress >= 0:
        segments.append(f"{bar} {SUCCESS}{pct_str}{C.RESET}")

    # Iteration
    if iteration > 1:
        segments.append(f"{WARN}迭代#{iteration}/{max_iterations}{C.RESET}")

    # 无任务时的空闲状态
    if not task_id:
        segments = []
        if provider and provider != "unknown":
            segments.append(f"{C.BOLD}⚡{C.RESET}{MUTED}{provider}{C.RESET}")
            if model and model != "unknown":
                segments[-1] = f"{C.BOLD}⚡{C.RESET}{MUTED}{provider}/{model}{C.RESET}"
        segments.append(f"{MUTED}等待输入需求...{C.RESET}")

    separator = f" {MUTED}│{C.RESET} "
    content = separator.join(segments)

    # 渲染边框
    top = f"{MUTED}┌{'─' * (content_width + 2)}┐{C.RESET}"
    mid = f"{MUTED}│{C.RESET} {content}"
    bot = f"{MUTED}└{'─' * (content_width + 2)}┘{C.RESET}"

    return f"{top}\n{mid}\n{bot}"


def status_line(
    task_id: str = "",
    status: str = "",
    step: str = "",
    progress: float = 0,
    provider: str = "",
    model: str = "",
    iteration: int = 0,
    max_iterations: int = 5,
    width: int = 0,
) -> str:
    """
    渲染单行紧凑状态栏 — 无边框，直接嵌入输出流。

    ⚡ kimi-code/kimi-k2.5 │ Task:abc123 │ 规划中 │ Planning │ ▰▰▰▱▱ 30%
    """
    status_colors = {
        "pending": (MUTED, "等待中"),
        "planning": (BRAND, "规划中"),
        "in_progress": (ACCENT, "执行中"),
        "paused": (WARN, "已暂停"),
        "reviewing": (C.BYELLOW, "审查中"),
        "completed": (SUCCESS, "已完成"),
        "failed": (ERROR, "失败"),
    }
    s_color, s_label = status_colors.get(status, (MUTED, status or "空闲"))

    # 迷你进度条 (10格)
    bar_width = 10
    filled = int(bar_width * progress / 100) if progress > 0 else 0
    bar = f"{SUCCESS}{'▰' * filled}{MUTED}{'▱' * (bar_width - filled)}{C.RESET}"
    pct_str = f"{int(progress)}%"

    segments = []

    # Provider
    if provider and provider != "unknown":
        prov_str = f"{provider}/{model}" if model and model != "unknown" else provider
        segments.append(f"{C.BOLD}⚡{C.RESET}{MUTED}{prov_str}{C.RESET}")

    if task_id:
        segments.append(f"{HIGHLIGHT}Task:{C.RESET}{MUTED}{task_id[:8]}{C.RESET}")
        segments.append(f"{s_color}{C.BOLD}{s_label}{C.RESET}")
        if step and status not in ("completed", "failed", ""):
            display_step = step if len(step) <= 20 else step[:18] + ".."
            segments.append(f"{MUTED}{display_step}{C.RESET}")
        if progress >= 0:
            segments.append(f"{bar} {SUCCESS}{pct_str}{C.RESET}")
        if iteration > 1:
            segments.append(f"{WARN}迭代#{iteration}/{max_iterations}{C.RESET}")
    else:
        segments.append(f"{MUTED}等待输入需求...{C.RESET}")

    separator = f" {MUTED}│{C.RESET} "
    return f"  {separator.join(segments)}"


def flow_node(icon: str, name: str, status: str = "pending") -> str:
    color_map = {
        "pending": MUTED,
        "active": ACCENT + C.BOLD,
        "running": BRAND + C.BOLD,
        "success": SUCCESS,
        "failed": ERROR,
        "skipped": MUTED + C.ITALIC,
    }
    c = color_map.get(status, MUTED)
    indicator = {
        "pending": "○",
        "active": "◉",
        "running": "⟳",
        "success": "✓",
        "failed": "✗",
        "skipped": "⊘",
    }.get(status, "○")
    return f"  {c}{indicator} {icon} {name}{C.RESET}"
