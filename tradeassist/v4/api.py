"""The 4.0 routes, under /v4: datasets, replay runs, scorecards, model lineage and deployments, and the shadow forecasts.

A replay is started from here but never runs here: the route starts `python run.py --replay ...` as its own process
(runs.py) and returns at once, so the web app and the Trading Desk are never slowed by one. Opening the held-out year
is deliberately not offered here; it is one command, run on purpose (`python run.py --replay holdout RUN`).
"""

from __future__ import annotations

import json
from datetime import date, datetime
from typing import Any

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field

from . import replay_store


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)


class RunIn(Strict):
    dataset: str | None = Field(default=None, max_length=40)
    start: date | None = None
    end: date | None = None
    families: list[str] | None = Field(default=None, max_length=8)
    retrain: str | None = Field(default=None, pattern="^(weekly|monthly|never)$")


class DeployIn(Strict):
    run: str = Field(min_length=1, max_length=80)
    label: str = Field(default="", max_length=80)


class SwitchIn(Strict):
    on: bool


def build_router_v4(svc: Any) -> APIRouter:
    router = APIRouter(prefix="/v4")

    def store():
        return replay_store(svc)

    def bad(exc: Exception) -> HTTPException:
        return HTTPException(400, str(exc))

    # ---- datasets ---------------------------------------------------------------------------------------------
    @router.get("/replay/datasets")
    def datasets() -> dict[str, Any]:
        from ..intelligence.data.dataset import summaries
        return {"datasets": summaries(store())}

    @router.get("/replay/datasets/{dataset_id}/checks")
    def dataset_checks(dataset_id: str) -> dict[str, Any]:
        rows = store().query("SELECT symbol, level, code, text FROM dataset_checks WHERE dataset_id = ? ORDER BY id", (dataset_id,))
        if not rows and store().one("SELECT 1 FROM datasets WHERE id = ?", (dataset_id,)) is None:
            raise HTTPException(404, f"No dataset {dataset_id}")
        return {"dataset": dataset_id, "checks": [dict(r) for r in rows]}

    @router.post("/replay/datasets/build")
    def dataset_build() -> dict[str, Any]:
        from .runs import build_dataset
        return build_dataset(svc)

    # ---- runs -------------------------------------------------------------------------------------------------
    @router.get("/replay/runs")
    def runs(limit: int = Query(50, ge=1, le=500)) -> dict[str, Any]:
        from .runs import interrupted
        rows = store().query("""SELECT id, kind, dataset_id, parent_run, created_at, status, first_session, last_session, checkpoint,
                                started_at, finished_at, heartbeat_at, ledger_sha256, error, summary_json FROM replay_runs
                                WHERE kind != 'shadow' ORDER BY created_at DESC LIMIT ?""", (limit,))
        out = []
        for r in rows:
            d = {k: r[k] for k in r.keys() if k != "summary_json"}
            d["headline"] = json.loads(r["summary_json"]).get("headline") if r["summary_json"] else None
            d["interrupted"] = r["status"] == "running" and interrupted(r["heartbeat_at"])
            out.append(d)
        return {"runs": out}

    @router.post("/replay/runs")
    def new_run(body: RunIn) -> dict[str, Any]:
        """Register a development replay and start it in its own process."""
        from ..intelligence.config import ReplayConfig
        from ..intelligence.replay.runner import ReplayError, create_run
        from .runs import start
        ds = body.dataset
        if not ds:
            row = store().one("SELECT id FROM datasets ORDER BY created_at DESC LIMIT 1")
            if row is None:
                raise HTTPException(400, "No dataset yet: build one first.")
            ds = row["id"]
        uni = store().one("SELECT universe FROM datasets WHERE id = ?", (ds,))
        if uni is None:
            raise HTTPException(404, f"No dataset {ds}")
        from ..intelligence.data.dataset import development_start
        changes: dict[str, Any] = {"universe": uni["universe"], "start": development_start(store(), ds)}
        for k in ("start", "end", "retrain"):
            if getattr(body, k) is not None:
                changes[k] = getattr(body, k)
        if body.families:
            changes["families"] = tuple(body.families)
        try:
            cfg = ReplayConfig().with_(**changes).check()
            run_id = create_run(store(), ds, cfg)
        except (ReplayError, ValueError) as exc:
            raise bad(exc) from None
        return start(svc, run_id)

    @router.post("/replay/runs/{run_id}/start")
    def resume_run(run_id: str) -> dict[str, Any]:
        from .runs import start
        try:
            return start(svc, run_id)
        except ValueError as exc:
            raise bad(exc) from None

    @router.post("/replay/runs/{run_id}/stop")
    def stop_run(run_id: str) -> dict[str, Any]:
        from .runs import stop
        try:
            return stop(svc, run_id)
        except ValueError as exc:
            raise HTTPException(404, str(exc)) from None

    @router.get("/replay/runs/{run_id}")
    def run(run_id: str) -> dict[str, Any]:
        from ..intelligence.replay.runner import ReplayError, run_status
        from .runs import interrupted, tail
        try:
            out = run_status(store(), run_id)
        except ReplayError as exc:
            raise HTTPException(404, str(exc)) from None
        ctl = store().one("SELECT stop_requested_at, log_path FROM run_controls WHERE run_id = ?", (run_id,))
        out["stop_requested_at"] = ctl["stop_requested_at"] if ctl else None
        out["log"] = tail(ctl["log_path"]) if ctl else []
        out["interrupted"] = out["status"] == "running" and interrupted(out.get("heartbeat_at"))
        return out

    @router.get("/replay/runs/{run_id}/scorecard")
    def scorecard(run_id: str) -> dict[str, Any]:
        """The finished run's scorecard, or the one so far for a run still going."""
        from ..intelligence.replay.metrics import scorecard as card
        r = store().one("SELECT status, summary_json FROM replay_runs WHERE id = ?", (run_id,))
        if r is None:
            raise HTTPException(404, f"No run {run_id}")
        if r["status"] == "complete" and r["summary_json"]:
            return json.loads(r["summary_json"])
        return card(store(), run_id)

    def known(run_id: str) -> None:
        if store().one("SELECT 1 FROM replay_runs WHERE id = ?", (run_id,)) is None:
            raise HTTPException(404, f"No run {run_id}")

    @router.get("/replay/runs/{run_id}/lineage")
    def lineage(run_id: str) -> dict[str, Any]:
        from ..intelligence.learning.registry import lineage as L
        known(run_id)
        return {"run": run_id, "training": L(store(), run_id)}

    @router.get("/replay/runs/{run_id}/curve")
    def curve(run_id: str, horizon: int = Query(1, ge=1, le=20)) -> dict[str, Any]:
        from ..intelligence.replay.insight import learning_curve
        return {"run": run_id, "horizon": horizon, "months": learning_curve(store(), run_id, horizon)}

    @router.get("/replay/runs/{run_id}/errors")
    def run_errors(run_id: str, horizon: int = Query(1, ge=1, le=20), limit: int = Query(40, ge=1, le=200)) -> dict[str, Any]:
        from ..intelligence.replay.insight import errors
        return {"run": run_id, "horizon": horizon, **errors(store(), run_id, horizon, limit)}

    @router.get("/replay/runs/{run_id}/symbols/{symbol}")
    def run_symbol(run_id: str, symbol: str) -> dict[str, Any]:
        from ..intelligence.replay.insight import symbol_record
        return symbol_record(store(), run_id, symbol.upper())

    @router.get("/replay/runs/{run_id}/predictions")
    def predictions(run_id: str, symbol: str | None = None, task: str | None = Query(None, pattern="^(daily|trade)$"),
                    horizon: int | None = Query(None, ge=1, le=20), session: str | None = None,
                    limit: int = Query(200, ge=1, le=2000)) -> dict[str, Any]:
        known(run_id)
        sql = ["""SELECT p.id, p.task, p.horizon, p.symbol, p.session, p.target_session, p.model_version, p.family, p.last_close,
                  p.pred_return, p.low_return, p.high_return, p.p_up, p.sigma, p.setup, p.p_target, p.p_stop, p.p_timeout,
                  p.expected_return, p.regime, o.status AS outcome, o.realized_return, o.abs_error, o.direction_hit, o.covered,
                  o.barrier, o.net_return, d.take, d.reason
                  FROM predictions p LEFT JOIN outcomes o ON o.prediction_id = p.id LEFT JOIN trade_decisions d ON d.prediction_id = p.id
                  WHERE p.run_id = ?"""]
        args: list[Any] = [run_id]
        for col, val in (("p.symbol", symbol.upper() if symbol else None), ("p.task", task), ("p.horizon", horizon), ("p.session", session)):
            if val is not None:
                sql.append(f"AND {col} = ?")
                args.append(val)
        sql.append("ORDER BY p.session DESC, p.symbol LIMIT ?")
        args.append(limit)
        return {"run": run_id, "predictions": [dict(r) for r in store().query(" ".join(sql), args)]}

    @router.get("/replay/holdout")
    def holdout_register() -> dict[str, Any]:
        from ..intelligence.replay.holdout import openings
        return {"openings": openings(store()), "command": "python run.py --replay holdout RUN"}

    # ---- deployed models --------------------------------------------------------------------------------------
    @router.get("/models/deployments")
    def deployments() -> dict[str, Any]:
        from ..intelligence.learning.deploy import listing
        return {"deployments": listing(store())}

    @router.post("/models/deploy")
    def deploy(body: DeployIn) -> dict[str, Any]:
        from ..intelligence.learning.deploy import DeployError, deploy as D
        try:
            return {"deployment": D(store(), body.run, body.label)}
        except DeployError as exc:
            raise bad(exc) from None

    @router.post("/models/deployments/{dep_id}/activate")
    def activate(dep_id: str) -> dict[str, Any]:
        from ..intelligence.learning.deploy import DeployError, set_active
        try:
            set_active(store(), dep_id)
        except DeployError as exc:
            raise HTTPException(404, str(exc)) from None
        return {"active": dep_id}

    # ---- the models on the paper desk (phase 6) ---------------------------------------------------------------
    @router.get("/paper")
    def paper() -> dict[str, Any]:
        from .paper import paper_for
        mp = paper_for(svc)
        return {**mp.eligibility(), "record": mp.record(), "last_run": mp.last}

    @router.post("/models/deployments/{dep_id}/paper")
    def paper_switch(dep_id: str, body: SwitchIn) -> dict[str, Any]:
        from .paper import paper_for
        return paper_for(svc).set_switch(dep_id, body.on)

    # ---- the shadow forecasts ---------------------------------------------------------------------------------
    def shadow_run() -> tuple[dict[str, Any] | None, str | None]:
        from ..intelligence.learning.deploy import active
        dep = active(store())
        return dep, (dep or {}).get("shadow_run")

    def validation(dep: dict[str, Any]) -> dict[str, Any]:
        """What stands behind the deployed models: the run they came from and, if opened, the held-out year."""
        src = store().one("SELECT id, kind, summary_json FROM replay_runs WHERE id = ?", (dep["source_run"],))
        hold = store().one("""SELECT r.id, r.status, r.summary_json, h.ordinal FROM holdout_openings h JOIN replay_runs r ON r.id = h.run_id
                              WHERE h.parent_run = ? OR h.run_id = ? ORDER BY h.id DESC LIMIT 1""", (dep["source_run"], dep["source_run"]))
        out: dict[str, Any] = {"source_run": dep["source_run"], "status": "replayed"}
        if src is not None and src["summary_json"]:
            out["replay_headline"] = json.loads(src["summary_json"]).get("headline")
        if hold is not None and hold["status"] == "complete":
            out["status"] = "holdout_tested" if hold["ordinal"] == 1 else "holdout_reopened"
            out["holdout_headline"] = json.loads(hold["summary_json"]).get("headline") if hold["summary_json"] else None
        families = {f"{m['task']}:{m['horizon']}": m["family"] for m in dep["manifest"]["models"]}
        out["families"] = families
        out["learned"] = any(f not in ("no_change", "base_rate") for f in families.values())
        return out

    @router.get("/shadow")
    def shadow() -> dict[str, Any]:
        from ..intelligence.replay.metrics import scorecard as card
        dep, run_id = shadow_run()
        if dep is None:
            return {"deployment": None, "text": "Nothing deployed yet. Run a replay, then deploy it from Model Lab or with "
                                                 "'python run.py --replay deploy RUN'."}
        out: dict[str, Any] = {"deployment": {k: dep[k] for k in ("id", "label", "created_at", "source_run")},
                               "validation": validation(dep), "run": run_id}
        if run_id:
            r = store().one("SELECT checkpoint, heartbeat_at FROM replay_runs WHERE id = ?", (run_id,))
            out["last_session"], out["last_run_at"] = r["checkpoint"], r["heartbeat_at"]
            out["record"] = card(store(), run_id)
        return out

    @router.get("/shadow/latest")
    def shadow_latest(symbol: str | None = None) -> dict[str, Any]:
        from ..intelligence.replay.insight import latest, symbol_record
        dep, run_id = shadow_run()
        if not run_id:
            return {"predictions": [], "deployment": dep and dep["id"]}
        out: dict[str, Any] = {"deployment": dep["id"], "validation": validation(dep),
                               "predictions": latest(store(), run_id, symbol.upper() if symbol else None)}
        if symbol:
            out["record"] = symbol_record(store(), run_id, symbol.upper())
            src = dep["source_run"]
            if store().one("SELECT 1 FROM replay_runs WHERE id = ?", (src,)) is not None:
                out["replay_record"] = symbol_record(store(), src, symbol.upper())
        return out

    @router.get("/shadow/opportunities")
    def shadow_opportunities(limit: int = Query(30, ge=1, le=200)) -> dict[str, Any]:
        """Today's frozen forecasts as opportunities: every qualifying setup with its outcome probabilities and decision,
        and the stocks whose next-day forecast leans hardest against their own volatility."""
        from ..intelligence.replay.insight import latest
        dep, run_id = shadow_run()
        if not run_id:
            return {"setups": [], "leaning": [], "deployment": None}
        rows = latest(store(), run_id)
        setups = sorted([r for r in rows if r["task"] == "trade"], key=lambda r: -(r["expected_return"] if r["expected_return"] is not None else -9))
        daily = [r for r in rows if r["task"] == "daily" and r["sigma"]]
        leaning = sorted(daily, key=lambda r: -abs((r["pred_return"] or 0) / r["sigma"]))
        return {"deployment": dep["id"], "validation": validation(dep), "session": rows[0]["session"] if rows else None,
                "setups": setups[:limit], "leaning": [r for r in leaning if r["pred_return"]][:limit]}

    @router.get("/shadow/why")
    def shadow_why(symbol: str, horizon: int = Query(1, ge=1, le=20)) -> dict[str, Any]:
        from ..intelligence.learning.deploy import models
        from ..intelligence.replay.insight import why_changed
        dep, run_id = shadow_run()
        if not run_id:
            raise HTTPException(404, "No shadow forecasts yet")
        model = models(store(), dep["id"]).get(("daily", horizon), (None, None, None))[2]
        return why_changed(store(), run_id, symbol.upper(), horizon, model)

    @router.post("/shadow/run")
    def shadow_now(force: bool = False) -> dict[str, Any]:
        from .shadow import run_shadow
        return run_shadow(svc, force=force)

    @router.get("/shadow/specialists")
    def specialists(run: str | None = None) -> dict[str, Any]:
        """Which stocks the per-stock corrections are used for: in a run's final models, or the active deployment's."""
        from ..intelligence.learning.deploy import models
        from ..intelligence.learning.registry import lineage, load_state
        out: dict[str, Any] = {"tasks": {}, "challengers": {}}
        if run:
            state = load_state(store(), run)
            training = lineage(store(), run)
        else:
            dep, _run = shadow_run()
            if dep is None:
                return {**out, "text": "Nothing deployed yet."}
            state = models(store(), dep["id"])
            run = dep["source_run"]
            training = lineage(store(), run) if store().one("SELECT 1 FROM replay_runs WHERE id = ?", (run,)) else []
        for (task, h), (vid, fam, model) in state.items():
            if task != "daily":
                continue
            entry: dict[str, Any] = {"version": vid, "family": fam, "specialist": fam.endswith("_spec")}
            if entry["specialist"]:
                entry["by_stock"] = getattr(model, "by_stock", {})
            out["tasks"][str(h)] = entry
        for t in training:
            for fam, c in t["candidates"].items():
                if fam.endswith("_spec") and c.get("role") == "challenger":
                    b = out["challengers"].setdefault(f"{fam}:{t['horizon']}", {"tested": 0, "passed": 0})
                    b["tested"] += 1
                    b["passed"] += int(bool(c.get("passes")))
        out["run"] = run
        return out

    @router.get("/events")
    def events(symbol: str, days: int = Query(7, ge=1, le=60), as_of: str | None = Query(None, max_length=40)) -> dict[str, Any]:
        """The stock's structured news events published before `as_of` (a forecast's cutoff), or before now (phase 7)."""
        from .events import for_symbol
        if as_of is not None:
            try:
                datetime.fromisoformat(as_of)
            except ValueError:
                raise HTTPException(422, "as_of is an ISO date and time") from None
        return for_symbol(svc, symbol.upper(), days, as_of)

    # ---- Research's own record ----------------------------------------------------------------------------------
    @router.get("/research/record")
    def research_record() -> dict[str, Any]:
        """How Research's own saved forecasts have fared once graded (grading.py)."""
        from .grading import research_record as rec
        return {"by_horizon": rec(svc)}

    @router.post("/research/grade")
    def research_grade() -> dict[str, Any]:
        """Grade what is due now, rather than waiting for the hourly job."""
        from .grading import grade_research
        return grade_research(svc)

    return router


__all__ = ["build_router_v4"]
