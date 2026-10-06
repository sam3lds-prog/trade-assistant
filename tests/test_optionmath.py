import math
from datetime import datetime

import pytest

from tradeassist import optionmath as om


def test_black_scholes_textbook_value():
    # S=100, K=100, r=5%, sigma=20%, T=1: call 10.4506, put 5.5735 (standard textbook figures).
    forward = 100 * math.exp(0.05)
    assert om.black76_price(forward, 100, 1.0, 0.05, 0.20, "call") == pytest.approx(10.4506, abs=1e-4)
    assert om.black76_price(forward, 100, 1.0, 0.05, 0.20, "put") == pytest.approx(5.5735, abs=1e-4)


@pytest.mark.parametrize("strike", [70, 95, 100, 110, 140])
def test_put_call_parity(strike):
    f, t, r, vol = 103.0, 0.4, 0.04, 0.33
    call = om.black76_price(f, strike, t, r, vol, "call")
    put = om.black76_price(f, strike, t, r, vol, "put")
    assert call - put == pytest.approx(math.exp(-r * t) * (f - strike), abs=1e-10)


@pytest.mark.parametrize("option_type", ["call", "put"])
@pytest.mark.parametrize("vol", [0.08, 0.25, 0.9, 2.5])
@pytest.mark.parametrize("strike", [80, 100, 125])
def test_implied_vol_round_trip(option_type, vol, strike):
    f, t, r = 100.0, 0.25, 0.04
    price = om.black76_price(f, strike, t, r, vol, option_type)
    if price < 1e-6:
        pytest.skip("price too small to invert")
    assert om.implied_vol(price, f, strike, t, r, option_type) == pytest.approx(vol, abs=2e-5)


def test_implied_vol_rejects_impossible_prices():
    f, t, r = 100.0, 0.25, 0.04
    assert om.implied_vol(4.0, f, 90, t, r, "call") is None       # below intrinsic value
    assert om.implied_vol(0.0, f, 100, t, r, "call") is None
    assert om.implied_vol(None, f, 100, t, r, "call") is None
    assert om.implied_vol(150.0, f, 100, t, r, "call") is None     # worth more than the stock


@pytest.mark.parametrize("option_type", ["call", "put"])
def test_greeks_match_finite_differences(option_type):
    spot, r, q, t, vol, strike = 100.0, 0.04, 0.015, 0.3, 0.28, 105.0
    fwd = lambda s, tt: s * math.exp((r - q) * tt)
    price = lambda s, tt=t, v=vol: om.black76_price(fwd(s, tt), strike, tt, r, v, option_type)
    g = om.black76_greeks(spot, fwd(spot, t), strike, t, r, vol, option_type)
    h = 0.01
    assert g.delta == pytest.approx((price(spot + h) - price(spot - h)) / (2 * h), abs=1e-5)
    assert g.gamma == pytest.approx((price(spot + h) - 2 * price(spot) + price(spot - h)) / h ** 2, abs=1e-4)
    assert g.vega == pytest.approx(price(spot, v=vol + 0.005) - price(spot, v=vol - 0.005), abs=1e-5)
    assert g.theta == pytest.approx(price(spot, tt=t - 1 / 365) - price(spot), abs=1e-9)
    assert g.theta < 0


def test_implied_forward_recovers_dividend_adjusted_forward():
    spot, r, q, t, vol = 200.0, 0.04, 0.03, 0.5, 0.25
    true_forward = spot * math.exp((r - q) * t)
    pairs = [(k, om.black76_price(true_forward, k, t, r, vol, "call"),
              om.black76_price(true_forward, k, t, r, vol, "put")) for k in (190, 195, 200, 205, 210)]
    forward, method = om.implied_forward(pairs, spot, t, r)
    assert method == "parity"
    assert forward == pytest.approx(true_forward, abs=1e-8)


def test_implied_forward_falls_back_on_bad_quotes():
    forward, method = om.implied_forward([(100, 30.0, 0.5)], 100.0, 0.25, 0.04)   # implies a 30% forward premium
    assert method == "carry" and forward == pytest.approx(100 * math.exp(0.01))
    assert om.implied_forward([], 100.0, 0.25, 0.04)[1] == "carry"


def test_smile_fit_reproduces_a_quadratic_and_does_not_extrapolate():
    forward = 100.0
    strikes = [80, 85, 90, 95, 100, 105, 110, 115, 120]
    true = lambda k: 0.25 - 0.3 * math.log(k / forward) + 0.9 * math.log(k / forward) ** 2
    smile = om.fit_smile(strikes, [true(k) for k in strikes], [1.0] * len(strikes), forward)
    assert smile.vol_at(97.5, forward) == pytest.approx(true(97.5), abs=1e-9)
    assert smile.vol_at(300, forward) == pytest.approx(smile.vol_at(120, forward))   # clamped at the data edge
    assert om.fit_smile(strikes[:3], [0.2] * 3, [1.0] * 3, forward) is None


def test_probability_and_expected_move():
    assert om.prob_finish_beyond(100, 100, 1.0, 0.2, above=True) == pytest.approx(om.norm_cdf(-0.1))
    above = om.prob_finish_beyond(100, 110, 0.5, 0.3, above=True)
    below = om.prob_finish_beyond(100, 110, 0.5, 0.3, above=False)
    assert above + below == pytest.approx(1.0) and above < 0.5
    assert om.expected_move(100, 0.2, 0.25) == pytest.approx(10.0)


def test_time_to_expiry_uses_new_york_close():
    now = datetime(2026, 10, 1, 16, 0, tzinfo=om.NY)
    assert om.years_to_expiry("2026-10-31", now) == pytest.approx(30 / 365)
    assert om.years_to_expiry("2026-09-01", now) == om.MIN_T       # already expired: floor, never negative
    assert om.days_to_expiry("2026-10-31", now.date()) == 30


def test_time_to_expiry_counts_the_extra_hour_when_clocks_go_back():
    # US clocks go back on 1 Nov 2026, so 4 pm on 30 Oct to 4 pm on 6 Nov is 7 days and 1 hour.
    now = datetime(2026, 10, 30, 16, 0, tzinfo=om.NY)
    assert om.years_to_expiry("2026-11-06", now) == pytest.approx((7 * 24 + 1) / 24 / 365)


def test_greeks_against_closed_form_with_scipy():
    from scipy.stats import norm
    spot, strike, r, q, t, vol = 150.0, 160.0, 0.045, 0.02, 0.6, 0.31
    forward = spot * math.exp((r - q) * t)
    d1 = (math.log(spot / strike) + (r - q + vol ** 2 / 2) * t) / (vol * math.sqrt(t))
    g = om.black76_greeks(spot, forward, strike, t, r, vol, "put")
    assert g.delta == pytest.approx(math.exp(-q * t) * (norm.cdf(d1) - 1), abs=1e-9)
    assert g.gamma == pytest.approx(math.exp(-q * t) * norm.pdf(d1) / (spot * vol * math.sqrt(t)), abs=1e-9)
    assert g.vega == pytest.approx(spot * math.exp(-q * t) * norm.pdf(d1) * math.sqrt(t) / 100, abs=1e-9)
