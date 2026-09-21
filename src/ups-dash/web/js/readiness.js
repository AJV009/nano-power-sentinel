/* Recovery readiness + wear.

   This panel answers a question nothing else on the dashboard does: IF THE
   POWER DIED RIGHT NOW, WOULD THE RECOVERY ACTUALLY WORK?

   Every check here is something that fails SILENTLY and would otherwise only
   be discovered during a real outage, when it is too late to fix. r8169 drops
   the WoL flag on reboot; Windows rewrites the UEFI boot order on every boot;
   a NUT unit that died stays dead. None of those announce themselves. */

import { num, pct, isNum, esc, rel, DASH } from "./format.js";

function check(ok, label, detail) {
  const mark = ok === true ? "OK" : ok === false ? "FAIL" : "?";
  const cls = ok === true ? "on" : ok === false ? "off" : "";
  return `<div class="kv">
      <span class="k">${esc(label)}${detail ? `<br><span style="font-size:11px">${esc(detail)}</span>` : ""}</span>
      <span class="v"><span class="badge ${cls}">${mark}</span></span>
    </div>`;
}

export function readinessPanel(snap, health) {
  const h = health || {};
  const w = h.wol || {}, b = h.boot || {}, units = h.units || {};
  const ups = snap.ups || {};
  const box = snap.box || {};
  const svc = snap.services || {};

  // Both sides now report {active, type, result, healthy}. `healthy` already
  // accounts for oneshot units that ran and exited -- "inactive" is the right
  // state for those, not a failure.
  const allUnits = Object.assign({}, svc, units);
  const entries = Object.entries(allUnits);
  const dead = entries.filter(([, v]) => v && v.healthy === false);

  const checks = [
    check(w.armed, "Box can be woken over the network",
          w.ethtool ? `ethtool Wake-on: ${w.ethtool}` : "not reported"),
    check(b.linux_first, "Linux boots first",
          b.first_label ? String(b.first_label).split("\t")[0] : "boot order unknown"),
    check(ups.ok, "UPS is readable",
          ups.ok ? `status ${ups.status}` : "cannot read the UPS"),
    check(box.state === "awake" ? true : box.state === "hibernated" ? null : false,
          "Box is reachable", box.why),
    check(dead.length === 0, "Every power-chain service is running",
          dead.length
            ? dead.map(([k, v]) => `${k.replace(".service", "")}=${v.active}`).join(", ")
            : `${entries.length} units healthy`),
  ];

  const stale = isNum(h.age_sec) && h.age_sec > 900;
  return `<section class="panel"><h2>Recovery readiness</h2>
    <div class="card">
      ${checks.join("")}
      ${stale ? `<div class="lockwhy">Privileged health data is ${num(h.age_sec / 60, 0)} min old — the box-health timer may not be running.</div>` : ""}
      ${!health ? `<div class="lockwhy">No privileged health data. Install box-health.timer on the box, or the WoL and boot-order checks above cannot be made.</div>` : ""}
    </div></section>`;
}

export function wearPanel(snap, health) {
  const nv = (health || {}).nvme || {};
  const nano = snap.nano || {};
  const pm = nano.power_mode || {};
  const link = nano.ups_link || {};
  const kv = (k, v, note) => `<div class="kv"><span class="k">${esc(k)}${
    note ? `<br><span style="font-size:11px">${esc(note)}</span>` : ""
  }</span><span class="v">${v}</span></div>`;

  return `<section class="panel"><h2>Wear &amp; power events</h2><div class="card">
    ${kv("Unsafe shutdowns",
         `${num(nv.unsafe_shutdowns)} / ${num(nv.power_cycles)}`,
         "times the box lost power without shutting down cleanly — this is the number this whole system exists to stop increasing")}
    ${kv("SSD wear", nv.percentage_used != null ? pct(nv.percentage_used) : DASH,
         `${num(nv.power_on_hours)} power-on hours · ${num(nv.media_errors)} media errors`)}
    ${kv("SD written since boot",
         nano.sd_written_gb != null ? `${num(nano.sd_written_gb, 2)} GB` : DASH,
         "the wear the tiered-write design exists to avoid")}
    ${kv("Jetson power mode", esc(pm.name || DASH),
         pm.mode === 1 ? "reduced, as intended" : "NOT the reduced mode")}
    ${kv("UPS USB link", link.present ? esc((link.hidraw || []).join(", ")) : "ABSENT",
         link.present ? "HID interface enumerated"
                      : "the driver cannot read the UPS without this")}
  </div></section>`;
}
