"""Multi-leg option strategies built from listed contracts, for the simulator.

A strategy is a template (an iron condor, a bull put spread, a calendar...) resolved against
the stock's real option chain: each leg is a listed contract with a two-sided quote, chosen
by delta, by distance from the stock price, or by the strikes you name. The result is the
same set of legs the simulator takes, with the net debit or credit at the midpoints and at
the bid and ask, and the risk at expiration worked out by the simulator itself.

Nothing here places an order. Positions with sold options can be modelled but are not
recorded on the Positions screen.

`parse` reads a description such as "AAPL iron condor 45 days 16 delta 5 wide" into a
template and its settings, saying what it understood and what it assumed. It reads only
what it recognises; anything it cannot place is reported rather than guessed.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Any, Callable, Sequence

from ..chain import two_sided
from ..optionmath import NY


class StrategyError(ValueError):
    pass


@dataclass(frozen=True)
class Template:
    key: str
    label: str
    group: str                  # bullish, bearish, neutral, volatile, time
    description: str
    roles: tuple[tuple[str, str, int, str], ...]   # (option_type, side, contracts per unit, role name), strike order
    defined_risk: bool
    defaults: dict[str, Any] = field(default_factory=dict)
    wings: tuple[int, ...] = ()           # strike positions that move out when widening, in role order
    wing_dirs: tuple[int, ...] = ()       # -1 moves down, +1 moves up, for each wing
    calendar: bool = False


T = Template
TEMPLATES: dict[str, Template] = {t.key: t for t in [
    T("long_call", "Long call", "bullish", "Buy one call. The most you can lose is what you pay.",
      (("call", "buy", 1, "call bought"),), True, {"delta": 0.50, "dte": 45}),
    T("long_put", "Long put", "bearish", "Buy one put. The most you can lose is what you pay.",
      (("put", "buy", 1, "put bought"),), True, {"delta": 0.50, "dte": 45}),
    T("bull_call_spread", "Bull call spread (debit)", "bullish",
      "Buy a call and sell a higher one: a cheaper bet on a rise, with the gain capped at the upper strike.",
      (("call", "buy", 1, "call bought"), ("call", "sell", 1, "call sold")), True, {"delta": 0.50, "dte": 45},
      wings=(1,), wing_dirs=(1,)),
    T("bear_put_spread", "Bear put spread (debit)", "bearish",
      "Buy a put and sell a lower one: a cheaper bet on a fall, with the gain capped at the lower strike.",
      (("put", "sell", 1, "put sold"), ("put", "buy", 1, "put bought")), True, {"delta": 0.50, "dte": 45},
      wings=(0,), wing_dirs=(-1,)),
    T("bull_put_spread", "Bull put spread (credit)", "bullish",
      "Sell a put and buy a lower one: collect a credit that is kept if the stock stays above the sold strike.",
      (("put", "buy", 1, "put bought"), ("put", "sell", 1, "put sold")), True, {"delta": 0.30, "dte": 45},
      wings=(0,), wing_dirs=(-1,)),
    T("bear_call_spread", "Bear call spread (credit)", "bearish",
      "Sell a call and buy a higher one: collect a credit that is kept if the stock stays below the sold strike.",
      (("call", "sell", 1, "call sold"), ("call", "buy", 1, "call bought")), True, {"delta": 0.30, "dte": 45},
      wings=(1,), wing_dirs=(1,)),
    T("long_straddle", "Long straddle", "volatile",
      "Buy the at-the-money call and put: gains on a big move either way, loses if the stock stays put.",
      (("put", "buy", 1, "put bought"), ("call", "buy", 1, "call bought")), True, {"dte": 30}),
    T("short_straddle", "Short straddle", "neutral",
      "Sell the at-the-money call and put: collects the most premium, with no limit to the loss on a big move.",
      (("put", "sell", 1, "put sold"), ("call", "sell", 1, "call sold")), False, {"dte": 30}),
    T("long_strangle", "Long strangle", "volatile",
      "Buy an out-of-the-money put and call: cheaper than a straddle, needs a bigger move.",
      (("put", "buy", 1, "put bought"), ("call", "buy", 1, "call bought")), True, {"delta": 0.25, "dte": 30},
      wings=(0, 1), wing_dirs=(-1, 1)),
    T("short_strangle", "Short strangle", "neutral",
      "Sell an out-of-the-money put and call: keeps the credit if the stock stays between them, with no limit "
      "to the loss beyond.",
      (("put", "sell", 1, "put sold"), ("call", "sell", 1, "call sold")), False, {"delta": 0.20, "dte": 45},
      wings=(0, 1), wing_dirs=(-1, 1)),
    T("iron_condor", "Iron condor", "neutral",
      "Sell an out-of-the-money put spread and call spread: a credit kept if the stock stays between the sold "
      "strikes, with the loss capped by the wings.",
      (("put", "buy", 1, "put wing bought"), ("put", "sell", 1, "put sold"), ("call", "sell", 1, "call sold"),
       ("call", "buy", 1, "call wing bought")), True, {"delta": 0.20, "dte": 45},
      wings=(0, 3), wing_dirs=(-1, 1)),
    T("iron_butterfly", "Iron butterfly", "neutral",
      "Sell the at-the-money put and call and buy wings either side: a larger credit than a condor, kept in "
      "full only if the stock ends at the middle strike.",
      (("put", "buy", 1, "put wing bought"), ("put", "sell", 1, "put sold"), ("call", "sell", 1, "call sold"),
       ("call", "buy", 1, "call wing bought")), True, {"dte": 30}, wings=(0, 3), wing_dirs=(-1, 1)),
    T("call_butterfly", "Butterfly (calls)", "neutral",
      "Buy a call, sell two higher, buy one higher still, evenly spaced: a small debit that pays most if the "
      "stock ends at the middle strike.",
      (("call", "buy", 1, "lower call bought"), ("call", "sell", 2, "middle calls sold"),
       ("call", "buy", 1, "upper call bought")), True, {"dte": 30}, wings=(0, 2), wing_dirs=(-1, 1)),
    T("put_butterfly", "Butterfly (puts)", "neutral",
      "The same shape with puts: pays most if the stock ends at the middle strike.",
      (("put", "buy", 1, "lower put bought"), ("put", "sell", 2, "middle puts sold"),
       ("put", "buy", 1, "upper put bought")), True, {"dte": 30}, wings=(0, 2), wing_dirs=(-1, 1)),
    T("call_calendar", "Calendar (calls)", "time",
      "Sell a near call and buy a later one at the same strike: gains from the near option's faster decay if "
      "the stock stays near the strike.",
      (("call", "sell", 1, "near call sold"), ("call", "buy", 1, "later call bought")), True,
      {"dte": 30, "far_days": 30}, calendar=True),
    T("put_calendar", "Calendar (puts)", "time",
      "Sell a near put and buy a later one at the same strike.",
      (("put", "sell", 1, "near put sold"), ("put", "buy", 1, "later put bought")), True,
      {"dte": 30, "far_days": 30}, calendar=True),
]}
del T


# --- choosing contracts -----------------------------------------------------------------------

def quoted(analysis: Any, kind: str) -> list[dict[str, Any]]:
    """Contracts of one type with a two-sided quote, lowest strike first."""
    return sorted((c for c in analysis.contracts if c["option_type"] == kind and two_sided(c)), key=lambda c: c["strike"])


def _delta(c: dict[str, Any]) -> float | None:
    d = c.get("delta")
    try:
        d = float(d)
    except (TypeError, ValueError):
        return None
    return d if math.isfinite(d) else None


def by_delta(analysis: Any, kind: str, target: float) -> dict[str, Any]:
    """The quoted contract whose delta is closest to `target` (0.30 means a 0.30 call or a -0.30 put)."""
    rows = [(abs(abs(d) - abs(target)), c) for c in quoted(analysis, kind) if (d := _delta(c)) is not None]
    if not rows:
        raise StrategyError(f"No quoted {kind}s with a delta for {analysis.expiration}")
    return min(rows, key=lambda x: (x[0], x[1]["strike"]))[1]


def nearest(analysis: Any, kind: str, price: float) -> dict[str, Any]:
    rows = quoted(analysis, kind)
    if not rows:
        raise StrategyError(f"No quoted {kind}s for {analysis.expiration}")
    return min(rows, key=lambda c: (abs(c["strike"] - price), c["strike"]))


def at_the_money(analysis: Any) -> float:
    """The listed strike nearest the forward with both a call and a put quoted."""
    calls = {c["strike"] for c in quoted(analysis, "call")}
    both = sorted(k for k in (c["strike"] for c in quoted(analysis, "put")) if k in calls)
    if not both:
        raise StrategyError(f"No strike with both a call and a put quoted for {analysis.expiration}")
    return min(both, key=lambda k: (abs(k - analysis.forward), k))


def beyond(analysis: Any, kind: str, strike: float, distance: float, direction: int) -> dict[str, Any]:
    """The quoted strike nearest `distance` dollars from `strike` in `direction`, at least one strike away."""
    rows = [c for c in quoted(analysis, kind) if (c["strike"] - strike) * direction > 1e-9]
    if not rows:
        raise StrategyError(f"No quoted {kind} {'above' if direction > 0 else 'below'} {strike:g} for {analysis.expiration}")
    return min(rows, key=lambda c: (abs(abs(c["strike"] - strike) - distance), abs(c["strike"] - strike)))


def step(analysis: Any, kind: str, strike: float, n: int) -> float:
    """The quoted strike `n` places above (n > 0) or below `strike`."""
    ks = [c["strike"] for c in quoted(analysis, kind)]
    if not ks:
        raise StrategyError(f"No quoted {kind}s for {analysis.expiration}")
    i = min(range(len(ks)), key=lambda j: abs(ks[j] - strike))
    j = i + n
    if not 0 <= j < len(ks):
        raise StrategyError(f"No quoted {kind} strike {'above' if n > 0 else 'below'} {ks[i]:g}")
    return ks[j]


def even_wings(analysis: Any, kind: str, middle: float, gap: float) -> tuple[float, float]:
    """Wings the same distance either side of `middle`: the smallest listed distance at least `gap`
    with a quoted strike on both sides; failing that, the nearest strikes (uneven, and noted)."""
    ks = {c["strike"] for c in quoted(analysis, kind)}
    for d in sorted({abs(k - middle) for k in ks if abs(k - middle) >= gap - 1e-9 and k != middle}):
        if any(abs(k - (middle - d)) < 1e-9 for k in ks) and any(abs(k - (middle + d)) < 1e-9 for k in ks):
            return middle - d, middle + d
    return beyond(analysis, kind, middle, gap, -1)["strike"], beyond(analysis, kind, middle, gap, 1)["strike"]


def default_width(analysis: Any) -> float:
    """About 2.5% of the stock price: a common distance between a sold strike and its wing."""
    return max(analysis.spot * 0.025, 0.5)


def pick_expiration(dates: Sequence[str], today: date, dte: int, after: str | None = None,
                    min_gap: int = 0) -> str:
    """The listed expiration nearest `dte` days out (and at least `min_gap` days after `after`)."""
    usable = [d for d in dates if (date.fromisoformat(d) - today).days >= 1]
    if after:
        floor = date.fromisoformat(after) + timedelta(days=max(min_gap, 1))
        usable = [d for d in usable if date.fromisoformat(d) >= floor]
    if not usable:
        raise StrategyError("No listed expiration fits" + (f" after {after}" if after else ""))
    return min(usable, key=lambda d: (abs((date.fromisoformat(d) - today).days - dte), d))


# --- resolving a template --------------------------------------------------------------------

def choose_strikes(t: Template, analysis: Any, params: dict[str, Any]) -> tuple[list[float], list[str]]:
    """Strikes in the template's role order, and a sentence for each choice."""
    delta = float(params.get("delta") or t.defaults.get("delta") or 0.5)
    if delta > 1:
        delta /= 100                                   # "16 delta" means 0.16
    if not 0.02 <= delta <= 0.98:
        raise StrategyError("delta must be between 0.02 and 0.98 (or 2 to 98)")
    width = params.get("width")
    width = float(width) if width else default_width(analysis)
    if width <= 0:
        raise StrategyError("width must be more than zero")
    center = params.get("center")
    why: list[str] = []

    def d(c: dict[str, Any]) -> str:
        v = _delta(c)
        return f"{abs(v):.2f} delta" if v is not None else "no delta"

    key = t.key
    if key in ("long_call", "long_put"):
        kind = t.roles[0][0]
        c = nearest(analysis, kind, float(center)) if center else by_delta(analysis, kind, delta)
        why.append(f"{t.roles[0][3].capitalize()}: {c['strike']:g} ({d(c)})" + ("" if center else f", nearest {delta:.2f}"))
        return [c["strike"]], why
    if key in ("bull_call_spread", "bear_put_spread"):
        kind = t.roles[0][0]
        long = by_delta(analysis, kind, delta) if not center else nearest(analysis, kind, float(center))
        other = beyond(analysis, kind, long["strike"], width, 1 if kind == "call" else -1)
        why.append(f"{kind.capitalize()} bought: {long['strike']:g} ({d(long)})")
        why.append(f"{kind.capitalize()} sold: {other['strike']:g}, about ${width:.2f} {'above' if kind == 'call' else 'below'}")
        return sorted([long["strike"], other["strike"]]), why
    if key in ("bull_put_spread", "bear_call_spread"):
        kind = "put" if key == "bull_put_spread" else "call"
        short = by_delta(analysis, kind, delta)
        wing = beyond(analysis, kind, short["strike"], width, -1 if kind == "put" else 1)
        why.append(f"{kind.capitalize()} sold: {short['strike']:g} ({d(short)}, nearest {delta:.2f})")
        why.append(f"{kind.capitalize()} bought: {wing['strike']:g}, about ${width:.2f} further out")
        return sorted([short["strike"], wing["strike"]]), why
    if key in ("long_straddle", "short_straddle"):
        k = nearest(analysis, "call", float(center))["strike"] if center else at_the_money(analysis)
        why.append(f"Call and put at {k:g}, the strike nearest the {'price you gave' if center else 'forward price'}")
        return [k, k], why
    if key in ("long_strangle", "short_strangle"):
        p, c = by_delta(analysis, "put", delta), by_delta(analysis, "call", delta)
        if p["strike"] >= c["strike"]:
            raise StrategyError("The put and call strikes meet at this delta; choose a smaller delta")
        why.append(f"Put {p['strike']:g} ({d(p)}) and call {c['strike']:g} ({d(c)}), nearest {delta:.2f}")
        return [p["strike"], c["strike"]], why
    if key == "iron_condor":
        sp, sc = by_delta(analysis, "put", delta), by_delta(analysis, "call", delta)
        if sp["strike"] >= sc["strike"]:
            raise StrategyError("The sold put and call meet at this delta; choose a smaller delta")
        lp = beyond(analysis, "put", sp["strike"], width, -1)
        lc = beyond(analysis, "call", sc["strike"], width, 1)
        why.append(f"Sold put {sp['strike']:g} ({d(sp)}) and sold call {sc['strike']:g} ({d(sc)}), nearest {delta:.2f}")
        why.append(f"Wings {lp['strike']:g} and {lc['strike']:g}, about ${width:.2f} beyond")
        return [lp["strike"], sp["strike"], sc["strike"], lc["strike"]], why
    if key == "iron_butterfly":
        k = nearest(analysis, "call", float(center))["strike"] if center else at_the_money(analysis)
        lp = beyond(analysis, "put", k, width, -1)
        lc = beyond(analysis, "call", k, width, 1)
        why.append(f"Put and call sold at {k:g}; wings {lp['strike']:g} and {lc['strike']:g}, about ${width:.2f} away")
        return [lp["strike"], k, k, lc["strike"]], why
    if key in ("call_butterfly", "put_butterfly"):
        kind = t.roles[0][0]
        k = nearest(analysis, kind, float(center))["strike"] if center else at_the_money(analysis)
        lo = beyond(analysis, kind, k, width, -1)["strike"]
        gap = k - lo
        hi = beyond(analysis, kind, k, gap, 1)["strike"]
        if abs((hi - k) - gap) > 1e-9:
            why.append(f"Wings uneven ({lo:g} / {k:g} / {hi:g}): no strike {gap:g} above the middle is quoted")
        why.append(f"Middle strike {k:g} ({'the price you gave' if center else 'nearest the forward'}), "
                   f"wings {lo:g} and {hi:g}")
        return [lo, k, hi], why
    if t.calendar:
        kind = t.roles[0][0]
        k = nearest(analysis, kind, float(center))["strike"] if center else at_the_money(analysis)
        why.append(f"Both legs at {k:g}, the strike nearest the {'price you gave' if center else 'forward price'}")
        return [k, k], why
    raise StrategyError(f"Unknown strategy {t.key}")


def snap(analysis: Any, kind: str, strike: float) -> tuple[float, str | None]:
    """A strike you named, or the nearest quoted one with a note."""
    c = nearest(analysis, kind, strike)
    if abs(c["strike"] - strike) < 1e-9:
        return strike, None
    return c["strike"], f"{strike:g} {kind} is not quoted; used {c['strike']:g}"


def check_order(t: Template, strikes: Sequence[float]) -> None:
    """Strikes must sit in the template's order: verticals and wings outside, the sold strikes inside."""
    n = len(t.roles)
    if len(strikes) != n:
        raise StrategyError(f"{t.label} takes {n} strike{'s' if n > 1 else ''} (in order: "
                            f"{', '.join(r[3] for r in t.roles)})")
    if t.key in ("long_straddle", "short_straddle") or t.calendar:
        if strikes[0] != strikes[1]:
            raise StrategyError(f"Both legs of a {t.label.lower()} use the same strike")
        return
    if t.key == "iron_butterfly":
        ok = strikes[0] < strikes[1] == strikes[2] < strikes[3]
    elif t.key in ("call_butterfly", "put_butterfly"):
        ok = strikes[0] < strikes[1] < strikes[2]
    else:
        ok = all(a < b for a, b in zip(strikes, strikes[1:]))
    if not ok:
        raise StrategyError(f"The strikes are out of order for a {t.label.lower()}: "
                            f"{', '.join(r[3] for r in t.roles)}, lowest first")


def adjust(t: Template, near: Any, far: Any | None, strikes: list[float], op: dict[str, Any]) -> list[float]:
    """Move strikes one listed strike: the whole trade up or down, the wings wider or narrower, or one leg."""
    kind_of = [r[0] for r in t.roles]
    chain_of = [far if (t.calendar and i == 1) else near for i in range(len(strikes))]
    what = str(op.get("op") or "").lower()
    out = list(strikes)
    if what in ("up", "down"):
        n = 1 if what == "up" else -1
        out = [step(chain_of[i], kind_of[i], k, n) for i, k in enumerate(strikes)]
        if t.calendar or t.key in ("long_straddle", "short_straddle"):
            out = [out[0]] * len(out)
        elif t.key == "iron_butterfly":
            out[2] = out[1]
        elif t.key in ("call_butterfly", "put_butterfly"):
            out[0], out[2] = even_wings(near, kind_of[0], out[1], strikes[1] - strikes[0])
    elif what in ("wider", "narrower"):
        if not t.wings:
            raise StrategyError(f"A {t.label.lower()} has no wings to move")
        for i, direction in zip(t.wings, t.wing_dirs):
            out[i] = step(chain_of[i], kind_of[i], strikes[i], direction if what == "wider" else -direction)
        if t.key in ("call_butterfly", "put_butterfly"):
            gap = out[1] - out[0]
            out[2] = beyond(near, kind_of[2], out[1], gap, 1)["strike"]
    elif what == "leg":
        i, by = int(op.get("leg", -1)), int(op.get("by") or 0)
        if not 0 <= i < len(strikes) or by not in (-1, 1):
            raise StrategyError("Give a leg (0 for the first) and by: 1 or -1")
        out[i] = step(chain_of[i], kind_of[i], strikes[i], by)
        if t.calendar or t.key in ("long_straddle", "short_straddle"):
            out = [out[i]] * len(out)
        elif t.key == "iron_butterfly" and i in (1, 2):
            out[1] = out[2] = out[i]
    else:
        raise StrategyError("op must be up, down, wider, narrower or leg")
    try:
        check_order(t, out)
    except StrategyError:
        if what == "narrower":
            raise StrategyError("It cannot be narrower: the wings are already next to the strikes inside them") from None
        raise StrategyError("That move would put the strikes out of order") from None
    return out


def legs_for(t: Template, near: Any, far: Any | None, strikes: Sequence[float], contracts: int) -> list[dict[str, Any]]:
    out = []
    for i, ((kind, side, qty, role), k) in enumerate(zip(t.roles, strikes)):
        a = far if (t.calendar and i == 1) else near
        c = a.find_by_terms(kind, k)
        if c is None or not two_sided(c):
            raise StrategyError(f"The {a.expiration} {k:g} {kind} has no two-sided quote")
        out.append({"role": role, "option_type": kind, "side": side, "strike": k, "expiration": a.expiration,
                    "contracts": qty * contracts, "bid": c["bid"], "ask": c["ask"], "mid": c.get("mid"),
                    "delta": _delta(c), "iv": c.get("iv"), "spread_pct": c.get("spread_pct"),
                    "option_symbol": c.get("symbol")})
    return out


def prices(legs: Sequence[dict[str, Any]]) -> dict[str, Any]:
    """Net cost in dollars at the midpoints and at the natural price (paying the ask, selling at the bid).
    Positive is paid, negative received."""
    mid = sum((1 if l["side"] == "buy" else -1) * l["contracts"] * 100 * l["mid"] for l in legs)
    natural = sum((l["ask"] if l["side"] == "buy" else -l["bid"]) * l["contracts"] * 100 for l in legs)
    return {"net_mid": mid, "net_natural": natural, "credit": mid < 0,
            "slippage_to_natural": natural - mid}


def risk_check(max_loss: float | None, unlimited: bool, limit: float, contracts: int) -> dict[str, Any]:
    """The most the trade can lose against the per-trade limit (account size x the per-trade percentage)."""
    if unlimited:
        return {"status": "fail", "limit": limit, "max_loss": None,
                "text": "The loss is not limited (options sold without a matching option bought). This is far "
                        "beyond any per-trade limit; most brokers also require margin approval for it."}
    if max_loss is None:
        return {"status": "unknown", "limit": limit, "max_loss": None,
                "text": "The most it can lose could not be worked out exactly (legs expire on different days); "
                        "see the simulation."}
    loss = abs(min(max_loss, 0.0))
    per_unit = loss / max(contracts, 1)
    fits = int(limit // per_unit) if per_unit > 0 else None
    if loss <= limit:
        return {"status": "pass", "limit": limit, "max_loss": loss, "units_within_limit": fits,
                "text": f"The most it can lose, ${loss:,.0f}, is within the per-trade limit of ${limit:,.0f}."}
    return {"status": "fail", "limit": limit, "max_loss": loss, "units_within_limit": fits,
            "text": f"The most it can lose, ${loss:,.0f}, is over the per-trade limit of ${limit:,.0f}"
                    + (f"; {fits} at a time would fit." if fits else "; even one is over it.")}


class Strategies:
    def __init__(self, svc: Any):
        self.svc = svc

    def templates(self) -> list[dict[str, Any]]:
        return [{"key": t.key, "label": t.label, "group": t.group, "description": t.description,
                 "legs": [{"option_type": r[0], "side": r[1], "ratio": r[2], "role": r[3]} for r in t.roles],
                 "defined_risk": t.defined_risk, "defaults": t.defaults, "has_width": t.key in WIDTH_TEMPLATES,
                 "has_wings": bool(t.wings),
                 "uses_delta": "delta" in t.defaults, "calendar": t.calendar} for t in TEMPLATES.values()]

    def build(self, req: dict[str, Any]) -> dict[str, Any]:
        key = str(req.get("strategy") or "").lower()
        t = TEMPLATES.get(key)
        if t is None:
            raise StrategyError(f"Unknown strategy {key or '(none)'}; choose one of {', '.join(TEMPLATES)}")
        symbol = self.svc._symbol(req.get("symbol") or "")
        contracts = int(req.get("contracts") or 1)
        if not 1 <= contracts <= 100:
            raise StrategyError("contracts must be between 1 and 100")
        today = datetime.now(NY).date()
        dates = self.svc.market.expirations(symbol)
        if not dates:
            raise StrategyError(f"No listed options found for {symbol}")
        exp = req.get("expiration")
        if exp and exp not in dates:
            raise StrategyError(f"{exp} is not a listed {symbol} expiration")
        notes: list[str] = []
        if not exp and req.get("after_earnings") and not req.get("dte"):
            when = None
            try:
                e = self.svc._earnings(symbol, None)
                when = e.get("date") if e else None
            except Exception:              # noqa: BLE001 - an estimate that cannot be made is just missing
                when = None
            later = sorted(d for d in dates if when and d > when)
            if later:
                exp = later[0]
                notes.append(f"The first expiration after the next earnings report ({when}"
                             f"{', estimated' if e.get('estimated') else ''}).")
            else:
                notes.append("No earnings date is known, so the usual expiration was used.")
        exp = exp or pick_expiration(dates, today, int(req.get("dte") or t.defaults.get("dte", 45)))
        near = self.svc._chain_for(symbol, exp)
        far = None
        if t.calendar:
            far_exp = req.get("far_expiration")
            if far_exp and (far_exp not in dates or far_exp <= exp):
                raise StrategyError("far_expiration must be a listed expiration after the near one")
            far_exp = far_exp or pick_expiration(dates, today, (date.fromisoformat(exp) - today).days
                                                 + int(req.get("far_days") or t.defaults.get("far_days", 30)),
                                                 after=exp, min_gap=7)
            far = self.svc._chain_for(symbol, far_exp)
        center = req.get("center")
        if center is not None:
            if t.key not in CENTER_TEMPLATES:
                notes.append(f"The middle strike {float(center):g} was not used: a {t.label.lower()} picks its strikes by delta.")
                req = {k: v for k, v in req.items() if k != "center"}
            elif not 0.5 * near.spot <= float(center) <= 1.5 * near.spot:
                raise StrategyError(f"A strike near {float(center):g} is far from the stock price (${near.spot:,.2f}); "
                                    "a number such as a delta or a day count may have been read as a price")
        given = req.get("strikes")
        if given:
            given = [float(k) for k in given]
            check_order(t, given)
            far_off = [k for k in given if not 0.5 * near.spot <= k <= 1.5 * near.spot]
            if far_off:
                raise StrategyError(f"Strike{'s' if len(far_off) > 1 else ''} {', '.join(f'{k:g}' for k in far_off)} "
                                    f"far from the stock price (${near.spot:,.2f}); a date written as 11/20 reads as "
                                    "strikes, so write dates as Nov 20")
            snapped = []
            for i, ((kind, *_), k) in enumerate(zip(t.roles, given)):
                k2, note = snap(far if (t.calendar and i == 1) else near, kind, k)
                snapped.append(k2)
                if note:
                    notes.append(note)
            check_order(t, snapped)
            strikes, why = snapped, ["Strikes as given" + (" (some moved to the nearest quoted)" if notes else "")]
        else:
            strikes, why = choose_strikes(t, near, req)
            check_order(t, strikes)
        if req.get("adjust"):
            strikes = adjust(t, near, far, strikes, req["adjust"])
            why = [f"Strikes moved ({req['adjust'].get('op')})"]
        if t.key in ("call_butterfly", "put_butterfly") and abs((strikes[1] - strikes[0]) - (strikes[2] - strikes[1])) > 1e-9:
            notes.append(f"The wings are uneven ({strikes[0]:g} / {strikes[1]:g} / {strikes[2]:g}): the listed strikes are not "
                         "evenly spaced here, so the loss is not the same on both sides.")
        legs = legs_for(t, near, far, strikes, contracts)
        cost = prices(legs)
        sim = self.svc.simulate(symbol=symbol, legs=[{"expiration": l["expiration"], "option_type": l["option_type"],
                                                       "strike": l["strike"], "side": l["side"],
                                                       "contracts": l["contracts"], "entry_price": l["mid"]}
                                                      for l in legs])
        prof = sim["expiry"]
        s = self.svc.settings
        limit = s.account_size * s.max_risk_per_trade_pct / 100
        risk = risk_check(prof["max_loss"], prof["unlimited_loss"], limit, contracts)
        if t.calendar:
            notes.append("For a calendar paid as a debit, the most it can lose is close to the debit, if the "
                         "later option is not worth more than the near one at the near expiration; early "
                         "assignment of the sold option is not modelled.")
        wide = [l for l in legs if l["spread_pct"] is not None and l["spread_pct"] > s.max_spread_pct]
        if wide:
            notes.append("Wide quotes on " + ", ".join(f"{l['strike']:g} {l['option_type']}" for l in wide)
                         + f" (over {s.max_spread_pct:g}% of the midpoint): the natural price is well off the midpoint.")
        if any(l["side"] == "sell" for l in legs):
            notes.append("Includes options sold: this is modelled only. It can be simulated but not recorded on the "
                         "Positions screen, and the app never places orders.")
        if self.svc.market.info.mode == "demo":
            notes.insert(0, "Demo data: these prices are made up.")
        return {"symbol": symbol, "strategy": t.key, "label": t.label, "group": t.group, "description": t.description,
                "defined_risk": t.defined_risk, "expiration": exp, "far_expiration": far.expiration if far else None,
                "dte": (date.fromisoformat(exp) - today).days, "spot": near.spot, "contracts": contracts,
                "strikes": strikes, "legs": legs, "choices": why, **cost,
                "max_gain": prof["max_gain"], "max_loss": prof["max_loss"], "unlimited_gain": prof["unlimited_gain"],
                "unlimited_loss": prof["unlimited_loss"], "breakevens": prof["breakevens"], "exact": prof["exact"],
                "p_profit_at_expiry": sim["odds_at_expiry"]["p_profit"], "greeks": sim["greeks"],
                "risk_check": risk, "notes": notes, "data_source": self.svc.market.info.mode,
                "request": {k: v for k, v in {**req, "strikes": strikes, "expiration": exp,
                                              "far_expiration": far.expiration if far else None}.items()
                            if k != "adjust"}}

    def parse_and_build(self, text: str, symbol: str | None = None,
                        fallback: Callable[[str], dict[str, Any] | None] | None = None) -> dict[str, Any]:
        from ..scanner import LARGE_CAPS
        known = set(self.svc.settings.watchlist) | KNOWN_TICKERS | set(LARGE_CAPS)
        parsed = parse(text, known, default_symbol=symbol)
        if parsed["spec"].get("strategy") is None and fallback is not None:
            ai = fallback(text)
            if ai and ai.get("strategy") in TEMPLATES:
                parsed["spec"] = {**{k: v for k, v in ai.items() if v is not None}, **{
                    k: v for k, v in parsed["spec"].items() if v is not None}}
                if parsed["spec"].get("strikes"):
                    fitted = _strikes_for(parsed["spec"]["strategy"], parsed["spec"]["strikes"])
                    if fitted is None:
                        parsed["spec"].pop("strikes")
                        parsed["assumed"].append("strikes chosen by the template: the ones read do not fit it")
                    else:
                        parsed["spec"]["strikes"] = fitted
                parsed["understood"].append("The strategy was read by the AI model, then checked against the chain")
                parsed["source"] = "ai"
        if parsed["spec"].get("strategy") is None:
            raise StrategyError("Could not tell which strategy that is. Name one, for example \"AAPL iron condor "
                                "45 days 16 delta\" or \"bull put spread on KO, 30 days\".")
        if not parsed["spec"].get("symbol"):
            raise StrategyError("Could not tell which stock that is. Include its ticker, for example AAPL.")
        result = self.build(parsed["spec"])
        return {"parsed": parsed, "result": result}


# --- reading a description --------------------------------------------------------------------

WIDTH_TEMPLATES = {"bull_call_spread", "bear_put_spread", "bull_put_spread", "bear_call_spread", "iron_condor",
                   "iron_butterfly", "call_butterfly", "put_butterfly"}
KNOWN_TICKERS = {"SPY", "QQQ", "IWM", "DIA", "XLK", "XLF", "XLE", "XLV", "XLY", "XLP", "XLI", "XLU", "XLB", "XLC",
                 "XLRE", "GLD", "SLV", "TLT", "SMH", "ARKK"}
_STOP = {"A", "I", "IV", "ATM", "OTM", "ITM", "DTE", "IC", "CALL", "PUT", "CALLS", "PUTS", "BUY", "SELL", "THE", "AND",
         "FOR", "ON", "IN", "OF", "TO", "AT", "MY", "ME", "AN", "OR", "BY", "WITH", "LONG", "SHORT", "IRON", "FLY",
         "SPREAD", "WIDE", "DAYS", "DAY", "WEEK", "WEEKS", "MONTH", "MONTHS", "DELTA", "NEXT", "THIS", "EXP", "ETF",
         "EOD", "OK", "AM", "PM", "US", "USA", "AI", "PNL", "P", "C", "X", "UP", "DOWN"}
_COMMON = {"low", "now", "cost", "met", "dis", "lin", "all", "key", "big", "fun", "run", "see", "win", "for", "are",
           "has", "one", "two", "c", "f", "t", "v", "ma", "so", "de", "go", "on", "it", "be", "can", "do", "pep"}
MONTHS = {m: i for i, m in enumerate(["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"], 1)}

# "short-term" and "long-term" are not sides of a trade.
SELL_WORDS = re.compile(r"\b(?:sell|selling|sold|write|writing|credit|short(?![-\s]term))\b")
BUY_WORDS = re.compile(r"\b(?:buy|buying|bought|debit|long(?![-\s]term))\b")
CENTER_TEMPLATES = {"long_call", "long_put", "bull_call_spread", "bear_put_spread", "long_straddle", "short_straddle",
                    "iron_butterfly", "call_butterfly", "put_butterfly", "call_calendar", "put_calendar"}


def read_strategy_words(low: str) -> tuple[str | None, str | None]:
    """(template, None), (None, a reason it cannot be built), or (None, None) when no structure is named.

    The structure (spread, straddle, condor...) is read first, then whether it is sold or bought from
    words anywhere in the description, so "sell AAPL put spread" is a credit spread, not the debit one."""
    sell, buy = bool(SELL_WORDS.search(low)), bool(BUY_WORDS.search(low))
    if re.search(r"\b(?:reverse|long)\s+iron\b", low):
        return None, "A reverse (bought) iron condor or butterfly is not one of the templates; a long straddle or strangle also gains on a big move"
    if re.search(r"\biron\s*(?:butterfly|fly)\b", low):
        return "iron_butterfly", None
    if re.search(r"\biron\s*condor\b|\bcondor\b", low):
        return "iron_condor", None
    if re.search(r"\b(?:butterfly|fly)\b", low):
        if re.search(r"\b(?:short|sell(?:ing)?|reverse)\s+(?:an?\s+|the\s+)?(?:\w+\s+)?(?:butterfly|fly)\b", low):
            return None, "A sold butterfly is not one of the templates"
        return ("put_butterfly" if re.search(r"\bputs?\b", low) else "call_butterfly"), None
    if re.search(r"\bcalendar\b|\btime\s+spread\b|\bhorizontal\s+spread\b", low):
        if re.search(r"\b(?:short|reverse)\s+(?:an?\s+|the\s+)?(?:\w+\s+)?calendar\b", low):
            return None, "A sold (reverse) calendar is not one of the templates"
        return ("put_calendar" if re.search(r"\bputs?\b", low) else "call_calendar"), None
    for word in ("straddle", "strangle"):
        if re.search(rf"\b{word}\b", low):
            if sell and buy:
                return None, f"Say whether the {word} is bought or sold"
            return (f"short_{word}" if sell else f"long_{word}"), None
    named = [(r"\bbull(?:ish)?\s+put\b|\bput\s+credit\b|\bcredit\s+put\b", "bull_put_spread"),
             (r"\bbear(?:ish)?\s+call\b|\bcall\s+credit\b|\bcredit\s+call\b", "bear_call_spread"),
             (r"\bbull(?:ish)?\s+call\b|\bcall\s+debit\b|\bdebit\s+call\b", "bull_call_spread"),
             (r"\bbear(?:ish)?\s+put\b|\bput\s+debit\b|\bdebit\s+put\b", "bear_put_spread")]
    for pattern, key in named:
        if re.search(pattern, low):
            return key, None
    for kind, sold, bought in (("put", "bull_put_spread", "bear_put_spread"), ("call", "bear_call_spread", "bull_call_spread")):
        if re.search(rf"\b{kind}\s+(?:vertical|spread)s?\b", low):
            if sell and buy:
                return None, f"Say whether the {kind} spread is bought (a debit) or sold (a credit)"
            return (sold if sell else bought), None
    if re.search(r"\b(?:calls?|puts?)\b", low) and (sell or buy):
        if sell and buy:
            return None, "That reads as more than one leg: name the strategy, for example \"bull call spread 190/200\""
        if sell:
            return None, ("Selling a single call or put is not one of the templates; a credit spread (bull put or bear "
                          "call) limits the loss")
        return ("long_put" if re.search(r"\bputs?\b", low) and not re.search(r"\bcalls?\b", low) else "long_call"), None
    return None, None


# Without a named strategy, a view picks the usual defined-risk structure for it.
VIEWS: list[tuple[str, str, str]] = [
    (r"\b(?:big\s+move|either\s+way|volatil\w*|earnings\s+(?:move|play)|breakout)\b", "long_straddle",
     "a big move either way: long straddle"),
    (r"\b(?:neutral|sideways|range|range-bound|income|stay\s+(?:between|flat|put))\b", "iron_condor",
     "a range: iron condor"),
    (r"\b(?:bullish|go(?:es)?\s+up|rise|rally|upside)\b.*\bspread\b|\bspread\b.*\b(?:bullish|up|rise)\b",
     "bull_call_spread", "a rise, with a spread: bull call spread"),
    (r"\b(?:bearish|go(?:es)?\s+down|fall|drop|downside)\b.*\bspread\b|\bspread\b.*\b(?:bearish|down|fall)\b",
     "bear_put_spread", "a fall, with a spread: bear put spread"),
    (r"\b(?:bullish|go(?:es)?\s+up|rise|rally|upside)\b", "long_call", "a rise: long call"),
    (r"\b(?:bearish|go(?:es)?\s+down|fall|drop|downside|protect\w*|hedge)\b", "long_put", "a fall: long put"),
    (r"\bcalls?\b", "long_call", "a call: long call"),
    (r"\bputs?\b", "long_put", "a put: long put"),
]


def _slash_date(text: str, today: date) -> date | None:
    """A month/day (or month/day/year) that is today or later, or None."""
    nums = re.findall(r"\d+(?:\.\d+)?", text)
    if len(nums) not in (2, 3) or any("." in x for x in nums):
        return None
    m, d = int(nums[0]), int(nums[1])
    if len(nums) == 3:
        y = int(nums[2])
        y = y + 2000 if len(nums[2]) == 2 else y if len(nums[2]) == 4 else -1
    else:
        y = today.year + (1 if (m, d) < (today.month, today.day) else 0)
    try:
        when = date(y, m, d)
    except ValueError:
        return None
    return when if today <= when <= today + timedelta(days=1100) else None


def _third_friday(year: int, month: int) -> date:
    first = date(year, month, 1)
    return first + timedelta(days=(4 - first.weekday()) % 7 + 14)


def _expiry_days(text: str, today: date) -> tuple[int | None, str | None, str | None]:
    """Days to the expiration described, or an exact date. Returns (days, iso_date, words understood)."""
    t = text.lower()
    m = re.search(r"\b(20\d\d)-(\d\d)-(\d\d)\b", t)
    if m:
        try:
            d = date(int(m[1]), int(m[2]), int(m[3]))
            return (d - today).days, d.isoformat(), f"expiration {d.isoformat()}"
        except ValueError:
            pass
    m = re.search(r"\b(\d{1,3})\s*-?\s*(?:dte|days?|d)\b", t)
    if m:
        return int(m[1]), None, f"about {int(m[1])} days out"
    m = re.search(r"\b(\d{1,2})\s*-?\s*weeks?\b", t)
    if m:
        return 7 * int(m[1]), None, f"about {m[1]} weeks out"
    m = re.search(r"\b(\d{1,2})\s*-?\s*months?\b", t)
    if m:
        return 30 * int(m[1]), None, f"about {m[1]} months out"
    if re.search(r"\bnext\s+month\b", t):
        y, mo = (today.year + 1, 1) if today.month == 12 else (today.year, today.month + 1)
        third = _third_friday(y, mo)
        return (third - today).days, None, f"next month's monthly ({third.isoformat()})"
    # The coming Friday: later this week on a weekday, the end of the coming week at a weekend.
    coming = (4 - today.weekday()) % 7 or 7
    if re.search(r"\bnext\s+week\b", t):
        days = coming + 7 if today.weekday() <= 4 else coming
        return days, None, f"next week's Friday ({(today + timedelta(days=days)).isoformat()})"
    if re.search(r"\b(?:this|next|coming)\s+friday\b|\bthis\s+week\b|\bweeklies?\b|\bweekly\b", t):
        return coming, None, f"the coming Friday ({(today + timedelta(days=coming)).isoformat()})"
    m = re.search(r"\b(january|february|march|april|june|july|august|september|october|november|december|jan|feb|mar|"
                  r"apr|jun|jul|aug|sept|sep|oct|nov|dec)\b\.?(?:\s+(\d{1,2})(?!\d))?|\b(may)\s+(\d{1,2})(?!\d)", t)
    if m:
        name, day = (m[1], m[2]) if m[1] else (m[3], m[4])
        month, day = MONTHS[name[:3]], int(day) if day else None
        year = today.year + (1 if month < today.month or (month == today.month and day and day < today.day) else 0)
        if day:
            try:
                d = date(year, month, day)
                return (d - today).days, None, f"near {d.isoformat()}"
            except ValueError:
                pass
        # A month alone means its standard expiration, the third Friday; once that has passed, next year's.
        third = _third_friday(year, month)
        if third <= today:
            third = _third_friday(year + 1, month)
        return (third - today).days, None, f"the {name[:3].capitalize()} monthly ({third.isoformat()})"
    return None, None, None


def parse(text: str, known: set[str], default_symbol: str | None = None, today: date | None = None) -> dict[str, Any]:
    """Read a trade description into a strategy template and its settings."""
    today = today or datetime.now(NY).date()
    raw = " ".join(str(text or "").split())
    if not raw:
        raise StrategyError("Describe the trade, for example \"AAPL iron condor 45 days 16 delta\"")
    if len(raw) > 500:
        raise StrategyError("Keep the description under 500 characters")
    low = raw.lower()
    spec: dict[str, Any] = {"symbol": None, "strategy": None}
    understood: list[str] = []
    assumed: list[str] = []

    # ticker: $XYZ, a known ticker in any case, or a capitalised token that is not an ordinary word
    sym = None
    m = re.search(r"\$([A-Za-z]{1,5}(?:\.[A-Za-z])?)\b", raw)
    if m:
        sym = m[1].upper()
    caps = [tok for tok in re.findall(r"\b[A-Z]{1,5}(?:\.[A-Z])?\b", raw) if tok not in _STOP]
    if not sym:
        sym = next((tok for tok in caps if tok in known), None) or next((tok for tok in caps if len(tok) >= 2), None)
    if not sym:     # written in lower case: only a known ticker that is not also an everyday word
        sym = next((tok.upper() for tok in re.findall(r"\b[a-z]{3,5}\b", raw)
                    if tok.upper() in known and tok not in _COMMON), None)
    if sym:
        spec["symbol"] = sym
        understood.append(f"stock {sym}")
    elif default_symbol:
        spec["symbol"] = default_symbol.upper()
        assumed.append(f"stock {default_symbol.upper()} (the one on screen)")

    key, refused = read_strategy_words(low)
    if refused:
        raise StrategyError(refused + ".")
    if key:
        spec["strategy"] = key
        understood.append(TEMPLATES[key].label.lower())
    if spec["strategy"] is None:
        for pattern, key, words in VIEWS:
            if re.search(pattern, low):
                spec["strategy"] = key
                assumed.append(f"strategy for {words}")
                break

    groups = list(re.finditer(r"\b(\d+(?:\.\d+)?)(?:\s*/\s*(\d+(?:\.\d+)?)){1,3}\b", raw))
    date_group, slash_date = None, None
    if len(groups) > 1:            # with strikes elsewhere, a month/day pair still ahead is the expiration
        dated = [(g, d) for g in groups if (d := _slash_date(g[0], today)) is not None]
        preferred = [(g, d) for g, d in dated
                     if re.search(r"\b(?:exp\w*|for|on|by|until|in)\s*$", raw[:g.start()].lower())]
        if preferred or dated:
            date_group, slash_date = (preferred or dated)[-1]
    days, iso, words = _expiry_days(raw, today)
    if slash_date is not None:
        days, iso, words = (slash_date - today).days, slash_date.isoformat(), f"expiration near {slash_date.isoformat()}"
    if re.search(r"\bearnings\b", low):
        spec["after_earnings"] = True
        if days is None:
            understood.append("the first expiration after earnings")
    if iso:
        spec["expiration_near"] = iso
    if days is not None:
        spec["dte"] = max(days, 1)
        understood.append(words)

    m = re.search(r"\b(\d{1,2}(?:\.\d+)?)\s*-?\s*(?:delta\b|Δ|δ)", low)
    if m:
        spec["delta"] = float(m[1]) / (100 if float(m[1]) >= 1 else 1)
        understood.append(f"{spec['delta']:.2f} delta")
    m = re.search(r"\$?(\d+(?:\.\d+)?)\s*(?:-|\s)?(?:points?\s+|dollars?\s+)?wide\b|\bwidth\s+(?:of\s+)?\$?(\d+(?:\.\d+)?)", low)
    if m:
        spec["width"] = float(m[1] or m[2])
        understood.append(f"${spec['width']:g} wide")
    # Not the second strike of "25/30 spread".
    m = re.search(r"(?<![/\d.])\b(\d{1,3})\b(?!\s*/)\s*(?:x\b|contracts?\b|lots?\b|spreads?\b|condors?\b|of\s+them\b)", low)
    if m and int(m[1]) > 0:
        spec["contracts"] = int(m[1])
        understood.append(f"{int(m[1])} at a time")
    if "contracts" not in spec:
        m = re.search(r"\b(?:buy|sell|long|short)\s+(\d{1,2})\b(?!\s*(?:-|/|\.\d|delta|δ|dte|days?|d\b|weeks?|months?|wide|"
                      r"points?|dollars?|%))", low)
        if m and 1 <= int(m[1]) <= 50:
            spec["contracts"] = int(m[1])
            understood.append(f"{int(m[1])} at a time")
    # strikes written as 190/200 or 180/190/210/220
    m = next((g for g in groups if g is not date_group), None)
    if m:
        ks = [float(x) for x in re.findall(r"\d+(?:\.\d+)?", m[0])]
        if all(k > 0 for k in ks):
            spec["strikes"] = ks
            understood.append("strikes " + "/".join(f"{k:g}" for k in ks))
    units = r"(?!\s*-?\s*(?:delta\b|δ|dte\b|days?\b|d\b|weeks?\b|months?\b|wide\b|points?\b|dollars?\b|%|/))"
    center = None
    if "strikes" not in spec:
        m = re.search(r"\b(?:at|around|near|pin(?:ned)?\s+at|target|cent(?:er|re)d?\s+(?:at|on)|strike)\s+\$?(\d+(?:\.\d+)?)\b"
                      + units, low)
        if m:
            center = float(m[1])
        else:      # a lone strike written before the type: "230 calls", "195p"
            m = re.search(r"(?<![/\d.$])(?<!buy )(?<!sell )\b(\d{2,5}(?:\.\d+)?)\s*(?:calls?|puts?|c|p)\b", low)
            if m:
                center = float(m[1])
    strategy = spec.get("strategy")
    if center is not None:
        if strategy is None or strategy in CENTER_TEMPLATES:
            spec["center"] = center
            understood.append(f"strike near {center:g}")
        else:
            assumed.append(f"the price {center:g} was not used: a {TEMPLATES[strategy].label.lower()} picks its strikes by "
                           "delta (write strikes such as 55/60 to choose them)")
    if strategy and "strikes" in spec:
        spec["strikes"] = _strikes_for(strategy, spec["strikes"])
        if spec["strikes"] is None:
            del spec["strikes"]
            assumed.append("strikes chosen by the template: the numbers given do not fit it")
    if strategy:
        t = TEMPLATES[strategy]
        if "dte" not in spec and not spec.get("after_earnings"):
            assumed.append(f"about {t.defaults.get('dte', 45)} days out")
        if "delta" in t.defaults and "delta" not in spec and "strikes" not in spec and \
                ("center" not in spec or t.key not in CENTER_TEMPLATES):
            assumed.append(f"{t.defaults['delta']:.2f} delta")
        if t.key in WIDTH_TEMPLATES and "width" not in spec and "strikes" not in spec:
            assumed.append("wings about 2.5% of the stock price apart")
        if "contracts" not in spec:
            assumed.append("1 at a time")
    if "expiration_near" in spec:
        spec["dte"] = max((date.fromisoformat(spec.pop("expiration_near")) - today).days, 1)
    return {"text": raw, "spec": {k: v for k, v in spec.items() if v is not None}, "understood": understood,
            "assumed": assumed, "source": "rules"}


def _strikes_for(strategy: str, ks: list[float]) -> list[float] | None:
    """Strikes written in a description, put in the template's order; None when they do not fit."""
    t = TEMPLATES[strategy]
    n = len(t.roles)
    s = sorted(ks)
    if strategy in ("long_straddle", "short_straddle", "call_calendar", "put_calendar", "long_call", "long_put"):
        return [s[0]] * n if len(set(s)) == 1 or len(s) == 1 else None
    if strategy == "iron_butterfly":
        if len(s) == 3:
            return [s[0], s[1], s[1], s[2]]
        return s if len(s) == 4 and s[1] == s[2] else None
    return s if len(s) == n and len(set(s)) == n else None


__all__ = ["TEMPLATES", "Strategies", "StrategyError", "parse", "prices", "risk_check", "choose_strikes", "adjust",
           "check_order", "pick_expiration", "by_delta", "at_the_money", "beyond", "step"]
