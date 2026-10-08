"""Exposure, open risk, marks and risk models (2.2, Sprint 3).

Reference values are worked out independently in the tests (numpy least squares, sorting,
textbook option prices), not by calling the code under test a second way.
"""

from __future__ import annotations

import math
from datetime import datetime, timedelta

import numpy as np
import pandas as pd
import pytest
from fastapi.testclient import TestClient

from tradeassist import portfolio
from tradeassist.api import create_app
from tradeassist.config import Settings
from tradeassist.db import Database
from tradeassist.desk import quote_sides
from tradeassist.optionmath import NY, black76_greeks, black76_price
from tradeassist.providers.demo import DemoData
from tradeassist.service import TradeAssistant

NOW = datetime(2026, 10, 6, 14, 0, tzinfo=NY)
LEGS = [{"option_type": "CALL", "strike": 200.0, "expiration": "2027-01-15"}]
OPT_MARK = {"price": 10.0, "underlying": 200.0, "delta": 0.55, "theta": -0.05, "vega": 0.4, "gamma": 0.01,
            "iv": 0.30, "model_vol": 0.28, "growth": 0.03, "rate": 0.045, "dte": 100, "bid": 9.8, "ask": 10.2,
            "liquidation_price": 9.8}


def view(symbol, direction="LONG", qty=100, price=100.0, *, invalidation=None, risk_amount=None, kind="STOCK",
         mark=None, legs=None, pid=1):
    m = mark if mark is not None else {"price": price, "underlying": price,
                                       "liquidation_price": price - 0.05 if direction == "LONG" else price + 0.05}
    return {"id": pid, "symbol": symbol, "direction": direction, "instrument_type": kind,
            "multiplier": 100 if kind == "OPTION" else 1, "description": f"{direction} {qty} {symbol}",
            "bias": 1 if direction == "LONG" else -1, "invalidation_price": invalidation, "risk_amount": risk_amount,
            "thesis_check": {"status": "INTACT"}, "legs": legs or [], "ledger": {"open_quantity": qty}, "status": "OPEN",
            "mark": m, "executions": []}


SECTORS = {"KO": "XLP", "PEP": "XLP", "AAPL": "XLK", "MSFT": "XLK"}


def analyse(views, account=100_000.0, sector=SECTORS.get):
    daily = portfolio.daily_loss([], "REALIZED_PLUS_UNREALIZED", account, 3.0, NOW)
    return portfolio.analyse(views, {}, account, 10.0, daily, sector)


# --- 3.1 gross and net exposure --------------------------------------------------------------------------

def test_3_1_offsetting_long_and_short_show_net_and_gross():
    r = analyse([view("KO", "LONG", 100, 60.0, pid=1), view("KO", "SHORT", 100, 60.0, pid=2),
                 view("PEP", "LONG", 50, 150.0, pid=3)], account=20_000)
    t = r["totals"]
    assert t["net_exposure"] == pytest.approx(7_500) and t["gross_exposure"] == pytest.approx(19_500)
    assert t["long_exposure"] == pytest.approx(13_500) and t["short_exposure"] == pytest.approx(-6_000)
    ko = next(x for x in r["by_symbol"] if x["symbol"] == "KO")
    assert ko["net"] == 0 and ko["gross"] == pytest.approx(12_000) and ko["exposure"] == 0
    assert ko["long"] == pytest.approx(6_000) and ko["short"] == pytest.approx(-6_000) and ko["positions"] == 2
    staples = next(x for x in r["by_sector"] if x["sector"] == "Consumer staples")
    assert staples["net"] == pytest.approx(7_500) and staples["gross"] == pytest.approx(19_500)
    # A hedged position is still a large position: the concentration check uses gross.
    text = " ".join(w["text"] for w in r["warnings"])
    assert "KO exposure of $12,000 is 60% of the account (net $0 after longs and shorts offset)" in text


def test_3_1_options_keep_delta_premium_notional_and_value_apart():
    opt = portfolio.exposure(view("AAPL", qty=2, kind="OPTION", mark=dict(OPT_MARK), legs=LEGS), 1.0)
    assert opt["dollar_delta"] == pytest.approx(0.55 * 100 * 2 * 200)          # delta-equivalent stock dollars
    assert opt["market_value"] == pytest.approx(2_000) == opt["premium_at_risk"]  # what the contracts are worth
    assert opt["notional"] == pytest.approx(2 * 100 * 200)                         # contracts x 100 x stock price
    assert opt["liquidation_value"] == pytest.approx(9.8 * 200)                    # at the bid
    r = analyse([view("AAPL", qty=2, kind="OPTION", mark=dict(OPT_MARK), legs=LEGS, pid=1),
                 view("MSFT", qty=10, price=400.0, pid=2)])
    t = r["totals"]
    assert t["gross_exposure"] == pytest.approx(22_000 + 4_000)
    assert t["option_premium_at_risk"] == pytest.approx(2_000) and t["notional"] == pytest.approx(40_000 + 4_000)
    assert t["market_value"] == pytest.approx(2_000 + 4_000)
    assert set(portfolio.DEFINITIONS) >= {"gross_exposure", "net_exposure", "market_value", "notional",
                                          "premium_at_risk", "liquidation_value"}


# --- 3.2 open-risk completeness --------------------------------------------------------------------------

def test_3_2_each_position_has_a_risk_basis():
    e = lambda v: portfolio.exposure(v, 1.0)                                       # noqa: E731
    inv = e(view("A", qty=100, price=50.0, invalidation=45.0))
    assert inv["risk_basis"] == "INVALIDATION" and inv["open_risk"] == pytest.approx(500)
    cfg = e(view("B", qty=100, price=50.0, risk_amount=300.0))
    assert cfg["risk_basis"] == "CONFIGURED" and cfg["open_risk"] == 300
    unk = e(view("C", qty=100, price=50.0))
    assert unk["risk_basis"] == "UNKNOWN" and unk["open_risk"] is None and "no invalidation" in unk["risk_reason"]
    past = e(view("D", qty=100, price=40.0, invalidation=45.0))
    assert past["risk_basis"] == "BEYOND_INVALIDATION" and past["open_risk"] is None and past["beyond_invalidation"]
    assert past["risk_to_invalidation"] == 0                       # the distance is zero; the risk is not
    short_past = e(view("E", "SHORT", 100, 60.0, invalidation=55.0))
    assert short_past["risk_basis"] == "BEYOND_INVALIDATION"
    prem = e(view("F", qty=2, kind="OPTION", mark=dict(OPT_MARK), legs=LEGS))
    assert prem["risk_basis"] == "PREMIUM" and prem["open_risk"] == pytest.approx(2_000)
    model = e(view("G", qty=2, kind="OPTION", mark=dict(OPT_MARK), legs=LEGS, invalidation=180.0))
    assert model["risk_basis"] == "INVALIDATION" and 0 < model["open_risk"] < 2_000
    gone = e(view("H", qty=2, kind="OPTION", mark=dict(OPT_MARK), legs=LEGS, invalidation=210.0))  # call, stock below
    assert gone["risk_basis"] == "PREMIUM" and gone["open_risk"] == pytest.approx(2_000)


def test_3_2_the_limit_conclusion_says_whether_it_covers_everything():
    within = analyse([view("A", qty=100, price=50.0, invalidation=45.0)])["risk_coverage"]
    assert within["within_limit"] is True and within["includes_all_positions"] and "across all 1 position" in within["conclusion"]
    partial = analyse([view("A", qty=100, price=50.0, invalidation=45.0, pid=1), view("C", qty=10, price=50.0, pid=2)])
    rc = partial["risk_coverage"]
    assert rc["within_limit"] is None and rc["status"] == "PARTIAL" and rc["unknown"][0]["description"] == "LONG 10 C"
    assert "not known whether the whole is inside the limit" in rc["conclusion"]
    over = analyse([view("A", qty=1000, price=50.0, invalidation=30.0, pid=1), view("C", qty=10, price=50.0, pid=2)],
                   account=100_000)["risk_coverage"]
    assert over["within_limit"] is False and over["total_known"] == pytest.approx(20_000)
    assert over["conclusion"].startswith("Open risk is at least $20,000, above the 10% limit")
    mixed = analyse([view("A", qty=100, price=50.0, invalidation=45.0, pid=1), view("B", qty=10, price=50.0, risk_amount=80, pid=2),
                     view("F", qty=1, kind="OPTION", mark=dict(OPT_MARK), legs=LEGS, pid=3)])["risk_coverage"]
    assert mixed["bases"] == {"INVALIDATION": 500.0, "PREMIUM": 1000.0, "CONFIGURED": 80.0}
    assert mixed["total_known"] == pytest.approx(1_580) and mixed["within_limit"] is True


# --- 3.5 marks and liquidation --------------------------------------------------------------------------

def test_3_5_liquidation_uses_the_side_you_would_trade_against():
    long_ = quote_sides(9.8, 10.2, 10.1, +1)
    assert long_["liquidation_price"] == 9.8 and long_["mid"] == pytest.approx(10.0)
    assert long_["spread_pct"] == pytest.approx(4.0) and "bid" in long_["liquidation_basis"]
    short = quote_sides(9.8, 10.2, 10.1, -1)
    assert short["liquidation_price"] == 10.2 and "ask" in short["liquidation_basis"]
    nobid = quote_sides(0.0, 0.05, None, +1)
    assert nobid["liquidation_price"] == 0.0 and nobid["mid"] is None and "no bid" in nobid["liquidation_basis"]
    assert quote_sides(None, 10.2, 10.0, +1)["liquidation_price"] is None
    assert quote_sides(10.5, 10.2, 10.0, +1)["liquidation_price"] is None              # crossed: not trusted


def test_3_5_position_views_carry_mark_and_liquidation_pnl(tmp_path):
    settings = Settings(data_mode="demo", db_path=tmp_path / "t.db")
    c = TestClient(create_app(settings, TradeAssistant(settings, provider=DemoData(), db=Database(":memory:"))))
    long_ = c.post("/v2/positions", json={"symbol": "AAPL", "quantity": 10, "entry": {"price": 100.0}}).json()
    short = c.post("/v2/positions", json={"symbol": "AAPL", "direction": "SHORT", "quantity": 10,
                                          "entry": {"price": 100.0}}).json()
    for p, sign in ((long_, 1), (short, -1)):
        mk, pnl = p["mark"], p["pnl"]
        assert mk["mark_basis"] in ("last trade", "bid/ask midpoint", "closing price")
        liq = mk["bid"] if sign > 0 else mk["ask"]
        assert mk["liquidation_price"] == liq
        assert pnl["liquidation_unrealized"] == pytest.approx(sign * (liq - 100.0) * 10)
        assert pnl["liquidation_value"] == pytest.approx(sign * liq * 10)
        # Closing costs the spread: the liquidation estimate is never better than the analytical mark.
        assert pnl["liquidation_unrealized"] <= pnl["unrealized"] + 1e-9


# --- 3.6 risk models ----------------------------------------------------------------------------------

def bars_from(closes, dates):
    return pd.DataFrame({"date": dates, "open": closes, "high": closes, "low": closes, "close": closes, "volume": 1e6})


def test_3_6_missing_days_are_not_zero_returns_and_beta_uses_the_overlap():
    rng = np.random.default_rng(3)
    dates = pd.bdate_range("2026-01-02", periods=150)
    m = 0.01 * rng.standard_normal(150)
    noise = 0.004 * rng.standard_normal(150)
    spy = 400 * np.exp(np.cumsum(m))
    stock = 50 * np.exp(np.cumsum(1.5 * m + noise))
    keep = np.ones(150, bool)
    keep[[20, 21, 60, 100]] = False                                                # the stock misses four days
    rets = portfolio.returns_frame({"SPY": bars_from(spy, dates), "X": bars_from(stock[keep], dates[keep])}, days=200)
    assert rets["X"].isna().sum() >= 4 and not (rets["X"] == 0).any()               # no invented zero returns
    # Old behaviour, for contrast: carrying the close forward turns a missing day into a 0% day.
    filled = pd.DataFrame({"SPY": spy, "X": pd.Series(stock[keep], dates[keep])}, index=dates).ffill().pct_change()
    assert (filled["X"] == 0).sum() >= 4
    # Reference beta: least squares on log-free simple returns, computed here from the raw arrays.
    pair = rets[["X", "SPY"]].dropna()
    slope = np.linalg.lstsq(np.c_[pair["SPY"].to_numpy(), np.ones(len(pair))], pair["X"].to_numpy(), rcond=None)[0][0]
    st = portfolio.beta_stats(rets)["X"]
    assert st["status"] == "OK" and st["samples"] == len(pair) and st["beta"] == pytest.approx(slope, rel=1e-9)
    assert st["beta"] == pytest.approx(1.5, abs=0.15)


def test_3_6_too_little_overlap_gives_no_beta_and_says_so():
    dates = pd.bdate_range("2026-01-02", periods=150)
    spy = 400 * np.exp(np.cumsum(0.01 * np.random.default_rng(1).standard_normal(150)))
    young = bars_from(np.linspace(10, 12, 30), dates[-30:])                        # listed six weeks ago
    rets = portfolio.returns_frame({"SPY": bars_from(spy, dates), "NEW": young})
    st = portfolio.beta_stats(rets)["NEW"]
    assert st["beta"] is None and st["status"] == "INSUFFICIENT" and st["samples"] == 29
    assert "NEW" not in portfolio.betas(rets)
    x = portfolio.exposure(view("NEW", qty=100, price=12.0), None)
    assert x["beta_assumed"] and x["beta_dollars"] == x["dollar_delta"]
    q = portfolio.return_quality(rets)["NEW"]
    assert not q["reliable"] and "fewer than the 40 needed" in q["issues"][0]


def test_3_6_a_stale_feed_is_flagged():
    dates = pd.bdate_range("2026-01-02", periods=80)
    closes = np.r_[np.linspace(10, 11, 60), np.full(20, 11.0)]                       # 20 identical closes
    spy = np.linspace(400, 420, 80)
    q = portfolio.return_quality(portfolio.returns_frame({"SPY": bars_from(spy, dates), "S": bars_from(closes, dates)}))
    assert q["S"]["longest_unchanged_run"] == 20 and any("21 unchanged closes" in i for i in q["S"]["issues"])


def test_3_6_bad_day_estimates_match_hand_calculations():
    rng = np.random.default_rng(7)
    n = 120
    a, b = 0.012 * rng.standard_normal(n), 0.02 * rng.standard_normal(n)
    rets = pd.DataFrame({"A": a, "B": b}, index=pd.bdate_range("2026-01-02", periods=n))
    exp = {"A": 10_000.0, "B": -4_000.0}
    out = portfolio.one_day_risk(rets, exp)
    w = np.array([10_000.0, -4_000.0])
    cov = np.cov(np.vstack([a, b]), ddof=1)                                         # sample covariance, as pandas
    assert out["parametric_95"] == pytest.approx(1.645 * math.sqrt(w @ cov @ w), rel=1e-12)
    pnl = np.sort(10_000 * a - 4_000 * b)
    q = np.quantile(pnl, 0.05)
    assert out["historical_95"] == pytest.approx(-q, rel=1e-12)
    assert out["expected_shortfall_95"] == pytest.approx(-pnl[pnl <= q].mean(), rel=1e-12)
    assert out["worst_day"] == pytest.approx(-pnl[0]) and out["samples"] == n
    assert "not a maximum" in out["note"]
    short = portfolio.one_day_risk(rets.head(30), exp)
    assert not short["available"] and "40" in short["reason"]


def test_3_6_stress_never_takes_a_stock_below_zero():
    big = portfolio.exposure(view("LEV", qty=100, price=50.0), 3.0)
    shorted = portfolio.exposure(view("LEV2", "SHORT", 100, 50.0, pid=2), 3.0)
    crash, = portfolio.stress([big, shorted], [{"name": "crash", "market_move_pct": -50}], 100_000)
    rows = {r["symbol"]: r for r in crash["positions"]}
    assert rows["LEV"]["pnl"] == pytest.approx(-5_000) and rows["LEV"]["clamped"]       # all of it, not 150%
    assert rows["LEV2"]["pnl"] == pytest.approx(5_000) and rows["LEV2"]["stock_move"] == -1.0
    opt = portfolio.exposure(view("O", qty=1, kind="OPTION", mark={**OPT_MARK, "underlying": 200.0}, legs=LEGS), 4.0)
    wipe, = portfolio.stress([opt], [{"name": "wipe", "market_move_pct": -50, "iv_shift_points": 20}], 100_000)
    assert wipe["pnl"] == pytest.approx(-1_000, abs=1e-6)                           # a bought call loses its premium, no more


def test_3_6_option_model_reference_values():
    # Hull, Options, Futures and Other Derivatives, Example 15.6: S=42, K=40, r=10%, sigma=20%, T=0.5.
    s, k, r, sig, t = 42.0, 40.0, 0.10, 0.20, 0.5
    f = s * math.exp(r * t)
    assert black76_price(f, k, t, r, sig, "call") == pytest.approx(4.76, abs=0.005)
    assert black76_price(f, k, t, r, sig, "put") == pytest.approx(0.81, abs=0.005)
    # Vega is per volatility point: a one-point rise moves the price by about vega.
    g = black76_greeks(s, f, k, t, r, sig, "call")
    bumped = black76_price(f, k, t, r, sig + 0.01, "call") - black76_price(f, k, t, r, sig, "call")
    assert bumped == pytest.approx(g.vega, rel=0.02)          # equal to first order; convexity adds about 1%
    # The stress test's volatility shift is in the same units: +5 points is +0.05.
    x = portfolio.exposure(view("O", qty=1, kind="OPTION", mark=dict(OPT_MARK), legs=LEGS), 1.0)
    up, = portfolio.stress([x], [{"name": "vol", "iv_shift_points": 1}], 100_000, now=datetime.now(NY))
    assert 0 < up["pnl"] < 100 * 0.6                                                 # about one vega (0.4 x 100)


def test_3_6_correlation_needs_enough_shared_days():
    dates = pd.bdate_range("2026-01-02", periods=150)
    rng = np.random.default_rng(2)
    spy = 400 * np.exp(np.cumsum(0.01 * rng.standard_normal(150)))
    a = 50 * np.exp(np.cumsum(0.01 * rng.standard_normal(150)))
    young = bars_from(np.linspace(10, 12, 20), dates[-20:])
    bars = {"SPY": bars_from(spy, dates), "A": bars_from(a, dates), "NEW": young}
    daily = portfolio.daily_loss([], "REALIZED_PLUS_UNREALIZED", 100_000, 3.0, NOW)
    r = portfolio.analyse([view("A", pid=1), view("NEW", qty=10, price=12.0, pid=2)], bars, 100_000, 10.0, daily, lambda s: None)
    m = dict(zip(r["correlation"]["symbols"], r["correlation"]["matrix"]))
    assert m["A"][r["correlation"]["symbols"].index("NEW")] is None
    assert any(w.get("code") == "RETURN_QUALITY" and "NEW" in w["text"] for w in r["warnings"])
    assert r["bad_day"]["available"] is False                                         # 19 shared days only


def test_3_6_expiry_and_tiny_prices_stay_bounded():
    # Repricing at a stock price near zero must not fail or go negative.
    x = portfolio.exposure(view("O", qty=1, kind="OPTION", mark=dict(OPT_MARK), legs=LEGS), 1.0)
    v = portfolio.reprice_option(x, 1e-6, 0.0, datetime.now(NY))
    assert v is not None and 0.0 <= v < 1e-6
    expired = dict(x, option={**x["option"], "expiration": (datetime.now(NY) - timedelta(days=1)).date().isoformat()})
    assert portfolio.reprice_option(expired, 250.0, 0.0, datetime.now(NY)) == pytest.approx(50.0)
