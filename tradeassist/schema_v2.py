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


@migration(4, "signal occurrences: the stock's daily range as a share of its price on the day")
def signal_atr_pct(cur: sqlite3.Cursor) -> None:
    # Lets past cases from a calm stock be compared with a volatile one (options/reversal_score.py).
    columns = {row[1] for row in cur.execute("PRAGMA table_info(signal_occurrences)").fetchall()}
    if "atr_pct" not in columns:
        cur.execute("ALTER TABLE signal_occurrences ADD COLUMN atr_pct REAL")
