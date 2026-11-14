import struct
import time
import unittest

from kv_cache_hud.config import is_google_ip
from kv_cache_hud.net_probe_win import (
    ESTATS_DATA_ROD_SIZE,
    ESTATS_PATH_ROD_SIZE,
    NetProbeWin,
    parse_estats_data_rod,
    parse_estats_path_rod,
)
from kv_cache_hud.net_probe_wsl import parse_ss_tin_output, parse_wsl_list_output
from kv_cache_hud.process_watcher import ProcessWatcher


class TestNetProbeWin(unittest.TestCase):
    def test_google_ip_cidr(self) -> None:
        self.assertTrue(is_google_ip("172.217.113.4"))
        self.assertTrue(is_google_ip("142.250.199.10"))
        self.assertFalse(is_google_ip("127.0.0.1"))
        self.assertFalse(is_google_ip("192.168.1.1"))
        self.assertFalse(is_google_ip("not-an-ip"))

    def test_estats_guard_against_disabled_collection(self) -> None:
        # Simulate uninitialized kernel stack garbage when EnableCollection == 0.
        garbage = b"\xff" * ESTATS_DATA_ROD_SIZE
        self.assertIsNone(parse_estats_data_rod(0, garbage))
        self.assertIsNone(parse_estats_path_rod(0, b"\xff" * ESTATS_PATH_ROD_SIZE))

        valid_data = bytearray(ESTATS_DATA_ROD_SIZE)
        struct.pack_into("<QQQ", valid_data, 0, 4096, 5, 16384)
        struct.pack_into("<Q", valid_data, 72, 4096)
        struct.pack_into("<Q", valid_data, 88, 16384)
        parsed_d = parse_estats_data_rod(1, bytes(valid_data))
        self.assertEqual(parsed_d["data_bytes_out"], 4096)
        self.assertEqual(parsed_d["data_bytes_in"], 16384)
        self.assertEqual(parsed_d["thru_bytes_acked"], 4096)
        self.assertEqual(parsed_d["thru_bytes_received"], 16384)

        valid_path = bytearray(ESTATS_PATH_ROD_SIZE)
        struct.pack_into("<I", valid_path, 0, 5)
        struct.pack_into("<II", valid_path, 20, 2, 1400)
        struct.pack_into("<III", valid_path, 60, 999, 999, 999)
        struct.pack_into("<III", valid_path, 104, 18, 22, 4)
        parsed_p = parse_estats_path_rod(1, bytes(valid_path))
        self.assertEqual(parsed_p["pkts_retrans"], 5)
        self.assertEqual(parsed_p["sample_rtt_ms"], 18)
        self.assertEqual(parsed_p["smoothed_rtt_ms"], 22)
        self.assertEqual(parsed_p["rtt_var_ms"], 4)

    def test_wsl_parsers(self) -> None:
        raw_utf16 = (
            "  NAME            STATE           VERSION\r\n"
            "* Ubuntu-22.04    Stopped         2\r\n"
            "  docker-desktop  Running         2\r\n"
        ).encode("utf-16-le")
        distros = parse_wsl_list_output(raw_utf16)
        self.assertEqual(len(distros), 2)
        self.assertEqual(distros[0].name, "Ubuntu-22.04")
        self.assertTrue(distros[0].is_default)

        ss_out = (
            "State      Recv-Q Send-Q Local Address:Port  Peer Address:Port\n"
            "ESTAB      0      0      172.25.10.2:41522   172.217.113.4:443\n"
            "\t cubic wscale:8,8 rto:228 rtt:24.5/3.1 ato:40 bytes_acked:9800 bytes_received:44100 retrans:0/1\n"
        )
        parsed_ss = parse_ss_tin_output(ss_out)
        self.assertEqual(len(parsed_ss), 1)
        self.assertAlmostEqual(parsed_ss[0]["rtt_ms"], 24.5)
        self.assertEqual(parsed_ss[0]["bytes_acked"], 9800)
        self.assertEqual(parsed_ss[0]["retrans"], 1)

    def test_live_process_and_socket_probe_no_crash(self) -> None:
        pw = ProcessWatcher()
        npw = NetProbeWin()
        pinfo = pw.inspect_targets()
        self.assertGreater(pinfo["proc_count"], 10)
        self.assertGreater(pinfo["self_ram_mb"], 1.0)
        net_snap = npw.poll(time.time(), pinfo)
        self.assertIn(net_snap["telemetry_mode"], ("FULL_KERNEL_ESTATS", "STANDARD_USER"))
        if pinfo["target_pids"]:
            self.assertGreaterEqual(net_snap["established_count"], 0)


if __name__ == "__main__":
    unittest.main()
