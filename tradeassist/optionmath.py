"""Option pricing on the forward (Black-76), Greeks, implied volatility, smile fitting.

Limitations worth knowing:
- Prices are European. US equity options are American, so early-exercise value is
  ignored. That matters for deep in-the-money puts and for calls just before an
  ex-dividend date; callers flag those cases instead of trusting the model.
- Dividends are not looked up. The forward is implied from put-call parity near the
  money, which captures expected dividends and borrow cost without a dividend feed.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import date, datetime, time as dtime, timezone
from typing import Iterable, Sequence
from zoneinfo import ZoneInfo

import numpy as np

NY = ZoneInfo("America/New_York")
MIN_T = 0.25 / 365.0  # never price with less than six hours to expiry
SQRT_2PI = math.sqrt(2.0 * math.pi)


def norm_cdf(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def norm_pdf(x: float) -> float:
    return math.exp(-0.5 * x * x) / SQRT_2PI


def cp_sign(option_type: str) -> int:
    t = option_type.lower()
    if t in ("call", "c"):
        return 1
    if t in ("put", "p"):
        return -1
    raise ValueError(f"option_type must be call or put, got {option_type!r}")


def years_to_expiry(expiration: str | date, now: datetime | None = None) -> float:
    """Calendar time to 4:00 pm New York on the expiration date, in years (ACT/365)."""
    if isinstance(expiration, str):
        expiration = date.fromisoformat(expiration)
    now = now.astimezone(NY) if now else datetime.now(NY)
    expiry_dt = datetime.combine(expiration, dtime(16, 0), tzinfo=NY)
    # Subtract in UTC: two New York wall-clock times an hour of daylight saving apart would
    # otherwise be treated as if no clock change happened between them.
    seconds = (expiry_dt.astimezone(timezone.utc) - now.astimezone(timezone.utc)).total_seconds()
    return max(seconds / (365.0 * 86400.0), MIN_T)


def days_to_expiry(expiration: str | date, today: date | None = None) -> int:
    if isinstance(expiration, str):
        expiration = date.fromisoformat(expiration)
    today = today or datetime.now(NY).date()
    return (expiration - today).days


def _d1_d2(forward: float, strike: float, t: float, sigma: float) -> tuple[float, float]:
    vol_sqrt_t = sigma * math.sqrt(t)
    d1 = (math.log(forward / strike) + 0.5 * sigma * sigma * t) / vol_sqrt_t
    return d1, d1 - vol_sqrt_t


def black76_price(forward: float, strike: float, t: float, rate: float, sigma: float, option_type: str) -> float:
    """Discounted Black-76 price of a European option on a forward."""
    cp = cp_sign(option_type)
    df = math.exp(-rate * t)
    if sigma <= 0 or t <= 0:
        return df * max(cp * (forward - strike), 0.0)
    d1, d2 = _d1_d2(forward, strike, t, sigma)
    return df * cp * (forward * norm_cdf(cp * d1) - strike * norm_cdf(cp * d2))


@dataclass(frozen=True)
class Greeks:
    delta: float   # per $1 move in the underlying
    gamma: float   # change in delta per $1 move
    theta: float   # per calendar day, in option-price dollars (negative for long options)
    vega: float    # per 1 volatility point (0.01)


def black76_greeks(spot: float, forward: float, strike: float, t: float, rate: float, sigma: float,
                   option_type: str) -> Greeks:
    """Greeks with respect to spot, holding the carry implied by `forward` fixed."""
    cp = cp_sign(option_type)
    df = math.exp(-rate * t)
    carry = forward / spot  # dF/dS when carry is proportional
    if sigma <= 0 or t <= 0:
        itm = cp * (forward - strike) > 0
        return Greeks(delta=cp * df * carry if itm else 0.0, gamma=0.0, theta=0.0, vega=0.0)
    d1, d2 = _d1_d2(forward, strike, t, sigma)
    sqrt_t = math.sqrt(t)
    delta = cp * df * carry * norm_cdf(cp * d1)
    gamma = df * carry * carry * norm_pdf(d1) / (forward * sigma * sqrt_t)
    vega = df * forward * norm_pdf(d1) * sqrt_t / 100.0
    # Theta by a one-day bump with spot fixed: the forward drifts toward spot as time passes.
    one_day = 1.0 / 365.0
    if t > one_day + MIN_T:
        growth = math.log(carry) / t  # r - q implied by the forward
        t2 = t - one_day
        fwd2 = spot * math.exp(growth * t2)
        theta = black76_price(fwd2, strike, t2, rate, sigma, option_type) - \
            black76_price(forward, strike, t, rate, sigma, option_type)
    else:
        intrinsic = max(cp * (spot - strike), 0.0)
        theta = intrinsic - black76_price(forward, strike, t, rate, sigma, option_type)
    return Greeks(delta=delta, gamma=gamma, theta=theta, vega=vega)


def implied_vol(price: float, forward: float, strike: float, t: float, rate: float, option_type: str,
                lo: float = 1e-4, hi: float = 6.0) -> float | None:
    """Volatility that reproduces `price`, or None when no volatility can (price outside model bounds)."""
    if price is None or not math.isfinite(price) or price <= 0 or forward <= 0 or strike <= 0 or t <= 0:
        return None
    p_lo = black76_price(forward, strike, t, rate, lo, option_type)
    p_hi = black76_price(forward, strike, t, rate, hi, option_type)
    if price <= p_lo + 1e-10 or price >= p_hi:
        return None
    for _ in range(100):
        mid = 0.5 * (lo + hi)
        p_mid = black76_price(forward, strike, t, rate, mid, option_type)
        if p_mid > price:
            hi = mid
        else:
            lo = mid
        if hi - lo < 1e-7:
            break
    return 0.5 * (lo + hi)


def implied_forward(pairs: Iterable[tuple[float, float, float]], spot: float, t: float, rate: float,
                    max_pairs: int = 5) -> tuple[float, str]:
    """Forward price implied by put-call parity.

    `pairs` holds (strike, call_mid, put_mid) for strikes where both sides have usable
    two-sided quotes. The strikes nearest the money are used and the median taken. If no
    usable pairs exist, or the answer is implausible, fall back to spot * exp(rate * t).

    Returns (forward, method) where method is "parity" or "carry".
    """
    fallback = spot * math.exp(rate * t)
    usable = [(k, c, p) for k, c, p in pairs
              if k > 0 and c is not None and p is not None and c > 0 and p > 0]
    if not usable or spot <= 0:
        return fallback, "carry"
    usable.sort(key=lambda row: abs(row[0] - spot))
    growth = math.exp(rate * t)
    estimates = [k + growth * (c - p) for k, c, p in usable[:max_pairs]]
    forward = float(np.median(estimates))
    # A parity forward more than 10% from the carry forward means bad quotes, not real dividends.
    if not math.isfinite(forward) or abs(forward / fallback - 1.0) > 0.10:
        return fallback, "carry"
    return forward, "parity"


def expected_move(spot: float, sigma: float, t: float) -> float:
    """One-standard-deviation move in dollars implied by volatility `sigma` over `t` years."""
    return spot * sigma * math.sqrt(t)


def prob_finish_beyond(forward: float, level: float, t: float, sigma: float, above: bool) -> float:
    """Risk-neutral probability the underlying finishes above (or below) `level` at expiry."""
    if sigma <= 0 or t <= 0 or level <= 0 or forward <= 0:
        finishes_above = forward > level
        return 1.0 if finishes_above == above else 0.0
    _, d2 = _d1_d2(forward, level, t, sigma)
    p_above = norm_cdf(d2)
    return p_above if above else 1.0 - p_above


@dataclass(frozen=True)
class Smile:
    """Quadratic fit of implied volatility against log-moneyness ln(K/F)."""
    coeffs: tuple[float, float, float]
    k_min: float
    k_max: float
    iv_min: float
    iv_max: float
    n_points: int

    def vol_at(self, strike: float, forward: float) -> float:
        k = math.log(strike / forward)
        k = min(max(k, self.k_min), self.k_max)  # do not extrapolate the parabola past the data
        a, b, c = self.coeffs
        vol = a * k * k + b * k + c
        return min(max(vol, 0.5 * self.iv_min), 1.5 * self.iv_max)


def fit_smile(strikes: Sequence[float], ivs: Sequence[float], weights: Sequence[float],
              forward: float, min_points: int = 5) -> Smile | None:
    """Weighted least-squares quadratic through (ln(K/F), iv). None if too few usable points."""
    rows = [(math.log(k / forward), iv, w) for k, iv, w in zip(strikes, ivs, weights)
            if k > 0 and iv is not None and math.isfinite(iv) and iv > 0 and w > 0]
    if len(rows) < min_points:
        return None
    ks = np.array([r[0] for r in rows])
    vs = np.array([r[1] for r in rows])
    ws = np.array([r[2] for r in rows])
    if np.ptp(ks) < 1e-6:
        return None
    coeffs = np.polyfit(ks, vs, deg=2, w=np.sqrt(ws))
    return Smile(coeffs=(float(coeffs[0]), float(coeffs[1]), float(coeffs[2])),
                 k_min=float(ks.min()), k_max=float(ks.max()),
                 iv_min=float(vs.min()), iv_max=float(vs.max()), n_points=len(rows))
