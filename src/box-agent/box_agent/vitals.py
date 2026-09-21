"""Assemble the telemetry snapshot."""

import json
import os
import time

from . import gpu as gpu_mod
from . import logs, power, sysfs, system
from . import __version__

_GPU = gpu_mod.discover()
_SAMPLER = system.CpuSampler()
# pkcheck is a subprocess; at a 5 s poll that would be ~17k spawns a day for a
# value that only changes when someone edits polkit. Cache it.
_HIB_CACHE = {"at": 0.0, "value": None}
_HIB_TTL = 60.0


def start():
    _SAMPLER.start()


def gpu_pci():
    return (_GPU or {}).get("pci", "none")


HEALTH_FILE = "/run/box-agent/health.json"


def privileged_health():
    """Read what the root-run box-health timer collected.

    Kept in a file rather than granting this agent capabilities: the WoL flag,
    NVMe SMART and efibootmgr all need root, and the agent is deliberately
    NoNewPrivileges. A stale or missing file reports its own age rather than
    pretending to be current."""
    try:
        with open(HEALTH_FILE) as fh:
            data = json.load(fh)
        data["age_sec"] = round(time.time() - float(data.get("ts") or 0), 1)
        return data
    except Exception:
        return None


def _cached_hibernate_auth():
    now = time.time()
    if now - _HIB_CACHE["at"] > _HIB_TTL:
        _HIB_CACHE["at"] = now
        _HIB_CACHE["value"] = power.can_hibernate()
    return _HIB_CACHE["value"]


def build(cursor=None):
    errors = []
    hwmons = sysfs.hwmon_by_name()

    nvme_dir = (hwmons.get("nvme") or [None])[0]
    nvme_temp = sysfs.temp_by_label(nvme_dir, "Composite") or sysfs.first_temp(nvme_dir)

    uptime = None
    raw = sysfs.read("/proc/uptime")
    if raw:
        try:
            uptime = round(float(raw.split()[0]), 1)
        except Exception:
            pass

    events, new_cursor, err = logs.read_journal(cursor)
    if err:
        errors.append(err)

    return {
        "ts": time.time(),
        "agent": {"version": __version__, "errors": errors},
        "host": os.uname().nodename,
        # boot_id is the definitive resume-vs-cold-boot discriminator: it is
        # PRESERVED across hibernate and changes on a real reboot.
        "boot_id": sysfs.read("/proc/sys/kernel/random/boot_id"),
        "uptime_sec": uptime,
        "cpu": system.cpu_block(hwmons, _SAMPLER),
        "mem": system.mem_block(),
        "gpu": gpu_mod.block(_GPU),
        "nvme": {"temp_c": nvme_temp},
        "can_hibernate": _cached_hibernate_auth(),
        "health": privileged_health(),
        "governor": {
            "active": logs.unit_active("hibernate-governor"),
            "events": events,
            "cursor": new_cursor,
        },
    }
