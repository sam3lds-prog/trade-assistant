"""2.4 options lab: volatility across expirations and the Monte Carlo check.

The financial calculations are tested against closed-form answers with fixed inputs and a fixed
seed, so every number here is deterministic."""

from __future__ import annotations

import math
from datetime import date, timedelta

import pytest
from fastapi.testclient import TestClient
from scipy.special import ndtr

from tradeassist.api import create_app
from tradeassist.config import Settings
from tradeassist.db import Database
from tradeassist.features import FEATURE_DEFAULTS
from tradeassist.optionmath import black76_price, prob_finish_beyond
from tradeassist.options import volatility as vol
from tradeassist.options.lab import pick_expirations
from tradeassist.providers.demo import DemoData
from tradeassist.service import TradeAssistant


# --- Monte Carlo ---------------------------------------------------------------------------------

BASE = dict(spot=100.0, forward=100.4, t=45 / 365, rate=0.04, sigma=0.30)


@pytest.mark.parametrize("option_type,strike", [("call", 100.0), ("call", 110.0), ("put", 100.0), ("put", 90.0)])
def test_monte_carlo_agrees_with_black76_within_its_standard_error(option_type, strike):
    m = vol.monte_carlo(**BASE, strike=strike, option_type=option_type)
    bs = black76_price(BASE["forward"], strike, BASE["t"], BASE["rate"], BASE["sigma"], option_type)
    assert m["black76"] == pytest.approx(bs)
    assert abs(m["price"] - bs) <= 3 * m["standard_error"] + 0.005
    assert m["consistent"] is True
    assert 0 < m["standard_error"] < 0.1
    above = option_type == "call"
    p = prob_finish_beyond(BASE["forward"], strike, BASE["t"], BASE["sigma"], above=above)
    assert m["p_finish_itm_formula"] == pytest.approx(p)
    assert m["p_finish_itm"] == pytest.approx(p, abs=0.015)


def test_monte_carlo_is_reproducible_with_a_seed_and_moves_with_another():
    a = vol.monte_carlo(**BASE, strike=105, option_type="call")
    b = vol.monte_carlo(**BASE, strike=105, option_type="call")
    c = vol.monte_carlo(**BASE, strike=105, option_type="call", seed=8)
    assert a["price"] == b["price"] and a["histogram"] == b["histogram"] and a["p_touch_close"] == b["p_touch_close"]
    assert a["seed"] == 7 and c["price"] != a["price"]


def test_touch_on_closes_matches_the_corrected_formula_and_any_time_is_more_likely():
    for option_type, strike in (("call", 110.0), ("put", 90.0)):
        m = vol.monte_carlo(**BASE, strike=strike, option_type=option_type, paths=40_000)
        assert m["p_touch_close"] == pytest.approx(m["p_touch_close_formula"], abs=0.015)
        assert m["p_touch_any_time"] > m["p_touch_close_formula"] > m["p_finish_itm_formula"]


def test_touch_probability_closed_forms():
    # With no drift in the log price the reflection formula is 2 N(-b / (sigma sqrt t)).
    s, t, sigma = 100.0, 0.5, 0.25
    growth = 0.5 * sigma * sigma
    for barrier in (115.0, 85.0):
        b = abs(math.log(barrier / s))
        assert vol.touch_probability(s, barrier, t, sigma, growth) == pytest.approx(2 * ndtr(-b / (sigma * math.sqrt(t))))
    assert vol.touch_probability(s, s, t, sigma, 0.03) == 1.0
    assert vol.touch_probability(s, 120, 0.0, sigma, 0.03) == 0.0
    daily = vol.touch_probability(s, 115, t, sigma, growth, 252)
    shifted = 115 * math.exp(vol.BGK * sigma * math.sqrt(1 / 252))
    assert daily == pytest.approx(vol.touch_probability(s, shifted, t, sigma, growth))
    assert daily < vol.touch_probability(s, 115, t, sigma, growth)


def test_an_earnings_jump_adds_its_variance_and_drops_the_formula_touch_odds():
    m = vol.monte_carlo(**BASE, strike=100, option_type="call", event_move=0.06, event_t=10 / 365)
    eff = math.sqrt((BASE["sigma"] ** 2 * BASE["t"] + 0.06 ** 2) / BASE["t"])
    assert m["black76"] == pytest.approx(black76_price(BASE["forward"], 100, BASE["t"], BASE["rate"], eff, "call"))
    assert m["consistent"] is True and m["event"]["move_pct"] == pytest.approx(6.0)
    assert m["p_touch_close_formula"] is None and m["p_touch_any_time"] is None
    plain = vol.monte_carlo(**BASE, strike=100, option_type="call")
    assert m["price"] > plain["price"]
    # A jump after expiration is not added.
    late = vol.monte_carlo(**BASE, strike=100, option_type="call", event_move=0.06, event_t=1.0)
    assert late["event"] is None and late["price"] == plain["price"]


def test_monte_carlo_keeps_runs_bounded():
    m = vol.monte_carlo(spot=100, forward=104, t=1.0, rate=0.04, sigma=0.3, strike=100, option_type="call",
                        paths=500_000)
    assert m["paths"] == vol.MC_MAX_PATHS and m["paths"] * m["steps"] <= vol.MC_MAX_CELLS
    assert m["steps_reduced"] is True and any("rather than daily" in n for n in m["notes"])
    small = vol.monte_carlo(**BASE, strike=100, option_type="call", paths=10)
    assert small["paths"] == 2_000 and small["steps"] == round(BASE["t"] * 252)


@pytest.mark.parametrize("change,message", [({"t": 0.0}, "expired"), ({"sigma": -0.1}, "sigma"),
                                            ({"spot": float("nan")}, "spot"), ({"option_type": "straddle"}, "call or put"),
                                            ({"event_move": 1.5, "event_t": 0.01}, "event_move"),
                                            ({"premium": -1.0}, "premium")])
def test_monte_carlo_refuses_bad_inputs(change, message):
    args = {**BASE, "strike": 100.0, "option_type": "call", **change}
    with pytest.raises(ValueError, match=message):
        vol.monte_carlo(**args)


def test_the_buyer_block_measures_the_price_paid():
    fair = black76_price(BASE["forward"], 100, BASE["t"], BASE["rate"], BASE["sigma"], "call")
    m = vol.monte_carlo(**BASE, strike=100, option_type="call", premium=fair, contracts=2)
    b = m["buyer"]
    assert b["model_edge_per_share"] == pytest.approx(0.0, abs=1e-9)
    assert b["pnl"]["p5"] == pytest.approx(-fair * 200)                       # most paths lose the whole premium
    assert b["pnl"]["p5"] <= b["pnl"]["p25"] <= b["pnl"]["median"] <= b["pnl"]["p75"] <= b["pnl"]["p95"]
    assert 0.25 < b["p_profit"] < 0.5
    # At a fair price the average outcome is about zero (undiscounted payoff, so slightly positive).
    assert abs(b["average_pnl"]) < 3 * m["standard_error"] * 200 + 5
    assert "buyer" not in vol.monte_carlo(**BASE, strike=100, option_type="call")


def test_histogram_counts_every_path_and_marks_the_strike():
    m = vol.monte_carlo(**BASE, strike=104, option_type="call")
    h = m["histogram"]
    assert sum(h["counts"]) == m["paths"] and len(h["edges"]) == len(h["counts"]) + 1 and h["strike"] == 104
    t = m["terminal"]
    assert t["p5"] < t["p25"] < t["median"] < t["p75"] < t["p95"]


# --- volatility across expirations ---------------------------------------------------------------

def _row(dte, iv, earnings=None, spot=100.0):
    return {"expiration": (date(2026, 10, 10) + timedelta(days=dte)).isoformat(), "dte": dte, "t": dte / 365,
            "atm_iv": iv, "earnings": earnings, "spot": spot, "skew_25d": None}


def test_implied_earnings_move_from_an_expiration_before_the_report():
    rows = [_row(10, 0.25, "BEFORE"), _row(20, 0.40, "INCLUDES"), _row(50, 0.33, "INCLUDES")]
    ev = vol.implied_event_move(rows, date(2026, 10, 25))
    expected = math.sqrt((0.40 ** 2 - 0.25 ** 2) * 20 / 365)
    assert ev["available"] is True and ev["move_pct"] == pytest.approx(100 * expected)
    assert ev["move_dollars"] == pytest.approx(100 * expected) and ev["base_vol"] == pytest.approx(0.25)
    assert ev["expiration"] == rows[1]["expiration"] and "before the report" in ev["method"]


def test_implied_earnings_move_from_two_expirations_after_the_report():
    rows = [_row(3, 0.50, "SAME_DAY"), _row(20, 0.40, "INCLUDES"), _row(50, 0.33, "INCLUDES")]
    ev = vol.implied_event_move(rows, date(2026, 10, 13))
    base_var = (0.33 ** 2 * 50 - 0.40 ** 2 * 20) / 30
    assert ev["available"] is True and ev["base_vol"] == pytest.approx(math.sqrt(base_var))
    assert ev["move_pct"] == pytest.approx(100 * math.sqrt((0.40 ** 2 - base_var) * 20 / 365))
    assert "both after the report" in ev["method"]


def test_implied_earnings_move_says_why_it_is_missing():
    assert vol.implied_event_move([_row(20, 0.3)], None) is None
    assert vol.implied_event_move([_row(10, 0.3, "BEFORE")], date(2026, 12, 1))["available"] is False
    one_after = vol.implied_event_move([_row(20, 0.3, "INCLUDES")], date(2026, 10, 15))
    assert one_after["available"] is False and "two after" in one_after["reason"]
    flat = vol.implied_event_move([_row(10, 0.3, "BEFORE"), _row(20, 0.29, "INCLUDES")], date(2026, 10, 15))
    assert flat["available"] is False and "no extra volatility" in flat["reason"]


def test_iv30_interpolates_total_variance():
    rows = [_row(5, 0.9), _row(20, 0.20), _row(40, 0.30)]
    w = 0.04 * 20 + (0.09 * 40 - 0.04 * 20) * (10 / 20)
    assert vol.iv30(rows) == pytest.approx(math.sqrt(w / 30))          # the 5-day expiration is left out
    assert vol.iv30([_row(10, 0.2), _row(20, 0.25)]) == 0.25
    assert vol.iv30([_row(45, 0.3), _row(60, 0.35)]) == 0.3
    assert vol.iv30([_row(3, 0.3)]) is None


def test_shape_compares_the_front_with_about_75_days():
    assert vol.shape([_row(10, 0.20), _row(30, 0.22), _row(75, 0.24), _row(180, 0.20)])["shape"] == "UPWARD"
    inverted = vol.shape([_row(10, 0.40), _row(75, 0.30)])
    assert inverted["shape"] == "INVERTED" and inverted["difference_points"] == pytest.approx(-10)
    assert vol.shape([_row(10, 0.300), _row(75, 0.305)])["shape"] == "FLAT"
    assert vol.shape([_row(3, 0.5), _row(75, 0.3)])["shape"] is None    # the front must be a week out


def test_realized_volatility_is_annualised_and_missing_when_short():
    up = [100 * 1.01 ** i for i in range(30)]
    assert vol.realized(up)["hv20"] == pytest.approx(0.0, abs=1e-12)
    zigzag = [100 * (1.02 if i % 2 else 1.0) for i in range(70)]
    r = math.log(1.02)
    hv = vol.realized(zigzag)
    expected = math.sqrt(sum((x - 0) ** 2 for x in [r, -r] * 5) / 9) * math.sqrt(252)     # mean 0 over 10 returns
    assert hv["hv10"] == pytest.approx(expected)
    assert hv["hv60"] is not None and vol.realized(up)["hv60"] is None


class _Chain:
    """Just enough of ChainAnalysis for the per-expiration helpers."""

    def __init__(self, contracts, spot=100.0, forward=100.0, t=30 / 365, expiration="2026-11-09"):
        self.contracts, self.spot, self.forward, self.t, self.expiration = contracts, spot, forward, t, expiration
        self.smile, self.notes = None, []

    def find_by_terms(self, option_type, strike):
        return next((c for c in self.contracts if c["option_type"] == option_type and c["strike"] == strike), None)


def _c(option_type, strike, delta, iv, bid=1.0, ask=1.1):
    return {"option_type": option_type, "strike": strike, "delta": delta, "iv": iv, "bid": bid, "ask": ask}


def test_iv_at_delta_interpolates_between_listed_contracts():
    chain = _Chain([_c("call", 100, 0.50, 0.21), _c("call", 105, 0.40, 0.20), _c("call", 110, 0.20, 0.24),
                    _c("call", 115, 0.10, 0.30), _c("call", 120, 0.22, 0.99, bid=0, ask=0.05),     # no bid: left out
                    _c("put", 90, -0.20, 0.27), _c("put", 95, -0.30, 0.25), _c("put", 100, -0.50, 0.22)])
    assert vol.iv_at_delta(chain, "call", 0.25) == pytest.approx(0.24 + (0.20 - 0.24) * 0.05 / 0.20)
    assert vol.iv_at_delta(chain, "put", -0.25) == pytest.approx(0.26)
    assert vol.iv_at_delta(chain, "call", 0.05) is None                    # the chain does not reach it
    assert vol.iv_at_delta(_Chain([_c("call", 100, 0.5, 0.2)]), "call", 0.25) is None


def test_expiration_row_flags_earnings_and_the_straddle():
    chain = _Chain([_c("call", 100, 0.52, 0.20, 2.0, 2.2), _c("put", 100, -0.48, 0.22, 1.8, 2.0),
                    _c("call", 110, 0.20, 0.19), _c("call", 105, 0.35, 0.195),
                    _c("put", 90, -0.20, 0.26), _c("put", 95, -0.32, 0.24)])
    today = date(2026, 10, 10)
    row = vol.expiration_row(chain, today, date(2026, 11, 1))
    assert row["earnings"] == "INCLUDES" and row["dte"] == 30 and row["atm_iv"] == pytest.approx(0.21)
    assert row["one_sd_move"] == pytest.approx(100 * 0.21 * math.sqrt(30 / 365))
    assert row["straddle"]["price"] == pytest.approx(2.1 + 1.9) and row["straddle_pct"] == pytest.approx(4.0)
    assert row["skew_25d"] is not None and row["skew_25d"] > 0
    assert vol.expiration_row(chain, today, date(2026, 11, 9))["earnings"] == "SAME_DAY"
    assert vol.expiration_row(chain, today, date(2026, 12, 1))["earnings"] == "BEFORE"
    assert vol.expiration_row(chain, today, None)["earnings"] is None
    empty = vol.expiration_row(_Chain([]), today, None)
    assert empty["atm_iv"] is None and empty["one_sd_move"] is None and empty["straddle"] is None


def test_pick_expirations_spreads_across_horizons():
    today = date(2026, 10, 10)
    fridays = [(today + timedelta(days=d)).isoformat() for d in range(0, 400, 7)]
    picked = pick_expirations(fridays, today, 8)
    days = [(date.fromisoformat(d) - today).days for d in picked]
    assert picked == sorted(set(picked)) and len(picked) <= 8 and 0 not in days
    assert days[:2] == [7, 14] and any(25 <= d <= 35 for d in days) and any(80 <= d <= 100 for d in days)
    assert pick_expirations([today.isoformat()], today) == []


def test_profile_reports_the_premium_over_realized():
    chain = _Chain([_c("call", 100, 0.52, 0.30, 2.0, 2.2), _c("put", 100, -0.48, 0.30, 1.8, 2.0)], t=30 / 365)
    zigzag = [100 * (1.01 if i % 2 else 1.0) for i in range(80)]
    p = vol.profile([chain], date(2026, 10, 10), None, zigzag)
    hv20 = p["realized"]["hv20"]
    assert p["iv30"] == pytest.approx(0.30) and 0.1 < hv20 < 0.2
    assert p["iv_premium_points"] == pytest.approx(100 * (0.30 - hv20)) and p["iv_to_realized"] == pytest.approx(0.30 / hv20)
    assert p["event"] is None and p["skew"] is None
    still = vol.profile([chain], date(2026, 10, 10), None, [100 * 1.01 ** i for i in range(80)])
    assert still["realized"]["hv20"] == pytest.approx(0, abs=1e-12)
    assert "iv_premium_points" not in still and "iv_to_realized" not in still   # a stock that has not moved: no ratio
    assert "realized" not in vol.profile([chain], date(2026, 10, 10), None)


# --- the API on demo data -------------------------------------------------------------------------

@pytest.fixture()
def app(tmp_path):
    settings = Settings(data_mode="demo", db_path=tmp_path / "t.db")
    svc = TradeAssistant(settings, provider=DemoData(), db=Database(tmp_path / "t.db"))
    yield TestClient(create_app(settings, svc)), svc
    svc.db.close()


def test_volatility_endpoint_on_demo_data(app):
    c, _ = app
    r = c.get("/v2/options/aapl/volatility")
    assert r.status_code == 200, r.text
    p = r.json()
    assert p["symbol"] == "AAPL" and p["data_source"] == "demo" and len(p["expirations"]) >= 4
    assert [x["dte"] for x in p["expirations"]] == sorted(x["dte"] for x in p["expirations"])
    assert all(x["atm_iv"] and 0.05 < x["atm_iv"] < 1.5 for x in p["expirations"])
    assert p["shape"]["shape"] in ("UPWARD", "FLAT", "INVERTED") and p["iv30"] > 0
    assert p["realized"]["hv20"] is not None and "iv_rank" in p and p["failed"] == []
    assert "NaN" not in r.text


def test_monte_carlo_endpoint_on_demo_data(app):
    c, svc = app
    exp = c.get("/api/expirations", params={"symbol": "AAPL"}).json()["expirations"][3]["date"]
    chain = c.get("/api/chain", params={"symbol": "AAPL", "expiration": exp}).json()
    k = min((x for x in chain["contracts"] if x["option_type"] == "call"), key=lambda x: abs(x["moneyness_pct"] - 3))
    body = {"symbol": "AAPL", "expiration": exp, "option_type": "call", "strike": k["strike"]}
    r = c.post("/v2/options/montecarlo", json=body)
    assert r.status_code == 200, r.text
    m = r.json()
    assert m["consistent"] is True and m["premium_source"] == "midpoint" and m["buyer"]["premium"] == pytest.approx(k["mid"])
    assert m["seed"] == 7 and m["data_source"] == "demo"
    again = c.post("/v2/options/montecarlo", json=body).json()
    assert again["price"] == m["price"]
    mine = c.post("/v2/options/montecarlo", json={**body, "premium": 0.5, "contracts": 3}).json()
    assert mine["premium_source"] == "you" and mine["buyer"]["premium"] == 0.5
    jump = c.post("/v2/options/montecarlo", json={**body, "earnings_jump": True}).json()
    assert jump["event"] is not None or any("No earnings jump" in n for n in jump["notes"])
    missing = c.post("/v2/options/montecarlo", json={**body, "strike": 12345})
    assert missing.status_code == 400 and "not in the chain" in missing.json()["error"]
    assert c.post("/v2/options/montecarlo", json={**body, "option_type": "straddle"}).status_code == 422
    assert c.post("/v2/options/montecarlo", json={**body, "paths": 10}).status_code == 422
    assert svc.journal.list() == []                                        # a model only: nothing recorded


def test_the_options_lab_can_be_switched_off(tmp_path):
    settings = Settings(data_mode="demo", db_path=tmp_path / "o.db", features={**FEATURE_DEFAULTS, "options_lab": False})
    svc = TradeAssistant(settings, provider=DemoData(), db=Database(tmp_path / "o.db"))
    c = TestClient(create_app(settings, svc))
    r = c.get("/v2/options/AAPL/volatility")
    assert r.status_code == 404 and "FEATURE_OPTIONS_LAB=0" in r.json()["error"]
    assert c.post("/v2/options/montecarlo", json={"symbol": "AAPL", "expiration": "2026-11-20", "option_type": "call",
                                                  "strike": 200}).status_code == 404
    svc.db.close()
