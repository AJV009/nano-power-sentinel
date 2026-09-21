"""Hibernate on request -- the only privileged thing this agent does.

`systemctl hibernate` talks to logind over D-Bus, which polkit governs. The
agent runs with NoNewPrivileges=yes, so sudo (setuid) cannot work here; the
authorisation has to come from a polkit rule for this user and this one
action. Until such a rule is installed, can_hibernate() reports False and the
dashboard disables the button rather than offering something that will fail.

There is no "power off" and no arbitrary command. Hibernate is the only
action this module can express.
"""

import os
import subprocess

ACTION = "org.freedesktop.login1.hibernate"
TIMEOUT = 20


def hibernate():
    """Returns (ok, detail). Never raises."""
    try:
        proc = subprocess.run(["systemctl", "hibernate"],
                              stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                              timeout=TIMEOUT)
    except subprocess.TimeoutExpired:
        # Not necessarily failure: once the freeze starts this call simply
        # stops being answered. Treat it as "probably began, verify by absence".
        return True, "no response within %ds - the freeze likely began" % TIMEOUT
    except Exception as exc:
        return False, "%s: %s" % (type(exc).__name__, exc)
    out = proc.stdout.decode("utf-8", "replace").strip()
    return proc.returncode == 0, out or ("exit %d" % proc.returncode)


def can_hibernate():
    """Check authorisation WITHOUT hibernating, so the UI can tell the truth
    about whether the button would work before anyone presses it."""
    try:
        proc = subprocess.run(
            ["pkcheck", "--action-id", ACTION, "--process", str(os.getpid())],
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=8)
        return proc.returncode == 0
    except Exception:
        return None
