/* HEALTH -- "will this system still work next month?"
   Slow-moving things that no other screen shows. */

import { get } from "./api.js";
import { num, pct, gib, dur, DASH, isNum, esc } from "./format.js";
import { readinessPanel, wearPanel } from "./readiness.js";

function ageDays(mfr) {
  if (!mfr) return null;
  const m = /(\d{4})\/(\d{2})\/(\d{2})/.exec(mfr);
  if (!m) return null;
  const then = new Date(+m[1], +m[2] - 1, +m[3]).getTime();
  return Math.floor((Date.now() - then) / 86400000);
}

function kv(k, v, note) {
  return `<div class="kv"><span class="k">${esc(k)}${
    note ? `<br><span style="font-size:11px">${esc(note)}</span>` : ""
  }</span><span class="v">${v}</span></div>`;
}

function packPanel(ups, stats) {
  const days = ageDays(ups.batt_mfr_date);
  return `
    <section class="panel"><h2>Battery pack</h2><div class="card">
      ${kv("Pack voltage", `${num(ups.batt_v, 1)} V`,
           "nominal " + num(ups.voltage_nominal || 24, 0) + " V — the honest degradation signal")}
      ${kv("Manufactured", esc(ups.batt_mfr_date || DASH),
           days != null ? `${days} days ago` : "")}
      ${kv("Claimed runtime", dur(ups.runtime), "at the present load")}
      ${kv("Last self-test", esc(ups.test_result || DASH))}
      ${kv("Episodes recorded", num(stats.episodes),
           "each outage is roughly one partial cycle")}
    </div></section>`;
}

function mainsPanel(ups, samples) {
  const vals = samples.map((s) => s.input_v).filter(isNum);
  let body;
  if (!vals.length) {
    body = '<div class="empty">no mains history recorded yet</div>';
  } else {
    const lo = Math.min(...vals), hi = Math.max(...vals);
    const avg = vals.reduce((a, b) => a + b, 0) / vals.length;
    const tl = ups.transfer_low, th = ups.transfer_high;
    const span = th - tl;
    const posn = (v) => Math.max(0, Math.min(100, ((v - tl) / span) * 100));
    body = `
      <div class="bar" style="height:30px;margin-bottom:8px">
        <div class="fill" style="left:${posn(lo)}%;width:${posn(hi) - posn(lo)}%"></div>
      </div>
      <div class="legend" style="display:flex;justify-content:space-between">
        <span>${num(tl, 0)} V cutoff</span><span>observed ${num(lo, 0)}–${num(hi, 0)} V</span>
        <span>${num(th, 0)} V cutoff</span>
      </div>
      ${kv("Mean input", `${num(avg, 1)} V`, `${vals.length} samples`)}
      ${kv("Closest approach to a transfer",
           `${num(Math.min(lo - tl, th - hi), 0)} V`,
           "headroom before the UPS would switch to battery")}`;
  }
  return `<section class="panel"><h2>Mains quality</h2><div class="card">${body}</div></section>`;
}

function infraPanel(nano, services, stats) {
  const disk = nano.disk || {};
  const danger = isNum(disk.used_pct) && disk.used_pct >= 90;
  const svc = Object.entries(services || {}).map(([u, s]) => {
    const st = (s && typeof s === "object") ? s : { active: s, healthy: s === "active" };
    return `<div class="kv"><span class="k">${esc(u.replace(".service", ""))}</span>
     <span class="v"><span class="badge ${st.healthy ? "on" : "off"}">${esc(st.active)}</span></span></div>`;
  }).join("");
  return `
    <section class="panel"><h2>Infrastructure</h2><div class="card">
      ${kv("SD card", `${num(disk.free_gb, 1)} GB free · ${pct(disk.used_pct)} used`,
           danger ? "CRITICAL - this card is the system's weakest component"
                  : "the system's single point of failure")}
      ${kv("Jetson RAM", `${num(nano.ram_used_mb, 0)} / ${num(nano.ram_total_mb, 0)} MB`)}
      ${kv("Jetson uptime", dur(nano.uptime_sec))}
      ${svc}
    </div></section>
    <section class="panel"><h2>Telemetry store</h2><div class="card">
      ${kv("Database", `${num(stats.db_bytes / 1048576, 2)} MB`)}
      ${kv("1 Hz rows", num(stats.samples_1hz), "written only during episodes")}
      ${kv("30 s rows", num(stats.samples_30s), "idle cadence")}
      ${kv("Hourly rows", num(stats.samples_hourly), "rolled up after 7 days")}
      ${kv("Events", num(stats.events))}
    </div></section>`;
}

export async function renderHealth(root) {
  root.innerHTML = '<div class="empty">loading…</div>';
  let d;
  try {
    d = await get("api/health");
  } catch (e) {
    root.innerHTML = `<div class="empty">could not load health: ${esc(e.message)}</div>`;
    return;
  }
  const ups = d.ups || {}, nano = d.nano || {}, stats = d.stats || {};
  const samples = (d.mains || []).concat(d.idle || []);
  // Readiness leads: "would recovery work right now" outranks every other
  // question on this screen.
  let snap = null;
  try { snap = await get("api/now"); } catch (e) { snap = null; }
  const health = snap && ((snap.box || {}).last_vitals || {}).health;
  root.innerHTML = (snap ? readinessPanel(snap, health) : "")
                 + (snap ? wearPanel(snap, health) : "")
                 + packPanel(ups, stats) + mainsPanel(ups, samples)
                 + infraPanel(nano, d.services, stats);
}
