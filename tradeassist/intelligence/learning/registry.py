"""Which model each task uses on each session, and every version there has been (4.0, phase 2).

A replay run has its own lineage. Each task (the daily forecast at each horizon, and the trade model) starts with a
baseline champion; every training day records a decision (`training_runs`: promoted, kept or insufficient data) and
a new model version (`model_versions`: the family, the training cutoff, the rows it saw, its parameters, its
artifact's hash). Both tables are immutable.

`run_state` holds the current champion's artifact for each task, so a stopped run resumes with exactly the model it
had. Earlier artifacts are not kept: each version is reproducible from the dataset, the configuration and its cutoff,
and its hash says whether a rebuild matches.
"""

from __future__ import annotations

import hashlib
import json
import pickle
from datetime import date
from typing import Any

from .trainer import TrainingResult


def version_id(family: str, task: str, horizon: int, session: date | str | None) -> str:
    tag = "init" if session is None else str(session).replace("-", "")
    return f"{family}.{task[0]}{horizon}.{tag}"


def artifact(model: Any) -> tuple[bytes, str]:
    blob = pickle.dumps(model, protocol=5)
    return blob, hashlib.sha256(blob).hexdigest()


def load_artifact(blob: bytes, sha: str) -> Any:
    if hashlib.sha256(blob).hexdigest() != sha:
        raise RuntimeError("A model artifact does not match its recorded hash")
    return pickle.loads(blob)  # noqa: S301 - written by this app into its own replay.db, checked against its hash


def record_version(cur: Any, run_id: str, vid: str, task: str, horizon: int, model: Any, session: str) -> tuple[bytes, str]:
    blob, sha = artifact(model)
    params = model.params() if hasattr(model, "params") else {}
    cur.execute("""INSERT INTO model_versions (run_id, id, task, horizon, family, trained_through, train_rows, params_json,
                   artifact_sha256, created_session) VALUES (?,?,?,?,?,?,?,?,?,?)""",
                (run_id, vid, task, horizon, model.family, getattr(model, "trained_through", None),
                 int(getattr(model, "train_rows", 0)), json.dumps(params, default=float), sha, session))
    return blob, sha


def set_state(cur: Any, run_id: str, task: str, horizon: int, vid: str, family: str, blob: bytes, sha: str, session: str) -> None:
    cur.execute("""INSERT INTO run_state (run_id, task, horizon, model_version, family, artifact, artifact_sha256, updated_session)
                   VALUES (?,?,?,?,?,?,?,?) ON CONFLICT(run_id, task, horizon) DO UPDATE SET model_version = excluded.model_version,
                   family = excluded.family, artifact = excluded.artifact, artifact_sha256 = excluded.artifact_sha256,
                   updated_session = excluded.updated_session""", (run_id, task, horizon, vid, family, blob, sha, session))


def load_state(store: Any, run_id: str) -> dict[tuple[str, int], tuple[str, str, Any]]:
    out = {}
    for r in store.query("SELECT * FROM run_state WHERE run_id = ?", (run_id,)):
        out[(r["task"], int(r["horizon"]))] = (r["model_version"], r["family"], load_artifact(r["artifact"], r["artifact_sha256"]))
    return out


def record_training(cur: Any, run_id: str, session: str, result: TrainingResult, before: str, after: str) -> None:
    body = {**result.candidates, "_streaks": result.streaks}
    cur.execute("""INSERT INTO training_runs (run_id, session, task, horizon, champion_before, champion_after, decision, reason,
                   candidates_json) VALUES (?,?,?,?,?,?,?,?,?)""",
                (run_id, session, result.task, result.horizon, before, after, result.decision, result.reason,
                 json.dumps(body, default=_json_default)))


def load_streaks(store: Any, run_id: str) -> dict[tuple[str, int], dict[str, int]]:
    """Each task's challenger pass streaks as of the run's last training day (for a resumed run)."""
    out: dict[tuple[str, int], dict[str, int]] = {}
    for r in store.query("""SELECT t.task, t.horizon, t.candidates_json FROM training_runs t
                            WHERE t.run_id = ? AND t.id = (SELECT MAX(id) FROM training_runs u
                                                           WHERE u.run_id = t.run_id AND u.task = t.task AND u.horizon = t.horizon)""",
                         (run_id,)):
        out[(r["task"], int(r["horizon"]))] = {k: int(v) for k, v in (json.loads(r["candidates_json"]).get("_streaks") or {}).items()}
    return out


def _json_default(v: Any) -> Any:
    try:
        return float(v)
    except (TypeError, ValueError):
        return str(v)


def lineage(store: Any, run_id: str) -> list[dict[str, Any]]:
    return [{**{k: r[k] for k in r.keys() if k != "candidates_json"},
             "candidates": {k: v for k, v in json.loads(r["candidates_json"]).items() if not k.startswith("_")}} for r in store.query(
        "SELECT id, session, task, horizon, champion_before, champion_after, decision, reason, candidates_json FROM training_runs "
        "WHERE run_id = ? ORDER BY session, task, horizon", (run_id,))]


__all__ = ["version_id", "artifact", "load_artifact", "record_version", "set_state", "load_state", "load_streaks", "record_training", "lineage"]
