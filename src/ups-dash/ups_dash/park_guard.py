"""The second half of the park phase machine: the power-on GUARD.

After a park the box has no standby power, so it comes back only if its BIOS
powers it on with the returning AC (AC BACK = Always On) -- at ~floor charge,
far below the wake gate. Left up, the next outage would find it on a pack
with little to give. So a box that powers on inside GUARD_SEC of the output
returning is put back to sleep, unless it should legitimately be up; asleep
WITH mains it has standby power and an armed NIC, and the sentinel's normal
gated WoL brings it back once the pack recovers.

Split out of park.py for the 300-line cap; ParkTracker inherits this.

THE ONE RULE THAT MATTERS: the guard puts a box to sleep only while the park
marker is ON DISK (the ledger's "park" key, re-read from dash.json). The
sentinel stands down on any box it sees up with no marker (setting
outage_seen False), and would then never wake the box we put back to sleep
-- a workstation asleep until somebody notices.
"""

from . import park_io, states

RETURNING = states.PARK_RETURNING

GUARD_SEC = 600.0        # how long after the return a power-on is ours
REHIB_RETRY_SEC = 60.0   # still awake this long after a request: ask again
REHIB_MAX_TRIES = 3
DOWN_CONFIRM_SEC = 20.0  # not awake this long after a rehibernate: done
RETURN_OB_SEC = 10.0     # on battery this long while returning: a new outage


def _num(v):
    return isinstance(v, (int, float)) and not isinstance(v, bool)


class GuardMixin(object):

    def _returning(self, now, ups, ok, ob, box, testing, hold):
        m = self.m
        if ok and ob is True and not testing:
            # Mains failed again. A blip must not end the guard (the box may
            # be powering on right now); a real new outage is a new story,
            # and the ordinary settle may park again.
            if self._ob_since is None:
                self._ob_since = now
            elif now - self._ob_since >= RETURN_OB_SEC:
                self._end(now)
            return
        if ok:
            self._ob_since = None
        sent, back = m.get("rehibernate_sent"), m.get("returned_at")
        if box == "awake":
            self._down_since = None
            if self._job is not None:
                return                              # a hibernate in flight
            if sent is None and (self._blind_return or not _num(back)
                                 or now - back > GUARD_SEC):
                # Up outside the window, or back while ups-dash was not
                # watching: not a power-on we can vouch for -- it may be a
                # human at the button. Leave it up.
                self._end(now)
                return
            if sent is not None and now - sent < REHIB_RETRY_SEC:
                return
            if self._tries >= REHIB_MAX_TRIES:
                self._fail(now, "rehibernate", "still awake after %d "
                           "hibernate requests" % self._tries)
                self._end(now)
                return
            if self._keep_up(now, ups.get("charge"), hold):
                # The gate is already open: the sentinel stands down on it.
                self._end(now)
                return
            on_disk = park_io.as_marker(self.ledger.persisted(park_io.MARKER))
            if (on_disk or {}).get("phase") != RETURNING:
                self._fail(now, "marker", "park marker not on disk, so the "
                           "sentinel would never wake it; leaving the box up")
                self._end(now)
                return
            # Intent FIRST: this sleep belongs to the outage, so the box reads
            # RECOVERING ("waiting for the gate") once it drops, not BOX_LOST.
            self._declare(states.CAUSE_OUTAGE, now)
            self._tries += 1
            m["rehibernate_sent"] = now
            self._save()
            ctx = {"at": now, "try": self._tries, "charge": ups.get("charge"),
                   "hold_at_park": m.get("hold_at_park"), "hold": bool(hold)}
            self._job = park_io.Job("rehibernate", self._hibernate, ctx,
                                    self._spawn)
            return
        if sent is None and self._stuck(now, ups, ok, ob,
                                        "OFF" in (ups.get("flags") or []),
                                        testing):
            return                              # a power-cycle in flight
        if sent is not None:
            # Once a request is out the window no longer applies: clearing
            # the marker while the box is still up would stand the sentinel
            # down just before we put it to sleep.
            if self._down_since is None:
                self._down_since = now
            elif now - self._down_since >= DOWN_CONFIRM_SEC:
                self._end(now)   # back asleep, with standby power this time
            return
        if not _num(back) or now - back >= GUARD_SEC:
            # Never reached the agent. Which way (park_stuck.py): no power
            # drawn at all -- AC BACK is probably not Always On; powered but
            # never on the LAN -- stuck before the OS; on the LAN but silent
            # -- another OS, or the agent died. Only a human can help now.
            # Remembered on disk until the box is next seen awake.
            kind = self._stuck_outcome()
            self.outcome = {"outcome": kind, "ts": now,
                            "armed_at": m.get("armed_at"),
                            "returned_at": back, "charge": m.get("charge"),
                            "cycles": m.get("cycles") or 0,
                            "watts": ups.get("watts")}
            self.ledger.set(park_io.OUTCOME, self.outcome)
            self._event(now, kind, {"returned_at": back,
                                    "charge": ups.get("charge"),
                                    "cycles": m.get("cycles") or 0,
                                    "watts": ups.get("watts")})
            self._end(now)

    def _keep_up(self, now, charge, hold):
        """The box may stay up only if nobody put it down on purpose (the
        hold SNAPSHOT from park time -- cause.py may have moved on since --
        or a hold now), the outage is what took it, and the sentinel's own
        gate is already open: charge >= wake AND mains stable."""
        m, tun = self.m, self.tun
        if m.get("hold_at_park") or hold:
            return False
        if m.get("cause_at_park") != states.CAUSE_OUTAGE:
            return False
        if not _num(charge) or not _num(tun["wake"]) or charge < tun["wake"]:
            return False
        return (self._ol_since is not None and _num(tun["stable"])
                and now - self._ol_since >= tun["stable"])
