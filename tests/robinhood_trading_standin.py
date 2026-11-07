"""A stand-in for the order tools of Robinhood's Trading MCP server, for the live adapter's tests (3.0 phase 6).

Robinhood does not publish these tools' arguments or replies. This server follows the shapes its own API uses
(an order with id, ref_id, state, cumulative_quantity, average_price and executions), declares its arguments as a
real server would, and can misbehave on request: refuse in review, never answer a place call, or answer after
placing. It also offers tools the adapter must never call. Passing these tests shows the adapter's logic; it does
not show that the real server matches.
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import datetime, timezone

from mcp import types
from mcp.server.mcpserver import MCPServer

READ_ONLY = types.ToolAnnotations(read_only_hint=True)
ACCOUNT = "5QR99887766"


class TradingStandin:
    def __init__(self):
        self.server = MCPServer("robinhood-trading-orders-standin")
        self.orders: dict[str, dict] = {}
        self.calls: list[tuple[str, dict]] = []
        self.forbidden_calls: list[str] = []
        self.review_refuses = False
        self.place_mode = "queue"          # queue | fill | hang | error | hang_after_place
        self.ask = 29.0
        self._register()

    def _order(self, **kw) -> dict:
        oid = str(uuid.uuid4())
        o = {"id": oid, "state": "queued", "cumulative_quantity": "0.00000000", "average_price": None, "executions": [],
             "created_at": datetime.now(timezone.utc).isoformat(), "updated_at": datetime.now(timezone.utc).isoformat(), **kw}
        self.orders[oid] = o
        return o

    def fill(self, oid: str, quantity: float | None = None, price: float | None = None) -> None:
        o = self.orders[oid]
        q = float(quantity or o["quantity"])
        p = float(price or o["price"])
        o["executions"].append({"id": str(uuid.uuid4()), "price": f"{p:.4f}", "quantity": f"{q:.8f}",
                                "timestamp": datetime.now(timezone.utc).isoformat()})
        done = sum(float(e["quantity"]) for e in o["executions"])
        o["cumulative_quantity"] = f"{done:.8f}"
        o["average_price"] = f"{sum(float(e['quantity']) * float(e['price']) for e in o['executions']) / done:.4f}"
        o["state"] = "filled" if done >= float(o["quantity"]) - 1e-9 else "partially_filled"

    def _register(self) -> None:
        srv = self.server

        @srv.tool(annotations=READ_ONLY)
        def get_accounts() -> dict:
            """Accounts this sign-in may use."""
            self.calls.append(("get_accounts", {}))
            return {"data": {"results": [{"account_number": ACCOUNT, "type": "cash", "agentic_allowed": True},
                                         {"account_number": "9ZZ11112222", "type": "margin", "agentic_allowed": False}]}}

        @srv.tool()
        def review_equity_order(account_number: str, symbol: str, side: str, quantity: float, type: str, price: float,
                                time_in_force: str) -> dict:
            """Check an equity order without placing it."""
            self.calls.append(("review_equity_order", {"symbol": symbol, "price": price}))
            if self.review_refuses:
                return {"data": {"can_place": False, "errors": ["Insufficient buying power"]}}
            return {"data": {"review_id": f"rv-{uuid.uuid4().hex[:8]}", "can_place": True, "estimated_total": quantity * price}}

        @srv.tool()
        async def place_equity_order(account_number: str, symbol: str, side: str, quantity: float, type: str, price: float,
                                     time_in_force: str, ref_id: str, review_id: str | None = None) -> dict:
            """Place an equity order. ref_id makes a repeat return the first order."""
            self.calls.append(("place_equity_order", {"symbol": symbol, "ref_id": ref_id, "account_number": account_number}))
            if self.place_mode == "error":
                raise ValueError("Order rejected: market closed")
            if self.place_mode == "hang":
                await asyncio.sleep(30)
            same = next((o for o in self.orders.values() if o["ref_id"] == ref_id), None)
            o = same or self._order(account_number=account_number, symbol=symbol, side=side, quantity=str(quantity), type=type,
                                    price=str(price), time_in_force=time_in_force, ref_id=ref_id)
            if self.place_mode == "fill" and not same:
                self.fill(o["id"], price=min(float(price), self.ask))
            if self.place_mode == "hang_after_place":
                await asyncio.sleep(30)
            return {"data": o}

        @srv.tool(annotations=READ_ONLY)
        def get_equity_orders(account_number: str, symbol: str | None = None) -> dict:
            """Recent equity orders."""
            self.calls.append(("get_equity_orders", {"symbol": symbol}))
            rows = [o for o in self.orders.values() if o["account_number"] == account_number and (not symbol or o["symbol"] == symbol)]
            return {"data": {"results": rows}}

        @srv.tool(annotations=READ_ONLY)
        def get_equity_order(order_id: str) -> dict:
            """One equity order."""
            self.calls.append(("get_equity_order", {"order_id": order_id}))
            if order_id not in self.orders:
                raise ValueError("Not found")
            return {"data": self.orders[order_id]}

        @srv.tool()
        def cancel_equity_order(order_id: str) -> dict:
            """Cancel an equity order."""
            self.calls.append(("cancel_equity_order", {"order_id": order_id}))
            o = self.orders[order_id]
            if o["state"] in ("filled", "cancelled"):
                raise ValueError(f"Order is {o['state']}")
            o["state"] = "cancelled"
            return {"data": o}

        # Tools the adapter must never call.
        @srv.tool()
        def place_option_order(option_id: str, quantity: int, price: float) -> dict:
            """Place an option order."""
            self.forbidden_calls.append("place_option_order")
            return {"data": {}}

        @srv.tool()
        def transfer_funds(amount: float) -> dict:
            """Move money."""
            self.forbidden_calls.append("transfer_funds")
            return {"data": {}}

        @srv.tool(annotations=types.ToolAnnotations(destructive_hint=True))
        def get_equity_orders_and_cancel() -> dict:
            """A read-sounding name the server marks destructive."""
            self.forbidden_calls.append("get_equity_orders_and_cancel")
            return {"data": {}}
