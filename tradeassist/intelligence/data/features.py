"""What a model may know about a stock after a session's close (4.0).

Every feature is causal: computed from that session's bar and the bars before it, with rolling windows only. The
replay therefore computes the whole table once and reads row T on session T, which is the same as recomputing from
data that stops at T; tests/test_v4_replay.py proves the equivalence on truncated datasets for every feature, and
a feature that broke it would fail there.

Built on analogs.features (3.0's causal set: moves in daily ranges, gaps, volume, RSI, distance from the 50-day
average, volatility and its rank, the market's trend and volatility), adding longer returns, the 200-day distance,
the market's one- and five-day returns, the stock's strength against its sector fund, and its own trailing drift.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd

from ... import analogs
from .dataset import Dataset
from .universe import MARKET, MIN_HISTORY

FEATURE_VERSION = "features-4.0"
FEATURES = ("ret1", "ret5", "ret20", "ret60", "move_atr", "gap_atr", "log_vol_ratio", "rsi", "dist50", "dist200",
            "rvol", "rvol_rank", "drawdown20", "atr_pct", "mkt_ret1", "mkt_ret5", "mkt_ret20", "mkt_rvol",
            "mkt_above200", "sec_rel5", "sec_rel20", "drift250")


def symbol_features(bars: pd.DataFrame, market: pd.DataFrame | None, sector: pd.DataFrame | None) -> pd.DataFrame:
    """One row per bar of `bars`, indexed by date, each from that day and earlier only."""
    f = analogs.features(bars, market)
    c = f["close"]
    out = pd.DataFrame(index=pd.DatetimeIndex(f["date"], name="date"))
    for k in ("close", "ret1", "ret20", "move_atr", "gap_atr", "log_vol_ratio", "rsi", "dist50", "rvol", "rvol_rank",
              "drawdown20", "atr_pct", "mkt_ret20", "mkt_rvol", "above200", "mkt_above200", "mkt_rvol_med"):
        out[k] = f[k].to_numpy()
    out["ret5"] = (c / c.shift(5) - 1).to_numpy()
    out["ret60"] = (c / c.shift(60) - 1).to_numpy()
    out["dist200"] = (c / c.rolling(200, min_periods=120).mean() - 1).to_numpy()
    out["drift250"] = f["ret1"].rolling(250, min_periods=60).mean().to_numpy()
    out["bars_so_far"] = np.arange(1, len(f) + 1)
    if market is not None and len(market):
        m = market.set_index(pd.to_datetime(market["date"]).dt.normalize())["close"].astype(float)
        mret1, mret5 = m / m.shift(1) - 1, m / m.shift(5) - 1
        out["mkt_ret1"] = mret1.reindex(out.index).to_numpy()
        out["mkt_ret5"] = mret5.reindex(out.index).to_numpy()
    else:
        out["mkt_ret1"] = np.nan
        out["mkt_ret5"] = np.nan
    if sector is not None and len(sector):
        s = sector.set_index(pd.to_datetime(sector["date"]).dt.normalize())["close"].astype(float)
        out["sec_rel5"] = out["ret5"] - (s / s.shift(5) - 1).reindex(out.index).to_numpy()
        out["sec_rel20"] = out["ret20"] - (s / s.shift(20) - 1).reindex(out.index).to_numpy()
    else:
        out["sec_rel5"] = np.nan
        out["sec_rel20"] = np.nan
    out["mkt_above200"] = out["mkt_above200"].astype(float)
    out["above200"] = out["above200"].astype(float)
    return out


@dataclass
class Panel:
    """Every stock's features on every session, and the prices the labels are read from.

    `features` is indexed by (session, symbol). The price tables (sessions x symbols) are NaN where a stock had no
    bar. `eligible` marks a stock that can be predicted on a session: a bar that day, a year of history, and the
    volatility the bands need."""
    features: pd.DataFrame
    open: pd.DataFrame
    high: pd.DataFrame
    low: pd.DataFrame
    close: pd.DataFrame
    eligible: pd.DataFrame
    regime: pd.Series
    sessions: pd.DatetimeIndex

    def rows(self, session: pd.Timestamp) -> pd.DataFrame:
        """The eligible stocks' feature rows for one session."""
        try:
            block = self.features.xs(session, level="session")
        except KeyError:
            return self.features.iloc[0:0].droplevel("session")
        ok = self.eligible.loc[session]
        return block[block.index.isin(ok[ok].index)]


def build_panel(ds: Dataset) -> Panel:
    sessions = pd.DatetimeIndex([pd.Timestamp(d) for d in ds.calendar.sessions], name="session")
    market = ds.bars.get(MARKET)
    frames, prices = [], {k: {} for k in ("open", "high", "low", "close")}
    for sym in ds.stocks:
        bars = ds.bars[sym]
        sector_sym = ds.universe.sector_of(sym)
        f = symbol_features(bars, market, ds.bars.get(sector_sym) if sector_sym else None)
        f = f.reindex(sessions)
        f.index.name = "session"
        f["symbol"] = sym
        frames.append(f.reset_index())
        b = bars.set_index(pd.to_datetime(bars["date"]).dt.normalize())
        for k in prices:
            prices[k][sym] = b[k].astype(float).reindex(sessions)
    feats = pd.concat(frames, ignore_index=True).set_index(["session", "symbol"]).sort_index()
    tables = {k: pd.DataFrame(v, index=sessions) for k, v in prices.items()}
    have_bar = tables["close"].notna()
    history = feats["bars_so_far"].unstack("symbol").reindex(index=sessions, columns=tables["close"].columns)
    rv = feats["rvol"].unstack("symbol").reindex(index=sessions, columns=tables["close"].columns)
    atr = feats["atr_pct"].unstack("symbol").reindex(index=sessions, columns=tables["close"].columns)
    eligible = have_bar & (history >= MIN_HISTORY) & (rv > 0) & np.isfinite(rv) & (atr > 0) & np.isfinite(atr)
    regime = _regimes(market, sessions)
    return Panel(features=feats, open=tables["open"], high=tables["high"], low=tables["low"], close=tables["close"],
                 eligible=eligible.fillna(False).astype(bool), regime=regime, sessions=sessions)


def _regimes(market: pd.DataFrame | None, sessions: pd.DatetimeIndex) -> pd.Series:
    if market is None or not len(market):
        return pd.Series("unknown", index=sessions)
    f = analogs.features(market, market).set_index("date")
    labels = [analogs.regime_of(row) for _, row in f.iterrows()]
    return pd.Series(labels, index=pd.DatetimeIndex(f.index)).reindex(sessions).fillna("unknown")


def matrix(rows: pd.DataFrame) -> np.ndarray:
    """The model inputs of some feature rows, in FEATURES order (NaN where unknown)."""
    return rows[list(FEATURES)].to_numpy(dtype=float)


__all__ = ["FEATURES", "FEATURE_VERSION", "Panel", "build_panel", "symbol_features", "matrix"]
