"""Research 3.0: one complete, saved investigation of a stock (the StudyOrchestrator).

Select a stock, a direction, a horizon and the kind of trade, and the study gathers, in one pass, what 2.x asked
for screen by screen: the trend and the market's regime; the stock's signals and any reversal forming; analogs of
today's situation in its own history and its peers'; the options market (implied volatility, the expected move,
the move priced for earnings); the unified forecast with its calibration; and every strategy compared under the
same scenarios. It is saved with an as-of time, the engines' versions and the inputs' fingerprint, so a trade made
from it can later be judged on what was known then. What could not be read is listed, never filled in.

The AI is optional and has one job here: to challenge the thesis from the evidence (what supports it, what
contradicts it, what is missing, what would prove it wrong). It writes no numbers and draws no path.

**Interrupted studies (3.1).** A study runs in a thread of the process that started it, which records its name
(host and process id) on the row and beats every few seconds while it runs. A study left `running` by a process
that is gone (a restart, a crash) or that stopped beating long ago is marked failed as interrupted, when this
process starts and whenever studies are read; another live process's study on this computer is left to that
process. A failed study can be run again from its saved inputs (POST /v3/studies/{id}/retry), as a new study that
names the one it retries.
"""

from __future__ import annotations

import json
import logging
import math
import os
import socket
import threading
import time
import traceback
from datetime import datetime, timedelta, timezone
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
from ..providers.base import DataError, QuotaExceeded
from ..service import RequestError

log = logging.getLogger(__name__)

ENGINE_VERSION = "study-3.0"
HORIZONS = (1, 3, 5, 10, 20)
THESES = ("intraday_bounce", "swing", "earnings_reaction", "reversal", "investment")
CAL_KEY = "forecast:calibration:{mode}:{h}"
CAL_JOB_STATE = "forecast:calibration-job:{mode}:{h}"     # 4.2: the latest calibration job's state, for the screens
CAL_JOB = "research.calibrate"
WARM_HORIZONS = (1, 5)                                      # the horizons Catalyst Scenarios and the one-click study use
WARM_AFTER_DAYS = 7                                         # a calibration older than this is refreshed in the background
WARM_DELAY = 180.0                                          # seconds after start before a warm-up calibration may run
REAL_MODES = ("live", "sandbox", "robinhood")
MIN_HISTORY_BARS = 300                                      # a stock with fewer daily bars cannot be walked forward
MAX_RUNNING = 3
BEAT_SECONDS = 5.0             # a running study's heartbeat
ORPHAN_SECONDS = 180.0         # a study whose heartbeat is older than this, in a process that is not checkable, is orphaned
INTERRUPTED = ("Interrupted: the app stopped (or the computer slept) while this study was running, so it did not finish. "
               "Run it again.")


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:          # it exists, under another user
        return True
    except OSError:
        return False
    return True


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


class TradierCheckIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    symbol: str = Field(default="SPY", min_length=1, max_length=12, pattern=r"^[A-Za-z.\-]+$")


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
        self.host = socket.gethostname()
        self.worker = f"{self.host}:{os.getpid()}"
        self._beat: threading.Thread | None = None
        try:
            self.sweep()                         # what a stopped process left running is failed now, not left spinning
        except Exception:  # noqa: BLE001 - an old database without the columns is migrated before use; never block start
            pass

    # --- interrupted studies ---------------------------------------------------------
    def _owner_alive(self, worker: str | None, heartbeat_at: str | None, created_at: str | None) -> bool:
        """Whether the process named on a running study may still be running it."""
        if worker == self.worker:
            return False                                         # ours, and not among our live threads (checked by the caller)
        host, _, pid = (worker or "").rpartition(":")
        if host == self.host and pid.isdigit():
            return _pid_alive(int(pid))                          # another process on this computer: alive is alive
        stamp = heartbeat_at or created_at
        if not stamp:
            return False
        try:
            age = (pd.Timestamp.now(tz="UTC") - pd.Timestamp(stamp)).total_seconds()
        except (ValueError, TypeError):
            return False
        return age < ORPHAN_SECONDS

    def sweep(self) -> list[str]:
        """Mark as failed (interrupted) every study left running by a process that is not running it any more."""
        rows = self.svc.db.query("SELECT id, symbol, worker, heartbeat_at, created_at FROM studies WHERE status = 'running'")
        if not rows:
            return []
        with self._lock:
            mine = {k for k, t in self._running.items() if t.is_alive()}
        dead = [r for r in rows if r["id"] not in mine and not self._owner_alive(r["worker"], r["heartbeat_at"], r["created_at"])]
        if not dead:
            return []
        with self.svc.db.tx() as cur:
            for r in dead:
                cur.execute("UPDATE studies SET status = 'failed', error = ? WHERE id = ? AND status = 'running'", (INTERRUPTED, r["id"]))
        for r in dead:
            self.svc.bus.publish("study.failed", r["id"], symbol=r["symbol"], error="interrupted")
        return [r["id"] for r in dead]

    def _beating(self) -> None:
        """One thread per process: beats for every study this process is running, and stops when there are none."""
        while True:
            with self._lock:
                live = [k for k, t in self._running.items() if t.is_alive()]
            if not live:
                with self._lock:
                    self._beat = None
                return
            try:
                with self.svc.db.tx() as cur:
                    cur.executemany("UPDATE studies SET heartbeat_at = ? WHERE id = ? AND status = 'running'", [(C.utc_now(), k) for k in live])
            except Exception:  # noqa: BLE001 - a missed beat is caught up on the next
                pass
            threading.Event().wait(BEAT_SECONDS)

    def retry(self, study_id: str) -> dict[str, Any]:
        """Run a failed study again from its saved inputs, as a new study that names the one it retries."""
        self.sweep()
        r = self.svc.db.one("SELECT status, inputs_json FROM studies WHERE id = ?", (study_id,))
        if r is None:
            raise RequestError(f"No study {study_id}")
        if r["status"] == "running":
            raise RequestError("That study is still running")
        req = StudyIn.model_validate_json(r["inputs_json"])
        out = self.start(req, retry_of=study_id)
        out["retry_of"] = study_id
        return out

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

    def start(self, req: StudyIn, retry_of: str | None = None) -> dict[str, Any]:
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
                               engine_version, contract_version, opportunity_id, inputs_json, evidence_json, fingerprint, worker, heartbeat_at,
                               retry_of) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'running', ?, ?, ?, ?, '{}', '', ?, ?, ?)""",
                            (study_id, self.svc.market.info.mode, req.symbol, now, now, req.horizon_days, req.thesis, req.direction,
                             req.objective, ENGINE_VERSION, C.CONTRACT_VERSION, req.opportunity_id, req.model_dump_json(),
                             self.worker, now, retry_of))
            t = threading.Thread(target=self._run_safely, args=(study_id, req), name=f"study-{req.symbol}", daemon=True)
            self._running[study_id] = t
            t.start()
            if self._beat is None or not self._beat.is_alive():
                self._beat = threading.Thread(target=self._beating, name="study-heartbeat", daemon=True)
                self._beat.start()
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
        cal = self.usable_calibration(h)
        fc = F.forecast(target, pool, h, calibration=cal, implied_sigma=implied, event=event, beta=beta, market_sigma=market_sigma,
                        last_price=last)
        # 4.2: directional evidence and range validation, said separately, with the sample behind them. The study keeps
        # what was true when it ran; a calibration finished later is used by the next study.
        fc["readiness"] = F.readiness(cal, h, self._job_view(h))
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
            missing.append(f"A validated forecast: {fc['readiness']['label']}. " + fc["calibration"]["text"])
        evidence = C.EvidenceSnapshot(as_of=as_of, items=[C.EvidenceItem(**it) for it in items], missing=missing,
                                      provenance=C.Provenance(data_source=svc.market.info.label, as_of=as_of,
                                                              engines={"study": ENGINE_VERSION, "forecast": F.MODEL_VERSION,
                                                                       "analogs": A.ENGINE_VERSION, "contracts": C.CONTRACT_VERSION},
                                                              inputs_hash=C.digest({"req": req.model_dump(), "last_bar": str(bars["date"].iloc[-1]),
                                                                                    "last": last, "peers": [p["symbol"] for p in pr["peers"]]})))
        forecast_contract = C.ForecastVersion(
            model_version=fc["model_version"], horizon_days=h, base=C.ScenarioPath(**fc["base"]),
            alternatives=[C.ScenarioPath(**a) for a in fc["alternatives"]], components=fc["components"], weights=fc["weights"],
            calibration=C.Calibration(**fc["calibration"]), explanation=fc["explanation"], invalidators=fc["invalidators"],
            readiness=fc["readiness"])
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
        view = {"last": last, "future_times": self._future_times(bars, h), "last_bar_date": str(pd.Timestamp(bars["date"].iloc[-1]).date()),
                "peers": pr, "analogs": found, "strategies": strategies,
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
        if r["status"] == "running" and self.sweep():
            r = self.svc.db.one("SELECT * FROM studies WHERE id = ?", (study_id,))
        out: dict[str, Any] = {"id": r["id"], "symbol": r["symbol"], "status": r["status"], "created_at": r["created_at"],
                               "horizon_days": r["horizon_days"], "thesis": r["thesis"], "direction": r["direction"], "error": r["error"],
                               "retry_of": r["retry_of"] if "retry_of" in r.keys() else None,
                               "retryable": r["status"] == "failed"}
        if r["status"] in ("complete", "partial"):
            out["study"] = {"id": r["id"], "symbol": r["symbol"], "created_at": r["created_at"], "horizon_days": r["horizon_days"],
                            "thesis": r["thesis"], "direction": r["direction"], "status": r["status"],
                            "evidence": json.loads(r["evidence_json"]), "forecast": json.loads(r["forecast_json"]) if r["forecast_json"] else None,
                            "strategies": json.loads(r["strategies_json"] or "[]"), "fingerprint": r["fingerprint"]}
            out["view"] = json.loads(r["view_json"] or "{}")
            # 4.1: the study's fixed coordinates (its anchor price and session dates), so the chart draws it where it was
            # made instead of from the chart's newest close.
            from .scenarios import study_frame
            try:
                out["frame"] = study_frame(r["created_at"], out["view"])
            except (ValueError, TypeError):
                out["frame"] = None
            # Where each strategy can go under the policy and broker in force now (not when the study ran).
            from .drafts import routes_for
            try:
                out["routes"] = routes_for(self.svc, (out["view"].get("strategies") or {}).get("candidates") or [])
            except Exception:  # noqa: BLE001 - a route is advice for the screen; the study is still readable without it
                out["routes"] = {}
        return out

    def recent(self, symbol: str | None = None, limit: int = 20) -> list[dict[str, Any]]:
        self.sweep()
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

    # --- calibration (4.2: a durable background job per mode and horizon) -----------------------------------------
    def usable_calibration(self, horizon: int, mode: str | None = None) -> dict[str, Any] | None:
        """This mode's calibration for the horizon, if it was made for the current forecast model."""
        cal = self.svc.db.cache_get(CAL_KEY.format(mode=mode or self.svc.market.info.mode, h=horizon))
        if not cal or cal.get("horizon_days") != horizon or cal.get("model_version", F.MODEL_VERSION) != F.MODEL_VERSION:
            return None
        return cal

    def _set_job(self, horizon: int, mode: str, **values: Any) -> dict[str, Any]:
        key = CAL_JOB_STATE.format(mode=mode, h=horizon)
        state = {**(self.svc.db.cache_get(key) or {}), **values, "horizon_days": horizon, "mode": mode, "updated_at": C.utc_now()}
        self.svc.db.cache_put(key, state)
        return state

    def _job_view(self, horizon: int, mode: str | None = None) -> dict[str, Any] | None:
        """The latest calibration job for the horizon, reconciled with the queue: a job whose worker stopped is shown as
        interrupted (it is taken up again when its lease runs out) rather than running forever."""
        mode = mode or self.svc.market.info.mode
        state = self.svc.db.cache_get(CAL_JOB_STATE.format(mode=mode, h=horizon))
        if not state:
            return None
        jid = state.get("job_id")
        row = self.svc.db.one("SELECT status, error, attempts, due_at FROM jobs WHERE id = ?", (jid,)) if jid else None
        out = dict(state)
        if row is not None:
            if row["status"] == "queued" and state.get("state") == "running":
                out.update(state="queued", note="Interrupted (the app stopped while it ran); it runs again shortly.")
            elif row["status"] == "queued" and state.get("state") == "deferred":
                out["until"] = row["due_at"]
            elif row["status"] == "failed" and state.get("state") in ("queued", "running"):
                out.update(state="failed", error=row["error"] or "the job failed")
        return out

    def calibration(self, horizon: int) -> dict[str, Any]:
        cal = self.svc.db.cache_get(CAL_KEY.format(mode=self.svc.market.info.mode, h=horizon))
        job = self._job_view(horizon)
        return {"horizon_days": horizon, "calibration": cal, "contract": F.calibration_contract(cal, horizon),
                "readiness": F.readiness(self.usable_calibration(horizon), horizon, job), "job": job}

    def calibrate_now(self, horizon: int, symbols: list[str] | None = None, years: float = 4.0, progress=None) -> dict[str, Any]:
        """Walk the forecast forward on history and fit it (forecast.fit). Every stock that cannot be used is listed with
        why; a used-up data quota stops the whole run (it is deferred, not half done). A result that cannot replace a
        usable calibration (too little history this time) is kept beside it as the last attempt, never over it."""
        svc = self.svc
        mode = svc.market.info.mode
        requested = [svc._symbol(s) for s in (symbols or list(svc.settings.watchlist)[:20])]
        failed: list[dict[str, str]] = []
        try:
            spy = svc.market.bars("SPY", years, complete=True)
        except QuotaExceeded:
            raise
        except DataError as exc:
            spy = None
            failed.append({"symbol": "SPY", "reason": f"the market's history: {str(exc)[:160]}"})
        prepared: dict[str, A.Series] = {}
        cutoff = None
        for n, s in enumerate(dict.fromkeys(requested), 1):
            if progress:
                progress(n - 1, len(requested), s)
            try:
                bars = svc.market.bars(s, years, complete=True)
            except QuotaExceeded:
                raise
            except DataError as exc:
                failed.append({"symbol": s, "reason": str(exc)[:200]})
                continue
            if bars is None or len(bars) < MIN_HISTORY_BARS:
                failed.append({"symbol": s, "reason": f"only {0 if bars is None else len(bars)} daily bars (at least {MIN_HISTORY_BARS} are needed)"})
                continue
            last = str(pd.Timestamp(bars["date"].iloc[-1]).date())
            cutoff = max(cutoff or last, last)
            prepared[s] = A.prepare(s, bars, spy, own=True)
        from ..sectors import TICKER_FUND
        peers = {s: [p for p in prepared if p != s and TICKER_FUND.get(p) and TICKER_FUND.get(p) == TICKER_FUND.get(s)][:3] for s in prepared}
        rows = F.walk_forward(prepared, peers, horizon, progress=progress) if prepared else pd.DataFrame()
        cal = F.fit(rows, horizon) if len(rows) else {**F.fit(pd.DataFrame({"date": [], "realized": [], "sigma": [], "symbol": []}), horizon),
                                                       "reason": "no_history", "text": "No stock's history could be used: see the failed list."}
        cal.update({"symbols": list(prepared), "requested": requested, "failed": failed, "years": years, "data_source": svc.market.info.label,
                    "mode": mode, "cutoff": cutoff, "run_at": C.utc_now(), "rows": int(len(rows))})
        key = CAL_KEY.format(mode=mode, h=horizon)
        prev = svc.db.cache_get(key)
        keep_prev = (cal["status"] == "insufficient_data" and prev and prev.get("model_version", F.MODEL_VERSION) == F.MODEL_VERSION
                     and prev.get("status") != "insufficient_data")
        if keep_prev:
            prev["last_attempt"] = {k: cal.get(k) for k in ("status", "reason", "text", "run_at", "rows", "failed", "symbols")}
            svc.db.cache_put(key, prev)
            cal = {**cal, "kept_previous": True}
        else:
            svc.db.cache_put(key, cal)
        svc.bus.publish("study.calibrated", None, horizon_days=horizon, status=cal["status"])
        return cal

    def _active(self, horizon: int, mode: str) -> bool:
        st = self._job_view(horizon, mode)
        if not st or st.get("state") not in ("queued", "running", "deferred"):
            return False
        row = self.svc.db.one("SELECT status FROM jobs WHERE id = ?", (st.get("job_id"),)) if st.get("job_id") else None
        return bool(row and row["status"] in ("queued", "running"))

    def queue_calibration(self, horizon: int, *, symbols: list[str] | None = None, years: float = 4.0, reason: str = "requested",
                          dedupe: str | None = None, delay_seconds: float = 0.0) -> dict[str, Any] | None:
        from .workers import workers_for
        mode = self.svc.market.info.mode
        if self._active(horizon, mode):
            return None
        key = dedupe or f"{CAL_JOB}:{mode}:{horizon}:{reason}:{C.new_id('cal')}"
        jid = workers_for(self.svc).enqueue("calibration", CAL_JOB, {"horizon_days": horizon, "symbols": symbols, "years": years, "mode": mode,
                                                                    "reason": reason}, dedupe_key=key, max_attempts=2,
                                                 due_at=(datetime.now(timezone.utc) + timedelta(seconds=delay_seconds)) if delay_seconds else None)
        if jid is None:
            return None
        return self._set_job(horizon, mode, state="queued", job_id=jid, reason=reason, queued_at=C.utc_now(), started_at=None,
                             finished_at=None, error=None, done=0, total=0, current=None)

    def calibrate(self, req: CalibrateIn) -> dict[str, Any]:
        if req.horizon_days not in HORIZONS:
            raise RequestError(f"horizon_days must be one of {', '.join(map(str, HORIZONS))}")
        state = self.queue_calibration(req.horizon_days, symbols=req.symbols, years=req.years)
        if state is None:
            raise RequestError("A calibration for this horizon is already queued or running")
        return self._job_view(req.horizon_days) or state

    def run_calibration(self, payload: dict[str, Any]) -> dict[str, Any]:
        """The `research.calibrate` job (the calibration worker runs it)."""
        from ..jobs import Deferred
        from .workers import workers_for
        h, mode = int(payload["horizon_days"]), str(payload.get("mode") or self.svc.market.info.mode)
        if mode != self.svc.market.info.mode:
            self._set_job(h, mode, state="skipped", finished_at=C.utc_now(), error="the data source changed before it ran")
            return {"skipped": "the data source changed"}
        worker = workers_for(self.svc).workers.get("calibration")
        self._set_job(h, mode, state="running", started_at=C.utc_now(), error=None, until=None)
        last = [0.0]

        def progress(done: int, total: int, current: str) -> None:
            if time.time() - last[0] < 1.0:
                return
            last[0] = time.time()
            self._set_job(h, mode, state="running", done=done, total=total, current=current)
            if worker is not None:
                worker.touch()
        try:
            cal = self.calibrate_now(h, payload.get("symbols"), float(payload.get("years") or 4.0), progress)
        except QuotaExceeded as exc:
            until = datetime.fromtimestamp(exc.retry_at or time.time() + 60, timezone.utc)
            self._set_job(h, mode, state="deferred", until=until.isoformat(timespec="seconds"), error=str(exc)[:300])
            raise Deferred(until, "the data source's request quota is used up") from exc
        except Exception as exc:  # noqa: BLE001 - recorded on the job, shown with a retry
            from ..events import redact
            self._set_job(h, mode, state="failed", finished_at=C.utc_now(), error=redact(f"{exc.__class__.__name__}: {exc}")[:300])
            log.exception("Calibration of %s sessions failed", h)
            return {"failed": str(exc)[:200]}
        self._set_job(h, mode, state="done", finished_at=C.utc_now(), status=cal["status"], kept_previous=bool(cal.get("kept_previous")))
        return {"status": cal["status"], "rows": cal.get("rows"), "kept_previous": bool(cal.get("kept_previous"))}

    def warm(self, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        """Queue the 1- and 5-session calibrations that are missing, out of date (older than a week) or made with an
        older method, once real market data is connected (the `research.warm` job, every six hours, and at start).
        Once per day per horizon at most; never per study or per stock. At start they wait WARM_DELAY seconds, so
        the app's first pages are not slowed by years of history being read."""
        delay = float((payload or {}).get("delay_seconds") or 0.0)
        mode = self.svc.market.info.mode
        if mode not in REAL_MODES:
            return {"skipped": f"{mode} data: calibrations are warmed only on real market data"}
        queued = []
        today = datetime.now(NY).date().isoformat()
        for h in WARM_HORIZONS:
            cal = self.svc.db.cache_get(CAL_KEY.format(mode=mode, h=h))
            due = not cal or cal.get("model_version", F.MODEL_VERSION) != F.MODEL_VERSION or (cal.get("method") or "cal-3.0") != F.CALIBRATION_METHOD
            if cal and not due:
                try:
                    age = (pd.Timestamp.now(tz="UTC") - pd.Timestamp(cal.get("run_at"))).total_seconds() / 86400
                except (ValueError, TypeError):
                    age = WARM_AFTER_DAYS + 1
                due = age > WARM_AFTER_DAYS
            if due and self.queue_calibration(h, reason="warm", dedupe=f"{CAL_JOB}:{mode}:{h}:warm:{today}",
                                                  delay_seconds=delay) is not None:
                queued.append(h)
        return {"queued": queued, "mode": mode}


def install(svc: Any) -> None:
    from ..api_v3 import register
    from ..jobs import Schedule
    from .workers import workers_for
    svc.research3 = Research(svc)
    rw = workers_for(svc)
    rw.add("calibration", CAL_JOB, svc.research3.run_calibration)
    rw.add("calibration", "research.warm", svc.research3.warm)
    rw.schedule("calibration", Schedule("research.warm", 6 * 3600, 3600))
    try:
        if svc.market.info.mode in REAL_MODES:
            svc.research3.warm({"delay_seconds": WARM_DELAY})   # real data connected: the 1- and 5-session calibrations are queued if due
        rw.resume()
    except Exception:  # noqa: BLE001 - never block start
        log.exception("Research could not queue its background work")

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

        @router.post("/studies/{study_id}/retry")
        def retry(study_id: str) -> dict[str, Any]:
            return r.retry(study_id)

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

        @router.post("/data/tradier-check")
        def tradier_check(body: TradierCheckIn) -> dict[str, Any]:
            from ..tradier_check import check_tradier
            return check_tradier(svc, body.symbol)

        @router.get("/options/history")
        def option_history(option_symbol: str, days: int = Query(90, ge=5, le=730)) -> dict[str, Any]:
            try:
                return premium_history(svc, option_symbol, days)
            except ValueError as exc:
                raise RequestError(str(exc)) from exc

    register(svc, routes)


__all__ = ["Research", "StudyIn", "install", "HORIZONS", "THESES", "ENGINE_VERSION"]
