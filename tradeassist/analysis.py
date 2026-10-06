"""V2 analysis of one stock: trend state and evidence, relative strength and data checks.

This sits beside the classic service (service.py) and uses its market data, database and
pattern-lab verdicts. Later parts of V2 (market regime, live signals, evaluators) build on
the readings made here. Results are cached briefly, keyed by when the bars were fetched, so a
screen that asks twice in a row does not do the work twice.
"""

from __future__ import annotations

import logging
import math
import time
from datetime import datetime, timezone
from typing import Any

import numpy as np
import pandas as pd

from . import regime as regime_engine
from . import signalbook
from . import trend as trend_engine
from .dataquality import assess
from .market import complete_only
from .patterns import PATTERNS, detect_all, recent_events
from .providers.base import DataError
from .sectors import SECTOR_FUNDS, sector_fund
from .versions import SIGNAL_ENGINE_VERSION, TREND_ENGINE_VERSION

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
        try:
            regime = self.regime()["regime"]
        except (DataError, ValueError):
            regime = None
        return {"trend_state": cur["state"], "bullish_score": cur["bullish_score"], "bearish_score": cur["bearish_score"],
                "market_regime": regime,
                "relative_strength_spy": rs.get("spy_3m"), "relative_strength_sector": rs.get("sector_3m"),
                "_raw": {"trend": {"version": TREND_ENGINE_VERSION, "state": cur["state"], "status": cur["status"],
                                   "confirmed_state": (t["confirmed"] or {}).get("state"), "net": cur["net_score"],
                                   "confidence": cur["confidence"]["level"]}}}

    # --- market regime -------------------------------------------------------------------------
    def regime(self) -> dict[str, Any]:
        spy = self.market.enriched("SPY", 3)
        info = self.market.bar_info("SPY")
        return self._cached(("regime", info.get("fetched_at"), len(spy)), lambda: self._regime(spy))

    def _regime(self, spy: pd.DataFrame) -> dict[str, Any]:
        notes = []
        try:
            qqq = self.market.enriched("QQQ", 3)
        except DataError as exc:
            qqq = None
            notes.append(f"QQQ history unavailable, so SPY is not cross-checked: {exc}")
        vix = None
        if self.market.info.mode != "demo":       # the demo source has no VIX
            try:
                vix = self.market.bars("VIX", 3)
            except DataError:
                vix = None
        out = regime_engine.analyse(spy, qqq, vix)
        out["notes"] = notes + out["notes"]
        if self.market.info.mode == "demo":
            out["notes"].insert(0, "Demo prices are synthetic, and the demo QQQ moves largely on its own, so the "
                                   "demo market is often 'in transition'.")
        return out

    def interpretation(self, symbol: str, trend: dict[str, Any], regime: dict[str, Any]) -> dict[str, Any]:
        enriched = self.market.enriched(symbol, 3)
        day = trend["provisional"] or trend["confirmed"]
        row = enriched.iloc[-1] if trend["provisional"] else complete_only(enriched).iloc[-1]
        readings = regime_engine.interpret(row, day["state"], regime["regime"], day["volatility"].get("band_width_rank"))
        return {"date": day["date"], "status": day["status"], "stock_state": day["state"], "regime": regime["regime"],
                "readings": readings,
                "note": "How these readings are usually read in this trend and this market. They explain; they do not "
                        "add to the trend scores."}

    # --- signal history and calibration -----------------------------------------------------------
    def _regime_by_date(self, years: float) -> pd.Series | None:
        try:
            spy = self.market.enriched("SPY", years, complete=True)
        except DataError:
            return None
        try:
            qqq = self.market.enriched("QQQ", years, complete=True)
        except DataError:
            qqq = None
        vix = None
        if self.market.info.mode != "demo":
            try:
                vix = self.market.bars("VIX", years, complete=True)
            except DataError:
                vix = None
        series = regime_engine.regime_series(spy, qqq, vix)
        return pd.Series(series["regime"].to_numpy(), index=pd.to_datetime(series["date"]))

    def build_history(self, symbols: list[str] | None = None, years: float = 5.0) -> dict[str, Any]:
        """Record every past signal on `symbols` (default: the watchlist) with what followed, and
        check the evidence score against outcomes. Finished bars only."""
        source = self.market.info.mode
        names = [self.svc._symbol(s) for s in (symbols or self.svc.settings.watchlist) if s.strip()]
        if not names:
            raise ValueError("Give at least one symbol")
        if len(names) > 60:
            raise ValueError("At most 60 symbols per run")
        years = max(1.5, min(float(years), 10.0))
        regimes = self._regime_by_date(years)
        try:
            spy = self.market.bars("SPY", years, complete=True)
        except DataError:
            spy = None
        built, warnings, days, total = [], [], [], 0
        for sym in dict.fromkeys(names):
            try:
                df = self.market.enriched(sym, years, complete=True)
                sector = self.sector(sym)
                bench = {"SPY": spy}
                if sector:
                    try:
                        bench[sector] = self.market.bars(sector, years, complete=True)
                    except DataError:
                        pass
                # Without the validated-pattern points: the lab found those on this same history.
                states = trend_engine.state_series(df, bench, sector, None)
                rows = signalbook.collect(sym, df, spy, regimes, states)
                signalbook.store(self.svc.db, source, sym, rows)
                days.append(signalbook.score_days(sym, df, states, spy, regimes))
                total += len(rows)
                built.append(sym)
            except (DataError, ValueError) as exc:
                warnings.append(f"{sym}: {exc}")
        if not built:
            raise ValueError("No price history could be loaded for those symbols")
        all_days = pd.concat(days, ignore_index=True)
        calibration = {str(h): signalbook.calibrate(all_days, h) for h in (10, 20)}
        meta = {"run_at": datetime.now(timezone.utc).isoformat(timespec="seconds"), "data_source": source,
                "symbols": built, "years": years, "occurrences": total, "warnings": warnings,
                "signal_engine_version": SIGNAL_ENGINE_VERSION, "trend_engine_version": TREND_ENGINE_VERSION,
                "calibration": calibration}
        self.svc.db.cache_put(f"signals:history:{source}", meta)
        return meta

    def history_meta(self) -> dict[str, Any] | None:
        return self.svc.db.cache_get(f"signals:history:{self.market.info.mode}")

    def calibration(self) -> dict[str, Any]:
        meta = self.history_meta()
        if not meta:
            return {"built": False, "note": "Build the signal history first (Pattern lab screen, or "
                                            "POST /v2/signals/history/build)."}
        return {"built": True, "run_at": meta["run_at"], "symbols": meta["symbols"], "data_source": meta["data_source"],
                "trend_engine_version": meta["trend_engine_version"], "calibration": meta["calibration"]}

    def signals(self, symbol: str) -> dict[str, Any]:
        """What has fired on `symbol` in the last ten bars, and how the same signals turned out before:
        across all stocks in the history, in the current market regime, and on this stock alone."""
        symbol = self.svc._symbol(symbol)
        source = self.market.info.mode
        meta = self.history_meta()
        enriched = self.market.enriched(symbol, 3)
        t = self.trend(symbol)
        try:
            regime_now = self.regime()["regime"]
        except (DataError, ValueError):
            regime_now = None
        validated = self.validated()
        live = []
        for e in recent_events(enriched, lookback=10):
            live.append({"key": f"pattern:{e['key']}", "label": e["label"], "direction": e["direction"],
                         "date": e["date"], "bars_ago": e["bars_ago"], "status": e["status"],
                         "lab": validated.get(e["key"], [])})
        last_date = enriched["date"].iloc[-1].strftime("%Y-%m-%d")
        recent_dates = {d.strftime("%Y-%m-%d") for d in enriched["date"].tail(10)}
        for tr in t["transitions"]:
            if tr["date"] in recent_dates:
                key = f"trend:{tr['to']}"
                live.append({"key": key, "label": signalbook.signal_label(key), "direction": signalbook.signal_direction(key),
                             "date": tr["date"], "status": "CONFIRMED", "lab": []})
        if t["changed_today"]:
            key = f"trend:{t['provisional']['state']}"
            live.append({"key": key, "label": signalbook.signal_label(key), "direction": signalbook.signal_direction(key),
                         "date": last_date, "status": "PROVISIONAL", "lab": []})
        for s in live:
            rows = signalbook.load(self.svc.db, source, s["key"]) if meta else []
            s["history"] = {
                "all": signalbook.summarize(rows, s["direction"]),
                "by_horizon": [signalbook.summarize(rows, s["direction"], h) for h in signalbook.HORIZONS],
                "same_regime": signalbook.summarize([r for r in rows if r["regime"] == regime_now], s["direction"])
                if regime_now else None,
                "this_stock": signalbook.summarize([r for r in rows if r["symbol"] == symbol], s["direction"]),
            }
        out = {"symbol": symbol, "as_of": last_date, "regime": regime_now, "signals": live,
               "history_built": bool(meta), "history_run_at": meta["run_at"] if meta else None,
               "history_symbols": len(meta["symbols"]) if meta else 0, "notes": []}
        out["score"] = self._score_calibration(symbol, enriched, meta, regime_now)
        if not meta:
            out["notes"].append("The signal history has not been built, so there are no past cases to compare with. "
                                "Build it from the Pattern lab screen.")
        elif symbol not in meta["symbols"]:
            out["notes"].append(f"{symbol} is not in the signal history, so its own past cases are not counted; the "
                                "figures across all stocks still apply.")
        if source == "demo":
            out["notes"].insert(0, "Demo prices are random: these frequencies say nothing about real markets.")
        return out

    def _score_calibration(self, symbol: str, enriched: pd.DataFrame, meta: dict[str, Any] | None,
                           regime_now: str | None) -> dict[str, Any]:
        sector = self.sector(symbol)
        bench, _ = self.benchmarks(symbol, sector)
        states = trend_engine.state_series(enriched, bench, sector, None)
        complete = enriched["bar_complete"].to_numpy(dtype=bool) if "bar_complete" in enriched.columns else None
        idx = len(enriched) - 1
        if complete is not None and not complete[-1]:
            idx = int(np.flatnonzero(complete)[-1]) if complete.any() else idx
        net = float(states["net_score"].iloc[idx])
        out: dict[str, Any] = {"net_score_for_calibration": round(net, 1) if math.isfinite(net) else None,
                               "date": enriched["date"].iloc[idx].strftime("%Y-%m-%d"),
                               "note": "The net score without validated-pattern points, on the last finished bar, so it "
                                       "matches how the history was scored."}
        if not meta:
            return {**out, "calibrated": None}
        bucket = signalbook.bucket_for(net)
        for h, cal in meta["calibration"].items():
            row = next((b for b in cal["buckets"] if bucket and b["low"] == bucket[0]), None)
            out[f"h{h}"] = {"calibrated": cal["calibrated"], "verdict": cal["verdict"], "bucket": row,
                            "same_regime": (row or {}).get("by_regime", {}).get(regime_now) if regime_now else None}
        out["calibrated"] = meta["calibration"][str(signalbook.MAIN_HORIZON)]["calibrated"]
        return out

    def patterns(self, symbol: str) -> dict[str, Any]:
        """Each pattern on this stock: how often it fired, how it turned out here and across all stocks,
        and the pattern lab's verdict."""
        symbol = self.svc._symbol(symbol)
        source = self.market.info.mode
        meta = self.history_meta()
        enriched = self.market.enriched(symbol, 5, complete=True)
        hits = detect_all(enriched)
        validated = self.validated()
        mine = signalbook.grouped(signalbook.load(self.svc.db, source, symbol=symbol)) if meta else {}
        rows = []
        for key, pat in PATTERNS.items():
            fired = np.flatnonzero(hits[key].to_numpy(dtype=bool)) if key in hits.columns else []
            pooled = signalbook.load(self.svc.db, source, f"pattern:{key}") if meta else []
            rows.append({"key": key, "label": pat.label, "direction": pat.direction, "description": pat.description,
                         "events_here": int(len(fired)),
                         "last_date": enriched["date"].iloc[fired[-1]].strftime("%Y-%m-%d") if len(fired) else None,
                         "this_stock": signalbook.summarize(mine.get(f"pattern:{key}", []), pat.direction),
                         "all_stocks": signalbook.summarize(pooled, pat.direction), "lab": validated.get(key, [])})
        rows.sort(key=lambda r: -(r["events_here"]))
        return {"symbol": symbol, "years": 5, "patterns": rows, "history_built": bool(meta),
                "history_run_at": meta["run_at"] if meta else None}

    # --- everything about one stock --------------------------------------------------------------
    def analysis(self, symbol: str) -> dict[str, Any]:
        symbol = self.svc._symbol(symbol)
        out: dict[str, Any] = {"symbol": symbol, "data_source": self.market.info.mode}
        bars = self.market.bars(symbol, 3)
        out["data_quality"] = assess(symbol, bars, self.market.bar_info(symbol))
        out["trend"] = self.trend(symbol)
        try:
            out["regime"] = self.regime()
            out["interpretation"] = self.interpretation(symbol, out["trend"], out["regime"])
        except (DataError, ValueError) as exc:
            out["regime"] = None
            out.setdefault("errors", []).append(f"Market regime unavailable: {exc}")
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
