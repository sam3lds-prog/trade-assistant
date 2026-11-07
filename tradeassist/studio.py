"""The Strategy Studio (3.0 Research): shares, calls, puts and spreads compared under one set of scenarios.

Every candidate is valued under the same scenarios from the study's forecast (the base path, an earnings beat
and a guidance cut when a release falls inside the horizon, a market rally and sell-off), so the comparison is
like for like. For each: the expected P&L in each scenario, the percentiles of the P&L, the chance of a profit, the
four risk measures (riskmath.py), the breakevens, what an implied-volatility change does (and, before earnings,
the volatility collapse after the release, which can make a long option lose even when the stock moves the right
way), an estimate of how likely a limit at the fair price is to fill, and whether the account can trade it as a
single order (only shares and single options can be sent from the Trading Desk; spreads stay research and manual
orders until a broker path for them is verified).
"""

from __future__ import annotations

import math
from typing import Any

from .riskmath import finite

LONG_KINDS = ("shares", "long_call", "bull_call_spread", "bull_put_spread")
SHORT_KINDS = ("short", "long_put", "bear_put_spread", "bear_call_spread")
EVENT_KINDS = ("long_straddle", "iron_condor")
EXECUTABLE = {"shares", "short", "long_call", "long_put"}
INSTRUMENT = {"shares": "shares", "short": "short"}


def fill_likelihood(kind: str, legs: list[dict[str, Any]]) -> float | None:
    """A rough chance that a limit at the fair price fills within the session, from the quoted spread: an estimate
    for comparing candidates, not a promise (the Trading Desk's record of fills replaces it as it grows)."""
    if kind in ("shares", "short"):
        return 0.97
    spreads = [float(l["spread_pct"]) for l in legs if finite(l.get("spread_pct"))]
    if not spreads:
        return None
    worst = max(spreads)
    one = max(0.15, min(0.95, 0.95 - 0.018 * worst))
    return round(one ** max(1, len(legs)) if len(legs) > 1 else one, 3)


def scenarios_from(forecast: dict[str, Any]) -> list[dict[str, Any]]:
    """The scenarios every candidate is valued under: the base and each alternative path's end point, with the
    forecast's spread (a lognormal standard deviation in percent)."""
    sd = max(0.5, 100 * float(forecast.get("sigma") or 0.02) * float(forecast.get("band_k") or 1.0))
    out = [{"key": "base", "label": "Base", "expected_pct": float(forecast["base"]["path_pct"][-1]), "sd_pct": sd}]
    for alt in forecast.get("alternatives") or []:
        out.append({"key": alt["key"], "label": alt["label"], "expected_pct": float(alt["path_pct"][-1]), "sd_pct": sd * 0.8})
    return out


def compare(whatif: Any, symbol: str, *, forecast: dict[str, Any], horizon_days: int, direction: str | None = None,
            kinds: list[str] | None = None, earnings_crush_points: float | None = None) -> dict[str, Any]:
    """Each candidate under the forecast's scenarios. `earnings_crush_points` is how many volatility points the
    options are expected to lose once the earnings release is out (from the event move options price)."""
    if kinds is None:
        kinds = list(LONG_KINDS if direction == "LONG" else SHORT_KINDS if direction == "SHORT" else LONG_KINDS + SHORT_KINDS)
        if earnings_crush_points:
            kinds += list(EVENT_KINDS)
    scen = scenarios_from(forecast)
    base = scen[0]
    last = whatif._live_price(symbol)
    rows, failed = [], []
    for kind in dict.fromkeys(kinds):
        qty = 100 if kind in ("shares", "short") else 1
        try:
            r = whatif._position(symbol, kind=kind, quantity=qty, expected_pct=base["expected_pct"], sd_pct=base["sd_pct"],
                                 horizon_bars=horizon_days, group="1d", last=last)
        except Exception as exc:  # noqa: BLE001 - one structure without contracts must not stop the comparison
            failed.append({"kind": kind, "error": str(exc)[:200]})
            continue
        by_scenario = {"base": r["expected_pnl"]}
        for s in scen[1:]:
            try:
                alt = whatif._position(symbol, kind=kind, quantity=qty, expected_pct=s["expected_pct"], sd_pct=s["sd_pct"],
                                       horizon_bars=horizon_days, group="1d", last=last)
                by_scenario[s["key"]] = alt["expected_pnl"]
            except Exception:  # noqa: BLE001
                by_scenario[s["key"]] = None
        iv = {}
        if kind not in ("shares", "short"):
            for pts in (-5.0, 5.0):
                try:
                    iv[f"{pts:+.0f}"] = whatif._position(symbol, kind=kind, quantity=qty, expected_pct=base["expected_pct"],
                                                         sd_pct=base["sd_pct"], horizon_bars=horizon_days, group="1d", last=last,
                                                         iv_shift=pts)["expected_pnl"]
                except Exception:  # noqa: BLE001
                    iv[f"{pts:+.0f}"] = None
            if earnings_crush_points:
                try:
                    iv["crush"] = whatif._position(symbol, kind=kind, quantity=qty, expected_pct=base["expected_pct"],
                                                   sd_pct=base["sd_pct"], horizon_bars=horizon_days, group="1d", last=last,
                                                   iv_shift=-abs(earnings_crush_points))["expected_pnl"]
                except Exception:  # noqa: BLE001
                    iv["crush"] = None
        # Round-trip trading costs: the stock's cost per side (STOCK_COST_BPS) both ways, or for options the quoted
        # spread (half to get in, half to get out), 5% of the premium where no spread is quoted.
        if kind in ("shares", "short"):
            costs = 2 * float(whatif.svc.settings.stock_cost_bps) / 1e4 * qty * last
        else:
            costs = 0.0
            for lg in r.get("legs") or []:
                bid, ask, mid = lg.get("bid"), lg.get("ask"), lg.get("mid")
                width = (ask - bid) if finite(bid) and finite(ask) and ask >= bid else 0.05 * (mid or 0.0)
                costs += float(width) * float(lg.get("contracts") or 1) * 100.0
        after = {k: (v - costs if v is not None else None) for k, v in by_scenario.items()}
        capital = r.get("capital_required")
        notes = list(r.get("notes") or [])[:2]
        if "crush" in iv and iv["crush"] is not None and r["expected_pnl"] > 0 > iv["crush"]:
            notes.append("Gains under the base path but loses if volatility collapses after the earnings release.")
        if kind not in EXECUTABLE:
            notes.append("Several legs: valued here; enter it by hand at the broker (the Trading Desk sends single orders only).")
        rows.append({
            "key": kind, "label": r["label"], "instrument": INSTRUMENT.get(kind, "option" if kind in ("long_call", "long_put") else "multi_leg"),
            "quantity": qty, "legs": r.get("legs") or [], "cost": float(r["cost"]), "credit": bool(r.get("credit")),
            "expected_pnl": after, "expected_pnl_before_costs": by_scenario, "costs": costs, "pnl_p10": r["pnl_p10"], "pnl_p50": r["pnl_p50"], "pnl_p90": r["pnl_p90"],
            "p_profit": r["p_profit"], "theoretical_max_loss": r.get("theoretical_max_loss"), "unlimited_loss": bool(r.get("unlimited_loss")),
            "modeled_worst_loss": r.get("modeled_worst_loss"), "expected_shortfall": r.get("expected_shortfall"),
            "capital_required": capital, "breakevens": r.get("breakevens") or [], "iv_sensitivity": iv,
            "fill_likelihood": fill_likelihood(kind, r.get("legs") or []), "executable": kind in EXECUTABLE,
            "expiration": r.get("expiration"), "legs_text": r.get("legs_text") or [],
            "return_on_capital": (after["base"] / capital) if capital else None,
            "risk_adjusted": after["base"] / (r.get("theoretical_max_loss") or r.get("expected_shortfall") or 1.0),
            "notes": notes})
    ranked = sorted(rows, key=lambda x: -x["risk_adjusted"])
    best = next((x for x in ranked if x["executable"]), None)
    why = None
    if best:
        risk_word = "the most it can lose" if best["theoretical_max_loss"] else "the average of its worst tenth"
        why = (f"{best['label']}: the highest expected result under the base path per dollar of {risk_word} "
               f"({best['risk_adjusted']:+.2f}) among the structures the Trading Desk can send.")
        if best["expected_pnl"]["base"] <= 0:
            why = ("No structure the Trading Desk can send has a positive expected result under the base path after trading "
                   "costs: holding cash is the comparison's answer.")
            best = None
    return {"symbol": symbol, "horizon_days": horizon_days, "scenarios": scen, "candidates": rows, "failed": failed,
            "best": best["key"] if best else None, "why": why,
            "note": ("Every candidate is valued under the same scenarios: the forecast's base path and its alternatives. "
                     "Expected P&L per scenario after round-trip trading costs; percentiles of the P&L (before costs); the most "
                     "it can lose from the contract terms. "
                     "Fill chances are estimates from the quoted spread. Not advice; no order is placed.")}


def crush_points(vol_profile: dict[str, Any] | None, horizon_days: int) -> float | None:
    """How many points of implied volatility the front options would lose after an earnings release inside the
    horizon: the expiration's ATM volatility less the base volatility without the event."""
    if not vol_profile:
        return None
    ev = vol_profile.get("event") or {}
    earn = vol_profile.get("earnings") or {}
    days = earn.get("trading_days") if isinstance(earn, dict) else None
    if not ev or not ev.get("available") or days is None or not (0 <= days <= horizon_days):
        return None
    base = ev.get("base_vol")
    exp = next((e for e in vol_profile.get("expirations") or [] if e.get("expiration") == ev.get("expiration")), None)
    if not exp or not finite(base) or not finite(exp.get("atm_iv")):
        return None
    pts = 100 * (float(exp["atm_iv"]) - float(base))
    return round(pts, 2) if pts > 0 and math.isfinite(pts) else None


__all__ = ["compare", "scenarios_from", "fill_likelihood", "crush_points", "EXECUTABLE"]
