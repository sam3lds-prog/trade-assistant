"""Finished versus unfinished daily bars, data-quality checks and provisional signals."""

from datetime import datetime

import numpy as np
import pandas as pd
import pytest
from fastapi.testclient import TestClient

import tradeassist.market as market_mod
from tradeassist.api import create_app
from tradeassist.config import Settings
from tradeassist.dataquality import assess
from tradeassist.db import Database
from tradeassist.market import (CONFIRMED, PROVISIONAL, Market, complete_only, settled_through, signal_status)
from tradeassist.optionmath import NY
from tradeassist.patterns import recent_events
from tradeassist.providers.base import ProviderInfo
from tradeassist.providers.demo import DemoData
from tradeassist.scorecard import trend_snapshot
from tradeassist.service import TradeAssistant


def at(text: str) -> float:
    return datetime.fromisoformat(text).replace(tzinfo=NY).timestamp()


def frame(last: str, n: int = 300, seed: int = 1) -> pd.DataFrame:
    dates = pd.bdate_range(end=last, periods=n)
    rng = np.random.default_rng(seed)
    close = 100 * np.exp(np.cumsum(rng.normal(0, 0.01, n)))
    open_ = close * (1 + rng.normal(0, 0.003, n))
    return pd.DataFrame({"date": dates, "open": open_, "high": np.maximum(open_, close) * 1.01,
                         "low": np.minimum(open_, close) * 0.99, "close": close, "volume": 1e6})


class Bars:
    """A history source that counts its requests and returns bars up to a set date."""
    info = ProviderInfo(mode="live", label="test", realtime=True, has_greeks=False, note="", history_adjustment="split")

    def __init__(self, last: str):
        self.last, self.calls = last, 0

    def history(self, symbol, start, end):
        self.calls += 1
        return frame(self.last)


@pytest.fixture()
def clock(monkeypatch):
    now = {"t": at("2026-10-06T11:00:00")}
    monkeypatch.setattr(market_mod.time, "time", lambda: now["t"])
    return now


def test_a_bar_is_finished_only_if_fetched_after_its_close():
    assert settled_through(at("2026-10-06T11:00:00")).isoformat() == "2026-10-05"
    assert settled_through(at("2026-10-06T16:05:00")).isoformat() == "2026-10-05"   # closing prints still arriving
    assert settled_through(at("2026-10-06T16:30:00")).isoformat() == "2026-10-06"
    assert settled_through(at("2026-10-10T09:00:00")).isoformat() == "2026-10-09"   # a Saturday


def test_todays_bar_is_marked_unfinished_and_refetched_after_the_close(clock):
    src = Bars("2026-10-06")
    m = Market(src, Database(":memory:"), Settings(data_mode="demo"))
    df = m.bars("AAPL")
    assert not df["bar_complete"].iloc[-1] and df["bar_complete"].iloc[:-1].all()
    assert signal_status(df) == PROVISIONAL and signal_status(df, -2) == CONFIRMED
    done = m.bars("AAPL", complete=True)
    assert done["date"].iloc[-1] == pd.Timestamp("2026-10-05") and src.calls == 1
    assert m.bar_info("AAPL")["last_bar_status"] == "PARTIAL" and m.bar_info("AAPL")["adjustment"] == "split"

    clock["t"] += 20 * 60                               # 20 minutes later
    m.bars("AAPL", complete=True)
    assert src.calls == 1                               # finished bars alone do not need the newer partial bar
    m.bars("AAPL")
    assert src.calls == 2                               # the chart and quotes want today's bar kept fresh

    clock["t"] = at("2026-10-06T16:30:00")              # after the close the cached partial bar is refetched
    assert m.bar_info("AAPL")["last_bar_status"] == "PARTIAL"
    df = m.bars("AAPL", complete=True)
    assert src.calls == 3 and df["date"].iloc[-1] == pd.Timestamp("2026-10-06") and df["bar_complete"].all()
    assert m.bar_info("AAPL")["last_bar_status"] == "COMPLETE"
    m.bars("AAPL")
    assert src.calls == 3


def test_complete_only_and_signal_status_leave_unmarked_frames_alone():
    df = frame("2026-10-06", 30)
    assert complete_only(df) is df and signal_status(df) == CONFIRMED
    df["bar_complete"] = True
    df.loc[df.index[-1], "bar_complete"] = False
    assert len(complete_only(df)) == 29 and complete_only(df).index[-1] == 28


def test_readings_and_patterns_from_an_unfinished_bar_are_provisional(clock):
    m = Market(Bars("2026-10-06"), Database(":memory:"), Settings(data_mode="demo"))
    enriched = m.enriched("AAPL")
    assert trend_snapshot(enriched)["status"] == PROVISIONAL
    assert trend_snapshot(m.enriched("AAPL", complete=True))["status"] == CONFIRMED
    # Force a pattern on every bar and check each event is labelled by the bar it came from.
    import tradeassist.patterns as pat
    real = pat.detect_all
    try:
        pat.detect_all = lambda df: pd.DataFrame({"hammer": [True] * len(df)}, index=df.index)
        assert recent_events(enriched)[0]["status"] == PROVISIONAL
        assert recent_events(complete_only(enriched))[0]["status"] == CONFIRMED
    finally:
        pat.detect_all = real


def test_data_quality_finds_broken_bars():
    df = frame("2026-10-05", 300)
    df.loc[10, "high"] = df.loc[10, "low"] * 0.9          # high below the low
    df.loc[200:210, "volume"] = 0
    df.loc[250:, ["open", "high", "low", "close"]] *= 0.5  # an unadjusted 2-for-1 split
    df = df.drop(index=range(100, 106)).reset_index(drop=True)   # six weekdays missing
    df["bar_complete"] = True
    df.loc[df.index[-1], "bar_complete"] = False
    report = assess("TEST", df, {"adjustment": "unknown", "fetched_at": "2026-10-05T11:00:00-04:00"},
                    now=at("2026-10-05T11:00:00"))
    codes = {c["code"]: c for c in report["checks"]}
    assert report["status"] == "warn" and report["last_bar_status"] == "PARTIAL"
    assert {"PARTIAL_BAR", "OHLC_INCONSISTENT", "SUSPECT_SPLIT", "GAPS", "ZERO_VOLUME", "ADJUSTMENT"} <= set(codes)
    assert codes["GAPS"]["count"] == 1 and codes["OHLC_INCONSISTENT"]["count"] == 1
    assert "STALE" not in codes and "SHORT_HISTORY" not in codes
    clean = frame("2026-10-02", 400)
    clean["bar_complete"] = True
    ok = assess("OK", clean, {"adjustment": "none"}, now=at("2026-10-05T11:00:00"))
    assert ok["status"] == "ok" and [c["code"] for c in ok["checks"]] == ["ADJUSTMENT"]
    stale = assess("OLD", clean, {"adjustment": "none"}, now=at("2026-10-14T17:00:00"))
    assert stale["status"] == "warn" and stale["checks"][0]["code"] == "STALE"
    bad = clean.copy()
    bad.loc[5, "close"] = 0
    assert assess("BAD", bad, {}, now=at("2026-10-05T11:00:00"))["status"] == "fail"


def test_older_bar_caches_gain_an_adjustment_column(tmp_path):
    import sqlite3
    from tradeassist.db import SCHEMA
    path = tmp_path / "old.db"
    conn = sqlite3.connect(path)
    conn.executescript(SCHEMA)
    conn.execute("INSERT INTO bars_meta VALUES ('AAPL', 'live', '2023-01-01', '2026-10-05', 0)")
    conn.commit()
    conn.close()
    db = Database(path)
    assert db.one("SELECT adjustment FROM bars_meta WHERE symbol = 'AAPL'")["adjustment"] == "unknown"
    assert db.schema_version() >= 2


def test_data_quality_and_bar_status_through_the_api(tmp_path, clock):
    settings = Settings(data_mode="demo", db_path=tmp_path / "t.db")
    demo = DemoData()
    assistant = TradeAssistant(settings, provider=demo, db=Database(":memory:"))
    c = TestClient(create_app(settings, assistant))
    d = c.get("/v2/data/AAPL").json()
    assert d["adjustment"] == "none" and d["bars"] > 500 and d["status"] in ("ok", "warn")
    t = c.get("/api/technicals/AAPL").json()
    assert t["last_bar"]["status"] in (CONFIRMED, PROVISIONAL)
    if t["last_bar"]["status"] == PROVISIONAL:
        assert t["confirmed_structure"]["state"] in ("up", "down", "range") and t["last_bar"]["last_complete_date"]
    else:
        assert t["confirmed_structure"] is None
    r = c.get("/api/research/AAPL").json()
    assert r["last_bar"]["status"] == t["last_bar"]["status"]
