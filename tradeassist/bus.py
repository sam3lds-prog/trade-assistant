"""The shared event stream (3.0): one ordered record of what changed, for every screen and both processes.

A widget no longer polls its own endpoint to learn that a proposal was approved, an opportunity re-ranked or the
daemon paused: it reads `/v3/events?after=N` and refreshes what the event names. Events are written to the
database (`app_events`) so the trading daemon, which is a separate process, and the web app see the same
stream; a waiting reader in this process is woken at once, and one in the other process within a second.

Topics are dotted names: `proposal.updated`, `opportunity.ranked`, `study.completed`, `desk.mode`,
`daemon.heartbeat`, `notification.sent`, `workspace.saved`. An event carries ids and a little context, never a
credential (values pass through events.redact), and never the whole object: readers fetch the current state.
"""

from __future__ import annotations

import json
import threading
import time
from typing import Any

from .contracts import utc_now
from .events import redact

KEEP = 5000                  # rows kept; older events are pruned as new ones arrive
MAX_WAIT_SECONDS = 25.0


class EventBus:
    def __init__(self, db: Any, origin: str = "app"):
        self.db = db
        self.origin = origin
        self._cond = threading.Condition()
        self._writes = 0

    def publish(self, topic: str, subject: str | None = None, **data: Any) -> int:
        clean = {k: (redact(v) if isinstance(v, str) else v) for k, v in data.items()}
        with self.db.tx() as cur:
            cur.execute("INSERT INTO app_events (at, topic, subject, origin, data_json) VALUES (?, ?, ?, ?, ?)",
                        (utc_now(), topic, subject, self.origin, json.dumps(clean, default=str)[:4000]))
            event_id = int(cur.lastrowid)
            self._writes += 1
            if self._writes % 200 == 0:
                cur.execute("DELETE FROM app_events WHERE id <= ?", (event_id - KEEP,))
        with self._cond:
            self._cond.notify_all()
        return event_id

    def latest_id(self) -> int:
        row = self.db.one("SELECT MAX(id) AS m FROM app_events")
        return int(row["m"] or 0) if row else 0

    def since(self, after: int, topics: list[str] | None = None, limit: int = 200) -> list[dict[str, Any]]:
        sql, params = "SELECT * FROM app_events WHERE id > ?", [int(after)]
        if topics:
            likes = []
            for t in topics:
                if t.endswith(".*"):
                    likes.append("topic LIKE ?")
                    params.append(t[:-2] + ".%")
                else:
                    likes.append("topic = ?")
                    params.append(t)
            sql += " AND (" + " OR ".join(likes) + ")"
        sql += " ORDER BY id LIMIT ?"
        params.append(int(limit))
        return [{"id": r["id"], "at": r["at"], "topic": r["topic"], "subject": r["subject"], "origin": r["origin"],
                 "data": json.loads(r["data_json"] or "{}")} for r in self.db.query(sql, params)]

    def wait(self, after: int, topics: list[str] | None = None, timeout: float = 20.0) -> list[dict[str, Any]]:
        """Events after `after`, waiting up to `timeout` seconds for one to arrive."""
        deadline = time.monotonic() + max(0.0, min(MAX_WAIT_SECONDS, timeout))
        while True:
            got = self.since(after, topics)
            left = deadline - time.monotonic()
            if got or left <= 0:
                return got
            with self._cond:
                self._cond.wait(min(1.0, left))      # a write from the other process shows up within a second


def bus_for(svc: Any) -> EventBus:
    bus = getattr(svc, "bus", None)
    if bus is None:
        bus = svc.bus = EventBus(svc.db)
    return bus


__all__ = ["EventBus", "bus_for", "KEEP"]
