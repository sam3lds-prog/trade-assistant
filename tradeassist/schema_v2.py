"""Schema changes for Trade Assistant V2, as numbered migrations (see migrations.py)."""

from __future__ import annotations

import sqlite3

from .migrations import migration


@migration(1, "unified positions, option contracts, executions and position snapshots")
def unified_positions(cur: sqlite3.Cursor) -> None:
    for sql in (
        """CREATE TABLE IF NOT EXISTS positions (
            id                    INTEGER PRIMARY KEY AUTOINCREMENT,
            symbol                TEXT NOT NULL,
            instrument_type       TEXT NOT NULL CHECK (instrument_type IN ('STOCK', 'OPTION', 'OPTION_SPREAD')),
            direction             TEXT NOT NULL CHECK (direction IN ('LONG', 'SHORT')),
            status                TEXT NOT NULL CHECK (status IN ('PLANNED', 'OPEN', 'PARTIALLY_CLOSED', 'CLOSED',
                                                                  'CANCELLED')),
            quantity              REAL NOT NULL,           -- planned size: shares, or contracts for options
            multiplier            REAL NOT NULL DEFAULT 1, -- 100 for an equity option
            open_quantity         REAL NOT NULL DEFAULT 0, -- worked out from executions
            opened_at             TEXT,
            closed_at             TEXT,
            average_entry_price   REAL,
            average_exit_price    REAL,
            realized_pnl          REAL NOT NULL DEFAULT 0,
            fees                  REAL NOT NULL DEFAULT 0,
            thesis                TEXT NOT NULL DEFAULT '',
            setup_type            TEXT NOT NULL DEFAULT '',
            intended_horizon      INTEGER,                 -- trading days
            invalidation_price    REAL,                    -- price of the underlying that proves the idea wrong
            target_1              REAL,
            target_2              REAL,
            risk_amount           REAL,                    -- dollars at risk, if entered
            initial_risk_per_unit REAL,
            data_source           TEXT NOT NULL DEFAULT '',
            legacy_trade_id       INTEGER UNIQUE REFERENCES trades (id) ON DELETE SET NULL,
            notes                 TEXT NOT NULL DEFAULT '',
            created_at            TEXT NOT NULL,
            updated_at            TEXT NOT NULL
        )""",
        "CREATE INDEX IF NOT EXISTS idx_positions_symbol ON positions (symbol)",
        "CREATE INDEX IF NOT EXISTS idx_positions_status ON positions (status)",
        """CREATE TABLE IF NOT EXISTS option_contracts (
            position_id       INTEGER NOT NULL REFERENCES positions (id) ON DELETE CASCADE,
            leg               INTEGER NOT NULL DEFAULT 1,
            underlying_symbol TEXT NOT NULL,
            option_symbol     TEXT NOT NULL DEFAULT '',
            option_type       TEXT NOT NULL CHECK (option_type IN ('CALL', 'PUT')),
            strike            REAL NOT NULL,
            expiration        TEXT NOT NULL,
            multiplier        REAL NOT NULL DEFAULT 100,
            ratio             INTEGER NOT NULL DEFAULT 1,
            PRIMARY KEY (position_id, leg)
        )""",
        """CREATE TABLE IF NOT EXISTS executions (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            position_id INTEGER NOT NULL REFERENCES positions (id) ON DELETE CASCADE,
            side        TEXT NOT NULL CHECK (side IN ('BUY', 'SELL', 'SELL_SHORT', 'BUY_TO_COVER')),
            quantity    REAL NOT NULL CHECK (quantity > 0),
            price       REAL NOT NULL CHECK (price >= 0),
            fees        REAL NOT NULL DEFAULT 0 CHECK (fees >= 0),
            executed_at TEXT NOT NULL,
            source      TEXT NOT NULL DEFAULT 'MANUAL' CHECK (source IN ('MANUAL', 'ROBINHOOD', 'IMPORT')),
            note        TEXT NOT NULL DEFAULT '',
            created_at  TEXT NOT NULL
        )""",
        "CREATE INDEX IF NOT EXISTS idx_executions_position ON executions (position_id, executed_at)",
        """CREATE TABLE IF NOT EXISTS position_snapshots (
            id                       INTEGER PRIMARY KEY AUTOINCREMENT,
            position_id              INTEGER NOT NULL REFERENCES positions (id) ON DELETE CASCADE,
            snapshot_type            TEXT NOT NULL CHECK (snapshot_type IN ('PLAN', 'ENTRY', 'EXIT', 'REVIEW')),
            timestamp                TEXT NOT NULL,
            underlying_price REAL, spy_price REAL, qqq_price REAL, vix REAL,
            rsi_14 REAL, macd REAL, macd_signal REAL, macd_histogram REAL,
            sma_20 REAL, sma_50 REAL, sma_200 REAL, ema_20 REAL, adx REAL, atr REAL, volume_ratio REAL,
            structure_state TEXT, trend_state TEXT,
            relative_strength_spy REAL, relative_strength_sector REAL,
            bullish_score REAL, bearish_score REAL, market_regime TEXT,
            iv REAL, iv_rank REAL, delta REAL, gamma REAL, theta REAL, vega REAL,
            bid REAL, ask REAL, mid REAL, spread_pct REAL, dte INTEGER,
            signal_engine_version    TEXT,
            data_timestamp           TEXT,
            data_source              TEXT,
            bar_status               TEXT,
            raw_json                 TEXT NOT NULL DEFAULT '{}'
        )""",
        "CREATE INDEX IF NOT EXISTS idx_snapshots_position ON position_snapshots (position_id, timestamp)",
    ):
        cur.execute(sql)
    # Bring every classic option-journal entry across, so nothing already recorded is missing
    # from the new screens. The classic journal keeps working and stays in step (journal.py).
    from .positions import sync_legacy_trade
    for (trade_id,) in cur.execute("SELECT id FROM trades ORDER BY id").fetchall():
        sync_legacy_trade(cur, trade_id)


@migration(2, "record how cached daily bars are adjusted for splits and dividends")
def bar_adjustment(cur: sqlite3.Cursor) -> None:
    columns = {row[1] for row in cur.execute("PRAGMA table_info(bars_meta)").fetchall()}
    if "adjustment" not in columns:
        cur.execute("ALTER TABLE bars_meta ADD COLUMN adjustment TEXT NOT NULL DEFAULT 'unknown'")


@migration(3, "signal occurrences: each past signal with its context and what followed")
def signal_occurrences(cur: sqlite3.Cursor) -> None:
    cur.execute("""CREATE TABLE IF NOT EXISTS signal_occurrences (
        id             INTEGER PRIMARY KEY AUTOINCREMENT,
        data_source    TEXT NOT NULL,
        engine_version TEXT NOT NULL,
        trend_version  TEXT NOT NULL,
        created_at     TEXT NOT NULL,
        symbol         TEXT NOT NULL,
        signal_key     TEXT NOT NULL,      -- pattern:<key> or trend:<state>
        direction      INTEGER NOT NULL,   -- +1 expects a rise, -1 a fall, 0 neither
        date           TEXT NOT NULL,      -- the finished bar the signal came from
        close          REAL,
        trend_state    TEXT, net_score REAL, regime TEXT,
        fwd_5 REAL, fwd_10 REAL, fwd_20 REAL,   -- next open to the close 5, 10, 20 days on
        spy_5 REAL, spy_10 REAL, spy_20 REAL,   -- SPY over the same days
        high_20 REAL, low_20 REAL,              -- best and worst prices in the next 20 days, from entry
        UNIQUE (data_source, symbol, signal_key, date)
    )""")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_signals_key ON signal_occurrences (data_source, signal_key)")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_signals_symbol ON signal_occurrences (data_source, symbol)")


@migration(4, "signal occurrences: daily range on the day, and the lowest and highest close over each horizon")
def signal_atr_and_closes(cur: sqlite3.Cursor) -> None:
    # atr_pct lets past cases from a calm stock be compared with a volatile one, and the closes say
    # whether a case reached a stop on a closing basis within its horizon (options/reversal_score.py).
    columns = {row[1] for row in cur.execute("PRAGMA table_info(signal_occurrences)").fetchall()}
    for name in ("atr_pct", "close_low_5", "close_low_10", "close_low_20", "close_high_5", "close_high_10",
                 "close_high_20"):
        if name not in columns:
            cur.execute(f"ALTER TABLE signal_occurrences ADD COLUMN {name} REAL")


SNAPSHOT_PROVENANCE_COLUMNS = (
    ("execution_id", "INTEGER REFERENCES executions (id) ON DELETE SET NULL"),   # the fill an ENTRY or EXIT is about
    ("executed_at", "TEXT"),            # when that fill happened (UTC)
    ("observation_cutoff", "TEXT"),     # the latest moment of market data the snapshot uses
    ("captured_at", "TEXT"),            # when the snapshot was taken
    ("method", "TEXT"),                 # LIVE, PRIOR_BAR, END_OF_DAY or JOURNAL_QUOTE (snapshots.py)
    ("capture_timing", "TEXT"),         # CONTEMPORANEOUS or RETROSPECTIVE
    ("uses_later_data", "INTEGER"),     # 1 when the data is from after the fill: hindsight
    ("provenance_json", "TEXT NOT NULL DEFAULT '{}'"),
    ("superseded_at", "TEXT"),          # set when a later snapshot replaces this one; never deleted
    ("superseded_reason", "TEXT"),
)


@migration(5, "snapshot provenance and timing, one effective lifecycle snapshot, audited corrections, import ids")
def history_integrity(cur: sqlite3.Cursor) -> None:
    """2.1.1: what each snapshot is about and when its data is from; a record of every correction
    to a fill; broker execution ids for idempotent imports.

    Existing snapshots get their provenance inferred from what 2.0 and 2.1 stored (marked
    `inferred` in provenance_json). Where an older version stored two ENTRY or two EXIT snapshots
    for one position, the extra ones are marked superseded (the first ENTRY and the last EXIT
    stay effective); nothing is deleted. Fills are not changed."""
    import json
    from datetime import datetime, timezone

    have = {row[1] for row in cur.execute("PRAGMA table_info(position_snapshots)").fetchall()}
    for name, kind in SNAPSHOT_PROVENANCE_COLUMNS:
        if name not in have:
            cur.execute(f"ALTER TABLE position_snapshots ADD COLUMN {name} {kind}")
    have = {row[1] for row in cur.execute("PRAGMA table_info(executions)").fetchall()}
    if "external_id" not in have:
        cur.execute("ALTER TABLE executions ADD COLUMN external_id TEXT")
    cur.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_executions_external ON executions (source, external_id) "
                "WHERE external_id IS NOT NULL")
    cur.execute("""CREATE TABLE IF NOT EXISTS execution_corrections (
        id             INTEGER PRIMARY KEY AUTOINCREMENT,
        position_id    INTEGER NOT NULL REFERENCES positions (id) ON DELETE CASCADE,
        execution_id   INTEGER,                 -- kept even if the fill is later removed
        action         TEXT NOT NULL CHECK (action IN ('UPDATE', 'DELETE', 'INSERT')),
        previous_json  TEXT NOT NULL,           -- the fill's values before
        new_json       TEXT NOT NULL,           -- and after
        reason         TEXT NOT NULL CHECK (length(trim(reason)) > 0),
        source         TEXT NOT NULL,           -- USER, JOURNAL_SYNC or REPAIR
        ledger_before  TEXT NOT NULL,
        ledger_after   TEXT NOT NULL,
        changed_at     TEXT NOT NULL
    )""")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_corrections_position ON execution_corrections (position_id, changed_at)")
    cur.execute("""CREATE TABLE IF NOT EXISTS snapshot_reconstruction (
        position_id   INTEGER NOT NULL REFERENCES positions (id) ON DELETE CASCADE,
        snapshot_type TEXT NOT NULL,
        execution_id  INTEGER NOT NULL DEFAULT 0,     -- 0 for a PLAN, which has no fill
        status        TEXT NOT NULL CHECK (status IN ('CREATED', 'UNAVAILABLE')),
        snapshot_id   INTEGER,
        reason        TEXT NOT NULL DEFAULT '',
        attempts      INTEGER NOT NULL DEFAULT 1,
        updated_at    TEXT NOT NULL,
        PRIMARY KEY (position_id, snapshot_type, execution_id)
    )""")

    def when(text: str | None) -> datetime | None:
        if not text:
            return None
        try:
            moment = datetime.fromisoformat(str(text).replace("Z", "+00:00"))
        except ValueError:
            return None
        return moment if moment.tzinfo else moment.replace(tzinfo=timezone.utc)

    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    opening = {"LONG": ("BUY",), "SHORT": ("SELL_SHORT", "SELL")}
    snaps = cur.execute("SELECT s.id, s.position_id, s.snapshot_type, s.timestamp, s.raw_json, p.direction, p.status "
                        "FROM position_snapshots s JOIN positions p ON p.id = s.position_id "
                        "WHERE s.method IS NULL ORDER BY s.position_id, s.id").fetchall()
    for sid, pid, kind, stamp, raw_json, direction, _status in snaps:
        try:
            raw = json.loads(raw_json or "{}")
        except ValueError:
            raw = {}
        fills = cur.execute("SELECT id, side, executed_at FROM executions WHERE position_id = ? ORDER BY executed_at, id",
                            (pid,)).fetchall()
        anchor = None
        if kind == "ENTRY":
            anchor = next((f for f in fills if f[1] in opening[direction]), None)
        elif kind == "EXIT":
            closes = [f for f in fills if f[1] not in opening[direction]]
            anchor = closes[-1] if closes else None
        method = "END_OF_DAY" if raw.get("as_of") else "LIVE"
        captured = raw.get("taken_at") or stamp
        cutoff = stamp if method == "END_OF_DAY" else captured
        executed = anchor[2] if anchor else None
        later = bool(executed and when(cutoff) and when(executed) and when(cutoff) > when(executed))
        if kind in ("PLAN", "REVIEW"):
            timing = "CONTEMPORANEOUS"
        elif method == "LIVE" and when(captured) and when(executed) and \
                abs((when(captured) - when(executed)).total_seconds()) <= 15 * 60:
            timing = "CONTEMPORANEOUS"
        else:
            timing = "RETROSPECTIVE"
        provenance = {"origin": "before 2.1.1", "inferred": True}
        if method == "END_OF_DAY":
            provenance["note"] = "Reconstructed at the fill date's close, which may be after the fill."
        cur.execute("UPDATE position_snapshots SET execution_id = ?, executed_at = ?, observation_cutoff = ?, "
                    "captured_at = ?, method = ?, capture_timing = ?, uses_later_data = ?, provenance_json = ? WHERE id = ?",
                    (anchor[0] if anchor else None, executed, cutoff, captured, method, timing, int(later),
                     json.dumps(provenance), sid))
    # One effective PLAN, ENTRY and EXIT per position: the first PLAN and ENTRY, the last EXIT.
    for kind, keep in (("PLAN", "MIN"), ("ENTRY", "MIN"), ("EXIT", "MAX")):
        cur.execute(f"UPDATE position_snapshots SET superseded_at = ?, superseded_reason = ? "
                    f"WHERE snapshot_type = ? AND superseded_at IS NULL AND id NOT IN ("
                    f"SELECT {keep}(id) FROM position_snapshots WHERE snapshot_type = ? AND superseded_at IS NULL "
                    f"GROUP BY position_id)",
                    (now, "a second snapshot of this kind from before 2.1.1", kind, kind))
    cur.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_snapshots_effective ON position_snapshots (position_id, snapshot_type) "
                "WHERE superseded_at IS NULL AND snapshot_type IN ('PLAN', 'ENTRY', 'EXIT')")


@migration(6, "financing adjustments (borrow fees, dividends, splits) and option lifecycle events")
def financing_and_option_events(cur: sqlite3.Cursor) -> None:
    """2.2: money that moves outside the fills (borrow fees on a short, dividends owed or received,
    other cash adjustments), stock splits, and how an option ended (expired, exercised) or was
    adjusted. None of it changes a fill: splits and contract adjustments are applied when the
    fills are replayed (positions.adjusted_fills), and an option event adds the closing fill it
    implies, linked here."""
    cur.execute("""CREATE TABLE IF NOT EXISTS position_adjustments (
        id            INTEGER PRIMARY KEY AUTOINCREMENT,
        position_id   INTEGER NOT NULL REFERENCES positions (id) ON DELETE CASCADE,
        kind          TEXT NOT NULL CHECK (kind IN ('BORROW_FEE', 'BORROW_RATE', 'DIVIDEND_OWED', 'DIVIDEND_RECEIVED',
                                                    'CASH_ADJUSTMENT', 'SPLIT')),
        amount        REAL NOT NULL DEFAULT 0,      -- effect on profit in dollars: negative is a cost
        rate_pct      REAL,                         -- yearly borrow rate, for BORROW_RATE (and BORROW_FEE if known)
        quantity      REAL,                         -- shares the fee or dividend is on
        per_share     REAL,                         -- dividend per share
        ratio         REAL,                         -- SPLIT: new shares per old share (2 for a 2-for-1)
        effective_at  TEXT NOT NULL,                -- ex-date, charge date or split date (UTC)
        period_start  TEXT,                         -- the days a borrow fee covers
        period_end    TEXT,
        source        TEXT NOT NULL DEFAULT 'MANUAL' CHECK (source IN ('MANUAL', 'IMPORT', 'ROBINHOOD')),
        external_id   TEXT,
        note          TEXT NOT NULL DEFAULT '',
        details_json  TEXT NOT NULL DEFAULT '{}',   -- e.g. the plan levels a split changed, before and after
        created_at    TEXT NOT NULL,
        voided_at     TEXT,                         -- a mistaken entry is voided with a reason, never deleted
        void_reason   TEXT
    )""")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_adjustments_position ON position_adjustments (position_id, effective_at)")
    cur.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_adjustments_external ON position_adjustments (source, external_id) "
                "WHERE external_id IS NOT NULL")
    cur.execute("""CREATE TABLE IF NOT EXISTS option_events (
        id                     INTEGER PRIMARY KEY AUTOINCREMENT,
        position_id            INTEGER NOT NULL REFERENCES positions (id) ON DELETE CASCADE,
        event                  TEXT NOT NULL CHECK (event IN ('EXPIRED_WORTHLESS', 'EXERCISED', 'CONTRACT_ADJUSTED')),
        quantity               REAL NOT NULL CHECK (quantity > 0),   -- contracts it applied to
        event_at               TEXT NOT NULL,
        underlying_price       REAL,                                 -- the stock price it was settled against
        underlying_price_source TEXT,
        settlement_price       REAL,                                 -- per share the option leg was closed at
        execution_id           INTEGER REFERENCES executions (id) ON DELETE SET NULL,
        resulting_position_id  INTEGER REFERENCES positions (id) ON DELETE SET NULL,
        resulting_execution_id INTEGER REFERENCES executions (id) ON DELETE SET NULL,
        previous_json          TEXT NOT NULL DEFAULT '{}',           -- the contract's terms before an adjustment
        new_json               TEXT NOT NULL DEFAULT '{}',
        reason                 TEXT NOT NULL DEFAULT '',
        created_at             TEXT NOT NULL
    )""")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_option_events_position ON option_events (position_id, event_at)")


@migration(7, "research runs: the inputs, data fingerprint and results of each backtest and study")
def research_runs(cur: sqlite3.Cursor) -> None:
    """2.3: each backtest, walk-forward, pattern-lab run and signal-history build is recorded with
    what produced it (runs.py), so a result can be reproduced and a change traced to the data
    or to the settings."""
    cur.execute("""CREATE TABLE IF NOT EXISTS research_runs (
        id             INTEGER PRIMARY KEY AUTOINCREMENT,
        kind           TEXT NOT NULL,                -- BACKTEST, BACKTEST_V2, WALK_FORWARD, WALK_FORWARD_V2, PATTERNS, SIGNAL_HISTORY
        run_at         TEXT NOT NULL,
        config_hash    TEXT NOT NULL,                -- strategy, parameters, symbols, dates, costs, seed, versions
        data_hash      TEXT NOT NULL,                -- the price history it read
        result_hash    TEXT NOT NULL,                -- its deterministic output
        fingerprint_json TEXT NOT NULL,
        summary_json   TEXT NOT NULL DEFAULT '{}'
    )""")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_research_runs_kind ON research_runs (kind, run_at)")


@migration(8, "database rules for positions and option legs, and a log of repairs")
def integrity_rules(cur: sqlite3.Cursor) -> None:
    """2.3: rules the database itself enforces on new writes (the tables already check fills'
    quantities, prices, fees, sides, statuses, directions and instrument types), and a record of
    every repair. SQLite cannot add CHECK constraints to existing tables without rebuilding them,
    so the new rules are triggers; they apply to inserts and updates from now on and do not
    touch existing rows (reconcile.py reports any that would break them)."""
    rules = {
        "positions": ("NEW.quantity IS NULL OR NEW.quantity <= 0", "a position's planned quantity must be above zero"),
        "positions_mult": ("NEW.multiplier IS NULL OR NEW.multiplier <= 0", "a position's multiplier must be above zero"),
        "option_contracts": ("NEW.strike IS NULL OR NEW.strike <= 0 OR NEW.multiplier IS NULL OR NEW.multiplier <= 0 "
                             "OR length(NEW.expiration) != 10", "an option leg needs a strike and multiplier above zero "
                                                                 "and an expiration like 2026-11-20"),
        "executions": ("NEW.executed_at IS NULL OR length(NEW.executed_at) < 19", "a fill needs a full time stamp"),
        "position_adjustments": ("NEW.kind = 'SPLIT' AND (NEW.ratio IS NULL OR NEW.ratio <= 0)",
                                 "a split needs a ratio above zero"),
    }
    targets = {"positions": "positions", "positions_mult": "positions", "option_contracts": "option_contracts",
               "executions": "executions", "position_adjustments": "position_adjustments"}
    for name, (condition, message) in rules.items():
        table = targets[name]
        quoted = message.replace("'", "''")
        # An update is refused only when it makes a good row bad, so a row that broke a rule
        # before 2.3 can still have its other figures recomputed (it is reported, not frozen).
        was_bad = condition.replace("NEW.", "OLD.")
        for op, when in (("INSERT", condition), ("UPDATE", f"({condition}) AND NOT ({was_bad})")):
            cur.execute(f"CREATE TRIGGER IF NOT EXISTS check_{name}_{op.lower()} BEFORE {op} ON {table} "
                        f"WHEN {when} BEGIN SELECT RAISE(ABORT, '{quoted}'); END")
    cur.execute("""CREATE TABLE IF NOT EXISTS maintenance_log (
        id           INTEGER PRIMARY KEY AUTOINCREMENT,
        action       TEXT NOT NULL,                 -- REPAIR, BACKFILL
        at           TEXT NOT NULL,
        backup_path  TEXT,                          -- the copy taken first
        details_json TEXT NOT NULL DEFAULT '{}'
    )""")


@migration(9, "one database rule per option-leg field")
def option_leg_rules(cur: sqlite3.Cursor) -> None:
    """2.3.1: migration 8 checked an option leg's strike, multiplier and expiration in one rule, and
    an update is refused only when it makes a good row bad. So a leg written before 2.3 with a
    malformed expiration could still have its strike set to a negative number. Each field now has
    its own rule (migration 8 itself is left as shipped)."""
    for op in ("insert", "update"):
        cur.execute(f"DROP TRIGGER IF EXISTS check_option_contracts_{op}")
    rules = {
        "strike": ("NEW.strike IS NULL OR NEW.strike <= 0", "an option leg needs a strike above zero"),
        "multiplier": ("NEW.multiplier IS NULL OR NEW.multiplier <= 0", "an option leg needs a multiplier above zero"),
        "expiration": ("NEW.expiration IS NULL OR length(NEW.expiration) != 10",
                       "an option leg needs an expiration like 2026-11-20"),
    }
    for name, (condition, message) in rules.items():
        quoted = message.replace("'", "''")
        was_bad = condition.replace("NEW.", "OLD.")
        for op, when in (("INSERT", condition), ("UPDATE", f"({condition}) AND NOT ({was_bad})")):
            cur.execute(f"CREATE TRIGGER IF NOT EXISTS check_option_{name}_{op.lower()} BEFORE {op} ON option_contracts "
                        f"WHEN {when} BEGIN SELECT RAISE(ABORT, '{quoted}'); END")


@migration(10, "a record of AI requests, for the daily limit and the health screen")
def ai_usage(cur: sqlite3.Cursor) -> None:
    """2.4: one row per request to the AI model: when, what for, the model, tokens and web
    searches used, and whether it worked. No prompt, answer or key is stored."""
    cur.execute("""CREATE TABLE IF NOT EXISTS ai_usage (
        id            INTEGER PRIMARY KEY AUTOINCREMENT,
        at            TEXT NOT NULL,
        day           TEXT NOT NULL,               -- New York date, for the daily limit
        purpose       TEXT NOT NULL,
        model         TEXT NOT NULL,
        input_tokens  INTEGER NOT NULL DEFAULT 0,
        output_tokens INTEGER NOT NULL DEFAULT 0,
        web_searches  INTEGER NOT NULL DEFAULT 0,
        ok            INTEGER NOT NULL,
        error         TEXT
    )""")
    cur.execute("CREATE INDEX IF NOT EXISTS ai_usage_day ON ai_usage (day)")


@migration(11, "your broker's trade history, imported read-only")
def broker_history(cur: sqlite3.Cursor) -> None:
    """2.6: the rows of Robinhood's account activity report (or the Robinhood connection's order history)
    that are trades or change a holding: buys and sells of shares, options opened and closed, expirations,
    assignments, exercises and splits. Deposits, transfers, dividends and fees are counted at import but
    not kept. Each row keeps its original columns so every figure can be checked against the statement.
    Kept on this computer only; nothing here is sent anywhere."""
    cur.execute("""CREATE TABLE IF NOT EXISTS broker_imports (
        id              INTEGER PRIMARY KEY AUTOINCREMENT,
        source          TEXT NOT NULL,             -- robinhood_csv, robinhood_link or sample
        filename        TEXT,
        imported_at     TEXT NOT NULL,
        rows_read       INTEGER NOT NULL DEFAULT 0,
        rows_added      INTEGER NOT NULL DEFAULT 0,
        rows_duplicate  INTEGER NOT NULL DEFAULT 0,
        rows_ignored    INTEGER NOT NULL DEFAULT 0,
        rows_unreadable INTEGER NOT NULL DEFAULT 0,
        first_date      TEXT,
        last_date       TEXT,
        summary_json    TEXT
    )""")
    cur.execute("""CREATE TABLE IF NOT EXISTS broker_activity (
        id            INTEGER PRIMARY KEY AUTOINCREMENT,
        import_id     INTEGER NOT NULL REFERENCES broker_imports(id) ON DELETE CASCADE,
        source        TEXT NOT NULL,
        row_key       TEXT NOT NULL UNIQUE,        -- the row's own content, so the same row is kept once
        file_order    INTEGER NOT NULL,            -- position in its file (the report lists newest first)
        activity_date TEXT NOT NULL,
        process_date  TEXT,
        settle_date   TEXT,
        code          TEXT NOT NULL,               -- Buy, Sell, BTO, STC, STO, BTC, OEXP, OASGN, OEXCS, SPL
        symbol        TEXT,
        description   TEXT,
        kind          TEXT NOT NULL CHECK (kind IN ('STOCK', 'OPTION')),
        option_type   TEXT CHECK (option_type IN ('CALL', 'PUT') OR option_type IS NULL),
        strike        REAL,
        expiration    TEXT,
        quantity      REAL,
        short_flag    INTEGER NOT NULL DEFAULT 0,
        price         REAL,
        amount        REAL,
        raw_json      TEXT NOT NULL
    )""")
    cur.execute("CREATE INDEX IF NOT EXISTS broker_activity_date ON broker_activity (activity_date)")
    cur.execute("CREATE INDEX IF NOT EXISTS broker_activity_symbol ON broker_activity (symbol)")


@migration(12, "which imports each history row came in")
def broker_import_rows(cur: sqlite3.Cursor) -> None:
    """2.6: a row can arrive in more than one report (they overlap). Each import's rows are listed here, so
    removing one import keeps the rows another import also brought."""
    cur.execute("""CREATE TABLE IF NOT EXISTS broker_import_rows (
        import_id   INTEGER NOT NULL REFERENCES broker_imports(id) ON DELETE CASCADE,
        activity_id INTEGER NOT NULL REFERENCES broker_activity(id) ON DELETE CASCADE,
        PRIMARY KEY (import_id, activity_id)
    )""")
    cur.execute("INSERT OR IGNORE INTO broker_import_rows (import_id, activity_id) SELECT import_id, id FROM broker_activity")


@migration(13, "the morning plan's picks and watches")
def morning_tables(cur: sqlite3.Cursor) -> None:
    """2.8: the trade cards the morning plan suggested (so a scorecard can grade them later from price
    history alone) and the price triggers being watched. Records only; nothing here can place an order."""
    cur.execute("""CREATE TABLE IF NOT EXISTS morning_picks (
        id           INTEGER PRIMARY KEY AUTOINCREMENT,
        day          TEXT NOT NULL,
        symbol       TEXT NOT NULL,
        setup        TEXT NOT NULL,                 -- fade, ride or reversal
        direction    TEXT NOT NULL CHECK (direction IN ('LONG', 'SHORT')),
        entry        REAL NOT NULL,
        stop         REAL NOT NULL,
        target_1     REAL NOT NULL,
        target_2     REAL,
        horizon_days INTEGER NOT NULL,
        instrument   TEXT NOT NULL,                 -- SHARES, CALL or PUT
        option_json  TEXT,
        limit_price  REAL,
        contracts    INTEGER,
        shares       INTEGER,
        created_at   TEXT NOT NULL,
        source       TEXT NOT NULL,                 -- email or screen
        UNIQUE (day, symbol, setup)
    )""")
    cur.execute("""CREATE TABLE IF NOT EXISTS morning_watches (
        id              INTEGER PRIMARY KEY AUTOINCREMENT,
        symbol          TEXT NOT NULL,
        direction       TEXT NOT NULL CHECK (direction IN ('LONG', 'SHORT')),
        kind            TEXT NOT NULL CHECK (kind IN ('above', 'below')),
        level           REAL NOT NULL,
        why             TEXT,
        created_at      TEXT NOT NULL,
        active          INTEGER NOT NULL DEFAULT 1,
        triggered_at    TEXT,
        triggered_price REAL,
        emailed         INTEGER NOT NULL DEFAULT 0
    )""")


@migration(14, "live news items and the price signals they set off")
def live_news_tables(cur: sqlite3.Cursor) -> None:
    """2.10: stock-moving news as it arrived (press releases, 8-K filings, headlines) and, per stock, what the
    price did after it: watched, turned into a trade card, or no reaction. Records only; nothing here can place
    an order. Kept for a week (livenews.KEEP_DAYS)."""
    cur.execute("""CREATE TABLE IF NOT EXISTS live_news (
        id           INTEGER PRIMARY KEY AUTOINCREMENT,
        source       TEXT NOT NULL,                 -- RTPR, SEC EDGAR, Finnhub or Demo
        external_id  TEXT NOT NULL,                 -- the source's own id, so an item is kept once
        symbols      TEXT NOT NULL DEFAULT '[]',    -- JSON list of tickers
        headline     TEXT NOT NULL,
        summary      TEXT NOT NULL DEFAULT '',
        url          TEXT,
        kind         TEXT NOT NULL DEFAULT 'news',  -- press_release, filing or news
        published_at TEXT,
        received_at  TEXT NOT NULL,
        tone         REAL,
        extra_json   TEXT,                          -- impact score, rule names, filing items
        UNIQUE (source, external_id)
    )""")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_live_news_received ON live_news (received_at)")
    cur.execute("""CREATE TABLE IF NOT EXISTS live_signals (
        id            INTEGER PRIMARY KEY AUTOINCREMENT,
        symbol        TEXT NOT NULL,
        news_id       INTEGER NOT NULL REFERENCES live_news (id) ON DELETE CASCADE,
        news_ids      TEXT NOT NULL DEFAULT '[]',   -- later items on the same stock that joined this signal
        opened_at     TEXT NOT NULL,
        state         TEXT NOT NULL CHECK (state IN ('waiting', 'watching', 'opportunity', 'no_reaction',
                                                      'stopped', 'done', 'skipped')),
        direction     TEXT CHECK (direction IN ('LONG', 'SHORT')),
        path          TEXT,                         -- reaction or day_move
        opportunity_at TEXT,                        -- when the move confirmed; it is watched for an hour after
        ref_price     REAL,                         -- the stock when the item arrived
        ref_at        TEXT,
        prev_close    REAL,
        atr           REAL,
        last_price    REAL,
        last_at       TEXT,
        checks        INTEGER NOT NULL DEFAULT 0,
        checks_json   TEXT NOT NULL DEFAULT '[]',
        next_check_at TEXT,
        card_json     TEXT,
        holdings_json TEXT,
        reason        TEXT,
        emailed       INTEGER NOT NULL DEFAULT 0,
        updated_at    TEXT NOT NULL
    )""")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_live_signals_symbol ON live_signals (symbol, opened_at)")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_live_signals_due ON live_signals (state, next_check_at)")
