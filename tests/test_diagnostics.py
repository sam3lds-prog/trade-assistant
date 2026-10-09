"""Reconciliation, repair, database rules, the data health view, trade reviews, error messages
and structured logging (2.3, Sprint 7)."""

from __future__ import annotations

import logging
import sqlite3
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from tradeassist import events
from tradeassist.api import create_app
from tradeassist.config import Settings
from tradeassist.db import Database
from tradeassist.positions import PositionError
from tradeassist.providers.demo import DemoData
from tradeassist.reconcile import diagnose, repair
from tradeassist.service import TradeAssistant

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture()
def app(tmp_path):
    settings = Settings(data_mode="demo", db_path=tmp_path / "t.db", sandbox_token="secret-token-abc123")
    svc = TradeAssistant(settings, provider=DemoData(), db=Database(tmp_path / "t.db"))
    return TestClient(create_app(settings, svc)), svc


def fills(svc):
    return [tuple(r) for r in svc.db.query("SELECT * FROM executions ORDER BY id")]


# --- 6.2 and 6.4: reconciliation report and controlled repair -----------------------------------------

def test_6_4_a_clean_book_reconciles(app):
    c, svc = app
    p = c.post("/v2/positions", json={"symbol": "KO", "quantity": 100, "entry": {"price": 60, "executed_at": "2026-10-01T10:00"}}).json()
    c.post(f"/v2/positions/{p['id']}/executions", json={"side": "SELL", "quantity": 40, "price": 61, "executed_at": "2026-10-02T10:00"})
    r = c.get("/v2/diagnostics/reconciliation").json()
    assert r["counts"]["positions"] == 1 and r["counts"]["open"] == 1 and r["database"]["ok"]
    assert r["ledger_mismatches"] == [] and r["invalid_states"] == [] and not r["needs_repair"]


def test_6_2_mismatches_are_found_read_only_then_repaired_after_a_backup(app, tmp_path):
    c, svc = app
    p = c.post("/v2/positions", json={"symbol": "KO", "quantity": 100, "entry": {"price": 60, "executed_at": "2026-10-01T10:00"}}).json()
    with svc.db.tx() as cur:                                   # a stored figure drifts from the fills
        cur.execute("UPDATE positions SET realized_pnl = 999, open_quantity = 50 WHERE id = ?", (p["id"],))
    before_fills = fills(svc)
    r = c.get("/v2/diagnostics/reconciliation").json()
    m = r["ledger_mismatches"][0]
    assert m["id"] == p["id"] and m["differences"]["realized_pnl"] == {"stored": 999, "from_fills": 0.0}
    assert r["needs_repair"] and svc.db.one("SELECT realized_pnl FROM positions")[0] == 999      # the report changed nothing
    refused = c.post("/v2/diagnostics/repair", json={})
    assert refused.status_code == 400 and "confirm" in refused.json()["error"]
    done = c.post("/v2/diagnostics/repair", json={"confirm": True}).json()
    assert done["recomputed"][0]["id"] == p["id"] and Path(done["backup"]).is_file()
    assert svc.db.one("SELECT realized_pnl, open_quantity FROM positions")[:] == (0.0, 100.0)
    assert fills(svc) == before_fills                                                        # no fill touched
    log = svc.db.one("SELECT action, backup_path FROM maintenance_log")
    assert log["action"] == "REPAIR" and log["backup_path"] == done["backup"]
    again = c.post("/v2/diagnostics/repair", json={"confirm": True}).json()
    assert again["recomputed"] == [] and again["journal_resynced"] == []                      # repeatable


def test_6_4_journal_drift_duplicates_and_unresolved_options_are_reported(app):
    c, svc = app
    tid = svc.journal.create(data_source="demo", symbol="AAPL", option_symbol="X", option_type="call", strike=200,
                             expiration="2026-10-02", contracts=1, thesis="t" * 20, planned_limit=2.0, max_limit=2.0,
                             plan={}, quote={}, scorecard=[], signals=[], overrides=[])
    svc.journal.log_attempt(tid, side="buy", bid=1.9, ask=2.1, limit_price=2.0, filled=True)
    with svc.db.tx() as cur:
        cur.execute("UPDATE executions SET price = 2.5 WHERE position_id = (SELECT id FROM positions WHERE legacy_trade_id = ?)",
                    (tid,))
    q = c.post("/v2/positions", json={"symbol": "MSFT", "quantity": 2}).json()
    same = {"side": "BUY", "quantity": 1, "price": 400, "executed_at": "2026-10-01T10:00"}
    c.post(f"/v2/positions/{q['id']}/executions", json=same)
    c.post(f"/v2/positions/{q['id']}/executions", json=same)
    r = diagnose(svc.db, svc.market)
    assert r["journal_mismatches"][0]["trade_id"] == tid
    assert r["possible_duplicate_fills"][0]["count"] == 2 and "not merged" in r["possible_duplicate_fills"][0]["note"]
    assert any(u["symbol"] == "AAPL" for u in r["unresolved_options"])
    done = repair(svc.db, svc.market)
    assert done["journal_resynced"] == [tid]
    assert diagnose(svc.db, svc.market)["journal_mismatches"] == []
    assert svc.db.one("SELECT COUNT(*) FROM executions WHERE position_id = ?", (q["id"],))[0] == 2  # duplicates kept


def test_6_4_a_2_1_database_report(tmp_path):
    path = tmp_path / "old.db"
    conn = sqlite3.connect(path)
    conn.executescript((FIXTURES / "db_2_1_0.sql").read_text())
    conn.close()
    db = Database(path)
    r = diagnose(db)
    assert r["counts"] == {"positions": 10, "open": 3, "closed": 3, "planned": 2, "cancelled": 2, "journal_mirrors": 5}
    assert r["ledger_mismatches"] == [] and r["database"]["ok"]
    assert [i["symbol"] for i in r["lifecycle_issues"]] == ["KO"]                  # reported, kept as recorded
    assert len(r["hindsight_snapshots"]) == 5 and len(r["missing_snapshots"]) == 10
    done = repair(db)
    assert done["recomputed"] == [] and done["left_as_recorded"][0]["symbol"] == "KO"
    db.close()


# --- 6.1 database rules ---------------------------------------------------------------------------------------

def test_6_1_the_database_refuses_impossible_rows(app):
    _, svc = app
    with pytest.raises(sqlite3.IntegrityError, match="planned quantity must be above zero"):
        with svc.db.tx() as cur:
            cur.execute("INSERT INTO positions (symbol, instrument_type, direction, status, quantity, created_at, updated_at) "
                        "VALUES ('A', 'STOCK', 'LONG', 'PLANNED', 0, 'x', 'x')")
    with pytest.raises(sqlite3.IntegrityError, match="strike above zero"):
        with svc.db.tx() as cur:
            cur.execute("INSERT INTO positions (symbol, instrument_type, direction, status, quantity, created_at, updated_at) "
                        "VALUES ('A', 'OPTION', 'LONG', 'PLANNED', 1, 'x', 'x')")
            cur.execute("INSERT INTO option_contracts (position_id, underlying_symbol, option_type, strike, expiration) "
                        "VALUES (last_insert_rowid(), 'A', 'CALL', -5, '2026-12-18')")
    with pytest.raises(sqlite3.IntegrityError):
        with svc.db.tx() as cur:
            cur.execute("INSERT INTO executions (position_id, side, quantity, price, fees, executed_at, created_at) "
                        "VALUES (1, 'BUY', -1, 1, 0, '2026-10-01T14:00:00+00:00', 'x')")
    names = {r[0] for r in svc.db.query("SELECT name FROM sqlite_master WHERE type = 'table'")}
    assert {"position_adjustments", "option_events", "research_runs", "maintenance_log", "snapshot_reconstruction",
            "execution_corrections"} <= names
    assert svc.db.one("PRAGMA foreign_keys")[0] == 1


def test_6_1_rows_from_before_the_rules_are_reported_and_still_recomputed(tmp_path):
    path = tmp_path / "old.db"
    conn = sqlite3.connect(path)
    conn.executescript((FIXTURES / "db_2_1_0.sql").read_text())
    pid = conn.execute("SELECT id FROM positions WHERE status = 'OPEN' ORDER BY id LIMIT 1").fetchone()[0]
    conn.execute("UPDATE positions SET quantity = 0, realized_pnl = 5 WHERE id = ?", (pid,))   # broken before 2.3
    conn.commit()
    conn.close()
    db = Database(path)
    r = diagnose(db)
    assert [b["id"] for b in r["rule_breaks"]] == [pid] and r["ledger_mismatches"][0]["id"] == pid
    done = repair(db)                                     # the UPDATE is allowed: it does not make the row worse
    assert done["recomputed"][0]["id"] == pid and diagnose(db)["ledger_mismatches"] == []
    with pytest.raises(sqlite3.IntegrityError, match="above zero"):
        with db.tx() as cur:                              # but a good row cannot be made bad
            cur.execute("UPDATE positions SET quantity = -1 WHERE id = (SELECT MIN(id) FROM positions WHERE id != ?)",
                        (pid,))
    db.close()


# --- 7.4 data health --------------------------------------------------------------------------------------------

def test_7_4_data_health_shows_the_source_quotes_history_snapshots_and_migrations(app):
    c, svc = app
    c.post("/v2/positions", json={"symbol": "AAPL", "quantity": 10, "entry": {"price": 100}})
    c.post("/v2/positions", json={"symbol": "AAPL", "instrument_type": "OPTION", "quantity": 1,
                                  "option": {"option_type": "CALL", "strike": 7777, "expiration": "2027-01-15"},
                                  "entry": {"price": 1.0, "executed_at": "2026-10-01T10:00"}})
    h = c.get("/v2/health/data").json()
    assert h["source"]["label"] == "Demo data (synthetic)" and h["source"]["status"] in ("OK", "NOT_USED_YET")
    assert h["quotes"]["open_positions"] == 2 and h["positions_missing_valuations"][0]["description"].startswith("Long 1")
    assert h["missing_option_chains"] and "chain" in h["missing_option_chains"][0]["error"]
    assert any(b["symbol"] == "AAPL" and b["held"] for b in h["history"])
    assert h["migration"]["schema_version"] >= 8 and h["migration"]["latest"]["version"] == h["migration"]["schema_version"]
    assert h["reconciliation"]["database_ok"] and "snapshots" in h and isinstance(h["recent_problems"], list)
    assert "secret-token-abc123" not in str(h)


# --- 7.3 trade reviews ------------------------------------------------------------------------------------------

def test_7_3_a_completed_trade_is_explained(app):
    c, _ = app
    p = c.post("/v2/positions", json={"symbol": "KO", "quantity": 100, "thesis": "Pullback to support", "setup_type": "pullback",
                                      "invalidation_price": 1, "target_1": 10_000,
                                      "entry": {"price": 60, "executed_at": "2026-09-01T10:00"}}).json()
    c.post(f"/v2/positions/{p['id']}/executions", json={"side": "SELL", "quantity": 100, "price": 62, "fees": 1,
                                                        "executed_at": "2026-09-20T10:00"})
    a = c.get(f"/v2/positions/{p['id']}/analytics").json()
    rv = a["review"]
    assert rv["planned"]["thesis"] == "Pullback to support" and rv["planned"]["invalidation_price"] == 1
    assert rv["executed"]["average_entry_price"] == 60 and rv["executed"]["average_exit_price"] == 62
    assert rv["at_entry"]["available"] and rv["at_entry"]["method"] == "PRIOR_BAR"
    assert rv["thesis_invalidated"] is False and rv["target_1_reached"] is False
    assert rv["result"]["trading"] == pytest.approx(199) and rv["result"]["net"] == pytest.approx(199)
    assert "does not say why" in rv["note"] and any("never closed through" in t for t in rv["what_happened"])
    q = c.post("/v2/positions", json={"symbol": "MSFT", "quantity": 1, "entry": {"price": 400, "executed_at": "2026-09-01T10:00"}}).json()
    c.post(f"/v2/positions/{q['id']}/executions", json={"side": "SELL", "quantity": 1, "price": 390, "executed_at": "2026-09-02T10:00"})
    missing = c.get(f"/v2/positions/{q['id']}/analytics").json()["review"]["missing"]
    assert any("No invalidation price" in m for m in missing) and any("No risk" in m for m in missing)


# --- 7.5 error messages and 8.3 logging -----------------------------------------------------------------------

def test_7_5_database_errors_get_actionable_messages(app, monkeypatch):
    c, svc = app

    def clash(req):
        raise sqlite3.IntegrityError("UNIQUE constraint failed: executions.source, executions.external_id")
    monkeypatch.setattr(svc.desk, "create", clash)
    r = c.post("/v2/positions", json={"symbol": "KO", "quantity": 1})
    assert r.status_code == 409 and "Nothing was saved" in r.json()["error"]

    def busy(req):
        raise sqlite3.OperationalError("database is locked")
    monkeypatch.setattr(svc.desk, "create", busy)
    r = c.post("/v2/positions", json={"symbol": "KO", "quantity": 1})
    assert r.status_code == 503 and "busy" in r.json()["error"]


def test_8_3_events_are_structured_and_never_carry_credentials(app, caplog):
    c, _ = app
    caplog.set_level(logging.INFO, logger="tradeassist.events")
    p = c.post("/v2/positions", json={"symbol": "KO", "quantity": 1, "entry": {"price": 60, "executed_at": "2026-10-01T10:00"}}).json()
    c.post(f"/v2/positions/{p['id']}/executions", json={"side": "SELL", "quantity": 1, "price": 61, "executed_at": "2026-10-02T10:00"})
    c.post(f"/v2/positions/{p['id']}/executions", json={"side": "BUY", "quantity": 1, "price": 61})
    line = next(r.getMessage() for r in caplog.records if "event=invalid_transition" in r.getMessage())
    assert "already closed" in line and "Traceback" not in caplog.text
    c.post(f"/v2/positions/{p['id']}/executions/{p['executions'][0]['id']}/correct", json={"price": 60.5, "reason": "broker slip"})
    assert any("event=correction_recorded" in r.getMessage() for r in caplog.records)
    events.emit("provider_failure", logging.WARNING, source="Tradier", error="401 Bearer abcdefghijklmnop1234567890 token=xyz")
    assert "abcdefghijklmnop1234567890" not in caplog.text and "token=xyz" not in caplog.text
    assert events.recent(1)[0]["event"] == "provider_failure" and "[redacted]" in events.recent(1)[0]["error"]
    record = logging.LogRecord("x", logging.INFO, "", 0, "Authorization: Bearer Zz9Yy8Xx7Ww6Vv5Uu4Tt3Ss2Rr1", None, None)
    events.RedactingFilter().filter(record)
    assert "Zz9Yy8" not in record.getMessage()


def test_8_3_migrations_are_logged_with_their_backup(tmp_path, caplog):
    caplog.set_level(logging.INFO, logger="tradeassist.events")
    path = tmp_path / "old.db"
    conn = sqlite3.connect(path)
    conn.executescript((FIXTURES / "db_2_1_0.sql").read_text())
    conn.close()
    Database(path).close()
    line = next(r.getMessage() for r in caplog.records if "event=migration_applied" in r.getMessage())
    assert "versions=5,6,7,8,9" in line and "backup=old-before-migration-5-" in line


def test_position_errors_stay_plain(app):
    c, _ = app
    r = c.post("/v2/positions/12345/plan", json={"thesis": "x"})
    assert r.status_code == 400 and r.json()["error"] == "No position 12345"
    assert isinstance(PositionError("x"), ValueError)
