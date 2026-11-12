"""4.5 forward evidence: every scenario study's base, no change, single-prompt control and provider forecasts frozen
before the window opens (a late one is kept and never scored), graded on the target close from the frozen anchor, a
conditional case scored only when its trigger was observed, and the variants compared paired on the same studies."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pandas as pd
import pytest
from fastapi.testclient import TestClient

from tradeassist.api import create_app
from tradeassist.config import Settings
from tradeassist.db import Database
from tradeassist.optionmath import NY
from tradeassist.providers.demo import DemoData
from tradeassist.service import TradeAssistant
from tradeassist.v3.forward import MIN_PAIRED, point_of, window_start
from tradeassist.v3.scenario_providers import Demo, Step
from tests.test_one_click import _cases, _wait, drain


class Control:
    """The single-prompt control, stubbed: canned cases at once, with a model and token use."""
    name, label = "ai", "Single AI call"

    def __init__(self, fail=False):
        self.fail = fail

    def config(self):
        return {"provider": "ai"}

    def ready(self):
        return {"ok": True, "detail": "ok"}

    def advance(self, ctx, state):
        if self.fail:
            return Step(state, failed="the model was unavailable", stage="report")
        return Step({"stage": "done"}, report="control", cases=_cases(drop_seed=True), stage="report", model="claude-test",
                    usage={"input_tokens": 1200, "output_tokens": 300})


@pytest.fixture(scope="module")
def app(tmp_path_factory):
    import tradeassist.api as api_mod
    api_mod.TEST_HOSTS = ("testserver",)
    path = tmp_path_factory.mktemp("fwd") / "t.db"
    s = Settings(data_mode="demo", db_path=path, desk_worker="off", scenario_provider="demo", scenario_auto=True, scenario_control=True)
    svc = TradeAssistant(s, provider=DemoData(), db=Database(path))
    control = {"stub": Control()}
    svc.scenario_provider_override = lambda name: control["stub"] if name == "ai" else Demo()
    c = TestClient(create_app(s, svc))
    one = _wait(c, c.post("/v3/studies", json={"symbol": "AAPL", "horizon_days": 5}).json()["id"])
    drain(svc)
    yield c, svc, one, control
    svc.db.close()


def _rows(svc, sid):
    return {r["variant"]: dict(r) for r in svc.db.query("SELECT * FROM scenario_forecasts WHERE study_id = ?", (sid,))}


def test_catalyst_mean_1_is_the_cases_mean_and_their_outer_band():
    v = {"cases": [{"key": "central", "return_pct": [0.0, 1.0], "low_pct": [-1, -2.0], "high_pct": [1, 3.0], "quantified": False},
                   {"key": "bullish", "return_pct": [2.0, 4.0], "low_pct": [0, 1.0], "high_pct": [3, 7.0], "quantified": True},
                   {"key": "bearish", "return_pct": [-2.0, -2.0], "low_pct": [-4, -6.0], "high_pct": [0, 1.0], "quantified": True}]}
    pt = point_of(v)
    assert pt["median_pct"] == pytest.approx(1.0) and pt["low_pct"] == -6.0 and pt["high_pct"] == 7.0 and len(pt["cases"]) == 3
    assert window_start([{"date": "2026-10-23"}]) == datetime(2026, 10, 23, 9, 30, tzinfo=NY)


def test_each_variant_is_frozen_before_the_window_and_the_control_stays_apart(app):
    c, svc, st, _ = app
    sid = st["id"]
    rows = _rows(svc, sid)
    assert set(rows) == {"base", "no_change", "provider", "control"}
    assert all(r["status"] == "frozen" for r in rows.values())               # the suite's clock: Thursday after the close
    assert all(r["window_start"].startswith(st["frame"]["sessions"][0]["date"]) for r in rows.values())
    assert rows["base"]["median_pct"] == pytest.approx(st["study"]["forecast"]["base"]["path_pct"][-1]) and rows["no_change"]["median_pct"] == 0.0
    assert rows["no_change"]["low_pct"] == rows["base"]["low_pct"] and rows["base"]["anchor_price"] == st["frame"]["anchor"]["price"]
    assert rows["provider"]["rule"] == rows["control"]["rule"] == "catalyst-mean-1"
    assert json.loads(rows["control"]["cost_json"])["usage"]["input_tokens"] == 1200 and rows["control"]["latency_seconds"] is not None
    # The control has its own version; the study's paths, valuation and decision are the provider's.
    p = c.get(f"/v3/studies/{sid}/pipeline").json()["pipeline"]
    assert p["status"] == "complete" and rows["provider"]["version_id"] == p["version_id"]
    cv = svc.scenarios.version(rows["control"]["version_id"])
    assert cv["origin"] == "control" and cv["id"] != p["version_id"]
    assert p["valuation"]["version_id"] == p["version_id"]
    assert {f["variant"] for f in p["forward"]} == {"base", "no_change", "provider", "control"}


def test_a_late_forecast_is_kept_but_never_replaces_or_is_scored(app):
    c, svc, st, _ = app
    sid = st["id"]
    fw = svc.forward
    before = _rows(svc, sid)["provider"]
    late = datetime.fromisoformat(before["window_start"]) + timedelta(minutes=5)
    v = svc.scenarios.version(before["version_id"])
    run = svc.db.one("SELECT * FROM scenario_runs WHERE id = ?", (before["run_id"],))
    assert fw.freeze_version(sid, v, run, "provider", now=late) == "frozen"          # the frozen one stands
    assert _rows(svc, sid)["provider"]["made_at"] == before["made_at"]
    # A study whose provider answered after the window opened: late.
    with svc.db.tx() as cur:
        cur.execute("DELETE FROM scenario_forecasts WHERE study_id = ? AND variant = 'provider'", (sid,))
    assert fw.freeze_version(sid, v, run, "provider", now=late) == "late"
    assert _rows(svc, sid)["provider"]["status"] == "late"
    # Back in time again (a retry before the window): frozen.
    assert fw.freeze_version(sid, v, run, "provider", now=late - timedelta(hours=1)) == "frozen"


def test_grading_scores_the_variants_and_a_conditional_case_only_when_its_trigger_was_seen(app, monkeypatch):
    c, svc, st, _ = app
    sid = st["id"]
    rows = _rows(svc, sid)
    anchor, target = rows["base"]["anchor_price"], rows["base"]["target_date"]
    realized = 3.0
    with svc.db.tx() as cur:
        # 4.5.1: only a verified trigger, recorded before the target session closed, scores its case. The bearish one was
        # recorded after the outcome was known, so it is not scored even though it was verified.
        cur.execute("INSERT INTO scenario_triggers (study_id, case_key, observed_at, recorded_at, source_json, status) VALUES (?, 'bullish', ?, ?, ?, 'verified')",
                    (sid, "2026-10-23T12:00:00+00:00", "2026-10-23T12:05:00+00:00", json.dumps({"title": "Company release"})))
        cur.execute("INSERT INTO scenario_triggers (study_id, case_key, observed_at, recorded_at, source_json, status) VALUES (?, 'bearish', ?, ?, ?, 'verified')",
                    (sid, "2026-10-23T12:00:00+00:00", f"{target}T23:00:00+00:00", json.dumps({"title": "Hindsight"})))
        cur.execute("UPDATE scenario_forecasts SET status = 'late' WHERE study_id = ? AND variant = 'control'", (sid,))
    bars = pd.DataFrame({"date": pd.to_datetime([rows["base"]["anchor_date"], target]), "open": [anchor, anchor], "high": [anchor, anchor * 1.05],
                         "low": [anchor, anchor], "close": [anchor, anchor * (1 + realized / 100)], "volume": [1e6, 1e6]})
    monkeypatch.setattr(svc.market, "bars", lambda sym, years=1.0, complete=True: bars)
    after = datetime.fromisoformat(target).replace(hour=20, tzinfo=NY).timestamp()
    out = svc.forward.grade(now=after)
    assert out["graded"] == 3                                                 # base, no change, provider; the late control is not
    g = _rows(svc, sid)
    for v in ("base", "no_change", "provider"):
        assert g[v]["status"] == "graded" and g[v]["realized_pct"] == pytest.approx(realized)
        assert g[v]["abs_error"] == pytest.approx(abs(realized - g[v]["median_pct"]))
    assert g["no_change"]["direction_hit"] is None and g["control"]["status"] == "late_graded" and g["control"]["abs_error"] is None
    scores = json.loads(g["provider"]["case_scores_json"])
    assert [s["key"] for s in scores] == ["bullish"] and scores[0]["source"] == "Company release"
    assert svc.forward.grade(now=after)["graded"] == 0                         # once each
    sc = c.get("/v3/scenarios/scorecard", params={"horizon": 5}).json()
    by = {v["variant"]: v for v in sc["variants"]}
    assert by["provider"]["n"] == by["base"]["n"] == by["no_change"]["n"] == 1 and by["control"]["n"] == 0 and by["control"]["late"] == 1
    pb = next(p for p in sc["pairs"] if p["a"] == "provider" and p["b"] == "base")
    assert pb["n"] == 1 and pb["mae_a"] == pytest.approx(g["provider"]["abs_error"])
    assert next(p for p in sc["pairs"] if p["a"] == "provider" and p["b"] == "control")["n"] == 0
    assert sc["cases"]["n"] == 1 and f"at least {MIN_PAIRED}" in sc["verdict"] and "no weight in the base" in sc["verdict"]


def test_a_failed_control_leaves_the_study_alone(app):
    c, svc, st, control = app
    control["stub"] = Control(fail=True)
    two = _wait(c, c.post("/v3/studies", json={"symbol": "MSFT", "horizon_days": 5}).json()["id"])
    drain(svc)
    p = c.get(f"/v3/studies/{two['id']}/pipeline").json()["pipeline"]
    assert p["status"] == "complete" and p["error"] is None
    runs = {json.loads(r["external_json"]).get("role", "provider"): r["status"]
            for r in svc.db.query("SELECT status, external_json FROM scenario_runs WHERE study_id = ?", (two["id"],))}
    assert runs == {"provider": "complete", "control": "failed"}
    assert set(_rows(svc, two["id"])) == {"base", "no_change", "provider"}
    control["stub"] = Control()


def test_forecasts_whose_target_passed_are_graded_soon_after_start(tmp_path):
    path = tmp_path / "t.db"
    s = Settings(data_mode="demo", db_path=path, desk_worker="off")
    svc = TradeAssistant(s, provider=DemoData(), db=Database(path))
    create_app(s, svc)
    with svc.db.tx() as cur:
        cur.execute("INSERT INTO studies (id, mode, symbol, created_at, as_of, horizon_days, thesis, status, engine_version, contract_version, "
                    "inputs_json, evidence_json, fingerprint) VALUES ('study_old', 'demo', 'KO', '2026-01-02', '2026-01-02', 5, 'swing', 'complete', "
                    "'x', 'x', '{}', '{}', '')")
        cur.execute("""INSERT INTO scenario_forecasts (id, study_id, mode, symbol, horizon, variant, made_at, window_start, target_date, anchor_price,
                       anchor_date, median_pct, low_pct, high_pct, status) VALUES ('sfc_old', 'study_old', 'demo', 'KO', 5, 'base', '2026-01-02',
                       '2026-01-05T09:30:00-05:00', '2026-01-09', 60, '2026-01-02', 0.5, -3, 3, 'frozen')""")
    svc2 = TradeAssistant(s, provider=DemoData(), db=Database(path))
    create_app(s, svc2)
    assert svc2.db.one("SELECT COUNT(*) AS n FROM jobs WHERE kind = 'scenario.grade' AND status = 'queued'")["n"] == 1
    svc.db.close()
    svc2.db.close()
    _ = timezone


def test_the_smoke_check_reports_what_is_ready(tmp_path, capsys):
    import run
    s = Settings(data_mode="demo", db_path=tmp_path / "t.db", scenario_provider="demo")
    assert run.scenario_check(s, "SPY") == 0
    out = capsys.readouterr().out
    assert "Scenario provider: demo" in out and "OK:" in out
    s = Settings(data_mode="demo", db_path=tmp_path / "t2.db", scenario_provider="mirofish")
    assert run.scenario_check(s, "SPY") == 1 and "MIROFISH_URL is not set" in capsys.readouterr().out
