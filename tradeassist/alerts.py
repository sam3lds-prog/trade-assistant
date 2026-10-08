"""Alerts: things about open positions and the account that deserve a look now.

Alerts only. Nothing here places, changes or cancels an order, sends a message or acts in
any way; the app lists the alerts and you decide.

    INVALIDATED        the stock is through the position's invalidation price
    TARGET_REACHED     the stock has reached target 1 or target 2
    TREND_AGAINST      the trend engine's confirmed state now points against the position
    EXPIRING           a call or put expires within five days
    EARNINGS_SOON      earnings within three trading days for a stock held
    DAILY_LOSS         today's loss has reached three quarters of the daily stop, or the stop
    OPEN_RISK          risk to invalidation across positions is above the limit
    OPEN_RISK_UNKNOWN  some positions' risk could not be measured, so the limit check is incomplete
    DAILY_LOSS_UNKNOWN some positions' change today could not be measured
    PROVISIONAL        a trend alert that rests on today's unfinished bar (shown, marked so)
    UNRESOLVED_EXPIRATION an option past expiration with contracts open and no recorded outcome
    BORROW_UNKNOWN     a short whose borrow fees are not recorded (or not for every day)
"""

from __future__ import annotations

from datetime import date
from typing import Any

import numpy as np

from .trend import STATE_WORDS

EXPIRY_DAYS = 5
EARNINGS_DAYS = 3
SEVERITY = {"fail": 0, "warn": 1, "info": 2}


def _alert(kind: str, level: str, text: str, **extra: Any) -> dict[str, Any]:
    return {"kind": kind, "level": level, "text": text, **extra}


def position_alerts(view: dict[str, Any], trend: dict[str, Any] | None, earnings: dict[str, Any] | None,
                    today: date) -> list[dict[str, Any]]:
    out = []
    d = view["description"]
    tc = view["thesis_check"]["status"]
    if tc == "INVALIDATED":
        out.append(_alert("INVALIDATED", "fail", f"{d}: {view['thesis_check']['text']}", position_id=view["id"]))
    elif tc in ("TARGET_1_REACHED", "TARGET_2_REACHED"):
        out.append(_alert("TARGET_REACHED", "info", f"{d}: {view['thesis_check']['text']}", position_id=view["id"]))
    if trend:
        for key, status in (("confirmed", "CONFIRMED"), ("provisional", "PROVISIONAL")):
            day = trend.get(key)
            if not day:
                continue
            against = (view["bias"] > 0 and day["state"] in ("DOWN", "STRONG_DOWN")) or \
                      (view["bias"] < 0 and day["state"] in ("UP", "STRONG_UP"))
            if against:
                when = "today's bar so far reads" if status == "PROVISIONAL" else "the trend engine now reads"
                out.append(_alert("TREND_AGAINST", "warn", f"{d}: {when} {STATE_WORDS[day['state']].lower()}, against the "
                                                            "position.", position_id=view["id"], status=status))
                break
    if (view.get("lifecycle") or {}).get("state") == "UNRESOLVED":
        out.append(_alert("UNRESOLVED_EXPIRATION", "warn", f"{d} is past its expiration with contracts still open. Record "
                                                           "whether it expired worthless or was exercised; it is not "
                                                           "assumed either way.", position_id=view["id"]))
    fin = view.get("financing") or {}
    if fin.get("borrow_status") in ("UNKNOWN", "PARTIAL") and view.get("status") in ("OPEN", "PARTIALLY_CLOSED"):
        out.append(_alert("BORROW_UNKNOWN", "info", f"{d}: {fin['borrow_text']} Record the fees your broker charged to see "
                                                    "the short's net result.", position_id=view["id"]))
    if view["instrument_type"] == "OPTION" and view.get("legs"):
        exp = date.fromisoformat(view["legs"][0]["expiration"])
        days = (exp - today).days
        if 0 <= days <= EXPIRY_DAYS:
            out.append(_alert("EXPIRING", "warn", f"{d} expires in {days} day{'s' if days != 1 else ''}: decide whether to "
                                                  "close or let it expire.", position_id=view["id"]))
    if earnings and earnings.get("date"):
        when = date.fromisoformat(earnings["date"])
        if today <= when:
            n = int(np.busday_count(today, when))
            if n <= EARNINGS_DAYS:
                est = " (estimated)" if earnings.get("estimated") else ""
                out.append(_alert("EARNINGS_SOON", "warn", f"{view['symbol']} reports earnings on {when:%b} {when.day}{est}; "
                                                           f"{d} is held through it.", position_id=view["id"]))
    return out


def account_alerts(daily: dict[str, Any], open_risk: float | None, risk_limit: float | None,
                   open_risk_unknown: int = 0) -> list[dict[str, Any]]:
    out = []
    loss = daily.get("loss_status", daily.get("status")) if daily else None
    if loss in ("STOP", "WARN"):
        out.append(_alert("DAILY_LOSS", "fail" if loss == "STOP" else "warn", daily["text"]))
    if daily and daily.get("coverage_status", "COMPLETE") != "COMPLETE":
        missing = daily.get("missing") or [{"description": d, "reason": "unknown"} for d in daily.get("unknown", [])]
        out.append(_alert("DAILY_LOSS_UNKNOWN", "warn", "Today's change is not counted toward the daily stop for "
                                                        + "; ".join(f"{m['description']} ({m['reason']})" for m in missing)
                                                        + ", so it is not known whether the account is within the stop."))
    if open_risk is not None and risk_limit and open_risk > risk_limit:
        out.append(_alert("OPEN_RISK", "fail", f"Risk to invalidation across open positions is ${open_risk:,.0f}, above the "
                                               f"${risk_limit:,.0f} limit."))
    elif open_risk_unknown:
        n = open_risk_unknown
        out.append(_alert("OPEN_RISK_UNKNOWN", "warn", f"Risk to invalidation could not be measured for {n} open "
                                                       f"position{'s' if n != 1 else ''} (no current price, or a stock "
                                                       "position without an invalidation price), so it is not known "
                                                       "whether open risk is inside the limit."))
    return out


def sort_alerts(alerts: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return sorted(alerts, key=lambda a: (SEVERITY.get(a["level"], 3), a["kind"]))
