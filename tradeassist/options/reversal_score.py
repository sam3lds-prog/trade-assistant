"""Scoring a call or put for a reversal: is this contract a good way to trade this turn?

The thesis comes from the reversal engine (reversal.py): a potential or confirmed bottom is a
bet on a rise, so calls; a top, puts. The stock target is the first level in the way (the last
lower high, resistance, the 50-day average), the invalidation is just beyond the reversal's
low (or high), and the horizon is in trading days. The options evaluator values each contract
in its scenario matrix; this module scores it out of 100:

    reversal setup      30   the stage (confirmed, candidate, watch) and the evidence points
    payoff              20   gain at target 1 against the loss if invalidated, and return on premium
    target in reach     15   how far target 1 is, in the moves option prices expect by the horizon
    breakeven           10   how much of the move to target 1 the contract needs just to break even
    volatility price     5   implied volatility against what the stock has actually been doing
    time decay          10   the value lost by the horizon if the stock does not move
    liquidity           10   the bid-ask spread and open interest

GOOD 70 or more, FAIR 50 to 69, POOR under 50. A contract on the evaluator's avoid list is
AVOID whatever its score, and earnings before expiration take 10 points off (implied volatility
usually falls after the report). Some things cap the verdict however high the score: no
potential bottom or top (POOR at best), only a stretched move with no sign yet (FAIR), a target
more than one and a half expected moves away, or losing more than half the premium if the
stock does not move (FAIR). The score summarises these checks. It is not a probability.
"""

from __future__ import annotations

import math
from datetime import date, datetime
from typing import Any

import numpy as np

from ..simulate import eval_moment, expiry_moment, position_pnl
from .evaluator import _leg, horizon_date

WEIGHTS = {"setup": 30, "payoff": 20, "reach": 15, "breakeven": 10, "volatility": 5, "decay": 10, "liquidity": 10}
WORDS = {"setup": "Reversal setup", "payoff": "Payoff", "reach": "Target in reach", "breakeven": "Breakeven",
         "volatility": "Price of volatility", "decay": "Time decay", "liquidity": "Liquidity"}
FAR_TARGET_SD = 1.5          # a target beyond this many expected moves caps the verdict at FAIR
HEAVY_DECAY = 0.5            # losing more than this share of the premium if the stock stays put caps it too
LATE_ATR = 4.0               # a stop this many daily ranges away means the turn is already well under way
STAGE_FIT = {"CONFIRMED": 1.0, "CANDIDATE": 0.65, "WATCH": 0.3, "NONE": 0.0, "FAILED": 0.0}
STAGE_WORDS = {"CONFIRMED": "Confirmed reversal", "CANDIDATE": "Potential {noun}", "WATCH": "Stretched, no sign yet",
               "NONE": "No {noun} in sight", "FAILED": "Failed {noun}"}
VERDICT_ORDER = ("AVOID", "POOR", "FAIR", "GOOD")


def _interp(x: float, pts: list[tuple[float, float]]) -> float:
    xs, ys = zip(*pts)
    return float(np.interp(x, xs, ys))


def verdict(score: float, avoid: bool, cap: str | None = None) -> str:
    if avoid:
        return "AVOID"
    v = "GOOD" if score >= 70 else "FAIR" if score >= 50 else "POOR"
    if cap and VERDICT_ORDER.index(cap) < VERDICT_ORDER.index(v):
        return cap
    return v


def stage_cap(side: dict[str, Any]) -> tuple[str | None, str | None]:
    """However good the contract, a reversal trade is only as good as the reversal."""
    noun = side["side"]
    if side["stage"] in ("NONE", "FAILED"):
        return "POOR", (f"No potential {noun} on the last finished bar, so there is no reversal to trade: "
                        "at best POOR.")
    if side["stage"] == "WATCH":
        return "FAIR", f"Stretched, but no sign of a {noun} yet: at best FAIR until one appears."
    return None, None


def score_contract(k: dict[str, Any], side: dict[str, Any], thesis: dict[str, Any], hv20: float | None,
                   iv_rank: float | None, earnings_before_expiry: bool) -> dict[str, Any]:
    """Score one contract from the options evaluator (`k`) for the reversal `side`."""
    parts: dict[str, tuple[float, str]] = {}
    fit = STAGE_FIT.get(side["stage"], 0.0)
    ev_share = min(1.0, side["score"] / 60.0)                    # 60 points of evidence count as full
    setup = WEIGHTS["setup"] * (0.6 * fit + 0.4 * ev_share)
    word = STAGE_WORDS[side["stage"]].format(noun=side["side"])
    parts["setup"] = (setup, f"{word}, with {side['score']:.0f} evidence points "
                             f"({side['exhaustion']:.0f} exhaustion, {side['confirmation']:.0f} confirmation).")
    if k.get("premium") is None:
        return {"score": 0.0, "verdict": "AVOID", "components": [], "reasons": k.get("avoid", []), "earnings_note": "", "caps": [],
                "note": "No two-sided quote, so the contract cannot be valued or scored."}
    rr = k.get("reward_to_risk") or 0.0
    rop = k.get("return_on_premium") or 0.0
    payoff = WEIGHTS["payoff"] * (0.7 * _interp(rr, [(0, 0), (1, 0.3), (2, 0.65), (3, 1.0)])
                                  + 0.3 * _interp(rop, [(0, 0), (0.5, 0.4), (1.0, 0.7), (2.0, 1.0)]))
    parts["payoff"] = (payoff, f"At target 1 it makes {k['at_target']:+,.0f} for {k['if_invalidated']:,.0f} if "
                               f"invalidated ({rr:.2f} to 1), {100 * rop:+.0f}% on the premium.")
    caps: list[tuple[str, str]] = []
    atr = (side.get("levels") or {}).get("atr")
    if atr and atr > 0:
        away = abs(thesis["spot"] - thesis["invalidation"]) / atr
        if away > LATE_ATR:
            caps.append(("FAIR", f"The invalidation price is {away:.1f} daily ranges away: the turn is already well under "
                                 "way, so a loss would be large before the idea is proved wrong. At best FAIR; a "
                                 "pullback toward the stop would be a better entry."))
    vol_now = thesis.get("atm_iv") or k.get("iv")
    years = max(int(thesis.get("horizon") or 20), 1) / 252.0
    if vol_now and thesis["target_1"] > 0 and thesis["spot"] > 0:
        z = abs(math.log(thesis["target_1"] / thesis["spot"])) / (vol_now * math.sqrt(years))
        reach = WEIGHTS["reach"] * _interp(z, [(0.5, 1.0), (1.0, 0.6), (1.5, 0.25), (2.0, 0.0)])
        parts["reach"] = (reach, f"Target 1 is {z:.1f} times the move option prices expect over "
                                 f"{thesis.get('horizon', 20)} trading days ({100 * vol_now:.0f}% implied volatility).")
        if z > FAR_TARGET_SD:
            caps.append(("FAIR", f"Target 1 is {z:.1f} expected moves away for the time allowed: at best FAIR. A later "
                                 "expiration or a nearer target would fit better."))
    else:
        parts["reach"] = (WEIGHTS["reach"] * 0.5, "No implied volatility to measure the target against; scored half.")
    be = k.get("breakeven_at_horizon")
    move = thesis["target_1"] - thesis["spot"]
    if be is not None and move:
        need = (be - thesis["spot"]) / move                     # share of the move to target 1 needed to break even
        breakeven = WEIGHTS["breakeven"] * _interp(need, [(0.0, 1.0), (0.5, 0.8), (0.8, 0.4), (1.0, 0.15), (1.2, 0.0)])
        parts["breakeven"] = (breakeven, f"Breaks even by the horizon at ${be:,.2f}, {100 * max(need, 0):.0f}% of the way "
                                         "to target 1.")
    else:
        parts["breakeven"] = (0.0, "No breakeven before the horizon within the range checked.")
    iv = k.get("iv")
    if iv and hv20 and hv20 > 0:
        ratio = iv / hv20
        vol = WEIGHTS["volatility"] * _interp(ratio, [(0.9, 1.0), (1.1, 0.85), (1.4, 0.45), (1.8, 0.0)])
        text = f"Implied volatility {100 * iv:.0f}% against {100 * hv20:.0f}% actual over 20 days ({ratio:.2f} times)."
        if iv_rank is not None:
            vol *= _interp(iv_rank, [(0, 1.0), (50, 0.9), (80, 0.6), (100, 0.4)])
            text += f" Implied volatility rank {iv_rank:.0f}."
        parts["volatility"] = (vol, text)
    else:
        parts["volatility"] = (WEIGHTS["volatility"] * 0.5, "No volatility comparison available; scored half.")
    flat = k.get("if_unchanged")
    if flat is not None and k.get("premium"):
        lost = max(0.0, -flat) / k["premium"]
        decay = WEIGHTS["decay"] * _interp(lost, [(0.0, 1.0), (0.15, 0.8), (0.3, 0.45), (0.5, 0.0)])
        parts["decay"] = (decay, f"If the stock does not move, it loses {100 * lost:.0f}% of the premium by the horizon.")
        if lost > HEAVY_DECAY:
            caps.append(("FAIR", f"Loses {100 * lost:.0f}% of the premium if the stock goes nowhere: the move has to "
                                 "come quickly. At best FAIR."))
    else:
        parts["decay"] = (0.0, "Time decay could not be measured.")
    spread = k.get("spread_pct")
    oi = k.get("open_interest") or 0
    liq = WEIGHTS["liquidity"] * (0.7 * (_interp(spread, [(0, 1.0), (3, 0.9), (6, 0.5), (10, 0.0)]) if spread is not None else 0)
                                  + 0.3 * _interp(math.log10(oi + 1), [(1, 0.0), (2, 0.5), (3, 1.0)]))
    parts["liquidity"] = (liq, f"Spread {spread:.1f}% of the midpoint, open interest {oi:,.0f}." if spread is not None
                          else "No two-sided quote.")
    total = sum(v for v, _ in parts.values())
    reasons = list(k.get("avoid", []))
    if earnings_before_expiry:
        total -= 10
        reasons_text = "Earnings before expiration: 10 points off."
    else:
        reasons_text = ""
    total = max(0.0, total)
    cap, cap_text = stage_cap(side)
    if cap:
        caps.insert(0, (cap, cap_text))
    worst = min((c for c, _ in caps), key=VERDICT_ORDER.index) if caps else None
    return {"score": round(total, 1), "verdict": verdict(total, bool(reasons), worst), "reasons": reasons,
            "earnings_note": reasons_text, "caps": [t for _, t in caps],
            "components": [{"key": key, "name": WORDS[key], "weight": WEIGHTS[key], "points": round(v, 1), "why": why}
                           for key, (v, why) in parts.items()],
            "note": "The score summarises these checks. It is not a probability."}


def analog_outcomes(k: dict[str, Any], row: dict[str, Any], analysis: Any, cases: list[dict[str, Any]], *,
                    direction: int, spot: float, invalidation: float, horizon: int, rate: float, contracts: int,
                    now: datetime, source: str, atr_pct: float | None = None) -> dict[str, Any] | None:
    """What the contract would make if the stock moved as it did after past reversals of this kind.

    Each past case gives the stock's return over `horizon` trading days from the next day's open and
    the worst price reached in the next 20 days. With `atr_pct` (this stock's daily range as a share
    of its price now) and the case's own, the case's moves are rescaled to this stock's volatility,
    so a calm stock's past cases are not read as small moves for a volatile one (the scale is kept
    between a quarter and four times). A case whose worst price crossed the invalidation counts as
    stopped out at the evaluator's invalidation value (the exit rule is on the stock); otherwise the
    contract is valued at the case's price on the horizon date with today's implied volatility.
    Path, volatility changes and gaps through the stop are not modelled."""
    col = f"fwd_{horizon}"
    stop_ret = invalidation / spot - 1.0
    usable = [c for c in cases if c.get(col) is not None and math.isfinite(c[col])]
    if not usable or k.get("premium") is None:
        return None
    today = now.date()
    exp_day = date.fromisoformat(k["expiration"])
    h_day = min(horizon_date(today, horizon), exp_day)
    moment = expiry_moment(k["expiration"]) if h_day == exp_day else eval_moment(h_day, now)
    leg = _leg(row, analysis, contracts)

    def scale(c: dict[str, Any]) -> float:
        theirs = c.get("atr_pct")
        if not atr_pct or not theirs or not (math.isfinite(atr_pct) and math.isfinite(theirs)) or theirs <= 0:
            return 1.0
        return float(np.clip(atr_pct / theirs, 0.25, 4.0))

    def hit(c: dict[str, Any], f: float) -> bool:
        worst = c.get("low_20") if direction > 0 else c.get("high_20")      # the worst price for this side
        return worst is not None and math.isfinite(worst) and direction * (worst * f - stop_ret) <= 0

    scales = [scale(c) for c in usable]
    prices = np.array([spot * max(0.01, 1.0 + c[col] * f) for c, f in zip(usable, scales)], dtype=float)
    pnl = position_pnl([leg], prices, moment, rate, 0.0)
    stopped = np.array([hit(c, f) for c, f in zip(usable, scales)], dtype=bool)
    pnl = np.where(stopped, k["if_invalidated"], pnl)
    premium = k["premium"]
    edges = np.linspace(-premium, max(float(pnl.max()), premium * 0.25), 13)
    counts, _ = np.histogram(np.clip(pnl, edges[0], edges[-1]), bins=edges)
    scaled = any(f != 1.0 for f in scales)
    return {"source": source, "cases": int(len(pnl)), "horizon": horizon, "horizon_date": h_day.isoformat(),
            "profitable": float(np.mean(pnl > 0)), "stopped_out": float(np.mean(stopped)),
            "median": float(np.median(pnl)), "mean": float(np.mean(pnl)),
            "p10": float(np.percentile(pnl, 10)), "p90": float(np.percentile(pnl, 90)),
            "best": float(pnl.max()), "worst": float(pnl.min()), "premium": premium, "volatility_scaled": scaled,
            "histogram": {"edges": [float(e) for e in edges], "counts": [int(c) for c in counts]},
            "note": f"{len(pnl)} past cases {source}: the stock's move over {horizon} trading days after each"
                    + (", rescaled to this stock's daily range," if scaled else ",")
                    + " applied to today's price; cases that reached the invalidation within 20 days count as "
                      "stopped out. Today's implied volatility is kept, and the path between is ignored. Past "
                      "cases, not a forecast."}


def outcome_table(k: dict[str, Any], thesis: dict[str, Any], names: dict[str, str]) -> list[dict[str, Any]]:
    """The contract's gain or loss in the plain cases: the evaluator's scenario matrix in words."""
    m = {(r["when"], r["iv_shift"]): r for r in k.get("scenarios", [])}
    base, half = m.get(("horizon", 0.0)), m.get(("halfway", 0.0))
    low, high = m.get(("horizon", -0.05)), m.get(("horizon", 0.05))
    if not base:
        return []
    prem = k["premium"]
    rows = [
        ("target_2", thesis["target_2"], base["target_2"], f"Reaches target 2 ({names.get('target_2', 'target 2')}) by {base['date']}"),
        ("target_1", thesis["target_1"], base["target_1"], f"Reaches target 1 ({names.get('target_1', 'target 1')}) by {base['date']}"),
        ("target_1_early", thesis["target_1"], half["target_1"] if half else None, f"Reaches target 1 halfway there, by {half['date']}" if half else ""),
        ("target_1_iv_down", thesis["target_1"], low["target_1"] if low else None, "Reaches target 1, but implied volatility falls 5 points"),
        ("target_1_iv_up", thesis["target_1"], high["target_1"] if high else None, "Reaches target 1, and implied volatility rises 5 points"),
        ("unchanged", thesis["spot"], k["if_unchanged"], f"Goes nowhere: still at today's price on {base['date']}"),
        ("invalidated", thesis["invalidation"], k["if_invalidated"], "Fails: closes at the invalidation price and you exit"),
    ]
    return [{"key": key, "case": text, "stock_price": px, "pnl": v, "share_of_premium": v / prem if prem else None}
            for key, px, v, text in rows if v is not None and text]


POOR_PAST_SHARE = 0.35       # fewer past cases than this share profitable caps the verdict (with enough cases)


def weigh_past_cases(sc: dict[str, Any], pc: dict[str, Any] | None, min_cases: int) -> None:
    """Let the past cases speak: with enough of them, a contract that would rarely have paid is at best
    FAIR; with too few, the same finding is shown as a caution and the verdict is left alone."""
    sc.setdefault("cautions", [])
    if not pc or pc.get("error") or not pc.get("cases"):
        return
    share = pc["profitable"]
    if share >= POOR_PAST_SHARE:
        return
    text = (f"Only {100 * share:.0f}% of {pc['cases']} past cases {pc['source']} would have been profitable with this "
            f"contract ({100 * pc['stopped_out']:.0f}% reached the invalidation)")
    if pc["cases"] >= min_cases:
        sc["caps"].append(text + ": at best FAIR.")
        if sc["verdict"] == "GOOD":
            sc["verdict"] = "FAIR"
    else:
        sc["cautions"].append(text + ". Too few cases to cap the verdict, but worth weighing.")
