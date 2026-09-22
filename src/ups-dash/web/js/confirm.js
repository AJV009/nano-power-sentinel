/* The review popup: a matrix of exactly what will change -- setting, where
   it lives, current, new -- and one Confirm. Opened by the save bar's SAVE
   and by "Reset all to baseline".

   Nothing is written until Confirm. After it, each row reports its own
   verdict in place, and the popup stays open until dismissed so the result
   is read rather than flashed. A proper modal: backdrop, focus lands on
   Confirm, Tab stays inside, Esc / backdrop / Cancel close it -- except
   while writes are in flight, when closing would hide their outcome. The
   page underneath is scroll-locked, and its scroll position is untouched. */

import { esc } from "./format.js";

function verdict(row, v, fmt) {
  const now = v.now == null ? "?" : fmt(row.key, v.now);
  switch (v.state) {
    case "busy": return ["busy", "sending…"];
    case "ok":
      return ["ok", row.kind === "ups" ? `✓ applied — the UPS confirms ${now}` : "✓ applied"];
    case "unverified": return ["warn", "✓ accepted — but could not be read back to confirm"];
    case "refused":
      return ["warn", `! accepted, but the UPS still reports ${now} — the firmware may not allow this value. Still pending.`];
    case "held": return ["warn", `‖ ${v.reason}. Still pending.`];
    default: return ["bad", `✗ ${v.reason || "failed"}. Still pending.`];
  }
}

function matrixHtml(rows) {
  return `
    <div class="mx" role="table" aria-label="Changes to apply">
      <div class="mx-row mx-head" role="row">
        <span role="columnheader">Setting</span><span role="columnheader">Where</span>
        <span role="columnheader">Current</span><span role="columnheader">New</span>
      </div>
      ${rows.map((r) => `
      <div class="mx-row" role="row" data-key="${esc(r.key)}">
        <span class="mx-name" role="cell">${esc(r.label)}</span>
        <span class="mx-where" role="cell"><span class="where">${esc(r.where)}</span></span>
        <span class="mx-chg">
          <span class="mx-cur" role="cell">${esc(r.fromText)}</span>
          <span class="mx-arrow" aria-hidden="true">→</span>
          <span class="mx-new" role="cell">${esc(r.toText)}</span>
        </span>
        <span class="mx-res" role="status" data-res></span>
      </div>`).join("")}
    </div>`;
}

function lockScroll() {
  const root = document.documentElement;
  // Desktop scrollbars vanish under overflow:hidden; pad by their width so
  // the page behind the backdrop does not shift sideways.
  const sb = window.innerWidth - root.clientWidth;
  root.classList.add("modal-open");
  if (sb > 0) document.body.style.paddingRight = `${sb}px`;
}
function unlockScroll() {
  document.documentElement.classList.remove("modal-open");
  document.body.style.paddingRight = "";
}

/* opts: { rows, blocks, warns, episode, fmt(key, v),
           run(rows, override, onRow) -> Promise<{results, notes, interlock}>,
           heldOf(rows, out) -> rows } */
export function openReview(opts) {
  const rows = opts.rows;
  const n = rows.length;
  const blocked = opts.blocks.length > 0;
  const opener = document.activeElement;
  let busy = false;
  let done = false;

  const back = document.createElement("div");
  back.className = "modal-back";
  back.innerHTML = `
    <div class="modal" role="dialog" aria-modal="true" aria-labelledby="mx-title">
      <div class="modal-head">
        <h3 id="mx-title">Review ${n} change${n === 1 ? "" : "s"}</h3>
        <div class="modal-sub" data-sub>Nothing has been written yet.</div>
      </div>
      <div class="modal-body">
        ${opts.episode ? `<div class="warnbox"><b>An outage episode is open.</b> Tunables take
          effect immediately; UPS writes are held by the interlock unless you override.</div>` : ""}
        ${blocked ? `<div class="warnbox"><b>Confirm is blocked.</b> ${esc(opts.blocks[0])}</div>` : ""}
        ${opts.warns.map((w) => `<div class="lockwhy">${esc(w)}</div>`).join("")}
        ${matrixHtml(rows)}
        <div data-notes></div>
        <div data-interlock></div>
      </div>
      <div class="modal-foot">
        <button type="button" class="btn" data-cancel>Cancel</button>
        <button type="button" class="btn primary" data-confirm ${blocked ? "disabled" : ""}>
          ${blocked ? "Blocked" : `Confirm ${n} change${n === 1 ? "" : "s"}`}</button>
      </div>
    </div>`;
  const $ = (sel) => back.querySelector(sel);
  const cancel = $("[data-cancel]");
  const confirm = $("[data-confirm]");

  function close() {
    if (busy) return;
    back.remove();
    document.removeEventListener("keydown", onKey, true);
    unlockScroll();
    if (opener && opener.isConnected && opener.focus) opener.focus();
    if (opts.onClose) opts.onClose();
  }

  function onKey(e) {
    if (e.key === "Escape") { e.preventDefault(); close(); return; }
    if (e.key !== "Tab") return;
    const f = Array.from(back.querySelectorAll("button:not([disabled])"));
    if (!f.length) { e.preventDefault(); return; }
    const first = f[0];
    const last = f[f.length - 1];
    if (e.shiftKey && (document.activeElement === first || !back.contains(document.activeElement))) {
      e.preventDefault(); last.focus();
    } else if (!e.shiftKey && (document.activeElement === last || !back.contains(document.activeElement))) {
      e.preventDefault(); first.focus();
    }
  }

  function paintRow(key, v) {
    const row = rows.find((r) => r.key === key);
    const node = Array.from(back.querySelectorAll(".mx-row[data-key]"))
      .find((el) => el.dataset.key === key);
    const cell = node && node.querySelector("[data-res]");
    if (!row || !cell) return;
    const [cls, text] = verdict(row, v, opts.fmt);
    cell.className = `mx-res ${cls}`;
    cell.textContent = text;
  }

  // Verdicts from the first run and any override re-run, so the summary
  // always describes every row in the matrix.
  const acc = { results: {}, notes: [], interlock: null };

  function finish(out, all) {
    const ok = all.filter((r) => ["ok", "unverified"].includes((out.results[r.key] || {}).state)).length;
    $("#mx-title").textContent = ok === all.length
      ? `Applied ${ok} change${ok === 1 ? "" : "s"}`
      : `Applied ${ok} of ${all.length}`;
    $("[data-sub]").textContent = ok === all.length
      ? "Every change took. The page shows the new values."
      : "Anything not applied is still pending in the bar — fix it, or revert it there.";
    $("[data-notes]").innerHTML = out.notes.map((t) => `<div class="notebox">${esc(t)}</div>`).join("");
    const held = opts.heldOf(all, out);
    const lock = $("[data-interlock]");
    lock.innerHTML = held.length ? `
      <div class="warnbox">${esc(out.interlock || "Interlocked.")}
        <div class="btnrow"><button type="button" class="btn danger" data-override>
          Override and apply ${held.length} held change${held.length === 1 ? "" : "s"} anyway</button></div>
      </div>` : "";
    const ovr = lock.querySelector("[data-override]");
    if (ovr) {
      ovr.addEventListener("click", () => go(held, true));
      // It sits below the matrix; on a phone that is often off-screen.
      lock.scrollIntoView({ block: "nearest" });
    }
  }

  async function go(subset, override) {
    busy = true;
    cancel.disabled = true;
    confirm.disabled = true;
    confirm.classList.remove("hidden");
    confirm.textContent = "Applying…";
    $("[data-sub]").textContent = "Writing — the UPS takes a few seconds per setting to confirm.";
    $("[data-interlock]").innerHTML = "";
    let out;
    try {
      out = await opts.run(subset, override, paintRow);
    } catch (e) {
      out = { results: {}, notes: [`Stopped: ${e.message}`], interlock: null };
    }
    Object.assign(acc.results, out.results);
    acc.notes = acc.notes.concat(out.notes);
    acc.interlock = out.interlock;
    busy = false;
    done = true;
    finish(acc, rows);
    cancel.disabled = false;
    cancel.textContent = "Close";
    confirm.classList.add("hidden");
    cancel.focus();
  }

  cancel.addEventListener("click", close);
  confirm.addEventListener("click", () => { if (!done && !blocked) go(rows, false); });
  back.addEventListener("click", (e) => { if (e.target === back) close(); });
  document.addEventListener("keydown", onKey, true);

  lockScroll();
  document.body.appendChild(back);
  (blocked ? cancel : confirm).focus();
}
