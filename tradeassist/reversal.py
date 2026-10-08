"""Trend reversals: is a fall ending in a bottom, or a rise ending in a top?

Nobody can see a true bottom or top while it forms; it is only certain afterwards. What can be
seen is evidence, in two kinds:

- Exhaustion: the move has gone a long way (in daily ranges), the stock is stretched from its
  20-day average and RSI is at an extreme; RSI or MACD fails to confirm the latest low (or high),
  a divergence; a climax day on heavy volume with a wide range that closes off its extreme; a
  reversal candle (hammer, engulfing, a reclaim of the Bollinger band, RSI leaving its extreme).
- Confirmation: the swings change (a higher low after the fall, then a close above the last
  lower high, or the mirror for a top), the trend engine leaves its downtrend (or uptrend), the
  price takes back its 20- and 50-day averages, and volume favours the new direction.

Each side is scored out of 100 points from that evidence and given a stage:

    NONE        no meaningful prior move to reverse
    WATCH       a long move that is stretched or at an RSI extreme, no reversal sign yet
    CANDIDATE   a potential bottom (or top): exhaustion signs, not yet confirmed by the swings
    CONFIRMED   the swings or the trend engine have turned after the move
    FAILED      a candidate whose low (or high) was taken out on a closing basis

The stage on every past day uses only that day's bar and earlier ones, so the engine's past
calls can be counted: how often a potential bottom on this stock was followed by a rise, and
across the signal history. Points are evidence, not probabilities. A candidate is earlier and
cheaper but wrong more often; a confirmed reversal gives up some of the move for more evidence.
"""

from __future__ import annotations

import math
from typing import Any

import numpy as np
import pandas as pd

from .patterns import detect_all
from .structure import SWING_ATR, find_levels, find_swings, structure_detail
from .versions import REVERSAL_ENGINE_VERSION

SIDES = ("bottom", "top")
STAGES = ("NONE", "WATCH", "CANDIDATE", "CONFIRMED", "FAILED")
STAGE_RANK = {"NONE": 0, "FAILED": 0, "WATCH": 1, "CANDIDATE": 2, "CONFIRMED": 3}
MOVE_WINDOW = 60          # bars over which the prior fall (or rise) is measured
MIN_MOVE_ATR = 4.0        # a move of this many daily ranges is worth reversing
CANDIDATE_DAYS = 10       # a candidate stays live this long unless confirmed or failed
CONFIRMED_DAYS = 15       # a confirmation is reported this long after it happens
STOP_BUFFER_ATR = 0.25
REVERSAL_CANDLES = {"bottom": ("hammer", "bullish_engulfing", "bollinger_low_reclaim", "rsi_leaves_oversold"),
                    "top": ("shooting_star", "bearish_engulfing", "bollinger_high_reject", "rsi_leaves_overbought")}
WEIGHTS = {"move": 10, "rsi_extreme": 8, "stretch": 7, "rsi_divergence": 10, "macd_divergence": 5, "climax": 10,
           "candle": 10, "swing_turn": 10, "structure_break": 15, "average_20": 5, "average_50": 5, "volume": 5}
EVIDENCE_WORDS = {
    "move": "A long prior move", "rsi_extreme": "RSI at an extreme", "stretch": "Stretched from the 20-day average",
    "rsi_divergence": "RSI divergence", "macd_divergence": "MACD divergence", "climax": "Climax day",
    "candle": "Reversal candle", "swing_turn": "First swing the other way", "structure_break": "Swing structure broken",
    "average_20": "Back across the 20-day average", "average_50": "Back across the 50-day average",
    "volume": "Volume favours the turn",
}
KIND_WORDS = {"exhaustion": ("move", "rsi_extreme", "stretch", "rsi_divergence", "macd_divergence", "climax", "candle"),
              "confirmation": ("swing_turn", "structure_break", "average_20", "average_50", "volume")}


def _a(df: pd.DataFrame, col: str) -> np.ndarray:
    return df[col].to_numpy(dtype=float) if col in df.columns else np.full(len(df), np.nan)


def evaluate(df: pd.DataFrame, trend_states: pd.DataFrame | None = None,
             reversal: float = SWING_ATR) -> dict[str, Any]:
    """Per-bar evidence, scores and stages for both sides. `df` is an enriched frame."""
    n = len(df)
    close, high, low = _a(df, "close"), _a(df, "high"), _a(df, "low")
    atr, rsi, hist = _a(df, "atr"), _a(df, "rsi"), _a(df, "macd_hist")
    ema20, sma20, sma50 = _a(df, "ema20"), _a(df, "sma20"), _a(df, "sma50")
    bb_low, bb_high, vol_ratio, volume = _a(df, "bb_low"), _a(df, "bb_high"), _a(df, "vol_ratio"), _a(df, "volume")
    swings = find_swings(df, reversal)
    sd = structure_detail(df, swings)
    broken = sd["broken"].to_numpy()
    hits = detect_all(df)
    states = trend_states["trend_state"].to_numpy() if trend_states is not None else None
    chg = np.diff(close, prepend=np.nan)
    up_vol = pd.Series(np.where(chg > 0, volume, 0.0)).rolling(10, min_periods=10).sum().to_numpy()
    dn_vol = pd.Series(np.where(chg < 0, volume, 0.0)).rolling(10, min_periods=10).sum().to_numpy()

    out: dict[str, Any] = {"swings": swings, "sd": sd}
    for side in SIDES:
        d = 1 if side == "bottom" else -1          # +1: looking for the end of a fall
        ext = low if d > 0 else high               # the extreme of the move being reversed
        points = {k: np.zeros(n) for k in WEIGHTS}
        stage = np.array(["NONE"] * n, dtype=object)
        move_atr = np.full(n, np.nan)
        extreme_price = np.full(n, np.nan)
        anchor_price = np.full(n, np.nan)          # what a reversal is wrong beyond
        candle_keys = [k for k in REVERSAL_CANDLES[side] if k in hits.columns]
        candle_hit = (hits[candle_keys].to_numpy(dtype=bool).any(axis=1) if candle_keys else np.zeros(n, bool))
        cand_bar = conf_bar = fail_bar = -10_000
        cand_extreme = math.nan
        conf_anchor = math.nan
        k_sw = 0
        last_same: Any = None                      # latest confirmed swing of the reversed kind (lows for a bottom)
        prev_same: Any = None
        last_opp: Any = None
        last_break = -10_000
        for t in range(n):
            while k_sw < len(swings) and swings[k_sw].confirmed <= t:
                s = swings[k_sw]
                if (s.kind == "low") == (d > 0):
                    prev_same, last_same = last_same, s
                else:
                    last_opp = s
                k_sw += 1
            a = atr[t]
            if not (np.isfinite(a) and a > 0 and np.isfinite(close[t])):
                continue
            # The move being reversed: from its start (the opposite extreme in the 60 bars before) to its
            # extreme (the lowest low, or highest high, of the last 40 bars). A reversal only counts while
            # the price is still near that extreme: once it has retraced most of the move, the turn is old.
            w0 = max(0, t - 40)
            seg_ext = ext[w0:t + 1]
            if not np.isfinite(seg_ext).any():
                continue
            i_ext = w0 + int(np.nanargmin(seg_ext) if d > 0 else np.nanargmax(seg_ext))
            base_seg = (high if d > 0 else low)[max(0, i_ext - MOVE_WINDOW):i_ext + 1]
            if not np.isfinite(base_seg).any():
                continue
            base = float(np.nanmax(base_seg) if d > 0 else np.nanmin(base_seg))
            size = d * (base - ext[i_ext])
            mv = size / a
            move_atr[t] = mv
            retraced = (d * (close[t] - ext[i_ext]) / size) if size > 0 else 1.0
            context = mv >= MIN_MOVE_ATR and retraced <= 0.6
            context_recent = context
            points["move"][t] = WEIGHTS["move"] * float(np.clip((mv - MIN_MOVE_ATR) / 2.0, 0, 1)) * (retraced <= 0.6) \
                if mv >= MIN_MOVE_ATR else 0.0

            # --- exhaustion -----------------------------------------------------------------------
            r5 = rsi[max(0, t - 4):t + 1]
            rsi_ext = bool(np.isfinite(r5).any() and ((np.nanmin(r5) <= 30) if d > 0 else (np.nanmax(r5) >= 70)))
            points["rsi_extreme"][t] = WEIGHTS["rsi_extreme"] * rsi_ext
            st5 = d * (sma20[max(0, t - 4):t + 1] - close[max(0, t - 4):t + 1]) / a
            band = (close[max(0, t - 4):t + 1] < bb_low[max(0, t - 4):t + 1]) if d > 0 else \
                   (close[max(0, t - 4):t + 1] > bb_high[max(0, t - 4):t + 1])
            stretch = bool((np.isfinite(st5) & (st5 >= 2.0)).any() or band.any())
            points["stretch"][t] = WEIGHTS["stretch"] * stretch
            # The extreme of the current leg: since the last swing the other way, at most 40 bars back.
            start = min(t, max(t - 40, (last_opp.index + 1) if last_opp is not None else 0))
            seg = ext[start:t + 1]
            cur_i = (start + int(np.nanargmin(seg) if d > 0 else np.nanargmax(seg))) if np.isfinite(seg).any() else i_ext
            extreme_price[t] = ext[cur_i]
            div_r = div_m = False
            ref = last_same if (last_same is not None and last_same.index < start) else prev_same
            if ref is not None and ref.index < cur_i and cur_i >= t - 10:
                further = (ext[cur_i] < ref.price) if d > 0 else (ext[cur_i] > ref.price)
                if further and np.isfinite(rsi[cur_i]) and np.isfinite(rsi[ref.index]):
                    div_r = bool(d * (rsi[cur_i] - rsi[ref.index]) >= 3 and d * (50 - rsi[cur_i]) > 0)
                if further and np.isfinite(hist[cur_i]) and np.isfinite(hist[ref.index]):
                    div_m = bool(d * hist[ref.index] < 0 and d * (hist[cur_i] - hist[ref.index]) > 0)
            points["rsi_divergence"][t] = WEIGHTS["rsi_divergence"] * div_r
            points["macd_divergence"][t] = WEIGHTS["macd_divergence"] * div_m
            climax = False
            for j in range(max(1, t - 4), t + 1):
                rng = high[j] - low[j]
                if not (np.isfinite(vol_ratio[j]) and vol_ratio[j] >= 2.0 and rng >= 1.5 * atr[j - 1] > 0):
                    continue
                extreme20 = (low[j] <= np.nanmin(low[max(0, j - 20):j + 1])) if d > 0 else \
                            (high[j] >= np.nanmax(high[max(0, j - 20):j + 1]))
                off = ((close[j] - low[j]) / rng) if d > 0 else ((high[j] - close[j]) / rng)
                if extreme20 and off >= 0.4:
                    climax = True
            points["climax"][t] = WEIGHTS["climax"] * climax
            candle = bool(candle_hit[max(0, t - 2):t + 1].any())
            points["candle"][t] = WEIGHTS["candle"] * candle

            # --- confirmation ---------------------------------------------------------------------
            turn_label = "HL" if d > 0 else "LH"
            swing_turn = bool(last_same is not None and last_same.label == turn_label and last_same.confirmed >= t - 30)
            points["swing_turn"][t] = WEIGHTS["swing_turn"] * swing_turn
            if broken[t] == -d and (t == 0 or broken[t - 1] != -d):
                last_break = t
            engine_turn = False
            if states is not None and t >= 1:
                window = states[max(0, t - 20):t + 1]
                was = ("DOWN", "STRONG_DOWN") if d > 0 else ("UP", "STRONG_UP")
                # Out of the downtrend (uptrend) altogether: neutral or the other side, mirrored exactly.
                now_ok = ("NEUTRAL", "UP_WEAKENING", "UP", "STRONG_UP") if d > 0 else \
                         ("NEUTRAL", "DOWN_DEVELOPING", "DOWN", "STRONG_DOWN")
                engine_turn = bool(any(s in was for s in window[:-1]) and states[t] in now_ok)
            structure_break = t - last_break <= 20
            points["structure_break"][t] = WEIGHTS["structure_break"] if structure_break else (10.0 if engine_turn else 0.0)
            above20 = bool(np.isfinite(ema20[t]) and d * (close[t] - ema20[t]) > 0 and t >= 3
                           and d * (ema20[t] - ema20[t - 3]) > 0)
            points["average_20"][t] = WEIGHTS["average_20"] * above20
            points["average_50"][t] = WEIGHTS["average_50"] * bool(np.isfinite(sma50[t]) and d * (close[t] - sma50[t]) > 0)
            if np.isfinite(up_vol[t]) and np.isfinite(dn_vol[t]) and up_vol[t] + dn_vol[t] > 0:
                ratio = (up_vol[t] / dn_vol[t]) if dn_vol[t] > 0 else 10.0
                points["volume"][t] = WEIGHTS["volume"] * bool(ratio >= 1.2 if d > 0 else ratio <= 1 / 1.2)

            # --- stage ------------------------------------------------------------------------------
            early = rsi_ext or stretch
            signs = int(div_r or div_m) + int(climax) + int(candle)
            # A live candidate fails on a close beyond its extreme; only then can a new one start.
            if t - cand_bar <= CANDIDATE_DAYS and np.isfinite(cand_extreme) and d * (close[t] - cand_extreme) < 0:
                fail_bar, cand_bar, cand_extreme = t, -10_000, math.nan
            # The move must have paused: the extreme is not today's bar and the close is half a daily range
            # or more off it. Without this, every new low in a falling market would look like a bottom.
            off_extreme = i_ext < t and d * (close[t] - ext[i_ext]) >= 0.5 * a
            cooled = t - fail_bar > 3 or climax
            if context and off_extreme and cooled and (signs >= 2 or (signs >= 1 and early)):
                if t - cand_bar > CANDIDATE_DAYS or not np.isfinite(cand_extreme):
                    cand_extreme = ext[cur_i]
                cand_bar = t
            confirm = context_recent and (structure_break or ((swing_turn or engine_turn) and above20))
            if confirm and t - conf_bar > CONFIRMED_DAYS:
                conf_bar = t
                conf_anchor = float(ext[i_ext])
                if swing_turn:                     # after a higher low, being wrong means losing that low
                    conf_anchor = last_same.price
            # A close through what the confirmation rests on fails it.
            if t - conf_bar <= CONFIRMED_DAYS and np.isfinite(conf_anchor) and d * (close[t] - conf_anchor) < 0:
                fail_bar, conf_bar, conf_anchor = t, -10_000, math.nan
            if t - conf_bar <= CONFIRMED_DAYS:
                stage[t] = "CONFIRMED"
                anchor_price[t] = conf_anchor
            elif t - cand_bar <= CANDIDATE_DAYS:
                stage[t] = "CANDIDATE"
                anchor_price[t] = cand_extreme
            elif t - fail_bar <= 5:
                stage[t] = "FAILED"
            elif context and early:
                stage[t] = "WATCH"
                anchor_price[t] = extreme_price[t]
        score = sum(points.values())
        out[side] = {"points": points, "score": score, "stage": stage, "move_atr": move_atr,
                     "extreme": extreme_price, "anchor": anchor_price}
    return out


def events(ev: dict[str, Any]) -> list[tuple[int, str]]:
    """(bar, signal key) for each new candidate and each new confirmation, both sides."""
    out = []
    for side in SIDES:
        st = ev[side]["stage"]
        for t in range(1, len(st)):
            if st[t] == "CANDIDATE" and st[t - 1] not in ("CANDIDATE", "CONFIRMED"):
                out.append((t, f"reversal:{side}_candidate"))
            if st[t] == "CONFIRMED" and st[t - 1] != "CONFIRMED":
                out.append((t, f"reversal:{side}_confirmed"))
    return sorted(out)


def levels(df: pd.DataFrame, ev: dict[str, Any], side: str, t: int) -> dict[str, Any]:
    """Where the reversal is wrong, and where it would pay: the levels a trade on it would use."""
    d = 1 if side == "bottom" else -1
    close = float(df["close"].iloc[t])
    atr = float(df["atr"].iloc[t])
    s = ev[side]
    ext = df["low"].to_numpy(dtype=float) if d > 0 else df["high"].to_numpy(dtype=float)
    anchor = s["anchor"][t] if np.isfinite(s["anchor"][t]) else s["extreme"][t]
    anchor_is = f"the {'low' if d > 0 else 'high'} of the reversal"
    if not np.isfinite(anchor) or d * (close - anchor) <= 0:
        lo = max(0, t - 20)
        anchor = float(ext[lo:t + 1].min() if d > 0 else ext[lo:t + 1].max())
        anchor_is = f"the {'lowest low' if d > 0 else 'highest high'} of the last 20 days"
    swings = [x for x in ev["swings"] if x.confirmed <= t]
    # After the turn, a trade entered now is wrong below the latest higher low (or above the latest
    # lower high) if one has formed since the extreme: a nearer, more useful stop than the extreme.
    hits = np.flatnonzero(np.abs(ext[:t + 1] - anchor) < 1e-9)
    since = int(hits[-1]) if len(hits) else -1
    kind = "low" if d > 0 else "high"
    later = [x for x in swings if x.kind == kind and x.index > since and d * (x.price - anchor) > 0
             and d * (close - x.price) >= 1.0 * atr]
    if later:
        anchor = float(later[-1].price)
        anchor_is = "the last higher low" if d > 0 else "the last lower high"
    invalidation = float(anchor - d * STOP_BUFFER_ATR * atr)
    risk = abs(close - invalidation)
    lv = find_levels(swings, close, atr, t)
    names: list[tuple[float, str]] = []
    side_levels = lv["resistance"] if d > 0 else lv["support"]
    names += [(x["price"], "resistance" if d > 0 else "support") for x in side_levels]
    sd = ev["sd"]
    key_price = sd["hi_price"].iloc[t] if d > 0 else sd["lo_price"].iloc[t]
    key_label = sd["hi_label"].iloc[t] if d > 0 else sd["lo_label"].iloc[t]
    if np.isfinite(key_price) and key_label in (("LH", "EH") if d > 0 else ("HL", "EL")):
        names.append((float(key_price), "the last lower high" if d > 0 else "the last higher low"))
    for col, word in (("sma50", "the 50-day average"), ("sma200", "the 200-day average")):
        v = float(df[col].iloc[t])
        if np.isfinite(v):
            names.append((v, word))
    ahead = sorted({(round(p, 4), w) for p, w in names if d * (p - close) >= 0.5 * risk}, key=lambda x: d * (x[0] - close))
    t1 = ahead[0] if ahead else (close + d * 2 * risk, "two times the risk")
    later = [x for x in ahead[1:] if d * (x[0] - t1[0]) >= 0.5 * risk]
    t2 = later[0] if later else (t1[0] + d * risk, "one more time the risk beyond target 1")
    # Only the last lower high (higher low) confirms: a close above a higher high says nothing new.
    key_ok = np.isfinite(key_price) and key_label in (("LH", "EH") if d > 0 else ("HL", "EL"))
    confirm_price = float(key_price) if (key_ok and d * (key_price - close) > 0) else None
    return {"close": close, "atr": atr, "anchor": float(anchor), "invalidation": invalidation,
            "invalidation_is": f"a quarter of a daily range beyond {anchor_is}",
            "target_1": float(t1[0]), "target_1_is": t1[1], "target_2": float(t2[0]), "target_2_is": t2[1],
            "risk_per_share": risk, "confirms_above" if d > 0 else "confirms_below": confirm_price}


def _evidence(ev: dict[str, Any], side: str, t: int, df: pd.DataFrame) -> list[dict[str, Any]]:
    s, d = ev[side], (1 if side == "bottom" else -1)
    rsi = float(df["rsi"].iloc[t]) if np.isfinite(df["rsi"].iloc[t]) else None
    texts = {
        "move": f"The {'fall' if d > 0 else 'rise'} over the last {MOVE_WINDOW} days is {s['move_atr'][t]:.1f} daily ranges.",
        "rsi_extreme": f"RSI {'reached 30 or below' if d > 0 else 'reached 70 or above'} in the last five days"
                       + (f" (now {rsi:.0f})." if rsi is not None else "."),
        "stretch": f"The close was {'two or more daily ranges below' if d > 0 else 'two or more daily ranges above'} the "
                   f"20-day average, or outside the Bollinger band, in the last five days.",
        "rsi_divergence": ("The latest low is below the last swing low, but RSI is higher than it was then: selling "
                           "is losing force." if d > 0 else
                           "The latest high is above the last swing high, but RSI is lower than it was then: buying "
                           "is losing force."),
        "macd_divergence": f"The MACD histogram is {'less negative' if d > 0 else 'less positive'} at the new "
                           f"{'low' if d > 0 else 'high'} than at the last one.",
        "climax": f"A day in the last five on twice the usual volume, with a wide range, that closed well "
                  f"{'off its low' if d > 0 else 'off its high'}: often a {'selling' if d > 0 else 'buying'} climax.",
        "candle": "A " + ("hammer, bullish engulfing, Bollinger reclaim or RSI leaving oversold" if d > 0 else
                          "shooting star, bearish engulfing, Bollinger rejection or RSI leaving overbought")
                  + " in the last three days.",
        "swing_turn": f"A {'higher low' if d > 0 else 'lower high'} has formed in the last 30 days.",
        "structure_break": f"The stock closed {'above the last lower high' if d > 0 else 'below the last higher low'} in "
                           f"the last 20 days, or the trend engine left its {'downtrend' if d > 0 else 'uptrend'}.",
        "average_20": f"The close is back {'above' if d > 0 else 'below'} a {'rising' if d > 0 else 'falling'} 20-day average.",
        "average_50": f"The close is {'above' if d > 0 else 'below'} the 50-day average.",
        "volume": f"More shares traded on {'up' if d > 0 else 'down'} days than {'down' if d > 0 else 'up'} days over "
                  "the last ten.",
    }
    out = []
    for kind, keys in KIND_WORDS.items():
        for k in keys:
            pts = float(s["points"][k][t])
            out.append({"key": k, "kind": kind, "name": EVIDENCE_WORDS[k], "weight": WEIGHTS[k],
                        "points": round(pts, 1), "met": pts > 0, "text": texts[k]})
    return out


def describe(side: str, stage: str, lv: dict[str, Any], symbol: str) -> dict[str, str]:
    d = 1 if side == "bottom" else -1
    noun = "bottom" if d > 0 else "top"
    conf = lv.get("confirms_above") if d > 0 else lv.get("confirms_below")
    confirm = (f"It would be confirmed by a close {'above' if d > 0 else 'below'} ${conf:,.2f} "
               f"({'the last lower high' if d > 0 else 'the last higher low'})." if conf else
               "It would be confirmed by a swing the other way and a close back across the 20-day average.")
    wrong = f"It is wrong on a close {'below' if d > 0 else 'above'} ${lv['invalidation']:,.2f}."
    text = {
        "CONFIRMED": f"Confirmed {'reversal up' if d > 0 else 'reversal down'}: {symbol}'s swings or trend have turned "
                     f"after a long {'fall' if d > 0 else 'rise'}. {wrong}",
        "CANDIDATE": f"Potential {noun}: signs of exhaustion, not yet confirmed. {confirm} {wrong}",
        "WATCH": f"Stretched after a long {'fall' if d > 0 else 'rise'}, with no reversal sign yet. Stretched can stay "
                 f"stretched; wait for a sign before treating this as a {noun}.",
        "FAILED": f"A potential {noun} failed: the {'low' if d > 0 else 'high'} was taken out on a close.",
        "NONE": f"No {'fall' if d > 0 else 'rise'} long enough to look for a {noun}.",
    }[stage]
    return {"text": text, "confirm": confirm, "wrong": wrong}


def side_view(df: pd.DataFrame, ev: dict[str, Any], side: str, t: int, symbol: str) -> dict[str, Any]:
    s = ev[side]
    stage = str(s["stage"][t])
    lv = levels(df, ev, side, t)
    since = t
    while since > 0 and s["stage"][since - 1] == stage:
        since -= 1
    ex = _evidence(ev, side, t, df)
    return {"side": side, "stage": stage, "score": round(float(s["score"][t]), 1),
            "exhaustion": round(sum(e["points"] for e in ex if e["kind"] == "exhaustion"), 1),
            "confirmation": round(sum(e["points"] for e in ex if e["kind"] == "confirmation"), 1),
            "since": pd.Timestamp(df["date"].iloc[since]).strftime("%Y-%m-%d"), "evidence": ex, "levels": lv,
            **describe(side, stage, lv, symbol)}


def analyse(df: pd.DataFrame, symbol: str, trend_states: pd.DataFrame | None = None,
            ev: dict[str, Any] | None = None) -> dict[str, Any]:
    """Both sides on the last bar, with the reading on the last finished bar beside it when today's bar
    is unfinished. `ev` is a result of evaluate() on the same frame, to save working it out again."""
    if len(df) < 80:
        raise ValueError("At least 80 daily bars are needed to look for a reversal")
    ev = ev if ev is not None else evaluate(df, trend_states)
    complete = df["bar_complete"].to_numpy(dtype=bool) if "bar_complete" in df.columns else np.ones(len(df), bool)
    last = len(df) - 1
    done = np.flatnonzero(complete)
    ci = int(done[-1]) if len(done) else last

    def day(t: int, status: str) -> dict[str, Any]:
        b, tp = side_view(df, ev, "bottom", t, symbol), side_view(df, ev, "top", t, symbol)
        lead = b if (STAGE_RANK[b["stage"]], b["score"]) >= (STAGE_RANK[tp["stage"]], tp["score"]) else tp
        if STAGE_RANK[lead["stage"]] == 0:
            headline = "No reversal in sight: neither a potential bottom nor a potential top."
        else:
            headline = lead["text"]
        return {"date": pd.Timestamp(df["date"].iloc[t]).strftime("%Y-%m-%d"), "status": status, "bottom": b,
                "top": tp, "leading": lead["side"] if STAGE_RANK[lead["stage"]] else None, "headline": headline}

    confirmed = day(ci, "CONFIRMED")
    provisional = day(last, "PROVISIONAL") if not complete[last] else None
    start = max(0, len(df) - 252)
    daily = [{"date": pd.Timestamp(df["date"].iloc[t]).strftime("%Y-%m-%d"), "close": float(df["close"].iloc[t]),
              "provisional": not bool(complete[t]),
                "bottom": str(ev["bottom"]["stage"][t]), "top": str(ev["top"]["stage"][t]),
                "bottom_score": round(float(ev["bottom"]["score"][t]), 1), "top_score": round(float(ev["top"]["score"][t]), 1)}
               for t in range(start, len(df))]
    current = provisional or confirmed
    return {"version": REVERSAL_ENGINE_VERSION, "symbol": symbol, "as_of": current["date"], "status": current["status"],
            "confirmed": confirmed, "provisional": provisional, "daily": daily,
            "note": "A true bottom or top is only known afterwards. These are the signs that have tended to come with "
                    "one; points are evidence, not chances."}
