"""Learn the live thresholds from what the units themselves report.

THE SENTINEL publishes its four keys in its state file (ledger.peer(),
docs/LEDGER.md) -- while that heartbeat is fresh they are taken from there
and nowhere else. That replaced a regex scraper that once missed a start
line, so CONFIG showed a 50 % wake gate while the sentinel ran 70 %.

THE GOVERNOR (and the sentinel, when its heartbeat is stale) announces its
configuration when it starts:

  [hibernate-governor] started: reserve=30% safety=30s write_rate=0.50 GB/s ...
  [ups-sentinel] started: wake gate = charge >= 50% AND mains stable 120s | ...

Parsing those means the dashboard never keeps a second copy of the real
configuration, so it cannot drift out of sync with what is actually running.
The values below are fallbacks used only until one of those is seen.

`values` is that log/file/default fallback; `live` is what is in effect --
the fallback with a fresh heartbeat's sentinel keys on top.
"""

import re
import time

from . import config as cfgmod

# Mirror the COMPILED defaults of hibernate-governor and ups-sentinel (and
# config.BASELINE). When they drift, CONFIG shows a value nothing is running.
DEFAULTS = {
    "reserve_pct": 50.0, "safety_sec": 30.0, "write_rate_gbps": 0.5,
    "fixed_overhead_sec": 15.0, "wake_charge_pct": 70.0,
    "mains_stable_sec": 120.0, "wake_tries": 5.0, "wake_interval_sec": 30.0,
    # ups-dash's own tunables (park.py) -- see the note on LOCAL_PARK_KEYS
    # below for why these can't be learned from a log line like the rest.
    "park_enabled": 1.0, "park_floor_pct": 35.0,
    "source": "built-in defaults",
}

# park_enabled/park_floor_pct are consumed by ups-dash itself, not the
# sentinel, so no startup/reload log line will ever mention them -- the
# regexes below can only ever learn the six keys they were written for.
# Fall back to the file config.py writes, so CONFIG still shows what is
# actually in effect rather than a value frozen at whatever DEFAULTS says.
LOCAL_PARK_KEYS = ("park_enabled", "park_floor_pct")
# The sentinel's own keys come from the same file, and the sentinel loads that
# file on start and on every change -- so until one of its log lines is seen,
# the file is the best evidence of what it runs. The journal is followed from
# NOW (-n 0), so a sentinel that started before ups-dash is never "seen":
# without this, CONFIG showed the stale DEFAULTS (50 %) while it ran at 70 %.
SENTINEL_KEYS = ("wake_charge_pct", "mains_stable_sec", "wake_tries",
                 "wake_interval_sec")
LOCAL_RECHECK_SEC = 5.0    # feed() runs ~1 Hz; don't re-read the file that often

GOVERNOR = re.compile(
    r"reserve=([\d.]+)%\s+safety=([\d.]+)s\s+write_rate=([\d.]+)\s*GB/s"
    r"\s+overhead=([\d.]+)s")
SENTINEL = re.compile(r"charge\s*>=\s*([\d.]+)%\s+AND\s+mains stable\s+([\d.]+)s")
HIBERNATING = re.compile(r"HIBERNATING")
# A unit can change its tunables WITHOUT restarting, so the startup line alone
# goes stale. Follow the reload lines too, or the dashboard validates against
# values that are no longer running.
RELOADED = re.compile(r"tunables reloaded:")
RELOAD_PAIR = re.compile(r"(\w+)\s+([\d.]+)\s*->\s*([\d.]+)")
RAM_IN_USE = re.compile(r"ram(?:\s+in\s+use)?[= ]([\d.]+)\s*GiB")


class Learner(object):
    """Holds the live tunables plus two side-signals scraped from the same logs."""

    def __init__(self):
        self.values = dict(DEFAULTS)
        self.live = dict(DEFAULTS, sentinel_source="built-in defaults")
        self.last_hibernate_signal = 0.0
        self.last_ram_gb = None
        self._local_checked = 0.0
        self._sentinel_seen = False
        self._sentinel_src = "built-in defaults"   # of the fallback

    def feed(self, events):
        for ev in events or []:
            msg = ev.get("msg") or ""
            m = GOVERNOR.search(msg)
            if m:
                self.values.update({
                    "reserve_pct": float(m.group(1)),
                    "safety_sec": float(m.group(2)),
                    "write_rate_gbps": float(m.group(3)),
                    "fixed_overhead_sec": float(m.group(4)),
                    "source": "hibernate-governor startup log",
                })
            m = SENTINEL.search(msg)
            if m:
                self._sentinel_seen = True
                self._sentinel_src = "ups-sentinel log"
                self.values.update({
                    "wake_charge_pct": float(m.group(1)),
                    "mains_stable_sec": float(m.group(2)),
                })
            if RELOADED.search(msg):
                if "[ups-sentinel]" in msg:
                    self._sentinel_seen = True
                    self._sentinel_src = "ups-sentinel log"
                for key, _old, new in RELOAD_PAIR.findall(msg):
                    if key in DEFAULTS:
                        self.values[key] = float(new)
                        self.values["source"] = "live reload log"
            if HIBERNATING.search(msg):
                self.last_hibernate_signal = ev.get("ts") or time.time()
            m = RAM_IN_USE.search(msg)
            if m:
                self.last_ram_gb = float(m.group(1))
        self._sync_local()

    def _sync_local(self):
        """Fill in park_enabled/park_floor_pct from the file, throttled.

        Never raises -- config.read_local() already fails soft to {}, and a
        missing/malformed file here must never disturb the six keys the
        regexes above are responsible for.
        """
        now = time.time()
        if now - self._local_checked < LOCAL_RECHECK_SEC:
            return
        self._local_checked = now
        local = cfgmod.read_local()
        keys = LOCAL_PARK_KEYS + (() if self._sentinel_seen else SENTINEL_KEYS)
        for key in keys:
            if key not in local:
                continue
            try:
                v = float(local[key])
            except (TypeError, ValueError):
                continue
            lo, hi = cfgmod.LOCAL_SPEC.get(key, (v, v))
            if lo <= v <= hi:        # the sentinel skips out-of-range values too
                self.values[key] = v
                if key in SENTINEL_KEYS:
                    self._sentinel_src = "tunables file"

    def peer(self, facts):
        """Once per tick, after feed(): `facts` is ledger.peer()'s -- None
        unless the sentinel's heartbeat is fresh. Rebuilds `live`, keeping
        the same dict while nothing changed (the ring holds 3600 snaps).
        Never raises."""
        try:
            got = {}
            tun = facts.get("tunables") if isinstance(facts, dict) else None
            for key in SENTINEL_KEYS if isinstance(tun, dict) else ():
                v = tun.get(key)
                if isinstance(v, (int, float)) and not isinstance(v, bool):
                    lo, hi = cfgmod.LOCAL_SPEC.get(key, (v, v))
                    if lo <= v <= hi:
                        got[key] = float(v)
            src = "ups-sentinel state" if got else self._sentinel_src
            live = dict(self.values, **got)
            live["sentinel_source"] = src
            live["source"] = "%s · sentinel: %s" % (self.values.get("source"), src)
            if live != self.live:
                self.live = live
        except Exception:
            pass
