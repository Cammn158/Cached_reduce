let audioCtx = null;

function ensureCtx() {
  if (!audioCtx && (window.AudioContext || window.webkitAudioContext)) {
    const Ctx = window.AudioContext || window.webkitAudioContext;
    audioCtx = new Ctx();
  }
  if (audioCtx && audioCtx.state === "suspended") {
    audioCtx.resume().catch(() => {});
  }
  return audioCtx;
}

export function playAlertTone(level = "INFO") {
  const ctx = ensureCtx();
  if (!ctx) return;

  const now = ctx.currentTime;
  const freqs =
    level === "CRITICAL"
      ? [880, 660, 880]
      : level === "WARNING"
      ? [740, 740]
      : level === "TEST"
      ? [587, 880]
      : [523];

  freqs.forEach((hz, idx) => {
    const osc = ctx.createOscillator();
    const gain = ctx.createGain();
    osc.type = level === "CRITICAL" ? "sawtooth" : "sine";
    osc.frequency.setValueAtTime(hz, now + idx * 0.16);
    gain.gain.setValueAtTime(0.001, now + idx * 0.16);
    gain.gain.exponentialRampToValueAtTime(0.08, now + idx * 0.16 + 0.02);
    gain.gain.exponentialRampToValueAtTime(0.0001, now + idx * 0.16 + 0.14);
    osc.connect(gain);
    gain.connect(ctx.destination);
    osc.start(now + idx * 0.16);
    osc.stop(now + idx * 0.16 + 0.15);
  });
}
