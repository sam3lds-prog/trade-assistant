"""3.0 phase 6: live execution through a separate Robinhood adapter. Off unless switched on; bound to one account;
only share limit orders; reviewed, then placed once with a reference; an unanswered placement is found by
reconciliation and never sent again; a ceiling no policy can raise; the kill switch cancels at the broker."""

from __future__ import annotations

import json
from datetime import datetime, timedelta

import pytest
import time_machine
from mcp import Client, types

from tests.robinhood_trading_standin import ACCOUNT, TradingStandin
from tradeassist import policy as P
from tradeassist.api import create_app
from tradeassist.brokers import BrokerError
from tradeassist.config import Settings
from tradeassist.db import Database
from tradeassist.optionmath import NY
from tradeassist.providers.demo import DemoData
from tradeassist.providers.mcp_bridge import ReadOnlyBridge, acceptable_tool_name
from tradeassist.providers.robinhood import ALLOWED_TOOLS
from tradeassist.robinhood_exec import EXEC_TOOLS, RobinhoodExecution, TradingBridge, ref_id
from tradeassist.service import TradeAssistant
from tradeassist.v3.trading import ProposalIn, TradingDesk

pytestmark = pytest.mark.clock("2026-10-22T10:30")
ALLOWED = frozenset(n for names in EXEC_TOOLS.values() for n in names)


def build(tmp_path, **kw):
    s = Settings(**{"data_mode": "demo", "db_path": tmp_path / "t.db", "desk_worker": "off", **kw})
    svc = TradeAssistant(s, provider=DemoData(), db=Database(tmp_path / "t.db"))
    create_app(s, svc)
    return svc


@pytest.fixture
def live(tmp_path, monkeypatch):
    svc = build(tmp_path, robinhood_execution=True, robinhood_execution_account=ACCOUNT, live_max_order=100.0)
    desk = svc.trading
    monkeypatch.setattr(TradingDesk, "mode", property(lambda self: "live"))      # real prices, as far as the desk can tell
    standin = TradingStandin()
    bridge = TradingBridge(lambda: Client(standin.server), ALLOWED, call_timeout=1.5, first_call_timeout=15)
    desk.brokers["live"] = RobinhoodExecution(bridge, ACCOUNT)
    assert desk.check_live(0)["ready"], desk.live_reasons
    P.save(svc.db, {**P.current(svc.db).model_dump(), "mode": "approval_required", "account_kind": "live", "trial_capital": 1000,
                    "max_order": 100, "max_daily_new_buys": 500}, live_allowed=True)
    yield svc, desk, standin
    bridge.close()
    svc.db.close()


def approved(desk, qty=1, symbol="AMZN", **kw):
    p = desk.create(ProposalIn(symbol=symbol, side="buy", quantity=qty, thesis="live test", **kw))
    assert p["status"] == "awaiting_approval", p["risk"]["text"]
    return desk.decide(p["pending_approval"]["id"], True, p["order_hash"])


def run(desk):
    return desk.make_worker("t", None).drain()


def placed(standin):
    return [c for c in standin.calls if c[0] == "place_equity_order"]


# --- the gates ------------------------------------------------------------------------------------------------------

def test_live_trading_is_off_unless_switched_on(tmp_path):
    svc = build(tmp_path)
    desk = svc.trading
    assert "live" not in desk.brokers and not desk.live_status()["enabled"]
    with pytest.raises(ValueError, match="not enabled"):
        P.save(svc.db, {**P.current(svc.db).model_dump(), "mode": "approval_required", "account_kind": "live"}, live_allowed=desk.live_ready)
    with pytest.raises(BrokerError):
        desk.broker_for("live")
    svc.db.close()


def test_the_market_data_connection_still_cannot_act():
    for name in EXEC_TOOLS["place"] + EXEC_TOOLS["review"] + EXEC_TOOLS["cancel"]:
        assert not acceptable_tool_name(name)
    assert not (ALLOWED & ALLOWED_TOOLS)
    with pytest.raises(ValueError):
        ReadOnlyBridge(lambda: None, frozenset({"place_equity_order"}))


def test_the_trading_bridge_calls_only_its_own_tools():
    for bad in ("transfer_funds", "place_option_order", "cancel_all_equity_orders", "get_equity_orders_and_cancel", "get_accounts_and_transfer"):
        with pytest.raises(ValueError):
            TradingBridge(lambda: None, frozenset({bad}))
    standin = TradingStandin()
    bridge = TradingBridge(lambda: Client(standin.server), ALLOWED)
    try:
        with pytest.raises(PermissionError):
            bridge.call("transfer_funds", {"amount": 1})
        bridge._tools = {**bridge.tools(), "get_equity_orders": bridge.tools()["get_equity_orders"].model_copy(
            update={"annotations": types.ToolAnnotations(read_only_hint=False)})}
        assert not bridge.may_call("get_equity_orders")             # a read the server says changes something
        assert standin.forbidden_calls == []
    finally:
        bridge.close()


def test_the_account_must_be_one_this_sign_in_can_trade_and_allowed_for_agents():
    standin = TradingStandin()
    for account, words in (("1234", "not one of the accounts"), ("9ZZ11112222", "not enabled for agent trading")):
        bridge = TradingBridge(lambda: Client(standin.server), ALLOWED)
        try:
            r = RobinhoodExecution(bridge, account).readiness(0)
            assert not r["ready"] and any(words in x for x in r["reasons"])
        finally:
            bridge.close()


def test_live_orders_need_real_prices(live, monkeypatch):
    _, desk, _ = live
    monkeypatch.setattr(TradingDesk, "mode", property(lambda self: "demo"))
    assert not desk.check_live(0)["ready"] and "demo" in " ".join(desk.live_reasons)


# --- sending -------------------------------------------------------------------------------------------------------

def test_a_live_order_is_reviewed_then_placed_once_with_its_reference_then_reconciled(live):
    svc, desk, standin = live
    p = approved(desk)
    assert p["terms"]["account_ref"].startswith(f"…{ACCOUNT[-4:]}-") and p["account_kind"] == "live"
    run(desk)
    f = desk.get(p["id"])
    names = [c[0] for c in standin.calls]
    assert names.index("review_equity_order") < names.index("place_equity_order")
    call = placed(standin)
    assert len(call) == 1 and call[0][1]["ref_id"] == ref_id(f["orders"][0]["client_order_id"]) and call[0][1]["account_number"] == ACCOUNT
    assert f["status"] == "submitted" and f["orders"][0]["broker"] == "robinhood"
    standin.fill(f["orders"][0]["broker_order_id"], price=28.9)
    desk.reconcile()
    g = desk.get(p["id"])
    assert g["status"] == "filled" and g["orders"][0]["fills"][0]["price"] == 28.9 and g["reserved"] == pytest.approx(0)
    desk.reconcile()
    assert len(desk.get(p["id"])["orders"][0]["fills"]) == 1 and len(placed(standin)) == 1


def test_a_review_refusal_stops_the_order_before_it_is_placed(live):
    _, desk, standin = live
    standin.review_refuses = True
    p = approved(desk)
    run(desk)
    f = desk.get(p["id"])
    assert f["status"] == "rejected" and "Insufficient buying power" in f["events"][-1]["reason"] and placed(standin) == []
    assert f["reserved"] == pytest.approx(0)


def test_a_server_refusal_is_a_rejection(live):
    _, desk, standin = live
    standin.place_mode = "error"
    p = approved(desk)
    run(desk)
    assert desk.get(p["id"])["status"] == "rejected"


def test_a_placement_without_an_answer_is_found_by_reconciliation_and_never_resent(live):
    _, desk, standin = live
    standin.place_mode = "hang_after_place"
    p = approved(desk)
    run(desk)                                                  # times out, is unknown, and the reconcile job finds it
    f = desk.get(p["id"])
    assert len(placed(standin)) == 1 and "unknown" in [e["to_status"] for e in f["events"]]
    assert f["status"] == "submitted" and f["orders"][0]["broker_order_id"] in standin.orders
    standin.place_mode = "queue"
    run(desk)
    desk.reconcile()
    assert len(placed(standin)) == 1


def test_a_placement_that_never_arrived_fails_and_frees_its_capital(live):
    _, desk, standin = live
    standin.place_mode = "hang"
    p = approved(desk)
    run(desk)
    assert desk.get(p["id"])["status"] == "unknown" and desk.get(p["id"])["reserved"] > 0
    with time_machine.travel(datetime.now(NY) + timedelta(minutes=16), tick=True):
        for _ in range(3):
            desk.reconcile()
    f = desk.get(p["id"])
    assert f["status"] == "failed" and standin.orders == {} and f["reserved"] == pytest.approx(0)
    assert len(placed(standin)) == 1


# --- limits ------------------------------------------------------------------------------------------------------------

def test_the_live_ceiling_caps_every_policy(live):
    svc, desk, _ = live
    P.save(svc.db, {**P.current(svc.db).model_dump(), "max_order": 900, "trial_capital": 5000, "max_daily_new_buys": 5000}, live_allowed=True)
    p = desk.create(ProposalIn(symbol="AMZN", side="buy", quantity=5))
    c = {x["key"]: x for x in p["risk"]["checks"]}
    assert p["status"] == "blocked" and c["live_ceiling"]["status"] == "fail" and c["order_size"]["status"] == "pass"


def test_an_approval_is_bound_to_the_account(live):
    _, desk, _ = live
    p = desk.create(ProposalIn(symbol="AMZN", side="buy", quantity=1))
    desk.live_account_ref = "…2222"                           # the configured account changed after it was written
    r = desk.decide(p["pending_approval"]["id"], True, p["order_hash"])
    assert r["status"] == "blocked" and "bound to …2222" in r["risk"]["text"]


def test_only_share_limit_orders_are_sent(live):
    _, desk, _ = live
    ex = desk.brokers["live"]
    opt = {"client_order_id": "x", "terms": {"instrument": "option", "option": {"option_symbol": "AMZN261218C00030000"}, "side": "buy_to_open",
                                              "order_type": "limit", "limit_price": 1.0, "symbol": "AMZN", "quantity": 1}}
    with pytest.raises(BrokerError, match="by hand"):
        ex._values(opt)
    with pytest.raises(BrokerError, match="limit"):
        ex._values({"client_order_id": "x", "terms": {"instrument": "equity", "side": "buy", "order_type": "market", "symbol": "AMZN", "quantity": 1}})


def test_the_kill_switch_cancels_a_working_live_order_at_robinhood(live):
    _, desk, standin = live
    p = approved(desk)
    run(desk)
    assert desk.get(p["id"])["status"] == "submitted"
    desk.set_kill_switch(True, "testing live")
    run(desk)
    assert desk.get(p["id"])["status"] == "cancelled" and ("cancel_equity_order", {"order_id": desk.get(p["id"])["orders"][0]["broker_order_id"]}) in standin.calls
    blocked = desk.create(ProposalIn(symbol="AMZN", side="buy", quantity=1))
    assert blocked["status"] == "blocked" and "kill switch" in blocked["risk"]["text"]


def test_the_status_says_real_orders_can_be_sent_only_when_they_can(live):
    svc, desk, _ = live
    assert svc.status()["places_orders"] is True
    desk.live_ready = False
    assert svc.status()["places_orders"] is False
    desk.check_live(0)
    P.save(svc.db, {**P.current(svc.db).model_dump(), "mode": "paper", "account_kind": "paper"}, live_allowed=True)
    assert svc.status()["places_orders"] is False


# --- from the independent review of the order path (3.0 phase 7) ------------------------------------------------------

def test_an_unreadable_answer_to_a_placement_is_unknown_and_found(live, monkeypatch):
    _, desk, standin = live
    ex = desk.brokers["live"]
    real = ex.bridge.call

    def garble(name, arguments=None):
        out = real(name, arguments)
        return {"data": "Order submitted"} if name == "place_equity_order" else out
    monkeypatch.setattr(ex.bridge, "call", garble)
    p = approved(desk)
    run(desk)
    f = desk.get(p["id"])
    assert "unknown" in [e["to_status"] for e in f["events"]] and f["status"] == "submitted"     # found by its reference
    assert len(placed(standin)) == 1 and f["reserved"] > 0


def test_cumulative_and_execution_answers_never_count_the_same_shares_twice(live):
    _, desk, standin = live
    p = approved(desk, qty=2)
    run(desk)
    oid = desk.get(p["id"])["orders"][0]["broker_order_id"]
    standin.fill(oid, quantity=1, price=28.9)
    o = standin.orders[oid]
    executions = o["executions"]
    o["executions"] = []                               # this answer gives only the cumulative figures
    desk.reconcile()
    o["executions"] = executions                       # the next lists the executions
    desk.reconcile()
    standin.fill(oid, quantity=1, price=28.8)
    desk.reconcile()
    g = desk.get(p["id"])
    assert sum(f["quantity"] for f in g["orders"][0]["fills"]) == 2 and g["status"] == "filled"
    assert desk.positions("live")["positions"][0]["quantity"] == 2


def test_another_account_with_the_same_last_digits_is_another_account():
    from tradeassist.robinhood_exec import account_ref
    a, b = account_ref("5QR99887766"), account_ref("7XY00117766")
    assert a.startswith("…7766-") and b.startswith("…7766-") and a != b


def test_the_adapter_refuses_an_order_written_for_another_account(live):
    _, desk, standin = live
    p = approved(desk)
    with desk.db.tx() as cur:
        terms = {**p["terms"], "account_ref": "…7766-0000000000"}
        cur.execute("UPDATE broker_orders SET terms_json = ? WHERE proposal_id = ?", (json.dumps(terms), p["id"]))
    with pytest.raises(BrokerError, match="different account"):
        desk.brokers["live"].place({"terms": terms, "client_order_id": "x", "created_at": "2026-10-22T14:30:00+00:00"})
    assert placed(standin) == []
