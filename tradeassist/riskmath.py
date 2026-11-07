"""Outcome statistics and the four risk measures, kept apart (3.0).

A P&L distribution is described by its own percentiles, not by the P&L at the underlying's percentiles: the two
agree only when the P&L only rises (or only falls) with the price. A straddle loses most when the stock does not
move, so its worst tenth of outcomes sits in the middle of the price range, not at the 10th percentile price.

Risk has four different meanings here and none stands in for another:

- **theoretical maximum loss**: what the payoff structure can lose at expiration, from the contract terms alone
  (None when there is no limit, or when the terms alone cannot say);
- **modeled worst loss**: the worst outcome among the simulated ones, which depends on how wide the simulation
  ran and is never a guaranteed cap;
- **expected shortfall**: the average loss in the worst tenth of modeled outcomes;
- **capital required**: what the account must put up or reserve to hold the position.

The Trading Desk sizes and authorises on the theoretical figure and the capital; the modeled figures describe a
scenario and never cap anything.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any, Iterable, Sequence

import numpy as np

TAIL = 0.10


def weighted_quantile(values: np.ndarray, weights: np.ndarray, q: float) -> float:
    """The q-quantile of a discrete distribution, interpolated on the midpoints of each value's probability (so a
    symmetric distribution's median is its middle value exactly)."""
    values = np.asarray(values, dtype=float)
    weights = np.asarray(weights, dtype=float)
    if values.size == 0:
        raise ValueError("no values")
    order = np.argsort(values, kind="mergesort")
    v, w = values[order], weights[order]
    total = float(w.sum())
    if total <= 0:
        raise ValueError("weights must add up to more than zero")
    mid = (np.cumsum(w) - 0.5 * w) / total
    return float(np.interp(q, mid, v))


def monotone(pnl: np.ndarray, tol: float = 1e-9) -> int:
    """+1 when the P&L never falls as the price rises, -1 when it never rises, 0 otherwise."""
    d = np.diff(np.asarray(pnl, dtype=float))
    scale = tol * max(1.0, float(np.max(np.abs(pnl))) if len(pnl) else 1.0)
    if np.all(d >= -scale):
        return 1
    if np.all(d <= scale):
        return -1
    return 0


def outcome_stats(prices: np.ndarray, weights: np.ndarray, pnl: np.ndarray,
                  price_quantile: dict[float, float] | None = None, tail: float = TAIL) -> dict[str, Any]:
    """The figures of one P&L curve under a scenario.

    `price_quantile` maps 0.10, 0.50 and 0.90 to the exact price at that percentile when the caller has it: a P&L
    that only rises or only falls with the price is then read off the curve at the matching price, exactly; any
    other shape takes its percentiles from the weighted distribution of the P&L itself."""
    prices = np.asarray(prices, dtype=float)
    weights = np.asarray(weights, dtype=float)
    pnl = np.asarray(pnl, dtype=float)
    weights = weights / weights.sum()
    shape = monotone(pnl)
    out: dict[str, Any] = {}
    for q, key in ((0.10, "pnl_p10"), (0.50, "pnl_p50"), (0.90, "pnl_p90")):
        if shape != 0 and price_quantile and q in price_quantile:
            at = price_quantile[q] if shape > 0 else price_quantile[round(1 - q, 2)]
            out[key] = float(np.interp(at, prices, pnl))
        else:
            out[key] = weighted_quantile(pnl, weights, q)
    order = np.argsort(pnl, kind="mergesort")
    cum = np.cumsum(weights[order])
    idx = order[cum <= tail + 1e-12]
    if len(idx) == 0:
        idx = order[:1]
    tw = weights[idx]
    shortfall = -float((pnl[idx] * tw).sum() / tw.sum())
    out.update({
        "expected_pnl": float((weights * pnl).sum()),
        "p_profit": float(weights[pnl > 0].sum()),
        "expected_shortfall": max(0.0, shortfall),
        "modeled_worst_loss": max(0.0, -float(pnl.min())),
        "modeled_best": float(pnl.max()),
        "shape": {1: "rises with the price", -1: "falls with the price", 0: "not monotonic in the price"}[shape],
    })
    # The 2.x names, kept for the screens that read them.
    out["cvar10"] = out["expected_shortfall"]
    out["worst_in_range"] = -float(pnl.min())
    out["best_in_range"] = out["modeled_best"]
    return out


@dataclass(frozen=True)
class PayoffLeg:
    """One piece of a position at expiration: shares (option_type None) or one option series.

    `quantity` is signed: shares held (negative for a short) or contracts (negative when sold). `entry` is the
    price per share paid (or received, for a sold option: still a positive number)."""
    option_type: str | None
    quantity: float
    entry: float
    strike: float = 0.0
    multiplier: float = 100.0
    expiration: str | None = None

    def value(self, s: float) -> float:
        if self.option_type is None:
            return self.quantity * (s - self.entry)
        intrinsic = max(s - self.strike, 0.0) if self.option_type.upper().startswith("C") else max(self.strike - s, 0.0)
        return self.quantity * self.multiplier * (intrinsic - self.entry)

    def slope_above(self) -> float:
        if self.option_type is None:
            return self.quantity
        return self.quantity * self.multiplier if self.option_type.upper().startswith("C") else 0.0


def theoretical_max_loss(legs: Sequence[PayoffLeg]) -> dict[str, Any]:
    """The most the position can lose at expiration from the contract terms alone.

    The expiration payoff of shares and options is a straight line between strikes, so its lowest point is at a
    stock price of zero, at a strike, or without limit when the line still falls above the highest strike. Before
    expiration a model value cannot go below this floor except through rates, dividends and early assignment of a
    sold American option, which are named in the basis. Options expiring on different days have no single payoff
    line: the terms alone cannot give their floor, and the answer says so instead of guessing."""
    legs = [lg for lg in legs if lg.quantity]
    if not legs:
        return {"value": 0.0, "unlimited": False, "known": True, "basis": "no position"}
    expirations = {lg.expiration for lg in legs if lg.option_type is not None}
    if len(expirations) > 1:
        return {"value": None, "unlimited": False, "known": False,
                "basis": "the options expire on different days, so the contract terms alone do not fix the most it can lose"}
    slope = sum(lg.slope_above() for lg in legs)
    if slope < -1e-9:
        return {"value": None, "unlimited": True, "known": True,
                "basis": "more calls sold (or shares shorted) than calls and shares held: the loss grows without limit as the price rises"}
    points = sorted({0.0, *(lg.strike for lg in legs if lg.option_type is not None)})
    low = min(sum(lg.value(p) for lg in legs) for p in points)
    loss = max(0.0, -low)
    sold = any(lg.option_type is not None and lg.quantity < 0 for lg in legs)
    basis = "the lowest point of the payoff at expiration, from the strikes and the prices paid"
    if sold:
        basis += "; a sold option can be assigned early, and dividends are not included"
    return {"value": float(loss), "unlimited": False, "known": True, "basis": basis}


def legs_from_built(built_legs: Iterable[dict[str, Any]], multiplier: float = 100.0) -> list[PayoffLeg]:
    """The strategy builder's legs ({option_type, side, strike, expiration, contracts, mid}) as payoff legs."""
    out = []
    for lg in built_legs:
        sign = 1 if str(lg.get("side", "buy")).lower() == "buy" else -1
        entry = lg.get("mid") if lg.get("mid") is not None else lg.get("entry_price")
        out.append(PayoffLeg(option_type=str(lg["option_type"]).upper(), quantity=sign * float(lg["contracts"]),
                             entry=float(entry or 0.0), strike=float(lg["strike"]), multiplier=float(lg.get("multiplier") or multiplier),
                             expiration=lg.get("expiration")))
    return out


def capital_required(kind: str, *, cost: float, credit: bool, quantity: float = 0.0, last: float = 0.0,
                     theoretical: float | None = None) -> dict[str, Any]:
    """What the account must put up to hold the position, with the rule used. Margin rules differ by broker and
    account: these are the plain cash-account and Regulation T figures, and the Trading Desk asks the broker's own
    review before any live order."""
    if kind == "shares":
        return {"value": float(cost), "basis": "the price of the shares (a cash account)"}
    if kind == "short":
        value = 1.5 * abs(quantity) * last
        return {"value": float(value), "basis": "Regulation T initial margin: the short's proceeds plus 50% of its value"}
    if credit:
        if theoretical is not None:
            return {"value": float(theoretical), "basis": "a defined-risk credit: the most it can lose is reserved"}
        return {"value": None, "basis": "a sold option with no limit to its loss: margin is set by the broker"}
    return {"value": float(cost), "basis": "the debit paid"}


def risk_block(*, theoretical: dict[str, Any], stats: dict[str, Any], capital: dict[str, Any]) -> dict[str, Any]:
    """The four measures side by side, as every 3.0 reply carries them."""
    return {
        "theoretical_max_loss": theoretical.get("value"),
        "theoretical_basis": theoretical.get("basis"),
        "unlimited_loss": bool(theoretical.get("unlimited")),
        "defined_risk": theoretical.get("value") is not None,
        "modeled_worst_loss": stats.get("modeled_worst_loss"),
        "expected_shortfall": stats.get("expected_shortfall"),
        "capital_required": capital.get("value"),
        "capital_basis": capital.get("basis"),
    }


def finite(x: Any) -> bool:
    try:
        return x is not None and math.isfinite(float(x))
    except (TypeError, ValueError):
        return False


__all__ = ["TAIL", "weighted_quantile", "monotone", "outcome_stats", "PayoffLeg", "theoretical_max_loss", "legs_from_built",
           "capital_required", "risk_block", "finite"]
