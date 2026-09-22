"""Translate raw NUT variables into the dashboard's UPS schema.

The one rule that matters here: when the read failed, `ok` is False and every
derived boolean is None -- NOT False.  "Cannot read the sensor" is a third
state.  Folding it into "we're fine" is exactly the bug that shipped in
ups-sentinel once, where on_battery(None) was False and a dead sensor read as
mains-present (NOTES.md 2026-09-20).
"""

from . import nut

DEFAULT_NOMINAL_W = 865.0


def build(raw):
    ok = raw is not None
    flags = nut.status_flags(raw)
    load_pct = nut.as_int(raw, "ups.load")
    nominal = nut.as_float(raw, "ups.realpower.nominal") or DEFAULT_NOMINAL_W
    test_result = (raw or {}).get("ups.test.result")
    return {
        "ok": ok,
        "status": (raw or {}).get("ups.status"),
        "flags": flags,
        "on_battery": ("OB" in flags) if ok else None,
        "charging": ("CHRG" in flags) if ok else None,
        "low_battery": ("LB" in flags) if ok else None,
        "output_off": ("OFF" in flags) if ok else None,
        "replace_battery": ("RB" in flags) if ok else None,
        "overload": ("OVER" in flags) if ok else None,
        "charge": nut.as_float(raw, "battery.charge"),
        "runtime": nut.as_int(raw, "battery.runtime"),
        "batt_v": nut.as_float(raw, "battery.voltage"),
        "load_pct": load_pct,
        # There is no live ups.realpower on this unit, so watts are DERIVED
        # from an integer percent and quantise in `quantum_w` steps.  Never
        # render more precision than that.
        "watts": round(load_pct * nominal / 100.0, 1) if load_pct is not None else None,
        "quantum_w": round(nominal / 100.0, 2),
        "nominal_w": nominal,
        "input_v": nut.as_float(raw, "input.voltage"),
        "transfer_low": nut.as_float(raw, "input.transfer.low"),
        "transfer_high": nut.as_float(raw, "input.transfer.high"),
        "batt_mfr_date": (raw or {}).get("battery.mfr.date"),
        "test_result": test_result,
        "model": (raw or {}).get("device.model"),
        # -- added for the settings / self-test / transfer-cause features --
        "transfer_reason": (raw or {}).get("input.transfer.reason"),
        "sensitivity": (raw or {}).get("input.sensitivity"),
        "beeper": (raw or {}).get("ups.beeper.status"),
        "timer_shutdown": nut.as_int(raw, "ups.timer.shutdown"),
        "timer_reboot": nut.as_int(raw, "ups.timer.reboot"),
        "lb_charge": nut.as_float(raw, "battery.charge.low"),
        "lb_runtime": nut.as_int(raw, "battery.runtime.low"),
        # "In progress" while a self-test (manual or automatic) is running;
        # briefly true during the OL OFF / OL DISCHRG blip NUT issue #2104
        # describes -- callers must not treat that blip alone as OUTPUT_OFF.
        "self_test": ("progress" in (test_result or "").lower()) if ok else None,
        "driver_version": (raw or {}).get("driver.version"),
        "pollfreq": nut.as_int(raw, "driver.parameter.pollfreq"),
        "pollinterval": nut.as_int(raw, "driver.parameter.pollinterval"),
    }
