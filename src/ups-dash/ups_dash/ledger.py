"""The state ledger, ups-dash's half (docs/LEDGER.md).

ONE WRITER PER FILE. This module is the only writer of
settings.LEDGER_DIR/dash.json -- the wake hold, the park marker and its
outcome, and why the box is down -- and ups-sentinel only reads it. The
sentinel's own facts live in settings.SENTINEL_STATE, written only by it;
peer() reads them (ledger_peer.py), fail-safe: missing, stale or unparseable
is None, never a guess.

WHY: three marker files, a log scraper and browser-side rules were three ways
of learning one fact, and they disagreed -- CONFIG showed a 50 % wake gate
while the sentinel ran 70 %, because the scraper missed a start line.

  get(key)           the value (a detached copy); None when unset
  set(key, value)    persists only on change; True once the value is on
                     disk. A failed write keeps the value in memory and is
                     retried by the next set() or tick()
  persisted(key)     the value as dash.json holds it right now (re-read)
  peer(now)          (facts|None, age_sec|None): the sentinel's state
  migrate_legacy()   fold the old wake-hold / park / park-outcome files in,
                     and delete them only once dash.json is on disk
  start()            collector startup: load, migrate, and ALWAYS re-persist
                     -- that is what heals a corrupt file

Writes are atomic (tmp + fsync + os.replace): the sentinel may read at any
moment and must never see half a file. Thread-safe (control actions reach
hold.py from the HTTP threads) and never raises -- it runs inside the
collector's 1 Hz loop.
"""

import json
import math
import os
import threading
import time

from . import settings
from .ledger_peer import PeerReader, STALE_SEC as PEER_STALE_SEC  # noqa: F401

SCHEMA = 1
WRITER = "ups-dash"
KEYS = ("hold", "park", "park_outcome", "cause")
UNREADABLE_HOLD = {"ts": None, "reason": "unreadable hold file"}
_BAD = object()


def _num(v):
    return (isinstance(v, (int, float)) and not isinstance(v, bool)
            and math.isfinite(v))       # json.load accepts NaN / Infinity


def _copy(v):
    """A detached JSON-shaped copy: memory holds exactly what a reader gets
    back from disk, and a caller mutating its dict afterwards (park.py does)
    cannot change the ledger behind its back -- or hide a change from set()."""
    return None if v is None else json.loads(json.dumps(v))


def _log(msg):
    print("[ups-dash] ledger: %s" % msg, flush=True)


def _legacy_value(key, path):
    """A legacy file read the way its old reader read it: a hold or a park
    marker present but unreadable is still a hold or a park."""
    try:
        with open(path) as fh:
            data = json.load(fh)
    except Exception:
        data = _BAD
    if key == "hold":
        if isinstance(data, dict):
            return data
        return dict(UNREADABLE_HOLD) if data is _BAD else {"ts": None, "reason": "?"}
    if key == "park":
        return data if isinstance(data, dict) else {}
    return data if isinstance(data, dict) and data.get("outcome") else None


class Ledger(object):
    def __init__(self, directory=None, peer_path=None, legacy=None):
        self.path = os.path.join(directory or settings.LEDGER_DIR, "dash.json")
        self._peer = PeerReader(peer_path or settings.SENTINEL_STATE)
        self.legacy = legacy if legacy is not None else (
            ("hold", settings.WAKE_HOLD), ("park", settings.PARK_MARKER),
            ("park_outcome", settings.PARK_OUTCOME))
        self.updated = None        # dash.json's "updated", once read or written
        self.lock = threading.RLock()    # re-entrant: a caller may hold it
        self._data = dict((k, None) for k in KEYS)
        self._loaded = self._dirty = self._failing = False

    # ---- ours: dash.json ------------------------------------------------

    def _load(self):
        self._loaded = True
        try:
            with open(self.path) as fh:
                doc = json.load(fh)
        except FileNotFoundError:
            return
        except Exception as exc:
            doc = exc
        if not isinstance(doc, dict):
            # Written atomically, so this is disk damage, and there is no
            # telling what it held. Start empty and let start() heal it: a
            # lost park only means the guard does not act, while an invented
            # one would hold the sentinel off a recovery for 30 min.
            _log("%s unreadable (%s) -- starting empty"
                 % (self.path, doc if isinstance(doc, Exception) else type(doc).__name__))
            return
        for k in KEYS:
            self._data[k] = _copy(doc.get(k))
        upd = doc.get("updated")
        self.updated = upd if _num(upd) else None

    def _persist(self):
        doc = dict(self._data, schema=SCHEMA, writer=WRITER, updated=time.time())
        tmp = self.path + ".tmp"
        try:
            os.makedirs(os.path.dirname(self.path), exist_ok=True)
            with open(tmp, "w") as fh:
                json.dump(doc, fh, sort_keys=True)
                fh.flush()
                os.fsync(fh.fileno())     # a hold must survive a jetson reboot
            os.replace(tmp, self.path)
        except Exception as exc:
            self._dirty = True
            if not self._failing:
                _log("cannot write %s (%s: %s) -- retrying every tick"
                     % (self.path, type(exc).__name__, exc))
            self._failing = True
            return False
        if self._failing:
            _log("%s written again" % self.path)
        self._dirty = self._failing = False
        self.updated = doc["updated"]
        return True

    def get(self, key):
        try:
            with self.lock:
                if not self._loaded:
                    self._load()
                return _copy(self._data.get(key))
        except Exception:
            return None

    def set(self, key, value):
        """Persist `value` under `key` if it changed. True once it is on disk
        (also when it already was). Never raises."""
        try:
            with self.lock:
                if not self._loaded:
                    self._load()
                if key not in self._data:
                    _log("refusing unknown key %r" % (key,))
                    return False
                new = _copy(value)
                if new == self._data[key] and not self._dirty:
                    return True
                self._data[key] = new
                return self._persist()
        except Exception as exc:
            _log("set %s failed: %s: %s" % (key, type(exc).__name__, exc))
            return False

    def persisted(self, key):
        """The value as dash.json holds it NOW -- re-read, not remembered.
        None when the file is missing or unreadable: a caller that needs a
        fact to be on disk (park_guard.py) must read that as "not there"."""
        try:
            with open(self.path) as fh:
                doc = json.load(fh)
            return doc.get(key) if isinstance(doc, dict) else None
        except Exception:
            return None

    @property
    def dirty(self):
        return self._dirty

    def tick(self):
        """Once per collector tick: retry a failed write. Never raises."""
        try:
            with self.lock:
                if self._dirty:
                    self._persist()
        except Exception:
            pass

    def migrate_legacy(self, force=False):
        """Fold the legacy files into dash.json, then delete them -- but only
        once dash.json is on disk: until then they are what the sentinel
        falls back to. A legacy file OLDER than dash.json's last write is a
        leftover whose delete failed before: deleted, never folded, or it
        would resurrect a hold ups-dash has since cleared. `force` writes
        dash.json even when nothing was folded (start()).
        Returns {path: what happened}. Never raises."""
        report = {}
        try:
            with self.lock:
                if not self._loaded:
                    self._load()
                for key, path in self.legacy:
                    try:
                        mtime = os.stat(path).st_mtime
                    except FileNotFoundError:
                        continue
                    except Exception as exc:
                        report[path] = "left alone, cannot stat (%s)" % exc
                        continue
                    if self.updated is not None and mtime <= self.updated:
                        report[path] = "leftover"
                    else:
                        self._data[key] = _legacy_value(key, path)
                        report[path] = "folded into %r" % key
                folded = any(w.startswith("folded") for w in report.values())
                ok = self._persist() if (force or folded) else not self._dirty
                for path, what in list(report.items()):
                    if what.startswith("left alone"):
                        continue
                    if not ok:
                        report[path] = what + ", kept: dash.json not written"
                        continue
                    try:
                        os.remove(path)
                        report[path] = what + ", deleted"
                    except FileNotFoundError:
                        pass
                    except Exception as exc:
                        report[path] = what + ", delete failed (%s)" % exc
                for path in sorted(report):
                    _log("legacy %s: %s" % (path, report[path]))
        except Exception as exc:
            _log("migration failed: %s: %s" % (type(exc).__name__, exc))
        return report

    def start(self):
        """Collector startup. Writes dash.json even when nothing changed, so
        a corrupt file is healed now rather than at the next change, which
        may be weeks away. True once it is on disk."""
        self.migrate_legacy(force=True)
        return not self._dirty

    # ---- theirs: the sentinel's state.json (ledger_peer.py) -------------

    @property
    def peer_status(self):
        """fresh | stale | dead | missing | unreadable, as of the last peer()."""
        return self._peer.status

    def peer(self, now=None):
        """(facts|None, age_sec|None): the sentinel's own state, None unless
        its heartbeat is fresh. Never raises."""
        return self._peer.read(now)


# ---- the process-wide ledger ----------------------------------------------
# hold.py, park.py, cause.py and the collector all share this one, so there
# is exactly one writer of dash.json in the process.

_default = None
_default_lock = threading.Lock()


def default():
    global _default
    with _default_lock:
        if _default is None:
            _default = Ledger()
        return _default


def get(key):
    return default().get(key)


def set(key, value):      # noqa: A001 -- the contract's name; builtin unused here
    return default().set(key, value)


def peer(now=None):
    return default().peer(now)


def migrate_legacy():
    return default().migrate_legacy()
