"""The collector's per-tick side of the state ledger (docs/LEDGER.md).

Split out of collector.py for the 300-line cap, along the ledger seam. Once
per tick the collector calls begin() -- retry a failed dash.json write, read
the sentinel's state once -- and, with the snapshot built, finish(), which
adds what the ledger contributes:

  snap["ledger"] = {"sentinel": facts|None, "sentinel_age": s|None,
                    "sentinel_status": "fresh"|"stale"|"dead"|"missing"|
                                       "unreadable",
                    "dash_updated": ts|None}
  snap["view"]   = {"mode": ..., "on_battery": True|False|None}

and clears a stale wake hold. snap["view"] is what the browser renders
instead of re-deriving it (the rule used to live twice, in timeline.js
effectiveMode() and now.js isSelfTest()):

  mode        derived.mode, but None while the state is SELF_TEST -- the
              calm default -- and "parked" while it is PARKED (the park is
              armed or parked, or back on mains waiting for the box)
  on_battery  ups.on_battery with a self-test's OB set aside: the one
              "is mains lost" answer, also while a park holds the headline

Never raises into the 1 Hz loop.
"""

from . import hold, states

HOLD_STALE_SEC = 3600.0   # the sentinel's old valve: a hold this old is over
HOLD_AWAKE_SEC = 60.0     # ...once the box has been seen up this long


def _num(v):
    return (isinstance(v, (int, float)) and not isinstance(v, bool)
            and v == v and abs(v) != float("inf"))


def view(state, derived, on_battery):
    st = (state or {}).get("state")
    if st == states.SELF_TEST:
        # SELF_TEST is classify()'s reading of self_test_explains() for an
        # awake box. A box that was already down keeps its own state during
        # a test, and so its own mode -- exactly what the browser did.
        mode = None
    elif st == states.PARKED:
        mode = "parked"
    else:
        mode = (derived or {}).get("mode")
    return {"mode": mode, "on_battery": on_battery}


class LedgerTick(object):
    def __init__(self, led):
        self.led = led
        self.facts = self.age = None
        self._awake_since = None

    def begin(self, now):
        """Returns the sentinel's facts: None unless its heartbeat is fresh."""
        try:
            self.led.tick()
            self.facts, self.age = self.led.peer(now)
        except Exception:
            self.facts, self.age = None, None
        return self.facts

    def finish(self, snap, now, on_battery):
        try:
            snap["view"] = view(snap.get("state"), snap.get("derived"), on_battery)
        except Exception:
            snap["view"] = {"mode": None, "on_battery": None}
        snap["ledger"] = {"sentinel": self.facts, "sentinel_age": self.age,
                          "sentinel_status": getattr(self.led, "peer_status", None),
                          "dash_updated": getattr(self.led, "updated", None)}
        try:
            self._stale_hold(now, (snap.get("box") or {}).get("state"),
                             (snap.get("park") or {}).get("phase"),
                             snap.get("wake_hold"))
        except Exception as exc:
            print("[ups-dash] stale-hold check failed: %s: %s"
                  % (type(exc).__name__, exc), flush=True)

    def _stale_hold(self, now, box_state, phase, held):
        """The sentinel's old stale-hold valve, moved here: one writer per
        file, so it now only IGNORES a stale hold and ups-dash clears it.
        cause.py lifts the hold on the box's down -> up edge; if that edge
        was missed (a restart while the box was down), a leftover hold
        would block the NEXT outage's recovery. Never during a park story:
        the box powering itself on with the mains is not the manual
        shutdown ending (cause.py, park_guard.py)."""
        if box_state != "awake":
            self._awake_since = None
            return
        if self._awake_since is None:
            self._awake_since = now
        up = now - self._awake_since
        if not held or phase is not None or up < HOLD_AWAKE_SEC:
            return
        ts = held.get("ts")                    # get_hold(): always a dict
        age = now - ts if _num(ts) else up     # no stamp: count from seeing it up
        if age > HOLD_STALE_SEC:
            hold.clear_hold(expect=held)      # not a newer one placed meanwhile
            if hold.get_hold() is None:
                print("[ups-dash] stale wake hold cleared (%s, %ds old) - the "
                      "box has been up" % (held.get("reason"), age), flush=True)
