"""Journal access.

This is how the dashboard learns what hibernate-governor decided WITHOUT
modifying it (DASHBOARD.md principle 2).  The governor is verified,
safety-critical code; its log is a read-only interface.

Cursor-based, so each 5 s poll only transfers lines that are actually new.
"""

import json
import subprocess

UNIT = "hibernate-governor"
# systemd-sleep logs "Performing sleep operation 'hibernate'" and "System
# returned from sleep operation" under this unit: the box's own account of
# going down and coming back, for the dashboard's power log. Tagged by unit
# so ups-dash's learner still reads the governor's lines only.
SLEEP_UNIT = "systemd-hibernate.service"
# ...but only these two of its lines; the rest (freeze/thaw, PID 1's own
# "Starting System Hibernate") is noise, and must not crowd the governor's
# startup line out of the first read, which ups-dash learns tunables from.
SLEEP_KEEP = ("Performing sleep operation", "System returned from sleep")
LINES = 100
TIMEOUT = 5


def read_journal(cursor=None):
    cmd = ["journalctl", "-u", UNIT, "-u", SLEEP_UNIT, "-o", "json", "--no-pager"]
    cmd += ["--after-cursor", cursor] if cursor else ["-n", str(LINES * 4)]
    try:
        proc = subprocess.run(cmd, stdout=subprocess.PIPE,
                              stderr=subprocess.DEVNULL, timeout=TIMEOUT)
        raw = proc.stdout.decode("utf-8", "replace")
    except Exception:
        return [], cursor, "journal read failed"

    events, last = [], cursor
    for line in raw.splitlines():
        if not line.strip():
            continue
        try:
            rec = json.loads(line)
        except Exception:
            continue
        msg = rec.get("MESSAGE")
        if isinstance(msg, list):
            msg = "".join(chr(b) for b in msg)
        try:
            ts = int(rec.get("__REALTIME_TIMESTAMP")) / 1000000.0
        except Exception:
            ts = None
        last = rec.get("__CURSOR", last)
        if msg:
            unit = (rec.get("_SYSTEMD_UNIT") or "").replace(".service", "")
            if unit != UNIT and not any(k in msg for k in SLEEP_KEEP):
                continue
            events.append({"ts": ts, "msg": msg, "unit": unit})
    return events[-LINES:], last, None


def unit_active(unit):
    try:
        proc = subprocess.run(["systemctl", "is-active", unit],
                              stdout=subprocess.PIPE,
                              stderr=subprocess.DEVNULL, timeout=3)
        return proc.stdout.decode().strip() == "active"
    except Exception:
        return None
