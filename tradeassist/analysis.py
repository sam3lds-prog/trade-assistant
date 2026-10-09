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
REVERSAL_YEARS = 5
REVERSAL_KEYS = ("reversal:bottom_candidate", "reversal:bottom_confirmed", "reversal:top_candidate",
                 "reversal:top_confirmed")
REVERSAL_CASES = {"reversal:bottom_candidate": "potential bottoms", "reversal:bottom_confirmed": "confirmed reversals up",
                  "reversal:top_candidate": "potential tops", "reversal:top_confirmed": "confirmed reversals down"}


class Analyst:
    def __init__(self, svc: Any):
        self.svc = svc
        self.market = svc.market
        self._cache: dict[tuple, tuple[float, Any]] = {}
        self._sectors: dict[str, str | None] = {}
        self._reversal_cases: dict[str, dict[str, list[dict[str, Any]]]] = {}   # this stock's past readings
        self._reversal_pooled: dict[str, list[dict[str, Any]]] = {}            # all stocks', from the history

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

    def benchmarks(self, symbol: str, sector: str | None,
                   years: float = BENCH_YEARS) -> tuple[dict[str, pd.DataFrame | None], list[str]]:
        out: dict[str, pd.DataFrame | None] = {}
        notes: list[str] = []
        for etf in dict.fromkeys(["SPY", "QQQ"] + ([sector] if sector else [])):
            if etf == symbol:
                continue
            try:
                out[etf] = self.market.bars(etf, years)
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
        bench, _ = self.benchmarks(symbol, sector, years)
        states = trend_engine.state_series(df, bench, sector, None)
        last = states.iloc[-1]
        rs = relative_strength(df, bench, sector)
        regime = None
        try:
            spy = complete_only(self.market.enriched("SPY", years))
            spy = spy[spy["date"] <= cut].reset_index(drop=True)
            qqq = complete_only(self.market.enriched("QQQ", years))
            qqq = qqq[qqq["date"] <= cut].reset_index(drop=True)
            vix = None
            if self.market.info.mode != "demo":       # as the live regime does, when the source has VIX
                try:
                    vix = self.market.bars("VIX", years, complete=True)
                    vix = vix[vix["date"] <= cut]
                except DataError:
                    vix = None
            if len(spy) >= 60:
                regime = regime_engine.regime_series(spy, qqq if len(qqq) >= 30 else None, vix)["regime"].iloc[-1]
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
        # Trend changes are found the way the history was recorded: without validated-pattern points,
        # so a live change is the same kind of event as the past cases it is compared with.
        sector = self.sector(symbol)
        bench, _ = self.benchmarks(symbol, sector)
        states = trend_engine.state_series(enriched, bench, sector, None)
        complete = (enriched["bar_complete"].to_numpy(dtype=bool) if "bar_complete" in enriched.columns
                    else np.ones(len(enriched), dtype=bool))
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
        st = states["trend_state"].to_numpy()
        for i in range(max(1, len(st) - 10), len(st)):
            if st[i] != st[i - 1]:
                key = f"trend:{st[i]}"
                live.append({"key": key, "label": signalbook.signal_label(key), "direction": signalbook.signal_direction(key),
                             "date": enriched["date"].iloc[i].strftime("%Y-%m-%d"),
                             "status": "CONFIRMED" if complete[i] else "PROVISIONAL", "lab": []})
        live.sort(key=lambda x: x["date"], reverse=True)
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
        out["score"] = self._score_calibration(symbol, enriched, meta, regime_now, states)
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
                           regime_now: str | None, states: pd.DataFrame) -> dict[str, Any]:
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

    def _previous_close(self, view: dict[str, Any], today: Any) -> tuple[float | None, str | None]:
        """Yesterday's closing value of what a position holds: for stock, the last daily close before
        today; for an option, the chain's previous close, or else an estimate from today's price, its
        delta and theta, and the stock's move since yesterday's close."""
        try:
            bars = self.market.bars(view["symbol"], 1)
            before = bars[bars["date"] < pd.Timestamp(today)]
            stock_prev = float(before["close"].iloc[-1]) if len(before) else None
        except DataError:
            stock_prev = None
        if view["instrument_type"] == "STOCK":
            return stock_prev, "previous daily close" if stock_prev is not None else None
        mark = view.get("mark")
        if not mark or mark.get("price") is None:
            try:
                mark = self.svc.desk.mark(view)
            except (DataError, ValueError, AttributeError):
                return None, None
        if mark.get("prev_close") is not None:
            return float(mark["prev_close"]), "the option's previous close"
        delta, theta, price, under = (mark.get(k) for k in ("delta", "theta", "price", "underlying"))
        if None in (delta, price, under) or stock_prev is None:
            return None, None
        return max(0.0, price - delta * (under - stock_prev) - (theta or 0.0)), "estimated from delta and theta"

    def daily_loss(self, views: list[dict[str, Any]] | None = None) -> dict[str, Any]:
        from . import portfolio
        views = self._views("all") if views is None else views
        today = datetime.now(NY).date()
        start = datetime.now(NY).replace(hour=0, minute=0, second=0, microsecond=0)
        relevant = []
        for v in views:
            if v["status"] not in ("OPEN", "PARTIALLY_CLOSED", "CLOSED"):
                continue
            touched_today = any(datetime.fromisoformat(e["executed_at"]) >= start for e in v["executions"])
            if v["status"] == "CLOSED" and not touched_today:
                continue                                  # closed before today: nothing to count
            held_overnight = any(datetime.fromisoformat(e["executed_at"]) < start for e in v["executions"])
            if held_overnight:
                v = {**v}
                v["prev_close"], v["prev_source"] = self._previous_close(v, today)
            relevant.append(v)
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
                years = min(10.0, max(1.0, (datetime.now(NY).date() - datetime.fromisoformat(since).date()).days / 365 + 0.2))
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
        earliest: dict[str, str] = {}
        for v in views:                           # one fetch per stock, long enough for its oldest trade
            if v.get("opened_at") and (v["symbol"] not in earliest or v["opened_at"] < earliest[v["symbol"]]):
                earliest[v["symbol"]] = v["opened_at"]
        rows = []
        for v in views:
            bars = self._history_for(v["symbol"], earliest.get(v["symbol"]), cache)
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
        risks = [exposure(v, None)["open_risk"] for v in open_views]
        open_risk = float(sum(r for r in risks if r is not None)) if open_views else None
        daily = self.daily_loss(views) if self.svc.settings.features.get("portfolio_risk") else None
        found.extend(alert_rules.account_alerts(daily, open_risk, s.account_size * s.max_open_risk_pct / 100.0,
                                                sum(1 for r in risks if r is None)))
        out = {"checked_at": datetime.now(NY).isoformat(timespec="seconds"), "alerts": alert_rules.sort_alerts(found),
               "open_positions": len(open_views), "errors": errors,
               "note": "Alerts only: nothing is bought, sold or sent. You decide what to do."}
        self.svc.db.cache_put(f"alerts:last:{self.market.info.mode}", {"checked_at": out["checked_at"], "count": len(found)})
        return out

    # --- reversals: potential bottoms and tops ------------------------------------------------------
    def reversal(self, symbol: str, history: bool = True) -> dict[str, Any]:
        """Is `symbol` at a potential bottom or top, or has a reversal been confirmed (reversal.py)?
        With `history`, also how the same readings turned out before, on this stock over five years
        and across the stocks in the signal history."""
        symbol = self.svc._symbol(symbol)
        enriched = self.market.enriched(symbol, REVERSAL_YEARS)
        info = self.market.bar_info(symbol)
        meta = self.history_meta() if history else None
        key = ("reversal", symbol, info.get("fetched_at"), len(enriched), history, (meta or {}).get("run_at"))
        return self._cached(key, lambda: self._reversal(symbol, enriched, history, meta))

    def _reversal(self, symbol: str, enriched: pd.DataFrame, history: bool,
                  meta: dict[str, Any] | None) -> dict[str, Any]:
        from . import reversal as reversal_engine
        sector = self.sector(symbol)
        bench, notes = self.benchmarks(symbol, sector, years=REVERSAL_YEARS)
        # Without validated-pattern points, as the signal history records them.
        states = trend_engine.state_series(enriched, bench, sector, None)
        ev = reversal_engine.evaluate(enriched, states)
        out = reversal_engine.analyse(enriched, symbol, states, ev=ev)
        dates = enriched["date"].dt.strftime("%Y-%m-%d").tolist()
        ci = dates.index(out["confirmed"]["date"])
        out["trend_state"] = {"state": str(states["trend_state"].iloc[ci]), "date": dates[ci]}
        out["notes"] = notes
        if self.market.info.mode == "demo":
            out["notes"].insert(0, "Demo prices are synthetic: this reading says nothing about the real stock.")
        out["data_source"] = self.market.info.mode
        if not history:
            return out
        # This stock's own past readings, on finished bars only, with what followed each.
        complete = (enriched["bar_complete"].to_numpy(dtype=bool) if "bar_complete" in enriched.columns
                    else np.ones(len(enriched), dtype=bool))
        n_done = int(np.flatnonzero(complete)[-1]) + 1 if complete.any() else 0
        done = enriched.iloc[:n_done].reset_index(drop=True)
        spy = bench.get("SPY") if symbol != "SPY" else enriched
        outcome = signalbook.outcomes(done, spy) if n_done else pd.DataFrame()
        cases: dict[str, list[dict[str, Any]]] = {k: [] for k in REVERSAL_KEYS}
        atr_pct = signalbook.atr_share(done)
        for i, k in reversal_engine.events(ev):
            if i >= n_done:
                continue
            o = outcome.iloc[i]
            cases[k].append({"symbol": symbol, "date": dates[i], "close": float(done["close"].iloc[i]),
                             "atr_pct": float(atr_pct[i]) if np.isfinite(atr_pct[i]) else None,
                             **{c: (float(o[c]) if np.isfinite(o[c]) else None) for c in outcome.columns}})
        self._reversal_cases[symbol] = cases
        pooled: dict[str, list[dict[str, Any]]] = {}
        source = self.market.info.mode
        if meta:
            pooled = {k: signalbook.load(self.svc.db, source, k) for k in REVERSAL_KEYS}
            self._reversal_pooled = pooled
        past = []
        for k in REVERSAL_KEYS:
            direction = signalbook.signal_direction(k)
            mine = cases[k]
            past.append({"key": k, "label": signalbook.signal_label(k), "direction": direction,
                         "this_stock": [signalbook.summarize(mine, direction, h) for h in signalbook.HORIZONS],
                         "all_stocks": [signalbook.summarize(pooled[k], direction, h) for h in signalbook.HORIZONS]
                         if meta else None,
                         "recent": [{"date": r["date"], "close": r["close"], "fwd_10": r.get("fwd_10"),
                                     "fwd_20": r.get("fwd_20")} for r in mine[-6:]][::-1]})
        out["past"] = {"years": REVERSAL_YEARS, "signals": past, "history_built": bool(meta),
                       "history_run_at": meta["run_at"] if meta else None,
                       "history_symbols": len(meta["symbols"]) if meta else 0}
        if not meta:
            out["notes"].append("The signal history has not been built, so only this stock's own past cases are "
                                "counted. Build it from the Pattern lab screen to add the other stocks'.")
        elif meta.get("signal_engine_version") != SIGNAL_ENGINE_VERSION or not any(pooled.values()):
            out["notes"].append("The signal history was built before reversal signals were recorded; rebuild it on "
                                "the Pattern lab screen to include them.")
        return out

    def reversal_scan(self, symbols: list[str] | None = None) -> dict[str, Any]:
        """The reversal reading for each stock on the watchlist (or `symbols`), strongest first."""
        from .reversal import STAGE_RANK
        names = [s.strip().upper() for s in (symbols or self.svc.settings.watchlist) if s.strip()]
        if not names:
            raise ValueError("Give at least one symbol")
        if len(names) > 60:
            raise ValueError("At most 60 symbols per scan")
        rows, errors = [], []
        for sym in dict.fromkeys(names):
            try:
                r = self.reversal(sym, history=False)
            except (DataError, ValueError) as exc:
                errors.append(f"{sym}: {exc}")
                continue
            day = r["confirmed"]
            lead = day["leading"]
            pick = day[lead] if lead else max(day["bottom"], day["top"], key=lambda s: s["score"])
            prov = r["provisional"]
            rows.append({"symbol": r["symbol"], "date": day["date"], "leading": lead, "stage": pick["stage"],
                         "side": pick["side"], "score": pick["score"], "exhaustion": pick["exhaustion"],
                         "confirmation": pick["confirmation"], "since": pick["since"],
                         "bottom": {"stage": day["bottom"]["stage"], "score": day["bottom"]["score"]},
                         "top": {"stage": day["top"]["stage"], "score": day["top"]["score"]},
                         "close": pick["levels"]["close"], "invalidation": pick["levels"]["invalidation"],
                         "target_1": pick["levels"]["target_1"], "headline": day["headline"],
                         "trend_state": r["trend_state"]["state"],
                         "today": ({"stage": prov[pick["side"]]["stage"], "date": prov["date"]}
                                   if prov and prov[pick["side"]]["stage"] != pick["stage"] else None)})
        rows.sort(key=lambda x: (STAGE_RANK[x["stage"]] if x["leading"] else 0, x["score"]), reverse=True)
        return {"checked_at": datetime.now(NY).isoformat(timespec="seconds"), "rows": rows, "errors": errors,
                "data_source": self.market.info.mode,
                "note": "Stages are read on the last finished bar. A true bottom or top is only known afterwards; "
                        "points are evidence, not chances."}

    def reversal_options(self, req: dict[str, Any]) -> dict[str, Any]:
        """Calls for a potential bottom, puts for a potential top: each contract valued by the options
        evaluator with the reversal's own levels, scored out of 100 (options/reversal_score.py), and
        the best one simulated, with what it would have made after the past cases of the same reading."""
        from datetime import date as _date
        from .options import reversal_score
        from .versions import OPTIONS_EVALUATOR_VERSION, REVERSAL_ENGINE_VERSION
        symbol = self.svc._symbol(req["symbol"])
        rv = self.reversal(symbol)
        day = rv["confirmed"]
        notes: list[str] = []
        own = req.get("contracts_to_score") or None
        side = str(req.get("side") or "auto").lower()
        if side not in ("auto", "bottom", "top"):
            raise ValueError("side must be auto, bottom or top")
        if own:
            kinds = {str(c["option_type"]).lower() for c in own}
            if len(kinds) > 1:
                raise ValueError("Score calls or puts, not both at once: calls are for a bottom, puts for a top")
            own_side = "bottom" if kinds == {"call"} else "top"
            if side == "auto":
                side = own_side
            elif side != own_side:
                raise ValueError(f"{'Calls' if own_side == 'bottom' else 'Puts'} are for a potential {own_side}, "
                                 f"not a {side}")
        if side == "auto":
            side = day["leading"] or ("bottom" if day["bottom"]["score"] >= day["top"]["score"] else "top")
            if not day["leading"]:
                notes.append("Neither a potential bottom nor a potential top is in sight, so the contracts were scored "
                             f"on the stronger side ({side}) and the setup counts for little.")
        sv = day[side]
        d = 1 if side == "bottom" else -1
        lv = sv["levels"]
        names = {"target_1": lv["target_1_is"], "target_2": lv["target_2_is"]}
        spot = self.market.spot(symbol)
        inv = float(req.get("invalidation_price") or lv["invalidation"])
        t1 = float(req.get("target_price") or lv["target_1"])
        t2 = float(req.get("target_2") or lv["target_2"])
        if d * (spot - inv) <= 0:
            raise ValueError(f"{symbol} at ${spot:,.2f} is already {'below' if d > 0 else 'above'} the invalidation "
                             f"price ${inv:,.2f}: the potential {side} has failed for now.")
        if d * (t1 - spot) <= 0:
            if req.get("target_price") or d * (t2 - spot) <= 0:
                raise ValueError(f"{symbol} at ${spot:,.2f} is already past the target ${t1:,.2f}: the move this "
                                 "reversal pointed to has happened. Give a target further out to score contracts.")
            notes.append(f"{symbol} is already past target 1 (${t1:,.2f}), so target 2 is used as the target.")
            t1, names["target_1"] = t2, names["target_2"]
            t2, names["target_2"] = t1 + d * abs(t1 - spot) * 0.5, "half as far again"
        if d * (t2 - t1) <= 0:
            t2, names["target_2"] = t1 + d * abs(t1 - spot) * 0.5, "half as far again"
        if req.get("target_price"):
            names["target_1"] = "your target"
        horizon = int(req.get("horizon_days") or 20)
        contracts = int(req.get("contracts") or 1)
        ev = self.evaluate_options({"symbol": symbol, "direction": "BULLISH" if d > 0 else "BEARISH",
                                    "target_price": t1, "target_2": t2, "invalidation_price": inv,
                                    "horizon_days": horizon, "contracts": contracts, "budget": req.get("budget"),
                                    "expirations": req.get("expirations"), "earnings_date": req.get("earnings_date")},
                                   only=own)
        thesis = ev["thesis"]
        enriched = self.market.enriched(symbol, 3, complete=True)
        hv = float(enriched["hv20"].iloc[-1]) if "hv20" in enriched.columns and len(enriched) else float("nan")
        hv20 = hv if math.isfinite(hv) and hv > 0 else None
        ivr = self.svc.iv_rank(symbol)
        iv_rank = ivr["rank"] if ivr.get("days", 0) >= 30 and ivr.get("rank") is not None else None
        if iv_rank is None:
            notes.append("Too few days of recorded implied volatility for a rank, so the volatility check compares "
                         "implied with actual volatility only.")
        earn = ev.get("earnings") or {}
        ed = _date.fromisoformat(earn["date"]) if earn.get("date") else None
        today = datetime.now(NY).date()
        # Past cases of the same reading, for the analog simulation: across stocks when there are enough.
        key = f"reversal:{side}_{'confirmed' if sv['stage'] == 'CONFIRMED' else 'candidate'}"
        short = [h for h in signalbook.HORIZONS if h <= horizon]
        h_past = max(short) if short else min(signalbook.HORIZONS)
        pooled = [r for r in getattr(self, "_reversal_pooled", {}).get(key, [])
                  if r.get(f"fwd_{h_past}") is not None]
        mine = [r for r in self._reversal_cases.get(symbol, {}).get(key, []) if r.get(f"fwd_{h_past}") is not None]
        if not short:
            cases, where = [], ""
            notes.append(f"Past cases are measured over {min(signalbook.HORIZONS)} trading days or more, longer than "
                         f"this {horizon}-day horizon, so the contract is not simulated on them.")
        elif len(pooled) >= signalbook.MIN_CASES:
            cases, where = pooled, f"after {REVERSAL_CASES[key]} across the stocks in the signal history"
        elif len(mine) >= 8:
            cases, where = mine, f"after {REVERSAL_CASES[key]} on {symbol} over {REVERSAL_YEARS} years"
            if len(mine) < signalbook.MIN_CASES:
                notes.append(f"Only {len(mine)} past cases on {symbol}: treat the simulation of past cases as a rough "
                             "guide.")
        else:
            cases, where = [], ""
            notes.append("Too few past cases of this reading to simulate the contract on them"
                         + ("." if rv["past"]["history_built"] else "; building the signal history adds other stocks'."))

        now = datetime.now(NY)
        rate = self.svc.settings.risk_free_rate

        def past_cases(k: dict[str, Any]) -> dict[str, Any] | None:
            if not cases or k.get("premium") is None:
                return None
            try:
                a = self.svc._chain_for(symbol, k["expiration"])
                row = a.find_by_terms(k["option_type"].lower(), k["strike"])
                return reversal_score.analog_outcomes(k, row, a, cases, direction=d, spot=spot, invalidation=inv,
                                                      horizon=h_past, rate=rate, contracts=contracts, now=now,
                                                      source=where, atr_pct=lv["atr"] / lv["close"]) if row else None
            except (DataError, ValueError) as exc:
                return {"error": str(exc)}

        for k in ev["contracts"]:
            before = ed is not None and today <= ed <= _date.fromisoformat(k["expiration"])
            k["reversal_score"] = reversal_score.score_contract(k, sv, {**thesis, "target_1": t1}, hv20, iv_rank, before)
            k["past_cases"] = past_cases(k)
            reversal_score.weigh_past_cases(k["reversal_score"], k["past_cases"], signalbook.MIN_CASES)
        # Best verdict first, then the highest score: a capped contract ranks below an uncapped one.
        ranked = sorted(ev["contracts"], key=lambda k: (reversal_score.VERDICT_ORDER.index(k["reversal_score"]["verdict"]),
                                                        k["reversal_score"]["score"]), reverse=True)

        def detail(k: dict[str, Any]) -> dict[str, Any]:
            return {**k, "outcomes": reversal_score.outcome_table(k, {**thesis, "target_1": t1, "target_2": t2}, names)}

        def simulated(k: dict[str, Any]) -> dict[str, Any]:
            out = detail(k)
            if k.get("premium") is not None:
                h_day = min(_date.fromisoformat(thesis["horizon_date"]), _date.fromisoformat(k["expiration"]))
                try:
                    out["simulation"] = self.svc.simulate(symbol=symbol, eval_date=h_day.isoformat(), legs=[{
                        "expiration": k["expiration"], "option_type": k["option_type"].lower(), "strike": k["strike"],
                        "side": "buy", "contracts": contracts, "entry_price": k["mid"]}])
                except (DataError, ValueError) as exc:
                    out["simulation"] = {"error": str(exc)}
            return out

        # With your own contracts, each is shown in full; otherwise the best-ranked one.
        mine_full = [simulated(c) for c in ranked] if own else None
        best = (mine_full[0] if mine_full else simulated(ranked[0])) if ranked else None
        compact = [{k: v for k, v in c.items() if k != "scenarios"} for c in ranked]
        prov = rv["provisional"]
        return {"version": REVERSAL_ENGINE_VERSION, "options_evaluator_version": OPTIONS_EVALUATOR_VERSION,
                "symbol": symbol, "side": side, "contract_type": "CALL" if d > 0 else "PUT",
                "setup": {"date": day["date"], "stage": sv["stage"], "score": sv["score"], "exhaustion": sv["exhaustion"],
                          "confirmation": sv["confirmation"], "text": sv["text"], "levels": lv,
                          "today": prov[side]["stage"] if prov and prov[side]["stage"] != sv["stage"] else None},
                "thesis": {**thesis, "target_1_is": names["target_1"], "target_2_is": names["target_2"],
                           "invalidation_is": "your price" if req.get("invalidation_price") else lv["invalidation_is"]},
                "verdict_counts": {v: sum(1 for c in ranked if c["reversal_score"]["verdict"] == v)
                                   for v in ("GOOD", "FAIR", "POOR", "AVOID")},
                "best": best, "own": mine_full, "contracts": compact,
                "market": ev["market"], "earnings": ev.get("earnings"), "stock_grade": ev.get("stock_grade"),
                "exit_rule": ev["exit_rule"], "weights": reversal_score.WEIGHTS,
                "notes": notes + ev["notes"] + ["The score summarises the checks; it is not a probability. GOOD is 70 or "
                                                "more, FAIR 50 to 69, POOR below 50, AVOID when a check rules it out."],
                "data_source": self.market.info.mode}

    # --- everything about one stock --------------------------------------------------------------
    def analysis(self, symbol: str) -> dict[str, Any]:
        symbol = self.svc._symbol(symbol)
        out: dict[str, Any] = {"symbol": symbol, "data_source": self.market.info.mode}
        bars = self.market.bars(symbol, 3)
        out["data_quality"] = assess(symbol, bars, self.market.bar_info(symbol))
        out["data_quality"]["bars_freshness"] = self.market.bars_freshness(symbol)
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
                            "prevclose": q.get("prevclose"), "description": q.get("description"),
                            "freshness": q.get("_meta")}
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
