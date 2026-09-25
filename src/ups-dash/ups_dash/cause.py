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

PUBLISHED to the state ledger's "cause" key (ledger.py) whenever it changes:
{"down_cause", "intent", "intent_at", "since"} -- `since` is when the current
down_cause was attributed. Informational for other readers: the sentinel
never takes a decision from ups-dash's classifier (docs/LEDGER.md).
"""

import time

from . import hold
from .states import (CAUSE_OUTAGE, CAUSE_UNKNOWN, CAUSE_EMERGENCY_SAFE,
                     CAUSE_EMERGENCY_INSTANT, CAUSE_MANUAL_HIBERNATE,
                     MANUAL_CAUSES, PARK_ARMED, PARK_PARKED, PARK_RETURNING)
from .states import (EMERGENCY_INSTANT_CUT as EV_INSTANT_CUT,
                     EMERGENCY_SAFE_CUT as EV_SAFE_CUT,
                     EMERGENCY_SAFE_STARTED as EV_SAFE_STARTED,
                     MANUAL_HIBERNATE as EV_MANUAL_HIBERNATE)

# How long a declared intent stays valid. A hibernate can take a while to
# actually drop the box (image write, then power-off), but an intent from an
# hour ago must not explain an unrelated disappearance later.
INTENT_TTL = 420.0


class CauseTracker(object):
    def __init__(self, ledger=None):
        self.cause = None          # set only while the box is down
        self._since = None         # when `cause` was attributed
        self._intent = None
        self._intent_at = 0.0
        self._was_awake = None
        self._ledger = ledger      # None: publish nowhere (tests)
        self._published = None

    def attribution(self):
        """The ledger's "cause" value."""
        return {"down_cause": self.cause, "intent": self._intent,
                "intent_at": self._intent_at if self._intent else None,
                "since": self._since if self.cause else None}

    def _publish(self):
        """To the ledger, only when it changed. Never raises."""
        if self._ledger is None:
            return
        try:
            doc = self.attribution()
            if doc != self._published:
                self._ledger.set("cause", doc)   # a failed write: ledger retries
                self._published = doc
        except Exception:
            pass

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
                    self.cause, self._since = cause, ts or now
                    self._was_awake = False
                    self._publish()
                    return cause
        except Exception:
            pass
        return None

    def declare_intent(self, cause, now=None):
        """Called by a control action just before it acts."""
        self._intent = cause
        self._intent_at = now or time.time()
        self._publish()

    def clear_intent(self):
        self._intent = None
        self._intent_at = 0.0
        self._publish()

    def update(self, now, box_state, on_battery, park=None):
        """Call once per tick. Returns the current cause, or None if up.

        `park` is snap["park"] (park.py) as it stood BEFORE this tick's park
        decision -- the phase the box's return happened in."""
        cause = self._update(now, box_state, on_battery, park)
        self._publish()
        return cause

    def _update(self, now, box_state, on_battery, park):
        # Another OS (lanprobe.py) is a box somebody is using: up, for every
        # purpose here -- a hold from before is over once they boot it.
        awake = box_state in ("awake", "other_os")

        if awake:
            came_back = (self._was_awake is False)
            # After a battery-floor park the box powers ITSELF on with the
            # mains (BIOS AC BACK). That is not "you woke it": the guard puts
            # it straight back to sleep, so a manual shutdown's hold must
            # survive -- the sentinel reads it once the park story ends, and
            # clearing it here would let the next gate wake a box you
            # switched off -- and a declared intent must still attribute the
            # drop. (The guard itself decides on its hold SNAPSHOT.)
            if came_back and (park or {}).get("phase") in (
                    PARK_ARMED, PARK_PARKED, PARK_RETURNING):
                came_back = False
            self.cause = None
            self._was_awake = True
            # ⚠ The intent is NOT cleared on every awake tick. It is declared
            # while the box is still up, and after a hibernate request the box
            # keeps LOOKING up for a poll-grace window (up to 15 s) before the
            # agent goes silent. The previous version cleared it here, so the
            # intent was wiped before the box ever went down and a deliberate
            # hibernate was reported as "Box unreachable". It is consumed on
            # attribution below, or expires via INTENT_TTL.
            if came_back:
                # Genuinely returned from being down: the manual shutdown is
                # over, so lift the hold that stops the sentinel waking it.
                self.clear_intent()
                hold.clear_hold()
            return None

        if box_state == "unknown":
            # Never seen since start -- we genuinely cannot attribute this.
            return self.cause

        # Box is down. Attribute once, on the transition, and then hold:
        # re-deciding every tick would let the cause flip as the UPS state
        # changes underneath (e.g. mains returning mid-outage).
        if self.cause is None:
            self._since = now
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

        # (Removed: an "unknown -> outage" upgrade once the UPS went on
        # battery. Cause is attributed on the DOWN transition, so "unknown"
        # already means the box went down while mains was fine; an outage
        # starting later does not make it an outage casualty. That rule is
        # what relabelled a 19:05 manual hibernate as "Box hibernated" when
        # the power failed at 20:30.)

        self._was_awake = False
        return self.cause

    @property
    def is_manual(self):
        return self.cause in MANUAL_CAUSES
