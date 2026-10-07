"""Portfolio risk: exposures, betas, risk to invalidation, stress tests and the daily loss stop."""

from datetime import datetime

import pytest
from fastapi.testclient import TestClient

from tradeassist import portfolio
from tradeassist.api import create_app
from tradeassist.config import Settings, load_settings
from tradeassist.db import Database
from tradeassist.optionmath import NY
from tradeassist.providers.demo import DemoData
from tradeassist.service import TradeAssistant
from test_trend import walk


def view(symbol, direction, qty, price, prev, fills, invalidation=None, kind="STOCK", mark=None, legs=None):
    return {"id": 1, "symbol": symbol, "direction": direction, "instrument_type": kind, "multiplier": 100 if kind == "OPTION" else 1,
            "description": f"{direction} {qty} {symbol}", "bias": 1 if direction == "LONG" else -1,
            "invalidation_price": invalidation, "thesis_check": {"status": "INTACT"}, "legs": legs or [],
            "ledger": {"open_quantity": qty}, "status": "OPEN", "prev_close": prev,
            "mark": mark or {"price": price, "underlying": price},
            "executions": [{"side": s, "quantity": q, "price": p, "fees": 0.0, "executed_at": t} for s, q, p, t in fills]}


NOW = datetime(2026, 10, 6, 14, 0, tzinfo=NY)


def test_daily_loss_counts_open_moves_unless_told_not_to():
    v = view("AAPL", "LONG", 100, 190.0, 200.0, [("BUY", 100, 205.0, "2026-10-01T14:00:00+00:00")])
    both = portfolio.daily_loss([v], "REALIZED_PLUS_UNREALIZED", 10_000, 3.0, NOW)
    closed = portfolio.daily_loss([v], "REALIZED_ONLY", 10_000, 3.0, NOW)
    assert both["unrealized_change_today"] == pytest.approx(-1000.0) and both["counted"] == pytest.approx(-1000.0)
    assert both["status"] == "STOP" and closed["status"] == "OK" and closed["counted"] == 0
    # Bought and partly sold today: the realized part and the open part both count.
    t = view("KO", "LONG", 50, 61.0, 60.0, [("BUY", 100, 60.0, "2026-10-06T14:00:00+00:00"),
                                             ("SELL", 50, 59.0, "2026-10-06T15:00:00+00:00")])
    d = portfolio.daily_loss([t], "REALIZED_PLUS_UNREALIZED", 10_000, 3.0, NOW)
    assert d["realized_today"] == pytest.approx(-50.0) and d["unrealized_change_today"] == pytest.approx(50.0)
    short = view("MSFT", "SHORT", 10, 260.0, 250.0, [("SELL_SHORT", 10, 255.0, "2026-09-30T14:00:00+00:00")])
    assert portfolio.daily_loss([short], "REALIZED_PLUS_UNREALIZED", 10_000, 3.0, NOW)["counted"] == pytest.approx(-100.0)
    # Held overnight and sold today: only the move from yesterday's close counts (+$100, not -$900).
    sold = view("Y", "LONG", 0, None, 90.0, [("BUY", 100, 100.0, "2026-09-28T14:00:00+00:00"),
                                             ("SELL", 100, 91.0, "2026-10-06T14:00:00+00:00")])
    sold["mark"] = None
    d2 = portfolio.daily_loss([sold], "REALIZED_PLUS_UNREALIZED", 10_000, 3.0, NOW)
    assert d2["realized_today"] == pytest.approx(-900) and d2["counted"] == pytest.approx(100.0)
    unknown = view("X", "LONG", 1, 10.0, None, [("BUY", 1, 9.0, "2026-09-30T14:00:00+00:00")])
    u = portfolio.daily_loss([unknown], "REALIZED_PLUS_UNREALIZED", 10_000, 3.0, NOW)
    assert u["unknown"] == ["LONG 1 X"] and u["counted"] == 0


def test_exposure_beta_and_risk_to_invalidation():
    long_ = portfolio.exposure(view("AAPL", "LONG", 100, 200.0, 199.0, [], invalidation=190.0), 1.5)
    assert long_["dollar_delta"] == 20_000 and long_["beta_dollars"] == 30_000 and long_["risk_to_invalidation"] == 1000
    short = portfolio.exposure(view("MSFT", "SHORT", 10, 250.0, 249.0, [], invalidation=270.0), None)
    assert short["dollar_delta"] == -2500 and short["risk_to_invalidation"] == 200 and short["beta_dollars"] == -2500
    no_plan = portfolio.exposure(view("KO", "LONG", 10, 60.0, 60.0, []), 0.6)
    assert no_plan["risk_to_invalidation"] is None and no_plan["missing"]
    opt = view("NVDA", "LONG", 2, 10.0, None, [], invalidation=90.0, kind="OPTION",
               mark={"price": 10.0, "underlying": 100.0, "delta": 0.5, "theta": -0.1, "vega": 0.2, "gamma": 0.02, "iv": 0.4, "dte": 40},
               legs=[{"option_type": "CALL", "strike": 100.0, "expiration": "2026-11-20"}])
    o = portfolio.exposure(opt, 2.0)
    assert o["dollar_delta"] == pytest.approx(0.5 * 100 * 2 * 100) and o["theta"] == pytest.approx(-20)
    assert 0 < o["risk_to_invalidation"] < 2000           # loses part of the premium at 90, not all of it


def test_betas_and_stress():
    spy = walk(n=200, seed=1)
    stock = spy.copy()
    stock["close"] = spy["close"] ** 2 / spy["close"].iloc[0]       # moves about twice as much
    rets = portfolio.returns_frame({"SPY": spy, "TWICE": stock})
    b = portfolio.betas(rets)
    assert b["SPY"] == 1.0 and b["TWICE"] == pytest.approx(2.0, rel=0.1)
    x = portfolio.exposure(view("TWICE", "LONG", 100, 50.0, 50.0, []), 2.0)
    y = portfolio.exposure(view("SHORTY", "SHORT", 100, 20.0, 20.0, []), 1.0)
    res = portfolio.stress([x, y], [{"name": "down", "market_move_pct": -10}, {"name": "gap", "largest_gap_pct": 20},
                                    {"name": "one", "moves": {"SHORTY": 50}}], 10_000)
    down, gap, one = res
    assert down["pnl"] == pytest.approx(100 * 50 * -0.2 + 100 * 20 * 0.1)
    assert gap["worst"]["symbol"] == "TWICE" and gap["pnl"] == pytest.approx(-1000)
    assert one["pnl"] == pytest.approx(-1000) and one["share_of_account"] == pytest.approx(-0.1)


def test_portfolio_through_the_api_and_the_ticket_check(tmp_path):
    settings = Settings(data_mode="demo", db_path=tmp_path / "t.db", account_size=20_000)
    svc = TradeAssistant(settings, provider=DemoData(), db=Database(":memory:"))
    c = TestClient(create_app(settings, svc))
    empty = c.get("/v2/portfolio/risk").json()
    assert empty["count"] == 0 and empty["daily_loss"]["mode"] == "REALIZED_PLUS_UNREALIZED"
    c.post("/v2/positions", json={"symbol": "AAPL", "quantity": 100, "invalidation_price": 1, "entry": {"price": 400, "executed_at": "2026-10-01"}})
    c.post("/v2/positions", json={"symbol": "KO", "quantity": 100, "direction": "SHORT", "entry": {"price": 10}})
    r = c.get("/v2/portfolio/risk").json()
    assert r["count"] == 2 and {x["symbol"] for x in r["positions"]} == {"AAPL", "KO"}
    assert len(r["stress"]) == len(portfolio.DEFAULT_SCENARIOS) and r["correlation"]["symbols"] == ["AAPL", "KO"]
    assert any("limit" in w["text"] for w in r["warnings"])
    s = c.post("/v2/portfolio/stress", json={"scenarios": [{"name": "x", "market_move_pct": -10, "moves": {"KO": 20}}]}).json()
    assert s["stress"][0]["name"] == "x"
    assert c.post("/v2/portfolio/stress", json={"scenarios": [{"moves": {"KO": 500}}]}).status_code == 400
    # The ticket's daily loss stop now counts open positions: KO shorted at $10 is deep underwater.
    loss, how = svc.loss_today()
    assert how == "closed and open" and loss < 0


def test_daily_loss_mode_setting(monkeypatch, tmp_path):
    monkeypatch.setenv("DAILY_LOSS_MODE", "realized_only")
    assert load_settings(tmp_path / "none.env").daily_loss_mode == "REALIZED_ONLY"
    monkeypatch.setenv("DAILY_LOSS_MODE", "sometimes")
    with pytest.raises(ValueError):
        load_settings(tmp_path / "none.env")


def test_option_stress_starts_from_zero_and_uses_the_chain_model():
    opt = view("AAPL", "LONG", 10, 10.0, None, [], invalidation=180.0, kind="OPTION",
               mark={"price": 10.0, "underlying": 200.0, "delta": 0.55, "theta": -0.05, "vega": 0.4, "gamma": 0.01,
                     "iv": 0.30, "model_vol": 0.28, "growth": 0.03, "rate": 0.045, "dte": 100},
               legs=[{"option_type": "CALL", "strike": 200.0, "expiration": "2027-01-15"}])
    x = portfolio.exposure(opt, 1.0)
    flat, down = portfolio.stress([x], [{"name": "flat"}, {"name": "down", "market_move_pct": -10}], 100_000)
    assert flat["pnl"] == pytest.approx(0.0, abs=1e-6)                  # no move, no change: no profit or loss
    assert -10.0 * 100 * 10 <= down["pnl"] < 0                         # never more than the premium
    assert 0 < x["risk_to_invalidation"] <= 10.0 * 100 * 10
