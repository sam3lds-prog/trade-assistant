"""Opportunities from a study's scenarios (4.4): Qualified setup, Watch for trigger, or No setup, with reason codes.

After the one-click pipeline values the study's fixed strategies under each case (v3/valuation.py), a deterministic
evaluator decides what, if anything, to highlight. Every setup it considers is one of the valuation's own: a single
structure sized to the budget and the loss limit, or the What-If allocator's split for a case. Each one is checked:

| check | passes when |
| --- | --- |
| fresh usable quotes | the snapshot's stock quote is within the policy's quote age; real-time (not the sandbox's delayed quotes) |
| liquidity | every option leg has a two-sided quote, its spread at most OPPORTUNITY_MAX_SPREAD_PCT of the midpoint |
| eligible strategy | the Trading Desk or your broker can take it (its route is not research-only); its loss has a limit |
| payoff | modeled net P&L after round-trip costs at least OPPORTUNITY_MIN_NET_PCT of the capital it ties up |
| risk | the contract's own most-it-can-lose and the modeled worst tenth both within the loss limit; inside the budget |
| support | no trigger: a validated directional edge, or 30+ independent past cases, in the setup's direction; a trigger: |
|         | independently observed (recorded with its source after the study's cutoff) |
| timing | the study's sessions have not passed; a trigger's session has not passed unobserved |

- **Qualified setup**: every check passes. Without a trigger it is valued under the Central case (the base path) and
  must stay within the loss limit in every case; with a trigger, under that trigger's case once it is observed.
- **Watch for trigger**: a Bullish or Bearish case's setup passes on payoff, risk, liquidity and eligibility if its
  trigger happens. It stays conditional until the trigger is recorded as observed. A bullish case carries a long
  direction and a bearish case a short one.
- **No setup**: the most material failed check, and how many setups each check stopped.

Qualified setups are ranked by their worst-case modeled net P&L across the cases; Watch setups by their net P&L in
their own case. A projected case payoff is not a historical edge: a scenario observation in Discover is never eligible
for the autopilot. Demo and sandbox results are labelled as examples. Nothing here proposes or sends an order.
"""

from __future__ import annotations

import json
import logging
import math
from datetime import date, datetime, timezone
from typing import Any, Literal

from pydantic import Field

from .. import contracts as C
from ..optionmath import NY
from ..service import RequestError

log = logging.getLogger(__name__)

EVALUATOR_VERSION = "opportunity-4.4"
VALIDATED_CASES = 30
STATUS_LABELS = {"qualified": "Qualified setup", "watch": "Watch for trigger", "no_setup": "No setup"}
REASONS = {
    "valuation_failed": "the strategies could not be valued",
    "expired_session": "the study's sessions have passed",
    "stale_quote": "the stock quote is older than the policy's limit",
    "delayed_quotes": "the quotes are delayed (sandbox): not fresh enough to qualify",
    "expired_trigger": "the trigger's session has passed without it being observed",
    "no_two_sided_quote": "an option leg has no two-sided quote",
    "wide_spread": "an option leg's spread is wider than OPPORTUNITY_MAX_SPREAD_PCT of its midpoint",
    "cost_eroded": "spreads and fees consume the expected payoff",
    "payoff_below_minimum": "the modeled net upside is below OPPORTUNITY_MIN_NET_PCT of the capital",
    "no_base_support": "no validated directional edge or setup history behind the base case",
    "scenario_downside_over_limit": "its modeled worst tenth is over the loss limit",
    "contract_loss_over_limit": "the most it can lose (the contract terms) is over the loss limit",
    "unlimited_loss": "its loss has no limit",
    "over_budget": "it needs more capital than the budget",
    "not_eligible": "research only: neither the Trading Desk nor a broker order can take it",
    "direction_mismatch": "its direction is not the case's",
}
PRIORITY = list(REASONS)                       # the order above is the order of materiality
GLOBAL = {"valuation_failed", "expired_session"}
QUALIFY_ONLY = {"stale_quote", "delayed_quotes"}       # a Watch is rechecked when its trigger happens


def _f(x: Any) -> float | None:
    try:
        v = float(x)
    except (TypeError, ValueError):
        return None
    return v if math.isfinite(v) else None


def setups_from(val: dict[str, Any], lim: dict[str, Any]) -> list[dict[str, Any]]:
    """Every setup the valuation offers: each structure sized to the budget and the loss limit (one unit at least, so
    one that does not fit is still judged and says why), and the allocator's split for each case."""
    out = []
    by_key = {s["key"]: s for s in val.get("structures") or []}
    for s in by_key.values():
        cap, ml = _f(s.get("capital")) or 0.0, _f(s.get("max_loss"))
        units = int(lim["budget"] // cap) if cap > 0 else 1
        if ml:
            units = min(units, int(lim["loss_limit"] // ml))
        units = max(1, units)
        out.append({"key": f"single:{s['key']}", "kind": "single", "label": f"{s['label']} ×{units}", "parts": [(s, units)],
                    "direction": s["direction"]})
    for m in val.get("mixes") or []:
        if not str(m.get("key", "")).startswith("mix:"):
            continue
        parts = [(by_key[p["key"]], int(p["units"])) for p in m.get("parts") or [] if p["key"] in by_key]
        if not parts:
            continue
        dirs = {s["direction"] for s, _ in parts}
        out.append({"key": m["key"], "kind": "mix", "label": m["label"], "parts": parts, "direction": dirs.pop() if len(dirs) == 1 else "MIXED"})
    for su in out:
        parts = su["parts"]
        cases = list(parts[0][0]["by_case"])
        su["by_case"] = {k: {"net": sum(n * s["by_case"][k]["expected_pnl"] for s, n in parts),
                             "worst_tenth": sum(n * s["by_case"][k]["worst_tenth"] for s, n in parts)} for k in cases}
        su["costs"] = sum(n * float(s.get("costs") or 0.0) for s, n in parts)
        su["capital"] = sum(n * float(s.get("capital") or 0.0) for s, n in parts)
        su["unlimited_loss"] = any(s.get("unlimited_loss") for s, _ in parts)
        su["max_loss"] = None if su["unlimited_loss"] else sum(n * float(s.get("max_loss") or 0.0) for s, n in parts)
        su["worst_case"] = min(v["net"] for v in su["by_case"].values()) if su["by_case"] else 0.0
        su["units"] = {s["key"]: n for s, n in parts}
    return out


def _quote_checks(su: dict[str, Any], max_spread: float) -> list[str]:
    fails = []
    for s, _ in su["parts"]:
        for lg in s.get("legs") or []:
            bid, ask = _f(lg.get("bid")), _f(lg.get("ask"))
            if not (bid and ask and ask >= bid > 0):
                fails.append("no_two_sided_quote")
            sp = _f(lg.get("spread_pct"))
            if sp is not None and sp > max_spread:
                fails.append("wide_spread")
    return list(dict.fromkeys(fails))


def _support(direction: str, readiness: dict[str, Any] | None, base_pct: float | None, analogs: dict[str, Any] | None) -> tuple[bool, str]:
    """A supported base case (a validated directional edge pointing this way) or validated setup history."""
    want = 1.0 if direction == "LONG" else -1.0 if direction == "SHORT" else 0.0
    if not want:
        return False, "a volatility structure has no direction for the base case to support"
    d = (readiness or {}).get("directional") or {}
    if d.get("status") == "edge" and base_pct is not None and base_pct * want > 0:
        return True, "the base forecast's directional edge was validated on its holdout and points this way"
    a = analogs or {}
    if (a.get("independent") or 0) >= VALIDATED_CASES and _f(a.get("median_pct")) is not None and float(a["median_pct"]) * want > 0:
        return True, f"{a['independent']} independent past cases of this setup moved this way (median {float(a['median_pct']):+.2f}%)"
    return False, REASONS["no_base_support"]


def evaluate(*, symbol: str, version: dict[str, Any], valuation: dict[str, Any], thresholds: dict[str, Any], routes: dict[str, str],
             readiness: dict[str, Any] | None = None, analogs: dict[str, Any] | None = None, observed: dict[str, dict[str, Any]] | None = None,
             mode: str = "live", realtime: bool = True, today: date | None = None) -> dict[str, Any]:
    """The decision for one valuation. Pure: everything it reads is passed in (`routes`: structure key → route)."""
    today = today or datetime.now(NY).date()
    observed = observed or {}
    lim = {"budget": float(thresholds["budget"]), "loss_limit": float(thresholds["loss_limit"])}
    min_net, max_spread = float(thresholds["min_net_pct"]), float(thresholds["max_spread_pct"])
    sessions = version.get("sessions") or []
    last_day = date.fromisoformat(sessions[-1]["date"]) if sessions else today
    cases = {c["key"]: c for c in version.get("cases") or []}
    base_pct = _f(((version.get("base") or {}).get("return_pct") or [None])[-1])
    fresh = valuation.get("freshness") or {}
    globals_: list[str] = []
    if valuation.get("status") == "failed" or not valuation.get("structures"):
        globals_.append("valuation_failed")
    if last_day < today:
        globals_.append("expired_session")
    quote_fails: list[str] = []
    if fresh.get("stale"):
        quote_fails.append("stale_quote")
    if mode == "sandbox" or (not realtime and mode not in ("demo",)):
        quote_fails.append("delayed_quotes")
    cat_session = version.get("catalyst_session")
    cat_day = date.fromisoformat(sessions[int(cat_session) - 1]["date"]) if cat_session and sessions else None

    def trigger_day(case_key: str) -> date:
        # A dated catalyst's case must be seen by the catalyst's session; an undated one by the last session.
        return cat_day if (cat_day and (version.get("catalyst") or {}).get("event_date")) else last_day

    evaluated: list[dict[str, Any]] = []
    for su in setups_from(valuation, lim):
        common: list[str] = []
        common += _quote_checks(su, max_spread)
        if any(routes.get(s["key"]) == "research" for s, _ in su["parts"]):
            common.append("not_eligible")
        if su["unlimited_loss"]:
            common.append("unlimited_loss")
        if su["capital"] > lim["budget"] + 1e-9:
            common.append("over_budget")
        if su["max_loss"] is not None and su["max_loss"] > lim["loss_limit"] + 1e-9:
            common.append("contract_loss_over_limit")
        cap = su["capital"] or 1.0

        def payoff(net: float, cap: float = cap, costs: float = su["costs"]) -> list[str]:
            if 100.0 * net / cap >= min_net:
                return []
            return ["cost_eroded"] if 100.0 * (net + costs) / cap >= min_net else ["payoff_below_minimum"]

        # Without a trigger: the Central case (the base path), inside the loss limit in every case.
        if "central" in cases and su["direction"] in ("LONG", "SHORT"):
            net = su["by_case"]["central"]["net"]
            fails = list(globals_) + quote_fails + common + payoff(net)
            ok, why = _support(su["direction"], readiness, base_pct, analogs)
            if not ok:
                fails.append("no_base_support")
            if max(v["worst_tenth"] for v in su["by_case"].values()) > lim["loss_limit"] + 1e-9:
                fails.append("scenario_downside_over_limit")
            evaluated.append({"setup": su, "case": None, "mode": "base", "net": net, "fails": list(dict.fromkeys(fails)), "support": why})
        # A case with a trigger: Watch until the trigger is observed, then Qualified.
        for k in ("bullish", "bearish"):
            c = cases.get(k)
            if c is None or not c.get("quantified"):
                continue
            want = "LONG" if c["direction"] == "up" else "SHORT" if c["direction"] == "down" else None
            fails = list(globals_) + common
            if su["direction"] != want and su["direction"] != "EVENT":
                fails.append("direction_mismatch")
            net = su["by_case"][k]["net"]
            fails += payoff(net)
            if su["by_case"][k]["worst_tenth"] > lim["loss_limit"] + 1e-9:
                fails.append("scenario_downside_over_limit")
            seen = observed.get(k)
            if seen:
                fails += quote_fails
            elif trigger_day(k) < today:
                fails.append("expired_trigger")
            evaluated.append({"setup": su, "case": k, "mode": "triggered" if seen else "watch", "net": net, "fails": list(dict.fromkeys(fails)),
                              "direction": want, "observed": seen})

    qualified = sorted([e for e in evaluated if not e["fails"] and e["mode"] in ("base", "triggered")],
                       key=lambda e: (-e["setup"]["worst_case"], e["setup"]["capital"]))
    watch = sorted([e for e in evaluated if not e["fails"] and e["mode"] == "watch"], key=lambda e: (-e["net"], e["setup"]["capital"]))
    counts: dict[str, int] = {}
    for e in evaluated:
        for f in e["fails"]:
            counts[f] = counts.get(f, 0) + 1
    if qualified:
        status, best = "qualified", qualified[0]
    elif watch:
        status, best = "watch", watch[0]
    else:
        status = "no_setup"
        # The setup closest to qualifying (fewest failed checks, then the best payoff), and its most material failure.
        best = min(evaluated, key=lambda e: (len(e["fails"]), -e["net"])) if evaluated else None
    if status == "no_setup":
        codes = sorted(set(globals_) or (best["fails"] if best else ["valuation_failed"]), key=PRIORITY.index)
        reason = {"code": codes[0], "text": REASONS[codes[0]]}
    else:
        reason = None
    shown = best

    def describe(e: dict[str, Any] | None) -> dict[str, Any] | None:
        if e is None:
            return None
        su = e["setup"]
        k = e["case"] or "central"
        c = cases.get(k) or {}
        direction = e.get("direction") or su["direction"]
        until = trigger_day(e["case"]) if e["mode"] == "watch" else last_day
        return {"key": su["key"], "label": su["label"], "kind": su["kind"], "units": su["units"], "direction": direction,
                "case": e["case"], "case_label": C.CASE_LABELS.get(k) if e["case"] else "Central (the base path)", "mode": e["mode"],
                "trigger": c.get("trigger") if e["case"] else None, "invalidation": c.get("invalidation"),
                "observed": e.get("observed"), "expires_on": until.isoformat(),
                "net_pnl": round(e["net"], 2), "net_pct": round(100.0 * e["net"] / (su["capital"] or 1.0), 2),
                "gross_pnl": round(e["net"] + su["costs"], 2), "costs": round(su["costs"], 2), "capital": round(su["capital"], 2),
                "max_loss": round(su["max_loss"], 2) if su["max_loss"] is not None else None, "unlimited_loss": su["unlimited_loss"],
                "worst_case": round(su["worst_case"], 2), "worst_tenth": round(su["by_case"][k]["worst_tenth"], 2) if k in su["by_case"] else None,
                "by_case": {kk: {"net": round(v["net"], 2), "worst_tenth": round(v["worst_tenth"], 2)} for kk, v in su["by_case"].items()},
                "structures": [s["key"] for s, _ in su["parts"]], "fails": [{"code": f, "text": REASONS[f]} for f in e["fails"]],
                "support": e.get("support")}

    setup = describe(shown)
    answers = None
    if setup and status != "no_setup":
        what = f"{setup['label']} on {symbol}, {'long' if setup['direction'] == 'LONG' else 'short' if setup['direction'] == 'SHORT' else setup['direction'].lower()}"
        when_ = (f"Only if {setup['trigger']}, by {setup['expires_on']}." if status == "watch" else
                 f"Now: the {setup['case_label']} trigger was observed ({(setup['observed'] or {}).get('title')}); until {setup['expires_on']}."
                 if setup["mode"] == "triggered" else f"Now, until the last session ({setup['expires_on']}).")
        risk = "no limit" if setup["unlimited_loss"] else f"{setup['max_loss']:,.0f}"
        why = (f"Modeled net {setup['net_pnl']:+,.0f} ({setup['net_pct']:+.1f}% of {setup['capital']:,.0f} capital) after {setup['costs']:,.0f} of "
               f"round-trip costs under the {setup['case_label']} case; worst tenth {setup['worst_tenth']:,.0f}; most it can lose {risk}, within "
               f"the {lim['loss_limit']:,.0f} limit; spreads within {max_spread:g}% of the midpoint."
               + (f" Support: {setup['support']}." if setup.get("support") else ""))
        invalid = setup["invalidation"] or "the base forecast's own invalidators"
        answers = {"what": what, "when": when_, "why": why, "invalidates": invalid}
    example = mode in ("demo", "sandbox")
    return {"evaluator": EVALUATOR_VERSION, "status": status, "label": STATUS_LABELS[status], "setup": setup, "reason": reason,
            "answers": answers, "counts": {k: counts[k] for k in sorted(counts, key=PRIORITY.index)},
            "reasons": [{"code": k, "text": REASONS[k], "setups": counts[k]} for k in sorted(counts, key=PRIORITY.index)],
            "qualified": [describe(e) for e in qualified[:5]], "watch": [describe(e) for e in watch[:5]],
            "thresholds": {**lim, "min_net_pct": min_net, "max_spread_pct": max_spread}, "example": example,
            "example_words": ("Demo example: the prices are made up." if mode == "demo" else
                              "Sandbox example: the quotes are delayed 15 minutes." if mode == "sandbox" else None),
            "note": ("A projected case payoff, not a historical edge: it is never eligible for the autopilot. Cases have no "
                     "probabilities. Nothing is proposed or sent.")}


def fingerprint(d: dict[str, Any]) -> str:
    """What makes a decision materially different: its status, the setup, its case and size."""
    s = d.get("setup") or {}
    return C.digest({"status": d["status"], "setup": s.get("key") if d["status"] != "no_setup" else None, "case": s.get("case"),
                     "units": s.get("units") if d["status"] != "no_setup" else None, "mode": s.get("mode"),
                     "reason": (d.get("reason") or {}).get("code")})[:16]


class Opportunities:
    def __init__(self, svc: Any):
        self.svc = svc

    def thresholds(self) -> dict[str, Any]:
        from .valuation import limits
        lim = limits(self.svc)
        s = self.svc.settings
        return {"budget": lim["budget"], "loss_limit": lim["loss_limit"], "min_net_pct": s.opportunity_min_net_pct,
                "max_spread_pct": s.opportunity_max_spread_pct}

    def observed(self, study_id: str) -> dict[str, dict[str, Any]]:
        return {r["case_key"]: {**json.loads(r["source_json"]), "observed_at": r["observed_at"], "recorded_at": r["recorded_at"]}
                for r in self.svc.db.query("SELECT * FROM scenario_triggers WHERE study_id = ?", (study_id,))}

    def evaluate_study(self, study_id: str, version: dict[str, Any], valuation: dict[str, Any], today: date | None = None) -> dict[str, Any]:
        from .drafts import routes_for
        got = self.svc.research3.get(study_id)
        st, view = got.get("study") or {}, got.get("view") or {}
        cands = (view.get("strategies") or {}).get("candidates") or []
        try:
            routes = {k: v["route"] for k, v in routes_for(self.svc, cands).items()}
        except Exception:  # noqa: BLE001 - without routes nothing is called eligible
            routes = {c["key"]: "research" for c in cands}
        if self.svc.market.info.mode == "demo":
            # Made-up prices make every route research-only; the demo still shows the decision it would make (an example).
            routes = {k: ("manual" if v == "research" else v) for k, v in routes.items()}
        h = str(st.get("horizon_days") or version.get("horizon_sessions"))
        an = ((view.get("analogs") or {}).get("summary") or {}).get("by_horizon", {}).get(h) or {}
        return evaluate(symbol=st.get("symbol") or version.get("symbol"), version=version, valuation=valuation, thresholds=self.thresholds(),
                        routes=routes, readiness=(st.get("forecast") or {}).get("readiness"), analogs=an, observed=self.observed(study_id),
                        mode=self.svc.market.info.mode, realtime=bool(self.svc.market.info.realtime), today=today)

    # --- after the findings ------------------------------------------------------------------------------------------
    def on_findings(self, study_id: str, version: dict[str, Any], valuation: dict[str, Any]) -> dict[str, Any]:
        d = self.evaluate_study(study_id, version, valuation)
        return self.record(study_id, version, valuation, d)

    def record(self, study_id: str, version: dict[str, Any], valuation: dict[str, Any], d: dict[str, Any]) -> dict[str, Any]:
        fp = fingerprint(d)
        prev = self.svc.db.one("SELECT * FROM scenario_decisions WHERE study_id = ? ORDER BY created_at DESC, rowid DESC LIMIT 1", (study_id,))
        material = prev is None or prev["fingerprint"] != fp
        did = C.new_id("sdec")
        d = {**d, "id": did, "study_id": study_id, "version_id": version["id"], "valuation_id": valuation.get("id"), "made_at": C.utc_now(),
             "fingerprint": fp, "changed": material}
        # The in-app notice: new on a material change to a setup; an unchanged repeat keeps the earlier notice's state.
        notice = "new" if (material and d["status"] in ("qualified", "watch")) else ("none" if material else (prev["notice_state"] if prev else "none"))
        with self.svc.db.tx() as cur:
            cur.execute("UPDATE scenario_decisions SET notice_state = 'seen' WHERE study_id = ? AND notice_state = 'new'", (study_id,))
            cur.execute("""INSERT INTO scenario_decisions (id, study_id, version_id, valuation_id, created_at, status, fingerprint, decision_json,
                           reason_codes_json, notice_state) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                        (did, study_id, version["id"], valuation.get("id"), d["made_at"], d["status"], fp, json.dumps(d, default=str),
                         json.dumps(list(d["counts"])), notice))
        pl = getattr(self.svc, "pipeline", None)
        if pl is not None:
            pl._update(study_id, decision={k: d[k] for k in ("id", "status", "label", "fingerprint", "made_at")})
        d["opportunity_id"] = self.publish(study_id, d)
        if d["opportunity_id"]:
            with self.svc.db.tx() as cur:
                cur.execute("UPDATE scenario_decisions SET opportunity_id = ? WHERE id = ?", (d["opportunity_id"], did))
        if material and d["status"] in ("qualified", "watch"):
            self.notify(study_id, d)
        self.svc.bus.publish("scenario.decision", study_id, status=d["status"], decision_id=did, changed=material)
        return d

    def publish(self, study_id: str, d: dict[str, Any]) -> str | None:
        """A Qualified or Watch decision as a scenario observation in Discover: one per study and direction, updated
        on a repeat run; removed again when the study no longer has a setup."""
        d3 = getattr(self.svc, "discover3", None)
        if d3 is None:
            return None
        st = self.svc.db.one("SELECT symbol, mode FROM studies WHERE id = ?", (study_id,))
        if st is None or st["mode"] != self.svc.market.info.mode:
            return None
        s = d.get("setup") or {}
        if d["status"] == "no_setup" or s.get("direction") not in ("LONG", "SHORT"):
            self.withdraw(study_id)
            return None
        for other in ("LONG", "SHORT"):
            if other != s["direction"]:
                self.withdraw(study_id, other)
        key = f"scenario:{study_id}:{s['direction']}"
        cat = (s.get("trigger") or "")[:200] or None
        horizon = "event" if (s.get("case") and "earnings" in str(cat or "").lower()) else "swing"
        obs = {"source": "study", "source_key": key, "symbol": st["symbol"], "direction": s["direction"], "horizon": horizon, "kind": "scenario",
               "raw_score": 80.0 if d["status"] == "qualified" else 50.0, "catalyst": cat if d["status"] == "watch" else None,
               "edge": {"known": False, "expected_pct": 0.0, "cases": 0, "low_pct": None, "high_pct": None, "validated": False,
                        "independent_cases": None, "basis": "a projected case payoff, not a historical record"},
               "payload": {"scenario": {"study_id": study_id, "decision_id": d["id"], "status": d["status"], "label": d["label"],
                                        "version_id": d["version_id"], "valuation_id": d["valuation_id"], "case": s.get("case"),
                                        "trigger": s.get("trigger"), "expires_on": s.get("expires_on"), "setup": s.get("label"),
                                        "net_pnl": s.get("net_pnl"), "max_loss": s.get("max_loss"), "example": d.get("example")},
                           "setup_label": f"Scenario: {d['label']} ({s.get('label')})", "source_score_scale": "Qualified 80, Watch 50"}}
        d3.ingest([obs])
        r = self.svc.db.one("SELECT candidate_id FROM opportunity_observations WHERE mode = ? AND source = 'study' AND source_key = ?",
                            (self.svc.market.info.mode, key))
        cid = r["candidate_id"] if r else None
        if cid:
            d3.link_study(cid, study_id, st["symbol"])
        return cid

    def withdraw(self, study_id: str, direction: str | None = None) -> None:
        d3 = getattr(self.svc, "discover3", None)
        if d3 is None:
            return
        mode = self.svc.market.info.mode
        rows = self.svc.db.query("SELECT id, candidate_id, symbol, direction, horizon FROM opportunity_observations WHERE mode = ? AND source = 'study' "
                                 "AND source_key LIKE ?", (mode, f"scenario:{study_id}:{direction or ''}%"))
        for r in rows:
            with self.svc.db.tx() as cur:
                cur.execute("DELETE FROM opportunity_observations WHERE id = ?", (r["id"],))
                if r["candidate_id"]:
                    cur.execute("""UPDATE opportunity_candidates SET status = 'retired' WHERE id = ? AND status = 'active' AND NOT EXISTS
                                   (SELECT 1 FROM opportunity_observations WHERE candidate_id = ?)""", (r["candidate_id"], r["candidate_id"]))
            if r["candidate_id"]:
                d3._rebuild(r["symbol"], r["direction"], r["horizon"], only_existing=True)

    def notify(self, study_id: str, d: dict[str, Any]) -> int | None:
        """The optional outbox (SCENARIO_NOTIFY=1): once per material change; the in-app notice is always made."""
        if not self.svc.settings.scenario_notify:
            return None
        desk = getattr(self.svc, "trading", None)
        notifier = getattr(desk, "notifier", None)
        if notifier is None:
            from ..notify import Notifier
            notifier = Notifier(self.svc)
        s, a = d["setup"] or {}, d["answers"] or {}
        sym = self.svc.db.one("SELECT symbol FROM studies WHERE id = ?", (study_id,))["symbol"]
        subject = f"{d['label']}: {sym} {s.get('label')}" + (" (example)" if d.get("example") else "")
        body = "\n".join(x for x in (a.get("what"), a.get("when"), a.get("why"), f"Invalidated if {a.get('invalidates')}.", d["note"]) if x)
        return notifier.enqueue("scenario.decision", subject, body, severity="info", dedupe_key=f"scenario:{study_id}:{d['fingerprint']}")

    # --- triggers, notices, reads --------------------------------------------------------------------------------
    def latest(self, study_id: str) -> dict[str, Any] | None:
        r = self.svc.db.one("SELECT * FROM scenario_decisions WHERE study_id = ? ORDER BY created_at DESC, rowid DESC LIMIT 1", (study_id,))
        if r is None:
            return None
        return {**json.loads(r["decision_json"]), "notice_state": r["notice_state"], "opportunity_id": r["opportunity_id"]}

    def trigger(self, study_id: str, case_key: str, title: str, published_at: str, url: str | None = None, note: str = "") -> dict[str, Any]:
        """Record that a case's trigger happened, with an independent source published after the study's cutoff; then
        value the same strategies again from a fresh snapshot and decide again."""
        if case_key not in ("bullish", "bearish"):
            raise RequestError("Only a Bullish or Bearish case's trigger can be recorded.")
        r = self.svc.db.one("SELECT created_at, pipeline_json FROM studies WHERE id = ?", (study_id,))
        if r is None or not r["pipeline_json"]:
            raise RequestError("This study has no scenarios.")
        p = json.loads(r["pipeline_json"])
        if not p.get("version_id"):
            raise RequestError("This study's scenarios have no version yet.")
        try:
            pub = datetime.fromisoformat(published_at.replace("Z", "+00:00"))
        except ValueError as exc:
            raise RequestError(f"published_at must be a date and time, got {published_at!r}") from exc
        pub = pub if pub.tzinfo else pub.replace(tzinfo=NY)
        cutoff = datetime.fromisoformat(str(r["created_at"]).replace("Z", "+00:00"))
        cutoff = cutoff if cutoff.tzinfo else cutoff.replace(tzinfo=timezone.utc)
        if pub <= cutoff:
            raise RequestError("The source was published before the study's evidence cutoff: it cannot show the trigger happened afterwards.")
        if pub > datetime.now(timezone.utc):
            raise RequestError("The source's publication time is in the future.")
        src = {"title": title.strip()[:200], "url": (url or "").strip()[:500] or None, "published_at": pub.astimezone(timezone.utc).isoformat(),
               "note": note.strip()[:300]}
        with self.svc.db.tx() as cur:
            cur.execute("""INSERT INTO scenario_triggers (study_id, case_key, observed_at, recorded_at, source_json) VALUES (?, ?, ?, ?, ?)
                           ON CONFLICT(study_id, case_key) DO UPDATE SET observed_at = excluded.observed_at, recorded_at = excluded.recorded_at,
                           source_json = excluded.source_json""", (study_id, case_key, src["published_at"], C.utc_now(), json.dumps(src)))
        self.svc.pipeline.revalue(study_id)                     # a fresh snapshot, then the decision again (via the hook)
        return self.latest(study_id) or {}

    def notices(self) -> list[dict[str, Any]]:
        mode = self.svc.market.info.mode
        rows = self.svc.db.query("""SELECT d.*, s.symbol FROM scenario_decisions d JOIN studies s ON s.id = d.study_id
                                    WHERE d.notice_state = 'new' AND s.mode = ? ORDER BY d.created_at DESC LIMIT 20""", (mode,))
        out = []
        for r in rows:
            d = json.loads(r["decision_json"])
            out.append({"id": r["id"], "study_id": r["study_id"], "symbol": r["symbol"], "status": r["status"], "label": d["label"],
                        "setup": (d.get("setup") or {}).get("label"), "when": (d.get("answers") or {}).get("when"), "made_at": r["created_at"],
                        "example": d.get("example")})
        return out

    def seen(self, decision_id: str) -> dict[str, Any]:
        with self.svc.db.tx() as cur:
            cur.execute("UPDATE scenario_decisions SET notice_state = 'seen' WHERE id = ? AND notice_state = 'new'", (decision_id,))
            if cur.rowcount == 0 and self.svc.db.one("SELECT 1 FROM scenario_decisions WHERE id = ?", (decision_id,)) is None:
                raise RequestError(f"No decision {decision_id}")
        return {"notices": self.notices()}


class TriggerIn(C.Strict):
    case: Literal["bullish", "bearish"]
    title: str = Field(min_length=3, max_length=200)
    published_at: str = Field(min_length=10, max_length=40)
    url: str | None = Field(default=None, max_length=500)
    note: str = Field(default="", max_length=300)


def install(svc: Any) -> None:
    from ..api_v3 import register
    svc.opportunities = op = Opportunities(svc)
    svc.on_scenario_findings = op.on_findings

    def routes(router: Any, svc: Any) -> None:
        @router.get("/studies/{study_id}/decision")
        def decision(study_id: str) -> dict[str, Any]:
            return {"decision": op.latest(study_id)}

        @router.post("/studies/{study_id}/decision/trigger")
        def trigger(study_id: str, body: TriggerIn) -> dict[str, Any]:
            return {"decision": op.trigger(study_id, body.case, body.title, body.published_at, body.url, body.note),
                    "pipeline": svc.pipeline.view(study_id)}

        @router.get("/scenarios/notices")
        def notices() -> dict[str, Any]:
            return {"notices": op.notices()}

        @router.post("/scenarios/notices/{decision_id}/seen")
        def seen(decision_id: str) -> dict[str, Any]:
            return op.seen(decision_id)

    register(svc, routes)


__all__ = ["evaluate", "setups_from", "fingerprint", "Opportunities", "install", "REASONS", "STATUS_LABELS", "EVALUATOR_VERSION"]
