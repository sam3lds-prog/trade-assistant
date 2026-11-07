"""Where the Trading Desk's orders go (3.0 phase 5). A broker answers three questions in one shape:

    place(order)   send it; returns {state, broker_order_id, fills, message}
    status(order)  what became of it; same shape. For an order whose submission was never confirmed, a broker
                   that has no record of its client order id answers state "not_found"; with "conclusive" it is
                   sure the order was never placed, otherwise the desk asks again before it believes it.
    cancel(order)  ask to cancel; same shape

`order` is the desk's `broker_orders` row as a dict (terms, client_order_id, broker_order_id, created_at, ...). Fills
are cumulative: every fill the broker knows of, each with an id, so applying them twice changes nothing.

Phase 5 has the **paper broker**: it fills against the live quote, never better than the market and never beyond
the limit (a buy at the ask when the ask is at or below the limit, a sell at the bid when the bid is at or above
it), and a day order it could not fill expires at the close. Phase 6 adds the Robinhood adapter behind the same
three calls.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Protocol

from .optionmath import NY
from .providers.base import DataError


class BrokerError(RuntimeError):
    """The broker refused or failed in a way that is known: nothing was placed."""


class BrokerTimeout(RuntimeError):
    """The request may or may not have reached the broker: the order's state is unknown until reconciled."""


class Broker(Protocol):
    name: str

    def place(self, order: dict[str, Any]) -> dict[str, Any]: ...

    def status(self, order: dict[str, Any]) -> dict[str, Any]: ...

    def cancel(self, order: dict[str, Any]) -> dict[str, Any]: ...


BUYS = ("buy", "buy_to_open", "buy_to_cover", "buy_to_close")


class PaperBroker:
    """Simulated fills against the quote. It keeps no state of its own: the desk's order row is the record."""
    name = "paper"

    def __init__(self, svc: Any):
        self.svc = svc

    def _try_fill(self, order: dict[str, Any]) -> dict[str, Any]:
        t = order["terms"]
        if order.get("state") in ("filled", "cancelled", "expired", "rejected"):
            return {"state": order["state"], "broker_order_id": order.get("broker_order_id"), "fills": order.get("fills") or []}
        boid = order.get("broker_order_id") or f"paper-{order['client_order_id']}"
        now = datetime.now(NY)
        created = datetime.fromisoformat(order["created_at"]).astimezone(NY)
        if t.get("time_in_force", "day") == "day" and (now.date() > created.date() or now.hour >= 16):
            return {"state": "expired", "broker_order_id": boid, "fills": [], "message": "a day order not filled by the close"}
        try:
            sym = t["option"]["option_symbol"] if t.get("option") else t["symbol"]
            q = self.svc.market.quote(t["symbol"]) if not t.get("option") else self._option_quote(t)
        except (DataError, ValueError) as exc:
            return {"state": order.get("state") if order.get("state") in ("submitted", "accepted") else "submitted",
                    "broker_order_id": boid, "fills": [], "message": f"no quote for {t['symbol']}: {exc}"}
        bid, ask, limit = q.get("bid"), q.get("ask"), t.get("limit_price")
        price = None
        if t["side"] in BUYS and ask and ask > 0 and (limit is None or ask <= limit + 1e-9):
            price = float(ask)
        elif t["side"] not in BUYS and bid and bid > 0 and (limit is None or bid >= limit - 1e-9):
            price = float(bid)
        if price is None:
            return {"state": "submitted", "broker_order_id": boid, "fills": [],
                    "message": f"working: limit {limit} against bid {bid} / ask {ask} for {sym}"}
        fill = {"at": datetime.now(NY).isoformat(timespec="seconds"), "quantity": float(t["quantity"]), "price": round(price, 4),
                "fees": 0.0, "external_id": f"{boid}-1"}
        return {"state": "filled", "broker_order_id": boid, "fills": [fill], "message": f"filled at {price:.2f} (paper)"}

    def _option_quote(self, t: dict[str, Any]) -> dict[str, Any]:
        o = t["option"]
        chain = self.svc.market.chain(o["underlying"], o["expiration"])
        row = next((c for c in chain.contracts if c.get("symbol") == o["option_symbol"]), None)
        if row is None:
            raise DataError(f"{o['option_symbol']} is not in the chain")
        return row

    def place(self, order: dict[str, Any]) -> dict[str, Any]:
        return self._try_fill({**order, "state": "submitting"})

    def status(self, order: dict[str, Any]) -> dict[str, Any]:
        if order.get("state") in ("intent", "submitting", "unknown") and not order.get("broker_order_id"):
            # The paper broker places in the same step it records; a crash before that means it was never placed.
            return {"state": "not_found", "conclusive": True, "broker_order_id": None, "fills": [], "message": "never reached the paper broker"}
        return self._try_fill(order)

    def cancel(self, order: dict[str, Any]) -> dict[str, Any]:
        if order.get("state") in ("filled", "cancelled", "expired", "rejected"):
            return {"state": order["state"], "broker_order_id": order.get("broker_order_id"), "fills": order.get("fills") or []}
        return {"state": "cancelled", "broker_order_id": order.get("broker_order_id"), "fills": order.get("fills") or [],
                "message": "cancelled (paper)"}


__all__ = ["Broker", "BrokerError", "BrokerTimeout", "PaperBroker", "BUYS"]
