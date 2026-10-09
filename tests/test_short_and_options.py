"""Short-sale financing and the option lifecycle (2.2, Sprint 4)."""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from tradeassist import portfolio
from tradeassist.alerts import position_alerts
from tradeassist.api import create_app
from tradeassist.config import Settings
from tradeassist.db import Database
from tradeassist.optionmath import NY
from tradeassist.positions import (OptionLeg, PositionError, add_adjustment, add_execution, adjust_contract, create,
                                   exercise, expire_worthless, read, void_adjustment)
from tradeassist.providers.demo import DemoData
from tradeassist.service import TradeAssistant

PAST_EXPIRY = "2026-10-02"                         # a Friday before "today" in these tests


@pytest.fixture()
def db():
    d = Database(":memory:")
    yield d
    d.close()


@pytest.fixture()
def client(tmp_path):
    settings = Settings(data_mode="demo", db_path=tmp_path / "t.db")
    svc = TradeAssistant(settings, provider=DemoData(), db=Database(":memory:"))
    return TestClient(create_app(settings, svc)), svc


def short_position(db, qty=100, price=50.0):
    with db.tx() as cur:
        pid = create(cur, symbol="CRWD", instrument_type="STOCK", direction="SHORT", quantity=qty)
        add_execution(cur, pid, side="SELL_SHORT", quantity=qty, price=price, executed_at="2026-10-01T10:00")
    return pid


def option(db, kind="CALL", strike=100.0, qty=2, price=3.0, expiration=PAST_EXPIRY, bought="2026-09-20T10:00", symbol="AAPL"):
    with db.tx() as cur:
        pid = create(cur, symbol=symbol, instrument_type="OPTION", direction="LONG", quantity=qty,
                     option=OptionLeg(kind, strike, expiration))
        add_execution(cur, pid, side="BUY", quantity=qty, price=price, executed_at=bought)
    return pid


def get(db, pid):
    with db.tx() as cur:
        return read(cur, pid)


# --- 3.3 short positions: borrow fees, dividends, partial covers ---------------------------------------

def test_3_3_trading_profit_and_net_profit_after_borrow_fees_and_dividends(db):
    pid = short_position(db)
    assert get(db, pid)["financing"]["borrow_status"] == "UNKNOWN"          # nothing recorded: not known, not zero
    with db.tx() as cur:
        add_execution(cur, pid, side="BUY_TO_COVER", quantity=40, price=45.0, executed_at="2026-10-05T10:00")
        add_adjustment(cur, pid, "BORROW_FEE", amount=-12.50, rate_pct=9.0, effective_at="2026-10-05T16:00",
                       period_start="2026-10-01T10:00", period_end="2026-10-05T16:00")
        add_adjustment(cur, pid, "DIVIDEND_OWED", per_share=0.50, quantity=60, effective_at="2026-10-06T09:00")
        add_execution(cur, pid, side="BUY_TO_COVER", quantity=60, price=48.0, executed_at="2026-10-07T10:00")
    p = get(db, pid)
    fin = p["financing"]
    assert p["status"] == "CLOSED" and p["ledger"]["realized_pnl"] == pytest.approx(40 * 5 + 60 * 2)   # trading: 320
    assert fin["by_kind"]["BORROW_FEE"] == pytest.approx(-12.50) and fin["by_kind"]["DIVIDEND_OWED"] == pytest.approx(-30.0)
    assert fin["financing_total"] == pytest.approx(-42.50)
    assert fin["borrow_status"] == "PARTIAL" and "2 of the 7 days" in fin["borrow_text"]       # Oct 6 and 7 not recorded
    assert "first Oct 06, 2026" in fin["borrow_text"]
    assert fin["borrow_estimate"] is not None and fin["borrow_estimate"] < 0                   # shown, labelled, not added
    with db.tx() as cur:
        add_adjustment(cur, pid, "BORROW_FEE", amount=-8.75, effective_at="2026-10-07T16:00",
                       period_start="2026-10-05T16:00", period_end="2026-10-07T10:00")
    fin = get(db, pid)["financing"]
    assert fin["borrow_status"] == "RECORDED" and fin["borrow_estimate"] is None
    assert fin["financing_total"] == pytest.approx(-51.25)


def test_3_3_net_economic_profit_through_the_api(client):
    c, _ = client
    p = c.post("/v2/positions", json={"symbol": "KO", "direction": "SHORT", "quantity": 100,
                                      "entry": {"price": 60.0, "executed_at": "2026-10-01T10:00"}}).json()
    c.post(f"/v2/positions/{p['id']}/executions", json={"side": "BUY_TO_COVER", "quantity": 100, "price": 55.0,
                                                        "executed_at": "2026-10-06T10:00"})
    r = c.post(f"/v2/positions/{p['id']}/adjustments", json={"kind": "BORROW_FEE", "amount": -20.0,
                                                             "effective_at": "2026-10-06T16:00",
                                                             "period_start": "2026-10-01T10:00", "period_end": "2026-10-06T10:00"})
    assert r.status_code == 200, r.text
    v = r.json()
    assert v["pnl"]["trading_total"] == pytest.approx(500) and v["pnl"]["financing"] == pytest.approx(-20)
    assert v["pnl"]["net_economic"] == pytest.approx(480) and v["financing"]["borrow_status"] == "RECORDED"
    aid = v["adjustments"][0]["id"]
    voided = c.post(f"/v2/positions/{p['id']}/adjustments/{aid}/void", json={"reason": "entered on the wrong trade"}).json()
    assert voided["pnl"]["financing"] == 0 and voided["adjustments"][0]["void_reason"] == "entered on the wrong trade"
    assert voided["financing"]["borrow_status"] == "UNKNOWN"
    assert c.post(f"/v2/positions/{p['id']}/adjustments", content='{"kind": "BORROW_FEE", "amount": NaN}',
                  headers={"Content-Type": "application/json"}).status_code == 422


def test_3_3_adjustments_are_checked(db):
    s = short_position(db)
    with db.tx() as cur:
        long_ = create(cur, symbol="KO", instrument_type="STOCK", direction="LONG", quantity=10)
        for args, kw, message in (
            ((s, "BORROW_FEE"), {"amount": 5.0}, "is a cost"),
            ((long_, "BORROW_FEE"), {"amount": -5.0}, "stock sold short"),
            ((s, "DIVIDEND_RECEIVED"), {"amount": 5.0}, "held long"),
            ((s, "DIVIDEND_OWED"), {"amount": 5.0}, "is a cost"),
            ((s, "DIVIDEND_OWED"), {}, "per share"),
            ((s, "BORROW_RATE"), {}, "yearly borrow rate"),
            ((s, "CASH_ADJUSTMENT"), {"amount": float("inf")}, "finite"),
            ((s, "SPLIT"), {"ratio": 1.0}, "split ratio"),
            ((s, "NOTHING"), {"amount": 1.0}, "kind must be"),
        ):
            with pytest.raises(PositionError, match=message):
                add_adjustment(cur, *args, **kw)
        aid = add_adjustment(cur, s, "BORROW_FEE", amount=-3.0, external_id="rh-fee-1", source="ROBINHOOD")
        assert add_adjustment(cur, s, "BORROW_FEE", amount=-3.0, external_id="rh-fee-1", source="ROBINHOOD",
                              effective_at=cur.execute("SELECT effective_at FROM position_adjustments WHERE id = ?",
                                                       (aid,)).fetchone()[0]) == aid          # imported twice: once
        with pytest.raises(PositionError, match="Say why"):
            void_adjustment(cur, s, aid, " ")
    assert db.one("SELECT COUNT(*) FROM position_adjustments")[0] == 1


def test_3_3_a_split_replays_earlier_fills_in_new_units_without_changing_them(db):
    with db.tx() as cur:
        pid = create(cur, symbol="NVDA", instrument_type="STOCK", direction="LONG", quantity=10, invalidation_price=380.0,
                     target_1=480.0, created_at="2026-09-01T14:00:00+00:00")
        add_execution(cur, pid, side="BUY", quantity=10, price=400.0, executed_at="2026-09-02T10:00")
        add_adjustment(cur, pid, "SPLIT", ratio=4, effective_at="2026-09-10T09:30")
        add_execution(cur, pid, side="SELL", quantity=40, price=105.0, executed_at="2026-09-12T10:00")
    p = get(db, pid)
    assert p["executions"][0]["quantity"] == 10 and p["executions"][0]["price"] == 400.0     # as recorded
    assert p["status"] == "CLOSED" and p["ledger"]["realized_pnl"] == pytest.approx(40 * 5)  # 40 sold at 105 vs 100
    assert p["invalidation_price"] == pytest.approx(95.0) and p["target_1"] == pytest.approx(120.0)
    assert p["quantity"] == 40 and p["ledger"]["units_adjusted"]
    split = p["adjustments"][0]
    assert split["details"]["plan_levels_before"] == {"invalidation_price": 380.0, "target_1": 480.0}
    # A short splits the same way.
    with db.tx() as cur:
        sid = create(cur, symbol="X", instrument_type="STOCK", direction="SHORT", quantity=100)
        add_execution(cur, sid, side="SELL_SHORT", quantity=100, price=30.0, executed_at="2026-09-02T10:00")
        add_adjustment(cur, sid, "SPLIT", ratio=0.1, effective_at="2026-09-10T09:30")           # 1-for-10 reverse split
        add_execution(cur, sid, side="BUY_TO_COVER", quantity=10, price=280.0, executed_at="2026-09-12T10:00")
    s = get(db, sid)
    assert s["status"] == "CLOSED" and s["ledger"]["realized_pnl"] == pytest.approx(10 * 20)
    # Voiding the split puts the plan back and the replay returns to the recorded units.
    with db.tx() as cur:
        q = create(cur, symbol="Y", instrument_type="STOCK", direction="LONG", quantity=10, invalidation_price=90.0,
                   created_at="2026-09-01T14:00:00+00:00")
        add_execution(cur, q, side="BUY", quantity=10, price=100.0, executed_at="2026-09-02T10:00")
        aid = add_adjustment(cur, q, "SPLIT", ratio=2, effective_at="2026-09-10T09:30")
        void_adjustment(cur, q, aid, "split recorded on the wrong stock")
    y = get(db, q)
    assert y["invalidation_price"] == 90.0 and y["ledger"]["open_quantity"] == 10 and not y["ledger"]["units_adjusted"]


def test_3_3_daily_loss_uses_split_adjusted_fills():
    v = {"id": 1, "symbol": "N", "direction": "LONG", "instrument_type": "STOCK", "multiplier": 1, "description": "N",
         "executions": [{"side": "BUY", "quantity": 10, "price": 400.0, "fees": 0, "executed_at": "2026-09-02T14:00:00+00:00"}],
         "unit_adjustments": [{"effective_at": "2026-09-10T13:30:00+00:00", "quantity_factor": 4.0, "price_factor": 0.25}],
         "mark": {"price": 101.0}, "prev_close": 100.0}
    d = portfolio.daily_loss([v], "REALIZED_PLUS_UNREALIZED", 100_000, 3.0, datetime(2026, 10, 6, 14, tzinfo=NY))
    assert d["known_total"] == pytest.approx(40 * 1.0)                                    # 40 shares up $1, not 10 up $-299


# --- 3.4 how a bought option ends ------------------------------------------------------------------------

def test_3_4_expired_worthless_closes_at_zero_on_the_expiration_close(db):
    pid = option(db)
    with db.tx() as cur:
        done = expire_worthless(cur, pid, underlying_price=95.0, underlying_price_source="entered")
    p = get(db, pid)
    assert p["status"] == "CLOSED" and p["ledger"]["realized_pnl"] == pytest.approx(-600.0)
    assert p["executions"][-1]["price"] == 0 and p["executions"][-1]["executed_at"] == "2026-10-02T20:00:00+00:00"
    assert p["lifecycle"]["state"] == "EXPIRED_WORTHLESS" and done["in_the_money_by"] == 0
    ev = p["option_events"][0]
    assert ev["event"] == "EXPIRED_WORTHLESS" and ev["execution_id"] == p["executions"][-1]["id"] and ev["underlying_price"] == 95.0


def test_3_4_in_the_money_at_expiration_is_not_assumed_worthless(db):
    pid = option(db)
    with db.tx() as cur:
        with pytest.raises(PositionError, match=r"\$5.00 in the money"):
            expire_worthless(cur, pid, underlying_price=105.0)
        expire_worthless(cur, pid, underlying_price=105.0, reason="Told the broker not to exercise")
    assert get(db, pid)["option_events"][0]["reason"] == "Told the broker not to exercise"
    future = option(db, expiration=(datetime.now(NY) + timedelta(days=30)).date().isoformat(), bought="2026-10-01T10:00")
    with db.tx() as cur:
        with pytest.raises(PositionError, match="has not expired yet"):
            expire_worthless(cur, future)


def test_3_4_an_expired_option_without_an_outcome_is_unresolved(db):
    pid = option(db)
    p = get(db, pid)
    assert p["lifecycle"]["state"] == "UNRESOLVED" and p["status"] == "OPEN" and p["ledger"]["realized_pnl"] == 0
    p.update({"description": "Long 2 AAPL calls", "bias": 1, "thesis_check": {"status": "INTACT"}})
    kinds = [a["kind"] for a in position_alerts(p, None, None, datetime.now(NY).date())]
    assert "UNRESOLVED_EXPIRATION" in kinds
    x = portfolio.exposure({**p, "mark": {"price": 5.0, "underlying": 105.0, "expired": True}}, None)
    assert x["open_risk"] is None and "record how it ended" in x["risk_reason"]


def test_3_4_exercising_a_call_splits_the_economics_correctly(db):
    pid = option(db, kind="CALL", strike=100.0, qty=2, price=3.0, expiration="2026-12-18")
    with db.tx() as cur:
        done = exercise(cur, pid, underlying_price=110.0, event_at="2026-10-05T15:00")
    opt, stock = get(db, pid), get(db, done["stock_position_id"])
    assert opt["status"] == "CLOSED" and opt["ledger"]["realized_pnl"] == pytest.approx((10 - 3) * 200)
    assert opt["lifecycle"]["state"] == "EXERCISED" and done["shares"] == 200
    assert stock["direction"] == "LONG" and stock["ledger"]["open_quantity"] == 200 and stock["ledger"]["cost_basis"] == 110
    assert stock["setup_type"] == "option exercise" and stock["derived_from"][0]["position_id"] == pid
    with db.tx() as cur:
        add_execution(cur, done["stock_position_id"], side="SELL", quantity=200, price=115.0, executed_at="2026-10-06T10:00")
    total = get(db, pid)["ledger"]["realized_pnl"] + get(db, done["stock_position_id"])["ledger"]["realized_pnl"]
    assert total == pytest.approx((115 - 100 - 3) * 200)          # bought at the strike after paying the premium


def test_3_4_exercising_a_put_against_shares_held(db):
    with db.tx() as cur:
        sid = create(cur, symbol="AAPL", instrument_type="STOCK", direction="LONG", quantity=100)
        add_execution(cur, sid, side="BUY", quantity=100, price=90.0, executed_at="2026-09-15T10:00")
    pid = option(db, kind="PUT", strike=100.0, qty=1, price=4.0, expiration="2026-12-18")
    with db.tx() as cur:
        done = exercise(cur, pid, underlying_price=85.0, event_at="2026-10-05T15:00", apply_to_position_id=sid)
    assert done["stock_position_id"] == sid
    put, held = get(db, pid), get(db, sid)
    assert put["ledger"]["realized_pnl"] == pytest.approx((15 - 4) * 100)
    assert held["status"] == "CLOSED" and held["ledger"]["realized_pnl"] == pytest.approx(-5 * 100)
    assert put["ledger"]["realized_pnl"] + held["ledger"]["realized_pnl"] == pytest.approx((100 - 90 - 4) * 100)


def test_3_4_exercise_is_checked(db):
    pid = option(db, kind="CALL", strike=100.0, expiration="2026-12-18")
    with db.tx() as cur:
        with pytest.raises(PositionError, match="out of the money"):
            exercise(cur, pid, underlying_price=95.0, event_at="2026-10-05T15:00")
        with pytest.raises(PositionError, match="before the option was bought"):
            exercise(cur, pid, underlying_price=110.0, event_at="2026-09-01T15:00")
        with pytest.raises(PositionError, match="Only 2"):
            exercise(cur, pid, underlying_price=110.0, quantity=3, event_at="2026-10-05T15:00")
        other = create(cur, symbol="MSFT", instrument_type="STOCK", direction="LONG", quantity=1)
        with pytest.raises(PositionError, match="AAPL stock position"):
            exercise(cur, pid, underlying_price=110.0, event_at="2026-10-05T15:00", apply_to_position_id=other)
    past = option(db, expiration=PAST_EXPIRY)
    with db.tx() as cur:
        with pytest.raises(PositionError, match="after it expires"):
            exercise(cur, past, underlying_price=110.0, event_at="2026-10-05T15:00")
    assert db.one("SELECT COUNT(*) FROM option_events")[0] == 0


def test_3_4_contract_adjustments_keep_the_economics_and_the_original_terms(db):
    # A 3-for-2 split: each contract now delivers 150 shares at a strike of 100 instead of 100 at 150.
    pid = option(db, kind="CALL", strike=150.0, qty=1, price=6.0, expiration="2026-12-18", bought="2026-09-01T10:00")
    with db.tx() as cur:
        adjust_contract(cur, pid, effective_at="2026-09-10T09:30", new_strike=100.0, new_multiplier=150,
                        reason="3-for-2 stock split")
        add_execution(cur, pid, side="SELL", quantity=1, price=5.0, executed_at="2026-09-15T10:00")
    p = get(db, pid)
    assert p["ledger"]["realized_pnl"] == pytest.approx(1 * 150 * 5.0 - 1 * 100 * 6.0)
    assert p["legs"][0]["strike"] == 100.0 and p["multiplier"] == 150
    ev = p["option_events"][0]
    assert ev["previous"]["strike"] == 150.0 and ev["previous"]["multiplier"] == 100 and ev["reason"] == "3-for-2 stock split"
    assert p["executions"][0]["price"] == 6.0                                         # the fill is as recorded
    # A 2-for-1: twice the contracts at half the strike.
    two = option(db, kind="CALL", strike=200.0, qty=1, price=6.0, expiration="2026-12-18", bought="2026-09-01T10:00")
    with db.tx() as cur:
        adjust_contract(cur, two, effective_at="2026-09-10T09:30", new_strike=100.0, new_multiplier=100, quantity_factor=2,
                        reason="2-for-1 split")
        add_execution(cur, two, side="SELL", quantity=2, price=3.5, executed_at="2026-09-15T10:00")
    assert get(db, two)["ledger"]["realized_pnl"] == pytest.approx(2 * 100 * 3.5 - 100 * 6.0)
    with db.tx() as cur:
        with pytest.raises(PositionError, match="fraction of a contract"):
            adjust_contract(cur, option(db, qty=1, expiration="2026-12-18"), effective_at="2026-09-25T09:30",
                            new_strike=100.0, new_multiplier=100, quantity_factor=1.5, reason="bad")


def test_3_4_option_events_through_the_api(client):
    c, svc = client
    p = c.post("/v2/positions", json={"symbol": "AAPL", "instrument_type": "OPTION", "quantity": 1,
                                      "option": {"option_type": "CALL", "strike": 5000, "expiration": PAST_EXPIRY},
                                      "entry": {"price": 0.5, "executed_at": "2026-09-20T10:00"}}).json()
    assert p["lifecycle"]["state"] == "UNRESOLVED" and p["mark"]["expired"]
    r = c.post(f"/v2/positions/{p['id']}/option/expire", json={})                 # the stock price comes from the bars
    assert r.status_code == 200, r.text
    ev = r.json()["option_events"][0]
    assert ev["underlying_price_source"] == f"daily close on {PAST_EXPIRY}" and ev["underlying_price"] > 0
    assert r.json()["lifecycle"]["state"] == "EXPIRED_WORTHLESS"
    exp = (datetime.now(NY) + timedelta(days=60)).date().isoformat()
    q = c.post("/v2/positions", json={"symbol": "AAPL", "instrument_type": "OPTION", "quantity": 1,
                                      "option": {"option_type": "CALL", "strike": 1, "expiration": exp},
                                      "entry": {"price": 0.5, "executed_at": "2026-10-01T10:00"}}).json()
    ex = c.post(f"/v2/positions/{q['id']}/option/exercise", json={"underlying_price": 120.0, "executed_at": "2026-10-05T15:00"})
    assert ex.status_code == 200, ex.text
    stock = c.get(f"/v2/positions/{ex.json()['event']['stock_position_id']}").json()
    assert stock["ledger"]["open_quantity"] == 100 and stock["derived_from"][0]["event"] == "EXERCISED"
    tid = svc.journal.create(data_source="demo", symbol="KO", option_symbol="X", option_type="call", strike=60,
                             expiration=PAST_EXPIRY, contracts=1, thesis="t" * 20, planned_limit=1.0, max_limit=1.0,
                             plan={}, quote={}, scorecard=[], signals=[], overrides=[])
    svc.journal.log_attempt(tid, side="buy", bid=0.9, ask=1.1, limit_price=1.0, filled=True)
    mirror = svc.db.one("SELECT id FROM positions WHERE legacy_trade_id = ?", (tid,))["id"]
    r = c.post(f"/v2/positions/{mirror}/option/expire", json={})
    assert r.status_code == 400 and "option journal" in r.json()["error"]


def test_3_4_assignment_is_out_of_scope(db):
    # Only bought calls and puts can be recorded, so nothing can be assigned: the database refuses the event.
    import sqlite3
    pid = option(db)
    with pytest.raises(sqlite3.IntegrityError):
        with db.tx() as cur:
            cur.execute("INSERT INTO option_events (position_id, event, quantity, event_at, created_at) "
                        "VALUES (?, 'ASSIGNED', 1, '2026-10-02T20:00:00+00:00', '2026-10-02T20:00:00+00:00')", (pid,))
