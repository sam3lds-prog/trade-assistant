"""What-if simulation of an option position: profit and loss across stock prices and dates.

A position is one to four legs on the same stock, each bought or sold. Every leg is valued
with the same Black-76 model the rest of the app uses, at the volatility the chain's smile
gives its strike, shifted by a chosen number of volatility points, and with the stock's carry
(dividends, borrow) taken from that expiration's forward price. On its expiration day a leg is
worth its intrinsic value.

What this assumes, and so what to keep in mind when reading it:
- Each strike keeps its own volatility as the stock moves ("sticky strike"), plus the shift.
  In a real sell-off implied volatility usually rises, and after earnings it usually falls.
- Options are valued as European. Early exercise and assignment are not modelled.
- The odds come from the market's own pricing (a lognormal distribution at the at-the-money
  implied volatility, centred on the forward). They are what option prices imply, not a
  forecast, and they leave out fat tails and jumps such as earnings gaps.
- Fees are not included.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import date, datetime, time as dtime, timedelta, timezone
from typing import Any, Sequence

import numpy as np
from scipy.special import ndtr

from .optionmath import NY, black76_greeks

MULTIPLIER = 100
MIN_VOL = 0.01
SECONDS_PER_YEAR = 365.0 * 86400.0


@dataclass(frozen=True)
class Leg:
    option_type: str      # "call" or "put"
    strike: float
    expiration: str       # YYYY-MM-DD
    side: int             # +1 bought, -1 sold
    contracts: int
    entry: float          # premium per share paid (bought) or received (sold)
    vol: float            # volatility that values this strike (from the chain's smile)
    growth: float         # carry rate implied by this expiration's forward: ln(F/S) / T

    @property
    def cp(self) -> int:
        return 1 if self.option_type == "call" else -1

    @property
    def units(self) -> int:
        """Shares the leg controls, signed: +100 per contract bought, -100 per contract sold."""
        return self.side * self.contracts * MULTIPLIER

    def describe(self) -> str:
        verb = "Buy" if self.side > 0 else "Sell"
        return f"{verb} {self.contracts} {self.expiration} {self.strike:g} {self.option_type}"


def expiry_moment(expiration: str) -> datetime:
    return datetime.combine(date.fromisoformat(expiration), dtime(16, 0), tzinfo=NY)


def years_between(start: datetime, end: datetime) -> float:
    return (end.astimezone(timezone.utc) - start.astimezone(timezone.utc)).total_seconds() / SECONDS_PER_YEAR


def eval_moment(day: date, now: datetime) -> datetime:
    """Today means now; any later day means that day's 4:00 pm close in New York."""
    return now if day <= now.astimezone(NY).date() else datetime.combine(day, dtime(16, 0), tzinfo=NY)


def black76_vec(forward: np.ndarray, strike: float, t: float, rate: float, sigma: float, cp: int) -> np.ndarray:
    df = math.exp(-rate * t)
    if t <= 0 or sigma <= 0:
        return df * np.maximum(cp * (forward - strike), 0.0)
    vst = sigma * math.sqrt(t)
    d1 = (np.log(forward / strike) + 0.5 * sigma * sigma * t) / vst
    d2 = d1 - vst
    return df * cp * (forward * ndtr(cp * d1) - strike * ndtr(cp * d2))


def leg_value(leg: Leg, prices: np.ndarray, when: datetime, rate: float, iv_shift: float = 0.0) -> np.ndarray:
    """Value per share of one leg at stock prices `prices` at moment `when`."""
    prices = np.asarray(prices, dtype=float)
    tau = years_between(when, expiry_moment(leg.expiration))
    if tau <= 1e-9:
        return np.maximum(leg.cp * (prices - leg.strike), 0.0)
    forward = prices * math.exp(leg.growth * tau)
    return black76_vec(forward, leg.strike, tau, rate, max(leg.vol + iv_shift, MIN_VOL), leg.cp)


def position_pnl(legs: Sequence[Leg], prices: np.ndarray, when: datetime, rate: float,
                 iv_shift: float = 0.0) -> np.ndarray:
    """Profit or loss in dollars of the whole position, against the entry prices."""
    prices = np.asarray(prices, dtype=float)
    total = np.zeros_like(prices)
    for leg in legs:
        total += leg.units * (leg_value(leg, prices, when, rate, iv_shift) - leg.entry)
    return total


def net_premium(legs: Sequence[Leg]) -> float:
    """Dollars paid to open (positive) or received (negative)."""
    return float(sum(leg.units * leg.entry for leg in legs))


def expiry_profile(legs: Sequence[Leg]) -> dict[str, Any]:
    """Exact maximum gain, maximum loss and breakevens when every leg expires on the same day.

    The payoff at expiration is a straight line between strikes, so its extremes sit at a strike,
    at a stock price of zero, or run without limit when calls bought outnumber calls sold."""
    def f(s: float) -> float:
        return float(sum(leg.units * (max(leg.cp * (s - leg.strike), 0.0) - leg.entry) for leg in legs))

    points = sorted({0.0, *(leg.strike for leg in legs)})
    values = [f(p) for p in points]
    slope_above = float(sum(leg.units for leg in legs if leg.cp == 1))
    unlimited_gain, unlimited_loss = slope_above > 1e-9, slope_above < -1e-9
    breakevens: list[float] = []
    for (a, fa), (b, fb) in zip(zip(points, values), zip(points[1:], values[1:])):
        if fa == 0 and a > 0:
            breakevens.append(a)
        elif fa * fb < 0:
            breakevens.append(a + (b - a) * fa / (fa - fb))
    last_point, last_value = points[-1], values[-1]
    if last_value == 0 and last_point > 0 and last_point not in breakevens:
        breakevens.append(last_point)
    if slope_above != 0 and last_value * slope_above < 0:
        breakevens.append(last_point - last_value / slope_above)
    return {"max_gain": None if unlimited_gain else round(max(values), 6),
            "max_loss": None if unlimited_loss else round(min(values), 6),
            "unlimited_gain": unlimited_gain, "unlimited_loss": unlimited_loss,
            "breakevens": sorted(round(b, 4) for b in breakevens)}


def crossings(prices: np.ndarray, pnl: np.ndarray) -> list[float]:
    """Stock prices where a P&L curve crosses zero, by straight-line interpolation."""
    out = []
    for i in range(len(prices) - 1):
        a, b = pnl[i], pnl[i + 1]
        if a == 0:
            out.append(float(prices[i]))
        elif a * b < 0:
            out.append(float(prices[i] + (prices[i + 1] - prices[i]) * a / (a - b)))
    return out


Z = np.linspace(-6.0, 6.0, 2401)
WEIGHTS = np.exp(-0.5 * Z * Z)
WEIGHTS /= WEIGHTS.sum()


def market_odds(legs: Sequence[Leg], spot: float, rate: float, vol: float, growth: float, when: datetime,
                now: datetime, iv_shift: float = 0.0, max_loss: float | None = None) -> dict[str, Any]:
    """Market-implied chances for the position at moment `when`, from a lognormal distribution of
    the stock price centred on its forward at the at-the-money implied volatility."""
    t = max(years_between(now, when), 0.0)
    if t < 1e-6 or vol <= 0:
        pnl_now = float(position_pnl(legs, np.array([spot]), when, rate, iv_shift)[0])
        return {"p_profit": 1.0 if pnl_now > 0 else 0.0, "p_max_loss": None, "low": spot, "mid": spot,
                "high": spot, "pnl_low": pnl_now, "pnl_mid": pnl_now, "pnl_high": pnl_now, "one_sd_move": 0.0}
    forward = spot * math.exp(growth * t)
    sd = vol * math.sqrt(t)
    prices = forward * np.exp(-0.5 * sd * sd + sd * Z)
    pnl = position_pnl(legs, prices, when, rate, iv_shift)
    out = {"p_profit": float(WEIGHTS[pnl > 0.005].sum()), "p_max_loss": None, "one_sd_move": spot * sd}
    if max_loss is not None and max_loss < -0.005:
        out["p_max_loss"] = float(WEIGHTS[pnl <= max_loss + 0.5].sum())
    for name, z in (("low", -1.2815516), ("mid", 0.0), ("high", 1.2815516)):   # 10th, 50th, 90th percentiles
        s = forward * math.exp(-0.5 * sd * sd + sd * z)
        out[name] = s
        out[f"pnl_{name}"] = float(position_pnl(legs, np.array([s]), when, rate, iv_shift)[0])
    return out


def trading_days(start: date, end: date) -> list[date]:
    """Weekdays from start to end inclusive. Market holidays are not removed."""
    days, d = [], start
    while d <= end:
        if d.weekday() < 5:
            days.append(d)
        d += timedelta(days=1)
    return days


def nice_step(span: float, count: int) -> float:
    rough = span / max(count, 1)
    mag = 10 ** math.floor(math.log10(rough))
    return next(m * mag for m in (1, 2, 2.5, 5, 10) if m * mag >= rough)


def position_greeks(legs: Sequence[Leg], spot: float, rate: float, now: datetime) -> dict[str, float]:
    """Delta and gamma in shares, theta in dollars per day, vega in dollars per volatility point."""
    total = {"delta": 0.0, "gamma": 0.0, "theta": 0.0, "vega": 0.0}
    for leg in legs:
        t = years_between(now, expiry_moment(leg.expiration))
        if t <= 0:
            continue
        g = black76_greeks(spot, spot * math.exp(leg.growth * t), leg.strike, t, rate, leg.vol, leg.option_type)
        for k in total:
            total[k] += leg.units * getattr(g, k)
    return total


def simulate(legs: Sequence[Leg], spot: float, rate: float, atm_vol: float, now: datetime,
             eval_day: date | None = None, iv_shift: float = 0.0, range_pct: float | None = None,
             curve_points: int = 161, grid_dates: int = 7) -> dict[str, Any]:
    """The full what-if picture for one position. `iv_shift` is in volatility points as a fraction
    (0.05 means five points higher), `range_pct` the half-width of the price axis (0.2 = +/-20%)."""
    if not legs:
        raise ValueError("A position needs at least one leg")
    today = now.astimezone(NY).date()
    first_exp = min(date.fromisoformat(leg.expiration) for leg in legs)
    if first_exp < today:
        raise ValueError("A leg has already expired")
    same_expiry = len({leg.expiration for leg in legs}) == 1
    # Today always comes first, even on a weekend, so "today" means now and not next Monday.
    days = [today] + [d for d in trading_days(today, first_exp) if d > today]
    eval_day = min(max(eval_day or today, today), first_exp)
    at_expiry = expiry_moment(first_exp.isoformat())     # the 4:00 pm close, even on the expiration day itself

    def moment(day: date) -> datetime:
        return at_expiry if day == first_exp else eval_moment(day, now)
    when = moment(eval_day)
    nearest = min(legs, key=lambda leg: leg.expiration)

    t_exp = max(years_between(now, at_expiry), 1.0 / 365.0)
    if range_pct:
        lo, hi = spot * (1 - range_pct), spot * (1 + range_pct)
    else:
        move = 2.5 * max(atm_vol, 0.05) * math.sqrt(t_exp)
        lo, hi = spot * math.exp(-max(move, 0.05)), spot * math.exp(max(move, 0.05))
        strikes = [leg.strike for leg in legs]
        lo, hi = min(lo, min(strikes) * 0.95), max(hi, max(strikes) * 1.05)
    lo = max(lo, spot * 0.01)
    prices = np.linspace(lo, hi, curve_points)
    pnl_when = position_pnl(legs, prices, when, rate, iv_shift)
    pnl_exp = position_pnl(legs, prices, at_expiry, rate, iv_shift)

    if same_expiry:
        profile = expiry_profile(legs)
        profile["exact"] = True
    else:
        wide = np.linspace(spot * 0.01, spot * 4, 4000)
        wide_pnl = position_pnl(legs, wide, at_expiry, rate, iv_shift)
        # Far enough up, every call (expired or not) moves dollar for dollar with the stock, so more calls
        # sold than bought means no limit to the loss, and more bought than sold no limit to the gain.
        slope = sum(leg.units for leg in legs if leg.cp == 1)
        profile = {"max_gain": None if slope > 0 else float(wide_pnl.max()),
                   "max_loss": None if slope < 0 else float(wide_pnl.min()),
                   "unlimited_gain": slope > 0, "unlimited_loss": slope < 0,
                   "breakevens": crossings(wide, wide_pnl), "exact": False}

    step = nice_step(hi - lo, 10)
    levels = list(np.arange(math.ceil(lo / step) * step, hi + step * 0.01, step))
    if not any(abs(level - spot) < step * 0.25 for level in levels):
        levels.append(spot)
    levels = sorted({round(float(x), 2) for x in levels}, reverse=True)
    if len(days) <= grid_dates:
        columns = days
    else:
        picks = np.linspace(0, len(days) - 1, grid_dates).round().astype(int)
        columns = [days[i] for i in sorted(set(picks))]
    level_array = np.array(levels)
    grid = [position_pnl(legs, level_array, moment(d), rate, iv_shift).tolist() for d in columns]

    odds_when = market_odds(legs, spot, rate, atm_vol, nearest.growth, when, now, iv_shift, profile["max_loss"])
    odds_exp = market_odds(legs, spot, rate, atm_vol, nearest.growth, at_expiry, now, iv_shift, profile["max_loss"])
    if len(days) > 120:
        picks = np.linspace(0, len(days) - 1, 120).round().astype(int)
        slider = [days[i] for i in sorted(set(picks))]
    else:
        slider = days
    return {
        "spot": spot, "rate": rate, "atm_vol": atm_vol, "iv_shift": iv_shift,
        "eval_date": eval_day.isoformat(), "first_expiration": first_exp.isoformat(), "same_expiry": same_expiry,
        "days_to_eval": (eval_day - today).days, "days_to_expiry": (first_exp - today).days,
        "dates": [d.isoformat() for d in slider],
        "net_premium": net_premium(legs),
        "curve": {"prices": prices.tolist(), "pnl_at_date": pnl_when.tolist(), "pnl_at_expiry": pnl_exp.tolist(),
                  "breakevens_at_date": crossings(prices, pnl_when)},
        "expiry": profile,
        "grid": {"prices": levels, "dates": [d.isoformat() for d in columns],
                 "pnl": [[grid[c][r] for c in range(len(columns))] for r in range(len(levels))]},
        "odds_at_date": odds_when, "odds_at_expiry": odds_exp,
        "greeks": position_greeks(legs, spot, rate, now),       # today, at today's volatility
        "notes": [
            "Each strike keeps the volatility its option is priced at today, plus the change you set. "
            "In a sell-off implied volatility usually rises; after earnings it usually drops.",
            "Valued as European options: early exercise and early assignment are not modelled.",
            "The chances come from today's option prices (a lognormal spread at the at-the-money implied "
            "volatility). They are what the market prices in, not a forecast, and they understate the "
            "chance of big gaps such as earnings moves.",
            "Fees are not included. Dates skip weekends but not market holidays.",
        ] + ([] if same_expiry else [
            "The legs expire on different days. \"At expiration\" means the first expiration, with the "
            "later legs valued by the model, so the maximum gain and loss are read from stock prices up to "
            "four times today's rather than worked out exactly."]),
    }
