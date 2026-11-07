"""The common capital allocator (3.0): one interface for every way the app splits money.

Two engines sit behind it, each kept for what it does well, and both answer in the same shape:

- **across stocks** (`blend.py`): several opportunities, each with a stop and targets, valued together on
  correlated price paths, within the budget, the share any one may take, the number of positions, the sector cap
  and the account's risk limits; with no edge it keeps the cash.
- **across strategies on one stock** (`whatif.py`): shares, options and spreads under one scenario, maximising the
  expected result less a multiple of the worst tenth.

What both answers carry: the rows (stock, instrument, units, dollars, the expected result), the cash left, the
combined expected result and its spread, the most the whole allocation can lose from the contract terms (None when
a short or a naked option has no limit), and the capital already reserved by orders the Trading Desk has open,
which is taken from the budget before anything is split.
"""

from __future__ import annotations

from typing import Any

from .opportunities import make
from .providers.base import DataError


def reserved_capital(svc: Any) -> float:
    """Capital held by the Trading Desk's open orders and approved proposals (its budget ledger), for this data mode."""
    try:
        row = svc.db.one("""SELECT COALESCE(SUM(CASE WHEN kind = 'reserve' THEN amount WHEN kind = 'release' THEN -amount ELSE 0 END), 0) AS r
                            FROM budget_ledger WHERE mode = ?""", (svc.market.info.mode,))
        return max(0.0, float(row["r"] or 0.0)) if row else 0.0
    except Exception:  # noqa: BLE001 - a database from before the Trading Desk has no ledger rows
        return 0.0


def _atr(svc: Any, symbol: str) -> float | None:
    try:
        b = svc.market.bars(symbol, 0.25, complete=True).tail(15)
        prev = b["close"].shift(1)
        tr = (b["high"] - b["low"]).combine((b["high"] - prev).abs(), max).combine((b["low"] - prev).abs(), max)
        return float(tr.tail(14).mean())
    except (DataError, KeyError, ValueError):
        return None


def to_opportunity(svc: Any, c: dict[str, Any]) -> dict[str, Any]:
    """A Discover candidate as the blend engine's opportunity: its levels, or levels from its daily range when a scanner
    gave none (entry at the price, the stop 1.5 ranges away, the target 2 ranges the other way)."""
    d = 1 if c["direction"] == "LONG" else -1
    price = c.get("entry") or svc.market.spot(c["symbol"])
    atr = _atr(svc, c["symbol"]) or 0.02 * price
    stop = c.get("invalidation") or price - d * 1.5 * atr
    target = c.get("target") or price + d * 2.0 * atr
    if d * (price - stop) <= 0:
        stop = price - d * 1.5 * atr
    if d * (target - price) <= 0:
        target = price + d * 2.0 * atr
    e = c.get("edge") or {}
    cases = int(e.get("cases") or 0)
    edge = {"known": bool(e.get("known")), "expected_pct": float(e.get("expected_pct") or 0.0), "raw_pct": None, "cases": cases,
            "weight": cases / (cases + 100.0) if cases else 0.0, "cost_pct": 0.0, "frequency": None, "enough": cases >= 30,
            "basis": e.get("basis") or "the candidate's record"}
    return make(source="manual", key=c["id"], symbol=c["symbol"], direction=c["direction"], setup=(c.get("kinds") or ["other"])[0],
                setup_label=", ".join(c.get("kinds") or []) or "Discover", horizon="day" if c["horizon"] == "intraday" else "swing",
                horizon_days=1 if c["horizon"] == "intraday" else 5, entry=float(price), stop=float(stop), target_1=float(target),
                atr=atr, score=float(c.get("rank_score") or 0.0), edge=edge, candidate_id=c["id"])


def allocate_candidates(svc: Any, candidates: list[dict[str, Any]], *, budget: float, goal: str = "balanced", tail_pct: float = 2.0,
                        max_share: float = 0.25, max_positions: int = 8, sector_cap: float = 0.4,
                        instruments: list[str] | None = None) -> dict[str, Any]:
    from .blend import Blender, BlendError
    held = reserved_capital(svc)
    available = max(0.0, budget - held)
    notes = []
    if held:
        notes.append(f"${held:,.0f} is already reserved by open orders and approved proposals, so ${available:,.0f} is split.")
    opps, skipped = [], []
    for c in candidates:
        try:
            opps.append(to_opportunity(svc, c))
        except (ValueError, DataError) as exc:
            skipped.append({"id": c.get("id"), "symbol": c.get("symbol"), "why": str(exc)[:160]})
    if not opps:
        raise ValueError("None of the opportunities could be valued: " + "; ".join(s["why"] for s in skipped[:3]))
    hub = getattr(svc, "hub", None)
    if hub is not None and any(i in ("call", "put") for i in (instruments or [])):
        try:
            hub.ensure_options(opps)
        except Exception as exc:  # noqa: BLE001 - options are optional; shares can still be split
            notes.append(f"Options could not be priced: {str(exc)[:120]}")
    if available < 50:
        return {"engine": "blend", "budget": budget, "reserved": held, "available": available, "rows": [], "left": available,
                "combined": None, "skipped": skipped, "notes": notes + ["Nothing left to split."], "theoretical_max_loss": 0.0}
    try:
        r = Blender(svc).run(opps, budget=available, goal=goal, tail_pct=tail_pct, max_share=max_share, max_positions=max_positions,
                             sector_cap=sector_cap, instruments=instruments)
    except BlendError as exc:
        raise ValueError(str(exc)) from exc
    rows = []
    for row in r["rows"]:
        cid = next((o.get("candidate_id") for o in opps if o["key"] == row["key"]), row["key"])
        theo = None if row["instrument"] == "short" else float(row["dollars"])
        rows.append({**{k: row[k] for k in ("symbol", "direction", "instrument", "units", "unit", "unit_cost", "dollars", "expected_pnl",
                                            "p_profit", "entry", "stop", "target_1", "label", "option", "horizon", "horizon_days")},
                     "candidate_id": cid, "theoretical_max_loss": theo, "risk_at_stop": row["risk_at_stop"]})
    comb = r.get("combined")
    theo_total = None if any(x["theoretical_max_loss"] is None for x in rows) else float(sum(x["theoretical_max_loss"] for x in rows))
    return {"engine": "blend", "budget": budget, "reserved": held, "available": available, "rows": rows, "left": r["left"],
            "combined": ({**comb, "expected_shortfall": comb.get("cvar10"), "theoretical_max_loss": theo_total} if comb else None),
            "theoretical_max_loss": theo_total, "correlation": r.get("correlation"), "sectors": r.get("sectors"), "stress": r.get("stress"),
            "skipped": skipped + [{"symbol": f.get("symbol"), "why": f.get("why")} for f in (r.get("fallback") or {}).get("skipped", [])
                                  if isinstance(f, dict)],
            "notes": notes + ([r["note"]] if r.get("note") else []),
            "holds_cash": not rows,
            "note": ("The split across the opportunities on correlated price paths, within the budget less what open orders hold, the "
                     "caps and your risk limits. Without an edge it keeps the cash. Not advice; no order is placed.")}


def allocate_strategies(whatif: Any, symbol: str, *, budget: float, risk: float, expected_pct: float, sd_pct: float, horizon_days: int,
                        kinds: list[str] | None = None) -> dict[str, Any]:
    """One stock, several strategies: the what-if split, in the allocator's shape."""
    held = reserved_capital(whatif.svc)
    available = max(0.0, budget - held)
    r = whatif.allocate(symbol, budget=available, risk=risk, expected_pct=expected_pct, sd_pct=sd_pct, horizon_bars=horizon_days,
                        group="1d", kinds=kinds)
    comb = r.get("combined")
    rows = [{"symbol": symbol, "direction": None, "instrument": x["kind"], "units": x["units"], "unit": x["unit"], "dollars": x["dollars"],
             "expected_pnl": x["expected_pnl"], "p_profit": x["p_profit"], "label": x["label"], "expiration": x.get("expiration")} for x in r["rows"]]
    return {"engine": "whatif", "budget": budget, "reserved": held, "available": available, "rows": rows, "left": r["left"],
            "combined": comb, "theoretical_max_loss": (comb or {}).get("theoretical_max_loss"), "skipped": r.get("skipped") or [],
            "notes": [r["note"]], "holds_cash": not rows}


__all__ = ["allocate_candidates", "allocate_strategies", "reserved_capital", "to_opportunity"]
