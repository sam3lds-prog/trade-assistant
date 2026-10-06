import numpy as np
import pandas as pd
import pytest

from tests.conftest import make_bars, random_walk
from tradeassist.indicators import enrich
from tradeassist.patterns import PATTERNS
from tradeassist.structure import analyse, find_levels, find_swings, find_trendlines, structure_series, Swing


def path(*legs, start=101.0):
    """Closes moving one dollar a day: each leg is a signed number of days. Ten days of +1/-1
    chop first, so the average true range settles at exactly one dollar."""
    closes = [100.0, 101.0] * 10
    price = start
    for leg in legs:
        for _ in range(abs(leg)):
            price += 1 if leg > 0 else -1
            closes.append(price)
    return enrich(make_bars(closes))


def test_swings_are_labelled_and_dated_on_the_day_they_become_known():
    df = path(+20, -6, +10, -6, +10, -19)
    assert df["atr"].iloc[-1] == pytest.approx(1.0)
    swings = find_swings(df)
    got = [(s.kind, s.price, s.index, s.confirmed, s.label) for s in swings]
    assert got == [
        ("low", 100.0, 14, 20, ""),        # where the first rise started, known once it was two dollars up
        ("high", 121.0, 39, 41, ""),       # the top of the first rise, known two dollars lower
        ("low", 115.0, 45, 47, "HL"),
        ("high", 125.0, 55, 57, "HH"),
        ("low", 119.0, 61, 63, "HL"),
        ("high", 129.0, 71, 73, "HH"),
    ]
    s = structure_series(df, swings)
    up_from = int(np.flatnonzero(s["struct_up_start"])[0])
    assert up_from == 57                                       # the day the higher high became known
    assert (s["struct_state"].iloc[:57] == 0).all() and (s["struct_state"].iloc[57:82] == 1).all()
    assert int(np.flatnonzero(s["struct_up_break"])[0]) == 82  # first close below the 119 higher low
    assert df["close"].iloc[82] == 118 and (s["struct_state"].iloc[82:] == 0).all()


def test_a_downtrend_and_its_break():
    df = path(-20, +6, -10, +6, -10, +19, start=101.0)
    labels = [s.label for s in find_swings(df)]
    assert labels == ["", "", "LH", "LL", "LH", "LL"]
    s = structure_series(df)
    assert s["struct_down_start"].sum() == 1 and s["struct_down_break"].sum() == 1
    broke = int(np.flatnonzero(s["struct_down_break"])[0])
    lower_high = [x for x in find_swings(df) if x.label == "LH"][-1]
    assert df["close"].iloc[broke] > lower_high.price >= df["close"].iloc[broke - 1]


def test_equal_highs_count_as_neither_higher_nor_lower():
    df = path(+20, -6, +6, -6, start=101.0)                      # the second top matches the first
    swings = find_swings(df)
    assert [s.label for s in swings if s.kind == "high"] == ["", "EH"]
    assert (structure_series(df, swings)["struct_state"] == 0).all()


def test_nothing_is_known_before_it_happens():
    """Cutting the history at any day must not change anything computed for the days before it."""
    full = enrich(random_walk(n=700, seed=7))
    whole = structure_series(full)
    swings = find_swings(full)
    for cut in (150, 333, 512, 699):
        part = full.iloc[:cut].reset_index(drop=True)
        early = structure_series(part)
        pd.testing.assert_frame_equal(early.reset_index(drop=True), whole.iloc[:cut].reset_index(drop=True))
        assert find_swings(part) == [s for s in swings if s.confirmed < cut]


def test_enrich_and_the_pattern_lab_use_the_same_structure():
    df = enrich(random_walk(n=600, seed=3))
    s = structure_series(df)
    for col in s.columns:
        assert (df[col].to_numpy() == s[col].to_numpy()).all()
    for key, col in [("structure_up", "struct_up_start"), ("structure_down", "struct_down_start"),
                     ("structure_up_break", "struct_up_break"), ("structure_down_break", "struct_down_break")]:
        assert key in PATTERNS and (PATTERNS[key].detect(df).to_numpy() == df[col].to_numpy()).all()
    assert PATTERNS["structure_up"].direction == 1 and PATTERNS["structure_up_break"].direction == -1


def test_support_and_resistance_group_nearby_swings():
    sw = [Swing("low", 10, 95.0, 12, ""), Swing("low", 40, 95.3, 42, "HL"), Swing("high", 25, 104.8, 27, ""),
          Swing("high", 60, 105.1, 62, "HH"), Swing("high", 80, 105.2, 82, "EH"), Swing("low", 70, 90.0, 72, "LL")]
    lv = find_levels(sw, close=100.0, atr=1.0, as_of=100)
    assert [(round(x["price"], 2), x["touches"]) for x in lv["resistance"]] == [(105.03, 3)]
    assert [(round(x["price"], 2), x["touches"]) for x in lv["support"]] == [(95.15, 2), (90.0, 1)]
    assert lv["resistance"][0]["strength"] == "strong" and lv["support"][1]["strength"] == "single swing"
    # Only swings already known count, and only within the lookback.
    assert find_levels(sw, 100.0, 1.0, as_of=50)["resistance"][0]["touches"] == 1
    assert find_levels(sw, 100.0, 1.0, as_of=400, lookback=252) == {"support": [], "resistance": []}
    # A long run of swings a little apart does not chain into one wide level.
    run = [Swing("low", i, 90.0 + 0.4 * i, i + 1, "") for i in range(10)]
    widths = [x["touches"] for x in find_levels(run, 100.0, 1.0, as_of=20, per_side=10)["support"]]
    assert max(widths) <= 3 and sum(widths) == 10


def test_trendlines_run_through_the_last_two_rising_lows_or_falling_highs():
    close = np.full(120, 100.0)
    sw = [Swing("low", 20, 90.0, 22, ""), Swing("high", 40, 110.0, 42, ""), Swing("low", 60, 94.0, 62, "HL"),
          Swing("high", 80, 108.0, 82, "LH")]
    lines = {t["kind"]: t for t in find_trendlines(sw, close, as_of=119)}
    support, resistance = lines["rising support"], lines["falling resistance"]
    assert support["slope_per_day"] == pytest.approx(0.1) and support["value_now"] == pytest.approx(94 + 0.1 * 59)
    assert not support["crossed"]                                           # 100 is above 99.9
    assert resistance["value_now"] == pytest.approx(108 - 0.05 * 39) and not resistance["crossed"]
    above = {t["kind"]: t for t in find_trendlines(sw, np.full(120, 107.0), as_of=119)}
    assert above["falling resistance"]["crossed"] and not above["rising support"]["crossed"]
    falling_lows = [Swing("low", 20, 94.0, 22, ""), Swing("low", 60, 90.0, 62, "LL")]
    assert find_trendlines(falling_lows, close, 119) == []                  # no rising support to draw


def test_the_reading_says_what_the_structure_is_and_what_would_break_it():
    up = analyse(path(+20, -6, +10, -6, +10, -3))
    assert up["state"] == "up" and up["headline"].startswith("Uptrend")
    assert "$119.00" in up["watch"] and "higher than the one before" in up["detail"]
    assert {t["kind"] for t in up["trendlines"]} == {"rising support"}
    broken = analyse(path(+20, -6, +10, -6, +10, -19))
    assert broken["state"] == "range" and broken["broke"] == "up" and "closed through" in broken["watch"]
    assert len(broken["state_series"]) == len(path(+20, -6, +10, -6, +10, -19))
    assert all({"date", "confirmed_date", "label"} <= set(s) for s in broken["swings"])
    down = analyse(path(-20, +6, -10, +6, -10, +3))
    assert down["state"] == "down" and "lower high" in down["watch"]


def test_a_break_is_reported_even_on_the_day_a_swing_is_confirmed():
    """Whatever bar a break lands on, the reading as of that bar says the structure just broke."""
    checked = 0
    for seed in range(12):
        df = enrich(random_walk(n=500, seed=seed, vol=0.02))
        breaks = np.flatnonzero((df["struct_up_break"] | df["struct_down_break"]).to_numpy())
        for b in breaks:
            got = analyse(df.iloc[:b + 1].reset_index(drop=True))
            assert got["broke"] in ("up", "down") and got["state"] == "range", (seed, b)
            checked += 1
    assert checked > 20


def test_a_missing_high_or_low_does_not_stop_swing_detection():
    raw = random_walk(n=700, seed=5)
    clean_swings = len(find_swings(enrich(raw)))
    holed = raw.copy()
    holed.loc[57, "high"] = np.nan
    holed.loc[300, "low"] = np.nan
    swings = find_swings(enrich(holed))
    assert len(swings) >= clean_swings - 4 and swings[-1].confirmed > 600
