"""The held-out year, opened once (4.0, phase 4).

Development replays end before config.HOLDOUT_FROM and never load a bar after it. Once the design is settled, the
year from then to the dataset's end is replayed exactly as the development run would have continued:

- it starts with the models the development run ended with (registry.run_state, inherited by the runner);
- it keeps retraining on the same schedule under the same rules, as it would have in production;
- every forecast is frozen and graded like any other.

The first opening is the untouched test. The register (`holdout_openings`) records every opening. A second one needs
`reopen=True`, and its scorecard says plainly that the year is no longer untouched: once a result has been seen and
the design changed because of it, the year is just more development data.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import date, datetime, timezone
from typing import Any

from ..config import HOLDOUT_FROM, ReplayConfig
from .runner import ReplayError, register_run


def openings(store: Any) -> list[dict[str, Any]]:
    return [dict(r) for r in store.query("SELECT * FROM holdout_openings ORDER BY id")]


def open_holdout(store: Any, parent_run: str, dataset_id: str | None = None, *, reopen: bool = False, note: str = "",
                 allow_failed_checks: bool = False) -> str:
    parent = store.one("SELECT * FROM replay_runs WHERE id = ?", (parent_run,))
    if parent is None:
        raise ReplayError(f"No run {parent_run}")
    if parent["kind"] != "development" or parent["status"] != "complete":
        raise ReplayError("The held-out year is evaluated from a finished development run.")
    previous = openings(store)
    if previous and not reopen:
        first = previous[0]
        raise ReplayError(f"The held-out year was already opened on {first['opened_at'][:10]} (run {first['run_id']}). Opening it "
                          "again means it is no longer untouched; pass reopen to do it anyway, and the scorecard will say so.")
    dataset_id = dataset_id or parent["dataset_id"]
    ds = store.one("SELECT * FROM datasets WHERE id = ?", (dataset_id,))
    if ds is None:
        raise ReplayError(f"No dataset {dataset_id}")
    last = date.fromisoformat(ds["last_date"])
    sessions = [r["date"] for r in store.query("SELECT date FROM dataset_bars WHERE dataset_id = ? AND symbol = 'SPY' AND date >= ? "
                                                "ORDER BY date", (dataset_id, HOLDOUT_FROM.isoformat()))]
    if len(sessions) < 20:
        raise ReplayError(f"Dataset {dataset_id} reaches {last}: build a newer one (python run.py --replay dataset build) "
                          f"that covers the held-out year from {HOLDOUT_FROM}.")
    cfg = ReplayConfig.from_dict(json.loads(parent["config_json"])).with_(
        kind="holdout", start=date.fromisoformat(sessions[0]), end=date.fromisoformat(sessions[-1]))

    def register(cur: Any, run_id: str) -> None:
        # counted again inside the transaction that creates the run (and `ordinal` is unique), so two openings at
        # once cannot both be the first, and a run never exists without its entry in the register
        cur.execute("SELECT COUNT(*) FROM holdout_openings")
        n = int(cur.fetchone()[0])
        if n and not reopen:
            raise ReplayError("The held-out year was opened a moment ago by another process.")
        cur.execute("INSERT INTO holdout_openings (run_id, parent_run, opened_at, ordinal, note) VALUES (?, ?, ?, ?, ?)",
                    (run_id, parent_run, datetime.now(timezone.utc).isoformat(timespec="seconds"), n + 1, note))
    try:
        return register_run(store, dataset_id, cfg, parent_run=parent_run, allow_failed_checks=allow_failed_checks, then=register)
    except sqlite3.IntegrityError:
        raise ReplayError("The held-out year was opened a moment ago by another process.") from None


def holdout_note(store: Any, run_id: str) -> str | None:
    """What a holdout run's scorecard must say about how untouched its year was."""
    r = store.one("SELECT ordinal FROM holdout_openings WHERE run_id = ?", (run_id,))
    if r is None:
        return None
    if r["ordinal"] == 1:
        return "The first evaluation of the held-out year: no model or rule was chosen with these sessions in view."
    return (f"The held-out year has been opened {r['ordinal']} times: it is no longer untouched, so this result is "
            "development evidence, not a final test.")


__all__ = ["open_holdout", "openings", "holdout_note"]
