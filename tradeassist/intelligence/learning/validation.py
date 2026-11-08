"""Is a challenger better than the champion, by more than chance? (4.0, phase 2)

Both are judged on the same validation window, which neither trained on. The comparison is made per date, not per
row: on one day a hundred stocks move with the same market, so they are one observation, not a hundred. Five-day
windows overlap from one day to the next, so only every fifth date is used: overlapping dates are not independent,
and a correction for the overlap (Newey-West) is overconfident with the few dozen dates a window holds. The test is
Student's t on those dates, one-sided, at alpha divided among the day's challengers. The rule itself is
config.PromotionRule, fixed before any replay. (The whole-run scorecards, with thousands of dates, use Newey-West.)
"""

from __future__ import annotations

import math
from typing import Any

import numpy as np
import pandas as pd

from ..config import PromotionRule
from ..models.baseline import ReturnModel
from ..models.target_stop import TradeModel, log_loss


def newey_west_t(x: np.ndarray, lags: int) -> float:
    """t-statistic of the mean of x with heteroskedasticity- and autocorrelation-consistent errors."""
    x = np.asarray(x, dtype=float)
    x = x[np.isfinite(x)]
    n = len(x)
    if n < 3:
        return 0.0
    d = x - x.mean()
    var = float(d @ d) / n
    for k in range(1, min(lags, n - 1) + 1):
        w = 1 - k / (lags + 1)
        var += 2 * w * float(d[k:] @ d[:-k]) / n
    if var <= 0:
        return 0.0
    return float(x.mean() / math.sqrt(var / n))


def evaluate_daily(model: ReturnModel, rows: pd.DataFrame) -> dict[str, Any]:
    h = model.horizon
    y = rows[f"y{h}"].to_numpy(float)
    p = model.predict(rows)
    err = np.abs(y - p["mu"])
    covered = (y >= p["low"]) & (y <= p["high"])
    sized = np.abs(p["mu"]) > 0.25 * p["sigma"]
    hits = np.sign(p["mu"][sized]) == np.sign(y[sized])
    dates = rows.index.get_level_values("session")
    return {"err": err, "dates": dates, "rows": int(len(y)), "mae": float(err.mean()) if len(err) else None,
            "nochange_mae": float(np.abs(y).mean()) if len(y) else None,
            "coverage": float(covered.mean()) if len(y) else None,
            "tail": float(np.quantile(err, 0.95)) if len(err) else None,
            "directional_accuracy": float(hits.mean()) if len(hits) else None, "directional_cases": int(sized.sum())}


def threshold(rule: PromotionRule, challengers: int, dates: int) -> float:
    """The t a challenger must exceed on `dates` independent dates: one-sided at alpha, divided among the day's
    challengers (Bonferroni)."""
    from scipy.stats import t as student
    return float(student.ppf(1 - rule.alpha / max(1, challengers), max(1, dates - 1)))


def independent_gain(gain: pd.Series, horizon: int) -> np.ndarray:
    """Every h-th date counting back from the latest, so no two windows overlap."""
    g = gain.sort_index().to_numpy()
    return g[::-1][::max(1, horizon)][::-1]


def plain_t(x: np.ndarray) -> float:
    x = x[np.isfinite(x)]
    if len(x) < 3 or x.std(ddof=1) <= 0:
        return 0.0
    return float(x.mean() / (x.std(ddof=1) / math.sqrt(len(x))))


def compare_daily(champ: dict[str, Any], chal: dict[str, Any], rule: PromotionRule, horizon: int, interval: float,
                  challengers: int = 1) -> dict[str, Any]:
    """Whether the challenger passes every part of the rule against the champion on the same rows."""
    gain = pd.Series(champ["err"] - chal["err"]).groupby(np.asarray(champ["dates"])).mean()
    ind = independent_gain(gain, horizon)
    t = plain_t(ind)
    need = threshold(rule, challengers, len(ind))
    checks = {
        "enough_dates": len(ind) >= rule.min_validation_dates,
        "enough_rows": chal["rows"] >= rule.min_validation_rows,
        "better_than_chance": t > need,
        "band_holds": chal["coverage"] is not None and abs(chal["coverage"] - interval) <= rule.coverage_tolerance,
        "tail_not_worse": chal["tail"] is not None and champ["tail"] is not None
                          and chal["tail"] <= champ["tail"] * (1 + rule.tail_tolerance),
    }
    checks = {k: bool(v) for k, v in checks.items()}
    return {"passes": all(checks.values()), "checks": checks, "t": t, "t_needed": need,
            "mae_gain": float(gain.mean()) if len(gain) else None, "dates": int(len(gain)), "independent_dates": int(len(ind))}


def summary(ev: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in ev.items() if k not in ("err", "dates", "ll", "net")}


def evaluate_trade(model: TradeModel, rows: pd.DataFrame, min_edge: float) -> dict[str, Any]:
    p = model.predict(rows)
    y = rows["y"].to_numpy()
    ll = log_loss(p, y) if model.fitted else np.full(len(y), math.log(3))
    take = np.nan_to_num(p["expected"], nan=-1.0) >= min_edge if model.fitted else np.zeros(len(y), dtype=bool)
    net = rows["net"].to_numpy(float)
    brier = None
    if model.fitted and len(y):
        onehot = np.column_stack([(y == c).astype(float) for c in ("target", "stop", "timeout")])
        probs = np.column_stack([p["p_target"], p["p_stop"], p["p_timeout"]])
        brier = float(((probs - onehot) ** 2).sum(axis=1).mean())
    return {"ll": ll, "dates": rows.index.get_level_values("session"), "rows": int(len(y)),
            "log_loss": float(ll.mean()) if len(ll) else None, "brier": brier, "taken": int(take.sum()),
            "taken_mean_net": float(net[take].mean()) if take.any() else 0.0, "all_mean_net": float(net.mean()) if len(net) else None}


def compare_trade(champ: dict[str, Any], chal: dict[str, Any], rule: PromotionRule, hold: int, challengers: int = 1,
                  block_of: Any = None) -> dict[str, Any]:
    """Setups are sparse and a trade lasts `hold` sessions, so the observations are blocks of `hold` sessions (the
    mean gain of the setups in each), not single dates: trades set up on neighbouring days overlap in time.
    `block_of(session)` numbers the blocks."""
    by_date = pd.Series(champ["ll"] - chal["ll"]).groupby(np.asarray(champ["dates"])).mean()
    if block_of is not None and len(by_date):
        blocks = by_date.groupby([block_of(d) for d in by_date.index]).mean().to_numpy()
    else:
        blocks = by_date.to_numpy()
    t = plain_t(blocks)
    need = threshold(rule, challengers, len(blocks))
    checks = {
        "enough_rows": chal["rows"] >= rule.trade_min_validation_rows,
        "enough_dates": len(blocks) >= rule.min_validation_dates,
        "better_than_chance": t > need,
        "trades_not_worse": chal["taken_mean_net"] >= champ["taken_mean_net"],
    }
    checks = {k: bool(v) for k, v in checks.items()}
    return {"passes": all(checks.values()), "checks": checks, "t": t, "t_needed": need,
            "ll_gain": float(by_date.mean()) if len(by_date) else None, "dates": int(len(by_date)), "independent_dates": int(len(blocks))}


__all__ = ["threshold", "independent_gain", "plain_t", "newey_west_t", "evaluate_daily", "compare_daily", "evaluate_trade", "compare_trade", "summary"]
