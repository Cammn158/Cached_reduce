from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from .transcript_watcher import SessionLedger, StepRecord

PHASE_IDLE = "IDLE_COOLING"
PHASE_TOOL = "TOOL_LOCAL"
PHASE_UPLOAD_TTFB = "UPLOAD_WAIT_TTFB"
PHASE_THINKING = "TPU_THINKING_STREAM"
PHASE_DISPATCH = "DISPATCH_COMMIT"

CONTROL_FRAME_FILTER_BYTES = 320
UPLOAD_TRIGGER_BYTES = 1024


def _percentile(sorted_vals: List[float], pct: float) -> float:
    if not sorted_vals:
        return 0.0
    if len(sorted_vals) == 1:
        return sorted_vals[0]
    rank = (len(sorted_vals) - 1) * pct
    lo = int(rank)
    hi = min(lo + 1, len(sorted_vals) - 1)
    frac = rank - lo
    return sorted_vals[lo] + (sorted_vals[hi] - sorted_vals[lo]) * frac


def classify_ttl_temp(ttl_remaining: float) -> str:
    if ttl_remaining <= 0.0:
        return "EVAPORATED"
    if ttl_remaining <= 60.0:
        return "CRITICAL"
    if ttl_remaining <= 180.0:
        return "COOLING"
    return "WARM"


def build_latency_profile(steps: List[StepRecord]) -> Dict[str, Any]:
    if not steps:
        return {
            "last_step_s": 0.0,
            "p50_s": 0.0,
            "p95_s": 0.0,
            "avg_ttfb_s": 0.0,
            "last_tok_per_s": 0.0,
            "hit_avg_s": 0.0,
            "miss_avg_s": 0.0,
            "miss_count": 0,
            "hit_count": 0,
            "last_breakdown": {
                "ttfb_s": 0.0,
                "thinking_gen_s": 0.0,
                "monitor_s": 0.0,
                "tool_local_s": 0.0,
                "total_s": 0.0,
            },
        }

    totals = sorted(s.total_latency_s for s in steps)
    ttfbs = [s.ttfb_s for s in steps if s.ttfb_s > 0]
    hits = [s.total_latency_s for s in steps if not s.is_cache_miss]
    misses = [s.total_latency_s for s in steps if s.is_cache_miss]
    last = steps[-1]

    return {
        "last_step_s": round(last.total_latency_s, 1),
        "p50_s": round(_percentile(totals, 0.50), 1),
        "p95_s": round(_percentile(totals, 0.95), 1),
        "avg_ttfb_s": round(sum(ttfbs) / len(ttfbs), 1) if ttfbs else 0.0,
        "last_tok_per_s": round(last.tok_per_s, 1),
        "hit_avg_s": round(sum(hits) / len(hits), 1) if hits else 0.0,
        "miss_avg_s": round(sum(misses) / len(misses), 1) if misses else 0.0,
        "miss_count": len(misses),
        "hit_count": len(hits),
        "last_breakdown": {
            "ttfb_s": round(last.ttfb_s, 2),
            "thinking_gen_s": round(last.thinking_gen_s, 2),
            "monitor_s": round(last.monitor_s, 2),
            "tool_local_s": round(last.tool_local_s, 2),
            "total_s": round(last.total_latency_s, 2),
        },
    }


@dataclass
class ActivePhaseState:
    session_id: str = ""
    phase: str = PHASE_IDLE
    phase_started_ts: float = 0.0
    cycle_started_ts: float = 0.0
    tool_elapsed_s: float = 0.0
    ttfb_elapsed_s: float = 0.0
    thinking_elapsed_s: float = 0.0
    ack_confirmed: bool = False
    saw_upload_bytes: bool = False
    upload_bytes_accum: int = 0
    acked_bytes_accum: int = 0
    stream_bytes_accum: int = 0
    active_tools: List[str] = field(default_factory=list)
    endpoint_switched_in_cycle: bool = False
    dispatch_flash_until: float = 0.0
    last_endpoint_ip: str = ""


class StepLifecycleFSM:
    def __init__(self, ttl_seconds: float = 300.0, slow_tpu_seconds: float = 90.0) -> None:
        self.ttl_seconds = ttl_seconds
        self.slow_tpu_seconds = slow_tpu_seconds
        self.state = ActivePhaseState()

    def _reset_for_session(self, ledger: SessionLedger, now: float) -> None:
        self.state = ActivePhaseState(session_id=ledger.session_id)
        if ledger.pending_hint == "TOOL_LOCAL":
            self.state.phase = PHASE_TOOL
            self.state.phase_started_ts = ledger.pending_hint_ts or now
            self.state.cycle_started_ts = self.state.phase_started_ts
            self.state.active_tools = list(ledger.pending_tools)
        elif ledger.pending_hint == "WAITING_TPU" and (
            ledger.not_fully_idle or ledger.db_status == "CASCADE_RUN_STATUS_RUNNING"
        ):
            self.state.phase = PHASE_UPLOAD_TTFB
            self.state.phase_started_ts = ledger.pending_hint_ts or now
            self.state.cycle_started_ts = self.state.phase_started_ts
        else:
            self.state.phase = PHASE_IDLE
            self.state.phase_started_ts = now

    def update(
        self,
        now: float,
        ledger: Optional[SessionLedger],
        new_steps: List[StepRecord],
        net_snap: Dict[str, object],
        proc_info: Dict[str, object],
    ) -> Dict[str, object]:
        if ledger is None:
            return {
                "phase": PHASE_IDLE,
                "phase_elapsed_s": 0.0,
                "tool_elapsed_s": 0.0,
                "ttfb_elapsed_s": 0.0,
                "thinking_elapsed_s": 0.0,
                "total_tpu_wait_s": 0.0,
                "ack_confirmed": False,
                "is_slow_tpu": False,
                "ttl_remaining_s": 0.0,
                "ttl_temp": "EVAPORATED",
                "active_tools": [],
                "upload_bytes": 0,
                "stream_bytes": 0,
                "endpoint_ip": str(net_snap.get("active_remote_ip", "")),
                "dispatch_active": False,
            }

        if self.state.session_id != ledger.session_id:
            self._reset_for_session(ledger, now)

        st = self.state
        delta_out = int(net_snap.get("delta_out_bytes", 0))
        delta_in = int(net_snap.get("delta_in_bytes", 0))
        delta_acked = int(net_snap.get("delta_acked_bytes", 0))
        estats = bool(net_snap.get("estats_enabled", False))
        ack_pending = bool(net_snap.get("ack_pending", False))
        active_ip = str(net_snap.get("active_remote_ip", ""))
        if active_ip:
            st.last_endpoint_ip = active_ip
        if bool(net_snap.get("endpoint_switched", False)):
            st.endpoint_switched_in_cycle = True

        proc_tools = list(proc_info.get("active_tool_procs", []))
        is_running = ledger.not_fully_idle or ledger.db_status == "CASCADE_RUN_STATUS_RUNNING"

        # Commit newly flushed PLANNER_RESPONSE steps first.
        if new_steps:
            for idx, rec in enumerate(new_steps):
                is_last = idx == len(new_steps) - 1
                if is_last and st.phase in (PHASE_UPLOAD_TTFB, PHASE_THINKING):
                    if st.ttfb_elapsed_s > 0:
                        rec.ttfb_s = st.ttfb_elapsed_s
                    if st.thinking_elapsed_s > 0:
                        rec.thinking_gen_s = st.thinking_elapsed_s
                        rec.tok_per_s = (
                            rec.output_tokens / rec.thinking_gen_s
                            if rec.thinking_gen_s >= 0.2 and rec.output_tokens > 0
                            else 0.0
                        )
                    if st.tool_elapsed_s > 0:
                        rec.tool_local_s = st.tool_elapsed_s
                    rec.total_latency_s = max(
                        0.1, rec.tool_local_s + rec.ttfb_s + rec.thinking_gen_s + rec.monitor_s
                    )
                    rec.historical = False
                if active_ip:
                    rec.endpoint_ip = active_ip
                if not rec.historical and rec.is_cache_miss and rec.miss_reason == "UNKNOWN_OR_ENDPOINT_SWITCH":
                    rec.miss_reason = (
                        "SOCKET_ENDPOINT_SWITCH"
                        if st.endpoint_switched_in_cycle
                        else "CONTEXT_TRUNCATION_OR_RESET"
                    )

            st.tool_elapsed_s = 0.0
            st.ttfb_elapsed_s = 0.0
            st.thinking_elapsed_s = 0.0
            st.upload_bytes_accum = 0
            st.acked_bytes_accum = 0
            st.stream_bytes_accum = 0
            st.ack_confirmed = False
            st.saw_upload_bytes = False
            st.endpoint_switched_in_cycle = False
            st.dispatch_flash_until = now + 0.8

            if ledger.pending_hint == "TOOL_LOCAL":
                st.phase = PHASE_TOOL
                st.phase_started_ts = now
                st.cycle_started_ts = now
                st.active_tools = list(ledger.pending_tools) or proc_tools
            else:
                st.phase = PHASE_IDLE
                st.phase_started_ts = now
                st.active_tools = []
        else:
            # Advance live state machine on 100ms telemetry ticks.
            if st.phase == PHASE_IDLE:
                if ledger.pending_hint == "TOOL_LOCAL" or (is_running and proc_tools):
                    st.phase = PHASE_TOOL
                    st.phase_started_ts = now
                    st.cycle_started_ts = now
                    st.active_tools = list(ledger.pending_tools) or proc_tools
                elif ledger.pending_hint == "WAITING_TPU" and is_running:
                    st.phase = PHASE_UPLOAD_TTFB
                    st.phase_started_ts = ledger.pending_hint_ts or now
                    st.cycle_started_ts = st.phase_started_ts
                    st.saw_upload_bytes = delta_out > 0
                    st.upload_bytes_accum = delta_out
                    st.acked_bytes_accum = delta_acked
                elif is_running and delta_out >= UPLOAD_TRIGGER_BYTES:
                    st.phase = PHASE_UPLOAD_TTFB
                    st.phase_started_ts = now
                    st.cycle_started_ts = now
                    st.saw_upload_bytes = True
                    st.upload_bytes_accum = delta_out
                    st.acked_bytes_accum = delta_acked

            elif st.phase == PHASE_TOOL:
                st.tool_elapsed_s = max(0.0, now - st.phase_started_ts)
                if ledger.pending_tools or proc_tools:
                    st.active_tools = list(ledger.pending_tools) or proc_tools
                if ledger.pending_hint == "WAITING_TPU" or (
                    not proc_tools and delta_out >= UPLOAD_TRIGGER_BYTES and st.tool_elapsed_s >= 0.4
                ):
                    st.phase = PHASE_UPLOAD_TTFB
                    st.phase_started_ts = now
                    st.ttfb_elapsed_s = 0.0
                    st.upload_bytes_accum = delta_out
                    st.acked_bytes_accum = delta_acked
                    st.saw_upload_bytes = delta_out > 0
                    st.ack_confirmed = False

            elif st.phase == PHASE_UPLOAD_TTFB:
                st.ttfb_elapsed_s = max(0.0, now - st.phase_started_ts)
                st.upload_bytes_accum += delta_out
                st.acked_bytes_accum += delta_acked
                if delta_out > 0:
                    st.saw_upload_bytes = True

                if estats:
                    if (
                        st.saw_upload_bytes
                        and (
                            st.acked_bytes_accum >= max(1, int(st.upload_bytes_accum * 0.8))
                            or (not ack_pending and delta_out == 0)
                        )
                    ) or (ledger.pending_hint == "WAITING_TPU" and st.acked_bytes_accum > 0):
                        st.ack_confirmed = True
                elif st.ttfb_elapsed_s >= 0.25 and (st.saw_upload_bytes or ledger.pending_hint == "WAITING_TPU"):
                    st.ack_confirmed = True

                if delta_in > CONTROL_FRAME_FILTER_BYTES:
                    st.phase = PHASE_THINKING
                    st.phase_started_ts = now
                    st.thinking_elapsed_s = 0.0
                    st.stream_bytes_accum = delta_in
                    st.ack_confirmed = True
                elif not is_running and ledger.pending_hint == "IDLE" and st.ttfb_elapsed_s > 5.0:
                    st.phase = PHASE_IDLE
                    st.phase_started_ts = now

            elif st.phase == PHASE_THINKING:
                st.thinking_elapsed_s = max(0.0, now - st.phase_started_ts)
                st.stream_bytes_accum += delta_in
                if not is_running and ledger.pending_hint == "IDLE" and st.thinking_elapsed_s > 5.0:
                    st.phase = PHASE_IDLE
                    st.phase_started_ts = now

        last_touch = ledger.last_touch_ts
        if last_touch > 0:
            ttl_remaining = max(0.0, self.ttl_seconds - max(0.0, now - last_touch))
        else:
            ttl_remaining = 0.0

        total_tpu_wait = (
            (st.ttfb_elapsed_s + st.thinking_elapsed_s)
            if st.phase in (PHASE_UPLOAD_TTFB, PHASE_THINKING)
            else 0.0
        )
        phase_elapsed = (
            st.tool_elapsed_s
            if st.phase == PHASE_TOOL
            else st.ttfb_elapsed_s
            if st.phase == PHASE_UPLOAD_TTFB
            else st.thinking_elapsed_s
            if st.phase == PHASE_THINKING
            else 0.0
        )

        return {
            "phase": st.phase,
            "phase_elapsed_s": round(phase_elapsed, 1),
            "tool_elapsed_s": round(st.tool_elapsed_s, 1),
            "ttfb_elapsed_s": round(st.ttfb_elapsed_s, 1),
            "thinking_elapsed_s": round(st.thinking_elapsed_s, 1),
            "total_tpu_wait_s": round(total_tpu_wait, 1),
            "ack_confirmed": st.ack_confirmed,
            "is_slow_tpu": total_tpu_wait >= self.slow_tpu_seconds,
            "ttl_remaining_s": round(ttl_remaining, 1),
            "ttl_temp": classify_ttl_temp(ttl_remaining),
            "active_tools": st.active_tools,
            "upload_bytes": st.upload_bytes_accum,
            "stream_bytes": st.stream_bytes_accum,
            "endpoint_ip": st.last_endpoint_ip,
            "dispatch_active": now < st.dispatch_flash_until,
        }
