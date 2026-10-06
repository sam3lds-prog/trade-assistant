"""Market-data providers. All are read-only."""

from __future__ import annotations

from ..config import Settings
from .base import DataError, MarketData, ProviderInfo
from .demo import DemoData
from .tradier import TradierData


def make_provider(settings: Settings, interactive: bool = False) -> MarketData:
    """The data source chosen in settings. `interactive` allows a browser sign-in (Robinhood
    only); the running web app passes False so it never opens a browser on its own."""
    mode = settings.resolved_mode
    if mode == "live":
        return TradierData(settings.live_token, sandbox=False)
    if mode == "sandbox":
        return TradierData(settings.sandbox_token, sandbox=True)
    if mode == "robinhood":
        from .robinhood import make_robinhood
        # Robinhood supplies quotes and option chains. Long daily price history comes from
        # Tradier when a token is present, since Robinhood's history may not reach far enough.
        fallback: MarketData | None = None
        if settings.live_token:
            fallback = TradierData(settings.live_token, sandbox=False)
        elif settings.sandbox_token:
            fallback = TradierData(settings.sandbox_token, sandbox=True)
        return make_robinhood(settings.robinhood_signin_path, settings.robinhood_callback_port, interactive,
                              fallback, settings.robinhood_poll_seconds, url=settings.robinhood_url)
    return DemoData(live=True)


__all__ = ["DataError", "MarketData", "ProviderInfo", "DemoData", "TradierData", "make_provider"]
