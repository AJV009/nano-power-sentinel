"""Drive the REAL ParkTracker (park.py + park_guard.py + park_stuck.py) through
whole park stories on a virtual 1 Hz clock, then classify() each tick the way
the collector does. Only the I/O edges are fakes: the ledger, the UPS
command, the hibernate call, the ARP check. Every scenario starts on battery
with the box already asleep, parks, and returns at RETURN.

S1 is 2026-09-25 exactly: the box powers on with the output (load 0 -> 13 %),
never reaches the agent and never answers ARP. The old guard waited 10
minutes and said "press its power button: AC BACK is probably off"."""
import os, sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "src", "ups-dash"))

from ups_dash import park, states          # noqa: E402

RETURN = 300          # mains back (the park itself arms at ~60, cuts at ~120)
CUT_AFTER = 60        # shutdown.reboot 1: ACK -> output off
OFF_FOR = 4           # on mains the output is back ~4 s later (bench test 1)


class Ledger(object):
    def __init__(self):
        self.d = {}

    def get(self, k):
        return self.d.get(k)

    def set(self, k, v):
        self.d[k] = v
        return True

    def persisted(self, k):
        return self.d.get(k)


class Store(object):
    def __init__(self):
        self.events = []

    def add_event(self, ts, kind, ep, detail):
        self.events.append((ts, kind, detail))


def story(boots_at=None, lan=False, powers_on=True, refuse_cycle=False,
          ignore_cycle=False, until=3600, hold=True, boots_from=1):
    """boots_at: seconds after a power-on that the agent answers (None =
    never), from the `boots_from`-th power-on on. Each power-cycle is a
    fresh power-on."""
    t = {"now": 0.0}
    cmds, st = [], Store()
    cycle = {"acked_at": None}
    power = {"on_at": None, "n": 0}  # when the box last powered on, how often

    def instcmd(cmd, val):
        cmds.append((t["now"], cmd, val))
        if len(cmds) == 1:
            return True, "OK"            # the park itself
        if refuse_cycle:
            return False, "ACCESS-DENIED"
        if not ignore_cycle:
            cycle["acked_at"] = t["now"]
        return True, "OK"

    tr = park.ParkTracker(store=st, instcmd=instcmd, ledger=Ledger(),
                          hibernate=lambda: (True, "exit 0"),
                          spawn=lambda fn: fn(), file_tunables=lambda: {},
                          lan_present=lambda: lan and power["on_at"] is not None)
    log = []
    for sec in range(until):
        now = float(sec)
        t["now"] = now
        out_off = False
        if cycle["acked_at"] is not None:
            cut = cycle["acked_at"] + CUT_AFTER
            if cut <= now < cut + OFF_FOR:
                out_off, power["on_at"] = True, None
            elif now >= cut + OFF_FOR:
                cycle["acked_at"] = None
        if sec < RETURN:
            ob = True
            parked = tr.phase in ("armed", "parked") and sec >= 125
            ups = ({"ok": False} if parked else
                   {"ok": True, "on_battery": True, "flags": ["OB", "DISCHRG"],
                    "charge": 35.0, "load_pct": 0, "watts": 0.0,
                    "timer_shutdown": -1, "timer_reboot": 0})
        else:
            ob = False
            if (powers_on and not out_off and power["on_at"] is None
                    and sec >= RETURN + 4):
                power["on_at"] = now
                power["n"] += 1
            on = power["on_at"] is not None and not out_off
            ups = {"ok": True, "on_battery": False,
                   "flags": ["OL", "CHRG"] + (["OFF"] if out_off else []),
                   "charge": 40.0, "load_pct": 13 if on else 0,
                   "watts": 112.5 if on else 0.0,
                   "timer_shutdown": -1, "timer_reboot": 0}
        awake = (boots_at is not None and power["on_at"] is not None
                 and power["n"] >= boots_from
                 and now - power["on_at"] >= boots_at)
        box = "awake" if awake else "hibernated"
        snap = tr.tick(now, ups, box, states.CAUSE_OUTAGE, {}, False,
                       {"ts": 0} if hold else None, {"wake_charge_pct": 70.0,
                                                     "mains_stable_sec": 120.0})
        if awake and snap.get("rehibernate_sent"):
            power["on_at"] = None            # it went back to sleep
        state = states.classify(ups, box, states.CAUSE_OUTAGE, {}, None, {},
                                None, None, None, snap, None)
        log.append((now, snap, state))
    return tr, st, cmds, log


def kinds(st):
    return [k for _, k, _ in st.events]


def first(st, kind):
    return next((ts for ts, k, _ in st.events if k == kind), None)


def check(name, cond, why=""):
    print("  %s  %s%s" % ("PASS" if cond else "FAIL", name,
                          "" if cond else "\n        -> " + why))
    return cond


results = []

# S1. 2026-09-25: powered on, stuck, never on the LAN.
tr, st, cmds, log = story()
cycles = [c for c in cmds[1:] if c[1] == "shutdown.reboot"]
k = kinds(st)
outcome_at = first(st, "stuck_pre_os")
states_seen = [s["state"] for _, _, s in log]
results.append(check(
    "S1. stuck before the OS: 2 power-cycles, then 'stuck', never 'press power'",
    len(cycles) == 2 and k.count(states.BOX_POWERED_ON) == 3   # one per power-on
    and k.count(states.BOX_STUCK_PRE_OS) == 2
    and k.count(states.BOX_POWER_CYCLED) == 2
    and "no_power_on" not in k and outcome_at is not None
    and log[-1][2]["short"] == "Stuck before its OS",
    "cmds=%s kinds=%s last=%s" % (cmds, k, log[-1][2]["short"])))
first_cycle = cycles[0][0] if cycles else None
results.append(check(
    "S1. first cycle only after 7 min powered (power-on at %s, cycle at %s)"
    % (RETURN + 4, first_cycle),
    first_cycle is not None and first_cycle - (RETURN + 4) >= 420))
cyc = [(now, s) for now, snap, s in log if snap.get("cycling")]
results.append(check(
    "S1. while cycling the state is PARKED, never OUTPUT_OFF",
    cyc and all(s["state"] == states.PARKED for _, s in cyc)
    and states.OUTPUT_OFF not in states_seen,
    "states while cycling: %s" % sorted({s["state"] for _, s in cyc})))
results.append(check(
    "S1. outcome only after the LAST cycle's fresh 10-min window",
    outcome_at is not None and cycles
    and outcome_at - (cycles[-1][0] + CUT_AFTER + OFF_FOR) >= 600))

# S2. A normal return: boots in 55 s, put back to sleep, never cycled.
tr, st, cmds, log = story(boots_at=55)
results.append(check(
    "S2. normal boot (55 s): rehibernated, no power-cycle, no outcome",
    len(cmds) == 1 and states.BOX_REHIBERNATED in kinds(st)
    and not any(k in kinds(st) for k in ("stuck_pre_os", "no_power_on")),
    "cmds=%s kinds=%s" % (cmds, kinds(st))))

# S3. On the LAN but the agent never answers: another OS, never cycled.
tr, st, cmds, log = story(lan=True)
results.append(check(
    "S3. on the LAN, agent silent: never power-cycled, outcome lan_no_agent",
    len(cmds) == 1 and "lan_no_agent" in kinds(st)
    and states.BOX_LAN_NO_AGENT in kinds(st),
    "cmds=%s kinds=%s" % (cmds, kinds(st))))

# S4. Never draws power: the original outcome and text, unchanged.
tr, st, cmds, log = story(powers_on=False)
results.append(check(
    "S4. no power drawn at all: no_power_on, 'Needs power button'",
    len(cmds) == 1 and "no_power_on" in kinds(st)
    and log[-1][2]["short"] == "Needs power button",
    "cmds=%s kinds=%s" % (cmds, kinds(st))))

# S5. The UPS refuses the power-cycle: one failure, no retry, stuck outcome.
tr, st, cmds, log = story(refuse_cycle=True)
fails = [d for _, k, d in st.events if k == states.PARK_FAILED]
results.append(check(
    "S5. UPS refuses the cycle: one 'cycle' failure, no retry, stuck outcome",
    len(cmds) == 2 and len(fails) == 1 and fails[0]["stage"] == "cycle"
    and "stuck_pre_os" in kinds(st),
    "cmds=%s kinds=%s" % (cmds, kinds(st))))

# S6. ACKed but the output never cuts: times out, no more tries.
tr, st, cmds, log = story(ignore_cycle=True)
fails = [d for _, k, d in st.events if k == states.PARK_FAILED]
results.append(check(
    "S6. ACK but no cut: times out once, no more tries, stuck outcome",
    len(cmds) == 2 and len(fails) == 1 and "stuck_pre_os" in kinds(st),
    "cmds=%s kinds=%s" % (cmds, kinds(st))))

# S7. Stuck on the first power-on, boots after ONE cycle: rehibernated as a
# normal return, no second cycle, no outcome.
tr, st, cmds, log = story(boots_at=55, boots_from=2)
results.append(check(
    "S7. boots after one cycle: rehibernated, exactly one cycle, no outcome",
    len(cmds) == 2 and states.BOX_REHIBERNATED in kinds(st)
    and not any(k in kinds(st) for k in ("stuck_pre_os", "no_power_on")),
    "cmds=%s kinds=%s" % (cmds, kinds(st))))

print("\n  %d/%d passed" % (sum(results), len(results)))
sys.exit(0 if all(results) else 1)
