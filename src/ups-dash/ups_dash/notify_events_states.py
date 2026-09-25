"""The states.py-vocabulary half of the notification catalog.

Split out of notify_events.py purely for the 300-line cap; `transitions()`
in notify_events.py calls straight into `state_events()` below as part of
the same pass over one prev/curr edge -- there is no separate lifecycle,
no separate testing surface, this is exactly as "the same module" as
_mains() or _services() are to each other.

WHY THIS FILE EXISTS AT ALL: `curr["state"]["state"]` (states.classify()'s
output) is the single, authoritative answer to "what is the system doing
right now and why" -- see states.py's own docstring for the incident that
made that necessary (a manually powered-off box reporting "waiting for the
50% gate" because nothing tracked WHY it was down). Re-deriving that same
judgement here from on_battery/box_state/down_cause combinations would
reintroduce exactly the drift states.py exists to end. So every check below
keys off `curr["state"]["state"]` crossing a boundary, and every message
reuses `curr["state"]["sentence"]` / `"detail"` / `"action"` rather than
writing a second copy of the same explanation.

SELF_TEST has no alert of its own on purpose (events.SELF_TEST_STARTED is
notify=False): entering it is routine, and leaving it is either a return to
NOMINAL (nothing to say) or a hand-over to a real state whose own boundary
below fires as usual -- OUTPUT_OFF still pages if the output stays off after
the test stops explaining it. Every kind emitted here is an events.CATALOG
key, so the phone and HISTORY name an occurrence the same way.

THE PARK (park.py) speaks through the same boundaries. Entering PARKED is
the headline ("Parking the UPS"); while PARKED neither OUTPUT_OFF nor the
UPS going unreadable can surface -- classify() ranks PARKED above both,
because a parked UPS cuts its output and switches itself off on purpose.
The guard's HIBERNATING is "back to sleep", not an alarm, and BOX_LOST with
the no-power-on outcome is "press its power button". park_events() below
carries the two park facts that are not state boundaries.
"""

from . import events, states
from .notify_util import _g, _n, ep_suffix, park_phase


def _state_text(st, curr):
    """The reused half of a message: states.py's own detail (falling back to
    its one-line sentence), plus its action line and the episode tag."""
    msg = st.get("detail") or st.get("sentence") or ""
    if st.get("action"):
        msg += " ACTION: %s" % st["action"]
    return msg + ep_suffix(curr)


def _meta_item(kind, st, curr, ts):
    """A push whose title, priority and tags come from events.CATALOG."""
    meta = events.meta(kind)
    return (kind, meta["label"], _state_text(st, curr), meta["priority"],
            meta["tags"], ts)


def state_events(prev, curr, ts):
    """The boundary crossings of `curr["state"]["state"]`. Each is checked
    independently (not elif) because they are mutually exclusive by
    construction in states.classify() -- curr can only ever equal one of
    them on a given tick -- so there is no ordering to get wrong here."""
    out = []
    p = (prev.get("state") or {}).get("state")
    c = (curr.get("state") or {}).get("state")
    st = curr.get("state") or {}
    # On another OS nothing hibernates the box: an outage there is the one
    # case this system cannot handle by itself. Booting Windows is not news.
    # A severity edge inside one state, so it comes before the early return.
    if (c == states.OTHER_OS and st.get("severity") == "critical"
            and _g(prev, "state", "severity") != "critical"):
        out.append(_meta_item(events.BOX_OTHER_OS_ON_BATTERY, st, curr, ts))
    if c == p:
        return out   # nothing crossed a state boundary this tick

    # The park's headline. Its later phases stay PARKED, so they are silent.
    if c == states.PARKED and park_phase(curr) == states.PARK_ARMED:
        out.append(_meta_item(events.UPS_PARK_ARMED, st, curr, ts))

    # A box that was down -- and announced -- before the park is not news
    # when the park ends early: no second "hibernated" push after PARKED.
    was_parked = p == states.PARKED

    if c == states.OUTAGE_DOWN and p != states.OUTAGE_DOWN and not was_parked:
        out.append((events.BOX_HIBERNATED_OUTAGE,
                    st.get("short") or "Box hibernated",
                    _state_text(st, curr), "high", ["zzz"], ts))

    if c == states.MANUAL_DOWN and p != states.MANUAL_DOWN and not was_parked:
        # The distinguishing fact vs. box_hibernated above: a human did this
        # on purpose, so the sentinel will NOT auto-wake it -- already
        # spelled out in states.py's own detail text, reused verbatim.
        out.append((events.BOX_HIBERNATED_MANUAL,
                    st.get("short") or "Box off (by you)",
                    _state_text(st, curr), "default",
                    ["raised_hand", "zzz"], ts))

    if c == states.OUTPUT_OFF and p != states.OUTPUT_OFF:
        # The new, critical case this rewrite exists for: no amount of
        # waiting or Wake-on-LAN brings this back -- only a human at the box,
        # front-panel button (bench-tested, docs/UPS-TOOLING.md §7). A
        # self-test's OFF blip never gets here: classify() answers SELF_TEST
        # for it.
        out.append((events.UPS_OUTPUT_OFF,
                    st.get("short") or "UPS output OFF",
                    _state_text(st, curr), "max",
                    ["rotating_light", "no_entry"], ts))

    # Not into BLIND: "cannot read the UPS" is not evidence the output came
    # back -- the same confirmed-edge rule as mains_restored.
    if p == states.OUTPUT_OFF and c not in (states.OUTPUT_OFF, states.BLIND,
                                            states.PARKED):
        out.append((events.UPS_OUTPUT_RESTORED, "UPS output restored",
                    "UPS output is back on -- the box has standby power "
                    "again. Current status: %s."
                    % (st.get("sentence") or st.get("short") or "?"),
                    "default", ["white_check_mark", "electric_plug"], ts))

    if c == states.RECOVERING and p != states.RECOVERING:
        out.append((events.WAKE_GATE_WAIT,
                    st.get("short") or "Waiting at wake gate",
                    _state_text(st, curr), "low", ["hourglass"], ts))

    outcome = _g(curr, "park", "outcome")
    if (c == states.BOX_LOST and p != states.BOX_LOST
            and outcome in states.PARK_OUTCOMES):
        # A park's end with the box not back: the three ways it can happen
        # need three different things from a human (park_stuck.py).
        out.append(_meta_item(events.PARK_NO_POWER_ON
                              if outcome == states.PARK_NO_POWER_ON
                              else outcome, st, curr, ts))
    elif c == states.BOX_LOST and p != states.BOX_LOST:
        out.append((events.BOX_LOST, st.get("short") or "Box unreachable",
                    _state_text(st, curr), "high",
                    ["warning", "grey_question"], ts))

    # HIBERNATING became reachable after this file was first written: it now
    # covers both the governor acting on its own threshold and an emergency
    # shutdown sequence in flight. Worth a high-priority alert either way --
    # it is the moment the box is actually going down, and the emergency case
    # is still abortable for a few seconds.
    # ...except the park guard's: the box powered itself on with the mains
    # and is going back to sleep until the gate opens -- routine, not alarm.
    # Nor straight after PARKED with no guard: below the reserve classify()
    # answers "HIBERNATING NOW" for a box that is already down.
    if (c == states.HIBERNATING and p != states.HIBERNATING
            and park_phase(curr) == states.PARK_RETURNING):
        out.append(_meta_item(events.BOX_REHIBERNATED, st, curr, ts))
    elif c == states.HIBERNATING and p != states.HIBERNATING and not was_parked:
        out.append((events.BOX_HIBERNATING,
                    st.get("short") or "Shutting down",
                    _state_text(st, curr), "high",
                    ["rotating_light", "computer"], ts))

    return out


_FAILED_TEXT = {
    "arm": "The UPS refused the park command, so the pack keeps draining "
           "through its idle inverter. Retried at most every 5 min.",
    "cut": "The UPS accepted the park but never cut its output, so the pack "
           "keeps draining. Retried at most every 5 min.",
    "rehibernate": "The box powered itself on after the park and would not "
                   "go back to sleep. It is up at a low charge; the governor "
                   "hibernates it at the next outage.",
    "marker": "The park marker could not be kept on disk, so the box was "
              "left up rather than put to sleep with no wake coming.",
    "cycle": "The box was stuck before its OS after the park, and the UPS "
             "would not power-cycle it. It needs its power button.",
}


def park_events(prev, curr, ts):
    """The park facts that are not state boundaries: the park was skipped
    because the box was still up (the governor is not doing its job), a park
    step failed, and a stuck box is being power-cycled. Each is stamped once
    by park.py (`skipped_at`, `failed_at`, `cycled_at`) and pushed on the
    stamp's edge."""
    out = []
    for kind, key in ((events.UPS_PARK_SKIPPED, "skipped_at"),
                      (events.PARK_FAILED, "failed_at"),
                      (events.BOX_POWER_CYCLED, "cycled_at")):
        stamp = _g(curr, "park", key)
        if stamp is None or stamp == _g(prev, "park", key):
            continue
        meta = events.meta(kind)
        if kind == events.BOX_POWER_CYCLED:
            msg = ("The box powered on with the mains after the park but has "
                   "not reached its OS or the network in 7 minutes, so the UPS "
                   "is power-cycling it (try %s of %s). Its hibernate image is "
                   "untouched." % (_n(_g(curr, "park", "cycles")),
                                   _n(_g(curr, "park", "max_cycles"))))
        elif kind == events.UPS_PARK_SKIPPED:
            msg = ("The pack is at or below the %s%% park floor and under the "
                   "governor's reserve, but the box is still up -- it would be "
                   "hard-cut, so the UPS was not parked. The hibernate "
                   "governor should have put it to sleep by now."
                   % _n(_g(curr, "park", "floor_pct")))
        else:
            stage = _g(curr, "park", "failed_stage")
            msg = "%s (%s)" % (_FAILED_TEXT.get(stage, "A park step failed."),
                               _g(curr, "park", "failed_detail"))
        out.append((kind, meta["label"], msg + ep_suffix(curr),
                    meta["priority"], meta["tags"], ts))
    return out
