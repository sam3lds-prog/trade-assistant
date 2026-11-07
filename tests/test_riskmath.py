"""3.0: outcome percentiles and the four risk measures (theoretical, modeled, shortfall, capital) kept apart."""

import numpy as np
import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from tradeassist import riskmath as R


def lognormal(last=100.0, sd=0.05, points=241):
    z = np.linspace(-4, 4, points)
    w = np.exp(-0.5 * z * z)
    return last * np.exp(sd * z), w / w.sum()


def test_weighted_quantile_reads_the_middle_of_a_symmetric_distribution_exactly():
    v = np.array([3.0, 1.0, 2.0])
    w = np.array([1.0, 1.0, 1.0])
    assert R.weighted_quantile(v, w, 0.5) == 2.0
    assert R.weighted_quantile(v, w, 0.0) == 1.0 and R.weighted_quantile(v, w, 1.0) == 3.0
    with pytest.raises(ValueError):
        R.weighted_quantile(np.array([]), np.array([]), 0.5)


def test_a_monotone_pnl_is_read_at_the_matching_price_and_a_falling_one_at_the_mirror():
    prices, w = lognormal()
    q = {0.10: 95.0, 0.50: 100.0, 0.90: 105.0}
    up = R.outcome_stats(prices, w, prices - 100.0, q)
    assert (up["pnl_p10"], up["pnl_p50"], up["pnl_p90"]) == pytest.approx((-5.0, 0.0, 5.0))
    down = R.outcome_stats(prices, w, 100.0 - prices, q)
    assert (down["pnl_p10"], down["pnl_p90"]) == pytest.approx((-5.0, 5.0))
    assert down["shape"] == "falls with the price"


def test_a_v_shaped_pnl_has_its_worst_outcomes_in_the_middle():
    prices, w = lognormal()
    pnl = np.abs(prices - 100.0) - 4.0                          # a straddle at expiry, $4 debit
    st_ = R.outcome_stats(prices, w, pnl, {0.10: 93.8, 0.50: 100.0, 0.90: 106.6})
    assert st_["shape"] == "not monotonic in the price"
    assert st_["pnl_p10"] == pytest.approx(R.weighted_quantile(pnl, w, 0.10))
    assert st_["pnl_p10"] < 0 < float(np.interp(93.8, prices, pnl)) + 4.0   # nowhere near the P&L at the 10th price
    assert st_["modeled_worst_loss"] == pytest.approx(4.0, abs=0.05)
    assert 0 < st_["expected_shortfall"] <= st_["modeled_worst_loss"] + 1e-9


def test_theoretical_max_loss_comes_from_the_payoff_not_the_scenario():
    shares = R.PayoffLeg(None, 100, 50.0)
    put = R.PayoffLeg("PUT", 1, 2.0, strike=45.0, expiration="2026-12-18")
    assert R.theoretical_max_loss([shares])["value"] == pytest.approx(5000.0)
    hedged = R.theoretical_max_loss([shares, put])
    assert hedged["value"] == pytest.approx(100 * (50 - 45) + 200) and hedged["known"]
    short_call = R.PayoffLeg("CALL", -1, 1.5, strike=55.0, expiration="2026-12-18")
    assert R.theoretical_max_loss([short_call])["unlimited"]
    covered = R.theoretical_max_loss([shares, short_call])
    assert not covered["unlimited"] and covered["value"] == pytest.approx(5000 - 150) and "assigned early" in covered["basis"]
    spread = R.theoretical_max_loss([R.PayoffLeg("CALL", 1, 3.0, 100.0, expiration="e"), R.PayoffLeg("CALL", -1, 1.0, 105.0, expiration="e")])
    assert spread["value"] == pytest.approx(200.0)
    calendar = R.theoretical_max_loss([R.PayoffLeg("CALL", -1, 2.0, 100, expiration="a"), R.PayoffLeg("CALL", 1, 3.0, 100, expiration="b")])
    assert calendar["value"] is None and not calendar["known"] and "different days" in calendar["basis"]


def test_capital_required_states_its_rule():
    assert R.capital_required("shares", cost=5000, credit=False)["value"] == 5000
    short = R.capital_required("short", cost=5000, credit=False, quantity=100, last=50)
    assert short["value"] == pytest.approx(7500) and "Regulation T" in short["basis"]
    assert R.capital_required("bull_put_spread", cost=120, credit=True, theoretical=380)["value"] == 380
    assert R.capital_required("short_straddle", cost=500, credit=True, theoretical=None)["value"] is None


@settings(max_examples=60, deadline=None)
@given(st.lists(st.tuples(st.sampled_from(["CALL", "PUT"]), st.integers(-3, 3), st.floats(0.05, 20), st.floats(20, 180)),
                min_size=1, max_size=4),
       st.integers(-200, 200))
def test_the_theoretical_floor_is_never_above_any_modeled_expiry_outcome(opts, shares_qty):
    legs = [R.PayoffLeg(t, q, e, k, expiration="e") for t, q, e, k in opts if q] + [R.PayoffLeg(None, shares_qty, 100.0)]
    th = R.theoretical_max_loss(legs)
    grid = np.linspace(0.0, 400.0, 4001)
    values = np.array([sum(lg.value(s) for lg in legs) for s in grid])
    if th["unlimited"]:
        assert values[-1] < values[-2] or sum(lg.slope_above() for lg in legs) < 0
    else:
        assert -values.min() <= th["value"] + 1e-6
