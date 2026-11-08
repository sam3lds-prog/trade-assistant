"""The prediction ledger: what each model said, frozen when it said it, and what then happened (4.0).

Rows are only ever inserted. The database refuses an UPDATE or DELETE on a prediction, an outcome or a take/pass
decision (store.py), so a forecast can never be revised once its price is known.

Each prediction carries a hash of its own content (`row_sha256`) and a hash of the feature row it was made from,
and a run's ledger hash folds them all together in a fixed order. Two runs of the same dataset with the same
configuration must end with the same ledger hash, including a run that was stopped and resumed part way: that is
the reproducibility test (tests/test_v4_replay.py). Floats are written to twelve significant digits for the hash.
"""

from __future__ import annotations

import hashlib
import json
import math
from typing import Any, Iterable

PREDICTION_FIELDS = ("task", "horizon", "symbol", "session", "target_session", "model_version", "family", "last_close",
                     "pred_return", "low_return", "high_return", "p_up", "sigma", "setup", "p_target", "p_stop", "p_timeout",
                     "expected_return", "regime")


def _fmt(v: Any) -> str:
    if v is None:
        return "~"
    if isinstance(v, float):
        return "nan" if not math.isfinite(v) else format(v, ".12g")
    return str(v)


def row_hash(row: dict[str, Any]) -> str:
    body = "|".join(_fmt(row.get(k)) for k in PREDICTION_FIELDS) + "|" + _fmt(row.get("fhash"))
    return hashlib.sha256(body.encode()).hexdigest()


def feature_hash(values: Iterable[float]) -> str:
    return hashlib.sha256(",".join(_fmt(float(v)) for v in values).encode()).hexdigest()[:16]


def _clean(v: Any) -> Any:
    if isinstance(v, float) and not math.isfinite(v):
        return None
    return v


def insert_predictions(cur: Any, run_id: str, cutoff_at: str, rows: list[dict[str, Any]]) -> list[int]:
    ids = []
    for r in rows:
        r = {k: _clean(v) for k, v in r.items()}
        payload = dict(r.get("payload") or {})
        payload["fhash"] = r.get("fhash")
        cur.execute(f"""INSERT INTO predictions (run_id, cutoff_at, {", ".join(PREDICTION_FIELDS)}, payload_json, row_sha256)
                        VALUES (?, ?, {", ".join("?" for _ in PREDICTION_FIELDS)}, ?, ?)""",
                    (run_id, cutoff_at, *[r.get(k) for k in PREDICTION_FIELDS], json.dumps(payload, default=str), row_hash(r)))
        ids.append(int(cur.lastrowid))
    return ids


def due(cur: Any, run_id: str, session: str) -> list[dict[str, Any]]:
    """Predictions whose window ends at this session's close and that have no outcome yet."""
    cur.execute("""SELECT p.* FROM predictions p LEFT JOIN outcomes o ON o.prediction_id = p.id
                   WHERE p.run_id = ? AND p.target_session = ? AND o.prediction_id IS NULL ORDER BY p.id""", (run_id, session))
    cols = [d[0] for d in cur.description]
    return [dict(zip(cols, r)) for r in cur.fetchall()]


def insert_outcome(cur: Any, run_id: str, prediction_id: int, matured: str, **fields: Any) -> None:
    keys = ("status", "realized_return", "abs_error", "direction_hit", "covered", "barrier", "exit_session", "gross_return", "net_return")
    vals = [_clean(fields.get(k)) for k in keys]
    cur.execute(f"""INSERT INTO outcomes (prediction_id, run_id, matured_session, {", ".join(keys)}, payload_json)
                    VALUES (?, ?, ?, {", ".join("?" for _ in keys)}, ?)""",
                (prediction_id, run_id, matured, *vals, json.dumps(fields.get("payload") or {}, default=str)))


def insert_decision(cur: Any, run_id: str, prediction_id: int, session: str, symbol: str, take: bool, reason: str,
                    gate: dict[str, Any]) -> None:
    cur.execute("""INSERT INTO trade_decisions (prediction_id, run_id, session, symbol, take, reason, gate_json)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""", (prediction_id, run_id, session, symbol, int(take), reason,
                                                     json.dumps({k: _clean(v) for k, v in gate.items()}, default=str)))


def ledger_hash(store: Any, run_id: str) -> str:
    """One hash of every prediction and outcome of a run, independent of row ids and wall-clock times. Each
    prediction's hash is recomputed from its columns, so a row changed underneath its stored hash changes the result;
    a stored hash that no longer matches its row is an error."""
    h = hashlib.sha256()
    cols = ", ".join(f"p.{k}" for k in PREDICTION_FIELDS)
    for r in store.query(f"""SELECT {cols}, p.payload_json, p.row_sha256, o.status, o.realized_return, o.barrier, o.net_return, d.take
                             FROM predictions p LEFT JOIN outcomes o ON o.prediction_id = p.id
                             LEFT JOIN trade_decisions d ON d.prediction_id = p.id
                             WHERE p.run_id = ? ORDER BY p.session, p.task, p.horizon, p.symbol""", (run_id,)):
        row = {k: r[k] for k in PREDICTION_FIELDS}
        row["fhash"] = json.loads(r["payload_json"]).get("fhash")
        digest = row_hash(row)
        if digest != r["row_sha256"]:
            raise LedgerError(f"A prediction of run {run_id} ({row['symbol']} {row['session']}) no longer matches its hash")
        h.update(("|".join(_fmt(x) for x in (digest, r["status"], r["realized_return"], r["barrier"], r["net_return"], r["take"])) + "\n").encode())
    return h.hexdigest()


class LedgerError(RuntimeError):
    pass


__all__ = ["LedgerError", "insert_predictions", "insert_outcome", "insert_decision", "due", "ledger_hash", "row_hash", "feature_hash",
           "PREDICTION_FIELDS"]
