import math
from datetime import datetime

import numpy as np
import pytest

from tradeassist.optionmath import NY, black76_price, prob_finish_beyond, years_to_expiry
from tradeassist.simulate import (Leg, crossings, expiry_profile, market_odds, position_pnl, simulate,
                                  trading_days, years_between)

NOW = datetime(2026, 10, 6, 11, 0, tzinfo=NY)
EXP = "2026-11-20"
RATE = 0.04


def leg(kind="call", strike=100.0, side=1, n=1, entry=5.0, vol=0.30, growth=0.04, exp=EXP):
    return Leg(kind, strike, exp, side, n, entry, vol, growth)


def test_value_today_matches_the_app_pricing_model():
    one = leg()
    t = years_to_expiry(EXP, NOW)
    spot = 103.0
    expected = black76_price(spot * math.exp(0.04 * t), 100.0, t, RATE, 0.30, "call")
    pnl = position_pnl([one], np.array([spot]), NOW, RATE)[0]
    assert pnl == pytest.approx(100 * (expected - 5.0))
    # Five volatility points higher is the same as pricing at 35%.
    higher = position_pnl([one], np.array([spot]), NOW, RATE, iv_shift=0.05)[0]
    assert higher == pytest.approx(100 * (black76_price(spot * math.exp(0.04 * t), 100.0, t, RATE, 0.35, "call") - 5.0))
    # On the expiration day the option is worth what it is in the money.
    at_close = datetime(2026, 11, 20, 16, 0, tzinfo=NY)
    assert position_pnl([one], np.array([90.0, 112.0]), at_close, RATE).tolist() == pytest.approx([-500.0, 700.0])
    sold = leg(side=-1, n=3)
    assert position_pnl([sold], np.array([112.0]), at_close, RATE)[0] == pytest.approx(-3 * 700.0)


@pytest.mark.parametrize("legs, gain, loss, breakevens", [
    ([leg("call", 100, 1, 1, 5.0)], None, -500.0, [105.0]),                           # long call
    ([leg("put", 100, 1, 2, 4.0)], 2 * (100 - 4) * 100, -800.0, [96.0]),              # long puts
    ([leg("put", 100, -1, 1, 3.0)], 300.0, -9700.0, [97.0]),                          # short put
    ([leg("call", 100, -1, 1, 3.0)], 300.0, None, [103.0]),                           # short call
    ([leg("call", 100, 1, 1, 5.0), leg("call", 110, -1, 1, 2.0)], 700.0, -300.0, [103.0]),   # call spread
    ([leg("call", 100, 1, 1, 5.0), leg("put", 100, 1, 1, 4.0)], None, -900.0, [91.0, 109.0]),  # straddle
    ([leg("put", 90, 1, 1, 1.0), leg("put", 95, -1, 1, 2.5), leg("call", 105, -1, 1, 2.5),
      leg("call", 110, 1, 1, 1.0)], 300.0, -200.0, [92.0, 108.0]),                    # iron condor
])
def test_expiry_profile_is_exact(legs, gain, loss, breakevens):
    p = expiry_profile(legs)
    assert p["max_gain"] == (pytest.approx(gain) if gain is not None else None)
    assert p["max_loss"] == (pytest.approx(loss) if loss is not None else None)
    assert p["unlimited_gain"] == (gain is None) and p["unlimited_loss"] == (loss is None)
    assert p["breakevens"] == pytest.approx(breakevens)
    at_close = datetime(2026, 11, 20, 16, 0, tzinfo=NY)
    for b in breakevens:                                                     # really zero there
        assert position_pnl(legs, np.array([b]), at_close, RATE)[0] == pytest.approx(0.0, abs=1e-6)


def test_market_odds_match_the_closed_form_for_a_long_call():
    one = leg(entry=5.0, vol=0.30)
    at_close = datetime(2026, 11, 20, 16, 0, tzinfo=NY)
    odds = market_odds([one], 100.0, RATE, 0.30, 0.04, at_close, NOW, max_loss=-500.0)
    t = years_between(NOW, at_close)       # in UTC: the clocks go back on November 1
    forward = 100.0 * math.exp(0.04 * t)
    assert odds["p_profit"] == pytest.approx(prob_finish_beyond(forward, 105.0, t, 0.30, above=True), abs=0.003)
    assert odds["p_max_loss"] == pytest.approx(prob_finish_beyond(forward, 100.0, t, 0.30, above=False), abs=0.003)
    assert odds["low"] < odds["mid"] < odds["high"] and odds["mid"] == pytest.approx(
        forward * math.exp(-0.5 * 0.09 * t))
    assert odds["one_sd_move"] == pytest.approx(100 * 0.30 * math.sqrt(t))
    today = market_odds([one], 100.0, RATE, 0.30, 0.04, NOW, NOW)            # no time: no spread of outcomes
    assert today["p_profit"] in (0.0, 1.0) and today["mid"] == 100.0


def test_full_simulation_shapes_and_controls():
    legs = [leg("call", 100, 1, 2, 5.0), leg("call", 110, -1, 2, 2.0)]
    out = simulate(legs, 100.0, RATE, 0.30, NOW)
    assert out["eval_date"] == "2026-10-06" and out["first_expiration"] == EXP and out["same_expiry"]
    assert out["net_premium"] == pytest.approx(2 * 100 * (5.0 - 2.0))
    assert out["expiry"]["max_gain"] == pytest.approx(1400.0) and out["expiry"]["max_loss"] == pytest.approx(-600.0)
    prices = np.array(out["curve"]["prices"])
    assert prices[0] < 100 < prices[-1] and prices[0] <= 95 and prices[-1] >= 110 * 1.05 - 1e-9
    assert len(out["curve"]["pnl_at_date"]) == len(prices) == len(out["curve"]["pnl_at_expiry"])
    grid = out["grid"]
    assert grid["dates"][0] == "2026-10-06" and grid["dates"][-1] == EXP and len(grid["dates"]) == 7
    assert 100.0 in grid["prices"] and grid["prices"] == sorted(grid["prices"], reverse=True)
    assert len(grid["pnl"]) == len(grid["prices"]) and all(len(r) == 7 for r in grid["pnl"])
    last_col = [row[-1] for row in grid["pnl"]]
    expected = position_pnl(legs, np.array(grid["prices"]), datetime(2026, 11, 20, 16, 0, tzinfo=NY), RATE)
    assert last_col == pytest.approx(expected.tolist())
    assert all(datetime.fromisoformat(d).weekday() < 5 for d in out["dates"])
    assert out["greeks"]["delta"] > 0 and out["greeks"]["theta"] < 0      # a long call spread
    # A later date and a volatility drop are honoured.
    later = simulate(legs, 100.0, RATE, 0.30, NOW, eval_day=datetime(2026, 11, 2).date(), iv_shift=-0.10)
    assert later["eval_date"] == "2026-11-02" and later["iv_shift"] == -0.10
    mid = len(prices) // 2
    assert later["curve"]["pnl_at_date"][mid] != out["curve"]["pnl_at_date"][mid]
    # Dates past the first expiration are pulled back to it; a fixed range is used when asked.
    clipped = simulate(legs, 100.0, RATE, 0.30, NOW, eval_day=datetime(2027, 1, 1).date(), range_pct=0.2)
    assert clipped["eval_date"] == EXP and clipped["curve"]["prices"][0] == pytest.approx(80.0)
    assert clipped["curve"]["pnl_at_date"] == pytest.approx(clipped["curve"]["pnl_at_expiry"])
    with pytest.raises(ValueError):
        simulate([], 100.0, RATE, 0.30, NOW)


def test_legs_on_different_expirations_are_valued_by_the_model_at_the_first():
    near, far = leg("call", 100, -1, 1, 3.0, exp="2026-10-30"), leg("call", 100, 1, 1, 6.0, exp="2026-12-18")
    out = simulate([near, far], 100.0, RATE, 0.30, NOW)
    assert not out["same_expiry"] and not out["expiry"]["exact"] and out["first_expiration"] == "2026-10-30"
    assert out["expiry"]["max_gain"] > 0 > out["expiry"]["max_loss"]       # a calendar: hurt by big moves
    assert any("different days" in n for n in out["notes"])


def test_helpers():
    assert crossings(np.array([1.0, 2.0, 3.0]), np.array([-1.0, 1.0, 3.0])) == [1.5]
    days = trading_days(datetime(2026, 10, 9).date(), datetime(2026, 10, 13).date())
    assert [d.isoformat() for d in days] == ["2026-10-09", "2026-10-12", "2026-10-13"]


# --- through the app --------------------------------------------------------------------------

def test_simulate_endpoint_resolves_legs_from_the_chain(tmp_path):
    from fastapi.testclient import TestClient
    from tradeassist.api import create_app
    from tradeassist.config import Settings
    settings = Settings(data_mode="demo", db_path=tmp_path / "t.db")
    client = TestClient(create_app(settings))
    exps = client.get("/api/expirations?symbol=AAPL").json()["expirations"]
    exp = next(e["date"] for e in exps if e["dte"] >= 25)
    chain = client.get(f"/api/chain?symbol=AAPL&expiration={exp}&width_pct=1000").json()
    k = min({c["strike"] for c in chain["contracts"]}, key=lambda s: abs(s - chain["spot"]))
    row = next(c for c in chain["contracts"] if c["strike"] == k and c["option_type"] == "call")
    r = client.post("/api/simulate", json={"symbol": "aapl", "legs": [{"expiration": exp, "option_type": "call",
                                                                         "strike": k, "contracts": 2}]})
    assert r.status_code == 200, r.text
    d = r.json()
    assert d["symbol"] == "AAPL" and d["legs"][0]["entry_source"] == "midpoint"
    assert d["legs"][0]["entry_price"] == pytest.approx(row["mid"]) and d["net_premium"] == pytest.approx(200 * row["mid"])
    mine = client.post("/api/simulate", json={"symbol": "AAPL", "iv_shift": -5, "legs": [
        {"expiration": exp, "option_type": "call", "strike": k, "entry_price": 1.23}]}).json()
    assert mine["legs"][0]["entry_source"] == "you" and mine["net_premium"] == pytest.approx(123.0)
    assert mine["iv_shift"] == pytest.approx(-0.05)
    bad = [({"legs": []}, 422), ({"legs": [{"expiration": exp, "option_type": "call", "strike": 1.23}]}, 400),
           ({"legs": [{"expiration": "2020-01-17", "option_type": "call", "strike": k}]}, 400),
           ({"legs": [{"expiration": exp, "option_type": "straddle", "strike": k}]}, 400),
           ({"iv_shift": 500, "legs": [{"expiration": exp, "option_type": "call", "strike": k}]}, 422),
           ({"legs": [{"expiration": exp, "option_type": "call", "strike": k}] * 5}, 422)]
    for body, status in bad:
        assert client.post("/api/simulate", json={"symbol": "AAPL", **body}).status_code == status, body


def test_technicals_endpoint(tmp_path):
    from fastapi.testclient import TestClient
    from tradeassist.api import create_app
    from tradeassist.config import Settings
    client = TestClient(create_app(Settings(data_mode="demo", db_path=tmp_path / "t.db")))
    d = client.get("/api/technicals/AAPL?bars=252").json()
    assert len(d["bars"]) == 252 and {"ema20", "sma50", "sma200", "macd", "macd_signal", "macd_hist", "state"} <= set(d["bars"][0])
    assert d["structure"]["state"] in ("up", "down", "range") and d["structure"]["headline"]
    assert all(s["date"] >= d["bars"][0]["date"] for s in d["swings"])
    assert all(lv["price"] < d["bars"][-1]["close"] for lv in d["levels"]["support"])
    assert set(d["lab"]) == {"structure_up", "structure_down", "structure_up_break", "structure_down_break"}
    wide = client.get("/api/technicals/AAPL?bars=252&swing_atr=3").json()
    assert len(wide["swings"]) < len(d["swings"])
    assert client.get("/api/technicals/AAPL?swing_atr=7").status_code == 400


def test_mixed_expirations_report_an_unlimited_loss_or_gain():
    short_two = [leg("call", 100, -2, 1, 3.0, exp="2026-10-30"), leg("call", 100, 1, 1, 6.0, exp="2026-12-18")]
    out = simulate(short_two, 100.0, RATE, 0.30, NOW)
    assert out["expiry"]["unlimited_loss"] and out["expiry"]["max_loss"] is None and out["expiry"]["max_gain"] > 0
    long_two = [leg("call", 100, 1, 2, 3.0, exp="2026-10-30"), leg("call", 100, -1, 1, 6.0, exp="2026-12-18")]
    out = simulate(long_two, 100.0, RATE, 0.30, NOW)
    assert out["expiry"]["unlimited_gain"] and out["expiry"]["max_gain"] is None


def test_on_the_expiration_day_at_expiration_means_the_close():
    morning = datetime(2026, 11, 20, 11, 0, tzinfo=NY)
    one = leg("call", 100, 1, 1, 1.5)
    out = simulate([one], 100.0, RATE, 0.30, morning)
    mid = min(range(len(out["curve"]["prices"])), key=lambda i: abs(out["curve"]["prices"][i] - 100.0))
    at_100 = out["curve"]["prices"][mid]
    assert out["curve"]["pnl_at_expiry"][mid] == pytest.approx(100 * (max(at_100 - 100, 0) - 1.5))
    assert out["expiry"]["max_loss"] == pytest.approx(-150.0)
    assert out["odds_at_expiry"]["high"] > out["odds_at_expiry"]["low"]     # five hours still to go
    assert out["grid"]["dates"] == ["2026-11-20"]
    row = out["grid"]["prices"].index(100.0)
    assert out["grid"]["pnl"][row][0] == pytest.approx(-150.0)


def test_on_a_weekend_today_is_still_today():
    saturday = datetime(2026, 10, 10, 12, 0, tzinfo=NY)
    out = simulate([leg()], 100.0, RATE, 0.30, saturday)
    assert out["eval_date"] == out["dates"][0] == out["grid"]["dates"][0] == "2026-10-10"
    assert out["dates"][1] == "2026-10-12"
    row = out["grid"]["prices"].index(100.0)
    now_value = position_pnl([leg()], np.array([100.0]), saturday, RATE)[0]
    assert out["grid"]["pnl"][row][0] == pytest.approx(now_value)


def test_a_position_that_costs_nothing_has_no_chance_of_a_full_loss():
    fly = [leg("call", 95, 1, 1, 7.0), leg("call", 100, -1, 2, 4.0), leg("call", 105, 1, 1, 1.0)]   # zero net cost
    out = simulate(fly, 100.0, RATE, 0.30, NOW)
    assert out["net_premium"] == pytest.approx(0.0, abs=1e-9) and out["odds_at_expiry"]["p_max_loss"] is None


def test_an_expired_leg_is_reported_as_expired(tmp_path):
    from fastapi.testclient import TestClient
    from tradeassist.api import create_app
    from tradeassist.config import Settings
    client = TestClient(create_app(Settings(data_mode="demo", db_path=tmp_path / "t.db")))
    r = client.post("/api/simulate", json={"symbol": "AAPL", "legs": [{"expiration": "2020-01-17", "option_type": "call", "strike": 100}]})
    assert r.status_code == 400 and "already expired" in r.json()["error"]
