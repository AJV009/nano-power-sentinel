"""Derived values: the drain trend and the three timeline projections.

The hibernate projection deliberately mirrors hibernate-governor's own rule, so
the timeline marks the moment the governor will ACTUALLY act on rather than an
independent guess that could disagree with it.
"""

DRAIN_WINDOW = 300.0
MARGIN_WINDOW = 180.0     # the margin moves in 12 s steps (pollfreq), so wide
MIN_MARGIN_RATE = 0.02    # s per s: below this, call it flat rather than guess


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


def _margin(runtime, charge, cost, tunables):
    """The governor's own margin: how much slack it has left before it must
    hibernate. It compares runtime-to-reserve against cost + safety."""
    if runtime is None or not charge or cost is None:
        return None
    to_reserve = runtime * (charge - tunables["reserve_pct"]) / charge
    return to_reserve - cost - tunables["safety_sec"]


def margin_rate(ring, now, margin_now, cost, tunables, window=MARGIN_WINDOW):
    """How fast the margin is shrinking, in seconds per second. None when
    there is not enough history.

    WHY THIS EXISTS: the margin was being reported directly as "hibernate in
    N s", which assumes it falls a second per second. It does not -- it is
    runtime x (charge - reserve) / charge, and the UPS's own runtime estimate
    moves as the load changes. On 2026-09-22 the margin sat near 99 s for
    minutes while the alert had already claimed "~48 s", so the push landed
    ~3 min early. Measuring the slope keeps the projection honest.
    """
    if margin_now is None:
        return None
    old = None
    for snap in ring:
        if snap["ts"] >= now - window:
            old = snap
            break
    if not old:
        return None
    dt = now - old["ts"]
    if dt < 30:
        return None
    ups = old.get("ups") or {}
    then = _margin(ups.get("runtime"), ups.get("charge"), cost, tunables)
    if then is None:
        return None
    return (then - margin_now) / dt


def hibernate_cost(ram_gb, tunables):
    if ram_gb is None:
        return None
    return round(ram_gb / tunables["write_rate_gbps"]
                 + tunables["fixed_overhead_sec"], 1)


def project(ups, box_state, charge, runtime, tunables, ram_gb, drain,
            down_cause=None, self_test=False, margin_slope=None):
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
            margin = _margin(runtime, charge, cost, tunables)
            if margin is not None:
                # The margin is what the governor compares; the ETA is when
                # that margin reaches zero at its OWN measured rate. Reporting
                # the margin as an ETA alerted ~3 min early (2026-09-22).
                out["hibernate_margin_sec"] = round(margin, 1)
                out["margin_rate_sec_per_sec"] = (
                    round(margin_slope, 3) if margin_slope is not None else None)
                if margin <= 0:
                    out["eta_hibernate_sec"] = 0
                elif margin_slope is not None and margin_slope > MIN_MARGIN_RATE:
                    out["eta_hibernate_sec"] = round(margin / margin_slope, 1)
                # else: shrinking too slowly to call -- leave it None rather
                # than publish a countdown that will not come true.
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
