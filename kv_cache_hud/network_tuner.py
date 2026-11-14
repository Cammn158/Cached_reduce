from __future__ import annotations

import subprocess
import winreg
from typing import Any, Dict, Optional

from .net_probe_win import is_user_admin

REG_TCPIP_PATH = r"SYSTEM\CurrentControlSet\Services\Tcpip\Parameters"
DEFAULT_KEEPALIVE_MS = 7_200_000
DEFAULT_INTERVAL_MS = 1_000
OPTIMIZED_KEEPALIVE_MS = 15_000
OPTIMIZED_INTERVAL_MS = 5_000

ENABLE_PS_CMD = (
    'New-ItemProperty -Path "HKLM:\\SYSTEM\\CurrentControlSet\\Services\\Tcpip\\Parameters" '
    '-Name "KeepAliveTime" -PropertyType DWord -Value 15000 -Force; '
    'New-ItemProperty -Path "HKLM:\\SYSTEM\\CurrentControlSet\\Services\\Tcpip\\Parameters" '
    '-Name "KeepAliveInterval" -PropertyType DWord -Value 5000 -Force'
)

RESTORE_PS_CMD = (
    'Remove-ItemProperty -Path "HKLM:\\SYSTEM\\CurrentControlSet\\Services\\Tcpip\\Parameters" '
    '-Name "KeepAliveTime","KeepAliveInterval" -ErrorAction SilentlyContinue'
)


def read_windows_keepalive() -> Dict[str, Any]:
    keepalive_ms: Optional[int] = None
    interval_ms: Optional[int] = None
    try:
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, REG_TCPIP_PATH, 0, winreg.KEY_READ) as key:
            try:
                val, _ = winreg.QueryValueEx(key, "KeepAliveTime")
                keepalive_ms = int(val)
            except FileNotFoundError:
                keepalive_ms = None
            try:
                val_i, _ = winreg.QueryValueEx(key, "KeepAliveInterval")
                interval_ms = int(val_i)
            except FileNotFoundError:
                interval_ms = None
    except OSError:
        pass

    effective_ms = keepalive_ms if keepalive_ms is not None else DEFAULT_KEEPALIVE_MS
    effective_interval = interval_ms if interval_ms is not None else DEFAULT_INTERVAL_MS
    is_default = keepalive_ms is None
    is_optimized = effective_ms <= 30_000

    return {
        "registry_path": f"HKLM\\{REG_TCPIP_PATH}",
        "raw_keepalive_ms": keepalive_ms,
        "effective_keepalive_ms": effective_ms,
        "effective_interval_ms": effective_interval,
        "is_default": is_default,
        "is_optimized": is_optimized,
        "is_admin": is_user_admin(),
        "label": (
            f"Đã tối ưu ({effective_ms // 1000}s)"
            if is_optimized
            else (
                "Mặc định Windows (7,200,000 ms / 2 giờ)"
                if is_default
                else f"Tùy chỉnh ({effective_ms} ms)"
            )
        ),
        "enable_ps_cmd": ENABLE_PS_CMD,
        "restore_ps_cmd": RESTORE_PS_CMD,
    }


def read_wsl_keepalive(distro: str, distro_running: bool) -> Dict[str, Any]:
    if not distro_running:
        return {
            "distro": distro,
            "running": False,
            "keepalive_s": None,
            "label": f"WSL2 ({distro}) đang tắt — không đánh thức VM",
        }
    try:
        proc = subprocess.run(
            ["wsl.exe", "-d", distro, "--", "sysctl", "-n", "net.ipv4.tcp_keepalive_time"],
            capture_output=True,
            timeout=1.5,
            text=True,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        val = int(proc.stdout.strip())
        return {
            "distro": distro,
            "running": True,
            "keepalive_s": val,
            "label": f"{val}s" + (" (Đã tối ưu)" if val <= 30 else " (Mặc định Linux 2 giờ)"),
        }
    except (OSError, ValueError, subprocess.SubprocessError):
        return {
            "distro": distro,
            "running": True,
            "keepalive_s": None,
            "label": "Không đọc được sysctl WSL2",
        }


def apply_keepalive_15s() -> Dict[str, Any]:
    if not is_user_admin():
        return {
            "ok": False,
            "requires_admin": True,
            "message": "Cần chạy bằng run_hud_admin.bat (quyền Administrator) để ghi Registry HKLM, hoặc chạy lệnh PowerShell kèm theo.",
            "powershell_cmd": ENABLE_PS_CMD,
            "status": read_windows_keepalive(),
        }
    try:
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, REG_TCPIP_PATH, 0, winreg.KEY_SET_VALUE) as key:
            winreg.SetValueEx(key, "KeepAliveTime", 0, winreg.REG_DWORD, OPTIMIZED_KEEPALIVE_MS)
            winreg.SetValueEx(key, "KeepAliveInterval", 0, winreg.REG_DWORD, OPTIMIZED_INTERVAL_MS)
        status = read_windows_keepalive()
        return {
            "ok": status["is_optimized"],
            "requires_admin": False,
            "message": "Đã áp dụng TCP KeepAliveTime = 15000ms & KeepAliveInterval = 5000ms (áp dụng cho các kết nối mở mới).",
            "status": status,
        }
    except OSError as exc:
        return {
            "ok": False,
            "requires_admin": True,
            "message": f"Không thể ghi Registry: {exc}",
            "status": read_windows_keepalive(),
        }


def restore_keepalive_default() -> Dict[str, Any]:
    if not is_user_admin():
        return {
            "ok": False,
            "requires_admin": True,
            "message": "Cần chạy bằng run_hud_admin.bat (quyền Administrator) để xóa key Registry về mặc định Windows.",
            "powershell_cmd": RESTORE_PS_CMD,
            "status": read_windows_keepalive(),
        }
    try:
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, REG_TCPIP_PATH, 0, winreg.KEY_SET_VALUE) as key:
            for name in ("KeepAliveTime", "KeepAliveInterval"):
                try:
                    winreg.DeleteValue(key, name)
                except FileNotFoundError:
                    pass
        status = read_windows_keepalive()
        return {
            "ok": status["is_default"],
            "requires_admin": False,
            "message": "Đã khôi phục KeepAliveTime về mặc định Windows (7,200,000 ms / 2 giờ).",
            "status": status,
        }
    except OSError as exc:
        return {
            "ok": False,
            "requires_admin": True,
            "message": f"Không thể xóa giá trị Registry: {exc}",
            "status": read_windows_keepalive(),
        }
