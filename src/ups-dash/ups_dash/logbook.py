"""The power log: everything HISTORY needs to tell the story on its own.

WHY (2026-09-25): a park at 35 % was followed by six hours of a box that was
powered on, stuck before its OS and invisible. Working that out took the
sentinel's journal, the 1 Hz samples (load 0 -> 164 W four seconds after the
output returned), the box's own journal and a LAN sweep. HISTORY showed two
cards and a summary line. Everything below was already flowing through the
collector; it just was not written down.

What is recorded, all as ordinary events (store.event, notify=False kinds in
events.py), each tagged with its source for the HISTORY log:

  ups      its status flags changing (OL CHRG -> OB DISCHRG -> OB OFF ...),
           going unreadable; the draw stepping >= LOAD_STEP_W while the box
           is NOT up (then the draw is the evidence: 0 -> 164 W is a power-
           on); every 10 % of charge on battery
  box      awake / hibernated / unreachable, and hibernate-governor's and
           systemd-sleep's own lines, relayed by box-agent
  sentinel ups-sentinel's journal lines
  nut      nut-driver / upsd / upsmon / the stall watchdog's lines
  dash     the dashboard's own verdict changing (states.classify), and its
           start with the jetson's uptime (a jetson boot vs a restart)
  jetson   the wall clock jumping (no RTC battery: NTP stepped it 3 h)

SD WEAR: nothing here writes per tick. Repeating lines are collapsed: the
same line with its numbers masked is stored at most once per REPEAT_SEC, with
the count it swallowed. The draw is logged only while the box is not up --
a busy workstation would otherwise log every 12 s.

Never raises into the 1 Hz loop.
"""

import re
import time

from . import events

LOAD_STEP_W = 40.0        # a UPS load quantum is ~8.65 W; a boot is 70-160 W
REPEAT_SEC = 600.0
CLOCK_JUMP_SEC = 5.0
_DIGITS = re.compile(r"\d+(?:\.\d+)?")

SOURCE = {
    events.UPS_STATUS: "ups", events.UPS_LOAD_STEP: "ups",
    events.UPS_CHARGE_STEP: "ups", events.BOX_STATE: "box",
    events.BOX_LOG: "box", events.SENTINEL_LOG: "sentinel",
    events.NUT_LOG: "nut", events.DASH_STATE: "dash",
    events.COLLECTOR_STARTED: "dash", events.JETSON_CLOCK_JUMP: "jetson",
}


def source_of(kind):
    """ups | box | sentinel | nut | dash | jetson | park -- for the UI."""
    if kind in SOURCE:
        return SOURCE[kind]
    if "park" in kind or kind == "no_power_on":
        return "park"
    if kind.startswith(("box_", "manual_")) or kind in ("stuck_pre_os",
                                                        "lan_no_agent"):
        return "box"
    if kind.startswith(("ups_", "self_test", "transfer", "mains", "emergency")):
        return "ups"
    return "dash"


def _uptime():
    try:
        with open("/proc/uptime") as fh:
            return float(fh.read().split()[0])
    except Exception:
        return None


class Logbook(object):
    def __init__(self, store, clock=time.time, mono=time.monotonic):
        self.store = store
        self._clock, self._mono = clock, mono
        self._ups = self._box = self._state = self._watts = None
        self._bucket = None
        self._offset = None
        self._seen = {}               # masked line -> [last stored ts, swallowed]
        self._box_after = None        # box lines at or before this: stored already
        try:
            self._box_after = store.last_event_ts(events.BOX_LOG)
        except Exception:
            pass

    # ---- writes ---------------------------------------------------------

    def _add(self, ts, kind, ep, detail):
        try:
            self.store.add_event(ts, kind, ep, detail)
        except Exception:
            pass

    def started(self, now, ep=None):
        up = _uptime()
        self._add(now, events.COLLECTOR_STARTED, ep, {
            "jetson_uptime_s": round(up) if up is not None else None,
            "jetson_booted": up is not None and up < 600})

    # ---- per tick -------------------------------------------------------

    def observe(self, snap, now, ep=None):
        try:
            self._observe(snap or {}, now, ep)
        except Exception as exc:
            print("[ups-dash] logbook: %s: %s" % (type(exc).__name__, exc),
                  flush=True)

    def _observe(self, snap, now, ep):
        self._clock_jump(now, ep)
        ups = snap.get("ups") or {}
        box = (snap.get("box") or {}).get("state")
        ok = bool(ups.get("ok"))
        status = (ups.get("status") or "?") if ok else "unreadable"
        if self._ups is not None and status != self._ups:
            self._add(now, events.UPS_STATUS, ep, {
                "from": self._ups, "to": status, "charge": ups.get("charge"),
                "watts": ups.get("watts")})
        self._ups = status

        w = ups.get("watts") if ok else None
        if isinstance(w, (int, float)):
            quiet = box != "awake" or ups.get("on_battery") is True
            if (quiet and self._watts is not None
                    and abs(w - self._watts) >= LOAD_STEP_W):
                self._add(now, events.UPS_LOAD_STEP, ep, {
                    "from_w": self._watts, "to_w": w,
                    "load_pct": ups.get("load_pct"), "box": box})
            self._watts = w

        charge = ups.get("charge")
        if ok and ups.get("on_battery") is True and isinstance(charge, (int, float)):
            bucket = int(min(charge, 99.9) // 10)     # 100 % is not a band of its own
            if self._bucket is not None and bucket < self._bucket:
                self._add(now, events.UPS_CHARGE_STEP, ep, {
                    "charge": charge, "runtime": ups.get("runtime"),
                    "watts": w})
            self._bucket = bucket if self._bucket is None else min(bucket, self._bucket)
        elif ok and ups.get("on_battery") is False:
            self._bucket = None

        if self._box is not None and box != self._box:
            self._add(now, events.BOX_STATE, ep, {
                "from": self._box, "to": box,
                "why": (snap.get("box") or {}).get("why")})
        self._box = box

        st = snap.get("state") or {}
        if self._state is not None and st.get("state") != self._state:
            self._add(now, events.DASH_STATE, ep, {
                "from": self._state, "to": st.get("state"),
                "short": st.get("short"), "sentence": st.get("sentence")})
        self._state = st.get("state")

    def _clock_jump(self, now, ep):
        offset = now - self._mono()
        if (self._offset is not None
                and abs(offset - self._offset) >= CLOCK_JUMP_SEC):
            self._add(now, events.JETSON_CLOCK_JUMP, ep,
                      {"delta_s": round(offset - self._offset, 1)})
        self._offset = offset

    # ---- journal lines --------------------------------------------------

    def lines(self, entries, ep=None):
        """Journal lines from the jetson units (journal.JournalTail).
        Returns them unchanged, for the learner."""
        for e in entries or []:
            unit = e.get("unit") or ""
            kind = (events.SENTINEL_LOG if unit.startswith("ups-sentinel")
                    else events.NUT_LOG)
            self._line(e, kind, ep, unit)
        return entries

    def box_lines(self, entries, ep=None):
        """The box's lines, relayed by box-agent. Its first answer after a
        restart replays the last 100: skip what is already stored. Returns
        hibernate-governor's lines only: systemd-sleep's are for the log,
        and the learner must never read them."""
        gov = [e for e in entries or []
               if (e.get("unit") or "hibernate-governor").startswith("hibernate-governor")]
        for e in entries or []:
            ts = e.get("ts")
            if (isinstance(ts, (int, float)) and self._box_after is not None
                    and ts <= self._box_after):
                continue
            self._line(e, events.BOX_LOG, ep, e.get("unit") or "hibernate-governor")
            if isinstance(ts, (int, float)):
                self._box_after = max(ts, self._box_after or ts)
        return gov

    def _line(self, e, kind, ep, unit):
        try:
            msg = (e.get("msg") or "").strip()
            if not msg:
                return
            ts = e.get("ts") if isinstance(e.get("ts"), (int, float)) else self._clock()
            key = (kind, _DIGITS.sub("#", msg))
            seen = self._seen.get(key)
            if seen is not None and ts - seen[0] < REPEAT_SEC:
                seen[1] += 1
                return
            detail = {"unit": unit, "msg": msg}
            if seen is not None and seen[1]:
                detail["repeats"] = seen[1]
            self._seen[key] = [ts, 0]
            if len(self._seen) > 500:
                for k in sorted(self._seen, key=lambda k: self._seen[k][0])[:250]:
                    del self._seen[k]
            self._add(ts, kind, ep, detail)
        except Exception:
            pass
