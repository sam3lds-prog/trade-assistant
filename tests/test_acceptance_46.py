"""4.6.0 acceptance: the plan's Scenarios A to H, end to end through the API where they can be (demo market data, a
MiroFish stand-in that speaks the pinned server's routes), and on the pieces themselves where the scenario is about
a calculation (F) or about accumulated evidence (G, H). The real self-hosted MiroFish is exercised separately and
only on request: `python run.py --scenario-smoke SPY` (it costs model calls)."""

from __future__ import annotations

import json
from dataclasses import replace
from datetime import datetime, timezone

import pytest
import requests
from fastapi.testclient import TestClient

from tradeassist.api import create_app
from tradeassist.config import Settings
from tradeassist.db import Database
from tradeassist.providers.demo import DemoData
from tradeassist.service import TradeAssistant
from tradeassist.v3 import simulation as G
from tradeassist.v3 import valuation as V
from tradeassist.v3.opportunity import evaluate
from tradeassist.v3.scenario_providers import MiroFish
from tests.test_one_click import FakeMiroFish, _wait, drain
from tests.test_v451 import FakeSvc, _row, call, study, version


@pytest.fixture(scope="module")
def app(tmp_path_factory):
    path = tmp_path_factory.mktemp("acc46") / "t.db"
    s = Settings(data_mode="demo", db_path=path, desk_worker="off", scenario_provider="mirofish", scenario_auto=True,
                 mirofish_url="http://127.0.0.1:5001", mirofish_cost_per_run_usd=0.20)
    svc = TradeAssistant(s, provider=DemoData(), db=Database(path))
    c = TestClient(create_app(s, svc))
    yield c, svc, s
    svc.db.close()


@pytest.fixture
def mirofish(app):
    c, svc, s = app
    state = {}

    def use(fake=None, **settings):
        svc.settings = replace(s, **settings)
        state["fake"] = fake or FakeMiroFish()
        p = G.profile(svc.settings, "quick")
        svc.scenario_provider_override = lambda name: MiroFish("http://127.0.0.1:5001", session=state["fake"], poll_first=0.0, poll_most=0.0,
                                                               max_rounds=p["rounds"], max_agents=p["agents"], platform=p["platform"],
                                                               parallel_profiles=p["parallel_profiles"], profile="quick")
        return state["fake"]
    yield use
    svc.settings = s
    svc.scenario_provider_override = None
    with svc.db.tx() as cur:
        cur.execute("DELETE FROM kv_cache WHERE key = ?", (G.CIRCUIT_KEY,))


def _study(c, sym, key):
    s = c.post("/v3/studies", json={"symbol": sym, "horizon_days": 5, "request_key": key}).json()
    return _wait(c, s["id"])


def test_a_successful_research_run(app, mirofish):
    """A: one click gives the quantitative forecast, frozen evidence, a MiroFish run, three checked cases, saved paths,
    the strategies repriced, a decision, the run's cost record and the forward record."""
    c, svc, _ = app
    fake = mirofish()
    s = _study(c, "AAPL", "accept-a-0001")
    assert s["study"]["forecast"] and s["pipeline"]["status"] == "running"
    drain(svc)
    p = c.get(f"/v3/studies/{s['id']}/pipeline").json()["pipeline"]
    assert p["status"] == "complete", p["error"]
    assert svc.db.one("SELECT packet_json FROM scenario_seeds WHERE id = ?", (p["seed_id"],))                    # frozen evidence
    assert ("POST", "/api/simulation/start") in fake.calls and p["run"]["remote"]["simulation_id"]
    v = c.get(f"/v3/scenario-versions/{p['version_id']}").json()
    assert [x["key"] for x in v["cases"]] == ["central", "bullish", "bearish"] and v["origin"] == "auto"
    assert p["valuation"]["structures"] and all("quality" in x for x in p["valuation"]["structures"])
    assert p["decision"]["status"] in ("qualified", "watch", "no_setup")
    assert p["usage"]["provider"] == "mirofish" and p["usage"]["est_low_usd"] is not None and p["finalize"]["state"] == "complete"
    assert {f["variant"] for f in p["forward"]} >= {"base", "no_change", "provider"}


def test_b_mirofish_unavailable(app, mirofish):
    """B: the quantitative research succeeds, the provider is named unavailable, nothing is shown as MiroFish's, a
    retry is offered, and no simulation was started."""
    c, svc, _ = app

    class Off(FakeMiroFish):
        def get(self, url, timeout=None):
            raise requests.ConnectionError("Connection refused")
    fake = mirofish(Off())
    s = _study(c, "MSFT", "accept-b-0001")
    drain(svc)
    p = c.get(f"/v3/studies/{s['id']}/pipeline").json()["pipeline"]
    assert c.get(f"/v3/studies/{s['id']}").json()["study"]["forecast"]
    assert p["status"] == "failed" and "did not answer" in p["error"] and p["can_retry"] and p["version_id"] is None and not p["demo"]
    assert fake.calls == [] and c.post("/v3/scenarios/check", json={}).json()["state"] == "unavailable"


def test_c_a_cost_over_the_automatic_limit_waits_and_the_budget_still_holds(app, mirofish):
    c, svc, _ = app
    fake = mirofish(mirofish_cost_per_run_usd=3.0)                     # Quick estimated at about 0.56 to 2.22: over 0.50
    s = _study(c, "KO", "accept-c-0001")
    drain(svc)
    p = c.get(f"/v3/studies/{s['id']}/pipeline").json()["pipeline"]
    assert p["status"] == "awaiting_approval" and fake.calls == []
    est = p["run"]["estimate"]
    assert est["low"] < est["high"] and "rough" in est["words"] and "MIROFISH_AUTO_APPROVAL_USD" in p["governance"]["reason"]
    assert c.post(f"/v3/studies/{s['id']}/pipeline/approve", json={}).json()["pipeline"]["status"] == "running"
    drain(svc)
    assert c.get(f"/v3/studies/{s['id']}/pipeline").json()["pipeline"]["status"] == "complete"
    # Today's spending (at most) now leaves too little for another run of the same estimate within a 2.50 daily limit.
    mirofish(mirofish_cost_per_run_usd=3.0, mirofish_daily_budget_usd=2.5)
    s2 = _study(c, "JPM", "accept-c-0002")
    drain(svc)
    p2 = c.get(f"/v3/studies/{s2['id']}/pipeline").json()["pipeline"]
    assert p2["status"] == "failed" and "daily" in p2["error"] and "MiroFish was not called" in p2["error"]


class Crash(BaseException):
    pass


def test_d_a_crash_after_the_paid_report_recovers_without_a_new_run(app, mirofish, monkeypatch):
    c, svc, _ = app
    fake = mirofish()
    real = svc.scenarios.create_version
    hit = {"n": 0}

    def once(*a, **k):
        if not hit["n"]:
            hit["n"] = 1
            raise Crash("stopped before the price paths were written")
        return real(*a, **k)
    monkeypatch.setattr(svc.scenarios, "create_version", once)
    s = _study(c, "NVDA", "accept-d-0001")
    with pytest.raises(Crash):
        drain(svc)
    with svc.db.tx() as cur:
        cur.execute("UPDATE jobs SET lease_until = ? WHERE status = 'running'", ("2000-01-01T00:00:00Z",))
    svc.pipeline.recover()
    drain(svc)
    p = svc.pipeline.view(s["id"])
    assert p["status"] == "complete" and fake.calls.count(("POST", "/api/simulation/start")) == 1
    runs = svc.db.query("SELECT id FROM scenario_runs WHERE study_id = ?", (s["id"],))
    assert len(runs) == 1 and svc.db.one("SELECT COUNT(*) AS n FROM scenario_versions WHERE run_id = ?", (runs[0]["id"],))["n"] == 1
    assert svc.db.one("SELECT COUNT(*) AS n FROM scenario_decisions WHERE study_id = ?", (s["id"],))["n"] == 1


def test_e_an_unverified_catalyst_does_not_qualify_anything(app, mirofish):
    c, svc, _ = app
    mirofish()
    s = _study(c, "AMD", "accept-e-0001")
    drain(svc)
    p = svc.pipeline.view(s["id"])
    case = "bullish"
    import time
    time.sleep(0.05)
    when = datetime.now(timezone.utc).isoformat()
    r = c.post(f"/v3/studies/{s['id']}/decision/trigger", json={"case": case, "title": "Wins an important contract", "published_at": when})
    assert r.status_code == 200, r.text
    assert r.json()["triggers"][case]["status"] == "unverified" and r.json()["decision"]["status"] != "qualified"
    assert p["version_id"]
    # The evaluator itself: a reported trigger keeps a Watch; only a verified one qualifies.
    struct = {"key": "long_call", "label": "call", "direction": "LONG", "quantity": 1, "costs": 5.0, "capital": 100.0, "max_loss": 100.0,
              "unlimited_loss": False, "legs": [{"option_symbol": "X", "bid": 1.0, "ask": 1.1, "spread_pct": 2.0}], "flags": [],
              "quality": {"tier": "execution", "reasons": []},
              "by_case": {"central": {"expected_pnl": -5, "worst_tenth": 60}, "bullish": {"expected_pnl": 60, "worst_tenth": 20},
                          "bearish": {"expected_pnl": -90, "worst_tenth": 99}}}
    ver = {"id": "v", "sessions": [{"date": "2026-10-23"}, {"date": "2026-10-29"}], "catalyst": {"kind": "other"}, "base": {"return_pct": [0, 0.2]},
           "cases": [{"key": "bullish", "direction": "up", "quantified": True, "trigger": "a contract", "invalidation": "x"}]}
    th = {"budget": 1000.0, "loss_limit": 200.0, "min_net_pct": 5.0, "max_spread_pct": 10.0}
    args = dict(symbol="AMD", version=ver, valuation={"status": "complete", "structures": [struct], "mixes": [], "freshness": {}},
                thresholds=th, routes={"long_call": "manual"}, today=datetime(2026, 10, 22).date())
    assert evaluate(**args, observed={"bullish": {"title": "contract", "status": "unverified"}})["status"] == "watch"
    assert evaluate(**args, observed={"bullish": {"title": "contract", "status": "verified"}})["status"] == "qualified"


def test_f_an_option_expiring_before_the_horizon_uses_its_own_session():
    svc = FakeSvc([_row("XYZ2026-10-26C110", 110, "call", "2026-10-26")])
    res, _ = V.value(svc, study(call("2026-10-26")), version())
    x = res["structures"][0]
    assert x["eval_session"] == 2 and x["by_case"]["bullish"]["case_price"] == pytest.approx(100.0)
    res, _ = V.value(FakeSvc([_row("XYZ2026-10-22C110", 110, "call", "2026-10-22")]), study(call("2026-10-22")), version())
    assert res["structures"] == [] and res["failed"][0]["code"] == "cannot_value_reliably"


def test_g_and_h_the_scorecard_reports_evidence_and_costs(app):
    """G: pairwise accuracy, direction, coverage, sample sizes, intervals, strategy outcomes by category, costs. H: a
    costlier provider that is not better is reported as such (the full cases are in tests/test_v453.py)."""
    c, svc, _ = app
    sc = c.get("/v3/scenarios/scorecard", params={"horizon": 5}).json()
    for key in ("variants", "pairs", "costs", "profiles", "strategies", "method", "verdict", "missing"):
        assert key in sc, key
    pb = next(p for p in sc["pairs"] if p["a"] == "provider" and p["b"] == "base")
    assert pb["verdict"] == "insufficient" and "insufficient evidence" in sc["verdict"] and "no weight in the base" in sc["verdict"]
    assert set(sc["strategies"]["categories"]) == {"historical_hypothetical", "model_only", "observed"}
    _ = json
