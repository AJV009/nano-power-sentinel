"""The I/O edges of park.py: the marker and outcome (two keys of the state
ledger), the worker for slow calls, and where the park tunables come from.

Split out of park.py for the 300-line cap, along the seam that matters for
testing: park.py is the phase machine and never touches a file, a socket or
a thread itself -- it calls what it was handed (a ledger.Ledger among them),
and these are the defaults it is handed. Every function here is a
never-raise wrapper, because all of it runs inside the collector's 1 Hz loop.
"""

import threading

from . import config, control, ledger, settings, upscmd

# NUT replies that mean "this driver has no such command" rather than "no".
# 2.7.4's usbhid-ups lacks shutdown.return; NUT master maps it to exactly the
# 0x40 = 1 write bench tests 1/3/4 proved, and so does shutdown.reboot 1.
UNSUPPORTED = ("CMD-NOT-SUPPORTED", "INVALID", "UNKNOWN")

FLOOR_RANGE = (15.0, 80.0)
DEFAULT_FLOOR = 35.0
DEFAULT_ENABLED = 1.0


# ---- the marker and the outcome: ledger keys (dash.json) ------------------
# The shapes are the old files' exactly: the marker {"phase", "armed_at",
# "charge", "floor", "hold_at_park", "cause_at_park", "returned_at",
# "rehibernate_sent"}, the outcome {"outcome", "ts", "armed_at",
# "returned_at", "charge"}. ups-sentinel reads the marker; nothing else does.

MARKER, OUTCOME = "park", "park_outcome"


def default_ledger():
    return ledger.default()


def as_marker(value):
    """None when there is no park; {} when present but unreadable -- still a
    park story, exactly as hold.get_hold() treats a hold (and the sentinel
    a marker)."""
    if value is None:
        return None
    return value if isinstance(value, dict) else {}


def as_outcome(value):
    return value if isinstance(value, dict) and value.get("outcome") else None


# ---- the two slow calls -------------------------------------------------

def arm_command(instcmd=None):
    """Arm the park. Returns (ok, {"cmd", "reply"}). Never raises.

    `shutdown.reboot 1` first: that exact command was bench-proven on THIS
    unit AND this driver (test 4, NUT master, 2026-09-22 -- cut after 61 s,
    zero drain parked, output back 3 s after mains). `shutdown.return` maps
    to the same register (0x40 = 1) in NUT master but was never run here, so
    it is only the fallback for a driver that lacks shutdown.reboot. Value
    1, never NUT's default 10: BX/XS firmware ACKs 10 and never acts on it.
    Any refusal other than "unsupported" (credentials, ACCESS-DENIED) is a
    refusal: the fallback would be refused the same way.
    NEVER load.off / load.off.delay here: 0x15 latches OFF until the front
    button is pressed (upscmd.py), which is the opposite of a park.
    """
    run = instcmd or upscmd.instcmd
    try:
        ok, reply = run("shutdown.reboot", 1)
        if ok:
            return True, {"cmd": "shutdown.reboot 1", "reply": reply}
        if not any(tok in str(reply).upper() for tok in UNSUPPORTED):
            return False, {"cmd": "shutdown.reboot 1", "reply": reply}
        ok2, reply2 = run("shutdown.return", None)
        return ok2, {"cmd": "shutdown.return",
                     "reply": "%s (shutdown.reboot 1: %s)" % (reply2, reply)}
    except Exception as exc:
        return False, {"cmd": "shutdown.reboot 1",
                       "reply": "%s: %s" % (type(exc).__name__, exc)}


def hibernator(box_url=None):
    """The default rehibernate call: box-agent POST /hibernate, <= 25 s."""
    url = box_url or settings.BOX_URL
    return lambda: control.box_hibernate(url)


ARP_TABLE = "/proc/net/arp"
ATF_COM = 0x2            # the entry resolved: something answered ARP


def lan_present(ip=None, table=ARP_TABLE):
    """Does the box's NIC answer ARP right now? The collector polls box-agent
    every 5 s, so the kernel keeps asking; a resolved entry means an OS (or
    at least a live NIC stack) is up at that address. A box stuck in its
    firmware never resolves -- 2026-09-25, not one reply in six hours. Never
    raises: unreadable reads as absent."""
    ip = ip or settings.BOX_IP
    try:
        with open(table) as fh:
            next(fh, None)                               # the header row
            for line in fh:
                cols = line.split()
                if len(cols) >= 3 and cols[0] == ip:
                    return bool(int(cols[2], 16) & ATF_COM)
    except Exception:
        pass
    return False


def spawn_thread(fn):
    """Run `fn` on a daemon thread. instcmd can take 3 x 8 s against a hung
    upsd and box_hibernate 25 s against a half-booted box; neither may stall
    the 1 Hz loop that feeds every alert."""
    threading.Thread(target=fn, name="ups-dash-park", daemon=True).start()


class Job(object):
    """One slow call in flight. `done`/`result` are read by the loop and set
    once by the worker (single assignments, safe under the GIL)."""

    def __init__(self, kind, fn, ctx, spawn):
        self.kind, self.ctx = kind, ctx
        self.done, self.result = False, (False, "not run")
        try:
            spawn(self._work(fn))
        except Exception as exc:
            self.result, self.done = (False, "could not start: %s" % exc), True

    def _work(self, fn):
        def work():
            try:
                res = fn()
            except Exception as exc:
                res = (False, "%s: %s" % (type(exc).__name__, exc))
            self.result = res
            self.done = True
        return work


# ---- tunables -----------------------------------------------------------

def _f(v):
    if isinstance(v, bool):
        return 1.0 if v else 0.0
    return float(v) if isinstance(v, (int, float)) else None


def _pick(key, sources, lo, hi, default):
    """First in-range value: the collector's learned tunables, then the file
    config.py writes, then the built-in default. An out-of-range value is
    skipped rather than clamped -- the same "advisory file" rule the
    sentinel applies to /etc/ups-dash/tunables.json."""
    for src in sources:
        v = _f(src.get(key))
        if v is not None and lo <= v <= hi:
            return v
    return default


def _highest(key, sources):
    vals = [_f(s.get(key)) for s in sources]
    vals = [v for v in vals if v is not None]
    return max(vals) if vals else None


def resolve(learned, file_vals):
    """The values park.py acts on. `learned` is collector.tunables;
    `file_vals` is config.read_local(). Defaults (enabled, 35 %) apply when
    neither says anything, per the park contract.

    The wake gate and mains stability feed only the guard's "keep it up"
    decision, so they take the HIGHER of the two sources: without a fresh
    sentinel heartbeat the learner can lag what runs (it once showed 50 %
    while the gate was 70 %), and a file edit lands before the reload. Erring
    high only means "back to sleep", after which the sentinel's own gate
    wakes the box -- never a box left up too early.
    """
    srcs = (learned if isinstance(learned, dict) else {},
            file_vals if isinstance(file_vals, dict) else {})
    return {
        "enabled": _pick("park_enabled", srcs, 0.0, 1.0, DEFAULT_ENABLED) >= 0.5,
        "floor": _pick("park_floor_pct", srcs, FLOOR_RANGE[0], FLOOR_RANGE[1],
                       DEFAULT_FLOOR),
        "wake": _highest("wake_charge_pct", srcs),
        "stable": _highest("mains_stable_sec", srcs),
        # The governor's reserve is only ever learned from its own log.
        "reserve": _f(srcs[0].get("reserve_pct")),
    }


def read_local():
    try:
        data = config.read_local()
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}
