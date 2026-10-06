"""Market access used by the rest of the app: quotes, cached daily bars, analysed chains."""

from __future__ import annotations

import time
from datetime import datetime, timedelta
from typing import Any

import pandas as pd

from .chain import ChainAnalysis, analyze_chain
from .config import Settings
from .db import Database
from .indicators import enrich
from .optionmath import NY
from .providers.base import BAR_COLUMNS, DataError, MarketData, display_symbol

BAR_REFRESH_SECONDS = 4 * 3600
SUSPECT_MOVE = 0.40  # a one-day move this large usually means an unadjusted split


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
    def bars(self, symbol: str, years: float = 3.0) -> pd.DataFrame:
        """Daily bars for about `years` years, oldest first. Cached in SQLite and refreshed
        every few hours. The last row can be today's unfinished bar during market hours."""
        symbol = display_symbol(symbol)
        today = datetime.now(NY).date()
        start = (today - timedelta(days=int(years * 365.25) + 5)).isoformat()
        end = today.isoformat()
        meta = self.db.one("SELECT * FROM bars_meta WHERE symbol = ?", (symbol,))
        fresh = (meta is not None and meta["source"] == self.info.mode
                 and time.time() - meta["fetched_at"] < BAR_REFRESH_SECONDS
                 and meta["first_date"] <= (pd.Timestamp(start) + pd.Timedelta(days=7)).date().isoformat())
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
                cur.execute("INSERT INTO bars_meta (symbol, source, first_date, last_date, fetched_at) "
                            "VALUES (?, ?, ?, ?, ?) ON CONFLICT(symbol) DO UPDATE SET source = excluded.source, "
                            "first_date = excluded.first_date, last_date = excluded.last_date, "
                            "fetched_at = excluded.fetched_at",
                            (symbol, self.info.mode, start, rows[-1][1], time.time()))
        stored = self.db.query("SELECT date, open, high, low, close, volume FROM bars "
                               "WHERE symbol = ? AND date >= ? ORDER BY date", (symbol, start))
        df = pd.DataFrame([dict(r) for r in stored], columns=BAR_COLUMNS)
        df["date"] = pd.to_datetime(df["date"])
        return df

    def enriched(self, symbol: str, years: float = 3.0) -> pd.DataFrame:
        return enrich(self.bars(symbol, years))

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
