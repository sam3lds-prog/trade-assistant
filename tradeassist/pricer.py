"""Limit-price suggestions for a single option contract.

Prices are worked in integer cents to avoid float drift. The suggestions are a ladder
of valid prices inside the bid-ask spread plus a plan for walking the order up (or
down, when selling). Nothing here predicts a fill: whether a price inside the spread
fills depends on the market makers on the other side, which no quote shows.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Iterable

LOW_PRICE_LIMIT = 300  # cents: US option tick sizes change at $3.00

RUNG_POSITIONS = {"patient": 0.50, "balanced": 0.65, "fast": 0.80}


@dataclass(frozen=True)
class TickRule:
    low: int        # tick in cents for prices under $3
    high: int       # tick in cents for prices at $3 and above
    evidence: str
    assumed: bool = False   # True when no quote proved a finer tick, so the coarsest scheme was assumed
    stated: bool = False    # True when the data source states the increments itself

    def tick_at(self, cents: int) -> int:
        return self.low if cents < LOW_PRICE_LIMIT else self.high


def to_cents(price: float) -> int:
    return int(round(price * 100))


def infer_tick_rule(quotes: Iterable[float | None]) -> TickRule:
    """Work out the class's price increments from the quotes actually showing in the chain.

    US equity options trade in one of three schemes: $0.01 under $3 and $0.05 at or above
    (penny-program classes), $0.05 and $0.10 (other classes), or $0.01 everywhere (a few
    heavily traded ETFs). Only those three are ever returned. A quote that is not a multiple
    of a coarser tick proves a finer scheme. With no such evidence the coarsest scheme is
    assumed, whose prices are valid in the finer schemes too.
    """
    low, high = [], []
    for q in quotes:
        if q is None or q <= 0:
            continue
        cents = to_cents(q)
        (low if cents < LOW_PRICE_LIMIT else high).append(cents)
    counts = f"{len(low)} quotes under $3 and {len(high)} at or above $3 in this expiration"
    if any(c % 5 for c in high):
        return TickRule(1, 1, f"{counts} show $0.01 increments at every price")
    if any(c % 5 for c in low) or any(c % 10 for c in high):
        return TickRule(1, 5, f"{counts} show $0.01 increments under $3 and $0.05 above")
    return TickRule(5, 10, f"{counts} all sit on $0.05 / $0.10 steps, so those are assumed; this class may "
                           "allow finer prices", assumed=True)


KNOWN_SCHEMES = {(1, 5), (5, 10), (1, 1)}


def stated_tick_rule(min_ticks: Any, quotes: Iterable[float | None] = ()) -> TickRule | None:
    """Price increments as the data source states them ({"below_tick", "above_tick", "cutoff_price"}).

    Used only when it is one of the three schemes US equity options trade in, changes at $3.00, and
    no quote in the chain contradicts it. Otherwise None, and the caller works it out from quotes."""
    if not isinstance(min_ticks, dict):
        return None
    try:
        low, high = to_cents(float(min_ticks["below_tick"])), to_cents(float(min_ticks["above_tick"]))
        cutoff = to_cents(float(min_ticks["cutoff_price"]))
    except (KeyError, TypeError, ValueError):
        return None
    if (low, high) not in KNOWN_SCHEMES or (cutoff != LOW_PRICE_LIMIT and low != high):
        return None
    rule = TickRule(low, high, f"the data source states ${low / 100:.2f} increments under $3 and "
                               f"${high / 100:.2f} at or above", stated=True)
    for q in quotes:
        if q is not None and q > 0 and to_cents(q) % rule.tick_at(to_cents(q)):
            return None                  # a live quote sits between the stated steps: do not trust them
    return rule


def tick_rule(quotes: Iterable[float | None], stated: Any = None) -> TickRule:
    """The class's price increments: as stated by the data source when that is credible, else inferred."""
    quotes = list(quotes)
    return stated_tick_rule(stated, quotes) or infer_tick_rule(quotes)


def valid_prices(lo_cents: int, hi_cents: int, rule: TickRule, limit: int = 5000) -> list[int]:
    """Every valid price from lo to hi inclusive, in cents."""
    out: list[int] = []
    tick = rule.tick_at(lo_cents)
    c = -(-lo_cents // tick) * tick  # round up to a valid price
    if c >= LOW_PRICE_LIMIT and rule.tick_at(c) != tick:
        tick = rule.tick_at(c)
        c = -(-lo_cents // tick) * tick
    while c <= hi_cents and len(out) < limit:
        out.append(c)
        c += rule.tick_at(c)
    return out


def _floor(prices: list[int], target: float) -> int:
    below = [p for p in prices if p <= target + 1e-9]
    return below[-1] if below else prices[0]


def _ceil(prices: list[int], target: float) -> int:
    above = [p for p in prices if p >= target - 1e-9]
    return above[0] if above else prices[-1]


def _nearest(prices: list[int], target: float) -> int:
    return min(prices, key=lambda p: (abs(p - target), p))


def quote_age_seconds(contract: dict[str, Any], now: datetime | None = None) -> float | None:
    """Age of the older of the two quote sides, in seconds."""
    now = now or datetime.now(timezone.utc)
    ages = []
    for key in ("bid_time", "ask_time"):
        stamp = contract.get(key)
        if stamp:
            try:
                ages.append((now - datetime.fromisoformat(stamp)).total_seconds())
            except ValueError:
                pass
    return max(ages) if ages else None


def build_ladder(bid: float | None, ask: float | None, rule: TickRule, side: str = "buy",
                 model_value: float | None = None, contracts: int = 1,
                 calibration: dict[str, Any] | None = None) -> dict[str, Any]:
    """Suggested limit prices for buying (or selling) one option contract.

    Returns a dict with `ok`, `rungs`, `walk`, `flags` and the reference numbers.
    `position` on each rung is how far across the spread the price sits, measured from
    the passive side: 0 = joining your own side of the quote, 1 = crossing the spread.
    """
    if side not in ("buy", "sell"):
        raise ValueError("side must be buy or sell")
    flags: list[dict[str, str]] = []
    result: dict[str, Any] = {"ok": False, "side": side, "bid": bid, "ask": ask, "rungs": [], "walk": None,
                              "flags": flags, "tick_rule": {"low": rule.low / 100, "high": rule.high / 100,
                                                            "evidence": rule.evidence, "assumed": rule.assumed}}

    if ask is None or ask <= 0:
        flags.append({"level": "fail", "text": "There is no offer on this contract right now, so there is "
                                               "nothing to price against."})
        return result
    if bid is None:
        bid = 0.0
    bid_c, ask_c = to_cents(bid), to_cents(ask)
    if bid_c > ask_c:
        flags.append({"level": "fail", "text": "The bid is above the ask. These quotes are stale or crossed; "
                                               "refresh before pricing."})
        return result
    if bid_c == ask_c:
        flags.append({"level": "warn", "text": "Bid and ask are the same price (a locked market). "
                                               "The only sensible limit is that price."})
    if bid_c == 0:
        if side == "sell":
            flags.append({"level": "fail", "text": "There is no bid, so there is no one to sell to at any price "
                                                   "right now."})
            return result
        flags.append({"level": "warn", "text": "There is no bid. The midpoint means little on a one-sided "
                                               "market; lean on the model value and expect a hard exit."})

    spread_c = ask_c - bid_c
    mid_c = 0.5 * (bid_c + ask_c)
    grid = valid_prices(bid_c, ask_c, rule)
    if bid_c == 0:
        grid = [p for p in grid if p > 0] or grid
    if not grid:
        grid = [ask_c]

    model_c = model_value * 100 if model_value is not None and model_value > 0 else None
    skew = None
    patient_target = mid_c
    if model_c is not None and spread_c > 0:
        skew = (model_c - mid_c) / spread_c
        inside = bid_c <= model_c <= ask_c
        # A gap smaller than one price increment is noise, not a stale quote.
        material = abs(model_c - mid_c) >= rule.tick_at(int(mid_c))
        if not material or not inside:
            pass
        elif side == "buy" and skew <= -0.25:
            patient_target = model_c
            flags.append({"level": "warn", "text": (
                f"The midpoint (${mid_c / 100:.2f}) looks high against the model value (${model_c / 100:.2f}). "
                "One side of the quote may be stale. The patient price starts at the model value instead.")})
        elif side == "sell" and skew >= 0.25:
            patient_target = model_c
            flags.append({"level": "warn", "text": (
                f"The midpoint (${mid_c / 100:.2f}) looks low against the model value (${model_c / 100:.2f}). "
                "One side of the quote may be stale. The patient price starts at the model value instead.")})
        elif abs(skew) >= 0.25:
            direction = "above" if skew > 0 else "below"
            flags.append({"level": "info", "text": (
                f"The model value (${model_c / 100:.2f}) is {direction} the midpoint (${mid_c / 100:.2f}). "
                "A mid-price order may sit unfilled; expect to walk it.")})
        if not inside:
            flags.append({"level": "warn", "text": (
                f"The model value (${model_c / 100:.2f}) is outside the bid-ask range (${bid_c / 100:.2f} to "
                f"${ask_c / 100:.2f}), so the model and the market disagree. The suggestions below use the "
                "quote alone; be wary of both.")})

    def target_at(position: float) -> float:
        # position 0.5 is the midpoint; further rungs move toward the far side of the spread.
        base = patient_target
        far = ask_c if side == "buy" else bid_c
        if position <= 0.5:
            return base
        return base + (far - base) * (position - 0.5) / 0.5

    if side == "buy":
        picks = {"patient": _floor(grid, target_at(0.50)), "balanced": _nearest(grid, target_at(0.65)),
                 "fast": _ceil(grid, target_at(0.80))}
    else:
        picks = {"patient": _ceil(grid, target_at(0.50)), "balanced": _nearest(grid, target_at(0.65)),
                 "fast": _floor(grid, target_at(0.80))}
    # Keep the ladder monotonic toward the far side.
    order = ["patient", "balanced", "fast"]
    for prev, cur in zip(order, order[1:]):
        if side == "buy":
            picks[cur] = max(picks[cur], picks[prev])
        else:
            picks[cur] = min(picks[cur], picks[prev])

    descriptions = {
        "patient": "Best price, least likely to fill quickly. Start here when you are not in a hurry.",
        "balanced": "A middle course: gives up part of the spread for a better chance of a fill.",
        "fast": "Close to crossing the spread. Use when getting filled matters more than the last few cents.",
    }
    seen: dict[int, dict[str, Any]] = {}
    for name in order:
        cents = picks[name]
        position = _position(cents, bid_c, ask_c, side)
        if cents in seen:
            seen[cents]["names"].append(name)
            continue
        far_c = ask_c if side == "buy" else bid_c
        saving_c = (far_c - cents) if side == "buy" else (cents - far_c)
        rung = {
            "names": [name], "price": cents / 100, "position": position,
            "saving_vs_crossing": saving_c * contracts,          # dollars, since 1 cent x 100 shares = $1
            "note": descriptions[name],
            "observed_fill_rate": _bucket_rate(calibration, position),
        }
        if cents == bid_c and side == "buy":
            rung["note"] = "Joins the bid. You are in line with other buyers and may wait a long time."
        if cents == ask_c and side == "buy":
            rung["note"] = "This is the ask. It should fill at once but pays the full spread."
        if cents == ask_c and side == "sell":
            rung["note"] = "Joins the ask. You are in line with other sellers and may wait a long time."
        if cents == bid_c and side == "sell":
            rung["note"] = "This is the bid. It should fill at once but gives up the full spread."
        seen[cents] = rung
    rungs = list(seen.values())
    for rung in rungs:
        rung["name"] = " / ".join(rung.pop("names"))

    start_c, cap_c = picks["patient"], picks["fast"]
    lo, hi = min(start_c, cap_c), max(start_c, cap_c)
    steps = [p for p in grid if lo <= p <= hi]
    if side == "sell":
        steps = steps[::-1]
    max_steps = 8
    if len(steps) > max_steps:  # thin a long walk to a handful of evenly spaced prices
        idx = sorted({round(i * (len(steps) - 1) / (max_steps - 1)) for i in range(max_steps)})
        steps = [steps[i] for i in idx]
    result["walk"] = {
        "start": start_c / 100, "cap": cap_c / 100, "steps": [s / 100 for s in steps],
        "wait_seconds": 25,
        "instructions": (
            "Enter the first price. If it has not filled after about 25 seconds, edit the order to the next "
            "price. Stop at the cap: if it will not fill there, the market has moved or the contract is too "
            "thin, and reassessing beats chasing."),
    }
    result.update({
        "ok": True, "bid": bid_c / 100, "ask": ask_c / 100, "mid": mid_c / 100,
        "spread": spread_c / 100, "spread_pct": (100.0 * spread_c / mid_c) if mid_c > 0 else None,
        "spread_ticks": max(len(grid) - 1, 0), "model_value": model_c / 100 if model_c is not None else None,
        "model_skew": skew, "rungs": rungs, "grid": [p / 100 for p in grid] if len(grid) <= 60 else None,
        "contracts": contracts,
    })
    if spread_c > 0 and len(grid) == 2:
        if rule.assumed and spread_c > 1:
            flags.append({"level": "info", "text": "No quote in this expiration shows a price finer than the "
                                                   "$0.05 / $0.10 steps, so none is suggested between the bid "
                                                   "and the ask. If Robinhood accepts one, use it."})
        else:
            flags.append({"level": "info", "text": "The spread is one tick wide. There is no price between the "
                                                   "bid and the ask: either wait on your side or cross."})
    return result


def _position(cents: int, bid_c: int, ask_c: int, side: str) -> float:
    if ask_c == bid_c:
        return 1.0
    frac = (cents - bid_c) / (ask_c - bid_c)
    return frac if side == "buy" else 1.0 - frac


BUCKETS = [(0.0, 0.50, "up to the midpoint"), (0.50, 0.65, "midpoint to 65%"),
           (0.65, 0.80, "65% to 80%"), (0.80, 1.0001, "80% to the far side")]


def bucket_for(position: float) -> int:
    for i, (lo, hi, _) in enumerate(BUCKETS):
        if (position <= hi) if i == 0 else (lo < position <= hi):
            return i
    return len(BUCKETS) - 1


def _bucket_rate(calibration: dict[str, Any] | None, position: float) -> dict[str, Any] | None:
    if not calibration:
        return None
    bucket = calibration.get("buckets", [])[bucket_for(position)] if calibration.get("buckets") else None
    if not bucket or bucket["attempts"] == 0:
        return None
    return {"attempts": bucket["attempts"], "fill_rate": bucket["fill_rate"], "label": bucket["label"]}
