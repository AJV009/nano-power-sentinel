"""Derived values: the drain trend and the three timeline projections.

The hibernate projection deliberately mirrors hibernate-governor's own rule, so
the timeline marks the moment the governor will ACTUALLY act on rather than an
independent guess that could disagree with it.
"""

DRAIN_WINDOW = 300.0


def drain_rate(ring, now, charge, window=DRAIN_WINDOW):
    """%/min. Positive = draining, negative = charging, None = not enough history.

    battery.charge is an integer percent, so over a few seconds the delta reads
    as a flat zero -- hence the wide window.
    """
    if charge is None:
        return None
    oldest = None
    for snap in ring:
        if snap["ts"] >= now - window:
            oldest = snap
            break
    if not oldest or oldest.get("charge") is None:
        return None
    dt_min = (now - oldest["ts"]) / 60.0
    if dt_min < 0.5:
        return None
    return round((oldest["charge"] - charge) / dt_min, 3)


def hibernate_cost(ram_gb, tunables):
    if ram_gb is None:
        return None
    return round(ram_gb / tunables["write_rate_gbps"]
                 + tunables["fixed_overhead_sec"], 1)


def project(ups, box_state, charge, runtime, tunables, ram_gb, drain,
            down_cause=None, self_test=False):
    """Numbers only. The NARRATIVE lives in states.classify().

    `mode` here is kept for the timeline's three visual modes, but it must not
    claim "recovering" unless the box is genuinely waiting to be auto-woken.
    A manually shut-down box, or one whose UPS output has been cut, is never
    going to be woken by the sentinel -- reporting a countdown there is a lie.

    `self_test` is states.self_test_explains(): while True, the OFF / OB a
    battery test shows is not an output cut or an outage, so the timeline
    must not say "needs the front-panel button" or start a hibernate
    countdown beside a state line that says "Self-test".
    """
    out = {"mode": "mains", "drain_pct_min": drain, "eta_empty_sec": None,
           "eta_hibernate_sec": None, "eta_wake_sec": None,
           "hibernate_cost_sec": hibernate_cost(ram_gb, tunables)}

    # Output de-energised: no countdown of any kind is meaningful.
    if not self_test and "OFF" in (ups.get("flags") or []):
        out["mode"] = "output_off"
        return out

    if ups.get("on_battery") and not self_test:
        out["mode"] = "battery"
        out["eta_empty_sec"] = runtime
        if runtime is not None and charge:
            to_reserve = runtime * (charge - tunables["reserve_pct"]) / charge
            out["runtime_to_reserve_sec"] = round(to_reserve, 1)
            cost = out["hibernate_cost_sec"]
            if cost is not None:
                out["eta_hibernate_sec"] = round(
                    to_reserve - cost - tunables["safety_sec"], 1)
    elif (box_state in ("hibernated", "unreachable") and charge is not None
          and down_cause == "outage"):
        # Only an OUTAGE-caused absence recovers automatically. The sentinel
        # refuses to wake a box it did not put to sleep, so anything else
        # must not display a wake countdown.
        out["mode"] = "recovering"
        gap = tunables["wake_charge_pct"] - charge
        if gap <= 0:
            out["eta_wake_sec"] = 0
        elif drain is not None and drain < -0.01:
            out["eta_wake_sec"] = round(gap / (-drain) * 60.0, 1)
    elif box_state != "awake":
        # Down, but not for a reason that auto-recovers.
        out["mode"] = "down"
    return out
