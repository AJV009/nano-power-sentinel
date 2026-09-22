/* Reset to the known-good baseline -- CONFIG tab, last panel.

   Complete: every tunable in config.BASELINE (5 governor, 4 sentinel,
   2 park) PLUS the UPS firmware settings and the beeper
   (upsops.UPS_BASELINE), each by its human name, current -> baseline.

   Pressing it no longer writes anything. It STAGES every baseline value
   into the tab's pending store -- which also undoes any edit already
   pending on a setting that is at baseline -- and opens the same review
   popup as SAVE, so a reset is read and confirmed like any other change.
   Cancelling leaves the reset staged in the bar, revertible row by row. */

import { esc } from "./format.js";
import { resetRows } from "./catalog.js";
import { setText } from "./cfgdom.js";

export function resetHtml() {
  return `
    <section class="panel" data-reset><h2>Reset</h2>
      <div class="notebox" data-reset-note></div>
      <div class="cfg" data-reset-list></div>
      <div class="btnrow">
        <button type="button" class="btn" id="doreset">Reset all to baseline</button>
      </div>
    </section>`;
}

function listHtml(rows) {
  return rows.map((r) => `
    <div class="kv"><span class="k">${esc(r.label)} <span class="where">${esc(r.where)}</span></span>
      <span class="v">${r.differs ? `${esc(r.curText)} → <b>${esc(r.baseText)}</b>` : esc(r.baseText)}</span></div>`)
    .join("");
}

function noteText(rows) {
  const drift = rows.filter((r) => r.differs);
  const unknown = drift.filter((r) => r.unknown).length;
  if (!drift.length) return "Everything already matches the baseline.";
  return `${drift.length} of ${rows.length} settings differ from the baseline`
    + `${unknown ? ` (${unknown} could not be read, so they are written anyway)` : ""}. `
    + "Resetting stages the baseline for review — nothing is written until you confirm.";
}

/* getPlan() -> {key: baselineValue}; onStaged() opens the review popup.
   Returns sync() to run after any store change. */
export function wireReset(root, store, getCat, getPlan, onStaged) {
  const panel = root.querySelector("[data-reset]");
  if (!panel) return () => {};
  const note = panel.querySelector("[data-reset-note]");
  const list = panel.querySelector("[data-reset-list]");
  const btn = panel.querySelector("#doreset");
  let lastHtml = "";

  btn.addEventListener("click", () => {
    const plan = getPlan();
    store.batch(() => Object.keys(plan).forEach((k) => store.stage(k, plan[k])));
    if (store.size) onStaged();
  });

  return function sync() {
    const plan = getPlan();
    const rows = resetRows(store, getCat(), plan);
    const html = listHtml(rows);
    if (html !== lastHtml) { list.innerHTML = html; lastHtml = html; }
    setText(note, noteText(rows));
    // Only while something IN EFFECT differs. Pending edits alone do not
    // enable it: staging the baseline over them would silently discard them
    // with nothing left to review -- the bar's x / Discard all do that job.
    const drift = rows.some((r) => r.differs);
    btn.disabled = !drift;
    btn.classList.toggle("primary", drift);
  };
}
