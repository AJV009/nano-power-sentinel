"""Collector-side memory for two UPS facts no single read can give: is a
self-test running, and why did the UPS go to battery.

Split out of collector.py for the 300-line cap. The collector owns one
UpsExtras and calls it once per tick; everything here is tick-to-tick
bookkeeping plus two rare store writes. No network, never raises into the
1 Hz loop.

SELF-TEST -> snap["self_test"] = {"active", "started", "by_dashboard",
"result", "suspected"}:
  active     True if the UPS says "In progress", OR the dashboard started a
             test under SELFTEST_WINDOW s ago (upsops.selftest_started()).
             False only when the UPS confirms no test and there is no
             window; None when unreadable -- never folded into False. The
             window exists only to cover the lag before "In progress" is
             read, so a verdict read after the start closes it early --
             otherwise "Self-test running" outlives the "passed" push by a
             minute.
  suspected  (beyond the contract) a young OL OFF with nothing of ours to
             explain it. OFF is quick-polled every ~2 s, ups.test.result
             only every pollfreq (~30 s), so an automatic or front-panel
             test shows its ~10 s OFF blip (NUT #2104) BEFORE the UPS says
             "In progress" -- often it is over by then. Without this window
             that blip fires the critical OUTPUT_OFF alert; docs/UPS-TOOLING
             §4: "anything reacting to OFF needs a persistence window".
             Bounded by OFF_CONFIRM_SEC, and dropped the moment an awake box
             stops answering: a test never takes the box's power away.

TRANSFER -> snap["transfer"] = {"reason", "trusted"}. input.transfer.reason
is refreshed only every pollfreq, so at the on-battery edge it may still
describe the PREVIOUS transfer. Trusted once pollfreq+2 s have passed since
the latest on-battery edge -- whether still on battery or back on mains: a
10 s blip never got a fresh read, so its "reason" is the one before it.
"""

try:
    from . import upsops
except ImportError:      # lands separately; everything here works without it
    upsops = None

from . import events, states

SELFTEST_WINDOW = 90.0    # the contract's "dashboard-started < 90 s ago"
OFF_CONFIRM_SEC = 20.0    # blip is ~10 s; past this an OFF is real
CUT_MEMORY = 300.0        # a cut of ours this recent explains any OFF
REASON_MARGIN = 2.0
DEFAULT_POLLFREQ = 30


def _num(v):
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def dashboard_started():
    """upsops.selftest_started() -> ts|None. Never raises."""
    fn = getattr(upsops, "selftest_started", None)
    try:
        ts = fn() if fn else None
    except Exception:
        return None
    return ts if _num(ts) else None


def _our_cut_recent(cut, now):
    cut = cut or {}
    if cut.get("active"):
        return True
    at = cut.get("started")
    return (cut.get("phase") in ("cutting", "done")
            and (not _num(at) or now - at < CUT_MEMORY))


class UpsExtras(object):
    def __init__(self):
        self._st_since = None     # start of the current active stretch
        self._st_dash = False
        self._res_last = None     # last readable ups.test_result
        self._verdict_at = None   # when a progress -> final edge was read
        self._off_since = None    # OFF edge; 0.0 = already OFF when first seen
        self._off_last = None     # last CONFIRMED OFF value
        self._off_box_awake = False
        self._ob_last = None      # last CONFIRMED on_battery value
        self._ob_edge = None      # latest on-battery edge
        self._xfer_logged = None  # episode id the transfer cause was stored for

    def self_test(self, now, ups, cut=None, box_state=None):
        ups = ups or {}
        ok = bool(ups.get("ok"))
        flags = ups.get("flags") or []
        ups_st = ups.get("self_test")
        if "self_test" not in ups and ok:          # pre-contract upsblock
            ups_st = "progress" in (ups.get("test_result") or "").lower()
        res = ups.get("test_result") if ok else None
        if isinstance(res, str):
            if events.self_test_verdict(self._res_last, res):
                self._verdict_at = now
            self._res_last = res
        dash_ts = dashboard_started()
        by_dash = (dash_ts is not None and now - dash_ts < SELFTEST_WINDOW
                   and not (self._verdict_at is not None
                            and self._verdict_at >= dash_ts))

        if ups_st is True or by_dash:
            active = True
        else:
            active = False if ups_st is False else None
        if active is True:
            if self._st_since is None:
                self._st_since = dash_ts if by_dash else now
            self._st_dash = self._st_dash or by_dash
        elif active is False:
            self._st_since, self._st_dash = None, False

        off = ("OFF" in flags) if ok else None
        if off is True and self._off_since is None:
            # Only an edge we SAW is young. OFF on the first read after a
            # start or an unreadable stretch is a latch of unknown age.
            self._off_since = now if self._off_last is False else 0.0
            self._off_box_awake = (box_state == "awake")
        elif off is False:
            self._off_since = None
        if off is not None:
            self._off_last = off
        suspected = bool(
            off is True and active is not True and self._off_since
            and now - self._off_since < OFF_CONFIRM_SEC
            and "OL" in flags and not {"OB", "OVER", "LB"} & set(flags)
            and not _our_cut_recent(cut, now)
            and not (self._off_box_awake and box_state != "awake"))

        started = (self._st_since if active is True
                   else self._off_since if suspected else None)
        return {"active": active, "started": started,
                "by_dashboard": bool(active is True and self._st_dash),
                "result": ups.get("test_result"), "suspected": suspected}

    def transfer(self, now, ups):
        ups = ups or {}
        ob = ups.get("on_battery")
        if ob is True and self._ob_last is not True:
            self._ob_edge = now
        if ob is not None:
            self._ob_last = ob
        pf = ups.get("pollfreq")
        pf = pf if _num(pf) and pf > 0 else DEFAULT_POLLFREQ
        reason = ups.get("transfer_reason")
        reason = reason.strip() if isinstance(reason, str) and reason.strip() else None
        settled = self._ob_edge is None or now - self._ob_edge >= pf + REASON_MARGIN
        return {"reason": reason,
                "trusted": bool(reason and ob is not None and settled)}

    def record(self, store, prev, snap, episode_id, testing, now):
        """The two store writes. Never raises."""
        try:
            self._record(store, prev or {}, snap, episode_id, testing, now)
        except Exception:
            pass

    def _record(self, store, prev, snap, episode_id, testing, now):
        ups = snap.get("ups") or {}
        xfer = snap.get("transfer") or {}
        # Once per outage episode, the first tick the reason can be believed.
        if (episode_id is not None and episode_id != self._xfer_logged
                and ups.get("on_battery") is True and not testing
                and xfer.get("trusted")):
            self._xfer_logged = episode_id
            store.add_event(now, states.TRANSFER_CAUSE, episode_id,
                            {"reason": xfer.get("reason")})
        if not prev:
            return          # first tick: nothing lived through yet
        verdict = events.self_test_verdict(
            (prev.get("ups") or {}).get("test_result"), ups.get("test_result"))
        if verdict:
            store.add_event(now, verdict, episode_id,
                            {"result": ups.get("test_result"),
                             "batt_v": ups.get("batt_v"),
                             "charge": ups.get("charge")})
        # The UPS's own tests only: actions.py logs the ones it starts.
        st = snap.get("self_test") or {}
        if (st.get("active") is True and not st.get("by_dashboard")
                and (prev.get("self_test") or {}).get("active") is not True):
            store.add_event(now, states.SELF_TEST_STARTED, episode_id,
                            {"by_dashboard": False,
                             "result": ups.get("test_result")})
