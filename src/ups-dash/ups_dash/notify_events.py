"""Snapshot-to-notification transitions -- pure, no I/O, no state.

`transitions(prev, curr)` is the whole contract: feed it two collector
snapshots (see collector.py `_tick()` for the shape) and it returns the list
of (kind, title, message, priority, tags, ts) tuples worth telling a human
about.  Nothing else touches the network, the disk, or the clock (beyond
reading `curr["ts"]`), which is what makes this file testable with plain
synthetic dicts and safe to import from anywhere.  The states.py-vocabulary
half of the catalog lives in notify_events_states.py (split out for the
300-line cap, not a different lifecycle -- see that file's docstring).

THE RULE THAT MATTERS: notify on TRANSITIONS, not on STATE.  A 1 Hz loop
holds a state true for thousands of consecutive ticks; comparing prev vs curr
on each field means only the tick where the value actually changed produces
anything, and every other tick produces an empty list.  That is the entire
de-duplication strategy -- no timers, no "already sent" bookkeeping, just:
did this specific field (including `curr["state"]["state"]`) change since
the last look.

`prev is None` (nothing to compare against -- collector just started) always
returns [].  Otherwise every event this run has already lived through would
re-fire the moment the daemon restarts, which is exactly the kind of
mid-outage log spam this module exists to prevent.

THE BUG THIS REWRITE FIXES: the old catalog only knew about outage-driven
events (mains lost/restored, hibernate, box asleep/awake, wake gate, service
died).  A real incident showed the gap: an operator-triggered emergency
shutdown took the box down and cut the UPS output, and NOTHING was queued --
there was no notion of "box went down because a human did it" and no notion
of the UPS `OFF` flag at all.  `states.py` now carries the single vocabulary
for what is happening and why (`curr["state"]`, `curr["down_cause"]`); this
module's job is to notice when THAT vocabulary crosses a boundary, not to
re-derive it from raw fields.  Prefer `curr["state"]["sentence"]` /
`"detail"` / `"action"` over writing a second copy of the same explanation --
duplicating that text is exactly how the dashboard and the notifier used to
disagree (see states.py's own docstring).

`ups.on_battery` and `ups.low_battery` are True/False/None (nut.py,
upsblock.py) -- None means "could not read the UPS", a third state, NOT "on
mains" / "not low".  Every check below uses `is True` / `is False`, never
truthiness, so None never gets folded into either side of a transition.
Deliberately asymmetric where it matters: fire on a `None/False -> True`
edge (never miss a real event) but require a confirmed `True -> False` edge
to announce the all-clear (never falsely claim recovery).  This exact bug
already shipped once in this project for `on_battery`; the same asymmetry
now applies to `low_battery` and to service `healthy`, for the same reason.

SELF-TESTS: a battery test shows OL OFF / OL DISCHRG for seconds (NUT
#2104) and used to page as UPS OUTPUT OFF. states.classify() now answers
SELF_TEST for that, and `_mains()` treats an on-battery reading that
`notify_util.testing()` explains as not-on-battery -- so a test produces
neither "Mains lost" nor the matching "Mains restored", only its verdict.
"""

from . import events, states
from .notify_events_states import park_events, state_events
from .notify_util import (_g, _n, ep_suffix, park_phase, testing, ups_flag,
                          ups_line)

HIBERNATE_IMMINENT_SEC = 60.0
# hibernate-governor's own safety_sec is commonly ~30s; alerting a bit ahead
# of that means the push notification has a chance to land BEFORE the box
# goes dark, not arrive describing something already over.


def _on_battery(snap):
    """`ups.on_battery` with a running self-test's reading set aside --
    the test switches the load to the pack on purpose. Stays None when
    unreadable; a test never turns "unknown" into "on mains"."""
    ob = _g(snap, "ups", "on_battery")
    return False if (ob is True and testing(snap)) else ob


def _mains(prev, curr, ts):
    """`on_battery` edges. Independent of `curr["state"]` on purpose: an
    outage that starts while the box is already down (rare, but possible)
    still deserves this event even though the box-level state the same tick
    is OUTAGE_DOWN, not ON_BATTERY.

    Self-test readings are masked, not dropped: if the pack is still
    carrying the load once the test stops explaining it (a real outage that
    began mid-test), THAT tick is the edge and "Mains lost" fires then --
    late, never lost (the same reasoning as notify.py's DEFERRED, NOT
    DROPPED). And a test-masked stretch never earns a "Mains restored"."""
    out = []
    p_ob = _on_battery(prev)
    c_ob = _on_battery(curr)
    if p_ob is None and park_phase(prev) in (states.PARK_ARMED, states.PARK_PARKED):
        # A parked UPS is silent ON PURPOSE, and it only ever parks on
        # battery: its return with mains is the confirmed edge, not a guess.
        p_ob = True
    if c_ob is True and p_ob is not True:
        out.append((events.MAINS_LOST, "Mains lost",
                    "UPS switched to battery power. %s%s"
                    % (ups_line(curr), ep_suffix(curr)),
                    "high", ["warning", "battery"], ts))
    elif c_ob is False and p_ob is True:
        # Deliberately NOT `p_ob is not False` on the other side: only a
        # confirmed True -> confirmed False edge counts as "restored". A
        # None (unreadable) anywhere in this transition must never produce
        # this message -- see module docstring.
        out.append((events.MAINS_RESTORED, "Mains restored",
                    "UPS reports mains power is back%s. Charge %s%%."
                    % (_outage_was(curr), _n(_g(curr, "ups", "charge"))),
                    "default", ["white_check_mark", "electric_plug"], ts))
    return out


def _outage_was(curr):
    """` -- the outage was: <reason>` once snap["transfer"] can be believed
    (see upsextras.py: a blip shorter than pollfreq never got a fresh read,
    so its reason would describe the transfer before it)."""
    if _g(curr, "transfer", "trusted") is not True:
        return ""
    reason = _g(curr, "transfer", "reason")
    return " -- the outage was: %s" % reason if reason else ""


def _hibernate_imminent(prev, curr, ts):
    p_mode = _g(prev, "derived", "mode")
    c_mode = _g(curr, "derived", "mode")
    p_eta = _g(prev, "derived", "eta_hibernate_sec")
    c_eta = _g(curr, "derived", "eta_hibernate_sec")
    p_imminent = (p_mode == "battery" and isinstance(p_eta, (int, float))
                 and p_eta <= HIBERNATE_IMMINENT_SEC)
    c_imminent = (c_mode == "battery" and isinstance(c_eta, (int, float))
                 and c_eta <= HIBERNATE_IMMINENT_SEC)
    if c_imminent and not p_imminent:
        # Quote the governor's own margin and how fast it is closing, so the
        # projection can be judged rather than taken on faith: reporting the
        # margin ITSELF as the ETA sent this push ~3 min early on 2026-09-22.
        margin = _g(curr, "derived", "hibernate_margin_sec")
        rate = _g(curr, "derived", "margin_rate_sec_per_sec")
        detail = ""
        if isinstance(margin, (int, float)) and isinstance(rate, (int, float)):
            detail = (" The governor's margin is %ds and closing %.2f s per "
                      "second." % (int(margin), rate))
        return [(events.HIBERNATE_IMMINENT, "Hibernate imminent",
                 ("hibernate-governor is projected to hibernate the box in "
                  "~%ds.%s%s" % (max(0, int(c_eta)), detail, ep_suffix(curr))),
                 "max", ["rotating_light", "hourglass_flowing_sand"], ts)]
    return []


def _box_woke(prev, curr, ts):
    """Box agent responding again after being hibernated/unreachable -- the
    low-level "it's back" fact, independent of WHY it went away (that part
    is covered by the states.py-driven events in notify_events_states.py)."""
    p_state = _g(prev, "box", "state")
    c_state = _g(curr, "box", "state")
    if park_phase(curr) == states.PARK_RETURNING:
        return []   # powered itself on after a park; the guard's push says so
    if c_state == "awake" and p_state in ("hibernated", "unreachable"):
        # Not from "unknown": that is the collector's own startup state
        # before the first successful poll, and firing "woke" for that would
        # just mean "the daemon restarted", not "the box resumed".
        return [(events.BOX_WOKE, "Box woke",
                 "The box is back (state=awake).", "default",
                 ["computer"], ts)]
    return []


def _battery_flags(prev, curr, ts):
    """LB (low battery) and RB (replace battery) -- orthogonal to the
    states.py state machine (a unit can be LB while ON_BATTERY, or RB while
    otherwise NOMINAL), so these are read straight off the UPS fields
    rather than through `curr["state"]`."""
    out = []
    p_lb = _g(prev, "ups", "low_battery")
    c_lb = _g(curr, "ups", "low_battery")
    if c_lb is True and p_lb is not True:
        out.append((events.LOW_BATTERY, "LOW BATTERY",
                    "UPS reports LOW BATTERY -- %s The hibernate governor "
                    "should already be acting on this.%s"
                    % (ups_line(curr), ep_suffix(curr)),
                    "max", ["battery", "rotating_light"], ts))

    # RB is the tri-state ups.replace_battery (None while unreadable -- an
    # unreadable read reports flags=[], which must NOT read as "RB cleared").
    # Same edge as low_battery: fire on anything -> True.
    if (ups_flag(curr, "replace_battery", "RB") is True
            and ups_flag(prev, "replace_battery", "RB") is not True):
        out.append((events.REPLACE_BATTERY, "Replace battery",
                    "UPS reports RB (replace battery) -- battery voltage "
                    "%sV, charge %s%%. Rare, but runtime estimates go "
                    "unreliable until it's swapped.%s"
                    % (_n(_g(curr, "ups", "batt_v")),
                       _n(_g(curr, "ups", "charge")), ep_suffix(curr)),
                    "high", ["battery", "warning"], ts))
    return out


def _overload(prev, curr, ts):
    """OVER: the load is beyond what the UPS will carry. Critical on either
    supply -- on battery it can drop the output, on mains it can trip. Only
    a readable UPS can assert it (ups_flag is None otherwise)."""
    if not _g(curr, "ups", "ok"):
        return []
    if (ups_flag(curr, "overload", "OVER") is True
            and ups_flag(prev, "overload", "OVER") is not True):
        meta = events.meta(events.UPS_OVERLOAD)
        return [(events.UPS_OVERLOAD, meta["label"],
                 "UPS reports OVERLOAD -- load %s%% (~%s W). It may cut its "
                 "output to protect itself; shed load now.%s"
                 % (_n(_g(curr, "ups", "load_pct")), _n(_g(curr, "ups", "watts")),
                    ep_suffix(curr)),
                 meta["priority"], meta["tags"], ts)]
    return []


_VERDICT_TEXT = {
    events.SELF_TEST_PASSED: "UPS battery self-test passed",
    events.SELF_TEST_WARNING: ("UPS battery self-test ended with a warning or "
                               "was aborted -- run it again later; a repeat "
                               "points at an ageing pack"),
    events.SELF_TEST_FAILED: ("UPS battery self-test FAILED -- the pack may "
                              "not carry the load in an outage; plan a "
                              "replacement"),
}


def _self_test_result(prev, curr, ts):
    """The verdict of a self-test, on the "In progress" -> final edge of
    ups.test_result (events.self_test_verdict). One push per test; the start
    is routine and silent."""
    result = _g(curr, "ups", "test_result")
    kind = events.self_test_verdict(_g(prev, "ups", "test_result"), result)
    if not kind:
        return []
    meta = events.meta(kind)
    return [(kind, meta["label"],
             "%s (UPS says: %s). Battery %sV, charge %s%%.%s"
             % (_VERDICT_TEXT[kind], result, _n(_g(curr, "ups", "batt_v")),
                _n(_g(curr, "ups", "charge")), ep_suffix(curr)),
             meta["priority"], meta["tags"], ts)]


def _services(prev, curr, ts):
    """One event per unit that just left healthy, not one big summary --
    each unit failing is independently actionable ("restart nut-server" is a
    different fix than "restart ups-sentinel").

    Keys off `healthy`, never `active`: a Type=oneshot unit reads
    ActiveState=inactive once it has run and succeeded -- that is correct,
    not a failure. Treating bare `active` as the health signal makes every
    oneshot unit a permanent false alarm the moment it finishes its one job.
    """
    out = []
    p_svc = prev.get("services")
    c_svc = curr.get("services")
    if not isinstance(p_svc, dict) or not isinstance(c_svc, dict):
        return out
    for unit, c_entry in c_svc.items():
        p_entry = p_svc.get(unit)
        if not isinstance(p_entry, dict) or not isinstance(c_entry, dict):
            continue
        if p_entry.get("healthy") is True and c_entry.get("healthy") is False:
            out.append((events.SERVICE_DIED, "Power-chain service died",
                        ("systemd unit %s is unhealthy (active=%s, "
                         "result=%s)."
                         % (unit, c_entry.get("active"), c_entry.get("result"))),
                        "max", ["skull", "rotating_light"], ts))
    return out


def transitions(prev, curr):
    """Return the list of notification tuples for the prev -> curr edge.

    Each tuple is (kind, title, message, priority, tags, ts) -- exactly the
    positional arguments Notifier.notify() takes, so callers can just do
    `notifier.notify(*item)` for each one returned here.
    """
    if not isinstance(prev, dict) or not isinstance(curr, dict):
        return []
    ts = curr.get("ts") or 0
    try:
        out = []
        out += _mains(prev, curr, ts)
        out += _hibernate_imminent(prev, curr, ts)
        out += _box_woke(prev, curr, ts)
        out += state_events(prev, curr, ts)
        out += park_events(prev, curr, ts)
        out += _battery_flags(prev, curr, ts)
        out += _overload(prev, curr, ts)
        out += _self_test_result(prev, curr, ts)
        out += _services(prev, curr, ts)
        return out
    except Exception:
        # Pure function, no caller expects an exception; a malformed
        # snapshot should mean "nothing to report", never a crash.
        return []
