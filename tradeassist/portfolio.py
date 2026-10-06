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


def exposure(view: dict[str, Any], beta: float | None) -> dict[str, Any]:
    """Dollar exposure, Greeks and risk to invalidation for one position view (Desk.view)."""
    led, mark = view["ledger"], view.get("mark") or {}
    qty = led["open_quantity"]
    sign = 1 if view["direction"] == "LONG" else -1
    under = finite(mark.get("underlying"))
    price = finite(mark.get("price"))
    out: dict[str, Any] = {"id": view["id"], "symbol": view["symbol"], "description": view["description"],
                           "instrument_type": view["instrument_type"], "direction": view["direction"],
                           "open_quantity": qty, "price": price, "underlying": under, "beta": beta,
                           "bias": view["bias"], "thesis_status": view["thesis_check"]["status"],
                           "invalidation_price": view.get("invalidation_price"), "missing": []}
    if price is None or under is None:
        out["missing"].append(mark.get("error") or "no current price")
        out.update({"dollar_delta": 0.0, "market_value": 0.0, "risk_to_invalidation": None, "theta": 0.0, "vega": 0.0,
                    "gamma_dollars": 0.0})
        return out
    inv = finite(view.get("invalidation_price"))
    if view["instrument_type"] == "STOCK":
        out["dollar_delta"] = sign * qty * under
        out["market_value"] = sign * qty * price
        out["theta"] = out["vega"] = out["gamma_dollars"] = 0.0
        out["risk_to_invalidation"] = (max(0.0, sign * (under - inv)) * qty) if inv else None
        if inv is None:
            out["missing"].append("no invalidation price, so its risk is unknown")
    else:
        leg = view["legs"][0]
        mult = view["multiplier"]
        delta = finite(mark.get("delta"))
        if delta is None:
            out["missing"].append("no delta from the chain")
            delta = 0.0
        out["dollar_delta"] = delta * mult * qty * under
        out["market_value"] = price * mult * qty
        out["theta"] = (finite(mark.get("theta")) or 0.0) * mult * qty
        out["vega"] = (finite(mark.get("vega")) or 0.0) * mult * qty
        gamma = finite(mark.get("gamma")) or 0.0
        out["gamma_dollars"] = gamma * mult * qty * under * under / 100.0     # change in dollar delta for a 1% move
        out["dte"] = mark.get("dte")
        out["iv"] = finite(mark.get("iv"))
        out["option"] = {"option_type": leg["option_type"], "strike": leg["strike"], "expiration": leg["expiration"]}
        if inv:
            at_inv = reprice_option(out, inv, 0.0, datetime.now(NY))
            out["risk_to_invalidation"] = max(0.0, (price - at_inv) * mult * qty) if at_inv is not None else price * mult * qty
        else:
            out["risk_to_invalidation"] = price * mult * qty
            out["missing"].append("no invalidation price: the whole premium is counted as at risk")
    out["beta_dollars"] = out["dollar_delta"] * (beta if beta is not None else 1.0)
    return out


def reprice_option(x: dict[str, Any], underlying: float, iv_shift: float, now: datetime) -> float | None:
    """Model value per share of a position's option at another stock price and volatility."""
    o = x.get("option")
    if not o:
        return None
    vol = x.get("iv")
    if vol is None:
        return None
    leg = Leg(o["option_type"].lower(), float(o["strike"]), o["expiration"], 1, 1, float(x["price"]), float(vol), 0.0)
    return float(leg_value(leg, np.array([underlying]), now, 0.04, iv_shift)[0])


def stress(exposures: list[dict[str, Any]], scenarios: list[dict[str, Any]], account_size: float,
           now: datetime | None = None) -> list[dict[str, Any]]:
    now = now or datetime.now(NY)
    priced = [x for x in exposures if x["price"] is not None and x["underlying"] is not None]
    largest = max(priced, key=lambda x: abs(x["dollar_delta"]), default=None)
    out = []
    for sc in scenarios:
        m = float(sc.get("market_move_pct", 0)) / 100.0
        iv = float(sc.get("iv_shift_points", 0)) / 100.0
        moves = {k.upper(): float(v) / 100.0 for k, v in (sc.get("moves") or {}).items()}
        gap = float(sc.get("largest_gap_pct", 0)) / 100.0
        rows, total = [], 0.0
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
                v = reprice_option(x, s_new, iv, now)
                pnl = ((v - x["price"]) * 100 * x["open_quantity"]) if v is not None else x["dollar_delta"] * move
            rows.append({"id": x["id"], "symbol": x["symbol"], "description": x["description"], "stock_move": move, "pnl": pnl})
            total += pnl
        worst = min(rows, key=lambda r: r["pnl"], default=None)
        out.append({"name": sc.get("name") or "Custom scenario", "market_move_pct": 100 * m, "iv_shift_points": 100 * iv,
                    "pnl": total, "share_of_account": total / account_size if account_size else None,
                    "worst": worst, "positions": rows})
    return out


def day_pnl(view: dict[str, Any], today_start: datetime) -> dict[str, Any]:
    """Today's realized and unrealized change for one position, from its fills and marks."""
    fills = [Fill(e["side"], e["quantity"], e["price"], e["fees"], e["executed_at"]) for e in view["executions"]]
    before = [f for f in fills if datetime.fromisoformat(f.executed_at) < today_start]
    mult = view["multiplier"]
    now_led = account(view["direction"], mult, fills)
    prev_led = account(view["direction"], mult, before)
    realized = now_led.realized_pnl - prev_led.realized_pnl
    mark = view.get("mark") or {}
    price, prev = finite(mark.get("price")), finite(mark.get("prev_close"))
    unreal_now = now_led.unrealized(price) if (price is not None and now_led.open_quantity > 0) else 0.0
    if prev_led.open_quantity > 0:
        if prev is None:
            unreal_prev, known = unreal_now if price is not None else 0.0, False
        else:
            unreal_prev, known = prev_led.unrealized(prev), True
    else:
        unreal_prev, known = 0.0, True
    if price is None and now_led.open_quantity > 0:
        known = False
    return {"realized": realized, "unrealized_change": (unreal_now - unreal_prev) if known else 0.0, "known": known}


def daily_loss(views: list[dict[str, Any]], mode: str, account_size: float, stop_pct: float,
               now: datetime | None = None) -> dict[str, Any]:
    now = now or datetime.now(NY)
    start = now.astimezone(NY).replace(hour=0, minute=0, second=0, microsecond=0)
    realized = unreal = 0.0
    unknown = []
    for v in views:
        d = day_pnl(v, start)
        realized += d["realized"]
        unreal += d["unrealized_change"]
        if not d["known"]:
            unknown.append(v["description"])
    counted = realized + (unreal if mode == "REALIZED_PLUS_UNREALIZED" else 0.0)
    cap = account_size * stop_pct / 100.0
    status = "STOP" if counted <= -cap else "WARN" if counted <= -0.75 * cap else "OK"
    text = {"STOP": f"Today's loss of ${-counted:,.0f} has reached the daily stop of {stop_pct:g}% (${cap:,.0f}). "
                    "The rule is no new trades today.",
            "WARN": f"Today's loss of ${-counted:,.0f} is three quarters of the way to the daily stop (${cap:,.0f}).",
            "OK": f"Today: {'+' if counted >= 0 else '-'}${abs(counted):,.0f} against a daily stop of ${cap:,.0f}."}[status]
    return {"mode": mode, "realized_today": realized, "unrealized_change_today": unreal, "counted": counted,
            "stop": cap, "stop_pct": stop_pct, "status": status, "text": text,
            "unknown": unknown,
            "note": ("Counts closed trades and today's change in open positions." if mode == "REALIZED_PLUS_UNREALIZED"
                     else "Counts closed trades only; open losses do not count until they are closed.")}


def analyse(views: list[dict[str, Any]], bars: dict[str, pd.DataFrame], account_size: float, max_open_risk_pct: float,
            daily: dict[str, Any], sector_of, scenarios: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    rets = returns_frame(bars)
    beta = betas(rets)
    held = sorted({v["symbol"] for v in views})
    exps = [exposure(v, beta.get(v["symbol"])) for v in views]
    gross = sum(abs(x["dollar_delta"]) for x in exps)
    net = sum(x["dollar_delta"] for x in exps)
    beta_net = sum(x["beta_dollars"] for x in exps)
    risk_known = [x["risk_to_invalidation"] for x in exps if x["risk_to_invalidation"] is not None]
    open_risk = float(sum(risk_known))
    risk_cap = account_size * max_open_risk_pct / 100.0
    by_symbol: dict[str, float] = {}
    by_sector: dict[str, float] = {}
    def sector_name(sym: str) -> str:
        if sym in INDEX_FUNDS:
            return "Index funds"
        fund = sym if sym in SECTOR_FUNDS else sector_of(sym)
        return SECTOR_FUNDS.get(fund, "Sector not known") if fund else "Sector not known"
    for x in exps:
        by_symbol[x["symbol"]] = by_symbol.get(x["symbol"], 0.0) + x["dollar_delta"]
        name = sector_name(x["symbol"])
        x["sector"] = name
        by_sector[name] = by_sector.get(name, 0.0) + x["dollar_delta"]
    warnings: list[dict[str, str]] = []
    if open_risk > risk_cap:
        warnings.append({"level": "fail", "text": f"Risk to invalidation across open positions is ${open_risk:,.0f}, above "
                                                  f"the {max_open_risk_pct:g}% limit (${risk_cap:,.0f})."})
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
        for m in x["missing"]:
            warnings.append({"level": "info", "text": f"{x['description']}: {m}."})
    theta = sum(x["theta"] for x in exps)
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
        if cols:
            d = np.array([by_symbol.get(s, 0.0) for s in cols])
            cov = rets[cols].cov().to_numpy()
            var = float(d @ cov @ d)
            one_day = 1.645 * math.sqrt(var) if var > 0 else 0.0
    stress_rows = stress(exps, scenarios or DEFAULT_SCENARIOS, account_size)
    if daily["status"] != "OK":
        warnings.insert(0, {"level": "fail" if daily["status"] == "STOP" else "warn", "text": daily["text"]})
    return {"positions": exps, "count": len(exps), "symbols": held,
            "totals": {"gross_exposure": gross, "net_exposure": net, "spy_equivalent": beta_net,
                       "long_exposure": sum(x["dollar_delta"] for x in exps if x["dollar_delta"] > 0),
                       "short_exposure": sum(x["dollar_delta"] for x in exps if x["dollar_delta"] < 0),
                       "market_value": sum(x["market_value"] for x in exps), "theta_per_day": theta,
                       "vega_per_point": sum(x["vega"] for x in exps),
                       "gamma_dollars_per_1pct": sum(x["gamma_dollars"] for x in exps),
                       "open_risk": open_risk, "open_risk_limit": risk_cap,
                       "open_risk_unknown": sum(1 for x in exps if x["risk_to_invalidation"] is None),
                       "one_day_move_95": one_day},
            "by_symbol": [{"symbol": k, "exposure": v} for k, v in sorted(by_symbol.items(), key=lambda kv: -abs(kv[1]))],
            "by_sector": [{"sector": k, "exposure": v} for k, v in sorted(by_sector.items(), key=lambda kv: -abs(kv[1]))],
            "correlation": corr, "correlated_pairs": pairs, "stress": stress_rows, "daily_loss": daily,
            "warnings": warnings, "account_size": account_size}


__all__ = ["analyse", "daily_loss", "stress", "exposure", "DEFAULT_SCENARIOS", "DAILY_LOSS_MODES"]
