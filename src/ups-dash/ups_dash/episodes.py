"""Episode detection.

An episode is a contiguous period of "not normal" -- an outage, or the box
being absent.  It deliberately spans the WHOLE story: outage, hibernate, mains
back, gate wait, wake.  One card in HISTORY then tells the complete arc rather
than fragmenting it into four unrelated rows.

Closing requires a calm period, so a flapping supply does not shred one outage
into a dozen episodes.
"""

PRE_ROLL = 300           # seconds of pre-trigger context flushed into an episode
CLOSE_AFTER = 60.0       # calm required before an episode is declared over


class EpisodeTracker(object):
    def __init__(self, store):
        self.store = store
        self.current = None
        self._calm_since = None
        row = store.open_episode_row()
        if row:
            # Survived a restart mid-episode; adopt it rather than orphan it.
            self.current = {"id": row["id"], "kind": row["kind"],
                            "started": row["started"]}

    @property
    def id(self):
        return self.current["id"] if self.current else None

    def update(self, now, ups, box_state, charge, preroll, self_test=False,
               park_active=False):
        """`preroll` is a callable returning flattened rows from the ring buffer.

        `self_test` is states.self_test_explains(): a battery test can show
        DISCHRG (and, on a unit that reports it, OB) for a few seconds. That
        is not an outage and must not open one -- it would stamp a routine
        test into HISTORY as a power cut. If OB outlives the test, the next
        tick without the flag opens the episode as usual.

        `park_active` (a park marker exists, park.py) keeps the story in ONE
        episode: after a park the box powers itself on and is put back to
        sleep, and that minute awake on mains would otherwise read as calm
        and split the outage into two cards."""
        on_batt = ups.get("on_battery") is True and not self_test
        abnormal = (on_batt or box_state in ("hibernated", "unreachable")
                    or bool(park_active))
        if abnormal:
            self._calm_since = None
            if self.current is None:
                self._open(now, on_batt, box_state, charge, preroll)
            else:
                self.store.note_charge_min(self.current["id"], charge)
        elif self.current is not None:
            if self._calm_since is None:
                self._calm_since = now
            elif now - self._calm_since >= CLOSE_AFTER:
                self._close(now, charge)

    def _open(self, now, on_batt, box_state, charge, preroll):
        kind = "outage" if on_batt else "box_absent"
        ep_id = self.store.open_episode(now, kind, charge)
        self.current = {"id": ep_id, "kind": kind, "started": now}
        self.store.add_event(now, "episode_start", ep_id,
                             {"kind": kind, "charge": charge,
                              "box_state": box_state})
        # Flush pre-trigger context so the trace shows what led INTO the event,
        # not just what happened after it was already obvious.
        rows = preroll(now - PRE_ROLL)
        if rows:
            self.store.insert_many(rows, 1, ep_id)

    def _close(self, now, charge):
        ep_id = self.current["id"]
        self.store.update_episode(ep_id, ended=now, charge_end=charge)
        self.store.add_event(now, "episode_end", ep_id,
                             {"charge": charge,
                              "duration": round(now - self.current["started"], 1)})
        self.current = None
        self._calm_since = None

    def snapshot(self, now):
        if not self.current:
            return None
        out = dict(self.current)
        out["elapsed"] = round(now - self.current["started"], 1)
        return out
