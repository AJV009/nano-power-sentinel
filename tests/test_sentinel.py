"""Drive the REAL sentinel main() loop through scripted scenarios on a virtual
clock. Only the I/O edges are replaced; the decision logic is the shipped code.

The ledger scenarios (I onwards) replace nothing on the read side: the real
hold/park readers run against real files in a temp dir, on the virtual clock.
SENTINEL_SRC=<path> runs everything against another build (e.g. the
pre-ledger one) to show which scenarios it fails."""
import atexit, importlib.machinery, importlib.util, json, shutil, sys, tempfile, types

import os
JETSON = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "src", "jetson")
sys.path.insert(0, JETSON)                      # its sibling, ups_sentinel_io
SRC = os.environ.get("SENTINEL_SRC") or os.path.join(JETSON, "ups-sentinel")

def load():
    loader = importlib.machinery.SourceFileLoader("sentinel", SRC)
    spec = importlib.util.spec_from_loader("sentinel", loader)
    m = importlib.util.module_from_spec(spec); loader.exec_module(m); return m

class Stop(Exception): pass

def tmpdir():
    d = tempfile.mkdtemp(prefix="sentinel-test-")
    atexit.register(shutil.rmtree, d, True)
    return d

def paths():
    d = tmpdir()
    return types.SimpleNamespace(ledger=os.path.join(d, "ledger", "dash.json"),
        hold=os.path.join(d, "wake-hold"), park=os.path.join(d, "park"),
        run=os.path.join(d, "run"))

def put(path, text, mtime=None):
    """Make `path` hold `text`; rewritten only when that changes."""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    try:
        with open(path) as fh:
            if fh.read() == text: return
    except FileNotFoundError: pass
    with open(path, "w") as fh: fh.write(text)
    if mtime is not None: os.utime(path, (mtime, mtime))

def gone(path):
    if os.path.exists(path): os.remove(path)

def dash(hold=None, park=None):
    return json.dumps({"schema": 1, "writer": "ups-dash", "updated": 0.0, "hold": hold,
                       "park": park, "park_outcome": None, "cause": None})

def state_json(p):
    try:
        with open(os.path.join(p.run, "state.json")) as fh: return json.load(fh)
    except Exception: return None

def run(name, scenario, until, expect_wol, extra=None):
    extra = extra or {}
    m = load()
    p = extra.get("paths") or paths()
    clock = {"t": extra.get("t0", 0.0)}
    logs, wols, pubs = [], [], []
    m.load_tunables = lambda initial=False: None
    m.WAKE_INTERVAL = extra.get("interval", m.WAKE_INTERVAL)
    jump = extra.get("jump") or (lambda t: 0.0)     # wall-clock step at t
    m.time = types.SimpleNamespace(time=lambda: clock["t"] + jump(clock["t"]),
                                   monotonic=lambda: clock["t"],
                                   sleep=lambda s: _sleep(s))
    def _sleep(s):
        clock["t"] += s
        if clock["t"] > until: raise Stop()
    def at(): return scenario(clock["t"])
    m.ups_status = lambda: at()["st"]
    m.box_up     = lambda: at()["up"]
    m.ups_charge = lambda: at().get("charge", 80.0)
    # Every path goes to a temp dir. The readers are stubbed unless the
    # scenario is about the ledger, which exercises the real ones.
    m.LEDGER_FILE, m.WAKE_HOLD, m.PARK_MARKER, m.RUN_DIR = p.ledger, p.hold, p.park, p.run
    if not extra.get("real_ledger"):
        m.hold_age   = lambda: (10.0 if at().get("hold") else None)
        m.park_phase = lambda: at().get("park")
    real_publish = getattr(m, "publish", None)
    if real_publish is not None:               # record every pass + the file after it
        def spy(facts):
            real_publish(facts)
            pubs.append((clock["t"], facts, state_json(p)))
        m.publish = spy
    m.send_wol   = lambda: wols.append(clock["t"])
    m.log        = lambda msg: logs.append((clock["t"], msg))
    why = ""
    try: m.main()
    except Stop: pass
    except Exception as exc: why = "crashed: %r" % exc
    exhausted = [t for t, msg in logs if "exhausted" in msg]
    got = len(wols) > 0
    ok = not why and (got == expect_wol) and not (extra.get("no_exhaust") and exhausted)
    check = extra.get("check")
    if check is not None:
        ok = ok and check(logs, wols)
    verify = extra.get("verify")                # returns "" or why it failed
    ctx = types.SimpleNamespace(m=m, p=p, logs=logs, wols=wols, pubs=pubs, note="")
    if ok and verify is not None:
        try: why = verify(ctx)
        except Exception as exc: why = "verify raised %r" % exc
        ok = not why
    print("  %s  %-58s wol=%-2d exhausted=%s%s" %
          ("PASS" if ok else "FAIL", name, len(wols), bool(exhausted),
           "\n        -> " + why if why else ""))
    if ctx.note: print("        " + ctx.note)
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


# F-H. BATTERY-FLOOR PARK (2026-09-22). ups-dash owns the park; the sentinel
# only reads its marker. After a park the box powers itself ON with the mains
# (BIOS AC BACK) and ups-dash sends it back to sleep. The sentinel must not
# take that power-on as "the box is back" (it would stand down and never wake
# it), and must not wake it while ups-dash is still sorting it out.
def park_story(t, clear_at=780):
    if 100 <= t < 400: st = OB                    # outage; box hibernates at 150
    elif 400 <= t < 600: st = None                # parked: the UPS switched itself off
    else: st = OL                                 # mains back at 600
    park = None
    if 400 <= t < 600: park = "parked"
    elif 600 <= t < clear_at: park = "returning"
    up = t < 150 or 660 <= t < 720 or t > 1500    # AC BACK power-on, re-hibernated
    return {"st": st, "up": up, "park": park,
            "charge": 40.0 if t < 1000 else 75.0}
results.append(run("F. park: AC-BACK power-on neither stands down nor wakes early",
    park_story, 1400, True, {"check": lambda logs, wols:
        not any("mains is back - standing down" in m for _, m in logs)
        and all(t >= 1000 for t in wols)}))

results.append(run("G. park marker left behind (ups-dash died) -> stale, still wakes",
    lambda t: dict(park_story(t, clear_at=10**9), up=(t < 150 or 660 <= t < 720)), 2700, True,
    {"check": lambda logs, wols: all(t >= 600 + 1800 for t in wols)}))

results.append(run("H. park with a box that was already off -> never woken",
    lambda t: dict(park_story(t), up=(660 <= t < 720)), 1400, False))


# I-O. THE LEDGER (docs/LEDGER.md). The sentinel reads ups-dash's dash.json
# (legacy files while it is absent) with the REAL readers, and publishes its
# own state.json. It never writes, or deletes, anything of ups-dash's.
def outage(t):                  # box up at the outage, hibernated at 150
    return {"st": OB if 100 <= t < 300 else OL, "up": t < 150}

def deferred(want):             # the last published pass deferred for `want`
    return lambda c: "" if c.pubs and c.pubs[-1][2] and c.pubs[-1][2]["deferring"] == want \
        else "last published deferring is not %r" % want

P = paths()
def i_story(t):
    put(P.ledger, dash(hold={"ts": 140.0, "reason": "hibernate"} if t >= 140 else None))
    return outage(t)
results.append(run("I. hold placed in dash.json during the outage -> must NOT wake",
    i_story, 700, False, {"paths": P, "real_ledger": True, "verify": deferred("hold")}))

P2 = paths()
def j_story(t):
    if 140 <= t < 800: put(P2.hold, '{"ts": 140, "reason": "hibernate"}', mtime=140.0)
    else: gone(P2.hold)
    return outage(t)
results.append(run("J. no dash.json: legacy wake-hold honoured, wakes once removed",
    j_story, 900, True, {"paths": P2, "real_ledger": True,
    "verify": lambda c: "" if all(t >= 800 for t in c.wols) else "woke under the hold"}))

P3 = paths(); put(P3.ledger, '{"schema": 1, "hold": nul')
results.append(run("K. corrupt dash.json -> held (and parked), never woken, no crash",
    outage, 2400, False, {"paths": P3, "real_ledger": True, "verify": lambda c:
        deferred("hold")(c) or ("" if any(d and d["deferring"] == "park:?" for _, _, d
                                          in c.pubs) else "corrupt park not read as '?'")}))

KEYS = {"schema", "writer", "updated", "pid", "started", "state", "outage_seen",
        "ups_status", "ol_since", "gate", "wake", "deferring", "tunables"}
def verify_l(c):
    docs = [(t, f, d) for t, f, d in c.pubs if d]
    if not docs: return "no state.json was ever written"
    last, m = docs[-1][2], c.m
    if set(last) != KEYS: return "keys differ from LEDGER.md: %s" % sorted(set(last) ^ KEYS)
    if (last["schema"], last["writer"], last["pid"]) != (1, "ups-sentinel", os.getpid()):
        return "bad schema/writer/pid"
    if last["tunables"] != {k: float(getattr(m, n)) for k, (n, _, _) in m.TUNABLE_SPEC.items()}:
        return "tunables are not the live module values"
    for t, f, d in docs:
        if d["updated"] > t: return "file from the future at %s" % t
        g = d["gate"]
        if d["state"] == "recovering" and (not g or g["need_charge"] != m.WAKE_CHARGE_PCT
                or g["need_stable_sec"] != m.MAINS_STABLE_SEC): return "bad gate at %s" % t
        if d["state"] == "online" and g is not None: return "gate while online at %s" % t
    for (t0, f0, _), (t, f, d) in zip(c.pubs, c.pubs[1:]):
        if f["state"] != f0["state"] and (d["state"], d["updated"]) != (f["state"], t):
            return "state change at %s not written at once" % t
    seq = [d["state"] for _, _, d in docs]
    seq = [s for i, s in enumerate(seq) if i == 0 or s != seq[i - 1]]
    if seq != ["online", "onbatt", "recovering", "online"]: return "states %s" % seq
    if {d["updated"] for t, _, d in docs if t <= 35} != {0.0, 30.0}:
        return "no 30 s heartbeat (or rewritten on every pass)"
    if not any(d["gate"] and d["gate"]["open"] and d["wake"]["last_wol"] == c.wols[0]
               for _, _, d in docs): return "never published an open gate + the WoL"
    if not any(d["deferring"] == "blind" and d["ups_status"] is None for _, _, d in docs):
        return "blind pass not published"
    wol = [d for t, _, d in docs if t == c.wols[0]][0]
    c.note = "state.json on the WoL pass: %s\n        at the end: %s" % (
        json.dumps(wol), json.dumps(last))
    return ""

P4 = paths(); put(P4.ledger, dash())
results.append(run("L. publishes state.json: schema, gate, rewrite on change, heartbeat",
    lambda t: {"st": OB if 100 <= t < 300 else None if 640 <= t < 650 else OL,
               "up": t < 150 or t >= 560, "charge": 40.0 if t < 500 else 80.0},
    700, True, {"paths": P4, "real_ledger": True, "verify": verify_l}))

def stale_story(t):             # box up an hour+ past the hold, then an outage
    return {"st": OB if 4000 <= t < 4200 else OL, "up": t < 4050 or t >= 4400}
def kept(path, text=None):
    def v(c):
        if not os.path.exists(path): return "the hold file was DELETED"
        if text is not None and open(path).read() != text: return "the hold file changed"
        n = sum("stale wake hold" in msg for _, msg in c.logs)
        return "" if n == 1 else "stale hold logged %d times, want 1" % n
    return v

P5 = paths(); put(P5.hold, '{"ts": 100, "reason": "hibernate"}', mtime=100.0)
results.append(run("M. stale legacy hold (box up 1h+) -> ignored, wakes, NOT deleted",
    stale_story, 4500, True, {"paths": P5, "real_ledger": True, "verify": kept(P5.hold)}))

P6 = paths(); put(P6.ledger, dash(hold={"ts": 100.0, "reason": "hibernate"}))
def n_story(t):                 # a NEW hold in the second outage is honoured
    if t >= 4640: put(P6.ledger, dash(hold={"ts": 4640.0, "reason": "hibernate"}))
    st = OB if 4000 <= t < 4200 or 4600 <= t < 4800 else OL
    return {"st": st, "up": t < 4050 or 4400 <= t < 4650}
results.append(run("N. stale dash.json hold ignored; a NEW hold after it is honoured",
    n_story, 5200, True, {"paths": P6, "real_ledger": True, "verify": lambda c:
        "" if all(t < 4600 for t in c.wols) else "woke under the new hold"}))

P7 = paths(); put(P7.hold, '{"ts": 100, "reason": "hibernate"}', mtime=100.0)
results.append(run("O. restarted at an outage with a stale hold, box up -> MUST wake",
    lambda t: {"st": OB if t < 5200 else OL, "up": t < 5050}, 5500, True,
    {"paths": P7, "real_ledger": True, "t0": 5000.0, "verify": kept(P7.hold)}))

# P. Every scenario above stubs load_tunables. Its file handling now lives in
# ups_sentinel_io: it must still land in THIS module's globals (which main()
# reads and state.json reports), and a bad file must still change nothing.
# Q. CLOCK JUMP (09-24): no RTC battery, NTP stepped +3 h. Still 120 s monotonic.
results.append(run("Q. wall clock jumps +3h mid-window -> WoL still waits 120s",
    lambda t: {"st": OB if 100 <= t < 300 else OL, "up": t < 150},
    700, True, {"jump": lambda t: 10800.0 if t >= 320 else 0.0,
                "check": lambda logs, wols: bool(wols) and wols[0] >= 420}))
# R. TWO OUTAGES, box off by hand (09-25): the second used to go unlogged.
results.append(run("R. second outage while off by hand is logged, never woken",
    lambda t: {"st": OB if (100 <= t < 200 or 300 <= t < 400) else OL, "up": False},
    700, False, {"check": lambda logs, wols:
                 sum("MAINS LOST" in msg for _, msg in logs) == 2}))

def tunables():
    m, logs = load(), []
    m.log = lambda msg: logs.append(msg)
    f = m.TUNABLES_FILE = os.path.join(tmpdir(), "tunables.json")
    put(f, json.dumps({"wake_charge_pct": 55, "wake_tries": 99, "mains_stable_sec": "x"}))
    m.load_tunables(initial=True)
    ok = (m.WAKE_CHARGE_PCT, m.WAKE_TRIES, m.MAINS_STABLE_SEC) == (55.0, 5, 120.0)
    put(f, "[1, 2", mtime=12345.0); m.load_tunables()             # corrupt: kept
    ok = ok and m.WAKE_CHARGE_PCT == 55.0 and sum("rejected" in x for x in logs) == 1
    m.load_tunables()                                             # mtime unmoved
    ok = ok and sum("rejected" in x for x in logs) == 1 and len(logs) == 4
    print("  %s  %-58s" % ("PASS" if ok else "FAIL", "P. tunables file -> this module; bad file ignored"))
    return ok
results.append(tunables())

print("\n  %d/%d passed" % (sum(results), len(results)))
sys.exit(0 if all(results) else 1)
