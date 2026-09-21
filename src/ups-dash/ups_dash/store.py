"""SQLite storage with tiered write rates.

THE CONSTRAINT THAT SHAPED THIS FILE (DASHBOARD.md §07): the jetson's 29 GB SD
card is 81% full and is the single point of failure for a machine whose whole
job is to still be alive when everything else is not.  Writing 1 Hz samples to
it forever would wear out the weakest component in the system.

So:
    on mains, nothing happening  ->  one row per 30 s      (res=30)
    episode active               ->  full 1 Hz             (res=1)
    discrete events              ->  always, forever

Full resolution exactly when it matters, near-zero writes the other 99% of the
time.  The live screen reads the RAM ring buffer, not this file.
"""

import json
import os
import sqlite3
import threading
import time

SCHEMA = """
CREATE TABLE IF NOT EXISTS event (
  id         INTEGER PRIMARY KEY,
  ts         REAL NOT NULL,
  kind       TEXT NOT NULL,
  episode_id INTEGER,
  detail     TEXT
);
CREATE INDEX IF NOT EXISTS event_ts ON event(ts);
CREATE INDEX IF NOT EXISTS event_ep ON event(episode_id);

CREATE TABLE IF NOT EXISTS episode (
  id           INTEGER PRIMARY KEY,
  started      REAL NOT NULL,
  ended        REAL,
  kind         TEXT NOT NULL,
  charge_start REAL,
  charge_min   REAL,
  charge_end   REAL,
  hibernated   INTEGER DEFAULT 0,
  hib_bytes    INTEGER,
  hib_secs     REAL,
  resumed_at   REAL,
  resume_secs  REAL,
  wake_cause   TEXT,
  summary      TEXT
);
CREATE INDEX IF NOT EXISTS episode_started ON episode(started);

CREATE TABLE IF NOT EXISTS sample (
  ts           REAL NOT NULL,
  res          INTEGER NOT NULL,
  episode_id   INTEGER,
  ups_status   TEXT,
  charge       REAL,
  runtime      INTEGER,
  batt_v       REAL,
  load_pct     INTEGER,
  input_v      REAL,
  watts        REAL,
  box_state    TEXT,
  box_gpu_w    REAL,
  box_gpu_c    REAL,
  box_cpu_c    REAL,
  box_nvme_c   REAL,
  box_gpu_busy INTEGER,
  box_vram_mb  REAL,
  box_ram_gb   REAL,
  box_cpu_util REAL,
  nano_cpu_c   REAL,
  nano_ram_mb  INTEGER,
  PRIMARY KEY (ts, res)
);
CREATE INDEX IF NOT EXISTS sample_res_ts ON sample(res, ts);
CREATE INDEX IF NOT EXISTS sample_ep ON sample(episode_id);
"""

SAMPLE_COLS = [
    "ups_status", "charge", "runtime", "batt_v", "load_pct", "input_v", "watts",
    "box_state", "box_gpu_w", "box_gpu_c", "box_cpu_c", "box_nvme_c",
    "box_gpu_busy", "box_vram_mb", "box_ram_gb", "box_cpu_util",
    "nano_cpu_c", "nano_ram_mb",
]

# Retention.  Events and episode traces are small and kept forever; the 30 s
# mains-idle stream is the only thing that grows without bound, so it rolls up.
IDLE_KEEP_DAYS = 7
ROLLUP_RES = 3600


class Store(object):
    def __init__(self, path):
        parent = os.path.dirname(path)
        if parent and not os.path.isdir(parent):
            os.makedirs(parent, 0o755)
        self.path = path
        self._lock = threading.RLock()
        self._db = sqlite3.connect(path, check_same_thread=False, timeout=10.0)
        self._db.row_factory = sqlite3.Row
        with self._lock:
            self._db.execute("PRAGMA journal_mode=WAL")
            # NORMAL rather than FULL: one fsync per checkpoint instead of per
            # write.  Losing the last second of telemetry to a power cut is
            # acceptable; wearing out the card is not.
            self._db.execute("PRAGMA synchronous=NORMAL")
            self._db.executescript(SCHEMA)
            self._db.commit()

    # ---- writes -------------------------------------------------------

    def add_event(self, ts, kind, episode_id=None, detail=None):
        with self._lock:
            cur = self._db.execute(
                "INSERT INTO event (ts, kind, episode_id, detail) VALUES (?,?,?,?)",
                (ts, kind, episode_id,
                 json.dumps(detail) if detail is not None else None))
            self._db.commit()
            return cur.lastrowid

    def open_episode(self, ts, kind, charge_start):
        with self._lock:
            cur = self._db.execute(
                "INSERT INTO episode (started, kind, charge_start, charge_min) "
                "VALUES (?,?,?,?)", (ts, kind, charge_start, charge_start))
            self._db.commit()
            return cur.lastrowid

    def update_episode(self, ep_id, **fields):
        if not fields:
            return
        cols = ", ".join("%s=?" % k for k in fields)
        with self._lock:
            self._db.execute("UPDATE episode SET %s WHERE id=?" % cols,
                             list(fields.values()) + [ep_id])
            self._db.commit()

    def note_charge_min(self, ep_id, charge):
        if charge is None:
            return
        with self._lock:
            self._db.execute(
                "UPDATE episode SET charge_min=MIN(COALESCE(charge_min,?),?) "
                "WHERE id=?", (charge, charge, ep_id))
            self._db.commit()

    def insert_sample(self, row, res, episode_id=None):
        vals = [row.get(c) for c in SAMPLE_COLS]
        with self._lock:
            self._db.execute(
                "INSERT OR REPLACE INTO sample (ts, res, episode_id, %s) "
                "VALUES (?,?,?,%s)" % (",".join(SAMPLE_COLS),
                                       ",".join("?" * len(SAMPLE_COLS))),
                [row["ts"], res, episode_id] + vals)
            self._db.commit()

    def insert_many(self, rows, res, episode_id=None):
        """Bulk flush -- used to write the pre-trigger ring buffer into an
        episode so the trace includes the moments BEFORE it opened."""
        if not rows:
            return
        payload = [[r["ts"], res, episode_id] + [r.get(c) for c in SAMPLE_COLS]
                   for r in rows]
        with self._lock:
            self._db.executemany(
                "INSERT OR REPLACE INTO sample (ts, res, episode_id, %s) "
                "VALUES (?,?,?,%s)" % (",".join(SAMPLE_COLS),
                                       ",".join("?" * len(SAMPLE_COLS))),
                payload)
            self._db.commit()

    # ---- reads --------------------------------------------------------

    def _rows(self, sql, args=()):
        with self._lock:
            return [dict(r) for r in self._db.execute(sql, args).fetchall()]

    def episodes(self, limit=30, before=None):
        if before:
            return self._rows(
                "SELECT * FROM episode WHERE started < ? "
                "ORDER BY started DESC LIMIT ?", (before, limit))
        return self._rows(
            "SELECT * FROM episode ORDER BY started DESC LIMIT ?", (limit,))

    def episode(self, ep_id):
        rows = self._rows("SELECT * FROM episode WHERE id=?", (ep_id,))
        if not rows:
            return None
        ep = rows[0]
        ep["trace"] = self._rows(
            "SELECT * FROM sample WHERE episode_id=? ORDER BY ts", (ep_id,))
        ep["events"] = self._rows(
            "SELECT * FROM event WHERE episode_id=? ORDER BY ts", (ep_id,))
        return ep

    def recent_events(self, limit=100):
        return self._rows("SELECT * FROM event ORDER BY ts DESC LIMIT ?", (limit,))

    def samples_since(self, since, res=30):
        return self._rows(
            "SELECT * FROM sample WHERE res=? AND ts>=? ORDER BY ts",
            (res, since))

    def open_episode_row(self):
        rows = self._rows(
            "SELECT * FROM episode WHERE ended IS NULL ORDER BY started DESC LIMIT 1")
        return rows[0] if rows else None

    def stats(self):
        with self._lock:
            def one(sql):
                r = self._db.execute(sql).fetchone()
                return r[0] if r else 0
            return {
                "events": one("SELECT COUNT(*) FROM event"),
                "episodes": one("SELECT COUNT(*) FROM episode"),
                "samples_1hz": one("SELECT COUNT(*) FROM sample WHERE res=1"),
                "samples_30s": one("SELECT COUNT(*) FROM sample WHERE res=30"),
                "samples_hourly": one("SELECT COUNT(*) FROM sample WHERE res=%d"
                                      % ROLLUP_RES),
                "db_bytes": os.path.getsize(self.path)
                            if os.path.exists(self.path) else 0,
            }

    # ---- housekeeping -------------------------------------------------

    def maintain(self, now=None):
        """Roll 30 s idle samples older than IDLE_KEEP_DAYS into hourly means,
        then delete the originals.  Episode traces (res=1) are never touched --
        an outage is finite and its full detail is the point."""
        now = now or time.time()
        cutoff = now - IDLE_KEEP_DAYS * 86400
        with self._lock:
            self._db.execute(
                "INSERT OR REPLACE INTO sample "
                "(ts, res, charge, runtime, batt_v, load_pct, input_v, watts, "
                " box_gpu_w, box_gpu_c, box_cpu_c, box_nvme_c, nano_cpu_c) "
                "SELECT CAST(ts/3600 AS INTEGER)*3600, ?, AVG(charge), "
                "  AVG(runtime), AVG(batt_v), AVG(load_pct), AVG(input_v), "
                "  AVG(watts), AVG(box_gpu_w), AVG(box_gpu_c), AVG(box_cpu_c), "
                "  AVG(box_nvme_c), AVG(nano_cpu_c) "
                "FROM sample WHERE res=30 AND ts<? GROUP BY CAST(ts/3600 AS INTEGER)",
                (ROLLUP_RES, cutoff))
            self._db.execute("DELETE FROM sample WHERE res=30 AND ts<?", (cutoff,))
            self._db.commit()

    def close(self):
        with self._lock:
            try:
                self._db.close()
            except Exception:
                pass
