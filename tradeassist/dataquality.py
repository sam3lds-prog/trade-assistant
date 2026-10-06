"""Checks on a stock's daily bars before anything is read from them.

Each check says what was found and how much it matters ("info", "warn" or "fail"). The
report also says whether the last bar is finished, because a signal from today's bar so far
is provisional (see market.py) and must never be shown as confirmed.
"""

from __future__ import annotations

import time
from typing import Any

import numpy as np
import pandas as pd

from .market import SUSPECT_MOVE, complete_only, settled_through

MIN_BARS = 260            # about a year: the 52-week measures and the 200-day average need it
MISSING_SESSIONS = 3      # a run of this many absent weekdays is more than a market holiday
ADJUSTMENT_WORDS = {
    "none": "Synthetic prices: there are no splits or dividends to adjust for.",
    "split": "The source adjusts these prices for splits but not for dividends.",
    "split_dividend": "The source adjusts these prices for splits and dividends.",
    "unknown": "The source does not say how its daily prices are adjusted. The bars are checked below for "
               "one-day jumps that look like an unadjusted split.",
}


def _check(code: str, level: str, text: str, **extra: Any) -> dict[str, Any]:
    return {"code": code, "level": level, "text": text, **extra}


def assess(symbol: str, bars: pd.DataFrame, info: dict[str, Any], now: float | None = None) -> dict[str, Any]:
    """Data-quality report for `bars` (as returned by Market.bars) and the cache record `info`."""
    now = time.time() if now is None else now
    checks: list[dict[str, Any]] = []
    if bars.empty:
        return {"symbol": symbol, "status": "fail", "bars": 0,
                "checks": [_check("NO_DATA", "fail", f"No daily bars for {symbol}.")]}
    done = complete_only(bars)
    last_date = bars["date"].iloc[-1].date()
    partial = len(done) < len(bars)
    last_complete = done["date"].iloc[-1].date() if len(done) else None

    if partial:
        checks.append(_check("PARTIAL_BAR", "info",
                             f"The bar for {last_date:%b} {last_date.day} is still forming (fetched "
                             f"{info.get('fetched_text') or info.get('fetched_at') or 'earlier'}). Signals that use it are provisional; "
                             "confirmed signals use the last finished bar."))
    expected = settled_through(now)
    if last_complete is not None:
        behind = int(np.busday_count(last_complete, expected))
        if behind > 1:
            checks.append(_check("STALE", "warn",
                                 f"The last finished bar is from {last_complete:%b} {last_complete.day}, {behind} "
                                 "weekdays before the latest close. Either the market was shut or a newer bar "
                                 "is missing.", weekdays_behind=behind))

    o, h, l, c = (bars[k].to_numpy(dtype=float) for k in ("open", "high", "low", "close"))
    v = bars["volume"].to_numpy(dtype=float)
    bad_price = ~np.isfinite(c) | (c <= 0) | (l <= 0)
    if bad_price.any():
        days = ", ".join(d.strftime("%Y-%m-%d") for d in bars.loc[bad_price, "date"].head(3))
        checks.append(_check("NON_POSITIVE", "fail", f"{int(bad_price.sum())} bars have a zero, negative or "
                                                     f"missing price ({days}).", count=int(bad_price.sum())))
    tol = 1e-6 * np.maximum(np.abs(c), 1)
    inconsistent = (h + tol < np.maximum(o, c)) | (l - tol > np.minimum(o, c)) | (l > h + tol)
    if inconsistent.any():
        days = ", ".join(d.strftime("%Y-%m-%d") for d in bars.loc[inconsistent, "date"].head(3))
        checks.append(_check("OHLC_INCONSISTENT", "warn",
                             f"{int(inconsistent.sum())} bars have a high below the open or close, or a low above "
                             f"them ({days}). Ranges and candle patterns on those days are unreliable.",
                             count=int(inconsistent.sum())))
    recent = bars["date"] >= bars["date"].iloc[-1] - pd.Timedelta(days=365)
    zero = (v <= 0) & recent.to_numpy()
    if partial:
        zero[-1] = False                       # a bar that has only just started can have no volume yet
    if zero.any():
        checks.append(_check("ZERO_VOLUME", "warn" if zero.sum() > 5 else "info",
                             f"{int(zero.sum())} days in the last year show no volume. Volume measures are "
                             "unreliable for this stock.", count=int(zero.sum())))

    days = bars["date"].dt.date.to_numpy()
    gaps = []
    for a, b in zip(days[:-1], days[1:]):
        missing = int(np.busday_count(a, b)) - 1
        if missing >= MISSING_SESSIONS:
            gaps.append((a, b, missing))
    if gaps:
        a, b, n = max(gaps, key=lambda g: g[2])
        checks.append(_check("GAPS", "warn", f"{len(gaps)} stretch{'es' if len(gaps) != 1 else ''} of "
                                             f"{MISSING_SESSIONS} or more weekdays with no bar; the longest is {n} "
                                             f"weekdays after {a.isoformat()}.", count=len(gaps)))

    moves = pd.Series(c).pct_change().abs().to_numpy()
    jumps = np.flatnonzero(moves > SUSPECT_MOVE)
    if len(jumps):
        when = ", ".join(bars["date"].iloc[i].strftime("%Y-%m-%d") for i in jumps[:3])
        checks.append(_check("SUSPECT_SPLIT", "warn",
                             f"One-day moves over {SUSPECT_MOVE:.0%} on {when}. This may be a split that is not "
                             "adjusted in the history; indicators and studies across that date may be wrong.",
                             dates=[bars["date"].iloc[i].strftime("%Y-%m-%d") for i in jumps[:10]]))
    if len(done) < MIN_BARS:
        checks.append(_check("SHORT_HISTORY", "warn", f"Only {len(done)} finished daily bars; 52-week measures and "
                                                      "the 200-day average are incomplete."))
    adjustment = info.get("adjustment") or "unknown"
    checks.append(_check("ADJUSTMENT", "info", ADJUSTMENT_WORDS.get(adjustment, ADJUSTMENT_WORDS["unknown"]),
                         adjustment=adjustment))

    levels = {c["level"] for c in checks}
    status = "fail" if "fail" in levels else "warn" if "warn" in levels else "ok"
    return {"symbol": symbol, "status": status, "bars": int(len(bars)),
            "first_date": bars["date"].iloc[0].strftime("%Y-%m-%d"), "last_date": last_date.isoformat(),
            "last_complete_date": last_complete.isoformat() if last_complete else None,
            "last_bar_status": "PARTIAL" if partial else "COMPLETE",
            "source": info.get("source"), "fetched_at": info.get("fetched_at"), "adjustment": adjustment,
            "checks": checks}
