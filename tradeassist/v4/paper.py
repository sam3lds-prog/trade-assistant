"""The deployed models' trades on the paper desk, and their record (4.0, phase 6).

When a deployment's paper switch is on (Model Lab, Forward record), each setup the take/pass gate took after
yesterday's close becomes a proposal on the Trading Desk at today's ask, through the same policy, risk engine,
approval and paper broker as every other order. The model never gets a lane of its own:

- **Paper only.** Nothing is written unless the active trading policy's account is `paper` or `shadow` and its mode
  is `paper` or `capped_autopilot`. Under any live policy the switch does nothing, whatever it says.
- **Your approval.** A model proposal waits for your approval like any other. Only when the policy is already in
  `capped_autopilot` mode (on a paper or shadow account) does the policy authorize it, within the autopilot's daily
  order count, counted with the autopilot's own buys.
- **No new limits.** Sizes come from the policy's order, capital and daily limits; the models cannot change a limit.
- **Exits** are the trade's own: the stop and target in daily ranges from the entry and the five-session time limit,
  checked every five minutes in market hours while the app or the daemon runs (software-monitored, never a stop
  resting at a broker, as for the autopilot).

Every proposal is linked to the frozen forecast it came from (`model_trade_links`), so the record can say which
forecasts made money, how paper fills compared with the simulated outcome of the same forecast, and what passing
on them would have done.
"""

from __future__ import annotations

import json
import math
from datetime import datetime, timezone
from typing import Any

from .. import contracts as C
from ..marketdays import add_trading_days, previous_trading_day
from ..optionmath import NY
from ..providers.base import DataError
from ..service import RequestError
from . import has_replays, replay_store

ALLOWED_ACCOUNTS = ("paper", "shadow")
ALLOWED_MODES = ("paper", "capped_autopilot")


def _iso(t: datetime) -> str:
    return t.astimezone(timezone.utc).isoformat(timespec="seconds")


class ModelPaper:
    def __init__(self, svc: Any):
        self.svc = svc
        self.last: dict[str, Any] | None = None

    @property
    def desk(self) -> Any:
        return self.svc.trading

    @property
    def db(self) -> Any:
        return self.svc.db

    # ---- may it act at all ----------------------------------------------------------------------------------
    def deployment(self) -> dict[str, Any] | None:
        if not has_replays(self.svc):
            return None
        from ..intelligence.learning.deploy import active
        dep = active(replay_store(self.svc))
        if dep is None:
            return None
        row = replay_store(self.svc).one("SELECT paper_trading, paper_changed_at FROM deployments WHERE id = ?", (dep["id"],))
        dep["paper_trading"] = bool(row["paper_trading"])
        dep["paper_changed_at"] = row["paper_changed_at"]
        return dep

    def eligibility(self) -> dict[str, Any]:
        pol = self.desk.policy()
        dep = self.deployment()
        checks = []

        def add(key: str, ok: bool, text: str) -> None:
            checks.append({"key": key, "ok": bool(ok), "text": text})
        add("deployment", dep is not None, "a deployed model set" if dep else "nothing is deployed")
        add("switch", bool(dep and dep["paper_trading"]), "the deployment's paper switch is on" if dep and dep["paper_trading"]
            else "the deployment's paper switch is off (Model Lab, Forward record)")
        trade = next((m for m in (dep or {}).get("manifest", {}).get("models", []) if m["task"] == "trade"), None)
        add("trained", bool(trade and trade.get("trained_through")), "its trade model was trained on matured outcomes"
            if trade and trade.get("trained_through") else "its trade model was never trained: the gate takes nothing")
        add("paper_account", pol.account_kind in ALLOWED_ACCOUNTS, f"the policy's account is {pol.account_kind}"
            + ("" if pol.account_kind in ALLOWED_ACCOUNTS else ": model trades go to paper or shadow accounts only"))
        add("paper_mode", pol.mode in ALLOWED_MODES, f"the policy's mode is {pol.mode}"
            + ("" if pol.mode in ALLOWED_MODES else ": model trades need paper or capped_autopilot"))
        add("kill_switch", not self.desk.kill_switch().get("on"), "the kill switch is off" if not self.desk.kill_switch().get("on")
            else "the kill switch is on")
        return {"ok": all(c["ok"] for c in checks), "checks": checks, "deployment": dep and dep["id"],
                "authorizes": pol.mode == "capped_autopilot" and pol.account_kind in ALLOWED_ACCOUNTS}

    def set_switch(self, dep_id: str, on: bool) -> dict[str, Any]:
        store = replay_store(self.svc)
        if store.one("SELECT 1 FROM deployments WHERE id = ?", (dep_id,)) is None:
            raise RequestError(f"No deployment {dep_id}")
        now = _iso(datetime.now(timezone.utc))
        with store.tx() as cur:
            cur.execute("UPDATE deployments SET paper_trading = ?, paper_changed_at = ? WHERE id = ?", (int(on), now, dep_id))
        with self.db.tx() as cur:
            cur.execute("INSERT INTO audit_log (at, actor, action, subject, data_json) VALUES (?, 'you', 'model.paper_switch', ?, ?)",
                        (now, dep_id, json.dumps({"on": on})))
        return {"deployment": dep_id, "paper_trading": on, **self.eligibility()}

    # ---- the job ----------------------------------------------------------------------------------------------
    def run(self, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        now = datetime.now(NY)
        out: dict[str, Any] = {"at": _iso(now), "entries": [], "exits": [], "skipped": []}
        e = self.eligibility()
        out["eligibility"] = e
        if not e["ok"]:
            out["skipped"].append({"why": "; ".join(c["text"] for c in e["checks"] if not c["ok"])})
            return self._done(out)
        pol = self.desk.policy()
        out["exits"] = self._exits(pol, now)
        out["entries"], skipped = self._entries(pol, now, e)
        out["skipped"] += skipped
        return self._done(out)

    def _done(self, out: dict[str, Any]) -> dict[str, Any]:
        self.last = out
        with self.db.tx() as cur:
            cur.execute("INSERT INTO audit_log (at, actor, action, subject, data_json) VALUES (?, 'model', 'model.paper', NULL, ?)",
                        (out["at"], json.dumps(out, default=str)[:20000]))
        return out

    def _today_signals(self, dep: dict[str, Any], now: datetime) -> tuple[str | None, list[dict[str, Any]]]:
        """The setups the gate took after the last session's close before today."""
        last = previous_trading_day(now.date())          # the session that closed before today's
        run_id = dep.get("shadow_run")
        if not run_id:
            return None, []
        rows = replay_store(self.svc).query("""SELECT p.id, p.symbol, p.session, p.model_version, p.setup, p.p_target, p.p_stop, p.expected_return,
                                               p.last_close, p.payload_json FROM predictions p JOIN trade_decisions d ON d.prediction_id = p.id
                                               WHERE p.run_id = ? AND p.task = 'trade' AND p.session = ? AND d.take = 1
                                               ORDER BY p.expected_return DESC""", (run_id, last.isoformat()))
        return run_id, [dict(r) for r in rows]

    def _size(self, ask: float, pol: Any, book: dict[str, Any]) -> int:
        room = min(pol.max_order, pol.trial_capital - book["capital_in_use"], pol.max_daily_new_buys - book["today_new_buys"])
        return max(0, int(math.floor(room / ask))) if ask > 0 else 0

    def _authorized_today(self, pol: Any, now: datetime) -> int:
        start = _iso(datetime.fromisoformat(now.date().isoformat()).replace(tzinfo=NY))
        r = self.db.one("""SELECT COUNT(*) AS n FROM trade_proposals WHERE source IN ('autopilot', 'model') AND side = 'buy' AND account_kind = ?
                           AND mode = ? AND created_at >= ? AND status NOT IN ('blocked', 'cancelled', 'expired', 'rejected', 'failed',
                           'awaiting_approval', 'draft')""", (pol.account_kind, self.desk.mode, start))
        return int(r["n"])

    def _entries(self, pol: Any, now: datetime, e: dict[str, Any]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        from ..v3.trading import ProposalIn
        dep = self.deployment()
        run_id, signals = self._today_signals(dep, now)
        entries, skipped = [], []
        if not signals:
            return entries, [{"why": "the gate took no setup after the last close"}]
        cfg = dep["manifest"]["config"]["trade"]
        book = self.desk.book(pol.account_kind)
        busy = {p["symbol"] for p in book["open_proposals"]} | set(book["positions"])
        for s in signals:
            if self.db.one("SELECT 1 FROM model_trade_links WHERE shadow_run = ? AND prediction_id = ? AND role = 'entry'", (run_id, s["id"])):
                continue                                   # already proposed (this job runs every five minutes)
            if s["symbol"] in busy:
                skipped.append({"symbol": s["symbol"], "why": "already held or proposed"})
                continue
            try:
                ask = float(self.svc.market.quote(s["symbol"]).get("ask") or 0)
            except (DataError, ValueError):
                skipped.append({"symbol": s["symbol"], "why": "no quote"})
                continue
            qty = self._size(ask, pol, book)
            if qty < 1:
                skipped.append({"symbol": s["symbol"], "why": f"one share at ${ask:,.2f} does not fit the order, capital and daily limits"})
                continue
            atr_abs = float(json.loads(s["payload_json"]).get("atr_pct") or 0) * float(s["last_close"])
            stop = round(ask - cfg["stop_atr"] * atr_abs, 2) if atr_abs > 0 else None
            target = round(ask + cfg["target_atr"] * atr_abs, 2) if atr_abs > 0 else None
            last_day = add_trading_days(datetime.fromisoformat(s["session"]).date(), int(cfg["hold_sessions"]))
            time_exit = _iso(datetime.combine(last_day, datetime.min.time()).replace(hour=15, minute=50, tzinfo=NY))
            thesis = (f"Model ({s['model_version']}): {s['setup']} setup after the {s['session']} close; target first "
                      f"{100 * (s['p_target'] or 0):.0f}%, stop first {100 * (s['p_stop'] or 0):.0f}%, expected "
                      f"{100 * (s['expected_return'] or 0):+.2f}% net. Frozen forecast {s['id']} in {run_id}.")
            try:
                p = self.desk.create(ProposalIn(symbol=s["symbol"], side="buy", quantity=qty, limit_price=round(ask, 2), thesis=thesis,
                                                source="model", exit_plan=C.ExitPlan(stop=stop if stop and stop > 0 else None,
                                                                                     target=target, time_exit=time_exit,
                                                                                     rules=["model", f"prediction:{s['id']}"])),
                                     actor="model")
            except RequestError as exc:
                skipped.append({"symbol": s["symbol"], "why": str(exc)})
                continue
            self._link(p, "entry", None, dep, run_id, s, stop, target, time_exit)
            busy.add(s["symbol"])
            got = {"symbol": s["symbol"], "proposal": p["id"], "status": p["status"], "quantity": qty}
            if p["status"] == "awaiting_approval" and e["authorizes"]:
                if self._authorized_today(pol, now) < pol.autopilot_max_orders_per_day:
                    a = self.desk._ask_approval(p["id"], notify=False)
                    got["status"] = self.desk.decide(a["id"], True, p["order_hash"], actor="model", channel="policy")["status"]
                else:
                    got["why"] = "the policy's daily order count is used up: it waits for your approval"
            entries.append(got)
        return entries, skipped

    def _link(self, p: dict[str, Any], role: str, entry: str | None, dep: dict[str, Any], run_id: str, s: dict[str, Any],
              stop: float | None, target: float | None, time_exit: str | None, reason: str = "") -> None:
        with self.db.tx() as cur:
            cur.execute("""INSERT INTO model_trade_links (proposal_id, mode, account_kind, role, entry_proposal, created_at, deployment_id, shadow_run,
                           prediction_id, model_version, symbol, setup, p_target, p_stop, expected_return, stop, target, time_exit, reason)
                           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                        (p["id"], self.desk.mode, p["account_kind"], role, entry, _iso(datetime.now(timezone.utc)), dep["id"], run_id, s["id"],
                         s["model_version"], s["symbol"], s.get("setup"), s.get("p_target"), s.get("p_stop"), s.get("expected_return"),
                         stop, target, time_exit, reason))

    # ---- exits ------------------------------------------------------------------------------------------------
    def open_trades(self, account_kind: str) -> list[dict[str, Any]]:
        """The models' entries that filled and whose shares are still held, with their exit plan."""
        held = self.desk.positions(account_kind, marks=False)
        qty = {p["symbol"]: p["quantity"] for p in held["positions"]}
        out = []
        for r in self.db.query("""SELECT l.*, p.status FROM model_trade_links l JOIN trade_proposals p ON p.id = l.proposal_id
                                  WHERE l.role = 'entry' AND l.account_kind = ? AND l.mode = ? ORDER BY l.created_at""", (account_kind, self.desk.mode)):
            if r["status"] not in ("filled", "partially_filled", "closed"):
                continue
            ex = self.db.one("""SELECT p.status FROM model_trade_links l JOIN trade_proposals p ON p.id = l.proposal_id
                                WHERE l.role = 'exit' AND l.entry_proposal = ? ORDER BY l.created_at DESC""", (r["proposal_id"],))
            if ex is not None and ex["status"] not in ("blocked", "cancelled", "expired", "rejected", "failed"):
                continue                                   # an exit is written (or done)
            if qty.get(r["symbol"], 0) <= 0:
                continue
            out.append({**dict(r), "quantity": qty[r["symbol"]]})
        return out

    def exit_plans(self, account_kind: str) -> dict[str, dict[str, Any]]:
        from ..autopilot import PROTECTION_WORDS
        return {t["symbol"]: {"managed_by": "model", "protection": "software", "stop": t["stop"], "target": t["target"],
                              "time_exit": t["time_exit"], "text": "The deployed models' trade: " + PROTECTION_WORDS}
                for t in self.open_trades(account_kind)}

    def _exits(self, pol: Any, now: datetime) -> list[dict[str, Any]]:
        from ..v3.trading import ProposalIn
        out = []
        dep = self.deployment()
        for t in self.open_trades(pol.account_kind):
            why = None
            try:
                q = self.svc.market.quote(t["symbol"])
                bid = float(q.get("bid") or q.get("last") or 0)
            except (DataError, ValueError):
                out.append({"symbol": t["symbol"], "status": "not checked", "why": "no quote"})
                continue
            if t["time_exit"] and now >= datetime.fromisoformat(t["time_exit"]):
                why = "the five-session time limit"
            elif t["stop"] and bid <= t["stop"]:
                why = f"the stop ({t['stop']:.2f})" + (" gapped through" if bid < t["stop"] * 0.995 else "")
            elif t["target"] and bid >= t["target"]:
                why = f"the target ({t['target']:.2f})"
            if not why:
                continue
            try:
                p = self.desk.create(ProposalIn(symbol=t["symbol"], side="sell", quantity=t["quantity"], limit_price=round(bid, 2) if bid > 0 else None,
                                                thesis=f"Model exit: {why}.", source="model"), actor="model")
            except RequestError as exc:
                out.append({"symbol": t["symbol"], "why": str(exc)})
                continue
            s = {"id": t["prediction_id"], "model_version": t["model_version"], "symbol": t["symbol"], "setup": t["setup"],
                 "p_target": t["p_target"], "p_stop": t["p_stop"], "expected_return": t["expected_return"]}
            self._link(p, "exit", t["proposal_id"], dep or {"id": t["deployment_id"]}, t["shadow_run"], s, t["stop"], t["target"], t["time_exit"], why)
            status = p["status"]
            if status == "awaiting_approval" and pol.mode == "capped_autopilot" and pol.account_kind in ALLOWED_ACCOUNTS:
                a = self.desk._ask_approval(p["id"], notify=False)
                status = self.desk.decide(a["id"], True, p["order_hash"], actor="model", channel="policy")["status"]
            out.append({"symbol": t["symbol"], "proposal": p["id"], "status": status, "why": why})
        return out

    # ---- the record -------------------------------------------------------------------------------------------
    def record(self) -> dict[str, Any]:
        """Each model trade: its fills, its result, and the simulated outcome of the same frozen forecast."""
        trades = []
        for e in self.db.query("SELECT * FROM model_trade_links WHERE role = 'entry' AND mode = ? ORDER BY created_at DESC LIMIT 500",
                               (self.desk.mode,)):
            entry = self._fill(e["proposal_id"])
            x = self.db.one("SELECT proposal_id, reason FROM model_trade_links WHERE role = 'exit' AND entry_proposal = ? ORDER BY created_at DESC",
                            (e["proposal_id"],))
            exit_fill = self._fill(x["proposal_id"]) if x else None
            ret = None
            if entry and exit_fill and entry["price"] > 0:
                ret = exit_fill["price"] / entry["price"] - 1.0 - (entry["fees"] + exit_fill["fees"]) / (entry["price"] * entry["quantity"])
            sim = None
            if has_replays(self.svc):
                o = replay_store(self.svc).one("SELECT barrier, net_return FROM outcomes WHERE prediction_id = ?", (e["prediction_id"],))
                sim = dict(o) if o else None
            trades.append({"symbol": e["symbol"], "entry_proposal": e["proposal_id"], "model_version": e["model_version"], "setup": e["setup"],
                           "expected_return": e["expected_return"], "entry": entry, "exit": exit_fill, "exit_reason": x["reason"] if x else None,
                           "return": ret, "simulated": sim, "created_at": e["created_at"]})
        done = [t for t in trades if t["return"] is not None]
        by_version: dict[str, dict[str, Any]] = {}
        for t in done:
            b = by_version.setdefault(t["model_version"], {"trades": 0, "wins": 0, "sum": 0.0})
            b["trades"] += 1
            b["wins"] += int(t["return"] > 0)
            b["sum"] += t["return"]
        for b in by_version.values():
            b["win_rate"] = b["wins"] / b["trades"]
            b["mean_return"] = b["sum"] / b["trades"]
        paired = [t for t in done if t["simulated"] and t["simulated"].get("net_return") is not None]
        return {"trades": trades, "closed": len(done), "open": sum(1 for t in trades if t["entry"] and not t["exit"]),
                "mean_return": sum(t["return"] for t in done) / len(done) if done else None,
                "by_model": by_version,
                "fill_vs_simulation": (sum(t["return"] - t["simulated"]["net_return"] for t in paired) / len(paired)) if paired else None,
                "abstained": self._abstained()}

    def _fill(self, pid: str) -> dict[str, Any] | None:
        r = self.db.one("""SELECT SUM(f.quantity) AS q, SUM(f.quantity * f.price) AS v, SUM(f.fees) AS fees, MAX(f.at) AS at
                           FROM broker_fills f JOIN broker_orders o ON o.id = f.order_id WHERE o.proposal_id = ?""", (pid,))
        if r is None or not r["q"]:
            return None
        return {"quantity": float(r["q"]), "price": float(r["v"]) / float(r["q"]), "fees": float(r["fees"] or 0), "at": r["at"]}

    def _abstained(self) -> dict[str, Any] | None:
        """What the setups the gate passed on, in the same forward record, turned out to be worth."""
        dep = self.deployment()
        if not dep or not dep.get("shadow_run"):
            return None
        r = replay_store(self.svc).one("""SELECT COUNT(*) AS n, AVG(o.net_return) AS mean FROM predictions p JOIN trade_decisions d ON d.prediction_id = p.id
                                          JOIN outcomes o ON o.prediction_id = p.id WHERE p.run_id = ? AND d.take = 0 AND o.status = 'graded'""",
                                       (dep["shadow_run"],))
        return {"passed_setups": int(r["n"]), "their_mean_net": r["mean"]}


def paper_for(svc: Any) -> ModelPaper:
    mp = getattr(svc, "model_paper", None)
    if mp is None:
        mp = svc.model_paper = ModelPaper(svc)
    return mp


__all__ = ["ModelPaper", "paper_for", "ALLOWED_ACCOUNTS", "ALLOWED_MODES"]
