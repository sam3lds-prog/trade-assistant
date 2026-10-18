"""Similar patterns (2.12): where in this stock's own past the shape on the screen appeared before, and what
the price did next.

The shape is the closes of the window on the screen, normalised (each close as standard deviations from the
window's mean, on a log scale, so a $50 stock and a $500 stock, a rise and a fall of the same form, all compare
alike). Every window of the same length in the stock's history of the same bar size is scored against it by Pearson
correlation on the normalised closes (the root-mean-square distance between two standardised shapes is a
function of that correlation, sqrt(2 - 2r), so it is reported but adds nothing).

A match needs a correlation of at least MIN_CORRELATION, matches never overlap each other, and neither a
match nor the bars read off after it may reach into the window on the screen (otherwise the window's own
path would be counted as "what followed"); the best MAX_MATCHES are kept. For each match the bars after it are read off: the return at
each horizon, the best and worst along the way, and the whole path, so the chart can draw what came next
over the end of the window on the screen.

What this is, and is not. The frequencies are how often the stock's own past matches went on to rise, over
each horizon, with a 95% Wilson interval: with 8 matches the interval runs from about 15% to 85%, which is the
honest size of what 8 cases can say. They are not a forecast, and a shape that looked the same before had
other news and another market behind it. Nothing here places an order.
"""

from __future__ import annotations

from typing import Any

import numpy as np
import pandas as pd

from .signalbook import wilson

MIN_CORRELATION = 0.75
FLOOR_CORRELATION = 0.5        # the bar is never lowered below this on its own
MAX_MATCHES = 20
MIN_WINDOW = 8
MAX_WINDOW = 400
# The horizons read off after each match, in bars, by bar size; each with how long that is in trading time.
HORIZONS: dict[str, tuple[tuple[int, str], ...]] = {
    "1m": ((5, "5 minutes"), (15, "15 minutes"), (30, "30 minutes"), (60, "1 hour"), (120, "2 hours")),
    "5m": ((3, "15 minutes"), (6, "30 minutes"), (12, "1 hour"), (36, "3 hours"), (78, "1 day")),
    "15m": ((2, "30 minutes"), (4, "1 hour"), (12, "3 hours"), (26, "1 day"), (52, "2 days")),
    "1h": ((1, "1 hour"), (3, "3 hours"), (7, "1 day"), (14, "2 days"), (35, "1 week")),
    "1d": ((1, "1 day"), (3, "3 days"), (5, "1 week"), (10, "2 weeks"), (20, "1 month")),
    "1w": ((1, "1 week"), (2, "2 weeks"), (4, "1 month"), (8, "2 months"), (13, "1 quarter")),
    "1M": ((1, "1 month"), (3, "1 quarter"), (6, "6 months"), (12, "1 year")),
    "1Q": ((1, "1 quarter"), (2, "6 months"), (4, "1 year")),
}
MIN_CASES = 10                 # fewer matches than this: the frequencies are shown as too few to lean on
GROUP_WORDS = {"1m": "one-minute", "5m": "five-minute", "15m": "fifteen-minute", "1h": "hourly", "1w": "weekly",
               "1M": "monthly", "1Q": "quarterly"}   # the average range's unit in the scenario labels


def normalise(closes: np.ndarray) -> np.ndarray | None:
    """A shape: log closes as standard deviations from their mean. None for a flat window."""
    x = np.log(np.asarray(closes, dtype=float))
    if len(x) < 2 or not np.all(np.isfinite(x)):
        return None
    sd = x.std()
    if sd <= 1e-9:
        return None
    return (x - x.mean()) / sd


def sliding(x: np.ndarray, n: int) -> np.ndarray:
    """Every window of `n` consecutive values of `x`, as rows (a view, no copy)."""
    return np.lib.stride_tricks.sliding_window_view(x, n)


def score_windows(closes: np.ndarray, shape: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """For every window of len(shape) bars in `closes`: its correlation with the shape and its distance from
    it. Windows that are flat or hold bad prices score nan."""
    n = len(shape)
    x = np.log(np.asarray(closes, dtype=float))
    if len(x) < n:
        return np.array([]), np.array([])
    w = sliding(x, n)
    mean = w.mean(axis=1, keepdims=True)
    sd = w.std(axis=1)
    with np.errstate(invalid="ignore", divide="ignore"):
        z = (w - mean) / sd[:, None]
        corr = (z * shape).mean(axis=1)                     # both standardised, so this is Pearson's r
        dist = np.sqrt(((z - shape) ** 2).mean(axis=1))
    bad = ~np.isfinite(corr) | (sd <= 1e-9)
    corr[bad], dist[bad] = np.nan, np.nan
    return corr, dist


def pick_matches(corr: np.ndarray, dist: np.ndarray, n: int, exclude: tuple[int, int] | None,
                 min_corr: float = MIN_CORRELATION, limit: int = MAX_MATCHES, guard: int = 0) -> list[int]:
    """The start bars of the best matches: correlation at least `min_corr`, best first, none overlapping another
    or the window `exclude` (start, end bars of the shape on the screen), and none whose `guard` bars after
    reach into that window."""
    if len(corr) == 0:
        return []
    score = np.where(np.isfinite(corr), corr, -np.inf)
    order = np.argsort(-score)
    taken: list[int] = []
    for i in order:
        i = int(i)
        if not np.isfinite(corr[i]) or corr[i] < min_corr:
            break
        if exclude is not None and i <= exclude[1] and i + n - 1 + guard >= exclude[0]:
            continue
        if any(abs(i - j) < n for j in taken):
            continue
        taken.append(i)
        if len(taken) >= limit:
            break
    return sorted(taken)


def _pct(a: float, b: float) -> float:
    return 100.0 * (a / b - 1.0)


def search(series: pd.DataFrame, start: int, end: int, group: str, *, atr: float | None = None,
           min_corr: float = MIN_CORRELATION, limit: int = MAX_MATCHES) -> dict[str, Any]:
    """Matches for the window `series[start:end+1]` (bar positions) in the rest of `series`, and what followed
    each. `series` has close, high and low, oldest first; `atr` is one average range of these bars in price,
    for the scenario buckets."""
    n = end - start + 1
    closes = series["close"].to_numpy(dtype=float)
    highs = series["high"].to_numpy(dtype=float)
    lows = series["low"].to_numpy(dtype=float)
    out: dict[str, Any] = {"window": {"start": int(start), "end": int(end), "bars": int(n)}, "matches": [],
                           "horizons": [], "scenarios": [], "overlay": None, "searched": int(len(closes)),
                           "min_correlation": min_corr, "note": None}
    if n < MIN_WINDOW:
        out["note"] = f"Select at least {MIN_WINDOW} bars to search for a pattern (this window has {n})."
        return out
    if n > MAX_WINDOW:
        out["note"] = f"A pattern can be at most {MAX_WINDOW} bars long (this window has {n}); zoom in or group the bars."
        return out
    shape = normalise(closes[start:end + 1])
    if shape is None:
        out["note"] = "The window is flat, so there is no shape to match."
        return out
    horizons = HORIZONS.get(group, HORIZONS["1d"])
    max_h = horizons[-1][0]
    corr, dist = score_windows(closes, shape)
    starts = pick_matches(corr, dist, n, (start, end), min_corr, limit, guard=max_h)
    # The windows the search may use: not overlapping the window on the screen, and with their bars after not
    # reaching into it either.
    eligible = np.array([c for i, c in enumerate(corr) if np.isfinite(c) and (i + n - 1 + max_h < start or i > end)])
    out["best_correlation"] = float(eligible.max()) if len(eligible) else None
    out["eligible_windows"] = int(len(eligible))
    if not len(eligible):
        out["note"] = (f"This stock's {len(closes)} bars at this grouping hold no stretch of {n + max_h} bars before the "
                       f"window to compare with (the window, then {max_h} bars after it). Select a shorter window, or "
                       "group the bars more finely.")
        return out
    asked = min_corr
    while not starts and min_corr - 0.1 >= FLOOR_CORRELATION - 1e-9:
        # Nothing that alike: the bar is lowered a step at a time, down to the floor, and the result says so.
        min_corr = round(min_corr - 0.1, 2)
        starts = pick_matches(corr, dist, n, (start, end), min_corr, limit, guard=max_h)
    out["min_correlation"] = min_corr
    out["lowered_from"] = asked if min_corr < asked and starts else None
    if not starts:
        best = f" The closest was {out['best_correlation']:.0%}; lower the bar, or select a shorter window." \
            if out["best_correlation"] is not None else ""
        out["note"] = (f"No window of {n} bars in this stock's {len(closes)} bars of history matched the shape at "
                       f"{min_corr:.0%} correlation or better.{best}")
        return out
    q_mean, q_sd = np.log(closes[start:end + 1]).mean(), np.log(closes[start:end + 1]).std()
    matches = []
    after_paths: list[np.ndarray] = []
    for i in starts:
        j = i + n - 1                                         # the match's last bar
        z = normalise(closes[i:j + 1])
        fit = np.exp(q_mean + z * q_sd)                        # the match's shape drawn at the screen window's scale
        last = closes[j]
        after = closes[j + 1:j + 1 + max_h]
        rel = after / last - 1.0                               # the path after, as a fraction of the match's last close
        fwd: dict[str, Any] = {}
        for h, _label in horizons:
            if j + h < len(closes):
                seg_hi, seg_lo = highs[j + 1:j + 1 + h].max(), lows[j + 1:j + 1 + h].min()
                fwd[str(h)] = {"pct": _pct(closes[j + h], last), "best_pct": _pct(seg_hi, last),
                               "worst_pct": _pct(seg_lo, last)}
        matches.append({"start": int(i), "end": int(j), "correlation": float(corr[i]), "distance": float(dist[i]),
                        "last_close": float(last), "fit": [float(v) for v in fit],
                        "after": [float(v) for v in rel], "forward": fwd})
        if len(rel):
            after_paths.append(rel)
    out["matches"] = matches
    # The frequencies: how often the matches went on to rise, over each horizon.
    for h, label in horizons:  # noqa: B007
        pcts = [m["forward"][str(h)]["pct"] for m in matches if str(h) in m["forward"]]
        if not pcts:
            continue
        arr = np.array(pcts)
        ups = int((arr > 0).sum())
        lo, hi = wilson(ups, len(arr))
        row = {"bars": h, "label": label, "cases": int(len(arr)), "up": ups, "down": int((arr < 0).sum()),
               "up_frequency": ups / len(arr), "low": lo, "high": hi, "median_pct": float(np.median(arr)),
               "mean_pct": float(arr.mean()), "p25_pct": float(np.percentile(arr, 25)),
               "p75_pct": float(np.percentile(arr, 75)), "enough": len(arr) >= MIN_CASES}
        if atr and atr > 0:
            last_q = closes[end]
            edge = 100.0 * atr / last_q                        # one average range as a percent of the current price
            unit = "daily range" if group == "1d" else f"{GROUP_WORDS.get(group, 'bar')} range"
            row["scenarios"] = {
                "up_range": {"label": f"Up more than one {unit} (+{edge:.1f}%)", "cases": int((arr >= edge).sum())},
                "within": {"label": f"Within one {unit} either way", "cases": int(((arr > -edge) & (arr < edge)).sum())},
                "down_range": {"label": f"Down more than one {unit} (-{edge:.1f}%)", "cases": int((arr <= -edge).sum())},
            }
            for v in row["scenarios"].values():
                v["frequency"] = v["cases"] / len(arr)
                v["low"], v["high"] = wilson(v["cases"], len(arr))
        out["horizons"].append(row)
    # The pooled path after: the median and quartiles across matches, bar by bar, from the window's last close.
    if after_paths:
        width = max(len(p) for p in after_paths)
        grid = np.full((len(after_paths), width), np.nan)
        for row_k, p in enumerate(after_paths):
            grid[row_k, :len(p)] = p
        counts = np.sum(np.isfinite(grid), axis=0)
        keep = counts >= max(3, min(len(after_paths), MIN_CASES) // 2)
        last_q = float(closes[end])
        with np.errstate(all="ignore"):
            med = np.nanmedian(grid, axis=0)
            p25 = np.nanpercentile(grid, 25, axis=0)
            p75 = np.nanpercentile(grid, 75, axis=0)
        out["overlay"] = {"from_close": last_q,
                          "median": [float(last_q * (1 + v)) if keep[k] else None for k, v in enumerate(med)],
                          "p25": [float(last_q * (1 + v)) if keep[k] else None for k, v in enumerate(p25)],
                          "p75": [float(last_q * (1 + v)) if keep[k] else None for k, v in enumerate(p75)],
                          "cases": [int(c) for c in counts]}
    lowered = (f" Nothing matched at {asked:.0%}, so the bar was lowered to {min_corr:.0%}: these are looser likenesses."
               if out["lowered_from"] else "")
    out["note"] = (f"{len(matches)} match{'es' if len(matches) != 1 else ''} of {n} bars in {len(closes)} bars of this "
                   f"stock's own history, at {min_corr:.0%} correlation or better.{lowered} Frequencies are what followed "
                   "those matches, not a forecast" + ("; fewer than 10 cases say little." if len(matches) < MIN_CASES else "."))
    return out


__all__ = ["search", "normalise", "score_windows", "pick_matches", "HORIZONS", "MIN_CORRELATION", "MAX_MATCHES",
           "MIN_WINDOW", "MAX_WINDOW", "MIN_CASES"]
