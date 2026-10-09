"""Journal analytics: how the closed trades actually went, and what habits cost or earned.

Every closed unified position counts (stock long and short, calls and puts, and the option
journal's trades through their mirrors). For each trade:

- profit after fees, R multiple (profit over the initial risk, see positions.initial_risk),
  days held;
- MAE and MFE: the worst and best the *stock* went against and for the trade while it was
  held, from daily highs and lows from the day after entry to the exit day, as a percentage
  of the entry and in R (the distance to the invalidation price is 1R). Option prices have no
  daily history here, so for calls and puts these are measured on the stock;
- context at entry: the trend state and market regime from the ENTRY snapshot;
- behaviour: held past the invalidation price (a daily close beyond it before the exit),
  exited before target 1 with the stock never invalidated, added to a losing position, risk
  above the per-trade limit, traded against the trend, overrode a ticket limit (option
  journal).

Totals: win rate, average win and loss, profit factor (gross wins over gross losses),
expectancy per trade in dollars and in R, largest win and loss, longest losing streak,
equity curve and its deepest drawdown, and every figure broken down by setup, direction,
instrument, trend state and regime at entry, holding time and behaviour. Under 20 closed
trades the numbers are flagged as unreliable: a few trades move them a lot.
"""

from __future__ import annotations

from collections import defaultdict
from datetime import datetime
from typing import Any, Callable

import numpy as np
import pandas as pd

from .optionmath import NY

MIN_TRADES = 20
HOLD_BUCKETS = ((0, 1, "same day"), (1, 5, "1 to 4 days"), (5, 20, "5 to 19 days"), (20, 60, "20 to 59 days"),
                (60, 10_000, "60 days or more"))
UP_STATES = ("STRONG_UP", "UP")
DOWN_STATES = ("STRONG_DOWN", "DOWN")


def _date(iso: str | None):
    return datetime.fromisoformat(iso).astimezone(NY).date() if iso else None


def snapshot(pos: dict[str, Any], kind: str) -> dict[str, Any] | None:
    """The effective snapshot of this kind (snapshots.py): superseded ones are history, not context."""
    snaps = [s for s in pos.get("snapshots", []) if s["snapshot_type"] == kind and s.get("superseded_at") is None]
    return snaps[0] if snaps else None


def entry_context(pos: dict[str, Any]) -> tuple[dict[str, Any] | None, str | None]:
    """The ENTRY snapshot when it shows what could have been known at entry, else None and why.
    A snapshot built from data after the fill (uses_later_data) is hindsight: it is shown on the
    position, but never counted as the trend or regime at entry."""
    s = snapshot(pos, "ENTRY")
    if s is None:
        return None, "not recorded"
    if s.get("uses_later_data"):
        return None, "recorded with hindsight"
    return s, None


def excursions(pos: dict[str, Any], bars: pd.DataFrame | None, until: str | None = None) -> dict[str, Any]:
    """MAE and MFE on the stock while the position was held, in percent and in R."""
    if bars is None or bars.empty or not pos.get("opened_at"):
        return {"available": False}
    start = _date(pos["opened_at"])
    end = _date(until or pos.get("closed_at")) or datetime.now(NY).date()
    d = pd.to_datetime(bars["date"]).dt.date
    window = bars[(d > start) & (d <= end)]
    entry_snap = entry_context(pos)[0] or {}
    if pos["instrument_type"] == "STOCK":
        entry, basis = pos["ledger"]["average_entry_price"], "average entry price"
    else:
        entry, basis = entry_snap.get("underlying_price"), "stock price in the entry snapshot"
        if entry is None:
            on = bars[d <= start]
            entry = float(on["close"].iloc[-1]) if len(on) else None
            basis = "stock's close on the entry day (approximate)"
    if not entry or window.empty:
        return {"available": False, "reason": "no daily bars after the entry yet" if entry else "no entry price for the stock"}
    hi, lo = float(window["high"].max()), float(window["low"].min())
    bias = pos.get("bias") or (1 if pos["direction"] == "LONG" else -1)
    if bias > 0:
        mfe, mae = hi / entry - 1, lo / entry - 1
    else:
        mfe, mae = 1 - lo / entry, 1 - hi / entry      # for a bet on a fall, a lower low is favourable
    # Excursions are measured from the entry: a trade that never went against you has an MAE of zero.
    mfe, mae = max(mfe, 0.0), min(mae, 0.0)
    inv = pos.get("invalidation_price")
    r_unit = abs(entry - inv) / entry if inv else None
    # A close through the invalidation on the exit day itself may come after the exit, so for a closed
    # trade only the closes of earlier days count.
    held = window[pd.to_datetime(window["date"]).dt.date < end] if pos.get("closed_at") else window
    closes = held["close"].to_numpy(dtype=float)
    held_past = bool(inv is not None and len(closes) and np.any(bias * (closes - inv) < 0))
    t1, t2 = pos.get("target_1"), pos.get("target_2")
    reached = bool(t1 is not None and (hi >= t1 if bias > 0 else lo <= t1))
    reached_2 = bool(t2 is not None and (hi >= t2 if bias > 0 else lo <= t2))
    return {"available": True, "underlying_entry": entry, "entry_basis": basis, "mfe_pct": 100 * mfe, "mae_pct": 100 * mae,
            "mfe_r": mfe / r_unit if r_unit else None, "mae_r": mae / r_unit if r_unit else None,
            "closed_beyond_invalidation": held_past, "reached_target_1": reached, "reached_target_2": reached_2,
            "days": int(len(window))}


def behaviour_detail(pos: dict[str, Any], exc: dict[str, Any], risk_limit: float) -> dict[str, bool | None]:
    """Each habit as True (it happened), False (checked, it did not) or None (it could not be
    checked: no price history, no entry-time context, no risk measure, or it does not apply).
    A check that could not be made is never counted as "did not happen"."""
    out: dict[str, bool | None] = {}
    closed = pos["status"] == "CLOSED"
    if exc.get("available") and closed:
        out["held_past_invalidation"] = bool(exc["closed_beyond_invalidation"]) if pos.get("invalidation_price") is not None \
            else None
        out["exited_before_target"] = (not exc["closed_beyond_invalidation"] and not exc["reached_target_1"]) \
            if pos.get("target_1") is not None else None
    else:
        out["held_past_invalidation"] = out["exited_before_target"] = None
    execs = pos.get("executions", [])
    opening = ("BUY",) if pos["direction"] == "LONG" else ("SELL_SHORT", "SELL")
    adds = [e for e in execs if e["side"] in opening]
    sign = 1 if pos["direction"] == "LONG" else -1
    out["added_to_a_loser"] = bool(len(adds) > 1 and any(sign * (e["price"] - adds[0]["price"]) < 0 for e in adds[1:]))
    risk = pos["ledger"].get("initial_risk")
    out["oversized"] = bool(risk > risk_limit * 1.001) if risk and risk_limit else None
    entry = entry_context(pos)[0]
    state = (entry or {}).get("trend_state")
    bias = pos.get("bias") or (1 if pos["direction"] == "LONG" else -1)
    out["against_the_trend"] = bool((bias > 0 and state in DOWN_STATES) or (bias < 0 and state in UP_STATES)) \
        if state else None
    # Ticket limits exist only for option-journal trades; for others the habit does not apply.
    out["overrode_a_limit"] = bool(pos.get("legacy_overrides")) if (pos.get("legacy_trade_id") or "legacy_overrides" in pos) \
        else None
    return out


def behaviour(pos: dict[str, Any], exc: dict[str, Any], risk_limit: float) -> list[str]:
    """The habits that happened (behaviour_detail)."""
    return [k for k, v in behaviour_detail(pos, exc, risk_limit).items() if v is True]


DEFINITIONS = {
    "trade": "One closed position, from its first fill to the fill that closed it, however many fills it took. "
             "Partial exits and adds belong to that one trade.",
    "pnl": "Net economic profit: realized from the fills after their fees, plus borrow fees, dividends and cash "
           "adjustments recorded on the position.",
    "win": "A trade with a net profit above zero; a loss is below zero; exactly zero is neither.",
    "profit_factor": "Gross profit of the wins divided by gross loss of the losses; none when there are no losses.",
    "expectancy": "Average net profit per trade, in dollars; in R, over the trades that have an initial risk.",
    "r_multiple": "Net profit divided by the initial risk (positions.initial_risk).",
    "drawdown": "Deepest fall of the running total of closed results from its peak, in trade order by close date.",
    "mae_mfe": "Worst and best the stock moved against and for the trade on daily highs and lows from the day after "
               "entry to the exit day; the entry day's own range is not counted.",
    "habits": "Each habit is compared with trades checked and found without it; trades that could not be checked "
              "(no price history, no entry context, no risk) are left out of both groups.",
}

BEHAVIOUR_WORDS = {
    "held_past_invalidation": "Held after the stock closed through the invalidation price",
    "exited_before_target": "Exited before target 1 without being invalidated",
    "added_to_a_loser": "Added to the position at a worse price",
    "oversized": "Risk above the per-trade limit",
    "against_the_trend": "Traded against the trend at entry",
    "overrode_a_limit": "Overrode a ticket limit",
}


def trade_row(pos: dict[str, Any], exc: dict[str, Any], risk_limit: float) -> dict[str, Any]:
    """One closed position lifecycle as one trade. Its result is the net economic profit: what the
    fills made after their fees, plus the borrow fees, dividends and cash adjustments recorded
    on it (positions.financing)."""
    context, missing_context = entry_context(pos)
    entry = context or {}
    led = pos["ledger"]
    risk = led.get("initial_risk")
    fin = pos.get("financing") or {}
    trading = led["realized_pnl"]
    pnl = trading + float(fin.get("financing_total") or 0.0)
    detail = behaviour_detail(pos, exc, risk_limit)
    days = None
    if pos.get("opened_at") and pos.get("closed_at"):
        days = (datetime.fromisoformat(pos["closed_at"]) - datetime.fromisoformat(pos["opened_at"])).total_seconds() / 86400
    return {"id": pos["id"], "symbol": pos["symbol"], "description": pos.get("description", pos["symbol"]),
            "instrument_type": pos["instrument_type"], "direction": pos["direction"],
            "bias": pos.get("bias"), "setup_type": pos.get("setup_type") or "not given",
            "opened_at": pos.get("opened_at"), "closed_at": pos.get("closed_at"), "days_held": days,
            "pnl": pnl, "trading_pnl": trading, "financing": pnl - trading, "borrow_status": fin.get("borrow_status"),
            "fees": led["fees"], "r_multiple": pnl / risk if risk else None, "initial_risk": risk,
            "behaviour_checks": detail, "lifecycle_issues": len(led.get("lifecycle_issues") or []),
            "trend_at_entry": entry.get("trend_state") or "not recorded",
            "regime_at_entry": entry.get("market_regime") or "not recorded",
            "entry_context": "available" if context is not None else missing_context,
            "excursions": exc, "behaviour": [k for k, v in detail.items() if v is True], "data_source": pos.get("data_source")}


def summarize(rows: list[dict[str, Any]]) -> dict[str, Any]:
    n = len(rows)
    if not n:
        return {"trades": 0}
    pnl = np.array([r["pnl"] for r in rows], dtype=float)
    wins, losses = pnl[pnl > 0], pnl[pnl < 0]
    rs = [r["r_multiple"] for r in rows if r["r_multiple"] is not None]
    gross_win, gross_loss = float(wins.sum()), float(-losses.sum())
    streak = worst = 0
    for x in pnl:
        streak = streak + 1 if x < 0 else 0
        worst = max(worst, streak)
    mfe = [r["excursions"]["mfe_r"] for r in rows if r["excursions"].get("mfe_r") is not None]
    mae = [r["excursions"]["mae_r"] for r in rows if r["excursions"].get("mae_r") is not None]
    return {"trades": n, "wins": int(len(wins)), "losses": int(len(losses)), "win_rate": len(wins) / n,
            "average_win": float(wins.mean()) if len(wins) else None,
            "average_loss": float(losses.mean()) if len(losses) else None,
            "profit_factor": gross_win / gross_loss if gross_loss > 0 else None,
            "expectancy": float(pnl.mean()), "expectancy_r": float(np.mean(rs)) if rs else None, "r_trades": len(rs),
            "total": float(pnl.sum()), "largest_win": float(pnl.max()), "largest_loss": float(pnl.min()),
            "longest_losing_streak": worst,
            "average_days_held": float(np.mean([r["days_held"] for r in rows if r["days_held"] is not None]))
            if any(r["days_held"] is not None for r in rows) else None,
            "average_mfe_r": float(np.mean(mfe)) if mfe else None, "average_mae_r": float(np.mean(mae)) if mae else None,
            "enough": n >= MIN_TRADES}


def _group(rows: list[dict[str, Any]], key: Callable[[dict[str, Any]], list[str] | str]) -> list[dict[str, Any]]:
    groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for r in rows:
        keys = key(r)
        for k in (keys if isinstance(keys, list) else [keys]):
            groups[k].append(r)
    out = [{"group": k, **summarize(v)} for k, v in groups.items()]
    return sorted(out, key=lambda g: -g["trades"])


def hold_bucket(r: dict[str, Any]) -> str:
    d = r["days_held"]
    if d is None:
        return "unknown"
    for lo, hi, label in HOLD_BUCKETS:
        if lo <= d < hi:
            return label
    return "unknown"


def journal_stats(rows: list[dict[str, Any]]) -> dict[str, Any]:
    rows = sorted(rows, key=lambda r: r["closed_at"] or "")
    equity, peak, dd, curve = 0.0, 0.0, 0.0, []
    for r in rows:
        equity += r["pnl"]
        peak = max(peak, equity)
        dd = min(dd, equity - peak)
        curve.append({"date": (r["closed_at"] or "")[:10], "equity": equity, "trade": r["id"]})
    behaviours = []
    for flag, words in BEHAVIOUR_WORDS.items():
        checks = [r.get("behaviour_checks", {}).get(flag, flag in r["behaviour"]) for r in rows]
        with_ = [r for r, c in zip(rows, checks) if c is True]
        without = [r for r, c in zip(rows, checks) if c is False]       # checked and not done; unknown left out
        if with_:
            behaviours.append({"key": flag, "label": words, "with": summarize(with_), "without": summarize(without),
                               "not_checked": sum(1 for c in checks if c is None)})
    overall = summarize(rows)
    notes = []
    if rows and not overall["enough"]:
        notes.append(f"Only {len(rows)} closed trade{'s' if len(rows) != 1 else ''}: with fewer than {MIN_TRADES}, a single "
                     "trade can swing every figure here.")
    if any(r["r_multiple"] is None for r in rows):
        notes.append("Trades without an invalidation price or a risk amount have no R multiple and are left out of the R "
                     "figures.")
    if any(r["data_source"] == "demo" for r in rows):
        notes.append("Some trades were recorded on demo data.")
    if any(r.get("borrow_status") in ("UNKNOWN", "PARTIAL") for r in rows):
        k = sum(1 for r in rows if r.get("borrow_status") in ("UNKNOWN", "PARTIAL"))
        notes.append(f"{k} short trade{'s have' if k != 1 else ' has'} borrow fees not recorded for every day, so "
                     "their net profit leaves those costs out.")
    if any(r.get("lifecycle_issues") for r in rows):
        notes.append("Some positions were opened again after closing, which versions before 2.1.1 allowed; each counts "
                     "as one trade here.")
    without = [r for r in rows if r.get("entry_context", "available") != "available"]
    if without:
        hind = sum(1 for r in without if r["entry_context"] == "recorded with hindsight")
        notes.append(f"{len(without)} of {len(rows)} trades have no entry-time market context"
                     + (f" ({hind} only have a snapshot built from data after the fill, which is not used)" if hind else "")
                     + ": they count under 'not recorded' for trend and regime at entry and in the against-the-trend "
                       "habit. Rebuilding snapshots can add it where the price history allows.")
    return {"overall": overall, "max_drawdown": dd, "equity_curve": curve,
            "coverage": {"trades": len(rows), "entry_context": len(rows) - len(without),
                         "excursions": sum(1 for r in rows if r["excursions"].get("available")),
                         "r_multiple": sum(1 for r in rows if r["r_multiple"] is not None),
                         "borrow_known": sum(1 for r in rows if r.get("borrow_status") not in ("UNKNOWN", "PARTIAL"))},
            "definitions": DEFINITIONS,
            "by_setup": _group(rows, lambda r: r["setup_type"]),
            "by_direction": _group(rows, lambda r: "bet on a rise" if (r["bias"] or 0) > 0 else "bet on a fall"),
            "by_instrument": _group(rows, lambda r: {"STOCK": "stock", "OPTION": "calls and puts"}.get(r["instrument_type"], r["instrument_type"])),
            "by_trend_at_entry": _group(rows, lambda r: r["trend_at_entry"]),
            "by_regime_at_entry": _group(rows, lambda r: r["regime_at_entry"]),
            "by_holding_time": _group(rows, hold_bucket),
            "behaviour": behaviours, "trades": rows[::-1], "notes": notes}


def review(pos: dict[str, Any], exc: dict[str, Any], risk_limit: float) -> dict[str, Any]:
    """A completed trade explained: what was planned, what was done, what was known at entry,
    whether the thesis was invalidated or the targets reached, the result after identifiable
    costs, and what is missing. Descriptive only: it says what happened, not why it worked or
    failed."""
    led, fin = pos["ledger"], pos.get("financing") or {}
    context, why = entry_context(pos)
    inv, t1, t2 = pos.get("invalidation_price"), pos.get("target_1"), pos.get("target_2")
    has_bars = bool(exc.get("available"))
    missing = []
    if context is None:
        missing.append("Historical entry-time market data is unavailable for this position "
                       f"({'it was recorded with hindsight' if why == 'recorded with hindsight' else 'no entry snapshot'}).")
    if not has_bars:
        missing.append("No daily price history after the entry, so the moves while it was held are unknown.")
    if inv is None:
        missing.append("No invalidation price was set, so whether the thesis was invalidated cannot be judged.")
    if not led.get("initial_risk"):
        missing.append("No risk was set, so there is no R multiple.")
    if fin.get("borrow_status") in ("UNKNOWN", "PARTIAL"):
        missing.append(fin.get("borrow_text") or "Borrow fees are not fully recorded.")
    signals = [s.get("name") if isinstance(s, dict) else str(s) for s in pos.get("legacy_signals") or []]
    trading, financing_total = led["realized_pnl"], float(fin.get("financing_total") or 0.0)
    unreal = (pos.get("pnl") or {}).get("unrealized") if pos["status"] != "CLOSED" else 0.0
    net = trading + financing_total + (unreal or 0.0) if unreal is not None else None
    risk = led.get("initial_risk")
    invalidated = bool(exc["closed_beyond_invalidation"]) if has_bars and inv is not None else None
    t1_hit = bool(exc["reached_target_1"]) if has_bars and t1 is not None else None
    t2_hit = bool(exc.get("reached_target_2")) if has_bars and t2 is not None else None
    lines = []
    if invalidated is True:
        lines.append(f"The stock closed through the invalidation price ({inv:,.2f}) while the position was open.")
    elif invalidated is False:
        lines.append(f"The stock never closed through the invalidation price ({inv:,.2f}) while it was held.")
    if t1_hit is not None:
        lines.append(f"Target 1 ({t1:,.2f}) was {'reached' if t1_hit else 'not reached'} on the daily highs and lows"
                     + (f"; target 2 ({t2:,.2f}) was {'reached' if t2_hit else 'not reached'}." if t2_hit is not None else "."))
    if context is not None and context.get("trend_state"):
        lines.append(f"At entry the trend engine read {context['trend_state'].replace('_', ' ').lower()}"
                     + (f" in a {context['market_regime'].replace('_', ' ').lower()} market" if context.get("market_regime") else "")
                     + ".")
    return {
        "planned": {"thesis": pos.get("thesis"), "setup": pos.get("setup_type"), "invalidation_price": inv, "target_1": t1,
                    "target_2": t2, "risk_amount": pos.get("risk_amount"), "initial_risk": risk,
                    "intended_horizon_days": pos.get("intended_horizon"), "planned_quantity": pos.get("quantity")},
        "executed": {"fills": len(pos.get("executions", [])), "opened_at": led.get("opened_at"), "closed_at": led.get("closed_at"),
                     "average_entry_price": led.get("average_entry_price"), "average_exit_price": led.get("average_exit_price"),
                     "largest_size": led.get("max_open_quantity"), "days_held": pos.get("days_held"),
                     "corrections": len(pos.get("corrections") or [])},
        "at_entry": ({"available": True, "trend_state": context.get("trend_state"), "market_regime": context.get("market_regime"),
                      "bullish_score": context.get("bullish_score"), "bearish_score": context.get("bearish_score"),
                      "rsi_14": context.get("rsi_14"), "structure": context.get("structure_state"),
                      "method": context.get("method"), "data_as_of": context.get("data_timestamp")}
                     if context is not None else {"available": False, "reason": why}),
        "signals_at_entry": signals,
        "thesis_invalidated": invalidated, "target_1_reached": t1_hit, "target_2_reached": t2_hit,
        "result": {"trading": trading, "fees": led.get("fees"), "financing": financing_total, "unrealized": unreal,
                   "net": net, "r_multiple": (net / risk) if (net is not None and risk) else None},
        "habits": behaviour_detail(pos, exc, risk_limit),
        "what_happened": lines, "missing": missing,
        "note": "A description of what happened, from the records and daily prices. It does not say why the trade won "
                "or lost.",
    }


def position_analytics(pos: dict[str, Any], exc: dict[str, Any], risk_limit: float) -> dict[str, Any]:
    """One position: its result so far, MAE and MFE, and how the picture changed since entry."""
    entry = snapshot(pos, "ENTRY") or {}
    later = [s for s in pos.get("snapshots", []) if s["snapshot_type"] in ("EXIT", "REVIEW")]
    last = later[-1] if later else None
    fields = ("underlying_price", "trend_state", "market_regime", "bullish_score", "bearish_score", "rsi_14",
              "structure_state", "relative_strength_spy", "iv", "delta")
    compare = [{"field": f, "entry": entry.get(f), "latest": (last or {}).get(f)} for f in fields
               if entry.get(f) is not None or (last or {}).get(f) is not None]
    return {"id": pos["id"], "description": pos.get("description"), "status": pos["status"], "pnl": pos.get("pnl"),
            "excursions": exc, "behaviour": [{"key": b, "label": BEHAVIOUR_WORDS[b]} for b in behaviour(pos, exc, risk_limit)],
            "behaviour_checks": behaviour_detail(pos, exc, risk_limit),
            "review": review(pos, exc, risk_limit),
            "compare": compare, "compared_with": (last or {}).get("snapshot_type"),
            "thesis_check": pos.get("thesis_check"), "days_held": pos.get("days_held"),
            "fills": len(pos.get("executions", []))}
