"""Tracks WHY the box is down.

THE GAP THIS FILLS: a hibernated box and a manually powered-off box look
identical from the outside -- the agent stops answering, and that is all you
see. Without a cause the dashboard guessed "outage recovery" for everything,
so a deliberate shutdown was reported as "mains back, waiting for the 50%
gate" while the UPS output was actually dead.

ups-sentinel has always had this idea: it tracks `outage_seen` and refuses to
wake a box that went down for a reason it did not cause. This is the same
discipline on the dashboard side.

HOW ATTRIBUTION WORKS: a control action declares an INTENT before the box
disappears. If the box then goes away shortly afterwards, that intent becomes
the cause. If it goes away with no intent on record, the UPS decides: on
battery means the outage took it; otherwise the cause is genuinely unknown,
which is itself worth saying out loud rather than papering over.
"""

import time

from .states import (CAUSE_OUTAGE, CAUSE_UNKNOWN, CAUSE_EMERGENCY_SAFE,
                     CAUSE_EMERGENCY_INSTANT, CAUSE_MANUAL_HIBERNATE,
                     MANUAL_CAUSES)
from .states import (EMERGENCY_INSTANT_CUT as EV_INSTANT_CUT,
                     EMERGENCY_SAFE_CUT as EV_SAFE_CUT,
                     EMERGENCY_SAFE_STARTED as EV_SAFE_STARTED,
                     MANUAL_HIBERNATE as EV_MANUAL_HIBERNATE)

# How long a declared intent stays valid. A hibernate can take a while to
# actually drop the box (image write, then power-off), but an intent from an
# hour ago must not explain an unrelated disappearance later.
INTENT_TTL = 420.0


class CauseTracker(object):
    def __init__(self):
        self.cause = None          # set only while the box is down
        self._intent = None
        self._intent_at = 0.0
        self._was_awake = None

    def recover_from_store(self, store, now=None, window=21600.0):
        """Re-learn the cause after a restart.

        Without this, restarting the collector turns "you cut the power ten
        minutes ago" into "an unknown reason" -- the knowledge was only ever
        in memory while the evidence was sitting in the event log the whole
        time. Looks back a few hours; older than that and whatever happened is
        no longer the explanation for the current state.
        """
        now = now or time.time()
        implies = {
            EV_INSTANT_CUT: CAUSE_EMERGENCY_INSTANT,
            EV_SAFE_CUT: CAUSE_EMERGENCY_SAFE,
            EV_SAFE_STARTED: CAUSE_EMERGENCY_SAFE,
            EV_MANUAL_HIBERNATE: CAUSE_MANUAL_HIBERNATE,
        }
        try:
            for ev in store.recent_events(200):      # newest first
                ts = ev.get("ts") or 0
                if now - ts > window:
                    break
                cause = implies.get(ev.get("kind"))
                if cause:
                    self.cause = cause
                    self._was_awake = False
                    return cause
        except Exception:
            pass
        return None

    def declare_intent(self, cause, now=None):
        """Called by a control action just before it acts."""
        self._intent = cause
        self._intent_at = now or time.time()

    def clear_intent(self):
        self._intent = None
        self._intent_at = 0.0

    def update(self, now, box_state, on_battery):
        """Call once per tick. Returns the current cause, or None if up."""
        awake = (box_state == "awake")

        if awake:
            # Back up: forget everything, so the next disappearance is
            # attributed on its own evidence rather than stale history.
            self.cause = None
            self.clear_intent()
            self._was_awake = True
            return None

        if box_state == "unknown":
            # Never seen since start -- we genuinely cannot attribute this.
            return self.cause

        # Box is down. Attribute once, on the transition, and then hold:
        # re-deciding every tick would let the cause flip as the UPS state
        # changes underneath (e.g. mains returning mid-outage).
        if self.cause is None:
            if self._intent and (now - self._intent_at) <= INTENT_TTL:
                self.cause = self._intent
            elif on_battery is True:
                self.cause = CAUSE_OUTAGE
            elif self._was_awake is None:
                # The collector started with the box already down; we have no
                # evidence either way and must not invent any.
                self.cause = CAUSE_UNKNOWN
            else:
                self.cause = CAUSE_UNKNOWN
            self.clear_intent()

        # An outage that begins AFTER a manual shutdown does not retroactively
        # turn it into an outage -- but a box that went down for an unknown
        # reason and is now clearly on battery probably did go down to the
        # outage, so allow that one upgrade.
        if self.cause == CAUSE_UNKNOWN and on_battery is True:
            self.cause = CAUSE_OUTAGE

        self._was_awake = False
        return self.cause

    @property
    def is_manual(self):
        return self.cause in MANUAL_CAUSES
