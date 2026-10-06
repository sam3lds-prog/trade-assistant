"""Market regime and indicators read in context."""

import numpy as np
import pandas as pd
import pytest
from fastapi.testclient import TestClient

from tradeassist import regime
from tradeassist.api import create_app
from tradeassist.config import Settings
from tradeassist.db import Database
from tradeassist.indicators import enrich
from tradeassist.providers.demo import DemoData
from tradeassist.service import TradeAssistant
from test_trend import walk


def test_classify_regimes():
    assert regime.classify("UP", "UP", "CALM") == "BULL_CALM"
    assert regime.classify("STRONG_UP", None, "STRESSED") == "BULL_VOLATILE"
    assert regime.classify("UP", "DOWN", "NORMAL") == "TRANSITION"           # SPY and QQQ disagree
    assert regime.classify("DOWN", "STRONG_DOWN", "NORMAL") == "BEAR_CALM"
    assert regime.classify("DOWN", "DOWN", "ELEVATED") == "BEAR_VOLATILE"
    assert regime.classify("UP_WEAKENING", "UP", "CALM") == "TRANSITION"
    assert regime.classify("NEUTRAL", "UP", "STRESSED") == "RANGE"


def test_volatility_buckets_from_vix_or_spy():
    assert regime._vol_bucket(14.0, np.nan, np.nan) == ("CALM", "VIX")
    assert regime._vol_bucket(20.0, 0.5, 0.99) == ("ELEVATED", "VIX")          # VIX wins when there is one
    assert regime._vol_bucket(31.0, np.nan, np.nan) == ("STRESSED", "VIX")
    assert regime._vol_bucket(np.nan, 0.40, 0.2) == ("STRESSED", "SPY realized")
    assert regime._vol_bucket(np.nan, 0.12, 0.5) == ("NORMAL", "SPY realized")
    assert regime._vol_bucket(np.nan, np.nan, np.nan)[0] == "NORMAL"
    fake = pd.DataFrame({"date": pd.bdate_range("2026-01-01", periods=100), "close": np.linspace(300, 400, 100)})
    assert regime.usable_vix(fake) is None                                    # a stock price, not VIX
    real = fake.assign(close=np.linspace(12, 28, 100))
    assert regime.usable_vix(real) is real


def test_regime_series_is_causal_and_follows_spy():
    spy = enrich(walk(drift=0.0015, seed=21))
    qqq = enrich(walk(drift=0.0015, seed=22))
    full = regime.regime_series(spy, qqq)
    part = regime.regime_series(enrich(spy.iloc[:400][["date", "open", "high", "low", "close", "volume"]]),
                                enrich(qqq.iloc[:400][["date", "open", "high", "low", "close", "volume"]]))
    assert part["regime"].tolist() == full["regime"].iloc[:400].tolist()
    bull = full["regime"].isin(["BULL_CALM", "BULL_VOLATILE"]).mean()
    bear = full["regime"].isin(["BEAR_CALM", "BEAR_VOLATILE"]).mean()
    assert bull > 0.4 and bear < 0.15


def test_unfinished_spy_bar_gives_a_provisional_regime():
    spy = enrich(walk(seed=4))
    spy["bar_complete"] = True
    spy.loc[spy.index[-1], "bar_complete"] = False
    r = regime.analyse(spy)
    assert r["status"] == "PROVISIONAL" and r["confirmed"]["status"] == "CONFIRMED"
    assert r["confirmed"]["date"] < r["provisional"]["date"] and "qqq" not in r["confirmed"]
    assert any("VIX" in n for n in r["notes"]) and len(r["history"]) == 252
    with pytest.raises(ValueError):
        regime.analyse(spy.iloc[:40])


def row(**kw):
    base = {"rsi": 55.0, "pos52": 50.0, "close": 100.0, "bb_high": 105.0, "bb_low": 95.0}
    return pd.Series({**base, **kw})


def test_overbought_means_different_things_in_different_trends():
    up = regime.interpret(row(rsi=76), "UP", "BULL_CALM")[0]
    flat = regime.interpret(row(rsi=76), "NEUTRAL", "RANGE")[0]
    down = regime.interpret(row(rsi=76), "DOWN", "BEAR_CALM")[0]
    assert up["lean"] == "bullish" and "strength" in up["reading"]
    assert flat["lean"] == "bearish" and down["lean"] == "bearish"
    assert regime.interpret(row(rsi=25), "DOWN", "BEAR_CALM")[0]["lean"] == "bearish"
    assert regime.interpret(row(rsi=25), "UP", "BULL_CALM")[0]["lean"] == "bullish"
    assert "pullback zone" in regime.interpret(row(rsi=45), "UP", "BULL_CALM")[0]["reading"]


def test_52_week_and_bands_in_context():
    hi_in_bear = {r["indicator"]: r for r in regime.interpret(row(pos52=95), "NEUTRAL", "BEAR_CALM")}
    assert hi_in_bear["52-week range"]["lean"] == "bullish" and "relative strength" in hi_in_bear["52-week range"]["reading"]
    lo_in_bull = {r["indicator"]: r for r in regime.interpret(row(pos52=5), "NEUTRAL", "BULL_CALM")}
    assert lo_in_bull["52-week range"]["lean"] == "bearish" and "laggard" in lo_in_bull["52-week range"]["reading"]
    walk_band = {r["indicator"]: r for r in regime.interpret(row(close=107), "STRONG_UP", "BULL_CALM")}
    assert walk_band["Bollinger bands"]["lean"] == "bullish"
    stretched = {r["indicator"]: r for r in regime.interpret(row(close=107), "NEUTRAL", "RANGE", 0.1)}
    assert stretched["Bollinger bands"]["lean"] == "bearish" and "Band width" in stretched
    caution = regime.interpret(row(rsi=76), "UP", "TRANSITION")
    assert "transition" in caution[0]["reading"] and "transition" not in caution[1]["reading"]


def test_regime_through_the_api(tmp_path):
    settings = Settings(data_mode="demo", db_path=tmp_path / "t.db")
    c = TestClient(create_app(settings, TradeAssistant(settings, provider=DemoData(), db=Database(":memory:"))))
    g = c.get("/v2/regime").json()
    assert g["regime"] in regime.REGIMES and g["version"].startswith("regime")
    assert g["confirmed"]["spy"]["state"] and g["confirmed"]["qqq"]["state"]
    a = c.get("/v2/analysis/KO").json()
    assert a["regime"]["regime"] == g["regime"] and a["interpretation"]["readings"]
    p = c.post("/v2/positions", json={"symbol": "KO", "quantity": 3, "entry": {"price": 60}}).json()
    assert p["snapshots"][0]["market_regime"] == g["regime"]
