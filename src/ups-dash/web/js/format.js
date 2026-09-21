/* Formatting.  One rule above all: a value that could not be read renders as
   an em dash, NEVER as 0 and never as a stale number.  "Cannot read" is a
   third state all the way to the pixel. */

export const DASH = "—";

export const isNum = (v) => typeof v === "number" && isFinite(v);

export function num(v, digits = 0) {
  return isNum(v) ? v.toFixed(digits) : DASH;
}

export function pct(v, digits = 0) {
  return isNum(v) ? `${v.toFixed(digits)}%` : DASH;
}

export function temp(c) {
  return isNum(c) ? `${c.toFixed(1)}°` : DASH;
}

/* ups.load is an integer percent, so watts quantise in ~8.65 W steps.  The
   tilde is not decoration -- it is the honest precision of this reading. */
export function watts(w) {
  return isNum(w) ? `~${Math.round(w)}` : DASH;
}

export function dur(sec) {
  if (!isNum(sec)) return DASH;
  const s = Math.max(0, Math.round(sec));
  if (s < 90) return `${s}s`;
  const m = Math.round(s / 60);
  if (m < 90) return `${m}m`;
  const h = Math.floor(m / 60);
  return `${h}h ${String(m % 60).padStart(2, "0")}m`;
}

export function clock(ts) {
  if (!isNum(ts)) return DASH;
  return new Date(ts * 1000).toLocaleTimeString([], {
    hour: "2-digit", minute: "2-digit", hour12: false,
  });
}

export function dateLabel(ts) {
  if (!isNum(ts)) return DASH;
  return new Date(ts * 1000).toLocaleDateString([], {
    weekday: "short", day: "numeric", month: "short",
  });
}

export function rel(ts) {
  if (!isNum(ts)) return DASH;
  const d = Date.now() / 1000 - ts;
  if (d < 60) return "just now";
  if (d < 3600) return `${Math.round(d / 60)}m ago`;
  if (d < 86400) return `${Math.round(d / 3600)}h ago`;
  return `${Math.round(d / 86400)}d ago`;
}

export function gib(v, digits = 1) {
  return isNum(v) ? `${v.toFixed(digits)} GiB` : DASH;
}

export function el(tag, cls, html) {
  const n = document.createElement(tag);
  if (cls) n.className = cls;
  if (html !== undefined) n.innerHTML = html;
  return n;
}

export function esc(s) {
  return String(s == null ? "" : s).replace(/[&<>"]/g,
    (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
}
