"""4.0 phase 4: deployments, the held-out year opened once, and the daily shadow forecasts.

The shadow forecast runs the replay's own feature and prediction code on live bars: for the same session, a stock's
inputs hash the same in both. A session is forecast once, before its next open or not at all, and graded once its
window has closed.
"""

import json
from datetime import date, datetime

import pandas as pd
import pytest

from tests import v4_fixtures as fx
from tradeassist import marketdays
from tradeassist.intelligence.data import dataset as DS
from tradeassist.intelligence.data.features import FACTORS, FEATURES, build_panel
from tradeassist.intelligence.learning import deploy as DEP
from tradeassist.intelligence.learning.ledger import feature_hash
from tradeassist.intelligence.live import live_dataset, shadow_step
from tradeassist.intelligence.replay.holdout import open_holdout
from tradeassist.intelligence.replay.runner import Replay, ReplayError, create_run
from tradeassist.intelligence.store import ReplayStore
from tradeassist.optionmath import NY

DEV_START, DEV_END = date(2023, 3, 1), date(2025, 10, 31)


@pytest.fixture(scope="module")
def world(tmp_path_factory):
    """A frozen dataset to 31 March 2026, a finished development run, and its deployment."""
    bars = fx.calendar_market()
    store = ReplayStore(tmp_path_factory.mktemp("rp") / "replay.db")
    ds = DS.freeze(store, bars, fx.universe(), source="test", adjustment="none")
    assert not ds.has_failures()
    cfg = fx.config(DEV_START, DEV_END, families=("ridge",), retrain="monthly")
    run_id = create_run(store, ds.id, cfg)
    Replay(store, run_id).run()
    dep = DEP.deploy(store, run_id, "test")
    return {"bars": bars, "store": store, "ds": ds, "run": run_id, "dep": dep, "cfg": cfg}


def _fetch(bars):
    def fetch(sym, start, end):
        df = bars.get(sym)
        if df is None:
            raise RuntimeError("no such symbol")
        return df[(df["date"] >= pd.Timestamp(start)) & (df["date"] <= pd.Timestamp(end))]
    return fetch


def _evening(d: date, hour: int = 17, minute: int = 30) -> datetime:
    return datetime(d.year, d.month, d.day, hour, minute, tzinfo=NY)


# ---- deployments ----------------------------------------------------------------------------------------------------

def test_a_deployment_carries_the_runs_final_models_and_travels_between_computers_intact(world, tmp_path):
    store = world["store"]
    dep = DEP.active(store)
    assert dep["id"] == world["dep"] and dep["manifest"]["source_run"] == world["run"]
    assert {(m["task"], m["horizon"]) for m in dep["manifest"]["models"]} == {("daily", 1), ("daily", 5), ("trade", 5)}
    final = {(r["task"], r["horizon"]): r["artifact_sha256"] for r in store.query("SELECT * FROM run_state WHERE run_id = ?", (world["run"],))}
    assert {(m["task"], m["horizon"]): m["artifact_sha256"] for m in dep["manifest"]["models"]} == final
    assert DEP.deploy(store, world["run"]) == dep["id"]                          # the same models are the same deployment
    path = DEP.export(store, dep["id"], tmp_path / "models.json")
    other = ReplayStore(":memory:")
    new = DEP.import_file(other, path)
    assert DEP.active(other)["manifest_sha256"] == dep["manifest_sha256"]
    assert {k: v[0] for k, v in DEP.models(other, new).items()} == {k: v[0] for k, v in DEP.models(store, dep["id"]).items()}
    body = json.loads(path.read_text())
    body["manifest"]["models"][0]["family"] = "gbm"                              # tampered on the way
    (tmp_path / "bad.json").write_text(json.dumps(body))
    with pytest.raises(DEP.DeployError, match="altered"):
        DEP.import_file(other, tmp_path / "bad.json")
    with pytest.raises(Exception, match="immutable"):
        with store.tx() as cur:
            cur.execute("UPDATE deployment_models SET family = 'x' WHERE deployment_id = ?", (dep["id"],))


def test_an_unfinished_run_cannot_be_deployed(world):
    store = world["store"]
    run_id = create_run(store, world["ds"].id, world["cfg"].with_(end=date(2024, 6, 28)))
    Replay(store, run_id).run(max_sessions=5)
    with pytest.raises(DEP.DeployError, match="finished"):
        DEP.deploy(store, run_id)


# ---- the held-out year ----------------------------------------------------------------------------------------------

def test_the_held_out_year_is_opened_once_continuing_the_development_models(world):
    store = world["store"]
    dev_bars = store.one("SELECT MAX(date) FROM dataset_bars WHERE dataset_id = ?", (world["ds"].id,))[0]
    assert dev_bars >= "2026-03-31"                                              # the dataset holds the held-out year...
    dev_preds = store.one("SELECT MAX(session), MAX(target_session) FROM predictions WHERE run_id = ?", (world["run"],))
    assert dev_preds[0] <= "2025-10-31"                                          # ...and the development run never forecast in it
    graded_late = store.one("""SELECT COUNT(*) FROM outcomes o JOIN predictions p ON p.id = o.prediction_id
                               WHERE p.run_id = ? AND o.matured_session >= '2025-11-01'""", (world["run"],))[0]
    assert graded_late == 0                                                      # nor graded anything with its prices
    hold = open_holdout(store, world["run"])
    out = Replay(store, hold).run()
    first = store.one("SELECT session, model_version FROM predictions WHERE run_id = ? ORDER BY session, id LIMIT 1", (hold,))
    assert first["session"] == "2025-11-03" and first["model_version"].endswith("@" + world["run"])
    card = out["scorecard"]
    assert card["holdout_opening"] == 1 and card["notes"][0].startswith("The first evaluation of the held-out year")
    assert card["daily"]["1"]["graded"] > 0
    runs_before = store.one("SELECT COUNT(*) FROM replay_runs WHERE kind = 'holdout'")[0]
    with pytest.raises(ReplayError, match="already opened"):
        open_holdout(store, world["run"])
    assert store.one("SELECT COUNT(*) FROM replay_runs WHERE kind = 'holdout'")[0] == runs_before     # a refused opening leaves no run
    with pytest.raises(Exception, match="UNIQUE"):                           # nor can two openings share an ordinal
        with store.tx() as cur:
            cur.execute("INSERT INTO holdout_openings (run_id, parent_run, opened_at, ordinal) VALUES (?, ?, 'x', 1)", (hold, world["run"]))
    again = open_holdout(store, world["run"], reopen=True)
    card2 = Replay(store, again).run()["scorecard"]
    assert card2["holdout_opening"] == 2 and "no longer untouched" in card2["notes"][0]
    assert [o["ordinal"] for o in store.query("SELECT ordinal FROM holdout_openings ORDER BY id")] == [1, 2]


# ---- shadow forecasts -----------------------------------------------------------------------------------------------

def test_shadow_forecasts_use_the_replays_own_features_and_are_made_once_before_the_next_open(world):
    store = world["store"]
    bars = world["bars"]
    t = date(2026, 3, 2)                                                         # a Monday
    out = shadow_step(store, _fetch(bars), t, _evening(t))
    assert out["session"] == "2026-03-02" and out["predicted"]["daily"] == 2 * len(fx.STOCKS)
    run_id = out["run"]
    # The live inputs are the replay's inputs: the same stock on the same session hashes the same.
    rows = store.query("SELECT symbol, payload_json FROM predictions WHERE run_id = ? AND session = ? AND task = 'daily' AND horizon = 1",
                       (run_id, t.isoformat()))
    panel = build_panel(world["ds"])
    replay_rows = panel.features.xs(pd.Timestamp(t), level="session")
    for r in rows:
        payload = json.loads(r["payload_json"])
        assert payload["fhash"] == feature_hash(replay_rows.loc[r["symbol"], list(FEATURES)].to_numpy(float)), r["symbol"]
        assert set(payload["x"]) == set(FEATURES) | set(FACTORS) and payload["deployment"] == world["dep"]
    # Asked again, nothing changes.
    assert shadow_step(store, _fetch(bars), t, _evening(t, 19))["predicted"] == "already"
    # Next evening: yesterday's one-day forecasts are graded; today's are made.
    t1 = marketdays.next_trading_day(t)
    out1 = shadow_step(store, _fetch(bars), t1, _evening(t1))
    assert out1["graded"] == len(fx.STOCKS) and out1["predicted"]["daily"] == 2 * len(fx.STOCKS)
    # A session first seen after the next open is left out, not forecast late.
    t2 = marketdays.next_trading_day(t1)
    late = shadow_step(store, _fetch(bars), t2, datetime.combine(marketdays.next_trading_day(t2), datetime.min.time().replace(hour=10), NY))
    assert late["predicted"] == "missed" and "left out of the record" in " ".join(late["notes"])
    assert store.one("SELECT COUNT(*) FROM predictions WHERE run_id = ? AND session = ?", (run_id, t2.isoformat()))[0] == 0
    # Five sessions on, the five-day forecasts of the first day are graded too, and nothing is graded twice.
    t5 = t
    for _ in range(5):
        t5 = marketdays.next_trading_day(t5)
    shadow_step(store, _fetch(bars), t5, _evening(t5))
    graded = store.one("""SELECT COUNT(*) FROM predictions p JOIN outcomes o ON o.prediction_id = p.id
                          WHERE p.run_id = ? AND p.session = ? AND p.task = 'daily'""", (run_id, t.isoformat()))[0]
    assert graded == 2 * len(fx.STOCKS)


def test_the_live_view_stops_at_the_settled_session_whatever_the_source_holds(world):
    bars = world["bars"]
    ds = live_dataset(_fetch(bars), fx.universe(), date(2026, 3, 2))
    assert ds.calendar.last == date(2026, 3, 2)
    assert max(df["date"].max() for df in ds.bars.values()) == pd.Timestamp("2026-03-02")


def test_without_a_deployment_the_shadow_job_does_nothing():
    store = ReplayStore(":memory:")
    assert "no deployment" in shadow_step(store, lambda *a: pd.DataFrame(), date(2026, 3, 2))["skipped"]


def test_a_stock_missing_from_one_evenings_bars_is_left_due_not_graded_no_data():
    from tradeassist.intelligence.learning import ledger as L
    from tradeassist.intelligence.replay.clock import AsOf
    from tradeassist.intelligence.replay.labels import LabelBook
    from tradeassist.intelligence.replay.runner import grade
    store = ReplayStore(":memory:")
    bars = fx.market(n=400)
    bars.pop("AAPL")                                                             # tonight's fetch of AAPL failed
    ds = DS.freeze(store, bars, fx.universe(), source="test", adjustment="none")
    s = ds.calendar.sessions
    cfg = fx.config(s[300], s[380])
    run_id = create_run(store, ds.id, cfg)
    panel = build_panel(ds)
    labels = LabelBook(panel, ds.calendar, cfg.horizons, cfg.trade)
    view = AsOf(panel, labels, ds.calendar, s[350])
    base = {"symbol": "AAPL", "model_version": "m", "family": "no_change", "last_close": 100.0, "sigma": 0.02, "regime": "x", "fhash": "f"}
    with store.tx() as cur:
        L.insert_predictions(cur, run_id, "c", [
            {**base, "task": "daily", "horizon": 1, "session": s[349].isoformat(), "target_session": s[350].isoformat(),
             "pred_return": 0.0, "low_return": -0.01, "high_return": 0.01, "p_up": 0.5},
            {**base, "task": "trade", "horizon": 5, "session": s[345].isoformat(), "target_session": s[350].isoformat(), "setup": "pullback"}])
        assert grade(cur, run_id, view, labels, final=False) == 0              # live: left due, no error, nothing written
    assert store.one("SELECT COUNT(*) FROM outcomes WHERE run_id = ?", (run_id,))[0] == 0
    late = AsOf(panel, labels, ds.calendar, s[362])                              # still missing more than two weeks on
    with store.tx() as cur:
        assert grade(cur, run_id, late, labels, final=False) == 2
    assert {r[0] for r in store.query("SELECT status FROM outcomes WHERE run_id = ?", (run_id,))} == {"no_data"}


def test_the_shadow_step_is_made_once_at_a_time_in_this_process(monkeypatch):
    from tradeassist.v4 import shadow as SH
    assert SH._LOCK.acquire(blocking=False)
    try:
        got = SH.run_shadow(object(), now=datetime(2026, 3, 2, 18, 0, tzinfo=NY))
    finally:
        SH._LOCK.release()
    assert "right now" in got["skipped"]
