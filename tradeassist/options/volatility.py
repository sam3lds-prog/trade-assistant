"""Implied volatility across expirations and strikes, what it says about expected moves, and a
Monte Carlo check of option prices and odds.

Everything here reads the market's own option prices. The figures are what those prices imply
under a model (lognormal stock prices, Black-76), not forecasts of what will happen:

- **Term structure**: at-the-money implied volatility for each expiration. Usually later
  expirations carry a little more; when the nearest ones carry more ("inverted"), the market
  is pricing an event soon, often earnings.
- **Expected move**: one standard deviation of the stock price by expiration from the at-the-
  money volatility (S x vol x sqrt(t)), and the price of the at-the-money straddle, which is
  what the market charges for a move of either sign. The straddle is about 0.8 of the
  standard deviation for a lognormal stock.
- **Skew**: implied volatility of the 25-delta put minus that of the 25-delta call. Positive
  (puts dearer) is normal for stocks: protection against falls costs more.
- **Implied earnings move**: the extra variance an expiration just after earnings carries over
  the ones without the event, as one standard deviation of the earnings-day move.
- **Volatility against realized**: implied 30-day volatility against what the stock has
  actually moved over 10, 20 and 60 days.

`monte_carlo` simulates stock paths from the same inputs (risk-neutral drift from the
forward, the chain's volatility, an optional earnings jump) with a fixed seed. It prices the
option, which should agree with Black-76 within its standard error (a check, not a better
price); and it gives what a formula cannot: the chance the stock touches the strike before
expiration, and the spread of outcomes for a buyer at a given price.
"""

from __future__ import annotations

import math
from datetime import date
from typing import Any, Iterable, Sequence

import numpy as np
from scipy.special import ndtr

from ..chain import ChainAnalysis, atm_iv, mid_price, two_sided
from ..optionmath import black76_price, prob_finish_beyond

MIN_POINTS_FOR_SKEW = 3
FLAT_POINTS = 0.01                 # one volatility point: less than this between front and back is "flat"
TRADING_DAYS = 252
MC_SEED = 7
MC_MAX_CELLS = 5_000_000           # paths x steps kept in memory at once
MC_MAX_PATHS = 100_000


def _finite(x: Any) -> float | None:
    try:
        f = float(x)
    except (TypeError, ValueError):
        return None
    return f if math.isfinite(f) else None


def iv_at_delta(analysis: ChainAnalysis, option_type: str, target: float) -> float | None:
    """Implied volatility at a delta (0.25 for a call, -0.25 for a put), interpolated linearly
    between the two listed contracts either side of it. None when the chain does not reach it."""
    rows = sorted((float(c["delta"]), float(c["iv"])) for c in analysis.contracts
                  if c["option_type"] == option_type and two_sided(c) and _finite(c.get("delta")) is not None
                  and _finite(c.get("iv")) and 0.02 < abs(c["delta"]) < 0.98)
    if len(rows) < 2:
        return None
    for (d0, v0), (d1, v1) in zip(rows, rows[1:]):
        if d0 <= target <= d1:
            return v0 if d1 == d0 else v0 + (v1 - v0) * (target - d0) / (d1 - d0)
    return None


def atm_straddle(analysis: ChainAnalysis) -> dict[str, Any] | None:
    """The call and put at the listed strike nearest the forward, with two-sided quotes."""
    strikes = sorted({c["strike"] for c in analysis.contracts}, key=lambda k: abs(k - analysis.forward))
    for k in strikes[:3]:
        call = analysis.find_by_terms("call", k)
        put = analysis.find_by_terms("put", k)
        if call and put and two_sided(call) and two_sided(put):
            c_mid, p_mid = mid_price(call), mid_price(put)
            return {"strike": k, "call_mid": c_mid, "put_mid": p_mid, "price": c_mid + p_mid,
                    "ask": call["ask"] + put["ask"], "bid": call["bid"] + put["bid"]}
    return None


def expiration_row(analysis: ChainAnalysis, today: date, earnings: date | None) -> dict[str, Any]:
    """One expiration of the term structure."""
    exp = date.fromisoformat(analysis.expiration)
    iv = atm_iv(analysis)
    s = analysis.spot
    one_sd = s * iv * math.sqrt(analysis.t) if iv and analysis.t > 0 else None
    straddle = atm_straddle(analysis)
    c25, p25 = iv_at_delta(analysis, "call", 0.25), iv_at_delta(analysis, "put", -0.25)
    row = {"expiration": analysis.expiration, "dte": (exp - today).days, "t": analysis.t, "atm_iv": iv, "spot": s,
           "forward": analysis.forward, "one_sd_move": one_sd, "one_sd_pct": 100 * one_sd / s if one_sd else None,
           "range_low": s - one_sd if one_sd else None, "range_high": s + one_sd if one_sd else None,
           "straddle": straddle, "straddle_pct": 100 * straddle["price"] / s if straddle else None,
           "iv_25d_call": c25, "iv_25d_put": p25,
           "skew_25d": (p25 - c25) if c25 is not None and p25 is not None else None,
           "butterfly_25d": ((p25 + c25) / 2 - iv) if c25 is not None and p25 is not None and iv else None,
           "smile": None, "earnings": None, "notes": list(analysis.notes)}
    if analysis.smile is not None:
        f = analysis.forward
        row["smile"] = [{"moneyness_pct": m, "strike": f * (1 + m / 100), "iv": analysis.smile.vol_at(f * (1 + m / 100), f)}
                        for m in (-20, -15, -10, -5, 0, 5, 10, 15, 20)]
    if earnings is not None:
        row["earnings"] = ("BEFORE" if exp < earnings else "SAME_DAY" if exp == earnings else "INCLUDES")
    return row


def shape(rows: Sequence[dict[str, Any]]) -> dict[str, Any]:
    """Upward sloping, inverted or flat: the front expiration (at least a week out) against the one
    nearest 60 to 90 days, in volatility points."""
    usable = [r for r in rows if r["atm_iv"] and r["dte"] >= 7]
    if len(usable) < 2:
        return {"shape": None, "text": "Too few expirations with a usable at-the-money volatility."}
    front = usable[0]
    back = min(usable[1:], key=lambda r: abs(r["dte"] - 75))
    diff = back["atm_iv"] - front["atm_iv"]
    if diff > FLAT_POINTS:
        kind, text = "UPWARD", ("Later expirations carry more volatility than the nearest, the usual shape when "
                                "nothing in particular is expected soon.")
    elif diff < -FLAT_POINTS:
        kind, text = "INVERTED", ("The nearest expiration carries more volatility than later ones: the market is "
                                  "pricing an event soon (often earnings) or is under stress now.")
    else:
        kind, text = "FLAT", "Volatility is about the same across expirations."
    return {"shape": kind, "front": front["expiration"], "back": back["expiration"],
            "difference_points": 100 * diff, "text": text}


def implied_event_move(rows: Sequence[dict[str, Any]], earnings: date | None) -> dict[str, Any] | None:
    """One standard deviation of the earnings-day move implied by the term structure.

    With an expiration before the report (vol s0) and the first after it (vol s1, time t1), the
    base volatility is taken to be s0 and the event variance is s1^2 t1 - s0^2 t1. With only
    expirations after it, the base comes from the first two: (s2^2 t2 - s1^2 t1) / (t2 - t1).
    An expiration on the report date itself is left out, since whether it includes the move
    depends on whether the report comes before the open or after the close."""
    if earnings is None:
        return None
    before = [r for r in rows if r["earnings"] == "BEFORE" and r["atm_iv"] and r["t"] > 0]
    after = [r for r in rows if r["earnings"] == "INCLUDES" and r["atm_iv"] and r["t"] > 0]
    if not after:
        return {"available": False, "reason": "no expiration after the earnings date has a usable volatility"}
    a = after[0]
    if before:
        b = before[-1]
        base_var, method = b["atm_iv"] ** 2, f"base volatility from {b['expiration']}, before the report"
    elif len(after) >= 2:
        a2 = after[1]
        if a2["t"] <= a["t"]:
            return {"available": False, "reason": "the expirations after earnings are too close together"}
        base_var = (a2["atm_iv"] ** 2 * a2["t"] - a["atm_iv"] ** 2 * a["t"]) / (a2["t"] - a["t"])
        method = f"base volatility from {a['expiration']} and {a2['expiration']}, both after the report"
    else:
        return {"available": False, "reason": "needs an expiration before the report, or two after it"}
    event_var = a["atm_iv"] ** 2 * a["t"] - base_var * a["t"]
    if base_var <= 0 or event_var <= 0:
        return {"available": False, "reason": "the expiration after the report carries no extra volatility"}
    move = math.sqrt(event_var)
    return {"available": True, "expiration": a["expiration"], "move_pct": 100 * move,
            "move_dollars": a["spot"] * move if a.get("spot") else None,
            "base_vol": math.sqrt(base_var), "method": method,
            "note": "One standard deviation of the earnings-day move that option prices imply; actual moves are often "
                    "larger or smaller."}


def iv30_basis(rows: Sequence[dict[str, Any]]) -> str | None:
    """How iv30 was found: interpolated between expirations either side of 30 days, or the nearest one."""
    pts = [r["dte"] for r in rows if r["atm_iv"] and r["dte"] >= 7]
    if not pts:
        return None
    if any(d <= 30 for d in pts) and any(d > 30 for d in pts):
        return "interpolated"
    near = min(pts, key=lambda d: abs(d - 30))
    return f"the {near}-day expiration (none on the other side of 30 days)"


def iv30(rows: Sequence[dict[str, Any]]) -> float | None:
    """At-the-money volatility 30 days out, interpolated in total variance between the nearest
    expirations either side (at least a week out)."""
    pts = [(r["dte"], r["atm_iv"]) for r in rows if r["atm_iv"] and r["dte"] >= 7]
    if not pts:
        return None
    below = [p for p in pts if p[0] <= 30]
    above = [p for p in pts if p[0] > 30]
    if below and above:
        (d1, v1), (d2, v2) = below[-1], above[0]
        w = v1 * v1 * d1 + (v2 * v2 * d2 - v1 * v1 * d1) * (30 - d1) / (d2 - d1)
        return math.sqrt(max(w, 1e-12) / 30)
    return (below or above)[0 if above else -1][1]


def realized(closes: Iterable[float], windows: Sequence[int] = (10, 20, 60)) -> dict[str, float | None]:
    """Annualised standard deviation of daily log returns over each window (most recent days)."""
    c = np.asarray([x for x in closes if x and x > 0], dtype=float)
    out: dict[str, float | None] = {}
    rets = np.diff(np.log(c)) if len(c) > 1 else np.array([])
    for w in windows:
        out[f"hv{w}"] = float(np.std(rets[-w:], ddof=1) * math.sqrt(TRADING_DAYS)) if len(rets) >= w else None
    return out


def profile(analyses: Sequence[ChainAnalysis], today: date, earnings: date | None,
            closes: Iterable[float] | None = None) -> dict[str, Any]:
    """The whole picture for one stock (see the module notes)."""
    rows = [expiration_row(a, today, earnings) for a in sorted(analyses, key=lambda a: a.expiration)]
    out: dict[str, Any] = {"expirations": rows, "shape": shape(rows), "event": implied_event_move(rows, earnings),
                           "iv30": iv30(rows), "iv30_basis": iv30_basis(rows)}
    if closes is not None:
        rv = realized(closes)
        out["realized"] = rv
        if out["iv30"] and (rv.get("hv20") or 0) > 1e-4:      # a stock that has not moved has no ratio
            out["iv_premium_points"] = 100 * (out["iv30"] - rv["hv20"])
            out["iv_to_realized"] = out["iv30"] / rv["hv20"]
    front = next((r for r in rows if r["skew_25d"] is not None and r["dte"] >= 7), None)
    out["skew"] = ({"expiration": front["expiration"], "points": 100 * front["skew_25d"],
                    "text": ("Puts are dearer than calls the same distance out: the usual tilt, protection costs more."
                             if front["skew_25d"] > 0.005 else
                             "Calls are dearer than puts the same distance out: the market is paying up for upside."
                             if front["skew_25d"] < -0.005 else "Puts and calls the same distance out cost about the same.")}
                   if front else None)
    return out


# --- Monte Carlo --------------------------------------------------------------------------------------

BGK = 0.5826                       # Broadie-Glasserman-Kou: a daily check acts like a barrier this many sigma-steps further


def touch_probability(spot: float, barrier: float, t: float, sigma: float, growth: float,
                      checks_per_year: float | None = None) -> float:
    """Chance a lognormal stock (drift `growth`, volatility `sigma`) reaches `barrier` before `t`:
    at any moment (the reflection formula), or, with `checks_per_year`, on one of that many
    evenly spaced checks a year (the barrier moved out by 0.5826 sigma sqrt(dt), Broadie,
    Glasserman and Kou 1997)."""
    if t <= 0 or sigma <= 0:
        return 1.0 if barrier == spot else 0.0
    if checks_per_year:
        shift = math.exp(BGK * sigma * math.sqrt(1.0 / checks_per_year))
        barrier = barrier * shift if barrier > spot else barrier / shift
    b = math.log(barrier / spot)
    if b == 0:
        return 1.0
    nu = growth - 0.5 * sigma * sigma
    st = sigma * math.sqrt(t)
    if b > 0:
        p = ndtr((-b + nu * t) / st) + math.exp(2 * nu * b / sigma ** 2) * ndtr((-b - nu * t) / st)
    else:
        p = ndtr((b - nu * t) / st) + math.exp(2 * nu * b / sigma ** 2) * ndtr((b + nu * t) / st)
    return float(min(max(p, 0.0), 1.0))


def monte_carlo(*, spot: float, forward: float, t: float, rate: float, sigma: float, strike: float, option_type: str,
                premium: float | None = None, paths: int = 20_000, steps: int | None = None, seed: int = MC_SEED,
                event_move: float | None = None, event_t: float | None = None, multiplier: float = 100.0,
                contracts: int = 1) -> dict[str, Any]:
    """Simulate risk-neutral stock paths and value one option on them (see the module notes).

    `sigma` is the volatility for the option's strike; `event_move` (a fraction, 0.06 for 6%) adds
    one lognormal jump with that standard deviation at time `event_t`. Antithetic paths halve the
    noise. Results are reproducible: the same inputs and seed give the same numbers."""
    for name, value in (("spot", spot), ("forward", forward), ("strike", strike), ("sigma", sigma)):
        if not (math.isfinite(value) and value > 0):
            raise ValueError(f"{name} must be a positive number")
    if not (math.isfinite(t) and t > 0):
        raise ValueError("The option has expired or expires now")
    if option_type not in ("call", "put"):
        raise ValueError("option_type must be call or put")
    if event_move is not None and not (math.isfinite(event_move) and 0 <= event_move < 1):
        raise ValueError("event_move must be a fraction from 0 to 1")
    paths = int(min(max(paths, 2_000), MC_MAX_PATHS))
    paths += paths % 2
    daily = max(1, int(round(t * TRADING_DAYS)))
    n_steps = int(steps) if steps else min(daily, TRADING_DAYS)
    reduced = n_steps < daily
    if paths * n_steps > MC_MAX_CELLS:
        n_steps, reduced = max(1, MC_MAX_CELLS // paths), True
    cp = 1 if option_type == "call" else -1
    growth = math.log(forward / spot) / t
    dt = t / n_steps
    rng = np.random.default_rng(seed)
    half = paths // 2
    z = rng.standard_normal((half, n_steps))
    z = np.vstack([z, -z])
    inc = (growth - 0.5 * sigma * sigma) * dt + sigma * math.sqrt(dt) * z
    jump_step = None
    if event_move and event_t is not None and 0 < event_t < t:
        jump_step = min(n_steps - 1, int(event_t / dt))
        zj = rng.standard_normal(half)
        zj = np.concatenate([zj, -zj])
        inc[:, jump_step] += event_move * zj - 0.5 * event_move * event_move
    log_path = np.log(spot) + np.cumsum(inc, axis=1)
    s_t = np.exp(log_path[:, -1])
    payoff = np.maximum(cp * (s_t - strike), 0.0)
    disc = math.exp(-rate * t)
    pair = 0.5 * (payoff[:half] + payoff[half:])           # antithetic pairs are independent of each other
    mc_price = disc * float(payoff.mean())
    se = disc * float(pair.std(ddof=1) / math.sqrt(half))
    total_var = sigma * sigma * t + (event_move ** 2 if jump_step is not None else 0.0)
    eff_vol = math.sqrt(total_var / t)
    bs = black76_price(forward, strike, t, rate, eff_vol, option_type)
    if strike >= spot:
        touched = (log_path >= math.log(strike)).any(axis=1)
    else:
        touched = (log_path <= math.log(strike)).any(axis=1)
    pct = np.percentile(s_t, [5, 25, 50, 75, 95])
    out: dict[str, Any] = {
        "paths": paths, "steps": n_steps, "seed": seed, "steps_reduced": reduced,
        "price": mc_price, "standard_error": se, "black76": bs, "difference": mc_price - bs,
        "consistent": abs(mc_price - bs) <= 3 * se + 0.005,
        "p_finish_itm": float((cp * (s_t - strike) > 0).mean()),
        "p_finish_itm_formula": prob_finish_beyond(forward, strike, t, eff_vol, above=cp > 0),
        # Reaching the strike on a closing price (as simulated, one check a trading day) and at any moment.
        "p_touch_close": float(touched.mean()),
        # The daily-check correction is an approximation that fails within a step or two of the barrier.
        "p_touch_close_formula": (touch_probability(spot, strike, t, sigma, growth, n_steps / t)
                                  if jump_step is None and abs(math.log(strike / spot)) >= 2 * sigma * math.sqrt(dt)
                                  else None),
        "p_touch_any_time": (touch_probability(spot, strike, t, sigma, growth) if jump_step is None else None),
        "terminal": {"p5": float(pct[0]), "p25": float(pct[1]), "median": float(pct[2]), "p75": float(pct[3]),
                     "p95": float(pct[4])},
        "histogram": _histogram(s_t, strike),
        "event": ({"move_pct": 100 * event_move, "step": jump_step} if jump_step is not None else None),
        "notes": [
            "Risk-neutral: stock paths drift at the rate implied by the forward, not at what anyone expects the "
            "stock to do. The odds are what option prices imply, not a forecast.",
            "The chance of touching the strike is given two ways: on a closing price (the simulation checks once a "
            "trading day) and at any moment during the day (a formula, shown only without an earnings jump). "
            "Touching it any time is more likely than closing at or past it.",
            "One volatility for every path: fat tails and changes in volatility are not included."
            + (" An earnings jump is added on the report date." if jump_step is not None else ""),
        ],
    }
    if reduced:
        out["notes"].append(f"Paths were checked {n_steps} times, about every {daily / n_steps:.1f} trading days, rather "
                            "than daily, to keep the run small; touching the strike between checks is not counted.")
    if premium is not None:
        if not (math.isfinite(premium) and premium >= 0):
            raise ValueError("premium must be zero or more")
        pnl = (payoff - premium) * multiplier * contracts
        q = np.percentile(pnl, [5, 25, 50, 75, 95])
        out["buyer"] = {"premium": premium, "p_profit": float((pnl > 0).mean()),
                        "average_pnl": float(pnl.mean()), "model_edge_per_share": bs - premium,
                        "pnl": {"p5": float(q[0]), "p25": float(q[1]), "median": float(q[2]), "p75": float(q[3]),
                                "p95": float(q[4])},
                        "note": "Outcomes are in dollars at expiration; the model value is today's (discounted). Paying "
                                "more than the model value means paying for volatility above what the chain implies at "
                                "this strike; paying less, the reverse."}
    return out


def _histogram(values: np.ndarray, strike: float, bins: int = 30) -> dict[str, Any]:
    lo, hi = np.percentile(values, [0.5, 99.5])
    if hi <= lo:
        hi = lo * 1.01 + 0.01
    counts, edges = np.histogram(np.clip(values, lo, hi), bins=bins, range=(lo, hi))
    return {"edges": [float(e) for e in edges], "counts": [int(c) for c in counts], "strike": strike}


__all__ = ["profile", "expiration_row", "iv_at_delta", "atm_straddle", "shape", "implied_event_move", "iv30",
           "realized", "monte_carlo", "touch_probability"]
