"""Emergency shutdown: two modes, both ending with the UPS output off.

INTENDED USE: something is happening in the room and everything needs to stop
NOW. That framing drives every decision here -- speed and few taps beat
ceremony, because a confirmation nobody can complete under stress is worse
than no button at all.

  safe     hibernate the box, wait for its draw to actually fall, then cut.
           The box comes back resumable instead of hard-killed.
  instant  cut immediately, whatever is running. No waiting, no conditions.

WHY WATCHING THE LOAD IS THE RIGHT SIGNAL: the agent going silent only means
hibernation STARTED -- the machine is still writing its image and still
drawing power. `ups.load` falling is physical evidence it actually stopped.
Cutting on "agent unreachable" would cut mid-write, which is the exact data
loss the safe mode exists to prevent.

⚠ NEITHER MODE IS REVERSIBLE ONCE IT FIRES. This UPS has no `load.on` and no
`shutdown.return`; it latches at `OL OFF` even after mains returns and waits
for a human to press its front-panel button. With the output dead the box has
no +5VSB, so Wake-on-LAN cannot rescue it either.

The safe mode runs asynchronously and publishes its phase, so the UI can show
progress and offer an abort while the sequence is still in its early stages.
The instant mode is synchronous and has no abort -- that is the point of it.
"""

import threading
import time

from . import states, upscmd

SAFE_DELAY = 15          # brief window so the safe path stays abortable
LOAD_WAIT = 180.0        # how long to wait for the draw to fall
LOAD_POLL = 2.0
LOAD_FLOOR = 3           # percent; at or below this the box has clearly stopped

PENDING = {"active": False, "phase": "idle", "mode": None, "started": None,
           "deadline": None, "detail": None, "baseline_load": None,
           "current_load": None}
_lock = threading.RLock()


def state():
    with _lock:
        out = dict(PENDING)
    if out.get("deadline"):
        remaining = out["deadline"] - time.time()
        out["remaining"] = max(0.0, round(remaining, 1))
        if remaining <= 0 and out.get("phase") == "cutting":
            # The cut has fired. Leaving this "active" forever made the UI
            # offer an Abort button for something that already happened.
            out["active"] = False
            out["phase"] = "done"
            with _lock:
                PENDING.update({"active": False, "phase": "done"})
    return out


def _set(**kw):
    with _lock:
        PENDING.update(kw)


def _load_now(collector):
    snap = collector.snapshot() or {}
    return (snap.get("ups") or {}).get("load_pct")


def _wait_for_draw_to_fall(collector, baseline):
    """Wait until the box's draw actually drops. Returns (dropped, detail).

    Paced on wall-clock polling rather than a tight loop: a check that returns
    in a millisecond proves nothing about a machine that takes 20 seconds to
    write its hibernate image.
    """
    target = None
    if isinstance(baseline, int) and baseline > LOAD_FLOOR:
        # Half the starting draw, or the floor, whichever is higher.
        target = max(LOAD_FLOOR, int(baseline * 0.5))
    deadline = time.time() + LOAD_WAIT
    while time.time() < deadline:
        time.sleep(LOAD_POLL)
        load = _load_now(collector)
        _set(current_load=load)
        if load is None:
            continue
        if target is None:
            if load <= LOAD_FLOOR:
                return True, "draw fell to %s%%" % load
        elif load <= target:
            return True, "draw fell from %s%% to %s%%" % (baseline, load)
    return False, "draw did not fall within %ds" % int(LOAD_WAIT)


def _safe_sequence(collector, store, box_url):
    from . import control
    episode = collector.episodes.id
    baseline = _load_now(collector)
    _set(active=True, phase="hibernating", mode="safe", started=time.time(),
         baseline_load=baseline, current_load=baseline, deadline=None,
         detail="asking the box to hibernate")
    store.add_event(time.time(), states.EMERGENCY_SAFE_STARTED, episode,
                    {"baseline_load": baseline})

    ok, detail = control.box_hibernate(box_url)
    if not _still_running():
        return
    _set(phase="waiting_for_draw", detail="hibernate requested: %s" % (detail,))

    dropped, why = _wait_for_draw_to_fall(collector, baseline)
    if not _still_running():
        return
    if not dropped:
        _set(active=False, phase="aborted",
             detail="NOT cutting: %s. Cutting power to a machine still "
                    "drawing is what this mode exists to avoid." % why)
        store.add_event(time.time(), states.EMERGENCY_SAFE_ABORTED, episode,
                        {"reason": why})
        return

    _set(phase="cutting", detail=why, deadline=time.time() + SAFE_DELAY)
    ok, detail = upscmd.cut_output(SAFE_DELAY)
    store.add_event(time.time(), states.EMERGENCY_SAFE_CUT, episode,
                    {"ok": ok, "detail": detail, "why": why})
    if ok:
        _set(detail="output cut scheduled in %ds (%s)" % (SAFE_DELAY, why))
    else:
        _set(active=False, phase="failed", deadline=None,
             detail="UPS refused: %s" % detail)


def _still_running():
    with _lock:
        return PENDING.get("active") and PENDING.get("phase") != "aborted"


def request(collector, store, body):
    """Start an emergency shutdown. Returns (payload, http_status)."""
    body = body or {}
    mode = body.get("mode")
    if mode not in ("safe", "instant"):
        return {"error": "mode must be 'safe' or 'instant'"}, 400
    if not body.get("confirmed"):
        # The UI collects three taps; the API still demands explicit intent so
        # a stray request can never cut power to the workstation.
        return {"error": "refused: confirmed flag not set"}, 400

    user, _pw = upscmd.load_credentials()
    if not user:
        return {"error": "No UPS command credentials on the jetson "
                         "(/etc/ups-dash/upscmd.json) -- cannot cut the output."}, 503

    with _lock:
        if PENDING.get("active"):
            return {"error": "a shutdown sequence is already running",
                    "state": dict(PENDING)}, 409

    if mode == "instant":
        collector.cause.declare_intent(states.CAUSE_EMERGENCY_INSTANT, time.time())
        _set(active=True, phase="cutting", mode="instant", started=time.time(),
             deadline=None, detail="immediate cut requested")
        ok, detail = upscmd.instcmd("load.off")
        store.add_event(time.time(), states.EMERGENCY_INSTANT_CUT,
                        collector.episodes.id, {"ok": ok, "detail": detail})
        _set(active=False, phase="done" if ok else "failed", detail=detail)
        return ({"ok": ok, "mode": "instant", "detail": detail,
                 "warning": "Output cut. The UPS stays OFF until its "
                            "front-panel button is pressed."},
                200 if ok else 502)

    collector.cause.declare_intent(states.CAUSE_EMERGENCY_SAFE, time.time())
    threading.Thread(target=_safe_sequence, daemon=True,
                     args=(collector, store, collector.box.base)).start()
    return {"ok": True, "mode": "safe", "async": True,
            "detail": "Hibernating the box, then cutting once its draw falls."}, 202


def abort(collector, store):
    """Cancel whatever is in flight. Works on the safe path; an instant cut
    has already happened by the time anyone could press this."""
    ok, detail = upscmd.abort_cut()
    _set(active=False, phase="aborted", deadline=None,
         detail="aborted: %s" % detail)
    store.add_event(time.time(), states.EMERGENCY_ABORT, collector.episodes.id,
                    {"ok": ok, "detail": detail})
    return {"ok": ok, "detail": detail}, (200 if ok else 502)
