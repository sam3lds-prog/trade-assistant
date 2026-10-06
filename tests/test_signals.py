"""Signal history, past-case statistics and calibration of the evidence score."""

from datetime import date

import numpy as np
import pandas as pd
import pytest
from fastapi.testclient import TestClient

import tradeassist.market as market_mod
from tradeassist import signalbook
from tradeassist.api import create_app
from tradeassist.config import Settings
from tradeassist.db import Database
from tradeassist.features import FEATURE_DEFAULTS
from tradeassist.indicators import enrich
from tradeassist.providers.demo import DemoData
from tradeassist.service import TradeAssistant
from test_bars import at
from test_trend import walk


def test_wilson_interval():
    lo, hi = signalbook.wilson(50, 100)
    assert lo == pytest.approx(0.4038, abs=1e-3) and hi == pytest.approx(0.5962, abs=1e-3)
    assert signalbook.wilson(0, 0) == (None, None)
    lo, hi = signalbook.wilson(10, 10)
    assert hi == 1.0 and 0.65 < lo < 0.75


def test_outcomes_start_at_the_next_open_and_never_look_back():
    df = walk(n=80, seed=2)
    spy = walk(n=80, seed=9)
    o = signalbook.outcomes(df, spy)
    t = 30
    assert o["fwd_10"].iloc[t] == pytest.approx(df["close"].iloc[t + 10] / df["open"].iloc[t + 1] - 1)
    assert o["spy_10"].iloc[t] == pytest.approx(spy["close"].iloc[t + 10] / spy["open"].iloc[t + 1] - 1)
    assert o["high_20"].iloc[t] == pytest.approx(df["high"].iloc[t + 1:t + 21].max() / df["open"].iloc[t + 1] - 1)
    assert o["low_20"].iloc[t] == pytest.approx(df["low"].iloc[t + 1:t + 21].min() / df["open"].iloc[t + 1] - 1)
    assert np.isnan(o["fwd_10"].iloc[-5]) and np.isnan(o["high_20"].iloc[-10])     # the future is not known yet


def test_summaries_count_the_signal_way_round():
    rows = [{"date": f"2026-0{1 + i // 28}-{1 + i % 28:02d}", "fwd_10": r, "spy_10": 0.0, "high_20": abs(r) + 0.01,
             "low_20": -abs(r) - 0.01} for i, r in enumerate([0.05, -0.02, -0.03, -0.04, 0.01])]
    up = signalbook.summarize(rows, +1)
    down = signalbook.summarize(rows, -1)
    assert up["cases"] == 5 and up["went_the_signal_way"] == 2 and down["went_the_signal_way"] == 3
    assert up["mean_return"] == pytest.approx(-0.006) and down["mean_return"] == pytest.approx(0.006)
    assert down["mean_excess_vs_spy"] == pytest.approx(0.006) and not up["enough"]
    assert signalbook.summarize([], 1)["frequency"] is None


def test_calibration_needs_order_and_a_real_gap():
    rng = np.random.default_rng(1)
    n = 6000
    net = rng.uniform(-100, 100, n)
    good = pd.DataFrame({"net": net, "regime": "BULL_CALM", "fwd_10": rng.normal(0.0004 * net, 0.03), "spy_10": 0.0,
                         "fwd_20": np.nan, "spy_20": np.nan, "fwd_5": np.nan, "spy_5": np.nan})
    c = signalbook.calibrate(good)
    freqs = [b["frequency"] for b in c["buckets"]]
    assert c["calibrated"] and c["ordered"] and freqs[-1] > freqs[0] + 0.2 and c["p_value"] < 0.05
    noise = good.assign(fwd_10=rng.normal(0, 0.03, n))
    assert not signalbook.calibrate(noise)["calibrated"]
    assert c["buckets"][0]["by_regime"]["BULL_CALM"]["cases"] == c["buckets"][0]["cases"]
    assert signalbook.bucket_for(-45) == (-50.0, -25.0) and signalbook.bucket_for(100) == (50.0, 100.01)


def test_collect_records_patterns_and_trend_changes_with_context():
    df = enrich(walk(n=400, drift=0.0008, seed=6))
    from tradeassist import trend
    states = trend.state_series(df)
    regimes = pd.Series("RANGE", index=pd.to_datetime(df["date"]))
    rows = signalbook.collect("TEST", df, None, regimes, states)
    keys = {r["signal_key"] for r in rows}
    assert any(k.startswith("pattern:") for k in keys) and any(k.startswith("trend:") for k in keys)
    r = next(r for r in rows if r["signal_key"].startswith("trend:"))
    i = int(np.flatnonzero(df["date"].dt.strftime("%Y-%m-%d") == r["date"])[0])
    assert r["trend_state"] == states["trend_state"].iloc[i] == r["signal_key"].split(":")[1]
    assert r["regime"] == "RANGE" and r["direction"] == signalbook.signal_direction(r["signal_key"])
    if i + 10 < len(df):
        assert r["fwd_10"] == pytest.approx(df["close"].iloc[i + 10] / df["open"].iloc[i + 1] - 1)


@pytest.fixture()
def client(tmp_path, monkeypatch):
    # 11:00 in New York on a trading day, so today's demo bar is unfinished.
    monkeypatch.setattr(market_mod.time, "time", lambda: at("2026-10-06T11:00:00"))
    settings = Settings(data_mode="demo", db_path=tmp_path / "t.db", watchlist=["AAPL", "MSFT", "KO", "XOM", "JPM"])
    svc = TradeAssistant(settings, provider=DemoData(today=date(2026, 10, 6)), db=Database(":memory:"))
    return TestClient(create_app(settings, svc)), svc


def test_signal_history_through_the_api(client):
    c, svc = client
    assert c.get("/v2/signals/calibration").json()["built"] is False
    before = c.get("/v2/signals/AAPL").json()
    assert before["history_built"] is False and before["notes"]
    m = c.post("/v2/signals/history/build", json={"years": 2}).json()
    assert m["symbols"] == ["AAPL", "MSFT", "KO", "XOM", "JPM"] and m["occurrences"] > 100
    assert set(m["calibration"]) == {"10", "20"}
    # Nothing is recorded from today's unfinished bar.
    assert not svc.db.query("SELECT 1 FROM signal_occurrences WHERE date = '2026-10-06'")
    g = c.get("/v2/signals/AAPL").json()
    assert g["history_built"] and g["score"]["calibrated"] in (True, False)
    assert g["score"]["date"] == "2026-10-05"            # the last finished bar
    for s in g["signals"]:
        assert s["status"] in ("CONFIRMED", "PROVISIONAL") and s["history"]["all"]["horizon"] == 10
        if s["date"] == "2026-10-06":
            assert s["status"] == "PROVISIONAL"
    p = c.get("/v2/patterns/AAPL").json()
    assert len(p["patterns"]) == 20 and p["patterns"][0]["events_here"] >= p["patterns"][-1]["events_here"]
    again = c.post("/v2/signals/history/build", json={"symbols": ["AAPL"], "years": 2}).json()
    n = svc.db.one("SELECT COUNT(*) AS n FROM signal_occurrences WHERE symbol = 'AAPL'")["n"]
    assert again["occurrences"] == n                       # a rebuild replaces, it does not duplicate
    assert c.post("/v2/signals/history/build", json={"symbols": [], "years": 2}).status_code in (200, 400)


def test_signals_switched_off(tmp_path):
    off = Settings(data_mode="demo", db_path=tmp_path / "t.db", features={**FEATURE_DEFAULTS, "signal_calibration": False})
    c = TestClient(create_app(off, TradeAssistant(off, provider=DemoData(), db=Database(":memory:"))))
    assert c.get("/v2/signals/AAPL").status_code == 404 and c.post("/v2/signals/history/build", json={}).status_code == 404
