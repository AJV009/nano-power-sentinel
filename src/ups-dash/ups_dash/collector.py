"""The collector loop.

  UPS poller   1 Hz   upsd on localhost
  box poller   5 s    box-agent, hard timeout
  journal tail        ups-sentinel + nut-*, read-only

Absence of the box is not self-explanatory, so it is correlated with what the
governor last said: a commanded hibernate reads as "asleep", anything else
reads as "unreachable".  Two different facts, never merged.
"""

import collections
import subprocess
import threading
import time

from . import derive, nanovitals, nut, upsblock
from .boxpoll import BoxClient
from .episodes import EpisodeTracker
from .journal import JournalTail
from .notify import Notifier
from .sample import flatten
from .tunables import Learner
from . import settings, states, upsoff
from .cause import CauseTracker

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
        self.learner = Learner()
        self.episodes = EpisodeTracker(store)
        self.cause = CauseTracker()
        # A restart must not turn "you cut the power" into "unknown reason":
        # the evidence is in the event log, so read it back.
        self.cause.recover_from_store(store)
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
        return self.learner.values

    # ---- polling ------------------------------------------------------

    def _poll_services(self):
        """Health, not just ActiveState.

        A Type=oneshot unit that ran and exited reads "inactive", which is
        correct rather than broken. Judging on is-active alone produces a
        permanent false alarm."""
        out = {}
        for unit in NANO_UNITS:
            try:
                proc = subprocess.run(
                    ["systemctl", "show", unit, "-p", "ActiveState",
                     "-p", "Type", "-p", "Result"],
                    stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=5)
                kv = {}
                for line in proc.stdout.decode().splitlines():
                    if "=" in line:
                        k, v = line.split("=", 1)
                        kv[k] = v
                state = kv.get("ActiveState", "unknown")
                out[unit] = {
                    "active": state,
                    "type": kv.get("Type", ""),
                    "result": kv.get("Result", ""),
                    "healthy": state in ("active", "activating")
                               or (kv.get("Type") == "oneshot"
                                   and kv.get("Result") == "success"),
                }
            except Exception:
                out[unit] = {"active": "unknown", "healthy": None}
        self.services = out

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

        ups = upsblock.build(self.nut.read())
        box_state, why = self._box_state(now)
        ram_gb = self.learner.last_ram_gb
        if ram_gb is None and self._box_data:
            ram_gb = (self._box_data.get("mem") or {}).get("used_gb")
        drain = derive.drain_rate(self.ring, now, ups.get("charge"))

        # WHY the box is down decides almost everything the user is told, so
        # attribute it before anything reads the state.
        down_cause = self.cause.update(now, box_state, ups.get("on_battery"))

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
                                      ram_gb, drain, down_cause),
            "tunables": self.tunables,
            "services": self.services,
        }

        self.episodes.update(now, ups, box_state, ups.get("charge"),
                             lambda since: self.ring_since(since))
        snap["episode"] = self.episodes.snapshot(now)
        # Surfaced live so the UI can show a countdown and an abort button
        # while a scheduled output cut is still cancellable.
        snap["ups_cut"] = upsoff.state()
        # The authoritative reading of what is happening. Every piece of
        # user-facing text -- state line, timeline, notifications -- is built
        # from this one call so they cannot drift apart.
        snap["state"] = states.classify(
            ups, box_state, down_cause, self.tunables, snap["episode"],
            snap["ups_cut"], snap["derived"].get("eta_hibernate_sec"))
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
