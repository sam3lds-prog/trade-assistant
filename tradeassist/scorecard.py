"""Risk checks for buying a single option contract.

Each check returns pass, warn, fail or info with the number behind it. The checks
look for known ways long-option trades lose money (wide spreads, expensive
volatility, fast time decay, long odds, oversized positions). Passing every check
does not make a trade safe: the whole premium is always at risk.
"""

from __future__ import annotations

import math
from datetime import date, timedelta
from typing import Any

import pandas as pd

from .chain import ChainAnalysis
from .config import Settings
from .optionmath import days_to_expiry, expected_move, prob_finish_beyond

# Thresholds are judgement calls, kept in one place so they are easy to see and change.
THRESHOLDS = {
    "spread_warn_pct": 5.0,
    "open_interest_pass": 500, "open_interest_warn": 100,
    "volume_pass": 100, "volume_warn": 10,
    "iv_hv_warn": 1.3, "iv_hv_fail": 1.75,
    "iv_rank_warn": 70.0, "iv_rank_min_days": 60,
    "theta_warn_pct": 1.0, "theta_fail_pct": 3.0,
    "dte_warn": 21,
    "breakeven_warn": 1.0, "breakeven_fail": 1.5,
    "deep_itm_delta": 0.90,
    "earnings_estimate_margin_days": 10,
}

ORDER = {"fail": 0, "warn": 1, "info": 2, "pass": 3}


def _check(group: str, key: str, label: str, status: str, value: str, detail: str,
           blocking: bool = False) -> dict[str, Any]:
    return {"group": group, "key": key, "label": label, "status": status, "value": value,
            "detail": detail, "blocking": blocking and status == "fail"}


def trend_snapshot(enriched: pd.DataFrame) -> dict[str, Any]:
    """Latest indicator readings and the direction each one leans."""
    if enriched.empty:
        return {"as_of": None, "readings": [], "bullish": 0, "bearish": 0}
    last = enriched.iloc[-1]
    readings: list[dict[str, Any]] = []

    def add(name: str, value: Any, lean: str, text: str) -> None:
        readings.append({"name": name, "value": None if value is None or pd.isna(value) else float(value),
                         "lean": lean, "text": text})

    if not pd.isna(last.get("macd")) and not pd.isna(last.get("macd_signal")):
        up = last["macd"] > last["macd_signal"]
        add("MACD", last["macd"] - last["macd_signal"], "bullish" if up else "bearish",
            "MACD line is above its signal line" if up else "MACD line is below its signal line")
    if not pd.isna(last.get("rsi")):
        r = last["rsi"]
        lean = "bearish" if r >= 70 else "bullish" if r <= 30 else "neutral"
        text = ("RSI is overbought (70 or more)" if r >= 70 else
                "RSI is oversold (30 or less)" if r <= 30 else "RSI is in its normal range")
        add("RSI (14)", r, lean, text)
    if not pd.isna(last.get("sma50")) and not pd.isna(last.get("sma200")):
        up = last["close"] > last["sma200"]
        add("200-day average", last["close"] / last["sma200"] - 1.0, "bullish" if up else "bearish",
            "Price is above its 200-day average" if up else "Price is below its 200-day average")
    if not pd.isna(last.get("sma50")):
        up = last["close"] > last["sma50"]
        add("50-day average", last["close"] / last["sma50"] - 1.0, "bullish" if up else "bearish",
            "Price is above its 50-day average" if up else "Price is below its 50-day average")
    if "struct_state" in enriched.columns:
        st = int(last["struct_state"])
        add("Swing structure", st, "bullish" if st > 0 else "bearish" if st < 0 else "neutral",
            "Higher highs and higher lows" if st > 0 else "Lower highs and lower lows" if st < 0
            else "Swings are mixed: no clear trend")
    if not pd.isna(last.get("adx")):
        a = last["adx"]
        add("ADX (14)", a, "neutral", "A trend is in force (ADX above 25)" if a > 25 else
            "No strong trend (ADX 25 or below)")
    if not pd.isna(last.get("pos52")):
        add("52-week range position", last["pos52"], "neutral",
            f"Price sits {last['pos52']:.0f}% of the way up its 52-week range")
    bullish = sum(1 for r in readings if r["lean"] == "bullish")
    bearish = sum(1 for r in readings if r["lean"] == "bearish")
    finished = bool(last["bar_complete"]) if "bar_complete" in enriched.columns else True
    return {"as_of": last["date"].strftime("%Y-%m-%d"), "readings": readings,
            "bullish": bullish, "bearish": bearish,
            # Readings from today's unfinished bar can change by the close.
            "status": "CONFIRMED" if finished else "PROVISIONAL"}


def build_scorecard(row: dict[str, Any], analysis: ChainAnalysis, enriched: pd.DataFrame, settings: Settings,
                    limit_price: float, contracts: int, earnings: dict[str, Any] | None = None,
                    iv_rank: dict[str, Any] | None = None, open_risk: float = 0.0,
                    today: date | None = None, trend_validated: bool = False,
                    symbol_risk: float = 0.0) -> dict[str, Any]:
    """`open_risk` is premium already committed across the whole journal; `symbol_risk` is the
    part of it in this underlying, which counts toward the per-trade limit so that one idea
    cannot be split across several small tickets."""
    th = THRESHOLDS
    checks: list[dict[str, Any]] = []
    spot, forward, t = analysis.spot, analysis.forward, analysis.t
    is_call = row["option_type"] == "call"
    dte = days_to_expiry(row["expiration"], today)
    mid = row.get("mid")
    iv = row.get("iv") or row.get("model_vol")

    # --- liquidity -------------------------------------------------------
    spread_pct = row.get("spread_pct")
    if spread_pct is None:
        checks.append(_check("Liquidity", "spread", "Bid-ask spread", "fail", "no two-sided quote",
                             "Without both a bid and an ask the cost of getting in and out cannot be measured.",
                             blocking=True))
    else:
        status = ("pass" if spread_pct <= th["spread_warn_pct"] else
                  "warn" if spread_pct <= settings.max_spread_pct else "fail")
        round_trip = (row["ask"] - row["bid"]) * 100 * contracts
        checks.append(_check(
            "Liquidity", "spread", "Bid-ask spread", status, f"{spread_pct:.1f}% of the midpoint",
            f"Buying at the ask and selling at the bid would cost ${round_trip:,.0f} on {contracts} "
            f"contract{'s' if contracts != 1 else ''} before the stock moves at all. "
            f"Under {th['spread_warn_pct']:.0f}% is tight; over {settings.max_spread_pct:.0f}% is blocked.",
            blocking=True))
    oi = row.get("open_interest") or 0
    status = "pass" if oi >= th["open_interest_pass"] else "warn" if oi >= th["open_interest_warn"] else "fail"
    checks.append(_check("Liquidity", "open_interest", "Open interest", status, f"{oi:,.0f} contracts",
                         "Open interest is how many contracts exist. Thin contracts are harder to exit at a "
                         "fair price."))
    volume = row.get("volume") or 0
    status = "pass" if volume >= th["volume_pass"] else "warn" if volume >= th["volume_warn"] else "info"
    checks.append(_check("Liquidity", "volume", "Volume today", status, f"{volume:,.0f} contracts",
                         "Low volume early in the session is normal; low volume late in the day means few "
                         "people are trading this contract."))

    # --- volatility ------------------------------------------------------
    hv = None
    if not enriched.empty and not pd.isna(enriched["hv20"].iloc[-1]):
        hv = float(enriched["hv20"].iloc[-1])
    if iv and hv and hv > 0:
        ratio = iv / hv
        status = "pass" if ratio <= th["iv_hv_warn"] else "warn" if ratio <= th["iv_hv_fail"] else "fail"
        checks.append(_check(
            "Volatility", "iv_vs_hv", "Implied vs recent actual volatility", status,
            f"{iv:.0%} implied vs {hv:.0%} actual ({ratio:.2f}x)",
            "The option is priced for the stock to move this much. When implied volatility is far above "
            "what the stock has been doing, buyers are paying up, often ahead of a known event."))
    else:
        checks.append(_check("Volatility", "iv_vs_hv", "Implied vs recent actual volatility", "info",
                             "not available", "Needs an implied volatility and 20 days of price history."))
    enough = iv_rank and iv_rank.get("days", 0) >= th["iv_rank_min_days"] and iv_rank.get("rank") is not None
    if enough and not iv_rank.get("fresh", False):
        checks.append(_check("Volatility", "iv_rank", "Implied volatility rank", "info",
                             f"last recorded {iv_rank.get('as_of', 'some time ago')}",
                             "The newest implied-volatility record is not from today, so a rank would describe "
                             "an old price. Record today's value to see it."))
    elif enough:
        rank = iv_rank["rank"]
        status = "warn" if rank >= th["iv_rank_warn"] else "pass"
        checks.append(_check("Volatility", "iv_rank", "Implied volatility rank", status,
                             f"{rank:.0f} out of 100 over {iv_rank['days']} days",
                             f"Where the implied volatility recorded on {iv_rank['as_of']} sits in its own "
                             "recorded range. High rank means options are expensive relative to their own "
                             "history."))
    else:
        days = (iv_rank or {}).get("days", 0)
        checks.append(_check("Volatility", "iv_rank", "Implied volatility rank", "info",
                             f"building history ({days} of {th['iv_rank_min_days']} days)",
                             "Run the daily snapshot to record implied volatility; the rank appears once "
                             "there is enough history."))

    checks.append(_earnings_check(row, earnings, today))

    # --- time decay ------------------------------------------------------
    theta = row.get("theta")
    if theta is not None and mid:
        pct = 100.0 * abs(theta) / mid
        status = "pass" if pct < th["theta_warn_pct"] else "warn" if pct < th["theta_fail_pct"] else "fail"
        checks.append(_check("Time decay", "theta", "Daily time decay", status,
                             f"${abs(theta) * 100 * contracts:,.0f} a day ({pct:.1f}% of the premium)",
                             "What the position loses each day if the stock and volatility stand still. "
                             "Decay speeds up as expiration approaches."))
    else:
        checks.append(_check("Time decay", "theta", "Daily time decay", "info", "not available",
                             "No usable Greeks for this contract."))
    status = "fail" if dte < settings.min_dte else "warn" if dte < th["dte_warn"] else "pass"
    checks.append(_check("Time decay", "dte", "Days to expiration", status, f"{dte} days",
                         f"Under {th['dte_warn']} days the option decays quickly; under {settings.min_dte} "
                         "is blocked because there is little time to be right.", blocking=True))

    # --- odds --------------------------------------------------------------
    breakeven = row["strike"] + limit_price if is_call else row["strike"] - limit_price
    need = (breakeven - spot) if is_call else (spot - breakeven)
    need_pct = 100.0 * need / spot
    stock_move_pct = need_pct if is_call else -need_pct   # shown as the stock's move: negative for a put
    odds: dict[str, Any] = {"breakeven": breakeven, "move_needed": need, "move_needed_pct": need_pct}
    if iv:
        em = expected_move(spot, iv, t)
        ratio = need / em if em > 0 else math.inf
        p_profit = prob_finish_beyond(forward, breakeven, t, iv, above=is_call) if breakeven > 0 else 0.0
        odds.update({"expected_move": em, "expected_move_pct": 100.0 * em / spot,
                     "prob_profit_at_expiry": p_profit})
        status = "pass" if ratio <= th["breakeven_warn"] else "warn" if ratio <= th["breakeven_fail"] else "fail"
        direction = "rise" if is_call else "fall"
        checks.append(_check(
            "Odds", "breakeven", "Move needed to break even", status,
            f"{stock_move_pct:+.1f}% vs a typical move of ±{100.0 * em / spot:.1f}%",
            f"The stock must {direction} to ${breakeven:,.2f} by expiration for this trade to make money "
            f"if held to the end. The market's own pricing puts the chance of that near {p_profit:.0%}."))
    else:
        checks.append(_check("Odds", "breakeven", "Move needed to break even", "info", f"{stock_move_pct:+.1f}%",
                             "No implied volatility available to compare against."))
    delta = row.get("delta")
    if delta is not None:
        odds["delta"] = delta
        deep = abs(delta) >= th["deep_itm_delta"]
        checks.append(_check(
            "Odds", "delta", "Delta", "info", f"{delta:+.2f}",
            f"The option moves about ${abs(delta) * 100:,.0f} per contract for each $1 move in the stock. "
            "Delta is also a rough gauge of the chance of finishing in the money."
            + (" This contract is deep in the money: the model value ignores early exercise and is less "
               "reliable here." if deep else "")))

    # --- trend -----------------------------------------------------------
    trend = trend_snapshot(enriched)
    aligned = trend["bullish"] if is_call else trend["bearish"]
    against = trend["bearish"] if is_call else trend["bullish"]
    status = "warn" if against > aligned and against >= 2 else "info"
    caveat = ("" if trend_validated else
              " These indicators have not passed the pattern lab's tests on your data, so treat this as "
              "context, not evidence.")
    checks.append(_check("Trend", "trend", "Trend indicators", status,
                         f"{aligned} with the trade, {against} against",
                         "Counts MACD, RSI extremes, the 50- and 200-day averages and the swing structure." + caveat
                         + (" Today's daily bar is not finished, so these readings are provisional until the close."
                            if trend.get("status") == "PROVISIONAL" else "")))

    # --- sizing ----------------------------------------------------------
    premium = round(limit_price * 100 * contracts, 2)
    per_trade_cap = settings.account_size * settings.max_risk_per_trade_pct / 100.0
    open_cap = settings.account_size * settings.max_open_risk_pct / 100.0
    cent = 0.005  # compare money to the cent: 1.10 * 100 * 5 is 550.0000000000001 in floating point
    room = per_trade_cap - symbol_risk
    max_contracts = max(int((room + cent) // round(limit_price * 100, 2)), 0) if limit_price > 0 else 0
    position = premium + symbol_risk
    status = "pass" if position <= per_trade_cap + cent else "fail"
    already = (f" That includes ${symbol_risk:,.0f} already open or planned in {analysis.symbol}."
               if symbol_risk > 0 else "")
    checks.append(_check(
        "Sizing", "trade_risk", "Premium at risk", status,
        f"${position:,.0f} ({100.0 * position / settings.account_size:.1f}% of the account)",
        f"The most this position can lose.{already} Your limit is {settings.max_risk_per_trade_pct:g}% per "
        f"trade (${per_trade_cap:,.0f}), which allows {max_contracts} more "
        f"contract{'s' if max_contracts != 1 else ''} at this price.", blocking=True))
    status = "pass" if open_risk + premium <= open_cap + cent else "fail"
    checks.append(_check(
        "Sizing", "open_risk", "Total premium at risk", status,
        f"${open_risk + premium:,.0f} with this trade",
        f"Open positions and saved plans in your journal, plus this trade. Your limit is "
        f"{settings.max_open_risk_pct:g}% of the account (${open_cap:,.0f}).", blocking=True))

    counts = {s: sum(1 for c in checks if c["status"] == s) for s in ("pass", "warn", "fail", "info")}
    if counts["fail"]:
        verdict = "fail"
        summary = f"{counts['fail']} red flag{'s' if counts['fail'] != 1 else ''}. Fix or skip this trade."
    elif counts["warn"]:
        verdict = "warn"
        summary = f"{counts['warn']} caution{'s' if counts['warn'] != 1 else ''}. Read them before trading."
    else:
        verdict = "pass"
        summary = "No red flags found. The full premium is still at risk."
    return {"verdict": verdict, "summary": summary, "counts": counts, "checks": checks, "odds": odds,
            "trend": trend, "dte": dte, "premium": premium, "max_contracts": max_contracts,
            "blocking": [c["key"] for c in checks if c["blocking"]]}


def _earnings_check(row: dict[str, Any], earnings: dict[str, Any] | None, today: date | None) -> dict[str, Any]:
    today = today or date.today()
    expiration = date.fromisoformat(row["expiration"])
    if not earnings or not earnings.get("date"):
        return _check("Volatility", "earnings", "Earnings before expiration", "warn", "date unknown",
                      "No earnings date is available. Look it up and enter it: buying options through an "
                      "earnings report is where studies find retail traders lose the most.")
    when = date.fromisoformat(earnings["date"])
    source = earnings.get("source", "unknown source")
    estimated = bool(earnings.get("estimated"))
    margin = THRESHOLDS["earnings_estimate_margin_days"] if estimated else 0
    label = f"{when.strftime('%b')} {when.day}, {when.year} ({'estimate' if estimated else 'entered by you'})"
    if estimated and earnings.get("overdue"):
        return _check("Volatility", "earnings", "Earnings before expiration", "fail", f"{label}, overdue",
                      f"Past filings point to a report around this date and none has been filed yet ({source}). "
                      "It is probably late and could come any day. Enter the confirmed date to settle it.",
                      blocking=True)
    if when < today - timedelta(days=margin):
        text = ("The estimated date has already passed, so the next report date is unknown."
                if estimated else "The earnings date you entered has passed. Enter the next one.")
        return _check("Volatility", "earnings", "Earnings before expiration", "warn", f"{label} is in the past",
                      text + " Look up the next date before trading.")
    if when - timedelta(days=margin) <= expiration:
        detail = (f"Earnings fall on or before expiration ({source}). Option prices swell before the report "
                  "and usually drop right after it, whichever way the stock moves.")
        if estimated:
            detail += (f" The date is estimated from past filings and can be off by a week or more, so "
                       f"anything within {margin} days of expiration is treated as a conflict. "
                       "Enter the confirmed date to settle it.")
        return _check("Volatility", "earnings", "Earnings before expiration", "fail", label, detail, blocking=True)
    return _check("Volatility", "earnings", "Earnings before expiration", "pass", label,
                  f"The next report is expected after this option expires ({source}).")

