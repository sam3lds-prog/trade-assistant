"""Alerts only: the rules, and that the endpoint changes nothing."""

from datetime import date

from fastapi.testclient import TestClient

from tradeassist import alerts
from tradeassist.api import create_app
from tradeassist.config import Settings
from tradeassist.db import Database
from tradeassist.providers.demo import DemoData
from tradeassist.service import TradeAssistant


def view(**kw):
    base = {"id": 7, "symbol": "AAPL", "description": "Long 10 AAPL shares", "bias": 1, "instrument_type": "STOCK",
            "legs": [], "thesis_check": {"status": "INTACT", "text": ""}}
    return {**base, **kw}


def test_position_rules():
    today = date(2026, 10, 6)
    inv = alerts.position_alerts(view(thesis_check={"status": "INVALIDATED", "text": "through 190"}), None, None, today)
    assert inv[0]["kind"] == "INVALIDATED" and inv[0]["level"] == "fail"
    t = {"confirmed": {"state": "DOWN"}, "provisional": None}
    assert alerts.position_alerts(view(), t, None, today)[0]["kind"] == "TREND_AGAINST"
    assert not alerts.position_alerts(view(bias=-1), t, None, today)                # a short is with a downtrend
    prov = alerts.position_alerts(view(), {"confirmed": {"state": "UP"}, "provisional": {"state": "STRONG_DOWN"}}, None, today)
    assert prov[0]["status"] == "PROVISIONAL"
    opt = view(instrument_type="OPTION", legs=[{"expiration": "2026-10-09"}], description="Long 1 AAPL call")
    assert alerts.position_alerts(opt, None, None, today)[0]["kind"] == "EXPIRING"
    soon = alerts.position_alerts(view(), None, {"date": "2026-10-08", "estimated": True}, today)
    assert soon[0]["kind"] == "EARNINGS_SOON" and "estimated" in soon[0]["text"]
    assert not alerts.position_alerts(view(), None, {"date": "2026-11-20"}, today)
    acct = alerts.account_alerts({"status": "STOP", "text": "stop"}, 5000, 1000)
    assert [a["kind"] for a in alerts.sort_alerts(acct)] == ["DAILY_LOSS", "OPEN_RISK"]


def test_alerts_endpoint_only_reads(tmp_path):
    settings = Settings(data_mode="demo", db_path=tmp_path / "t.db")
    svc = TradeAssistant(settings, provider=DemoData(), db=Database(":memory:"))
    c = TestClient(create_app(settings, svc))
    spot = svc.market.spot("KO")
    p = c.post("/v2/positions", json={"symbol": "KO", "quantity": 10, "invalidation_price": round(spot * 1.05, 2),
                                      "entry": {"price": round(spot * 1.10, 2)}}).json()
    before = svc.db.query("SELECT * FROM executions"), svc.db.query("SELECT id, status, open_quantity FROM positions")
    a = c.post("/v2/alerts/check", json={}).json()
    assert any(x["kind"] == "INVALIDATED" and x["position_id"] == p["id"] for x in a["alerts"])
    assert "nothing is bought" in a["note"]
    after = svc.db.query("SELECT * FROM executions"), svc.db.query("SELECT id, status, open_quantity FROM positions")
    assert [dict(r) for r in before[0]] == [dict(r) for r in after[0]] and [dict(r) for r in before[1]] == [dict(r) for r in after[1]]
    assert c.get("/v2/alerts").json()["alerts"]
