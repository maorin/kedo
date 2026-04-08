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

    # 尝试多个位置
    paths_to_try = [
        config_path,
        "kedo.yaml",
        "kedo.yml",
        ".kedo.yaml",
        os.path.expanduser("~/.config/kedo/config.yaml"),
        os.path.join(project_root, "config.yaml"),
    ]

    for p in paths_to_try:
        if p and Path(p).exists():
            with open(p) as f:
                config = yaml.safe_load(f) or {}
            break

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

    subparsers = parser.add_subparsers(dest="command")
    subparsers.add_parser("server", help="仅启动 Web 服务 (无 REPL)")

    args = parser.parse_args()

    # 加载配置
    config = load_config(args.config)

    # REPL 模式日志写入文件，避免污染 CLI 输出；server 模式输出到 stderr
    is_repl = args.command != "server"
    project_path_abs = os.path.abspath(args.project_path or ".")
    log_dir = config.get("storage_dir", os.path.join(project_path_abs, ".kedo"))
    Path(log_dir).mkdir(parents=True, exist_ok=True)
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

    # 确保 storage 目录
    storage_dir = config.get("storage_dir", os.path.join(project_path, ".kedo"))
    config["storage_dir"] = storage_dir
    Path(storage_dir).mkdir(parents=True, exist_ok=True)

    if args.command == "server":
        # 仅启动 Web 服务
        _run_server_only(config)
    else:
        # 交互式 REPL
        _run_repl(config, project_path)


def _run_repl(config: dict, project_path: str):
    """启动交互式 REPL"""
    try:
        from cli.repl import KedoREPL
        repl = KedoREPL(config=config, project_path=project_path)
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
