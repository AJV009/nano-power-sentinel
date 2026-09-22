"""The sentinel's half of the state ledger, read-only: its state.json.

Split out of ledger.py for the 300-line cap, along the one-writer-per-file
seam (docs/LEDGER.md): ledger.py WRITES ups-dash's dash.json; this only
READS the file ups-sentinel writes (settings.SENTINEL_STATE, tmpfs, on
change + a 30 s heartbeat). Fail-safe: missing, stale or unparseable is
None, never a guess -- ups-dash then falls back to its own sources.
"""

import json
import math
import os
import time

STALE_SEC = 90.0     # three missed heartbeats: dead, hung, or an old build


def _num(v):
    return (isinstance(v, (int, float)) and not isinstance(v, bool)
            and math.isfinite(v))       # json.load accepts NaN / Infinity


def pid_gone(pid):
    """True only when the pid certainly does not exist. EPERM -- a root
    process seen from the unprivileged ups-dash -- means it does."""
    try:
        if not _num(pid) or pid <= 0 or int(pid) != pid:
            return False
        os.kill(int(pid), 0)
    except ProcessLookupError:
        return True
    except Exception:
        return False
    return False


class PeerReader(object):
    def __init__(self, path):
        self.path = path
        self.status = None      # fresh | stale | dead | missing | unreadable
        self._key = self._facts = None

    def read(self, now=None):
        """(facts, age_sec). facts is None unless the heartbeat is fresh
        (<= STALE_SEC) and its pid is not known to be gone; age is None only
        when there is no telling (no file). `status` says which. Parsed only
        when the file changes, so an unchanged one costs a stat -- and the
        3600 snapshots in the ring share one dict. Never raises."""
        now = time.time() if now is None else now
        try:
            st = os.stat(self.path)
        except FileNotFoundError:
            self.status = "missing"
            return None, None
        except Exception:
            self.status = "unreadable"
            return None, None
        try:
            key = (st.st_ino, st.st_mtime_ns, st.st_size)
            if key != self._key:
                with open(self.path) as fh:
                    facts = json.load(fh)
                self._facts = facts if isinstance(facts, dict) else None
                self._key = key
            facts = self._facts
        except Exception:
            facts = None
        upd = (facts or {}).get("updated")
        if facts is None or not _num(upd):
            self.status = "unreadable"
            return None, round(max(0.0, now - st.st_mtime), 1)
        age = round(max(0.0, now - upd), 1)
        if age > STALE_SEC:
            self.status = "stale"
            return None, age
        if pid_gone(facts.get("pid")):
            self.status = "dead"
            return None, age
        self.status = "fresh"
        return facts, age
