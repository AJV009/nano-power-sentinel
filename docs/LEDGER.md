# The state ledger

**Status:** implemented 2026-09-22.

## Why

The facts the system acts on were scattered, and the scatter has already
caused bugs:

| Fact | Where it lived | What went wrong |
|---|---|---|
| wake hold | `/var/lib/ups-dash/wake-hold` | — |
| park | `/var/lib/ups-dash/park` | — |
| park outcome | `/var/lib/ups-dash/park-outcome` | — |
| why the box is down | ups-dash memory, rebuilt from the event log | — |
| the sentinel's own state (armed? gate? tries?) | **only in its log lines**, scraped by regex | CONFIG showed a 50 % wake gate while the sentinel ran 70 %: the scraper missed a start line |
| effective display mode | re-derived in the browser (`effectiveMode`, `isSelfTest`) | two copies of one rule |

Three marker files, a log scraper, and browser-side rules add up to three
different ways of learning a fact. The ledger makes it one.

## The rule that does not change

**Two independent deciders.** The sentinel must still recover the box when
ups-dash is dead or wrong.

- It *reads* ups-dash's facts, fail-safe: a missing or stale fact means it
  falls back to its own rules.
- It never takes a decision from ups-dash's classifier.
- It writes only its own file.

One vocabulary, one ledger, two deciders. That independence is the safety
property the design exists to keep.

## Files: one per writer, placed by durability

| File | Writer | Storage | Written |
|---|---|---|---|
| `/var/lib/ups-dash/ledger/dash.json` | ups-dash | persistent | on change only |
| `/run/ups-sentinel/state.json` | ups-sentinel | tmpfs | on change + heartbeat every 30 s |

- **Why two storage places.** A hold or a park must survive a jetson reboot
  mid-outage. The sentinel's own state is rebuilt from scratch on every
  start anyway, so it belongs in tmpfs.
- **SD wear.** A 30 s heartbeat to the SD card would be ~2,900 writes a day
  for nothing. Keeping it in tmpfs is what the tiered-persistence design
  already does.
- **`/run/ups-sentinel`** comes from `RuntimeDirectory=ups-sentinel`
  (`RuntimeDirectoryMode=0755`) in the unit, so the unprivileged ups-dash can
  read it.
- **Writes are atomic**: tmp file + `os.replace`. A reader never sees half a
  file.
- **Single writer per file.** Nobody else ever writes it. That includes the
  sentinel's old stale-hold valve, which deleted `wake-hold`: it now *ignores*
  a stale hold, and ups-dash clears it.

### `dash.json`

```json
{
  "schema": 1, "writer": "ups-dash", "updated": 1790019000.0,
  "hold": null,
  "park": null,
  "park_outcome": null,
  "cause": {"down_cause": "outage", "intent": null, "intent_at": null, "since": 1790018000.0}
}
```

Field shapes:

| Field | Shape | Source |
|---|---|---|
| `hold` | `null` or `{"ts", "reason"}` | as `hold.py` wrote |
| `park` | `null` or `{"phase", "armed_at", "charge", "floor", "hold_at_park", "cause_at_park", "returned_at", "rehibernate_sent"}` | as `park_io` wrote the marker |
| `park_outcome` | `null` or `{"outcome", "ts", "armed_at", "returned_at", "charge"}` | — |
| `cause` | `{"down_cause", "intent", "intent_at", "since"}` | ups-dash's attribution of why the box is down. Informational for other readers. |

### `state.json` (the sentinel)

```json
{
  "schema": 1, "writer": "ups-sentinel", "updated": 1790019000.0,
  "pid": 1234, "started": 1790010000.0,
  "state": "online | onbatt | recovering",
  "outage_seen": true,
  "ups_status": "OL CHRG",
  "ol_since": 1790018500.0,
  "gate": {"charge": 42.0, "need_charge": 70.0,
           "stable_sec": 60.0, "need_stable_sec": 120.0, "open": false},
  "wake": {"tries": 0, "max": 5, "last_wol": null, "exhausted": false},
  "deferring": null,
  "tunables": {"wake_charge_pct": 70.0, "mains_stable_sec": 120.0,
               "wake_tries": 5.0, "wake_interval_sec": 10.0}
}
```

- **`gate`** is `null` unless the sentinel is evaluating a wake.
- **`deferring`** is one of `null`, `"hold"`, `"park:<phase>"` or `"blind"`.
- **A heartbeat older than 90 s** means the sentinel is not publishing: dead,
  hung, or an old build.

## Readers and their staleness rules

**The sentinel reads `dash.json`.** Its rules are unchanged, only the source
moves:

- **`hold`.** It honours a hold as today.
  - A hold present but unreadable counts as **held**, which is the existing
    `hold.get_hold` precedent.
  - A hold older than an hour while the box is up is *ignored*, not deleted.
- **`park`.** It honours a park for `PARK_STALE_SEC` (30 min) after mains
  returns.
- **Missing `dash.json` (transition only).** It falls back to the legacy
  `wake-hold` and `park` files.
- **The reader stays inside the sentinel** (~30 lines), with no import from
  ups-dash. A missing or broken ups-dash install must never stop the
  sentinel from starting.

**ups-dash reads `state.json`** for three things:

- **CONFIG / tunables.** The sentinel keys (`wake_charge_pct`,
  `mains_stable_sec`, `wake_tries`, `wake_interval_sec`) come from
  `state.json` while its heartbeat is fresh. This replaces the regex scraper
  for those keys. When the heartbeat is stale, it falls back to the tunables
  file.
- **States.** The RECOVERING text quotes the sentinel's live gate, e.g.
  "sentinel holding: 42 / 70 % · mains 60 / 120 s", instead of recomputing it.
- **Readiness.** A new check, "Sentinel is publishing": the heartbeat is
  fresh and the pid is alive.

## ups-dash internals

- **`ledger.py`**: owns `dash.json`.
  - `get(key)` and `set(key, value)`. `set` persists only when the value
    changed.
  - `peer()`: the sentinel's facts and their age.
  - `migrate_legacy()`: on startup, fold `wake-hold`, `park` and
    `park-outcome` into `dash.json`, then delete them.
  - It **always re-persists on startup**, which heals a corrupt file.
- **Call sites keep their APIs.**
  - `hold.py` (`set_hold`, `clear_hold`, `get_hold`) becomes a thin façade
    over the ledger.
  - `park_io`'s marker and outcome functions read and write the ledger's
    `park` and `park_outcome` keys.
  - `cause.py` publishes its attribution to `cause`.
- **The browser renders only.** The collector adds
  `snap["view"] = {"mode": ...}`: `derived.mode`, adjusted for a self-test
  (`None`) and a park (`"parked"`). `timeline.js` and `now.js` use
  `snap.view.mode` in place of their own `effectiveMode`/`isSelfTest`.
- **`snap["ledger"]`** = `{"sentinel": <facts or None>, "sentinel_age": s,
  "dash_updated": ts}`.

## Deploy order

1. **Sentinel first.** It reads `dash.json` and falls back to the legacy
   files, so it works before and after migration.
2. **ups-dash second.** It migrates the legacy files into `dash.json` on
   startup.

## Trade-offs to know

1. **A corrupt `dash.json` with ups-dash dead: the box is never auto-woken.**
   "Unreadable counts as held" is the existing hold precedent. It fails
   toward *not* waking, and a human can always press Wake. ups-dash
   re-persists `dash.json` on every start, which heals the file.
2. **Rolling ups-dash back to a pre-ledger build: delete `dash.json`.**
   While `dash.json` exists the sentinel ignores the legacy files entirely,
   so holds written by an old ups-dash would be missed.
3. **The sentinel is two files now**: `ups-sentinel` imports its I/O helpers
   from `ups_sentinel_io.py`. Deploy both, rendered, together (as
   `scripts/install.sh jetson` does). The script alone crash-loops on import
   and never wakes anything.
4. **`state` is the sentinel's own loop variable.** It stays `"onbatt"`
   after mains returns while it defers for a hold or park. Readers tell those
   phases apart with `ups_status` and `deferring`.

## Not in scope

The box side: hibernate-governor and box-agent. The governor's values are
still learned from its log. The box could publish a `state.json` through
box-agent later, using the same schema conventions.
