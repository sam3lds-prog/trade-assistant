import json
import re
from datetime import datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from mcp import Client

from tests.robinhood_standin import STAMP, Standin, ident
from tradeassist.api import create_app
from tradeassist.config import Settings, load_settings
from tradeassist.db import Database
from tradeassist.optionmath import NY
from tradeassist.providers.base import DataError
from tradeassist.providers.demo import DemoData
from tradeassist.providers.mcp_bridge import ReadOnlyBridge, is_read_only, parse_tool_result
from tradeassist.providers.robinhood import (ALLOWED_TOOLS, RobinhoodData, find_bars, is_market_data_tool,
                                             iso_time, records, unwrap)
from tradeassist.robinhood_check import run_check, shorten
from tradeassist.service import TradeAssistant


def connect(variant="a", fallback=None):
    standin = Standin(variant)
    bridge = ReadOnlyBridge(lambda: Client(standin.server), ALLOWED_TOOLS, label="Robinhood")
    if variant == "c":
        standin.use_observed_schemas(bridge)
    return standin, RobinhoodData(bridge, history_fallback=fallback, poll_seconds=3.0)


@pytest.fixture(params=["a", "b", "c"])
def rh(request):
    standin, provider = connect(request.param)
    yield standin, provider
    provider.bridge.close()


# --- the read-only guard -----------------------------------------------------------

def test_only_the_listed_market_data_tools_may_be_called():
    assert ALLOWED_TOOLS == {"get_equity_quotes", "get_option_chains", "get_option_instruments", "get_option_quotes",
                             "get_equity_historicals", "get_stock_historicals", "get_earnings_results"}
    for name in ALLOWED_TOOLS:
        assert is_read_only(name, None, ALLOWED_TOOLS) and is_market_data_tool(name), name
    refused = [
        # tools that act
        "place_equity_order", "place_option_order", "cancel_option_order", "review_option_order",
        "decline_trade_approval", "add_to_watchlist", "remove_from_watchlist", "create_alert", "update_watchlist",
        "set_trade_approval_setting",
        # names that begin like a read but act, in several spellings
        "get_and_reset_alerts", "get_PlaceOrder", "get_CancelOrder", "get_PLACE_ORDER", "get_optionOrderSubmit",
        "get_option-order-submit", "get_buy_stock", "get_and_sell", "get_submit_trade", "get_and_execute_trade",
        "get_and_exercise_option", "get_close_all", "get_set_stop_loss", "get_send_money", "get_or_create_watchlist",
        "getAndPlaceOrder", "GET_EQUITY_QUOTES", "get_equity_quotes ", "get_equity_quote_and_buy",
        # tools that read the account
        "get_accounts", "get_portfolio", "get_option_positions", "get_realized_pnl", "get_watchlists",
        "get_equity_orders", "get_option_orders", "get_order_history", "get_trade_approvals", "get_holdings",
        "get_balances", "get_buying_power", "get_transactions", "get_trades", "get_me", "get_OptionPositions",
        # look-alikes of the market-data tools
        "get_equity_holdings_quotes", "get_option_chain_for_holdings", "get_option_instruments_held",
        "get_my_option_quotes", "get_holdings_historicals", "get_balance_historicals", "get_historical_trades", "get_historicals",
        "search", "",
    ]
    for name in refused:
        assert not is_read_only(name, None, ALLOWED_TOOLS) and not is_market_data_tool(name), name
    # Being on a list is not enough if the server says the tool changes things.
    from mcp import types
    assert not is_read_only("get_equity_quotes", types.ToolAnnotations(destructive_hint=True), ALLOWED_TOOLS)
    assert not is_read_only("get_equity_quotes", types.ToolAnnotations(read_only_hint=False), ALLOWED_TOOLS)
    assert not is_market_data_tool("get_option_quotes", types.ToolAnnotations(read_only_hint=False))


def test_a_bridge_cannot_be_built_with_a_tool_that_is_not_a_plain_read():
    for bad in ("place_option_order", "get_and_reset_alerts", "get_PlaceOrder", "get_equity_quote_and_buy",
                "get_option_orders", "cancel_order", "search"):
        with pytest.raises(ValueError, match="may not be called"):
            ReadOnlyBridge(lambda: None, frozenset({"get_equity_quotes", bad}))


def test_bridge_refuses_everything_off_the_list_and_nothing_reaches_the_server():
    standin, provider = connect("a")
    try:
        bridge = provider.bridge
        assert {"place_option_order", "cancel_equity_order", "get_accounts", "get_PlaceOrder"} <= set(bridge.tools())
        for name in sorted(set(bridge.tools()) - ALLOWED_TOOLS):
            with pytest.raises(PermissionError, match="not on this app's list"):
                bridge.call(name, {"symbols": ["AAPL"], "symbol": "AAPL", "option_id": "x", "order_id": "x"})
            with pytest.raises(PermissionError, match="not a market-data tool"):
                provider._call(bridge.tools()[name], {})
            assert not bridge.may_call(name)
        with pytest.raises(PermissionError):
            bridge.call("get_nothing", {})
        assert standin.forbidden_calls == [] and bridge.calls == []
    finally:
        provider.bridge.close()


def test_a_listed_tool_the_server_marks_as_changing_things_is_refused():
    from mcp import types
    standin, provider = connect("a")
    try:
        real = provider.bridge.tools()["get_equity_quotes"]
        marked = real.model_copy(update={"annotations": types.ToolAnnotations(read_only_hint=False)})
        provider.bridge._tools = {**provider.bridge.tools(), "get_equity_quotes": marked}
        with pytest.raises(PermissionError, match="does not describe it as a read-only tool"):
            provider.bridge.call("get_equity_quotes", {"symbols": ["AAPL"]})
        with pytest.raises(DataError, match="changes something"):
            provider.quotes(["AAPL"])
        assert standin.calls == []
    finally:
        provider.bridge.close()


def test_a_look_alike_is_never_used_when_the_expected_tool_is_missing():
    """The server here offers look-alikes such as get_equity_quote_and_buy and get_holdings_historicals."""
    for missing, action in [("get_equity_quotes", lambda p: p.quotes(["AAPL"])),
                            ("get_option_chains", lambda p: p.expirations("AAPL")),
                            ("get_equity_historicals", lambda p: p._history("AAPL", "2026-01-01", "2026-10-01"))]:
        standin, provider = connect("a")
        try:
            tools = dict(provider.bridge.tools())
            del tools[missing]
            provider.bridge._tools = tools
            with pytest.raises(DataError, match="none was called") as caught:
                action(provider)
            assert "robinhood-check" in str(caught.value)
            assert standin.forbidden_calls == [] and standin.calls == []
        finally:
            provider.bridge.close()


def test_a_full_session_never_touches_account_or_order_tools(rh):
    standin, provider = rh
    symbol = "AAPL"
    expiration = provider.expirations(symbol)[4]
    chain = provider.chain(symbol, expiration)
    provider.quotes([symbol, chain[0]["symbol"]])
    end = datetime.now(NY).date()
    provider.history(symbol, (end - timedelta(days=300)).isoformat(), end.isoformat())
    assert standin.forbidden_calls == []
    assert set(provider.bridge.calls) <= {"get_equity_quotes", "get_option_chains", "get_option_instruments",
                                          "get_option_quotes", "get_equity_historicals", "get_stock_historicals"}


# --- mapping replies -----------------------------------------------------------------

def test_quotes_chain_and_greeks_match_the_source_in_both_layouts(rh):
    standin, provider = rh
    demo = standin.demo
    truth_quote = demo.quotes(["AAPL"])["AAPL"]
    got = provider.quotes(["AAPL"])["AAPL"]
    assert got["last"] == pytest.approx(truth_quote["last"]) and got["bid"] == pytest.approx(truth_quote["bid"])
    assert got["prevclose"] == pytest.approx(truth_quote["prevclose"]) and got["type"] == "stock"
    assert got["bid_time"].startswith("2026-10-05T19:59:58.123")            # kept to the millisecond

    expirations = provider.expirations("AAPL")
    assert expirations == demo.expirations("AAPL")
    expiration = expirations[2]
    truth = {c["symbol"]: c for c in demo.chain("AAPL", expiration)}
    chain = provider.chain("AAPL", expiration)
    assert len(chain) == len(truth) and {c["symbol"] for c in chain} == set(truth)   # same OCC-style symbols
    for c in chain:
        t = truth[c["symbol"]]
        assert c["expiration"] == expiration and c["contract_size"] == 100
        assert (c["strike"], c["option_type"]) == (t["strike"], t["option_type"])
        assert c["bid"] == pytest.approx(t["bid"]) and c["ask"] == pytest.approx(t["ask"])
        assert c["open_interest"] == t["open_interest"] and c["bidsize"] == t["bidsize"]
        assert c["greeks"]["delta"] == pytest.approx(t["greeks"]["delta"], abs=1e-6)
        assert c["greeks"]["theta"] == pytest.approx(t["greeks"]["theta"], abs=1e-6)
        assert c["greeks"]["mid_iv"] == pytest.approx(t["greeks"]["mid_iv"], abs=1e-6)
        assert c["greeks"]["smv_vol"] is None                                 # Robinhood has no smoothed volatility

    one = chain[len(chain) // 2]
    both = provider.quotes(["AAPL", one["symbol"]])
    assert both[one["symbol"]]["type"] == "option" and both[one["symbol"]]["bid"] == one["bid"]
    assert "AAPL" in both


def test_requests_are_built_from_what_each_tool_declares():
    standin, provider = connect("a")
    try:
        expiration = provider.expirations("AAPL")[2]
        chain = provider.chain("AAPL", expiration)
        quote_calls = [args["ids"] for name, args in standin.calls if name == "get_option_quotes"]
        assert all(len(ids) <= 20 for ids in quote_calls)                     # the declared maximum is respected
        assert sum(len(ids) for ids in quote_calls) == len(chain)
        assert ("get_option_instruments", {"chain_symbol": "AAPL", "expiration_dates": expiration}) in standin.calls
        before = len(standin.calls)
        provider.chain("AAPL", expiration)                                    # contract list is reused
        names = [name for name, _ in standin.calls[before:]]
        assert "get_option_instruments" not in names and "get_option_chains" not in names
    finally:
        provider.bridge.close()
    standin, provider = connect("b")
    try:
        expiration = provider.expirations("MSFT")[1]
        provider.chain("MSFT", expiration)
        assert ("get_option_chains", {"underlying_symbol": "MSFT"}) in standin.calls
        quote_calls = [args["instrument_ids"] for name, args in standin.calls if name == "get_option_quotes"]
        assert quote_calls and all(isinstance(ids, list) and len(ids) <= 100 for ids in quote_calls)
        assert all(args.get("symbol") == "MSFT" for name, args in standin.calls if name == "get_equity_quotes")
    finally:
        provider.bridge.close()


def test_only_strikes_near_the_stock_price_are_quoted_on_a_long_chain(monkeypatch):
    standin, provider = connect("a")
    try:
        monkeypatch.setattr("tradeassist.providers.robinhood.STRIKE_WINDOW", 0.10)
        monkeypatch.setattr("tradeassist.providers.robinhood.WHOLE_CHAIN_LIMIT", 80)
        real = standin.contracts
        def many(symbol, expiration):                # pad the chain so it is long enough to trim
            rows = real(symbol, expiration)
            extra = []
            for i in range(60):
                c = dict(rows[0]); c["strike"] = 1.0 + i; c["symbol"] = f"PAD{i:03d}"
                extra.append(c)
            for c in extra:
                standin.by_id[ident(c["symbol"])] = c
            return rows + extra
        standin.contracts = many
        spot = standin.demo.quotes(["AAPL"])["AAPL"]["last"]
        chain = provider.chain("AAPL", provider.expirations("AAPL")[2])
        assert chain and all(abs(c["strike"] / spot - 1) <= 0.10 for c in chain) and len(chain) >= 10
    finally:
        provider.bridge.close()


def test_history_tries_spans_then_uses_the_fallback_for_long_ranges():
    end = datetime.now(NY).date()
    three_years = (end - timedelta(days=3 * 365)).isoformat()
    standin, provider = connect("a")
    try:
        bars = provider.history("AAPL", three_years, end.isoformat())
        spans = [args["span"] for name, args in standin.calls if name == "get_equity_historicals"]
        assert spans == ["5year", "year"]                                     # the first is refused for daily bars
        assert len(bars) == 252 and list(bars.columns) == ["date", "open", "high", "low", "close", "volume"]
        truth = standin.demo.history("AAPL", "2000-01-01", "2100-01-01").tail(252)
        assert bars["close"].tolist() == pytest.approx(truth["close"].tolist())
        assert bars["date"].tolist() == truth["date"].tolist()                # midnight-UTC stamps keep their day
    finally:
        provider.bridge.close()
    standin, provider = connect("a", fallback=DemoData())
    try:
        bars = provider.history("AAPL", three_years, end.isoformat())
        assert len(bars) > 700                                                # one year is not enough: fallback used
        short = provider.history("AAPL", (end - timedelta(days=200)).isoformat(), end.isoformat())
        assert 100 < len(short) < 160                                         # short range: Robinhood's own bars
    finally:
        provider.bridge.close()
    standin, provider = connect("b")
    try:
        bars = provider.history("KO", three_years, end.isoformat())
        assert len(bars) > 700 and ("get_stock_historicals", {"symbol": "KO", "start_date": three_years}) in standin.calls
    finally:
        provider.bridge.close()


def test_helpers():
    assert unwrap({"data": [1], "guide": "g"}) == [1] and unwrap({"data": 1, "other": 2}) == {"data": 1, "other": 2}
    assert records({"data": {"quotes": [{"a": 1}, "junk"]}}, "quotes") == [{"a": 1}]
    assert records({"data": {"symbol": "A", "bid_price": "1"}}) == [{"symbol": "A", "bid_price": "1"}]
    assert records({"data": {"x": [{"a": 1}], "y": [{"b": 2}]}}) == [] and records("text") == []
    assert iso_time(STAMP) == "2026-10-05T19:59:58.123456+00:00"
    assert iso_time(1791230398123).startswith("2026-10-05T") and iso_time("soon") is None and iso_time(None) is None
    bars = find_bars({"results": [{"symbol": "A", "historicals": [
        {"begins_at": "2026-10-05T00:00:00Z", "open_price": "1", "high_price": "2", "low_price": "1", "close_price": "2", "volume": 5},
        {"begins_at": "2026-10-06T13:30:00Z", "open_price": "2", "high_price": "3", "low_price": "2", "close_price": "3", "volume": 6},
        {"begins_at": None, "close_price": "9"}]}]})
    assert [b["date"] for b in bars] == ["2026-10-05", "2026-10-06"] and bars[0]["close"] == 2.0
    assert find_bars({"nothing": 1}) == [] and find_bars([{"date": "2026-01-02", "close": "x"}]) == []

    class Part:
        def __init__(self, text): self.text = text
    class Result:
        def __init__(self, text=None, structured=None, error=False):
            self.content = [Part(text)] if text is not None else []
            self.structured_content, self.is_error = structured, error
    assert parse_tool_result(Result('{"data": 1}')) == {"data": 1}
    assert parse_tool_result(Result("not json", {"result": '{"a": 2}'})) == {"a": 2}
    assert parse_tool_result(Result(None, {"x": 1})) == {"x": 1}
    with pytest.raises(DataError, match="reported an error"):
        parse_tool_result(Result("rate limited", error=True))
    with pytest.raises(DataError, match="could not read"):
        parse_tool_result(Result("plain words"))


# --- replies the app does not understand must fail loudly, not quietly ---------------------

def test_unreadable_replies_raise_clear_errors():
    standin, provider = connect("a")
    try:
        expiration = provider.expirations("AAPL")[2]
        provider.chain("AAPL", expiration)
        real_call = provider.bridge.call

        def garbled(name, arguments=None):
            if name == "get_option_quotes":
                return {"data": {"quotes": [{"instrument_id": i, "px": "1.0"} for i in arguments["ids"]]}, "guide": ""}
            return real_call(name, arguments)
        provider.bridge.call = garbled
        with pytest.raises(DataError, match="no\\s+readable quotes"):
            provider.chain("AAPL", expiration)

        def no_expirations(name, arguments=None):
            return {"data": {"chains": [{"id": "x"}]}, "guide": ""} if name == "get_option_chains" else real_call(name, arguments)
        provider.bridge.call = no_expirations
        with pytest.raises(DataError, match="no option expirations"):
            provider.expirations("NVDA")

        def adjusted_only(name, arguments=None):
            if name == "get_option_chains":
                return {"data": {"chains": [{"symbol": "TSLA1", "expiration_dates": ["2030-01-18"],
                                             "trade_value_multiplier": "150.0"}]}, "guide": ""}
            return real_call(name, arguments)
        provider.bridge.call = adjusted_only
        with pytest.raises(DataError, match="adjusted option contracts"):
            provider.expirations("TSLA")
    finally:
        provider.bridge.close()


def test_missing_tools_and_unknown_arguments_explain_what_to_do():
    class Odd:
        name, annotations = "get_option_quotes", None
        input_schema = {"type": "object", "properties": {"contracts": {"type": "array"}}, "required": ["contracts"]}
    standin, provider = connect("a")
    try:
        provider.bridge._tools = {**provider.bridge.tools(), "get_option_quotes": Odd()}
        with pytest.raises(DataError, match="contract-id argument"):
            provider._option_quotes(["x"])
    finally:
        provider.bridge.close()


def test_connection_failures_become_readable_errors():
    def broken():
        raise RuntimeError("network down")
    bridge = ReadOnlyBridge(broken, ALLOWED_TOOLS, label="Robinhood")
    with pytest.raises(DataError, match="Could not connect to Robinhood"):
        bridge.tools()

    from contextlib import asynccontextmanager
    from tradeassist.providers.mcp_bridge import SignInRequired

    @asynccontextmanager
    async def needs_sign_in():
        raise ExceptionGroup("auth", [SignInRequired("The Robinhood sign-in is missing or has expired.")])
        yield
    bridge = ReadOnlyBridge(needs_sign_in, ALLOWED_TOOLS, label="Robinhood")
    with pytest.raises(SignInRequired, match="sign-in is missing"):
        bridge.tools()


# --- the whole app on Robinhood data -----------------------------------------------------------

def test_app_runs_end_to_end_on_robinhood_data(tmp_path, rh):
    standin, provider = rh
    provider.history_fallback = DemoData()
    settings = Settings(data_mode="robinhood", db_path=tmp_path / "t.db", account_size=50_000,
                        watchlist=["AAPL", "MSFT", "KO", "JPM", "HD"])
    assistant = TradeAssistant(settings, provider=provider, db=Database(":memory:"))
    assistant._traded_today = lambda: True
    client = TestClient(create_app(settings, assistant))

    status = client.get("/api/status").json()
    assert status["data"]["mode"] == "robinhood" and status["data"]["label"] == "Robinhood"
    exps = client.get("/api/expirations", params={"symbol": "AAPL"}).json()
    exp = next(e["date"] for e in exps["expirations"] if e["dte"] >= 25)
    chain = client.get("/api/chain", params={"symbol": "AAPL", "expiration": exp}).json()
    row = min((c for c in chain["contracts"] if c["option_type"] == "call"),
              key=lambda c: abs(c["strike"] - chain["spot"] * 1.03))
    assert row["greeks_source"] == "provider" and row["model_source"] == "smile fitted across strikes"
    req = {"symbol": "AAPL", "expiration": exp, "option_type": "call", "strike": row["strike"]}
    priced = client.post("/api/price", json=req).json()
    assert priced["ladder"]["ok"] and priced["scorecard"]["checks"] and priced["data"]["poll_seconds"] == 3.0
    assert not any("made up" in w["text"] or "delayed" in w["text"] for w in priced["warnings"])
    live = client.post("/api/live", json=req).json()
    assert live["bid"] == priced["contract"]["bid"] and live["bid_time"].startswith("2026-10-05T19:59:58.123")
    truth = next(c for c in standin.demo.chain("AAPL", exp) if c["symbol"] == priced["contract"]["symbol"])
    assert priced["contract"]["bid"] == pytest.approx(truth["bid"]) and priced["contract"]["delta"] == pytest.approx(
        truth["greeks"]["delta"], abs=1e-6)
    assert client.get("/api/research/MSFT").status_code == 200
    study = client.post("/api/patterns/run", json={"years": 3, "horizons": [10]}).json()
    assert study["data_source"] == "robinhood" and study["verdicts_given"]
    assert standin.forbidden_calls == []


def test_signed_out_app_reports_what_to_do(tmp_path):
    from tradeassist.providers.mcp_bridge import SignInRequired
    from contextlib import asynccontextmanager

    @asynccontextmanager
    async def signed_out():
        raise SignInRequired("The Robinhood sign-in is missing or has expired. Stop the app, run "
                             "'python run.py --robinhood-check' to sign in again, then restart.")
        yield
    provider = RobinhoodData(ReadOnlyBridge(signed_out, ALLOWED_TOOLS, label="Robinhood"))
    settings = Settings(data_mode="robinhood", db_path=tmp_path / "t.db")
    client = TestClient(create_app(settings, TradeAssistant(settings, provider=provider, db=Database(":memory:"))))
    assert client.get("/api/status").status_code == 200                       # the page still loads
    r = client.get("/api/expirations", params={"symbol": "AAPL"})
    assert r.status_code == 502 and "--robinhood-check" in r.json()["error"]


# --- the check command ----------------------------------------------------------------------------

def test_check_reports_each_step_and_writes_a_shareable_capture(tmp_path, rh):
    standin, provider = rh
    out = tmp_path / "capture.json"
    result = run_check(provider, "AAPL", out)
    assert result["ok"], result["steps"]
    assert [s["step"] for s in result["steps"]] == ["Connect and list tools", "Stock quote", "Option expirations",
                                                    "Option chain and Greeks", "Live quote for one contract",
                                                    "Daily price history", "Earnings dates (not used yet)"]
    earnings = result["steps"][-1]
    assert earnings["optional"] and earnings["ok"] == (standin.variant == "c")   # only the real layout has the tool
    capture = json.loads(out.read_text())
    by_name = {t["name"]: t for t in capture["tools"]}
    assert by_name["place_option_order"]["may_be_called"] is False and by_name["get_accounts"]["may_be_called"] is False
    assert by_name["get_option_quotes"]["may_be_called"] is True and by_name["get_option_quotes"]["input_schema"]
    assert {n for n, t in by_name.items() if t["may_be_called"]} <= ALLOWED_TOOLS
    assert {c["tool"] for c in capture["calls"]} <= {"get_equity_quotes", "get_option_chains",
                                                     "get_option_instruments", "get_option_quotes",
                                                     "get_equity_historicals", "get_stock_historicals",
                                                     "get_earnings_results"}
    assert standin.forbidden_calls == [] and "123456789" not in out.read_text() and "5QR12345" not in out.read_text()
    assert provider.recorder is None                                          # recording stops afterwards


def test_check_pinpoints_a_mismatch(tmp_path):
    standin, provider = connect("a")
    try:
        real_call = provider.bridge.call
        provider.bridge.call = lambda name, arguments=None: (
            {"data": {"quotes": [{"instrument_id": i, "px": 1} for i in arguments["ids"]]}, "guide": ""}
            if name == "get_option_quotes" else real_call(name, arguments))
        result = run_check(provider, "AAPL", tmp_path / "c.json")
        failed = [s for s in result["steps"] if not s["ok"]]
        assert not result["ok"] and failed[0]["step"] == "Option chain and Greeks"
        capture = json.loads((tmp_path / "c.json").read_text())
        bad = next(c for c in capture["calls"] if c["tool"] == "get_option_quotes")
        assert bad["reply"]["data"]["quotes"][0]["px"] == 1                   # the evidence needed to fix it
    finally:
        provider.bridge.close()


def test_shorten_trims_and_removes_personal_details():
    out = shorten({"account_number": "123", "quotes": list(range(10)), "note": "x" * 500,
                   "nested": {"user_id": 7, "email": "a@b.c", "bid_price": "1.0", "first_name": "Sam", "owner": "S",
                              "Authorization": "Bearer abc", "session_id": "q", "symbol": "AAPL",
                              "updated_at": "1791230398123", "volume": "52345678.000000"},
                   "guide": "Signed in as Sam Example <sam@example.com>; default account 5QR12345.",
                   "url": "https://api.robinhood.com/accounts/5QR12345/positions/?token=abcdef",
                   "instrument": "https://api.robinhood.com/options/instruments/1a2b3c4d-0000/"})
    assert out["account_number"] == "[removed]"
    assert out["nested"] == {"user_id": "[removed]", "email": "[removed]", "bid_price": "1.0",
                             "first_name": "[removed]", "owner": "[removed]", "Authorization": "[removed]",
                             "session_id": "[removed]", "symbol": "AAPL", "updated_at": "1791230398123",
                             "volume": "52345678.000000"}                    # market data is left alone
    assert out["quotes"] == [0, 1, 2, "... 7 more"] and len(out["note"]) == 303
    text = json.dumps(out)
    assert "sam@example.com" not in text and "5QR12345" not in text and "abcdef" not in text
    assert out["instrument"].endswith("/1a2b3c4d-0000/")                      # contract ids are needed for a fix


def test_capture_scrubs_tool_descriptions_schemas_and_error_text(tmp_path):
    from tradeassist.robinhood_check import scrub_schema, scrub_text
    schema = scrub_schema({"type": "object", "properties": {
        "account_number": {"type": "string", "default": "5QR12345", "enum": ["5QR12345", "7AB99999"]},
        "symbols": {"type": "array", "items": {"type": "string"}, "maxItems": 20, "examples": [["AAPL"]]},
        "interval": {"type": "string", "enum": ["day", "week"], "description": "for sam@example.com"}}})
    assert schema["properties"]["account_number"] == {"type": "string"}
    assert schema["properties"]["symbols"]["maxItems"] == 20 and "examples" not in schema["properties"]["symbols"]
    assert schema["properties"]["interval"]["enum"] == ["day", "week"]        # what a fix needs is kept
    assert "sam@example.com" not in json.dumps(schema)
    message = scrub_text("Options are not enabled for account 5QR12345 (sam@example.com). Visit "
                         "https://robinhood.com/account/5QR12345/options with Bearer eyJhbGciOi.abc.def")
    assert "5QR12345" not in message and "sam@example.com" not in message and "eyJ" not in message

    # End to end: a server error that names the account must not reach the file.
    standin, provider = connect("a")
    try:
        real_call = provider.bridge.call

        def failing(name, arguments=None):
            if name == "get_option_chains":
                raise DataError("The data source reported an error: Options are not enabled for account 5QR12345 "
                                "(Sam, sam@example.com)")
            return real_call(name, arguments)
        provider.bridge.call = failing
        result = run_check(provider, "AAPL", tmp_path / "c.json")
        text = (tmp_path / "c.json").read_text()
        assert not result["ok"] and "5QR12345" not in text and "sam@example.com" not in text
        assert (tmp_path / "c.json").stat().st_mode & 0o777 == 0o600
    finally:
        provider.bridge.close()


def test_settings_accept_robinhood_mode(monkeypatch, tmp_path):
    for key in ("TRADIER_LIVE_TOKEN", "TRADIER_SANDBOX_TOKEN", "ROBINHOOD_POLL_SECONDS", "ROBINHOOD_CALLBACK_PORT"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("DATA_MODE", "robinhood")
    monkeypatch.setenv("TRADEASSIST_DB", str(tmp_path / "d" / "x.db"))
    empty = tmp_path / "none.env"
    empty.write_text("")
    s = load_settings(empty)
    assert s.resolved_mode == "robinhood" and s.robinhood_signin_path == tmp_path / "d" / "robinhood_signin.json"
    assert s.robinhood_poll_seconds == 3.0 and s.robinhood_callback_port == 8421
    monkeypatch.setenv("ROBINHOOD_POLL_SECONDS", "0.1")
    assert load_settings(empty).robinhood_poll_seconds == 1.0                 # never faster than once a second
    from tradeassist.providers import make_provider
    provider = make_provider(s)                                               # builds without connecting
    assert provider.info.mode == "robinhood" and provider.history_fallback is None
    assert provider.clock()["state"] in ("open", "closed", "pre", "post")


# --- mistakes that would mislead a trader must stop the app, not slip through -------------------

def patched(provider, replace):
    """Route chosen tools to `replace[name](arguments, real_call)`, leaving the rest alone."""
    real_call = provider.bridge.call
    provider.bridge.call = lambda name, arguments=None: (
        replace[name](arguments, real_call) if name in replace else real_call(name, arguments))


def test_quotes_are_matched_to_contracts_by_id_never_by_position():
    standin, provider = connect("a")
    try:
        expiration = provider.expirations("AAPL")[2]

        def without_ids(arguments, real_call):               # sorted another way, and no id on any row
            rows = real_call("get_option_quotes", arguments)["data"]["quotes"]
            return {"data": {"quotes": [{k: v for k, v in r.items() if k != "instrument_id"}
                                        for r in reversed(rows)]}, "guide": ""}
        patched(provider, {"get_option_quotes": without_ids})
        with pytest.raises(DataError, match="no\\s+readable quotes"):
            provider.chain("AAPL", expiration)
    finally:
        provider.bridge.close()
    # One contract asked for and one answered is unambiguous, so that still works without an id.
    standin, provider = connect("a")
    try:
        chain = provider.chain("AAPL", provider.expirations("AAPL")[2])
        one = chain[5]
        patched(provider, {"get_option_quotes": lambda arguments, real_call: {"data": {"quotes": [
            {k: v for k, v in real_call("get_option_quotes", arguments)["data"]["quotes"][0].items()
             if k != "instrument_id"}]}, "guide": ""}})
        assert provider.quotes([one["symbol"]])[one["symbol"]]["bid"] == one["bid"]
    finally:
        provider.bridge.close()


def test_two_contracts_with_the_same_terms_are_refused():
    standin, provider = connect("a")
    try:
        def doubled(arguments, real_call):
            payload = real_call("get_option_instruments", arguments)
            rows = payload["data"]["results"]
            twin = {k: v for k, v in rows[0].items() if k not in ("chain_symbol", "chain_id")}
            return {"data": {"results": rows + [{**twin, "id": "unmarked-twin"}]}, "guide": ""}
        patched(provider, {"get_option_instruments": doubled})
        with pytest.raises(DataError, match="two different"):
            provider.chain("AAPL", provider.expirations("AAPL")[2])
    finally:
        provider.bridge.close()


def test_a_list_of_contracts_that_is_cut_short_is_refused():
    standin, provider = connect("a")
    try:
        expiration = provider.expirations("AAPL")[2]

        def first_page_only(arguments, real_call):
            payload = real_call("get_option_instruments", arguments)
            return {"data": {"results": payload["data"]["results"][:20],
                             "next": "https://api.robinhood.com/options/instruments/?page=2"}, "guide": ""}
        patched(provider, {"get_option_instruments": first_page_only})
        with pytest.raises(DataError, match="more contracts than it sent"):
            provider.chain("AAPL", expiration)
    finally:
        provider.bridge.close()


def test_pages_of_contracts_are_followed_when_the_tool_says_how():
    standin, provider = connect("a")
    try:
        expiration = provider.expirations("AAPL")[2]
        whole = len(provider.chain("AAPL", expiration))
        provider._instruments.clear()
        tool = provider.bridge.tools()["get_option_instruments"]
        schema = {**tool.input_schema, "properties": {**tool.input_schema["properties"], "cursor": {"type": "string"}}}
        provider.bridge._tools = {**provider.bridge.tools(),
                                  "get_option_instruments": tool.model_copy(update={"input_schema": schema})}
        asked = []

        def paged(arguments, real_call):
            asked.append(arguments.get("cursor"))
            rows = real_call("get_option_instruments", {k: v for k, v in arguments.items() if k != "cursor"})["data"]["results"]
            start = int(arguments.get("cursor") or 0)
            nxt = str(start + 15) if start + 15 < len(rows) else None
            return {"data": {"results": rows[start:start + 15], "next_cursor": nxt}, "guide": ""}
        patched(provider, {"get_option_instruments": paged})
        assert len(provider.chain("AAPL", expiration)) == whole and len(asked) > 2 and asked[0] is None
    finally:
        provider.bridge.close()


def test_bars_within_the_day_are_not_passed_off_as_daily_bars():
    standin, provider = connect("b")
    try:
        def intraday(arguments, real_call):
            rows = [{"date": f"2026-09-{day:02d}T{hour}:30:00Z", "open": 100, "high": 101, "low": 99, "close": 100.5,
                     "volume": 1000} for day in (28, 29, 30) for hour in (14, 15, 16, 17)]
            return {"data": {"bars": rows}, "guide": ""}
        patched(provider, {"get_stock_historicals": intraday})
        with pytest.raises(DataError, match="more than one bar per day"):
            provider._history("AAPL", "2026-09-01", "2026-10-01")
    finally:
        provider.bridge.close()
    standin, provider = connect("a")
    try:
        tool = provider.bridge.tools()["get_equity_historicals"]
        props = {**tool.input_schema["properties"], "interval": {"type": "string", "enum": ["5minute", "hour"]}}
        provider.bridge._tools = {**provider.bridge.tools(), "get_equity_historicals": tool.model_copy(
            update={"input_schema": {**tool.input_schema, "properties": props}})}
        with pytest.raises(DataError, match="does not offer daily bars"):
            provider._history("AAPL", "2026-01-01", "2026-10-01")
        assert not [c for c in standin.calls if c[0] == "get_equity_historicals"]
    finally:
        provider.bridge.close()


def test_volatility_and_greeks_in_other_units_are_set_aside_not_shown():
    from tradeassist.chain import analyze_chain
    demo = DemoData()
    expiration = demo.expirations("AAPL")[3]
    spot = demo.quotes(["AAPL"])["AAPL"]["last"]
    good = analyze_chain("AAPL", expiration, demo.chain("AAPL", expiration), spot, 0.04)
    assert good.notes == [] and all(c["greeks_source"] == "provider" for c in good.contracts
                                    if c["delta"] is not None and abs(c["delta"]) < 0.9)
    assert any(c["iv_source"] == "provider" for c in good.contracts)

    def altered(change):
        rows = demo.chain("AAPL", expiration)
        for c in rows:
            c["greeks"] = change(dict(c["greeks"]))
        return analyze_chain("AAPL", expiration, rows, spot, 0.04)

    percent = altered(lambda g: {**g, "mid_iv": g["mid_iv"] * 100})           # 24 instead of 0.24
    assert all(c["iv_source"] != "provider" and (c["iv"] is None or c["iv"] < 3) for c in percent.contracts)
    assert any("implied volatilities" in n for n in percent.notes)
    near = min((c for c in percent.contracts if c["model_value"]), key=lambda c: abs(c["strike"] - spot))
    assert near["model_value"] == pytest.approx(near["mid"], rel=0.25)        # the model is not wrecked

    yearly = altered(lambda g: {**g, "theta": g["theta"] * 365})              # theta per year
    assert all(c["greeks_source"] == "computed" for c in yearly.contracts if c["theta"] is not None)
    # The ratio the note prints is the median across the chain, which drifts a little with the demo
    # data's day: anything near 365 shows the per-year theta was caught.
    ratio = re.search(r"theta values are about (\d+(?:\.\d+)?) times", " ".join(yearly.notes))
    assert ratio and 300 <= float(ratio.group(1)) <= 430
    for c in yearly.contracts:
        t = good.find(c["symbol"])
        if c["theta"] is not None and t["theta"] is not None:
            assert c["theta"] == pytest.approx(t["theta"], rel=0.5, abs=0.01)

    per_one = altered(lambda g: {**g, "vega": g["vega"] * 100})               # vega per 1.00 of volatility
    assert all(c["greeks_source"] == "computed" for c in per_one.contracts if c["vega"] is not None)
    assert any("vega" in n for n in per_one.notes)

    flipped = altered(lambda g: {**g, "delta": -g["delta"]})                  # a call cannot have a negative delta
    assert all(c["greeks_source"] == "computed" for c in flipped.contracts
               if c["delta"] is not None and abs(c["delta"]) > 0.01)


def test_a_quote_with_no_time_is_flagged_on_the_pricing_screen(tmp_path, monkeypatch):
    standin, provider = connect("a")
    try:
        def untimed(arguments, real_call):
            payload = real_call("get_option_quotes", arguments)
            return {"data": {"quotes": [{k: v for k, v in r.items() if k != "updated_at"}
                                        for r in payload["data"]["quotes"]]}, "guide": ""}
        patched(provider, {"get_option_quotes": untimed})
        monkeypatch.setattr(provider, "clock", lambda: {"state": "open", "description": "", "date": "", "next_change": "",
                                                        "next_state": ""})
        settings = Settings(data_mode="robinhood", db_path=tmp_path / "t.db")
        client = TestClient(create_app(settings, TradeAssistant(settings, provider=provider, db=Database(":memory:"))))
        expiration = provider.expirations("AAPL")[3]
        spot = standin.demo.quotes(["AAPL"])["AAPL"]["last"]
        strike = min({c["strike"] for c in standin.demo.chain("AAPL", expiration)}, key=lambda k: abs(k - spot))
        r = client.post("/api/price", json={"symbol": "AAPL", "expiration": expiration, "strike": strike,
                                            "option_type": "call", "side": "buy", "contracts": 1})
        assert r.status_code == 200, r.text
        assert any("gave no time for this quote" in w["text"] for w in r.json()["warnings"])
    finally:
        provider.bridge.close()


def test_times_sent_as_numbers_in_text_are_read():
    assert iso_time("1791230398123").startswith("2026-10-05T") and iso_time("1791230398").startswith("2026-10-05T")
    assert iso_time(True) is None and iso_time("") is None
    from tradeassist.providers.robinhood import occ_symbol
    assert occ_symbol("BRK.B", "2026-11-20", "call", 502.5) == "BRKB261120C00502500"
    with pytest.raises(ValueError):
        occ_symbol("F", "2027-01-15", "CALL", 12)


# --- the connection itself ---------------------------------------------------------------------

class FakeClient:
    """A connected client whose behaviour each test sets."""
    def __init__(self, behaviour):
        self.behaviour, self.attempts = behaviour, 0

    async def list_tools(self):
        from types import SimpleNamespace
        tool = SimpleNamespace(name="get_equity_quotes", annotations=None, input_schema={})
        return SimpleNamespace(tools=[tool], next_cursor=None)

    async def call_tool(self, name, arguments):
        self.attempts += 1
        return await self.behaviour(self.attempts)


def fake_bridge(behaviour, **kwargs):
    from contextlib import asynccontextmanager
    made = []

    @asynccontextmanager
    async def connect_fake():
        client = FakeClient(behaviour)
        made.append(client)
        yield client
    return ReadOnlyBridge(connect_fake, ALLOWED_TOOLS, label="Robinhood", **kwargs), made


def test_a_request_that_gets_no_answer_is_not_repeated():
    import asyncio
    import time as clock

    async def hang(attempt):
        await asyncio.sleep(30)
    bridge, made = fake_bridge(hang, call_timeout=0.5)
    started = clock.time()
    with pytest.raises(DataError, match="did not answer within"):
        bridge.call("get_equity_quotes", {})
    assert clock.time() - started < 3 and sum(c.attempts for c in made) == 1 and len(made) == 1
    bridge.close()


def test_a_request_the_server_rejects_is_reported_as_such_and_not_repeated():
    from mcp import MCPError

    async def reject(attempt):
        raise MCPError(-32602, "Invalid params: unknown argument 'symbols'")
    bridge, made = fake_bridge(reject)
    with pytest.raises(DataError, match="did not accept the request: .*unknown argument"):
        bridge.call("get_equity_quotes", {})
    assert sum(c.attempts for c in made) == 1 and len(made) == 1
    bridge.close()


def test_a_connection_that_went_stale_is_reopened_once():
    from types import SimpleNamespace
    state = {"fail": False}

    async def flaky(attempt):
        if state["fail"]:
            state["fail"] = False
            raise ConnectionError("stream closed")
        return SimpleNamespace(content=[SimpleNamespace(text='{"data": {"ok": 1}}')], is_error=False,
                               structured_content=None)
    bridge, made = fake_bridge(flaky)
    assert bridge.call("get_equity_quotes", {}) == {"data": {"ok": 1}}
    state["fail"] = True
    assert bridge.call("get_equity_quotes", {}) == {"data": {"ok": 1}} and len(made) == 2   # reconnected once
    bridge.close()
    import threading
    assert not [t for t in threading.enumerate() if t.name == "mcp-bridge" and t.is_alive()]


def test_the_server_address_must_be_encrypted_unless_it_is_this_computer():
    from tradeassist.providers.mcp_bridge import check_server_url
    assert check_server_url("https://agent.robinhood.com/mcp/trading")
    assert check_server_url("http://127.0.0.1:8931/mcp") and check_server_url("http://localhost:1/mcp")
    for bad in ("http://agent.robinhood.com/mcp/trading", "ftp://x/y", "agent.robinhood.com"):
        with pytest.raises(ValueError, match="https"):
            check_server_url(bad)


# --- second review round ---------------------------------------------------------------------------

def textbook_chain(spot, days, vol, strikes, change=lambda g: g, rate=0.04):
    """A chain priced with the textbook formula, with a data source's Greeks attached."""
    from datetime import timezone
    from tradeassist.optionmath import black76_greeks, black76_price
    now = datetime(2026, 10, 5, 14, 0, tzinfo=timezone.utc)
    expiration = (now + timedelta(days=days)).date().isoformat()
    from tradeassist.optionmath import years_to_expiry
    t = years_to_expiry(expiration, now)
    forward = spot * (1 + rate * t)
    rows = []
    for strike in strikes:
        for kind in ("call", "put"):
            value = black76_price(forward, strike, t, rate, vol, kind)
            g = black76_greeks(spot, forward, strike, t, rate, vol, kind)
            bid, ask = round(max(value - 0.03, 0.01), 2), round(value + 0.03, 2)
            rows.append({"symbol": f"X{kind[0]}{strike}", "underlying": "X", "option_type": kind, "strike": strike,
                         "expiration": expiration, "bid": bid, "ask": ask, "bidsize": 10, "asksize": 10,
                         "last": value, "volume": 10, "open_interest": 100, "contract_size": 100,
                         "greeks": change({"delta": g.delta, "gamma": g.gamma, "theta": g.theta, "vega": g.vega,
                                           "mid_iv": vol, "smv_vol": None})})
    return expiration, rows, now


def test_unit_check_leaves_correct_greeks_alone_across_ordinary_chains():
    from tradeassist.chain import analyze_chain
    strikes = [80, 85, 90, 95, 100, 105, 110, 115, 120]
    for days, vol in [(2, 0.25), (7, 0.08), (30, 0.30), (30, 1.50), (365, 0.40), (700, 0.20)]:
        expiration, rows, now = textbook_chain(100.0, days, vol, strikes)
        a = analyze_chain("X", expiration, rows, 100.0, 0.04, now=now)
        assert a.notes == [], (days, vol, a.notes)
        atm = a.find_by_terms("call", 100)
        assert atm["iv"] == pytest.approx(vol, rel=0.1), (days, vol)          # whichever source was used, it is right
        if days >= 30:              # enough strikes near the money to compare units
            assert atm["greeks_source"] == "provider" and atm["iv_source"] == "provider", (days, vol)


def test_unit_check_is_not_made_on_the_last_day_when_it_cannot_be():
    """On expiration day this app's own theta means something else, so a mismatch proves nothing."""
    from tradeassist.chain import analyze_chain
    expiration, rows, now = textbook_chain(100.0, 0, 0.30, [96, 97, 98, 99, 100, 101, 102, 103, 104])
    for hour in (14, 17, 19):                                                  # morning, midday, last hour (UTC)
        a = analyze_chain("X", expiration, rows, 100.0, 0.04, now=now.replace(hour=hour, minute=30))
        assert a.notes == [], (hour, a.notes)
        atm = a.find_by_terms("call", 100)
        assert atm["iv_source"] == "provider" and atm["iv"] == pytest.approx(0.30)


def test_units_that_cannot_be_checked_are_not_trusted_and_percent_deltas_are_caught():
    from tradeassist.chain import analyze_chain
    # Two strikes only: too few quotes to compare, so the app's own figures are used.
    wrong = lambda g: {**g, "theta": g["theta"] * 365, "vega": g["vega"] * 100, "mid_iv": g["mid_iv"] * 10}
    expiration, rows, now = textbook_chain(100.0, 30, 0.25, [100], change=wrong)
    a = analyze_chain("X", expiration, rows, 100.0, 0.04, now=now)
    call = a.find_by_terms("call", 100)
    assert call["greeks_source"] == "computed" and call["iv_source"] == "computed"
    assert call["iv"] == pytest.approx(0.25, abs=0.03) and -0.2 < call["theta"] < 0
    # Deltas sent as percentages.
    expiration, rows, now = textbook_chain(100.0, 30, 0.25, [80, 85, 90, 95, 100, 105, 110, 115, 120],
                                           change=lambda g: {**g, "delta": g["delta"] * 100})
    a = analyze_chain("X", expiration, rows, 100.0, 0.04, now=now)
    assert all(c["greeks_source"] == "computed" and abs(c["delta"]) <= 1 for c in a.contracts)
    assert any("delta" in n for n in a.notes)


def test_scrubber_catches_the_ways_an_account_can_be_written_and_spares_market_data():
    from tradeassist.robinhood_check import scrub_schema, scrub_text
    for text in ("account (5QR12345)", "account number is 5QR12345", "Account: #5QR12345", "acct. 5QR12345",
                 '{"account_number": "5QR12345"}', "https://x.test/y?account_number=5QR12345", "user id 5QR12345",
                 "portfolio 5QR12345 is restricted"):
        assert "5QR12345" not in scrub_text(text), text
    assert "123-45-6789" not in scrub_text("SSN 123-45-6789") and "555" not in scrub_text("call (415) 555-0100 now")
    for keep in ("AAPL261106C00205000", "2026-10-05T19:59:58.123456Z", "1791230398123", "204.040000",
                 "1a2b3c4d-0000-4111-8222-333344445555", "https://api.robinhood.com/options/instruments/1a2b3c4d/",
                 "5year span supports only the week interval", "Get real-time equity quotes for up to 20 symbols."):
        assert scrub_text(keep) == keep, keep
    out = shorten({"session": "reg", "symbol": "AAPL", "portfolio": "5QR12345", "nickname": "Sam", "ip": "10.0.0.1"})
    assert out == {"session": "reg", "symbol": "AAPL", "portfolio": "[removed]", "nickname": "[removed]",
                   "ip": "[removed]"}
    # For tools the app does not call, only argument names and types are kept.
    schema = {"type": "object", "required": ["which"], "properties": {
        "which": {"type": "string", "const": "5QR12345", "default": "5QR12345", "enum": ["5QR12345"],
                  "description": "Signed in as Sam Example"}}}
    assert scrub_schema(schema, keep_values=False) == {"type": "object", "required": ["which"],
                                                       "properties": {"which": {"type": "string"}}}
    kept = scrub_schema({"properties": {"interval": {"type": "string", "enum": ["day", "week"]}}}, keep_values=True)
    assert kept["properties"]["interval"]["enum"] == ["day", "week"]


def test_scope_notes_only_stay_quiet_for_wording_that_is_plainly_read_only():
    from tradeassist.robinhood_check import scope_notes
    assert len(scope_notes("market_data:read offline_access")) == 1
    assert any("more than reading" in n for n in scope_notes("market_data trading"))
    for unclear in ("mcp", "internal", "agentic", "accounts"):
        assert any("not clear" in n for n in scope_notes(unclear)), unclear
    assert any("did not say" in n for n in scope_notes(None))
    assert any("more than reading" in n for n in scope_notes("agentic:all"))


def test_a_dropped_connection_is_reopened_not_reported_as_a_refusal():
    from types import SimpleNamespace
    from mcp import MCPError, types
    state = {"drop": False}

    async def behaviour(attempt):
        if state["drop"]:
            state["drop"] = False
            raise MCPError(types.CONNECTION_CLOSED, "Connection closed")
        return SimpleNamespace(content=[SimpleNamespace(text='{"data": 1}')], is_error=False, structured_content=None)
    bridge, made = fake_bridge(behaviour)
    assert bridge.call("get_equity_quotes", {}) == {"data": 1}
    state["drop"] = True
    assert bridge.call("get_equity_quotes", {}) == {"data": 1} and len(made) == 2
    bridge.close()


def test_the_list_of_callable_tools_cannot_be_swapped_after_the_bridge_is_built():
    bridge, _ = fake_bridge(None)
    with pytest.raises(AttributeError):
        bridge.allowed = frozenset({"get_accounts"})
    assert bridge.allowed == ALLOWED_TOOLS
    for account_tool in ("get_accounts", "get_holdings", "get_portfolio", "get_buying_power", "get_my_option_quotes"):
        with pytest.raises(ValueError):
            ReadOnlyBridge(lambda: None, frozenset({account_tool}))


def test_dates_written_as_digits_are_not_mistaken_for_clock_times():
    assert iso_time("20261005").startswith("2026-10-05") and iso_time("١٢٣٤٥٦٧٨٩٠") is None
    assert find_bars([{"date": "2026-10-05", "close": "1.0"}])[0]["date"] == "2026-10-05"


def test_a_stock_with_two_sets_of_contracts_needs_the_reply_to_say_which_is_which():
    standin, provider = connect("a")              # this server lists a standard and an adjusted chain
    try:
        def unmarked(arguments, real_call):
            rows = real_call("get_option_instruments", arguments)["data"]["results"]
            rows = [{k: v for k, v in r.items() if k not in ("chain_symbol", "chain_id")}
                    for r in rows if r["id"] != "adjusted-one"]
            return {"data": {"results": rows}, "guide": ""}
        patched(provider, {"get_option_instruments": unmarked})
        with pytest.raises(DataError, match="more than one set of option contracts"):
            provider.chain("AAPL", provider.expirations("AAPL")[2])
    finally:
        provider.bridge.close()


# --- what Robinhood's server was actually seen to send (capture of 2026-10-06) -------------------------

SEEN_STOCK_QUOTE = {"data": {"results": [{
    "quote": {"symbol": "AAPL", "last_trade_price": "333.130000", "venue_last_trade_time": "2026-10-05T19:59:59.939984769Z",
              "last_non_reg_trade_price": "333.080000", "venue_last_non_reg_trade_time": "2026-10-06T05:46:21.544Z",
              "adjusted_previous_close": "332.890000", "previous_close": "332.890000",
              "previous_close_date": "2026-10-05", "bid_price": "333.030000",
              "venue_bid_time": "2026-10-06T05:45:33.40453Z", "ask_price": "333.110000",
              "venue_ask_time": "2026-10-06T05:45:33.40453Z", "has_traded": True, "state": "active"},
    "close": {"symbol": "AAPL", "date": "2026-10-05", "price": "332.89", "interpolated": False,
              "source": "sip-list-exchange-close"}}]}, "guide": "..."}
SEEN_CHAINS = {"data": {"chains": [{
    "id": "7dd906e5-7d4b-4161-a3fe-2c3b62038482", "symbol": "AAPL", "can_open_position": True, "cash_component": None,
    "expiration_dates": ["2026-10-07", "2026-10-09", "2026-10-12"], "trade_value_multiplier": "100.0000",
    "underlying_instruments": [{"instrument": "http://edge-internal.example/instruments/450dfc6d/", "symbol": ""}],
    "min_ticks": {"above_tick": "0.05", "below_tick": "0.01", "cutoff_price": "3.00"},
    "late_close_state": "disabled", "extended_hours_state": "enabled", "settle_on_open": False,
    "sellout_time_to_expiration": 1800}]}, "guide": "..."}


def test_the_stock_quote_and_chain_replies_robinhood_really_sent_are_read_correctly():
    standin, provider = connect("c")
    try:
        patched(provider, {"get_equity_quotes": lambda arguments, real_call: SEEN_STOCK_QUOTE,
                           "get_option_chains": lambda arguments, real_call: SEEN_CHAINS})
        q = provider.quotes(["AAPL"])["AAPL"]
        assert q["last"] == 333.08                     # the after-hours trade is the newer of the two
        assert q["trade_time"] == "2026-10-06T05:46:21.544000+00:00"
        assert (q["bid"], q["ask"], q["prevclose"]) == (333.03, 333.11, 332.89)
        assert q["bid_time"] == q["ask_time"] == "2026-10-06T05:45:33.404530+00:00"
        assert provider.expirations("AAPL") == ["2026-10-07", "2026-10-09", "2026-10-12"]
        assert provider._chain_list("AAPL")["id"] == "7dd906e5-7d4b-4161-a3fe-2c3b62038482"
        assert provider._chain_list("AAPL")["min_ticks"]["below_tick"] == "0.01"
    finally:
        provider.bridge.close()
    # In regular hours the regular trade is the newer one and wins.
    from tradeassist.providers.robinhood import newest
    assert newest((1.0, "2026-10-06T14:00:00Z"), (2.0, "2026-10-06T12:00:00Z")) == (1.0, "2026-10-06T14:00:00+00:00")
    assert newest((None, "2026-10-06T14:00:00Z"), (2.0, None)) == (2.0, None) and newest((1.0, None), (2.0, None))[0] == 1.0
    assert iso_time("2026-10-05T19:59:59.939984769Z") == "2026-10-05T19:59:59.939984+00:00"   # nanoseconds


def test_requests_follow_the_argument_layouts_robinhood_declares():
    standin, provider = connect("c")
    try:
        expiration = provider.expirations("AAPL")[3]
        chain = provider.chain("AAPL", expiration)
        truth = standin.demo.chain("AAPL", expiration)
        assert len(chain) == len(truth)
        asked = [args for name, args in standin.calls if name == "get_option_instruments"]
        assert len(asked) == 2                                     # two pages, the cursor passed back as given
        assert all(a["chain_id"] == "chain-AAPL" and a["chain_symbol"] is None for a in asked)   # the id alone
        assert all(a["expiration_dates"] == expiration for a in asked) and asked[1]["cursor"] == "page:30"
        quote_calls = [args["instrument_ids"] for name, args in standin.calls if name == "get_option_quotes"]
        assert len(quote_calls) == 1 and len(quote_calls[0]) == len(truth)     # the whole expiration in one request
        assert ("get_equity_quotes", {"symbols": ["AAPL"]}) in standin.calls
        assert ("get_option_chains", {"underlying_symbol": "AAPL"}) in standin.calls
        assert all(c["min_ticks"] == {"above_tick": "0.05", "below_tick": "0.01", "cutoff_price": "3.00"} for c in chain)

        end = datetime.now(NY).date()
        start = end - timedelta(days=400)
        bars = provider._history("AAPL", start.isoformat(), end.isoformat())
        call = next(args for name, args in standin.calls if name == "get_equity_historicals")
        assert call == {"symbols": ["AAPL"], "start_time": f"{start}T00:00:00Z", "end_time": f"{end}T23:59:59Z",
                        "interval": "day"}                          # full times, as Robinhood requires
        truth = standin.demo.history("AAPL", start.isoformat(), end.isoformat())
        assert bars["date"].tolist() == truth["date"].tolist()      # 04:00Z stamps keep their New York day
        assert bars["close"].tolist() == pytest.approx(truth["close"].tolist())   # and the filler bar is dropped
        assert provider.earnings_raw("AAPL")["data"]["results"][0]["report_date"] == "2026-10-29"
        assert standin.forbidden_calls == []
    finally:
        provider.bridge.close()


def test_price_steps_stated_by_the_data_source_are_used_unless_a_quote_contradicts_them():
    from tradeassist.pricer import stated_tick_rule, tick_rule
    penny = {"above_tick": "0.05", "below_tick": "0.01", "cutoff_price": "3.00"}
    rule = tick_rule([0.10, 0.20, 3.10, 5.50], penny)               # these quotes alone would look like 5c/10c
    assert (rule.low, rule.high, rule.stated, rule.assumed) == (1, 5, True, False)
    assert tick_rule([0.10, 3.10], None).assumed                     # without a statement: the cautious guess
    assert stated_tick_rule(penny, [3.07]) is None                   # a quote between the stated steps
    assert tick_rule([3.07], penny).high == 1 and not tick_rule([3.07], penny).stated
    for odd in ({"above_tick": "0.25", "below_tick": "0.01", "cutoff_price": "3.00"},
                {"above_tick": "0.05", "below_tick": "0.01", "cutoff_price": "5.00"},
                {"above_tick": "x"}, "0.05", None):
        assert stated_tick_rule(odd) is None
    assert stated_tick_rule({"above_tick": "0.01", "below_tick": "0.01", "cutoff_price": "0.00"}).high == 1


def test_the_app_uses_the_stated_price_steps_on_the_pricing_screen(tmp_path):
    standin, provider = connect("c")
    try:
        settings = Settings(data_mode="robinhood", db_path=tmp_path / "t.db")
        client = TestClient(create_app(settings, TradeAssistant(settings, provider=provider, db=Database(":memory:"))))
        expiration = provider.expirations("AAPL")[3]
        spot = standin.demo.quotes(["AAPL"])["AAPL"]["last"]
        strike = min({c["strike"] for c in standin.demo.chain("AAPL", expiration)}, key=lambda k: abs(k - spot))
        req = {"symbol": "AAPL", "expiration": expiration, "strike": strike, "option_type": "call"}
        r = client.post("/api/price", json=req)
        assert r.status_code == 200, r.text
        assert "data source states" in json.dumps(r.json()["ladder"])
        live = client.post("/api/live", json=req)
        assert live.status_code == 200 and live.json()["bid_time"].startswith("2026-10-05T19:59:58.123")
    finally:
        provider.bridge.close()


def test_the_check_still_records_the_option_replies_when_the_stock_quote_fails(tmp_path):
    """So that one run of the check shows every mismatch, not just the first."""
    standin, provider = connect("c")
    try:
        patched(provider, {"get_equity_quotes": lambda arguments, real_call: {"data": {"results": [{"odd": 1}]}, "guide": ""}})
        result = run_check(provider, "AAPL", tmp_path / "c.json")
        by_step = {s["step"]: s for s in result["steps"]}
        assert not result["ok"] and not by_step["Stock quote"]["ok"]
        assert "contracts were read" in by_step["Option chain and Greeks"]["detail"]
        capture = json.loads((tmp_path / "c.json").read_text())
        assert {"get_option_instruments", "get_option_quotes"} <= {c["tool"] for c in capture["calls"]}
        guide = next(c for c in capture["calls"] if c["tool"] == "get_option_chains")["reply"]["guide"]
        assert guide.startswith("Prices are text")                  # Robinhood's usage notes are kept
    finally:
        provider.bridge.close()


def test_daily_bars_that_land_on_weekends_are_refused_as_misdated():
    standin, provider = connect("c")
    try:
        standin.bar_stamp = lambda day: f"{day:%Y-%m-%d}T02:00:00Z"      # the evening before, New York time
        end = datetime.now(NY).date()
        with pytest.raises(DataError, match="fall on a weekend"):
            provider._history("AAPL", (end - timedelta(days=300)).isoformat(), end.isoformat())
        standin.bar_stamp = lambda day: f"{day:%Y-%m-%d}T13:30:00Z"      # stamped at the open instead: fine
        bars = provider._history("AAPL", (end - timedelta(days=300)).isoformat(), end.isoformat())
        assert len(bars) > 150 and (bars["date"].dt.dayofweek < 5).all()
    finally:
        provider.bridge.close()


# --- every listed strike is offered, not just those near the stock price -----------------------------

def test_the_whole_expiration_is_listed_including_strikes_far_from_the_stock_price():
    standin, provider = connect("c")
    try:
        expiration = provider.expirations("AAPL")[3]
        truth = standin.demo.chain("AAPL", expiration)
        spot = standin.demo.quotes(["AAPL"])["AAPL"]["last"]
        chain = provider.chain("AAPL", expiration)
        assert {(c["strike"], c["option_type"]) for c in chain} == {(c["strike"], c["option_type"]) for c in truth}
        assert max(abs(c["strike"] / spot - 1) for c in chain) > 0.25           # well beyond the old 20% list
    finally:
        provider.bridge.close()


def test_large_quote_requests_fall_back_to_small_ones_when_refused_or_cut_short():
    for behaviour in ("refuse", "truncate"):
        standin, provider = connect("c")
        try:
            sizes = []

            def limited(arguments, real_call, behaviour=behaviour):
                ids = arguments["instrument_ids"]
                sizes.append(len(ids))
                if len(ids) > 20 and behaviour == "refuse":
                    raise DataError("The data source reported an error: at most 20 instrument_ids")
                return real_call("get_option_quotes", {"instrument_ids": ids[:20]})
            patched(provider, {"get_option_quotes": limited})
            expiration = provider.expirations("AAPL")[3]
            truth = {c["symbol"]: c for c in standin.demo.chain("AAPL", expiration)}
            chain = provider.chain("AAPL", expiration)
            assert len(chain) == len(truth) and all(c["bid"] == pytest.approx(truth[c["symbol"]]["bid"]) for c in chain)
            assert sizes[0] == len(truth) and all(n <= 20 for n in sizes[1:]), (behaviour, sizes)
            sizes.clear()
            provider.chain("AAPL", expiration)                                  # and it remembers
            assert all(n <= 20 for n in sizes), (behaviour, sizes)
        finally:
            provider.bridge.close()


def test_a_listed_contract_with_no_quote_is_still_shown():
    standin, provider = connect("c")
    try:
        def one_missing(arguments, real_call):
            payload = real_call("get_option_quotes", arguments)
            payload["data"]["results"] = payload["data"]["results"][1:]
            return payload
        patched(provider, {"get_option_quotes": one_missing})
        expiration = provider.expirations("AAPL")[3]
        chain = provider.chain("AAPL", expiration)
        assert len(chain) == len(standin.demo.chain("AAPL", expiration))
        blank = [c for c in chain if c["bid"] is None and c["ask"] is None]
        assert len(blank) == 1 and blank[0]["strike"] > 0 and blank[0]["greeks"] == {}
        from tradeassist.chain import analyze_chain
        analysis = analyze_chain("AAPL", expiration, chain, 204.0, 0.04)        # and the analysis copes with it
        assert analysis.find(blank[0]["symbol"])["mid"] is None
    finally:
        provider.bridge.close()
