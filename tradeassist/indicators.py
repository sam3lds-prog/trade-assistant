"""Technical indicators, implemented to the standard definitions.

RSI, ATR and ADX use Wilder's smoothing (seeded with a simple average, then
prev + (x - prev) / n), which is what broker charts show. The earlier version of
this project used rolling means, which gives different numbers.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

TRADING_DAYS = 252


def sma(series: pd.Series, window: int) -> pd.Series:
    return series.rolling(window, min_periods=window).mean()


def ema(series: pd.Series, span: int) -> pd.Series:
    """Exponential moving average, seeded with the first value (adjust=False)."""
    return series.ewm(span=span, adjust=False).mean()


def wilder_smooth(series: pd.Series, period: int) -> pd.Series:
    """Wilder's moving average. The first output is the simple mean of the first
    `period` non-missing values; each later value is prev + (x - prev) / period."""
    values = series.to_numpy(dtype=float)
    out = np.full(len(values), np.nan)
    valid = np.flatnonzero(~np.isnan(values))
    if len(valid) < period:
        return pd.Series(out, index=series.index)
    start = valid[0]
    window = values[start:start + period]
    if np.isnan(window).any():
        # Gaps inside the seed window: fall back to the first clean run of `period` values.
        for s in valid:
            window = values[s:s + period]
            if len(window) == period and not np.isnan(window).any():
                start = s
                break
        else:
            return pd.Series(out, index=series.index)
    seed_idx = start + period - 1
    prev = float(np.mean(values[start:start + period]))
    out[seed_idx] = prev
    for i in range(seed_idx + 1, len(values)):
        x = values[i]
        if not np.isnan(x):
            prev = prev + (x - prev) / period
        out[i] = prev
    return pd.Series(out, index=series.index)


def macd(close: pd.Series, fast: int = 12, slow: int = 26, signal: int = 9) -> tuple[pd.Series, pd.Series, pd.Series]:
    """MACD line, signal line and histogram. Values before `slow` bars are unreliable
    and are masked out so that a crossover cannot fire on warm-up noise."""
    line = ema(close, fast) - ema(close, slow)
    sig = ema(line, signal)
    hist = line - sig
    warm = slow + signal - 1
    mask = np.arange(len(close)) < warm
    line = line.mask(mask)
    sig = sig.mask(mask)
    hist = hist.mask(mask)
    return line, sig, hist


def rsi(close: pd.Series, period: int = 14) -> pd.Series:
    delta = close.diff()
    gain = delta.clip(lower=0.0)
    loss = (-delta).clip(lower=0.0)
    avg_gain = wilder_smooth(gain, period)
    avg_loss = wilder_smooth(loss, period)
    total = avg_gain + avg_loss
    out = 100.0 * avg_gain / total
    out = out.where(total > 0, 50.0)          # no movement at all: neutral
    out = out.where(~avg_gain.isna(), np.nan)  # keep warm-up as missing
    return out


def true_range(high: pd.Series, low: pd.Series, close: pd.Series) -> pd.Series:
    prev_close = close.shift(1)
    ranges = pd.concat([high - low, (high - prev_close).abs(), (low - prev_close).abs()], axis=1)
    tr = ranges.max(axis=1)
    tr.iloc[0] = np.nan  # no previous close on the first bar
    return tr


def atr(high: pd.Series, low: pd.Series, close: pd.Series, period: int = 14) -> pd.Series:
    return wilder_smooth(true_range(high, low, close), period)


def adx(high: pd.Series, low: pd.Series, close: pd.Series, period: int = 14) -> tuple[pd.Series, pd.Series, pd.Series]:
    """Average Directional Index with +DI and -DI (Wilder)."""
    up = high.diff()
    down = -low.diff()
    plus_dm = pd.Series(np.where((up > down) & (up > 0), up, 0.0), index=high.index)
    minus_dm = pd.Series(np.where((down > up) & (down > 0), down, 0.0), index=high.index)
    plus_dm.iloc[0] = np.nan
    minus_dm.iloc[0] = np.nan
    atr_ = atr(high, low, close, period)
    plus_di = 100.0 * wilder_smooth(plus_dm, period) / atr_
    minus_di = 100.0 * wilder_smooth(minus_dm, period) / atr_
    di_sum = plus_di + minus_di
    dx = (100.0 * (plus_di - minus_di).abs() / di_sum).where(di_sum > 0, 0.0)
    dx = dx.where(~di_sum.isna(), np.nan)
    return wilder_smooth(dx, period), plus_di, minus_di


def realized_vol(close: pd.Series, window: int = 20) -> pd.Series:
    """Annualised standard deviation of daily log returns over `window` trading days."""
    log_ret = np.log(close / close.shift(1))
    return log_ret.rolling(window, min_periods=window).std(ddof=1) * np.sqrt(TRADING_DAYS)


def range_position(high: pd.Series, low: pd.Series, close: pd.Series, lookback: int = TRADING_DAYS) -> pd.Series:
    """Where the close sits in the trailing high-low range, 0 (at the low) to 100 (at the high)."""
    hi = high.rolling(lookback, min_periods=lookback).max()
    lo = low.rolling(lookback, min_periods=lookback).min()
    width = hi - lo
    return (100.0 * (close - lo) / width).where(width > 0, 50.0).where(~width.isna(), np.nan)


def bollinger(close: pd.Series, window: int = 20, n_std: float = 2.0) -> tuple[pd.Series, pd.Series, pd.Series]:
    mid = sma(close, window)
    sd = close.rolling(window, min_periods=window).std(ddof=0)
    return mid - n_std * sd, mid, mid + n_std * sd


def enrich(bars: pd.DataFrame) -> pd.DataFrame:
    """Add the indicator columns used by the pattern lab, scorecard and scanner.

    `bars` needs columns date, open, high, low, close, volume, sorted by date. Every
    column at row t uses data up to and including row t only.
    """
    df = bars.copy().reset_index(drop=True)
    close, high, low = df["close"], df["high"], df["low"]
    df["macd"], df["macd_signal"], df["macd_hist"] = macd(close)
    df["rsi"] = rsi(close)
    df["adx"], df["plus_di"], df["minus_di"] = adx(high, low, close)
    df["atr"] = atr(high, low, close)
    for window in (10, 20, 50, 200):
        df[f"sma{window}"] = sma(close, window)
    df["hv20"] = realized_vol(close, 20)
    df["pos52"] = range_position(high, low, close, TRADING_DAYS)
    df["bb_low"], df["bb_mid"], df["bb_high"] = bollinger(close)
    avg_volume = df["volume"].rolling(20, min_periods=20).mean().shift(1)
    df["vol_ratio"] = (df["volume"] / avg_volume).where(avg_volume > 0)
    df["ema20"] = ema(close, 20)
    from .structure import structure_series      # swing structure: also uses only data up to each bar
    df = pd.concat([df, structure_series(df)], axis=1)
    return df
