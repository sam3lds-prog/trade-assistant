"""Bars within the day, and bars grouped by hour, day, week, month or quarter (2.12).

Daily bars live in `market.bars`. This module adds the finer and coarser grains the interactive chart needs:

    within the day   fetched from the data source (Tradier timesales at 1, 5 or 15 minutes; Robinhood at 5
                     minutes or an hour; made up on demo data from each day's open, high, low and close) and
                     kept in `intraday_bars`, keyed by symbol and the source's interval;
    grouped          built from the finest bars held: 15-minute and hourly bars from 5-minute ones, weekly,
                     monthly and quarterly bars from daily ones.

Every bar carries a UTC time in seconds (`t`), the New York time the chart shows it at (`tl`, the same
moment written as if New York were UTC, which is how the chart library draws a local clock), the prices, the
volume and, when the source gives it, the volume-weighted average price. Nothing here places an order.
"""

from __future__ import annotations

import logging
import math
import threading
import time
from datetime import datetime, timedelta, timezone
from typing import Any

import numpy as np
import pandas as pd

from .optionmath import NY
from .providers.base import DataError

log = logging.getLogger(__name__)

# What a bar size is called on the chart, and how many seconds it holds (0: a calendar period).
GROUPS: dict[str, dict[str, Any]] = {
    "1m": {"label": "1 minute", "seconds": 60, "kind": "intraday", "source": "1m"},
    "5m": {"label": "5 minutes", "seconds": 300, "kind": "intraday", "source": "5m"},
    "15m": {"label": "15 minutes", "seconds": 900, "kind": "intraday", "source": "5m"},
    "1h": {"label": "1 hour", "seconds": 3600, "kind": "intraday", "source": "5m"},
    "1d": {"label": "1 day", "seconds": 86400, "kind": "daily", "source": "1d"},
    "1w": {"label": "1 week", "seconds": 7 * 86400, "kind": "grouped", "source": "1d"},
    "1M": {"label": "1 month", "seconds": 0, "kind": "grouped", "source": "1d"},
    "1Q": {"label": "1 quarter", "seconds": 0, "kind": "grouped", "source": "1d"},
}
# How far back the chart can reach for each range word, in calendar days, and the groups that suit it.
RANGES: dict[str, dict[str, Any]] = {
    "1d": {"days": 1, "label": "1 day", "default": "5m"},
    "5d": {"days": 7, "label": "5 days", "default": "15m"},
    "1m": {"days": 31, "label": "1 month", "default": "1h"},
    "3m": {"days": 92, "label": "3 months", "default": "1d"},
    "6m": {"days": 183, "label": "6 months", "default": "1d"},
    "1y": {"days": 366, "label": "1 year", "default": "1d"},
    "2y": {"days": 731, "label": "2 years", "default": "1d"},
    "5y": {"days": 1827, "label": "5 years", "default": "1w"},
    "10y": {"days": 3653, "label": "10 years", "default": "1M"},
}
INTRADAY_COLUMNS = ["t", "open", "high", "low", "close", "volume", "vwap"]
FRESH_SECONDS = 60            # while the market trades, the latest bars are refetched this often
QUIET_SECONDS = 15 * 60       # outside trading, this often
HOLD_DAYS = 45                # bars within the day are kept this long
SESSION_OPEN = (9, 30)
SESSION_CLOSE = (16, 0)


def local_shift(t: int) -> int:
    """The UTC seconds that, drawn as UTC, read as New York time: `t` plus New York's offset then."""
    return int(t + datetime.fromtimestamp(t, NY).utcoffset().total_seconds())


def intraday_frame(rows: list[dict[str, Any]]) -> pd.DataFrame:
    """Bars within the day as a clean frame: typed, sorted, one bar per moment, bad rows dropped."""
    if not rows:
        return pd.DataFrame(columns=INTRADAY_COLUMNS)
    df = pd.DataFrame(rows)
    for col in INTRADAY_COLUMNS:
        if col not in df.columns:
            df[col] = None
    df = df[INTRADAY_COLUMNS].copy()
    for col in INTRADAY_COLUMNS:
        df[col] = pd.to_numeric(df[col], errors="coerce")
    df = df.dropna(subset=["t", "open", "high", "low", "close"])
    df = df[(df["close"] > 0) & (df["open"] > 0) & (df["high"] >= df["low"])]
    df["t"] = df["t"].astype("int64")
    df["volume"] = df["volume"].fillna(0.0)
    return df.sort_values("t").drop_duplicates("t", keep="last").reset_index(drop=True)


def regular_session(df: pd.DataFrame) -> pd.DataFrame:
    """Only the bars of the regular session (9:30 to 16:00 New York)."""
    if df.empty:
        return df
    local = pd.to_datetime(df["t"], unit="s", utc=True).dt.tz_convert(NY)
    minutes = local.dt.hour * 60 + local.dt.minute
    keep = (minutes >= SESSION_OPEN[0] * 60 + SESSION_OPEN[1]) & (minutes < SESSION_CLOSE[0] * 60 + SESSION_CLOSE[1])
    return df.loc[keep].reset_index(drop=True)


def group_intraday(df: pd.DataFrame, seconds: int) -> pd.DataFrame:
    """Finer bars grouped into `seconds`-long ones, each starting on the session's grid (9:30, 10:30, ...)
    so an hourly bar is 9:30-10:30 rather than 9:00-10:00. Bars never cross a day."""
    if df.empty or seconds <= 0:
        return df.copy()
    local = pd.to_datetime(df["t"], unit="s", utc=True).dt.tz_convert(NY)
    since_open = ((local.dt.hour - SESSION_OPEN[0]) * 60 + (local.dt.minute - SESSION_OPEN[1])) * 60 + local.dt.second
    g = df.assign(_day=local.dt.strftime("%Y-%m-%d"), _slot=(since_open // seconds).astype(int),
                  _pv=df["vwap"].fillna(0.0) * df["volume"].fillna(0.0),
                  _pvol=df["volume"].where(df["vwap"].notna(), 0.0))
    agg = g.groupby(["_day", "_slot"], sort=True).agg(
        t=("t", "min"), open=("open", "first"), high=("high", "max"), low=("low", "min"), close=("close", "last"),
        volume=("volume", "sum"), _pv=("_pv", "sum"), _pvol=("_pvol", "sum"))
    agg["vwap"] = np.where(agg["_pvol"] > 0, agg["_pv"] / agg["_pvol"].replace(0, np.nan), np.nan)
    return agg.reset_index(drop=True)[INTRADAY_COLUMNS]


def group_daily(bars: pd.DataFrame, group: str) -> pd.DataFrame:
    """Daily bars grouped by week (ending Friday), month or quarter. Each bar is dated on its first trading
    day and says whether its period has finished (`complete`)."""
    if bars.empty:
        return pd.DataFrame(columns=["date", "open", "high", "low", "close", "volume", "complete"])
    rule = {"1w": "W-FRI", "1M": "ME", "1Q": "QE"}[group]
    df = bars.copy()
    df["date"] = pd.to_datetime(df["date"])
    done = df["bar_complete"] if "bar_complete" in df.columns else pd.Series(True, index=df.index)
    df = df.assign(_done=done.astype(bool))
    g = df.set_index("date").resample(rule, label="right", closed="right")
    out = g.agg(first=("open", lambda s: s.index.min()), open=("open", "first"), high=("high", "max"),
                low=("low", "min"), close=("close", "last"), volume=("volume", "sum"), done=("_done", "all"),
                n=("close", "size"))
    out = out[out["n"] > 0]
    ends = out.index
    last_date = df["date"].max()
    today = pd.Timestamp(datetime.now(NY).date())
    # A period is finished when its last calendar day has passed and its last bar was finished.
    complete = (ends < today) & out["done"]
    out = out.assign(date=pd.to_datetime(out["first"]), complete=complete.to_numpy())
    if len(out) and ends[-1] >= today and last_date >= pd.Timestamp(datetime.now(NY).date()):
        out.iloc[-1, out.columns.get_loc("complete")] = False
    return out[["date", "open", "high", "low", "close", "volume", "complete"]].reset_index(drop=True)


def demo_intraday(daily: pd.DataFrame, symbol: str, days: int = HOLD_DAYS, minutes: int = 5) -> pd.DataFrame:
    """Made-up bars within the day for demo data: each day's path is a random walk pinned to that day's open
    and close, stretched to touch its high and low, with volume heaviest at the open and the close. The same
    for a symbol and day every time."""
    if daily.empty:
        return intraday_frame([])
    per_day = int((SESSION_CLOSE[0] * 60 + SESSION_CLOSE[1] - SESSION_OPEN[0] * 60 - SESSION_OPEN[1]) / minutes)
    rows: list[dict[str, Any]] = []
    tail = daily.tail(days)
    for d, o, h, lo, c, v in tail[["date", "open", "high", "low", "close", "volume"]].itertuples(index=False, name=None):
        day = pd.Timestamp(d).date()
        seed = int.from_bytes(f"intraday|{symbol}|{day.isoformat()}".encode(), "little") % (2 ** 32)
        rng = np.random.default_rng(seed)
        steps = rng.standard_normal(per_day)
        walk = np.cumsum(steps)
        bridge = walk - np.arange(1, per_day + 1) / per_day * walk[-1]          # a walk pinned to end at 0
        path = o + (c - o) * np.arange(1, per_day + 1) / per_day + bridge * (h - lo) / (4 * math.sqrt(per_day))
        path = np.concatenate([[o], path])
        lo_p, hi_p = path.min(), path.max()
        if hi_p > lo_p:
            path = lo + (path - lo_p) * (h - lo) / (hi_p - lo_p)               # touch the day's high and low
        path[0], path[-1] = o, c
        shape = 1.0 + 1.5 * (np.linspace(-1, 1, per_day) ** 2)                 # U-shaped volume
        vols = v * shape / shape.sum()
        start = datetime(day.year, day.month, day.day, SESSION_OPEN[0], SESSION_OPEN[1], tzinfo=NY)
        day_rows = []
        for i in range(per_day):
            a, b = path[i], path[i + 1]
            wiggle = abs(rng.standard_normal()) * (h - lo) / per_day
            hi_b, lo_b = min(max(a, b) + wiggle, h), max(min(a, b) - wiggle, lo)
            t = int((start + timedelta(minutes=minutes * i)).timestamp())
            day_rows.append({"t": t, "open": round(a, 2), "high": round(hi_b, 2), "low": round(lo_b, 2),
                             "close": round(b, 2), "volume": float(round(vols[i])), "vwap": round((a + b + hi_b + lo_b) / 4, 2)})
        # The day's high and low are touched by the bars that come nearest them.
        day_rows[int(np.argmax([r["high"] for r in day_rows]))]["high"] = round(h, 2)
        day_rows[int(np.argmin([r["low"] for r in day_rows]))]["low"] = round(lo, 2)
        rows += day_rows
    return intraday_frame(rows)


class Intraday:
    """Bars within the day for the app: fetched from the source, kept in the database, grouped on demand."""

    def __init__(self, market: Any, db: Any):
        self.market = market
        self.db = db
        self._lock = threading.Lock()
        self._locks: dict[str, threading.Lock] = {}
        self.last_error: dict[str, str] = {}

    # --- what the source can do -------------------------------------------------------------------------------
    def offered(self) -> dict[str, Any]:
        """Which source intervals exist and how far back they reach, in days."""
        provider = self.market.provider
        if self.market.info.mode == "demo":
            return {"1m": 0, "5m": HOLD_DAYS, "note": "Demo bars within the day are made up from each day's open, "
                                                      "high, low and close."}
        reach = getattr(provider, "intraday_reach", None)
        return dict(reach() if callable(reach) else {"1m": 0, "5m": 0})

    def available_groups(self, days: float) -> list[str]:
        """The groups a range of `days` calendar days can be shown in, finest first."""
        offer = self.offered()
        out = []
        for name, g in GROUPS.items():
            if g["kind"] == "intraday":
                if days <= offer.get(g["source"], 0):
                    out.append(name)
            elif g["kind"] == "daily":
                out.append(name)
            elif name == "1w" and days >= 21:
                out.append(name)
            elif name == "1M" and days >= 92:
                out.append(name)
            elif name == "1Q" and days >= 366:
                out.append(name)
        return out

    # --- fetching and keeping -----------------------------------------------------------------------------------
    def _fetch(self, symbol: str, interval: str, start: datetime, end: datetime) -> pd.DataFrame:
        provider = self.market.provider
        if self.market.info.mode == "demo":
            daily = self.market.bars(symbol, 0.3)
            made = demo_intraday(daily, symbol)
            return made[(made["t"] >= int(start.timestamp())) & (made["t"] <= int(end.timestamp()))].reset_index(drop=True)
        fetch = getattr(provider, "intraday", None)
        if fetch is None:
            raise DataError(f"{self.market.info.label} does not provide bars within the day")
        return self.market._call(f"intraday {symbol} {interval}",
                                 lambda: fetch(symbol, interval, start, end))

    def bars(self, symbol: str, interval: str, days: float) -> pd.DataFrame:
        """The last `days` days of `interval` bars (a source interval: 1m or 5m), from the database when they
        are fresh enough, else fetched and kept. Regular session only, and nothing after this moment.

        Freshness is judged by what was asked for last time (`asked_from`, kept with the bars), not by the
        first bar held: the first bar sits at 9:30 of a trading day, so a span's first calendar day often has
        none. One fetch runs at a time for a symbol and interval; the database lock is taken only to read
        and write, never across the fetch."""
        interval = interval if interval in ("1m", "5m") else "5m"
        now = time.time()
        lock = self._lock_for(symbol, interval)
        with lock:
            meta = self.db.one("SELECT * FROM intraday_meta WHERE symbol = ? AND interval = ?", (symbol, interval))
            trading = self._trading_now()
            reach = self.offered().get(interval, 0)
            wanted = min(days, reach) if reach else days
            held = meta is not None and meta["source"] == self.market.info.mode
            asked_from = float(meta["asked_from"]) if held and meta["asked_from"] is not None else None
            covers = asked_from is not None and asked_from <= now - wanted * 86400 + 3600
            fresh = held and covers and now - meta["fetched_at"] < (FRESH_SECONDS if trading else QUIET_SECONDS)
            if not fresh:
                if self.market.info.mode != "demo" and reach <= 0:
                    raise DataError(f"{self.market.info.label} does not offer {GROUPS[interval]['label']} bars")
                span = min(max(days, 2), reach) if reach else days
                end = datetime.now(timezone.utc)
                start = end - timedelta(days=span + 1)
                from_t = start.timestamp()
                # Refresh only what may have changed: from the start of the last kept day when the rest is held.
                if held and covers:
                    start = datetime.fromtimestamp(meta["last_t"], timezone.utc).astimezone(NY).replace(
                        hour=0, minute=0, second=0, microsecond=0)
                    from_t = min(asked_from, from_t)
                try:
                    frame = regular_session(self._fetch(symbol, interval, start, end))
                    self.last_error.pop(f"{symbol}:{interval}", None)
                except DataError as exc:
                    if not held:
                        raise
                    self.last_error[f"{symbol}:{interval}"] = str(exc)
                    frame = None
                if frame is not None and not frame.empty:
                    frame = frame[frame["t"] <= int(now)]
                    rows = [(symbol, interval, int(t), o, h, lo, c, v, None if (w is None or not math.isfinite(w)) else w)
                            for t, o, h, lo, c, v, w in frame[INTRADAY_COLUMNS].itertuples(index=False, name=None)]
                    keep_from = int(now - HOLD_DAYS * 86400)
                    with self.db.tx() as cur:
                        if not held:
                            cur.execute("DELETE FROM intraday_bars WHERE symbol = ? AND interval = ?", (symbol, interval))
                        cur.executemany("INSERT INTO intraday_bars (symbol, interval, t, open, high, low, close, volume, "
                                        "vwap) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?) ON CONFLICT(symbol, interval, t) DO "
                                        "UPDATE SET open = excluded.open, high = excluded.high, low = excluded.low, "
                                        "close = excluded.close, volume = excluded.volume, vwap = excluded.vwap", rows)
                        cur.execute("DELETE FROM intraday_bars WHERE symbol = ? AND interval = ? AND t < ?",
                                    (symbol, interval, keep_from))
                        first = cur.execute("SELECT MIN(t), MAX(t) FROM intraday_bars WHERE symbol = ? AND interval = ?",
                                            (symbol, interval)).fetchone()
                        cur.execute("INSERT INTO intraday_meta (symbol, interval, source, first_t, last_t, fetched_at, "
                                    "asked_from) VALUES (?, ?, ?, ?, ?, ?, ?) ON CONFLICT(symbol, interval) DO UPDATE SET "
                                    "source = excluded.source, first_t = excluded.first_t, last_t = excluded.last_t, "
                                    "fetched_at = excluded.fetched_at, asked_from = excluded.asked_from",
                                    (symbol, interval, self.market.info.mode, first[0], first[1], now, from_t))
                elif frame is not None and not held:
                    raise DataError(f"No bars within the day came back for {symbol}")
            since = int(now - days * 86400)
            stored = self.db.query("SELECT t, open, high, low, close, volume, vwap FROM intraday_bars WHERE symbol = ? "
                                   "AND interval = ? AND t >= ? AND t <= ? ORDER BY t", (symbol, interval, since, int(now)))
        df = pd.DataFrame([dict(r) for r in stored], columns=INTRADAY_COLUMNS)
        if df.empty:
            return intraday_frame([])
        df["t"] = df["t"].astype("int64")
        return df

    def _lock_for(self, symbol: str, interval: str) -> threading.Lock:
        with self._lock:
            return self._locks.setdefault(f"{symbol}:{interval}", threading.Lock())

    def _trading_now(self) -> bool:
        try:
            state = (self.market.clock() or {}).get("state")
        except Exception:                                 # noqa: BLE001 - no clock: assume trading
            return True
        return state in ("open", "premarket", "pre", "postmarket", "post", None)

    # --- the series for a chart ----------------------------------------------------------------------------------
    def series(self, symbol: str, group: str, days: float) -> pd.DataFrame:
        """Bars of `group` size covering the last `days` days. Within-the-day groups come back with `t` (UTC
        seconds); daily and grouped bars with `date`. Each frame has `complete`."""
        g = GROUPS[group]
        if g["kind"] == "intraday":
            base = self.bars(symbol, g["source"], days)
            out = group_intraday(base, g["seconds"]) if g["seconds"] > GROUPS[g["source"]]["seconds"] else base
            if out.empty:
                return out.assign(complete=pd.Series(dtype=bool))
            last_start = int(out["t"].iloc[-1])
            now = time.time()
            complete = np.ones(len(out), dtype=bool)
            if now < last_start + g["seconds"] and self._trading_now():
                complete[-1] = False                     # the bar still forming
            return out.assign(complete=complete)
        years = max(0.5, days / 365.25 + 0.3)
        daily = self.market.bars(symbol, years)
        if g["kind"] == "daily":
            out = daily.rename(columns={"bar_complete": "complete"})
            if "complete" not in out.columns:
                out = out.assign(complete=True)
            return out
        return group_daily(daily, group)


__all__ = ["GROUPS", "RANGES", "Intraday", "intraday_frame", "regular_session", "group_intraday", "group_daily",
           "demo_intraday", "local_shift"]
