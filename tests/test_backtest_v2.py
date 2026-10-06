"""Backtest V2: condition strategies, regime filters, borrow fees, Monte Carlo, walk-forward, options refusal."""

import numpy as np
import pandas as pd
import pytest
from fastapi.testclient import TestClient

from tradeassist import backtest_v2 as bv
from tradeassist.api import create_app
from tradeassist.backtest import Backtester, BacktestParams
from tradeassist.config import Settings
from tradeassist.db import Database
from tradeassist.features import FEATURE_DEFAULTS
from tradeassist.indicators import enrich
from tradeassist.providers.demo import DemoData
from tradeassist.service import TradeAssistant
from tradeassist import trend
from test_trend import walk


def test_strategies_are_checked_and_never_run_as_code():
    ok = bv.validate({"long": [{"field": "rsi", "op": "<", "value": 30}, {"field": "close", "op": "crosses_above", "value": "sma50"}]})
    assert len(ok["long"]) == 2 and ok["short"] == []
    bad = [{"long": []},
           {"long": [{"field": "rsi", "op": "==", "value": 1}]},
           {"long": [{"field": "__class__", "op": ">", "value": 1}]},
           {"long": [{"field": "rsi", "op": ">", "value": "os.system('x')"}]},
           {"long": [{"field": "trend_state", "op": ">", "value": 1}]},
           {"long": [{"field": "pattern", "op": "fired", "value": "nope"}]},
           {"long": [{"field": "rsi", "op": "in", "value": [1]}]},
           {"long": [{"field": "rsi", "op": ">", "value": float("nan")}]}]
    for b in bad:
        with pytest.raises(bv.StrategyError):
            bv.validate(b)


def test_conditions_read_only_the_past():
    df = enrich(walk(n=400, seed=12))
    states = trend.state_series(df)
    conds = [{"field": "close", "op": "crosses_above", "value": "sma20"}, {"field": "trend_state", "op": "in", "value": ["UP", "STRONG_UP", "NEUTRAL"]}]
    full = bv.evaluate_conditions(bv.columns(df, states, None), conds, None)
    k = 300
    part_df = enrich(df.iloc[:k][["date", "open", "high", "low", "close", "volume"]])
    part = bv.evaluate_conditions(bv.columns(part_df, trend.state_series(part_df), None), conds, None)
    assert (part == full[:k]).all() and full.any()
    c, s20 = df["close"].to_numpy(), df["sma20"].to_numpy()
    i = int(np.flatnonzero(bv.evaluate_conditions(bv.columns(df, None, None), conds[:1], None))[0])
    assert c[i] > s20[i] and c[i - 1] <= s20[i - 1]


def test_regime_filter_and_both_sides():
    df = enrich(walk(n=300, seed=5))
    frames = {"A": df}
    states = {"A": trend.state_series(df)}
    regimes = pd.Series(["BULL_CALM"] * 150 + ["BEAR_CALM"] * 150, index=pd.to_datetime(df["date"]))
    cal = Backtester(frames).calendar
    strat = bv.validate({"long": [{"field": "rsi", "op": ">", "value": 0}], "short": [], "regimes": ["BULL_CALM"]})
    d, _, counts = bv.signal_matrices(frames, states, regimes, strat, cal)
    assert d[150:, 0].max() == 0 and d[:150, 0].sum() > 0 and counts["blocked_by_regime"] > 0
    both = bv.validate({"long": [{"field": "rsi", "op": ">", "value": 0}], "short": [{"field": "rsi", "op": ">", "value": 0}]})
    d2, _, _ = bv.signal_matrices(frames, states, None, both, cal)
    assert not d2.any()                                   # conflicting signals cancel


def test_shorts_pay_to_borrow():
    df = walk(n=120, seed=3)
    frames = {"A": df}
    n = len(df)
    direction = np.zeros((n, 1), dtype=int)
    direction[10, 0] = -1
    p = BacktestParams(stop_loss_pct=0.9, take_profit_pct=0.9, holding_period_days=30, allow_shorts=True)
    free = Backtester(frames, (direction, np.ones((n, 1)))).run(p)
    paid = Backtester(frames, (direction, np.ones((n, 1)))).run(BacktestParams(**{**p.__dict__, "short_borrow_rate_pct": 10.0}))
    t0, t1 = free["trades"][0], paid["trades"][0]
    days = t1["days_held"]
    assert t1["borrow_cost"] == pytest.approx(t1["entry_price"] * t1["shares"] * 0.10 * days / 360, rel=1e-6)
    assert t1["pnl"] == pytest.approx(t0["pnl"] - t1["borrow_cost"])
    with pytest.raises(ValueError):
        BacktestParams(short_borrow_rate_pct=-1).validate()


def test_monte_carlo():
    rng = np.random.default_rng(0)
    r = rng.normal(0.0005, 0.01, 500)
    m = bv.monte_carlo(r, 100_000)
    assert m["return_pct"]["p5"] <= m["return_pct"]["p50"] <= m["return_pct"]["p95"]
    assert m["max_drawdown_pct"]["p5"] <= m["max_drawdown_pct"]["p95"] <= 0
    assert bv.monte_carlo(r, 100_000) == m                   # seeded: the same every run
    assert bv.monte_carlo(r[:30], 100_000) is None


@pytest.fixture()
def client(tmp_path):
    settings = Settings(data_mode="demo", db_path=tmp_path / "t.db", watchlist=["AAPL", "MSFT", "KO", "XOM"])
    return TestClient(create_app(settings, TradeAssistant(settings, provider=DemoData(), db=Database(":memory:"))))


def test_backtest_v2_through_the_api(client):
    f = client.get("/v2/backtest/fields").json()
    assert "rsi" in f["numeric"] and "trend_following" in f["presets"] and f["options_backtest"] is False
    r = client.post("/v2/backtest", json={"preset": "both_ways", "years": 2}).json()
    assert r["num_trades"] > 0 and r["monte_carlo"]["paths"] == 1000 and r["params"]["short_borrow_rate_pct"] == 0.5
    assert {t["direction"] for t in r["trades"]} <= {"LONG", "SHORT"} and "benchmark" in r["equity_curve"][0]
    custom = client.post("/v2/backtest", json={"years": 2, "monte_carlo": False, "params": {"stop_loss_pct": 0.03},
                                               "strategy": {"long": [{"field": "pattern", "op": "fired", "value": "hammer"}]}}).json()
    assert custom["monte_carlo"] is None and custom["params"]["stop_loss_pct"] == 0.03
    assert client.post("/v2/backtest", json={"strategy": {"long": [{"field": "x", "op": ">", "value": 1}]}}).status_code == 400
    assert client.post("/v2/backtest", json={"preset": "nope"}).status_code == 400
    w = client.post("/v2/backtest/walkforward", json={"preset": "trend_following", "years": 4, "anchored": True}).json()
    assert w["anchored"] and w["folds"] and len({f["train_start"] for f in w["folds"]}) == 1


def test_option_backtests_are_refused_not_faked(tmp_path, client):
    assert client.post("/v2/backtest", json={"instrument": "OPTION", "preset": "pullback"}).status_code == 404
    on = Settings(data_mode="demo", db_path=tmp_path / "u.db", features={**FEATURE_DEFAULTS, "options_backtest": True})
    c2 = TestClient(create_app(on, TradeAssistant(on, provider=DemoData(), db=Database(":memory:"))))
    r = c2.post("/v2/backtest", json={"instrument": "OPTION", "preset": "pullback"})
    assert r.status_code == 400 and "historical option prices" in r.json()["error"]
