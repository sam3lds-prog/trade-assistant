"""Journal analytics: per-trade figures, excursions, habits and the totals."""

import pandas as pd
import pytest
from fastapi.testclient import TestClient

from tradeassist import analytics
from tradeassist.api import create_app
from tradeassist.config import Settings
from tradeassist.db import Database
from tradeassist.providers.demo import DemoData
from tradeassist.service import TradeAssistant


def row(pnl, r=None, setup="breakout", bias=1, days=5, flags=(), closed="2026-09-01"):
    return {"id": 1, "symbol": "X", "description": "x", "instrument_type": "STOCK", "direction": "LONG", "bias": bias,
            "setup_type": setup, "opened_at": None, "closed_at": f"{closed}T20:00:00+00:00", "days_held": days, "pnl": pnl,
            "fees": 0, "r_multiple": r, "initial_risk": None, "trend_at_entry": "UP", "regime_at_entry": "BULL_CALM",
            "excursions": {"available": False}, "behaviour": list(flags), "data_source": "live"}


def test_totals_profit_factor_expectancy_and_streaks():
    rows = [row(300, 1.5), row(-100, -0.5), row(-100, -0.5), row(200, 1.0), row(-50, None)]
    s = analytics.summarize(rows)
    assert s["trades"] == 5 and s["wins"] == 2 and s["win_rate"] == pytest.approx(0.4)
    assert s["profit_factor"] == pytest.approx(500 / 250) and s["expectancy"] == pytest.approx(50)
    assert s["expectancy_r"] == pytest.approx(0.375) and s["r_trades"] == 4
    assert s["longest_losing_streak"] == 2 and s["largest_loss"] == -100 and not s["enough"]
    assert analytics.summarize([row(10)])["profit_factor"] is None          # no losses: undefined, not infinite


def test_journal_groups_drawdown_and_habits():
    rows = [row(100, 1, closed="2026-09-01"), row(-300, -1, setup="pullback", flags=["held_past_invalidation"], closed="2026-09-02"),
            row(50, 0.5, bias=-1, days=30, closed="2026-09-03")]
    j = analytics.journal_stats(rows)
    assert j["max_drawdown"] == pytest.approx(-300) and [p["equity"] for p in j["equity_curve"]] == [100, -200, -150]
    assert {g["group"] for g in j["by_setup"]} == {"breakout", "pullback"}
    assert {g["group"] for g in j["by_direction"]} == {"bet on a rise", "bet on a fall"}
    habit = next(b for b in j["behaviour"] if b["key"] == "held_past_invalidation")
    assert habit["with"]["expectancy"] == -300 and habit["without"]["expectancy"] == 75
    assert any("fewer than 20" in n for n in j["notes"])


def test_excursions_on_the_stock_in_percent_and_r():
    bars = pd.DataFrame({"date": pd.bdate_range("2026-09-01", periods=6), "open": 100.0,
                         "high": [101, 104, 106, 103, 102, 108.0], "low": [99, 97, 99, 94, 96, 100.0],
                         "close": [100, 103, 104, 95, 100, 107.0], "volume": 1e6})
    pos = {"instrument_type": "STOCK", "direction": "LONG", "bias": 1, "opened_at": "2026-09-01T14:00:00+00:00",
           "closed_at": "2026-09-07T20:00:00+00:00", "invalidation_price": 96.0, "target_1": 110.0,
           "ledger": {"average_entry_price": 100.0}, "snapshots": [], "status": "CLOSED", "executions": []}
    e = analytics.excursions(pos, bars)
    # Sept 2 to Sept 7 (exit day): highs up to 106, lows down to 94.
    assert e["mfe_pct"] == pytest.approx(6.0) and e["mae_pct"] == pytest.approx(-6.0)
    assert e["mfe_r"] == pytest.approx(1.5) and e["mae_r"] == pytest.approx(-1.5)
    assert e["closed_beyond_invalidation"] and not e["reached_target_1"]
    assert "held_past_invalidation" in analytics.behaviour(pos, e, 1000)
    short = {**pos, "direction": "SHORT", "bias": -1, "invalidation_price": 108.0, "target_1": 95.0}
    s = analytics.excursions(short, bars)
    assert s["mfe_pct"] == pytest.approx(6.0) and s["mae_pct"] == pytest.approx(-6.0) and s["reached_target_1"]
    never = analytics.excursions({**pos, "ledger": {"average_entry_price": 90.0}}, bars)
    assert never["mae_pct"] == 0.0                                   # never below the entry: no adverse move


def test_habits_from_fills_and_risk():
    pos = {"direction": "LONG", "bias": 1, "status": "CLOSED", "target_1": None, "snapshots": [{"snapshot_type": "ENTRY", "trend_state": "DOWN"}],
           "executions": [{"side": "BUY", "price": 100.0}, {"side": "BUY", "price": 95.0}, {"side": "SELL", "price": 99.0}],
           "ledger": {"initial_risk": 900.0}, "legacy_overrides": ["spread"]}
    flags = analytics.behaviour(pos, {"available": False}, 500.0)
    assert set(flags) == {"added_to_a_loser", "oversized", "against_the_trend", "overrode_a_limit"}


def test_stats_and_position_analytics_through_the_api(tmp_path):
    settings = Settings(data_mode="demo", db_path=tmp_path / "t.db")
    c = TestClient(create_app(settings, TradeAssistant(settings, provider=DemoData(), db=Database(":memory:"))))
    assert c.get("/v2/journal/stats").json()["overall"]["trades"] == 0
    p = c.post("/v2/positions", json={"symbol": "KO", "quantity": 10, "invalidation_price": 1, "setup_type": "pullback",
                                      "entry": {"price": 60, "executed_at": "2026-08-03"}}).json()
    entry = p["snapshots"][0]
    assert entry["data_timestamp"] == "2026-08-03" and entry["raw"]["as_of"] == "2026-08-03"   # as of the fill date
    c.post(f"/v2/positions/{p['id']}/executions", json={"side": "SELL", "quantity": 10, "price": 62, "executed_at": "2026-08-14"})
    j = c.get("/v2/journal/stats").json()
    assert j["overall"]["trades"] == 1 and j["overall"]["total"] == pytest.approx(20)
    assert j["trades"][0]["trend_at_entry"] == entry["trend_state"] and j["trades"][0]["excursions"]["available"]
    a = c.get(f"/v2/positions/{p['id']}/analytics").json()
    assert a["compared_with"] == "EXIT" and a["excursions"]["days"] >= 8
