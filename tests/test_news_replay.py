"""The news replay (2.16): the executable price at each delay after a headline from trade prints, with latency,
spread, slippage and halts accounted for; the table across events and its verdicts; the harness on demo data;
the record the live assessment reads; and the API."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import numpy as np
import pytest
from fastapi.testclient import TestClient

from tradeassist import livenews as L
from tradeassist import newsreplay as R
from tradeassist.api import create_app
from tradeassist.config import Settings
from tradeassist.db import Database
from tradeassist.providers.demo import DemoData
from tradeassist.service import TradeAssistant

T0 = datetime(2026, 10, 19, 14, 30, 0, 500000, tzinfo=timezone.utc).timestamp()   # 10:30:00.5 New York, a Monday


def _prints(t0=T0, pre=100.0, jump_to=110.0, drift_to=112.0, gap=None, seconds_before=1800, seconds_after=3900,
            bounce=0.02):
    """Made-up prints every second: flat before the headline (with a bid-ask bounce), a jump to `jump_to` in the
    first second after it, then a straight drift to `drift_to` by 30 minutes and flat after. `gap` = (from, to)
    seconds after the headline with no prints, the shape of a halt."""
    rows = []
    first = int(t0) - seconds_before
    side = np.random.default_rng(7).choice([-1.0, 1.0], size=seconds_before + seconds_after)   # which side each trade hit
    for i in range(seconds_before + seconds_after):
        t = first + i
        since = t - t0
        if gap and gap[0] <= since < gap[1]:
            continue
        if since < 0:
            price = pre + bounce * side[i]
        elif since < 1800:
            price = jump_to + (drift_to - jump_to) * since / 1800
        else:
            price = drift_to
        rows.append({"t": float(t), "price": round(price, 4), "volume": 100.0})
    return R.prints_frame(rows)


def test_prints_are_cleaned_and_kept_in_order():
    df = R.prints_frame([{"t": 10, "price": 5.0}, {"t": 9, "price": 4.9, "volume": None}, {"t": 10, "price": 0.0},
                         {"t": "x", "price": 1}, {"t": 11, "price": 5.1, "volume": 7}])
    assert df["t"].tolist() == [9.0, 10.0, 11.0] and df["volume"].tolist() == [0.0, 0.0, 7.0]
    assert R.prints_frame([]).empty
    assert R.resolution_of(np.array([1.0, 2.0, 3.0])) == 1.0
    assert R.resolution_of(np.array([1.0, 1.25, 1.5])) == 0.25
    assert R.resolution_of(np.array([])) == 1.0


def test_the_spread_is_read_from_the_bounce_and_halts_from_the_gaps():
    df = _prints()
    before = df[df["t"] < T0]["price"].to_numpy()
    est = R.estimate_spread_bps(before)
    assert est == pytest.approx(1e4 * 0.04 / 100, rel=0.15)          # trades at $100 ± 0.02: a 4 bps spread
    assert R.estimate_spread_bps(np.linspace(100, 101, 200)) is None   # a trend, no bounce
    assert R.estimate_spread_bps(np.array([1.0, 2.0])) is None
    t = _prints(gap=(120, 420))["t"].to_numpy()
    assert R.halt_gaps(t, 60.0) == [(pytest.approx(int(T0) + 119), pytest.approx(int(T0) + 420))]
    assert R.in_regular_session(T0) and not R.in_regular_session(T0 - 3 * 3600)


def test_one_event_the_executable_price_at_each_delay_and_what_it_made():
    out = R.replay_event(_prints(), T0, 1, feed_latency_s=1.0, spread_bps=10.0, slippage_bps=5.0, close_price=113.0)
    assert out["skipped"] is None and out["ref_price"] == pytest.approx(100.0, abs=0.02) and out["resolution_s"] == 1.0
    assert out["jump_1m_pct"] == pytest.approx(100 * (110 + 2 * 60 / 1800) / out["ref_price"] - 100, abs=0.05)
    assert out["spread_bps"] == 10.0 and not out["spread_estimated"] and out["cost_pct_each_way"] == pytest.approx(0.1)
    assert out["first_print"]["lag_s"] == pytest.approx(0.5, abs=0.01)   # the next whole second after 10:30:00.5
    rows = {r["offset_s"]: r for r in out["offsets"]}
    assert sorted(rows) == [0.1, 0.5, 1.0, 5.0, 30.0]
    # Whole-second stamps: 100 ms and 500 ms after receipt (at +1.0 s) land on the same next second, said so.
    assert rows[0.1]["fill_t"] == rows[0.5]["fill_t"] == int(T0) + 2 and "next second" in rows[0.1]["note"]
    assert rows[1.0]["fill_t"] == int(T0) + 3 and rows[30.0]["fill_t"] == int(T0) + 32
    # The fill pays half the spread and the slippage on top of the print.
    assert rows[0.1]["fill"] == pytest.approx(rows[0.1]["raw_price"] * 1.001)
    # Every delay was past the jump: each caught less than nothing of the first minute's move.
    assert all(r["captured"] < 0 for r in rows.values())
    assert rows[30.0]["captured"] < rows[0.1]["captured"]
    # But the drift after the jump paid at 30 minutes, after costs, and the close paid more.
    net = rows[30.0]["net"]
    assert net["1800"] == pytest.approx(100 * (112 * 0.999 / rows[30.0]["fill"] - 1), abs=0.01) and net["1800"] > 0
    assert net["close"] > net["1800"] and net["60"] < net["300"]
    assert not out["halted"] and out["halts"] == []
    # A short on a fall pays the costs the other way.
    down = R.replay_event(_prints(jump_to=90.0, drift_to=88.0), T0, -1, spread_bps=10.0, slippage_bps=5.0)
    r30 = {r["offset_s"]: r for r in down["offsets"]}[30.0]
    assert r30["fill"] == pytest.approx(r30["raw_price"] * 0.999) and r30["net"]["1800"] > 0


def test_a_halt_fills_at_the_reopening_and_thin_data_is_skipped():
    out = R.replay_event(_prints(gap=(3, 303)), T0, 1, feed_latency_s=1.0, spread_bps=10.0)
    rows = {r["offset_s"]: r for r in out["offsets"]}
    assert rows[5.0]["halted"] and rows[30.0]["halted"] and rows[5.0]["fill_t"] == int(T0) + 304   # the first print after the gap
    assert "reopening" in rows[5.0]["note"] and out["halted"] and out["halts"][0]["seconds"] == pytest.approx(301)
    assert not rows[0.1]["halted"]                                             # sent before the halt: filled
    assert R.replay_event(R.prints_frame([]), T0, 1)["skipped"]
    late = _prints(seconds_before=-5)                                          # nothing traded before the headline
    assert "before the headline" in R.replay_event(late, T0, 1)["skipped"]
    with pytest.raises(ValueError):
        R.replay_event(_prints(), T0, 0)
    # The spread falls back to the default when the prints give no estimate.
    flat = R.replay_event(_prints(bounce=0.0), T0, 1)
    assert flat["spread_bps"] == R.DEFAULT_SPREAD_BPS and not flat["spread_estimated"]
    est = R.replay_event(_prints(), T0, 1)
    assert est["spread_estimated"] and est["spread_bps"] == pytest.approx(4.0, rel=0.15)


def _event(symbol, result, catalyst="earnings"):
    return {"symbol": symbol, "published_at": "2026-10-19T14:30:00+00:00", "headline": f"{symbol} news", "source": "Demo",
            "catalyst": catalyst, "catalyst_label": "Earnings release", "direction": 1, "result": result}


def test_the_table_across_events_and_its_verdicts():
    jump = R.replay_event(_prints(), T0, 1, spread_bps=10.0)                   # the jump was gone; the drift paid
    gone = R.replay_event(_prints(jump_to=110.0, drift_to=110.0), T0, 1, spread_bps=10.0)   # nothing after the jump
    skipped = {"skipped": "no trade prints around the headline"}
    agg = R.aggregate([_event(f"A{i}", jump) for i in range(10)] + [_event("Z", skipped)])
    assert agg["events"] == 10 and agg["skipped"] == 1
    row30 = next(r for r in agg["table"] if r["offset_s"] == 30.0)
    assert row30["n"] == 10 and row30["horizons"]["1800"]["median"] > 0 and row30["horizons"]["1800"]["hit_rate"] == 1.0
    words = " ".join(v["text"] for v in agg["verdicts"])
    assert "A second leg has been there" in words and "paying for speed buys nothing" in words
    assert "stamped to the second" in words and "1 event could not be replayed" in words
    none = R.aggregate([_event(f"B{i}", gone) for i in range(10)])
    words = " ".join(v["text"] for v in none["verdicts"])
    assert "No second leg" in words
    few = R.aggregate([_event("C", jump)])
    assert few["verdicts"][0]["state"] == "warn" and "too few" in few["verdicts"][0]["text"]


# --- the harness on demo data -----------------------------------------------------------------------------------------

@pytest.fixture
def app(tmp_path):
    from tradeassist.analysis import Analyst
    from tradeassist.morning import Morning
    s = Settings(data_mode="demo", db_path=tmp_path / "t.db", live_mover_sweep_minutes=0)
    svc = TradeAssistant(s, provider=DemoData(), db=Database(tmp_path / "t.db"))
    analyst = Analyst(svc)
    feed = L.LiveFeed(svc, Morning(svc, analyst, s.features), analyst)
    replay = R.NewsReplay(svc, feed)
    feed.replay = replay
    return svc, feed, replay


def test_demo_prints_are_made_for_any_moment_with_the_move_landing_in_the_first_second():
    df = R.demo_prints("NVDA", 100.0, T0 - 600, T0 + 600, jump_at=T0, jump=0.05)
    assert len(df) > 500 and df["t"].is_monotonic_increasing
    before = df[df["t"] < T0]["price"]
    after1 = df[(df["t"] >= T0) & (df["t"] < T0 + 2)]["price"]
    later = df[df["t"] > T0 + 300]["price"]
    assert after1.iloc[0] / before.mean() > 1.03 and later.mean() / before.mean() > 1.045
    assert R.demo_prints("NVDA", 0.0, T0, T0 + 10).empty


def test_the_feed_s_items_are_replayed_and_the_record_feeds_the_assessment(app):
    svc, feed, replay = app
    when = L.now_utc() - timedelta(hours=2)
    for i, (sym, move) in enumerate([("NVDA", 0.05), ("MSFT", 0.04), ("AAPL", -0.04), ("KO", 0.03), ("JPM", 0.05),
                                     ("XOM", 0.04), ("PG", 0.03), ("HD", 0.05), ("MRK", -0.05)]):
        item = L.make_item("Demo", f"r{i}", [sym], f"{sym} raises full-year outlook" if move > 0 else f"{sym} cuts guidance",
                           published=when - timedelta(minutes=i * 40), kind="press_release")
        item["extra"]["demo_move"] = move
        feed.ingest(item)
    events = replay.events_from_feed()
    assert len(events) == 9 and events[0]["symbol"] == "NVDA" and events[0]["news_id"]
    # A second item on a stock inside half an hour joins the first event rather than making another.
    feed.ingest(L.make_item("Demo", "r-dup", ["NVDA"], "NVDA more news", published=when - timedelta(minutes=5)))
    assert len(replay.events_from_feed()) == 9
    params = {"feed_latency_s": 1.0, "spread_bps": None, "slippage_bps": 5.0, "offsets": R.OFFSETS, "horizons": R.HORIZONS}
    out = replay.run(replay.events_from_feed(), params)
    assert out["events"] == 9 and out["run_id"] == 1 and out["mode"] == "demo"
    first = out["results"][0]["result"]
    assert first["skipped"] is None and first["prints"] > 1000 and first["resolution_s"] == 1.0 and first["spread_estimated"]
    rows = {r["offset_s"]: r for r in first["offsets"]}
    assert rows[0.1]["captured"] < 0.5 and rows[30.0]["captured"] < rows[1.0]["captured"]   # the demo move lands fast
    kinds = {k["catalyst"]: k for k in out["kinds"]}
    assert kinds["guidance_raise"]["events"] == 7 and kinds["guidance_cut"]["events"] == 2
    assert svc.db.one("SELECT COUNT(*) AS n FROM news_replays")["n"] == 9
    record = replay.record_for("guidance_raise")
    assert record["cases"] == 7 and record["label"] == "Guidance raised" and isinstance(record["median_net_pct"], float)
    assert replay.record_for("bankruptcy") is None
    # A second run replaces the record's rows for the same events rather than doubling them.
    replay.run(replay.events_from_feed(), params)
    replay._record_cache.clear()
    assert replay.record_for("guidance_raise")["cases"] == 7
    # The record reaches the live assessment through the feed.
    item = L.make_item("Demo", "live-1", ["NVDA"], "NVDA raises full-year outlook", published=L.now_utc(), kind="press_release")
    item["extra"]["demo_move"] = 0.05
    feed.ingest(item)
    sig = [L.signal_row(r) for r in feed.db.query("SELECT * FROM live_signals WHERE symbol = 'NVDA' ORDER BY id DESC")][0]
    feed.check(sig, svc.market.provider.quotes(["NVDA"])["NVDA"], "open")
    sig = [L.signal_row(r) for r in feed.db.query("SELECT * FROM live_signals WHERE id = ?", (sig["id"],))][0]
    assert sig["intel"]["second_leg"]["cases"] == 7 and sig["intel"]["priced_in"]["usual_from"] == "the usual size of such news"


def test_status_defaults_and_an_empty_run_is_refused(app):
    svc, feed, replay = app
    st = replay.status()
    assert st["job"]["state"] == "idle" and st["result"] is None and st["reach"]["days"] == 7 and st["feed_events"] == 0
    assert st["defaults"]["offsets"] == [0.1, 0.5, 1.0, 5.0, 30.0]
    with pytest.raises(ValueError, match="Nothing to replay"):
        replay.start([])


def test_the_api_runs_a_replay_on_typed_in_events(tmp_path):
    s = Settings(data_mode="demo", db_path=tmp_path / "t.db", live_mover_sweep_minutes=0)
    svc = TradeAssistant(s, provider=DemoData(), db=Database(tmp_path / "t.db"))
    when = (datetime.now(timezone.utc) - timedelta(hours=3)).isoformat(timespec="seconds")
    with TestClient(create_app(s, svc)) as client:
        st = client.get("/v2/live/replay").json()
        assert st["job"]["state"] == "idle" and st["mode"] == "demo"
        bad = client.post("/v2/live/replay", json={"events": "manual", "manual": [{"symbol": "AAPL", "published_at": "yesterday-ish"}]})
        assert bad.status_code == 400 and "could not be read" in bad.json()["error"]
        future = client.post("/v2/live/replay", json={"events": "manual", "manual": [{"symbol": "AAPL", "published_at": "2099-01-01T10:00:00"}]})
        assert future.status_code == 400 and "future" in future.json()["error"]
        empty = client.post("/v2/live/replay", json={"events": "feed"})
        assert empty.status_code == 400 and "Nothing to replay" in empty.json()["error"]
        out = client.post("/v2/live/replay", json={"events": "manual", "spread_bps": 8, "manual": [
            {"symbol": "aapl", "published_at": when, "headline": "Apple to be acquired", "direction": None},
            {"symbol": "MSFT", "published_at": "2026-10-19 10:30", "headline": "Microsoft news", "direction": "SHORT"}]}).json()
        assert out["events"] == 2 and out["job"]["state"] in ("running", "done")
        for _ in range(100):
            st = client.get("/v2/live/replay").json()
            if st["job"]["state"] != "running":
                break
            import time
            time.sleep(0.1)
        assert st["job"]["state"] == "done"
        r = st["result"]
        assert r["events"] == 2 and r["params"]["spread_bps"] == 8 and [e["symbol"] for e in r["results"]] == ["AAPL", "MSFT"]
        assert r["results"][0]["catalyst"] == "acquisition_target" and r["results"][0]["direction"] == 1
        assert r["results"][1]["direction"] == -1 and r["results"][1]["published_at"].startswith("2026-10-19T10:30:00-04:00")
        assert r["verdicts"][0]["state"] == "warn"                                  # two events: too few to call
        assert client.post("/v2/live/replay/stop", json={}).json()["job"]["state"] == "done"
    off = Settings(data_mode="demo", db_path=tmp_path / "u.db", features={**__import__("tradeassist.features", fromlist=["FEATURE_DEFAULTS"]).FEATURE_DEFAULTS, "live_news": False})
    svc2 = TradeAssistant(off, provider=DemoData(), db=Database(tmp_path / "u.db"))
    with TestClient(create_app(off, svc2)) as client:
        assert client.get("/v2/live/replay").status_code == 404
