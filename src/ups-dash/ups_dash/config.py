"""Config writes, split across two machines.

The sentinel's tunables live here on the jetson; the governor's live on the
box.  This module is the ONLY writer, which is what makes the cross-machine
constraint enforceable at all -- neither script can see the other's file, so
the relationship between wake% and reserve% cannot be checked locally by
either one.  Each script still clamps its own values independently.
"""

import json
import os
import urllib.request

LOCAL_PATH = "/etc/ups-dash/tunables.json"

LOCAL_SPEC = {
    "wake_charge_pct":   (20.0, 100.0),
    "mains_stable_sec":  (30.0, 1800.0),
    "wake_tries":        (1.0, 20.0),
    "wake_interval_sec": (5.0, 300.0),
    # Battery-floor PARK: ups-dash's own feature (park.py), not a governor or
    # sentinel constant -- the sentinel never logs these, so they have no
    # compiled-in twin to match the way the rest of BASELINE does.
    "park_enabled":      (0.0, 1.0),
    "park_floor_pct":    (15.0, 80.0),
}
BOX_SPEC = {
    "reserve_pct":          (10.0, 80.0),
    "safety_sec":           (0.0, 300.0),
    "write_rate_gbps":      (0.1, 5.0),
    "fixed_overhead_sec":   (0.0, 120.0),
    "comms_loss_limit_sec": (15.0, 600.0),
}

# park_enabled is stored as a float like every other tunable (one file
# format, no special-casing on disk) but is boolean in meaning: any truthy
# value coerces to 1.0 rather than being validated as a number in [0, 1].
BOOLEAN_KEYS = frozenset({"park_enabled"})

# The known-good baseline the Reset button restores.
#
# ⚠ THESE MUST MATCH the compiled-in defaults in hibernate-governor and
# ups-sentinel, or "reset" and "fresh install" would disagree and the same
# system would behave differently depending on its history.
#
# Reset WRITES these values rather than deleting the override file, because
# the loaders keep their current value for any key that is simply absent --
# deleting the file would revert nothing until the next restart, which is
# exactly the wrong behaviour for a button someone reaches for after
# misconfiguring something.
BASELINE = {
    # governor (workstation)
    "reserve_pct": 50.0,
    "safety_sec": 30.0,
    "write_rate_gbps": 0.5,
    "fixed_overhead_sec": 15.0,
    "comms_loss_limit_sec": 45.0,
    # sentinel (jetson)
    "wake_charge_pct": 70.0,
    "mains_stable_sec": 120.0,
    "wake_tries": 5.0,
    "wake_interval_sec": 30.0,
    # battery-floor park (jetson; ups-dash's own feature)
    "park_enabled": 1.0,
    "park_floor_pct": 35.0,
}

# THE ONE GENUINELY DANGEROUS COMBINATION.  If the box is allowed to wake at
# or below the reserve, it wakes into a charge where the governor immediately
# wants to hibernate again -- a wake/hibernate flapping loop that would chew
# through both the pack and the box.
WAKE_RESERVE_MARGIN = 10.0


def read_local():
    try:
        with open(LOCAL_PATH) as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _write_local(merged):
    tmp = LOCAL_PATH + ".tmp"
    with open(tmp, "w") as fh:
        json.dump(merged, fh, indent=2, sort_keys=True)
        fh.write("\n")
    os.replace(tmp, LOCAL_PATH)     # atomic; the sentinel may read at any moment


def read_box(base_url, timeout=3.0):
    try:
        with urllib.request.urlopen(base_url.rstrip("/") + "/config",
                                    timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8")).get("tunables") or {}
    except Exception:
        return None                 # None = box unreachable, NOT "empty config"


def _put_box(base_url, payload, timeout=5.0):
    req = urllib.request.Request(
        base_url.rstrip("/") + "/config",
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"}, method="PUT")
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _truthy(raw):
    """Coerce a JSON bool/number/string to True/False for BOOLEAN_KEYS.

    Accepts what a browser is actually likely to send (JSON true/false, 1/0,
    "on"/"off") rather than only Python-native types, since this is fed
    straight from the PUT body.
    """
    if isinstance(raw, bool):
        return raw
    if isinstance(raw, (int, float)):
        return raw != 0
    if isinstance(raw, str):
        return raw.strip().lower() not in ("", "0", "false", "off", "no")
    return bool(raw)


def _coerce(updates, spec):
    ok, bad = {}, {}
    for key, raw in updates.items():
        if key not in spec:
            continue
        if key in BOOLEAN_KEYS:
            val = 1.0 if _truthy(raw) else 0.0
        else:
            try:
                val = float(raw)
            except Exception:
                bad[key] = "not a number"
                continue
        lo, hi = spec[key]
        if not (lo <= val <= hi):
            bad[key] = "outside the permitted range %g..%g" % (lo, hi)
            continue
        ok[key] = val
    return ok, bad


def apply(updates, box_url, live_tunables):
    """Validate, then write.  Nothing is written unless validation passes."""
    if not isinstance(updates, dict):
        return {"error": "payload must be a JSON object"}, 400

    unknown = [k for k in updates
               if k not in LOCAL_SPEC and k not in BOX_SPEC]
    local_ok, local_bad = _coerce(updates, LOCAL_SPEC)
    box_ok, box_bad = _coerce(updates, BOX_SPEC)
    rejected = dict(local_bad)
    rejected.update(box_bad)
    for k in unknown:
        rejected[k] = "unknown tunable"

    # Build the state that WOULD result, and check the coupled constraint
    # against it rather than against either half alone.
    #
    # The CONFIG FILES are authoritative here, not `live_tunables`. The latter
    # is learned from the units' logs and therefore lags by up to a poll -- and
    # a safety constraint validated against a lagging cache is not a safety
    # constraint. This exact gap let a bad wake/reserve pair through once.
    warning = None
    merged = dict(live_tunables or {})       # fallback for keys absent from files
    merged.update(read_local())
    box_cfg = read_box(box_url)
    if box_cfg is None:
        warning = ("The box is unreachable, so its current reserve could not be "
                   "confirmed; the wake/reserve check used the last value seen "
                   "in the governor's log.")
    else:
        merged.update(box_cfg)
    merged.update(local_ok)
    merged.update(box_ok)
    wake, reserve = merged.get("wake_charge_pct"), merged.get("reserve_pct")
    if isinstance(wake, (int, float)) and isinstance(reserve, (int, float)):
        if wake < reserve + WAKE_RESERVE_MARGIN:
            return {"error": (
                "Wake (%g%%) must be at least %g points above reserve (%g%%). "
                "Otherwise the box wakes into a charge where the governor "
                "immediately wants to hibernate again - a wake/hibernate "
                "flapping loop." % (wake, WAKE_RESERVE_MARGIN, reserve)),
                "rejected": rejected}, 422

    # Floor >= reserve is legitimate (it just means "park immediately after
    # hibernating"), so this is a heads-up, never a rejection -- unlike the
    # wake/reserve pair above, which is a real flapping-loop hazard.
    park_warning = None
    floor = merged.get("park_floor_pct")
    if isinstance(floor, (int, float)) and isinstance(reserve, (int, float)):
        if floor >= reserve:
            park_warning = (
                "Battery floor (%g%%) is at or above reserve (%g%%): the UPS "
                "will park straight after the box hibernates." % (floor, reserve))

    applied = {}
    if local_ok:
        current = read_local()
        current.update(local_ok)
        _write_local(current)
        applied.update(local_ok)
    if box_ok:
        try:
            result = _put_box(box_url, box_ok)
            applied.update(result.get("applied") or {})
            rejected.update(result.get("rejected") or {})
        except Exception as exc:
            # The box is asleep or unreachable.  Say so plainly rather than
            # reporting a success that did not happen.
            rejected.update({k: "box unreachable (%s)" % exc for k in box_ok})

    out = {"applied": applied, "rejected": rejected}
    if warning:
        out["warning"] = warning
    if park_warning:
        out["park_warning"] = park_warning
    return out, 200


def reset(box_url, live_tunables):
    """Restore every tunable to the known-good baseline and apply it.

    Deliberately routed through apply() rather than writing the files
    directly, so a reset gets the same validation, the same cross-machine
    handling and the same audit event as any other change. A reset that
    bypassed the safety checks would be the one path able to install a
    dangerous combination.
    """
    result, code = apply(dict(BASELINE), box_url, live_tunables)
    result["reset_to"] = dict(BASELINE)
    return result, code
