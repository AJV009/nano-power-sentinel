"""Drive the REAL sentinel main() loop through scripted scenarios on a virtual
clock. Only the I/O edges are replaced; the decision logic is the shipped code."""
import importlib.machinery, importlib.util, sys, types

import os
SRC = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "src", "jetson", "ups-sentinel")

def load():
    loader = importlib.machinery.SourceFileLoader("sentinel", SRC)
    spec = importlib.util.spec_from_loader("sentinel", loader)
    m = importlib.util.module_from_spec(spec); loader.exec_module(m); return m

class Stop(Exception): pass

def run(name, scenario, until, expect_wol, extra=None):
    m = load()
    clock = {"t": 0.0}
    logs, wols = [], []
    m.load_tunables = lambda initial=False: None
    m.WAKE_INTERVAL = (extra or {}).get("interval", m.WAKE_INTERVAL)
    m.time = types.SimpleNamespace(time=lambda: clock["t"],
                                   sleep=lambda s: _sleep(s))
    def _sleep(s):
        clock["t"] += s
        if clock["t"] > until: raise Stop()
    def at(): return scenario(clock["t"])
    m.ups_status = lambda: at()["st"]
    m.box_up     = lambda: at()["up"]
    m.ups_charge = lambda: at().get("charge", 80.0)
    m.hold_age   = lambda: (10.0 if at().get("hold") else None)
    m.send_wol   = lambda: wols.append(clock["t"])
    m.log        = lambda msg: logs.append((clock["t"], msg))
    try: m.main()
    except Stop: pass
    exhausted = [t for t, msg in logs if "exhausted" in msg]
    got = len(wols) > 0
    ok = (got == expect_wol) and not ((extra or {}).get("no_exhaust") and exhausted)
    print("  %s  %-58s wol=%-2d exhausted=%s" %
          ("PASS" if ok else "FAIL", name, len(wols), bool(exhausted)))
    return ok

OB, OL = "OB DISCHRG", "OL CHRG"
results = []

# A. THE BUG: hibernated by hand at t<0, outage at 100, mains back at 300.
results.append(run("A. box already off before the outage -> must NOT wake",
    lambda t: {"st": OB if 100 <= t < 300 else OL, "up": False}, 700, False))

# B. REGRESSION: the normal path must still work.
results.append(run("B. box up at outage, governor hibernates it -> MUST wake",
    lambda t: {"st": OB if 100 <= t < 300 else OL, "up": t < 150 or t > 900}, 700, True))

# C. FLAP: outage takes the box down, mains blips, outage again, then back.
def flap(t):
    if 100 <= t < 300: st = OB
    elif 300 <= t < 310: st = OL          # 10 s blip, box still asleep
    elif 310 <= t < 500: st = OB
    else: st = OL
    return {"st": st, "up": t < 150 or t > 1200}
results.append(run("C. flapping mains while the box sleeps -> MUST still wake", flap, 1000, True))

# D. HOLD: up at outage, you hibernate it DURING the outage.
results.append(run("D. hibernated by hand during the outage (hold) -> must NOT wake",
    lambda t: {"st": OB if 100 <= t < 300 else OL, "up": t < 150,
               "hold": t >= 140}, 700, False))

# E. RESUME GRACE: 10 s interval, box takes ~49 s to resume after first packet.
def slow(t):
    return {"st": OB if 100 <= t < 300 else OL, "up": t < 150 or t >= 478}
results.append(run("E. 10s retries, slow resume -> wake, NO false 'exhausted'",
    slow, 600, True, {"interval": 10.0, "no_exhaust": True}))

print("\n  %d/%d passed" % (sum(results), len(results)))
sys.exit(0 if all(results) else 1)
