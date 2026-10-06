import numpy as np
import pandas as pd
import pytest

from tradeassist import indicators as ind
from tests.conftest import make_bars, random_walk

# The worked RSI example published by StockCharts (Wilder's method, 14 periods).
STOCKCHARTS_CLOSES = [44.34, 44.09, 44.15, 43.61, 44.33, 44.83, 45.10, 45.42, 45.84, 46.08, 45.89, 46.03, 45.61,
                      46.28, 46.28, 46.00, 46.03, 46.41, 46.22, 45.64, 46.21, 46.25, 45.71, 46.45, 45.78, 45.35,
                      44.03, 44.18, 44.22, 44.57, 43.42, 42.66, 43.13]


def reference_rsi(closes, period=14):
    """Plain-loop Wilder RSI, written independently of the module under test."""
    out = [float("nan")] * len(closes)
    gains = [max(closes[i] - closes[i - 1], 0.0) for i in range(1, len(closes))]
    losses = [max(closes[i - 1] - closes[i], 0.0) for i in range(1, len(closes))]
    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period
    out[period] = 100.0 - 100.0 / (1.0 + avg_gain / avg_loss)
    for i in range(period, len(gains)):
        avg_gain = (avg_gain * (period - 1) + gains[i]) / period
        avg_loss = (avg_loss * (period - 1) + losses[i]) / period
        out[i + 1] = 100.0 - 100.0 / (1.0 + avg_gain / avg_loss)
    return out


def reference_adx(high, low, close, period=14):
    """Plain-loop Wilder ADX using running sums, as in Wilder's book."""
    n = len(close)
    tr, pdm, mdm = [0.0] * n, [0.0] * n, [0.0] * n
    for i in range(1, n):
        tr[i] = max(high[i] - low[i], abs(high[i] - close[i - 1]), abs(low[i] - close[i - 1]))
        up, down = high[i] - high[i - 1], low[i - 1] - low[i]
        pdm[i] = up if up > down and up > 0 else 0.0
        mdm[i] = down if down > up and down > 0 else 0.0
    tr_s, p_s, m_s = sum(tr[1:period + 1]), sum(pdm[1:period + 1]), sum(mdm[1:period + 1])
    dx = [float("nan")] * n
    for i in range(period, n):
        if i > period:
            tr_s = tr_s - tr_s / period + tr[i]
            p_s = p_s - p_s / period + pdm[i]
            m_s = m_s - m_s / period + mdm[i]
        pdi, mdi = 100 * p_s / tr_s, 100 * m_s / tr_s
        dx[i] = 100 * abs(pdi - mdi) / (pdi + mdi)
    adx = [float("nan")] * n
    first = 2 * period - 1
    adx[first] = sum(dx[period:first + 1]) / period
    for i in range(first + 1, n):
        adx[i] = (adx[i - 1] * (period - 1) + dx[i]) / period
    return adx


def test_rsi_matches_published_example():
    rsi = ind.rsi(pd.Series(STOCKCHARTS_CLOSES))
    assert rsi.iloc[:14].isna().all()
    assert rsi.iloc[14] == pytest.approx(70.46, abs=0.05)    # first value of the published example
    assert rsi.iloc[-1] == pytest.approx(37.8, abs=0.3)


def test_rsi_matches_independent_reference():
    closes = random_walk(300, seed=7)["close"].tolist()
    got = ind.rsi(pd.Series(closes)).to_numpy()
    want = np.array(reference_rsi(closes))
    np.testing.assert_allclose(got[14:], want[14:], rtol=1e-9)


def test_rsi_extremes():
    assert ind.rsi(pd.Series(np.arange(1.0, 40.0))).iloc[-1] == pytest.approx(100.0)
    assert ind.rsi(pd.Series(np.arange(40.0, 1.0, -1.0))).iloc[-1] == pytest.approx(0.0)
    assert ind.rsi(pd.Series([10.0] * 40)).iloc[-1] == pytest.approx(50.0)


def test_adx_matches_independent_reference():
    df = random_walk(400, seed=3)
    got, _, _ = ind.adx(df["high"], df["low"], df["close"])
    want = np.array(reference_adx(df["high"].tolist(), df["low"].tolist(), df["close"].tolist()))
    np.testing.assert_allclose(got.to_numpy()[27:], want[27:], rtol=1e-9)
    assert got.iloc[:27].isna().all()


def test_adx_is_high_in_a_steady_trend_and_bounded():
    closes = np.linspace(100, 200, 120)
    df = make_bars(closes, highs=closes + 0.5, lows=closes - 0.5)
    adx, plus_di, minus_di = ind.adx(df["high"], df["low"], df["close"])
    assert adx.iloc[-1] > 60
    assert plus_di.iloc[-1] > minus_di.iloc[-1]
    assert adx.dropna().between(0, 100).all()


def test_macd_is_masked_during_warm_up_and_signs_follow_trend():
    up = pd.Series(np.linspace(100, 160, 120))
    line, signal, hist = ind.macd(up)
    assert line.iloc[:34].isna().all() and not np.isnan(line.iloc[34])     # 26 + 9 - 1 warm-up bars
    assert line.iloc[-1] > 0
    down = pd.Series(np.linspace(160, 100, 120))
    assert ind.macd(down)[0].iloc[-1] < 0


def test_realized_vol_recovers_known_volatility():
    rng = np.random.default_rng(0)
    closes = 100 * np.exp(np.cumsum(0.02 * rng.standard_normal(5000)))
    hv = ind.realized_vol(pd.Series(closes), 4000).iloc[-1]
    assert hv == pytest.approx(0.02 * np.sqrt(252), rel=0.05)


def test_range_position_endpoints():
    closes = np.concatenate([np.linspace(100, 200, 260), [200.0]])
    df = make_bars(closes)
    assert ind.range_position(df["high"], df["low"], df["close"]).iloc[-1] == pytest.approx(100.0)


def test_enrich_never_uses_future_bars(walk):
    """Indicators at row t must be identical whether or not later bars exist."""
    full = ind.enrich(walk)
    cut = 600
    partial = ind.enrich(walk.iloc[:cut])
    cols = [c for c in full.columns if c != "date"]
    pd.testing.assert_frame_equal(full.iloc[:cut][cols], partial[cols], check_exact=False, rtol=1e-12)
