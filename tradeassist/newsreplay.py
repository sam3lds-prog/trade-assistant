"""The news replay (2.16): what was really available to a trader after a headline, from trade prints.

The question it answers is the one that decides whether a sub-second news feed is worth paying for: when a
headline crosses the wire, what price could an order actually have got 100 ms, 500 ms, 1 s, 5 s and 30 s later,
and what did that trade make afterwards? Not the first print after the headline, which a machine in the same
building got, but the print a marketable order would have met after

    the feed's latency    the time from publication to the app's receipt (RTPR about a second; a free feed
                          minutes), set by the run and compared with what the feed itself reported
    the reaction delay    the offset: how long after receipt the order left
    the spread            half of it is paid on the way in and again on the way out; estimated from the prints
                          of the half hour before the news (Roll's measure of the bid-ask bounce), or set
    slippage              a further cost in basis points for an order that moves the price
    halts                 a volatility or news-pending halt shows as a gap with no prints in regular hours; an
                          order sent into the gap fills at the reopening, not at the last print before it
    the prints' resolution Tradier stamps prints to the second, so an offset under a second is measured at the
                          first print that is certainly not earlier (the next whole second), and 100 ms and
                          500 ms land on the same print unless the headline sat near the end of a second. A
                          feed stamped to the millisecond would tell them apart; this one says so instead of
                          pretending.

Each event is traded the way the news should push it (newsintel.classify), never the way the price turned out
to go, and held to a minute, five, fifteen, thirty, an hour and the close. Across events the harness reports,
per offset and horizon, the median and mean result after costs and how often it was positive, how much of the
first minute's move each offset still caught, and two verdicts in plain words: whether sub-second entry beat
entry at 30 seconds by enough to pay for a feed, and whether a second leg (entering at 30 s, holding half an
hour) has been there to take at all. Those records feed the live assessment (newsintel.second_leg).

What it is not: a backtest of a strategy. It measures one thing, the executable price after a headline, on the
news this app itself collected (the feed's items of the last few days, or events typed in by hand). Nothing
here places an order.
"""

from __future__ import annotations

import json
import logging
import math
import statistics
import threading
import time
from datetime import datetime, timedelta, timezone
from typing import Any

import numpy as np
import pandas as pd

from .background import OneJob
from .events import redact
from .newsintel import CATALYSTS, MIN_RECORD_CASES, classify
from .optionmath import NY
from .providers.base import DataError

log = logging.getLogger(__name__)

OFFSETS = (0.1, 0.5, 1.0, 5.0, 30.0)                 # seconds after the app received the headline
HORIZONS = (60, 300, 900, 1800, 3600)                # seconds after publication the trade is marked or closed
HORIZON_WORDS = {60: "1 min", 300: "5 min", 900: "15 min", 1800: "30 min", 3600: "1 hour", "close": "the close"}
SECOND_LEG = (30.0, 1800)                            # the offset and horizon the live "second leg" record uses
BEFORE_MINUTES = 30                                  # prints read before the headline: the pre-news price, the spread
AFTER_MINUTES = 65                                   # ... and after it: every horizon but the close
DEFAULT_SPREAD_BPS = 5.0                             # when the prints give no spread estimate
DEFAULT_SLIPPAGE_BPS = 5.0
DEFAULT_FEED_LATENCY_S = 1.0
HALT_GAP_MIN_S = 60.0                                # a gap with no prints at least this long in regular hours ...
HALT_GAP_MAX_S = 300.0                               # ... (scaled by how often the stock printed before) reads as a halt
MIN_JUMP_PCT = 0.05                                  # a first-minute move under this is "no move" for the captured share
MERGE_MINUTES = 30                                   # one event per stock per half hour
MIN_AGE_MINUTES = 31                                 # a headline younger than this has no 30-minute horizon yet
MAX_EVENTS = 150
KEEP_DAYS = 60
SESSION_OPEN, SESSION_CLOSE = (9, 30), (16, 0)


def prints_frame(rows: list[dict[str, Any]]) -> pd.DataFrame:
    """Trade prints as a clean frame: `t` (UTC seconds), `price`, `volume`, in time order, bad rows dropped. Prints
    in the same second keep the order they came in, which is the order they traded."""
    df = pd.DataFrame(rows, columns=["t", "price", "volume"])
    if df.empty:
        return df
    for col in ("t", "price", "volume"):
        df[col] = pd.to_numeric(df[col], errors="coerce")
    df = df.dropna(subset=["t", "price"])
    df = df[(df["price"] > 0) & (df["t"] > 0)]
    df["volume"] = df["volume"].fillna(0.0)
    return df.sort_values("t", kind="stable").reset_index(drop=True)


def resolution_of(t: np.ndarray) -> float:
    """How finely the prints are stamped: 1.0 for whole seconds, else the smallest positive step seen."""
    if len(t) == 0:
        return 1.0
    if np.allclose(t, np.round(t)):
        return 1.0
    steps = np.diff(np.unique(t))
    steps = steps[steps > 0]
    return float(min(1.0, steps.min())) if len(steps) else 1.0


def in_regular_session(t: float) -> bool:
    local = datetime.fromtimestamp(t, NY)
    minutes = local.hour * 60 + local.minute
    return local.weekday() < 5 and SESSION_OPEN[0] * 60 + SESSION_OPEN[1] <= minutes < SESSION_CLOSE[0] * 60 + SESSION_CLOSE[1]


def estimate_spread_bps(prices: np.ndarray) -> float | None:
    """Roll's measure of the effective spread from the bid-ask bounce in successive trade prices: two times the
    square root of minus the covariance of consecutive price changes. None when the changes are positively
    correlated (a trending half hour, or too few prints), in which case the caller falls back to a default."""
    if len(prices) < 30:
        return None
    changes = np.diff(prices.astype(float))
    if len(changes) < 20:
        return None
    cov = float(np.cov(changes[:-1], changes[1:])[0, 1])
    if not math.isfinite(cov) or cov >= 0:
        return None
    spread = 2.0 * math.sqrt(-cov)
    mid = float(np.median(prices))
    bps = 1e4 * spread / mid if mid > 0 else None
    return bps if bps is not None and bps >= 0.1 else None          # under a tenth of a point: noise, not a bounce


def halt_gaps(t: np.ndarray, gap_s: float) -> list[tuple[float, float]]:
    """Gaps with no prints of at least `gap_s` seconds that lie in regular hours: the shape of a halt."""
    out = []
    for a, b in zip(t[:-1], t[1:]):
        if b - a >= gap_s and in_regular_session(float(a)) and in_regular_session(float(b) - 1):
            out.append((float(a), float(b)))
    return out


def _first_at_or_after(t: np.ndarray, moment: float) -> int | None:
    i = int(np.searchsorted(t, moment, side="left"))
    return i if i < len(t) else None


def replay_event(prints: pd.DataFrame, published_t: float, direction: int, *, feed_latency_s: float = DEFAULT_FEED_LATENCY_S,
                 offsets: tuple[float, ...] = OFFSETS, horizons: tuple[int, ...] = HORIZONS,
                 spread_bps: float | None = None, slippage_bps: float = DEFAULT_SLIPPAGE_BPS,
                 close_price: float | None = None) -> dict[str, Any]:
    """One event, from its prints: the executable price at each offset after the app received the headline, and
    the trade's result at each horizon after publication. Pure arithmetic, so it can be tested alone.

    `direction` is the way the news should push the stock (+1 long, -1 short), decided before the price moved.
    An entry pays half the spread plus the slippage; an exit pays the same again. The "captured" share says how
    much of the first minute's move was still ahead of the fill: 1 means all of it, 0 none, negative that the
    fill was already past the price a minute after the headline."""
    if direction not in (1, -1):
        raise ValueError("direction must be +1 or -1")
    if prints is None or prints.empty:
        return {"skipped": "no trade prints around the headline"}
    t = prints["t"].to_numpy(dtype=float)
    p = prints["price"].to_numpy(dtype=float)
    d = float(direction)
    resolution = resolution_of(t)
    before = p[(t < published_t) & (t >= published_t - BEFORE_MINUTES * 60)]
    i_ref = int(np.searchsorted(t, published_t, side="left")) - 1
    if i_ref < 0 or published_t - t[i_ref] > BEFORE_MINUTES * 60:
        return {"skipped": "no print in the half hour before the headline (the stock was not trading)", "resolution_s": resolution}
    ref_price, ref_t = float(p[i_ref]), float(t[i_ref])
    spread_est = estimate_spread_bps(before)
    spread = float(spread_bps) if spread_bps is not None else (spread_est if spread_est is not None else DEFAULT_SPREAD_BPS)
    cost = (0.5 * spread + slippage_bps) / 1e4                     # paid on the way in, and again on the way out
    pre_gaps = np.diff(t[(t < published_t) & (t >= published_t - BEFORE_MINUTES * 60)])
    typical_gap = float(np.median(pre_gaps)) if len(pre_gaps) else 1.0
    halt_gap = max(HALT_GAP_MIN_S, min(HALT_GAP_MAX_S, 30.0 * typical_gap))
    halts = halt_gaps(t[t >= published_t - 60], halt_gap)

    def price_at(moment: float) -> tuple[int | None, float]:
        """The first print certainly not earlier than `moment`: with whole-second stamps, the next whole second."""
        target = math.ceil(moment - 1e-9) if resolution >= 1.0 else moment
        return _first_at_or_after(t, target), target

    i_first, _ = price_at(published_t)
    first_print = {"lag_s": float(t[i_first] - published_t), "price": float(p[i_first]),
                   "move_pct": 100 * (p[i_first] / ref_price - 1)} if i_first is not None else None
    i_1m, _ = price_at(published_t + 60)
    p_1m = float(p[i_1m]) if i_1m is not None else None
    jump_pct = 100 * (p_1m / ref_price - 1) if p_1m else None
    exits: dict[str, float | None] = {}
    for h in horizons:
        i_h, _ = price_at(published_t + h)
        exits[str(h)] = float(p[i_h]) if i_h is not None else None
    exits["close"] = float(close_price) if close_price else None
    rows = []
    for o in offsets:
        target = published_t + feed_latency_s + o
        i_fill, asked = price_at(target)
        if i_fill is None:
            rows.append({"offset_s": o, "target_t": target, "fill": None, "note": "no print after this moment in the data"})
            continue
        fill_t, raw = float(t[i_fill]), float(p[i_fill])
        gap_before = float(fill_t - t[i_fill - 1]) if i_fill > 0 else 0.0
        halted = any(a <= target <= b for a, b in halts) or (gap_before >= halt_gap and i_fill > 0 and t[i_fill - 1] <= target
                                                             and in_regular_session(float(t[i_fill - 1])))
        fill = raw * (1 + d * cost)
        captured = None
        if jump_pct is not None and abs(jump_pct) >= MIN_JUMP_PCT:
            captured = d * (p_1m - fill) / abs(p_1m - ref_price)
        net = {}
        for key, px in exits.items():
            net[key] = 100 * d * (px * (1 - d * cost) / fill - 1) if px else None
        rows.append({"offset_s": o, "target_t": target, "asked_t": asked, "fill_t": fill_t, "lag_s": fill_t - target,
                     "raw_price": raw, "fill": fill, "since_ref_pct": 100 * (raw / ref_price - 1), "halted": bool(halted),
                     "gap_before_s": gap_before, "captured": captured, "net": net,
                     "note": "halted: filled at the reopening" if halted else
                             ("whole-second stamps: measured at the next second" if resolution >= 1.0 and o < 1.0 else "")})
    return {"skipped": None, "ref_price": ref_price, "ref_t": ref_t, "ref_age_s": published_t - ref_t,
            "first_print": first_print, "price_1m": p_1m, "jump_1m_pct": jump_pct, "spread_bps": spread,
            "spread_estimated": spread_bps is None and spread_est is not None, "slippage_bps": slippage_bps,
            "feed_latency_s": feed_latency_s, "cost_pct_each_way": 100 * cost, "resolution_s": resolution,
            "halt_gap_s": halt_gap, "halts": [{"from": a, "to": b, "seconds": b - a} for a, b in halts],
            "halted": any(r.get("halted") for r in rows), "exits": exits, "offsets": rows, "prints": int(len(t))}


# --- across events ------------------------------------------------------------------------------------------------

def _median(values: list[float]) -> float | None:
    vals = [float(v) for v in values if isinstance(v, (int, float)) and math.isfinite(v)]
    return statistics.median(vals) if vals else None


def _mean(values: list[float]) -> float | None:
    vals = [float(v) for v in values if isinstance(v, (int, float)) and math.isfinite(v)]
    return statistics.fmean(vals) if vals else None


def _hit(values: list[float]) -> float | None:
    vals = [float(v) for v in values if isinstance(v, (int, float)) and math.isfinite(v)]
    return sum(v > 0 for v in vals) / len(vals) if vals else None


def aggregate(results: list[dict[str, Any]], offsets: tuple[float, ...] = OFFSETS,
              horizons: tuple[int, ...] = HORIZONS) -> dict[str, Any]:
    """The table across events: per offset and horizon the median and mean net result and how often it was
    positive; per offset the share of the first minute's move still caught, the fill's lag and the halts; and
    the two verdicts in words."""
    done = [r for r in results if r.get("result") and not r["result"].get("skipped")]
    keys = [str(h) for h in horizons] + ["close"]
    table = []
    for o in offsets:
        cells = {}
        rows = [next((x for x in r["result"]["offsets"] if x["offset_s"] == o and x.get("fill")), None) for r in done]
        rows = [x for x in rows if x]
        for k in keys:
            nets = [x["net"].get(k) for x in rows]
            cells[k] = {"median": _median(nets), "mean": _mean(nets), "hit_rate": _hit(nets),
                        "n": sum(1 for v in nets if isinstance(v, (int, float)) and math.isfinite(v))}
        table.append({"offset_s": o, "n": len(rows), "captured_median": _median([x["captured"] for x in rows]),
                      "lag_median_s": _median([x["lag_s"] for x in rows]),
                      "since_ref_median_pct": _median([x["since_ref_pct"] for x in rows]),
                      "halted": sum(1 for x in rows if x.get("halted")), "horizons": cells})
    by_offset = {row["offset_s"]: row for row in table}
    fast, slow = by_offset.get(offsets[0]), by_offset.get(SECOND_LEG[0])
    leg_key = str(SECOND_LEG[1])
    verdicts = []
    n = len(done)
    skipped = [r for r in results if r.get("result") and r["result"].get("skipped")]
    if n < MIN_RECORD_CASES:
        verdicts.append({"state": "warn", "text": f"{n} event{'s' if n != 1 else ''} replayed: too few to call anything "
                                                  f"(at least {MIN_RECORD_CASES} are needed). Listen for a few more days, "
                                                  "or add events by hand."})
    elif fast and slow and leg_key in fast["horizons"] and leg_key in slow["horizons"]:
        f_med, s_med = fast["horizons"][leg_key]["median"], slow["horizons"][leg_key]["median"]
        if f_med is not None and s_med is not None:
            gain = f_med - s_med
            jumps = _median([r["result"]["jump_1m_pct"] for r in done])
            fast_caught = fast["captured_median"]
            verdicts.append({"state": "pass" if gain >= 0.5 else "warn" if gain >= 0.15 else "fail",
                             "text": (f"Sub-second entry: entering {offsets[0] * 1000:.0f} ms after receipt and holding to "
                                      f"{HORIZON_WORDS[SECOND_LEG[1]]} made a median {f_med:+.2f}% after costs, against "
                                      f"{s_med:+.2f}% for entering at {SECOND_LEG[0]:.0f} s: the sub-second feed was worth "
                                      f"{gain:+.2f} points an event over {n} events"
                                      + (f", catching a median {100 * fast_caught:.0f}% of the first minute's move"
                                         if fast_caught is not None else "")
                                      + (f" (median first-minute move {abs(jumps):.1f}%)." if jumps is not None else ".")
                                      + (" That is a repeatable edge worth a feed." if gain >= 0.5 else
                                         " Small: the first move was mostly gone by the time of any print after the "
                                         "headline, so a faster feed would not have caught it." if gain >= 0.15 else
                                         " Nothing: the jump was over before the first print after receipt, so paying "
                                         "for speed buys nothing here."))})
        leg = slow["horizons"][leg_key] if slow else None
        if leg and leg["median"] is not None:
            good = leg["median"] > 0 and (leg["hit_rate"] or 0) >= 0.5
            verdicts.append({"state": "pass" if good else "fail",
                             "text": (f"The second leg: entering at {SECOND_LEG[0]:.0f} s and holding to "
                                      f"{HORIZON_WORDS[SECOND_LEG[1]]} made a median {leg['median']:+.2f}% after costs, "
                                      f"positive {100 * leg['hit_rate']:.0f}% of the time over {leg['n']} events. "
                                      + ("A second leg has been there to take: that is the trade to build on, not the "
                                         "first second." if good else
                                         "No second leg on average: after the first move, the news was in the price."))})
    halted = sum(1 for r in done if r["result"].get("halted"))
    if halted:
        verdicts.append({"state": "info", "text": f"{halted} event{'s' if halted != 1 else ''} met a trading halt: the order "
                                                  "would have filled at the reopening, well away from the last print."})
    if skipped:
        verdicts.append({"state": "info", "text": f"{len(skipped)} event{'s' if len(skipped) != 1 else ''} could not be "
                                                  "replayed (no prints around the headline, usually news on a stock that "
                                                  "was not trading at the time)."})
    if done and all(r["result"].get("resolution_s", 1.0) >= 1.0 for r in done):
        verdicts.append({"state": "info", "text": "The prints are stamped to the second, so the 100 ms and 500 ms rows are "
                                                  "measured at the first print of the next whole second and usually agree; "
                                                  "telling them apart needs a feed stamped to the millisecond."})
    return {"events": n, "skipped": len(skipped), "halted": halted, "table": table, "verdicts": verdicts,
            "offsets": list(offsets), "horizons": keys}


# --- the harness --------------------------------------------------------------------------------------------------

def demo_prints(symbol: str, level: float, start_t: float, end_t: float, jump_at: float | None = None,
                jump: float = 0.0) -> pd.DataFrame:
    """Made-up prints for demo data: one every two seconds around `level` (the stock's last close), the same for
    a symbol and moment every time, with the item's made-up move landing as a real news move does: most of it in
    the first second, the rest over the next two minutes. Made for any moment, so the screen can be learned on a
    weekend too."""
    import hashlib
    if not level or level <= 0 or end_t <= start_t:
        return prints_frame([])
    first = int(start_t // 2) * 2
    n = int((end_t - first) // 2) + 1
    seed = int(hashlib.sha256(f"prints|{symbol}|{first}".encode()).hexdigest()[:8], 16)
    rng = np.random.default_rng(seed)
    steps = 0.0004 * level * rng.standard_normal(n)
    bounce = 0.0005 * level * np.where(np.arange(n) % 2 == 0, 1, -1)   # the bid-ask bounce, for the spread estimate
    path = level + np.cumsum(steps) + bounce
    rows = []
    for i in range(n):
        ts = float(first + 2 * i)
        if ts < start_t or ts > end_t:
            continue
        price = float(path[i])
        if jump_at is not None and jump and ts >= jump_at:
            since = ts - jump_at
            share = 0.7 if since < 1 else 0.7 + 0.2 * min(1.0, since / 10) + 0.1 * min(1.0, since / 120)
            price *= 1 + jump * share
        rows.append({"t": ts, "price": round(max(price, 0.01), 2), "volume": 100.0})
    return prints_frame(rows)


class NewsReplay:
    """Runs replays in the background, keeps every event's rows, and serves the record to the live assessment."""

    def __init__(self, svc: Any, feed: Any = None):
        self.svc = svc
        self.feed = feed
        self._job = OneJob("news-replay", self._keep)
        self._latest: dict[str, Any] | None = None
        self._loaded = False
        self._lock = threading.Lock()
        self._record_cache: dict[str, tuple[float, dict[str, Any] | None]] = {}

    @property
    def mode(self) -> str:
        return self.svc.market.info.mode

    def _key(self) -> str:
        return f"live:replay:{self.mode}"

    def _keep(self, result: dict[str, Any], job: dict[str, Any]) -> None:
        result["job_id"] = job["id"]
        from .service import clean
        try:
            self.svc.db.cache_put(self._key(), clean(result))
        except Exception as exc:                      # noqa: BLE001 - kept in memory regardless
            log.warning("News replay not saved: %s", exc)
        self._latest = clean(result)
        with self._lock:
            self._record_cache.clear()

    # --- what can be replayed ----------------------------------------------------------------------------------
    def reach(self) -> dict[str, Any]:
        if self.mode == "demo":
            return {"days": 7, "resolution_s": 2.0, "note": "Demo prints are made up, one every two seconds, and the made-up "
                                                            "move lands mostly in the first second: the verdicts here are "
                                                            "about the demo, not a market."}
        fn = getattr(self.svc.market.provider, "tick_reach", None)
        return dict(fn()) if callable(fn) else {"days": 0, "resolution_s": None,
                                                "note": f"{self.svc.market.info.label} gives no trade prints."}

    def events_from_feed(self, days: float | None = None, limit: int = MAX_EVENTS) -> list[dict[str, Any]]:
        """The feed's items of the last few days (as far as the prints reach) that are at least half an hour old
        (younger ones have no 30-minute horizon yet), one event per stock per half hour, newest first. Items with
        no publication time cannot be replayed and are left out."""
        from .livenews import item_row, parse_time
        reach = self.reach()
        days = min(days or reach["days"] or 0, reach["days"] or 0)
        if days <= 0:
            return []
        now = datetime.now(timezone.utc)
        since = (now - timedelta(days=days)).isoformat(timespec="seconds")
        until = (now - timedelta(minutes=MIN_AGE_MINUTES)).isoformat(timespec="seconds")
        rows = self.svc.db.query("SELECT * FROM live_news WHERE published_at >= ? AND published_at <= ? AND symbols != '[]' "
                                 "ORDER BY published_at DESC LIMIT 2000", (since, until))
        out: list[dict[str, Any]] = []
        last_by_symbol: dict[str, datetime] = {}
        universe_all = getattr(self.svc.settings, "live_universe", "large") == "all"
        universe = self.feed.universe() if self.feed is not None and not universe_all else None
        for r in rows:
            it = item_row(r)
            when = parse_time(it.get("published_at"))
            if when is None:
                continue
            for sym in it["symbols"][:3]:
                if universe is not None and sym not in universe:
                    continue
                prev = last_by_symbol.get(sym)
                if prev is not None and abs((prev - when).total_seconds()) < MERGE_MINUTES * 60:
                    continue
                last_by_symbol[sym] = when
                out.append({"symbol": sym, "published_at": when.isoformat(), "headline": it["headline"],
                            "summary": it.get("summary") or "", "source": it["source"], "news_id": it["id"],
                            "kind": it.get("kind"), "extra": it.get("extra") or {}, "tone": it.get("tone")})
                if len(out) >= limit:
                    return out
        return out

    # --- the run -----------------------------------------------------------------------------------------------
    def start(self, events: list[dict[str, Any]], *, feed_latency_s: float = DEFAULT_FEED_LATENCY_S,
              spread_bps: float | None = None, slippage_bps: float = DEFAULT_SLIPPAGE_BPS,
              offsets: tuple[float, ...] = OFFSETS, horizons: tuple[int, ...] = HORIZONS) -> dict[str, Any]:
        if not events:
            raise ValueError("Nothing to replay: listen to Live news for a day or two first, or add an event by hand.")
        if self.reach()["days"] <= 0:
            raise DataError(self.reach()["note"])
        params = {"feed_latency_s": float(feed_latency_s), "spread_bps": spread_bps, "slippage_bps": float(slippage_bps),
                  "offsets": tuple(offsets), "horizons": tuple(horizons)}

        def work(progress):
            return self.run(events, params, progress=progress)
        return self._job.start({"events": len(events)}, len(events) + 1, work)

    def stop(self) -> dict[str, Any]:
        return self._job.stop()

    def status(self) -> dict[str, Any]:
        if not self._loaded:
            self._loaded = True
            if self._latest is None:
                try:
                    self._latest = self.svc.db.cache_get(self._key())
                except Exception:                     # noqa: BLE001
                    self._latest = None
        return {**self._job.status_brief(), "result": self._latest, "reach": self.reach(), "mode": self.mode,
                "defaults": {"feed_latency_s": DEFAULT_FEED_LATENCY_S, "slippage_bps": DEFAULT_SLIPPAGE_BPS,
                             "spread_bps": DEFAULT_SPREAD_BPS, "offsets": list(OFFSETS), "horizons": list(HORIZONS)},
                "feed_events": len(self.events_from_feed(limit=MAX_EVENTS))}

    def run(self, events: list[dict[str, Any]], params: dict[str, Any], progress: Any = None) -> dict[str, Any]:
        """Replay every event in turn (inline; `start` wraps this in the background job) and aggregate."""
        from .livenews import parse_time
        run_id = int(self.svc.db.one("SELECT COALESCE(MAX(run_id), 0) + 1 AS n FROM news_replays WHERE mode = ?",
                                     (self.mode,))["n"])
        results = []
        for i, ev in enumerate(events):
            if progress is not None:
                progress(i, len(events) + 1, f"{ev['symbol']} {str(ev.get('published_at'))[:16]}")
            when = parse_time(ev.get("published_at"))
            cat = classify(ev.get("headline") or "", ev.get("summary") or "", kind=ev.get("kind"), extra=ev.get("extra") or {},
                           tone=ev.get("tone"))
            direction = int(ev.get("direction") or 0) or int(cat["direction"])
            entry = {"symbol": ev["symbol"], "published_at": when.isoformat() if when else None,
                     "headline": (ev.get("headline") or "")[:300], "source": ev.get("source") or "manual",
                     "news_id": ev.get("news_id"), "catalyst": cat["catalyst"], "catalyst_label": cat["label"],
                     "direction": direction, "result": None}
            if when is None:
                entry["result"] = {"skipped": "no publication time"}
            elif direction == 0:
                entry["result"] = {"skipped": "the headline gives no direction to trade (neither the kind of news nor "
                                              "its tone says which way)"}
            else:
                try:
                    entry["result"] = self._replay_one(ev["symbol"], when, direction, params, ev)
                except (DataError, ValueError) as exc:
                    entry["result"] = {"skipped": redact(str(exc))[:300]}
                except Exception as exc:              # noqa: BLE001 - one event never stops the run
                    log.exception("News replay: %s could not be replayed", ev["symbol"])
                    entry["result"] = {"skipped": f"an unexpected {type(exc).__name__}"}
            self._store(run_id, entry, params)
            results.append(entry)
        if progress is not None:
            progress(len(events), len(events) + 1, "summing up")
        summary = aggregate(results, params["offsets"], params["horizons"])
        by_kind: dict[str, dict[str, Any]] = {}
        for ev in results:
            by_kind.setdefault(ev["catalyst"], []).append(ev)
        kinds = []
        for key, evs in by_kind.items():
            agg = aggregate(evs, params["offsets"], params["horizons"])
            leg = next((row for row in agg["table"] if row["offset_s"] == SECOND_LEG[0]), None)
            cell = (leg or {}).get("horizons", {}).get(str(SECOND_LEG[1]), {})
            kinds.append({"catalyst": key, "label": CATALYSTS.get(key, {}).get("label", key), "events": agg["events"],
                          "median_net_pct": cell.get("median"), "hit_rate": cell.get("hit_rate"),
                          "median_jump_pct": _median([e["result"]["jump_1m_pct"] for e in evs if e["result"] and not e["result"].get("skipped")])})
        kinds.sort(key=lambda k: -k["events"])
        self._prune()
        return {**summary, "run_id": run_id, "built_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                "mode": self.mode, "params": {**params, "offsets": list(params["offsets"]), "horizons": list(params["horizons"])},
                "reach": self.reach(), "kinds": kinds, "results": results}

    def _replay_one(self, symbol: str, when: datetime, direction: int, params: dict[str, Any],
                    ev: dict[str, Any]) -> dict[str, Any]:
        start = when - timedelta(minutes=BEFORE_MINUTES)
        end = when + timedelta(minutes=AFTER_MINUTES)
        if end > datetime.now(timezone.utc):
            end = datetime.now(timezone.utc)
        prints = self._prints(symbol, start, end, when, ev)
        close = self._close_on(symbol, when)
        return replay_event(prints, when.timestamp(), direction, feed_latency_s=params["feed_latency_s"],
                            offsets=params["offsets"], horizons=params["horizons"], spread_bps=params["spread_bps"],
                            slippage_bps=params["slippage_bps"], close_price=close)

    def _prints(self, symbol: str, start: datetime, end: datetime, when: datetime, ev: dict[str, Any]) -> pd.DataFrame:
        if self.mode == "demo":
            daily = self.svc.market.bars(symbol, 0.3)
            level = float(daily["close"].iloc[-1]) if daily is not None and not daily.empty else 0.0
            move = float((ev.get("extra") or {}).get("demo_move") or 0.0)
            return demo_prints(symbol, level, start.timestamp(), end.timestamp(), jump_at=when.timestamp(), jump=move)
        fetch = getattr(self.svc.market.provider, "ticks", None)
        if fetch is None:
            raise DataError(f"{self.svc.market.info.label} gives no trade prints")
        return self.svc.market._call(f"ticks {symbol}", lambda: fetch(symbol, start, end))

    def _close_on(self, symbol: str, when: datetime) -> float | None:
        """The close of the session the headline fell in (or the next session's, for news after the close)."""
        try:
            df = self.svc.market.bars(symbol, 0.2)
        except Exception:                             # noqa: BLE001 - no close: the "close" horizon stays empty
            return None
        if df is None or df.empty:
            return None
        local = when.astimezone(NY)
        day = local.date() if (local.hour, local.minute) < SESSION_CLOSE else local.date() + timedelta(days=1)
        dates = pd.to_datetime(df["date"]).dt.date
        rows = df[dates >= day]
        if rows.empty:
            return None
        bar = rows.iloc[0]
        if "bar_complete" in df.columns and not bool(bar["bar_complete"]):
            return None
        return float(bar["close"])

    def _store(self, run_id: int, entry: dict[str, Any], params: dict[str, Any]) -> None:
        r = entry.get("result") or {}
        from .service import clean
        try:
            with self.svc.db.tx() as cur:
                cur.execute("""INSERT INTO news_replays (mode, run_id, symbol, news_id, published_at, headline, source,
                               catalyst, direction, feed_latency_s, spread_bps, slippage_bps, ref_price, jump_1m_pct, halted,
                               resolution_s, skipped, result_json, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                               ?, ?, ?, ?, ?, ?)
                               ON CONFLICT(mode, symbol, published_at, run_id) DO NOTHING""",
                            (self.mode, run_id, entry["symbol"], entry.get("news_id"), entry.get("published_at") or "",
                             entry.get("headline") or "", entry.get("source") or "", entry["catalyst"], entry["direction"],
                             params["feed_latency_s"], r.get("spread_bps", params["spread_bps"]), params["slippage_bps"],
                             r.get("ref_price"), r.get("jump_1m_pct"), int(bool(r.get("halted"))), r.get("resolution_s"),
                             r.get("skipped"), json.dumps(clean(r)), datetime.now(timezone.utc).isoformat(timespec="seconds")))
        except Exception as exc:                      # noqa: BLE001 - a row that cannot be kept is still reported
            log.warning("News replay row not saved: %s", redact(str(exc))[:200])

    def _prune(self) -> None:
        cutoff = (datetime.now(timezone.utc) - timedelta(days=KEEP_DAYS)).isoformat(timespec="seconds")
        try:
            with self.svc.db.tx() as cur:
                cur.execute("DELETE FROM news_replays WHERE created_at < ?", (cutoff,))
        except Exception:                             # noqa: BLE001
            log.info("News replays not pruned")

    # --- the record the live assessment reads --------------------------------------------------------------------
    def record_for(self, catalyst: str) -> dict[str, Any] | None:
        """What entering at 30 s and holding 30 minutes has made on this kind of news, from the latest replay of each
        event (a re-run replaces the older rows of the same event). Cached for a minute."""
        with self._lock:
            hit = self._record_cache.get(catalyst)
            if hit and time.time() - hit[0] < 60:
                return hit[1]
        try:
            rows = self.svc.db.query(
                "SELECT result_json, jump_1m_pct FROM news_replays r WHERE mode = ? AND catalyst = ? AND skipped IS NULL AND "
                "id IN (SELECT MAX(id) FROM news_replays WHERE mode = ? GROUP BY symbol, published_at)",
                (self.mode, catalyst, self.mode))
        except Exception:                             # noqa: BLE001 - no table yet
            rows = []
        nets, caught, jumps = [], [], []
        for r in rows:
            try:
                res = json.loads(r["result_json"])
            except (TypeError, ValueError):
                continue
            row = next((x for x in res.get("offsets", []) if x.get("offset_s") == SECOND_LEG[0] and x.get("fill")), None)
            if not row:
                continue
            net = (row.get("net") or {}).get(str(SECOND_LEG[1]))
            if isinstance(net, (int, float)):
                nets.append(net)
            if isinstance(row.get("captured"), (int, float)):
                caught.append(row["captured"])
            if isinstance(r["jump_1m_pct"], (int, float)):
                jumps.append(r["jump_1m_pct"])
        out = None
        if nets:
            out = {"catalyst": catalyst, "label": CATALYSTS.get(catalyst, {}).get("label", catalyst), "cases": len(nets),
                   "median_net_pct": _median(nets), "mean_net_pct": _mean(nets), "hit_rate": _hit(nets),
                   "median_captured": _median(caught), "median_jump_pct": _median(jumps)}
        with self._lock:
            self._record_cache[catalyst] = (time.time(), out)
        return out


__all__ = ["OFFSETS", "HORIZONS", "SECOND_LEG", "prints_frame", "replay_event", "aggregate", "estimate_spread_bps",
           "halt_gaps", "resolution_of", "demo_prints", "NewsReplay"]
