"""4.5.2: MiroFish reliability, self-hosting and cost governance.

Profiles and what the pinned MiroFish can be told; the estimate (unknown, scaled, or from recorded runs) and its
range; admission (automatic, your approval, or refused over a budget), the approval of simulation spending only and
its idempotence; the agent count checked before any agent is prepared; the circuit breaker (opens after repeated
provider failures, a cancel does not count, one free probe after the cooldown, a manual reset) with the quantitative
study kept and the demo never substituted; the usage ledger (never $0 for an unknown cost, failed runs by the
stages they reached, measured AI tokens, entered actual costs); the checkpoint before a costly write; layered
readiness; and the read-only check and the paid functional test from the command line."""

from __future__ import annotations

import json
from dataclasses import replace
from datetime import datetime, timedelta, timezone

import pytest
import requests
from fastapi.testclient import TestClient

from tradeassist.api import create_app
from tradeassist.config import Settings
from tradeassist.db import Database
from tradeassist.providers.demo import DemoData
from tradeassist.service import TradeAssistant
from tradeassist.v3 import simulation as G
from tradeassist.v3.scenario_providers import MiroFish
from tests.test_one_click import FakeMiroFish, Resp, _wait, drain, fake_mirofish


@pytest.fixture(scope="module")
def app(tmp_path_factory):
    path = tmp_path_factory.mktemp("v452") / "t.db"
    s = Settings(data_mode="demo", db_path=path, desk_worker="off", scenario_provider="mirofish", scenario_auto=False,
                 mirofish_url="http://127.0.0.1:5001")
    svc = TradeAssistant(s, provider=DemoData(), db=Database(path))
    c = TestClient(create_app(s, svc))
    ids = [_wait(c, c.post("/v3/studies", json={"symbol": sym, "horizon_days": 5, "scenarios": False}).json()["id"])["id"]
           for sym in ("AAPL", "MSFT", "KO", "JPM", "NVDA", "AMD", "XOM", "PEP", "WMT", "CVX", "MRK", "ORCL", "IBM", "T")]
    yield c, svc, ids, s
    svc.db.close()


@pytest.fixture
def use(app):
    """Settings and a fake MiroFish for one test; the ledger and circuit start empty."""
    c, svc, ids, s = app
    with svc.db.tx() as cur:
        cur.execute("DELETE FROM scenario_usage")
        cur.execute("DELETE FROM kv_cache WHERE key IN (?, ?)", (G.CIRCUIT_KEY, G.SMOKE_KEY))
    svc.db.cache_put("research:scenario_settings", {})
    made = {}

    def go(fake=None, **settings):
        svc.settings = replace(s, **settings)
        made["fake"] = fake or FakeMiroFish()
        svc.scenario_provider_override = lambda name: _mf(svc, made["fake"])
        return made["fake"]
    yield go
    svc.settings = s
    svc.scenario_provider_override = None


def _mf(svc, fake):
    p = G.profile(svc.settings, (svc.db.cache_get("research:scenario_settings") or {}).get("profile") or svc.settings.scenario_default_profile)
    return MiroFish("http://127.0.0.1:5001", session=fake, poll_first=0.0, poll_most=0.0, max_rounds=p["rounds"], max_agents=p["agents"],
                    platform=p["platform"], parallel_profiles=p["parallel_profiles"], profile=p["key"])


def _paid(fake):
    return [c for c in fake.calls if c[0] == "POST"]


# --- profiles, the estimate and admission ----------------------------------------------------------------------------

def test_profiles_follow_the_settings_and_deep_always_asks():
    s = Settings(data_mode="demo", mirofish_max_rounds=10, mirofish_max_agents=40, mirofish_run_minutes=45)
    q, st, d = (G.profile(s, k) for k in G.PROFILE_KEYS)
    assert (q["rounds"], q["agents"], q["platform"], q["minutes"]) == (4, 20, "reddit", 20.0)
    assert (st["rounds"], st["agents"], st["platform"], st["minutes"]) == (10, 40, "parallel", 45.0)
    assert (d["rounds"], d["agents"], d["minutes"]) == (30, 120, 90.0) and d["always_ask"] and not q["always_ask"]
    assert q["load"] < st["load"] < d["load"]
    assert G.profile(s, "nonsense")["key"] == "quick"


def test_the_estimate_is_unknown_scaled_or_from_recorded_runs(app, use):
    c, svc, ids, _ = app
    use()
    e = G.estimate(svc, "quick")
    assert e["known"] is False and e["low"] is None and "unknown" in e["words"]           # never $0
    use(mirofish_cost_per_run_usd=1.0)
    q, st = G.estimate(svc, "quick"), G.estimate(svc, "standard")
    assert st["mid"] == pytest.approx(1.0) and st["low"] == pytest.approx(0.5) and st["high"] == pytest.approx(2.0)
    assert q["mid"] < st["mid"] and "rough" in q["words"]
    # Two finished Quick runs whose actual cost you entered: the estimate comes from them.
    for i, usd in enumerate((0.20, 0.30)):
        with svc.db.tx() as cur:
            cur.execute("""INSERT INTO scenario_usage (run_id, study_id, provider, profile, created_at, status, day, month, measured_usd,
                           cost_confidence, updated_at) VALUES (?, 's', 'mirofish', 'quick', ?, 'complete', '2026-10-01', '2026-10', ?, 'user_entered', ?)""",
                        (f"srun_h{i}", "2026-10-01T12:00:00Z", usd, "2026-10-01T12:00:00Z"))
    h = G.estimate(svc, "quick")
    assert h["basis"].startswith("2 recorded runs") and h["low"] == pytest.approx(0.16) and h["high"] == pytest.approx(0.375)


def test_admission_is_automatic_within_the_limits_otherwise_it_asks_or_refuses(app, use):
    c, svc, ids, _ = app
    use()
    a = G.admit(svc, "quick")
    assert a["decision"] == "approval" and "unknown" in a["reason"]
    use(mirofish_require_approval_on_unknown_cost=False)
    assert G.admit(svc, "quick")["decision"] == "auto"
    use(mirofish_cost_per_run_usd=0.5)
    assert G.admit(svc, "quick")["decision"] == "auto"                        # up to ~0.37
    a = G.admit(svc, "standard")
    assert a["decision"] == "approval" and "MIROFISH_AUTO_APPROVAL_USD" in a["reason"]      # up to 1.00 > 0.50
    use(mirofish_cost_per_run_usd=0.01)
    assert "always asks first" in G.admit(svc, "deep")["reason"]
    use(mirofish_cost_per_run_usd=0.5, mirofish_daily_budget_usd=0.2)
    a = G.admit(svc, "quick")
    assert a["decision"] == "refused" and a["code"] == "budget_exceeded" and "daily" in a["reason"]
    use(mirofish_cost_per_run_usd=0.5, mirofish_monthly_budget_usd=0.3)
    assert G.admit(svc, "quick")["decision"] == "refused" and "monthly" in G.admit(svc, "quick")["reason"]


# --- the pipeline under governance --------------------------------------------------------------------------------------

def test_an_unknown_cost_waits_for_approval_without_calling_mirofish_then_runs_once(app, use):
    """Scenario C: no paid request begins until you approve; approving twice (a browser retry) starts it once; the
    approval is for simulation spending only and is recorded with the run."""
    c, svc, ids, _ = app
    fake = use()
    sid = ids[0]
    assert c.post(f"/v3/studies/{sid}/pipeline/start", json={}).status_code == 200
    drain(svc)
    p = c.get(f"/v3/studies/{sid}/pipeline").json()["pipeline"]
    assert p["status"] == "awaiting_approval" and p["can_approve"] and p["can_cancel"] and _paid(fake) == []
    assert "unknown" in p["governance"]["reason"] and p["stages"][1]["state"] == "approval"
    assert s_tudy_ok(c, sid)                                                    # the quantitative study stands meanwhile
    a = c.post(f"/v3/studies/{sid}/pipeline/approve", json={})
    assert a.status_code == 200 and a.json()["pipeline"]["status"] == "running"
    assert c.post(f"/v3/studies/{sid}/pipeline/approve", json={}).status_code == 400            # nothing waits now
    drain(svc)
    p = c.get(f"/v3/studies/{sid}/pipeline").json()["pipeline"]
    assert p["status"] == "complete", p["error"]
    assert [x for x in fake.calls if x == ("POST", "/api/graph/ontology/generate")] == [("POST", "/api/graph/ontology/generate")]
    u = p["usage"]
    assert u["approval"]["approved"] and u["approval"]["for"] == "simulation spending only" and u["profile"] == "quick"
    assert u["cost_confidence"] == "unknown" and u["incurred_low_usd"] is None and "unknown" in u["words"]
    assert u["agents"] == 8 and u["rounds"] == 10                     # what the fake server reported


def s_tudy_ok(c, sid):
    s = c.get(f"/v3/studies/{sid}").json()
    return s["status"] in ("complete", "partial") and bool(s["study"]["forecast"])


def test_the_profile_reaches_mirofish_and_a_large_graph_asks_before_any_agent_is_prepared(app, use):
    c, svc, ids, _ = app
    fake = use(FakeMiroFish(agents=50), mirofish_cost_per_run_usd=0.05)
    sid = ids[1]
    sent = []
    real = fake.request

    def spy(method, url, **kw):
        sent.append((url.rsplit("/api/", 1)[-1], kw.get("json")))
        return real(method, url, **kw)
    fake.request = spy
    c.post(f"/v3/studies/{sid}/pipeline/start", json={})
    drain(svc)
    p = svc.pipeline.view(sid)
    assert p["status"] == "awaiting_approval" and "50 agents, more than the profile's 20" in p["governance"]["reason"]
    assert ("POST", "/api/simulation/prepare") not in fake.calls and ("POST", "/api/simulation/create") not in fake.calls
    c.post(f"/v3/studies/{sid}/pipeline/approve", json={})
    drain(svc)
    assert svc.pipeline.view(sid)["status"] == "complete"
    prep = next(j for u, j in sent if u == "simulation/prepare")
    start = next(j for u, j in sent if u == "simulation/start")
    assert prep["parallel_profile_count"] == 3 and start["platform"] == "reddit" and start["max_rounds"] == 4


def test_a_budget_that_is_used_up_refuses_and_keeps_the_study(app, use):
    c, svc, ids, _ = app
    fake = use(mirofish_cost_per_run_usd=1.0, mirofish_daily_budget_usd=0.1)
    sid = ids[2]
    c.post(f"/v3/studies/{sid}/pipeline/start", json={})
    drain(svc)
    p = svc.pipeline.view(sid)
    assert p["status"] == "failed" and "MiroFish was not called" in p["error"] and "daily" in p["error"] and _paid(fake) == []
    assert p["governance"]["state"] == "budget_exceeded" and s_tudy_ok(c, sid)
    r = c.post("/v3/scenarios/check", json={}).json()
    assert r["state"] == "budget_exceeded" and r["state_label"] == "Budget exceeded"


class Down(FakeMiroFish):
    """MiroFish answers /health but its stages fail with a server error."""

    def request(self, method, url, timeout=None, **kw):
        self.calls.append((method, url))
        return Resp(500, {"success": False, "error": "LLM provider unavailable"})


def test_the_circuit_opens_after_repeated_failures_and_recovers_with_one_free_probe(app, use):
    c, svc, ids, _ = app
    use(Down(), mirofish_cost_per_run_usd=0.05, mirofish_circuit_failures=2, mirofish_circuit_cooldown_seconds=60)
    for sid in ids[3:5]:
        c.post(f"/v3/studies/{sid}/pipeline/start", json={})
        drain(svc)
        assert svc.pipeline.view(sid)["status"] == "failed"
    st = G.Circuit(svc).state()
    assert st["state"] == "open" and st["failures"] == 2 and "LLM provider unavailable" in st["last_error"]
    # Open: the next study keeps its quantitative research, MiroFish is not called, and nothing is substituted.
    healthy = use(mirofish_cost_per_run_usd=0.05, mirofish_circuit_failures=2, mirofish_circuit_cooldown_seconds=60)
    sid = ids[5]
    c.post(f"/v3/studies/{sid}/pipeline/start", json={})
    drain(svc)
    p = svc.pipeline.view(sid)
    assert p["status"] == "failed" and "circuit is open" in p["error"] and healthy.calls == [] and not p["demo"] and s_tudy_ok(c, sid)
    assert c.post("/v3/scenarios/check", json={}).json()["state"] == "circuit_open"
    # After the cooldown: one free probe (its lists), it passes, the circuit closes and the run goes ahead.
    st = svc.db.cache_get(G.CIRCUIT_KEY)
    st["opened_at"] = (datetime.now(timezone.utc) - timedelta(minutes=5)).isoformat()
    svc.db.cache_put(G.CIRCUIT_KEY, st)
    assert G.Circuit(svc).state()["state"] == "half_open"
    p = c.post(f"/v3/studies/{sid}/pipeline/retry", json={}).json()["pipeline"]
    drain(svc)
    assert svc.pipeline.view(sid)["status"] == "complete" and G.Circuit(svc).state()["state"] == "closed"
    assert healthy.calls[0] == ("GET", "/api/graph/project/list")                               # the probe came first


def test_a_cancel_is_not_a_provider_failure_and_the_circuit_can_be_reset(app, use):
    c, svc, ids, _ = app
    use(mirofish_cost_per_run_usd=0.05)
    sid = ids[6]
    c.post(f"/v3/studies/{sid}/pipeline/start", json={})
    svc.pipeline.start({"study_id": sid}) if not svc.pipeline.load(sid).get("run_id") else None
    c.post(f"/v3/studies/{sid}/pipeline/cancel", json={})
    assert G.Circuit(svc).state()["failures"] == 0
    G.Circuit(svc).failure("x")
    G.Circuit(svc).failure("y")
    G.Circuit(svc).failure("z")
    assert G.Circuit(svc).state()["state"] == "open"
    r = c.post("/v3/scenarios/circuit/reset", json={}).json()
    assert r["circuit"]["state"] == "closed" and r["circuit"]["failures"] == 0


# --- duplicate paid work ------------------------------------------------------------------------------------------------

class Crash(BaseException):
    pass


def test_a_stop_in_the_middle_of_a_costly_write_reconciles_instead_of_sending_it_again(app, use):
    """The process stops while the ontology request is out (after the server made the project, before the answer was
    saved). On restart the step finds the write marked as being sent, looks for the project by its name, and goes on
    with it: one project, one ontology request."""
    c, svc, ids, _ = app
    fake = use(mirofish_cost_per_run_usd=0.05)
    real = fake.request
    state = {"crashed": False}

    def crash_once(method, url, **kw):
        out = real(method, url, **kw)
        if url.endswith("/api/graph/ontology/generate") and not state["crashed"]:
            state["crashed"] = True
            raise Crash("the app stopped mid-request")
        return out
    fake.request = crash_once
    sid = ids[7]
    c.post(f"/v3/studies/{sid}/pipeline/start", json={})
    with pytest.raises(Crash):
        drain(svc)
    run_id = svc.pipeline.load(sid)["run_id"]
    saved = json.loads(svc.db.one("SELECT external_json FROM scenario_runs WHERE id = ?", (run_id,))["external_json"])
    assert saved["state"]["sending"] == "ontology"                           # recorded before it was sent
    with svc.db.tx() as cur:
        cur.execute("UPDATE jobs SET lease_until = ? WHERE status = 'running'", ("2000-01-01T00:00:00Z",))
    drain(svc)
    assert svc.pipeline.view(sid)["status"] == "complete"
    assert fake.calls.count(("POST", "/api/graph/ontology/generate")) == 1 and len(fake.projects) == 1


def test_a_write_whose_connection_broke_after_sending_is_reconciled_not_resent():
    class Broken(FakeMiroFish):
        def request(self, method, url, timeout=None, **kw):
            if url.endswith("/api/simulation/create"):
                super().request(method, url, timeout=timeout, **kw)            # the server made it
                raise requests.ConnectionError("Connection aborted: RemoteDisconnected")
            return super().request(method, url, timeout=timeout, **kw)
    mf = fake_mirofish(Broken())
    out = mf.advance({"run_id": "r"}, {"stage": "sim_create", "project_id": "proj_1"})
    assert out.state["stage"] == "sim_create_reconcile"
    refused = fake_mirofish(FakeMiroFish())
    refused.http = type("S", (), {"request": lambda *a, **k: (_ for _ in ()).throw(requests.ConnectionError("NewConnectionError: Connection refused"))})()
    out = refused.advance({"run_id": "r"}, {"stage": "sim_create", "project_id": "proj_1"})
    assert out.state.get("transient") == 1 and out.state["stage"] == "sim_create"          # never connected: safe to send again


# --- readiness --------------------------------------------------------------------------------------------------------

class Other:
    calls = []

    def get(self, url, timeout=None):
        return Resp(200, {"status": "ok", "service": "Some Other Service"})


def test_readiness_has_levels_and_says_what_to_do(app, use):
    c, svc, ids, _ = app
    use(mirofish_cost_per_run_usd=0.05)
    r = c.post("/v3/scenarios/check", json={}).json()
    assert r["state"] == "ready" and r["levels"]["A"]["ok"] and r["levels"]["B"]["ok"] and r["functional"]["ok"] is None
    assert "model and Zep keys are set" in r["levels"]["B"]["detail"]
    svc.scenario_provider_override = lambda name: MiroFish("http://127.0.0.1:5001", session=Other())
    r = c.post("/v3/scenarios/check", json={}).json()
    assert r["state"] == "config_error" and "not MiroFish" in r["detail"] and r["action"]
    svc.scenario_provider_override = lambda name: MiroFish("")
    assert c.post("/v3/scenarios/check", json={}).json()["state"] == "config_error"

    class NoAnswer:
        def get(self, url, timeout=None):
            raise requests.ConnectionError("refused")
    svc.scenario_provider_override = lambda name: MiroFish("http://127.0.0.1:5001", session=NoAnswer())
    r = c.post("/v3/scenarios/check", json={}).json()
    assert r["state"] == "unavailable" and "setup_mirofish.sh start" in r["action"]

    class HealthOnly(FakeMiroFish):
        def request(self, method, url, timeout=None, **kw):
            return Resp(404, {"success": False, "error": "no such route"})
    svc.scenario_provider_override = lambda name: MiroFish("http://127.0.0.1:5001", session=HealthOnly())
    r = c.post("/v3/scenarios/check", json={}).json()
    assert r["state"] == "degraded" and "answers /health but not its API" in r["detail"]
    s = c.get("/v3/scenarios/settings").json()
    assert s["profile"] == "quick" and [p["key"] for p in s["profiles"]] == ["quick", "standard", "deep"]
    assert c.post("/v3/scenarios/settings", json={"profile": "standard"}).json()["profile"] == "standard"


# --- the ledger ---------------------------------------------------------------------------------------------------------

def test_the_ledger_counts_failed_runs_by_their_stage_and_takes_entered_and_measured_costs(app, use):
    c, svc, ids, _ = app
    use(mirofish_cost_per_run_usd=1.0, scenario_ai_input_usd_per_mtok=3.0, scenario_ai_output_usd_per_mtok=15.0)
    led = G.Ledger(svc)
    day, month = G.day_month()
    rows = [("srun_a", "mirofish", "complete", "done", "2026-10-22T20:00:00Z"),
            ("srun_b", "mirofish", "failed", "run_wait", "2026-10-22T20:00:00Z"),
            ("srun_c", "mirofish", "failed", "ontology", None)]
    with svc.db.tx() as cur:
        for rid, prov, status, stage, started in rows:
            cur.execute("""INSERT INTO scenario_usage (run_id, study_id, provider, profile, created_at, started_at, ended_at, status, stage_reached, day, month,
                           est_low_usd, est_high_usd, cost_confidence, updated_at) VALUES (?, 's', ?, 'standard', ?, ?, ?, ?, ?, ?, ?, 0.5, 2.0, 'estimate', ?)""",
                        (rid, prov, "2026-10-22T20:00:00Z", started, "2026-10-22T20:30:00Z", status, stage, day, month, "2026-10-22T20:00:00Z"))
    sp = led.spent("day", day)
    assert sp["low_usd"] == pytest.approx(0.5 + 0.5 * 0.85) and sp["high_usd"] == pytest.approx(2.0 + 2.0 * 0.85)   # the unsent one costs nothing
    v = led.enter_actual("srun_a", 0.73, "from the provider's bill")
    assert v["cost_confidence"] == "user_entered" and v["incurred_low_usd"] == v["incurred_high_usd"] == pytest.approx(0.73)
    rep = c.get("/v3/scenarios/usage").json()
    assert rep["average_successful_usd"] == pytest.approx(0.73) and rep["failure_rate"] == pytest.approx(2 / 3)
    assert "never as $0.00" in rep["note"] and rep["budgets"]["daily_limit"] == 5.0
    assert c.post("/v3/scenarios/usage/srun_a", json={"actual_usd": -1}).status_code == 400
    # An AI call's tokens are measured, and priced at the rates you set.
    sid = ids[8]
    with svc.db.tx() as cur:
        seed = svc.db.one("SELECT id FROM scenario_seeds LIMIT 1")
        cur.execute("""INSERT INTO scenario_runs (id, seed_id, study_id, provider, status, created_at, schema_version, prompt_version, config_json, external_json)
                       VALUES ('srun_ai', ?, ?, 'ai', 'complete', ?, 1, 'p', '{}', ?)""",
                    (seed["id"], sid, "2026-10-22T20:00:00Z", json.dumps({"role": "control", "usage": {"input_tokens": 100_000, "output_tokens": 10_000}})))
    led.sync("srun_ai")
    r = led.row("srun_ai")
    assert r["cost_confidence"] == "measured" and r["measured_usd"] == pytest.approx(0.3 + 0.15)


# --- the command line -----------------------------------------------------------------------------------------------------

def test_the_readiness_check_reports_every_level_and_spends_nothing(app, use, capsys, monkeypatch):
    import run
    c, svc, ids, s = app
    fake = use(mirofish_cost_per_run_usd=0.05)
    monkeypatch.setattr("tradeassist.v3.scenario_providers.requests.Session", lambda: fake)
    settings = replace(s, data_mode="demo", db_path=s.db_path, mirofish_cost_per_run_usd=0.05)
    code = run.scenario_check(settings, "SPY")
    out = capsys.readouterr().out
    for words in ("Level A, connection: ok", "Level B, API and storage: ok", "Level C, functional:", "Circuit breaker: closed",
                  "Profile Quick (default)", "Next Quick run:", "Spending today", "minimum open interest"):
        assert words in out, words
    assert _paid(fake) == [] and code == 0


def test_the_functional_test_needs_real_data_and_asks_before_spending(app, capsys, monkeypatch):
    import run
    c, svc, ids, s = app
    assert run.scenario_smoke(replace(s, data_mode="demo"), "SPY", "quick", yes=True) == 1
    out = capsys.readouterr().out
    assert "needs real Tradier data" in out and "Functional test FAILED" in out


# --- regressions from the independent review ------------------------------------------------------------------------

def test_a_crash_before_admission_cannot_send_the_run_unapproved(app, use, monkeypatch):
    c, svc, ids, _ = app
    fake = use()                                                             # unknown cost: it must ask first
    real = svc.pipeline._govern
    state = {"n": 0}

    def crash_once(*a, **k):
        if not state["n"]:
            state["n"] = 1
            raise Crash("stopped between creating the run and admitting it")
        return real(*a, **k)
    monkeypatch.setattr(svc.pipeline, "_govern", crash_once)
    sid = ids[9]
    c.post(f"/v3/studies/{sid}/pipeline/start", json={})
    with pytest.raises(Crash):
        drain(svc)
    with svc.db.tx() as cur:
        cur.execute("UPDATE jobs SET lease_until = ? WHERE status = 'running'", ("2000-01-01T00:00:00Z",))
    svc.pipeline.recover()
    # Whatever runs next (the start again, or a left-over step), the run is admitted before anything is sent.
    run_id = svc.pipeline.load(sid)["run_id"]
    if run_id:
        svc.pipeline.step({"run_id": run_id, "n": 0})
    drain(svc)
    p = svc.pipeline.view(sid)
    assert p["status"] == "awaiting_approval" and _paid(fake) == []


def test_waiting_for_an_agent_approval_neither_burns_the_time_cap_nor_holds_the_slot(app, use):
    c, svc, ids, _ = app
    fake = use(FakeMiroFish(agents=50), mirofish_cost_per_run_usd=0.05)
    a, b = ids[10], ids[11]
    c.post(f"/v3/studies/{a}/pipeline/start", json={})
    drain(svc)
    assert svc.pipeline.view(a)["status"] == "awaiting_approval"
    assert c.post(f"/v3/studies/{a}/pipeline/retry", json={}).status_code == 400          # approve or cancel first
    fake.agents = 8                                                                       # the next study's graph fits
    c.post(f"/v3/studies/{b}/pipeline/start", json={})
    drain(svc)
    assert svc.pipeline.view(b)["status"] == "complete"                                   # not kept waiting for a's slot
    # Approved 25 minutes later: the wait is not the run's time.
    run_id = svc.pipeline.load(a)["run_id"]
    ext = json.loads(svc.db.one("SELECT external_json FROM scenario_runs WHERE id = ?", (run_id,))["external_json"])
    long_ago = (datetime.now(timezone.utc) - timedelta(minutes=25)).isoformat()
    ext.update(started_at=long_ago, paused_at=long_ago)
    with svc.db.tx() as cur:
        cur.execute("UPDATE scenario_runs SET external_json = ? WHERE id = ?", (json.dumps(ext), run_id))
    use(fake, mirofish_cost_per_run_usd=0.05)
    c.post(f"/v3/studies/{a}/pipeline/approve", json={})
    drain(svc)
    assert svc.pipeline.view(a)["status"] == "complete" and G.Circuit(svc).state()["failures"] == 0


def test_approval_refuses_while_the_circuit_is_open_and_an_unknown_agent_count_asks(app, use):
    c, svc, ids, _ = app

    class NoCount(FakeMiroFish):
        def request(self, method, url, timeout=None, **kw):
            if "/api/simulation/entities/" in url:
                self.calls.append((method, "/api/simulation/entities"))
                return Resp(200, {"success": True, "data": {"entities": "not a list"}})
            return super().request(method, url, timeout=timeout, **kw)
    fake = use(NoCount(), mirofish_cost_per_run_usd=0.05)
    sid = ids[12]
    c.post(f"/v3/studies/{sid}/pipeline/start", json={})
    drain(svc)
    p = svc.pipeline.view(sid)
    assert p["status"] == "awaiting_approval" and "did not report how many agents" in p["governance"]["reason"]
    assert ("POST", "/api/simulation/prepare") not in fake.calls
    for _ in range(3):
        G.Circuit(svc).failure("down")
    r = c.post(f"/v3/studies/{sid}/pipeline/approve", json={})
    assert r.status_code == 400 and "circuit is open" in r.json()["error"]
    G.Circuit(svc).reset()
    assert c.post(f"/v3/studies/{sid}/pipeline/approve", json={}).status_code == 200
    drain(svc)
    assert svc.pipeline.view(sid)["status"] == "complete"


def test_a_reply_cut_off_after_a_costly_write_is_reconciled_not_resent():
    class CutOff(FakeMiroFish):
        def request(self, method, url, timeout=None, **kw):
            out = super().request(method, url, timeout=timeout, **kw)
            if url.endswith("/api/graph/ontology/generate"):
                raise requests.exceptions.ChunkedEncodingError("Connection broken: IncompleteRead")
            return out
    fake = CutOff()
    mf = fake_mirofish(fake)
    ctx = {"run_id": "r1", "filename": "x.md", "markdown": "# x", "question": "q", "instructions": "i"}
    out = mf.advance(ctx, {})
    assert out.state["stage"] == "ontology_reconcile" and len(fake.projects) == 1
    out = mf.advance(ctx, out.state)
    assert out.state["stage"] == "build" and len(fake.projects) == 1                     # found by its name: one project


def test_an_approval_gated_start_starts_the_control_when_approved(app, use, monkeypatch):
    c, svc, ids, _ = app
    use()
    started = []
    monkeypatch.setattr(svc.pipeline, "_start_control", lambda study_id, seed, main, attempt: started.append(study_id) or None)
    sid = ids[13]
    c.post(f"/v3/studies/{sid}/pipeline/start", json={})
    drain(svc)
    assert started == [] and svc.pipeline.view(sid)["status"] == "awaiting_approval"
    c.post(f"/v3/studies/{sid}/pipeline/approve", json={})
    assert started == [sid]
