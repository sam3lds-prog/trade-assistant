"""The morning plan (2.8): big movers, what kind of trade each one supports, and reversal triggers.

**The idea, and the research behind it.** What a big one-day move does next depends on whether there was
news. After extreme moves *without* identifiable news, prices have tended to reverse; after moves *with*
news they have tended to keep drifting the same way (Chan 2003; Savor 2012 — both strongest in smaller
stocks, so the edge here, on large ones, is thinner). So each mover is checked for news (headlines when a
Finnhub key is set, SEC filings when EDGAR is on) and read as one of:

  RIDE the news    news found, a move of at least RIDE_MIN_RANGES daily ranges → trade with the move
  FADE the move    no news found, at least FADE_MIN_RANGES ranges, and overextended (RSI at an extreme,
                   stretched from the 20-day average, or a one-way run) → trade against the move
  no setup         everything else, with the reason. News that cannot be checked never counts as "no news".

A move is measured in the stock's own daily ranges (ATR), so a 4% day in a quiet stock counts for more
than 4% in a volatile one. Each setup carries its **past record**: how often the same price pattern on
these stocks over the last five years was followed by a move the trade's way, with a 95% range. History
cannot say which past moves had news, so the record conditions on the price pattern alone, and it is a
past frequency, never a forecast.

**Reversal watch** (the second strategy: wait for the turn, then jump) lists stocks whose fall or rise
shows signs of ending (reversal.py stages), each with the exact price that would confirm the turn. A
watch on that price is checked once a minute while the market is open and emails when it hits.

**The trade card** turns a setup into numbers: entry, stop and targets on the stock; a contract picked for
the risk budget; and the limit price to type, capped at the price where reaching target 1 only pays back
what the stop loses (settings.morning_min_payoff, default 1.0 — paying more than that cap means the trade
pays less than it risks even when it works). Option values at the target assume implied volatility settles
5 points for fades, where the move has usually pumped it. Everything here is advisory: nothing places an
order, and before the open (options trade 9:30–16:00 New York time) option prices are the previous close's.
"""

from __future__ import annotations

import hashlib
import json
import logging
import math
import threading
import time
from datetime import datetime, timedelta
from typing import Any, Callable

import numpy as np
import pandas as pd

from .background import OneJob
from .background import why as _why
from .ideas import SCAN_PACE, earnings_within
from .indicators import atr as atr_series
from .indicators import rsi as rsi_series
from .optionmath import NY
from .providers.base import DataError
from .scanner import TOP_200
from .sectors import TICKER_FUND
from .signalbook import wilson

log = logging.getLogger(__name__)

MOVE_MIN_RANGES = 2.0          # listed as a mover: at least this many daily ranges...
MOVE_MIN_PCT = 3.0             # ...and at least this percent, so quiet stocks need a real move too
RIDE_MIN_RANGES = 1.5          # with news, a smaller move already counts
FADE_MIN_RANGES = 2.5          # against the move only after a large one
STRETCH_ATR = 2.0              # overextended: this far from the 20-day average...
RSI_LOW, RSI_HIGH = 30.0, 70.0  # ...or RSI at an extreme...
RUN_DAYS = 3                   # ...or at least this many one-way days in a row
MAX_MOVERS = 14
MAX_CARDS = 8
MOVER_HORIZON = 5              # trading days a mover trade is given
REVERSAL_HORIZON = 15          # trading days a reversal trade is given
NEWS_HOURS = 36
FADE_STOP_ATR = 1.0            # a fade's stop: one daily range beyond the entry
RECORD_YEARS = 5
MIN_RECORD_CASES = 30
FADE_IV_SETTLE = -0.05         # option values at a fade's target assume implied volatility settles 5 points
WATCH_SECONDS = 60
MAX_WATCH_SYMBOLS = 40


def movers_universe(watchlist: list[str]) -> list[str]:
    """The stocks scanned for movers: the 200 largest plus every stock the sector lists name (about 330
    large, optionable US companies), plus the watchlist. A fixed list; small caps are not covered."""
    return sorted(set(TOP_200) | set(TICKER_FUND) | {s.strip().upper() for s in watchlist if s.strip()})


def tick_of(price: float) -> float:
    """The usual option price increment: a penny under $3, a nickel above. The Price screen works the exact
    increments out of the live chain; this is for a plan written before the open."""
    return 0.01 if price < 3 else 0.05


def floor_tick(price: float) -> float:
    t = tick_of(price)
    return max(t, math.floor(price / t + 1e-9) * t)


def limit_prices(*, mid: float, bid: float | None, value_at_target: float, value_at_stop: float,
                 min_payoff: float) -> dict[str, Any]:
    """The limit arithmetic. `value_at_target` and `value_at_stop` are the contract's per-share values on
    the horizon date with the stock at target 1 and at the stop. Paying P risks (P − value_at_stop) to make
    (value_at_target − P), so the **cap** is the price where that ratio equals `min_payoff`:
    P = (V_t + r·V_s) / (1 + r). Above the cap the trade pays less than `min_payoff` times what it risks
    even when the stock does what the plan says."""
    v_t, v_s = max(value_at_target, 0.0), max(value_at_stop, 0.0)
    r = max(min_payoff, 0.1)
    cap = (v_t + r * v_s) / (1 + r)
    if v_t - v_s <= 0.005:
        return {"viable": False, "cap": cap, "why": "Even free, the contract is worth no more at the target than "
                                                    "at the stop by the horizon, so no price makes this trade pay."}
    if cap <= 0.011:
        return {"viable": False, "cap": cap, "why": f"The price where target 1 pays {r:g} times what the stop loses "
                                                    "is under a cent, so no tradable price passes. The shares are "
                                                    "suggested instead."}
    suggested = floor_tick(min(mid, cap))
    patient = max(floor_tick(suggested - tick_of(suggested)), 0.01)
    payoff = (v_t - suggested) / max(suggested - v_s, 1e-9)
    out = {"viable": True, "fair": mid, "cap": floor_tick(cap), "suggested": suggested, "patient": patient,
           "payoff_at_suggested": payoff, "value_at_target": v_t, "value_at_stop": v_s, "min_payoff": r}
    if mid > cap:
        out["viable"] = False
        out["why"] = (f"The midpoint (${mid:,.2f}) is above the cap (${floor_tick(cap):,.2f}), the price where "
                      f"target 1 pays {r:g} times what the stop loses: at today's prices the option costs more "
                      "than the move pays. The shares are suggested instead.")
    if bid is not None and suggested < bid - 1e-9:
        out["below_bid"] = True
    return out


# --- moves, overextension and the past record ------------------------------------------------------------

def features_of(df: pd.DataFrame) -> dict[str, np.ndarray] | None:
    """Close, ATR(14), RSI(14), SMA20 and run length per bar of a finished-bars frame; None when too short."""
    if len(df) < 40:
        return None
    close = df["close"].to_numpy(dtype=float)
    atr = atr_series(df["high"], df["low"], df["close"]).to_numpy(dtype=float)
    rsi = rsi_series(df["close"]).to_numpy(dtype=float)
    sma20 = df["close"].rolling(20).mean().to_numpy(dtype=float)
    chg = np.sign(np.diff(close, prepend=close[0]))
    run = np.ones(len(close))
    for i in range(1, len(close)):
        run[i] = run[i - 1] + 1 if chg[i] == chg[i - 1] and chg[i] != 0 else 1
    return {"close": close, "atr": atr, "rsi": rsi, "sma20": sma20, "run": run * chg,
            "dates": df["date"].dt.strftime("%Y-%m-%d").to_numpy()}


def overextended(f: dict[str, np.ndarray], i: int, move_direction: int) -> tuple[bool, list[str]]:
    """Whether bar `i` looks overextended against a move in `move_direction` (+1 a spike, -1 a drop)."""
    why = []
    rsi, close, sma, atr, run = f["rsi"][i], f["close"][i], f["sma20"][i], f["atr"][i], f["run"][i]
    if np.isfinite(rsi) and (rsi <= RSI_LOW if move_direction < 0 else rsi >= RSI_HIGH):
        why.append(f"RSI {rsi:.0f}")
    if np.isfinite(sma) and np.isfinite(atr) and atr > 0 and move_direction * (close - sma) >= STRETCH_ATR * atr:
        why.append(f"{abs(close - sma) / atr:.1f} ranges {'above' if move_direction > 0 else 'under'} its 20-day average")
    if move_direction * run >= RUN_DAYS:
        why.append(f"{abs(run):.0f} {'up' if move_direction > 0 else 'down'} days in a row")
    return bool(why), why


def classify(*, ranges: float | None, move_pct: float, direction: int, news_state: str,
             overext: bool, overext_why: list[str]) -> dict[str, Any]:
    """The setup for one mover. `news_state` is "news", "no_news" or "unchecked"."""
    if ranges is None:
        return {"kind": None, "direction": None, "why": "The stock's daily range could not be measured."}
    if news_state == "news" and ranges >= RIDE_MIN_RANGES:
        word = "buy" if direction > 0 else "short"
        return {"kind": "ride", "direction": "LONG" if direction > 0 else "SHORT",
                "why": f"News behind a {ranges:.1f}-range move: moves with news have tended to keep going ({word})."}
    if news_state == "no_news" and ranges >= FADE_MIN_RANGES and overext:
        word = "buy" if direction < 0 else "short"
        return {"kind": "fade", "direction": "SHORT" if direction > 0 else "LONG",
                "why": f"No news found behind a {ranges:.1f}-range move, and it looks overextended "
                       f"({', '.join(overext_why)}): moves like this have tended to reverse ({word})."}
    if news_state == "unchecked":
        why = "News could not be checked (no Finnhub key and no SEC contact set), so neither setup applies."
    elif ranges < RIDE_MIN_RANGES:
        why = f"An ordinary move for this stock ({ranges:.1f} of its daily ranges)."
    elif news_state == "no_news" and ranges < FADE_MIN_RANGES:
        why = f"No news found, but at {ranges:.1f} ranges the move is not large enough to fade ({FADE_MIN_RANGES:g}+)."
    elif news_state == "no_news":
        why = "No news found, but the stock is not overextended (RSI, stretch and run are all ordinary)."
    else:
        why = "No setup."
    return {"kind": None, "direction": None, "why": why}


def past_record(frames: dict[str, dict[str, np.ndarray]], *, move_direction: int, min_ranges: float,
                need_overext: bool, trade_direction: int, horizons: tuple[int, ...] = (1, 3, 5)) -> dict[str, Any]:
    """Across all `frames` (five years of finished bars): after close-to-close moves of at least
    `min_ranges` daily ranges in `move_direction` (overextended on the day, when `need_overext`), how often
    the next `h` trading days went the trade's way, with a Wilson 95% range and the average move. Pure
    price history: whether those past moves had news is not knowable here."""
    out: dict[str, Any] = {"cases": 0, "horizons": {}, "years": RECORD_YEARS,
                           "what": f"moves of {min_ranges:g}+ daily ranges {'up' if move_direction > 0 else 'down'}"
                                   + (", already overextended before the move" if need_overext else "")}
    wins = {h: 0 for h in horizons}
    total = {h: 0 for h in horizons}
    moves = {h: [] for h in horizons}
    for f in frames.values():
        close, atr = f["close"], f["atr"]
        n = len(close)
        if n < 40:
            continue
        prev = close[:-1]
        day_move = np.diff(close)
        with np.errstate(invalid="ignore", divide="ignore"):
            in_ranges = day_move / atr[:-1]
            in_pct = 100 * day_move / prev
        for i in np.flatnonzero((move_direction * in_ranges >= min_ranges)
                                & (move_direction * in_pct >= MOVE_MIN_PCT) & np.isfinite(in_ranges)):
            t = i + 1                                   # the index of the big-move day's close
            # Overextension is read on the bar BEFORE the move, as the live pre-market reading has to read
            # it (the move's own bar is not finished yet), so the record counts the same pattern.
            if need_overext and not overextended(f, i, move_direction)[0]:
                continue
            for h in horizons:
                if t + h < n:
                    fwd = trade_direction * (close[t + h] / close[t] - 1)
                    total[h] += 1
                    wins[h] += fwd > 0
                    moves[h].append(fwd)
    for h in horizons:
        if total[h]:
            lo, hi = wilson(wins[h], total[h])
            out["horizons"][str(h)] = {"cases": total[h], "frequency": wins[h] / total[h],
                                       "low": lo, "high": hi, "mean_move_pct": 100 * float(np.mean(moves[h])),
                                       "enough": total[h] >= MIN_RECORD_CASES}
    out["cases"] = max(total.values(), default=0)
    return out


# --- the plan ---------------------------------------------------------------------------------------------

class Morning:
    """Builds the plan (in the background from the screen, inline from the 7:00 email run)."""

    def __init__(self, svc: Any, analyst: Any, features: dict[str, bool]):
        self.svc = svc
        self.analyst = analyst
        self.features = features
        self._result: dict[str, Any] | None = None
        self._loaded = False
        self._job = OneJob("morning-plan", self._keep)
        self._news = None

    # --- job plumbing (as the trade-ideas scan) ---------------------------------------------------------
    def _key(self) -> str:
        return f"morning:last:{self.svc.market.info.mode}"

    def _keep(self, result: dict[str, Any], job: dict[str, Any]) -> None:
        result["job_id"] = job["id"]
        try:
            from .service import clean
            self.svc.db.cache_put(self._key(), clean(result))
        except Exception as exc:                      # noqa: BLE001 - kept in memory even if it cannot be saved
            log.warning("Morning plan not saved: %s", exc)
        self._result = result

    def save_result(self, plan: dict[str, Any]) -> None:
        """Keep a plan built outside the job (the 7:00 email run), so the screen shows it too."""
        self._keep(dict(plan), {"id": 0})

    def start(self) -> dict[str, Any]:
        names = movers_universe(self.svc.settings.watchlist)

        def work(progress):
            with self.svc.market.paced(SCAN_PACE.get(self.svc.market.info.mode, 0.0)):
                return self.plan(progress=progress)
        out = self._job.start({}, len(names) + 60, work)
        return {**out, **self._result_line()}

    def stop(self) -> dict[str, Any]:
        return {**self._job.stop(), **self._result_line()}

    def _result_line(self) -> dict[str, Any]:
        r = self._result
        return {"result_at": r.get("checked_at") if r else None, "result_job": r.get("job_id") if r else None}

    def status(self, brief: bool = False) -> dict[str, Any]:
        if not self._loaded:
            self._loaded = True
            try:
                saved = self.svc.db.cache_get(self._key())
            except Exception:                         # noqa: BLE001 - a missing or damaged copy is just no result
                saved = None
            if self._result is None and saved:
                self._result = saved
        out = {**self._job.status_brief(), **self._result_line()}
        if not brief:
            out["result"] = self._result
        return out

    # --- data helpers -----------------------------------------------------------------------------------
    def news_client(self):
        if self._news is None:
            from .news import NewsClient
            self._news = getattr(self.svc, "news", None) or NewsClient(self.svc.settings, self.svc.db, self.svc.sec)
        return self._news

    def _quotes(self, names: list[str], progress=None, total: int = 0) -> dict[str, dict[str, Any]]:
        market, out = self.svc.market, {}
        self.last_quote_error: str | None = None
        for i in range(0, len(names), 50):
            if progress:
                progress(1 + i // 50, total, "quotes")
            chunk = names[i:i + 50]
            try:
                out.update(market._call("morning quotes", lambda c=chunk: market.provider.quotes(c)))
            except DataError as exc:
                log.info("Morning quotes failed for a chunk: %s", exc)
                self.last_quote_error = str(exc)
        return out

    def _move(self, q: dict[str, Any], f: dict[str, np.ndarray]) -> dict[str, Any] | None:
        """The move a quote shows against the last finished close: pre-market (or live) against yesterday,
        or, when the quote still sits on that close, yesterday's close against the day before."""
        close, dates = f["close"], f["dates"]
        last = q.get("last")
        if not last or last <= 0 or len(close) < 21:
            return None
        ref_i = len(close) - 1
        move_is = f"vs {dates[ref_i]}'s close"
        if abs(last - close[ref_i]) / close[ref_i] < 5e-4:      # the quote IS that close: show yesterday's move
            last = close[ref_i]
            ref_i -= 1
            move_is = f"{dates[ref_i + 1]} close-to-close"
        ref = close[ref_i]
        atr = f["atr"][ref_i]
        if not (ref > 0 and np.isfinite(atr) and atr > 0):
            return None
        move = last / ref - 1
        return {"price": float(last), "reference": float(ref), "move_pct": 100 * move, "move_is": move_is,
                "ranges": abs(last - ref) / atr, "atr": float(atr), "ref_index": ref_i}

    def _news_state(self, symbol: str) -> dict[str, Any]:
        """Whether identifiable news sits behind the move: headlines and filings in the last 36 hours."""
        client = self.news_client()
        demo = self.svc.market.info.mode == "demo"
        if demo:
            # Demo headlines exist for every stock; a deterministic split keeps both setups visible in demo.
            none = int(hashlib.sha256(f"{symbol}|{datetime.now(NY).date()}".encode()).hexdigest()[:4], 16) % 3 == 0
            if none:
                return {"state": "no_news", "label": "No news found (made up)", "items": []}
            return {"state": "news", "label": "Demo headline (made up)",
                    "items": [{"headline": f"{symbol} demo headline", "source": "Demo", "published": None, "url": None}]}
        if not client.configured and not (self.svc.sec and self.svc.sec.enabled):
            return {"state": "unchecked", "label": "News not checked (no Finnhub key, no SEC contact)", "items": []}
        try:
            got = client.company(symbol, days=3)
        except Exception as exc:                      # noqa: BLE001 - news failing must not sink the plan
            return {"state": "unchecked", "label": f"News check failed ({_why(exc)})", "items": []}
        fresh = []
        for it in got.get("items") or []:
            if _fresh(it.get("published")):
                fresh.append({"headline": it.get("headline"), "source": it.get("source"),
                              "published": it.get("published"), "url": it.get("url")})
        if got.get("errors") and not fresh:
            return {"state": "unchecked", "label": "News check failed; not treated as no news", "items": []}
        if not fresh:
            return {"state": "no_news", "label": f"No news found (last {NEWS_HOURS}h: "
                                                 f"{', '.join(got.get('sources') or []) or 'nothing checked'})",
                    "items": []}
        filings = [x for x in fresh if x["source"] == "SEC EDGAR"]
        label = filings[0]["headline"].split(":")[0] if filings else \
            f"{len(fresh)} headline{'s' if len(fresh) != 1 else ''}"
        return {"state": "news", "label": label, "items": fresh[:4]}

    def _frames(self, names: list[str], progress, done_base: int, total: int) -> dict[str, dict[str, np.ndarray]]:
        frames: dict[str, dict[str, np.ndarray]] = {}
        self._last_frame_error: str | None = None
        misses = 0
        for i, sym in enumerate(names):
            if progress:
                progress(done_base + i, total, sym)
            try:
                f = features_of(self.svc.market.bars(sym, RECORD_YEARS, complete=True))
            except Exception as exc:                  # noqa: BLE001 - one stock never stops the morning
                log.info("Morning: no bars for %s: %s", sym, exc)
                self._last_frame_error = _why(exc) if not isinstance(exc, DataError) else str(exc)
                f = None
            if f is not None:
                frames[sym] = f
            else:
                misses += 1
                if misses >= 15 and not frames:       # nothing read in 15 names: the source is down, stop grinding
                    log.warning("Morning: the first %d names all failed; the data source looks down", misses)
                    break
        return frames

    def _record(self, frames, *, move_direction: int, min_ranges: float, need_overext: bool,
                trade_direction: int) -> dict[str, Any]:
        key = (f"morning:record:{self.svc.market.info.mode}:{datetime.now(NY).date()}:{move_direction}:{min_ranges:g}:"
               f"{int(need_overext)}:{trade_direction}")
        hit = self.svc.db.cache_get(key, 20 * 3600)
        if hit is not None:
            return hit
        out = past_record(frames, move_direction=move_direction, min_ranges=min_ranges,
                          need_overext=need_overext, trade_direction=trade_direction)
        self.svc.db.cache_put(key, out)
        return out

    # --- the trade card ---------------------------------------------------------------------------------
    def card(self, *, symbol: str, direction: str, entry: float, stop: float, target_1: float,
             target_2: float, horizon_days: int, setup: str, levels_why: str = "") -> dict[str, Any]:
        """Entry, stop and targets on the stock; the contract for the risk budget; the limit to type."""
        from .options.evaluator import best_within_budget, evaluate, horizon_date, pick_expirations
        s = self.svc.settings
        d = 1 if direction == "LONG" else -1
        if not (entry > 0 and d * (entry - stop) > 0 and d * (target_1 - entry) > 0):
            raise ValueError("The stop must sit behind the entry and target 1 ahead of it")
        if d * (target_2 - target_1) <= 0:
            target_2 = target_1 + d * abs(target_1 - entry) * 0.5
        risk_ps = abs(entry - stop)
        budget = s.account_size * s.max_risk_per_trade_pct / 100.0
        shares = int(min(budget // risk_ps, s.account_size // entry)) if risk_ps > 0 else 0
        today = datetime.now(NY).date()
        h_day = horizon_date(today, horizon_days)
        card: dict[str, Any] = {
            "symbol": symbol, "setup": setup, "direction": direction,
            "side_word": "buy" if d > 0 else "short", "entry": entry, "stop": stop,
            "target_1": target_1, "target_2": target_2, "horizon_days": horizon_days,
            "horizon_date": h_day.isoformat(), "levels_why": levels_why, "risk_per_share": risk_ps,
            "shares": {"count": shares, "value": shares * entry,
                       "at_target_1": d * shares * (target_1 - entry) if shares else None,
                       "at_stop": -shares * risk_ps if shares else None,
                       "note": None if shares else "One share risks more than the per-trade limit."},
            "option": None, "warnings": [], "notes": []}
        if not shares:
            card["warnings"].append(f"One share of {symbol} risks ${risk_ps:,.2f} to the stop, more than the "
                                    f"${budget:,.0f} risk per trade: no share position fits the limit.")
        clock = self.svc.market.clock().get("state")
        card["price_basis"] = ("live" if clock == "open" else
                               "the previous close (options trade 9:30–16:00 New York time)")
        try:
            exps = pick_expirations(self.svc.market.expirations(symbol), today, h_day)
            if not exps:
                raise DataError(f"no {symbol} expiration leaves time after the horizon")
            analyses = [self.svc._chain_for(symbol, e) for e in exps]
            earnings = self.svc._earnings(symbol, None)
            opt = evaluate(symbol=symbol, direction=d, spot=entry, analyses=analyses, target_1=target_1,
                           target_2=target_2, invalidation=stop, horizon=horizon_days,
                           rate=s.risk_free_rate, contracts=1, earnings=earnings, entry=entry)
        except (DataError, ValueError) as exc:
            card["notes"].append(f"No contract: {exc}. The shares carry the plan.")
            card["order_text"] = self._order_text(card, None)
            return card
        if earnings_within((earnings or {}).get("date"), horizon_days, today):
            card["warnings"].append(f"Earnings on {earnings['date']}"
                                    + (" (estimated)" if earnings.get("estimated") else "")
                                    + " fall inside the horizon: a report can gap the stock past the stop.")
        row, category, why = best_within_budget(opt, budget)
        if row is None:
            card["notes"].append("No listed contract passed the checks (quotes, spread, open interest, expiring "
                                 "after the horizon, paying at target 1). The shares carry the plan.")
            card["order_text"] = self._order_text(card, None)
            return card
        shift = FADE_IV_SETTLE if setup == "fade" else 0.0
        v_t, v_s = _scenario_values(row, shift)
        limits = limit_prices(mid=float(row["mid"]), bid=row.get("bid"), value_at_target=v_t,
                              value_at_stop=v_s, min_payoff=s.morning_min_payoff)
        option = {"option_type": row["option_type"], "strike": row["strike"], "expiration": row["expiration"],
                  "dte": row["dte"], "bid": row.get("bid"), "ask": row.get("ask"), "mid": row.get("mid"),
                  "delta": row.get("delta"), "iv": row.get("iv"), "open_interest": row.get("open_interest"),
                  "spread_pct": row.get("spread_pct"), "category": category, "why": why,
                  "flags": row.get("flags") or [], "limits": limits,
                  "chance_profit_horizon": row.get("market_implied_profit_at_horizon"),
                  "iv_note": (f"Option values on the horizon date assume implied volatility settles "
                              f"{-100 * shift:g} points as the move calms." if shift else None)}
        if limits["viable"]:
            # At most 10 contracts: filling a big stack of cheap contracts at the suggested limit is not realistic.
            n = max(1, min(10, int(budget // (limits["suggested"] * 100))))
            cost = n * limits["suggested"] * 100
            per = limits["value_at_target"] - limits["suggested"]
            option.update({"contracts": n, "cost": cost,
                           "at_target_1": n * 100 * per,
                           "if_stopped": -n * 100 * (limits["suggested"] - limits["value_at_stop"]),
                           "max_loss": -cost})
            if cost > budget:
                card["warnings"].append(f"One contract costs ${cost:,.0f}, more than the ${budget:,.0f} risk per "
                                        "trade; all of it can be lost.")
            card["option"] = option
        else:
            card["option"] = option
            card["notes"].append(option["limits"].get("why", "The option costs more than the move pays."))
            card["notes"].append("The shares carry the plan instead.")
        card["order_text"] = self._order_text(card, option if limits["viable"] else None)
        return card

    @staticmethod
    def _order_text(card: dict[str, Any], option: dict[str, Any] | None) -> str:
        d = card["direction"]
        if option:
            kind = "Call" if option["option_type"].upper() == "CALL" else "Put"
            return (f"Buy {option['contracts']} {card['symbol']} {option['expiration']} "
                    f"${option['strike']:g} {kind} · Limit ${option['limits']['suggested']:.2f} · Good for day")
        n = card["shares"]["count"] or 1
        verb = "Buy" if d == "LONG" else "Sell short"
        return (f"{verb} {n} {card['symbol']} · Limit ${card['entry']:.2f} · stop ${card['stop']:.2f}, "
                f"target ${card['target_1']:.2f}")

    def _mover_setup(self, row: dict[str, Any], frames: dict[str, dict[str, np.ndarray]], d_move: int,
                     cards: list[dict[str, Any]]) -> None:
        """One mover's news, setup, past record and (up to MAX_CARDS) trade card, written onto `row`."""
        f = frames[row["symbol"]]
        # Overextension on the bar before the move: the reference bar (pre-market, that is the last finished
        # close; for a close-to-close move, the close before it), matching the past record's reading.
        over, over_why = overextended(f, row["ref_index"], d_move)
        news = self._news_state(row["symbol"])
        setup = classify(ranges=row["ranges"], move_pct=row["move_pct"], direction=d_move,
                         news_state=news["state"], overext=over, overext_why=over_why)
        listed = row["ranges"] >= (RIDE_MIN_RANGES if setup["kind"] == "ride" else MOVE_MIN_RANGES)
        row.update({"news": news, "setup": setup, "overextended": over, "overextended_why": over_why,
                    "record": None, "card_key": None, "listed": listed})
        if not setup["kind"]:
            return
        trade_d = 1 if setup["direction"] == "LONG" else -1
        row["record"] = self._record(frames, move_direction=d_move,
                                     min_ranges=FADE_MIN_RANGES if setup["kind"] == "fade" else RIDE_MIN_RANGES,
                                     need_overext=setup["kind"] == "fade", trade_direction=trade_d)
        if len(cards) >= MAX_CARDS:
            return
        entry = row["price"]
        atr = row["atr"]
        if setup["kind"] == "fade":
            stop = entry - trade_d * FADE_STOP_ATR * atr
            t1 = entry + 0.5 * (row["reference"] - entry)           # halfway back to where the move started
            t2 = row["reference"]
            why_lv = (f"Stop one daily range {'below' if trade_d > 0 else 'above'} the entry; target 1 halfway "
                      f"back to the pre-move close (${row['reference']:,.2f}), target 2 all the way.")
        else:
            try:
                from .evaluator import suggest_levels
                sug = suggest_levels(self.svc.market.enriched(row["symbol"], 3), trade_d, entry)
                stop, t1, t2 = sug["invalidation"], sug["target_1"], sug["target_2"]
                why_lv = " ".join(sug["notes"])
            except Exception as exc:                  # noqa: BLE001 - chart levels are a bonus
                stop = entry - trade_d * 1.2 * atr
                t1, t2 = entry + trade_d * 2.4 * atr, entry + trade_d * 3.6 * atr
                why_lv = f"Chart levels unavailable ({_why(exc)}); stop 1.2 daily ranges, targets 2 and 3 times the risk."
        try:
            card = self.card(symbol=row["symbol"], direction=setup["direction"], entry=entry, stop=stop,
                             target_1=t1, target_2=t2, horizon_days=MOVER_HORIZON, setup=setup["kind"],
                             levels_why=why_lv)
            card["key"] = f"{row['symbol']}:{setup['kind']}"
            card["record"] = row["record"]
            card["news"] = news
            cards.append(card)
            row["card_key"] = card["key"]
        except (DataError, ValueError) as exc:
            row["setup"] = {**setup, "why": setup["why"] + f" (No card: {_why(exc)}.)"}

    # --- the whole plan ---------------------------------------------------------------------------------
    def plan(self, progress: Callable[[int, int, str | None], None] | None = None) -> dict[str, Any]:
        started = time.time()
        s = self.svc.settings
        names = movers_universe(s.watchlist)
        total = len(names) + 60
        if progress:
            progress(0, total, None)
        notes: list[str] = []
        clock = self.svc.market.clock().get("state")
        regime_label = None
        try:
            regime_label = self.analyst.regime()["confirmed"]["label"]
        except (DataError, ValueError, KeyError) as exc:
            notes.append(f"The market regime could not be read ({exc}).")
        quotes = self._quotes(names + ["SPY"], progress, total)
        if not quotes:
            # The data source is down for everything (an expired sign-in, no network). Grinding through the
            # bars of ~290 names would take minutes and end in a plan that reads "a quiet open" — the unknown
            # shown as calm. Fail now, with the source's own message, which says what to do.
            raise DataError(f"No quotes could be read, so there is no plan. {self.last_quote_error or ''}".strip())
        frames = self._frames(names, progress, 8, total)
        if not frames:
            raise DataError("Quotes were read but no price history could be, so there is no plan. "
                            "The last error: " + (self._last_frame_error or "none recorded"))
        spy = None
        try:
            spy_f = features_of(self.svc.market.bars("SPY", 1.0, complete=True))
            if spy_f is not None and quotes.get("SPY"):
                spy = self._move(quotes["SPY"], spy_f)
        except Exception as exc:                      # noqa: BLE001
            log.info("Morning: no SPY line: %s", exc)

        # 1. The movers.
        movers = []
        for sym in names:
            f, q = frames.get(sym), quotes.get(sym)
            if f is None or q is None:
                continue
            m = self._move(q, f)
            if m is None:
                continue
            if abs(m["move_pct"]) >= MOVE_MIN_PCT and m["ranges"] >= MOVE_MIN_RANGES * 0.75:
                movers.append({"symbol": sym, **m})
        movers.sort(key=lambda r: -r["ranges"])
        movers = movers[:MAX_MOVERS]
        done = 8 + len(names)

        # 2. News, setups, records and cards for each mover.
        cards: list[dict[str, Any]] = []
        for k, row in enumerate(movers):
            if progress:
                progress(done + k, total, row["symbol"])
            d_move = 1 if row["move_pct"] > 0 else -1
            try:
                self._mover_setup(row, frames, d_move, cards)
            except Exception as exc:                  # noqa: BLE001 - one stock never stops the morning
                log.warning("Morning: %s could not be read: %r", row["symbol"], exc)
                row.update({"news": {"state": "unchecked", "label": f"Not read ({_why(exc)})", "items": []},
                            "setup": {"kind": None, "direction": None, "why": f"Not read: {_why(exc)}."},
                            "overextended": False, "overextended_why": [], "record": None, "card_key": None,
                            "listed": row["ranges"] >= MOVE_MIN_RANGES})
        movers = [r for r in movers if r.get("listed", True)]

        # 3. Reversal watch: the watchlist plus today's movers.
        reversal_rows: list[dict[str, Any]] = []
        if self.features.get("reversal_analysis"):
            watch_names = list(dict.fromkeys([*s.watchlist, *[r["symbol"] for r in movers]]))[:MAX_WATCH_SYMBOLS]
            for k, sym in enumerate(watch_names):
                if progress:
                    progress(done + len(movers) + k, total, sym)
                try:
                    r = self.analyst.reversal(sym, history=False)
                except (DataError, ValueError) as exc:
                    log.info("Morning: no reversal reading for %s: %s", sym, exc)
                    continue
                day = r["confirmed"]
                for side in ("bottom", "top"):
                    pick = day[side]
                    if pick["stage"] not in ("CANDIDATE", "CONFIRMED"):
                        continue
                    lv = pick["levels"]
                    trigger = lv.get("confirms_above") if side == "bottom" else lv.get("confirms_below")
                    reversal_rows.append({
                        "symbol": sym, "side": side, "stage": pick["stage"], "score": pick["score"],
                        "direction": "LONG" if side == "bottom" else "SHORT",
                        "headline": pick.get("text") or day.get("headline"),
                        "close": lv["close"], "trigger": trigger,
                        "trigger_kind": "above" if side == "bottom" else "below",
                        "invalidation": lv["invalidation"], "target_1": lv["target_1"],
                        "target_2": lv.get("target_2"), "since": pick.get("since"),
                        "evidence": [e["text"] for e in (pick.get("evidence") or [])[:4]],
                        "confirmed": pick["stage"] == "CONFIRMED"})
            reversal_rows.sort(key=lambda x: (x["stage"] != "CONFIRMED", -x["score"]))
            for row in reversal_rows:
                if row["confirmed"] and len(cards) < MAX_CARDS + 2:
                    entry = quotes.get(row["symbol"], {}).get("last") or row["close"]
                    try:
                        card = self.card(symbol=row["symbol"], direction=row["direction"], entry=float(entry),
                                         stop=row["invalidation"], target_1=row["target_1"],
                                         target_2=row.get("target_2") or row["target_1"],
                                         horizon_days=REVERSAL_HORIZON, setup="reversal",
                                         levels_why="Levels from the reversal reading (Analyze a stock has the detail).")
                        card["key"] = f"{row['symbol']}:reversal:{row['side']}"
                        cards.append(card)
                        row["card_key"] = card["key"]
                    except Exception as exc:          # noqa: BLE001 - one stock never stops the morning
                        log.warning("Morning: no reversal card for %s: %r", row["symbol"], exc)
                        row["note"] = f"No card: {_why(exc)}."
        else:
            notes.append("Reversal analysis is switched off, so the reversal watch is empty.")

        if self.svc.market.info.mode == "demo":
            notes.insert(0, "Demo prices and headlines are synthetic: this plan says nothing about real stocks.")
        basis = ("Live prices." if clock == "open" else
                 "Stock prices are pre-market; option prices are the previous close's (options trade 9:30–16:00 "
                 "New York time)." if clock == "premarket" else
                 "The market is closed: prices are the last session's.")
        return {"checked_at": datetime.now(NY).isoformat(timespec="seconds"), "day": datetime.now(NY).date().isoformat(),
                "seconds": round(time.time() - started, 1), "clock": clock, "price_basis": basis,
                "market": {"regime": regime_label,
                           "spy_move_pct": spy["move_pct"] if spy else None,
                           "spy_move_is": spy["move_is"] if spy else None},
                "universe_size": len(names), "read": len(frames), "movers": movers, "cards": cards,
                "reversal": reversal_rows, "notes": notes, "data_source": self.svc.market.info.mode,
                "min_payoff": s.morning_min_payoff,
                "rule": "A mover with news is read to keep going; a big mover with no news found, already "
                        "overextended, is read to snap back. Past records count what followed the same price "
                        "pattern on these stocks over five years: frequencies, not forecasts. Nothing here "
                        "places an order."}


def _fresh(when: Any, hours: int = NEWS_HOURS, now: datetime | None = None) -> bool:
    """Whether a published time falls in the last `hours`. SEC filing dates carry no time of day, so a
    date-only value counts by its New York date; anything unreadable counts as not fresh, never an error."""
    if not when:
        return False
    now = now or datetime.now(NY)
    try:
        parsed = datetime.fromisoformat(str(when).replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return False
    if parsed.tzinfo is None:
        if (parsed.hour, parsed.minute, parsed.second) == (0, 0, 0):   # a bare filing date, no time of day
            return (now.date() - parsed.date()).days * 24 <= hours
        parsed = parsed.replace(tzinfo=NY)                             # a naive time is read as New York time
    return parsed >= now - timedelta(hours=hours)


def _scenario_values(row: dict[str, Any], shift: float) -> tuple[float, float]:
    """The contract's per-share value at target 1 and at the stop, from its scenario matrix at `shift`
    implied-volatility change: the horizon-date value at the target, and the worse of the horizon and
    halfway values at the stop (as Decide counts the loss)."""
    mid = float(row["mid"])
    rows = row.get("scenarios") or []
    at = {(m["when"], round(m["iv_shift"], 2)): m for m in rows}
    base = at.get(("horizon", round(shift, 2)))
    soon = at.get(("halfway", round(shift, 2)))
    if base is None:
        return mid + float(row.get("at_target") or 0.0) / 100, max(0.0, mid + float(row.get("if_invalidated") or 0.0) / 100)
    v_t = mid + float(base["target_1"]) / 100
    loss = min(float(base["invalidation"]), float(soon["invalidation"]) if soon else float(base["invalidation"]))
    return v_t, max(0.0, mid + loss / 100)


# --- picks and the scorecard ------------------------------------------------------------------------------

def record_picks(db: Any, plan: dict[str, Any], source: str) -> int:
    """Keep today's cards as picks, one per (day, symbol, setup), so the scorecard can grade them later."""
    n = 0
    with db.tx() as cur:
        for c in plan.get("cards") or []:
            option = c.get("option")
            viable = bool(option and (option.get("limits") or {}).get("viable"))
            cur.execute("""INSERT INTO morning_picks (day, symbol, setup, direction, entry, stop, target_1, target_2,
                           horizon_days, instrument, option_json, limit_price, contracts, shares, created_at, source)
                           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                           ON CONFLICT(day, symbol, setup) DO NOTHING""",
                        (plan["day"], c["symbol"], c["setup"], c["direction"], c["entry"], c["stop"], c["target_1"],
                         c["target_2"], c["horizon_days"],
                         (option["option_type"] if viable else "SHARES"),
                         json.dumps({k: option.get(k) for k in ("option_type", "strike", "expiration")}) if viable else None,
                         (option["limits"]["suggested"] if viable else None),
                         (option.get("contracts") if viable else None), c["shares"]["count"],
                         datetime.now(NY).isoformat(timespec="seconds"), source))
            n += cur.rowcount
    return n


def scorecard(svc: Any, days_back: int = 7) -> dict[str, Any]:
    """How the stock plan of each recent pick has gone: the entry against the close 1, 3 and 5 trading days
    on, the trade's way. Option picks are graded by their stock plan too — no source has past option
    prices — and the note says so."""
    since = (datetime.now(NY).date() - timedelta(days=days_back + 3)).isoformat()
    rows = svc.db.query("SELECT * FROM morning_picks WHERE day >= ? ORDER BY day DESC, symbol", (since,))
    out, profitable_3d, graded_3d = [], 0, 0
    for r in rows:
        d = 1 if r["direction"] == "LONG" else -1
        entry = float(r["entry"])
        item = {"day": r["day"], "symbol": r["symbol"], "setup": r["setup"], "direction": r["direction"],
                "instrument": r["instrument"], "entry": entry, "stop": r["stop"], "target_1": r["target_1"],
                "after": {}}
        try:
            df = svc.market.bars(r["symbol"], 0.3, complete=True)
            closes = df[df["date"] >= pd.Timestamp(r["day"])]["close"].to_numpy(dtype=float)
        except Exception:                             # noqa: BLE001 - no bars: the pick stays pending
            closes = np.array([])
        for h in (1, 3, 5):
            if len(closes) >= h:
                item["after"][str(h)] = {"close": float(closes[h - 1]),
                                         "move_pct": 100 * d * (closes[h - 1] / entry - 1)}
        if "3" in item["after"]:
            graded_3d += 1
            profitable_3d += item["after"]["3"]["move_pct"] > 0
        out.append(item)
    return {"picks": out, "graded_3d": graded_3d, "profitable_3d": profitable_3d,
            "note": "Each pick is graded by its stock plan: the entry against the next closes, the trade's way, "
                    "counting the pick day's own close as the first. Option picks are graded the same way — no "
                    "connected source has past option prices."}


# --- watches ----------------------------------------------------------------------------------------------

def add_watch(db: Any, *, symbol: str, direction: str, kind: str, level: float, why: str) -> int:
    with db.tx() as cur:
        cur.execute("INSERT INTO morning_watches (symbol, direction, kind, level, why, created_at) "
                    "VALUES (?, ?, ?, ?, ?, ?)",
                    (symbol.upper(), direction, kind, level, why[:300], datetime.now(NY).isoformat(timespec="seconds")))
        return int(cur.lastrowid)


def remove_watch(db: Any, watch_id: int) -> None:
    with db.tx() as cur:
        cur.execute("UPDATE morning_watches SET active = 0 WHERE id = ?", (watch_id,))


def list_watches(db: Any, include_triggered_days: int = 3) -> list[dict[str, Any]]:
    since = (datetime.now(NY).date() - timedelta(days=include_triggered_days)).isoformat()
    rows = db.query("SELECT * FROM morning_watches WHERE active = 1 OR (triggered_at IS NOT NULL AND "
                    "triggered_at >= ?) ORDER BY id DESC", (since,))
    return [dict(r) for r in rows]


class WatchChecker:
    """Once a minute while the market is open (or pre-market, for the stock price), checks the active
    watches against live quotes; a trigger marks the row, and emails when email is set up. Started only
    when a watch exists, so the app carries no idle thread."""

    def __init__(self, svc: Any, morning: Morning):
        self.svc = svc
        self.morning = morning
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()
        self.stop = threading.Event()

    def ensure_running(self) -> None:
        with self._lock:
            if self._thread is None or not self._thread.is_alive():
                self.stop = threading.Event()
                self._thread = threading.Thread(target=self._run, name="morning-watches", daemon=True)
                self._thread.start()

    def _run(self) -> None:
        while True:
            try:
                if not self.check():
                    with self._lock:
                        # A watch created after check() looked would be orphaned: look once more before ending.
                        if not any(w["active"] for w in list_watches(self.svc.db)):
                            self._thread = None
                            return
            except Exception:                          # noqa: BLE001 - the checker must survive anything
                log.exception("Watch check failed")
            if self.stop.wait(WATCH_SECONDS):
                return

    def check(self) -> bool:
        """One pass. Returns False when there is nothing left to watch."""
        active = [w for w in list_watches(self.svc.db) if w["active"]]
        if not active:
            return False
        state = self.svc.market.clock().get("state")
        if state not in ("open", "premarket"):
            if state == "unknown":
                log.info("Watches not checked: the market clock is unavailable")
            return True
        syms = sorted({w["symbol"] for w in active})
        quotes: dict[str, Any] = {}
        for i in range(0, len(syms), 50):
            chunk = syms[i:i + 50]
            quotes.update(self.svc.market._call("watch quotes", lambda c=chunk: self.svc.market.provider.quotes(c)))
        for w in active:
            q = quotes.get(w["symbol"]) or {}
            last = q.get("last")
            if not last:
                continue
            hit = last >= w["level"] if w["kind"] == "above" else last <= w["level"]
            if not hit:
                continue
            with self.svc.db.tx() as cur:
                cur.execute("UPDATE morning_watches SET active = 0, triggered_at = ?, triggered_price = ? "
                            "WHERE id = ? AND active = 1",
                            (datetime.now(NY).isoformat(timespec="seconds"), float(last), w["id"]))
                claimed = cur.rowcount
            if claimed:
                self._announce(w, float(last))
        return True

    def _announce(self, w: dict[str, Any], price: float) -> None:
        from . import mailer
        subject = f"Triggered: {w['symbol']} {w['kind']} ${w['level']:,.2f} (now ${price:,.2f})"
        body = (f"{w['symbol']} traded {w['kind']} ${w['level']:,.2f} at ${price:,.2f}.\n\n"
                f"Why it was watched: {w['why']}\n\n"
                f"Open the Morning plan screen for the trade card at live prices. Nothing has been ordered.")
        if not mailer.configured(self.svc.settings):
            log.info("Watch %s triggered; email is not set up, so it shows on the screen only", w["id"])
            return
        try:
            mailer.send(self.svc.settings, subject, body)
            with self.svc.db.tx() as cur:
                cur.execute("UPDATE morning_watches SET emailed = 1 WHERE id = ?", (w["id"],))
        except mailer.MailError as exc:               # its message is already scrubbed and says what to fix
            log.warning("Watch email failed: %s", str(exc)[:300])
        except Exception as exc:                      # noqa: BLE001 - a failed email never kills the checker
            log.warning("Watch email failed: %s", _why(exc))


__all__ = ["Morning", "WatchChecker", "movers_universe", "classify", "overextended", "past_record",
           "limit_prices", "features_of", "record_picks", "scorecard", "add_watch", "remove_watch",
           "list_watches", "tick_of", "floor_tick"]
