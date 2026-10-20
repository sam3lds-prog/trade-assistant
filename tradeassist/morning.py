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
from .optionmath import NY, black76_price
from .pricer import age_seconds, fair_value
from .providers.base import DataError
from .scanner import TOP_200, TOP_250  # noqa: F401 - TOP_200 is part of this module's surface (tests read it)
from .sectors import SECTOR_FUNDS, TICKER_FUND
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
MOVER_HORIZON = 5              # trading days a mover trade is given
REVERSAL_HORIZON = 15          # trading days a reversal trade is given
NEWS_HOURS = 36
FADE_STOP_ATR = 1.0            # a fade's stop: one daily range beyond the entry
RECORD_YEARS = 5
MIN_RECORD_CASES = 30
FADE_IV_SETTLE = -0.05         # option values at a fade's target assume implied volatility settles 5 points
WATCH_SECONDS = 60
MAX_WATCH_SYMBOLS = 40
PREOPEN_MOVE = 0.0025          # 2.10: the stock moved more than this since the close, so last session's option
                               # quote is for another stock price and the contract is repriced instead
OPEN_STATES = ("open",)
PRE_STATES = ("premarket", "pre")   # Tradier says "premarket", Robinhood's clock "pre"
POST_STATES = ("postmarket", "post")


MORNING_CARDS = 20             # 2.15: trade cards for the best-scored opportunities
CARD_SECONDS = 15 * 60         # a card is rebuilt after this long (the morning session's rescans reuse younger ones)
QUIET_RANGES = 0.5             # a move under this many daily ranges is not an opportunity
NEWS_CHECKS = 20               # news is looked up for the biggest movers, this many at most
SESSION_RECORD_NAMES = 30      # stocks whose 5-minute bars feed the rest-of-the-day record
SESSION_RECORD_DAYS = 30
RUN_SAVE_SECONDS = 30 * 60     # a rescan's opportunities are kept for replay at most this often
RUN_TOP = 40                   # ...the best this many of them
CONTEXT_US = {"SPY": "S&P 500 (SPY)", "QQQ": "Nasdaq 100 (QQQ)", "IWM": "Russell 2000 (IWM)", "DIA": "Dow (DIA)"}
# Overseas markets before New York opens: the real index from stooq's free quotes (move from its own open) when it
# answers, else the US-listed fund that tracks it (its move from yesterday's close, pre-market trading included).
OVERSEAS = [
    ("japan", "Japan (Nikkei 225)", "^nkx", "EWJ", "Asia"),
    ("hong_kong", "Hong Kong (Hang Seng)", "^hsi", "EWH", "Asia"),
    ("china", "China (Shanghai)", "^shc", "ASHR", "Asia"),
    ("korea", "Korea (KOSPI)", "^kospi", "EWY", "Asia"),
    ("australia", "Australia (All Ordinaries)", "^aor", "EWA", "Asia"),
    ("germany", "Germany (DAX)", "^dax", "EWG", "Europe"),
    ("uk", "UK (FTSE 100)", "^ukx", "EWU", "Europe"),
    ("france", "France (CAC 40)", "^cac", "EWQ", "Europe"),
    ("euro_area", "Euro area (Euro Stoxx 50)", None, "FEZ", "Europe"),
]


def movers_universe(watchlist: list[str]) -> list[str]:
    """The stocks the Morning plan reads (2.15): the 250 largest US companies (a fixed list, close to the largest
    by market value in mid-2026) plus the watchlist. Small caps are not covered."""
    return sorted(set(TOP_250) | {s.strip().upper() for s in watchlist if s.strip()})


def live_universe(watchlist: list[str]) -> list[str]:
    """The stocks Live news trades: the 250 largest plus every stock the sector lists name (about 340 large,
    optionable US companies), plus the watchlist."""
    return sorted(set(TOP_250) | set(TICKER_FUND) | {s.strip().upper() for s in watchlist if s.strip()})


def tick_of(price: float) -> float:
    """The usual option price increment: a penny under $3, a nickel above. The Price screen works the exact
    increments out of the live chain; this is for a plan written before the open."""
    return 0.01 if price < 3 else 0.05


def floor_tick(price: float) -> float:
    t = tick_of(price)
    return max(t, math.floor(price / t + 1e-9) * t)


def _floor_valid(price: float, rule: Any = None) -> float:
    """The highest valid option price at or below `price`: on the chain's own increments when known (2.10),
    else the usual penny-under-$3, nickel-above rule. Never below one increment."""
    if rule is None:
        return floor_tick(price)
    c = int(math.floor(price * 100 + 1e-6))
    c -= c % rule.tick_at(c)
    return max(c, rule.tick_at(1)) / 100


def _step_down(price: float, rule: Any = None) -> float:
    """The next valid price below `price` (one increment down), never below one increment."""
    if rule is None:
        return max(floor_tick(price - tick_of(price)), 0.01)
    return _floor_valid(price - 0.005, rule)


def limit_prices(*, mid: float, bid: float | None, value_at_target: float, value_at_stop: float,
                 min_payoff: float, fair: float | None = None, fair_basis: str | None = None,
                 rule: Any = None) -> dict[str, Any]:
    """The limit arithmetic. `value_at_target` and `value_at_stop` are the contract's per-share values on
    the horizon date with the stock at target 1 and at the stop. Paying P risks (P − value_at_stop) to make
    (value_at_target − P), so the **cap** is the price where that ratio equals `min_payoff`:
    P = (V_t + r·V_s) / (1 + r). Above the cap the trade pays less than `min_payoff` times what it risks
    even when the stock does what the plan says.

    2.10: the suggested price starts from `fair` (entry_estimate: the fair price from the quote's evidence,
    or before the open the contract repriced at the stock's current price), not from the midpoint, and it
    sits on the chain's own price increments when `rule` (a pricer.TickRule) is given."""
    price = fair if fair is not None and fair > 0 else mid
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
    suggested = _floor_valid(min(price, cap), rule)
    patient = _step_down(suggested, rule)
    payoff = (v_t - suggested) / max(suggested - v_s, 1e-9)
    out = {"viable": True, "fair": price, "mid": mid, "fair_basis": fair_basis or "The midpoint.",
           "cap": _floor_valid(cap, rule), "suggested": suggested, "patient": patient,
           "payoff_at_suggested": payoff, "value_at_target": v_t, "value_at_stop": v_s, "min_payoff": r}
    if price > cap:
        out["viable"] = False
        out["why"] = (f"The fair price (${price:,.2f}) is above the cap (${_floor_valid(cap, rule):,.2f}), the price "
                      f"where target 1 pays {r:g} times what the stop loses: at today's prices the option costs more "
                      "than the move pays. The shares are suggested instead.")
    if bid is not None and suggested < bid - 1e-9:
        out["below_bid"] = True
    return out


def last_session_date(now: datetime | None = None) -> Any:
    """The date of the regular session whose closing quotes an option chain shows outside regular hours: today
    from 4 p.m. New York time on a weekday, else the weekday before (market holidays are not known here)."""
    now = (now or datetime.now(NY)).astimezone(NY)
    day = now.date()
    if now.weekday() < 5 and (now.hour, now.minute) >= (16, 0):
        return day
    day -= timedelta(days=1)
    while day.weekday() >= 5:
        day -= timedelta(days=1)
    return day


def entry_estimate(row: dict[str, Any], analyses: list[Any], entry: float, clock: str,
                   ref_close: float | None, rule: Any = None) -> dict[str, Any]:
    """2.10: what to pay now for a card's contract, with how it was worked out. `entry` is the stock's price
    now (not a planned entry: the limit is typed now, against today's stock). `ref_close` is the close of the
    session the option quotes come from, or None when it could not be confirmed (Morning.card checks its date).

    - While options trade: the fair price from the quote's evidence (pricer.fair_value: midpoint, model value,
      size-weighted midpoint, a recent trade), never above the midpoint.
    - Outside regular hours the quotes are that session's closing quotes. When the stock has moved since, the
      quote is for another stock price, so the contract is repriced: Black-76 at the volatility its own last
      midpoint implies against the forward at that close, with the forward moved by entry / close (sticky
      strike). Before 2.10 the card used the old midpoint, which for a fade (puts after a gap up, calls after
      a gap down) priced the contract well above where it opens.
    - When the close or the clock cannot be read, nothing is repriced and the basis says so (`unchecked`)."""
    from .optionmath import implied_vol
    from .pricer import to_cents
    mid = float(row["mid"])
    a = next((x for x in analyses if getattr(x, "expiration", None) == row.get("expiration")), None)
    c = a.find(row["option_symbol"]) if a is not None and row.get("option_symbol") else None
    out: dict[str, Any] = {"price": mid, "mid": mid, "repriced": False, "unchecked": True, "evidence": [],
                           "basis": "The midpoint (the contract's chain row was not found)."}
    if a is None or c is None or not (a.spot and a.spot > 0 and a.forward and a.forward > 0 and entry > 0):
        return out
    tick = rule.tick_at(to_cents(mid)) if rule is not None else 1
    strike, kind = float(c["strike"]), c["option_type"]

    def from_quote(model_value: Any, live: bool, note: str, unchecked: bool) -> dict[str, Any]:
        f = fair_value(c.get("bid"), c.get("ask"), "buy", model_value=model_value, bidsize=c.get("bidsize"),
                       asksize=c.get("asksize"), last=c.get("last"), last_age_seconds=age_seconds(c.get("trade_time")),
                       quote_live=live, fill_prices=c.get("fill_prices"), tick_cents=tick)
        if f is None:
            return out
        out.update({"price": f["anchor"], "basis": f["basis"] + note, "evidence": f["evidence"], "unchecked": unchecked})
        return out

    vol = c.get("model_vol") or c.get("iv")
    if clock in OPEN_STATES:
        model_now = (black76_price(a.forward * entry / a.spot, strike, a.t, a.rate, float(vol), kind)
                     if vol and vol > 0 and a.t > 0 else None)
        return from_quote(model_now, True, "", False)
    if clock not in (*PRE_STATES, *POST_STATES, "closed"):
        return from_quote(c.get("model_value"), False, " The market clock could not be read, so whether these quotes "
                                                       "are live is unknown: check them against Robinhood.", True)
    if not ref_close or ref_close <= 0:
        return from_quote(c.get("model_value"), False, " Option quotes are the last session's, and the close they were "
                                                       "made at could not be confirmed, so any move since is not allowed "
                                                       "for: price it live after 9:30.", True)
    moved = entry / ref_close - 1.0
    if abs(moved) < PREOPEN_MOVE:
        return from_quote(c.get("model_value"), False, " Quotes are the last session's; the stock has not moved enough "
                                                       "since to reprice them.", False)
    # The forward at that close: parity on those very quotes, or the carry forward rebuilt from the close.
    f_ref = a.forward if a.forward_method == "parity" else a.forward * ref_close / a.spot
    own = implied_vol(mid, f_ref, strike, a.t, a.rate, kind) if a.t > 0 else None
    vol_used = own or vol
    if not vol_used or vol_used <= 0 or a.t <= 0:
        return from_quote(c.get("model_value"), False, " The stock has moved since the close but the contract's "
                                                       "volatility is unknown, so it is not repriced: price it live "
                                                       "after 9:30.", True)
    price = float(black76_price(f_ref * entry / ref_close, strike, a.t, a.rate, float(vol_used), kind))
    out.update({"price": price, "repriced": True, "unchecked": False, "basis": (
        f"Option quotes are the last session's, made with the stock at ${ref_close:,.2f}; it is now ${entry:,.2f} "
        f"({100 * moved:+.1f}%). The contract is repriced at that stock price with "
        f"{'the volatility its last midpoint implies' if own else 'the chain model volatility'} "
        f"({100 * float(vol_used):.0f}%): the midpoint of ${mid:,.2f} is for the old price. Implied volatility often "
        "jumps at the open after a gap, which makes the contract dearer than this: price it live after 9:30 before you "
        "trust the limit."),
        "evidence": [{"key": "midpoint", "label": "Last session's midpoint", "price": mid, "used": False,
                      "note": "Quoted when the stock was at its close; not used."},
                     {"key": "repriced", "label": "Repriced at the stock's price now", "price": price, "used": True,
                      "note": "Black-76, the forward moved with the stock, at the contract's own volatility."}]})
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
    # 2.15: the open and the volume too, for the day-trade record (open to close after a gap) and volume against
    # normal. A frame without them (older tests build close-only frames) gets the close and no volume.
    open_ = df["open"].to_numpy(dtype=float) if "open" in df else close.copy()
    volume = df["volume"].to_numpy(dtype=float) if "volume" in df else np.full(len(close), np.nan)
    return {"close": close, "atr": atr, "rsi": rsi, "sma20": sma20, "run": run * chg, "open": open_,
            "volume": volume, "dates": df["date"].dt.strftime("%Y-%m-%d").to_numpy()}


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
        why = ("News could not be checked or ruled out (no source set up, the check failed, or headlines came back "
               "that could not be matched to the stock), so neither setup applies.")
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
        self._cards: dict[tuple, tuple[float, dict[str, Any]]] = {}   # 2.15: recent cards, reused by rescans
        self._run_saved_at = 0.0
        self._intraday = None
        from .opportunities import Runs
        self.runs = Runs(svc.db)

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

    def start(self, rescan: bool = False) -> dict[str, Any]:
        """Build the plan in the background. A `rescan` (the morning session's refresh) keeps the reversal watch of
        today's plan instead of reading it again, and saves its opportunities for replay at most every half hour."""
        names = movers_universe(self.svc.settings.watchlist)

        def work(progress):
            with self.svc.market.paced(SCAN_PACE.get(self.svc.market.info.mode, 0.0)):
                return self.plan(progress=progress, rescan=rescan)
        out = self._job.start({"rescan": rescan}, len(names) + 60, work)
        return {**out, **self._result_line()}

    @property
    def running(self) -> bool:
        return (self._job.status_brief().get("job") or {}).get("state") == "running"

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
            from .searchnews import for_service
            self._news = getattr(self.svc, "news", None) or NewsClient(self.svc.settings, self.svc.db, self.svc.sec,
                                                                       search=for_service(self.svc))
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
        if not client.configured and not (self.svc.sec and self.svc.sec.enabled) and not client.searchable:
            return {"state": "unchecked", "label": "News not checked (no Finnhub key, no SEC contact, search off)",
                    "items": []}
        try:
            got = client.company(symbol, days=3, deep=True, fresh_hours=NEWS_HOURS)
        except Exception as exc:                      # noqa: BLE001 - news failing must not sink the plan
            return {"state": "unchecked", "label": f"News check failed ({_why(exc)})", "items": []}
        fresh = []
        for it in got.get("items") or []:
            if _fresh(it.get("published")):
                fresh.append({"headline": it.get("headline"), "source": it.get("source"),
                              "published": it.get("published"), "url": it.get("url")})
        if got.get("errors") and not fresh:
            return {"state": "unchecked", "label": "News check failed; not treated as no news", "items": []}
        if not fresh and got.get("unmatched"):
            # 2.11: a search returned recent headlines that could not be tied to this stock (a name that is also
            # a word, a brand the name list does not know). That is not "no news", so the fade does not apply.
            n, moves = int(got["unmatched"]), int(got.get("move_only") or 0)
            if moves == n:
                label = (f"News not ruled out: the only recent headline{'s' if n != 1 else ''} about {symbol} "
                         f"{'say' if n != 1 else 'says'} that it moved, not why; read {'them' if n != 1 else 'it'} "
                         "before fading")
            else:
                label = (f"News not ruled out: the search found {n} recent headline{'s' if n != 1 else ''} that may "
                         f"be about {symbol} but could not be tied to it; read them before fading")
            return {"state": "unchecked", "items": [], "label": label}
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
             target_2: float, horizon_days: int, setup: str, levels_why: str = "",
             day_trade: bool = False) -> dict[str, Any]:
        """Entry, stop and targets on the stock; the contract for the risk budget; the limit to type.

        2.15: a `day_trade` card is out by 15:55 the same session (today's, or the next one's outside trading
        hours). Its contract is still one that expires at least a week later (the contract checks), so it is sold
        with time value left; the contract's figures at the target are at the next close, a little below what it
        is worth earlier in the day."""
        from . import marketdays
        from .options.evaluator import best_within_budget, evaluate, pick_expirations
        s = self.svc.settings
        d = 1 if direction == "LONG" else -1
        if not (entry > 0 and d * (entry - stop) > 0 and d * (target_1 - entry) > 0):
            raise ValueError("The stop must sit behind the entry and target 1 ahead of it")
        if d * (target_2 - target_1) <= 0:
            target_2 = target_1 + d * abs(target_1 - entry) * 0.5
        if min(stop, target_1, target_2) <= 0:
            # A short's targets two or three times a very wide risk can fall below zero (a reversal read on a long
            # slide); no price gets there, and the contract's valuation would take the log of a negative price.
            raise ValueError("A target or the stop falls at or below zero: the risk is too wide for a card")
        risk_ps = abs(entry - stop)
        budget = s.account_size * s.max_risk_per_trade_pct / 100.0
        shares = int(min(budget // risk_ps, s.account_size // entry)) if risk_ps > 0 else 0
        now_ny = datetime.now(NY)
        today = now_ny.date()
        if day_trade:
            horizon_days = 1
            sess = marketdays.session(today)
            h_day = today if sess and now_ny < sess[1] else marketdays.next_trading_day(today)
        else:
            # The holidays known (2.15): the same close paper trading and the blend use (marketdays.horizon_close).
            h_day = marketdays.horizon_close(now_ny, horizon_days).date()
        card: dict[str, Any] = {
            "symbol": symbol, "setup": setup, "direction": direction,
            "side_word": "buy" if d > 0 else "short", "entry": entry, "stop": stop,
            "target_1": target_1, "target_2": target_2, "horizon_days": horizon_days, "day_trade": bool(day_trade),
            "horizon_date": h_day.isoformat(), "levels_why": levels_why, "risk_per_share": risk_ps,
            "shares": {"count": shares, "value": shares * entry,
                       "at_target_1": d * shares * (target_1 - entry) if shares else None,
                       "at_stop": -shares * risk_ps if shares else None,
                       "note": None if shares else "One share risks more than the per-trade limit."},
            "option": None, "warnings": [], "notes": []}
        if not shares:
            card["warnings"].append(f"One share of {symbol} risks ${risk_ps:,.2f} to the stop, more than the "
                                    f"${budget:,.0f} risk per trade: no share position fits the limit.")
        if day_trade:
            card["notes"].append(f"A day trade: out by 15:55 New York time on {h_day:%a %b %d} whatever happens. A "
                                 "contract's figures at the target are at the next close, a little under what it is "
                                 "worth earlier in the day.")
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
                           rate=s.risk_free_rate, contracts=1, earnings=earnings, entry=entry,
                           quote_live=clock in OPEN_STATES)
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
        ref_close = None
        if clock not in OPEN_STATES:
            # The close the option quotes belong to, only when the saved history reaches that very session (a
            # failed refresh or the minutes before a bar is final leave it unconfirmed, and nothing is repriced).
            try:
                done = self.svc.market.bars(symbol, 0.3, complete=True)
                if done["date"].iloc[-1].date() == last_session_date():
                    ref_close = float(done["close"].iloc[-1])
            except Exception:                         # noqa: BLE001 - unconfirmed: the estimate says so
                ref_close = None
        a_row = next((x for x in analyses if x.expiration == row.get("expiration")), None)
        rule = None
        if a_row is not None:
            from .pricer import tick_rule
            rule = tick_rule([q for k in a_row.contracts for q in (k.get("bid"), k.get("ask"))],
                             next((k.get("min_ticks") for k in a_row.contracts if k.get("min_ticks")), None))
        # The stock's price now, which is not always the card's entry: a thesis on Decide can plan an entry
        # away from today's price, but the limit is typed today.
        stock_now = entry
        if setup in ("analysis", "manual"):
            try:
                last = self.svc.market.quote(symbol).get("last")
                stock_now = float(last) if last and last > 0 else entry
            except DataError:
                stock_now = entry
        est = entry_estimate(row, analyses, stock_now, clock or "unknown", ref_close, rule)
        limits = limit_prices(mid=float(row["mid"]), bid=row.get("bid") if clock in OPEN_STATES else None,
                              value_at_target=v_t, value_at_stop=v_s, min_payoff=s.morning_min_payoff,
                              fair=est["price"], fair_basis=est["basis"], rule=rule)
        limits["fair_evidence"] = est["evidence"]
        limits["repriced"] = est["repriced"]
        limits["unchecked"] = est["unchecked"]
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

    def _mover_setup(self, row: dict[str, Any], frames: dict[str, dict[str, np.ndarray]], d_move: int) -> None:
        """One mover's news, setup and past record, written onto `row`. (2.15: its trade card is built later, with
        the other best-scored opportunities.)"""
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

    # --- 2.15: the markets before the open, and every stock as a scored opportunity ---------------------
    def _stooq_client(self):
        if getattr(self, "_stooq", None) is None:
            from .factors import Stooq
            # Real indexes only with real data: demo data is made up, and the tests must not reach the network.
            self._stooq = Stooq(enabled=bool(getattr(self.svc.settings, "stooq_quotes", True))
                                and self.svc.market.info.mode != "demo")
        return self._stooq

    def _context(self, quotes: dict[str, dict[str, Any]]) -> dict[str, Any]:
        """Overseas markets, the US index funds and the sector funds, each with its move and where it was read."""
        from .factors import pct_move
        st = self._stooq_client()
        stooq_rows = st.quotes([o[2] for o in OVERSEAS if o[2]]) if st.enabled else {}
        overseas = []
        for key, label, code, proxy, region in OVERSEAS:
            move, source = None, "no reading"
            s = stooq_rows.get(code) if code else None
            if s and s.get("close") and s.get("open"):
                move = 100.0 * (s["close"] / s["open"] - 1.0)
                source = f"stooq {code}, from its own open ({s.get('date')} {s.get('time')})"
            elif proxy in quotes:
                m, how = pct_move(quotes[proxy])
                if m is not None:
                    move, source = m, f"{proxy}, the US-listed fund that tracks it ({how}, against yesterday's close)"
            overseas.append({"key": key, "label": label, "region": region, "move_pct": move, "source": source,
                             "proxy": proxy})

        def avg(region: str) -> float | None:
            v = [o["move_pct"] for o in overseas if o["region"] == region and o["move_pct"] is not None]
            return float(np.mean(v)) if v else None
        us = [{"symbol": sym, "label": label, "move_pct": pct_move(quotes[sym])[0] if sym in quotes else None}
              for sym, label in CONTEXT_US.items()]
        sectors = [{"fund": fund, "label": SECTOR_FUNDS[fund], "move_pct": pct_move(quotes[fund])[0] if fund in quotes else None}
                   for fund in SECTOR_FUNDS]
        return {"overseas": overseas, "asia_avg": avg("Asia"), "europe_avg": avg("Europe"), "us": us, "sectors": sectors,
                "stooq": {"enabled": st.enabled, "last_error": st.last_error},
                "note": "Context, not a signal on its own: the move overseas is already in the US funds' prices before the "
                        "open, so each stock's own part of its move is measured against SPY (its beta times SPY's move)."}

    @staticmethod
    def _beta(f: dict[str, np.ndarray], spy_f: dict[str, np.ndarray] | None) -> float | None:
        """The stock's daily return per 1% of SPY's over the last year of finished bars they share."""
        if spy_f is None:
            return None
        both = pd.concat([pd.Series(f["close"], index=f["dates"]), pd.Series(spy_f["close"], index=spy_f["dates"])],
                         axis=1, join="inner").tail(251)
        if len(both) < 61:
            return None
        r = np.log(both.astype(float)).diff().dropna()
        v = float(r.iloc[:, 1].var())
        return float(r.cov().iloc[0, 1] / v) if v > 0 else None

    def _rows(self, names: list[str], quotes: dict[str, dict[str, Any]], frames: dict[str, dict[str, np.ndarray]],
              spy_f: dict[str, np.ndarray] | None, spy_move: float | None, sector_moves: dict[str, float | None],
              phase: str) -> list[dict[str, Any]]:
        """One reading per stock: the move since the last close (in percent and daily ranges), its own part beyond
        what SPY's move explains, the sector fund's move, volume against normal, and since the open (in the session)."""
        from .opportunities import realized_vol
        rows = []
        for sym in names:
            f, q = frames.get(sym), quotes.get(sym)
            if f is None or q is None:
                continue
            m = self._move(q, f)
            if m is None:
                continue
            row = {"symbol": sym, **m, "atr_pct": 100.0 * m["atr"] / m["reference"],
                   "signed_ranges": m["ranges"] if m["move_pct"] >= 0 else -m["ranges"]}
            beta = self._beta(f, spy_f)
            row["beta"] = beta
            row["market_part_pct"] = row["own_pct"] = row["own_ranges"] = None
            if beta is not None and spy_move is not None:
                row["market_part_pct"] = beta * spy_move
                row["own_pct"] = m["move_pct"] - row["market_part_pct"]
                row["own_ranges"] = row["own_pct"] / row["atr_pct"] if row["atr_pct"] > 0 else None
            fund = TICKER_FUND.get(sym)
            row["sector"], row["sector_move_pct"] = fund, sector_moves.get(fund) if fund else None
            vol, avg = q.get("volume"), q.get("average_volume")
            row["volume_ratio"] = float(vol) / float(avg) if vol and avg and avg > 0 else None
            row["open"] = row["since_open_pct"] = row["since_open_ranges"] = row["gap_ranges"] = None
            if phase == "session" and m["move_is"].startswith("vs"):
                op = q.get("open")
                if op and op > 0:
                    row["open"] = float(op)
                    row["since_open_pct"] = 100.0 * (m["price"] / op - 1.0)
                    row["since_open_ranges"] = (m["price"] - op) / m["atr"]
                    row["gap_ranges"] = (op - m["reference"]) / m["atr"]
            row["vol"] = realized_vol(f["close"])
            rows.append(row)
        return rows

    def _intraday_store(self):
        if self._intraday is None:
            cv = getattr(self.svc, "chart_view", None)
            if cv is not None and getattr(cv, "intraday", None) is not None:
                self._intraday = cv.intraday
            else:
                from .intraday import Intraday
                self._intraday = Intraday(self.svc.market, self.svc.db)
        return self._intraday

    def _session_bars(self, symbols: list[str]) -> dict[str, pd.DataFrame]:
        store, out = self._intraday_store(), {}
        for sym in symbols:
            try:
                df = store.bars(sym, "5m", SESSION_RECORD_DAYS)
            except Exception as exc:                  # noqa: BLE001 - a stock without bars is left out of the record
                log.info("Morning: no 5-minute bars for %s: %s", sym, exc)
                continue
            if df is not None and not df.empty:
                out[sym] = df
        return out

    @staticmethod
    def _session_extras(df: pd.DataFrame, today: Any, minute: int) -> dict[str, Any]:
        """Today's VWAP and opening range (the first 30 minutes) from 5-minute bars, and where the price is
        against them."""
        from .opportunities import session_frame
        s = session_frame(df)
        s = s[s["day"] == today]
        if s.empty:
            return {}
        typ = (s["high"] + s["low"] + s["close"]) / 3.0
        vol = s["volume"].astype(float)
        last = float(s["close"].iloc[-1])
        out: dict[str, Any] = {"vwap": float((typ * vol).sum() / vol.sum()) if vol.sum() > 0 else None}
        out["above_vwap"] = (last > out["vwap"]) if out["vwap"] else None
        first = s[s["minute"] <= 30]
        if not first.empty and minute > 30:
            hi, lo = float(first["high"].max()), float(first["low"].min())
            out.update({"or_high": hi, "or_low": lo,
                        "breakout": "up" if last > hi else "down" if last < lo else None})
        return out

    def _gap_record(self, frames, *, move_d: int, min_r: float, fade: bool, trade_d: int) -> dict[str, Any]:
        from .opportunities import gap_record
        key = (f"morning:gap:{self.svc.market.info.mode}:{datetime.now(NY).date()}:{move_d}:{min_r:g}:{int(fade)}:"
               f"{trade_d}:{len(frames)}")
        hit = self.svc.db.cache_get(key, 20 * 3600)
        if hit is not None:
            return hit
        out = gap_record(frames, gap_direction=move_d, min_ranges=min_r, trade_direction=trade_d, need_overext=fade)
        self.svc.db.cache_put(key, out)
        return out

    def _edge(self, row: dict[str, Any], kind: str, trade_d: int, *, phase: str, horizon: str, frames, cases,
              minute: int) -> dict[str, Any]:
        """The trade's edge from the record that fits when it would be entered (opportunities.py explains the pull
        toward zero). Day trades: before the open, the gap's open-to-close record; in the session, the rest-of-the-day
        record after a swing of this size by this time; after the close, the next day's close-to-close record.
        Swing trades: the close-to-close record MOVER_HORIZON days on."""
        from .opportunities import cost_pct, edge_from
        from .opportunities import intraday_record_from as from_cases
        cpct = cost_pct(self.svc.settings)
        move_d = 1 if row["move_pct"] >= 0 else -1
        bucket = lambda v, hi: min(hi, max(0.5, math.floor(abs(v) * 2) / 2))      # noqa: E731
        if horizon == "swing" or phase == "closed":
            h = str(MOVER_HORIZON) if horizon == "swing" else "1"
            if row["ranges"] < 1.0 or abs(row["move_pct"]) < MOVE_MIN_PCT:
                return edge_from(None, 0, cost_pct=cpct, basis="The move is too small for the close-to-close record "
                                                               f"(it counts moves of {MOVE_MIN_PCT:g}% and a daily range or more)")
            rec = self._record(frames, move_direction=move_d, min_ranges=bucket(row["ranges"], 3.0), need_overext=kind == "fade",
                               trade_direction=trade_d)
            r = (rec.get("horizons") or {}).get(h) or {}
            span = f"{MOVER_HORIZON} trading days on" if horizon == "swing" else "the next day, close to close"
            return edge_from(r.get("mean_move_pct"), r.get("cases"), cost_pct=cpct, frequency=r.get("frequency"),
                             low=r.get("low"), high=r.get("high"),
                             basis=f"Five years of {len(frames)} large stocks: after {rec['what']}, the move {span}, the trade's way")
        so = row.get("since_open_ranges")
        if phase == "session" and so is not None and cases is not None and len(cases["move"]):
            sd = 1 if so >= 0 else -1
            rec = from_cases(cases, min_ranges=bucket(so, 2.5), move_direction=sd, trade_direction=trade_d)
            return edge_from(rec.get("mean_move_pct"), rec.get("cases"), cost_pct=cpct, frequency=rec.get("frequency"),
                             low=rec.get("low"), high=rec.get("high"),
                             basis=f"5-minute bars of {cases['stocks']} of today's movers over the last {SESSION_RECORD_DAYS} "
                                   f"days: after {bucket(so, 2.5):g}+ daily ranges {'up' if sd > 0 else 'down'} from the open "
                                   f"{cases['minute']} minutes in, the rest of the day, the trade's way")
        if row["ranges"] < QUIET_RANGES:
            return edge_from(None, 0, cost_pct=cpct, basis="Too small a move to have a record")
        rec = self._gap_record(frames, move_d=move_d, min_r=bucket(row["ranges"], 3.0), fade=kind == "fade", trade_d=trade_d)
        late = " (the record runs from the open; it is later than that now)" if phase == "session" else ""
        return edge_from(rec.get("mean_move_pct"), rec.get("cases"), cost_pct=cpct, frequency=rec.get("frequency"),
                         low=rec.get("low"), high=rec.get("high"),
                         basis=f"Five years of {len(frames)} large stocks: after {rec['what']}, the trade's way{late}")

    def _setup_and_edge(self, row: dict[str, Any], *, phase: str, horizon: str, frames, cases,
                        minute: int) -> tuple[str, int, str, dict[str, Any]]:
        """The kind of trade, its direction, its label and its edge. A checked mover keeps its news setup (ride or
        fade); otherwise the direction is whichever way the record favours, with the move or against it."""
        move_d = 1 if row["move_pct"] >= 0 else -1
        st = row.get("setup") or {}
        if st.get("kind"):
            d = 1 if st["direction"] == "LONG" else -1
            label = {"ride": "Ride the news", "fade": "Fade the move"}[st["kind"]]
            return st["kind"], d, label, self._edge(row, st["kind"], d, phase=phase, horizon=horizon, frames=frames,
                                                    cases=cases, minute=minute)
        so = row.get("since_open_ranges")
        swing = phase == "session" and so is not None and abs(so) >= 1.0
        quiet = row["ranges"] < QUIET_RANGES and not (so is not None and abs(so) >= QUIET_RANGES)
        if quiet:
            from .opportunities import cost_pct, edge_from
            return "quiet", move_d, "Quiet", edge_from(None, 0, cost_pct=cost_pct(self.svc.settings),
                                                       basis="An ordinary move for this stock: no trade")
        kind = "swing" if swing else "gap"
        with_d = (1 if so >= 0 else -1) if swing else move_d
        e_with = self._edge(row, kind, with_d, phase=phase, horizon=horizon, frames=frames, cases=cases, minute=minute)
        e_against = self._edge(row, kind, -with_d, phase=phase, horizon=horizon, frames=frames, cases=cases, minute=minute)
        checked = " (news not checked)" if not row.get("news") else ""
        what = "morning swing" if swing else "move"
        # The two records are mirror images (the same cases, opposite signs), so taking whichever looks better would
        # always pick the luckier side of the noise. The fade is chosen only when its record wins the trade's way
        # more often than not with 95% confidence (the Wilson interval's low end above 50%); otherwise the trade
        # rides the move, with whatever edge (often none after costs) that record shows.
        if e_against["known"] and e_against["expected_pct"] > e_with["expected_pct"] and (e_against.get("low") or 0.0) > 0.5:
            return kind, -with_d, f"Fade the {what}{checked}", e_against
        return kind, with_d, f"Ride the {what}{checked}", e_with

    @staticmethod
    def _score(row: dict[str, Any], kind: str, trade_d: int, edge: dict[str, Any], phase: str,
               minute: int) -> list[dict[str, Any]]:
        """The points behind an opportunity's score (0 to 100)."""
        from .opportunities import part
        parts = []
        so = row.get("since_open_ranges")
        if phase == "session" and so is not None:
            parts.append(part("swing", "Swing since the open", min(abs(so) / 3.0, 1.0) * 25, 25,
                              f"{so:+.1f} daily ranges since the open"))
            parts.append(part("move", "Move since the close", min(row["ranges"] / 4.0, 1.0) * 10, 10,
                              f"{row['move_pct']:+.1f}%, {row['ranges']:.1f} daily ranges"))
        else:
            parts.append(part("move", "Move since the close", min(row["ranges"] / 4.0, 1.0) * 35, 35,
                              f"{row['move_pct']:+.1f}%, {row['ranges']:.1f} daily ranges ({row['move_is']})"))
        own = row.get("own_ranges")
        if own is not None:
            parts.append(part("own", "Its own move", min(abs(own) / 3.0, 1.0) * 15, 15,
                              f"{row['own_pct']:+.1f}% beyond what SPY's move explains ({abs(own):.1f} ranges)"))
        vr = row.get("volume_ratio")
        if phase == "session" and vr is not None:
            frac = max(minute / 390.0, 0.1)
            pace = vr / frac
            parts.append(part("volume", "Volume", float(np.clip((pace - 1.0) / 2.0, 0, 1)) * 10, 10,
                              f"trading at {pace:.1f}× its normal pace for this time of day"))
        news = (row.get("news") or {}).get("state")
        setup_pts = 15 if kind in ("ride", "fade") else 10 if kind == "swing" else 5 if news == "unchecked" else 0
        parts.append(part("setup", "Setup", setup_pts, 15,
                          {"ride": "news behind the move", "fade": "no news and overextended", "swing": "a sharp morning swing"}
                          .get(kind, "news could not be checked" if news == "unchecked" else "no clear setup")))
        if edge["known"]:
            parts.append(part("edge", "Past record", float(np.clip(edge["expected_pct"] / 0.5, -1, 1)) * 25, 25,
                              f"{edge['expected_pct']:+.2f}% expected after costs and the pull toward zero "
                              f"({edge['cases']} cases)"))
        else:
            parts.append(part("edge", "Past record", 0, 25, "no record"))
        ext = row.get("extras") or {}
        if ext.get("breakout") and (ext["breakout"] == "up") == (trade_d > 0):
            parts.append(part("levels", "Opening range", 5, 5, f"broke the opening range {ext['breakout']}"))
        elif ext.get("above_vwap") is not None and ext["above_vwap"] == (trade_d > 0):
            parts.append(part("levels", "VWAP", 3, 5, f"{'above' if ext['above_vwap'] else 'below'} VWAP, the trade's way"))
        return parts

    def _levels(self, row: dict[str, Any], trade_d: int, kind: str, horizon: str,
                refine: bool = False) -> tuple[float, float, float, str]:
        from .opportunities import day_levels
        entry, atr = row["price"], row["atr"]
        if horizon == "day":
            return day_levels(entry=entry, atr=atr, direction=trade_d, setup=kind, reference=row["reference"])
        if kind == "fade":
            stop = entry - trade_d * FADE_STOP_ATR * atr
            return (stop, entry + 0.5 * (row["reference"] - entry), row["reference"],
                    f"Stop one daily range {'below' if trade_d > 0 else 'above'} the entry; target 1 halfway back to the "
                    f"pre-move close (${row['reference']:,.2f}), target 2 all the way.")
        if refine:
            try:
                from .evaluator import suggest_levels
                sug = suggest_levels(self.svc.market.enriched(row["symbol"], 3), trade_d, entry)
                return sug["invalidation"], sug["target_1"], sug["target_2"], " ".join(sug["notes"])
            except Exception as exc:                  # noqa: BLE001 - chart levels are a bonus
                log.info("Morning: no chart levels for %s: %s", row["symbol"], exc)
        return (entry - trade_d * 1.2 * atr, entry + trade_d * 2.4 * atr, entry + trade_d * 3.6 * atr,
                "Stop 1.2 daily ranges from the entry, targets 2 and 3 times the risk.")

    def _card_for(self, opp: dict[str, Any], row: dict[str, Any], horizon: str) -> dict[str, Any] | None:
        """The trade card for an opportunity, reused when one for the same trade was built in the last
        CARD_SECONDS (the morning session's rescans)."""
        key = (opp["symbol"], opp["direction"], opp["setup"], horizon, round(opp["entry"] / max(row["atr"], 1e-9) * 4))
        now = time.time()
        hit = self._cards.get(key)
        if hit and now - hit[0] < CARD_SECONDS:
            return dict(hit[1])
        stop, t1, t2, why = (self._levels(row, 1 if opp["direction"] == "LONG" else -1, opp["setup"], horizon, refine=True)
                             if horizon == "swing" else (opp["stop"], opp["target_1"], opp["target_2"], opp.get("levels_why", "")))
        try:
            card = self.card(symbol=opp["symbol"], direction=opp["direction"], entry=opp["entry"], stop=stop, target_1=t1,
                             target_2=t2, horizon_days=MOVER_HORIZON if horizon == "swing" else 1,
                             setup=opp["setup"] if opp["setup"] in ("ride", "fade") else ("ride" if opp["setup"] != "fade" else "fade"),
                             levels_why=why, day_trade=horizon == "day")
        except (DataError, ValueError) as exc:
            log.info("Morning: no card for %s: %s", opp["symbol"], exc)
            return None
        self._cards[key] = (now, card)
        if len(self._cards) > 400:                     # forget the oldest
            for k in sorted(self._cards, key=lambda k: self._cards[k][0])[:100]:
                self._cards.pop(k, None)
        return dict(card)

    def _opportunities(self, rows: list[dict[str, Any]], *, phase: str, horizon: str, frames, cases,
                       minute: int) -> list[dict[str, Any]]:
        from .opportunities import make, total_score
        out = []
        day = datetime.now(NY).date().isoformat()
        for row in rows:
            try:
                kind, d, label, edge = self._setup_and_edge(row, phase=phase, horizon=horizon, frames=frames, cases=cases,
                                                            minute=minute)
                parts = self._score(row, kind, d, edge, phase, minute)
                stop, t1, t2, why = self._levels(row, d, kind, horizon)
                news = row.get("news") or {"state": "not_checked", "label": "Not checked (outside the biggest movers)", "items": []}
                opp = make(source="morning", key=f"morning:{day}:{row['symbol']}:{kind}:{'L' if d > 0 else 'S'}",
                           symbol=row["symbol"], direction="LONG" if d > 0 else "SHORT", setup=kind, setup_label=label,
                           horizon=horizon, horizon_days=1 if horizon == "day" else MOVER_HORIZON, entry=row["price"],
                           stop=stop, target_1=t1, target_2=t2, atr=row["atr"], vol=row.get("vol"),
                           score=total_score(parts) if kind != "quiet" else min(total_score(parts), 10.0),
                           score_parts=parts, edge=edge,
                           why=[x for x in [(row.get("setup") or {}).get("why"),
                                            ", ".join(row.get("overextended_why") or []) or None] if x],
                           price=row["price"], reference=row["reference"], move_pct=row["move_pct"], move_is=row["move_is"],
                           ranges=row["ranges"], signed_ranges=row["signed_ranges"], since_open_pct=row.get("since_open_pct"),
                           since_open_ranges=row.get("since_open_ranges"), gap_ranges=row.get("gap_ranges"),
                           own_pct=row.get("own_pct"), own_ranges=row.get("own_ranges"), beta=row.get("beta"),
                           volume_ratio=row.get("volume_ratio"), sector_move_pct=row.get("sector_move_pct"),
                           news={"state": news.get("state"), "label": news.get("label"), "items": (news.get("items") or [])[:3]},
                           extras=row.get("extras") or {}, levels_why=why, tradable=kind != "quiet", card_key=None)
            except (ValueError, KeyError, TypeError) as exc:
                log.info("Morning: %s is not an opportunity: %s", row.get("symbol"), exc)
                continue
            out.append(opp)
        out.sort(key=lambda o: (-o["score"], o["symbol"]))
        for i, o in enumerate(out, 1):
            o["rank"] = i
        return out

    # --- the whole plan ---------------------------------------------------------------------------------
    def plan(self, progress: Callable[[int, int, str | None], None] | None = None, rescan: bool = False) -> dict[str, Any]:
        from .opportunities import atr_by_day_from_frame, intraday_cases, load_prefs, option_of
        started = time.time()
        s = self.svc.settings
        prefs = load_prefs(self.svc.db, s)["morning"]
        horizon = prefs["horizon"]
        names = movers_universe(s.watchlist)
        total = len(names) + 60
        if progress:
            progress(0, total, None)
        notes: list[str] = []
        clock = self.svc.market.clock().get("state")
        phase = "session" if clock in OPEN_STATES else "pre" if clock in PRE_STATES else "closed"
        now_ny = datetime.now(NY)
        minute = max(5, min(390, int((now_ny - now_ny.replace(hour=9, minute=30, second=0, microsecond=0)).total_seconds() // 60)))
        regime_label = None
        try:
            regime_label = self.analyst.regime()["confirmed"]["label"]
        except (DataError, ValueError, KeyError) as exc:
            notes.append(f"The market regime could not be read ({exc}).")
        extra = list(CONTEXT_US) + list(SECTOR_FUNDS) + [o[3] for o in OVERSEAS]
        quotes = self._quotes(list(dict.fromkeys(names + extra)), progress, total)
        if not any(sym in quotes for sym in names):
            # The data source is down for everything (an expired sign-in, no network). Grinding through the
            # bars of 250 names would take minutes and end in a plan that reads "a quiet open" — the unknown
            # shown as calm. Fail now, with the source's own message, which says what to do.
            raise DataError(f"No quotes could be read, so there is no plan. {self.last_quote_error or ''}".strip())
        frames = self._frames(names, progress, 8, total)
        if not frames:
            raise DataError("Quotes were read but no price history could be, so there is no plan. "
                            "The last error: " + (self._last_frame_error or "none recorded"))
        spy = spy_f = None
        try:
            spy_f = features_of(self.svc.market.bars("SPY", 1.0, complete=True))
            if spy_f is not None and quotes.get("SPY"):
                spy = self._move(quotes["SPY"], spy_f)
        except Exception as exc:                      # noqa: BLE001
            log.info("Morning: no SPY line: %s", exc)
        context = self._context(quotes)
        sector_moves = {x["fund"]: x["move_pct"] for x in context["sectors"]}

        # 1. Every stock's reading, and the movers among them.
        rows = self._rows(names, quotes, frames, spy_f, spy["move_pct"] if spy else None, sector_moves, phase)
        movers = [r for r in rows if abs(r["move_pct"]) >= MOVE_MIN_PCT and r["ranges"] >= MOVE_MIN_RANGES * 0.75]
        movers.sort(key=lambda r: -r["ranges"])
        movers = movers[:MAX_MOVERS]
        done = 8 + len(names)

        # 2. News, setups and records for the biggest movers (and, in the session, the sharpest morning swings).
        checked = list(movers)
        if phase == "session":
            swings = sorted((r for r in rows if r.get("since_open_ranges") is not None and r not in checked),
                            key=lambda r: -abs(r["since_open_ranges"]))
            checked += [r for r in swings if abs(r["since_open_ranges"]) >= 1.0][:max(0, NEWS_CHECKS - len(checked))]
        for k, row in enumerate(checked):
            if progress:
                progress(done + k, total, row["symbol"])
            d_move = 1 if row["move_pct"] > 0 else -1
            try:
                self._mover_setup(row, frames, d_move)
            except Exception as exc:                  # noqa: BLE001 - one stock never stops the morning
                log.warning("Morning: %s could not be read: %r", row["symbol"], exc)
                row.update({"news": {"state": "unchecked", "label": f"Not read ({_why(exc)})", "items": []},
                            "setup": {"kind": None, "direction": None, "why": f"Not read: {_why(exc)}."},
                            "overextended": False, "overextended_why": [], "record": None, "card_key": None,
                            "listed": row["ranges"] >= MOVE_MIN_RANGES})
        movers = [r for r in movers if r.get("listed", True)]

        # 3. In the session: 5-minute bars of the sharpest swings, for the rest-of-the-day record, VWAP and the
        # opening range.
        cases = None
        if phase == "session" and horizon == "day":
            top = sorted((r for r in rows if r.get("since_open_ranges") is not None),
                         key=lambda r: -abs(r["since_open_ranges"]))[:SESSION_RECORD_NAMES]
            bars5 = self._session_bars([r["symbol"] for r in top])
            for r in top:
                if r["symbol"] in bars5:
                    r["extras"] = self._session_extras(bars5[r["symbol"]], now_ny.date(), minute)
            atr_of = {sym: atr_by_day_from_frame(frames[sym]) for sym in bars5 if sym in frames}
            cases = intraday_cases(bars5, atr_of, minute=minute, skip_day=now_ny.date()) if bars5 else None
            if not bars5:
                notes.append("No bars within the day could be read, so the morning swings are judged by the gap's "
                             "open-to-close record instead.")

        # 4. Every stock as an opportunity, scored; cards for the best.
        opps = self._opportunities(rows, phase=phase, horizon=horizon, frames=frames, cases=cases, minute=minute)
        cards: list[dict[str, Any]] = []
        by_symbol = {r["symbol"]: r for r in rows}
        for opp in [o for o in opps if o["tradable"]][:MORNING_CARDS]:
            if progress:
                progress(min(total - 1, done + len(checked)), total, opp["symbol"])
            card = self._card_for(opp, by_symbol[opp["symbol"]], horizon)
            if card is None:
                continue
            card["key"] = f"{opp['symbol']}:{opp['setup']}:{'L' if opp['direction'] == 'LONG' else 'S'}"
            card["record"] = by_symbol[opp["symbol"]].get("record")
            card["news"] = opp["news"]
            card["score"], card["opp_key"], card["setup_label"] = opp["score"], opp["key"], opp["setup_label"]
            card["edge"] = opp["edge"]
            cards.append(card)
            opp["card_key"], opp["option"] = card["key"], option_of(card)
            if horizon == "swing":                    # the card's chart levels replace the quick ones
                opp.update({k: card[k] for k in ("stop", "target_1", "target_2")})
                opp["levels_why"] = card.get("levels_why") or opp.get("levels_why")
            row = by_symbol[opp["symbol"]]
            if row in movers:
                row["card_key"] = card["key"]

        # 5. Reversal watch: the watchlist plus today's movers (a rescan keeps today's).
        reversal_rows: list[dict[str, Any]] = []
        prior = self._result if (rescan and self._result and self._result.get("day") == now_ny.date().isoformat()) else None
        if prior is not None:
            reversal_rows = prior.get("reversal") or []
            cards += [c for c in prior.get("cards") or [] if c.get("setup") == "reversal"]
        elif self.features.get("reversal_analysis"):
            reversal_rows, rev_cards = self._reversal_watch(s, movers, quotes, done, total, progress, len(cards))
            cards += rev_cards
        else:
            notes.append("Reversal analysis is switched off, so the reversal watch is empty.")

        if self.svc.market.info.mode == "demo":
            notes.insert(0, "Demo prices and headlines are synthetic: this plan says nothing about real stocks.")
        basis = ("Live prices." if clock == "open" else
                 "Stock prices are pre-market; option prices are the previous close's (options trade 9:30–16:00 "
                 "New York time)." if clock in PRE_STATES else
                 "The market is closed: prices are the last session's.")
        am_end = prefs["am_end"]
        result = {"checked_at": now_ny.isoformat(timespec="seconds"), "day": now_ny.date().isoformat(),
                  "seconds": round(time.time() - started, 1), "clock": clock, "phase": phase, "horizon": horizon,
                  "rescan": rescan, "price_basis": basis, "am_end": am_end,
                  "market": {"regime": regime_label,
                             "spy_move_pct": spy["move_pct"] if spy else None,
                             "spy_move_is": spy["move_is"] if spy else None},
                  "context": context, "universe_size": len(names), "read": len(frames), "movers": movers, "cards": cards,
                  "opportunities": opps, "reversal": reversal_rows, "notes": notes,
                  "data_source": self.svc.market.info.mode, "min_payoff": s.morning_min_payoff,
                  "session_record": ({"stocks": cases["stocks"], "days": SESSION_RECORD_DAYS, "minute": cases["minute"],
                                      "cases": int(len(cases["move"]))} if cases else None),
                  "rule": "A mover with news is read to keep going; a big mover with no news found, already "
                          "overextended, is read to snap back; every other stock goes whichever way its record favours. "
                          "Records are what followed the same pattern on these stocks: frequencies, not forecasts. "
                          "Nothing here places an order."}
        if not rescan or time.time() - self._run_saved_at >= RUN_SAVE_SECONDS:
            run_cards = {c["key"]: c for c in cards}
            top = [{**o, "card": run_cards.get(o.get("card_key"))} for o in opps if o["tradable"]][:RUN_TOP]
            result["run_id"] = self.runs.save("morning", top, {"label": f"Morning plan {now_ny:%b %d %H:%M}", "phase": phase,
                                                               "horizon": horizon})
            if result["run_id"]:
                self._run_saved_at = time.time()
        return result

    def _reversal_watch(self, s, movers, quotes, done: int, total: int, progress, cards_so_far: int):
        """The watchlist plus today's movers, read for a forming or confirmed bottom or top; a card for each
        confirmed one (a 15-day trade)."""
        reversal_rows: list[dict[str, Any]] = []
        cards: list[dict[str, Any]] = []
        watch_names = list(dict.fromkeys([*s.watchlist, *[r["symbol"] for r in movers]]))[:MAX_WATCH_SYMBOLS]
        for k, sym in enumerate(watch_names):
            if progress:
                progress(min(total - 1, done + len(movers) + k), total, sym)
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
            if row["confirmed"] and len(cards) < 3:
                entry = quotes.get(row["symbol"], {}).get("last") or row["close"]
                try:
                    card = self.card(symbol=row["symbol"], direction=row["direction"], entry=float(entry),
                                     stop=row["invalidation"], target_1=row["target_1"],
                                     target_2=row.get("target_2") or row["target_1"],
                                     horizon_days=REVERSAL_HORIZON, setup="reversal",
                                     levels_why="Levels from the reversal reading (Analyze opportunity has the detail).")
                    card["key"] = f"{row['symbol']}:reversal:{row['side']}"
                    cards.append(card)
                    row["card_key"] = card["key"]
                except Exception as exc:              # noqa: BLE001 - one stock never stops the morning
                    log.warning("Morning: no reversal card for %s: %r", row["symbol"], exc)
                    row["note"] = f"No card: {_why(exc)}."
        return reversal_rows, cards


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
        if state not in ("open", *PRE_STATES):         # 2.10: Robinhood's clock says "pre", Tradier's "premarket"
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
