"""Later replay.db migrations (after store.py's first). Same rules: never edit a shipped one; add the next number."""

from __future__ import annotations

import sqlite3

from .store import _freeze, migration


@migration(2, "4.0 phase 4: deployed models, the holdout register, run controls")
def deployments(cur: sqlite3.Cursor) -> None:
    # A deployment is a frozen set of models, one per task, copied out of a finished run (or imported from another
    # computer). Its models never change; a new set is a new deployment. One is active at a time.
    cur.execute("""CREATE TABLE IF NOT EXISTS deployments (
        id               TEXT PRIMARY KEY,
        created_at       TEXT NOT NULL,
        source_run       TEXT NOT NULL,
        label            TEXT NOT NULL DEFAULT '',
        manifest_json    TEXT NOT NULL,
        manifest_sha256  TEXT NOT NULL,
        active           INTEGER NOT NULL DEFAULT 0,
        retired_at       TEXT,
        shadow_run       TEXT REFERENCES replay_runs (id)
    )""")
    cur.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_deployments_one_active ON deployments (active) WHERE active = 1")
    cur.execute("""CREATE TABLE IF NOT EXISTS deployment_models (
        deployment_id    TEXT NOT NULL REFERENCES deployments (id),
        task             TEXT NOT NULL,
        horizon          INTEGER NOT NULL,
        model_version    TEXT NOT NULL,
        family           TEXT NOT NULL,
        trained_through  TEXT,
        artifact         BLOB NOT NULL,
        artifact_sha256  TEXT NOT NULL,
        PRIMARY KEY (deployment_id, task, horizon)
    )""")
    _freeze(cur, "deployment_models")
    # Every time the held-out year is evaluated. The first opening is the untouched test; any later one is recorded
    # as a reopening, and its scorecard says the year is no longer untouched.
    cur.execute("""CREATE TABLE IF NOT EXISTS holdout_openings (
        id           INTEGER PRIMARY KEY AUTOINCREMENT,
        run_id       TEXT NOT NULL REFERENCES replay_runs (id),
        parent_run   TEXT NOT NULL REFERENCES replay_runs (id),
        opened_at    TEXT NOT NULL,
        ordinal      INTEGER NOT NULL UNIQUE,     -- two openings at once cannot both be the first
        note         TEXT NOT NULL DEFAULT ''
    )""")
    _freeze(cur, "holdout_openings")
    # A stop asked for from the screen: the run checks it between sessions and pauses at its checkpoint.
    cur.execute("""CREATE TABLE IF NOT EXISTS run_controls (
        run_id             TEXT PRIMARY KEY REFERENCES replay_runs (id),
        stop_requested_at  TEXT,
        log_path           TEXT,
        spawned_at         TEXT               -- when the screen last started a process for it (a second click waits)
    )""")
