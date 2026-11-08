"""4.0 in the app: the replay store, the routes under /v4, the scheduled jobs, the command line (see docs/V4.md).

The learning engine itself lives in tradeassist/intelligence and cannot reach an order path. This package connects it
to the app: it opens data/replay.db, hands the engine a history-only fetch function when a dataset is built, serves
the results, and registers the jobs (grading Research's forecasts) with the job queue that the daemon, or the app
when no daemon runs, works through.
"""

from __future__ import annotations

import threading
from typing import Any

_LOCK = threading.Lock()


def replay_store(svc: Any):
    """The app's one connection to replay.db (opened on first use; in memory when the app's database is)."""
    from ..intelligence.store import ReplayStore, replay_path
    with _LOCK:
        store = getattr(svc, "_replay_store", None)
        if store is None:
            path = ":memory:" if str(svc.settings.db_path) == ":memory:" else replay_path(svc.settings)
            store = svc._replay_store = ReplayStore(path)
        return store


def has_replays(svc: Any) -> bool:
    """Whether replay.db exists yet (the jobs do not create it: a user who never runs a replay never gets the file)."""
    from ..intelligence.store import replay_path
    return getattr(svc, "_replay_store", None) is not None or (str(svc.settings.db_path) != ":memory:" and replay_path(svc.settings).exists())


def jobs(svc: Any) -> tuple[dict[str, Any], list[Any]]:
    """The 4.0 jobs for the worker: grading Research's live forecasts once an hour, and the deployed models' shadow
    forecast after each close (checked every half hour; it acts once per session)."""
    from ..jobs import Schedule
    from .grading import grade_research

    def grade(_: dict[str, Any]) -> Any:
        from ..marketdays import phase
        if phase() == "open":                 # it fetches bars; during the session the worker is left to the orders
            return {"skipped": "the market is open"}
        return grade_research(svc)

    def shadow(_: dict[str, Any]) -> Any:
        if not has_replays(svc):
            return {"skipped": "no replays yet"}
        from .shadow import run_shadow
        return run_shadow(svc)
    handlers = {"research.grade": grade, "shadow.forecast": shadow}
    schedules = [Schedule("research.grade", 3600, 1800), Schedule("shadow.forecast", 1800, 1200)]
    return handlers, schedules


def install(svc: Any) -> None:
    if getattr(svc, "_v4_installed", False):
        return
    exts = getattr(svc, "job_extensions", None)
    if exts is None:
        exts = svc.job_extensions = []
    exts.append(jobs)
    svc._v4_installed = True


__all__ = ["replay_store", "install", "jobs", "has_replays"]
