"""3.0 foundations: the versioned contracts, the order hash an approval binds to, the proposal state machine, the
shared event stream and the /v3 routes."""

import threading
import time

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from tradeassist import contracts as C
from tradeassist.api import create_app
from tradeassist.bus import EventBus
from tradeassist.config import Settings
from tradeassist.db import Database
from tradeassist.providers.demo import DemoData
from tradeassist.service import TradeAssistant


def terms(**kw):
    base = dict(account_kind="paper", symbol="AAPL", instrument="equity", side="buy", quantity=3, limit_price=200.5)
    base.update(kw)
    return C.OrderTerms(**base)


def test_the_order_hash_changes_with_every_term_and_nothing_else():
    t = terms()
    assert t.order_hash() == terms().order_hash()
    for change in ({"quantity": 4}, {"limit_price": 200.51}, {"side": "sell"}, {"symbol": "MSFT"}, {"account_kind": "live"},
                   {"time_in_force": "gtc"}, {"extended_hours": True}, {"account_ref": "acct-1"}):
        assert terms(**change).order_hash() != t.order_hash(), change


def test_order_terms_refuse_what_cannot_be_sent():
    with pytest.raises(ValidationError):
        terms(limit_price=None)                                       # a limit order with no limit
    with pytest.raises(ValidationError):
        terms(quantity=0)
    with pytest.raises(ValidationError):
        terms(limit_price=float("nan"))
    with pytest.raises(ValidationError):
        terms(instrument="option", side="buy_to_open")                # an option order with no contract
    with pytest.raises(ValidationError):
        terms(side="buy_to_open")                                     # an option side on a share order
    with pytest.raises(ValidationError):
        C.OrderTerms(**{**terms().model_dump(), "surprise": 1})       # unknown fields are refused
    opt = C.OptionTerms(option_symbol="AAPL261218C00200000", underlying="AAPL", option_type="CALL", strike=200, expiration="2026-12-18")
    o = terms(instrument="option", side="buy_to_open", option=opt, limit_price=4.1, quantity=2)
    assert o.notional() == pytest.approx(2 * 4.1 * 100)


def test_a_proposal_must_carry_the_hash_of_its_own_terms():
    t = terms()
    now = C.utc_now()
    p = C.TradeProposal(id=C.new_id("prop"), created_at=now, updated_at=now, status="draft", source="research", terms=t,
                        order_hash=t.order_hash(), expires_at=now)
    assert p.order_hash == t.order_hash()
    with pytest.raises(ValidationError):
        C.TradeProposal(id="x", created_at=now, updated_at=now, status="draft", source="research", terms=t,
                        order_hash=terms(quantity=4).order_hash(), expires_at=now)
    with pytest.raises(ValidationError):
        C.TradeProposal(id="x", created_at=now, updated_at=now, status="approved-ish", source="research", terms=t,
                        order_hash=t.order_hash(), expires_at=now)


def test_an_unknown_submission_can_only_be_settled_by_reconciliation_never_retried():
    assert not C.can_move("unknown", "submitting") and not C.can_move("unknown", "authorized")
    assert C.can_move("submitting", "unknown") and C.can_move("unknown", "filled")
    for state in C.TERMINAL_STATES - {"filled"}:
        assert C.TRANSITIONS[state] == frozenset()
    assert not C.can_move("awaiting_approval", "submitting")          # nothing is sent before it is authorized
    assert set(C.TRANSITIONS) == set(C.PROPOSAL_STATES)
    for targets in C.TRANSITIONS.values():
        assert targets <= set(C.PROPOSAL_STATES)


def test_ids_are_unique_and_ordered_by_time():
    ids = [C.new_id("x") for _ in range(2000)]
    assert len(set(ids)) == len(ids)
    a = C.new_id("x")
    time.sleep(0.003)
    assert C.new_id("x")[:14] > a[:14]


def test_the_event_stream_is_shared_by_two_processes_on_one_database(tmp_path):
    web, daemon = EventBus(Database(tmp_path / "e.db"), "app"), EventBus(Database(tmp_path / "e.db"), "daemon")
    start = web.latest_id()
    first = web.publish("proposal.updated", "prop_1", status="awaiting_approval")
    daemon.publish("daemon.heartbeat", None, ok=True)
    got = web.since(start)
    assert [e["topic"] for e in got] == ["proposal.updated", "daemon.heartbeat"] and got[1]["origin"] == "daemon"
    assert [e["topic"] for e in web.since(start, ["proposal.*"])] == ["proposal.updated"]
    seen = []
    t = threading.Thread(target=lambda: seen.extend(web.wait(got[-1]["id"], timeout=5)))
    t.start()
    time.sleep(0.2)
    daemon.publish("opportunity.ranked", None, count=3)              # written by the other connection
    t.join(timeout=6)
    assert [e["topic"] for e in seen] == ["opportunity.ranked"] and first < seen[0]["id"]
    assert web.wait(seen[0]["id"], timeout=0.2) == []


def test_events_never_carry_a_credential(tmp_path):
    bus = EventBus(Database(tmp_path / "e.db"))
    bus.publish("x.y", None, note="Authorization: Bearer abcdefghijklmnopqrstuvwxyz0123456789")
    assert "abcdefghijklmnop" not in str(bus.since(0))


def test_the_v3_routes_answer_beside_the_old_ones(tmp_path):
    settings = Settings(data_mode="demo", db_path=tmp_path / "t.db")
    svc = TradeAssistant(settings, provider=DemoData(), db=Database(tmp_path / "t.db"))
    client = TestClient(create_app(settings, svc))
    st = client.get("/v3/status").json()
    assert st["contract_version"] == "3.0" and st["data"]["mode"] == "demo" and st["schema_version"] >= 19
    schema = client.get("/v3/contracts").json()
    assert {"TradeProposal", "OrderTerms", "Opportunity", "StudyRun", "ForecastVersion"} <= set(schema["models"])
    assert "unknown" in schema["transitions"]["submitting"]
    assert client.get("/v3/events").json() == {"events": [], "latest": st["latest_event"]}
    svc.bus.publish("study.completed", "study_1")
    ev = client.get(f"/v3/events?after={st['latest_event']}").json()
    assert [e["topic"] for e in ev["events"]] == ["study.completed"] and ev["latest"] == ev["events"][0]["id"]
    assert client.get("/api/status").status_code == 200 and client.get("/v2/features").status_code == 200


def test_a_new_database_has_the_3_0_decision_tables():
    db = Database(":memory:")
    names = {r["name"] for r in db.query("SELECT name FROM sqlite_master WHERE type = 'table'")}
    assert {"opportunity_candidates", "opportunity_observations", "studies", "forecast_records", "trading_policies",
            "trade_proposals", "proposal_events", "approvals", "broker_orders", "broker_fills", "budget_ledger", "jobs",
            "heartbeats", "notifications", "workspace_layouts", "remote_sessions", "execution_state", "audit_log",
            "app_events"} <= names
    # One active candidate per stock, direction and horizon.
    row = ("c1", "demo", "AAPL", "LONG", "swing", "active", "[]", "[]", "t", "t", "t", 1.0, "[]", "{}")
    sql = ("INSERT INTO opportunity_candidates (id, mode, symbol, direction, horizon, status, kinds_json, sources_json, "
           "first_seen_at, updated_at, expires_at, rank_score, components_json, edge_json) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)")
    with db.tx() as cur:
        cur.execute(sql, row)
    import sqlite3
    with pytest.raises(sqlite3.IntegrityError):
        with db.tx() as cur:
            cur.execute(sql, ("c2",) + row[1:])
    with db.tx() as cur:                                             # an expired one does not block a new one
        cur.execute("UPDATE opportunity_candidates SET status = 'expired' WHERE id = 'c1'")
        cur.execute(sql, ("c2",) + row[1:])
