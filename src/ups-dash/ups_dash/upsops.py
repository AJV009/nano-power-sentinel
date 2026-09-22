"""UPS-side settings, beeper and self-test.

THE THREE SHUTDOWN REGISTERS this family exposes (docs/UPS-TOOLING.md §3):
  0x15  PowerSummary.DelayBeforeShutdown        load.off / load.off.delay
        Cuts and STAYS OFF until the front-panel button is pressed. This is
        what upsoff.py's emergency modes use, on purpose -- see the warning
        in upscmd.py's module docstring.
  0x40  APCGeneralCollection.APCDelayBeforeReboot   shutdown.reboot
        Cuts after a grace period, then RESTORES on its own -- but only when
        armed while the output is ON. Bench-tested 2026-09-22 (see
        docs/UPS-TOOLING.md §7, test 2): sent while OL OFF after a 0x15 cut,
        the UPS ACKs it, never arms (ups.timer.reboot stays 0), and the
        output stays off. Nothing software-side brings back an output
        latched OFF by 0x15 -- only the front-panel button does.
  0x41  APCGeneralCollection.APCDelayBeforeShutdown  (apcupsd's "hibernate")
        Not reachable from NUT at all -- a PowerSummary row shadows it.
        Listed here only so nobody goes looking for a way to reach it.

Everything that talks to the UPS goes through upscmd.instcmd()/setvar();
this module is the policy layer -- what is allowed, under what conditions,
and how to tell whether a write actually took.
"""

import threading
import time

from . import upscmd

# ---- editable / read-only settings -----------------------------------------

# name -> spec. "block_key" is the matching key in upsblock.build()'s output,
# used both to show the current value and (for enums) as the canonical form.
SETTINGS = {
    "input.sensitivity": {
        "type": "enum", "choices": ("low", "medium", "high"),
        "label": "Input sensitivity", "unit": None, "block_key": "sensitivity",
        "help": ("Transfer window on this 230V family, per the APC manual: "
                 "low 156-300V, medium 176-294V (default), high 176-288V. "
                 "Mains here runs ~243-246V."),
    },
    "battery.charge.low": {
        "type": "int", "min": 10, "max": 50, "step": 5, "unit": "%",
        "label": "Low-battery charge point", "block_key": "lb_charge",
        "help": ("The UPS's OWN low-battery point. Raising it makes the UPS "
                 "assert LB earlier, and LB makes the box's upsmon act "
                 "independently of the hibernate governor."),
    },
    "battery.runtime.low": {
        "type": "int", "min": 60, "max": 600, "step": 30, "unit": "s",
        "label": "Low-battery runtime point", "block_key": "lb_runtime",
        "help": "Same as the charge point above, expressed as a runtime.",
    },
}

READONLY = {
    "input.transfer.low": {
        "label": "Transfer low", "unit": "V", "block_key": "transfer_low",
        "help": ("Follows input.sensitivity. Writing arbitrary transfer "
                 "points is the one setting here that could let bad voltage "
                 "through to the loads, so it is deliberately not offered."),
    },
    "input.transfer.high": {
        "label": "Transfer high", "unit": "V", "block_key": "transfer_high",
        "help": "Follows input.sensitivity; see input.transfer.low.",
    },
}

# What CONFIG's "Reset all to baseline" stages for the UPS itself, next to
# config.BASELINE for the tunables. Keys are SETTINGS names plus "beeper"
# (a ups.beeper.status value, sent as beeper.enable/disable). These are the
# values this system was tested with -- medium is also the APC default (see
# input.sensitivity's help); the beeper is off because the tested preference
# is silent at night. Each SETTINGS value must pass validate() below, or the
# UPS row of a reset is refused.
UPS_BASELINE = {
    "input.sensitivity": "medium",
    "battery.charge.low": 10,
    "battery.runtime.low": 120,
    "beeper": "disabled",
}


def describe(ups_block):
    """{"editable", "readonly", "beeper"} for GET /api/config.

    Never raises: an absent/unreadable ups_block just shows every value as
    None, same tri-state discipline as upsblock.build(). "block_key" names
    the matching field in the live snapshot's ups block, so the page can
    keep these values current from the stream without a second map.
    """
    ups_block = ups_block or {}
    editable = []
    for name, spec in SETTINGS.items():
        entry = {"name": name, "label": spec["label"], "type": spec["type"],
                 "unit": spec.get("unit"), "value": ups_block.get(spec["block_key"]),
                 "block_key": spec["block_key"], "help": spec["help"]}
        if spec["type"] == "enum":
            entry["choices"] = list(spec["choices"])
        else:
            entry["min"] = spec["min"]
            entry["max"] = spec["max"]
            entry["step"] = spec["step"]
        editable.append(entry)
    readonly = []
    for name, spec in READONLY.items():
        readonly.append({"name": name, "label": spec["label"],
                         "value": ups_block.get(spec["block_key"]),
                         "block_key": spec["block_key"],
                         "unit": spec.get("unit"), "help": spec["help"]})
    return {"editable": editable, "readonly": readonly,
            "beeper": ups_block.get("beeper")}


def validate(name, value):
    """Check name/value against SETTINGS. Returns (ok, value_or_detail).

    On ok True, the second element is the coerced, canonical value (e.g. a
    lowercased choice, or an int already snapped to a valid step). On ok
    False, it is a human-readable reason. Pure -- no I/O, safe to call
    speculatively before touching the wire.
    """
    spec = SETTINGS.get(name)
    if spec is None:
        return False, "unknown setting %r" % (name,)
    if spec["type"] == "enum":
        choices = spec["choices"]
        val = str(value).strip().lower()
        if val not in choices:
            return False, "%r must be one of %s" % (value, ", ".join(choices))
        return True, val
    if spec["type"] == "int":
        try:
            val = int(round(float(value)))
        except Exception:
            return False, "%r is not a number" % (value,)
        lo, hi, step = spec["min"], spec["max"], spec["step"]
        if not (lo <= val <= hi):
            return False, "%s must be between %d and %d" % (name, lo, hi)
        if (val - lo) % step != 0:
            return False, ("%s must be in steps of %d starting from %d"
                           % (name, step, lo))
        return True, val
    return False, "unsupported setting type %r" % (spec["type"],)


SLEEP_BEFORE_VERIFY = 3.0


def _norm(v):
    """Numeric-aware equality helper: "10" == "10.000000" but "low" != "high"."""
    try:
        return round(float(v), 3)
    except Exception:
        return str(v).strip().lower()


def apply_setting(name, value, reread, sleep=SLEEP_BEFORE_VERIFY):
    """Validate, write, then re-read to confirm the firmware kept it.

    `reread` is a callable taking no arguments and returning the freshly
    read raw value for `name` (or None if it could not be read) -- kept as
    an injected callable rather than an import of nut.py/upscmd.py here so
    this stays testable without a real NUT connection.

    Returns {"ok", "detail", "name", "value", "verified", "now"}, exactly
    the shape the ups-set HTTP endpoint reports. `ok` reflects whether the
    SET VAR was accepted, not whether it was verified -- the firmware may
    silently ignore a value, which is precisely what `verified` is for:
    True/False once re-read, None if the re-read itself failed.
    """
    ok, coerced = validate(name, value)
    if not ok:
        return {"ok": False, "detail": coerced, "name": name, "value": value,
                "verified": None, "now": None}
    ok, detail = upscmd.setvar(name, coerced)
    if not ok:
        return {"ok": False, "detail": detail, "name": name, "value": coerced,
                "verified": None, "now": None}
    if sleep:
        time.sleep(sleep)
    try:
        now = reread()
    except Exception:
        now = None
    verified = None if now is None else (_norm(now) == _norm(coerced))
    return {"ok": True, "detail": detail, "name": name, "value": coerced,
            "verified": verified, "now": now}


# ---- beeper -----------------------------------------------------------------

BEEPER_MODES = ("enable", "disable", "mute")


def beeper(mode):
    """beeper.<mode> instant command. Returns (ok, detail, http_code).

    Not interlocked at the HTTP layer (see actions.py) -- mute is needed
    exactly during an outage, which is exactly when the interlock would
    otherwise be held by an open episode.
    """
    if mode not in BEEPER_MODES:
        return False, "mode must be one of %s" % ", ".join(BEEPER_MODES), 400
    ok, detail = upscmd.instcmd("beeper.%s" % mode)
    return ok, detail, (200 if ok else 502)


# ---- self-test ----------------------------------------------------------

SELFTEST_MIN_CHARGE = 90
# Matches the "less than 90 s ago" window the snapshot contract gives
# snap["self_test"]["active"] for a dashboard-started test.
SELFTEST_ACTIVE_WINDOW = 90.0

_lock = threading.RLock()
_last_started = {"ts": None}


def selftest_started():
    """ts of the last dashboard-initiated self-test, or None.

    Module-level by design: the collector (owned by another agent) reads
    this every tick to decide snap["self_test"]["by_dashboard"]/"active"
    without upsops having to know anything about the snapshot shape.
    """
    with _lock:
        return _last_started["ts"]


def _dashboard_test_recent(now=None):
    ts = selftest_started()
    if ts is None:
        return False
    now = time.time() if now is None else now
    return (now - ts) < SELFTEST_ACTIVE_WINDOW


def selftest_start(ups_block, cut_state):
    """Start test.battery.start.quick. Returns (ok, detail, http_code).

    Refused with 409 unless: the UPS is readable, on-line (OL, not on
    battery), not OFF, charged to at least SELFTEST_MIN_CHARGE%, no output
    cut is pending (cut_state, i.e. upsoff.state(), inactive), and no
    self-test is already running -- either the UPS's own
    (ups_block["self_test"]) or one this dashboard started less than
    SELFTEST_ACTIVE_WINDOW seconds ago.

    WHY: a battery self-test briefly reports OL OFF / OL DISCHRG (NUT issue
    #2104) and the UPS also runs its own automatic self-tests; today that
    trips the critical UPS_OUTPUT_OFF alert. Refusing to START one from here
    unless conditions are already clean keeps a dashboard-triggered test
    from being the thing that causes a false alarm; states.py/collector.py
    (owned elsewhere) still need the persistence window for automatic ones.
    """
    ups_block = ups_block or {}
    if not ups_block.get("ok"):
        return False, "UPS is not readable", 409
    flags = ups_block.get("flags") or []
    if "OL" not in flags:
        return False, "UPS is not on-line (on battery)", 409
    if ups_block.get("output_off"):
        return False, "UPS output is OFF", 409
    charge = ups_block.get("charge")
    if charge is None or charge < SELFTEST_MIN_CHARGE:
        shown = charge if charge is not None else "?"
        return False, ("battery charge (%s%%) must be at least %d%%"
                       % (shown, SELFTEST_MIN_CHARGE)), 409
    if (cut_state or {}).get("active"):
        return False, "an output-cut sequence is pending or in progress", 409
    if ups_block.get("self_test") or _dashboard_test_recent():
        return False, "a self-test is already running", 409
    ok, detail = upscmd.instcmd("test.battery.start.quick")
    if ok:
        with _lock:
            _last_started["ts"] = time.time()
    return ok, detail, (200 if ok else 502)


def selftest_stop():
    """test.battery.stop. Returns (ok, detail, http_code)."""
    ok, detail = upscmd.instcmd("test.battery.stop")
    return ok, detail, (200 if ok else 502)
