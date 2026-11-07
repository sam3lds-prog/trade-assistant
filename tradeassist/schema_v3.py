"""3.0 migrations: the decision records, from candidate to reconciled order (see contracts.py).

Same rules as schema_v2.py: never edit a shipped migration; each must be safe on any older database; one
statement per execute (no executescript, which would commit mid-migration). Every table that holds trading
records carries the data mode, so demo, sandbox, Robinhood and live records never mix, and every order and
proposal carries its account kind, so paper, shadow and live outcomes stay apart.
"""

from __future__ import annotations

import sqlite3

from .migrations import migration


@migration(19, "3.0 decision records: candidates, studies, proposals, approvals, orders, jobs, notifications, workspaces")
def decision_records(cur: sqlite3.Cursor) -> None:
    cur.execute("""CREATE TABLE IF NOT EXISTS opportunity_candidates (
        id                  TEXT PRIMARY KEY,
        mode                TEXT NOT NULL,
        symbol              TEXT NOT NULL,
        direction           TEXT NOT NULL CHECK (direction IN ('LONG', 'SHORT')),
        horizon             TEXT NOT NULL CHECK (horizon IN ('intraday', 'swing', 'event')),
        status              TEXT NOT NULL CHECK (status IN ('active', 'expired', 'retired')),
        kinds_json          TEXT NOT NULL,
        sources_json        TEXT NOT NULL,
        first_seen_at       TEXT NOT NULL,
        updated_at          TEXT NOT NULL,
        expires_at          TEXT NOT NULL,
        rank_score          REAL NOT NULL,
        components_json     TEXT NOT NULL,
        edge_json           TEXT NOT NULL,
        catalyst            TEXT,
        next_event_json     TEXT,
        entry               REAL,
        invalidation        REAL,
        target              REAL,
        liquidity_json      TEXT NOT NULL DEFAULT '{}',
        data_status         TEXT NOT NULL DEFAULT 'unknown',
        eligible_json       TEXT NOT NULL DEFAULT '[]',
        conflicts_json      TEXT NOT NULL DEFAULT '[]',
        autopilot_eligible  INTEGER NOT NULL DEFAULT 0,
        autopilot_json      TEXT NOT NULL DEFAULT '[]',
        study_id            TEXT,
        version             INTEGER NOT NULL DEFAULT 1
    )""")
    # One live candidate per stock, direction and horizon: a second scanner that sees the same trade adds evidence
    # to it instead of listing it again.
    cur.execute("""CREATE UNIQUE INDEX IF NOT EXISTS idx_candidates_one_active
                   ON opportunity_candidates (mode, symbol, direction, horizon) WHERE status = 'active'""")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_candidates_rank ON opportunity_candidates (mode, status, rank_score)")
    cur.execute("""CREATE TABLE IF NOT EXISTS opportunity_observations (
        id            INTEGER PRIMARY KEY AUTOINCREMENT,
        candidate_id  TEXT REFERENCES opportunity_candidates (id) ON DELETE CASCADE,
        mode          TEXT NOT NULL,
        source        TEXT NOT NULL CHECK (source IN ('morning', 'ideas', 'scanner', 'reversal', 'live', 'earnings',
                                                      'manual', 'study')),
        source_key    TEXT NOT NULL,
        symbol        TEXT NOT NULL,
        direction     TEXT NOT NULL CHECK (direction IN ('LONG', 'SHORT')),
        horizon       TEXT NOT NULL,
        kind          TEXT NOT NULL,
        observed_at   TEXT NOT NULL,
        raw_score     REAL,
        payload_json  TEXT NOT NULL,
        UNIQUE (mode, source, source_key)
    )""")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_observations_candidate ON opportunity_observations (candidate_id, observed_at)")
    cur.execute("""CREATE TABLE IF NOT EXISTS studies (
        id                TEXT PRIMARY KEY,
        mode              TEXT NOT NULL,
        symbol            TEXT NOT NULL,
        created_at        TEXT NOT NULL,
        as_of             TEXT NOT NULL,
        horizon_days      INTEGER NOT NULL CHECK (horizon_days > 0),
        thesis            TEXT NOT NULL,
        direction         TEXT CHECK (direction IS NULL OR direction IN ('LONG', 'SHORT')),
        objective         TEXT,
        status            TEXT NOT NULL CHECK (status IN ('running', 'complete', 'partial', 'failed')),
        engine_version    TEXT NOT NULL,
        contract_version  TEXT NOT NULL,
        opportunity_id    TEXT,
        inputs_json       TEXT NOT NULL,
        evidence_json     TEXT NOT NULL,
        forecast_json     TEXT,
        strategies_json   TEXT,
        fingerprint       TEXT NOT NULL,
        error             TEXT
    )""")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_studies_symbol ON studies (mode, symbol, created_at)")
    cur.execute("""CREATE TABLE IF NOT EXISTS forecast_records (
        id             INTEGER PRIMARY KEY AUTOINCREMENT,
        mode           TEXT NOT NULL,
        study_id       TEXT,
        symbol         TEXT NOT NULL,
        model_version  TEXT NOT NULL,
        kind           TEXT NOT NULL DEFAULT 'live' CHECK (kind IN ('live', 'backtest')),
        made_at        TEXT NOT NULL,
        as_of_date     TEXT NOT NULL,
        horizon_days   INTEGER NOT NULL CHECK (horizon_days > 0),
        target_date    TEXT NOT NULL,
        regime         TEXT,
        median_pct     REAL NOT NULL,
        low_pct        REAL NOT NULL,
        high_pct       REAL NOT NULL,
        interval       REAL NOT NULL DEFAULT 0.8,
        baseline_json  TEXT NOT NULL DEFAULT '{}',
        realized_pct   REAL,
        covered        INTEGER,
        direction_hit  INTEGER,
        evaluated_at   TEXT,
        UNIQUE (mode, kind, symbol, model_version, as_of_date, horizon_days, study_id)
    )""")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_forecasts_due ON forecast_records (mode, evaluated_at, target_date)")
    cur.execute("""CREATE TABLE IF NOT EXISTS trading_policies (
        id          TEXT PRIMARY KEY,
        name        TEXT NOT NULL UNIQUE,
        mode        TEXT NOT NULL CHECK (mode IN ('disabled', 'paper', 'approval_required', 'capped_autopilot')),
        body_json   TEXT NOT NULL,
        version     INTEGER NOT NULL DEFAULT 1,
        enabled     INTEGER NOT NULL DEFAULT 0,
        created_at  TEXT NOT NULL,
        updated_at  TEXT NOT NULL
    )""")
    cur.execute("""CREATE TABLE IF NOT EXISTS trade_proposals (
        id                  TEXT PRIMARY KEY,
        mode                TEXT NOT NULL,
        account_kind        TEXT NOT NULL CHECK (account_kind IN ('paper', 'live', 'shadow')),
        account_ref         TEXT,
        created_at          TEXT NOT NULL,
        updated_at          TEXT NOT NULL,
        source              TEXT NOT NULL,
        status              TEXT NOT NULL CHECK (status IN ('draft', 'awaiting_approval', 'authorized', 'submitting',
                                     'submitted', 'partially_filled', 'filled', 'cancel_requested', 'cancelled', 'rejected',
                                     'expired', 'blocked', 'unknown', 'failed', 'closed')),
        symbol              TEXT NOT NULL,
        instrument          TEXT NOT NULL CHECK (instrument IN ('equity', 'option')),
        side                TEXT NOT NULL,
        quantity            REAL NOT NULL CHECK (quantity > 0),
        order_type          TEXT NOT NULL,
        limit_price         REAL CHECK (limit_price IS NULL OR limit_price > 0),
        terms_json          TEXT NOT NULL,
        order_hash          TEXT NOT NULL,
        study_id            TEXT,
        opportunity_id      TEXT,
        thesis              TEXT NOT NULL DEFAULT '',
        evidence_hash       TEXT,
        exit_plan_json      TEXT NOT NULL DEFAULT '{}',
        risk_json           TEXT,
        policy_id           TEXT,
        authorization_json  TEXT,
        expires_at          TEXT NOT NULL,
        version             INTEGER NOT NULL DEFAULT 1
    )""")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_proposals_status ON trade_proposals (mode, account_kind, status, updated_at)")
    cur.execute("""CREATE TABLE IF NOT EXISTS proposal_events (
        id           INTEGER PRIMARY KEY AUTOINCREMENT,
        proposal_id  TEXT NOT NULL REFERENCES trade_proposals (id) ON DELETE CASCADE,
        at           TEXT NOT NULL,
        from_status  TEXT,
        to_status    TEXT NOT NULL,
        actor        TEXT NOT NULL,
        reason       TEXT NOT NULL DEFAULT '',
        data_json    TEXT
    )""")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_proposal_events ON proposal_events (proposal_id, id)")
    cur.execute("""CREATE TABLE IF NOT EXISTS approvals (
        id           TEXT PRIMARY KEY,
        proposal_id  TEXT NOT NULL REFERENCES trade_proposals (id) ON DELETE CASCADE,
        created_at   TEXT NOT NULL,
        expires_at   TEXT NOT NULL,
        token_hash   TEXT NOT NULL UNIQUE,
        order_hash   TEXT NOT NULL,
        decision     TEXT NOT NULL CHECK (decision IN ('pending', 'approved', 'declined', 'expired', 'superseded')),
        decided_at   TEXT,
        decided_by   TEXT,
        channel      TEXT,
        used_at      TEXT
    )""")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_approvals_proposal ON approvals (proposal_id, decision)")
    cur.execute("""CREATE TABLE IF NOT EXISTS broker_orders (
        id                  TEXT PRIMARY KEY,
        proposal_id         TEXT NOT NULL REFERENCES trade_proposals (id),
        mode                TEXT NOT NULL,
        account_kind        TEXT NOT NULL CHECK (account_kind IN ('paper', 'live', 'shadow')),
        broker              TEXT NOT NULL CHECK (broker IN ('paper', 'robinhood', 'fake')),
        account_ref         TEXT,
        client_order_id     TEXT NOT NULL UNIQUE,
        broker_order_id     TEXT,
        state               TEXT NOT NULL CHECK (state IN ('intent', 'submitting', 'submitted', 'unknown', 'accepted',
                                     'partially_filled', 'filled', 'cancel_requested', 'cancelled', 'rejected', 'expired',
                                     'failed')),
        created_at          TEXT NOT NULL,
        updated_at          TEXT NOT NULL,
        terms_json          TEXT NOT NULL,
        review_json         TEXT,
        request_json        TEXT,
        response_json       TEXT,
        filled_quantity     REAL NOT NULL DEFAULT 0 CHECK (filled_quantity >= 0),
        average_fill_price  REAL,
        last_reconciled_at  TEXT,
        error               TEXT
    )""")
    cur.execute("""CREATE UNIQUE INDEX IF NOT EXISTS idx_broker_orders_external
                   ON broker_orders (broker, broker_order_id) WHERE broker_order_id IS NOT NULL""")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_broker_orders_state ON broker_orders (account_kind, state)")
    cur.execute("""CREATE TABLE IF NOT EXISTS broker_fills (
        id           INTEGER PRIMARY KEY AUTOINCREMENT,
        order_id     TEXT NOT NULL REFERENCES broker_orders (id) ON DELETE CASCADE,
        at           TEXT NOT NULL,
        quantity     REAL NOT NULL CHECK (quantity > 0),
        price        REAL NOT NULL CHECK (price >= 0),
        fees         REAL NOT NULL DEFAULT 0 CHECK (fees >= 0),
        external_id  TEXT NOT NULL,
        UNIQUE (order_id, external_id)
    )""")
    cur.execute("""CREATE TABLE IF NOT EXISTS budget_ledger (
        id            INTEGER PRIMARY KEY AUTOINCREMENT,
        at            TEXT NOT NULL,
        mode          TEXT NOT NULL,
        account_kind  TEXT NOT NULL CHECK (account_kind IN ('paper', 'live', 'shadow')),
        policy_id     TEXT,
        proposal_id   TEXT,
        order_id      TEXT,
        kind          TEXT NOT NULL CHECK (kind IN ('reserve', 'release', 'fill', 'realized', 'adjust')),
        amount        REAL NOT NULL,
        note          TEXT NOT NULL DEFAULT ''
    )""")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_budget_ledger ON budget_ledger (mode, account_kind, at)")
    cur.execute("""CREATE TABLE IF NOT EXISTS jobs (
        id            INTEGER PRIMARY KEY AUTOINCREMENT,
        kind          TEXT NOT NULL,
        dedupe_key    TEXT UNIQUE,
        status        TEXT NOT NULL CHECK (status IN ('queued', 'running', 'done', 'failed', 'discarded')),
        due_at        TEXT NOT NULL,
        stale_after   TEXT,                -- past this, the job is discarded, not run late
        created_at    TEXT NOT NULL,
        started_at    TEXT,
        finished_at   TEXT,
        attempts      INTEGER NOT NULL DEFAULT 0,
        max_attempts  INTEGER NOT NULL DEFAULT 3,
        lease_until   TEXT,
        worker        TEXT,
        payload_json  TEXT NOT NULL DEFAULT '{}',
        result_json   TEXT,
        error         TEXT
    )""")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_jobs_due ON jobs (status, due_at)")
    cur.execute("""CREATE TABLE IF NOT EXISTS heartbeats (
        component  TEXT PRIMARY KEY,
        at         TEXT NOT NULL,
        pid        INTEGER,
        host       TEXT,
        info_json  TEXT NOT NULL DEFAULT '{}'
    )""")
    cur.execute("""CREATE TABLE IF NOT EXISTS notifications (
        id               INTEGER PRIMARY KEY AUTOINCREMENT,
        created_at       TEXT NOT NULL,
        kind             TEXT NOT NULL,
        severity         TEXT NOT NULL CHECK (severity IN ('info', 'warn', 'critical')),
        subject          TEXT NOT NULL,
        body             TEXT NOT NULL,
        link             TEXT,
        proposal_id      TEXT,
        dedupe_key       TEXT UNIQUE,
        status           TEXT NOT NULL CHECK (status IN ('queued', 'sent', 'partial', 'failed', 'suppressed')),
        attempts         INTEGER NOT NULL DEFAULT 0,
        next_attempt_at  TEXT,
        channels_json    TEXT NOT NULL DEFAULT '[]',
        results_json     TEXT NOT NULL DEFAULT '{}',
        sent_at          TEXT
    )""")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_notifications_status ON notifications (status, next_attempt_at)")
    cur.execute("""CREATE TABLE IF NOT EXISTS workspace_layouts (
        id          TEXT PRIMARY KEY,
        workspace   TEXT NOT NULL CHECK (workspace IN ('discover', 'research', 'desk', 'portfolio')),
        name        TEXT NOT NULL,
        layout_json TEXT NOT NULL,
        state_json  TEXT NOT NULL DEFAULT '{}',
        is_default  INTEGER NOT NULL DEFAULT 0,
        created_at  TEXT NOT NULL,
        updated_at  TEXT NOT NULL,
        UNIQUE (workspace, name)
    )""")
    cur.execute("""CREATE TABLE IF NOT EXISTS remote_sessions (
        token_hash    TEXT PRIMARY KEY,
        created_at    TEXT NOT NULL,
        expires_at    TEXT NOT NULL,
        last_seen_at  TEXT NOT NULL,
        label         TEXT NOT NULL DEFAULT '',
        revoked_at    TEXT
    )""")
    cur.execute("""CREATE TABLE IF NOT EXISTS execution_state (
        key         TEXT PRIMARY KEY,
        value_json  TEXT NOT NULL,
        updated_at  TEXT NOT NULL
    )""")
    cur.execute("""CREATE TABLE IF NOT EXISTS audit_log (
        id         INTEGER PRIMARY KEY AUTOINCREMENT,
        at         TEXT NOT NULL,
        actor      TEXT NOT NULL,
        action     TEXT NOT NULL,
        subject    TEXT,
        data_json  TEXT
    )""")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_audit_log ON audit_log (action, id)")
    # The shared event stream (bus.py): what changed, for the screens and for the other process. Pruned as it grows.
    cur.execute("""CREATE TABLE IF NOT EXISTS app_events (
        id         INTEGER PRIMARY KEY AUTOINCREMENT,
        at         TEXT NOT NULL,
        topic      TEXT NOT NULL,
        subject    TEXT,
        origin     TEXT NOT NULL DEFAULT 'app',
        data_json  TEXT NOT NULL DEFAULT '{}'
    )""")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_app_events_topic ON app_events (topic, id)")


@migration(20, "3.0 studies keep what the research screen shows")
def study_views(cur: sqlite3.Cursor) -> None:
    """The study's own record is the contract (evidence, forecast, strategies); the screen also shows the analog
    table, the peers and the forecast's chart overlay. Kept beside it so a saved study reopens exactly as it ran."""
    columns = {row[1] for row in cur.execute("PRAGMA table_info(studies)").fetchall()}
    if "view_json" not in columns:
        cur.execute("ALTER TABLE studies ADD COLUMN view_json TEXT")
