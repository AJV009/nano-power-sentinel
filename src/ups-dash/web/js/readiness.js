/* Recovery readiness + wear.

   This panel answers a question nothing else on the dashboard does: IF THE
   POWER DIED RIGHT NOW, WOULD THE RECOVERY ACTUALLY WORK?

   Every check here is something that fails SILENTLY and would otherwise only
   be discovered during a real outage, when it is too late to fix. r8169 drops
   the WoL flag on reboot; Windows rewrites the UEFI boot order on every boot;
   a NUT unit that died stays dead. None of those announce themselves. */

import { num, pct, isNum, esc, rel, dur, DASH } from "./format.js";

function check(ok, label, detail) {
  const mark = ok === true ? "OK" : ok === false ? "FAIL" : "?";
  const cls = ok === true ? "on" : ok === false ? "off" : "";
  return `<div class="kv">
      <span class="k">${esc(label)}${detail ? `<br><span style="font-size:11px">${esc(detail)}</span>` : ""}</span>
      <span class="v"><span class="badge ${cls}">${mark}</span></span>
    </div>`;
}

/* UPS USB link -- reads sysfs (idVendor 051d, interface driver) rather than
   /dev/hidraw*: the only hidraw node on the jetson is the touchscreen, so the
   old check always read healthy even with the UPS unplugged. usbhid-ups
   detaches the kernel HID driver while it holds the device, so a hidraw node
   existing is not itself a sign of health (UPS-TOOLING.md §2). */
function upsLinkCheck(link) {
  const l = link || {};
  if (l.held) return check(true, "UPS USB link", `held by NUT on port ${l.port || "?"}`);
  if (l.present === true) {
    return check(false, "UPS USB link", `on the bus but NUT is not attached (driver: ${l.driver || "?"})`);
  }
  if (l.present === false) {
    return check(false, "UPS USB link", "not on the USB bus — unplugged, or the UPS is off");
  }
  return check(null, "UPS USB link", "cannot read sysfs");
}

function replaceBatteryCheck(ups) {
  const rb = ups.replace_battery;
  if (rb == null) return check(null, "Battery needs replacing", "cannot read");
  return check(!rb, "Battery needs replacing",
    rb ? "RB flag is set on the UPS — replace the pack" : "no RB flag from the UPS");
}

/* The sentinel publishes its own state (/run/ups-sentinel/state.json, 30 s
   heartbeat; docs/LEDGER.md). A heartbeat older than 90 s, or a pid that is
   gone, means it is not running its loop -- and nothing else would say so
   until the next outage. snap.ledger comes from ledger_tick.py. */
function sentinelCheck(led) {
  const label = "Sentinel is publishing";
  if (!led) return check(null, label, "not reported by this ups-dash");
  const s = led.sentinel, age = led.sentinel_age, st = led.sentinel_status;
  if (s) {
    return check(true, label, `heartbeat ${dur(age)} ago · ${s.state || "?"}`
      + (isNum(s.pid) ? ` · pid ${s.pid}` : ""));
  }
  if (st === "unreadable") {
    return check(null, label, isNum(age)
      ? `state file unreadable (written ${dur(age)} ago)` : "cannot read its state file");
  }
  if (st === "dead") {
    return check(false, label, `its pid is gone (last state ${dur(age)} ago) — it is down`);
  }
  if (st === "missing") {
    return check(false, label, "no state from the sentinel — it may be down or an older build");
  }
  if (isNum(age)) {
    return check(false, label, `no state from the sentinel for ${dur(age)} — it may be down or an older build`);
  }
  return check(null, label, "unknown");
}

export function readinessPanel(snap, health) {
  const h = health || {};
  const w = h.wol || {}, b = h.boot || {}, units = h.units || {};
  const ups = snap.ups || {};
  const box = snap.box || {};
  const svc = snap.services || {};
  const link = (snap.nano || {}).ups_link || {};

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
    upsLinkCheck(link),
    check(box.state === "awake" ? true : box.state === "hibernated" ? null : false,
          "Box is reachable", box.why),
    check(dead.length === 0, "Every power-chain service is running",
          dead.length
            ? dead.map(([k, v]) => `${k.replace(".service", "")}=${v.active}`).join(", ")
            : `${entries.length} units healthy`),
    sentinelCheck(snap.ledger),
    replaceBatteryCheck(ups),
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
  const ups = snap.ups || {};
  const kv = (k, v, note) => `<div class="kv"><span class="k">${esc(k)}${
    note ? `<br><span style="font-size:11px">${esc(note)}</span>` : ""
  }</span><span class="v">${v}</span></div>`;

  const cadence = isNum(ups.pollinterval) && isNum(ups.pollfreq)
    ? `status/timers every ${num(ups.pollinterval)}s — load, voltage and runtime refresh only every ${num(ups.pollfreq)}s`
    : "poll cadence unavailable";

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
    ${kv("NUT driver", esc(ups.driver_version || DASH), cadence)}
  </div></section>`;
}
