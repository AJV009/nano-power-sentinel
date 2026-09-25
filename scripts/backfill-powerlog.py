#!/usr/bin/env python3
"""One-off: fill the power log (ups_dash/logbook.py) for the days before it
existed, from what the jetson already kept -- the stored samples and the
journal of the jetson units. Run ON THE JETSON, as the ups-dash user:

    python3 backfill-powerlog.py [--days 7] [--dry-run]

It replays the same Logbook the collector runs, so the rows are exactly what
the live code would have written: UPS status changes, draw steps while the
box was not up, charge steps on battery, box state changes, and the
sentinel's / NUT's journal lines (collapsed the same way). It stops at the
first "monitoring started" event, where the live log begins, so nothing is
written twice. Not replayable: the dashboard's own verdict (its state was
never stored) and the box's lines older than box-agent's last 100.

Safe beside the running ups-dash: SQLite WAL, the store's own lock/timeout.
"""
import argparse
import json
import os
import sqlite3
import subprocess
import sys

sys.path.insert(0, os.path.expanduser("~/projects/ups-dash"))
from ups_dash import events                      # noqa: E402
from ups_dash.collector import TAIL_UNITS        # noqa: E402
from ups_dash.logbook import Logbook             # noqa: E402
from ups_dash.store import Store                 # noqa: E402

DB = "/var/lib/ups-dash/telemetry.db"


class Counting(object):
    """The store, or a dry-run stand-in that only counts."""
    def __init__(self, store, dry):
        self.store, self.dry, self.n = store, dry, {}

    def add_event(self, ts, kind, ep, detail):
        self.n[kind] = self.n.get(kind, 0) + 1
        if not self.dry:
            self.store.add_event(ts, kind, ep, detail)

    def last_event_ts(self, kind):
        return None


def journal(since, until):
    cmd = ["journalctl", "-o", "json", "--no-pager",
           "--since", "@%d" % since, "--until", "@%d" % until]
    for u in TAIL_UNITS:
        cmd += ["-u", u]
    out = []
    for raw in subprocess.run(cmd, stdout=subprocess.PIPE).stdout.splitlines():
        try:
            r = json.loads(raw.decode("utf-8", "replace"))
        except Exception:
            continue
        msg = r.get("MESSAGE")
        if isinstance(msg, list):
            msg = "".join(chr(b) for b in msg)
        if msg:
            out.append({"ts": int(r["__REALTIME_TIMESTAMP"]) / 1e6,
                        "unit": r.get("UNIT") or r.get("_SYSTEMD_UNIT") or "?",
                        "msg": msg})
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=float, default=7.0)
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()

    db = sqlite3.connect(DB)
    db.row_factory = sqlite3.Row
    first = db.execute("SELECT MIN(ts) FROM event WHERE kind=? AND detail LIKE ?",
                       (events.COLLECTOR_STARTED, "%jetson_uptime_s%")).fetchone()[0]
    if first is None:
        sys.exit("no live power log yet (no 'monitoring started' with uptime): "
                 "deploy the logbook first")
    until, since = first, first - a.days * 86400
    if db.execute("SELECT COUNT(*) FROM event WHERE kind IN (?,?) AND ts < ?",
                  (events.UPS_STATUS, events.SENTINEL_LOG, until)).fetchone()[0]:
        sys.exit("already backfilled before %d -- refusing to write it twice" % until)

    sink = Counting(Store(DB), a.dry_run)
    lb = Logbook(sink)
    lb._clock_jump = lambda now, ep: None       # replay: no live clock to compare
    lines = journal(since, until)
    rows = db.execute("SELECT * FROM sample WHERE res IN (1, 30) AND ts >= ? "
                      "AND ts < ? ORDER BY ts",
                      (since, until)).fetchall()
    li = 0
    for r in rows:
        now = r["ts"]
        batch = []
        while li < len(lines) and lines[li]["ts"] <= now:
            batch.append(lines[li])
            li += 1
        lb.lines(batch)
        ok = r["ups_status"] is not None
        flags = (r["ups_status"] or "").split()
        lb.observe({"ups": {"ok": ok, "status": r["ups_status"],
                            "on_battery": ("OB" in flags) if ok else None,
                            "charge": r["charge"], "watts": r["watts"],
                            "load_pct": r["load_pct"], "runtime": r["runtime"]},
                    "box": {"state": r["box_state"]}}, now)
    lb.lines(lines[li:])
    print("%s %d samples + %d journal lines (%.1f days before %d) -> %d events"
          % ("would write" if a.dry_run else "wrote", len(rows), len(lines),
             a.days, until, sum(sink.n.values())))
    for k, n in sorted(sink.n.items(), key=lambda kv: -kv[1]):
        print("  %5d  %s" % (n, k))


if __name__ == "__main__":
    main()
