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
from .optionmath import NY
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

    def snapshot_extra(self, symbol: str, values: dict[str, Any], as_of: Any = None) -> dict[str, Any]:
        """Trend readings for a position snapshot (Desk.snapshot_extras). With `as_of`, a past date,
        they are worked out from the bars up to that day, as the engines would have seen them then."""
        if as_of is not None:
            return self._snapshot_extra_on(symbol, as_of)
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

    def _snapshot_extra_on(self, symbol: str, as_of: Any) -> dict[str, Any]:
        cut = pd.Timestamp(as_of)
        years = min(10.0, max(3.0, (datetime.now(NY).date() - cut.date()).days / 365 + 2.2))
        df = complete_only(self.market.enriched(symbol, years))
        df = df[df["date"] <= cut].reset_index(drop=True)
        if len(df) < 30:
            raise ValueError(f"not enough history before {cut.date()} for a trend reading")
        sector = self.sector(symbol)
        bench, _ = self.benchmarks(symbol, sector)
        states = trend_engine.state_series(df, bench, sector, None)
        last = states.iloc[-1]
        rs = relative_strength(df, bench, sector)
        regime = None
        try:
            spy = complete_only(self.market.enriched("SPY", years))
            spy = spy[spy["date"] <= cut].reset_index(drop=True)
            qqq = complete_only(self.market.enriched("QQQ", years))
            qqq = qqq[qqq["date"] <= cut].reset_index(drop=True)
            if len(spy) >= 60:
                regime = regime_engine.regime_series(spy, qqq if len(qqq) >= 30 else None)["regime"].iloc[-1]
        except DataError:
            regime = None
        net = float(last["net_score"])
        return {"trend_state": last["trend_state"],
                "bullish_score": round(float(last["bullish_score"]), 1) if math.isfinite(float(last["bullish_score"])) else None,
                "bearish_score": round(float(last["bearish_score"]), 1) if math.isfinite(float(last["bearish_score"])) else None,
                "market_regime": regime, "relative_strength_spy": rs.get("spy_3m"),
                "relative_strength_sector": rs.get("sector_3m"),
                "_raw": {"trend": {"version": TREND_ENGINE_VERSION, "state": last["trend_state"], "status": "CONFIRMED",
                                   "as_of": df["date"].iloc[-1].strftime("%Y-%m-%d"),
                                   "net": round(net, 1) if math.isfinite(net) else None,
                                   "note": "Worked out from the bars up to the fill date, without validated-pattern points."}}}

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

    # --- position evaluator ------------------------------------------------------------------------
    def evaluate_position(self, req: dict[str, Any]) -> dict[str, Any]:
        """Grade a stock trade idea, or an existing stock position (`position_id`) with its own plan."""
        from .evaluator import evaluate
        from .positions import read
        req = dict(req)
        if req.get("position_id"):
            with self.svc.db.tx() as cur:
                pos = read(cur, int(req["position_id"]))
            if pos["instrument_type"] != "STOCK":
                raise ValueError("This evaluates stock positions; use the options evaluator for calls and puts")
            req.setdefault("symbol", pos["symbol"])
            req["symbol"] = pos["symbol"]
            req["direction"] = pos["direction"]
            for k in ("invalidation_price", "target_1", "target_2"):
                if req.get(k) is None and pos.get(k) is not None:
                    req[k] = pos[k]
            if req.get("entry_price") is None and pos["ledger"]["cost_basis"]:
                req["entry_price"] = pos["ledger"]["cost_basis"]
            if req.get("horizon_days") is None and pos.get("intended_horizon"):
                req["horizon_days"] = pos["intended_horizon"]
        symbol = self.svc._symbol(req["symbol"])
        direction = 1 if str(req.get("direction", "LONG")).upper() == "LONG" else -1
        entry = req.get("entry_price")
        if entry is None:
            entry = self.market.spot(symbol)
        enriched = self.market.enriched(symbol, 3)
        trend = self.trend(symbol)
        try:
            regime = self.regime()
        except (DataError, ValueError):
            regime = None
        dq = assess(symbol, self.market.bars(symbol, 3), self.market.bar_info(symbol))
        earnings = self.svc._earnings(symbol, req.get("earnings_date"))
        signals = None
        if self.svc.settings.features.get("signal_calibration"):
            try:
                signals = self.signals(symbol)
            except (DataError, ValueError) as exc:
                log.info("No signal history for the evaluation of %s: %s", symbol, exc)
        out = evaluate(symbol=symbol, direction=direction, df=enriched, entry=float(entry), trend=trend, regime=regime,
                       data_quality=dq, earnings=earnings, signals=signals,
                       account_size=self.svc.settings.account_size, risk_pct=self.svc.settings.max_risk_per_trade_pct,
                       invalidation=req.get("invalidation_price"), target_1=req.get("target_1"),
                       target_2=req.get("target_2"), horizon=int(req.get("horizon_days") or 20))
        out["position_id"] = req.get("position_id")
        out["entry_source"] = "yours" if req.get("entry_price") is not None else "current price"
        out["earnings"] = earnings
        out["data_source"] = self.market.info.mode
        return out

    # --- options contract evaluator --------------------------------------------------------------------
    def evaluate_options(self, req: dict[str, Any], only: list[dict[str, Any]] | None = None) -> dict[str, Any]:
        """Which calls (bullish) or puts (bearish) fit the thesis; with `only`, just those contracts."""
        from .evaluator import suggest_levels
        from .options.evaluator import evaluate as evaluate_contracts, horizon_date, pick_expirations
        symbol = self.svc._symbol(req["symbol"])
        direction = 1 if str(req.get("direction", "BULLISH")).upper() in ("BULLISH", "LONG") else -1
        spot = self.market.spot(symbol)
        enriched = self.market.enriched(symbol, 3)
        sug = suggest_levels(enriched, direction, spot)
        t1 = req.get("target_price") or sug["target_1"]
        t2 = req.get("target_2") or (sug["target_2"] if not req.get("target_price") else t1 + (t1 - spot) * 0.5)
        inv = req.get("invalidation_price") or sug["invalidation"]
        horizon = int(req.get("horizon_days") or 20)
        today = datetime.now(NY).date()
        all_exps = self.market.expirations(symbol)
        if only:
            exps = sorted({str(c["expiration"]) for c in only})
            missing = [e for e in exps if e not in all_exps]
            if missing:
                raise ValueError(f"{symbol} has no options expiring on {', '.join(missing)}")
        elif req.get("expirations"):
            exps = [e for e in req["expirations"] if e in all_exps][:6]
            if not exps:
                raise ValueError("None of those expirations are listed for this stock")
        else:
            exps = pick_expirations(all_exps, today, horizon_date(today, horizon))
            if not exps:
                raise ValueError(f"No {symbol} expiration leaves time after a {horizon}-day horizon")
        analyses = [self.svc._chain_for(symbol, e) for e in exps]
        earnings = self.svc._earnings(symbol, req.get("earnings_date"))
        out = evaluate_contracts(symbol=symbol, direction=direction, spot=spot, analyses=analyses, target_1=float(t1),
                                 target_2=float(t2), invalidation=float(inv), horizon=horizon,
                                 rate=self.svc.settings.risk_free_rate, contracts=int(req.get("contracts") or 1),
                                 budget=req.get("budget"), earnings=earnings,
                                 only=[(str(c["option_type"]).lower(), float(c["strike"]), str(c["expiration"]))
                                       for c in only] if only else None)
        out["levels_source"] = {"target_1": "yours" if req.get("target_price") else "suggested",
                                "invalidation": "yours" if req.get("invalidation_price") else "suggested",
                                "notes": sug["notes"]}
        out["earnings"] = earnings
        try:
            stock = self.evaluate_position({"symbol": symbol, "direction": "LONG" if direction > 0 else "SHORT",
                                            "entry_price": spot, "invalidation_price": float(inv),
                                            "target_1": float(t1), "target_2": float(t2), "horizon_days": horizon})
            out["stock_grade"] = {"grade": stock["grade"], "score": stock["score"], "caps": stock["caps"],
                                  "components": stock["components"]}
        except (DataError, ValueError) as exc:
            out["stock_grade"] = {"error": str(exc)}
        out["data_source"] = self.market.info.mode
        return out

    # --- portfolio risk ----------------------------------------------------------------------------------
    def _views(self, status: str) -> list[dict[str, Any]]:
        desk = getattr(self.svc, "desk", None)
        if desk is None:
            from .desk import Desk
            desk = self.svc.desk = Desk(self.svc)
        return desk.list(status)["positions"]

    def daily_loss(self, views: list[dict[str, Any]] | None = None) -> dict[str, Any]:
        from . import portfolio
        views = self._views("all") if views is None else views
        relevant = [v for v in views if v["status"] in ("OPEN", "PARTIALLY_CLOSED", "CLOSED")]
        s = self.svc.settings
        return portfolio.daily_loss(relevant, s.daily_loss_mode, s.account_size, s.daily_loss_stop_pct)

    def portfolio(self, scenarios: list[dict[str, Any]] | None = None) -> dict[str, Any]:
        from . import portfolio
        views = self._views("all")
        open_views = [v for v in views if v["status"] in ("OPEN", "PARTIALLY_CLOSED")]
        bars: dict[str, pd.DataFrame] = {}
        notes = []
        for sym in sorted({v["symbol"] for v in open_views} | {"SPY"}):
            try:
                bars[sym] = self.market.bars(sym, 1, complete=True)
            except DataError as exc:
                notes.append(f"{sym}: no price history for correlation and beta ({exc}).")
        s = self.svc.settings
        out = portfolio.analyse(open_views, bars, s.account_size, s.max_open_risk_pct, self.daily_loss(views), self.sector,
                                scenarios)
        out["notes"] = notes
        out["data_source"] = self.market.info.mode
        out["limits"] = {"account_size": s.account_size, "max_open_risk_pct": s.max_open_risk_pct,
                         "daily_loss_stop_pct": s.daily_loss_stop_pct, "daily_loss_mode": s.daily_loss_mode}
        return out

    # --- journal analytics ----------------------------------------------------------------------------
    def _history_for(self, symbol: str, since: str | None, cache: dict[str, Any]) -> pd.DataFrame | None:
        if symbol not in cache:
            years = 1.0
            if since:
                years = min(5.0, max(1.0, (datetime.now(NY).date() - datetime.fromisoformat(since).date()).days / 365 + 0.2))
            try:
                cache[symbol] = self.market.bars(symbol, years)
            except DataError:
                cache[symbol] = None
        return cache[symbol]

    def _with_overrides(self, view: dict[str, Any]) -> dict[str, Any]:
        from .journal import JournalError
        if view.get("legacy_trade_id"):
            try:
                view["legacy_overrides"] = self.svc.journal.get(view["legacy_trade_id"]).get("overrides") or []
            except JournalError:                 # the classic entry is gone; the mirror still counts
                view["legacy_overrides"] = []
        return view

    def journal_stats(self) -> dict[str, Any]:
        from . import analytics
        views = [self._with_overrides(v) for v in self._views("closed")]
        limit = self.svc.settings.account_size * self.svc.settings.max_risk_per_trade_pct / 100.0
        cache: dict[str, Any] = {}
        rows = []
        for v in views:
            bars = self._history_for(v["symbol"], v.get("opened_at"), cache)
            rows.append(analytics.trade_row(v, analytics.excursions(v, bars), limit))
        out = analytics.journal_stats(rows)
        out["risk_limit_per_trade"] = limit
        return out

    def position_analytics(self, position_id: int) -> dict[str, Any]:
        from . import analytics
        desk = getattr(self.svc, "desk", None)
        if desk is None:
            from .desk import Desk
            desk = self.svc.desk = Desk(self.svc)
        v = self._with_overrides(desk.view(position_id))
        bars = self._history_for(v["symbol"], v.get("opened_at"), {})
        limit = self.svc.settings.account_size * self.svc.settings.max_risk_per_trade_pct / 100.0
        exc = analytics.excursions(v, bars, until=v.get("closed_at") or datetime.now(NY).isoformat())
        return analytics.position_analytics(v, exc, limit)

    # --- backtest V2 ---------------------------------------------------------------------------------
    def _backtest_inputs(self, symbols: list[str] | None, years: float) -> tuple[dict, dict, Any, list[str]]:
        names = [self.svc._symbol(s) for s in (symbols or self.svc.settings.watchlist) if s.strip()]
        if not names:
            raise ValueError("Give at least one symbol")
        if len(names) > 40:
            raise ValueError("At most 40 symbols per run")
        span = years + 1.1                       # the 200-day average and the 52-week range need a year first
        try:
            spy = self.market.bars("SPY", span, complete=True)
        except DataError:
            spy = None
        frames, states, warnings = {}, {}, []
        for sym in dict.fromkeys(names):
            try:
                df = self.market.enriched(sym, span, complete=True)
                sector = self.sector(sym)
                bench = {"SPY": spy}
                if sector:
                    try:
                        bench[sector] = self.market.bars(sector, span, complete=True)
                    except DataError:
                        pass
                frames[sym] = df
                states[sym] = trend_engine.state_series(df, bench, sector, None)
            except (DataError, ValueError) as exc:
                warnings.append(f"{sym}: {exc}")
        if not frames:
            raise ValueError("No price history could be loaded for those symbols")
        return frames, states, self._regime_by_date(span), warnings

    def _strategy(self, req: dict[str, Any]) -> dict[str, Any]:
        from .backtest_v2 import PRESETS
        if req.get("preset"):
            if req["preset"] not in PRESETS:
                raise ValueError(f"Unknown preset {req['preset']!r}; use one of {', '.join(PRESETS)}")
            base = PRESETS[req["preset"]]
            return {"long": base["long"], "short": base["short"], "regimes": req.get("regimes") or base["regimes"]}
        st = req.get("strategy") or {}
        return {"long": st.get("long") or [], "short": st.get("short") or [], "regimes": st.get("regimes") or []}

    def _bt_params(self, overrides: dict[str, Any] | None) -> Any:
        params = {"short_borrow_rate_pct": 0.5, **(overrides or {})}
        try:
            return self.svc._params(params)
        except Exception as exc:                 # the classic validator raises RequestError
            raise ValueError(str(exc)) from exc

    def _options_check(self, req: dict[str, Any]) -> None:
        if str(req.get("instrument") or "STOCK").upper() == "OPTION":
            from .options.history import provider_for
            provider = provider_for(self.svc.settings)
            if not provider.available():
                raise ValueError(provider.reason)

    def backtest(self, req: dict[str, Any]) -> dict[str, Any]:
        from . import backtest_v2
        self._options_check(req)
        years = max(1.0, min(float(req.get("years") or 3.0), 10.0))
        strategy = self._strategy(req)
        backtest_v2.validate(strategy)
        frames, states, regimes, warnings = self._backtest_inputs(req.get("symbols"), years)
        start = (datetime.now(NY).date() - pd.Timedelta(days=int(years * 365.25))).isoformat()
        out = backtest_v2.run(frames, states, regimes, strategy, self._bt_params(req.get("params")), start,
                              bool(req.get("monte_carlo", True)), int(req.get("n_trials") or 1))
        out["data_warnings"] = warnings
        out["data_source"] = self.market.info.mode
        out["trend_engine_version"] = TREND_ENGINE_VERSION
        if len(out["trades"]) > 500:
            out["trades_truncated"] = len(out["trades"])
            out["trades"] = out["trades"][-500:]
        return out

    def backtest_walk_forward(self, req: dict[str, Any]) -> dict[str, Any]:
        from . import backtest_v2
        self._options_check(req)
        years = max(2.0, min(float(req.get("years") or 4.0), 10.0))
        train, test = int(req.get("train_days") or 252), int(req.get("test_days") or 63)
        if not 60 <= train <= 1000 or not 20 <= test <= 500:
            raise ValueError("Training window must be 60 to 1000 days and test window 20 to 500 days")
        strategy = self._strategy(req)
        backtest_v2.validate(strategy)
        frames, states, regimes, warnings = self._backtest_inputs(req.get("symbols"), years)
        out = backtest_v2.run_walk_forward(frames, states, regimes, strategy, self._bt_params(req.get("params")),
                                           req.get("grid"), train, test, bool(req.get("anchored")))
        out["data_warnings"] = warnings
        out["data_source"] = self.market.info.mode
        return out

    # --- alerts ---------------------------------------------------------------------------------------
    def alerts(self) -> dict[str, Any]:
        """Alerts only: a list of things to look at. Nothing is acted on."""
        from . import alerts as alert_rules
        from .portfolio import exposure
        views = self._views("all")
        open_views = [v for v in views if v["status"] in ("OPEN", "PARTIALLY_CLOSED")]
        today = datetime.now(NY).date()
        found, errors = [], []
        for v in open_views:
            trend = None
            if self.svc.settings.features.get("trend_engine_v2"):
                try:
                    trend = self.trend(v["symbol"])
                except (DataError, ValueError) as exc:
                    errors.append(f"{v['symbol']}: {exc}")
            try:
                earnings = self.svc._earnings(v["symbol"], None)
            except Exception as exc:              # an earnings lookup must not hide the other alerts
                earnings = None
                log.info("No earnings date for %s: %s", v["symbol"], exc)
            found.extend(alert_rules.position_alerts(v, trend, earnings, today))
        s = self.svc.settings
        risks = [exposure(v, None)["risk_to_invalidation"] for v in open_views]
        open_risk = float(sum(r for r in risks if r is not None)) if open_views else None
        daily = self.daily_loss(views) if self.svc.settings.features.get("portfolio_risk") else None
        found.extend(alert_rules.account_alerts(daily, open_risk, s.account_size * s.max_open_risk_pct / 100.0))
        out = {"checked_at": datetime.now(NY).isoformat(timespec="seconds"), "alerts": alert_rules.sort_alerts(found),
               "open_positions": len(open_views), "errors": errors,
               "note": "Alerts only: nothing is bought, sold or sent. You decide what to do."}
        self.svc.db.cache_put(f"alerts:last:{self.market.info.mode}", {"checked_at": out["checked_at"], "count": len(found)})
        return out

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
