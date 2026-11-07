"""Research 3.0: one complete, saved investigation of a stock (the StudyOrchestrator).

Select a stock, a direction, a horizon and the kind of trade, and the study gathers, in one pass, what 2.x asked
for screen by screen: the trend and the market's regime; the stock's signals and any reversal forming; analogs of
today's situation in its own history and its peers'; the options market (implied volatility, the expected move,
the move priced for earnings); the unified forecast with its calibration; and every strategy compared under the
same scenarios. It is saved with an as-of time, the engines' versions and the inputs' fingerprint, so a trade made
from it can later be judged on what was known then. What could not be read is listed, never filled in.

The AI is optional and has one job here: to challenge the thesis from the evidence (what supports it, what
contradicts it, what is missing, what would prove it wrong). It writes no numbers and draws no path.
"""

from __future__ import annotations

import json
import math
import threading
import traceback
from datetime import datetime
from typing import Any

import numpy as np
import pandas as pd
from fastapi import APIRouter, Query
from pydantic import BaseModel, ConfigDict, Field

from .. import analogs as A
from .. import contracts as C
from .. import forecast as F
from .. import studio
from ..marketdays import add_trading_days
from ..optionmath import NY
from ..peers import peers_for
from ..premium import history as premium_history
from ..providers.base import DataError
from ..service import RequestError

ENGINE_VERSION = "study-3.0"
HORIZONS = (1, 3, 5, 10, 20)
THESES = ("intraday_bounce", "swing", "earnings_reaction", "reversal", "investment")
CAL_KEY = "forecast:calibration:{mode}:{h}"
MAX_RUNNING = 3


class StudyIn(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)
    symbol: str = Field(min_length=1, max_length=12)
    horizon_days: int = 5
    thesis: str = "swing"
    direction: str | None = None
    objective: str | None = Field(default=None, max_length=300)
    peers_add: list[str] = Field(default_factory=list, max_length=8)
    peers_remove: list[str] = Field(default_factory=list, max_length=20)
    opportunity_id: str | None = Field(default=None, max_length=64)


class CalibrateIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    horizon_days: int = 5
    symbols: list[str] | None = Field(default=None, max_length=40)
    years: float = Field(default=4.0, ge=2.0, le=8.0)


class ChallengeIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    provider: str = "auto"


def _iso(ts: Any) -> str:
    return pd.Timestamp(ts).isoformat()


def _finite(x: Any) -> float | None:
    try:
        f = float(x)
    except (TypeError, ValueError):
        return None
    return f if math.isfinite(f) else None


class Research:
    def __init__(self, svc: Any):
        self.svc = svc
        self._lock = threading.Lock()
        self._running: dict[str, threading.Thread] = {}
        self._calibrating: dict[str, Any] | None = None

    # --- studies -------------------------------------------------------------
    def check(self, req: StudyIn) -> StudyIn:
        req.symbol = self.svc._symbol(req.symbol)
        if req.horizon_days not in HORIZONS:
            raise RequestError(f"horizon_days must be one of {', '.join(map(str, HORIZONS))}")
        if req.thesis not in THESES:
            raise RequestError(f"thesis must be one of {', '.join(THESES)}")
        if req.direction is not None:
            req.direction = req.direction.upper()
            if req.direction not in ("LONG", "SHORT"):
                raise RequestError("direction must be LONG, SHORT or empty")
        return req

    def start(self, req: StudyIn) -> dict[str, Any]:
        req = self.check(req)
        with self._lock:
            alive = {k: t for k, t in self._running.items() if t.is_alive()}
            self._running = alive
            if len(alive) >= MAX_RUNNING:
                raise RequestError(f"{len(alive)} studies are already running; wait for one to finish.")
            study_id = C.new_id("study")
            now = C.utc_now()
            with self.svc.db.tx() as cur:
                cur.execute("""INSERT INTO studies (id, mode, symbol, created_at, as_of, horizon_days, thesis, direction, objective, status,
                               engine_version, contract_version, opportunity_id, inputs_json, evidence_json, fingerprint)
                               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'running', ?, ?, ?, ?, '{}', '')""",
                            (study_id, self.svc.market.info.mode, req.symbol, now, now, req.horizon_days, req.thesis, req.direction,
                             req.objective, ENGINE_VERSION, C.CONTRACT_VERSION, req.opportunity_id, req.model_dump_json()))
            t = threading.Thread(target=self._run_safely, args=(study_id, req), name=f"study-{req.symbol}", daemon=True)
            self._running[study_id] = t
            t.start()
        d3 = getattr(self.svc, "discover3", None)
        if req.opportunity_id and d3 is not None:
            d3.link_study(req.opportunity_id, study_id, req.symbol)
        self.svc.bus.publish("study.started", study_id, symbol=req.symbol, horizon_days=req.horizon_days)
        return {"id": study_id, "status": "running", "symbol": req.symbol}

    def _run_safely(self, study_id: str, req: StudyIn) -> None:
        try:
            self.run(req, study_id=study_id)
        except Exception as exc:  # noqa: BLE001 - the study is marked failed, with the reason
            from ..events import redact
            with self.svc.db.tx() as cur:
                cur.execute("UPDATE studies SET status = 'failed', error = ? WHERE id = ?", (redact(str(exc))[:500], study_id))
            self.svc.bus.publish("study.failed", study_id, symbol=req.symbol, error=str(exc)[:200])
            traceback.print_exc()

    def run(self, req: StudyIn, study_id: str | None = None) -> dict[str, Any]:
        """The whole study, synchronously (start() runs it in a thread)."""
        req = self.check(req)
        svc, sym, h = self.svc, req.symbol, req.horizon_days
        study_id = study_id or C.new_id("study")
        as_of = C.utc_now()
        missing: list[str] = []
        items: list[dict[str, Any]] = []
        lean = 1 if req.direction == "LONG" else -1 if req.direction == "SHORT" else 0

        def supports(sign: float | None) -> str:
            if sign is None or lean == 0 or sign == 0:
                return "neutral"
            return "for" if sign * lean > 0 else "against"

        # Price and history.
        q = svc.market.quote(sym)
        last = _finite(q.get("last")) or _finite(q.get("close"))
        bars = svc.market.bars(sym, 5.0, complete=True)
        try:
            spy = svc.market.bars("SPY", 5.0, complete=True)
        except DataError as exc:
            spy = None
            missing.append(f"The market's history (SPY): {exc}")
        # Peers and their histories.
        try:
            pr = peers_for(svc, sym, n=5, add=req.peers_add, remove=req.peers_remove)
        except (ValueError, DataError) as exc:
            pr = {"peers": [], "notes": [str(exc)]}
        target = A.prepare(sym, bars, spy, own=True)
        pool = [target]
        for p in pr["peers"]:
            try:
                pool.append(A.prepare(p["symbol"], svc.market.bars(p["symbol"], 5.0, complete=True), spy, own=False))
            except DataError as exc:
                missing.append(f"{p['symbol']}'s history: {exc}")
        if not pr["peers"]:
            missing.append("Comparable companies: " + (" ".join(pr.get("notes") or []) or "none found"))
        # The options market.
        vol = None
        try:
            from ..options.lab import OptionsLab
            vol = (getattr(svc, "lab", None) or OptionsLab(svc)).volatility(sym)
        except (ValueError, DataError) as exc:
            missing.append(f"The options market: {exc}")
        implied = None
        event = None
        if vol:
            iv30 = _finite(vol.get("iv30"))
            if iv30:
                implied = iv30 * math.sqrt(h / 252.0)
            ev, earn = vol.get("event") or {}, vol.get("earnings") or {}
            if isinstance(earn, dict) and earn.get("trading_days") is not None and ev.get("available"):
                event = {"in_days": int(earn["trading_days"]), "move_pct": _finite(ev.get("move_pct"))}
            items.append({"key": "options", "label": "What options price", "kind": "options", "source": svc.market.info.label,
                          "summary": (f"30-day implied volatility {100 * iv30:.0f}%" if iv30 else "No 30-day implied volatility")
                          + (f", IV rank {vol['iv_rank']['rank']:.0f}" if isinstance(vol.get("iv_rank"), dict) and _finite(vol['iv_rank'].get('rank')) is not None else "")
                          + (f"; earnings in {event['in_days']} trading days, priced to move {event['move_pct']:.1f}%" if event and event.get("move_pct") else "")
                          + ".", "data": {"iv30": iv30, "implied_sigma_pct": 100 * implied if implied else None, "event": event,
                                          "skew": vol.get("skew"), "iv_rank": vol.get("iv_rank")}})
        # Beta to the market and the market's spread to the horizon.
        beta = market_sigma = None
        if spy is not None:
            a = pd.Series(np.log(bars["close"].astype(float).to_numpy()), index=pd.to_datetime(bars["date"])).diff()
            b = pd.Series(np.log(spy["close"].astype(float).to_numpy()), index=pd.to_datetime(spy["date"])).diff()
            j = pd.concat([a, b], axis=1, join="inner").dropna().iloc[-250:]
            if len(j) >= 60 and j.iloc[:, 1].var() > 0:
                beta = float(j.iloc[:, 0].cov(j.iloc[:, 1]) / j.iloc[:, 1].var())
                market_sigma = float(j.iloc[-20:, 1].std() * math.sqrt(h))
        # The forecast (with the analogs inside it).
        cal = svc.db.cache_get(CAL_KEY.format(mode=svc.market.info.mode, h=h))
        fc = F.forecast(target, pool, h, calibration=cal, implied_sigma=implied, event=event, beta=beta, market_sigma=market_sigma,
                        last_price=last)
        found = fc.pop("analog_search", None) or {"analogs": [], "summary": {}, "notes": []}
        hs = (found.get("summary") or {}).get("by_horizon", {}).get(str(h))
        if hs:
            med = hs["median_pct"]
            items.append({"key": "analogs", "label": "Similar past situations", "kind": "analog", "source": A.ENGINE_VERSION,
                          "cases": hs["independent"], "supports": supports(np.sign(med)),
                          "summary": (f"{hs['independent']} independent cases of {A.EVENT_LABELS.get(found['kind'], found['kind'])} "
                                      f"({found['summary'].get('own', 0)} on {sym}, the rest on its peers): {h} days later the median was "
                                      f"{med:+.2f}%, the middle 80% {hs['p10_pct']:+.1f}% to {hs['p90_pct']:+.1f}%"
                                      + (f"; higher {100 * hs['up_frequency']:.0f}% of the time ({100 * hs['up_low']:.0f}-{100 * hs['up_high']:.0f}%)."
                                         if hs.get("up_frequency") is not None else ".")),
                          "data": {"kind": found["kind"], "regime_now": found.get("regime_now")}})
        else:
            missing.append("Similar past situations: " + (" ".join(found.get("notes") or []) or "none with a finished forward window"))
        cmp = A.peer_comparison(found.get("analogs") or [], h)
        if cmp.get("own") and cmp.get("peer"):
            items.append({"key": "peers", "label": "This stock against its peers", "kind": "peer",
                          "summary": f"After the same kind of event, {sym} itself: median {cmp['own']['median_pct']:+.2f}% "
                                     f"({cmp['own']['independent']} cases); its peers: {cmp['peer']['median_pct']:+.2f}% ({cmp['peer']['independent']}).",
                          "supports": supports(np.sign(cmp['own']['median_pct'])), "data": cmp})
        # The trend, the regime, signals and reversals (the 2.x engines).
        analyst = getattr(svc, "analyst", None)
        if analyst is None:
            from ..analysis import Analyst
            analyst = Analyst(svc)
        try:
            an = analyst.analysis(sym)
            tr = an.get("trend") or {}
            state = tr.get("state") or tr.get("label")
            items.append({"key": "trend", "label": "Trend", "kind": "technical", "source": "trend engine",
                          "summary": str((an.get("interpretation") or {}).get("summary") or f"The trend engine reads it as {str(state).lower()}.")[:400],
                          "supports": supports(1 if "UP" in str(state).upper() or "BULL" in str(state).upper() else
                                               -1 if "DOWN" in str(state).upper() or "BEAR" in str(state).upper() else 0),
                          "data": {"state": state}})
            reg = an.get("regime") or {}
            if reg:
                items.append({"key": "regime", "label": "The market's regime", "kind": "regime",
                              "summary": str(reg.get("label") or reg.get("state") or reg.get("summary") or fc.get("regime"))[:300],
                              "data": {"state": reg.get("state")}})
        except (DataError, ValueError) as exc:
            missing.append(f"Trend and regime: {exc}")
        try:
            sg = analyst.signals(sym)
            recent = [s for s in sg.get("signals") or [] if s.get("status") in ("CONFIRMED", "confirmed", "provisional", "PROVISIONAL", None)][:4]
            for s in recent:
                d = 1 if str(s.get("direction", "")).lower() in ("bullish", "long", "up") else -1 if str(s.get("direction", "")).lower() in ("bearish", "short", "down") else 0
                items.append({"key": f"signal:{s.get('key')}", "label": s.get("label") or s.get("key"), "kind": "technical",
                              "summary": f"{s.get('label')} on {s.get('date')}", "supports": supports(d), "as_of": s.get("date"),
                              "data": {"history": (s.get("history") or {}).get("this_stock")}})
        except (DataError, ValueError, AttributeError) as exc:
            missing.append(f"Signals: {exc}")
        # News.
        news = getattr(svc, "news", None)
        if news is not None and svc.settings.features.get("market_news"):
            try:
                n = news.company(sym, 7)
                tone = n.get("tone") or {}
                heads = [it.get("headline") for it in (n.get("items") or [])[:3] if it.get("headline")]
                items.append({"key": "news", "label": "News", "kind": "news", "source": ", ".join(n.get("sources") or []) or None,
                              "summary": (f"{len(n.get('items') or [])} items in a week" + (f"; tone {tone.get('label')}" if tone.get("label") else "")
                                          + (": " + " | ".join(heads) if heads else ".")),
                              "supports": supports(np.sign(tone.get("score") or 0) if isinstance(tone, dict) else 0)})
            except Exception as exc:  # noqa: BLE001 - news is optional
                missing.append(f"News: {exc}")
        # Strategies under the same scenarios.
        strategies: dict[str, Any] = {"candidates": [], "failed": [], "best": None}
        whatif = getattr(svc, "whatif", None)
        if whatif is None:
            from .. import factors as Fx
            from ..analysis import Analyst
            from ..chartview import ChartView
            from ..whatif import WhatIf
            whatif = WhatIf(svc, ChartView(svc, Analyst(svc)), Fx.Factors(svc, Fx.Stooq(enabled=False)))
        try:
            strategies = studio.compare(whatif, sym, forecast=fc, horizon_days=h, direction=req.direction,
                                        earnings_crush_points=studio.crush_points(vol, h))
        except (RequestError, DataError, ValueError) as exc:
            missing.append(f"Strategies: {exc}")
        # The record.
        if fc["calibration"]["status"] != "calibrated":
            missing.append("A validated forecast: " + fc["calibration"]["text"])
        evidence = C.EvidenceSnapshot(as_of=as_of, items=[C.EvidenceItem(**it) for it in items], missing=missing,
                                      provenance=C.Provenance(data_source=svc.market.info.label, as_of=as_of,
                                                              engines={"study": ENGINE_VERSION, "forecast": F.MODEL_VERSION,
                                                                       "analogs": A.ENGINE_VERSION, "contracts": C.CONTRACT_VERSION},
                                                              inputs_hash=C.digest({"req": req.model_dump(), "last_bar": str(bars["date"].iloc[-1]),
                                                                                    "last": last, "peers": [p["symbol"] for p in pr["peers"]]})))
        forecast_contract = C.ForecastVersion(
            model_version=fc["model_version"], horizon_days=h, base=C.ScenarioPath(**fc["base"]),
            alternatives=[C.ScenarioPath(**a) for a in fc["alternatives"]], components=fc["components"], weights=fc["weights"],
            calibration=C.Calibration(**fc["calibration"]), explanation=fc["explanation"], invalidators=fc["invalidators"])
        cands = []
        for c in strategies.get("candidates") or []:
            cands.append(C.StrategyCandidate(key=c["key"], label=c["label"], instrument=c["instrument"], legs=c["legs"], cost=c["cost"],
                                             credit=c["credit"], expected_pnl={k: v for k, v in c["expected_pnl"].items() if v is not None},
                                             pnl_p10=c["pnl_p10"], pnl_p50=c["pnl_p50"], pnl_p90=c["pnl_p90"], p_profit=c["p_profit"],
                                             theoretical_max_loss=c["theoretical_max_loss"], unlimited_loss=c["unlimited_loss"],
                                             modeled_worst_loss=c["modeled_worst_loss"], expected_shortfall=c["expected_shortfall"],
                                             capital_required=c["capital_required"], breakevens=c["breakevens"],
                                             iv_sensitivity={k: v for k, v in c["iv_sensitivity"].items() if v is not None},
                                             fill_likelihood=c["fill_likelihood"], executable=c["executable"], notes=c["notes"]))
        status = "complete" if not [m for m in missing if not m.startswith("A validated forecast")] else "partial"
        study = C.StudyRun(id=study_id, symbol=sym, created_at=as_of, horizon_days=h, thesis=req.thesis, direction=req.direction,
                           objective=req.objective, opportunity_id=req.opportunity_id, status=status, evidence=evidence,
                           forecast=forecast_contract, strategies=cands, fingerprint=evidence.provenance.inputs_hash or "")
        view = {"last": last, "future_times": self._future_times(bars, h), "peers": pr, "analogs": found, "strategies": strategies,
                "forecast": {k: fc[k] for k in ("median_pct", "low_pct", "high_pct", "sigma", "band_k", "regime")},
                "data_source": svc.market.info.label, "data_mode": svc.market.info.mode}
        self._save(study, req, view)
        self._record_forecast(study, fc, bars)
        svc.bus.publish("study.completed", study_id, symbol=sym, status=status, horizon_days=h)
        return {"study": json.loads(study.model_dump_json()), "view": view}

    def _future_times(self, bars: pd.DataFrame, h: int) -> list[int]:
        last = pd.Timestamp(bars["date"].iloc[-1]).date()
        out = []
        for k in range(1, h + 1):
            d = add_trading_days(last, k)
            out.append(int(datetime(d.year, d.month, d.day, 16, tzinfo=NY).timestamp()))
        return out

    def _save(self, study: C.StudyRun, req: StudyIn, view: dict[str, Any]) -> None:
        with self.svc.db.tx() as cur:
            cur.execute("SELECT 1 FROM studies WHERE id = ?", (study.id,))
            exists = cur.fetchone() is not None
            row = (self.svc.market.info.mode, study.symbol, study.created_at, study.created_at, study.horizon_days, study.thesis,
                   study.direction, study.objective, study.status, ENGINE_VERSION, C.CONTRACT_VERSION, study.opportunity_id,
                   req.model_dump_json(), study.evidence.model_dump_json(), study.forecast.model_dump_json() if study.forecast else None,
                   json.dumps([c.model_dump() for c in study.strategies]), study.fingerprint, json.dumps(view, default=str))
            if exists:
                cur.execute("""UPDATE studies SET mode=?, symbol=?, created_at=?, as_of=?, horizon_days=?, thesis=?, direction=?, objective=?,
                               status=?, engine_version=?, contract_version=?, opportunity_id=?, inputs_json=?, evidence_json=?,
                               forecast_json=?, strategies_json=?, fingerprint=?, view_json=?, error=NULL WHERE id=?""", row + (study.id,))
            else:
                cur.execute("""INSERT INTO studies (mode, symbol, created_at, as_of, horizon_days, thesis, direction, objective, status,
                               engine_version, contract_version, opportunity_id, inputs_json, evidence_json, forecast_json,
                               strategies_json, fingerprint, view_json, id) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""", row + (study.id,))

    def _record_forecast(self, study: C.StudyRun, fc: dict[str, Any], bars: pd.DataFrame) -> None:
        """Kept so the forecast can be judged later against what happened (the Portfolio's review)."""
        as_of = pd.Timestamp(bars["date"].iloc[-1]).date()
        target = F.record_target(as_of, study.horizon_days)
        with self.svc.db.tx() as cur:
            cur.execute("""INSERT OR IGNORE INTO forecast_records (mode, study_id, symbol, model_version, kind, made_at, as_of_date,
                           horizon_days, target_date, regime, median_pct, low_pct, high_pct, interval, baseline_json)
                           VALUES (?, ?, ?, ?, 'live', ?, ?, ?, ?, ?, ?, ?, ?, 0.8, ?)""",
                        (self.svc.market.info.mode, study.id, study.symbol, fc["model_version"], study.created_at, str(as_of),
                         study.horizon_days, str(target), fc.get("regime"), fc["median_pct"], fc["low_pct"], fc["high_pct"],
                         json.dumps({"no_move": 0.0})))

    def get(self, study_id: str) -> dict[str, Any]:
        r = self.svc.db.one("SELECT * FROM studies WHERE id = ?", (study_id,))
        if r is None:
            raise RequestError(f"No study {study_id}")
        out: dict[str, Any] = {"id": r["id"], "symbol": r["symbol"], "status": r["status"], "created_at": r["created_at"],
                               "horizon_days": r["horizon_days"], "thesis": r["thesis"], "direction": r["direction"], "error": r["error"]}
        if r["status"] in ("complete", "partial"):
            out["study"] = {"id": r["id"], "symbol": r["symbol"], "created_at": r["created_at"], "horizon_days": r["horizon_days"],
                            "thesis": r["thesis"], "direction": r["direction"], "status": r["status"],
                            "evidence": json.loads(r["evidence_json"]), "forecast": json.loads(r["forecast_json"]) if r["forecast_json"] else None,
                            "strategies": json.loads(r["strategies_json"] or "[]"), "fingerprint": r["fingerprint"]}
            out["view"] = json.loads(r["view_json"] or "{}")
        return out

    def recent(self, symbol: str | None = None, limit: int = 20) -> list[dict[str, Any]]:
        mode = self.svc.market.info.mode
        sql, params = "SELECT id, symbol, status, created_at, horizon_days, thesis, direction FROM studies WHERE mode = ?", [mode]
        if symbol:
            sql += " AND symbol = ?"
            params.append(self.svc._symbol(symbol))
        sql += " ORDER BY created_at DESC LIMIT ?"
        params.append(max(1, min(int(limit), 100)))
        return [dict(r) for r in self.svc.db.query(sql, params)]

    # --- the AI's challenge ------------------------------------------------------
    def challenge(self, study_id: str, provider: str = "claude") -> dict[str, Any]:
        got = self.get(study_id)
        if "study" not in got:
            raise RequestError("The study has not finished")
        s = got["study"]
        lines = [f"STOCK {s['symbol']}; thesis {s['thesis']}; direction {s['direction'] or 'not set'}; horizon {s['horizon_days']} trading days."]
        for it in s["evidence"]["items"]:
            lines.append(f"- [{it['kind']}; {it['supports']}] {it['label']}: {it['summary']}")
        if s["evidence"]["missing"]:
            lines.append("MISSING: " + "; ".join(s["evidence"]["missing"]))
        fc = s.get("forecast") or {}
        if fc:
            lines.append(f"FORECAST (computed by the app, not by you): calibration {fc['calibration']['status']}; {fc['calibration']['text']}")
        prompt = ("Challenge this trading thesis using only the evidence below. Do not predict prices or give numbers of your own; "
                  "do not suggest position sizes. Answer with one JSON object: {\"supporting\": [..], \"contradicting\": [..], "
                  "\"missing\": [..], \"invalidators\": [..], \"questions\": [..]}, each a list of short sentences.\n\n" + "\n".join(lines))
        system = "You are a sceptical research analyst. You find weaknesses in trade theses. You never invent figures."
        provider = provider.lower()
        if provider == "auto":                       # whichever is set up: Claude first, then ChatGPT
            ai_, gpt_ = getattr(self.svc, "ai", None), getattr(self.svc, "chatgpt", None)
            provider = "claude" if ai_ is not None and ai_.configured else "chatgpt" if gpt_ is not None and gpt_.configured else ""
            if not provider:
                raise RequestError("No AI is set up: add ANTHROPIC_API_KEY or OPENAI_API_KEY to .env to have a thesis challenged.")
        if provider == "chatgpt":
            gpt = getattr(self.svc, "chatgpt", None)
            if gpt is None or not gpt.configured:
                raise RequestError("ChatGPT is not set up (OPENAI_API_KEY)")
            reply = gpt.ask_json(purpose="study_challenge", prompt=prompt, system=system, max_tokens=1500)
            parsed, model = reply["json"], reply["model"]
        else:
            ai = getattr(self.svc, "ai", None)
            if ai is None or not ai.configured:
                raise RequestError("Claude is not set up (ANTHROPIC_API_KEY)")
            from ..ai import extract_json
            reply = ai.ask(purpose="study_challenge", prompt=prompt, system=system, max_tokens=1500, web_search=False)
            parsed, model = extract_json(reply["text"]), reply["model"]
        if not isinstance(parsed, dict):
            raise RequestError("The AI's answer was not the JSON asked for. Try again.")
        clean = {k: [str(x)[:300] for x in (parsed.get(k) or []) if isinstance(x, (str, int, float))][:8]
                 for k in ("supporting", "contradicting", "missing", "invalidators", "questions")}
        self.svc.bus.publish("study.challenged", study_id, provider=provider)
        return {"study_id": study_id, "provider": provider, "model": model, **clean,
                "note": "Written by a language model from the study's evidence; it can be wrong, and it gives no numbers."}

    # --- calibration ---------------------------------------------------------------
    def calibration(self, horizon: int) -> dict[str, Any]:
        cal = self.svc.db.cache_get(CAL_KEY.format(mode=self.svc.market.info.mode, h=horizon))
        job = self._calibrating
        return {"horizon_days": horizon, "calibration": cal, "contract": F.calibration_contract(cal, horizon),
                "job": {k: v for k, v in (job or {}).items() if k != "thread"} or None}

    def calibrate_now(self, horizon: int, symbols: list[str] | None = None, years: float = 4.0, progress=None) -> dict[str, Any]:
        svc = self.svc
        names = [svc._symbol(s) for s in (symbols or list(svc.settings.watchlist)[:20])]
        try:
            spy = svc.market.bars("SPY", years, complete=True)
        except DataError:
            spy = None
        prepared: dict[str, A.Series] = {}
        for s in dict.fromkeys(names):
            try:
                prepared[s] = A.prepare(s, svc.market.bars(s, years, complete=True), spy, own=True)
            except DataError:
                continue
        from ..sectors import TICKER_FUND
        peers = {s: [p for p in prepared if p != s and TICKER_FUND.get(p) and TICKER_FUND.get(p) == TICKER_FUND.get(s)][:3] for s in prepared}
        rows = F.walk_forward(prepared, peers, horizon, progress=progress)
        cal = F.fit(rows, horizon)
        cal.update({"symbols": list(prepared), "years": years, "data_source": svc.market.info.label,
                    "run_at": C.utc_now(), "rows": int(len(rows))})
        svc.db.cache_put(CAL_KEY.format(mode=svc.market.info.mode, h=horizon), cal)
        svc.bus.publish("study.calibrated", None, horizon_days=horizon, status=cal["status"])
        return cal

    def calibrate(self, req: CalibrateIn) -> dict[str, Any]:
        if req.horizon_days not in HORIZONS:
            raise RequestError(f"horizon_days must be one of {', '.join(map(str, HORIZONS))}")
        with self._lock:
            if self._calibrating and self._calibrating["thread"].is_alive():
                raise RequestError("A calibration is already running")
            state: dict[str, Any] = {"horizon_days": req.horizon_days, "state": "running", "done": 0, "total": 0, "current": None,
                                     "started_at": C.utc_now(), "error": None}

            def progress(done: int, total: int, current: str) -> None:
                state.update(done=done, total=total, current=current)

            def work() -> None:
                try:
                    self.calibrate_now(req.horizon_days, req.symbols, req.years, progress)
                    state["state"] = "done"
                except Exception as exc:  # noqa: BLE001
                    state.update(state="failed", error=str(exc)[:300])
            t = threading.Thread(target=work, name="forecast-calibration", daemon=True)
            state["thread"] = t
            self._calibrating = state
            t.start()
        return {k: v for k, v in state.items() if k != "thread"}


def install(svc: Any) -> None:
    from ..api_v3 import register
    svc.research3 = Research(svc)

    def routes(router: APIRouter, svc: Any) -> None:
        r = svc.research3

        @router.post("/studies")
        def start(body: StudyIn) -> dict[str, Any]:
            return r.start(body)

        @router.get("/studies")
        def recent(symbol: str | None = None, limit: int = 20) -> dict[str, Any]:
            return {"studies": r.recent(symbol, limit)}

        @router.get("/studies/{study_id}")
        def one(study_id: str) -> dict[str, Any]:
            return r.get(study_id)

        @router.post("/studies/{study_id}/challenge")
        def challenge(study_id: str, body: ChallengeIn) -> dict[str, Any]:
            return r.challenge(study_id, body.provider)

        @router.get("/peers/{symbol}")
        def peers(symbol: str, add: str = "", remove: str = "") -> dict[str, Any]:
            try:
                return peers_for(svc, svc._symbol(symbol), add=[x for x in add.split(",") if x], remove=[x for x in remove.split(",") if x])
            except ValueError as exc:
                raise RequestError(str(exc)) from exc

        @router.get("/forecast/calibration")
        def calibration(horizon_days: int = Query(5)) -> dict[str, Any]:
            return r.calibration(horizon_days)

        @router.post("/forecast/calibrate")
        def calibrate(body: CalibrateIn) -> dict[str, Any]:
            return r.calibrate(body)

        @router.get("/options/history")
        def option_history(option_symbol: str, days: int = Query(90, ge=5, le=730)) -> dict[str, Any]:
            try:
                return premium_history(svc, option_symbol, days)
            except ValueError as exc:
                raise RequestError(str(exc)) from exc

    register(svc, routes)


__all__ = ["Research", "StudyIn", "install", "HORIZONS", "THESES", "ENGINE_VERSION"]
