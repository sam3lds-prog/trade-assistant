"""Event-conditioned historical analogs (3.0 Research): when did this stock, or a comparable one, last look like this,
and what followed?

similar.py matches the *shape* of a chart window. This module matches the *situation*: the kind of event that
just happened (a gap, a drop or surge of several daily ranges, a volume spike, a pullback in an uptrend, a
breakout, an earnings day) and the conditions around it (how stretched the stock was, its trend, its volatility,
the market's regime). It searches the stock's own history and its peers'.

Guards, each tested:

- **Point in time.** Every feature is computed from data up to and including its own day (rolling windows only);
  a candidate counts only when its whole forward window ended before the study's as-of date.
- **No overlap.** The stock's own recent days, whose forward window would include today, are never candidates.
- **Independence.** Two analogs whose forward windows overlap on the same stock, or that happened on the same day
  on different stocks (one market event seen through several names), are not independent observations: the
  dependent ones are listed, marked, weighted half, and the frequencies use the independent count.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any, Sequence

import numpy as np
import pandas as pd

ENGINE_VERSION = "analogs-3.0"
HORIZONS = (1, 3, 5, 10, 20)
EVENT_LOOKBACK = 3            # an event in the last three sessions defines the current situation

EVENT_LABELS = {
    "gap_up": "a gap up of at least a daily range", "gap_down": "a gap down of at least a daily range",
    "surge": "a rise of at least two daily ranges", "drop": "a fall of at least two daily ranges",
    "volume_spike": "volume at least 2.5 times its 20-day average", "pullback": "a pullback in an uptrend",
    "breakout": "a close at a 60-day high on heavy volume", "earnings": "an earnings release", "setup": "a similar setup (no event)",
}
FEATURES = ("move_atr", "gap_atr", "log_vol_ratio", "rsi", "dist50", "ret20", "rvol_rank", "drawdown20", "mkt_ret20")
WEIGHTS = np.array([1.2, 0.8, 0.6, 1.0, 0.8, 0.8, 0.6, 0.6, 0.7])
LABELS = {"move_atr": "day's move in daily ranges", "gap_atr": "opening gap in daily ranges", "log_vol_ratio": "volume against its average",
          "rsi": "RSI(14)", "dist50": "distance from the 50-day average", "ret20": "20-day return", "rvol_rank": "volatility against its year",
          "drawdown20": "drop from the 20-day high", "mkt_ret20": "the market's 20-day return"}


def _rsi(close: pd.Series, n: int = 14) -> pd.Series:
    d = close.diff()
    up = d.clip(lower=0).ewm(alpha=1 / n, adjust=False, min_periods=n).mean()
    dn = (-d.clip(upper=0)).ewm(alpha=1 / n, adjust=False, min_periods=n).mean()
    rs = up / dn.replace(0, np.nan)
    return 100 - 100 / (1 + rs)


def features(df: pd.DataFrame, market: pd.DataFrame | None = None) -> pd.DataFrame:
    """Causal features per day: each row uses only that day and the days before it."""
    d = df.reset_index(drop=True).copy()
    d["date"] = pd.to_datetime(d["date"]).dt.normalize()
    c, o, h, low, v = d["close"].astype(float), d["open"].astype(float), d["high"].astype(float), d["low"].astype(float), d["volume"].astype(float)
    prev = c.shift(1)
    tr = pd.concat([h - low, (h - prev).abs(), (low - prev).abs()], axis=1).max(axis=1)
    atr_pct = (tr.rolling(14, min_periods=10).mean() / c)
    base = atr_pct.shift(1)
    out = pd.DataFrame({"date": d["date"], "close": c})
    out["ret1"] = c / prev - 1
    out["atr_pct"] = atr_pct
    out["move_atr"] = out["ret1"] / base
    out["gap_atr"] = (o / prev - 1) / base
    avg_vol = v.rolling(20, min_periods=10).mean().shift(1)
    out["vol_ratio"] = v / avg_vol
    out["log_vol_ratio"] = np.log(out["vol_ratio"].clip(lower=0.05))
    out["rsi"] = _rsi(c) / 100.0
    out["dist50"] = c / c.rolling(50, min_periods=30).mean() - 1
    out["ret20"] = c / c.shift(20) - 1
    rvol = out["ret1"].rolling(20, min_periods=15).std()
    lo, hi = rvol.rolling(250, min_periods=60).min(), rvol.rolling(250, min_periods=60).max()
    out["rvol"] = rvol
    out["rvol_rank"] = ((rvol - lo) / (hi - lo)).clip(0, 1)
    out["drawdown20"] = c / c.rolling(20, min_periods=10).max() - 1
    out["high60_prev"] = c.rolling(60, min_periods=40).max().shift(1)
    out["above200"] = c > c.rolling(200, min_periods=120).mean()
    if market is not None and len(market):
        m = market.reset_index(drop=True)
        m = pd.DataFrame({"date": pd.to_datetime(m["date"]).dt.normalize(), "mc": m["close"].astype(float)})
        m["mkt_ret20"] = m["mc"] / m["mc"].shift(20) - 1
        m["mkt_above200"] = m["mc"] > m["mc"].rolling(200, min_periods=120).mean()
        m["mkt_rvol"] = (m["mc"] / m["mc"].shift(1) - 1).rolling(20, min_periods=15).std()
        m["mkt_rvol_med"] = m["mkt_rvol"].rolling(250, min_periods=60).median()
        out = out.merge(m[["date", "mkt_ret20", "mkt_above200", "mkt_rvol", "mkt_rvol_med"]], on="date", how="left")
    else:
        out["mkt_ret20"], out["mkt_above200"], out["mkt_rvol"], out["mkt_rvol_med"] = np.nan, np.nan, np.nan, np.nan
    return out


def regime_of(row: pd.Series) -> str:
    """The market's regime on a day: trend (the market above or below its 200-day average) and volatility (its
    20-day volatility above or below its year's median)."""
    above, rv, med = row.get("mkt_above200"), row.get("mkt_rvol"), row.get("mkt_rvol_med")
    if above is None or (isinstance(above, float) and math.isnan(above)):
        return "unknown"
    trend = "uptrend" if bool(above) else "downtrend"
    calm = "calm" if (rv is not None and med is not None and not (isinstance(rv, float) and math.isnan(rv)) and rv <= med) else "volatile"
    return f"{trend}, {calm}"


def detect(f: pd.DataFrame, earnings: Sequence[pd.Timestamp] = ()) -> pd.DataFrame:
    """The events on each day, as boolean columns (and the day's direction for each)."""
    ev = pd.DataFrame(index=f.index)
    ev["gap_up"] = f["gap_atr"] >= 1.0
    ev["gap_down"] = f["gap_atr"] <= -1.0
    ev["surge"] = f["move_atr"] >= 2.0
    ev["drop"] = f["move_atr"] <= -2.0
    ev["volume_spike"] = f["vol_ratio"] >= 2.5
    ev["pullback"] = (f["drawdown20"] <= -2.5 * f["atr_pct"]) & (f["dist50"] > 0) & f["above200"].fillna(False).astype(bool)
    ev["breakout"] = (f["close"] >= f["high60_prev"]) & (f["vol_ratio"] >= 1.5)
    ev["earnings"] = f["date"].isin(pd.to_datetime(list(earnings)).normalize()) if len(earnings) else False
    return ev.fillna(False).astype(bool)


@dataclass
class Series:
    symbol: str
    feats: pd.DataFrame
    events: pd.DataFrame
    own: bool


def prepare(symbol: str, bars: pd.DataFrame, market: pd.DataFrame | None, own: bool,
            earnings: Sequence[pd.Timestamp] = ()) -> Series:
    f = features(bars, market)
    return Series(symbol=symbol, feats=f, events=detect(f, earnings), own=own)


def current_situation(s: Series, as_of: int | None = None) -> dict[str, Any]:
    """The events of the last few sessions (newest first) and today's feature row."""
    i = len(s.feats) - 1 if as_of is None else as_of
    kinds: list[str] = []
    for j in range(i, max(-1, i - EVENT_LOOKBACK), -1):
        for k in ("earnings", "gap_down", "gap_up", "drop", "surge", "breakout", "pullback", "volume_spike"):
            if bool(s.events.at[j, k]) and k not in kinds:
                kinds.append(k)
    row = s.feats.iloc[i]
    return {"index": i, "date": row["date"], "kinds": kinds or ["setup"], "row": row}


def _vector(rows: pd.DataFrame) -> np.ndarray:
    x = rows[list(FEATURES)].to_numpy(dtype=float)
    return x


def search(target: Series, pool: Sequence[Series], *, horizon: int = 5, kind: str | None = None, as_of: int | None = None,
           limit: int = 25, min_similarity: float = 0.05) -> dict[str, Any]:
    """Analogs of `target`'s situation on `as_of` (its last day by default) among `pool` (target included)."""
    cur = current_situation(target, as_of)
    kind = kind or cur["kinds"][0]
    as_of_date = cur["date"]
    now = cur["row"]
    max_h = max(HORIZONS + (horizon,))
    cands: list[tuple[Series, int]] = []
    for s in pool:
        f = s.feats
        n = len(f)
        last_ok = int(np.searchsorted(f["date"].to_numpy(), np.datetime64(as_of_date), side="left")) - 1
        # the whole forward window must have ended before the as-of day
        upto = last_ok - horizon
        if s is target:
            upto = min(upto, cur["index"] - horizon - 1)
        if upto < 60:
            continue
        idx = np.arange(60, min(upto, n - 1) + 1)
        if kind == "setup":
            mask = np.ones(len(idx), dtype=bool)
            idx = idx[::3]                                 # every third day: fewer near-duplicates
            mask = mask[: len(idx)]
        else:
            mask = s.events[kind].to_numpy()[idx]
        for i in idx[mask]:
            cands.append((s, int(i)))
    out: dict[str, Any] = {"engine": ENGINE_VERSION, "symbol": target.symbol, "as_of": str(pd.Timestamp(as_of_date).date()),
                           "kind": kind, "kind_label": EVENT_LABELS.get(kind, kind), "situation": cur["kinds"], "horizon": horizon,
                           "regime_now": regime_of(now), "analogs": [], "summary": {}, "searched": len(cands), "notes": []}
    if not cands:
        out["notes"].append("No earlier case of this situation, on this stock or its peers, has a finished forward window.")
        return out
    rows = pd.DataFrame([s.feats.iloc[i] for s, i in cands]).reset_index(drop=True)
    x = _vector(rows)
    t = np.array([float(now.get(k)) if now.get(k) is not None else np.nan for k in FEATURES])
    scale = np.nanstd(np.vstack([x, t[None, :]]), axis=0)
    scale[~np.isfinite(scale) | (scale <= 1e-9)] = 1.0
    diff = (x - t[None, :]) / scale[None, :]
    diff[~np.isfinite(diff)] = 2.0                         # a feature unknown on either side counts as far apart
    dist = np.sqrt(((diff ** 2) * WEIGHTS[None, :]).sum(axis=1) / WEIGHTS.sum())
    sim = np.exp(-0.5 * dist ** 2)
    sim *= np.array([1.0 if s.own else 0.8 for s, _ in cands])   # the stock's own history counts a little more
    order = np.argsort(-sim)
    chosen: list[dict[str, Any]] = []
    windows: dict[str, list[int]] = {}
    days_taken: set[str] = set()
    for k in order:
        if len(chosen) >= limit or sim[k] < min_similarity:
            break
        s, i = cands[k]
        f = s.feats
        c0 = float(f.at[i, "close"])
        fwd = {h: float(f.at[i + h, "close"] / c0 - 1) for h in HORIZONS if i + h < len(f)}
        if horizon not in fwd and i + horizon < len(f):
            fwd[horizon] = float(f.at[i + horizon, "close"] / c0 - 1)
        path = [float(f.at[i + j, "close"] / c0 - 1) for j in range(1, max_h + 1) if i + j < len(f)]
        day = str(pd.Timestamp(f.at[i, "date"]).date())
        overlaps = any(abs(i - j) <= horizon for j in windows.get(s.symbol, []))
        same_day = day in days_taken
        independent = not overlaps and not same_day
        windows.setdefault(s.symbol, []).append(i)
        days_taken.add(day)
        matched = []
        for col in ("move_atr", "rsi", "dist50", "ret20"):
            a, b = f.at[i, col], now.get(col)
            if a is not None and b is not None and np.isfinite(a) and np.isfinite(b):
                fmt = (lambda z: f"{100 * z:.0f}") if col == "rsi" else (lambda z: f"{z:+.1f}") if col == "move_atr" else (lambda z: f"{100 * z:+.1f}%")
                matched.append(f"{LABELS[col]} {fmt(a)} (now {fmt(b)})")
        chosen.append({"symbol": s.symbol, "date": day, "source": "own" if s.own else "peer", "kind": kind,
                       "similarity": round(float(sim[k]), 4), "independent": independent, "weight": float(sim[k]) * (1.0 if independent else 0.5),
                       "regime": regime_of(f.iloc[i]), "forward_pct": {str(h): round(100 * v, 3) for h, v in sorted(fwd.items())},
                       "path_pct": [round(100 * p, 3) for p in path], "criteria": matched})
    out["analogs"] = chosen
    out["summary"] = summarize(chosen, horizon)
    if out["summary"].get("independent", 0) < 8:
        out["notes"].append("Fewer than eight independent cases: treat the frequencies as a sketch, not a record.")
    return out


def wilson(k: float, n: float, z: float = 1.96) -> tuple[float | None, float | None]:
    if n <= 0:
        return None, None
    p = k / n
    den = 1 + z * z / n
    centre = (p + z * z / (2 * n)) / den
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / den
    return max(0.0, centre - half), min(1.0, centre + half)


def weighted_quantile(values: np.ndarray, weights: np.ndarray, q: float) -> float:
    from .riskmath import weighted_quantile as wq
    return wq(values, weights, q)


def summarize(analogs: list[dict[str, Any]], horizon: int) -> dict[str, Any]:
    out: dict[str, Any] = {"cases": len(analogs), "independent": sum(1 for a in analogs if a["independent"]),
                           "own": sum(1 for a in analogs if a["source"] == "own"), "by_horizon": {}}
    for h in sorted(set(HORIZONS) | {horizon}):
        vals = [(a["forward_pct"].get(str(h)), a["weight"], a["independent"]) for a in analogs if a["forward_pct"].get(str(h)) is not None]
        if not vals:
            continue
        v = np.array([x[0] for x in vals], dtype=float)
        w = np.array([x[1] for x in vals], dtype=float)
        ind = [x for x in vals if x[2]]
        n_ind = len(ind)
        ups = sum(1 for x in ind if x[0] > 0)
        lo, hi = wilson(ups, n_ind)
        out["by_horizon"][str(h)] = {"cases": len(vals), "independent": n_ind,
                                     "median_pct": weighted_quantile(v, w, 0.5), "p10_pct": weighted_quantile(v, w, 0.1),
                                     "p90_pct": weighted_quantile(v, w, 0.9), "mean_pct": float((v * w).sum() / w.sum()),
                                     "up_frequency": ups / n_ind if n_ind else None, "up_low": lo, "up_high": hi}
    return out


def peer_comparison(analogs: list[dict[str, Any]], horizon: int) -> dict[str, Any]:
    """How the stock's own cases and its peers' compare at the horizon."""
    out = {}
    for src in ("own", "peer"):
        sub = [a for a in analogs if a["source"] == src]
        if sub:
            s = summarize(sub, horizon)["by_horizon"].get(str(horizon))
            out[src] = s
    return out


__all__ = ["ENGINE_VERSION", "HORIZONS", "EVENT_LABELS", "features", "detect", "prepare", "current_situation", "search", "summarize",
           "regime_of", "wilson", "peer_comparison", "Series"]
