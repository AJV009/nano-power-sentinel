/* Every editable item on the CONFIG tab, in page order, with its human
   name, where it lives (jetson / box / UPS) and how its value reads.

   The save bar's list, the review matrix and the Reset panel all label and
   format values through here, so a setting cannot read "reserve_pct = 50"
   in one place and "Reserve after hibernate 50 %" in another. Pure: no DOM,
   no network -- the review/reset logic below is testable in node. */

import { TIER1, TIER2, validate } from "./configdefs.js";
import { num, isNum, DASH } from "./format.js";
import { same } from "./pending.js";

export const BEEPER_KEY = "beeper";

// Beeper status (what the UPS reports) -> instant command that sets it.
export const BEEPER_MODE = { enabled: "enable", disabled: "disable" };

export function buildCatalog(upsSettings, upsBaseline) {
  const items = [];
  TIER1.concat(TIER2).forEach((d) => items.push({
    key: d.key, kind: "tunable", label: d.name, where: d.machine,
    unit: d.unit, dec: d.step < 1 ? 2 : 0,
  }));
  items.push({ key: "park_enabled", kind: "tunable", label: "Battery-floor park",
               where: "jetson", bool: true });
  items.push({ key: "park_floor_pct", kind: "tunable", label: "Battery floor",
               where: "jetson", unit: "%", dec: 0 });
  const editable = (upsSettings && upsSettings.editable) || [];
  editable.forEach((s) => items.push({
    key: s.name, kind: "ups", label: s.label || s.name, where: "UPS",
    unit: s.unit || "", dec: 0, type: s.type, blockKey: s.block_key,
  }));
  // A baseline entry the settings list did not describe (older backend)
  // still gets a row, labelled by its NUT name, rather than vanishing.
  Object.keys(upsBaseline || {}).forEach((k) => {
    if (k !== BEEPER_KEY && !items.some((i) => i.key === k)) {
      items.push({ key: k, kind: "ups", label: k, where: "UPS", unit: "", dec: 0 });
    }
  });
  items.push({ key: BEEPER_KEY, kind: "beeper", label: "Beeper", where: "UPS" });
  return items;
}

export function itemFor(cat, key) {
  return cat.find((i) => i.key === key)
    || { key, kind: "tunable", label: key, where: "?", unit: "", dec: 0 };
}

export const TUNABLE_KEYS = TIER1.concat(TIER2).map((d) => d.key)
  .concat(["park_enabled", "park_floor_pct"]);

/* park_enabled is stored as 0.0/1.0 like every other tunable but reads as
   on/off everywhere it is shown. Unknown is an em dash, never 0. */
export function fmtVal(item, v) {
  if (v == null || v === "") return DASH;
  if (item.bool) return Number(v) >= 0.5 ? "on" : "off";
  const n = typeof v === "number" ? v : (isFinite(Number(v)) ? Number(v) : null);
  if (n !== null && item.kind !== "beeper" && item.type !== "enum") {
    const body = num(n, item.dec || 0);
    return item.unit ? `${body} ${item.unit}` : body;
  }
  return String(v);
}

/* The value that is actually running for a tunable. The units' own logs
   win; the written file is the fallback for keys no log line carries
   (comms_loss_limit_sec, and anything before the first startup line). */
export function liveTunable(key, tunables, files, where) {
  const t = tunables || {};
  if (isNum(t[key])) return t[key];
  const f = (files || {})[where === "box" ? "box" : "jetson"];
  return f && isNum(f[key]) ? f[key] : null;
}

export function floorWarning(floor, reserve) {
  if (!isNum(floor) || !isNum(reserve) || floor < reserve) return "";
  return `Floor (${num(floor, 0)}%) is at or above reserve (${num(reserve, 0)}%) — the `
    + `UPS will park straight after the box hibernates.`;
}

/* Live validation of what WOULD result. blocks stop Confirm; warns are
   shown but never stop it. The wake/reserve rule only blocks when this edit
   touches the pair -- a pair already in effect is not the user's edit to
   answer for, and must not hold up an unrelated beeper change. */
export function checks(store) {
  const r = store.value("reserve_pct");
  const w = store.value("wake_charge_pct");
  const touched = (k) => store.isPending(k);
  const blocks = touched("reserve_pct") || touched("wake_charge_pct")
    ? validate({ reserve_pct: r, wake_charge_pct: w }) : [];
  const warns = [];
  const parkOn = Number(store.value("park_enabled")) >= 0.5;
  if (parkOn && (touched("park_floor_pct") || touched("reserve_pct") || touched("park_enabled"))) {
    const fw = floorWarning(store.value("park_floor_pct"), r);
    if (fw) warns.push(fw);
  }
  return { blocks, warns };
}

/* Pending changes as display rows, in page order. */
export function reviewRows(store, cat) {
  const order = (k) => { const i = cat.findIndex((x) => x.key === k); return i < 0 ? 1e6 : i; };
  return store.pending()
    .sort((a, b) => order(a.key) - order(b.key))
    .map((p) => {
      const item = itemFor(cat, p.key);
      return { key: p.key, kind: item.kind, label: item.label, where: item.where,
               to: p.to, fromText: fmtVal(item, p.from), toText: fmtVal(item, p.to) };
    });
}

/* The full baseline: the eleven tunables plus the UPS firmware settings and
   the beeper, limited to items this page can actually edit. */
export function baselinePlan(cat, baseline, upsBaseline) {
  const plan = {};
  const all = Object.assign({}, baseline || {}, upsBaseline || {});
  cat.forEach((item) => { if (all[item.key] !== undefined) plan[item.key] = all[item.key]; });
  return plan;
}

/* One row per baseline item: what is in effect vs what reset restores.
   Unknown current values count as drift -- reset writes them anyway, the
   way the old all-at-once reset did, so "unknown" can never hide a gap. */
export function resetRows(store, cat, plan) {
  return cat.filter((item) => plan[item.key] !== undefined).map((item) => {
    const cur = store.current(item.key);
    const base = plan[item.key];
    const unknown = cur == null;
    const differs = unknown || !same(cur, base);
    return { key: item.key, label: item.label, where: item.where, unknown, differs,
             curText: fmtVal(item, cur), baseText: fmtVal(item, base) };
  });
}
