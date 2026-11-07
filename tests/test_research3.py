"""3.0 phase 3: Research. Analogs that never see the future, forecasts whose weights are learned and judged out of
sample, the contract's own price history kept apart from theoretical values, strategies compared under one set of
scenarios after costs, and the study that ties them together."""

from __future__ import annotations

import time
from types import SimpleNamespace

import numpy as np
import pandas as pd
import pytest
from fastapi.testclient import TestClient

from tradeassist import analogs as A
from tradeassist import forecast as F
from tradeassist import premium, studio
from tradeassist.api import create_app
from tradeassist.config import Settings
from tradeassist.db import Database
from tradeassist.providers.demo import DemoData
from tradeassist.service import TradeAssistant


def frame(closes, start="2021-01-04", volume=None):
    c = np.asarray(closes, dtype=float)
    dates = pd.bdate_range(start, periods=len(c))
    o = np.concatenate([[c[0]], c[:-1]])
    v = np.full(len(c), 1e6) if volume is None else np.asarray(volume, dtype=float)
    return pd.DataFrame({"date": dates, "open": o, "high": np.maximum(o, c) * 1.002, "low": np.minimum(o, c) * 0.998, "close": c, "volume": v})


def walk(n=900, seed=3, vol=0.015):
    rng = np.random.default_rng(seed)
    return 100 * np.exp(np.cumsum(vol * rng.standard_normal(n)))


# --- analogs -----------------------------------------------------------------------------------------------------

def test_features_use_no_later_data():
    df = frame(walk())
    full = A.features(df)
    cut = A.features(df.iloc[:600])
    cols = ["move_atr", "gap_atr", "rsi", "dist50", "ret20", "rvol_rank", "drawdown20"]
    pd.testing.assert_frame_equal(full.loc[:599, cols].reset_index(drop=True), cut[cols].reset_index(drop=True))


def test_analogs_end_before_the_as_of_day_and_never_overlap_today():
    df = frame(walk())
    s = A.prepare("XYZ", df, None, own=True)
    out = A.search(s, [s], horizon=5, kind="setup", limit=30)
    as_of = pd.Timestamp(out["as_of"])
    assert out["analogs"]
    for a in out["analogs"]:
        day = pd.Timestamp(a["date"])
        i = int(s.feats.index[s.feats["date"] == day][0])
        assert s.feats.at[i + 5, "date"] < as_of                         # the forward window ended before today
        assert i <= len(df) - 1 - 5 - 1                                  # and does not reach into today's
    # A study dated in the past sees only what was known then.
    mid = 500
    past = A.search(s, [s], horizon=5, kind="setup", as_of=mid, limit=30)
    assert all(pd.Timestamp(a["date"]) + pd.offsets.BDay(5) < s.feats.at[mid, "date"] for a in past["analogs"])


def test_overlapping_and_same_day_analogs_are_marked_and_frequencies_use_the_independent_ones():
    rng = np.random.default_rng(9)
    closes = walk(700, seed=4)
    # Drops of several daily ranges on two consecutive days, several times: each pair overlaps.
    for i in range(120, 640, 60):
        closes[i:] *= 0.94
        closes[i + 1:] *= 0.95
    vol = 1e6 * (1 + 0.1 * rng.standard_normal(700))
    a = A.prepare("AAA", frame(closes, volume=np.abs(vol)), None, own=True)
    b = A.prepare("BBB", frame(closes * 0.5, volume=np.abs(vol)), None, own=False)    # a peer that moved the same days
    out = A.search(a, [a, b], horizon=5, kind="drop", limit=40)
    rows = out["analogs"]
    assert any(not r["independent"] for r in rows)
    seen = {}
    for r in rows:
        key = r["date"]
        if key in seen:
            assert not r["independent"]                                   # the same day through another stock
        seen.setdefault(key, r)
    h = out["summary"]["by_horizon"]["5"]
    assert h["independent"] == sum(1 for r in rows if r["independent"] and "5" in r["forward_pct"]) < h["cases"]


def test_peer_analogs_count_slightly_less_than_the_stocks_own():
    closes = walk(700, seed=5)
    a = A.prepare("AAA", frame(closes), None, own=True)
    b = A.prepare("BBB", frame(closes), None, own=False)                    # identical history, as a peer
    rows = A.search(a, [a, b], horizon=5, kind="setup", limit=40)["analogs"]
    own = {r["date"]: r["similarity"] for r in rows if r["source"] == "own"}
    peer = {r["date"]: r["similarity"] for r in rows if r["source"] == "peer"}
    both = set(own) & set(peer)
    assert both and all(peer[d] == pytest.approx(0.8 * own[d], abs=1.5e-4) for d in both)      # rounded to 4 places


# --- the forecast ------------------------------------------------------------------------------------------------

def synthetic_rows(n=1200, signal=0.6, seed=1):
    rng = np.random.default_rng(seed)
    dates = pd.bdate_range("2020-01-01", periods=n)
    mom = 0.02 * rng.standard_normal(n)
    sigma = np.full(n, 0.03)
    realized = signal * mom + 0.03 * rng.standard_normal(n)
    return pd.DataFrame({"symbol": "X", "date": dates, "realized": realized, "analog": 0.0, "momentum": mom, "meanrev": 0.0,
                         "drift": 0.0, "sigma": sigma, "regime": np.where(np.arange(n) % 2, "uptrend, calm", "downtrend, volatile")})


def test_weights_are_learned_on_earlier_dates_and_judged_on_later_ones():
    cal = F.fit(synthetic_rows(signal=0.8), 5)
    assert cal["directional"] and cal["weights"]["momentum"] > 0.3 and cal["status"] == "calibrated"
    assert abs(cal["coverage"] - 0.8) <= F.COVERAGE_TOLERANCE and cal["mae_pct"] < cal["baseline_mae_pct"]
    assert cal["train_cases"] > cal["test_cases"] > 0 and set(cal["by_regime"]) == {"uptrend, calm", "downtrend, volatile"}
    assert "never saw" in cal["text"]


def test_with_no_edge_the_base_path_is_flat_and_says_so():
    cal = F.fit(synthetic_rows(signal=0.0, seed=2), 5)
    assert not cal["directional"] and all(v == 0 for v in cal["weights"].values())
    assert "did not beat assuming no move by more than chance" in cal["text"] and cal["status"] in ("calibrated", "not_calibrated")
    assert F.fit(synthetic_rows(n=40), 5)["status"] == "insufficient_data"


def test_the_forecast_is_flat_until_calibrated_and_an_earnings_release_widens_the_band_from_its_day():
    s = A.prepare("XYZ", frame(walk()), None, own=True)
    fc = F.forecast(s, [s], 5, last_price=100.0)
    assert fc["calibration"]["status"] == "not_calibrated" and all(p == 0 for p in fc["base"]["path_pct"])
    assert fc["base"]["low_pct"][0] < 0 < fc["base"]["high_pct"][0] and fc["base"]["high_pct"][-1] > fc["base"]["high_pct"][0]
    ev = F.forecast(s, [s], 5, event={"in_days": 3, "move_pct": 6.0}, beta=1.1, market_sigma=0.02, last_price=100.0)
    widths = [h - lo for lo, h in zip(ev["base"]["low_pct"], ev["base"]["high_pct"])]
    assert widths[2] - widths[1] > 5 * (widths[1] - widths[0])                 # the release's move arrives on day 3
    keys = [a["key"] for a in ev["alternatives"]]
    assert keys == ["earnings_beat", "guidance_cut", "market_up", "market_down"]
    assert all("probability" not in a for a in ev["alternatives"])            # assumptions, never probabilities
    assert ev["alternatives"][0]["path_pct"][1] == ev["base"]["path_pct"][1] and ev["alternatives"][0]["path_pct"][2] > 5.9


# --- the option's own history ---------------------------------------------------------------------------------------

def test_option_symbols_are_read_and_refused():
    t = premium.parse_occ("AAPL261218C00200000")
    assert t == {"underlying": "AAPL", "expiration": "2026-12-18", "option_type": "CALL", "strike": 200.0}
    assert premium.parse_occ("brk.b270115p00450500")["strike"] == 450.5
    with pytest.raises(ValueError):
        premium.parse_occ("AAPL")


@pytest.fixture
def svc(tmp_path):
    s = Settings(data_mode="demo", db_path=tmp_path / "t.db")
    service = TradeAssistant(s, provider=DemoData(), db=Database(tmp_path / "t.db"))
    yield service
    service.db.close()


def test_without_a_traded_record_the_history_is_theoretical_and_labelled(svc):
    exp = svc.market.expirations("AAPL")[1]
    occ = svc.market.chain("AAPL", exp).contracts[5]["symbol"]
    h = premium.history(svc, occ, 60)
    assert h["kind"] == "theoretical" and "not prices anyone traded at" in " ".join(h["notes"]) and h["bars"]
    assert all(set(b) == {"t", "date", "close", "iv"} for b in h["bars"])        # no open, high or low: no one traded
    assert h["underlying"] == "AAPL" and h["underlying_bars"]


def test_a_source_with_the_contracts_record_gives_traded_bars(svc, monkeypatch):
    exp = svc.market.expirations("AAPL")[1]
    occ = svc.market.chain("AAPL", exp).contracts[5]["symbol"]
    seen = {}

    def option_history(symbol, start, end):
        seen["args"] = (symbol, start, end)
        return pd.DataFrame({"date": pd.bdate_range("2026-09-01", periods=10), "open": 3.0, "high": 3.4, "low": 2.8, "close": 3.1, "volume": 50.0})
    svc.market.provider.option_history = option_history
    monkeypatch.setattr(type(svc.market), "info", property(lambda self: SimpleNamespace(mode="live", label="Tradier (live)", realtime=True)))
    h = premium.history(svc, occ, 60)
    assert h["kind"] == "actual" and h["source"] == "Tradier (live)" and len(h["bars"]) == 10 and h["bars"][0]["open"] == 3.0
    assert seen["args"][0] == occ


# --- the strategy studio ------------------------------------------------------------------------------------------

def test_every_strategy_is_valued_under_the_same_scenarios_after_costs(svc):
    from tradeassist import factors as Fx
    from tradeassist.analysis import Analyst
    from tradeassist.chartview import ChartView
    from tradeassist.whatif import WhatIf
    w = WhatIf(svc, ChartView(svc, Analyst(svc)), Fx.Factors(svc, Fx.Stooq(enabled=False)))
    s = A.prepare("AAPL", svc.market.bars("AAPL", 4, complete=True), None, own=True)
    fc = F.forecast(s, [s], 5, event={"in_days": 2, "move_pct": 5.0}, beta=1.0, market_sigma=0.02)
    out = studio.compare(w, "AAPL", forecast=fc, horizon_days=5, direction="LONG", earnings_crush_points=8.0)
    keys = [c["key"] for c in out["candidates"]]
    assert {"shares", "long_call"} <= set(keys) and "long_straddle" in keys
    scen = [s_["key"] for s_ in out["scenarios"]]
    assert scen[0] == "base" and {"earnings_beat", "guidance_cut"} <= set(scen)
    for c in out["candidates"]:
        assert set(c["expected_pnl"]) == set(scen) and c["costs"] > 0
        assert c["expected_pnl"]["base"] == pytest.approx(c["expected_pnl_before_costs"]["base"] - c["costs"])
        assert c["executable"] == (c["key"] in studio.EXECUTABLE)
    call = next(c for c in out["candidates"] if c["key"] == "long_call")
    assert "crush" in call["iv_sensitivity"] and call["iv_sensitivity"]["crush"] < call["expected_pnl_before_costs"]["base"]
    assert out["best"] in (None, *studio.EXECUTABLE) and ("cash" in out["why"] if out["best"] is None else out["why"])


def test_the_crush_is_the_front_volatility_less_its_base():
    prof = {"event": {"available": True, "expiration": "2026-11-20", "base_vol": 0.30},
            "earnings": {"trading_days": 3}, "expirations": [{"expiration": "2026-11-20", "atm_iv": 0.42}]}
    assert studio.crush_points(prof, 5) == pytest.approx(12.0)
    assert studio.crush_points(prof, 2) is None                                # the release is after the horizon
    assert studio.crush_points(None, 5) is None


# --- the study, through the API ----------------------------------------------------------------------------------

@pytest.fixture
def client(tmp_path):
    s = Settings(data_mode="demo", db_path=tmp_path / "t.db")
    service = TradeAssistant(s, provider=DemoData(), db=Database(tmp_path / "t.db"))
    return TestClient(create_app(s, service)), service


def wait_for(client, study_id, timeout=60):
    end = time.time() + timeout
    while time.time() < end:
        s = client.get(f"/v3/studies/{study_id}").json()
        if s["status"] != "running":
            return s
        time.sleep(0.2)
    raise AssertionError("the study did not finish")


def test_a_study_gathers_the_evidence_forecasts_compares_and_is_saved(client):
    c, svc = client
    before = svc.bus.latest_id()
    r = c.post("/v3/studies", json={"symbol": "msft", "horizon_days": 5, "thesis": "swing", "direction": "LONG", "peers_add": ["ORCL"]}).json()
    assert r["status"] == "running" and r["symbol"] == "MSFT"
    s = wait_for(c, r["id"])
    st, view = s["study"], s["view"]
    assert s["status"] in ("complete", "partial") and st["symbol"] == "MSFT" and st["direction"] == "LONG"
    keys = {i["key"] for i in st["evidence"]["items"]}
    assert {"options", "trend"} <= keys and st["evidence"]["provenance"]["engines"]["forecast"] == F.MODEL_VERSION
    assert any("validated forecast" in m for m in st["evidence"]["missing"])        # nothing calibrated yet, and it says so
    assert st["forecast"]["calibration"]["status"] == "not_calibrated" and len(st["forecast"]["base"]["path_pct"]) == 5
    assert any(p["symbol"] == "ORCL" and p["source"] == "manual" for p in view["peers"]["peers"])
    assert len(view["future_times"]) == 5 and view["strategies"]["candidates"]
    assert all(c_["costs"] > 0 for c_ in view["strategies"]["candidates"])
    rec = svc.db.one("SELECT * FROM forecast_records WHERE study_id = ?", (r["id"],))
    assert rec["kind"] == "live" and rec["horizon_days"] == 5 and rec["realized_pct"] is None
    topics = [e["topic"] for e in svc.bus.since(before)]
    assert topics[0] == "study.started" and "study.completed" in topics
    listing = c.get("/v3/studies?symbol=MSFT").json()["studies"]
    assert listing[0]["id"] == r["id"]
    assert c.post("/v3/studies", json={"symbol": "MSFT", "horizon_days": 7}).status_code == 400
    assert c.post("/v3/studies", json={"symbol": "MSFT", "thesis": "yolo"}).status_code == 400


def test_a_calibration_is_learned_from_history_and_then_used_by_studies(client):
    c, svc = client
    job = c.post("/v3/forecast/calibrate", json={"horizon_days": 5, "symbols": ["AAPL", "MSFT", "KO"], "years": 3}).json()
    assert job["state"] == "running"
    assert c.post("/v3/forecast/calibrate", json={"horizon_days": 5}).status_code == 400         # one at a time
    end = time.time() + 120
    while time.time() < end:
        d = c.get("/v3/forecast/calibration?horizon_days=5").json()
        if d["job"]["state"] != "running":
            break
        time.sleep(0.5)
    assert d["job"]["state"] == "done" and d["calibration"]["rows"] > 100
    assert d["contract"]["status"] in ("calibrated", "not_calibrated", "insufficient_data") and d["calibration"]["symbols"] == ["AAPL", "MSFT", "KO"]
    s = wait_for(c, c.post("/v3/studies", json={"symbol": "AAPL", "horizon_days": 5}).json()["id"])
    assert s["study"]["forecast"]["calibration"]["status"] == d["contract"]["status"]
    other = c.get("/v3/forecast/calibration?horizon_days=10").json()
    assert other["calibration"] is None and "Not checked yet" in other["contract"]["text"]


def test_the_ai_challenges_the_thesis_in_words_only(client):
    c, svc = client
    s = wait_for(c, c.post("/v3/studies", json={"symbol": "AAPL", "horizon_days": 3, "direction": "SHORT"}).json()["id"])
    assert c.post(f"/v3/studies/{s['id']}/challenge", json={}).status_code == 400                  # no AI set up
    asked = {}

    def ask(**kw):
        asked.update(kw)
        return {"text": '{"supporting": ["Falling trend"], "contradicting": ["Strong peers", 7], "missing": [], "invalidators": ["A close above the band"], "questions": ["Why now?"]}',
                "model": "claude-x"}
    svc.ai = SimpleNamespace(ask=ask, configured=True)
    r = c.post(f"/v3/studies/{s['id']}/challenge", json={}).json()
    assert r["provider"] == "claude" and r["contradicting"] == ["Strong peers", "7"] and "no numbers" in r["note"]
    assert "Do not predict prices" in asked["prompt"] and "FORECAST (computed by the app, not by you)" in asked["prompt"]
    assert asked["web_search"] is False


def test_the_peers_and_option_history_routes(client):
    c, svc = client
    p = c.get("/v3/peers/NVDA?add=TSM&remove=AMD").json()
    assert p["sector_fund"] == "XLK" and all(x["symbol"] != "AMD" for x in p["peers"]) and p["removed"] == ["AMD"]
    assert any(x["symbol"] == "TSM" and x["why"] == "named by you" for x in p["peers"])
    assert c.get("/v3/peers/NVDA?add=not a ticker").status_code == 400
    occ = svc.market.chain("AAPL", svc.market.expirations("AAPL")[1]).contracts[3]["symbol"]
    h = c.get(f"/v3/options/history?option_symbol={occ}&days=30").json()
    assert h["kind"] == "theoretical" and h["option_symbol"] == occ
    assert c.get("/v3/options/history?option_symbol=NOPE").status_code == 400
