"""4.0 phase 1-3: the replay's guarantees.

1. A prediction for session T is the same whatever data exists after T (prefix invariance).
2. An outcome is invisible to training until its window has closed (maturity).
3. The same dataset and configuration give the same ledger, including a run stopped and resumed (reproducibility).
4. The database refuses to change or delete a prediction, an outcome, a decision or a dataset's bars (immutability).
5. Horizons count exchange sessions, across holidays and early closes (calendar).
And: the held-out year cannot be replayed in development; one run, one worker, resumable after any failure; the
barriers, the promotion rule (and how often it promotes a model that is no better) and the scorecard.
"""

import sqlite3
from datetime import date

import numpy as np
import pandas as pd
import pytest

from tests import v4_fixtures as fx
from tradeassist.intelligence.config import DATASET_FROM, HOLDOUT_FROM, ConfigError, PromotionRule, ReplayConfig, validation_window
from tradeassist.intelligence.data import dataset as DS
from tradeassist.intelligence.data.calendar import Calendar, from_market_dates
from tradeassist.intelligence.data.features import FEATURES, build_panel
from tradeassist.intelligence.learning import trainer as TR
from tradeassist.intelligence.learning import validation as V
from tradeassist.intelligence.learning.ledger import ledger_hash
from tradeassist.intelligence.learning.registry import lineage as lineage_of
from tradeassist.intelligence.replay.clock import AsOf, LookaheadError
from tradeassist.intelligence.replay.execution import simulate
from tradeassist.intelligence.replay.labels import LabelBook
from tradeassist.intelligence.replay.runner import Replay, ReplayError, create_run
from tradeassist.intelligence.store import ReplayStore

N = 700


@pytest.fixture(scope="module")
def bars():
    return fx.market(n=N)


@pytest.fixture
def store():
    s = ReplayStore(":memory:")
    yield s
    s.close()


def _ds(store, bars, last=None):
    if last is not None:
        bars = {k: v[v["date"] <= pd.Timestamp(last)] for k, v in bars.items()}
    return DS.freeze(store, bars, fx.universe(), source="test", adjustment="none")


def _quick(start, end, **over):
    return fx.config(start, end, families=("drift", "ridge"), **over)


def _run(store, ds_id, cfg, **kw):
    run_id = create_run(store, ds_id, cfg)
    out = Replay(store, run_id).run(**kw)
    return run_id, out


def _rows(store, run_id, through: str):
    return [tuple(r) for r in store.query(
        "SELECT session, task, horizon, symbol, row_sha256 FROM predictions WHERE run_id = ? AND session <= ? "
        "ORDER BY session, task, horizon, symbol", (run_id, through))]


# ---- 1. prefix invariance -------------------------------------------------------------------------------------------

def test_every_feature_on_a_session_is_the_same_whatever_comes_after_it(store, bars):
    full = build_panel(_ds(store, bars))
    sessions = full.sessions
    for cut in (sessions[300], sessions[451], sessions[-40]):
        short = build_panel(_ds(store, bars, last=cut))
        a = full.features.xs(cut, level="session")[list(FEATURES)]
        b = short.features.xs(cut, level="session")[list(FEATURES)]
        pd.testing.assert_frame_equal(a, b, check_exact=False, rtol=1e-12, atol=1e-15)
        pd.testing.assert_series_equal(full.eligible.loc[cut], short.eligible.loc[cut])


def test_predictions_up_to_a_session_do_not_change_when_later_data_exists(store, bars):
    ds = _ds(store, bars)
    s = ds.calendar.sessions
    start, early_end, late_end = s[280], s[520], s[640]
    run_a, _ = _run(store, ds.id, _quick(start, early_end))
    run_b, _ = _run(store, ds.id, _quick(start, late_end))
    a, b = _rows(store, run_a, early_end.isoformat()), _rows(store, run_b, early_end.isoformat())
    assert len(a) > 2000 and a == b
    # the models were retrained along the way, so this compares learned forecasts, not only the baseline
    assert store.one("SELECT COUNT(*) FROM training_runs WHERE run_id = ? AND decision != 'insufficient_data' AND session <= ?",
                     (run_a, early_end.isoformat()))[0] > 0


# ---- 2. maturity ----------------------------------------------------------------------------------------------------

def test_a_five_day_outcome_is_invisible_to_training_until_its_window_closes(store, bars):
    ds = _ds(store, bars)
    panel = build_panel(ds)
    cfg = fx.config(ds.calendar.sessions[300], ds.calendar.sessions[600])
    labels = LabelBook(panel, ds.calendar, cfg.horizons, cfg.trade)
    t = ds.calendar.sessions[450]
    ts = pd.Timestamp(t)
    for k in range(0, 8):
        view = AsOf(panel, labels, ds.calendar, ds.calendar.add(t, k))
        seen = ts in set(view.daily_examples(5, 1000).index.get_level_values("session"))
        assert seen == (k >= 5), k
        seen1 = ts in set(view.daily_examples(1, 1000).index.get_level_values("session"))
        assert seen1 == (k >= 1), k
        trades = view.trade_examples(1000)
        if len(trades):
            assert trades.index.get_level_values("session").max() <= pd.Timestamp(ds.calendar.add(view.session, -5))


def test_the_view_hands_out_no_label_but_the_matured_one_asked_for(store, bars):
    ds = _ds(store, bars)
    panel = build_panel(ds)
    labels = LabelBook(panel, ds.calendar, (1, 5), fx.config(date(2019, 1, 2), date(2019, 6, 3)).trade)
    found = 0
    for i in range(400, 460):
        view = AsOf(panel, labels, ds.calendar, ds.calendar.sessions[i])
        today = view.setups()
        found += len(today)
        assert not {"y1", "y5"} & set(today.columns)              # today's setups carry no outcome, not even a later one
        assert "y1" in view.daily_examples(1, 300).columns and "y5" not in view.daily_examples(1, 300).columns
        assert "y5" in view.daily_examples(5, 300).columns and "y1" not in view.daily_examples(5, 300).columns
    assert found > 0


def test_the_view_refuses_to_read_after_its_cutoff(store, bars):
    ds = _ds(store, bars)
    panel = build_panel(ds)
    labels = LabelBook(panel, ds.calendar, (1, 5), fx.config(date(2019, 1, 2), date(2019, 6, 3)).trade)
    t = ds.calendar.sessions[400]
    view = AsOf(panel, labels, ds.calendar, t)
    assert view.close("AAPL", t) is not None
    with pytest.raises(LookaheadError):
        view.close("AAPL", ds.calendar.add(t, 1))


# ---- 3. reproducibility ---------------------------------------------------------------------------------------------

def test_the_same_data_and_settings_give_the_same_ledger_even_after_a_stop_and_resume(store, bars):
    ds = _ds(store, bars)
    s = ds.calendar.sessions
    cfg = fx.config(s[280], s[560], families=("ridge", "gbm"), retrain="monthly")
    run_a, out_a = _run(store, ds.id, cfg)
    run_b, out_b = _run(store, ds.id, cfg)
    assert out_a["ledger_sha256"] == out_b["ledger_sha256"]
    run_c = create_run(store, ds.id, cfg)
    paused = Replay(store, run_c).run(max_sessions=137)
    assert paused["status"] == "paused"
    again = Replay(store, run_c).run(max_sessions=61)            # a second interruption, from a fresh process's view
    assert again["status"] == "paused"
    done = Replay(store, run_c).run()
    assert done["ledger_sha256"] == out_a["ledger_sha256"]
    assert ledger_hash(store, run_c) == ledger_hash(store, run_a)


def test_a_run_that_failed_in_its_first_session_resumes_where_it_was(store, bars, monkeypatch):
    ds = _ds(store, bars)
    s = ds.calendar.sessions
    cfg = _quick(s[280], s[330])
    clean, out = _run(store, ds.id, cfg)
    run_id = create_run(store, ds.id, cfg)
    real = Replay.step
    def broken(self, d):
        raise RuntimeError("the network went away")
    monkeypatch.setattr(Replay, "step", broken)
    with pytest.raises(RuntimeError):
        Replay(store, run_id).run()
    assert store.one("SELECT status FROM replay_runs WHERE id = ?", (run_id,))[0] == "failed"
    monkeypatch.setattr(Replay, "step", real)
    done = Replay(store, run_id).run()                            # installs nothing twice, and ends where the clean run did
    assert done["ledger_sha256"] == out["ledger_sha256"]
    with pytest.raises(ReplayError, match="complete"):
        Replay(store, run_id).run()


def test_a_run_is_worked_by_one_process_at_a_time(store, bars):
    from datetime import datetime, timedelta, timezone
    ds = _ds(store, bars)
    s = ds.calendar.sessions
    run_id = create_run(store, ds.id, _quick(s[280], s[300]))
    fresh = datetime.now(timezone.utc).isoformat(timespec="seconds")
    with store.tx() as cur:
        cur.execute("UPDATE replay_runs SET status = 'running', worker = 'another-mac:4242', heartbeat_at = ? WHERE id = ?", (fresh, run_id))
    with pytest.raises(ReplayError, match="another process"):
        Replay(store, run_id).run()
    stale = (datetime.now(timezone.utc) - timedelta(minutes=10)).isoformat(timespec="seconds")
    with store.tx() as cur:                                        # that process died: its lease ran out
        cur.execute("UPDATE replay_runs SET heartbeat_at = ? WHERE id = ?", (stale, run_id))
    assert Replay(store, run_id).run()["status"] == "complete"


def test_two_processes_resuming_one_run_at_once_cannot_both_have_it(store, bars, monkeypatch):
    from datetime import datetime, timezone
    from tradeassist.intelligence.replay import runner as RN
    ds = _ds(store, bars)
    s = ds.calendar.sessions
    run_id = create_run(store, ds.id, _quick(s[280], s[300]))

    def racing():                                  # the other process claims it between this one's look and its claim
        with store.tx() as cur:
            cur.execute("UPDATE replay_runs SET status = 'running', worker = 'other-mac:1', heartbeat_at = ? WHERE id = ?",
                        (datetime.now(timezone.utc).isoformat(timespec="seconds"), run_id))
        return "this-mac:2"
    monkeypatch.setattr(RN, "worker_name", racing)
    with pytest.raises(ReplayError, match="claimed a moment ago"):
        Replay(store, run_id).run()
    assert store.one("SELECT worker, status FROM replay_runs WHERE id = ?", (run_id,))[:] == ("other-mac:1", "running")


# ---- 4. immutability ------------------------------------------------------------------------------------------------

def test_predictions_outcomes_decisions_and_bars_cannot_be_changed_or_deleted(store, bars):
    ds = _ds(store, bars)
    s = ds.calendar.sessions
    run_id, _ = _run(store, ds.id, _quick(s[280], s[420]))
    for sql in ("UPDATE predictions SET pred_return = 0.5 WHERE run_id = ?",
                "DELETE FROM predictions WHERE run_id = ?",
                "UPDATE outcomes SET realized_return = 0 WHERE run_id = ?",
                "DELETE FROM outcomes WHERE run_id = ?",
                "UPDATE trade_decisions SET take = 1 WHERE run_id = ?",
                "DELETE FROM training_runs WHERE run_id = ?",
                "UPDATE model_versions SET family = 'x' WHERE run_id = ?"):
        with pytest.raises(sqlite3.IntegrityError, match="immutable"):
            with store.tx() as cur:
                cur.execute(sql, (run_id,))
    with pytest.raises(sqlite3.IntegrityError, match="immutable"):
        with store.tx() as cur:
            cur.execute("UPDATE dataset_bars SET close = close * 2 WHERE dataset_id = ?", (ds.id,))
    # INSERT OR REPLACE deletes the old row first: that delete is refused too
    for table in ("predictions", "outcomes", "trade_decisions"):
        with pytest.raises(sqlite3.IntegrityError, match="immutable"):
            with store.tx() as cur:
                cur.execute(f"INSERT OR REPLACE INTO {table} SELECT * FROM {table} WHERE run_id = ? LIMIT 1", (run_id,))
    # a row changed underneath its stored hash (here by switching the guard off) is caught by the ledger hash
    from tradeassist.intelligence.learning.ledger import LedgerError
    with store.tx() as cur:
        cur.execute("DROP TRIGGER predictions_no_update")
        cur.execute("UPDATE predictions SET pred_return = 0.5 WHERE id = (SELECT MIN(id) FROM predictions WHERE run_id = ?)", (run_id,))
    with pytest.raises(LedgerError, match="no longer matches"):
        ledger_hash(store, run_id)


def test_a_dataset_is_named_by_its_content_and_an_altered_file_is_refused(store, bars):
    a = _ds(store, bars)
    assert _ds(store, bars).id == a.id                          # frozen once: the same bars are the same dataset
    changed = {k: v.copy() for k, v in bars.items()}
    changed["AAPL"].loc[100, "close"] *= 1.01
    assert DS.freeze(store, changed, fx.universe(), source="test", adjustment="none").id != a.id
    with store.tx() as cur:                                      # tamper underneath the triggers
        cur.execute("DROP TRIGGER dataset_bars_no_update")
        cur.execute("UPDATE dataset_bars SET close = close + 1 WHERE dataset_id = ? AND symbol = 'MSFT' AND date = ?",
                    (a.id, a.bars["MSFT"]["date"].iloc[50].date().isoformat()))
    with pytest.raises(DS.DatasetError, match="altered"):
        DS.load(store, a.id)


# ---- 5. calendar ----------------------------------------------------------------------------------------------------

def test_horizons_count_exchange_sessions_across_holidays():
    from tradeassist import marketdays
    from tradeassist.forecast import record_target
    days = marketdays.trading_days_between(date(2025, 11, 3), date(2026, 1, 30))
    cal, problems = from_market_dates(days)
    assert problems == []
    assert cal.add(date(2025, 11, 26), 1) == date(2025, 11, 28)             # Thanksgiving is not a session
    assert cal.add(date(2025, 12, 24), 1) == date(2025, 12, 26)
    assert cal.cutoff_at(date(2025, 11, 28)).startswith("2025-11-28T14:00")  # an early close: 1 p.m., plus an hour
    assert cal.cutoff_at(date(2025, 11, 26)).startswith("2025-11-26T17:00")
    assert cal.is_week_end(date(2025, 11, 28)) and not cal.is_week_end(date(2025, 11, 26))
    assert record_target(date(2026, 11, 25), 1) == date(2026, 11, 27)      # Research's forecasts too (was the holiday)


def test_a_market_proxy_that_disagrees_with_the_exchange_calendar_fails_the_dataset_checks():
    from tradeassist import marketdays
    days = marketdays.trading_days_between(date(2025, 11, 3), date(2025, 12, 31))
    days = [d for d in days if d != date(2025, 12, 10)] + [date(2025, 12, 25)]
    _, problems = from_market_dates(days)
    codes = sorted(p["code"] for p in problems)
    assert codes == ["CALENDAR_EXTRA_SESSION", "CALENDAR_MISSING_SESSION"]
    assert all(p["level"] == "fail" for p in problems)


def test_calendar_steps_and_bounds():
    cal = Calendar([date(2024, 1, 2), date(2024, 1, 3), date(2024, 1, 5)])
    assert cal.add(date(2024, 1, 3), 1) == date(2024, 1, 5)
    assert cal.add(date(2024, 1, 5), 1) is None
    assert cal.on_or_after(date(2024, 1, 4)) == date(2024, 1, 5)
    assert cal.on_or_before(date(2024, 1, 4)) == date(2024, 1, 3)


def test_a_run_that_ends_midweek_does_not_treat_its_last_session_as_a_week_end(store, bars):
    ds = _ds(store, bars)
    s = ds.calendar.sessions
    wednesday = next(d for d in s[400:] if d.weekday() == 2)
    friday = next(d for d in s[300:] if d.weekday() == 4)
    replay = Replay(store, create_run(store, ds.id, _quick(s[280], wednesday)))
    assert replay.dataset.calendar.last == wednesday
    assert not replay.dataset.calendar.is_week_end(wednesday)   # past its end, the cut calendar asks the exchange's
    assert not replay.is_training_day(wednesday) and replay.is_training_day(friday)


# ---- the held-out year ----------------------------------------------------------------------------------------------

def test_a_development_replay_cannot_reach_the_held_out_year(store, bars):
    with pytest.raises(ConfigError, match="held out"):
        ReplayConfig(end=HOLDOUT_FROM).check()
    with pytest.raises(ConfigError):
        ReplayConfig(kind="holdout", start=date(2025, 6, 2), end=date(2026, 1, 30)).check()
    ds = _ds(store, bars)
    s = ds.calendar.sessions
    run_id = create_run(store, ds.id, _quick(s[280], s[400]))
    replay = Replay(store, run_id)
    assert replay.dataset.calendar.last == s[400]                # later bars are cut before any feature is built
    assert max(df["date"].max() for df in replay.dataset.bars.values()) <= pd.Timestamp(s[400])


def test_a_dataset_with_failed_checks_is_refused_unless_asked(store):
    from tradeassist import marketdays
    days = [d for d in marketdays.trading_days_between(date(2025, 1, 2), date(2026, 6, 30)) if d != date(2025, 3, 5)]
    rng = np.random.default_rng(1)
    def frame(n, base):
        c = base * np.exp(np.cumsum(0.01 * rng.standard_normal(n)))
        return pd.DataFrame({"date": pd.to_datetime(days), "open": c, "high": c * 1.01, "low": c * 0.99, "close": c, "volume": 1e6})
    ds = DS.freeze(store, {"SPY": frame(len(days), 500), "AAPL": frame(len(days), 200)},
                   fx.universe(), source="sandbox", adjustment="unknown")
    assert ds.has_failures()
    cfg = _quick(date(2025, 6, 2), date(2025, 9, 30))
    with pytest.raises(ReplayError, match="failed its checks"):
        create_run(store, ds.id, cfg)
    assert create_run(store, ds.id, cfg, allow_failed_checks=True)
    with pytest.raises(ReplayError, match="Only development"):  # the held-out year has its own register (phase 4)
        create_run(store, ds.id, cfg.with_(kind="holdout", start=HOLDOUT_FROM, end=date(2026, 3, 2)), allow_failed_checks=True)


# ---- trades ---------------------------------------------------------------------------------------------------------

def _trade(opens, highs, lows, closes, atr=1.0, cost=0.0):
    return simulate(np.array(opens, float), np.array(highs, float), np.array(lows, float), np.array(closes, float), atr,
                    target_atr=1.5, stop_atr=1.0, cost_bps_per_side=cost)


def test_the_triple_barrier_reads_each_day_in_order_and_scores_an_ambiguous_day_as_the_stop():
    t = _trade([100, 100.5], [100.8, 101.6], [99.5, 100.2], [100.4, 101.2])
    assert t["barrier"] == "target" and t["exit_offset"] == 1 and t["exit_price"] == pytest.approx(101.5)
    s = _trade([100, 99.6], [100.5, 99.9], [99.4, 98.8], [99.7, 99.0])
    assert s["barrier"] == "stop" and s["exit_price"] == pytest.approx(99.0)
    gap = _trade([100, 97.0], [100.5, 97.5], [99.5, 96.5], [100.2, 97.2])
    assert gap["barrier"] == "stop" and gap["exit_price"] == pytest.approx(97.0)          # a gap fills worse than the stop
    both = _trade([100], [101.7], [98.9], [100.0])
    assert both["barrier"] == "ambiguous" and both["exit_price"] == pytest.approx(99.0)    # daily bars cannot tell: the stop
    timeout = _trade([100, 100.2, 100.1], [100.6, 100.7, 100.9], [99.6, 99.8, 99.7], [100.3, 100.4, 100.6])
    assert timeout["barrier"] == "timeout" and timeout["exit_price"] == pytest.approx(100.6)
    costly = _trade([100, 100.2, 100.1], [100.6, 100.7, 100.9], [99.6, 99.8, 99.7], [100.3, 100.4, 100.6], cost=10)
    assert costly["net"] < timeout["net"] and costly["net"] == pytest.approx(100.6 * 0.999 / (100 * 1.001) - 1)
    assert _trade([100, np.nan], [101, 101], [99.5, 99.5], [100, 100])["barrier"] is None


def test_every_setup_gets_a_frozen_forecast_and_a_decision_and_is_graded(store, bars):
    ds = _ds(store, bars)
    s = ds.calendar.sessions
    run_id, out = _run(store, ds.id, _quick(s[280], s[600]))
    n_trade = store.one("SELECT COUNT(*) FROM predictions WHERE run_id = ? AND task = 'trade'", (run_id,))[0]
    n_dec = store.one("SELECT COUNT(*) FROM trade_decisions WHERE run_id = ?", (run_id,))[0]
    assert n_trade > 50 and n_dec == n_trade
    graded = store.one("""SELECT COUNT(*) FROM predictions p JOIN outcomes o ON o.prediction_id = p.id
                          WHERE p.run_id = ? AND p.task = 'trade' AND o.status = 'graded'""", (run_id,))[0]
    assert graded >= n_trade - 30                                # all but the last few, whose windows had not closed
    # passed setups are graded as well as taken ones
    card = out["scorecard"]["trade"]
    assert card["all_setups"]["trades"] == card["taken"].get("trades", 0) + card["passed"].get("trades", 0)
    # before any trade model was trained, nothing was taken
    first_training = store.one("SELECT MIN(session) FROM training_runs WHERE run_id = ? AND task = 'trade' AND decision != "
                               "'insufficient_data'", (run_id,))[0]
    early = store.one("SELECT COUNT(*) FROM trade_decisions WHERE run_id = ? AND take = 1 AND session < ?",
                      (run_id, first_training or "9999"))[0]
    assert early == 0


# ---- learning -------------------------------------------------------------------------------------------------------

def test_the_replay_finds_a_real_edge_and_scores_it_against_no_change(store):
    # half of each day's own move comes back the next day: an edge a linear model should find and keep
    ds = DS.freeze(store, fx.market(n=900, reversion=0.5), fx.universe(), source="test", adjustment="none")
    s = ds.calendar.sessions
    run_id, out = _run(store, ds.id, _quick(s[280], s[890]))
    card = out["scorecard"]
    one = card["daily"]["1"]
    assert card["champions"]["daily:1"]["family"] == "ridge"     # the planted one-day reversal was learned...
    learned = one["by_family"]["ridge"]                          # ...and, once promoted, beat 'no change' on the frozen record
    assert learned["graded"] > 1000 and learned["gain_bp"] > 0 and learned["t"] > 1.645
    assert 0.72 <= learned["coverage"] <= 0.88
    assert one["by_family"]["no_change"]["gain_bp"] == pytest.approx(0.0)
    assert card["daily"]["5"]["graded"] > 0 and card["headline"].startswith("1-day forecasts")
    lineage = store.query("SELECT decision, reason FROM training_runs WHERE run_id = ? AND task = 'daily' AND horizon = 1 "
                          "AND decision = 'promoted'", (run_id,))
    assert lineage and "separate validation windows" in lineage[0]["reason"]
    # once promoted, the model keeps facing 'no change' on every training day, so it could lose its place again
    later = [r["candidates"] for r in lineage_of(store, run_id) if r["task"] == "daily" and r["horizon"] == 1
             and r["candidates"].get("ridge", {}).get("role") == "champion"]
    assert later and all(c["no_change"]["role"] == "challenger" for c in later)


def test_promotion_needs_more_than_chance_divided_among_the_challengers_on_two_separate_windows():
    rule = PromotionRule()
    assert V.threshold(rule, 4, 30) > V.threshold(rule, 1, 30) > 1.645
    gain = pd.Series(np.arange(20, dtype=float), index=pd.bdate_range("2024-01-01", periods=20))
    assert list(V.independent_gain(gain, 5)) == [4.0, 9.0, 14.0, 19.0]   # every fifth date, the latest included
    assert V.newey_west_t(np.ones(10), 2) == 0.0 and V.plain_t(np.array([1.0, 2.0, 3.0])) > 0
    rng = np.random.default_rng(0)
    dates = np.repeat(pd.bdate_range("2024-01-01", periods=60), 50)
    err = np.abs(rng.standard_normal(len(dates)))
    same = {"err": err, "dates": dates, "rows": len(err), "coverage": 0.8, "tail": float(np.quantile(err, 0.95))}
    assert not V.compare_daily(same, dict(same), rule, 1, 0.8, 4)["passes"]      # a copy of the champion never wins
    better = dict(same, err=err * 0.8, tail=float(np.quantile(err * 0.8, 0.95)))
    assert V.compare_daily(same, better, rule, 1, 0.8, 4)["passes"]
    assert TR._families("ridge", "no_change", ("drift", "ridge", "gbm")) == ["ridge", "no_change", "drift", "gbm"]
    assert TR._families("gbm", "base_rate", ("gbm",)) == ["gbm", "base_rate"]
    # the champion wins nothing if a challenger passes on the latest window but not on the one before it
    ev = [{"a": same, "b": better}, {"a": same, "b": dict(same)}]
    chosen, cands = TR._choose("a", ["a", "b"], ev, lambda x, y, k: V.compare_daily(x, y, rule, 1, 0.8, k))
    assert chosen is None and [w["passes"] for w in cands["b"]["windows"]] == [True, False]


def test_a_challenger_no_better_than_the_champion_is_rarely_promoted_over_a_whole_run():
    """The run-wide false promotion rate of the configured rule. Every challenger's error equals the champion's plus
    noise, so any promotion is a false one; a run trains weekly from its first possible training day to the held-out
    year, as a default ten-year development run does. The rule must promote in under 5% of such runs, per horizon."""
    from tradeassist import marketdays
    cfg = ReplayConfig()
    rule = cfg.promotion
    challengers = len(TR._families("no_change", "no_change", cfg.families)) - 1
    sessions = len(marketdays.trading_days_between(DATASET_FROM, HOLDOUT_FROM))
    runs = 150
    rng = np.random.default_rng(2026)
    for h in cfg.horizons:
        n_val = validation_window(rule, h)
        pick = V.independent_gain(pd.Series(np.arange(n_val)), h).astype(int)       # positions the test keeps, oldest first
        need = V.threshold(rule, challengers, len(pick))
        noise = rng.standard_normal((runs, challengers, sessions + h))
        gain = sum(noise[:, :, j:j + sessions] for j in range(h))                   # overlapping h-session outcomes
        first = cfg.min_train_sessions + n_val * rule.confirmations
        promoted = np.zeros(runs, dtype=bool)
        for day in range(first, sessions, 5):
            ok = np.ones((runs, challengers), dtype=bool)
            for w in range(rule.confirmations):
                hi = day - w * n_val
                seg = gain[:, :, hi - n_val + 1: hi + 1][:, :, pick]
                t = seg.mean(-1) / (seg.std(-1, ddof=1) / np.sqrt(seg.shape[-1]))
                ok &= t > need
            promoted |= ok.any(-1)
        assert seg[0, 0].size == len(pick) and V.plain_t(seg[0, 0]) == pytest.approx(t[0, 0])
        assert promoted.mean() < 0.05, (h, promoted.mean())


def test_a_mis_specified_run_is_refused():
    with pytest.raises(ConfigError):
        ReplayConfig(families=("gbm", "crystal_ball")).check()
    with pytest.raises(ConfigError):
        ReplayConfig(horizons=(0,)).check()
    cfg = ReplayConfig(start=date(2017, 1, 3), end=date(2018, 1, 3))
    assert ReplayConfig.from_dict(cfg.to_dict()) == cfg and cfg.sha256() == ReplayConfig.from_dict(cfg.to_dict()).sha256()
    assert cfg.with_(seed=8).sha256() != cfg.sha256()
