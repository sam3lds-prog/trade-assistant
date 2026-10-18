"""Limit-price suggestions for a single option contract.

Prices are worked in integer cents to avoid float drift. The suggestions are a ladder
of valid prices inside the bid-ask spread plus a plan for walking the order up (or
down, when selling). Nothing here predicts a fill: whether a price inside the spread
fills depends on the market makers on the other side, which no quote shows.

**The fair price (2.10).** Up to 2.9 the ladder started at the midpoint and walked up to 80% of
the way to the ask. On a wide option spread market makers often fill below the midpoint, so that
paid too much. The ladder now centres on a fair price worked out from the evidence that is there:

    the midpoint                       always
    the model value                    the contract priced at the volatility of its neighbouring
                                       strikes, when it sits inside the bid and ask
    the size-weighted midpoint         when both quote sizes are given: more contracts waiting to
                                       buy than to sell pull it toward the ask, and the reverse
    the last trade                     when it printed inside the bid and ask within 15 minutes,
                                       while the market is open

When the midpoint sits a quarter of the spread or more from a model value inside the quote, one side
of the quote looks stale: the midpoint and the size-weighted midpoint (built from the same quote)
are left out, and the model value (with a recent trade, if any) carries the fair price.

The fair price is the middle (median) of the evidence used. A buyer's fair price is never above
the midpoint and a seller's never below it: evidence may lower what you pay, never raise it.
The patient price sits below the fair price, the walk stops at it, and the fast price, past it,
is shown for when a fill matters more than the cents. Robinhood's own estimates of a price that
is likely to fill, when its connection sends them, are shown beside the evidence for reference;
they are not used, because how Robinhood works them out is not published.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from statistics import median
from typing import Any, Iterable

LOW_PRICE_LIMIT = 300  # cents: US option tick sizes change at $3.00

# Where the rungs sit (2.10): the patient price this share of the way from the fair price back to your own
# side of the quote, the fast price this share of the way from the fair price to the far side.
PATIENT_SHARE = 0.40
FAST_SHARE = 0.30
LAST_TRADE_MAX_AGE = 15 * 60       # seconds: an older trade says little about where the quote sits now

FILL_PRICE_LABELS = {
    "buy_likely": "Robinhood's buy price likely to fill",
    "buy_less_likely": "Robinhood's buy price less likely to fill",
    "sell_likely": "Robinhood's sell price likely to fill",
    "sell_less_likely": "Robinhood's sell price less likely to fill",
}


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
    """Age of the older of the two quote sides, in seconds.

    A stamp without a time zone is read in the same zone as `now` rather than failing: a source that
    drops the offset still gives an age, which is what the freshness checks are asking for."""
    now = now or datetime.now(timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    ages = []
    for key in ("bid_time", "ask_time"):
        stamp = contract.get(key)
        if stamp:
            try:
                when = datetime.fromisoformat(stamp)
            except (TypeError, ValueError):
                continue
            if when.tzinfo is None:
                when = when.replace(tzinfo=now.tzinfo)
            ages.append((now - when).total_seconds())
    return max(ages) if ages else None


def age_seconds(stamp: Any, now: datetime | None = None) -> float | None:
    """Seconds since an ISO time; None when there is no readable time. A time without a zone is read as UTC."""
    if not stamp:
        return None
    try:
        when = datetime.fromisoformat(str(stamp).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    now = now or datetime.now(timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    return (now - when).total_seconds()


def _finite_positive(value: Any) -> float | None:
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    return out if out == out and out not in (float("inf"), float("-inf")) and out > 0 else None


def fair_value(bid: float | None, ask: float | None, side: str = "buy", *, model_value: float | None = None,
               bidsize: float | None = None, asksize: float | None = None, last: float | None = None,
               last_age_seconds: float | None = None, quote_live: bool = True,
               fill_prices: dict[str, Any] | None = None, tick_cents: int = 1) -> dict[str, Any] | None:
    """The fair price to anchor a limit on, from the evidence that is there (see the module notes).

    Returns None when there is no usable quote (no offer, or a crossed one). Otherwise `fair` is the median
    of the evidence used, `anchor` is the price the ladder centres on (the fair price, but never above the
    midpoint when buying nor below it when selling) and `evidence` lists every piece, used or not, with why."""
    if side not in ("buy", "sell"):
        raise ValueError("side must be buy or sell")
    ask_v = _finite_positive(ask)
    if ask_v is None:
        return None
    bid_v = _finite_positive(bid) or 0.0
    bid_c, ask_c = to_cents(bid_v), to_cents(ask_v)
    if bid_c > ask_c:
        return None
    mid_c = 0.5 * (bid_c + ask_c)
    spread_c = ask_c - bid_c
    evidence: list[dict[str, Any]] = [{"key": "midpoint", "label": "Midpoint of the bid and ask", "price": mid_c / 100,
                                       "used": True, "note": "Halfway between the bid and the ask."}]
    used = [mid_c]

    model = _finite_positive(model_value)
    if model is not None:
        model_c = model * 100
        inside = bid_c <= model_c <= ask_c
        evidence.append({"key": "model", "label": "Model value", "price": model, "used": inside,
                         "note": ("The contract priced at the volatility of its neighbouring strikes: what it is worth "
                                  "if its quote matched the rest of the chain." if inside else
                                  "Outside the bid and ask, so the model and the market disagree; not used.")})
        if inside:
            used.append(model_c)
    # A quote whose midpoint sits a quarter of the spread or more from a model value inside it (and at least a
    # price step away) has one side out of line with the rest of the chain, usually a stale bid or offer. Then
    # the midpoint, and the size-weighted midpoint built from the same quote, are not evidence of value.
    stale = (model is not None and spread_c > 0 and bid_c <= model * 100 <= ask_c
             and abs(model * 100 - mid_c) >= max(tick_cents, 1) and abs(model * 100 - mid_c) >= 0.25 * spread_c)
    if stale:
        used.remove(mid_c)
        evidence[0].update(used=False, note="A quarter of the spread or more from the model value: one side of the "
                                            "quote looks stale, so the midpoint is not used.")

    b_size, a_size = _finite_positive(bidsize), _finite_positive(asksize)
    if b_size and a_size and bid_c > 0 and spread_c >= 2 * max(tick_cents, 1):
        micro_c = (bid_c * a_size + ask_c * b_size) / (b_size + a_size)
        evidence.append({"key": "size_weighted", "label": "Size-weighted midpoint", "price": micro_c / 100,
                         "used": not stale,
                         "note": (f"{b_size:,.0f} contracts bid against {a_size:,.0f} offered: more waiting to buy than "
                                  "to sell pulls it toward the ask, and the reverse." if not stale else
                                  "Built from the same quote as the midpoint, which looks stale; not used.")})
        if not stale:
            used.append(micro_c)

    last_v = _finite_positive(last)
    if last_v is not None:
        last_c = last_v * 100
        inside = bid_c <= last_c <= ask_c
        fresh = quote_live and last_age_seconds is not None and 0 <= last_age_seconds <= LAST_TRADE_MAX_AGE
        if fresh and inside:
            note = f"Traded {_age_words(last_age_seconds)} ago, inside today's quote."
        elif not quote_live:
            note = "The market is closed, so the last trade says little about the next fill; not used."
        elif last_age_seconds is None:
            note = "The data source gave no time for the last trade, so its age is unknown; not used."
        elif not fresh:
            note = f"Traded {_age_words(last_age_seconds)} ago, too long ago to say where it fills now; not used."
        else:
            note = "Traded outside today's bid and ask; not used."
        evidence.append({"key": "last_trade", "label": "Last trade", "price": last_v, "used": fresh and inside,
                         "note": note})
        if fresh and inside:
            used.append(last_c)

    for key, label in FILL_PRICE_LABELS.items():
        value = _finite_positive((fill_prices or {}).get(key))
        if value is not None and key.startswith(side):
            evidence.append({"key": f"robinhood_{key}", "label": label, "price": value, "used": False,
                             "note": "Robinhood's own estimate, shown for reference. Not used in the fair price: "
                                     "how Robinhood works it out is not published."})

    fair_c = float(median(used))
    anchor_c = min(mid_c, fair_c) if side == "buy" else max(mid_c, fair_c)
    names = [e["label"].lower() for e in evidence if e["used"]]
    if names == ["midpoint of the bid and ask"]:
        basis = "The midpoint: no other evidence was usable."
    elif len(names) == 1:
        basis = f"The {names[0]}: the quote looks stale on one side, so it is the only evidence used."
    else:
        basis = f"The middle of {len(names)} pieces of evidence: {', '.join(names)}."
        if stale:
            basis += " The quote looks stale on one side, so its midpoint is left out."
    if anchor_c != fair_c:
        basis += (" The evidence points above the midpoint, so a buyer starts from the midpoint instead."
                  if side == "buy" else
                  " The evidence points below the midpoint, so a seller starts from the midpoint instead.")
    return {"side": side, "mid": mid_c / 100, "fair": fair_c / 100, "anchor": anchor_c / 100, "stale_quote": stale,
            "below_mid": anchor_c < mid_c - 1e-9, "above_mid": anchor_c > mid_c + 1e-9,
            "evidence": evidence, "used": len(names), "basis": basis}


def _age_words(seconds: float) -> str:
    if seconds < 90:
        return f"{seconds:.0f} seconds"
    if seconds < 5400:
        return f"{seconds / 60:.0f} minutes"
    return f"{seconds / 3600:.1f} hours"


def build_ladder(bid: float | None, ask: float | None, rule: TickRule, side: str = "buy",
                 model_value: float | None = None, contracts: int = 1,
                 calibration: dict[str, Any] | None = None, *, bidsize: float | None = None,
                 asksize: float | None = None, last: float | None = None, last_age_seconds: float | None = None,
                 quote_live: bool = True, fill_prices: dict[str, Any] | None = None) -> dict[str, Any]:
    """Suggested limit prices for buying (or selling) one option contract.

    Returns a dict with `ok`, `rungs`, `walk`, `flags`, `fair` and the reference numbers.
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
        grid = [p for p in grid if p > 0]
    if not grid:                                   # a quote off the class's increments: only the ask is a price
        grid = [ask_c]

    model_c = model_value * 100 if model_value is not None and model_value > 0 else None
    skew = None
    if model_c is not None and spread_c > 0:
        skew = (model_c - mid_c) / spread_c
        inside = bid_c <= model_c <= ask_c
        # A gap smaller than one price increment is noise, not a stale quote.
        material = abs(model_c - mid_c) >= rule.tick_at(int(mid_c))
        if not material or not inside:
            pass
        elif side == "buy" and skew <= -0.25:
            flags.append({"level": "warn", "text": (
                f"The midpoint (${mid_c / 100:.2f}) looks high against the model value (${model_c / 100:.2f}). "
                "One side of the quote may be stale. The fair price leans toward the model value.")})
        elif side == "sell" and skew >= 0.25:
            flags.append({"level": "warn", "text": (
                f"The midpoint (${mid_c / 100:.2f}) looks low against the model value (${model_c / 100:.2f}). "
                "One side of the quote may be stale. The fair price leans toward the model value.")})
        elif abs(skew) >= 0.25:
            direction = "above" if skew > 0 else "below"
            flags.append({"level": "info", "text": (
                f"The model value (${model_c / 100:.2f}) is {direction} the midpoint (${mid_c / 100:.2f}). "
                "A fair-price order may sit unfilled; expect to wait, or to walk it.")})
        if not inside:
            flags.append({"level": "warn", "text": (
                f"The model value (${model_c / 100:.2f}) is outside the bid-ask range (${bid_c / 100:.2f} to "
                f"${ask_c / 100:.2f}), so the model and the market disagree. The fair price below leaves the model "
                "out; be wary of both.")})

    fair = fair_value(bid_c / 100, ask_c / 100, side, model_value=model_value, bidsize=bidsize, asksize=asksize,
                      last=last, last_age_seconds=last_age_seconds, quote_live=quote_live, fill_prices=fill_prices,
                      tick_cents=rule.tick_at(int(mid_c)))
    anchor_c = fair["anchor"] * 100 if fair else mid_c
    if side == "buy":
        picks = {"patient": _floor(grid, anchor_c - PATIENT_SHARE * (anchor_c - bid_c)),
                 "balanced": _floor(grid, anchor_c),
                 "fast": _ceil(grid, anchor_c + FAST_SHARE * (ask_c - anchor_c))}
    else:
        picks = {"patient": _ceil(grid, anchor_c + PATIENT_SHARE * (ask_c - anchor_c)),
                 "balanced": _ceil(grid, anchor_c),
                 "fast": _floor(grid, anchor_c - FAST_SHARE * (anchor_c - bid_c))}
    # Keep the ladder monotonic toward the far side.
    order = ["patient", "balanced", "fast"]
    for prev, cur in zip(order, order[1:]):
        if side == "buy":
            picks[cur] = max(picks[cur], picks[prev])
        else:
            picks[cur] = min(picks[cur], picks[prev])

    descriptions = {
        "patient": "Below the fair price: the best price, least likely to fill quickly. Start here when you can wait.",
        "balanced": ("The fair price (the evidence below). The walk stops here: never above the midpoint when buying."
                     if side == "buy" else
                     "The fair price (the evidence below). The walk stops here: never below the midpoint when selling."),
        "fast": "Past the fair price, toward crossing the spread. Only when a fill now matters more than the cents.",
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

    if fair is not None:
        fair["limit"] = picks["balanced"] / 100     # the valid price to type: the fair price, rounded your way
        if side == "buy" and picks["balanced"] > anchor_c + 1e-9:
            # No valid price at or below the fair price: with no bid and a one-step offer, the ask is the only
            # price there is. Said plainly rather than shown as a "fair" price above the estimate.
            fair["limit"] = None
            flags.append({"level": "warn", "text": (
                f"No valid price lies at or below the fair estimate (${anchor_c / 100:.3f}): the only price to pay is "
                f"the ask, ${ask_c / 100:.2f}. A contract quoted like this is close to worthless; be sure you want it.")})
    # The walk goes from the patient price to the fair price and stops there (2.10; it used to run on to 80%
    # of the way across the spread). The fast price stays on the ladder for when a fill matters more.
    start_c, cap_c = picks["patient"], picks["balanced"]
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
            "price. Stop at the fair price: if it will not fill there, wait for the quote to come to you or "
            "reassess. Paying the fast price buys speed, not value."),
    }
    result.update({
        "ok": True, "bid": bid_c / 100, "ask": ask_c / 100, "mid": mid_c / 100,
        "spread": spread_c / 100, "spread_pct": (100.0 * spread_c / mid_c) if mid_c > 0 else None,
        "spread_ticks": max(len(grid) - 1, 0), "model_value": model_c / 100 if model_c is not None else None,
        "model_skew": skew, "rungs": rungs, "grid": [p / 100 for p in grid] if len(grid) <= 60 else None,
        "contracts": contracts, "fair": fair,
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
