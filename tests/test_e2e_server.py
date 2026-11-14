import asyncio
import json
import unittest
from pathlib import Path

import aiohttp

from kv_cache_hud.config import default_antigravity_dir
from kv_cache_hud.server import HudEngine, start_hud_server


class TestE2EServer(unittest.TestCase):
    def test_live_http_and_websocket_pipeline(self) -> None:
        agy = default_antigravity_dir()
        if not (agy / "brain").exists():
            self.skipTest("Antigravity brain directory not present")

        async def _run() -> None:
            engine = HudEngine(antigravity_dir=agy, settings_path=Path("tests/_tmp_e2e_settings.json"), enable_os_toast=False)
            runner, port = await start_hud_server(engine, host="127.0.0.1", preferred_port=3385)
            base = f"http://127.0.0.1:{port}"
            try:
                async with aiohttp.ClientSession() as client:
                    async with client.get(f"{base}/") as r:
                        self.assertEqual(r.status, 200)
                        html = await r.text()
                        self.assertIn("Cache &amp; Latency HUD", html)

                    async with client.get(f"{base}/api/state") as r:
                        self.assertEqual(r.status, 200)
                        state = await r.json()
                        self.assertIn("active_session", state)
                        self.assertIn("fsm", state)
                        self.assertIn("network", state)
                        self.assertIn("savings", state)
                        self.assertEqual(state["savings"]["run_live_steps"], 0)
                        self.assertLess(state["process"]["self_ram_mb"], 80.0)
                        act = state["active_session"]
                        self.assertIsNotNone(act)

                    async with client.post(
                        f"{base}/api/settings",
                        json={"guard_mode": "auto", "auto_guard_tokens": 50000, "sound_enabled": False},
                    ) as r:
                        self.assertEqual(r.status, 200)
                        sres = await r.json()
                        self.assertTrue(sres["ok"])
                        self.assertFalse(sres["settings"]["sound_enabled"])

                    async with client.post(
                        f"{base}/api/rename_session",
                        json={"session_id": act["session_id"], "alias": "Phiên Kiểm Thử E2E"},
                    ) as r:
                        self.assertEqual(r.status, 200)
                        rres = await r.json()
                        self.assertTrue(rres["ok"])

                    async with client.post(
                        f"{base}/api/select_session",
                        json={"session_id": act["session_id"], "auto_follow": True, "environment": "windows"},
                    ) as r:
                        self.assertEqual(r.status, 200)
                        selres = await r.json()
                        self.assertTrue(selres["ok"])

                    async with client.get(f"{base}/api/sessions") as r:
                        self.assertEqual(r.status, 200)
                        sess_data = await r.json()
                        self.assertGreater(len(sess_data["sessions"]), 0)

                    async with client.get(f"{base}/api/network_status") as r:
                        self.assertEqual(r.status, 200)
                        net_data = await r.json()
                        self.assertTrue(net_data["ok"])
                        self.assertIn("windows", net_data["keepalive"])

                    async with client.get(f"{base}/api/handoff?session_id={act['session_id']}&claim=1") as r:
                        self.assertEqual(r.status, 200)
                        hd = await r.json()
                        self.assertIn("Context Handoff", hd["markdown"])
                        self.assertIn("claimed_saved_tokens", hd)

                    async with client.post(f"{base}/api/reset_savings") as r:
                        self.assertEqual(r.status, 200)
                        rs = await r.json()
                        self.assertTrue(rs["ok"])
                        self.assertEqual(rs["savings"]["run_total_saved_tokens"], 0)
                        self.assertEqual(rs["savings"]["lifetime_saved_tokens"], 0)

                    async with client.get(f"{base}/api/export_json") as r:
                        self.assertEqual(r.status, 200)
                        exp = json.loads(await r.text())
                        self.assertIn("steps", exp)

                    async with client.post(f"{base}/api/test_alert") as r:
                        self.assertEqual(r.status, 200)
                        ta = await r.json()
                        self.assertTrue(ta["ok"])

                    async with client.ws_connect(f"ws://127.0.0.1:{port}/ws") as ws:
                        msgs = []
                        for _ in range(3):
                            msg = await asyncio.wait_for(ws.receive_str(), timeout=3.0)
                            msgs.append(json.loads(msg))
                        self.assertEqual(len(msgs), 3)
                        self.assertIn("fsm", msgs[-1])
            finally:
                await runner.cleanup()
                tmp_set = Path("tests/_tmp_e2e_settings.json")
                if tmp_set.exists():
                    tmp_set.unlink()

        asyncio.run(_run())


if __name__ == "__main__":
    unittest.main()
