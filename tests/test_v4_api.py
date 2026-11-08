"""4.0 in the app: the /v4 routes, grading Research's live forecasts, the command line, the job hook."""

import json
from datetime import date, timedelta

import pandas as pd
import pytest
from fastapi.testclient import TestClient

from tests import v4_fixtures as fx
from tradeassist.api import create_app
from tradeassist.config import Settings
from tradeassist.db import Database
from tradeassist.intelligence.data import dataset as DS
from tradeassist.intelligence.data.universe import LARGE_CAPS_2026
from tradeassist.intelligence.replay.runner import Replay, create_run
from tradeassist.providers.demo import DemoData
from tradeassist.service import TradeAssistant
from tradeassist.v4 import replay_store
from tradeassist.v4.grading import grade_research, research_record, target_session


@pytest.fixture
def app(tmp_path):
    settings = Settings(data_mode="demo", db_path=tmp_path / "t.db")
    svc = TradeAssistant(settings, provider=DemoData(), db=Database(tmp_path / "t.db"))
    return svc, TestClient(create_app(settings, svc))


def test_the_engines_stock_list_is_the_scanners():
    from tradeassist.scanner import LARGE_CAPS
    assert LARGE_CAPS_2026 == tuple(LARGE_CAPS)


def test_replay_db_is_its_own_file_beside_the_trading_database(app, tmp_path):
    svc, client = app
    store = replay_store(svc)
    assert store.path == str(tmp_path / "replay.db")
    tables = {r[0] for r in svc.db.query("SELECT name FROM sqlite_master WHERE type = 'table'")}
    assert "predictions" not in tables and "datasets" not in tables          # nothing of the replay in the trading database
    assert client.get("/v4/replay/runs").json() == {"runs": []}
    assert client.get("/v4/replay/datasets").json() == {"datasets": []}


def test_routes_serve_a_finished_run(app):
    svc, client = app
    store = replay_store(svc)
    ds = DS.freeze(store, fx.market(n=520), fx.universe(), source="test", adjustment="none")
    s = ds.calendar.sessions
    run_id = create_run(store, ds.id, fx.config(s[280], s[500], families=("ridge",)))
    Replay(store, run_id).run()
    runs = client.get("/v4/replay/runs").json()["runs"]
    assert runs[0]["id"] == run_id and runs[0]["status"] == "complete" and runs[0]["headline"]
    r = client.get(f"/v4/replay/runs/{run_id}").json()
    assert r["predictions"] > 1000 and r["graded"] > 1000 and r["config"]["horizons"] == [1, 5]
    card = client.get(f"/v4/replay/runs/{run_id}/scorecard").json()
    assert card["daily"]["1"]["graded"] > 0 and "headline" in card
    preds = client.get(f"/v4/replay/runs/{run_id}/predictions", params={"symbol": "aapl", "task": "daily", "horizon": 1, "limit": 5}).json()
    assert len(preds["predictions"]) == 5 and {p["symbol"] for p in preds["predictions"]} == {"AAPL"}
    lineage = client.get(f"/v4/replay/runs/{run_id}/lineage").json()["training"]
    assert lineage and all(isinstance(t["candidates"], dict) for t in lineage)
    assert client.get("/v4/replay/runs/nope/lineage").status_code == 404
    assert client.get("/v4/replay/runs/nope/predictions").status_code == 404
    checks = client.get(f"/v4/replay/datasets/{ds.id}/checks").json()
    assert checks["dataset"] == ds.id
    assert client.get("/v4/replay/runs/nope").status_code == 404
    assert client.get("/v4/replay/datasets/nope/checks").status_code == 404


def test_v4_reads_refuse_another_sites_page(app):
    _, client = app
    r = client.get("/v4/replay/runs", headers={"sec-fetch-site": "cross-site"})
    assert r.status_code == 403


def test_research_forecasts_are_graded_once_on_the_exchange_calendar(app):
    svc, client = app
    bars = svc.market.bars("AAPL", years=1.0, complete=True)
    dates = [d.date() for d in pd.to_datetime(bars["date"])]
    as_of = dates[-12]
    closes = dict(zip(dates, bars["close"].astype(float)))
    target = target_session(as_of, 5)
    with svc.db.tx() as cur:
        for h, (med, lo, hi) in ((5, (1.0, -3.0, 4.0)), (1, (0.0, -1.0, 1.0))):
            cur.execute("""INSERT INTO forecast_records (mode, study_id, symbol, model_version, kind, made_at, as_of_date, horizon_days,
                           target_date, regime, median_pct, low_pct, high_pct) VALUES (?, ?, 'AAPL', 'forecast-3.0', 'live', ?, ?, ?, ?, '', ?, ?, ?)""",
                        (svc.market.info.mode, f"s{h}", as_of.isoformat(), as_of.isoformat(), h, (as_of + timedelta(days=h)).isoformat(), med, lo, hi))
        cur.execute("""INSERT INTO forecast_records (mode, study_id, symbol, model_version, kind, made_at, as_of_date, horizon_days,
                       target_date, regime, median_pct, low_pct, high_pct) VALUES (?, 'fresh', 'AAPL', 'forecast-3.0', 'live', ?, ?, 20, ?, '', 0, -5, 5)""",
                    (svc.market.info.mode, dates[-1].isoformat(), dates[-1].isoformat(), dates[-1].isoformat()))
    got = grade_research(svc)
    assert got["graded"] == 2 and got["waiting"] == 1
    r5 = svc.db.one("SELECT * FROM forecast_records WHERE study_id = 's5'")
    expected = 100 * (closes[target] / closes[as_of] - 1)
    assert r5["realized_pct"] == pytest.approx(expected)
    assert r5["covered"] == int(-3.0 <= expected <= 4.0)
    assert r5["direction_hit"] == int(expected > 0)
    assert svc.db.one("SELECT direction_hit FROM forecast_records WHERE study_id = 's1'")["direction_hit"] is None   # a flat median leans nowhere
    first = r5["evaluated_at"]
    assert grade_research(svc)["graded"] == 0                                   # graded once, never again
    assert svc.db.one("SELECT evaluated_at FROM forecast_records WHERE study_id = 's5'")["evaluated_at"] == first
    rec = research_record(svc)
    assert rec["5"][0]["n"] == 1 and client.get("/v4/research/record").json()["by_horizon"]["1"][0]["n"] == 1


def _record(svc, study, as_of, h, med=0.5, lo=-2.0, hi=2.0):
    with svc.db.tx() as cur:
        cur.execute("""INSERT INTO forecast_records (mode, study_id, symbol, model_version, kind, made_at, as_of_date, horizon_days,
                       target_date, regime, median_pct, low_pct, high_pct) VALUES (?, ?, 'AAPL', 'forecast-3.0', 'live', ?, ?, ?, ?, '', ?, ?, ?)""",
                    (svc.market.info.mode, study, as_of.isoformat(), as_of.isoformat(), h, as_of.isoformat(), med, lo, hi))


def test_grading_works_through_a_backlog_and_closes_out_what_can_never_be_graded(app):
    svc, client = app
    dates = [d.date() for d in pd.to_datetime(svc.market.bars("AAPL", years=1.0, complete=True)["date"])]
    for i, k in enumerate((-40, -35, -30)):
        _record(svc, f"old{i}", dates[k], 1)
    saturday = next(d for d in (dates[-60] + timedelta(days=j) for j in range(7)) if d.weekday() == 5)
    _record(svc, "weekend", saturday, 1)                          # no bar on its as-of date, ever
    first = grade_research(svc, limit=2)                          # the oldest first, the rest left for the next run
    assert first["graded"] == 1 and first["closed_without_data"] == 1 and first["left_for_next_run"] == 2
    gone = svc.db.one("SELECT evaluated_at, realized_pct FROM forecast_records WHERE study_id = 'weekend'")
    assert gone["evaluated_at"] and gone["realized_pct"] is None
    assert client.post("/v4/research/grade").json()["graded"] == 2      # or now, on request
    assert research_record(svc)["1"][0]["n"] == 3                 # the closed-out record is not counted as a forecast


def test_the_grading_job_leaves_market_hours_to_the_orders(app, monkeypatch):
    svc, _ = app
    from tradeassist import marketdays
    from tradeassist.v4 import jobs
    handlers, _ = jobs(svc)
    monkeypatch.setattr(marketdays, "phase", lambda now=None: "open")
    assert handlers["research.grade"]({}) == {"skipped": "the market is open"}
    monkeypatch.setattr(marketdays, "phase", lambda now=None: "post")
    assert "graded" in handlers["research.grade"]({})


def test_target_sessions_skip_market_holidays():
    assert target_session(date(2026, 11, 25), 1) == date(2026, 11, 27)
    assert target_session(date(2026, 12, 24), 1) == date(2026, 12, 28)


def test_the_grading_job_runs_in_whichever_worker_holds_the_queue(app):
    svc, _ = app
    worker = svc.trading.make_worker("test-worker", None)
    assert "research.grade" in worker.handlers
    assert any(s.kind == "research.grade" for s in worker.schedules)
    from tradeassist.daemon import handlers_for
    handlers, _ = handlers_for(svc)
    daemon_worker = svc.trading.make_worker("daemon", None, handlers, [])
    assert "research.grade" in daemon_worker.handlers and "desk.submit" in daemon_worker.handlers


def test_the_command_line_builds_runs_and_reports(tmp_path, capsys, monkeypatch):
    from tradeassist.v4 import cli
    settings = Settings(data_mode="demo", db_path=tmp_path / "t.db")
    store_path = tmp_path / "replay.db"
    from tradeassist.intelligence.store import ReplayStore
    store = ReplayStore(store_path)
    DS.freeze(store, fx.market(n=520), fx.universe(), source="test", adjustment="none")
    store.close()
    s = pd.bdate_range("2018-01-02", periods=520)
    assert cli.main(settings, ["dataset", "list"]) == 0
    assert "test-10" in capsys.readouterr().out
    code = cli.main(settings, ["run", "--universe", "test-10", "--start", str(s[300].date()), "--end", str(s[330].date()),
                               "--families", "ridge", "--retrain", "never"])
    out = capsys.readouterr().out
    assert code == 0 and "Ledger hash" in out and "1-day" in out
    assert cli.main(settings, ["runs"]) == 0
    run_id = capsys.readouterr().out.split()[0]
    assert cli.main(settings, ["report", run_id, "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["run"] == run_id
    assert cli.main(settings, ["run", "--universe", "test-10", "--end", "2025-12-01"]) == 1
    assert "held out" in capsys.readouterr().out
