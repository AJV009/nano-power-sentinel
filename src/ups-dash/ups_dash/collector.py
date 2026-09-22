"""The collector loop.

  UPS poller   1 Hz   upsd on localhost
  box poller   5 s    box-agent, hard timeout
  journal tail        ups-sentinel + nut-*, read-only
  state ledger 1 Hz   the sentinel's state.json read; dash.json written on
                      change only (ledger.py, ledger_tick.py)

Absence of the box is not self-explanatory, so it is correlated with what the
governor last said: a commanded hibernate reads as "asleep", anything else
reads as "unreachable".  Two different facts, never merged.
"""

import collections
import threading
import time

from . import derive, ledger, nanovitals, nut, services, upsblock
from .boxpoll import BoxClient
from .episodes import EpisodeTracker
from .journal import JournalTail
from .notify import Notifier
from .sample import flatten
from .tunables import Learner
from . import hold, park_io, settings, states, upsoff
from .cause import CauseTracker
from .ledger_tick import LedgerTick
from .park import ParkTracker
from .upsextras import UpsExtras

UPS_POLL = 1.0
BOX_POLL = 5.0
SERVICE_POLL = 30.0
IDLE_PERSIST = 30.0        # res=30 cadence while nothing is happening
RING_SECONDS = 3600        # 60 min of 1 Hz history in RAM, never on disk
MAINTAIN_EVERY = 3600
HIBERNATE_MEMORY = 900     # how long a HIBERNATING log line explains absence

NANO_UNITS = ["nut-driver.service", "nut-server.service",
              "nut-monitor.service", "ups-sentinel.service"]


class Collector(object):
    def __init__(self, store, ups_name=None, box_url=None):
        ups_name = ups_name or settings.UPS_NAME
        box_url = box_url or settings.BOX_URL
        self.store = store
        self.nut = nut.NutClient(ups=ups_name)
        self.box = BoxClient(box_url)
        self.tail = JournalTail(NANO_UNITS)
        # The state ledger (ledger.py): our facts in dash.json, the
        # sentinel's in its state file. Loaded, the legacy marker files
        # folded in, and re-persisted BEFORE anything below reads a hold, a
        # park or a cause.
        self.ledger = ledger.default()
        self.ledger.start()
        self.ledger_tick = LedgerTick(self.ledger)
        self.learner = Learner()
        self.episodes = EpisodeTracker(store)
        self.cause = CauseTracker(self.ledger)     # publishes "cause"
        # Self-test and transfer-cause memory (upsextras.py docstring).
        self.extras = UpsExtras()
        # A restart must not turn "you cut the power" into "unknown reason":
        # the evidence is in the event log, so read it back.
        self.cause.recover_from_store(store)
        # Battery-floor park + power-on guard (park.py). Its slow calls run on
        # a worker thread; it only needs to be told how to reach the box and
        # how to declare why the box is about to go down.
        self.park = ParkTracker(store, hibernate=park_io.hibernator(box_url),
                                declare_intent=self.cause.declare_intent,
                                ledger=self.ledger)
        # Push notifications. Disabled silently if /etc/ups-dash/notify.json
        # is absent or has no topic, so this is inert until configured.
        self.notifier = Notifier("/etc/ups-dash/notify.json")
        self.notifier.start()
        self._notify_prev = None
        self.ring = collections.deque(maxlen=RING_SECONDS)
        self.services = {}
        self.subscribers = []

        self._lock = threading.RLock()
        self._snapshot = None
        self._box_data = None
        self._box_seen = 0.0
        self._boot_id = None
        self._timers = {"box": 0.0, "svc": 0.0, "idle": 0.0, "maint": 0.0}

    @property
    def tunables(self):
        """What is in effect: the sentinel's own keys from its heartbeat
        while fresh, the log/file fallback otherwise (tunables.py)."""
        return self.learner.live

    # ---- polling ------------------------------------------------------

    def _poll_services(self):
        self.services = services.poll(NANO_UNITS)

    def _poll_box(self, now):
        data = self.box.fetch()
        if data is None:
            return
        self._box_data = data
        self._box_seen = now
        self.learner.feed((data.get("governor") or {}).get("events"))
        bid = data.get("boot_id")
        if self._boot_id and bid and bid != self._boot_id:
            # boot_id survives hibernate and changes on a real reboot, so this
            # is the definitive "it cold-booted rather than resumed" signal.
            self.store.add_event(now, "box_rebooted", self.episodes.id,
                                 {"old": self._boot_id, "new": bid})
        self._boot_id = bid or self._boot_id

    def _box_state(self, now):
        if self._box_seen and now - self._box_seen < BOX_POLL * 3:
            return "awake", "agent responding"
        if now - self.learner.last_hibernate_signal < HIBERNATE_MEMORY:
            return "hibernated", "governor reported HIBERNATING"
        if not self._box_seen:
            return "unknown", "never seen since collector start"
        return "unreachable", self.box.last_error or "no response"

    # ---- the loop -----------------------------------------------------

    def run(self):
        self.tail.start()
        self._poll_services()
        while True:
            try:
                self._tick()
            except Exception as exc:
                print("[ups-dash] tick failed: %s: %s"
                      % (type(exc).__name__, exc), flush=True)
            time.sleep(UPS_POLL)

    def _due(self, key, now, period):
        if now - self._timers[key] >= period:
            self._timers[key] = now
            return True
        return False

    def _tick(self):
        now = time.time()
        self.learner.feed(self.tail.drain())
        if self._due("box", now, BOX_POLL):
            self._poll_box(now)
        if self._due("svc", now, SERVICE_POLL):
            self._poll_services()
        # The sentinel's facts, read once per tick: None unless its heartbeat
        # is fresh. Its tunables then win over the log and the file.
        sentinel = self.ledger_tick.begin(now)
        self.learner.peer(sentinel)

        ups = upsblock.build(self.nut.read())
        box_state, why = self._box_state(now)
        ram_gb = self.learner.last_ram_gb
        if ram_gb is None and self._box_data:
            ram_gb = (self._box_data.get("mem") or {}).get("used_gb")
        drain = derive.drain_rate(self.ring, now, ups.get("charge"))

        # Surfaced live so the UI can show a countdown and an abort button
        # while a scheduled output cut is still cancellable.
        ups_cut = upsoff.state()
        # A battery self-test shows OFF / DISCHRG (NUT #2104). `testing` is
        # the one answer to "does the test explain it" -- classify, derive,
        # episodes, the cause and the notifier all use the same one.
        self_test = self.extras.self_test(now, ups, ups_cut, box_state)
        testing = states.self_test_explains(self_test, ups_cut, ups)
        on_batt = ups.get("on_battery")
        if testing and on_batt is True:
            on_batt = False

        # WHY the box is down decides almost everything the user is told, so
        # attribute it before anything reads the state. The park phase as it
        # stood before this tick: a box powering itself on after a park is
        # not "you woke it" (cause.py).
        down_cause = self.cause.update(now, box_state, on_batt,
                                       self.park.snapshot(now))

        snap = {
            "ts": now,
            "ups": ups,
            "charge": ups.get("charge"),          # flat copy for drain_rate
            "box": {
                "state": box_state, "why": why,
                "seen": self._box_seen or None,
                "age": round(now - self._box_seen, 1) if self._box_seen else None,
                "vitals": self._box_data if box_state == "awake" else None,
                "last_vitals": self._box_data,
            },
            "nano": nanovitals.collect(),
            "down_cause": down_cause,
            "derived": derive.project(ups, box_state, ups.get("charge"),
                                      ups.get("runtime"), self.tunables,
                                      ram_gb, drain, down_cause, testing),
            "tunables": self.tunables,
            "services": self.services,
            "self_test": self_test,
            "transfer": self.extras.transfer(now, ups),
        }

        snap["ups_cut"] = ups_cut
        snap["wake_hold"] = hold.get_hold()
        # The park decides only now that the UPS and box readings exist, and
        # before classify(), so the state line and the marker the sentinel
        # reads describe the same tick. Never raises; slow calls are async.
        snap["park"] = self.park.tick(
            now, ups, box_state, down_cause, ups_cut, testing,
            snap["wake_hold"], self.tunables, self.episodes.id)
        self.episodes.update(now, ups, box_state, ups.get("charge"),
                             lambda since: self.ring_since(since), testing,
                             snap["park"].get("phase") is not None)
        snap["episode"] = self.episodes.snapshot(now)
        # The authoritative reading of what is happening. Every piece of
        # user-facing text -- state line, timeline, notifications -- is built
        # from this one call so they cannot drift apart.
        snap["state"] = states.classify(
            ups, box_state, down_cause, self.tunables, snap["episode"],
            snap["ups_cut"], snap["derived"].get("eta_hibernate_sec"),
            snap.get("wake_hold"), self_test, snap["park"], sentinel)
        # snap["ledger"], snap["view"] (what the browser renders instead of
        # re-deriving it), and a stale hold cleared (ledger_tick.py).
        self.ledger_tick.finish(snap, now, on_batt)
        # TRANSFER_CAUSE once per outage, and the UPS's own self-test
        # start/verdict, into the event log. Never raises.
        self.extras.record(self.store, self._notify_prev, snap,
                           self.episodes.id, testing, now)
        # Enqueue-only: all network I/O happens on the notifier's own thread,
        # so an unreachable ntfy server can never stall power monitoring.
        try:
            self.notifier.observe(self._notify_prev, snap, now)
        except Exception:
            pass
        self._notify_prev = snap

        self.ring.append(snap)
        with self._lock:
            self._snapshot = snap
        self._persist(snap, now)
        self._publish(snap)

        if self._due("maint", now, MAINTAIN_EVERY):
            try:
                self.store.maintain(now)
            except Exception:
                pass

    def _persist(self, snap, now):
        """Tiered writes -- see DASHBOARD.md §07.  Full 1 Hz only while an
        episode is open; otherwise one row per 30 s.  The SD card is the
        system's weakest component and must not be worn out by idle telemetry."""
        row = flatten(snap)
        if self.episodes.id is not None:
            self.store.insert_sample(row, 1, self.episodes.id)
        elif self._due("idle", now, IDLE_PERSIST):
            self.store.insert_sample(row, 30, None)

    # ---- fan-out ------------------------------------------------------

    def snapshot(self):
        with self._lock:
            return self._snapshot

    def ring_since(self, since):
        return [flatten(s) for s in self.ring if s["ts"] >= since]

    def subscribe(self, q):
        with self._lock:
            self.subscribers.append(q)

    def unsubscribe(self, q):
        with self._lock:
            if q in self.subscribers:
                self.subscribers.remove(q)

    def _publish(self, snap):
        with self._lock:
            subs = list(self.subscribers)
        for q in subs:
            try:
                if q.qsize() < 5:   # slow client: drop, never block the loop
                    q.put_nowait(snap)
            except Exception:
                pass
