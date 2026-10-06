"""Where historical option prices would come from, for backtesting option strategies.

An honest option backtest needs the bid and ask of every contract on every past day. None of
the connected sources (Tradier, Robinhood, demo) provides that, so the only provider here
says so. Option backtests are never approximated by pricing past options with today's
volatility or a model volatility: the result would look precise and mean nothing, because
volatility, skew and spreads on those days are exactly what such a test would need to get
right.

A real source (an options data vendor's end-of-day files, for example) can be added by
implementing HistoricalOptionDataProvider and switching on FEATURE_OPTIONS_BACKTEST.
"""

from __future__ import annotations

from datetime import date
from typing import Any, Protocol


class OptionHistoryUnavailable(RuntimeError):
    pass


class HistoricalOptionDataProvider(Protocol):
    name: str

    def available(self) -> bool: ...
    def expirations_on(self, symbol: str, day: date) -> list[str]: ...
    def chain_on(self, symbol: str, day: date, expiration: str) -> list[dict[str, Any]]: ...


class NoHistoricalOptionData:
    """The provider used when no source of past option prices is connected."""
    name = "none"
    reason = ("No connected data source provides historical option prices (past bids and asks for each contract), "
              "so option strategies cannot be backtested honestly. Stock strategies can be.")

    def available(self) -> bool:
        return False

    def expirations_on(self, symbol: str, day: date) -> list[str]:
        raise OptionHistoryUnavailable(self.reason)

    def chain_on(self, symbol: str, day: date, expiration: str) -> list[dict[str, Any]]:
        raise OptionHistoryUnavailable(self.reason)


def provider_for(settings: Any) -> HistoricalOptionDataProvider:
    return NoHistoricalOptionData()
