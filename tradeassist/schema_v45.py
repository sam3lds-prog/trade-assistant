"""4.5.x migrations of the trading database. Same rules as schema_v4.py: never edit a shipped migration; each must be
safe on any older database; one statement per execute. Every change here only adds (columns with defaults, tables,
indexes), so the 4.5.0 code still runs on a migrated database if a release is rolled back.
"""

from __future__ import annotations

import logging
import sqlite3

from .migrations import migration

log = logging.getLogger(__name__)


def _columns(cur: sqlite3.Cursor, table: str) -> set[str]:
    return {row[1] for row in cur.execute(f"PRAGMA table_info({table})").fetchall()}


@migration(28, "4.5.1 verified triggers, resumable finalization, one automatic version per run")
def verified_triggers(cur: sqlite3.Cursor) -> None:
    """A trigger is now evidence submitted for review with a verification status (unverified, verification_pending,
    verified, rejected, expired) and every time that matters kept apart: when the event is said to have happened,
    when the source says it was published (and when you said it was), when it was retrieved and verified, when you
    recorded it, and the study's evidence cutoff. Triggers recorded before 4.5.1 were never verified: they are
    `unverified` and no longer qualify a setup on their own. Every submission is also kept, in order, in
    `scenario_trigger_evidence`.

    One automatic (or control) version per provider run, so finishing a run again after a crash reuses its version
    instead of adding another (only when the database has no duplicates already; otherwise the code's own check
    applies). A decision is found by its valuation, so a resumed finalization does not record or notify twice."""
    cols = _columns(cur, "scenario_triggers")
    for name, ddl in (("status", "TEXT NOT NULL DEFAULT 'unverified'"), ("claimed_published_at", "TEXT"), ("published_at", "TEXT"),
                      ("event_at", "TEXT"), ("retrieved_at", "TEXT"), ("verified_at", "TEXT"), ("cutoff", "TEXT"),
                      ("deadline", "TEXT"), ("verification_json", "TEXT NOT NULL DEFAULT '{}'"), ("version_id", "TEXT")):
        if name not in cols:
            cur.execute(f"ALTER TABLE scenario_triggers ADD COLUMN {name} {ddl}")
    # Before 4.5.1 observed_at held the publication time you entered: it is your claim, not the source's.
    cur.execute("UPDATE scenario_triggers SET claimed_published_at = observed_at WHERE claimed_published_at IS NULL")
    cur.execute("""CREATE TABLE IF NOT EXISTS scenario_trigger_evidence (
        id                 INTEGER PRIMARY KEY AUTOINCREMENT,
        study_id           TEXT NOT NULL REFERENCES studies (id),
        case_key           TEXT NOT NULL,
        submitted_at       TEXT NOT NULL,
        source_json        TEXT NOT NULL,
        status             TEXT NOT NULL,
        verification_json  TEXT NOT NULL DEFAULT '{}'
    )""")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_trigger_evidence_study ON scenario_trigger_evidence (study_id, case_key, id)")
    dup = cur.execute("""SELECT run_id FROM scenario_versions WHERE origin IN ('auto', 'control') GROUP BY run_id
                         HAVING COUNT(*) > 1 LIMIT 1""").fetchone()
    if dup is None:
        cur.execute("""CREATE UNIQUE INDEX IF NOT EXISTS idx_scenario_versions_one_auto ON scenario_versions (run_id)
                       WHERE origin IN ('auto', 'control')""")
    else:
        log.warning("Run %s has more than one automatic scenario version: the one-per-run index is not created", dup[0])
    cur.execute("CREATE INDEX IF NOT EXISTS idx_scenario_decisions_valuation ON scenario_decisions (study_id, valuation_id)")
