"""Options contract evaluator: which call or put fits a stock thesis, and which to avoid.

The thesis is about the stock: a direction, a target price, an invalidation price (the stock
price that proves the idea wrong) and a horizon in trading days. Each candidate contract is
valued in a scenario matrix with the same Black-76 model as the simulator (simulate.py):
the stock at the invalidation price, unchanged, at target 1 and at target 2; on the horizon
date and halfway there; and with implied volatility five points lower, unchanged and five
points higher. Entry is at the midpoint; the cost of crossing the spread is shown apart.

The exit rule stays on the stock. "Invalidated" means the stock closed at the invalidation
price, and the loss shown is what the option would be worth then, not a stop on the option
price, which jumps around with the spread and volatility.

Contracts are ranked four ways:

    BEST THESIS MATCH      largest return on premium if the stock reaches target 1 by the horizon
    LOWEST COST            cheapest contract that still makes money at target 1 by the horizon
    BEST RISK-ADJUSTED     largest gain at target 1 for each dollar lost if invalidated
    MOST CONSERVATIVE      smallest share of the premium lost if invalidated, among contracts that
                           pay at the target and leave at least twice the horizon to expiration

Contracts that should not be used are listed with the reason: no two-sided quote, a wide
spread, little open interest, expiring before the horizon, losing money even if the target
is reached, or costing more than the budget. Earnings before expiration are flagged: implied
volatility usually falls after the report.

Chances shown are market-implied (from option prices, a lognormal at the at-the-money
volatility), not forecasts. Only bought calls and puts are evaluated.
"""

from __future__ import annotations

import math
from datetime import date, datetime
from typing import Any, Sequence

import numpy as np

from ..optionmath import NY, days_to_expiry, prob_finish_beyond
from ..simulate import WEIGHTS, Z, Leg, MULTIPLIER, eval_moment, expiry_moment, expiry_profile, \
    implied_expectation, market_odds, position_pnl, trading_days, years_between
from ..versions import OPTIONS_EVALUATOR_VERSION

IV_SHIFTS = (-0.05, 0.0, 0.05)
MIN_OPEN_INTEREST = 100
WIDE_SPREAD_PCT = 10.0
DELTA_RANGE = (0.10, 0.90)
MAX_CANDIDATES_PER_EXPIRY = 24
CATEGORIES = ("BEST THESIS MATCH", "LOWEST COST", "BEST RISK-ADJUSTED", "MOST CONSERVATIVE")


class OptionsEvaluationError(ValueError):
    pass


def horizon_date(today: date, horizon: int) -> date:
    """The date `horizon` trading days after today (market holidays are not removed)."""
    days = [d for d in trading_days(today, date.fromordinal(today.toordinal() + horizon * 2 + 10)) if d > today]
    return days[min(horizon, len(days)) - 1]


def pick_expirations(expirations: Sequence[str], today: date, horizon_day: date, wanted: int = 4) -> list[str]:
    """Up to `wanted` expirations that leave time after the horizon: the first one at least a week
    after it, and ones near two, three and four times the horizon."""
    after = sorted(e for e in expirations if date.fromisoformat(e) >= date.fromordinal(horizon_day.toordinal() + 7))
    if not after:
        return []
    h = max((horizon_day - today).days, 1)
    chosen = [after[0]]
    for mult in (2, 3, 4.5):
        target = today.toordinal() + mult * h
        best = min(after, key=lambda e: abs(date.fromisoformat(e).toordinal() - target))
        if best not in chosen:
            chosen.append(best)
    return sorted(chosen)[:wanted]


def _leg(row: dict[str, Any], analysis: Any, contracts: int) -> Leg:
    growth = math.log(analysis.forward / analysis.spot) / analysis.t if analysis.t > 0 else 0.0
    vol = row.get("model_vol") or row.get("iv")
    return Leg(row["option_type"], float(row["strike"]), analysis.expiration, 1, contracts, float(row["mid"]),
               float(vol), growth)


def value_contract(row: dict[str, Any], analysis: Any, thesis: dict[str, Any], now: datetime, rate: float,
                   contracts: int, earnings: dict[str, Any] | None, budget: float | None) -> dict[str, Any]:
    """Scenario values, ratios and problems for one contract."""
    exp = analysis.expiration
    out: dict[str, Any] = {"option_symbol": row.get("symbol"), "option_type": row["option_type"].upper(),
                           "strike": float(row["strike"]), "expiration": exp, "dte": days_to_expiry(exp),
                           "bid": row.get("bid"), "ask": row.get("ask"), "mid": row.get("mid"),
                           "spread_pct": row.get("spread_pct"), "open_interest": row.get("open_interest"),
                           "volume": row.get("volume"), "delta": row.get("delta"), "theta": row.get("theta"),
                           "vega": row.get("vega"), "iv": row.get("iv") or row.get("model_vol")}
    avoid: list[str] = []
    flags: list[str] = []
    if row.get("mid") is None or not (row.get("bid") and row.get("ask")) or not (row.get("model_vol") or row.get("iv")):
        avoid.append("No two-sided quote, so there is no fair price to trade at.")
        out.update({"avoid": avoid, "flags": flags, "usable": False})
        return out
    leg = _leg(row, analysis, contracts)
    premium = leg.entry * MULTIPLIER * contracts
    spread_cost = (row["ask"] - row["bid"]) * MULTIPLIER * contracts
    out["premium"] = premium
    out["spread_cost"] = spread_cost
    exp_day = date.fromisoformat(exp)
    h_day = min(thesis["horizon_date"], exp_day)
    half_day = date.fromordinal(now.astimezone(NY).date().toordinal() + max(1, (h_day.toordinal()
                                                                                 - now.astimezone(NY).date().toordinal()) // 2))
    when = {"horizon": expiry_moment(exp) if h_day == exp_day else eval_moment(h_day, now),
            "halfway": eval_moment(min(half_day, exp_day), now)}
    prices = {"invalidation": thesis["invalidation"], "unchanged": thesis["spot"], "target_1": thesis["target_1"],
              "target_2": thesis["target_2"]}
    px = np.array(list(prices.values()), dtype=float)
    matrix = []
    for when_name, moment in when.items():
        for shift in IV_SHIFTS:
            pnl = position_pnl([leg], px, moment, rate, shift)
            matrix.append({"when": when_name, "date": moment.astimezone(NY).date().isoformat(), "iv_shift": shift,
                           **{name: float(v) for name, v in zip(prices, pnl)}})
    out["scenarios"] = matrix
    base = next(m for m in matrix if m["when"] == "horizon" and m["iv_shift"] == 0.0)
    soon = next(m for m in matrix if m["when"] == "halfway" and m["iv_shift"] == 0.0)
    gain = base["target_1"]
    loss = min(base["invalidation"], soon["invalidation"])          # whichever comes worse, capped by the premium
    loss = max(loss, -premium)
    out["at_target"] = gain
    out["at_target_2"] = base["target_2"]
    out["if_invalidated"] = loss
    out["if_unchanged"] = base["unchanged"]
    out["return_on_premium"] = gain / premium if premium > 0 else None
    out["reward_to_risk"] = gain / abs(loss) if loss < 0 else None
    out["loss_share_of_premium"] = abs(loss) / premium if premium > 0 else None
    out["after_spread_at_target"] = gain - spread_cost       # half the spread to get in, half to get out
    prof = expiry_profile([leg])
    out["breakeven_at_expiration"] = prof["breakevens"][0] if prof["breakevens"] else None
    # The stock price at which the contract breaks even on the horizon date, from a fine grid.
    lo, hi = sorted([thesis["spot"] * 0.5, thesis["spot"] * 1.5])
    grid = np.linspace(lo, hi, 801)
    curve = position_pnl([leg], grid, when["horizon"], rate, 0.0)
    cross = np.flatnonzero(np.diff(np.sign(curve)) != 0)
    out["breakeven_at_horizon"] = float(grid[cross[0]]) if len(cross) else None
    atm = thesis.get("atm_iv") or leg.vol
    odds = market_odds([leg], thesis["spot"], rate, atm, leg.growth, expiry_moment(exp), now)
    out["market_implied_profit_at_expiration"] = odds["p_profit"]
    # On the horizon date (2.5): the chance of a profit then, and what option prices imply the position is worth
    # in expectation, from the whole distribution (the same weighting), next to what it costs. Each contract is
    # read on its own implied volatility, the one its price implies: on the at-the-money one the skew alone
    # would make out-of-the-money calls look cheap and puts dear, which is not an edge.
    at_h = market_odds([leg], thesis["spot"], rate, leg.vol, leg.growth, when["horizon"], now)
    exp_pnl = implied_expectation([leg], thesis["spot"], rate, leg.vol, leg.growth, when["horizon"], now)
    out["market_implied_profit_at_horizon"] = at_h["p_profit"]
    out["implied_expected_pnl_at_horizon"] = exp_pnl
    out["implied_value_at_horizon"] = premium + exp_pnl if exp_pnl is not None else None
    # The spread of outcomes then (2.5.1): profit or loss at the 10th, 50th and 90th percentile stock price of
    # the same distribution. Unknown without a usable volatility, never 0.
    known_vol = leg.vol is not None and math.isfinite(leg.vol) and leg.vol > 0
    for name, key in (("p10", "pnl_low"), ("p50", "pnl_mid"), ("p90", "pnl_high")):
        v = at_h.get(key) if known_vol else None
        out[f"implied_pnl_{name}_at_horizon"] = float(v) if v is not None and math.isfinite(v) else None

    if thesis["horizon_date"] > exp_day:
        avoid.append(f"Expires {exp_day:%b} {exp_day.day}, before the {thesis['horizon']}-day horizon.")
    if row.get("spread_pct") is not None and row["spread_pct"] > WIDE_SPREAD_PCT:
        avoid.append(f"Wide spread: {row['spread_pct']:.0f}% of the midpoint, ${spread_cost:,.0f} to get in and out.")
    if (row.get("open_interest") or 0) < MIN_OPEN_INTEREST:
        avoid.append(f"Little open interest ({row.get('open_interest') or 0:,.0f} contracts): hard to exit at a fair price.")
    if gain <= 0:
        avoid.append("Loses money even if the stock reaches target 1 by the horizon: time decay outruns the move.")
    if budget is not None and premium > budget:
        avoid.append(f"Costs ${premium:,.0f}, above the ${budget:,.0f} budget.")
    if earnings and earnings.get("date"):
        ed = date.fromisoformat(earnings["date"])
        if now.astimezone(NY).date() <= ed <= exp_day:
            flags.append(f"Earnings on {ed:%b} {ed.day} fall before expiration: implied volatility usually drops after "
                         "the report, which cuts the option's value even if the stock moves your way.")
    iv_low = next(m for m in matrix if m["when"] == "horizon" and m["iv_shift"] == IV_SHIFTS[0])
    if gain > 0 and iv_low["target_1"] <= 0:
        flags.append("Pays at the target only if implied volatility holds: five points lower and it loses.")
    if out["breakeven_at_horizon"] is not None and thesis["direction"] * (out["breakeven_at_horizon"] - thesis["target_1"]) > 0:
        flags.append("Needs the stock beyond target 1 by the horizon to break even.")
    out.update({"avoid": avoid, "flags": flags, "usable": not avoid})
    return out


def rank(rows: list[dict[str, Any]], thesis: dict[str, Any]) -> list[dict[str, Any]]:
    """One contract per category, with why it won."""
    usable = [r for r in rows if r["usable"]]
    picks = []

    def pick(category: str, pool: list[dict[str, Any]], key, why) -> None:
        if pool:
            best = max(pool, key=key)
            picks.append({"category": category, "option_symbol": best["option_symbol"], "contract": best, "why": why(best)})

    pick("BEST THESIS MATCH", usable, lambda r: r["return_on_premium"] or -1,
         lambda r: f"Makes {100 * r['return_on_premium']:.0f}% on the premium if {thesis['symbol']} reaches "
                   f"${thesis['target_1']:,.2f} by {thesis['horizon_date']:%b} {thesis['horizon_date'].day}.")
    pick("LOWEST COST", usable, lambda r: -r["premium"],
         lambda r: f"The cheapest at ${r['premium']:,.0f} that still makes money at target 1 by the horizon.")
    pick("BEST RISK-ADJUSTED", [r for r in usable if r["reward_to_risk"]], lambda r: r["reward_to_risk"],
         lambda r: f"Gains ${r['at_target']:,.0f} at target 1 for ${abs(r['if_invalidated']):,.0f} lost if "
                   f"invalidated: {r['reward_to_risk']:.2f} to 1.")
    conservative = [r for r in usable if r["dte"] >= 2 * thesis["horizon_calendar_days"]]
    pick("MOST CONSERVATIVE", conservative, lambda r: -(r["loss_share_of_premium"] or 1),
         lambda r: f"Loses the smallest share of its premium if invalidated ({100 * r['loss_share_of_premium']:.0f}%) "
                   f"and has {r['dte']} days left, more than twice the horizon.")
    return picks


PERCENTILE_Z = (("p10", -1.2815516), ("p50", 0.0), ("p90", 1.2815516))     # as simulate.market_odds


def stock_outlook(*, spot: float, direction: int, target_1: float, invalidation: float, vol: float, growth: float,
                  t: float, entry: float | None = None, expiration: str | None = None) -> dict[str, Any]:
    """The stock on the horizon date under one lognormal: centred on one forward (spot grown at the carry the
    option chain implies) at at-the-money volatility `vol` over `t` years. Every figure here, the chances
    included, uses that forward and those weights, so Decide never shows two chances for one trade.

    The chance the shares are ahead is measured against `entry` when one is given (the typed entry), and
    against today's price otherwise. The chances about the target and the invalidation do not depend on it."""
    fwd = spot * math.exp(growth * t)
    sd = vol * math.sqrt(t)
    prices = fwd * np.exp(-0.5 * sd * sd + sd * Z)
    basis = float(entry) if entry is not None else spot
    ahead = (prices - basis) * direction > 0
    at = lambda z: fwd * math.exp(-0.5 * sd * sd + sd * z)   # noqa: E731 - the stock price at a z-score
    return {"reach_target_1_by_horizon": prob_finish_beyond(fwd, target_1, t, vol, above=direction > 0),
            "beyond_invalidation_at_horizon": prob_finish_beyond(fwd, invalidation, t, vol, above=direction < 0),
            "forward_at_horizon": fwd,
            "expected_price_at_horizon": float((WEIGHTS * prices).sum()),
            "stock_profit_at_horizon": float(WEIGHTS[ahead].sum()),
            "stock_profit_basis": "entry" if entry is not None else "spot", "stock_profit_price": basis,
            "range_1sd_at_horizon": {"low": at(-1.0), "high": at(1.0)},
            "stock_percentiles_at_horizon": {**{name: at(z) for name, z in PERCENTILE_Z}, "expiration": expiration}}


def evaluate(*, symbol: str, direction: int, spot: float, analyses: list[Any], target_1: float, target_2: float,
             invalidation: float, horizon: int, rate: float, contracts: int = 1, budget: float | None = None,
             earnings: dict[str, Any] | None = None, now: datetime | None = None,
             only: list[tuple[str, float, str]] | None = None, entry: float | None = None) -> dict[str, Any]:
    """Evaluate the calls (direction +1) or puts (-1) in `analyses` (ChainAnalysis per expiration)
    for the thesis. `only` restricts it to given (option_type, strike, expiration) contracts. `entry`, the
    stock price the share trade is planned at, only changes the chance the shares are ahead."""
    if direction not in (1, -1):
        raise OptionsEvaluationError("direction must be BULLISH or BEARISH")
    if direction * (target_1 - spot) <= 0:
        raise OptionsEvaluationError(f"Target 1 (${target_1:,.2f}) must be {'above' if direction > 0 else 'below'} the "
                                     f"stock price (${spot:,.2f}) for a {'bullish' if direction > 0 else 'bearish'} thesis")
    if direction * (spot - invalidation) <= 0:
        raise OptionsEvaluationError(f"The invalidation price (${invalidation:,.2f}) must be "
                                     f"{'below' if direction > 0 else 'above'} the stock price (${spot:,.2f})")
    now = now or datetime.now(NY)
    today = now.astimezone(NY).date()
    h_day = horizon_date(today, horizon)
    kind = "call" if direction > 0 else "put"
    atm = None
    if analyses:
        near = min(analyses, key=lambda a: a.expiration)
        ivs = [c.get("iv") for c in near.contracts if c.get("iv") and abs(c["strike"] / near.spot - 1) < 0.03]
        atm = float(np.median(ivs)) if ivs else None
    thesis = {"symbol": symbol, "direction": direction, "spot": spot, "target_1": target_1, "target_2": target_2,
              "invalidation": invalidation, "horizon": horizon, "horizon_date": h_day,
              "horizon_calendar_days": (h_day - today).days, "atm_iv": atm}
    rows: list[dict[str, Any]] = []
    for a in analyses:
        cands = [c for c in a.contracts if c["option_type"] == kind]
        if only is not None:
            cands = [c for c in cands if any(o[0] == kind and abs(o[1] - c["strike"]) < 1e-6 and o[2] == a.expiration
                                             for o in only)]
        else:
            cands = [c for c in cands if c.get("delta") is not None and DELTA_RANGE[0] <= abs(c["delta"]) <= DELTA_RANGE[1]]
            cands.sort(key=lambda c: abs(c["strike"] - spot))
            cands = cands[:MAX_CANDIDATES_PER_EXPIRY]
        for c in sorted(cands, key=lambda c: c["strike"]):
            rows.append(value_contract(c, a, thesis, now, rate, contracts, earnings, budget))
    if only is not None and len(rows) < len(only):
        found = {(r["option_type"].lower(), r["strike"], r["expiration"]) for r in rows}
        missing = [o for o in only if (o[0], o[1], o[2]) not in found]
        if missing:
            raise OptionsEvaluationError("Not in the chain: " + ", ".join(f"{e} {k:g} {t}" for t, k, e in missing))
    t_h = max(years_between(now, eval_moment(h_day, now)), 1 / 365)
    market = None
    if atm:
        near = min(analyses, key=lambda a: a.expiration)
        growth = math.log(near.forward / near.spot) / near.t if near.t > 0 and near.spot and near.forward else 0.0
        # One time and one forward for every stock figure: the chances, the expected price, the chance the
        # shares are ahead, the range and the percentiles (stock_outlook).
        market = {"atm_iv": atm, "atm_expiration": near.expiration,
                  **stock_outlook(spot=spot, direction=direction, target_1=target_1, invalidation=invalidation, vol=atm,
                                  growth=growth, t=t_h, entry=entry, expiration=near.expiration),
                  "note": "Market-implied from option prices (lognormal, at-the-money volatility, no drift beyond the "
                          "forward). Not a forecast, and it is the chance of being past the level on that date, not of "
                          "touching it before."}
    return {"version": OPTIONS_EVALUATOR_VERSION, "symbol": symbol,
            "thesis": {**{k: v for k, v in thesis.items() if k != "horizon_date"}, "horizon_date": h_day.isoformat(),
                       "direction": "BULLISH" if direction > 0 else "BEARISH", "contract_type": kind.upper(),
                       "contracts": contracts, "budget": budget},
            "picks": rank(rows, thesis), "contracts": rows,
            "avoid": [{"option_symbol": r["option_symbol"], "expiration": r["expiration"], "strike": r["strike"],
                       "reasons": r["avoid"]} for r in rows if r["avoid"]],
            "market": market, "expirations": [a.expiration for a in analyses],
            "exit_rule": f"Exit when {symbol} closes {'below' if direction > 0 else 'above'} ${invalidation:,.2f}, "
                         "whatever the option is quoted at.",
            "notes": ["Entry at the midpoint; the spread cost is shown apart.",
                      "Values use each strike's own implied volatility (sticky strike) and European exercise.",
                      "Only bought calls and puts are evaluated; spreads and sold options are not."]}
