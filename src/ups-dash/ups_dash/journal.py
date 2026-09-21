"""Tail journald for the jetson-side units.

This is how the dashboard learns what ups-sentinel decided WITHOUT modifying
it (DASHBOARD.md principle 2).  The sentinel is verified, safety-critical code;
its log is a read-only interface.

Unmatched lines are kept rather than dropped, so a wording change degrades into
"I don't understand this line" instead of silent data loss.
"""

import json
import subprocess
import threading


class JournalTail(object):
    def __init__(self, units, maxlen=500):
        self.units = list(units)
        self._lock = threading.Lock()
        self._pending = []
        self._maxlen = maxlen
        self._proc = None
        self._thread = None
        self.alive = False

    def start(self):
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def _run(self):
        cmd = ["journalctl", "-f", "-o", "json", "-n", "0", "--no-pager"]
        for unit in self.units:
            cmd += ["-u", unit]
        while True:
            try:
                self._proc = subprocess.Popen(
                    cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
                self.alive = True
                for raw in self._proc.stdout:
                    try:
                        rec = json.loads(raw.decode("utf-8", "replace"))
                    except Exception:
                        continue
                    msg = rec.get("MESSAGE")
                    if isinstance(msg, list):
                        msg = "".join(chr(b) for b in msg)
                    if not msg:
                        continue
                    ts = rec.get("__REALTIME_TIMESTAMP")
                    try:
                        ts = int(ts) / 1000000.0
                    except Exception:
                        ts = None
                    with self._lock:
                        self._pending.append({
                            "ts": ts,
                            "unit": rec.get("_SYSTEMD_UNIT") or "?",
                            "msg": msg,
                        })
                        if len(self._pending) > self._maxlen:
                            del self._pending[:-self._maxlen]
            except Exception:
                pass
            self.alive = False
            # journalctl died (log rotation, systemd restart).  Retry rather
            # than leaving the dashboard permanently deaf to the sentinel.
            import time
            time.sleep(5)

    def drain(self):
        with self._lock:
            out = self._pending
            self._pending = []
        return out
