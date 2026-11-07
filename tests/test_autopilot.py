"""3.0 phase 7: the capped autopilot. Off unless the policy says so; only autopilot-eligible opportunities; sized and
counted within the policy; every order through the same risk engine and an approval bound to its hash; its own
exits; a shadow book; and live only after the promotion gates."""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest
import time_machine
from fastapi.testclient import TestClient

from tradeassist import policy as P
from tradeassist.api import create_app
from tradeassist.config import Settings
from tradeassist.db import Database
from tradeassist.optionmath import NY
from tradeassist.providers.demo import DemoData
from tradeassist.service import RequestError, TradeAssistant
from tradeassist.v3 import discover as D
from tradeassist.v3.trading import ProposalIn

pytestmark = pytest.mark.clock("2026-10-22T10:30")
GOOD = {"known": True, "expected_pct": 0.6, "cases": 120, "enough": True, "basis": "test"}


@pytest.fixture
def ap(tmp_path, monkeypatch):
    s = Settings(data_mode="demo", db_path=tmp_path / "t.db", desk_worker="off")
    svc = TradeAssistant(s, provider=DemoData(), db=Database(tmp_path / "t.db"))
    client = TestClient(create_app(s, svc))
    # Discover sees real, liquid prices here, so candidates can be eligible (on demo data they never are).
    monkeypatch.setattr(D.Discover, "mode", property(lambda self: "live"))
    monkeypatch.setattr(svc.discover3, "_liquidity_of", lambda sym: {"dollar_volume": 5e9, "spread_bps": 2.0})
    svc.discover3._caught_up = 1e12
    yield svc, svc.autopilot, client
    svc.db.close()


def policy(svc, **kw):
    base = {**P.current(svc.db).model_dump(), "mode": "capped_autopilot", "account_kind": "paper", "trial_capital": 1000, "max_order": 100,
            "max_daily_new_buys": 500, "autopilot_min_score": 40, "autopilot_max_orders_per_day": 2}
    return P.save(svc.db, {**base, **kw}, live_allowed=True)


def candidate(svc, symbol="AMZN", edge=GOOD, direction="LONG", stop=20.0, target=40.0):
    svc.discover3.ingest([{"source": "ideas", "source_key": f"ideas:{symbol}:{direction}", "symbol": symbol, "direction": direction,
                           "horizon": "swing", "kind": "momentum", "raw_score": 70.0, "entry": 29.0, "invalidation": stop, "target": target,
                           "catalyst": None, "edge": D._edge(edge), "payload": {}}])


def run_jobs(svc):
    return svc.trading.make_worker("t", None).drain()


def autopilot_rows(svc):
    return svc.db.query("SELECT * FROM trade_proposals WHERE source = 'autopilot' ORDER BY created_at")


def test_it_is_off_unless_the_policy_turns_it_on(ap):
    svc, pilot, _ = ap
    candidate(svc)
    assert "off" in pilot.run()["skipped"]
    assert autopilot_rows(svc) == []


def test_it_buys_an_eligible_opportunity_within_the_policy_and_records_why(ap):
    svc, pilot, _ = ap
    policy(svc)
    candidate(svc)
    r = pilot.run()
    assert r["gates"]["ok"] and len(r["entries"]) == 1 and r["entries"][0]["status"] == "authorized"
    p = svc.trading.get(r["entries"][0]["proposal"])
    assert p["source"] == "autopilot" and p["terms"]["quantity"] == 3                       # $100 an order at about $29
    assert p["authorization"]["by"] == "autopilot" and p["authorization"]["channel"] == "policy"
    assert p["authorization"]["order_hash"] == p["order_hash"] and p["risk"]["ok"]
    assert p["exit_plan"]["stop"] == 20.0 and p["exit_plan"]["target"] == 40.0 and p["exit_plan"]["time_exit"]
    run_jobs(svc)
    assert svc.trading.get(p["id"])["status"] == "filled"
    again = pilot.run()
    assert again["entries"] == [] and any(x.get("why") == "already held or proposed" for x in again["skipped"])
    assert svc.db.one("SELECT COUNT(*) AS n FROM audit_log WHERE action = 'autopilot.run'")["n"] == 2


def test_it_passes_over_what_is_not_eligible_or_scores_too_low(ap):
    svc, pilot, _ = ap
    policy(svc, autopilot_min_score=95)
    candidate(svc, "AMZN")
    candidate(svc, "KO", edge={"known": True, "expected_pct": 0.5, "cases": 12, "enough": False})       # too short a record
    r = pilot.run()
    assert r["entries"] == []
    assert [x["symbol"] for x in r["skipped"] if "score" in x.get("why", "")] == ["AMZN"]
    assert all(x.get("symbol") != "KO" for x in r["skipped"])                                       # not even considered


def test_it_keeps_to_its_buys_a_day(ap):
    svc, pilot, _ = ap
    policy(svc, autopilot_max_orders_per_day=1, max_order=1000, trial_capital=10_000, max_daily_new_buys=10_000)
    candidate(svc, "AMZN")
    candidate(svc, "MSFT")
    r = pilot.run()
    assert len(r["entries"]) == 1
    assert "the most the policy allows" in pilot.run()["skipped"][0]["why"]


def test_it_sells_at_its_stop_its_target_or_its_time(ap):
    svc, pilot, _ = ap
    policy(svc)
    candidate(svc, stop=29.5, target=40.0)                    # a stop above the price: the next run sells
    pilot.run()
    run_jobs(svc)
    r = pilot.run()
    assert len(r["exits"]) == 1 and "the stop" in r["exits"][0]["why"] and r["exits"][0]["status"] == "authorized"
    run_jobs(svc)
    assert svc.trading.positions("paper")["positions"] == []
    sells = [x for x in autopilot_rows(svc) if x["side"] == "sell"]
    assert len(sells) == 1 and sells[0]["status"] == "filled"


def test_time_runs_out_on_a_swing(ap):
    svc, pilot, _ = ap
    policy(svc)
    candidate(svc, stop=10.0, target=90.0)
    pilot.run()
    run_jobs(svc)
    assert pilot.run()["exits"] == []
    with time_machine.travel(datetime(2026, 10, 29, 15, 55, tzinfo=NY), tick=True):     # five trading days on, after 15:50
        svc.discover3.expire()
        out = pilot._exits(svc.trading.policy(), datetime.now(NY))
    assert len(out) == 1 and out[0]["why"] == "its time ran out"


def test_the_shadow_account_keeps_its_own_book(ap):
    svc, pilot, _ = ap
    policy(svc, account_kind="shadow")
    candidate(svc)
    pilot.run()
    run_jobs(svc)
    assert [p["symbol"] for p in svc.trading.positions("shadow")["positions"]] == ["AMZN"]
    assert svc.trading.positions("paper")["positions"] == []
    assert pilot.record()["days"] == 1


def test_live_needs_every_promotion_gate(ap, monkeypatch):
    svc, pilot, client = ap
    r = client.put("/v3/desk/policy", json={**P.current(svc.db).model_dump(), "mode": "capped_autopilot", "account_kind": "live"})
    assert r.status_code == 400 and "AUTOPILOT_LIVE" in r.json()["error"]
    policy(svc, account_kind="live")
    candidate(svc)
    g = pilot.gates()
    failing = {c["key"] for c in g["checks"] if c["status"] != "pass"}
    assert not g["ok"] and {"env", "live", "shadow_days"} <= failing
    assert pilot.run()["entries"] == [] and autopilot_rows(svc) == []
    import dataclasses
    monkeypatch.setattr(svc, "settings", dataclasses.replace(svc.settings, autopilot_live=True))
    svc.trading.live_ready = True
    monkeypatch.setattr(pilot, "record", lambda: {"days": 10, "result": -30.0})
    assert {c["key"] for c in pilot.gates()["checks"] if c["status"] != "pass"} == {"shadow_result"}       # lost more than a day's stop
    monkeypatch.setattr(pilot, "record", lambda: {"days": 10, "result": 12.5})
    assert pilot.gates()["ok"]


def test_the_autopilot_cannot_approve_outside_its_mode_or_through_the_kill_switch(ap):
    svc, pilot, _ = ap
    p = svc.trading.create(ProposalIn(symbol="AMZN", quantity=1))
    with pytest.raises(RequestError, match="capped_autopilot"):
        svc.trading.decide(p["pending_approval"]["id"], True, p["order_hash"], actor="autopilot", channel="policy")
    policy(svc)
    svc.trading.set_kill_switch(True, "test")
    candidate(svc)
    r = pilot.run()
    assert not r["gates"]["ok"] and r["entries"] == []


def test_after_a_sleep_a_missed_run_is_not_replayed(ap):
    svc, pilot, _ = ap
    policy(svc)
    w = svc.trading.make_worker("t", None)
    w.step()
    queued = svc.db.query("SELECT due_at FROM jobs WHERE kind = 'autopilot.run'")
    assert len(queued) == 1
    with time_machine.travel(datetime.now(NY) + timedelta(hours=3), tick=False):
        w.jobs.discard_stale()
        from tradeassist.jobs import schedule_tick
        schedule_tick(w.jobs, w.schedules)
    rows = svc.db.query("SELECT status FROM jobs WHERE kind = 'autopilot.run' ORDER BY id")
    assert [r["status"] for r in rows][-1] == "queued" and len(rows) == 2


def test_the_routes(ap):
    svc, _, client = ap
    st = client.get("/v3/autopilot").json()
    assert not st["gates"]["ok"] and st["record"] == {"days": 0, "result": 0.0}
    assert "off" in client.post("/v3/autopilot/run").json()["skipped"]
