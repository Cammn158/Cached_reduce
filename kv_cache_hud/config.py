from __future__ import annotations

import ipaddress
import json
import os
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import List, Tuple

DEFAULT_PORT = 3380
PORT_SCAN_RANGE = range(3380, 3391)

GOOGLE_IPV4_CIDRS: Tuple[str, ...] = (
    "172.217.0.0/16",
    "142.250.0.0/15",
    "172.253.0.0/16",
    "74.125.0.0/16",
    "216.58.192.0/19",
    "108.177.0.0/17",
    "64.233.160.0/19",
    "209.85.128.0/17",
    "216.239.32.0/19",
    "34.0.0.0/9",
    "35.184.0.0/13",
)

GOOGLE_IPV6_CIDRS: Tuple[str, ...] = (
    "2404:6800::/32",
    "2607:f8b0::/32",
    "2800:3f0::/32",
    "2a00:1450::/32",
)

GOOGLE_V4_NETWORKS = tuple(ipaddress.IPv4Network(c) for c in GOOGLE_IPV4_CIDRS)
GOOGLE_V6_NETWORKS = tuple(ipaddress.IPv6Network(c) for c in GOOGLE_IPV6_CIDRS)


def is_google_ip(ip_str: str) -> bool:
    if not ip_str:
        return False
    try:
        addr = ipaddress.ip_address(ip_str)
    except ValueError:
        return False
    nets = GOOGLE_V4_NETWORKS if addr.version == 4 else GOOGLE_V6_NETWORKS
    return any(addr in net for net in nets)


def default_antigravity_dir() -> Path:
    env_dir = os.environ.get("ANTIGRAVITY_HOME")
    if env_dir:
        return Path(env_dir)
    return Path.home() / ".gemini" / "antigravity"


def project_root_dir() -> Path:
    return Path(__file__).resolve().parent.parent


@dataclass
class HudSettings:
    host: str = "127.0.0.1"
    port: int = DEFAULT_PORT
    ttl_seconds: float = 300.0
    slow_tpu_seconds: float = 90.0
    bloat_threshold_tokens: int = 15000
    cache_miss_ratio_threshold: float = 0.50
    cache_miss_min_prev_context: int = 10000
    auto_follow: bool = True
    pinned_session_id: str = ""
    environment: str = "windows"
    wsl_distro: str = "Ubuntu-22.04"
    sound_enabled: bool = True
    toast_enabled: bool = True
    guard_mode: str = "auto"  # auto | on | off
    auto_guard_tokens: int = 50000
    session_aliases: dict[str, str] = field(default_factory=dict)
    lifetime_saved_tokens: int = 0
    lifetime_guard_rescued_tokens: int = 0
    lifetime_handoff_rescued_tokens: int = 0
    lifetime_live_steps: int = 0

    @classmethod
    def load(cls, path: Path) -> "HudSettings":
        if not path.exists():
            return cls()
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return cls()
        defaults = asdict(cls())
        merged = {k: raw.get(k, v) for k, v in defaults.items()}
        if not isinstance(merged.get("session_aliases"), dict):
            merged["session_aliases"] = {}
        return cls(**merged)

    def save(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(asdict(self), ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(path)
