from __future__ import annotations

import ctypes
import socket
import struct
from ctypes import wintypes
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Set, Tuple

from .config import is_google_ip

AF_INET = 2
AF_INET6 = 23
TCP_TABLE_OWNER_PID_CONNECTIONS = 5

MIB_TCP_STATE_NAMES = {
    1: "CLOSED",
    2: "LISTEN",
    3: "SYN_SENT",
    4: "SYN_RCVD",
    5: "ESTABLISHED",
    6: "FIN_WAIT1",
    7: "FIN_WAIT2",
    8: "CLOSE_WAIT",
    9: "CLOSING",
    10: "LAST_ACK",
    11: "TIME_WAIT",
    12: "DELETE_TCB",
}

ESTATS_TYPE_DATA = 1
ESTATS_TYPE_PATH = 3
ESTATS_DATA_ROD_SIZE = 96
ESTATS_PATH_ROD_SIZE = 160
ERROR_ACCESS_DENIED = 5
ERROR_INSUFFICIENT_BUFFER = 122

_iphlp = ctypes.windll.iphlpapi
_shell32 = ctypes.windll.shell32


class MIB_TCPROW(ctypes.Structure):
    _fields_ = [
        ("dwState", wintypes.DWORD),
        ("dwLocalAddr", wintypes.DWORD),
        ("dwLocalPort", wintypes.DWORD),
        ("dwRemoteAddr", wintypes.DWORD),
        ("dwRemotePort", wintypes.DWORD),
    ]


def is_user_admin() -> bool:
    try:
        return bool(_shell32.IsUserAnAdmin())
    except OSError:
        return False


def parse_estats_data_rod(rw_enabled: int, rod_bytes: bytes) -> Optional[Dict[str, int]]:
    # Never inspect rod_bytes unless EnableCollection == 1; otherwise kernel leaves stack garbage.
    if rw_enabled != 1 or len(rod_bytes) < ESTATS_DATA_ROD_SIZE:
        return None
    data_bytes_out, _, data_bytes_in = struct.unpack_from("<QQQ", rod_bytes, 0)
    thru_acked = struct.unpack_from("<Q", rod_bytes, 72)[0]
    # Offset 80 holds 32-bit RcvNxt + 4B x64 padding; ThruBytesReceived sits at 88.
    thru_received = struct.unpack_from("<Q", rod_bytes, 88)[0]
    return {
        "data_bytes_out": int(data_bytes_out),
        "data_bytes_in": int(data_bytes_in),
        "thru_bytes_acked": int(thru_acked),
        "thru_bytes_received": int(thru_received),
    }


def parse_estats_path_rod(rw_enabled: int, rod_bytes: bytes) -> Optional[Dict[str, int]]:
    if rw_enabled != 1 or len(rod_bytes) < ESTATS_PATH_ROD_SIZE:
        return None
    fast_retran = struct.unpack_from("<I", rod_bytes, 0)[0]
    pkts_retrans, bytes_retrans = struct.unpack_from("<II", rod_bytes, 20)
    # Windows x64 TCP_ESTATS_PATH_ROD_v0 stores SampleRtt/SmoothedRtt/RttVar at byte 104.
    sample_rtt, smoothed_rtt, rtt_var = struct.unpack_from("<III", rod_bytes, 104)
    if smoothed_rtt > 600000:
        smoothed_rtt = 0
    if sample_rtt > 600000:
        sample_rtt = 0
    if rtt_var > 600000:
        rtt_var = 0
    return {
        "fast_retrans": int(fast_retran),
        "pkts_retrans": max(int(pkts_retrans), int(fast_retran)),
        "bytes_retrans": int(bytes_retrans),
        "sample_rtt_ms": int(sample_rtt),
        "smoothed_rtt_ms": int(smoothed_rtt),
        "rtt_var_ms": int(rtt_var),
    }


@dataclass
class SocketSnapshot:
    local_ip: str
    local_port: int
    remote_ip: str
    remote_port: int
    state_code: int
    state: str
    pid: int
    is_google: bool
    estats_active: bool = False
    data_bytes_out: int = 0
    data_bytes_in: int = 0
    thru_bytes_acked: int = 0
    smoothed_rtt_ms: int = 0
    pkts_retrans: int = 0
    delta_out: int = 0
    delta_in: int = 0
    delta_acked: int = 0

    @property
    def tuple_key(self) -> Tuple[str, int, str, int]:
        return (self.local_ip, self.local_port, self.remote_ip, self.remote_port)

    def to_dict(self) -> Dict[str, object]:
        return {
            "local_ip": self.local_ip,
            "local_port": self.local_port,
            "remote_ip": self.remote_ip,
            "remote_port": self.remote_port,
            "state": self.state,
            "pid": self.pid,
            "is_google": self.is_google,
            "estats_active": self.estats_active,
            "data_bytes_out": self.data_bytes_out,
            "data_bytes_in": self.data_bytes_in,
            "thru_bytes_acked": self.thru_bytes_acked,
            "smoothed_rtt_ms": self.smoothed_rtt_ms,
            "pkts_retrans": self.pkts_retrans,
            "delta_out": self.delta_out,
            "delta_in": self.delta_in,
            "delta_acked": self.delta_acked,
        }


class NetProbeWin:
    def __init__(self) -> None:
        self.is_admin = is_user_admin()
        self._tried_enable: Set[Tuple[int, int, int, int]] = set()
        self._enable_denied = False
        self._prev_socket_bytes: Dict[Tuple[str, int, str, int], Tuple[int, int, int, int]] = {}
        self._prev_io: Optional[Tuple[int, int]] = None
        self._known_google_ips: Set[str] = set()
        self._last_active_ip: str = ""
        self._last_sample_ts: float = 0.0

    def _query_tcp_v4(self, target_pids: Set[int]) -> List[Tuple[MIB_TCPROW, SocketSnapshot]]:
        sz = wintypes.DWORD(0)
        ret = _iphlp.GetExtendedTcpTable(None, ctypes.byref(sz), False, AF_INET, TCP_TABLE_OWNER_PID_CONNECTIONS, 0)
        if ret != ERROR_INSUFFICIENT_BUFFER or sz.value == 0:
            return []
        buf = ctypes.create_string_buffer(sz.value)
        ret = _iphlp.GetExtendedTcpTable(buf, ctypes.byref(sz), False, AF_INET, TCP_TABLE_OWNER_PID_CONNECTIONS, 0)
        if ret != 0:
            return []

        count = struct.unpack_from("<I", buf, 0)[0]
        rows: List[Tuple[MIB_TCPROW, SocketSnapshot]] = []
        offset = 4
        for idx in range(count):
            pos = offset + idx * 24
            if pos + 24 > len(buf):
                break
            st, la, lp, ra, rp, pid = struct.unpack_from("<6I", buf, pos)
            if target_pids and pid not in target_pids:
                continue
            rport = socket.ntohs(rp & 0xFFFF)
            lport = socket.ntohs(lp & 0xFFFF)
            if rport != 443:
                continue
            lip = socket.inet_ntoa(struct.pack("<I", la))
            rip = socket.inet_ntoa(struct.pack("<I", ra))
            snap = SocketSnapshot(
                local_ip=lip,
                local_port=lport,
                remote_ip=rip,
                remote_port=rport,
                state_code=st,
                state=MIB_TCP_STATE_NAMES.get(st, str(st)),
                pid=pid,
                is_google=is_google_ip(rip),
            )
            rows.append((MIB_TCPROW(st, la, lp, ra, rp), snap))
        return rows

    def _query_tcp_v6(self, target_pids: Set[int]) -> List[SocketSnapshot]:
        sz = wintypes.DWORD(0)
        ret = _iphlp.GetExtendedTcpTable(None, ctypes.byref(sz), False, AF_INET6, TCP_TABLE_OWNER_PID_CONNECTIONS, 0)
        if ret != ERROR_INSUFFICIENT_BUFFER or sz.value == 0:
            return []
        buf = ctypes.create_string_buffer(sz.value)
        ret = _iphlp.GetExtendedTcpTable(buf, ctypes.byref(sz), False, AF_INET6, TCP_TABLE_OWNER_PID_CONNECTIONS, 0)
        if ret != 0:
            return []

        count = struct.unpack_from("<I", buf, 0)[0]
        snaps: List[SocketSnapshot] = []
        offset = 4
        for idx in range(count):
            pos = offset + idx * 56
            if pos + 56 > len(buf):
                break
            l_raw, _lscope, lp, r_raw, _rscope, rp, st, pid = struct.unpack_from("<16sII16sIIII", buf, pos)
            if target_pids and pid not in target_pids:
                continue
            rport = socket.ntohs(rp & 0xFFFF)
            if rport != 443:
                continue
            lip = socket.inet_ntop(socket.AF_INET6, l_raw)
            rip = socket.inet_ntop(socket.AF_INET6, r_raw)
            snaps.append(
                SocketSnapshot(
                    local_ip=lip,
                    local_port=socket.ntohs(lp & 0xFFFF),
                    remote_ip=rip,
                    remote_port=rport,
                    state_code=st,
                    state=MIB_TCP_STATE_NAMES.get(st, str(st)),
                    pid=pid,
                    is_google=is_google_ip(rip),
                )
            )
        return snaps

    def _read_estats_v4(self, row: MIB_TCPROW, snap: SocketSnapshot) -> None:
        raw_key = (row.dwLocalAddr, row.dwLocalPort, row.dwRemoteAddr, row.dwRemotePort)
        if not self._enable_denied and raw_key not in self._tried_enable:
            self._tried_enable.add(raw_key)
            rw_on = ctypes.create_string_buffer(b"\x01", 1)
            r1 = _iphlp.SetPerTcpConnectionEStats(ctypes.byref(row), ESTATS_TYPE_DATA, rw_on, 0, 1, 0)
            r2 = _iphlp.SetPerTcpConnectionEStats(ctypes.byref(row), ESTATS_TYPE_PATH, rw_on, 0, 1, 0)
            if r1 == ERROR_ACCESS_DENIED or r2 == ERROR_ACCESS_DENIED:
                self._enable_denied = True

        rw_data = ctypes.create_string_buffer(1)
        rod_data = ctypes.create_string_buffer(ESTATS_DATA_ROD_SIZE)
        ret_d = _iphlp.GetPerTcpConnectionEStats(
            ctypes.byref(row), ESTATS_TYPE_DATA, rw_data, 0, 1, None, 0, 0, rod_data, 0, ESTATS_DATA_ROD_SIZE
        )
        parsed_d = parse_estats_data_rod(rw_data.raw[0], rod_data.raw) if ret_d == 0 else None

        rw_path = ctypes.create_string_buffer(1)
        rod_path = ctypes.create_string_buffer(ESTATS_PATH_ROD_SIZE)
        ret_p = _iphlp.GetPerTcpConnectionEStats(
            ctypes.byref(row), ESTATS_TYPE_PATH, rw_path, 0, 1, None, 0, 0, rod_path, 0, ESTATS_PATH_ROD_SIZE
        )
        parsed_p = parse_estats_path_rod(rw_path.raw[0], rod_path.raw) if ret_p == 0 else None

        if parsed_d:
            snap.estats_active = True
            snap.data_bytes_out = parsed_d["data_bytes_out"]
            snap.data_bytes_in = parsed_d["data_bytes_in"]
            snap.thru_bytes_acked = parsed_d["thru_bytes_acked"]
        if parsed_p:
            snap.smoothed_rtt_ms = parsed_p["smoothed_rtt_ms"]
            snap.pkts_retrans = parsed_p["pkts_retrans"]

    def poll(self, now: float, proc_info: Dict[str, object]) -> Dict[str, object]:
        pids = set(proc_info.get("target_pids", []))
        v4_rows = self._query_tcp_v4(pids) if pids else []
        v6_snaps = self._query_tcp_v6(pids) if pids else []

        sockets: List[SocketSnapshot] = []
        total_delta_out = 0
        total_delta_in = 0
        total_delta_acked = 0
        total_retrans_delta = 0
        any_estats = False
        active_ip = ""
        active_socket_delta = 0
        rtts: List[int] = []
        retrans_total = 0

        for row, snap in v4_rows:
            if snap.state_code == 5:
                self._read_estats_v4(row, snap)
            if snap.estats_active:
                any_estats = True
                prev = self._prev_socket_bytes.get(snap.tuple_key)
                if prev:
                    p_out, p_in, p_ret, p_ack = prev
                    snap.delta_out = max(0, snap.data_bytes_out - p_out)
                    snap.delta_in = max(0, snap.data_bytes_in - p_in)
                    snap.delta_acked = max(0, snap.thru_bytes_acked - p_ack)
                    total_retrans_delta += max(0, snap.pkts_retrans - p_ret)
                self._prev_socket_bytes[snap.tuple_key] = (
                    snap.data_bytes_out,
                    snap.data_bytes_in,
                    snap.pkts_retrans,
                    snap.thru_bytes_acked,
                )
                total_delta_out += snap.delta_out
                total_delta_in += snap.delta_in
                total_delta_acked += snap.delta_acked
                if snap.smoothed_rtt_ms > 0:
                    rtts.append(snap.smoothed_rtt_ms)
                retrans_total += snap.pkts_retrans
                traffic = snap.delta_out + snap.delta_in
                if traffic > active_socket_delta:
                    active_socket_delta = traffic
                    active_ip = snap.remote_ip
            sockets.append(snap)

        # EStats thru_bytes_acked starts accumulating only after enabling collection, so compare per-tick deltas.
        thru_acked_pending = total_delta_out > total_delta_acked

        sockets.extend(v6_snaps)

        established = [s for s in sockets if s.state_code == 5]
        stalled_states = [s for s in sockets if s.state in ("SYN_SENT", "CLOSE_WAIT")]
        current_ips = {s.remote_ip for s in established if s.is_google}
        if not active_ip and established:
            google_est = [s for s in established if s.is_google]
            active_ip = (google_est[0] if google_est else established[0]).remote_ip

        endpoint_switched = False
        if active_ip and self._last_active_ip and active_ip != self._last_active_ip:
            if self._last_active_ip not in current_ips:
                endpoint_switched = True
        if active_ip:
            self._last_active_ip = active_ip
        self._known_google_ips.update(current_ips)

        # Fallback byte delta from process I/O counters when TCP EStats cannot be enabled in User mode.
        io_read = int(proc_info.get("io_read_bytes", 0))
        io_write = int(proc_info.get("io_write_bytes", 0))
        approx_out = 0
        approx_in = 0
        if self._prev_io is not None and pids:
            prev_r, prev_w = self._prev_io
            approx_in = max(0, io_read - prev_r)
            approx_out = max(0, io_write - prev_w)
        if pids:
            self._prev_io = (io_read, io_write)

        dt = max(0.05, now - self._last_sample_ts) if self._last_sample_ts > 0 else 0.1
        self._last_sample_ts = now

        eff_out = total_delta_out if any_estats else approx_out
        eff_in = total_delta_in if any_estats else approx_in
        telemetry_mode = "FULL_KERNEL_ESTATS" if any_estats else "STANDARD_USER"

        return {
            "telemetry_mode": telemetry_mode,
            "is_admin": self.is_admin,
            "estats_enabled": any_estats,
            "target_pids": sorted(pids),
            "sockets": [s.to_dict() for s in sockets],
            "established_count": len(established),
            "stalled_socket_count": len(stalled_states),
            "active_remote_ip": active_ip,
            "google_ips": sorted(current_ips),
            "endpoint_switched": endpoint_switched,
            "delta_out_bytes": eff_out,
            "delta_in_bytes": eff_in,
            "delta_acked_bytes": total_delta_acked if any_estats else 0,
            "out_kbps": round((eff_out / 1024.0) / dt, 2),
            "in_kbps": round((eff_in / 1024.0) / dt, 2),
            "ack_pending": thru_acked_pending if any_estats else False,
            "smoothed_rtt_ms": round(sum(rtts) / len(rtts), 1) if rtts else 0.0,
            "retrans_total": retrans_total,
            "retrans_delta": total_retrans_delta,
        }
