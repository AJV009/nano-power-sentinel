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
