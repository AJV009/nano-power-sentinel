"""The wake hold: "a human put this box down, do not auto-wake it."

WHY IT EXISTS: ups-sentinel wakes the box after an outage. Without this, a box
you deliberately hibernated gets woken anyway the next time power flaps --
which is exactly what happened on 2026-09-21: hibernated by hand at 19:05, an
outage at 20:30, and the sentinel woke it at 21:06 when mains returned.

"Only wake a box that was up when the outage began" fixes the case where you
hibernate BEFORE an outage. It cannot fix hibernating DURING one -- the box
was up when mains failed, so it looks like an outage casualty. This file is
what distinguishes the two.

DIVISION OF LABOUR, deliberately lopsided:
  ups-dash  owns the lifecycle -- writes the hold on Hibernate / emergency
            shutdown, clears it on Wake or when the box is seen coming back
  sentinel  only READS it, plus a stale-hold safety valve

That keeps the change to the safety-critical process to a single read, and
puts the state tracking in the service that already watches the box every
five seconds regardless of mains.

The file lives on persistent storage, not /run: if the jetson reboots during
an outage, a hold in tmpfs would vanish and the box would be woken against
the instruction it was given.
"""

import json
import os
import time

from . import settings

PATH = settings.WAKE_HOLD


def set_hold(reason, now=None):
    """Place the hold. Never raises."""
    try:
        tmp = PATH + ".tmp"
        with open(tmp, "w") as fh:
            json.dump({"ts": now or time.time(), "reason": reason}, fh)
        os.replace(tmp, PATH)       # atomic: the sentinel may read at any moment
        return True
    except Exception:
        return False


def clear_hold():
    try:
        os.remove(PATH)
        return True
    except FileNotFoundError:
        return False
    except Exception:
        return False


def get_hold():
    """Returns {"ts", "reason"} or None."""
    try:
        with open(PATH) as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else {"ts": None, "reason": "?"}
    except FileNotFoundError:
        return None
    except Exception:
        # Present but unreadable still means somebody asked for a hold.
        return {"ts": None, "reason": "unreadable hold file"}
