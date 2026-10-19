"""Run the optional trial control service with a single durable worker."""

from __future__ import annotations

import argparse
import os

import uvicorn


def main() -> None:
    parser = argparse.ArgumentParser(description="启动可选的 InstaBox 试用控制服务。")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8001)
    args = parser.parse_args()
    if not 1 <= args.port <= 65535:
        parser.error("port must be between 1 and 65535")
    # The launcher stores ownership and resource records, never world-readable.
    os.umask(0o077)
    uvicorn.run("src.instabox_launcher.app:create_app", factory=True,
                host=args.host, port=args.port, workers=1, proxy_headers=False)


if __name__ == "__main__":
    main()
