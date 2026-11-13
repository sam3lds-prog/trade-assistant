"""Strategies revalued under a scenario version's cases (4.3, the one-click pipeline; corrected in 4.5.1).

The study picked its candidate strategies before any provider ran (shares, single options, spreads), each with its
exact legs. Once a version's case paths exist, those **same** structures are valued under every case, from **one**
frozen snapshot: one stock quote and one chain per expiration needed, taken once, with each leg's bid, ask, midpoint,
implied volatility, open interest, quote time and how old its Greeks are (Tradier refreshes them hourly, separately
from the quotes).

**The session a structure is valued at (4.5.1).** Each case is a distribution in absolute prices at one session:
lognormal around the case's saved price at that session (the anchor times its cumulative move), with the case's saved
log-return dispersion at that session. A structure is valued at the session it is held to:

- shares and short sales: the target session;
- options: the target session, or the earliest leg's expiration when one expires first. The distribution is then the
  case's saved price and dispersion **at the last forecast session on or before that expiration**, never the target
  session's (4.5.0 valued a contract expiring after two sessions against the five-session distribution). A structure
  with legs of different expirations is closed at the first expiration: the expiring legs at their intrinsic value, the
  rest at their model value on that date. Its legs are not carried on to the target, because the cases give the price
  at each session separately, not the joint path that would need.
- a leg expiring before the first forecast session has no scenario price for its expiration: it **cannot be valued
  reliably**, and says so, instead of being priced against a later session.

Before an earnings release inside the horizon, options valued **at or after** the release's session are valued after
the volatility collapse the release is expected to bring; options valued before it keep today's implied volatility
(4.5.0 applied the collapse whatever the session). A price grid wider than the range the option model was evaluated on
is refused (cannot be valued reliably), never extrapolated.

So a quote that moves after the study changes what the legs cost now, never where the case says the stock ends. Every
structure valued at the same session is on the same price grid within a case, so they can be added up into mixes.

**Data quality (4.5.1).** Each structure carries a tier:

- **execution**: every quote it depends on is real-time, fresh within the policy's quote age while the market is open,
  two-sided and not crossed; every option leg is inside the spread limit and its open interest (when known) is at least
  OPPORTUNITY_MIN_OPEN_INTEREST. Only these can back a Qualified setup.
- **research**: it can be valued, but a quote is stale, delayed, of unknown age, from the last session (market closed),
  one-sided, crossed or too wide, an open interest is too low, or a leg had no midpoint now and the study's own
  midpoint was used. Useful for the modeled analysis, never for a Qualified setup.
- **unavailable**: it cannot be valued reliably; it is listed under `failed` with the reason.

A quote whose time the source did not give is never taken as fresh. Missing or old Greeks do not stop a valuation (the
model volatility comes from the chain's quotes, not from the provider's Greeks) and are flagged on the leg.

For each structure and case: the modeled net P&L after round-trip costs (the quoted spread for options, STOCK_COST_BPS
both ways for shares), the 10th-percentile result, the average loss in the worst tenth (CVaR), and, apart from the
model, the contract's own most it can lose and the capital it ties up. Then a few budgeted mixes (cash; each structure
alone at the budget; and, for each case, the What-If allocator's split under that case) are revalued across all
cases and ranked by their worst-case modeled net P&L. A probability-weighted result would need empirical case weights,
and there are none, so none is shown. Mixes combine only structures valued at the same session.
"""

from __future__ import annotations

import math
from datetime import date, datetime, timezone
from typing import Any

import numpy as np

from .. import contracts as C
from .. import riskmath
from .. import studio
from ..freshness import observe, parse, quote_time, session_state
from ..optionmath import NY
from ..providers.base import DataError
from ..service import RequestError
from ..whatif import cvar10, greedy_allocate, scenario_distribution

VALUATION_VERSION = "valuation-4.5.1"
LONG_KINDS, SHORT_KINDS, EVENT_KINDS = set(studio.LONG_KINDS), set(studio.SHORT_KINDS), set(studio.EVENT_KINDS)
MIX_RISK = 1.0                        # the allocator's weight on the worst tenth
MIX_MAX_SHARE = 0.5
GREEKS_OLD_SECONDS = 2 * 3600         # Tradier refreshes Greeks hourly: older than two hours while open is flagged
OUTSIDE_MODEL_LIMIT = 0.005           # the most probability weight a case's grid may put outside the model's range
TIERS = ("execution", "research", "unavailable")

# Why a structure is research quality and not execution quality (codes shared with the opportunity evaluator).
QUALITY_REASONS = {
    "stale_quote": "a quote is older than the policy's limit",
    "delayed_quotes": "the quotes are delayed (sandbox): not fresh enough to qualify",
    "quote_freshness_unknown": "a quote's time is unknown: it cannot be called fresh",
    "market_closed": "the market is closed: the quotes are the last session's, not executable now",
    "one_sided_quote": "a quote has no bid or no ask",
    "crossed_quote": "a quote is crossed (the ask is below the bid)",
    "no_two_sided_quote": "an option leg has no two-sided quote",
    "wide_spread": "an option leg's spread is wider than OPPORTUNITY_MAX_SPREAD_PCT of its midpoint",
    "low_open_interest": "an option leg's open interest is below OPPORTUNITY_MIN_OPEN_INTEREST",
    "study_midpoint_used": "an option leg had no midpoint now: the study's older midpoint was used",
    "open_interest_unknown": "an option leg's open interest is not reported: its liquidity cannot be checked",
}


def _f(x: Any) -> float | None:
    try:
        v = float(x)
    except (TypeError, ValueError):
        return None
    return v if math.isfinite(v) else None


def direction_of(kind: str) -> str:
    return "LONG" if kind in LONG_KINDS else "SHORT" if kind in SHORT_KINDS else "EVENT"


def limits(svc: Any) -> dict[str, Any]:
    """The budget mixes are sized to and the loss limit a setup must stay inside: SCENARIO_BUDGET and
    SCENARIO_MAX_LOSS, or the Trading Desk policy's trial capital and daily loss stop."""
    s = svc.settings
    pol = None
    desk = getattr(svc, "trading", None)
    if desk is not None:
        try:
            pol = desk.policy()
        except Exception:  # noqa: BLE001 - without a policy the settings' own figures are used
            pol = None
    budget = s.scenario_budget or (float(pol.trial_capital) if pol else 500.0)
    loss = s.scenario_max_loss or (float(pol.max_daily_loss) if pol else 25.0)
    return {"budget": float(budget), "loss_limit": float(loss),
            "budget_basis": "SCENARIO_BUDGET" if s.scenario_budget else "the Trading Desk policy's trial capital",
            "loss_basis": "SCENARIO_MAX_LOSS" if s.scenario_max_loss else "the Trading Desk policy's daily loss stop",
            "max_quote_age_seconds": float(pol.max_quote_age_seconds) if pol else 20.0}


# --- the snapshot ---------------------------------------------------------------------------------------------------

def _session(svc: Any, now: datetime) -> str:
    try:
        return session_state(svc.market.clock(), now)
    except Exception:  # noqa: BLE001 - without a clock, weekday regular hours decide
        return session_state(None, now)


def _observed(svc: Any, kind: str, symbol: str, q: dict[str, Any], taken: datetime, session: str) -> dict[str, Any]:
    info = svc.market.info
    limits_ = getattr(svc.settings, "freshness", None) or {}
    o = observe(kind, symbol, info.label, quote_time(q), retrieved_at=taken, session=session, realtime=bool(info.realtime) or info.mode == "demo",
                limits=limits_)
    return {"freshness": o["freshness"], "age_seconds": o.get("age_seconds"), "observed_at": o.get("observed_at")}


def snapshot(svc: Any, symbol: str, candidates: list[dict[str, Any]]) -> dict[str, Any]:
    """One frozen look at the market for every candidate's legs: the stock quote and one chain per expiration, each
    quote with its own time and freshness (freshness.observe, against the market clock when it was taken)."""
    taken = datetime.now(timezone.utc)
    session = _session(svc, taken)
    out: dict[str, Any] = {"symbol": symbol, "taken_at": C.utc_now(), "session": session, "quote": None, "legs": {}, "chains": {},
                           "problems": [], "mode": svc.market.info.mode,
                           # Demo prices are made up and labelled as examples; they are not called delayed (as before 4.5.1).
                           "realtime": bool(svc.market.info.realtime) or svc.market.info.mode == "demo"}
    try:
        q = svc.market.quote(symbol)
        meta = q.get("_meta") or {}
        fr = meta.get("freshness")
        age = _f(meta.get("age_seconds"))
        if fr is None:                                    # a provider without _meta: judged here, from the quote's own times
            o = _observed(svc, "STOCK_QUOTE", symbol, q, taken, session)
            fr, age = o["freshness"], o["age_seconds"]
        out["quote"] = {"last": _f(q.get("last")) or _f(q.get("close")), "bid": _f(q.get("bid")), "ask": _f(q.get("ask")),
                        "trade_time": q.get("trade_time"), "quote_time": quote_time(q), "age_seconds": age, "freshness": fr,
                        "realtime": bool(meta.get("realtime", svc.market.info.realtime)) or svc.market.info.mode == "demo"}
    except (DataError, RequestError, ValueError) as exc:
        out["problems"].append(f"No quote for {symbol}: {exc}")
    exps = sorted({str(lg["expiration"]) for c in candidates for lg in (c.get("legs") or []) if lg.get("expiration")})
    for exp in exps:
        try:
            analysis = svc._chain_for(symbol, exp)          # the simulator reuses this same chain while it is fresh
        except (DataError, RequestError, ValueError) as exc:
            out["problems"].append(f"No {exp} chain: {exc}")
            continue
        out["chains"][exp] = {"spot": _f(getattr(analysis, "spot", None)), "contracts": len(analysis.contracts)}
        for row in analysis.contracts:
            sym = row.get("symbol")
            if not sym:
                continue
            g = row.get("greeks") or {}
            o = _observed(svc, "OPTION_QUOTE", sym, row, taken, session)
            greeks_at = parse(g.get("updated_at"))
            out["legs"][sym] = {"bid": _f(row.get("bid")), "ask": _f(row.get("ask")), "mid": _f(row.get("mid")),
                                "iv": _f(row.get("model_vol") or row.get("iv")), "spread_pct": _f(row.get("spread_pct")),
                                "open_interest": _f(row.get("open_interest")), "volume": _f(row.get("volume")),
                                "quote_time": quote_time(row), "freshness": o["freshness"], "age_seconds": o["age_seconds"],
                                "greeks_updated_at": g.get("updated_at"),
                                "greeks_age_seconds": (taken - greeks_at).total_seconds() if greeks_at else None,
                                "greeks_source": row.get("greeks_source")}
    return out


# --- data quality ---------------------------------------------------------------------------------------------------

def _freshness_codes(fr: str | None, age: float | None, realtime: bool, max_age: float) -> list[str]:
    """Why a quote is not execution quality (none when it is). Unknown freshness is never fresh."""
    if not realtime or fr == "DELAYED":
        return ["delayed_quotes"]
    if fr == "LAST_SESSION":
        return ["market_closed"]
    if fr == "STALE":
        return ["stale_quote"]
    if fr not in ("FRESH",) or age is None:
        return ["quote_freshness_unknown"]
    if age > max_age:
        return ["stale_quote"]
    return []


def stock_quality(snap: dict[str, Any], lim: dict[str, Any], kind: str) -> list[str]:
    q = snap.get("quote") or {}
    if not q:
        return ["quote_freshness_unknown"]
    codes = _freshness_codes(q.get("freshness"), _f(q.get("age_seconds")), bool(q.get("realtime", snap.get("realtime", True))),
                             float(lim["max_quote_age_seconds"]))
    bid, ask = _f(q.get("bid")), _f(q.get("ask"))
    if bid and ask and ask < bid:
        codes.append("crossed_quote")
    side = ask if kind == "shares" else bid
    if not side or side <= 0:
        codes.append("one_sided_quote")
    return codes


def leg_quality(lg: dict[str, Any], lim: dict[str, Any], realtime: bool, max_spread: float, min_oi: float) -> tuple[list[str], list[str]]:
    """(codes that keep the leg research quality, informational flags)."""
    codes: list[str] = []
    notes: list[str] = []
    bid, ask = _f(lg.get("bid")), _f(lg.get("ask"))
    if bid is not None and ask is not None and ask < bid:
        codes.append("crossed_quote")
    elif not (bid and ask and ask >= bid > 0):
        codes.append("no_two_sided_quote")
    # An option quote is fresh by its own kind's limit (FRESHNESS_OPTION_QUOTE_SECONDS, five minutes by default): bids
    # and asks on a contract change less often than the stock's, so the policy's stock-quote age is not applied to it.
    codes += _freshness_codes(lg.get("freshness"), _f(lg.get("age_seconds")), realtime, float("inf"))
    sp = _f(lg.get("spread_pct"))
    if sp is not None and sp > max_spread:
        codes.append("wide_spread")
    oi = _f(lg.get("open_interest"))
    if oi is None:
        codes.append("open_interest_unknown")                # unknown liquidity is not execution quality (as unknown age)
    elif oi < min_oi:
        codes.append("low_open_interest")
    if lg.get("study_mid_used"):
        codes.append("study_midpoint_used")
    gage = _f(lg.get("greeks_age_seconds"))
    if lg.get("greeks_updated_at") is None:
        notes.append(f"{lg.get('option_symbol')}: no Greeks from the source (the model volatility comes from the quotes)")
    elif gage is not None and gage > GREEKS_OLD_SECONDS:
        notes.append(f"{lg.get('option_symbol')}: the source's Greeks are {gage / 3600:.1f} hours old")
    return list(dict.fromkeys(codes)), notes


def tier_of(codes: list[str]) -> str:
    return "research" if codes else "execution"


# --- the case distributions -------------------------------------------------------------------------------------------

def session_index(version: dict[str, Any], day: date) -> int | None:
    """The 0-based position of the last forecast session on or before `day`, or None when `day` is before the first.
    The stock does not trade between sessions, so its price at an expiration that falls between two sessions is the
    earlier session's close."""
    best = None
    for i, s in enumerate(version.get("sessions") or []):
        if date.fromisoformat(str(s["date"])) <= day:
            best = i
    return best


def case_grid(anchor: float, case: dict[str, Any], i: int) -> tuple[np.ndarray, np.ndarray]:
    """The case's price distribution at session i (0-based): lognormal around its saved price there, with its saved
    log-return dispersion there. Nothing is extrapolated past the saved sessions."""
    rp, sd = case.get("return_pct") or [], case.get("log_sd") or []
    if i >= len(rp) or i >= len(sd):
        raise RequestError(f"the {case.get('key')} case has no saved price at session {i + 1}")
    return scenario_distribution(anchor, float(rp[i]), 100.0 * float(sd[i]))


def _crush_points(svc: Any, version: dict[str, Any]) -> float | None:
    cat = version.get("catalyst") or {}
    cs = version.get("catalyst_session")
    if cat.get("kind") != "earnings" or not cs or cs > int(version["horizon_sessions"]):
        return None
    try:
        from ..options.lab import OptionsLab
        vol = (getattr(svc, "lab", None) or OptionsLab(svc)).volatility(version["symbol"])
        return studio.crush_points(vol, int(version["horizon_sessions"]))
    except Exception:  # noqa: BLE001 - without it options are valued at today's volatility, and it says so
        return None


class CannotValue(RequestError):
    """The structure cannot be valued reliably from what the version and the snapshot hold."""


def value(svc: Any, study: dict[str, Any], version: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    """Every candidate of the study under every case of the version, from one snapshot, and the budgeted mixes."""
    st, view = study["study"], study.get("view") or {}
    sym = st["symbol"]
    cands = [c for c in (view.get("strategies") or {}).get("candidates") or []]
    lim = limits(svc)
    s = svc.settings
    max_spread = float(s.opportunity_max_spread_pct)
    min_oi = float(getattr(s, "opportunity_min_open_interest", 10.0))
    snap = snapshot(svc, sym, cands)
    sessions = version["sessions"]
    target = date.fromisoformat(sessions[-1]["date"])
    last_i = len(sessions) - 1
    anchor = float(version["anchor"]["price"])
    crush = _crush_points(svc, version)
    cat_session = int(version.get("catalyst_session") or 0) if crush else 0
    cost_bps = float(s.stock_cost_bps)
    cases = list(version["cases"])
    grid_cache: dict[tuple[str, int], tuple[np.ndarray, np.ndarray]] = {}

    def grid(key: str, i: int) -> tuple[np.ndarray, np.ndarray]:
        if (key, i) not in grid_cache:
            grid_cache[(key, i)] = case_grid(anchor, next(c for c in cases if c["key"] == key), i)
        return grid_cache[(key, i)]

    structures, failed = [], []
    today = datetime.now(NY).date()
    realtime = bool(snap.get("realtime", True))
    if target < today:
        failed.append({"key": "all", "label": "Every structure", "error": f"the target session {target.isoformat()} has passed",
                       "code": "expired_session"})
        cands = []
    for c in cands:
        kind, qty = c["key"], float(c.get("quantity") or 1)
        flags: list[str] = []
        notes: list[str] = []
        legs: list[dict[str, Any]] = []
        entry = None
        curve = None
        eval_day, eval_i = target, last_i
        crushed = False
        quality: list[str] = []
        try:
            if kind in ("shares", "short"):
                q = snap["quote"] or {}
                side = q.get("ask") if kind == "shares" else q.get("bid")
                entry = side or q.get("last")
                if not entry:
                    raise CannotValue("no stock quote in the snapshot")
                if not side:
                    flags.append("no " + ("ask" if kind == "shares" else "bid") + " quoted: the last trade is used")
                quality = stock_quality(snap, lim, kind)
                sign = 1.0 if kind == "shares" else -1.0
                costs = 2 * cost_bps / 1e4 * qty * entry
                theoretical = riskmath.theoretical_max_loss([riskmath.PayoffLeg(None, sign * qty, entry)])
                capital = riskmath.capital_required(kind, cost=qty * entry, credit=False, quantity=qty, last=entry,
                                                    theoretical=theoretical["value"])["value"]
            else:
                legs, missing = _legs_now(snap, c.get("legs") or [])
                if not legs:
                    raise CannotValue("the study saved no legs for it")
                if missing:
                    raise CannotValue("; ".join(missing))
                exps = sorted({date.fromisoformat(str(lg["expiration"])) for lg in legs})
                exp_min = exps[0]
                if exp_min < today:
                    raise CannotValue(f"a leg expired on {exp_min.isoformat()}")
                eval_day = min(target, exp_min)
                i = session_index(version, eval_day)
                if i is None:
                    raise CannotValue(f"a leg expires {exp_min.isoformat()}, before the first forecast session "
                                      f"({sessions[0]['date']}): the cases have no price for that date")
                eval_i = i
                if exp_min < target:
                    flags.append(f"a leg expires {exp_min.isoformat()}, before the target session: valued at that expiration, against "
                                 f"the cases' prices at session {i + 1} ({sessions[i]['date']})")
                if len(exps) > 1:
                    flags.append(f"its legs expire on different dates: the whole structure is closed at the first expiration "
                                 f"({exp_min.isoformat()}), the later legs at their model value then")
                for lg in legs:
                    if not lg["mid"]:
                        if not lg["study_mid"]:
                            raise CannotValue(f"{lg['option_symbol']}: no midpoint now and none saved by the study")
                        lg["mid"] = lg["study_mid"]
                        lg["study_mid_used"] = True
                        flags.append(f"{lg['option_symbol']}: no midpoint now; the study's ({lg['study_mid']}) is used, so it is "
                                     "research only")
                    if lg.get("spread_pct") is not None and lg["spread_pct"] > max_spread:
                        flags.append(f"{lg['option_symbol']}: the spread is {lg['spread_pct']:.1f}% of the midpoint")
                    codes, lnotes = leg_quality(lg, lim, realtime, max_spread, min_oi)
                    lg["quality"] = {"tier": tier_of(codes), "reasons": codes}
                    quality += codes
                    notes += lnotes
                crushed = bool(crush and cat_session and eval_i + 1 >= cat_session)
                sim_legs = [{"option_type": lg["option_type"], "strike": lg["strike"], "expiration": lg["expiration"], "side": lg["side"],
                             "contracts": int(lg.get("contracts") or 1), "entry_price": lg["mid"]} for lg in legs]
                lo = min(float(grid(k["key"], eval_i)[0].min()) for k in cases)
                hi = max(float(grid(k["key"], eval_i)[0].max()) for k in cases)
                spot = float((snap["chains"].get(str(legs[0]["expiration"])) or {}).get("spot") or anchor)
                rng = min(90.0, max(10.0, 100.0 * max(abs(hi / spot - 1), abs(lo / spot - 1)) + 2.0))
                sim = svc.simulate(symbol=sym, legs=sim_legs, eval_date=eval_day.isoformat(), range_pct=rng,
                                   iv_shift=-abs(crush) if crushed else 0.0)
                curve = (np.array(sim["curve"]["prices"], dtype=float), np.array(sim["curve"]["pnl_at_date"], dtype=float))
                for k in cases:
                    prices, weights = grid(k["key"], eval_i)
                    outside = float(weights[(prices < curve[0].min()) | (prices > curve[0].max())].sum())
                    if outside > OUTSIDE_MODEL_LIMIT:
                        raise CannotValue(f"the {k['key']} case's prices at session {eval_i + 1} reach beyond the range the option "
                                          f"model was evaluated on ({outside:.1%} of the case outside): not extrapolated")
                costs = 0.0
                for lg in legs:
                    width = (lg["ask"] - lg["bid"]) if (lg["bid"] and lg["ask"] and lg["ask"] >= lg["bid"]) else 0.05 * (lg["mid"] or 0.0)
                    costs += float(width) * float(lg.get("contracts") or 1) * 100.0
                net = sum((1 if lg["side"] == "buy" else -1) * float(lg["mid"]) * float(lg.get("contracts") or 1) * 100.0 for lg in legs)
                built = [{"option_type": lg["option_type"], "strike": lg["strike"], "side": lg["side"], "contracts": lg.get("contracts") or 1,
                          "mid": lg["mid"], "expiration": lg["expiration"]} for lg in legs]
                theoretical = riskmath.theoretical_max_loss(riskmath.legs_from_built(built))
                capital = riskmath.capital_required(kind, cost=abs(net), credit=net < 0, quantity=qty, last=anchor,
                                                    theoretical=theoretical["value"])["value"]
        except CannotValue as exc:
            failed.append({"key": kind, "label": c.get("label"), "error": str(exc)[:240], "code": "cannot_value_reliably"})
            continue
        except (RequestError, DataError, ValueError, KeyError) as exc:
            failed.append({"key": kind, "label": c.get("label"), "error": str(exc)[:240], "code": "cannot_value_reliably"})
            continue
        by_case, vectors = {}, {}
        for case in cases:
            prices, weights = grid(case["key"], eval_i)
            if curve is None:
                pnl = (1.0 if kind == "shares" else -1.0) * qty * (prices - float(entry))
            else:
                pnl = np.interp(prices, curve[0], curve[1])
            pnl = pnl - costs
            vectors[case["key"]] = pnl
            stats = riskmath.outcome_stats(prices, weights, pnl)
            by_case[case["key"]] = {"expected_pnl": float(stats["expected_pnl"]), "pnl_p10": float(stats["pnl_p10"]),
                                    "pnl_p90": float(stats["pnl_p90"]), "worst_tenth": cvar10(pnl, weights),
                                    "p_profit": float(stats["p_profit"]),
                                    "case_price": float(anchor * (1 + case["return_pct"][eval_i] / 100.0))}
        max_loss = theoretical.get("value") if not theoretical.get("unlimited") else None
        quality = list(dict.fromkeys(quality))
        structures.append({"key": kind, "label": c.get("label"), "direction": direction_of(kind), "instrument": c.get("instrument"),
                           "quantity": qty, "legs": legs, "entry": entry, "costs": round(costs, 2),
                           "capital": float(capital) if capital is not None else None,
                           "max_loss": float(max_loss) if max_loss is not None else None, "unlimited_loss": bool(theoretical.get("unlimited")),
                           "executable": kind in studio.EXECUTABLE, "eval_date": eval_day.isoformat(), "eval_session": eval_i + 1,
                           "volatility": ("after the release's expected collapse" if crushed else "today's implied volatility")
                           if curve is not None else None,
                           "quality": {"tier": tier_of(quality), "reasons": quality,
                                       "words": [QUALITY_REASONS[r] for r in quality if r in QUALITY_REASONS]},
                           "by_case": by_case, "flags": flags, "notes": notes,
                           "worst_case": min(v["expected_pnl"] for v in by_case.values()) if by_case else None, "_vectors": vectors})
    mixes = _mixes(structures, {k["key"]: grid(k["key"], last_i) for k in cases} if cases else {}, lim, grid)
    for x in structures:
        x.pop("_vectors", None)
    q = snap["quote"] or {}
    stock_codes = stock_quality(snap, lim, "shares") if q else ["quote_freshness_unknown"]
    fresh = {"quote_age_seconds": q.get("age_seconds"), "quote_freshness": q.get("freshness"), "realtime": q.get("realtime"),
             "session": snap.get("session"),
             # Unknown age is not fresh (4.5.1): 4.5.0 called a quote with no time fresh.
             "stale": any(code in stock_codes for code in ("stale_quote", "quote_freshness_unknown")),
             "delayed": not svc.market.info.realtime, "demo": svc.market.info.mode == "demo",
             "tier": tier_of([x for x in stock_codes if x != "one_sided_quote"])}
    status = "failed" if not structures else "partial" if (failed or snap["problems"]) else "complete"
    words = ("options valued after the earnings release's volatility collapse at or after its session "
             f"({crush:.1f} points); before it, at today's implied volatility" if crush else "options valued at today's implied volatility")
    return {"valuation_version": VALUATION_VERSION, "version_id": version["id"], "study_id": st["id"], "symbol": sym,
            "target_date": target.isoformat(), "anchor": version["anchor"], "snapshot_at": snap["taken_at"], "iv_assumption": words,
            "crush_points": crush, "limits": lim, "freshness": fresh, "structures": structures, "failed": failed, "mixes": mixes,
            "best_mix": mixes[0]["key"] if mixes else "cash", "status": status, "problems": snap["problems"],
            "note": ("Each structure is valued at the session it is held to (the target session, or its first expiration), against the "
                     "case's saved price and dispersion at that session, from one quote snapshot. Net of round-trip costs. Mixes are "
                     "ranked by their worst-case modeled net P&L across the cases; no case has a probability, so no probability-weighted "
                     "result is shown. Not advice; no order is placed.")}, snap


def _legs_now(snap: dict[str, Any], legs: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], list[str]]:
    rows, missing = [], []
    for lg in legs:
        q = snap["legs"].get(lg.get("option_symbol") or "")
        if q is None:
            missing.append(f"{lg.get('option_symbol')}: not in the snapshot's chain")
        q = q or {}
        rows.append({**{k: lg.get(k) for k in ("option_symbol", "option_type", "strike", "expiration", "side", "contracts")},
                     "bid": q.get("bid"), "ask": q.get("ask"), "mid": q.get("mid"), "spread_pct": q.get("spread_pct"), "iv": q.get("iv"),
                     "open_interest": q.get("open_interest"), "volume": q.get("volume"), "quote_time": q.get("quote_time"),
                     "freshness": q.get("freshness"), "age_seconds": q.get("age_seconds"), "greeks_updated_at": q.get("greeks_updated_at"),
                     "greeks_age_seconds": q.get("greeks_age_seconds"), "study_mid": _f(lg.get("mid")), "study_mid_used": False})
    return rows, missing


def _mixes(structures: list[dict[str, Any]], grids: dict[str, Any], lim: dict[str, Any], grid: Any = None) -> list[dict[str, Any]]:
    """Cash, each structure alone at the budget, and the What-If allocator's split under each case, all revalued under
    every case. A mix combines only structures valued at the same session (the same price grid in each case), so
    the allocator works per session. Mixes are bounded by the budget only; whether one stays inside the loss limit is
    reported beside it (the opportunity checks decide, 4.4)."""
    budget, loss_limit = lim["budget"], lim["loss_limit"]
    ok = [s for s in structures if not s["unlimited_loss"] and s["capital"] and 0 < s["capital"] <= budget]
    out: list[dict[str, Any]] = [{"key": "cash", "label": "Cash", "parts": [], "capital": 0.0, "max_loss": 0.0, "within_loss_limit": True,
                                  "by_case": {k: {"expected_pnl": 0.0, "worst_tenth": 0.0} for k in grids}, "worst_case": 0.0,
                                  "worst_tenth": 0.0}]

    def weights_of(ck: str, session: int) -> np.ndarray:
        if grid is None:
            return grids[ck][1]
        return grid(ck, session - 1)[1]

    def describe(parts: list[tuple[dict[str, Any], int]], key: str, label: str) -> dict[str, Any]:
        by_case = {}
        session = parts[0][0].get("eval_session") or 1
        for ck in grids:
            weights = weights_of(ck, session)
            total = sum(n * s["_vectors"][ck] for s, n in parts)
            by_case[ck] = {"expected_pnl": float((weights * total).sum()), "worst_tenth": cvar10(total, weights)}
        most = float(sum(n * (s["max_loss"] or 0) for s, n in parts))
        return {"key": key, "label": label, "parts": [{"key": s["key"], "label": s["label"], "units": n} for s, n in parts],
                "capital": float(sum(n * s["capital"] for s, n in parts)), "max_loss": most, "within_loss_limit": most <= loss_limit + 1e-9,
                "eval_session": session, "by_case": by_case, "worst_case": min(v["expected_pnl"] for v in by_case.values()),
                "worst_tenth": max(v["worst_tenth"] for v in by_case.values())}
    for s in ok:
        units = int(budget // s["capital"])
        if units >= 1:
            out.append(describe([(s, units)], f"single:{s['key']}", f"{s['label']} ×{units}"))
    by_session: dict[int, list[dict[str, Any]]] = {}
    for s in ok:
        by_session.setdefault(int(s.get("eval_session") or 1), []).append(s)
    many = len(by_session) > 1
    for session, group in sorted(by_session.items(), reverse=True):
        for ck in grids:
            weights = weights_of(ck, session)
            items = [{"s": s, "capital": s["capital"], "pnl": s["_vectors"][ck], "units": 0, "most": 10_000} for s in group]
            greedy_allocate(items, weights, budget=budget, max_share=MIX_MAX_SHARE, lam=MIX_RISK)
            parts = [(it["s"], int(it["units"])) for it in items if it["units"] > 0]
            if parts:
                key = f"mix:{ck}" if not many or session == max(by_session) else f"mix:{ck}:s{session}"
                out.append(describe(parts, key, f"Split for the {ck} case: " + ", ".join(f"{s['label']} ×{n}" for s, n in parts)
                                    + (f" (held to session {session})" if many else "")))
    seen, uniq = set(), []
    for m in sorted(out, key=lambda m: (-m["worst_case"], m["capital"])):
        sig = tuple(sorted((p["key"], p["units"]) for p in m["parts"]))
        if sig in seen:
            continue
        seen.add(sig)
        uniq.append(m)
    return uniq[:10]


def save(svc: Any, result: dict[str, Any], snap: dict[str, Any]) -> str:
    import json
    vid = C.new_id("sval")
    with svc.db.tx() as cur:
        cur.execute("""INSERT INTO scenario_valuations (id, version_id, study_id, created_at, status, snapshot_json, result_json)
                       VALUES (?, ?, ?, ?, ?, ?, ?)""",
                    (vid, result["version_id"], result["study_id"], C.utc_now(), result["status"], json.dumps(snap, default=str),
                     json.dumps(result, default=str)))
    return vid


def load(svc: Any, valuation_id: str) -> dict[str, Any]:
    import json
    r = svc.db.one("SELECT * FROM scenario_valuations WHERE id = ?", (valuation_id,))
    if r is None:
        raise RequestError(f"No valuation {valuation_id}")
    return {"id": r["id"], "created_at": r["created_at"], **json.loads(r["result_json"])}


def latest_for_version(svc: Any, version_id: str) -> dict[str, Any] | None:
    r = svc.db.one("SELECT id FROM scenario_valuations WHERE version_id = ? ORDER BY created_at DESC, id DESC LIMIT 1", (version_id,))
    return load(svc, r["id"]) if r else None


__all__ = ["value", "save", "load", "latest_for_version", "limits", "snapshot", "direction_of", "VALUATION_VERSION", "session_index",
           "case_grid", "stock_quality", "leg_quality", "QUALITY_REASONS", "TIERS"]
