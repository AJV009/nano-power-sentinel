/* The floating save bar: [ n changes ] [ SAVE ].

   Fixed just above the bottom tab bar on phones (at the viewport bottom on
   desktop, where the tabs move to the top), shown only while something is
   pending. "n changes" opens a list growing upward out of the bar, one row
   per change with its own revert (x); SAVE opens the review popup.

   Owns only its own nodes. It re-renders its list from the store on every
   change, but never touches the rest of the tab. */

import { esc } from "./format.js";
import { reviewRows, checks } from "./catalog.js";

export function saveBarHtml() {
  // The spacer is ALWAYS present, not only while the bar is: toggling it
  // would change the page height the moment the last change is saved, and a
  // page scrolled to the bottom would jump. A fixed gap never moves anything.
  return `
    <div class="pbar-space" aria-hidden="true"></div>
    <div class="pbar hidden" data-pbar role="region" aria-label="Unsaved changes">
      <div class="pbar-in">
        <div class="pbar-list hidden" id="pbar-list" data-plist>
          <div data-prows></div>
          <button type="button" class="pbar-discard" data-discard>Discard all</button>
        </div>
        <div class="pbar-row">
          <button type="button" class="pbar-count" data-pcount aria-expanded="false"
                  aria-controls="pbar-list"></button>
          <button type="button" class="btn primary pbar-save" data-psave>Save</button>
        </div>
      </div>
    </div>`;
}

function rowsHtml(rows) {
  return rows.map((r) => `
    <div class="pl-row">
      <div class="pl-txt">
        <span class="pl-name">${esc(r.label)}</span>
        <span class="pl-chg">${esc(r.fromText)} → <b>${esc(r.toText)}</b>
          <span class="where">${esc(r.where)}</span></span>
      </div>
      <button type="button" class="pl-x" data-revert="${esc(r.key)}"
              aria-label="Revert ${esc(r.label)}" title="Revert">×</button>
    </div>`).join("");
}

/* Returns sync(): call after any store change. */
export function wireSaveBar(root, store, getCat, onSave) {
  const bar = root.querySelector("[data-pbar]");
  const list = root.querySelector("[data-plist]");
  const prows = root.querySelector("[data-prows]");
  const count = root.querySelector("[data-pcount]");
  const save = root.querySelector("[data-psave]");
  let open = false;
  let lastHtml = "";
  let lastLabel = "";

  function setOpen(v) {
    open = v;
    list.classList.toggle("hidden", !open);
    count.setAttribute("aria-expanded", String(open));
    bar.classList.toggle("open", open);
  }

  function sync() {
    const n = store.size;
    bar.classList.toggle("hidden", n === 0);
    if (n === 0) {
      if (open) setOpen(false);
      lastHtml = "";
      prows.innerHTML = "";
      return;
    }
    const blocked = checks(store).blocks.length > 0;
    bar.classList.toggle("blocked", blocked);
    // "changes" is its own span so a 320 px phone can drop it and still fit
    // "10 · blocked" -- the word that matters when Confirm is blocked.
    const label = `<b>${n}</b><span class="pc-word"> change${n === 1 ? "" : "s"}</span>`
      + `${blocked ? " · blocked" : ""}`
      + `<span class="caret" aria-hidden="true">${open ? "▾" : "▴"}</span>`;
    if (label !== lastLabel) { count.innerHTML = label; lastLabel = label; }
    const html = rowsHtml(reviewRows(store, getCat()));
    if (html !== lastHtml) { prows.innerHTML = html; lastHtml = html; }
  }

  count.addEventListener("click", () => { setOpen(!open); sync(); });
  save.addEventListener("click", () => { setOpen(false); sync(); onSave(); });
  list.addEventListener("click", (e) => {
    const x = e.target.closest("[data-revert]");
    if (x) {
      store.revert(x.dataset.revert);
      // The row just clicked is gone; keep keyboard focus inside the list.
      const next = prows.querySelector("[data-revert]");
      if (next) next.focus(); else if (store.size) count.focus();
      return;
    }
    if (e.target.closest("[data-discard]")
        && window.confirm(`Discard all ${store.size} unsaved changes?`)) store.clearAll();
  });
  bar.addEventListener("keydown", (e) => {
    if (e.key === "Escape" && open) { setOpen(false); sync(); count.focus(); }
  });
  return sync;
}
