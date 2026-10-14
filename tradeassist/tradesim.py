"""Simulate one trade (2.7): what a stock trade with a stop and targets can make or lose, and how often.

The stock is walked forward one trading day at a time on many paths, from the entry price, under the same
lognormal the rest of the app uses for option-implied figures: centred on the forward (the carry the option
chain implies) at the at-the-money implied volatility of the expiration nearest after the horizon. Without
usable option prices, the stock's own volatility over the last 60 finished days is used instead, stepped in
trading days, and the result says so.

The trade's own rules are applied on every path: out at the stop or at the target, whichever the price
touches first, otherwise out at the close on the horizon date. A touch between two closes is found with the
Brownian-bridge probability. When both the stop and a target could have been touched within one day, that
day is walked again in 32 smaller steps (a bridge between its two closes) to see which came first, so the
order is not guessed. Fills are at the level itself; a gap through the stop costs more, which is not added.

A bought call (for a long) or put (for a short) is walked on the same random numbers at its own implied
volatility and carry, the ones its price implies, and sold on the same stock rules at its model value at the
middle of the day the level is touched (at the close on the horizon date). Walking it at the at-the-money
volatility instead would let the skew alone look like a profit on out-of-the-money calls and a loss on puts.

What this is not: a forecast. The model has no view on direction, so averaged over every path a trade
comes out close to break-even: what is left is the carry in the forward price (interest less expected
dividends), not an edge. What it shows is the spread of results and how often each exit happens if the
market is as uncertain as option prices say. Any edge is whatever makes the target likelier than that, and
that is the trader's view, not the model's. Paths and seeds are fixed, so the same inputs give the same
figures.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, replace
from datetime import date, datetime
from typing import Any, Sequence

import numpy as np

from .optionmath import NY
from .simulate import MULTIPLIER, Leg, eval_moment, leg_value, trading_days, years_between

PATHS = 20_000
SEED = 20_270
REFINE_SEED = 20_271
REFINE_STEPS = 32
AMBIGUOUS = 1e-9            # a touch chance below this, for the stop or a target, is not an open question
SAMPLE_PATHS = 24
REALIZED_DAYS = 60
LEVEL_RANGE = (0.05, 20.0)  # stop and targets between a twentieth of the entry and twenty times it
PLANS = {"target_1": "All out at target 1", "target_2": "All out at target 2",
         "split": "Half out at target 1, the rest at target 2"}
PCTS = (5, 10, 25, 50, 75, 90, 95)


class TradeSimError(ValueError):
    pass


@dataclass(frozen=True)
class Plan:
    direction: int            # +1 long, -1 short
    entry: float
    stop: float
    target_1: float
    target_2: float
    shares: int
    plan: str = "target_1"

    @property
    def targets(self) -> list[float]:
        return {"target_1": [self.target_1], "target_2": [self.target_2]}.get(self.plan, [self.target_1, self.target_2])

    def check(self) -> None:
        d = self.direction
        if d not in (1, -1):
            raise TradeSimError("direction must be LONG or SHORT")
        if self.plan not in PLANS:
            raise TradeSimError(f"The exit plan must be one of {', '.join(PLANS)}")
        if not (self.entry > 0 and math.isfinite(self.entry)):
            raise TradeSimError("The entry must be above zero")
        lo, hi = LEVEL_RANGE[0] * self.entry, LEVEL_RANGE[1] * self.entry
        used = [("The stop", self.stop), ("Target 1", self.target_1)] + ([("Target 2", self.target_2)] if self.plan != "target_1" else [])
        for name, v in used:
            if not (math.isfinite(v) and lo <= v <= hi):
                raise TradeSimError(f"{name} (${v:,.2f}) must be between a twentieth of the entry and twenty times it "
                                    f"(${lo:,.2f} to ${hi:,.2f})")
        if d * (self.entry - self.stop) <= 0:
            raise TradeSimError(f"The stop must be {'below' if d > 0 else 'above'} the entry for a "
                                f"{'long' if d > 0 else 'short'}")
        if d * (self.target_1 - self.entry) <= 0:
            raise TradeSimError(f"Target 1 must be {'above' if d > 0 else 'below'} the entry")
        if self.plan != "target_1" and d * (self.target_2 - self.target_1) <= 0:
            raise TradeSimError(f"Target 2 must be beyond target 1 for the plan \"{PLANS[self.plan]}\"")
        if self.shares < 0:
            raise TradeSimError("Shares cannot be negative")


def part_sizes(plan: str, units: int) -> list[int]:
    """Whole shares or contracts per exit: all in one, or split with the odd one out at target 1."""
    if plan != "split":
        return [units]
    return [units - units // 2, units // 2]


def _touch_chance(x0: np.ndarray, x1: np.ndarray, level: float, above: bool, var: float) -> np.ndarray:
    """The chance a log-price path from x0 to x1 over a step of variance `var` touched `level`: 1 when it ends
    beyond it, otherwise the Brownian-bridge probability exp(-2 (b - x0)(b - x1) / var)."""
    gap0, gap1 = (level - x0, level - x1) if above else (x0 - level, x1 - level)
    beyond = gap1 <= 0
    with np.errstate(over="ignore", invalid="ignore", under="ignore"):
        p = np.exp(-2.0 * np.clip(gap0, 0, None) * np.clip(gap1, 0, None) / var) if var > 0 else np.zeros_like(x0)
    return np.where(beyond | (gap0 <= 0), 1.0, p)


def _first_touches(x0: np.ndarray, x1: np.ndarray, var: float, stop: tuple[float, bool],
                   targets: list[tuple[float, bool]], rng: np.random.Generator,
                   m: int = REFINE_STEPS) -> tuple[np.ndarray, list[np.ndarray]]:
    """Walk one day again in `m` bridge steps between its two closes, and return the first small step in which
    the stop and each target was touched (`m` when it was not). The targets share one uniform per small step,
    so a farther target is never touched before a nearer one."""
    n = len(x0)
    sd = math.sqrt(var / m)
    walk_ = np.cumsum(rng.standard_normal((n, m)) * sd, axis=1)
    frac = np.arange(1, m + 1) / m
    bridge = x0[:, None] + walk_ - frac * walk_[:, -1:] + frac * (x1 - x0)[:, None]
    prev = np.concatenate([x0[:, None], bridge[:, :-1]], axis=1)
    u_stop, u_tgt = rng.random((n, m)), rng.random((n, m))

    def first(level: float, above: bool, u: np.ndarray) -> np.ndarray:
        hit = u < _touch_chance(prev, bridge, level, above, var / m)
        return np.where(hit.any(axis=1), hit.argmax(axis=1), m)
    return first(*stop, u_stop), [first(lv, ab, u_tgt) for lv, ab in targets]


def walk(plan: Plan, times: Sequence[float], vol: float, growth: float, paths: int = PATHS,
         seed: int = SEED) -> dict[str, np.ndarray]:
    """Run the paths. `times` are the years from now to each step's close (increasing). Returns, per exit part
    (two for the split plan, one otherwise) and path, the exit price, the step it happened in and how it
    ended (1 target, -1 stop, 0 horizon), and the closes of every path, for the fan."""
    plan.check()
    if not len(times) or vol is None or not math.isfinite(vol) or vol <= 0:
        raise TradeSimError("A positive volatility and at least one trading day are needed")
    rng, refine = np.random.default_rng(seed), np.random.default_rng(REFINE_SEED)
    n = len(times)
    dts = np.maximum(np.diff(np.concatenate([[0.0], np.asarray(times, dtype=float)])), 1e-6)
    d = plan.direction
    x = np.full(paths, math.log(plan.entry))
    closes = np.empty((n, paths))
    stop = (math.log(plan.stop), d < 0)
    targets = plan.targets
    ltargets = [(math.log(t), d > 0) for t in targets]
    parts = len(targets)
    exit_px = np.full((parts, paths), np.nan)
    exit_step = np.full((parts, paths), -1, dtype=int)
    exit_kind = np.zeros((parts, paths), dtype=int)
    open_ = np.ones((parts, paths), dtype=bool)
    for k in range(n):
        var = vol * vol * dts[k]
        x1 = x + (growth - 0.5 * vol * vol) * dts[k] + math.sqrt(var) * rng.standard_normal(paths)
        u_stop, u_tgt = rng.random(paths), rng.random(paths)
        live = open_.any(axis=0)
        p_stop = _touch_chance(x, x1, *stop, var)
        p_tgt = [_touch_chance(x, x1, lv, ab, var) for lv, ab in ltargets]
        stop_hit = u_stop < p_stop
        tgt_hit = [u_tgt < p for p in p_tgt]
        stop_first = [np.ones(paths, dtype=bool) for _ in targets]       # when both happen, which came first
        p_open = np.max(np.stack([np.where(open_[j], p, 0.0) for j, p in enumerate(p_tgt)]), axis=0)
        both = live & (p_stop > AMBIGUOUS) & (p_open > AMBIGUOUS)
        if both.any():
            idx = np.flatnonzero(both)
            t_stop, t_tgts = _first_touches(x[idx], x1[idx], var, stop, ltargets, refine)
            stop_hit[idx] = t_stop < REFINE_STEPS
            coin = refine.random(len(idx)) < 0.5                       # the same small step: either first
            for j, t_j in enumerate(t_tgts):
                tgt_hit[j][idx] = t_j < REFINE_STEPS
                stop_first[j][idx] = (t_stop < t_j) | ((t_stop == t_j) & coin)
        for j in range(parts):
            lv = open_[j]
            stop_now = lv & stop_hit & (~tgt_hit[j] | stop_first[j])
            tgt_now = lv & tgt_hit[j] & ~stop_now
            exit_px[j, stop_now], exit_kind[j, stop_now], exit_step[j, stop_now] = plan.stop, -1, k
            exit_px[j, tgt_now], exit_kind[j, tgt_now], exit_step[j, tgt_now] = targets[j], 1, k
            open_[j] &= ~(stop_now | tgt_now)
        x = x1
        closes[k] = np.exp(x1)
    last = np.exp(x)
    for j in range(parts):
        left = open_[j]
        exit_px[j, left], exit_kind[j, left], exit_step[j, left] = last[left], 0, n - 1
    return {"exit_px": exit_px, "exit_step": exit_step, "exit_kind": exit_kind, "closes": closes}


def share_pnl(plan: Plan, w: dict[str, np.ndarray]) -> np.ndarray:
    """Dollars made or lost on the shares, per path: whole shares in each exit part."""
    sizes = np.array(part_sizes(plan.plan, plan.shares), dtype=float)[:, None]
    return (plan.direction * sizes * (w["exit_px"] - plan.entry)).sum(axis=0)


def exit_moments(now: datetime, moments: Sequence[datetime]) -> tuple[list[datetime], list[datetime]]:
    """For each step, the moment a level touched during it is valued at (the middle of the step) and its close."""
    starts = [now] + list(moments[:-1])
    return [a + (b - a) / 2 for a, b in zip(starts, moments)], list(moments)


def contract_pnl(leg: Leg, plan: str, now: datetime, moments: Sequence[datetime], w: dict[str, np.ndarray],
                 rate: float) -> np.ndarray:
    """Dollars made or lost on the contract, per path: each part of it sold when the stock exits, at its model
    value then (the middle of the day for a stop or target, the close on the horizon date)."""
    mids, closes = exit_moments(now, moments)
    sizes = part_sizes(plan, leg.contracts)
    total = np.zeros(w["exit_px"].shape[1])
    for j, size in enumerate(sizes):
        if not size:
            continue
        value = np.zeros_like(total)
        for k in np.unique(w["exit_step"][j]):
            for intraday in (True, False):
                m = (w["exit_step"][j] == k) & ((w["exit_kind"][j] != 0) == intraday)
                if m.any():
                    value[m] = leg_value(leg, w["exit_px"][j, m], (mids if intraday else closes)[int(k)], rate)
        total += (value - leg.entry) * size * MULTIPLIER
    return total


EXIT_WORDS = {"target": "Reached the target first", "stop": "Stopped out", "horizon": "Neither by the horizon date",
              "both": "Both targets reached", "t1_stop": "Target 1, then the rest stopped",
              "t1_horizon": "Target 1, the rest held to the horizon"}


def exit_labels(kind: np.ndarray) -> np.ndarray:
    """One label per path for how the whole trade ended."""
    if kind.shape[0] == 1:
        k = kind[0]
        return np.where(k == 1, "target", np.where(k == -1, "stop", "horizon"))
    a, b = kind
    return np.select([(a == 1) & (b == 1), (a == 1) & (b == -1), (a == 1) & (b == 0), a == -1],
                     ["both", "t1_stop", "t1_horizon", "stop"], default="horizon")


def summary(pnl: np.ndarray, labels: np.ndarray, bins: int = 30) -> dict[str, Any]:
    """Percentiles, chances, the average with its standard error, a histogram and the result by exit."""
    q = np.percentile(pnl, PCTS)
    lo, hi = float(pnl.min()), float(pnl.max())
    if hi - lo < 1e-9:
        lo, hi = lo - 1.0, hi + 1.0
    counts, edges = np.histogram(pnl, bins=bins, range=(lo, hi))
    by_exit = []
    for key, words in EXIT_WORDS.items():
        m = labels == key
        if m.any():
            by_exit.append({"key": key, "label": words, "share": float(m.mean()), "average": float(pnl[m].mean()),
                            "low": float(pnl[m].min()), "high": float(pnl[m].max())})
    return {"mean": float(pnl.mean()), "standard_error": float(pnl.std(ddof=1) / math.sqrt(len(pnl))),
            **{f"p{p}": float(v) for p, v in zip(PCTS, q)},
            "chance_profit": float((pnl > 0.005).mean()), "chance_loss": float((pnl < -0.005).mean()),
            "worst": lo, "best": hi,
            "histogram": {"edges": [float(e) for e in edges], "counts": [int(c) for c in counts]}, "by_exit": by_exit}


def fan(closes: np.ndarray, days: Sequence[date]) -> list[dict[str, Any]]:
    """The stock's price on each day: 10th, 25th, 50th, 75th and 90th percentile across paths."""
    q = np.percentile(closes, (10, 25, 50, 75, 90), axis=1)
    return [{"date": d.isoformat(), "p10": float(q[0, i]), "p25": float(q[1, i]), "p50": float(q[2, i]),
             "p75": float(q[3, i]), "p90": float(q[4, i])} for i, d in enumerate(days)]


def samples(plan: Plan, w: dict[str, np.ndarray], labels: np.ndarray, days: Sequence[date],
            count: int = SAMPLE_PATHS) -> list[dict[str, Any]]:
    """A few paths to draw, each up to the day it ended (the last part's exit), with how it ended."""
    last_part = np.argmax(w["exit_step"], axis=0)
    if w["exit_step"].shape[0] > 1:
        last_part = np.where(w["exit_step"][0] == w["exit_step"][1], 1, last_part)
    out = []
    for i in range(min(count, w["closes"].shape[1])):
        j = int(last_part[i])
        end = int(w["exit_step"][j, i])
        pts = [plan.entry] + [float(v) for v in w["closes"][: end + 1, i]]
        if w["exit_kind"][j, i] != 0:
            pts[-1] = float(w["exit_px"][j, i])           # out at the level, not at that day's close
        out.append({"prices": pts, "ended": str(labels[i]), "exit_day": days[end].isoformat()})
    return out


def realized_vol(closes: np.ndarray, days: int = REALIZED_DAYS) -> float | None:
    """Annualised close-to-close volatility over the last `days` finished days (per trading year)."""
    c = np.asarray(closes, dtype=float)[-(days + 1):]
    if len(c) < 21:
        return None
    r = np.diff(np.log(c))
    r = r[np.isfinite(r)]
    v = float(r.std(ddof=1) * math.sqrt(252)) if len(r) > 1 else None
    return v if v and math.isfinite(v) and v > 0 else None


def schedule(today: date, horizon_day: date, now: datetime) -> tuple[list[date], list[datetime], list[float]]:
    """The trading days after today through the horizon date, their closes, and years from now to each."""
    days = [d for d in trading_days(today, horizon_day) if d > today]
    moments = [eval_moment(d, now) for d in days]
    return days, moments, [years_between(now, m) for m in moments]


def fixed(plan: Plan) -> dict[str, Any]:
    """The share trade's result at each level, if the price gets there: arithmetic, not a chance."""
    d, n = plan.direction, plan.shares
    a, b = part_sizes("split", n)
    return {"at_stop": d * n * (plan.stop - plan.entry), "at_target_1": d * n * (plan.target_1 - plan.entry),
            "at_target_2": d * n * (plan.target_2 - plan.entry),
            "at_split": d * (a * (plan.target_1 - plan.entry) + b * (plan.target_2 - plan.entry))}


def run(*, plan: Plan, days: Sequence[date], now: datetime, moments: Sequence[datetime], times: Sequence[float],
        vol: float, growth: float, rate: float, leg: Leg | None = None, paths: int = PATHS,
        seed: int = SEED) -> dict[str, Any]:
    """The whole simulation for one plan, with the contract when given (walked on the same random numbers at
    its own volatility and carry)."""
    w = walk(plan, times, vol, growth, paths, seed)
    labels = exit_labels(w["exit_kind"])
    out = {"with_shares": {**summary(share_pnl(plan, w), labels), "fixed": fixed(plan),
                           "part_sizes": part_sizes(plan.plan, plan.shares)},
           "fan": fan(w["closes"], days), "samples": samples(plan, w, labels, days), "paths": paths, "seed": seed}
    if leg is not None:
        wc = walk(plan, times, leg.vol, leg.growth, paths, seed)
        out["with_contract"] = {**summary(contract_pnl(leg, plan.plan, now, moments, wc, rate), exit_labels(wc["exit_kind"])),
                                "cost": leg.entry * leg.contracts * MULTIPLIER, "max_loss": -leg.entry * leg.contracts * MULTIPLIER,
                                "volatility": leg.vol, "part_sizes": part_sizes(plan.plan, leg.contracts)}
    return out


def _chain_volatility(svc: Any, symbol: str, today: date, h_day: date) -> tuple[float, float, str, float] | None:
    """The at-the-money implied volatility and carry of the first expiration at least a week after the
    horizon, read straight from the chain: (vol, growth, expiration, spot). None when it cannot be read."""
    from .options.evaluator import pick_expirations
    from .providers.base import DataError
    try:
        exps = pick_expirations(svc.market.expirations(symbol), today, h_day, wanted=1)
        if not exps:
            return None
        a = svc._chain_for(symbol, exps[0])
    except (DataError, ValueError):
        return None
    ivs = [c.get("iv") for c in a.contracts if c.get("iv") and abs(c["strike"] / a.spot - 1) < 0.03]
    if not ivs or not (a.t > 0 and a.spot and a.forward):
        return None
    return float(np.median(ivs)), math.log(a.forward / a.spot) / a.t, a.expiration, a.spot


def simulate_trade(svc: Any, analyst: Any, req: dict[str, Any]) -> dict[str, Any]:
    """The simulation for one trade request: levels and size from the grade unless given, volatility and the
    contract from the option chain when it can be read."""
    from .ideas import earnings_within
    from .options.evaluator import _leg, horizon_date
    from .providers.base import DataError
    symbol = svc._symbol(req["symbol"])
    direction = 1 if str(req.get("direction", "LONG")).upper() == "LONG" else -1
    side = "LONG" if direction > 0 else "SHORT"
    spot = svc.market.spot(symbol)
    entry = float(req.get("entry_price") or spot)
    if not (LEVEL_RANGE[0] * spot <= entry <= LEVEL_RANGE[1] * spot):
        raise TradeSimError(f"The entry (${entry:,.2f}) must be between a twentieth of the price now and twenty times "
                            f"it (${spot:,.2f})")
    horizon = int(req.get("horizon_days") or 20)
    grade = analyst.evaluate_position({"symbol": symbol, "direction": side, "entry_price": entry,
                                       "invalidation_price": req.get("stop"), "target_1": req.get("target_1"),
                                       "target_2": req.get("target_2"), "horizon_days": horizon})
    lv, sz = grade["levels"], grade["sizing"]
    notes: list[str] = []
    budget = float(sz["risk_budget"])
    account = float(svc.settings.account_size)
    if req.get("shares") is not None:
        shares = int(req["shares"])
        if shares < 1:
            raise TradeSimError("Simulate at least one share")
    else:
        shares = int(sz["shares"])
        if shares < 1:
            shares = 1
            notes.append(f"One share risks ${abs(entry - lv['invalidation']):,.2f} to the stop, more than your "
                         f"${budget:,.0f} risk per trade, so no position fits your limit. The figures are for one share.")
        elif shares * entry > account:
            shares = max(1, int(account // entry))
            notes.append(f"The size your risk per trade allows would cost more than your ${account:,.0f} account, so it "
                         f"is cut to {shares} share{'s' if shares != 1 else ''}.")
    plan = Plan(direction, entry, float(lv["invalidation"]), float(lv["target_1"]), float(lv["target_2"]), shares,
                str(req.get("exit_plan") or "target_1"))
    plan.check()
    if req.get("target_2") is not None and direction * (plan.target_2 - plan.target_1) <= 0:
        raise TradeSimError("Target 2 must be beyond target 1")
    notes.extend(n for n in lv.get("notes") or [] if "below zero" in n)
    if plan.plan == "split" and shares == 1:
        notes.append("With one share the split plan sells it at target 1.")
    now = datetime.now(NY)
    today = now.date()
    h_day = horizon_date(today, horizon)
    days, moments, times = schedule(today, h_day, now)
    rate = svc.settings.risk_free_rate
    vol = growth = None
    vol_source = vol_expiration = None
    leg, contract = None, None
    want = req.get("contract")
    if isinstance(want, dict) and str(want.get("option_type", "")).upper() != ("CALL" if direction > 0 else "PUT"):
        raise TradeSimError(f"A {'long' if direction > 0 else 'short'} is simulated with a "
                            f"{'call' if direction > 0 else 'put'}, not a {str(want.get('option_type')).lower()}")
    if want != "none":
        only = [want] if isinstance(want, dict) else None
        try:
            opt = analyst.evaluate_options({"symbol": symbol, "direction": "BULLISH" if direction > 0 else "BEARISH",
                                            "target_price": plan.target_1, "target_2": plan.target_2,
                                            "invalidation_price": plan.stop, "horizon_days": horizon,
                                            "entry_price": entry, "contracts": 1}, only=only)
        except (DataError, ValueError) as exc:
            if only:
                raise TradeSimError(f"The contract you chose could not be used: {exc}") from exc
            opt = None
            notes.append(f"No contract is simulated: {exc}")
        if opt and opt.get("market"):
            m = opt["market"]
            # The carry from the forward and the price the evaluator read it against, over its own time.
            o_spot, t_h = float(opt["thesis"]["spot"]), max(times[-1], 1 / 365)
            vol, vol_source, vol_expiration = float(m["atm_iv"]), "implied", m.get("atm_expiration")
            growth = math.log(m["forward_at_horizon"] / o_spot) / t_h if m.get("forward_at_horizon") else 0.0
        row, why, category = None, None, None
        if opt and only:
            row = opt["contracts"][0] if opt["contracts"] else None
            if row and any("before the" in a and "horizon" in a for a in row.get("avoid") or []):
                raise TradeSimError(f"That contract expires on {row['expiration']}, before the horizon date "
                                    f"({h_day.isoformat()}). Choose a later expiration or a shorter horizon.")
            notes.extend(f"The contract you chose: {a}" for a in (row or {}).get("avoid") or [])
            why = "The contract you chose."
        elif opt:
            # The best gain at target 1 for each dollar lost if invalidated, among the contracts one of which the
            # risk per trade pays for; failing that, the best of all (Decide's pick), said so below.
            from .options.evaluator import best_within_budget
            row, category, why = best_within_budget(opt, budget)
            if row is None:
                notes.append("No listed contract passed the checks for this trade (quotes, spread, open interest, "
                             "expiring after the horizon, paying at target 1), so only the shares are simulated.")
        if row and row.get("mid"):
            analysis = svc._chain_for(symbol, row["expiration"])
            chain_row = next((c for c in analysis.contracts if c["option_type"] == row["option_type"].lower()
                              and abs(c["strike"] - row["strike"]) < 1e-6), None)
            if chain_row is None or not (chain_row.get("model_vol") or chain_row.get("iv")):
                notes.append("The chosen contract has no usable implied volatility now, so only the shares are simulated.")
            else:
                leg = _leg(chain_row, analysis, 1)
                premium = leg.entry
                if abs(entry / spot - 1) > 0.001:
                    # The shares start at your entry, so the contract is bought with the stock there too.
                    premium = float(leg_value(leg, np.array([entry]), now, rate)[0])
                    notes.append(f"The shares start at your entry (${entry:,.2f}; the stock is at ${spot:,.2f} now), so the "
                                 f"contract is priced at its model value with the stock at your entry: ${premium:,.2f} a "
                                 f"share instead of today's ${leg.entry:,.2f} midpoint.")
                n = int(req.get("contracts") or max(1, min(500, int(budget // (premium * MULTIPLIER)))))
                leg = replace(leg, entry=premium, contracts=n)
                cost = premium * MULTIPLIER * n
                contract = {"option_symbol": row.get("option_symbol"), "option_type": row["option_type"],
                            "strike": row["strike"], "expiration": row["expiration"], "contracts": n,
                            "premium": premium, "midpoint": row["mid"], "bid": row.get("bid"), "ask": row.get("ask"),
                            "iv": leg.vol, "delta": row.get("delta"), "spread_cost": row.get("spread_cost"),
                            "category": category, "why": why, "flags": row.get("flags") or [], "cost": cost,
                            "contracts_source": "yours" if req.get("contracts") else "risk budget"}
                if cost > budget and not req.get("contracts"):
                    notes.append(f"{n} contract{'s' if n != 1 else ''} cost{'s' if n == 1 else ''} ${cost:,.0f}, more than "
                                 f"your ${budget:,.0f} risk per trade. The loss at the stop is usually less than the cost, "
                                 "but all of it can be lost.")
                if plan.plan == "split" and n == 1:
                    notes.append("With one contract the split plan sells it at target 1.")
    time_grid = times
    if vol is None:
        read = _chain_volatility(svc, symbol, today, h_day)
        if read:
            vol, growth, vol_expiration, _ = read
            vol_source = "implied"
        else:
            enriched = svc.market.enriched(symbol, 1, complete=True)
            vol = realized_vol(enriched["close"].to_numpy(dtype=float))
            if vol is None:
                raise TradeSimError(f"No volatility for {symbol}: no usable option prices and too little price history")
            vol_source, growth = "realized", rate
            # The stock's own volatility is per trading day, so the paths step in trading days here.
            time_grid = [(i + 1) / 252 for i in range(len(days))]
            notes.append(f"No option prices to read the market's uncertainty from, so the stock's own volatility over the "
                         f"last {REALIZED_DAYS} trading days is used ({100 * vol:.0f}% a year).")
    out = run(plan=plan, days=days, now=now, moments=moments, times=time_grid, vol=vol, growth=growth or 0.0,
              rate=rate, leg=leg)
    if direction < 0:
        notes.append("Short sale: the borrow fee and whether shares can be borrowed are not visible to this app and "
                     "are not in these figures.")
    notes.append("Fills are at the stop or target price itself; a gap through the stop costs more.")
    earnings = grade.get("earnings") or {}
    if earnings_within(earnings.get("date"), horizon, today):
        notes.append(f"Earnings on {earnings['date']}{' (estimated)' if earnings.get('estimated') else ''} fall inside "
                     "the horizon: the report can gap the stock past the stop or the target.")
    if svc.market.info.mode == "demo":
        notes.insert(0, "Demo prices are synthetic: this simulation says nothing about the real stock.")
    return {"symbol": symbol, "direction": side, "spot": spot, "entry": entry, "stop": plan.stop,
            "target_1": plan.target_1, "target_2": plan.target_2, "shares": shares,
            "shares_source": "yours" if req.get("shares") is not None else "risk budget",
            "risk_budget": budget, "position_value": shares * entry, "plan": plan.plan,
            "plan_label": PLANS[plan.plan], "plans": PLANS, "horizon_days": horizon, "horizon_date": h_day.isoformat(),
            "first_day": days[0].isoformat(),
            "levels_source": {"stop": lv["invalidation_source"], "targets": lv["targets_source"]},
            "level_notes": lv.get("notes") or [],
            "grade": {"grade": grade["grade"], "score": grade["score"], "caps": [c["reason"] for c in grade["caps"]]},
            "volatility": {"value": vol, "source": vol_source, "expiration": vol_expiration, "growth": growth},
            **out, "contract_spec": contract, "notes": notes, "data_source": svc.market.info.mode,
            "model": "A model, not a forecast: the stock walks at the volatility option prices imply, with no view on "
                     "direction, and the trade's own stop and targets are applied on every path. Averaged over all "
                     "paths a trade comes out close to break-even; what is left is the carry in the forward price "
                     "(interest less expected dividends), not an edge. The contract is walked at its own implied "
                     "volatility. What this shows is how often each exit happens and what each one makes or loses."}


__all__ = ["Plan", "run", "simulate_trade", "walk", "share_pnl", "contract_pnl", "summary", "exit_labels", "fan",
           "schedule", "fixed", "part_sizes", "realized_vol", "TradeSimError", "PLANS", "PATHS", "SEED"]
