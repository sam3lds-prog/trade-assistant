"""Read-only Tradier market-data client.

Only GET requests to /v1/markets/* are made. The production host gives real-time
quotes and Greeks to brokerage account holders; the sandbox host is delayed 15
minutes and returns no Greeks.
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Any

import pandas as pd
import requests

from .base import (DataError, ProviderInfo, api_symbol, as_list, bars_frame, dicts, display_symbol,
                   normalize_contract, normalize_quote)

log = logging.getLogger(__name__)

LIVE_URL = "https://api.tradier.com/v1"
SANDBOX_URL = "https://sandbox.tradier.com/v1"


class TradierData:
    def __init__(self, token: str, sandbox: bool, session: requests.Session | None = None,
                 timeout: float = 10.0, max_retries: int = 3):
        if not token:
            raise ValueError("A Tradier API token is required")
        self.base_url = SANDBOX_URL if sandbox else LIVE_URL
        self.timeout = timeout
        self.max_retries = max_retries
        self._session = session or requests.Session()
        self._session.headers.update({"Authorization": f"Bearer {token}", "Accept": "application/json"})
        self._lock = threading.Lock()
        self._rate_available: int | None = None
        self._rate_expiry: float = 0.0
        if sandbox:
            self.info = ProviderInfo(
                mode="sandbox", label="Tradier sandbox", realtime=False, has_greeks=False,
                note="Quotes are delayed 15 minutes. Do not set a live limit price from them.",
                poll_seconds=3.0)   # the sandbox allows 60 requests a minute, and its quotes are delayed anyway
        else:
            self.info = ProviderInfo(
                mode="live", label="Tradier live data", realtime=True, has_greeks=True,
                note="Real-time quotes. Greeks from Tradier refresh about once an hour.",
                poll_seconds=1.0)   # one request a second uses half of the 120-a-minute allowance

    # --- transport -------------------------------------------------------
    def _wait_for_rate_limit(self) -> None:
        with self._lock:
            if self._rate_available is not None and self._rate_available <= 1:
                delay = self._rate_expiry - time.time()
                if 0 < delay <= 65:
                    log.info("Tradier rate limit reached, waiting %.1fs", delay)
                    time.sleep(delay)
                self._rate_available = None

    def _record_rate_limit(self, headers: Any) -> None:
        try:
            available = headers.get("X-Ratelimit-Available")
            expiry = headers.get("X-Ratelimit-Expiry")
            if available is None or expiry is None:
                return
            expiry_f = float(expiry)
            if expiry_f > 1e11:  # milliseconds
                expiry_f /= 1000.0
            with self._lock:
                self._rate_available = int(available)
                self._rate_expiry = expiry_f
        except (TypeError, ValueError):
            pass

    def _get(self, path: str, params: dict[str, Any]) -> dict[str, Any]:
        url = f"{self.base_url}{path}"
        last_error = "unknown error"
        for attempt in range(self.max_retries):
            self._wait_for_rate_limit()
            try:
                resp = self._session.get(url, params=params, timeout=self.timeout)
            except requests.RequestException as exc:
                last_error = f"network error: {exc.__class__.__name__}"
                time.sleep(0.5 * (attempt + 1))
                continue
            self._record_rate_limit(resp.headers)
            if resp.status_code == 200:
                try:
                    body = resp.json()
                except ValueError as exc:
                    raise DataError(f"Tradier returned a non-JSON reply for {path}") from exc
                return body if isinstance(body, dict) else {}
            if resp.status_code in (401, 403):
                raise DataError("Tradier rejected the API token. Check the token in .env and that it "
                                "matches the environment (live or sandbox).")
            if resp.status_code == 429 or resp.status_code >= 500:
                last_error = f"HTTP {resp.status_code}"
                time.sleep(1.0 * (attempt + 1))
                continue
            raise DataError(f"Tradier request failed for {path}: HTTP {resp.status_code} {resp.text[:200]}")
        raise DataError(f"Tradier request failed for {path} after {self.max_retries} tries ({last_error})")

    # --- market data -----------------------------------------------------
    def quotes(self, symbols: list[str]) -> dict[str, dict[str, Any]]:
        if not symbols:
            return {}
        body = self._get("/markets/quotes", {"symbols": ",".join(api_symbol(s) for s in symbols),
                                             "greeks": "false"})
        quotes = body.get("quotes")
        out: dict[str, dict[str, Any]] = {}
        for raw in dicts(quotes.get("quote") if isinstance(quotes, dict) else None):
            q = normalize_quote(raw)
            key = str(raw.get("symbol", "")) if raw.get("type") == "option" else q["symbol"]
            q["symbol"] = key
            out[key] = q
        return out

    def history(self, symbol: str, start: str, end: str) -> pd.DataFrame:
        body = self._get("/markets/history", {"symbol": api_symbol(symbol), "interval": "daily",
                                              "start": start, "end": end})
        history = body.get("history") or {}
        return bars_frame(dicts(history.get("day")) if isinstance(history, dict) else [])

    def expirations(self, symbol: str) -> list[str]:
        body = self._get("/markets/options/expirations", {"symbol": api_symbol(symbol),
                                                          "includeAllRoots": "true"})
        expirations = body.get("expirations") or {}
        dates = as_list(expirations.get("date")) if isinstance(expirations, dict) else []
        return sorted(str(d) for d in dates if isinstance(d, str) and len(d) == 10)

    def chain(self, symbol: str, expiration: str) -> list[dict[str, Any]]:
        body = self._get("/markets/options/chains", {"symbol": api_symbol(symbol), "expiration": expiration,
                                                     "greeks": "true"})
        options = body.get("options") or {}
        raw_list = dicts(options.get("option")) if isinstance(options, dict) else []
        contracts = [normalize_contract(raw) for raw in raw_list]
        root = display_symbol(symbol)
        for c in contracts:
            c["underlying"] = c["underlying"] or root
        # Adjusted contracts (after splits or mergers) deliver something other than 100 shares
        # and cannot be priced as standard options, so they are left out.
        return [c for c in contracts if c["strike"] is not None and c["strike"] > 0
                and c["option_type"] in ("call", "put") and c["contract_size"] == 100]

    def clock(self) -> dict[str, Any]:
        body = self._get("/markets/clock", {})
        clock = body.get("clock")
        if not isinstance(clock, dict):
            clock = {}
        return {"state": clock.get("state", "unknown"), "description": clock.get("description", ""),
                "date": clock.get("date", ""), "next_change": clock.get("next_change", ""),
                "next_state": clock.get("next_state", "")}
