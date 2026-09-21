"""CPU and memory telemetry.

CPU utilisation needs deltas, so a background thread keeps a current value and
the request handler only ever reads it.  That avoids both the "first request
returns null" problem and a race between concurrent requests.
"""

import os
import threading
import time

from . import sysfs

SAMPLE_PERIOD = 2.0


class CpuSampler(object):
    def __init__(self):
        self._lock = threading.Lock()
        self._prev = None
        self.util = None

    def sample(self):
        try:
            with open("/proc/stat") as fh:
                line = fh.readline()
        except Exception:
            return
        if not line or not line.startswith("cpu "):
            return
        try:
            parts = [int(x) for x in line.split()[1:]]
        except Exception:
            return
        idle = parts[3] + (parts[4] if len(parts) > 4 else 0)
        total = sum(parts)
        with self._lock:
            prev, self._prev = self._prev, (idle, total)
        if prev:
            d_idle, d_total = idle - prev[0], total - prev[1]
            if d_total > 0:
                self.util = round(100.0 * (1.0 - d_idle / float(d_total)), 1)

    def run(self):
        # Two samples 200 ms apart so the very first request already has a real
        # value.  Priming twice back-to-back gives d_total == 0 and utilisation
        # silently stays null until a whole period has elapsed.
        try:
            self.sample()
            time.sleep(0.2)
            self.sample()
        except Exception:
            pass
        while True:
            time.sleep(SAMPLE_PERIOD)
            try:
                self.sample()
            except Exception:
                pass

    def start(self):
        threading.Thread(target=self.run, daemon=True).start()


def cpu_block(hwmons, sampler):
    k10 = (hwmons.get("k10temp") or [None])[0]
    temp = sysfs.temp_by_label(k10, "Tctl") or sysfs.first_temp(k10)
    load = None
    raw = sysfs.read("/proc/loadavg")
    if raw:
        try:
            load = [float(x) for x in raw.split()[:3]]
        except Exception:
            load = None
    return {"temp_c": temp, "util_pct": sampler.util, "load": load,
            "cores": os.cpu_count()}


def mem_block():
    vals = {}
    try:
        with open("/proc/meminfo") as fh:
            for line in fh:
                bits = line.split(":")
                if len(bits) == 2:
                    try:
                        vals[bits[0]] = int(bits[1].split()[0])
                    except Exception:
                        pass
    except Exception:
        return {}

    def gb(key):
        v = vals.get(key)
        return round(v / 1048576.0, 2) if v is not None else None

    def delta(a, b):
        if a in vals and b in vals:
            return round((vals[a] - vals[b]) / 1048576.0, 2)
        return None

    return {
        "total_gb": gb("MemTotal"),
        "avail_gb": gb("MemAvailable"),
        # Roughly what a hibernate image would have to carry.
        "used_gb": delta("MemTotal", "MemAvailable"),
        "swap_total_gb": gb("SwapTotal"),
        "swap_used_gb": delta("SwapTotal", "SwapFree"),
    }
