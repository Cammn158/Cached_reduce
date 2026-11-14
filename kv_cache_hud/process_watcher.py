from __future__ import annotations

import ctypes
import os
from ctypes import wintypes
from dataclasses import dataclass
from typing import Dict, List, Set

TH32CS_SNAPPROCESS = 0x00000002
PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
IGNORED_HELPER_CHILDREN = frozenset({"conhost.exe", "openconsole.exe", "windowsterminal.exe"})
LOCAL_TOOL_EXECUTABLES = frozenset({
    "powershell.exe",
    "pwsh.exe",
    "cmd.exe",
    "wsl.exe",
    "bash.exe",
    "python.exe",
    "node.exe",
    "git.exe",
    "gcc.exe",
    "g++.exe",
    "clang.exe",
    "cmake.exe",
    "ninja.exe",
    "msbuild.exe",
    "dotnet.exe",
    "cargo.exe",
    "go.exe",
})


class PROCESSENTRY32W(ctypes.Structure):
    _fields_ = [
        ("dwSize", wintypes.DWORD),
        ("cntUsage", wintypes.DWORD),
        ("th32ProcessID", wintypes.DWORD),
        ("th32DefaultHeapID", ctypes.c_size_t),
        ("th32ModuleID", wintypes.DWORD),
        ("cntThreads", wintypes.DWORD),
        ("th32ParentProcessID", wintypes.DWORD),
        ("pcPriClassBase", wintypes.LONG),
        ("dwFlags", wintypes.DWORD),
        ("szExeFile", ctypes.c_wchar * 260),
    ]


class IO_COUNTERS(ctypes.Structure):
    _fields_ = [
        ("ReadOperationCount", ctypes.c_uint64),
        ("WriteOperationCount", ctypes.c_uint64),
        ("OtherOperationCount", ctypes.c_uint64),
        ("ReadTransferCount", ctypes.c_uint64),
        ("WriteTransferCount", ctypes.c_uint64),
        ("OtherTransferCount", ctypes.c_uint64),
    ]


class PROCESS_MEMORY_COUNTERS(ctypes.Structure):
    _fields_ = [
        ("cb", wintypes.DWORD),
        ("PageFaultCount", wintypes.DWORD),
        ("PeakWorkingSetSize", ctypes.c_size_t),
        ("WorkingSetSize", ctypes.c_size_t),
        ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
        ("QuotaPagedPoolUsage", ctypes.c_size_t),
        ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
        ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
        ("PagefileUsage", ctypes.c_size_t),
        ("PeakPagefileUsage", ctypes.c_size_t),
    ]


_kernel32 = ctypes.windll.kernel32
_psapi = ctypes.windll.psapi


@dataclass
class ProcNode:
    pid: int
    ppid: int
    name: str


class ProcessWatcher:
    def __init__(self, target_names: tuple[str, ...] = ("language_server.exe", "language_server_windows_x64.exe")) -> None:
        self.target_names = {n.lower() for n in target_names}

    def snapshot_processes(self) -> List[ProcNode]:
        snap = _kernel32.CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0)
        if snap == wintypes.HANDLE(-1).value:
            return []
        nodes: List[ProcNode] = []
        try:
            entry = PROCESSENTRY32W()
            entry.dwSize = ctypes.sizeof(PROCESSENTRY32W)
            ok = _kernel32.Process32FirstW(snap, ctypes.byref(entry))
            while ok:
                nodes.append(
                    ProcNode(
                        pid=int(entry.th32ProcessID),
                        ppid=int(entry.th32ParentProcessID),
                        name=str(entry.szExeFile),
                    )
                )
                ok = _kernel32.Process32NextW(snap, ctypes.byref(entry))
        finally:
            _kernel32.CloseHandle(snap)
        return nodes

    def inspect_targets(self) -> Dict[str, object]:
        nodes = self.snapshot_processes()
        by_pid: Dict[int, ProcNode] = {n.pid: n for n in nodes}
        children_map: Dict[int, List[ProcNode]] = {}
        target_pids: List[int] = []

        for n in nodes:
            children_map.setdefault(n.ppid, []).append(n)
            if n.name.lower() in self.target_names:
                target_pids.append(n.pid)

        descendants: List[Dict[str, object]] = []
        seen: Set[int] = set(target_pids)
        queue = list(target_pids)

        while queue:
            cur = queue.pop(0)
            for child in children_map.get(cur, []):
                if child.pid in seen:
                    continue
                seen.add(child.pid)
                queue.append(child.pid)
                lname = child.name.lower()
                if lname in IGNORED_HELPER_CHILDREN:
                    continue
                descendants.append(
                    {
                        "pid": child.pid,
                        "ppid": child.ppid,
                        "name": child.name,
                        "is_tool_exec": lname in LOCAL_TOOL_EXECUTABLES,
                    }
                )

        total_read_bytes = 0
        total_write_bytes = 0
        total_other_bytes = 0
        for pid in target_pids:
            r, w, o = self.get_io_counters(pid)
            total_read_bytes += r
            total_write_bytes += w
            total_other_bytes += o

        active_tool_procs = [d["name"] for d in descendants if d["is_tool_exec"]]
        return {
            "target_pids": sorted(target_pids),
            "descendants": descendants,
            "active_tool_procs": active_tool_procs,
            "io_read_bytes": total_read_bytes,
            "io_write_bytes": total_write_bytes,
            "io_other_bytes": total_other_bytes,
            "self_ram_mb": self.get_self_ram_mb(),
            "proc_count": len(by_pid),
        }

    @staticmethod
    def get_io_counters(pid: int) -> tuple[int, int, int]:
        h = _kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if not h:
            return (0, 0, 0)
        try:
            ioc = IO_COUNTERS()
            if _kernel32.GetProcessIoCounters(h, ctypes.byref(ioc)):
                return (
                    int(ioc.ReadTransferCount),
                    int(ioc.WriteTransferCount),
                    int(ioc.OtherTransferCount),
                )
            return (0, 0, 0)
        finally:
            _kernel32.CloseHandle(h)

    @staticmethod
    def get_self_ram_mb() -> float:
        h = _kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, os.getpid())
        if not h:
            return 0.0
        try:
            pmc = PROCESS_MEMORY_COUNTERS()
            pmc.cb = ctypes.sizeof(PROCESS_MEMORY_COUNTERS)
            if _psapi.GetProcessMemoryInfo(h, ctypes.byref(pmc), pmc.cb):
                return round(pmc.WorkingSetSize / (1024 * 1024), 1)
            return 0.0
        finally:
            _kernel32.CloseHandle(h)
