"""Signal history: every past occurrence of a signal, what followed it, and how often the trend
engine's evidence score was followed by a gain.

A signal is a pattern completing (patterns.py) or the trend engine changing state (trend.py).
Each past occurrence is stored in `signal_occurrences` with the trend state, net score and
market regime that day, and what happened next, measured the same way as the pattern lab
(eventstudy.py): entry at the next day's open, exit at the close 5, 10 or 20 trading days
after the signal, so nothing uses information from after the signal. Only finished bars are
used, so an occurrence is never recorded from today's unfinished bar.

Two different things come out of this, and they must not be confused:

- Evidence points (the trend engine's scores) count how much of the evidence leans each way.
  They are not probabilities.
- Historical frequencies: of the past occasions like this one, how many were followed by a
  move the signal's way. These are observed counts with a 95% interval (Wilson), not a
  forecast. A score is called *calibrated* only when, on this data, higher score ranges were
  followed by gains significantly more often than lower ones, in order.

The score history used for calibration leaves out the validated-pattern points, because the
lab's verdicts were found on the same history and would leak it.
"""

from __future__ import annotations

import math
from collections import defaultdict
from datetime import datetime, timezone
from typing import Any, Iterable

import numpy as np
import pandas as pd
from scipy import stats

from . import trend as trend_engine
from .eventstudy import forward_returns
from .patterns import PATTERNS, detect_all
from .versions import SIGNAL_ENGINE_VERSION, TREND_ENGINE_VERSION

HORIZONS = (5, 10, 20)
MAIN_HORIZON = 10
MIN_CASES = 30            # fewer past occurrences than this and the frequency is shown as too few
MFE_DAYS = 20
SCORE_BUCKETS = ((-100.0, -50.0), (-50.0, -25.0), (-25.0, 0.0), (0.0, 25.0), (25.0, 50.0), (50.0, 100.01))
TRANSITION_DIRECTION = {"STRONG_UP": 1, "UP": 1, "UP_WEAKENING": -1, "NEUTRAL": 0, "DOWN_DEVELOPING": -1, "DOWN": -1,
                        "STRONG_DOWN": -1}


def signal_label(key: str) -> str:
    kind, _, name = key.partition(":")
    if kind == "pattern" and name in PATTERNS:
        return PATTERNS[name].label
    if kind == "trend":
        return f"Trend turns to {trend_engine.STATE_WORDS.get(name, name).lower()}"
    return key


def signal_direction(key: str) -> int:
    kind, _, name = key.partition(":")
    if kind == "pattern" and name in PATTERNS:
        return int(PATTERNS[name].direction)
    if kind == "trend":
        return TRANSITION_DIRECTION.get(name, 0)
    return 0


def wilson(wins: int, n: int, z: float = 1.96) -> tuple[float | None, float | None]:
    if n <= 0:
        return None, None
    p = wins / n
    centre = (p + z * z / (2 * n)) / (1 + z * z / n)
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / (1 + z * z / n)
    return max(0.0, centre - half), min(1.0, centre + half)


# --- collecting occurrences -------------------------------------------------------------------

def outcomes(df: pd.DataFrame, spy: pd.DataFrame | None) -> pd.DataFrame:
    """For each day of `df`: returns over each horizon, SPY's over the same days, and the best and
    worst prices reached in the next 20 days, all measured from the next day's open."""
    out = pd.DataFrame(index=df.index)
    entry = df["open"].shift(-1)
    spy_open = spy_close = None
    if spy is not None and not spy.empty:
        s = spy.set_index(pd.to_datetime(spy["date"])).sort_index()
        s = s[~s.index.duplicated(keep="last")]
        dates = pd.to_datetime(df["date"])
        spy_open = s["open"].reindex(dates).to_numpy()
        spy_close = s["close"].reindex(dates).to_numpy()
    for h in HORIZONS:
        out[f"fwd_{h}"] = forward_returns(df, h).to_numpy()
        if spy_open is not None:
            so = pd.Series(spy_open).shift(-1).to_numpy()
            sc = pd.Series(spy_close).shift(-h).to_numpy()
            out[f"spy_{h}"] = sc / so - 1.0
        else:
            out[f"spy_{h}"] = np.nan
    hi = df["high"].to_numpy(dtype=float)
    lo = df["low"].to_numpy(dtype=float)
    n = len(df)
    mfe, mae = np.full(n, np.nan), np.full(n, np.nan)
    e = entry.to_numpy(dtype=float)
    for i in range(n - MFE_DAYS):
        window_hi = hi[i + 1: i + 1 + MFE_DAYS].max()
        window_lo = lo[i + 1: i + 1 + MFE_DAYS].min()
        if np.isfinite(e[i]) and e[i] > 0:
            mfe[i] = window_hi / e[i] - 1.0
            mae[i] = window_lo / e[i] - 1.0
    out["high_20"] = mfe     # best price reached, as a return from entry (long side)
    out["low_20"] = mae      # worst price reached
    return out


def collect(symbol: str, df: pd.DataFrame, spy: pd.DataFrame | None, regimes: pd.Series | None,
            states: pd.DataFrame) -> list[dict[str, Any]]:
    """Every pattern event and trend change on `symbol` (finished bars only), with what followed."""
    if len(df) < 60:
        return []
    out_frame = outcomes(df, spy)
    hits = detect_all(df)
    st = states["trend_state"].to_numpy()
    net = states["net_score"].to_numpy(dtype=float)
    dates = pd.to_datetime(df["date"])
    reg = (regimes.reindex(dates, method="ffill").to_numpy() if regimes is not None
           else np.full(len(df), None, dtype=object))
    rows: list[dict[str, Any]] = []

    def add(i: int, key: str) -> None:
        o = out_frame.iloc[i]
        rows.append({"symbol": symbol, "signal_key": key, "direction": signal_direction(key),
                     "date": dates.iloc[i].strftime("%Y-%m-%d"), "close": float(df["close"].iloc[i]),
                     "trend_state": st[i], "net_score": float(net[i]) if np.isfinite(net[i]) else None,
                     "regime": reg[i] if isinstance(reg[i], str) else None,
                     **{k: (float(o[k]) if np.isfinite(o[k]) else None) for k in out_frame.columns}})

    for key in hits.columns:
        for i in np.flatnonzero(hits[key].to_numpy(dtype=bool)):
            add(int(i), f"pattern:{key}")
    for i in range(1, len(st)):
        if st[i] != st[i - 1]:
            add(i, f"trend:{st[i]}")
    return rows


def score_days(symbol: str, df: pd.DataFrame, states: pd.DataFrame, spy: pd.DataFrame | None,
               regimes: pd.Series | None) -> pd.DataFrame:
    """One row per day with the net score, the regime and what followed: the input to calibration."""
    o = outcomes(df, spy)
    dates = pd.to_datetime(df["date"])
    reg = regimes.reindex(dates, method="ffill").to_numpy() if regimes is not None else np.full(len(df), None)
    return pd.DataFrame({"symbol": symbol, "date": dates.to_numpy(), "net": states["net_score"].to_numpy(dtype=float),
                         "regime": reg, **{f"fwd_{h}": o[f"fwd_{h}"].to_numpy() for h in HORIZONS},
                         **{f"spy_{h}": o[f"spy_{h}"].to_numpy() for h in HORIZONS}})


# --- statistics ------------------------------------------------------------------------------------

def summarize(rows: Iterable[dict[str, Any]], direction: int, horizon: int = MAIN_HORIZON) -> dict[str, Any]:
    """How the past occurrences in `rows` turned out over `horizon` days, the signal's way round."""
    sign = direction if direction != 0 else 1
    fwd, excess, best, worst, blocks = [], [], [], [], set()
    for r in rows:
        f = r.get(f"fwd_{horizon}")
        if f is None or not math.isfinite(f):
            continue
        fwd.append(sign * f)
        spy = r.get(f"spy_{horizon}")
        if spy is not None and math.isfinite(spy):
            excess.append(sign * (f - spy))
        hi, lo = r.get("high_20"), r.get("low_20")
        if hi is not None and lo is not None:
            best.append(hi if sign > 0 else -lo)
            worst.append(lo if sign > 0 else -hi)
        d = datetime.fromisoformat(str(r["date"])[:10]).toordinal()
        blocks.add(d // max(1, round(horizon * 7 / 5)))
    n = len(fwd)
    wins = sum(1 for x in fwd if x > 0)
    lo_, hi_ = wilson(wins, n)
    return {"horizon": horizon, "cases": n, "independent_periods": len(blocks),
            "went_the_signal_way": wins, "frequency": wins / n if n else None, "frequency_low": lo_,
            "frequency_high": hi_, "mean_return": float(np.mean(fwd)) if n else None,
            "median_return": float(np.median(fwd)) if n else None,
            "mean_excess_vs_spy": float(np.mean(excess)) if excess else None,
            "mean_best_20d": float(np.mean(best)) if best else None, "mean_worst_20d": float(np.mean(worst)) if worst else None,
            "enough": n >= MIN_CASES, "direction": direction}


def calibrate(days: pd.DataFrame, horizon: int = MAIN_HORIZON) -> dict[str, Any]:
    """Frequency of a gain after each range of net score; and whether the score is calibrated."""
    col = f"fwd_{horizon}"
    d = days[np.isfinite(days["net"]) & np.isfinite(days[col])]
    buckets = []
    for lo, hi in SCORE_BUCKETS:
        sel = d[(d["net"] >= lo) & (d["net"] < hi)]
        n = len(sel)
        up = int((sel[col] > 0).sum())
        a, b = wilson(up, n)
        spy = sel[f"spy_{horizon}"]
        ex = (sel[col] - spy)[np.isfinite(spy)]
        by_regime = {}
        for g, part in sel.groupby("regime"):
            if isinstance(g, str) and len(part):
                k = int((part[col] > 0).sum())
                by_regime[g] = {"cases": int(len(part)), "rose": k, "frequency": k / len(part)}
        buckets.append({"low": lo, "high": min(hi, 100.0), "cases": n, "rose": up, "frequency": up / n if n else None,
                        "frequency_low": a, "frequency_high": b, "mean_return": float(sel[col].mean()) if n else None,
                        "mean_excess_vs_spy": float(ex.mean()) if len(ex) else None, "by_regime": by_regime})
    usable = [b for b in buckets if b["cases"] >= MIN_CASES]
    ordered = len(usable) >= 3 and all(x["frequency"] <= y["frequency"] + 0.02 for x, y in zip(usable, usable[1:]))
    p_value = None
    if len(usable) >= 2:
        p_value = block_gap_test(d, col, (usable[0]["low"], usable[0]["high"]),
                                 (usable[-1]["low"], usable[-1]["high"] + (0.01 if usable[-1]["high"] >= 100 else 0)),
                                 horizon)
    calibrated = bool(ordered and p_value is not None and p_value < 0.05)
    if calibrated:
        verdict = ("On this data, higher net scores have been followed by gains more often, in order, and the gap "
                   "between the lowest and highest ranges is unlikely to be chance. The frequencies below can be read "
                   "as how often similar readings were followed by a gain here; they are still not forecasts.")
    elif not usable:
        verdict = "Not enough history to check the score against outcomes."
    else:
        verdict = ("On this data, higher net scores have not reliably been followed by gains more often. Treat the "
                   "score as a count of evidence only.")
    return {"horizon": horizon, "buckets": buckets, "calibrated": calibrated, "ordered": ordered,
            "p_value": p_value, "verdict": verdict, "days": int(len(d))}


def block_gap_test(d: pd.DataFrame, col: str, low: tuple[float, float], high: tuple[float, float],
                   horizon: int) -> float | None:
    """One-sided p-value that days in the `high` score range were followed by a gain more often than
    days in the `low` range. Days are grouped into non-overlapping blocks of `horizon` trading days
    across all stocks, and each block counts once: overlapping holding windows, and stocks moving
    together on the same dates, are not independent observations."""
    span = max(1, round(horizon * 7 / 5))
    blocks = pd.to_datetime(d["date"]).map(pd.Timestamp.toordinal) // span
    rose = (d[col] > 0).astype(float)

    def means(lo: float, hi: float) -> np.ndarray:
        sel = (d["net"] >= lo) & (d["net"] < hi)
        return rose[sel].groupby(blocks[sel]).mean().to_numpy()

    top, bottom = means(*high), means(*low)
    if len(top) < 10 or len(bottom) < 10:
        return None
    t = stats.ttest_ind(top, bottom, equal_var=False)
    if not np.isfinite(t.statistic):
        return None
    return float(t.pvalue / 2) if t.statistic > 0 else 1.0 - float(t.pvalue / 2)


def bucket_for(net: float | None) -> tuple[float, float] | None:
    if net is None or not math.isfinite(net):
        return None
    for lo, hi in SCORE_BUCKETS:
        if lo <= net < hi:
            return lo, hi
    return None


# --- storage ---------------------------------------------------------------------------------------

COLUMNS = ("symbol", "signal_key", "direction", "date", "close", "trend_state", "net_score", "regime",
           "fwd_5", "fwd_10", "fwd_20", "spy_5", "spy_10", "spy_20", "high_20", "low_20")


def store(db: Any, source: str, symbol: str, rows: list[dict[str, Any]]) -> None:
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    with db.tx() as cur:
        cur.execute("DELETE FROM signal_occurrences WHERE data_source = ? AND symbol = ?", (source, symbol))
        cur.executemany(
            f"INSERT INTO signal_occurrences (data_source, engine_version, trend_version, created_at, "
            f"{', '.join(COLUMNS)}) VALUES (?, ?, ?, ?, {', '.join('?' for _ in COLUMNS)})",
            [(source, SIGNAL_ENGINE_VERSION, TREND_ENGINE_VERSION, now, *(r.get(c) for c in COLUMNS)) for r in rows])


def load(db: Any, source: str, signal_key: str | None = None, symbol: str | None = None) -> list[dict[str, Any]]:
    sql, args = "SELECT * FROM signal_occurrences WHERE data_source = ?", [source]
    if signal_key:
        sql += " AND signal_key = ?"
        args.append(signal_key)
    if symbol:
        sql += " AND symbol = ?"
        args.append(symbol)
    return [dict(r) for r in db.query(sql + " ORDER BY date", tuple(args))]


def grouped(rows: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    out: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for r in rows:
        out[r["signal_key"]].append(r)
    return out
