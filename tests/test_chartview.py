"""The interactive chart (2.12): bars within the day and grouped bars, the pattern search, the chart, similar
and outlook calls, the API, and the page's pure helpers under Node."""

from __future__ import annotations

import json
import shutil
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pandas as pd
import pytest
from fastapi.testclient import TestClient

from tradeassist import intraday as I
from tradeassist import similar as S
from tradeassist.analysis import Analyst
from tradeassist.api import create_app
from tradeassist.chartview import ChartView, RequestError
from tradeassist.config import Settings
from tradeassist.db import Database
from tradeassist.news import NewsClient
from tradeassist.optionmath import NY
from tradeassist.providers.base import DataError
from tradeassist.providers.demo import DemoData
from tradeassist.service import TradeAssistant

STATIC = Path(__file__).resolve().parents[1] / "tradeassist" / "static"


def service(tmp_path, **kw):
    s = Settings(data_mode="demo", db_path=tmp_path / "t.db", **kw)
    svc = TradeAssistant(s, provider=DemoData(), db=Database(tmp_path / "t.db"))
    svc.news = NewsClient(s, svc.db, None)
    return svc


# --- bars within the day -----------------------------------------------------------------------------------------

def test_demo_bars_within_the_day_follow_each_days_open_high_low_and_close(tmp_path):
    svc = service(tmp_path)
    daily = svc.market.bars("AAPL", 0.3)
    bars = I.demo_intraday(daily, "AAPL", days=5)
    assert len(bars) == 5 * 78
    local = pd.to_datetime(bars["t"], unit="s", utc=True).dt.tz_convert(NY)
    assert local.dt.strftime("%H:%M").iloc[0] == "09:30" and local.dt.strftime("%H:%M").iloc[77] == "15:55"
    for d, o, h, lo, c in daily.tail(5)[["date", "open", "high", "low", "close"]].itertuples(index=False, name=None):
        day = bars[local.dt.date == pd.Timestamp(d).date()]
        assert len(day) == 78
        assert day["open"].iloc[0] == round(o, 2) and day["close"].iloc[-1] == round(c, 2)
        assert day["high"].max() <= h + 0.011 and day["low"].min() >= lo - 0.011
        assert abs(day["high"].max() - h) < 0.02 and abs(day["low"].min() - lo) < 0.02   # the day's extremes are touched
    again = I.demo_intraday(daily, "AAPL", days=5)
    assert again.equals(bars)                                            # the same every time


def test_bars_are_grouped_on_the_sessions_grid_and_never_across_a_day():
    rows = []
    for day in ("2026-10-15", "2026-10-16"):
        start = datetime.fromisoformat(f"{day}T09:30:00").replace(tzinfo=NY)
        for i in range(78):
            t = int((start + timedelta(minutes=5 * i)).timestamp())
            rows.append({"t": t, "open": 100 + i, "high": 101 + i, "low": 99 + i, "close": 100.5 + i, "volume": 10,
                         "vwap": 100.2 + i})
    rows.append({"t": int(datetime.fromisoformat("2026-10-16T08:00:00").replace(tzinfo=NY).timestamp()), "open": 1, "high": 1,
                 "low": 1, "close": 1, "volume": 1, "vwap": 1})                   # pre-market: dropped
    df = I.regular_session(I.intraday_frame(rows))
    assert len(df) == 156
    hourly = I.group_intraday(df, 3600)
    assert len(hourly) == 14                                              # 9:30, 10:30, ..., 15:30 (a half hour)
    first = hourly.iloc[0]
    assert first["open"] == 100 and first["close"] == 100.5 + 11 and first["high"] == 101 + 11 and first["low"] == 99
    assert first["volume"] == 120 and abs(first["vwap"] - (100.2 + 5.5)) < 1e-9
    local = pd.to_datetime(hourly["t"], unit="s", utc=True).dt.tz_convert(NY).dt.strftime("%H:%M").tolist()
    assert local[:3] == ["09:30", "10:30", "11:30"] and local[6] == "15:30" and local[7] == "09:30"
    q = I.group_intraday(df, 900)
    assert len(q) == 52 and q["volume"].iloc[0] == 30


def test_daily_bars_group_by_week_month_and_quarter_dated_on_their_first_day():
    dates = pd.bdate_range("2026-07-01", "2026-10-16")
    df = pd.DataFrame({"date": dates, "open": 1.0, "high": 2.0, "low": 0.5, "close": np.arange(len(dates), dtype=float),
                       "volume": 1.0, "bar_complete": True})
    weekly = I.group_daily(df, "1w")
    assert weekly["date"].iloc[0] == pd.Timestamp("2026-07-01") and weekly["date"].iloc[1] == pd.Timestamp("2026-07-06")
    assert weekly["close"].iloc[0] == 2 and weekly["volume"].iloc[1] == 5 and bool(weekly["complete"].iloc[0])
    monthly = I.group_daily(df, "1M")
    assert monthly["date"].tolist()[:2] == [pd.Timestamp("2026-07-01"), pd.Timestamp("2026-08-03")]
    assert not bool(monthly["complete"].iloc[-1])                        # October is not over
    quarterly = I.group_daily(df, "1Q")
    assert len(quarterly) == 2 and bool(quarterly["complete"].iloc[0]) and not bool(quarterly["complete"].iloc[1])


def test_bars_within_the_day_are_kept_and_refreshed_sparingly(tmp_path, monkeypatch):
    svc = service(tmp_path)
    it = I.Intraday(svc.market, svc.db)
    calls = []
    real = it._fetch
    monkeypatch.setattr(it, "_fetch", lambda *a: calls.append(a) or real(*a))
    a = it.bars("AAPL", "5m", 5)
    b = it.bars("AAPL", "5m", 5)
    assert len(calls) == 1 and a.equals(b) and len(a) % 78 == 0 and 3 * 78 <= len(a) <= 5 * 78
    stored = svc.db.one("SELECT COUNT(*) AS n FROM intraday_bars WHERE symbol = 'AAPL' AND interval = '5m'")["n"]
    assert stored >= len(a) and stored % 78 == 0                         # a day's slack is fetched beyond the ask
    monkeypatch.setattr(I, "FRESH_SECONDS", 0)
    monkeypatch.setattr(I, "QUIET_SECONDS", 0)
    it.bars("AAPL", "5m", 5)
    assert len(calls) == 2                                               # refreshed once stale

    def fail(*a):
        raise DataError("down")
    monkeypatch.setattr(it, "_fetch", fail)
    c = it.bars("AAPL", "5m", 5)
    assert len(c) == len(a) and "down" in it.last_error["AAPL:5m"]          # what is held stands, and the failure is kept
    assert it.available_groups(1) == ["5m", "15m", "1h", "1d"] and it.available_groups(400) == ["1d", "1w", "1M", "1Q"]


class FakeLive:
    """A live-like source: 5-minute bars for the last `reach` calendar days, weekdays only, 9:30 to 16:00."""

    def __init__(self, reach=39):
        self.reach, self.calls = reach, []

    def intraday_reach(self):
        return {"1m": 0, "5m": 40, "note": "about 40 days"}

    def intraday(self, symbol, interval, start, end):
        self.calls.append((start, end))
        rows, day = [], datetime.now(NY).replace(hour=0, minute=0, second=0, microsecond=0)
        for back in range(self.reach):
            d = day - timedelta(days=back)
            if d.weekday() >= 5:
                continue
            for i in range(78):
                t = int((d.replace(hour=9, minute=30) + timedelta(minutes=5 * i)).timestamp())
                if start.timestamp() <= t <= end.timestamp():
                    rows.append({"t": t, "open": 10, "high": 11, "low": 9, "close": 10.5, "volume": 1, "vwap": 10.1})
        return I.intraday_frame(rows)


def test_a_live_source_is_asked_once_for_the_span_then_only_for_the_last_day(tmp_path, monkeypatch):
    svc = service(tmp_path)
    src = FakeLive(reach=38)                      # a source a little short of its stated reach, weekends off
    monkeypatch.setattr(type(svc.market), "info", property(lambda self: SimpleNamespace(mode="live", label="Fake")))
    monkeypatch.setattr(svc.market, "provider", src)
    svc.market._call = lambda what, fn: fn()
    it = I.Intraday(svc.market, svc.db)
    it._trading_now = lambda: True
    a = it.bars("AAPL", "5m", 40)
    b = it.bars("AAPL", "5m", 40)
    c = it.bars("AAPL", "5m", 5)
    assert len(src.calls) == 1 and a.equals(b) and len(c) < len(a)        # one fetch covers every later call
    monkeypatch.setattr(I, "FRESH_SECONDS", 0)
    it.bars("AAPL", "5m", 40)
    assert len(src.calls) == 2
    start, end = src.calls[-1]
    assert (end - start).days <= 4, "a refresh asks only from the last kept day, not the whole span again"
    assert svc.db.one("SELECT asked_from FROM intraday_meta WHERE symbol = 'AAPL'")["asked_from"] <= src.calls[0][0].timestamp() + 1


def test_the_series_for_each_grouping_has_times_the_chart_can_draw(tmp_path):
    svc = service(tmp_path)
    cv = ChartView(svc, Analyst(svc))
    for group in ("5m", "15m", "1h", "1d", "1w", "1M", "1Q"):
        frame, notes = cv.series("AAPL", group, 30)
        assert len(frame) > 20 and frame["t"].is_monotonic_increasing and frame["t"].is_unique
        assert (frame["tl"] - frame["t"]).abs().max() <= 5 * 3600           # shifted by New York's offset only
        assert {"atr", "rsi", "macd", "ema20", "sma50"} <= set(frame.columns) and notes["kind"] == I.GROUPS[group]["kind"]
    assert I.local_shift(int(datetime(2026, 7, 1, 13, 30, tzinfo=timezone.utc).timestamp())) == \
        int(datetime(2026, 7, 1, 9, 30, tzinfo=timezone.utc).timestamp())


def test_tradier_timesales_are_read_from_their_documented_shape():
    from tradeassist.providers.tradier import TradierData
    t = TradierData("token", sandbox=True)
    sent = {}

    def get(path, params):
        sent.update(path=path, **params)
        return {"series": {"data": [
            {"time": "2026-10-16T09:30:00", "timestamp": 1792143000, "open": 10, "high": 11, "low": 9, "close": 10.5,
             "volume": 100, "vwap": 10.2},
            {"time": "2026-10-16T09:35:00", "timestamp": 1792143300, "open": 10.5, "high": 11, "low": 10, "close": 10.8,
             "volume": 50, "vwap": None},
            {"time": "bad"}]}}
    t._get = get
    start = datetime(2026, 10, 16, 13, 0, tzinfo=timezone.utc)
    df = t.intraday("BRK.B", "5m", start, start + timedelta(hours=7))
    assert sent["path"] == "/markets/timesales" and sent["interval"] == "5min" and sent["symbol"] == "BRK/B"
    assert sent["session_filter"] == "open" and sent["start"] == "2026-10-16 09:00"
    assert df["t"].tolist() == [1792143000, 1792143300] and df["vwap"].iloc[0] == 10.2 and pd.isna(df["vwap"].iloc[1])
    assert t.intraday_reach()["5m"] == 40
    with pytest.raises(DataError):
        t.intraday("AAPL", "1h", start, start)


def test_robinhood_bars_within_the_day_keep_their_times_and_skip_daily_ones():
    from tradeassist.providers.robinhood import find_intraday_bars
    rows = [{"begins_at": "2026-10-16T13:30:00Z", "open_price": "10.0", "high_price": "11", "low_price": "9", "close_price": "10.5",
             "volume": 7, "interpolated": False},
            {"begins_at": "2026-10-16T00:00:00Z", "open_price": "1", "close_price": "1"},        # a daily bar
            {"begins_at": "2026-10-16T13:35:00Z", "close_price": "10.6", "interpolated": True}]
    got = find_intraday_bars({"results": [{"symbol": "AAPL", "historicals": rows}]})
    assert got == [{"t": 1792157400, "open": 10.0, "high": 11.0, "low": 9.0, "close": 10.5, "volume": 7.0, "vwap": None}]


# --- the pattern search -------------------------------------------------------------------------------------------

def planted(seed=1, n=3000):
    rng = np.random.default_rng(seed)
    base = np.cumsum(rng.standard_normal(n)) * 0.3 + 100
    v = np.concatenate([np.linspace(0, -6, 20), np.linspace(-6, 0, 20)])
    for s in (500, 1500, 2500, n - 100):
        base[s:s + 40] += v * 2
    return pd.DataFrame({"close": base, "high": base + 0.5, "low": base - 0.5})


def test_a_planted_shape_is_found_in_the_past_but_never_in_itself():
    df = planted()
    n = len(df)
    r = S.search(df, n - 100, n - 61, "1d", atr=1.0, min_corr=0.9)
    starts = [m["start"] for m in r["matches"]]
    assert all(any(abs(s - want) <= 3 for s in starts) for want in (500, 1500, 2500))
    assert all(abs(s - (n - 100)) >= 40 for s in starts)
    assert all(abs(a - b) >= 40 for i, a in enumerate(starts) for b in starts[i + 1:])     # no overlap
    assert all(m["correlation"] >= 0.9 for m in r["matches"]) and r["min_correlation"] == 0.9
    assert all(m["end"] + 20 < n - 100 for m in r["matches"]), "nothing read after a match reaches the window"
    # A shape right before the window would have the window as its "after": refused.
    twice = pd.DataFrame({"close": np.concatenate([base := np.linspace(100, 110, 60), base, base]), "high": 0.0, "low": 0.0})
    twice["high"], twice["low"] = twice["close"] + 0.5, twice["close"] - 0.5
    r2 = S.search(twice, 120, 179, "1d", min_corr=0.9)
    assert all(m["end"] + 20 < 120 for m in r2["matches"])
    h = next(x for x in r["horizons"] if x["bars"] == 5)
    assert h["cases"] == len(r["matches"]) and h["up"] + h["down"] <= h["cases"] and 0 <= h["low"] <= h["up_frequency"] <= h["high"] <= 1
    sc = h["scenarios"]
    assert sc["up_range"]["cases"] + sc["within"]["cases"] + sc["down_range"]["cases"] == h["cases"]
    assert len(r["overlay"]["median"]) == 20 and r["overlay"]["from_close"] == df["close"].iloc[n - 61]
    m = r["matches"][0]
    assert len(m["fit"]) == 40 and len(m["after"]) == 20 and set(m["forward"]) == {"1", "3", "5", "10", "20"}


def test_the_bar_is_lowered_a_step_at_a_time_when_nothing_matches_and_the_result_says_so():
    rng = np.random.default_rng(7)
    base = np.cumsum(rng.standard_normal(600)) + 100
    df = pd.DataFrame({"close": base, "high": base + 0.5, "low": base - 0.5})
    r = S.search(df, 560, 599, "1d", min_corr=0.99)
    assert r["lowered_from"] == 0.99 and r["matches"] and r["min_correlation"] < 0.99 and "lowered" in r["note"]
    S.search(df, 560, 599, "1d", min_corr=0.99, limit=20)
    strict = pd.DataFrame({"close": np.linspace(100, 101, 60) + np.r_[np.zeros(40), np.sin(np.arange(20))],
                           "high": 0.0, "low": 0.0})
    strict["high"], strict["low"] = strict["close"] + 0.1, strict["close"] - 0.1
    r3 = S.search(strict, 40, 59, "1d", min_corr=0.99)
    assert r3["matches"] == [] and "closest was" in r3["note"] and r3["lowered_from"] is None
    assert "at least 8 bars" in S.search(df, 595, 599, "1d")["note"]
    short = S.search(df.iloc[:70].reset_index(drop=True), 30, 69, "1d", min_corr=0.5)   # 40 bars, 20 after: no room
    assert short["matches"] == [] and short["eligible_windows"] == 0 and "hold no stretch" in short["note"]
    flat = pd.DataFrame({"close": [100.0] * 50, "high": [100.0] * 50, "low": [100.0] * 50})
    assert "flat" in S.search(flat, 30, 49, "1d")["note"]


# --- the chart, similar and outlook -------------------------------------------------------------------------------

@pytest.fixture
def view(tmp_path):
    svc = service(tmp_path)
    return ChartView(svc, Analyst(svc))


def test_the_chart_covers_the_range_in_the_grouping_asked_for_and_loads_the_history_around_it(view):
    for rk, group, want in (("1d", None, "5m"), ("5d", "1h", "1h"), ("6m", None, "1d"), ("2y", "1w", "1w"), ("5y", "1M", "1M")):
        c = view.chart("AAPL", rk, group)
        assert c["group"]["key"] == want and c["range"]["range"] == rk
        bars, w = c["bars"], c["window"]
        assert len(bars) >= w["bars"] and w["end"] - w["start"] + 1 == w["bars"]
        assert all(bars[i]["tl"] < bars[i + 1]["tl"] for i in range(len(bars) - 1))
        assert isinstance(bars[w["end"] - w["offset"]]["complete"], bool) and len(c["future_times"]) >= 12
        assert c["future_times"][0] > bars[-1]["tl"]
        assert c["structure"]["swings"] and c["news"] is not None
        if len(bars) >= 80:
            assert c["reversal"] is not None and set(c["reversal"]["now"]) == {"bottom", "top"}
            assert c["reversal"]["now"]["bottom"]["stage"] in ("NONE", "WATCH", "CANDIDATE", "CONFIRMED", "FAILED")
            assert all(m["stage"] in ("CANDIDATE", "CONFIRMED", "FAILED") and m["tl"] in {b["tl"] for b in bars}
                       for m in c["reversal"]["markers"])
        else:
            assert c["reversal"] is None
    one = view.chart("AAPL", "1d")
    assert one["window"]["bars"] == 78 and one["group"]["kind"] == "intraday"
    assert "1d" in one["range"]["groups"] and "1w" not in one["range"]["groups"]


def test_custom_ranges_are_checked_and_honoured(view):
    c = view.chart("AAPL", "custom", "1d", "2026-03-02", "2026-06-30")
    first = datetime.fromtimestamp(c["bars"][c["window"]["start"] - c["window"]["offset"]]["t"], NY)
    last = datetime.fromtimestamp(c["bars"][c["window"]["end"] - c["window"]["offset"]]["t"], NY)
    assert first.date() >= datetime(2026, 3, 2).date() and last.date() <= datetime(2026, 6, 30).date()
    for bad in [dict(start=None), dict(start="2026-06-30", end="2026-03-02"), dict(start="yesterday")]:
        with pytest.raises(RequestError):
            view.chart("AAPL", "custom", None, bad.get("start"), bad.get("end"))
    with pytest.raises(RequestError):
        view.chart("AAPL", "weekly")
    with pytest.raises(RequestError, match="traded on"):
        view.chart("AAPL", "custom", "1d", "2026-10-17", "2026-10-18")          # a weekend
    # 1D is the last day that has bars, whatever the clock says.
    one = view.chart("AAPL", "1d")
    days = {datetime.fromtimestamp(b["t"], NY).date() for b in one["bars"][one["window"]["start"] - one["window"]["offset"]:]}
    assert len(days) == 1


def test_the_news_sits_on_the_bars_of_the_range(view):
    c = view.chart("AAPL", "1m")
    lo = c["bars"][c["window"]["start"] - c["window"]["offset"]]["t"]
    hi = c["bars"][c["window"]["end"] - c["window"]["offset"]]["t"] + 3600
    assert c["news"]["items"], "demo headlines are dated inside the month"
    for n in c["news"]["items"]:
        assert lo <= n["t"] <= hi and n["headline"] and n["tl"] in {b["tl"] for b in c["bars"]}
        # A headline sits on a bar of its own New York day (a pre-market headline on that day's first bar).
        bar = next(b for b in c["bars"] if b["tl"] == n["tl"])
        assert datetime.fromtimestamp(bar["t"], NY).date() <= datetime.fromtimestamp(n["t"], NY).date()
        later = [b for b in c["bars"] if b["t"] > bar["t"]]
        if later and datetime.fromtimestamp(bar["t"], NY).date() < datetime.fromtimestamp(n["t"], NY).date():
            assert datetime.fromtimestamp(later[0]["t"], NY).date() > datetime.fromtimestamp(n["t"], NY).date()
    far = view.chart("AAPL", "2y", "1d")
    assert far["news"]["items"] == [] and "30 days" in far["news"]["note"]


def test_similar_takes_the_window_on_the_screen_and_future_times_continue_the_grid(view):
    c = view.chart("AAPL", "6m")
    we = c["window"]["end"]
    sim = view.similar("AAPL", "6m", None, None, None, we - 39, we, min_corr=0.6)
    assert sim["window"] == {"start": we - 39, "end": we, "bars": 40, "tl_start": sim["window_times"][0], "tl_end": sim["window_times"][-1]}
    assert len(sim["window_times"]) == 40 and len(sim["future_times"]) == 20
    days = [datetime.fromtimestamp(t, timezone.utc) for t in sim["future_times"]]
    assert all(d.weekday() < 5 for d in days) and days[0] > datetime.fromtimestamp(sim["window_times"][-1], timezone.utc)
    for m in sim["matches"]:
        assert m["tl_end"] < sim["window_times"][0] and "date_start" in m
    intra = view.chart("AAPL", "1d")
    sim2 = view.similar("AAPL", "1d", None, None, None, intra["window"]["end"] - 20, intra["window"]["end"], min_corr=0.6)
    stamps = [datetime.fromtimestamp(t, timezone.utc) for t in sim2["future_times"]]
    assert stamps[0].strftime("%H:%M") == "09:30" and all(9 <= s.hour < 16 for s in stamps)   # the next session
    with pytest.raises(RequestError):
        view.similar("AAPL", "6m", None, None, None, min_corr=0.1)


def test_the_outlook_reads_patterns_a_top_or_bottom_the_signals_and_the_news_each_with_its_count(view):
    o = view.outlook("AAPL", "6m", None, None, None, horizon=5, min_corr=0.6)
    keys = [r["key"] for r in o["readings"]]
    assert keys[0] == "patterns" and "reversal" in keys and keys[-1] == "news"
    voted = []
    for r in o["readings"]:
        if r.get("frequency") is not None:
            assert 0 <= r["low"] <= r["frequency"] <= r["high"] <= 1 and r["cases"] > 0
        assert r["enough"] == (bool(r.get("cases")) and r["cases"] >= 10)
        assert r["votes"] == bool(r["enough"] and r.get("direction") and (r.get("frequency") or 0) >= 0.55)
        if r["votes"]:
            voted.append(r["direction"])
    lean = o["lean"]
    assert lean["of"] == len(voted) and lean["for"] == max(voted.count(1), voted.count(-1), 0) if voted else lean["of"] == 0
    assert lean["direction"] == ("higher" if voted.count(1) > voted.count(-1) else "lower" if voted.count(-1) > voted.count(1) else "no lean")
    assert "is a forecast" in o["note"] and o["horizon"]["bars"] == 5 and o["similar"]["matches"] is not None
    assert view.outlook("AAPL", "6m", None, None, None, min_corr=0.6)["horizon"]["bars"] == 5   # the default: a week on
    news = o["readings"][-1]
    assert "move" in news and "text" in news and all((n["url"] or "https://").startswith("http") for n in news["items"])


# --- the API ------------------------------------------------------------------------------------------------------

@pytest.fixture
def client(tmp_path):
    s = Settings(data_mode="demo", db_path=tmp_path / "t.db")
    svc = TradeAssistant(s, provider=DemoData(), db=Database(tmp_path / "t.db"))
    return TestClient(create_app(s, svc)), s


def test_the_chart_routes_answer_and_check_their_arguments(client):
    c, s = client
    assert c.get("/v2/chart/AAPL", params={"range": "1d"}).json()["group"]["key"] == "5m"
    assert c.get("/v2/chart/AAPL", params={"range": "1y", "group": "1w"}).json()["group"]["key"] == "1w"
    assert c.get("/v2/chart/AAPL", params={"range": "weekly"}).status_code == 400
    assert c.get("/v2/chart/AAPL", params={"range": "1y", "swing_atr": 2.5}).status_code == 400
    assert c.get("/v2/chart/AAPL/similar", params={"range": "6m", "min_correlation": 0.6}).status_code == 200
    out = c.get("/v2/chart/AAPL/outlook", params={"range": "6m", "horizon": 5, "min_correlation": 0.6}).json()
    assert out["readings"] and out["lean"]
    s.features["analyze_interactive"] = False
    assert c.get("/v2/chart/AAPL").status_code == 404


def test_settings_know_the_chart_feature(monkeypatch, tmp_path):
    from tradeassist.config import load_settings
    monkeypatch.setenv("FEATURE_ANALYZE_INTERACTIVE", "0")
    assert load_settings(tmp_path / "none.env").features["analyze_interactive"] is False


# --- the page's pure helpers -----------------------------------------------------------------------------------------

RUNNER = r"""
const fs = require("fs"), vm = require("vm");
const ctx = vm.createContext({ window: {}, document: {} });
const src = fs.readFileSync(process.argv[1], "utf8");
const start = src.indexOf("const CHARTX = ");
const end = src.indexOf("function interactiveChart(");
vm.runInContext(src.slice(start, end) + "\n;globalThis.CHARTX = CHARTX;", ctx);
const calls = JSON.parse(process.argv[2]);
process.stdout.write(JSON.stringify(calls.map(([fn, args]) => { try { return { ok: ctx.CHARTX[fn](...args) }; } catch (e) { return { error: String(e.message) }; } })));
"""


@pytest.mark.skipif(shutil.which("node") is None, reason="Node.js is not installed")
def test_the_pages_helpers_clamp_windows_build_overlays_and_lay_out_scenarios():
    sim = {"window_times": [10, 20, 30], "future_times": [40, 50],
           "matches": [{"fit": [1.0, 2.0, 3.0], "after": [0.1, -0.1]}, {"fit": [1.5, 2.5, 3.5], "after": []}],
           "overlay": {"from_close": 100.0, "median": [101.0, None], "p25": [99.0, 98.0], "p75": [103.0, 104.0]}}
    h = {"cases": 10, "up": 7, "up_frequency": 0.7, "low": 0.4, "high": 0.9,
         "scenarios": {"up_range": {"label": "Up", "frequency": 0.3, "low": 0.1, "high": 0.6, "cases": 3},
                       "within": {"label": "Within", "frequency": 0.5, "low": 0.2, "high": 0.8, "cases": 5},
                       "down_range": {"label": "Down", "frequency": 0.2, "low": 0.05, "high": 0.5, "cases": 2}}}
    done = subprocess.run(["node", "-e", RUNNER, str(STATIC / "chart.js"), json.dumps([
        ["clampWindow", [10.2, 50.9, 40, 400]], ["clampWindow", [30, 10, 100, 400]], ["clampWindow", [0, 999, 1000, 400]],
        ["overlayLines", [sim, 5]], ["scenarioRows", [h]], ["scenarioRows", [None]]])],
        capture_output=True, text=True, timeout=60)
    assert done.returncode == 0, done.stderr
    a, b, c, lines, rows, none = [x["ok"] for x in json.loads(done.stdout)]
    assert a == [10, 39] and b == [10, 30] and c == [600, 999]
    kinds = [ln["kind"] for ln in lines]
    assert kinds == ["match", "match", "median", "p25", "p75"]
    first = lines[0]["points"]
    assert [p["time"] for p in first] == [10, 20, 30, 40, 50] and abs(first[3]["value"] - 110) < 1e-9
    assert len(first) == len({p["time"] for p in first})                         # times unique and ascending
    assert lines[1]["points"][-1]["time"] == 30
    assert [p["time"] for p in lines[2]["points"]] == [30, 40] and lines[2]["points"][0]["value"] == 100
    assert [r["key"] for r in rows] == ["up_range", "within", "down_range", "up"] and rows[-1]["pct"] == 70
    assert none == []
