"""Synthetic market data for trying the app without a Tradier token.

Everything here is made up: prices follow a seeded random walk and option quotes are
generated from a model. It is deterministic for a given symbol and day, so the app
and its tests behave the same on every run. Nothing here says anything about real
markets, and study results computed on it are meaningless as trading evidence.
"""

from __future__ import annotations

import hashlib
import math
import re
import time
from datetime import date, datetime, timedelta, timezone
from typing import Any

import numpy as np
import pandas as pd

from ..optionmath import NY, black76_greeks, black76_price, years_to_expiry
from .base import BAR_COLUMNS, DataError, ProviderInfo, display_symbol

HISTORY_DAYS = 1600
RATE = 0.04
NON_PENNY = {"COST"}                 # quoted in $0.05 / $0.10 steps
PENNY_EVERYWHERE = {"SPY", "QQQ", "IWM"}
STOCK_SYMBOL = re.compile(r"^[A-Z]{1,5}(\.[A-Z])?$")


def _seed(*parts: str) -> int:
    digest = hashlib.sha256("|".join(parts).encode()).hexdigest()
    return int(digest[:12], 16)


def _occ(symbol: str, expiration: str, option_type: str, strike: float) -> str:
    d = date.fromisoformat(expiration)
    root = symbol.replace(".", "").replace("/", "")
    return f"{root}{d:%y%m%d}{'C' if option_type == 'call' else 'P'}{int(round(strike * 1000)):08d}"


def _third_friday(year: int, month: int) -> date:
    first = date(year, month, 1)
    offset = (4 - first.weekday()) % 7
    return first + timedelta(days=offset + 14)


class DemoData:
    def __init__(self, today: date | None = None, with_greeks: bool = True, live: bool = False):
        """`live` makes prices wobble every couple of seconds so that live updating has
        something to show. Off by default, which keeps the data fixed for a given day."""
        self._today = today
        self.with_greeks = with_greeks
        self.live = live
        self._occ_index: dict[str, tuple[str, str]] = {}   # option symbol -> (underlying, expiration)
        self.info = ProviderInfo(
            mode="demo", label="Demo data (synthetic)", realtime=False, has_greeks=with_greeks,
            note="Synthetic prices for trying the app. Add a Tradier token to .env for real data.",
            history_adjustment="none")
        self._history_cache: dict[str, pd.DataFrame] = {}
        self._market_cache: dict[str, np.ndarray] = {}

    # --- helpers ---------------------------------------------------------
    @property
    def today(self) -> date:
        return self._today or datetime.now(NY).date()

    def _profile(self, symbol: str) -> tuple[float, float, float]:
        """(starting price, annual volatility, market beta) for a symbol."""
        rng = np.random.default_rng(_seed("profile", symbol))
        if symbol == "SPY":
            return 380.0, 0.0, 1.0
        return float(rng.uniform(40, 420)), float(rng.uniform(0.14, 0.38)), float(rng.uniform(0.7, 1.3))

    def _market_factor(self, dates: pd.DatetimeIndex) -> np.ndarray:
        key = f"{dates[0]:%Y-%m-%d}:{dates[-1]:%Y-%m-%d}"
        if key not in self._market_cache:
            self._market_cache = {key: np.array([
                np.random.default_rng(_seed("mkt", d.strftime("%Y-%m-%d"))).standard_normal() for d in dates])}
        return self._market_cache[key]

    def _full_history(self, symbol: str) -> pd.DataFrame:
        symbol = display_symbol(symbol)
        key = f"{symbol}:{self.today.isoformat()}"
        if key in self._history_cache:
            return self._history_cache[key]
        last_day = self.today
        while last_day.weekday() >= 5:
            last_day -= timedelta(days=1)
        dates = pd.bdate_range(end=pd.Timestamp(last_day), periods=HISTORY_DAYS)
        # Random numbers are keyed to the calendar date so yesterday's bars do not change today.
        market = self._market_factor(dates)
        own = np.array([np.random.default_rng(_seed("own", symbol, d.strftime("%Y-%m-%d"))).standard_normal(4)
                        for d in dates])
        start, idio_vol, beta = self._profile(symbol)
        market_vol = 0.15
        daily = beta * market_vol / math.sqrt(252) * market + idio_vol / math.sqrt(252) * own[:, 0]
        daily += 0.07 / 252  # modest upward drift
        close = start * np.exp(np.cumsum(daily))
        prev_close = np.concatenate([[start], close[:-1]])
        day_vol = math.sqrt((beta * market_vol) ** 2 + idio_vol ** 2) / math.sqrt(252)
        open_ = prev_close * np.exp(0.35 * day_vol * own[:, 1])
        spread_hi = np.abs(own[:, 2]) * day_vol * 0.8
        spread_lo = np.abs(own[:, 3]) * day_vol * 0.8
        high = np.maximum(open_, close) * (1 + spread_hi)
        low = np.minimum(open_, close) * (1 - spread_lo)
        base_volume = 2e6 + (_seed("vol", symbol) % 30) * 1e6
        volume = base_volume * np.exp(0.35 * own[:, 1] + 6.0 * np.abs(daily))
        df = pd.DataFrame({"date": dates, "open": np.round(open_, 2), "high": np.round(high, 2),
                           "low": np.round(low, 2), "close": np.round(close, 2),
                           "volume": np.round(volume)})
        df["high"] = df[["open", "high", "close"]].max(axis=1)
        df["low"] = df[["open", "low", "close"]].min(axis=1)
        self._history_cache[key] = df
        return df

    def _spot(self, symbol: str) -> float:
        last = float(self._full_history(symbol)["close"].iloc[-1])
        if not self.live:
            return last
        bucket = str(int(time.time() // 2))
        wobble = np.random.default_rng(_seed("tick", symbol, bucket)).standard_normal()
        return round(last * (1.0 + 0.0004 * float(wobble)), 2)

    def _ticks(self, symbol: str) -> tuple[float, float]:
        if symbol in PENNY_EVERYWHERE:
            return 0.01, 0.01
        if symbol in NON_PENNY:
            return 0.05, 0.10
        return 0.01, 0.05

    def _atm_vol(self, symbol: str) -> float:
        _, idio, beta = self._profile(symbol)
        realized = math.sqrt((beta * 0.15) ** 2 + idio ** 2)
        return realized * 1.12  # implied volatility usually sits a little above realized

    # --- provider interface ---------------------------------------------
    def quotes(self, symbols: list[str]) -> dict[str, dict[str, Any]]:
        out: dict[str, dict[str, Any]] = {}
        now = datetime.now(timezone.utc).isoformat()
        for raw in symbols:
            if raw in self._occ_index:               # an option from a chain served earlier
                underlying, expiration = self._occ_index[raw]
                contract = next((c for c in self.chain(underlying, expiration) if c["symbol"] == raw), None)
                if contract:
                    out[raw] = {**{k: contract[k] for k in ("symbol", "description", "bid", "ask", "bidsize",
                                                             "asksize", "last", "volume", "bid_time", "ask_time",
                                                             "trade_time")}, "type": "option"}
                continue
            symbol = display_symbol(raw)
            if symbol == "VIX" or not STOCK_SYMBOL.match(symbol):
                continue
            bars = self._full_history(symbol)
            close = float(bars["close"].iloc[-1])
            last = self._spot(symbol)
            tail = bars.tail(252)
            out[symbol] = {
                "symbol": symbol, "description": f"{symbol} (demo)", "type": "stock",
                "last": last, "bid": round(last - 0.01, 2), "ask": round(last + 0.01, 2),
                "bidsize": 300.0, "asksize": 300.0,
                "prevclose": float(bars["close"].iloc[-2]), "open": float(bars["open"].iloc[-1]),
                "high": float(bars["high"].iloc[-1]), "low": float(bars["low"].iloc[-1]),
                "close": None if self.live else close, "volume": float(bars["volume"].iloc[-1]),
                "average_volume": float(bars["volume"].tail(20).mean()),
                "week_52_high": float(tail["high"].max()), "week_52_low": float(tail["low"].min()),
                "bid_time": now, "ask_time": now, "trade_time": now,
            }
        return out

    def history(self, symbol: str, start: str, end: str) -> pd.DataFrame:
        bars = self._full_history(symbol)
        mask = (bars["date"] >= pd.Timestamp(start)) & (bars["date"] <= pd.Timestamp(end))
        return bars.loc[mask, BAR_COLUMNS].reset_index(drop=True)

    def expirations(self, symbol: str) -> list[str]:
        today = self.today
        out: set[date] = set()
        friday = today + timedelta(days=(4 - today.weekday()) % 7)
        if friday == today:
            friday += timedelta(days=7)
        for i in range(6):
            out.add(friday + timedelta(days=7 * i))
        year, month = today.year, today.month
        for _ in range(8):
            tf = _third_friday(year, month)
            if tf > today:
                out.add(tf)
            month += 1
            if month > 12:
                year, month = year + 1, 1
        return sorted(d.isoformat() for d in out)

    def chain(self, symbol: str, expiration: str) -> list[dict[str, Any]]:
        symbol = display_symbol(symbol)
        if expiration not in self.expirations(symbol):
            raise DataError(f"No {symbol} options expire on {expiration}")
        spot = self._spot(symbol)
        t = years_to_expiry(expiration)
        forward = spot * math.exp((RATE - 0.012) * t)   # includes a made-up 1.2% dividend yield
        atm = self._atm_vol(symbol)
        step = 1.0 if spot < 50 else 2.5 if spot < 150 else 5.0 if spot < 400 else 10.0
        lo = math.floor(spot * 0.75 / step) * step
        hi = math.ceil(spot * 1.25 / step) * step
        strikes = np.arange(lo, hi + step / 2, step)
        low_tick, high_tick = self._ticks(symbol)
        liquidity = 0.5 + (_seed("liq", symbol) % 100) / 60.0
        now = datetime.now(timezone.utc).isoformat()
        contracts: list[dict[str, Any]] = []
        for strike in strikes:
            k = math.log(strike / forward)
            smile_vol = atm * (1.0 - 0.45 * k + 1.8 * k * k) * (1.0 + 0.08 * math.exp(-t * 12))
            for option_type in ("call", "put"):
                occ = _occ(symbol, expiration, option_type, float(strike))
                noise = np.random.default_rng(_seed("quote", occ, self.today.isoformat()))
                quote_vol = smile_vol * (1.0 + 0.002 * noise.standard_normal())
                fair = black76_price(forward, float(strike), t, RATE, quote_vol, option_type)
                tick = low_tick if fair < 3 else high_tick
                otm = abs(k)
                half = max(tick / 2, fair * (0.008 + 0.10 * otm) / liquidity + 0.004)
                bid = math.floor((fair - half) / tick + 1e-9) * tick
                ask = math.ceil((fair + half) / tick - 1e-9) * tick
                stale = _seed("stale", occ) % 9 == 0
                if stale:  # one side has not been refreshed: the offer sits several ticks too high
                    ask += tick * (3 + _seed("stale2", occ) % 4)
                bid = max(round(bid, 2), 0.0)
                ask = round(max(ask, bid + tick), 2)
                weight = math.exp(-((k / 0.09) ** 2))
                open_interest = float(int(6000 * weight * liquidity * (0.4 + noise.random())))
                volume = float(int(open_interest * 0.15 * noise.random()))
                if weight > 0.05 and _seed("flow", occ, self.today.isoformat()) % 45 == 0:
                    # Now and then a contract trades more than its open interest, as new positions open.
                    volume = float(int(max(open_interest, 200) * (1.2 + (_seed("flow2", occ) % 30) / 10)))
                greeks = None
                if self.with_greeks:
                    g = black76_greeks(spot, forward, float(strike), t, RATE, quote_vol, option_type)
                    greeks = {"delta": g.delta, "gamma": g.gamma, "theta": g.theta, "vega": g.vega,
                              "bid_iv": None, "mid_iv": quote_vol, "ask_iv": None,
                              "smv_vol": smile_vol, "updated_at": now}
                self._occ_index[occ] = (symbol, expiration)
                contracts.append({
                    "symbol": occ, "underlying": symbol,
                    "description": f"{symbol} {expiration} {strike:g} {option_type}",
                    "option_type": option_type, "strike": float(strike), "expiration": expiration,
                    "contract_size": 100, "bid": bid, "ask": ask,
                    "bidsize": float(5 + _seed("bs", occ) % 60), "asksize": float(5 + _seed("as", occ) % 60),
                    "last": round(fair, 2), "volume": volume, "open_interest": open_interest,
                    "bid_time": now, "ask_time": now, "trade_time": now, "greeks": greeks,
                })
        return contracts

    def clock(self) -> dict[str, Any]:
        now = datetime.now(NY)
        minutes = now.hour * 60 + now.minute
        is_open = self.live or (now.weekday() < 5 and 570 <= minutes < 960)
        return {"state": "open" if is_open else "closed",
                "description": "Demo market, always open" if self.live else
                               "Demo clock (regular hours only, holidays ignored)",
                "date": now.date().isoformat(), "next_change": "", "next_state": ""}
