#!/usr/bin/env python3
"""
The I/O edges of ups-sentinel: the site it talks to, and the calls that touch
the UPS, the network and the disk.

Split out of ups-sentinel for the 300-line cap, along the seam its tests use:
ups-sentinel keeps every decision, and imports these by name so that
tests/test_sentinel.py can replace each one on the sentinel module and drive
the real main() on a virtual clock. So nothing here decides anything, keeps
state (the caller passes in what must be remembered), or reads the clock.

Installed next to ups-sentinel in /usr/local/sbin by scripts/install.sh, which
renders the site placeholders below in every file of src/jetson/. Copying
either file onto the jetson raw would target the documentation-range box.
Stdlib only, Python 3.8, and it never imports ups-dash.
"""
import json, os, socket, subprocess

UPS               = "apc@localhost"
BOX_IP            = "10.0.0.20"
BOX_MAC           = "aa:bb:cc:dd:ee:ff"
BROADCAST         = "10.0.0.255"


def log(m):
    print("[ups-sentinel] " + m, flush=True)


def _upsc(var):
    try:
        r = subprocess.run(["upsc", UPS, var], capture_output=True, text=True, timeout=10)
        return r.stdout.strip() if r.returncode == 0 else None
    except Exception:
        return None


def ups_status():
    return _upsc("ups.status")


def ups_charge():
    v = _upsc("battery.charge")
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def box_up():
    return subprocess.run(["ping", "-c1", "-W2", BOX_IP],
                          stdout=subprocess.DEVNULL,
                          stderr=subprocess.DEVNULL).returncode == 0


def send_wol():
    mac = bytes.fromhex(BOX_MAC.replace(":", ""))
    pkt = b"\xff" * 6 + mac * 16
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
    for port in (9, 7):
        s.sendto(pkt, (BROADCAST, port))
    s.close()


def read_json(path):
    """A JSON-object file as a dict; None when the file does not exist; False
    when it is present but unreadable or not an object. Present is still
    evidence, so the caller decides how False fails safe. Never raises."""
    try:
        with open(path) as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else False
    except FileNotFoundError:
        return None
    except Exception:
        return False


def mtime(path):
    """The file's mtime, or None when it cannot be stat'ed. Never raises."""
    try:
        return os.stat(path).st_mtime
    except Exception:
        return None


def apply_tunables(path, spec, ns, log, memo, initial=False):
    """Load the dashboard-editable tunables file into `ns` (the sentinel's
    globals) under `spec` = {key: (global name, lo, hi)}. Never raises; a bad
    file or value leaves the current value untouched. Re-reads only when the
    file's mtime has moved from memo[0]. The policy -- which globals, what
    range -- is the sentinel's TUNABLE_SPEC; this is only the file."""
    try:
        m = os.stat(path).st_mtime
    except Exception:
        return                      # no file is a perfectly normal state
    if not initial and m == memo[0]:
        return
    memo[0] = m
    try:
        with open(path) as fh:
            data = json.load(fh)
        if not isinstance(data, dict):
            raise ValueError("top level is not an object")
    except Exception as exc:
        log("tunables file rejected (%s) - keeping current values" % exc)
        return
    changed = []
    for key, (name, lo, hi) in spec.items():
        if key not in data:
            continue
        try:
            val = float(data[key])
        except Exception:
            log("tunable %s: not a number - ignored" % key)
            continue
        if not (lo <= val <= hi):
            log("tunable %s=%s outside %g..%g - ignored" % (key, val, lo, hi))
            continue
        if ns[name] != val:
            changed.append("%s %g -> %g" % (key, ns[name], val))
            ns[name] = val
    if changed:
        log("tunables reloaded: " + "; ".join(changed))


def write_json(path, doc):
    """Atomic (tmp + os.replace): a reader never sees half a file. Creates the
    directory if it is missing -- systemd's RuntimeDirectory= normally has.
    The file is 0644 whatever the umask, so the unprivileged ups-dash can read
    it. RAISES on failure; the caller decides what a failed write costs."""
    os.makedirs(os.path.dirname(path), mode=0o755, exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w") as fh:
        json.dump(doc, fh)
    os.chmod(tmp, 0o644)
    os.replace(tmp, path)
