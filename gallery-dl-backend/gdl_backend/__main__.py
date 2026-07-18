from __future__ import annotations

import argparse

import uvicorn

from .app import create_app
from .config import AppSettings


def main() -> None:
    parser = argparse.ArgumentParser(description="gallery-dl + native proxy backend")
    parser.add_argument("--config", help="JSON 配置文件路径")
    parser.add_argument("--host", help="覆盖监听地址")
    parser.add_argument("--port", type=int, help="覆盖监听端口")
    args = parser.parse_args()
    settings = AppSettings.load(args.config)
    if args.host:
        settings.server.host = args.host
    if args.port:
        settings.server.port = args.port
    settings.validate()
    uvicorn.run(
        create_app(settings),
        host=settings.server.host,
        port=settings.server.port,
        log_level="info",
    )


if __name__ == "__main__":
    main()
