/* The CONFIG tab's pending-changes store.

   ONE store for everything editable on the tab -- governor/sentinel
   tunables, battery-floor park, UPS firmware settings, the beeper
   preference -- keyed by the name the backend uses for it ("reserve_pct",
   "input.sensitivity", "beeper"). Controls only ever write here; nothing is
   sent anywhere until the review popup's Confirm.

   Three layers per key, read top-down:
     pending   what the user staged and has not confirmed yet
     landed    a value that was just applied, held until the live readings
               catch up -- the governor/sentinel report through their logs
               and the beeper through the next UPS poll, so a re-read made
               straight after an apply still shows the OLD value for a few
               seconds. Without this layer the control would snap back to
               the old value and look like the apply had failed.
     live      the latest reading (GET api/config, then the SSE stream)
   current(k) = landed ?? live, and a pending value equal to current is not a
   change, so it is dropped rather than kept as a no-op row.

   Pure: no DOM, no network. The clock is injectable for tests. */

const LAND_TTL_MS = 120000;   // give up waiting for a reading to catch up

/* Equality across the shapes these values arrive in: NUT hands back
   "10.000000" where we sent 10, and "Medium" should equal "medium". null
   (could not read) only ever equals null -- unknown is not a value. */
export function same(a, b) {
  if (a == null || b == null) return a == null && b == null;
  const na = typeof a === "number" ? a : (String(a).trim() === "" ? NaN : Number(a));
  const nb = typeof b === "number" ? b : (String(b).trim() === "" ? NaN : Number(b));
  if (isFinite(na) && isFinite(nb)) return Math.abs(na - nb) < 1e-9;
  return String(a).trim().toLowerCase() === String(b).trim().toLowerCase();
}

export function createStore(opts) {
  const now = (opts && opts.now) || (() => Date.now());
  const live = new Map();
  const landed = new Map();    // key -> { v, at }
  const pend = new Map();      // key -> staged value (insertion-ordered)
  const subs = new Set();
  let depth = 0;
  let queued = false;

  function emit() {
    if (depth) { queued = true; return; }
    subs.forEach((fn) => fn());
  }
  function current(k) {
    const l = landed.get(k);
    if (l) return l.v;
    return live.has(k) ? live.get(k) : null;
  }
  // Drop a pending value that no longer differs from what is in effect.
  function prune(k) {
    if (pend.has(k) && same(pend.get(k), current(k))) { pend.delete(k); return true; }
    return false;
  }

  return {
    current,
    value(k) { return pend.has(k) ? pend.get(k) : current(k); },
    isPending(k) { return pend.has(k); },
    isLanded(k) { return landed.has(k); },
    get size() { return pend.size; },
    pending() {
      return Array.from(pend, ([key, to]) => ({ key, from: current(key), to }));
    },

    /* A fresh reading. Clears a landed overlay once the reading agrees with
       it (or it has waited too long), and prunes a pending value the
       reading has made redundant. Emits only when something changed, so
       feeding it every snapshot is cheap. */
    setLive(k, v) {
      const val = v === undefined ? null : v;
      const before = current(k);
      live.set(k, val);
      let changed = false;
      const l = landed.get(k);
      if (l && (same(l.v, val) || now() - l.at > LAND_TTL_MS)) {
        landed.delete(k);
        changed = true;
      }
      if (!same(before, current(k))) changed = true;
      if (prune(k)) changed = true;
      if (changed) emit();
    },

    /* Just applied: show v as current until a reading confirms it. */
    land(k, v) {
      if (same(live.get(k), v)) landed.delete(k);
      else landed.set(k, { v, at: now() });
      prune(k);
      emit();
    },

    stage(k, v) {
      if (same(v, current(k))) {
        if (!pend.delete(k)) return;
      } else {
        if (pend.has(k) && same(pend.get(k), v)) return;
        pend.set(k, v);
      }
      emit();
    },
    revert(k) { if (pend.delete(k)) emit(); },
    clearAll() { if (pend.size) { pend.clear(); emit(); } },

    /* Group several writes into one notification. */
    batch(fn) {
      depth += 1;
      try { fn(); } finally {
        depth -= 1;
        if (!depth && queued) { queued = false; emit(); }
      }
    },
    subscribe(fn) { subs.add(fn); return () => subs.delete(fn); },
  };
}
