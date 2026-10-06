"""Trend Engine V2: states, evidence, the state machine, causality and the API."""

import numpy as np
import pandas as pd
import pytest
from fastapi.testclient import TestClient

from tradeassist import trend
from tradeassist.api import create_app
from tradeassist.config import Settings
from tradeassist.db import Database
from tradeassist.features import FEATURE_DEFAULTS
from tradeassist.indicators import enrich
from tradeassist.providers.demo import DemoData
from tradeassist.sectors import sector_fund
from tradeassist.service import TradeAssistant


def walk(n=600, drift=0.0, vol=0.012, seed=3, start="2023-01-02"):
    rng = np.random.default_rng(seed)
    close = 100 * np.exp(np.cumsum(rng.normal(drift, vol, n)))
    open_ = np.r_[close[0], close[:-1]] * (1 + rng.normal(0, 0.002, n))
    return pd.DataFrame({"date": pd.bdate_range(start, periods=n), "open": open_,
                         "high": np.maximum(open_, close) * (1 + np.abs(rng.normal(0, 0.006, n))),
                         "low": np.minimum(open_, close) * (1 - np.abs(rng.normal(0, 0.006, n))),
                         "close": close, "volume": rng.integers(800_000, 1_200_000, n).astype(float)})


def test_steady_trends_read_as_trends():
    rising = trend.analyse(enrich(walk(drift=0.002)))
    falling = trend.analyse(enrich(walk(drift=-0.002)))
    share = lambda r, side: np.mean([h["state"] in side for h in r["history"]])
    assert share(rising, trend.UP_SIDE) > 0.6 and share(falling, trend.DOWN_SIDE) > 0.6
    assert share(rising, trend.DOWN_SIDE) < 0.2 and share(falling, trend.UP_SIDE) < 0.2
    assert rising["state"] in ("UP", "STRONG_UP")
    c = rising["confirmed"]
    assert c["bullish_score"] > 60 > c["bearish_score"] and c["net_score"] > 25
    assert 0 <= c["bearish_score"] <= 100 and abs(sum(x["weight"] for x in c["components"]) - 100) < 1e-9
    assert "not probabilities" in rising["note"]


def test_the_state_on_any_day_uses_nothing_after_it():
    df = enrich(walk(drift=0.0005, seed=11))
    full = trend.state_series(df)
    for k in (260, 333, 410, 599):
        part = trend.state_series(enrich(df.iloc[:k][["date", "open", "high", "low", "close", "volume"]]))
        assert part["trend_state"].tolist() == full["trend_state"].iloc[:k].tolist()
        assert np.allclose(part["net_score"].to_numpy(), full["net_score"].iloc[:k].to_numpy(), equal_nan=True)


def test_the_state_machine_never_skips_a_step():
    rng = np.random.default_rng(5)
    for _ in range(40):
        n = 400
        net = np.cumsum(rng.normal(0, 9, n)).clip(-100, 100)
        coin = rng.random(n)
        states = trend.run_states(net, np.ones(n), coin < 0.3, coin > 0.7, rng.random(n) < 0.05,
                                  rng.random(n) < 0.5, rng.random(n) < 0.5)
        for a, b in zip(states, states[1:]):
            if a in trend.UP_SIDE:
                assert b not in ("DOWN", "STRONG_DOWN"), (a, b)
            if a in ("DOWN", "STRONG_DOWN", "DOWN_DEVELOPING"):
                assert b not in trend.UP_SIDE, (a, b)
            if b == "DOWN" and a not in ("DOWN", "STRONG_DOWN"):
                assert a == "DOWN_DEVELOPING"
            if b == "STRONG_UP":
                assert a in ("UP", "STRONG_UP")


def test_hysteresis_keeps_a_trend_through_a_soft_patch():
    net = np.array([0, 30, 30, 18, 20, 16, 30, 10, 5, -5, -30, -30, -30, -30, -30, -10.0])
    n = len(net)
    states = trend.run_states(net, np.ones(n), np.zeros(n, bool), np.zeros(n, bool), np.zeros(n, bool),
                              np.zeros(n, bool), np.zeros(n, bool))
    assert states[:7] == ["NEUTRAL", "UP", "UP", "UP", "UP", "UP", "UP"]
    assert states[7] == "UP_WEAKENING" and states[9] == "NEUTRAL"
    assert states[10:15] == ["DOWN_DEVELOPING"] * 4 + ["DOWN"]      # five days without a down structure
    assert states[15] == "NEUTRAL"
    thin = trend.run_states(np.full(3, 80.0), np.full(3, 0.3), *[np.zeros(3, bool)] * 5)
    assert thin == ["NEUTRAL"] * 3                                  # too little data for a state


def test_unfinished_bar_gives_a_provisional_reading_beside_the_confirmed_one():
    df = enrich(walk(drift=0.001, seed=8))
    df["bar_complete"] = True
    df.loc[df.index[-1], "bar_complete"] = False
    r = trend.analyse(df)
    assert r["status"] == "PROVISIONAL" and r["provisional"]["date"] == df["date"].iloc[-1].strftime("%Y-%m-%d")
    assert r["confirmed"]["date"] == df["date"].iloc[-2].strftime("%Y-%m-%d")
    assert r["confirmed"]["status"] == "CONFIRMED"
    assert all(t["date"] != r["provisional"]["date"] for t in r["transitions"])
    assert r["history"][-1]["complete"] is False


def test_relative_strength_and_validated_patterns_add_evidence():
    stock, spy = walk(drift=0.002, seed=1), walk(drift=0.0, seed=2)
    df = enrich(stock)
    alone = trend.analyse(df)["confirmed"]
    both = trend.analyse(df, {"SPY": spy})["confirmed"]
    rs = {c["key"]: c for c in both["components"]}["relative_strength"]
    assert rs["available"] == 10 and rs["bullish"] > 5          # the two SPY items; no sector fund given
    assert {c["key"]: c for c in alone["components"]}["relative_strength"]["available"] == 0
    assert alone["coverage"] < both["coverage"]
    # A bullish pattern that passed the lab, firing on every day, earns the pattern points.
    import tradeassist.trend as tmod
    real = tmod.detect_all
    try:
        tmod.detect_all = lambda d: pd.DataFrame({"hammer": np.ones(len(d), bool)}, index=d.index)
        withp = trend.analyse(df, validated={"hammer": [{"horizon": 10, "verdict": "supported"}]})["confirmed"]
        pts = {c["key"]: c for c in withp["components"]}["patterns"]
        assert pts["bullish"] == 4 and pts["available"] == 8
        failed = trend.analyse(df, validated={"hammer": [{"horizon": 10, "verdict": "contradicted"}]})["confirmed"]
        assert {c["key"]: c for c in failed["components"]}["patterns"]["bullish"] == 0
    finally:
        tmod.detect_all = real


def test_sector_funds():
    assert sector_fund("AAPL") == "XLK" and sector_fund("brk/b") == "XLF" and sector_fund("META") == "XLC"
    assert sector_fund("XLK") is None and sector_fund("ZZZZ") is None
    assert sector_fund("ZZZZ", 3674) == "XLK" and sector_fund("ZZZZ", "6022") == "XLF"
    assert sector_fund("ZZZZ", 1311) == "XLE" and sector_fund("ZZZZ", 2834) == "XLV"


def test_trend_through_the_api_and_in_position_snapshots(tmp_path):
    settings = Settings(data_mode="demo", db_path=tmp_path / "t.db")
    c = TestClient(create_app(settings, TradeAssistant(settings, provider=DemoData(), db=Database(":memory:"))))
    r = c.get("/v2/trend/AAPL").json()
    assert r["state"] in trend.STATES and r["version"].startswith("trend_engine_v2")
    assert r["relative_strength"]["sector_fund"] == "XLK" and "spy_3m" in r["relative_strength"]
    assert len(r["history"]) == 252 and r["confirmed"]["evidence"]
    a = c.get("/v2/analysis/AAPL").json()
    assert a["trend"]["state"] == r["state"] and a["data_quality"]["symbol"] == "AAPL"
    p = c.post("/v2/positions", json={"symbol": "AAPL", "quantity": 5, "entry": {"price": 200}}).json()
    snap = p["snapshots"][0]
    assert snap["trend_state"] in trend.STATES and snap["bullish_score"] is not None
    assert snap["raw"]["analysis"]["trend"]["version"].startswith("trend_engine_v2")

    off = Settings(data_mode="demo", db_path=tmp_path / "u.db", features={**FEATURE_DEFAULTS, "trend_engine_v2": False})
    c2 = TestClient(create_app(off, TradeAssistant(off, provider=DemoData(), db=Database(":memory:"))))
    assert c2.get("/v2/trend/AAPL").status_code == 404
    p2 = c2.post("/v2/positions", json={"symbol": "AAPL", "quantity": 5, "entry": {"price": 200}}).json()
    assert p2["snapshots"][0]["trend_state"] is None


@pytest.mark.parametrize("n", [10, 29])
def test_too_little_history_is_refused(n):
    with pytest.raises(ValueError):
        trend.analyse(enrich(walk(n=n)))
