"""V2 analysis of one stock: trend state and evidence, relative strength and data checks.

This sits beside the classic service (service.py) and uses its market data, database and
pattern-lab verdicts. Later parts of V2 (market regime, live signals, evaluators) build on
the readings made here. Results are cached briefly, keyed by when the bars were fetched, so a
screen that asks twice in a row does not do the work twice.
"""

from __future__ import annotations

import logging
import time
from typing import Any

import numpy as np
import pandas as pd

from . import trend as trend_engine
from .dataquality import assess
from .market import complete_only
from .providers.base import DataError
from .sectors import SECTOR_FUNDS, sector_fund
from .versions import TREND_ENGINE_VERSION

log = logging.getLogger(__name__)

CACHE_SECONDS = 60
BENCH_YEARS = 3.0


class Analyst:
    def __init__(self, svc: Any):
        self.svc = svc
        self.market = svc.market
        self._cache: dict[tuple, tuple[float, Any]] = {}
        self._sectors: dict[str, str | None] = {}

    # --- small helpers ----------------------------------------------------------------
    def _cached(self, key: tuple, build):
        hit = self._cache.get(key)
        if hit is not None and time.time() - hit[0] < CACHE_SECONDS:
            return hit[1]
        value = build()
        self._cache[key] = (time.time(), value)
        if len(self._cache) > 256:
            for k in sorted(self._cache, key=lambda k: self._cache[k][0])[:64]:
                self._cache.pop(k, None)
        return value

    def sector(self, symbol: str) -> str | None:
        """The sector fund to compare `symbol` with: the fixed list, else its SEC industry code."""
        if symbol in self._sectors:
            return self._sectors[symbol]
        fund = sector_fund(symbol)
        if fund is None and symbol not in SECTOR_FUNDS and symbol not in ("SPY", "QQQ", "DIA", "IWM", "VIX"):
            sec = getattr(self.svc, "sec", None)
            if sec is not None and sec.enabled:
                try:
                    cik, _ = sec.cik_for(symbol)
                    fund = sector_fund(symbol, (sec.submissions(cik) or {}).get("sic"))
                except Exception as exc:          # SEC lookups are a convenience; never fatal here
                    log.info("No SEC industry for %s: %s", symbol, exc)
        self._sectors[symbol] = fund
        return fund

    def benchmarks(self, symbol: str, sector: str | None) -> tuple[dict[str, pd.DataFrame | None], list[str]]:
        out: dict[str, pd.DataFrame | None] = {}
        notes: list[str] = []
        for etf in dict.fromkeys(["SPY", "QQQ"] + ([sector] if sector else [])):
            if etf == symbol:
                continue
            try:
                out[etf] = self.market.bars(etf, BENCH_YEARS)
            except DataError as exc:
                out[etf] = None
                notes.append(f"{etf} history unavailable, so relative strength against it is left out: {exc}")
        return out, notes

    def validated(self) -> dict[str, list[dict[str, Any]]]:
        try:
            return self.svc._validated_patterns()
        except Exception as exc:                  # a damaged lab record must not stop the analysis
            log.warning("Pattern lab verdicts unavailable: %s", exc)
            return {}

    # --- trend ---------------------------------------------------------------------------
    def trend(self, symbol: str) -> dict[str, Any]:
        symbol = self.svc._symbol(symbol)
        enriched = self.market.enriched(symbol, 3)
        info = self.market.bar_info(symbol)
        validated = self.validated()
        key = ("trend", symbol, info.get("fetched_at"), len(enriched), tuple(sorted(validated)))
        return self._cached(key, lambda: self._trend(symbol, enriched, validated))

    def _trend(self, symbol: str, enriched: pd.DataFrame, validated: dict[str, Any]) -> dict[str, Any]:
        sector = self.sector(symbol)
        bench, notes = self.benchmarks(symbol, sector)
        out = trend_engine.analyse(enriched, bench, sector, validated)
        out["symbol"] = symbol
        out["relative_strength"] = relative_strength(enriched, bench, sector)
        out["notes"] = notes + ([] if validated else ["The pattern lab has not been run on this data, so validated "
                                                       "patterns are left out of the evidence."])
        if sector is None and symbol not in SECTOR_FUNDS:
            out["notes"].append("This stock's sector is not known, so it is compared with SPY only.")
        if self.market.info.mode == "demo":
            out["notes"].insert(0, "Demo prices are synthetic: this reading says nothing about the real stock.")
        out["data_source"] = self.market.info.mode
        return out

    def snapshot_extra(self, symbol: str, values: dict[str, Any]) -> dict[str, Any]:
        """Trend readings for a position snapshot (Desk.snapshot_extras)."""
        t = self.trend(symbol)
        cur = t["provisional"] or t["confirmed"]
        rs = t["relative_strength"]
        return {"trend_state": cur["state"], "bullish_score": cur["bullish_score"], "bearish_score": cur["bearish_score"],
                "relative_strength_spy": rs.get("spy_3m"), "relative_strength_sector": rs.get("sector_3m"),
                "_raw": {"trend": {"version": TREND_ENGINE_VERSION, "state": cur["state"], "status": cur["status"],
                                   "confirmed_state": (t["confirmed"] or {}).get("state"), "net": cur["net_score"],
                                   "confidence": cur["confidence"]["level"]}}}

    # --- everything about one stock --------------------------------------------------------------
    def analysis(self, symbol: str) -> dict[str, Any]:
        symbol = self.svc._symbol(symbol)
        out: dict[str, Any] = {"symbol": symbol, "data_source": self.market.info.mode}
        bars = self.market.bars(symbol, 3)
        out["data_quality"] = assess(symbol, bars, self.market.bar_info(symbol))
        out["trend"] = self.trend(symbol)
        try:
            q = self.market.quote(symbol)
            out["quote"] = {"last": q.get("last"), "bid": q.get("bid"), "ask": q.get("ask"),
                            "prevclose": q.get("prevclose"), "description": q.get("description")}
        except DataError as exc:
            out["quote"] = {"error": str(exc)}
        for extra in getattr(self, "extras", []):
            try:
                out.update(extra(symbol, out))
            except (DataError, ValueError, KeyError) as exc:
                out.setdefault("errors", []).append(f"{getattr(extra, '__name__', 'extra')}: {exc}")
        return out


def relative_strength(enriched: pd.DataFrame, bench: dict[str, pd.DataFrame | None], sector: str | None,
                      complete: bool = True) -> dict[str, Any]:
    """The stock's return less each benchmark's, over one and three months, in percentage points,
    on the last finished bar."""
    df = complete_only(enriched) if complete else enriched
    out: dict[str, Any] = {"sector_fund": sector, "as_of": df["date"].iloc[-1].strftime("%Y-%m-%d") if len(df) else None}
    for name, frame in bench.items():
        if frame is None or frame.empty:
            continue
        b = frame.set_index(pd.to_datetime(frame["date"]))["close"].astype(float).sort_index()
        b = b[~b.index.duplicated(keep="last")].reindex(pd.to_datetime(df["date"]), method="ffill").to_numpy()
        c = df["close"].to_numpy(dtype=float)
        tag = "sector" if name == sector else name.lower()
        for days, label in ((trend_engine.RS_SHORT, "1m"), (trend_engine.RS_LONG, "3m")):
            if len(c) > days and np.isfinite(b[-1]) and np.isfinite(b[-1 - days]) and b[-1 - days] > 0:
                out[f"{tag}_{label}"] = round(100 * ((c[-1] / c[-1 - days]) - (b[-1] / b[-1 - days])), 2)
    return out
