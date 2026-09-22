"""POST /api/control/<action> dispatch.

Split out of server.py once server.py hit the 300-line file cap. Behaviour
for the original five actions (wake, hibernate, reset-config, ups-off,
ups-abort) is unchanged -- this is a straight move, not a rewrite.

THE INTERLOCK: control.interlock() refuses while an episode is open, unless
the caller overrides (control.py has the full reasoning). Every action goes
through it EXCEPT beeper: muting the alarm is exactly what you need to do
mid-outage.

events.py is being extended in parallel by another agent with the kind
strings this module needs (ups_setting_changed, beeper_changed,
self_test_started). Resolved via getattr() with the plain string as
fallback, so this module works whether or not that landed first -- see each
EV_* constant below.
"""

import time

from . import config as cfgmod
from . import control, hold, states, upsoff, upsops
from . import nut as nutmod
from . import settings

NO_INTERLOCK = ("beeper",)

EV_UPS_SETTING_CHANGED = getattr(states, "UPS_SETTING_CHANGED", "ups_setting_changed")
EV_BEEPER_CHANGED = getattr(states, "BEEPER_CHANGED", "beeper_changed")
EV_SELF_TEST_STARTED = getattr(states, "SELF_TEST_STARTED", "self_test_started")


def _reread(name):
    """Build a reread() callable for upsops.apply_setting(): a fresh,
    short-lived NUT client, LIST VAR, pick out the one variable. A dedicated
    GET VAR round trip would be marginally cheaper, but this reuses the
    already-proven client rather than adding a second wire parser."""
    def _fn():
        client = nutmod.NutClient(host=settings.NUT_HOST, port=settings.NUT_PORT,
                                  ups=settings.UPS_NAME)
        try:
            vars_ = client.read()
        finally:
            client.close()
        return (vars_ or {}).get(name)
    return _fn


def handle(action, body, collector, store):
    """Run one control action. Returns (payload, http_status)."""
    body = body or {}
    override = bool(body.get("override"))

    if action not in NO_INTERLOCK:
        refusal = control.interlock(collector, override)
        if refusal:
            return {"error": refusal, "interlocked": True}, 409

    if action == "wake":
        # Waking it by hand ends the manual shutdown.
        hold.clear_hold()
        ok, detail = control.magic_packet()
        store.add_event(time.time(), states.MANUAL_WAKE, collector.episodes.id,
                        {"ok": ok, "detail": detail, "override": override})
        return {"ok": ok, "detail": detail}, (200 if ok else 500)

    if action == "hibernate":
        # Declare intent BEFORE acting: the box may vanish within a second,
        # and an unattributed disappearance gets reported as an unexplained
        # failure rather than something you did.
        collector.cause.declare_intent(states.CAUSE_MANUAL_HIBERNATE, time.time())
        # And tell the sentinel: you switched it off on purpose, so the next
        # outage recovery must not switch it back on.
        hold.set_hold("hibernated from the dashboard")
        store.add_event(time.time(), states.MANUAL_HIBERNATE,
                        collector.episodes.id, {"override": override})
        ok, detail = control.box_hibernate(collector.box.base)
        return {"ok": ok, "detail": detail}, (200 if ok else 502)

    if action == "reset-config":
        result, code = cfgmod.reset(collector.box.base, collector.tunables)
        if code == 200 and result.get("applied"):
            store.add_event(time.time(), states.CONFIG_CHANGE,
                            collector.episodes.id,
                            dict(result["applied"], reset=True))
        return result, code

    # Switching the UPS output off is the one action here with no software
    # undo, so it lives behind a typed confirmation phrase and a cancellable
    # delay. See upsoff.py for the full reasoning.
    if action == "ups-off":
        return upsoff.request(collector, store, body)

    if action == "ups-abort":
        return upsoff.abort(collector, store)

    if action == "ups-set":
        name = body.get("name")
        value = body.get("value")
        ok, detail = upsops.validate(name, value)
        if not ok:
            return ({"ok": False, "detail": detail, "name": name,
                     "value": value, "verified": None, "now": None}, 400)
        result = upsops.apply_setting(name, value, _reread(name))
        code = 200 if result.get("ok") else 502
        if result.get("ok"):
            store.add_event(time.time(), EV_UPS_SETTING_CHANGED,
                            collector.episodes.id,
                            {"name": name, "value": result.get("value"),
                             "verified": result.get("verified")})
        return result, code

    if action == "beeper":
        mode = body.get("mode")
        ok, detail, code = upsops.beeper(mode)
        if ok:
            store.add_event(time.time(), EV_BEEPER_CHANGED,
                            collector.episodes.id, {"mode": mode})
        return {"ok": ok, "detail": detail, "mode": mode}, code

    if action == "self-test":
        sub = body.get("action")
        if sub == "start":
            ups_block = (collector.snapshot() or {}).get("ups")
            ok, detail, code = upsops.selftest_start(ups_block, upsoff.state())
            if ok:
                store.add_event(time.time(), EV_SELF_TEST_STARTED,
                                collector.episodes.id, {"detail": detail})
            return {"ok": ok, "detail": detail}, code
        if sub == "stop":
            ok, detail, code = upsops.selftest_stop()
            return {"ok": ok, "detail": detail}, code
        return {"error": "action must be 'start' or 'stop'"}, 400

    return {"error": "unknown action", "action": action}, 404
