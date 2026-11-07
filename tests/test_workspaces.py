"""3.0 phase 2: saved workspaces, the session, the command palette's tickers and the watchlist quotes."""

import pytest
from fastapi.testclient import TestClient

from tradeassist.api import create_app
from tradeassist.config import Settings
from tradeassist.db import Database
from tradeassist.providers.demo import DemoData
from tradeassist.service import TradeAssistant


@pytest.fixture
def client(tmp_path):
    settings = Settings(data_mode="demo", db_path=tmp_path / "t.db")
    svc = TradeAssistant(settings, provider=DemoData(), db=Database(tmp_path / "t.db"))
    return TestClient(create_app(settings, svc)), svc


def layout(**kw):
    base = {"workspace": "research", "name": "Earnings week",
            "widgets": [{"id": "a", "type": "chart", "x": 0, "y": 0, "w": 8, "h": 9, "group": "blue"},
                        {"id": "b", "type": "chain", "x": 8, "y": 0, "w": 4, "h": 9, "group": "blue", "config": {"side": "put"}},
                        {"id": "c", "type": "classic:analyze", "x": 0, "y": 9, "w": 12, "h": 10, "group": "none", "collapsed": True}],
            "context": {"blue": {"symbol": "GOOGL", "horizon": "swing", "contract": {"option_symbol": "GOOGL261218P00180000"}}}}
    base.update(kw)
    return base


def test_a_layout_is_saved_listed_reopened_and_replaced_by_name(client):
    c, svc = client
    before = svc.bus.latest_id()
    saved = c.post("/v3/workspaces", json=layout()).json()
    assert saved["id"].startswith("ws_") and saved["workspace"] == "research" and len(saved["widgets"]) == 3
    assert saved["context"]["blue"]["symbol"] == "GOOGL" and saved["widgets"][2]["collapsed"]
    assert [e["topic"] for e in svc.bus.since(before)] == ["workspace.saved"]
    again = c.post("/v3/workspaces", json=layout(widgets=layout()["widgets"][:1])).json()
    assert again["id"] == saved["id"] and len(again["widgets"]) == 1                 # same workspace and name: replaced
    other = c.post("/v3/workspaces", json=layout(name="Swing", is_default=True)).json()
    assert other["id"] != saved["id"] and other["is_default"]
    third = c.post("/v3/workspaces", json=layout(name="Day trade", is_default=True)).json()
    listing = c.get("/v3/workspaces").json()
    defaults = [x["name"] for x in listing["layouts"] if x["workspace"] == "research" and x["is_default"]]
    assert defaults == ["Day trade"] and third["is_default"]                       # one default per workspace
    assert c.get(f"/v3/workspaces/{saved['id']}").json()["name"] == "Earnings week"
    assert c.post(f"/v3/workspaces/{saved['id']}/delete").json() == {"deleted": saved["id"]}
    assert c.get(f"/v3/workspaces/{saved['id']}").status_code == 400


@pytest.mark.parametrize("bad, why", [
    ({"workspace": "casino"}, "workspace"),
    ({"widgets": [{"id": "a", "type": "chart", "x": 0, "y": 0, "w": 4, "h": 4, "group": "teal"}]}, "link group"),
    ({"widgets": [{"id": "a", "type": "Chart!", "x": 0, "y": 0, "w": 4, "h": 4}]}, "not a widget type"),
    ({"widgets": [{"id": "a", "type": "chart", "x": 0, "y": 0, "w": 4, "h": 4}] * 2}, "twice"),
])
def test_a_layout_that_cannot_be_rebuilt_is_refused(client, bad, why):
    c, _ = client
    r = c.post("/v3/workspaces", json=layout(**bad))
    assert r.status_code == 400 and why in r.json()["error"]


def test_the_shape_of_a_layout_is_checked_before_anything_is_saved(client):
    c, _ = client
    assert c.post("/v3/workspaces", json=layout(widgets=[{"id": "a", "type": "chart", "x": 0, "y": 0, "w": 13, "h": 4}])).status_code == 422
    assert c.post("/v3/workspaces", json={**layout(), "surprise": 1}).status_code == 422
    many = [{"id": f"w{i}", "type": "chart", "x": 0, "y": i, "w": 2, "h": 2} for i in range(41)]
    assert c.post("/v3/workspaces", json=layout(widgets=many)).status_code == 422
    huge = layout(context={"blue": {"note": "x" * 210_000}})
    assert c.post("/v3/workspaces", json=huge).status_code == 400
    assert c.get("/v3/workspaces").json()["layouts"] == []


def test_the_session_keeps_the_open_tabs_the_dock_and_each_link_groups_context(client):
    c, _ = client
    assert c.get("/v3/workspaces").json()["session"] == {"tabs": [], "active": None, "dock": {}, "context": {}}
    body = {"tabs": [{"id": "t1", "workspace": "research", "name": "Research", "widgets": layout()["widgets"]}], "active": "t1",
            "dock": {"widget": {"type": "ticket", "group": "blue", "config": {"side": "sell", "contracts": 3}}, "open": True},
            "context": {"blue": {"symbol": "NVDA", "direction": "SHORT"}, "green": {"symbol": "AMD"}}}
    assert c.post("/v3/workspaces/session", json=body).status_code == 200
    back = c.get("/v3/workspaces").json()["session"]
    assert back == body
    assert c.post("/v3/workspaces/session", json={**body, "tabs": [body["tabs"][0]] * 13}).status_code == 422


def test_the_palette_knows_the_watchlist_and_the_large_companies_and_quotes_come_back_in_order(client):
    c, svc = client
    syms = c.get("/v3/symbols").json()
    assert syms["watchlist"] == list(svc.settings.watchlist) and {"AAPL", "SPY", "XLK"} <= set(syms["symbols"])
    assert len(syms["symbols"]) == len(set(syms["symbols"]))
    q = c.get("/v3/quotes?symbols=msft,AAPL").json()
    assert [x["symbol"] for x in q["quotes"]] == ["MSFT", "AAPL"] and all(x["last"] > 0 for x in q["quotes"])
    assert all(x["change_pct"] is not None for x in q["quotes"]) and q["data_source"] == "demo"
    assert c.get("/v3/quotes?symbols=").status_code == 400


def test_the_workstation_and_the_classic_screens_each_have_a_page(client):
    c, _ = client
    ws, classic = c.get("/"), c.get("/classic")
    assert ws.status_code == classic.status_code == 200
    assert 'data-app="workstation"' in ws.text and "/ws-core.js?v=" in ws.text and "/gridstack.js?v=" in ws.text
    assert 'data-view="morning"' in classic.text and "ws-core.js" not in classic.text
    assert c.get("/gridstack.css").status_code == 200 and c.get("/LICENSE-gridstack.txt").status_code == 200
