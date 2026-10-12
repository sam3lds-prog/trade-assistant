"""Position evaluator: grades a stock trade idea, long or short, before or after entry.

Given a symbol, a direction and optionally an entry, an invalidation price and targets, it
works out the missing levels from the chart (the swing that would prove the idea wrong, the
support and resistance in the way), the reward-to-risk, a size that fits the risk limit, and
six component scores:

    trend alignment     30   does the trend engine's state agree with the direction
    market regime       15   does the market regime favour that direction
    reward to risk      20   distance to target 1 against distance to the invalidation price
    past cases          15   how similar past readings turned out (signal history), if built
    volatility and fit  10   the stop's distance in daily ranges, unusual volatility, liquidity
    catalysts           10   earnings inside the holding period

The weighted score gives a grade: A (80+), B (65+), C (50+), D (35+), otherwise AVOID. Some
findings cap the grade whatever the score (listed in `caps`): a short against a strong
uptrend, an invalidation price on the wrong side, reward under 0.75 times the risk, data
that failed its checks, earnings within two trading days. The grade summarises these checks.
It is not a probability, and the past-case frequencies it uses are counts, not forecasts.

For a short, short-sale risks are listed separately: this app cannot see borrow availability,
borrow fees or short interest, and says so; it can see squeeze-like run-ups, gaps against
the position and an uptrend it would be fighting.
"""

from __future__ import annotations

import math
from datetime import date, datetime
from typing import Any

import numpy as np
import pandas as pd

from .market import complete_only
from .optionmath import NY
from .structure import SWING_ATR, analyse as analyse_structure
from .versions import EVALUATOR_VERSION

WEIGHTS = {"trend": 30, "regime": 15, "reward_risk": 20, "history": 15, "volatility": 10, "catalysts": 10}
COMPONENT_WORDS = {"trend": "Trend alignment", "regime": "Market regime", "reward_risk": "Reward to risk",
                   "history": "Past cases", "volatility": "Volatility and fit", "catalysts": "Catalysts"}
GRADES = (("A", 80.0), ("B", 65.0), ("C", 50.0), ("D", 35.0))
GRADE_ORDER = ["A", "B", "C", "D", "AVOID"]
TREND_FIT = {"STRONG_UP": 100, "UP": 85, "UP_WEAKENING": 50, "NEUTRAL": 40, "DOWN_DEVELOPING": 20, "DOWN": 10,
             "STRONG_DOWN": 0}
REGIME_FIT_LONG = {"BULL_CALM": 100, "BULL_VOLATILE": 75, "RANGE": 55, "TRANSITION": 40, "BEAR_CALM": 25,
                   "BEAR_VOLATILE": 15}
REGIME_FIT_SHORT = {"BEAR_VOLATILE": 90, "BEAR_CALM": 100, "TRANSITION": 45, "RANGE": 50, "BULL_VOLATILE": 25,
                    "BULL_CALM": 15}
STOP_BUFFER_ATR = 0.25      # the suggested invalidation sits this far beyond the swing or level
FALLBACK_STOP_ATR = 2.0
MIN_RR = 0.75
SHORT_TARGET_FLOOR = 0.05   # 2.7: a short's suggested target is never below a twentieth of the entry


class EvaluationError(ValueError):
    pass


def grade_for(score: float) -> str:
    for g, floor in GRADES:
        if score >= floor:
            return g
    return "AVOID"


def cap(grade: str, ceiling: str) -> str:
    return grade if GRADE_ORDER.index(grade) >= GRADE_ORDER.index(ceiling) else ceiling


def _interp(x: float, points: list[tuple[float, float]]) -> float:
    xs, ys = zip(*points)
    return float(np.interp(x, xs, ys))


def suggest_levels(df: pd.DataFrame, direction: int, entry: float, reversal: float = SWING_ATR) -> dict[str, Any]:
    """Invalidation and targets from the swing structure and support and resistance, on finished bars."""
    done = complete_only(df)
    st = analyse_structure(done, reversal)
    atr = float(done["atr"].iloc[-1]) if math.isfinite(float(done["atr"].iloc[-1])) else entry * 0.02
    swings = st["swings"]
    lows = [s for s in swings if s["kind"] == "low" and s["price"] < entry]
    highs = [s for s in swings if s["kind"] == "high" and s["price"] > entry]
    support = [lv["price"] for lv in st["levels"]["support"] if lv["price"] < entry]
    resistance = [lv["price"] for lv in st["levels"]["resistance"] if lv["price"] > entry]
    notes = []
    if direction > 0:
        anchor = lows[-1]["price"] if lows else (support[0] if support else None)
        if anchor is not None and entry - anchor <= 6 * atr:
            invalidation = anchor - STOP_BUFFER_ATR * atr
            notes.append(f"Invalidation a quarter of a daily range below the last swing low or support (${anchor:,.2f}).")
        else:
            invalidation = entry - FALLBACK_STOP_ATR * atr
            notes.append("No swing low or support close enough below the entry; invalidation set two daily ranges below.")
        targets = sorted(resistance)
    else:
        anchor = highs[-1]["price"] if highs else (resistance[0] if resistance else None)
        if anchor is not None and anchor - entry <= 6 * atr:
            invalidation = anchor + STOP_BUFFER_ATR * atr
            notes.append(f"Invalidation a quarter of a daily range above the last swing high or resistance (${anchor:,.2f}).")
        else:
            invalidation = entry + FALLBACK_STOP_ATR * atr
            notes.append("No swing high or resistance close enough above the entry; invalidation set two daily ranges "
                         "above.")
        targets = sorted(support, reverse=True)
    risk = abs(entry - invalidation)
    # A level too close to the entry is not worth a target; then the target is a multiple of the risk.
    targets = [t for t in targets if abs(t - entry) >= 0.5 * risk]
    t1 = targets[0] if targets else entry + direction * 2 * risk
    t2 = targets[1] if len(targets) > 1 else entry + direction * 3 * risk
    if not targets:
        notes.append("No support or resistance in the way; targets set at two and three times the risk.")
    elif len(targets) == 1:
        notes.append("Target 1 at the first level in the way; target 2 at three times the risk.")
    else:
        notes.append("Targets at the first two levels in the way.")
    if direction < 0:
        # 2.7: a short cannot make more than the price. A multiple of a wide risk can land at or below zero;
        # such a target is set at a twentieth of the entry instead, and the note says so.
        floor = SHORT_TARGET_FLOOR * entry
        if t1 < floor or t2 < floor:
            notes.append(f"A target at a multiple of the risk would be at or below zero; set at ${floor:,.2f}, a "
                         "twentieth of the entry, instead.")
            t1, t2 = max(t1, floor), max(t2, floor)
    return {"invalidation": float(invalidation), "target_1": float(t1), "target_2": float(t2), "atr": atr,
            "structure_state": st["state"], "notes": notes,
            "levels": {"support": st["levels"]["support"], "resistance": st["levels"]["resistance"]}}


def evaluate(*, symbol: str, direction: int, df: pd.DataFrame, entry: float, trend: dict[str, Any],
             regime: dict[str, Any] | None, data_quality: dict[str, Any] | None, earnings: dict[str, Any] | None,
             signals: dict[str, Any] | None, account_size: float, risk_pct: float, invalidation: float | None = None,
             target_1: float | None = None, target_2: float | None = None, horizon: int = 20,
             today: date | None = None) -> dict[str, Any]:
    """Grade one stock trade idea. `direction` is +1 for long, -1 for short."""
    if direction not in (1, -1):
        raise EvaluationError("direction must be LONG or SHORT")
    if not (entry and entry > 0):
        raise EvaluationError("An entry price above zero is needed")
    today = today or datetime.now(NY).date()
    sug = suggest_levels(df, direction, entry)
    inv = invalidation if invalidation is not None else sug["invalidation"]
    t1 = target_1 if target_1 is not None else sug["target_1"]
    t2 = target_2 if target_2 is not None else sug["target_2"]
    caps: list[dict[str, str]] = []
    if direction * (entry - inv) <= 0:
        caps.append({"grade": "AVOID", "reason": f"The invalidation price ${inv:,.2f} is on the wrong side of the entry "
                                                 f"${entry:,.2f} for a {'long' if direction > 0 else 'short'}."})
    if direction * (t1 - entry) <= 0:
        caps.append({"grade": "AVOID", "reason": f"Target 1 (${t1:,.2f}) is on the wrong side of the entry."})
    risk_ps = abs(entry - inv)
    rr1 = abs(t1 - entry) / risk_ps if risk_ps > 0 else 0.0
    rr2 = abs(t2 - entry) / risk_ps if risk_ps > 0 else 0.0
    if rr1 < MIN_RR:
        if target_1 is None:
            caps.append({"grade": "AVOID", "reason": f"The first level in the way (${t1:,.2f}) is only {rr1:.2f} times the "
                                                     "risk away. If you expect the stock to go through it, enter your own "
                                                     "target and grade it again."})
        else:
            caps.append({"grade": "AVOID", "reason": f"Target 1 pays {rr1:.2f} times the risk; under {MIN_RR} is not "
                                                     "worth it."})
    budget = account_size * risk_pct / 100.0
    shares = int(budget // risk_ps) if risk_ps > 0 else 0
    sizing = {"risk_per_share": risk_ps, "risk_budget": budget, "shares": shares,
              "position_value": shares * entry, "share_of_account": shares * entry / account_size if account_size else None,
              "note": f"Shares whose loss at the invalidation price is at most {risk_pct:g}% of the ${account_size:,.0f} "
                      "account. Gaps can make the loss larger."}

    day = trend["provisional"] or trend["confirmed"]
    comps: dict[str, dict[str, Any]] = {}

    # --- trend alignment ----------------------------------------------------------------------
    state = day["state"]
    fit = TREND_FIT[state] if direction > 0 else 100 - TREND_FIT[state]
    why = [f"Trend engine: {day['label'].lower()} ({day['confirmation']['level'].lower()}, "
           f"confidence {day['confidence']['level'].lower()}), "
           f"{'with' if fit >= 60 else 'against' if fit <= 40 else 'neither with nor against'} a "
           f"{'long' if direction > 0 else 'short'}."]
    if day["status"] == "PROVISIONAL":
        fit -= 10
        why.append("It is provisional: today's bar is not finished.")
    if day["confidence"]["level"] == "LOW":
        fit -= 10
        why.append("Low confidence in the reading.")
    comps["trend"] = {"score": max(0.0, min(100.0, fit)), "why": why}
    if direction < 0 and state == "STRONG_UP":
        caps.append({"grade": "D", "reason": "A short against a strong uptrend."})
    if direction > 0 and state == "STRONG_DOWN":
        caps.append({"grade": "D", "reason": "A long against a strong downtrend."})

    # --- market regime -------------------------------------------------------------------------
    if regime:
        g = regime["regime"]
        fit_map = REGIME_FIT_LONG if direction > 0 else REGIME_FIT_SHORT
        comps["regime"] = {"score": float(fit_map.get(g, 50)),
                           "why": [f"{regime['label']}: {(regime.get('provisional') or regime.get('confirmed') or {}).get('text', '')}".strip()]}
    else:
        comps["regime"] = {"score": 50.0, "why": ["Market regime unavailable; scored as neutral."]}

    # --- reward to risk ---------------------------------------------------------------------------
    comps["reward_risk"] = {"score": _interp(rr1, [(0.0, 0.0), (1.0, 40.0), (1.5, 60.0), (2.0, 80.0), (3.0, 100.0)]),
                            "why": [f"Target 1 pays {rr1:.2f} times the risk and target 2 {rr2:.2f} times."]}

    # --- past cases ---------------------------------------------------------------------------------
    hist_score, hist_why, analogs = 50.0, [], None
    if signals and signals.get("history_built"):
        sc = signals.get("score") or {}
        h10 = sc.get("h10") or {}
        bucket = h10.get("bucket")
        if bucket and bucket.get("cases", 0) >= 30 and bucket.get("frequency") is not None:
            freq = bucket["frequency"] if direction > 0 else 1 - bucket["frequency"]
            if h10.get("calibrated"):
                hist_score = _interp(freq, [(0.4, 0.0), (0.5, 50.0), (0.6, 100.0)])
                hist_why.append(f"On past days with a similar net score, the move went this way {100 * freq:.0f}% of the "
                                f"time over 10 days ({bucket['cases']:,} days); the score is calibrated on this data.")
            else:
                hist_why.append(f"Past days with a similar net score went this way {100 * freq:.0f}% of the time, but the "
                                "score is not calibrated, so this is not counted.")
        aligned = [s for s in signals.get("signals", []) if s["direction"] == direction and s["history"]["all"]["enough"]]
        against = [s for s in signals.get("signals", []) if s["direction"] == -direction and s["history"]["all"]["enough"]]
        if aligned or against:
            bits = []
            for s in aligned + against:
                h = s["history"]["same_regime"] if (s["history"].get("same_regime") or {}).get("enough") else s["history"]["all"]
                bits.append((s, h))
            good = [h["frequency"] for s, h in bits if s["direction"] == direction]
            bad = [h["frequency"] for s, h in bits if s["direction"] == -direction]
            edge = (np.mean(good) - 0.5 if good else 0.0) - (np.mean(bad) - 0.5 if bad else 0.0)
            hist_score = max(0.0, min(100.0, hist_score + 200 * edge))
            hist_why.append(f"{len(aligned)} recent signal{'s' if len(aligned) != 1 else ''} for this direction and "
                            f"{len(against)} against, with enough past cases; their record "
                            f"{'helps' if edge > 0.01 else 'hurts' if edge < -0.01 else 'is close to even'}.")
        key = f"trend:{state}"
        analogs = next((s for s in signals.get("signals", []) if s["key"] == key), None)
    else:
        hist_why.append("The signal history has not been built, so past cases are scored as neutral.")
    comps["history"] = {"score": hist_score, "why": hist_why}

    # --- volatility and fit -----------------------------------------------------------------------------
    atr = sug["atr"]
    stop_atr = risk_ps / atr if atr > 0 else float("nan")
    vol_score, vol_why = 100.0, []
    if math.isfinite(stop_atr):
        if stop_atr < 0.5:
            vol_score -= 50
            vol_why.append(f"The invalidation is only {stop_atr:.1f} daily ranges away: ordinary noise can reach it.")
        elif stop_atr > 4:
            vol_score -= 25
            vol_why.append(f"The invalidation is {stop_atr:.1f} daily ranges away, which makes the position small.")
        else:
            vol_why.append(f"The invalidation is {stop_atr:.1f} daily ranges away.")
    if day["volatility"].get("high"):
        vol_score -= 25
        vol_why.append("Daily ranges are wider than usual for this stock.")
    done = complete_only(df)
    dollar_vol = float((done["close"] * done["volume"]).tail(20).mean())
    if dollar_vol < 5e6:
        vol_score -= 30
        vol_why.append(f"Thin trading: about ${dollar_vol / 1e6:,.1f} million a day.")
    comps["volatility"] = {"score": max(0.0, vol_score), "why": vol_why}

    # --- catalysts ----------------------------------------------------------------------------------------
    cat_score, cat_why = 100.0, []
    if earnings and earnings.get("date"):
        when = date.fromisoformat(earnings["date"])
        days = int(np.busday_count(today, when)) if when >= today else -1
        est = " (estimated)" if earnings.get("estimated") else ""
        if 0 <= days <= 2:
            cat_score = 10.0
            cat_why.append(f"Earnings on {when:%b} {when.day}{est}, within two trading days: a gap past the invalidation "
                           "price is likely enough to plan for.")
            caps.append({"grade": "C", "reason": "Earnings within two trading days."})
        elif 0 <= days <= horizon:
            cat_score = 35.0
            cat_why.append(f"Earnings on {when:%b} {when.day}{est}, inside the {horizon}-day holding period.")
        else:
            cat_why.append(f"Next earnings {when:%b} {when.day}{est}, after the holding period.")
    else:
        cat_score = 60.0
        cat_why.append("The next earnings date is not known: check it before trading.")
    comps["catalysts"] = {"score": cat_score, "why": cat_why}

    # --- data -------------------------------------------------------------------------------------------
    if data_quality and data_quality.get("status") == "fail":
        caps.append({"grade": "AVOID", "reason": "The price data failed its checks."})

    total = sum(WEIGHTS[k] * comps[k]["score"] for k in WEIGHTS) / sum(WEIGHTS.values())
    grade = grade_for(total)
    for c in caps:
        grade = cap(grade, c["grade"])
    out = {"version": EVALUATOR_VERSION, "symbol": symbol, "direction": "LONG" if direction > 0 else "SHORT",
           "instrument_type": "STOCK", "entry": entry, "as_of": day["date"], "status": day["status"],
           "grade": grade, "score": round(total, 1), "caps": caps,
           "components": [{"key": k, "name": COMPONENT_WORDS[k], "weight": w, "score": round(comps[k]["score"], 1),
                           "why": comps[k]["why"]} for k, w in WEIGHTS.items()],
           "levels": {"invalidation": inv, "target_1": t1, "target_2": t2,
                      "invalidation_source": "yours" if invalidation is not None else "suggested",
                      "targets_source": "yours" if target_1 is not None else "suggested",
                      "suggested": {k: sug[k] for k in ("invalidation", "target_1", "target_2")},
                      "notes": sug["notes"], "atr": atr},
           "reward_risk": {"target_1": rr1, "target_2": rr2}, "sizing": sizing,
           "analogs": analogs, "trend_state": state, "regime": (regime or {}).get("regime"),
           "note": "The grade summarises these checks. It is not a probability."}
    if direction < 0:
        out["short_sale_risk"] = short_risks(done, day, earnings, today)
    out["what_would_change_it"] = changes(out, direction, state)
    return out


def short_risks(df: pd.DataFrame, day: dict[str, Any], earnings: dict[str, Any] | None, today: date) -> list[dict[str, Any]]:
    """What can go wrong with a short beyond the price moving up."""
    out = [{"level": "warn", "text": "Borrow availability and the borrow fee are not visible to this app. Check that "
                                     "your broker can lend the shares and what it charges per day before shorting."},
           {"level": "info", "text": "Short interest and days to cover are not available from the connected data, so "
                                     "crowding cannot be judged here."},
           {"level": "info", "text": "A short's loss has no ceiling: a gap above the invalidation price can cost much "
                                     "more than planned."}]
    close = df["close"].to_numpy(dtype=float)
    if len(close) > 21:
        run = close[-1] / close[-21] - 1
        vol = df["vol_ratio"].tail(5).max() if "vol_ratio" in df.columns else float("nan")
        if run > 0.30:
            out.append({"level": "fail" if (math.isfinite(vol) and vol > 2) else "warn",
                        "text": f"The stock is up {100 * run:.0f}% in a month"
                                + (" on heavy volume" if math.isfinite(vol) and vol > 2 else "")
                                + ": shorts in a run like this risk a squeeze."})
    if len(df) > 2:
        atr = df["atr"].to_numpy(dtype=float)
        gaps = (df["open"].to_numpy(dtype=float)[1:] - close[:-1]) / atr[:-1]
        gaps = gaps[-252:][np.isfinite(gaps[-252:])]
        if len(gaps):
            worst = float(gaps.max())
            if worst > 1.5:
                out.append({"level": "warn", "text": f"In the past year the stock has opened up to {worst:.1f} daily ranges "
                                                     "above the previous close: gaps against a short happen here."})
    if day["state"] in ("STRONG_UP", "UP"):
        out.append({"level": "warn", "text": "The trend engine reads an uptrend: the short is against it."})
    if earnings and earnings.get("date"):
        when = date.fromisoformat(earnings["date"])
        if today <= when and (when - today).days <= 30:
            out.append({"level": "warn", "text": f"Earnings on {when:%b} {when.day}: surprises gap shorts badly."})
    return out


def changes(ev: dict[str, Any], direction: int, state: str) -> list[str]:
    """Plain suggestions of what would raise the grade."""
    out = []
    comps = {c["key"]: c for c in ev["components"]}
    if comps["reward_risk"]["score"] < 60:
        out.append("A better entry (closer to the invalidation price) or a farther target would improve the reward to risk.")
    if comps["trend"]["score"] < 50:
        out.append(f"The trend would need to turn {'up' if direction > 0 else 'down'}; the engine reads "
                   f"{state.replace('_', ' ').lower()} now.")
    if comps["catalysts"]["score"] < 50:
        out.append("Waiting until after earnings removes the gap risk.")
    if comps["history"]["score"] == 50 and "not been built" in " ".join(comps["history"]["why"]):
        out.append("Build the signal history to have past cases count.")
    return out
