"""Market access used by the rest of the app: quotes, cached daily bars, analysed chains."""

from __future__ import annotations

import time
from datetime import date, datetime, timedelta
from datetime import time as clock_time
from typing import Any

import pandas as pd

from .chain import ChainAnalysis, analyze_chain
from .config import Settings
from .db import Database
from .indicators import enrich
from .optionmath import NY
from .providers.base import BAR_COLUMNS, DataError, MarketData, display_symbol

BAR_REFRESH_SECONDS = 4 * 3600
PARTIAL_REFRESH_SECONDS = 15 * 60   # today's unfinished bar is refetched this often when it is wanted
SUSPECT_MOVE = 0.40  # a one-day move this large usually means an unadjusted split
# A daily bar counts as finished only when it was fetched after this time on its own date. The
# regular session ends at 4 p.m. New York time; the margin lets closing prints arrive. A bar
# fetched earlier stays unfinished, even after the close, until it is fetched again.
CLOSE_SETTLED = clock_time(16, 10)
PROVISIONAL, CONFIRMED = "PROVISIONAL", "CONFIRMED"


def settled_through(epoch_seconds: float) -> date:
    """The latest date whose daily bar was final at this moment."""
    moment = datetime.fromtimestamp(epoch_seconds, NY)
    return moment.date() if moment.time() >= CLOSE_SETTLED else moment.date() - timedelta(days=1)


def complete_mask(dates: pd.Series, fetched_at: float) -> pd.Series:
    """True for each bar that was finished when the bars were fetched."""
    return dates <= pd.Timestamp(settled_through(fetched_at))


def complete_only(bars: pd.DataFrame) -> pd.DataFrame:
    """The bars without an unfinished last bar. Frames without the marker are returned as they are."""
    if "bar_complete" not in bars.columns or bool(bars["bar_complete"].all()):
        return bars
    return bars.loc[bars["bar_complete"]].reset_index(drop=True)


def signal_status(bars: pd.DataFrame, position: int = -1) -> str:
    """CONFIRMED when the bar a signal comes from is finished, PROVISIONAL when it is still forming."""
    if "bar_complete" not in bars.columns or bars.empty:
        return CONFIRMED
    return CONFIRMED if bool(bars["bar_complete"].iloc[position]) else PROVISIONAL


class Market:
    def __init__(self, provider: MarketData, db: Database, settings: Settings):
        self.provider = provider
        self.db = db
        self.settings = settings
        self._clock: tuple[float, dict[str, Any]] | None = None

    @property
    def info(self):
        return self.provider.info

    # --- quotes ----------------------------------------------------------
    def quote(self, symbol: str) -> dict[str, Any]:
        symbol = display_symbol(symbol)
        quotes = self.provider.quotes([symbol])
        q = quotes.get(symbol)
        if not q:
            raise DataError(f"No quote found for {symbol}. Check the ticker.")
        return q

    def spot(self, symbol: str) -> float:
        return self.spot_from_quote(self.quote(symbol), symbol)

    def spot_from_quote(self, q: dict[str, Any], symbol: str) -> float:
        """The stock price to value options against, from a quote already fetched."""
        price = q.get("last")
        bid, ask = q.get("bid"), q.get("ask")
        if self.clock().get("state") == "open":
            # During market hours the bid/ask midpoint is fresher than the last trade.
            if bid and ask and ask > bid > 0 and (ask - bid) / ask < 0.01:
                price = 0.5 * (bid + ask)
        elif q.get("close"):
            # Outside regular hours option quotes are frozen at the close, so price them against
            # the closing stock price, not an after-hours quote.
            price = q["close"]
        if not price or price <= 0:
            raise DataError(f"No usable price for {symbol}")
        return float(price)

    # --- daily bars --------------------------------------------------------
    def bars(self, symbol: str, years: float = 3.0, complete: bool = False) -> pd.DataFrame:
        """Daily bars for about `years` years, oldest first, cached in SQLite. Each row says in
        `bar_complete` whether that day's bar was finished when it was fetched; during market
        hours the last row is today's bar so far. With `complete=True` that unfinished bar is
        left out, which is what studies and confirmed signals use."""
        symbol = display_symbol(symbol)
        now = time.time()
        today = datetime.now(NY).date()
        start = (today - timedelta(days=int(years * 365.25) + 5)).isoformat()
        end = today.isoformat()
        meta = self.db.one("SELECT * FROM bars_meta WHERE symbol = ?", (symbol,))
        fresh = (meta is not None and meta["source"] == self.info.mode
                 and now - meta["fetched_at"] < BAR_REFRESH_SECONDS
                 and meta["first_date"] <= (pd.Timestamp(start) + pd.Timedelta(days=7)).date().isoformat())
        if fresh and meta["last_date"] > settled_through(meta["fetched_at"]).isoformat():
            # The cache holds an unfinished bar. Fetch again once the day has closed, so the bar can
            # be confirmed, and every few minutes while the bar is wanted.
            if settled_through(now).isoformat() >= meta["last_date"]:
                fresh = False
            elif not complete and now - meta["fetched_at"] > PARTIAL_REFRESH_SECONDS:
                fresh = False
        if not fresh:
            frame = self.provider.history(symbol, start, end)
            if frame.empty:
                raise DataError(f"No price history returned for {symbol}")
            rows = [(symbol, d.strftime("%Y-%m-%d"), o, h, l, c, v) for d, o, h, l, c, v in
                    frame[BAR_COLUMNS].itertuples(index=False, name=None)]
            with self.db.tx() as cur:
                cur.execute("DELETE FROM bars WHERE symbol = ?", (symbol,))
                cur.executemany("INSERT INTO bars (symbol, date, open, high, low, close, volume) "
                                "VALUES (?, ?, ?, ?, ?, ?, ?)", rows)
                cur.execute("INSERT INTO bars_meta (symbol, source, first_date, last_date, fetched_at, adjustment) "
                            "VALUES (?, ?, ?, ?, ?, ?) ON CONFLICT(symbol) DO UPDATE SET source = excluded.source, "
                            "first_date = excluded.first_date, last_date = excluded.last_date, "
                            "fetched_at = excluded.fetched_at, adjustment = excluded.adjustment",
                            (symbol, self.info.mode, start, rows[-1][1], now,
                             getattr(self.info, "history_adjustment", "unknown")))
            fetched_at = now
        else:
            fetched_at = meta["fetched_at"]
        stored = self.db.query("SELECT date, open, high, low, close, volume FROM bars "
                               "WHERE symbol = ? AND date >= ? ORDER BY date", (symbol, start))
        df = pd.DataFrame([dict(r) for r in stored], columns=BAR_COLUMNS)
        df["date"] = pd.to_datetime(df["date"])
        df["bar_complete"] = complete_mask(df["date"], fetched_at)
        return complete_only(df) if complete else df

    def enriched(self, symbol: str, years: float = 3.0, complete: bool = False) -> pd.DataFrame:
        return enrich(self.bars(symbol, years, complete))

    def bar_info(self, symbol: str) -> dict[str, Any]:
        """Where and when the cached bars came from, and whether the last one is finished."""
        meta = self.db.one("SELECT * FROM bars_meta WHERE symbol = ?", (display_symbol(symbol),))
        if meta is None:
            return {"cached": False}
        settled = settled_through(meta["fetched_at"]).isoformat()
        partial = meta["last_date"] > settled
        when = datetime.fromtimestamp(meta["fetched_at"], NY)
        return {"cached": True, "source": meta["source"], "adjustment": meta["adjustment"],
                "fetched_at": when.isoformat(timespec="seconds"),
                "fetched_text": f"{when:%H:%M} New York time on {when:%b} {when.day}",
                "last_date": meta["last_date"], "last_bar_status": "PARTIAL" if partial else "COMPLETE",
                "settled_through": settled}

    @staticmethod
    def data_warnings(symbol: str, bars: pd.DataFrame) -> list[str]:
        warnings: list[str] = []
        if len(bars) < 260:
            warnings.append(f"{symbol}: only {len(bars)} daily bars, so 52-week measures are incomplete.")
        moves = bars["close"].pct_change().abs()
        jumps = bars.loc[moves > SUSPECT_MOVE, "date"]
        if len(jumps):
            days = ", ".join(d.strftime("%Y-%m-%d") for d in jumps.head(3))
            warnings.append(f"{symbol}: one-day move over {SUSPECT_MOVE:.0%} on {days}. "
                            "This may be a split that is not adjusted in the history; check before trusting "
                            "indicators or study results across that date.")
        return warnings

    # --- options ---------------------------------------------------------
    def expirations(self, symbol: str) -> list[str]:
        today = datetime.now(NY).date().isoformat()
        return [d for d in self.provider.expirations(display_symbol(symbol)) if d >= today]

    def chain(self, symbol: str, expiration: str) -> ChainAnalysis:
        symbol = display_symbol(symbol)
        contracts = self.provider.chain(symbol, expiration)
        if not contracts:
            raise DataError(f"No option chain returned for {symbol} {expiration}")
        return analyze_chain(symbol, expiration, contracts, self.spot(symbol), self.settings.risk_free_rate)

    def clock(self) -> dict[str, Any]:
        """Market state, cached for half a minute so pricing does not spend a request on it each time."""
        if self._clock is not None and time.time() - self._clock[0] < 30:
            return self._clock[1]
        try:
            value = self.provider.clock()
        except DataError:
            value = {"state": "unknown", "description": "Market clock unavailable", "date": "",
                     "next_change": "", "next_state": ""}
        self._clock = (time.time(), value)
        return value
