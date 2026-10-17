"""2.10: the entry price is worked out from the evidence, not taken from the midpoint: the morning card's
contract before and after the open, the limit cap, Robinhood's own fill estimates, and the pricing screen."""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from tradeassist.api import create_app
from tradeassist.config import Settings
from tradeassist.db import Database
from tradeassist.morning import entry_estimate, last_session_date, limit_prices
from tradeassist.optionmath import black76_price, implied_vol
from tradeassist.providers.demo import DemoData
from tradeassist.providers.robinhood import fill_prices
from tradeassist.service import TradeAssistant


def _analysis(row, spot=100.0, forward=100.3, method="parity"):
    return SimpleNamespace(expiration="2026-11-20", spot=spot, forward=forward, forward_method=method, t=0.1,
                           rate=0.04, find=lambda sym: row if sym == row["symbol"] else None)


def _row(**kw):
    base = {"symbol": "ACME261120P00095000", "option_type": "put", "strike": 95.0, "bid": 1.90, "ask": 2.30,
            "bidsize": 20, "asksize": 20, "last": None, "trade_time": None, "model_vol": 0.35, "iv": 0.35}
    return {**base, **kw}


def _card_row(chain_row):
    return {"option_symbol": chain_row["symbol"], "expiration": "2026-11-20", "mid": 0.5 * (chain_row["bid"] + chain_row["ask"])}


def test_before_the_open_a_gapped_stock_reprices_the_contract_instead_of_using_the_old_midpoint():
    """The fade that motivated 2.10: the stock gapped up 5% before the open and the card buys puts. Last
    session's put quote was made with the stock at its close, so its midpoint is far above what the put is
    worth now."""
    chain = _row()
    est = entry_estimate(_card_row(chain), [_analysis(chain)], entry=105.0, clock="premarket", ref_close=100.0)
    own = implied_vol(2.10, 100.3, 95.0, 0.1, 0.04, "put")            # the volatility its last midpoint implies
    expected = black76_price(100.3 * 105.0 / 100.0, 95.0, 0.1, 0.04, own, "put")
    assert est["repriced"] and not est["unchecked"] and est["price"] == pytest.approx(expected)
    assert est["price"] < est["mid"] == pytest.approx(2.10)
    assert "last session" in est["basis"] and "9:30" in est["basis"]
    # Robinhood's clock word for the same hours, and a gap down for calls, work the same way.
    call = _row(symbol="ACME261120C00105000", option_type="call", strike=105.0)
    down = entry_estimate(_card_row(call), [_analysis(call)], entry=95.0, clock="pre", ref_close=100.0)
    assert down["repriced"] and down["price"] < down["mid"]


def test_before_the_open_a_flat_stock_keeps_the_quote_and_ignores_the_last_trade():
    chain = _row(last=1.95, trade_time="2026-10-16T19:59:00+00:00")
    est = entry_estimate(_card_row(chain), [_analysis(chain)], entry=100.1, clock="premarket", ref_close=100.0)
    assert not est["repriced"] and est["price"] <= est["mid"] + 1e-9
    trade = next(e for e in est["evidence"] if e["key"] == "last_trade")
    assert trade["used"] is False and "closed" in trade["note"]


def test_while_options_trade_the_fair_price_uses_the_live_evidence():
    chain = _row(bidsize=5, asksize=80, model_vol=None, iv=None)   # many sellers, few buyers: worth less
    est = entry_estimate(_card_row(chain), [_analysis(chain)], entry=100.0, clock="open", ref_close=None)
    micro = (1.90 * 80 + 2.30 * 5) / 85
    assert not est["repriced"] and est["price"] == pytest.approx((2.10 + micro) / 2)
    assert any(e["key"] == "size_weighted" and e["used"] for e in est["evidence"])


def test_a_contract_missing_from_its_chain_falls_back_to_the_midpoint():
    chain = _row()
    est = entry_estimate({"option_symbol": "OTHER", "expiration": "2026-11-20", "mid": 2.10}, [_analysis(chain)],
                         entry=100.0, clock="open", ref_close=None)
    assert est["price"] == 2.10 and not est["evidence"]


def test_the_limit_starts_from_the_fair_price_and_the_cap_still_rules():
    lm = limit_prices(mid=2.10, bid=1.90, value_at_target=4.0, value_at_stop=1.0, min_payoff=1.0, fair=1.97,
                      fair_basis="evidence")
    assert lm["viable"] and lm["suggested"] == 1.97 and lm["fair"] == 1.97 and lm["mid"] == 2.10
    assert lm["patient"] == 1.96 and lm["fair_basis"] == "evidence"
    capped = limit_prices(mid=2.10, bid=1.90, value_at_target=3.0, value_at_stop=0.0, min_payoff=1.0, fair=1.97)
    assert capped["suggested"] == 1.50                     # (3 + 0) / 2: never above the cap
    over = limit_prices(mid=2.10, bid=1.90, value_at_target=2.0, value_at_stop=0.0, min_payoff=1.0, fair=1.97)
    assert not over["viable"] and "fair price" in over["why"]
    plain = limit_prices(mid=2.10, bid=1.90, value_at_target=4.0, value_at_stop=1.0, min_payoff=1.0)
    assert plain["fair"] == 2.10                           # no estimate given: the midpoint, as before


def test_robinhood_fill_estimates_are_read_when_present():
    raw = {"high_fill_rate_buy_price": "2.18", "low_fill_rate_buy_price": "2.01", "high_fill_rate_sell_price": "1.99",
           "low_fill_rate_sell_price": "0", "bid_price": "1.9"}
    assert fill_prices(raw) == {"buy_likely": 2.18, "buy_less_likely": 2.01, "sell_likely": 1.99}
    assert fill_prices({"bid_price": "1.9"}) is None


def test_the_pricing_screen_shows_the_fair_price_and_its_evidence(tmp_path):
    s = Settings(data_mode="demo", db_path=tmp_path / "t.db")
    svc = TradeAssistant(s, provider=DemoData(), db=Database(tmp_path / "t.db"))
    with TestClient(create_app(s, svc)) as client:
        exp = client.get("/api/expirations", params={"symbol": "AAPL"}).json()["expirations"][2]["date"]
        chain = client.get("/api/chain", params={"symbol": "AAPL", "expiration": exp}).json()["contracts"]
        row = next(c for c in chain if c["option_type"] == "call" and c["bid"] and c["ask"] - c["bid"] >= 0.04)
        req = {"symbol": "AAPL", "expiration": exp, "option_type": "call", "strike": row["strike"]}
        lad = client.post("/api/price", json=req).json()["ladder"]
        fair = lad["fair"]
        assert fair["anchor"] <= lad["mid"] + 1e-9 and fair["evidence"][0]["key"] == "midpoint"
        assert any(e["key"] == "size_weighted" for e in fair["evidence"])     # the demo quotes carry sizes
        prices = {r["name"]: r["price"] for r in lad["rungs"]}
        assert lad["walk"]["cap"] <= fair["anchor"] + 1e-9 or lad["walk"]["cap"] == min(prices.values())
        live = client.post("/api/live", json=req).json()["ladder"]
        assert live["fair"]["mid"] == live["mid"]


def test_a_big_gap_on_robinhood_data_is_still_repriced():
    """Review finding: a gap over about 10% makes parity fail ("carry" forward), and on Robinhood the chain's
    stock price before the open is the pre-market trade. The reference must still be the close."""
    chain = _row()
    a = _analysis(chain, spot=115.0, forward=115.0 * 1.004, method="carry")   # the chain was read at 115 pre-market
    est = entry_estimate(_card_row(chain), [a], entry=115.0, clock="pre", ref_close=100.0)
    assert est["repriced"] and est["price"] < 0.5 * est["mid"]              # the put is worth far less after +15%
    assert "+15.0%" in est["basis"]


def test_without_a_confirmed_close_or_clock_nothing_is_repriced_and_it_says_so():
    chain = _row()
    no_close = entry_estimate(_card_row(chain), [_analysis(chain)], entry=105.0, clock="premarket", ref_close=None)
    assert not no_close["repriced"] and no_close["unchecked"] and "could not be confirmed" in no_close["basis"]
    unknown = entry_estimate(_card_row(chain), [_analysis(chain)], entry=104.0, clock="unknown", ref_close=100.0)
    assert not unknown["repriced"] and unknown["unchecked"] and "clock could not be read" in unknown["basis"]


def test_the_session_whose_closing_quotes_are_shown():
    from datetime import datetime

    from tradeassist.optionmath import NY
    assert str(last_session_date(datetime(2026, 10, 16, 7, 0, tzinfo=NY))) == "2026-10-15"     # Friday pre-market
    assert str(last_session_date(datetime(2026, 10, 16, 17, 0, tzinfo=NY))) == "2026-10-16"    # Friday evening
    assert str(last_session_date(datetime(2026, 10, 17, 12, 0, tzinfo=NY))) == "2026-10-16"    # Saturday
    assert str(last_session_date(datetime(2026, 10, 19, 8, 0, tzinfo=NY))) == "2026-10-16"     # Monday pre-market


def test_limits_sit_on_the_chains_own_increments():
    from tradeassist.pricer import TickRule
    nickel = TickRule(5, 10, "nickel class")
    lm = limit_prices(mid=2.10, bid=1.90, value_at_target=4.0, value_at_stop=1.0, min_payoff=1.0, fair=2.04,
                      rule=nickel)
    assert lm["suggested"] == 2.00 and lm["patient"] == 1.95          # $2.04 is not a price in a nickel class
    penny = TickRule(1, 1, "penny everywhere")
    lm = limit_prices(mid=4.10, bid=4.00, value_at_target=8.0, value_at_stop=2.0, min_payoff=1.0, fair=4.07,
                      rule=penny)
    assert lm["suggested"] == 4.07 and lm["patient"] == 4.06          # pennies above $3 in a penny-everywhere class
