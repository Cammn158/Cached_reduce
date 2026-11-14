from __future__ import annotations

import argparse
import asyncio

from .config import DEFAULT_PORT
from .server import run_forever


def main() -> None:
    parser = argparse.ArgumentParser(description="KV Cache & Step Lifecycle HUD")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument("--no-browser", action="store_true")
    args = parser.parse_args()

    try:
        asyncio.run(run_forever(host=args.host, port=args.port, open_browser=not args.no_browser))
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
