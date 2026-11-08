"""4.0 phases 4 and 5 through the app: starting, stopping and resuming replays from the screen (as their own process),
deploying and switching models, the shadow routes, and the holdout, deploy, export and import commands."""

import json
from datetime import date, datetime

import pandas as pd
import pytest
from fastapi.testclient import TestClient

from tests import v4_fixtures as fx
from tradeassist.api import create_app
from tradeassist.config import Settings
from tradeassist.db import Database
from tradeassist.intelligence.data import dataset as DS
from tradeassist.intelligence.live import shadow_step
from tradeassist.intelligence.replay.runner import Replay, create_run
from tradeassist.optionmath import NY
from tradeassist.providers.demo import DemoData
from tradeassist.service import TradeAssistant
from tradeassist.v4 import replay_store, runs


@pytest.fixture
def app(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(runs, "SPAWN", lambda args, log: calls.append(args))
    settings = Settings(data_mode="demo", db_path=tmp_path / "t.db")
    svc = TradeAssistant(settings, provider=DemoData(), db=Database(tmp_path / "t.db"))
    return svc, TestClient(create_app(settings, svc)), calls


def _dataset(svc, last="2026-03-31"):
    bars = fx.calendar_market(first="2022-01-03", last=last)
    return bars, DS.freeze(replay_store(svc), bars, fx.universe(), source="test", adjustment="none")


def test_the_screen_starts_a_replay_as_its_own_process_and_stopping_pauses_it(app):
    svc, client, calls = app
    _, ds = _dataset(svc)
    r = client.post("/v4/replay/runs", json={"start": "2023-03-01", "end": "2024-06-28", "families": ["ridge"], "retrain": "monthly"})
    assert r.status_code == 200, r.text
    run_id = r.json()["run"]
    assert calls == [["resume", run_id]]                                       # handed to `python run.py --replay resume RUN`
    assert client.post(f"/v4/replay/runs/{run_id}/start").status_code == 400   # a second click before it starts: nothing more
    assert calls == [["resume", run_id]]
    store = replay_store(svc)
    assert store.one("SELECT status FROM replay_runs WHERE id = ?", (run_id,))["status"] == "queued"
    assert client.post("/v4/replay/runs", json={"end": "2025-12-31"}).status_code == 400   # into the held-out year: refused
    assert client.post("/v4/replay/runs", json={"families": ["oracle"]}).status_code == 400
    # The child process would run it; here it runs inline, and a stop asked from the screen pauses it between sessions.
    replay = Replay(store, run_id)
    done = []
    replay.progress = lambda p: (done.append(p), client.post(f"/v4/replay/runs/{run_id}/stop") if len(done) == 40 else None)
    out = replay.run()
    assert out["status"] == "paused" and len(done) == 40
    st = client.get(f"/v4/replay/runs/{run_id}").json()
    assert st["status"] == "paused" and st["checkpoint"] and st["stop_requested_at"]
    assert client.post(f"/v4/replay/runs/{run_id}/start").status_code == 200 and calls[-1] == ["resume", run_id]
    assert client.get(f"/v4/replay/runs/{run_id}").json()["stop_requested_at"] is None   # resuming clears the request
    finished = Replay(store, run_id).run()
    assert finished["status"] == "complete"
    assert client.post(f"/v4/replay/runs/{run_id}/start").status_code == 400            # a complete run cannot be restarted
    assert client.post("/v4/replay/datasets/build").json()["started"] and calls[-1] == ["dataset", "build"]
    curve = client.get(f"/v4/replay/runs/{run_id}/curve?horizon=1").json()["months"]
    assert curve and {"month", "gain_bp", "coverage", "family"} <= set(curve[0])
    errs = client.get(f"/v4/replay/runs/{run_id}/errors?horizon=5").json()
    assert errs["largest"] and errs["by_regime"]
    assert client.get(f"/v4/replay/runs/{run_id}/symbols/aapl").json()["daily"]["1"]["graded"] > 0


def test_a_run_whose_heartbeat_stopped_is_shown_as_interrupted(app):
    svc, client, _ = app
    _, ds = _dataset(svc, last="2024-12-31")
    store = replay_store(svc)
    run_id = create_run(store, ds.id, fx.config(date(2023, 3, 1), date(2024, 6, 28), families=("ridge",)))
    with store.tx() as cur:
        cur.execute("UPDATE replay_runs SET status = 'running', heartbeat_at = '2026-01-01T00:00:00+00:00' WHERE id = ?", (run_id,))
    listed = client.get("/v4/replay/runs").json()["runs"][0]
    assert listed["interrupted"] is True
    assert client.post(f"/v4/replay/runs/{run_id}/start").status_code == 200        # it can be resumed


def test_deploying_switching_and_the_shadow_routes(app):
    svc, client, calls = app
    bars, ds = _dataset(svc)
    store = replay_store(svc)
    assert client.get("/v4/shadow").json()["deployment"] is None
    run_id = create_run(store, ds.id, fx.config(date(2023, 3, 1), date(2025, 10, 31), families=("ridge",), retrain="monthly"))
    Replay(store, run_id).run()
    dep = client.post("/v4/models/deploy", json={"run": run_id}).json()["deployment"]
    assert client.post("/v4/models/deploy", json={"run": "nope"}).status_code == 400
    listing = client.get("/v4/models/deployments").json()["deployments"]
    assert listing[0]["id"] == dep and listing[0]["active"]

    def fetch(sym, start, end):
        df = bars[sym]
        return df[(df["date"] >= pd.Timestamp(start)) & (df["date"] <= pd.Timestamp(end))]
    for d in (date(2026, 3, 27), date(2026, 3, 30)):
        shadow_step(store, fetch, d, datetime(d.year, d.month, d.day, 17, 30, tzinfo=NY))
    sh = client.get("/v4/shadow").json()
    assert sh["deployment"]["id"] == dep and sh["last_session"] == "2026-03-30"
    assert sh["validation"]["status"] == "replayed" and sh["record"]["daily"]["1"]["graded"] == len(fx.STOCKS)
    latest = client.get("/v4/shadow/latest", params={"symbol": "msft"}).json()
    assert {p["horizon"] for p in latest["predictions"] if p["task"] == "daily"} == {1, 5} and latest["replay_record"]["daily"]
    opp = client.get("/v4/shadow/opportunities").json()
    assert opp["session"] == "2026-03-30" and all(r["horizon"] == 1 or r["task"] == "daily" for r in opp["leaning"])
    why = client.get("/v4/shadow/why", params={"symbol": "MSFT", "horizon": 1}).json()
    assert why["from"] == "2026-03-27" and why["to"] == "2026-03-30" and why["inputs"]
    # Switching back to an earlier deployment
    other = create_run(store, ds.id, fx.config(date(2023, 3, 1), date(2025, 6, 30), families=("drift",), retrain="monthly"))
    Replay(store, other).run()
    dep2 = client.post("/v4/models/deploy", json={"run": other}).json()["deployment"]
    assert client.get("/v4/shadow").json()["deployment"]["id"] == dep2
    assert client.post(f"/v4/models/deployments/{dep}/activate").json()["active"] == dep
    assert client.get("/v4/shadow").json()["deployment"]["id"] == dep
    assert client.get("/v4/replay/holdout").json()["openings"] == []


def test_the_holdout_deploy_export_and_import_commands(tmp_path, capsys):
    from tradeassist.intelligence.store import ReplayStore
    from tradeassist.v4 import cli
    settings = Settings(data_mode="demo", db_path=tmp_path / "a" / "t.db")
    (tmp_path / "a").mkdir()
    store = ReplayStore(tmp_path / "a" / "replay.db")
    ds = DS.freeze(store, fx.calendar_market(first="2022-01-03", last="2026-02-27"), fx.universe(), source="test", adjustment="none")
    run_id = create_run(store, ds.id, fx.config(date(2023, 3, 1), date(2025, 10, 31), families=("ridge",), retrain="monthly"))
    Replay(store, run_id).run()
    store.close()
    assert cli.main(settings, ["holdout", run_id]) == 0
    out = capsys.readouterr().out
    assert "first time" in out and "Ledger hash" in out
    assert cli.main(settings, ["holdout", run_id]) == 1 and "already opened" in capsys.readouterr().out
    assert cli.main(settings, ["deploy", run_id]) == 0
    dep = capsys.readouterr().out.split(" as ")[1].split(" ")[0]
    assert cli.main(settings, ["deployments"]) == 0 and dep in capsys.readouterr().out
    assert cli.main(settings, ["export", dep, str(tmp_path / "m.json")]) == 0
    capsys.readouterr()
    other = Settings(data_mode="demo", db_path=tmp_path / "b" / "t.db")
    (tmp_path / "b").mkdir()
    assert cli.main(other, ["import", str(tmp_path / "m.json")]) == 0
    assert "the file is intact" in capsys.readouterr().out
    assert json.loads((tmp_path / "m.json").read_text())["format"] == "tradeassist-models/1"
