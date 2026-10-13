"""Command-line launcher for the TranslatedSubs API."""

from __future__ import annotations

import argparse
from typing import Sequence

import uvicorn
from src.config.bootstrap import load_bootstrap


def _port(value: str) -> int:
    try:
        port = int(value)
    except (TypeError, ValueError) as exc:
        raise argparse.ArgumentTypeError("端口必须是整数") from exc
    if not 1 <= port <= 65535:
        raise argparse.ArgumentTypeError("端口必须在 1 到 65535 之间")
    return port


def build_parser() -> argparse.ArgumentParser:
    http = load_bootstrap().get("http", {})
    parser = argparse.ArgumentParser(
        prog="python -m src.cli",
        description="启动 TranslatedSubs API 和 Web 工作台。",
    )
    parser.add_argument(
        "--debug",
        action="store_true",
        help="跳过启动时的网站连通性检查；实际业务请求仍按正常规则执行。",
    )
    parser.add_argument(
        "--host",
        default=str(http.get("host", "127.0.0.1")),
        help="监听地址（默认从 config.local.json 读取）",
    )
    parser.add_argument(
        "--port",
        type=_port,
        default=_port(str(http.get("port", 8000))),
        help="监听端口（默认从 config.local.json 读取）",
    )
    parser.add_argument(
        "--reload",
        action="store_true",
        help="启用 Uvicorn 自动重载，适合本地开发。",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> None:
    args = build_parser().parse_args(argv)
    target = "src.handler.app:create_debug_app" if args.debug else "src.handler.app:app"
    uvicorn.run(
        target,
        factory=args.debug,
        host=args.host,
        port=args.port,
        reload=args.reload,
    )


if __name__ == "__main__":  # pragma: no cover - exercised by the CLI itself
    main()
