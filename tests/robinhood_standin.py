"""Stand-ins for Robinhood's Trading MCP server, for tests.

Robinhood's real server could not be reached while this was written, and it does not
publish its reply formats. These servers follow what is publicly reported about it:
tool names (get_equity_quotes, get_option_chains, get_option_instruments,
get_option_quotes), replies wrapped as {"data": ..., "guide": ...}, numbers sent as
text, and field names such as bid_price and ask_price. Variant "b" deliberately uses
different argument names and reply layouts, to test that the app adapts to what a
server declares instead of assuming one layout.

Passing these tests shows the plumbing and the mapping logic work. It does not show
that the real server matches either variant.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from zoneinfo import ZoneInfo
from typing import Annotated, Literal

from mcp import types
from mcp.server.mcpserver import MCPServer
from pydantic import Field

from tradeassist.providers.demo import DemoData

READ_ONLY = types.ToolAnnotations(read_only_hint=True)

# Argument layouts as Robinhood's server declared them on 2026-10-06 (from a --robinhood-check capture).
OBSERVED_SCHEMAS = {
    "get_equity_quotes": {"type": "object", "properties": {"symbols": {"type": ["null", "array"], "items": {"type": "string"}}},
                          "required": ["symbols"], "additionalProperties": False},
    "get_option_chains": {"type": "object", "properties": {"ids": {"type": "string"}, "underlying_symbol": {"type": "string"}},
                          "additionalProperties": False},
    "get_option_instruments": {"type": "object", "properties": {
        "chain_id": {"type": "string"}, "chain_symbol": {"type": "string"}, "expiration_dates": {"type": "string"},
        "strike_price": {"type": "string"}, "type": {"type": "string"}, "state": {"type": "string"},
        "tradability": {"type": "string"}, "ids": {"type": "string"}, "cursor": {"type": "string"}},
        "additionalProperties": False},
    "get_option_quotes": {"type": "object", "properties": {"instrument_ids": {"type": ["null", "array"], "items": {"type": "string"}}},
                          "required": ["instrument_ids"], "additionalProperties": False},
    "get_equity_historicals": {"type": "object", "properties": {
        "symbols": {"type": ["null", "array"], "items": {"type": "string"}}, "start_time": {"type": "string"},
        "end_time": {"type": "string"}, "interval": {"type": "string"}, "bounds": {"type": "string"},
        "adjustment_type": {"type": "string"}}, "required": ["symbols", "start_time"], "additionalProperties": False},
    "get_earnings_results": {"type": "object", "properties": {"symbol": {"type": "string"}}, "required": ["symbol"],
                             "additionalProperties": False},
}
RFC3339 = r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d+)?(Z|[+-]\d{2}:\d{2})"
STAMP = "2026-10-05T19:59:58.123456Z"


def money(value) -> str | None:
    return None if value is None else f"{value:.6f}"


def ident(occ: str) -> str:
    h = hashlib.sha256(occ.encode()).hexdigest()
    return f"{h[:8]}-{h[8:12]}-{h[12:16]}-{h[16:20]}-{h[20:32]}"


class Standin:
    def __init__(self, variant: str = "a"):
        self.demo = DemoData()
        self.variant = variant
        self.calls: list[tuple[str, dict]] = []
        self.forbidden_calls: list[str] = []
        self.by_id: dict[str, dict] = {}
        self.bar_stamp = None          # tests may set a function day -> timestamp to mis-stamp daily bars
        self.server = MCPServer("robinhood-trading-standin")
        self._register_common()
        {"a": self._register_a, "b": self._register_b, "c": self._register_c}[variant]()

    def use_observed_schemas(self, bridge) -> None:
        """Give the client the argument layouts Robinhood's server really declares (variant c)."""
        bridge._tools = {name: (tool.model_copy(update={"input_schema": OBSERVED_SCHEMAS[name]})
                                if name in OBSERVED_SCHEMAS else tool)
                         for name, tool in bridge.tools().items()}

    def wrap(self, data):
        return {"data": data, "guide": "Prices are text. Unwrap data first."}

    def contracts(self, symbol: str, expiration: str) -> list[dict]:
        out = self.demo.chain(symbol, expiration)
        for c in out:
            self.by_id[ident(c["symbol"])] = c
        return out

    def option_quote(self, contract_id: str, nested_greeks: bool, with_id: bool, as_text: bool) -> dict:
        c = self.by_id[contract_id]
        g = c["greeks"]
        conv = money if as_text else (lambda v: v)
        greeks = {"delta": conv(g["delta"]), "gamma": conv(g["gamma"]), "theta": conv(g["theta"]),
                  "vega": conv(g["vega"]), "rho": conv(0.01)}
        row = {"bid_price": conv(c["bid"]), "ask_price": conv(c["ask"]), "bid_size": int(c["bidsize"]),
               "ask_size": int(c["asksize"]), "mark_price": conv((c["bid"] + c["ask"]) / 2),
               "last_trade_price": conv(c["last"]), "volume": int(c["volume"]),
               "open_interest": int(c["open_interest"]), "implied_volatility": conv(g["mid_iv"]),
               "updated_at": STAMP}
        if with_id:
            row["instrument_id"] = contract_id
        if nested_greeks:
            row["greeks"] = greeks
        else:
            row.update(greeks)
        return row

    # --- tools that must never be called -------------------------------------
    def _register_common(self) -> None:
        srv = self.server

        @srv.tool()
        def place_option_order(option_id: str, quantity: int, price: float) -> dict:
            """Place a single-leg option order."""
            self.forbidden_calls.append("place_option_order")
            return self.wrap({"order": {"id": "x", "state": "queued"}})

        @srv.tool()
        def cancel_equity_order(order_id: str) -> dict:
            """Cancel an equity order."""
            self.forbidden_calls.append("cancel_equity_order")
            return self.wrap({})

        @srv.tool(annotations=READ_ONLY)
        def get_accounts() -> dict:
            """List accounts."""
            self.forbidden_calls.append("get_accounts")
            return self.wrap({"results": [{"account_number": "123456789", "agentic_allowed": True}]})

        @srv.tool(annotations=READ_ONLY)
        def get_option_positions() -> dict:
            """List option positions."""
            self.forbidden_calls.append("get_option_positions")
            return self.wrap({"results": []})

        @srv.tool(annotations=types.ToolAnnotations(destructive_hint=True))
        def get_and_reset_alerts() -> dict:
            """A read-sounding name on a tool the server marks as destructive."""
            self.forbidden_calls.append("get_and_reset_alerts")
            return self.wrap({})

        # Names that begin like a read, carry no warning from the server, and are not market data.
        # They resemble the tools the app wants, to prove it never settles for a look-alike.
        def lookalike(name: str):
            def tool(symbols: list[str] | None = None, symbol: str | None = None, ids: list[str] | None = None) -> dict:
                self.forbidden_calls.append(name)
                return self.wrap({"results": [{"account_number": "5QR12345", "owner": "Sam Example"}]})
            tool.__name__ = name
            tool.__doc__ = f"Look-alike tool {name}."
            return tool
        for name in ("get_equity_quote_and_buy", "get_equity_holdings_quotes", "get_holdings_historicals",
                     "get_balance_historicals", "get_option_chain_for_holdings", "get_option_instruments_held",
                     "get_my_option_quotes", "get_PlaceOrder", "get_buying_power", "get_and_submit_trade"):
            srv.tool()(lookalike(name))

    # --- variant a: the layout public reports describe ---------------------------
    def _register_a(self) -> None:
        srv, demo = self.server, self.demo

        @srv.tool(annotations=READ_ONLY)
        def get_equity_quotes(symbols: Annotated[list[str], Field(max_length=20)]) -> dict:
            """Get real-time equity quotes and prior close for up to 20 symbols."""
            self.calls.append(("get_equity_quotes", {"symbols": symbols}))
            rows = []
            for symbol, q in demo.quotes(symbols).items():
                rows.append({"symbol": symbol, "last_trade_price": money(q["last"]), "bid_price": money(q["bid"]),
                             "ask_price": money(q["ask"]), "bid_size": 300, "ask_size": 200,
                             "previous_close": money(q["prevclose"]), "updated_at": STAMP})
            return self.wrap({"quotes": rows})

        @srv.tool(annotations=READ_ONLY)
        def get_option_chains(symbol: str) -> dict:
            """Get option chains (expirations) for an underlying."""
            self.calls.append(("get_option_chains", {"symbol": symbol}))
            dates = demo.expirations(symbol)
            return self.wrap({"chains": [
                {"id": "adjusted-chain", "symbol": symbol + "1", "expiration_dates": dates[:2],
                 "trade_value_multiplier": "150.0000"},
                {"id": "standard-chain", "symbol": symbol, "expiration_dates": dates,
                 "trade_value_multiplier": "100.0000"}]})

        @srv.tool(annotations=READ_ONLY)
        def get_option_instruments(chain_symbol: str, expiration_dates: str, state: str = "active") -> str:
            """Get option contracts for an underlying and expiration."""
            self.calls.append(("get_option_instruments", {"chain_symbol": chain_symbol,
                                                          "expiration_dates": expiration_dates}))
            rows = [{"id": ident(c["symbol"]), "chain_symbol": chain_symbol, "chain_id": "standard-chain",
                     "strike_price": f"{c['strike']:.4f}",
                     "type": c["option_type"], "expiration_date": expiration_dates, "state": "active",
                     "tradability": "tradable"} for c in self.contracts(chain_symbol, expiration_dates)]
            # A contract adjusted after a corporate action, at a strike the standard chain also has.
            rows.insert(3, {**rows[0], "id": "adjusted-one", "chain_symbol": chain_symbol + "1",
                            "chain_id": "adjusted-chain"})
            rows.append({"id": "expired-one", "chain_symbol": chain_symbol, "strike_price": "100.0000",
                         "type": "call", "expiration_date": expiration_dates, "state": "expired"})
            return json.dumps(self.wrap({"results": rows}))        # sent as text, as some servers do

        @srv.tool(annotations=READ_ONLY)
        def get_option_quotes(ids: Annotated[list[str], Field(max_length=20)]) -> dict:
            """Get real-time quotes for option contracts, with Greeks."""
            self.calls.append(("get_option_quotes", {"ids": list(ids)}))
            return self.wrap({"quotes": [self.option_quote(i, True, True, True) for i in ids if i in self.by_id]})

        @srv.tool(annotations=READ_ONLY)
        def get_equity_historicals(symbols: list[str], interval: Literal["5minute", "day", "week"],
                                   span: Literal["week", "year", "5year"], bounds: str = "regular") -> dict:
            """Get OHLCV historicals."""
            self.calls.append(("get_equity_historicals", {"symbols": symbols, "interval": interval, "span": span}))
            if span == "5year" and interval != "week":
                raise ValueError("5year span supports only the week interval")
            bars = demo.history(symbols[0], "2000-01-01", "2100-01-01").tail(252)
            rows = [{"begins_at": f"{d:%Y-%m-%d}T00:00:00Z", "open_price": money(o), "high_price": money(h),
                     "low_price": money(l), "close_price": money(c), "volume": int(v), "interpolated": False}
                    for d, o, h, l, c, v in bars.itertuples(index=False, name=None)]
            return self.wrap({"results": [{"symbol": symbols[0], "interval": interval, "historicals": rows}]})

    # --- variant b: same information, different names and layout ------------------
    def _register_b(self) -> None:
        srv, demo = self.server, self.demo

        @srv.tool(annotations=READ_ONLY)
        def get_equity_quotes(symbol: str) -> dict:
            """Quote for one symbol."""
            self.calls.append(("get_equity_quotes", {"symbol": symbol}))
            q = demo.quotes([symbol]).get(symbol)
            if not q:
                return self.wrap({"results": []})
            return self.wrap({"symbol": symbol, "last_trade_price": q["last"], "bid_price": q["bid"],
                              "ask_price": q["ask"], "adjusted_previous_close": q["prevclose"],
                              "updated_at": 1791230398123})

        @srv.tool(annotations=READ_ONLY)
        def get_option_chains(underlying_symbol: str) -> dict:
            """Expirations for an underlying."""
            self.calls.append(("get_option_chains", {"underlying_symbol": underlying_symbol}))
            return self.wrap({"chain_id": "c-1", "expirations": demo.expirations(underlying_symbol)})

        @srv.tool(annotations=READ_ONLY)
        def get_option_instruments(symbol: str, option_type: str | None = None) -> dict:
            """Every contract for an underlying (no expiration filter)."""
            self.calls.append(("get_option_instruments", {"symbol": symbol}))
            rows = []
            for expiration in demo.expirations(symbol):
                rows += [{"url": f"https://api.robinhood.com/options/instruments/{ident(c['symbol'])}/",
                          "strike": c["strike"], "option_type": c["option_type"].upper(),
                          "expiration": expiration} for c in self.contracts(symbol, expiration)]
            return self.wrap(rows)

        @srv.tool(annotations=READ_ONLY)
        def get_option_quotes(instrument_ids: str) -> dict:
            """Quotes for a comma-separated list of contract ids."""
            ids = [i for i in instrument_ids.split(",") if i]
            self.calls.append(("get_option_quotes", {"instrument_ids": ids}))
            # Not in the order asked, and the contract is named only by an address.
            return self.wrap([{**self.option_quote(i, False, False, False),
                               "instrument": f"https://api.robinhood.com/options/instruments/{i}/"}
                              for i in reversed(ids)])

        @srv.tool(annotations=READ_ONLY)
        def get_stock_historicals(symbol: str, start_date: str, end_date: str) -> dict:
            """Daily bars between two dates."""
            self.calls.append(("get_stock_historicals", {"symbol": symbol, "start_date": start_date}))
            bars = demo.history(symbol, start_date, end_date)
            return self.wrap({"bars": [{"date": f"{d:%Y-%m-%d}", "open": o, "high": h, "low": l, "close": c,
                                        "volume": int(v)} for d, o, h, l, c, v in
                                       bars.itertuples(index=False, name=None)]})

    # --- variant c: what Robinhood's server was actually seen to do -----------------------------
    # Seen for real (a capture from 2026-10-06): every tool's argument layout, the stock-quote reply
    # and the option-chain reply. Still a guess: the replies for option contracts, option quotes,
    # price history and earnings, which are modelled on the two replies that were seen.
    def _register_c(self) -> None:
        import re
        srv, demo = self.server, self.demo

        @srv.tool(annotations=READ_ONLY)
        def get_equity_quotes(symbols: list[str] | None) -> dict:
            """Get real-time stock quotes and the official last-completed-session close for one or more symbols."""
            self.calls.append(("get_equity_quotes", {"symbols": symbols}))
            rows = []
            for symbol, q in demo.quotes(symbols or []).items():
                rows.append({"quote": {
                    "symbol": symbol, "last_trade_price": money(q["last"] - 0.25),
                    "venue_last_trade_time": "2026-10-05T19:59:57.939984769Z",       # nanoseconds, as seen
                    "last_non_reg_trade_price": money(q["last"]),                     # the newer, after-hours trade
                    "venue_last_non_reg_trade_time": "2026-10-05T19:59:58.544Z",
                    "adjusted_previous_close": money(q["prevclose"]), "previous_close": money(q["prevclose"]),
                    "previous_close_date": "2026-10-02", "bid_price": money(q["bid"]), "venue_bid_time": STAMP,
                    "ask_price": money(q["ask"]), "venue_ask_time": STAMP, "has_traded": True, "state": "active"},
                    "close": {"symbol": symbol, "date": "2026-10-02", "price": f"{q['prevclose']:.2f}",
                              "interpolated": False, "source": "sip-list-exchange-close"}})
            return self.wrap({"results": rows})

        @srv.tool(annotations=READ_ONLY)
        def get_option_chains(ids: str | None = None, underlying_symbol: str | None = None) -> dict:
            """List option chains for one or more underlyings."""
            self.calls.append(("get_option_chains", {"underlying_symbol": underlying_symbol}))
            return self.wrap({"chains": [{
                "id": f"chain-{underlying_symbol}", "symbol": underlying_symbol, "can_open_position": True,
                "cash_component": None, "expiration_dates": demo.expirations(underlying_symbol),
                "trade_value_multiplier": "100.0000",
                "underlying_instruments": [{"instrument": "http://edge-internal.example/instruments/450dfc6d/", "symbol": ""}],
                "min_ticks": {"above_tick": "0.05", "below_tick": "0.01", "cutoff_price": "3.00"},
                "settle_on_open": False}]})

        @srv.tool(annotations=READ_ONLY)
        def get_option_instruments(chain_id: str | None = None, chain_symbol: str | None = None,
                                   expiration_dates: str | None = None, strike_price: str | None = None,
                                   type: str | None = None, state: str | None = None,
                                   tradability: str | None = None, ids: str | None = None,
                                   cursor: str | None = None) -> dict:
            """List option contracts. One of chain_symbol, chain_id, or ids is required."""
            self.calls.append(("get_option_instruments", {"chain_id": chain_id, "chain_symbol": chain_symbol,
                                                          "expiration_dates": expiration_dates, "cursor": cursor}))
            if sum(x is not None for x in (chain_id, chain_symbol, ids)) != 1:
                raise ValueError("exactly one of chain_symbol, chain_id, or ids is required")
            symbol = chain_id.removeprefix("chain-") if chain_id else chain_symbol
            rows = [{"id": ident(c["symbol"]), "chain_id": f"chain-{symbol}", "chain_symbol": symbol,
                     "strike_price": f"{c['strike']:.4f}", "type": c["option_type"], "expiration_date": expiration_dates,
                     "state": "active", "tradability": "tradable",
                     "min_ticks": {"above_tick": "0.05", "below_tick": "0.01", "cutoff_price": "3.00"},
                     "url": f"http://edge-internal.example/options/instruments/{ident(c['symbol'])}/"}
                    for c in self.contracts(symbol, expiration_dates)]
            start = int(cursor.removeprefix("page:")) if cursor else 0
            nxt = f"page:{start + 30}" if start + 30 < len(rows) else None
            return self.wrap({"results": rows[start:start + 30], "next": nxt})

        @srv.tool(annotations=READ_ONLY)
        def get_option_quotes(instrument_ids: list[str] | None) -> dict:
            """Get real-time quotes for one or more option contracts by instrument UUID, plus the prior close."""
            self.calls.append(("get_option_quotes", {"instrument_ids": list(instrument_ids or [])}))
            rows = []
            for i in reversed(instrument_ids or []):
                q = self.option_quote(i, False, True, True)
                stamp = q.pop("updated_at")
                q.update({"venue_bid_time": stamp, "venue_ask_time": stamp})
                rows.append({"quote": q, "close": {"instrument_id": i, "date": "2026-10-02", "price": "1.00"}})
            return self.wrap({"results": rows})

        @srv.tool(annotations=READ_ONLY)
        def get_equity_historicals(symbols: list[str] | None, start_time: str, end_time: str | None = None,
                                   interval: str | None = None, bounds: str | None = None,
                                   adjustment_type: str | None = None) -> dict:
            """Get OHLCV bars for one or more equity symbols across an explicit time range."""
            self.calls.append(("get_equity_historicals", {"symbols": symbols, "start_time": start_time,
                                                          "end_time": end_time, "interval": interval}))
            for label, value in (("start_time", start_time), ("end_time", end_time)):
                if value is not None and not re.fullmatch(RFC3339, value):       # the real server's own complaint
                    raise ValueError(f"{label} must be RFC3339 (e.g. '2026-01-01T00:00:00Z')")
            bars = demo.history(symbols[0], start_time[:10], (end_time or "2100-01-01")[:10])
            def midnight_new_york(day) -> str:              # 04:00Z in summer, 05:00Z in winter
                local = datetime(day.year, day.month, day.day, tzinfo=ZoneInfo("America/New_York"))
                return local.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
            rows = [{"begins_at": self.bar_stamp(d) if self.bar_stamp else midnight_new_york(d),
                     "open_price": money(o), "high_price": money(h),
                     "low_price": money(l), "close_price": money(c), "volume": int(v), "interpolated": False}
                    for d, o, h, l, c, v in bars.itertuples(index=False, name=None)]
            if rows:                                     # a filler bar for a day with no trading
                rows.insert(1, {**rows[0], "begins_at": "2020-07-04T04:00:00Z", "interpolated": True})
            return self.wrap({"results": [{"symbol": symbols[0], "interval": interval or "day", "bars": rows}]})

        @srv.tool(annotations=READ_ONLY)
        def get_earnings_results(symbol: str) -> dict:
            """Get recent and upcoming earnings for ONE equity symbol."""
            self.calls.append(("get_earnings_results", {"symbol": symbol}))
            return self.wrap({"symbol": symbol, "results": [{"report_date": "2026-10-29", "timing": "pm",
                                                             "eps_estimate": "1.61", "verified": True}]})
