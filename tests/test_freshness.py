"""Market-data freshness, failures and recovery (2.2, Sprint 5)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from tradeassist import freshness, portfolio
from tradeassist.api import create_app
from tradeassist.config import Settings, load_settings
from tradeassist.db import Database
from tradeassist.market import Market
from tradeassist.optionmath import NY
from tradeassist.providers.base import DataError
from tradeassist.providers.demo import DemoData
from tradeassist.service import TradeAssistant

UTC = timezone.utc


def ny(*a):
    return datetime(*a, tzinfo=NY)


def obs(observed, now, session="OPEN", realtime=True, **kw):
    return freshness.observe("STOCK_QUOTE", "AAPL", "test", observed.isoformat() if observed else None,
                             retrieved_at=now, session=session, realtime=realtime, **kw)


# --- 4.1 freshness -------------------------------------------------------------------------------------

def test_4_1_fresh_stale_and_delayed_while_the_market_is_open():
    now = ny(2026, 10, 7, 11, 0)                                                  # a Wednesday morning
    assert obs(now - timedelta(seconds=30), now)["freshness"] == "FRESH"
    stale = obs(now - timedelta(minutes=10), now)
    assert stale["freshness"] == "STALE" and stale["age_seconds"] == 600 and "10 minutes old" in stale["warnings"]
    assert obs(now - timedelta(minutes=16), now, realtime=False)["freshness"] == "DELAYED"     # sandbox: 15 min by design
    assert obs(now - timedelta(minutes=25), now, realtime=False)["freshness"] == "STALE"
    # Options are allowed longer than stocks before they are stale.
    opt = freshness.observe("OPTION_QUOTE", "AAPL C", "test", (now - timedelta(minutes=4)).isoformat(), retrieved_at=now,
                            session="OPEN")
    assert opt["freshness"] == "FRESH"


def test_4_1_a_weekend_price_is_old_but_not_stale():
    sat = ny(2026, 10, 10, 12, 0)
    assert obs(ny(2026, 10, 9, 15, 59, 58), sat, session="CLOSED")["freshness"] == "LAST_SESSION"
    assert obs(ny(2026, 10, 7, 15, 59), sat, session="CLOSED")["freshness"] == "STALE"          # Wednesday's
    monday_pre = ny(2026, 10, 12, 8, 0)
    assert obs(ny(2026, 10, 9, 16, 0), monday_pre, session="CLOSED")["freshness"] == "LAST_SESSION"
    # One extra day is allowed for a market holiday, which is not in a calendar here.
    assert obs(ny(2026, 10, 8, 15, 59), sat, session="CLOSED")["freshness"] == "LAST_SESSION"


def test_4_1_unknown_times_and_clock_problems_are_flagged():
    now = ny(2026, 10, 7, 11, 0)
    none = obs(None, now)
    assert none["freshness"] == "UNKNOWN" and "no time" in none["warnings"][0]
    future = obs(now + timedelta(minutes=10), now)
    assert any("in the future" in w for w in future["warnings"])
    cached = obs(now - timedelta(seconds=5), now, from_cache=True, cache_reason="the source timed out")
    assert cached["freshness"] == "STALE" and cached["from_cache"] and "a saved copy, because the source timed out" in cached["warnings"]


def test_4_1_thresholds_are_configurable(monkeypatch, tmp_path):
    assert freshness.thresholds({"FRESHNESS_STOCK_QUOTE_SECONDS": "15"})["STOCK_QUOTE"] == 15
    with pytest.raises(ValueError, match="number of seconds"):
        freshness.thresholds({"FRESHNESS_OPTION_QUOTE_SECONDS": "soon"})
    with pytest.raises(ValueError, match="zero or more"):
        freshness.thresholds({"FRESHNESS_OPTION_QUOTE_SECONDS": "-1"})
    monkeypatch.setenv("FRESHNESS_STOCK_QUOTE_SECONDS", "30")
    assert load_settings(tmp_path / "none.env").freshness["STOCK_QUOTE"] == 30
    now = ny(2026, 10, 7, 11, 0)
    assert obs(now - timedelta(seconds=45), now, limits={"STOCK_QUOTE": 30})["freshness"] == "STALE"


def test_4_1_quote_completeness():
    assert freshness.quote_completeness({"last": 10.0, "bid": 9.9, "ask": 10.1}) == ("COMPLETE", [])
    status, warnings = freshness.quote_completeness({"last": 10.0, "bid": 10.2, "ask": 10.1})
    assert status == "PARTIAL" and "crossed quote" in warnings[0]
    assert freshness.quote_completeness({"last": 10.0})[0] == "PARTIAL"
    assert freshness.quote_completeness({"last": None, "bid": None, "ask": None})[0] == "MISSING"
    assert freshness.quote_completeness({"last": -1.0, "bid": 0.0, "ask": 0.0})[1][0] == "invalid last -1.0"
    assert freshness.quote_completeness(None) == ("MISSING", ["no quote returned"])


# --- 4.2 failures and recovery -----------------------------------------------------------------------------

class Flaky(DemoData):
    """Demo data that can be made to fail, counting the requests that reach it."""

    def __init__(self):
        super().__init__()
        self.down = False
        self.calls = 0

    def quotes(self, symbols):
        self.calls += 1
        if self.down:
            raise DataError("Tradier request failed for /markets/quotes after 3 tries (HTTP 503)")
        return super().quotes(symbols)

    def history(self, symbol, start, end):
        self.calls += 1
        if self.down:
            raise DataError("Tradier request failed for /markets/history after 3 tries (timeout)")
        return super().history(symbol, start, end)


def market():
    settings = Settings(data_mode="demo")
    return Market(Flaky(), Database(":memory:"), settings)


def test_4_2_a_failed_quote_is_never_quietly_replaced():
    m = market()
    good = m.quote("AAPL")
    # Demo data is not real-time: during market hours its quotes read DELAYED, after hours LAST_SESSION.
    assert good["_meta"]["freshness"] in ("FRESH", "LAST_SESSION", "DELAYED") and not good["_meta"]["from_cache"]
    m.provider.down = True
    with pytest.raises(DataError, match="503"):
        m.quote("AAPL")                                             # pricing never gets an old quote
    old = m.quote("AAPL", allow_stale=True)                         # a read-only view may, marked as such
    assert old["last"] == good["last"] and old["_meta"]["freshness"] == "STALE" and old["_meta"]["from_cache"]
    assert any("the source failed" in w for w in old["_meta"]["warnings"])
    with pytest.raises(DataError):
        m.quote("MSFT", allow_stale=True)                           # nothing saved for it: no made-up price


def test_4_2_repeated_failures_pause_requests_instead_of_hammering_the_source():
    m = market()
    m.quote("AAPL")
    m.provider.down = True
    for _ in range(freshness.FAILURES_TO_PAUSE):
        with pytest.raises(DataError):
            m.quote("AAPL")
    calls = m.provider.calls
    with pytest.raises(DataError, match="paused"):
        m.quote("AAPL")
    assert m.provider.calls == calls                                 # the paused request never left the app
    assert m.quote("AAPL", allow_stale=True)["_meta"]["from_cache"]
    h = m.health.summary()
    assert h["status"] == "PAUSED" and h["consecutive_failures"] == 3 and h["recent_failures"][-1]["what"] == "quote AAPL"
    m.health.paused_until = 0                                        # the pause ends
    m.provider.down = False
    m.quote("AAPL")
    assert m.health.summary()["status"] == "OK" and m.health.consecutive_failures == 0


def test_4_2_history_failures_fall_back_to_saved_bars_and_say_so():
    m = market()
    bars = m.bars("AAPL", 1)
    m.db.query("UPDATE bars_meta SET fetched_at = fetched_at - 86400 * 2")      # force a refresh
    m.provider.down = True
    again = m.bars("AAPL", 1)
    assert len(again) == len(bars)
    info, fresh = m.bar_info("AAPL"), m.bars_freshness("AAPL")
    assert "timeout" in info["refresh_failed"] and fresh["freshness"] == "STALE" and fresh["from_cache"]
    m2 = market()
    m2.provider.down = True
    with pytest.raises(DataError):
        m2.bars("AAPL", 1)                                           # nothing saved: the failure shows


def test_4_2_source_health_counts():
    clock = [1000.0]
    h = freshness.SourceHealth(failures_to_pause=2, pause_seconds=10, clock=lambda: clock[0])
    assert h.summary()["status"] == "NOT_USED_YET"
    h.success()
    h.failure("quote X", "timeout")
    assert h.summary()["status"] == "DEGRADED" and h.paused() == 0
    h.failure("quote X", "timeout")
    assert h.paused() == 10 and h.summary()["status"] == "PAUSED"
    clock[0] += 11
    assert h.paused() == 0


# --- 4.3 shown with the results -------------------------------------------------------------------------------

def test_4_3_positions_and_portfolio_carry_freshness(tmp_path):
    settings = Settings(data_mode="demo", db_path=tmp_path / "t.db", sandbox_token="secret-token-xyz")
    svc = TradeAssistant(settings, provider=DemoData(), db=Database(":memory:"))
    c = TestClient(create_app(settings, svc))
    p = c.post("/v2/positions", json={"symbol": "AAPL", "quantity": 10, "invalidation_price": 1, "entry": {"price": 100}}).json()
    f = p["mark"]["freshness"]
    assert f["kind"] == "STOCK_QUOTE" and f["provider"] == "Demo data (synthetic)" and f["freshness"] in ("FRESH", "LAST_SESSION", "DELAYED")
    d = c.get("/v2/data/AAPL").json()
    assert d["quote_freshness"]["instrument"] == "AAPL" and d["bars_freshness"]["kind"] == "DAILY_BARS"
    h = c.get("/v2/source/health").json()
    assert h["status"] == "OK" and h["successes"] > 0 and "secret-token-xyz" not in str(h)


def test_4_3_stale_prices_are_named_in_the_portfolio():
    stale = {"freshness": "STALE", "warnings": ["2.0 hours old"]}
    v = {"id": 1, "symbol": "AAPL", "direction": "LONG", "instrument_type": "STOCK", "multiplier": 1,
         "description": "Long 10 AAPL", "bias": 1, "invalidation_price": 90.0, "thesis_check": {"status": "INTACT"},
         "legs": [], "ledger": {"open_quantity": 10}, "status": "OPEN",
         "mark": {"price": 100.0, "underlying": 100.0, "freshness": stale}, "executions": []}
    daily = portfolio.daily_loss([], "REALIZED_PLUS_UNREALIZED", 10_000, 3.0, datetime.now(UTC))
    r = portfolio.analyse([v], {}, 10_000, 10.0, daily, lambda s: None)
    assert r["positions"][0]["quote_freshness"] == "STALE" and r["coverage"]["stale"][0]["description"] == "Long 10 AAPL"
    w = next(w for w in r["warnings"] if w.get("code") == "STALE_PRICES")
    assert "priced from a stale or undated quote" in w["text"]
    assert r["totals"]["gross_exposure"] == 1_000                    # still counted, but flagged
