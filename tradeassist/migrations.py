"""Versioned schema migrations for the SQLite database.

The app stores everything in one SQLite file on your computer, so this is a deliberately
small migration framework rather than Alembic (which would bring SQLAlchemy and an ORM
layer the app does not otherwise use). Each migration has a version number, a name and a
function that changes the schema; applied versions are recorded in `schema_migrations`, and
every migration runs inside a transaction, so a failure leaves the file as it was.

Rules for adding one: never edit a migration that has shipped; add a new one with the next
number. Migrations must be safe to run on a database created by any earlier version.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Callable


@dataclass(frozen=True)
class Migration:
    version: int
    name: str
    apply: Callable[[sqlite3.Cursor], None]


MIGRATIONS: list[Migration] = []


def migration(version: int, name: str):
    def register(fn: Callable[[sqlite3.Cursor], None]) -> Callable[[sqlite3.Cursor], None]:
        if any(m.version == version for m in MIGRATIONS):
            raise ValueError(f"Migration {version} is defined twice")
        MIGRATIONS.append(Migration(version, name, fn))
        MIGRATIONS.sort(key=lambda m: m.version)
        return fn
    return register


def applied_versions(conn: sqlite3.Connection) -> list[int]:
    conn.execute("CREATE TABLE IF NOT EXISTS schema_migrations (version INTEGER PRIMARY KEY, name TEXT NOT NULL, "
                 "applied_at TEXT NOT NULL)")
    return [r[0] for r in conn.execute("SELECT version FROM schema_migrations ORDER BY version")]


def migrate(conn: sqlite3.Connection) -> list[int]:
    """Apply every migration not yet recorded, in order. Returns the versions applied now."""
    from . import schema_v2  # noqa: F401 - registers the migrations
    done = set(applied_versions(conn))
    conn.commit()
    ran = []
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
