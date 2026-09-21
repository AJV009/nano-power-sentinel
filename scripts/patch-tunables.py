#!/usr/bin/env python3
"""Make hibernate-governor / ups-sentinel read dashboard-editable tunables.

DELIBERATELY MINIMAL.  The loader REASSIGNS the existing module globals, so
every expression that already reads RESERVE_PCT (and friends) is untouched.
No logic changes, no restructuring, compiled-in defaults byte-identical.

Idempotent: running it twice is a no-op.  Backs up before touching anything
and refuses to install a file that does not compile.
"""

import os
import py_compile
import shutil
import sys
import time

BLOCK = '''

# ---- dashboard-editable tunables ------------------------------------------
# Values above may be overridden from TUNABLES_FILE.  That file is ADVISORY:
# anything missing, malformed, mistyped or out of range leaves the compiled-in
# default untouched, and a bad file can NEVER stop this process from starting.
# That single property is what makes it safe to let a web page write here.
TUNABLES_FILE = "/etc/ups-dash/tunables.json"
TUNABLE_SPEC = {
%(spec)s}
_TUN_MTIME = [None]


def load_tunables(initial=False):
    """Never raises.  Re-reads only when the file's mtime has moved."""
    try:
        mtime = os.stat(TUNABLES_FILE).st_mtime
    except Exception:
        return                      # no file is a perfectly normal state
    if not initial and mtime == _TUN_MTIME[0]:
        return
    _TUN_MTIME[0] = mtime
    try:
        import json
        with open(TUNABLES_FILE) as fh:
            data = json.load(fh)
        if not isinstance(data, dict):
            raise ValueError("top level is not an object")
    except Exception as exc:
        log("tunables file rejected (%%s) - keeping current values" %% exc)
        return
    g, changed = globals(), []
    for key, (name, lo, hi) in TUNABLE_SPEC.items():
        if key not in data:
            continue
        try:
            val = float(data[key])
        except Exception:
            log("tunable %%s: not a number - ignored" %% key)
            continue
        if not (lo <= val <= hi):
            log("tunable %%s=%%s outside %%g..%%g - ignored" %% (key, val, lo, hi))
            continue
        if g[name] != val:
            changed.append("%%s %%g -> %%g" %% (key, g[name], val))
            g[name] = val
    if changed:
        log("tunables reloaded: " + "; ".join(changed))
'''

GOVERNOR = {
    "path": "/usr/local/sbin/hibernate-governor",
    "import_old": "import os, socket, subprocess, time",
    "import_new": "import os, socket, subprocess, time",
    "anchor": "REPORT_EVERY      = 15.0\n",
    "spec": (
        '    "reserve_pct":          ("RESERVE_PCT",      10.0,  80.0),\n'
        '    "safety_sec":           ("SAFETY_SEC",        0.0, 300.0),\n'
        '    "write_rate_gbps":      ("WRITE_RATE_GBPS",   0.1,   5.0),\n'
        '    "fixed_overhead_sec":   ("FIXED_OVERHEAD",    0.0, 120.0),\n'
        '    "comms_loss_limit_sec": ("COMMS_LOSS_LIMIT", 15.0, 600.0),\n'
    ),
    "main_old": 'def main():\n    log("started: reserve=',
    "main_new": 'def main():\n    load_tunables(initial=True)\n    log("started: reserve=',
    "loop_old": "    while True:\n        now = time.time()\n        try:",
    "loop_new": "    while True:\n        now = time.time()\n        load_tunables()\n        try:",
}

SENTINEL = {
    "path": "/usr/local/sbin/ups-sentinel",
    "import_old": "import socket, subprocess, time",
    "import_new": "import os, socket, subprocess, time",
    "anchor": "REPORT_EVERY      = 30.0\n",
    "spec": (
        '    "wake_charge_pct":   ("WAKE_CHARGE_PCT",  20.0, 100.0),\n'
        '    "mains_stable_sec":  ("MAINS_STABLE_SEC", 30.0, 1800.0),\n'
        '    "wake_tries":        ("WAKE_TRIES",        1.0,  20.0),\n'
        '    "wake_interval_sec": ("WAKE_INTERVAL",     5.0, 300.0),\n'
    ),
    "main_old": 'def main():\n    log("started: wake gate',
    "main_new": 'def main():\n    load_tunables(initial=True)\n    log("started: wake gate',
    "loop_old": "    while True:\n        now = time.time()\n        st  = ups_status()",
    "loop_new": "    while True:\n        now = time.time()\n        load_tunables()\n        st  = ups_status()",
}


def patch(cfg):
    path = cfg["path"]
    src = open(path).read()

    if "load_tunables" in src:
        print("  already patched, nothing to do")
        return 0

    for key in ("anchor", "main_old", "loop_old"):
        if src.count(cfg[key]) != 1:
            print("  ABORT: anchor %r matched %d times (expected 1)"
                  % (key, src.count(cfg[key])))
            return 1

    out = src
    if cfg["import_old"] != cfg["import_new"]:
        if out.count(cfg["import_old"]) != 1:
            print("  ABORT: import line not found exactly once")
            return 1
        out = out.replace(cfg["import_old"], cfg["import_new"], 1)
    out = out.replace(cfg["anchor"],
                      cfg["anchor"] + BLOCK % {"spec": cfg["spec"]}, 1)
    out = out.replace(cfg["main_old"], cfg["main_new"], 1)
    out = out.replace(cfg["loop_old"], cfg["loop_new"], 1)

    tmp = path + ".new"
    with open(tmp, "w") as fh:
        fh.write(out)
    try:
        py_compile.compile(tmp, doraise=True, cfile="/tmp/_tuncheck.pyc")
    except Exception as exc:
        os.unlink(tmp)
        print("  ABORT: patched file does not compile: %s" % exc)
        return 1

    backup = "%s.pre-tunables-%s" % (path, time.strftime("%Y%m%d%H%M%S"))
    shutil.copy2(path, backup)
    shutil.copymode(path, tmp)
    os.rename(tmp, path)
    print("  patched OK (backup: %s)" % backup)
    return 0


if __name__ == "__main__":
    which = sys.argv[1] if len(sys.argv) > 1 else ""
    cfg = {"governor": GOVERNOR, "sentinel": SENTINEL}.get(which)
    if not cfg:
        print("usage: patch-tunables.py governor|sentinel")
        sys.exit(2)
    print("patching %s" % cfg["path"])
    sys.exit(patch(cfg))
