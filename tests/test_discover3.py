"""3.0 phase 4: Discover. Every scanner's sighting of the same trade becomes one candidate, ranked on components that
mean the same whoever found it; a conflict, a stale sighting or a missing record shows in the rank and in the
autopilot's reasons; and the one allocator takes off what open orders already hold."""

from __future__ import annotations

import time
from datetime import datetime, timedelta

import pytest
import time_machine
from fastapi.testclient import TestClient

from tradeassist import allocator
from tradeassist.api import create_app
from tradeassist.config import Settings
from tradeassist.db import Database
from tradeassist.optionmath import NY
from tradeassist.providers.demo import DemoData
from tradeassist.service import TradeAssistant
from tradeassist.v3 import discover as D


@pytest.fixture
def client(tmp_path):
    s = Settings(data_mode="demo", db_path=tmp_path / "t.db")
    svc = TradeAssistant(s, provider=DemoData(), db=Database(tmp_path / "t.db"))
    c = TestClient(create_app(s, svc))
    svc.discover3._caught_up = time.time() + 1e6       # no reading of the scanners' own lists unless a test asks
    yield c, svc
    svc.db.close()


def obs(symbol="MSFT", direction="LONG", source="ideas", key=None, horizon="swing", score=70.0, edge=None, catalyst=None, kind="momentum",
        entry=100.0, stop=95.0, target=110.0):
    return {"source": source, "source_key": key or f"{source}:{symbol}:{direction}", "symbol": symbol, "direction": direction,
            "horizon": horizon, "kind": kind, "raw_score": score, "entry": entry, "invalidation": stop, "target": target,
            "catalyst": catalyst, "edge": D._edge(edge), "payload": {"setup_label": kind, "source_score_scale": "0-100"}}


def active(svc):
    return svc.db.query("SELECT * FROM opportunity_candidates WHERE status = 'active' ORDER BY symbol, direction")


# --- one candidate per trade ---------------------------------------------------------------------------------------

def test_three_scanners_that_see_the_same_trade_make_one_candidate(client):
    _, svc = client
    d = svc.discover3
    d.ingest([obs(source="ideas"), obs(source="morning", kind="breakout"), obs(source="reversal", kind="reversal")])
    rows = active(svc)
    assert len(rows) == 1
    c = d.candidate(rows[0]["id"])
    assert sorted(c["sources"]) == ["ideas", "morning", "reversal"] and set(c["kinds"]) == {"momentum", "breakout", "reversal"}
    assert len(c["observations"]) == 3
    assert [x["symbol"] for x in d.feed()] == ["MSFT"]


def test_a_sighting_seen_again_is_refreshed_not_added(client):
    _, svc = client
    d = svc.discover3
    d.ingest([obs(score=50)])
    d.ingest([obs(score=80)])
    n = svc.db.one("SELECT COUNT(*) AS n FROM opportunity_observations")["n"]
    assert n == 1
    c = d.feed()[0]
    quality = next(p for p in c["components"] if p["key"] == "setup_quality")
    assert quality["value"] == pytest.approx(0.8)


def test_a_different_horizon_or_direction_is_a_different_candidate(client):
    _, svc = client
    d = svc.discover3
    d.ingest([obs(horizon="swing"), obs(horizon="intraday", key="morning:MSFT:day", source="morning"), obs(symbol="KO")])
    assert len(active(svc)) == 3


# --- the rank and what it is made of -----------------------------------------------------------------------------

def test_the_rank_is_the_weighted_sum_of_its_shown_parts(client):
    _, svc = client
    d = svc.discover3
    d.ingest([obs(edge={"known": True, "expected_pct": 0.8, "cases": 60, "enough": True, "basis": "test"}, catalyst="Beats estimates")])
    c = d.feed()[0]
    parts = {p["key"]: p for p in c["components"]}
    assert set(parts) == set(D.WEIGHTS)
    positive = sum(w for w in D.WEIGHTS.values() if w > 0)
    expected = 100 * max(0.0, min(1.0, sum(p["weight"] * p["value"] for p in parts.values()) / positive))
    assert c["rank_score"] == pytest.approx(expected, abs=0.01)
    assert parts["catalyst"]["value"] == 1.0 and "news" in parts["catalyst"]["text"]
    assert parts["historical_support"]["value"] > 0 and "60 past cases" in parts["historical_support"]["text"]
    assert all(p["text"] for p in parts.values())


def test_a_validated_record_ranks_above_the_same_setup_without_one(client):
    _, svc = client
    d = svc.discover3
    good = {"known": True, "expected_pct": 0.6, "cases": 80, "enough": True, "basis": "test"}
    d.ingest([obs(symbol="AAPL", edge=good), obs(symbol="KO")])
    ranked = [c["symbol"] for c in d.feed()]
    assert ranked.index("AAPL") < ranked.index("KO")
    ko = next(c for c in d.feed() if c["symbol"] == "KO")
    assert not ko["edge"]["validated"] and any("no validated historical edge" in r for r in ko["autopilot_reasons"])


def test_a_short_or_losing_record_is_not_validated():
    assert not D._edge({"known": True, "expected_pct": 1.2, "cases": 12, "enough": False})["validated"]
    assert not D._edge({"known": True, "expected_pct": -0.3, "cases": 200, "enough": True})["validated"]
    assert D._edge({"known": True, "expected_pct": 0.3, "cases": 30, "enough": True})["validated"]


def test_the_opposite_trade_marks_both_as_conflicted(client):
    _, svc = client
    d = svc.discover3
    d.ingest([obs(direction="LONG")])
    before = d.feed()[0]["rank_score"]
    d.ingest([obs(direction="SHORT", source="reversal", kind="reversal", stop=105, target=90)])
    rows = {c["direction"]: c for c in d.feed()}
    assert set(rows) == {"LONG", "SHORT"}
    for c in rows.values():
        assert next(p for p in c["components"] if p["key"] == "conflict")["value"] == 1.0
        assert "the opposite trade is also flagged" in c["autopilot_reasons"] and c["conflicts"]
    assert rows["LONG"]["rank_score"] < before                     # the long was re-ranked when the short arrived


# --- freshness and expiry ----------------------------------------------------------------------------------------

def test_a_candidate_drops_off_when_no_scan_sees_it_again_and_a_new_sighting_starts_afresh(client):
    _, svc = client
    d = svc.discover3
    d.ingest([obs(source="ideas")])
    first = active(svc)[0]
    with time_machine.travel(datetime.now(NY) + timedelta(days=8), tick=True):
        assert d.feed() == []
        assert svc.db.one("SELECT status FROM opportunity_candidates WHERE id = ?", (first["id"],))["status"] == "expired"
        d.ingest([obs(source="morning", kind="breakout")])
        now = active(svc)
        assert len(now) == 1 and now[0]["id"] != first["id"]
        c = d.candidate(now[0]["id"])
        assert c["sources"] == ["morning"] and len(c["observations"]) == 1      # not the expired one's sighting
        d.ingest([obs(source="ideas")])                                       # the old sighting, seen again, joins it
        c = d.candidate(now[0]["id"])
        assert sorted(c["sources"]) == ["ideas", "morning"]


def test_freshness_falls_as_the_newest_sighting_ages(client):
    _, svc = client
    d = svc.discover3
    d.ingest([obs()])
    fresh = next(p for p in d.feed()[0]["components"] if p["key"] == "freshness")["value"]
    with time_machine.travel(datetime.now(NY) + timedelta(hours=30), tick=True):
        d.ingest([obs(symbol="KO")])                                           # any ingest re-ranks only what it touches
        d._rebuild("MSFT", "LONG", "swing")
        later = next(p for p in d.candidate(active(svc)[1]["id"])["components"] if p["key"] == "freshness")["value"]
    assert fresh > later


# --- the autopilot's bar -----------------------------------------------------------------------------------------

def test_only_a_validated_liquid_unconflicted_trade_on_real_prices_is_autopilot_eligible(client, monkeypatch):
    _, svc = client
    d = svc.discover3
    monkeypatch.setattr(D.Discover, "mode", property(lambda self: "live"))
    monkeypatch.setattr(d, "_liquidity_of", lambda s: {"dollar_volume": 5e9 if s == "AAPL" else 2e6, "spread_bps": 2.0})
    good = {"known": True, "expected_pct": 0.5, "cases": 120, "enough": True, "basis": "test"}
    d.ingest([obs(symbol="AAPL", edge=good), obs(symbol="TINY", edge=good), obs(symbol="KO")])
    rows = {c["symbol"]: c for c in d.feed()}
    assert rows["AAPL"]["autopilot_eligible"] and rows["AAPL"]["autopilot_reasons"] == []
    assert rows["TINY"]["autopilot_reasons"] == ["too thin to trade automatically"]
    assert not rows["KO"]["autopilot_eligible"]
    assert [c["symbol"] for c in d.feed(eligible_only=True)] == ["AAPL"]
    d.ingest([obs(symbol="AAPL", direction="SHORT", source="reversal")])
    assert not d.feed(eligible_only=True)


def test_demo_prices_are_never_eligible(client):
    _, svc = client
    d = svc.discover3
    d.ingest([obs(edge={"known": True, "expected_pct": 0.5, "cases": 120, "enough": True})])
    assert "demo prices are made up" in d.feed()[0]["autopilot_reasons"]


# --- the scanners as observations ---------------------------------------------------------------------------------

def test_a_scanner_signal_becomes_an_observation_with_its_record_after_costs(client, monkeypatch):
    _, svc = client
    from tradeassist import scanner
    row = {"symbol": "XOM", "date": "2026-10-22", "close": 110.0, "rsi": 71.0,
           "signals": [{"key": "macd_bear", "label": "MACD turned down", "direction": -1, "status": "confirmed", "bars_ago": 0,
                        "past": {"cases": 400, "independent_periods": 90, "frequency": 0.54, "mean_return": -0.004, "mean_excess_vs_spy": -0.012}},
                       {"key": "flat", "label": "Nothing", "direction": 0, "status": "forming"}]}
    monkeypatch.setattr(scanner.Scanner, "technical", lambda self, **k: {"rows": [row], "errors": []})
    got, notes = svc.discover3.observe_scanner(["XOM"])
    assert notes == [] and len(got) == 1
    o = got[0]
    assert o["direction"] == "SHORT" and o["source"] == "scanner" and o["raw_score"] == 60.0
    assert o["edge"]["known"] and o["edge"]["cases"] == 90 and o["edge"]["expected_pct"] > 0     # a fall, read the short's way


def test_the_scanners_cached_lists_are_taken_in_when_the_feed_is_read(client, monkeypatch):
    c, svc = client
    d = svc.discover3
    calls = []
    monkeypatch.setattr(d, "observe_hub", lambda sources: (calls.append(sources) or [obs(symbol="NVDA")], []))
    d._caught_up = 0.0
    assert [x["symbol"] for x in c.get("/v3/discover").json()["candidates"]] == ["NVDA"]
    c.get("/v3/discover")
    assert len(calls) == 1                                                    # not again within two minutes


# --- the allocator --------------------------------------------------------------------------------------------------

def test_capital_reserved_by_open_orders_is_taken_off_the_budget_first(client):
    _, svc = client
    mode = svc.market.info.mode
    with svc.db.tx() as cur:
        for kind, amount in (("reserve", 3000.0), ("reserve", 500.0), ("release", 1500.0)):
            cur.execute("INSERT INTO budget_ledger (at, mode, account_kind, kind, amount) VALUES (?, ?, 'paper', ?, ?)",
                        (datetime.now(NY).isoformat(), mode, kind, amount))
    assert allocator.reserved_capital(svc) == pytest.approx(2000.0)
    d = svc.discover3
    d.ingest([obs(symbol="AAPL", entry=None, stop=None, target=None), obs(symbol="MSFT", entry=None, stop=None, target=None)])
    r = d.allocate(D.AllocateIn(budget=6000, instruments=["shares"]))
    assert r["reserved"] == pytest.approx(2000.0) and r["available"] == pytest.approx(4000.0)
    assert any("already reserved" in n for n in r["notes"])
    assert sum(x["dollars"] for x in r["rows"]) <= 4000.0 + 1e-6
    assert r["theoretical_max_loss"] == pytest.approx(sum(x["dollars"] for x in r["rows"]))
    assert all(x["candidate_id"] for x in r["rows"])


def test_levels_for_a_candidate_without_them_come_from_its_daily_range(client):
    _, svc = client
    c = {"id": "opp_x", "symbol": "KO", "direction": "SHORT", "horizon": "swing", "kinds": ["momentum"], "rank_score": 40.0,
         "edge": {"known": False}, "entry": None, "invalidation": None, "target": None}
    o = allocator.to_opportunity(svc, c)
    assert o["stop"] > o["entry"] > o["target_1"] and o["candidate_id"] == "opp_x"


# --- the routes ------------------------------------------------------------------------------------------------------

def test_the_routes(client):
    c, svc = client
    svc.discover3.ingest([obs(symbol="AAPL"), obs(symbol="MSFT", direction="SHORT", stop=105, target=90)])
    d = c.get("/v3/discover?direction=SHORT").json()
    assert [x["symbol"] for x in d["candidates"]] == ["MSFT"] and d["weights"] == D.WEIGHTS and d["candidates"][0]["rank"] == 1
    one = c.get(f"/v3/discover/{d['candidates'][0]['id']}").json()
    assert one["observations"][0]["source"] == "ideas"
    assert c.get("/v3/discover/opp_nope").status_code == 400
    assert c.post("/v3/discover/refresh", json={"sources": ["tea leaves"]}).status_code == 400
    assert c.post("/v3/discover/allocate", json={"budget": -5}).status_code == 422
    assert c.post("/v3/discover/allocate", json={"budget": 5000, "horizon": "intraday"}).status_code == 400   # nothing intraday


def test_a_refresh_runs_the_reversal_scan_and_reports(client):
    c, _ = client
    r = c.post("/v3/discover/refresh", json={"sources": ["reversal"]})
    assert r.status_code == 200
    body = r.json()
    assert body["observations"] >= 0 and isinstance(body["notes"], list) and isinstance(body["feed"], list)
    for x in body["feed"]:
        assert x["sources"] == ["reversal"] and x["kinds"] == ["reversal"]


def test_a_study_started_from_an_opportunity_is_linked_to_it(client):
    c, svc = client
    svc.discover3.ingest([obs(symbol="MSFT")])
    cid = svc.discover3.feed()[0]["id"]
    r = c.post("/v3/studies", json={"symbol": "MSFT", "horizon_days": 5, "thesis": "swing", "opportunity_id": cid}).json()
    assert svc.discover3.candidate(cid)["study_id"] == r["id"]
    other = c.post("/v3/studies", json={"symbol": "AAPL", "horizon_days": 5, "opportunity_id": cid}).json()
    assert svc.discover3.candidate(cid)["study_id"] == r["id"]               # a study of another stock is not linked
    end = time.time() + 90
    while time.time() < end and any(c.get(f"/v3/studies/{i}").json()["status"] == "running" for i in (r["id"], other["id"])):
        time.sleep(0.3)
