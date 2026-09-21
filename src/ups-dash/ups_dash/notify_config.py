"""Notifier config: /etc/ups-dash/notify.json, every key optional.

Same posture as tunables.py's LOCAL_PATH: one small JSON file under
/etc/ups-dash, read once, defaults for anything absent.

A missing file, an unreadable file, and a file with no `topic` all collapse
to the same outcome -- notifications are off.  That is the expected state on
a fresh install (nobody has an ntfy topic configured yet), not an error, so
this must never raise and never print anything.  Notifier is the one that
decides `enabled` from the returned dict; this module only ever hands back
values, never an exception.
"""

import json

PATH = "/etc/ups-dash/notify.json"

DEFAULTS = {
    "enabled": True,
    "server": "https://ntfy.sh",
    "topic": None,
    "token": None,
    "min_priority": "low",
    # Not part of the documented shape in the task, but every field here is
    # explicitly "optional, sane default" -- these two just cover where the
    # persisted queue lives and how big it may grow before dropping oldest.
    "queue_path": "/var/lib/ups-dash/notify_queue.jsonl",
    "max_queue": 200,
}

# ntfy's own priority words, low to high.  Used both for the `Priority:`
# header value and to compare an event's priority against `min_priority`.
PRIORITY_RANK = {"min": 1, "low": 2, "default": 3, "high": 4, "max": 5,
                 "urgent": 5}


def load(path=None):
    """Return a fully-populated config dict.  Never raises."""
    cfg = dict(DEFAULTS)
    try:
        with open(path or PATH) as fh:
            data = json.load(fh)
        if isinstance(data, dict):
            cfg.update(data)
    except Exception:
        pass   # no file / bad json / permissions -- defaults stand, and with
               # topic left at None the notifier disables itself downstream
    return cfg
