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
"""

from . import states
from .notify_util import ep_suffix


def _state_text(st, curr):
    """The reused half of a message: states.py's own detail (falling back to
    its one-line sentence), plus its action line and the episode tag."""
    msg = st.get("detail") or st.get("sentence") or ""
    if st.get("action"):
        msg += " ACTION: %s" % st["action"]
    return msg + ep_suffix(curr)


def state_events(prev, curr, ts):
    """Six boundary crossings of `curr["state"]["state"]`. Each is checked
    independently (not elif) because they are mutually exclusive by
    construction in states.classify() -- curr can only ever equal one of
    them on a given tick -- so there is no ordering to get wrong here."""
    out = []
    p = (prev.get("state") or {}).get("state")
    c = (curr.get("state") or {}).get("state")
    if c == p:
        return out   # nothing crossed a state boundary this tick
    st = curr.get("state") or {}

    if c == states.OUTAGE_DOWN and p != states.OUTAGE_DOWN:
        out.append(("box_hibernated", st.get("short") or "Box hibernated",
                    _state_text(st, curr), "high", ["zzz"], ts))

    if c == states.MANUAL_DOWN and p != states.MANUAL_DOWN:
        # The distinguishing fact vs. box_hibernated above: a human did this
        # on purpose, so the sentinel will NOT auto-wake it -- already
        # spelled out in states.py's own detail text, reused verbatim.
        out.append(("box_down_manual", st.get("short") or "Box off (by you)",
                    _state_text(st, curr), "default",
                    ["raised_hand", "zzz"], ts))

    if c == states.OUTPUT_OFF and p != states.OUTPUT_OFF:
        # The new, critical case this rewrite exists for: no amount of
        # waiting or Wake-on-LAN brings this back, only a human at the box.
        out.append(("ups_output_off", st.get("short") or "UPS output OFF",
                    _state_text(st, curr), "max",
                    ["rotating_light", "no_entry"], ts))

    if p == states.OUTPUT_OFF and c != states.OUTPUT_OFF:
        out.append(("ups_output_restored", "UPS output restored",
                    "UPS output is back on -- the box has standby power "
                    "again. Current status: %s."
                    % (st.get("sentence") or st.get("short") or "?"),
                    "default", ["white_check_mark", "electric_plug"], ts))

    if c == states.RECOVERING and p != states.RECOVERING:
        out.append(("wake_gate_wait",
                    st.get("short") or "Waiting at wake gate",
                    _state_text(st, curr), "low", ["hourglass"], ts))

    if c == states.BOX_LOST and p != states.BOX_LOST:
        out.append(("box_lost", st.get("short") or "Box unreachable",
                    _state_text(st, curr), "high",
                    ["warning", "grey_question"], ts))

    # HIBERNATING became reachable after this file was first written: it now
    # covers both the governor acting on its own threshold and an emergency
    # shutdown sequence in flight. Worth a high-priority alert either way --
    # it is the moment the box is actually going down, and the emergency case
    # is still abortable for a few seconds.
    if c == states.HIBERNATING and p != states.HIBERNATING:
        out.append(("box_hibernating", st.get("short") or "Shutting down",
                    _state_text(st, curr), "high",
                    ["rotating_light", "computer"], ts))

    return out
