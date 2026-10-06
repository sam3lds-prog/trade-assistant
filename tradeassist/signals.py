"""The original scanner's composite score, computed for every day of history.

This reproduces the scoring in the project's `tradier_backend.py` (52-week position,
MACD state, RSI zone, ADX, volume spike, candle) so that the strategy can be
backtested on signals it would really have produced. Two deliberate differences:
RSI and ADX use the standard Wilder definitions, and the volume average excludes
the current day so a spike is measured against prior days only.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from .patterns import PATTERNS


def legacy_scores(enriched: pd.DataFrame) -> pd.DataFrame:
    df = enriched
    pos, macd_line, macd_sig, rsi_, adx_ = df["pos52"], df["macd"], df["macd_signal"], df["rsi"], df["adx"]
    vol_spike = (df["vol_ratio"] > 1.5).fillna(False)
    trending = (adx_ > 25).fillna(False)
    hammer = PATTERNS["hammer"].detect(df)
    star = PATTERNS["shooting_star"].detect(df)

    long_score = pd.Series(0.0, index=df.index)
    long_score += np.where(pos <= 20, 0.3 * (20 - pos) / 20, 0.0)
    long_score += np.where(macd_line > macd_sig, 0.25, 0.0)
    long_score += np.where((rsi_ > 30) & (rsi_ < 50), 0.2, np.where(rsi_ < 30, 0.1, 0.0))
    long_score += np.where(trending, 0.15, 0.0)
    long_score += np.where(vol_spike, 0.1, 0.0)
    long_score += np.where(hammer, 0.1, 0.0)

    short_score = pd.Series(0.0, index=df.index)
    short_score += np.where(pos >= 80, 0.3 * (pos - 80) / 20, 0.0)
    short_score += np.where(macd_line < macd_sig, 0.25, 0.0)
    short_score += np.where(rsi_ > 70, 0.2, np.where((rsi_ > 60) & (rsi_ < 70), 0.1, 0.0))
    short_score += np.where(trending, 0.15, 0.0)
    short_score += np.where(vol_spike, 0.1, 0.0)
    short_score += np.where(star, 0.1, 0.0)

    # No score until every input exists (about a year of bars for the 52-week range).
    ready = pos.notna() & macd_line.notna() & rsi_.notna() & adx_.notna()
    long_score = long_score.where(ready, 0.0).fillna(0.0)
    short_score = short_score.where(ready, 0.0).fillna(0.0)
    return pd.DataFrame({"date": df["date"], "long_score": long_score, "short_score": short_score})


def legacy_signals(enriched: pd.DataFrame, min_score: float = 0.4, confirmation_days: int = 3) -> pd.DataFrame:
    """Daily signal type and score, plus whether it has held for `confirmation_days` in a row."""
    scores = legacy_scores(enriched)
    long_s, short_s = scores["long_score"], scores["short_score"]
    signal_type = np.where((long_s > short_s) & (long_s >= min_score), "LONG",
                           np.where((short_s > long_s) & (short_s >= min_score), "SHORT", "NEUTRAL"))
    scores["signal_type"] = signal_type
    scores["signal_score"] = np.maximum(long_s, short_s)
    # Length of the current run of identical signal types, ending at each row.
    change = scores["signal_type"] != scores["signal_type"].shift(1)
    run = change.groupby(change.cumsum()).cumcount() + 1
    scores["run_length"] = run
    scores["confirmed"] = (scores["signal_type"] != "NEUTRAL") & (run >= max(confirmation_days, 1))
    return scores
