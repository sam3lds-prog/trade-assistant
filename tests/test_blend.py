"""2.15: the blend optimizer: correlated paths, each trade's exits, the goals, every cap and limit, and the equal-risk
split shown when no record beats its costs.

The rules pinned here: the split never spends more than the budget, never puts more than the cap in one trade or
one sector, keeps every trade's loss at the stop inside the per-trade limit when the limits apply, keeps the
tail loss inside the goal's limit, takes one instrument per stock, and with no edge (or a negative one) holds cash
rather than invent a reason to trade."""

from __future__ import annotations

import itertools
from datetime import date, datetime

import numpy as np
import pandas as pd
import pytest

from tradeassist import blend as B
from tradeassist import marketdays
from tradeassist.config import Settings
from tradeassist.db import Database
from tradeassist.opportunities import cost_pct, edge_from, make
from tradeassist.optionmath import NY
from tradeassist.providers.demo import DemoData
from tradeassist.service import TradeAssistant


@pytest.fixture
def svc(tmp_path):
    s = Settings(data_mode="demo", db_path=tmp_path / "b.db", account_size=50_000)
    service = TradeAssistant(s, provider=DemoData(live=True), db=Database(tmp_path / "b.db"))
    yield service
    service.db.close()


def opp(svc, sym, direction="LONG", edge_pct=0.5, cases=400, horizon="day", score=50.0, option=None):
    px = svc.market.spot(sym)
    atr = px * 0.02
    d = 1 if direction == "LONG" else -1
    o = make(source="morning", key=f"m:{sym}:{direction}", symbol=sym, direction=direction, setup="ride", setup_label="Ride",
             horizon=horizon, horizon_days=1 if horizon == "day" else 5, entry=px, stop=px - d * 0.6 * atr,
             target_1=px + d * 0.9 * atr, target_2=px + d * 1.5 * atr, atr=atr, score=score,
             edge=edge_from(edge_pct, cases, basis="test", cost_pct=cost_pct(svc.settings)))
    if option:
        o["option"] = option
    return o


# --- the pieces -----------------------------------------------------------------------------------------------

def test_the_correlation_is_valid_and_fills_gaps_with_the_average():
    rng = np.random.default_rng(1)
    base = rng.standard_normal(300)
    rets = pd.DataFrame({"A": base + 0.3 * rng.standard_normal(300), "B": base + 0.3 * rng.standard_normal(300),
                         "C": rng.standard_normal(300)})
    c, info = B.correlation_of(rets, ["A", "B", "C", "MISSING"])
    assert c.shape == (4, 4) and np.allclose(np.diag(c), 1.0) and np.all(np.linalg.eigvalsh(c) > 0)
    assert c[0, 1] > 0.6 and abs(c[0, 2]) < 0.3                       # shrunk toward the average, still clearly paired
    assert c[3, 0] == pytest.approx(c[3, 1], abs=1e-6)               # the stock with no history gets the average
    bad = np.array([[1, 0.99, -0.99], [0.99, 1, 0.99], [-0.99, 0.99, 1]])
    fixed = B.nearest_correlation(bad)
    assert np.all(np.linalg.eigvalsh(fixed) > 0) and np.allclose(np.diag(fixed), 1.0)


def test_exits_take_the_stop_first_and_hold_the_rest():
    rng = np.random.default_rng(3)
    x = np.log(np.array([[100.0] * 4, [103.0, 97.0, 100.5, 100.0], [100.0, 100.0, 100.0, 100.0]]))
    var = np.array([1e-6, 1e-6])
    st, px, kind = B.walk_exits(x, var, direction=1, stop=98.0, target=102.0, rng=rng)
    assert list(kind) == [1, -1, 0, 0] and px[0] == 102.0 and px[1] == 98.0 and st[2] == 2
    wide = np.log(np.array([[100.0] * 2000, [100.0] * 2000]))
    st, px, kind = B.walk_exits(wide, np.array([0.25]), direction=1, stop=99.0, target=101.0, rng=rng)
    # Both levels are well inside one step's range, so nearly every path touches both: the stop is the one taken.
    assert (kind == -1).mean() > 0.95 and (kind == 1).mean() < 0.01


def test_cvar_is_the_average_of_the_worst_tenth():
    assert B.cvar(np.arange(-10.0, 90.0)) == pytest.approx(5.5)
    assert B.cvar(np.full(100, 3.0)) == 0.0


def test_the_day_grid_starts_at_the_open_or_now_and_ends_at_15_55():
    pre = datetime(2026, 10, 20, 8, 0, tzinfo=NY)
    m, years, note = B.day_grid(pre)
    assert (m[0].hour, m[0].minute) == (9, 30) and (m[-1].hour, m[-1].minute) == (15, 55) and "open" in note
    assert years[0] == 0 and np.all(np.diff(years) > 0) and len(m) == 27
    during = datetime(2026, 10, 20, 14, 2, tzinfo=NY)
    m, _, note = B.day_grid(during)
    assert m[0] == during and "now" in note
    late = datetime(2026, 11, 25, 17, 0, tzinfo=NY)                  # before Thanksgiving: the next session is the 27th
    m, _, note = B.day_grid(late)
    assert m[0].date() == date(2026, 11, 27) and (m[-1].hour, m[-1].minute) == (12, 55)   # an early close
    m, years, _ = B.swing_grid(datetime(2026, 10, 20, 12, 0, tzinfo=NY), 5)
    # To the close 5 trading days after today (the trade card's horizon date), today's afternoon the first step.
    assert len(m) == 7 and m[-1].date() == date(2026, 10, 27) and years[1] < 1 / 252
    assert m[-1] == marketdays.horizon_close(datetime(2026, 10, 20, 12, 0, tzinfo=NY), 5)


# --- the split -------------------------------------------------------------------------------------------------

SYMS = [("AAPL", "LONG", 0.6), ("MSFT", "LONG", 0.3), ("XOM", "SHORT", 0.5), ("JPM", "LONG", -0.2), ("NVDA", "LONG", 0.8),
        ("KO", "SHORT", 0.2), ("CAT", "LONG", 0.4)]


def test_every_cap_and_limit_holds(svc):
    opps = [opp(svc, s, d, e, score=60 - i) for i, (s, d, e) in enumerate(SYMS)]
    b = B.Blender(svc)
    for goal in ("dollars", "balanced", "low_risk"):
        r = b.run(opps, budget=20_000, goal=goal, max_share=0.25, sector_cap=0.4, max_positions=4)
        assert r["spent"] <= 20_000 + 1e-6 and len(r["rows"]) <= 4
        assert len({x["symbol"] for x in r["rows"]}) == len(r["rows"])                  # one instrument per stock
        for x in r["rows"]:
            assert x["dollars"] <= 0.25 * 20_000 + 1e-6
            assert x["risk_at_stop"] <= r["limits"]["per_trade"] + 1e-6
            assert x["symbol"] != "JPM"                                                 # a negative edge is never bought
        sectors = {}
        for x in r["rows"]:
            sectors[x["sector"]] = sectors.get(x["sector"], 0) + x["dollars"]
        assert all(v <= 0.4 * 20_000 + 1e-6 for v in sectors.values())
        if r["combined"] and goal != "dollars":
            assert r["combined"]["cvar10"] <= r["tail_cap"] + 1e-6
        assert r["limits"]["open_used_by_blend"] <= r["limits"]["open_total"] + 1e-6
    lo = b.run(opps, budget=20_000, goal="low_risk")
    hi = b.run(opps, budget=20_000, goal="dollars")
    assert lo["combined"]["cvar10"] <= hi["combined"]["cvar10"] + 1e-6


def test_the_greedy_split_is_close_to_the_best_one(svc):
    """On two trades with a small budget, the greedy search is checked against every whole-share split."""
    opps = [opp(svc, "AAPL", "LONG", 0.6), opp(svc, "KO", "SHORT", 0.4)]
    b = B.Blender(svc)
    budget = 3000.0
    r = b.run(opps, budget=budget, goal="dollars", max_share=1.0, sector_cap=1.0, obey_limits=False)
    got = r["combined"]["expected_pnl"]
    # Value of each unit on the same paths, for the brute force.
    units = {}
    for o in opps:
        inst = "shares" if o["direction"] == "LONG" else "short"
        one = b.run([o], budget=budget, goal="dollars", max_share=1.0, sector_cap=1.0, obey_limits=False,
                    units={f"{o['key']}|{inst}": 1})
        units[o["symbol"]] = (one["rows"][0]["expected_pnl"], o["entry"])
    best = 0.0
    a_max, k_max = int(budget // units["AAPL"][1]), int(budget // units["KO"][1])
    for a, k in itertools.product(range(a_max + 1), range(k_max + 1)):
        if a * units["AAPL"][1] + k * units["KO"][1] <= budget:
            best = max(best, a * units["AAPL"][0] + k * units["KO"][0])
    assert got >= 0.95 * best - 1e-6


def test_no_edge_holds_cash_and_shows_an_equal_risk_split_for_study(svc):
    opps = [opp(svc, s, d, -0.3) for s, d, _ in SYMS[:4]]
    r = B.Blender(svc).run(opps, budget=10_000, goal="dollars")
    assert r["rows"] == [] and r["combined"] is None and r["spent"] == 0
    fb = r["fallback"]
    assert fb["rows"] and all(x["risk_at_stop"] <= r["limits"]["per_trade"] + 1e-6 for x in fb["rows"])
    assert any("for study" in n for n in r["notes"])


def test_units_given_are_valued_as_they_are(svc):
    o = opp(svc, "AAPL", "LONG", 0.6)
    r = B.Blender(svc).run([o], budget=50_000, goal="balanced", units={f"{o['key']}|shares": 7})
    assert r["given"] and r["rows"][0]["units"] == 7 and r["spent"] == pytest.approx(7 * o["entry"])
    assert len(r["fan"]) == r["steps"] + 1 and r["fan"][0]["p50"] == pytest.approx(0, abs=1e-9)


def test_a_contract_is_valued_and_limited_by_its_premium(svc):
    o = opp(svc, "AAPL", "LONG", 0.8)
    exp = sorted(svc.market.expirations("AAPL"))[3]
    o["option"] = {"option_type": "CALL", "strike": round(o["entry"]), "expiration": exp, "premium": 4.0, "bid": 3.9, "ask": 4.1,
                   "iv": 0.3}
    b = B.Blender(svc)
    r = b.run([o], budget=20_000, goal="dollars", instruments=["call"], max_share=1.0)
    assert all(x["instrument"] == "call" for x in r["rows"])
    for x in r["rows"]:
        assert x["unit_cost"] == pytest.approx(400.0) and x["risk_at_stop"] <= r["limits"]["per_trade"] + 1e-6
    tight = Settings(data_mode="demo", db_path=svc.settings.db_path, account_size=10_000)   # $200 a trade < one contract
    svc.settings = tight
    r = B.Blender(svc).run([o], budget=20_000, goal="dollars", instruments=["call"])
    assert r["rows"] == [] and any("risk per trade" in s["why"] for s in r["skipped"])


def test_a_swing_blend_lets_each_trade_leave_at_its_own_horizon(svc):
    swing = opp(svc, "AAPL", "LONG", 0.6, horizon="swing")
    day = opp(svc, "MSFT", "LONG", 0.6, horizon="day")
    r = B.Blender(svc).run([swing, day], budget=20_000, goal="dollars", obey_limits=False)
    assert r["horizon"] == "swing"
    only_day = B.Blender(svc).run([swing, day], budget=20_000, goal="dollars", horizon="day", obey_limits=False)
    assert all(x["symbol"] == "MSFT" for x in only_day["rows"]) and any(s["symbol"] == "AAPL" for s in only_day["skipped"])


def test_bad_requests_say_what_is_wrong(svc):
    b = B.Blender(svc)
    with pytest.raises(B.BlendError, match="budget"):
        b.run([opp(svc, "AAPL")], budget=50)
    with pytest.raises(B.BlendError, match="goal"):
        b.run([opp(svc, "AAPL")], budget=5000, goal="yolo")
    with pytest.raises(B.BlendError, match="No opportunities"):
        b.run([], budget=5000)


# --- from the independent review of 2.15 ----------------------------------------------------------------------

def _far(svc, sym, days, edge_pct, score):
    """A swing trade with levels no path reaches, so it leaves at its horizon."""
    px = svc.market.spot(sym)
    return make(source="ideas", key=f"k:{sym}", symbol=sym, direction="LONG", setup="x", setup_label="x", horizon="swing",
                horizon_days=days, entry=px, stop=px * 0.01, target_1=px * 100, target_2=px * 200, score=score, vol=0.25,
                edge=edge_from(edge_pct, 100_000, basis="t", cost_pct=cost_pct(svc.settings)))


def test_each_trade_expects_its_whole_edge_by_its_own_horizon(svc):
    """A 5-day trade blended with a 20-day one used to get 5/20 of its edge (the drift was spread over the whole
    grid), so mixed blends leaned to the longest trade."""
    now = datetime(2026, 10, 20, 8, 0, tzinfo=NY)
    a, b = _far(svc, "AAPL", 5, 2.0, 60), _far(svc, "MSFT", 20, 2.0, 50)
    units = {f"{a['key']}|shares": 100, f"{b['key']}|shares": 100}
    mixed = B.Blender(svc).run([a, b], budget=1_000_000, goal="dollars", units=units, now=now, obey_limits=False)
    alone = B.Blender(svc).run([a], budget=1_000_000, goal="dollars", units={f"{a['key']}|shares": 100}, now=now, obey_limits=False)
    row = {x["symbol"]: x for x in mixed["rows"]}
    implied = 100 * a["entry"] * a["edge"]["expected_pct"] / 100                     # the edge, net of costs, in dollars
    assert row["AAPL"]["expected_pnl"] == pytest.approx(implied, rel=0.15)
    assert alone["rows"][0]["expected_pnl"] == pytest.approx(implied, rel=0.15)
    # The same horizon alone and in a mix: the card's close, five trading days after today.
    assert alone["end"] == marketdays.horizon_close(now, 5).isoformat(timespec="minutes")
    assert mixed["end"] == marketdays.horizon_close(now, 20).isoformat(timespec="minutes")


def test_a_day_blend_keeps_a_stocks_day_trade_behind_its_swing_trade(svc):
    swing = opp(svc, "AAPL", "LONG", 0.8, horizon="swing", score=80)
    swing["key"] = "ideas:AAPL"
    day = opp(svc, "AAPL", "LONG", 0.6, horizon="day", score=60)
    msft = opp(svc, "MSFT", "LONG", 0.5, horizon="day", score=50)
    r = B.Blender(svc).run([swing, day, msft], budget=50_000, goal="dollars", horizon="day", obey_limits=False,
                           now=datetime(2026, 10, 20, 8, 0, tzinfo=NY))
    assert "AAPL" in {x["symbol"] for x in r["rows"]}
    assert not any("already in the blend" in s["why"] for s in r["skipped"])


def test_units_that_break_a_cap_or_limit_say_so(svc):
    o = opp(svc, "AAPL", "LONG", 0.6)
    r = B.Blender(svc).run([o], budget=10_000, goal="balanced", units={f"{o['key']}|shares": 5_000})
    assert not r["within_limits"]
    text = " ".join(r["breaches"])
    assert "budget" in text and "more than 20% of the budget" in text and "a trade" in text
    assert any(n.startswith("These units break") for n in r["notes"])
    ok = B.Blender(svc).run([o], budget=50_000, goal="dollars", units={f"{o['key']}|shares": 1})
    assert ok["within_limits"] and ok["breaches"] == []


def test_caps_count_what_a_portfolio_already_holds(svc):
    opps = [opp(svc, s, "LONG", 0.6, score=60 - i) for i, s in enumerate(["AAPL", "MSFT", "NVDA"])]   # all XLK
    b = B.Blender(svc)
    free = b.run(opps, budget=10_000, goal="dollars", sector_cap=0.4, obey_limits=False)
    assert sum(x["dollars"] for x in free["rows"]) > 0
    full = b.run(opps, budget=10_000, goal="dollars", sector_cap=0.4, obey_limits=False, cap_base=20_000,
                 held_sectors={"XLK": 8_000})
    assert full["rows"] == []                       # 40% of a $20,000 portfolio is already in technology
    held_risk = b.run(opps, budget=10_000, goal="dollars", held_risk=10_000)
    assert held_risk["rows"] == []                  # the open-risk limit ($5,000 of $50,000) is already used
