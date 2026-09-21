"""Push notifications via ntfy (https://github.com/binwiederhier/ntfy).

THE ONE RULE THAT MATTERS: nothing in here may block the 1 Hz collector
loop. `notify()` only builds a small dict and appends it to a disk-backed
queue (notify_queue.py) -- local, fast, no network. All network I/O -- the
actual HTTP POST to ntfy, with retry and backoff -- happens on a single
background daemon thread. A hung or unreachable ntfy server stalls that
thread, never the caller.

THE DESIGN PROBLEM THIS EXISTS FOR: during a power cut the Jetson stays up
(its own UPS) but the household internet almost certainly does not (the ISP
modem has none). So "MAINS LOST" is exactly the message most likely to be
generated at the one moment it cannot be sent. The queue persists it to
disk, the sender retries with backoff and replays in order once a route to
ntfy exists again, and every message carries its ORIGINAL event time in the
body -- because a "MAINS LOST" push that arrives 45 minutes late, formatted
identically to a live one, reads as a live emergency. That is worse than no
notification at all.

Transition detection (deciding WHEN to call notify()) lives in
notify_events.py as a pure function -- this module is just the transport.
"""

import json
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

from . import notify_config
from . import notify_events
from .notify_queue import NotifyQueue

SEND_TIMEOUT = 10.0     # a hung ntfy server ties up the sender thread, not
                         # the collector -- this just bounds how long
BASE_BACKOFF = 5.0
MAX_BACKOFF = 300.0     # 5 min ceiling -- long enough to stop hammering a
                         # dead route, short enough that reconnection is
                         # noticed promptly once the outage actually ends
POLL_INTERVAL = 2.0
LATE_WARN_SEC = 30.0    # below this, "just now" and "the real time" read
                         # the same to a human; above it, say so explicitly

# HTTP statuses that will never succeed on retry -- move on rather than
# retrying a malformed request forever and starving everything queued
# behind it. Everything else (429, 5xx, network errors) is retried.
NON_RETRYABLE = (400, 401, 403, 404, 413)

UPS_UNREADABLE_SUSTAINED_SEC = 120.0  # a single dropped NUT read (transient
                                       # ERR DATA-STALE) is normal; this is
                                       # long enough that it is not


class Notifier(object):
    def __init__(self, config_path=None):
        cfg = notify_config.load(config_path)
        self.server = (cfg.get("server") or "https://ntfy.sh").rstrip("/")
        self.topic = cfg.get("topic")
        self.token = cfg.get("token")
        self.min_priority = cfg.get("min_priority") or "low"
        # Missing/unset topic is the documented "silently disable" trigger,
        # same as a config file that failed to load at all.
        self.enabled = bool(cfg.get("enabled", True)) and bool(self.topic)
        # mains-alert debounce (see _suppressed)
        self._mains_value = None
        self._mains_since = None
        self._mains_pending = None

        self.queue = None
        if self.enabled:
            self.queue = NotifyQueue(cfg.get("queue_path"),
                                     int(cfg.get("max_queue") or 200))

        self._wake = threading.Event()
        self._thread = None
        self._ups_bad_since = None
        self._ups_sustained_sent = False

    # ---- the pure-transition convenience wrapper -----------------------

    def observe(self, prev, curr, now=None):
        """Feed one collector tick through notify_events.transitions() plus
        the one duration-based check (UPS unreadable) that a two-snapshot
        pure function structurally cannot do on its own, and queue whatever
        comes out. Safe to call every tick regardless of `enabled`."""
        if not self.enabled or curr is None:
            return
        now = now if now is not None else time.time()
        try:
            for item in notify_events.transitions(prev, curr):
                if item[0] in self.MAINS_EVENTS:
                    self._defer_mains(item, curr, now)
                    continue
                self.notify(*item)
            self._release_mains(curr, now)
            sustained = self._check_ups_sustained(curr, now)
            if sustained is not None:
                self.notify(*sustained)
        except Exception:
            pass

    # Mains events get a settling period; everything else fires immediately.
    MAINS_EVENTS = ("mains_lost", "mains_restored")
    MAINS_SETTLE = 4.0

    def _defer_mains(self, item, curr, now):
        """Park a mains alert until the condition has actually settled.

        WHY: cutting the UPS output makes this unit report OB for about a
        second while it switches, which produced a "Mains lost" alert
        immediately followed one second later by "Mains restored" -- while
        mains had never gone anywhere. False alarms are what train somebody
        to ignore the true one. hibernate-governor already debounces its own
        trigger for this reason (DEBOUNCE=3).

        ⚠ DEFERRED, NOT DROPPED. transitions() emits a mains event exactly
        once, on the edge. Simply suppressing that edge would discard it
        permanently, so a REAL outage would produce no alert whatsoever --
        far worse than the false alarm being fixed. The event is held with
        its original timestamp and released once the condition holds.
        """
        want = (item[0] == "mains_lost")      # the on_battery value that must persist
        self._mains_pending = (item, want, now)

    def _release_mains(self, curr, now):
        """Emit or discard a parked mains alert."""
        if not self._mains_pending:
            return
        item, want, parked_at = self._mains_pending
        on_batt = (curr.get("ups") or {}).get("on_battery")

        # An emergency cut in flight explains any transient by itself.
        if (curr.get("ups_cut") or {}).get("active"):
            self._mains_pending = None
            return
        if on_batt is None:
            return                              # unreadable: keep waiting, assert nothing
        if bool(on_batt) is not want:
            self._mains_pending = None          # it bounced back -- never real
            return
        if now - parked_at >= self.MAINS_SETTLE:
            self._mains_pending = None
            self.notify(*item)                  # original timestamp preserved

    def _check_ups_sustained(self, curr, now):
        ok = (curr.get("ups") or {}).get("ok")
        if ok:
            self._ups_bad_since = None
            self._ups_sustained_sent = False
            return None
        if self._ups_bad_since is None:
            self._ups_bad_since = now
            return None
        if (not self._ups_sustained_sent
                and now - self._ups_bad_since >= UPS_UNREADABLE_SUSTAINED_SEC):
            self._ups_sustained_sent = True
            return ("ups_unreadable", "UPS unreadable",
                    ("upsd has not returned a valid reading for over %ds."
                     % int(UPS_UNREADABLE_SUSTAINED_SEC)),
                    "high", ["warning", "grey_question"], curr.get("ts") or now)
        return None

    # ---- public API -----------------------------------------------------

    def notify(self, kind, title, message, priority="default", tags=None,
               ts=None):
        """Queue one notification. Never raises, never blocks on network."""
        if not self.enabled:
            return
        try:
            rank = notify_config.PRIORITY_RANK
            if rank.get(priority, 3) < rank.get(self.min_priority, 2):
                return   # below the configured floor -- not even queued
            item = {"kind": kind, "title": title, "message": message,
                    "priority": priority, "tags": list(tags or []),
                    "ts": ts if ts is not None else time.time()}
            self.queue.push(item)
            self._wake.set()

            dropped = self.queue.pending_drops()
            if dropped:
                # Tell the human a gap happened rather than let them assume
                # silence meant nothing occurred.
                self.queue.push({
                    "kind": "queue_overflow",
                    "title": "Notifications dropped while offline",
                    "message": ("%d older queued notification(s) were "
                                "dropped because the offline queue filled "
                                "up (cap=%d)." % (dropped, self.queue.max_len)),
                    "priority": "default", "tags": ["warning"],
                    "ts": time.time()})
        except Exception:
            pass

    def start(self):
        if not self.enabled or self._thread is not None:
            return
        self._thread = threading.Thread(target=self._sender_loop, daemon=True)
        self._thread.start()

    # ---- background sender ----------------------------------------------

    def _sender_loop(self):
        backoff = BASE_BACKOFF
        while True:
            try:
                item = self.queue.peek()
                if item is None:
                    self._wake.wait(POLL_INTERVAL)
                    self._wake.clear()
                    backoff = BASE_BACKOFF
                    continue

                result = self._send(item)
                if result == "ok":
                    self.queue.pop()
                    backoff = BASE_BACKOFF
                    continue   # drain the rest of the backlog promptly
                if result == "drop":
                    print("[ups-dash] notify: dropping unsendable item "
                          "kind=%s" % item.get("kind"), flush=True)
                    self.queue.pop()
                    continue
                # "retry": leave it at the head, back off, try again --
                # order is preserved, nothing behind it jumps the queue.
                time.sleep(backoff)
                backoff = min(backoff * 2, MAX_BACKOFF)
            except Exception:
                # Whatever just went wrong, the loop itself must not die --
                # that would silently turn off notifications for the rest
                # of the process's life.
                time.sleep(BASE_BACKOFF)

    def _send(self, item):
        try:
            url = self.server + "/" + urllib.parse.quote(self.topic)
            body = self._format_body(item).encode("utf-8")
            headers = {
                "Title": _ascii(item.get("title") or ""),
                "Priority": _ascii(item.get("priority") or "default"),
                "Tags": _ascii(",".join(item.get("tags") or [])),
            }
            if self.token:
                headers["Authorization"] = "Bearer %s" % self.token
            req = urllib.request.Request(url, data=body, headers=headers,
                                         method="POST")
            with urllib.request.urlopen(req, timeout=SEND_TIMEOUT) as resp:
                resp.read()
            return "ok"
        except urllib.error.HTTPError as exc:
            return "drop" if exc.code in NON_RETRYABLE else "retry"
        except Exception:
            # Includes URLError (no route, DNS failure, connection refused)
            # -- exactly the "modem is down" case this module is built for.
            return "retry"

    def _format_body(self, item):
        ts = item.get("ts")
        now = time.time()
        message = item.get("message") or ""
        if not ts:
            return message
        stamp = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(ts))
        body = "%s\n\nEvent time: %s" % (message, stamp)
        lag = now - ts
        if lag > LATE_WARN_SEC:
            body += "  (delivered %s late -- do not treat as live)" % (
                _fmt_duration(lag))
        return body


def _ascii(s):
    """HTTP headers travel as latin-1 under the hood; our own titles/tags
    are always plain ASCII, but this keeps a stray non-ASCII unit name from
    turning into a hard failure instead of a slightly-mangled header."""
    try:
        return s.encode("ascii", "ignore").decode("ascii")
    except Exception:
        return ""


def _fmt_duration(seconds):
    seconds = int(seconds)
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    if h:
        return "%dh%dm" % (h, m)
    if m:
        return "%dm%ds" % (m, s)
    return "%ds" % s
