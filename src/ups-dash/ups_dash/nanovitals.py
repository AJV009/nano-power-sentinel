"""The jetson's own vitals.

Note there are no INA3221 power rails on a Nano dev kit, so this box CANNOT
measure its own power draw.  Any "nano watts" figure would be fabricated --
do not add one.
"""

import glob
import os
import re


def _read(path):
    try:
        with open(path) as fh:
            return fh.read().strip()
    except Exception:
        return None


def _thermal_zones():
    temps = {}
    for zone in glob.glob("/sys/devices/virtual/thermal/thermal_zone*"):
        name = _read(os.path.join(zone, "type"))
        raw = _read(os.path.join(zone, "temp"))
        if name and raw:
            try:
                temps[name] = round(int(raw) / 1000.0, 1)
            except Exception:
                pass
    return temps


def _meminfo():
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
        pass
    return vals


def _disk():
    try:
        st = os.statvfs("/")
        return {
            "free_gb": round(st.f_bavail * st.f_frsize / 1073741824.0, 2),
            "total_gb": round(st.f_blocks * st.f_frsize / 1073741824.0, 2),
            "used_pct": round(100.0 * (1 - st.f_bavail / float(st.f_blocks)), 1),
        }
    except Exception:
        return None


def _sd_written_gb():
    """Sectors written to the SD card since boot.

    This is the direct measure of the wear the tiered-write design exists to
    avoid (DASHBOARD.md §07). If it climbs fast while nothing is happening,
    the tiering is not working."""
    try:
        with open("/proc/diskstats") as fh:
            for line in fh:
                f = line.split()
                if len(f) > 9 and f[2] == "mmcblk0":
                    return round(int(f[9]) * 512 / 1073741824.0, 3)
    except Exception:
        pass
    return None


def _power_mode():
    """nvpmodel mode. 0 = MAXN, 1 = 5W. Worth surfacing because a reset to
    MAXN would quietly undo the power reduction and nobody would notice."""
    raw = _read("/var/lib/nvpmodel/status")
    if not raw:
        return None
    m = re.search(r"pmode:(\d+)", raw)
    if not m:
        return None
    n = int(m.group(1))
    return {"mode": n, "name": {0: "MAXN", 1: "5W"}.get(n, "mode %d" % n)}


def _ups_link():
    """Is the APC still enumerated? During the pollinterval=1 incident the HID
    interface vanished while lsusb still listed the device, so presence of a
    hidraw node is the honest check."""
    try:
        nodes = sorted(n for n in os.listdir("/dev") if n.startswith("hidraw"))
        return {"hidraw": nodes, "present": bool(nodes)}
    except Exception:
        return {"hidraw": [], "present": None}


def collect():
    temps = _thermal_zones()
    mem = _meminfo()
    used_mb = None
    if "MemTotal" in mem and "MemAvailable" in mem:
        used_mb = int((mem["MemTotal"] - mem["MemAvailable"]) / 1024)
    uptime = None
    raw = _read("/proc/uptime")
    if raw:
        try:
            uptime = round(float(raw.split()[0]), 1)
        except Exception:
            pass
    return {
        "temps": temps,
        "cpu_c": temps.get("CPU-therm"),
        "ram_used_mb": used_mb,
        "ram_total_mb": int(mem.get("MemTotal", 0) / 1024) or None,
        # The SD card is the system's weakest component; surface it always.
        "disk": _disk(),
        "uptime_sec": uptime,
        "sd_written_gb": _sd_written_gb(),
        "power_mode": _power_mode(),
        "ups_link": _ups_link(),
    }
