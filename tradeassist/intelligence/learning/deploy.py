"""Deployed models: the champions of a finished run, frozen, hashed and portable (4.0, phase 4).

A deployment copies each task's current model out of a finished run (registry.run_state) with its artifact and
hash, under a manifest that says where it came from: the run, its configuration's hash, the dataset, the feature
version and each model's training cutoff. The live shadow forecasts use the active deployment and never retrain:
training happens in a replay (on the faster computer), and the result is deployed.

`export` writes a deployment to one file and `import_file` reads it back on another computer, checking every hash,
so the M1 that serves forecasts runs exactly the models the M5 trained.
"""

from __future__ import annotations

import base64
import hashlib
import json
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .. import ENGINE_VERSION
from ..data.features import FACTOR_VERSION, FEATURE_VERSION
from .registry import load_artifact

FORMAT = "tradeassist-models/1"


class DeployError(RuntimeError):
    pass


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _sha(obj: Any) -> str:
    return hashlib.sha256(json.dumps(obj, sort_keys=True).encode()).hexdigest()


def deploy(store: Any, run_id: str, label: str = "", activate: bool = True) -> str:
    run = store.one("SELECT * FROM replay_runs WHERE id = ?", (run_id,))
    if run is None:
        raise DeployError(f"No run {run_id}")
    if run["kind"] not in ("development", "holdout") or run["status"] != "complete":
        raise DeployError("Only a finished development or holdout run can be deployed.")
    models = store.query("SELECT * FROM run_state WHERE run_id = ? ORDER BY task, horizon", (run_id,))
    if not models:
        raise DeployError(f"Run {run_id} has no models")
    entries = []
    for m in models:
        model = load_artifact(m["artifact"], m["artifact_sha256"])
        entries.append({"task": m["task"], "horizon": int(m["horizon"]), "model_version": m["model_version"], "family": m["family"],
                        "trained_through": getattr(model, "trained_through", None), "artifact_sha256": m["artifact_sha256"]})
    ds = store.one("SELECT spec_json FROM datasets WHERE id = ?", (run["dataset_id"],))
    if ds is None:
        raise DeployError(f"Run {run_id}'s dataset is missing")
    manifest = {"format": FORMAT, "engine": ENGINE_VERSION, "features": FEATURE_VERSION, "factors": FACTOR_VERSION, "source_run": run_id,
                "universe": json.loads(ds["spec_json"])["universe"],
                "run_kind": run["kind"], "dataset": run["dataset_id"], "config": json.loads(run["config_json"]),
                "config_sha256": run["config_sha256"], "ledger_sha256": run["ledger_sha256"], "models": entries}
    blobs = {(m["task"], int(m["horizon"])): bytes(m["artifact"]) for m in models}
    return _insert(store, manifest, blobs, label or f"from {run_id}", activate)


def _insert(store: Any, manifest: dict[str, Any], blobs: dict[tuple[str, int], bytes], label: str, activate: bool) -> str:
    digest = _sha(manifest)
    existing = store.one("SELECT id FROM deployments WHERE manifest_sha256 = ?", (digest,))
    if existing is not None:
        if activate:
            set_active(store, existing["id"])
        return existing["id"]
    dep_id = "dep-" + datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:6]
    with store.tx() as cur:
        cur.execute("INSERT INTO deployments (id, created_at, source_run, label, manifest_json, manifest_sha256) VALUES (?,?,?,?,?,?)",
                    (dep_id, _now(), manifest["source_run"], label, json.dumps(manifest), digest))
        for e in manifest["models"]:
            blob = blobs[(e["task"], int(e["horizon"]))]
            if hashlib.sha256(blob).hexdigest() != e["artifact_sha256"]:
                raise DeployError(f"The {e['task']} model for horizon {e['horizon']} does not match its hash")
            cur.execute("""INSERT INTO deployment_models (deployment_id, task, horizon, model_version, family, trained_through, artifact,
                           artifact_sha256) VALUES (?,?,?,?,?,?,?,?)""",
                        (dep_id, e["task"], int(e["horizon"]), e["model_version"], e["family"], e["trained_through"], blob, e["artifact_sha256"]))
    if activate:
        set_active(store, dep_id)
    return dep_id


def set_active(store: Any, dep_id: str) -> None:
    if store.one("SELECT 1 FROM deployments WHERE id = ?", (dep_id,)) is None:
        raise DeployError(f"No deployment {dep_id}")
    now = _now()
    with store.tx() as cur:
        cur.execute("UPDATE deployments SET active = 0, retired_at = COALESCE(retired_at, ?) WHERE active = 1 AND id != ?", (now, dep_id))
        cur.execute("UPDATE deployments SET active = 1, retired_at = NULL WHERE id = ?", (dep_id,))


def active(store: Any) -> dict[str, Any] | None:
    r = store.one("SELECT * FROM deployments WHERE active = 1")
    return None if r is None else {**{k: r[k] for k in r.keys() if k != "manifest_json"}, "manifest": json.loads(r["manifest_json"])}


def listing(store: Any) -> list[dict[str, Any]]:
    out = []
    for r in store.query("SELECT * FROM deployments ORDER BY created_at DESC"):
        m = json.loads(r["manifest_json"])
        out.append({"id": r["id"], "created_at": r["created_at"], "label": r["label"], "active": bool(r["active"]),
                    "retired_at": r["retired_at"], "source_run": r["source_run"], "run_kind": m.get("run_kind"),
                    "shadow_run": r["shadow_run"], "models": m["models"], "manifest_sha256": r["manifest_sha256"]})
    return out


def models(store: Any, dep_id: str) -> dict[tuple[str, int], tuple[str, str, Any]]:
    """The deployment's models as the runner's state: (task, horizon) -> (version, family, model)."""
    out = {}
    for r in store.query("SELECT * FROM deployment_models WHERE deployment_id = ?", (dep_id,)):
        out[(r["task"], int(r["horizon"]))] = (r["model_version"], r["family"], load_artifact(r["artifact"], r["artifact_sha256"]))
    if not out:
        raise DeployError(f"Deployment {dep_id} has no models")
    return out


def export(store: Any, dep_id: str, path: str | Path) -> Path:
    r = store.one("SELECT * FROM deployments WHERE id = ?", (dep_id,))
    if r is None:
        raise DeployError(f"No deployment {dep_id}")
    manifest = json.loads(r["manifest_json"])
    blobs = {f"{m['task']}:{m['horizon']}": base64.b64encode(bytes(m["artifact"])).decode()
             for m in store.query("SELECT * FROM deployment_models WHERE deployment_id = ?", (dep_id,))}
    body = {"format": FORMAT, "manifest": manifest, "manifest_sha256": r["manifest_sha256"], "artifacts": blobs}
    path = Path(path)
    path.write_text(json.dumps(body))
    return path


def import_file(store: Any, path: str | Path, activate: bool = True) -> str:
    body = json.loads(Path(path).read_text())
    if body.get("format") != FORMAT:
        raise DeployError("That is not a Trade Assistant models file")
    manifest = body["manifest"]
    if _sha(manifest) != body.get("manifest_sha256"):
        raise DeployError("The models file's manifest does not match its hash: it has been altered")
    if manifest.get("features") != FEATURE_VERSION:
        raise DeployError(f"The models were trained on features {manifest.get('features')}; this app computes {FEATURE_VERSION}")
    from ..models.daily import uses_factors
    if any(uses_factors(e["family"]) for e in manifest["models"]) and manifest.get("factors") != FACTOR_VERSION:
        raise DeployError(f"The models read factors {manifest.get('factors') or '(none recorded)'}; this app computes {FACTOR_VERSION}")
    blobs = {}
    for e in manifest["models"]:
        raw = body["artifacts"].get(f"{e['task']}:{e['horizon']}")
        if raw is None:
            raise DeployError(f"The models file has no {e['task']} model for horizon {e['horizon']}")
        blobs[(e["task"], int(e["horizon"]))] = base64.b64decode(raw)
    return _insert(store, manifest, blobs, f"imported from {Path(path).name}", activate)


__all__ = ["deploy", "export", "import_file", "set_active", "active", "listing", "models", "DeployError", "FORMAT"]
