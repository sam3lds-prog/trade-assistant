"""A simulated long trade with a target, a stop and a time limit, from daily bars (4.0, phase 3).

Entry at the open of the session after the setup. Each later session: an open beyond a barrier fills at that open
(a gap through the stop fills worse than the stop, a gap through the target better than the target); otherwise a
high at or above the target fills at the target and a low at or below the stop at the stop. A session whose range
reaches both is `ambiguous`: daily bars cannot say which came first, so it is scored as the stop, the worse case.
After `hold` sessions the trade leaves at the close. Costs (spread and slippage, per side) come off both fills.
"""

from __future__ import annotations

import math
from typing import Any

import numpy as np

BARRIERS = ("target", "stop", "timeout", "ambiguous")


def simulate(opens: np.ndarray, highs: np.ndarray, lows: np.ndarray, closes: np.ndarray, atr_abs: float, *,
             target_atr: float, stop_atr: float, cost_bps_per_side: float) -> dict[str, Any]:
    """`opens`... are the trade's sessions, entry day first. Returns the barrier, the exit day's offset (0 = entry
    day), the prices and the gross and net returns; barrier None when a bar is missing."""
    n = len(opens)
    if n == 0 or not all(np.isfinite(a).all() for a in (opens, highs, lows, closes)) or not math.isfinite(atr_abs) or atr_abs <= 0:
        return {"barrier": None}
    entry = float(opens[0])
    target = entry + target_atr * atr_abs
    stop = entry - stop_atr * atr_abs
    barrier, exit_price, k_exit = "timeout", float(closes[-1]), n - 1
    for k in range(n):
        o, h, lo = float(opens[k]), float(highs[k]), float(lows[k])
        if k > 0 and o >= target:
            barrier, exit_price, k_exit = "target", o, k
            break
        if k > 0 and o <= stop:
            barrier, exit_price, k_exit = "stop", o, k
            break
        up, down = h >= target, lo <= stop
        if up and down:
            barrier, exit_price, k_exit = "ambiguous", stop, k
            break
        if down:
            barrier, exit_price, k_exit = "stop", stop, k
            break
        if up:
            barrier, exit_price, k_exit = "target", target, k
            break
    gross = exit_price / entry - 1.0
    cost = cost_bps_per_side / 1e4
    net = (exit_price * (1 - cost)) / (entry * (1 + cost)) - 1.0
    return {"barrier": barrier, "exit_offset": k_exit, "entry": entry, "target": target, "stop": stop,
            "exit_price": exit_price, "gross": gross, "net": net}


def outcome_class(barrier: str | None) -> str | None:
    """What the trade model learns: an ambiguous day counts as the stop."""
    if barrier is None:
        return None
    return "stop" if barrier == "ambiguous" else barrier


def equity_stats(net_returns: list[float], dates: list[str]) -> dict[str, Any]:
    """Trade statistics, each trade one unit of capital: win rate, expectancy, profit factor, and the deepest fall of
    the running total (by exit date)."""
    r = np.asarray(net_returns, dtype=float)
    if not len(r):
        return {"trades": 0}
    wins, losses = r[r > 0], r[r <= 0]
    order = np.argsort(np.asarray(dates))
    curve = np.cumsum(r[order])
    peak = np.maximum.accumulate(np.concatenate([[0.0], curve]))[1:]
    return {"trades": int(len(r)), "win_rate": float((r > 0).mean()), "expectancy": float(r.mean()),
            "median": float(np.median(r)), "profit_factor": float(wins.sum() / -losses.sum()) if losses.sum() < 0 else None,
            "total": float(r.sum()), "max_drawdown": float((curve - peak).min()) if len(curve) else 0.0}


__all__ = ["simulate", "outcome_class", "equity_stats", "BARRIERS"]
