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

    def update(self, now, ups, box_state, charge, preroll):
        """`preroll` is a callable returning flattened rows from the ring buffer."""
        abnormal = bool(ups.get("on_battery")) or box_state in ("hibernated",
                                                                "unreachable")
        if abnormal:
            self._calm_since = None
            if self.current is None:
                self._open(now, ups, box_state, charge, preroll)
            else:
                self.store.note_charge_min(self.current["id"], charge)
        elif self.current is not None:
            if self._calm_since is None:
                self._calm_since = now
            elif now - self._calm_since >= CLOSE_AFTER:
                self._close(now, charge)

    def _open(self, now, ups, box_state, charge, preroll):
        kind = "outage" if ups.get("on_battery") else "box_absent"
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
