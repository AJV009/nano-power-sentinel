"""Persistent NUT client.

Speaks the NUT wire protocol to upsd over TCP 3493.  Same approach as
hibernate-governor, which has been running against this upsd for a day.

THE CONTRACT: read() returns a dict on success and None on failure.  None
means "could not read", which is a THIRD STATE -- never fold it into
"on mains" or "on battery".  This exact bug shipped in ups-sentinel once
(NOTES.md 2026-09-20) where on_battery(None) was False, so a dead sensor
read as "we're fine".
"""

import socket


class NutClient(object):
    def __init__(self, host="127.0.0.1", port=3493, ups="apc", timeout=3.0):
        self.host = host
        self.port = port
        self.ups = ups
        self.timeout = timeout
        self._sock = None
        self._buf = b""

    def close(self):
        if self._sock is not None:
            try:
                self._sock.close()
            except Exception:
                pass
        self._sock = None
        self._buf = b""

    def _connect(self):
        sock = socket.create_connection((self.host, self.port), timeout=self.timeout)
        sock.settimeout(self.timeout)
        self._sock = sock
        self._buf = b""

    def _readline(self):
        while b"\n" not in self._buf:
            chunk = self._sock.recv(4096)
            if not chunk:
                raise IOError("upsd closed the connection")
            self._buf += chunk
        line, self._buf = self._buf.split(b"\n", 1)
        return line.decode("utf-8", "replace").rstrip("\r")

    def read(self):
        """Return {varname: value} or None.  Never raises."""
        try:
            if self._sock is None:
                self._connect()
            self._sock.sendall(("LIST VAR %s\n" % self.ups).encode("ascii"))
            out = {}
            started = False
            while True:
                line = self._readline()
                if line.startswith("BEGIN LIST VAR"):
                    started = True
                elif line.startswith("END LIST VAR"):
                    return out if started else None
                elif line.startswith("ERR"):
                    # e.g. ERR DATA-STALE while the driver is wedged.
                    self.close()
                    return None
                elif line.startswith("VAR "):
                    bits = line[4:].split(" ", 2)
                    if len(bits) == 3:
                        val = bits[2]
                        if len(val) >= 2 and val[0] == '"' and val[-1] == '"':
                            val = val[1:-1]
                        out[bits[1]] = val
        except Exception:
            self.close()
            return None


def as_float(vars_, key):
    if not vars_:
        return None
    try:
        return float(vars_[key])
    except Exception:
        return None


def as_int(vars_, key):
    v = as_float(vars_, key)
    return int(v) if v is not None else None


def status_flags(vars_):
    """ups.status is space-separated flags: OL, OB, LB, CHRG, DISCHRG, OFF, RB."""
    if not vars_:
        return []
    return (vars_.get("ups.status") or "").split()
