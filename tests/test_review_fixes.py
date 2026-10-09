"""Regression tests for the findings of the independent review of 2.1.0 to 2.3.0 (fixed in 2.3.1).

Each test names the finding it covers; docs/IMPROVEMENTS.md lists them with their severity."""

from __future__ import annotations

import logging
import sqlite3
import sys
from datetime import datetime, timedelta
from pathlib import Path

import numpy as np
import pandas as pd
import pytest
from fastapi.testclient import TestClient

from tradeassist import events, freshness, portfolio, signalbook
from tradeassist.analytics import behaviour_detail, excursions
from tradeassist.api import create_app
from tradeassist.config import Settings
from tradeassist.db import Database
from tradeassist.optionmath import NY
from tradeassist.positions import (OptionLeg, PositionError, account, add_adjustment, add_execution, adjust_contract,
                                   create, expire_worthless, read, void_adjustment, Fill)
from tradeassist.providers.base import DataError
from tradeassist.providers.demo import DemoData
from tradeassist.reconcile import diagnose, repair
from tradeassist.service import TradeAssistant

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture()
def db():
    d = Database(":memory:")
    yield d
    d.close()


@pytest.fixture()
def app(tmp_path):
    settings = Settings(data_mode="demo", db_path=tmp_path / "t.db")
    svc = TradeAssistant(settings, provider=DemoData(), db=Database(tmp_path / "t.db"))
    return TestClient(create_app(settings, svc)), svc


def stock(db, direction="LONG", qty=100.0, symbol="KO"):
    with db.tx() as cur:
        return create(cur, symbol=symbol, instrument_type="STOCK", direction=direction, quantity=qty)


def fill(db, pid, side, qty, price, at, **kw):
    with db.tx() as cur:
        return add_execution(cur, pid, side=side, quantity=qty, price=price, executed_at=at, **kw)


def get(db, pid):
    with db.tx() as cur:
        return read(cur, pid)


# --- A1: one position whose fills no longer add up must not take down the rest ----------------------------

def broken_position(svc, client):
    p = client.post("/v2/positions", json={"symbol": "MSFT", "quantity": 1, "entry": {"price": 400, "executed_at": "2026-10-01T10:00"}}).json()
    with svc.db.tx() as cur:                       # as a 2.1.0 database could hold: a fill too small to account
        cur.execute("UPDATE executions SET quantity = 0.0000005 WHERE position_id = ?", (p["id"],))
    return p


def test_A1_an_unreadable_position_is_listed_apart_and_counted_as_unknown(app):
    c, svc = app
    good = c.post("/v2/positions", json={"symbol": "KO", "quantity": 10, "invalidation_price": 1,
                                         "entry": {"price": 60, "executed_at": "2026-10-01T10:00"}}).json()
    bad = broken_position(svc, c)
    listed = c.get("/v2/positions?status=open").json()
    assert [p["id"] for p in listed["positions"]] == [good["id"]]
    assert listed["invalid"][0]["id"] == bad["id"] and "too small" in listed["invalid"][0]["error"]
    assert listed["invalid"][0]["executions"][0]["quantity"] == pytest.approx(5e-7)
    risk = c.get("/v2/portfolio/risk").json()
    assert risk["coverage"]["status"] == "PARTIAL" and any(w.get("code") == "UNREADABLE_POSITION" for w in risk["warnings"])
    assert risk["risk_coverage"]["within_limit"] is not True and risk["daily_loss"]["coverage_status"] != "COMPLETE"
    r = c.get("/v2/diagnostics/reconciliation").json()
    assert [x["id"] for x in r["invalid_states"]] == [bad["id"]]
    assert c.post("/v2/diagnostics/repair", json={"confirm": True}).status_code == 200
    h = c.get("/v2/health/data").json()
    assert h["unreadable_positions"][0]["id"] == bad["id"]
    # Correcting the wrong fill brings it back.
    eid = listed["invalid"][0]["executions"][0]["id"]
    fixed = c.post(f"/v2/positions/{bad['id']}/executions/{eid}/correct", json={"quantity": 1, "reason": "typo in 2.1"})
    assert fixed.status_code == 200 and fixed.json()["ledger"]["open_quantity"] == 1
    assert c.get("/v2/positions?status=open").json()["invalid"] == []


def test_A1_repair_of_a_mirror_with_a_stray_fill_reads_the_journal_and_says_so(app):
    _, svc = app
    tid = svc.journal.create(data_source="demo", symbol="AAPL", option_symbol="X", option_type="call", strike=200,
                             expiration="2026-12-18", contracts=1, thesis="t" * 20, planned_limit=2.0, max_limit=2.0,
                             plan={}, quote={}, scorecard=[], signals=[], overrides=[])
    svc.journal.log_attempt(tid, side="buy", bid=1.9, ask=2.1, limit_price=2.0, filled=True)
    pid = svc.db.one("SELECT id FROM positions WHERE legacy_trade_id = ?", (tid,))[0]
    with svc.db.tx() as cur:                       # a stray sale dated before the purchase: the mirror no longer accounts
        cur.execute("INSERT INTO executions (position_id, side, quantity, price, fees, executed_at, created_at) "
                    "VALUES (?, 'SELL', 1, 3.0, 0, '2026-01-01T15:00:00+00:00', 'x')", (pid,))
    r = diagnose(svc.db)
    assert r["journal_mismatches"][0]["trade_id"] == tid
    done = repair(svc.db)
    assert done["journal_resynced"] == [tid] and done["failed"] == [] and "JOURNAL_SYNC" in done["note"]
    assert [x[0] for x in svc.db.query("SELECT side FROM executions WHERE position_id = ?", (pid,))] == ["BUY"]
    assert svc.db.one("SELECT action, source FROM execution_corrections WHERE position_id = ?", (pid,))[:] == ("DELETE", "JOURNAL_SYNC")


# --- A2: a change of units must not quietly reopen a closed position ------------------------------------

def test_A2_a_split_that_would_reopen_a_closed_position_needs_confirmation(db):
    pid = stock(db)
    fill(db, pid, "BUY", 100, 100.0, "2026-06-01T10:00")
    fill(db, pid, "SELL", 100, 110.0, "2026-06-20T10:00")
    with pytest.raises(PositionError, match="confirm_reopen"):
        with db.tx() as cur:
            add_adjustment(cur, pid, "SPLIT", ratio=2, effective_at="2026-06-10")
    assert get(db, pid)["status"] == "CLOSED" and get(db, pid)["adjustments"] == []          # nothing written
    with pytest.raises(PositionError, match="Say why"):
        with db.tx() as cur:
            add_adjustment(cur, pid, "SPLIT", ratio=2, effective_at="2026-06-10", confirm_reopen=True)
    with db.tx() as cur:
        add_adjustment(cur, pid, "SPLIT", ratio=2, effective_at="2026-06-10", confirm_reopen=True,
                       note="the sale was 100 post-split shares")
    p = get(db, pid)
    assert p["status"] == "PARTIALLY_CLOSED" and p["ledger"]["open_quantity"] == 100
    c = p["corrections"][-1]
    assert c["execution_id"] is None and c["source"] == "USER" and "split" in c["new"]["adjustment"]


def test_A2_voiding_a_split_and_adjusting_a_contract_are_guarded_too(db):
    pid = stock(db)
    fill(db, pid, "BUY", 100, 100.0, "2026-06-01T10:00")
    with db.tx() as cur:
        aid = add_adjustment(cur, pid, "SPLIT", ratio=0.5, effective_at="2026-06-10")
    fill(db, pid, "SELL", 50, 210.0, "2026-06-20T10:00")
    assert get(db, pid)["status"] == "CLOSED"
    with pytest.raises(PositionError, match="confirm_reopen"):
        with db.tx() as cur:
            void_adjustment(cur, pid, aid, "entered by mistake")
    assert get(db, pid)["status"] == "CLOSED" and not get(db, pid)["adjustments"][0]["voided_at"]
    with db.tx() as cur:
        oid = create(cur, symbol="AAPL", instrument_type="OPTION", direction="LONG", quantity=1,
                     option=OptionLeg("CALL", 200.0, "2026-12-18"))
    fill(db, oid, "BUY", 1, 3.0, "2026-06-01T10:00")
    fill(db, oid, "SELL", 1, 4.0, "2026-06-20T10:00")
    with pytest.raises(PositionError, match="confirm_reopen"):
        with db.tx() as cur:
            adjust_contract(cur, oid, effective_at="2026-06-10", new_strike=100.0, new_multiplier=100.0,
                            quantity_factor=2.0, reason="2-for-1 split")
    with db.tx() as cur:                            # a change that keeps it closed needs nothing more
        adjust_contract(cur, oid, effective_at="2026-06-10", new_strike=199.0, new_multiplier=100.0, reason="special dividend")
    assert get(db, oid)["status"] == "CLOSED"


# --- A3, A4, A6: splits and remainders ------------------------------------------------------------------

def test_A3_a_split_entered_as_a_date_applies_to_fills_made_that_day(db):
    pid = stock(db, qty=200)
    fill(db, pid, "BUY", 100, 100.0, "2026-06-09T10:00")
    fill(db, pid, "BUY", 100, 50.0, "2026-06-10T11:00")             # on the ex-date: already in post-split shares
    with db.tx() as cur:
        add_adjustment(cur, pid, "SPLIT", ratio=2, effective_at="2026-06-10")
    led = get(db, pid)["ledger"]
    assert led["open_quantity"] == pytest.approx(300) and led["cost_basis"] == pytest.approx(50.0)


def test_A4_a_remainder_too_small_to_sell_closes_with_the_fill_that_left_it():
    led = account("LONG", 1, [Fill("BUY", 1, 10.0, 0, "a"), Fill("SELL", 0.9999995, 11.0, 0, "b")])
    assert led.status() == "CLOSED" and led.open_quantity == 0 and led.rounded_off == pytest.approx(5e-7)
    assert led.realized_pnl == pytest.approx(1.0)
    # A split's fraction: 10 shares 1-for-3 is 3.3333...; the broker sells 3.333333 (six decimals).
    led = account("LONG", 1, [Fill("BUY", 10 / 3, 30.0, 0, "a"), Fill("SELL", 3.333333, 33.0, 0, "b")])
    assert led.status() == "CLOSED"
    led = account("LONG", 1, [Fill("BUY", 10 / 3, 30.0, 0, "a"), Fill("SELL", 3.333334, 33.0, 0, "b")])   # rounded up
    assert led.status() == "CLOSED" and led.closed_quantity == pytest.approx(10 / 3)
    with pytest.raises(PositionError, match="only 1 is open"):
        account("LONG", 1, [Fill("BUY", 1, 10.0, 0, "a"), Fill("SELL", 1.01, 11.0, 0, "b")])    # a real over-sale


def test_A6_voiding_a_split_keeps_later_splits_and_plan_edits(db):
    pid = stock(db)
    with db.tx() as cur:
        cur.execute("UPDATE positions SET invalidation_price = 90, target_1 = 150 WHERE id = ?", (pid,))
    fill(db, pid, "BUY", 100, 100.0, "2026-06-01T10:00")
    with db.tx() as cur:
        first = add_adjustment(cur, pid, "SPLIT", ratio=2, effective_at="2026-06-10")
        add_adjustment(cur, pid, "SPLIT", ratio=3, effective_at="2026-07-10")
        void_adjustment(cur, pid, first, "only the 3-for-1 happened")
    p = get(db, pid)
    assert p["quantity"] == pytest.approx(300) and p["invalidation_price"] == pytest.approx(30)
    assert p["target_1"] == pytest.approx(50) and p["ledger"]["open_quantity"] == pytest.approx(300)


# --- A5: borrow fees ----------------------------------------------------------------------------------------

def test_A5_borrow_fees_with_a_gap_are_partial(db):
    pid = stock(db, "SHORT")
    fill(db, pid, "SELL_SHORT", 100, 50.0, "2026-01-02T10:00")
    fill(db, pid, "BUY_TO_COVER", 100, 45.0, "2026-03-31T10:00")
    with db.tx() as cur:
        add_adjustment(cur, pid, "BORROW_FEE", amount=-30, effective_at="2026-01-31T16:00",
                       period_start="2026-01-02T10:00", period_end="2026-01-31T16:00")
        add_adjustment(cur, pid, "BORROW_FEE", amount=-30, effective_at="2026-03-31T16:00",
                       period_start="2026-03-01T10:00", period_end="2026-03-31T16:00")
    fin = get(db, pid)["financing"]
    assert fin["borrow_status"] == "PARTIAL" and "28 of the 88 nights" in fin["borrow_text"]
    with db.tx() as cur:
        add_adjustment(cur, pid, "BORROW_FEE", amount=-30, effective_at="2026-02-28T16:00",
                       period_start="2026-02-01T10:00", period_end="2026-02-28T16:00")
    assert get(db, pid)["financing"]["borrow_status"] == "RECORDED"


# --- A8, A9, A10, A11 ------------------------------------------------------------------------------------------

def test_A8_a_back_dated_fill_cannot_move_a_reopening(tmp_path):
    path = tmp_path / "old.db"
    conn = sqlite3.connect(path)
    conn.executescript((FIXTURES / "db_2_1_0.sql").read_text())
    conn.close()
    d = Database(path)
    ko = d.one("SELECT id FROM positions WHERE symbol = 'KO' AND legacy_trade_id IS NULL")[0]
    fills = [tuple(r) for r in d.query("SELECT side, executed_at FROM executions WHERE position_id = ? ORDER BY executed_at", (ko,))]
    sell_at = next(at for side, at in fills if side == "SELL")
    gap = (datetime.fromisoformat(sell_at) + timedelta(hours=2)).isoformat()
    with pytest.raises(PositionError, match="closed position is finished"):
        fill(d, ko, "BUY", 3, 60.0, gap)
    d.close()


def test_A9_a_journal_mirror_thesis_is_edited_in_the_journal(app):
    c, svc = app
    tid = svc.journal.create(data_source="demo", symbol="AAPL", option_symbol="X", option_type="call", strike=200,
                             expiration="2026-12-18", contracts=1, thesis="t" * 20, planned_limit=2.0, max_limit=2.0,
                             plan={}, quote={}, scorecard=[], signals=[], overrides=[])
    pid = svc.db.one("SELECT id FROM positions WHERE legacy_trade_id = ?", (tid,))[0]
    r = c.post(f"/v2/positions/{pid}/plan", json={"thesis": "edited here"})
    assert r.status_code == 400 and "option journal" in r.json()["error"]
    assert c.post(f"/v2/positions/{pid}/plan", json={"invalidation_price": 150}).status_code == 200


def test_A10_expiring_worthless_without_a_stock_price_needs_a_reason(db):
    with db.tx() as cur:
        oid = create(cur, symbol="AAPL", instrument_type="OPTION", direction="LONG", quantity=1,
                     option=OptionLeg("CALL", 200.0, "2026-10-02"))
    fill(db, oid, "BUY", 1, 3.0, "2026-09-20T10:00")
    with pytest.raises(PositionError, match="not saved here"):
        with db.tx() as cur:
            expire_worthless(cur, oid)
    with db.tx() as cur:
        expire_worthless(cur, oid, reason="closed at 180 on the broker statement")
    assert get(db, oid)["status"] == "CLOSED"


def test_A11_each_option_leg_field_has_its_own_rule(db):
    with db.tx() as cur:
        oid = create(cur, symbol="AAPL", instrument_type="OPTION", direction="LONG", quantity=1,
                     option=OptionLeg("CALL", 200.0, "2026-12-18"))
    names = {r[0] for r in db.query("SELECT name FROM sqlite_master WHERE type = 'trigger'")}
    assert {"check_option_strike_update", "check_option_multiplier_update", "check_option_expiration_update"} <= names
    assert "check_option_contracts_update" not in names
    with pytest.raises(sqlite3.IntegrityError, match="strike above zero"):
        with db.tx() as cur:
            cur.execute("UPDATE option_contracts SET strike = -5 WHERE position_id = ?", (oid,))


# --- B1, B2, B3, B7: the ticket and the daily stop ----------------------------------------------------------

def test_B1_the_ticket_counts_open_risk_on_the_positions_screen(app):
    c, svc = app
    spot = svc.market.spot("KO")
    c.post("/v2/positions", json={"symbol": "KO", "quantity": 200, "invalidation_price": round(spot * 0.8, 2),
                                  "entry": {"price": round(spot, 2)}})
    c.post("/v2/positions", json={"symbol": "MSFT", "quantity": 1, "entry": {"price": 400}})     # risk unknown
    exp = c.get("/api/expirations", params={"symbol": "AAPL"}).json()["expirations"][3]["date"]
    chain = c.get("/api/chain", params={"symbol": "AAPL", "expiration": exp}).json()
    k = min(chain["contracts"], key=lambda x: abs(x["moneyness_pct"] - 8))
    t = c.post("/api/ticket", json={"symbol": "AAPL", "expiration": exp, "option_type": k["option_type"], "strike": k["strike"],
                                    "contracts": 1, "limit_price": k["ask"], "thesis": "A thesis long enough to pass the check."}).json()
    check = next(x for x in t["priced"]["scorecard"]["checks"] if x["key"] == "open_risk")
    assert check["status"] == "fail" and "open_risk" in [v["key"] for v in t["violations"]] and not t["can_save"]
    assert any(n["key"] == "open_risk_incomplete" and "MSFT" in n["text"] for n in t["risk_notes"])


def test_B2_unreadable_positions_never_make_a_complete_day(app, monkeypatch):
    _, svc = app

    def fail(*a, **k):
        raise DataError("history service down")
    monkeypatch.setattr(svc.analyst, "daily_loss", fail)
    d = svc.daily_loss_status()
    assert d["coverage_status"] == "UNAVAILABLE" and d["status"] == "INCOMPLETE" and "not known" in d["text"]


def test_B3_a_stale_price_does_not_measure_today():
    start = datetime(2026, 10, 7, tzinfo=NY)
    v = {"id": 1, "description": "Long 10 KO", "direction": "LONG", "multiplier": 1.0, "unit_adjustments": [],
         "executions": [{"side": "BUY", "quantity": 10, "price": 60.0, "fees": 0, "executed_at": "2026-10-01T14:00:00+00:00"}],
         "prev_close": 61.0, "mark": {"price": 70.0, "freshness": {"freshness": "STALE", "from_cache": True,
                                                                   "warnings": ["a saved copy"]}}}
    d = portfolio.day_pnl(v, start)
    assert d["known"] is False and "saved copy" in d["reason"]
    v["mark"]["freshness"] = {"freshness": "STALE", "warnings": ["10 minutes old"]}   # the live source's quote: today's market
    assert portfolio.day_pnl(v, start)["unrealized_change"] == pytest.approx(90.0)


def test_B7_a_previous_close_from_history_that_could_not_be_refreshed_is_unknown(app, monkeypatch):
    c, svc = app
    c.post("/v2/positions", json={"symbol": "KO", "quantity": 10, "entry": {"price": 60, "executed_at": "2026-09-01T10:00"}})
    monkeypatch.setattr(svc.market, "bar_info", lambda s: {"refresh_failed": "timeout"})
    assert svc.analyst.daily_loss()["coverage_status"] == "COMPLETE"     # the saved history still reaches yesterday
    real = svc.market.bars
    monkeypatch.setattr(svc.market, "bars", lambda s, *a, **k: (lambda b: b[b["date"] < b["date"].max() - pd.Timedelta(days=7)])(real(s, *a, **k)))
    d = svc.analyst.daily_loss()
    assert d["coverage_status"] == "UNAVAILABLE" and "could not be refreshed" in d["missing"][0]["reason"]


# --- B4, B15, B16: risk figures ---------------------------------------------------------------------------------

def test_B4_the_bad_day_names_holdings_it_leaves_out():
    rng = np.random.default_rng(1)
    rets = pd.DataFrame({"AAPL": rng.normal(0, 0.01, 120), "SPY": rng.normal(0, 0.01, 120)})
    r = portfolio.one_day_risk(rets, {"AAPL": 1_000.0, "KO": 60_000.0})
    assert r["available"] and not r["complete"] and r["left_out"][0]["symbol"] == "KO"
    assert r["share_of_exposure_measured"] == pytest.approx(1_000 / 61_000) and "KO" in r["note"]


def test_B15_a_total_nothing_could_measure_is_unknown():
    daily = portfolio.daily_loss([], "REALIZED_PLUS_UNREALIZED", 10_000, 3.0)
    v = {"id": 1, "symbol": "A", "direction": "LONG", "instrument_type": "STOCK", "multiplier": 1, "description": "A",
         "bias": 1, "invalidation_price": None, "risk_amount": None, "thesis_check": {"status": "UNKNOWN"}, "legs": [],
         "ledger": {"open_quantity": 5}, "status": "OPEN", "mark": {"price": None, "error": "no quote"}, "executions": []}
    t = portfolio.analyse([v], {}, 10_000, 10.0, daily, lambda s: None)["totals"]
    assert t["estimated_liquidation_value"] is None and t["market_value"] is None and t["theta_per_day"] is None


def test_B16_a_bought_option_without_a_volatility_loses_at_most_its_value():
    x = {"id": 1, "symbol": "AAPL", "description": "call", "instrument_type": "OPTION", "direction": "LONG", "priced": True,
         "beta": 1.0, "underlying": 100.0, "price": 1.0, "multiplier": 100.0, "open_quantity": 1, "dollar_delta": 5_000.0,
         "market_value": 100.0, "option": None}
    s = portfolio.stress([x], [{"name": "crash", "market_move_pct": -20, "iv_shift_points": 20}], 10_000)[0]
    assert s["pnl"] == pytest.approx(-100.0)


# --- B5, B8: credentials and other web pages ---------------------------------------------------------------------

def test_B5_credentials_are_redacted_everywhere(app, monkeypatch, caplog):
    c, svc = app
    secret = "Authorization: Bearer shortTok123"

    def leak(*a, **k):
        raise DataError(f"HTTP 401 sent {secret}")
    monkeypatch.setattr(svc.market.provider, "expirations", leak)
    monkeypatch.setattr(svc.market.provider, "quotes", leak)
    bodies = [c.get("/api/expirations", params={"symbol": "AAPL"}).text, c.get("/v2/data/AAPL").text,
              c.get("/v2/source/health").text, c.get("/v2/health/data").text]
    assert all("shortTok123" not in b for b in bodies) and "shortTok123" not in caplog.text
    for raw, gone in (('{"access_token": "abc"}', "abc"), ('{"token":"s3cr3t"}', "s3cr3t"), ("Bearer abc/def+ghi=", "def+ghi"),
                      ("eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjMifQ.ab_c-d", "ab_c-d"), ("cb?code=xyz123&x=1", "xyz123"),
                      ("pwd=hunter2", "hunter2"), ("passwd=hunter2", "hunter2"), ("token 'abc123'", "abc123")):
        assert gone not in events.redact(raw), raw
    assert events.redact("Tradier token is missing") == "Tradier token is missing"
    try:
        raise RuntimeError("request failed; headers={'Authorization': 'Bearer Zz9'} password=hunter2")
    except RuntimeError:
        record = logging.LogRecord("x", logging.ERROR, "", 0, "Unhandled error", None, sys.exc_info())
    events.RedactingFilter().filter(record)
    text = logging.Formatter().format(record)
    assert "Zz9" not in text and "hunter2" not in text and "Traceback" in text


def test_B8_other_hosts_and_other_pages_cannot_change_anything(app):
    c, _ = app
    assert c.get("/v2/positions", headers={"Host": "attacker.example:8000"}).status_code == 400
    evil = c.post("/v2/diagnostics/repair", json={"confirm": True}, headers={"Origin": "https://evil.example"})
    assert evil.status_code == 403 and "another web page" in evil.json()["error"]
    assert c.post("/v2/positions/1/snapshot", headers={"Sec-Fetch-Site": "cross-site"}).status_code == 403
    assert c.post("/v2/diagnostics/repair", json={"confirm": True}, headers={"Origin": "http://testserver"}).status_code == 200
    assert c.get("/v2/positions", headers={"Origin": "https://evil.example"}).status_code == 200     # reading is harmless


# --- B6, B14: calibration and intervals -----------------------------------------------------------------------

def test_B6_the_held_out_dates_are_not_part_of_the_check():
    rng = np.random.default_rng(3)
    n = 8000
    net = rng.uniform(-100, 100, n)
    dates = np.tile(pd.bdate_range("2018-01-01", periods=n // 4).to_numpy(), 4)
    late = pd.Series(dates) >= pd.Series(dates).quantile(0.7)
    effect = np.where(late.to_numpy(), 0.002, 0.0) * net                       # only the latest 30% has a relationship
    days = pd.DataFrame({"date": dates, "net": net, "regime": "BULL_CALM", "fwd_10": rng.normal(effect, 0.03),
                         "spy_10": 0.0, "fwd_20": np.nan, "spy_20": np.nan, "fwd_5": np.nan, "spy_5": np.nan})
    c = signalbook.calibrate(days)
    assert c["calibration_status"] == "NOT_CALIBRATED" and not c["calibrated"] and c["checked_before"]


def test_B14_one_period_holding_most_cases_counts_as_little_more_than_one():
    lo, hi = signalbook.effective_interval(100, 109, [100] + [1] * 9)
    assert hi - lo > 0.6                                                     # close to one observation: very wide
    equal = signalbook.effective_interval(60, 100, [10] * 10)
    assert equal == pytest.approx(signalbook.effective_interval(60, 100, 10))   # equal periods: their count


# --- B9: research fingerprints ---------------------------------------------------------------------------------

def test_B9_a_run_fingerprints_the_benchmarks_and_regime_indexes_it_read(app):
    _, svc = app
    with svc.market.recording() as seen:
        svc.analyst._backtest_inputs(["AAPL"], 1.0)
    assert {"AAPL", "SPY", "QQQ"} <= set(seen)
    assert svc.market.bars("SPY", 1.0) is not None and getattr(svc.market._recorder, "seen", None) is None   # stopped


# --- B10, B11, B12: freshness -----------------------------------------------------------------------------------

def ny(*a):
    return datetime(*a, tzinfo=NY)


def test_B10_a_quote_is_as_old_as_its_bid_and_ask():
    now = ny(2026, 10, 7, 11, 0)
    live = {"bid_time": (now - timedelta(seconds=20)).isoformat(), "ask_time": (now - timedelta(seconds=10)).isoformat(),
            "trade_time": (now - timedelta(days=2)).isoformat()}
    assert freshness.quote_time(live) == (now - timedelta(seconds=20)).isoformat()
    old = {"bid_time": (now - timedelta(hours=5)).isoformat(), "ask_time": (now - timedelta(hours=5)).isoformat(),
           "trade_time": now.isoformat()}
    f = freshness.observe("OPTION_QUOTE", "X", "t", freshness.quote_time(old), retrieved_at=now, session="OPEN")
    assert f["freshness"] == "STALE"
    assert freshness.quote_time({"trade_time": "2026-10-07T10:00:00-04:00"}) == "2026-10-07T10:00:00-04:00"


def test_B11_a_time_in_the_future_is_unknown_not_fresh():
    now = ny(2026, 10, 7, 11, 0)
    f = freshness.observe("STOCK_QUOTE", "AAPL", "t", (now + timedelta(days=1)).isoformat(), retrieved_at=now, session="OPEN")
    assert f["freshness"] == "UNKNOWN" and any("future" in w for w in f["warnings"])


def test_B12_a_monday_holiday_does_not_make_fridays_close_stale():
    friday_close = ny(2026, 9, 4, 16, 0)
    for now in (ny(2026, 9, 7, 17, 0), ny(2026, 9, 8, 8, 0)):            # Labor Day evening, Tuesday before the open
        f = freshness.observe("DAILY_BARS", "AAPL", "t", friday_close.isoformat(), retrieved_at=now, session="CLOSED")
        assert f["freshness"] == "LAST_SESSION"
    f = freshness.observe("DAILY_BARS", "AAPL", "t", ny(2026, 9, 3, 16, 0).isoformat(), retrieved_at=ny(2026, 9, 8, 8, 0),
                          session="CLOSED")
    assert f["freshness"] == "STALE"                                     # Thursday's is two sessions old


# --- B13: trade reviews never use the range after the exit -----------------------------------------------------------

def test_B13_the_exit_days_range_after_an_intraday_exit_is_not_counted():
    bars = pd.DataFrame({"date": pd.to_datetime(["2026-09-01", "2026-09-02", "2026-09-03"]), "open": [100, 101, 102],
                         "high": [101, 103, 112], "low": [99, 100, 101], "close": [100, 102, 111], "volume": [1e6] * 3})
    pos = {"instrument_type": "STOCK", "direction": "LONG", "bias": 1, "status": "CLOSED",
           "opened_at": "2026-09-01T14:00:00+00:00", "closed_at": "2026-09-03T13:35:00+00:00",        # sold 09:35 New York
           "ledger": {"average_entry_price": 100.0, "initial_risk": 500}, "invalidation_price": 95.0, "target_1": 110.0,
           "executions": [{"side": "BUY", "price": 100.0}, {"side": "SELL", "price": 102.5}], "snapshots": []}
    x = excursions(pos, bars)
    assert x["reached_target_1"] is False and x["mfe_pct"] == pytest.approx(3.0)
    assert behaviour_detail(pos, x, 1000)["exited_before_target"] is True
    at_close = {**pos, "closed_at": "2026-09-03T20:00:00+00:00"}          # sold at the 4 p.m. close: the day counts
    assert excursions(at_close, bars)["reached_target_1"] is True


def test_B5_uvicorns_loggers_are_protected_too():
    root_handler = logging.StreamHandler()
    logging.getLogger().addHandler(root_handler)
    try:
        events.protect_logging("uvicorn.error")
        events.protect_logging("uvicorn.error")                         # idempotent
        assert sum(isinstance(f, events.RedactingFilter) for f in logging.getLogger("uvicorn.error").filters) == 1
        assert any(isinstance(f, events.RedactingFilter) for f in root_handler.filters)
        record = logging.LogRecord("uvicorn.error", logging.ERROR, "", 0, "boom", None, None)
        record.exc_text = "Traceback ... Authorization: Bearer Qq1"
        logging.getLogger("uvicorn.error").filters[0].filter(record)
        assert "Qq1" not in record.exc_text
    finally:
        logging.getLogger().removeHandler(root_handler)
        lg = logging.getLogger("uvicorn.error")
        for f in [f for f in lg.filters if isinstance(f, events.RedactingFilter)]:
            lg.removeFilter(f)


# --- Follow-up: problems the reviewers found in the first round of fixes ----------------------------------------------

def test_followup_a_remainder_followed_by_a_purchase_is_an_addition_as_recorded():
    led = account("LONG", 1, [Fill("BUY", 1, 10.0, 0, "a"), Fill("SELL", 0.9999995, 11.0, 0, "b"), Fill("BUY", 10, 12.0, 0, "c")])
    assert led.lifecycle_issues == [] and led.open_quantity == pytest.approx(10.0000005)


def test_followup_a_plan_save_from_the_screen_works_on_a_journal_mirror(app):
    c, svc = app
    tid = svc.journal.create(data_source="demo", symbol="AAPL", option_symbol="X", option_type="call", strike=200,
                             expiration="2026-12-18", contracts=1, thesis="t" * 20, planned_limit=2.0, max_limit=2.0,
                             plan={}, quote={}, scorecard=[], signals=[], overrides=[])
    pid = svc.db.one("SELECT id FROM positions WHERE legacy_trade_id = ?", (tid,))[0]
    pos = c.get(f"/v2/positions/{pid}").json()
    same = c.post(f"/v2/positions/{pid}/plan", json={"thesis": pos["thesis"], "notes": pos["notes"], "invalidation_price": 180})
    assert same.status_code == 200 and same.json()["invalidation_price"] == 180


def test_followup_a_contract_adjustment_on_a_date_applies_to_that_days_fills(db):
    with db.tx() as cur:
        oid = create(cur, symbol="AAPL", instrument_type="OPTION", direction="LONG", quantity=3,
                     option=OptionLeg("CALL", 200.0, "2026-12-18"))
    fill(db, oid, "BUY", 1, 4.0, "2026-06-09T10:00")
    fill(db, oid, "BUY", 2, 2.0, "2026-06-10T11:00")              # on the ex-date: already adjusted contracts
    with db.tx() as cur:
        adjust_contract(cur, oid, effective_at="2026-06-10", new_strike=100.0, new_multiplier=100.0, quantity_factor=2.0,
                        reason="2-for-1 split")
    led = get(db, oid)["ledger"]
    assert led["open_quantity"] == pytest.approx(4) and led["cost_basis"] == pytest.approx(2.0)


def test_followup_adjustments_cannot_split_an_open_position_into_two_trades(db):
    pid = stock(db, qty=200)
    fill(db, pid, "BUY", 100, 100.0, "2026-06-01T10:00")
    fill(db, pid, "SELL", 50, 110.0, "2026-06-20T10:00")
    fill(db, pid, "BUY", 20, 105.0, "2026-06-25T10:00")
    with pytest.raises(PositionError, match="two trades"):
        with db.tx() as cur:                       # 1-for-2 before the sale: 50 shares, all sold, then a new buy
            add_adjustment(cur, pid, "SPLIT", ratio=0.5, effective_at="2026-06-10")
    assert get(db, pid)["adjustments"] == [] and get(db, pid)["ledger"]["lifecycle_issues"] == []


def test_followup_correcting_an_unreadable_position_cannot_create_a_reopening(db):
    pid = stock(db)
    fill(db, pid, "BUY", 10, 100.0, "2026-06-01T10:00")
    fill(db, pid, "SELL", 10, 110.0, "2026-06-02T10:00")
    with db.tx() as cur:                           # a stray sale, as an older version could leave
        cur.execute("INSERT INTO executions (position_id, side, quantity, price, fees, executed_at, created_at) "
                    "VALUES (?, 'SELL', 5, 111, 0, '2026-06-03T14:00:00+00:00', 'x')", (pid,))
    eid = db.one("SELECT MAX(id) FROM executions")[0]
    from tradeassist.positions import correct_execution
    with pytest.raises(PositionError, match="two trades"):
        with db.tx() as cur:
            correct_execution(cur, pid, eid, {"side": "BUY"}, "it was a buy")


def test_followup_an_option_on_a_held_stock_is_not_blocked_by_the_shares_risk(app):
    c, svc = app
    spot = svc.market.spot("AAPL")
    c.post("/v2/positions", json={"symbol": "AAPL", "quantity": 20, "invalidation_price": round(spot * 0.85, 2),
                                  "entry": {"price": round(spot, 2)}})
    exp = c.get("/api/expirations", params={"symbol": "AAPL"}).json()["expirations"][3]["date"]
    chain = c.get("/api/chain", params={"symbol": "AAPL", "expiration": exp}).json()
    k = min((x for x in chain["contracts"] if x["option_type"] == "put"), key=lambda x: abs(x["moneyness_pct"] + 5))
    t = c.post("/api/ticket", json={"symbol": "AAPL", "expiration": exp, "option_type": "put", "strike": k["strike"],
                                    "contracts": 1, "limit_price": k["ask"], "thesis": "A protective put on the shares I hold."}).json()
    trade = next(x for x in t["priced"]["scorecard"]["checks"] if x["key"] == "trade_risk")
    assert trade["status"] == "pass" and t["priced"]["book_risk"]["symbol"] > 0


def test_followup_ticket_risk_from_a_saved_quote_is_unknown(app):
    c, svc = app
    c.post("/v2/positions", json={"symbol": "KO", "quantity": 10, "invalidation_price": 1, "entry": {"price": 60}})
    svc.market.quote("KO")
    real = svc.market.provider.quotes

    def down(symbols):
        if "KO" in symbols:
            raise DataError("Tradier request failed (timeout)")
        return real(symbols)
    svc.market.provider.quotes = down
    book = svc.book_risk()
    assert book["total"] == 0 and "saved copy" in book["unknown"][0]["reason"]
    q = svc.market.quote("KO", allow_stale=True)["_meta"]
    assert q["from_cache"] and q["saved_at"] and q["age_seconds"] >= 0


def test_followup_an_open_positions_excursions_include_today():
    bars = pd.DataFrame({"date": pd.to_datetime(["2026-09-01", "2026-09-02"]), "open": [100, 100], "high": [101, 100],
                         "low": [99, 85], "close": [100, 86], "volume": [1e6] * 2})
    pos = {"instrument_type": "STOCK", "direction": "LONG", "bias": 1, "status": "OPEN", "opened_at": "2026-09-01T14:00:00+00:00",
           "closed_at": None, "ledger": {"average_entry_price": 100.0}, "invalidation_price": 90.0, "target_1": None,
           "snapshots": []}
    x = excursions(pos, bars, until="2026-09-02T15:00:00+00:00")
    assert x["mae_pct"] == pytest.approx(-15.0)


def test_followup_an_unreadable_position_closed_before_today_does_not_touch_today(app):
    c, svc = app
    p = c.post("/v2/positions", json={"symbol": "MSFT", "quantity": 1, "entry": {"price": 400, "executed_at": "2026-09-01T10:00"}}).json()
    c.post(f"/v2/positions/{p['id']}/executions", json={"side": "SELL", "quantity": 1, "price": 410, "executed_at": "2026-09-02T10:00"})
    with svc.db.tx() as cur:
        cur.execute("UPDATE executions SET fees = 0.0000001, quantity = 0.0000005 WHERE position_id = ? AND side = 'BUY'", (p["id"],))
    assert c.get("/v2/positions").json()["invalid"]
    assert svc.analyst.daily_loss()["coverage_status"] == "COMPLETE"


def test_followup_repair_logs_what_it_did_even_when_an_item_fails(app, monkeypatch):
    _, svc = app
    import tradeassist.reconcile as rc
    p = svc.desk.create({"symbol": "KO", "quantity": 1, "entry": {"price": 60, "executed_at": "2026-10-01T10:00"}})
    with svc.db.tx() as cur:
        cur.execute("UPDATE positions SET realized_pnl = 5 WHERE id = ?", (p["id"],))

    def refuse(cur, pid, correction=False):
        raise sqlite3.IntegrityError("a rule says no")
    monkeypatch.setattr(rc, "recompute", refuse)
    done = repair(svc.db)
    assert done["failed"][0]["problem"] == "a rule says no"
    assert "a rule says no" in svc.db.one("SELECT details_json FROM maintenance_log ORDER BY id DESC")[0]


def test_followup_an_open_short_with_fees_through_yesterday_is_recorded(db):
    pid = stock(db, "SHORT")
    now = datetime.now(NY)
    opened = (now - timedelta(days=10)).replace(hour=10, minute=0, second=0, microsecond=0)
    fill(db, pid, "SELL_SHORT", 100, 50.0, opened.isoformat())
    with db.tx() as cur:
        add_adjustment(cur, pid, "BORROW_FEE", amount=-5, effective_at=(now - timedelta(days=1)).isoformat(),
                       period_start=opened.isoformat(), period_end=(now - timedelta(days=1)).isoformat())
    assert get(db, pid)["financing"]["borrow_status"] == "RECORDED"


def test_followup_uvicorns_access_lines_keep_their_arguments():
    r = logging.LogRecord("uvicorn.access", logging.INFO, "", 0, '%s - "%s %s HTTP/%s" %d',
                          ("127.0.0.1:5", "GET", "/cb?code=abc123", "1.1", 200), None)
    events.RedactingFilter().filter(r)
    assert len(r.args) == 5 and "abc123" not in r.getMessage()
