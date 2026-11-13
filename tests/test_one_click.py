"""4.3 the one-click Research pipeline: Run study → evidence and strategies → the scenario provider in the background →
validated price paths saved automatically → the study's fixed candidates revalued → findings.

Through the API on demo data with injected providers: the canned demo provider, a fake MiroFish server (its pinned
routes, polled to completion, with a write that times out and is reconciled instead of sent again), and providers that
misbehave (a result for another packet, one that arrives after Cancel). Also the request key (a retry of the same
click finds the same study), duplicate continuations, an unavailable provider, restart recovery, the run-time cap,
MiroFish's concurrency, fixed anchors and the legs the valuation prices, and the deterministic sizing rules."""

from __future__ import annotations

import json
import re
import time
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from urllib.parse import urlparse

import pytest
import requests
from fastapi.testclient import TestClient

from tradeassist import contracts as C
from tradeassist.api import create_app
from tradeassist.config import Settings, _loopback_url
from tradeassist.db import Database
from tradeassist.jobs import _iso
from tradeassist.providers.demo import DemoData
from tradeassist.service import TradeAssistant
from tradeassist.v3 import scenarios as SC
from tradeassist.v3.pipeline import Pipeline
from tradeassist.v3.scenario_providers import Demo, MiroFish, Step
from tradeassist.v3.workers import workers_for
from tests.test_catalyst_scenarios import FIXTURE, _seed

# --- helpers -----------------------------------------------------------------------------------------------------------


def _wait(c, study_id, timeout=120):
    end = time.time() + timeout
    while time.time() < end:
        s = c.get(f"/v3/studies/{study_id}").json()
        if s["status"] != "running":
            return s
        time.sleep(0.2)
    raise AssertionError("the study did not finish")


def drain(svc, rounds=60):
    """Run the scenario jobs to the end, bringing any that wait (a poll's back-off) forward."""
    rw = workers_for(svc)
    out = []
    for _ in range(rounds):
        out += rw.drain("scenarios")
        n = svc.db.one("SELECT COUNT(*) AS n FROM jobs WHERE status = 'queued' AND kind LIKE 'scenario.%'")["n"]
        if not n:
            break
        with svc.db.tx() as cur:
            cur.execute("UPDATE jobs SET due_at = ? WHERE status = 'queued' AND kind LIKE 'scenario.%'", (_iso(datetime.now(timezone.utc)),))
    return out


def _cases(seed_id=None, drop_seed=False):
    fx = json.loads(FIXTURE.read_text())
    if drop_seed:
        fx.pop("seed_id")
    elif seed_id:
        fx["seed_id"] = seed_id
    return fx


class Resp:
    def __init__(self, code, body):
        self.status_code, self._body = code, body

    def json(self):
        if self._body is None:
            raise ValueError("not json")
        return self._body


class FakeMiroFish:
    """MiroFish's pinned routes, in memory: each long stage answers "processing" once before it finishes."""

    def __init__(self, report=None, timeout_on=(), agents=8):
        self.calls, self.projects, self.sims, self.stopped = [], [], [], []
        self.agents = agents                      # 4.5.2: the graph's filtered entities, one agent each
        self.timeout_on = set(timeout_on)
        self.seen = {}
        self.report = report if report is not None else "The swarm leaned bearish.\n\n```json\n" + json.dumps(_cases(drop_seed=True)) + "\n```"

    def _once(self, key):
        self.seen[key] = self.seen.get(key, 0) + 1
        return self.seen[key] == 1

    def get(self, url, timeout=None):
        return Resp(200, {"status": "ok"})

    def request(self, method, url, timeout=None, **kw):
        path = urlparse(url).path
        self.calls.append((method, path))
        ok = lambda data: Resp(200, {"success": True, "data": data})          # noqa: E731
        if path == "/api/graph/ontology/generate":
            pid = f"proj_{len(self.projects) + 1}"
            self.projects.append({"project_id": pid, "name": kw["data"]["project_name"], "status": "ontology_generated"})
            if ("POST", path) in self.timeout_on:
                self.timeout_on.discard(("POST", path))
                raise requests.Timeout("slow model")
            return ok({"project_id": pid})
        if path == "/api/graph/project/list":
            return ok(self.projects)
        if path == "/api/graph/build":
            return ok({"task_id": "task_1"})
        if path.startswith("/api/graph/task/"):
            return ok({"status": "processing", "progress": 40} if self._once("task") else {"status": "completed", "result": {"graph_id": "g_1"}})
        if path.startswith("/api/graph/project/"):
            return ok({"graph_id": "g_1"})
        if path == "/api/simulation/create":
            sid = f"sim_{len(self.sims) + 1}"
            self.sims.append({"simulation_id": sid, "project_id": kw["json"]["project_id"]})
            if ("POST", path) in self.timeout_on:
                self.timeout_on.discard(("POST", path))
                raise requests.Timeout("no answer")
            return ok({"simulation_id": sid})
        if path.startswith("/api/simulation/entities/"):
            return ok({"entities": [], "entity_types": ["Investor"], "total_count": self.agents + 3, "filtered_count": self.agents})
        if path == "/api/simulation/list":
            return ok([s for s in self.sims if s["project_id"] == kw["params"]["project_id"]])
        if path == "/api/simulation/prepare":
            return ok({"task_id": "ptask"})
        if path == "/api/simulation/prepare/status":
            return ok({"status": "processing", "progress": 50} if self._once("prepare") else {"status": "completed"})
        if path == "/api/simulation/start":
            assert kw["json"]["max_rounds"] >= 1
            return ok({})
        if path.endswith("/run-status"):
            return ok({"runner_status": "running", "current_round": 3, "total_rounds": 10} if self._once("run")
                      else {"runner_status": "completed", "current_round": 10})
        if path == "/api/report/generate":
            return ok({"task_id": "rtask"})
        if path == "/api/report/generate/status":
            return ok({"status": "processing"} if self._once("report") else {"status": "completed"})
        if path.startswith("/api/report/by-simulation/"):
            return ok({"report_id": "rep_1", "markdown_content": self.report})
        if path == "/api/simulation/stop":
            self.stopped.append(kw["json"]["simulation_id"])
            return ok({})
        return Resp(404, {"success": False, "error": f"no route {path}"})


def fake_mirofish(fake):
    return MiroFish("http://127.0.0.1:5001", session=fake, poll_first=0.0, poll_most=0.0)


@pytest.fixture(scope="module")
def app(tmp_path_factory):
    import tradeassist.api as api_mod
    api_mod.TEST_HOSTS = ("testserver",)
    path = tmp_path_factory.mktemp("oneclick") / "t.db"
    # 4.5.2: a MiroFish run of unknown cost waits for approval; these tests give it a small known estimate so it starts on its own.
    s = Settings(data_mode="demo", db_path=path, desk_worker="off", scenario_provider="demo", scenario_auto=True, mirofish_cost_per_run_usd=0.05)
    svc = TradeAssistant(s, provider=DemoData(), db=Database(path))
    c = TestClient(create_app(s, svc))
    studies = {}
    # One with scenarios (the Research setting is on), the rest made without, for the tests to start their own.
    first = c.post("/v3/studies", json={"symbol": "AAPL", "horizon_days": 5, "request_key": "click-aapl-0001"})
    assert first.status_code == 200, first.text
    studies["auto"] = _wait(c, first.json()["id"])
    for key, sym, h in (("a", "MSFT", 5), ("b", "KO", 5), ("c", "JPM", 5), ("d", "NVDA", 1), ("e", "AMD", 5), ("f", "XOM", 5),
                        ("three", "AAPL", 3)):
        studies[key] = _wait(c, c.post("/v3/studies", json={"symbol": sym, "horizon_days": h, "scenarios": False}).json()["id"])
    yield c, svc, studies
    svc.db.close()


@pytest.fixture
def provider(app):
    """Set the provider the pipeline uses for one test."""
    _, svc, _ = app

    def use(p):
        svc.scenario_provider_override = p
    yield use
    svc.scenario_provider_override = None


# --- the one click -----------------------------------------------------------------------------------------------------

def test_one_click_gives_paths_valuation_and_findings_with_no_manual_step(app):
    c, svc, st = app
    s = st["auto"]
    again = c.post("/v3/studies", json={"symbol": "AAPL", "horizon_days": 5, "request_key": "click-aapl-0001"}).json()
    assert again["id"] == s["id"] and again["reused"] is True                  # a browser retry of the same click
    p = s["pipeline"]
    assert p["status"] == "running" and [x["key"] for x in p["stages"]] == ["evidence", "provider", "paths", "findings"]
    assert p["stages"][0]["state"] == "done" and p["stages"][1]["state"] == "queued"
    # The quantitative study is complete and readable while the provider has not run yet.
    assert s["status"] in ("complete", "partial") and s["study"]["forecast"] and s["view"]["strategies"]["candidates"]
    ran = drain(svc)
    assert [r["kind"] for r in ran] == ["scenario.start", "scenario.step", "scenario.finish"]
    s = c.get(f"/v3/studies/{s['id']}").json()
    p = s["pipeline"]
    assert p["status"] == "complete" and all(x["state"] == "done" for x in p["stages"]), p
    assert p["demo"] is True and p["catalyst"]["title"] and p["catalyst_basis"]
    # One version saved automatically, its paths in the study's own coordinates.
    v = c.get(f"/v3/scenario-versions/{p['version_id']}").json()
    assert v["origin"] == "auto" and v["version"] == 1
    assert v["anchor"] == s["frame"]["anchor"] and [x["date"] for x in v["sessions"]] == [x["date"] for x in s["frame"]["sessions"]]
    assert v["base"]["return_pct"] == s["study"]["forecast"]["base"]["path_pct"]
    for case in v["cases"]:
        assert case["price"][-1] == pytest.approx(v["anchor"]["price"] * (1 + case["return_pct"][-1] / 100))
    bull = next(x for x in v["cases"] if x["key"] == "bullish")
    bear = next(x for x in v["cases"] if x["key"] == "bearish")
    assert bull["quantified"] and bear["quantified"] and bull["return_pct"][-1] > v["base"]["return_pct"][-1] > bear["return_pct"][-1]
    sources = {a["issuer"]["source"] for a in v["assumptions"] if a["issuer"]}
    assert sources <= {"options_implied_move", "analog_tail", "implied_sensitivity"}       # never the provider's own number
    # The run keeps the report and the parsed cases apart.
    run = svc.db.one("SELECT * FROM scenario_runs WHERE id = ?", (p["run"]["id"],))
    assert run["status"] == "complete" and run["report_text"].startswith("Demo provider") and json.loads(run["cases_json"])
    # The study's own candidates, priced under every case from one snapshot, with cash among the mixes.
    val = p["valuation"]
    assert val["status"] in ("complete", "partial") and val["structures"] and val["version_id"] == v["id"]
    cands = {x["key"]: x for x in s["view"]["strategies"]["candidates"]}
    for x in val["structures"]:
        assert x["key"] in cands and set(x["by_case"]) == {"central", "bullish", "bearish"}
        assert [lg["option_symbol"] for lg in x["legs"]] == [lg["option_symbol"] for lg in cands[x["key"]]["legs"]]
        for k, row in x["by_case"].items():
            case = next(cc for cc in v["cases"] if cc["key"] == k)
            assert row["case_price"] == pytest.approx(case["price"][-1])
    assert any(m["key"] == "cash" for m in val["mixes"]) and val["best_mix"] == val["mixes"][0]["key"]
    assert "No case has a probability" in val["note"] or "no probability" in val["note"].lower()
    assert p["findings"]["conclusion"].startswith("AAPL") and "None has a probability" in p["findings"]["conclusion"]
    # Nothing ran twice: the continuation is keyed by study, provider and configuration.
    svc.pipeline.after_study(s["id"])
    assert drain(svc) == [] or all(r["result"].get("skipped") for r in drain(svc))
    assert svc.db.one("SELECT COUNT(*) AS n FROM scenario_runs WHERE study_id = ?", (s["id"],))["n"] == 1


def test_a_new_quote_does_not_move_the_saved_paths_and_value_again_uses_the_same_legs(app, monkeypatch):
    c, svc, st = app
    drain(svc)
    s = c.get(f"/v3/studies/{st['auto']['id']}").json()
    p = s["pipeline"]
    v = c.get(f"/v3/scenario-versions/{p['version_id']}").json()
    before = p["valuation"]
    real = svc.market.quote

    def moved(sym):
        q = dict(real(sym))
        for k in ("last", "bid", "ask", "close"):
            if q.get(k):
                q[k] = float(q[k]) * 1.1
        return q
    monkeypatch.setattr(svc.market, "quote", moved)
    r = c.post(f"/v3/studies/{s['id']}/pipeline/revalue", json={})
    assert r.status_code == 200, r.text
    after = r.json()["pipeline"]["valuation"]
    assert after["id"] != before["id"]
    v2 = c.get(f"/v3/scenario-versions/{p['version_id']}").json()
    assert [cc["price"] for cc in v2["cases"]] == [cc["price"] for cc in v["cases"]]           # the chart's forecast did not move
    shares_b = next(x for x in before["structures"] if x["key"] == "shares")
    shares_a = next(x for x in after["structures"] if x["key"] == "shares")
    assert shares_a["entry"] == pytest.approx(shares_b["entry"] * 1.1, rel=1e-6)             # what it costs now did
    assert shares_a["by_case"]["bullish"]["case_price"] == shares_b["by_case"]["bullish"]["case_price"]
    assert shares_a["by_case"]["bullish"]["expected_pnl"] < shares_b["by_case"]["bullish"]["expected_pnl"]


def test_the_packet_carries_strategy_context_but_nothing_about_contracts_or_the_account(app):
    c, svc, st = app
    drain(svc)
    p = c.get(f"/v3/studies/{st['auto']['id']}").json()["pipeline"]
    md = c.get(f"/v3/scenario-seeds/{p['seed_id']}?format=markdown").text
    assert "## What the strategies being compared depend on" in md and "implied volatility" in md
    low = md.lower()
    for word in ("budget", "account", "token", "position size", "broker", "quantity"):
        assert word not in low, word
    cands = c.get(f"/v3/studies/{st['auto']['id']}").json()["view"]["strategies"]["candidates"]
    for cand in cands:
        for lg in cand["legs"]:
            assert lg["option_symbol"] not in md


def test_other_horizons_and_scenarios_off_keep_the_study_flow_as_it_was(app):
    c, svc, st = app
    assert "pipeline" not in st["three"] and "pipeline" not in st["a"]
    r = c.post(f"/v3/studies/{st['three']['id']}/pipeline/start", json={})
    assert r.status_code == 400 and "1 or 5-session" in r.json()["error"]


# --- providers that fail or misbehave ------------------------------------------------------------------------------------

def test_an_unavailable_provider_leaves_the_study_and_offers_retry(app, provider):
    c, svc, st = app
    drain(svc)
    provider(lambda name: MiroFish(""))
    r = c.post(f"/v3/studies/{st['a']['id']}/pipeline/start", json={})
    assert r.status_code == 200, r.text
    drain(svc)
    s = c.get(f"/v3/studies/{st['a']['id']}").json()
    p = s["pipeline"]
    assert s["status"] in ("complete", "partial") and s["study"]["strategies"]                  # the quantitative study stands
    assert p["status"] == "failed" and "MIROFISH_URL is not set" in p["error"] and p["can_retry"] and not p["can_cancel"]
    assert next(x for x in p["stages"] if x["key"] == "provider")["state"] == "failed"
    # Retry with a working provider: a new attempt, a new run.
    provider(lambda name: Demo())
    p = c.post(f"/v3/studies/{st['a']['id']}/pipeline/retry", json={}).json()["pipeline"]
    assert p["attempt"] == 2 and p["status"] == "running"
    drain(svc)
    p = c.get(f"/v3/studies/{st['a']['id']}/pipeline").json()["pipeline"]
    assert p["status"] == "complete" and p["version_id"]
    assert svc.db.one("SELECT COUNT(*) AS n FROM scenario_runs WHERE study_id = ?", (st["a"]["id"],))["n"] == 2


class Scripted:
    """A provider that answers with whatever the test gives it."""
    name, label = "mirofish", "Scripted"

    def __init__(self, fn):
        self.fn, self.stops = fn, []

    def config(self):
        return {"provider": "scripted"}

    def ready(self):
        return {"ok": True, "detail": "ok"}

    def advance(self, ctx, state):
        return self.fn(ctx, state)

    def stop(self, state):
        self.stops.append(state)
        return "stopped"


def test_a_result_for_another_packet_is_refused_and_nothing_is_drawn(app, provider):
    c, svc, st = app
    drain(svc)
    provider(Scripted(lambda ctx, state: Step({"stage": "done"}, report="x", cases=_cases("seed_someone_else"), stage="report")))
    c.post(f"/v3/studies/{st['b']['id']}/pipeline/start", json={})
    drain(svc)
    p = c.get(f"/v3/studies/{st['b']['id']}/pipeline").json()["pipeline"]
    assert p["status"] == "needs_attention" and "not this study's packet" in p["error"] and p["version_id"] is None
    run = svc.db.one("SELECT status, cases_json FROM scenario_runs WHERE id = ?", (p["run"]["id"],))
    assert run["status"] == "needs_attention" and run["cases_json"] is None
    assert svc.db.one("SELECT COUNT(*) AS n FROM scenario_versions WHERE run_id = ?", (p["run"]["id"],))["n"] == 0


def test_a_result_that_arrives_after_cancel_is_not_applied(app, provider):
    c, svc, st = app

    def late(ctx, state):
        with svc.db.tx() as cur:                      # the person presses Cancel while the provider is out
            cur.execute("UPDATE scenario_runs SET status = 'canceled' WHERE id = ?", (ctx["run_id"],))
        return Step({"stage": "done", "simulation_id": "sim_9", "started": True}, report="late", cases=_cases(drop_seed=True), stage="report")
    prov = Scripted(late)
    drain(svc)
    provider(prov)
    c.post(f"/v3/studies/{st['c']['id']}/pipeline/start", json={})
    ran = drain(svc)
    assert any("was not applied" in str(r.get("result")) for r in ran)
    run_id = svc.db.one("SELECT id FROM scenario_runs WHERE study_id = ? ORDER BY created_at DESC LIMIT 1", (st["c"]["id"],))["id"]
    assert svc.db.one("SELECT status, cases_json, report_text FROM scenario_runs WHERE id = ?", (run_id,))["cases_json"] is None
    assert svc.db.one("SELECT COUNT(*) AS n FROM scenario_versions WHERE run_id = ?", (run_id,))["n"] == 0
    assert prov.stops and prov.stops[0]["simulation_id"] == "sim_9"                            # the server was asked to stop


def test_cancel_stops_a_running_simulation(app, provider):
    c, svc, st = app
    drain(svc)
    fake = FakeMiroFish()
    provider(lambda name: fake_mirofish(fake))
    c.post(f"/v3/studies/{st['d']['id']}/pipeline/start", json={})
    w = workers_for(svc).worker("scenarios")
    for _ in range(12):                                # up to the simulation's first poll
        w.run_once()
        with svc.db.tx() as cur:
            cur.execute("UPDATE jobs SET due_at = ? WHERE status = 'queued' AND kind LIKE 'scenario.%'", (_iso(datetime.now(timezone.utc)),))
        if ("GET", "/api/simulation/sim_1/run-status") in fake.calls:
            break
    p = c.get(f"/v3/studies/{st['d']['id']}/pipeline").json()["pipeline"]
    assert p["status"] == "running" and p["can_cancel"] and p["run"]["remote"]["simulation_id"] == "sim_1"
    p = c.post(f"/v3/studies/{st['d']['id']}/pipeline/cancel", json={}).json()["pipeline"]
    assert p["status"] == "canceled" and fake.stopped == ["sim_1"] and p["can_retry"]
    drain(svc)                                          # the queued poll finds the run canceled and does nothing
    assert ("POST", "/api/report/generate") not in fake.calls


# --- MiroFish through its pinned routes ----------------------------------------------------------------------------------

def test_mirofish_runs_through_its_stages_saving_each_id_and_reconciles_a_timed_out_write(app, provider):
    c, svc, st = app
    drain(svc)
    fake = FakeMiroFish(timeout_on={("POST", "/api/simulation/create")})
    provider(lambda name: fake_mirofish(fake))
    c.post(f"/v3/studies/{st['e']['id']}/pipeline/start", json={})
    drain(svc)
    p = c.get(f"/v3/studies/{st['e']['id']}/pipeline").json()["pipeline"]
    assert p["status"] == "complete", p
    # The simulation create timed out but had run: it was found by its project, not created again.
    assert len(fake.sims) == 1 and fake.calls.count(("POST", "/api/simulation/create")) == 1
    assert ("GET", "/api/simulation/list") in fake.calls
    assert fake.calls.count(("POST", "/api/graph/ontology/generate")) == 1
    order = [path for _, path in fake.calls]
    for a, b in (("/api/graph/ontology/generate", "/api/graph/build"), ("/api/graph/build", "/api/simulation/create"),
                 ("/api/simulation/prepare", "/api/simulation/start"), ("/api/simulation/start", "/api/report/generate"),
                 ("/api/report/generate", "/api/report/by-simulation/sim_1")):
        assert order.index(a) < order.index(b), (a, b)
    assert p["run"]["remote"] == {"project_id": "proj_1", "graph_id": "g_1", "simulation_id": "sim_1", "report_id": "rep_1"}
    run = svc.db.one("SELECT report_text, report_excerpt FROM scenario_runs WHERE id = ?", (p["run"]["id"],))
    assert run["report_text"].startswith("The swarm leaned bearish") and run["report_excerpt"]
    v = c.get(f"/v3/scenario-versions/{p['version_id']}").json()
    assert v["origin"] == "auto" and {x["key"] for x in v["cases"]} == {"central", "bullish", "bearish"}
    # The bullish driver's proposed +4.0 pp is the provider's; the size used is a rule's.
    bull = next(a for a in v["assumptions"] if a["key"] == "bullish")
    assert bull["issuer"] is None or (bull["issuer"]["source"] != "provider" and bull["issuer"]["value_pct"] != 4.0)


def test_a_restart_resumes_from_the_recorded_ids_without_a_second_paid_run(app, provider):
    c, svc, st = app
    drain(svc)
    fake = FakeMiroFish()
    provider(lambda name: fake_mirofish(fake))
    c.post(f"/v3/studies/{st['f']['id']}/pipeline/start", json={})
    w = workers_for(svc).worker("scenarios")
    w.run_once()                                        # start: the packet and the run
    w.run_once()                                        # the ontology: a project on the server
    assert fake.projects and fake.calls.count(("POST", "/api/graph/ontology/generate")) == 1
    with svc.db.tx() as cur:                            # the process stopped before the next step was queued
        cur.execute("DELETE FROM jobs WHERE status = 'queued' AND kind = 'scenario.step'")
    out = svc.pipeline.recover()
    assert out["queued"]
    drain(svc)
    p = c.get(f"/v3/studies/{st['f']['id']}/pipeline").json()["pipeline"]
    assert p["status"] == "complete" and fake.calls.count(("POST", "/api/graph/ontology/generate")) == 1


def test_a_finished_study_whose_continuation_was_never_queued_is_recovered(app, provider):
    c, svc, st = app
    drain(svc)
    provider(lambda name: Demo())
    sid = st["a"]["id"]
    if svc.pipeline.load(sid) is None:
        c.post(f"/v3/studies/{sid}/pipeline/start", json={})
        drain(svc)
    p = svc.pipeline.load(sid)
    p.update({"status": "running", "stage": "provider", "run_id": None, "attempt": 7, "version_id": None})
    with svc.db.tx() as cur:
        cur.execute("UPDATE studies SET pipeline_json = ? WHERE id = ?", (json.dumps(p), sid))
    assert svc.pipeline.recover()["queued"] == [sid]
    drain(svc)
    assert svc.pipeline.view(sid)["status"] == "complete"


def test_a_run_that_takes_too_long_is_stopped(app, provider):
    c, svc, st = app
    drain(svc)
    fake = FakeMiroFish()
    provider(lambda name: fake_mirofish(fake))
    sid = st["b"]["id"]
    assert c.post(f"/v3/studies/{sid}/pipeline/start", json={}).status_code == 200
    w = workers_for(svc).worker("scenarios")
    for _ in range(16):                     # 4.5.2 added a stage (the agent count, read before any agent is prepared)
        w.run_once()
        with svc.db.tx() as cur:
            cur.execute("UPDATE jobs SET due_at = ? WHERE status = 'queued' AND kind LIKE 'scenario.%'", (_iso(datetime.now(timezone.utc)),))
        if ("POST", "/api/simulation/start") in fake.calls:
            break
    run_id = svc.pipeline.load(sid)["run_id"]
    r = svc.db.one("SELECT external_json FROM scenario_runs WHERE id = ?", (run_id,))
    ext = json.loads(r["external_json"])
    ext["started_at"] = _iso(datetime.now(timezone.utc) - timedelta(hours=3))
    with svc.db.tx() as cur:
        cur.execute("UPDATE scenario_runs SET external_json = ? WHERE id = ?", (json.dumps(ext), run_id))
    drain(svc)
    p = svc.pipeline.view(sid)
    assert p["status"] == "failed" and "MIROFISH_RUN_MINUTES" in p["error"] and fake.stopped == ["sim_1"]


def test_runs_wait_for_a_free_mirofish_slot(app, provider):
    c, svc, st = app
    calls = []

    def slow(ctx, state):
        calls.append(ctx["run_id"])
        return Step({**state, "stage": "x"}, next_in=60.0, stage="simulation", detail="running")
    drain(svc)                                          # nothing left over from another test
    provider(Scripted(slow))
    one, two = st["c"]["id"], st["d"]["id"]
    for sid in (one, two):
        r = c.post(f"/v3/studies/{sid}/pipeline/start", json={})
        assert r.status_code == 200, r.text
    w = workers_for(svc).worker("scenarios")
    results = w.drain()
    assert any(r["status"] == "deferred" for r in results), results
    running = svc.db.query("SELECT study_id FROM scenario_runs WHERE provider = 'mirofish' AND status = 'running'")
    assert len(running) == 1 and len(set(calls)) == 1
    for sid in (one, two):
        c.post(f"/v3/studies/{sid}/pipeline/cancel", json={})


# --- settings and the sizing rules ---------------------------------------------------------------------------------------

def test_research_settings_choose_the_provider_and_the_automatic_switch(app):
    c, svc, st = app
    r = c.get("/v3/scenarios/settings").json()
    assert r["provider"] == "demo" and r["auto"] is True and r["horizons"] == [1, 5] and r["readiness"]["ok"]
    assert r["mirofish"]["upstream"].startswith("7657031")
    r = c.post("/v3/scenarios/settings", json={"auto": False}).json()
    assert r["auto"] is False and r["source"]["auto"] == "Research settings"
    assert c.post("/v3/scenarios/settings", json={"provider": "bogus"}).status_code == 422
    assert c.post("/v3/scenarios/settings", json={"url": "http://example.com"}).status_code == 422
    off = c.post("/v3/studies", json={"symbol": "KO", "horizon_days": 5}).json()
    assert off["scenarios"] is False
    c.post("/v3/scenarios/settings", json={"auto": True})
    _wait(c, off["id"])


def test_the_mirofish_address_must_be_this_computer():
    assert _loopback_url("http://127.0.0.1:5001/") == "http://127.0.0.1:5001"
    assert _loopback_url("http://localhost:5001") == "http://localhost:5001"
    assert _loopback_url("") == ""
    for bad in ("http://10.0.0.5:5001", "https://mirofish.example.com", "ftp://127.0.0.1"):
        with pytest.raises(ValueError, match="loopback"):
            _loopback_url(bad)


def _seed_with(**over):
    s = _seed(h=5)
    s["sources"] = [{"id": "S1", "kind": "options", "title": "What options price"}, {"id": "S2", "kind": "analog", "title": "Past situations"},
                    {"id": "S3", "kind": "news", "title": "News"}]
    s.update(over)
    return s


def _imp(*cases):
    return [{"key": k, "label": C.CASE_LABELS[k], "stock_direction": d, "trigger": "t", "invalidation": "i", "drivers": drv} for k, d, drv in cases]


def test_each_case_is_sized_only_by_a_deterministic_rule():
    pl = Pipeline(None)
    issuer = lambda d, ids=("S3",): {"type": "issuer_event", "direction": d, "timing": "first_session_after_release", "reason": "r", "source_ids": list(ids)}  # noqa: E731
    cases = _imp(("central", "flat", [issuer("flat")]), ("bullish", "up", [issuer("up")]), ("bearish", "down", [issuer("down", ())]))
    # An earnings release with a priced move: the options-implied move, on the catalyst's session.
    seed = _seed_with(catalyst={"kind": "earnings", "title": "Results"}, catalyst_session=2, volatility={"event_move_pct": 4.2, "implied_sigma_pct": 3.0},
                      analogs={"independent": 40, "median_pct": 0.2, "p10_pct": -5.0, "p90_pct": 6.0})
    out, notes = pl.auto_assumptions(seed, cases)
    by = {a["key"]: a for a in out}
    assert by["central"]["issuer"] is None
    assert by["bullish"]["issuer"] == {**by["bullish"]["issuer"], "value_pct": 4.2, "session": 2, "source": "options_implied_move", "source_ids": ["S1"]}
    assert by["bearish"]["issuer"] is None and any("cites no evidence" in n for n in notes)     # an uncited driver stays a hypothesis
    # No priced event: the analogs' tail when there are enough independent cases, else the implied standard deviation.
    seed = _seed_with(catalyst={"kind": "other", "title": "Setup"}, catalyst_session=1, volatility={"implied_sigma_pct": 3.0},
                      analogs={"independent": 40, "median_pct": 0.2, "p10_pct": -5.0, "p90_pct": 6.0})
    by = {a["key"]: a for a in pl.auto_assumptions(seed, cases)[0]}
    assert by["bullish"]["issuer"]["source"] == "analog_tail" and by["bullish"]["issuer"]["value_pct"] == pytest.approx(5.8)
    assert by["bullish"]["issuer"]["source_ids"] == ["S2"]
    seed["analogs"]["independent"] = 12
    by = {a["key"]: a for a in pl.auto_assumptions(seed, cases)[0]}
    assert by["bullish"]["issuer"]["source"] == "implied_sensitivity" and by["bullish"]["issuer"]["value_pct"] == pytest.approx(3.0)
    seed["volatility"] = {}
    out, notes = pl.auto_assumptions(seed, cases)
    assert {a["key"]: a for a in out}["bullish"]["issuer"] is None and any("no rule could size" in n for n in notes)


def test_a_factor_shock_takes_the_sign_that_moves_the_stock_the_drivers_way():
    pl = Pipeline(None)
    seed = _seed_with(catalyst={"kind": "other", "title": "Setup"}, catalyst_session=1)
    for r in seed["factors"]["rows"]:
        r["sd_daily_pct"] = 0.5
    market = lambda d: {"type": "market", "direction": d, "timing": "first_session_after_release", "factor": "us_market", "reason": "tape", "source_ids": ["S3"]}  # noqa: E731
    yields = lambda d: {"type": "macro", "direction": d, "timing": "by_horizon", "factor": "yield_10y", "reason": "rates", "source_ids": ["S3"]}  # noqa: E731
    out, _ = pl.auto_assumptions(seed, _imp(("bullish", "up", [market("up"), yields("up")]), ("bearish", "down", [market("down"), yields("down")])))
    for a in out:
        res = SC.case_paths(seed, a)
        want = 1 if a["key"] == "bullish" else -1
        for f in a["factors"]:
            single = SC.case_paths(seed, {"key": "x", "issuer": None, "dispersion": None, "factors": [f]})
            assert single["attribution"]["factor_pct"][-1] * want > 0, (a["key"], f)
            assert f["source"] == "factor_volatility" and abs(f["value"]) == pytest.approx(abs(SC.rule_value(seed, "factor_volatility", "up", f["key"])))
        assert res["attribution"]["factor_pct"][-1] * want > 0


def test_a_size_credited_to_a_rule_must_be_the_rules_own(app):
    c, svc, st = app
    drain(svc)
    p = c.get(f"/v3/studies/{st['auto']['id']}/pipeline").json()["pipeline"]
    run_id = p["run"]["id"]
    seed = svc.scenarios._seed(p["seed_id"])
    want = SC.rule_value(seed, "implied_sensitivity", "up")
    assert want is not None
    bad = c.post(f"/v3/scenario-runs/{run_id}/versions", json={"cases": [{"key": "bullish", "issuer": {"value_pct": want + 2, "session": 1,
                                                                                                   "source": "implied_sensitivity", "source_ids": ["S1"]}}]})
    assert bad.status_code == 400 and "rule gives" in bad.json()["error"]
    ok = c.post(f"/v3/scenario-runs/{run_id}/versions", json={"parent_version": 1, "cases": [{"key": "bullish", "issuer": {"value_pct": want, "session": 1,
                                                                                                                           "source": "implied_sensitivity", "source_ids": ["S1"]}}]})
    assert ok.status_code == 200, ok.text
    assert ok.json()["origin"] == "manual" and ok.json()["version"] == 2                       # an edit is another version


def test_the_valuation_refuses_a_target_that_has_passed(app):
    c, svc, st = app
    drain(svc)
    from tradeassist.v3 import valuation as V
    s = svc.research3.get(st["auto"]["id"])
    v = svc.scenarios.version(svc.pipeline.load(st["auto"]["id"])["version_id"])
    old = {**v, "sessions": [{**x, "date": "2020-01-0" + str(i + 2)} for i, x in enumerate(v["sessions"])]}
    res, _ = V.value(svc, s, old)
    assert res["status"] == "failed" and "has passed" in res["failed"][0]["error"]


def test_the_request_key_is_checked(app):
    c, svc, st = app
    assert c.post("/v3/studies", json={"symbol": "KO", "horizon_days": 5, "request_key": "bad key with spaces"}).status_code == 422
    assert c.post("/v3/studies", json={"symbol": "KO", "horizon_days": 5, "request_key": "short"}).status_code == 422
    assert re.match(r"^[A-Za-z0-9_.:-]+$", "click-aapl-0001")
    _ = replace
