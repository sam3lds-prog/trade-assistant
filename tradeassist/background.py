"""One background job at a time, shared by the long scans (trade ideas, the morning plan).

A job runs in one daemon thread, reports progress, can be stopped between steps, and never lets one
exception escape the thread: the job ends as "failed" with a short, redacted reason. The caller gives the
work as a callable taking a `progress(done, total, current)` function; calling progress raises JobStopped
once stop() has been asked, so the work ends cleanly between steps.
"""

from __future__ import annotations

import logging
import threading
from datetime import datetime
from typing import Any, Callable

from .events import redact
from .optionmath import NY
from .providers.base import DataError

log = logging.getLogger(__name__)


class JobStopped(Exception):
    pass


def why(exc: BaseException) -> str:
    """What went wrong, for the screen: the message of an expected error, the kind of an unexpected one."""
    if isinstance(exc, (DataError, ValueError)):
        return redact(str(exc))[:300]
    return f"an unexpected {type(exc).__name__}"


class OneJob:
    """The job slot. `keep(result, job)` runs on success, inside no lock, before the job is marked done."""

    def __init__(self, name: str, keep: Callable[[Any, dict[str, Any]], None] | None = None):
        self.name = name
        self._keep = keep
        self._lock = threading.Lock()
        self._job: dict[str, Any] | None = None
        self._runs = 0

    def start(self, meta: dict[str, Any], total: int, work: Callable[[Callable[[int, int, str | None], None]], Any],
              ) -> dict[str, Any]:
        """Start `work` in the background unless a job is already running; either way, the brief status."""
        with self._lock:
            if self._job and self._job["state"] == "running":
                return {**self.status_brief_locked(), "already_running": True}
            self._runs += 1
            job = {"id": self._runs, "state": "running", **meta, "total": total, "done": 0, "current": None,
                   "started_at": datetime.now(NY).isoformat(timespec="seconds"), "error": None, "stop": False}
            self._job = job
        try:
            threading.Thread(target=self._run, args=(job, work), name=self.name, daemon=True).start()
        except RuntimeError as exc:                   # no thread could be started: say so, never "running" forever
            with self._lock:
                job.update(state="failed", error=why(exc))
        return self.status_brief()

    def _run(self, job: dict[str, Any], work) -> None:
        def progress(done: int, total: int, current: str | None) -> None:
            with self._lock:
                job.update(done=min(done, total), total=total, current=current)
                if job["stop"]:
                    raise JobStopped()
        try:
            result = work(progress)
            if self._keep is not None:
                self._keep(result, job)
            with self._lock:
                job.update(state="done", done=job["total"], current=None,
                           finished_at=datetime.now(NY).isoformat(timespec="seconds"))
        except JobStopped:
            with self._lock:
                job.update(state="stopped", current=None)
        except Exception as exc:                      # noqa: BLE001 - reported on the screen, not raised
            log.exception("%s failed", self.name)
            with self._lock:
                job.update(state="failed", error=why(exc), current=None)

    def stop(self) -> dict[str, Any]:
        with self._lock:
            if self._job and self._job["state"] == "running":
                self._job["stop"] = True
            return self.status_brief_locked()

    def status_brief(self) -> dict[str, Any]:
        with self._lock:
            return self.status_brief_locked()

    def status_brief_locked(self) -> dict[str, Any]:
        return {"job": ({k: v for k, v in self._job.items() if k != "stop"} if self._job else {"state": "idle"})}


__all__ = ["OneJob", "JobStopped", "why"]
