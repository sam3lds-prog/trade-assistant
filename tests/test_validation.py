"""Historical analytics, signals and backtests are causal, honest about dependence, and
reproducible (2.3, Sprint 6)."""

from __future__ import annotations

import math

import numpy as np
import pandas as pd
import pytest
from fastapi.testclient import TestClient

from conftest import random_walk
from tradeassist import analytics, backtest_v2, reversal, runs, signalbook, trend
from tradeassist.api import create_app
from tradeassist.backtest import Backtester, BacktestParams, walk_forward
from tradeassist.config import Settings
from tradeassist.db import Database
from tradeassist.indicators import enrich
from tradeassist.patterns import detect_all
from tradeassist.providers.demo import DemoData
from tradeassist.service import TradeAssistant

RAW = ["date", "open", "high", "low", "close", "volume"]


def walks():
    return [(seed, enrich(random_walk(n=520, seed=seed, drift=d))) for seed, d in ((3, 0.0006), (8, -0.0005), (21, 0.0))]


# --- 5.2 no look-ahead, checked by truncation -------------------------------------------------------

@pytest.mark.parametrize("seed,df", walks())
def test_5_2_every_engine_reads_the_same_on_a_truncated_history(seed, df):
    """A reading for day T computed from the history up to T must equal the reading for T from the
    full history: indicators, patterns, trend states, reversal stages and events, and the
    backtest conditions."""
    full_states = trend.state_series(df)
    full_rev = reversal.evaluate(df, full_states)
    full_hits = detect_all(df)
    strat = backtest_v2.validate({"long": [{"field": "trend_state", "op": "in", "value": ["UP", "STRONG_UP"]},
                                           {"field": "rsi", "op": "crosses_above", "value": 50}],
                                  "short": [{"field": "close", "op": "crosses_below", "value": "sma50"}], "regimes": []})
    cal = pd.DatetimeIndex(df["date"])
    full_dir, _, _ = backtest_v2.signal_matrices({"X": df}, {"X": full_states}, None, strat, cal)
    for k in (300, 377, 451):
        cut = enrich(df.iloc[:k][RAW])
        for col in ("rsi", "macd_hist", "sma50", "atr", "adx", "bb_low"):
            assert np.allclose(cut[col].to_numpy(), df[col].iloc[:k].to_numpy(), equal_nan=True), (seed, k, col)
        assert detect_all(cut).equals(full_hits.iloc[:k].reset_index(drop=True)), (seed, k)
        states = trend.state_series(cut)
        assert states["trend_state"].tolist() == full_states["trend_state"].iloc[:k].tolist(), (seed, k)
        rev = reversal.evaluate(cut, states)
        for side in reversal.SIDES:
            assert rev[side]["stage"].tolist() == full_rev[side]["stage"][:k].tolist(), (seed, k, side)
        assert reversal.events(rev) == [e for e in reversal.events(full_rev) if e[0] < k]
        d, _, _ = backtest_v2.signal_matrices({"X": cut}, {"X": states}, None, strat, cal[:k])
        assert (d[:, 0] == full_dir[:k, 0]).all(), (seed, k)


def test_5_2_recorded_signals_and_their_outcomes_do_not_depend_on_later_bars():
    df = enrich(random_walk(n=520, seed=5, drift=0.0003))
    full = signalbook.collect("X", df, None, None, trend.state_series(df))
    k = 430
    cut = enrich(df.iloc[:k][RAW])
    part = signalbook.collect("X", cut, None, None, trend.state_series(cut))
    key = lambda r: (r["signal_key"], str(r["date"])[:10])                      # noqa: E731
    assert {key(r) for r in part} == {key(r) for r in full if r["date"] < str(df["date"].iloc[k].date())}
    by = {key(r): r for r in full}
    last_known = df["date"].iloc[k - 1]
    for r in part:
        # An outcome that ends before the cut was fully known then and must match; one that needs later bars is empty.
        i = int(np.flatnonzero(df["date"].dt.strftime("%Y-%m-%d") == str(r["date"])[:10])[0])
        if i + 1 + 10 <= k - 1:
            assert r["fwd_10"] == pytest.approx(by[key(r)]["fwd_10"]), r
        else:
            assert r["fwd_10"] is None or not math.isfinite(r["fwd_10"]) or df["date"].iloc[i + 10] <= last_known


# --- 5.3 calibration honest about dependence --------------------------------------------------------------

def test_5_3_intervals_use_separate_periods_not_overlapping_cases():
    lo, hi = signalbook.effective_interval(60, 100, 10)
    assert (lo, hi) == pytest.approx(signalbook.wilson(6.0, 10))
    naive = signalbook.wilson(60, 100)
    assert hi - lo > 2 * (naive[1] - naive[0])                                    # much wider: ten periods, not 100 cases
    assert signalbook.effective_interval(60, 100, 500) == pytest.approx(naive)      # never narrower than the naive one
    # Twenty stocks firing on the same five dates are five periods of evidence, not a hundred.
    rows = [{"date": f"2026-0{1 + m}-15", "fwd_10": 0.02 if j % 3 else -0.01, "spy_10": 0.0, "high_20": 0.03, "low_20": -0.02}
            for m in range(5) for j in range(20)]
    s = signalbook.summarize(rows, +1)
    assert s["cases"] == 100 and s["independent_periods"] == 5
    assert s["frequency_high"] - s["frequency_low"] > s["naive_high"] - s["naive_low"]
    assert "not a forecast" in s["kind"] and "separate periods" in s["interval_method"]


def test_5_3_a_score_that_only_worked_early_is_not_called_calibrated():
    rng = np.random.default_rng(4)
    n = 8000
    net = rng.uniform(-100, 100, n)
    dates = np.tile(pd.bdate_range("2018-01-01", periods=n // 4).to_numpy(), 4)
    late = pd.Series(dates) >= pd.Series(dates).quantile(0.65)
    effect = np.where(late.to_numpy(), -0.0004, 0.0006) * net                      # the relationship reversed lately
    days = pd.DataFrame({"date": dates, "net": net, "regime": "BULL_CALM", "fwd_10": rng.normal(effect, 0.03),
                         "spy_10": 0.0, "fwd_20": np.nan, "spy_20": np.nan, "fwd_5": np.nan, "spy_5": np.nan})
    c = signalbook.calibrate(days)
    assert c["in_sample_calibrated"] and c["calibration_status"] == "CALIBRATED_IN_SAMPLE_ONLY" and not c["calibrated"]
    assert c["holdout"]["confirms"] is False and "held out" in c["verdict"]
    steady = days.assign(fwd_10=rng.normal(0.0006 * net, 0.03))
    s = signalbook.calibrate(steady)
    assert s["calibration_status"] == "CALIBRATED" and s["calibrated"] and s["holdout"]["confirms"]
    assert all(b["independent_periods"] <= b["cases"] for b in s["buckets"])


# --- 5.4 the backtest engine ---------------------------------------------------------------------------------

def test_5_4_walk_forward_choices_never_see_the_test_window():
    frames = {f"S{i}": enrich(random_walk(n=900, seed=40 + i)) for i in range(3)}
    rng = np.random.default_rng(0)
    cal = Backtester(frames).calendar
    signals = (rng.choice([-1, 0, 0, 0, 1], size=(len(cal), 3)), rng.uniform(0, 1, (len(cal), 3)))
    base = BacktestParams(confirmation_days=1)
    grid = {"stop_loss_pct": [0.04, 0.08], "take_profit_pct": [0.10, 0.20]}
    first = walk_forward(frames, base, grid, train_days=252, test_days=63, signal_override=signals, warmup_days=0)
    train_end = pd.Timestamp(first["folds"][0]["train_end"])
    # Scramble every price after the first training window. Its choice must not move.
    scrambled = {}
    for s, df in frames.items():
        df = df.copy()
        after = df["date"] > train_end
        factor = np.exp(np.cumsum(rng.normal(0, 0.05, after.sum())))
        for col in ("open", "high", "low", "close"):
            df.loc[after, col] = df.loc[after, col].to_numpy() * factor
        scrambled[s] = df
    second = walk_forward(scrambled, base, grid, train_days=252, test_days=63, signal_override=signals, warmup_days=0)
    assert second["folds"][0]["chosen"] == first["folds"][0]["chosen"]
    assert second["folds"][0]["train_sharpe"] == pytest.approx(first["folds"][0]["train_sharpe"])
    assert second["folds"][0]["test_return_pct"] != pytest.approx(first["folds"][0]["test_return_pct"])
    for f in first["folds"]:
        assert f["train_end"] < f["test_start"]


def test_5_4_monte_carlo_is_reproducible_with_its_seed():
    daily = np.random.default_rng(9).normal(0.0004, 0.01, 600)
    a = backtest_v2.monte_carlo(daily, 100_000, seed=7)
    b = backtest_v2.monte_carlo(daily, 100_000, seed=7)
    c = backtest_v2.monte_carlo(daily, 100_000, seed=8)
    assert a == b and a != c


def test_5_4_position_sizing_stays_inside_the_account():
    frames = {f"S{i}": enrich(random_walk(n=300, seed=60 + i)) for i in range(12)}
    cal = Backtester(frames).calendar
    always = (np.ones((len(cal), 12), dtype=int), np.ones((len(cal), 12)))
    res = Backtester(frames, always).run(BacktestParams(position_size_pct=0.2, max_positions=3, confirmation_days=1))
    held = {}
    for t in res["trades"]:
        for d in pd.bdate_range(t["entry_date"], t["exit_date"]):
            held.setdefault(d, set()).add(t["symbol"])
    assert max(len(v) for v in held.values()) <= 3                                 # never more than max_positions
    assert all(t["shares"] * t["entry_price"] <= 0.2 * 100_000 * 1.5 for t in res["trades"])
    assert min(p["equity"] for p in res["equity_curve"]) > 0


# --- 5.1 journal analytics ---------------------------------------------------------------------------------

def closed(pnl=100.0, financing=0.0, risk=None, opened="2026-09-01T14:00:00+00:00", **kw):
    pos = {"id": 1, "symbol": "X", "instrument_type": "STOCK", "direction": "SHORT", "bias": -1, "status": "CLOSED",
           "opened_at": opened, "closed_at": "2026-09-10T20:00:00+00:00", "setup_type": "", "target_1": None,
           "invalidation_price": None, "snapshots": [], "executions": [{"side": "SELL_SHORT", "price": 50.0}],
           "ledger": {"realized_pnl": pnl, "fees": 0.0, "initial_risk": risk, "lifecycle_issues": []},
           "financing": {"financing_total": financing, "borrow_status": "RECORDED"}}
    pos.update(kw)
    return pos


def test_5_1_a_trade_is_one_position_and_its_result_is_net_of_financing():
    r = analytics.trade_row(closed(320.0, -51.25, risk=500.0), {"available": False}, 1_000)
    assert r["pnl"] == pytest.approx(268.75) and r["trading_pnl"] == 320 and r["financing"] == pytest.approx(-51.25)
    assert r["r_multiple"] == pytest.approx(268.75 / 500)
    j = analytics.journal_stats([r])
    assert "One closed position" in j["definitions"]["trade"] and j["overall"]["total"] == pytest.approx(268.75)


def test_5_1_habits_that_could_not_be_checked_are_left_out_not_counted_as_absent():
    checked = closed(-300.0, invalidation_price=55.0, id=1)
    unchecked = closed(500.0, invalidation_price=55.0, id=2)
    exc_yes = {"available": True, "closed_beyond_invalidation": True, "reached_target_1": False}
    rows = [analytics.trade_row(checked, exc_yes, 1_000), analytics.trade_row(unchecked, {"available": False}, 1_000)]
    assert rows[0]["behaviour_checks"]["held_past_invalidation"] is True
    assert rows[1]["behaviour_checks"]["held_past_invalidation"] is None           # no price history: unknown
    habit = next(b for b in analytics.journal_stats(rows)["behaviour"] if b["key"] == "held_past_invalidation")
    assert habit["with"]["trades"] == 1 and habit["without"]["trades"] == 0 and habit["not_checked"] == 1
    # Habits that need a plan level are unknown without one, not "did not happen".
    no_plan = analytics.behaviour_detail(closed(), exc_yes, 1_000)
    assert no_plan["held_past_invalidation"] is None and no_plan["exited_before_target"] is None
    assert no_plan["against_the_trend"] is None and no_plan["oversized"] is None and no_plan["overrode_a_limit"] is None
    assert no_plan["added_to_a_loser"] is False                                     # known from the fills alone


def test_5_1_unknown_borrow_costs_are_named():
    pos = closed()
    pos["financing"] = {"financing_total": 0.0, "borrow_status": "UNKNOWN"}
    r = analytics.trade_row(pos, {"available": False}, 1_000)
    j = analytics.journal_stats([r])
    assert any("borrow fees not recorded" in n for n in j["notes"]) and j["coverage"]["borrow_known"] == 0


# --- 5.5 reproducible runs -----------------------------------------------------------------------------------

def test_5_5_runs_are_fingerprinted_and_repeatable(tmp_path):
    settings = Settings(data_mode="demo", db_path=tmp_path / "t.db")
    c = TestClient(create_app(settings, TradeAssistant(settings, provider=DemoData(), db=Database(":memory:"))))
    body = {"symbols": ["AAPL", "MSFT"], "years": 2, "preset": "trend_following", "monte_carlo": True}
    a = c.post("/v2/backtest", json=body).json()["run"]
    b = c.post("/v2/backtest", json=body).json()["run"]
    assert a["id"] != b["id"] and (a["config_hash"], a["data_hash"], a["result_hash"]) == \
        (b["config_hash"], b["data_hash"], b["result_hash"])
    assert a["seed"] == backtest_v2.MC_SEED and a["versions"]["app"]
    diff = c.get("/v2/research/runs/compare", params={"a": a["id"], "b": b["id"]}).json()
    assert diff["same_result"] and diff["same_inputs"] and diff["verdict"] == "Same result."
    other = c.post("/v2/backtest", json={**body, "params": {"stop_loss_pct": 0.03}}).json()["run"]
    d2 = c.get("/v2/research/runs/compare", params={"a": a["id"], "b": other["id"]}).json()
    assert not d2["same_inputs"] and any(ch["field"] == "config.params.stop_loss_pct" for ch in d2["config_changes"])
    listed = c.get("/v2/research/runs", params={"kind": "BACKTEST_V2"}).json()["runs"]
    assert [r["id"] for r in listed][:3] == [other["id"], b["id"], a["id"]]
    one = c.get(f"/v2/research/runs/{a['id']}").json()
    assert one["fingerprint"]["data"]["symbols"]["AAPL"]["bars"] > 0
    assert c.get("/v2/research/runs/99999").status_code == 404


def test_5_5_a_data_change_is_traced_to_the_symbol():
    df = random_walk(n=200, seed=2)
    fa = runs.fingerprint("X", {"p": 1}, {"A": df, "B": df}, {"r": 1}, "demo")
    changed = df.copy()
    changed.loc[150, "close"] *= 1.01
    fb = runs.fingerprint("X", {"p": 1}, {"A": df, "B": changed}, {"r": 2}, "demo")
    cmp = runs.compare({"id": 1, "result_hash": fa["result_hash"], "fingerprint": fa},
                       {"id": 2, "result_hash": fb["result_hash"], "fingerprint": fb})
    assert [x["symbol"] for x in cmp["data_changes"]] == ["B"] and not cmp["config_changes"]
    assert "price history changed for 1 symbol" in cmp["verdict"]
    assert runs.digest({"a": 1.0, "run_at": "x"}) == runs.digest({"run_at": "y", "a": 1.0000000000001})
