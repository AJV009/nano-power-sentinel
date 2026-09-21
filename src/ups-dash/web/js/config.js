/* CONFIG -- "what are the thresholds, and what happens if I change them?" */

import { get, put, post } from "./api.js";
import { TIER1, TIER2, TIER3, validate } from "./configdefs.js";
import { num, dur, isNum, esc } from "./format.js";

let LAST = null;    // latest snapshot, for the live effect preview
let dirty = {};     // key -> pending value

export function noteSnapshot(snap) { LAST = snap; }

/* Effect preview computed with the governor's OWN formula, so the number
   quoted is the number the governor will actually act on. */
function effect(def, value, tun) {
  if (!LAST || !LAST.ups || !LAST.ups.ok) return "";
  const u = LAST.ups;
  if (def.key === "reserve_pct" && isNum(u.runtime) && isNum(u.charge) && u.charge > 0) {
    const at = (r) => u.runtime * (u.charge - r) / u.charge;
    const d = at(tun.reserve_pct) - at(value);
    if (Math.abs(d) < 30) return "";
    return `At the present ~${Math.round(u.watts)} W draw, hibernate would fire about ${dur(Math.abs(d))} ${d > 0 ? "sooner" : "later"} than it does now.`;
  }
  if (def.key === "wake_charge_pct" && isNum(u.charge)) {
    return value <= u.charge
      ? "The pack is already above this, so a wake would fire as soon as mains steadied."
      : `The pack would need to climb ${num(value - u.charge)} more points before the box is allowed back.`;
  }
  if (def.key === "write_rate_gbps") {
    const ram = (((LAST.box || {}).last_vitals || {}).mem || {}).used_gb;
    if (isNum(ram)) return `With ${num(ram, 1)} GiB in use, the estimated write takes ${dur(ram / value)}.`;
  }
  if (def.key === "mains_stable_sec") {
    return `After mains returns, the box waits at least ${dur(value)} before it is allowed to wake.`;
  }
  return "";
}

function row(def, tun) {
  const v = dirty[def.key] !== undefined ? dirty[def.key] : tun[def.key];
  const dec = def.step < 1 ? 2 : 0;
  return `
    <div class="cfg" data-key="${def.key}">
      <div class="top">
        <span class="name">${esc(def.name)}</span>
        <span class="val"><span data-out>${num(v, dec)}</span> ${esc(def.unit)}</span>
      </div>
      <div class="meta">
        <span>${esc(def.machine)}</span><span>range ${def.min}–${def.max}</span>
        <span data-dirty class="hidden">unsaved</span>
      </div>
      <input type="range" min="${def.min}" max="${def.max}" step="${def.step}"
             value="${isNum(v) ? v : def.min}">
      <div class="effect">${esc(def.help)}</div>
      <div class="effect" data-effect></div>
    </div>`;
}

function lockedRow(t) {
  return `
    <div class="cfg locked">
      <div class="top"><span class="name">${esc(t.name)}</span>
        <span class="val">${esc(t.value)}</span></div>
      <div class="meta"><span>${esc(t.machine)}</span><span>locked</span></div>
      <div class="lockwhy">${esc(t.why)}</div>
    </div>`;
}

function refreshSaveBar(root, tun) {
  const keys = Object.keys(dirty);
  const errs = validate(Object.assign({}, tun, dirty));
  const bar = root.querySelector("#savebar");
  const err = root.querySelector("#cfgerr");
  err.innerHTML = errs.length ? esc(errs[0]) : "";
  err.classList.toggle("hidden", !errs.length);
  bar.classList.toggle("hidden", keys.length === 0);
  const btn = root.querySelector("#save");
  if (btn) {
    btn.disabled = errs.length > 0;
    btn.textContent = errs.length
      ? "Blocked — see the warning above"
      : `Apply ${keys.length} change${keys.length === 1 ? "" : "s"}`;
  }
}

function wire(root, tun) {
  root.querySelectorAll(".cfg[data-key]").forEach((node) => {
    const def = TIER1.concat(TIER2).find((d) => d.key === node.dataset.key);
    const input = node.querySelector("input");
    if (!input || !def) return;
    input.addEventListener("input", () => {
      const value = parseFloat(input.value);
      node.querySelector("[data-out]").textContent = num(value, def.step < 1 ? 2 : 0);
      node.querySelector("[data-effect]").textContent = effect(def, value, tun);
      if (value === tun[def.key]) delete dirty[def.key];
      else dirty[def.key] = value;
      node.querySelector("[data-dirty]")
        .classList.toggle("hidden", dirty[def.key] === undefined);
      refreshSaveBar(root, tun);
    });
  });

  const btn = root.querySelector("#save");
  if (btn) btn.addEventListener("click", async () => {
    btn.disabled = true; btn.textContent = "applying…";
    const res = await put("api/config", dirty);
    const out = root.querySelector("#cfgresult");
    if (!res.ok) {
      out.className = "warnbox";
      out.innerHTML = esc(res.data.error || `failed (${res.status})`);
    } else {
      const ap = Object.entries(res.data.applied || {});
      const rj = Object.entries(res.data.rejected || {});
      out.className = rj.length ? "warnbox" : "notebox";
      out.innerHTML = [
        ap.length ? `Applied: ${ap.map(([k, v]) => `${esc(k)} = ${v}`).join(", ")}` : "",
        rj.length ? `Rejected: ${rj.map(([k, v]) => `${esc(k)} (${esc(v)})`).join(", ")}` : "",
      ].filter(Boolean).join("<br>");
      dirty = {};
      setTimeout(() => renderConfig(root, out.innerHTML, out.className), 1200);
    }
  });
}

export async function renderConfig(root, carryMsg, carryCls) {
  root.innerHTML = '<div class="empty">loading…</div>';
  let d;
  try {
    d = await get("api/config");
  } catch (e) {
    root.innerHTML = `<div class="empty">could not load config: ${esc(e.message)}</div>`;
    return;
  }
  const tun = d.tunables || {};
  const boxDown = d.files && d.files.box === null;
  const live = LAST && LAST.episode;

  root.innerHTML = `
    <div id="cfgresult" class="${carryCls || "notebox hidden"}">${carryMsg || ""}</div>
    ${live ? `<div class="warnbox"><b>An episode is in progress.</b>
      Changes take effect immediately — they are not deferred until it ends.</div>` : ""}
    ${boxDown ? `<div class="warnbox">The box is not answering, so its
      tunables (reserve, margins) cannot be written right now.</div>` : ""}
    <div class="notebox">Live values are read from each unit's own startup log
      (source: ${esc(tun.source || "unknown")}), so this page shows what is
      actually running rather than a second copy of the configuration.</div>
    <div class="warnbox hidden" id="cfgerr"></div>
    <section class="panel"><h2>Thresholds</h2>${TIER1.map((x) => row(x, tun)).join("")}</section>
    <section class="panel"><h2>Margins</h2>${TIER2.map((x) => row(x, tun)).join("")}</section>
    <div id="savebar" class="btnrow hidden"><button class="btn primary" id="save">Apply</button></div>
    <section class="panel"><h2>Locked — shown so the reasoning is not lost</h2>
      ${TIER3.map(lockedRow).join("")}</section>
    ${resetSection(tun, d.baseline)}`;
  wire(root, tun);
  wireReset(root);
  refreshSaveBar(root, tun);
}

/* Reset to the known-good baseline.

   Not a dangerous action — it restores the values this system was tuned and
   tested with — so one confirmation is right, unlike the emergency controls.
   What it does need is to not be a mystery button: it lists exactly what it
   will restore and how many settings currently differ, so pressing it is an
   informed choice rather than a leap. */

function driftFrom(tun, baseline) {
  if (!baseline) return [];
  return Object.keys(baseline).filter((k) =>
    isNum(tun[k]) && Math.abs(tun[k] - baseline[k]) > 1e-9);
}

function labelFor(key) {
  const def = TIER1.concat(TIER2).find((d) => d.key === key);
  return def ? def.name : key;
}

function resetSection(tun, baseline) {
  if (!baseline) return "";
  const drift = driftFrom(tun, baseline);
  const dec = (v) => (v < 1 ? 2 : 0);
  const rows = Object.keys(baseline).map((k) => {
    const changed = drift.indexOf(k) !== -1;
    const cur = isNum(tun[k]) ? num(tun[k], dec(baseline[k])) : "?";
    const base = num(baseline[k], dec(baseline[k]));
    return `<div class="kv"><span class="k">${esc(labelFor(k))}</span>
      <span class="v">${changed ? `${cur} → <b>${base}</b>` : base}</span></div>`;
  }).join("");

  return `
    <section class="panel"><h2>Reset</h2>
      <div class="notebox">
        ${drift.length
          ? `<b>${drift.length} setting${drift.length === 1 ? "" : "s"} differ from the baseline.</b>
             Resetting restores the values this system was tuned and tested with,
             and applies them immediately.`
          : "Everything already matches the baseline."}
      </div>
      <div class="cfg">${rows}</div>
      <div id="resetresult" class="notebox hidden"></div>
      <div class="btnrow">
        <button class="btn ${drift.length ? "primary" : ""}" id="doreset"
                ${drift.length ? "" : "disabled"}>Reset all to baseline</button>
      </div>
    </section>`;
}

function wireReset(root) {
  const btn = root.querySelector("#doreset");
  if (!btn) return;
  btn.addEventListener("click", async () => {
    if (!window.confirm("Reset every threshold to the tested baseline and apply it now?")) return;
    btn.disabled = true;
    btn.textContent = "resetting…";
    const res = await post("api/control/reset-config", {});
    const out = root.querySelector("#resetresult");
    out.classList.remove("hidden");
    if (!res.ok) {
      out.className = "warnbox";
      out.innerHTML = esc((res.data && res.data.error) || `failed (${res.status})`);
      btn.disabled = false;
      btn.textContent = "Reset all to baseline";
      return;
    }
    const rj = Object.entries(res.data.rejected || {});
    out.className = rj.length ? "warnbox" : "notebox";
    out.innerHTML = [
      `Reset applied: ${Object.keys(res.data.applied || {}).length} settings.`,
      rj.length ? `Not applied: ${rj.map(([k, v]) => `${esc(k)} (${esc(v)})`).join(", ")}` : "",
    ].filter(Boolean).join("<br>");
    // Re-read so the page reflects what the units actually loaded, rather
    // than what we asked for.
    setTimeout(() => renderConfig(root, out.innerHTML, out.className), 1500);
  });
}
