"""The first half of the park phase machine: none -> armed -> parked, and the
ways a park can end before mains returns.

Split out of park.py for the 300-line cap. ParkTracker (park.py) inherits
these methods; they read and write its fields and never do I/O themselves --
the arm command goes to the injected worker, files and events go through
park.py's never-raise helpers.
"""

from . import park_io, states

ARMED, PARKED, RETURNING = states.PARK_ARMED, states.PARK_PARKED, states.PARK_RETURNING

SETTLE_SEC = 60.0        # ups.load refreshes every ~12 s: >= 4 fresh reads
MAX_LOAD_PCT = 3         # upsoff.LOAD_FLOOR: at or below, the box has stopped
                         # (finished writing its image, not merely silent)
ARM_RETRY_SEC = 300.0    # after a refusal: at most one retry per 5 min
CUT_GRACE_SEC = 60.0     # ACK -> cut, bench tests 1/3/4 (60-62 s)
CUT_TIMEOUT_SEC = 180.0  # still live on battery by now: it never armed
STUCK_SEC = 120.0        # OL OFF this long: the output did not come back


def _num(v):
    return isinstance(v, (int, float)) and not isinstance(v, bool)


class ArmMixin(object):

    def _consider_arm(self, now, ups, ob, off, box, cause, cut, testing, hold):
        """none -> armed. Every condition must hold continuously for
        SETTLE_SEC; any miss restarts the count. `_blocked` says why not, for
        the UI, once we are on battery at or below the floor."""
        charge, tun = ups.get("charge"), self.tun
        self._blocked = None
        if (not tun["enabled"] or ob is not True or testing
                or not _num(charge) or charge > tun["floor"]):
            self._cond_since = self._awake_since = None
            return
        if box == "awake":
            # Never park a running box: the armed cut would hard-kill it.
            self._cond_since = None
            self._blocked = "box awake"
            self._awake_at_floor(now, charge)
            return
        self._awake_since = None
        load = ups.get("load_pct")
        t_sd, t_rb = ups.get("timer_shutdown"), ups.get("timer_reboot")
        if off:
            why = "UPS output already off"
        elif not _num(load) or load > MAX_LOAD_PCT:
            # Also the proof the box finished writing its hibernate image:
            # "agent silent" only means hibernation STARTED (upsoff.py).
            why = "load %s%% (needs <= %d%%)" % (load, MAX_LOAD_PCT)
        elif cut.get("active"):
            why = "emergency shutdown in progress"
        elif (_num(t_sd) and t_sd >= 0) or (_num(t_rb) and t_rb > 0):
            why = "a UPS shutdown timer is already counting"
        elif self._job is not None:
            why = "command in flight"
        elif self._fail_at is not None and now - self._fail_at < ARM_RETRY_SEC:
            why = "UPS refused; retry in %ds" % (ARM_RETRY_SEC - (now - self._fail_at))
        else:
            why = None
        if why:
            self._cond_since, self._blocked = None, why
            return
        if self._cond_since is None:
            self._cond_since = now
        left = SETTLE_SEC - (now - self._cond_since)
        if left > 0:
            self._blocked = "settling, %ds" % left
            return
        self._cond_since = None
        # hold and cause are SNAPSHOTS: by the time the box powers on again,
        # cause.py may have moved on, and the guard must not flip with it.
        ctx = {"at": now, "charge": charge, "floor": tun["floor"],
               "load_pct": load, "hold_at_park": bool(hold),
               "cause_at_park": cause}
        self._job = park_io.Job(
            "arm", lambda: park_io.arm_command(self._instcmd), ctx, self._spawn)

    def _awake_at_floor(self, now, charge):
        """Once per episode: the box still up below the floor AND below the
        governor's reserve, for SETTLE_SEC. Floor >= reserve is legitimate
        (the box stays up until the governor's threshold), so only a box
        awake under the reserve means the governor is not doing its job."""
        reserve = self.tun["reserve"]
        if _num(reserve) and charge >= reserve:
            self._awake_since = None
            return
        if self._awake_since is None:
            self._awake_since = now
        key = self._ep if self._ep is not None else "no-episode"
        if now - self._awake_since >= SETTLE_SEC and self._skip_key != key:
            self._skip_key, self._skipped_at = key, now
            self._event(now, states.UPS_PARK_SKIPPED,
                        {"charge": charge, "floor": self.tun["floor"],
                         "reserve": reserve})

    def _while_parked(self, now, ok, ob, off):
        """armed -> parked -> returning, or the park ends early."""
        m = self.m
        armed_at = m.get("armed_at")
        young = _num(armed_at) and now - armed_at < CUT_TIMEOUT_SEC
        if not ok or off or ob:
            self._saw_parked = True
        if not ok or (off and ob):
            # Silent = it switched its own electronics off (~30 s after the
            # cut, bench test 4): expected, which is why PARKED outranks BLIND.
            self._stuck_since = None
            self._cut_seen = self._cut_seen or bool(off)
            self._to(now, PARKED, "UPS went silent" if not ok else "OB OFF")
            return
        if off:
            # OL OFF: the ~4 s mains-side cycle of an arm, or the seconds
            # before a parked UPS restores. Longer is a dead output, and the
            # story must end so OUTPUT_OFF (press the button) can surface.
            self._cut_seen = True
            if self._stuck_since is None:
                self._stuck_since = now
            elif now - self._stuck_since >= STUCK_SEC:
                self._fail(now, "return", "output still OFF %ds after mains "
                           "returned" % (now - self._stuck_since), push=False)
                self._end(now)
            return
        self._stuck_since = None
        if ob:
            if m["phase"] == ARMED and young:
                return          # the grace before the cut: expected
            if m["phase"] == ARMED:
                # ACKed but never armed (bench test 2's signature): retry
                # only after ARM_RETRY_SEC, like a refused command.
                self._fail(now, "cut", "output still live on battery %s "
                           "after the UPS accepted the park" % (
                               "%ds" % (now - armed_at) if _num(armed_at)
                               else "long"))
                self._fail_at = now
            else:
                # Parked, yet live on battery: mains came back between two
                # reads and failed again. The park is over; the pack drains
                # again, so the ordinary settle may re-park straight away.
                print("[ups-dash] park: output live on battery again; park "
                      "over", flush=True)
            self._end(now)
            return
        if m["phase"] == ARMED and not self._cut_seen and young:
            return   # mains back inside the grace: the armed cut still fires
        # Output back on mains. If we never SAW it parked (adopted after a
        # restart and found already back), when it returned is unknown.
        self._blind_return = not self._saw_parked
        m["phase"], m["returned_at"] = RETURNING, now
        self._save()

    def _to(self, now, phase, why):
        if self.m["phase"] != phase:
            self.m["phase"] = phase
            self._save()
            self._event(now, states.UPS_PARKED,
                        {"why": why, "charge": self.m.get("charge")})
