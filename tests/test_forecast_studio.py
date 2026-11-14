"""4.7 the forecast studio's backend: one authoritative result per study (the ForecastBundle), per-study presentation
state, the Scenario Lab's preview (nothing written), saved versions with research-only assessments (nothing
published), version-matched valuations and decisions, one-version comparison, decision focus, promotion, and the
Trading Desk draft's reference checks.

Through the API on demo data with the canned demo provider, the same set-up as tests/test_one_click.py."""

from __future__ import annotations

import json
from dataclasses import replace

import pytest
from fastapi.testclient import TestClient

from tradeassist.api import create_app
from tradeassist.config import Settings
from tradeassist.db import Database
from tradeassist.providers.demo import DemoData
from tradeassist.service import TradeAssistant
from tradeassist.v3.forecast_state import comparable, headline
from tradeassist.v3.scenario_lab import promotion_problems
from tests.test_one_click import _wait, drain

TABLES = ("scenario_versions", "scenario_valuations", "scenario_decisions", "scenario_assessments", "opportunity_observations",
          "opportunity_candidates", "jobs", "notifications", "scenario_forecasts", "scenario_runs", "scenario_seeds", "study_presentation")


def counts(svc):
    return {t: svc.db.one(f"SELECT COUNT(*) AS n FROM {t}")["n"] for t in TABLES}


@pytest.fixture(scope="module")
def app(tmp_path_factory):
    import tradeassist.api as api_mod
    api_mod.TEST_HOSTS = ("testserver",)
    path = tmp_path_factory.mktemp("studio") / "t.db"
    # Room in the loss limit and the budget, so the demo's cases can make setups worth checking (as in the 4.4 tests).
    s = Settings(data_mode="demo", db_path=path, desk_worker="off", scenario_provider="demo", scenario_auto=True,
                 mirofish_cost_per_run_usd=0.05, scenario_budget=5000.0, scenario_max_loss=1000.0)
    svc = TradeAssistant(s, provider=DemoData(), db=Database(path))
    c = TestClient(create_app(s, svc))
    out = {}
    for key, sym, h, scen in (("auto", "AAPL", 5, None), ("other", "MSFT", 5, None), ("base_only", "KO", 5, False), ("ten", "JPM", 10, None)):
        body = {"symbol": sym, "horizon_days": h, "request_key": f"studio-{key}-0001"}
        if scen is not None:
            body["scenarios"] = scen
        out[key] = _wait(c, c.post("/v3/studies", json=body).json()["id"])["id"]
    drain(svc)
    yield c, svc, out
    svc.db.close()


def _bundle(c, sid, **q):
    r = c.get(f"/v3/studies/{sid}/result", params=q)
    assert r.status_code == 200, r.text
    return r.json()


def _assumptions(b):
    return [{k: a[k] for k in ("key", "visible", "issuer", "factors", "dispersion")} for a in b["selected_version"]["assumptions"]]


def _with_bullish(cases, value, source="user_stress"):
    out = json.loads(json.dumps(cases))
    for x in out:
        if x["key"] == "bullish":
            x["issuer"] = {"value_pct": value, "session": 1, "source": source, "source_ids": []}
    return out


# --- the bundle ----------------------------------------------------------------------------------------------------------

def test_one_click_study_gives_one_authoritative_result_and_reading_it_writes_nothing(app):
    """Test 1: evidence, the base, the automatic cases, the valuation and the decision, with no manual step; all from saved
    records (no paid simulation, no market data, no repricing)."""
    c, svc, st = app
    before = counts(svc)
    b = _bundle(c, st["auto"])
    assert counts(svc) == before
    assert b["contract"] == "forecast-bundle-1" and b["symbol"] == "AAPL" and b["horizon_sessions"] == 5 and b["data_mode"] == "demo"
    assert b["lifecycle"]["state"] == "decision_ready" and b["lifecycle"]["baseline_ready"] and not b["lifecycle"]["running"]
    assert b["anchor"]["price"] > 0 and len(b["sessions"]) == 5 and len(b["base"]["path_pct"]) == 5
    assert b["base"]["price"][-1] == pytest.approx(b["anchor"]["price"] * (1 + b["base"]["path_pct"][-1] / 100))
    ctx = b["selected_context"]
    assert ctx["mode"] == "official" and ctx["is_official"] and ctx["version_id"] == b["automatic_run"]["official_version_id"]
    assert [v["kind"] for v in b["versions"]] == ["official"] and b["versions"][0]["grading"] == "frozen for the forward scorecard"
    sv = b["selected_version"]
    assert [x["key"] for x in sv["cases"]] == ["central", "bullish", "bearish"] and sv["sources"]
    # The valuation and the decision are the selected version's own.
    assert b["valuation"]["version_id"] == ctx["version_id"] == b["decision"]["version_id"]
    assert b["decision"]["kind"] == "official" and b["decision"]["current"] and not b["decision"]["research_only"]
    assert b["decision"]["id"] == b["automatic_run"]["decision_id"]
    rows = {r["key"]: r for r in b["strategies"]}
    cands = {x["key"] for x in c.get(f"/v3/studies/{st['auto']}").json()["view"]["strategies"]["candidates"]}
    assert set(rows) == cands
    for r in rows.values():
        if r["not_valued"] is None:
            assert set(r["by_case"]) == {"central", "bullish", "bearish"} and r["snapshot_at"] == b["valuation"]["snapshot_at"]
            assert r["quality"] in ("execution", "research")
    assert rows["long_call"]["entry"]["kind"] == "debit" and rows["long_call"]["legs"][0]["option_type"]
    assert b["provenance"]["scenario_provider"] == "demo" and b["provenance"]["bundle"] == "forecast-bundle-1"


def test_baseline_only_and_an_unsupported_horizon_make_no_provider_request(app):
    """Tests 2 and 3: a baseline-only study and a 10-session study are complete without any scenario run."""
    c, svc, st = app
    for key in ("base_only", "ten"):
        b = _bundle(c, st[key])
        assert b["pipeline"] is None and b["automatic_run"] is None and b["versions"] == []
        assert b["lifecycle"]["state"] == "baseline_complete" and b["selected_context"]["mode"] == "baseline"
        assert b["base"]["path_pct"] and b["strategies"]
        assert svc.db.one("SELECT COUNT(*) AS n FROM scenario_runs WHERE study_id = ?", (st[key],))["n"] == 0
    assert _bundle(c, st["base_only"])["scenarios"]["supported"] is True
    ten = _bundle(c, st["ten"])["scenarios"]
    assert ten["supported"] is False and "10-session" in ten["reason"] and "baseline only" in ten["reason"]


@pytest.mark.parametrize("directional,range_,kind", [
    ("pending", "pending", "pending"), ("insufficient", "insufficient", "insufficient"), ("no_edge", "validated", "range_only"),
    ("not_held", "below_target", "no_edge"), ("edge", "below_target", "edge"), ("edge", "validated", "edge_and_range")])
def test_a_neutral_base_says_what_it_can_and_cannot_claim(directional, range_, kind):
    """Test 5: a flat base comes with its readiness, never as an unexplained 0.00% prediction."""
    fc = {"base": {"path_pct": [0.0, 0.0, 0.0]}, "readiness": {"directional": {"status": directional}, "range": {"status": range_}}}
    h = headline(fc)
    assert h["kind"] == kind and h["label"] and h["flat"] is True
    assert ("flat by design" in h["text"]) is (kind not in ("edge", "edge_and_range"))
    assert headline({"base": {"path_pct": [0.4, 1.2]}, "readiness": {"directional": {"status": "edge"}, "range": {"status": "validated"}}})["flat"] is False
    assert headline(None)["kind"] == "none"


# --- presentation state ------------------------------------------------------------------------------------------------------

def test_view_preferences_are_per_study_and_name_only_the_studys_own_versions(app):
    """Test 7: hiding Bearish on one study leaves another study's Bearish shown; a foreign version is refused."""
    c, svc, st = app
    r = c.post(f"/v3/studies/{st['auto']}/presentation", json={"visible_layers": {"bearish": False}})
    assert r.status_code == 200 and r.json()["visible_layers"]["bearish"] is False and r.json()["visible_layers"]["bullish"] is True
    assert _bundle(c, st["auto"])["presentation"]["visible_layers"]["bearish"] is False
    assert _bundle(c, st["other"])["presentation"]["visible_layers"]["bearish"] is True
    other_v = _bundle(c, st["other"])["selected_context"]["version_id"]
    bad = c.post(f"/v3/studies/{st['auto']}/presentation", json={"selected_version_id": other_v})
    assert bad.status_code == 400 and "another study" in bad.json()["error"]
    assert c.get(f"/v3/studies/{st['auto']}/result", params={"version": other_v}).status_code == 400
    assert c.post(f"/v3/studies/{st['auto']}/presentation", json={"visible_layers": {"gold": False}}).status_code == 400
    # Only the fields sent change.
    c.post(f"/v3/studies/{st['auto']}/presentation", json={"chart_focus": "decision"})
    p = c.get(f"/v3/studies/{st['auto']}/presentation").json()
    assert p["chart_focus"] == "decision" and p["visible_layers"]["bearish"] is False
    c.post(f"/v3/studies/{st['auto']}/presentation", json={"visible_layers": {"bearish": True}, "chart_focus": "cases"})


# --- Scenario Lab ---------------------------------------------------------------------------------------------------------------

def test_a_preview_draws_a_conditional_line_and_writes_nothing(app):
    """Test 8: a changed factor or move gives an unsaved path: no version, valuation, provider run, decision, notice."""
    c, svc, st = app
    b = _bundle(c, st["auto"])
    vid = b["selected_context"]["version_id"]
    cases = _with_bullish(_assumptions(b), 6.0)
    before = counts(svc)
    r = c.post(f"/v3/studies/{st['auto']}/scenario-lab/preview", json={"from_version_id": vid, "cases": cases, "generation": 41})
    assert r.status_code == 200, r.text
    pv = r.json()
    assert counts(svc) == before
    assert pv["preview"] is True and pv["saved"] is False and pv["generation"] == 41 and "Nothing is stored" in pv["note"]
    bull = next(x for x in pv["cases"] if x["key"] == "bullish")
    assert bull["quantified"] and bull["price"][0] == pytest.approx(pv["anchor"]["price"] * (1 + bull["return_pct"][0] / 100))
    assert pv["anchor"] == b["anchor"] and [s["date"] for s in pv["sessions"]] == [s["date"] for s in b["sessions"]]
    # The same checks as a saved version: a session past the horizon, an unknown factor.
    bad = json.loads(json.dumps(cases))
    bad[1]["issuer"]["session"] = 9
    bad[2]["factors"] = [{"key": "gold", "value": 1.0, "session": 1, "source": "user_stress", "source_ids": []}]
    r = c.post(f"/v3/studies/{st['auto']}/scenario-lab/preview", json={"from_version_id": vid, "cases": bad})
    assert r.status_code == 400 and "after the horizon" in r.json()["error"] and "'gold'" in r.json()["error"]
    assert counts(svc) == before
    # A version of another study cannot be edited from this one.
    other_v = _bundle(c, st["other"])["selected_context"]["version_id"]
    assert c.post(f"/v3/studies/{st['auto']}/scenario-lab/preview", json={"from_version_id": other_v, "cases": cases}).status_code == 400


def test_save_version_keeps_the_assumptions_values_them_and_assesses_without_publishing(app):
    """Tests 9, 10 and 11: a saved version has its parent, anchor, dates and computed path; it is valued under its own
    cases; its assessment is research only (no decision, Discover observation, notice or forward record), and the
    official version and decision are untouched."""
    c, svc, st = app
    b = _bundle(c, st["auto"])
    official = b["selected_context"]["version_id"]
    official_decision = b["decision"]["id"]
    cases = _with_bullish(_assumptions(b), 9.0)
    before = counts(svc)
    notices = c.get("/v3/scenarios/notices").json()["notices"]
    r = c.post(f"/v3/studies/{st['auto']}/scenario-lab/versions", json={"from_version_id": official, "cases": cases})
    assert r.status_code == 200, r.text
    out = r.json()
    ver, a = out["version"], out["assessment"]
    after = counts(svc)
    assert after["scenario_versions"] == before["scenario_versions"] + 1 and after["scenario_assessments"] == before["scenario_assessments"] + 1
    for t in ("scenario_decisions", "opportunity_observations", "opportunity_candidates", "notifications", "scenario_forecasts", "scenario_runs"):
        assert after[t] == before[t], t
    assert c.get("/v3/scenarios/notices").json()["notices"] == notices
    assert ver["origin"] == "manual" and ver["parent_version"] == 1 and ver["anchor"] == b["anchor"]
    bull = next(x for x in ver["cases"] if x["key"] == "bullish")
    att = bull["attribution"]
    assert att["issuer_pct"][-1] == 9.0
    assert bull["terminal"]["return_pct"] == pytest.approx(b["base"]["path_pct"][-1] + att["factor_pct"][-1] + 9.0, abs=1e-6)
    assert a["research_only"] and a["exploratory"] and a["status"] in ("qualified", "watch", "no_setup") and "Research only" in a["note"]
    assert a["label"].endswith("under these assumptions")
    # The study's official record is unchanged.
    p = svc.pipeline.load(st["auto"])
    assert p["version_id"] == official and p["decision"]["id"] == official_decision
    # Selected now: the new version, with its own valuation and assessment, never the official decision as its own.
    nb = _bundle(c, st["auto"])
    assert nb["selected_context"]["version_id"] == ver["id"] and nb["selected_context"]["mode"] == "exploratory"
    assert nb["valuation"]["version_id"] == ver["id"] and nb["decision"]["kind"] == "exploratory" and nb["decision"]["version_id"] == ver["id"]
    assert "belongs to v1" in nb["decision_note"]
    assert [v["kind"] for v in nb["versions"]] == ["official", "exploratory"] and "never enters the forward scorecard" in nb["versions"][1]["grading"]
    assert nb["versions"][1]["parent_id"] == official
    # Back to the official version: its own valuation and decision again.
    ob = _bundle(c, st["auto"], version=official)
    assert ob["valuation"]["version_id"] == official and ob["decision"]["id"] == official_decision and ob["decision"]["kind"] == "official"
    c.post(f"/v3/studies/{st['auto']}/presentation", json={"selected_version_id": official})


def test_one_compatible_version_compares_read_only(app):
    """Test 12: one earlier version beside the selected one, without changing the selected version's decision."""
    c, svc, st = app
    b = _bundle(c, st["auto"])
    ex = next(v for v in b["versions"] if v["kind"] == "exploratory")
    cb = _bundle(c, st["auto"], version=b["selected_context"]["version_id"], compare=ex["id"])
    cmp_ = cb["comparison"]
    assert cmp_["comparable"] is True and cmp_["label"] == ex["label"] and {x["key"] for x in cmp_["cases"]} == {"central", "bullish", "bearish"}
    assert cb["decision"]["id"] == b["decision"]["id"] and cb["valuation"]["id"] == b["valuation"]["id"]
    assert cb["selected_context"]["comparison_version_id"] == ex["id"]
    # Two versions of different anchors or sessions are not drawn together.
    v1 = svc.scenarios.version(b["selected_context"]["version_id"])
    moved = {**v1, "id": "x", "anchor": {**v1["anchor"], "price": v1["anchor"]["price"] + 1}}
    assert comparable(v1, moved) == (False, "it starts from another anchor price or date")
    assert comparable(v1, {**v1, "id": "y", "sessions": v1["sessions"][:-1] + [{"date": "2099-01-02"}]})[0] is False
    assert comparable(v1, v1)[0] is False


def test_the_decision_focus_is_only_the_case_the_selected_versions_decision_names(app):
    """Test 15: the chart may emphasise a case only for the decision of the version drawn."""
    c, svc, st = app
    b = _bundle(c, st["auto"])
    d = b["decision"]
    quantified = {x["key"] for x in b["selected_version"]["cases"] if x["quantified"]}
    if d["status"] in ("qualified", "watch") and (d["setup"] or {}).get("case") in quantified:
        assert d["focus_case"] == d["setup"]["case"]
    else:
        assert d["focus_case"] in (None, *quantified)
    ex = next(v for v in b["versions"] if v["kind"] == "exploratory")
    eb = _bundle(c, st["auto"], version=ex["id"])
    assert eb["decision"]["kind"] == "exploratory" and eb["decision"]["version_id"] == ex["id"]


# --- promotion -----------------------------------------------------------------------------------------------------------------

def test_only_a_rule_sized_edit_can_become_official_and_only_when_confirmed(app):
    c, svc, st = app
    sid = st["other"]
    b = _bundle(c, sid)
    official = b["selected_context"]["version_id"]
    stress = c.post(f"/v3/studies/{sid}/scenario-lab/versions", json={"from_version_id": official, "cases": _with_bullish(_assumptions(b), 5.0)}).json()
    chk = c.get(f"/v3/studies/{sid}/scenario-lab/promotion", params={"version_id": stress["version"]["id"]}).json()
    assert chk["allowed"] is False and any("user stress" in x for x in chk["problems"])
    assert c.post(f"/v3/studies/{sid}/scenario-lab/promote", json={"version_id": stress["version"]["id"], "confirm": True}).status_code == 400
    # The automatic version's own rule-sized assumptions, saved as an edit: it meets the automatic run's standard.
    same = c.post(f"/v3/studies/{sid}/scenario-lab/versions", json={"from_version_id": official, "cases": _assumptions(b)}).json()
    vid = same["version"]["id"]
    chk = c.get(f"/v3/studies/{sid}/scenario-lab/promotion", params={"version_id": vid}).json()
    assert chk["allowed"] is True, chk["problems"]
    unconfirmed = c.post(f"/v3/studies/{sid}/scenario-lab/promote", json={"version_id": vid}).json()
    assert unconfirmed["pipeline"] is None and svc.pipeline.load(sid)["version_id"] == official
    forward = svc.db.one("SELECT COUNT(*) AS n FROM scenario_forecasts WHERE study_id = ?", (sid,))["n"]
    r = c.post(f"/v3/studies/{sid}/scenario-lab/promote", json={"version_id": vid, "confirm": True})
    assert r.status_code == 200, r.text
    p = svc.pipeline.load(sid)
    assert p["version_id"] == vid and p["promotions"][-1] == {**p["promotions"][-1], "version_id": vid, "replaced": official}
    d = svc.opportunities.latest(sid)
    assert d["version_id"] == vid and d["valuation_id"] == p["valuation_id"]
    assert svc.db.one("SELECT COUNT(*) AS n FROM scenario_forecasts WHERE study_id = ?", (sid,))["n"] == forward     # never scored
    nb = _bundle(c, sid)
    kinds = {v["id"]: v["kind"] for v in nb["versions"]}
    assert kinds[vid] == "promoted" and kinds[official] == "previous" and nb["selected_context"]["is_official"]
    assert nb["decision"]["kind"] == "official" and nb["decision"]["current"]
    old = _bundle(c, sid, version=official)
    assert old["decision"] is None or (old["decision"]["current"] is False and "superseded" in (old["decision_note"] or ""))


def test_promotion_rules_follow_the_automatic_runs_standard():
    cases = {"bullish": {"drivers": [{"type": "issuer_event", "direction": "up", "source_ids": ["S1"]},
                                     {"type": "market", "direction": "up", "factor": "market", "source_ids": ["S2"]}]}}
    ok = [{"key": "bullish", "issuer": {"value_pct": 3.0, "source": "analog_tail"}, "factors": [{"key": "market", "source": "factor_volatility"}],
           "dispersion": None}]
    assert promotion_problems({}, cases, ok) == []
    down = [{**ok[0], "issuer": {"value_pct": -3.0, "source": "analog_tail"}}]
    assert "no cited issuer driver of this case goes down" in promotion_problems({}, cases, down)[0]
    sector = [{**ok[0], "factors": [{"key": "sector", "source": "factor_volatility"}]}]
    assert "names the sector factor" in promotion_problems({}, cases, sector)[0]
    provider = [{**ok[0], "issuer": {"value_pct": 3.0, "source": "provider"}}]
    assert "provider" in promotion_problems({}, cases, provider)[0]
    widened = [{**ok[0], "dispersion": {"value_pct": 2.0, "source": "user_stress", "source_ids": []}}]
    assert len(promotion_problems({}, cases, widened)) == 2


# --- Trading Desk references ---------------------------------------------------------------------------------------------------

def test_the_draft_api_refuses_mismatched_study_version_valuation_or_decision(app):
    """Test 20."""
    c, svc, st = app
    sid = st["auto"]
    b = _bundle(c, sid)
    off, d, val = b["selected_context"]["version_id"], b["decision"], b["valuation"]
    ex = next(v for v in b["versions"] if v["kind"] == "exploratory")
    strat = "shares"

    def draft(scenario, study=sid):
        return c.post("/v3/drafts", json={"study_id": study, "strategy": strat, "scenario": scenario})

    ok = draft({"version_id": off, "valuation_id": val["id"], "decision_id": d["id"]})
    assert ok.status_code == 200, ok.text
    ref = ok.json()["source"]["scenario"]
    assert ref["official"] is True and ref["exploratory"] is False and ref["snapshot_at"] == val["snapshot_at"] and ref["decision_id"] == d["id"]
    # The decision with another version.
    r = draft({"version_id": ex["id"], "decision_id": d["id"]})
    assert r.status_code == 400 and "another scenario version" in r.json()["error"]
    # The decision with another valuation.
    other_val = svc.db.one("SELECT id FROM scenario_valuations WHERE version_id = ?", (ex["id"],))["id"]
    r = draft({"version_id": off, "valuation_id": other_val, "decision_id": d["id"]})
    assert r.status_code == 400 and "another scenario version" in r.json()["error"]         # the valuation is checked first
    # A research-only assessment is not a decision.
    asm = _bundle(c, sid, version=ex["id"])["decision"]["id"]
    r = draft({"version_id": ex["id"], "decision_id": asm})
    assert r.status_code == 400 and "exploratory assessment" in r.json()["error"]
    # Another study's version or decision.
    assert draft({"version_id": off}, study=st["other"]).status_code == 400
    # From an exploratory version: allowed, said to be research only.
    r = draft({"version_id": ex["id"]})
    assert r.status_code == 200 and r.json()["source"]["scenario"]["exploratory"] is True
    assert "exploratory scenario version" in r.json()["notes"][0]


def test_a_superseded_decision_is_not_carried_into_a_draft(app):
    c, svc, st = app
    sid = st["auto"]
    first = svc.opportunities.latest(sid)
    svc.pipeline.revalue(sid)                      # a fresh snapshot and the decision again: a later decision exists
    latest = svc.opportunities.latest(sid)
    assert latest["id"] != first["id"]
    r = c.post("/v3/drafts", json={"study_id": sid, "strategy": "shares", "scenario": {"version_id": first["version_id"], "decision_id": first["id"]}})
    assert r.status_code == 400 and "superseded" in r.json()["error"]
    r = c.post("/v3/drafts", json={"study_id": sid, "strategy": "shares", "scenario": {"version_id": latest["version_id"], "decision_id": latest["id"]}})
    assert r.status_code == 200, r.text


def test_an_expired_version_cannot_be_drafted(app, monkeypatch):
    c, svc, st = app
    b = _bundle(c, st["auto"])
    real = svc.scenarios.version

    def stale(vid):
        v = real(vid)
        return {**v, "stale": True}
    monkeypatch.setattr(svc.scenarios, "version", stale)
    r = c.post("/v3/drafts", json={"study_id": st["auto"], "strategy": "shares", "scenario": {"version_id": b["selected_context"]["version_id"]}})
    assert r.status_code == 400 and "has passed" in r.json()["error"]


def test_the_migration_adds_only_tables(tmp_path):
    db = Database(tmp_path / "m.db")
    cols = {r["name"] for r in db.query("PRAGMA table_info(scenario_assessments)")}
    assert {"id", "study_id", "version_id", "valuation_id", "status", "assessment_json"} <= cols
    assert {r["name"] for r in db.query("PRAGMA table_info(study_presentation)")} == {"study_id", "state_json", "updated_at"}
    db.close()


_ = replace
