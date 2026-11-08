"""Training on a scheduled day: fit the candidates, compare them, refit the winner through the cutoff (4.0, phase 2).

Only rows whose outcome had matured by the training session's close are used (clock.AsOf hands out nothing else).
They are split in time, each split purged so that no training label's window reaches into the rows after it:

    [ fit ............................ | calibrate the band | ] [ validation window ]   <- choosing
    [ fit ............................................ | calibrate the band ]           <- deploying

1. Every allowed family, the champion's included, is fitted on the same early rows, its band calibrated on the next
   block, and judged on the validation window (the last `validation_sessions` matured sessions).
2. A challenger replaces the champion's family only if it passes the promotion rule against the champion's family
   on that window (validation.py). Otherwise the champion's family stays.
3. The family that stays is refitted on everything matured, its band on the latest block, and deployed from the next
   prediction on. Weekly retraining therefore keeps the champion current even when nothing beats it.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

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
    streaks: dict[str, int] = field(default_factory=dict)   # training days in a row each challenger has passed


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


def _daily_model(family: str, h: int, cfg: ReplayConfig, fit: pd.DataFrame, cal: pd.DataFrame, cutoff: str) -> D.ReturnModel:
    m = D.make(family, h, cfg.interval, cfg.seed).fit_point(fit)
    m.fit_band(cal)
    m.trained_through = cutoff
    return m


def _choose(champion_family: str, families: list[str], evals: dict[str, dict[str, Any]], compare, streaks: dict[str, int],
            confirmations: int) -> tuple[str | None, dict[str, Any], dict[str, int]]:
    """Compare every challenger with the champion; promote the strongest one that has now passed on `confirmations`
    training days running. Returns (promoted family or None, the candidates' record, the new pass streaks)."""
    champ = evals[champion_family]
    candidates: dict[str, Any] = {champion_family: {"role": "champion", **V.summary(champ)}}
    challengers = families[1:]
    new_streaks: dict[str, int] = {}
    best, best_t = None, None
    for fam in challengers:
        cmp = compare(champ, evals[fam], len(challengers))
        streak = streaks.get(fam, 0) + 1 if cmp["passes"] else 0
        new_streaks[fam] = streak
        candidates[fam] = {"role": "challenger", **V.summary(evals[fam]), **cmp, "streak": streak}
        if cmp["passes"] and streak >= confirmations and (best_t is None or cmp["t"] > best_t):
            best, best_t = fam, cmp["t"]
    if best:
        new_streaks = {}
    return best, candidates, new_streaks


def _why_kept(champion_family: str, cutoff: str, candidates: dict[str, Any], confirmations: int) -> str:
    parts = []
    for f, c in candidates.items():
        if c["role"] != "challenger":
            continue
        failed = [k for k, ok in c.get("checks", {}).items() if not ok]
        if failed:
            parts.append(f"{f}: failed {', '.join(failed)}")
        else:
            parts.append(f"{f}: passed ({c['streak']} of {confirmations} days in a row needed)")
    return f"no challenger promoted; {champion_family} refitted through {cutoff}. " + "; ".join(parts)


def train_daily(view: AsOf, h: int, champion_family: str, cfg: ReplayConfig, streaks: dict[str, int] | None = None) -> TrainingResult:
    rule = cfg.promotion
    streaks = dict(streaks or {})
    ex = view.daily_examples(h, cfg.train_window_sessions)
    sess = _sessions(ex)
    cutoff = view.session.isoformat()
    n_val = validation_window(rule, h)
    if len(sess) < cfg.min_train_sessions + n_val:
        return TrainingResult("daily", h, "insufficient_data", champion_family,
                              reason=f"{len(sess)} matured sessions; {cfg.min_train_sessions + n_val} needed", streaks=streaks)
    v0 = sess[-n_val]
    val = _between(ex, v0, sess[-1])
    last_train = _purged_last(view, v0, h)
    if last_train is None:
        return TrainingResult("daily", h, "insufficient_data", champion_family, reason="no rows before the validation window",
                              streaks=streaks)
    train = _upto(ex, last_train)
    fit, cal = _fit_cal(view, train, h)
    families = [champion_family] + [f for f in cfg.families if f != champion_family]
    evals = {fam: V.evaluate_daily(_daily_model(fam, h, cfg, fit, cal, cutoff), val) for fam in families}
    best, candidates, streaks = _choose(champion_family, families, evals,
                                        lambda a, b, k: V.compare_daily(a, b, rule, h, cfg.interval, k), streaks, rule.confirmations)
    chosen = best or champion_family
    all_fit, all_cal = _fit_cal(view, ex, h)
    model = _daily_model(chosen, h, cfg, all_fit, all_cal, cutoff)
    if best:
        c = candidates[best]
        reason = (f"{best} beat {champion_family} on {c['dates']} validation dates (t = {c['t']:.2f}, {c['t_needed']:.2f} needed) "
                  f"on {c['streak']} training days running, and passed every check")
        return TrainingResult("daily", h, "promoted", chosen, model, reason, candidates, streaks)
    return TrainingResult("daily", h, "kept", chosen, model, _why_kept(champion_family, cutoff, candidates, rule.confirmations),
                          candidates, streaks)


def _trade_model(family: str, cfg: ReplayConfig, fit: pd.DataFrame, cal: pd.DataFrame, cutoff: str) -> TS.TradeModel:
    m = TS.make(family, cfg.trade.hold_sessions, cfg.seed).fit(fit)
    m.calibrate(cal)
    m.trained_through = cutoff
    return m


def train_trade(view: AsOf, champion_family: str, cfg: ReplayConfig, streaks: dict[str, int] | None = None) -> TrainingResult:
    rule = cfg.promotion
    streaks = dict(streaks or {})
    hold = cfg.trade.hold_sessions
    ex = view.trade_examples(cfg.train_window_sessions)
    cutoff = view.session.isoformat()
    last = view.labels.matured_through(hold, view.ts)
    span = 0
    if last is not None and len(ex):
        span = view.labels.pos[last] - view.labels.pos[_sessions(ex)[0]] + 1
    n_val = validation_window(rule, hold)
    need = cfg.min_train_sessions + n_val
    if len(ex) < rule.trade_min_train_rows or span < need:
        return TrainingResult("trade", hold, "insufficient_data", champion_family,
                              reason=f"{len(ex)} matured setups over {span} sessions; at least {rule.trade_min_train_rows} setups "
                                     f"over {need} sessions needed", streaks=streaks)
    v0 = view.panel.sessions[view.labels.pos[last] - n_val + 1]
    val = _between(ex, v0, last)
    last_train = _purged_last(view, v0, hold)
    if last_train is None:
        return TrainingResult("trade", hold, "insufficient_data", champion_family, reason="no rows before the validation window",
                              streaks=streaks)
    train = _upto(ex, last_train)
    fit, cal = _fit_cal(view, train, hold)
    families = [champion_family] + [f for f in cfg.trade_families if f != champion_family]
    evals = {fam: V.evaluate_trade(_trade_model(fam, cfg, fit, cal, cutoff), val, cfg.trade.min_edge) for fam in families}
    best, candidates, streaks = _choose(champion_family, families, evals,
                                        lambda a, b, k: V.compare_trade(a, b, rule, hold, k, lambda d: view.labels.pos[d] // hold),
                                        streaks, rule.confirmations)
    chosen = best or champion_family
    all_fit, all_cal = _fit_cal(view, ex, hold)
    model = _trade_model(chosen, cfg, all_fit, all_cal, cutoff)
    if best:
        c = candidates[best]
        return TrainingResult("trade", hold, "promoted", chosen, model,
                              f"{best} beat {champion_family}'s log loss on {c['dates']} dates (t = {c['t']:.2f}, {c['t_needed']:.2f} "
                              f"needed) on {c['streak']} training days running, without taking worse trades", candidates, streaks)
    return TrainingResult("trade", hold, "kept", chosen, model, _why_kept(champion_family, cutoff, candidates, rule.confirmations),
                          candidates, streaks)


__all__ = ["TrainingResult", "train_daily", "train_trade"]
