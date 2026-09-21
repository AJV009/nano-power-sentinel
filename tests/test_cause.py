"""Drive CauseTracker through the REAL sequences, not a pre-supplied cause."""
import os, sys, tempfile
ROOT = sys.argv[1] if len(sys.argv) > 1 else os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "src", "ups-dash")
sys.path.insert(0, ROOT)
os.environ["UPS_DASH_WAKE_HOLD"] = os.path.join(tempfile.mkdtemp(), "wake-hold")
from ups_dash import cause as C, states as S
try:
    from ups_dash import hold
except ImportError:
    hold = None

def run(name, steps, want):
    t = C.CauseTracker(); now = 1000.0; got = None
    for kind, arg in steps:
        if kind == "intent": t.declare_intent(arg, now)
        else:
            got = t.update(now, kind, arg)
        now += 1.0
    ok = got == want
    print("  %s  %-54s -> %-18s (want %s)" % ("PASS" if ok else "FAIL", name, got, want))
    return ok

AWAKE_GRACE = [("awake", False)] * 12      # box still answering ~12 s after the request
r = []
# The 19:05 incident, exactly: intent declared, agent keeps answering for the
# poll-grace window, THEN goes silent.
r.append(run("dashboard Hibernate, box looks awake 12s, then gone",
    [("intent", S.CAUSE_MANUAL_HIBERNATE)] + AWAKE_GRACE + [("unreachable", False)],
    S.CAUSE_MANUAL_HIBERNATE))
# 19:05 -> 20:30: manual down, THEN the outage starts. Must not become "outage".
r.append(run("manual hibernate, outage starts later",
    [("intent", S.CAUSE_MANUAL_HIBERNATE)] + AWAKE_GRACE +
    [("unreachable", False)] * 5 + [("unreachable", True)] * 5,
    S.CAUSE_MANUAL_HIBERNATE))
# Regression: the governor hibernating during an outage is still an outage.
r.append(run("box goes down while on battery, no intent",
    [("awake", True)] * 3 + [("unreachable", True)], S.CAUSE_OUTAGE))
# Unexplained disappearance on mains stays honest.
r.append(run("box vanishes on mains, nobody asked",
    [("awake", False)] * 3 + [("unreachable", False)], S.CAUSE_UNKNOWN))
print("\n  %d/%d passed" % (sum(r), len(r)))

if hold:
    hold.set_hold("test")
    t = C.CauseTracker()
    for k in ["awake", "unreachable", "unreachable", "awake"]:
        t.update(2000.0, k, False)
    print("  %s  hold lifted when the box comes back from being down" %
          ("PASS" if hold.get_hold() is None else "FAIL"))
    hold.set_hold("test"); t = C.CauseTracker()
    for k in ["awake"] * 6:
        t.update(2000.0, k, False)
    print("  %s  hold KEPT while the box merely still looks awake after the request" %
          ("PASS" if hold.get_hold() is not None else "FAIL"))
