import numpy as np
import pandas as pd
import pytest

from tradeassist import eventstudy as es
from tradeassist.indicators import enrich
from tradeassist.patterns import PATTERNS, Pattern, detect_all, recent_events
from tests.conftest import make_bars


def test_forward_return_enters_next_open_and_exits_h_closes_later():
    bars = make_bars([10, 11, 12, 13, 14, 15], opens=[10, 10.5, 11.5, 12.5, 13.5, 14.5])
    fwd = es.forward_returns(bars, 2)
    assert fwd.iloc[0] == pytest.approx(12 / 10.5 - 1)      # signal on bar 0: buy bar 1's open, sell bar 2's close
    assert fwd.iloc[3] == pytest.approx(15 / 13.5 - 1)
    assert fwd.iloc[4:].isna().all()                         # not enough future bars: no return, no guess


def test_benjamini_hochberg_known_values():
    q = es.benjamini_hochberg([0.01, 0.04, 0.03, 0.005, None])
    assert q[:4] == pytest.approx([0.02, 0.04, 0.04, 0.02])
    assert q[4] is None
    assert es.benjamini_hochberg([None, None]) == [None, None]


def planted(monkeypatch, drift_after_event):
    """Random walks with a marker pattern every 15 bars, optionally followed by a planted drift."""
    frames = {}
    for i in range(8):
        rng = np.random.default_rng(100 + i)
        n = 1200
        rets = 0.012 * rng.standard_normal(n)
        marks = np.zeros(n, dtype=bool)
        marks[np.arange(300 + i, n - 30, 15)] = True
        for t in np.flatnonzero(marks):
            rets[t + 1:t + 6] += drift_after_event
        closes = 100 * np.exp(np.cumsum(rets))
        df = make_bars(closes, start="2020-01-02")
        df["marker"] = marks
        frames[f"S{i}"] = df
    pattern = Pattern("planted", "Planted marker", +1, "test pattern", lambda df: df["marker"])
    monkeypatch.setitem(PATTERNS, "planted", pattern)
    return frames


def test_a_real_effect_is_found_and_marked_supported(monkeypatch):
    frames = planted(monkeypatch, drift_after_event=0.006)
    study = es.run_study(frames, horizons=[5], cost_bps=5, pattern_keys=["planted"])
    r = study["results"][0]
    assert r["verdict"] == "supported"
    assert r["mean_excess"] > 0.015 and r["out_of_sample_excess"] > 0 and r["q_value"] < 0.01
    assert r["n_events"] > 400 and r["blocks_in_sample"] >= es.MIN_BLOCKS
    assert r["t_in_sample"] > 3 and r["t_out_of_sample"] > 3 and r["p_out_of_sample"] < 0.01
    assert study["verdicts_given"] and "other symbols" in study["benchmark"]


def test_no_effect_is_not_called_supported(monkeypatch):
    frames = planted(monkeypatch, drift_after_event=0.0)
    r = es.run_study(frames, horizons=[5], pattern_keys=["planted"])["results"][0]
    assert r["verdict"] == "no_evidence"


def test_an_effect_in_the_wrong_direction_is_contradicted(monkeypatch):
    frames = planted(monkeypatch, drift_after_event=-0.006)
    r = es.run_study(frames, horizons=[5], pattern_keys=["planted"])["results"][0]
    assert r["verdict"] == "contradicted" and r["mean_excess"] < 0


def test_too_few_cases_is_insufficient(monkeypatch):
    frames = planted(monkeypatch, drift_after_event=0.01)
    for df in frames.values():
        keep = np.flatnonzero(df["marker"].to_numpy())[:2]
        df["marker"] = False
        df.loc[keep, "marker"] = True
    r = es.run_study(frames, horizons=[5], pattern_keys=["planted"])["results"][0]
    assert r["verdict"] == "insufficient"


def noise_frames(seed, n_symbols=6, n=1000):
    """Stocks that share a market factor and drift but have no pattern effects at all."""
    rng = np.random.default_rng(seed)
    market = 0.0004 + 0.009 * rng.standard_normal(n)
    frames = {}
    for i in range(n_symbols):
        closes = 100 * np.exp(np.cumsum(rng.uniform(0.7, 1.3) * market + 0.012 * rng.standard_normal(n)))
        opens = np.concatenate([[100.0], closes[:-1]]) * np.exp(0.003 * rng.standard_normal(n))
        highs = np.maximum(opens, closes) * (1 + np.abs(0.006 * rng.standard_normal(n)))
        lows = np.minimum(opens, closes) * (1 - np.abs(0.006 * rng.standard_normal(n)))
        frames[f"S{i}"] = enrich(make_bars(closes, start="2019-01-02", opens=opens, highs=highs, lows=lows))
    return frames


def test_noise_gives_no_verdicts_and_no_bias_by_pattern_family():
    """On data with no pattern effects, breakout patterns must not look systematically bad nor
    mean-reversion patterns good. An earlier baseline (each stock's own average return) failed
    this: new 52-week highs averaged a t-statistic near -1.35 at 20 days."""
    keys = ["new_52w_high", "new_52w_low", "rsi_leaves_oversold", "enters_low_zone", "macd_bull_cross"]
    t_stats = {k: [] for k in keys}
    verdicts = []
    for seed in range(24):
        study = es.run_study(noise_frames(seed), horizons=[20], pattern_keys=keys)
        for r in study["results"]:
            verdicts.append(r["verdict"])
            if r["t_in_sample"] is not None:
                t_stats[r["pattern"]].append(r["t_in_sample"])
    assert "supported" not in verdicts and "contradicted" not in verdicts
    for key, values in t_stats.items():
        assert len(values) >= 12
        assert abs(np.mean(values)) < 0.6, (key, np.mean(values))          # centred near zero (24 small samples)
        assert np.mean(np.abs(values) > 2.0) < 0.20, key                    # and rarely looks significant


def test_fewer_than_five_symbols_get_numbers_against_the_benchmark_but_no_verdict(monkeypatch):
    frames = dict(list(planted(monkeypatch, drift_after_event=0.006).items())[:3])
    with pytest.raises(ValueError, match="at least 5 symbols"):
        es.run_study(frames, horizons=[5], pattern_keys=["planted"])
    benchmark = make_bars(100 * np.exp(np.cumsum(np.full(1200, 0.0002))), start="2020-01-02")
    study = es.run_study(frames, horizons=[5], pattern_keys=["planted"], benchmark=benchmark, benchmark_label="SPY")
    r = study["results"][0]
    assert study["benchmark"] == "SPY" and not study["verdicts_given"]
    assert r["verdict"] == "insufficient" and "at least 5 symbols" in r["verdict_text"]
    assert r["mean_excess"] > 0.015                                         # the numbers are still reported


def test_discovery_events_that_reach_into_the_held_out_period_are_dropped(monkeypatch):
    frames = planted(monkeypatch, drift_after_event=0.0)
    r = es.run_study(frames, horizons=[20], pattern_keys=["planted"])["results"][0]
    assert r["n_in_sample"] + r["n_out_of_sample"] < r["n_events"]


def test_block_test_matches_a_plain_t_test_when_blocks_are_independent():
    rng = np.random.default_rng(3)
    excess = pd.Series(0.01 + 0.05 * rng.standard_normal(60))
    blocks = pd.Series(np.arange(60) * 2)                                   # no neighbouring blocks: no widening
    got = es.block_test(excess, blocks)
    from scipy import stats
    want = stats.ttest_1samp(excess, 0.0)
    assert got["t_stat"] == pytest.approx(want.statistic) and got["p_value"] == pytest.approx(want.pvalue)
    assert es.block_test(excess[:2], blocks[:2])["t_stat"] is None


def test_detectors_do_not_use_future_bars(walk):
    full = detect_all(enrich(walk))
    partial = detect_all(enrich(walk.iloc[:650]))
    pd.testing.assert_frame_equal(full.iloc[:650], partial)


def test_macd_cross_fires_once_on_the_crossing_bar():
    closes = np.concatenate([np.linspace(120, 100, 80), np.linspace(100, 125, 60)])
    df = enrich(make_bars(closes))
    hits = np.flatnonzero(PATTERNS["macd_bull_cross"].detect(df).to_numpy())
    assert len(hits) == 1 and 80 <= hits[0] <= 90
    i = hits[0]
    assert df["macd"].iloc[i] > df["macd_signal"].iloc[i] and df["macd"].iloc[i - 1] <= df["macd_signal"].iloc[i - 1]


def test_candle_patterns_detect_textbook_shapes():
    closes = list(np.linspace(110, 100, 15)) + [100.5]
    opens = [closes[0]] + closes[:-1]
    opens[-1] = 100.3
    highs = [max(o, c) + 0.05 for o, c in zip(opens, closes)]
    lows = [min(o, c) - 0.05 for o, c in zip(opens, closes)]
    lows[-1] = 98.0                                          # long lower shadow after a decline
    df = enrich(make_bars(closes, opens=opens, highs=highs, lows=lows))
    assert PATTERNS["hammer"].detect(df).iloc[-1]
    assert not PATTERNS["shooting_star"].detect(df).iloc[-1]
    events = recent_events(df, lookback=3)
    assert any(e["key"] == "hammer" and e["bars_ago"] == 0 for e in events)
