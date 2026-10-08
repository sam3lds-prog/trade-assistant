"""Copies of the database file, taken before anything changes stored records.

The app keeps every position, fill and journal entry in one SQLite file, so a copy of that
file is a complete way back. A copy is taken:

- before a schema migration runs on an existing database (db.py), and
- before a repair rewrites any stored figure (reconcile.py).

Copies use SQLite's online backup, which is consistent even while the app has the file open
in WAL mode, and are written beside the database in `backups/` unless told otherwise. Old
automatic copies are pruned so the folder does not grow without limit; copies made by hand
are never removed.
"""

from __future__ import annotations

import re
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

KEEP_AUTOMATIC = 10            # newest automatic copies kept per kind
SAFE_LABEL = re.compile(r"[^a-z0-9_-]+")


def backup_connection(conn: sqlite3.Connection, dest: Path) -> Path:
    """Write a consistent copy of the database behind `conn` to `dest` and return the path."""
    dest = Path(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    if dest.exists():
        raise FileExistsError(f"{dest} already exists")
    target = sqlite3.connect(dest)
    try:
        conn.backup(target)
    finally:
        target.close()
    return dest


def backup_path(db_path: Path, label: str, folder: Path | None = None) -> Path:
    """Where a new copy of `db_path` goes: <folder>/<name>-<label>-<UTC time>.db."""
    db_path = Path(db_path)
    folder = Path(folder) if folder is not None else db_path.parent / "backups"
    clean = SAFE_LABEL.sub("-", label.lower()).strip("-") or "manual"
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    return folder / f"{db_path.stem}-{clean}-{stamp}.db"


def prune(folder: Path, db_stem: str, prefix: str, keep: int = KEEP_AUTOMATIC) -> list[Path]:
    """Remove all but the newest `keep` copies whose label starts with `prefix`. Returns what was
    removed. Copies are named with a sortable UTC time, so name order is age order within a label."""
    clean = SAFE_LABEL.sub("-", prefix.lower()).strip("-")
    copies = sorted(Path(folder).glob(f"{db_stem}-{clean}*.db"), key=lambda p: p.name.rsplit("-", 1)[-1])
    removed = copies[:-keep] if keep > 0 else copies
    for p in removed:
        p.unlink(missing_ok=True)
    return removed


__all__ = ["backup_connection", "backup_path", "prune", "KEEP_AUTOMATIC"]
