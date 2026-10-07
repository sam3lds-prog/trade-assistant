"""Common shapes for market-data providers.

Providers are read-only by design: there is no order method anywhere in this package.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Protocol

import pandas as pd


class DataError(RuntimeError):
    """A market-data request failed in a way the user should see."""


@dataclass(frozen=True)
class ProviderInfo:
    mode: str            # live | sandbox | demo
    label: str
    realtime: bool       # False means quotes are delayed or synthetic
    has_greeks: bool
    note: str
    poll_seconds: float = 1.0   # shortest sensible gap between live quote refreshes for this source
    # What the source says about corporate actions in its daily history: "none" for synthetic
    # prices with no splits or dividends, "split" or "split_dividend" when the source states it,
    # "unknown" when it does not say. Unadjusted splits are also looked for in the data itself.
    history_adjustment: str = "unknown"


class MarketData(Protocol):
    info: ProviderInfo

    def quotes(self, symbols: list[str]) -> dict[str, dict[str, Any]]: ...
    def history(self, symbol: str, start: str, end: str) -> pd.DataFrame: ...
    def expirations(self, symbol: str) -> list[str]: ...
    def chain(self, symbol: str, expiration: str) -> list[dict[str, Any]]: ...
    def clock(self) -> dict[str, Any]: ...


BAR_COLUMNS = ["date", "open", "high", "low", "close", "volume"]


def api_symbol(symbol: str) -> str:
    """Tradier writes share classes with a slash: BRK.B -> BRK/B."""
    return symbol.strip().upper().replace(".", "/")


def display_symbol(symbol: str) -> str:
    return symbol.strip().upper().replace("/", ".")


def as_list(value: Any) -> list:
    """Tradier returns a bare object when there is one item, a list when several, null when none."""
    if value is None:
        return []
    if isinstance(value, list):
        return value
    return [value]


def dicts(value: Any) -> list[dict]:
    """`as_list`, keeping only the entries that are objects (drops nulls and stray strings)."""
    return [item for item in as_list(value) if isinstance(item, dict)]


def num(value: Any) -> float | None:
    """Float, or None for missing / non-numeric values (Tradier uses null and sometimes 'NaN')."""
    if value is None:
        return None
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    if out != out or out in (float("inf"), float("-inf")):
        return None
    return out


def epoch_ms_to_iso(value: Any) -> str | None:
    """Tradier quote timestamps are epoch milliseconds (older docs show seconds)."""
    ts = num(value)
    if ts is None or ts <= 0:
        return None
    if ts > 1e11:  # milliseconds
        ts /= 1000.0
    try:
        return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat()
    except (OverflowError, OSError, ValueError):
        return None


def normalize_quote(raw: dict[str, Any]) -> dict[str, Any]:
    return {
        "symbol": display_symbol(str(raw.get("symbol", ""))),
        "description": raw.get("description") or "",
        "type": raw.get("type") or "",
        "last": num(raw.get("last")),
        "bid": num(raw.get("bid")),
        "ask": num(raw.get("ask")),
        "bidsize": num(raw.get("bidsize")),
        "asksize": num(raw.get("asksize")),
        "prevclose": num(raw.get("prevclose")),
        "open": num(raw.get("open")),
        "high": num(raw.get("high")),
        "low": num(raw.get("low")),
        "close": num(raw.get("close")),
        "volume": num(raw.get("volume")),
        "average_volume": num(raw.get("average_volume")),
        "week_52_high": num(raw.get("week_52_high")),
        "week_52_low": num(raw.get("week_52_low")),
        "bid_time": epoch_ms_to_iso(raw.get("bid_date")),
        "ask_time": epoch_ms_to_iso(raw.get("ask_date")),
        "trade_time": epoch_ms_to_iso(raw.get("trade_date")),
    }


def normalize_contract(raw: dict[str, Any]) -> dict[str, Any]:
    greeks_raw = raw.get("greeks") or None
    greeks = None
    if isinstance(greeks_raw, dict):
        greeks = {
            "delta": num(greeks_raw.get("delta")),
            "gamma": num(greeks_raw.get("gamma")),
            "theta": num(greeks_raw.get("theta")),
            "vega": num(greeks_raw.get("vega")),
            "bid_iv": num(greeks_raw.get("bid_iv")),
            "mid_iv": num(greeks_raw.get("mid_iv")),
            "ask_iv": num(greeks_raw.get("ask_iv")),
            "smv_vol": num(greeks_raw.get("smv_vol")),
            "updated_at": greeks_raw.get("updated_at"),
        }
    return {
        "symbol": str(raw.get("symbol", "")),
        "underlying": display_symbol(str(raw.get("underlying") or raw.get("root_symbol") or "")),
        "description": raw.get("description") or "",
        "option_type": str(raw.get("option_type") or "").lower(),
        "strike": num(raw.get("strike")),
        "expiration": raw.get("expiration_date") or "",
        "contract_size": int(num(raw.get("contract_size")) or 100),
        "bid": num(raw.get("bid")),
        "ask": num(raw.get("ask")),
        "bidsize": num(raw.get("bidsize")),
        "asksize": num(raw.get("asksize")),
        "last": num(raw.get("last")),
        "prevclose": num(raw.get("prevclose")),
        "volume": num(raw.get("volume")) or 0.0,
        "open_interest": num(raw.get("open_interest")) or 0.0,
        "bid_time": epoch_ms_to_iso(raw.get("bid_date")),
        "ask_time": epoch_ms_to_iso(raw.get("ask_date")),
        "trade_time": epoch_ms_to_iso(raw.get("trade_date")),
        "greeks": greeks,
    }


def bars_frame(rows: list[dict[str, Any]]) -> pd.DataFrame:
    """Daily bars as a clean frame: typed, sorted, de-duplicated, with bad rows dropped."""
    if not rows:
        return pd.DataFrame(columns=BAR_COLUMNS)
    df = pd.DataFrame(rows)
    for col in BAR_COLUMNS:
        if col not in df.columns:
            df[col] = None
    df = df[BAR_COLUMNS].copy()
    df["date"] = pd.to_datetime(df["date"], errors="coerce")
    for col in BAR_COLUMNS[1:]:
        df[col] = pd.to_numeric(df[col], errors="coerce")
    df = df.dropna(subset=["date", "open", "high", "low", "close"])
    df = df[(df["close"] > 0) & (df["open"] > 0) & (df["high"] >= df["low"])]
    df["volume"] = df["volume"].fillna(0.0)
    return df.sort_values("date").drop_duplicates("date", keep="last").reset_index(drop=True)
