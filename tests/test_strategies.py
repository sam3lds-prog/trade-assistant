"""2.4 strategy builder: templates resolved against listed contracts, adjustments, the risk
check and the plain-language reader."""

from __future__ import annotations

import math
from datetime import date

import pytest
from fastapi.testclient import TestClient
from scipy.special import ndtr

from tradeassist.api import create_app
from tradeassist.config import Settings
from tradeassist.db import Database
from tradeassist.features import FEATURE_DEFAULTS
from tradeassist.options import strategies as st
from tradeassist.providers.demo import DemoData
from tradeassist.service import TradeAssistant


class _Chain:
    """A tidy chain: strikes 80 to 120 by 5 around a 100 stock, 30% volatility, 30 days."""

    def __init__(self, expiration="2026-11-09", spot=100.0, step=5.0, skip=()):
        self.expiration, self.spot, self.forward, self.t = expiration, spot, spot, 30 / 365
        self.contracts = []
        k = 80.0
        while k <= 120.0 + 1e-9:
            d1 = (math.log(spot / k) + 0.5 * 0.09 * self.t) / (0.3 * math.sqrt(self.t))
            for kind, delta in (("call", float(ndtr(d1))), ("put", float(ndtr(d1)) - 1)):
                if (kind, k) in skip:
                    continue
                intrinsic = max((spot - k) if kind == "call" else (k - spot), 0)
                mid = round(intrinsic + 3.4 * math.exp(-((k - spot) / 12) ** 2) + 0.2, 2)
                self.contracts.append({"option_type": kind, "strike": k, "bid": mid - 0.05, "ask": mid + 0.05, "mid": mid,
                                       "delta": delta, "iv": 0.3, "spread_pct": 10 / mid, "symbol": f"X{kind}{k:g}"})
            k += step

    def find_by_terms(self, kind, strike):
        return next((c for c in self.contracts if c["option_type"] == kind and c["strike"] == strike), None)


# --- choosing contracts -----------------------------------------------------------------------

def test_contract_pickers():
    c = _Chain()
    assert st.at_the_money(c) == 100
    assert st.by_delta(c, "call", 0.50)["strike"] == 100
    assert st.by_delta(c, "put", 0.25)["strike"] == st.by_delta(c, "put", -0.25)["strike"] < 100
    assert st.beyond(c, "call", 100, 7.4, 1)["strike"] == 105 and st.beyond(c, "call", 100, 8, 1)["strike"] == 110
    assert st.beyond(c, "put", 100, 1, -1)["strike"] == 95                 # at least one strike away
    assert st.step(c, "call", 100, 2) == 110 and st.step(c, "put", 100, -1) == 95
    with pytest.raises(st.StrategyError, match="above"):
        st.step(c, "call", 120, 1)
    with pytest.raises(st.StrategyError, match="above"):
        st.beyond(c, "call", 120, 5, 1)
    one_sided = _Chain()
    for x in one_sided.contracts:
        x["bid"] = 0
    with pytest.raises(st.StrategyError, match="No quoted"):
        st.by_delta(one_sided, "call", 0.3)


def test_pick_expiration():
    dates = ["2026-10-10", "2026-10-16", "2026-11-20", "2026-12-18", "2027-01-15"]
    today = date(2026, 10, 10)
    assert st.pick_expiration(dates, today, 45) == "2026-11-20"
    assert st.pick_expiration(dates, today, 0) == "2026-10-16"              # never today's
    assert st.pick_expiration(dates, today, 30, after="2026-11-20", min_gap=7) == "2026-12-18"
    with pytest.raises(st.StrategyError):
        st.pick_expiration(dates, today, 30, after="2027-01-15")


@pytest.mark.parametrize("key,expected", [
    ("long_call", [100]), ("long_put", [100]),
    ("bull_call_spread", [100, 105]), ("bear_put_spread", [95, 100]),
    ("long_straddle", [100, 100]), ("short_straddle", [100, 100]),
    ("iron_butterfly", [95, 100, 100, 105]), ("call_butterfly", [95, 100, 105]), ("put_butterfly", [95, 100, 105]),
    ("call_calendar", [100, 100]),
])
def test_default_strikes(key, expected):
    strikes, why = st.choose_strikes(st.TEMPLATES[key], _Chain(), {})
    assert strikes == expected and why
    st.check_order(st.TEMPLATES[key], strikes)


def test_credit_spreads_condors_and_strangles_follow_delta_and_width():
    c = _Chain()
    put = st.by_delta(c, "put", 0.30)["strike"]
    assert st.choose_strikes(st.TEMPLATES["bull_put_spread"], c, {})[0] == [put - 5, put]
    assert st.choose_strikes(st.TEMPLATES["bull_put_spread"], c, {"width": 10})[0] == [put - 10, put]
    call = st.by_delta(c, "call", 0.30)["strike"]
    assert st.choose_strikes(st.TEMPLATES["bear_call_spread"], c, {})[0] == [call, call + 5]
    ic, _ = st.choose_strikes(st.TEMPLATES["iron_condor"], c, {"delta": 16, "width": 5})
    assert ic == [st.by_delta(c, "put", 0.16)["strike"] - 5, st.by_delta(c, "put", 0.16)["strike"],
                  st.by_delta(c, "call", 0.16)["strike"], st.by_delta(c, "call", 0.16)["strike"] + 5]
    sg, _ = st.choose_strikes(st.TEMPLATES["long_strangle"], c, {"delta": 0.25})
    assert sg[0] < 100 < sg[1]
    with pytest.raises(st.StrategyError, match="delta"):
        st.choose_strikes(st.TEMPLATES["iron_condor"], c, {"delta": 0.001})
    with pytest.raises(st.StrategyError, match="meet"):
        st.choose_strikes(st.TEMPLATES["iron_condor"], c, {"delta": 0.6})


def test_a_butterfly_with_a_missing_wing_says_so():
    c = _Chain(skip={("call", 105.0)})
    strikes, why = st.choose_strikes(st.TEMPLATES["call_butterfly"], c, {})
    assert strikes == [95, 100, 110] and any("uneven" in w for w in why)


def test_strike_order_is_checked():
    ic = st.TEMPLATES["iron_condor"]
    st.check_order(ic, [80, 90, 110, 120])
    for bad in ([90, 80, 110, 120], [80, 90, 90, 120], [80, 90, 110]):
        with pytest.raises(st.StrategyError):
            st.check_order(ic, bad)
    with pytest.raises(st.StrategyError, match="same strike"):
        st.check_order(st.TEMPLATES["long_straddle"], [95, 100])
    st.check_order(st.TEMPLATES["iron_butterfly"], [90, 100, 100, 110])


def test_adjustments_move_one_listed_strike():
    c = _Chain()
    ic = st.TEMPLATES["iron_condor"]
    base = [85.0, 90.0, 110.0, 115.0]
    assert st.adjust(ic, c, None, base, {"op": "up"}) == [90, 95, 115, 120]
    assert st.adjust(ic, c, None, base, {"op": "down"}) == [80, 85, 105, 110]
    assert st.adjust(ic, c, None, base, {"op": "wider"}) == [80, 90, 110, 120]
    with pytest.raises(st.StrategyError, match="narrower"):
        st.adjust(ic, c, None, base, {"op": "narrower"})
    assert st.adjust(ic, c, None, [80.0, 90.0, 110.0, 120.0], {"op": "narrower"}) == base
    assert st.adjust(ic, c, None, base, {"op": "leg", "leg": 2, "by": -1}) == [85, 90, 105, 115]
    with pytest.raises(st.StrategyError, match="out of order"):
        st.adjust(ic, c, None, base, {"op": "leg", "leg": 2, "by": 1})                 # onto its own wing
    with pytest.raises(st.StrategyError, match="out of order"):
        st.adjust(ic, c, None, base, {"op": "leg", "leg": 0, "by": 1})
    fly = st.TEMPLATES["iron_butterfly"]
    assert st.adjust(fly, c, None, [95.0, 100.0, 100.0, 105.0], {"op": "up"}) == [100, 105, 105, 110]
    assert st.adjust(fly, c, None, [90.0, 100.0, 100.0, 110.0], {"op": "leg", "leg": 1, "by": 1}) == [90, 105, 105, 110]
    with pytest.raises(st.StrategyError, match="out of order"):
        st.adjust(fly, c, None, [95.0, 100.0, 100.0, 105.0], {"op": "leg", "leg": 2, "by": 1})
    cb = st.TEMPLATES["call_butterfly"]
    assert st.adjust(cb, c, None, [95.0, 100.0, 105.0], {"op": "wider"}) == [90, 100, 110]   # kept even
    with pytest.raises(st.StrategyError, match="no wings"):
        st.adjust(st.TEMPLATES["long_straddle"], c, None, [100.0, 100.0], {"op": "wider"})
    assert st.adjust(st.TEMPLATES["long_straddle"], c, None, [100.0, 100.0], {"op": "up"}) == [105, 105]
    with pytest.raises(st.StrategyError, match="op must"):
        st.adjust(ic, c, None, base, {"op": "sideways"})


def test_prices_and_the_risk_check():
    legs = [{"side": "buy", "contracts": 1, "bid": 2.0, "ask": 2.2, "mid": 2.1},
            {"side": "sell", "contracts": 1, "bid": 1.0, "ask": 1.2, "mid": 1.1}]
    p = st.prices(legs)
    assert p["net_mid"] == pytest.approx(100) and p["net_natural"] == pytest.approx(120) and p["credit"] is False
    assert p["slippage_to_natural"] == pytest.approx(20)
    ok = st.risk_check(-150.0, False, 200.0, 1)
    assert ok["status"] == "pass" and ok["max_loss"] == 150 and ok["units_within_limit"] == 1
    over = st.risk_check(-450.0, False, 200.0, 3)
    assert over["status"] == "fail" and over["units_within_limit"] == 1 and "1 at a time" in over["text"]
    assert st.risk_check(-300.0, False, 200.0, 1)["units_within_limit"] == 0
    assert st.risk_check(None, True, 200.0, 1)["status"] == "fail"
    assert st.risk_check(None, False, 200.0, 1)["status"] == "unknown"


# --- reading a description --------------------------------------------------------------------

TODAY = date(2026, 10, 10)          # a Saturday
KNOWN = {"AAPL", "KO", "SPY", "LOW", "MSFT", "NVDA"}


def P(text, **kw):
    return st.parse(text, KNOWN, today=TODAY, **kw)


@pytest.mark.parametrize("text,strategy", [
    ("AAPL iron condor", "iron_condor"), ("aapl condor", "iron_condor"), ("iron fly on KO", "iron_butterfly"),
    ("KO butterfly", "call_butterfly"), ("KO put butterfly", "put_butterfly"), ("KO calendar", "call_calendar"),
    ("put calendar on KO", "put_calendar"), ("sell a straddle on SPY", "short_straddle"), ("SPY straddle", "long_straddle"),
    ("short strangle SPY", "short_strangle"), ("SPY strangle", "long_strangle"),
    ("bull put spread KO", "bull_put_spread"), ("sell a put spread on KO", "bull_put_spread"),
    ("put credit spread KO", "bull_put_spread"), ("bear call spread KO", "bear_call_spread"),
    ("call credit spread KO", "bear_call_spread"), ("bull call spread KO", "bull_call_spread"),
    ("KO call spread", "bull_call_spread"), ("bear put spread KO", "bear_put_spread"), ("KO put spread", "bear_put_spread"),
    ("buy AAPL calls", "long_call"), ("buy 2 AAPL puts", "long_put"),
    ("NVDA could make a big move either way", "long_straddle"), ("KO will stay in a range", "iron_condor"),
    ("bullish on AAPL with a spread", "bull_call_spread"), ("bearish on AAPL", "long_put"), ("AAPL 200 calls", "long_call"),
])
def test_strategy_words(text, strategy):
    assert P(text)["spec"]["strategy"] == strategy


def test_settings_in_a_description():
    p = P("AAPL iron condor 45 days 16 delta 5 wide 3 contracts")
    assert p["spec"] == {"symbol": "AAPL", "strategy": "iron_condor", "dte": 45, "delta": 0.16, "width": 5.0, "contracts": 3}
    assert p["assumed"] == [] and "0.16 delta" in p["understood"]
    assert P("KO bull put spread 30 dte")["spec"]["dte"] == 30
    assert P("KO straddle 3 weeks")["spec"]["dte"] == 21 and P("KO straddle 2 months")["spec"]["dte"] == 60
    assert P("KO straddle next week")["spec"]["dte"] == 13       # from a Saturday: the Friday after next
    assert P("KO straddle this week")["spec"]["dte"] == 6
    assert P("KO condor December")["spec"]["dte"] == (date(2026, 12, 18) - TODAY).days   # the third Friday
    assert P("KO condor nov 20")["spec"]["dte"] == 41 and P("KO condor 2026-11-20")["spec"]["dte"] == 41
    assert P("KO condor next month")["spec"]["dte"] == (date(2026, 11, 20) - TODAY).days
    assert P("KO condor in may 15")["spec"]["dte"] == (date(2027, 5, 15) - TODAY).days
    assert "dte" not in P("KO condor, the market may fall")["spec"]                  # "may" alone is a word
    assert P("buy 2 AAPL calls")["spec"]["contracts"] == 2
    assert P("butterfly at 200 on AAPL")["spec"]["center"] == 200
    assert P("NVDA straddle for earnings")["spec"]["after_earnings"] is True


def test_strikes_and_dates_written_with_slashes():
    p = P("AAPL 190/200 call spread 11/20")
    assert p["spec"]["strikes"] == [190, 200] and p["spec"]["dte"] == 41
    assert P("KO 60/55 put spread")["spec"]["strikes"] == [55, 60]                    # put in the template's order
    assert P("KO 50/55/65/70 iron condor")["spec"]["strikes"] == [50, 55, 65, 70]
    assert P("KO 55/60/65 iron fly")["spec"]["strikes"] == [55, 60, 60, 65]
    bad = P("KO 50/55/65 iron condor")
    assert "strikes" not in bad["spec"] and any("do not fit" in a for a in bad["assumed"])


def test_tickers_are_read_carefully():
    assert P("$tsla straddle")["spec"]["symbol"] == "TSLA"
    assert P("a low delta KO condor")["spec"]["symbol"] == "KO"                       # "low" is a word, not LOW
    assert P("SELL IRON CONDOR ON SPY")["spec"]["symbol"] == "SPY"
    assert P("iron condor", default_symbol="msft")["spec"]["symbol"] == "MSFT"
    assert "symbol" not in P("an iron condor on something")["spec"]
    with pytest.raises(st.StrategyError):
        P("   ")
    with pytest.raises(st.StrategyError, match="500"):
        P("x" * 501)


# --- the API on demo data -------------------------------------------------------------------------

@pytest.fixture()
def app(tmp_path):
    settings = Settings(data_mode="demo", db_path=tmp_path / "t.db")
    svc = TradeAssistant(settings, provider=DemoData(), db=Database(tmp_path / "t.db"))
    yield TestClient(create_app(settings, svc)), svc
    svc.db.close()


def test_every_template_builds_from_the_demo_chain(app):
    c, svc = app
    templates = c.get("/v2/strategies").json()["templates"]
    assert {t["key"] for t in templates} == set(st.TEMPLATES)
    for t in templates:
        r = c.post("/v2/strategies/build", json={"symbol": "AAPL", "strategy": t["key"], "contracts": 2})
        assert r.status_code == 200, (t["key"], r.text)
        d = r.json()
        tpl = st.TEMPLATES[t["key"]]
        assert [(l["option_type"], l["side"], l["contracts"]) for l in d["legs"]] == \
            [(k, s, q * 2) for k, s, q, _ in tpl.roles]
        assert all(l["bid"] > 0 and l["ask"] > l["bid"] for l in d["legs"])
        assert d["unlimited_loss"] is (not tpl.defined_risk)
        assert d["net_natural"] >= d["net_mid"] - 1e-9                      # paying the spread costs more
        if tpl.calendar:
            assert d["far_expiration"] > d["expiration"] and d["exact"] is False
        else:
            assert d["exact"] is True
        assert "NaN" not in r.text


def test_the_numbers_match_the_payoff(app):
    c, svc = app
    d = c.post("/v2/strategies/build", json={"symbol": "AAPL", "strategy": "bull_call_spread", "width": 10}).json()
    lo, hi = d["strikes"]
    debit = d["net_mid"]
    assert d["max_loss"] == pytest.approx(-debit, abs=0.01) and d["max_gain"] == pytest.approx((hi - lo) * 100 - debit, abs=0.01)
    assert d["breakevens"] == [pytest.approx(lo + debit / 100, abs=1e-3)]
    ic = c.post("/v2/strategies/build", json={"symbol": "AAPL", "strategy": "iron_condor"}).json()
    lp, sp, sc, lc = ic["strikes"]
    credit = -ic["net_mid"]
    assert credit > 0 and ic["max_gain"] == pytest.approx(credit, abs=0.01)
    assert ic["max_loss"] == pytest.approx(-(max(sp - lp, lc - sc) * 100 - credit), abs=0.01)
    # The same legs in the simulator give the same picture.
    sim = c.post("/api/simulate", json={"symbol": "AAPL", "legs": [
        {"expiration": l["expiration"], "option_type": l["option_type"], "strike": l["strike"], "side": l["side"],
         "contracts": l["contracts"], "entry_price": l["mid"]} for l in ic["legs"]]}).json()
    assert sim["expiry"]["max_loss"] == pytest.approx(ic["max_loss"]) and sim["expiry"]["breakevens"] == ic["breakevens"]


def test_the_risk_check_uses_the_settings(app):
    c, svc = app
    limit = svc.settings.account_size * svc.settings.max_risk_per_trade_pct / 100
    d = c.post("/v2/strategies/build", json={"symbol": "AAPL", "strategy": "call_butterfly"}).json()
    rc = d["risk_check"]
    assert rc["limit"] == pytest.approx(limit) and rc["max_loss"] == pytest.approx(-d["max_loss"])
    assert rc["status"] == ("pass" if -d["max_loss"] <= limit else "fail")
    naked = c.post("/v2/strategies/build", json={"symbol": "AAPL", "strategy": "short_strangle"}).json()
    assert naked["risk_check"]["status"] == "fail" and naked["max_loss"] is None
    assert any("modelled only" in n for n in naked["notes"])


def test_given_strikes_adjustments_and_errors(app):
    c, svc = app
    d = c.post("/v2/strategies/build", json={"symbol": "AAPL", "strategy": "iron_condor"}).json()
    up = c.post("/v2/strategies/build", json={**d["request"], "adjust": {"op": "up"}}).json()
    assert all(b > a for a, b in zip(d["strikes"], up["strikes"])) and up["expiration"] == d["expiration"]
    wide = c.post("/v2/strategies/build", json={**d["request"], "adjust": {"op": "wider"}}).json()
    assert wide["strikes"][0] < d["strikes"][0] and wide["strikes"][3] > d["strikes"][3]
    assert wide["strikes"][1:3] == d["strikes"][1:3]
    spot = d["spot"]
    k = round(spot / 5) * 5
    mine = c.post("/v2/strategies/build", json={"symbol": "AAPL", "strategy": "bull_call_spread", "strikes": [k + 0.3, k + 10]})
    assert mine.status_code == 200 and mine.json()["strikes"] == [k, k + 10] and any("not quoted" in n for n in mine.json()["notes"])
    far = c.post("/v2/strategies/build", json={"symbol": "AAPL", "strategy": "bull_call_spread", "strikes": [11, 20]})
    assert far.status_code == 400 and "far from the stock price" in far.json()["error"]
    assert c.post("/v2/strategies/build", json={"symbol": "AAPL", "strategy": "bull_call_spread",
                                                "strikes": [k + 10, k]}).status_code == 400
    assert c.post("/v2/strategies/build", json={"symbol": "AAPL", "strategy": "wheel"}).status_code == 400
    assert c.post("/v2/strategies/build", json={"symbol": "AAPL", "strategy": "iron_condor",
                                                "expiration": "2030-01-01"}).status_code == 400
    assert c.post("/v2/strategies/build", json={"symbol": "AAPL", "strategy": "iron_condor", "contracts": 0}).status_code == 422
    assert svc.journal.list() == [] and svc.db.one("SELECT COUNT(*) FROM positions")[0] == 0


def test_parse_endpoint(app):
    c, svc = app
    r = c.post("/v2/strategies/parse", json={"text": "AAPL iron condor 45 days 16 delta 5 wide"})
    assert r.status_code == 200, r.text
    d = r.json()
    assert d["parsed"]["spec"]["strategy"] == "iron_condor" and d["result"]["strategy"] == "iron_condor"
    assert len(d["result"]["legs"]) == 4 and d["parsed"]["source"] == "rules"
    on_screen = c.post("/v2/strategies/parse", json={"text": "straddle for earnings", "symbol": "KO"}).json()
    assert on_screen["result"]["symbol"] == "KO" and any("earnings" in n.lower() for n in on_screen["result"]["notes"])
    vague = c.post("/v2/strategies/parse", json={"text": "make me money"})
    assert vague.status_code == 400 and "Could not tell which strategy" in vague.json()["error"]
    no_stock = c.post("/v2/strategies/parse", json={"text": "an iron condor please"})
    assert no_stock.status_code == 400 and "which stock" in no_stock.json()["error"]


def test_a_fallback_reader_is_used_only_when_the_rules_find_nothing(app):
    c, svc = app
    seen = []

    def reader(text):
        seen.append(text)
        return {"strategy": "long_strangle", "symbol": "KO", "dte": 20}

    svc.strategy_fallback = reader
    d = c.post("/v2/strategies/parse", json={"text": "something clever on Coca-Cola"}).json()
    assert d["parsed"]["source"] == "ai" and d["result"]["strategy"] == "long_strangle" and d["result"]["symbol"] == "KO"
    c.post("/v2/strategies/parse", json={"text": "KO iron condor"})
    assert seen == ["something clever on Coca-Cola"]
    svc.strategy_fallback = lambda text: {"strategy": "rm -rf", "symbol": "KO"}
    assert c.post("/v2/strategies/parse", json={"text": "gibberish"}).status_code == 400   # not a template: refused


def test_the_strategy_builder_can_be_switched_off(tmp_path):
    settings = Settings(data_mode="demo", db_path=tmp_path / "o.db", features={**FEATURE_DEFAULTS, "strategy_builder": False})
    svc = TradeAssistant(settings, provider=DemoData(), db=Database(tmp_path / "o.db"))
    c = TestClient(create_app(settings, svc))
    assert c.get("/v2/strategies").status_code == 404
    assert c.post("/v2/strategies/parse", json={"text": "KO condor"}).status_code == 404
    svc.db.close()
