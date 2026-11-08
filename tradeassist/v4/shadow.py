"""The daily shadow forecast, as a job of the app's worker (4.0, phase 4).

After the close (45 minutes after, so the daily bar has settled), the job hands the engine a fetch function over
the app's own cached daily bars, finished sessions only, and runs `intelligence.live.shadow_step`. On a computer
that serves forecasts (the M1) the daemon runs it every half hour; it does nothing until the session has closed,
and nothing more once that session is forecast.
"""

from __future__ import annotations

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


def run_shadow(svc: Any, now: datetime | None = None, force: bool = False) -> dict[str, Any]:
    from ..intelligence.live import after_close, shadow_step
    from ..market import settled_through
    now = now or datetime.now(timezone.utc)
    if not force and not after_close(now):
        return {"skipped": "the session has not closed and settled yet"}
    settled = settled_through(now.timestamp() if now else time.time())
    return shadow_step(replay_store(svc), fetcher(svc), settled, now)


__all__ = ["run_shadow", "fetcher"]
