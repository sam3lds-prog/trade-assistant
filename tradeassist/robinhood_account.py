"""Your holdings and past orders through the Robinhood connection, read-only (2.6). Off unless switched on.

With ROBINHOOD_ACCOUNT_READ=1 in .env (and DATA_MODE=robinhood), the bridge may also call the account
tools listed in ACCOUNT_TOOLS below, and only those, by exact name: reads of holdings and of past orders.
Nothing can place, change, review or cancel an order: those tools are refused by name in the bridge
(providers/mcp_bridge.py) whatever this list says.

Robinhood does not document the arguments or reply shapes of these tools, and its connection may not
offer option orders at all (the account activity report does have them). So this module reads only what
the server says it offers, accepts the field names Robinhood's own API uses, and when a reply does not
look like that it says which fields it found (names only, never values) instead of guessing. Filled
orders become rows in the same shape as the report's, so the same rebuilding and analysis apply.

**Two sources, one history.** Where the report and the connection both cover a day, the report is used
(it has the fees); the connection's orders count only after the report's last day. tradehistory.activity
applies this.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from typing import Any

from .providers.base import DataError
from .providers.mcp_bridge import acceptable_account_tool_name
from .providers.robinhood import records, required, unwrap

# What may be read, by job, in the order tried. Exact names only.
ACCOUNT_TOOLS: dict[str, tuple[str, ...]] = {
    "share holdings": ("get_equity_positions", "get_portfolio"),
    "option holdings": ("get_option_positions",),
    "share orders": ("get_equity_orders",),
    "option orders": ("get_option_orders",),
}
ACCOUNT_ALLOWED = frozenset(n for names in ACCOUNT_TOOLS.values() for n in names)
assert all(acceptable_account_tool_name(n) for n in ACCOUNT_ALLOWED), "ACCOUNT_TOOLS holds a name that is not a plain read"
PAGE_ARGS = ("cursor", "page_token", "next", "after", "page_cursor")
MAX_PAGES = 50

SYMBOL_KEYS = ("symbol", "ticker", "chain_symbol", "instrument_symbol", "underlying_symbol")
QTY_KEYS = ("cumulative_quantity", "filled_quantity", "processed_quantity", "quantity", "shares", "qty")
PRICE_KEYS = ("average_price", "avg_price", "price", "executed_price")
TIME_KEYS = ("last_transaction_at", "updated_at", "executed_at", "filled_at", "created_at", "timestamp")
STRIKE_KEYS = ("strike_price", "strike")
EXPIRY_KEYS = ("expiration_date", "expiration", "expiry")
TYPE_KEYS = ("option_type", "call_put", "right", "type")


def _num(v: Any) -> float | None:
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if f == f and abs(f) != float("inf") else None


def _first(d: dict[str, Any], keys: tuple[str, ...]) -> Any:
    return next((d[k] for k in keys if k in d and d[k] not in (None, "")), None)


def shape(value: Any, depth: int = 0) -> Any:
    """The shape of a reply, keys and types only, never values: safe to put in a capture file to share."""
    if depth > 4:
        return "…"
    if isinstance(value, dict):
        return {k: shape(v, depth + 1) for k, v in list(value.items())[:40]}
    if isinstance(value, list):
        return [shape(value[0], depth + 1), f"({len(value)} items)"] if value else []
    return type(value).__name__


class RobinhoodAccount:
    """Reads holdings and past orders through a bridge built with account reads switched on."""

    def __init__(self, bridge: Any):
        self.bridge = bridge

    def offered(self) -> dict[str, str | None]:
        """For each job, the tool that does it if the server offers it and the bridge may call it."""
        tools = self.bridge.tools()
        out: dict[str, str | None] = {}
        for job, names in ACCOUNT_TOOLS.items():
            out[job] = next((n for n in names if n in tools and n in self.bridge.account_reads and self.bridge.may_call(n)), None)
        return out

    def _read_all(self, name: str) -> list[dict[str, Any]]:
        tool = self.bridge.tools()[name]
        missing = required(tool)
        if missing:
            raise DataError(f"Robinhood's {name} needs arguments this app does not fill ({', '.join(missing)}). "
                            "Run 'python run.py --robinhood-check' and send the capture file so it can be matched.")
        props = ((getattr(tool, "input_schema", None) or {}).get("properties") or {})
        page_arg = next((a for a in PAGE_ARGS if a in props), None)
        out: list[dict[str, Any]] = []
        args: dict[str, Any] = {}
        for _ in range(MAX_PAGES):
            payload = self.bridge.call(name, args)
            out.extend(records(payload, "orders", "positions", "results", "holdings", "items"))
            data = unwrap(payload)
            nxt = data.get("next") or data.get("next_cursor") or data.get("cursor") if isinstance(data, dict) else None
            if not (page_arg and isinstance(nxt, str) and nxt):
                break
            args = {page_arg: nxt}
        return out

    # --- holdings ----------------------------------------------------------------------------------
    def holdings(self) -> dict[str, Any]:
        offered = self.offered()
        items, problems = [], []
        if offered["share holdings"]:
            for rec in self._read_all(offered["share holdings"]):
                sym, qty = _first(rec, SYMBOL_KEYS), _num(_first(rec, ("quantity", "shares", "qty")))
                if not sym or qty is None:
                    problems.append(f"a share holding with fields {sorted(rec)[:20]}")
                    continue
                if abs(qty) < 1e-9:
                    continue
                items.append({"kind": "STOCK", "symbol": str(sym).upper(), "quantity": abs(qty), "side": "long" if qty > 0 else "short",
                              "average": _num(_first(rec, ("average_buy_price", "average_cost", "average_price", "avg_price")))})
        if offered["option holdings"]:
            for rec in self._read_all(offered["option holdings"]):
                leg = rec.get("option") if isinstance(rec.get("option"), dict) else rec
                sym = _first(rec, SYMBOL_KEYS) or _first(leg, SYMBOL_KEYS)
                strike, exp, typ = _num(_first(leg, STRIKE_KEYS)), _first(leg, EXPIRY_KEYS), str(_first(leg, ("option_type", "call_put", "right")) or "").upper()
                qty = _num(_first(rec, ("quantity", "qty")))
                if not sym or strike is None or not exp or typ not in ("CALL", "PUT") or qty is None:
                    problems.append(f"an option holding with fields {sorted(rec)[:20]}")
                    continue
                side = str(rec.get("type") or rec.get("position_type") or ("short" if qty < 0 else "long")).lower()
                items.append({"kind": "OPTION", "symbol": str(sym).upper(), "option_type": typ, "strike": strike,
                              "expiration": str(exp)[:10], "quantity": abs(qty), "side": "short" if side.startswith("short") else "long",
                              "average": _num(_first(rec, ("average_price", "average_open_price", "avg_price")))})
        return {"offered": offered, "holdings": items, "problems": problems}

    # --- past orders -> rows like the report's ------------------------------------------------------
    def order_rows(self) -> tuple[list[dict[str, Any]], list[dict[str, Any]], dict[str, str | None]]:
        """Filled orders as report-shaped rows, and the orders that could not be read (with field names)."""
        offered = self.offered()
        rows: list[dict[str, Any]] = []
        unreadable: list[dict[str, Any]] = []
        if offered["share orders"]:
            for rec in self._read_all(offered["share orders"]):
                if str(rec.get("state") or rec.get("status") or "filled").lower() not in ("filled", "partially_filled", "executed"):
                    continue
                try:
                    rows.append(_share_row(rec))
                except ValueError as exc:
                    unreadable.append({"what": "a share order", "reason": str(exc), "fields": sorted(rec)[:25]})
        if offered["option orders"]:
            for rec in self._read_all(offered["option orders"]):
                if str(rec.get("state") or rec.get("status") or "filled").lower() not in ("filled", "partially_filled", "executed"):
                    continue
                try:
                    rows.extend(_option_rows(rec))
                except ValueError as exc:
                    unreadable.append({"what": "an option order", "reason": str(exc), "fields": sorted(rec)[:25]})
        return rows, unreadable, offered


def _when(rec: dict[str, Any]) -> str:
    return _stamp(rec)[0]


def _stamp(rec: dict[str, Any]) -> tuple[str, str]:
    """(New York date, full timestamp) of an order's first fill."""
    execs = list(rec.get("executions") or []) if isinstance(rec.get("executions"), list) else []
    for leg in rec.get("legs") or []:                  # a multi-leg order's fills sit on its legs
        if isinstance(leg, dict) and isinstance(leg.get("executions"), list):
            execs.extend(leg["executions"])
    stamps = sorted(str(e.get("timestamp") or e.get("settlement_date")) for e in execs
                    if isinstance(e, dict) and (e.get("timestamp") or e.get("settlement_date")))
    t = next((s for s in stamps if s), None) or _first(rec, TIME_KEYS)
    if not t:
        raise ValueError("no time on the order")
    try:
        dt = datetime.fromisoformat(str(t).replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"an order time that is not a date ({str(t)[:30]})") from exc
    if dt.tzinfo is not None:
        from .optionmath import NY
        dt = dt.astimezone(NY)
    return dt.date().isoformat(), dt.isoformat()


def _filled(rec: dict[str, Any]) -> tuple[float, float]:
    """(quantity filled, average price) from executions when given, else the order's own fields."""
    execs = [e for e in (rec.get("executions") or []) if isinstance(e, dict)]
    q = [(_num(e.get("quantity")), _num(e.get("price"))) for e in execs]
    q = [(a, b) for a, b in q if a and b is not None]
    if q:
        total = sum(a for a, _ in q)
        return total, sum(a * b for a, b in q) / total
    qty, px = _num(_first(rec, QTY_KEYS)), _num(_first(rec, PRICE_KEYS))
    if not qty or px is None:
        raise ValueError("no filled quantity and price")
    return qty, px


def _money_text(v: float) -> str:
    return f"(${abs(v):,.2f})" if v < 0 else f"${v:,.2f}"


def _key(rec: dict[str, Any], suffix: str) -> str:
    ident = rec.get("id") or hashlib.sha256(json.dumps(rec, sort_keys=True, default=str).encode()).hexdigest()[:24]
    return f"rh:{ident}:{suffix}"


def _share_row(rec: dict[str, Any]) -> dict[str, Any]:
    sym = _first(rec, SYMBOL_KEYS)
    side = str(rec.get("side") or "").lower()
    if not sym or side not in ("buy", "sell"):
        raise ValueError("no ticker or no buy/sell side")
    qty, px = _filled(rec)
    fees = _num(rec.get("fees")) or 0.0
    amount = -(qty * px) - fees if side == "buy" else qty * px - fees
    day, ts = _stamp(rec)
    return {"row_key": _key(rec, "0"), "ts": ts, "activity_date": day, "code": "Buy" if side == "buy" else "Sell", "kind": "STOCK",
            "symbol": str(sym).upper(), "description": f"{str(sym).upper()} (Robinhood connection)", "quantity": qty,
            "price": px, "amount": amount,
            "raw": {"Activity Date": day, "Instrument": str(sym).upper(), "Description": "from the Robinhood connection",
                    "Trans Code": "Buy" if side == "buy" else "Sell", "Quantity": f"{qty:g}", "Price": f"${px:,.2f}",
                    "Amount": _money_text(amount), "Order": str(rec.get("id") or "")}}


def _option_rows(rec: dict[str, Any]) -> list[dict[str, Any]]:
    sym = _first(rec, SYMBOL_KEYS)
    legs = [leg for leg in (rec.get("legs") or []) if isinstance(leg, dict)]
    if not sym or not legs:
        raise ValueError("no underlying ticker or no legs")
    day, ts = _stamp(rec)
    out = []
    for i, leg in enumerate(legs):
        side, effect = str(leg.get("side") or "").lower(), str(leg.get("position_effect") or "").lower()
        strike, exp = _num(_first(leg, STRIKE_KEYS)), _first(leg, EXPIRY_KEYS)
        typ = str(_first(leg, ("option_type", "call_put", "right", "type")) or "").upper()
        if side not in ("buy", "sell") or effect not in ("open", "close"):
            raise ValueError("a leg without a buy/sell side and an open/close effect")
        if strike is None or not exp or typ not in ("CALL", "PUT"):
            raise ValueError("a leg that does not give its strike, expiration and call or put")
        if leg.get("executions"):
            qty, px = _filled(leg)
        elif len(legs) == 1:
            qty, px = _filled(rec)
        else:
            raise ValueError("a multi-leg order without each leg's own fill price")
        code = {("buy", "open"): "BTO", ("sell", "close"): "STC", ("sell", "open"): "STO", ("buy", "close"): "BTC"}[(side, effect)]
        amount = -(qty * px * 100) if side == "buy" else qty * px * 100
        e = str(exp)[:10]
        desc = f"{str(sym).upper()} {int(e[5:7])}/{int(e[8:10])}/{e[:4]} {typ.capitalize()} ${strike:,.2f}"
        out.append({"row_key": _key(rec, str(i)), "ts": f"{ts}#{i}", "activity_date": day, "code": code, "kind": "OPTION", "symbol": str(sym).upper(),
                    "option_type": typ, "strike": strike, "expiration": e, "description": desc, "quantity": qty, "price": px,
                    "amount": amount,
                    "raw": {"Activity Date": day, "Instrument": str(sym).upper(), "Description": desc + " (Robinhood connection)",
                            "Trans Code": code, "Quantity": f"{qty:g}", "Price": f"${px:,.2f}", "Amount": _money_text(amount),
                            "Order": str(rec.get("id") or "")}})
    return out


def store_pulled(db: Any, rows: list[dict[str, Any]], unreadable: list[dict[str, Any]], offered: dict[str, Any]) -> dict[str, Any]:
    """Keep the pulled rows (each once, by order id) as an import from the connection."""
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    dates = sorted(r["activity_date"] for r in rows)
    # Newest first, as the report lists them, so one day's rows replay in the order they filled.
    rows = sorted(rows, key=lambda r: r.get("ts") or r["activity_date"], reverse=True)
    with db.tx() as cur:
        cur.execute("INSERT INTO broker_imports (source, filename, imported_at, rows_read) VALUES ('robinhood_link', ?, ?, ?)",
                    ("Robinhood connection", now, len(rows) + len(unreadable)))
        import_id = cur.lastrowid
        added = dup = 0
        for n, r in enumerate(rows):
            cur.execute("""INSERT OR IGNORE INTO broker_activity (import_id, source, row_key, file_order, activity_date, code,
                           symbol, description, kind, option_type, strike, expiration, quantity, short_flag, price, amount, raw_json)
                           VALUES (?, 'robinhood_link', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, ?, ?, ?)""",
                        (import_id, r["row_key"], n, r["activity_date"], r["code"], r["symbol"], r["description"], r["kind"],
                         r.get("option_type"), r.get("strike"), r.get("expiration"), r["quantity"], r["price"], r["amount"],
                         json.dumps(r["raw"], sort_keys=True)))
            if cur.rowcount:
                added += 1
            else:
                dup += 1
        summary = {"offered": offered, "unreadable": unreadable[:50], "unreadable_total": len(unreadable), "ignored": [],
                   "unmodelled": [], "unmodelled_total": 0}
        cur.execute("""UPDATE broker_imports SET rows_added = ?, rows_duplicate = ?, rows_unreadable = ?, first_date = ?,
                       last_date = ?, summary_json = ? WHERE id = ?""",
                    (added, dup, len(unreadable), dates[0] if dates else None, dates[-1] if dates else None,
                     json.dumps(summary), import_id))
    return {"import_id": import_id, "source": "robinhood_link", "filename": "Robinhood connection", "rows_read": len(rows),
            "rows_added": added, "rows_duplicate": dup, "rows_ignored": 0, "rows_unreadable": len(unreadable),
            "first_date": dates[0] if dates else None, "last_date": dates[-1] if dates else None, **summary}


__all__ = ["RobinhoodAccount", "ACCOUNT_TOOLS", "ACCOUNT_ALLOWED", "shape", "store_pulled"]
