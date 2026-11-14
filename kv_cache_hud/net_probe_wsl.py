from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass
from typing import Dict, List

_RE_RTT = re.compile(r"\brtt:(\d+(?:\.\d+)?)/(\d+(?:\.\d+)?)")
_RE_BYTES_ACKED = re.compile(r"\bbytes_acked:(\d+)")
_RE_BYTES_RCVD = re.compile(r"\bbytes_received:(\d+)")
_RE_RETRANS = re.compile(r"\bretrans:(\d+)/(\d+)")
_RE_PORT_443 = re.compile(r"(\S+):(\d+)\s+(\S+):443\b")


@dataclass
class WslDistro:
    name: str
    state: str
    version: str
    is_default: bool


def parse_wsl_list_output(raw_bytes: bytes) -> List[WslDistro]:
    if not raw_bytes:
        return []
    if b"\x00" in raw_bytes:
        text = raw_bytes.decode("utf-16-le", errors="ignore")
    else:
        text = raw_bytes.decode("utf-8", errors="ignore")
    distros: List[WslDistro] = []
    for line in text.splitlines():
        s = line.strip()
        if not s or s.upper().startswith("NAME"):
            continue
        is_def = s.startswith("*")
        if is_def:
            s = s[1:].strip()
        parts = re.split(r"\s{2,}", s)
        if len(parts) >= 3:
            distros.append(WslDistro(name=parts[0], state=parts[1], version=parts[2], is_default=is_def))
    return distros


def parse_ss_tin_output(text: str) -> List[Dict[str, object]]:
    sockets: List[Dict[str, object]] = []
    lines = text.splitlines()
    idx = 0
    while idx < len(lines):
        line = lines[idx]
        m = _RE_PORT_443.search(line)
        if m:
            detail = lines[idx + 1] if idx + 1 < len(lines) else ""
            rtt_m = _RE_RTT.search(detail)
            ack_m = _RE_BYTES_ACKED.search(detail)
            rcv_m = _RE_BYTES_RCVD.search(detail)
            ret_m = _RE_RETRANS.search(detail)
            sockets.append(
                {
                    "local_ip": m.group(1),
                    "local_port": int(m.group(2)),
                    "remote_ip": m.group(3),
                    "remote_port": 443,
                    "rtt_ms": float(rtt_m.group(1)) if rtt_m else 0.0,
                    "bytes_acked": int(ack_m.group(1)) if ack_m else 0,
                    "bytes_received": int(rcv_m.group(1)) if rcv_m else 0,
                    "retrans": int(ret_m.group(2)) if ret_m else 0,
                }
            )
            idx += 2
            continue
        idx += 1
    return sockets


class NetProbeWsl:
    def __init__(self, distro: str = "Ubuntu-22.04") -> None:
        self.distro = distro
        self._last_distro_scan = 0.0
        self._distros: List[WslDistro] = []

    def list_distros(self, now: float, force: bool = False) -> List[Dict[str, object]]:
        if not force and (now - self._last_distro_scan) < 10.0 and self._distros:
            return [d.__dict__ for d in self._distros]
        self._last_distro_scan = now
        try:
            proc = subprocess.run(
                ["wsl.exe", "-l", "-v"],
                capture_output=True,
                timeout=2.0,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
            self._distros = parse_wsl_list_output(proc.stdout)
        except (OSError, subprocess.SubprocessError):
            self._distros = []
        return [d.__dict__ for d in self._distros]

    def poll(self, now: float, active: bool) -> Dict[str, object]:
        distros = self.list_distros(now)
        running = [d["name"] for d in distros if str(d.get("state", "")).lower() == "running"]
        if not active or self.distro not in running:
            return {
                "distros": distros,
                "running_distros": running,
                "selected_distro": self.distro,
                "distro_running": self.distro in running,
                "sockets": [],
            }
        # Only invoke `ss -tin` when the target distro is already Running to avoid booting the VM.
        try:
            proc = subprocess.run(
                ["wsl.exe", "-d", self.distro, "--", "ss", "-tin", "dport", "=", ":443"],
                capture_output=True,
                timeout=1.5,
                text=True,
                encoding="utf-8",
                errors="replace",
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
            sockets = parse_ss_tin_output(proc.stdout)
        except (OSError, subprocess.SubprocessError):
            sockets = []
        return {
            "distros": distros,
            "running_distros": running,
            "selected_distro": self.distro,
            "distro_running": True,
            "sockets": sockets,
        }
