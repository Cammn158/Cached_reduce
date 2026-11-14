from __future__ import annotations

import argparse
import asyncio
import json
import sys
import urllib.request
import webbrowser

from .config import DEFAULT_PORT
from .server import run_forever


def is_hud_instance_alive(host: str, port: int, timeout: float = 0.6) -> bool:
    probe_host = "127.0.0.1" if host in ("0.0.0.0", "::", "") else host
    url = f"http://{probe_host}:{port}/api/state"
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:
            if resp.status != 200:
                return False
            data = json.loads(resp.read().decode("utf-8"))
            return isinstance(data, dict) and "fsm" in data and "network" in data
    except (OSError, ValueError):
        return False


def main() -> None:
    parser = argparse.ArgumentParser(description="KV Cache & Step Lifecycle HUD")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument("--no-browser", action="store_true")
    parser.add_argument("--force-new", action="store_true")
    parser.add_argument("--reuse-only", action="store_true")
    args = parser.parse_args()

    probe_host = "127.0.0.1" if args.host in ("0.0.0.0", "::", "") else args.host
    already_running = (not args.force_new) and is_hud_instance_alive(probe_host, args.port)
    if args.reuse_only:
        if already_running:
            if not args.no_browser:
                webbrowser.open(f"http://{probe_host}:{args.port}")
            sys.exit(0)
        sys.exit(1)

    if already_running:
        url = f"http://{probe_host}:{args.port}"
        print(f"[KV Cache HUD] Already running at {url} — opening browser.")
        if not args.no_browser:
            webbrowser.open(url)
        return

    try:
        asyncio.run(run_forever(host=args.host, port=args.port, open_browser=not args.no_browser))
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
