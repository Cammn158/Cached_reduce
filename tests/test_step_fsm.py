import unittest
from pathlib import Path

from kv_cache_hud.step_fsm import (
    PHASE_IDLE,
    PHASE_THINKING,
    PHASE_TOOL,
    PHASE_UPLOAD_TTFB,
    StepLifecycleFSM,
    build_latency_profile,
)
from kv_cache_hud.transcript_watcher import SessionLedger, StepRecord


class TestStepFSM(unittest.TestCase):
    def test_full_5_phase_lifecycle(self) -> None:
        fsm = StepLifecycleFSM(ttl_seconds=300.0, slow_tpu_seconds=90.0)
        ledger = SessionLedger(
            session_id="s1",
            transcript_path=Path("dummy.jsonl"),
            not_fully_idle=True,
            db_status="CASCADE_RUN_STATUS_RUNNING",
            last_touch_ts=1000.0,
            pending_hint="TOOL_LOCAL",
            pending_tools=["run_command"],
            pending_hint_ts=1000.0,
        )

        # Phase 1: Tool Local
        out1 = fsm.update(
            1002.0,
            ledger,
            [],
            {
                "delta_out_bytes": 0,
                "delta_in_bytes": 0,
                "delta_acked_bytes": 0,
                "estats_enabled": True,
                "ack_pending": False,
                "active_remote_ip": "172.217.113.4",
            },
            {"active_tool_procs": ["powershell.exe"]},
        )
        self.assertEqual(out1["phase"], PHASE_TOOL)
        self.assertEqual(out1["active_tools"], ["run_command"])

        # Transition to Phase 2: Upload & First-Byte ACK
        ledger.pending_hint = "WAITING_TPU"
        ledger.pending_tools = []
        ledger.pending_hint_ts = 1004.0
        out2 = fsm.update(
            1004.1,
            ledger,
            [],
            {
                "delta_out_bytes": 4096,
                "delta_in_bytes": 0,
                "delta_acked_bytes": 0,
                "estats_enabled": True,
                "ack_pending": True,
                "active_remote_ip": "172.217.113.4",
            },
            {"active_tool_procs": []},
        )
        self.assertEqual(out2["phase"], PHASE_UPLOAD_TTFB)
        self.assertFalse(out2["ack_confirmed"])

        # Small HTTP/2 control frame (64 bytes) with kernel ACK must NOT prematurely trigger Thinking
        out2_ping = fsm.update(
            1005.0,
            ledger,
            [],
            {
                "delta_out_bytes": 0,
                "delta_in_bytes": 64,
                "delta_acked_bytes": 4096,
                "estats_enabled": True,
                "ack_pending": False,
                "active_remote_ip": "172.217.113.4",
            },
            {"active_tool_procs": []},
        )
        self.assertEqual(out2_ping["phase"], PHASE_UPLOAD_TTFB)
        self.assertTrue(out2_ping["ack_confirmed"])

        # Phase 3: Thinking Stream when payload > 320 bytes arrives, and verify >90s alert
        out3 = fsm.update(
            1012.0,
            ledger,
            [],
            {
                "delta_out_bytes": 0,
                "delta_in_bytes": 2048,
                "delta_acked_bytes": 0,
                "estats_enabled": True,
                "ack_pending": False,
                "active_remote_ip": "172.217.113.4",
            },
            {"active_tool_procs": []},
        )
        self.assertEqual(out3["phase"], PHASE_THINKING)

        out3_slow = fsm.update(
            1105.0,
            ledger,
            [],
            {
                "delta_out_bytes": 0,
                "delta_in_bytes": 1024,
                "delta_acked_bytes": 0,
                "estats_enabled": True,
                "ack_pending": False,
                "active_remote_ip": "172.217.113.4",
            },
            {"active_tool_procs": []},
        )
        self.assertTrue(out3_slow["is_slow_tpu"])

        # Phase 4: Commit step -> verify CONTEXT_TRUNCATION_OR_RESET on live miss without IP switch
        rec = StepRecord(
            step_index=2,
            llm_step_num=1,
            created_at="2026-11-14T05:00:00Z",
            timestamp=1106.0,
            input_tokens=20000,
            cache_read_tokens=1500,
            output_tokens=930,
            context_tokens=21500,
            cache_ratio=0.07,
            fresh_ratio=0.93,
            delta_context=1500,
            is_bloat=False,
            is_cache_miss=True,
            miss_reason="UNKNOWN_OR_ENDPOINT_SWITCH",
            model="G4 Carbon",
            model_changed=False,
            gap_seconds=106.0,
        )
        ledger.steps.append(rec)
        ledger.last_touch_ts = 1106.0
        ledger.pending_hint = "IDLE"
        ledger.not_fully_idle = False
        ledger.db_status = "CASCADE_RUN_STATUS_IDLE"

        out4 = fsm.update(
            1106.1,
            ledger,
            [rec],
            {
                "delta_out_bytes": 0,
                "delta_in_bytes": 0,
                "delta_acked_bytes": 0,
                "estats_enabled": True,
                "ack_pending": False,
                "active_remote_ip": "172.217.113.4",
            },
            {"active_tool_procs": []},
        )
        self.assertEqual(out4["phase"], PHASE_IDLE)
        self.assertTrue(out4["dispatch_active"])
        self.assertEqual(rec.miss_reason, "CONTEXT_TRUNCATION_OR_RESET")
        self.assertGreater(rec.tok_per_s, 5.0)
        self.assertAlmostEqual(out4["ttl_remaining_s"], 299.9, places=1)

        prof = build_latency_profile([rec])
        self.assertGreater(prof["p50_s"], 90.0)


if __name__ == "__main__":
    unittest.main()
