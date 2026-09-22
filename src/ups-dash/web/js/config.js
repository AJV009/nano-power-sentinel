/* CONFIG -- "what are the thresholds, and what happens if I change them?"

   Every edit on this tab is STAGED, reviewed, then confirmed:

     control ──> pending store ──> save bar  [ n changes ] [ SAVE ]
     (pending.js)                  (savebar.js)
                          SAVE / Reset ──> review popup, current vs new
                                           (confirm.js)
                                   Confirm ──> one apply, verdict per row
                                               (applychanges.js)

   A control writes only to the store; the store's one listener repaints
   each row in place (text, thumb, pill). The tab is never re-rendered on an
   edit, and the store lives at module level, so a tab switch or an incoming
   snapshot cannot wipe an unsaved edit -- snapshots only refresh the live
   readings underneath it. */

import { get, put, post } from "./api.js";
import { TIER1, TIER2, TIER3 } from "./configdefs.js";
import { num, dur, isNum, esc } from "./format.js";
import { createStore } from "./pending.js";
import { buildCatalog, itemFor, fmtVal, liveTunable, checks, reviewRows,
         baselinePlan, BEEPER_KEY } from "./catalog.js";
import { applyAll, settle, heldRows } from "./applychanges.js";
import { openReview } from "./confirm.js";
import { saveBarHtml, wireSaveBar } from "./savebar.js";
import { parkHtml, wireParkConfig } from "./parkconfig.js";
import { upsFirmwareHtml, wireUpsFirmware, paintReadonly } from "./upsconfig.js";
import { resetHtml, wireReset } from "./resetcfg.js";
import { setText, pillSync } from "./cfgdom.js";

let LAST = null;              // latest snapshot, for the live effect preview
const store = createStore();  // survives re-renders and tab switches
let view = null;              // the mounted tab: { node, cat, d, plan, syncers, ... }
let renderSeq = 0;

const mounted = () => !!(view && view.node.isConnected);

/* Effect preview computed with the governor's OWN formula, so the number
   quoted is the number the governor will actually act on. Shown while the
   row is pending: "now" is what is in effect, the partner of a pair is
   read with its own pending edit included. */
function effect(def, value) {
  if (!LAST || !LAST.ups || !LAST.ups.ok) return "";
  const u = LAST.ups;
  if (def.key === "reserve_pct" && isNum(u.runtime) && isNum(u.charge) && u.charge > 0) {
    const cur = store.current("reserve_pct");
    if (!isNum(cur)) return "";
    const at = (r) => u.runtime * (u.charge - r) / u.charge;
    const d = at(cur) - at(value);
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
  if (def.key === "wake_tries" || def.key === "wake_interval_sec") {
    // Shows the whole window rather than two numbers in isolation. A 10 s
    // interval once meant five packets in ~41 s, the box took ~49 s to
    // resume, and the sentinel declared failure two seconds before it came
    // up. The sentinel now waits RESUME_GRACE after the last packet, so a
    // short interval is no longer harmful -- this just makes the timing
    // visible instead of surprising.
    const tries = def.key === "wake_tries" ? value : store.value("wake_tries");
    const every = def.key === "wake_interval_sec" ? value : store.value("wake_interval_sec");
    if (isNum(tries) && isNum(every)) {
      const span = Math.max(0, (tries - 1) * every);
      return `${num(tries)} packets over ${dur(span)}, then up to 2m more for the box to finish resuming before it is reported as failed.`;
    }
  }
  if (def.key === "mains_stable_sec") {
    return `After mains returns, the box waits at least ${dur(value)} before it is allowed to wake.`;
  }
  return "";
}

function tierRow(def) {
  const v = store.value(def.key);
  return `
    <div class="cfg" data-tier="${def.key}">
      <div class="top">
        <span class="name">${esc(def.name)}</span>
        <span class="val"><span data-out>${num(v, def.step < 1 ? 2 : 0)}</span> ${esc(def.unit)}</span>
      </div>
      <div class="meta">
        <span>${esc(def.machine)}</span><span>range ${def.min}–${def.max}</span>
        <span data-dirty class="pill hidden"></span>
      </div>
      <input type="range" min="${def.min}" max="${def.max}" step="${def.step}"
             value="${isNum(v) ? v : def.min}" aria-label="${esc(def.name)}">
      <div class="effect">${esc(def.help)}</div>
      <div class="effect hidden" data-effect></div>
      ${def.key === "wake_charge_pct" ? '<div class="lockwhy block hidden" data-block></div>' : ""}
    </div>`;
}

function wireTiers(node) {
  const rows = TIER1.concat(TIER2).map((def) => {
    const row = node.querySelector(`.cfg[data-tier="${def.key}"]`);
    const input = row.querySelector("input");
    input.addEventListener("input", () => store.stage(def.key, parseFloat(input.value)));
    return { def, row, input };
  });
  return function sync() {
    // The wake/reserve block is shown under wake, directly below reserve,
    // so it is on screen whichever of the two is being dragged.
    const blocks = checks(store).blocks;
    rows.forEach(({ def, row, input }) => {
      const v = store.value(def.key);
      setText(row.querySelector("[data-out]"), num(v, def.step < 1 ? 2 : 0));
      if (isNum(v) && parseFloat(input.value) !== v) input.value = String(v);
      const fx = store.isPending(def.key) && isNum(v) ? effect(def, v) : "";
      const fxNode = row.querySelector("[data-effect]");
      setText(fxNode, fx);
      fxNode.classList.toggle("hidden", !fx);
      const blk = row.querySelector("[data-block]");
      if (blk) {
        setText(blk, blocks[0] || "");
        blk.classList.toggle("hidden", !blocks.length);
      }
      pillSync(row, store, def.key);
    });
  };
}

function lockedRow(t) {
  // pollinterval's displayed value is read live when we have it, so the
  // pollfreq figure in parentheses can never silently drift from reality.
  const u = LAST && LAST.ups;
  const val = t.name === "NUT pollinterval" && u && isNum(u.pollfreq)
    ? `2 s (pollfreq ${num(u.pollfreq)} s)`
    : t.value;
  return `
    <div class="cfg locked">
      <div class="top"><span class="name">${esc(t.name)}</span>
        <span class="val">${esc(val)}</span></div>
      <div class="meta"><span>${esc(t.machine)}</span><span>locked</span></div>
      <div class="lockwhy">${esc(t.why)}</div>
    </div>`;
}

/* ---- live readings -> store (never touches a pending value) ---- */

function feedConfig(d, cat) {
  const ups = d.ups_settings || {};
  store.batch(() => {
    cat.forEach((item) => {
      if (item.kind === "tunable") {
        store.setLive(item.key, liveTunable(item.key, d.tunables, d.files, item.where));
      }
    });
    (ups.editable || []).forEach((s) => store.setLive(s.name, s.value));
    const beep = ups.beeper !== undefined ? ups.beeper : (LAST && LAST.ups ? LAST.ups.beeper : null);
    store.setLive(BEEPER_KEY, beep);
  });
  const ro = {};
  (ups.readonly || []).forEach((s) => { if (s.block_key) ro[s.block_key] = s.value; });
  if (mounted()) paintReadonly(view.node, ro);
}

function feedSnapshot(snap) {
  const t = snap.tunables;
  const u = snap.ups;
  const files = (view.d && view.d.files) || {};
  store.batch(() => view.cat.forEach((item) => {
    if (item.kind === "tunable" && t) {
      store.setLive(item.key, liveTunable(item.key, t, files, item.where));
    } else if (item.kind === "ups" && u && item.blockKey) {
      store.setLive(item.key, u[item.blockKey]);
    } else if (item.kind === "beeper" && u) {
      store.setLive(item.key, u.beeper);
    }
  }));
  if (u) paintReadonly(view.node, u);
}

export function noteSnapshot(snap) {
  LAST = snap;
  if (!mounted()) return;
  feedSnapshot(snap);
  view.syncTiers();   // effect previews follow the live draw and charge
}

function syncAll() {
  if (mounted()) view.syncers.forEach((fn) => fn());
}

/* After an apply: re-read api/config and fold it in, in place. Not awaited
   by the popup -- GET api/config waits up to 3 s on a sleeping box, and the
   verdicts are already on screen. */
async function refresh() {
  try {
    const d = await get("api/config");
    if (!mounted()) return;
    view.d = d;
    feedConfig(d, view.cat);
  } catch (e) { /* the stream keeps the readings current regardless */ }
}

function review() {
  if (!mounted()) return;
  const rows = reviewRows(store, view.cat);
  if (!rows.length) return;
  const { blocks, warns } = checks(store);
  openReview({
    rows, blocks, warns,
    episode: !!(LAST && LAST.episode),
    fmt: (key, v) => fmtVal(itemFor(view.cat, key), v),
    heldOf: heldRows,
    run: async (subset, override, onRow) => {
      const out = await applyAll(subset, { put, post }, onRow, { override });
      settle(store, subset, out);
      refresh();
      return out;
    },
  });
}

function mount(root, d) {
  if (view && view.unsub) view.unsub();
  const cat = buildCatalog(d.ups_settings, d.ups_baseline);
  feedConfig(d, cat);
  const tun = d.tunables || {};
  const boxDown = d.files && d.files.box === null;
  root.innerHTML = `
    <div data-cfgview>
      ${LAST && LAST.episode ? `<div class="warnbox"><b>An episode is in progress.</b>
        Confirmed changes take effect immediately — they are not deferred until it ends.</div>` : ""}
      ${boxDown ? `<div class="warnbox">The box is not answering, so its
        tunables (reserve, margins) cannot be written right now.</div>` : ""}
      <div class="notebox">Live values come from the units themselves — the
        sentinel's published state, the governor's startup log (source:
        ${esc(tun.source || "unknown")}) — so this page shows what is
        actually running rather than a second copy of the configuration. Edits
        are staged: nothing is written until you review and confirm them.</div>
      <section class="panel"><h2>Thresholds</h2>${TIER1.map(tierRow).join("")}</section>
      <section class="panel"><h2>Margins</h2>${TIER2.map(tierRow).join("")}</section>
      ${parkHtml(store)}
      <section class="panel"><h2>Locked — shown so the reasoning is not lost</h2>
        ${TIER3.map(lockedRow).join("")}</section>
      ${upsFirmwareHtml(d.ups_settings || { editable: [], readonly: [] }, store)}
      ${resetHtml()}
      ${saveBarHtml()}
    </div>`;
  const node = root.querySelector("[data-cfgview]");
  view = { node, cat, d, plan: baselinePlan(cat, d.baseline, d.ups_baseline) };
  view.syncTiers = wireTiers(node);
  view.syncers = [
    view.syncTiers,
    wireParkConfig(node, store),
    wireUpsFirmware(node, store),
    wireReset(node, store, () => view.cat, () => view.plan, review),
    wireSaveBar(node, store, () => view.cat, review),
  ];
  view.unsub = store.subscribe(syncAll);
  syncAll();
}

/* isCurrent() -> is CONFIG still the active tab. A slow api/config (up to
   3 s with the box asleep) must not paint over a tab switched to since. */
export async function renderConfig(root, isCurrent) {
  const seq = ++renderSeq;
  const stillHere = () => seq === renderSeq && (!isCurrent || isCurrent());
  // Re-entering a CONFIG already on screen keeps it up until the fresh copy
  // is ready, rather than flashing "loading…" and losing the scroll spot.
  if (!mounted()) root.innerHTML = '<div class="empty">loading…</div>';
  let d;
  try {
    d = await get("api/config");
  } catch (e) {
    if (stillHere()) root.innerHTML = `<div class="empty">could not load config: ${esc(e.message)}</div>`;
    return;
  }
  if (stillHere()) mount(root, d);
}
