"""2.5: what option prices imply a position is worth in expectation.

The figure is the profit or loss at every stock price of the distribution the simulator already uses
(market_odds: a lognormal at the at-the-money volatility), weighted the same way. It is not the
two-point shortcut P(target) x gain + P(invalidation) x loss, which ignores every price in between,
and an unknown volatility gives unknown, never $0.
"""

from __future__ import annotations

import math
from datetime import datetime, timedelta

import numpy as np
import pytest
from fastapi.testclient import TestClient

from tradeassist.api import create_app
from tradeassist.config import Settings
from tradeassist.db import Database
from tradeassist.optionmath import NY, black76_price, prob_finish_beyond
from tradeassist.providers.demo import DemoData
from tradeassist.service import TradeAssistant
from tradeassist.simulate import Leg, expiry_moment, implied_expectation, market_odds, position_pnl, years_between

NOW = datetime(2026, 10, 12, 10, 0, tzinfo=NY)
EXP = "2026-12-18"
SPOT, VOL, RATE = 100.0, 0.30, 0.04


def call(strike=105.0, entry=3.10, contracts=1, vol=VOL):
    return Leg("call", strike, EXP, 1, contracts, entry, vol, 0.0)


def test_at_expiration_it_matches_the_closed_form_expected_payoff():
    """With no carry, E[max(S - K, 0)] under the lognormal is the undiscounted Black-76 price."""
    leg, when = call(contracts=2), expiry_moment(EXP)
    t = years_between(NOW, when)
    fair = black76_price(SPOT, leg.strike, t, RATE, VOL, "call") * math.exp(RATE * t)
    got = implied_expectation([leg], SPOT, RATE, VOL, 0.0, when, NOW)
    assert got == pytest.approx(2 * 100 * (fair - leg.entry), abs=0.05)


def test_it_is_not_the_two_point_shortcut_when_prices_fall_between_target_and_invalidation():
    leg, when = call(strike=100.0, entry=4.0), expiry_moment(EXP)
    t = years_between(NOW, when)
    target, invalidation = 115.0, 92.0
    full = implied_expectation([leg], SPOT, RATE, VOL, 0.0, when, NOW)
    gain, loss = position_pnl([leg], np.array([target, invalidation]), when, RATE)
    shortcut = (prob_finish_beyond(SPOT, target, t, VOL, above=True) * gain
                + prob_finish_beyond(SPOT, invalidation, t, VOL, above=False) * loss)
    # Most of the distribution sits between 92 and 115, where the call is worth something but neither
    # point says what: the shortcut misses it by tens of dollars on one contract.
    between = prob_finish_beyond(SPOT, invalidation, t, VOL, True) - prob_finish_beyond(SPOT, target, t, VOL, True)
    assert between > 0.5
    assert abs(full - shortcut) > 50
    assert full == pytest.approx(market_odds([leg], SPOT, RATE, VOL, 0.0, when, NOW)["expected_pnl"])


def test_before_expiration_it_uses_the_model_value_at_each_price():
    leg = call()
    when = NOW + timedelta(days=20)
    got = implied_expectation([leg], SPOT, RATE, VOL, 0.0, when, NOW)
    t_total = years_between(NOW, expiry_moment(EXP))
    fair_now = black76_price(SPOT, leg.strike, t_total, RATE, VOL, "call")
    # Fair price grows at the rate between now and then (no drift beyond the forward): close to zero edge.
    assert got == pytest.approx(100 * (fair_now * math.exp(RATE * years_between(NOW, when)) - leg.entry), abs=0.5)


@pytest.mark.parametrize("vol", [None, 0.0, -0.1, float("nan")])
def test_an_unknown_volatility_gives_unknown_not_zero(vol):
    assert implied_expectation([call()], SPOT, RATE, vol, 0.0, NOW + timedelta(days=20), NOW) is None


def test_market_odds_reports_the_expectation_and_unknown_without_volatility():
    when = NOW + timedelta(days=20)
    assert isinstance(market_odds([call()], SPOT, RATE, VOL, 0.0, when, NOW)["expected_pnl"], float)
    assert market_odds([call()], SPOT, RATE, 0.0, 0.0, when, NOW)["expected_pnl"] is None


@pytest.fixture
def client(tmp_path):
    settings = Settings(data_mode="demo", db_path=tmp_path / "t.db")
    svc = TradeAssistant(settings, provider=DemoData(), db=Database(tmp_path / "t.db"))
    with TestClient(create_app(settings, svc)) as c:
        yield c
    svc.db.close()


def test_the_option_picker_adds_the_new_fields_and_keeps_the_old(client):
    r = client.post("/v2/options/evaluate", json={"symbol": "AAPL", "direction": "BULLISH", "horizon_days": 20})
    assert r.status_code == 200
    d = r.json()
    for k in ("reach_target_1_by_horizon", "beyond_invalidation_at_horizon", "atm_iv", "note"):
        assert k in d["market"]
    m = d["market"]
    assert m["range_1sd_at_horizon"]["low"] < d["thesis"]["spot"] < m["range_1sd_at_horizon"]["high"]
    assert m["expected_price_at_horizon"] == pytest.approx(d["thesis"]["spot"], rel=0.02)
    assert 0.3 < m["stock_profit_at_horizon"] < 0.7
    usable = [k for k in d["contracts"] if k["usable"]]
    assert usable
    for k in usable:
        for old in ("at_target", "if_invalidated", "if_unchanged", "breakeven_at_horizon", "market_implied_profit_at_expiration"):
            assert old in k
        assert 0.0 <= k["market_implied_profit_at_horizon"] <= 1.0
        assert k["implied_value_at_horizon"] == pytest.approx(k["premium"] + k["implied_expected_pnl_at_horizon"])
        # Each contract read on its own implied volatility is priced fairly against itself: the expectation is
        # what is paid, give or take the carry to the horizon and the smile's fit to the midpoint.
        assert abs(k["implied_expected_pnl_at_horizon"]) < 0.03 * k["premium"] + 2, k


def test_the_simulator_reports_the_expectation_too(client):
    chain = client.post("/v2/options/evaluate", json={"symbol": "AAPL", "direction": "BULLISH"}).json()
    k = next(x for x in chain["contracts"] if x["usable"])
    s = client.post("/api/simulate", json={"symbol": "AAPL", "legs": [{"expiration": k["expiration"], "option_type": "call",
                                                                        "strike": k["strike"], "contracts": 1}]})
    assert s.status_code == 200
    assert isinstance(s.json()["odds_at_date"]["expected_pnl"], float)
