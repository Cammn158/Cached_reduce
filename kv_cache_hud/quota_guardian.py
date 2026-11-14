from __future__ import annotations

import subprocess
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Set

from .config import HudSettings
from .step_fsm import PHASE_THINKING, PHASE_UPLOAD_TTFB
from .transcript_watcher import SessionLedger


def classify_cache_miss(
    prev_context: int,
    cache_read: int,
    gap_s: float,
    model_changed: bool,
    endpoint_switched: bool,
    ttl_seconds: float = 300.0,
    ratio_threshold: float = 0.50,
    min_prev_context: int = 10000,
) -> str:
    if prev_context < min_prev_context:
        return ""
    if cache_read >= prev_context * ratio_threshold:
        return ""
    if model_changed:
        return "MODEL_CHANGE"
    if gap_s >= ttl_seconds:
        return "TTL_TIMEOUT"
    if endpoint_switched:
        return "SOCKET_ENDPOINT_SWITCH"
    return "CONTEXT_TRUNCATION_OR_RESET"


def diagnose_network_health(phase: str, total_tpu_wait_s: float, net_snap: Dict[str, object]) -> Dict[str, str]:
    if phase not in (PHASE_UPLOAD_TTFB, PHASE_THINKING) or total_tpu_wait_s < 45.0:
        return {"status": "NORMAL", "detail": ""}

    estab = int(net_snap.get("established_count", 0))
    stalled = int(net_snap.get("stalled_socket_count", 0))
    retrans_delta = int(net_snap.get("retrans_delta", 0))
    rtt_ms = float(net_snap.get("smoothed_rtt_ms", 0.0))

    if estab == 0 or stalled > 0 or retrans_delta >= 5 or rtt_ms >= 1500.0:
        return {
            "status": "NETWORK_STALL_SUSPECTED",
            "detail": (
                f"Phát hiện dấu hiệu đứt/nghẽn TCP cục bộ (Estab={estab}, Stalled={stalled}, "
                f"Retrans+={retrans_delta}, RTT={rtt_ms:.0f}ms). Cân nhắc hủy và gửi lại trước khi hết TTL 5p."
            ),
        }
    return {
        "status": "HEALTHY_TPU_THINKING",
        "detail": (
            f"Đường truyền ổn định (Estab={estab}, RTT={rtt_ms:.0f}ms, không nghẽn socket) — "
            "Google TPU đang suy luận thật, giữ kết nối để tránh mất token đã tính."
        ),
    }


def send_windows_toast(title: str, body: str) -> bool:
    safe_title = title.replace("'", "''")
    safe_body = body.replace("'", "''")
    ps_script = (
        "[Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, ContentType = WindowsRuntime] > $null; "
        "$tpl = [Windows.UI.Notifications.ToastNotificationManager]::GetTemplateContent("
        "[Windows.UI.Notifications.ToastTemplateType]::ToastText02); "
        "$nodes = $tpl.GetElementsByTagName('text'); "
        f"$nodes.Item(0).AppendChild($tpl.CreateTextNode('{safe_title}')) > $null; "
        f"$nodes.Item(1).AppendChild($tpl.CreateTextNode('{safe_body}')) > $null; "
        "$toast = [Windows.UI.Notifications.ToastNotification]::new($tpl); "
        "[Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier('KV Cache HUD').Show($toast)"
    )
    try:
        subprocess.Popen(
            ["powershell.exe", "-NoProfile", "-WindowStyle", "Hidden", "-Command", ps_script],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        return True
    except OSError:
        return False


class QuotaGuardian:
    def __init__(self, enable_os_toast: bool = True) -> None:
        self.enable_os_toast = enable_os_toast
        self._alerted_milestones: Dict[str, Set[int]] = {}
        self._claimed_rescues: Set[str] = set()
        self._recent_alerts: List[Dict[str, Any]] = []

    def consume_cycle_rescue(self, session_id: str, prev_touch_ts: float) -> bool:
        cycle_key = f"{session_id}:{int(prev_touch_ts)}"
        if cycle_key in self._claimed_rescues:
            return False
        milestones = self._alerted_milestones.get(cycle_key)
        if not milestones or not (milestones & {90, 30}):
            return False
        if len(self._claimed_rescues) >= 50:
            self._claimed_rescues.clear()
        self._claimed_rescues.add(cycle_key)
        return True

    def evaluate(
        self,
        now: float,
        ledger: Optional[SessionLedger],
        fsm_out: Dict[str, object],
        net_snap: Dict[str, object],
        settings: HudSettings,
    ) -> Dict[str, Any]:
        last_step = ledger.steps[-1] if (ledger and ledger.steps) else None
        current_context = last_step.context_tokens if last_step else 0

        if settings.guard_mode == "on":
            guard_active = True
            guard_reason = "Bật thủ công (ON)"
        elif settings.guard_mode == "off":
            guard_active = False
            guard_reason = "Tắt thủ công (OFF)"
        else:
            guard_active = current_context >= settings.auto_guard_tokens
            guard_reason = (
                f"Tự kích hoạt (>= {settings.auto_guard_tokens // 1000}k token)"
                if guard_active
                else f"Chờ ngưỡng {settings.auto_guard_tokens // 1000}k token"
            )

        ttl = float(fsm_out.get("ttl_remaining_s", 0.0))
        phase = str(fsm_out.get("phase", ""))
        total_tpu_wait = float(fsm_out.get("total_tpu_wait_s", 0.0))
        net_diag = diagnose_network_health(phase, total_tpu_wait, net_snap)

        triggered_alert: Optional[Dict[str, Any]] = None
        if ledger and guard_active and current_context > 0 and ledger.last_touch_ts > 0:
            cycle_key = f"{ledger.session_id}:{int(ledger.last_touch_ts)}"
            if len(self._alerted_milestones) > 20:
                self._alerted_milestones.clear()
            fired = self._alerted_milestones.setdefault(cycle_key, set())

            milestone: Optional[int] = None
            level = ""
            title = ""
            msg = ""

            if ttl <= 0.0 and 0 not in fired:
                milestone = 0
                level = "CRITICAL"
                title = f"KV Cache {current_context // 1000}k đã hết hạn!"
                msg = (
                    f"TTL 5 phút đã về 00:00. Gửi tin nhắn mới sẽ nạp lại {current_context:,} Fresh token (Cây vàng). "
                    "Dùng Copy Handoff để mở phiên gọn nhẹ."
                )
            elif 0.0 < ttl <= 30.0 and 30 not in fired:
                milestone = 30
                level = "WARNING"
                title = f"Còn {int(ttl)}s trước khi rớt {current_context // 1000}k Cache!"
                msg = "Chuẩn bị gửi bước tiếp theo để làm mới bộ đếm 5 phút trên Google TPU."
            elif 30.0 < ttl <= 90.0 and 90 not in fired:
                milestone = 90
                level = "INFO"
                title = f"Cache đang nguội — còn {int(ttl)}s"
                msg = f"Đang giữ {current_context:,} token trên TPU HBM."

            if milestone is not None:
                fired.add(milestone)
                triggered_alert = {
                    "id": f"{cycle_key}:{milestone}",
                    "ts": now,
                    "milestone": milestone,
                    "level": level,
                    "title": title,
                    "message": msg,
                }
                self._recent_alerts.append(triggered_alert)
                if len(self._recent_alerts) > 20:
                    self._recent_alerts.pop(0)
                if self.enable_os_toast and settings.toast_enabled and milestone in (30, 0):
                    send_windows_toast(title, msg)

        return {
            "guard_active": guard_active,
            "guard_mode": settings.guard_mode,
            "auto_guard_tokens": settings.auto_guard_tokens,
            "guard_reason": guard_reason,
            "break_even_loss_tokens": current_context if ttl <= 0.0 else 0,
            "potential_loss_tokens": current_context,
            "network_diagnosis": net_diag,
            "triggered_alert": triggered_alert,
            "recent_alerts": list(self._recent_alerts[-5:]),
        }

    def trigger_test_alert(self, now: float, settings: HudSettings) -> Dict[str, Any]:
        alert = {
            "id": f"test:{int(now * 1000)}",
            "ts": now,
            "milestone": -1,
            "level": "TEST",
            "title": "Test Chuông & Thông Báo Thành Công",
            "message": "Cảnh báo âm thanh Web Audio và Windows Toast đang hoạt động bình thường.",
        }
        toast_sent = False
        if self.enable_os_toast and settings.toast_enabled:
            toast_sent = send_windows_toast(alert["title"], alert["message"])
        self._recent_alerts.append(alert)
        if len(self._recent_alerts) > 20:
            self._recent_alerts.pop(0)
        return {"alert": alert, "toast_dispatched": toast_sent}


@dataclass
class SavingsTracker:
    started_ts: float
    run_live_cache_tokens: int = 0
    run_guard_rescued_tokens: int = 0
    run_handoff_rescued_tokens: int = 0
    run_live_steps: int = 0
    _claimed_handoffs: Set[str] = field(default_factory=set)

    def record_live_updates(
        self,
        sessions: Dict[str, SessionLedger],
        updated_steps: Dict[str, list],
        guardian: QuotaGuardian,
        settings: HudSettings,
    ) -> bool:
        if not updated_steps:
            return False
        for sid, new_steps in updated_steps.items():
            s_ledger = sessions.get(sid)
            if not s_ledger:
                continue
            base_pos = len(s_ledger.steps) - len(new_steps)
            for offset, rec in enumerate(new_steps):
                pos = base_pos + offset
                prev_ts = s_ledger.steps[pos - 1].timestamp if pos > 0 else 0.0
                self.run_live_steps += 1
                settings.lifetime_live_steps += 1
                if rec.cache_read_tokens > 0:
                    self.run_live_cache_tokens += rec.cache_read_tokens
                    settings.lifetime_saved_tokens += rec.cache_read_tokens
                    if not rec.is_cache_miss and prev_ts > 0 and guardian.consume_cycle_rescue(sid, prev_ts):
                        self.run_guard_rescued_tokens += rec.cache_read_tokens
                        settings.lifetime_guard_rescued_tokens += rec.cache_read_tokens
        return True

    def record_handoff_cutloss(
        self,
        ledger: Optional[SessionLedger],
        handoff_md: str,
        now: float,
        ttl_seconds: float,
        settings: HudSettings,
    ) -> int:
        if ledger is None or not ledger.steps or ledger.last_touch_ts <= 0:
            return 0
        ttl_rem = max(0.0, ttl_seconds - max(0.0, now - ledger.last_touch_ts))
        if ttl_rem > 0.0:
            return 0
        # Deduplicate cut-loss savings by session cycle so repeated clicks never inflate total.
        hkey = f"{ledger.session_id}:{int(ledger.last_touch_ts)}"
        if hkey in self._claimed_handoffs:
            return 0
        est_handoff = max(100, len(handoff_md) // 4)
        saved = max(0, ledger.steps[-1].context_tokens - est_handoff)
        if saved <= 0:
            return 0
        if len(self._claimed_handoffs) >= 50:
            self._claimed_handoffs.clear()
        self._claimed_handoffs.add(hkey)
        self.run_handoff_rescued_tokens += saved
        settings.lifetime_handoff_rescued_tokens += saved
        settings.lifetime_saved_tokens += saved
        return saved

    def reset(self, now: float, settings: HudSettings) -> None:
        self.started_ts = now
        self.run_live_cache_tokens = 0
        self.run_guard_rescued_tokens = 0
        self.run_handoff_rescued_tokens = 0
        self.run_live_steps = 0
        self._claimed_handoffs.clear()
        settings.lifetime_saved_tokens = 0
        settings.lifetime_guard_rescued_tokens = 0
        settings.lifetime_handoff_rescued_tokens = 0
        settings.lifetime_live_steps = 0

    def to_dict(self, settings: HudSettings) -> Dict[str, Any]:
        return {
            "hud_started_ts": self.started_ts,
            "run_total_saved_tokens": self.run_live_cache_tokens + self.run_handoff_rescued_tokens,
            "run_live_cache_tokens": self.run_live_cache_tokens,
            "run_guard_rescued_tokens": self.run_guard_rescued_tokens,
            "run_handoff_rescued_tokens": self.run_handoff_rescued_tokens,
            "run_live_steps": self.run_live_steps,
            "lifetime_saved_tokens": settings.lifetime_saved_tokens,
            "lifetime_guard_rescued_tokens": settings.lifetime_guard_rescued_tokens,
            "lifetime_handoff_rescued_tokens": settings.lifetime_handoff_rescued_tokens,
            "lifetime_live_steps": settings.lifetime_live_steps,
        }
