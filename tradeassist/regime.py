"""Market regime: what the market as a whole is doing, day by day, and how that changes what
an indicator reading on one stock means.

The regime on a day comes from three things known by that evening:

- SPY's trend state from the trend engine (trend.py), with QQQ as a cross-check: when SPY is
  in an uptrend but QQQ is in a downtrend, the market is called in transition;
- volatility: the VIX close when the data source has VIX history, otherwise how SPY's own
  20-day realized volatility ranks against the past year;
- nothing else. Breadth (how many stocks are above their averages) would need prices for the
  whole market and is not included.

Regimes:

    BULL_CALM       SPY uptrend, volatility calm or normal
    BULL_VOLATILE   SPY uptrend, volatility elevated or stressed
    RANGE           SPY without a trend
    TRANSITION      SPY uptrend weakening or downtrend developing, or SPY and QQQ disagree
    BEAR_CALM       SPY downtrend, volatility calm or normal
    BEAR_VOLATILE   SPY downtrend, volatility elevated or stressed

`interpret` then reads RSI, the 52-week range position and the Bollinger bands of a stock in
the light of that stock's own trend state and the regime: RSI above 70 in an uptrend is
usually strength, not a top; in a range it is more often stretched. These readings explain;
they do not add points to any score.
"""

from __future__ import annotations

from typing import Any

import numpy as np
import pandas as pd

from . import trend as trend_engine
from .versions import REGIME_ENGINE_VERSION

REGIMES = {
    "BULL_CALM": "Bull market, calm", "BULL_VOLATILE": "Bull market, volatile", "RANGE": "Range-bound market",
    "TRANSITION": "Market in transition", "BEAR_CALM": "Bear market, orderly", "BEAR_VOLATILE": "Bear market, stressed",
}
REGIME_TEXT = {
    "BULL_CALM": "SPY is in an uptrend and volatility is low or normal. Pullbacks in leading stocks have tended to be "
                 "bought; breakouts are more likely to hold.",
    "BULL_VOLATILE": "SPY is in an uptrend but volatility is high. Trends can still work, with wider swings: stops "
                     "need more room and position sizes less.",
    "RANGE": "SPY has no clear trend. Moves away from the middle of a range tend to fade; breakouts fail more often.",
    "TRANSITION": "The market's trend is changing or its parts disagree. Signals in either direction are less "
                  "reliable until a direction is confirmed.",
    "BEAR_CALM": "SPY is in a downtrend with ordinary volatility. Rallies tend to fade at resistance; new highs are rare.",
    "BEAR_VOLATILE": "SPY is in a downtrend and volatility is high. Moves are large both ways, including sharp rallies "
                     "that do not last.",
}
VOL_BUCKETS = ("CALM", "NORMAL", "ELEVATED", "STRESSED")
VIX_LIMITS = (15.0, 20.0, 30.0)             # under 15 calm, 15 to 20 normal, 20 to 30 elevated, above stressed
HV_RANK_LIMITS = (0.30, 0.70, 0.90)         # realized-volatility rank against the past year, when there is no VIX
HV_STRESSED = 0.35                          # 35% annualized realized volatility on SPY is stressed whatever its rank


def _vol_bucket(vix: float, hv: float, hv_rank: float) -> tuple[str, str]:
    if np.isfinite(vix):
        k = int(np.searchsorted(VIX_LIMITS, vix, side="right"))
        return VOL_BUCKETS[k], "VIX"
    if np.isfinite(hv) and hv >= HV_STRESSED:
        return "STRESSED", "SPY realized"
    if np.isfinite(hv_rank):
        k = int(np.searchsorted(HV_RANK_LIMITS, hv_rank, side="right"))
        return VOL_BUCKETS[k], "SPY realized"
    return "NORMAL", "none"


def classify(spy_state: str, qqq_state: str | None, bucket: str) -> str:
    rough = bucket in ("ELEVATED", "STRESSED")
    if spy_state in ("STRONG_UP", "UP"):
        if qqq_state in trend_engine.DOWN_SIDE:
            return "TRANSITION"
        return "BULL_VOLATILE" if rough else "BULL_CALM"
    if spy_state in ("STRONG_DOWN", "DOWN"):
        if qqq_state in trend_engine.UP_SIDE and qqq_state != "UP_WEAKENING":
            return "TRANSITION"
        return "BEAR_VOLATILE" if rough else "BEAR_CALM"
    if spy_state in ("UP_WEAKENING", "DOWN_DEVELOPING"):
        return "TRANSITION"
    return "RANGE"


def _aligned(dates: pd.Series, frame: pd.DataFrame | None, column: str) -> np.ndarray:
    if frame is None or frame.empty:
        return np.full(len(dates), np.nan)
    s = frame.set_index(pd.to_datetime(frame["date"]))[column]
    s = s[~s.index.duplicated(keep="last")].sort_index()
    return s.reindex(pd.to_datetime(dates), method="ffill").to_numpy()


def usable_vix(vix: pd.DataFrame | None) -> pd.DataFrame | None:
    """VIX history only when it looks like VIX (closes between 5 and 150, median under 60)."""
    if vix is None or vix.empty or len(vix) < 60:
        return None
    c = vix["close"].astype(float)
    if c.min() < 5 or c.max() > 150 or c.median() > 60:
        return None
    return vix


def regime_series(spy: pd.DataFrame, qqq: pd.DataFrame | None = None, vix: pd.DataFrame | None = None,
                  spy_states: pd.DataFrame | None = None, qqq_states: pd.DataFrame | None = None) -> pd.DataFrame:
    """The regime on each of SPY's days (enriched frames for SPY and QQQ; VIX daily bars). Causal.
    The trend states of SPY and QQQ can be passed in when already worked out."""
    spy_states = trend_engine.state_series(spy) if spy_states is None else spy_states
    if qqq_states is None and qqq is not None and len(qqq) >= 30:
        qqq_states = trend_engine.state_series(qqq)
    qs = (_aligned(spy["date"], qqq_states, "trend_state") if qqq_states is not None
          else np.full(len(spy), None, dtype=object))
    vix = usable_vix(vix)
    vix_close = _aligned(spy["date"], vix, "close").astype(float)
    hv = spy["hv20"].to_numpy(dtype=float)
    hv_rank = trend_engine._rolling_rank(hv)
    rows = []
    for i in range(len(spy)):
        bucket, source = _vol_bucket(vix_close[i], hv[i], hv_rank[i])
        q = qs[i] if isinstance(qs[i], str) else None
        state = spy_states["trend_state"].iloc[i]
        rows.append((state, q, bucket, source, classify(state, q, bucket)))
    out = pd.DataFrame(rows, columns=["spy_state", "qqq_state", "vol_bucket", "vol_source", "regime"], index=spy.index)
    out.insert(0, "date", spy["date"].to_numpy())
    out["spy_net"] = spy_states["net_score"].to_numpy()
    out["vix"] = vix_close
    out["hv20"] = hv
    out["hv_rank"] = hv_rank
    return out


def _etf_view(df: pd.DataFrame, states: pd.DataFrame, i: int) -> dict[str, Any]:
    row = df.iloc[i]
    high = df["high"].iloc[max(0, i - 251): i + 1].max()
    f = lambda a, b: float(100 * (a / b - 1)) if np.isfinite(a) and np.isfinite(b) and b else None
    return {"state": states["trend_state"].iloc[i], "label": trend_engine.STATE_WORDS[states["trend_state"].iloc[i]],
            "net_score": round(float(states["net_score"].iloc[i]), 1) if np.isfinite(states["net_score"].iloc[i]) else None,
            "close": float(row["close"]), "vs_50d_pct": f(row["close"], row["sma50"]),
            "vs_200d_pct": f(row["close"], row["sma200"]), "off_52w_high_pct": f(row["close"], high)}


def analyse(spy: pd.DataFrame, qqq: pd.DataFrame | None = None, vix: pd.DataFrame | None = None,
            history: int = 252) -> dict[str, Any]:
    """Today's regime (on SPY's last finished bar, with the provisional one when today's bar is
    unfinished), the regimes over the past year and the changes between them."""
    if len(spy) < 60:
        raise ValueError("At least 60 daily SPY bars are needed for the market regime")
    spy = spy.reset_index(drop=True)
    qqq = qqq.reset_index(drop=True) if qqq is not None and len(qqq) >= 30 else None
    spy_states = trend_engine.state_series(spy)
    q_states = trend_engine.state_series(qqq) if qqq is not None else None
    series = regime_series(spy, qqq, vix, spy_states, q_states)
    complete = spy["bar_complete"].to_numpy(dtype=bool) if "bar_complete" in spy.columns else np.ones(len(spy), bool)
    done = np.flatnonzero(complete)
    ci = int(done[-1]) if len(done) else len(spy) - 1
    last = len(spy) - 1
    q_dates = pd.to_datetime(qqq["date"]).to_numpy() if qqq is not None else None

    def day(i: int, status: str) -> dict[str, Any]:
        r = series.iloc[i]
        out = {"date": pd.Timestamp(r["date"]).strftime("%Y-%m-%d"), "status": status, "regime": r["regime"],
               "label": REGIMES[r["regime"]], "text": REGIME_TEXT[r["regime"]],
               "spy": _etf_view(spy, spy_states, i),
               "volatility": {"bucket": r["vol_bucket"], "source": r["vol_source"],
                              "vix": float(r["vix"]) if np.isfinite(r["vix"]) else None,
                              "spy_realized_20d": float(r["hv20"]) if np.isfinite(r["hv20"]) else None,
                              "spy_realized_rank": float(r["hv_rank"]) if np.isfinite(r["hv_rank"]) else None}}
        if q_states is not None:
            k = int(np.searchsorted(q_dates, np.datetime64(pd.Timestamp(r["date"])), side="right")) - 1
            if k >= 0:
                out["qqq"] = _etf_view(qqq, q_states, k)
        return out

    confirmed = day(ci, "CONFIRMED")
    provisional = day(last, "PROVISIONAL") if not complete[last] else None
    reg = series["regime"].to_numpy()
    changes = [{"date": pd.Timestamp(series["date"].iloc[i]).strftime("%Y-%m-%d"), "from": reg[i - 1], "to": reg[i]}
               for i in range(1, ci + 1) if reg[i] != reg[i - 1]][-8:][::-1]
    start = max(0, len(spy) - history)
    notes = ["Breadth (how many stocks are above their averages) is not included."]
    if confirmed["volatility"]["source"] != "VIX":
        notes.append("No VIX history from this data source, so volatility is judged from SPY's own realized "
                     "volatility against the past year.")
    current = provisional or confirmed
    return {"version": REGIME_ENGINE_VERSION, "as_of": current["date"], "status": current["status"],
            "regime": current["regime"], "label": current["label"], "confirmed": confirmed, "provisional": provisional,
            "changes": changes, "notes": notes,
            "history": [{"date": pd.Timestamp(d).strftime("%Y-%m-%d"), "regime": g}
                        for d, g in zip(series["date"].iloc[start:], reg[start:])]}


# --- reading indicators in context -------------------------------------------------------------------

def interpret(row: pd.Series, stock_state: str, regime: str, band_width_rank: float | None = None) -> list[dict[str, Any]]:
    """RSI, 52-week position and Bollinger bands, read for this stock's trend and this market."""
    up = stock_state in ("STRONG_UP", "UP")
    down = stock_state in ("STRONG_DOWN", "DOWN")
    bull_mkt = regime in ("BULL_CALM", "BULL_VOLATILE")
    bear_mkt = regime in ("BEAR_CALM", "BEAR_VOLATILE")
    trendless = not up and not down
    out: list[dict[str, Any]] = []

    def add(indicator: str, value: str, lean: str, reading: str) -> None:
        out.append({"indicator": indicator, "value": value, "lean": lean, "reading": reading})

    rsi = float(row.get("rsi", np.nan))
    if np.isfinite(rsi):
        v = f"{rsi:.0f}"
        if rsi >= 70:
            if up:
                add("RSI (14)", v, "bullish", "Overbought in an uptrend. In trends this is usually strength: the common "
                                              "follow-through is a pause or a shallow pullback, not a reversal.")
            elif down:
                add("RSI (14)", v, "bearish", "Overbought in a downtrend: a sharp rally against the trend. Such rallies "
                                              "often fade at resistance.")
            else:
                add("RSI (14)", v, "bearish", "Overbought without a trend: the stock is stretched toward the top of its "
                                              "range, where moves more often fade.")
        elif rsi <= 30:
            if down:
                add("RSI (14)", v, "bearish", "Oversold in a downtrend. In trends this is usually weakness: bounces tend "
                                              "to be short until the structure changes.")
            elif up:
                add("RSI (14)", v, "bullish", "Oversold in an uptrend: a deep pullback. In an uptrend these have more often "
                                              "been bought than not, if the swing structure holds.")
            else:
                add("RSI (14)", v, "bullish", "Oversold without a trend: stretched toward the bottom of its range, where "
                                              "moves more often bounce.")
        elif up and 40 <= rsi <= 50:
            add("RSI (14)", v, "bullish", "In the pullback zone of an uptrend (RSI 40 to 50), where uptrends have "
                                          "often resumed.")
        elif down and 50 <= rsi <= 60:
            add("RSI (14)", v, "bearish", "In the rally zone of a downtrend (RSI 50 to 60), where downtrends have often "
                                          "resumed.")
        else:
            add("RSI (14)", v, "neutral", "In its normal range.")

    pos = float(row.get("pos52", np.nan))
    if np.isfinite(pos):
        v = f"{pos:.0f}% of the range"
        if pos >= 90:
            if bear_mkt:
                add("52-week range", v, "bullish", "Near its 52-week high while the market is in a downtrend: unusual "
                                                   "relative strength.")
            elif up:
                add("52-week range", v, "bullish", "Near its 52-week high in an uptrend. Stocks making new highs in a "
                                                   "rising market have tended to keep leading.")
            else:
                add("52-week range", v, "neutral", "Testing the top of its 52-week range without a trend: a breakout or "
                                                   "a failure there will say more.")
        elif pos <= 10:
            if bull_mkt:
                add("52-week range", v, "bearish", "Near its 52-week low while the market rises: a laggard. Cheap is not "
                                                   "the same as turning.")
            elif down:
                add("52-week range", v, "bearish", "Near its 52-week low in a downtrend: new lows tend to come in runs.")
            else:
                add("52-week range", v, "neutral", "Testing the bottom of its 52-week range without a trend.")
        else:
            add("52-week range", v, "neutral", "Inside its 52-week range.")

    close, hi, lo = (float(row.get(k, np.nan)) for k in ("close", "bb_high", "bb_low"))
    if np.isfinite(close) and np.isfinite(hi) and np.isfinite(lo):
        if close > hi:
            if up:
                add("Bollinger bands", "above the upper band", "bullish",
                    "Above the upper band in an uptrend. Strong trends can walk along the band for weeks.")
            else:
                add("Bollinger bands", "above the upper band", "bearish" if trendless or down else "neutral",
                    "Above the upper band without an uptrend: stretched. Without a trend, prices more often return "
                    "toward the middle band.")
        elif close < lo:
            if down:
                add("Bollinger bands", "below the lower band", "bearish",
                    "Below the lower band in a downtrend. Weak trends can walk down the band.")
            else:
                add("Bollinger bands", "below the lower band", "bullish" if trendless or up else "neutral",
                    "Below the lower band without a downtrend: stretched. Without a trend, prices more often return "
                    "toward the middle band.")
        else:
            add("Bollinger bands", "inside the bands", "neutral", "Inside the bands.")
        if band_width_rank is not None and np.isfinite(band_width_rank) and band_width_rank <= 0.15:
            add("Band width", f"narrower than {100 * (1 - band_width_rank):.0f}% of the past year", "neutral",
                "The bands are squeezed: volatility is unusually low. A bigger move often follows; the squeeze does "
                "not say which way.")
    if regime == "TRANSITION":
        for r in out:
            if r["lean"] != "neutral":
                r["reading"] += " The market is in transition, so read this with extra caution."
    return out
