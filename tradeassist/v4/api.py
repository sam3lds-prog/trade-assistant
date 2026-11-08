"""The 4.0 routes, under /v4: datasets, replay runs, scorecards, model lineage and predictions (read-only in phase 1).

Runs are started from the command line (`python run.py --replay run`), not from here: a ten-year replay is minutes to
hours of computation and belongs in its own process, never in the web app's.
"""

from __future__ import annotations

import json
from typing import Any

from fastapi import APIRouter, HTTPException, Query

from . import replay_store


def build_router_v4(svc: Any) -> APIRouter:
    router = APIRouter(prefix="/v4")

    def store():
        return replay_store(svc)

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

    @router.get("/replay/runs")
    def runs(limit: int = Query(50, ge=1, le=500)) -> dict[str, Any]:
        rows = store().query("""SELECT id, kind, dataset_id, parent_run, created_at, status, first_session, last_session, checkpoint,
                                started_at, finished_at, heartbeat_at, ledger_sha256, error, summary_json FROM replay_runs
                                ORDER BY created_at DESC LIMIT ?""", (limit,))
        out = []
        for r in rows:
            d = {k: r[k] for k in r.keys() if k != "summary_json"}
            d["headline"] = json.loads(r["summary_json"]).get("headline") if r["summary_json"] else None
            out.append(d)
        return {"runs": out}

    @router.get("/replay/runs/{run_id}")
    def run(run_id: str) -> dict[str, Any]:
        from ..intelligence.replay.runner import ReplayError, run_status
        try:
            return run_status(store(), run_id)
        except ReplayError as exc:
            raise HTTPException(404, str(exc)) from None

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

    @router.get("/replay/runs/{run_id}/lineage")
    def lineage(run_id: str) -> dict[str, Any]:
        from ..intelligence.learning.registry import lineage as L
        return {"run": run_id, "training": L(store(), run_id)}

    @router.get("/replay/runs/{run_id}/predictions")
    def predictions(run_id: str, symbol: str | None = None, task: str | None = Query(None, pattern="^(daily|trade)$"),
                    horizon: int | None = Query(None, ge=1, le=20), session: str | None = None,
                    limit: int = Query(200, ge=1, le=2000)) -> dict[str, Any]:
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

    @router.get("/research/record")
    def research_record() -> dict[str, Any]:
        """How Research's own saved forecasts have fared once graded (grading.py)."""
        from .grading import research_record as rec
        return {"by_horizon": rec(svc)}

    return router


__all__ = ["build_router_v4"]
