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
Wake-on-LAN will bring it back. Someone has to press the front-panel button.
"""

# The complete EVENT vocabulary lives in events.py and is re-exported here on
# purpose: callers import one module and get the whole language -- states,
# causes and event kinds. Keeping them in separate files is only the 300-line
# rule; conceptually this is one vocabulary and must stay that way, because
# the last time the store and the notifier knew different event names, a
# manual shutdown got recorded and never alerted.
from .events import *        # noqa: F401,F403
from . import events         # noqa: F401  (for events.meta / events.CATALOG)

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
NOMINAL = "nominal"

# severity drives the accent colour and notification priority
SEV = {
    BLIND: "unknown", OUTPUT_OFF: "critical", ON_BATTERY: "warn",
    HIBERNATING: "warn", OUTAGE_DOWN: "warn", RECOVERING: "ok",
    MANUAL_DOWN: "ok", BOX_LOST: "warn", NOMINAL: "nominal",
}


def _fmt_secs(sec):
    if sec is None:
        return "?"
    sec = int(max(0, sec))
    if sec < 90:
        return "%ds" % sec
    m = sec // 60
    return "%dm" % m if m < 90 else "%dh %02dm" % (m // 60, m % 60)


def classify(ups, box_state, down_cause, tunables, episode=None,
             ups_cut=None, eta_hibernate_sec=None):
    """Return the authoritative view of what is happening.

    Pure: no I/O, no clock reads beyond what is passed in. Everything the UI
    and the notifier say is built from this.
    """
    ups = ups or {}
    tun = tunables or {}
    flags = ups.get("flags") or []
    charge = ups.get("charge")
    wake_at = tun.get("wake_charge_pct")

    # 1. Cannot read the UPS. This outranks everything: with no data we do not
    #    know whether mains is present, so we assert nothing and hold.
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
        mains = "mains is present" if "OB" not in flags else "still on battery"
        return _mk(OUTPUT_OFF,
                   "UPS output OFF",
                   "UPS output is OFF · box has no power",
                   "The UPS is de-energised because %s. %s and the battery is "
                   "at %s%%, but the output stays off until somebody presses "
                   "the front-panel button — Wake-on-LAN cannot help, the "
                   "box has no standby power."
                   % (why, mains.capitalize(), _n(charge)),
                   action="Press the UPS front-panel power button to restore.")

    on_batt = ups.get("on_battery") is True

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

    if on_batt and eta_hibernate_sec is not None and eta_hibernate_sec <= 0:
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
        if charge is not None and wake_at is not None and charge < wake_at:
            return _mk(RECOVERING,
                       "Recharging",
                       "Mains back · box asleep · waiting for the %s%% gate"
                       % _n(wake_at),
                       "The pack is at %s%% and must reach %s%%, and mains must "
                       "hold %s uninterrupted, before the box is woken."
                       % (_n(charge), _n(wake_at), _fmt_secs(tun.get("mains_stable_sec"))),
                       action=None)
        return _mk(RECOVERING,
                   "Wake pending",
                   "Mains back · charge is past the %s%% gate" % _n(wake_at),
                   "The pack has recovered. The sentinel wakes the box once "
                   "mains has held %s uninterrupted."
                   % _fmt_secs(tun.get("mains_stable_sec")),
                   action=None)

    # 6. It vanished and we did not do it and there was no outage.
    return _mk(BOX_LOST,
               "Box unreachable",
               "Box unreachable · no outage recorded",
               "Mains is fine and nothing here asked the box to go down, so "
               "this was not planned. It may have crashed, lost its network, "
               "or been shut down at the machine itself.",
               action="Try Wake (WoL); if that fails, check it physically.")


def _n(v):
    return "?" if v is None else ("%g" % v)


def _mk(state, short, sentence, detail, action=None):
    return {"state": state, "severity": SEV[state], "short": short,
            "sentence": sentence, "detail": detail, "action": action}
