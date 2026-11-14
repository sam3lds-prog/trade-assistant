"""4.7 migrations of the trading database. Same rules as schema_v4.py and schema_v45.py: never edit a shipped migration;
each must be safe on any older database; one statement per execute. Only tables and indexes are added, so the 4.6 code
still runs on a migrated database if a release is rolled back (it ignores both tables).
"""

from __future__ import annotations

import sqlite3

from .migrations import migration


@migration(31, "4.7 forecast studio: each study's view preferences, and exploratory assessments kept apart from decisions")
def forecast_studio(cur: sqlite3.Cursor) -> None:
    """`study_presentation` holds how one study is being looked at: the scenario version and case selected, the version
    pinned for comparison, which chart layers are shown and the chart's focus. It is a view preference, kept apart from
    the immutable research records, and one row per study, so hiding a line on one study never hides it on another.

    `scenario_assessments` holds what the Opportunity Engine's checks say about an exploratory (person-edited) scenario
    version: research only. It is never published to Discover, never notified, never frozen for the forward scorecard
    and never written to `scenario_decisions`, which stay the official decisions of the one-click pipeline."""
    cur.execute("""CREATE TABLE IF NOT EXISTS study_presentation (
        study_id    TEXT PRIMARY KEY REFERENCES studies (id),
        state_json  TEXT NOT NULL,
        updated_at  TEXT NOT NULL
    )""")
    cur.execute("""CREATE TABLE IF NOT EXISTS scenario_assessments (
        id               TEXT PRIMARY KEY,
        study_id         TEXT NOT NULL REFERENCES studies (id),
        version_id       TEXT NOT NULL REFERENCES scenario_versions (id),
        valuation_id     TEXT,
        created_at       TEXT NOT NULL,
        status           TEXT NOT NULL CHECK (status IN ('qualified', 'watch', 'no_setup', 'incomplete')),
        assessment_json  TEXT NOT NULL
    )""")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_scenario_assessments_version ON scenario_assessments (version_id, created_at)")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_scenario_assessments_study ON scenario_assessments (study_id, created_at)")
