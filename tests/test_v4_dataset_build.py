"""4.0.1: building the dataset from a source that is slow, flaky or short (Robinhood).

The market fund is fetched first and tried three times, so a failure stops the build at once with its reason; any
other symbol is tried twice and reported with its reason; a history that starts years late is reported, and runs
start after its first year by themselves; Robinhood is asked for its whole history before its five-year span."""

from __future__ import annotations

from datetime import date
from types import SimpleNamespace

import pandas as pd
import pytest

from tests import v4_fixtures as fx
from tradeassist.intelligence.config import DEVELOPMENT_FROM, ReplayConfig
from tradeassist.intelligence.data import dataset as DS
from tradeassist.intelligence.data.universe import MARKET
from tradeassist.intelligence.replay.runner import create_run
from tradeassist.intelligence.store import ReplayStore


def _source(bars, fail=None):
    """A fetch over fixture bars; `fail[sym]` failures before it answers (an int), or always (True)."""
    calls: list[str] = []
    left = dict(fail or {})

    def fetch(sym, start, end):
        calls.append(sym)
        n = left.get(sym, 0)
        if n is True or n > 0:
            if n is not True:
                left[sym] = n - 1
            raise RuntimeError(f"{sym}: 400 Bad Request (session expired)")
        df = bars[sym]
        return df[(df["date"] >= pd.Timestamp(start)) & (df["date"] <= pd.Timestamp(end))]
    return fetch, calls


@pytest.fixture()
def bars():
    return fx.calendar_market(first="2021-11-01", last="2025-06-30")


def _build(store, fetch, first=date(2021, 11, 1)):
    return DS.build(store, fetch, fx.universe(), first, date(2025, 6, 30), source="robinhood", adjustment="unknown",
                    pause=lambda s: None)


def test_the_market_fund_comes_first_and_a_failure_stops_the_build_with_its_reason(bars):
    store = ReplayStore(":memory:")
    fetch, calls = _source(bars, {MARKET: True})
    with pytest.raises(DS.DatasetError, match=r"Could not get SPY's price history from robinhood: SPY: 400 Bad Request"):
        _build(store, fetch)
    assert calls == [MARKET] * 3                                   # three tries, and nothing else fetched
    assert store.one("SELECT COUNT(*) FROM datasets")[0] == 0


def test_a_passing_failure_is_tried_again_and_a_lasting_one_is_reported_with_why(bars):
    store = ReplayStore(":memory:")
    stocks = [s for s in fx.universe().symbols if s != MARKET]
    fetch, calls = _source(bars, {MARKET: 2, stocks[0]: 1, stocks[1]: True})
    ds = _build(store, fetch)
    assert calls[:3] == [MARKET] * 3 and MARKET in ds.bars and stocks[0] in ds.bars and stocks[1] not in ds.bars
    failed = [c for c in ds.checks if c["code"] == "FETCH_FAILED"]
    assert [c["symbol"] for c in failed] == [stocks[1]] and "400 Bad Request" in failed[0]["text"]


def test_a_history_that_starts_years_late_is_reported_and_runs_start_after_its_first_year(bars):
    store = ReplayStore(":memory:")
    fetch, _ = _source(bars)
    ds = _build(store, fetch, first=date(2015, 10, 1))             # ten years asked for, about four supplied
    short = [c for c in ds.checks if c["code"] == "SHORT_HISTORY"]
    assert short and short[0]["level"] == "warn" and "History starts 2021-11-01" in short[0]["text"]
    start = DS.development_start(store, ds.id)
    year_in = ds.calendar.sessions[260]                            # a year of bars (MIN_HISTORY), then the next month's first
    assert start == date(year_in.year + (year_in.month == 12), year_in.month % 12 + 1, 1) and start.year == 2022
    run = create_run(store, ds.id, fx.config(start, date(2025, 6, 2)))
    assert store.one("SELECT first_session FROM replay_runs WHERE id = ?", (run,))[0] >= start.isoformat()
    with pytest.raises(Exception, match="starts before the dataset"):
        create_run(store, ds.id, fx.config(date(2021, 6, 1), date(2025, 6, 2)))


def test_a_full_history_keeps_the_planned_start():
    store = ReplayStore(":memory:")
    full = fx.calendar_market(first="2015-10-01", last="2017-06-30")
    fetch, _ = _source(full)
    ds = DS.build(store, fetch, fx.universe(), date(2015, 10, 1), date(2017, 6, 30), source="sandbox", adjustment="split",
                  pause=lambda s: None)
    assert not [c for c in ds.checks if c["code"] == "SHORT_HISTORY"]
    assert DS.development_start(store, ds.id) == DEVELOPMENT_FROM == ReplayConfig().start


def test_robinhood_is_asked_for_its_whole_history_first_for_a_ten_year_build():
    from tradeassist.providers.robinhood import RobinhoodData
    asked: list[str] = []
    tool = SimpleNamespace(name="get_equity_historicals", annotations=None, input_schema={"properties": {
        "symbols": {"type": "array"}, "interval": {"enum": ["day", "week"]}, "span": {"enum": ["year", "5year", "all"]}}})
    days = pd.bdate_range("2015-10-01", "2025-06-30")

    class Fake(RobinhoodData):
        def _tool(self, job):
            return tool

        def _call(self, tool, args):
            asked.append(args["span"])
            step = 5 if args["span"] == "all" else 1                 # "all" answers with weekly bars here
            picked = days[::step] if args["span"] == "all" else days[-1260:]
            return {"results": [{"symbol": "SPY", "historicals": [
                {"begins_at": f"{d:%Y-%m-%d}T00:00:00Z", "open_price": "1", "high_price": "1", "low_price": "1",
                 "close_price": "1", "volume": 1} for d in picked]}]}
    rh = Fake(bridge=None)
    out = rh._history("SPY", "2015-10-01", "2025-06-30")
    assert asked == ["all", "5year"] and len(out) == 1260         # weekly bars refused, then the five-year span
    asked.clear()
    rh._history("SPY", "2022-06-30", "2025-06-30")                # three years, as Research asks: unchanged
    assert asked == ["5year"]
