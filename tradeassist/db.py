"""SQLite storage: price bars, volatility history, cached lookups, study results, journal."""

from __future__ import annotations

import json
import sqlite3
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

SCHEMA = """
CREATE TABLE IF NOT EXISTS bars (
    symbol TEXT NOT NULL,
    date   TEXT NOT NULL,
    open   REAL NOT NULL,
    high   REAL NOT NULL,
    low    REAL NOT NULL,
    close  REAL NOT NULL,
    volume REAL NOT NULL,
    PRIMARY KEY (symbol, date)
);
CREATE TABLE IF NOT EXISTS bars_meta (
    symbol     TEXT PRIMARY KEY,
    source     TEXT NOT NULL,
    first_date TEXT NOT NULL,
    last_date  TEXT NOT NULL,
    fetched_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS iv_history (
    symbol TEXT NOT NULL,
    date   TEXT NOT NULL,
    atm_iv REAL NOT NULL,
    hv20   REAL,
    spot   REAL,
    PRIMARY KEY (symbol, date)
);
CREATE TABLE IF NOT EXISTS kv_cache (
    key        TEXT PRIMARY KEY,
    value      TEXT NOT NULL,
    fetched_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS pattern_results (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id      TEXT NOT NULL,
    run_at      TEXT NOT NULL,
    source      TEXT NOT NULL,
    pattern     TEXT NOT NULL,
    horizon     INTEGER NOT NULL,
    verdict     TEXT NOT NULL,
    result_json TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_pattern_results_run ON pattern_results (run_id);
CREATE TABLE IF NOT EXISTS trades (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at     TEXT NOT NULL,
    data_source    TEXT NOT NULL,
    symbol         TEXT NOT NULL,
    option_symbol  TEXT NOT NULL,
    option_type    TEXT NOT NULL,
    strike         REAL NOT NULL,
    expiration     TEXT NOT NULL,
    contracts      INTEGER NOT NULL,
    thesis         TEXT NOT NULL DEFAULT '',
    status         TEXT NOT NULL,            -- planned | open | closed | cancelled
    planned_limit  REAL,
    max_limit      REAL,
    plan_json      TEXT NOT NULL DEFAULT '{}',
    quote_json     TEXT NOT NULL DEFAULT '{}',
    scorecard_json TEXT NOT NULL DEFAULT '[]',
    signals_json   TEXT NOT NULL DEFAULT '[]',
    overrides_json TEXT NOT NULL DEFAULT '[]',
    entry_price    REAL,
    entry_at       TEXT,
    exit_price     REAL,
    exit_at        TEXT,
    pnl            REAL,
    notes          TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS attempts (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    trade_id        INTEGER NOT NULL REFERENCES trades (id) ON DELETE CASCADE,
    at              TEXT NOT NULL,
    side            TEXT NOT NULL,           -- buy | sell
    bid             REAL NOT NULL,
    ask             REAL NOT NULL,
    limit_price     REAL NOT NULL,
    position        REAL NOT NULL,           -- 0 = passive side of the spread, 1 = crossing it
    filled          INTEGER NOT NULL,
    seconds_to_fill REAL
);
"""


class Database:
    """Small wrapper over sqlite3. One connection, guarded by a lock; fine for a single-user app."""

    def __init__(self, path: str | Path):
        self.path = str(path)
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        existed = self.path != ":memory:" and Path(self.path).is_file() and Path(self.path).stat().st_size > 0
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(self.path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA foreign_keys = ON")
        if self.path != ":memory:":
            self._conn.execute("PRAGMA journal_mode = WAL")
        self.backup_before_migration: Path | None = None
        try:
            if existed:
                self.backup_before_migration = self._backup_if_migrations_pending()
            with self.tx() as cur:
                cur.executescript(SCHEMA)
            from .migrations import migrate
            with self._lock:
                # Autocommit while migrating, so each migration can manage its own transaction.
                previous, self._conn.isolation_level = self._conn.isolation_level, None
                try:
                    self.migrated = migrate(self._conn)
                finally:
                    self._conn.isolation_level = previous
            if self.migrated:
                from .events import emit
                emit("migration_applied", versions=",".join(map(str, self.migrated)),
                     backup=self.backup_before_migration.name if self.backup_before_migration else None)
        except BaseException as exc:
            self._conn.close()               # a failed open must not leave the file held open
            if isinstance(exc, Exception):
                import logging as _logging
                from .events import emit
                emit("migration_failed", _logging.ERROR, error=exc,
                     backup=self.backup_before_migration.name if self.backup_before_migration else None)
            raise

    def schema_version(self) -> int:
        row = self.one("SELECT MAX(version) AS v FROM schema_migrations")
        return int(row["v"] or 0)

    def _backup_if_migrations_pending(self) -> Path | None:
        """Copy an existing database file before migrations change it (backup.py)."""
        from . import schema_v2  # noqa: F401 - registers the migrations
        from .backup import backup_connection, backup_path, prune
        from .migrations import MIGRATIONS
        has_table = self._conn.execute("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'schema_migrations'"
                                       ).fetchone() is not None
        done = {r[0] for r in self._conn.execute("SELECT version FROM schema_migrations")} if has_table else set()
        pending = [m.version for m in MIGRATIONS if m.version not in done]
        if not pending:
            return None
        label = f"before-migration-{pending[0]}"
        dest = backup_path(Path(self.path), label)
        backup_connection(self._conn, dest)
        prune(dest.parent, Path(self.path).stem, "before-migration")
        return dest

    def backup(self, label: str = "manual", folder: str | Path | None = None) -> Path:
        """A consistent copy of the whole database, written to `folder` (default: backups/ beside it)."""
        from .backup import backup_connection, backup_path
        if self.path == ":memory:" and folder is None:
            raise ValueError("An in-memory database needs a folder to back up to")
        base = Path(self.path) if self.path != ":memory:" else Path(folder) / "memory.db"
        with self._lock:
            self._conn.commit()
            return backup_connection(self._conn, backup_path(base, label, Path(folder) if folder else None))

    @contextmanager
    def tx(self) -> Iterator[sqlite3.Cursor]:
        with self._lock:
            cur = self._conn.cursor()
            try:
                yield cur
                self._conn.commit()
            except Exception:
                self._conn.rollback()
                raise
            finally:
                cur.close()

    def query(self, sql: str, params: tuple | list = ()) -> list[sqlite3.Row]:
        with self._lock:
            return self._conn.execute(sql, params).fetchall()

    def one(self, sql: str, params: tuple | list = ()) -> sqlite3.Row | None:
        with self._lock:
            return self._conn.execute(sql, params).fetchone()

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    # --- key/value cache -------------------------------------------------
    def cache_get(self, key: str, max_age_seconds: float | None = None) -> Any | None:
        row = self.one("SELECT value, fetched_at FROM kv_cache WHERE key = ?", (key,))
        if row is None:
            return None
        if max_age_seconds is not None and time.time() - row["fetched_at"] > max_age_seconds:
            return None
        return json.loads(row["value"])

    def cache_put(self, key: str, value: Any) -> None:
        with self.tx() as cur:
            cur.execute(
                "INSERT INTO kv_cache (key, value, fetched_at) VALUES (?, ?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value, fetched_at = excluded.fetched_at",
                (key, json.dumps(value), time.time()),
            )
