"""Price structure: swing highs and lows, higher highs and higher lows (or lower ones),
support and resistance levels, and trendlines through recent swings.

Swings come from a zigzag that adapts to how much the stock usually moves. A swing high is
confirmed once a day closes at least `reversal` times the 14-day average true range (ATR)
below the highest high since the last swing low; a swing low the other way round. Each swing
therefore has two dates: the day the high or low was made, and the later day it became
known. Everything derived here (the HH/HL/LH/LL labels, the trend state, breaks of
structure and the pattern-lab events) uses a swing only from the day it became known, so a
result for any past day uses no information from after that day.

Labels compare each swing with the previous swing of the same kind:
    HH higher high, LH lower high, EH equal high (within a quarter of an ATR)
    HL higher low,  LL lower low,  EL equal low
The trend state on a day is
    up     the latest swing high is HH and the latest swing low is HL
    down   the latest swing high is LH and the latest swing low is LL
    range  anything else
An up state is broken when a day closes below the latest higher low (and a down state when a
day closes above the latest lower high); it stays broken until the next swing is confirmed.
"""

from __future__ import annotations

import math
from dataclasses import asdict, dataclass
from typing import Any

import numpy as np
import pandas as pd

SWING_ATR = 2.0          # a reversal of this many ATRs confirms a swing
EQUAL_ATR = 0.25         # two swings this close (in ATRs) count as equal: a double top or bottom
LEVEL_ATR = 0.5          # swings this close (in ATRs) are treated as the same support or resistance level
LEVEL_LOOKBACK = 252     # trading days of swings used for support and resistance

STATE_WORDS = {1: "up", -1: "down", 0: "range"}


@dataclass(frozen=True)
class Swing:
    kind: str          # "high" or "low"
    index: int         # bar on which the high or low was made
    price: float
    confirmed: int     # bar on which the swing became known (always >= index)
    label: str         # HH, LH, EH, HL, LL, EL; "" for the first swing of its kind


def _arrays(df: pd.DataFrame) -> tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
    return (df["high"].to_numpy(dtype=float), df["low"].to_numpy(dtype=float),
            df["close"].to_numpy(dtype=float), df["atr"].to_numpy(dtype=float))


def find_swings(df: pd.DataFrame, reversal: float = SWING_ATR) -> list[Swing]:
    """Confirmed swings in order of the day they became known. `df` needs high, low, close, atr."""
    high, low, close, atr = _arrays(df)
    # A missing high or low can never be the extreme: it is treated as no move at all.
    high = np.where(np.isfinite(high), high, -np.inf)
    low = np.where(np.isfinite(low), low, np.inf)
    swings: list[Swing] = []
    last_of: dict[str, Swing] = {}
    direction = 0            # +1: rising leg, watching for a high; -1: falling leg; 0: not yet known
    hi_i = lo_i = -1

    def confirm(kind: str, index: int, t: int) -> None:
        price = float(high[index] if kind == "high" else low[index])
        prev = last_of.get(kind)
        label = ""
        if prev is not None:
            tol = EQUAL_ATR * atr[t]
            if kind == "high":
                label = "HH" if price > prev.price + tol else "LH" if price < prev.price - tol else "EH"
            else:
                label = "HL" if price > prev.price + tol else "LL" if price < prev.price - tol else "EL"
        swing = Swing(kind, index, price, t, label)
        swings.append(swing)
        last_of[kind] = swing

    for t in range(len(close)):
        if not (math.isfinite(atr[t]) and atr[t] > 0 and math.isfinite(close[t])
                and math.isfinite(high[t]) and math.isfinite(low[t])):
            continue
        if hi_i < 0:
            hi_i = lo_i = t
        if direction >= 0 and high[t] > high[hi_i]:
            hi_i = t
        if direction <= 0 and low[t] < low[lo_i]:
            lo_i = t
        threshold = reversal * atr[t]
        top_confirmed = direction >= 0 and close[t] <= high[hi_i] - threshold
        bottom_confirmed = direction <= 0 and close[t] >= low[lo_i] + threshold
        if direction == 0 and top_confirmed and bottom_confirmed:
            # Both extremes are far enough away: the earlier one is the older swing.
            top_confirmed, bottom_confirmed = hi_i < lo_i, lo_i <= hi_i
        if top_confirmed:
            confirm("high", hi_i, t)
            direction = -1
            lo_i = hi_i + 1 + int(np.argmin(low[hi_i + 1:t + 1])) if t > hi_i else t
        elif bottom_confirmed:
            confirm("low", lo_i, t)
            direction = 1
            hi_i = lo_i + 1 + int(np.argmax(high[lo_i + 1:t + 1])) if t > lo_i else t
    return swings


def structure_series(df: pd.DataFrame, swings: list[Swing] | None = None,
                     reversal: float = SWING_ATR) -> pd.DataFrame:
    """Per-bar trend state and the bars on which it changes, using only swings known by then.

    Columns: struct_state (+1 up, -1 down, 0 range), struct_up_start, struct_down_start,
    struct_up_break (closed below the latest higher low while up), struct_down_break.
    """
    swings = find_swings(df, reversal) if swings is None else swings
    close = df["close"].to_numpy(dtype=float)
    n = len(close)
    state = np.zeros(n, dtype=int)
    up_start, down_start = np.zeros(n, dtype=bool), np.zeros(n, dtype=bool)
    up_break, down_break = np.zeros(n, dtype=bool), np.zeros(n, dtype=bool)
    last: dict[str, Swing] = {}
    broken, prev, k = False, 0, 0
    for t in range(n):
        while k < len(swings) and swings[k].confirmed == t:
            last[swings[k].kind] = swings[k]
            broken = False
            k += 1
        hi, lo = last.get("high"), last.get("low")
        rising = hi is not None and lo is not None and hi.label == "HH" and lo.label == "HL"
        falling = hi is not None and lo is not None and hi.label == "LH" and lo.label == "LL"
        if rising and not broken and close[t] < lo.price:
            broken, up_break[t] = True, True
        if falling and not broken and close[t] > hi.price:
            broken, down_break[t] = True, True
        s = 1 if rising and not broken else -1 if falling and not broken else 0
        state[t] = s
        up_start[t] = s == 1 and prev != 1
        down_start[t] = s == -1 and prev != -1
        prev = s
    return pd.DataFrame({"struct_state": state, "struct_up_start": up_start, "struct_down_start": down_start,
                         "struct_up_break": up_break, "struct_down_break": down_break}, index=df.index)


def find_levels(swings: list[Swing], close: float, atr: float, as_of: int,
                lookback: int = LEVEL_LOOKBACK, per_side: int = 3) -> dict[str, list[dict[str, Any]]]:
    """Support and resistance from swings known by bar `as_of`.

    Swing highs and lows within `lookback` bars are grouped when they sit within half an ATR of
    each other; each group is one level, its price the average of its swings and its strength the
    number of swings in it. The nearest `per_side` levels above the price are resistance and the
    nearest below are support.
    """
    known = sorted((s for s in swings if s.confirmed <= as_of and s.index >= as_of - lookback),
                   key=lambda s: s.price)
    if not known or not (math.isfinite(atr) and atr > 0):
        return {"support": [], "resistance": []}
    tol = LEVEL_ATR * atr
    groups: list[list[Swing]] = []
    for s in known:
        # Joined to the group below when close to its average, and never making a group wider than
        # two tolerances, so a long run of nearby swings cannot chain into one very wide "level".
        if groups and s.price - np.mean([g.price for g in groups[-1]]) <= tol and s.price - groups[-1][0].price <= 2 * tol:
            groups[-1].append(s)
        else:
            groups.append([s])
    levels = []
    for g in groups:
        price = float(np.mean([s.price for s in g]))
        levels.append({"price": price, "touches": len(g),
                       "highs": sum(1 for s in g if s.kind == "high"), "lows": sum(1 for s in g if s.kind == "low"),
                       "first_index": min(s.index for s in g), "last_index": max(s.index for s in g),
                       "distance_pct": 100.0 * (price / close - 1.0),
                       "strength": "strong" if len(g) >= 3 else "tested twice" if len(g) == 2 else "single swing"})
    support = sorted((lv for lv in levels if lv["price"] < close), key=lambda lv: -lv["price"])[:per_side]
    resistance = sorted((lv for lv in levels if lv["price"] >= close), key=lambda lv: lv["price"])[:per_side]
    return {"support": support, "resistance": resistance}


def find_trendlines(swings: list[Swing], close: np.ndarray, as_of: int) -> list[dict[str, Any]]:
    """A rising line through the last two swing lows when they rise, and a falling line through the
    last two swing highs when they fall, extended to bar `as_of`."""
    out = []
    for kind, wanted, name in (("low", 1, "rising support"), ("high", -1, "falling resistance")):
        pts = [s for s in swings if s.kind == kind and s.confirmed <= as_of][-2:]
        if len(pts) < 2 or pts[1].index == pts[0].index:
            continue
        a, b = pts
        slope = (b.price - a.price) / (b.index - a.index)
        if slope * wanted <= 0:
            continue
        value = b.price + slope * (as_of - b.index)
        crossed = close[as_of] < value if kind == "low" else close[as_of] > value
        out.append({"kind": name, "from_index": a.index, "from_price": a.price, "through_index": b.index,
                    "through_price": b.price, "to_index": as_of, "value_now": float(value),
                    "slope_per_day": float(slope), "crossed": bool(crossed)})
    return out


def describe(swings: list[Swing], state: int, broke: str | None, levels: dict[str, list[dict[str, Any]]],
             dates: list[str], close: float) -> dict[str, Any]:
    """Plain-language reading of the structure as of the last bar."""
    hi = next((s for s in reversed(swings) if s.kind == "high"), None)
    lo = next((s for s in reversed(swings) if s.kind == "low"), None)
    words = {"HH": "higher than", "LH": "lower than", "EH": "level with",
             "HL": "higher than", "LL": "lower than", "EL": "level with"}
    parts = []
    if hi is not None and hi.label:
        parts.append(f"the last swing high (${hi.price:,.2f} on {dates[hi.index]}) was {words[hi.label]} the one before")
    if lo is not None and lo.label:
        parts.append(f"the last swing low (${lo.price:,.2f} on {dates[lo.index]}) was {words[lo.label]} the one before")
    detail = ("; ".join(parts) + ".") if parts else "Not enough swings yet to compare."
    detail = detail[0].upper() + detail[1:]
    if state == 1:
        headline = "Uptrend: higher highs and higher lows"
        watch = (f"The uptrend structure breaks if the stock closes below the last higher low, ${lo.price:,.2f} "
                 f"({100 * (lo.price / close - 1):+.1f}% from here).") if lo else ""
    elif state == -1:
        headline = "Downtrend: lower highs and lower lows"
        watch = (f"The downtrend structure breaks if the stock closes above the last lower high, ${hi.price:,.2f} "
                 f"({100 * (hi.price / close - 1):+.1f}% from here).") if hi else ""
    else:
        headline = "No clear trend: swings are mixed"
        watch = ("The stock closed through its last swing level, which ended the previous trend structure. "
                 if broke else "") + "A trend shows once highs and lows both step the same way."
    return {"state": STATE_WORDS[state], "headline": headline, "detail": detail, "watch": watch.strip()}


def analyse(df: pd.DataFrame, reversal: float = SWING_ATR) -> dict[str, Any]:
    """Everything the chart and the research brief show about structure, as of the last bar."""
    if df.empty:
        return {"swings": [], "state": "range", "levels": {"support": [], "resistance": []}, "trendlines": []}
    swings = find_swings(df, reversal)
    series = structure_series(df, swings, reversal)
    close = df["close"].to_numpy(dtype=float)
    last = len(df) - 1
    stamps = pd.to_datetime(df["date"])
    dates = [d.strftime("%Y-%m-%d") for d in stamps]
    spoken = [f"{d:%b} {d.day}, {d.year}" for d in stamps]
    atr_last = float(df["atr"].iloc[-1]) if math.isfinite(float(df["atr"].iloc[-1])) else float("nan")
    levels = find_levels(swings, float(close[last]), atr_last, last)
    trend = find_trendlines(swings, close, last)
    states = series["struct_state"].to_numpy()
    differs = np.flatnonzero(states != states[-1])
    since = int(differs[-1]) + 1 if len(differs) else 0
    breaks = series["struct_up_break"] | series["struct_down_break"]
    last_break = int(np.flatnonzero(breaks.to_numpy())[-1]) if breaks.any() else None
    last_conf = swings[-1].confirmed if swings else -1
    broke = None
    if last_break is not None and last_break >= last_conf:     # a break can land on a confirmation day
        broke = "up" if series["struct_up_break"].iloc[last_break] else "down"
    state = int(series["struct_state"].iloc[-1])
    out = describe(swings, state, broke, levels, spoken, float(close[last]))

    def swing_view(s: Swing) -> dict[str, Any]:
        v = asdict(s)
        v["date"], v["confirmed_date"] = dates[s.index], dates[s.confirmed]
        return v
    for side in ("support", "resistance"):
        for lv in levels[side]:
            lv["first_date"], lv["last_date"] = dates[lv["first_index"]], dates[lv["last_index"]]
    for tl in trend:
        tl["from_date"], tl["through_date"], tl["to_date"] = (dates[tl["from_index"]], dates[tl["through_index"]],
                                                             dates[tl["to_index"]])
    out.update({"reversal_atr": reversal, "swings": [swing_view(s) for s in swings], "levels": levels,
                "trendlines": trend, "state_series": series["struct_state"].tolist(),
                "state_since": dates[since],
                "broke": broke})
    return out
