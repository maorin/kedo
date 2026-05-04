#!/usr/bin/env python3
"""
kedo — AI 开发助手命令行工具

用法:
    kedo                        # 在当前目录启动交互式 REPL
    kedo /path/to/project       # 指定项目路径
    kedo --port 9000            # 指定 Dashboard 端口
    kedo --provider anthropic   # 指定 LLM (anthropic/openai/ollama/mock)
    kedo server                 # 仅启动 Web 服务 (无 REPL)

示例:
    $ cd my-project
    $ kedo
    kedo ❯ 实现一个用户登录功能，包含 JWT 认证
    kedo ❯ /flow          # 查看流程图
    kedo ❯ /review        # 审查候选版本
    kedo ❯ /approve       # 批准
"""
from __future__ import annotations

import argparse
import logging
import os
import sys
from pathlib import Path
from typing import Optional

# 确保项目目录在 Python path 中
project_root = Path(__file__).parent
if str(project_root) not in sys.path:
    sys.path.insert(0, str(project_root))


def setup_logging(verbose: bool = False, log_to_file: bool = False, log_dir: str = "."):
    """
    配置日志

    Args:
        verbose: 是否显示 DEBUG 级别日志
        log_to_file: 是否将日志写入文件（REPL 模式下默认开启，避免污染 CLI 输出）
        log_dir: 日志文件存放目录
    """
    level = logging.DEBUG if verbose else logging.WARNING

    if log_to_file:
        # REPL 模式：日志写入文件，不污染 CLI
        log_path = Path(log_dir) / "kedo.log"
        logging.basicConfig(
            level=level,
            format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
            datefmt="%H:%M:%S",
            filename=str(log_path),
            filemode="a",
        )
        # 确保 uvicorn 的日志也进文件
        for name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
            uv_logger = logging.getLogger(name)
            uv_logger.handlers = []
            uv_logger.addHandler(logging.FileHandler(str(log_path)))
    else:
        logging.basicConfig(
            level=level,
            format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
            datefmt="%H:%M:%S",
        )


def load_config(config_path: str = None) -> dict:
    """加载配置 (YAML 文件 + 环境变量)"""
    import yaml

    config = {}

    # 1) 加载项目配置（第一个找到的）
    paths_to_try = [
        config_path,
        "kedo.yaml",
        "kedo.yml",
        ".kedo.yaml",
        os.path.join(project_root, "config.yaml"),
    ]

    for p in paths_to_try:
        if p and Path(p).exists():
            with open(p, encoding="utf-8") as f:
                config = yaml.safe_load(f) or {}
            break

    # 2) 合并用户配置（~/.config/kedo/config.yaml）— 用户态优先
    # 历史：原逻辑是 `if v and (not config.get(k))`（user 仅在 repo 为空时生效），
    # 与 memory 里"用户态优先"的契约相反。当 repo config 加了非空默认（如
    # reviewer_provider: "none"）后，user 设的 reviewer_provider 会被静默丢掉。
    # 现在改成：user 任何非空值都覆盖 repo。
    user_cfg_path = os.path.expanduser("~/.config/kedo/config.yaml")
    if Path(user_cfg_path).exists():
        with open(user_cfg_path, encoding="utf-8") as f:
            user_cfg = yaml.safe_load(f) or {}
        for k, v in user_cfg.items():
            if v not in (None, ""):
                config[k] = v

    # 环境变量覆盖
    env_map = {
        "KEDO_PORT": ("port", int),
        "KEDO_HOST": ("host", str),
        "KEDO_PROVIDER": ("llm_provider", str),
        "KEDO_MODEL": ("model", str),
        "ANTHROPIC_API_KEY": ("anthropic_api_key", str),
        "OPENAI_API_KEY": ("openai_api_key", str),
        "KIMI_API_KEY": ("kimi_api_key", str),
        "MOONSHOT_API_KEY": ("kimi_api_key", str),
    }
    for env_key, (config_key, conv) in env_map.items():
        val = os.environ.get(env_key)
        if val:
            config[config_key] = conv(val)

    return config


def main():
    parser = argparse.ArgumentParser(
        prog="kedo",
        description="kedo — AI 开发助手命令行工具",
        epilog="示例: kedo /path/to/project --port 9000",
    )
    parser.add_argument(
        "project_path", nargs="?", default=".",
        help="项目根目录 (默认: 当前目录)",
    )
    parser.add_argument("--port", type=int, default=None, help="Dashboard 端口 (默认: 8000)")
    parser.add_argument("--host", default=None, help="绑定地址 (默认: 127.0.0.1)")
    parser.add_argument("--provider", default=None, help="LLM 提供商: anthropic/openai/kimi-code/kimi/ollama/mock")
    parser.add_argument("--model", default=None, help="模型名称")
    parser.add_argument("--config", default=None, help="配置文件路径")
    parser.add_argument("--verbose", "-v", action="store_true", help="详细日志")
    parser.add_argument("--strict", action="store_true", help="严格模式: 缺少 API Key 时报错而非回退到 Mock")
    parser.add_argument(
        "--connect", default="http://localhost:8000",
        help="REPL 默认连接已运行的 kedo server (默认 http://localhost:8000)。"
             "探测失败自动 fallback 内嵌模式",
    )
    parser.add_argument(
        "--embedded", action="store_true",
        help="REPL 强制走内嵌模式（自己起 uvicorn），跳过 --connect 探测",
    )

    subparsers = parser.add_subparsers(dest="command")
    subparsers.add_parser("server", help="仅启动 Web 服务 (无 REPL)")

    args = parser.parse_args()

    # 加载配置
    config = load_config(args.config)

    # 判断 REPL 是 thin-client 还是 embedded：thin-client 不需要文件日志（server 自己记）
    is_repl = args.command != "server"
    connect_url = None
    if is_repl and not args.embedded:
        # 默认尝试连已运行的 server；探测在 _run_repl 里再做（提前打到文件日志会污染）
        connect_url = args.connect

    project_path_abs = os.path.abspath(args.project_path or ".")
    log_dir = config.get("storage_dir", os.path.join(project_path_abs, ".kedo"))
    # 实战 bug 修：log_dir/storage_dir 在 config.yaml 里通常写成相对路径
    # ".kedo/state"，没 abs 化时基于 cwd 解析（nohup 启动 cwd=$HOME →
    # 写到 ~/.kedo/state，跑偏）。这里相对 project_path 解析。
    if not os.path.isabs(log_dir):
        log_dir = os.path.join(project_path_abs, log_dir)
    Path(log_dir).mkdir(parents=True, exist_ok=True)
    # thin-client 模式（决定要等到 _run_repl 探测后才知）：先按"会落到文件"配，
    # 探测成功是 thin-client 时再清掉根 logger handler — 见 _run_repl
    setup_logging(verbose=args.verbose, log_to_file=is_repl, log_dir=log_dir)

    # 命令行参数覆盖
    if args.port:
        config["port"] = args.port
    if args.host:
        config["host"] = args.host
    if args.provider:
        config["llm_provider"] = args.provider
    if args.model:
        config["model"] = args.model
    if args.strict:
        config["strict_mode"] = True

    # 默认值
    config.setdefault("port", 8000)
    config.setdefault("host", "127.0.0.1")
    config.setdefault("llm_provider", "anthropic")

    project_path = os.path.abspath(args.project_path)
    config["project_path"] = project_path

    # 确保 storage 目录（StateManager 期望 .kedo/state 子目录，与 default 对齐）
    # ★ 实战 bug 修复：config.yaml 里 storage_dir: ".kedo/state" 是相对路径，
    #   StateManager 直接拿来 Path() 会基于进程 cwd 解析，导致 nohup 启动时
    #   task_index.json / *_checkpoint.json 跑到 $HOME/.kedo/state 而不是项目里。
    #   现在显式相对 project_path 解析。
    storage_dir = config.get("storage_dir", os.path.join(project_path, ".kedo", "state"))
    if not os.path.isabs(storage_dir):
        storage_dir = os.path.join(project_path, storage_dir)
    config["storage_dir"] = storage_dir
    Path(storage_dir).mkdir(parents=True, exist_ok=True)

    if args.command == "server":
        # 仅启动 Web 服务
        _run_server_only(config)
    else:
        # 交互式 REPL
        _run_repl(config, project_path, connect_url=connect_url)


def _probe_server(url: str, timeout: float = 1.0) -> bool:
    """探测 url/api/llm/status 是否可达，可达即视为已有 server 在跑。"""
    import urllib.request
    import urllib.error
    probe_url = url.rstrip("/") + "/api/llm/status"
    try:
        with urllib.request.urlopen(probe_url, timeout=timeout) as resp:
            return resp.status == 200
    except (urllib.error.URLError, urllib.error.HTTPError, OSError, TimeoutError):
        return False


def _run_repl(config: dict, project_path: str, connect_url: Optional[str] = None):
    """启动交互式 REPL（默认尝试 thin-client 连已有 server，连不上 fallback 内嵌）"""
    effective_connect: Optional[str] = None
    if connect_url:
        if _probe_server(connect_url):
            effective_connect = connect_url
            # thin-client 不产生本地 server log，文件 handler 是 setup_logging 提前装的，
            # 这里清掉避免污染 .kedo/state/kedo.log（server 自己写）
            import logging as _logging
            for h in list(_logging.getLogger().handlers):
                if isinstance(h, _logging.FileHandler):
                    _logging.getLogger().removeHandler(h)
                    h.close()
            print(f"  \033[32m✓ 已连接到 server: {connect_url}\033[0m")
        else:
            print(
                f"  \033[33m⚠ 探测 {connect_url} 未响应，回退内嵌模式（自己起 server）\033[0m\n"
                f"  \033[90m提示: 想强制内嵌模式可加 --embedded；想换地址用 --connect URL\033[0m"
            )

    # v2 (prompt_toolkit, Claude Code 风) 优先；不可用 / 非 tty / KEDO_LEGACY_REPL 设了
    # → 回退到 v1 (DECSTBM + readline)
    repl_cls = None
    try:
        from cli.repl_pt import KedoREPLv2, use_v2
        if use_v2():
            repl_cls = KedoREPLv2
    except ImportError:
        # prompt_toolkit 未安装 → 走 v1
        repl_cls = None

    if repl_cls is None:
        from cli.repl import KedoREPL
        repl_cls = KedoREPL

    try:
        repl = repl_cls(
            config=config,
            project_path=project_path,
            connect_url=effective_connect,
        )
        repl.start()
    except KeyboardInterrupt:
        print("\n  再见! 👋\n")
        sys.exit(0)


def _run_server_only(config: dict):
    """仅启动 Web 服务"""
    import uvicorn
    from api.server import create_app

    app = create_app(config)
    port = config.get("port", 8000)
    host = config.get("host", "0.0.0.0")

    print(f"\n  🚀 kedo server on http://{host}:{port}")
    print(f"  📊 Dashboard: http://localhost:{port}")
    print(f"  📖 API Docs:  http://localhost:{port}/docs\n")

    uvicorn.run(app, host=host, port=port, log_level="info")


if __name__ == "__main__":
    main()
