"""Regression tests for the P0 defects fixed in 2.1.1 (docs/IMPROVEMENTS.md, Sprint 1).

1.1 portfolio risk with positions that have no price; 1.2 closed positions are finished;
1.3 today's loss kept apart from how much of it could be measured; 1.4 no NaN or infinity
anywhere in the accounts. Each test names the defect it guards.
"""

from __future__ import annotations

import math
import sqlite3
from datetime import datetime
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from tradeassist import portfolio
from tradeassist.alerts import account_alerts
from tradeassist.api import create_app
from tradeassist.config import Settings, load_settings
from tradeassist.db import Database
from tradeassist.journal import Journal, JournalError
from tradeassist.optionmath import NY
from tradeassist.positions import (CLOSED_MESSAGE, Fill, OptionLeg, PositionError, account, add_execution, create,
                                   read, recompute, update_plan)
from tradeassist.providers.base import DataError
from tradeassist.providers.demo import DemoData
from tradeassist.service import TradeAssistant

NOW = datetime(2026, 10, 6, 14, 0, tzinfo=NY)
FIXTURES = Path(__file__).parent / "fixtures"


def view(symbol, direction="LONG", qty=100, price=100.0, *, under=None, prev=None, fills=(), invalidation=None,
         kind="STOCK", mark=None, legs=None, pid=1, multiplier=None):
    m = mark if mark is not None else {"price": price, "underlying": price if under is None else under}
    return {"id": pid, "symbol": symbol, "direction": direction, "instrument_type": kind,
            "multiplier": multiplier or (100 if kind == "OPTION" else 1),
            "description": f"{direction} {qty} {symbol}", "bias": 1 if direction == "LONG" else -1,
            "invalidation_price": invalidation, "thesis_check": {"status": "INTACT"}, "legs": legs or [],
            "ledger": {"open_quantity": qty}, "status": "OPEN", "prev_close": prev, "mark": m,
            "executions": [{"side": s, "quantity": q, "price": p, "fees": 0.0, "executed_at": t} for s, q, p, t in fills]}


def no_price(symbol, **kw):
    return view(symbol, mark={"price": None, "error": f"{symbol} quote timed out"}, **kw)


def fills(*rows):
    return [Fill(side, q, p, fees, f"2026-10-0{i + 1}T15:00:00+00:00") for i, (side, q, p, *rest) in enumerate(rows)
            for fees in [rest[0] if rest else 0.0]]


def rows_of(db: Database) -> dict[str, list[tuple]]:
    return {t: [tuple(r) for r in db.query(f"SELECT * FROM {t} ORDER BY 1")] for t in ("positions", "executions")}


@pytest.fixture()
def db():
    d = Database(":memory:")
    yield d
    d.close()


def stock(db, symbol="AAPL", direction="LONG", qty=100):
    with db.tx() as cur:
        return create(cur, symbol=symbol, instrument_type="STOCK", direction=direction, quantity=qty)


def fill(db, pid, side, qty, price, when, fees=0.0):
    with db.tx() as cur:
        return add_execution(cur, pid, side=side, quantity=qty, price=price, fees=fees, executed_at=when)


# --- 1.1 portfolio risk when prices are missing --------------------------------------------------

def test_1_1_every_position_has_the_same_fields_priced_or_not():
    priced = portfolio.exposure(view("AAPL", invalidation=90.0), 1.2)
    missing = portfolio.exposure(no_price("MSFT"), 1.0)
    assert set(priced) == set(missing) - {"dte", "iv", "option", "model_vol", "growth", "rate"} | set(priced)
    for key in portfolio.EXPOSURE_FIELDS:
        assert key in missing and missing[key] is None           # unknown, never a made-up zero
    assert priced["priced"] is True and missing["priced"] is False
    assert missing["missing"] == ["MSFT quote timed out"] and set(missing["missing_fields"]) == {"price", "underlying_price"}


def test_1_1_portfolio_with_an_unpriced_holding_returns_and_says_it_is_incomplete():
    views = [view("AAPL", qty=100, price=200.0, invalidation=190.0, pid=1),
             view("KO", direction="SHORT", qty=50, price=60.0, invalidation=64.0, pid=2),
             no_price("MSFT", qty=10, invalidation=300.0, pid=3)]
    daily = portfolio.daily_loss([], "REALIZED_PLUS_UNREALIZED", 100_000, 3.0, NOW)
    r = portfolio.analyse(views, {}, 100_000, 10.0, daily, lambda s: None)       # used to raise KeyError
    t = r["totals"]
    # Known exposures are exactly the priced positions' own.
    assert t["gross_exposure"] == pytest.approx(20_000 + 3_000) and t["net_exposure"] == pytest.approx(17_000)
    assert t["long_exposure"] == pytest.approx(20_000) and t["short_exposure"] == pytest.approx(-3_000)
    assert t["market_value"] == pytest.approx(20_000 - 3_000)
    assert t["open_risk"] == pytest.approx(1_000 + 200) and t["open_risk_unknown"] == 1 and not t["open_risk_complete"]
    assert t["complete"] is False and t["unknown_counts"]["exposure"] == 1
    cov = r["coverage"]
    assert cov["status"] == "PARTIAL" and cov["priced"] == 2 and cov["positions"] == 3
    assert cov["unpriced"][0]["description"] == "LONG 10 MSFT" and cov["unpriced"][0]["reasons"] == ["MSFT quote timed out"]
    texts = [w["text"] for w in r["warnings"]]
    assert any(t_.startswith("Portfolio risk is incomplete because 1 position does not have a current price") for t_ in texts)
    assert any("not known whether the whole is inside the limit" in t_ for t_ in texts)
    # Stress tests still run, on what can be repriced, and say what they left out.
    for sc in r["stress"]:
        assert sc["complete"] is False and sc["excluded"] == ["LONG 10 MSFT"]
    msft = next(x for x in r["positions"] if x["symbol"] == "MSFT")
    assert msft["sector"] and msft["dollar_delta"] is None


def test_1_1_nothing_priced_is_unavailable_and_complete_portfolios_say_so():
    daily = portfolio.daily_loss([], "REALIZED_PLUS_UNREALIZED", 10_000, 3.0, NOW)
    none = portfolio.analyse([no_price("A", pid=1), no_price("B", pid=2)], {}, 10_000, 10.0, daily, lambda s: None)
    assert none["coverage"]["status"] == "UNAVAILABLE" and none["totals"]["gross_exposure"] == 0
    assert none["totals"]["complete"] is False and all(not s["complete"] for s in none["stress"])
    full = portfolio.analyse([view("A", invalidation=90.0)], {}, 10_000, 10.0, daily, lambda s: None)
    assert full["coverage"]["status"] == "COMPLETE" and full["totals"]["complete"] and full["totals"]["open_risk_complete"]
    assert not any(w.get("code") == "INCOMPLETE_PRICES" for w in full["warnings"])


def test_1_1_an_option_without_delta_has_unknown_exposure_but_known_value():
    opt = view("NVDA", qty=2, kind="OPTION", invalidation=90.0,
               mark={"price": 10.0, "underlying": 100.0, "iv": 0.4, "dte": 40},
               legs=[{"option_type": "CALL", "strike": 100.0, "expiration": "2026-11-20"}])
    x = portfolio.exposure(opt, 1.0)
    assert x["priced"] and x["market_value"] == pytest.approx(2_000) and x["dollar_delta"] is None
    assert x["beta_dollars"] is None and x["theta"] is None and {"delta", "theta"} <= set(x["missing_fields"])
    daily = portfolio.daily_loss([], "REALIZED_PLUS_UNREALIZED", 10_000, 3.0, NOW)
    r = portfolio.analyse([opt], {}, 10_000, 10.0, daily, lambda s: None)
    assert r["coverage"]["status"] == "PARTIAL" and r["totals"]["unknown_counts"]["exposure"] == 1


def test_1_1_stress_uses_the_position_multiplier_not_always_100():
    legs = [{"option_type": "CALL", "strike": 200.0, "expiration": "2027-01-15"}]
    mark = {"price": 10.0, "underlying": 200.0, "delta": 0.55, "theta": -0.05, "vega": 0.4, "gamma": 0.01,
            "iv": 0.30, "model_vol": 0.28, "growth": 0.03, "rate": 0.045, "dte": 100}
    std = portfolio.exposure(view("AAPL", qty=1, kind="OPTION", mark=dict(mark), legs=legs), 1.0)
    adj = portfolio.exposure(view("AAPL", qty=1, kind="OPTION", mark=dict(mark), legs=legs, multiplier=10), 1.0)
    s_std, = portfolio.stress([std], [{"name": "down", "market_move_pct": -10}], 100_000)
    s_adj, = portfolio.stress([adj], [{"name": "down", "market_move_pct": -10}], 100_000)
    assert s_std["pnl"] < 0 and s_adj["pnl"] == pytest.approx(s_std["pnl"] / 10)


def test_1_1_portfolio_endpoint_survives_a_quote_failure(tmp_path, monkeypatch):
    settings = Settings(data_mode="demo", db_path=tmp_path / "t.db", account_size=50_000)
    svc = TradeAssistant(settings, provider=DemoData(), db=Database(":memory:"))
    c = TestClient(create_app(settings, svc))
    for body in ({"symbol": "AAPL", "quantity": 10, "invalidation_price": 1, "entry": {"price": 150}},
                 {"symbol": "MSFT", "quantity": 5, "invalidation_price": 1, "entry": {"price": 300}}):
        assert c.post("/v2/positions", json=body).status_code == 200
    real = svc.desk.mark

    def flaky(pos):
        if pos["symbol"] == "MSFT":
            raise DataError("MSFT: the data source did not answer")
        return real(pos)
    monkeypatch.setattr(svc.desk, "mark", flaky)
    r = c.get("/v2/portfolio/risk")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["coverage"]["status"] == "PARTIAL" and body["count"] == 2
    assert body["coverage"]["unpriced"][0]["reasons"] == ["MSFT: the data source did not answer"]
    assert c.post("/v2/portfolio/stress", json={"scenarios": [{"market_move_pct": -5}]}).status_code == 200


# --- 1.2 closed positions are finished --------------------------------------------------------------

def test_1_2_buying_again_after_closing_is_refused_and_changes_nothing(db):
    pid = stock(db)
    fill(db, pid, "BUY", 100, 50.0, "2026-10-01T10:00")
    fill(db, pid, "SELL", 100, 55.0, "2026-10-01T15:00")
    before = rows_of(db)
    with pytest.raises(PositionError) as exc:
        fill(db, pid, "BUY", 10, 54.0, "2026-10-02T10:00")
    assert str(exc.value) == CLOSED_MESSAGE == "This position is already closed. Create a new position for a new trade."
    with pytest.raises(PositionError, match="already closed"):          # an extra exit as well
        fill(db, pid, "SELL", 10, 56.0, "2026-10-02T10:00")
    assert rows_of(db) == before
    with db.tx() as cur:
        p = read(cur, pid)
    assert p["status"] == "CLOSED" and p["ledger"]["realized_pnl"] == pytest.approx(500.0)


def test_1_2_covering_a_short_and_shorting_again_needs_a_new_position(db):
    pid = stock(db, "CRWD", "SHORT", 10)
    fill(db, pid, "SELL_SHORT", 10, 300.0, "2026-10-01T10:00")
    fill(db, pid, "BUY_TO_COVER", 10, 290.0, "2026-10-02T10:00")
    with pytest.raises(PositionError, match="already closed"):
        fill(db, pid, "SELL_SHORT", 5, 295.0, "2026-10-03T10:00")
    again = stock(db, "CRWD", "SHORT", 5)                              # the new trade: a new id
    fill(db, again, "SELL_SHORT", 5, 295.0, "2026-10-03T10:00")
    assert again != pid
    with db.tx() as cur:
        old, new = read(cur, pid), read(cur, again)
    assert old["status"] == "CLOSED" and old["ledger"]["realized_pnl"] == pytest.approx(100.0)
    assert new["status"] == "OPEN" and new["ledger"]["open_quantity"] == 5


def test_1_2_a_backdated_fill_cannot_split_one_position_into_two_trades(db):
    pid = stock(db)
    fill(db, pid, "BUY", 100, 50.0, "2026-10-01T10:00")
    fill(db, pid, "BUY", 50, 52.0, "2026-10-05T10:00")
    before = rows_of(db)
    with pytest.raises(PositionError, match="close the position completely before a later fill"):
        fill(db, pid, "SELL", 100, 51.0, "2026-10-03T10:00")          # would close, then the Oct 5 buy reopens
    assert rows_of(db) == before
    fill(db, pid, "SELL", 60, 51.0, "2026-10-03T10:00")                # a partial exit in between is fine
    with db.tx() as cur:
        assert read(cur, pid)["status"] == "PARTIALLY_CLOSED"


def test_1_2_cancelled_and_planned_transitions(db):
    pid = stock(db)
    with db.tx() as cur:
        from tradeassist.positions import cancel
        cancel(cur, pid)
    with pytest.raises(PositionError, match="cancelled"):
        fill(db, pid, "BUY", 1, 1.0, "2026-10-01T10:00")
    # Planned -> Open -> Partially closed -> Closed, and Open -> Closed in one exit.
    a = stock(db)
    seen = [fill(db, a, *step).status() for step in (("BUY", 100, 10.0, "2026-10-01T10:00"),
                                                     ("SELL", 40, 11.0, "2026-10-01T11:00"),
                                                     ("SELL", 60, 12.0, "2026-10-01T12:00"))]
    assert seen == ["OPEN", "PARTIALLY_CLOSED", "CLOSED"]
    b = stock(db)
    assert [fill(db, b, *step).status() for step in (("BUY", 5, 10.0, "2026-10-01T10:00"),
                                                     ("SELL", 5, 9.0, "2026-10-01T11:00"))] == ["OPEN", "CLOSED"]


def test_1_2_recompute_refuses_a_closed_position_opening_again(db):
    pid = stock(db)
    fill(db, pid, "BUY", 10, 10.0, "2026-10-01T10:00")
    fill(db, pid, "SELL", 10, 11.0, "2026-10-01T11:00")
    with pytest.raises(PositionError, match="already closed"):
        with db.tx() as cur:                       # even a direct insert cannot slip a closed position open
            cur.execute("INSERT INTO executions (position_id, side, quantity, price, fees, executed_at, created_at) "
                        "VALUES (?, 'BUY', 5, 10, 0, '2026-10-02T14:00:00+00:00', '2026-10-02T14:00:00+00:00')", (pid,))
            recompute(cur, pid)
    with db.tx() as cur:
        assert read(cur, pid)["status"] == "CLOSED" and len(read(cur, pid)["executions"]) == 2   # rolled back


def test_1_2_the_ledger_itself_refuses_fills_after_a_full_close():
    with pytest.raises(PositionError, match="already closed"):
        account("LONG", 1, fills(("BUY", 10, 5.0), ("SELL", 10, 6.0), ("BUY", 5, 7.0)))
    led = account("LONG", 1, fills(("BUY", 10, 5.0), ("SELL", 10, 6.0), ("BUY", 5, 7.0)), allow_reopen=True)
    assert led.lifecycle_issues and led.lifecycle_issues[0]["problem"] == "REOPENED_AFTER_CLOSE"


def test_1_2_a_position_reopened_by_an_older_version_stays_readable_and_can_be_closed(tmp_path):
    path = tmp_path / "old.db"
    conn = sqlite3.connect(path)
    conn.executescript((FIXTURES / "db_2_1_0.sql").read_text())
    conn.close()
    db = Database(path)
    ko = db.one("SELECT id FROM positions WHERE symbol = 'KO' AND legacy_trade_id IS NULL")["id"]
    with db.tx() as cur:
        p = read(cur, ko)
    assert p["status"] == "PARTIALLY_CLOSED" and p["ledger"]["open_quantity"] == 5          # kept as recorded
    assert [i["problem"] for i in p["ledger"]["lifecycle_issues"]] == ["REOPENED_AFTER_CLOSE"]
    led = fill(db, ko, "SELL", 5, 63.0, "2026-09-20T10:00")             # what is open can still be closed
    assert led.status() == "CLOSED" and led.realized_pnl == pytest.approx(10 * 2 + 5 * 2)
    db.close()


# --- 1.3 today's loss and how much of it was measured -----------------------------------------------

def test_1_3_partial_measurement_is_never_an_unqualified_ok():
    known = view("AAPL", qty=100, price=190.0, prev=191.0, fills=[("BUY", 100, 200.0, "2026-10-01T14:00:00+00:00")], pid=1)
    unknown = view("X", qty=10, price=10.0, prev=None, fills=[("BUY", 10, 9.0, "2026-09-30T14:00:00+00:00")], pid=2)
    d = portfolio.daily_loss([known, unknown], "REALIZED_PLUS_UNREALIZED", 100_000, 3.0, NOW)
    assert d["known_total"] == pytest.approx(-100.0) and d["counted"] == d["known_total"]
    assert d["loss_status"] == "OK" and d["coverage_status"] == "PARTIAL" and d["status"] == "INCOMPLETE"
    assert d["positions"] == 2 and d["positions_evaluated"] == 1 and d["positions_evaluated_pct"] == pytest.approx(50.0)
    assert d["capital_evaluated_pct"] == pytest.approx(100 * 20_000 / (20_000 + 90.0))
    assert d["missing"] == [{"id": 2, "description": "LONG 10 X", "reason": "no previous close"}]
    assert "not known whether the whole account is within the stop" in d["text"]
    assert d["unrealized_complete"] is False


def test_1_3_a_measured_stop_shows_even_when_something_else_is_unknown():
    big = view("AAPL", qty=100, price=170.0, prev=200.0, fills=[("BUY", 100, 200.0, "2026-10-01T14:00:00+00:00")], pid=1)
    unpriced = view("MSFT", qty=10, mark={"price": None, "error": "timed out"}, prev=300.0,
                    fills=[("BUY", 10, 300.0, "2026-10-01T14:00:00+00:00")], pid=2)
    d = portfolio.daily_loss([big, unpriced], "REALIZED_PLUS_UNREALIZED", 10_000, 3.0, NOW)
    assert d["loss_status"] == "STOP" and d["status"] == "STOP" and d["coverage_status"] == "PARTIAL"
    assert d["text"].startswith("Today's measured loss of $3,000 has reached the daily stop")
    assert "no current price (timed out)" in d["text"]
    alerts = account_alerts(d, 0.0, 1_000.0)
    kinds = {a["kind"]: a["level"] for a in alerts}
    assert kinds["DAILY_LOSS"] == "fail" and kinds["DAILY_LOSS_UNKNOWN"] == "warn"


def test_1_3_nothing_measured_is_unavailable_and_everything_measured_is_complete():
    u = view("X", qty=1, price=10.0, prev=None, fills=[("BUY", 1, 9.0, "2026-09-30T14:00:00+00:00")])
    d = portfolio.daily_loss([u], "REALIZED_PLUS_UNREALIZED", 10_000, 3.0, NOW)
    assert d["coverage_status"] == "UNAVAILABLE" and d["status"] == "INCOMPLETE" and d["loss_status"] == "OK"
    ok = view("A", qty=1, price=10.0, prev=10.0, fills=[("BUY", 1, 9.0, "2026-09-30T14:00:00+00:00")])
    full = portfolio.daily_loss([ok], "REALIZED_PLUS_UNREALIZED", 10_000, 3.0, NOW)
    assert full["coverage_status"] == "COMPLETE" and full["status"] == "OK" and full["capital_evaluated_pct"] == 100.0
    empty = portfolio.daily_loss([], "REALIZED_PLUS_UNREALIZED", 10_000, 3.0, NOW)
    assert empty["coverage_status"] == "COMPLETE" and empty["status"] == "OK"


def test_1_3_realized_losses_count_in_realized_only_mode_even_without_a_previous_close():
    # Bought last week, sold today at a $900 loss; no previous close could be found. Version 2.1 counted
    # nothing for it in either mode. Realized profit needs only the fills.
    sold = view("Y", qty=0, price=None, prev=None, fills=[("BUY", 100, 100.0, "2026-09-28T14:00:00+00:00"),
                                                          ("SELL", 100, 91.0, "2026-10-06T14:00:00+00:00")])
    sold["mark"] = None
    ro = portfolio.daily_loss([sold], "REALIZED_ONLY", 10_000, 3.0, NOW)
    assert ro["known_total"] == pytest.approx(-900.0) and ro["coverage_status"] == "COMPLETE" and ro["loss_status"] == "STOP"
    both = portfolio.daily_loss([sold], "REALIZED_PLUS_UNREALIZED", 10_000, 3.0, NOW)
    # With open moves counted, the day's figure needs yesterday's close, so it is not measured.
    assert both["realized_today"] == pytest.approx(-900.0) and both["coverage_status"] == "UNAVAILABLE"
    assert both["known_total"] == 0.0 and both["status"] == "INCOMPLETE"


def _ticket_app(tmp_path, policy="WARN"):
    settings = Settings(data_mode="demo", db_path=tmp_path / "t.db", incomplete_risk_policy=policy)
    svc = TradeAssistant(settings, provider=DemoData(), db=Database(":memory:"))
    client = TestClient(create_app(settings, svc))
    exp = next(e["date"] for e in client.get("/api/expirations", params={"symbol": "AAPL"}).json()["expirations"]
               if e["dte"] >= 30)
    chain = client.get("/api/chain", params={"symbol": "AAPL", "expiration": exp}).json()
    k = min(chain["contracts"], key=lambda c: abs(c["strike"] - chain["spot"]))
    ask = k.get("ask") or 1.0
    body = {"symbol": "AAPL", "expiration": exp, "option_type": k["option_type"], "strike": k["strike"], "contracts": 1,
            "limit_price": round(ask * 0.9, 2), "max_limit": round(ask, 2),
            "thesis": "A clear thesis with a date and an invalidation level."}
    return svc, client, body


def _partial(svc, monkeypatch):
    real = svc.analyst.daily_loss

    def partial(views=None):
        d = real(views)
        d.update({"coverage_status": "PARTIAL", "status": "INCOMPLETE", "positions": 2, "positions_evaluated": 1,
                  "missing": [{"id": 9, "description": "Long 10 MSFT shares", "reason": "no previous close"}],
                  "text": "Measured so far today: +$0 ... not known whether the whole account is within the stop."})
        return d
    monkeypatch.setattr(svc.analyst, "daily_loss", partial)


def test_1_3_ticket_warns_and_never_says_inside_all_limits_when_loss_is_unmeasured(tmp_path, monkeypatch):
    svc, c, body = _ticket_app(tmp_path)
    clean_ = c.post("/api/ticket", json=body).json()
    _partial(svc, monkeypatch)
    t = c.post("/api/ticket", json=body).json()
    assert t["incomplete_risk_policy"] == "WARN" and [n["key"] for n in t["risk_notes"]] == ["daily_loss_incomplete"]
    assert t["within_limits"] is False and t["can_save"] == clean_["can_save"]     # a warning, not a block
    assert not any(v["key"] == "daily_loss_incomplete" for v in t["violations"])


def test_1_3_block_policy_makes_unmeasured_loss_a_limit_you_must_override(tmp_path, monkeypatch):
    svc, c, body = _ticket_app(tmp_path, "BLOCK")
    _partial(svc, monkeypatch)
    t = c.post("/api/ticket", json=body).json()
    v = next(v for v in t["violations"] if v["key"] == "daily_loss_incomplete")
    assert v["overridable"] and any(b["key"] == "daily_loss_incomplete" for b in t["blocking"])
    over = c.post("/api/ticket", json={**body, "overrides": ["daily_loss_incomplete"]}).json()
    assert not any(b["key"] == "daily_loss_incomplete" for b in over["blocking"])


def test_1_3_policy_setting(monkeypatch, tmp_path):
    monkeypatch.setenv("INCOMPLETE_RISK_POLICY", "block")
    assert load_settings(tmp_path / "none.env").incomplete_risk_policy == "BLOCK"
    monkeypatch.setenv("INCOMPLETE_RISK_POLICY", "ignore")
    with pytest.raises(ValueError, match="WARN or BLOCK"):
        load_settings(tmp_path / "none.env")


def test_1_3_open_risk_alert_when_some_risk_is_unknown():
    alerts = account_alerts(None, 500.0, 1_000.0, open_risk_unknown=2)
    assert [a["kind"] for a in alerts] == ["OPEN_RISK_UNKNOWN"] and "2 open positions" in alerts[0]["text"]


# --- 1.4 every number finite ---------------------------------------------------------------------------

@pytest.mark.parametrize("bad", [float("nan"), float("inf"), float("-inf")])
def test_1_4_non_finite_fees_prices_and_quantities_are_refused(bad):
    with pytest.raises(PositionError, match="[Ff]ees must be a finite"):
        account("LONG", 1, [Fill("BUY", 10, 5.0, bad)])
    with pytest.raises(PositionError, match="finite price"):
        account("LONG", 1, [Fill("BUY", 10, bad, 0.0)])
    with pytest.raises(PositionError, match="above zero"):
        account("LONG", 1, [Fill("BUY", bad, 5.0, 0.0)])
    with pytest.raises(PositionError, match="multiplier"):
        account("LONG", bad, [Fill("BUY", 1, 5.0, 0.0)])


def test_1_4_negative_fees_and_prices_are_refused_but_zero_is_fine():
    with pytest.raises(PositionError, match="nor can fees"):
        account("LONG", 1, [Fill("BUY", 10, 5.0, -0.01)])
    with pytest.raises(PositionError, match="nor can fees"):
        account("LONG", 1, [Fill("BUY", 10, -5.0, 0.0)])
    # A bought option that expires worthless closes at zero: a legitimate fill.
    expired = account("LONG", 100, fills(("BUY", 2, 1.50, 1.30), ("SELL", 2, 0.0)))
    assert expired.status() == "CLOSED" and expired.realized_pnl == pytest.approx(-300.0 - 1.30)


def test_1_4_large_finite_numbers_work_and_overflow_is_refused():
    big = account("LONG", 1, fills(("BUY", 1e9, 1e6), ("SELL", 1e9, 1.1e6)))
    assert big.realized_pnl == pytest.approx(1e14) and math.isfinite(big.realized_pnl)
    with pytest.raises(PositionError, match="too large"):
        account("LONG", 1, fills(("BUY", 1e200, 1e200), ("SELL", 1e200, 1e-200)))


def test_1_4_precision_near_the_limits():
    # 0.1 + 0.2 is not 0.3 in binary; selling 0.3 still closes it exactly, with no dust left open.
    led = account("LONG", 1, fills(("BUY", 0.1, 10.0), ("BUY", 0.2, 10.0), ("SELL", 0.3, 11.0)))
    assert led.open_quantity == 0.0 and led.status() == "CLOSED" and led.realized_pnl == pytest.approx(0.3)
    led2 = account("LONG", 1, fills(("BUY", 0.3, 10.0), ("SELL", 0.1, 11.0), ("SELL", 0.2, 11.0)))
    assert led2.open_quantity == 0.0 and led2.status() == "CLOSED"
    with pytest.raises(PositionError, match="too small"):
        account("LONG", 1, [Fill("BUY", 1e-9, 10.0)])
    assert account("LONG", 1, [Fill("BUY", 1e-6, 10.0)]).open_quantity == pytest.approx(1e-6)


def test_1_4_plan_and_contract_inputs_are_checked_at_the_domain_layer(db):
    with db.tx() as cur:
        for kw in ({"invalidation_price": float("nan")}, {"risk_amount": float("inf")}, {"target_1": -1.0},
                   {"initial_risk_per_unit": float("nan")}):
            with pytest.raises(PositionError, match="positive number"):
                create(cur, symbol="A", instrument_type="STOCK", direction="LONG", quantity=1, **kw)
        with pytest.raises(PositionError, match="quantity"):
            create(cur, symbol="A", instrument_type="STOCK", direction="LONG", quantity=float("nan"))
        with pytest.raises(PositionError, match="strike"):
            create(cur, symbol="A", instrument_type="OPTION", direction="LONG", quantity=1,
                   option=OptionLeg("CALL", float("inf"), "2026-12-18"))
        with pytest.raises(PositionError, match="multiplier"):
            create(cur, symbol="A", instrument_type="OPTION", direction="LONG", quantity=1,
                   option=OptionLeg("CALL", 100.0, "2026-12-18", multiplier=float("nan")))
        with pytest.raises(PositionError, match="intended_horizon"):
            create(cur, symbol="A", instrument_type="STOCK", direction="LONG", quantity=1, intended_horizon=2.5)
        pid = create(cur, symbol="A", instrument_type="STOCK", direction="LONG", quantity=1)
        with pytest.raises(PositionError, match="positive number"):
            update_plan(cur, pid, {"risk_amount": float("nan")})
    before = rows_of(db)
    with pytest.raises(PositionError, match="fees"):
        fill(db, pid, "BUY", 1, 10.0, "2026-10-01T10:00", fees=float("nan"))
    with pytest.raises(PositionError, match="price"):
        fill(db, pid, "BUY", 1, float("inf"), "2026-10-01T10:00")
    assert rows_of(db) == before                                  # nothing reached the ledger


def test_1_4_journal_refuses_non_finite_numbers_but_takes_a_zero_close():
    db = Database(":memory:")
    j = Journal(db)
    tid = j.create(data_source="demo", symbol="AAPL", option_symbol="X", option_type="call", strike=200,
                   expiration="2026-12-18", contracts=1, thesis="t" * 20, planned_limit=2.0, max_limit=2.0, plan={},
                   quote={}, scorecard=[], signals=[], overrides=[])
    with pytest.raises(JournalError, match="finite"):
        j.log_attempt(tid, side="buy", bid=float("nan"), ask=2.1, limit_price=2.0, filled=True)
    with pytest.raises(JournalError, match="finite"):
        j.log_attempt(tid, side="buy", bid=1.9, ask=2.1, limit_price=2.0, filled=True, seconds_to_fill=float("inf"))
    j.log_attempt(tid, side="buy", bid=1.9, ask=2.1, limit_price=2.0, filled=True)
    with pytest.raises(JournalError, match="finite"):
        j.close_at(tid, float("nan"))
    assert j.close_at(tid, 0.0)["pnl"] == pytest.approx(-200.0)
    with pytest.raises(JournalError, match="finite"):
        j.create(data_source="demo", symbol="A", option_symbol="X", option_type="call", strike=float("nan"),
                 expiration="2026-12-18", contracts=1, thesis="t", planned_limit=1, max_limit=1, plan={}, quote={},
                 scorecard=[], signals=[], overrides=[])
    db.close()


@pytest.mark.parametrize("raw", ["inf", "nan", "-inf", "0", "-5"])
def test_1_4_account_settings_must_be_finite_and_positive(raw, monkeypatch, tmp_path):
    monkeypatch.setenv("ACCOUNT_SIZE", raw)
    with pytest.raises(ValueError, match="ACCOUNT_SIZE"):
        load_settings(tmp_path / "none.env")


def test_1_4_the_api_refuses_non_finite_numbers_in_fills_and_plans(tmp_path):
    settings = Settings(data_mode="demo", db_path=tmp_path / "t.db")
    c = TestClient(create_app(settings, TradeAssistant(settings, provider=DemoData(), db=Database(":memory:"))))
    pid = c.post("/v2/positions", json={"symbol": "KO", "quantity": 10}).json()["id"]
    for payload in ('{"side": "BUY", "quantity": 1, "price": 60, "fees": Infinity}',
                    '{"side": "BUY", "quantity": 1, "price": NaN}', '{"side": "BUY", "quantity": 1e400, "price": 60}'):
        r = c.post(f"/v2/positions/{pid}/executions", content=payload, headers={"Content-Type": "application/json"})
        assert r.status_code == 422, payload
    r = c.post(f"/v2/positions/{pid}/plan", content='{"risk_amount": -Infinity}', headers={"Content-Type": "application/json"})
    assert r.status_code == 422
    assert c.get(f"/v2/positions/{pid}").json()["executions"] == []
