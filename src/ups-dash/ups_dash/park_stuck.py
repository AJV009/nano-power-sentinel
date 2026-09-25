"""The park guard's third outcome: the box powered on but never reached its OS.

2026-09-25: a park at 35 % cut the box's power, mains returned at 15:59:47,
and 4 s later the UPS load jumped to 164 W -- AC BACK worked, the box was ON.
It then sat at ~104-112 W, black screen, not on the LAN (no ARP reply), for
six hours, while the dashboard said "press its power button: AC BACK is
probably not Always On". A forced power-off and one press brought it back
RESUMING Thursday's hibernate image, so that attempt never reached Linux:
the kernel wipes the image signature the moment it finds it. Stuck in the
firmware (DDR5 training after a full power loss is the likely stage) or the
boot menu. Normal Linux idle here is ~43 W; Tuesday's park return went
147 W -> ~75 W while resuming -> online in 55 s.

So, while the guard waits (phase "returning", no rehibernate sent):
  * load >= POWERED_PCT is the box powering on (the output was OFF seconds
    earlier, so nothing else on the UPS was running) -- logged once;
  * powered for STUCK_SEC, agent silent AND its NIC absent from the ARP table
    = stuck before the OS. The cure is the park's own command on mains:
    shutdown.reboot 1 cuts the output ~60 s later and restores it ~4 s after
    (bench test 1), and AC BACK powers the box on for another try. Safe by
    the evidence above: nothing has touched the disk or the hibernate image.
    At most MAX_CYCLES per park story, then it is reported, not retried;
  * powered but ON the LAN (answers ARP, agent silent) is a different box
    state -- another OS, or box-agent died -- and is never power-cycled.

A cycle in flight holds the story open past GUARD_SEC; the output coming
back restarts the guard window, exactly as the park's own return did.
"""

from . import park_io, states

POWERED_PCT = 6          # ~52 W: above standby (0 %), below every boot stage
STUCK_SEC = 420.0        # a good power-on reaches the agent in ~55 s. Long,
                         # because shutdown.reboot cuts ~60 s after the ACK and
                         # cannot be taken back: a box that finishes POST in
                         # that minute is cut mid-resume and cold-boots.
MAX_CYCLES = 2
CYCLE_TIMEOUT_SEC = 180.0   # ACK -> cut is ~60 s; no cut by now: it never armed


def _num(v):
    return isinstance(v, (int, float)) and not isinstance(v, bool)


class StuckMixin(object):

    def _reset_stuck(self):
        self._pow_since = None       # load continuously >= POWERED_PCT since
        self._pow_seen = False       # powered at all since the last return
        self._lan_seen = False       # agent silent but the NIC answers ARP
        self._cycle_off = False      # the cycle's cut has been seen

    def _stuck(self, now, ups, ok, ob, off, testing):
        """Called while returning with the box not awake and no rehibernate
        out. True = hold the story (a cycle in flight): the caller must not
        run its window logic this tick."""
        m = self.m
        if m.get("cycle_sent") is not None:
            return self._cycling(now, ok, off)
        load = ups.get("load_pct") if ok else None
        if not (_num(load) and load >= POWERED_PCT):
            self._pow_since = None
            return False
        if self._pow_since is None:
            self._pow_since = now
        if not self._pow_seen:
            self._pow_seen = True
            back = m.get("returned_at")
            self._event(now, states.BOX_POWERED_ON, {
                "load_pct": load, "watts": ups.get("watts"),
                "after_return_s": round(now - back, 1) if _num(back) else None})
        if now - self._pow_since < STUCK_SEC or ob is not False or testing:
            return False
        if self._lan(now):
            return False
        cycles = m.get("cycles") or 0
        if cycles >= MAX_CYCLES or self._job is not None:
            return self._job is not None
        m["cycles"], m["cycle_sent"] = cycles + 1, now
        self._cycle_off = False
        self._save()
        ctx = {"at": now, "cycle": cycles + 1, "max": MAX_CYCLES,
               "load_pct": load, "watts": ups.get("watts"),
               "powered_s": round(now - self._pow_since, 1)}
        self._event(now, states.BOX_STUCK_PRE_OS, ctx)
        self._job = park_io.Job("powercycle",
                                lambda: park_io.arm_command(self._instcmd),
                                ctx, self._spawn)
        return True

    def _lan(self, now):
        """Does the box's NIC answer ARP? Logged the first time it does."""
        try:
            present = bool(self._lan_present())
        except Exception:
            present = False
        if present and not self._lan_seen:
            self._lan_seen = True
            self._event(now, states.BOX_LAN_NO_AGENT, {})
        return present

    def _cycling(self, now, ok, off):
        m = self.m
        if self._job is not None:
            return True                       # the command is still going out
        if ok and off:
            self._cycle_off = True
            return True
        if self._cycle_off and ok:
            # Output back: a fresh return, a fresh guard window.
            m["cycle_sent"], m["returned_at"] = None, now
            self._save()
            self._reset_stuck()
            self._event(now, states.UPS_OUTPUT_BACK_CYCLE,
                        {"cycle": m.get("cycles")})
            return True
        if now - m["cycle_sent"] >= CYCLE_TIMEOUT_SEC:
            m["cycle_sent"], m["cycles"] = None, MAX_CYCLES   # no more tries
            self._save()
            self._fail(now, "cycle", "the UPS accepted the power-cycle but "
                       "never cut its output")
        return True

    def _reap_cycle(self, now, ok, ctx):
        if ok:
            self._cycled_at = now            # the push's stamp (notify)
            self._event(now, states.BOX_POWER_CYCLED, ctx)
            return
        m = self.m or {}
        m["cycle_sent"], m["cycles"] = None, MAX_CYCLES
        self._save()
        info = ctx.get("reply") if isinstance(ctx.get("reply"), dict) else {}
        self._fail(now, "cycle", "UPS refused %s: %s" % (
            info.get("cmd") or "shutdown.reboot 1", info.get("reply")))

    def _stuck_outcome(self):
        """What the window's end means, by what was seen during it."""
        if self._lan_seen:
            return states.PARK_LAN_NO_AGENT
        if self._pow_seen:
            return states.PARK_STUCK_PRE_OS
        return states.PARK_NO_POWER_ON
