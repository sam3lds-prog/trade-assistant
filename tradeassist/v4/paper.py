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
- **Entries** are written only inside the policy's window with the market open (a proposal written before would be
  blocked, and each forecast is proposed once).
- **Exits** are the trade's own: the stop and target in daily ranges from the entry and the five-session time limit
  (15 minutes before the window or an early close ends), checked every five minutes in market hours while the app or
  the daemon runs (software-monitored, never a stop resting at a broker, as for the autopilot). They sell only the
  models' own shares, one exit at a time (a blocked one is checked again, never duplicated), and they go on for
  trades already open after the switch is turned off or another run is deployed.

Every proposal is linked to the frozen forecast it came from (`model_trade_links`), so the record can say which
forecasts made money, how paper fills compared with the simulated outcome of the same forecast, and what passing
on them would have done.
"""

from __future__ import annotations

import json
import math
import sqlite3
from datetime import date, datetime, timedelta, timezone
from typing import Any

from .. import contracts as C
from .. import marketdays
from ..marketdays import add_trading_days, previous_trading_day
from ..optionmath import NY
from ..providers.base import DataError
from ..service import RequestError
from . import has_replays, replay_store

ALLOWED_ACCOUNTS = ("paper", "shadow")
ALLOWED_MODES = ("paper", "capped_autopilot")


EXIT_LEAD_MINUTES = 15       # the time exit is written this long before the window (or an early close) ends


def _iso(t: datetime) -> str:
    return t.astimezone(timezone.utc).isoformat(timespec="seconds")


def _tag(run_id: str, prediction_id: int) -> str:
    return f"prediction:{run_id}:{prediction_id}"


def trading_now(pol: Any, now: datetime) -> tuple[bool, str]:
    """Whether an order written now could pass the policy's window: the market open (holidays and early closes
    included) and the time inside the window. Returns (ok, why not)."""
    ny = now.astimezone(NY)
    if marketdays.phase(ny) != "open":
        return False, "the market is closed"
    if not pol.window(True) <= ny.time() < pol.window(False):
        return False, f"outside the policy's window ({pol.window_start} to {pol.window_end} New York)"
    return True, ""


def time_exit_on(day: date, pol: Any) -> datetime:
    """The time exit on the trade's last session: inside the policy's window and before an early close, with room for
    the sale to be written, checked and filled."""
    end = datetime.combine(day, pol.window(False), NY)
    s = marketdays.session(day)
    if s is not None:
        end = min(end, s[1])
    return end - timedelta(minutes=EXIT_LEAD_MINUTES)


def buy_limit(ask: float) -> float:
    """The buy limit at the ask, rounded up to a cent (a valid tick that still reaches the ask), or to a hundredth of
    a cent under a dollar."""
    if ask <= 0:
        return 0.0
    step = 100 if ask >= 1 else 10000
    return math.ceil(ask * step - 1e-9) / step


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
        pol = self.desk.policy()
        open_now, closed_why = trading_now(pol, now)
        # Trades already open keep their exits whatever the switch or the deployment now says: only a policy that is
        # not on a paper or shadow account, or the kill switch, stops them being managed.
        manage = pol.account_kind in ALLOWED_ACCOUNTS and pol.mode in ALLOWED_MODES and not self.desk.kill_switch().get("on")
        if manage and self.open_trades(pol.account_kind):
            out["exits"] = self._exits(pol, now) if open_now else [{"status": "waiting", "why": closed_why}]
        if not e["ok"]:
            out["skipped"].append({"why": "; ".join(c["text"] for c in e["checks"] if not c["ok"])})
            return self._done(out)
        if not open_now:                                   # an entry written now would only be blocked, and used up
            out["skipped"].append({"why": f"no entries yet: {closed_why}"})
            return self._done(out)
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
        """The policy's own buys today, the autopilot's and the models' together (autopilot.policy_buys_today)."""
        from ..autopilot import policy_buys_today
        start = _iso(datetime.combine(now.astimezone(NY).date(), datetime.min.time(), NY))
        return policy_buys_today(self.db, pol.account_kind, self.desk.mode, start)

    def _proposed(self, run_id: str, s: dict[str, Any]) -> str | None:
        """The proposal already written for this frozen forecast, if any: by its link, or (if the link was never
        written, say the app stopped in between) by the forecast's tag in the proposal's exit plan."""
        r = self.db.one("SELECT proposal_id FROM model_trade_links WHERE shadow_run = ? AND prediction_id = ? AND role = 'entry'", (run_id, s["id"]))
        if r is not None:
            return r["proposal_id"]
        r = self.db.one("SELECT id FROM trade_proposals WHERE source = 'model' AND side = 'buy' AND exit_plan_json LIKE ? ORDER BY created_at LIMIT 1",
                        (f'%"{_tag(run_id, s["id"])}"%',))
        return r["id"] if r is not None else None

    def _entries(self, pol: Any, now: datetime, e: dict[str, Any]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        from ..v3.trading import ProposalIn
        dep = self.deployment()
        run_id, signals = self._today_signals(dep, now)
        entries, skipped = [], []
        if not signals:
            return entries, [{"why": "the gate took no setup after the last close"}]
        cfg = dep["manifest"]["config"]["trade"]
        for s in signals:
            done = self._proposed(run_id, s)
            if done is not None:                           # one proposal per forecast (this job runs every five minutes)
                if self.db.one("SELECT 1 FROM model_trade_links WHERE proposal_id = ?", (done,)) is None:
                    self._link_existing(done, dep, run_id, s)
                continue
            book = self.desk.book(pol.account_kind)        # read again for each entry: the last one used capital
            busy = {p["symbol"] for p in book["open_proposals"]} | set(book["positions"])
            if s["symbol"] in busy:
                skipped.append({"symbol": s["symbol"], "why": "already held or proposed"})
                continue
            try:
                ask = float(self.svc.market.quote(s["symbol"]).get("ask") or 0)
            except (DataError, ValueError):
                skipped.append({"symbol": s["symbol"], "why": "no quote"})
                continue
            limit = buy_limit(ask)
            qty = self._size(limit, pol, book)
            if qty < 1:
                skipped.append({"symbol": s["symbol"], "why": f"one share at ${limit:,.2f} does not fit the order, capital and daily limits"})
                continue
            atr_abs = float(json.loads(s["payload_json"]).get("atr_pct") or 0) * float(s["last_close"])
            stop = round(limit - cfg["stop_atr"] * atr_abs, 2) if atr_abs > 0 else None
            target = round(limit + cfg["target_atr"] * atr_abs, 2) if atr_abs > 0 else None
            last_day = add_trading_days(datetime.fromisoformat(s["session"]).date(), int(cfg["hold_sessions"]))
            time_exit = _iso(time_exit_on(last_day, pol))
            thesis = (f"Model ({s['model_version']}): {s['setup']} setup after the {s['session']} close; target first "
                      f"{100 * (s['p_target'] or 0):.0f}%, stop first {100 * (s['p_stop'] or 0):.0f}%, expected "
                      f"{100 * (s['expected_return'] or 0):+.2f}% net. Frozen forecast {s['id']} in {run_id}.")
            try:
                p = self.desk.create(ProposalIn(symbol=s["symbol"], side="buy", quantity=qty, limit_price=limit, thesis=thesis,
                                                source="model", exit_plan=C.ExitPlan(stop=stop if stop and stop > 0 else None,
                                                                                     target=target, time_exit=time_exit,
                                                                                     rules=["model", _tag(run_id, s["id"])])),
                                     actor="model")
            except RequestError as exc:
                skipped.append({"symbol": s["symbol"], "why": str(exc)})
                continue
            try:
                self._link(p, "entry", None, dep, run_id, s, stop, target, time_exit)
            except sqlite3.IntegrityError:                 # another run of this job wrote it a moment ago
                self.desk.cancel(p["id"], actor="model", reason="a second proposal for the same forecast")
                continue
            got = {"symbol": s["symbol"], "proposal": p["id"], "status": p["status"], "quantity": qty}
            if p["status"] == "awaiting_approval":
                got.update(self._authorize(p, now))
            entries.append(got)
        return entries, skipped

    def _authorize(self, p: dict[str, Any], now: datetime, *, entry: bool = True) -> dict[str, Any]:
        """Approve a model proposal under the policy, if the policy in force at this moment says so (desk.decide checks it
        again inside its lock). Otherwise it waits for your approval."""
        pol = self.desk.policy()
        if not (pol.mode == "capped_autopilot" and pol.account_kind in ALLOWED_ACCOUNTS and p["account_kind"] in ALLOWED_ACCOUNTS):
            return {}
        if entry and self._authorized_today(pol, now) >= pol.autopilot_max_orders_per_day:
            return {"why": "the policy's daily order count is used up: it waits for your approval"}
        a = self.desk._ask_approval(p["id"], notify=False)
        try:
            return {"status": self.desk.decide(a["id"], True, p["order_hash"], actor="model", channel="policy")["status"]}
        except RequestError as exc:
            return {"why": str(exc)}

    def _link(self, p: dict[str, Any], role: str, entry: str | None, dep: dict[str, Any], run_id: str, s: dict[str, Any],
              stop: float | None, target: float | None, time_exit: str | None, reason: str = "") -> None:
        with self.db.tx() as cur:
            cur.execute("""INSERT INTO model_trade_links (proposal_id, mode, account_kind, role, entry_proposal, created_at, deployment_id, shadow_run,
                           prediction_id, model_version, symbol, setup, p_target, p_stop, expected_return, stop, target, time_exit, reason)
                           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                        (p["id"], self.desk.mode, p["account_kind"], role, entry, _iso(datetime.now(timezone.utc)), dep["id"], run_id, s["id"],
                         s["model_version"], s["symbol"], s.get("setup"), s.get("p_target"), s.get("p_stop"), s.get("expected_return"),
                         stop, target, time_exit, reason))

    def _link_existing(self, pid: str, dep: dict[str, Any], run_id: str, s: dict[str, Any]) -> None:
        p = self.desk.get(pid)
        plan = p.get("exit_plan") or {}
        try:
            self._link(p, "entry", None, dep, run_id, s, plan.get("stop"), plan.get("target"), plan.get("time_exit"), "linked afterwards")
        except sqlite3.IntegrityError:
            pass

    # ---- exits ------------------------------------------------------------------------------------------------
    def _stock_fills(self, account_kind: str) -> list[dict[str, Any]]:
        return [dict(r) for r in self.db.query(
            """SELECT p.id AS proposal_id, p.symbol, p.side, f.at, f.quantity FROM broker_fills f JOIN broker_orders o ON o.id = f.order_id
               JOIN trade_proposals p ON p.id = o.proposal_id WHERE p.account_kind = ? AND p.mode = ? AND p.instrument = 'equity'
               ORDER BY f.at, f.id""", (account_kind, self.desk.mode))]

    def open_trades(self, account_kind: str) -> list[dict[str, Any]]:
        """The models' entries that filled and that they still manage, each with the shares that are theirs to sell:
        what the entry bought less what its exits sold, never more than the account holds. An entry ends when it is
        sold out, or when the stock's position in the account went to nothing after it filled (you sold the shares):
        shares bought again later are never taken for the models'."""
        fills = self._stock_fills(account_kind)
        out = []
        for r in self.db.query("""SELECT l.*, p.status FROM model_trade_links l JOIN trade_proposals p ON p.id = l.proposal_id
                                  WHERE l.role = 'entry' AND l.account_kind = ? AND l.mode = ? ORDER BY l.created_at""", (account_kind, self.desk.mode)):
            mine = [f for f in fills if f["proposal_id"] == r["proposal_id"]]
            if not mine:
                continue                                   # not filled (yet)
            exits = [x["proposal_id"] for x in self.db.query(
                "SELECT proposal_id FROM model_trade_links WHERE role = 'exit' AND entry_proposal = ?", (r["proposal_id"],))]
            sold = sum(f["quantity"] for f in fills if f["proposal_id"] in exits)
            remaining = sum(f["quantity"] for f in mine) - sold
            first = min(f["at"] for f in mine)
            held, ended = 0.0, False
            for f in fills:
                if f["symbol"] != r["symbol"]:
                    continue
                held += f["quantity"] if f["side"] == "buy" else -f["quantity"]
                if f["at"] >= first and f["proposal_id"] != r["proposal_id"] and held <= 1e-9:
                    ended = True                           # the position was emptied after the entry filled
            if ended or remaining <= 1e-9 or held <= 1e-9:
                continue
            pending = self.db.one(f"""SELECT p.id, p.status FROM model_trade_links l JOIN trade_proposals p ON p.id = l.proposal_id
                                      WHERE l.role = 'exit' AND l.entry_proposal = ? AND p.status NOT IN ({",".join("?" * len(C.TERMINAL_STATES))})
                                      ORDER BY l.created_at DESC""", (r["proposal_id"], *sorted(C.TERMINAL_STATES)))
            out.append({**dict(r), "quantity": min(remaining, held), "pending_exit": dict(pending) if pending else None})
        return out

    def exit_plans(self, account_kind: str) -> dict[str, dict[str, Any]]:
        from ..autopilot import PROTECTION_WORDS
        return {t["symbol"]: {"managed_by": "model", "protection": "software", "stop": t["stop"], "target": t["target"],
                              "time_exit": t["time_exit"], "quantity": t["quantity"],
                              "text": "The deployed models' trade (their shares only), checked while the policy is on this account: "
                                      + PROTECTION_WORDS}
                for t in self.open_trades(account_kind)}

    def _exits(self, pol: Any, now: datetime) -> list[dict[str, Any]]:
        from ..v3.trading import ProposalIn
        out = []
        dep = self.deployment()
        marks = {p["symbol"]: p for p in self.desk.positions(pol.account_kind)["positions"]}
        for t in self.open_trades(pol.account_kind):
            pend = t["pending_exit"]
            if pend is not None:                           # one exit at a time: look at the one written, never add another
                if pend["status"] == "blocked":
                    try:
                        status = self.desk.recheck(pend["id"], actor="model")["status"]
                    except RequestError as exc:
                        out.append({"symbol": t["symbol"], "proposal": pend["id"], "why": str(exc)})
                        continue
                    if status == "awaiting_approval":
                        status = self._authorize(self.desk.get(pend["id"]), now, entry=False).get("status", status)
                    out.append({"symbol": t["symbol"], "proposal": pend["id"], "status": status, "why": "the exit was blocked: checked again"})
                continue
            pos = marks.get(t["symbol"]) or {}
            mark, age = pos.get("mark"), pos.get("mark_age_seconds")
            stale = mark is not None and age is not None and age > pol.max_quote_age_seconds
            timed_out = bool(t["time_exit"]) and now >= datetime.fromisoformat(t["time_exit"])
            if (mark is None or stale) and not timed_out:
                out.append({"symbol": t["symbol"], "status": "unchecked",
                            "why": ("no quote" if mark is None else f"the quote is {age:.0f} s old (the policy allows {pol.max_quote_age_seconds:g})")
                            + ": the stop and target wait for the next check"})
                continue
            why = None
            if mark is not None and not stale and t["stop"] and mark <= t["stop"]:
                why = f"the stop ({t['stop']:.2f})" + (" gapped through" if mark < t["stop"] * 0.995 else "")
            elif mark is not None and not stale and t["target"] and mark >= t["target"]:
                why = f"the target ({t['target']:.2f})"
            elif timed_out:
                why = "the five-session time limit"
            if not why:
                continue
            try:
                p = self.desk.create(ProposalIn(symbol=t["symbol"], side="sell", quantity=t["quantity"], thesis=f"Model exit: {why}.",
                                                source="model"), actor="model")
            except RequestError as exc:
                out.append({"symbol": t["symbol"], "why": str(exc)})
                continue
            s = {"id": t["prediction_id"], "model_version": t["model_version"], "symbol": t["symbol"], "setup": t["setup"],
                 "p_target": t["p_target"], "p_stop": t["p_stop"], "expected_return": t["expected_return"]}
            self._link(p, "exit", t["proposal_id"], dep or {"id": t["deployment_id"]}, t["shadow_run"], s, t["stop"], t["target"], t["time_exit"], why)
            status = p["status"]
            if status == "awaiting_approval":
                status = self._authorize(p, now, entry=False).get("status", status)
            out.append({"symbol": t["symbol"], "proposal": p["id"], "status": status, "why": why})
        return out

    # ---- the record -------------------------------------------------------------------------------------------
    def record(self) -> dict[str, Any]:
        """Each model trade: its fills, its result, and the simulated outcome of the same frozen forecast."""
        trades = []
        managed = {t["proposal_id"] for kind in ALLOWED_ACCOUNTS for t in self.open_trades(kind)}
        for e in self.db.query("SELECT * FROM model_trade_links WHERE role = 'entry' AND mode = ? ORDER BY created_at DESC LIMIT 500",
                               (self.desk.mode,)):
            entry = self._fill([e["proposal_id"]])
            xs = self.db.query("SELECT proposal_id, reason FROM model_trade_links WHERE role = 'exit' AND entry_proposal = ? ORDER BY created_at",
                               (e["proposal_id"],))
            exit_fill = self._fill([x["proposal_id"] for x in xs]) if xs else None
            reason = next((x["reason"] for x in reversed(xs) if self._fill([x["proposal_id"]])), None)
            ret = None
            done = bool(entry and exit_fill and exit_fill["quantity"] >= entry["quantity"] - 1e-9)
            if done and entry["price"] > 0:
                ret = exit_fill["price"] / entry["price"] - 1.0 - (entry["fees"] + exit_fill["fees"]) / (entry["price"] * entry["quantity"])
            state = ("closed" if done else "open" if e["proposal_id"] in managed
                     else "not filled" if not entry else "ended outside the models")
            sim = None
            if has_replays(self.svc):
                o = replay_store(self.svc).one("SELECT barrier, net_return FROM outcomes WHERE prediction_id = ?", (e["prediction_id"],))
                sim = dict(o) if o else None
            trades.append({"symbol": e["symbol"], "entry_proposal": e["proposal_id"], "model_version": e["model_version"], "setup": e["setup"],
                           "expected_return": e["expected_return"], "entry": entry, "exit": exit_fill, "exit_reason": reason if done else None,
                           "state": state, "return": ret, "simulated": sim, "created_at": e["created_at"]})
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
        return {"trades": trades, "closed": len(done), "open": sum(1 for t in trades if t["state"] == "open"),
                "mean_return": sum(t["return"] for t in done) / len(done) if done else None,
                "by_model": by_version,
                "fill_vs_simulation": (sum(t["return"] - t["simulated"]["net_return"] for t in paired) / len(paired)) if paired else None,
                "abstained": self._abstained()}

    def _fill(self, pids: list[str]) -> dict[str, Any] | None:
        if not pids:
            return None
        r = self.db.one(f"""SELECT SUM(f.quantity) AS q, SUM(f.quantity * f.price) AS v, SUM(f.fees) AS fees, MAX(f.at) AS at
                            FROM broker_fills f JOIN broker_orders o ON o.id = f.order_id WHERE o.proposal_id IN ({",".join("?" * len(pids))})""",
                        tuple(pids))
        if r is None or not r["q"]:
            return None
        return {"quantity": float(r["q"]), "price": float(r["v"]) / float(r["q"]), "fees": float(r["fees"] or 0), "at": r["at"]}

    def _abstained(self) -> dict[str, Any] | None:
        """What the setups the gate passed on turned out to be worth, on the same days as the trades: the sessions
        whose forecasts the models traded, in the same shadow runs."""
        if not has_replays(self.svc):
            return None
        days: dict[str, set[str]] = {}
        store = replay_store(self.svc)
        for link in self.db.query("SELECT DISTINCT shadow_run, prediction_id FROM model_trade_links WHERE role = 'entry' AND mode = ?",
                                  (self.desk.mode,)):
            r = store.one("SELECT session FROM predictions WHERE id = ? AND run_id = ?", (link["prediction_id"], link["shadow_run"]))
            if r is not None:
                days.setdefault(link["shadow_run"], set()).add(r["session"])
        n, total = 0, 0.0
        for run_id, sessions in days.items():
            marks = ",".join("?" * len(sessions))
            r = store.one(f"""SELECT COUNT(*) AS n, SUM(o.net_return) AS s FROM predictions p JOIN trade_decisions d ON d.prediction_id = p.id
                              JOIN outcomes o ON o.prediction_id = p.id WHERE p.run_id = ? AND p.session IN ({marks}) AND d.take = 0
                              AND o.status = 'graded'""", (run_id, *sorted(sessions)))
            n += int(r["n"])
            total += float(r["s"] or 0.0)
        return {"passed_setups": n, "their_mean_net": total / n if n else None,
                "sessions": sum(len(v) for v in days.values()), "basis": "the setups passed on the days the models traded"}


def paper_for(svc: Any) -> ModelPaper:
    mp = getattr(svc, "model_paper", None)
    if mp is None:
        mp = svc.model_paper = ModelPaper(svc)
    return mp


__all__ = ["ModelPaper", "paper_for", "ALLOWED_ACCOUNTS", "ALLOWED_MODES", "trading_now", "time_exit_on", "buy_limit"]
