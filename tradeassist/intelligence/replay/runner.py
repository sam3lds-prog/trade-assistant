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
from datetime import date, datetime, timedelta, timezone
from typing import Any, Callable

import numpy as np
import pandas as pd

from ... import marketdays
from ..config import ReplayConfig
from ..data import dataset as DS
from ..data.features import FACTORS, FEATURE_VERSION, FEATURES, build_panel, matrix
from ..learning import ledger as L
from ..learning import registry as R
from ..learning.trainer import train_daily, train_trade
from ..models import daily as D
from ..models import target_stop as TS
from ..models.meta_filter import decide
from .clock import AsOf
from .labels import LabelBook

Progress = Callable[[dict[str, Any]], None]
LEASE_SECONDS = 120            # a run whose heartbeat is younger than this belongs to the process that wrote it


class ReplayError(RuntimeError):
    pass


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def worker_name() -> str:
    return f"{socket.gethostname()}:{os.getpid()}"


def create_run(store: Any, dataset_id: str, cfg: ReplayConfig, *, parent_run: str | None = None,
               allow_failed_checks: bool = False) -> str:
    """Register a development run (status queued). Refuses a dataset with failed checks unless told otherwise."""
    cfg.check()
    if cfg.kind != "development":
        raise ReplayError("Only development replays are created here: the held-out year is opened once through its own register "
                          "(phase 4), and shadow runs belong to a deployment.")
    return register_run(store, dataset_id, cfg, parent_run=parent_run, allow_failed_checks=allow_failed_checks)


def register_run(store: Any, dataset_id: str, cfg: ReplayConfig, *, parent_run: str | None = None,
                 allow_failed_checks: bool = False, then: Callable[[Any, str], None] | None = None) -> str:
    """The run row itself, for any kind. Only create_run and the held-out year's register call this. `then(cur, run_id)`
    runs in the same transaction, so the register's entry and the run exist together or not at all."""
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
        if then is not None:
            then(cur, run_id)
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
        # Training days are read from the whole calendar: which days the market traded is not price data, and the
        # last session of a cut calendar would otherwise never look like the end of its week or month.
        self.schedule = full.calendar
        self.panel = build_panel(self.dataset)
        self.labels = LabelBook(self.panel, self.cal, self.cfg.horizons, self.cfg.trade)
        self.state: dict[tuple[str, int], tuple[str, str, Any]] = {}

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
            return self.schedule.is_week_end(d)
        if self.cfg.retrain == "monthly":
            return self.schedule.is_month_end(d)
        return False

    def stop_requested(self) -> bool:
        """A stop asked for from the screen (run_controls); checked between sessions."""
        r = self.store.one("SELECT stop_requested_at FROM run_controls WHERE run_id = ?", (self.run_id,))
        return r is not None and r["stop_requested_at"] is not None

    def run(self, *, max_sessions: int | None = None, should_stop: Callable[[], bool] | None = None) -> dict[str, Any]:
        sessions = self.sessions()
        should_stop = should_stop or self.stop_requested
        if not sessions:
            raise ReplayError("The dataset has no sessions in the run's dates")
        self.row = dict(self.store.one("SELECT * FROM replay_runs WHERE id = ?", (self.run_id,)))    # as it stands now
        if self.row["status"] == "complete":
            raise ReplayError("That run is complete")
        me = worker_name()
        if (self.row["status"] == "running" and self.row.get("worker") not in (None, me) and self.row.get("heartbeat_at")
                and (datetime.now(timezone.utc) - datetime.fromisoformat(self.row["heartbeat_at"])).total_seconds() < LEASE_SECONDS):
            raise ReplayError(f"Run {self.run_id} is already going in another process ({self.row['worker']}).")
        checkpoint = self.row.get("checkpoint")
        todo = [d for d in sessions if checkpoint is None or d.isoformat() > checkpoint]
        # A fresh start installs the first models; a resumption (models already installed, even if the run failed
        # during its first session) takes the ones it had.
        fresh = self.store.one("SELECT COUNT(*) FROM run_state WHERE run_id = ?", (self.run_id,))[0] == 0
        stale = (datetime.now(timezone.utc) - timedelta(seconds=LEASE_SECONDS)).isoformat(timespec="seconds")
        with self.store.tx() as cur:
            # The claim is one statement: of two processes resuming the same run at once, only one gets it.
            cur.execute("UPDATE replay_runs SET status = 'running', started_at = COALESCE(started_at, ?), worker = ?, heartbeat_at = ?, "
                        "error = NULL WHERE id = ? AND status != 'complete' AND NOT (status = 'running' AND worker IS NOT NULL "
                        "AND worker != ? AND heartbeat_at IS NOT NULL AND heartbeat_at > ?)", (_now(), me, _now(), self.run_id, me, stale))
            if cur.rowcount == 0:
                raise ReplayError(f"Run {self.run_id} was claimed a moment ago by another process.")
            cur.execute("UPDATE run_controls SET stop_requested_at = NULL WHERE run_id = ?", (self.run_id,))
            if fresh:
                if self.row.get("parent_run"):
                    self._inherit(cur, self.row["parent_run"], sessions[0].isoformat())
                else:
                    self._initial_state(cur, sessions[0].isoformat())
        if not fresh:
            self.state = R.load_state(self.store, self.run_id)
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

    def _beat(self) -> None:
        with self.store.tx() as cur:
            cur.execute("UPDATE replay_runs SET heartbeat_at = ? WHERE id = ?", (_now(), self.run_id))

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
                results.append(train_daily(view, h, self.state[("daily", h)][1], self.cfg))
                self._beat()                           # a long training day must not look like a dead process
            hold = self.cfg.trade.hold_sessions
            results.append(train_trade(view, self.state[("trade", hold)][1], self.cfg))
            self._beat()
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
            self._predict(cur, view)
            cur.execute("UPDATE replay_runs SET checkpoint = ?, heartbeat_at = ? WHERE id = ?", (sess, _now(), self.run_id))

    # ---- grading and predicting: the same functions serve the replay and the live shadow forecasts ---------------
    def _grade(self, cur: Any, view: AsOf) -> None:
        grade(cur, self.run_id, view, self.labels)

    def _predict(self, cur: Any, view: AsOf) -> None:
        predict(cur, self.run_id, view, self.cal, self.cfg, self.state)


GIVE_UP_DAYS = 14     # live grading: a forecast whose bars are still missing this long after its target is closed as no data


def grade(cur: Any, run_id: str, view: AsOf, labels: LabelBook, *, final: bool = True) -> int:
    """Outcomes for every prediction of the run whose window had closed by the view's session. Returns the count.

    In a replay the data are fixed (`final`): a missing bar is missing for good, graded `no_data`. Live (the shadow
    forecasts), a stock whose bars did not come this evening is left due and tried again the next evening, so one
    failed fetch never writes a permanent `no_data`; after GIVE_UP_DAYS it is closed as no data."""
    sess = view.session.isoformat()
    n = 0
    for p in L.due(cur, run_id, sess):
        target = date.fromisoformat(p["target_session"])
        wait = not final and (view.session - target).days <= GIVE_UP_DAYS
        if p["task"] == "daily":
            close = view.close(p["symbol"], target)
            if close is None:
                if not wait:
                    L.insert_outcome(cur, run_id, p["id"], sess, status="no_data")
                    n += 1
                continue
            y = close / p["last_close"] - 1.0
            mu, s = p["pred_return"], p["sigma"] or 0.0
            lean = mu is not None and abs(mu) > 0.25 * s
            L.insert_outcome(cur, run_id, p["id"], sess, status="graded", realized_return=y,
                             abs_error=abs(y - (mu or 0.0)),
                             direction_hit=(int(np.sign(mu) == np.sign(y)) if lean else None),
                             covered=int(p["low_return"] <= y <= p["high_return"]) if p["low_return"] is not None else None)
            n += 1
        else:
            s0 = pd.Timestamp(p["session"])
            try:
                o = labels.trade_outcome(s0, p["symbol"]) if s0 in labels.pos else {"barrier": None}
            except KeyError:                       # the stock is not in this evening's data at all
                o = {"barrier": None}
            if o.get("barrier") is None or pd.Timestamp(o["exit_session"]) > view.ts:
                if not wait:
                    L.insert_outcome(cur, run_id, p["id"], sess, status="no_data")
                    n += 1
                continue
            L.insert_outcome(cur, run_id, p["id"], sess, status="graded", barrier=o["barrier"],
                             exit_session=pd.Timestamp(o["exit_session"]).date().isoformat(), gross_return=o["gross"],
                             net_return=o["net"], realized_return=o["net"],
                             payload={"entry": o["entry"], "target": o["target"], "stop": o["stop"], "exit": o["exit_price"]})
            n += 1
    return n


def target_of(cal: Any, d: date, h: int) -> str:
    t = cal.add(d, h)
    if t is None:
        t = marketdays.add_trading_days(d, h)       # past the data's end: the exchange calendar (or weekdays past 2028)
    return t.isoformat()


def predict(cur: Any, run_id: str, view: AsOf, cal: Any, cfg: ReplayConfig, state: dict[tuple[str, int], tuple[str, str, Any]],
            *, keep_features: bool = False, extra: dict[str, Any] | None = None) -> dict[str, int]:
    """Freeze one forecast per horizon for every eligible stock, and a trade forecast and decision for every setup.
    `keep_features` stores the feature values themselves (the live shadow forecasts do, for "why did it change")."""
    d = view.session
    sess, cutoff, regime = d.isoformat(), cal.cutoff_at(d), view.regime()
    rows = view.rows()
    out: list[dict[str, Any]] = []
    if len(rows):
        x = matrix(rows)
        fh = [L.feature_hash(r) for r in x]
        closes = rows["close"].to_numpy(float)
        for h in cfg.horizons:
            vid, fam, model = state[("daily", h)]
            p = model.predict(rows)
            target = target_of(cal, d, h)
            for i, sym in enumerate(rows.index):
                payload = {"features": FEATURE_VERSION, "trained_through": getattr(model, "trained_through", None), **(extra or {})}
                if keep_features:
                    payload["x"] = _kept(rows, i, x[i])
                out.append({"task": "daily", "horizon": h, "symbol": sym, "session": sess, "target_session": target,
                            "model_version": vid, "family": fam, "last_close": float(closes[i]),
                            "pred_return": float(p["mu"][i]), "low_return": float(p["low"][i]), "high_return": float(p["high"][i]),
                            "p_up": float(p["p_up"][i]), "sigma": float(p["sigma"][i]), "regime": regime, "fhash": fh[i],
                            "payload": payload})
    L.insert_predictions(cur, run_id, cutoff, out)
    setups = view.setups()
    if not len(setups):
        return {"daily": len(out), "trade": 0}
    hold = cfg.trade.hold_sessions
    vid, fam, model = state[("trade", hold)]
    p = model.predict(setups)
    target = target_of(cal, d, hold)
    x = matrix(setups)
    trades = []
    for i, sym in enumerate(setups.index):
        exp = float(p["expected"][i]) if math.isfinite(p["expected"][i]) else None
        payload = {"features": FEATURE_VERSION, "trained_through": getattr(model, "trained_through", None),
                   "atr_pct": float(setups["atr_pct"].iloc[i]), "barrier": cfg.trade.version, **(extra or {})}
        if keep_features:
            payload["x"] = _kept(setups, i, x[i])
        trades.append({"task": "trade", "horizon": hold, "symbol": sym, "session": sess, "target_session": target,
                       "model_version": vid, "family": fam, "last_close": float(setups["close"].iloc[i]),
                       "sigma": float(setups["rvol"].iloc[i]), "setup": setups["setup"].iloc[i],
                       "p_target": _f(p["p_target"][i]), "p_stop": _f(p["p_stop"][i]), "p_timeout": _f(p["p_timeout"][i]),
                       "expected_return": exp, "regime": regime, "fhash": L.feature_hash(x[i]), "payload": payload})
    ids = L.insert_predictions(cur, run_id, cutoff, trades)
    for pid, t in zip(ids, trades):
        take, reason, gate = decide(t["expected_return"], bool(getattr(model, "fitted", False)), cfg.trade.min_edge)
        L.insert_decision(cur, run_id, pid, sess, t["symbol"], take, reason, gate)
    return {"daily": len(out), "trade": len(trades)}


def _kept(rows: pd.DataFrame, i: int, x: np.ndarray) -> dict[str, float | None]:
    """The inputs of row i as they were: the features, and the factors when the panel has them (phase 7)."""
    out = {k: (float(v) if np.isfinite(v) else None) for k, v in zip(FEATURES, x)}
    for k in FACTORS:
        if k in rows.columns:
            v = float(rows[k].iloc[i])
            out[k] = v if math.isfinite(v) else None
    return out


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


__all__ = ["Replay", "ReplayError", "create_run", "run_status", "worker_name", "grade", "predict", "target_of", "FEATURES"]
