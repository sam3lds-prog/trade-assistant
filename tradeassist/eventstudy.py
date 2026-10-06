"""Event studies: what happened after each past occurrence of a pattern, measured
against the market over the same days, with safeguards against fooling yourself.

Conventions
- A pattern completes at the close of bar t. The trade enters at the open of bar
  t+1 and exits at the close of bar t+h, so nothing uses information from the future.
- "Net" is the direction-adjusted return after round-trip costs.
- "Excess" is the direction-adjusted return minus the average return of the other
  symbols in the run over the same days. A stock's own average return is
  deliberately not used: in a finite sample it is inflated for stocks that made
  new highs and deflated for those that made new lows, which biases the comparison
  by pattern type. An index fund alone is not used either when there are enough
  symbols, because a watchlist that beat (or lagged) the index over the period
  would make every bullish (or bearish) pattern look good.
- With fewer than five symbols there is no fair comparison group. The numbers are
  then shown against the supplied market benchmark and no verdict is given.
- The test is on the average excess return per event, with standard errors
  clustered by non-overlapping blocks of h trading days across all symbols,
  because overlapping windows and same-day events on different stocks are not
  independent observations.
- Discovery and confirmation use different data. The first 60% of the calendar is
  the discovery period: its p-values are adjusted for the number of pattern and
  horizon combinations tested (Benjamini-Hochberg). The last 40% is held out, and
  a pattern is only called supported if the held-out period independently agrees.
  Discovery events whose holding window would run into the held-out period are dropped.
"""

from __future__ import annotations

import math
from typing import Any

import numpy as np
import pandas as pd
from scipy import stats

from .patterns import PATTERNS, Pattern

MIN_EVENTS = 30          # discovery-period occurrences
MIN_BLOCKS = 20          # discovery-period independent blocks
MIN_HELD_OUT_EVENTS = 10
MIN_HELD_OUT_BLOCKS = 8
FDR = 0.10
HELD_OUT_ALPHA = 0.10    # one-sided, in the direction found in discovery
OOS_FRACTION = 0.40
MIN_SYMBOLS = 5          # fewer than this and there is no fair comparison group


def forward_returns(df: pd.DataFrame, horizon: int) -> pd.Series:
    """Return from the next bar's open to the close `horizon` bars after the signal bar."""
    entry = df["open"].shift(-1)
    exit_ = df["close"].shift(-horizon)
    return exit_ / entry - 1.0


def block_test(excess: pd.Series, blocks: pd.Series) -> dict[str, Any]:
    """Test whether the mean excess return per event differs from zero, with standard errors
    clustered by block.

    The statistic is the plain average over events, the same number that is displayed.
    Averaging block means instead would be biased: how many times a pattern fires inside a
    block depends on what happened after the first time (a breakout that runs keeps making
    new highs, one that fails fires once), so equal block weights over-count the failures.
    Events in one block are not independent, so the variance is built from block totals; and
    since a window that starts late in one block ends in the next, the covariance between
    neighbouring blocks is added when it is positive.
    """
    values = excess.to_numpy(dtype=float)
    ids = blocks.to_numpy()
    n_events = len(values)
    unique = np.unique(ids)
    n = len(unique)
    out: dict[str, Any] = {"n_blocks": int(n), "t_stat": None, "p_value": None}
    if n < 3:
        return out
    mean = float(values.mean())
    sums = pd.Series(values - mean).groupby(ids).sum()
    scale = n / (n - 1)
    variance = scale * float((sums ** 2).sum())
    nxt = sums.reindex(sums.index + 1)
    lag1 = scale * float(np.nansum(sums.to_numpy() * nxt.to_numpy()))
    variance = (variance + 2.0 * max(lag1, 0.0)) / (n_events ** 2)
    if variance <= 0:
        return out
    t_stat = mean / math.sqrt(variance)
    out.update({"t_stat": float(t_stat), "p_value": float(2.0 * stats.t.sf(abs(t_stat), df=n - 1)), "dof": n - 1})
    return out


def _baselines(frames: dict[str, pd.DataFrame], fwd: dict[str, pd.Series], horizon: int,
               benchmark: pd.DataFrame | None) -> dict[str, pd.Series]:
    """Benchmark forward return for each symbol and date, indexed by date."""
    if benchmark is not None:
        bench = pd.Series(forward_returns(benchmark, horizon).to_numpy(), index=pd.DatetimeIndex(benchmark["date"]))
        return {s: bench for s in frames}
    table = pd.DataFrame({s: pd.Series(fwd[s].to_numpy(), index=pd.DatetimeIndex(frames[s]["date"])) for s in frames})
    total, count = table.sum(axis=1, skipna=True), table.notna().sum(axis=1)
    out = {}
    for s in frames:
        own = table[s]
        others = (total - own.fillna(0.0)) / (count - own.notna().astype(int)).where(lambda c: c > 0)
        out[s] = others
    return out


def _study_one(frames: dict[str, pd.DataFrame], pattern: Pattern, horizon: int, cost_bps: float,
               calendar: pd.DatetimeIndex, split_index: int, fwd: dict[str, pd.Series],
               baselines: dict[str, pd.Series]) -> dict[str, Any]:
    cost = 2.0 * cost_bps / 10_000.0
    position = pd.Series(np.arange(len(calendar)), index=calendar)
    rows = []
    for symbol, df in frames.items():
        r_all = fwd[symbol]
        base_all = baselines[symbol].reindex(pd.DatetimeIndex(df["date"])).to_numpy()
        hits = pattern.detect(df).to_numpy(dtype=bool) & r_all.notna().to_numpy() & np.isfinite(base_all)
        if not hits.any():
            continue
        r = r_all.to_numpy()[hits]
        rows.append(pd.DataFrame({
            "symbol": symbol, "date": df["date"].to_numpy()[hits], "raw": r,
            "net": pattern.direction * r - cost,
            "excess": pattern.direction * (r - base_all[hits]),
        }))
    base = {"pattern": pattern.key, "label": pattern.label, "direction": pattern.direction,
            "description": pattern.description, "horizon": horizon}
    empty = {"n_events": 0, "n_in_sample": 0, "n_out_of_sample": 0, "blocks_in_sample": 0,
             "blocks_out_of_sample": 0, "p_value": None, "t_in_sample": None, "t_out_of_sample": None,
             "p_out_of_sample": None, "mean_net": None, "mean_excess": None, "hit_rate": None,
             "in_sample_excess": None, "out_of_sample_excess": None}
    if not rows:
        return {**base, **empty}
    events = pd.concat(rows, ignore_index=True)
    idx = position.reindex(pd.DatetimeIndex(events["date"])).to_numpy()
    events["block"] = idx // horizon
    oos_mask = idx >= split_index
    purge_mask = (idx < split_index) & (idx + horizon >= split_index)   # window would cross into the held-out period
    ins, oos = events[~oos_mask & ~purge_mask], events[oos_mask]

    t_in = block_test(ins["excess"], ins["block"]) if len(ins) else {"n_blocks": 0, "t_stat": None, "p_value": None}
    t_out = block_test(oos["excess"], oos["block"]) if len(oos) else {"n_blocks": 0, "t_stat": None, "p_value": None}
    p_out = None
    if t_in["t_stat"] is not None and t_out["t_stat"] is not None:
        # One-sided: does the held-out period agree with the direction found in discovery?
        same_way = t_out["t_stat"] if t_in["t_stat"] >= 0 else -t_out["t_stat"]
        p_out = float(stats.t.sf(same_way, df=t_out["dof"]))
    return {
        **base,
        "n_events": int(len(events)), "n_symbols": int(events["symbol"].nunique()),
        "n_in_sample": int(len(ins)), "n_out_of_sample": int(len(oos)),
        "blocks_in_sample": t_in["n_blocks"], "blocks_out_of_sample": t_out["n_blocks"],
        "mean_net": float(events["net"].mean()), "median_net": float(events["net"].median()),
        "hit_rate": float((events["net"] > 0).mean()),
        "mean_excess": float(events["excess"].mean()),
        "in_sample_excess": float(ins["excess"].mean()) if len(ins) else None,
        "out_of_sample_excess": float(oos["excess"].mean()) if len(oos) else None,
        "t_in_sample": t_in["t_stat"], "p_value": t_in["p_value"],
        "t_out_of_sample": t_out["t_stat"], "p_out_of_sample": p_out,
        "first_event": pd.Timestamp(events["date"].min()).strftime("%Y-%m-%d"),
        "last_event": pd.Timestamp(events["date"].max()).strftime("%Y-%m-%d"),
    }


def benjamini_hochberg(p_values: list[float | None]) -> list[float | None]:
    """Adjusted p-values (q-values). None entries are skipped and stay None."""
    idx = [i for i, p in enumerate(p_values) if p is not None]
    out: list[float | None] = [None] * len(p_values)
    if not idx:
        return out
    ordered = sorted(idx, key=lambda i: p_values[i])
    m = len(ordered)
    running = 1.0
    for rank in range(m, 0, -1):
        i = ordered[rank - 1]
        running = min(running, p_values[i] * m / rank)
        out[i] = running
    return out


def _verdict(r: dict[str, Any]) -> tuple[str, str]:
    if r["n_events"] == 0:
        return "insufficient", "The pattern never occurred in this data."
    if not r["fair_baseline"]:
        return "insufficient", (f"A verdict needs at least {MIN_SYMBOLS} symbols, so that each stock can be "
                                "compared with the others over the same days.")
    thin = (r["n_in_sample"] < MIN_EVENTS or r["blocks_in_sample"] < MIN_BLOCKS
            or r["n_out_of_sample"] < MIN_HELD_OUT_EVENTS or r["blocks_out_of_sample"] < MIN_HELD_OUT_BLOCKS
            or r.get("q_value") is None or r["p_out_of_sample"] is None)
    if thin:
        return "insufficient", (
            f"{r['n_in_sample']} occurrences in {r['blocks_in_sample']} independent periods for discovery and "
            f"{r['n_out_of_sample']} in {r['blocks_out_of_sample']} held out. At least {MIN_EVENTS} in "
            f"{MIN_BLOCKS}, and {MIN_HELD_OUT_EVENTS} in {MIN_HELD_OUT_BLOCKS}, are needed before the numbers "
            "mean much.")
    discovered = r["q_value"] < FDR
    confirmed = r["p_out_of_sample"] < HELD_OUT_ALPHA
    positive = r["t_in_sample"] > 0
    if discovered and confirmed and positive and r["mean_net"] > 0:
        return "supported", ("Beat the other stocks by more than chance explains in the discovery period, did so again "
                             "in the held-out period, and stayed profitable after costs.")
    if discovered and confirmed and not positive:
        return "contradicted", ("Did reliably worse than the other stocks in the direction tested, in both periods: in "
                                "this data the pattern pointed the wrong way.")
    if discovered and confirmed:
        return "no_evidence", "Beat the other stocks in both periods but lost money after costs, so it is not usable."
    if discovered:
        return "no_evidence", ("Stood out in the discovery period but the held-out period did not confirm it. "
                               "That is what a lucky result looks like.")
    return "no_evidence", "Results are within what chance produces."


def run_study(frames: dict[str, pd.DataFrame], horizons: list[int] | None = None, cost_bps: float = 5.0,
              pattern_keys: list[str] | None = None, benchmark: pd.DataFrame | None = None,
              benchmark_label: str | None = None) -> dict[str, Any]:
    """Run every pattern at every horizon over enriched frames keyed by symbol.

    With five or more symbols each stock is compared with the average of the others.
    With fewer, `benchmark` (daily bars for a market index fund) is required and no
    verdicts are given.
    """
    horizons = horizons or [5, 10, 20]
    keys = pattern_keys or list(PATTERNS)
    frames = {s: df for s, df in frames.items() if len(df) > 260}
    if not frames:
        raise ValueError("Need at least one symbol with more than 260 daily bars")
    calendar = pd.DatetimeIndex(sorted(set().union(*[set(df["date"]) for df in frames.values()])))
    split_index = int(len(calendar) * (1.0 - OOS_FRACTION))
    split_date = calendar[split_index]

    fair = len(frames) >= MIN_SYMBOLS
    if not fair and benchmark is None:
        raise ValueError(f"The study needs at least {MIN_SYMBOLS} symbols, or a market benchmark to show "
                         "numbers against")
    results = []
    for h in horizons:
        fwd = {s: forward_returns(df, h) for s, df in frames.items()}
        baselines = _baselines(frames, fwd, h, None if fair else benchmark)
        for k in keys:
            results.append(_study_one(frames, PATTERNS[k], h, cost_bps, calendar, split_index, fwd, baselines))
    q_values = benjamini_hochberg([r.get("p_value") for r in results])
    for r, q in zip(results, q_values):
        r["q_value"] = q
        r["fair_baseline"] = fair
        r["verdict"], r["verdict_text"] = _verdict(r)
    order = {"supported": 0, "contradicted": 1, "no_evidence": 2, "insufficient": 3}
    results.sort(key=lambda r: (order[r["verdict"]], r["q_value"] if r["q_value"] is not None else 9.0))
    label = "the average of the other symbols in the run" if fair else (benchmark_label or "the market benchmark")
    return {
        "symbols": sorted(frames), "horizons": horizons, "cost_bps_per_side": cost_bps,
        "first_date": calendar[0].strftime("%Y-%m-%d"), "last_date": calendar[-1].strftime("%Y-%m-%d"),
        "split_date": split_date.strftime("%Y-%m-%d"), "tests_run": len(results),
        "false_discovery_rate": FDR, "held_out_alpha": HELD_OUT_ALPHA, "benchmark": label,
        "verdicts_given": fair, "results": results,
        "caveats": [
            "Uses today's list of symbols, so companies that failed or were dropped are missing "
            "(survivorship bias). That flatters long-side results.",
            "Returns are for the stock. An option on the same move also pays time decay and a wider spread.",
            "A supported pattern was true in this sample. It is evidence, not a promise.",
            "Re-running with different symbols, years or holding periods until something turns up is "
            "itself a way of fooling yourself: the correction only covers the tests in one run.",
        ],
    }
