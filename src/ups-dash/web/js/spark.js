/* Minimal canvas sparkline for the drain trend.

   This derives from charge, which the timeline already shows -- but it answers
   a different question: IS THE DRAIN ACCELERATING?  The timeline gives a point
   projection; this shows whether that projection is getting worse.  Given how
   fast this pack moves, that is worth its space. */

export function sparkline(canvas, points, opts = {}) {
  const dpr = window.devicePixelRatio || 1;
  const w = canvas.clientWidth || 300;
  const h = canvas.clientHeight || 52;
  canvas.width = w * dpr;
  canvas.height = h * dpr;
  const ctx = canvas.getContext("2d");
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  ctx.clearRect(0, 0, w, h);

  const vals = points.filter((p) => typeof p === "number" && isFinite(p));
  if (vals.length < 2) {
    ctx.fillStyle = opts.dim || "#7a7a85";
    ctx.font = "11px ui-monospace, monospace";
    ctx.fillText("not enough history yet", 10, h / 2 + 4);
    return;
  }

  let lo = Math.min(...vals), hi = Math.max(...vals);
  if (hi - lo < 1e-6) { hi += 0.5; lo -= 0.5; }
  const pad = 6;
  const x = (i) => pad + (i / (points.length - 1)) * (w - pad * 2);
  const y = (v) => h - pad - ((v - lo) / (hi - lo)) * (h - pad * 2);

  // zero line: above it the pack is draining, below it it is recharging
  if (lo < 0 && hi > 0) {
    ctx.strokeStyle = opts.line || "#26262b";
    ctx.setLineDash([3, 3]);
    ctx.beginPath(); ctx.moveTo(pad, y(0)); ctx.lineTo(w - pad, y(0)); ctx.stroke();
    ctx.setLineDash([]);
  }

  ctx.strokeStyle = opts.color || "#22d3ee";
  ctx.lineWidth = 1.5;
  ctx.beginPath();
  let started = false;
  points.forEach((v, i) => {
    if (typeof v !== "number" || !isFinite(v)) return;
    if (!started) { ctx.moveTo(x(i), y(v)); started = true; }
    else ctx.lineTo(x(i), y(v));
  });
  ctx.stroke();
}
