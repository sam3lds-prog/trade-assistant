"""The daily shadow forecast, as a job of the app's worker (4.0, phase 4).

After the close (45 minutes after, so the daily bar has settled), the job hands the engine a fetch function over
the app's own cached daily bars, finished sessions only, and runs `intelligence.live.shadow_step`. On a computer
that serves forecasts (the M1) the daemon runs it every half hour; it does nothing until the session has closed,
and nothing more once that session is forecast.
"""

from __future__ import annotations

import sqlite3
import threading
import time
from datetime import datetime, timezone
from typing import Any

import pandas as pd

from . import replay_store


def fetcher(svc: Any):
    years = 2.3

    def fetch(symbol: str, start: str, end: str) -> pd.DataFrame:
        bars = svc.market.bars(symbol, years=years, complete=True)
        return bars[(bars["date"] >= pd.Timestamp(start)) & (bars["date"] <= pd.Timestamp(end))]
    return fetch


_LOCK = threading.Lock()


def run_shadow(svc: Any, now: datetime | None = None, force: bool = False) -> dict[str, Any]:
    """One shadow step at a time in this process (the job and the screen's button share the lock); a step another
    process (the daemon) finished a moment earlier is reported, not an error."""
    from ..intelligence.live import after_close, shadow_step
    from ..market import settled_through
    now = now or datetime.now(timezone.utc)
    if not force and not after_close(now):
        return {"skipped": "the session has not closed and settled yet"}
    if not _LOCK.acquire(blocking=False):
        return {"skipped": "the shadow forecast is being made right now"}
    try:
        settled = settled_through(now.timestamp() if now else time.time())
        return shadow_step(replay_store(svc), fetcher(svc), settled, now)
    except sqlite3.IntegrityError:
        return {"skipped": "another process made this forecast a moment ago"}
    finally:
        _LOCK.release()


__all__ = ["run_shadow", "fetcher"]
