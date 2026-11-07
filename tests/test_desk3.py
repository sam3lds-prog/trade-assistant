"""3.0 phase 5: the Trading Desk. A proposal is checked when written, approved and sent; an approval is bound to the
exact order, used once and expires; an unmeasured risk blocks; an order whose submission was not seen is never sent
again but reconciled; the ledger reserves, releases and records; the kill switch stops everything."""

from __future__ import annotations

import json
from datetime import datetime, timedelta

import pytest
import time_machine
from fastapi.testclient import TestClient

from tradeassist import contracts as C
from tradeassist import policy as P
from tradeassist.api import create_app
from tradeassist.brokers import BrokerTimeout
from tradeassist.config import Settings
from tradeassist.db import Database
from tradeassist.optionmath import NY
from tradeassist.providers.base import DataError
from tradeassist.providers.demo import DemoData
from tradeassist.service import RequestError, TradeAssistant
from tradeassist.v3.trading import AmendIn, ProposalIn

pytestmark = pytest.mark.clock("2026-10-22T10:30")       # a Thursday morning: the market is open


@pytest.fixture
def app(tmp_path):
    s = Settings(data_mode="demo", db_path=tmp_path / "t.db", desk_worker="off")
    svc = TradeAssistant(s, provider=DemoData(), db=Database(tmp_path / "t.db"))
    client = TestClient(create_app(s, svc))
    limits(svc.trading, trial_capital=100_000, max_order=5_000, max_daily_new_buys=20_000)    # room for demo prices
    yield client, svc, svc.trading
    svc.db.close()


def limits(desk, **kw):
    return P.save(desk.db, {**P.current(desk.db).model_dump(), **kw})


def run_jobs(desk):
    return desk.make_worker("test", None).drain()


def buy(desk, symbol="AMZN", qty=1, **kw):
    return desk.create(ProposalIn(symbol=symbol, side="buy", quantity=qty, thesis="a test", **kw))


def approve(desk, p, **kw):
    return desk.decide(p["pending_approval"]["id"], True, p["order_hash"], **kw)


def held(desk, symbol="AMZN"):
    return next((x["quantity"] for x in desk.positions()["positions"] if x["symbol"] == symbol), 0)


class FakeBroker:
    name = "fake"

    def __init__(self, place=None, status=None):
        self._place, self._status = place, status
        self.placed, self.asked = [], []

    def place(self, order):
        self.placed.append(order["client_order_id"])
        if isinstance(self._place, Exception):
            raise self._place
        return self._place(order)

    def status(self, order):
        self.asked.append(order["client_order_id"])
        return self._status(order)

    def cancel(self, order):
        return {"state": "cancelled", "broker_order_id": order.get("broker_order_id"), "fills": []}


def filled_at(price):
    def answer(order):
        return {"state": "filled", "broker_order_id": f"B-{order['client_order_id']}",
                "fills": [{"at": datetime.now(NY).isoformat(), "quantity": order["terms"]["quantity"], "price": price, "fees": 0,
                           "external_id": f"F-{order['client_order_id']}"}]}
    return answer


# --- the whole path --------------------------------------------------------------------------------------------------

def test_a_proposal_is_checked_waits_for_approval_and_fills_on_paper(app):
    _, svc, desk = app
    p = buy(desk)
    assert p["status"] == "awaiting_approval" and p["risk"]["ok"] and p["pending_approval"]
    assert p["terms"]["limit_price"] == svc.market.quote("AMZN")["ask"] and p["account_kind"] == "paper"
    assert C.OrderTerms(**p["terms"]).order_hash() == p["order_hash"]
    a = approve(desk, p)
    assert a["status"] == "authorized" and a["reserved"] == pytest.approx(p["terms"]["limit_price"])
    run_jobs(desk)
    f = desk.get(p["id"])
    assert f["status"] == "filled" and f["orders"][0]["state"] == "filled" and f["reserved"] == pytest.approx(0)
    assert [e["to_status"] for e in f["events"]] == ["awaiting_approval", "authorized", "submitting", "filled"]
    assert held(desk) == 1
    book = desk.book()
    assert book["capital_in_use"] == pytest.approx(p["terms"]["limit_price"]) and book["today_new_buys"] == pytest.approx(p["terms"]["limit_price"])
    s = desk.create(ProposalIn(symbol="AMZN", side="sell", quantity=1))
    approve(desk, s)
    run_jobs(desk)
    assert desk.get(s["id"])["status"] == "filled" and held(desk) == 0
    realized = svc.db.one("SELECT amount FROM budget_ledger WHERE kind = 'realized'")["amount"]
    q = svc.market.quote("AMZN")
    assert realized == pytest.approx(q["bid"] - p["terms"]["limit_price"], abs=0.02)


# --- approvals -----------------------------------------------------------------------------------------------------

def test_an_approval_is_bound_to_the_exact_order(app):
    _, _, desk = app
    p = buy(desk)
    old = p["pending_approval"]["id"]
    changed = desk.amend(p["id"], AmendIn(limit_price=round(p["terms"]["limit_price"] - 0.05, 2)))
    assert changed["order_hash"] != p["order_hash"] and changed["version"] == 2
    with pytest.raises(RequestError, match="already superseded"):
        desk.decide(old, True, p["order_hash"])
    new = changed["pending_approval"]["id"]
    with pytest.raises(RequestError, match="order changed"):
        desk.decide(new, True, p["order_hash"])              # the old terms' hash: what the user saw is not what would be sent
    assert desk.decide(new, True, changed["order_hash"])["status"] == "authorized"


def test_an_approval_works_once_and_expires(app):
    _, _, desk = app
    p = buy(desk)
    approve(desk, p)
    with pytest.raises(RequestError, match="already approved"):
        approve(desk, p)
    q = buy(desk, "KO")
    with time_machine.travel(datetime.now(NY) + timedelta(minutes=11), tick=True):
        with pytest.raises(RequestError, match="expired"):
            approve(desk, q)
        again = desk.recheck(q["id"])
        assert again["pending_approval"] and again["pending_approval"]["id"] != q["pending_approval"]["id"]


def test_a_phone_approval_needs_its_token(app):
    _, _, desk = app
    p = buy(desk)
    with pytest.raises(RequestError, match="not valid"):
        desk.decide(p["pending_approval"]["id"], True, p["order_hash"], channel="remote", token="guess")
    asked = desk._ask_approval(p["id"], notify=False)
    assert desk.decide(asked["id"], True, p["order_hash"], channel="remote", token=asked["token"], actor="phone")["status"] == "authorized"
    row = desk.db.one("SELECT token_hash FROM approvals WHERE id = ?", (asked["id"],))
    assert asked["token"] not in row["token_hash"]                          # only its hash is stored


def test_declining_cancels_and_releases_nothing_reserved(app):
    _, _, desk = app
    p = buy(desk)
    d = desk.decide(p["pending_approval"]["id"], False, p["order_hash"])
    assert d["status"] == "cancelled" and d["reserved"] == 0


# --- the risk engine -----------------------------------------------------------------------------------------------

def checks(p):
    return {c["key"]: c for c in p["risk"]["checks"]}


def test_limits_on_size_capital_positions_and_duplicates(app):
    _, _, desk = app
    limits(desk, trial_capital=500, max_order=50, max_daily_new_buys=100)              # the trial's defaults
    big = buy(desk, "AMZN", qty=3)                                                    # about $87, over the $50 an order
    assert big["status"] == "blocked" and checks(big)["order_size"]["status"] == "fail"
    assert not big["pending_approval"]
    dup = buy(desk, "AMZN")
    assert checks(dup)["duplicate"]["status"] == "fail"                                # the blocked one is still open
    desk.cancel(big["id"])
    desk.cancel(dup["id"])
    p = buy(desk, "AMZN")
    assert checks(p)["duplicate"]["status"] == "pass" and p["status"] == "awaiting_approval"
    dup = buy(desk, "AMZN")
    assert dup["status"] == "blocked" and checks(dup)["duplicate"]["status"] == "fail"
    desk.cancel(dup["id"])
    limits(desk, max_open_positions=1)
    assert approve(desk, p)["status"] == "authorized"
    run_jobs(desk)
    other = buy(desk, "F")
    assert checks(other)["positions"]["status"] == "fail"


def test_selling_what_is_not_held_is_refused(app):
    _, _, desk = app
    s = desk.create(ProposalIn(symbol="AMZN", side="sell", quantity=1))
    assert s["status"] == "blocked" and checks(s)["holding"]["status"] == "fail"
    short = desk.create(ProposalIn(symbol="AMZN", side="sell_short", quantity=1))
    assert checks(short)["side"]["status"] == "fail" and checks(short)["max_loss"]["status"] == "fail"


def test_an_unmeasured_check_blocks(app, monkeypatch):
    _, svc, desk = app
    real = svc.market.quote

    def no_quote(symbol, *a, **k):
        if symbol == "AMZN":
            raise DataError("the source is down")
        return real(symbol, *a, **k)
    monkeypatch.setattr(svc.market, "quote", no_quote)
    p = desk.create(ProposalIn(symbol="AMZN", side="buy", quantity=1, limit_price=29.0))
    assert p["status"] == "blocked" and checks(p)["quote"]["status"] == "unknown" and not p["risk"]["ok"]


def test_a_position_without_a_quote_leaves_todays_result_unmeasured_which_blocks_buys_not_sells(app, monkeypatch):
    _, svc, desk = app
    p = buy(desk)
    approve(desk, p)
    run_jobs(desk)
    real = svc.market.quote
    calls = {"n": 0}

    def flaky(symbol, *a, **k):            # AMZN quotes for the order itself, not for valuing the position
        calls["n"] += 1
        return real(symbol, *a, **k)
    monkeypatch.setattr(svc.market, "quote", flaky)
    monkeypatch.setattr(desk, "positions", lambda account_kind=None, marks=True: {
        "account_kind": "paper", "positions": [{"symbol": "AMZN", "key": "AMZN", "quantity": 1.0, "avg_cost": 29.0, "cost": 29.0, "multiplier": 1.0}],
        "realized_today": 0.0, "day_pnl": None, "unmeasured": ["AMZN"]})
    b = buy(desk, "F")
    assert checks(b)["daily_loss"]["status"] == "unknown" and b["status"] == "blocked"
    s = desk.create(ProposalIn(symbol="AMZN", side="sell", quantity=1))
    assert "daily_loss" not in checks(s) and s["status"] == "awaiting_approval"


def test_the_daily_loss_stop_blocks_new_buys_but_lets_a_position_be_closed(app):
    _, _, desk = app
    p = buy(desk)
    approve(desk, p)
    run_jobs(desk)
    pr = desk._proposal(p["id"])
    with desk.db.tx() as cur:
        desk._ledger(cur, "realized", -30.0, pr, note="a loss earlier today")
    b = buy(desk, "F")
    assert checks(b)["daily_loss"]["status"] == "fail" and b["status"] == "blocked"
    s = desk.create(ProposalIn(symbol="AMZN", side="sell", quantity=1))
    assert s["status"] == "awaiting_approval"


@pytest.mark.clock("2026-10-22T17:05")
def test_nothing_is_sent_outside_the_window(app):
    _, _, desk = app
    p = buy(desk)
    assert checks(p)["window"]["status"] == "fail" and p["status"] == "blocked"


def test_risk_is_checked_again_just_before_sending(app):
    _, _, desk = app
    p = buy(desk)
    approve(desk, p)
    limits(desk, mode="disabled")
    run_jobs(desk)
    f = desk.get(p["id"])
    assert f["status"] == "blocked" and not f["orders"] and f["reserved"] == pytest.approx(0)
    assert "at submission" in f["events"][-1]["reason"]


# --- execution that does not see its answer ---------------------------------------------------------------------------

def test_a_timeout_makes_the_order_unknown_and_reconciliation_settles_it_without_resending(app):
    _, _, desk = app
    fake = FakeBroker(place=BrokerTimeout("no answer in 10 s"), status=filled_at(28.95))
    desk.brokers["paper"] = fake
    p = buy(desk)
    approve(desk, p)
    run_jobs(desk)                       # the submit, then the reconcile it queued
    f = desk.get(p["id"])
    assert len(fake.placed) == 1 and fake.asked
    assert f["status"] == "filled" and f["orders"][0]["fills"][0]["price"] == 28.95
    statuses = [e["to_status"] for e in f["events"]]
    assert statuses[-2:] == ["unknown", "filled"]
    run_jobs(desk)
    desk.reconcile()
    assert len(fake.placed) == 1 and len(f["orders"]) == 1 and len(desk.get(p["id"])["orders"][0]["fills"]) == 1   # fills counted once


def test_an_unknown_order_the_broker_never_received_fails_and_releases_its_capital(app):
    _, _, desk = app
    fake = FakeBroker(place=BrokerTimeout("connection reset"), status=lambda o: {"state": "not_found", "broker_order_id": None, "fills": []})
    desk.brokers["paper"] = fake
    p = buy(desk)
    approve(desk, p)
    run_jobs(desk)
    f = desk.get(p["id"])
    assert f["status"] == "failed" and f["reserved"] == pytest.approx(0) and len(fake.placed) == 1
    assert desk.db.one("SELECT COUNT(*) AS n FROM notifications WHERE kind = 'order.unknown'")["n"] == 1


def test_a_submission_cut_off_by_a_crash_is_recovered_as_unknown_never_resent(app):
    _, _, desk = app
    fake = FakeBroker(place=filled_at(29.0), status=lambda o: {"state": "not_found", "broker_order_id": None, "fills": []})
    desk.brokers["paper"] = fake
    p = buy(desk)
    approve(desk, p)
    with desk.db.tx() as cur:            # the worker wrote the order and died before it heard back
        cur.execute("""INSERT INTO broker_orders (id, proposal_id, mode, account_kind, broker, client_order_id, state, created_at, updated_at, terms_json)
                       VALUES ('ord_x', ?, 'demo', 'paper', 'fake', 'ta-x', 'submitting', '2026-10-22T14:00:00+00:00', '2026-10-22T14:00:00+00:00', ?)""",
                    (p["id"], json.dumps(p["terms"])))
        desk._move(cur, p["id"], "submitting", "desk", "sending")
    assert desk.recover()["recovered"] == 1
    assert desk.get(p["id"])["status"] == "unknown"
    run_jobs(desk)                       # the queued submit job sees the state and does not send; reconcile settles it
    assert fake.placed == [] and desk.get(p["id"])["status"] == "failed"


def test_a_limit_below_the_market_works_then_expires_at_the_close(app):
    _, svc, desk = app
    ask = svc.market.quote("AMZN")["ask"]
    p = desk.create(ProposalIn(symbol="AMZN", side="buy", quantity=1, limit_price=round(ask * 0.9, 2)))
    approve(desk, p)
    run_jobs(desk)
    assert desk.get(p["id"])["status"] == "submitted" and desk.get(p["id"])["reserved"] > 0
    with time_machine.travel(datetime(2026, 10, 22, 16, 5, tzinfo=NY), tick=True):
        desk.reconcile()
    f = desk.get(p["id"])
    assert f["status"] == "expired" and f["reserved"] == pytest.approx(0)


# --- the kill switch, the policy, the routes ------------------------------------------------------------------------

def test_the_kill_switch_cancels_working_orders_and_refuses_approvals(app):
    _, svc, desk = app
    ask = svc.market.quote("AMZN")["ask"]
    w = desk.create(ProposalIn(symbol="AMZN", side="buy", quantity=1, limit_price=round(ask * 0.9, 2)))
    approve(desk, w)
    run_jobs(desk)
    waiting = buy(desk, "F")
    desk.set_kill_switch(True, "testing")
    run_jobs(desk)
    assert desk.get(w["id"])["status"] == "cancelled" and desk.get(w["id"])["reserved"] == pytest.approx(0)
    with pytest.raises(RequestError, match="kill switch"):
        approve(desk, waiting)
    assert checks(buy(desk, "KO"))["kill_switch"]["status"] == "fail"
    assert desk.db.one("SELECT COUNT(*) AS n FROM audit_log WHERE action = 'kill_switch.on'")["n"] == 1
    desk.set_kill_switch(False)
    assert approve(desk, waiting)["status"] == "authorized"


def test_the_policy_is_versioned_audited_and_refuses_live_trading_until_it_is_enabled(app):
    c, svc, desk = app
    d = c.get("/v3/desk/policy").json()
    t = d["defaults"]
    assert t["trial_capital"] == 500 and t["max_order"] == 50 and t["max_daily_new_buys"] == 100 and t["mode"] == "paper"
    assert t["max_daily_loss"] == 25 and t["max_open_positions"] == 3 and t["instruments"] == ["equity"] and t["order_types"] == ["limit"]
    before = svc.db.one("SELECT COUNT(*) AS n FROM audit_log WHERE action = 'policy.changed'")["n"]
    live = {**d["policy"], "mode": "approval_required", "account_kind": "live"}
    r = c.put("/v3/desk/policy", json=live)
    assert r.status_code == 400 and "not enabled" in r.json()["error"]
    r = c.put("/v3/desk/policy", json={**d["policy"], "max_order": 40})
    assert r.status_code == 200 and r.json()["version"] == d["version"] + 1
    assert c.put("/v3/desk/policy", json={**d["policy"], "max_order": 900_000}).status_code == 400      # more than the capital
    assert svc.db.one("SELECT COUNT(*) AS n FROM audit_log WHERE action = 'policy.changed'")["n"] == before + 1


def test_the_routes(app):
    c, _, desk = app
    pre = c.post("/v3/proposals/preview", json={"symbol": "amzn", "quantity": 1}).json()
    assert pre["risk"]["ok"] and pre["terms"]["symbol"] == "AMZN" and not desk.listing()
    p = c.post("/v3/proposals", json={"symbol": "AMZN", "quantity": 1, "thesis": "x"}).json()
    assert c.get("/v3/proposals?status=awaiting_approval").json()["proposals"][0]["id"] == p["id"]
    bad = c.post(f"/v3/approvals/{p['pending_approval']['id']}/approve", json={"order_hash": "0" * 64})
    assert bad.status_code == 400
    ok = c.post(f"/v3/approvals/{p['pending_approval']['id']}/approve", json={"order_hash": p["order_hash"]}).json()
    assert ok["status"] == "authorized"
    st = c.get("/v3/desk/status").json()
    assert st["budget"]["reserved"] > 0 and st["worker"]["runs_jobs"] == "nobody"
    v3 = c.get("/v3/status").json()
    assert v3["trading"]["mode"] == "paper" and v3["risk"]["level"] == "pass" and "label" in v3["daemon"]
    assert c.post("/v3/proposals", json={"symbol": "AMZN", "quantity": 0}).status_code == 422
    assert c.post("/v3/proposals", json={"symbol": "AMZN", "quantity": 1, "source": "a rumour"}).status_code == 400
    assert c.post("/v3/notifications/test").status_code == 400          # no channel set up
    assert c.post("/v3/desk/kill", json={"on": True}).json()["on"] is True


def test_an_approved_order_that_uses_the_last_of_the_capital_is_still_sent(app):
    """Its own reservation is in the book when it is checked again before sending; it must not count twice."""
    _, svc, desk = app
    ask = svc.market.quote("AMZN")["ask"]
    limits(desk, trial_capital=round(ask * 2 + 0.01, 2), max_order=round(ask * 2 + 0.01, 2), max_daily_new_buys=round(ask * 2 + 0.01, 2))
    p = buy(desk, qty=2)
    assert approve(desk, p)["status"] == "authorized"
    run_jobs(desk)
    assert desk.get(p["id"])["status"] == "filled"
