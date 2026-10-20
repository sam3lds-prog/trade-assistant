"""2.15: opportunities: the edge and its pull toward zero, the records behind it, day-trade levels, the saved
screen settings and the stored runs.

The rules pinned here: an edge is a past average net of costs, weighted by cases / (cases + 100); with no record
there is no edge beyond the cost; a record counts only what was knowable then (overextension on the day before;
today left out of the rest-of-the-day record); levels always sit the trade's way."""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

import numpy as np
import pandas as pd
import pytest

from tradeassist import opportunities as op
from tradeassist.config import Settings
from tradeassist.db import Database
from tradeassist.morning import features_of


def test_the_pull_toward_zero_and_the_cost():
    assert op.shrink_weight(100) == pytest.approx(0.5) and op.shrink_weight(0) == 0
    assert op.shrink_weight(900) == pytest.approx(0.9)
    e = op.edge_from(1.0, 100, basis="b", cost_pct=0.1)
    assert e["known"] and e["expected_pct"] == pytest.approx(0.4) and e["raw_pct"] == 1.0
    none = op.edge_from(None, 0, basis="nothing", cost_pct=0.1)
    assert not none["known"] and none["expected_pct"] == pytest.approx(-0.1) and "no past cases" in none["basis"]
    assert op.edge_from(0.5, 1000, basis="b", cost_pct=0.0, scale=2.0)["expected_pct"] == pytest.approx(1000 / 1100)
    assert op.cost_pct(Settings(stock_cost_bps=5)) == pytest.approx(0.1)


def _frame(opens, closes, atr_level=2.0):
    n = len(closes)
    c = np.asarray(closes, dtype=float)
    o = np.asarray(opens, dtype=float)
    df = pd.DataFrame({"date": pd.bdate_range("2023-01-02", periods=n), "open": o, "close": c,
                       "high": np.maximum(o, c) + atr_level / 2, "low": np.minimum(o, c) - atr_level / 2,
                       "volume": np.full(n, 1e6)})
    return features_of(df)


def test_the_gap_record_counts_open_to_close_after_gaps_of_a_size():
    closes = [100.0] * 60
    opens = [100.0] * 60
    # Three gap-up days of about 3 ranges; two of them fade to the close, one keeps going.
    for i, close in ((30, 103.0), (40, 99.0), (50, 98.0)):
        opens[i] = 106.0
        closes[i] = close
        closes[i + 1:i + 5] = [100.0] * 4
    f = _frame(opens, closes)
    rec = op.gap_record({"X": f}, gap_direction=1, min_ranges=1.5, trade_direction=-1)
    assert rec["cases"] == 3
    assert rec["frequency"] == pytest.approx(1.0)          # all three closed below their open: the fade's way
    longs = op.gap_record({"X": f}, gap_direction=1, min_ranges=1.5, trade_direction=1)
    assert longs["frequency"] == 0 and longs["mean_move_pct"] == pytest.approx(-rec["mean_move_pct"])
    assert op.gap_record({"X": f}, gap_direction=-1, min_ranges=1.5, trade_direction=1)["cases"] == 0


def _bars(day: date, path: list[float], start=100.0):
    """5-minute bars for one session: `path` is the close of each bar (78 for a full day)."""
    t0 = int(datetime(day.year, day.month, day.day, 13, 30, tzinfo=timezone.utc).timestamp())   # 9:30 New York (EDT)
    rows, prev = [], start
    for k, c in enumerate(path):
        rows.append({"t": t0 + 300 * k, "open": prev, "high": max(prev, c), "low": min(prev, c), "close": c, "volume": 1000.0,
                     "vwap": None})
        prev = c
    return rows


def test_the_rest_of_the_day_record_leaves_today_out_and_filters_by_size():
    days = [date(2026, 10, 12), date(2026, 10, 13), date(2026, 10, 14)]
    up_then_fade = [100 + min(k, 6) for k in range(12)] + [106 - 0.1 * k for k in range(66)]       # +3 ranges by 30 min, then down
    up_then_more = [100 + min(k, 6) for k in range(12)] + [106 + 0.05 * k for k in range(66)]
    rows = _bars(days[0], up_then_fade) + _bars(days[1], up_then_more) + _bars(days[2], up_then_fade)
    df = pd.DataFrame(rows)
    atr = {"X": {d: 2.0 for d in days}}
    cases = op.intraday_cases({"X": df}, atr, minute=30, skip_day=days[2])
    assert len(cases["move"]) == 2 and cases["stocks"] == 1         # today (the third day) is left out
    rec = op.intraday_record_from(cases, min_ranges=2.0, move_direction=1, trade_direction=-1)
    assert rec["cases"] == 2 and rec["frequency"] == pytest.approx(0.5)
    assert op.intraday_record_from(cases, min_ranges=5.0, move_direction=1, trade_direction=-1)["cases"] == 0


def test_day_levels_sit_the_trade_s_way():
    stop, t1, t2, why = op.day_levels(entry=100.0, atr=2.0, direction=1, setup="ride")
    assert stop == pytest.approx(98.8) and t1 == pytest.approx(101.8) and t2 == pytest.approx(103.0) and "15:55" in why
    stop, t1, t2, why = op.day_levels(entry=106.0, atr=2.0, direction=-1, setup="fade", reference=100.0)
    assert stop == pytest.approx(107.2) and t1 == pytest.approx(103.0) and t2 == pytest.approx(100.0) and "gap" in why


def test_an_opportunity_checks_its_levels():
    with pytest.raises(ValueError, match="stop must sit behind"):
        op.make(source="morning", key="k", symbol="aapl", direction="LONG", setup="ride", setup_label="x", horizon="day",
                horizon_days=1, entry=100, stop=101, target_1=103)
    o = op.make(source="morning", key="k", symbol="aapl", direction="SHORT", setup="fade", setup_label="x", horizon="day",
                horizon_days=1, entry=100, stop=101, target_1=98, target_2=99)
    assert o["symbol"] == "AAPL" and o["target_2"] < o["target_1"] and o["sector"] == "XLK" and o["option"] is None
    card = {"option": {"option_type": "PUT", "strike": 95, "expiration": "2026-11-20", "iv": 0.3, "bid": 1.0, "ask": 1.2,
                       "limits": {"viable": True, "suggested": 1.1, "value_at_target": 2.0, "value_at_stop": 0.6}}}
    assert op.option_of(card)["premium"] == 1.1
    card["option"]["limits"]["viable"] = False
    assert op.option_of(card) is None


def test_saved_settings_are_checked(tmp_path):
    db = Database(tmp_path / "p.db")
    s = Settings(data_mode="demo", db_path=tmp_path / "p.db", account_size=25_000)
    p = op.load_prefs(db, s)
    assert p["blend"]["budget"] == 25_000 and p["morning"]["horizon"] == "day" and p["live"]["digest"] == "15"
    p = op.save_prefs(db, s, {"morning": {"auto_refresh": True, "refresh_minutes": 10, "am_end": "11:00"},
                              "blend": {"goal": "low_risk", "instruments": ["shares", "call"]}})
    assert p["morning"]["auto_refresh"] and p["morning"]["am_end"] == "11:00" and p["blend"]["instruments"] == ["shares", "call"]
    assert op.load_prefs(db, s)["blend"]["goal"] == "low_risk"
    for bad in ({"morning": {"refresh_minutes": 7}}, {"morning": {"am_end": "8:00"}}, {"live": {"digest": "5"}},
                {"blend": {"goal": "yolo"}}, {"blend": {"instruments": []}}, {"blend": {"nope": 1}}, {"other": {}}):
        with pytest.raises(ValueError):
            op.save_prefs(db, s, bad)
    db.close()


def test_runs_are_kept_listed_and_pruned(tmp_path):
    db = Database(tmp_path / "r.db")
    runs = op.Runs(db)
    o = op.make(source="morning", key="morning:x:AAPL", symbol="AAPL", direction="LONG", setup="ride", setup_label="x",
                horizon="day", horizon_days=1, entry=100, stop=99, target_1=102, card={"order_text": "Buy 1", "shares": {"count": 1}})
    rid = runs.save("morning", [o], {"label": "test"})
    got = runs.get(rid)
    assert got["opportunities"][0]["symbol"] == "AAPL" and "card" not in got["opportunities"][0]
    assert got["opportunities"][0]["card_summary"]["order_text"] == "Buy 1"
    assert runs.latest("morning")["id"] == rid and runs.listing()[0]["label"] == "test"
    assert runs.save("morning", []) is None and runs.save("nowhere", [o]) is None
    old = (datetime.now(timezone.utc) - timedelta(days=200)).isoformat()
    with db.tx() as cur:
        cur.execute("UPDATE opportunity_runs SET created_at = ? WHERE id = ?", (old, rid))
    runs.save("live", [o])
    assert runs.get(rid) is None                                    # pruned past KEEP_DAYS
    db.close()


def test_an_idea_s_edge_is_the_signal_history_s_record_against_spy():
    row = {"symbol": "MSFT", "direction": "SHORT", "grade": "B", "score": 70, "entry": 400.0, "stop": 410.0,
           "target_1": 380.0, "target_2": 370.0, "atr": 6.0, "reward_risk_1": 2.0, "caps": [],
           "trend": {"label": "Downtrend"}, "past_10d": {"cases": 400, "independent_periods": 100,
                                                         "mean_excess_vs_spy": -0.01, "frequency": 0.4}}
    o = op.from_idea(row, settings=Settings(stock_cost_bps=5), run_day="2026-10-20")
    # -1% against SPY over 10 days, the short's way +1%, doubled for 20 days, half weight, less 0.1% costs.
    assert o["edge"]["expected_pct"] == pytest.approx(2.0 * 0.5 - 0.1) and o["horizon"] == "swing" and o["horizon_days"] == 20
    assert o["key"] == "ideas:2026-10-20:MSFT:SHORT"
    row.pop("past_10d")
    assert not op.from_idea(row, settings=Settings(), run_day="2026-10-20")["edge"]["known"]
