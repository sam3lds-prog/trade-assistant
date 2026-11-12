"""Research's background workers (4.2), separate from the Trading Desk's.

The Trading Desk's worker drains its jobs one after another (submissions, reconciliation, notifications); a
calibration that reads years of history for twenty stocks, or a scenario provider that takes minutes to answer, must
never sit in front of an order. So Research has workers of its own, in the app's process, each claiming only its
own kinds of job from the same durable queue (jobs.py), with its own lease:

- **calibration**: `research.calibrate` (one horizon's forecast record: a walk-forward test on history) and
  `research.warm` (every six hours: queue the 1- and 5-session calibrations that are missing, out of date or made
  with an older method, once real market data is connected). A calibration renews its lease as it goes, so a long
  one is not taken over, and one interrupted by a restart is taken up again when its lease runs out.
- **scenarios**: the one-click pipeline's steps (4.3), each a short bounded step that queues the next one.

They start when there is work for them (a job queued, or left over from before a restart), never on their own in a
process that has none, and not at all with DESK_WORKER=off (where the caller drains them, as the tests do).
"""

from __future__ import annotations

import logging
import threading
from typing import Any, Callable

from ..jobs import Jobs, Schedule, Worker

log = logging.getLogger(__name__)

CALIBRATION_KINDS = ("research.calibrate", "research.warm")
SCENARIO_KINDS = ("scenario.start", "scenario.step", "scenario.finish", "scenario.recover", "scenario.grade", "scenario.verify_trigger")


class ResearchWorkers:
    def __init__(self, svc: Any):
        self.svc = svc
        self._lock = threading.Lock()
        self.handlers: dict[str, dict[str, Callable[[dict[str, Any]], Any]]] = {"calibration": {}, "scenarios": {}}
        self.schedules: dict[str, list[Schedule]] = {"calibration": [], "scenarios": []}
        self.leases = {"calibration": 300.0, "scenarios": 180.0}
        self.workers: dict[str, Worker] = {}

    def add(self, pool: str, kind: str, fn: Callable[[dict[str, Any]], Any]) -> None:
        self.handlers[pool][kind] = fn
        w = self.workers.get(pool)
        if w is not None:                      # a worker already made (at start, for left-over work) takes it on too
            w.handlers[kind] = fn

    def schedule(self, pool: str, sched: Schedule) -> None:
        self.schedules[pool].append(sched)
        w = self.workers.get(pool)
        if w is not None and sched not in w.schedules:
            w.schedules.append(sched)

    def worker(self, pool: str) -> Worker:
        with self._lock:
            w = self.workers.get(pool)
            if w is None:
                w = Worker(self.svc.db, dict(self.handlers[pool]), list(self.schedules[pool]), component=f"research-{pool}",
                           lease_seconds=self.leases[pool])
                self.workers[pool] = w
            return w

    @property
    def automatic(self) -> bool:
        return getattr(self.svc.settings, "desk_worker", "auto") != "off"

    def ensure(self, pool: str) -> None:
        """Start the pool's worker thread (once), unless the app runs no workers of its own."""
        if not self.automatic or not self.handlers[pool]:
            return
        w = self.worker(pool)
        if not w.running():
            w.start()

    def pending(self, pool: str) -> bool:
        kinds = list(self.handlers[pool])
        if not kinds:
            return False
        r = self.svc.db.one(f"SELECT COUNT(*) AS n FROM jobs WHERE status IN ('queued', 'running') AND kind IN ({','.join('?' * len(kinds))})", kinds)
        return bool(r and r["n"])

    def resume(self) -> None:
        """At start: a worker for any pool with work left over (queued, or running when the app stopped)."""
        for pool in self.handlers:
            try:
                if self.pending(pool):
                    self.ensure(pool)
            except Exception:  # noqa: BLE001 - an old database is migrated before use; never block start
                log.exception("Research workers could not check for left-over work")

    def drain(self, pool: str, limit: int = 50) -> list[dict[str, Any]]:
        """Run what is due now, in the caller's thread (DESK_WORKER=off, and the tests)."""
        return self.worker(pool).drain(limit)

    def enqueue(self, pool: str, kind: str, payload: dict[str, Any], **kw: Any) -> int | None:
        jid = Jobs(self.svc.db).enqueue(kind, payload, **kw)
        self.ensure(pool)
        return jid

    def status(self) -> dict[str, Any]:
        return {pool: {"running": bool(w.running()), "last_error": w.last_error} for pool, w in self.workers.items()}

    def shutdown(self) -> None:
        for w in self.workers.values():
            w.shutdown()


def workers_for(svc: Any) -> ResearchWorkers:
    rw = getattr(svc, "research_workers", None)
    if rw is None:
        rw = svc.research_workers = ResearchWorkers(svc)
    return rw


__all__ = ["ResearchWorkers", "workers_for", "CALIBRATION_KINDS", "SCENARIO_KINDS"]
