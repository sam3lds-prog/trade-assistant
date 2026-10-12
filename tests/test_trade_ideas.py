"""2.7: trade ideas across up to 200 large companies, and the simulation of one trade.

The scan's side for each stock is Today's lean; the grade is Decide's; the dollar figures are the sized
trade's arithmetic at each level. The simulation is checked against closed forms: the chance of touching
one level before another, and an average of about break-even when the stock has no drift beyond the
forward.
"""

from __future__ import annotations

import math
import time
from datetime import date, datetime, timedelta

import numpy as np
import pytest
from fastapi.testclient import TestClient

from tradeassist.analysis import Analyst
from tradeassist.api import create_app
from tradeassist.config import Settings
from tradeassist.db import Database
from tradeassist.features import FEATURE_DEFAULTS
from tradeassist.ideas import TradeIdeas, earnings_within, money_at, rank, sort_key
from tradeassist.optionmath import NY
from tradeassist.providers.base import DataError
from tradeassist.providers.demo import DemoData
from tradeassist.scanner import LARGE_CAPS, NEXT_100, TOP_200, universe
from tradeassist.sectors import TICKER_FUND
from tradeassist.service import TradeAssistant
from tradeassist.simulate import Leg, leg_value
from tradeassist.tradesim import Plan, TradeSimError, contract_pnl, exit_labels, run, schedule, share_pnl, walk

NAMES = ["AAPL", "MSFT", "JPM", "KO", "CAT", "UNH", "XOM", "NVDA", "PG", "BA"]


@pytest.fixture
def svc(tmp_path):
    s = Settings(data_mode="demo", db_path=tmp_path / "t.db")
    service = TradeAssistant(s, provider=DemoData(), db=Database(tmp_path / "t.db"))
    yield service
    service.db.close()


# --- the universe -----------------------------------------------------------------------------

def test_the_200_are_the_100_large_caps_and_100_more_each_with_a_sector_fund():
    assert len(TOP_200) == 200 and len(set(TOP_200)) == 200
    assert list(TOP_200[:100]) == list(LARGE_CAPS) and not set(NEXT_100) & set(LARGE_CAPS)
    assert all(t in TICKER_FUND for t in TOP_200)            # no SEC lookup needed to compare with its sector
    assert universe("top200", [])[1] == list(TOP_200)
    with pytest.raises(ValueError, match="top200"):
        universe("everything", [])


# --- the arithmetic and the ranking -------------------------------------------------------------

def test_the_dollar_figures_are_the_sized_trade_at_each_level():
    long_ = money_at(100.0, 95.0, 110.0, 115.0, 1, 40)
    assert (long_["at_target_1"], long_["at_target_2"], long_["at_stop"]) == (400.0, 600.0, -200.0)
    short = money_at(100.0, 104.0, 92.0, 88.0, -1, 50)
    assert (short["at_target_1"], short["at_target_2"], short["at_stop"]) == (400.0, 600.0, -200.0)
    # A trade too large for the risk limit has no size: its dollars are unknown, its per-share arithmetic is not.
    none = money_at(1000.0, 700.0, 1600.0, 1900.0, 1, 0)
    assert none["at_target_1"] is None and none["per_share"]["target_1"] == 600.0


def test_ideas_rank_by_grade_then_score_then_reward_and_avoid_is_set_apart():
    row = lambda sym, g, sc, rr, d="LONG": {"symbol": sym, "grade": g, "score": sc, "reward_risk_1": rr, "direction": d}  # noqa: E731
    ideas = [row("C1", "C", 60, 3.0), row("B2", "B", 70, 1.5), row("B1", "B", 70, 2.0), row("A1", "A", 81, 1.0),
             row("X", "AVOID", 90, 5.0), row("S1", "B", 66, 2.0, "SHORT")]
    out = rank(ideas, [])
    assert [r["symbol"] for r in out["long"]] == ["A1", "B1", "B2", "C1"]
    assert [r["symbol"] for r in out["short"]] == ["S1"] and [r["symbol"] for r in out["avoid"]] == ["X"]
    assert sort_key(ideas[0]) > sort_key(ideas[3])


def test_earnings_inside_the_holding_period_are_counted_in_trading_days():
    monday = date(2026, 10, 12)
    assert earnings_within("2026-11-06", 20, monday) is True             # 19 trading days on
    assert earnings_within("2026-11-12", 20, monday) is False            # 23 trading days on
    assert earnings_within("2026-10-01", 20, monday) is False and earnings_within(None, 20, monday) is None


# --- the scan ------------------------------------------------------------------------------------

def test_each_idea_is_the_lean_side_graded_as_decide_grades_it(svc):
    analyst = Analyst(svc)
    ideas = TradeIdeas(svc, analyst, dict(FEATURE_DEFAULTS))
    seen = []
    out = ideas.scan(NAMES, "custom", lambda phase, done, total, cur: seen.append((phase, done, total)))
    assert seen[0] == ("reading", 0, len(NAMES)) and any(p == "grading" for p, _, _ in seen)
    listed = out["long"] + out["short"] + out["avoid"] + out["held_back"]
    assert listed and out["scanned"] + len(out["errors"]) == len(NAMES)
    from tradeassist.today import Today
    lean = {r["symbol"]: r for r in Today(svc, analyst, dict(FEATURE_DEFAULTS)).candidates("watchlist")["long"]}
    for r in out["long"] + out["short"]:
        d = 1 if r["direction"] == "LONG" else -1
        assert r["grade"] != "AVOID"
        g = analyst.evaluate_position({"symbol": r["symbol"], "direction": r["direction"], "entry_price": r["entry"],
                                       "horizon_days": 20})
        assert (g["grade"], g["score"]) == (r["grade"], r["score"])
        assert r["stop"] == pytest.approx(g["levels"]["invalidation"]) and r["target_1"] == pytest.approx(g["levels"]["target_1"])
        assert r["shares"] == g["sizing"]["shares"]
        if r["shares"]:
            assert r["at_target_1"] == pytest.approx(d * r["shares"] * (r["target_1"] - r["entry"]))
            assert r["at_stop"] == pytest.approx(-r["shares"] * abs(r["entry"] - r["stop"]))
        if r["symbol"] in lean:
            assert r["direction"] == "LONG"
    assert all(r["held_back"] for r in out["held_back"])
    assert out["long"] == sorted(out["long"], key=sort_key)


def test_a_stock_that_cannot_be_read_is_listed_not_ranked(svc):
    analyst = Analyst(svc)
    real = analyst.trend

    def trend(sym):
        if sym == "KO":
            raise DataError("no history for KO")
        return real(sym)
    analyst.trend = trend
    out = TradeIdeas(svc, analyst, dict(FEATURE_DEFAULTS)).scan(["KO", "AAPL"], "custom")
    assert out["errors"] == [{"symbol": "KO", "error": "no history for KO"}]
    assert all(r["symbol"] != "KO" for k in ("long", "short", "avoid", "held_back") for r in out[k])


@pytest.fixture
def client(svc):
    with TestClient(create_app(svc.settings, svc)) as c:
        yield c


def wait_done(c, limit=120.0):
    end = time.time() + limit
    while time.time() < end:
        st = c.get("/v2/ideas").json()
        if st["job"]["state"] != "running":
            return st
        time.sleep(0.3)
    raise AssertionError("the scan did not finish")


def test_the_scan_runs_in_the_background_one_at_a_time(client):
    assert client.get("/v2/ideas").json() == {"job": {"state": "idle"}, "result": None}
    first = client.post("/v2/ideas/scan", json={"universe": "watchlist"}).json()["job"]
    assert first["state"] == "running" and first["total"] == 20
    again = client.post("/v2/ideas/scan", json={"universe": "large_caps"}).json()["job"]
    assert again["id"] == first["id"] and again["universe"] == "watchlist"         # one at a time
    st = wait_done(client)
    assert st["job"]["state"] == "done" and st["result"]["universe"] == "watchlist"
    assert st["result"]["account"]["risk_budget"] == pytest.approx(200.0)
    assert client.post("/v2/ideas/scan", json={"universe": "everything"}).status_code == 422


def test_the_flag_switches_trade_ideas_off(tmp_path):
    s = Settings(data_mode="demo", db_path=tmp_path / "o.db", features={**FEATURE_DEFAULTS, "trade_ideas": False})
    service = TradeAssistant(s, provider=DemoData(), db=Database(tmp_path / "o.db"))
    with TestClient(create_app(s, service)) as c:
        assert c.get("/v2/ideas").status_code == 404
        assert c.post("/v2/ideas/simulate", json={"symbol": "AAPL"}).status_code == 404
    service.db.close()


# --- the simulation: the paths ----------------------------------------------------------------------

DAILY = [k / 252 for k in range(1, 2521)]          # ten years of trading days: nearly every path reaches a level


def test_the_chance_of_touching_the_target_first_matches_the_closed_form():
    """With no drift in the log price, the chance of reaching U before L from S is ln(S/L) / ln(U/L)."""
    vol = 0.4
    w = walk(Plan(1, 100.0, 90.0, 120.0, 130.0, 10), DAILY, vol, 0.5 * vol * vol)
    up = float((w["exit_kind"][0] == 1).mean())
    assert up == pytest.approx(math.log(100 / 90) / math.log(120 / 90), abs=0.01)
    # For a short the levels swap sides.
    w = walk(Plan(-1, 100.0, 110.0, 80.0, 70.0, 10), DAILY, vol, 0.5 * vol * vol)
    down = float((w["exit_kind"][0] == 1).mean())
    assert down == pytest.approx(math.log(110 / 100) / math.log(110 / 80), abs=0.01)


def test_touches_between_closes_are_counted():
    """Monitored only at the close, a level is reached less often than with the bridge correction."""
    vol, plan = 0.5, Plan(1, 100.0, 80.0, 108.0, 120.0, 1)
    times = [k / 252 for k in range(1, 21)]
    w = walk(plan, times, vol, 0.0)
    with_bridge = float((w["exit_kind"][0] == 1).mean())
    closes = np.vstack([np.full(w["closes"].shape[1], 100.0), w["closes"]])
    at_close = float((closes.max(axis=0) >= 108.0).mean())
    assert with_bridge > at_close + 0.03


def test_with_no_drift_the_average_is_about_break_even_whatever_the_plan():
    for plan in ("target_1", "target_2", "split"):
        p = Plan(1, 50.0, 46.0, 56.0, 60.0, 100, plan)
        w = walk(p, [k / 252 for k in range(1, 41)], 0.35, 0.0)
        pnl = share_pnl(p, w)
        se = pnl.std(ddof=1) / math.sqrt(len(pnl))
        assert abs(pnl.mean()) < 4 * se, plan
        assert set(np.unique(exit_labels(w["exit_kind"]))) <= {"target", "stop", "horizon", "both", "t1_stop", "t1_horizon"}


def test_the_same_inputs_give_the_same_figures_and_a_short_gains_as_the_price_falls():
    p = Plan(-1, 100.0, 106.0, 90.0, 85.0, 30)
    a, b = walk(p, DAILY[:20], 0.3, 0.0), walk(p, DAILY[:20], 0.3, 0.0)
    assert np.array_equal(a["exit_px"], b["exit_px"])
    pnl = share_pnl(p, a)
    target = a["exit_kind"][0] == 1
    assert target.any() and np.allclose(pnl[target], 30 * 10.0)
    assert np.allclose(pnl[a["exit_kind"][0] == -1], -30 * 6.0)


def test_levels_on_the_wrong_side_are_refused():
    with pytest.raises(TradeSimError, match="stop must be below"):
        Plan(1, 100.0, 101.0, 110.0, 120.0, 10).check()
    with pytest.raises(TradeSimError, match="Target 1 must be below"):
        Plan(-1, 100.0, 105.0, 101.0, 95.0, 10).check()
    with pytest.raises(TradeSimError, match="Target 2 must be beyond"):
        Plan(1, 100.0, 95.0, 110.0, 105.0, 10, "split").check()


def test_the_contract_is_sold_on_the_same_exits_at_its_own_value():
    now = datetime(2026, 10, 12, 10, 0, tzinfo=NY)
    days, moments, times = schedule(now.date(), now.date() + timedelta(days=28), now)
    leg = Leg("call", 100.0, "2026-12-18", 1, 2, 4.0, 0.3, 0.0)
    p = Plan(1, 100.0, 95.0, 108.0, 112.0, 0)
    w = walk(p, times, 0.3, 0.0)
    pnl = contract_pnl(leg, moments, w, 0.04)
    stopped = np.flatnonzero(w["exit_kind"][0] == -1)
    k = int(w["exit_step"][0, stopped[0]])
    expect = (leg_value(leg, np.array([95.0]), moments[k], 0.04)[0] - 4.0) * 200
    assert pnl[stopped[0]] == pytest.approx(expect)
    assert pnl.min() >= -4.0 * 200 - 1e-9                                       # never more than the premium
    out = run(plan=p, days=days, moments=moments, times=times, vol=0.3, growth=0.0, rate=0.04, leg=leg)
    assert out["with_contract"]["cost"] == pytest.approx(800.0) and len(out["fan"]) == len(days)
    assert sum(x["share"] for x in out["with_shares"]["by_exit"]) == pytest.approx(1.0)


# --- the simulation through the app ----------------------------------------------------------------

def test_a_simulation_uses_the_grade_s_levels_and_size_and_the_chain(client):
    r = client.post("/v2/ideas/simulate", json={"symbol": "AAPL", "direction": "LONG", "exit_plan": "split"})
    assert r.status_code == 200
    s = r.json()
    g = client.post("/v2/position/evaluate", json={"symbol": "AAPL", "direction": "LONG", "entry_price": s["entry"]}).json()
    assert s["stop"] == pytest.approx(g["levels"]["invalidation"]) and s["shares"] == g["sizing"]["shares"]
    assert s["volatility"]["source"] == "implied" and s["volatility"]["expiration"]
    assert s["with_shares"]["fixed"]["at_stop"] == pytest.approx(s["shares"] * (s["stop"] - s["entry"]))
    if s["contract_spec"]:
        assert s["contract_spec"]["option_type"] == "CALL" and s["with_contract"]["cost"] > 0
    assert "not a forecast" in s["model"]
    # Your own levels and size, and shares only.
    mine = client.post("/v2/ideas/simulate", json={"symbol": "AAPL", "direction": "LONG", "stop": round(s["entry"] * 0.95, 2),
                                                   "target_1": round(s["entry"] * 1.06, 2), "shares": 7, "contract": "none"}).json()
    assert mine["shares"] == 7 and mine["contract_spec"] is None and "with_contract" not in mine
    assert mine["levels_source"] == {"stop": "yours", "targets": "yours"}
    bad = client.post("/v2/ideas/simulate", json={"symbol": "AAPL", "direction": "LONG", "stop": s["entry"] * 1.1})
    assert bad.status_code == 400 and "below the entry" in bad.text


def test_without_option_prices_the_stock_s_own_volatility_is_used_and_said(client):
    svc = client.app.state.assistant

    def no_chain(*a, **k):
        raise DataError("no option chain")
    svc.analyst.evaluate_options = no_chain
    s = client.post("/v2/ideas/simulate", json={"symbol": "KO", "direction": "SHORT"}).json()
    assert s["volatility"]["source"] == "realized" and s["contract_spec"] is None
    assert any("own volatility" in n for n in s["notes"]) and any("borrow" in n for n in s["notes"])
