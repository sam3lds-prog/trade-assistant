"""The replay: history walked one session at a time, as the app would have lived through it (4.0).

On each session T of the run, in one database transaction:

1. **Grade.** Every prediction whose window ends at T's close gets its outcome (the daily return, or the trade's
   barrier), read through the as-of view, which refuses anything later than T.
2. **Train** (on the schedule: after the last session of each week by default). Each task's candidates are fitted
   on outcomes that had matured by T, compared on a validation window, and the winner refitted (trainer.py). The
   decision and the new version are recorded.
3. **Predict.** Every eligible stock gets one frozen forecast per horizon from the champion, and every qualifying
   setup a frozen target/stop/timeout forecast and a take/pass decision.
4. **Checkpoint.** T is recorded as done. A run stopped at any point resumes after its checkpoint with the same
   models (registry.run_state) and ends with the same ledger hash as an uninterrupted one.

A development run reads its dataset only up to its last session (`Dataset.through`): the held-out year is not merely
unused, it is not loaded. Its last predictions whose windows reach past that session stay ungraded.

The same feature and prediction code serves the shadow forecasts of live sessions (phase 4); only the data and the
clock differ.
"""

from __future__ import annotations

import json
import math
import os
import socket
import time
import uuid
from datetime import date, datetime, timezone
from typing import Any, Callable

import numpy as np
import pandas as pd

from ... import marketdays
from ..config import ReplayConfig
from ..data import dataset as DS
from ..data.features import FEATURE_VERSION, FEATURES, build_panel, matrix
from ..learning import ledger as L
from ..learning import registry as R
from ..learning.trainer import train_daily, train_trade
from ..models import daily as D
from ..models import target_stop as TS
from ..models.meta_filter import decide
from .clock import AsOf
from .labels import LabelBook

Progress = Callable[[dict[str, Any]], None]


class ReplayError(RuntimeError):
    pass


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def worker_name() -> str:
    return f"{socket.gethostname()}:{os.getpid()}"


def create_run(store: Any, dataset_id: str, cfg: ReplayConfig, *, parent_run: str | None = None,
               allow_failed_checks: bool = False) -> str:
    """Register a run (status queued). Refuses a dataset with failed checks unless told otherwise."""
    cfg.check()
    ds = store.one("SELECT * FROM datasets WHERE id = ?", (dataset_id,))
    if ds is None:
        raise ReplayError(f"No dataset {dataset_id}")
    failed = store.query("SELECT text FROM dataset_checks WHERE dataset_id = ? AND level = 'fail' LIMIT 3", (dataset_id,))
    if failed and not allow_failed_checks:
        raise ReplayError("The dataset failed its checks: " + " ".join(r["text"] for r in failed) +
                          " Fix the data, or run with allow_failed_checks to replay it anyway.")
    if ds["universe"] != cfg.universe:
        raise ReplayError(f"The dataset holds universe {ds['universe']!r}; the configuration asks for {cfg.universe!r}.")
    if cfg.kind == "development" and cfg.start < date.fromisoformat(ds["first_date"]):
        raise ReplayError("The run starts before the dataset does.")
    run_id = "run-" + datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:6]
    with store.tx() as cur:
        cur.execute("""INSERT INTO replay_runs (id, kind, dataset_id, parent_run, created_at, config_json, config_sha256, status,
                       first_session, last_session) VALUES (?,?,?,?,?,?,?, 'queued', ?, ?)""",
                    (run_id, cfg.kind, dataset_id, parent_run, _now(), json.dumps(cfg.to_dict()), cfg.sha256(),
                     cfg.start.isoformat(), cfg.end.isoformat()))
    return run_id


class Replay:
    def __init__(self, store: Any, run_id: str, progress: Progress | None = None):
        self.store, self.run_id, self.progress = store, run_id, progress
        row = store.one("SELECT * FROM replay_runs WHERE id = ?", (run_id,))
        if row is None:
            raise ReplayError(f"No run {run_id}")
        self.row = dict(row)
        self.cfg = ReplayConfig.from_dict(json.loads(row["config_json"]))
        full = DS.load(store, row["dataset_id"])
        self.dataset = full.through(self.cfg.end) if self.cfg.kind == "development" else full
        self.cal = self.dataset.calendar
        self.panel = build_panel(self.dataset)
        self.labels = LabelBook(self.panel, self.cal, self.cfg.horizons, self.cfg.trade)
        self.state: dict[tuple[str, int], tuple[str, str, Any]] = {}
        self.streaks: dict[tuple[str, int], dict[str, int]] = {}

    # ---- set-up and resumption ----------------------------------------------------------------------------------
    def _initial_state(self, cur: Any, first: str) -> None:
        for h in self.cfg.horizons:
            m = D.make("no_change", h, self.cfg.interval, self.cfg.seed)
            self._install(cur, "daily", h, R.version_id("no_change", "daily", h, None), m, first)
        hold = self.cfg.trade.hold_sessions
        t = TS.make("base_rate", hold, self.cfg.seed)
        self._install(cur, "trade", hold, R.version_id("base_rate", "trade", hold, None), t, first)

    def _inherit(self, cur: Any, parent: str, first: str) -> None:
        """A holdout run starts with the models its development run ended with (phase 4)."""
        state = R.load_state(self.store, parent)
        if not state:
            raise ReplayError(f"Run {parent} has no models to start from")
        for (task, h), (vid, _fam, model) in state.items():
            self._install(cur, task, h, vid + "@" + parent, model, first)

    def _install(self, cur: Any, task: str, h: int, vid: str, model: Any, session: str) -> None:
        blob, sha = R.record_version(cur, self.run_id, vid, task, h, model, session)
        R.set_state(cur, self.run_id, task, h, vid, model.family, blob, sha, session)
        self.state[(task, h)] = (vid, model.family, model)

    # ---- the loop ---------------------------------------------------------------------------------------------
    def sessions(self) -> list[date]:
        return self.cal.between(self.cfg.start, self.cfg.end)

    def is_training_day(self, d: date) -> bool:
        if self.cfg.retrain == "weekly":
            return self.cal.is_week_end(d)
        if self.cfg.retrain == "monthly":
            return self.cal.is_month_end(d)
        return False

    def run(self, *, max_sessions: int | None = None, should_stop: Callable[[], bool] | None = None) -> dict[str, Any]:
        sessions = self.sessions()
        if not sessions:
            raise ReplayError("The dataset has no sessions in the run's dates")
        checkpoint = self.row.get("checkpoint")
        todo = [d for d in sessions if checkpoint is None or d.isoformat() > checkpoint]
        with self.store.tx() as cur:
            cur.execute("UPDATE replay_runs SET status = 'running', started_at = COALESCE(started_at, ?), worker = ?, heartbeat_at = ?, "
                        "error = NULL WHERE id = ?", (_now(), worker_name(), _now(), self.run_id))
            if checkpoint is None:
                if self.row.get("parent_run"):
                    self._inherit(cur, self.row["parent_run"], sessions[0].isoformat())
                else:
                    self._initial_state(cur, sessions[0].isoformat())
        if checkpoint is not None:
            self.state = R.load_state(self.store, self.run_id)
            self.streaks = R.load_streaks(self.store, self.run_id)
        started, done = time.monotonic(), 0
        try:
            for d in todo:
                if should_stop and should_stop():
                    return self._pause()
                self.step(d)
                done += 1
                if self.progress:
                    self.progress({"session": d.isoformat(), "done": sessions.index(d) + 1, "total": len(sessions),
                                   "seconds": time.monotonic() - started})
                if max_sessions is not None and done >= max_sessions:
                    return self._pause()
        except Exception as exc:
            with self.store.tx() as cur:
                cur.execute("UPDATE replay_runs SET status = 'failed', error = ?, heartbeat_at = ? WHERE id = ?",
                            (f"{exc.__class__.__name__}: {str(exc)[:500]}", _now(), self.run_id))
            raise
        return self.finish()

    def _pause(self) -> dict[str, Any]:
        with self.store.tx() as cur:
            cur.execute("UPDATE replay_runs SET status = 'paused', heartbeat_at = ? WHERE id = ?", (_now(), self.run_id))
        return {"status": "paused", "checkpoint": self.store.one("SELECT checkpoint FROM replay_runs WHERE id = ?", (self.run_id,))[0]}

    def finish(self) -> dict[str, Any]:
        from .metrics import scorecard
        digest = L.ledger_hash(self.store, self.run_id)
        card = scorecard(self.store, self.run_id)
        with self.store.tx() as cur:
            cur.execute("UPDATE replay_runs SET status = 'complete', finished_at = ?, heartbeat_at = ?, ledger_sha256 = ?, "
                        "summary_json = ? WHERE id = ?", (_now(), _now(), digest, json.dumps(card, default=str), self.run_id))
        return {"status": "complete", "ledger_sha256": digest, "scorecard": card}

    def step(self, d: date) -> None:
        view = AsOf(self.panel, self.labels, self.cal, d)
        sess = d.isoformat()
        training = self.is_training_day(d)
        results: list[Any] = []
        if training:                                   # fitted outside the transaction: it is the slow part
            for h in self.cfg.horizons:
                results.append(train_daily(view, h, self.state[("daily", h)][1], self.cfg, self.streaks.get(("daily", h))))
            hold = self.cfg.trade.hold_sessions
            results.append(train_trade(view, self.state[("trade", hold)][1], self.cfg, self.streaks.get(("trade", hold))))
        with self.store.tx() as cur:
            self._grade(cur, view)
            for res in results:
                key = (res.task, res.horizon)
                before = self.state[key][0]
                after = before
                if res.model is not None:
                    after = R.version_id(res.family, res.task, res.horizon, d)
                    self._install(cur, res.task, res.horizon, after, res.model, sess)
                R.record_training(cur, self.run_id, sess, res, before, after)
                self.streaks[key] = res.streaks
            self._predict(cur, view)
            cur.execute("UPDATE replay_runs SET checkpoint = ?, heartbeat_at = ? WHERE id = ?", (sess, _now(), self.run_id))

    # ---- grading ----------------------------------------------------------------------------------------------
    def _grade(self, cur: Any, view: AsOf) -> None:
        sess = view.session.isoformat()
        for p in L.due(cur, self.run_id, sess):
            if p["task"] == "daily":
                close = view.close(p["symbol"], view.session)
                if close is None:
                    L.insert_outcome(cur, self.run_id, p["id"], sess, status="no_data")
                    continue
                y = close / p["last_close"] - 1.0
                mu, s = p["pred_return"], p["sigma"] or 0.0
                lean = mu is not None and abs(mu) > 0.25 * s
                L.insert_outcome(cur, self.run_id, p["id"], sess, status="graded", realized_return=y,
                                 abs_error=abs(y - (mu or 0.0)),
                                 direction_hit=(int(np.sign(mu) == np.sign(y)) if lean else None),
                                 covered=int(p["low_return"] <= y <= p["high_return"]) if p["low_return"] is not None else None)
            else:
                o = self.labels.trade_outcome(pd.Timestamp(p["session"]), p["symbol"])
                if o.get("barrier") is None or pd.Timestamp(o["exit_session"]) > view.ts:
                    L.insert_outcome(cur, self.run_id, p["id"], sess, status="no_data")
                    continue
                L.insert_outcome(cur, self.run_id, p["id"], sess, status="graded", barrier=o["barrier"],
                                 exit_session=pd.Timestamp(o["exit_session"]).date().isoformat(), gross_return=o["gross"],
                                 net_return=o["net"], realized_return=o["net"],
                                 payload={"entry": o["entry"], "target": o["target"], "stop": o["stop"], "exit": o["exit_price"]})

    # ---- predicting -------------------------------------------------------------------------------------------
    def _target(self, d: date, h: int) -> str:
        t = self.cal.add(d, h)
        if t is None:
            t = marketdays.add_trading_days(d, h)       # past the dataset's end: the exchange calendar (or weekdays)
        return t.isoformat()

    def _predict(self, cur: Any, view: AsOf) -> None:
        d = view.session
        sess, cutoff, regime = d.isoformat(), self.cal.cutoff_at(d), view.regime()
        rows = view.rows()
        out: list[dict[str, Any]] = []
        if len(rows):
            x = matrix(rows)
            fh = [L.feature_hash(r) for r in x]
            closes = rows["close"].to_numpy(float)
            for h in self.cfg.horizons:
                vid, fam, model = self.state[("daily", h)]
                p = model.predict(rows)
                target = self._target(d, h)
                for i, sym in enumerate(rows.index):
                    out.append({"task": "daily", "horizon": h, "symbol": sym, "session": sess, "target_session": target,
                                "model_version": vid, "family": fam, "last_close": float(closes[i]),
                                "pred_return": float(p["mu"][i]), "low_return": float(p["low"][i]), "high_return": float(p["high"][i]),
                                "p_up": float(p["p_up"][i]), "sigma": float(p["sigma"][i]), "regime": regime, "fhash": fh[i],
                                "payload": {"features": FEATURE_VERSION, "trained_through": getattr(model, "trained_through", None)}})
        L.insert_predictions(cur, self.run_id, cutoff, out)
        setups = view.setups()
        if not len(setups):
            return
        hold = self.cfg.trade.hold_sessions
        vid, fam, model = self.state[("trade", hold)]
        p = model.predict(setups)
        target = self._target(d, hold)
        x = matrix(setups)
        trades = []
        for i, sym in enumerate(setups.index):
            exp = float(p["expected"][i]) if math.isfinite(p["expected"][i]) else None
            trades.append({"task": "trade", "horizon": hold, "symbol": sym, "session": sess, "target_session": target,
                           "model_version": vid, "family": fam, "last_close": float(setups["close"].iloc[i]),
                           "sigma": float(setups["rvol"].iloc[i]), "setup": setups["setup"].iloc[i],
                           "p_target": _f(p["p_target"][i]), "p_stop": _f(p["p_stop"][i]), "p_timeout": _f(p["p_timeout"][i]),
                           "expected_return": exp, "regime": regime, "fhash": L.feature_hash(x[i]),
                           "payload": {"features": FEATURE_VERSION, "trained_through": getattr(model, "trained_through", None),
                                       "atr_pct": float(setups["atr_pct"].iloc[i]), "barrier": self.cfg.trade.version}})
        ids = L.insert_predictions(cur, self.run_id, cutoff, trades)
        for pid, t in zip(ids, trades):
            take, reason, gate = decide(t["expected_return"], bool(getattr(model, "fitted", False)), self.cfg.trade.min_edge)
            L.insert_decision(cur, self.run_id, pid, sess, t["symbol"], take, reason, gate)


def _f(v: float) -> float | None:
    return float(v) if v is not None and math.isfinite(v) else None


def run_status(store: Any, run_id: str) -> dict[str, Any]:
    r = store.one("SELECT * FROM replay_runs WHERE id = ?", (run_id,))
    if r is None:
        raise ReplayError(f"No run {run_id}")
    out = {k: r[k] for k in r.keys() if k not in ("config_json", "summary_json")}
    out["config"] = json.loads(r["config_json"])
    out["summary"] = json.loads(r["summary_json"]) if r["summary_json"] else None
    counts = store.one("SELECT COUNT(*) AS predictions, SUM(CASE WHEN o.prediction_id IS NOT NULL THEN 1 ELSE 0 END) AS graded "
                       "FROM predictions p LEFT JOIN outcomes o ON o.prediction_id = p.id WHERE p.run_id = ?", (run_id,))
    out["predictions"], out["graded"] = int(counts["predictions"] or 0), int(counts["graded"] or 0)
    return out


__all__ = ["Replay", "ReplayError", "create_run", "run_status", "worker_name", "FEATURES"]
