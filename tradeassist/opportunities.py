"""Opportunities (2.15): one shape for a trade from any screen, its score, and the past record behind its edge.

The Morning plan, Live news and Trade ideas each find trades their own way. From 2.15 each also hands them on in
one shape, an *opportunity*, so they can be ranked, blended under a budget (blend.py), opened together on Analyze
opportunity, and paper traded (paper.py):

    key, source, symbol, direction (LONG or SHORT), setup, horizon ("day": out by 15:55 New York time; "swing":
    out on the horizon date), entry, stop, target_1, target_2, the stock's daily range (atr) and volatility, its
    sector fund, a score from 0 to 100 with the points behind it, the edge, the reasons, and the trade card with
    its contract when one was built.

**The edge** is what the blend optimizer maximises, so it is the most important number here and the easiest to
overstate. It is always a past average, never a forecast: how far the same kind of trade went the trade's way on
average in the price history (the record for that setup), net of the round trip's trading cost
(settings.stock_cost_bps each way), and pulled toward zero when the record has few cases. The pull is
cases / (cases + SHRINK_CASES): a record of 100 cases counts for half its average, one of 1,000 for 91%. A typical
day's move in a large stock is around 2%, so the average of 100 past days is still uncertain by about 0.2
percentage points either way, which is the size of most edges; the weight keeps a lucky small sample from
steering the money. With no record the edge is zero and the opportunity says so.

The records:

- **The day-trade record** (gap_record): on the stocks' own five years of daily bars, after the open gapped by at
  least as many daily ranges the same way (and was already overextended the day before, for a fade), how often
  the stock went the trade's way from the open to the close, and by how much on average. This is the record for a
  trade entered at the open and closed by the end of the day.
- **The rest-of-the-day record** (intraday_record): on 5-minute bars of the last weeks, pooled across the stocks
  being read, after a stock had moved at least as many daily ranges from the open by this time of day, how the
  rest of the day went. This is the record for a trade entered during the session (the morning swings and the
  news reactions).
- **The swing records**: the Morning plan's past record (close-to-close after a big move, 1, 3 and 5 days on) and
  the signal history's calibration (10 days on, against SPY) for Trade ideas.

Nothing here places an order.
"""

from __future__ import annotations

import json
import logging
import math
from datetime import date, datetime, time, timedelta
from typing import Any, Iterable

import numpy as np
import pandas as pd

from .optionmath import NY
from .sectors import sector_fund
from .signalbook import wilson

log = logging.getLogger(__name__)

SHRINK_CASES = 100                  # a record of this many cases counts for half its average
MIN_RECORD_CASES = 30
DAY_EXIT = time(15, 55)             # day trades are out five minutes before the close
SOURCES = ("morning", "live", "ideas", "manual")
HORIZONS = ("day", "swing")
INSTRUMENTS = ("shares", "short", "call", "put")
GOALS = ("dollars", "balanced", "low_risk")
GOAL_WORDS = {"dollars": "Most dollars", "balanced": "Best return for the risk", "low_risk": "Lowest risk"}
DIGEST_CHOICES = ("15", "30", "60", "session")

# Saved screen settings (kept in the database, set on the screens; .env keeps the account and the limits).
PREF_DEFAULTS: dict[str, Any] = {
    "morning": {"horizon": "day", "auto_refresh": False, "refresh_minutes": 5, "am_end": "11:30"},
    "live": {"digest": "15"},
    "blend": {"budget": None, "goal": "balanced", "tail_pct": 2.0, "max_share": 0.2, "max_positions": 10,
              "instruments": ["shares", "short", "call", "put"], "top_n": 10, "sector_cap": 0.4},
}
PREFS_KEY = "prefs:v1"


# --- saved settings -------------------------------------------------------------------------------------------

def _merge(base: dict[str, Any], patch: dict[str, Any]) -> dict[str, Any]:
    out = {k: (dict(v) if isinstance(v, dict) else v) for k, v in base.items()}
    for k, v in (patch or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _merge(out[k], v)
        else:
            out[k] = v
    return out


def check_prefs(p: dict[str, Any]) -> dict[str, Any]:
    """The settings with every value checked; raises ValueError naming the one that is wrong."""
    m, lv, b = p["morning"], p["live"], p["blend"]
    if m.get("horizon") not in HORIZONS:
        raise ValueError("The morning horizon must be day or swing")
    m["auto_refresh"] = bool(m.get("auto_refresh"))
    if int(m.get("refresh_minutes") or 0) not in (5, 10, 15, 30):
        raise ValueError("The morning refresh must be every 5, 10, 15 or 30 minutes")
    m["refresh_minutes"] = int(m["refresh_minutes"])
    try:
        hh, mm = (int(x) for x in str(m.get("am_end") or "").split(":"))
        if not (time(9, 45) <= time(hh, mm) <= time(16, 0)):
            raise ValueError
        m["am_end"] = f"{hh:02d}:{mm:02d}"
    except ValueError:
        raise ValueError("The end of the morning session must be a time from 09:45 to 16:00 (HH:MM)") from None
    if str(lv.get("digest")) not in DIGEST_CHOICES:
        raise ValueError("The news digest must be every 15, 30 or 60 minutes, or session")
    lv["digest"] = str(lv["digest"])
    if b.get("budget") is not None:
        b["budget"] = float(b["budget"])
        if not 100 <= b["budget"] <= 10_000_000:
            raise ValueError("The budget must be between $100 and $10,000,000")
    if b.get("goal") not in GOALS:
        raise ValueError(f"The goal must be one of {', '.join(GOALS)}")
    b["tail_pct"] = float(b.get("tail_pct", 2.0))
    if not 0.25 <= b["tail_pct"] <= 20:
        raise ValueError("The tail-loss limit must be between 0.25% and 20% of the budget")
    b["max_share"] = float(b.get("max_share", 0.2))
    if not 0.05 <= b["max_share"] <= 1:
        raise ValueError("The most in one position must be between 5% and 100% of the budget")
    b["max_positions"] = int(b.get("max_positions", 10))
    if not 1 <= b["max_positions"] <= 25:
        raise ValueError("The number of positions must be between 1 and 25")
    b["top_n"] = int(b.get("top_n", 10))
    if not 1 <= b["top_n"] <= 25:
        raise ValueError("The number of opportunities to blend must be between 1 and 25")
    b["sector_cap"] = float(b.get("sector_cap", 0.4))
    if not 0.1 <= b["sector_cap"] <= 1:
        raise ValueError("The most in one sector must be between 10% and 100% of the budget")
    inst = [i for i in dict.fromkeys(b.get("instruments") or []) if i in INSTRUMENTS]
    if not inst:
        raise ValueError(f"Allow at least one of {', '.join(INSTRUMENTS)}")
    b["instruments"] = inst
    return p


def load_prefs(db: Any, settings: Any) -> dict[str, Any]:
    try:
        saved = db.cache_get(PREFS_KEY) or {}
    except Exception:                                 # noqa: BLE001 - a damaged copy is the defaults
        saved = {}
    p = _merge(PREF_DEFAULTS, saved if isinstance(saved, dict) else {})
    try:
        p = check_prefs(p)
    except (ValueError, TypeError):
        p = _merge(PREF_DEFAULTS, {})
    if p["blend"].get("budget") is None:
        p["blend"]["budget"] = float(settings.account_size)
    return p


def save_prefs(db: Any, settings: Any, patch: dict[str, Any]) -> dict[str, Any]:
    """Merge `patch` into the saved settings, check every value, and keep them."""
    current = load_prefs(db, settings)
    unknown = [k for k in (patch or {}) if k not in PREF_DEFAULTS]
    if unknown:
        raise ValueError(f"Unknown settings group: {', '.join(unknown)}")
    for group, values in (patch or {}).items():
        if not isinstance(values, dict):
            raise ValueError(f"{group} must be a group of settings")
        bad = [k for k in values if k not in PREF_DEFAULTS[group]]
        if bad:
            raise ValueError(f"Unknown {group} setting: {', '.join(bad)}")
    p = check_prefs(_merge(current, patch))
    db.cache_put(PREFS_KEY, p)
    return p


# --- edges ------------------------------------------------------------------------------------------------------

def shrink_weight(cases: int | float | None) -> float:
    n = max(0.0, float(cases or 0))
    return n / (n + SHRINK_CASES)


def edge_from(mean_pct: float | None, cases: int | None, *, basis: str, cost_pct: float,
              frequency: float | None = None, low: float | None = None, high: float | None = None,
              scale: float = 1.0) -> dict[str, Any]:
    """The edge in percent of the entry, the trade's way: the past average (times `scale`, for a record measured
    over another span) net of the round trip's cost, times the weight its number of cases earns."""
    if mean_pct is None or not cases or not math.isfinite(float(mean_pct)):
        return {"known": False, "expected_pct": -cost_pct, "raw_pct": None, "cases": int(cases or 0), "weight": 0.0,
                "cost_pct": cost_pct, "frequency": None, "basis": basis + " — no past cases, so no edge is assumed "
                                                                         "beyond the cost of trading."}
    w = shrink_weight(cases)
    raw = float(mean_pct) * scale
    return {"known": True, "expected_pct": raw * w - cost_pct, "raw_pct": raw, "cases": int(cases), "weight": w,
            "cost_pct": cost_pct, "frequency": frequency, "low": low, "high": high,
            "enough": int(cases) >= MIN_RECORD_CASES, "basis": basis}


def cost_pct(settings: Any) -> float:
    """The round trip's trading cost in percent of the entry (settings.stock_cost_bps each way)."""
    return 2 * float(getattr(settings, "stock_cost_bps", 5.0)) / 100.0


def gap_record(frames: dict[str, dict[str, np.ndarray]], *, gap_direction: int, min_ranges: float,
               trade_direction: int, need_overext: bool = False, max_ranges: float | None = None) -> dict[str, Any]:
    """Across `frames` (finished daily bars with open, close, ATR): on days that opened at least `min_ranges`
    daily ranges (and less than `max_ranges`) away from the previous close in `gap_direction`, how often the stock
    went `trade_direction`'s way from the open to the close, and the average move in percent. Overextension, when
    asked, is read on the day before (as the live reading has to read it). A past frequency, not a forecast."""
    from .morning import overextended
    wins = total = 0
    moves: list[float] = []
    for f in frames.values():
        close, open_, atr = f["close"], f.get("open"), f["atr"]
        if open_ is None or len(close) < 40:
            continue
        prev = close[:-1]
        with np.errstate(invalid="ignore", divide="ignore"):
            gap = (open_[1:] - prev) / atr[:-1]
            oc = close[1:] / open_[1:] - 1.0
        ok = np.isfinite(gap) & np.isfinite(oc) & (gap_direction * gap >= min_ranges)
        if max_ranges is not None:
            ok &= gap_direction * gap < max_ranges
        for i in np.flatnonzero(ok):
            if need_overext and not overextended(f, i, gap_direction)[0]:
                continue
            m = trade_direction * float(oc[i])
            total += 1
            wins += m > 0
            moves.append(m)
    out: dict[str, Any] = {"cases": total, "what": f"opens {min_ranges:g}+ daily ranges {'up' if gap_direction > 0 else 'down'}"
                           + (" after an overextended day" if need_overext else "") + ", open to close"}
    if total:
        lo, hi = wilson(wins, total)
        out.update({"frequency": wins / total, "low": lo, "high": hi, "mean_move_pct": 100 * float(np.mean(moves)),
                    "enough": total >= MIN_RECORD_CASES})
    return out


def session_frame(df: pd.DataFrame) -> pd.DataFrame:
    """5-minute bars of the regular session with the New York date and minutes after 9:30 of each bar's end."""
    if df is None or df.empty:
        return pd.DataFrame(columns=["t", "open", "high", "low", "close", "volume", "day", "minute"])
    d = df.copy()
    ny = pd.to_datetime(d["t"], unit="s", utc=True).dt.tz_convert(NY)
    d["day"] = ny.dt.date
    d["minute"] = (ny.dt.hour * 60 + ny.dt.minute - 570).astype(int) + 5      # minutes after 9:30 at the bar's end
    return d[(d["minute"] > 0) & (d["minute"] <= 390)]


def intraday_cases(bars: dict[str, pd.DataFrame], atr_of: dict[str, dict[date, float]], *, minute: int,
                   skip_day: date | None = None) -> dict[str, Any]:
    """Every stock-day in `bars` (5-minute bars, a few weeks of them) that ran to the close: where the stock stood
    `minute` minutes after the open, in daily ranges from its open (signed), and its move from then to the close (a
    fraction, signed). `atr_of` gives each stock's daily range as of the day before each date; `skip_day` (today) is
    left out, its close not being in. The record for any size and direction is then a filter (intraday_record_from)."""
    moves, rests = [], []
    used = 0
    for sym, df in bars.items():
        s = session_frame(df)
        if s.empty:
            continue
        ranges = atr_of.get(sym) or {}
        counted = False
        for day, g in s.groupby("day"):
            if skip_day is not None and day == skip_day:
                continue
            g = g.sort_values("t")
            if g["minute"].iloc[-1] < 385:            # a day that did not run to the close
                continue
            at = g[g["minute"] <= minute]
            atr = ranges.get(day)
            if at.empty or not atr or atr <= 0:
                continue
            open_, now_px, close = float(g["open"].iloc[0]), float(at["close"].iloc[-1]), float(g["close"].iloc[-1])
            if open_ <= 0 or now_px <= 0:
                continue
            moves.append((now_px - open_) / atr)
            rests.append(close / now_px - 1.0)
            counted = True
        used += counted
    return {"move": np.array(moves, dtype=float), "rest": np.array(rests, dtype=float), "stocks": used, "minute": minute}


def intraday_record_from(cases: dict[str, Any], *, min_ranges: float, move_direction: int,
                         trade_direction: int) -> dict[str, Any]:
    """From intraday_cases: on days a stock stood at least `min_ranges` daily ranges from its open in
    `move_direction` by that minute, how often the rest of the day went `trade_direction`'s way, and the average
    move to the close in percent. A past frequency, not a forecast."""
    sel = move_direction * cases["move"] >= min_ranges
    m = trade_direction * cases["rest"][sel]
    total = int(sel.sum())
    out: dict[str, Any] = {"cases": total, "what": f"{min_ranges:g}+ daily ranges {'up' if move_direction > 0 else 'down'} "
                                                   f"from the open {cases['minute']} minutes in, then to the close"}
    if total:
        wins = int((m > 0).sum())
        lo, hi = wilson(wins, total)
        out.update({"frequency": wins / total, "low": lo, "high": hi, "mean_move_pct": 100 * float(m.mean()),
                    "enough": total >= MIN_RECORD_CASES})
    return out


def intraday_record(bars: dict[str, pd.DataFrame], atr_of: dict[str, dict[date, float]], *, minute: int,
                    min_ranges: float, move_direction: int, trade_direction: int,
                    skip_day: date | None = None) -> dict[str, Any]:
    """Pooled across the stocks in `bars`: on days when, `minute` minutes after the open, the stock stood at least
    `min_ranges` daily ranges from its open in `move_direction`, how often the rest of the day went
    `trade_direction`'s way, and the average move to the close in percent."""
    return intraday_record_from(intraday_cases(bars, atr_of, minute=minute, skip_day=skip_day),
                                min_ranges=min_ranges, move_direction=move_direction, trade_direction=trade_direction)


def atr_by_day_from_frame(f: dict[str, np.ndarray]) -> dict[date, float]:
    """Each date's daily range as of the day before it, from a morning frame (features_of)."""
    dates, atr = f["dates"], f["atr"]
    out = {}
    for i in range(1, len(dates)):
        v = atr[i - 1]
        if np.isfinite(v) and v > 0:
            out[date.fromisoformat(str(dates[i]))] = float(v)
    return out


def atr_by_day(bars: pd.DataFrame) -> dict[date, float]:
    """Each date's daily range (ATR 14) as of the day before it, from finished daily bars."""
    from .indicators import atr as atr_series
    if bars is None or len(bars) < 20:
        return {}
    a = atr_series(bars["high"], bars["low"], bars["close"]).shift(1)
    return {d.date(): float(v) for d, v in zip(pd.to_datetime(bars["date"]), a) if np.isfinite(v) and v > 0}


def realized_vol(closes: np.ndarray, days: int = 60) -> float | None:
    c = np.asarray(closes, dtype=float)[-(days + 1):]
    if len(c) < 21:
        return None
    r = np.diff(np.log(c))
    r = r[np.isfinite(r)]
    v = float(r.std(ddof=1) * math.sqrt(252)) if len(r) > 1 else None
    return v if v and math.isfinite(v) and v > 0 else None


# --- levels for a day trade --------------------------------------------------------------------------------------

def day_levels(*, entry: float, atr: float, direction: int, setup: str,
               reference: float | None = None) -> tuple[float, float, float, str]:
    """Stop and targets sized for one session. A fade (against a gap) aims to win back half the gap, then all of
    it, with the stop 0.6 of a daily range beyond the entry; everything else (riding a gap, a morning swing, a news
    reaction) risks 0.6 of a daily range for targets at 1.5 and 2.5 times that."""
    risk = 0.6 * atr
    stop = entry - direction * risk
    if setup == "fade" and reference and direction * (reference - entry) > 0.25 * atr:
        t1 = entry + 0.5 * (reference - entry)
        t2 = reference
        why = (f"Day-trade levels: stop 0.6 of a daily range {'below' if direction > 0 else 'above'} the entry, target 1 "
               f"half the gap back, target 2 the previous close (${reference:,.2f}). Out by 15:55 whatever happens.")
    else:
        t1 = entry + direction * 1.5 * risk
        t2 = entry + direction * 2.5 * risk
        why = ("Day-trade levels: stop 0.6 of a daily range from the entry, targets 1.5 and 2.5 times that risk. "
               "Out by 15:55 whatever happens.")
    return float(stop), float(t1), float(t2), why


# --- the opportunity ---------------------------------------------------------------------------------------------

def part(key: str, label: str, points: float, most: float, text: str) -> dict[str, Any]:
    return {"key": key, "label": label, "points": round(float(points), 1), "max": most, "text": text}


def total_score(parts: Iterable[dict[str, Any]]) -> float:
    return float(np.clip(sum(p["points"] for p in parts), 0, 100))


def option_of(card: dict[str, Any] | None) -> dict[str, Any] | None:
    """The bought call or put on a trade card, when its limit passed (the card's own check), in the blend's terms:
    what one contract costs at the suggested limit, and what the model needs to value it."""
    opt = (card or {}).get("option")
    lm = (opt or {}).get("limits") or {}
    if not opt or not lm.get("viable") or not lm.get("suggested"):
        return None
    return {"option_type": str(opt["option_type"]).upper(), "strike": float(opt["strike"]), "expiration": opt["expiration"],
            "premium": float(lm["suggested"]), "bid": opt.get("bid"), "ask": opt.get("ask"), "mid": opt.get("mid"),
            "iv": opt.get("iv"), "delta": opt.get("delta"), "contracts": opt.get("contracts"),
            "value_at_target": lm.get("value_at_target"), "value_at_stop": lm.get("value_at_stop")}


def make(*, source: str, key: str, symbol: str, direction: str, setup: str, setup_label: str, horizon: str,
         horizon_days: int, entry: float, stop: float, target_1: float, target_2: float | None = None,
         atr: float | None = None, vol: float | None = None, score: float = 0.0,
         score_parts: list[dict[str, Any]] | None = None, edge: dict[str, Any] | None = None,
         why: list[str] | None = None, card: dict[str, Any] | None = None, **extra: Any) -> dict[str, Any]:
    """One opportunity, checked: the stop behind the entry and target 1 ahead of it, the trade's way."""
    if source not in SOURCES:
        raise ValueError(f"source must be one of {', '.join(SOURCES)}")
    if horizon not in HORIZONS:
        raise ValueError("horizon must be day or swing")
    d = 1 if direction == "LONG" else -1
    if direction not in ("LONG", "SHORT"):
        raise ValueError("direction must be LONG or SHORT")
    if not (entry > 0 and d * (entry - stop) > 0 and d * (target_1 - entry) > 0):
        raise ValueError(f"{symbol}: the stop must sit behind the entry and target 1 ahead of it")
    if target_2 is None or d * (target_2 - target_1) <= 0:
        target_2 = target_1 + d * abs(target_1 - entry) * 0.5
    out = {"key": key, "source": source, "symbol": symbol.upper(), "direction": direction, "setup": setup,
           "setup_label": setup_label, "horizon": horizon, "horizon_days": int(horizon_days),
           "entry": float(entry), "stop": float(stop), "target_1": float(target_1), "target_2": float(target_2),
           "atr": float(atr) if atr else None, "vol": float(vol) if vol else None, "sector": sector_fund(symbol.upper()),
           "score": round(float(score), 1), "score_parts": score_parts or [],
           "edge": edge or {"known": False, "expected_pct": 0.0, "cases": 0, "weight": 0.0, "basis": "No record."},
           "why": why or [], "card": card, "option": option_of(card),
           "built_at": datetime.now(NY).isoformat(timespec="seconds")}
    out.update(extra)
    return out


def compact(opp: dict[str, Any]) -> dict[str, Any]:
    """An opportunity without its full trade card (for stored runs): the contract the blend needs is kept."""
    out = {k: v for k, v in opp.items() if k != "card"}
    card = opp.get("card") or {}
    if card:
        out["card_summary"] = {"order_text": card.get("order_text"), "horizon_date": card.get("horizon_date"),
                               "shares": (card.get("shares") or {}).get("count")}
    return out


def from_idea(row: dict[str, Any], *, settings: Any, run_day: str) -> dict[str, Any]:
    """A Trade ideas row (a 20-day swing trade, graded) as an opportunity. Its edge is the signal history's
    calibration for the stock's trend score: how stocks did against SPY 10 trading days after a day whose net score
    sat in the same range (doubled for the 20-day holding period, pulled toward zero by its independent periods),
    the trade's way. Without the signal history no edge is assumed."""
    from .ideas import HORIZON_DAYS
    d = 1 if row["direction"] == "LONG" else -1
    past = row.get("past_10d") or {}
    excess = past.get("mean_excess_vs_spy")
    cases = past.get("independent_periods") or past.get("cases")
    if excess is not None and cases:
        edge = edge_from(100 * d * float(excess), cases, cost_pct=cost_pct(settings), scale=HORIZON_DAYS / 10.0,
                         frequency=past.get("frequency"),
                         basis=f"Signal history: {int(past.get('cases') or 0)} days with a net score in this range, "
                               f"{int(cases)} independent periods; the average move against SPY 10 days on, doubled for "
                               "20 days, the trade's way")
    else:
        edge = edge_from(None, 0, cost_pct=cost_pct(settings),
                         basis="No signal history for this trend score (build it on Analyze opportunity → Signals)")
    grade_pts = 0.6 * float(row.get("score") or 0)
    rr = float(row.get("reward_risk_1") or 0)
    rr_pts = float(np.clip(rr / 3.0, 0, 1)) * 15
    edge_pts = float(np.clip(edge["expected_pct"] / 2.0, -1, 1)) * 25 if edge["known"] else 0.0
    parts = [part("grade", "Trade grade", grade_pts, 60, f"Grade {row['grade']} ({row.get('score', 0):.0f} of 100) × 0.6"),
             part("reward", "Reward to risk", rr_pts, 15, f"{rr:.1f} × the risk at target 1"),
             part("edge", "Past record", edge_pts, 25, f"{edge['expected_pct']:+.2f}% expected after the pull toward zero"
                  if edge["known"] else "No record: no points")]
    return make(source="ideas", key=f"ideas:{run_day}:{row['symbol']}:{row['direction']}", symbol=row["symbol"],
                direction=row["direction"], setup="idea", setup_label=f"Grade {row['grade']} {'buy' if d > 0 else 'short'}",
                horizon="swing", horizon_days=HORIZON_DAYS, entry=float(row["entry"]), stop=float(row["stop"]),
                target_1=float(row["target_1"]), target_2=float(row["target_2"]), atr=row.get("atr"),
                score=total_score(parts), score_parts=parts, edge=edge,
                why=[f"Trend {row['trend']['label']}; grade {row['grade']}."] + list(row.get("caps") or [])[:2],
                grade=row.get("grade"), price=float(row["entry"]))


# --- stored runs ---------------------------------------------------------------------------------------------------

class Runs:
    """Every list of opportunities a screen built (each morning plan, news digest and ideas scan), kept so paper
    trading can take the latest and replay any of them later. Kept 120 days."""

    KEEP_DAYS = 120

    def __init__(self, db: Any):
        self.db = db

    def save(self, source: str, opportunities: list[dict[str, Any]], meta: dict[str, Any] | None = None) -> int | None:
        if source not in SOURCES or not opportunities:
            return None
        from .service import clean
        now = datetime.now(NY)
        try:
            with self.db.tx() as cur:
                cur.execute("INSERT INTO opportunity_runs (source, created_at, day, count, payload_json, meta_json) "
                            "VALUES (?, ?, ?, ?, ?, ?)",
                            (source, now.isoformat(timespec="seconds"), now.date().isoformat(), len(opportunities),
                             json.dumps(clean([compact(o) for o in opportunities])), json.dumps(clean(meta or {}))))
                run_id = int(cur.lastrowid)
                cur.execute("DELETE FROM opportunity_runs WHERE created_at < ?",
                            ((now - timedelta(days=self.KEEP_DAYS)).isoformat(timespec="seconds"),))
            return run_id
        except Exception as exc:                      # noqa: BLE001 - a run not kept never breaks the screen
            log.warning("Opportunity run not kept: %s", exc)
            return None

    def latest(self, source: str) -> dict[str, Any] | None:
        r = self.db.one("SELECT * FROM opportunity_runs WHERE source = ? ORDER BY id DESC LIMIT 1", (source,))
        return self._row(r) if r else None

    def get(self, run_id: int) -> dict[str, Any] | None:
        r = self.db.one("SELECT * FROM opportunity_runs WHERE id = ?", (run_id,))
        return self._row(r) if r else None

    def listing(self, limit: int = 60) -> list[dict[str, Any]]:
        rows = self.db.query("SELECT id, source, created_at, day, count, meta_json FROM opportunity_runs "
                             "ORDER BY id DESC LIMIT ?", (limit,))
        out = []
        for r in rows:
            meta = json.loads(r["meta_json"] or "{}")
            out.append({"id": r["id"], "source": r["source"], "created_at": r["created_at"], "day": r["day"],
                        "count": r["count"], "label": meta.get("label") or r["source"]})
        return out

    @staticmethod
    def _row(r: Any) -> dict[str, Any]:
        return {"id": r["id"], "source": r["source"], "created_at": r["created_at"], "day": r["day"],
                "opportunities": json.loads(r["payload_json"] or "[]"), "meta": json.loads(r["meta_json"] or "{}")}


__all__ = ["PREF_DEFAULTS", "load_prefs", "save_prefs", "check_prefs", "edge_from", "shrink_weight", "cost_pct",
           "gap_record", "intraday_record", "intraday_cases", "intraday_record_from", "atr_by_day", "atr_by_day_from_frame",
           "session_frame", "realized_vol", "day_levels", "make",
           "compact", "from_idea", "option_of", "part", "total_score", "Runs", "SHRINK_CASES", "GOALS", "GOAL_WORDS",
           "INSTRUMENTS", "HORIZONS", "SOURCES", "DAY_EXIT"]
