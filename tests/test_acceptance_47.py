"""4.7 acceptance: the plan's 24 mandatory tests, each checked where it can be checked best. Most are covered by the
4.7 test files; the rest are here, through the API on demo data with the canned demo provider (and the MiroFish
stand-in where the provider's behaviour matters).

| Plan test | Checked by |
| --- | --- |
| 1 One-click complete research | test_forecast_studio.py::test_one_click_study_gives_one_authoritative_result...; test_forecast_studio_ui.py (first test) |
| 2 Baseline-only research | test_forecast_studio.py::test_baseline_only_and_an_unsupported_horizon_make_no_provider_request |
| 3 Unsupported horizon | the same |
| 4 No catalyst available | here: test_4_no_catalyst_is_a_labelled_hypothetical_never_news |
| 5 Neutral baseline | test_forecast_studio.py::test_a_neutral_base_says_what_it_can_and_cannot_claim; test_workstation.py (flat by design) |
| 6 Chart visibility | test_forecast_studio_ui.py::test_one_click_result_the_legend_controls_layers_per_study... |
| 7 Study-specific preferences | the same, and test_forecast_studio.py::test_view_preferences_are_per_study... |
| 8 Temporary preview | test_forecast_studio.py::test_a_preview_draws_a_conditional_line_and_writes_nothing; test_forecast_studio_ui.py |
| 9 Save scenario version | test_forecast_studio.py::test_save_version_keeps_the_assumptions_values_them...; test_catalyst_workstation.py |
| 10 Version-matched valuation | the same; test_forecast_studio.py::test_the_draft_api_refuses_mismatched... |
| 11 Exploratory decision safety | test_forecast_studio.py::test_save_version_...without_publishing; here: test_11_... with a Watch-worthy stress |
| 12 Historical comparison | test_forecast_studio.py::test_one_compatible_version_compares_read_only; test_forecast_studio_ui.py |
| 13 Switching stocks | test_forecast_studio_ui.py::test_a_new_stock_clears_every_forecast_layer...; test_catalyst_workstation.py |
| 14 Reopening a saved study | here: test_14_a_reopened_study_keeps_its_original_anchor_and_dates |
| 15 Decision highlight | test_forecast_studio.py::test_the_decision_focus_is_only_the_case...; test_forecast_studio_ui.py |
| 16 Trigger verification | here: test_16_an_unverified_source_stays_a_watch; tests/test_v451.py (verification itself) |
| 17 MiroFish unavailable | here: test_17_...; tests/test_acceptance_46.py::test_b_mirofish_unavailable |
| 18 Spending approval | here: test_18_...; tests/test_acceptance_46.py::test_c_... |
| 19 Interrupted paid simulation | tests/test_acceptance_46.py::test_d_... and tests/test_v451.py (six crash points) |
| 20 Trading Desk references | test_forecast_studio.py::test_the_draft_api_refuses_mismatched_study_version_valuation_or_decision |
| 21 Data-quality degradation | here: test_21_...; tests/test_v451.py, tests/test_opportunity.py |
| 22 Discover continuity | test_workstation.py::test_discover_ranks_once_..._studies_an_opportunity |
| 23 Chart timeframe compatibility | test_forecast_studio_ui.py::test_a_new_stock_clears_every_forecast_layer_and_session_paths_stay_on_daily_bars |
| 24 Regression and execution safety | here: test_24_...; tests/test_api.py::test_no_endpoint_can_place_an_order; the whole suite |
"""

from __future__ import annotations

import json
from dataclasses import replace
from datetime import datetime

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
from tests.test_one_click import FakeMiroFish, _wait, drain


@pytest.fixture(scope="module")
def app(tmp_path_factory):
    import tradeassist.api as api_mod
    api_mod.TEST_HOSTS = ("testserver",)
    path = tmp_path_factory.mktemp("acc47") / "t.db"
    s = Settings(data_mode="demo", db_path=path, desk_worker="off", scenario_provider="demo", scenario_auto=True,
                 mirofish_cost_per_run_usd=0.05, scenario_budget=5000.0, scenario_max_loss=1000.0)
    svc = TradeAssistant(s, provider=DemoData(), db=Database(path))
    c = TestClient(create_app(s, svc))
    yield c, svc, s
    svc.db.close()


def _study(c, sym, key, **body):
    s = _wait(c, c.post("/v3/studies", json={"symbol": sym, "horizon_days": 5, "request_key": key, **body}).json()["id"])
    # The continuation is queued by the study's own thread just after it is saved: wait for it before draining.
    svc = c.app.state.svc if hasattr(c.app.state, "svc") else None
    import time
    end = time.time() + 20
    while time.time() < end:
        p = c.get(f"/v3/studies/{s['id']}/pipeline").json()["pipeline"]
        if p is None or (p.get("stages") or [{}])[0].get("state") != "running":
            break
        time.sleep(0.1)
    _ = svc
    return s


def _bundle(c, sid, **q):
    r = c.get(f"/v3/studies/{sid}/result", params=q)
    assert r.status_code == 200, r.text
    return r.json()


def test_4_no_catalyst_is_a_labelled_hypothetical_never_news(app, monkeypatch):
    """With no earnings date and no sourced headline, the automatic run names a hypothetical current-setup case, and the
    result says it is hypothetical (the screen calls it "not reported news")."""
    c, svc, _ = app
    monkeypatch.setattr(svc, "_earnings", lambda *a, **k: None)
    real = svc.pipeline.auto_catalyst

    def no_news(study, sessions):                 # the real rule, on evidence with no sourced headline
        ev = dict(study.get("evidence") or {})
        ev["items"] = [i for i in ev.get("items") or [] if i.get("kind") != "news"]
        return real({**study, "evidence": ev}, sessions)
    monkeypatch.setattr(svc.pipeline, "auto_catalyst", no_news)
    s = _study(c, "KO", "acc47-four-0001")
    drain(svc)
    b = _bundle(c, s["id"])
    cat = b["selected_version"]["catalyst"]
    assert cat["hypothetical"] is True and cat["title"].startswith("Current setup:") and not cat.get("event_date"), (cat, b["automatic_run"])
    assert "hypothetical" in b["automatic_run"]["catalyst_basis"]
    assert b["selected_context"]["mode"] == "official" and b["base"]["path_pct"]


def test_11_an_attractive_stress_is_assessed_but_never_published_or_scored(app):
    c, svc, _ = app
    s = _study(c, "AAPL", "acc47-eleven-0001")
    drain(svc)
    b = _bundle(c, s["id"])
    cases = [{k: a[k] for k in ("key", "visible", "issuer", "factors", "dispersion")} for a in b["selected_version"]["assumptions"]]
    for x in cases:
        if x["key"] == "bullish":
            x["issuer"] = {"value_pct": 25.0, "session": 1, "source": "user_stress", "source_ids": []}
    rows = lambda t: svc.db.one(f"SELECT COUNT(*) AS n FROM {t}")["n"]       # noqa: E731
    before = {t: rows(t) for t in ("scenario_decisions", "opportunity_observations", "scenario_forecasts", "notifications", "scenario_strategy_outcomes")}
    notices = c.get("/v3/scenarios/notices").json()["notices"]
    out = c.post(f"/v3/studies/{s['id']}/scenario-lab/versions", json={"from_version_id": b["selected_context"]["version_id"], "cases": cases}).json()
    assert out["assessment"]["status"] in ("watch", "qualified", "no_setup") and out["assessment"]["research_only"]
    assert {t: rows(t) for t in before} == before and c.get("/v3/scenarios/notices").json()["notices"] == notices
    assert svc.pipeline.load(s["id"])["version_id"] == b["selected_context"]["version_id"]


@pytest.mark.clock("2026-12-30T12:00")
def test_14_a_reopened_study_keeps_its_original_anchor_and_dates(app):
    """A study opened after its window shows the forecast it made, at its anchor and sessions, labelled historical."""
    c, svc, _ = app
    old = svc.db.one("SELECT id FROM studies WHERE request_key = 'acc47-eleven-0001'")["id"]
    b = _bundle(c, old)
    frame = c.get(f"/v3/studies/{old}").json()["frame"]
    assert b["anchor"] == frame["anchor"] and [x["date"] for x in b["sessions"]] == [x["date"] for x in frame["sessions"]]
    assert b["historical"] is True and "original" in b["historical_note"]
    assert b["selected_version"]["stale"] is True and b["selected_version"]["anchor"] == b["anchor"]


def test_16_an_unverified_source_stays_a_watch(app):
    c, svc, _ = app
    s = _study(c, "MSFT", "acc47-sixteen-0001")
    drain(svc)
    b = _bundle(c, s["id"])
    d = b["decision"]
    if d["status"] != "watch":
        pytest.skip(f"the demo study made a {d['status']} decision; a Watch is needed to record a trigger")
    case = d["setup"]["case"]
    from datetime import timezone
    now = datetime.now(timezone.utc).isoformat()           # with its offset, as the screen sends it
    r = c.post(f"/v3/studies/{s['id']}/decision/trigger", json={"case": case, "title": "Reported: the event happened", "published_at": now})
    assert r.status_code == 200, r.text
    nb = _bundle(c, s["id"])
    assert nb["decision"]["status"] == "watch"
    assert nb["decision"]["setup"]["trigger_evidence"]["status"] == "unverified"


@pytest.fixture
def mirofish(app):
    c, svc, s = app
    state = {}

    def use(fake=None, **settings):
        svc.settings = replace(s, scenario_provider="mirofish", mirofish_url="http://127.0.0.1:5001", **settings)
        state["fake"] = fake or FakeMiroFish()
        p = G.profile(svc.settings, "quick")
        svc.scenario_provider_override = lambda name: MiroFish("http://127.0.0.1:5001", session=state["fake"], poll_first=0.0, poll_most=0.0,
                                                               max_rounds=p["rounds"], max_agents=p["agents"], platform=p["platform"],
                                                               parallel_profiles=p["parallel_profiles"], profile="quick")
        svc.db.cache_put("research:scenario_settings", {"provider": "mirofish", "auto": True})
        return state["fake"]
    yield use
    svc.settings = s
    svc.scenario_provider_override = None
    svc.db.cache_put("research:scenario_settings", {})
    with svc.db.tx() as cur:
        cur.execute("DELETE FROM kv_cache WHERE key = ?", (G.CIRCUIT_KEY,))


def test_17_mirofish_unavailable_leaves_a_usable_quantitative_study(app, mirofish):
    c, svc, _ = app

    class Off(FakeMiroFish):
        def get(self, url, timeout=None):
            raise requests.ConnectionError("Connection refused")
    fake = mirofish(Off())
    s = _study(c, "JPM", "acc47-seventeen-0001")
    drain(svc)
    b = _bundle(c, s["id"])
    assert b["lifecycle"]["state"] == "simulation_failed" and b["lifecycle"]["baseline_ready"]
    assert b["base"]["path_pct"] and b["strategies"] and b["selected_version"] is None and b["decision"] is None
    assert b["pipeline"]["can_retry"] and not b["pipeline"]["demo"] and fake.calls == []


def test_18_a_run_over_the_automatic_limit_waits_for_approval(app, mirofish):
    c, svc, _ = app
    fake = mirofish(mirofish_cost_per_run_usd=3.0)
    s = _study(c, "XOM", "acc47-eighteen-0001")
    drain(svc)
    b = _bundle(c, s["id"])
    assert b["lifecycle"]["state"] == "awaiting_approval" and b["pipeline"]["can_approve"] and fake.calls == []
    assert b["base"]["path_pct"] and b["strategies"]


def test_21_research_quality_data_is_never_an_executable_qualified_setup(app):
    """Demo prices are research quality: every structure says so, and a decision on them is an example."""
    c, svc, _ = app
    sid = svc.db.one("SELECT id FROM studies WHERE request_key = 'acc47-eleven-0001'")["id"]
    b = _bundle(c, sid)
    assert b["valuation"] and all(r["quality"] in ("research", None) for r in b["strategies"])
    d = b["decision"]
    assert d["example"] is True and (d["status"] != "qualified" or d["setup"]["data_quality"] == "research")


def test_24_no_4_7_route_can_place_an_order_or_change_a_trading_permission(app):
    c, svc, _ = app
    paths = c.get("/api/openapi.json").json()["paths"]
    new = [p for p in paths if "/scenario-lab/" in p or p.endswith("/result") or p.endswith("/presentation")]
    assert new and not [p for p in new if "order" in p.lower() or "desk" in p.lower() or "approv" in p.lower()]
    policy = json.dumps(c.get("/v3/desk/policy").json(), sort_keys=True) if "/v3/desk/policy" in paths else None
    sid = svc.db.one("SELECT id FROM studies WHERE request_key = 'acc47-eleven-0001'")["id"]
    b = _bundle(c, sid)
    c.post(f"/v3/studies/{sid}/scenario-lab/preview", json={"from_version_id": b["selected_context"]["version_id"],
                                                            "cases": [{"key": "bullish", "issuer": {"value_pct": 3, "session": 1, "source": "user_stress"}}]})
    if policy is not None:
        assert json.dumps(c.get("/v3/desk/policy").json(), sort_keys=True) == policy
    assert svc.db.one("SELECT COUNT(*) AS n FROM trade_proposals")["n"] == 0
