from __future__ import annotations

import json
import re
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

_RE_ISO_FRAC = re.compile(r"(\.\d{6})\d+")
_RE_MODEL_CHANGE = re.compile(r"`Model Selection` from .+?\s+to\s+([^\.\n]+)")
_RE_USER_REQ = re.compile(r"<USER_REQUEST>\s*(.*?)\s*</USER_REQUEST>", re.DOTALL)
_RE_CREATED_AT = re.compile(r"^Created At:\s*(.+)$", re.MULTILINE)
_RE_COMPLETED_AT = re.compile(r"^Completed At:\s*(.+)$", re.MULTILINE)


def parse_iso_timestamp(value: str) -> Optional[float]:
    if not value:
        return None
    s = value.strip()
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    s = _RE_ISO_FRAC.sub(r"\1", s)
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.timestamp()


def simplify_model_name(raw_name: str) -> str:
    name = raw_name.strip()
    if not name:
        return "Unknown"
    replacements = (
        ("Gemini 4 ", "G4 "),
        ("Gemini 3 ", "G3 "),
        ("Gemini 2.5 ", "G2.5 "),
    )
    for src, dst in replacements:
        if name.startswith(src):
            name = dst + name[len(src):]
            break
    return name


def _extract_user_preview(content: str) -> str:
    m = _RE_USER_REQ.search(content or "")
    text = (m.group(1) if m else (content or "")).strip()
    first_line = next((ln.strip() for ln in text.splitlines() if ln.strip()), "")
    if len(first_line) > 72:
        return first_line[:69] + "..."
    return first_line


def _extract_tool_names(tool_calls: Any) -> List[str]:
    if not isinstance(tool_calls, list):
        return []
    names: List[str] = []
    for item in tool_calls:
        if isinstance(item, dict) and item.get("name"):
            names.append(str(item["name"]))
    return names


@dataclass
class StepRecord:
    step_index: int
    llm_step_num: int
    created_at: str
    timestamp: float
    input_tokens: int
    cache_read_tokens: int
    output_tokens: int
    context_tokens: int
    cache_ratio: float
    fresh_ratio: float
    delta_context: int
    is_bloat: bool
    is_cache_miss: bool
    miss_reason: str
    model: str
    model_changed: bool
    gap_seconds: float
    tool_calls: List[str] = field(default_factory=list)
    tool_local_s: float = 0.0
    ttfb_s: float = 0.0
    thinking_gen_s: float = 0.0
    monitor_s: float = 0.0
    total_latency_s: float = 0.0
    tok_per_s: float = 0.0
    long_gap_before: bool = False
    historical: bool = True
    endpoint_ip: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "step_index": self.step_index,
            "llm_step_num": self.llm_step_num,
            "created_at": self.created_at,
            "timestamp": self.timestamp,
            "input_tokens": self.input_tokens,
            "cache_read_tokens": self.cache_read_tokens,
            "output_tokens": self.output_tokens,
            "context_tokens": self.context_tokens,
            "cache_ratio": round(self.cache_ratio, 4),
            "fresh_ratio": round(self.fresh_ratio, 4),
            "delta_context": self.delta_context,
            "is_bloat": self.is_bloat,
            "is_cache_miss": self.is_cache_miss,
            "miss_reason": self.miss_reason,
            "model": self.model,
            "model_changed": self.model_changed,
            "gap_seconds": round(self.gap_seconds, 2),
            "tool_calls": self.tool_calls,
            "tool_local_s": round(self.tool_local_s, 2),
            "ttfb_s": round(self.ttfb_s, 2),
            "thinking_gen_s": round(self.thinking_gen_s, 2),
            "monitor_s": round(self.monitor_s, 2),
            "total_latency_s": round(self.total_latency_s, 2),
            "tok_per_s": round(self.tok_per_s, 1),
            "long_gap_before": self.long_gap_before,
            "historical": self.historical,
            "endpoint_ip": self.endpoint_ip,
        }


@dataclass
class SessionLedger:
    session_id: str
    transcript_path: Path
    title: str = ""
    db_status: str = ""
    not_fully_idle: bool = False
    workspace: str = ""
    file_offset: int = 0
    file_mtime: float = 0.0
    current_model: str = "Unknown"
    pending_model_change: bool = False
    steps: List[StepRecord] = field(default_factory=list)
    total_cache_read: int = 0
    total_fresh_input: int = 0
    total_output: int = 0
    cache_miss_count: int = 0
    bloat_count: int = 0
    last_touch_ts: float = 0.0
    last_event_type: str = ""
    last_event_ts: float = 0.0
    pending_hint: str = "IDLE"
    pending_tools: List[str] = field(default_factory=list)
    pending_hint_ts: float = 0.0
    pending_tool_dur_s: float = 0.0
    last_user_prompt: str = ""
    first_user_prompt: str = ""
    recent_files: List[str] = field(default_factory=list)
    _prev_generic_done_ts: float = 0.0
    _prev_user_ts: float = 0.0
    _model_before_last: str = ""

    def summary_dict(self, alias: str = "") -> Dict[str, Any]:
        total_prompt = self.total_cache_read + self.total_fresh_input
        cache_pct = (self.total_cache_read / total_prompt * 100.0) if total_prompt > 0 else 0.0
        fresh_pct = (self.total_fresh_input / total_prompt * 100.0) if total_prompt > 0 else 0.0
        last_step = self.steps[-1] if self.steps else None
        return {
            "session_id": self.session_id,
            "title": alias or self.title or self.session_id[:12],
            "raw_title": self.title or self.session_id[:12],
            "alias": alias,
            "db_status": self.db_status,
            "not_fully_idle": self.not_fully_idle,
            "workspace": self.workspace,
            "file_mtime": self.file_mtime,
            "current_model": self.current_model,
            "llm_steps": len(self.steps),
            "total_cache_read": self.total_cache_read,
            "total_fresh_input": self.total_fresh_input,
            "total_output": self.total_output,
            "total_prompt": total_prompt,
            "cache_pct": round(cache_pct, 1),
            "fresh_pct": round(fresh_pct, 1),
            "cache_miss_count": self.cache_miss_count,
            "bloat_count": self.bloat_count,
            "current_context": last_step.context_tokens if last_step else 0,
            "last_touch_ts": self.last_touch_ts,
        }


class TranscriptWatcher:
    def __init__(
        self,
        antigravity_dir: Path,
        ttl_seconds: float = 300.0,
        bloat_threshold: int = 15000,
        miss_ratio_threshold: float = 0.50,
        miss_min_prev_context: int = 10000,
    ) -> None:
        self.antigravity_dir = antigravity_dir
        self.brain_dir = antigravity_dir / "brain"
        self.db_path = antigravity_dir / "conversation_summaries.db"
        self.ttl_seconds = ttl_seconds
        self.bloat_threshold = bloat_threshold
        self.miss_ratio_threshold = miss_ratio_threshold
        self.miss_min_prev_context = miss_min_prev_context
        self.sessions: Dict[str, SessionLedger] = {}
        self._last_db_scan = 0.0
        self._db_meta: Dict[str, Dict[str, Any]] = {}

    def _refresh_db_metadata(self, now: float, force: bool = False) -> None:
        if not force and (now - self._last_db_scan) < 1.0:
            return
        self._last_db_scan = now
        if not self.db_path.exists():
            return
        con: Optional[sqlite3.Connection] = None
        try:
            # Read-only URI avoids acquiring write locks on Antigravity's WAL database.
            con = sqlite3.connect(f"file:{self.db_path.as_posix()}?mode=ro", uri=True, timeout=0.5)
            rows = con.execute(
                "SELECT conversation_id, title, step_count, last_modified_time, "
                "workspace_uris, status, not_fully_idle "
                "FROM conversation_summaries"
            ).fetchall()
            meta: Dict[str, Dict[str, Any]] = {}
            for cid, title, step_count, mtime_str, ws, status, not_idle in rows:
                meta[str(cid)] = {
                    "title": str(title or ""),
                    "step_count": int(step_count or 0),
                    "mtime_ts": parse_iso_timestamp(str(mtime_str or "")) or 0.0,
                    "workspace": str(ws or ""),
                    "status": str(status or ""),
                    "not_fully_idle": bool(not_idle),
                }
            self._db_meta = meta
        except sqlite3.Error:
            pass
        finally:
            if con is not None:
                try:
                    con.close()
                except sqlite3.Error:
                    pass

    def _discover_sessions(self) -> None:
        if not self.brain_dir.exists():
            return
        for entry in self.brain_dir.iterdir():
            if not entry.is_dir():
                continue
            sid = entry.name
            if sid == "tempmediaStorage":
                continue
            tpath = entry / ".system_generated" / "logs" / "transcript.jsonl"
            if not tpath.exists():
                continue
            if sid not in self.sessions:
                self.sessions[sid] = SessionLedger(session_id=sid, transcript_path=tpath)

    def _consume_line(self, ledger: SessionLedger, raw_line: str) -> Optional[StepRecord]:
        raw_line = raw_line.strip()
        if not raw_line:
            return None
        try:
            obj = json.loads(raw_line)
        except json.JSONDecodeError:
            return None

        ev_type = str(obj.get("type", ""))
        status = str(obj.get("status", ""))
        created_at = str(obj.get("created_at", ""))
        ts = parse_iso_timestamp(created_at) or ledger.last_event_ts or ledger.file_mtime
        content = str(obj.get("content") or "")

        m_model = _RE_MODEL_CHANGE.search(content)
        if m_model:
            new_model = simplify_model_name(m_model.group(1))
            if ledger.current_model != "Unknown" and new_model != ledger.current_model:
                ledger.pending_model_change = True
            ledger.current_model = new_model

        ledger.last_event_type = ev_type
        ledger.last_event_ts = ts

        if ev_type == "USER_INPUT":
            preview = _extract_user_preview(content)
            if preview:
                if not ledger.first_user_prompt:
                    ledger.first_user_prompt = preview
                ledger.last_user_prompt = preview
                if not ledger.title:
                    ledger.title = preview
            ledger._prev_user_ts = ts
            ledger.pending_hint = "WAITING_TPU"
            ledger.pending_tools = []
            ledger.pending_hint_ts = ts
            return None

        if ev_type == "GENERIC":
            m_c = _RE_CREATED_AT.search(content)
            m_d = _RE_COMPLETED_AT.search(content)
            t_start = parse_iso_timestamp(m_c.group(1)) if m_c else None
            t_done = parse_iso_timestamp(m_d.group(1)) if m_d else None
            if t_start and t_done and t_done >= t_start:
                ledger.pending_tool_dur_s += (t_done - t_start)
            done_ts = t_done or ts
            ledger._prev_generic_done_ts = done_ts
            ledger.pending_hint = "WAITING_TPU"
            ledger.pending_tools = []
            ledger.pending_hint_ts = done_ts
            return None

        if ev_type == "PLANNER_RESPONSE" and status == "DONE":
            in_tok = int(obj.get("input_tokens") or 0)
            cache_tok = int(obj.get("cache_read_tokens") or 0)
            out_tok = int(obj.get("output_tokens") or 0)
            tools = _extract_tool_names(obj.get("tool_calls"))
            self._record_recent_files(ledger, obj.get("tool_calls"))

            if tools:
                ledger.pending_hint = "TOOL_LOCAL"
                ledger.pending_tools = tools
                ledger.pending_hint_ts = ts
            else:
                ledger.pending_hint = "IDLE"
                ledger.pending_tools = []
                ledger.pending_hint_ts = ts

            if in_tok == 0 and cache_tok == 0 and out_tok == 0:
                return None

            prev = ledger.steps[-1] if ledger.steps else None
            context_tok = cache_tok + in_tok
            prev_context = prev.context_tokens if prev else 0
            delta_ctx = context_tok - prev_context if prev else context_tok
            gap_s = max(0.0, ts - prev.timestamp) if prev else 0.0

            is_miss = bool(
                prev
                and prev_context >= self.miss_min_prev_context
                and cache_tok < (prev_context * self.miss_ratio_threshold)
            )
            is_bloat = bool(prev and delta_ctx >= self.bloat_threshold)

            miss_reason = ""
            if is_miss:
                if ledger.pending_model_change:
                    miss_reason = "MODEL_CHANGE"
                elif gap_s >= self.ttl_seconds:
                    miss_reason = "TTL_TIMEOUT"
                else:
                    miss_reason = "UNKNOWN_OR_ENDPOINT_SWITCH"
                ledger.cache_miss_count += 1
            if is_bloat:
                ledger.bloat_count += 1

            tool_s = ledger.pending_tool_dur_s
            ledger.pending_tool_dur_s = 0.0

            wait_start = max(ledger._prev_generic_done_ts, ledger._prev_user_ts)
            if prev and wait_start < prev.timestamp:
                wait_start = prev.timestamp
            wall_s = max(0.2, ts - wait_start) if wait_start > 0 else 1.0
            if wall_s > 600.0:
                wall_s = 1.0
            ttfb_est = min(wall_s * 0.28, 25.0)
            gen_est = max(0.1, wall_s - ttfb_est)
            tok_s = out_tok / gen_est if (out_tok > 0 and gen_est > 0) else 0.0

            cache_ratio = (cache_tok / context_tok) if context_tok > 0 else 0.0
            fresh_ratio = (in_tok / context_tok) if context_tok > 0 else 0.0

            rec = StepRecord(
                step_index=int(obj.get("step_index") or 0),
                llm_step_num=len(ledger.steps) + 1,
                created_at=created_at,
                timestamp=ts,
                input_tokens=in_tok,
                cache_read_tokens=cache_tok,
                output_tokens=out_tok,
                context_tokens=context_tok,
                cache_ratio=cache_ratio,
                fresh_ratio=fresh_ratio,
                delta_context=delta_ctx,
                is_bloat=is_bloat,
                is_cache_miss=is_miss,
                miss_reason=miss_reason,
                model=ledger.current_model,
                model_changed=ledger.pending_model_change,
                gap_seconds=gap_s,
                tool_calls=tools,
                tool_local_s=tool_s,
                ttfb_s=ttfb_est,
                thinking_gen_s=gen_est,
                monitor_s=0.0,
                total_latency_s=wall_s + tool_s,
                tok_per_s=tok_s,
                long_gap_before=gap_s >= self.ttl_seconds,
                historical=True,
            )

            ledger.pending_model_change = False
            ledger.steps.append(rec)
            ledger.total_cache_read += cache_tok
            ledger.total_fresh_input += in_tok
            ledger.total_output += out_tok
            ledger.last_touch_ts = ts
            return rec

        return None

    @staticmethod
    def _record_recent_files(ledger: SessionLedger, tool_calls: Any) -> None:
        if not isinstance(tool_calls, list):
            return
        for item in tool_calls:
            if not isinstance(item, dict):
                continue
            args = item.get("args")
            if not isinstance(args, dict):
                continue
            for key in ("Path", "AbsolutePath", "TargetFile", "file_path"):
                val = args.get(key)
                if isinstance(val, str) and val:
                    cleaned = val.strip('"')
                    if cleaned in ledger.recent_files:
                        ledger.recent_files.remove(cleaned)
                    ledger.recent_files.append(cleaned)
                    if len(ledger.recent_files) > 12:
                        ledger.recent_files.pop(0)

    def _sync_session(self, ledger: SessionLedger) -> List[StepRecord]:
        new_steps: List[StepRecord] = []
        meta = self._db_meta.get(ledger.session_id)
        if meta:
            if meta["title"]:
                ledger.title = meta["title"]
            ledger.db_status = meta["status"]
            ledger.not_fully_idle = meta["not_fully_idle"]
            ledger.workspace = meta["workspace"]

        try:
            st = ledger.transcript_path.stat()
        except OSError:
            return new_steps

        if st.st_size < ledger.file_offset:
            # File truncated or rewritten; rebuild from start.
            sid = ledger.session_id
            tpath = ledger.transcript_path
            ledger = SessionLedger(session_id=sid, transcript_path=tpath)
            if meta:
                if meta["title"]:
                    ledger.title = meta["title"]
                ledger.db_status = meta["status"]
                ledger.not_fully_idle = meta["not_fully_idle"]
                ledger.workspace = meta["workspace"]
            self.sessions[sid] = ledger

        ledger.file_mtime = st.st_mtime
        if st.st_size == ledger.file_offset:
            return new_steps

        try:
            with ledger.transcript_path.open("rb") as f:
                f.seek(ledger.file_offset)
                chunk = f.read()
                if not chunk:
                    return new_steps
                last_nl = chunk.rfind(b"\n")
                if last_nl == -1:
                    return new_steps
                complete_bytes = chunk[: last_nl + 1]
                ledger.file_offset += len(complete_bytes)
        except OSError:
            return new_steps

        for line in complete_bytes.decode("utf-8", errors="replace").splitlines():
            rec = self._consume_line(ledger, line)
            if rec is not None:
                new_steps.append(rec)

        return new_steps

    def poll(self, now: float, force_db: bool = False) -> Dict[str, List[StepRecord]]:
        self._refresh_db_metadata(now, force=force_db)
        self._discover_sessions()
        updated: Dict[str, List[StepRecord]] = {}
        for sid in list(self.sessions.keys()):
            ledger = self.sessions[sid]
            new_steps = self._sync_session(ledger)
            if new_steps:
                updated[sid] = new_steps
        return updated

    def pick_active_session_id(self, auto_follow: bool, pinned_id: str) -> str:
        if not self.sessions:
            return ""
        if not auto_follow and pinned_id in self.sessions:
            return pinned_id

        running = [
            s for s in self.sessions.values()
            if s.db_status == "CASCADE_RUN_STATUS_RUNNING" or s.not_fully_idle
        ]
        if running:
            running.sort(key=lambda s: s.file_mtime, reverse=True)
            return running[0].session_id

        ordered = sorted(self.sessions.values(), key=lambda s: s.file_mtime, reverse=True)
        return ordered[0].session_id
