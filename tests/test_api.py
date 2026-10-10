import json

import pytest
from fastapi.testclient import TestClient

from tradeassist.api import create_app
from tradeassist.config import Settings, load_settings
from tradeassist.db import Database
from tradeassist.patterns import PATTERNS
from tradeassist.providers.demo import DemoData
from tradeassist.service import TradeAssistant, clean

V2_WRITES = {
    "/v2/positions", "/v2/positions/{position_id}/executions", "/v2/positions/{position_id}/plan",
    "/v2/positions/{position_id}/cancel", "/v2/positions/{position_id}/snapshot",
    "/v2/position/evaluate", "/v2/options/evaluate", "/v2/options/compare", "/v2/portfolio/stress",
    "/v2/signals/history/build", "/v2/backtest", "/v2/backtest/walkforward", "/v2/alerts/check",
    "/v2/reversal/options",
    # 2.1.1: correcting a recorded fill (audited) and rebuilding snapshots. Records only; no orders.
    "/v2/positions/{position_id}/executions/{execution_id}/correct", "/v2/snapshots/backfill",
    # 2.2: borrow fees, dividends and splits; how a bought option ended. Records only; no orders.
    "/v2/positions/{position_id}/adjustments", "/v2/positions/{position_id}/adjustments/{adjustment_id}/void",
    "/v2/positions/{position_id}/option/expire", "/v2/positions/{position_id}/option/exercise",
    "/v2/positions/{position_id}/option/adjust",
    # 2.3: recompute stored figures from the fills, after a backup. Records only; no orders.
    "/v2/diagnostics/repair",
    # 2.4: options lab. Models only; no orders.
    "/v2/options/montecarlo",
    # 2.4: strategy builder. Models only; no orders and nothing recorded.
    "/v2/strategies/build",
    "/v2/strategies/parse",
    # 2.4: AI briefs and headline tone. They call the AI model; nothing is recorded but usage counts.
    "/v2/ai/market-brief",
    "/v2/ai/brief/{symbol}",
    "/v2/ai/tone",
}

THESIS = "Expect a bounce from the low end of the range within three weeks; wrong below the recent low."


@pytest.fixture()
def app(tmp_path):
    settings = Settings(data_mode="demo", db_path=tmp_path / "t.db", account_size=50_000,
                        watchlist=["AAPL", "MSFT", "KO", "COST"])
    assistant = TradeAssistant(settings, provider=DemoData(), db=Database(":memory:"))
    assistant._traded_today = lambda: True          # the tests must not depend on the day they run
    return TestClient(create_app(settings, assistant)), assistant


def contract(client, symbol="AAPL", option_type="call", min_dte=25, otm=1.03):
    exps = client.get("/api/expirations", params={"symbol": symbol}).json()
    exp = next(e["date"] for e in exps["expirations"] if e["dte"] >= min_dte)
    chain = client.get("/api/chain", params={"symbol": symbol, "expiration": exp}).json()
    rows = [c for c in chain["contracts"] if c["option_type"] == option_type]
    row = min(rows, key=lambda c: abs(c["strike"] - chain["spot"] * otm))
    return {"symbol": symbol, "expiration": exp, "option_type": option_type, "strike": row["strike"]}, row


def test_status_reports_mode_and_that_no_orders_are_placed(app):
    client, _ = app
    s = client.get("/api/status").json()
    assert s["data"]["mode"] == "demo" and s["data"]["realtime"] is False
    assert s["places_orders"] is False and s["limits"]["account_size"] == 50_000


def test_no_endpoint_can_place_an_order(app):
    client, _ = app
    paths = client.get("/api/openapi.json").json()["paths"]
    assert not [p for p in paths if "order" in p.lower()]
    writes = sorted(p for p, methods in paths.items() if "post" in methods and p.startswith("/api/"))
    assert writes == ["/api/backtest", "/api/journal/{trade_id}/attempt", "/api/journal/{trade_id}/cancel",
                      "/api/journal/{trade_id}/close", "/api/live", "/api/patterns/run", "/api/price",
                      "/api/simulate", "/api/snapshot", "/api/ticket", "/api/walkforward"]
    # The V2 writes only record what you did at the broker, or run analyses. Each one is listed here
    # so that adding a route means looking at it again.
    v2 = sorted(p for p, methods in paths.items() if "post" in methods and p.startswith("/v2/"))
    assert set(v2) <= V2_WRITES, set(v2) - V2_WRITES


def test_price_returns_ladder_scorecard_and_warnings(app):
    client, _ = app
    req, row = contract(client)
    r = client.post("/api/price", json={**req, "contracts": 2})
    assert r.status_code == 200
    data = r.json()
    lad = data["ladder"]
    assert lad["ok"] and row["bid"] <= lad["rungs"][0]["price"] <= row["ask"]
    assert data["warnings"][0]["level"] == "fail" and "made up" in data["warnings"][0]["text"]
    keys = {c["key"] for c in data["scorecard"]["checks"]}
    assert {"spread", "open_interest", "iv_vs_hv", "earnings", "theta", "dte", "breakeven", "trade_risk"} <= keys
    assert data["scorecard"]["premium"] == pytest.approx(data["assumed_limit"] * 200)
    json.dumps(data)                                                         # fully JSON-serialisable

    sell = client.post("/api/price", json={**req, "side": "sell"}).json()
    assert sell["ladder"]["side"] == "sell" and "scorecard" not in sell


def test_bad_requests_get_clear_messages(app):
    client, _ = app
    req, _ = contract(client)
    assert client.post("/api/price", json={**req, "strike": 1.23}).status_code == 400
    assert "must be call or put" in client.post("/api/price", json={**req, "option_type": "straddle"}).json()["error"]
    assert client.post("/api/price", json={**req, "contracts": 0}).status_code == 422
    assert client.get("/api/expirations", params={"symbol": "!!"}).status_code == 400
    assert client.post("/api/price", json={**req, "expiration": "2001-01-05"}).status_code == 502
    assert client.post("/api/price", json={**req, "earnings_date": "soon"}).status_code == 400
    assert client.get("/api/chart/AAPL", params={"pattern": "nope"}).status_code == 400
    assert client.post("/api/backtest", json={"symbols": ["AAPL"], "params": {"leverage": 9}}).status_code == 400
    assert client.get("/secrets.env").status_code == 404
    assert client.get("/..%2Frun.py").status_code == 404


def test_ticket_enforces_limits_and_saves_to_the_journal(app):
    client, assistant = app
    req, row = contract(client, otm=1.06)
    limit = row["ask"]
    base = {**req, "contracts": 1, "limit_price": row["bid"] + 0.01, "max_limit": limit, "thesis": THESIS,
            "earnings_date": "2030-01-15", "overrides": ["earnings_estimate"]}

    no_thesis = client.post("/api/ticket", json={**base, "thesis": "yolo"}).json()
    assert not no_thesis["can_save"] and any(v["key"] == "thesis" for v in no_thesis["blocking"])
    assert client.post("/api/ticket", json={**base, "thesis": "yolo", "save": True}).status_code == 400

    too_big = client.post("/api/ticket", json={**base, "contracts": 400}).json()
    sizing = next(v for v in too_big["violations"] if v["key"] == "trade_risk")
    assert sizing["overridable"] is False and not too_big["can_save"]
    forced = client.post("/api/ticket", json={**base, "contracts": 400, "overrides": ["trade_risk"]}).json()
    assert not forced["can_save"] and "trade_risk" in forced["ignored_overrides"]     # sizing cannot be overridden

    above = client.post("/api/ticket", json={**base, "max_limit": limit + 1.0}).json()
    assert any(v["key"] == "above_ask" for v in above["blocking"])
    assert client.post("/api/ticket", json={**base, "max_limit": 0.01, "limit_price": 5}).status_code == 400

    ok = client.post("/api/ticket", json={**base, "save": True}).json()
    assert ok["can_save"] and ok["saved_id"] == 1
    assert ok["plan"]["premium_at_risk"] == pytest.approx(limit * 100)
    assert ok["plan"]["stop_price"] == pytest.approx(limit * 0.5, abs=0.006)
    trade = client.get("/api/journal/1").json()
    assert trade["status"] == "planned" and trade["thesis"] == THESIS and trade["data_source"] == "demo"
    assert assistant.journal.open_risk() == 0


def test_earnings_block_needs_an_explicit_override_and_is_recorded(app):
    client, _ = app
    req, row = contract(client, otm=1.06)
    exp = req["expiration"]
    base = {**req, "contracts": 1, "limit_price": row["ask"], "max_limit": row["ask"], "thesis": THESIS,
            "earnings_date": exp}
    blocked = client.post("/api/ticket", json=base).json()
    v = next(v for v in blocked["violations"] if v["key"] == "earnings")
    assert v["overridable"] and not blocked["can_save"]
    saved = client.post("/api/ticket", json={**base, "overrides": ["earnings"], "save": True}).json()
    assert saved["saved_id"] and client.get(f"/api/journal/{saved['saved_id']}").json()["overrides"] == ["earnings"]


def test_journal_flow_through_the_api_and_open_risk_feeds_the_next_ticket(app):
    client, assistant = app
    req, row = contract(client, otm=1.06)
    body = {**req, "contracts": 1, "limit_price": row["ask"], "max_limit": row["ask"], "thesis": THESIS,
            "earnings_date": "2030-01-15", "save": True}
    tid = client.post("/api/ticket", json=body).json()["saved_id"]
    attempt = {"side": "buy", "bid": row["bid"], "ask": row["ask"], "limit_price": row["ask"], "filled": True,
               "seconds_to_fill": 2}
    assert client.post(f"/api/journal/{tid}/attempt", json=attempt).json()["status"] == "open"
    assert assistant.journal.open_risk() == pytest.approx(row["ask"] * 100)
    again = client.post("/api/price", json=req).json()
    total = next(c for c in again["scorecard"]["checks"] if c["key"] == "open_risk")
    assert total["value"].startswith(f"${row['ask'] * 100 + again['assumed_limit'] * 100:,.0f}")
    assert client.post(f"/api/journal/{tid}/close", json={"exit_price": -1}).status_code == 422
    closed = client.post(f"/api/journal/{tid}/close", json={"exit_price": 0, "note": "expired"}).json()
    assert closed["status"] == "closed" and closed["pnl"] == pytest.approx(-row["ask"] * 100)
    assert client.post(f"/api/journal/{tid}/cancel", json={}).status_code == 400
    page = client.get("/api/journal").json()
    assert page["stats"]["overall"]["trades"] == 1 and page["calibration"]["total_attempts"] == 1


def save_until_refused(client, symbol):
    req, row = contract(client, symbol=symbol, otm=1.06)
    body = {**req, "contracts": 1, "limit_price": row["ask"], "max_limit": row["ask"], "thesis": THESIS,
            "earnings_date": "2030-01-15", "overrides": ["earnings_estimate"], "save": True}
    saved = 0
    while saved < 100:
        r = client.post("/api/ticket", json=body)
        if r.status_code != 200:
            return saved, row["ask"] * 100, r.json()["error"]
        saved += 1
    raise AssertionError("no limit ever stopped the tickets")


def test_one_idea_cannot_be_split_across_tickets_to_beat_the_per_trade_limit(app):
    client, assistant = app
    per_trade_cap = 50_000 * 0.02
    saved, cost, reason = save_until_refused(client, "AAPL")
    assert "Premium at risk" in reason and saved >= 1
    committed = assistant.journal.committed_risk("AAPL")
    assert committed <= per_trade_cap < committed + cost                    # stopped exactly at the cap


def test_saved_plans_use_up_the_total_risk_limit(app):
    """Plans count before they fill: ticket after ticket across symbols must run into the total cap."""
    client, assistant = app
    total_cap = 50_000 * 0.10
    reasons = []
    for symbol in ["AAPL", "MSFT", "KO", "COST", "JPM", "HD", "PEP", "V"]:
        reasons.append(save_until_refused(client, symbol)[2])
    assert any("Total premium at risk" in r for r in reasons)
    assert assistant.journal.open_risk() == 0                               # nothing has filled
    assert assistant.journal.planned_risk() <= total_cap
    assert assistant.journal.planned_risk() > total_cap - 1_000


def test_entered_earnings_date_that_contradicts_filings_needs_a_recorded_override(app):
    client, assistant = app
    exps = client.get("/api/expirations", params={"symbol": "AAPL"}).json()["expirations"]
    from tradeassist.optionmath import NY
    estimate = assistant._estimated_earnings("AAPL", __import__("datetime").datetime.now(NY).date())["date"]
    later = next(e["date"] for e in exps if e["date"] > estimate)           # expires after the estimated report
    chain = client.get("/api/chain", params={"symbol": "AAPL", "expiration": later}).json()
    row = min((c for c in chain["contracts"] if c["option_type"] == "call"),
              key=lambda c: abs(c["strike"] - chain["spot"] * 1.08))
    base = {"symbol": "AAPL", "expiration": later, "option_type": "call", "strike": row["strike"], "contracts": 1,
            "limit_price": row["ask"], "max_limit": row["ask"], "thesis": THESIS}
    blocked = client.post("/api/ticket", json=base).json()
    assert any(v["key"] == "earnings" for v in blocked["blocking"])          # the estimate blocks it
    dodge = client.post("/api/ticket", json={**base, "earnings_date": "2031-06-01"}).json()
    keys = {v["key"] for v in dodge["blocking"]}
    assert "earnings_estimate" in keys and "earnings" not in keys and not dodge["can_save"]
    ok = client.post("/api/ticket", json={**base, "earnings_date": "2031-06-01",
                                          "overrides": ["earnings_estimate"], "save": True}).json()
    assert client.get(f"/api/journal/{ok['saved_id']}").json()["overrides"] == ["earnings_estimate"]


def test_non_finite_numbers_are_rejected(app):
    client, _ = app
    req, row = contract(client, otm=1.06)
    base = {**req, "contracts": 1, "limit_price": row["ask"], "max_limit": row["ask"], "thesis": THESIS, "save": True}
    for field in ("limit_price", "max_limit", "stop_loss_pct", "strike"):
        for bad in ("NaN", "Infinity"):
            raw = json.dumps({**base, field: "@@"}).replace('"@@"', bad)
            r = client.post("/api/ticket", content=raw, headers={"Content-Type": "application/json"})
            assert r.status_code == 422, (field, bad, r.status_code)
    assert client.get("/api/journal").json()["trades"] == []


def test_a_past_earnings_date_cannot_switch_the_check_off(app):
    client, _ = app
    req, _ = contract(client)
    data = client.post("/api/price", json={**req, "earnings_date": "2020-01-01"}).json()
    assert data["earnings"]["estimated"] and data["earnings"]["date"] >= "2026"   # the estimate is used instead
    assert any("has passed" in w["text"] for w in data["warnings"])
    far = client.post("/api/price", json={**req, "earnings_date": "2031-06-01"}).json()
    assert far["earnings"]["date"] == "2031-06-01"
    assert any("more than three weeks" in w["text"] for w in far["warnings"])


def test_pricing_records_todays_implied_volatility_once(app):
    client, assistant = app
    req, _ = contract(client)
    assert assistant.iv_rank("AAPL")["days"] == 0
    client.post("/api/price", json=req)
    client.post("/api/price", json=req)
    rank = assistant.iv_rank("AAPL")
    assert rank["days"] == 1 and rank["fresh"] is True


def test_nothing_is_recorded_when_the_market_has_not_traded(app):
    client, assistant = app
    assistant._traded_today = lambda: False
    req, _ = contract(client)
    client.post("/api/price", json=req)
    assert assistant.iv_rank("AAPL")["days"] == 0
    snap = client.post("/api/snapshot", json={"symbols": ["AAPL"]}).json()
    assert snap["saved"] == [] and "not traded today" in snap["skipped"]


def test_daily_loss_stop_blocks_new_tickets(app):
    client, assistant = app
    tid = assistant.journal.create(data_source="demo", symbol="AAPL", option_symbol="X", option_type="call",
                                   strike=1, expiration="2030-01-01", contracts=10, thesis="t", planned_limit=2,
                                   max_limit=2, plan={}, quote={}, scorecard=[], signals=[], overrides=[])
    assistant.journal.log_attempt(tid, side="buy", bid=1.9, ask=2.0, limit_price=2.0, filled=True)
    assistant.journal.close_at(tid, 0.0)                                      # lost $2,000 today: 4% of the account
    req, row = contract(client, otm=1.06)
    t = client.post("/api/ticket", json={**req, "contracts": 1, "limit_price": row["ask"], "max_limit": row["ask"],
                                         "thesis": THESIS, "earnings_date": "2030-01-15"}).json()
    stop = next(v for v in t["violations"] if v["key"] == "daily_loss")
    assert stop["overridable"] and not t["can_save"]


def test_snapshot_builds_iv_history_and_rank(app):
    client, assistant = app
    snap = client.post("/api/snapshot", json={"symbols": ["AAPL", "KO"]}).json()
    assert len(snap["saved"]) == 2 and snap["failed"] == []
    assert 0.05 < snap["saved"][0]["atm_iv"] < 1.0
    rank = assistant.iv_rank("AAPL")
    assert rank["days"] == 1 and rank["rank"] is None                         # one day cannot make a rank
    client.post("/api/snapshot", json={"symbols": ["AAPL"]})
    assert assistant.iv_rank("AAPL")["days"] == 1                             # same day overwrites, no duplicate


def test_research_patterns_and_backtests_run_end_to_end(app):
    client, assistant = app
    brief = client.get("/api/research/aapl").json()
    assert brief["symbol"] == "AAPL" and len(brief["chart"]) == 252 and brief["regime"]["available"]
    assert not brief["company"]["available"] and brief["trend"]["readings"]
    study = client.post("/api/patterns/run", json={"symbols": ["AAPL", "MSFT", "KO"], "years": 4,
                                                   "horizons": [5, 10]}).json()
    assert study["tests_run"] == 2 * len(PATTERNS) == 40 and study["data_source"] == "demo"
    assert client.get("/api/patterns").json()["last_run"]["run_id"] == study["run_id"]
    assert not study["verdicts_given"] and study["benchmark"] == "SPY"        # three symbols: numbers, no verdicts
    assert {r["verdict"] for r in study["results"]} == {"insufficient"}
    priced = client.post("/api/price", json=contract(client)[0]).json()
    for pattern in priced["recent_patterns"]:
        assert [v["horizon"] for v in pattern["verdicts"]] == [5, 10]          # lab results now attached
    assert assistant._validated_patterns()["hammer"] == [{"horizon": 5, "verdict": "insufficient"},
                                                         {"horizon": 10, "verdict": "insufficient"}]
    chart = client.get("/api/chart/AAPL", params={"pattern": "macd_bull_cross"}).json()
    assert len(chart["bars"]) == 260 and len(chart["events"]) >= 1
    assert set(chart["events"]) <= {b["date"] for b in chart["bars"]}
    bt = client.post("/api/backtest", json={"symbols": ["AAPL", "MSFT", "KO"], "years": 3,
                                            "params": {"stop_loss_pct": 0.06}, "n_trials": 10}).json()
    assert bt["params"]["stop_loss_pct"] == 0.06 and bt["significance"]["n_trials"] == 10
    assert 740 <= bt["trading_days"] <= 790          # the demo calendar has no holidays: about 261 days a year
    assert len(bt["equity_curve"]) == bt["trading_days"]
    wf = client.post("/api/walkforward", json={"symbols": ["AAPL", "MSFT", "KO"], "years": 3}).json()
    assert wf["folds"] and wf["combinations_tried"] == 18
    assert client.get("/").status_code == 200 and "Trade Assistant" in client.get("/").text
    assert client.get("/app.js").status_code == 200


def test_settings_loading(monkeypatch, tmp_path):
    for key in ("TRADIER_LIVE_TOKEN", "TRADIER_SANDBOX_TOKEN", "DATA_MODE", "ACCOUNT_SIZE", "WATCHLIST",
                "SEC_USER_AGENT", "TRADEASSIST_DB"):
        monkeypatch.delenv(key, raising=False)
    empty = tmp_path / "none.env"
    empty.write_text("")
    assert load_settings(empty).resolved_mode == "demo"
    monkeypatch.setenv("TRADIER_SANDBOX_TOKEN", "your_sandbox_token_here")     # placeholder from env.example
    assert load_settings(empty).resolved_mode == "demo"
    monkeypatch.setenv("TRADIER_SANDBOX_TOKEN", "abc123")
    assert load_settings(empty).resolved_mode == "sandbox"
    monkeypatch.setenv("TRADIER_LIVE_TOKEN", "live456")
    monkeypatch.setenv("ACCOUNT_SIZE", "25000")
    monkeypatch.setenv("WATCHLIST", "spy, qqq")
    s = load_settings(empty)
    assert s.resolved_mode == "live" and s.account_size == 25000 and s.watchlist == ["SPY", "QQQ"]
    monkeypatch.setenv("DATA_MODE", "demo")
    assert load_settings(empty).resolved_mode == "demo"                        # explicit choice wins over tokens
    monkeypatch.setenv("ACCOUNT_SIZE", "lots")
    with pytest.raises(ValueError, match="ACCOUNT_SIZE"):
        load_settings(empty)
    with pytest.raises(ValueError):
        _ = Settings(data_mode="live").resolved_mode


def test_clean_makes_values_json_safe():
    import numpy as np
    import pandas as pd
    out = clean({"a": np.float64("nan"), "b": np.int64(3), "c": [np.bool_(True), float("inf")],
                 "d": pd.Timestamp("2026-01-02"), 5: np.array([1.5, np.nan])})
    assert out == {"a": None, "b": 3, "c": [True, None], "d": "2026-01-02T00:00:00", "5": [1.5, None]}


class CountingDemo(DemoData):
    """Demo data that counts requests and lets a test move one option's quote."""
    def __init__(self):
        super().__init__()
        self.quote_calls, self.chain_calls, self.override = [], 0, {}

    def quotes(self, symbols):
        self.quote_calls.append(list(symbols))
        self._inside_quotes = True               # the demo builds option quotes from its own chain: not a request
        try:
            out = super().quotes(symbols)
        finally:
            self._inside_quotes = False
        for symbol, changes in self.override.items():
            if symbol in out and changes:
                out[symbol] = {**out[symbol], **changes}
        return out

    def chain(self, symbol, expiration):
        if not getattr(self, "_inside_quotes", False):
            self.chain_calls += 1
        return super().chain(symbol, expiration)


def test_live_quote_uses_one_small_request_and_rebuilds_the_ladder(tmp_path):
    settings = Settings(data_mode="demo", db_path=tmp_path / "t.db", account_size=50_000)
    provider = CountingDemo()
    assistant = TradeAssistant(settings, provider=provider, db=Database(":memory:"))
    assistant._traded_today = lambda: False
    client = TestClient(create_app(settings, assistant))
    req, row = contract(client, otm=1.05)
    priced = client.post("/api/price", json=req).json()
    assert priced["data"]["poll_seconds"] == 1.0 and priced["data"]["received_at"].endswith("+00:00")

    provider.quote_calls.clear()
    chains_before = provider.chain_calls
    first = client.post("/api/live", json=req).json()
    assert provider.quote_calls == [["AAPL", priced["contract"]["symbol"]]]      # stock and option in one request
    assert provider.chain_calls == chains_before                                  # the full chain is reused
    assert first["bid"] == row["bid"] and first["ask"] == row["ask"]
    assert [r["price"] for r in first["ladder"]["rungs"]] == [r["price"] for r in priced["ladder"]["rungs"]]
    assert 0 <= first["quote_age_ms"] < 5_000 and first["fetch_ms"] >= 0
    assert len(first["received_at"].split(".")[1].split("+")[0]) == 3            # milliseconds

    # The market moves: the next update must price from the new quote, on valid increments.
    occ = priced["contract"]["symbol"]
    provider.override[occ] = {"bid": round(row["bid"] + 0.20, 2), "ask": round(row["ask"] + 0.30, 2),
                              "bid_time": "2026-10-05T14:30:00.123000+00:00",
                              "ask_time": "2026-10-05T14:30:00.456000+00:00"}
    moved = client.post("/api/live", json=req).json()
    assert moved["bid"] == pytest.approx(row["bid"] + 0.20) and moved["ask"] == pytest.approx(row["ask"] + 0.30)
    assert moved["bid_time"].startswith("2026-10-05T14:30:00.123")               # the source's own timestamp, to the ms
    prices = [r["price"] for r in moved["ladder"]["rungs"]]
    assert all(moved["bid"] <= p <= moved["ask"] for p in prices) and prices != [r["price"] for r in first["ladder"]["rungs"]]
    assert moved["quote_age_ms"] > 1_000

    # After a minute the chain behind the live view is fetched again.
    assistant._chains[("AAPL", req["expiration"])]["at"] -= 61
    client.post("/api/live", json=req)
    assert provider.chain_calls == chains_before + 1

    assert client.post("/api/live", json={**req, "side": "sell"}).json()["ladder"]["side"] == "sell"
    assert client.post("/api/live", json={**req, "strike": 1.23}).status_code == 400
    assert client.post("/api/live", json={**req, "option_type": "x"}).status_code == 400
    provider.override[occ] = None
    provider.quotes = lambda symbols: {}                                           # the source returns nothing
    assert client.post("/api/live", json=req).status_code == 502


def test_moving_demo_prices_are_opt_in_and_quote_options():
    fixed, moving = DemoData(), DemoData(live=True)
    assert fixed.clock()["state"] in ("open", "closed") and moving.clock()["state"] == "open"
    quote = fixed.quotes(["AAPL"])["AAPL"]
    assert quote["last"] == quote["close"] == fixed.quotes(["AAPL"])["AAPL"]["last"]
    assert abs(moving.quotes(["AAPL"])["AAPL"]["last"] / quote["last"] - 1) < 0.005     # a wobble, not a jump
    expiration = fixed.expirations("BRK.B")[3]
    chain = fixed.chain("BRK.B", expiration)
    option = fixed.quotes([chain[10]["symbol"], "BRK.B", "NOPE99"])
    assert option[chain[10]["symbol"]]["bid"] == chain[10]["bid"] and option[chain[10]["symbol"]]["type"] == "option"
    assert set(option) == {chain[10]["symbol"], "BRK.B"}


def test_the_browser_is_told_not_to_reuse_an_old_copy_of_the_page(app):
    from tradeassist import __version__
    client, _ = app
    page = client.get("/")
    assert page.headers["cache-control"] == "no-cache"
    assert f'/app.js?v={__version__}-' in page.text and f'/app.css?v={__version__}-' in page.text
    script = client.get("/app.js")
    assert script.status_code == 200 and script.headers["cache-control"] == "no-cache"
    assert client.get("/api/status").json()["version"] == __version__
