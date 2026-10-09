"""Reproducible research runs: what produced a backtest or study result, and whether it changed.

Each run is recorded with three hashes:

    config_hash   the strategy or study settings, parameters, symbols, date range, costs and
                  execution assumptions, random seed, data source and the versions of the app
                  and of each engine
    data_hash     the price history it read: for each symbol, the first and last date, the
                  number of bars and a hash of every open, high, low, close and volume
    result_hash   the deterministic part of the output (everything except timestamps)

Two runs with the same config and data hashes must have the same result hash; a test checks
it. When a result changes, comparing two runs says whether the settings, the data (and for
which symbols) or the code changed.
"""

from __future__ import annotations

import hashlib
import json
import math
from datetime import datetime, timezone
from typing import Any

import numpy as np
import pandas as pd

from . import __version__
from .versions import (EVALUATOR_VERSION, OPTIONS_EVALUATOR_VERSION, REGIME_ENGINE_VERSION, REVERSAL_ENGINE_VERSION,
                       SIGNAL_ENGINE_VERSION, TREND_ENGINE_VERSION)

VOLATILE = {"run_at", "run_id", "checked_at", "taken_at", "run", "generated_at"}


def _canon(value: Any) -> Any:
    """A JSON-ready form that hashes the same whenever the content is the same."""
    if isinstance(value, dict):
        return {str(k): _canon(v) for k, v in sorted(value.items(), key=lambda kv: str(kv[0])) if str(k) not in VOLATILE}
    if isinstance(value, (list, tuple)):
        return [_canon(v) for v in value]
    if isinstance(value, np.ndarray):
        return [_canon(v) for v in value.tolist()]
    if isinstance(value, (np.floating, float)):
        f = float(value)
        return None if not math.isfinite(f) else round(f, 10)
    if isinstance(value, np.integer):
        return int(value)
    if isinstance(value, (pd.Timestamp, datetime)):
        return value.isoformat()
    return value


def digest(value: Any) -> str:
    return hashlib.sha256(json.dumps(_canon(value), sort_keys=True, default=str).encode()).hexdigest()[:16]


def data_fingerprint(frames: dict[str, pd.DataFrame | None]) -> dict[str, Any]:
    per = {}
    for sym in sorted(frames):
        df = frames[sym]
        if df is None or len(df) == 0:
            per[sym] = {"bars": 0}
            continue
        cols = [c for c in ("open", "high", "low", "close", "volume") if c in df.columns]
        values = np.round(df[cols].to_numpy(dtype=float), 8)
        dates = pd.to_datetime(df["date"]).dt.strftime("%Y-%m-%d").tolist()
        h = hashlib.sha256(json.dumps(dates).encode() + values.tobytes()).hexdigest()[:16]
        per[sym] = {"bars": int(len(df)), "first": dates[0], "last": dates[-1], "hash": h}
    return {"symbols": per, "hash": digest(per)}


def versions() -> dict[str, str]:
    return {"app": __version__, "signals": SIGNAL_ENGINE_VERSION, "trend": TREND_ENGINE_VERSION,
            "regime": REGIME_ENGINE_VERSION, "evaluator": EVALUATOR_VERSION, "options_evaluator": OPTIONS_EVALUATOR_VERSION,
            "reversal": REVERSAL_ENGINE_VERSION}


def fingerprint(kind: str, config: dict[str, Any], frames: dict[str, pd.DataFrame | None], result: dict[str, Any],
                data_source: str, seed: int | None = None) -> dict[str, Any]:
    data = data_fingerprint(frames)
    cfg = {"kind": kind, "config": _canon(config), "data_source": data_source, "seed": seed, "versions": versions()}
    return {"kind": kind, "config": cfg, "config_hash": digest(cfg), "data": data, "data_hash": data["hash"],
            "result_hash": digest(result), "run_at": datetime.now(timezone.utc).isoformat(timespec="seconds")}


def record(db: Any, fp: dict[str, Any], summary: dict[str, Any]) -> int | None:
    """Store a run; returns its id (None if the table does not exist yet)."""
    try:
        with db.tx() as cur:
            cur.execute("INSERT INTO research_runs (kind, run_at, config_hash, data_hash, result_hash, fingerprint_json, "
                        "summary_json) VALUES (?, ?, ?, ?, ?, ?, ?)",
                        (fp["kind"], fp["run_at"], fp["config_hash"], fp["data_hash"], fp["result_hash"],
                         json.dumps(fp, default=str), json.dumps(_canon(summary), default=str)))
            return int(cur.lastrowid)
    except Exception:          # noqa: BLE001 - recording must never fail the run itself
        return None


def attach(db: Any, out: dict[str, Any], kind: str, config: dict[str, Any], frames: dict[str, Any], data_source: str,
           seed: int | None = None, summary_keys: tuple[str, ...] = ()) -> dict[str, Any]:
    """Fingerprint `out`, record it, and add `run` (id and hashes) to it."""
    fp = fingerprint(kind, config, frames, out, data_source, seed)
    summary = {k: out.get(k) for k in summary_keys if k in out}
    rid = record(db, fp, summary)
    out["run"] = {"id": rid, "kind": kind, "config_hash": fp["config_hash"], "data_hash": fp["data_hash"],
                  "result_hash": fp["result_hash"], "run_at": fp["run_at"], "seed": seed, "versions": fp["config"]["versions"]}
    return out


def load(db: Any, run_id: int) -> dict[str, Any] | None:
    row = db.one("SELECT * FROM research_runs WHERE id = ?", (run_id,))
    if row is None:
        return None
    out = dict(row)
    out["fingerprint"] = json.loads(out.pop("fingerprint_json"))
    out["summary"] = json.loads(out.pop("summary_json") or "{}")
    return out


def listing(db: Any, kind: str | None = None, limit: int = 50) -> list[dict[str, Any]]:
    sql = "SELECT id, kind, run_at, config_hash, data_hash, result_hash, summary_json FROM research_runs"
    params: list[Any] = []
    if kind:
        sql += " WHERE kind = ?"
        params.append(kind)
    sql += " ORDER BY id DESC LIMIT ?"
    params.append(limit)
    return [{**{k: r[k] for k in ("id", "kind", "run_at", "config_hash", "data_hash", "result_hash")},
             "summary": json.loads(r["summary_json"] or "{}")} for r in db.query(sql, params)]


def compare(a: dict[str, Any], b: dict[str, Any]) -> dict[str, Any]:
    """What differs between two recorded runs, and so what explains a different result."""
    fa, fb = a["fingerprint"], b["fingerprint"]
    sa, sb = fa["data"]["symbols"], fb["data"]["symbols"]
    data_changes = [{"symbol": s, "before": sa.get(s), "after": sb.get(s)} for s in sorted(set(sa) | set(sb))
                    if sa.get(s) != sb.get(s)]
    config_changes = []

    def walk(x: Any, y: Any, path: str) -> None:
        if isinstance(x, dict) and isinstance(y, dict):
            for k in sorted(set(x) | set(y)):
                walk(x.get(k), y.get(k), f"{path}.{k}" if path else k)
        elif x != y:
            config_changes.append({"field": path, "before": x, "after": y})
    walk(fa["config"], fb["config"], "")
    same_inputs = not data_changes and not config_changes
    if a["result_hash"] == b["result_hash"]:
        verdict = "Same result."
    elif same_inputs:
        verdict = "Different results from the same settings and data: the calculation is not deterministic. Report this."
    else:
        verdict = "The results differ because " + " and ".join(
            p for p in (("the settings or code versions changed" if config_changes else ""),
                        (f"the price history changed for {len(data_changes)} symbol(s)" if data_changes else "")) if p) + "."
    return {"a": a["id"], "b": b["id"], "same_result": a["result_hash"] == b["result_hash"], "same_inputs": same_inputs,
            "config_changes": config_changes, "data_changes": data_changes, "verdict": verdict}


__all__ = ["attach", "fingerprint", "data_fingerprint", "record", "load", "listing", "compare", "digest"]
