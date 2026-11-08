"""The Trading Desk (3.0 phase 5): proposals, approvals, the risk engine, execution, reconciliation and the ledger.

A trade moves through one durable state machine (contracts.TRANSITIONS), and every move is a row in
`proposal_events`:

    created ─▶ awaiting_approval ─▶ authorized ─▶ submitting ─▶ submitted ─▶ filled
        └▶ blocked (a risk check failed or could not be measured; re-checked on request)
    submitting ─▶ unknown (the submission's outcome was not seen) ─▶ only reconciliation moves it on

**Approvals** are bound to the exact order: an approval records the hash of the order's terms (account, stock,
side, quantity, type, limit, time in force), and approving checks that hash against the proposal's current one.
Amending a proposal supersedes its approvals. An approval is used once and expires (the policy's
`approval_minutes`, never later than the proposal). Approving in the app needs the app (it listens on this computer
only); approving from a phone needs the link from a notification (a one-time token, stored hashed) and the PIN.

**Risk** is checked when the proposal is written, again when it is approved, and again just before it is sent;
any check that fails or cannot be measured blocks it (riskengine.py).

**Execution** is a job (`desk.submit`) run by the daemon, or by the app when no daemon runs. The broker order is
written with its client order id (the idempotency key) before it is sent. If the outcome is not seen (a timeout, a
crash), the order is `unknown` and is never sent again: reconciliation asks the broker what became of that client
order id. Phase 5 sends to the paper broker only.

**The ledger** (`budget_ledger`) reserves an approved buy's cost, releases it as it fills or when it is cancelled,
and records the realized result of each sale; the risk engine reads capital in use, today's new buying and today's
result from it and from the desk's fills.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import os
import secrets
import threading
from datetime import datetime, timedelta, timezone
from typing import Any

from fastapi import APIRouter
from pydantic import BaseModel, ConfigDict, Field

from .. import contracts as C
from .. import policy as P
from ..brokers import BUYS, BrokerError, BrokerTimeout, PaperBroker
from ..bus import bus_for
from ..freshness import session_state
from ..jobs import Jobs, Schedule, Worker, alive, last_heartbeat
from ..notify import Notifier
from ..optionmath import NY
from ..providers.base import DataError
from ..riskengine import OPENING_SIDES, assess
from ..service import RequestError

log = logging.getLogger(__name__)

PRE_SUBMIT = ("draft", "awaiting_approval", "blocked", "authorized")
NOT_FOUND_MISSES = 3          # an unknown order is called never-received only after this many empty look-ups...
NOT_FOUND_SECONDS = 900       # ...spread over at least this long, unless the broker's answer is conclusive (the paper broker)
WORKING = ("submitted", "partially_filled", "cancel_requested", "unknown", "submitting")
SIDE_WORDS = {"buy": "Buy", "sell": "Sell", "sell_short": "Sell short", "buy_to_cover": "Buy to cover", "buy_to_open": "Buy to open",
              "sell_to_close": "Sell to close", "sell_to_open": "Sell to open", "buy_to_close": "Buy to close"}
ORDER_TO_PROPOSAL = {"submitted": "submitted", "accepted": "submitted", "partially_filled": "partially_filled", "filled": "filled",
                     "cancelled": "cancelled", "expired": "expired", "rejected": "rejected", "failed": "failed",
                     "cancel_requested": "cancel_requested", "unknown": "unknown"}


class ProposalIn(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)
    symbol: str = Field(min_length=1, max_length=12)
    side: C.Side = "buy"
    quantity: float = Field(gt=0, le=1_000_000)
    limit_price: float | None = Field(default=None, gt=0, le=1_000_000)
    instrument: C.Instrument = "equity"
    option: C.OptionTerms | None = None
    time_in_force: C.TimeInForce = "day"
    thesis: str = Field(default="", max_length=2000)
    exit_plan: C.ExitPlan = Field(default_factory=C.ExitPlan)
    study_id: str | None = Field(default=None, max_length=64)
    opportunity_id: str | None = Field(default=None, max_length=64)
    source: str = "manual"


class AmendIn(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)
    quantity: float | None = Field(default=None, gt=0, le=1_000_000)
    limit_price: float | None = Field(default=None, gt=0, le=1_000_000)


class DecisionIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    order_hash: str = Field(min_length=16, max_length=128)


class KillIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    on: bool
    reason: str = Field(default="", max_length=300)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(t: datetime) -> str:
    return t.astimezone(timezone.utc).isoformat(timespec="seconds")


def token_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def link_key(settings: Any) -> bytes:
    """The key approval links are made from, kept beside the database (readable by you only) and never in it, so a
    copy of the database or a backup holds no usable link."""
    path = settings.db_path.parent / "approval_links.key"
    try:
        data = path.read_bytes()
        if len(data) >= 32:
            return data
    except FileNotFoundError:
        pass
    path.parent.mkdir(parents=True, exist_ok=True)
    data = secrets.token_bytes(32)
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:                     # the other process made it a moment ago
        return path.read_bytes()
    try:
        os.write(fd, data)
    finally:
        os.close(fd)
    return data


def approval_token(settings: Any, approval_id: str) -> str:
    return base64.urlsafe_b64encode(hmac.new(link_key(settings), approval_id.encode(), hashlib.sha256).digest()).decode().rstrip("=")


def describe(terms: dict[str, Any]) -> str:
    what = terms["option"]["option_symbol"] if terms.get("option") else terms["symbol"]
    qty = terms["quantity"]
    unit = "contract" if terms.get("option") else "share"
    limit = f" at ${terms['limit_price']:,.2f} limit" if terms.get("limit_price") else " at the market"
    return f"{SIDE_WORDS.get(terms['side'], terms['side'])} {qty:g} {unit}{'' if qty == 1 else 's'} of {what}{limit}"


class TradingDesk:
    def __init__(self, svc: Any, notifier: Notifier | None = None):
        self.svc = svc
        self.db = svc.db
        self.jobs = Jobs(svc.db)
        self.notifier = notifier or Notifier(svc)
        self.brokers: dict[str, Any] = {"paper": PaperBroker(svc)}
        self.live_ready = False                 # phase 6: true only after the Robinhood adapter's checks pass
        self.live_account_ref: str | None = None
        self.live_reasons: list[str] = ["live execution is off (ROBINHOOD_EXECUTION is not set)"]
        from ..robinhood_exec import account_ref, make_execution
        live = make_execution(svc.settings)
        if live is not None:
            self.brokers["live"] = live
            self.live_account_ref = account_ref(live.account)
            self.live_reasons = ["not checked yet: press Check live trading, or start the daemon"]
        self._lock = threading.RLock()
        self.worker: Worker | None = None
        self.worker_mode = svc.settings.desk_worker

    @property
    def mode(self) -> str:
        return self.svc.market.info.mode

    @property
    def bus(self) -> Any:
        return bus_for(self.svc)

    # --- the policy and the kill switch -------------------------------------------------------------------
    def policy(self) -> P.TradingPolicy:
        return P.current(self.db)

    def kill_switch(self) -> dict[str, Any]:
        r = self.db.one("SELECT value_json, updated_at FROM execution_state WHERE key = 'kill_switch'")
        return json.loads(r["value_json"]) if r else {"on": False}

    def set_kill_switch(self, on: bool, reason: str = "", actor: str = "you") -> dict[str, Any]:
        value = {"on": bool(on), "at": _iso(_now()), "by": actor, "reason": reason.strip()[:300]}
        with self.db.tx() as cur:
            cur.execute("INSERT INTO execution_state (key, value_json, updated_at) VALUES ('kill_switch', ?, ?) "
                        "ON CONFLICT(key) DO UPDATE SET value_json = excluded.value_json, updated_at = excluded.updated_at",
                        (json.dumps(value), value["at"]))
            cur.execute("INSERT INTO audit_log (at, actor, action, subject, data_json) VALUES (?, ?, ?, 'kill_switch', ?)",
                        (value["at"], actor, "kill_switch.on" if on else "kill_switch.off", json.dumps(value)))
        self.bus.publish("desk.kill_switch", None, on=bool(on), reason=value["reason"])
        if on:
            self.notifier.enqueue("desk.kill_switch", "Kill switch on: nothing will be sent",
                                  f"The Trading Desk's kill switch was switched on by {actor}"
                                  + (f": {value['reason']}" if value["reason"] else ".")
                                  + " Working orders are being cancelled; approvals are refused until it is switched off.",
                                  severity="critical")
            open_states = sorted(C.OPEN_ORDER_STATES - {"intent"})
            for o in self.db.query(f"SELECT DISTINCT proposal_id FROM broker_orders WHERE state IN ({','.join('?' * len(open_states))})", open_states):
                self.jobs.enqueue("desk.cancel", {"proposal_id": o["proposal_id"], "reason": "the kill switch"},
                                  dedupe_key=f"cancel:{o['proposal_id']}:kill:{value['at']}", max_attempts=6)
            self.ensure_worker()
        return value

    # --- the book: what the desk holds, has reserved and has done today ----------------------------------------
    def _fills(self, account_kind: str) -> list[dict[str, Any]]:
        rows = self.db.query("""SELECT f.at, f.quantity, f.price, f.fees, f.external_id, o.terms_json, o.proposal_id, o.id AS order_id
                                FROM broker_fills f JOIN broker_orders o ON o.id = f.order_id
                                WHERE o.mode = ? AND o.account_kind = ? ORDER BY f.at, f.id""", (self.mode, account_kind))
        return [{**dict(r), "terms": json.loads(r["terms_json"])} for r in rows]

    def positions(self, account_kind: str | None = None, *, marks: bool = True) -> dict[str, Any]:
        account_kind = account_kind or self.policy().account_kind
        today = datetime.now(NY).date().isoformat()
        book: dict[str, dict[str, Any]] = {}
        for f in self._fills(account_kind):
            t = f["terms"]
            key = t["option"]["option_symbol"] if t.get("option") else t["symbol"]
            mult = float(t["option"]["multiplier"]) if t.get("option") else 1.0
            p = book.setdefault(key, {"symbol": t["symbol"], "key": key, "instrument": t["instrument"], "multiplier": mult,
                                      "quantity": 0.0, "avg_cost": 0.0, "realized": 0.0, "opened_on": None, "fills": 0})
            q = float(f["quantity"])
            p["fills"] += 1
            if t["side"] in BUYS:
                total = p["quantity"] + q
                p["avg_cost"] = (p["avg_cost"] * p["quantity"] + float(f["price"]) * q) / total if total else 0.0
                if p["quantity"] <= 0:
                    p["opened_on"] = str(f["at"])[:10]
                p["quantity"] = total
            else:
                p["realized"] += (float(f["price"]) - p["avg_cost"]) * q * mult - float(f["fees"] or 0)
                p["quantity"] = max(0.0, p["quantity"] - q)
        rows = []
        day_pnl: float | None = 0.0
        unmeasured = []
        for p in book.values():
            if p["quantity"] <= 1e-9:
                continue
            p["cost"] = p["avg_cost"] * p["quantity"] * p["multiplier"]
            p["mark"] = p["value"] = p["unrealized"] = p["today"] = None
            if marks:
                try:
                    q = self.svc.market.quote(p["symbol"])
                    bid, ask, last = q.get("bid"), q.get("ask"), q.get("last")
                    mark = (bid + ask) / 2 if bid and ask else last
                    if not mark:
                        raise DataError("no price")
                    p["mark"] = float(mark)
                    p["mark_age_seconds"] = ((q.get("_meta") or {}).get("age_seconds"))
                    p["value"] = p["mark"] * p["quantity"] * p["multiplier"]
                    p["unrealized"] = (p["mark"] - p["avg_cost"]) * p["quantity"] * p["multiplier"]
                    ref = q.get("prevclose") if p["opened_on"] and p["opened_on"] < today and q.get("prevclose") else p["avg_cost"]
                    p["today"] = (p["mark"] - float(ref)) * p["quantity"] * p["multiplier"]
                except (DataError, ValueError, TypeError):
                    unmeasured.append(p["key"])
            rows.append(p)
            if day_pnl is not None and marks:
                day_pnl = None if p["today"] is None else day_pnl + p["today"]
        realized_today = self._ledger_sum("realized", since_day=today, account_kind=account_kind)
        if day_pnl is not None:
            day_pnl += realized_today
        return {"account_kind": account_kind, "positions": sorted(rows, key=lambda r: r["key"]), "realized_today": realized_today,
                "day_pnl": day_pnl if marks else None, "unmeasured": unmeasured}

    def _ledger_sum(self, kind: str, *, since_day: str | None = None, account_kind: str, note_like: str | None = None,
                    proposal_id: str | None = None) -> float:
        sql = "SELECT COALESCE(SUM(amount), 0) AS s FROM budget_ledger WHERE mode = ? AND account_kind = ? AND kind = ?"
        params: list[Any] = [self.mode, account_kind, kind]
        if since_day:
            start = datetime.fromisoformat(since_day).replace(tzinfo=NY)
            sql += " AND at >= ?"
            params.append(_iso(start))
        if note_like:
            sql += " AND note LIKE ?"
            params.append(note_like)
        if proposal_id:
            sql += " AND proposal_id = ?"
            params.append(proposal_id)
        r = self.db.one(sql, params)
        return float(r["s"] or 0.0) if r else 0.0

    def book(self, account_kind: str | None = None) -> dict[str, Any]:
        pol = self.policy()
        account_kind = account_kind or pol.account_kind
        pos = self.positions(account_kind)
        today = datetime.now(NY).date().isoformat()
        reserved = self._ledger_sum("reserve", account_kind=account_kind) - self._ledger_sum("release", account_kind=account_kind)
        cost = sum(p["cost"] for p in pos["positions"])
        start = _iso(datetime.fromisoformat(today).replace(tzinfo=NY))
        r = self.db.one("""SELECT COALESCE(SUM(CASE WHEN kind = 'reserve' THEN amount ELSE -amount END), 0) AS s FROM budget_ledger
                           WHERE mode = ? AND account_kind = ? AND at >= ? AND (kind = 'reserve' OR (kind = 'release' AND note LIKE 'unfilled%'
                           AND proposal_id IN (SELECT proposal_id FROM budget_ledger WHERE kind = 'reserve' AND mode = ? AND account_kind = ? AND at >= ?)))""",
                        (self.mode, account_kind, start, self.mode, account_kind, start))
        today_buys = float(r["s"] or 0.0)
        open_rows = self.db.query(f"""SELECT id, symbol, side, status FROM trade_proposals WHERE mode = ? AND account_kind = ?
                                      AND status NOT IN ({','.join('?' * len(C.TERMINAL_STATES))})""",
                                  (self.mode, account_kind, *sorted(C.TERMINAL_STATES)))
        held = {p["symbol"] for p in pos["positions"]}
        committed = ("authorized", "submitting", "submitted", "partially_filled", "unknown")
        return {"positions": {p["symbol"]: p for p in pos["positions"]}, "position_rows": pos["positions"],
                "reserved": max(0.0, reserved), "cost": cost, "capital_in_use": max(0.0, reserved) + cost,
                "today_new_buys": max(0.0, today_buys), "day_pnl": pos["day_pnl"], "realized_today": pos["realized_today"],
                "unmeasured": pos["unmeasured"],
                "open_proposals": [{"id": r["id"], "symbol": r["symbol"], "side": r["side"], "status": r["status"]} for r in open_rows],
                "new_symbol_buys": sorted({r["symbol"] for r in open_rows if r["side"] in OPENING_SIDES and r["status"] in committed
                                           and r["symbol"] not in held}),
                "kill_switch": self.kill_switch()}

    def check_live(self, max_age: float = 600.0) -> dict[str, Any]:
        """Whether live orders can go to Robinhood now: the adapter's own checks, plus real prices."""
        live = self.brokers.get("live")
        if live is None:
            self.live_ready = False
            return {"enabled": False, "ready": False, "reasons": self.live_reasons, "account": None, "tools": {}}
        r = live.readiness(max_age)
        reasons = list(r["reasons"])
        if self.mode == "demo":
            reasons.append("the app is on made-up demo prices; live orders need real ones")
        self.live_ready = not reasons
        self.live_reasons = reasons
        return {"enabled": True, "ready": self.live_ready, "reasons": reasons, "account": r["account"], "tools": r["tools"],
                "ceiling": self.svc.settings.live_max_order}

    def live_status(self) -> dict[str, Any]:
        return {"enabled": "live" in self.brokers, "ready": self.live_ready, "reasons": self.live_reasons, "account": self.live_account_ref,
                "ceiling": self.svc.settings.live_max_order}

    def assess(self, terms: C.OrderTerms, proposal_id: str | None = None) -> C.RiskAssessment:
        pol = self.policy()
        try:
            quote = self.svc.market.quote(terms.symbol)
        except (DataError, ValueError):
            quote = None
        try:
            market_state = session_state(self.svc.market.clock(), _now())
        except Exception:  # noqa: BLE001 - an unreadable clock is an unknown, which blocks
            market_state = "UNKNOWN"
        book = self.book(terms.account_kind)
        if proposal_id:
            # Its own reservation (made when it was approved) is already in the book: do not count it twice.
            own = (self._ledger_sum("reserve", account_kind=terms.account_kind, proposal_id=proposal_id)
                   - self._ledger_sum("release", account_kind=terms.account_kind, proposal_id=proposal_id))
            if own > 0:
                book["capital_in_use"] = max(0.0, book["capital_in_use"] - own)
                book["today_new_buys"] = max(0.0, book["today_new_buys"] - own)
        book["live_account_ref"] = self.live_account_ref
        book["live_max_order"] = self.svc.settings.live_max_order
        return assess(terms, pol, book, quote, now=datetime.now(NY), market_state=market_state,
                      data_mode=self.mode, live_ready=self.live_ready, proposal_id=proposal_id)

    # --- proposals ------------------------------------------------------------------------------------------
    def _terms(self, req: ProposalIn, pol: P.TradingPolicy) -> C.OrderTerms:
        sym = self.svc._symbol(req.symbol)
        limit = req.limit_price
        if limit is None:
            try:
                q = self.svc.market.quote(sym)
            except DataError as exc:
                raise RequestError(f"No quote for {sym} to set the limit from: {exc}. Enter a limit price.") from exc
            limit = q.get("ask") if req.side in BUYS else q.get("bid")
            if not limit:
                raise RequestError(f"{sym} has no {'ask' if req.side in BUYS else 'bid'} to set the limit from. Enter a limit price.")
        account_ref = self.live_account_ref if pol.account_kind == "live" else pol.account_kind
        try:
            return C.OrderTerms(account_kind=pol.account_kind, account_ref=account_ref, symbol=sym, instrument=req.instrument, side=req.side,
                                quantity=req.quantity, order_type="limit", limit_price=round(float(limit), 4),
                                time_in_force=req.time_in_force, option=req.option)
        except ValueError as exc:
            raise RequestError(str(exc).split("\n")[-1][:300]) from exc

    def preview(self, req: ProposalIn) -> dict[str, Any]:
        terms = self._terms(req, self.policy())
        risk = self.assess(terms)
        return {"terms": terms.model_dump(), "order_hash": terms.order_hash(), "describe": describe(terms.model_dump()),
                "risk": risk.model_dump()}

    def _event(self, cur: Any, pid: str, frm: str | None, to: str, actor: str, reason: str = "", data: Any = None) -> None:
        cur.execute("INSERT INTO proposal_events (proposal_id, at, from_status, to_status, actor, reason, data_json) VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (pid, _iso(_now()), frm, to, actor, reason[:500], json.dumps(data, default=str) if data is not None else None))

    def _move(self, cur: Any, pid: str, to: str, actor: str, reason: str = "", data: Any = None, **cols: Any) -> str:
        row = cur.execute("SELECT status FROM trade_proposals WHERE id = ?", (pid,)).fetchone()
        frm = row["status"]
        if frm == to and not cols:
            return frm
        if frm != to and not C.can_move(frm, to):
            raise RequestError(f"A proposal that is {frm.replace('_', ' ')} cannot become {to.replace('_', ' ')}.")
        sets = ", ".join([f"{k} = ?" for k in cols] + ["status = ?", "updated_at = ?"])
        cur.execute(f"UPDATE trade_proposals SET {sets} WHERE id = ?", (*cols.values(), to, _iso(_now()), pid))
        if frm != to:
            self._event(cur, pid, frm, to, actor, reason, data)
        return frm

    def create(self, req: ProposalIn, actor: str = "you") -> dict[str, Any]:
        pol = self.policy()
        if req.source not in ("research", "discover", "autopilot", "manual", "blend"):
            raise RequestError("source must be research, discover, autopilot, manual or blend")
        terms = self._terms(req, pol)
        pid = C.new_id("prop")
        risk = self.assess(terms, proposal_id=pid)
        status = "awaiting_approval" if risk.ok else "blocked"
        now = _now()
        expires = now + timedelta(minutes=pol.proposal_minutes)
        evidence = C.digest({"study": req.study_id, "opportunity": req.opportunity_id, "thesis": req.thesis})
        with self._lock, self.db.tx() as cur:
            cur.execute("""INSERT INTO trade_proposals (id, mode, account_kind, account_ref, created_at, updated_at, source, status, symbol, instrument,
                           side, quantity, order_type, limit_price, terms_json, order_hash, study_id, opportunity_id, thesis, evidence_hash,
                           exit_plan_json, risk_json, policy_id, expires_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                        (pid, self.mode, terms.account_kind, terms.account_ref, _iso(now), _iso(now), req.source, status, terms.symbol,
                         terms.instrument, terms.side, terms.quantity, terms.order_type, terms.limit_price, terms.model_dump_json(),
                         terms.order_hash(), req.study_id, req.opportunity_id, req.thesis.strip(), evidence, req.exit_plan.model_dump_json(),
                         risk.model_dump_json(), P.active(self.db)["id"], _iso(expires)))
            self._event(cur, pid, None, status, actor, "written" if risk.ok else risk.text, {"order_hash": terms.order_hash()})
        self.bus.publish("proposal.created", pid, symbol=terms.symbol, status=status)
        if risk.ok:
            self._ask_approval(pid)
        else:
            self.notifier.enqueue("proposal.blocked", f"Blocked: {describe(terms.model_dump())}", risk.text, severity="warn", proposal_id=pid,
                                  dedupe_key=f"blocked:{pid}:1")
        return self.get(pid)

    def _proposal(self, pid: str) -> dict[str, Any]:
        r = self.db.one("SELECT * FROM trade_proposals WHERE id = ?", (pid,))
        if r is None:
            raise RequestError(f"No proposal {pid}")
        p = dict(r)
        p["terms"] = json.loads(p.pop("terms_json"))
        p["exit_plan"] = json.loads(p.pop("exit_plan_json") or "{}")
        p["risk"] = json.loads(p.pop("risk_json")) if p.get("risk_json") else None
        p["authorization"] = json.loads(p.pop("authorization_json")) if p.get("authorization_json") else None
        return p

    def get(self, pid: str) -> dict[str, Any]:
        p = self._proposal(pid)
        p["describe"] = describe(p["terms"])
        p["events"] = [dict(e) for e in self.db.query("SELECT at, from_status, to_status, actor, reason FROM proposal_events WHERE proposal_id = ? ORDER BY id", (pid,))]
        p["approvals"] = [dict(a) for a in self.db.query("""SELECT id, created_at, expires_at, order_hash, decision, decided_at, decided_by, channel
                                                            FROM approvals WHERE proposal_id = ? ORDER BY created_at""", (pid,))]
        p["pending_approval"] = next((a for a in p["approvals"] if a["decision"] == "pending" and a["expires_at"] > _iso(_now())), None)
        orders = []
        for o in self.db.query("SELECT * FROM broker_orders WHERE proposal_id = ? ORDER BY created_at", (pid,)):
            d = {k: o[k] for k in ("id", "broker", "client_order_id", "broker_order_id", "state", "created_at", "updated_at", "filled_quantity",
                                   "average_fill_price", "last_reconciled_at", "error")}
            d["fills"] = [dict(f) for f in self.db.query("SELECT at, quantity, price, fees FROM broker_fills WHERE order_id = ? ORDER BY id", (o["id"],))]
            orders.append(d)
        p["orders"] = orders
        p["reserved"] = (self._ledger_sum("reserve", account_kind=p["account_kind"], proposal_id=pid)
                         - self._ledger_sum("release", account_kind=p["account_kind"], proposal_id=pid))
        return p

    def listing(self, status: str | None = None, limit: int = 50) -> list[dict[str, Any]]:
        sql = "SELECT id FROM trade_proposals WHERE mode = ?"
        params: list[Any] = [self.mode]
        if status == "open":
            sql += f" AND status NOT IN ({','.join('?' * len(C.TERMINAL_STATES))})"
            params += sorted(C.TERMINAL_STATES)
        elif status:
            sql += " AND status = ?"
            params.append(status)
        rows = self.db.query(sql + " ORDER BY updated_at DESC LIMIT ?", (*params, limit))
        out = []
        for r in rows:
            p = self._proposal(r["id"])
            p["describe"] = describe(p["terms"])
            pa = self.db.one("SELECT id, expires_at FROM approvals WHERE proposal_id = ? AND decision = 'pending' AND expires_at > ? ORDER BY created_at DESC",
                             (r["id"], _iso(_now())))
            p["pending_approval"] = dict(pa) if pa else None
            out.append(p)
        return out

    # --- approvals -------------------------------------------------------------------------------------------
    def _ask_approval(self, pid: str, notify: bool = True) -> dict[str, Any]:
        """A new single-use approval for the proposal's current terms; any earlier one is superseded."""
        p = self._proposal(pid)
        pol = self.policy()
        now = _now()
        expires = min(now + timedelta(minutes=pol.approval_minutes), datetime.fromisoformat(p["expires_at"]))
        aid = C.new_id("appr")
        token = approval_token(self.svc.settings, aid)
        with self.db.tx() as cur:
            cur.execute("UPDATE approvals SET decision = 'superseded', decided_at = ? WHERE proposal_id = ? AND decision = 'pending'", (_iso(now), pid))
            cur.execute("INSERT INTO approvals (id, proposal_id, created_at, expires_at, token_hash, order_hash, decision) VALUES (?, ?, ?, ?, ?, ?, 'pending')",
                        (aid, pid, _iso(now), _iso(expires), token_hash(token), p["order_hash"]))
        s = self.svc.settings
        link = f"{s.remote_base_url}/remote/a/{aid}" if s.remote_approvals and s.remote_base_url else None   # the token is added when sent
        if notify:
            risk = p.get("risk") or {}
            ny = expires.astimezone(NY)
            self.notifier.enqueue("proposal.approval", f"Approve? {describe(p['terms'])}",
                                  f"{describe(p['terms'])} in the {p['account_kind']} account.\n"
                                  f"Cost ${C.OrderTerms(**p['terms']).notional():,.2f}; {risk.get('text', '')}\n"
                                  + (f"Why: {p['thesis']}\n" if p.get("thesis") else "")
                                  + f"Approve by {ny:%H:%M} New York"
                                  + (" from the link (it asks for your PIN), or on the Trading Desk." if link else " on the Trading Desk."),
                                  link=link, proposal_id=pid, dedupe_key=f"approval:{aid}")
        self.bus.publish("proposal.awaiting_approval", pid, approval_id=aid, expires_at=_iso(expires))
        self.ensure_worker()
        return {"id": aid, "expires_at": _iso(expires), "token": token, "link": f"{link}?t={token}" if link else None}

    def decide(self, approval_id: str, approve: bool, order_hash: str, *, actor: str = "you", channel: str = "app",
               token: str | None = None) -> dict[str, Any]:
        with self._lock:
            a = self.db.one("SELECT * FROM approvals WHERE id = ?", (approval_id,))
            if a is None:
                raise RequestError("No such approval.")
            if channel == "remote" and not (token and hmac.compare_digest(token_hash(token), a["token_hash"])):
                raise RequestError("This approval link is not valid.")
            if a["decision"] != "pending" or a["used_at"]:
                raise RequestError(f"This approval was already {a['decision']}. Ask for a new one on the Trading Desk.")
            if a["expires_at"] <= _iso(_now()):
                with self.db.tx() as cur:
                    cur.execute("UPDATE approvals SET decision = 'expired', decided_at = ? WHERE id = ?", (_iso(_now()), approval_id))
                raise RequestError("This approval expired. Ask for a new one on the Trading Desk.")
            p = self._proposal(a["proposal_id"])
            if not (hmac.compare_digest(order_hash, a["order_hash"]) and hmac.compare_digest(order_hash, p["order_hash"])):
                raise RequestError("The order changed after this approval was asked for. Look at the new terms and approve those.")
            if p["status"] != "awaiting_approval":
                raise RequestError(f"The proposal is {p['status'].replace('_', ' ')}, not awaiting approval.")
            if self.kill_switch().get("on") and approve:
                raise RequestError("The kill switch is on: nothing can be approved until it is switched off.")
            if actor == "autopilot" and (channel != "policy" or self.policy().mode != "capped_autopilot"):
                raise RequestError("The autopilot approves only under a capped_autopilot policy.")
            now = _iso(_now())
            with self.db.tx() as cur:
                cur.execute("UPDATE approvals SET decision = ?, decided_at = ?, decided_by = ?, channel = ?, used_at = ? WHERE id = ? AND decision = 'pending'",
                            ("approved" if approve else "declined", now, actor, channel, now, approval_id))
                if not cur.rowcount:
                    raise RequestError("This approval was used a moment ago.")
                if not approve:
                    self._move(cur, p["id"], "cancelled", actor, f"declined ({channel})")
            if not approve:
                self.bus.publish("proposal.declined", p["id"])
                return self.get(p["id"])
            terms = C.OrderTerms(**p["terms"])
            risk = self.assess(terms, proposal_id=p["id"])
            with self.db.tx() as cur:
                if not risk.ok:
                    self._move(cur, p["id"], "blocked", actor, risk.text, risk_json=risk.model_dump_json())
                else:
                    auth = {"approval_id": approval_id, "by": actor, "channel": channel, "at": now, "order_hash": p["order_hash"]}
                    self._move(cur, p["id"], "authorized", actor, f"approved ({channel})", authorization_json=json.dumps(auth),
                               risk_json=risk.model_dump_json())
                    if terms.side in OPENING_SIDES:
                        self._ledger(cur, "reserve", terms.notional(), p, note=f"approved: {describe(p['terms'])}")
        self.bus.publish("proposal.authorized" if risk.ok else "proposal.blocked", p["id"])
        if risk.ok:
            self.jobs.enqueue("desk.submit", {"proposal_id": p["id"]}, dedupe_key=f"submit:{p['id']}:{approval_id}")
            self.ensure_worker()
        else:
            self.notifier.enqueue("proposal.blocked", f"Blocked at approval: {describe(p['terms'])}", risk.text, severity="warn",
                                  proposal_id=p["id"], dedupe_key=f"blocked:{p['id']}:{approval_id}")
        return self.get(p["id"])

    def _ledger(self, cur: Any, kind: str, amount: float, p: dict[str, Any], *, note: str = "", order_id: str | None = None) -> None:
        cur.execute("INSERT INTO budget_ledger (at, mode, account_kind, policy_id, proposal_id, order_id, kind, amount, note) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (_iso(_now()), p["mode"], p["account_kind"], p.get("policy_id"), p["id"], order_id, kind, round(float(amount), 6), note[:300]))

    @staticmethod
    def _reserved_rest(cur: Any, pid: str) -> float:
        r = cur.execute("""SELECT COALESCE(SUM(CASE WHEN kind = 'reserve' THEN amount WHEN kind = 'release' THEN -amount ELSE 0 END), 0) AS r
                           FROM budget_ledger WHERE proposal_id = ?""", (pid,)).fetchone()
        return max(0.0, float(r["r"] or 0.0))

    def _release_rest(self, cur: Any, p: dict[str, Any], why: str) -> None:
        rest = self._reserved_rest(cur, p["id"])
        if rest > 1e-9:
            self._ledger(cur, "release", rest, p, note=f"unfilled: {why}")

    # --- changing a proposal ------------------------------------------------------------------------------------
    def amend(self, pid: str, req: AmendIn, actor: str = "you") -> dict[str, Any]:
        with self._lock:
            p = self._proposal(pid)
            if p["status"] not in ("draft", "awaiting_approval", "blocked"):
                raise RequestError(f"A proposal that is {p['status'].replace('_', ' ')} cannot be changed.")
            terms = C.OrderTerms(**{**p["terms"], **{k: v for k, v in req.model_dump().items() if v is not None}})
            risk = self.assess(terms, proposal_id=pid)
            status = "awaiting_approval" if risk.ok else "blocked"
            with self.db.tx() as cur:
                cur.execute("UPDATE approvals SET decision = 'superseded', decided_at = ? WHERE proposal_id = ? AND decision = 'pending'", (_iso(_now()), pid))
                cur.execute("UPDATE trade_proposals SET quantity = ?, limit_price = ?, terms_json = ?, order_hash = ?, risk_json = ?, version = version + 1, "
                            "updated_at = ? WHERE id = ?", (terms.quantity, terms.limit_price, terms.model_dump_json(), terms.order_hash(),
                                                            risk.model_dump_json(), _iso(_now()), pid))
                self._event(cur, pid, p["status"], p["status"], actor, f"changed to {describe(terms.model_dump())}", {"order_hash": terms.order_hash()})
                if status != p["status"]:
                    self._move(cur, pid, status, actor, risk.text)
        self.bus.publish("proposal.amended", pid)
        if risk.ok:
            self._ask_approval(pid)
        return self.get(pid)

    def recheck(self, pid: str, actor: str = "you") -> dict[str, Any]:
        """Check a blocked or waiting proposal again; if it passes and has no live approval, ask for one."""
        with self._lock:
            p = self._proposal(pid)
            if p["status"] not in ("awaiting_approval", "blocked"):
                raise RequestError(f"A proposal that is {p['status'].replace('_', ' ')} is not waiting for a decision.")
            risk = self.assess(C.OrderTerms(**p["terms"]), proposal_id=pid)
            with self.db.tx() as cur:
                self._move(cur, pid, "awaiting_approval" if risk.ok else "blocked", actor, risk.text, risk_json=risk.model_dump_json())
        if risk.ok and not self.get(pid)["pending_approval"]:
            self._ask_approval(pid)
        self.bus.publish("proposal.checked", pid, ok=risk.ok)
        return self.get(pid)

    def cancel(self, pid: str, actor: str = "you", reason: str = "cancelled") -> dict[str, Any]:
        with self._lock:
            p = self._proposal(pid)
            if p["status"] in PRE_SUBMIT:
                with self.db.tx() as cur:
                    cur.execute("UPDATE approvals SET decision = 'superseded', decided_at = ? WHERE proposal_id = ? AND decision = 'pending'", (_iso(_now()), pid))
                    self._move(cur, pid, "cancelled", actor, reason)
                    self._release_rest(cur, p, reason)
            elif p["status"] in ("submitted", "partially_filled", "cancel_requested", "unknown"):
                self.jobs.enqueue("desk.cancel", {"proposal_id": pid, "reason": reason}, dedupe_key=f"cancel:{pid}:{_iso(_now())}", max_attempts=6)
                self.ensure_worker()
            else:
                raise RequestError(f"A proposal that is {p['status'].replace('_', ' ')} cannot be cancelled.")
        self.bus.publish("proposal.cancel", pid)
        return self.get(pid)

    def expire_due(self, payload: dict[str, Any] | None = None) -> dict[str, int]:
        now = _iso(_now())
        n = 0
        with self._lock:
            with self.db.tx() as cur:
                cur.execute("UPDATE approvals SET decision = 'expired', decided_at = ? WHERE decision = 'pending' AND expires_at <= ?", (now, now))
            rows = self.db.query("SELECT id FROM trade_proposals WHERE status IN ('draft', 'awaiting_approval', 'blocked', 'authorized') AND expires_at <= ?",
                                 (now,))
            for r in rows:
                p = self._proposal(r["id"])
                with self.db.tx() as cur:
                    self._move(cur, p["id"], "expired", "desk", "not decided in time")
                    self._release_rest(cur, p, "expired")
                self.bus.publish("proposal.expired", p["id"])
                n += 1
        return {"expired": n}

    # --- execution ----------------------------------------------------------------------------------------------
    def broker_for(self, account_kind: str, sending: bool = True) -> Any:
        if account_kind in ("paper", "shadow"):
            return self.brokers["paper"]
        b = self.brokers.get("live")
        if b is None:
            raise BrokerError("live execution is not enabled on this computer")
        if sending and not self.check_live()["ready"]:
            raise BrokerError("live execution is not ready: " + "; ".join(self.live_reasons))
        return b

    def _order_dict(self, o: Any) -> dict[str, Any]:
        d = dict(o)
        d["terms"] = json.loads(d.pop("terms_json"))
        d["fills"] = [{"at": f["at"], "quantity": f["quantity"], "price": f["price"], "fees": f["fees"], "external_id": f["external_id"]}
                      for f in self.db.query("SELECT * FROM broker_fills WHERE order_id = ? ORDER BY id", (d["id"],))]
        return d

    def run_submit(self, payload: dict[str, Any]) -> dict[str, Any]:
        pid = payload["proposal_id"]
        with self._lock:
            p = self._proposal(pid)
            if p["status"] == "submitting":
                # A worker stopped between writing the order and seeing the answer: never send again; find out.
                return self._to_unknown(p, "the worker stopped during the submission")
            if p["status"] != "authorized":
                return {"skipped": f"the proposal is {p['status']}"}
            terms = C.OrderTerms(**p["terms"])
            if terms.order_hash() != p["order_hash"] or (p["authorization"] or {}).get("order_hash") != p["order_hash"]:
                with self.db.tx() as cur:
                    self._move(cur, pid, "blocked", "desk", "the authorized terms do not match the order")
                    self._release_rest(cur, p, "terms mismatch")
                return {"blocked": "terms mismatch"}
            risk = self.assess(terms, proposal_id=pid)
            if not risk.ok:
                with self.db.tx() as cur:
                    self._move(cur, pid, "blocked", "desk", f"at submission: {risk.text}", risk_json=risk.model_dump_json())
                    self._release_rest(cur, p, "blocked at submission")
                self.bus.publish("proposal.blocked", pid)
                self.notifier.enqueue("proposal.blocked", f"Not sent: {describe(p['terms'])}", risk.text, severity="warn", proposal_id=pid,
                                      dedupe_key=f"blocked:{pid}:submit:{p['version']}")
                return {"blocked": risk.text}
            try:
                broker = self.broker_for(p["account_kind"])
            except BrokerError as exc:
                with self.db.tx() as cur:
                    self._move(cur, pid, "blocked", "desk", str(exc))
                    self._release_rest(cur, p, str(exc))
                return {"blocked": str(exc)}
            oid = C.new_id("ord")
            client_id = f"ta-{pid}-v{p['version']}"
            now = _iso(_now())
            with self.db.tx() as cur:
                cur.execute("""INSERT INTO broker_orders (id, proposal_id, mode, account_kind, broker, account_ref, client_order_id, state, created_at,
                               updated_at, terms_json, request_json) VALUES (?, ?, ?, ?, ?, ?, ?, 'submitting', ?, ?, ?, ?)""",
                            (oid, pid, p["mode"], p["account_kind"], broker.name, p.get("account_ref"), client_id, now, now, json.dumps(p["terms"]),
                             json.dumps({"client_order_id": client_id, "terms": p["terms"]})))
                self._move(cur, pid, "submitting", "desk", f"sending to {broker.name}", risk_json=risk.model_dump_json())
            order = self._order_dict(self.db.one("SELECT * FROM broker_orders WHERE id = ?", (oid,)))
            try:
                res = broker.place(order)
            except BrokerTimeout as exc:
                return self._to_unknown(self._proposal(pid), f"no answer from {broker.name}: {exc}")
            except BrokerError as exc:
                res = {"state": "rejected", "broker_order_id": None, "fills": [], "message": str(exc)}
            except Exception as exc:  # noqa: BLE001 - anything unexpected: the outcome is not known
                return self._to_unknown(self._proposal(pid), f"{exc.__class__.__name__}: {exc}")
            return self._apply(order, res, actor=broker.name)

    def _to_unknown(self, p: dict[str, Any], why: str) -> dict[str, Any]:
        with self.db.tx() as cur:
            cur.execute("UPDATE broker_orders SET state = 'unknown', updated_at = ?, error = ? WHERE proposal_id = ? AND state IN ('intent', 'submitting')",
                        (_iso(_now()), why[:500], p["id"]))
            if p["status"] != "unknown":
                self._move(cur, p["id"], "unknown", "desk", why)
        self.bus.publish("proposal.unknown", p["id"], reason=why[:200])
        self.notifier.enqueue("order.unknown", f"Unknown: {describe(p['terms'])}",
                              f"It is not known whether this order reached the broker ({why}). It will not be sent again; the desk is "
                              "asking the broker what became of it.", severity="critical", proposal_id=p["id"], dedupe_key=f"unknown:{p['id']}")
        self.jobs.enqueue("desk.reconcile", {"proposal_id": p["id"]}, dedupe_key=f"reconcile:{p['id']}:{_iso(_now())}")
        return {"unknown": why}

    def _apply(self, order: dict[str, Any], res: dict[str, Any], actor: str = "broker") -> dict[str, Any]:
        """Record what the broker said about one order: its fills (each once), its state, the proposal's state and the ledger."""
        with self._lock:
            p = self._proposal(order["proposal_id"])
            state = res.get("state") or order["state"]
            if state == "not_found":
                prior = json.loads(order.get("response_json") or "{}") if isinstance(order.get("response_json"), str) else {}
                misses = int(prior.get("misses") or 0) + 1
                age = (_now() - datetime.fromisoformat(order["created_at"])).total_seconds()
                if not (res.get("conclusive") or (misses >= NOT_FOUND_MISSES and age >= NOT_FOUND_SECONDS)):
                    # One empty answer is not proof: the broker may not list it yet. Keep it unknown, and its capital held.
                    with self.db.tx() as cur:
                        cur.execute("UPDATE broker_orders SET response_json = ?, last_reconciled_at = ? WHERE id = ?",
                                    (json.dumps({"misses": misses, "message": res.get("message")}), _iso(_now()), order["id"]))
                    return {"state": order["state"], "proposal": p["status"], "fills": 0, "still_looking": misses}
                state = "failed"
                res = {**res, "message": res.get("message") or "the broker has no record of it: it was never placed"}
            terms = order["terms"]
            mult = float(terms["option"]["multiplier"]) if terms.get("option") else 1.0
            opening = terms["side"] in OPENING_SIDES
            new_fills = []
            recancel = False
            with self.db.tx() as cur:
                known = {r["external_id"] for r in cur.execute("SELECT external_id FROM broker_fills WHERE order_id = ?", (order["id"],)).fetchall()}
                avg_cost = None
                if not opening:
                    held = self.positions(p["account_kind"], marks=False)["positions"]
                    key = terms["option"]["option_symbol"] if terms.get("option") else terms["symbol"]
                    avg_cost = next((h["avg_cost"] for h in held if h["key"] == key), None)
                for f in res.get("fills") or []:
                    if f["external_id"] in known:
                        continue
                    cur.execute("INSERT INTO broker_fills (order_id, at, quantity, price, fees, external_id) VALUES (?, ?, ?, ?, ?, ?)",
                                (order["id"], f["at"], float(f["quantity"]), float(f["price"]), float(f.get("fees") or 0), f["external_id"]))
                    new_fills.append(f)
                    if opening:
                        rest = self._reserved_rest(cur, p["id"])
                        amount = min(rest, float(f["quantity"]) * float(terms["limit_price"] or f["price"]) * mult)
                        if amount > 1e-9:
                            self._ledger(cur, "release", amount, p, note=f"fill: {f['quantity']:g} at {f['price']:.2f}", order_id=order["id"])
                    elif avg_cost is not None:
                        pnl = (float(f["price"]) - avg_cost) * float(f["quantity"]) * mult - float(f.get("fees") or 0)
                        self._ledger(cur, "realized", pnl, p, note=f"sold {f['quantity']:g} at {f['price']:.2f}", order_id=order["id"])
                rows = cur.execute("SELECT quantity, price FROM broker_fills WHERE order_id = ?", (order["id"],)).fetchall()
                filled = sum(float(r["quantity"]) for r in rows)
                avg = sum(float(r["quantity"]) * float(r["price"]) for r in rows) / filled if filled else None
                if state in ("cancelled", "expired") and filled and filled < float(terms["quantity"]) - 1e-9:
                    pass                                    # a part-filled order that ended: the fills stand
                elif filled and filled < float(terms["quantity"]) - 1e-9 and state in ("submitted", "accepted"):
                    state = "partially_filled"
                if order["state"] == "cancel_requested" and state in ("submitted", "accepted", "partially_filled"):
                    state = "cancel_requested"              # still working at the broker: the cancel is asked again below
                    recancel = True
                cur.execute("""UPDATE broker_orders SET state = ?, broker_order_id = COALESCE(?, broker_order_id), updated_at = ?, response_json = ?,
                               filled_quantity = ?, average_fill_price = ?, last_reconciled_at = ?, error = ? WHERE id = ?""",
                            (state, res.get("broker_order_id"), _iso(_now()), json.dumps(res, default=str)[:4000], filled, avg, _iso(_now()),
                             res.get("message") if state in ("rejected", "failed") else None, order["id"]))
                target = ORDER_TO_PROPOSAL.get(state, p["status"])
                if target != p["status"]:
                    if not C.can_move(p["status"], target) and C.can_move(p["status"], "submitted") and C.can_move("submitted", target):
                        self._move(cur, p["id"], "submitted", actor, "accepted")
                    if C.can_move(self._status(cur, p["id"]), target):
                        self._move(cur, p["id"], target, actor, res.get("message") or state)
                if state in ("filled", "cancelled", "expired", "rejected", "failed") and opening:
                    self._release_rest(cur, p, state)
        final = self._proposal(p["id"])["status"]
        if state in ("submitted", "accepted", "partially_filled", "cancel_requested") and (recancel or self.kill_switch().get("on")):
            self.jobs.enqueue("desk.cancel", {"proposal_id": p["id"], "reason": "the kill switch" if self.kill_switch().get("on") else "cancel asked for"},
                              dedupe_key=f"cancel:{p['id']}:{_iso(_now())[:16]}", max_attempts=6)
        self.bus.publish(f"proposal.{final}", p["id"], fills=len(new_fills))
        if new_fills:
            qty = sum(float(f["quantity"]) for f in new_fills)
            self.notifier.enqueue("order.filled", f"Filled: {describe(terms)}",
                                  f"{qty:g} filled at ${avg:,.2f} on average in the {p['account_kind']} account.", proposal_id=p["id"],
                                  dedupe_key=f"filled:{order['id']}:{filled:g}")
        elif state in ("rejected", "failed"):
            self.notifier.enqueue(f"order.{state}", f"{state.title()}: {describe(terms)}", res.get("message") or state, severity="warn",
                                  proposal_id=p["id"], dedupe_key=f"{state}:{order['id']}")
        self._loss_watch(p["account_kind"])
        return {"state": state, "proposal": final, "fills": len(new_fills)}

    @staticmethod
    def _status(cur: Any, pid: str) -> str:
        return cur.execute("SELECT status FROM trade_proposals WHERE id = ?", (pid,)).fetchone()["status"]

    def reconcile(self, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        """Ask the broker about every order that is not finished (or one proposal's), and record the answers."""
        pid = (payload or {}).get("proposal_id")
        sql = f"SELECT * FROM broker_orders WHERE mode = ? AND state IN ({','.join('?' * len(C.OPEN_ORDER_STATES))})"
        params: list[Any] = [self.mode, *sorted(C.OPEN_ORDER_STATES)]
        if pid:
            sql += " AND proposal_id = ?"
            params.append(pid)
        done = []
        in_flight = _iso(_now() - timedelta(minutes=2))
        for o in self.db.query(sql, params):
            order = self._order_dict(o)
            if order["state"] in ("intent", "submitting") and order["updated_at"] > in_flight:
                continue                                   # possibly being sent right now; recover() handles a stuck one
            try:
                broker = self.broker_for(order["account_kind"], sending=False)
                res = broker.status(order)
            except (BrokerTimeout, BrokerError) as exc:
                done.append({"order": order["id"], "error": str(exc)[:200]})
                continue
            if res.get("state") == "not_found" and order["state"] not in ("unknown", "intent", "submitting"):
                res = {**res, "state": order["state"]}       # a placed order the broker cannot find now: keep asking, do not guess
            done.append({"order": order["id"], **self._apply(order, res, actor=f"reconcile ({broker.name})")})
        return {"checked": len(done), "orders": done}

    def run_cancel(self, payload: dict[str, Any]) -> dict[str, Any]:
        pid = payload["proposal_id"]
        out = []
        rows = self.db.query("""SELECT * FROM broker_orders WHERE proposal_id = ?
                                AND state IN ('submitted', 'accepted', 'partially_filled', 'cancel_requested', 'unknown', 'submitting')""", (pid,))
        for o in rows:
            order = self._order_dict(o)
            if order["state"] in ("unknown", "submitting") or not order.get("broker_order_id"):
                # Find it first: nothing can be cancelled by an id the desk never heard. Retried until it is found or settled.
                res = self.broker_for(order["account_kind"], sending=False).status(order)
                if res.get("state") not in ("submitted", "accepted", "partially_filled"):
                    got = self._apply(order, res, actor="cancel (look-up)")
                    if got["state"] in ("unknown", "submitting"):
                        raise BrokerTimeout("the order is not found yet; the cancel will be tried again")
                    out.append(got)
                    continue
                order = {**order, "broker_order_id": res.get("broker_order_id") or order.get("broker_order_id")}
            p = self._proposal(pid)
            with self.db.tx() as cur:
                if C.can_move(p["status"], "cancel_requested"):
                    self._move(cur, pid, "cancel_requested", "desk", payload.get("reason") or "cancel asked for")
                cur.execute("UPDATE broker_orders SET state = 'cancel_requested', updated_at = ? WHERE id = ?", (_iso(_now()), order["id"]))
            order["state"] = "cancel_requested"
            res = self.broker_for(order["account_kind"], sending=False).cancel(order)
            out.append(self._apply(order, res, actor="cancel"))
        return {"cancelled": out}

    def recover(self, older_than_seconds: float = 180.0) -> dict[str, Any]:
        """On start: a submission that was cut off is unknown, and is reconciled, never sent again. Only one written
        more than three minutes ago: a younger one may still be in the hands of the other process."""
        cutoff = _iso(_now() - timedelta(seconds=older_than_seconds))
        stuck = self.db.query("SELECT id FROM trade_proposals WHERE status = 'submitting' AND updated_at <= ?", (cutoff,))
        for r in stuck:
            self._to_unknown(self._proposal(r["id"]), "the app or daemon stopped during the submission")
        return {"recovered": len(stuck)}

    def _loss_watch(self, account_kind: str) -> None:
        try:
            pol = self.policy()
            pnl = self.positions(account_kind)["day_pnl"]
        except Exception:  # noqa: BLE001 - the stop is enforced by the risk engine either way
            return
        if pnl is not None and pnl <= -pol.max_daily_loss:
            day = datetime.now(NY).date().isoformat()
            self.notifier.enqueue("desk.loss_stop", "Daily loss stop reached",
                                  f"The {account_kind} account is ${pnl:,.2f} today, past the ${pol.max_daily_loss:,.0f} stop. No new buys until "
                                  "tomorrow; selling what is held is still allowed.", severity="critical", dedupe_key=f"loss_stop:{account_kind}:{day}")

    # --- the worker ----------------------------------------------------------------------------------------------
    def handlers(self) -> dict[str, Any]:
        out = {"desk.submit": self.run_submit, "desk.reconcile": self.reconcile, "desk.expire": self.expire_due,
               "desk.cancel": self.run_cancel, "notify.deliver": self.notifier.deliver_due}
        ap = getattr(self.svc, "autopilot", None)
        if ap is not None:
            out["autopilot.run"] = ap.run
        return out

    @staticmethod
    def schedules() -> list[Schedule]:
        return [Schedule("desk.reconcile", 20, 40), Schedule("desk.expire", 30, 60), Schedule("notify.deliver", 15, 30),
                Schedule("autopilot.run", 300, 240, when="market")]

    def make_worker(self, component: str, defer_to: str | None, extra_handlers: dict[str, Any] | None = None,
                    extra_schedules: list[Schedule] | None = None) -> Worker:
        # Other areas' jobs (4.0: grading forecasts) run in the same worker, whichever process holds it.
        ext_handlers: dict[str, Any] = {}
        ext_schedules: list[Schedule] = []
        for provide in getattr(self.svc, "job_extensions", []):
            h, s = provide(self.svc)
            ext_handlers.update(h)
            ext_schedules.extend(s)
        return Worker(self.db, {**self.handlers(), **ext_handlers, **(extra_handlers or {})},
                      self.schedules() + ext_schedules + (extra_schedules or []),
                      component=component, defer_to=defer_to, info=lambda: {"mode": self.mode})

    def ensure_worker(self) -> None:
        """The app runs the desk's jobs itself unless a daemon does (DESK_WORKER=auto), or always (app), or never."""
        if self.worker_mode not in ("auto", "app"):
            return
        with self._lock:
            if self.worker is None:
                self.worker = self.make_worker("app-worker", "daemon" if self.worker_mode == "auto" else None)
                try:
                    if not alive(self.db, "daemon"):
                        self.recover()
                except Exception:  # noqa: BLE001
                    log.exception("Desk recovery failed")
            self.worker.start()

    def worker_status(self) -> dict[str, Any]:
        hb = last_heartbeat(self.db, "daemon")
        daemon_alive = alive(self.db, "daemon")
        app = self.worker
        return {"setting": self.worker_mode, "daemon": {"alive": daemon_alive, "last_seen": hb["at"] if hb else None,
                                                        "age_seconds": round(hb["age_seconds"], 1) if hb else None,
                                                        "pid": hb["pid"] if hb else None},
                "app_worker": {"running": bool(app and app.running()), "deferring": bool(app and app.running() and daemon_alive and app.defer_to),
                               "last_error": app.last_error if app else None},
                "runs_jobs": "daemon" if daemon_alive else ("app" if app and app.running() else "nobody"),
                "jobs": self.jobs.counts()}

    # --- status -------------------------------------------------------------------------------------------------
    def status(self) -> dict[str, Any]:
        row = P.active(self.db)
        pol = P.TradingPolicy(**row["policy"])
        try:
            b = self.book(pol.account_kind)
            budget = {"capital": pol.trial_capital, "in_use": round(b["capital_in_use"], 2), "reserved": round(b["reserved"], 2),
                      "positions_cost": round(b["cost"], 2), "today_new_buys": round(b["today_new_buys"], 2), "daily_buys_cap": pol.max_daily_new_buys,
                      "day_pnl": None if b["day_pnl"] is None else round(b["day_pnl"], 2), "daily_loss_stop": pol.max_daily_loss,
                      "positions": len(b["position_rows"]), "max_positions": pol.max_open_positions, "unmeasured": b["unmeasured"]}
        except Exception as exc:  # noqa: BLE001 - the status shows what it could not read
            budget = {"error": str(exc)[:200]}
        counts = {r["status"]: r["n"] for r in self.db.query("SELECT status, COUNT(*) AS n FROM trade_proposals WHERE mode = ? GROUP BY status", (self.mode,))}
        return {"policy": row, "summary": pol.summary(), "mode_words": P.MODE_WORDS[pol.mode], "kill_switch": self.kill_switch(),
                "live_ready": self.live_ready, "budget": budget, "proposals": counts, "worker": self.worker_status(),
                "notifications": self.notifier.describe(), "remote": self.remote_status(), "live": self.live_status()}

    def remote_status(self) -> dict[str, Any]:
        s = self.svc.settings
        return {"enabled": s.remote_approvals, "base_url": s.remote_base_url or None,
                "served_by": "daemon" if s.remote_approvals else None}

    # --- the workstation's status strip -------------------------------------------------------------------------
    def strip_trading(self) -> dict[str, Any]:
        pol = self.policy()
        label = {"disabled": "Trading off: nothing is sent", "paper": "Paper trading: approved orders fill on paper",
                 "approval_required": "Live: each order approved by you", "capped_autopilot": f"Capped autopilot ({pol.account_kind})"}[pol.mode]
        return {"mode": pol.mode, "label": label, "detail": pol.summary(), "account_kind": pol.account_kind, "live_ready": self.live_ready}

    def strip_risk(self) -> dict[str, Any]:
        ks = self.kill_switch()
        if ks.get("on"):
            return {"level": "fail", "label": "Kill switch on", "detail": ks.get("reason") or "", "kill_switch": True}
        waiting = self.db.one("SELECT COUNT(*) AS n FROM trade_proposals WHERE mode = ? AND status = 'awaiting_approval'", (self.mode,))["n"]
        unknown = self.db.one("SELECT COUNT(*) AS n FROM trade_proposals WHERE mode = ? AND status = 'unknown'", (self.mode,))["n"]
        if unknown:
            return {"level": "fail", "label": f"{unknown} order{'s' if unknown != 1 else ''} in an unknown state", "detail": "being reconciled with the broker",
                    "kill_switch": False}
        if waiting:
            return {"level": "warn", "label": f"{waiting} waiting for your approval", "detail": "Trading Desk: Approvals", "kill_switch": False}
        return {"level": "pass", "label": "Risk checks on", "detail": "every order is checked when written, approved and sent; an unmeasured check blocks it",
                "kill_switch": False}

    def strip_daemon(self) -> dict[str, Any]:
        w = self.worker_status()
        if w["runs_jobs"] == "daemon":
            return {"level": "pass", "label": "Daemon running", "detail": f"heartbeat {w['daemon']['age_seconds']:.0f} seconds ago"}
        if w["runs_jobs"] == "app":
            return {"level": "warn", "label": "Desk jobs run in the app", "detail": "they stop when the app stops; ./setup_daemon.sh keeps them running"}
        return {"level": "" if self.worker_mode in ("auto", "app") else "warn", "label": "Desk jobs idle",
                "detail": "they start with the first proposal" if self.worker_mode in ("auto", "app") else "DESK_WORKER waits for the daemon, which is not running"}


def install(svc: Any) -> None:
    from ..api_v3 import register, status_part
    from ..autopilot import Autopilot
    desk = TradingDesk(svc)
    svc.trading = desk
    svc.autopilot = Autopilot(svc)
    status_part(svc, "trading", desk.strip_trading)
    status_part(svc, "risk", desk.strip_risk)
    status_part(svc, "daemon", desk.strip_daemon)

    def routes(router: APIRouter, svc: Any) -> None:
        d: TradingDesk = svc.trading

        @router.get("/desk/status")
        def status() -> dict[str, Any]:
            return d.status()

        @router.get("/desk/policy")
        def get_policy() -> dict[str, Any]:
            return {**P.active(d.db), "modes": P.MODE_WORDS, "defaults": P.TradingPolicy().model_dump()}

        @router.put("/desk/policy")
        def put_policy(body: dict[str, Any]) -> dict[str, Any]:
            if body.get("mode") == "capped_autopilot" and body.get("account_kind") == "live" and not svc.settings.autopilot_live:
                raise RequestError("The autopilot may not trade live: AUTOPILOT_LIVE=1 is not set in .env. Use the shadow or paper account.")
            try:
                return P.save(d.db, body, live_allowed=d.live_ready)
            except ValueError as exc:
                raise RequestError(str(exc).split("\n")[-1][:400] if "validation error" in str(exc) else str(exc)[:400]) from exc

        @router.post("/desk/kill")
        def kill(body: KillIn) -> dict[str, Any]:
            return d.set_kill_switch(body.on, body.reason)

        @router.get("/desk/live")
        def live() -> dict[str, Any]:
            return d.live_status()

        @router.post("/desk/live/check")
        def live_check() -> dict[str, Any]:
            return d.check_live(max_age=0)

        @router.get("/desk/positions")
        def positions() -> dict[str, Any]:
            out = d.positions()
            # 3.1: who manages each holding's exit, and what kind of protection that is (never a broker stop here).
            exits = svc.autopilot.exit_plans(out["account_kind"]) if getattr(svc, "autopilot", None) else {}
            for row in out["positions"]:
                row["exit"] = exits.get(row["symbol"]) or {"managed_by": "you", "protection": "none",
                                                           "text": "Yours to exit: no stop is placed at a broker by this app."}
            out["exit_protection"] = svc.autopilot.protection() if getattr(svc, "autopilot", None) else None
            return out

        @router.get("/desk/ledger")
        def ledger(limit: int = 100) -> dict[str, Any]:
            rows = d.db.query("SELECT * FROM budget_ledger WHERE mode = ? ORDER BY id DESC LIMIT ?", (d.mode, max(1, min(limit, 500))))
            return {"rows": [dict(r) for r in rows]}

        @router.get("/desk/jobs")
        def jobs(limit: int = 50) -> dict[str, Any]:
            return {"jobs": d.jobs.recent(max(1, min(limit, 200))), "worker": d.worker_status()}

        @router.post("/desk/reconcile")
        def reconcile() -> dict[str, Any]:
            return d.reconcile()

        @router.get("/proposals")
        def proposals(status: str | None = None, limit: int = 50) -> dict[str, Any]:
            return {"proposals": d.listing(status, max(1, min(limit, 200)))}

        @router.post("/proposals")
        def create(body: ProposalIn) -> dict[str, Any]:
            return d.create(body)

        @router.post("/proposals/preview")
        def preview(body: ProposalIn) -> dict[str, Any]:
            return d.preview(body)

        @router.get("/proposals/{pid}")
        def one(pid: str) -> dict[str, Any]:
            return d.get(pid)

        @router.post("/proposals/{pid}/amend")
        def amend(pid: str, body: AmendIn) -> dict[str, Any]:
            return d.amend(pid, body)

        @router.post("/proposals/{pid}/recheck")
        def recheck(pid: str) -> dict[str, Any]:
            return d.recheck(pid)

        @router.post("/proposals/{pid}/cancel")
        def cancel(pid: str) -> dict[str, Any]:
            return d.cancel(pid)

        @router.post("/approvals/{aid}/approve")
        def approve(aid: str, body: DecisionIn) -> dict[str, Any]:
            return d.decide(aid, True, body.order_hash, channel="app")

        @router.post("/approvals/{aid}/decline")
        def decline(aid: str, body: DecisionIn) -> dict[str, Any]:
            return d.decide(aid, False, body.order_hash, channel="app")

        @router.get("/autopilot")
        def autopilot() -> dict[str, Any]:
            return svc.autopilot.status()

        @router.post("/autopilot/run")
        def autopilot_run() -> dict[str, Any]:
            return svc.autopilot.run()

        @router.get("/notifications")
        def notifications(limit: int = 30) -> dict[str, Any]:
            return {"notifications": d.notifier.recent(max(1, min(limit, 200))), **d.notifier.describe()}

        @router.post("/notifications/test")
        def test_notification() -> dict[str, Any]:
            chans = d.notifier.channels()
            if not chans:
                raise RequestError("No channel is set up: put SMTP, NTFY_TOPIC or TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID in .env.")
            nid = d.notifier.enqueue("test", "Test from the Trading Desk", "If this arrived, the desk can reach you here.")
            d.ensure_worker()
            return {"id": nid, "channels": chans}

    register(svc, routes)


__all__ = ["TradingDesk", "ProposalIn", "AmendIn", "DecisionIn", "install", "describe", "token_hash"]
