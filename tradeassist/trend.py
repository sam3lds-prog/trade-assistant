"""Trend Engine V2: one trend state per day, the evidence behind it, and how settled it is.

Six kinds of evidence are scored for each day, using only that day's bar and earlier ones:

    structure            22  swing highs and lows (HH/HL, LH/LL) and breaks of them
    moving averages      22  price against the 20-, 50- and 200-day averages, their order and slope
    momentum             20  MACD, RSI and the 20-day change
    relative strength    16  the stock's return against SPY and its sector fund
    volume               12  volume on up days against down days, and on-balance volume
    validated patterns    8  patterns that fired recently and passed the pattern lab

Each item gives up to its points to the bullish side or the bearish side (or neither). The
bullish and bearish scores are the points won as a share of the points that could be scored
that day, so each runs from 0 to 100; an item without data (no sector fund, no lab run yet,
too little history) is left out and lowers `coverage` instead. These are evidence points. They
are not probabilities: how often a reading was followed by a rise is measured separately
(signal calibration), and only that may be read as a frequency.

The state follows the net score (bullish minus bearish) through a small state machine:

    STRONG_UP, UP, UP_WEAKENING, NEUTRAL, DOWN_DEVELOPING, DOWN, STRONG_DOWN

with hysteresis (a state is entered at 25 points and kept down to 15), so one noisy day does
not flip it. An uptrend never jumps straight to DOWN: it passes through UP_WEAKENING or
DOWN_DEVELOPING, and DOWN_DEVELOPING becomes DOWN only once the swing structure turns down or
the bearish reading has held for five days. A downtrend must pass through NEUTRAL before UP.
STRONG_UP and STRONG_DOWN also need the swing structure and the order of the averages to
agree. Volatility does not push the state either way; it lowers the confidence.

Because every input is causal, the state on any past day is what the engine would have said
that evening. The state from today's unfinished bar is PROVISIONAL; the last finished bar's
state is CONFIRMED (see market.py).
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any, Callable

import numpy as np
import pandas as pd

from .patterns import PATTERNS, detect_all
from .structure import SWING_ATR, find_swings, structure_detail
from .versions import TREND_ENGINE_VERSION

WEIGHTS = {"structure": 22, "moving_averages": 22, "momentum": 20, "relative_strength": 16, "volume": 12,
           "patterns": 8}
COMPONENT_WORDS = {"structure": "Swing structure", "moving_averages": "Moving averages", "momentum": "Momentum",
                   "relative_strength": "Relative strength", "volume": "Volume", "patterns": "Validated patterns"}
STATES = ("STRONG_UP", "UP", "UP_WEAKENING", "NEUTRAL", "DOWN_DEVELOPING", "DOWN", "STRONG_DOWN")
UP_SIDE = ("STRONG_UP", "UP", "UP_WEAKENING")
DOWN_SIDE = ("DOWN_DEVELOPING", "DOWN", "STRONG_DOWN")
STATE_WORDS = {"STRONG_UP": "Strong uptrend", "UP": "Uptrend", "UP_WEAKENING": "Uptrend weakening",
               "NEUTRAL": "No trend", "DOWN_DEVELOPING": "Downtrend developing", "DOWN": "Downtrend",
               "STRONG_DOWN": "Strong downtrend"}
STATE_BIAS = {"STRONG_UP": 2, "UP": 1, "UP_WEAKENING": 0.5, "NEUTRAL": 0, "DOWN_DEVELOPING": -0.5, "DOWN": -1,
              "STRONG_DOWN": -2}
ENTER, STAY, STRONG = 25.0, 15.0, 55.0
CONFIRM_DOWN_DAYS = 5        # DOWN_DEVELOPING becomes DOWN after this many days without a down structure
MIN_COVERAGE = 0.5           # below this share of the points, there is too little data for a state
PATTERN_WINDOW = 5           # a pattern counts as evidence for this many days after it fires
RS_LONG, RS_SHORT = 63, 21   # about three months and one month of trading days


@dataclass
class Item:
    """One piece of evidence: its points to each side on every day, and where it has data."""
    component: str
    name: str
    weight: float
    bull: np.ndarray
    bear: np.ndarray
    available: np.ndarray
    text: Callable[[int], str]


def _arr(series: pd.Series | np.ndarray) -> np.ndarray:
    return np.asarray(series, dtype=float)


def _binary(component: str, name: str, weight: float, up: np.ndarray, down: np.ndarray, available: np.ndarray,
            text: Callable[[int], str]) -> Item:
    return Item(component, name, weight, np.where(available & up, weight, 0.0),
                np.where(available & down, weight, 0.0), available, text)


def _scaled(component: str, name: str, weight: float, value: np.ndarray, scale: float,
            text: Callable[[int], str]) -> Item:
    """Points in proportion to a signed reading, full points at `scale` or beyond."""
    available = np.isfinite(value)
    c = np.clip(np.where(available, value, 0.0) / scale, -1.0, 1.0)
    return Item(component, name, weight, weight * np.clip(c, 0, None), weight * np.clip(-c, 0, None), available, text)


def _pct(a: float, b: float) -> float:
    return 100.0 * (a / b - 1.0) if b else float("nan")


def _excess_return(close: np.ndarray, bench: np.ndarray, days: int) -> np.ndarray:
    out = np.full(len(close), np.nan)
    if len(close) > days:
        own = close[days:] / close[:-days] - 1.0
        theirs = bench[days:] / bench[:-days] - 1.0
        out[days:] = own - theirs
    return out


def _rolling_rank(values: np.ndarray, window: int = 252, min_periods: int = 60) -> np.ndarray:
    """Share of the previous `window` days with a lower value, for each day (causal)."""
    s = pd.Series(values)
    return s.rolling(window + 1, min_periods=min_periods + 1).apply(
        lambda w: float(np.mean(w[:-1] < w[-1])) if np.isfinite(w[-1]) else np.nan, raw=True).to_numpy()


def _align(dates: pd.Series, bench: pd.DataFrame | None) -> np.ndarray | None:
    """A benchmark's closes on the stock's dates (the last close known on or before each date)."""
    if bench is None or bench.empty:
        return None
    b = bench.set_index(pd.to_datetime(bench["date"]))["close"].astype(float)
    b = b[~b.index.duplicated(keep="last")].sort_index()
    out = b.reindex(pd.to_datetime(dates), method="ffill").to_numpy(dtype=float)
    return out if np.isfinite(out).sum() > RS_LONG else None


def build_items(df: pd.DataFrame, benchmarks: dict[str, pd.DataFrame | None] | None = None,
                sector: str | None = None, validated: dict[str, list[dict[str, Any]]] | None = None,
                reversal: float = SWING_ATR, sd: pd.DataFrame | None = None) -> list[Item]:
    """Every piece of evidence for every day of `df` (an enriched frame from indicators.enrich).
    `sd` is structure_detail for `df`, when already worked out."""
    benchmarks = benchmarks or {}
    n = len(df)
    close = _arr(df["close"])
    ema20, sma50, sma200 = _arr(df["ema20"]), _arr(df["sma50"]), _arr(df["sma200"])
    macd, sig, hist, rsi = _arr(df["macd"]), _arr(df["macd_signal"]), _arr(df["macd_hist"]), _arr(df["rsi"])
    volume = _arr(df["volume"])
    items: list[Item] = []

    # --- structure ---------------------------------------------------------------------
    sd = structure_detail(df, find_swings(df, reversal)) if sd is None else sd
    hl, ll = sd["hi_label"].to_numpy(), sd["lo_label"].to_numpy()
    broken = sd["broken"].to_numpy()
    known = (hl != "") & (ll != "")
    bull_lab = (hl == "HH").astype(float) + (ll == "HL").astype(float)
    bear_lab = (hl == "LH").astype(float) + (ll == "LL").astype(float)
    up = (hl == "HH") & (ll == "HL")
    down = (hl == "LH") & (ll == "LL")
    w = WEIGHTS["structure"]
    s_bull = np.where(up & (broken == 0), w, np.where(up, 0.0, np.where(down & (broken == -1), w / 2, w / 4 * bull_lab
                                                                          * (~down))))
    s_bear = np.where(down & (broken == 0), w, np.where(down, 0.0, np.where(up & (broken == 1), w / 2, w / 4 * bear_lab
                                                                            * (~up))))
    def s_text(i: int) -> str:
        if not known[i]:
            return "Not enough swings yet to compare highs and lows."
        words = {"HH": "a higher high", "LH": "a lower high", "EH": "an equal high", "HL": "a higher low",
                 "LL": "a lower low", "EL": "an equal low"}
        h, lo_ = words.get(hl[i], "the first"), words.get(ll[i], "the first")
        if up[i] and broken[i] == 1:
            return (f"Higher highs and higher lows, but the stock has closed below the last higher low "
                    f"(${sd['lo_price'].iloc[i]:,.2f}), which breaks the uptrend structure.")
        if down[i] and broken[i] == -1:
            return (f"Lower highs and lower lows, but the stock has closed above the last lower high "
                    f"(${sd['hi_price'].iloc[i]:,.2f}), which breaks the downtrend structure.")
        if up[i]:
            return f"Higher high and higher low: the last swing low to hold is ${sd['lo_price'].iloc[i]:,.2f}."
        if down[i]:
            return f"Lower high and lower low: the last swing high to clear is ${sd['hi_price'].iloc[i]:,.2f}."
        return f"Mixed swings: the last swing high was {h} and the last swing low {lo_}."
    items.append(Item("structure", "Swing highs and lows", w, np.where(known, s_bull, 0.0), np.where(known, s_bear, 0.0),
                      known, s_text))

    # --- moving averages ------------------------------------------------------------------
    def vs(avg: np.ndarray, label: str) -> Callable[[int], str]:
        return lambda i: (f"Close ${close[i]:,.2f} is {abs(_pct(close[i], avg[i])):.1f}% "
                          f"{'above' if close[i] > avg[i] else 'below'} the {label} (${avg[i]:,.2f}).")
    for avg, label, weight in ((ema20, "20-day average", 4), (sma50, "50-day average", 5), (sma200, "200-day average", 6)):
        ok = np.isfinite(avg)
        items.append(_binary("moving_averages", f"Price against the {label}", weight, close > avg, close < avg, ok,
                             vs(avg, label)))
    ok = np.isfinite(sma50) & np.isfinite(sma200)
    items.append(_binary("moving_averages", "50-day against the 200-day", 4, sma50 > sma200, sma50 < sma200, ok,
                         lambda i: f"The 50-day average is {'above' if sma50[i] > sma200[i] else 'below'} the 200-day "
                                   f"({abs(_pct(sma50[i], sma200[i])):.1f}% apart)."))
    slope = np.full(n, np.nan)
    slope[10:] = sma50[10:] / sma50[:-10] - 1.0
    items.append(_scaled("moving_averages", "Slope of the 50-day average", 3, slope, 0.01,
                         lambda i: f"The 50-day average is {'rising' if slope[i] > 0 else 'falling'}: "
                                   f"{100 * slope[i]:+.1f}% over two weeks."))

    # --- momentum ----------------------------------------------------------------------
    ok = np.isfinite(macd) & np.isfinite(sig)
    items.append(_binary("momentum", "MACD against its signal line", 5, macd > sig, macd < sig, ok,
                         lambda i: f"MACD ({macd[i]:.2f}) is {'above' if macd[i] > sig[i] else 'below'} its signal line "
                                   f"({sig[i]:.2f})."))
    turn = np.full(n, np.nan)
    turn[3:] = hist[3:] - hist[:-3]
    ok = np.isfinite(turn)
    items.append(_binary("momentum", "MACD histogram direction", 3, turn > 0, turn < 0, ok,
                         lambda i: f"The MACD histogram is {'rising' if turn[i] > 0 else 'falling'} over three days "
                                   f"({hist[i]:+.2f} now)."))
    ok = np.isfinite(macd)
    items.append(_binary("momentum", "MACD above or below zero", 4, macd > 0, macd < 0, ok,
                         lambda i: f"MACD is {'above' if macd[i] > 0 else 'below'} zero: the 12-day average is "
                                   f"{'above' if macd[i] > 0 else 'below'} the 26-day."))
    items.append(_scaled("momentum", "RSI (14)", 4, rsi - 50.0, 10.0,
                         lambda i: f"RSI is {rsi[i]:.0f}" + (" (overbought, which in an uptrend is strength more often "
                                                             "than a top)" if rsi[i] >= 70 else
                                                             " (oversold, which in a downtrend is weakness more often "
                                                             "than a bottom)" if rsi[i] <= 30 else "") + "."))
    roc = np.full(n, np.nan)
    roc[20:] = close[20:] / close[:-20] - 1.0
    items.append(_scaled("momentum", "Change over 20 days", 4, roc, 0.05,
                         lambda i: f"The stock is {100 * roc[i]:+.1f}% over the last 20 trading days."))

    # --- relative strength -------------------------------------------------------------------
    spy = _align(df["date"], benchmarks.get("SPY"))
    if spy is not None:
        ex_long = _excess_return(close, spy, RS_LONG)
        ex_short = _excess_return(close, spy, RS_SHORT)
        items.append(_scaled("relative_strength", "Against SPY, three months", 6, ex_long, 0.05,
                             lambda i: f"{100 * ex_long[i]:+.1f} points against SPY over three months."))
        items.append(_scaled("relative_strength", "Against SPY, one month", 4, ex_short, 0.03,
                             lambda i: f"{100 * ex_short[i]:+.1f} points against SPY over one month."))
    sec = _align(df["date"], benchmarks.get(sector)) if sector else None
    if sec is not None:
        ex_sec = _excess_return(close, sec, RS_LONG)
        items.append(_scaled("relative_strength", f"Against its sector ({sector}), three months", 6, ex_sec, 0.05,
                             lambda i: f"{100 * ex_sec[i]:+.1f} points against {sector} over three months."))

    # --- volume ----------------------------------------------------------------------------
    chg = np.diff(close, prepend=np.nan)
    vol_ok = np.isfinite(volume) & (volume > 0)
    up_vol = pd.Series(np.where(chg > 0, volume, 0.0)).rolling(20, min_periods=20).sum().to_numpy()
    dn_vol = pd.Series(np.where(chg < 0, volume, 0.0)).rolling(20, min_periods=20).sum().to_numpy()
    with np.errstate(divide="ignore", invalid="ignore"):
        ratio = np.log(up_vol / dn_vol)
    ratio = np.where(np.isfinite(ratio) & (pd.Series(vol_ok).rolling(20, min_periods=20).min().to_numpy() == 1),
                     ratio, np.nan)
    items.append(_scaled("volume", "Volume on up days against down days", 7, ratio, math.log(1.5),
                         lambda i: f"Over 20 days, {math.exp(ratio[i]):.2f} times as many shares traded on up days as on "
                                   "down days."))
    obv = np.cumsum(np.where(chg > 0, volume, np.where(chg < 0, -volume, 0.0)))
    avg_vol = pd.Series(volume).rolling(20, min_periods=20).mean().to_numpy()
    obv_chg = np.full(n, np.nan)
    obv_chg[20:] = (obv[20:] - obv[:-20]) / (20 * avg_vol[20:])
    obv_chg = np.where(np.isfinite(obv_chg) & (avg_vol > 0), obv_chg, np.nan)
    items.append(_scaled("volume", "On-balance volume", 5, obv_chg, 0.25,
                         lambda i: f"On-balance volume is {'rising' if obv_chg[i] > 0 else 'falling'} over 20 days "
                                   f"({100 * obv_chg[i]:+.0f}% of the period's volume)."))

    # --- validated patterns -----------------------------------------------------------------
    if validated:
        hits = detect_all(df)
        supported = {k for k, vs_ in validated.items() if any(v.get("verdict") == "supported" for v in vs_)}
        bull_n, bear_n = np.zeros(n), np.zeros(n)
        recent: dict[str, np.ndarray] = {}
        for key in supported:
            if key not in hits.columns or key not in PATTERNS:
                continue
            fired = pd.Series(hits[key].to_numpy(dtype=bool)).rolling(PATTERN_WINDOW, min_periods=1).max().to_numpy() > 0
            recent[key] = fired
            if PATTERNS[key].direction > 0:
                bull_n += fired
            elif PATTERNS[key].direction < 0:
                bear_n += fired
        w = WEIGHTS["patterns"]
        def p_text(i: int) -> str:
            names = [PATTERNS[k].label for k, f in recent.items() if f[i]]
            return ("Fired in the last five days and passed the pattern lab: " + "; ".join(names) + "."
                    if names else "No pattern that passed the pattern lab fired in the last five days.")
        items.append(Item("patterns", "Patterns that passed the lab", w, np.minimum(w, w / 2 * bull_n),
                          np.minimum(w, w / 2 * bear_n), np.ones(n, dtype=bool), p_text))
    return items


def scores(items: list[Item], n: int) -> dict[str, np.ndarray]:
    bull = np.zeros(n)
    bear = np.zeros(n)
    avail = np.zeros(n)
    for it in items:
        bull += it.bull
        bear += it.bear
        avail += np.where(it.available, it.weight, 0.0)
    with np.errstate(divide="ignore", invalid="ignore"):
        b = np.where(avail > 0, 100.0 * bull / avail, np.nan)
        r = np.where(avail > 0, 100.0 * bear / avail, np.nan)
    return {"bullish": b, "bearish": r, "net": b - r, "coverage": avail / 100.0}


def run_states(net: np.ndarray, coverage: np.ndarray, struct_up: np.ndarray, struct_down: np.ndarray,
               up_broken: np.ndarray, stacked_up: np.ndarray, stacked_down: np.ndarray) -> list[str]:
    """The state machine described at the top of this module, run day by day."""
    out: list[str] = []
    prev, held = "NEUTRAL", 1          # held: days in the current state, today included
    for i in range(len(net)):
        x = net[i]
        if not (np.isfinite(x) and coverage[i] >= MIN_COVERAGE):
            new = "NEUTRAL"
        elif prev in ("STRONG_UP", "UP"):
            if x <= -ENTER:
                new = "DOWN_DEVELOPING"
            elif x < STAY or up_broken[i]:
                new = "UP_WEAKENING"
            else:
                new = "STRONG_UP" if (x >= STRONG and struct_up[i] and stacked_up[i]) else "UP"
        elif prev == "UP_WEAKENING":
            if x <= -ENTER:
                new = "DOWN_DEVELOPING"
            elif x >= ENTER and not up_broken[i]:
                new = "UP"
            elif x <= 0:
                new = "NEUTRAL"
            else:
                new = "UP_WEAKENING"
        elif prev == "NEUTRAL":
            new = "UP" if x >= ENTER else "DOWN_DEVELOPING" if x <= -ENTER else "NEUTRAL"
        elif prev == "DOWN_DEVELOPING":
            if x > -STAY:
                new = "NEUTRAL"
            elif struct_down[i] or held + 1 >= CONFIRM_DOWN_DAYS:   # today would be day held + 1
                new = "DOWN"
            else:
                new = "DOWN_DEVELOPING"
        else:   # DOWN, STRONG_DOWN
            if x > -STAY:
                new = "NEUTRAL"
            else:
                new = "STRONG_DOWN" if (x <= -STRONG and struct_down[i] and stacked_down[i]) else "DOWN"
        held = held + 1 if new == prev else 1
        out.append(new)
        prev = new
    return out


def evaluate(df: pd.DataFrame, benchmarks: dict[str, pd.DataFrame | None] | None = None,
             sector: str | None = None, validated: dict[str, list[dict[str, Any]]] | None = None,
             reversal: float = SWING_ATR) -> dict[str, Any]:
    """Trend state, scores and evidence for every day of `df`, as arrays and per-day details."""
    n = len(df)
    sd = structure_detail(df, find_swings(df, reversal))
    items = build_items(df, benchmarks, sector, validated, reversal, sd)
    sc = scores(items, n)
    close, sma50, sma200 = _arr(df["close"]), _arr(df["sma50"]), _arr(df["sma200"])
    struct = _arr(df["struct_state"]) if "struct_state" in df.columns else np.zeros(n)
    up_broken = sd["broken"].to_numpy() == 1
    states = run_states(sc["net"], sc["coverage"], struct == 1, struct == -1, up_broken,
                        (close > sma50) & (sma50 > sma200), (close < sma50) & (sma50 < sma200))
    atr_pct = _arr(df["atr"]) / close
    width = (_arr(df["bb_high"]) - _arr(df["bb_low"])) / _arr(df["bb_mid"])
    return {"items": items, "scores": sc, "states": states, "atr_pct": atr_pct,
            "atr_rank": _rolling_rank(atr_pct), "width_rank": _rolling_rank(width), "adx": _arr(df["adx"])}


def bars_in_state(states: list[str], i: int) -> int:
    k = i
    while k > 0 and states[k - 1] == states[i]:
        k -= 1
    return i - k + 1


def confirmation_level(days: int) -> str:
    return "EARLY" if days < 3 else "DEVELOPING" if days < 10 else "ESTABLISHED"


def confidence(ev: dict[str, Any], i: int, provisional: bool) -> dict[str, Any]:
    """How much weight the state on day `i` deserves, with the reasons. Not a probability."""
    sc, state = ev["scores"], ev["states"][i]
    points, reasons = 0, []
    cov, net, adx = sc["coverage"][i], sc["net"][i], ev["adx"][i]
    if cov >= 0.84:
        points += 1
    elif cov < 0.6:
        points -= 1
        reasons.append(f"Only {100 * cov:.0f}% of the evidence could be scored (short history or missing comparisons).")
    if abs(net) >= 40:
        points += 1
        reasons.append(f"The evidence leans clearly one way ({net:+.0f} points net).")
    elif abs(net) < 15:
        points -= 1
        reasons.append(f"The evidence is close to balanced ({net:+.0f} points net).")
    if np.isfinite(adx):
        if adx >= 25 and state != "NEUTRAL":
            points += 1
            reasons.append(f"ADX {adx:.0f}: the move has a strong direction.")
        elif adx < 18:
            points -= 1
            reasons.append(f"ADX {adx:.0f}: little directional movement.")
    rank = ev["atr_rank"][i]
    if np.isfinite(rank) and rank >= 0.85:
        points -= 1
        reasons.append(f"Daily ranges are wider than on {100 * rank:.0f}% of the past year's days; trends whip about more "
                       "when volatility is high.")
    days = bars_in_state(ev["states"], i)
    if days >= 10:
        points += 1
        reasons.append(f"The state has held for {days} days.")
    elif days < 3:
        reasons.append(f"The state is {days} day{'s' if days != 1 else ''} old.")
    if provisional:
        points -= 1
        reasons.append("It comes from today's unfinished bar.")
    level = "HIGH" if points >= 2 else "MEDIUM" if points >= 0 else "LOW"
    return {"level": level, "points": points, "reasons": reasons}


def day_view(df: pd.DataFrame, ev: dict[str, Any], i: int, provisional: bool) -> dict[str, Any]:
    """Everything about day `i`: state, scores, confirmation, confidence, evidence by component."""
    sc = ev["scores"]
    state = ev["states"][i]
    days = bars_in_state(ev["states"], i)
    evidence = []
    for it in ev["items"]:
        if not it.available[i]:
            evidence.append({"component": it.component, "name": it.name, "weight": it.weight, "side": "unavailable",
                             "points": 0.0, "text": "No data for this yet."})
            continue
        side = "bullish" if it.bull[i] > 1e-9 else "bearish" if it.bear[i] > 1e-9 else "neutral"
        try:
            text = it.text(i)
        except (ValueError, ZeroDivisionError, IndexError):
            text = ""
        evidence.append({"component": it.component, "name": it.name, "weight": it.weight, "side": side,
                         "points": round(float(max(it.bull[i], it.bear[i])), 2), "text": text})
    components = []
    for key, weight in WEIGHTS.items():
        mine = [e for e in evidence if e["component"] == key]
        have = [e for e in mine if e["side"] != "unavailable"]
        components.append({"key": key, "name": COMPONENT_WORDS[key], "weight": weight,
                           "available": round(sum(e["weight"] for e in have), 2),
                           "bullish": round(sum(e["points"] for e in have if e["side"] == "bullish"), 2),
                           "bearish": round(sum(e["points"] for e in have if e["side"] == "bearish"), 2)})
    rank, wrank = ev["atr_rank"][i], ev["width_rank"][i]
    vol = {"atr_pct": float(100 * ev["atr_pct"][i]) if np.isfinite(ev["atr_pct"][i]) else None,
           "atr_rank": float(rank) if np.isfinite(rank) else None,
           "band_width_rank": float(wrank) if np.isfinite(wrank) else None,
           "squeeze": bool(np.isfinite(wrank) and wrank <= 0.15),
           "high": bool(np.isfinite(rank) and rank >= 0.85)}
    return {"date": pd.Timestamp(df["date"].iloc[i]).strftime("%Y-%m-%d"), "state": state, "label": STATE_WORDS[state],
            "status": "PROVISIONAL" if provisional else "CONFIRMED",
            "bullish_score": round(float(sc["bullish"][i]), 1) if np.isfinite(sc["bullish"][i]) else None,
            "bearish_score": round(float(sc["bearish"][i]), 1) if np.isfinite(sc["bearish"][i]) else None,
            "net_score": round(float(sc["net"][i]), 1) if np.isfinite(sc["net"][i]) else None,
            "coverage": round(float(sc["coverage"][i]), 2),
            "confirmation": {"level": confirmation_level(days), "days_in_state": days,
                             "since": pd.Timestamp(df["date"].iloc[i - days + 1]).strftime("%Y-%m-%d")},
            "confidence": confidence(ev, i, provisional), "components": components, "evidence": evidence,
            "volatility": vol, "close": float(df["close"].iloc[i])}


def transitions(df: pd.DataFrame, ev: dict[str, Any], limit: int = 10) -> list[dict[str, Any]]:
    states, net = ev["states"], ev["scores"]["net"]
    out = []
    for i in range(1, len(states)):
        if states[i] != states[i - 1]:
            out.append({"date": pd.Timestamp(df["date"].iloc[i]).strftime("%Y-%m-%d"), "from": states[i - 1],
                        "to": states[i], "net_score": round(float(net[i]), 1) if np.isfinite(net[i]) else None,
                        "close": float(df["close"].iloc[i])})
    return out[-limit:][::-1]


def analyse(df: pd.DataFrame, benchmarks: dict[str, pd.DataFrame | None] | None = None, sector: str | None = None,
            validated: dict[str, list[dict[str, Any]]] | None = None, history: int = 252) -> dict[str, Any]:
    """The trend read for the last day of `df`: confirmed (last finished bar) and, when the last bar
    is unfinished, provisional. `df` is an enriched frame; a `bar_complete` column marks finished bars."""
    if len(df) < 30:
        raise ValueError("At least 30 daily bars are needed for a trend reading")
    ev = evaluate(df, benchmarks, sector, validated)
    complete = df["bar_complete"].to_numpy(dtype=bool) if "bar_complete" in df.columns else np.ones(len(df), bool)
    last = len(df) - 1
    finished = np.flatnonzero(complete)
    confirmed_i = int(finished[-1]) if len(finished) else None
    provisional = None if complete[last] else day_view(df, ev, last, True)
    confirmed = day_view(df, ev, confirmed_i, False) if confirmed_i is not None else None
    start = max(0, len(df) - history)
    hist = [{"date": pd.Timestamp(d).strftime("%Y-%m-%d"), "close": float(c), "state": s,
             "net": round(float(x), 1) if np.isfinite(x) else None, "complete": bool(k)}
            for d, c, s, x, k in zip(df["date"].iloc[start:], df["close"].iloc[start:], ev["states"][start:],
                                     ev["scores"]["net"][start:], complete[start:])]
    current = provisional or confirmed
    return {"version": TREND_ENGINE_VERSION, "as_of": current["date"], "status": current["status"],
            "state": current["state"], "label": current["label"], "confirmed": confirmed, "provisional": provisional,
            "changed_today": bool(provisional and confirmed and provisional["state"] != confirmed["state"]),
            # Changes of state on finished bars only: one on today's bar so far is in `provisional`.
            "transitions": [t for t in transitions(df, ev, 11) if complete[last] or t["date"] != current["date"]][:10],
            "history": hist, "sector_fund": sector,
            "weights": WEIGHTS, "thresholds": {"enter": ENTER, "stay": STAY, "strong": STRONG},
            "note": "Scores are evidence points out of 100 on each side, not probabilities."}


def state_series(df: pd.DataFrame, benchmarks: dict[str, pd.DataFrame | None] | None = None,
                 sector: str | None = None, validated: dict[str, list[dict[str, Any]]] | None = None) -> pd.DataFrame:
    """The state and scores on every day, for studies and signal history."""
    ev = evaluate(df, benchmarks, sector, validated)
    sc = ev["scores"]
    return pd.DataFrame({"date": df["date"].to_numpy(), "trend_state": ev["states"], "bullish_score": sc["bullish"],
                         "bearish_score": sc["bearish"], "net_score": sc["net"], "coverage": sc["coverage"]},
                        index=df.index)
