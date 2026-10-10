"""The market's make-up: the main indexes, volatility, sectors, breadth, the regime and the
options market's put/call balance, all worked out from the app's own price data.

- Indexes: SPY (S&P 500), QQQ (Nasdaq 100), IWM (Russell 2000) and DIA (Dow), through their
  funds: the day, five days, a month and three months, and where each sits against its 50- and
  200-day averages.
- Volatility: VIX where the data source has it (the demo source does not), otherwise SPY's own
  20-day realized volatility, as the regime engine reads it.
- Sectors: the eleven Select Sector SPDR funds over a day and a month, and against SPY.
- Breadth: of a list of large companies (or the watchlist), how many rose on the day, how many
  are above their 50- and 200-day averages, and how many made new 52-week highs or lows. Stocks
  whose history could not be read are counted as such, not as zero.
- The options market: SPY's and QQQ's put/call volume and open interest over the nearest
  expirations.
"""

from __future__ import annotations

import math
import time
from datetime import datetime
from typing import Any

import pandas as pd

from .optionmath import NY
from .providers.base import DataError
from .sectors import SECTOR_FUNDS

INDEXES = (("SPY", "S&P 500"), ("QQQ", "Nasdaq 100"), ("IWM", "Russell 2000"), ("DIA", "Dow Jones"))
CACHE_SECONDS = 600
MAX_BREADTH = 120


def _f(x: Any) -> float | None:
    try:
        v = float(x)
    except (TypeError, ValueError):
        return None
    return v if math.isfinite(v) else None


def changes(df: pd.DataFrame) -> dict[str, Any]:
    """Moves over a day, five days, a month (21 bars) and three months (63), as percentages."""
    c = df["close"].astype(float).to_numpy()
    last = c[-1]

    def back(n: int) -> float | None:
        return 100 * (last / c[-1 - n] - 1) if len(c) > n and c[-1 - n] > 0 else None

    row = df.iloc[-1]
    s50, s200 = _f(row.get("sma50")), _f(row.get("sma200"))
    return {"close": float(last), "date": pd.Timestamp(row["date"]).strftime("%Y-%m-%d"),
            "bar_finished": bool(row["bar_complete"]) if "bar_complete" in df.columns else True,
            "day_pct": back(1), "week_pct": back(5), "month_pct": back(21), "quarter_pct": back(63),
            "vs_50d_pct": 100 * (last / s50 - 1) if s50 else None, "vs_200d_pct": 100 * (last / s200 - 1) if s200 else None,
            "rsi": _f(row.get("rsi"))}


def breadth(frames: dict[str, pd.DataFrame]) -> dict[str, Any]:
    """Advancers, decliners, stocks above their averages and new 52-week highs and lows. A stock with
    fewer than two usable closes is counted as unreadable; a measure no stock could give is None, not 0."""
    n = adv = dec = above50 = above200 = known50 = known200 = highs = lows = known_range = unusable = 0
    for df in frames.values():
        c = df["close"].astype(float).to_numpy() if len(df) else []
        if len(c) < 2 or not (math.isfinite(c[-1]) and math.isfinite(c[-2])):
            unusable += 1
            continue
        n += 1
        adv += c[-1] > c[-2]
        dec += c[-1] < c[-2]
        row = df.iloc[-1]
        s50, s200 = _f(row.get("sma50")), _f(row.get("sma200"))
        if s50:
            known50 += 1
            above50 += c[-1] > s50
        if s200:
            known200 += 1
            above200 += c[-1] > s200
        if len(df) >= 253:
            prior_hi = df["high"].astype(float).iloc[-253:-1].max()
            prior_lo = df["low"].astype(float).iloc[-253:-1].min()
            if math.isfinite(prior_hi) and math.isfinite(prior_lo):
                known_range += 1
                highs += c[-1] > prior_hi
                lows += c[-1] < prior_lo

    def count(v: int, measured: int) -> int | None:
        return int(v) if measured else None

    pct = lambda a, b: 100 * a / b if b else None          # noqa: E731
    return {"stocks": n, "advancing": count(adv, n), "declining": count(dec, n), "unchanged": count(n - adv - dec, n),
            "above_50d": count(above50, known50), "measured_50d": known50, "above_50d_pct": pct(above50, known50),
            "above_200d": count(above200, known200), "measured_200d": known200, "above_200d_pct": pct(above200, known200),
            "new_highs": count(highs, known_range), "new_lows": count(lows, known_range), "measured_52w": known_range,
            "unusable": unusable}


class MarketMakeup:
    def __init__(self, svc: Any, analyst: Any | None = None):
        self.svc = svc
        self.analyst = analyst
        self._cache: dict[str, tuple[float, dict[str, Any]]] = {}

    def build(self, breadth_universe: str = "large_caps", refresh: bool = False) -> dict[str, Any]:
        from .scanner import LARGE_CAPS, flow_expirations, flow_summary
        key = breadth_universe
        hit = self._cache.get(key)
        if hit and not refresh and time.time() - hit[0] < CACHE_SECONDS:
            return {**hit[1], "cached": True}
        market = self.svc.market
        errors: list[dict[str, str]] = []

        def read(sym: str) -> pd.DataFrame | None:
            try:
                return market.enriched(sym, 2)
            except (DataError, ValueError) as exc:
                errors.append({"symbol": sym, "error": str(exc)})
                return None

        indexes = []
        for sym, name in INDEXES:
            df = read(sym)
            if df is not None and len(df) > 1:
                indexes.append({"symbol": sym, "name": name, **changes(df)})
        spy = next((i for i in indexes if i["symbol"] == "SPY"), None)
        sectors = []
        for sym, name in SECTOR_FUNDS.items():
            df = read(sym)
            if df is None or len(df) < 2:
                continue
            row = {"symbol": sym, "name": name, **changes(df)}
            row["month_vs_spy_pct"] = (row["month_pct"] - spy["month_pct"]
                                       if spy and row["month_pct"] is not None and spy["month_pct"] is not None else None)
            sectors.append(row)
        sectors.sort(key=lambda r: (r["month_pct"] is None, -(r["month_pct"] or 0)))
        if breadth_universe == "watchlist":
            names = list(dict.fromkeys(self.svc.settings.watchlist))[:MAX_BREADTH]
        elif breadth_universe == "large_caps":
            names = list(LARGE_CAPS)
        else:
            raise ValueError("breadth must be large_caps or watchlist")
        frames = {}
        for sym in names:
            df = read(sym)
            if df is not None:
                frames[sym] = df
        regime = None
        if self.analyst is not None:
            try:
                g = self.analyst.regime()
                now = g.get("provisional") or g.get("confirmed") or {}
                regime = {"regime": now.get("regime"), "text": now.get("text"), "status": now.get("status"),
                          "date": now.get("date"), "volatility": now.get("volatility")}
            except (DataError, ValueError, KeyError) as exc:
                errors.append({"symbol": "regime", "error": str(exc)})
        options = []
        today = datetime.now(NY).date()
        for sym in ("SPY", "QQQ"):
            try:
                picked = flow_expirations(market.expirations(sym), today, 45, 2)
                if not picked:
                    continue
                analyses = [self.svc._chain_for(sym, e) for e in picked]
                s = flow_summary(analyses)
                options.append({"symbol": sym, "expirations": picked,
                                **{k: s[k] for k in ("call_volume", "put_volume", "put_call_volume",
                                                     "put_call_open_interest", "call_premium", "put_premium", "lean")}})
            except (DataError, ValueError) as exc:
                errors.append({"symbol": f"{sym} options", "error": str(exc)})
        b = breadth(frames)
        b.update({"universe": breadth_universe, "asked": len(names), "unreadable": len(names) - len(frames) + b["unusable"]})
        notes = ["Worked out from daily prices: the day's move is the latest bar against the one before, and is "
                 "provisional until the close."]
        if breadth_universe == "large_caps":
            notes.append("Breadth is over a fixed list of 100 large US companies, close to the S&P 100; it is not the "
                         "whole market.")
        if market.info.mode == "demo":
            notes.insert(0, "Demo data: these prices are made up, and the demo source has no VIX.")
        out = {"checked_at": datetime.now(NY).isoformat(timespec="seconds"), "indexes": indexes, "sectors": sectors,
               "breadth": b, "regime": regime, "options": options, "errors": errors, "notes": notes,
               "data_source": market.info.mode, "cached": False}
        self._cache[key] = (time.time(), out)
        return out


__all__ = ["MarketMakeup", "changes", "breadth", "INDEXES"]
