import time
import unittest
from pathlib import Path

from kv_cache_hud.config import HudSettings
from kv_cache_hud.handoff_builder import build_handoff_prompt
from kv_cache_hud.network_tuner import read_windows_keepalive
from kv_cache_hud.quota_guardian import (
    QuotaGuardian,
    SavingsTracker,
    classify_cache_miss,
    diagnose_network_health,
)
from kv_cache_hud.step_fsm import PHASE_THINKING
from kv_cache_hud.transcript_watcher import SessionLedger, StepRecord


class TestQuotaGuardian(unittest.TestCase):
    def test_classify_4_cache_miss_causes(self) -> None:
        self.assertEqual(classify_cache_miss(50000, 48000, 10.0, False, False), "")
        self.assertEqual(classify_cache_miss(50000, 0, 10.0, True, False), "MODEL_CHANGE")
        self.assertEqual(classify_cache_miss(50000, 0, 305.0, False, False), "TTL_TIMEOUT")
        self.assertEqual(classify_cache_miss(50000, 0, 40.0, False, True), "SOCKET_ENDPOINT_SWITCH")
        self.assertEqual(classify_cache_miss(50000, 0, 40.0, False, False), "CONTEXT_TRUNCATION_OR_RESET")

    def test_network_diagnosis_blackhole_vs_healthy(self) -> None:
        stall = diagnose_network_health(
            PHASE_THINKING, 65.0, {"established_count": 1, "stalled_socket_count": 0, "retrans_delta": 6, "smoothed_rtt_ms": 40}
        )
        self.assertEqual(stall["status"], "NETWORK_STALL_SUSPECTED")

        healthy = diagnose_network_health(
            PHASE_THINKING, 120.0, {"established_count": 3, "stalled_socket_count": 0, "retrans_delta": 0, "smoothed_rtt_ms": 25}
        )
        self.assertEqual(healthy["status"], "HEALTHY_TPU_THINKING")

    def test_auto_guard_threshold_and_milestone_dedup(self) -> None:
        guardian = QuotaGuardian(enable_os_toast=False)
        settings = HudSettings(guard_mode="auto", auto_guard_tokens=50000)
        ledger = SessionLedger(
            session_id="s-guard",
            transcript_path=Path("dummy.jsonl"),
            last_touch_ts=1000.0,
            current_model="G4 Carbon",
            first_user_prompt="Fix KV cache pipeline",
            recent_files=["kv_cache_hud/step_fsm.py"],
        )
        small_step = StepRecord(
            step_index=1, llm_step_num=1, created_at="", timestamp=1000.0,
            input_tokens=20000, cache_read_tokens=0, output_tokens=500,
            context_tokens=20000, cache_ratio=0.0, fresh_ratio=1.0,
            delta_context=20000, is_bloat=False, is_cache_miss=False,
            miss_reason="", model="G4 Carbon", model_changed=False, gap_seconds=0.0,
        )
        ledger.steps.append(small_step)

        res_standby = guardian.evaluate(1280.0, ledger, {"ttl_remaining_s": 20.0}, {}, settings)
        self.assertFalse(res_standby["guard_active"])
        self.assertIsNone(res_standby["triggered_alert"])

        # Cross 50k threshold -> auto guard activates and fires 30s milestone once
        big_step = StepRecord(
            step_index=2, llm_step_num=2, created_at="", timestamp=1000.0,
            input_tokens=40000, cache_read_tokens=20000, output_tokens=800,
            context_tokens=60000, cache_ratio=0.33, fresh_ratio=0.67,
            delta_context=40000, is_bloat=True, is_cache_miss=False,
            miss_reason="", model="G4 Carbon", model_changed=False, gap_seconds=10.0,
        )
        ledger.steps.append(big_step)

        res_warn1 = guardian.evaluate(1275.0, ledger, {"ttl_remaining_s": 25.0}, {}, settings)
        self.assertTrue(res_warn1["guard_active"])
        self.assertIsNotNone(res_warn1["triggered_alert"])
        self.assertEqual(res_warn1["triggered_alert"]["milestone"], 30)

        # Same milestone in same cycle must NOT fire again
        res_warn2 = guardian.evaluate(1276.0, ledger, {"ttl_remaining_s": 24.0}, {}, settings)
        self.assertIsNone(res_warn2["triggered_alert"])

        # Crossing 0s fires CRITICAL milestone
        res_crit = guardian.evaluate(1301.0, ledger, {"ttl_remaining_s": 0.0}, {}, settings)
        self.assertIsNotNone(res_crit["triggered_alert"])
        self.assertEqual(res_crit["triggered_alert"]["milestone"], 0)
        self.assertEqual(res_crit["break_even_loss_tokens"], 60000)

        handoff = build_handoff_prompt(ledger)
        self.assertIn("Fix KV cache pipeline", handoff)
        self.assertIn("kv_cache_hud/step_fsm.py", handoff)

    def test_read_windows_keepalive_no_error(self) -> None:
        status = read_windows_keepalive()
        self.assertIn("effective_keepalive_ms", status)
        self.assertGreater(status["effective_keepalive_ms"], 0)

    def test_savings_tracker_live_rescue_and_handoff_no_double_count(self) -> None:
        guardian = QuotaGuardian(enable_os_toast=False)
        settings = HudSettings(guard_mode="auto", auto_guard_tokens=50000)
        tracker = SavingsTracker(started_ts=1000.0)
        ledger = SessionLedger(
            session_id="s-save",
            transcript_path=Path("dummy.jsonl"),
            last_touch_ts=1000.0,
            current_model="G4 Carbon",
        )
        s1 = StepRecord(
            step_index=1, llm_step_num=1, created_at="", timestamp=1000.0,
            input_tokens=50000, cache_read_tokens=0, output_tokens=400,
            context_tokens=50000, cache_ratio=0.0, fresh_ratio=1.0,
            delta_context=50000, is_bloat=False, is_cache_miss=False,
            miss_reason="", model="G4 Carbon", model_changed=False, gap_seconds=0.0,
        )
        ledger.steps.append(s1)

        # Trigger 30s TTL warning on cycle 1000.0
        guard_res = guardian.evaluate(1275.0, ledger, {"ttl_remaining_s": 25.0}, {}, settings)
        self.assertIsNotNone(guard_res["triggered_alert"])

        # Step 2 hits cache after warning -> counted once in live cache and attributed to guard rescue
        s2 = StepRecord(
            step_index=2, llm_step_num=2, created_at="", timestamp=1285.0,
            input_tokens=2000, cache_read_tokens=48000, output_tokens=300,
            context_tokens=50000, cache_ratio=0.96, fresh_ratio=0.04,
            delta_context=0, is_bloat=False, is_cache_miss=False,
            miss_reason="", model="G4 Carbon", model_changed=False, gap_seconds=285.0,
        )
        ledger.steps.append(s2)
        ledger.last_touch_ts = 1285.0
        tracker.record_live_updates({"s-save": ledger}, {"s-save": [s2]}, guardian, settings)
        snap = tracker.to_dict(settings)
        self.assertEqual(snap["run_live_cache_tokens"], 48000)
        self.assertEqual(snap["run_guard_rescued_tokens"], 48000)
        self.assertEqual(snap["run_total_saved_tokens"], 48000)
        self.assertEqual(snap["lifetime_saved_tokens"], 48000)

        # Expired session + copy handoff claims cut-loss once without duplicating on repeat click
        handoff_md = "x" * 1600
        claimed1 = tracker.record_handoff_cutloss(ledger, handoff_md, 1600.0, 300.0, settings)
        claimed2 = tracker.record_handoff_cutloss(ledger, handoff_md, 1605.0, 300.0, settings)
        self.assertEqual(claimed1, 50000 - 400)
        self.assertEqual(claimed2, 0)
        self.assertEqual(tracker.to_dict(settings)["run_total_saved_tokens"], 48000 + 49600)

        tracker.reset(1610.0, settings)
        snap_reset = tracker.to_dict(settings)
        self.assertEqual(snap_reset["run_total_saved_tokens"], 0)
        self.assertEqual(snap_reset["lifetime_saved_tokens"], 0)


if __name__ == "__main__":
    unittest.main()
