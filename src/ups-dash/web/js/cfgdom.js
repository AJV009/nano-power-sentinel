/* DOM helpers shared by every CONFIG row (tunables, park, UPS, beeper). */

/* Write text only when it differs: rows are re-synced on every store change
   and every snapshot, and rewriting identical text still replaces the node
   (drops a text selection, re-announces to a screen reader). */
export function setText(el, s) {
  if (el && el.textContent !== s) el.textContent = s;
}

/* A row's status, from the store -- the "unsaved" pill the tab always had,
   now driven by the one pending store:
     unsaved     staged, not confirmed yet (row also gets an edge marker)
     confirming  applied; waiting for the unit's own reading to agree */
export function pillSync(node, store, key) {
  const pend = store.isPending(key);
  const land = !pend && store.isLanded(key);
  node.classList.toggle("is-pending", pend);
  const pill = node.querySelector("[data-dirty]");
  if (!pill) return;
  setText(pill, pend ? "unsaved" : land ? "confirming" : "");
  pill.classList.toggle("hidden", !pend && !land);
  pill.classList.toggle("land", land);
}
