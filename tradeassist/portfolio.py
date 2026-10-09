"""Portfolio risk across every open position: exposure, Greeks, concentration, correlation,
stress tests and the daily loss limit.

Exposure is measured in dollars of stock: shares times price for stock (negative when short),
and delta times 100 times contracts times the stock price for a call or put. Beta to SPY
(from the last six months of daily returns on finished bars) turns each into SPY-equivalent
dollars, so a long in a jumpy stock and a short in a quiet one are not treated as a hedge.

Open risk is measured per position on a stated basis (RISK_BASES): to the invalidation price
(the distance times the shares for stock, the option's model value at that stock price for a
call or put, never more than its value now); the whole premium for a bought option without
one; the dollars at risk you entered, for stock without an invalidation price; otherwise
unknown, as it is for stock already through its invalidation price. `risk_coverage` adds up
what is known and says whether that covers every position.

A bad day: 1.645 standard deviations of daily P&L from the covariance of the last six months
of returns (the normal approximation), and the same exposures applied to each of those days'
actual returns (historical 5th percentile, expected shortfall beyond it, worst day), all on
the days every holding has a return. Deltas only; estimates, never a maximum loss. Returns are
only taken between consecutive closes, so a missing day is never a zero return. Stress tests reprice everything with the simulator's model under a market move
(each stock moving its beta times the move), a volatility shift and optional moves for named
stocks.

Daily loss: DAILY_LOSS_MODE is REALIZED_PLUS_UNREALIZED by default, which counts today's
change in open positions as well as what was closed today, so a large open loss stops new
trades before it is realised. REALIZED_ONLY counts only closed trades.

Unknown is never zero. A position without a price, a delta or a previous close has those
figures as None; totals add up what is known and say what they leave out (`coverage`,
`unknown_counts`, `coverage_status`), so a partial sum is never presented as the whole.
"""

from __future__ import annotations

import math
from datetime import datetime
from typing import Any

import numpy as np
import pandas as pd

from .optionmath import NY
from .positions import Fill, account, adjusted_fills
from .sectors import SECTOR_FUNDS
from .simulate import Leg, leg_value

DAILY_LOSS_MODES = ("REALIZED_PLUS_UNREALIZED", "REALIZED_ONLY")
INDEX_FUNDS = ("SPY", "QQQ", "DIA", "IWM")
CORRELATION_DAYS = 126
HIGH_CORRELATION = 0.8
MAX_POSITION_SHARE = 0.25        # of the account, by exposure
MAX_SECTOR_SHARE = 0.40          # of gross exposure
DEFAULT_SCENARIOS = [
    {"name": "Market down 5%, volatility up 5 points", "market_move_pct": -5, "iv_shift_points": 5},
    {"name": "Market down 10%, volatility up 10 points", "market_move_pct": -10, "iv_shift_points": 10},
    {"name": "Crash: market down 20%, volatility up 20 points", "market_move_pct": -20, "iv_shift_points": 20},
    {"name": "Market up 5%, volatility down 2 points", "market_move_pct": 5, "iv_shift_points": -2},
    {"name": "Market up 10%, volatility down 4 points", "market_move_pct": 10, "iv_shift_points": -4},
    {"name": "Volatility up 10 points, market flat", "market_move_pct": 0, "iv_shift_points": 10},
    {"name": "Largest position gaps 20% against you", "market_move_pct": 0, "iv_shift_points": 0, "largest_gap_pct": 20},
]


def finite(v: Any) -> float | None:
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if math.isfinite(f) else None


MIN_SAMPLES = 40                 # overlapping daily returns needed for a beta, a correlation or a covariance
STALE_RUN = 3                    # this many unchanged closes in a row look like a stale feed, not a quiet stock
Z_95 = 1.645


def returns_frame(bars: dict[str, pd.DataFrame], days: int = CORRELATION_DAYS) -> pd.DataFrame:
    """Daily returns, one column per symbol, on the union of their dates.

    A return is only computed between two consecutive trading days that both have a close: a
    day missing for one stock leaves NaN for it on that day and the next, rather than a zero
    return (which carrying the last close forward would invent) or a two-day return set beside
    the market's one-day return. Everything that uses these returns pairs them up on the days
    both have."""
    closes = {}
    for sym, df in bars.items():
        if df is None or df.empty:
            continue
        s = df.set_index(pd.to_datetime(df["date"]))["close"].astype(float)
        s = s[~s.index.duplicated(keep="last")]
        closes[sym] = s.where(s > 0)                 # a zero or negative close is bad data, not a price
    if not closes:
        return pd.DataFrame()
    frame = pd.DataFrame(closes).sort_index()
    return frame.pct_change(fill_method=None).iloc[1:].tail(days)


def return_quality(rets: pd.DataFrame, market: str = "SPY") -> dict[str, dict[str, Any]]:
    """For each symbol: how many usable returns, how many of the market's days are missing, and
    whether runs of unchanged closes suggest a stale price feed."""
    out = {}
    market_days = rets[market].notna() if market in rets.columns else None
    for sym in rets.columns:
        col = rets[sym]
        usable = int(col.notna().sum())
        missing = int((market_days & col.isna()).sum()) if market_days is not None else 0
        zeros = (col == 0).astype(int)
        run = longest = 0
        for z in zeros:
            run = run + 1 if z else 0
            longest = max(longest, run)
        issues = []
        if usable < MIN_SAMPLES:
            issues.append(f"only {usable} daily returns, fewer than the {MIN_SAMPLES} needed")
        if missing:
            issues.append(f"{missing} of the market's trading days have no close")
        if longest >= STALE_RUN:
            issues.append(f"{longest + 1} unchanged closes in a row: the price may be stale")
        out[sym] = {"returns": usable, "missing_days": missing, "longest_unchanged_run": longest,
                    "issues": issues, "reliable": not issues}
    return out


def beta_stats(rets: pd.DataFrame, market: str = "SPY") -> dict[str, dict[str, Any]]:
    """Beta to the market for each symbol, from the days both have a return (the same days for
    the covariance and the market's variance), with the sample size. Fewer than MIN_SAMPLES
    overlapping days gives no beta."""
    out: dict[str, dict[str, Any]] = {}
    if market not in rets.columns:
        return out
    for sym in rets.columns:
        if sym == market:
            out[sym] = {"beta": 1.0, "samples": int(rets[market].notna().sum()), "status": "OK"}
            continue
        pair = rets[[sym, market]].dropna()
        var = float(pair[market].var()) if len(pair) > 1 else 0.0
        if len(pair) >= MIN_SAMPLES and var > 0:
            out[sym] = {"beta": float(pair[sym].cov(pair[market]) / var), "samples": len(pair), "status": "OK"}
        else:
            out[sym] = {"beta": None, "samples": len(pair), "status": "INSUFFICIENT",
                        "reason": f"{len(pair)} overlapping days with {market}, fewer than {MIN_SAMPLES}"}
    return out


def betas(rets: pd.DataFrame, market: str = "SPY") -> dict[str, float]:
    """The betas that could be measured (beta_stats), by symbol."""
    return {k: v["beta"] for k, v in beta_stats(rets, market).items() if v["beta"] is not None}


def one_day_risk(rets: pd.DataFrame, exposures: dict[str, float]) -> dict[str, Any]:
    """How much the exposures might lose in one day, two ways, on the days every holding has a
    return (one consistent sample):

    - parametric: 1.645 standard deviations of daily P&L from the covariance of returns, the
      normal approximation, about one day in twenty;
    - historical: the same exposures applied to each past day's actual returns; the 5th
      percentile loss and the average of the days worse than it (expected shortfall), plus the
      worst day.

    Both use deltas only, assume the sample is typical, and are estimates of a bad day, never a
    maximum loss."""
    cols = [s for s in exposures if s in rets.columns]
    gross = sum(abs(v) for v in exposures.values())
    left_out = [{"symbol": s, "exposure": exposures[s], "reason": "no daily returns in the price history"}
                for s in exposures if s not in cols and abs(exposures[s]) > 0]
    if not cols:
        return {"available": False, "reason": "no price history for the holdings", "left_out": left_out}
    sample = rets[cols].dropna()
    if len(sample) < MIN_SAMPLES:
        return {"available": False, "samples": len(sample),
                "reason": f"only {len(sample)} days on which every holding has a return; {MIN_SAMPLES} are needed"}
    d = np.array([exposures[s] for s in cols])
    var = float(d @ sample.cov().to_numpy() @ d)
    pnl = sample.to_numpy() @ d
    cut = float(np.quantile(pnl, 0.05))
    tail = pnl[pnl <= cut]
    out = {"available": True, "samples": len(sample), "symbols": cols, "left_out": left_out, "complete": not left_out,
           "share_of_exposure_measured": (sum(abs(exposures[s]) for s in cols) / gross) if gross else 1.0,
           "parametric_95": Z_95 * math.sqrt(var) if var > 0 else 0.0,
           "historical_95": max(0.0, -cut), "expected_shortfall_95": max(0.0, float(-tail.mean())) if len(tail) else None,
           "worst_day": max(0.0, float(-pnl.min())),
           "note": f"From {len(sample)} trading days with deltas only. Estimates of a bad day (about one in twenty), "
                   "not a maximum: larger losses happen, and gaps and volatility changes are not included."}
    if left_out:
        share = 100 * (1 - out["share_of_exposure_measured"])
        out["note"] += (f" It leaves out {', '.join(x['symbol'] for x in left_out)} ({share:.0f}% of the exposure), "
                        "which has no usable price history, so the whole portfolio's bad day is not known.")
    return out


EXPOSURE_FIELDS = ("dollar_delta", "market_value", "beta_dollars", "theta", "vega", "gamma_dollars",
                   "risk_to_invalidation", "open_risk", "notional", "premium_at_risk", "liquidation_value")
# How each position's open risk is measured (risk_coverage):
RISK_BASES = {
    "INVALIDATION": "the loss if the stock went to the invalidation price now",
    "PREMIUM": "the whole premium of a bought option, the most it can lose",
    "CONFIGURED": "the dollars at risk you entered for it (an assumption you made, not a measurement)",
    "BEYOND_INVALIDATION": "the stock is already through the invalidation price, so the plan no longer bounds the loss",
    "UNKNOWN": "cannot be measured",
}
# What each total measures. They are different things and are never added to one another.
DEFINITIONS = {
    "dollar_delta": "Delta-equivalent stock dollars: shares times the stock price, or delta x multiplier x contracts x "
                    "the stock price for an option. Signed: positive gains when the stock rises.",
    "gross_exposure": "Sum of each position's delta dollars without their signs: a long and a short of the same size "
                      "add up instead of cancelling.",
    "net_exposure": "Sum of each position's delta dollars with their signs: the directional bet left after longs and "
                    "shorts offset.",
    "market_value": "What the open positions are worth at the analytical mark: positive for what you hold, negative "
                    "for stock you owe on a short. For an option, its premium.",
    "notional": "Shares, or contracts x multiplier, times the stock price, unsigned and not delta-adjusted.",
    "premium_at_risk": "For bought options, their current value: the most they can still lose.",
    "liquidation_value": "What closing now would roughly realise: the bid for something held, the ask for a short. "
                         "An estimate from the quote, never a promised fill.",
    "open_risk": "Each position's risk by its basis (risk_coverage.bases), added up only where it is known.",
}


def exposure(view: dict[str, Any], beta: float | None) -> dict[str, Any]:
    """Dollar exposure, value, Greeks and risk for one position view (Desk.view).

    Every position comes back with the same fields. A figure that cannot be worked out is None,
    never 0: `priced` says whether the position has a current price and stock price, and
    `missing` and `missing_fields` say what is unknown and why. `risk_basis` says how `open_risk`
    was measured (RISK_BASES)."""
    led, mark = view["ledger"], view.get("mark") or {}
    qty = led["open_quantity"]
    sign = 1 if view["direction"] == "LONG" else -1
    under = finite(mark.get("underlying"))
    price = finite(mark.get("price"))
    mult = finite(view.get("multiplier")) or (100.0 if view["instrument_type"] == "OPTION" else 1.0)
    out: dict[str, Any] = {"id": view["id"], "symbol": view["symbol"], "description": view["description"],
                           "instrument_type": view["instrument_type"], "direction": view["direction"],
                           "open_quantity": qty, "multiplier": mult, "price": price, "underlying": under, "beta": beta,
                           "beta_assumed": beta is None,
                           "bias": view["bias"], "thesis_status": view["thesis_check"]["status"],
                           "invalidation_price": view.get("invalidation_price"), "missing": [], "missing_fields": [],
                           "risk_basis": "UNKNOWN", "risk_reason": None, "beyond_invalidation": False,
                           **{k: None for k in EXPOSURE_FIELDS}}
    if price is None:
        out["missing_fields"].append("price")
    if under is None:
        out["missing_fields"].append("underlying_price")
    out["priced"] = price is not None and under is not None
    if not out["priced"]:
        out["missing"].append(mark.get("error") or "no current price")
        out["risk_reason"] = "no current price"
        return out
    fresh = mark.get("freshness") or {}
    out["quote_freshness"] = fresh.get("freshness")
    if fresh.get("freshness") in ("STALE", "UNKNOWN"):
        out["missing"].append(f"price is {'stale' if fresh['freshness'] == 'STALE' else 'of unknown age'}"
                              + (f" ({'; '.join(fresh.get('warnings', [])[:2])})" if fresh.get("warnings") else ""))
    liq = finite(mark.get("liquidation_price"))
    out["liquidation_value"] = sign * liq * mult * qty if liq is not None else None
    out["notional"] = abs(qty * mult * under)
    inv = finite(view.get("invalidation_price"))
    configured = finite(view.get("risk_amount"))
    if view["instrument_type"] == "STOCK":
        out["dollar_delta"] = sign * qty * under
        out["market_value"] = sign * qty * price
        out["theta"] = out["vega"] = out["gamma_dollars"] = 0.0
        if inv:
            out["risk_to_invalidation"] = max(0.0, sign * (under - inv)) * qty
            if sign * (under - inv) <= 0:
                out["beyond_invalidation"] = True
                out["risk_basis"] = "BEYOND_INVALIDATION"
                out["risk_reason"] = (f"already through its invalidation price of {inv:,.2f}: the plan says exit, and "
                                      "until then the loss is not bounded by it")
            else:
                out["risk_basis"], out["open_risk"] = "INVALIDATION", out["risk_to_invalidation"]
        elif configured:
            out["risk_basis"], out["open_risk"] = "CONFIGURED", configured
            out["missing"].append("no invalidation price: its risk is the dollar amount you entered")
        else:
            out["missing"].append("no invalidation price, so its risk is unknown")
            out["missing_fields"].append("invalidation_price")
            out["risk_reason"] = "no invalidation price and no dollar risk entered"
    else:
        leg = view["legs"][0]
        delta = finite(mark.get("delta"))
        out["market_value"] = price * mult * qty
        out["premium_at_risk"] = price * mult * qty
        if delta is None:
            out["missing"].append("no delta from the chain, so its exposure is unknown")
            out["missing_fields"].append("delta")
        else:
            out["dollar_delta"] = delta * mult * qty * under
        for key in ("theta", "vega"):
            v = finite(mark.get(key))
            out[key] = v * mult * qty if v is not None else None
        gamma = finite(mark.get("gamma"))
        # Change in dollar delta for a 1% move.
        out["gamma_dollars"] = gamma * mult * qty * under * under / 100.0 if gamma is not None else None
        missing_greeks = [k for k in ("theta", "vega", "gamma") if finite(mark.get(k)) is None]
        if missing_greeks:
            out["missing_fields"].extend(missing_greeks)
            out["missing"].append(f"no {', '.join(missing_greeks)} from the chain")
        if mark.get("expired"):
            out["risk_reason"] = "past expiration with contracts still open: record how it ended"
            out["missing"].append("past expiration with no recorded outcome (expired worthless or exercised)")
        out["dte"] = mark.get("dte")
        out["iv"] = finite(mark.get("iv"))
        out["option"] = {"option_type": leg["option_type"], "strike": leg["strike"], "expiration": leg["expiration"]}
        out["model_vol"], out["growth"], out["rate"] = finite(mark.get("model_vol")), mark.get("growth"), mark.get("rate")
        bias = 1 if leg["option_type"] == "CALL" else -1
        if inv and bias * (under - inv) <= 0:
            out["beyond_invalidation"] = True
        change = option_change(out, inv, 0.0, datetime.now(NY)) if inv and not out["beyond_invalidation"] else None
        if mark.get("expired"):
            pass                                    # its risk depends on how it ended, which is not recorded yet
        elif change is not None:
            out["risk_to_invalidation"] = min(price, max(0.0, -change)) * mult * qty
            out["risk_basis"], out["open_risk"] = "INVALIDATION", out["risk_to_invalidation"]
        else:
            # A bought option can lose no more than what it is worth, whatever the stock does.
            out["risk_to_invalidation"] = price * mult * qty
            out["risk_basis"], out["open_risk"] = "PREMIUM", price * mult * qty
            if not inv:
                out["missing"].append("no invalidation price: the whole premium is counted as at risk")
    if out["dollar_delta"] is not None:
        out["beta_dollars"] = out["dollar_delta"] * (beta if beta is not None else 1.0)
        if beta is None:
            out["missing"].append("no beta to SPY: counted one for one")
    return out


def unreadable_exposure(u: dict[str, Any]) -> dict[str, Any]:
    """A position whose fills no longer account (Desk.unreadable), as an exposure that measures
    nothing: it counts in every coverage figure as missing, never as zero."""
    reason = f"its fills no longer add up ({u.get('error')})"
    return {"id": u["id"], "symbol": u["symbol"], "description": u["description"], "instrument_type": u["instrument_type"],
            "direction": u["direction"], "open_quantity": None, "multiplier": u.get("multiplier"), "price": None,
            "underlying": None, "beta": None, "beta_assumed": True, "bias": 0, "thesis_status": "UNKNOWN",
            "invalidation_price": None, "missing": [reason], "missing_fields": ["fills"], "risk_basis": "UNKNOWN",
            "risk_reason": reason, "beyond_invalidation": False, "priced": False, "unreadable": True,
            **{k: None for k in EXPOSURE_FIELDS}}


def risk_coverage(exps: list[dict[str, Any]], limit: float, limit_pct: float) -> dict[str, Any]:
    """Open risk by how it was measured, what could not be measured, and a conclusion against the
    limit that says whether it covers every position."""
    bases = {b: 0.0 for b in ("INVALIDATION", "PREMIUM", "CONFIGURED")}
    unknown = []
    for x in exps:
        if x["open_risk"] is not None:
            bases[x["risk_basis"]] += x["open_risk"]
        else:
            unknown.append({"id": x["id"], "description": x["description"], "basis": x["risk_basis"],
                            "reason": x.get("risk_reason") or RISK_BASES[x["risk_basis"]]})
    total = sum(bases.values())
    n, k = len(exps), len(exps) - len(unknown)
    status = "COMPLETE" if not unknown else ("UNAVAILABLE" if k == 0 else "PARTIAL")
    if total > limit:
        within = False
        text = (f"Open risk is at least ${total:,.0f}, above the {limit_pct:g}% limit (${limit:,.0f})"
                + (f"; {len(unknown)} more position{'s are' if len(unknown) != 1 else ' is'} not measured." if unknown else "."))
    elif unknown:
        within = None
        text = (f"Open risk is ${total:,.0f} across the {k} of {n} positions it could be measured for, inside the "
                f"{limit_pct:g}% limit (${limit:,.0f}), but {len(unknown)} position{'s are' if len(unknown) != 1 else ' is'} "
                "not measured, so it is not known whether the whole is inside the limit.")
    else:
        within = True
        text = (f"Open risk is ${total:,.0f} across all {n} position{'s' if n != 1 else ''}, inside the {limit_pct:g}% "
                f"limit (${limit:,.0f}).") if n else "No open positions."
    return {"status": status, "positions": n, "measured": k, "total_known": total, "limit": limit,
            "within_limit": within, "includes_all_positions": not unknown, "bases": bases, "unknown": unknown,
            "basis_words": RISK_BASES, "conclusion": text}


def known_sum(rows: list[dict[str, Any]], key: str) -> tuple[float, int]:
    """Sum of a figure over the rows that have it, and how many rows do not."""
    vals = [r[key] for r in rows if r.get(key) is not None]
    return float(sum(vals)), len(rows) - len(vals)


def coverage(exps: list[dict[str, Any]]) -> dict[str, Any]:
    """How much of the portfolio the totals actually cover."""
    unpriced = [{"id": x["id"], "description": x["description"], "symbol": x["symbol"], "reasons": x["missing"][:1],
                 "missing_fields": x["missing_fields"]} for x in exps if not x["priced"]]
    no_exposure = [x["description"] for x in exps if x["priced"] and x["dollar_delta"] is None]
    stale = [{"id": x["id"], "description": x["description"], "freshness": x.get("quote_freshness")}
             for x in exps if x["priced"] and x.get("quote_freshness") in ("STALE", "UNKNOWN")]
    n, k = len(exps), len(exps) - len(unpriced)
    status = "COMPLETE" if not unpriced and not no_exposure else ("UNAVAILABLE" if n and k == 0 else "PARTIAL")
    if status == "COMPLETE":
        text = f"Every open position ({n}) has a current price." if n else "No open positions."
    else:
        parts = []
        if unpriced:
            names = "; ".join(f"{u['description']} ({u['reasons'][0]})" if u["reasons"] else u["description"]
                              for u in unpriced)
            parts.append(f"{len(unpriced)} position{'s do' if len(unpriced) != 1 else ' does'} not have a current price: "
                         f"{names}")
        if no_exposure:
            parts.append(f"no delta, so no exposure, for {'; '.join(no_exposure)}")
        text = ("Portfolio risk is incomplete because " + "; and ".join(parts) + ". The totals cover the other "
                f"{n - len(unpriced) - len(no_exposure)} of {n} positions only, so they understate or overstate the whole.")
    stale_text = ""
    if stale:
        stale_text = (f"{len(stale)} position{'s are' if len(stale) != 1 else ' is'} priced from a stale or undated quote ("
                      + "; ".join(x["description"] for x in stale) + "): their figures use that older price.")
    return {"status": status, "positions": n, "priced": k, "unpriced": unpriced, "no_exposure": no_exposure,
            "stale": stale, "stale_text": stale_text, "text": text}


def reprice_option(x: dict[str, Any], underlying: float, iv_shift: float, now: datetime) -> float | None:
    """Model value per share of a position's option at another stock price and volatility, with the
    chain's own forward carry, rate and the volatility its smile gives the strike."""
    o = x.get("option")
    vol = x.get("model_vol") or x.get("iv")
    if not o or vol is None:
        return None
    leg = Leg(o["option_type"].lower(), float(o["strike"]), o["expiration"], 1, 1, float(x["price"]), float(vol),
              float(x.get("growth") or 0.0))
    return float(leg_value(leg, np.array([underlying]), now, float(x.get("rate") or 0.04), iv_shift)[0])


def option_change(x: dict[str, Any], underlying: float, iv_shift: float, now: datetime) -> float | None:
    """Change in value per share from now to the scenario, both on the model, so the model's own
    distance from the quoted midpoint does not show up as a profit or loss."""
    base = reprice_option(x, x["underlying"], 0.0, now)
    moved = reprice_option(x, underlying, iv_shift, now)
    if base is None or moved is None:
        return None
    return moved - base


def stress(exposures: list[dict[str, Any]], scenarios: list[dict[str, Any]], account_size: float,
           now: datetime | None = None) -> list[dict[str, Any]]:
    """Profit or loss of the priced positions under each scenario. Positions without a price
    cannot be repriced; they are listed in each scenario's `excluded` and `complete` is false."""
    now = now or datetime.now(NY)
    priced = [x for x in exposures if x["priced"]]
    excluded = [x["description"] for x in exposures if not x["priced"]]
    sized = [x for x in priced if x["dollar_delta"] is not None]
    largest = max(sized, key=lambda x: abs(x["dollar_delta"]), default=None)
    out = []
    for sc in scenarios:
        m = float(sc.get("market_move_pct", 0)) / 100.0
        iv = float(sc.get("iv_shift_points", 0)) / 100.0
        moves = {k.upper(): float(v) / 100.0 for k, v in (sc.get("moves") or {}).items()}
        gap = float(sc.get("largest_gap_pct", 0)) / 100.0
        rows, total, skipped = [], 0.0, list(excluded)
        for x in priced:
            move = (x["beta"] if x["beta"] is not None else 1.0) * m
            if x["symbol"] in moves:
                move = moves[x["symbol"]]
            if gap and largest is not None and x["symbol"] == largest["symbol"]:
                move = -gap * (1 if largest["dollar_delta"] > 0 else -1)
            # A stock cannot fall below zero, however large the beta times the market move.
            clamped = 1 + move < 0
            s_new = x["underlying"] * max(1 + move, 0.0)
            if x["instrument_type"] == "STOCK":
                sign = 1 if x["direction"] == "LONG" else -1
                pnl = sign * (s_new - x["underlying"]) * x["open_quantity"]
            else:
                ch = option_change(x, max(s_new, 1e-6), iv, now)
                # Never worth less than nothing: a bought option loses at most its value.
                if ch is not None:
                    pnl = max(ch, -x["price"]) * x["multiplier"] * x["open_quantity"]
                elif x["dollar_delta"] is not None:
                    pnl = x["dollar_delta"] * move
                    value = x.get("market_value")
                    if value is None and x.get("price") is not None:
                        value = x["price"] * x["multiplier"] * x["open_quantity"]
                    if value is not None:
                        pnl = max(pnl, -value)                # a bought option loses at most its value
                else:
                    skipped.append(x["description"])
                    continue
            rows.append({"id": x["id"], "symbol": x["symbol"], "description": x["description"],
                         "stock_move": max(move, -1.0), "pnl": pnl, "clamped": clamped})
            total += pnl
        worst = min(rows, key=lambda r: r["pnl"], default=None)
        out.append({"name": sc.get("name") or "Custom scenario", "market_move_pct": 100 * m, "iv_shift_points": 100 * iv,
                    "pnl": total, "share_of_account": total / account_size if account_size else None,
                    "worst": worst, "positions": rows, "excluded": skipped, "complete": not skipped})
    return out


def day_pnl(view: dict[str, Any], today_start: datetime) -> dict[str, Any]:
    """Today's realized and unrealized change for one position, from its fills, its mark and the
    previous close of what it holds (`view["prev_close"]`, worked out by the caller).

    Realized profit today (fills closed today, against their average cost) is always known from
    the fills. The unrealized change needs the price now for what is still open, and yesterday's
    close for what was held overnight; without them it is None, never zero, and `reason` says
    what was missing. Quantity held at midnight is measured from the previous close; quantity
    opened today from its fill, so a position bought last week and sold today counts only
    today's move."""
    fills = adjusted_fills([Fill(e["side"], e["quantity"], e["price"], e["fees"], e["executed_at"])
                            for e in view["executions"]], view.get("unit_adjustments") or [])
    before = [f for f in fills if datetime.fromisoformat(f.executed_at) < today_start]
    mult = view["multiplier"]
    now_led = account(view["direction"], mult, fills, allow_reopen=True)
    prev_led = account(view["direction"], mult, before, allow_reopen=True)
    weight = max(prev_led.open_quantity * (prev_led.cost_basis or 0.0),
                 max(now_led.open_quantity, now_led.closed_quantity - prev_led.closed_quantity) * (now_led.cost_basis or 0.0))
    base = {"weight": abs(weight * mult)}           # capital at cost the day's figure is about
    if len(before) == len(fills) and now_led.open_quantity <= 0:
        return {**base, "realized": 0.0, "unrealized_change": 0.0, "known": True}        # nothing happened today
    realized = now_led.realized_pnl - prev_led.realized_pnl
    mark = view.get("mark") or {}
    price = finite(mark.get("price"))
    prev = finite(view.get("prev_close"))
    reasons = []
    if now_led.open_quantity > 0 and price is None:
        err = mark.get("error")
        reasons.append("no current price" + (f" ({err})" if err else ""))
    elif now_led.open_quantity > 0 and (mark.get("freshness") or {}).get("freshness") == "STALE":
        # A saved or old quote can say a loss is smaller (or larger) than it is: it never measures today.
        why = "; ".join((mark.get("freshness") or {}).get("warnings", [])[:1])
        reasons.append("the price is stale" + (f" ({why})" if why else ""))
    if prev_led.open_quantity > 0 and prev is None:
        reasons.append("no previous close" + (f" ({view['prev_reason']})" if view.get("prev_reason") else ""))
    if reasons:
        return {**base, "realized": realized, "unrealized_change": None, "known": False, "reason": " and ".join(reasons)}
    unreal_now = now_led.unrealized(price) if now_led.open_quantity > 0 else 0.0
    unreal_prev = prev_led.unrealized(prev) if prev_led.open_quantity > 0 else 0.0
    return {**base, "realized": realized, "unrealized_change": unreal_now - unreal_prev, "known": True,
            "prev_source": view.get("prev_source")}


def daily_loss(views: list[dict[str, Any]], mode: str, account_size: float, stop_pct: float,
               now: datetime | None = None, unreadable: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    """Today's result against the daily loss stop, kept apart from how much of it could be measured.

    `loss_status` (OK, WARN, STOP) is the known total against the stop: a measured loss that
    reaches the stop is a STOP whatever else is unknown. `coverage_status` (COMPLETE, PARTIAL,
    UNAVAILABLE) says whether every position's day was measured. `status` combines them for
    older callers: the loss status, except that an OK with anything unmeasured is INCOMPLETE,
    never a plain OK."""
    now = now or datetime.now(NY)
    start = now.astimezone(NY).replace(hour=0, minute=0, second=0, microsecond=0)
    realized = unreal = known_total = 0.0
    weight_all = weight_known = 0.0
    missing, evaluated, unreal_missing = [], 0, 0
    for v in views:
        d = day_pnl(v, start)
        realized += d["realized"]
        weight_all += d["weight"]
        if mode == "REALIZED_ONLY":
            known = True                       # closed trades only: the fills say it all
            known_total += d["realized"]
        else:
            known = d["known"]
            if known:
                known_total += d["realized"] + d["unrealized_change"]
        if d["unrealized_change"] is not None:
            unreal += d["unrealized_change"]
        else:
            unreal_missing += 1
        if known:
            evaluated += 1
            weight_known += d["weight"]
        else:
            missing.append({"id": v.get("id"), "description": v["description"], "reason": d.get("reason") or "unknown"})
    for u in unreadable or []:                 # positions whose fills no longer account: nothing about them is known
        missing.append({"id": u["id"], "description": u["description"], "reason": "its fills no longer add up"})
        unreal_missing += 1
    n = len(views) + len(unreadable or [])
    coverage_status = "COMPLETE" if not missing else ("UNAVAILABLE" if evaluated == 0 else "PARTIAL")
    cap = account_size * stop_pct / 100.0
    loss_status = "STOP" if known_total <= -cap else "WARN" if known_total <= -0.75 * cap else "OK"
    status = loss_status if loss_status != "OK" else ("OK" if coverage_status == "COMPLETE" else "INCOMPLETE")
    loss = f"${-known_total:,.0f}"
    unmeasured = ""
    if missing:
        unmeasured = (f" {len(missing)} of {n} position{'s' if n != 1 else ''} could not be measured ("
                      + "; ".join(f"{m['description']}: {m['reason']}" for m in missing) + ").")
    if loss_status == "STOP":
        text = (f"Today's {'measured ' if missing else ''}loss of {loss} has reached the daily stop of {stop_pct:g}% "
                f"(${cap:,.0f}). The rule is no new trades today.{unmeasured}")
    elif loss_status == "WARN":
        text = (f"Today's {'measured ' if missing else ''}loss of {loss} is three quarters of the way to the daily "
                f"stop (${cap:,.0f}).{unmeasured}")
    elif coverage_status == "COMPLETE":
        text = f"Today: {'+' if known_total >= 0 else '-'}${abs(known_total):,.0f} against a daily stop of ${cap:,.0f}."
    elif coverage_status == "PARTIAL":
        text = (f"Measured so far today: {'+' if known_total >= 0 else '-'}${abs(known_total):,.0f} against a daily stop "
                f"of ${cap:,.0f}, but it is not known whether the whole account is within the stop.{unmeasured}")
    else:
        text = f"Today's result could not be measured, so it is not known whether the account is within the daily stop.{unmeasured}"
    return {"mode": mode, "realized_today": realized, "unrealized_change_today": unreal,
            "unrealized_complete": unreal_missing == 0,
            "known_total": known_total, "counted": known_total,
            "stop": cap, "stop_pct": stop_pct, "loss_status": loss_status, "coverage_status": coverage_status,
            "status": status, "text": text,
            "positions": n, "positions_evaluated": evaluated,
            "positions_evaluated_pct": 100.0 * evaluated / n if n else 100.0,
            "capital_evaluated_pct": 100.0 * weight_known / weight_all if weight_all > 0 else (100.0 if not missing else None),
            "missing": missing, "unknown": [m["description"] for m in missing],
            "note": ("Counts closed trades and today's change in open positions." if mode == "REALIZED_PLUS_UNREALIZED"
                     else "Counts closed trades only; open losses do not count until they are closed.")}


def _by(exps: list[dict[str, Any]], key: str) -> list[dict[str, Any]]:
    """Net and gross delta dollars grouped by `key`: net lets longs and shorts offset, gross does
    not, so a hedged book still shows how big it is."""
    groups: dict[str, dict[str, float]] = {}
    for x in exps:
        if x["dollar_delta"] is None:
            continue
        g = groups.setdefault(x[key], {"net": 0.0, "gross": 0.0, "long": 0.0, "short": 0.0, "positions": 0})
        d = x["dollar_delta"]
        g["net"] += d
        g["gross"] += abs(d)
        g["long" if d > 0 else "short"] += d
        g["positions"] += 1
    rows = [{key: k, "exposure": v["net"], **v} for k, v in groups.items()]
    return sorted(rows, key=lambda r: -r["gross"])


def analyse(views: list[dict[str, Any]], bars: dict[str, pd.DataFrame], account_size: float, max_open_risk_pct: float,
            daily: dict[str, Any], sector_of, scenarios: list[dict[str, Any]] | None = None,
            unreadable: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    """Portfolio totals, concentration, correlation, stress tests and warnings.

    Totals are sums over the positions whose figure is known. `coverage` says which positions
    are left out and why, and every total that leaves something out is flagged, so a partial
    sum is never presented as the whole portfolio. Delta dollars, market value, notional,
    premium at risk and liquidation value are separate measures (DEFINITIONS) and are never
    added to one another."""
    rets = returns_frame(bars)
    stats = beta_stats(rets)
    quality = return_quality(rets) if not rets.empty else {}
    exps = [exposure(v, (stats.get(v["symbol"]) or {}).get("beta")) for v in views]
    exps += [unreadable_exposure(u) for u in unreadable or []]
    held = sorted({x["symbol"] for x in exps})
    cov_ = coverage(exps)
    sized = [x for x in exps if x["dollar_delta"] is not None]
    gross = sum(abs(x["dollar_delta"]) for x in sized)
    net = sum(x["dollar_delta"] for x in sized)
    beta_net, _ = known_sum(exps, "beta_dollars")
    risk_cap = account_size * max_open_risk_pct / 100.0
    rc = risk_coverage(exps, risk_cap, max_open_risk_pct)

    def sector_name(sym: str) -> str:
        if sym in INDEX_FUNDS:
            return "Index funds"
        fund = sym if sym in SECTOR_FUNDS else sector_of(sym)
        return SECTOR_FUNDS.get(fund, "Sector not known") if fund else "Sector not known"
    for x in exps:
        x["sector"] = sector_name(x["symbol"])
        st = stats.get(x["symbol"]) or {}
        x["beta_samples"] = st.get("samples")
        if x["priced"] and st.get("status") == "INSUFFICIENT":
            x["missing"].append(f"beta not measured: {st['reason']}")
    by_symbol, by_sector = _by(exps, "symbol"), _by(exps, "sector")
    net_by_symbol = {r["symbol"]: r["net"] for r in by_symbol}
    warnings: list[dict[str, str]] = []
    if cov_["status"] != "COMPLETE":
        warnings.append({"level": "warn", "code": "INCOMPLETE_PRICES", "text": cov_["text"]})
    if cov_["stale"]:
        warnings.append({"level": "warn", "code": "STALE_PRICES", "text": cov_["stale_text"]})
    if rc["within_limit"] is False:
        warnings.append({"level": "fail", "code": "OPEN_RISK", "text": rc["conclusion"]})
    elif rc["within_limit"] is None:
        warnings.append({"level": "warn", "code": "OPEN_RISK_UNKNOWN", "text": rc["conclusion"]})
    for r in by_symbol:
        if account_size and r["gross"] > MAX_POSITION_SHARE * account_size:
            offset = f" (net ${r['net']:,.0f} after longs and shorts offset)" if abs(r["net"]) < r["gross"] - 1e-6 else ""
            warnings.append({"level": "warn", "text": f"{r['symbol']} exposure of ${r['gross']:,.0f} is "
                                                      f"{100 * r['gross'] / account_size:.0f}% of the account{offset}."})
    for r in by_sector:
        if gross and r["gross"] > MAX_SECTOR_SHARE * gross and len(exps) > 1:
            offset = f"; net ${r['net']:,.0f}" if abs(r["net"]) < r["gross"] - 1e-6 else ""
            warnings.append({"level": "warn", "text": f"{r['sector']}: {100 * r['gross'] / gross:.0f}% of gross exposure{offset}."})
    for x in exps:
        if x["thesis_status"] == "INVALIDATED":
            warnings.append({"level": "fail", "text": f"{x['description']} is through its invalidation price."})
        if x.get("dte") is not None and x["dte"] <= 7:
            warnings.append({"level": "warn", "text": f"{x['description']} expires in {x['dte']} days: time decay is fastest now."})
        if x["priced"]:                            # unpriced positions are named in the coverage warning
            for m in x["missing"]:
                if m.startswith("price is stale") or m.startswith("price is of unknown age"):
                    continue                       # named in the stale-prices warning
                warnings.append({"level": "info", "text": f"{x['description']}: {m}."})
    for sym in held:
        q = quality.get(sym)
        if q and q["issues"]:
            warnings.append({"level": "info", "code": "RETURN_QUALITY",
                             "text": f"{sym} price history: {'; '.join(q['issues'])}. Its beta, correlation and the "
                                     "bad-day estimates are less reliable."})
    theta, theta_unknown = known_sum(exps, "theta")
    if account_size and theta < -0.002 * account_size:
        warnings.append({"level": "warn", "text": f"Options lose about ${-theta:,.0f} a day to time decay, "
                                                  f"{100 * -theta / account_size:.2f}% of the account."})
    corr = None
    pairs = []
    cols = [s for s in held if s in rets.columns] if not rets.empty else []
    if len(cols) >= 2:
        c = rets[cols].corr(min_periods=MIN_SAMPLES)
        cell = lambda a, b: None if pd.isna(c.loc[a, b]) else round(float(c.loc[a, b]), 2)   # noqa: E731
        corr = {"symbols": cols, "matrix": [[cell(a, b) for b in cols] for a in cols], "min_samples": MIN_SAMPLES}
        for i, a in enumerate(cols):
            for b in cols[i + 1:]:
                if pd.isna(c.loc[a, b]):
                    continue
                rho = float(c.loc[a, b])
                same_way = np.sign(net_by_symbol.get(a, 0)) == np.sign(net_by_symbol.get(b, 0))
                if rho >= HIGH_CORRELATION:
                    pairs.append({"a": a, "b": b, "correlation": rho, "same_direction": bool(same_way)})
                    if same_way:
                        warnings.append({"level": "warn", "text": f"{a} and {b} move together (correlation {rho:.2f}) "
                                                                  "and are held the same way: closer to one bet than two."})
    bad_day = one_day_risk(rets, net_by_symbol) if not rets.empty else {"available": False, "reason": "no price history"}
    if bad_day.get("available") and cov_["status"] != "COMPLETE":
        bad_day["complete"] = False
        bad_day["note"] += " Positions without a price or a delta are not included."
    if bad_day.get("available") and bad_day.get("left_out"):
        warnings.append({"level": "warn", "code": "BAD_DAY_PARTIAL", "text": "The bad-day estimate leaves out "
                         + ", ".join(x["symbol"] for x in bad_day["left_out"]) + ", which has no usable price history, so "
                         "it understates how much a bad day could cost."})
    for u in unreadable or []:
        warnings.insert(0, {"level": "fail", "code": "UNREADABLE_POSITION",
                            "text": f"{u['description']}: its fills no longer add up ({u.get('error')}), so it is left out "
                                    "of every figure on this page. Correct the fill on the Positions screen."})
    stress_rows = stress(exps, scenarios or DEFAULT_SCENARIOS, account_size)
    if daily.get("loss_status", daily["status"]) in ("STOP", "WARN"):
        warnings.insert(0, {"level": "fail" if daily.get("loss_status", daily["status"]) == "STOP" else "warn",
                            "code": "DAILY_LOSS", "text": daily["text"]})
    elif daily.get("coverage_status", "COMPLETE") != "COMPLETE":
        warnings.insert(0, {"level": "warn", "code": "DAILY_LOSS_INCOMPLETE", "text": daily["text"]})
    mv, mv_unknown = known_sum(exps, "market_value")
    vega, vega_unknown = known_sum(exps, "vega")
    gamma, gamma_unknown = known_sum(exps, "gamma_dollars")
    liq, liq_unknown = known_sum(exps, "liquidation_value")
    notional, _ = known_sum(exps, "notional")
    premium, _ = known_sum([x for x in exps if x["instrument_type"] == "OPTION"], "premium_at_risk")
    exposure_unknown = len(exps) - len(sized)
    n = len(exps)

    def total(value: float, unknown: int) -> float | None:
        """A total over positions none of which could be measured is unknown, not $0."""
        return None if n and unknown == n else value
    return {"positions": exps, "count": len(exps), "symbols": held, "coverage": cov_, "risk_coverage": rc,
            "totals": {"gross_exposure": total(gross, exposure_unknown), "net_exposure": total(net, exposure_unknown),
                       "spy_equivalent": total(beta_net, exposure_unknown),
                       "long_exposure": total(sum(x["dollar_delta"] for x in sized if x["dollar_delta"] > 0), exposure_unknown),
                       "short_exposure": total(sum(x["dollar_delta"] for x in sized if x["dollar_delta"] < 0), exposure_unknown),
                       "market_value": total(mv, mv_unknown),
                       "gross_market_value": total(sum(abs(x["market_value"]) for x in exps if x["market_value"] is not None),
                                                   mv_unknown),
                       "notional": notional, "option_premium_at_risk": premium,
                       "estimated_liquidation_value": total(liq, liq_unknown), "liquidation_complete": liq_unknown == 0,
                       "theta_per_day": total(theta, theta_unknown), "vega_per_point": total(vega, vega_unknown),
                       "gamma_dollars_per_1pct": total(gamma, gamma_unknown),
                       "open_risk": rc["total_known"], "open_risk_limit": risk_cap,
                       "open_risk_unknown": len(rc["unknown"]), "open_risk_complete": rc["includes_all_positions"],
                       "one_day_move_95": bad_day.get("parametric_95") if bad_day.get("available") else None,
                       "complete": cov_["status"] == "COMPLETE",
                       "unknown_counts": {"exposure": exposure_unknown, "market_value": mv_unknown,
                                          "theta": theta_unknown, "vega": vega_unknown, "gamma": gamma_unknown,
                                          "open_risk": len(rc["unknown"]), "liquidation": liq_unknown}},
            "by_symbol": by_symbol, "by_sector": by_sector, "bad_day": bad_day,
            "betas": stats, "return_quality": quality, "definitions": DEFINITIONS,
            "correlation": corr, "correlated_pairs": pairs, "stress": stress_rows, "daily_loss": daily,
            "warnings": warnings, "account_size": account_size}


__all__ = ["analyse", "daily_loss", "stress", "exposure", "coverage", "risk_coverage", "known_sum", "returns_frame",
           "beta_stats", "betas", "one_day_risk", "return_quality", "DEFAULT_SCENARIOS", "DAILY_LOSS_MODES", "DEFINITIONS"]
