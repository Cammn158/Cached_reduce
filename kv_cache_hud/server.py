from __future__ import annotations

import asyncio
import json
import time
import webbrowser
from collections import deque
from pathlib import Path
from typing import Any, Deque, Dict, List, Optional, Set

from aiohttp import WSMsgType, web

from .config import (
    PORT_SCAN_RANGE,
    HudSettings,
    default_antigravity_dir,
    project_root_dir,
)
from .handoff_builder import build_handoff_prompt
from .net_probe_win import NetProbeWin
from .net_probe_wsl import NetProbeWsl
from .network_tuner import (
    apply_keepalive_15s,
    read_windows_keepalive,
    read_wsl_keepalive,
    restore_keepalive_default,
)
from .process_watcher import ProcessWatcher
from .quota_guardian import QuotaGuardian, SavingsTracker
from .step_fsm import PHASE_IDLE, StepLifecycleFSM, build_latency_profile
from .transcript_watcher import SessionLedger, TranscriptWatcher


class HudEngine:
    def __init__(
        self,
        antigravity_dir: Optional[Path] = None,
        settings_path: Optional[Path] = None,
        enable_os_toast: bool = True,
    ) -> None:
        self.agy_dir = antigravity_dir or default_antigravity_dir()
        self.settings_path = settings_path or (project_root_dir() / "hud_settings.json")
        self.settings = HudSettings.load(self.settings_path)

        self.transcript = TranscriptWatcher(
            self.agy_dir,
            ttl_seconds=self.settings.ttl_seconds,
            bloat_threshold=self.settings.bloat_threshold_tokens,
            miss_ratio_threshold=self.settings.cache_miss_ratio_threshold,
            miss_min_prev_context=self.settings.cache_miss_min_prev_context,
        )
        self.proc_watcher = ProcessWatcher()
        self.net_win = NetProbeWin()
        self.net_wsl = NetProbeWsl(self.settings.wsl_distro)
        self.fsm = StepLifecycleFSM(
            ttl_seconds=self.settings.ttl_seconds,
            slow_tpu_seconds=self.settings.slow_tpu_seconds,
        )
        self.guardian = QuotaGuardian(enable_os_toast=enable_os_toast)
        self.savings = SavingsTracker(started_ts=time.time())
        self._initial_scan_done = False

        self.ws_clients: Set[web.WebSocketResponse] = set()
        self.net_history: Deque[Dict[str, Any]] = deque(maxlen=80)
        self._last_transcript_poll = 0.0
        self._last_wsl_poll = 0.0
        self._last_keepalive_poll = 0.0
        self._last_broadcast = 0.0
        self._cached_wsl: Dict[str, Any] = {"distros": [], "sockets": [], "distro_running": False}
        self._cached_keepalive: Dict[str, Any] = {
            "windows": read_windows_keepalive(),
            "wsl": {"running": False, "keepalive_s": None, "label": "Chưa kiểm tra"},
        }
        self._latest_state: Dict[str, Any] = {}
        self._running = False

    def _active_ledger(self) -> Optional[SessionLedger]:
        sid = self.transcript.pick_active_session_id(
            self.settings.auto_follow,
            self.settings.pinned_session_id,
        )
        return self.transcript.sessions.get(sid) if sid else None

    def refresh_keepalive_cache(self) -> Dict[str, Any]:
        win_ka = read_windows_keepalive()
        wsl_ka = read_wsl_keepalive(
            self.settings.wsl_distro,
            bool(self._cached_wsl.get("distro_running", False)),
        )
        self._cached_keepalive = {"windows": win_ka, "wsl": wsl_ka}
        return self._cached_keepalive

    def tick_once(self, force_all: bool = False) -> tuple[Dict[str, Any], bool]:
        now = time.time()
        proc_info = self.proc_watcher.inspect_targets()
        net_snap = self.net_win.poll(now, proc_info)

        updated_steps: Dict[str, list] = {}
        if force_all or (now - self._last_transcript_poll) >= 0.2:
            self._last_transcript_poll = now
            was_bootstrapped = self._initial_scan_done
            updated_steps = self.transcript.poll(now, force_db=force_all)
            self._initial_scan_done = True
            if was_bootstrapped and updated_steps:
                if self.savings.record_live_updates(
                    self.transcript.sessions, updated_steps, self.guardian, self.settings
                ):
                    self.settings.save(self.settings_path)

        if force_all or (now - self._last_wsl_poll) >= 2.0:
            self._last_wsl_poll = now
            self.net_wsl.distro = self.settings.wsl_distro
            self._cached_wsl = self.net_wsl.poll(now, active=(self.settings.environment == "wsl2"))

        if force_all or (now - self._last_keepalive_poll) >= 5.0:
            self._last_keepalive_poll = now
            self.refresh_keepalive_cache()

        ledger = self._active_ledger()
        new_steps_for_active = updated_steps.get(ledger.session_id, []) if ledger else []
        fsm_out = self.fsm.update(now, ledger, new_steps_for_active, net_snap, proc_info)
        guard_out = self.guardian.evaluate(now, ledger, fsm_out, net_snap, self.settings)

        self.net_history.append(
            {
                "ts": round(now, 1),
                "out_kbps": net_snap["out_kbps"],
                "in_kbps": net_snap["in_kbps"],
                "rtt_ms": net_snap["smoothed_rtt_ms"],
                "retrans": net_snap["retrans_delta"],
            }
        )

        sessions_list = [
            s.summary_dict(self.settings.session_aliases.get(s.session_id, ""))
            for s in sorted(self.transcript.sessions.values(), key=lambda x: x.file_mtime, reverse=True)
        ]

        steps_payload: List[Dict[str, Any]] = []
        active_summary: Optional[Dict[str, Any]] = None
        latency_profile: Dict[str, Any] = build_latency_profile([])
        if ledger:
            alias = self.settings.session_aliases.get(ledger.session_id, "")
            active_summary = ledger.summary_dict(alias)
            steps_payload = [s.to_dict() for s in ledger.steps[-500:]]
            latency_profile = build_latency_profile(ledger.steps)

        state = {
            "server_ts": now,
            "settings": {
                "auto_follow": self.settings.auto_follow,
                "pinned_session_id": self.settings.pinned_session_id,
                "environment": self.settings.environment,
                "wsl_distro": self.settings.wsl_distro,
                "sound_enabled": self.settings.sound_enabled,
                "toast_enabled": self.settings.toast_enabled,
                "guard_mode": self.settings.guard_mode,
                "auto_guard_tokens": self.settings.auto_guard_tokens,
                "bloat_threshold_tokens": self.settings.bloat_threshold_tokens,
                "slow_tpu_seconds": self.settings.slow_tpu_seconds,
            },
            "active_session": active_summary,
            "sessions": sessions_list,
            "steps": steps_payload,
            "latency_profile": latency_profile,
            "fsm": fsm_out,
            "guardian": guard_out,
            "savings": self.savings.to_dict(self.settings),
            "network": {
                **net_snap,
                "wsl": self._cached_wsl,
                "keepalive": self._cached_keepalive,
            },
            "process": proc_info,
            "net_history": list(self.net_history),
        }
        self._latest_state = state
        has_urgent = bool(updated_steps or fsm_out["phase"] != PHASE_IDLE or guard_out.get("triggered_alert"))
        return state, has_urgent

    async def loop(self) -> None:
        self._running = True
        self.tick_once(force_all=True)
        while self._running:
            t0 = time.monotonic()
            state, has_urgent = self.tick_once(force_all=False)
            now = time.time()
            interval = 0.1 if has_urgent else 0.5
            if self.ws_clients and (now - self._last_broadcast) >= interval:
                self._last_broadcast = now
                payload = json.dumps(state, ensure_ascii=False)
                dead: List[web.WebSocketResponse] = []
                for ws in list(self.ws_clients):
                    try:
                        await ws.send_str(payload)
                    except (ConnectionError, RuntimeError):
                        dead.append(ws)
                for ws in dead:
                    self.ws_clients.discard(ws)

            elapsed = time.monotonic() - t0
            await asyncio.sleep(max(0.02, 0.1 - elapsed))

    def stop(self) -> None:
        self._running = False


def build_app(engine: HudEngine) -> web.Application:
    app = web.Application()
    static_dir = project_root_dir() / "static"

    async def handle_index(_request: web.Request) -> web.FileResponse:
        return web.FileResponse(static_dir / "index.html")

    async def handle_state(_request: web.Request) -> web.Response:
        if not engine._latest_state:
            engine.tick_once(force_all=True)
        return web.json_response(engine._latest_state)

    async def handle_sessions(_request: web.Request) -> web.Response:
        if not engine._latest_state:
            engine.tick_once(force_all=True)
        sessions = engine._latest_state.get("sessions", [])
        miss_reasons: Dict[str, int] = {}
        for ledger in engine.transcript.sessions.values():
            for st in ledger.steps:
                if st.is_cache_miss:
                    r = st.miss_reason or "UNKNOWN"
                    miss_reasons[r] = miss_reasons.get(r, 0) + 1
        return web.json_response({"sessions": sessions, "miss_reasons_summary": miss_reasons})

    async def handle_select_session(request: web.Request) -> web.Response:
        data = await request.json()
        if "auto_follow" in data:
            engine.settings.auto_follow = bool(data["auto_follow"])
        if "session_id" in data and data["session_id"]:
            engine.settings.pinned_session_id = str(data["session_id"])
            if "auto_follow" not in data:
                engine.settings.auto_follow = False
        if "environment" in data and data["environment"] in ("windows", "wsl2"):
            engine.settings.environment = str(data["environment"])
        if "wsl_distro" in data and data["wsl_distro"]:
            engine.settings.wsl_distro = str(data["wsl_distro"])
        engine.settings.save(engine.settings_path)
        state, _ = engine.tick_once(force_all=True)
        return web.json_response({"ok": True, "active_session": state.get("active_session")})

    async def handle_rename_session(request: web.Request) -> web.Response:
        data = await request.json()
        sid = str(data.get("session_id", "")).strip()
        alias = str(data.get("alias", "")).strip()
        if not sid:
            return web.json_response({"ok": False, "error": "Thiếu session_id"}, status=400)
        if alias:
            engine.settings.session_aliases[sid] = alias
        else:
            engine.settings.session_aliases.pop(sid, None)
        engine.settings.save(engine.settings_path)
        engine.tick_once(force_all=False)
        return web.json_response({"ok": True, "session_id": sid, "alias": alias})

    async def handle_export_json(request: web.Request) -> web.Response:
        sid = request.query.get("session_id", "")
        ledger = engine.transcript.sessions.get(sid) if sid else engine._active_ledger()
        if ledger is None:
            return web.json_response({"error": "Không tìm thấy phiên"}, status=404)
        alias = engine.settings.session_aliases.get(ledger.session_id, "")
        payload = {
            "exported_at": time.time(),
            "session": ledger.summary_dict(alias),
            "latency_profile": build_latency_profile(ledger.steps),
            "steps": [s.to_dict() for s in ledger.steps],
        }
        return web.Response(
            text=json.dumps(payload, ensure_ascii=False, indent=2),
            content_type="application/json",
            headers={
                "Content-Disposition": f'attachment; filename="kv_cache_hud_{ledger.session_id[:12]}.json"'
            },
        )

    async def handle_handoff(request: web.Request) -> web.Response:
        sid = request.query.get("session_id", "")
        claim = request.query.get("claim", "") == "1"
        ledger = engine.transcript.sessions.get(sid) if sid else engine._active_ledger()
        alias = engine.settings.session_aliases.get(ledger.session_id, "") if ledger else ""
        md = build_handoff_prompt(ledger, alias=alias)
        claimed = 0
        if claim and ledger is not None:
            claimed = engine.savings.record_handoff_cutloss(
                ledger, md, time.time(), engine.settings.ttl_seconds, engine.settings
            )
            if claimed > 0:
                engine.settings.save(engine.settings_path)
                engine.tick_once(force_all=False)
        return web.json_response(
            {
                "ok": ledger is not None,
                "session_id": ledger.session_id if ledger else "",
                "markdown": md,
                "claimed_saved_tokens": claimed,
            }
        )

    async def handle_reset_savings(_request: web.Request) -> web.Response:
        engine.savings.reset(time.time(), engine.settings)
        engine.settings.save(engine.settings_path)
        state, _ = engine.tick_once(force_all=False)
        return web.json_response({"ok": True, "savings": state["savings"]})

    async def handle_settings(request: web.Request) -> web.Response:
        data = await request.json()
        if "sound_enabled" in data:
            engine.settings.sound_enabled = bool(data["sound_enabled"])
        if "toast_enabled" in data:
            engine.settings.toast_enabled = bool(data["toast_enabled"])
        if "guard_mode" in data and data["guard_mode"] in ("auto", "on", "off"):
            engine.settings.guard_mode = str(data["guard_mode"])
        if "auto_guard_tokens" in data:
            val = int(data["auto_guard_tokens"])
            engine.settings.auto_guard_tokens = max(1000, min(val, 2_000_000))
        engine.settings.save(engine.settings_path)
        state, _ = engine.tick_once(force_all=False)
        return web.json_response({"ok": True, "settings": state["settings"], "guardian": state["guardian"]})

    async def handle_test_alert(_request: web.Request) -> web.Response:
        res = engine.guardian.trigger_test_alert(time.time(), engine.settings)
        return web.json_response({"ok": True, **res})

    async def handle_network_status(_request: web.Request) -> web.Response:
        ka = engine.refresh_keepalive_cache()
        return web.json_response({"ok": True, "keepalive": ka, "network": engine._latest_state.get("network", {})})

    async def handle_keepalive_enable(_request: web.Request) -> web.Response:
        res = apply_keepalive_15s()
        engine.refresh_keepalive_cache()
        return web.json_response(res)

    async def handle_keepalive_restore(_request: web.Request) -> web.Response:
        res = restore_keepalive_default()
        engine.refresh_keepalive_cache()
        return web.json_response(res)

    async def handle_ws(request: web.Request) -> web.WebSocketResponse:
        ws = web.WebSocketResponse(heartbeat=20.0)
        await ws.prepare(request)
        engine.ws_clients.add(ws)
        try:
            if not engine._latest_state:
                engine.tick_once(force_all=True)
            await ws.send_str(json.dumps(engine._latest_state, ensure_ascii=False))
            async for msg in ws:
                if msg.type == WSMsgType.ERROR:
                    break
        finally:
            engine.ws_clients.discard(ws)
        return ws

    bg_task_key = web.AppKey("bg_task", asyncio.Task)

    async def on_startup(_app: web.Application) -> None:
        _app[bg_task_key] = asyncio.create_task(engine.loop())

    async def on_cleanup(_app: web.Application) -> None:
        engine.stop()
        task = _app.get(bg_task_key)
        if task:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
        for ws in list(engine.ws_clients):
            await ws.close()

    app.on_startup.append(on_startup)
    app.on_cleanup.append(on_cleanup)

    app.router.add_get("/", handle_index)
    app.router.add_get("/ws", handle_ws)
    app.router.add_get("/api/state", handle_state)
    app.router.add_get("/api/sessions", handle_sessions)
    app.router.add_post("/api/select_session", handle_select_session)
    app.router.add_post("/api/rename_session", handle_rename_session)
    app.router.add_get("/api/export_json", handle_export_json)
    app.router.add_get("/api/handoff", handle_handoff)
    app.router.add_post("/api/reset_savings", handle_reset_savings)
    app.router.add_post("/api/settings", handle_settings)
    app.router.add_post("/api/test_alert", handle_test_alert)
    app.router.add_get("/api/network_status", handle_network_status)
    app.router.add_post("/api/keepalive/enable", handle_keepalive_enable)
    app.router.add_post("/api/keepalive/restore", handle_keepalive_restore)
    app.router.add_static("/static", static_dir, show_index=False)

    return app


async def start_hud_server(
    engine: HudEngine,
    host: str = "127.0.0.1",
    preferred_port: int = 3380,
) -> tuple[web.AppRunner, int]:
    app = build_app(engine)
    runner = web.AppRunner(app, access_log=None)
    await runner.setup()

    ports = [preferred_port] + [p for p in PORT_SCAN_RANGE if p != preferred_port]
    last_err: Optional[OSError] = None
    for port in ports:
        try:
            site = web.TCPSite(runner, host, port)
            await site.start()
            return runner, port
        except OSError as exc:
            last_err = exc
    await runner.cleanup()
    raise OSError(f"Không thể bind cổng trong dải {ports}: {last_err}")


async def run_forever(host: str = "127.0.0.1", port: int = 3380, open_browser: bool = True) -> None:
    engine = HudEngine()
    runner, bound_port = await start_hud_server(engine, host=host, preferred_port=port)
    url = f"http://{host}:{bound_port}"
    print(f"[KV Cache HUD] Running at {url} (mode={engine.net_win.is_admin and 'ADMIN' or 'USER'})")
    if open_browser:
        webbrowser.open(url)
    try:
        while True:
            await asyncio.sleep(3600)
    except (KeyboardInterrupt, asyncio.CancelledError):
        pass
    finally:
        await runner.cleanup()
