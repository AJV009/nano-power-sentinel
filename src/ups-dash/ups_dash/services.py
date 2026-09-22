"""systemd health of the power-chain units on the jetson.

Split out of collector.py for the 300-line cap; the collector polls it every
SERVICE_POLL seconds. Never raises: a unit that cannot be read is reported
as {"active": "unknown", "healthy": None}, never as healthy or dead.
"""

import subprocess


def poll(units):
    """{unit: {"active", "type", "result", "healthy"}} -- health, not just
    ActiveState.

    A Type=oneshot unit that ran and exited reads "inactive", which is
    correct rather than broken. Judging on is-active alone produces a
    permanent false alarm."""
    out = {}
    for unit in units:
        try:
            proc = subprocess.run(
                ["systemctl", "show", unit, "-p", "ActiveState",
                 "-p", "Type", "-p", "Result"],
                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=5)
            kv = {}
            for line in proc.stdout.decode().splitlines():
                if "=" in line:
                    k, v = line.split("=", 1)
                    kv[k] = v
            state = kv.get("ActiveState", "unknown")
            out[unit] = {
                "active": state,
                "type": kv.get("Type", ""),
                "result": kv.get("Result", ""),
                "healthy": state in ("active", "activating")
                           or (kv.get("Type") == "oneshot"
                               and kv.get("Result") == "success"),
            }
        except Exception:
            out[unit] = {"active": "unknown", "healthy": None}
    return out
