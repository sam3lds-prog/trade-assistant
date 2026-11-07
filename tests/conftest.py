import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd
import pytest
import time_machine

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import tradeassist.api as _api  # noqa: E402

# The test client calls itself "testserver"; only the tests let the app answer to that name.
_api.TEST_HOSTS = ("testserver",)

NY = ZoneInfo("America/New_York")
# Every test starts at the same moment, so a result never depends on when the suite runs. Before 3.0 the
# suite read the real clock: the same commit failed two tests at 7 p.m. New York and six at 2 a.m., and the
# demo market (which is generated around today's date) priced the same option differently each day.
# A Thursday after the close, when every session in the chart's range is complete and the demo market has stocks
# that moved more than a daily range (the movers tests need some). The clock still ticks
# from there, so timeouts, threads and the browser tests behave normally.
TEST_NOW = datetime(2026, 10, 22, 17, 0, tzinfo=NY)


def pytest_configure(config):
    config.addinivalue_line("markers", "clock(when): run the test with the clock at this New York time "
                                       "('2026-10-22T10:15') instead of where the suite's clock has got to")
    # Started before collection, so constants a test module sets at import (NOW = datetime.now(...)) are on it.
    config._ta_clock = time_machine.travel(TEST_NOW, tick=True)
    config._ta_clock.start()


def pytest_unconfigure(config):
    clock = getattr(config, "_ta_clock", None)
    if clock is not None:
        clock.stop()


def ny_time(when) -> datetime:
    when = datetime.fromisoformat(when) if isinstance(when, str) else when
    return when if when.tzinfo else when.replace(tzinfo=NY)


@pytest.fixture(autouse=True)
def _test_clock(request):
    """A test with a clock mark runs from that moment; the rest run on the suite's clock, which started at
    TEST_NOW and ticks on (a full run ends about a quarter of an hour later, still after the same close)."""
    mark = request.node.get_closest_marker("clock")
    if mark is None:
        yield
        return
    with time_machine.travel(ny_time(mark.args[0]), tick=True):
        yield


def make_bars(closes, start="2024-01-02", opens=None, highs=None, lows=None, volume=1_000_000):
    """Daily bars from a list of closes. Unless given, open = previous close and the
    high/low hug the open and close."""
    closes = np.asarray(closes, dtype=float)
    opens = np.asarray(opens, dtype=float) if opens is not None else np.concatenate([[closes[0]], closes[:-1]])
    highs = np.asarray(highs, dtype=float) if highs is not None else np.maximum(opens, closes)
    lows = np.asarray(lows, dtype=float) if lows is not None else np.minimum(opens, closes)
    dates = pd.bdate_range(start, periods=len(closes))
    return pd.DataFrame({"date": dates, "open": opens, "high": highs, "low": lows, "close": closes,
                         "volume": float(volume)})


def random_walk(n=900, seed=1, start=100.0, vol=0.015, drift=0.0003):
    rng = np.random.default_rng(seed)
    rets = drift + vol * rng.standard_normal(n)
    closes = start * np.exp(np.cumsum(rets))
    opens = np.concatenate([[start], closes[:-1]]) * np.exp(0.003 * rng.standard_normal(n))
    highs = np.maximum(opens, closes) * (1 + np.abs(0.006 * rng.standard_normal(n)))
    lows = np.minimum(opens, closes) * (1 - np.abs(0.006 * rng.standard_normal(n)))
    df = make_bars(closes, start="2021-01-04", opens=opens, highs=highs, lows=lows)
    df["volume"] = 1e6 * np.exp(0.3 * rng.standard_normal(n))
    return df


@pytest.fixture
def walk():
    return random_walk()


@pytest.fixture(autouse=True)
def _close_databases(monkeypatch):
    """Close every database a test opened, so no test leaves a connection for the garbage
    collector (the app itself keeps one connection open for its whole life)."""
    from tradeassist import db as dbmod
    opened = []
    real_init = dbmod.Database.__init__

    def tracking_init(self, *args, **kwargs):
        real_init(self, *args, **kwargs)
        opened.append(self)
    monkeypatch.setattr(dbmod.Database, "__init__", tracking_init)
    yield
    for d in opened:
        try:
            d.close()
        except Exception:  # noqa: BLE001 - already closed
            pass
