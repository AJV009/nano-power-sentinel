"""The wake hold: "a human put this box down, do not auto-wake it."

WHY IT EXISTS: ups-sentinel wakes the box after an outage. Without this, a box
you deliberately hibernated gets woken anyway the next time power flaps --
which is exactly what happened on 2026-09-21: hibernated by hand at 19:05, an
outage at 20:30, and the sentinel woke it at 21:06 when mains returned.

"Only wake a box that was up when the outage began" fixes the case where you
hibernate BEFORE an outage. It cannot fix hibernating DURING one -- the box
was up when mains failed, so it looks like an outage casualty. The hold is
what distinguishes the two.

DIVISION OF LABOUR, deliberately lopsided:
  ups-dash  owns the lifecycle -- writes the hold on Hibernate / emergency
            shutdown, clears it on Wake, when the box is seen coming back,
            or once it is stale (ledger_tick.py)
  sentinel  only READS it; a stale one it ignores, never deletes

That keeps the change to the safety-critical process to a single read, and
puts the state tracking in the service that already watches the box every
five seconds regardless of mains.

WHERE IT LIVES: the "hold" key of the state ledger (ledger.py, dash.json),
{"ts", "reason"} or null -- persistent storage, not /run: if the jetson
reboots during an outage, a hold in tmpfs would vanish and the box would be
woken against the instruction it was given. This module is only the facade
its callers (actions.py, upsoff.py, cause.py, the collector) always used.
"""

import time

from . import ledger

KEY = "hold"


def set_hold(reason, now=None):
    """Place the hold. True once it is on disk; a failed write is kept and
    retried by the ledger. Never raises."""
    return ledger.default().set(KEY, {"ts": now or time.time(), "reason": reason})


def clear_hold(expect=None):
    """Lift the hold -- when `expect` is given, only if it is still that one
    (a check that judged one hold must not lift a newer one placed from the
    HTTP thread meanwhile). True once it is gone from disk; False when there
    was none (or the write failed -- the ledger retries it). Never raises."""
    led = ledger.default()
    with led.lock:
        current = get_hold()
        if current is None or (expect is not None and current != expect):
            return False
        return led.set(KEY, None)


def get_hold():
    """Returns {"ts", "reason"} or None."""
    data = ledger.default().get(KEY)
    if data is None:
        return None
    # Present but unreadable still means somebody asked for a hold (the
    # ledger folds an unreadable legacy wake-hold file in exactly so).
    return data if isinstance(data, dict) else dict(ledger.UNREADABLE_HOLD)
