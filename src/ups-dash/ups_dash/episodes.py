"""Episode detection.

An episode is a contiguous period of "not normal" -- an outage, or the box
being absent.  It deliberately spans the WHOLE story: outage, hibernate, mains
back, gate wait, wake.  One card in HISTORY then tells the complete arc rather
than fragmenting it into four unrelated rows.

Closing requires a calm period, so a flapping supply does not shred one outage
into a dozen episodes.

THE STORY (2026-09-22): the episode row always had `hibernated`,
`resumed_at` and `wake_cause` columns, but nothing ever wrote them, so every
card in HISTORY read "rode it out" -- including outages in which the box
hibernated, the UPS parked and the sentinel woke it. _story() now records
what the box and UPS actually did, and _close() writes it as `summary`.
"""

import time

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
               park_active=False, park=None, last_wol=None):
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
            self._story(now, box_state, charge, park, last_wol)
        elif self.current is not None:
            self._story(now, box_state, charge, park, last_wol)
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

    def _story(self, now, box_state, charge, park, last_wol):
        """Note what the box and the UPS did, for HISTORY. Never raises."""
        try:
            cur, prev = self.current, getattr(self, "_box_prev", None)
            self._box_prev = box_state
            down = box_state in ("hibernated", "unreachable")
            if prev == "awake" and down and cur.get("down_charge") is None:
                cur["down_charge"] = charge
                self.store.update_episode(cur["id"], hibernated=1)
            pk = park or {}
            if pk.get("phase") in ("armed", "parked") and cur.get("park_charge") is None:
                cur["park_charge"] = pk.get("charge", charge)
            if prev in ("hibernated", "unreachable") and box_state == "awake":
                # Last wins: after a park the box powers on with the mains and
                # is put back to sleep, then the sentinel's WoL wakes it.
                if pk.get("phase") == "returning":
                    why = "power returning (AC BACK)"
                elif isinstance(last_wol, (int, float)) and now - last_wol < 300:
                    why = "the sentinel's WoL"
                else:
                    why = "woken outside the sentinel"
                cur["resumed_at"], cur["wake_cause"] = now, why
                self.store.update_episode(cur["id"], resumed_at=now,
                                          wake_cause=why)
        except Exception:
            pass

    def _summary(self):
        cur, bits = self.current, []
        if cur.get("down_charge") is not None:
            bits.append("hibernated at %g%%" % cur["down_charge"])
        if cur.get("park_charge") is not None:
            bits.append("UPS parked at %g%%" % cur["park_charge"])
        if cur.get("resumed_at"):
            bits.append("back %s via %s" % (
                time.strftime("%H:%M", time.localtime(cur["resumed_at"])),
                cur.get("wake_cause") or "?"))
        return " · ".join(bits) or None

    def _close(self, now, charge):
        ep_id = self.current["id"]
        self.store.update_episode(ep_id, ended=now, charge_end=charge,
                                  summary=self._summary())
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
