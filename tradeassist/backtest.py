"""Backtester for the scanner strategy, with walk-forward testing.

What changed from the project's original `backtester.py`:
- It runs on real historical signals instead of printing example numbers.
- Signals known at one day's close are traded at the next day's open (no look-ahead).
- Short positions are accounted for correctly (a falling price is a gain).
- Only trading days are simulated, so equity no longer drops to cash on weekends
  and the Sharpe ratio is computed on real daily returns.
- Stops and targets are checked against each day's high and low, with gaps filled
  at the open; if both are touched on one day the stop is assumed to come first.
- Slippage and commission are charged on every entry and exit.
- Drawdown is measured against the running peak, and the results come with a
  buy-and-hold benchmark and a Sharpe ratio adjusted for how many variations
  were tried.
"""

from __future__ import annotations

import itertools
import math
from dataclasses import asdict, dataclass, replace
from typing import Any

import numpy as np
import pandas as pd
from scipy import stats

from .signals import legacy_signals

TRADING_DAYS = 252
EULER_GAMMA = 0.5772156649015329


@dataclass(frozen=True)
class BacktestParams:
    initial_capital: float = 100_000.0
    position_size_pct: float = 0.05
    max_positions: int = 10
    stop_loss_pct: float = 0.05
    take_profit_pct: float = 0.15
    holding_period_days: int = 60       # calendar days, as in the original
    min_score: float = 0.4
    confirmation_days: int = 3
    cooldown_days: int = 5              # trading days before re-entering a symbol after an exit
    slippage_bps: float = 5.0           # per side
    commission: float = 0.0             # per order
    allow_shorts: bool = True
    risk_free_rate: float = 0.0        # for the Sharpe ratio; idle cash earns nothing in the simulation

    def validate(self) -> None:
        if self.initial_capital <= 0:
            raise ValueError("initial_capital must be positive")
        if not 0 < self.position_size_pct <= 1:
            raise ValueError("position_size_pct must be between 0 and 1")
        if self.max_positions < 1:
            raise ValueError("max_positions must be at least 1")
        if not 0 < self.stop_loss_pct < 1 or self.take_profit_pct <= 0:
            raise ValueError("stop_loss_pct must be between 0 and 1 and take_profit_pct positive")
        if self.confirmation_days < 1 or self.holding_period_days < 1:
            raise ValueError("confirmation_days and holding_period_days must be at least 1")


# --- statistics ------------------------------------------------------------

def sharpe_daily(returns: np.ndarray, risk_free_rate: float = 0.0) -> float | None:
    """Per-day Sharpe ratio (not annualised). None when it cannot be estimated."""
    r = np.asarray(returns, dtype=float)
    r = r[np.isfinite(r)]
    if len(r) < 20:
        return None
    excess = r - risk_free_rate / TRADING_DAYS
    sd = excess.std(ddof=1)
    if sd <= 1e-12:
        return None
    return float(excess.mean() / sd)


def annualise(sr_daily: float | None) -> float | None:
    return None if sr_daily is None else sr_daily * math.sqrt(TRADING_DAYS)


def probabilistic_sharpe(sr: float, n: int, skew: float, kurt: float, benchmark: float = 0.0) -> float:
    """Probability the true per-period Sharpe exceeds `benchmark` (Bailey & Lopez de Prado).
    `kurt` is ordinary kurtosis (3 for a normal distribution)."""
    denom = 1.0 - skew * sr + (kurt - 1.0) / 4.0 * sr * sr
    if n < 2 or denom <= 0:
        return float("nan")
    return float(stats.norm.cdf((sr - benchmark) * math.sqrt(n - 1) / math.sqrt(denom)))


def expected_max_sharpe(n_trials: int, variance: float) -> float:
    """Sharpe ratio expected from the best of `n_trials` strategies with no real edge."""
    if n_trials <= 1 or variance <= 0:
        return 0.0
    a = stats.norm.ppf(1.0 - 1.0 / n_trials)
    b = stats.norm.ppf(1.0 - 1.0 / (n_trials * math.e))
    return float(math.sqrt(variance) * ((1.0 - EULER_GAMMA) * a + EULER_GAMMA * b))


def deflated_sharpe(returns: np.ndarray, n_trials: int, trial_variance: float | None = None,
                    risk_free_rate: float = 0.0) -> dict[str, Any]:
    """Deflated Sharpe ratio: the chance the Sharpe is real after allowing for the number of
    variations tried and for fat-tailed returns. Values near 1 are reassuring; near 0.5 or
    below means the result is what luck alone would produce."""
    r = np.asarray(returns, dtype=float)
    r = r[np.isfinite(r)]
    sr = sharpe_daily(r, risk_free_rate)
    if sr is None:
        return {"deflated_sharpe": None, "prob_sharpe_positive": None, "benchmark_sharpe_annual": None,
                "n_trials": n_trials}
    n = len(r)
    skew = float(stats.skew(r))
    kurt = float(stats.kurtosis(r, fisher=False))
    variance = trial_variance if trial_variance is not None and trial_variance > 0 else 1.0 / (n - 1)
    sr0 = expected_max_sharpe(n_trials, variance)
    return {
        "deflated_sharpe": probabilistic_sharpe(sr, n, skew, kurt, sr0),
        "prob_sharpe_positive": probabilistic_sharpe(sr, n, skew, kurt, 0.0),
        "benchmark_sharpe_annual": annualise(sr0),
        "n_trials": n_trials,
    }


def max_drawdown(equity: np.ndarray) -> float:
    """Largest peak-to-trough fall as a negative fraction (for example -0.12)."""
    equity = np.asarray(equity, dtype=float)
    if len(equity) == 0:
        return 0.0
    peaks = np.maximum.accumulate(equity)
    return float((equity / peaks - 1.0).min())


# --- engine ----------------------------------------------------------------

class Backtester:
    """Simulates the scanner strategy over enriched daily bars keyed by symbol."""

    def __init__(self, frames: dict[str, pd.DataFrame],
                 signal_override: tuple[np.ndarray, np.ndarray] | None = None):
        """`signal_override` supplies (direction, score) matrices directly, shaped
        (trading days, symbols in sorted order); used to test the engine in isolation."""
        self._signal_override = signal_override
        frames = {s: df for s, df in frames.items() if len(df) > 0}
        if not frames:
            raise ValueError("No price data to backtest")
        self.symbols = sorted(frames)
        self.frames = frames
        self.calendar = pd.DatetimeIndex(sorted(set().union(*[set(df["date"]) for df in frames.values()])))
        self._prices = {field: self._matrix(field) for field in ("open", "high", "low", "close")}
        has_bar = np.isfinite(self._prices["close"])
        # Index of each symbol's final bar, to tell a holiday gap from data that has ended.
        self._last_bar = np.array([np.flatnonzero(has_bar[:, j])[-1] for j in range(len(self.symbols))])
        self._signal_cache: dict[tuple[float, int], tuple[np.ndarray, np.ndarray]] = {}

    def _matrix(self, field: str) -> np.ndarray:
        cols = [self.frames[s].set_index("date")[field].reindex(self.calendar).to_numpy(dtype=float)
                for s in self.symbols]
        return np.column_stack(cols)

    def _signals(self, min_score: float, confirmation_days: int) -> tuple[np.ndarray, np.ndarray]:
        """(direction, score) matrices: direction is +1/-1 where a confirmed signal exists, else 0."""
        if self._signal_override is not None:
            return self._signal_override
        key = (round(min_score, 6), confirmation_days)
        if key not in self._signal_cache:
            dirs, scores = [], []
            for s in self.symbols:
                sig = legacy_signals(self.frames[s], min_score, confirmation_days).set_index("date")
                direction = np.where(sig["confirmed"], np.where(sig["signal_type"] == "LONG", 1, -1), 0)
                dirs.append(pd.Series(direction, index=sig.index).reindex(self.calendar).fillna(0).to_numpy())
                scores.append(sig["signal_score"].reindex(self.calendar).fillna(0.0).to_numpy())
            self._signal_cache[key] = (np.column_stack(dirs).astype(int), np.column_stack(scores))
        return self._signal_cache[key]

    def run(self, params: BacktestParams, start: str | None = None, end: str | None = None,
            keep_trades: bool = True) -> dict[str, Any]:
        params.validate()
        lo = int(self.calendar.searchsorted(pd.Timestamp(start))) if start else 0
        hi = int(self.calendar.searchsorted(pd.Timestamp(end), side="right")) if end else len(self.calendar)
        if hi - lo < 2:
            raise ValueError("The backtest window holds fewer than two trading days")
        direction, score = self._signals(params.min_score, params.confirmation_days)
        o, h, l, c = (self._prices[f] for f in ("open", "high", "low", "close"))
        slip = params.slippage_bps / 10_000.0
        n_sym = len(self.symbols)

        cash = params.initial_capital
        positions: list[dict[str, Any]] = []
        trades: list[dict[str, Any]] = []
        pending: list[tuple[int, int, float]] = []
        cooldown_until = np.zeros(n_sym, dtype=int)
        last_close = np.full(n_sym, np.nan)
        equity_curve: list[float] = []
        exposure: list[float] = []
        prev_equity = params.initial_capital

        def close_position(pos: dict[str, Any], i: int, price: float, reason: str) -> None:
            nonlocal cash
            fill = price * (1 - slip) if pos["dir"] == 1 else price * (1 + slip)
            gross = pos["dir"] * (fill - pos["entry"]) * pos["shares"]
            cash += pos["collateral"] + gross - params.commission
            pnl = gross - 2 * params.commission
            entry_day = self.calendar[pos["entry_i"]]
            trades.append({
                "symbol": self.symbols[pos["sym"]], "direction": "LONG" if pos["dir"] == 1 else "SHORT",
                "entry_date": entry_day.strftime("%Y-%m-%d"), "exit_date": self.calendar[i].strftime("%Y-%m-%d"),
                "entry_price": round(pos["entry"], 4), "exit_price": round(fill, 4), "shares": pos["shares"],
                "pnl": pnl, "pnl_pct": 100.0 * pnl / pos["collateral"],
                "days_held": int((self.calendar[i] - entry_day).days), "exit_reason": reason,
            })
            cooldown_until[pos["sym"]] = i + 1 + params.cooldown_days

        for i in range(lo, hi):
            # 1. Enter positions decided at yesterday's close, at today's open.
            held = {p["sym"] for p in positions}
            for sym, d, _ in sorted(pending, key=lambda x: -x[2]):
                if len(positions) >= params.max_positions:
                    break
                px = o[i, sym]
                if sym in held or not np.isfinite(px) or px <= 0:
                    continue
                entry = px * (1 + slip) if d == 1 else px * (1 - slip)
                shares = int(prev_equity * params.position_size_pct // entry)
                collateral = shares * entry
                if shares < 1 or collateral + params.commission > cash:
                    continue
                cash -= collateral + params.commission
                positions.append({"sym": sym, "dir": d, "shares": shares, "entry": entry,
                                  "collateral": collateral, "entry_i": i})
                held.add(sym)
            pending = []

            # 2. Exits: stop, target, then time limit.
            still_open: list[dict[str, Any]] = []
            for pos in positions:
                sym, d = pos["sym"], pos["dir"]
                op, hp, lp, cp = o[i, sym], h[i, sym], l[i, sym], c[i, sym]
                if not np.isfinite(cp):
                    # No bar today. If the symbol's data has ended, or the test is ending, close at
                    # the last known price rather than leaving the position and its capital stranded.
                    if i > self._last_bar[sym] or i == hi - 1:
                        close_position(pos, i, float(last_close[sym]), "DATA_ENDED")
                    else:
                        still_open.append(pos)
                    continue
                stop = pos["entry"] * (1 - d * params.stop_loss_pct)
                target = pos["entry"] * (1 + d * params.take_profit_pct)
                carried = pos["entry_i"] < i
                exit_px, reason = None, ""
                # The open trades first: a gap past either level fills at the open. Only then do
                # the day's high and low matter, and if both levels are touched the stop is assumed
                # to come first (the cautious reading, since the order within the day is unknown).
                if d == 1:
                    if carried and op <= stop:
                        exit_px, reason = op, "STOP_LOSS_GAP"
                    elif carried and op >= target:
                        exit_px, reason = op, "TAKE_PROFIT_GAP"
                    elif lp <= stop:
                        exit_px, reason = stop, "STOP_LOSS"
                    elif hp >= target:
                        exit_px, reason = target, "TAKE_PROFIT"
                else:
                    if carried and op >= stop:
                        exit_px, reason = op, "STOP_LOSS_GAP"
                    elif carried and op <= target:
                        exit_px, reason = op, "TAKE_PROFIT_GAP"
                    elif hp >= stop:
                        exit_px, reason = stop, "STOP_LOSS"
                    elif lp <= target:
                        exit_px, reason = target, "TAKE_PROFIT"
                if exit_px is None:
                    held_days = (self.calendar[i] - self.calendar[pos["entry_i"]]).days
                    if held_days >= params.holding_period_days:
                        exit_px, reason = cp, "HOLDING_PERIOD"
                    elif i == hi - 1:
                        exit_px, reason = cp, "END_OF_TEST"
                if exit_px is None:
                    still_open.append(pos)
                else:
                    close_position(pos, i, exit_px, reason)
            positions = still_open

            # 3. Mark to market at the close.
            today_close = c[i]
            last_close = np.where(np.isfinite(today_close), today_close, last_close)
            invested = 0.0
            for pos in positions:
                mark = last_close[pos["sym"]]
                invested += pos["collateral"] + pos["dir"] * (mark - pos["entry"]) * pos["shares"]
            equity = cash + invested
            equity_curve.append(equity)
            exposure.append(sum(p["collateral"] for p in positions) / equity if equity > 0 else 0.0)
            prev_equity = equity

            # 4. Signals at today's close become tomorrow's orders.
            if i < hi - 1:
                held = {p["sym"] for p in positions}
                for sym in np.flatnonzero(direction[i] != 0):
                    d = int(direction[i, sym])
                    if sym in held or i + 1 < cooldown_until[sym] or (d == -1 and not params.allow_shorts):
                        continue
                    pending.append((int(sym), d, float(score[i, sym])))

        dates = self.calendar[lo:hi]
        equity_arr = np.array(equity_curve)
        result = self._metrics(params, dates, equity_arr, trades, float(np.mean(exposure)))
        result["benchmark"] = self._benchmark(lo, hi, params)
        result["equity_curve"] = [{"date": d.strftime("%Y-%m-%d"), "equity": round(float(e), 2)}
                                  for d, e in zip(dates, equity_arr)]
        result["trades"] = trades if keep_trades else []
        result["params"] = asdict(params)
        result["symbols"] = self.symbols
        return result

    def _metrics(self, params: BacktestParams, dates: pd.DatetimeIndex, equity: np.ndarray,
                 trades: list[dict[str, Any]], avg_exposure: float) -> dict[str, Any]:
        with_start = np.concatenate([[params.initial_capital], equity])
        returns = np.diff(with_start) / with_start[:-1]
        sr = sharpe_daily(returns, params.risk_free_rate)
        final = float(equity[-1])
        years = len(equity) / TRADING_DAYS
        wins = [t for t in trades if t["pnl"] > 0]
        losses = [t for t in trades if t["pnl"] <= 0]
        gross_win = sum(t["pnl"] for t in wins)
        gross_loss = -sum(t["pnl"] for t in losses)
        reasons: dict[str, int] = {}
        for t in trades:
            reasons[t["exit_reason"]] = reasons.get(t["exit_reason"], 0) + 1
        return {
            "start_date": dates[0].strftime("%Y-%m-%d"), "end_date": dates[-1].strftime("%Y-%m-%d"),
            "trading_days": int(len(equity)),
            "initial_capital": params.initial_capital, "final_capital": final,
            "total_return_pct": 100.0 * (final / params.initial_capital - 1.0),
            "cagr_pct": 100.0 * ((final / params.initial_capital) ** (1.0 / years) - 1.0) if years > 0 and final > 0 else None,
            "sharpe": annualise(sr), "sharpe_daily": sr,
            "max_drawdown_pct": 100.0 * max_drawdown(np.concatenate([[params.initial_capital], equity])),
            "num_trades": len(trades), "num_wins": len(wins), "num_losses": len(losses),
            "win_rate_pct": 100.0 * len(wins) / len(trades) if trades else None,
            "avg_win": gross_win / len(wins) if wins else None,
            "avg_loss": -gross_loss / len(losses) if losses else None,
            "profit_factor": gross_win / gross_loss if gross_loss > 0 else None,
            "avg_days_held": float(np.mean([t["days_held"] for t in trades])) if trades else None,
            "avg_exposure_pct": 100.0 * avg_exposure,
            "exit_reasons": reasons,
            "daily_returns": returns,
        }

    def _benchmark(self, lo: int, hi: int, params: BacktestParams) -> dict[str, Any]:
        """Buy and hold: equal dollars in every symbol at its first open in the window, never
        rebalanced. Money for a symbol with no bar yet waits in cash."""
        opens, closes = self._prices["open"][lo:hi], self._prices["close"][lo:hi]
        n_days, n_sym = closes.shape
        stake = params.initial_capital / n_sym
        slip = params.slippage_bps / 10_000.0
        equity = np.zeros(n_days)
        for j in range(n_sym):
            valid = np.flatnonzero(np.isfinite(opens[:, j]) & (opens[:, j] > 0))
            if not len(valid):
                equity += stake
                continue
            first = valid[0]
            shares = stake / (opens[first, j] * (1 + slip))
            marks = pd.Series(closes[:, j]).ffill().to_numpy()
            value = shares * marks
            value[:first] = stake
            equity += value
        with_start = np.concatenate([[params.initial_capital], equity])
        daily = np.diff(with_start) / with_start[:-1]
        return {
            "label": "Buy and hold: equal dollars in each symbol at the first open",
            "total_return_pct": 100.0 * (equity[-1] / params.initial_capital - 1.0),
            "sharpe": annualise(sharpe_daily(daily, params.risk_free_rate)),
            "max_drawdown_pct": 100.0 * max_drawdown(with_start),
            "equity": [round(float(e), 2) for e in equity],
        }


def run_backtest(frames: dict[str, pd.DataFrame], params: BacktestParams, start: str | None = None,
                 end: str | None = None, n_trials: int = 1) -> dict[str, Any]:
    """One backtest plus a Sharpe ratio adjusted for `n_trials` variations tried."""
    result = Backtester(frames).run(params, start, end)
    returns = result.pop("daily_returns")
    result["significance"] = deflated_sharpe(returns, max(int(n_trials), 1), None, params.risk_free_rate)
    result["notes"] = _notes(result)
    return _align_benchmark(result)


DEFAULT_GRID = {
    "stop_loss_pct": [0.04, 0.06, 0.08],
    "take_profit_pct": [0.10, 0.15, 0.20],
    "min_score": [0.4, 0.5],
}


def walk_forward(frames: dict[str, pd.DataFrame], base: BacktestParams, grid: dict[str, list[float]] | None = None,
                 train_days: int = 252, test_days: int = 63, start: str | None = None,
                 end: str | None = None) -> dict[str, Any]:
    """Choose parameters on one window, then trade the next window with them, and repeat.

    Only the out-of-sample windows count towards the reported result, which is the
    closest a backtest gets to showing how tuning would have worked in real time.
    """
    grid = grid or DEFAULT_GRID
    for name in grid:
        if not hasattr(base, name):
            raise ValueError(f"Unknown parameter in grid: {name}")
    combos = [dict(zip(grid, values)) for values in itertools.product(*grid.values())]
    bt = Backtester(frames)
    cal = bt.calendar
    lo = int(cal.searchsorted(pd.Timestamp(start))) if start else 0
    hi = int(cal.searchsorted(pd.Timestamp(end), side="right")) if end else len(cal)
    # Signals need about a year of bars to exist, so skip that warm-up before the first training window.
    lo = max(lo, 260)
    if hi - lo < train_days + test_days:
        raise ValueError(f"Not enough history for walk-forward: need {train_days + test_days} trading days "
                         f"after a 260-day warm-up, have {max(hi - lo, 0)}")
    folds = []
    oos_returns: list[np.ndarray] = []
    trial_variances: list[float] = []
    cursor = lo
    while cursor + train_days + test_days <= hi:
        train = (cal[cursor], cal[cursor + train_days - 1])
        test = (cal[cursor + train_days], cal[cursor + train_days + test_days - 1])
        scored = []
        for combo in combos:
            res = bt.run(replace(base, **combo), train[0], train[1], keep_trades=False)
            sr = res["sharpe_daily"] if res["num_trades"] >= 5 else None
            scored.append((sr, combo, res))
        usable = [s for s in scored if s[0] is not None]
        if len(usable) >= 2:
            trial_variances.append(float(np.var([s[0] for s in usable], ddof=1)))
        best = max(usable, key=lambda s: s[0]) if usable else None
        chosen = best[1] if best else {name: getattr(base, name) for name in grid}
        out = bt.run(replace(base, **chosen), test[0], test[1], keep_trades=False)
        oos_returns.append(out["daily_returns"])
        folds.append({
            "train_start": train[0].strftime("%Y-%m-%d"), "train_end": train[1].strftime("%Y-%m-%d"),
            "test_start": test[0].strftime("%Y-%m-%d"), "test_end": test[1].strftime("%Y-%m-%d"),
            "chosen": chosen, "had_enough_trades": best is not None,
            "train_sharpe": annualise(best[0]) if best else None,
            "test_sharpe": out["sharpe"], "test_return_pct": out["total_return_pct"],
            "test_trades": out["num_trades"],
        })
        cursor += test_days

    stitched = np.concatenate(oos_returns) if oos_returns else np.array([])
    equity = base.initial_capital * np.cumprod(1.0 + stitched)
    sr = sharpe_daily(stitched, base.risk_free_rate)
    train_sharpes = [f["train_sharpe"] for f in folds if f["train_sharpe"] is not None]
    test_sharpes = [f["test_sharpe"] for f in folds if f["test_sharpe"] is not None]
    variance = float(np.mean(trial_variances)) if trial_variances else None
    return {
        "folds": folds, "grid": grid, "combinations_tried": len(combos),
        "train_days": train_days, "test_days": test_days,
        "out_of_sample": {
            "total_return_pct": 100.0 * (equity[-1] / base.initial_capital - 1.0) if len(equity) else None,
            "sharpe": annualise(sr),
            "max_drawdown_pct": 100.0 * max_drawdown(np.concatenate([[base.initial_capital], equity])),
            "positive_folds": sum(1 for f in folds if f["test_return_pct"] > 0), "folds": len(folds),
            "trading_days": int(len(stitched)),
        },
        "avg_train_sharpe": float(np.mean(train_sharpes)) if train_sharpes else None,
        "avg_test_sharpe": float(np.mean(test_sharpes)) if test_sharpes else None,
        # The stitched returns come from windows the tuning never saw, so they are not "the best
        # of N" and need no penalty for the number of combinations: the plain probability applies.
        "significance": deflated_sharpe(stitched, 1, None, base.risk_free_rate),
        "in_sample_trial_variance": variance,
        "notes": [
            "Each test window is traded with parameters chosen only from the window before it.",
            "A training Sharpe well above the test Sharpe is the signature of over-fitting.",
            "Positions are closed at the end of each test window, which costs a little extra in slippage.",
            "The symbol list is today's, so failed or dropped companies are missing (survivorship bias).",
        ],
    }


def _align_benchmark(result: dict[str, Any]) -> dict[str, Any]:
    bench = result["benchmark"].pop("equity")
    for point, value in zip(result["equity_curve"], bench):
        point["benchmark"] = value
    return result


def _notes(result: dict[str, Any]) -> list[str]:
    notes = [
        "The symbol list is today's, so companies that failed or were dropped are missing "
        "(survivorship bias). That flatters the result.",
        "Signals at one day's close are traded at the next open; stops and targets fill at their "
        "price unless the stock gaps past them.",
    ]
    if result["num_trades"] < 30:
        notes.append(f"Only {result['num_trades']} trades. Too few to judge the strategy either way.")
    bench = result["benchmark"]
    if result["total_return_pct"] < bench["total_return_pct"]:
        notes.append("The strategy returned less than simply holding the same stocks.")
    sig = result["significance"]
    if sig.get("deflated_sharpe") is not None and sig["deflated_sharpe"] < 0.95:
        notes.append("The Sharpe ratio is not statistically distinguishable from luck at the usual 95% bar.")
    return notes
