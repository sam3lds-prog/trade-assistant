"""Migrations against real older databases.

The fixtures in tests/fixtures were written by the released code of each older version (see
make_legacy_dbs.py), so these tests open what a user's database actually looks like after
0.4.0, 2.0.0 and 2.1.0. For each one: a copy is backed up first; every migration applies;
nothing the user recorded (journal entries, limit attempts, fills, option legs, snapshots,
plans) is changed or lost; and opening it again changes nothing.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from tradeassist.backup import backup_path, prune
from tradeassist.db import Database
from tradeassist.migrations import MIGRATIONS

FIXTURES = Path(__file__).parent / "fixtures"
LEGACY = ["db_0_4_0", "db_2_0_0", "db_2_1_0"]
# Rows the user entered. Each is compared column by column on the columns the old version had.
RECORDS = {
    "trades": "id", "attempts": "id", "executions": "id", "option_contracts": "position_id, leg",
    "position_snapshots": "id",
}
# Plan and identity columns of a position; the rest is accounting worked out from the fills.
POSITION_FIELDS = ("id", "symbol", "instrument_type", "direction", "quantity", "multiplier", "thesis", "setup_type",
                   "intended_horizon", "invalidation_price", "target_1", "target_2", "risk_amount",
                   "initial_risk_per_unit", "data_source", "legacy_trade_id", "created_at")


def latest() -> int:
    from tradeassist import schema_v2  # noqa: F401 - registers the migrations
    return max(m.version for m in MIGRATIONS)


def load(name: str, folder: Path) -> Path:
    path = folder / f"{name}.db"
    conn = sqlite3.connect(path)
    conn.executescript((FIXTURES / f"{name}.sql").read_text())
    conn.close()
    return path


def table_rows(path: Path) -> dict[str, list[dict]]:
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    out = {}
    for table, order in {**RECORDS, "positions": "id"}.items():
        if conn.execute("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (table,)).fetchone():
            out[table] = [dict(r) for r in conn.execute(f"SELECT * FROM {table} ORDER BY {order}")]
    conn.close()
    return out


def assert_preserved(before: dict[str, list[dict]], after: dict[str, list[dict]]) -> None:
    for table in RECORDS:
        if table not in before:
            continue
        assert len(after[table]) == len(before[table]), table
        for old, new in zip(before[table], after[table]):
            assert {k: new[k] for k in old} == old, f"{table} row changed: {old} -> {new}"
    if "positions" in before:
        assert [r["id"] for r in after["positions"]] == [r["id"] for r in before["positions"]]
        for old, new in zip(before["positions"], after["positions"]):
            assert {k: new[k] for k in POSITION_FIELDS} == {k: old[k] for k in POSITION_FIELDS}


@pytest.mark.parametrize("name", LEGACY)
def test_a_legacy_database_migrates_without_losing_anything(name, tmp_path):
    path = load(name, tmp_path)
    before = table_rows(path)
    db = Database(path)
    # When migrations ran, a copy of the untouched file was taken first: the old database exactly.
    if db.migrated:
        assert db.backup_before_migration is not None and db.backup_before_migration.is_file()
        assert table_rows(db.backup_before_migration) == before
    else:
        assert db.backup_before_migration is None
    assert db.schema_version() == latest()
    after = table_rows(path)
    assert_preserved(before, after)
    if name == "db_0_4_0":                        # migration 1 mirrors every classic journal entry
        mirrors = db.query("SELECT legacy_trade_id, status FROM positions ORDER BY legacy_trade_id")
        assert [(r["legacy_trade_id"], r["status"]) for r in mirrors] == [
            (1, "PLANNED"), (2, "OPEN"), (3, "CLOSED"), (4, "CLOSED"), (5, "CANCELLED")]
    db.close()
    # Opening it again changes nothing and takes no second copy.
    again = Database(path)
    assert again.migrated == [] and again.backup_before_migration is None
    assert table_rows(path) == after
    again.close()


def test_a_new_database_has_every_table_and_takes_no_backup(tmp_path):
    db = Database(tmp_path / "new.db")
    assert db.backup_before_migration is None and db.schema_version() == latest()
    names = {r["name"] for r in db.query("SELECT name FROM sqlite_master WHERE type = 'table'")}
    assert {"trades", "attempts", "positions", "executions", "option_contracts", "position_snapshots",
            "signal_occurrences", "schema_migrations"} <= names
    assert not (tmp_path / "backups").exists()
    db.close()
    mem = Database(":memory:")
    assert mem.schema_version() == latest() and mem.backup_before_migration is None
    mem.close()


def test_foreign_keys_are_enforced_on_every_connection(tmp_path):
    db = Database(tmp_path / "fk.db")
    assert db.one("PRAGMA foreign_keys")[0] == 1
    with pytest.raises(sqlite3.IntegrityError):
        with db.tx() as cur:
            cur.execute("INSERT INTO executions (position_id, side, quantity, price, fees, executed_at, created_at) "
                        "VALUES (999, 'BUY', 1, 1, 0, '2026-01-01T00:00:00+00:00', '2026-01-01T00:00:00+00:00')")
    db.close()


def test_a_failed_migration_leaves_the_file_as_it_was(tmp_path, monkeypatch):
    from tradeassist import migrations
    path = load("db_2_1_0", tmp_path)
    before = table_rows(path)

    def broken(cur):
        cur.execute("UPDATE trades SET thesis = 'changed'")
        raise RuntimeError("boom")

    monkeypatch.setattr(migrations, "MIGRATIONS", [*migrations.MIGRATIONS,
                                                   migrations.Migration(latest() + 1, "broken", broken)])
    with pytest.raises(RuntimeError):
        Database(path)
    assert table_rows(path) == before


def test_manual_backups_and_pruning(tmp_path):
    db = Database(tmp_path / "live.db")
    with db.tx() as cur:
        cur.execute("INSERT INTO trades (created_at, data_source, symbol, option_symbol, option_type, strike, expiration, "
                    "contracts, status) VALUES ('2026-01-01', 'demo', 'AAPL', 'X', 'call', 1, '2026-12-18', 1, 'planned')")
    copy = db.backup("before repair")
    assert copy.parent == tmp_path / "backups" and "before-repair" in copy.name
    assert table_rows(copy)["trades"] == table_rows(tmp_path / "live.db")["trades"]
    db.close()
    mem = Database(":memory:")
    with pytest.raises(ValueError):
        mem.backup()
    mem.close()
    folder = tmp_path / "many"
    made = []
    for _ in range(13):
        p = backup_path(tmp_path / "live.db", "before-migration-9", folder)
        p.parent.mkdir(exist_ok=True)
        p.write_text("x")
        made.append(p)
    removed = prune(folder, "live", "before-migration", keep=10)
    assert removed == made[:3] and sorted(folder.iterdir()) == sorted(made[3:])
