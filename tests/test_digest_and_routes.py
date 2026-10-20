"""2.15: the news digest, the saved settings, the blend and paper routes, the flags that switch them off, and the
timed work.

The rules pinned here: the digest ranks only confirmed moves (and movers with news found) inside its window, best
first, each with the points behind its score; every new route answers 404 when its flag is off; a bad request says
what is wrong; the timed work rebuilds the morning plan only in the morning session and only when asked to."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from tradeassist.analysis import Analyst
from tradeassist.api import create_app
from tradeassist.clockwork import Clockwork
from tradeassist.config import Settings
from tradeassist.db import Database
from tradeassist.digest import NewsDigest, window_for
from tradeassist.features import FEATURE_DEFAULTS
from tradeassist.livenews import LiveFeed, iso
from tradeassist.morning import Morning
from tradeassist.optionmath import NY
from tradeassist.providers.demo import DemoData
from tradeassist.service import TradeAssistant


@pytest.fixture
def svc(tmp_path):
    s = Settings(data_mode="demo", db_path=tmp_path / "d.db", account_size=50_000, live_mover_sweep_minutes=0)
    service = TradeAssistant(s, provider=DemoData(live=True), db=Database(tmp_path / "d.db"))
    yield service
    service.db.close()


def _signal(svc, symbol, *, state="opportunity", minutes_ago=5, direction="LONG", tone=0.6, delay=20):
    now = datetime.now(timezone.utc)
    price = svc.market.spot(symbol)
    with svc.db.tx() as cur:
        cur.execute("INSERT INTO live_news (source, external_id, symbols, headline, url, kind, published_at, received_at, tone) "
                    "VALUES ('Demo', ?, ?, ?, NULL, 'press_release', ?, ?, ?)",
                    (f"{symbol}-{minutes_ago}", json.dumps([symbol]), f"{symbol} raises its outlook",
                     iso(now - timedelta(minutes=minutes_ago, seconds=delay)), iso(now - timedelta(minutes=minutes_ago)), tone))
        nid = cur.lastrowid
        d = 1 if direction == "LONG" else -1
        cur.execute("INSERT INTO live_signals (symbol, news_id, news_ids, opened_at, state, direction, path, opportunity_at, ref_price, "
                    "ref_at, prev_close, atr, last_price, updated_at) VALUES (?, ?, ?, ?, ?, ?, 'reaction', ?, ?, ?, ?, ?, ?, ?)",
                    (symbol, nid, json.dumps([nid]), iso(now - timedelta(minutes=minutes_ago)), state, direction,
                     iso(now - timedelta(minutes=minutes_ago - 1)), price * (1 - d * 0.01), iso(now), price * (1 - d * 0.02),
                     price * 0.02, price, iso(now)))


def test_the_window_of_each_choice():
    now = datetime(2026, 10, 20, 11, 0, tzinfo=NY)
    start, words = window_for("30", now)
    assert start == now - timedelta(minutes=30) and "30 minutes" in words
    start, words = window_for("session", now)
    assert (start.hour, start.minute) == (9, 30)
    start, _ = window_for("session", datetime(2026, 10, 20, 8, 0, tzinfo=NY))
    assert (start.hour, start.minute) == (4, 0)
    with pytest.raises(ValueError):
        window_for("5", now)


def test_the_digest_ranks_what_confirmed_in_its_window(svc):
    morning = Morning(svc, Analyst(svc), dict(FEATURE_DEFAULTS))
    feed = LiveFeed(svc, morning, Analyst(svc))
    _signal(svc, "AAPL", minutes_ago=5)
    _signal(svc, "MSFT", minutes_ago=10, tone=-0.6)                 # the headline's tone argues with the move
    _signal(svc, "KO", minutes_ago=50)                               # outside a 15-minute window
    _signal(svc, "XOM", state="watching", minutes_ago=3)             # not confirmed: never listed
    d = NewsDigest(svc, feed, morning).build("15")
    syms = [o["symbol"] for o in d["opportunities"]]
    assert syms[:2] == ["AAPL", "MSFT"] and "KO" not in syms and "XOM" not in syms
    tone = {o["symbol"]: next(p for p in o["score_parts"] if p["key"] == "tone")["points"] for o in d["opportunities"]}
    assert tone["AAPL"] > 0 > tone["MSFT"]
    for o in d["opportunities"]:
        assert o["horizon"] == "day" and o["source"] == "live" and 0 <= o["score"] <= 100
    assert d["run_id"] and d["count"] == 2
    wide = NewsDigest(svc, feed, morning).build("60")
    assert "KO" in [o["symbol"] for o in wide["opportunities"]]
    assert NewsDigest(svc, feed, morning).latest()["interval"] == "60"


@pytest.fixture
def client(svc, monkeypatch):
    monkeypatch.setattr(Clockwork, "ensure_running", lambda self: None)
    with TestClient(create_app(svc.settings, svc)) as c:
        yield c


def test_settings_round_trip_and_bad_values_are_refused(client):
    p = client.get("/v2/prefs").json()
    assert p["blend"]["budget"] == 50_000 and p["morning"]["horizon"] == "day"
    p = client.post("/v2/prefs", json={"live": {"digest": "30"}, "blend": {"goal": "dollars"}}).json()
    assert p["live"]["digest"] == "30" and p["blend"]["goal"] == "dollars"
    r = client.post("/v2/prefs", json={"morning": {"refresh_minutes": 3}})
    assert r.status_code == 400 and "every 5, 10, 15 or 30" in r.json()["error"]


def test_a_blend_needs_opportunities_first_and_a_basket_works_by_hand(client):
    r = client.post("/v2/blend", json={"source": "morning", "budget": 10_000})
    assert r.status_code == 400 and "Morning plan" in r.json()["error"]
    legs = [{"symbol": "AAPL", "direction": "LONG", "horizon": "swing"}, {"symbol": "KO", "direction": "SHORT", "horizon": "swing"}]
    r = client.post("/v2/blend", json={"source": "basket", "legs": legs, "budget": 20_000, "goal": "dollars", "instruments": ["shares", "short"]})
    assert r.status_code == 200
    out = r.json()
    assert {o["symbol"] for o in out["opportunities"]} == {"AAPL", "KO"}
    assert all(not o["edge"]["known"] for o in out["opportunities"])     # added by hand: no record, no edge
    assert out["rows"] == [] and out["fallback"]["rows"]                  # so the split holds cash; equal risk for study


def test_paper_through_the_routes(client):
    r = client.post("/v2/paper/portfolios", json={"mode": "MANUAL", "budget": 10_000, "horizon": "swing", "ends": "open",
                                                  "trades": [{"symbol": "AAPL", "instrument": "shares", "quantity": 5}]})
    assert r.status_code == 200
    d = r.json()
    pid = d["portfolio"]["id"]
    assert d["positions"][0]["status"] == "OPEN" and d["stats"]["open"] == 1
    assert client.get("/v2/paper").json()["portfolios"][0]["id"] == pid
    pos = d["positions"][0]["id"]
    d = client.post(f"/v2/paper/positions/{pos}/close", json={"quantity": 2}).json()
    assert d["positions"][0]["open_quantity"] == 3
    d = client.post(f"/v2/paper/portfolios/{pid}/mark", json={}).json()
    assert d["marks"]
    bad = client.post(f"/v2/paper/portfolios/{pid}/trades", json={"symbol": "AAPL", "instrument": "shares", "quantity": 10_000})
    assert bad.status_code == 400 and "free in this portfolio" in bad.json()["error"]
    assert client.post(f"/v2/paper/portfolios/{pid}/close", json={}).json()["portfolio"]["status"] == "CLOSED"
    assert client.post(f"/v2/paper/portfolios/{pid}/delete", json={}).json()["portfolios"] == []
    assert client.get("/v2/paper/runs").json()["runs"] == []


def test_the_flags_switch_the_new_areas_off(tmp_path, monkeypatch):
    monkeypatch.setattr(Clockwork, "ensure_running", lambda self: None)
    s = Settings(data_mode="demo", db_path=tmp_path / "f.db",
                 features={**FEATURE_DEFAULTS, "opportunity_blend": False, "paper_trading": False})
    service = TradeAssistant(s, provider=DemoData(), db=Database(tmp_path / "f.db"))
    with TestClient(create_app(s, service)) as c:
        assert c.post("/v2/blend", json={"budget": 1000}).status_code == 404
        assert c.get("/v2/opportunities/morning").status_code == 404
        assert c.get("/v2/paper").status_code == 404
        assert c.post("/v2/paper/portfolios", json={"budget": 1000}).status_code == 404
    service.db.close()


def test_the_timed_work_rescans_the_morning_only_when_asked_and_in_the_session(svc, monkeypatch):
    from tradeassist.opportunities import save_prefs

    class FakeMorning:
        running = False
        started = 0

        def start(self, rescan=False):
            FakeMorning.started += 1

    cw = Clockwork(svc, morning=FakeMorning())
    open_ = datetime(2026, 10, 20, 10, 0, tzinfo=NY)
    assert not cw.needed() and cw.tick(open_)["morning"] is None
    save_prefs(svc.db, svc.settings, {"morning": {"auto_refresh": True, "refresh_minutes": 5, "am_end": "11:30"}})
    assert cw.needed()
    assert cw.tick(open_)["morning"] == "rescan started" and FakeMorning.started == 1
    assert cw.tick(open_)["morning"] is None                         # not again within five minutes
    cw.last["morning_at"] = 0
    assert cw.tick(datetime(2026, 10, 20, 11, 45, tzinfo=NY))["morning"] is None    # after the morning session
    monkeypatch.setattr(svc.market, "clock", lambda: {"state": "closed"})
    cw.last["morning_at"] = 0
    assert cw.tick(open_)["morning"] is None                          # the market is shut


# --- from the independent review of 2.15 ----------------------------------------------------------------------

def test_the_session_window_never_starts_after_now():
    start, _ = window_for("session", datetime(2026, 10, 20, 3, 0, tzinfo=NY))          # 3:00 on a Tuesday
    assert start <= datetime(2026, 10, 20, 3, 0, tzinfo=NY) and start.date().isoformat() == "2026-10-19"


def test_starting_live_news_starts_the_timed_work(svc, monkeypatch):
    calls = []
    monkeypatch.setattr(Clockwork, "ensure_running", lambda self: calls.append(1))
    from tradeassist.livenews import LiveFeed
    monkeypatch.setattr(LiveFeed, "source_names", lambda self: ["Demo"])
    monkeypatch.setattr(LiveFeed, "start", lambda self: (setattr(self, "_test_running", True), {"running": True})[1])
    monkeypatch.setattr(LiveFeed, "running", property(lambda self: getattr(self, "_test_running", False)), raising=False)
    with TestClient(create_app(svc.settings, svc)) as c:
        before = len(calls)
        assert c.post("/v2/live/start", json={}).status_code == 200
        assert len(calls) > before


def test_a_real_money_split_always_obeys_the_limits(client):
    legs = [{"symbol": "AAPL", "direction": "LONG", "horizon": "swing"}]
    r = client.post("/v2/blend", json={"source": "basket", "legs": legs, "budget": 20_000, "obey_limits": False}).json()
    assert r["limits"]["obey"] is True
    r = client.post("/v2/blend", json={"source": "basket", "legs": legs, "budget": 20_000, "obey_limits": False, "paper": True}).json()
    assert r["limits"]["obey"] is False and r["limits"]["includes_real"] is False
