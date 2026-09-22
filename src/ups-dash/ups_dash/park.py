"""Battery-floor PARK and the power-on GUARD.

WHY: the box hibernates at the governor's reserve (50 %), but the pack keeps
draining 23-55 %/h after that through the UPS's own idle inverter -- on
2026-09-22 it bottomed at 21 %. Bench test 4 (docs/UPS-TOOLING.md §7,
BUILD-LOG 2026-09-22) proved the cure: `shutdown.return` (0x40 = 1) sent ON
BATTERY cuts the output ~60 s later, the UPS then switches its own
electronics off (USB gone, upsd "Data stale") and holds its charge with ZERO
drain, and restores the output ~3 s after mains returns. It cannot be
cancelled once armed.

THE COST: the cut takes the box's standby power, so its NIC forgets WoL. The
box comes back only if its BIOS powers it on with the returning AC (AC BACK =
Always On) -- at ~floor charge -- and the GUARD (park_guard.py) puts it
straight back to sleep until the sentinel's gate opens.

PHASES (the marker's "phase"; no marker = none):
  none -> armed          on battery, box down, load <= 3 %, charge <= floor,
                         all for SETTLE_SEC, and the UPS ACKed the command
  armed -> parked        OFF on battery, or the UPS went silent
  armed/parked -> returning   readable, OL, output on
  returning -> none      kept up / back asleep / window expired / gave up
park_arm.py holds the first three, park_guard.py the last.

The marker -- the "park" key of the state ledger (ledger.py, dash.json) -- is
the only interface with ups-sentinel, which only reads it: while it exists
the sentinel neither stands down on a box that powered itself on nor wakes
one. The outcome is the "park_outcome" key. park_enabled only gates a NEW arm
-- an armed UPS cannot be un-armed, so a story in flight always finishes.

Never raises into the 1 Hz loop, and holds no I/O of its own: instcmd,
hibernate, declare_intent, spawn, the ledger and the file tunables are all
injected (defaults in park_io.py), the way upsops.apply_setting takes reread.
"""

from . import park_io, states
from .park_arm import (ArmMixin, ARMED, PARKED, RETURNING, SETTLE_SEC,  # noqa: F401
                       MAX_LOAD_PCT, ARM_RETRY_SEC, CUT_GRACE_SEC,
                       CUT_TIMEOUT_SEC, STUCK_SEC)
from .park_guard import (GuardMixin, GUARD_SEC, REHIB_RETRY_SEC,  # noqa: F401
                         REHIB_MAX_TRIES, DOWN_CONFIRM_SEC, RETURN_OB_SEC)

PHASES = (ARMED, PARKED, RETURNING)
FILE_EVERY = 10.0        # re-read /etc/ups-dash/tunables.json at most this often


def _num(v):
    return isinstance(v, (int, float)) and not isinstance(v, bool)


class ParkTracker(ArmMixin, GuardMixin):
    def __init__(self, store=None, instcmd=None, hibernate=None,
                 declare_intent=None, spawn=None, ledger=None,
                 file_tunables=None):
        self.store = store
        self._instcmd = instcmd                # None = upscmd.instcmd
        self._hibernate = hibernate or park_io.hibernator()
        self._declare = declare_intent or (lambda cause, now=None: None)
        self._spawn = spawn or park_io.spawn_thread
        self.ledger = ledger or park_io.default_ledger()
        self._read_file = file_tunables or park_io.read_local
        self._file, self._file_at = {}, None
        self.tun = park_io.resolve(None, None)
        self._job = None
        self._ep = None
        self._cond_since = self._awake_since = self._ol_since = None
        self._fail_at = None               # last refusal (the retry gate)
        self._skip_key = self._skipped_at = None
        self._failed = None                # {"at", "stage", "detail"}: pushed
        self._blocked = None
        self._dirty = self._unlink = False
        self._reset_story()
        # Adopt a story that survived a restart. A marker that is present but
        # unreadable is still a park (the sentinel reads it that way too): it
        # is adopted as "parked" and ends when the UPS shows mains again.
        m = park_io.as_marker(self.ledger.get(park_io.MARKER))
        if m is not None:
            if m.get("phase") not in PHASES:
                m = dict(m, phase=PARKED)
            self.m = m
            self._tries = 1 if m.get("rehibernate_sent") else 0
        self.outcome = park_io.as_outcome(self.ledger.get(park_io.OUTCOME))

    def _reset_story(self):
        self.m = None
        self._tries = 0
        self._cut_seen = False
        self._stuck_since = self._down_since = self._ob_since = None
        # False on adoption: until the UPS is SEEN parked, a return we find
        # already done happened while nobody was watching (park_guard.py).
        self._saw_parked = False
        self._blind_return = False

    @property
    def phase(self):
        return (self.m or {}).get("phase")

    # ---- the tick -------------------------------------------------------

    def tick(self, now, ups, box_state, down_cause=None, ups_cut=None,
             testing=False, wake_hold=None, tunables=None, episode_id=None):
        """Once per collector tick, AFTER the UPS and box readings exist.
        `testing` is states.self_test_explains(); `tunables` the collector's.
        Returns snap["park"]. Never raises."""
        try:
            self._tick(now, ups or {}, box_state, down_cause, ups_cut or {},
                       bool(testing), wake_hold, tunables, episode_id)
        except Exception as exc:
            print("[ups-dash] park tick failed: %s: %s"
                  % (type(exc).__name__, exc), flush=True)
        return self.snapshot(now)

    def _tick(self, now, ups, box_state, cause, cut, testing, hold, learned, ep):
        self._ep = ep
        if self._file_at is None or now - self._file_at >= FILE_EVERY:
            self._file_at = now
            try:
                self._file = self._read_file() or {}
            except Exception:
                self._file = {}
        self.tun = park_io.resolve(learned, self._file)
        ok = bool(ups.get("ok"))
        ob = ups.get("on_battery") if ok else None
        off = ("OFF" in (ups.get("flags") or [])) if ok else None
        # Mains stability the sentinel's way: any OB restarts the window, an
        # unreadable tick holds it (absence of data is not "mains back").
        if ob is True:
            if self._ol_since is not None and self.m is None:
                self._failed = None    # a new outage: its first failure pushes
            self._ol_since = None
        elif ob is False and self._ol_since is None:
            self._ol_since = now
        if box_state == "awake" and self.outcome:
            # Somebody pressed the button (or fixed AC BACK): said, done.
            self.outcome = None
            self.ledger.set(park_io.OUTCOME, None)
        if self._dirty:
            self._save()               # a failed marker write is retried
        if self._unlink and self.ledger.set(park_io.MARKER, None):
            self._unlink = False
        self._reap(now)
        if self.phase is None:
            self._consider_arm(now, ups, ob, off, box_state, cause, cut,
                               testing, hold)
        else:
            self._blocked = None
            if self.phase in (ARMED, PARKED):
                self._while_parked(now, ok, ob, off)
            # Same tick as the return: a box already up must be judged now,
            # or the state line shows one tick of a guard that is not acting.
            if self.phase == RETURNING:
                self._returning(now, ups, ok, ob, box_state, testing, hold)
        self._reap(now)      # a synchronous spawn (tests) lands this tick

    # ---- results, files, events -----------------------------------------

    def _reap(self, now):
        job = self._job
        if job is None or not job.done:
            return
        self._job = None
        ok, info = job.result
        ctx = dict(job.ctx, ok=ok, reply=info)
        if job.kind == "rehibernate":
            self._event(now, states.BOX_REHIBERNATED, ctx)
            return
        if not ok:
            info = info if isinstance(info, dict) else {"reply": info}
            self._fail_at = now
            self._fail(now, "arm", "UPS refused %s: %s" % (
                info.get("cmd") or "shutdown.return", info.get("reply")))
            return
        # ACKed: the UPS WILL cut, so the marker is written whatever else
        # happened since the command went out.
        self._reset_story()
        self._saw_parked = True
        self._failed = None
        c = job.ctx
        self.m = {"phase": ARMED, "armed_at": c["at"], "charge": c["charge"],
                  "floor": c["floor"], "hold_at_park": c["hold_at_park"],
                  "cause_at_park": c["cause_at_park"],
                  "returned_at": None, "rehibernate_sent": None}
        self._save()
        self._event(now, states.UPS_PARK_ARMED, ctx)

    def _save(self):
        """Marker write; on failure retried every tick (the guard refuses to
        put a box to sleep while the marker is not on disk)."""
        self._dirty = not (self.m is not None
                           and self.ledger.set(park_io.MARKER, self.m))

    def _end(self, now):
        self._reset_story()
        self._dirty = False
        self._unlink = not self.ledger.set(park_io.MARKER, None)

    def _fail(self, now, stage, detail, push=True):
        """Always stored; pushed (via snapshot failed_at) only for the first
        failure since the last successful arm or the start of this outage,
        so a UPS that keeps refusing is one alert, not one per retry."""
        if push and self._failed is None:
            self._failed = {"at": now, "stage": stage, "detail": detail}
        self._event(now, states.PARK_FAILED, {"stage": stage, "detail": detail})

    def _event(self, now, kind, detail):
        print("[ups-dash] park: %s %s" % (kind, detail), flush=True)
        try:
            if self.store is not None:
                self.store.add_event(now, kind, self._ep, detail)
        except Exception:
            pass

    def snapshot(self, now=None):
        """snap["park"]: the contract fields, plus why it is not parking
        (`blocked`), the cut countdown, and the skip/failure stamps the
        notifier turns into pushes. Never raises."""
        try:
            return self._snapshot(now)
        except Exception:
            return {"phase": self.phase, "outcome": None, "enabled": None}

    def _snapshot(self, now):
        m, tun, failed = self.m or {}, self.tun, self._failed or {}
        armed_at, back = m.get("armed_at"), m.get("returned_at")
        cut_in = None
        if (m.get("phase") == ARMED and not self._cut_seen and _num(armed_at)
                and _num(now)):
            cut_in = max(0.0, round(armed_at + CUT_GRACE_SEC - now, 1))
        return {"phase": m.get("phase"), "armed_at": armed_at,
                "charge": m.get("charge"), "floor": m.get("floor"),
                "returned_at": back,
                "rehibernate_sent": m.get("rehibernate_sent"),
                "outcome": (self.outcome or {}).get("outcome"),
                "outcome_charge": (self.outcome or {}).get("charge"),
                "enabled": bool(tun["enabled"]), "floor_pct": tun["floor"],
                "hold_at_park": m.get("hold_at_park"),
                "cause_at_park": m.get("cause_at_park"),
                "cut_in": cut_in, "cut_seen": self._cut_seen,
                "guard_until": back + GUARD_SEC if _num(back) else None,
                "tries": self._tries, "blocked": self._blocked,
                "skipped_at": self._skipped_at,
                "failed_at": failed.get("at"),
                "failed_stage": failed.get("stage"),
                "failed_detail": failed.get("detail")}
