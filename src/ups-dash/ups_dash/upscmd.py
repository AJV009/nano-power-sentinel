"""Authenticated UPS commands over the NUT protocol: instant commands and
RW variable writes.

READ THIS BEFORE CHANGING ANYTHING HERE.

This module can de-energise the UPS output. `load.off` / `load.off.delay`
reach register 0x15 (PowerSummary.DelayBeforeShutdown), and on THIS hardware
that is proven, by test on 2026-09-20, to latch OFF until a human intervenes:

    load.off.delay fired -> output cut -> mains restored -> ups.status = OL OFF

The UPS came back to life, charged, and talked over USB, but its output stayed
OFF waiting for a human to press the front-panel button.

Consequences of a successful load.off / load.off.delay:
  * the box loses power outright -- this is a hard cut, not a hibernate
  * no +5VSB, so Wake-on-LAN CANNOT bring it back
  * somebody has to be physically present to restore power

That is why the emergency modes in upsoff.py deliberately keep using
load.off.delay: a delay with a visible countdown and a working `shutdown.stop`
abort (see abort_cut() below) is the difference between a recoverable mistake
and being locked out of your own workstation until you get home. Do not
change that.

Bench-tested 2026-09-22 (docs/UPS-TOOLING.md §7): `shutdown.reboot` reaches a
DIFFERENT register -- 0x40 (APCGeneralCollection.APCDelayBeforeReboot) --
which cuts after a grace period and then RESTORES ON ITS OWN, but only when
armed while the output is ON (test 1: on mains, back ~4 s later; test 3: on
battery, back ~1 s after mains returns, no BR-family re-trigger loop seen).
Sent while the output is already latched OFF by 0x15 (test 2), it ACKs but
never arms -- `ups.timer.reboot` stays 0 and the output stays off. Nothing
software-side restores a 0x15 latch; only the front-panel button does. The
emergency modes in this file and in upsoff.py deliberately stay on
load.off.delay (0x15) regardless -- see the warning above.

The only safety net for a load.off.delay cut is `shutdown.stop`
(`abort_cut()` below), which cancels a PENDING delayed cut before it fires.

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


def _authenticate(sock, path):
    """USERNAME/PASSWORD handshake shared by every write below. Returns
    (ok, detail); detail is None on success, a reason string otherwise."""
    user, password = load_credentials(path)
    if not user or not password:
        return False, ("no UPS command credentials configured "
                       "(%s missing or incomplete)" % path)
    reply = _converse(sock, "USERNAME %s" % user)
    if not reply.startswith("OK"):
        return False, "username rejected: %s" % reply
    reply = _converse(sock, "PASSWORD %s" % password)
    if not reply.startswith("OK"):
        return False, "password rejected: %s" % reply
    return True, None


def _logout(sock):
    if sock is None:
        return
    try:
        sock.sendall(b"LOGOUT\n")
    except Exception:
        pass
    try:
        sock.close()
    except Exception:
        pass


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
    sock = None
    try:
        sock = socket.create_connection((host, port), timeout=TIMEOUT)
        sock.settimeout(TIMEOUT)
        ok, detail = _authenticate(sock, path)
        if not ok:
            return False, detail
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
        _logout(sock)


def setvar(name, value, ups=None, host=None, port=None, path=CONF):
    """Write one RW NUT variable. Returns (ok, detail). Never raises.

    Sends `SET VAR <ups> <name> "<value>"` -- the NUT protocol requires the
    value to be quoted, even a bare number. Same fresh-connection, same
    authentication as instcmd() (see _authenticate above); this is a write,
    not a read, and shares nothing with the collector's polling socket.
    """
    ups = ups or settings.UPS_NAME
    host = host or settings.NUT_HOST
    port = port or settings.NUT_PORT
    sock = None
    try:
        sock = socket.create_connection((host, port), timeout=TIMEOUT)
        sock.settimeout(TIMEOUT)
        ok, detail = _authenticate(sock, path)
        if not ok:
            return False, detail
        quoted = str(value).replace("\\", "\\\\").replace('"', '\\"')
        line = 'SET VAR %s %s "%s"' % (ups, name, quoted)
        reply = _converse(sock, line)
        if reply.startswith("OK"):
            return True, reply
        return False, reply
    except Exception as exc:
        return False, "%s: %s" % (type(exc).__name__, exc)
    finally:
        _logout(sock)


def cut_output(delay_sec):
    """Schedule the UPS output cut. Abortable via abort_cut() until it fires.

    Deliberately load.off.delay (register 0x15, latches OFF) and NOT
    shutdown.reboot (register 0x40) -- see the module docstring. The
    emergency modes in upsoff.py exist to guarantee a cut, not to guarantee
    a comeback."""
    return instcmd("load.off.delay", int(delay_sec))


def abort_cut():
    """Cancel a pending delayed cut. The only undo this hardware offers."""
    return instcmd("shutdown.stop")
