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
    snaps = [s for s in pos.get("snapshots", []) if s["snapshot_type"] == kind]
    return snaps[0] if snaps else None


def excursions(pos: dict[str, Any], bars: pd.DataFrame | None, until: str | None = None) -> dict[str, Any]:
    """MAE and MFE on the stock while the position was held, in percent and in R."""
    if bars is None or bars.empty or not pos.get("opened_at"):
        return {"available": False}
    start = _date(pos["opened_at"])
    end = _date(until or pos.get("closed_at")) or datetime.now(NY).date()
    d = pd.to_datetime(bars["date"]).dt.date
    window = bars[(d > start) & (d <= end)]
    entry_snap = snapshot(pos, "ENTRY") or {}
    if pos["instrument_type"] == "STOCK":
        entry = pos["ledger"]["average_entry_price"]
    else:
        entry = entry_snap.get("underlying_price")
        if entry is None:
            on = bars[d <= start]
            entry = float(on["close"].iloc[-1]) if len(on) else None
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
    closes = window["close"].to_numpy(dtype=float)
    held_past = bool(inv is not None and len(closes) and np.any(bias * (closes - inv) < 0))
    t1 = pos.get("target_1")
    reached = bool(t1 is not None and (hi >= t1 if bias > 0 else lo <= t1))
    return {"available": True, "underlying_entry": entry, "mfe_pct": 100 * mfe, "mae_pct": 100 * mae,
            "mfe_r": mfe / r_unit if r_unit else None, "mae_r": mae / r_unit if r_unit else None,
            "closed_beyond_invalidation": held_past, "reached_target_1": reached, "days": int(len(window))}


def behaviour(pos: dict[str, Any], exc: dict[str, Any], risk_limit: float) -> list[str]:
    flags = []
    if exc.get("available"):
        if exc["closed_beyond_invalidation"] and pos["status"] == "CLOSED":
            # The stock closed through the invalidation price and the trade was still open on a later bar.
            flags.append("held_past_invalidation")
        if (pos["status"] == "CLOSED" and not exc["closed_beyond_invalidation"] and pos.get("target_1") is not None
                and not exc["reached_target_1"]):
            flags.append("exited_before_target")
    execs = pos.get("executions", [])
    opening = ("BUY",) if pos["direction"] == "LONG" else ("SELL_SHORT", "SELL")
    adds = [e for e in execs if e["side"] in opening]
    if len(adds) > 1:
        first = adds[0]["price"]
        sign = 1 if pos["direction"] == "LONG" else -1
        if any(sign * (e["price"] - first) < 0 for e in adds[1:]):
            flags.append("added_to_a_loser")
    risk = pos["ledger"].get("initial_risk")
    if risk and risk_limit and risk > risk_limit * 1.001:
        flags.append("oversized")
    entry = snapshot(pos, "ENTRY") or {}
    state = entry.get("trend_state")
    bias = pos.get("bias") or (1 if pos["direction"] == "LONG" else -1)
    if state and ((bias > 0 and state in DOWN_STATES) or (bias < 0 and state in UP_STATES)):
        flags.append("against_the_trend")
    if pos.get("legacy_overrides"):
        flags.append("overrode_a_limit")
    return flags


BEHAVIOUR_WORDS = {
    "held_past_invalidation": "Held after the stock closed through the invalidation price",
    "exited_before_target": "Exited before target 1 without being invalidated",
    "added_to_a_loser": "Added to the position at a worse price",
    "oversized": "Risk above the per-trade limit",
    "against_the_trend": "Traded against the trend at entry",
    "overrode_a_limit": "Overrode a ticket limit",
}


def trade_row(pos: dict[str, Any], exc: dict[str, Any], risk_limit: float) -> dict[str, Any]:
    entry = snapshot(pos, "ENTRY") or {}
    led = pos["ledger"]
    risk = led.get("initial_risk")
    pnl = led["realized_pnl"]
    days = None
    if pos.get("opened_at") and pos.get("closed_at"):
        days = (datetime.fromisoformat(pos["closed_at"]) - datetime.fromisoformat(pos["opened_at"])).total_seconds() / 86400
    return {"id": pos["id"], "symbol": pos["symbol"], "description": pos.get("description", pos["symbol"]),
            "instrument_type": pos["instrument_type"], "direction": pos["direction"],
            "bias": pos.get("bias"), "setup_type": pos.get("setup_type") or "not given",
            "opened_at": pos.get("opened_at"), "closed_at": pos.get("closed_at"), "days_held": days,
            "pnl": pnl, "fees": led["fees"], "r_multiple": pnl / risk if risk else None, "initial_risk": risk,
            "trend_at_entry": entry.get("trend_state") or "not recorded",
            "regime_at_entry": entry.get("market_regime") or "not recorded",
            "excursions": exc, "behaviour": behaviour(pos, exc, risk_limit), "data_source": pos.get("data_source")}


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
        with_ = [r for r in rows if flag in r["behaviour"]]
        without = [r for r in rows if flag not in r["behaviour"]]
        if with_:
            behaviours.append({"key": flag, "label": words, "with": summarize(with_), "without": summarize(without)})
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
    return {"overall": overall, "max_drawdown": dd, "equity_curve": curve,
            "by_setup": _group(rows, lambda r: r["setup_type"]),
            "by_direction": _group(rows, lambda r: "bet on a rise" if (r["bias"] or 0) > 0 else "bet on a fall"),
            "by_instrument": _group(rows, lambda r: {"STOCK": "stock", "OPTION": "calls and puts"}.get(r["instrument_type"], r["instrument_type"])),
            "by_trend_at_entry": _group(rows, lambda r: r["trend_at_entry"]),
            "by_regime_at_entry": _group(rows, lambda r: r["regime_at_entry"]),
            "by_holding_time": _group(rows, hold_bucket),
            "behaviour": behaviours, "trades": rows[::-1], "notes": notes}


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
            "compare": compare, "compared_with": (last or {}).get("snapshot_type"),
            "thesis_check": pos.get("thesis_check"), "days_held": pos.get("days_held"),
            "fills": len(pos.get("executions", []))}
