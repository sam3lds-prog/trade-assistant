"""Pattern detectors on daily bars.

Each detector returns a boolean Series that is True on the bar where the pattern
completes, using only that bar and earlier ones. `direction` is the hypothesis
being tested (+1 expects prices to rise afterwards, -1 to fall); the event study
decides whether the data backs it.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

import numpy as np
import pandas as pd

from .indicators import TRADING_DAYS


@dataclass(frozen=True)
class Pattern:
    key: str
    label: str
    direction: int
    description: str
    detect: Callable[[pd.DataFrame], pd.Series]


def _crosses_above(a: pd.Series, b: pd.Series | float) -> pd.Series:
    b_prev = b.shift(1) if isinstance(b, pd.Series) else b
    return ((a > b) & (a.shift(1) <= b_prev)).fillna(False)


def _crosses_below(a: pd.Series, b: pd.Series | float) -> pd.Series:
    b_prev = b.shift(1) if isinstance(b, pd.Series) else b
    return ((a < b) & (a.shift(1) >= b_prev)).fillna(False)


def _body(df: pd.DataFrame) -> pd.Series:
    return (df["close"] - df["open"]).abs()


def _hammer(df: pd.DataFrame) -> pd.Series:
    body = _body(df)
    rng = df["high"] - df["low"]
    lower = df[["open", "close"]].min(axis=1) - df["low"]
    upper = df["high"] - df[["open", "close"]].max(axis=1)
    after_decline = df["close"].shift(1) < df["sma10"].shift(1)
    return ((rng > 0) & (lower >= 2 * body) & (upper <= 0.5 * body.clip(lower=0.1 * rng))
            & (body >= 0.05 * rng) & after_decline).fillna(False)


def _shooting_star(df: pd.DataFrame) -> pd.Series:
    body = _body(df)
    rng = df["high"] - df["low"]
    lower = df[["open", "close"]].min(axis=1) - df["low"]
    upper = df["high"] - df[["open", "close"]].max(axis=1)
    after_advance = df["close"].shift(1) > df["sma10"].shift(1)
    return ((rng > 0) & (upper >= 2 * body) & (lower <= 0.5 * body.clip(lower=0.1 * rng))
            & (body >= 0.05 * rng) & after_advance).fillna(False)


def _bullish_engulfing(df: pd.DataFrame) -> pd.Series:
    prev_open, prev_close = df["open"].shift(1), df["close"].shift(1)
    return ((prev_close < prev_open) & (df["close"] > df["open"]) & (df["open"] <= prev_close)
            & (df["close"] >= prev_open) & (prev_close < df["sma10"].shift(1))).fillna(False)


def _bearish_engulfing(df: pd.DataFrame) -> pd.Series:
    prev_open, prev_close = df["open"].shift(1), df["close"].shift(1)
    return ((prev_close > prev_open) & (df["close"] < df["open"]) & (df["open"] >= prev_close)
            & (df["close"] <= prev_open) & (prev_close > df["sma10"].shift(1))).fillna(False)


def _new_high(df: pd.DataFrame) -> pd.Series:
    prior = df["high"].rolling(TRADING_DAYS, min_periods=TRADING_DAYS).max().shift(1)
    return ((df["close"] > prior) & (df["close"].shift(1) <= prior.shift(1))).fillna(False)


def _new_low(df: pd.DataFrame) -> pd.Series:
    prior = df["low"].rolling(TRADING_DAYS, min_periods=TRADING_DAYS).min().shift(1)
    return ((df["close"] < prior) & (df["close"].shift(1) >= prior.shift(1))).fillna(False)


PATTERNS: dict[str, Pattern] = {p.key: p for p in [
    Pattern("macd_bull_cross", "MACD crosses above its signal line", +1,
            "The 12/26 MACD line moves from below to above its 9-day signal line.",
            lambda df: _crosses_above(df["macd"], df["macd_signal"])),
    Pattern("macd_bear_cross", "MACD crosses below its signal line", -1,
            "The MACD line moves from above to below its signal line.",
            lambda df: _crosses_below(df["macd"], df["macd_signal"])),
    Pattern("rsi_leaves_oversold", "RSI climbs back above 30", +1,
            "14-day RSI closes above 30 after being at or below it: the classic oversold bounce.",
            lambda df: _crosses_above(df["rsi"], 30.0)),
    Pattern("rsi_leaves_overbought", "RSI drops back below 70", -1,
            "14-day RSI closes below 70 after being at or above it.",
            lambda df: _crosses_below(df["rsi"], 70.0)),
    Pattern("enters_low_zone", "Price enters the bottom fifth of its 52-week range", +1,
            "The mean-reversion idea behind the original scanner: buy weakness near the yearly low.",
            lambda df: _crosses_below(df["pos52"], 20.0)),
    Pattern("enters_high_zone", "Price enters the top fifth of its 52-week range", -1,
            "The mean-reversion idea in reverse: fade strength near the yearly high.",
            lambda df: _crosses_above(df["pos52"], 80.0)),
    Pattern("new_52w_high", "Closes at a new 52-week high", +1,
            "A breakout: the close exceeds every high of the prior 252 trading days.", _new_high),
    Pattern("new_52w_low", "Closes at a new 52-week low", -1,
            "A breakdown: the close is under every low of the prior 252 trading days.", _new_low),
    Pattern("golden_cross", "50-day average crosses above the 200-day", +1,
            "The 50-day simple average moves above the 200-day.",
            lambda df: _crosses_above(df["sma50"], df["sma200"])),
    Pattern("death_cross", "50-day average crosses below the 200-day", -1,
            "The 50-day simple average moves below the 200-day.",
            lambda df: _crosses_below(df["sma50"], df["sma200"])),
    Pattern("hammer", "Hammer candle after a decline", +1,
            "A long lower shadow (at least twice the body) with little upper shadow, after closing under "
            "the 10-day average.", _hammer),
    Pattern("shooting_star", "Shooting star candle after an advance", -1,
            "A long upper shadow with little lower shadow, after closing above the 10-day average.",
            _shooting_star),
    Pattern("bullish_engulfing", "Bullish engulfing after a decline", +1,
            "An up day whose body covers the previous down day's body, after closing under the 10-day average.",
            _bullish_engulfing),
    Pattern("bearish_engulfing", "Bearish engulfing after an advance", -1,
            "A down day whose body covers the previous up day's body, after closing above the 10-day average.",
            _bearish_engulfing),
    Pattern("bollinger_low_reclaim", "Closes back above the lower Bollinger band", +1,
            "The close returns inside the 20-day, 2-standard-deviation band after closing below it.",
            lambda df: _crosses_above(df["close"], df["bb_low"])),
    Pattern("bollinger_high_reject", "Closes back below the upper Bollinger band", -1,
            "The close returns inside the band after closing above it.",
            lambda df: _crosses_below(df["close"], df["bb_high"])),
    # Swing structure (see structure.py). Each is dated on the day it became known, never on the
    # day of the swing itself, which is only recognised as a swing some days later.
    Pattern("structure_up", "Swings turn to higher highs and higher lows", +1,
            "The latest swing high is above the one before and the latest swing low is above the one "
            "before. Swings are moves of at least two average true ranges.",
            lambda df: df["struct_up_start"].astype(bool)),
    Pattern("structure_down", "Swings turn to lower highs and lower lows", -1,
            "The latest swing high is below the one before and the latest swing low is below the one before.",
            lambda df: df["struct_down_start"].astype(bool)),
    Pattern("structure_up_break", "Uptrend breaks: closes below the last higher low", -1,
            "During higher highs and higher lows, a close below the most recent higher low.",
            lambda df: df["struct_up_break"].astype(bool)),
    Pattern("structure_down_break", "Downtrend breaks: closes above the last lower high", +1,
            "During lower highs and lower lows, a close above the most recent lower high.",
            lambda df: df["struct_down_break"].astype(bool)),
]}


def detect_all(enriched: pd.DataFrame) -> pd.DataFrame:
    """One boolean column per pattern, aligned to `enriched`."""
    return pd.DataFrame({key: p.detect(enriched).astype(bool).to_numpy() for key, p in PATTERNS.items()},
                        index=enriched.index)


def recent_events(enriched: pd.DataFrame, lookback: int = 5) -> list[dict]:
    """Patterns that completed within the last `lookback` bars, newest first."""
    events = detect_all(enriched).tail(lookback)
    dates = enriched["date"].tail(lookback)
    out = []
    for key in events.columns:
        hits = np.flatnonzero(events[key].to_numpy())
        if len(hits):
            last = hits[-1]
            row = len(enriched) - len(events) + int(last)
            finished = bool(enriched["bar_complete"].iloc[row]) if "bar_complete" in enriched.columns else True
            out.append({"key": key, "label": PATTERNS[key].label, "direction": PATTERNS[key].direction,
                        "date": dates.iloc[last].strftime("%Y-%m-%d"),
                        "bars_ago": len(events) - 1 - int(last),
                        # A pattern on today's unfinished bar can still disappear by the close.
                        "status": "CONFIRMED" if finished else "PROVISIONAL"})
    out.sort(key=lambda e: e["bars_ago"])
    return out
