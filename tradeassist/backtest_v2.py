"""Backtest V2: strategies written as conditions, filtered by market regime, with realistic shorts,
rolling walk-forward testing and a Monte Carlo check of how lucky the result was.

A strategy is a list of conditions for going long and, optionally, for going short. Each
condition compares a field with a number or with another field:

    {"field": "rsi", "op": "<", "value": 35}
    {"field": "close", "op": "crosses_above", "value": "sma50"}
    {"field": "trend_state", "op": "in", "value": ["UP", "STRONG_UP"]}
    {"field": "pattern", "op": "fired", "value": "hammer"}

All conditions in a list must hold on the same day (AND). The fields are the indicator
columns, the trend engine's state and net score, the market regime and the patterns. Nothing
is evaluated as code: fields and operators come from fixed lists.

Everything a condition reads is known at that day's close and uses finished bars only; the
trade is entered at the next day's open by the same engine as the classic backtest
(backtest.py), with its stops, targets, gap fills, slippage and cooldown. Shorts pay a borrow
fee (`short_borrow_rate_pct`, yearly, by the calendar day). `regimes` limits entries to days in
the listed market regimes.

Monte Carlo: the daily returns are resampled in blocks of 20 days (keeping streaks together)
1,000 times, to show the range of results a strategy with the same day-to-day behaviour could
have had. It says how much of the result could be the order luck put the days in; it does
not make the strategy's edge any more real.

Option strategies need the price of every contract on every past day. No connected source
supplies that, so option backtests are not run (see options/history.py); they are never
approximated from today's volatility.
"""

from __future__ import annotations

import math
from typing import Any

import numpy as np
import pandas as pd

from .backtest import Backtester, BacktestParams, deflated_sharpe, max_drawdown, walk_forward
from .patterns import PATTERNS, detect_all

NUMERIC_FIELDS = {
    "close": "Close", "open": "Open", "high": "High", "low": "Low", "volume": "Volume",
    "rsi": "RSI (14)", "macd": "MACD line", "macd_signal": "MACD signal line", "macd_hist": "MACD histogram",
    "sma10": "10-day average", "sma20": "20-day average", "sma50": "50-day average", "sma200": "200-day average",
    "ema20": "20-day exponential average", "adx": "ADX (14)", "atr": "Average true range", "pos52": "52-week range position (0-100)",
    "hv20": "20-day realized volatility", "vol_ratio": "Volume against its 20-day average", "bb_low": "Lower Bollinger band",
    "bb_mid": "Middle Bollinger band", "bb_high": "Upper Bollinger band", "struct_state": "Swing structure (+1 up, -1 down, 0 range)",
    "trend_net": "Trend engine net score", "trend_bullish": "Trend engine bullish score", "trend_bearish": "Trend engine bearish score",
    "change_5d": "Change over 5 days (%)", "change_20d": "Change over 20 days (%)",
}
CATEGORY_FIELDS = {"trend_state": "Trend engine state", "regime": "Market regime"}
OPS = (">", ">=", "<", "<=", "crosses_above", "crosses_below", "in", "not_in", "fired")
MAX_CONDITIONS = 8
MC_PATHS = 1000
MC_BLOCK = 20

PRESETS = {
    "trend_following": {"label": "Trend following: buy uptrends in a bull market",
                        "long": [{"field": "trend_state", "op": "in", "value": ["UP", "STRONG_UP"]},
                                 {"field": "close", "op": ">", "value": "sma50"}],
                        "short": [], "regimes": ["BULL_CALM", "BULL_VOLATILE"]},
    "pullback": {"label": "Pullback: an uptrend with RSI under 40",
                 "long": [{"field": "trend_state", "op": "in", "value": ["UP", "STRONG_UP"]},
                          {"field": "rsi", "op": "<", "value": 40}], "short": [], "regimes": []},
    "breakdown_short": {"label": "Breakdown short: a downtrend closing under the 50-day",
                        "long": [], "short": [{"field": "trend_state", "op": "in", "value": ["DOWN", "STRONG_DOWN"]},
                                              {"field": "close", "op": "crosses_below", "value": "sma50"}],
                        "regimes": ["BEAR_CALM", "BEAR_VOLATILE", "TRANSITION"]},
    "both_ways": {"label": "Both ways: long strong uptrends, short strong downtrends",
                  "long": [{"field": "trend_state", "op": "in", "value": ["STRONG_UP"]}],
                  "short": [{"field": "trend_state", "op": "in", "value": ["STRONG_DOWN"]}], "regimes": []},
}


class StrategyError(ValueError):
    pass


def validate(strategy: dict[str, Any]) -> dict[str, Any]:
    long_, short = strategy.get("long") or [], strategy.get("short") or []
    if not long_ and not short:
        raise StrategyError("Give at least one condition for going long or short")
    for _side, conds in (("long", long_), ("short", short)):
        if len(conds) > MAX_CONDITIONS:
            raise StrategyError(f"At most {MAX_CONDITIONS} conditions per side")
        for c in conds:
            f, op, v = c.get("field"), c.get("op"), c.get("value")
            if op not in OPS:
                raise StrategyError(f"Unknown operator {op!r}; use one of {', '.join(OPS)}")
            if op == "fired":
                if f != "pattern" or v not in PATTERNS:
                    raise StrategyError(f"'fired' needs field 'pattern' and a pattern key, got {v!r}")
                continue
            if f in CATEGORY_FIELDS:
                if op not in ("in", "not_in") or not isinstance(v, list) or not v:
                    raise StrategyError(f"{f} is compared with 'in' or 'not_in' and a list of values")
                continue
            if f not in NUMERIC_FIELDS:
                raise StrategyError(f"Unknown field {f!r}")
            if op in ("in", "not_in"):
                raise StrategyError(f"{f} is a number; compare it with >, <, or crosses")
            if isinstance(v, str):
                if v not in NUMERIC_FIELDS:
                    raise StrategyError(f"Unknown field {v!r} to compare {f} with")
            elif not (isinstance(v, (int, float)) and math.isfinite(float(v))):
                raise StrategyError(f"{f} must be compared with a number or another field")
    regimes = strategy.get("regimes") or []
    return {"long": long_, "short": short, "regimes": regimes}


def columns(df: pd.DataFrame, states: pd.DataFrame | None, regimes: pd.Series | None) -> pd.DataFrame:
    """Every field a condition can read, one row per day."""
    out = pd.DataFrame(index=df.index)
    for f in NUMERIC_FIELDS:
        if f in df.columns:
            out[f] = df[f].astype(float)
    out["change_5d"] = 100 * (df["close"] / df["close"].shift(5) - 1)
    out["change_20d"] = 100 * (df["close"] / df["close"].shift(20) - 1)
    if states is not None:
        out["trend_net"] = states["net_score"].to_numpy()
        out["trend_bullish"] = states["bullish_score"].to_numpy()
        out["trend_bearish"] = states["bearish_score"].to_numpy()
        out["trend_state"] = states["trend_state"].to_numpy()
    else:
        out["trend_state"] = None
    if regimes is not None:
        out["regime"] = regimes.reindex(pd.to_datetime(df["date"]), method="ffill").to_numpy()
    else:
        out["regime"] = None
    return out


def evaluate_conditions(cols: pd.DataFrame, conds: list[dict[str, Any]], hits: pd.DataFrame | None) -> np.ndarray:
    """True on the days every condition holds."""
    ok = np.ones(len(cols), dtype=bool)
    for c in conds:
        f, op, v = c["field"], c["op"], c.get("value")
        if op == "fired":
            ok &= hits[v].to_numpy(dtype=bool) if hits is not None and v in hits.columns else False
            continue
        if f in CATEGORY_FIELDS:
            vals = cols[f].to_numpy(dtype=object)
            inside = np.array([x in v for x in vals], dtype=bool)
            ok &= inside if op == "in" else ~inside & np.array([x is not None for x in vals])
            continue
        a = cols[f].to_numpy(dtype=float) if f in cols.columns else np.full(len(cols), np.nan)
        b = (cols[v].to_numpy(dtype=float) if v in cols.columns else np.full(len(cols), np.nan)) if isinstance(v, str) \
            else np.full(len(cols), float(v))
        with np.errstate(invalid="ignore"):
            if op == ">":
                ok &= a > b
            elif op == ">=":
                ok &= a >= b
            elif op == "<":
                ok &= a < b
            elif op == "<=":
                ok &= a <= b
            else:
                prev_a, prev_b = np.r_[np.nan, a[:-1]], np.r_[np.nan, b[:-1]]
                if op == "crosses_above":
                    ok &= (a > b) & (prev_a <= prev_b)
                else:
                    ok &= (a < b) & (prev_a >= prev_b)
    return ok


def signal_matrices(frames: dict[str, pd.DataFrame], states: dict[str, pd.DataFrame], regimes: pd.Series | None,
                    strategy: dict[str, Any], calendar: pd.DatetimeIndex) -> tuple[np.ndarray, np.ndarray, dict[str, int]]:
    """(direction, score) matrices in the engine's layout (calendar days by sorted symbols)."""
    symbols = sorted(frames)
    need_patterns = any(c["op"] == "fired" for c in strategy["long"] + strategy["short"])
    dirs, scores, counts = [], [], {"long_days": 0, "short_days": 0, "blocked_by_regime": 0}
    for sym in symbols:
        df = frames[sym]
        cols = columns(df, states.get(sym), regimes)
        hits = detect_all(df) if need_patterns else None
        go_long = evaluate_conditions(cols, strategy["long"], hits) if strategy["long"] else np.zeros(len(df), bool)
        go_short = evaluate_conditions(cols, strategy["short"], hits) if strategy["short"] else np.zeros(len(df), bool)
        both = go_long & go_short
        go_long, go_short = go_long & ~both, go_short & ~both
        if strategy["regimes"]:
            allowed = np.array([r in strategy["regimes"] for r in cols["regime"].to_numpy(dtype=object)], dtype=bool)
            counts["blocked_by_regime"] += int(((go_long | go_short) & ~allowed).sum())
            go_long, go_short = go_long & allowed, go_short & allowed
        counts["long_days"] += int(go_long.sum())
        counts["short_days"] += int(go_short.sum())
        d = np.where(go_long, 1, np.where(go_short, -1, 0))
        net = cols["trend_net"].to_numpy(dtype=float) if "trend_net" in cols.columns else np.zeros(len(df))
        sc = np.where(np.isfinite(net), np.abs(net), 0.0)
        idx = pd.to_datetime(df["date"])
        dirs.append(pd.Series(d, index=idx).reindex(calendar).fillna(0).to_numpy())
        scores.append(pd.Series(sc, index=idx).reindex(calendar).fillna(0).to_numpy())
    return np.column_stack(dirs).astype(int), np.column_stack(scores), counts


def monte_carlo(daily: np.ndarray, initial: float, paths: int = MC_PATHS, block: int = MC_BLOCK,
                seed: int = 7) -> dict[str, Any] | None:
    r = np.asarray(daily, dtype=float)
    r = r[np.isfinite(r)]
    n = len(r)
    if n < 2 * block:
        return None
    rng = np.random.default_rng(seed)
    starts = np.arange(0, n - block + 1)
    finals, drawdowns = np.empty(paths), np.empty(paths)
    for k in range(paths):
        picks = rng.choice(starts, size=math.ceil(n / block))
        path = np.concatenate([r[s:s + block] for s in picks])[:n]
        equity = initial * np.cumprod(1 + path)
        finals[k] = equity[-1] / initial - 1
        drawdowns[k] = max_drawdown(np.r_[initial, equity])
    q = lambda a, p: float(np.percentile(a, p))
    return {"paths": paths, "block_days": block,
            "return_pct": {"p5": 100 * q(finals, 5), "p50": 100 * q(finals, 50), "p95": 100 * q(finals, 95)},
            "max_drawdown_pct": {"p5": 100 * q(drawdowns, 5), "p50": 100 * q(drawdowns, 50), "p95": 100 * q(drawdowns, 95)},
            "chance_of_loss": float(np.mean(finals < 0)), "chance_drawdown_over_20pct": float(np.mean(drawdowns < -0.20)),
            "note": "Daily returns resampled in 20-day blocks. The range of outcomes the same day-to-day behaviour could "
                    "have produced in a different order; it does not test whether the edge is real."}


MC_SEED = 7       # fixed, so the same data and settings always give the same Monte Carlo range


def run(frames: dict[str, pd.DataFrame], states: dict[str, pd.DataFrame], regimes: pd.Series | None,
        strategy: dict[str, Any], params: BacktestParams, start: str | None = None,
        with_monte_carlo: bool = True, n_trials: int = 1, seed: int = MC_SEED) -> dict[str, Any]:
    strategy = validate(strategy)
    calendar = Backtester(frames).calendar
    direction, score, counts = signal_matrices(frames, states, regimes, strategy, calendar)
    result = Backtester(frames, (direction, score)).run(params, start)
    returns = result.pop("daily_returns")
    result["significance"] = deflated_sharpe(returns, max(int(n_trials), 1), None, params.risk_free_rate)
    bench = result["benchmark"].pop("equity")
    for point, value in zip(result["equity_curve"], bench):
        point["benchmark"] = value
    result["signals"] = counts
    result["strategy"] = strategy
    result["monte_carlo"] = monte_carlo(returns, params.initial_capital, seed=seed) if with_monte_carlo else None
    if result["monte_carlo"] is not None:
        result["monte_carlo"]["seed"] = seed
    shorts = [t for t in result["trades"] if t["direction"] == "SHORT"]
    result["borrow_cost"] = float(sum(t.get("borrow_cost", 0.0) for t in shorts))
    result["notes"] = notes(result, params)
    return result


def run_walk_forward(frames: dict[str, pd.DataFrame], states: dict[str, pd.DataFrame], regimes: pd.Series | None,
                     strategy: dict[str, Any], params: BacktestParams, grid: dict[str, list[float]] | None,
                     train_days: int, test_days: int, anchored: bool = False) -> dict[str, Any]:
    strategy = validate(strategy)
    bt = Backtester(frames)
    direction, score, counts = signal_matrices(frames, states, regimes, strategy, bt.calendar)
    grid = grid or {"stop_loss_pct": [0.04, 0.06, 0.08], "take_profit_pct": [0.10, 0.15, 0.20]}
    out = walk_forward(frames, params, grid, train_days, test_days, signal_override=(direction, score),
                       anchored=anchored, warmup_days=210)
    out["strategy"] = strategy
    out["signals"] = counts
    return out


def notes(result: dict[str, Any], params: BacktestParams) -> list[str]:
    out = ["Conditions are read at each day's close from finished bars and traded at the next open.",
           "The symbol list is today's, so companies that failed or were dropped are missing (survivorship bias).",
           "The trend engine's states here leave out validated-pattern points, which were found on this same history."]
    if result["num_trades"] < 30:
        out.append(f"Only {result['num_trades']} trades: too few to judge the strategy either way.")
    if result["signals"]["blocked_by_regime"]:
        out.append(f"{result['signals']['blocked_by_regime']} signal days were skipped because the market regime was not "
                   "one of those allowed.")
    if any(t["direction"] == "SHORT" for t in result["trades"]):
        out.append(f"Shorts paid {params.short_borrow_rate_pct:g}% a year to borrow (${result['borrow_cost']:,.0f} in "
                   "all). Hard-to-borrow stocks cost far more, and shares can be recalled.")
    if result["total_return_pct"] < result["benchmark"]["total_return_pct"]:
        out.append("The strategy returned less than simply holding the same stocks.")
    sig = result["significance"]
    if sig.get("deflated_sharpe") is not None and sig["deflated_sharpe"] < 0.95:
        out.append("The Sharpe ratio is not statistically distinguishable from luck at the usual 95% bar.")
    return out
