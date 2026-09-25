"""The single source of truth for what the system is doing.

WHY THIS MODULE EXISTS: the state vocabulary was previously implied in three
places -- derive.project(), the state-line text, and the notification rules --
and they disagreed. A manually powered-off box reported "mains back, waiting
for the 50% gate", because nothing tracked WHY the box was down. The sentinel
has always had this concept (`outage_seen`: only wake a box that went down
because of an outage); the dashboard did not.

Everything user-facing now derives from classify() so the text, the timeline
and the notifications cannot drift apart again.

THE TWO DIMENSIONS THAT MATTER:

    what the UPS is doing      OL / OB / OFF / unreadable
    why the box is down        outage / manual / emergency / unknown

`OFF` is the one people forget. After a load.off the UPS reports `OL OFF`:
mains present, battery full, output DE-ENERGISED. The box is not hibernating
and not unreachable -- it has no power at all, and no amount of waiting or
Wake-on-LAN will bring it back. Someone has to press the front-panel button --
bench-tested 2026-09-22 (docs/UPS-TOOLING.md §7): nothing software-side can.

...EXCEPT during a battery self-test. The test itself shows `OL OFF` for
~10 s and then `OL DISCHRG` (NUT #2104), which used to read as a dead output
and fire the most urgent alert this system has. SELF_TEST explains both OFF
and OB -- but never over an emergency cut of ours, and never over a UPS
shutdown timer that is counting: a real cut must not hide behind a test.
"""

# events.py is re-exported: ONE vocabulary (states, causes, event kinds), two
# files only for the 300-line rule. When the store and the notifier knew
# different event names, a manual shutdown was recorded and never alerted.
from .events import *        # noqa: F401,F403
from . import events         # noqa: F401  (for events.meta / events.CATALOG)
from .states_text import (_n, output_off_text, self_test_text, park_text,
                          park_return_text, park_no_power_text, recovering_text,
                          park_cycle_text, other_os_text)

# ---- why the box went away ------------------------------------------------
CAUSE_OUTAGE = "outage"
CAUSE_MANUAL_HIBERNATE = "manual_hibernate"
CAUSE_EMERGENCY_SAFE = "emergency_safe"
CAUSE_EMERGENCY_INSTANT = "emergency_instant"
CAUSE_UNKNOWN = "unknown"

MANUAL_CAUSES = (CAUSE_MANUAL_HIBERNATE, CAUSE_EMERGENCY_SAFE,
                 CAUSE_EMERGENCY_INSTANT)

CAUSE_LABEL = {
    CAUSE_OUTAGE: "a power outage",
    CAUSE_MANUAL_HIBERNATE: "you hibernated it",
    CAUSE_EMERGENCY_SAFE: "you ran a safe shutdown",
    CAUSE_EMERGENCY_INSTANT: "you cut power instantly",
    CAUSE_UNKNOWN: "an unknown reason",
}

# ---- system states --------------------------------------------------------
BLIND = "blind"
OUTPUT_OFF = "output_off"
ON_BATTERY = "on_battery"
HIBERNATING = "hibernating"
OUTAGE_DOWN = "outage_down"
RECOVERING = "recovering"
MANUAL_DOWN = "manual_down"
BOX_LOST = "box_lost"
SELF_TEST = "self_test"
NOMINAL = "nominal"
PARKED = "parked"
OTHER_OS = "other_os"      # up on another OS -- Windows (lanprobe.py)

# ---- the battery-floor park (park.py): marker phases and its outcomes -----
PARK_ARMED, PARK_PARKED, PARK_RETURNING = "armed", "parked", "returning"
PARK_NO_POWER_ON = "no_power_on"     # the box never powered itself back on
PARK_STUCK_PRE_OS = "stuck_pre_os"   # powered on, never reached its OS
PARK_LAN_NO_AGENT = "lan_no_agent"   # on the LAN, box-agent silent
PARK_OUTCOMES = (PARK_NO_POWER_ON, PARK_STUCK_PRE_OS, PARK_LAN_NO_AGENT)

# severity drives the accent colour and notification priority. SELF_TEST is
# "ok", not "nominal": something IS happening, it is just benign.
SEV = {
    BLIND: "unknown", OUTPUT_OFF: "critical", ON_BATTERY: "warn",
    HIBERNATING: "warn", OUTAGE_DOWN: "warn", RECOVERING: "ok",
    MANUAL_DOWN: "ok", BOX_LOST: "warn", SELF_TEST: "ok", NOMINAL: "nominal",
    PARKED: "warn", OTHER_OS: "ok",
}


def _num(v):
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def self_test_explains(self_test, ups_cut=None, ups=None):
    """True when a running (or unconfirmed) self-test is the explanation for
    OFF / OB, so neither may be reported as an outage or a dead output.

    The one shared answer: classify(), derive, episodes and the notifier all
    ask this, so the dashboard and the phone cannot disagree about a test.
    Never True over a cut of ours -- in flight, or fired since the test
    began (an instant cut is "done" before any tick sees it "active") -- nor
    while a UPS shutdown timer counts: that is a cut, whoever armed it.
    """
    st = self_test or {}
    if st.get("active") is not True and st.get("suspected") is not True:
        return False
    cut = ups_cut or {}
    if cut.get("active"):
        return False
    if cut.get("phase") in ("cutting", "done"):
        cut_at, began = cut.get("started"), st.get("started")
        # Unknown ordering counts against the test: a missed alert is worse
        # than a false one here.
        if not _num(began) or not _num(cut_at) or cut_at >= began:
            return False
    u = ups or {}
    t_sd, t_rb = u.get("timer_shutdown"), u.get("timer_reboot")
    if (_num(t_sd) and t_sd >= 0) or (_num(t_rb) and t_rb > 0):
        return False
    return True


def classify(ups, box_state, down_cause, tunables, episode=None,
             ups_cut=None, eta_hibernate_sec=None, wake_hold=None,
             self_test=None, park=None, sentinel=None):
    """Return the authoritative view of what is happening.

    Pure: no I/O, no clock reads beyond what is passed in. Everything the UI
    and the notifier say is built from this. `self_test` is the collector's
    snap["self_test"], `park` its snap["park"] (park.py), `sentinel` the
    sentinel's own facts (ledger.peer(): None unless its heartbeat is fresh).
    """
    ups = ups or {}
    tun = tunables or {}
    flags = ups.get("flags") or []
    charge = ups.get("charge")
    wake_at = tun.get("wake_charge_pct")
    # A self-test explains OFF and OB (see self_test_explains): with them
    # set aside, what is left is the true picture -- mains present, output
    # live. A box that was already down keeps its own state (re-entering it
    # after the test would re-fire its notification); an awake box shows
    # SELF_TEST where it would have shown NOMINAL.
    testing = self_test_explains(self_test, ups_cut, ups)
    if testing:
        flags = [f for f in flags if f not in ("OFF", "OB")]

    # 0. Parked. Outranks even BLIND -- but ONLY while the marker says we
    #    armed/parked: the UPS switching itself off (USB gone, "Data stale")
    #    is then EXPECTED. With no marker an unreadable UPS stays BLIND.
    pk = park or {}
    if pk.get("phase") in (PARK_ARMED, PARK_PARKED):
        return _mk(PARKED, *park_text(pk, ups.get("on_battery"), wake_at))
    # A power-cycle of a box stuck before its OS (park_stuck.py): the output
    # going OFF for seconds is ours, not a dead output.
    if pk.get("phase") == PARK_RETURNING and pk.get("cycling"):
        return _mk(PARKED, *park_cycle_text(pk))

    # 1. Cannot read the UPS. With no data we do not know whether mains is
    #    present, so we assert nothing and hold.
    if not ups.get("ok"):
        return _mk(BLIND,
                   "UPS unreadable",
                   "Cannot read the UPS — holding state",
                   "Absence of data is not evidence that mains returned. The "
                   "controllers keep whatever state they were already in.",
                   action="Check nut-driver and the USB link on the jetson.")

    # 2. Output de-energised. Dominant fact whenever true: nothing the software
    #    can do restores it, so say so plainly instead of implying a wait.
    if "OFF" in flags:
        why = CAUSE_LABEL.get(down_cause, CAUSE_LABEL[CAUSE_UNKNOWN])
        short, sentence, detail, action = output_off_text(
            why, "OB" in flags, _n(charge))
        return _mk(OUTPUT_OFF, short, sentence, detail, action=action)

    on_batt = ups.get("on_battery") is True and not testing

    # 2b. A park's return: the guard sending a box that powered itself on
    #     back to sleep, or still waiting for it to power on at all.
    awake = box_state == "awake"
    if pk.get("phase") == PARK_RETURNING and (awake or not pk.get("rehibernate_sent")):
        held = bool(wake_hold) or bool(pk.get("hold_at_park"))
        return _mk(HIBERNATING if awake else PARKED,
                   *park_return_text(awake, charge, wake_at, held, pk))

    # 3. Something is actively taking the box down RIGHT NOW. Worth its own
    #    state so the UI stops showing a countdown that has already elapsed.
    cut = ups_cut or {}
    if cut.get("active") and cut.get("phase") in ("hibernating", "waiting_for_draw"):
        detail = ("Waiting for its power draw to fall before cutting — "
                  "cutting a machine that is still writing its hibernate "
                  "image is what this sequence exists to avoid."
                  if cut.get("phase") == "waiting_for_draw"
                  else "Asking the box to hibernate.")
        return _mk(HIBERNATING, "Shutting down",
                   "Emergency shutdown in progress · %s"
                   % ("waiting for the draw to fall"
                      if cut.get("phase") == "waiting_for_draw"
                      else "hibernating the box"),
                   detail, action="Abort from the box controls if unintended.")

    # Up on another OS: nothing of ours runs there. On battery, nothing will
    # hibernate it -- critical, whatever SEV says for the calm case.
    if box_state == OTHER_OS:
        st = _mk(OTHER_OS, *other_os_text(on_batt, charge, ups.get("runtime")))
        return dict(st, severity="critical") if on_batt else st

    # After a park WoL cannot reach the box (no standby power): only its button.
    if pk.get("outcome") in PARK_OUTCOMES and box_state != "awake":
        return _mk(BOX_LOST, *park_no_power_text(pk))

    eta = eta_hibernate_sec    # box still up only: once down, "NOW" hid OUTAGE_DOWN
    if on_batt and box_state == "awake" and eta is not None and eta <= 0:
        return _mk(HIBERNATING, "Hibernating",
                   "HIBERNATING NOW · reserving %s%%" % _n(tun.get("reserve_pct")),
                   "The governor has reached its threshold and is writing the "
                   "hibernate image.", action=None)

    # 4. On battery, box still up.
    if on_batt and box_state == "awake":
        return _mk(ON_BATTERY,
                   "On battery",
                   "ON BATTERY · box awake",
                   "Running from the pack. The governor will hibernate the box "
                   "while enough charge remains to finish writing the image.",
                   action=None)

    # A human switched it off. This outranks "on battery": the outage did not
    # take this box down, and the sentinel will not wake it afterwards. The
    # previous order checked on_batt first and showed the outage state --
    # whose own text promised the box "will be woken once mains returns",
    # which the wake hold makes untrue.
    manual = (down_cause in MANUAL_CAUSES) or bool(wake_hold)
    if box_state != "awake" and manual:
        # The hold alone proves a human did it, even if the precise cause
        # was lost (a collector restart, say) -- never say "unknown" here.
        how = (CAUSE_LABEL[down_cause] if down_cause in MANUAL_CAUSES
               else "you switched it off")
        if on_batt:
            detail = ("Power is out and the pack is at %s%%, but that is not why "
                      "the box is off. It will stay off when mains returns -- "
                      "the sentinel does not undo a deliberate shutdown. Use "
                      "Wake when you want it back." % _n(charge))
        else:
            detail = ("Mains is fine and the pack is at %s%%. The sentinel will "
                      "NOT wake it automatically -- it only restores a box that "
                      "went down because of an outage. Use Wake when you want "
                      "it back." % _n(charge))
        return _mk(MANUAL_DOWN, "Box off (by you)",
                   "Box is down · %s%s" % (how, " · power out" if on_batt else ""),
                   detail, action="Wake (WoL) from the box controls.")

    # 4. On battery, box already down -> it hibernated for the outage.
    if on_batt:
        return _mk(OUTAGE_DOWN,
                   "Box hibernated",
                   "ON BATTERY · box hibernated",
                   "The box is safely hibernated and will be woken once mains "
                   "returns and the pack recovers to %s%%." % _n(wake_at),
                   action=None)

    # --- from here on: mains is present and the output is live ---

    if box_state == "awake":
        if testing:
            short, sentence, detail, action = self_test_text(
                (self_test or {}).get("active") is True)
            return _mk(SELF_TEST, short, sentence, detail, action=action)
        return _mk(NOMINAL, "All good", "On mains · box awake · sentinel idle",
                   None, action=None)

    # 5. The box is down while mains is fine. WHY it went down decides
    #    everything the user is told next.
    if down_cause in MANUAL_CAUSES:
        return _mk(MANUAL_DOWN,
                   "Box off (by you)",
                   "Box is down · %s" % CAUSE_LABEL[down_cause],
                   "Mains is fine and the pack is at %s%%. The sentinel will "
                   "NOT wake it automatically — it only restores a box that "
                   "went down because of an outage. Use Wake when you want it "
                   "back." % _n(charge),
                   action="Wake (WoL) from the box controls.")

    if down_cause == CAUSE_OUTAGE:
        short, sentence, detail, action = recovering_text(
            charge, wake_at, tun.get("mains_stable_sec"), sentinel)
        return _mk(RECOVERING, short, sentence, detail, action=action)

    # 6. It vanished and we did not do it and there was no outage.
    return _mk(BOX_LOST,
               "Box unreachable",
               "Box unreachable · no outage recorded",
               "Mains is fine and nothing here asked the box to go down, so "
               "this was not planned. It may have crashed, lost its network, "
               "or been shut down at the machine itself.",
               action="Try Wake (WoL); if that fails, check it physically.")


def _mk(state, short, sentence, detail, action=None):
    return {"state": state, "severity": SEV[state], "short": short,
            "sentence": sentence, "detail": detail, "action": action}
