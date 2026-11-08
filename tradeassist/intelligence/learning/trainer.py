"""Training on a scheduled day: fit the candidates, compare them, refit the winner through the cutoff (4.0, phase 2).

Only rows whose outcome had matured by the training session's close are used (clock.AsOf hands out nothing else).
They are split in time, every split purged so that no training label's window reaches into the rows after it. The
candidates are judged on `confirmations` validation windows that do not overlap: the latest one and the one before:

    [ fit ...................... | band ] [ window 2 ]                  <- the earlier test
    [ fit ...................................... | band ] [ window 1 ]  <- the latest test
    [ fit ............................................... | band ]      <- deploying

1. Every candidate family is fitted on the rows before each window (its band calibrated on the block just before the
   window) and judged on that window. The candidates are the champion's family, "no change" (so a promoted model can
   always be demoted again; for the trade model, the base rates) and the configuration's families.
2. A challenger replaces the champion's family only if it passes the promotion rule against it on every window
   (validation.py). The windows are disjoint, so a challenger that was merely lucky on the latest window must be
   lucky again on an independent one. Consecutive training days share most of their latest window, so passing on
   two training days in a row (the first design) was nearly the same test twice: over a nine-year run of weekly training
   days it let a lucky challenger through in more than nine runs out of ten (found in review; tests/test_v4_replay.py now measures the
   run-wide rate under the null).
3. The family that stays is refitted on everything matured, its band on the latest block, and deployed from the next
   prediction on. Weekly retraining therefore keeps the champion current even when nothing beats it.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable

import pandas as pd

from ..config import ReplayConfig, validation_window
from ..models import daily as D
from ..models import target_stop as TS
from ..replay.clock import AsOf
from . import validation as V

CAL_FRACTION = 0.2


@dataclass
class TrainingResult:
    task: str
    horizon: int
    decision: str                       # promoted | kept | insufficient_data
    family: str
    model: Any = None
    reason: str = ""
    candidates: dict[str, Any] = field(default_factory=dict)


def _sessions(rows: pd.DataFrame) -> list[pd.Timestamp]:
    return list(rows.index.get_level_values("session").unique().sort_values())


def _upto(rows: pd.DataFrame, last: pd.Timestamp) -> pd.DataFrame:
    return rows.loc[(slice(None, last), slice(None)), :]


def _between(rows: pd.DataFrame, first: pd.Timestamp, last: pd.Timestamp) -> pd.DataFrame:
    return rows.loc[(slice(first, last), slice(None)), :]


def _purged_last(view: AsOf, before: pd.Timestamp, h: int) -> pd.Timestamp | None:
    """The last as-of session whose h-session window ends no later than session `before`."""
    i = view.labels.pos[before] - h
    return view.panel.sessions[i] if i >= 0 else None


def _fit_cal(view: AsOf, rows: pd.DataFrame, h: int) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Split rows into an earlier block to fit on and a later block to calibrate on, purged between them."""
    sess = _sessions(rows)
    if len(sess) < 10:
        return rows, rows.iloc[0:0]
    c0 = sess[int(len(sess) * (1 - CAL_FRACTION))]
    last_fit = _purged_last(view, c0, h)
    fit = _upto(rows, last_fit) if last_fit is not None else rows.iloc[0:0]
    return fit, _between(rows, c0, sess[-1])


def _windows(view: AsOf, ex: pd.DataFrame, last: pd.Timestamp, n_val: int, count: int, h: int
             ) -> list[tuple[pd.DataFrame, pd.DataFrame]] | None:
    """(rows to train on, validation rows) for each of `count` disjoint windows of `n_val` sessions ending at `last`,
    latest first. None when the data does not reach far enough back for every window and its training rows."""
    out = []
    end_i = view.labels.pos[last]
    for j in range(count):
        hi = end_i - j * n_val
        lo = hi - n_val + 1
        if lo < 0:
            return None
        first, final = view.panel.sessions[lo], view.panel.sessions[hi]
        val = _between(ex, first, final)
        last_train = _purged_last(view, first, h)
        if last_train is None or not len(val):
            return None
        train = _upto(ex, last_train)
        if not len(train):
            return None
        out.append((train, val))
    return out


def _choose(champion_family: str, families: list[str], evals: list[dict[str, dict[str, Any]]],
            compare: Callable[[dict[str, Any], dict[str, Any], int], dict[str, Any]]) -> tuple[str | None, dict[str, Any]]:
    """Compare every challenger with the champion on every window; promote the one with the strongest weakest-window
    t among those that pass on all of them."""
    challengers = families[1:]
    candidates: dict[str, Any] = {champion_family: {"role": "champion", **V.summary(evals[0][champion_family])}}
    best, best_t = None, None
    for fam in challengers:
        per = [compare(ev[champion_family], ev[fam], len(challengers)) for ev in evals]
        passes = all(c["passes"] for c in per)
        worst = min(c["t"] for c in per)
        candidates[fam] = {"role": "challenger", **V.summary(evals[0][fam]), **per[0], "passes": passes,
                           "windows": [{"t": c["t"], "passes": c["passes"], "checks": c["checks"]} for c in per]}
        if passes and (best_t is None or worst > best_t):
            best, best_t = fam, worst
    return best, candidates


def _why_kept(champion_family: str, cutoff: str, candidates: dict[str, Any]) -> str:
    parts = []
    for f, c in candidates.items():
        if c["role"] != "challenger":
            continue
        failed = sorted({k for w in c.get("windows", []) for k, ok in w["checks"].items() if not ok})
        won = sum(1 for w in c.get("windows", []) if w["passes"])
        parts.append(f"{f}: failed {', '.join(failed)}" if failed else f"{f}: passed {won} of {len(c.get('windows', []))} windows")
    return f"no challenger promoted; {champion_family} refitted through {cutoff}. " + "; ".join(parts)


def _families(champion: str, baseline: str, configured: tuple[str, ...]) -> list[str]:
    """The champion first, then the baseline (so a promoted model can always lose its place again), then the rest."""
    out = [champion]
    for f in (baseline, *configured):
        if f not in out:
            out.append(f)
    return out


def _daily_model(family: str, h: int, cfg: ReplayConfig, fit: pd.DataFrame, cal: pd.DataFrame, cutoff: str) -> D.ReturnModel:
    m = D.make(family, h, cfg.interval, cfg.seed).fit_point(fit)
    m.fit_band(cal)
    m.trained_through = cutoff
    return m


def train_daily(view: AsOf, h: int, champion_family: str, cfg: ReplayConfig) -> TrainingResult:
    rule = cfg.promotion
    ex = view.daily_examples(h, cfg.train_window_sessions)
    sess = _sessions(ex)
    cutoff = view.session.isoformat()
    n_val = validation_window(rule, h)
    need = cfg.min_train_sessions + n_val * rule.confirmations
    if len(sess) < need:
        return TrainingResult("daily", h, "insufficient_data", champion_family, reason=f"{len(sess)} matured sessions; {need} needed")
    wins = _windows(view, ex, sess[-1], n_val, rule.confirmations, h)
    if wins is None:
        return TrainingResult("daily", h, "insufficient_data", champion_family, reason="not enough rows before the validation windows")
    families = _families(champion_family, "no_change", cfg.families)
    evals = []
    for train, val in wins:
        fit, cal = _fit_cal(view, train, h)
        evals.append({fam: V.evaluate_daily(_daily_model(fam, h, cfg, fit, cal, cutoff), val) for fam in families})
    best, candidates = _choose(champion_family, families, evals, lambda a, b, k: V.compare_daily(a, b, rule, h, cfg.interval, k))
    chosen = best or champion_family
    all_fit, all_cal = _fit_cal(view, ex, h)
    model = _daily_model(chosen, h, cfg, all_fit, all_cal, cutoff)
    if best:
        c = candidates[best]
        ts = ", ".join(f"{w['t']:.2f}" for w in c["windows"])
        reason = (f"{best} beat {champion_family} on {len(c['windows'])} separate validation windows (t = {ts}; {c['t_needed']:.2f} "
                  "needed on each) and passed every check")
        return TrainingResult("daily", h, "promoted", chosen, model, reason, candidates)
    return TrainingResult("daily", h, "kept", chosen, model, _why_kept(champion_family, cutoff, candidates), candidates)


def _trade_model(family: str, cfg: ReplayConfig, fit: pd.DataFrame, cal: pd.DataFrame, cutoff: str) -> TS.TradeModel:
    m = TS.make(family, cfg.trade.hold_sessions, cfg.seed).fit(fit)
    m.calibrate(cal)
    m.trained_through = cutoff
    return m


def train_trade(view: AsOf, champion_family: str, cfg: ReplayConfig) -> TrainingResult:
    rule = cfg.promotion
    hold = cfg.trade.hold_sessions
    ex = view.trade_examples(cfg.train_window_sessions)
    cutoff = view.session.isoformat()
    last = view.labels.matured_through(hold, view.ts)
    span = 0
    if last is not None and len(ex):
        span = view.labels.pos[last] - view.labels.pos[_sessions(ex)[0]] + 1
    n_val = validation_window(rule, hold)
    need = cfg.min_train_sessions + n_val * rule.confirmations
    if len(ex) < rule.trade_min_train_rows or span < need:
        return TrainingResult("trade", hold, "insufficient_data", champion_family,
                              reason=f"{len(ex)} matured setups over {span} sessions; at least {rule.trade_min_train_rows} setups "
                                     f"over {need} sessions needed")
    wins = _windows(view, ex, last, n_val, rule.confirmations, hold)
    if wins is None:
        return TrainingResult("trade", hold, "insufficient_data", champion_family, reason="not enough setups before the validation windows")
    families = _families(champion_family, "base_rate", cfg.trade_families)
    evals = []
    for train, val in wins:
        fit, cal = _fit_cal(view, train, hold)
        evals.append({fam: V.evaluate_trade(_trade_model(fam, cfg, fit, cal, cutoff), val, cfg.trade.min_edge) for fam in families})
    best, candidates = _choose(champion_family, families, evals,
                               lambda a, b, k: V.compare_trade(a, b, rule, hold, k, lambda d: view.labels.pos[d] // hold))
    chosen = best or champion_family
    all_fit, all_cal = _fit_cal(view, ex, hold)
    model = _trade_model(chosen, cfg, all_fit, all_cal, cutoff)
    if best:
        c = candidates[best]
        ts = ", ".join(f"{w['t']:.2f}" for w in c["windows"])
        return TrainingResult("trade", hold, "promoted", chosen, model,
                              f"{best} beat {champion_family}'s log loss on {len(c['windows'])} separate validation windows (t = {ts}; "
                              f"{c['t_needed']:.2f} needed on each), without taking worse trades", candidates)
    return TrainingResult("trade", hold, "kept", chosen, model, _why_kept(champion_family, cutoff, candidates), candidates)


__all__ = ["TrainingResult", "train_daily", "train_trade"]
