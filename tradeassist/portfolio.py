"""Portfolio risk across every open position: exposure, Greeks, concentration, correlation,
stress tests and the daily loss limit.

Exposure is measured in dollars of stock: shares times price for stock (negative when short),
and delta times 100 times contracts times the stock price for a call or put. Beta to SPY
(from the last six months of daily returns on finished bars) turns each into SPY-equivalent
dollars, so a long in a jumpy stock and a short in a quiet one are not treated as a hedge.

Risk to invalidation is what each position would lose if its stock went to its invalidation
price now: the distance times the shares for stock, and the option's model value at that
stock price for a call or put (never more than its value now). Without an invalidation price
a stock position's risk is unknown and an option's is its whole value.

The one-day move estimate is 1.645 standard deviations of the portfolio's daily P&L, from
the covariance of the last six months of daily returns and the dollar exposures: a rough
"bad day, one day in twenty" figure that uses deltas only and assumes the past six months
are typical. Stress tests reprice everything with the simulator's model under a market move
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
from .positions import Fill, account
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


def returns_frame(bars: dict[str, pd.DataFrame], days: int = CORRELATION_DAYS) -> pd.DataFrame:
    closes = {}
    for sym, df in bars.items():
        if df is None or df.empty:
            continue
        s = df.set_index(pd.to_datetime(df["date"]))["close"].astype(float)
        closes[sym] = s[~s.index.duplicated(keep="last")]
    if not closes:
        return pd.DataFrame()
    frame = pd.DataFrame(closes).sort_index().ffill()
    return frame.pct_change().iloc[1:].tail(days)


def betas(rets: pd.DataFrame, market: str = "SPY") -> dict[str, float]:
    out = {}
    if market not in rets.columns:
        return out
    m = rets[market]
    var = float(m.var())
    for sym in rets.columns:
        if sym == market:
            out[sym] = 1.0
            continue
        pair = rets[[sym, market]].dropna()
        if len(pair) >= 40 and var > 0:
            out[sym] = float(pair[sym].cov(pair[market]) / pair[market].var())
    return out


EXPOSURE_FIELDS = ("dollar_delta", "market_value", "beta_dollars", "theta", "vega", "gamma_dollars",
                   "risk_to_invalidation")


def exposure(view: dict[str, Any], beta: float | None) -> dict[str, Any]:
    """Dollar exposure, Greeks and risk to invalidation for one position view (Desk.view).

    Every position comes back with the same fields. A figure that cannot be worked out is None,
    never 0: `priced` says whether the position has a current price and stock price, and
    `missing` and `missing_fields` say what is unknown and why."""
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
                           **{k: None for k in EXPOSURE_FIELDS}}
    if price is None:
        out["missing_fields"].append("price")
    if under is None:
        out["missing_fields"].append("underlying_price")
    out["priced"] = price is not None and under is not None
    if not out["priced"]:
        out["missing"].append(mark.get("error") or "no current price")
        return out
    inv = finite(view.get("invalidation_price"))
    if view["instrument_type"] == "STOCK":
        out["dollar_delta"] = sign * qty * under
        out["market_value"] = sign * qty * price
        out["theta"] = out["vega"] = out["gamma_dollars"] = 0.0
        out["risk_to_invalidation"] = (max(0.0, sign * (under - inv)) * qty) if inv else None
        if inv is None:
            out["missing"].append("no invalidation price, so its risk is unknown")
            out["missing_fields"].append("invalidation_price")
    else:
        leg = view["legs"][0]
        delta = finite(mark.get("delta"))
        out["market_value"] = price * mult * qty
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
        out["dte"] = mark.get("dte")
        out["iv"] = finite(mark.get("iv"))
        out["option"] = {"option_type": leg["option_type"], "strike": leg["strike"], "expiration": leg["expiration"]}
        out["model_vol"], out["growth"], out["rate"] = finite(mark.get("model_vol")), mark.get("growth"), mark.get("rate")
        if inv:
            change = option_change(out, inv, 0.0, datetime.now(NY))
            out["risk_to_invalidation"] = (min(price, max(0.0, -change)) * mult * qty) if change is not None \
                else price * mult * qty
        else:
            out["risk_to_invalidation"] = price * mult * qty
            out["missing"].append("no invalidation price: the whole premium is counted as at risk")
    if out["dollar_delta"] is not None:
        out["beta_dollars"] = out["dollar_delta"] * (beta if beta is not None else 1.0)
        if beta is None:
            out["missing"].append("no beta to SPY: counted one for one")
    return out


def known_sum(rows: list[dict[str, Any]], key: str) -> tuple[float, int]:
    """Sum of a figure over the rows that have it, and how many rows do not."""
    vals = [r[key] for r in rows if r.get(key) is not None]
    return float(sum(vals)), len(rows) - len(vals)


def coverage(exps: list[dict[str, Any]]) -> dict[str, Any]:
    """How much of the portfolio the totals actually cover."""
    unpriced = [{"id": x["id"], "description": x["description"], "symbol": x["symbol"], "reasons": x["missing"][:1],
                 "missing_fields": x["missing_fields"]} for x in exps if not x["priced"]]
    no_exposure = [x["description"] for x in exps if x["priced"] and x["dollar_delta"] is None]
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
    return {"status": status, "positions": n, "priced": k, "unpriced": unpriced, "no_exposure": no_exposure,
            "text": text}


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
            s_new = x["underlying"] * (1 + move)
            if x["instrument_type"] == "STOCK":
                sign = 1 if x["direction"] == "LONG" else -1
                pnl = sign * (s_new - x["underlying"]) * x["open_quantity"]
            else:
                ch = option_change(x, s_new, iv, now)
                # Never worth less than nothing: a bought option loses at most its value.
                if ch is not None:
                    pnl = max(ch, -x["price"]) * x["multiplier"] * x["open_quantity"]
                elif x["dollar_delta"] is not None:
                    pnl = x["dollar_delta"] * move
                else:
                    skipped.append(x["description"])
                    continue
            rows.append({"id": x["id"], "symbol": x["symbol"], "description": x["description"], "stock_move": move, "pnl": pnl})
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
    fills = [Fill(e["side"], e["quantity"], e["price"], e["fees"], e["executed_at"]) for e in view["executions"]]
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
    if prev_led.open_quantity > 0 and prev is None:
        reasons.append("no previous close")
    if reasons:
        return {**base, "realized": realized, "unrealized_change": None, "known": False, "reason": " and ".join(reasons)}
    unreal_now = now_led.unrealized(price) if now_led.open_quantity > 0 else 0.0
    unreal_prev = prev_led.unrealized(prev) if prev_led.open_quantity > 0 else 0.0
    return {**base, "realized": realized, "unrealized_change": unreal_now - unreal_prev, "known": True,
            "prev_source": view.get("prev_source")}


def daily_loss(views: list[dict[str, Any]], mode: str, account_size: float, stop_pct: float,
               now: datetime | None = None) -> dict[str, Any]:
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
    n = len(views)
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


def analyse(views: list[dict[str, Any]], bars: dict[str, pd.DataFrame], account_size: float, max_open_risk_pct: float,
            daily: dict[str, Any], sector_of, scenarios: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    """Portfolio totals, concentration, correlation, stress tests and warnings.

    Totals are sums over the positions whose figure is known. `coverage` says which positions
    are left out and why, and every total that leaves something out is flagged, so a partial
    sum is never presented as the whole portfolio."""
    rets = returns_frame(bars)
    beta = betas(rets)
    held = sorted({v["symbol"] for v in views})
    exps = [exposure(v, beta.get(v["symbol"])) for v in views]
    cov_ = coverage(exps)
    sized = [x for x in exps if x["dollar_delta"] is not None]
    gross = sum(abs(x["dollar_delta"]) for x in sized)
    net = sum(x["dollar_delta"] for x in sized)
    beta_net, _ = known_sum(exps, "beta_dollars")
    open_risk, risk_unknown = known_sum(exps, "risk_to_invalidation")
    risk_cap = account_size * max_open_risk_pct / 100.0
    by_symbol: dict[str, float] = {}
    by_sector: dict[str, float] = {}

    def sector_name(sym: str) -> str:
        if sym in INDEX_FUNDS:
            return "Index funds"
        fund = sym if sym in SECTOR_FUNDS else sector_of(sym)
        return SECTOR_FUNDS.get(fund, "Sector not known") if fund else "Sector not known"
    for x in exps:
        x["sector"] = sector_name(x["symbol"])
        if x["dollar_delta"] is None:
            continue
        by_symbol[x["symbol"]] = by_symbol.get(x["symbol"], 0.0) + x["dollar_delta"]
        by_sector[x["sector"]] = by_sector.get(x["sector"], 0.0) + x["dollar_delta"]
    warnings: list[dict[str, str]] = []
    if cov_["status"] != "COMPLETE":
        warnings.append({"level": "warn", "code": "INCOMPLETE_PRICES", "text": cov_["text"]})
    if open_risk > risk_cap:
        warnings.append({"level": "fail", "code": "OPEN_RISK",
                         "text": f"Risk to invalidation across open positions is ${open_risk:,.0f}, above the "
                                 f"{max_open_risk_pct:g}% limit (${risk_cap:,.0f})"
                                 + (f", and {risk_unknown} more position{'s have' if risk_unknown != 1 else ' has'} risk "
                                    "that could not be measured." if risk_unknown else ".")})
    elif risk_unknown:
        warnings.append({"level": "warn", "code": "OPEN_RISK_UNKNOWN",
                         "text": f"Risk to invalidation is ${open_risk:,.0f} for the positions it could be measured for, "
                                 f"inside the {max_open_risk_pct:g}% limit (${risk_cap:,.0f}), but {risk_unknown} "
                                 f"position{'s have' if risk_unknown != 1 else ' has'} unknown risk, so it is not known "
                                 "whether the total is inside the limit."})
    for sym, d in by_symbol.items():
        if account_size and abs(d) > MAX_POSITION_SHARE * account_size:
            warnings.append({"level": "warn", "text": f"{sym} exposure of ${abs(d):,.0f} is {100 * abs(d) / account_size:.0f}% "
                                                      "of the account."})
    for name, d in by_sector.items():
        if gross and abs(d) > MAX_SECTOR_SHARE * gross and len(exps) > 1:
            warnings.append({"level": "warn", "text": f"{name}: {100 * abs(d) / gross:.0f}% of gross exposure."})
    for x in exps:
        if x["thesis_status"] == "INVALIDATED":
            warnings.append({"level": "fail", "text": f"{x['description']} is through its invalidation price."})
        if x.get("dte") is not None and x["dte"] <= 7:
            warnings.append({"level": "warn", "text": f"{x['description']} expires in {x['dte']} days: time decay is fastest now."})
        if x["priced"]:                            # unpriced positions are named in the coverage warning
            for m in x["missing"]:
                warnings.append({"level": "info", "text": f"{x['description']}: {m}."})
    theta, theta_unknown = known_sum(exps, "theta")
    if account_size and theta < -0.002 * account_size:
        warnings.append({"level": "warn", "text": f"Options lose about ${-theta:,.0f} a day to time decay, "
                                                  f"{100 * -theta / account_size:.2f}% of the account."})
    corr = None
    pairs = []
    one_day = None
    if len(held) >= 1 and not rets.empty:
        cols = [s for s in held if s in rets.columns]
        if len(cols) >= 2:
            c = rets[cols].corr()
            corr = {"symbols": cols, "matrix": [[round(float(c.loc[a, b]), 2) for b in cols] for a in cols]}
            for i, a in enumerate(cols):
                for b in cols[i + 1:]:
                    rho = float(c.loc[a, b])
                    same_way = np.sign(by_symbol.get(a, 0)) == np.sign(by_symbol.get(b, 0))
                    if rho >= HIGH_CORRELATION:
                        pairs.append({"a": a, "b": b, "correlation": rho, "same_direction": bool(same_way)})
                        if same_way:
                            warnings.append({"level": "warn", "text": f"{a} and {b} move together (correlation {rho:.2f}) "
                                                                      "and are held the same way: closer to one bet than two."})
        known_cols = [s for s in cols if s in by_symbol]
        if known_cols:
            d = np.array([by_symbol[s] for s in known_cols])
            cov = rets[known_cols].cov().to_numpy()
            var = float(d @ cov @ d)
            one_day = 1.645 * math.sqrt(var) if var > 0 else 0.0
    stress_rows = stress(exps, scenarios or DEFAULT_SCENARIOS, account_size)
    if daily.get("loss_status", daily["status"]) in ("STOP", "WARN"):
        warnings.insert(0, {"level": "fail" if daily.get("loss_status", daily["status"]) == "STOP" else "warn",
                            "code": "DAILY_LOSS", "text": daily["text"]})
    elif daily.get("coverage_status", "COMPLETE") != "COMPLETE":
        warnings.insert(0, {"level": "warn", "code": "DAILY_LOSS_INCOMPLETE", "text": daily["text"]})
    mv, mv_unknown = known_sum(exps, "market_value")
    vega, vega_unknown = known_sum(exps, "vega")
    gamma, gamma_unknown = known_sum(exps, "gamma_dollars")
    exposure_unknown = len(exps) - len(sized)
    return {"positions": exps, "count": len(exps), "symbols": held, "coverage": cov_,
            "totals": {"gross_exposure": gross, "net_exposure": net, "spy_equivalent": beta_net,
                       "long_exposure": sum(x["dollar_delta"] for x in sized if x["dollar_delta"] > 0),
                       "short_exposure": sum(x["dollar_delta"] for x in sized if x["dollar_delta"] < 0),
                       "market_value": mv, "theta_per_day": theta,
                       "vega_per_point": vega,
                       "gamma_dollars_per_1pct": gamma,
                       "open_risk": open_risk, "open_risk_limit": risk_cap,
                       "open_risk_unknown": risk_unknown, "open_risk_complete": risk_unknown == 0,
                       "one_day_move_95": one_day,
                       "complete": cov_["status"] == "COMPLETE",
                       "unknown_counts": {"exposure": exposure_unknown, "market_value": mv_unknown,
                                          "theta": theta_unknown, "vega": vega_unknown, "gamma": gamma_unknown,
                                          "open_risk": risk_unknown}},
            "by_symbol": [{"symbol": k, "exposure": v} for k, v in sorted(by_symbol.items(), key=lambda kv: -abs(kv[1]))],
            "by_sector": [{"sector": k, "exposure": v} for k, v in sorted(by_sector.items(), key=lambda kv: -abs(kv[1]))],
            "correlation": corr, "correlated_pairs": pairs, "stress": stress_rows, "daily_loss": daily,
            "warnings": warnings, "account_size": account_size}


__all__ = ["analyse", "daily_loss", "stress", "exposure", "coverage", "known_sum", "DEFAULT_SCENARIOS", "DAILY_LOSS_MODES"]
