"""The durable job queue (3.0 phase 5): work the Trading Desk must not lose or repeat by accident.

A job is a row in `jobs`: its kind, its payload, when it is due, and a `dedupe_key` so the same work is queued once.
A worker claims a job with a lease; if the worker dies, the lease runs out and another claims it (each handler is
written to be safe to run again: a submission re-reads the proposal and never places twice, see v3/trading.py).

Timed work is queued one slot at a time (`Schedule`): every N seconds, the job for the current slot, which goes
stale shortly after. A computer that slept through the afternoon wakes to the current slot only: the missed ones
were never queued, and any queued before the sleep are discarded as stale instead of run late.
"""

from __future__ import annotations

import json
import logging
import os
import socket
import sqlite3
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Callable

from .optionmath import NY

log = logging.getLogger(__name__)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(t: datetime) -> str:
    return t.astimezone(timezone.utc).isoformat(timespec="seconds")


class Jobs:
    def __init__(self, db: Any):
        self.db = db

    def enqueue(self, kind: str, payload: dict[str, Any] | None = None, *, due_at: datetime | None = None, dedupe_key: str | None = None,
                stale_after: datetime | None = None, max_attempts: int = 3) -> int | None:
        """Queue a job; with a `dedupe_key` already queued or done, nothing is added (returns None)."""
        now = _now()
        with self.db.tx() as cur:
            cur.execute("""INSERT OR IGNORE INTO jobs (kind, dedupe_key, status, due_at, stale_after, created_at, max_attempts, payload_json)
                           VALUES (?, ?, 'queued', ?, ?, ?, ?, ?)""",
                        (kind, dedupe_key, _iso(due_at or now), _iso(stale_after) if stale_after else None, _iso(now), max_attempts,
                         json.dumps(payload or {}, default=str)))
            return int(cur.lastrowid) if cur.rowcount else None

    def discard_stale(self) -> int:
        with self.db.tx() as cur:
            cur.execute("UPDATE jobs SET status = 'discarded', finished_at = ?, error = 'stale: its moment passed' "
                        "WHERE status = 'queued' AND stale_after IS NOT NULL AND stale_after < ?", (_iso(_now()), _iso(_now())))
            return cur.rowcount

    def recover(self) -> int:
        """Jobs whose worker stopped mid-run (its lease ran out) go back on the queue, or fail after their attempts."""
        now = _iso(_now())
        with self.db.tx() as cur:
            cur.execute("UPDATE jobs SET status = 'failed', finished_at = ?, error = 'the worker stopped while running it, too many times' "
                        "WHERE status = 'running' AND lease_until < ? AND attempts >= max_attempts", (now, now))
            failed = cur.rowcount
            cur.execute("UPDATE jobs SET status = 'queued', worker = NULL, lease_until = NULL WHERE status = 'running' AND lease_until < ?",
                        (now,))
            return failed + cur.rowcount

    def claim(self, worker: str, kinds: list[str] | None = None, lease_seconds: float = 120.0) -> dict[str, Any] | None:
        self.discard_stale()
        self.recover()
        now = _now()
        with self.db.tx() as cur:
            sql = "SELECT * FROM jobs WHERE status = 'queued' AND due_at <= ?"
            params: list[Any] = [_iso(now)]
            if kinds:
                sql += f" AND kind IN ({','.join('?' * len(kinds))})"
                params += kinds
            row = cur.execute(sql + " ORDER BY due_at, id LIMIT 1", params).fetchone()
            if row is None:
                return None
            cur.execute("UPDATE jobs SET status = 'running', started_at = ?, attempts = attempts + 1, lease_until = ?, worker = ? "
                        "WHERE id = ? AND status = 'queued'", (_iso(now), _iso(now + timedelta(seconds=lease_seconds)), worker, row["id"]))
            if not cur.rowcount:
                return None
        job = dict(row)
        job["payload"] = json.loads(job.pop("payload_json") or "{}")
        job.update(attempts=job["attempts"] + 1, status="running", worker=worker)
        return job

    def finish(self, job_id: int, result: Any = None) -> None:
        with self.db.tx() as cur:
            cur.execute("UPDATE jobs SET status = 'done', finished_at = ?, lease_until = NULL, result_json = ? WHERE id = ?",
                        (_iso(_now()), json.dumps(result, default=str)[:4000] if result is not None else None, job_id))

    def fail(self, job_id: int, error: str, retry_seconds: float = 30.0) -> str:
        with self.db.tx() as cur:
            row = cur.execute("SELECT attempts, max_attempts FROM jobs WHERE id = ?", (job_id,)).fetchone()
            if row and row["attempts"] < row["max_attempts"]:
                delay = retry_seconds * (2 ** (row["attempts"] - 1))
                cur.execute("UPDATE jobs SET status = 'queued', due_at = ?, lease_until = NULL, worker = NULL, error = ? WHERE id = ?",
                            (_iso(_now() + timedelta(seconds=delay)), error[:500], job_id))
                return "queued"
            cur.execute("UPDATE jobs SET status = 'failed', finished_at = ?, lease_until = NULL, error = ? WHERE id = ?",
                        (_iso(_now()), error[:500], job_id))
            return "failed"

    def recent(self, limit: int = 50) -> list[dict[str, Any]]:
        rows = self.db.query("SELECT id, kind, status, due_at, created_at, started_at, finished_at, attempts, worker, error FROM jobs "
                             "ORDER BY id DESC LIMIT ?", (limit,))
        return [dict(r) for r in rows]

    def counts(self) -> dict[str, int]:
        return {r["status"]: r["n"] for r in self.db.query("SELECT status, COUNT(*) AS n FROM jobs GROUP BY status")}

    def prune(self, keep_days: int = 14) -> int:
        with self.db.tx() as cur:
            cur.execute("DELETE FROM jobs WHERE status IN ('done', 'discarded') AND finished_at < ?",
                        (_iso(_now() - timedelta(days=keep_days)),))
            return cur.rowcount


@dataclass
class Schedule:
    kind: str
    every_seconds: int
    stale_seconds: int
    when: str = "always"          # always | market (weekdays 9:25 to 16:10 New York)
    payload: dict[str, Any] | None = None

    def slot(self, now: datetime) -> datetime:
        epoch = int(now.timestamp())
        return datetime.fromtimestamp(epoch - epoch % self.every_seconds, timezone.utc)

    def due(self, now: datetime) -> bool:
        if self.when == "always":
            return True
        ny = now.astimezone(NY)
        return ny.weekday() < 5 and (9, 25) <= (ny.hour, ny.minute) < (16, 10)


def schedule_tick(jobs: Jobs, schedules: list[Schedule], now: datetime | None = None) -> list[int]:
    """Queue the current slot of every schedule that is due (once: the slot is the dedupe key)."""
    now = now or _now()
    added = []
    for s in schedules:
        if not s.due(now):
            continue
        slot = s.slot(now)
        jid = jobs.enqueue(s.kind, s.payload, due_at=slot, dedupe_key=f"{s.kind}@{_iso(slot)}",
                           stale_after=slot + timedelta(seconds=s.stale_seconds), max_attempts=1)
        if jid:
            added.append(jid)
    return added


def heartbeat(db: Any, component: str, **info: Any) -> None:
    with db.tx() as cur:
        cur.execute("INSERT INTO heartbeats (component, at, pid, host, info_json) VALUES (?, ?, ?, ?, ?) "
                    "ON CONFLICT(component) DO UPDATE SET at = excluded.at, pid = excluded.pid, host = excluded.host, info_json = excluded.info_json",
                    (component, _iso(_now()), os.getpid(), socket.gethostname(), json.dumps(info, default=str)))


def last_heartbeat(db: Any, component: str) -> dict[str, Any] | None:
    r = db.one("SELECT * FROM heartbeats WHERE component = ?", (component,))
    if r is None:
        return None
    at = datetime.fromisoformat(r["at"])
    return {"component": component, "at": r["at"], "age_seconds": (_now() - at).total_seconds(), "pid": r["pid"], "host": r["host"],
            "info": json.loads(r["info_json"] or "{}")}


def alive(db: Any, component: str = "daemon", within: float = 45.0) -> bool:
    hb = last_heartbeat(db, component)
    return bool(hb and hb["age_seconds"] <= within)


class Worker:
    """Runs queued jobs with the handlers it was given. `run_once` does one job; `start` loops in a thread (the app's
    own worker when no daemon runs); the daemon calls `loop` in its main thread."""

    def __init__(self, db: Any, handlers: dict[str, Callable[[dict[str, Any]], Any]], schedules: list[Schedule] | None = None, *,
                 component: str = "app-worker", info: Callable[[], dict[str, Any]] | None = None,
                 defer_to: str | None = None):
        self.db = db
        self.jobs = Jobs(db)
        self.handlers = handlers
        self.schedules = schedules or []
        self.component = component
        self.info = info or (lambda: {})
        self.defer_to = defer_to          # the app's worker idles while this component (the daemon) is alive
        self.name = f"{component}:{os.getpid()}"
        self.stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._beat = 0.0
        self.last_error: str | None = None

    def deferring(self) -> bool:
        return bool(self.defer_to and alive(self.db, self.defer_to))

    def run_once(self) -> dict[str, Any] | None:
        job = self.jobs.claim(self.name, list(self.handlers))
        if job is None:
            return None
        fn = self.handlers[job["kind"]]
        try:
            result = fn(job["payload"])
        except Exception as exc:  # noqa: BLE001 - a failed job is recorded and retried, never lost
            self.last_error = f"{job['kind']}: {exc.__class__.__name__}: {str(exc)[:200]}"
            log.exception("Job %s (%s) failed", job["id"], job["kind"])
            state = self.jobs.fail(job["id"], f"{exc.__class__.__name__}: {exc}")
            return {"id": job["id"], "kind": job["kind"], "status": state, "error": str(exc)[:200]}
        self.jobs.finish(job["id"], result)
        return {"id": job["id"], "kind": job["kind"], "status": "done", "result": result}

    def drain(self, limit: int = 50) -> list[dict[str, Any]]:
        out = []
        for _ in range(limit):
            r = self.run_once()
            if r is None:
                break
            out.append(r)
        return out

    def step(self) -> int:
        """One pass: heartbeat, the schedules, then the jobs that are due."""
        if time.time() - self._beat >= 10:
            heartbeat(self.db, self.component, worker=self.name, **self.info())
            self._beat = time.time()
        if self.deferring():
            return 0
        schedule_tick(self.jobs, self.schedules)
        return len(self.drain())

    def loop(self, poll_seconds: float = 1.0) -> None:
        while not self.stop.is_set():
            try:
                self.step()
            except sqlite3.ProgrammingError as exc:
                if "closed database" in str(exc):     # the app is shutting down: there is nothing left to work on
                    self.last_error = "the database was closed"
                    return
                self.last_error = f"{exc.__class__.__name__}: {str(exc)[:200]}"
                log.exception("Worker pass failed")
            except Exception as exc:  # noqa: BLE001 - the loop must survive anything
                self.last_error = f"{exc.__class__.__name__}: {str(exc)[:200]}"
                log.exception("Worker pass failed")
            self.stop.wait(poll_seconds)

    def beat_in_background(self, every: float = 10.0) -> threading.Thread:
        """Keep the heartbeat going from a thread of its own (the daemon): a job that takes minutes must not let the
        app's worker conclude the daemon has stopped and take over its work."""
        def beat() -> None:
            while not self.stop.is_set():
                try:
                    heartbeat(self.db, self.component, worker=self.name, **self.info())
                    self._beat = time.time()
                except Exception:  # noqa: BLE001 - the next beat tries again
                    log.exception("Heartbeat failed")
                self.stop.wait(every)
        t = threading.Thread(target=beat, name=f"{self.component}-heartbeat", daemon=True)
        t.start()
        return t

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self.stop = threading.Event()
        self._thread = threading.Thread(target=self.loop, name=self.component, daemon=True)
        self._thread.start()

    def running(self) -> bool:
        return bool(self._thread and self._thread.is_alive())

    def shutdown(self, timeout: float = 5.0) -> None:
        """Stop the loop and wait for its current pass to finish (before the database it uses is closed)."""
        self.stop.set()
        if self._thread is not None:
            self._thread.join(timeout)


__all__ = ["Jobs", "Schedule", "Worker", "schedule_tick", "heartbeat", "last_heartbeat", "alive"]
