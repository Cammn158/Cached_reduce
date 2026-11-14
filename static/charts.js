function fmtTok(n) {
  if (n >= 1_000_000) return `${(n / 1_000_000).toFixed(2)}M`;
  if (n >= 1_000) return `${(n / 1_000).toFixed(1)}k`;
  return String(n);
}

function fmtClock(ts) {
  if (!ts) return "";
  const d = new Date(ts * 1000);
  return d.toTimeString().slice(0, 8);
}

export function renderStepsChart(svg, tooltip, steps, mode, onFormatHtml) {
  const slice = steps.slice(-40);
  const W = 1000;
  const H = 210;
  const padL = 8;
  const padR = 8;
  const padT = 16;
  const padB = 34;
  const plotW = W - padL - padR;
  const plotH = H - padT - padB;

  while (svg.firstChild) svg.removeChild(svg.firstChild);

  if (!slice.length) {
    const t = document.createElementNS("http://www.w3.org/2000/svg", "text");
    t.setAttribute("x", String(W / 2));
    t.setAttribute("y", String(H / 2));
    t.setAttribute("text-anchor", "middle");
    t.setAttribute("fill", "#8ea0b8");
    t.setAttribute("font-size", "13");
    t.textContent = "Chưa có dữ liệu bước LLM trong phiên này.";
    svg.appendChild(t);
    return;
  }

  const maxTok = Math.max(1, ...slice.map((s) => s.context_tokens || 1));
  const slotW = plotW / slice.length;
  const barW = Math.max(5, Math.min(22, slotW * 0.76));
  const ns = "http://www.w3.org/2000/svg";

  // Grid baseline
  const base = document.createElementNS(ns, "line");
  base.setAttribute("x1", String(padL));
  base.setAttribute("x2", String(W - padR));
  base.setAttribute("y1", String(padT + plotH));
  base.setAttribute("y2", String(padT + plotH));
  base.setAttribute("stroke", "#253245");
  svg.appendChild(base);

  slice.forEach((st, i) => {
    const cx = padL + i * slotW + slotW / 2;
    const x = cx - barW / 2;
    const ctx = st.context_tokens || 0;
    const totalH = mode === "pct" ? (ctx > 0 ? plotH : 4) : Math.max(4, (ctx / maxTok) * plotH);
    const yTop = padT + plotH - totalH;

    if (st.long_gap_before && i > 0) {
      const sepX = padL + i * slotW;
      const line = document.createElementNS(ns, "line");
      line.setAttribute("x1", String(sepX));
      line.setAttribute("x2", String(sepX));
      line.setAttribute("y1", String(padT));
      line.setAttribute("y2", String(padT + plotH));
      line.setAttribute("stroke", "#64748b");
      line.setAttribute("stroke-dasharray", "3 3");
      svg.appendChild(line);
    }

    const g = document.createElementNS(ns, "g");
    g.style.cursor = "pointer";

    if (st.is_cache_miss) {
      const rect = document.createElementNS(ns, "rect");
      rect.setAttribute("x", String(x));
      rect.setAttribute("y", String(yTop));
      rect.setAttribute("width", String(barW));
      rect.setAttribute("height", String(totalH));
      rect.setAttribute("rx", "2");
      rect.setAttribute("fill", "#eab308");
      g.appendChild(rect);
    } else {
      const cRatio = ctx > 0 ? st.cache_read_tokens / ctx : 0;
      const cacheH = totalH * cRatio;
      const freshH = totalH - cacheH;

      if (freshH > 0.5) {
        const rFresh = document.createElementNS(ns, "rect");
        rFresh.setAttribute("x", String(x));
        rFresh.setAttribute("y", String(yTop));
        rFresh.setAttribute("width", String(barW));
        rFresh.setAttribute("height", String(freshH));
        rFresh.setAttribute("rx", "1");
        rFresh.setAttribute("fill", "#ef4444");
        g.appendChild(rFresh);
      }
      if (cacheH > 0.5) {
        const rCache = document.createElementNS(ns, "rect");
        rCache.setAttribute("x", String(x));
        rCache.setAttribute("y", String(yTop + freshH));
        rCache.setAttribute("width", String(barW));
        rCache.setAttribute("height", String(cacheH));
        rCache.setAttribute("rx", "1");
        rCache.setAttribute("fill", "#16c772");
        g.appendChild(rCache);
      }
    }

    if (st.is_bloat) {
      const mark = document.createElementNS(ns, "text");
      mark.setAttribute("x", String(cx));
      mark.setAttribute("y", String(Math.max(11, yTop - 3)));
      mark.setAttribute("text-anchor", "middle");
      mark.setAttribute("fill", "#f97316");
      mark.setAttribute("font-size", "9");
      mark.setAttribute("font-weight", "bold");
      mark.textContent = "!";
      g.appendChild(mark);
    }

    g.addEventListener("mouseenter", () => {
      const pctC = (st.cache_ratio * 100).toFixed(1);
      const pctF = (st.fresh_ratio * 100).toFixed(1);
      const reasonLine = st.is_cache_miss
        ? `<div class="text-yellow">Rớt Cache: ${onFormatHtml(st.miss_reason || "MISS")}</div>`
        : "";
      const bloatLine = st.is_bloat
        ? `<div style="color:#f97316">[BLOAT] +${fmtTok(st.delta_context)} tok</div>`
        : "";
      tooltip.innerHTML =
        `<div><strong>Step #${st.step_index}</strong> (${fmtClock(st.timestamp)})</div>` +
        `<div class="text-green">Cache: ${fmtTok(st.cache_read_tokens)} (${pctC}%)</div>` +
        `<div class="text-red">Fresh: ${fmtTok(st.input_tokens)} (${pctF}%)</div>` +
        `<div>Output: ${fmtTok(st.output_tokens)} | ${st.total_latency_s}s</div>` +
        reasonLine +
        bloatLine;
      tooltip.classList.remove("hidden");
      const leftPct = Math.min(78, Math.max(2, (cx / W) * 100 - 8));
      tooltip.style.left = `${leftPct}%`;
      tooltip.style.top = "8px";
    });

    g.addEventListener("mouseleave", () => {
      tooltip.classList.add("hidden");
    });

    svg.appendChild(g);

    const stepInterval = Math.max(1, Math.floor(slice.length / 6));
    if (i === 0 || i === slice.length - 1 || i % stepInterval === 0) {
      const lbl = document.createElementNS(ns, "text");
      lbl.setAttribute("x", String(cx));
      lbl.setAttribute("y", String(H - 16));
      lbl.setAttribute("text-anchor", "middle");
      lbl.setAttribute("fill", "#cbd5e1");
      lbl.setAttribute("font-size", "10");
      lbl.textContent = `#${st.step_index}`;
      svg.appendChild(lbl);

      const tLbl = document.createElementNS(ns, "text");
      tLbl.setAttribute("x", String(cx));
      tLbl.setAttribute("y", String(H - 4));
      tLbl.setAttribute("text-anchor", "middle");
      tLbl.setAttribute("fill", "#64748b");
      tLbl.setAttribute("font-size", "9");
      tLbl.textContent = fmtClock(st.timestamp);
      svg.appendChild(tLbl);
    }
  });
}

export function renderNetLiveCanvas(canvas, history) {
  const ctx = canvas.getContext("2d");
  if (!ctx) return;
  const W = canvas.width;
  const H = canvas.height;
  ctx.clearRect(0, 0, W, H);
  ctx.fillStyle = "#0a0f16";
  ctx.fillRect(0, 0, W, H);

  if (!history || history.length < 2) {
    ctx.fillStyle = "#8ea0b8";
    ctx.font = "12px Segoe UI";
    ctx.fillText("Đang thu thập mẫu lưu lượng mạng 10Hz...", 14, H / 2);
    return;
  }

  const maxKbps = Math.max(10, ...history.map((h) => Math.max(h.out_kbps || 0, h.in_kbps || 0)));
  const maxRtt = Math.max(50, ...history.map((h) => h.rtt_ms || 0));

  const drawSeries = (key, maxVal, color) => {
    ctx.beginPath();
    ctx.strokeStyle = color;
    ctx.lineWidth = 2;
    history.forEach((item, idx) => {
      const x = (idx / (history.length - 1)) * (W - 20) + 10;
      const y = H - 20 - ((item[key] || 0) / maxVal) * (H - 36);
      if (idx === 0) ctx.moveTo(x, y);
      else ctx.lineTo(x, y);
    });
    ctx.stroke();
  };

  drawSeries("in_kbps", maxKbps, "#16c772");
  drawSeries("out_kbps", maxKbps, "#06b6d4");
  drawSeries("rtt_ms", maxRtt, "#eab308");

  ctx.fillStyle = "#cbd5e1";
  ctx.font = "11px Segoe UI";
  ctx.fillText(`In (KB/s - Xanh lá) | Out (KB/s - Xanh lơ) [Max ${maxKbps.toFixed(1)} KB/s] | RTT (ms - Vàng) [Max ${maxRtt.toFixed(0)}ms]`, 12, 16);
}
