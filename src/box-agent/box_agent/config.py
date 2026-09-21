"""The narrow config-write endpoint.

Deliberately NOT a file writer with a path parameter.  It accepts only the
five known governor keys, each within a hard range, and writes one fixed file.
There is no path, no shell, and no way to express anything else.

The governor independently clamps what it reads, so this is the outer of two
checks rather than the only one.
"""

import json
import os

PATH = "/etc/ups-dash/tunables.json"

# key -> (min, max).  Must stay in step with TUNABLE_SPEC inside
# hibernate-governor and with DASHBOARD.md §09.
SPEC = {
    "reserve_pct":          (10.0, 80.0),
    "safety_sec":           (0.0, 300.0),
    "write_rate_gbps":      (0.1, 5.0),
    "fixed_overhead_sec":   (0.0, 120.0),
    "comms_loss_limit_sec": (15.0, 600.0),
}


def read():
    try:
        with open(PATH) as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def apply(updates):
    """Returns (applied, rejected).  Rejections carry a human reason."""
    if not isinstance(updates, dict):
        return {}, {"_": "payload must be a JSON object"}

    current = read()
    applied, rejected = {}, {}
    for key, raw in updates.items():
        if key not in SPEC:
            rejected[key] = "unknown key (this endpoint accepts only %s)" % \
                            ", ".join(sorted(SPEC))
            continue
        try:
            val = float(raw)
        except Exception:
            rejected[key] = "not a number"
            continue
        lo, hi = SPEC[key]
        if not (lo <= val <= hi):
            rejected[key] = "outside the permitted range %g..%g" % (lo, hi)
            continue
        applied[key] = val

    if applied:
        merged = dict(current)
        merged.update(applied)
        tmp = PATH + ".tmp"
        # Atomic replace: the governor may read this file at any moment and
        # must never see a half-written one.
        with open(tmp, "w") as fh:
            json.dump(merged, fh, indent=2, sort_keys=True)
            fh.write("\n")
        os.replace(tmp, PATH)
    return applied, rejected
