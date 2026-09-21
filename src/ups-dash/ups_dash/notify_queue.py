"""Disk-persisted, capped FIFO for outbound notifications.

WHY THIS EXISTS: during a power cut the household router dies with the
mains (it has no UPS of its own), at exactly the moment "MAINS LOST" needs
to go out.  There is nowhere to send it.  This queue is what lets that
notification survive the gap -- process restart included -- and go out
later, in order, once the tunnel comes back.

FRUGALITY (the SD card is this system's known weak point, see store.py):
  - enqueue is an O(1) append to a JSONL file, never a rewrite, on the
    overwhelmingly common path (queue not at cap).
  - dequeue (a message was sent, or the queue is being trimmed) removes the
    item from memory immediately but only rewrites the file every
    COMPACT_EVERY removals, or when the queue drains to empty. Removals
    happen at network-send pace (seconds, gated by backoff), never at the
    1 Hz collector pace, so this already costs nothing while the system is
    quiet. The batching on top means even a long-outage backlog drain -- the
    one time removals come in a burst -- writes the file a fraction as often
    as "once per item".
  - the tradeoff: if the process is killed between compactions, the on-disk
    file can still list a few already-sent items, and they get re-sent on
    the next restart. A duplicate ntfy push is a minor annoyance; a silently
    dropped one is the failure this whole module exists to prevent. At-least-
    once, deliberately, not exactly-once.
"""

import json
import os
import threading

COMPACT_EVERY = 20


class NotifyQueue(object):
    def __init__(self, path, max_len=200):
        self.path = path
        self.max_len = max_len
        self._lock = threading.Lock()
        self._items = []
        self._dropped = 0
        self._pops_since_compact = 0
        self._load()

    # ---- startup --------------------------------------------------------

    def _load(self):
        parent = os.path.dirname(self.path)
        try:
            if parent and not os.path.isdir(parent):
                os.makedirs(parent, 0o755)
        except Exception:
            pass

        items = []
        try:
            with open(self.path) as fh:
                for line in fh:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        items.append(json.loads(line))
                    except Exception:
                        continue   # one torn/corrupt line (mid-write power
                                   # loss) must not lose the rest of the queue
        except Exception:
            pass   # no queue file yet -- fresh start, nothing to replay

        if len(items) > self.max_len:
            # A restart found more backlog than the cap allows (e.g. the
            # daemon was down for the whole outage). Same rule as a live
            # overflow: drop oldest, note how many, keep the newest.
            self._dropped += len(items) - self.max_len
            items = items[-self.max_len:]
            self._compact_locked(items)
        self._items = items

    # ---- writes -----------------------------------------------------

    def push(self, item):
        """Append one item. Drops the oldest item (and counts it) if the
        queue is already at cap."""
        with self._lock:
            overflow = len(self._items) >= self.max_len
            if overflow:
                self._items.pop(0)
                self._dropped += 1
            self._items.append(item)
            if overflow:
                # The plain append below would leave a stale head line in
                # the file; a full rewrite is the only correct fix, and
                # overflow is rare enough that paying for one here is fine.
                self._compact_locked(self._items)
            else:
                try:
                    with open(self.path, "a") as fh:
                        fh.write(json.dumps(item) + "\n")
                except Exception:
                    pass   # best-effort persistence; the in-memory copy
                           # still holds, so delivery this run is unaffected

    def pending_drops(self):
        """Return and reset the count of items dropped for cap overflow
        since the last call, so the caller can surface one note about it."""
        with self._lock:
            n, self._dropped = self._dropped, 0
            return n

    # ---- reads / removal ----------------------------------------------

    def peek(self):
        """Return a copy of the head item, or None. Does not remove it --
        the sender must call pop() only after a confirmed send (or a
        confirmed permanent failure), so a crash mid-send just retries."""
        with self._lock:
            return dict(self._items[0]) if self._items else None

    def pop(self):
        with self._lock:
            if not self._items:
                return
            self._items.pop(0)
            self._pops_since_compact += 1
            if not self._items or self._pops_since_compact >= COMPACT_EVERY:
                self._compact_locked(self._items)
                self._pops_since_compact = 0

    def __len__(self):
        with self._lock:
            return len(self._items)

    # ---- internals ------------------------------------------------------

    def _compact_locked(self, items):
        """Rewrite the file from `items`. Caller holds _lock. Atomic via
        tmp+rename (same pattern as config.py's tunables writer) so a power
        loss mid-write leaves either the old file or the new one, never a
        half-written one."""
        tmp = self.path + ".tmp"
        try:
            with open(tmp, "w") as fh:
                for it in items:
                    fh.write(json.dumps(it) + "\n")
            os.replace(tmp, self.path)
        except Exception:
            pass   # worst case the file lags memory until the next
                   # compaction; never raise out of a background thread
