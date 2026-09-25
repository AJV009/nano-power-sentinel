"""Client for box-agent on the W7900 box.

Pull rather than push (DASHBOARD.md D6): the jetson is already the controller,
so pulling lets it correlate "box unreachable" with "we just hibernated it" and
label the state correctly instead of alarming.

A failed fetch returns None.  The caller decides what absence MEANS -- this
module never guesses.
"""

import json
import urllib.error
import urllib.parse
import urllib.request

from .lanprobe import WHY as LAN_WHY

BOX_POLL = 5.0
HIBERNATE_MEMORY = 900     # how long a HIBERNATING log line explains absence


class BoxClient(object):
    def __init__(self, base_url, timeout=3.0):
        self.base = base_url.rstrip("/")
        self.timeout = timeout
        self.cursor = None
        self.last_error = None

    def fetch(self):
        """Return the vitals dict, or None if the box could not be reached."""
        url = self.base + "/vitals"
        if self.cursor:
            url += "?cursor=" + urllib.parse.quote(self.cursor)
        try:
            with urllib.request.urlopen(url, timeout=self.timeout) as resp:
                data = json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            self.last_error = "http %s" % exc.code
            return None
        except Exception as exc:
            # Distinguish the shapes of failure for the operator, but do NOT
            # try to infer box state here -- that is the collector's job.
            self.last_error = "%s: %s" % (type(exc).__name__, exc)
            return None
        self.last_error = None
        gov = data.get("governor") or {}
        if gov.get("cursor"):
            self.cursor = gov["cursor"]
        return data


class BoxStateMixin(object):
    """The collector's box half: poll box-agent, and say what the box is
    doing when it does not answer. Split out of collector.py (300-line cap);
    reads the Collector's fields (box, lan, learner, logbook, store, ...)."""

    def _poll_box(self, now):
        data = self.box.fetch()
        if data is None:
            return
        self._box_data = data
        self._box_seen = now
        # Logged whole; the learner gets hibernate-governor's lines only.
        self.learner.feed(self.logbook.box_lines(
            (data.get("governor") or {}).get("events"), self.episodes.id))
        bid = data.get("boot_id")
        if self._boot_id and bid and bid != self._boot_id:
            # boot_id survives hibernate and changes on a real reboot, so this
            # is the definitive "it cold-booted rather than resumed" signal.
            self.store.add_event(now, "box_rebooted", self.episodes.id,
                                 {"old": self._boot_id, "new": bid})
        self._boot_id = bid or self._boot_id

    def _box_state(self, now):
        agent = bool(self._box_seen) and now - self._box_seen < BOX_POLL * 3
        self.lan.want(not agent)
        if agent:
            return "awake", "agent responding"
        # Positive evidence of a running OS beats memory (lanprobe.py).
        os_ = self.lan.verdict(now)
        if os_ in ("windows", "firewalled"):
            return "other_os", LAN_WHY[os_]
        if now - self.learner.last_hibernate_signal < HIBERNATE_MEMORY:
            return "hibernated", "governor reported HIBERNATING"
        if not self._box_seen:
            return "unknown", "never seen since collector start"
        return "unreachable", (LAN_WHY["linux"] if os_ == "linux"
                               else self.box.last_error or "no response")
