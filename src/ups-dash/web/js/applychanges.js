/* Confirm -> apply, in one go, with a verdict per row.

   Order and transport, each unchanged from what the old per-control
   buttons did, just sequenced behind one Confirm:
     1. every tunable in ONE  PUT api/config   (the server validates the
        wake/reserve pair against the files and writes jetson + box)
     2. each UPS setting via  POST api/control/ups-set, one at a time --
        each write is re-read from the UPS (~3 s), and the firmware can
        silently ignore a value, so "accepted" and "the UPS now holds it"
        stay separate verdicts
     3. the beeper via        POST api/control/beeper (never interlocked)

   A 409 is the sentinel interlock (an outage episode is open). The first
   one holds every remaining UPS row back unsent; the caller offers an
   explicit override that re-runs only the held rows with override: true.

   No DOM here. `api` is { put, post } from api.js, injected so this runs
   in node against fakes. */

import { BEEPER_MODE } from "./catalog.js";
import { same } from "./pending.js";

export const BUSY = { state: "busy" };
const HELD = { state: "held", reason: "held by the interlock — not sent" };

async function call(fn) {
  try {
    return await fn();
  } catch (e) {
    // api.js throws on a network failure or a non-JSON body (e.g. a proxy
    // error page). That is a failed request, not a crashed popup.
    return { ok: false, status: 0, data: { error: `request failed: ${e.message}` } };
  }
}

function why(res) {
  const d = res.data || {};
  return d.detail || d.error || `failed (${res.status})`;
}

async function applyTunables(rows, api, set, out) {
  rows.forEach((r) => set(r.key, BUSY));
  const body = {};
  rows.forEach((r) => { body[r.key] = r.to; });
  const res = await call(() => api.put("api/config", body));
  const d = res.data || {};
  if (res.ok) {
    const ap = d.applied || {};
    const rj = d.rejected || {};
    rows.forEach((r) => {
      if (Object.prototype.hasOwnProperty.call(ap, r.key)) {
        set(r.key, { state: "ok", clear: true, landed: ap[r.key] });
      } else {
        set(r.key, { state: "bad", reason: rj[r.key] || "not confirmed by the server" });
      }
    });
    if (d.warning) out.notes.push(d.warning);
    if (d.park_warning) out.notes.push(d.park_warning);
    return;
  }
  // 422 = the wake/reserve pair failed the server's own check; nothing was
  // written. One explanation below the matrix, not the same paragraph on
  // every row.
  const rj = d.rejected || {};
  rows.forEach((r) => set(r.key, { state: "bad", reason: rj[r.key] || "not written — see below" }));
  out.notes.push(`Nothing was written to the tunables: ${why(res)}`);
}

function upsVerdict(res) {
  if (!res.ok || !res.data || res.data.ok === false) return { state: "bad", reason: why(res) };
  const v = res.data.verified;
  const now = res.data.now;
  // verified is tri-state: null means the read-back itself failed, which
  // says nothing about the UPS -- never reported as the firmware refusing.
  if (v === true) return { state: "ok", clear: true, now, landed: now };
  if (v === false) return { state: "refused", now };
  return { state: "unverified", clear: true };
}

async function applyUps(rows, api, set, out, override) {
  let held = false;
  for (const r of rows) {
    if (held) { set(r.key, HELD); continue; }
    set(r.key, BUSY);
    const body = { name: r.key, value: r.to };
    if (override) body.override = true;
    const res = await call(() => api.post("api/control/ups-set", body));
    if (res.status === 409) {
      held = true;
      out.interlock = (res.data && res.data.error) || "An outage episode is open.";
      set(r.key, HELD);
      continue;
    }
    set(r.key, upsVerdict(res));
  }
}

async function applyBeeper(row, api, set) {
  const mode = BEEPER_MODE[String(row.to).toLowerCase()];
  if (!mode) { set(row.key, { state: "bad", reason: `unknown beeper state ${row.to}` }); return; }
  set(row.key, BUSY);
  const res = await call(() => api.post("api/control/beeper", { mode }));
  if (!res.ok || !res.data || res.data.ok === false) {
    set(row.key, { state: "bad", reason: why(res) });
    return;
  }
  set(row.key, { state: "ok", clear: true, landed: row.to, detail: res.data.detail });
}

/* rows: [{ key, kind: "tunable"|"ups"|"beeper", to }]. onRow(key, verdict)
   fires as each row starts and finishes, so a slow UPS write shows as
   in-flight rather than a frozen popup. Resolves to
   { results: {key: verdict}, notes: [...], interlock: msg|null }. */
export async function applyAll(rows, api, onRow, opts) {
  const override = !!(opts && opts.override);
  const out = { results: {}, notes: [], interlock: null };
  const set = (key, v) => { out.results[key] = v; if (onRow) onRow(key, v); };
  const tun = rows.filter((r) => r.kind === "tunable");
  if (tun.length) await applyTunables(tun, api, set, out);
  await applyUps(rows.filter((r) => r.kind === "ups"), api, set, out, override);
  for (const r of rows.filter((x) => x.kind === "beeper")) await applyBeeper(r, api, set);
  return out;
}

/* Fold the verdicts back into the store: successes stop being pending (and
   hold their new value until the live readings catch up); failures stay
   pending so a retry is one SAVE away; a refused UPS write records what
   the UPS actually reports. */
export function settle(store, rows, out) {
  store.batch(() => rows.forEach((r) => {
    const v = out.results[r.key];
    if (!v) return;
    if (!v.clear) {
      // Refused: the UPS's own read-back is the truth about what it holds.
      if (v.now !== undefined && v.now !== null) store.setLive(r.key, v.now);
      return;
    }
    if (v.landed !== undefined && v.landed !== null) store.land(r.key, v.landed);
    if (store.isPending(r.key) && same(store.value(r.key), r.to)) store.revert(r.key);
  }));
}

export function heldRows(rows, out) {
  return rows.filter((r) => out.results[r.key] && out.results[r.key].state === "held");
}
