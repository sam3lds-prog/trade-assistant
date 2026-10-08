"""Position history and journal integrity (2.1.1, Sprint 2): plan fields that can be cleared,
lifecycle snapshots for every trade (classic journal included) with their timing and source,
audited corrections, idempotent imports, and a journal mirror whose fills keep their ids."""

from __future__ import annotations

import sqlite3
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from tradeassist import analytics
from tradeassist.api import create_app
from tradeassist.config import Settings
from tradeassist.db import Database
from tradeassist.journal import Journal
from tradeassist.optionmath import NY
from tradeassist.positions import (PositionError, add_execution, correct_execution, create, read, sync_legacy_trade,
                                   update_plan)
from tradeassist.providers.demo import DemoData
from tradeassist.service import TradeAssistant
from tradeassist.snapshots import close_of, cutoff_session

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture()
def app(tmp_path):
    settings = Settings(data_mode="demo", db_path=tmp_path / "t.db")
    svc = TradeAssistant(settings, provider=DemoData(), db=Database(":memory:"))
    return TestClient(create_app(settings, svc)), svc


def legacy_db(tmp_path) -> Path:
    path = tmp_path / "old.db"
    conn = sqlite3.connect(path)
    conn.executescript((FIXTURES / "db_2_1_0.sql").read_text())
    conn.close()
    return path


def journal_trade(j: Journal, quote=None, symbol="AAPL") -> int:
    return j.create(data_source="demo", symbol=symbol, option_symbol=f"{symbol}261218C00200000", option_type="call",
                    strike=200, expiration="2026-12-18", contracts=2, thesis="t" * 20, planned_limit=5.0, max_limit=5.2,
                    plan={"profit_target_pct": 50, "stop_loss_pct": 50}, quote=quote or {}, scorecard=[], signals=[],
                    overrides=[])


def effective(pos, kind):
    return [s for s in pos["snapshots"] if s["snapshot_type"] == kind and s["effective"]]


# --- 2.2 clearing plan fields ------------------------------------------------------------------------

def test_2_2_null_clears_omitted_keeps_number_updates_through_http(app):
    c, _ = app
    p = c.post("/v2/positions", json={"symbol": "KO", "quantity": 100, "invalidation_price": 50, "target_1": 80,
                                      "target_2": 90, "risk_amount": 700, "intended_horizon": 20}).json()
    pid = p["id"]
    assert p["pnl"]["initial_risk"] == 700
    r = c.post(f"/v2/positions/{pid}/plan", json={"risk_amount": None}).json()
    assert r["risk_amount"] is None and r["invalidation_price"] == 50 and r["target_1"] == 80   # only risk cleared
    r = c.post(f"/v2/positions/{pid}/plan", json={"thesis": "new"}).json()
    assert r["risk_amount"] is None and r["thesis"] == "new" and r["target_2"] == 90 and r["intended_horizon"] == 20
    r = c.post(f"/v2/positions/{pid}/plan", json={"risk_amount": 250.5}).json()
    assert r["risk_amount"] == 250.5
    r = c.post(f"/v2/positions/{pid}/plan", json={"target_1": None, "target_2": None, "intended_horizon": None,
                                                  "invalidation_price": None}).json()
    assert (r["target_1"], r["target_2"], r["intended_horizon"], r["invalidation_price"]) == (None, None, None, None)
    assert r["risk_amount"] == 250.5
    bad = c.post(f"/v2/positions/{pid}/plan", json={"thesis": None})
    assert bad.status_code == 400 and "cannot be null" in bad.json()["error"]
    assert c.post(f"/v2/positions/{pid}/plan", json={"risk_amount": -1}).status_code == 422


def test_2_2_after_clearing_risk_the_r_multiple_uses_what_remains(app):
    c, _ = app
    p = c.post("/v2/positions", json={"symbol": "KO", "quantity": 100, "invalidation_price": 1, "risk_amount": 1000,
                                      "entry": {"price": 60.0, "executed_at": "2026-10-01T10:00"}}).json()
    assert p["pnl"]["initial_risk"] == 1000
    r = c.post(f"/v2/positions/{p['id']}/plan", json={"risk_amount": None}).json()
    assert r["pnl"]["initial_risk"] == pytest.approx((60 - 1) * 100)          # entry to invalidation now
    r = c.post(f"/v2/positions/{p['id']}/plan", json={"invalidation_price": None}).json()
    assert r["pnl"]["initial_risk"] is None and r["pnl"]["r_multiple"] is None   # nothing left to measure R by


def test_2_2_persistence_layer_semantics():
    db = Database(":memory:")
    with db.tx() as cur:
        pid = create(cur, symbol="A", instrument_type="STOCK", direction="LONG", quantity=1, risk_amount=100.0,
                     target_1=20.0)
        update_plan(cur, pid, {"risk_amount": None})
        update_plan(cur, pid, {})
        row = read(cur, pid)
        assert row["risk_amount"] is None and row["target_1"] == 20.0
        with pytest.raises(PositionError, match="cannot be null"):
            update_plan(cur, pid, {"notes": None})
        with pytest.raises(PositionError, match="cannot be changed"):
            update_plan(cur, pid, {"quantity": 5})
    db.close()


# --- the journal mirror keeps its fills -------------------------------------------------------------

def test_journal_sync_is_idempotent_and_keeps_fill_ids():
    db = Database(":memory:")
    j = Journal(db)
    tid = journal_trade(j)
    j.log_attempt(tid, side="buy", bid=4.9, ask=5.1, limit_price=5.0, filled=True)
    j.log_attempt(tid, side="sell", bid=6.9, ask=7.1, limit_price=7.0, filled=True)
    rows = lambda: [tuple(r) for r in db.query("SELECT * FROM executions ORDER BY id")]   # noqa: E731
    first = rows()
    for _ in range(5):
        with db.tx() as cur:
            sync_legacy_trade(cur, tid)
    assert rows() == first and len(first) == 2
    assert db.one("SELECT COUNT(*) FROM execution_corrections")[0] == 0
    # The journal changes under it (an edit made directly in the database): the mirror follows, in place,
    # and the change is recorded.
    with db.tx() as cur:
        cur.execute("UPDATE trades SET exit_price = 7.25 WHERE id = ?", (tid,))
        sync_legacy_trade(cur, tid)
    after = rows()
    assert [r[0] for r in after] == [r[0] for r in first] and after[1][4] == 7.25
    audit = db.query("SELECT source, action FROM execution_corrections")
    assert [tuple(a) for a in audit] == [("JOURNAL_SYNC", "UPDATE")]
    db.close()


def test_resyncing_a_2_1_database_changes_no_fill(tmp_path):
    db = Database(legacy_db(tmp_path))
    before = [tuple(r) for r in db.query("SELECT * FROM executions ORDER BY id")]
    with db.tx() as cur:
        for (tid,) in db.query("SELECT id FROM trades"):
            sync_legacy_trade(cur, tid)
    assert [tuple(r) for r in db.query("SELECT * FROM executions ORDER BY id")] == before
    db.close()


# --- 2.1 lifecycle snapshots for journal trades ---------------------------------------------------------

def test_2_1_journal_trades_get_plan_entry_and_exit_snapshots_once(app):
    c, svc = app
    quote = {"bid": 4.9, "ask": 5.1, "mid": 5.0, "spot": 210.0, "iv": 0.3, "delta": 0.4,
             "at": datetime.now(timezone.utc).isoformat()}
    tid = journal_trade(svc.journal, quote)
    pid = svc.db.one("SELECT id FROM positions WHERE legacy_trade_id = ?", (tid,))["id"]
    svc.journal.log_attempt(tid, side="buy", bid=4.9, ask=5.1, limit_price=5.0, filled=True)
    svc.journal.close_at(tid, 6.0)
    for _ in range(3):                                       # repeated syncs and backfills change nothing
        with svc.db.tx() as cur:
            sync_legacy_trade(cur, tid)
        svc.desk.backfill_snapshots()
    p = c.get(f"/v2/positions/{pid}").json()
    assert len(effective(p, "PLAN")) == len(effective(p, "ENTRY")) == len(effective(p, "EXIT")) == 1
    plan, entry, exit_ = effective(p, "PLAN")[0], effective(p, "ENTRY")[0], effective(p, "EXIT")[0]
    assert plan["method"] == "JOURNAL_QUOTE" and plan["underlying_price"] == 210.0 and plan["bid"] == 4.9
    assert entry["method"] == "LIVE" and entry["capture_timing"] == "CONTEMPORANEOUS" and not entry["uses_later_data"]
    assert entry["execution_id"] == p["executions"][0]["id"] and exit_["execution_id"] == p["executions"][1]["id"]
    assert len(p["snapshots"]) == 3                          # nothing superseded, nothing duplicated


def test_2_1_backfill_of_a_2_1_database_is_safe_and_repeatable(tmp_path):
    path = legacy_db(tmp_path)
    settings = Settings(data_mode="demo", db_path=path)
    svc = TradeAssistant(settings, provider=DemoData(), db=Database(path))
    c = TestClient(create_app(settings, svc))
    fills_before = [tuple(r) for r in svc.db.query("SELECT * FROM executions ORDER BY id")]
    dry = c.post("/v2/snapshots/backfill", json={"dry_run": True}).json()
    assert dry["dry_run"] and dry["created"] == 0 and dry["planned"] > 0
    assert svc.db.one("SELECT COUNT(*) FROM snapshot_reconstruction")[0] == 0           # a dry run writes nothing
    first = c.post("/v2/snapshots/backfill", json={}).json()
    second = c.post("/v2/snapshots/backfill", json={}).json()
    assert first["created"] > 0 and second["created"] == 0 and second["superseded"] == 0
    assert [tuple(r) for r in svc.db.query("SELECT * FROM executions ORDER BY id")] == fills_before    # fills untouched
    mirrors = svc.db.query("SELECT id, status FROM positions WHERE legacy_trade_id IS NOT NULL")
    for m in mirrors:
        p = c.get(f"/v2/positions/{m['id']}").json()
        if p["executions"]:
            e = effective(p, "ENTRY")
            assert len(e) == 1 and e[0]["method"] == "PRIOR_BAR" and e[0]["uses_later_data"] is False
            assert e[0]["provenance"]["origin"] in ("desk", "reconcile")
        assert len(effective(p, "EXIT")) == (1 if p["status"] == "CLOSED" else 0)
    # The fixture's journal tickets saved no quote: their plan snapshots are recorded as unavailable, not invented.
    plans = svc.db.query("SELECT status, reason FROM snapshot_reconstruction WHERE snapshot_type = 'PLAN'")
    assert plans and all(r["status"] == "UNAVAILABLE" and "no quote" in r["reason"] for r in plans)
    status = c.get("/v2/snapshots/status").json()
    assert status["missing"] == [] and status["hindsight"] == []
    svc.db.close()


def test_2_1_hindsight_snapshots_from_2_1_are_kept_but_replaced(tmp_path):
    path = legacy_db(tmp_path)
    db = Database(path)
    old = db.one("SELECT * FROM position_snapshots s JOIN positions p ON p.id = s.position_id "
                 "WHERE p.symbol = 'AAPL' AND p.legacy_trade_id IS NULL AND s.snapshot_type = 'ENTRY'")
    # Migration 5 inferred how 2.1 made it: from the fill day's close, after a 10 a.m. fill.
    assert old["method"] == "END_OF_DAY" and old["uses_later_data"] == 1 and old["capture_timing"] == "RETROSPECTIVE"
    db.close()
    settings = Settings(data_mode="demo", db_path=path)
    svc = TradeAssistant(settings, provider=DemoData(), db=Database(path))
    c = TestClient(create_app(settings, svc))
    c.post("/v2/snapshots/backfill", json={"upgrade_hindsight": True})
    p = c.get(f"/v2/positions/{old['position_id']}").json()
    entries = [s for s in p["snapshots"] if s["snapshot_type"] == "ENTRY"]
    assert len(entries) == 2 and [s["effective"] for s in entries].count(True) == 1
    kept = next(s for s in entries if not s["effective"])
    new = next(s for s in entries if s["effective"])
    assert kept["id"] == old["id"] and "after the fill" in kept["superseded_reason"]
    assert new["method"] == "PRIOR_BAR" and new["data_timestamp"] == "2026-09-14"     # fill was Mon Sep 15, 10:00
    svc.db.close()


def test_migration_5_infers_provenance_and_keeps_one_effective_snapshot(tmp_path):
    path = legacy_db(tmp_path)
    conn = sqlite3.connect(path)
    # A duplicate ENTRY, as a 2.1 database could hold: kept, but only the first stays effective.
    conn.execute("INSERT INTO position_snapshots (position_id, snapshot_type, timestamp, raw_json) "
                 "SELECT position_id, 'ENTRY', timestamp, raw_json FROM position_snapshots WHERE snapshot_type = 'ENTRY' LIMIT 1")
    conn.commit()
    conn.close()
    db = Database(path)
    dup = db.query("SELECT id, superseded_at, superseded_reason FROM position_snapshots WHERE snapshot_type = 'ENTRY' "
                   "AND position_id = (SELECT position_id FROM position_snapshots GROUP BY position_id, snapshot_type "
                   "HAVING COUNT(*) > 1 LIMIT 1) ORDER BY id")
    assert dup[0]["superseded_at"] is None and dup[1]["superseded_at"] and "before 2.1.1" in dup[1]["superseded_reason"]
    plans = db.query("SELECT method, capture_timing, uses_later_data, provenance_json FROM position_snapshots "
                     "WHERE snapshot_type = 'PLAN'")
    assert plans and all(r["method"] == "LIVE" and r["capture_timing"] == "CONTEMPORANEOUS" and r["uses_later_data"] == 0
                         and '"inferred": true' in r["provenance_json"] for r in plans)
    with pytest.raises(sqlite3.IntegrityError):          # the index allows one effective ENTRY per position
        with db.tx() as cur:
            cur.execute("INSERT INTO position_snapshots (position_id, snapshot_type, timestamp) "
                        "SELECT position_id, 'ENTRY', timestamp FROM position_snapshots WHERE snapshot_type = 'ENTRY' "
                        "AND superseded_at IS NULL LIMIT 1")
    db.close()


# --- 2.3 snapshots are time-accurate ---------------------------------------------------------------------

def test_2_3_cutoff_is_the_last_bar_finished_before_the_fill():
    ny = lambda *a: datetime(*a, tzinfo=NY)                     # noqa: E731
    assert cutoff_session(ny(2026, 10, 1, 10, 0)) == date(2026, 9, 30)          # 10 a.m.: yesterday's close
    assert cutoff_session(ny(2026, 10, 1, 15, 59)) == date(2026, 9, 30)
    assert cutoff_session(ny(2026, 10, 1, 16, 0)) == date(2026, 10, 1)            # after the close: that day
    assert cutoff_session(ny(2026, 10, 5, 9, 45)) == date(2026, 10, 4)            # Monday: the bar filter picks Friday
    assert close_of(date(2026, 9, 30)) == "2026-09-30T20:00:00+00:00"


def test_2_3_a_10am_entry_is_not_read_from_that_days_close(app):
    c, _ = app
    p = c.post("/v2/positions", json={"symbol": "AAPL", "quantity": 10, "invalidation_price": 1,
                                      "entry": {"price": 200.0, "executed_at": "2026-10-01T10:00"}}).json()
    e = effective(p, "ENTRY")[0]
    assert e["method"] == "PRIOR_BAR" and e["data_timestamp"] == "2026-09-30" and e["uses_later_data"] is False
    assert e["observation_cutoff"] == "2026-09-30T20:00:00+00:00" and e["executed_at"] == "2026-10-01T14:00:00+00:00"
    assert e["underlying_price"] == 200.0 and e["raw"]["underlying_price_basis"] == "the fill price"
    assert e["capture_timing"] == "RETROSPECTIVE" and e["rsi_14"] is not None
    assert e["raw"]["analysis"]["trend"]["as_of"] <= "2026-09-30"                 # the trend reading too
    after_close = c.post("/v2/positions", json={"symbol": "MSFT", "quantity": 1,
                                                "entry": {"price": 400.0, "executed_at": "2026-10-01T16:30"}}).json()
    assert effective(after_close, "ENTRY")[0]["data_timestamp"] == "2026-10-01"
    monday = c.post("/v2/positions", json={"symbol": "KO", "quantity": 1,
                                           "entry": {"price": 60.0, "executed_at": "2026-10-05T09:45"}}).json()
    assert effective(monday, "ENTRY")[0]["data_timestamp"] == "2026-10-02"           # Friday's close
    live = c.post("/v2/positions", json={"symbol": "KO", "quantity": 1, "entry": {"price": 60.0}}).json()
    le = effective(live, "ENTRY")[0]
    assert le["method"] == "LIVE" and le["capture_timing"] == "CONTEMPORANEOUS" and le["uses_later_data"] is False


def test_2_3_analytics_never_use_hindsight_as_entry_context():
    pos = {"snapshots": [{"snapshot_type": "ENTRY", "superseded_at": None, "uses_later_data": True, "trend_state": "UP",
                          "market_regime": "BULL_CALM"}]}
    assert analytics.entry_context(pos) == (None, "recorded with hindsight")
    pos["snapshots"].append({"snapshot_type": "ENTRY", "superseded_at": None, "uses_later_data": False, "trend_state": "DOWN"})
    pos["snapshots"][0]["superseded_at"] = "2026-10-08T00:00:00+00:00"
    ctx, why = analytics.entry_context(pos)
    assert ctx["trend_state"] == "DOWN" and why is None
    assert analytics.entry_context({"snapshots": []}) == (None, "not recorded")


# --- 2.4 audited corrections ---------------------------------------------------------------------------------

def test_2_4_correcting_a_price_rebuilds_the_accounts_and_is_recorded(app):
    c, svc = app
    p = c.post("/v2/positions", json={"symbol": "KO", "quantity": 100,
                                      "entry": {"price": 60.0, "executed_at": "2026-10-01T10:00"}}).json()
    sell = c.post(f"/v2/positions/{p['id']}/executions",
                  json={"side": "SELL", "quantity": 100, "price": 65.0, "executed_at": "2026-10-02T10:00"}).json()
    assert sell["status"] == "CLOSED" and sell["pnl"]["realized"] == pytest.approx(500)
    eid = sell["executions"][1]["id"]
    r = c.post(f"/v2/positions/{p['id']}/executions/{eid}/correct", json={"price": 64.5, "reason": "Typed the wrong price"})
    assert r.status_code == 200, r.text
    fixed = r.json()
    assert fixed["pnl"]["realized"] == pytest.approx(450) and fixed["status"] == "CLOSED"
    cor = fixed["corrections"][0]
    assert cor["previous"]["price"] == 65.0 and cor["new"]["price"] == 64.5 and cor["reason"] == "Typed the wrong price"
    assert cor["source"] == "USER" and cor["ledger_before"]["realized_pnl"] == pytest.approx(500)
    assert cor["ledger_after"]["realized_pnl"] == pytest.approx(450) and cor["changed_at"]
    assert c.get(f"/v2/positions/{p['id']}/corrections").json()["corrections"][0]["id"] == cor["id"]
    # Same fill id, snapshots still tied to it: the exit time did not change, so the EXIT stands.
    assert [s["id"] for s in effective(fixed, "EXIT")] == [s["id"] for s in effective(sell, "EXIT")]


def test_2_4_a_quantity_correction_can_reopen_and_the_exit_snapshot_is_superseded(app):
    c, _ = app
    p = c.post("/v2/positions", json={"symbol": "KO", "quantity": 100,
                                      "entry": {"price": 60.0, "executed_at": "2026-10-01T10:00"}}).json()
    done = c.post(f"/v2/positions/{p['id']}/executions",
                  json={"side": "SELL", "quantity": 100, "price": 61.0, "executed_at": "2026-10-02T10:00"}).json()
    assert effective(done, "EXIT")
    eid = done["executions"][1]["id"]
    fixed = c.post(f"/v2/positions/{p['id']}/executions/{eid}/correct",
                   json={"quantity": 90, "reason": "Only 90 sold at the broker"}).json()
    assert fixed["status"] == "PARTIALLY_CLOSED" and fixed["ledger"]["open_quantity"] == 10
    assert fixed["correction"]["status_before"] == "CLOSED" and fixed["correction"]["status_after"] == "PARTIALLY_CLOSED"
    assert effective(fixed, "EXIT") == []
    assert any(s["snapshot_type"] == "EXIT" and not s["effective"] for s in fixed["snapshots"])
    # Now it is open again, it can be closed again normally.
    again = c.post(f"/v2/positions/{p['id']}/executions",
                   json={"side": "SELL", "quantity": 10, "price": 62.0, "executed_at": "2026-10-03T10:00"}).json()
    assert again["status"] == "CLOSED" and len(effective(again, "EXIT")) == 1


def test_2_4_correcting_the_entry_time_replaces_the_entry_snapshot(app):
    c, _ = app
    p = c.post("/v2/positions", json={"symbol": "AAPL", "quantity": 5,
                                      "entry": {"price": 200.0, "executed_at": "2026-10-01T10:00"}}).json()
    eid = p["executions"][0]["id"]
    fixed = c.post(f"/v2/positions/{p['id']}/executions/{eid}/correct",
                   json={"executed_at": "2026-09-29T10:00", "reason": "Bought two days earlier"}).json()
    e = effective(fixed, "ENTRY")
    assert len(e) == 1 and e[0]["data_timestamp"] == "2026-09-28" and e[0]["executed_at"] == "2026-09-29T14:00:00+00:00"
    old = [s for s in fixed["snapshots"] if s["snapshot_type"] == "ENTRY" and not s["effective"]]
    assert len(old) == 1 and "corrected" in old[0]["superseded_reason"]


def test_2_4_invalid_corrections_change_nothing(app):
    c, svc = app
    p = c.post("/v2/positions", json={"symbol": "KO", "quantity": 100,
                                      "entry": {"price": 60.0, "executed_at": "2026-10-01T10:00"}}).json()
    c.post(f"/v2/positions/{p['id']}/executions", json={"side": "SELL", "quantity": 50, "price": 61, "executed_at": "2026-10-02T10:00"})
    pos = c.get(f"/v2/positions/{p['id']}").json()
    buy, sell = pos["executions"]
    snapshot = lambda: ([tuple(r) for r in svc.db.query("SELECT * FROM executions ORDER BY id")],          # noqa: E731
                        [tuple(r) for r in svc.db.query("SELECT * FROM positions ORDER BY id")],
                        svc.db.one("SELECT COUNT(*) FROM execution_corrections")[0])
    before = snapshot()
    url = f"/v2/positions/{p['id']}/executions"
    cases = [
        (f"{url}/{buy['id']}/correct", {"quantity": 40, "reason": "would leave a sale of 50 against 40"}, "only 40"),
        (f"{url}/{sell['id']}/correct", {"side": "SELL_SHORT", "reason": "wrong side"}, "takes BUY or SELL"),
        (f"{url}/{sell['id']}/correct", {"price": 61, "reason": "no change"}, "already recorded"),
        (f"{url}/{sell['id']}/correct", {"price": 62, "reason": "  "}, "Say why"),
        (f"{url}/99999/correct", {"price": 62, "reason": "missing fill"}, "no fill"),
        (f"{url}/{sell['id']}/correct", {"executed_at": "2026-09-30T10:00", "reason": "before the buy"}, "nothing is open"),
    ]
    for path, body, message in cases:
        r = c.post(path, json=body)
        assert r.status_code in (400, 422), (body, r.text)
        if r.status_code == 400:
            assert message in r.json()["error"], (body, r.json())
    assert c.post(f"{url}/{sell['id']}/correct", json={"note": "x", "reason": "unknown field"}).status_code == 422
    assert snapshot() == before


def test_2_4_a_failure_while_recording_the_audit_rolls_the_fill_back(monkeypatch):
    import tradeassist.positions as positions
    db = Database(":memory:")
    with db.tx() as cur:
        pid = create(cur, symbol="A", instrument_type="STOCK", direction="LONG", quantity=10)
        add_execution(cur, pid, side="BUY", quantity=10, price=5.0, executed_at="2026-10-01T10:00")
    eid = db.one("SELECT id FROM executions")["id"]

    def broken(*a, **k):
        raise sqlite3.OperationalError("disk full")
    monkeypatch.setattr(positions, "record_correction", broken)
    with pytest.raises(sqlite3.OperationalError):
        with db.tx() as cur:
            correct_execution(cur, pid, eid, {"price": 6.0}, "test")
    assert db.one("SELECT price FROM executions")["price"] == 5.0
    assert db.one("SELECT COUNT(*) FROM execution_corrections")[0] == 0
    db.close()


def test_2_4_journal_mirrors_are_corrected_in_the_journal_not_here(app):
    c, svc = app
    tid = journal_trade(svc.journal)
    svc.journal.log_attempt(tid, side="buy", bid=4.9, ask=5.1, limit_price=5.0, filled=True)
    p = c.get("/v2/positions?status=open").json()["positions"][0]
    r = c.post(f"/v2/positions/{p['id']}/executions/{p['executions'][0]['id']}/correct", json={"price": 4, "reason": "x y z"})
    assert r.status_code == 400 and "option journal" in r.json()["error"]


# --- 2.5 imported fills are recorded once -----------------------------------------------------------------------

def test_2_5_the_same_broker_fill_is_recorded_once(app):
    c, svc = app
    p = c.post("/v2/positions", json={"symbol": "KO", "quantity": 100}).json()
    fill = {"side": "BUY", "quantity": 100, "price": 60.0, "executed_at": "2026-10-01T10:00", "source": "ROBINHOOD",
            "external_id": "rh-123"}
    for _ in range(3):
        r = c.post(f"/v2/positions/{p['id']}/executions", json=fill)
        assert r.status_code == 200, r.text
    got = c.get(f"/v2/positions/{p['id']}").json()
    assert len(got["executions"]) == 1 and got["ledger"]["open_quantity"] == 100
    assert got["executions"][0]["external_id"] == "rh-123"
    clash = c.post(f"/v2/positions/{p['id']}/executions", json={**fill, "price": 61.0})
    assert clash.status_code == 400 and "already recorded with different details" in clash.json()["error"]
    # Without an id, two identical fills are two fills: they are never merged.
    q = c.post("/v2/positions", json={"symbol": "MSFT", "quantity": 2}).json()
    same = {"side": "BUY", "quantity": 1, "price": 400.0, "executed_at": "2026-10-01T10:00"}
    c.post(f"/v2/positions/{q['id']}/executions", json=same)
    two = c.post(f"/v2/positions/{q['id']}/executions", json=same).json()
    assert len(two["executions"]) == 2 and two["ledger"]["open_quantity"] == 2


def test_2_5_external_ids_are_unique_per_source_in_the_database():
    db = Database(":memory:")
    with db.tx() as cur:
        a = create(cur, symbol="A", instrument_type="STOCK", direction="LONG", quantity=10)
        add_execution(cur, a, side="BUY", quantity=1, price=1.0, executed_at="2026-10-01T10:00", source="IMPORT",
                      external_id="x1")
        add_execution(cur, a, side="BUY", quantity=1, price=1.0, executed_at="2026-10-01T10:00", source="ROBINHOOD",
                      external_id="x1")                    # another source may reuse the id
    with pytest.raises(sqlite3.IntegrityError):
        with db.tx() as cur:
            cur.execute("INSERT INTO executions (position_id, side, quantity, price, fees, executed_at, source, created_at, "
                        "external_id) VALUES (?, 'BUY', 1, 1, 0, '2026-10-01T14:00:00+00:00', 'IMPORT', 'x', 'x1')", (a,))
    db.close()


def test_snapshot_timing_constants_are_documented():
    from tradeassist.snapshots import LIVE_WINDOW
    assert LIVE_WINDOW == timedelta(minutes=15)
