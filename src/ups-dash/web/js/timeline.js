/* The survival timeline -- the hero of the NOW screen.

   ONE axis in every mode: battery charge, 0 -> 100%.  Both thresholds that
   matter (the reserve to hibernate above, the gate to wake at) are charge
   thresholds, so they sit naturally on this axis.  Switching to a time axis
   on battery and back to a charge axis while recovering would make the same
   bar mean two different things.

   The TIME answer -- the thing you actually want -- is the headline number,
   and it changes with the mode:

     on mains     how much runway if the power drops right now
     on battery   how long until the governor hibernates the box
     recovering   how long until the box is allowed to wake

   That is what lets NOW carry no gauge and no standalone runtime readout:
   this single object already holds charge, runtime, drain rate and both
   thresholds. */

import { dur, pct, num, watts, DASH, isNum } from "./format.js";

/* Colour follows the server's severity so the whole UI shifts together and
   cannot disagree with the words next to it. */
const SEVERITY_TO_ACCENT = {
  nominal: "nominal", ok: "recovering", warn: "battery",
  critical: "critical", unknown: "blind",
};

export function stateOf(snap) {
  const st = (snap && snap.state) || {};
  if (st.severity) return SEVERITY_TO_ACCENT[st.severity] || "nominal";
  if (!snap || !snap.ups || !snap.ups.ok) return "blind";
  return "nominal";
}

function headline(snap) {
  const u = snap.ups, d = snap.derived || {}, t = snap.tunables || {};
  if (!u.ok) {
    return { big: DASH, unit: "", note: "cannot read the UPS — holding state" };
  }
  if (d.mode === "battery") {
    if (isNum(d.eta_hibernate_sec)) {
      return d.eta_hibernate_sec <= 0
        ? { big: "NOW", unit: "", note: "at or past the hibernate threshold" }
        : { big: dur(d.eta_hibernate_sec), unit: "",
            note: `until hibernate · reserving ${num(t.reserve_pct)}%` };
    }
    return { big: dur(u.runtime), unit: "", note: "until empty (hibernate estimate unavailable)" };
  }
  if (d.mode === "recovering") {
    if (d.eta_wake_sec === 0) {
      return { big: "READY", unit: "", note: `charge is past the ${num(t.wake_charge_pct)}% gate` };
    }
    return { big: dur(d.eta_wake_sec), unit: "",
             note: `until the ${num(t.wake_charge_pct)}% wake gate` };
  }
  /* With the output de-energised there is no runway and nothing to project.
     Showing "1h 53m runway" here was actively misleading: the box it refers
     to has no power at all, and no countdown will change that. */
  if (d.mode === "output_off") {
    return { big: "OFF", unit: "",
             note: "UPS output de-energised — needs the front-panel button" };
  }
  if (d.mode === "down") {
    const st = snap.state || {};
    return { big: DASH, unit: "",
             note: st.short ? st.short.toLowerCase() : "box is down" };
  }
  return { big: dur(u.runtime), unit: "", note: "runway if mains drops now" };
}

function legend(snap) {
  const u = snap.ups, d = snap.derived || {};
  const left = `charge ${pct(u.charge)}`;
  let mid;
  if (d.mode === "battery" && isNum(d.drain_pct_min)) {
    mid = `draining ${num(d.drain_pct_min, 2)} %/min`;
  } else if (d.mode === "recovering" && isNum(d.drain_pct_min)) {
    mid = `charging ${num(Math.abs(d.drain_pct_min), 2)} %/min`;
  } else {
    mid = `load ${watts(u.watts)} W`;
  }
  const right = (d.mode === "output_off" || d.mode === "down")
    ? `${num(u.batt_v, 1)} V pack · output off`
    : snap.episode
      ? `episode ${dur(snap.episode.elapsed)}`
      : `${num(u.batt_v, 1)} V pack`;
  return [left, mid, right];
}

export function renderTimeline(root, snap) {
  const u = snap.ups || {}, d = snap.derived || {}, t = snap.tunables || {};
  const h = headline(snap);
  const charge = isNum(u.charge) ? Math.max(0, Math.min(100, u.charge)) : 0;
  const reserve = t.reserve_pct;
  const gate = t.wake_charge_pct;
  const showGate = d.mode === "recovering" && isNum(gate);

  const marks = [];
  if (isNum(reserve)) {
    marks.push(`<div class="reserve" style="left:${reserve}%"></div>
       <div class="tick-label" style="left:${reserve}%">reserve ${num(reserve)}%</div>`);
  }
  if (showGate) {
    marks.push(`<div class="gate" style="left:${gate}%"></div>
       <div class="tick-label" style="left:${gate}%">gate ${num(gate)}%</div>`);
  }

  const [a, b, c] = legend(snap);
  root.innerHTML = `
    <div class="headline">
      <span class="big">${h.big}</span>
      <span class="unit">${h.note}</span>
    </div>
    <div class="bar">
      <div class="fill" style="width:${u.ok ? charge : 0}%"></div>
      ${marks.join("")}
    </div>
    <div class="legend"><span>${a}</span><span>${b}</span><span>${c}</span></div>`;
}
