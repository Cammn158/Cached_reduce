import { playAlertTone } from "./audio_alert.js";
import { renderNetLiveCanvas, renderStepsChart } from "./charts.js";

const PHASE_LABELS = {
  IDLE_COOLING: "Đang nghỉ / Giữ ấm KV Cache",
  TOOL_LOCAL: "Pha 1: Đang chạy Tool Local",
  UPLOAD_WAIT_TTFB: "Pha 2: Đang gửi Payload & Chờ First-Byte ACK / TTFB",
  TPU_THINKING_STREAM: "Pha 3: Google TPU đang Thinking & Stream kết quả",
  DISPATCH_COMMIT: "Pha 4: Đã chốt bước & Cập nhật Quota",
};

const MISS_LABELS = {
  TTL_TIMEOUT: "Hết TTL 5p",
  MODEL_CHANGE: "Đổi Model",
  SOCKET_ENDPOINT_SWITCH: "Đổi IP / Rớt Socket",
  CONTEXT_TRUNCATION_OR_RESET: "Nén / Sửa Prefix",
  UNKNOWN_OR_ENDPOINT_SWITCH: "Rớt Cache",
};

let latestState = null;
let chartMode = "tokens"; // tokens | pct
let stepFilter = "all";
let lastAlertId = "";

const el = (id) => document.getElementById(id);

function escapeHtml(str) {
  return String(str ?? "")
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;");
}

function fmtTok(n) {
  if (n >= 1_000_000) return `${(n / 1_000_000).toFixed(2)}M`;
  if (n >= 1_000) return `${(n / 1_000).toFixed(1)}k`;
  return String(n ?? 0);
}

function fmtMMSS(sec) {
  const s = Math.max(0, Math.floor(sec || 0));
  const mm = String(Math.floor(s / 60)).padStart(2, "0");
  const ss = String(s % 60).padStart(2, "0");
  return `${mm}:${ss}`;
}

function fmtClock(ts) {
  if (!ts) return "--:--:--";
  return new Date(ts * 1000).toTimeString().slice(0, 8);
}

async function postJson(url, body = {}) {
  const res = await fetch(url, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  return res.json();
}

function renderPhaseBanner(state) {
  const fsm = state.fsm || {};
  const net = state.network || {};
  const guard = state.guardian || {};
  const diag = guard.network_diagnosis || {};
  const banner = el("phaseBanner");

  banner.classList.remove("alert-red", "alert-yellow");
  const phase = fsm.phase || "IDLE_COOLING";

  if (fsm.is_slow_tpu || diag.status === "NETWORK_STALL_SUSPECTED") {
    banner.classList.add("alert-red");
    el("phaseTitle").textContent =
      diag.status === "NETWORK_STALL_SUSPECTED"
        ? "Cảnh báo: Nghi đứt/nghẽn TCP cục bộ khi chờ Google TPU!"
        : `Google TPU đang suy luận lâu (>90s) — Đã chờ ${fsm.total_tpu_wait_s}s`;
    el("phaseSub").textContent = diag.detail || "Chưa ghi xong bước mới vào transcript.jsonl";
  } else if (guard.guard_active && fsm.ttl_remaining_s <= 0 && (state.active_session?.current_context || 0) > 0) {
    banner.classList.add("alert-red");
    el("phaseTitle").textContent = `Cache ${fmtTok(state.active_session.current_context)} đã hết hạn 5 phút!`;
    el("phaseSub").textContent = "Gửi thêm tin nhắn lúc này sẽ phải nạp lại toàn bộ Fresh Input (Cây vàng).";
  } else if (guard.guard_active && fsm.ttl_remaining_s > 0 && fsm.ttl_remaining_s <= 60) {
    banner.classList.add("alert-yellow");
    el("phaseTitle").textContent = `Cảnh báo TTL: Còn ${fmtMMSS(fsm.ttl_remaining_s)} trước khi rớt Cache!`;
    el("phaseSub").textContent = PHASE_LABELS[phase] || phase;
  } else {
    el("phaseTitle").textContent = PHASE_LABELS[phase] || phase;
    const toolsTxt = (fsm.active_tools || []).length ? `Tools: ${fsm.active_tools.join(", ")}` : "";
    const ackTxt = phase === "UPLOAD_WAIT_TTFB" ? (fsm.ack_confirmed ? "Đã nhận ACK gửi xong" : "Đang đẩy bytes...") : "";
    el("phaseSub").textContent = [toolsTxt, ackTxt].filter(Boolean).join(" | ");
  }

  el("phaseTimer").textContent = `${Number(fsm.phase_elapsed_s || 0).toFixed(1)}s`;
  el("phaseNetHint").textContent = net.active_remote_ip
    ? `Edge ${net.active_remote_ip}:443 | ${net.in_kbps || 0} KB/s in`
    : "Chờ socket :443";

  document.querySelectorAll(".pipe-step").forEach((node) => {
    const p = node.getAttribute("data-phase");
    const active = p === phase || (p === "DISPATCH_COMMIT" && fsm.dispatch_active);
    node.classList.toggle("active", active);
  });

  el("pipeToolTime").textContent = fsm.tool_elapsed_s > 0 ? `${fsm.tool_elapsed_s}s` : "--";
  el("pipeTtfbTime").textContent =
    fsm.ttfb_elapsed_s > 0 ? `${fsm.ttfb_elapsed_s}s${fsm.ack_confirmed ? " ACK" : ""}` : "--";
  el("pipeThinkTime").textContent = fsm.thinking_elapsed_s > 0 ? `${fsm.thinking_elapsed_s}s` : "--";
  const lastStep = (state.steps || []).slice(-1)[0];
  el("pipeOutInfo").textContent = lastStep ? `${fmtTok(lastStep.output_tokens)} tok` : "--";
}

function renderOverview(state) {
  const sav = state.savings || {};
  el("savingsSincePill").textContent = sav.hud_started_ts
    ? `Từ lúc mở: ${fmtClock(sav.hud_started_ts)}`
    : "Từ lúc mở: --:--:--";
  el("runSavedBig").textContent = `+${fmtTok(sav.run_total_saved_tokens || 0)}`;
  el("lifetimeSavedText").textContent = `${fmtTok(sav.lifetime_saved_tokens || 0)} (${sav.lifetime_live_steps || 0} bước)`;
  el("liveStepsSinceOpen").textContent = `${sav.run_live_steps || 0} bước`;
  el("savedLiveCache").textContent = fmtTok(sav.run_live_cache_tokens || 0);
  el("savedGuardRescue").textContent = fmtTok(sav.run_guard_rescued_tokens || 0);
  el("savedHandoffCut").textContent = fmtTok(sav.run_handoff_rescued_tokens || 0);

  const sess = state.active_session;
  const fsm = state.fsm || {};
  const lat = state.latency_profile || {};
  const steps = state.steps || [];

  if (!sess) {
    el("modelPill").textContent = "Không có phiên";
    return;
  }

  el("modelPill").textContent = sess.current_model || "--";
  el("cachePctBig").textContent = `${sess.cache_pct}%`;
  el("freshPctBig").textContent = `${sess.fresh_pct}%`;
  el("cacheRatioBar").style.width = `${sess.cache_pct}%`;
  el("llmStepsCount").textContent = String(sess.llm_steps);
  el("totalCacheTok").textContent = fmtTok(sess.total_cache_read);
  el("totalFreshTok").textContent = fmtTok(sess.total_fresh_input);
  el("totalOutTok").textContent = fmtTok(sess.total_output);

  const last = steps[steps.length - 1];
  el("lastStepRatio").textContent = last
    ? `${(last.cache_ratio * 100).toFixed(1)}% / ${(last.fresh_ratio * 100).toFixed(1)}%`
    : "-- / --";

  // TTL Card
  const ttl = Number(fsm.ttl_remaining_s || 0);
  el("ttlClock").textContent = fmtMMSS(ttl);
  const ttlPct = Math.min(100, Math.max(0, (ttl / 300.0) * 100));
  const ttlBar = el("ttlBar");
  ttlBar.style.width = `${ttlPct}%`;
  ttlBar.className = ttl <= 0 ? "progress-fill-red" : ttl <= 60 ? "progress-fill-red" : ttl <= 180 ? "progress-fill-yellow" : "progress-fill-green";

  const tempMap = {
    WARM: "Đang nóng",
    COOLING: "Đang nguội",
    CRITICAL: "Nguy kịch",
    EVAPORATED: "Đã rớt (00:00)",
  };
  el("ttlTempBadge").textContent = tempMap[fsm.ttl_temp] || "Đang nguội";
  el("curContextTok").textContent = fmtTok(sess.current_context);
  el("missCountText").textContent = `${sess.cache_miss_count} lần`;

  const lastMiss = [...steps].reverse().find((s) => s.is_cache_miss);
  el("lastMissReasonBadge").textContent = lastMiss
    ? `Lần gần nhất: ${MISS_LABELS[lastMiss.miss_reason] || lastMiss.miss_reason}`
    : "";

  // Latency Breakdown
  el("tokPerSecBadge").textContent = `${lat.last_tok_per_s || 0} tok/s`;
  el("latLast").textContent = `${lat.last_step_s || 0}s`;
  el("latP50").textContent = `${lat.p50_s || 0}s`;
  el("latP95").textContent = `${lat.p95_s || 0}s`;
  el("latAvgTtfb").textContent = `${lat.avg_ttfb_s || 0}s`;

  const bd = lat.last_breakdown || {};
  const bdTotal = Math.max(0.01, bd.total_s || 0.01);
  el("breakdownTotal").textContent = `${bd.total_s || 0}s`;
  el("segTtfb").style.width = `${((bd.ttfb_s || 0) / bdTotal) * 100}%`;
  el("segGen").style.width = `${((bd.thinking_gen_s || 0) / bdTotal) * 100}%`;
  el("segMon").style.width = `${((bd.monitor_s || 0) / bdTotal) * 100}%`;
  el("segTool").style.width = `${((bd.tool_local_s || 0) / bdTotal) * 100}%`;
  el("hitVsMissCompare").textContent = `Khi Cache HIT: TB ${lat.hit_avg_s || 0}s/bước (${lat.hit_count || 0}) | Khi Cache MISS: TB ${lat.miss_avg_s || 0}s/bước (${lat.miss_count || 0})`;

  // 40 Steps Chart
  const slice = steps.slice(-40);
  const sumCache = slice.reduce((a, b) => a + (b.cache_read_tokens || 0), 0);
  const sumFresh = slice.reduce((a, b) => a + (b.input_tokens || 0), 0);
  el("chartTotalPrompt").textContent = `Tổng ${slice.length} bước đang hiển thị (${fmtTok(sumCache + sumFresh)} prompt):`;
  el("chartCacheSum").textContent = fmtTok(sumCache);
  el("chartFreshSum").textContent = fmtTok(sumFresh);
  el("chartWindowSpan").textContent =
    slice.length >= 2
      ? `${fmtClock(slice[0].timestamp)} -> ${fmtClock(slice[slice.length - 1].timestamp)}`
      : "--";

  renderStepsChart(el("stepsChartSvg"), el("chartTooltip"), steps, chartMode, escapeHtml);

  if (last) {
    el("latestStepSummary").innerHTML =
      `Bước mới nhất <strong>#${last.step_index}</strong> (${fmtClock(last.timestamp)}) | ` +
      `<span class="text-green">Cache Read: ${(last.cache_ratio * 100).toFixed(1)}% (${fmtTok(last.cache_read_tokens)})</span> | ` +
      `<span class="text-red">Fresh Input: ${(last.fresh_ratio * 100).toFixed(1)}% (${fmtTok(last.input_tokens)})</span> | ` +
      `Output: ${fmtTok(last.output_tokens)} | Trễ: ${last.total_latency_s}s`;
  }
}

function renderNetworkTab(state) {
  const net = state.network || {};
  const ka = net.keepalive || {};
  const winKa = ka.windows || {};
  const wslKa = ka.wsl || {};
  const guard = state.guardian || {};
  const proc = state.process || {};

  el("kaStatusPill").textContent = winKa.is_optimized ? "Tối ưu 15s" : "Mặc định";
  el("kaWinLabel").textContent = winKa.label || "--";
  el("kaWslLabel").textContent = wslKa.label || "--";
  const diag = guard.network_diagnosis || {};
  el("netDiagText").textContent = diag.detail || "Kết nối ổn định";

  el("guardPanelPill").textContent = guard.guard_active ? "GUARD ACTIVE" : "STANDBY";
  el("netTelemetrySummary").textContent = `${net.telemetry_mode || "--"} | RTT: ${net.smoothed_rtt_ms || 0}ms | Retrans: ${net.retrans_total || 0}`;

  renderNetLiveCanvas(el("netLiveCanvas"), state.net_history || []);

  const isWsl = state.settings?.environment === "wsl2";
  const sockets = net.sockets || [];
  const wslSockets = net.wsl?.sockets || [];
  if (isWsl) {
    el("socketCountPill").textContent = `${wslSockets.length} socket (WSL2 :443)`;
    el("socketsTableWrap").innerHTML = wslSockets.length
      ? `<table><thead><tr><th>Local Port</th><th>Remote IP (WSL2)</th><th>RTT</th><th>Acked</th><th>Recv</th><th>Retrans</th></tr></thead><tbody>${wslSockets
          .map(
            (s) =>
              `<tr><td>${s.local_port}</td><td>${escapeHtml(s.remote_ip)}</td><td>${s.rtt_ms || "--"}ms</td><td>${fmtTok(s.bytes_acked)}</td><td>${fmtTok(s.bytes_received)}</td><td>${s.retrans || 0}</td></tr>`
          )
          .join("")}</tbody></table>`
      : `<div class="text-muted">Chưa có socket WSL2 :443 (${escapeHtml(wslKa.label || "WSL2 chưa chạy hoặc chưa kết nối")})</div>`;
  } else {
    el("socketCountPill").textContent = `${sockets.length} socket (:443)`;
    el("socketsTableWrap").innerHTML = sockets.length
      ? `<table><thead><tr><th>Local Port</th><th>Remote IP (Google Edge)</th><th>State</th><th>Out</th><th>In</th><th>RTT</th></tr></thead><tbody>${sockets
          .map(
            (s) =>
              `<tr><td>${s.local_port}</td><td>${escapeHtml(s.remote_ip)}${s.is_google ? " (GG)" : ""}</td><td>${escapeHtml(s.state)}</td><td>${fmtTok(s.data_bytes_out)}</td><td>${fmtTok(s.data_bytes_in)}</td><td>${s.smoothed_rtt_ms || "--"}ms</td></tr>`
          )
          .join("")}</tbody></table>`
      : `<div class="text-muted">Không có kết nối :443 nào đang mở từ language_server.exe</div>`;
  }

  const pids = proc.target_pids || [];
  el("procPidPill").textContent = `PID: ${pids.join(", ") || "--"}`;
  const desc = proc.descendants || [];
  el("procsListWrap").innerHTML = desc.length
    ? `<table><thead><tr><th>PID</th><th>PPID</th><th>Tiến trình</th><th>Loại</th></tr></thead><tbody>${desc
        .map(
          (p) =>
            `<tr><td>${p.pid}</td><td>${p.ppid}</td><td>${escapeHtml(p.name)}</td><td>${p.is_tool_exec ? '<span class="text-cyan">Tool Local</span>' : "Helper"}</td></tr>`
        )
        .join("")}</tbody></table>`
    : `<div class="text-muted">Không có tiến trình con nào đang chạy.</div>`;
}

function renderStepsTab(state) {
  const steps = [...(state.steps || [])].reverse();
  const filtered = steps.filter((s) => {
    if (stepFilter === "miss") return s.is_cache_miss;
    if (stepFilter === "bloat") return s.is_bloat;
    if (stepFilter === "slow") return s.total_latency_s >= 90.0;
    return true;
  });

  el("stepsTableWrap").innerHTML = filtered.length
    ? `<table><thead><tr><th>Step</th><th>Giờ</th><th>Model</th><th>Cache Read</th><th>Fresh Input</th><th>Output</th><th>Độ trễ (Tool/TTFB/Gen)</th><th>Tools &amp; Nhãn</th></tr></thead><tbody>${filtered
        .map((s) => {
          const tags = [];
          if (s.is_cache_miss) tags.push(`<span class="text-yellow">${escapeHtml(MISS_LABELS[s.miss_reason] || "MISS")}</span>`);
          if (s.is_bloat) tags.push(`<span style="color:#f97316">[BLOAT +${fmtTok(s.delta_context)}]</span>`);
          if (s.tool_calls && s.tool_calls.length) tags.push(escapeHtml(s.tool_calls.join(", ")));
          return `<tr class="${s.is_cache_miss ? "row-miss" : ""}"><td>#${s.step_index}</td><td>${fmtClock(s.timestamp)}</td><td>${escapeHtml(s.model)}</td><td class="text-green">${fmtTok(s.cache_read_tokens)}</td><td class="text-red">${fmtTok(s.input_tokens)}</td><td>${fmtTok(s.output_tokens)}</td><td>${s.total_latency_s}s (${s.tool_local_s}/${s.ttfb_s}/${s.thinking_gen_s})</td><td>${tags.join(" | ") || "--"}</td></tr>`;
        })
        .join("")}</tbody></table>`
    : `<div class="text-muted">Không có bước nào khớp bộ lọc.</div>`;
}

async function refreshMultiSessionTab() {
  try {
    const res = await fetch("/api/sessions");
    const data = await res.json();
    const reasons = data.miss_reasons_summary || {};
    el("missReasonSummary").innerHTML = Object.entries(reasons).length
      ? Object.entries(reasons)
          .map(([k, v]) => `<span class="pill">${escapeHtml(MISS_LABELS[k] || k)}: ${v} lần</span>`)
          .join("")
      : `<span class="pill">Chưa có sự kiện rớt cache nào</span>`;

    const sessions = data.sessions || [];
    el("multiSessionTableWrap").innerHTML = `<table><thead><tr><th>Phiên</th><th>Bước</th><th>Cache Read</th><th>Fresh Đốt</th><th>Rớt Cache</th><th>Context</th></tr></thead><tbody>${sessions
      .map(
        (s) =>
          `<tr class="row-clickable" data-sid="${escapeHtml(s.session_id)}"><td>${escapeHtml(s.title)}</td><td>${s.llm_steps}</td><td class="text-green">${fmtTok(s.total_cache_read)}</td><td class="text-red">${fmtTok(s.total_fresh_input)}</td><td class="text-yellow">${s.cache_miss_count}</td><td>${fmtTok(s.current_context)}</td></tr>`
      )
      .join("")}</tbody></table>`;

    el("multiSessionTableWrap").querySelectorAll("tr[data-sid]").forEach((row) => {
      row.addEventListener("click", async () => {
        const sid = row.getAttribute("data-sid");
        const h = await fetch(`/api/handoff?session_id=${encodeURIComponent(sid)}`).then((r) => r.json());
        el("handoffTextarea").value = h.markdown || "";
        el("handoffTextarea").dataset.sid = sid;
      });
    });

    if (!el("handoffTextarea").value) {
      const activeSid = el("sessionSelect").value || sessions[0]?.session_id || "";
      if (activeSid) {
        const h = await fetch(`/api/handoff?session_id=${encodeURIComponent(activeSid)}`).then((r) => r.json());
        el("handoffTextarea").value = h.markdown || "";
        el("handoffTextarea").dataset.sid = activeSid;
      }
    }
  } catch (_) {}
}

function renderAll(state) {
  latestState = state;
  const set = state.settings || {};
  const net = state.network || {};
  const guard = state.guardian || {};
  const proc = state.process || {};
  const sav = state.savings || {};

  el("modeBadge").textContent = net.telemetry_mode === "FULL_KERNEL_ESTATS" ? "FULL KERNEL ESTATS" : "STANDARD USER";
  el("ramBadge").textContent = `RAM: ${proc.self_ram_mb || 0} MB`;
  el("savedBadge").textContent = `Tiết kiệm: +${fmtTok(sav.run_total_saved_tokens || 0)} | Lũy kế: ${fmtTok(sav.lifetime_saved_tokens || 0)}`;
  el("btnToggleSound").textContent = set.sound_enabled ? "Chuông: Bật" : "Chuông: Tắt";
  el("chkSound").checked = Boolean(set.sound_enabled);

  const gBadge = el("guardBadge");
  gBadge.textContent = guard.guard_active ? `Guard: ACTIVE (>=${Math.round((set.auto_guard_tokens || 50000) / 1000)}k)` : "Guard: STANDBY";
  gBadge.className = `badge ${guard.guard_active ? "badge-guard-on" : "badge-standby"}`;

  el("btnEnvWin").classList.toggle("active", set.environment !== "wsl2");
  el("btnEnvWsl").classList.toggle("active", set.environment === "wsl2");
  el("wslDistroLabel").textContent = set.wsl_distro || "Ubuntu-22.04";
  el("btnAutoFollow").classList.toggle("active", Boolean(set.auto_follow));

  // Sync Session dropdown without breaking active selection
  const sel = el("sessionSelect");
  const sessions = state.sessions || [];
  const activeId = state.active_session?.session_id || "";
  const sessHash = sessions.map((s) => `${s.session_id}:${s.title}:${s.llm_steps}`).join("|");
  if (sel.options.length !== sessions.length || sel.dataset.hash !== sessHash) {
    sel.innerHTML = sessions
      .map((s) => `<option value="${escapeHtml(s.session_id)}">${escapeHtml(s.title)} (${s.llm_steps} steps)</option>`)
      .join("");
    sel.dataset.hash = sessHash;
  }
  if (activeId) sel.value = activeId;

  // Trigger audio alert once per new alert ID
  const alertObj = guard.triggered_alert;
  if (alertObj && alertObj.id !== lastAlertId) {
    lastAlertId = alertObj.id;
    if (set.sound_enabled) playAlertTone(alertObj.level);
  }

  renderPhaseBanner(state);
  renderOverview(state);
  renderNetworkTab(state);
  renderStepsTab(state);
}

function connectWs() {
  const proto = location.protocol === "https:" ? "wss" : "ws";
  const ws = new WebSocket(`${proto}://${location.host}/ws`);
  ws.onopen = () => {
    el("liveBadge").textContent = "LIVE";
    el("liveBadge").className = "badge badge-live";
  };
  ws.onmessage = (ev) => {
    try {
      const data = JSON.parse(ev.data);
      renderAll(data);
    } catch (_) {}
  };
  ws.onclose = () => {
    el("liveBadge").textContent = "RECONNECTING";
    el("liveBadge").className = "badge badge-offline";
    setTimeout(connectWs, 1500);
  };
}

function bindEvents() {
  document.querySelectorAll(".tab-btn").forEach((btn) => {
    btn.addEventListener("click", () => {
      const target = btn.getAttribute("data-tab");
      document.querySelectorAll(".tab-btn").forEach((b) => b.classList.toggle("active", b === btn));
      document.querySelectorAll(".tab-panel").forEach((p) => p.classList.toggle("active", p.id === `tab-${target}`));
      if (target === "multisession") refreshMultiSessionTab();
    });
  });

  el("btnChartMode").addEventListener("click", () => {
    chartMode = chartMode === "tokens" ? "pct" : "tokens";
    el("btnChartMode").textContent = chartMode === "tokens" ? "Chế độ: Token" : "Chế độ: Tỉ lệ %";
    if (latestState) renderOverview(latestState);
  });

  document.querySelectorAll("[data-filter]").forEach((btn) => {
    btn.addEventListener("click", () => {
      stepFilter = btn.getAttribute("data-filter");
      document.querySelectorAll("[data-filter]").forEach((b) => b.classList.toggle("active", b === btn));
      if (latestState) renderStepsTab(latestState);
    });
  });

  el("btnAutoFollow").addEventListener("click", () => {
    const next = !(latestState?.settings?.auto_follow);
    postJson("/api/select_session", { auto_follow: next });
  });

  el("sessionSelect").addEventListener("change", (ev) => {
    postJson("/api/select_session", { session_id: ev.target.value, auto_follow: false });
  });

  el("btnPinSession").addEventListener("click", () => {
    const sid = el("sessionSelect").value;
    if (sid) postJson("/api/select_session", { session_id: sid, auto_follow: false });
  });

  el("btnRenameSession").addEventListener("click", () => {
    const sid = el("sessionSelect").value;
    if (!sid) return;
    const alias = prompt("Nhập tên gợi nhớ cho phiên này (để trống để khôi phục mặc định):", latestState?.active_session?.alias || "");
    if (alias !== null) postJson("/api/rename_session", { session_id: sid, alias });
  });

  el("btnEnvWin").addEventListener("click", () => postJson("/api/select_session", { environment: "windows" }));
  el("btnEnvWsl").addEventListener("click", () => postJson("/api/select_session", { environment: "wsl2" }));

  el("btnToggleSound").addEventListener("click", () => {
    const next = !(latestState?.settings?.sound_enabled);
    postJson("/api/settings", { sound_enabled: next });
  });

  const doTestAlert = async () => {
    playAlertTone("TEST");
    await postJson("/api/test_alert");
  };
  el("btnTestAlert").addEventListener("click", doTestAlert);
  el("btnTestAlert2").addEventListener("click", doTestAlert);

  el("btnExportJson").addEventListener("click", () => {
    const sid = el("sessionSelect").value || "";
    window.open(`/api/export_json?session_id=${encodeURIComponent(sid)}`, "_blank");
  });

  const copyHandoff = async (textOverride) => {
    let md = textOverride;
    const claimSid = (textOverride ? el("handoffTextarea").dataset.sid : "") || el("sessionSelect").value || "";
    if (!md) {
      const res = await fetch(`/api/handoff?session_id=${encodeURIComponent(claimSid)}&claim=1`).then((r) => r.json());
      md = res.markdown || "";
      el("handoffTextarea").value = md;
      if (res.session_id) el("handoffTextarea").dataset.sid = res.session_id;
    } else if (claimSid) {
      fetch(`/api/handoff?session_id=${encodeURIComponent(claimSid)}&claim=1`).catch(() => {});
    }
    if (navigator.clipboard && md) await navigator.clipboard.writeText(md);
  };
  el("btnCopyHandoff").addEventListener("click", () => copyHandoff());
  el("btnCopyHandoffBox").addEventListener("click", () => copyHandoff(el("handoffTextarea").value));

  el("btnResetSavings").addEventListener("click", () => {
    if (confirm("Đặt lại toàn bộ bộ đếm token tiết kiệm về 0?")) {
      postJson("/api/reset_savings");
    }
  });

  el("btnSaveGuard").addEventListener("click", () => {
    postJson("/api/settings", {
      guard_mode: el("selGuardMode").value,
      auto_guard_tokens: Number(el("inpGuardTokens").value || 50000),
      sound_enabled: el("chkSound").checked,
      toast_enabled: el("chkToast").checked,
    });
  });

  const showKaResult = (res) => {
    const box = el("kaFeedback");
    box.classList.remove("hidden");
    const cmdHtml = res.powershell_cmd ? `<br><code>${escapeHtml(res.powershell_cmd)}</code>` : "";
    box.innerHTML = `${escapeHtml(res.message || "")}${cmdHtml}`;
    if (latestState && res.status) {
      latestState.network.keepalive.windows = res.status;
      renderNetworkTab(latestState);
    }
  };
  el("btnKaEnable").addEventListener("click", async () => showKaResult(await postJson("/api/keepalive/enable")));
  el("btnKaRestore").addEventListener("click", async () => showKaResult(await postJson("/api/keepalive/restore")));
  el("btnKaRefresh").addEventListener("click", async () => {
    const res = await fetch("/api/network_status").then((r) => r.json());
    if (latestState && res.keepalive) {
      latestState.network.keepalive = res.keepalive;
      renderNetworkTab(latestState);
    }
  });
}

bindEvents();
fetch("/api/state")
  .then((r) => r.json())
  .then((data) => {
    renderAll(data);
    el("selGuardMode").value = data.settings?.guard_mode || "auto";
    el("inpGuardTokens").value = data.settings?.auto_guard_tokens || 50000;
    el("chkSound").checked = Boolean(data.settings?.sound_enabled);
    el("chkToast").checked = Boolean(data.settings?.toast_enabled);
  })
  .catch(() => {});
connectWs();
