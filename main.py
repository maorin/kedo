"""
kedo — 启动入口

用法:
    python main.py                          # 使用默认配置启动
    python main.py --config config.yaml     # 使用指定配置文件
    python main.py --port 8080              # 指定端口
"""
from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

import uvicorn
import yaml

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger(__name__)


def load_config(config_path: str = "config.yaml") -> dict:
    """加载配置文件"""
    path = Path(config_path)
    if path.exists():
        with open(path, encoding="utf-8") as f:
            return yaml.safe_load(f) or {}
    logger.warning(f"Config file not found: {config_path}, using defaults")
    return {}


def main():
    parser = argparse.ArgumentParser(description="kedo")
    parser.add_argument("--config", default="config.yaml", help="Config file path")
    parser.add_argument("--host", default="0.0.0.0", help="Server host")
    parser.add_argument("--port", type=int, default=8000, help="Server port")
    parser.add_argument("--reload", action="store_true", help="Enable auto-reload")
    args = parser.parse_args()

    config = load_config(args.config)
    config["host"] = args.host
    config["port"] = args.port

    # 确保存储目录存在
    storage_dir = config.get("storage_dir", ".kedo/state")
    Path(storage_dir).mkdir(parents=True, exist_ok=True)

    logger.info(f"Starting kedo on {args.host}:{args.port}")
    logger.info(f"Dashboard: http://localhost:{args.port}")
    logger.info(f"API docs:  http://localhost:{args.port}/docs")

    # 创建 app 并启动
    from api.server import create_app
    app = create_app(config)

    uvicorn.run(
        app,
        host=args.host,
        port=args.port,
        reload=args.reload,
        log_level="info",
    )


if __name__ == "__main__":
    main()
