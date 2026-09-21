"""Learn the live thresholds from the units' own startup lines.

The governor and sentinel each announce their configuration when they start:

  [hibernate-governor] started: reserve=30% safety=30s write_rate=0.50 GB/s ...
  [ups-sentinel] started: wake gate = charge >= 50% AND mains stable 120s | ...

Parsing those means the dashboard never keeps a second copy of the real
configuration, so it cannot drift out of sync with what is actually running.
The values below are fallbacks used only until a startup line is seen.
"""

import re
import time

DEFAULTS = {
    "reserve_pct": 30.0, "safety_sec": 30.0, "write_rate_gbps": 0.5,
    "fixed_overhead_sec": 15.0, "wake_charge_pct": 50.0,
    "mains_stable_sec": 120.0, "source": "built-in defaults",
}

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
        self.last_hibernate_signal = 0.0
        self.last_ram_gb = None

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
                self.values.update({
                    "wake_charge_pct": float(m.group(1)),
                    "mains_stable_sec": float(m.group(2)),
                })
            if RELOADED.search(msg):
                for key, _old, new in RELOAD_PAIR.findall(msg):
                    if key in DEFAULTS:
                        self.values[key] = float(new)
                        self.values["source"] = "live reload log"
            if HIBERNATING.search(msg):
                self.last_hibernate_signal = ev.get("ts") or time.time()
            m = RAM_IN_USE.search(msg)
            if m:
                self.last_ram_gb = float(m.group(1))
