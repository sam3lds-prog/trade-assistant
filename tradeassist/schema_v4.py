"""4.0 migrations of the trading database. Same rules as schema_v2.py and schema_v3.py: never edit a shipped migration;
each must be safe on any older database; one statement per execute.

The replay engine's own records live in data/replay.db (intelligence/store.py), not here.
"""

from __future__ import annotations

import sqlite3

from .migrations import migration


@migration(22, "4.0 Model Lab: saved layouts may belong to the fifth workspace")
def lab_workspace(cur: sqlite3.Cursor) -> None:
    """SQLite cannot change a CHECK constraint in place: the table is rebuilt with 'lab' allowed and every saved
    layout copied across unchanged."""
    cur.execute("""CREATE TABLE workspace_layouts_v4 (
        id          TEXT PRIMARY KEY,
        workspace   TEXT NOT NULL CHECK (workspace IN ('discover', 'research', 'desk', 'portfolio', 'lab')),
        name        TEXT NOT NULL,
        layout_json TEXT NOT NULL,
        state_json  TEXT NOT NULL DEFAULT '{}',
        is_default  INTEGER NOT NULL DEFAULT 0,
        created_at  TEXT NOT NULL,
        updated_at  TEXT NOT NULL,
        UNIQUE (workspace, name)
    )""")
    cur.execute("""INSERT INTO workspace_layouts_v4 (id, workspace, name, layout_json, state_json, is_default, created_at, updated_at)
                   SELECT id, workspace, name, layout_json, state_json, is_default, created_at, updated_at FROM workspace_layouts""")
    cur.execute("DROP TABLE workspace_layouts")
    cur.execute("ALTER TABLE workspace_layouts_v4 RENAME TO workspace_layouts")
