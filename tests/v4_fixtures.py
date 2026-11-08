"""Synthetic markets for the 4.0 replay tests: small, seeded, and with an edge a model can find.

Each stock's daily return has a market part, a sector part, noise, and a small mean-reverting part (yesterday's
idiosyncratic move partly undone today), so a learned model should beat "no change" on enough data while "no
change" stays the honest baseline.
"""

from __future__ import annotations

from datetime import date

import numpy as np
import pandas as pd

from tradeassist.intelligence.config import PromotionRule, ReplayConfig, TradeSpec
from tradeassist.intelligence.data import universe as U

STOCKS = ("AAPL", "MSFT", "NVDA", "ORCL", "JPM", "BAC", "GS", "MS", "XOM", "CVX")
SECTORS = {"AAPL": "XLK", "MSFT": "XLK", "NVDA": "XLK", "ORCL": "XLK", "JPM": "XLF", "BAC": "XLF", "GS": "XLF", "MS": "XLF",
           "XOM": "XLE", "CVX": "XLE"}


def sessions(start: str = "2018-01-02", n: int = 900) -> pd.DatetimeIndex:
    return pd.bdate_range(start, periods=n)


def market(n: int = 900, start: str = "2018-01-02", seed: int = 11, reversion: float = 0.25) -> dict[str, pd.DataFrame]:
    rng = np.random.default_rng(seed)
    dates = sessions(start, n)
    mkt = 0.0003 + 0.009 * rng.standard_normal(n)
    sec = {s: 0.006 * rng.standard_normal(n) for s in ("XLK", "XLF", "XLE")}
    out: dict[str, pd.DataFrame] = {}

    def bars(rets: np.ndarray, base: float, key: int) -> pd.DataFrame:
        r = np.random.default_rng(seed * 100 + key)
        close = base * np.exp(np.cumsum(rets))
        prev = np.concatenate([[base], close[:-1]])
        opens = prev * np.exp(0.003 * r.standard_normal(n))
        highs = np.maximum(opens, close) * (1 + np.abs(0.006 * r.standard_normal(n)))
        lows = np.minimum(opens, close) * (1 - np.abs(0.006 * r.standard_normal(n)))
        vol = 1e6 * np.exp(0.3 * r.standard_normal(n))
        return pd.DataFrame({"date": dates, "open": opens, "high": highs, "low": lows, "close": close, "volume": vol})

    out[U.MARKET] = bars(mkt, 300.0, 1)
    for k, (s, r) in enumerate(sec.items(), 2):
        out[s] = bars(mkt + r, 80.0, k)
    for k, s in enumerate(STOCKS, 10):
        r = np.random.default_rng(seed * 1000 + k)
        idio = 0.014 * r.standard_normal(n)
        idio[1:] -= reversion * idio[:-1]                  # the edge: part of yesterday's own move comes back
        beta = 0.8 + 0.4 * r.random()
        out[s] = bars(beta * mkt + sec[SECTORS[s]] + idio, 50 + 200 * r.random(), k)
    return out


def universe() -> U.Universe:
    return U.custom("test-10", list(STOCKS), context=(U.MARKET, "XLK", "XLF", "XLE"))


def config(start: date, end: date, **over) -> ReplayConfig:
    base = ReplayConfig(kind="development", start=start, end=end, universe="test-10", horizons=(1, 5), retrain="weekly",
                        min_train_sessions=120, train_window_sessions=400, families=("drift", "engines30", "ridge", "gbm"),
                        promotion=PromotionRule(validation_sessions=40, min_validation_dates=15, min_validation_rows=150,
                                                trade_min_validation_rows=20, trade_min_train_rows=120),
                        trade=TradeSpec(setups=("pullback", "drop", "gap_down")))
    return base.with_(**over)


def exchange_sessions(first: str, last: str) -> pd.DatetimeIndex:
    """Weekdays before 2025 (marketdays.py lists holidays from 2025), the exchange calendar from 2025 on."""
    from tradeassist import marketdays
    early = [d for d in pd.bdate_range(first, min(pd.Timestamp(last), pd.Timestamp("2024-12-31")))]
    late = [pd.Timestamp(d) for d in marketdays.trading_days_between(max(pd.Timestamp(first), pd.Timestamp("2025-01-02")).date(),
                                                                      pd.Timestamp(last).date())]
    return pd.DatetimeIndex(early + late)


def calendar_market(first: str = "2022-01-03", last: str = "2026-03-31", seed: int = 11, reversion: float = 0.4) -> dict[str, pd.DataFrame]:
    """fx.market on real session dates, so a dataset passes the calendar checks and reaches the held-out year."""
    dates = exchange_sessions(first, last)
    out = market(n=len(dates), start=first, seed=seed, reversion=reversion)
    for df in out.values():
        df["date"] = dates
    return out
