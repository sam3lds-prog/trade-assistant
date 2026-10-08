import random
import sqlite3

import pytest
from fastapi.testclient import TestClient

from tradeassist.api import create_app
from tradeassist.config import Settings
from tradeassist.db import SCHEMA, Database
from tradeassist.journal import Journal
from tradeassist.positions import Fill, PositionError, account, initial_risk
from tradeassist.providers.demo import DemoData
from tradeassist.service import TradeAssistant


def fills(*rows):
    return [Fill(side, q, p, fees, f"2026-10-0{i + 1}T15:00:00+00:00") for i, (side, q, p, *rest) in enumerate(rows)
            for fees in [rest[0] if rest else 0.0]]


# --- accounting --------------------------------------------------------------------------------

def test_short_stock_with_partial_covers():
    """Short 1,000 CRWD at $250, cover 300 at $240 and 400 at $235: 300 remain open."""
    led = account("SHORT", 1, fills(("SELL_SHORT", 1000, 250.0), ("BUY_TO_COVER", 300, 240.0), ("BUY_TO_COVER", 400, 235.0)))
    assert led.open_quantity == 300 and led.status() == "PARTIALLY_CLOSED"
    assert led.realized_pnl == pytest.approx(300 * 10 + 400 * 15)
    assert led.unrealized(243.60) == pytest.approx(300 * (250 - 243.60))
    assert led.average_exit_price == pytest.approx((300 * 240 + 400 * 235) / 700)
    assert led.market_value(243.60) == pytest.approx(-300 * 243.60)
    closed = account("SHORT", 1, fills(("SELL_SHORT", 1000, 250.0), ("BUY_TO_COVER", 1000, 260.0)))
    assert closed.status() == "CLOSED" and closed.realized_pnl == pytest.approx(-10_000)


def test_long_stock_scaling_in_and_out_with_fees():
    led = account("LONG", 1, fills(("BUY", 100, 50.0, 1.0), ("BUY", 100, 60.0, 1.0), ("SELL", 150, 70.0, 1.5)))
    assert led.average_entry_price == pytest.approx(55.0) and led.cost_basis == pytest.approx(55.0)
    assert led.realized_pnl == pytest.approx(150 * 15 - 3.5) and led.fees == pytest.approx(3.5)
    assert led.open_quantity == 50 and led.unrealized(65.0) == pytest.approx(50 * 10)
    # Adding again after a partial exit moves the cost of what is open, not what was realised.
    more = account("LONG", 1, fills(("BUY", 100, 50.0), ("SELL", 50, 60.0), ("BUY", 50, 80.0)))
    assert more.realized_pnl == pytest.approx(500) and more.cost_basis == pytest.approx((50 * 50 + 50 * 80) / 100)
    assert more.average_entry_price == pytest.approx((100 * 50 + 50 * 80) / 150)


def test_long_call_and_long_put_use_the_contract_multiplier():
    call = account("LONG", 100, fills(("BUY", 2, 5.40), ("SELL", 1, 8.10), ("SELL", 1, 3.00)))
    assert call.status() == "CLOSED" and call.realized_pnl == pytest.approx(100 * (2.70 - 2.40))
    put = account("LONG", 100, fills(("BUY", 3, 2.00, 0.03 * 3)))
    assert put.unrealized(2.50) == pytest.approx(150.0) and put.realized_pnl == pytest.approx(-0.09)


def test_fills_that_do_not_fit_the_direction_or_close_too_much_are_refused():
    with pytest.raises(PositionError, match="only 100 is open"):
        account("LONG", 1, fills(("BUY", 100, 10.0), ("SELL", 150, 11.0)))
    with pytest.raises(PositionError, match="nothing is open"):
        account("SHORT", 1, fills(("BUY_TO_COVER", 10, 10.0)))
    with pytest.raises(PositionError, match="takes BUY or SELL"):
        account("LONG", 1, fills(("SELL_SHORT", 10, 10.0)))
    with pytest.raises(PositionError, match="above zero"):
        account("LONG", 1, [Fill("BUY", 0, 10.0)])
    # A plain SELL opens a short and a plain BUY covers it.
    led = account("SHORT", 1, fills(("SELL", 10, 20.0), ("BUY", 10, 18.0)))
    assert led.realized_pnl == pytest.approx(20.0) and led.status() == "CLOSED"


def test_a_closed_position_realises_exactly_its_cash_flows():
    rng = random.Random(4)
    for _ in range(300):
        direction = rng.choice(["LONG", "SHORT"])
        mult = rng.choice([1, 100])
        opening, closing = ("BUY", "SELL") if direction == "LONG" else ("SELL_SHORT", "BUY_TO_COVER")
        held, rows, cash = 0, [], 0.0
        for _ in range(rng.randint(1, 8)):
            if held and rng.random() < 0.5:
                q = rng.randint(1, held)
                side = closing
                held -= q
            else:
                q = rng.randint(1, 50)
                side = opening
                held += q
            price, fee = round(rng.uniform(1, 300), 2), round(rng.uniform(0, 2), 2)
            rows.append((side, q, price, fee))
            cash += (q * price * mult if side in ("SELL", "SELL_SHORT") else -q * price * mult) - fee
            if not held:
                break                            # closed completely: a position takes no fills after that
        if held:
            rows.append((closing, held, round(rng.uniform(1, 300), 2), 0.0))
            cash += held * rows[-1][2] * mult * (1 if closing == "SELL" else -1)
        led = account(direction, mult, fills(*rows))
        assert led.open_quantity == 0 and led.realized_pnl == pytest.approx(cash, abs=1e-6)


def test_r_multiple_risk_comes_from_the_plan():
    led = account("SHORT", 1, fills(("SELL_SHORT", 1000, 250.0)))
    assert initial_risk({"direction": "SHORT", "instrument_type": "STOCK", "quantity": 1000, "invalidation_price": 255.0,
                         "multiplier": 1}, led) == pytest.approx(5000.0)
    assert initial_risk({"direction": "SHORT", "instrument_type": "STOCK", "quantity": 1000, "risk_amount": 4000,
                         "multiplier": 1}, led) == 4000
    opt = account("LONG", 100, fills(("BUY", 2, 3.0)))
    assert initial_risk({"direction": "LONG", "instrument_type": "OPTION", "quantity": 2, "multiplier": 100}, opt) == 600


# --- storage, migration and the classic journal -------------------------------------------------------

def test_an_older_database_gets_the_new_tables_and_its_journal_entries(tmp_path):
    path = tmp_path / "old.db"
    conn = sqlite3.connect(path)
    conn.executescript(SCHEMA)
    conn.execute("INSERT INTO trades (created_at, data_source, symbol, option_symbol, option_type, strike, expiration, "
                 "contracts, thesis, status, planned_limit, max_limit, entry_price, entry_at, exit_price, exit_at, pnl) "
                 "VALUES ('2026-09-01T14:00:00+00:00', 'live', 'AAPL', 'AAPL261120C00200000', 'call', 200, '2026-11-20', "
                 "2, 'old idea', 'closed', 5.0, 5.2, 5.1, '2026-09-01T14:05:00+00:00', 7.1, '2026-09-10T15:00:00+00:00', 400)")
    conn.commit()
    conn.close()
    db = Database(path)
    assert db.schema_version() >= 1 and 1 in db.migrated
    pos = db.query("SELECT * FROM positions")
    assert len(pos) == 1 and pos[0]["status"] == "CLOSED" and pos[0]["realized_pnl"] == pytest.approx(400.0)
    assert pos[0]["instrument_type"] == "OPTION" and pos[0]["legacy_trade_id"] == 1
    leg = db.one("SELECT * FROM option_contracts WHERE position_id = ?", (pos[0]["id"],))
    assert leg["option_type"] == "CALL" and leg["strike"] == 200
    db.close()
    again = Database(path)                                   # opening again changes nothing
    assert again.migrated == [] and len(again.query("SELECT * FROM positions")) == 1


def test_the_classic_journal_stays_in_step_with_its_mirror():
    db = Database(":memory:")
    j = Journal(db)
    tid = j.create(data_source="demo", symbol="AAPL", option_symbol="AAPL261120P00190000", option_type="put", strike=190,
                   expiration="2026-11-20", contracts=3, thesis="t" * 20, planned_limit=2.0, max_limit=2.1, plan={},
                   quote={}, scorecard=[], signals=[], overrides=[])
    mirror = lambda: db.one("SELECT * FROM positions WHERE legacy_trade_id = ?", (tid,))
    assert mirror()["status"] == "PLANNED" and mirror()["risk_amount"] == pytest.approx(630.0)
    j.log_attempt(tid, side="buy", bid=1.9, ask=2.1, limit_price=2.0, filled=True)
    assert mirror()["status"] == "OPEN" and mirror()["open_quantity"] == 3
    j.close_at(tid, 3.0)
    m = mirror()
    assert m["status"] == "CLOSED" and m["realized_pnl"] == pytest.approx(j.get(tid)["pnl"]) == pytest.approx(300.0)
    t2 = j.create(data_source="demo", symbol="KO", option_symbol="X", option_type="call", strike=60, expiration="2026-11-20",
                  contracts=1, thesis="t" * 20, planned_limit=1.0, max_limit=1.0, plan={}, quote={}, scorecard=[],
                  signals=[], overrides=[])
    j.cancel(t2)
    assert db.one("SELECT status FROM positions WHERE legacy_trade_id = ?", (t2,))["status"] == "CANCELLED"


# --- through the API ---------------------------------------------------------------------------------

@pytest.fixture()
def client(tmp_path):
    settings = Settings(data_mode="demo", db_path=tmp_path / "t.db")
    assistant = TradeAssistant(settings, provider=DemoData(), db=Database(":memory:"))
    return TestClient(create_app(settings, assistant)), assistant


def test_stock_long_and_short_through_the_api(client):
    c, _ = client
    r = c.post("/v2/positions", json={"symbol": "aapl", "direction": "LONG", "quantity": 100, "thesis": "Breakout holds",
                                      "setup_type": "breakout", "invalidation_price": 190, "target_1": 230,
                                      "entry": {"price": 200.0, "fees": 1.0, "executed_at": "2026-10-01T14:31:00-04:00"}})
    assert r.status_code == 200, r.text
    p = r.json()
    assert p["status"] == "OPEN" and p["description"] == "Long 100 AAPL shares" and p["ledger"]["open_quantity"] == 100
    assert p["pnl"]["initial_risk"] == pytest.approx(1000.0) and p["mark"]["price"] > 0
    assert p["thesis_check"]["status"] in ("INTACT", "INVALIDATED", "TARGET_1_REACHED")
    assert [s["snapshot_type"] for s in p["snapshots"]] == ["ENTRY"] and p["snapshots"][0]["rsi_14"] is not None
    mark = p["mark"]["price"]
    assert p["pnl"]["unrealized"] == pytest.approx(100 * (mark - 200.0))
    assert p["pnl"]["total"] == pytest.approx(p["pnl"]["unrealized"] - 1.0)
    part = c.post(f"/v2/positions/{p['id']}/executions", json={"side": "SELL", "quantity": 40, "price": 210.0}).json()
    assert part["status"] == "PARTIALLY_CLOSED" and part["pnl"]["realized"] == pytest.approx(40 * 10 - 1.0)
    done = c.post(f"/v2/positions/{p['id']}/executions", json={"side": "SELL", "quantity": 60, "price": 205.0}).json()
    assert done["status"] == "CLOSED" and done["pnl"]["unrealized"] == 0
    assert done["pnl"]["total"] == pytest.approx(400 + 300 - 1.0)
    assert [s["snapshot_type"] for s in done["snapshots"]] == ["ENTRY", "EXIT"]
    assert c.post(f"/v2/positions/{p['id']}/executions", json={"side": "SELL", "quantity": 1, "price": 1}).status_code == 400

    s = c.post("/v2/positions", json={"symbol": "CRWD", "direction": "SHORT", "quantity": 1000, "invalidation_price": 255,
                                      "entry": {"price": 250.0}}).json()
    assert s["description"] == "Short 1,000 CRWD shares" and s["bias"] == -1
    bad = c.post(f"/v2/positions/{s['id']}/executions", json={"side": "SELL", "quantity": 2000, "price": 240}).status_code
    assert bad == 200                                          # a further short sale adds to the short
    over = c.post(f"/v2/positions/{s['id']}/executions", json={"side": "BUY_TO_COVER", "quantity": 5000, "price": 240})
    assert over.status_code == 400 and "only 3000 is open" in over.json()["error"]
    listed = c.get("/v2/positions?status=open").json()["positions"]
    assert {x["symbol"] for x in listed} == {"CRWD"}


def test_option_positions_planning_and_cancelling(client):
    c, assistant = client
    exps = c.get("/api/expirations", params={"symbol": "AAPL"}).json()["expirations"]
    exp = next(e["date"] for e in exps if e["dte"] >= 25)
    chain = c.get(f"/api/chain?symbol=AAPL&expiration={exp}&width_pct=1000").json()
    k = min({x["strike"] for x in chain["contracts"]}, key=lambda s: abs(s - chain["spot"]))
    r = c.post("/v2/positions", json={"symbol": "AAPL", "instrument_type": "OPTION", "quantity": 2,
                                      "option": {"option_type": "PUT", "strike": k, "expiration": exp},
                                      "invalidation_price": chain["spot"] * 1.05, "entry": {"price": 4.0}})
    assert r.status_code == 200, r.text
    p = r.json()
    assert p["multiplier"] == 100 and p["legs"][0]["option_symbol"].startswith("AAPL") and p["bias"] == -1
    assert p["mark"]["delta"] < 0 and p["mark"]["underlying"] > 0 and p["snapshots"][0]["delta"] is not None
    assert p["pnl"]["initial_risk"] == pytest.approx(800.0)
    short_opt = c.post("/v2/positions", json={"symbol": "AAPL", "instrument_type": "OPTION", "direction": "SHORT",
                                              "quantity": 1, "option": {"option_type": "CALL", "strike": k, "expiration": exp}})
    assert short_opt.status_code == 400 and "not supported yet" in short_opt.json()["error"]
    plan = c.post("/v2/positions", json={"symbol": "KO", "quantity": 50, "thesis": "Wait for the pullback"}).json()
    assert plan["status"] == "PLANNED" and plan["snapshots"][0]["snapshot_type"] == "PLAN"
    upd = c.post(f"/v2/positions/{plan['id']}/plan", json={"invalidation_price": 50, "target_1": 80}).json()
    assert upd["invalidation_price"] == 50 and upd["target_1"] == 80
    gone = c.post(f"/v2/positions/{plan['id']}/cancel", json={"note": "never triggered"}).json()
    assert gone["status"] == "CANCELLED"
    assert c.post(f"/v2/positions/{plan['id']}/executions", json={"side": "BUY", "quantity": 1, "price": 1}).status_code == 400
    future = c.post("/v2/positions", json={"symbol": "KO", "quantity": 1, "entry": {"price": 60, "executed_at": "2099-01-01"}})
    assert future.status_code == 400 and "future" in future.json()["error"]
    assert c.get("/v2/positions/99999").status_code == 400
    wrong = c.post("/v2/positions", json={"symbol": "KO", "quantity": 1, "invalidation_price": 70, "entry": {"price": 60}})
    assert wrong.status_code == 400 and "below the entry" in wrong.json()["error"]
    short_t = c.post("/v2/positions", json={"symbol": "KO", "direction": "SHORT", "quantity": 1, "target_1": 70,
                                            "entry": {"price": 60}})
    assert short_t.status_code == 400 and "target 1 must be below" in short_t.json()["error"]


def test_the_feature_flag_switches_the_positions_api_off(tmp_path):
    settings = Settings(data_mode="demo", db_path=tmp_path / "t.db",
                        features={"unified_positions": False, "trend_engine_v2": False, "portfolio_risk": False,
                                  "signal_calibration": False, "options_backtest": False})
    c = TestClient(create_app(settings, TradeAssistant(settings, provider=DemoData(), db=Database(":memory:"))))
    r = c.get("/v2/positions")
    assert r.status_code == 404 and "switched off" in r.json()["error"]
    assert c.get("/api/status").status_code == 200                        # the classic app is unaffected


def test_feature_overrides_from_the_environment():
    from tradeassist.features import load_features
    assert load_features({"FEATURE_UNIFIED_POSITIONS": "0"})["unified_positions"] is False
    assert load_features({"FEATURE_OPTIONS_BACKTEST": "yes"})["options_backtest"] is True
    with pytest.raises(ValueError):
        load_features({"FEATURE_PORTFOLIO_RISK": "maybe"})


def test_the_page_loads_the_v2_script_with_a_fresh_address(client):
    c, _ = client
    page = c.get("/").text
    assert '/v2.js?v=' in page and 'data-view="positions"' in page
    assert "BUILDERS" in c.get("/v2.js").text
