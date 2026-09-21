"""Authenticated UPS instant commands over the NUT protocol.

READ THIS BEFORE CHANGING ANYTHING HERE.

This module can de-energise the UPS output, and on THIS hardware that is not
remotely reversible. Proven by test on 2026-09-20:

    load.off.delay fired -> output cut -> mains restored -> ups.status = OL OFF

The UPS came back to life, charged, and talked over USB, but its output stayed
OFF waiting for a human to press the front-panel button. `upscmd -l apc` offers
no `load.on` and no `shutdown.return`, so there is no software path back.

Consequences of a successful load.off:
  * the box loses power outright -- this is a hard cut, not a hibernate
  * no +5VSB, so Wake-on-LAN CANNOT bring it back
  * somebody has to be physically present to restore power

The only safety net is `shutdown.stop`, which cancels a PENDING delayed cut.
That is why the delayed form is the default and the immediate form is not
offered at all: a delay with a visible countdown and a working abort is the
difference between a recoverable mistake and being locked out of your own
workstation until you get home.

Credentials live in /etc/ups-dash/upscmd.json (mode 640, owned by the service
user) because /etc/nut/killer.secret is root-only and unreadable by this
unprivileged daemon.
"""

import json
import socket

from . import settings

CONF = settings.UPSCMD_CONF
TIMEOUT = 8.0


def load_credentials(path=CONF):
    """Returns (user, password) or (None, None). Never raises."""
    try:
        with open(path) as fh:
            data = json.load(fh)
        return data.get("user"), data.get("password")
    except Exception:
        return None, None


def _converse(sock, line):
    sock.sendall((line + "\n").encode("ascii"))
    buf = b""
    while b"\n" not in buf:
        chunk = sock.recv(4096)
        if not chunk:
            raise IOError("upsd closed the connection")
        buf += chunk
    return buf.split(b"\n", 1)[0].decode("utf-8", "replace").strip()


def instcmd(command, arg=None, ups=None, host=None, port=None,
            path=CONF):
    """Run one instant command. Returns (ok, detail). Never raises.

    A fresh connection per command on purpose: these are rare, deliberate,
    high-consequence actions, and sharing the collector's long-lived polling
    socket would risk one interfering with the other.
    """
    ups = ups or settings.UPS_NAME
    host = host or settings.NUT_HOST
    port = port or settings.NUT_PORT
    user, password = load_credentials(path)
    if not user or not password:
        return False, ("no UPS command credentials configured "
                       "(%s missing or incomplete)" % path)
    sock = None
    try:
        sock = socket.create_connection((host, port), timeout=TIMEOUT)
        sock.settimeout(TIMEOUT)
        reply = _converse(sock, "USERNAME %s" % user)
        if not reply.startswith("OK"):
            return False, "username rejected: %s" % reply
        reply = _converse(sock, "PASSWORD %s" % password)
        if not reply.startswith("OK"):
            return False, "password rejected: %s" % reply
        line = "INSTCMD %s %s" % (ups, command)
        if arg is not None:
            line += " %s" % arg
        reply = _converse(sock, line)
        if reply.startswith("OK"):
            return True, reply
        return False, reply
    except Exception as exc:
        return False, "%s: %s" % (type(exc).__name__, exc)
    finally:
        if sock is not None:
            try:
                sock.sendall(b"LOGOUT\n")
            except Exception:
                pass
            try:
                sock.close()
            except Exception:
                pass


def cut_output(delay_sec):
    """Schedule the UPS output cut. Abortable via abort_cut() until it fires."""
    return instcmd("load.off.delay", int(delay_sec))


def abort_cut():
    """Cancel a pending delayed cut. The only undo this hardware offers."""
    return instcmd("shutdown.stop")
