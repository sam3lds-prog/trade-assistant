"""Simulate one trade (2.7): what a stock trade with a stop and targets can make or lose, and how often.

The stock is walked forward one trading day at a time on many paths, from the entry price, under the same
lognormal the rest of the app uses for option-implied figures: centred on the forward (the carry the option
chain implies) at the at-the-money implied volatility of the expiration nearest after the horizon. Without
usable option prices, the stock's own volatility over the last 60 finished days is used instead, and the
result says so.

The trade's own rules are applied on every path: out at the stop or at the target, whichever the price
touches first, otherwise out at the close on the horizon date. Touches between two closes are found with
the Brownian-bridge probability, so a level crossed and recrossed within a day still counts. Fills are at
the level itself; a gap through the stop costs more than that, which the simulation does not add.

The same paths value a bought call (for a long) or put (for a short), exited on the same stock rules, at
its own implied volatility held constant, so the share trade and the contract can be compared outcome by
outcome.

What this is not: a forecast. The model has no view on direction, so averaged over every path a trade
comes out close to break-even (the carry and nothing else). What it shows is the spread of results and how
often each exit happens if the market is as uncertain as option prices say. Any edge is whatever makes the
target likelier than that, and that is the trader's view, not the model's. Paths and seed are fixed, so
the same inputs give the same figures.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any, Sequence

import numpy as np

from .optionmath import NY
from .simulate import MULTIPLIER, Leg, eval_moment, leg_value, trading_days, years_between

PATHS = 20_000
SEED = 20_270
SAMPLE_PATHS = 24
REALIZED_DAYS = 60
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

    def check(self) -> None:
        d = self.direction
        if d not in (1, -1):
            raise TradeSimError("direction must be LONG or SHORT")
        if not (self.entry > 0 and self.stop > 0 and self.target_1 > 0 and self.target_2 > 0):
            raise TradeSimError("Entry, stop and targets must be above zero")
        if d * (self.entry - self.stop) <= 0:
            raise TradeSimError(f"The stop must be {'below' if d > 0 else 'above'} the entry for a "
                                f"{'long' if d > 0 else 'short'}")
        if d * (self.target_1 - self.entry) <= 0:
            raise TradeSimError(f"Target 1 must be {'above' if d > 0 else 'below'} the entry")
        if self.plan != "target_1" and d * (self.target_2 - self.target_1) <= 0:
            raise TradeSimError(f"Target 2 must be beyond target 1 for the plan "
                                f"\"{PLANS.get(self.plan, self.plan)}\"")
        if self.plan not in PLANS:
            raise TradeSimError(f"The exit plan must be one of {', '.join(PLANS)}")
        if self.shares < 0:
            raise TradeSimError("Shares cannot be negative")


def _touch(x0: np.ndarray, x1: np.ndarray, level: float, above: bool, var: float, u: np.ndarray) -> np.ndarray:
    """Whether a log-price path from x0 to x1 over one step of variance `var` touched `level` on the way:
    certainly when it ends beyond it, otherwise with the Brownian-bridge probability."""
    if above:
        beyond = x1 >= level
        gap0, gap1 = level - x0, level - x1
    else:
        beyond = x1 <= level
        gap0, gap1 = x0 - level, x1 - level
    with np.errstate(over="ignore", invalid="ignore"):
        p = np.exp(-2.0 * np.clip(gap0, 0, None) * np.clip(gap1, 0, None) / var) if var > 0 else np.zeros_like(x0)
    return beyond | (u < p)


def walk(plan: Plan, times: Sequence[float], vol: float, growth: float, paths: int = PATHS,
         seed: int = SEED) -> dict[str, np.ndarray]:
    """Run the paths. `times` are the years from now to each trading day's close (increasing). Returns,
    per path, the price and step of each exit part (two parts for the split plan, one otherwise) and how
    each part ended: 1 target, -1 stop, 0 horizon; and the closes of every path, for the fan."""
    plan.check()
    if not times or vol is None or not math.isfinite(vol) or vol <= 0:
        raise TradeSimError("A positive volatility and at least one trading day are needed")
    rng = np.random.default_rng(seed)
    n = len(times)
    dts = np.diff(np.concatenate([[0.0], np.asarray(times, dtype=float)]))
    dts = np.maximum(dts, 1e-6)
    d = plan.direction
    x = np.full(paths, math.log(plan.entry))
    closes = np.empty((n, paths))
    lstop = math.log(plan.stop)
    targets = [plan.target_1] if plan.plan == "target_1" else [plan.target_2] if plan.plan == "target_2" \
        else [plan.target_1, plan.target_2]
    ltargets = [math.log(t) for t in targets]
    parts = len(targets)
    exit_px = np.full((parts, paths), np.nan)
    exit_step = np.full((parts, paths), -1, dtype=int)
    exit_kind = np.zeros((parts, paths), dtype=int)
    open_ = np.ones((parts, paths), dtype=bool)
    for k in range(n):
        var = vol * vol * dts[k]
        x1 = x + (growth - 0.5 * vol * vol) * dts[k] + math.sqrt(var) * rng.standard_normal(paths)
        u_stop, u_tgt = rng.random(paths), rng.random(paths)
        hit_stop = _touch(x, x1, lstop, above=d < 0, var=var, u=u_stop)
        for j, lt in enumerate(ltargets):
            live = open_[j]
            if not live.any():
                continue
            hit_t = _touch(x, x1, lt, above=d > 0, var=var, u=u_tgt)
            # Both levels inside one day: which came first is unknown, so the stop is taken (the cautious side).
            stop_now = live & hit_stop
            tgt_now = live & hit_t & ~hit_stop
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
    """Dollars made or lost on the shares, per path (each part an equal share of the position)."""
    parts = w["exit_px"].shape[0]
    return (plan.direction * plan.shares / parts * (w["exit_px"] - plan.entry)).sum(axis=0)


def contract_pnl(leg: Leg, moments: Sequence[datetime], w: dict[str, np.ndarray], rate: float) -> np.ndarray:
    """Dollars made or lost on the contract, per path: each part of it sold at the stock's exit, valued at
    that day's close at the contract's own implied volatility."""
    parts, paths = w["exit_px"].shape
    value = np.zeros(paths)
    for j in range(parts):
        for k in np.unique(w["exit_step"][j]):
            m = w["exit_step"][j] == k
            value[m] += leg_value(leg, w["exit_px"][j, m], moments[int(k)], rate) / parts
    return (value - leg.entry) * leg.contracts * MULTIPLIER


def summary(pnl: np.ndarray, kinds: np.ndarray, bins: int = 30) -> dict[str, Any]:
    """Percentiles, chances and a histogram of one result."""
    q = np.percentile(pnl, PCTS)
    lo, hi = float(pnl.min()), float(pnl.max())
    if hi - lo < 1e-9:
        lo, hi = lo - 1.0, hi + 1.0
    counts, edges = np.histogram(pnl, bins=bins, range=(lo, hi))
    return {"mean": float(pnl.mean()), "standard_error": float(pnl.std(ddof=1) / math.sqrt(len(pnl))),
            **{f"p{p}": float(v) for p, v in zip(PCTS, q)},
            "chance_profit": float((pnl > 0.005).mean()), "chance_loss": float((pnl < -0.005).mean()),
            "worst": lo, "best": hi,
            "histogram": {"edges": [float(e) for e in edges], "counts": [int(c) for c in counts]},
            "by_exit": _by_exit(pnl, kinds)}


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


def _by_exit(pnl: np.ndarray, labels: np.ndarray) -> list[dict[str, Any]]:
    out = []
    for key, words in EXIT_WORDS.items():
        m = labels == key
        if m.any():
            out.append({"key": key, "label": words, "share": float(m.mean()), "average": float(pnl[m].mean()),
                        "low": float(pnl[m].min()), "high": float(pnl[m].max())})
    return out


def fan(closes: np.ndarray, days: Sequence[date]) -> list[dict[str, Any]]:
    """The stock's price on each day: 10th, 25th, 50th, 75th and 90th percentile across paths."""
    q = np.percentile(closes, (10, 25, 50, 75, 90), axis=1)
    return [{"date": d.isoformat(), "p10": float(q[0, i]), "p25": float(q[1, i]), "p50": float(q[2, i]),
             "p75": float(q[3, i]), "p90": float(q[4, i])} for i, d in enumerate(days)]


def samples(plan: Plan, w: dict[str, np.ndarray], labels: np.ndarray, days: Sequence[date],
            count: int = SAMPLE_PATHS) -> list[dict[str, Any]]:
    """A few paths to draw, each up to the day it ended (the last part's exit), with how it ended."""
    last_part = np.argmax(w["exit_step"], axis=0)          # the part that ended last (the first, on a tie)
    if w["exit_step"].shape[0] > 1:
        tie = w["exit_step"][0] == w["exit_step"][1]
        last_part = np.where(tie, 1, last_part)
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
    """Annualised close-to-close volatility over the last `days` finished days."""
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


def run(*, plan: Plan, days: Sequence[date], moments: Sequence[datetime], times: Sequence[float], vol: float,
        growth: float, rate: float, leg: Leg | None = None, paths: int = PATHS, seed: int = SEED) -> dict[str, Any]:
    """The whole simulation for one plan, with the contract when given."""
    w = walk(plan, times, vol, growth, paths, seed)
    labels = exit_labels(w["exit_kind"])
    shares = share_pnl(plan, w)
    out = {"with_shares": {**summary(shares, labels), "fixed": _fixed(plan)},
           "fan": fan(w["closes"], days), "samples": samples(plan, w, labels, days), "paths": paths, "seed": seed}
    if leg is not None:
        c = contract_pnl(leg, moments, w, rate)
        out["with_contract"] = {**summary(c, labels), "cost": leg.entry * leg.contracts * MULTIPLIER,
                           "max_loss": -leg.entry * leg.contracts * MULTIPLIER}
    return out


def _fixed(plan: Plan) -> dict[str, Any]:
    """The share trade's result at each level, if the price gets there: arithmetic, not a chance."""
    d, n = plan.direction, plan.shares
    return {"at_stop": d * n * (plan.stop - plan.entry), "at_target_1": d * n * (plan.target_1 - plan.entry),
            "at_target_2": d * n * (plan.target_2 - plan.entry),
            "at_split": d * n * (0.5 * (plan.target_1 + plan.target_2) - plan.entry)}


def simulate_trade(svc: Any, analyst: Any, req: dict[str, Any]) -> dict[str, Any]:
    """The simulation for one trade request: levels and size from the grade unless given, volatility and the
    contract from the option chain when it can be read."""
    from dataclasses import replace

    from .options.evaluator import _leg, horizon_date
    from .providers.base import DataError
    symbol = svc._symbol(req["symbol"])
    direction = 1 if str(req.get("direction", "LONG")).upper() == "LONG" else -1
    side = "LONG" if direction > 0 else "SHORT"
    spot = svc.market.spot(symbol)
    entry = float(req.get("entry_price") or spot)
    horizon = int(req.get("horizon_days") or 20)
    grade = analyst.evaluate_position({"symbol": symbol, "direction": side, "entry_price": entry,
                                       "invalidation_price": req.get("stop"), "target_1": req.get("target_1"),
                                       "target_2": req.get("target_2"), "horizon_days": horizon})
    lv, sz = grade["levels"], grade["sizing"]
    shares = int(req["shares"]) if req.get("shares") is not None else int(sz["shares"])
    plan = Plan(direction, entry, float(lv["invalidation"]), float(lv["target_1"]), float(lv["target_2"]), shares,
                str(req.get("exit_plan") or "target_1"))
    plan.check()
    now = datetime.now(NY)
    today = now.date()
    h_day = horizon_date(today, horizon)
    days, moments, times = schedule(today, h_day, now)
    rate = svc.settings.risk_free_rate
    notes: list[str] = []
    vol = growth = None
    vol_source, vol_expiration = None, None
    leg, contract = None, None
    want = req.get("contract")
    if want != "none":
        only = [want] if isinstance(want, dict) else None
        try:
            opt = analyst.evaluate_options({"symbol": symbol, "direction": "BULLISH" if direction > 0 else "BEARISH",
                                            "target_price": plan.target_1, "target_2": plan.target_2,
                                            "invalidation_price": plan.stop, "horizon_days": horizon,
                                            "entry_price": entry, "contracts": 1}, only=only)
        except (DataError, ValueError) as exc:
            opt = None
            notes.append(f"The option chain could not be used ({exc}).")
        if opt and opt.get("market"):
            m = opt["market"]
            vol, vol_source, vol_expiration = float(m["atm_iv"]), "implied", m.get("atm_expiration")
            t_h = times[-1]
            growth = math.log(m["forward_at_horizon"] / spot) / t_h if t_h > 0 and m.get("forward_at_horizon") else 0.0
        if opt:
            if only:
                row = opt["contracts"][0] if opt["contracts"] else None
                why, category = "The contract you chose.", None
            else:
                # The best gain at target 1 for each dollar lost if invalidated, among the contracts one of which
                # your risk per trade pays for; failing that, the best of all (Decide's pick), said so below.
                budget = float(sz["risk_budget"])
                fits = [r for r in opt["contracts"] if r.get("usable") and r.get("reward_to_risk") and r["premium"] <= budget]
                pick = None
                if fits:
                    best = max(fits, key=lambda r: r["reward_to_risk"])
                    pick = {"contract": best, "category": "BEST RISK-ADJUSTED WITHIN YOUR RISK PER TRADE",
                            "why": f"Gains ${best['at_target']:,.0f} at target 1 for ${abs(best['if_invalidated']):,.0f} lost "
                                   f"if invalidated ({best['reward_to_risk']:.2f} to 1), and one costs ${best['premium']:,.0f}, "
                                   f"within your ${budget:,.0f} risk per trade."}
                else:
                    pick = next((p for p in opt["picks"] if p["category"] == "BEST RISK-ADJUSTED"), None) \
                        or (opt["picks"][0] if opt["picks"] else None)
                row = pick["contract"] if pick else None
                why, category = (pick["why"], pick["category"]) if pick else (None, None)
                if not pick:
                    notes.append("No listed contract passed the checks for this trade (quotes, spread, open interest, "
                                 "expiring after the horizon, paying at target 1), so only the shares are simulated.")
            if row and row.get("mid"):
                analysis = svc._chain_for(symbol, row["expiration"])
                chain_row = next((c for c in analysis.contracts if c["option_type"] == row["option_type"].lower()
                                  and abs(c["strike"] - row["strike"]) < 1e-6), None)
                if chain_row is not None and (chain_row.get("model_vol") or chain_row.get("iv")):
                    budget = float(sz["risk_budget"])
                    n = req.get("contracts") or max(1, min(500, int(budget // (row["mid"] * MULTIPLIER))))
                    leg = _leg(chain_row, analysis, int(n))
                    premium = leg.entry
                    if abs(entry / spot - 1) > 0.001:
                        # The shares start at your entry, so the contract is bought with the stock there too.
                        premium = float(leg_value(leg, np.array([entry]), now, rate)[0])
                        notes.append(f"The shares start at your entry (${entry:,.2f}, the stock is at ${spot:,.2f} now), "
                                     f"so the contract is priced at the model's value with the stock at your entry: "
                                     f"${premium:,.2f} a share instead of the ${leg.entry:,.2f} midpoint.")
                        leg = replace(leg, entry=premium)
                    contract = {"option_symbol": row.get("option_symbol"), "option_type": row["option_type"],
                                "strike": row["strike"], "expiration": row["expiration"], "contracts": leg.contracts,
                                "premium": premium, "midpoint": row["mid"], "bid": row.get("bid"), "ask": row.get("ask"),
                                "iv": leg.vol, "delta": row.get("delta"), "spread_cost": row.get("spread_cost"),
                                "category": category, "why": why, "flags": row.get("flags") or [],
                                "contracts_source": "yours" if req.get("contracts") else "risk budget"}
                    cost = premium * MULTIPLIER * leg.contracts
                    if cost > budget and not req.get("contracts"):
                        notes.append(f"One contract costs ${cost:,.0f}, more than your ${budget:,.0f} risk per trade. "
                                     "Its loss at the stop is usually less than its cost, but all of it can be lost.")
    if vol is None:
        enriched = svc.market.enriched(symbol, 1, complete=True)
        vol = realized_vol(enriched["close"].to_numpy(dtype=float))
        if vol is None:
            raise TradeSimError(f"No volatility for {symbol}: no usable option prices and too little price history")
        vol_source, growth = "realized", rate
        notes.append(f"No option prices to read the market's uncertainty from, so the stock's own volatility over the "
                     f"last {REALIZED_DAYS} trading days is used ({100 * vol:.0f}% a year).")
    out = run(plan=plan, days=days, moments=moments, times=times, vol=vol, growth=growth or 0.0, rate=rate, leg=leg)
    if direction < 0:
        notes.append("Short sale: the borrow fee and whether shares can be borrowed are not visible to this app and "
                     "are not in these figures.")
    notes.append("Fills are at the stop or target price itself; a gap through the stop costs more.")
    earnings = grade.get("earnings") or {}
    if earnings.get("date") and today <= date.fromisoformat(earnings["date"]) <= h_day:
        notes.append(f"Earnings on {earnings['date']}{' (estimated)' if earnings.get('estimated') else ''} fall inside "
                     "the horizon: the report can gap the stock past the stop or the target.")
    if svc.market.info.mode == "demo":
        notes.insert(0, "Demo prices are synthetic: this simulation says nothing about the real stock.")
    return {"symbol": symbol, "direction": side, "spot": spot, "entry": entry, "stop": plan.stop,
            "target_1": plan.target_1, "target_2": plan.target_2, "shares": shares,
            "shares_source": "yours" if req.get("shares") is not None else "risk budget",
            "risk_budget": sz["risk_budget"], "position_value": shares * entry, "plan": plan.plan,
            "plan_label": PLANS[plan.plan], "plans": PLANS, "horizon_days": horizon, "horizon_date": h_day.isoformat(),
            "first_day": days[0].isoformat(), "levels_source": {"stop": lv["invalidation_source"], "targets": lv["targets_source"]},
            "grade": {"grade": grade["grade"], "score": grade["score"], "caps": [c["reason"] for c in grade["caps"]]},
            "volatility": {"value": vol, "source": vol_source, "expiration": vol_expiration, "growth": growth},
            **out, "contract_spec": contract, "notes": notes, "data_source": svc.market.info.mode,
            "model": "A model, not a forecast: the stock walks at the volatility option prices imply, with no view on "
                     "direction, and the trade's own stop and targets are applied on every path. Averaged over all "
                     "paths it comes out close to break-even by construction; what it shows is how often each exit "
                     "happens and what each one makes or loses."}


__all__ = ["Plan", "run", "simulate_trade", "walk", "share_pnl", "contract_pnl", "summary", "exit_labels", "fan", "schedule",
           "realized_vol", "TradeSimError", "PLANS", "PATHS", "SEED"]
