"""One readable line per stored event, for HISTORY's power log and episode
cards. Server-side on purpose: the event vocabulary (events.py) and the
wording live in Python, and the browser only renders (the ledger rule)."""

import json

from . import events
from .logbook import source_of

_PREFIX = ("[ups-sentinel] ", "[hibernate-governor] ", "[nut-stall-watchdog] ")


def _n(v, unit=""):
    if isinstance(v, bool):
        return "yes" if v else "no"
    if not isinstance(v, (int, float)):
        return "?"
    return ("%d" % v if float(v).is_integer() else "%.1f" % v) + unit


def _dur(sec):
    if not isinstance(sec, (int, float)):
        return "?"
    sec = abs(int(sec))
    if sec < 90:
        return "%d s" % sec
    if sec < 5400:
        return "%d min" % round(sec / 60.0)
    if sec < 172800:
        return "%d h %02d min" % (sec // 3600, (sec % 3600) // 60)
    return "%.1f days" % (sec / 86400.0)


def _msg(d):
    msg = d.get("msg") or ""
    for p in _PREFIX:
        if msg.startswith(p):
            msg = msg[len(p):]
    if d.get("repeats"):
        msg += "  (+%d similar lines before this)" % d["repeats"]
    return msg


def _text(kind, d):
    if kind == events.UPS_STATUS:
        return "%s → %s · %s%% · %s" % (d.get("from"), d.get("to"),
                                        _n(d.get("charge")), _n(d.get("watts"), " W"))
    if kind == events.UPS_LOAD_STEP:
        return "draw %s → %s (box %s)" % (_n(d.get("from_w"), " W"),
                                          _n(d.get("to_w"), " W"), d.get("box"))
    if kind == events.UPS_CHARGE_STEP:
        return "battery %s%% · runtime %s · draw %s" % (
            _n(d.get("charge")), _dur(d.get("runtime")), _n(d.get("watts"), " W"))
    if kind == events.BOX_STATE:
        return "box %s → %s%s" % (d.get("from"), d.get("to"),
                                  " (%s)" % d["why"] if d.get("why") else "")
    if kind == events.DASH_STATE:
        return d.get("sentence") or d.get("short") or d.get("to") or "?"
    if kind in (events.SENTINEL_LOG, events.NUT_LOG, events.BOX_LOG):
        return _msg(d)
    if kind == events.COLLECTOR_STARTED:
        up = d.get("jetson_uptime_s")
        if d.get("jetson_booted"):
            return "the jetson booted %s ago" % _dur(up)
        return "ups-dash restarted (jetson up %s)" % _dur(up)
    if kind == events.JETSON_CLOCK_JUMP:
        delta = d.get("delta_s")
        return "jetson wall clock jumped %s%s" % (
            "+" if isinstance(delta, (int, float)) and delta > 0 else "-", _dur(delta))
    if kind == events.BOX_POWERED_ON:
        return "load %s%% (%s) %s after the output returned" % (
            _n(d.get("load_pct")), _n(d.get("watts"), " W"), _dur(d.get("after_return_s")))
    if kind in (events.BOX_STUCK_PRE_OS, events.BOX_POWER_CYCLED):
        return "powered %s at %s, not on the LAN · power-cycle %s of %s" % (
            _dur(d.get("powered_s")), _n(d.get("watts"), " W"),
            _n(d.get("cycle")), _n(d.get("max")))
    if kind in ("no_power_on", "stuck_pre_os", "lan_no_agent",
                events.PARK_NO_POWER_ON):
        return "guard window over · pack %s%%%s%s" % (
            _n(d.get("charge")),
            " · %s power-cycle(s)" % _n(d["cycles"]) if d.get("cycles") else "",
            " · box drawing %s" % _n(d["watts"], " W") if d.get("watts") else "")
    if kind == events.UPS_PARK_ARMED:
        r = d.get("reply") if isinstance(d.get("reply"), dict) else {}
        return "%s → %s at %s%% (floor %s%%)" % (
            r.get("cmd") or "park", r.get("reply") or ("OK" if d.get("ok") else "?"),
            _n(d.get("charge")), _n(d.get("floor")))
    if kind == events.UPS_PARKED:
        return "%s at %s%%" % (d.get("why") or "parked", _n(d.get("charge")))
    if kind == events.BOX_REHIBERNATED:
        return "hibernate request %s → %s" % (_n(d.get("try")),
                                             "ok" if d.get("ok") else "failed")
    if kind == events.EPISODE_START:
        return "%s · pack %s%% · box %s" % (
            (d.get("kind") or "?").replace("_", " "), _n(d.get("charge")),
            d.get("box_state") or "?")
    if kind == events.EPISODE_END:
        return "pack %s%% · lasted %s" % (_n(d.get("charge")), _dur(d.get("duration")))
    if kind == events.TRANSFER_CAUSE:
        return d.get("reason") or "?"
    if kind == events.CONFIG_CHANGE:
        return ", ".join("%s → %s" % (k.replace("_", " "), _n(v) if isinstance(v, (int, float)) else v)
                         for k, v in d.items())
    return None


def _generic(d):
    """Scalars only, and no epoch stamps: the row already has its time."""
    keep = [(k, v) for k, v in (d or {}).items()
            if k not in ("reply", "at", "ok") and not isinstance(v, (dict, list))
            and not (isinstance(v, (int, float)) and v > 1e9)]
    return " · ".join("%s %s" % (k.replace("_", " "),
                                 _n(v) if isinstance(v, (int, float)) else v)
                      for k, v in keep[:5] if v is not None)


def describe(row):
    """A stored event row -> {"id", "ts", "kind", "source", "label",
    "severity", "text", "detail"}. Never raises."""
    kind = row.get("kind") or "?"
    try:
        d = json.loads(row["detail"]) if row.get("detail") else {}
    except Exception:
        d = {"raw": row.get("detail")}
    if not isinstance(d, dict):
        d = {"value": d}
    meta = events.meta(kind)
    try:
        text = _text(kind, d)
    except Exception:
        text = None
    return {"id": row.get("id"), "ts": row.get("ts"), "kind": kind,
            "episode_id": row.get("episode_id"), "source": source_of(kind),
            "label": meta["label"], "severity": meta["severity"],
            "text": text if text is not None else _generic(d), "detail": d}
