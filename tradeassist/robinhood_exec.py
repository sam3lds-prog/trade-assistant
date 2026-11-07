"""Live execution through Robinhood's MCP server (3.0 phase 6): a separate adapter, off unless switched on.

The market-data connection (providers/robinhood.py) stays read-only and cannot call a tool that acts: that bridge
refuses every name with "place", "review", "cancel" or "order" in it. Orders go through this module only, which
builds its own connection (`TradingBridge`) and may call exactly these tools, found by name on the server:

| job | tools tried, in order | needed |
| --- | --- | --- |
| accounts | get_accounts | to confirm the account orders go to |
| review | review_equity_order, review_stock_order | used when the server has it |
| place | place_equity_order, place_stock_order | yes |
| look up one | get_equity_order, get_stock_order | used when the server has it |
| list orders | get_equity_orders, get_stock_orders | yes (reconciliation finds an order by its reference) |
| cancel | cancel_equity_order, cancel_stock_order | yes (the kill switch) |

Nothing else: no options, crypto, transfers or account settings, and a multi-leg order is never sent.

**Switched on only when all hold**: ROBINHOOD_EXECUTION=1, ROBINHOOD_EXECUTION_ACCOUNT names the account, that
account is on the server's list (and, where the server says, allowed for agent trading), the place, look-up and
cancel tools exist with arguments this module can fill, and the prices are real. Then the trading policy must be
set to a live mode by you; LIVE_MAX_ORDER caps every live order whatever the policy says.

**One order, one attempt.** Each order carries a reference derived from the desk's client order id (a UUID where
the server takes one), so the server can recognise a repeat. A place call that gets no answer, or whose connection
drops, is never repeated: the order is `unknown` until the look-up finds it (or establishes that it was never
received). A review the server answers with an error stops the order before it is placed.
"""

from __future__ import annotations

import hashlib
import re
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Callable

from .brokers import BrokerError, BrokerTimeout
from .providers.base import DataError
from .providers.mcp_bridge import NoAnswer, ReadOnlyBridge, Refused
from .providers.robinhood import choose, properties, records, required, unwrap

EXEC_TOOLS: dict[str, tuple[str, ...]] = {
    "accounts": ("get_accounts",),
    "review": ("review_equity_order", "review_stock_order"),
    "place": ("place_equity_order", "place_stock_order"),
    "lookup": ("get_equity_order", "get_stock_order"),
    "list": ("get_equity_orders", "get_stock_orders"),
    "cancel": ("cancel_equity_order", "cancel_stock_order"),
}
EXEC_NAME = re.compile(r"get_accounts|(review|place|cancel)_(equity|stock)_order|get_(equity|stock)_orders?")
NEVER = ("option", "crypto", "transfer", "withdraw", "deposit", "bank", "margin", "exercise", "and_", "or_", "all")
REQUIRED_JOBS = ("place", "list", "cancel", "accounts")
NAMESPACE = uuid.UUID("6f1c4b7e-3b0a-4f53-9d55-6a5f2f0c7e11")

STATE_MAP = {"queued": "submitted", "unconfirmed": "submitted", "confirmed": "submitted", "new": "submitted", "pending": "submitted",
             "accepted": "accepted", "open": "submitted", "partially_filled": "partially_filled", "partial": "partially_filled",
             "filled": "filled", "cancelled": "cancelled", "canceled": "cancelled", "rejected": "rejected", "failed": "rejected",
             "expired": "expired", "cancel_requested": "cancel_requested", "pending_cancel": "cancel_requested"}


def mask(account: str) -> str:
    return f"…{account[-4:]}" if account else ""


def account_ref(account: str) -> str:
    """How an order names its account: the last four digits to read, and a hash of the whole number so that another
    account ending in the same digits is a different account (an approval for one never sends to the other)."""
    return f"{mask(account)}-{hashlib.sha256(account.strip().encode()).hexdigest()[:10]}" if account else ""


def ref_id(client_order_id: str) -> str:
    return str(uuid.uuid5(NAMESPACE, client_order_id))


def acceptable_exec_name(name: str) -> bool:
    return EXEC_NAME.fullmatch(name) is not None and not any(f in name for f in NEVER)


class TradingBridge(ReadOnlyBridge):
    """The MCP connection that may act, restricted to EXEC_TOOLS by exact name. Reads (get_) must still be marked
    read-only by the server. A call that acts is sent once: no reconnect-and-repeat."""

    def __init__(self, connect: Callable[[], Any], allowed: frozenset[str], label: str = "Robinhood (trading)",
                 call_timeout: float = 20.0, first_call_timeout: float = 30.0):
        bad = sorted(n for n in allowed if not acceptable_exec_name(n))
        if bad:
            raise ValueError(f"These tool names may not be called by the trading bridge: {', '.join(bad)}")
        super().__init__(connect, frozenset(), label=label, first_call_timeout=first_call_timeout, call_timeout=call_timeout)
        self._allowed = frozenset(allowed)

    def _permitted(self, name: str, annotations: Any) -> bool:
        if name not in self._allowed or not acceptable_exec_name(name):
            return False
        if name.startswith("get_") and annotations is not None and (getattr(annotations, "destructive_hint", None) is True
                                                                     or getattr(annotations, "read_only_hint", None) is False):
            return False
        return True

    def call(self, name: str, arguments: dict[str, Any] | None = None) -> Any:
        if name not in self._allowed:
            raise PermissionError(f"Refusing to call {name!r}: it is not one of the trading adapter's tools.")
        tool = self.tools().get(name)
        if tool is None:
            raise DataError(f"{self.label} has no tool named {name}")
        if not self._permitted(name, getattr(tool, "annotations", None)):
            raise PermissionError(f"Refusing to call {name}: the server marks this read as one that changes something.")
        self.calls.append(name)
        del self.calls[:-500]
        from .providers.mcp_bridge import parse_tool_result
        result = self._submit(lambda client: client.call_tool(name, arguments or {}), self._call_timeout,
                              retry=name.startswith("get_"))
        return parse_tool_result(result)


def _num(v: Any) -> float | None:
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if f == f and abs(f) != float("inf") else None


def _order_of(payload: Any) -> dict[str, Any] | None:
    data = unwrap(payload)
    if isinstance(data, dict):
        for key in ("order", "result"):
            if isinstance(data.get(key), dict):
                return data[key]
        if "id" in data or "state" in data or "status" in data:
            return data
    rows = records(payload, "orders", "results")
    return rows[0] if len(rows) == 1 else None


class RobinhoodExecution:
    """A broker for the Trading Desk (brokers.Broker): place, status, cancel. Equity, single leg, limit only."""
    name = "robinhood"

    def __init__(self, bridge: TradingBridge, account: str):
        self.bridge = bridge
        self.account = account.strip()
        self._ready: dict[str, Any] | None = None
        self._ready_at = 0.0

    # --- is it usable? -------------------------------------------------------------------------------
    def tools(self) -> dict[str, str | None]:
        listed = self.bridge.tools()
        out: dict[str, str | None] = {}
        for job, names in EXEC_TOOLS.items():
            out[job] = next((n for n in names if n in listed and self.bridge.may_call(n)), None)
        return out

    def readiness(self, max_age: float = 600.0) -> dict[str, Any]:
        now = datetime.now(timezone.utc).timestamp()
        if self._ready is not None and now - self._ready_at < max_age:
            return self._ready
        reasons: list[str] = []
        try:
            tools = self.tools()
        except DataError as exc:
            out = {"ready": False, "reasons": [f"Robinhood could not be reached: {exc}"], "tools": {}, "account": mask(self.account)}
            self._ready, self._ready_at = out, now
            return out
        for job in REQUIRED_JOBS:
            if not tools.get(job):
                reasons.append(f"the server offers no {job} tool ({' or '.join(EXEC_TOOLS[job])})")
        if tools.get("accounts"):
            try:
                accts = records(self.bridge.call(tools["accounts"]), "accounts", "results")
                mine = [a for a in accts if str(a.get("account_number") or a.get("number") or a.get("id") or "") == self.account]
                if not mine:
                    reasons.append(f"account {mask(self.account)} is not one of the accounts this sign-in can trade")
                elif mine[0].get("agentic_allowed") is False:
                    reasons.append(f"account {mask(self.account)} is not enabled for agent trading at Robinhood")
            except DataError as exc:
                reasons.append(f"the account list could not be read: {exc}")
        if tools.get("place"):
            missing = self._unfillable(tools["place"])
            if missing:
                reasons.append(f"the place tool needs arguments this app cannot fill: {', '.join(missing)}")
        out = {"ready": not reasons, "reasons": reasons, "tools": tools, "account": mask(self.account)}
        self._ready, self._ready_at = out, now
        return out

    # --- arguments from each tool's declared schema ---------------------------------------------------
    FIELDS = {
        "symbol": ("symbol", "ticker", "instrument_symbol"),
        "side": ("side", "direction", "action"),
        "quantity": ("quantity", "shares", "qty"),
        "type": ("type", "order_type"),
        "price": ("limit_price", "price"),
        "time_in_force": ("time_in_force", "tif", "duration"),
        "ref": ("ref_id", "client_order_id", "idempotency_key", "client_id", "reference_id"),
        "account": ("account_number", "account_id", "account"),
        "review": ("review_id", "order_review_id", "confirmation_id", "confirmation_token", "preview_id"),
        "order_id": ("order_id", "id", "order_ids", "ids"),
        "extended": ("extended_hours", "extended"),
        "trigger": ("trigger",),
    }

    def _unfillable(self, tool_name: str) -> list[str]:
        tool = self.bridge.tools()[tool_name]
        known = {k for keys in self.FIELDS.values() for k in keys}
        return [r for r in required(tool) if r not in known]

    def _args(self, tool_name: str, values: dict[str, Any]) -> dict[str, Any]:
        tool = self.bridge.tools()[tool_name]
        props = properties(tool)
        args: dict[str, Any] = {}
        for field, value in values.items():
            if value is None:
                continue
            key = choose(props, *self.FIELDS[field])
            if key:
                args[key] = value
        missing = [r for r in required(tool) if r not in args]
        if missing:
            raise BrokerError(f"{tool_name} needs {', '.join(missing)}, which this app does not send")
        return args

    def _values(self, order: dict[str, Any]) -> dict[str, Any]:
        t = order["terms"]
        if t.get("option") or t["instrument"] != "equity":
            raise BrokerError("only share orders are sent to Robinhood automatically; option and multi-leg orders are placed by hand")
        if t["side"] not in ("buy", "sell"):
            raise BrokerError(f"{t['side']} is not sent automatically: only buying and selling shares")
        if t["order_type"] != "limit" or not t.get("limit_price"):
            raise BrokerError("only limit orders are sent automatically")
        return {"symbol": t["symbol"], "side": t["side"], "quantity": t["quantity"], "type": "limit", "price": t["limit_price"],
                "time_in_force": "gfd" if t.get("time_in_force", "day") == "day" else "gtc", "ref": ref_id(order["client_order_id"]),
                "account": self.account, "extended": bool(t.get("extended_hours")), "trigger": "immediate"}

    # --- the broker's three calls -----------------------------------------------------------------------
    def _answer(self, o: dict[str, Any] | None, order: dict[str, Any]) -> dict[str, Any]:
        if not o:
            return {"state": "not_found", "broker_order_id": order.get("broker_order_id"), "fills": [], "message": "no such order at Robinhood"}
        raw = str(o.get("state") or o.get("status") or "").lower()
        state = STATE_MAP.get(raw, "submitted" if raw else "submitted")
        boid = str(o.get("id") or o.get("order_id") or order.get("broker_order_id") or "") or None
        # What has filled in all, from the executions when the answer lists them, else from the cumulative figures. Only
        # the part beyond what the desk has recorded becomes a new fill, so the same shares are never counted twice
        # whichever form the next answer takes.
        execs = [(_num(e.get("quantity")), _num(e.get("price")), _num(e.get("fees")) or 0.0) for e in o.get("executions") or []]
        execs = [(q, pr, fe) for q, pr, fe in execs if q and pr is not None]
        if execs:
            total, cost, fees = sum(q for q, _, _ in execs), sum(q * pr for q, pr, _ in execs), sum(fe for _, _, fe in execs)
        else:
            total = _num(o.get("cumulative_quantity") or o.get("filled_quantity")) or 0.0
            avg = _num(o.get("average_price"))
            cost, fees = (avg * total if avg is not None else None), _num(o.get("fees")) or 0.0
        have = sum(float(f["quantity"]) for f in order.get("fills") or [])
        paid = sum(float(f["quantity"]) * float(f["price"]) for f in order.get("fills") or [])
        fees_had = sum(float(f.get("fees") or 0) for f in order.get("fills") or [])
        fills = list(order.get("fills") or [])
        if total > have + 1e-9 and cost is not None:
            fills.append({"at": str(o.get("updated_at") or datetime.now(timezone.utc).isoformat()), "quantity": round(total - have, 8),
                          "price": round((cost - paid) / (total - have), 6), "fees": max(0.0, fees - fees_had), "external_id": f"{boid}-filled-{total:g}"})
        msg = o.get("reject_reason") or o.get("rejection_reason") or o.get("message") or raw
        return {"state": state, "broker_order_id": boid, "fills": fills, "message": str(msg)[:300]}

    def place(self, order: dict[str, Any]) -> dict[str, Any]:
        ready = self.readiness()
        if not ready["ready"]:
            raise BrokerError("live execution is not ready: " + "; ".join(ready["reasons"]))
        tools = ready["tools"]
        if order["terms"].get("account_ref") != account_ref(self.account):
            raise BrokerError("the order names a different account from the one live trading is bound to")
        values = self._values(order)
        if tools.get("review"):
            try:
                review = unwrap(self.bridge.call(tools["review"], self._args(tools["review"], {k: v for k, v in values.items() if k != "ref"})))
            except (Refused, NoAnswer, DataError) as exc:           # nothing is placed by a review
                raise BrokerError(f"Robinhood's review refused it: {exc}") from exc
            problems = review.get("errors") or review.get("rejection_reason") if isinstance(review, dict) else None
            if problems or (isinstance(review, dict) and review.get("can_place") is False):
                raise BrokerError(f"Robinhood's review refused it: {str(problems or review.get('message') or 'not placeable')[:300]}")
            if isinstance(review, dict):
                values["review"] = review.get("review_id") or review.get("id") or review.get("confirmation_id")
        args = self._args(tools["place"], values)
        try:
            payload = self.bridge.call(tools["place"], args)
        except Refused as exc:
            raise BrokerError(str(exc)) from exc
        except NoAnswer as exc:
            raise BrokerTimeout(str(exc)) from exc
        except DataError as exc:                                     # anything else after sending: not known
            raise BrokerTimeout(str(exc)) from exc
        placed = _order_of(payload)
        if not placed or not (placed.get("id") or placed.get("order_id")):
            # It answered, but not with an order the desk can follow: the order may well be live. Find it by its reference.
            raise BrokerTimeout(f"Robinhood's answer to the placement could not be read ({str(unwrap(payload))[:120]})")
        return self._answer(placed, order)

    def status(self, order: dict[str, Any]) -> dict[str, Any]:
        tools = self.readiness()["tools"]
        one, many = tools.get("lookup"), tools.get("list")
        if not many:
            raise BrokerTimeout("no tool lists orders: the order cannot be reconciled")
        try:
            if order.get("broker_order_id") and one:
                return self._answer(_order_of(self.bridge.call(one, self._args(one, {"order_id": order["broker_order_id"]}))), order)
            rows = records(self.bridge.call(many, self._args(many, {"account": self.account, "symbol": order["terms"]["symbol"]})), "orders", "results")
        except Refused as exc:
            raise BrokerTimeout(f"the look-up was refused: {exc}") from exc
        except DataError as exc:
            raise BrokerTimeout(str(exc)) from exc
        want_ref, boid = ref_id(order["client_order_id"]), order.get("broker_order_id")
        match = next((r for r in rows if boid and str(r.get("id")) == boid), None) or \
            next((r for r in rows if str(r.get("ref_id") or r.get("client_order_id") or "") in (want_ref, order["client_order_id"])), None)
        if match is None and not any("ref_id" in r or "client_order_id" in r for r in rows):
            # The server does not echo the reference: match on the order itself, placed after the desk wrote it.
            t = order["terms"]
            since = datetime.fromisoformat(order["created_at"]) - timedelta(minutes=1)
            for r in rows:
                created = str(r.get("created_at") or "")
                try:
                    when = datetime.fromisoformat(created.replace("Z", "+00:00"))
                except ValueError:
                    continue
                if (str(r.get("symbol") or "").upper() == t["symbol"] and str(r.get("side") or "").lower() == t["side"]
                        and _num(r.get("quantity")) == float(t["quantity"]) and _num(r.get("price")) == float(t["limit_price"]) and when >= since):
                    match = r
                    break
        return self._answer(match, order)

    def cancel(self, order: dict[str, Any]) -> dict[str, Any]:
        tools = self.readiness()["tools"]
        if not order.get("broker_order_id"):
            return self.status(order)
        try:
            self.bridge.call(tools["cancel"], self._args(tools["cancel"], {"order_id": order["broker_order_id"], "account": self.account}))
        except Refused as exc:                                      # already filled or cancelled: see what it is
            return {**self.status(order), "message": f"cancel refused: {exc}"}
        except DataError as exc:
            raise BrokerTimeout(str(exc)) from exc
        return self.status(order)


def make_execution(settings: Any) -> RobinhoodExecution | None:
    """The live adapter, when ROBINHOOD_EXECUTION=1 and an account is named; None otherwise."""
    if not (settings.robinhood_execution and settings.robinhood_execution_account):
        return None
    from .providers.mcp_bridge import oauth_connector
    allowed = frozenset(n for names in EXEC_TOOLS.values() for n in names)
    bridge = TradingBridge(oauth_connector(settings.robinhood_url, settings.robinhood_signin_path, settings.robinhood_callback_port, False),
                           allowed)
    return RobinhoodExecution(bridge, settings.robinhood_execution_account)


__all__ = ["RobinhoodExecution", "TradingBridge", "EXEC_TOOLS", "make_execution", "ref_id", "mask", "account_ref", "acceptable_exec_name"]
