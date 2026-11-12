"""Strategies revalued under a scenario version's cases (4.3, the one-click pipeline).

The study picked its candidate strategies before any provider ran (shares, single options, spreads), each with its
exact legs. Once a version's case paths exist, those **same** structures are valued under every case, from **one**
frozen snapshot: one stock quote and one chain per expiration needed, taken once, with each leg's bid, ask, midpoint,
implied volatility and how old its Greeks are (Tradier refreshes them hourly, separately from the quotes).

Each case is a distribution in absolute prices at the target session: lognormal around the case's saved terminal
price (the anchor times its cumulative move), with the case's saved log-return dispersion. So a quote that moves after
the study changes what the legs cost now, never where the case says the stock ends. Every structure is valued on the
same price grid within a case, so they can be added up into mixes.

For each structure and case: the modeled net P&L after round-trip costs (the quoted spread for options, STOCK_COST_BPS
both ways for shares), the 10th-percentile result, the average loss in the worst tenth (CVaR), and, apart from the
model, the contract's own most it can lose and the capital it ties up. Then a few budgeted mixes (cash; each structure
alone at the budget; and, for each case, the What-If allocator's split under that case) are revalued across all
cases and ranked by their worst-case modeled net P&L. A probability-weighted result would need empirical case weights,
and there are none, so none is shown.

Options are valued at the target session, or at expiration when a leg expires first (said so). Before an earnings
release inside the horizon, options are valued after the volatility collapse the release is expected to bring.
Missing, one-sided or stale quotes are flagged; a structure that cannot be priced is listed, never guessed.
"""

from __future__ import annotations

import math
from datetime import date, datetime, timezone
from typing import Any

import numpy as np

from .. import contracts as C
from .. import riskmath
from .. import studio
from ..optionmath import NY
from ..providers.base import DataError
from ..service import RequestError
from ..whatif import cvar10, greedy_allocate, scenario_distribution

VALUATION_VERSION = "valuation-4.3"
LONG_KINDS, SHORT_KINDS, EVENT_KINDS = set(studio.LONG_KINDS), set(studio.SHORT_KINDS), set(studio.EVENT_KINDS)
MIX_RISK = 1.0                        # the allocator's weight on the worst tenth
MIX_MAX_SHARE = 0.5


def _f(x: Any) -> float | None:
    try:
        v = float(x)
    except (TypeError, ValueError):
        return None
    return v if math.isfinite(v) else None


def direction_of(kind: str) -> str:
    return "LONG" if kind in LONG_KINDS else "SHORT" if kind in SHORT_KINDS else "EVENT"


def limits(svc: Any) -> dict[str, Any]:
    """The budget mixes are sized to and the loss limit a setup must stay inside: SCENARIO_BUDGET and
    SCENARIO_MAX_LOSS, or the Trading Desk policy's trial capital and daily loss stop."""
    s = svc.settings
    pol = None
    desk = getattr(svc, "trading", None)
    if desk is not None:
        try:
            pol = desk.policy()
        except Exception:  # noqa: BLE001 - without a policy the settings' own figures are used
            pol = None
    budget = s.scenario_budget or (float(pol.trial_capital) if pol else 500.0)
    loss = s.scenario_max_loss or (float(pol.max_daily_loss) if pol else 25.0)
    return {"budget": float(budget), "loss_limit": float(loss),
            "budget_basis": "SCENARIO_BUDGET" if s.scenario_budget else "the Trading Desk policy's trial capital",
            "loss_basis": "SCENARIO_MAX_LOSS" if s.scenario_max_loss else "the Trading Desk policy's daily loss stop",
            "max_quote_age_seconds": float(pol.max_quote_age_seconds) if pol else 20.0}


def snapshot(svc: Any, symbol: str, candidates: list[dict[str, Any]]) -> dict[str, Any]:
    """One frozen look at the market for every candidate's legs: the stock quote and one chain per expiration."""
    out: dict[str, Any] = {"symbol": symbol, "taken_at": C.utc_now(), "quote": None, "legs": {}, "chains": {}, "problems": []}
    try:
        q = svc.market.quote(symbol)
        meta = q.get("_meta") or {}
        out["quote"] = {"last": _f(q.get("last")) or _f(q.get("close")), "bid": _f(q.get("bid")), "ask": _f(q.get("ask")),
                        "trade_time": q.get("trade_time"), "age_seconds": _f(meta.get("age_seconds")),
                        "freshness": meta.get("freshness"), "realtime": bool(meta.get("realtime", svc.market.info.realtime))}
    except (DataError, RequestError, ValueError) as exc:
        out["problems"].append(f"No quote for {symbol}: {exc}")
    exps = sorted({str(lg["expiration"]) for c in candidates for lg in (c.get("legs") or []) if lg.get("expiration")})
    for exp in exps:
        try:
            analysis = svc._chain_for(symbol, exp)          # the simulator reuses this same chain while it is fresh
        except (DataError, RequestError, ValueError) as exc:
            out["problems"].append(f"No {exp} chain: {exc}")
            continue
        out["chains"][exp] = {"spot": _f(getattr(analysis, "spot", None)), "contracts": len(analysis.contracts)}
        for row in analysis.contracts:
            sym = row.get("symbol")
            if not sym:
                continue
            g = row.get("greeks") or {}
            out["legs"][sym] = {"bid": _f(row.get("bid")), "ask": _f(row.get("ask")), "mid": _f(row.get("mid")),
                                "iv": _f(row.get("model_vol") or row.get("iv")), "spread_pct": _f(row.get("spread_pct")),
                                "open_interest": _f(row.get("open_interest")), "quote_time": row.get("bid_time") or row.get("trade_time"),
                                "greeks_updated_at": g.get("updated_at")}
    return out


def _legs_now(snap: dict[str, Any], legs: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], list[str]]:
    rows, flags = [], []
    for lg in legs:
        q = snap["legs"].get(lg.get("option_symbol") or "")
        bid, ask = (q or {}).get("bid"), (q or {}).get("ask")
        mid = (q or {}).get("mid") if q else None
        if q is None:
            flags.append(f"{lg.get('option_symbol')}: not in the snapshot's chain")
        elif not (bid and ask and ask >= bid > 0):
            flags.append(f"{lg.get('option_symbol')}: no two-sided quote")
        rows.append({**{k: lg.get(k) for k in ("option_symbol", "option_type", "strike", "expiration", "side", "contracts")},
                     "bid": bid, "ask": ask, "mid": mid, "spread_pct": (q or {}).get("spread_pct"), "iv": (q or {}).get("iv"),
                     "quote_time": (q or {}).get("quote_time"), "greeks_updated_at": (q or {}).get("greeks_updated_at"),
                     "study_mid": _f(lg.get("mid"))})
    return rows, flags


def _crush_points(svc: Any, version: dict[str, Any]) -> float | None:
    cat = version.get("catalyst") or {}
    cs = version.get("catalyst_session")
    if cat.get("kind") != "earnings" or not cs or cs > int(version["horizon_sessions"]):
        return None
    try:
        from ..options.lab import OptionsLab
        vol = (getattr(svc, "lab", None) or OptionsLab(svc)).volatility(version["symbol"])
        return studio.crush_points(vol, int(version["horizon_sessions"]))
    except Exception:  # noqa: BLE001 - without it options are valued at today's volatility, and it says so
        return None


def value(svc: Any, study: dict[str, Any], version: dict[str, Any]) -> dict[str, Any]:
    """Every candidate of the study under every case of the version, from one snapshot, and the budgeted mixes."""
    st, view = study["study"], study.get("view") or {}
    sym = st["symbol"]
    cands = [c for c in (view.get("strategies") or {}).get("candidates") or []]
    lim = limits(svc)
    snap = snapshot(svc, sym, cands)
    target = date.fromisoformat(version["sessions"][-1]["date"])
    anchor = float(version["anchor"]["price"])
    crush = _crush_points(svc, version)
    cost_bps = float(svc.settings.stock_cost_bps)
    cases = [c for c in version["cases"]]
    grids = {c["key"]: scenario_distribution(anchor, float(c["return_pct"][-1]), 100.0 * float(c["log_sd"][-1])) for c in cases}
    iv_words = (f"options valued after the earnings release's volatility collapse ({crush:.1f} points)" if crush else
                "options valued at today's implied volatility")
    structures, failed = [], []
    today = datetime.now(NY).date()
    if target < today:
        failed.append({"key": "all", "label": "Every structure", "error": f"the target session {target.isoformat()} has passed"})
        cands = []
    for c in cands:
        kind, qty = c["key"], float(c.get("quantity") or 1)
        flags: list[str] = []
        legs: list[dict[str, Any]] = []
        entry = None
        curve = None
        eval_day = target
        try:
            if kind in ("shares", "short"):
                q = snap["quote"] or {}
                entry = (q.get("ask") if kind == "shares" else q.get("bid")) or q.get("last")
                if not entry:
                    raise RequestError("no stock quote in the snapshot")
                if not (q.get("ask") if kind == "shares" else q.get("bid")):
                    flags.append("no " + ("ask" if kind == "shares" else "bid") + " quoted: the last trade is used")
                sign = 1.0 if kind == "shares" else -1.0
                costs = 2 * cost_bps / 1e4 * qty * entry
                theoretical = riskmath.theoretical_max_loss([riskmath.PayoffLeg(None, sign * qty, entry)])
                capital = riskmath.capital_required(kind, cost=qty * entry, credit=False, quantity=qty, last=entry,
                                                    theoretical=theoretical["value"])["value"]
            else:
                legs, flags = _legs_now(snap, c.get("legs") or [])
                if not legs:
                    raise RequestError("the study saved no legs for it")
                if any("not in the snapshot" in f for f in flags):
                    raise RequestError("; ".join(flags))
                exp_min = min(date.fromisoformat(str(lg["expiration"])) for lg in legs)
                if exp_min < today:
                    raise RequestError(f"a leg expired on {exp_min.isoformat()}")
                eval_day = min(target, exp_min)
                if exp_min < target:
                    flags.append(f"a leg expires {exp_min.isoformat()}, before the target session: valued at expiration")
                for lg in legs:
                    if not lg["mid"]:
                        lg["mid"] = lg["study_mid"]
                        flags.append(f"{lg['option_symbol']}: no midpoint now; the study's ({lg['study_mid']}) is used")
                    if lg.get("spread_pct") is not None and lg["spread_pct"] > svc.settings.opportunity_max_spread_pct:
                        flags.append(f"{lg['option_symbol']}: the spread is {lg['spread_pct']:.1f}% of the midpoint")
                sim_legs = [{"option_type": lg["option_type"], "strike": lg["strike"], "expiration": lg["expiration"], "side": lg["side"],
                             "contracts": int(lg.get("contracts") or 1), "entry_price": lg["mid"]} for lg in legs]
                lo = min(float(g[0].min()) for g in grids.values())
                hi = max(float(g[0].max()) for g in grids.values())
                spot = float((snap["chains"].get(str(legs[0]["expiration"])) or {}).get("spot") or anchor)
                rng = min(90.0, max(10.0, 100.0 * max(abs(hi / spot - 1), abs(lo / spot - 1)) + 2.0))
                sim = svc.simulate(symbol=sym, legs=sim_legs, eval_date=eval_day.isoformat(), range_pct=rng,
                                   iv_shift=-abs(crush) if crush else 0.0)
                curve = (np.array(sim["curve"]["prices"], dtype=float), np.array(sim["curve"]["pnl_at_date"], dtype=float))
                costs = 0.0
                for lg in legs:
                    width = (lg["ask"] - lg["bid"]) if (lg["bid"] and lg["ask"] and lg["ask"] >= lg["bid"]) else 0.05 * (lg["mid"] or 0.0)
                    costs += float(width) * float(lg.get("contracts") or 1) * 100.0
                net = sum((1 if lg["side"] == "buy" else -1) * float(lg["mid"]) * float(lg.get("contracts") or 1) * 100.0 for lg in legs)
                built = [{"option_type": lg["option_type"], "strike": lg["strike"], "side": lg["side"], "contracts": lg.get("contracts") or 1,
                          "mid": lg["mid"], "expiration": lg["expiration"]} for lg in legs]
                theoretical = riskmath.theoretical_max_loss(riskmath.legs_from_built(built))
                capital = riskmath.capital_required(kind, cost=abs(net), credit=net < 0, quantity=qty, last=anchor,
                                                    theoretical=theoretical["value"])["value"]
        except (RequestError, DataError, ValueError, KeyError) as exc:
            failed.append({"key": kind, "label": c.get("label"), "error": str(exc)[:240]})
            continue
        by_case, vectors = {}, {}
        for case in cases:
            prices, weights = grids[case["key"]]
            if curve is None:
                pnl = (1.0 if kind == "shares" else -1.0) * qty * (prices - float(entry))
            else:
                pnl = np.interp(prices, curve[0], curve[1])
            pnl = pnl - costs
            vectors[case["key"]] = pnl
            stats = riskmath.outcome_stats(prices, weights, pnl)
            by_case[case["key"]] = {"expected_pnl": float(stats["expected_pnl"]), "pnl_p10": float(stats["pnl_p10"]),
                                    "pnl_p90": float(stats["pnl_p90"]), "worst_tenth": cvar10(pnl, weights),
                                    "p_profit": float(stats["p_profit"]), "case_price": float(anchor * (1 + case["return_pct"][-1] / 100.0))}
        max_loss = theoretical.get("value") if not theoretical.get("unlimited") else None
        structures.append({"key": kind, "label": c.get("label"), "direction": direction_of(kind), "instrument": c.get("instrument"),
                           "quantity": qty, "legs": legs, "entry": entry, "costs": round(costs, 2),
                           "capital": float(capital) if capital is not None else None,
                           "max_loss": float(max_loss) if max_loss is not None else None, "unlimited_loss": bool(theoretical.get("unlimited")),
                           "executable": kind in studio.EXECUTABLE, "eval_date": eval_day.isoformat(), "by_case": by_case, "flags": flags,
                           "worst_case": min(v["expected_pnl"] for v in by_case.values()) if by_case else None, "_vectors": vectors})
    mixes = _mixes(structures, grids, lim)
    for s in structures:
        s.pop("_vectors", None)
    q = snap["quote"] or {}
    fresh = {"quote_age_seconds": q.get("age_seconds"), "realtime": q.get("realtime"),
             "stale": bool(q.get("age_seconds") is not None and q["age_seconds"] > lim["max_quote_age_seconds"]),
             "delayed": not svc.market.info.realtime, "demo": svc.market.info.mode == "demo"}
    status = "failed" if not structures else "partial" if (failed or snap["problems"]) else "complete"
    return {"valuation_version": VALUATION_VERSION, "version_id": version["id"], "study_id": st["id"], "symbol": sym,
            "target_date": target.isoformat(), "anchor": version["anchor"], "snapshot_at": snap["taken_at"], "iv_assumption": iv_words,
            "crush_points": crush, "limits": lim, "freshness": fresh, "structures": structures, "failed": failed, "mixes": mixes,
            "best_mix": mixes[0]["key"] if mixes else "cash", "status": status, "problems": snap["problems"],
            "note": ("Each structure is valued on the same price grid within a case: lognormal around the case's saved price at the target "
                     "session with its saved dispersion, from one quote snapshot. Net of round-trip costs. Mixes are ranked by their "
                     "worst-case modeled net P&L across the cases; no case has a probability, so no probability-weighted result is shown. "
                     "Not advice; no order is placed.")}, snap


def _mixes(structures: list[dict[str, Any]], grids: dict[str, Any], lim: dict[str, Any]) -> list[dict[str, Any]]:
    """Cash, each structure alone at the budget, and the What-If allocator's split under each case, all revalued under
    every case on the same grids. Mixes are bounded by the budget only; whether one stays inside the loss limit is
    reported beside it (the opportunity checks decide, 4.4)."""
    budget, loss_limit = lim["budget"], lim["loss_limit"]
    ok = [s for s in structures if not s["unlimited_loss"] and s["capital"] and 0 < s["capital"] <= budget]
    out: list[dict[str, Any]] = [{"key": "cash", "label": "Cash", "parts": [], "capital": 0.0, "max_loss": 0.0, "within_loss_limit": True,
                                  "by_case": {k: {"expected_pnl": 0.0, "worst_tenth": 0.0} for k in grids}, "worst_case": 0.0,
                                  "worst_tenth": 0.0}]

    def describe(parts: list[tuple[dict[str, Any], int]], key: str, label: str) -> dict[str, Any]:
        by_case = {}
        for ck, (_prices, weights) in grids.items():
            total = sum(n * s["_vectors"][ck] for s, n in parts)
            by_case[ck] = {"expected_pnl": float((weights * total).sum()), "worst_tenth": cvar10(total, weights)}
        most = float(sum(n * (s["max_loss"] or 0) for s, n in parts))
        return {"key": key, "label": label, "parts": [{"key": s["key"], "label": s["label"], "units": n} for s, n in parts],
                "capital": float(sum(n * s["capital"] for s, n in parts)), "max_loss": most, "within_loss_limit": most <= loss_limit + 1e-9,
                "by_case": by_case, "worst_case": min(v["expected_pnl"] for v in by_case.values()),
                "worst_tenth": max(v["worst_tenth"] for v in by_case.values())}
    for s in ok:
        units = int(budget // s["capital"])
        if units >= 1:
            out.append(describe([(s, units)], f"single:{s['key']}", f"{s['label']} ×{units}"))
    for ck, (_prices, weights) in grids.items():
        items = [{"s": s, "capital": s["capital"], "pnl": s["_vectors"][ck], "units": 0, "most": 10_000} for s in ok]
        if not items:
            continue
        greedy_allocate(items, weights, budget=budget, max_share=MIX_MAX_SHARE, lam=MIX_RISK)
        parts = [(it["s"], int(it["units"])) for it in items if it["units"] > 0]
        if parts:
            out.append(describe(parts, f"mix:{ck}", f"Split for the {ck} case: " + ", ".join(f"{s['label']} ×{n}" for s, n in parts)))
    seen, uniq = set(), []
    for m in sorted(out, key=lambda m: (-m["worst_case"], m["capital"])):
        sig = tuple(sorted((p["key"], p["units"]) for p in m["parts"]))
        if sig in seen:
            continue
        seen.add(sig)
        uniq.append(m)
    return uniq[:10]


def save(svc: Any, result: dict[str, Any], snap: dict[str, Any]) -> str:
    import json
    vid = C.new_id("sval")
    with svc.db.tx() as cur:
        cur.execute("""INSERT INTO scenario_valuations (id, version_id, study_id, created_at, status, snapshot_json, result_json)
                       VALUES (?, ?, ?, ?, ?, ?, ?)""",
                    (vid, result["version_id"], result["study_id"], C.utc_now(), result["status"], json.dumps(snap, default=str),
                     json.dumps(result, default=str)))
    return vid


def load(svc: Any, valuation_id: str) -> dict[str, Any]:
    import json
    r = svc.db.one("SELECT * FROM scenario_valuations WHERE id = ?", (valuation_id,))
    if r is None:
        raise RequestError(f"No valuation {valuation_id}")
    return {"id": r["id"], "created_at": r["created_at"], **json.loads(r["result_json"])}


def latest_for_version(svc: Any, version_id: str) -> dict[str, Any] | None:
    r = svc.db.one("SELECT id FROM scenario_valuations WHERE version_id = ? ORDER BY created_at DESC, id DESC LIMIT 1", (version_id,))
    return load(svc, r["id"]) if r else None


__all__ = ["value", "save", "load", "latest_for_version", "limits", "snapshot", "direction_of", "VALUATION_VERSION"]
_ = timezone
