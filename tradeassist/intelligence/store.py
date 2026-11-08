"""data/replay.db: the frozen datasets, the replay runs and their prediction ledgers (4.0).

A file of its own, apart from the trading database (data/tradeassist.db): a ten-year replay writes about half a
million predictions, and the trading database is copied to data/backups/ before every migration. Deleting
replay.db loses replays and nothing else; the app's trading records never live here.

What may never change is refused by the database itself, not by convention: a dataset's bars, a prediction, its
outcome, a take/pass decision, a model version and a training decision each have triggers that abort any UPDATE
or DELETE. Only a run's progress (status, checkpoint, heartbeat) and the artifact of each task's current model
(`run_state`) are rewritten as a run goes on.

Migrations follow the app's rules (migrations.py): never edit one that has shipped, add the next number, each runs
in its own transaction.
"""

from __future__ import annotations

import sqlite3
import threading
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Iterator

BUSY_SECONDS = 15.0
IMMUTABLE = ("datasets", "dataset_bars", "model_versions", "training_runs", "predictions", "outcomes", "trade_decisions")


@dataclass(frozen=True)
class ReplayMigration:
    version: int
    name: str
    apply: Callable[[sqlite3.Cursor], None]


MIGRATIONS: list[ReplayMigration] = []


def migration(version: int, name: str):
    def register(fn: Callable[[sqlite3.Cursor], None]) -> Callable[[sqlite3.Cursor], None]:
        if any(m.version == version for m in MIGRATIONS):
            raise ValueError(f"Replay migration {version} is defined twice")
        MIGRATIONS.append(ReplayMigration(version, name, fn))
        MIGRATIONS.sort(key=lambda m: m.version)
        return fn
    return register


def _freeze(cur: sqlite3.Cursor, table: str) -> None:
    for verb in ("UPDATE", "DELETE"):
        cur.execute(f"""CREATE TRIGGER IF NOT EXISTS {table}_no_{verb.lower()} BEFORE {verb} ON {table}
                        BEGIN SELECT RAISE(ABORT, '{table} rows are immutable: a replay record is never changed or deleted'); END""")


@migration(1, "4.0 datasets, replay runs, the prediction ledger and the model registry")
def initial(cur: sqlite3.Cursor) -> None:
    cur.execute("""CREATE TABLE IF NOT EXISTS datasets (
        id              TEXT PRIMARY KEY,          -- 'ds-' + the first 16 hex of the content hash
        created_at      TEXT NOT NULL,
        source          TEXT NOT NULL,             -- the data mode it was fetched in: live, sandbox, robinhood, demo
        adjustment      TEXT NOT NULL,             -- what the source says of splits and dividends
        universe        TEXT NOT NULL,
        first_date      TEXT NOT NULL,
        last_date       TEXT NOT NULL,
        symbols_json    TEXT NOT NULL,
        sessions        INTEGER NOT NULL,
        bars            INTEGER NOT NULL,
        content_sha256  TEXT NOT NULL UNIQUE,
        spec_json       TEXT NOT NULL
    )""")
    cur.execute("""CREATE TABLE IF NOT EXISTS dataset_bars (
        dataset_id  TEXT NOT NULL REFERENCES datasets (id),
        symbol      TEXT NOT NULL,
        date        TEXT NOT NULL,
        open        REAL NOT NULL,
        high        REAL NOT NULL,
        low         REAL NOT NULL,
        close       REAL NOT NULL,
        volume      REAL NOT NULL,
        PRIMARY KEY (dataset_id, symbol, date)
    ) WITHOUT ROWID""")
    cur.execute("""CREATE TABLE IF NOT EXISTS dataset_checks (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        dataset_id  TEXT NOT NULL REFERENCES datasets (id),
        symbol      TEXT,
        level       TEXT NOT NULL CHECK (level IN ('info', 'warn', 'fail')),
        code        TEXT NOT NULL,
        text        TEXT NOT NULL
    )""")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_dataset_checks ON dataset_checks (dataset_id, level)")
    cur.execute("""CREATE TABLE IF NOT EXISTS replay_runs (
        id             TEXT PRIMARY KEY,
        kind           TEXT NOT NULL CHECK (kind IN ('development', 'holdout', 'shadow')),
        dataset_id     TEXT REFERENCES datasets (id),
        parent_run     TEXT REFERENCES replay_runs (id),
        created_at     TEXT NOT NULL,
        config_json    TEXT NOT NULL,
        config_sha256  TEXT NOT NULL,
        status         TEXT NOT NULL CHECK (status IN ('queued', 'running', 'paused', 'complete', 'failed')),
        first_session  TEXT NOT NULL,
        last_session   TEXT NOT NULL,
        checkpoint     TEXT,                       -- the last session fully written
        started_at     TEXT,
        finished_at    TEXT,
        worker         TEXT,
        heartbeat_at   TEXT,
        ledger_sha256  TEXT,
        summary_json   TEXT,
        error          TEXT
    )""")
    cur.execute("""CREATE TABLE IF NOT EXISTS model_versions (
        run_id           TEXT NOT NULL REFERENCES replay_runs (id),
        id               TEXT NOT NULL,
        task             TEXT NOT NULL CHECK (task IN ('daily', 'trade')),
        horizon          INTEGER NOT NULL CHECK (horizon > 0),
        family           TEXT NOT NULL,
        trained_through  TEXT,                     -- the training cutoff: no label ending after it was used
        train_rows       INTEGER NOT NULL DEFAULT 0,
        params_json      TEXT NOT NULL DEFAULT '{}',
        artifact_sha256  TEXT,
        created_session  TEXT NOT NULL,
        PRIMARY KEY (run_id, id)
    )""")
    cur.execute("""CREATE TABLE IF NOT EXISTS training_runs (
        id               INTEGER PRIMARY KEY AUTOINCREMENT,
        run_id           TEXT NOT NULL REFERENCES replay_runs (id),
        session          TEXT NOT NULL,
        task             TEXT NOT NULL,
        horizon          INTEGER NOT NULL,
        champion_before  TEXT NOT NULL,
        champion_after   TEXT NOT NULL,
        decision         TEXT NOT NULL CHECK (decision IN ('promoted', 'kept', 'insufficient_data')),
        reason           TEXT NOT NULL,
        candidates_json  TEXT NOT NULL,
        UNIQUE (run_id, session, task, horizon)
    )""")
    cur.execute("""CREATE TABLE IF NOT EXISTS run_state (
        run_id           TEXT NOT NULL REFERENCES replay_runs (id),
        task             TEXT NOT NULL,
        horizon          INTEGER NOT NULL,
        model_version    TEXT NOT NULL,
        family           TEXT NOT NULL,
        artifact         BLOB,
        artifact_sha256  TEXT,
        updated_session  TEXT NOT NULL,
        PRIMARY KEY (run_id, task, horizon)
    )""")
    cur.execute("""CREATE TABLE IF NOT EXISTS predictions (
        id               INTEGER PRIMARY KEY AUTOINCREMENT,
        run_id           TEXT NOT NULL REFERENCES replay_runs (id),
        task             TEXT NOT NULL CHECK (task IN ('daily', 'trade')),
        horizon          INTEGER NOT NULL CHECK (horizon > 0),
        symbol           TEXT NOT NULL,
        session          TEXT NOT NULL,            -- made after this session's close, from bars through it
        cutoff_at        TEXT NOT NULL,            -- the moment it stands for (New York time)
        target_session   TEXT NOT NULL,            -- its window ends at this session's close
        model_version    TEXT NOT NULL,
        family           TEXT NOT NULL,
        last_close       REAL NOT NULL,
        pred_return      REAL,                     -- daily: the expected simple return to the target close
        low_return       REAL,                     -- daily: the band's 10th percentile
        high_return      REAL,                     -- daily: the band's 90th percentile
        p_up             REAL,
        sigma            REAL,
        setup            TEXT,                     -- trade: the setup that qualified
        p_target         REAL,
        p_stop           REAL,
        p_timeout        REAL,
        expected_return  REAL,                     -- trade: the expected net return after costs
        regime           TEXT,
        payload_json     TEXT NOT NULL DEFAULT '{}',
        row_sha256       TEXT NOT NULL,
        UNIQUE (run_id, task, horizon, symbol, session)
    )""")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_predictions_due ON predictions (run_id, target_session)")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_predictions_session ON predictions (run_id, session)")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_predictions_symbol ON predictions (run_id, symbol, session)")
    cur.execute("""CREATE TABLE IF NOT EXISTS outcomes (
        prediction_id    INTEGER PRIMARY KEY REFERENCES predictions (id),
        run_id           TEXT NOT NULL REFERENCES replay_runs (id),
        matured_session  TEXT NOT NULL,
        status           TEXT NOT NULL CHECK (status IN ('graded', 'no_data')),
        realized_return  REAL,
        abs_error        REAL,
        direction_hit    INTEGER,                  -- null when the prediction did not lean
        covered          INTEGER,
        barrier          TEXT CHECK (barrier IS NULL OR barrier IN ('target', 'stop', 'timeout', 'ambiguous')),
        exit_session     TEXT,
        gross_return     REAL,
        net_return       REAL,
        payload_json     TEXT NOT NULL DEFAULT '{}'
    )""")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_outcomes_run ON outcomes (run_id, matured_session)")
    cur.execute("""CREATE TABLE IF NOT EXISTS trade_decisions (
        prediction_id  INTEGER PRIMARY KEY REFERENCES predictions (id),
        run_id         TEXT NOT NULL REFERENCES replay_runs (id),
        session        TEXT NOT NULL,
        symbol         TEXT NOT NULL,
        take           INTEGER NOT NULL CHECK (take IN (0, 1)),
        reason         TEXT NOT NULL,
        gate_json      TEXT NOT NULL DEFAULT '{}'
    )""")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_trade_decisions ON trade_decisions (run_id, session)")
    for table in IMMUTABLE:
        _freeze(cur, table)


def applied_versions(conn: sqlite3.Connection) -> list[int]:
    conn.execute("CREATE TABLE IF NOT EXISTS schema_migrations (version INTEGER PRIMARY KEY, name TEXT NOT NULL, applied_at TEXT NOT NULL)")
    return [r[0] for r in conn.execute("SELECT version FROM schema_migrations ORDER BY version")]


def migrate(conn: sqlite3.Connection) -> list[int]:
    from . import store_migrations  # noqa: F401 - registers the later migrations
    done = set(applied_versions(conn))
    conn.commit()
    ran: list[int] = []
    for m in MIGRATIONS:
        if m.version in done:
            continue
        cur = conn.cursor()
        try:
            cur.execute("BEGIN")
            m.apply(cur)
            cur.execute("INSERT INTO schema_migrations (version, name, applied_at) VALUES (?, ?, ?)",
                        (m.version, m.name, datetime.now(timezone.utc).isoformat(timespec="seconds")))
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            cur.close()
        ran.append(m.version)
    return ran


class ReplayStore:
    """One connection to replay.db, guarded by a lock (the replay process and the app may both open it)."""

    def __init__(self, path: str | Path):
        self.path = str(path)
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(self.path, check_same_thread=False, timeout=BUSY_SECONDS, isolation_level=None)
        self._conn.row_factory = sqlite3.Row
        try:
            self._conn.execute("PRAGMA foreign_keys = ON")
            self._conn.execute(f"PRAGMA busy_timeout = {int(BUSY_SECONDS * 1000)}")
            if self.path != ":memory:":
                self._conn.execute("PRAGMA journal_mode = WAL")
            with self._lock:
                self.migrated = migrate(self._conn)
            self._conn.isolation_level = ""          # back to implicit transactions, committed by tx()
        except BaseException:
            self._conn.close()
            raise

    @contextmanager
    def tx(self) -> Iterator[sqlite3.Cursor]:
        with self._lock:
            cur = self._conn.cursor()
            try:
                yield cur
                self._conn.commit()
            except BaseException:
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

    def schema_version(self) -> int:
        row = self.one("SELECT MAX(version) AS v FROM schema_migrations")
        return int(row["v"] or 0)

    def close(self) -> None:
        with self._lock:
            self._conn.close()


def replay_path(settings) -> Path:
    """replay.db sits beside the trading database (data/ by default; wherever TRADEASSIST_DB points)."""
    return Path(settings.db_path).parent / "replay.db"


__all__ = ["ReplayStore", "replay_path", "migration", "MIGRATIONS", "IMMUTABLE"]
