"""Draft trades (3.1): a Research strategy handed to the Trading Desk with everything it was decided on.

A study compares shares, single options and spreads under the same scenarios. Picking one used to send only its
contract to the linked widgets, so the Trading Desk's proposal form started from nothing: the thesis, the size,
the limit and the exit had to be typed again, and whether the desk could send the trade at all was a guess ("Desk
can send: yes" was shown for a call even under the default policy, which allows shares only).

A **draft** is built here, from the saved study, and carries:

- where it came from: the study (and the opportunity it was bound to), the symbol, the strategy and its legs, the
  direction and the holding horizon;
- the scenario it was valued under: the forecast's model version, its calibration status and its range, worded as a
  calibrated 80% band only when the calibration for that horizon passed;
- the quantity and a proposed entry limit, with how the limit was set;
- its risk in separate terms that are never mixed up: the theoretical maximum loss from the contract terms, the
  modeled downside (the model's 10th percentile and its worst outcome), the expected shortfall (the average of the
  worst tenth) and the capital it ties up, which is not what a broker may require;
- an exit plan, and plainly what kind of exit it is: a record to act on, not an order at the broker;
- its **route**: `automatic` (the Trading Desk can write it as a proposal under the active policy and the account's
  broker can send it, after your approval), `manual` (a real order to enter by hand at your broker: a spread, or an
  instrument the policy or broker does not allow), or `research` (nothing to send: made-up demo prices, or no
  priced contract). An automatic draft carries the exact proposal body and the risk engine's verdict on it now.

Nothing here writes a proposal or places an order: the Proposals widget proposes an automatic draft only when you
press its button, and the proposal then waits for approval as any other does.
"""

from __future__ import annotations

import math
from datetime import datetime
from typing import Any, Literal

from fastapi import APIRouter
from pydantic import BaseModel, ConfigDict, Field

from .. import contracts as C
from ..marketdays import add_trading_days
from ..optionmath import NY
from ..providers.base import DataError
from ..service import RequestError

ROUTE_WORDS = {"automatic": "The Trading Desk can send it, after your approval",
               "manual": "Enter it by hand at your broker",
               "research": "Research only: nothing to send"}
CALIBRATED_RANGE = "the calibrated 80% band"
UNVALIDATED_RANGE = "the model-implied 10th to 90th percentile range (historical coverage not yet validated)"


class ScenarioRefIn(BaseModel):
    """4.4: the scenario a draft comes from: the version, the case it was chosen under, the valuation and decision."""
    model_config = ConfigDict(extra="forbid")
    version_id: str = Field(min_length=1, max_length=64)
    case: Literal["central", "bullish", "bearish"] | None = None
    valuation_id: str | None = Field(default=None, max_length=64)
    decision_id: str | None = Field(default=None, max_length=64)


class DraftIn(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)
    study_id: str = Field(min_length=1, max_length=64)
    strategy: str = Field(min_length=1, max_length=40)
    quantity: float | None = Field(default=None, gt=0, le=100_000)
    scenario: ScenarioRefIn | None = None


CASE_DIRECTION = {"bullish": "LONG", "bearish": "SHORT"}


def scenario_ref(svc: Any, study_id: str, strategy: str, ref: ScenarioRefIn) -> dict[str, Any]:
    """The scenario a draft is chosen under, checked against the study: a version, a case, a valuation or a decision
    of another study is refused, and a case's draft must go its way (a Bearish case's draft is a short-direction one)."""
    from .valuation import direction_of
    sc = getattr(svc, "scenarios", None)
    if sc is None:
        raise RequestError("Catalyst Scenarios are not running in this app")
    v = sc.version(ref.version_id)
    if v.get("study_id") != study_id:
        raise RequestError("That scenario version belongs to another study: draft from that study, or from this study's own scenarios.")
    case = None
    if ref.case:
        case = next((c for c in v["cases"] if c["key"] == ref.case), None)
        if case is None:
            raise RequestError(f"That scenario version has no {ref.case} case")
        want = CASE_DIRECTION.get(ref.case)
        have = direction_of(strategy)
        if want and have not in (want, "EVENT"):
            raise RequestError(f"The {case['label']} case goes {'up' if want == 'LONG' else 'down'}: draft a "
                               f"{'long' if want == 'LONG' else 'short'}-direction strategy for it, not {strategy.replace('_', ' ')}.")
    out: dict[str, Any] = {"version_id": v["id"], "version": v["version"], "origin": v.get("origin"), "run_id": v["run_id"],
                           "case": ref.case, "case_label": case["label"] if case else None, "trigger": case["trigger"] if case else None,
                           "invalidation": case["invalidation"] if case else None,
                           "case_price": case["terminal"]["price"] if case else None, "target_date": v["sessions"][-1]["date"]}
    if ref.valuation_id:
        from .valuation import load
        val = load(svc, ref.valuation_id)
        if val["version_id"] != v["id"]:
            raise RequestError("That valuation is of another scenario version")
        row = next((x for x in val["structures"] if x["key"] == strategy), None)
        if row is None:
            raise RequestError(f"That valuation did not value {strategy}")
        out["valuation_id"] = val["id"]
        out["valued"] = {k: {"expected_pnl": x["expected_pnl"], "worst_tenth": x["worst_tenth"]} for k, x in row["by_case"].items()}
        out["unit_quantity"] = row.get("quantity")
    if ref.decision_id:
        d = svc.db.one("SELECT study_id, status FROM scenario_decisions WHERE id = ?", (ref.decision_id,))
        if d is None or d["study_id"] != study_id:
            raise RequestError("That decision is of another study")
        out["decision_id"], out["decision_status"] = ref.decision_id, d["status"]
    return out


def _finite(x: Any) -> float | None:
    try:
        f = float(x)
    except (TypeError, ValueError):
        return None
    return f if math.isfinite(f) else None


def order_shape(candidate: dict[str, Any]) -> tuple[str, str | None]:
    """The order a candidate would be: (instrument, side). A spread has no single order (side None)."""
    kind, inst, legs = candidate.get("key"), candidate.get("instrument"), candidate.get("legs") or []
    if inst == "shares" or kind == "shares":
        return "equity", "buy"
    if inst == "short" or kind == "short":
        return "equity", "sell_short"
    if inst == "option" and len(legs) == 1:
        return "option", "buy" if (legs[0].get("side") or "buy") == "buy" else "sell_to_open"
    return "multi_leg", None


def route(desk: Any, candidate: dict[str, Any], data_mode: str) -> dict[str, Any]:
    """Where a strategy can go now: the Trading Desk, your broker by hand, or nowhere (research only), and why."""
    instrument, side = order_shape(candidate)
    legs = candidate.get("legs") or []
    priced = instrument == "equity" or (bool(legs) and all(_finite(l.get("mid")) or _finite(l.get("ask")) for l in legs))
    reasons: list[str] = []
    pol = desk.policy() if desk is not None else None
    if desk is None or pol is None:
        reasons.append("The Trading Desk is not running in this app")
    elif instrument == "multi_leg":
        reasons.append("Several legs: the Trading Desk sends single orders only, and no broker path for multi-leg orders is verified")
    else:
        if pol.mode == "disabled":
            reasons.append("The trading policy is off")
        if instrument not in pol.instruments:
            reasons.append(f"The policy allows {', '.join(pol.instruments)} orders, not {instrument}")
        if side not in pol.sides:
            reasons.append(f"The policy allows {' and '.join(s.replace('_', ' ') for s in pol.sides)}, not {side.replace('_', ' ')}")
        if pol.account_kind == "live":
            if instrument != "equity":
                reasons.append("The live broker adapter sends share orders only")
            if not desk.live_ready:
                reasons.append("Live execution is not ready on this computer")
        if not priced:
            reasons.append("The contract has no quote to set a limit from")
    if not reasons:
        kind, label = "automatic", ROUTE_WORDS["automatic"]
        if pol is not None and pol.account_kind in ("paper", "shadow"):
            label = f"The Trading Desk can send it to the {pol.account_kind} account, after your approval"
    elif data_mode == "demo":
        kind, label = "research", ROUTE_WORDS["research"]
        reasons.append("The prices are made up (demo data): there is no real order to enter")
    elif not priced:
        kind, label = "research", ROUTE_WORDS["research"]
    else:
        kind, label = "manual", ROUTE_WORDS["manual"]
    return {"route": kind, "label": label, "reasons": reasons, "instrument": instrument, "side": side,
            "account_kind": pol.account_kind if pol is not None else None}


def routes_for(svc: Any, candidates: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    desk = getattr(svc, "trading", None)
    mode = svc.market.info.mode
    return {c["key"]: route(desk, c, mode) for c in candidates if c.get("key")}


def _range_words(fc: dict[str, Any]) -> str:
    return CALIBRATED_RANGE if ((fc.get("calibration") or {}).get("status") == "calibrated") else UNVALIDATED_RANGE


def build(svc: Any, req: DraftIn) -> dict[str, Any]:
    research = getattr(svc, "research3", None)
    if research is None:
        raise RequestError("Research is not running in this app")
    got = research.get(req.study_id)
    if "study" not in got:
        raise RequestError("The study has not finished: run it, then draft a strategy from it")
    st, view = got["study"], got.get("view") or {}
    cands = (view.get("strategies") or {}).get("candidates") or []
    cand = next((c for c in cands if c.get("key") == req.strategy), None)
    if cand is None:
        raise RequestError(f"The study has no strategy {req.strategy!r}")
    sym, h = st["symbol"], int(st["horizon_days"])
    scen = scenario_ref(svc, st["id"], req.strategy, req.scenario) if req.scenario is not None else None
    fc = st.get("forecast") or {}
    base = fc.get("base") or {}
    last = _finite(view.get("last"))
    desk = getattr(svc, "trading", None)
    rt = route(desk, cand, svc.market.info.mode)
    instrument, side = rt["instrument"], rt["side"]
    unit_qty = float(cand.get("quantity") or 1)
    legs = cand.get("legs") or []
    notes: list[str] = []

    # The entry limit. Shares: the ask to buy, the bid to sell short, now. One option: its midpoint when the study
    # ran, never above it for a buyer; the Order ticket's ladder prices it from the live quote.
    limit = None
    basis = ""
    option = None
    if instrument == "equity":
        try:
            q = svc.market.quote(sym)
            limit = _finite(q.get("ask") if side == "buy" else q.get("bid"))
            basis = f"the {'ask' if side == 'buy' else 'bid'} now"
        except (DataError, ValueError):
            limit = None
        if limit is None and last:
            limit, basis = last, "the last price when the study ran (no quote now)"
    elif instrument == "option":
        lg = legs[0]
        mid, ask = _finite(lg.get("mid")), _finite(lg.get("ask"))
        limit = min(x for x in (mid, ask) if x is not None) if (mid or ask) else None
        basis = "the midpoint when the study ran; price it on the Order ticket for the fair-price ladder from the live quote"
        if scen is not None and lg.get("expiration"):
            # 4.4: from a scenario, a fresh quote for the same contract (the study's may be hours old by now).
            try:
                row = svc._chain_for(sym, str(lg["expiration"])).find_by_terms(str(lg.get("option_type", "")).lower(), float(lg["strike"]))
            except (DataError, RequestError, ValueError):
                row = None
            fm, fa = (_finite(row.get("mid")), _finite(row.get("ask"))) if row else (None, None)
            if fm or fa:
                limit = min(x for x in (fm, fa) if x is not None)
                basis = "the midpoint now (a fresh quote); price it on the Order ticket for the fair-price ladder"
            else:
                notes.append("No fresh quote for the contract now: the limit is the study's midpoint.")
        if lg.get("option_symbol") and lg.get("strike") and lg.get("expiration"):
            option = C.OptionTerms(option_symbol=str(lg["option_symbol"]), underlying=sym, option_type=str(lg.get("option_type", "")).upper(),
                                   strike=float(lg["strike"]), expiration=str(lg["expiration"]))
    limit = round(limit, 2) if limit else None

    # The size. For the desk: the most that fits the policy's order, capital and daily-buying limits (and the live
    # ceiling), as the autopilot sizes; otherwise the study's unit (100 shares, one contract or one spread).
    sizing = "as you set it"
    if req.quantity:
        qty = float(req.quantity)
    elif rt["route"] == "automatic" and limit and desk is not None:
        pol = desk.policy()
        book = desk.book(pol.account_kind)
        room = min(pol.max_order, pol.trial_capital - book["capital_in_use"], pol.max_daily_new_buys - book["today_new_buys"])
        if pol.account_kind == "live":
            room = min(room, float(svc.settings.live_max_order))
        each = limit * (100.0 if instrument == "option" else 1.0)
        fits = int(math.floor(max(0.0, room) / each)) if each > 0 else 0
        if fits >= 1:
            qty, sizing = float(fits), f"the most that fits the policy now: ${max(0.0, room):,.2f} of room in its order, capital and daily-buying limits"
        else:
            qty = 1.0
            sizing = f"one {'contract' if instrument == 'option' else 'share'}, which does not fit the policy's ${max(0.0, room):,.2f} of room: it will be blocked"
            notes.append(f"Even one {'contract' if instrument == 'option' else 'share'} at ${each:,.2f} is more than the policy's limits allow now (${max(0.0, room):,.2f} of room).")
    else:
        qty = unit_qty
        sizing = "the study's unit; size it to your own risk"
    scale = qty / unit_qty if unit_qty else 1.0

    # The scenario it was valued under, worded as its calibration allows.
    path, lows, highs = base.get("path_pct") or [], base.get("low_pct") or [], base.get("high_pct") or []
    scenario = {"key": "base", "label": "Base", "model_version": fc.get("model_version"),
                "calibration": (fc.get("calibration") or {}).get("status"), "range_words": _range_words(fc),
                "expected_pct": path[-1] if path else None, "low_pct": lows[-1] if lows else None, "high_pct": highs[-1] if highs else None}

    # Risk, in separate terms, scaled to the quantity.
    def scaled(x: Any) -> float | None:
        v = _finite(x)
        return round(v * scale, 2) if v is not None else None
    risk = {"theoretical_max_loss": None if cand.get("unlimited_loss") else scaled(cand.get("theoretical_max_loss")),
            "unlimited_loss": bool(cand.get("unlimited_loss")),
            "modeled_p10": scaled(cand.get("pnl_p10")), "modeled_worst_loss": scaled(cand.get("modeled_worst_loss")),
            "expected_shortfall": scaled(cand.get("expected_shortfall")), "capital_required": scaled(cand.get("capital_required")),
            "expected_pnl_base": scaled((cand.get("expected_pnl") or {}).get("base")), "costs": scaled(cand.get("costs")),
            "modeled_p_profit": _finite(cand.get("p_profit")), "breakevens": cand.get("breakevens") or [],
            "words": {"theoretical_max_loss": "the most it can lose from the contract terms",
                      "modeled_p10": "the model's 10th-percentile result (before costs)",
                      "expected_shortfall": "the average of the model's worst tenth of outcomes",
                      "capital_required": "the money it ties up here; a broker's margin or buying-power requirement can differ"}}

    # The exit: the opportunity's levels when it came from one, otherwise the forecast range's edge at the horizon.
    opp = None
    d3 = getattr(svc, "discover3", None)
    row = svc.db.one("SELECT opportunity_id FROM studies WHERE id = ?", (st["id"],))
    opp_id = row["opportunity_id"] if row else None
    if opp_id and d3 is not None:
        try:
            opp = d3.candidate(opp_id)
        except (RequestError, KeyError, ValueError):
            opp = None
    bullish = (instrument == "equity" and side == "buy") or (instrument == "option" and str((legs[0] if legs else {}).get("option_type", "")).lower() == "call")
    edge_pct = (lows[-1] if bullish else highs[-1]) if (lows and highs) else None
    level = round(last * (1 + edge_pct / 100), 2) if (last and edge_pct is not None) else None
    stop = _finite((opp or {}).get("invalidation")) or level
    target = _finite((opp or {}).get("target"))
    end = add_trading_days(datetime.now(NY).date(), h)
    time_exit = datetime(end.year, end.month, end.day, 15, 50, tzinfo=NY).isoformat()
    stop_from = "the opportunity's invalidation" if (opp and _finite(opp.get("invalidation"))) else f"the edge of {_range_words(fc)}"
    rules = [f"Out by {end.isoformat()} (the study's {h}-trading-day horizon)."]
    if stop:
        rules.append(f"Wrong if {sym} {'closes below' if bullish else 'closes above'} ${stop:,.2f} ({stop_from}).")
    if target:
        rules.append(f"Target {sym} ${target:,.2f} (the opportunity's).")
    rules.append("A record to act on, not an order: nothing sells it for you unless you place a stop at your broker "
                 "(the autopilot watches only the trades it made itself, and only while the app or the daemon runs).")
    # An exit plan's stop and target are prices of what is held: on an option they are the stock's, so they go in the rules.
    exit_plan = C.ExitPlan(stop=stop if instrument == "equity" else None, target=target if instrument == "equity" else None,
                           time_exit=time_exit, rules=rules)

    direction = st.get("direction") or ("LONG" if bullish else "SHORT")
    if scen is not None and scen.get("case") in CASE_DIRECTION:
        direction = CASE_DIRECTION[scen["case"]]                 # a bullish or bearish case carries its own direction
        if scen.get("invalidation"):
            rules.insert(1, f"Wrong if {scen['invalidation']} (the {scen['case_label']} case's invalidation).")
        if scen.get("trigger"):
            rules.insert(1, f"Conditional on: {scen['trigger']} (the {scen['case_label']} case's trigger).")
        exit_plan = C.ExitPlan(stop=exit_plan.stop, target=exit_plan.target, time_exit=exit_plan.time_exit, rules=rules)
    thesis = (f"{cand.get('label')} on {sym} from study {st['id']} ({st.get('thesis')}, {h} trading days, {direction.lower()}). "
              + (f"Base path {scenario['expected_pct']:+.2f}%; {scenario['range_words']} {scenario['low_pct']:+.1f}% to {scenario['high_pct']:+.1f}%. "
                 if scenario["expected_pct"] is not None and scenario["low_pct"] is not None else "")
              + (f"Model {scenario['model_version']}, {str(scenario['calibration']).replace('_', ' ')}." if scenario["model_version"] else "")
              + (f" Scenario: the {scen['case_label'] or 'base'} case of scenario version {scen['version']} ({scen['origin']})"
                 + (f", if {scen['trigger']}" if scen.get("trigger") else "") + (f"; valuation {scen['valuation_id']}" if scen.get("valuation_id") else "")
                 + (f"; decision {scen['decision_id']}" if scen.get("decision_id") else "") + "." if scen else ""))[:2000]

    execution: dict[str, Any] = {**rt}
    if rt["route"] == "automatic" and limit and desk is not None:
        from .trading import ProposalIn
        body = ProposalIn(symbol=sym, side=side, quantity=qty, limit_price=limit, instrument=instrument, option=option, thesis=thesis,
                          exit_plan=exit_plan, study_id=st["id"], opportunity_id=opp_id, source="research")
        execution["proposal"] = body.model_dump(mode="json")
        try:
            pv = desk.preview(body)
            execution["check"] = {"ok": pv["risk"]["ok"], "text": pv["risk"]["text"], "describe": pv["describe"],
                                  "failing": [c["label"] + ": " + str(c["text"]) for c in pv["risk"]["checks"] if c["status"] != "pass"]}
        except RequestError as exc:
            execution["check"] = {"ok": False, "text": str(exc), "describe": None, "failing": [str(exc)]}
    elif rt["route"] == "automatic":
        execution.update(route="research", label=ROUTE_WORDS["research"], reasons=["No price to set a limit from"])
    if execution["route"] == "manual":
        lines = []
        if instrument == "equity":
            lines.append(f"{'Buy' if side == 'buy' else 'Sell short'} {qty:g} {sym} shares, limit ${limit:,.2f}, day order" if limit else f"{side} {qty:g} {sym}")
        for lg in legs if instrument != "equity" else []:
            n = float(lg.get("contracts") or 1) * scale
            px = _finite(lg.get("mid"))
            lines.append(f"{'Buy' if (lg.get('side') or 'buy') == 'buy' else 'Sell'} {n:g} {sym} {lg.get('expiration')} "
                         f"{float(lg.get('strike') or 0):g} {str(lg.get('option_type', '')).upper()}" + (f" (mid ${px:,.2f} when studied)" if px else ""))
        if instrument == "multi_leg":
            net = _finite(cand.get("cost"))
            lines.append(f"As one multi-leg order, net {'credit' if cand.get('credit') else 'debit'} near ${abs(net) * scale:,.2f} in all at the study's midpoints."
                         if net is not None else "As one multi-leg order.")
        execution["manual_order"] = lines
    if st.get("status") == "partial":
        notes.append("The study was partial: see its Missing list before relying on it.")
    if scen is not None and scen.get("case") in CASE_DIRECTION and scen.get("decision_status") != "qualified":
        notes.append(f"Conditional: it was chosen under the {scen['case_label']} case, whose trigger has not been recorded as observed.")
    if scenario["calibration"] != "calibrated":
        notes.append("The forecast behind it is not calibrated for this horizon: its range is the model's, not a validated one.")

    return {"kind": "draft_trade", "version": 1, "created_at": C.utc_now(), "symbol": sym,
            "source": {"study_id": st["id"], "opportunity_id": opp_id, "study_as_of": st.get("created_at"),
                       "data_source": view.get("data_source"), "data_mode": view.get("data_mode"), "scenario": scen},
            "strategy": {"key": cand.get("key"), "label": cand.get("label"), "instrument": instrument,
                         "legs": [{k: lg.get(k) for k in ("side", "option_type", "strike", "expiration", "contracts", "bid", "ask", "mid", "option_symbol")} for lg in legs],
                         "legs_text": cand.get("legs_text") or []},
            "direction": direction, "horizon_days": h, "scenario": scenario, "quantity": qty,
            "unit": (("share" if instrument == "equity" else "contract" if instrument == "option" else "spread") + ("" if qty == 1 else "s")), "sizing": sizing,
            "limit": {"price": limit, "basis": basis}, "risk": risk, "exit_plan": exit_plan.model_dump(mode="json"),
            "execution": execution, "thesis": thesis, "notes": notes,
            "note": "A draft: nothing is proposed or sent until you press Propose on the Trading Desk, and a proposal waits for your approval."}


def install(svc: Any) -> None:
    from ..api_v3 import register

    def routes(router: APIRouter, svc: Any) -> None:
        @router.post("/drafts")
        def draft(body: DraftIn) -> dict[str, Any]:
            return build(svc, body)

    register(svc, routes)


__all__ = ["DraftIn", "build", "route", "routes_for", "order_shape", "install", "ROUTE_WORDS"]
