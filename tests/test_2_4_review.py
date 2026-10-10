"""Regression tests for the independent review of 2.4 (docs/IMPROVEMENTS.md, "Independent review
of 2.4"). Each test reproduces one finding and fails on the code before the fix."""

from __future__ import annotations

import math
import threading
from datetime import date, datetime, timedelta

import numpy as np
import pandas as pd
import pytest
from fastapi.testclient import TestClient
from scipy.special import ndtr

import tradeassist.api as api_module
from tradeassist import makeup as mk
from tradeassist import news as nw
from tradeassist.ai import AIClient, AIError, extract_json, parse_response
from tradeassist.api import create_app
from tradeassist.briefs import makeup_text
from tradeassist.config import Settings
from tradeassist.db import Database
from tradeassist.optionmath import NY
from tradeassist.options import strategies as st
from tradeassist.options import volatility as vol
from tradeassist.providers.demo import DemoData
from tradeassist.scanner import flow_summary
from tradeassist.service import TradeAssistant
from test_ai_news import FakeAnthropic, FakeFinnhub, Reply, answer, finnhub_rows, make_app, settings


# --- security and integration ------------------------------------------------------------------

def test_S1_the_test_client_name_is_not_a_production_host(tmp_path, monkeypatch):
    assert "testserver" not in api_module.ALLOWED_HOSTS
    monkeypatch.setattr(api_module, "TEST_HOSTS", ())
    s = Settings(data_mode="demo", db_path=tmp_path / "t.db")
    svc = TradeAssistant(s, provider=DemoData(), db=Database(tmp_path / "t.db"))
    c = TestClient(create_app(s, svc))                       # the client sends Host: testserver
    assert c.get("/api/status").status_code == 400
    assert c.post("/v2/ai/market-brief", json={}, headers={"Origin": "http://testserver"}).status_code == 400
    assert TestClient(create_app(s, svc), base_url="http://127.0.0.1").get("/api/status").status_code == 200
    svc.db.close()


@pytest.mark.parametrize("reply", [
    '{"strategy": ["long_strangle"], "symbol": "KO"}',
    '{"strategy": "long_strangle", "symbol": "KO", "dte": 1e999}',
    '{"strategy": "long_strangle", "symbol": "KO", "contracts": Infinity, "delta": NaN}',
    '{"strategy": "long_strangle", "symbol": "KO", "dte": true, "strikes": [1e999, 2]}',
])
def test_S2_strange_numbers_from_the_model_never_cause_a_server_error(tmp_path, reply):
    c, svc, fake = make_app(tmp_path, ai_replies=[Reply(200, answer(reply))])
    r = c.post("/v2/strategies/parse", json={"text": "a wide bet on Coca-Cola moving"})
    assert r.status_code in (200, 400), r.text
    if r.status_code == 200:
        spec = r.json()["parsed"]["spec"]
        assert "dte" not in spec and "contracts" not in spec and "strikes" not in spec
    svc.db.close()


def test_S2_S9_answers_that_cannot_be_read_are_not_kept(tmp_path):
    c, svc, fake = make_app(tmp_path, ai_replies=[Reply(200, answer('{"items": [{"i": 1e999, "tone": 1}]}'))])
    for _ in range(2):
        r = c.post("/v2/ai/tone", json={"symbol": "KO"})
        assert r.status_code == 503 and "could not be read" in r.json()["error"]
    assert len(fake.calls) == 2                               # asked again, not served from the cache
    fake.replies = [Reply(200, answer("no json at all"))]
    c.post("/v2/ai/brief/KO", json={"web_search": False})
    c.post("/v2/ai/brief/KO", json={"web_search": False})
    assert len(fake.calls) == 4
    svc.db.close()


@pytest.mark.parametrize("body", [
    [1, 2, 3], {"content": {"type": "text"}}, {"content": [{"type": "text", "text": "ok", "citations": ["x", 5]}]},
    {"content": [{"type": "web_search_tool_result", "content": "nope"}], "usage": {"input_tokens": "n/a"}},
    {"content": [{"type": "server_tool_use", "name": "web_search", "input": "q"}], "usage": {"input_tokens": 1e999}},
    {"content": [None, 3, {"type": "text", "text": 5}], "usage": [], "stop_reason": 7},
])
def test_S3_any_reply_shape_is_read_without_an_error(body):
    p = parse_response(body)
    assert isinstance(p["text"], str) and p["usage"]["input_tokens"] >= 0


def test_S3_a_malformed_reply_is_still_counted(tmp_path):
    c, svc, fake = make_app(tmp_path, ai_replies=[Reply(200, {"content": {"bad": 1}, "usage": {"input_tokens": "x"}})])
    r = c.post("/v2/ai/market-brief", json={"web_search": False})
    assert r.status_code == 200 and r.json()["brief"] is None
    assert svc.ai.status()["used_today"]["requests"] == 1
    svc.db.close()


def test_S4_one_bad_finnhub_row_does_not_stop_the_news(tmp_path):
    rows = finnhub_rows() + [{"headline": 12345}, {"headline": "Odd summary", "summary": {"x": 1}, "datetime": 1e300},
                             {"headline": "Milliseconds", "datetime": 1760000000000}, {"headline": "Bad url", "url": "httpx://x"}]
    db = Database(tmp_path / "t.db")
    client = nw.NewsClient(settings(tmp_path, finnhub_api_key="fh"), db, None, session=FakeFinnhub(body=rows))
    out = client.company("ACME")
    heads = {i["headline"]: i for i in out["items"]}
    assert out["errors"] == [] and "Odd summary" in heads and heads["Odd summary"]["published"] is None
    assert heads["Milliseconds"]["published"].startswith("2025-10") and heads["Bad url"]["url"] is None
    db.close()


def test_S5_overlapping_requests_cannot_pass_the_daily_limit(tmp_path):
    db = Database(tmp_path / "t.db")

    class Slow(FakeAnthropic):
        def post(self, *a, **kw):
            import time
            time.sleep(0.05)
            return super().post(*a, **kw)

    fake = Slow(Reply(200, answer("ok")))
    ai = AIClient(settings(tmp_path, ai_daily_requests=2), db, session=fake)
    results = []

    def go(i):
        try:
            ai.ask(purpose="t", prompt=f"p{i}")
            results.append("ok")
        except AIError:
            results.append("limit")

    threads = [threading.Thread(target=go, args=(i,)) for i in range(10)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert results.count("ok") == 2 and len(fake.calls) == 2 and ai.status()["used_today"]["requests"] == 2
    db.close()


def test_S6_measures_that_could_not_be_taken_are_unknown():
    short = {f"S{i}": pd.DataFrame({"date": pd.bdate_range("2026-01-02", periods=200), "close": np.linspace(100, 110, 200),
                                    "high": np.linspace(100, 110, 200), "low": np.linspace(100, 110, 200),
                                    "sma50": np.nan, "sma200": np.nan}) for i in range(3)}
    b = mk.breadth(short)
    assert b["stocks"] == 3 and b["new_highs"] is None and b["new_lows"] is None and b["above_200d"] is None
    m = {"checked_at": "2026-10-10T10:00:00", "data_source": "live", "indexes": [], "regime": None, "sectors": [],
         "options": [], "breadth": {**b, "universe": "large_caps"}}
    assert "unknown new 52-week highs" in makeup_text(m)

    class A:
        expiration = "2026-10-16"
        contracts = [{"option_type": "call", "strike": 100, "volume": 900, "open_interest": None, "mid": 1.0},
                     {"option_type": "put", "strike": 100, "volume": None, "open_interest": None, "mid": 1.0}]
    f = flow_summary([A()])
    assert f["call_volume"] == 900 and f["put_volume"] is None and f["put_call_volume"] is None
    assert f["call_open_interest"] is None and f["unusual"] == [] and f["unusual_unmeasured"] == 1
    assert f["expirations"][0]["put_volume"] is None and f["expirations"][0]["put_call_volume"] is None


def test_S7_another_site_cannot_set_off_costly_reads(tmp_path):
    c, svc, _ = make_app(tmp_path)
    for path in ("/v2/scan/technical", "/v2/news/KO", "/v2/market/makeup", "/api/status"):
        assert c.get(path, headers={"Sec-Fetch-Site": "cross-site"}).status_code == 403
        assert c.get(path, headers={"Sec-Fetch-Site": "same-site"}).status_code == 403
    assert c.get("/v2/news/KO", headers={"Sec-Fetch-Site": "same-origin"}).status_code == 200
    assert c.get("/v2/news/KO").status_code == 200                       # typed address or a tool: no header
    assert c.get("/", headers={"Sec-Fetch-Site": "cross-site"}).status_code == 200   # the page itself
    svc.db.close()


def test_S7_news_windows_are_few():
    assert [min(nw.DAY_CHOICES, key=lambda d: (abs(d - x), d)) for x in (1, 2, 5, 9, 20, 30)] == [1, 1, 3, 7, 14, 30]


def test_S8_a_turn_that_never_finishes_is_an_error_and_every_call_counts(tmp_path):
    db = Database(tmp_path / "t.db")
    fake = FakeAnthropic(Reply(200, answer("part", stop="pause_turn")))
    ai = AIClient(settings(tmp_path), db, session=fake)
    with pytest.raises(AIError, match="still searching"):
        ai.ask(purpose="t", prompt="p", web_search=True)
    assert len(fake.calls) == 3 and ai.status()["used_today"]["requests"] == 3
    db.close()


def test_S10_deep_nesting_and_odd_links():
    assert extract_json("[" * 50000 + "]" * 50000) is None
    assert extract_json("{" * 30000) is None


# --- the trade reader ------------------------------------------------------------------------------

TODAY = date(2026, 10, 10)          # a Saturday
KNOWN = {"AAPL", "KO", "SPY", "AMZN", "F", "LOW"}


def P(text, today=TODAY, **kw):
    return st.parse(text, KNOWN, today=today, **kw)


@pytest.mark.parametrize("text,strategy", [
    ("sell AAPL put spread 16 delta", "bull_put_spread"), ("sell 5 wide put spread on AAPL 16 delta", "bull_put_spread"),
    ("short the AAPL straddle", "short_straddle"), ("sell AAPL strangle 16 delta", "short_strangle"),
    ("selling a 30 delta AAPL call spread", "bear_call_spread"), ("a short-term bullish call spread on AAPL", "bull_call_spread"),
    ("AAPL long-term call spread", "bull_call_spread"), ("buy an AAPL put spread", "bear_put_spread"),
])
def test_H1_selling_anywhere_in_the_sentence_is_read(text, strategy):
    assert P(text)["spec"]["strategy"] == strategy


@pytest.mark.parametrize("text,reason", [
    ("sell AAPL puts 30 days", "Selling a single call or put"), ("buy the AAPL 190 call and sell the 200 call", "more than one leg"),
    ("short an AAPL butterfly", "sold butterfly"), ("reverse iron condor on AAPL", "reverse"),
])
def test_H1_trades_that_are_not_templates_are_refused_not_flipped(text, reason):
    with pytest.raises(st.StrategyError, match=reason):
        P(text)


@pytest.mark.parametrize("text", ["buy AAPL call at 30 delta", "AAPL bull call spread at 40 delta 30 days",
                                  "AAPL long put near 30 dte", "AAPL straddle around 45 days"])
def test_H2_a_delta_or_a_day_count_is_not_a_strike(text):
    assert "center" not in P(text)["spec"]


def test_H2_a_centre_far_from_the_price_is_refused(tmp_path):
    c, svc, _ = make_app(tmp_path)
    r = c.post("/v2/strategies/build", json={"symbol": "AAPL", "strategy": "long_call", "center": 30})
    assert r.status_code == 400 and "far from the stock price" in r.json()["error"]
    svc.db.close()


def test_H3_strike_pairs_and_widths_are_not_counts():
    p = P("AMZN bull call 25/30 spread")["spec"]
    assert p["strikes"] == [25, 30] and "contracts" not in p
    q = P("sell 5 point wide put spread on KO")["spec"]
    assert q["width"] == 5 and "contracts" not in q
    assert P("KO bull call 60/65 spread 3 contracts")["spec"]["contracts"] == 3


def test_M1_a_price_for_a_delta_built_strategy_is_reported_as_unused():
    p = P("KO bull put spread at 60")
    assert "center" not in p["spec"] and any("was not used" in a for a in p["assumed"])
    assert any("0.30 delta" in a for a in p["assumed"])


def test_M2_dates_and_strikes_written_with_slashes():
    p = P("F 10/12 call spread 11/20")["spec"]
    assert p["strikes"] == [10, 12] and p["dte"] == (date(2026, 11, 20) - TODAY).days
    q = P("F 10/11/12 call butterfly 12/19")["spec"]
    assert q["strikes"] == [10, 11, 12] and q["dte"] == (date(2026, 12, 19) - TODAY).days
    r = P("KO 55/60 put spread exp 11/20 or 12/18")["spec"]
    assert r["strikes"] == [55, 60]


def test_M3_relative_dates_delta_signs_and_lone_strikes():
    assert P("KO straddle next week")["spec"]["dte"] == 6
    assert P("KO straddle next friday")["spec"]["dte"] == 6
    tuesday = date(2026, 10, 13)
    assert P("KO straddle next week", today=tuesday)["spec"]["dte"] == 10
    assert P("KO straddle this friday", today=tuesday)["spec"]["dte"] == 3
    late = date(2026, 10, 20)
    assert P("KO condor Oct", today=late)["spec"]["dte"] == (date(2027, 10, 15) - late).days
    assert P("AAPL 25Δ put")["spec"]["delta"] == 0.25
    spec = P("buy 2 AAPL 230 calls Dec 19")["spec"]
    assert spec["center"] == 230 and spec["contracts"] == 2 and spec["strategy"] == "long_call"


# --- moving strikes, and the Monte Carlo ------------------------------------------------------------

class _Uneven:
    """Strikes 2.5 apart near 100 and 5 apart further out."""

    def __init__(self):
        self.expiration, self.spot, self.forward, self.t = "2026-11-09", 100.0, 100.0, 30 / 365
        ks = [85, 90, 95, 97.5, 100, 102.5, 105, 110, 115]
        self.contracts = []
        for k in ks:
            d1 = (math.log(100 / k) + 0.045 * self.t) / (0.3 * math.sqrt(self.t))
            for kind, delta in (("call", float(ndtr(d1))), ("put", float(ndtr(d1)) - 1)):
                self.contracts.append({"option_type": kind, "strike": float(k), "bid": 1.0, "ask": 1.1, "mid": 1.05,
                                       "delta": delta})

    def find_by_terms(self, kind, strike):
        return next((c for c in self.contracts if c["option_type"] == kind and c["strike"] == strike), None)


def test_M5_moving_a_butterfly_keeps_its_wings_even():
    t, c = st.TEMPLATES["call_butterfly"], _Uneven()
    up = st.adjust(t, c, None, [97.5, 100.0, 102.5], {"op": "up"})
    assert up == [100.0, 102.5, 105.0]
    up2 = st.adjust(t, c, None, up, {"op": "up"})
    assert up2 == [100.0, 105.0, 110.0]                    # no 107.5 listed: both wings widen to 5, still even


def test_H4_no_earnings_variance_is_removed_without_the_jump(tmp_path):
    c, svc, _ = make_app(tmp_path)
    exps = c.get("/api/expirations", params={"symbol": "AAPL"}).json()["expirations"]
    exp = next(e["date"] for e in exps if e["dte"] >= 30)
    chain = c.get("/api/chain", params={"symbol": "AAPL", "expiration": exp}).json()
    k = min((x for x in chain["contracts"] if x["option_type"] == "call"), key=lambda x: abs(x["moneyness_pct"]))
    body = {"symbol": "AAPL", "expiration": exp, "option_type": "call", "strike": k["strike"], "earnings_jump": True}
    plain = c.post("/v2/options/montecarlo", json={**body, "earnings_jump": False}).json()
    today = datetime.now(NY).date()
    # A report later today (after the close) still falls inside the option's life; one already past does not,
    # nor does one on the expiration day itself.
    for when, jumped in ((today - timedelta(days=3), False), (date.fromisoformat(exp), False),
                         (today, True), (today + timedelta(days=5), True)):
        svc.lab.volatility = lambda symbol, n=6, w=when: {"event": {"available": True, "move_pct": 4.0},
                                                          "earnings": {"date": w.isoformat()}}
        m = c.post("/v2/options/montecarlo", json=body).json()
        if jumped:
            assert m["event"] is not None and m["volatility"] < plain["volatility"]
        else:
            assert m["event"] is None and m["volatility"] == pytest.approx(plain["volatility"])
            assert any("No earnings jump was added" in n for n in m["notes"])
    svc.db.close()


def test_L1_L2_thinned_checks_are_labelled_and_the_formula_is_hidden_near_the_money():
    long_dated = vol.monte_carlo(spot=100, forward=104, t=2.0, rate=0.04, sigma=0.3, strike=120, option_type="call",
                                 paths=4000)
    assert long_dated["steps"] == 252 and long_dated["steps_reduced"] is True
    assert any("about every 2.0 trading days" in n for n in long_dated["notes"])
    atm = vol.monte_carlo(spot=100, forward=100.3, t=30 / 365, rate=0.04, sigma=0.3, strike=100.2, option_type="call")
    assert atm["p_touch_close_formula"] is None and atm["p_touch_any_time"] is not None


def test_L4_iv30_says_how_it_was_found():
    rows = [{"dte": 45, "atm_iv": 0.3}, {"dte": 60, "atm_iv": 0.32}]
    assert vol.iv30(rows) == 0.3 and "45-day" in vol.iv30_basis(rows)
    assert vol.iv30_basis([{"dte": 20, "atm_iv": 0.3}, {"dte": 40, "atm_iv": 0.3}]) == "interpolated"


def test_L5_stocks_without_two_usable_closes_are_unreadable():
    ok = pd.DataFrame({"date": pd.bdate_range("2026-01-02", periods=2), "close": [10.0, 11.0], "high": [10, 11], "low": [10, 11],
                       "sma50": np.nan, "sma200": np.nan})
    one = ok.tail(1)
    nan_last = ok.assign(close=[10.0, np.nan])
    b = mk.breadth({"OK": ok, "ONE": one, "NAN": nan_last})
    assert (b["stocks"], b["advancing"], b["unchanged"], b["unusable"]) == (1, 1, 0, 2)
