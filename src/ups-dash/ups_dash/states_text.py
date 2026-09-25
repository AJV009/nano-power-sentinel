"""Wording for the states whose text has variants: OUTPUT_OFF, SELF_TEST,
RECOVERING (which quotes the sentinel's live gate) and the battery-floor park
(PARKED, the guard's HIBERNATING, "needs its power button").

Split out of states.py for the 300-line cap only. states.classify() still
decides WHICH state applies; this file only says it, and nothing outside
states.py should import it -- the single-source rule is about who decides,
and that stays in one place. Pure string building, no imports from the rest
of the package, so it can never form an import cycle with states.py.
"""


def _n(v):
    return "?" if v is None else ("%g" % v)


def _fmt_secs(sec):
    if sec is None:
        return "?"
    sec = int(max(0, sec))
    if sec < 90:
        return "%ds" % sec
    m = sec // 60
    return "%dm" % m if m < 90 else "%dh %02dm" % (m // 60, m % 60)


def output_off_text(why, on_battery, charge):
    """(short, sentence, detail, action) for OUTPUT_OFF.

    Bench-tested 2026-09-22 (docs/UPS-TOOLING.md §7, test 2): shutdown.reboot
    sent while the output is latched OFF by load.off/load.off.delay ACKs but
    never arms -- ups.timer.reboot stays 0 and the output stays off. Nothing
    software-side restores it. The front-panel button is the only way back;
    Wake-on-LAN cannot help either, since the box has no standby power.
    """
    mains = "Still on battery" if on_battery else "Mains is present"
    head = "The UPS is de-energised because %s. %s and the battery is at %s%%" % (
        why, mains, charge)
    detail = ("%s, but the output stays off until somebody presses the "
              "front-panel button — Wake-on-LAN cannot help, the box has "
              "no standby power." % head)
    action = "Press the UPS front-panel power button to restore."
    return "UPS output OFF", "UPS output is OFF · box has no power", detail, action


def _num(v):
    # finite too: json.load accepts NaN / Infinity, and round() raises on them
    return (isinstance(v, (int, float)) and not isinstance(v, bool)
            and v == v and abs(v) != float("inf"))


def _q(v):
    """A number from the sentinel's file: anything else reads as "?"."""
    return _n(v) if _num(v) else "?"


def gate_quote(sentinel):
    """The sentinel's own live wake gate, quoted rather than recomputed --
    or None when there is nothing fresh to quote (`sentinel` is ledger
    peer() facts: None unless its heartbeat is fresh)."""
    gate = (sentinel or {}).get("gate")
    if not isinstance(gate, dict):
        return None
    if gate.get("open") is True:
        wake = sentinel.get("wake") if isinstance(sentinel.get("wake"), dict) else {}
        return "gate open — WoL sent %s / %s" % (_q(wake.get("tries")), _q(wake.get("max")))
    secs = [round(v) if _num(v) else None
            for v in (gate.get("stable_sec"), gate.get("need_stable_sec"))]
    return "sentinel holding: %s / %s %% · mains %s / %s s" % (
        _q(gate.get("charge")), _q(gate.get("need_charge")), _q(secs[0]), _q(secs[1]))


def recovering_text(charge, wake_at, stable_sec, sentinel=None):
    """(short, sentence, detail, action) for RECOVERING: mains back, the box
    asleep because of the outage, waiting for the sentinel's gate. With a
    fresh heartbeat the detail leads with the sentinel's own numbers and the
    variant follows its gate; without one, both are computed here."""
    quote = gate_quote(sentinel)
    gate = (sentinel or {}).get("gate") if quote else None
    if gate is not None:
        c, need = gate.get("charge"), gate.get("need_charge")
        recharging = gate.get("open") is not True and _num(c) and _num(need) and c < need
    else:
        recharging = charge is not None and wake_at is not None and charge < wake_at
    if recharging:
        short = "Recharging"
        sentence = "Mains back · box asleep · waiting for the %s%% gate" % _n(wake_at)
        detail = ("The pack is at %s%% and must reach %s%%, and mains must hold "
                  "%s uninterrupted, before the box is woken."
                  % (_n(charge), _n(wake_at), _fmt_secs(stable_sec)))
    else:
        short = "Wake pending"
        sentence = "Mains back · charge is past the %s%% gate" % _n(wake_at)
        detail = ("The pack has recovered. The sentinel wakes the box once "
                  "mains has held %s uninterrupted." % _fmt_secs(stable_sec))
    if quote is None:
        return short, sentence, detail, None
    if gate.get("open") is not True:
        return short, sentence, "%s. The box is woken once both are met." % quote, None
    wake = sentinel.get("wake")
    if isinstance(wake, dict) and wake.get("exhausted") is True:
        return (short, sentence, "%s. The box has not answered and the sentinel "
                "has stopped trying." % quote, "Wake (WoL) from the box controls; "
                "if that fails, check it physically.")
    return short, sentence, "%s. The sentinel is waking the box." % quote, None


def self_test_text(confirmed=True):
    """(short, sentence, detail, action) for SELF_TEST.

    `confirmed` False is the unconfirmed case: the UPS showed a short OL OFF
    with nothing of ours to explain it, which is how an automatic or
    front-panel test starts (NUT #2104), but ups.test.result -- re-read only
    every pollfreq -- has not said "In progress" yet.
    """
    if confirmed:
        sentence = "UPS self-test running · brief switch to battery is expected"
        lead = "The UPS is testing its battery by briefly running the load from it."
    else:
        sentence = "UPS showed OFF briefly · looks like a self-test, confirming"
        lead = ("The UPS reported its output OFF for a moment while mains is "
                "present and nothing here asked for a cut — the signature of "
                "a battery self-test starting. The UPS re-reads its test "
                "result only every ~30 s; if the output is still off after a "
                "short wait this becomes an OUTPUT OFF alert.")
    detail = ("%s During a test the UPS can report OFF or DISCHRG for a few "
              "seconds; that is normal for a self-test, not an outage and not "
              "a dead output, so no outage or output-off alerts fire for it. "
              "The box keeps its power. The result is reported when the test "
              "finishes." % lead)
    return "Self-test", sentence, detail, None


# ---- the battery-floor park (park.py) ------------------------------------
# Bench test 4 (docs/UPS-TOOLING.md §7): ~60 s after shutdown.return the
# output cuts, ~30 s later the UPS switches its own electronics off (USB
# gone, upsd "Data stale") and holds the pack with zero drain; the output is
# back ~3 s after mains returns. "10 minutes" is park_guard.GUARD_SEC.

_PARK_AFTER = ("When mains returns the output comes back within seconds; the "
               "box then powers itself on (BIOS AC BACK = Always On) and is "
               "put straight back to sleep until the pack reaches %s%%, when "
               "the sentinel wakes it as usual.")


def park_text(pk, on_battery, wake_at):
    """(short, sentence, detail, action) for PARKED while armed or parked.
    The UPS going unreadable is the expected middle of a park, not a fault."""
    charge, after = _n(pk.get("charge")), _PARK_AFTER % _n(wake_at)
    if pk.get("phase") == "armed" and on_battery is False:
        return ("Parking the UPS",
                "Mains back before the cut · output drops for a few seconds",
                "The UPS was parked at %s%% and mains returned inside its "
                "~60 s grace. An armed park cannot be cancelled, so the output "
                "still cuts for a few seconds and comes straight back; the box "
                "is already asleep and loses nothing. %s" % (charge, after), None)
    if pk.get("phase") == "armed" and not pk.get("cut_seen"):
        cut_in = pk.get("cut_in")
        when = "in ~%s" % _fmt_secs(cut_in) if cut_in else "any moment"
        return ("Parking the UPS", "Parking the UPS · output off %s" % when,
                "The box is asleep and the pack is down to %s%%, so the UPS "
                "has been told to park: its output cuts ~60 s after the "
                "command, then it switches itself off and holds the pack "
                "instead of feeding its idle inverter (23-55 %%/h). It cannot "
                "be cancelled once armed. %s" % (charge, after), None)
    return ("UPS parked",
            "UPS parked at %s%% · battery held · output returns with mains" % charge,
            "The box is asleep and the UPS has switched its output, and then "
            "itself, off: the pack holds at %s%% instead of draining through "
            "the idle inverter. Going silent over USB is expected while "
            "parked. %s" % (charge, after), None)


def park_return_text(awake, charge, wake_at, held, pk=None):
    """(short, sentence, detail, action) for a park's return: the guard
    putting a box that powered itself on back to sleep (HIBERNATING), or --
    `awake` False -- still waiting for it to power on at all (PARKED), or
    powered on and still booting (park_stuck.py)."""
    pk = pk or {}
    if not awake and pk.get("on_lan"):
        return ("Box on LAN, agent silent",
                "Box is on the network · box-agent not answering",
                "The box powered on with the mains and answers on the LAN, "
                "but box-agent does not: it may have booted another OS, or "
                "the agent is down. It is never power-cycled in this state.",
                None)
    if not awake and pk.get("powered_at"):
        return ("Box booting",
                "Box powered on with the mains · waiting for it to boot",
                "The UPS load rose right after the output returned, so AC BACK "
                "powered the box on. A normal resume reaches the network in "
                "about a minute. If it is still silent and off the LAN after 7 "
                "minutes it is stuck before its OS (firmware or boot menu), and "
                "the UPS power-cycles it -- at most %d times." % pk_max(pk),
                None)
    if not awake:
        return ("Mains back", "Mains back after a park · waiting for the box to power on",
                "The UPS output is back. The park took the box's standby "
                "power, so Wake-on-LAN cannot reach it: it returns only if its "
                "BIOS powers it on with the mains (AC BACK = Always On), and is "
                "then put straight back to sleep until the pack reaches %s%%. "
                "If it has not appeared within 10 minutes it needs its power "
                "button." % _n(wake_at), None)
    if held:
        return ("Back to sleep",
                "Box powered on with the mains · back to sleep (off by you)",
                "The park cut the box's power and its BIOS powered it on when "
                "mains returned. You had switched it off, so it is being put "
                "back to sleep and stays off. Use Wake when you want it.", None)
    return ("Back to sleep",
            "Box powered on with the mains · back to sleep until %s%%" % _n(wake_at),
            "The park cut the box's power and its BIOS powered it on when mains "
            "returned. The pack is at %s%% and the wake gate (%s%%, mains "
            "stable) is not open yet, so it is hibernated again -- this time "
            "with standby power and an armed NIC, so the sentinel's normal "
            "Wake-on-LAN brings it back once the pack has recovered."
            % (_n(charge), _n(wake_at)), None)


def other_os_text(on_battery, charge, runtime):
    """(short, sentence, detail, action) for a box up on another OS --
    Windows, as far as the LAN can tell (lanprobe.py)."""
    if on_battery:
        return ("ON BATTERY · box on Windows",
                "ON BATTERY · box on Windows · nothing will hibernate it",
                "The box is running another OS, where neither the governor nor "
                "box-agent runs. Nothing will put it to sleep: it keeps drawing "
                "from the pack (%s%%, ~%s min at this load) until the UPS is "
                "empty and cuts it hard." % (_n(charge), _n(runtime // 60)
                                              if isinstance(runtime, int) else "?"),
                "Shut Windows down now, or reboot into Linux so the governor "
                "protects it.")
    return ("Box on Windows",
            "Box is up on Windows · not monitored",
            "The box answers on the LAN but not like Linux: it is running "
            "another OS -- Windows, most likely. The governor and box-agent "
            "only run in Linux, so while it is here an outage will NOT "
            "hibernate it, and the UPS will not park.",
            None)


def pk_max(pk):
    return pk.get("max_cycles") or 2      # park_stuck.MAX_CYCLES, via the snapshot


def park_cycle_text(pk):
    """(short, sentence, detail, action) while a stuck box is power-cycled."""
    return ("Power-cycling the box",
            "Box stuck before its OS · power-cycling it (%s of %d)"
            % (_n(pk.get("cycles")), pk_max(pk)),
            "The box has drawn power since the output returned but never "
            "reached its OS or the network. The UPS cuts its output for a few "
            "seconds (shutdown.reboot, ~60 s after the command) and the BIOS "
            "powers the box on again. Nothing is lost: it never got as far as "
            "reading its hibernate image.", None)


def park_no_power_text(pk):
    """(short, sentence, detail, action) once the guard window closed with the
    box never seen -- shown until it is next seen awake. Which of the three
    outcomes (park_stuck.py) decides what to tell the human."""
    parked_charge, outcome = pk.get("outcome_charge"), pk.get("outcome")
    if outcome == "stuck_pre_os":
        return ("Stuck before its OS",
                "Box is ON but never booted · hold its power button",
                "After the park the box powered on with the mains (the UPS "
                "shows %s W) but never reached its OS or the network%s. That "
                "is a hang in the firmware or the boot menu -- DDR5 memory "
                "training after a full power loss is the usual one."
                % (_n(pk.get("outcome_watts")),
                   ", even after %s power-cycle(s)" % _n(pk.get("outcome_cycles"))
                   if pk.get("outcome_cycles") else ""),
                "Hold the power button ~10 s, then press it once. Before that, "
                "note which status LED is lit on the board (CPU / DRAM / VGA / "
                "BOOT). Its hibernate image is untouched, so it resumes.")
    if outcome == "lan_no_agent":
        return ("On LAN, agent silent", "Box is up on the network · box-agent silent",
                "After the park the box powered on and answers on the LAN, but "
                "box-agent never did: it may have booted another OS, or the "
                "agent failed to start.",
                "Check the box's screen; restart box-agent if it is Linux.")
    return ("Needs power button", "Box is off · press its power button",
            "The UPS parked at %s%% and its output came back with the mains, "
            "but the box did not power itself on within 10 minutes. Its BIOS "
            "AC BACK is probably not set to Always On, and Wake-on-LAN cannot "
            "reach a box that lost its standby power in the cut."
            % _n(parked_charge),
            "Press the box's power button; set AC BACK = Always On in its "
            "BIOS so the next park recovers by itself.")
