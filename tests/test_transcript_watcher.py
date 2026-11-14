import json
import tempfile
import time
import unittest
from pathlib import Path

from kv_cache_hud.config import default_antigravity_dir
from kv_cache_hud.transcript_watcher import (
    TranscriptWatcher,
    parse_iso_timestamp,
    simplify_model_name,
)


def _write_jsonl(path: Path, records: list[dict], append: bool = False) -> None:
    mode = "a" if append else "w"
    with path.open(mode, encoding="utf-8") as f:
        for r in records:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")


class TestTranscriptWatcher(unittest.TestCase):
    def test_parse_iso_with_7_digit_fraction(self) -> None:
        ts = parse_iso_timestamp("2026-11-14 05:13:34.8470131+00:00")
        self.assertIsNotNone(ts)
        self.assertGreater(ts, 1700000000.0)
        self.assertEqual(simplify_model_name("Gemini 4 Carbon (Medium)"), "G4 Carbon (Medium)")

    def test_incremental_parsing_miss_and_bloat(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            agy = Path(tmp)
            sid = "conv-test-001"
            tpath = agy / "brain" / sid / ".system_generated" / "logs" / "transcript.jsonl"
            tpath.parent.mkdir(parents=True, exist_ok=True)

            _write_jsonl(
                tpath,
                [
                    {
                        "step_index": 0,
                        "type": "USER_INPUT",
                        "status": "DONE",
                        "created_at": "2026-11-14T04:00:00Z",
                        "content": (
                            "<USER_REQUEST>\nBuild HUD\n</USER_REQUEST>\n"
                            "<USER_SETTINGS_CHANGE>\n"
                            "The user changed setting `Model Selection` from None to Gemini 4 Hydrogen (Medium)."
                        ),
                    },
                    {
                        "step_index": 1,
                        "type": "PLANNER_RESPONSE",
                        "status": "DONE",
                        "created_at": "2026-11-14T04:00:10Z",
                        "input_tokens": 12000,
                        "cache_read_tokens": 0,
                        "output_tokens": 800,
                        "tool_calls": [{"name": "run_command", "args": {"CommandLine": "dir"}}],
                    },
                    {
                        "step_index": 2,
                        "type": "GENERIC",
                        "status": "DONE",
                        "created_at": "2026-11-14T04:00:15Z",
                        "content": "Created At: 2026-11-14T11:00:11+07:00\nCompleted At: 2026-11-14T11:00:15+07:00\nOK",
                    },
                    {
                        "step_index": 3,
                        "type": "PLANNER_RESPONSE",
                        "status": "DONE",
                        "created_at": "2026-11-14T04:00:30Z",
                        "input_tokens": 20000,
                        "cache_read_tokens": 12000,
                        "output_tokens": 1200,
                    },
                ],
            )

            watcher = TranscriptWatcher(agy, ttl_seconds=300.0, bloat_threshold=15000)
            updates = watcher.poll(time.time(), force_db=True)
            self.assertIn(sid, updates)
            self.assertEqual(len(updates[sid]), 2)

            ledger = watcher.sessions[sid]
            self.assertEqual(ledger.current_model, "G4 Hydrogen (Medium)")
            self.assertEqual(ledger.total_cache_read, 12000)
            self.assertEqual(ledger.total_fresh_input, 32000)
            self.assertEqual(ledger.bloat_count, 1)
            self.assertTrue(ledger.steps[1].is_bloat)
            self.assertFalse(ledger.steps[1].is_cache_miss)
            self.assertAlmostEqual(ledger.steps[1].tool_local_s, 4.0, places=2)

            # Append model switch + TTL timeout -> verify incremental tail & miss classification
            _write_jsonl(
                tpath,
                [
                    {
                        "step_index": 4,
                        "type": "USER_INPUT",
                        "status": "DONE",
                        "created_at": "2026-11-14T04:06:00Z",
                        "content": (
                            "<USER_REQUEST>\nContinue\n</USER_REQUEST>\n"
                            "<USER_SETTINGS_CHANGE>\n"
                            "The user changed setting `Model Selection` from Gemini 4 Hydrogen (Medium) to Gemini 4 Carbon (Medium)."
                        ),
                    },
                    {
                        "step_index": 5,
                        "type": "PLANNER_RESPONSE",
                        "status": "DONE",
                        "created_at": "2026-11-14T04:06:15Z",
                        "input_tokens": 33000,
                        "cache_read_tokens": 0,
                        "output_tokens": 500,
                    },
                ],
                append=True,
            )

            updates2 = watcher.poll(time.time())
            self.assertIn(sid, updates2)
            self.assertEqual(len(updates2[sid]), 1)
            self.assertEqual(ledger.cache_miss_count, 1)
            self.assertTrue(ledger.steps[2].is_cache_miss)
            self.assertEqual(ledger.steps[2].miss_reason, "MODEL_CHANGE")

    def test_read_real_antigravity_brain_directory(self) -> None:
        agy = default_antigravity_dir()
        if not (agy / "brain").exists():
            self.skipTest("Antigravity brain directory not present")
        watcher = TranscriptWatcher(agy)
        watcher.poll(time.time(), force_db=True)
        self.assertGreater(len(watcher.sessions), 0)
        # Verify current conversation ground truth if present
        cur = watcher.sessions.get("092ba3b4-3f73-4fea-acdf-fd0b55e7bc01")
        if cur:
            self.assertGreaterEqual(len(cur.steps), 4)
            step7 = next((s for s in cur.steps if s.step_index == 7), None)
            if step7:
                self.assertEqual(step7.input_tokens, 27538)
                self.assertEqual(step7.cache_read_tokens, 0)
                self.assertEqual(step7.output_tokens, 2589)


if __name__ == "__main__":
    unittest.main()
