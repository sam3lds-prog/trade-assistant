"""Split a budget across several opportunities (2.15): which trades, which instrument, and how many of each.

**The model.** Every stock in the blend is walked forward together on PATHS paths, from its entry to the end of the
horizon: one session for day trades (15-minute steps from the open, or from now once trading, to 15:55), or one
trading day at a time to the horizon date for swing trades. The stocks move together the way they have over the
last year: their correlation from daily returns, pulled 30% toward the average correlation so a short history does
not make two stocks look like perfect hedges, and repaired to a valid matrix. Each stock's volatility is its own
over the last 60 days. Each one drifts by its opportunity's edge (opportunities.py: a past average the trade's way,
pulled toward zero when its record is short) and by nothing else: with no edge the model has no view on direction.

Each trade's own rules apply on every path: out at the stop or at target 1, whichever the price touches first (the
touch between two steps is found with the Brownian-bridge chance; when both could have been touched in one step the
stop is taken, the cautious reading), otherwise out at the end of the horizon. Shares and shorts pay
settings.stock_cost_bps each way. A bought call or put is valued with Black-76 at its own implied volatility when the
stock exits, and sold half its spread below that value; it costs the card's suggested limit. What a short costs to
borrow is not included.

**The split.** Every goal maximises the expected profit; they differ in how much tail risk they accept, measured
as the average loss in the worst tenth of outcomes (CVaR 10%):

    Most dollars              no tail limit: the most expected profit the caps allow
    Best return for the risk  the tail loss kept within `tail_pct` of the budget (2% unless set)
    Lowest risk               the tail loss kept within LOW_RISK_TAIL_PCT of the budget, and trades preferred by the
                              expected profit they add for each dollar of tail loss they add

A greedy search adds the block of units that adds the most expected profit per dollar spent (per dollar of added
tail loss, for Lowest risk), halving blocks when nothing more fits, within: the budget; at most `max_share` of it in any one trade and `sector_cap` in any one sector fund; at
most `max_positions` trades, one instrument each; and, unless switched off (paper portfolios may), your limits: the
loss at the stop of one trade (the whole premium, for an option) within MAX_RISK_PER_TRADE_PCT of ACCOUNT_SIZE, and
everything open (your positions and journal included) within MAX_OPEN_RISK_PCT.

When no trade's record beats its costs, nothing is the honest split; an equal-risk split across the best-scored
trades is shown beside it, marked as having no edge behind it.

A model with stated assumptions, not a forecast, and nothing here places an order.
"""

from __future__ import annotations

import hashlib
import logging
import math
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

import numpy as np
import pandas as pd

from . import marketdays
from .opportunities import DAY_EXIT, GOAL_WORDS, GOALS, INSTRUMENTS, realized_vol
from .optionmath import NY
from .providers.base import DataError
from .simulate import MULTIPLIER, Leg, leg_value
from .tradesim import _touch_chance

log = logging.getLogger(__name__)

PATHS = 4000
SEED = 20_315
DAY_STEP_MINUTES = 15
TRADING_MINUTES_PER_YEAR = 252 * 390
CORR_SHRINK = 0.30
CORR_DAYS = 250
MAX_CANDIDATES = 25
MAX_SHARES = 100_000
MAX_CONTRACTS = 100
LOW_RISK_TAIL_PCT = 0.5
DEFAULT_TAIL_PCT = 2.0
TAIL = 0.10
STRESS_SHOCKS = (-5.0, -2.0, 2.0, 5.0)
DEFAULT_VOL = 0.30


class BlendError(ValueError):
    pass


@dataclass(eq=False)
class Candidate:
    opp: dict[str, Any]
    instrument: str
    unit_cost: float          # the money one unit ties up
    unit_risk: float          # what one unit loses at the stop (the whole premium for an option)
    pnl: np.ndarray           # one unit's profit or loss on each path
    most: int
    leg: Leg | None = None
    units: int = 0


# --- time grids --------------------------------------------------------------------------------------------------

def day_grid(now: datetime) -> tuple[list[datetime], np.ndarray, str]:
    """The moments a day trade is stepped through: from the open (before it) or from now (during the session) to
    15:55, every 15 minutes; after 15:55 or on a closed day, the next session's. Returns the moments (the first is
    the start), the trading-time years from the start to each, and a sentence on what was assumed."""
    now = now.astimezone(NY)
    day = now.date()
    s = marketdays.session(day)
    exit_at = datetime.combine(day, DAY_EXIT, NY) if s else None
    if s and s[1].time() < DAY_EXIT:                 # an early close: out five minutes before it
        exit_at = s[1] - timedelta(minutes=5)
    if s is None or now >= exit_at:
        day = marketdays.next_trading_day(day)
        s = marketdays.session(day)
        exit_at = min(datetime.combine(day, DAY_EXIT, NY), s[1] - timedelta(minutes=5))
        start, note = s[0], f"A plan for the next session ({day:%a %b %d}): entered at the open at today's price, out by {exit_at:%H:%M}."
    elif now < s[0]:
        start, note = s[0], f"Entered at the open at the price now (before the open), out by {exit_at:%H:%M}."
    else:
        start, note = now, f"Entered now, out by {exit_at:%H:%M} New York time."
    moments = [start]
    t = start
    while t < exit_at:
        t = min(t + timedelta(minutes=DAY_STEP_MINUTES), exit_at)
        moments.append(t)
    if len(moments) < 2:
        moments.append(exit_at)
    years = np.array([(m - start).total_seconds() / 60.0 / TRADING_MINUTES_PER_YEAR for m in moments])
    return moments, years, note


def swing_grid(now: datetime, days: int) -> tuple[list[datetime], np.ndarray, str]:
    """One step per trading day's close, to the close `days` trading days after today (marketdays.horizon_close,
    the trade card's horizon date); when today's session has not closed yet, its close is the first step (the
    part of it left)."""
    now = now.astimezone(NY)
    today = now.date()
    days = max(1, int(days))
    closes: list[datetime] = []
    s = marketdays.session(today)
    first_frac = 1.0
    if s and now < s[1]:
        closes.append(s[1])
        left = (s[1] - max(now, s[0])).total_seconds() / 60.0
        first_frac = max(left / 390.0, 1 / 390.0)
    d = today
    for _ in range(days):
        d = marketdays.next_trading_day(d)
        closes.append(marketdays.session(d)[1])
    moments = [now] + closes
    steps = np.array([first_frac] + [1.0] * (len(closes) - 1))
    years = np.concatenate([[0.0], np.cumsum(steps) / 252.0])
    return moments, years, f"Held up to {days} trading day{'s' if days != 1 else ''}, to the close of {closes[-1]:%a %b %d}."


# --- correlations and paths -----------------------------------------------------------------------------------

def nearest_correlation(c: np.ndarray) -> np.ndarray:
    """A valid correlation matrix close to `c`: negative eigenvalues lifted to a small floor, unit diagonal."""
    c = (c + c.T) / 2
    w, v = np.linalg.eigh(c)
    w = np.clip(w, 1e-4, None)
    out = (v * w) @ v.T
    d = np.sqrt(np.clip(np.diag(out), 1e-12, None))
    out = out / np.outer(d, d)
    np.fill_diagonal(out, 1.0)
    return out


def correlation_of(returns: pd.DataFrame, symbols: list[str]) -> tuple[np.ndarray, dict[str, Any]]:
    """The symbols' correlation from daily returns, pulled CORR_SHRINK toward the average and made valid. A stock
    without enough shared history gets the average correlation with the rest."""
    n = len(symbols)
    if n == 1:
        return np.ones((1, 1)), {"days": None, "average": None}
    cols = [s for s in symbols if s in returns.columns]
    raw = returns[cols].corr(min_periods=60) if cols else pd.DataFrame()
    c = np.full((n, n), np.nan)
    for i, a in enumerate(symbols):
        for j, b in enumerate(symbols):
            if a in raw.index and b in raw.columns:
                c[i, j] = raw.loc[a, b]
    off = c[~np.eye(n, dtype=bool)]
    avg = float(np.nanmean(off)) if np.isfinite(off).any() else 0.3
    c = np.where(np.isfinite(c), c, avg)
    np.fill_diagonal(c, 1.0)
    shrunk = (1 - CORR_SHRINK) * c + CORR_SHRINK * np.where(np.eye(n, dtype=bool), 1.0, avg)
    days = int(returns[cols].dropna().shape[0]) if cols else 0
    return nearest_correlation(shrunk), {"days": days, "average": avg}


def joint_log_paths(spots: np.ndarray, vols: np.ndarray, drifts: np.ndarray, corr: np.ndarray, years: np.ndarray,
                    paths: int = PATHS, seed: int = SEED) -> np.ndarray:
    """Log prices, shape (steps + 1, stocks, paths), walked together under the correlation. `drifts` are each
    stock's growth rate a year (the mean of the price, not of its log)."""
    rng = np.random.default_rng(seed)
    chol = np.linalg.cholesky(corr)
    n = len(spots)
    out = np.empty((len(years), n, paths))
    out[0] = np.log(spots)[:, None]
    dts = np.diff(years)
    for k, dt in enumerate(dts, start=1):
        eps = rng.standard_normal((n, paths))
        z = chol @ eps
        out[k] = out[k - 1] + ((drifts - 0.5 * vols ** 2) * dt)[:, None] + (vols * math.sqrt(max(dt, 1e-12)))[:, None] * z
    return out


def walk_exits(x: np.ndarray, var: np.ndarray, *, direction: int, stop: float, target: float,
               rng: np.random.Generator) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """For one stock's log-price paths `x` (steps + 1, paths) with per-step variances `var`: the step each path
    exits, its exit price and how (1 target, -1 stop, 0 the horizon). The stop is taken when both could have been
    touched within one step."""
    steps, paths = x.shape[0] - 1, x.shape[1]
    ls, lt = math.log(stop), math.log(target)
    exit_step = np.full(paths, steps, dtype=int)
    exit_px = np.exp(x[-1]).copy()
    kind = np.zeros(paths, dtype=int)
    open_ = np.ones(paths, dtype=bool)
    for k in range(steps):
        x0, x1 = x[k], x[k + 1]
        p_stop = _touch_chance(x0, x1, ls, direction < 0, var[k])
        p_tgt = _touch_chance(x0, x1, lt, direction > 0, var[k])
        hit_stop = open_ & (rng.random(paths) < p_stop)
        hit_tgt = open_ & ~hit_stop & (rng.random(paths) < p_tgt)
        exit_step[hit_stop], exit_px[hit_stop], kind[hit_stop] = k + 1, stop, -1
        exit_step[hit_tgt], exit_px[hit_tgt], kind[hit_tgt] = k + 1, target, 1
        open_ &= ~(hit_stop | hit_tgt)
        if not open_.any():
            break
    return exit_step, exit_px, kind


def cvar(total: np.ndarray, tail: float = TAIL) -> float:
    """The average loss in the worst `tail` of outcomes, as a positive number (0 when even those are profits)."""
    k = max(1, int(round(tail * len(total))))
    worst = np.partition(total, k - 1)[:k]
    return max(0.0, -float(worst.mean()))


def _seed_for(symbol: str) -> int:
    return SEED + int(hashlib.sha256(symbol.encode()).hexdigest()[:6], 16)


# --- the blend ----------------------------------------------------------------------------------------------------

class Blender:
    def __init__(self, svc: Any):
        self.svc = svc

    # --- inputs ---------------------------------------------------------------------------------------------
    def _bars(self, symbol: str) -> pd.DataFrame | None:
        try:
            return self.svc.market.bars(symbol, 1.2, complete=True)
        except (DataError, ValueError) as exc:
            log.info("Blend: no bars for %s: %s", symbol, exc)
            return None

    def limits(self, obey: bool, include_real: bool = True) -> dict[str, Any]:
        """Your limits in dollars, and the risk already open (your positions and the journal) unless `include_real` is
        off (a paper portfolio is measured on its own)."""
        s = self.svc.settings
        out = {"obey": bool(obey), "per_trade": s.account_size * s.max_risk_per_trade_pct / 100.0,
               "open_total": s.account_size * s.max_open_risk_pct / 100.0, "open_now": 0.0, "unknown": [],
               "includes_real": bool(include_real)}
        if not obey or not include_real:
            return out
        try:
            committed = float(self.svc.journal.committed_risk())
        except Exception:                             # noqa: BLE001 - unmeasured is said, never counted as zero
            committed = 0.0
            out["unknown"].append("the option journal's committed risk")
        try:
            book = self.svc.book_risk()
            out["open_now"] = committed + float(book["total"])
            out["unknown"] += [u.get("description", "a position") for u in book.get("unknown") or []]
        except Exception:                             # noqa: BLE001
            out["open_now"] = committed
            out["unknown"].append("the open positions")
        return out

    def _units_for(self, opp: dict[str, Any], instruments: list[str]) -> list[str]:
        d = opp["direction"]
        opt = opp.get("option") or {}
        out = []
        if d == "LONG" and "shares" in instruments:
            out.append("shares")
        if d == "SHORT" and "short" in instruments:
            out.append("short")
        if opt and d == "LONG" and opt.get("option_type") == "CALL" and "call" in instruments:
            out.append("call")
        if opt and d == "SHORT" and opt.get("option_type") == "PUT" and "put" in instruments:
            out.append("put")
        return out

    # --- the whole thing --------------------------------------------------------------------------------------
    def run(self, opportunities: list[dict[str, Any]], *, budget: float, goal: str = "balanced", tail_pct: float = DEFAULT_TAIL_PCT,
            max_share: float = 0.2, max_positions: int = 10, sector_cap: float = 0.4,
            instruments: list[str] | None = None, obey_limits: bool = True, horizon: str | None = None,
            units: dict[str, int] | None = None, paths: int = PATHS, now: datetime | None = None,
            real_open_risk: bool = True, cap_base: float | None = None, held_sectors: dict[str, float] | None = None,
            held_risk: float = 0.0) -> dict[str, Any]:
        """The split (or, with `units` as {"<key>|<instrument>": n}, the figures of a split given as it is, checked
        against every cap and limit). A paper portfolio adding to what it holds passes `cap_base` (its whole value,
        which the share and sector caps are measured against), `held_sectors` (dollars already in each sector) and
        `held_risk` (risk already open in it), so the caps hold for the portfolio, not only for the new trades."""
        if not 100 <= budget <= 10_000_000:
            raise BlendError("The budget must be between $100 and $10,000,000")
        if goal not in GOALS:
            raise BlendError(f"The goal must be one of {', '.join(GOALS)}")
        instruments = [i for i in (instruments or list(INSTRUMENTS)) if i in INSTRUMENTS]
        if not instruments:
            raise BlendError("Allow at least one instrument")
        max_share = float(np.clip(max_share, 0.05, 1.0))
        sector_cap = float(np.clip(sector_cap, 0.1, 1.0))
        max_positions = int(np.clip(max_positions, 1, 25))
        now = (now or datetime.now(NY)).astimezone(NY)
        skipped: list[dict[str, Any]] = []
        kinds = {o["horizon"] for o in opportunities}
        horizon = horizon or ("day" if kinds == {"day"} else "swing")
        if horizon == "day":
            # The horizon decides first, so a stock's day trade is not lost behind its higher-scored swing trade.
            for o in opportunities:
                if o["horizon"] != "day":
                    skipped.append({"key": o["key"], "symbol": o["symbol"], "why": "a swing trade cannot go in a day-trade blend"})
            opportunities = [o for o in opportunities if o["horizon"] == "day"]
            if not opportunities:
                raise BlendError("None of these opportunities is a day trade")
        # One trade per stock (the best scored), the best MAX_CANDIDATES of them.
        seen: dict[str, dict[str, Any]] = {}
        for o in sorted(opportunities, key=lambda o: -(o.get("score") or 0)):
            if o["symbol"] in seen:
                skipped.append({"key": o["key"], "symbol": o["symbol"], "why": f"{o['symbol']} is already in the blend "
                                                                                "through a higher-scored trade"})
                continue
            seen[o["symbol"]] = o
        opps = list(seen.values())[:MAX_CANDIDATES]
        for o in list(seen.values())[MAX_CANDIDATES:]:
            skipped.append({"key": o["key"], "symbol": o["symbol"], "why": f"only the best {MAX_CANDIDATES} are blended"})
        if not opps:
            raise BlendError("No opportunities to blend")
        if horizon == "day":
            moments, years, grid_note = day_grid(now)
        else:
            moments, years, grid_note = swing_grid(now, max(int(o.get("horizon_days") or 5) for o in opps))
        steps = len(moments) - 1
        # Each trade's own exit step. In a swing blend a day trade leaves at the first close and an N-day trade at the
        # close N trading days after today (the first step is today's close while today's session is still to come).
        sess = marketdays.session(moments[0].date())
        open_today = horizon == "swing" and bool(sess) and moments[0] < sess[1]
        exit_steps = [steps if horizon == "day" else
                      (1 if o["horizon"] == "day" else min(steps, int(o.get("horizon_days") or steps) + (1 if open_today else 0)))
                      for o in opps]

        # Volatility, correlation, betas: a year of daily bars for each stock and SPY.
        bars = {o["symbol"]: self._bars(o["symbol"]) for o in opps}
        spy = self._bars("SPY")
        frames = {k: v for k, v in bars.items() if v is not None and not v.empty}
        if spy is not None and not spy.empty:
            frames["SPY"] = spy
        from .portfolio import returns_frame
        rets = returns_frame(frames, days=CORR_DAYS) if frames else pd.DataFrame()
        symbols = [o["symbol"] for o in opps]
        vols, notes = [], [grid_note]
        for o in opps:
            v = o.get("vol")
            b = bars.get(o["symbol"])
            if not v and b is not None and not b.empty:
                v = realized_vol(b["close"].to_numpy(dtype=float))
            if not v:
                v = DEFAULT_VOL
                notes.append(f"{o['symbol']}: no volatility could be measured; {DEFAULT_VOL:.0%} a year is assumed.")
            vols.append(float(v))
        vols_a = np.array(vols)
        corr, corr_info = correlation_of(rets, symbols)
        spots = np.array([o["entry"] for o in opps], dtype=float)
        drifts = []
        for o, n_o in zip(opps, exit_steps):
            e = o.get("edge") or {}
            gross = (float(e.get("expected_pct") or 0.0) + float(e.get("cost_pct") or 0.0)) if e.get("known") else 0.0
            d = 1 if o["direction"] == "LONG" else -1
            # Spread over the trade's own span, not the whole grid's, so a 5-day trade in a blend with a 20-day one
            # still expects its whole edge by the day it leaves.
            drifts.append(math.log(max(1e-6, 1 + d * gross / 100.0)) / max(float(years[n_o]), 1e-6))
        drifts_a = np.array(drifts)
        x = joint_log_paths(spots, vols_a, drifts_a, corr, years, paths=paths)

        # Each trade's exits, and one unit's result for each instrument it allows.
        cost_rate = float(self.svc.settings.stock_cost_bps) / 10_000.0
        rate = float(self.svc.settings.risk_free_rate)
        dts = np.diff(years)
        mids = [moments[k] + (moments[k + 1] - moments[k]) / 2 for k in range(steps)]
        cands: list[Candidate] = []
        exits: dict[str, tuple[np.ndarray, np.ndarray, np.ndarray]] = {}
        for i, o in enumerate(opps):
            d = 1 if o["direction"] == "LONG" else -1
            var = (vols_a[i] ** 2) * dts
            n_o = exit_steps[i]
            st, px, kind = walk_exits(x[: n_o + 1, i, :], var[:n_o], direction=d, stop=o["stop"], target=o["target_1"],
                                      rng=np.random.default_rng(_seed_for(o["symbol"])))
            exits[o["key"]] = (st, px, kind)
            allowed = self._units_for(o, instruments)
            if not allowed:
                skipped.append({"key": o["key"], "symbol": o["symbol"], "why": "none of the allowed instruments fits it "
                                + ("(a short needs shorting allowed)" if d < 0 and "short" not in instruments else
                                   "(no contract on its card)" if any(k in instruments for k in ("call", "put")) else "")})
                continue
            for inst in allowed:
                if inst in ("shares", "short"):
                    entry = o["entry"]
                    pnl = d * (px - entry) - cost_rate * (entry + px)
                    cands.append(Candidate(o, inst, unit_cost=entry, unit_risk=abs(entry - o["stop"]) + cost_rate * 2 * entry,
                                           pnl=pnl, most=MAX_SHARES))
                else:
                    opt = o["option"]
                    iv = opt.get("iv")
                    if not iv or iv <= 0:
                        skipped.append({"key": o["key"], "symbol": o["symbol"], "why": f"its {inst} has no implied volatility to value it"})
                        continue
                    leg = Leg(inst, float(opt["strike"]), opt["expiration"], 1, 1, float(opt["premium"]), float(iv), rate)
                    value = np.zeros(paths)
                    for k in np.unique(st):
                        # A path out at a level is valued in the middle of the step it touched; one held, at the end.
                        m = st == k
                        held, lvl = m & (kind == 0), m & (kind != 0)
                        if held.any():
                            value[held] = leg_value(leg, px[held], moments[int(k)], rate)
                        if lvl.any():
                            value[lvl] = leg_value(leg, px[lvl], mids[max(int(k) - 1, 0)], rate)
                    bid, ask = opt.get("bid"), opt.get("ask")
                    half = 0.5 * (ask - bid) if bid is not None and ask is not None and ask > bid else 0.03 * float(opt["premium"])
                    sold = np.maximum(value - half, 0.0)
                    pnl = (sold - leg.entry) * MULTIPLIER
                    cost = leg.entry * MULTIPLIER
                    cands.append(Candidate(o, inst, unit_cost=cost, unit_risk=cost, pnl=pnl, most=MAX_CONTRACTS, leg=leg))
        if not cands:
            raise BlendError("Nothing can be valued: " + "; ".join(f"{s['symbol']}: {s['why']}" for s in skipped[:5]))

        lim = self.limits(obey_limits, include_real=real_open_risk)
        tail_pct = float(np.clip(tail_pct, 0.1, 50.0))
        cap_pct = None if goal == "dollars" else (min(LOW_RISK_TAIL_PCT, tail_pct) if goal == "low_risk" else tail_pct)
        tail_cap = float("inf") if cap_pct is None else budget * cap_pct / 100.0
        caps = {"cap_base": float(cap_base or budget), "held_sectors": dict(held_sectors or {}), "held_risk": float(held_risk or 0.0)}
        breaches: list[str] = []
        if units is not None:
            for c in cands:
                c.units = max(0, int(units.get(f"{c.opp['key']}|{c.instrument}", 0)))
            fallback = None
            breaches = self._breaches(cands, budget=budget, max_share=max_share, max_positions=max_positions,
                                      sector_cap=sector_cap, lim=lim, tail_cap=tail_cap, **caps)
        else:
            self._greedy(cands, budget=budget, tail_cap=tail_cap, goal=goal, max_share=max_share, max_positions=max_positions,
                         sector_cap=sector_cap, lim=lim, skipped=skipped, **caps)
            fallback = None
            if not any(c.units for c in cands):
                fallback = self._equal_risk(cands, budget=budget, max_share=max_share, max_positions=max_positions,
                                            sector_cap=sector_cap, lim=lim, **caps)
        out = self._report(cands, budget=budget, goal=goal, x=x, moments=moments, exits=exits, opps=opps,
                           corr=corr, rets=rets, lim=lim, rate=rate, cost_rate=cost_rate)
        out.update({"goal": goal, "goal_label": GOAL_WORDS[goal], "tail_pct": cap_pct, "tail_cap": None if cap_pct is None else tail_cap,
                    "max_share": max_share,
                    "max_positions": max_positions, "sector_cap": sector_cap, "instruments": instruments,
                    "horizon": horizon, "start": moments[0].isoformat(timespec="minutes"),
                    "end": moments[-1].isoformat(timespec="minutes"), "steps": steps, "paths": paths, "seed": SEED,
                    "skipped": skipped, "notes": notes, "correlation_days": corr_info.get("days"),
                    "average_correlation": corr_info.get("average"), "given": units is not None,
                    "breaches": breaches, "within_limits": not breaches,
                    "data_source": self.svc.market.info.mode})
        if breaches:
            out["notes"].append("These units break " + ("a cap or limit" if len(breaches) == 1 else f"{len(breaches)} caps or limits")
                                + ": " + "; ".join(breaches) + ". The optimizer's own split never does; plan the split again "
                                "to get one that fits.")
        if fallback is not None:
            fb_units = {f"{c.opp['key']}|{c.instrument}": n for c, n in fallback.items() if n}
            for c in cands:
                c.units = 0
            for c, n in fallback.items():
                c.units = n
            out["fallback"] = self._report(cands, budget=budget, goal=goal, x=x, moments=moments, exits=exits,
                                           opps=opps, corr=corr, rets=rets, lim=lim, rate=rate, cost_rate=cost_rate)
            out["fallback"]["units"] = fb_units
            out["notes"].append("No trade's past record beats its costs within this goal's limits, so the split is to hold "
                                "cash. The equal-risk split shown beside it has no edge behind it: it is for study, not a "
                                "recommendation.")
        out["model"] = ("Paths walked together at each stock's own volatility and last year's correlation, drifting only by "
                        "each trade's past-record edge (pulled toward zero when its record is short); each trade's stop and "
                        "target 1 applied on every path, the stop taken when both could have been hit in one step. A model, "
                        "not a forecast; nothing here places an order.")
        return out

    # --- the search ---------------------------------------------------------------------------------------------
    def _greedy(self, cands: list[Candidate], *, budget: float, tail_cap: float, goal: str, max_share: float,
                max_positions: int, sector_cap: float, lim: dict[str, Any], skipped: list[dict[str, Any]],
                cap_base: float, held_sectors: dict[str, float], held_risk: float) -> None:
        total = np.zeros_like(cands[0].pnl)
        spent = 0.0
        risk_used = 0.0
        sectors: dict[str, float] = dict(held_sectors)
        chosen: dict[str, str] = {}                 # opportunity key -> the instrument it uses
        best, tail_now = 0.0, 0.0
        blocks = [max(1, int(0.10 * budget / c.unit_cost)) for c in cands]
        why_out: dict[str, str] = {}
        for c in cands:
            if c.unit_cost > cap_base * max_share:
                why_out[f"{c.opp['key']}|{c.instrument}"] = (f"one {'contract' if c.leg else 'share'} costs ${c.unit_cost:,.0f}, "
                                                            f"more than {max_share:.0%} of the budget")
            elif lim["obey"] and c.unit_risk > lim["per_trade"] + 1e-9:
                why_out[f"{c.opp['key']}|{c.instrument}"] = (f"one {'contract' if c.leg else 'share'} risks ${c.unit_risk:,.0f}, "
                                                            f"more than your ${lim['per_trade']:,.0f} risk per trade")
        open_left = lim["open_total"] - lim["open_now"] - held_risk if lim["obey"] else float("inf")
        for _ in range(4000):
            pick, pick_rate, pick_gain, pick_b, pick_tail = None, 0.0, 0.0, 0, 0.0
            for i, c in enumerate(cands):
                b = blocks[i]
                key = c.opp["key"]
                if b < 1 or f"{key}|{c.instrument}" in why_out:
                    continue
                if chosen.get(key, c.instrument) != c.instrument:
                    continue
                if key not in chosen and len(chosen) >= max_positions:
                    continue
                cost = b * c.unit_cost
                sec = c.opp.get("sector") or f"none:{c.opp['symbol']}"
                if (c.units + b > c.most or spent + cost > budget + 1e-9 or (c.units + b) * c.unit_cost > max_share * cap_base + 1e-9
                        or sectors.get(sec, 0.0) + cost > sector_cap * cap_base + 1e-9):
                    continue
                if lim["obey"] and ((c.units + b) * c.unit_risk > lim["per_trade"] + 1e-9 or risk_used + b * c.unit_risk > open_left + 1e-9):
                    continue
                trial = total + b * c.pnl
                gain = float(trial.mean()) - best
                if gain <= 1e-9:
                    continue
                tail = cvar(trial) if tail_cap < float("inf") else 0.0
                if tail > tail_cap + 1e-9:
                    continue
                rate = gain / max(tail - tail_now, 1e-6 * cost) if goal == "low_risk" else gain / cost
                if rate > pick_rate:
                    pick, pick_rate, pick_gain, pick_b, pick_tail = i, rate, gain, b, tail
            if pick is None:
                if all(b <= 1 for b in blocks):
                    break
                blocks = [max(1, b // 2) if b > 1 else 1 for b in blocks]
                continue
            c = cands[pick]
            c.units += pick_b
            total = total + pick_b * c.pnl
            spent += pick_b * c.unit_cost
            risk_used += pick_b * c.unit_risk
            sec = c.opp.get("sector") or f"none:{c.opp['symbol']}"
            sectors[sec] = sectors.get(sec, 0.0) + pick_b * c.unit_cost
            chosen[c.opp["key"]] = c.instrument
            best += pick_gain
            tail_now = pick_tail
        for k, why in why_out.items():
            key, inst = k.split("|")
            if not any(c.units for c in cands if c.opp["key"] == key):
                skipped.append({"key": key, "symbol": next(c.opp["symbol"] for c in cands if c.opp["key"] == key),
                                "instrument": inst, "why": why})

    def _equal_risk(self, cands: list[Candidate], *, budget: float, max_share: float, max_positions: int,
                    sector_cap: float, lim: dict[str, Any], cap_base: float, held_sectors: dict[str, float],
                    held_risk: float) -> dict[Candidate, int]:
        """The same risk in each of the best-scored trades (shares or a short where allowed, else the contract),
        within every cap: for study when no record beats its costs."""
        by_opp: dict[str, Candidate] = {}
        for c in sorted(cands, key=lambda c: (-(c.opp.get("score") or 0), c.leg is not None)):
            by_opp.setdefault(c.opp["key"], c)
        picks = list(by_opp.values())[:max_positions]
        if not picks:
            return {}
        per_risk = budget * 0.01 if not lim["obey"] else min(lim["per_trade"], budget * 0.01)
        open_left = (lim["open_total"] - lim["open_now"] - held_risk) if lim["obey"] else float("inf")
        per_risk = min(per_risk, max(open_left, 0.0) / len(picks)) if open_left < float("inf") else per_risk
        out: dict[Candidate, int] = {}
        spent, sectors = 0.0, dict(held_sectors)
        for c in picks:
            n = int(per_risk // max(c.unit_risk, 1e-9))
            n = min(n, int(max_share * cap_base // c.unit_cost), c.most)
            sec = c.opp.get("sector") or f"none:{c.opp['symbol']}"
            room = min(budget - spent, sector_cap * cap_base - sectors.get(sec, 0.0))
            n = min(n, int(max(room, 0) // c.unit_cost))
            if n >= 1:
                out[c] = n
                spent += n * c.unit_cost
                sectors[sec] = sectors.get(sec, 0.0) + n * c.unit_cost
        return out

    @staticmethod
    def _breaches(cands: list[Candidate], *, budget: float, max_share: float, max_positions: int, sector_cap: float,
                  lim: dict[str, Any], tail_cap: float, cap_base: float, held_sectors: dict[str, float],
                  held_risk: float) -> list[str]:
        """What a split given as units breaks: the budget, the caps, the tail limit and (when they apply) your risk
        limits. Each in words; empty when it fits."""
        held = [c for c in cands if c.units > 0]
        out: list[str] = []
        spent = sum(c.units * c.unit_cost for c in held)
        if spent > budget + 1e-6:
            out.append(f"it spends ${spent:,.0f} of a ${budget:,.0f} budget")
        if len({c.opp["key"] for c in held}) > max_positions:
            out.append(f"{len({c.opp['key'] for c in held})} trades, more than the {max_positions} allowed")
        sectors: dict[str, float] = dict(held_sectors)
        for c in held:
            if c.units * c.unit_cost > max_share * cap_base + 1e-6:
                out.append(f"{c.opp['symbol']} is ${c.units * c.unit_cost:,.0f}, more than {max_share:.0%} of the budget")
            sec = c.opp.get("sector") or f"none:{c.opp['symbol']}"
            sectors[sec] = sectors.get(sec, 0.0) + c.units * c.unit_cost
        for sec, dollars in sectors.items():
            if not sec.startswith("none:") and dollars > sector_cap * cap_base + 1e-6:
                out.append(f"{sec} holds ${dollars:,.0f}, more than {sector_cap:.0%} of the budget")
        if held and tail_cap < float("inf"):
            tail = cvar(sum(c.units * c.pnl for c in held))
            if tail > tail_cap + 1e-6:
                out.append(f"its worst tenth averages a ${tail:,.0f} loss, past the goal's ${tail_cap:,.0f}")
        if lim["obey"]:
            for c in held:
                if c.units * c.unit_risk > lim["per_trade"] + 1e-6:
                    out.append(f"{c.opp['symbol']} risks ${c.units * c.unit_risk:,.0f} to its stop, more than your "
                               f"${lim['per_trade']:,.0f} a trade")
            open_left = lim["open_total"] - lim["open_now"] - held_risk
            used = sum(c.units * c.unit_risk for c in held)
            if used > open_left + 1e-6:
                out.append(f"it risks ${used:,.0f} in all, more than the ${max(open_left, 0):,.0f} of open risk your limits leave")
        return out

    # --- the figures --------------------------------------------------------------------------------------------
    def _report(self, cands: list[Candidate], *, budget: float, goal: str, x: np.ndarray,
                moments: list[datetime], exits: dict[str, Any], opps: list[dict[str, Any]], corr: np.ndarray,
                rets: pd.DataFrame, lim: dict[str, Any], rate: float, cost_rate: float) -> dict[str, Any]:
        held = [c for c in cands if c.units > 0]
        paths = x.shape[2]
        total = np.zeros(paths)
        for c in held:
            total += c.units * c.pnl
        spent = float(sum(c.units * c.unit_cost for c in held))
        k = max(1, int(round(TAIL * paths)))
        worst_idx = np.argpartition(total, k - 1)[:k]
        rows = []
        index = {o["key"]: i for i, o in enumerate(opps)}
        for c in sorted(held, key=lambda c: -c.units * c.unit_cost):
            o = c.opp
            pnl = c.units * c.pnl
            dollars = c.units * c.unit_cost
            if c.leg is not None:
                opt = o["option"]
                label = (f"Buy {c.units} {o['symbol']} {opt['expiration']} ${opt['strike']:g} {c.instrument} · limit "
                         f"${opt['premium']:.2f}")
            else:
                label = f"{'Buy' if c.instrument == 'shares' else 'Sell short'} {c.units} {o['symbol']} · about ${o['entry']:,.2f}"
            rows.append({"key": o["key"], "symbol": o["symbol"], "direction": o["direction"], "instrument": c.instrument,
                         "units": int(c.units), "unit": "contracts" if c.leg else "shares", "unit_cost": c.unit_cost,
                         "dollars": dollars, "share": dollars / budget, "risk_at_stop": c.units * c.unit_risk,
                         "expected_pnl": float(pnl.mean()), "p_profit": float((pnl > 0).mean()),
                         "tail_contribution": float(pnl[worst_idx].mean()), "sector": o.get("sector"),
                         "entry": o["entry"], "stop": o["stop"], "target_1": o["target_1"], "target_2": o.get("target_2"),
                         "edge_pct": (o.get("edge") or {}).get("expected_pct"), "score": o.get("score"),
                         "setup_label": o.get("setup_label"), "source": o.get("source"), "label": label,
                         "option": o.get("option") if c.leg else None, "horizon": o.get("horizon"),
                         "horizon_days": o.get("horizon_days")})
        combined = None
        if held:
            q10, q50, q90 = np.percentile(total, [10, 50, 90])
            loses_all = sum(c.units * c.unit_cost for c in held if c.instrument in ("shares", "call", "put"))
            combined = {"expected_pnl": float(total.mean()), "pnl_p10": float(q10), "pnl_p50": float(q50),
                        "pnl_p90": float(q90), "p_profit": float((total > 0).mean()), "cvar10": cvar(total),
                        "worst": float(total.min()), "best": float(total.max()),
                        "return_on_budget": float(total.mean()) / budget, "tail_share": cvar(total) / budget,
                        "loss_if_every_stop": float(sum(c.units * c.unit_risk for c in held)),
                        "most_it_can_lose": None if any(c.instrument == "short" for c in held) else float(loses_all),
                        "unlimited_loss": any(c.instrument == "short" for c in held)}
        fan = self._fan(held, x, moments, exits, index, rate, cost_rate) if held else []
        syms = [c.opp["symbol"] for c in held]
        idx = [index[c.opp["key"]] for c in held]
        sectors: dict[str, float] = {}
        for c in held:
            sectors[c.opp.get("sector") or "Unknown"] = sectors.get(c.opp.get("sector") or "Unknown", 0.0) + c.units * c.unit_cost
        return {"budget": budget, "rows": rows, "spent": spent, "left": budget - spent, "combined": combined, "fan": fan,
                "correlation": {"symbols": syms, "matrix": [[float(corr[i, j]) for j in idx] for i in idx]},
                "sectors": [{"sector": k, "dollars": v, "share": v / budget} for k, v in sorted(sectors.items(), key=lambda kv: -kv[1])],
                "stress": self._stress(held, rets, rate) if held else [],
                "limits": {**lim, "open_used_by_blend": float(sum(c.units * c.unit_risk for c in held))}}

    def _fan(self, held: list[Candidate], x: np.ndarray, moments: list[datetime], exits: dict[str, Any],
             index: dict[str, int], rate: float, cost_rate: float) -> list[dict[str, Any]]:
        """The blend's profit or loss at each step: 10th, 25th, 50th, 75th and 90th percentile across paths."""
        steps = x.shape[0] - 1
        paths = x.shape[2]
        value = np.zeros((steps + 1, paths))
        for c in held:
            o = c.opp
            i = index[o["key"]]
            st, px, _ = exits[o["key"]]
            d = 1 if o["direction"] == "LONG" else -1
            final = c.units * c.pnl
            for k in range(1, steps + 1):
                open_ = st > k
                done = ~open_
                prices = np.exp(x[k, i, :])
                if c.leg is None:
                    now_pnl = c.units * (d * (prices - o["entry"]) - cost_rate * o["entry"])
                else:
                    now_pnl = c.units * (leg_value(c.leg, prices, moments[k], rate) - c.leg.entry) * MULTIPLIER
                value[k] += np.where(done, final, now_pnl)
        q = np.percentile(value, [10, 25, 50, 75, 90], axis=1)
        return [{"at": moments[k].isoformat(timespec="minutes"), "p10": float(q[0, k]), "p25": float(q[1, k]),
                 "p50": float(q[2, k]), "p75": float(q[3, k]), "p90": float(q[4, k])} for k in range(steps + 1)]

    def _stress(self, held: list[Candidate], rets: pd.DataFrame, rate: float) -> list[dict[str, Any]]:
        """The blend's result if the market moved at once by each shock: each stock by its beta to SPY times the
        shock (beta 1 without enough history), options repriced now at their own volatility."""
        betas = {}
        if "SPY" in rets.columns:
            m = rets["SPY"]
            for c in held:
                s = c.opp["symbol"]
                if s in rets.columns:
                    pair = pd.concat([rets[s], m], axis=1).dropna()
                    if len(pair) >= 60 and pair.iloc[:, 1].var() > 0:
                        betas[s] = float(pair.cov().iloc[0, 1] / pair.iloc[:, 1].var())
        now = datetime.now(NY)
        out = []
        for shock in STRESS_SHOCKS:
            pnl = 0.0
            for c in held:
                o = c.opp
                beta = betas.get(o["symbol"], 1.0)
                moved = o["entry"] * (1 + beta * shock / 100.0)
                if c.leg is None:
                    d = 1 if o["direction"] == "LONG" else -1
                    pnl += c.units * d * (moved - o["entry"])
                else:
                    pnl += c.units * (float(leg_value(c.leg, np.array([moved]), now, rate)[0]) - c.leg.entry) * MULTIPLIER
            out.append({"market_pct": shock, "pnl": pnl})
        return out


def keys_of(rows: list[dict[str, Any]]) -> dict[str, int]:
    """{"<key>|<instrument>": units} for a split's rows, the form `units` takes."""
    return {f"{r['key']}|{r['instrument']}": int(r["units"]) for r in rows}


__all__ = ["Blender", "BlendError", "day_grid", "swing_grid", "correlation_of", "nearest_correlation", "joint_log_paths",
           "walk_exits", "cvar", "keys_of", "PATHS", "SEED"]
