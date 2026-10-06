import numpy as np
import pandas as pd
import pytest

from tradeassist.backtest import (Backtester, BacktestParams, deflated_sharpe, expected_max_sharpe, max_drawdown,
                                  probabilistic_sharpe, run_backtest, sharpe_daily, walk_forward)
from tradeassist.indicators import enrich
from tests.conftest import make_bars, random_walk

FREE = dict(slippage_bps=0.0, commission=0.0, position_size_pct=0.5, max_positions=1, cooldown_days=0,
            holding_period_days=1000)


def engine(closes, signal_day, direction, **bar_kwargs):
    """One-symbol backtester with a single hand-placed signal at the close of `signal_day`."""
    bars = make_bars(closes, **bar_kwargs)
    n = len(bars)
    d = np.zeros((n, 1), dtype=int)
    d[signal_day, 0] = direction
    return Backtester({"TEST": bars}, signal_override=(d, np.abs(d).astype(float)))


def test_long_trade_enters_next_open_and_profits_from_a_rise():
    closes = [100, 100, 100, 104, 108, 112]
    bt = engine(closes, signal_day=1, direction=1)
    res = bt.run(BacktestParams(stop_loss_pct=0.5, take_profit_pct=5.0, **FREE))
    trade = res["trades"][0]
    assert trade["entry_date"] == "2024-01-04"                 # signal on day index 1, entry on day index 2
    assert trade["entry_price"] == 100 and trade["exit_price"] == 112
    assert trade["shares"] == 500                               # 50% of $100,000 at $100
    assert trade["pnl"] == pytest.approx(6000)
    assert res["final_capital"] == pytest.approx(106000)
    assert trade["exit_reason"] == "END_OF_TEST"


def test_short_trade_profits_when_price_falls():
    """The original backtester lost money on winning shorts. A falling price must be a gain."""
    closes = [100, 100, 100, 96, 92, 90]
    res = engine(closes, 1, -1).run(BacktestParams(stop_loss_pct=0.5, take_profit_pct=0.9, **FREE))
    trade = res["trades"][0]
    assert trade["direction"] == "SHORT"
    assert trade["pnl"] == pytest.approx(500 * 10)
    assert res["final_capital"] == pytest.approx(105000)
    equity = [p["equity"] for p in res["equity_curve"]]
    assert equity == sorted(equity)                             # equity rises as the short works


def test_short_trade_loses_when_price_rises():
    closes = [100, 100, 100, 103, 104, 104]
    res = engine(closes, 1, -1).run(BacktestParams(stop_loss_pct=0.5, take_profit_pct=0.9, **FREE))
    assert res["trades"][0]["pnl"] == pytest.approx(-2000)
    assert res["final_capital"] == pytest.approx(98000)


def test_stop_loss_fills_at_the_stop_price_intraday():
    closes = [100, 100, 100, 99, 97, 99]
    lows = [100, 100, 100, 98, 94, 97]
    res = engine(closes, 1, 1, lows=lows).run(BacktestParams(stop_loss_pct=0.05, take_profit_pct=0.5, **FREE))
    trade = res["trades"][0]
    assert trade["exit_reason"] == "STOP_LOSS" and trade["exit_price"] == pytest.approx(95.0)
    assert trade["exit_date"] == "2024-01-08"


def test_gap_through_stop_fills_at_the_open_not_the_stop():
    closes = [100, 100, 100, 100, 88, 90]
    opens = [100, 100, 100, 100, 90, 88]
    res = engine(closes, 1, 1, opens=opens).run(BacktestParams(stop_loss_pct=0.05, take_profit_pct=0.5, **FREE))
    trade = res["trades"][0]
    assert trade["exit_reason"] == "STOP_LOSS_GAP" and trade["exit_price"] == pytest.approx(90.0)
    assert trade["pnl"] == pytest.approx(-5000)


def test_stop_is_assumed_first_when_stop_and_target_hit_the_same_day():
    closes = [100, 100, 100, 100, 100, 100]
    highs = [100, 100, 100, 100, 120, 100]
    lows = [100, 100, 100, 100, 90, 100]
    res = engine(closes, 1, 1, highs=highs, lows=lows).run(
        BacktestParams(stop_loss_pct=0.05, take_profit_pct=0.15, **FREE))
    assert res["trades"][0]["exit_reason"] == "STOP_LOSS"


def test_take_profit_for_shorts_and_longs():
    closes = [100, 100, 100, 100, 100, 100]
    lows = [100, 100, 100, 100, 80, 100]
    res = engine(closes, 1, -1, lows=lows).run(BacktestParams(stop_loss_pct=0.05, take_profit_pct=0.15, **FREE))
    trade = res["trades"][0]
    assert trade["exit_reason"] == "TAKE_PROFIT" and trade["exit_price"] == pytest.approx(85.0)
    assert trade["pnl"] == pytest.approx(500 * 15)
    highs = [100, 100, 100, 100, 120, 100]
    res = engine(closes, 1, 1, highs=highs).run(BacktestParams(stop_loss_pct=0.05, take_profit_pct=0.15, **FREE))
    trade = res["trades"][0]
    assert trade["exit_reason"] == "TAKE_PROFIT" and trade["exit_price"] == pytest.approx(115.0)
    assert trade["pnl"] == pytest.approx(500 * 15)


def test_gap_above_target_fills_at_the_open_even_if_the_stop_trades_later():
    """The open comes first. Long from 100, target 115, stop 95: a day that opens at 118 and
    later dips to 94 was already sold at 118."""
    closes = [100, 100, 100, 100, 110, 110]
    opens = [100, 100, 100, 100, 118, 110]
    highs = [100, 100, 100, 100, 119, 110]
    lows = [100, 100, 100, 100, 94, 110]
    res = engine(closes, 1, 1, opens=opens, highs=highs, lows=lows).run(
        BacktestParams(stop_loss_pct=0.05, take_profit_pct=0.15, **FREE))
    trade = res["trades"][0]
    assert trade["exit_reason"] == "TAKE_PROFIT_GAP" and trade["exit_price"] == pytest.approx(118.0)
    assert trade["pnl"] == pytest.approx(500 * 18)


def test_position_is_closed_when_a_symbols_data_ends():
    """A symbol whose bars stop must not strand its position and capital for the rest of the test."""
    long_lived = make_bars([50.0] * 20)
    short_lived = make_bars([100.0] * 8 + [104.0] * 4)                     # 12 bars, then nothing
    direction = np.zeros((20, 2), dtype=int)
    direction[1, 1] = 1                                                     # column order is sorted: A, B
    bt = Backtester({"A": long_lived, "B": short_lived}, signal_override=(direction, np.abs(direction).astype(float)))
    res = bt.run(BacktestParams(stop_loss_pct=0.5, take_profit_pct=5.0, **FREE))
    trade = res["trades"][0]
    assert res["num_trades"] == 1 and trade["symbol"] == "B" and trade["exit_reason"] == "DATA_ENDED"
    assert trade["exit_price"] == pytest.approx(104.0) and trade["pnl"] == pytest.approx(500 * 4)
    assert res["final_capital"] == pytest.approx(100000 + trade["pnl"])
    assert res["equity_curve"][-1]["equity"] == pytest.approx(102000)


def test_benchmark_is_a_true_buy_and_hold_from_the_first_open():
    a = make_bars([100, 110, 121, 121], opens=[100, 100, 110, 121])       # +21% from its first open
    b = make_bars([50, 50, 45, 40], opens=[50, 50, 50, 45])               # -20%
    direction = np.zeros((4, 2), dtype=int)
    res = Backtester({"A": a, "B": b}, signal_override=(direction, direction.astype(float))).run(
        BacktestParams(**FREE))
    bench = res["benchmark"]
    assert bench["total_return_pct"] == pytest.approx(0.5)                # (21% - 20%) / 2, never rebalanced
    assert "Buy and hold" in bench["label"]
    assert bench["equity"][0] == pytest.approx(50000 * 1.0 + 50000 * 1.0)  # day one: bought at the open, flat close
    assert bench["equity"][1] == pytest.approx(50000 * 1.10 + 50000 * 1.0)


def test_costs_reduce_profit():
    closes = [100, 100, 100, 104, 108, 112]
    params = BacktestParams(stop_loss_pct=0.5, take_profit_pct=5.0, **{**FREE, "slippage_bps": 10.0, "commission": 1.0})
    trade = engine(closes, 1, 1).run(params)["trades"][0]
    assert trade["entry_price"] == pytest.approx(100.1) and trade["exit_price"] == pytest.approx(112 * 0.999)
    assert trade["pnl"] == pytest.approx(trade["shares"] * (112 * 0.999 - 100.1) - 2.0)


def test_holding_period_exit_and_no_shorts_option():
    closes = [100] * 12
    params = BacktestParams(stop_loss_pct=0.5, take_profit_pct=5.0, **{**FREE, "holding_period_days": 5})
    trade = engine(closes, 1, 1).run(params)["trades"][0]
    assert trade["exit_reason"] == "HOLDING_PERIOD" and trade["days_held"] >= 5
    res = engine(closes, 1, -1).run(BacktestParams(allow_shorts=False, **FREE))
    assert res["num_trades"] == 0 and res["final_capital"] == 100000


def test_equity_curve_has_trading_days_only():
    res = engine([100] * 30, 1, 1).run(BacktestParams(stop_loss_pct=0.5, take_profit_pct=5.0, **FREE))
    dates = pd.to_datetime([p["date"] for p in res["equity_curve"]])
    assert (dates.dayofweek < 5).all() and len(dates) == 30
    assert all(p["equity"] == pytest.approx(100000) for p in res["equity_curve"])   # no weekend dips to cash


def test_max_drawdown_is_relative_to_running_peak():
    assert max_drawdown(np.array([100, 120, 90, 130, 117])) == pytest.approx(-0.25)
    assert max_drawdown(np.array([100, 110, 120])) == 0.0


def test_sharpe_and_significance_statistics():
    rng = np.random.default_rng(5)
    r = 0.001 + 0.01 * rng.standard_normal(1000)
    sr = sharpe_daily(r)
    assert sr == pytest.approx(r.mean() / r.std(ddof=1))
    assert sharpe_daily(np.zeros(100)) is None and sharpe_daily(r[:5]) is None
    assert probabilistic_sharpe(0.0, 500, 0.0, 3.0) == pytest.approx(0.5)
    assert expected_max_sharpe(1, 0.01) == 0.0
    assert expected_max_sharpe(100, 0.01) > expected_max_sharpe(10, 0.01) > 0
    one = deflated_sharpe(r, 1)
    many = deflated_sharpe(r, 500)
    assert one["deflated_sharpe"] == pytest.approx(one["prob_sharpe_positive"])
    assert many["deflated_sharpe"] < one["deflated_sharpe"]          # more trials, less credit
    assert many["prob_sharpe_positive"] == pytest.approx(one["prob_sharpe_positive"])


def test_full_run_on_random_walks_is_consistent():
    frames = {f"S{i}": enrich(random_walk(700, seed=i)) for i in range(4)}
    res = run_backtest(frames, BacktestParams())
    assert len(res["equity_curve"]) == 700 and res["num_trades"] > 20
    # Every dollar of the final balance is explained by the trades (costs included).
    assert res["final_capital"] == pytest.approx(100000 + sum(t["pnl"] for t in res["trades"]), rel=1e-9)
    assert all("benchmark" in p for p in res["equity_curve"])
    by_symbol = {}
    for t in res["trades"]:
        assert t["entry_date"] <= t["exit_date"]
        by_symbol.setdefault(t["symbol"], []).append(t)
    for trades in by_symbol.values():                                      # never two positions in one symbol
        trades.sort(key=lambda t: t["entry_date"])
        for first, second in zip(trades, trades[1:]):
            assert first["exit_date"] < second["entry_date"]


def test_signals_do_not_see_the_future():
    """Changing prices after a cutoff must not change any trade entered before it."""
    base = {f"S{i}": random_walk(700, seed=10 + i) for i in range(3)}
    altered = {}
    for name, df in base.items():
        df2 = df.copy()
        df2.loc[500:, ["open", "high", "low", "close"]] *= 1.5
        altered[name] = df2
    cutoff = base["S0"]["date"].iloc[499].strftime("%Y-%m-%d")
    run = lambda frames: run_backtest({s: enrich(f) for s, f in frames.items()}, BacktestParams(), end=cutoff)
    a, b = run(base), run(altered)
    assert a["trades"] == b["trades"] and a["final_capital"] == b["final_capital"]


def test_walk_forward_runs_and_reports_only_held_out_windows():
    frames = {f"S{i}": enrich(random_walk(900, seed=20 + i)) for i in range(4)}
    wf = walk_forward(frames, BacktestParams(), grid={"stop_loss_pct": [0.04, 0.08], "min_score": [0.4, 0.5]},
                      train_days=200, test_days=60)
    assert wf["combinations_tried"] == 4 and len(wf["folds"]) == (900 - 260 - 200) // 60
    assert wf["out_of_sample"]["trading_days"] == 60 * len(wf["folds"])
    assert wf["significance"]["n_trials"] == 1                             # held-out returns carry no trial penalty

    # Leakage check: wreck every price from the second fold's test window onward. The parameters
    # chosen for the first two folds, and their training Sharpe, must not change.
    cut = pd.Timestamp(wf["folds"][1]["test_start"])
    damaged = {}
    for name in frames:
        raw = random_walk(900, seed=20 + int(name[1:]))
        late = raw["date"] >= cut
        raw.loc[late, ["open", "high", "low", "close"]] = raw.loc[late, ["open", "high", "low", "close"]].to_numpy()[::-1] * 0.7
        raw.loc[late, "high"] = raw.loc[late, ["open", "high", "low", "close"]].max(axis=1)
        raw.loc[late, "low"] = raw.loc[late, ["open", "low", "close"]].min(axis=1)
        damaged[name] = enrich(raw)
    wf2 = walk_forward(damaged, BacktestParams(), grid={"stop_loss_pct": [0.04, 0.08], "min_score": [0.4, 0.5]},
                       train_days=200, test_days=60)
    for before, after in zip(wf["folds"][:2], wf2["folds"][:2]):
        assert before["chosen"] == after["chosen"] and before["train_sharpe"] == after["train_sharpe"]
    assert wf["folds"][0]["test_return_pct"] == wf2["folds"][0]["test_return_pct"]
    assert wf["folds"][1]["test_return_pct"] != wf2["folds"][1]["test_return_pct"]
    with pytest.raises(ValueError):
        walk_forward(frames, BacktestParams(), train_days=600, test_days=200)
    with pytest.raises(ValueError):
        walk_forward(frames, BacktestParams(), grid={"nonsense": [1]})
