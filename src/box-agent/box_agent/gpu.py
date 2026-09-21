"""W7900 discovery and telemetry.

Pick the amdgpu card with the most VRAM: that is the W7900 (48 GB) rather than
the 9900X's iGPU (2 GB).  Never hardcode card0/card1 -- enumeration order is
not stable across boots.
"""

import glob
import os

from . import sysfs


def discover():
    best = None
    for dev in glob.glob("/sys/class/drm/card*/device"):
        total = sysfs.as_int(os.path.join(dev, "mem_info_vram_total"))
        if total is None:
            continue
        if best is None or total > best[0]:
            best = (total, dev)
    if best is None:
        return None
    total, dev = best
    hwmon = None
    for cand in sorted(glob.glob(os.path.join(dev, "hwmon", "hwmon*"))):
        if sysfs.read(os.path.join(cand, "name")) == "amdgpu":
            hwmon = cand
            break
    return {"dev": dev, "hwmon": hwmon, "vram_total": total,
            "pci": os.path.basename(os.path.realpath(dev))}


def block(gpu):
    if not gpu:
        return {"present": False}
    dev, hw = gpu["dev"], gpu["hwmon"]
    cap_min = sysfs.as_float("%s/power1_cap_min" % hw, 1000000.0, 1) if hw else None
    cap_max = sysfs.as_float("%s/power1_cap_max" % hw, 1000000.0, 1) if hw else None
    vram_used = sysfs.as_int(os.path.join(dev, "mem_info_vram_used"))
    return {
        "present": True,
        "pci": gpu["pci"],
        "power_w": sysfs.as_float("%s/power1_average" % hw, 1000000.0, 1) if hw else None,
        "power_cap_w": sysfs.as_float("%s/power1_cap" % hw, 1000000.0, 1) if hw else None,
        # min == max means the cap is hardware-locked and cannot be lowered.
        # On this W7900 that is the case at 241 W -- see DASHBOARD.md §12.
        "cap_locked": (cap_min is not None and cap_max is not None
                       and cap_min == cap_max),
        "temp_edge_c": sysfs.temp_by_label(hw, "edge"),
        "temp_junction_c": sysfs.temp_by_label(hw, "junction"),
        "temp_mem_c": sysfs.temp_by_label(hw, "mem"),
        "busy_pct": sysfs.as_int(os.path.join(dev, "gpu_busy_percent")),
        "vram_used_mb": round(vram_used / 1048576.0, 1) if vram_used else 0,
        "vram_total_mb": round(gpu["vram_total"] / 1048576.0, 1),
    }
