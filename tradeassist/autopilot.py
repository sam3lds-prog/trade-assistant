"""The capped autopilot (3.0 phase 7): buys and sells within a written policy, with no one clicking approve.

It acts only when the trading policy is in `capped_autopilot` mode, and then only as the policy allows: on
Discover candidates that are **autopilot-eligible** (a validated edge after costs, no conflict, liquid, real
prices), long only, at or above the policy's minimum score and in its horizons, at most
`autopilot_max_orders_per_day` new buys a day, each sized to the policy's order, capital and daily limits. Every
order it writes goes through the same risk engine, approval record and execution as yours: it records an approval
by "autopilot" on the "policy" channel, bound to the order's hash like any other.

**Accounts.** `shadow`: its orders fill on the paper broker in a book of their own, so what it would have done can
be compared with what you did. `paper`: the paper account. `live`: real orders, and only after it has earned it:

**Promotion gates for live** (all must hold, checked at every run):

1. AUTOPILOT_LIVE=1 in .env, besides everything live execution itself needs (phase 6);
2. at least `autopilot_shadow_days` trading days on which it decided in the shadow or paper account;
3. its shadow and paper record over those days is better than a single day's loss stop (it has not lost more than
   `max_daily_loss` in total);
4. no order of its own is in an unknown state.

**Exits.** Each position it opened is closed by a sale it writes itself when the price reaches the stop or the
target recorded from the opportunity, or the time runs out (the close for an intraday trade, five trading days for
a swing). A sale reduces risk, so the loss stop never blocks it.

Everything it decides, including deciding to do nothing and why, is written to the audit log and the event stream.
"""

from __future__ import annotations

import json
import math
from datetime import datetime, timezone
from typing import Any

from . import contracts as C
from .marketdays import add_trading_days
from .optionmath import NY
from .providers.base import DataError
from .service import RequestError

HORIZON_DAYS = {"intraday": 0, "swing": 5, "event": 3}


def _iso(t: datetime) -> str:
    return t.astimezone(timezone.utc).isoformat(timespec="seconds")


class Autopilot:
    def __init__(self, svc: Any):
        self.svc = svc
        self.db = svc.db
        self.last: dict[str, Any] | None = None

    @property
    def desk(self) -> Any:
        return self.svc.trading

    # --- what it has earned --------------------------------------------------------------------------------
    def record(self) -> dict[str, Any]:
        """Its shadow and paper record: the days it decided and the result of its positions."""
        d = self.desk
        days = self.db.one("""SELECT COUNT(DISTINCT substr(created_at, 1, 10)) AS n FROM trade_proposals
                              WHERE source = 'autopilot' AND account_kind IN ('shadow', 'paper') AND mode = ?""", (d.mode,))["n"]
        result = 0.0
        measured = True
        for kind in ("shadow", "paper"):
            realized = self.db.one("""SELECT COALESCE(SUM(l.amount), 0) AS s FROM budget_ledger l JOIN trade_proposals p ON p.id = l.proposal_id
                                      WHERE l.kind = 'realized' AND l.account_kind = ? AND l.mode = ? AND p.source = 'autopilot'""",
                                   (kind, d.mode))["s"]
            result += float(realized or 0)
            mine = self._own_positions(kind)
            for pos in d.positions(kind)["positions"]:
                if pos["key"] in mine:
                    if pos.get("unrealized") is None:
                        measured = False
                    else:
                        result += pos["unrealized"]
        return {"days": int(days or 0), "result": round(result, 2) if measured else None}

    def _own_positions(self, account_kind: str) -> set[str]:
        rows = self.db.query("""SELECT DISTINCT symbol FROM trade_proposals WHERE source = 'autopilot' AND account_kind = ? AND mode = ?
                                AND side = 'buy' AND status IN ('filled', 'partially_filled')""", (account_kind, self.desk.mode))
        return {r["symbol"] for r in rows}

    def gates(self) -> dict[str, Any]:
        d = self.desk
        pol = d.policy()
        checks = []

        def add(key: str, label: str, ok: bool | None, text: str) -> None:
            checks.append({"key": key, "label": label, "status": "pass" if ok else ("unknown" if ok is None else "fail"), "text": text})
        add("mode", "Policy", pol.mode == "capped_autopilot", f"the policy is {pol.mode}" + ("" if pol.mode == "capped_autopilot" else ": the autopilot is off"))
        ks = d.kill_switch()
        add("kill_switch", "Kill switch", not ks.get("on"), "on" if ks.get("on") else "off")
        if pol.account_kind == "live":
            add("env", "Switched on for live", bool(self.svc.settings.autopilot_live), "AUTOPILOT_LIVE=1" if self.svc.settings.autopilot_live
                else "AUTOPILOT_LIVE is not set in .env")
            add("live", "Live execution", d.live_ready, "ready" if d.live_ready else "; ".join(d.live_reasons))
            rec = self.record()
            add("shadow_days", "Days of practice", rec["days"] >= pol.autopilot_shadow_days,
                f"{rec['days']} of {pol.autopilot_shadow_days} trading days decided in the shadow or paper account")
            add("shadow_result", "Practice result", None if rec["result"] is None else rec["result"] > -pol.max_daily_loss,
                "not measured" if rec["result"] is None else f"${rec['result']:+,.2f} over them (it must not have lost more than ${pol.max_daily_loss:,.0f})")
            unknown = self.db.one("SELECT COUNT(*) AS n FROM trade_proposals WHERE source = 'autopilot' AND status = 'unknown'")["n"]
            add("unknown", "Nothing unresolved", unknown == 0, f"{unknown} of its orders in an unknown state" if unknown else "none")
        return {"ok": all(c["status"] == "pass" for c in checks), "checks": checks, "account_kind": pol.account_kind}

    # --- one run ----------------------------------------------------------------------------------------------
    def run(self, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        d = self.desk
        pol = d.policy()
        if pol.mode != "capped_autopilot":
            return {"skipped": "the autopilot is off (the policy is not in capped_autopilot mode)"}
        now = datetime.now(NY)
        out: dict[str, Any] = {"at": _iso(now), "account_kind": pol.account_kind, "exits": [], "entries": [], "skipped": [], "gates": None}
        g = self.gates()
        out["gates"] = g
        if not g["ok"]:
            out["skipped"].append({"why": "; ".join(f"{c['label']}: {c['text']}" for c in g["checks"] if c["status"] != "pass")})
            return self._done(out)
        out["exits"] = self._exits(pol, now)
        today = now.date().isoformat()
        start = datetime.fromisoformat(today).replace(tzinfo=NY)
        bought = self.db.one("""SELECT COUNT(*) AS n FROM trade_proposals WHERE source = 'autopilot' AND side = 'buy' AND account_kind = ?
                                AND mode = ? AND created_at >= ? AND status NOT IN ('blocked', 'cancelled', 'expired', 'rejected', 'failed')""",
                             (pol.account_kind, d.mode, _iso(start)))["n"]
        room = pol.autopilot_max_orders_per_day - int(bought)
        if room <= 0:
            out["skipped"].append({"why": f"{bought} buys today, the most the policy allows"})
            return self._done(out)
        d3 = getattr(self.svc, "discover3", None)
        if d3 is None:
            out["skipped"].append({"why": "Discover is not running"})
            return self._done(out)
        book = d.book(pol.account_kind)
        busy = {p["symbol"] for p in book["open_proposals"]} | set(book["positions"])
        for c in d3.feed(eligible_only=True, direction="LONG", limit=50):
            if room <= 0:
                break
            why = None
            if c["horizon"] not in pol.autopilot_horizons:
                why = f"horizon {c['horizon']} is not in the policy"
            elif c["rank_score"] < pol.autopilot_min_score:
                why = f"score {c['rank_score']:.0f} is under {pol.autopilot_min_score:.0f}"
            elif c["symbol"] in busy:
                why = "already held or proposed"
            if why:
                out["skipped"].append({"symbol": c["symbol"], "why": why})
                continue
            got = self._enter(c, pol, book)
            (out["entries"] if got.get("proposal") else out["skipped"]).append(got)
            if got.get("status") in ("authorized", "submitted", "filled"):
                room -= 1
                busy.add(c["symbol"])
        return self._done(out)

    def _done(self, out: dict[str, Any]) -> dict[str, Any]:
        self.last = out
        with self.db.tx() as cur:
            cur.execute("INSERT INTO audit_log (at, actor, action, subject, data_json) VALUES (?, 'autopilot', 'autopilot.run', ?, ?)",
                        (out["at"], out["account_kind"], json.dumps(out, default=str)[:20000]))
        self.desk.bus.publish("autopilot.run", None, entries=len(out["entries"]), exits=len(out["exits"]), skipped=len(out["skipped"]),
                              gates_ok=bool(out["gates"] and out["gates"]["ok"]))
        return out

    def _size(self, ask: float, pol: Any, book: dict[str, Any]) -> int:
        room = min(pol.max_order, pol.trial_capital - book["capital_in_use"], pol.max_daily_new_buys - book["today_new_buys"])
        if pol.account_kind == "live":
            room = min(room, self.svc.settings.live_max_order)
        return max(0, int(math.floor(room / ask))) if ask > 0 else 0

    def _authorize(self, p: dict[str, Any]) -> dict[str, Any]:
        d = self.desk
        a = d._ask_approval(p["id"], notify=False)
        return d.decide(a["id"], True, p["order_hash"], actor="autopilot", channel="policy")

    def _enter(self, c: dict[str, Any], pol: Any, book: dict[str, Any]) -> dict[str, Any]:
        from .v3.trading import ProposalIn
        d = self.desk
        try:
            ask = float(self.svc.market.quote(c["symbol"]).get("ask") or 0)
        except (DataError, ValueError):
            return {"symbol": c["symbol"], "why": "no quote"}
        qty = self._size(ask, pol, book)
        if qty < 1:
            return {"symbol": c["symbol"], "why": f"one share at ${ask:,.2f} does not fit the order, capital and daily limits"}
        days = HORIZON_DAYS.get(c["horizon"], 5)
        exit_at = (datetime.now(NY).replace(hour=15, minute=50, second=0, microsecond=0) if days == 0 else
                   datetime.combine(add_trading_days(datetime.now(NY).date(), days), datetime.min.time()).replace(hour=15, minute=50, tzinfo=NY))
        edge = c.get("edge") or {}
        thesis = (f"Autopilot: {', '.join(c['kinds'])} found by {', '.join(c['sources'])}; score {c['rank_score']:.0f}; "
                  f"{edge.get('expected_pct', 0):+.2f}% after costs over {edge.get('cases', 0)} past cases.")
        try:
            p = d.create(ProposalIn(symbol=c["symbol"], side="buy", quantity=qty, thesis=thesis, opportunity_id=c["id"], source="autopilot",
                                    exit_plan=C.ExitPlan(stop=c.get("invalidation") or None, target=c.get("target") or None,
                                                         time_exit=_iso(exit_at), rules=["autopilot"])), actor="autopilot")
        except RequestError as exc:
            return {"symbol": c["symbol"], "why": str(exc)}
        if p["status"] != "awaiting_approval":
            return {"symbol": c["symbol"], "proposal": p["id"], "status": p["status"], "why": (p.get("risk") or {}).get("text")}
        r = self._authorize(p)
        if r["status"] == "authorized":
            d.notifier.enqueue("autopilot.entry", f"Autopilot: {r['describe']}", f"{r['describe']} in the {r['account_kind']} account. {thesis}",
                               proposal_id=r["id"], dedupe_key=f"autopilot:{r['id']}")
        return {"symbol": c["symbol"], "proposal": r["id"], "status": r["status"], "quantity": qty}

    def _exits(self, pol: Any, now: datetime) -> list[dict[str, Any]]:
        from .v3.trading import ProposalIn
        d = self.desk
        out = []
        held = {p["symbol"]: p for p in d.positions(pol.account_kind)["positions"]}
        rows = self.db.query("""SELECT * FROM trade_proposals WHERE source = 'autopilot' AND side = 'buy' AND account_kind = ? AND mode = ?
                                AND status IN ('filled', 'partially_filled') ORDER BY created_at""", (pol.account_kind, d.mode))
        open_sells = {p["symbol"] for p in d.book(pol.account_kind)["open_proposals"] if p["side"] == "sell"}
        for r in rows:
            sym = r["symbol"]
            pos = held.get(sym)
            if not pos or sym in open_sells:
                continue
            plan = json.loads(r["exit_plan_json"] or "{}")
            mark = pos.get("mark")
            why = None
            if mark is not None and plan.get("stop") and mark <= plan["stop"]:
                why = f"the stop: {mark:.2f} at or under {plan['stop']:.2f}"
            elif mark is not None and plan.get("target") and mark >= plan["target"]:
                why = f"the target: {mark:.2f} at or over {plan['target']:.2f}"
            elif plan.get("time_exit") and now >= datetime.fromisoformat(plan["time_exit"]):
                why = "its time ran out"
            if not why:
                continue
            try:
                p = d.create(ProposalIn(symbol=sym, side="sell", quantity=pos["quantity"], thesis=f"Autopilot exit: {why}", source="autopilot"),
                             actor="autopilot")
            except RequestError as exc:
                out.append({"symbol": sym, "why": str(exc)})
                continue
            status = self._authorize(p)["status"] if p["status"] == "awaiting_approval" else p["status"]
            out.append({"symbol": sym, "proposal": p["id"], "status": status, "why": why})
            open_sells.add(sym)
        return out

    def status(self) -> dict[str, Any]:
        pol = self.desk.policy()
        return {"gates": self.gates(), "record": self.record(), "last": self.last,
                "limits": {"max_orders_per_day": pol.autopilot_max_orders_per_day, "min_score": pol.autopilot_min_score,
                           "horizons": pol.autopilot_horizons, "shadow_days": pol.autopilot_shadow_days}}


__all__ = ["Autopilot"]
