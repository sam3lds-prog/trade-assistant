"""2.6: reading your holdings and past orders through the Robinhood connection, only when switched on.

The stand-in server below offers the account reads under the names publicly reported for Robinhood's
connection, with replies in the field names Robinhood's own API uses (side, position_effect, executions,
chain_symbol, strike_price, expiration_date). Passing these shows the guard and the mapping work; it does
not show the real server replies this way, which is why the check command records the reply shapes.
"""

from __future__ import annotations

from datetime import date

import pytest
from mcp import Client, types
from mcp.server.mcpserver import MCPServer

from tradeassist.db import Database
from tradeassist.providers.mcp_bridge import ReadOnlyBridge, acceptable_account_tool_name
from tradeassist.providers.robinhood import ALLOWED_TOOLS
from tradeassist.robinhood_account import ACCOUNT_ALLOWED, RobinhoodAccount, shape, store_pulled
from tradeassist.tradeanalysis import rebuild, summarize
from tradeassist.tradehistory import activity, parse_report, store

READ_ONLY = types.ToolAnnotations(read_only_hint=True)


def standin(option_orders: bool = True, destructive_option_orders: bool = False, paged: bool = False):
    srv, called = MCPServer("robinhood-account-standin"), []

    def wrap(data):
        return {"data": data, "guide": "Prices are text."}

    @srv.tool()
    def place_equity_order(symbol: str, quantity: float, side: str) -> dict:
        """Place an order."""
        called.append("place_equity_order")
        return wrap({"id": "x"})

    @srv.tool()
    def review_equity_order(symbol: str, quantity: float, side: str) -> dict:
        """Simulate an order."""
        called.append("review_equity_order")
        return wrap({})

    @srv.tool(annotations=READ_ONLY)
    def get_accounts() -> dict:
        """List accounts, with numbers and balances."""
        called.append("get_accounts")
        return wrap({"results": [{"account_number": "5QR12345", "buying_power": "1000.00"}]})

    @srv.tool(annotations=READ_ONLY)
    def get_equity_positions() -> dict:
        """Equity positions."""
        called.append("get_equity_positions")
        return wrap({"results": [{"symbol": "AAPL", "quantity": "10.00000000", "average_buy_price": "180.0000"},
                                 {"symbol": "KO", "quantity": "0.00000000", "average_buy_price": "0"}]})

    @srv.tool(annotations=READ_ONLY)
    def get_equity_orders(cursor: str | None = None) -> dict:
        """Equity order history."""
        called.append("get_equity_orders")
        first = [{"id": "o-1", "symbol": "AAPL", "side": "buy", "state": "filled", "quantity": "10", "fees": "0.00",
                  "executions": [{"price": "180.00", "quantity": "10", "timestamp": "2026-01-05T15:31:00Z"}]},
                 {"id": "o-2", "symbol": "AAPL", "side": "sell", "state": "cancelled", "quantity": "10"}]
        second = [{"id": "o-3", "symbol": "MSFT", "side": "buy", "state": "filled", "cumulative_quantity": "2",
                   "average_price": "400.00", "last_transaction_at": "2026-01-06T16:00:00Z"},
                  {"id": "o-4", "symbol": "MSFT", "side": "sell", "state": "filled", "cumulative_quantity": "2",
                   "average_price": "410.50", "fees": "0.02", "last_transaction_at": "2026-01-09T18:00:00Z"}]
        if paged:
            return wrap({"results": second, "next": None} if cursor else {"results": first, "next": "page-2"})
        return wrap({"results": first + second})

    if option_orders:
        ann = types.ToolAnnotations(destructive_hint=True) if destructive_option_orders else READ_ONLY

        @srv.tool(annotations=ann)
        def get_option_orders() -> dict:
            """Option order history."""
            called.append("get_option_orders")
            leg = lambda side, effect, strike, px, when: {   # noqa: E731
                "side": side, "position_effect": effect, "option_type": "call", "strike_price": f"{strike:.4f}",
                "expiration_date": "2026-02-20", "executions": [{"price": f"{px:.2f}", "quantity": "1", "timestamp": when}]}
            return wrap({"results": [
                {"id": "p-1", "chain_symbol": "AAPL", "state": "filled", "legs": [leg("buy", "open", 190, 5.0, "2026-01-07T15:00:00Z"),
                                                                                  leg("sell", "open", 200, 2.0, "2026-01-07T15:00:00Z")]},
                {"id": "p-2", "chain_symbol": "AAPL", "state": "filled", "legs": [leg("sell", "close", 190, 7.5, "2026-01-20T15:00:00Z"),
                                                                                  leg("buy", "close", 200, 3.0, "2026-01-20T15:00:00Z")]},
                {"id": "p-3", "chain_symbol": "TSLA", "state": "filled",
                 "legs": [{"side": "buy", "position_effect": "open", "option": "https://api.robinhood.com/options/instruments/x/"}],
                 "price": "3.00", "quantity": "1", "updated_at": "2026-01-08T15:00:00Z"}]})

    return srv, called


def bridge_for(srv, account=True):
    return ReadOnlyBridge(lambda: Client(srv), ALLOWED_TOOLS, label="Robinhood",
                          account_reads=ACCOUNT_ALLOWED if account else frozenset())


# --- the guard ---------------------------------------------------------------------------------

def test_the_account_list_takes_plain_reads_only():
    for ok in ("get_equity_positions", "get_portfolio", "get_option_positions", "get_equity_orders", "get_option_orders"):
        assert acceptable_account_tool_name(ok), ok
    for bad in ("place_equity_order", "review_equity_order", "cancel_equity_order", "get_order_status", "get_orders_and_cancel",
                "get_equity_orders_place", "get_and_place_order", "get_Equity_Orders", "get_option_order", "get_transfer_history",
                "get_buy_orders_and_sell", "update_watchlist", "get_close_positions"):
        assert not acceptable_account_tool_name(bad), bad
    with pytest.raises(ValueError):
        ReadOnlyBridge(lambda: None, ALLOWED_TOOLS, account_reads=frozenset({"place_equity_order"}))


def test_with_account_reads_off_no_account_tool_is_ever_called():
    srv, called = standin()
    b = bridge_for(srv, account=False)
    try:
        for name in ("get_equity_orders", "get_equity_positions", "place_equity_order"):
            with pytest.raises(PermissionError):
                b.call(name, {})
        assert called == []
    finally:
        b.close()


def test_with_account_reads_on_orders_are_still_never_placed_or_reviewed():
    srv, called = standin()
    b = bridge_for(srv)
    try:
        for name in ("place_equity_order", "review_equity_order", "get_accounts"):
            with pytest.raises(PermissionError):
                b.call(name, {"symbol": "AAPL", "quantity": 1, "side": "buy"} if "order" in name else {})
        assert called == []
    finally:
        b.close()


def test_a_listed_read_the_server_marks_destructive_is_refused():
    srv, called = standin(destructive_option_orders=True)
    b = bridge_for(srv)
    try:
        assert RobinhoodAccount(b).offered()["option orders"] is None
        with pytest.raises(PermissionError):
            b.call("get_option_orders", {})
        assert "get_option_orders" not in called
    finally:
        b.close()


# --- reading -------------------------------------------------------------------------------------

def test_filled_orders_become_report_rows_and_rebuild_into_trades(tmp_path):
    srv, called = standin(paged=True)
    b = bridge_for(srv)
    db = Database(tmp_path / "t.db")
    try:
        rows, unreadable, offered = RobinhoodAccount(b).order_rows()
        assert offered == {"share holdings": "get_equity_positions", "option holdings": None,
                           "share orders": "get_equity_orders", "option orders": "get_option_orders"}
        assert called.count("get_equity_orders") == 2                     # followed the cursor to the second page
        assert sorted(r["code"] for r in rows) == ["BTC", "BTO", "Buy", "Buy", "STC", "STO", "Sell"]
        assert [u["what"] for u in unreadable] == ["an option order"]     # a leg without strike and expiration
        assert "strike" in unreadable[0]["reason"] and "price" in unreadable[0]["fields"]
        out = store_pulled(db, rows, unreadable, offered)
        assert out["rows_added"] == 7 and out["rows_unreadable"] == 1
        again = store_pulled(db, rows, unreadable, offered)
        assert again["rows_added"] == 0 and again["rows_duplicate"] == 7     # the same orders are kept once
        trades = {t["symbol"] + " " + t["strategy"]: t for t in summarize(rebuild(activity(db)), date(2026, 2, 1))}
        assert trades["MSFT Shares (long)"]["realized_pnl"] == pytest.approx(2 * 410.5 - 0.02 - 800)
        spread = trades["AAPL Call debit spread"]
        assert spread["realized_pnl"] == pytest.approx((750 - 300) - (500 - 200))
        assert trades["AAPL Shares (long)"]["status"] == "OPEN"
    finally:
        b.close()
        db.close()


def test_holdings_are_read_and_empty_ones_dropped():
    srv, _ = standin()
    b = bridge_for(srv)
    try:
        h = RobinhoodAccount(b).holdings()
        assert h["holdings"] == [{"kind": "STOCK", "symbol": "AAPL", "quantity": 10.0, "side": "long", "average": 180.0}]
    finally:
        b.close()


def test_the_report_wins_on_the_days_it_covers_so_nothing_is_counted_twice(tmp_path):
    db = Database(tmp_path / "t.db")
    try:
        text = ('"Activity Date","Process Date","Settle Date","Instrument","Description","Trans Code","Quantity","Price","Amount"\n'
                '"1/5/2026","1/5/2026","1/6/2026","AAPL","Apple","Buy","10","$180.00","($1,800.00)"\n')
        store(db, parse_report(text), "robinhood_csv", "a.csv")
        srv, _ = standin(option_orders=False)
        b = bridge_for(srv)
        rows, unreadable, offered = RobinhoodAccount(b).order_rows()
        b.close()
        store_pulled(db, rows, unreadable, offered)
        skipped: list[int] = []
        kept = activity(db, skipped)
        # The report covers up to Jan 5: the connection's Jan 5 buy is left out; its later MSFT orders count.
        assert [(r["source"], r["code"], r["symbol"]) for r in kept] == [
            ("robinhood_csv", "Buy", "AAPL"), ("robinhood_link", "Buy", "MSFT"), ("robinhood_link", "Sell", "MSFT")]
        assert len(skipped) == 1
    finally:
        db.close()


def test_the_check_records_reply_shapes_never_values():
    payload = {"data": {"results": [{"account_number": "5QR12345", "symbol": "AAPL", "quantity": "10"}], "next": None}}
    s = shape(payload)
    text = str(s)
    assert "5QR12345" not in text and "AAPL" not in text and "account_number" in text and "str" in text


def test_pull_says_plainly_when_it_is_switched_off(tmp_path):
    from fastapi.testclient import TestClient

    from tradeassist.api import create_app
    from tradeassist.config import Settings
    from tradeassist.providers.demo import DemoData
    from tradeassist.service import TradeAssistant
    settings = Settings(data_mode="demo", db_path=tmp_path / "t.db")
    svc = TradeAssistant(settings, provider=DemoData(), db=Database(tmp_path / "t.db"))
    with TestClient(create_app(settings, svc)) as c:
        r = c.post("/v2/history/pull")
        assert r.status_code == 400 and "ROBINHOOD_ACCOUNT_READ=1" in r.text
        assert c.get("/v2/history/link").json() == {"enabled": False, "using_robinhood": False, "offered": None}
    svc.db.close()
    on = Settings(data_mode="demo", db_path=tmp_path / "u.db", robinhood_account_read=True)
    svc = TradeAssistant(on, provider=DemoData(), db=Database(tmp_path / "u.db"))
    with TestClient(create_app(on, svc)) as c:
        r = c.post("/v2/history/pull")
        assert r.status_code == 400 and "not using the Robinhood connection" in r.text
    svc.db.close()


def test_an_order_without_a_time_or_side_is_reported_not_guessed():
    from tradeassist.robinhood_account import _option_rows, _share_row
    with pytest.raises(ValueError, match="buy/sell"):
        _share_row({"id": "x", "symbol": "AAPL", "side": "", "quantity": "1", "average_price": "1"})
    with pytest.raises(ValueError, match="no time"):
        _share_row({"id": "x", "symbol": "AAPL", "side": "buy", "quantity": "1", "average_price": "1"})
    with pytest.raises(ValueError, match="multi-leg"):
        _option_rows({"id": "y", "chain_symbol": "AAPL", "updated_at": "2026-01-02T15:00:00Z", "price": "1", "quantity": "1",
                      "legs": [{"side": "buy", "position_effect": "open", "option_type": "call", "strike_price": "1", "expiration_date": "2026-02-20"},
                               {"side": "sell", "position_effect": "open", "option_type": "call", "strike_price": "2", "expiration_date": "2026-02-20"}]})
