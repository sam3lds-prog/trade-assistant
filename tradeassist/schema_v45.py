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


@migration(29, "4.5.2 simulation usage: one ledger row per provider run, with its estimate, its measured or entered cost and its ids")
def scenario_usage(cur: sqlite3.Cursor) -> None:
    """What each scenario run cost, or may have cost: its profile, model, MiroFish ids, agents and rounds, times and
    status, the estimate made before it started (a range, with its basis), the cost measured (an AI call's tokens at
    the prices you set) or entered by you from your provider's bill, and how sure that is (`measured`,
    `user_entered`, `estimate`, `unknown`). Interrupted and failed runs are kept: a run that reached a paid stage
    has a cost even when it produced nothing. An unknown cost is NULL, never 0."""
    cur.execute("""CREATE TABLE IF NOT EXISTS scenario_usage (
        run_id           TEXT PRIMARY KEY,
        study_id         TEXT NOT NULL,
        provider         TEXT NOT NULL,
        role             TEXT NOT NULL DEFAULT 'provider',
        profile          TEXT,
        model            TEXT,
        project_id       TEXT,
        simulation_id    TEXT,
        agents           INTEGER,
        rounds           INTEGER,
        total_rounds     INTEGER,
        created_at       TEXT NOT NULL,
        started_at       TEXT,
        ended_at         TEXT,
        status           TEXT NOT NULL,
        stage_reached    TEXT,
        day              TEXT NOT NULL,
        month            TEXT NOT NULL,
        est_low_usd      REAL,
        est_high_usd     REAL,
        est_basis        TEXT,
        measured_usd     REAL,
        cost_confidence  TEXT NOT NULL DEFAULT 'unknown',
        input_tokens     INTEGER,
        output_tokens    INTEGER,
        retries          INTEGER NOT NULL DEFAULT 0,
        stopped_early    INTEGER NOT NULL DEFAULT 0,
        approval_json    TEXT NOT NULL DEFAULT '{}',
        note             TEXT,
        updated_at       TEXT NOT NULL
    )""")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_scenario_usage_day ON scenario_usage (provider, day)")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_scenario_usage_month ON scenario_usage (provider, month)")


@migration(30, "4.5.3 forward validation: each forecast's provenance and errors, and each Qualified setup's frozen outcome record")
def forward_validation(cur: sqlite3.Cursor) -> None:
    """Each frozen forecast also keeps its data cutoff, model version, simulation profile and configuration, the
    catalyst's kind, the market regime at the study, whether its band is its own or borrowed (no change uses the
    base's), and once graded its signed error and band width, so accuracy can be broken down and compared honestly.
    A Qualified setup's decision record is frozen once (its legs, prices, entry and exit rules, projected results) and
    graded after its exit session as observed, historical hypothetical, model-only or ungradable."""
    cols = _columns(cur, "scenario_forecasts")
    for name, ddl in (("data_cutoff", "TEXT"), ("model_version", "TEXT"), ("profile", "TEXT"), ("config_json", "TEXT"),
                      ("catalyst_kind", "TEXT"), ("market_regime", "TEXT"), ("band_basis", "TEXT"), ("signed_error", "REAL"),
                      ("width", "REAL")):
        if name not in cols:
            cur.execute(f"ALTER TABLE scenario_forecasts ADD COLUMN {name} {ddl}")
    cur.execute("UPDATE scenario_forecasts SET band_basis = 'base_band' WHERE variant = 'no_change' AND band_basis IS NULL")
    cur.execute("UPDATE scenario_forecasts SET signed_error = realized_pct - median_pct WHERE status = 'graded' AND signed_error IS NULL")
    cur.execute("UPDATE scenario_forecasts SET width = high_pct - low_pct WHERE width IS NULL")
    cur.execute("""CREATE TABLE IF NOT EXISTS scenario_strategy_outcomes (
        id              TEXT PRIMARY KEY,
        decision_id     TEXT NOT NULL UNIQUE,
        study_id        TEXT NOT NULL,
        version_id      TEXT,
        valuation_id    TEXT,
        symbol          TEXT NOT NULL,
        mode            TEXT NOT NULL,
        profile         TEXT,
        catalyst_kind   TEXT,
        recommended_at  TEXT NOT NULL,
        direction       TEXT,
        strategy        TEXT NOT NULL,
        exit_date       TEXT NOT NULL,
        horizon         INTEGER,
        record_json     TEXT NOT NULL,
        status          TEXT NOT NULL CHECK (status IN ('open', 'graded', 'ungradable')),
        category        TEXT CHECK (category IS NULL OR category IN ('historical_hypothetical', 'model_only', 'ungradable')),
        pnl             REAL,
        return_on_risk  REAL,
        outcome_json    TEXT,
        created_at      TEXT NOT NULL,
        graded_at       TEXT
    )""")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_strategy_outcomes_due ON scenario_strategy_outcomes (status, exit_date)")
