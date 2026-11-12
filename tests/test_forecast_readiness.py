"""4.2 Tradier and forecast readiness: the calibration's three-way split with purged labels, the honest readiness
states (pending, failed, insufficient, no edge with the range shown, coverage off target), the durable calibration
job (queued, run, deferred by a used-up quota, kept beside a usable result when it finds too little), the demo to
live transition, Tradier's quota handling and the read-only data check."""

from __future__ import annotations

import dataclasses
import json
import time
from datetime import datetime, timedelta, timezone

import numpy as np
import pandas as pd
import pytest
from fastapi.testclient import TestClient

from tests.test_research3 import synthetic_rows
from tests.test_tradier import CHAIN_BODY, FakeResponse, FakeSession
from tradeassist import forecast as F
from tradeassist.api import create_app
from tradeassist.config import Settings
from tradeassist.db import Database
from tradeassist.jobs import Deferred, Jobs, Worker
from tradeassist.market import Market
from tradeassist.providers.base import DataError, QuotaExceeded
from tradeassist.providers.demo import DemoData
from tradeassist.providers.tradier import TradierData
from tradeassist.service import TradeAssistant
from tradeassist.tradier_check import check_tradier
from tradeassist.v3.workers import workers_for


# --- the fit ----------------------------------------------------------------------------------------------------

def test_the_fit_purges_outcomes_that_overlap_the_next_period_and_reports_from_the_holdout():
    rows = synthetic_rows(signal=0.8)
    cal = F.fit(rows, 5)
    d = cal["diagnostics"]
    assert cal["method"] == F.CALIBRATION_METHOD and cal["purged_cases"] == d["purged_cases"] > 0
    assert cal["train_cases"] > cal["validation_cases"] > 0 and cal["test_cases"] == d["holdout_cases"] > 0
    v_start, h_start = pd.Timestamp(d["validation_start"]), pd.Timestamp(d["holdout_start"])
    from tradeassist.marketdays import add_trading_days
    train = rows[rows["date"] < v_start]
    kept = [t for t in train["date"] if pd.Timestamp(add_trading_days(t.date(), 5)) < v_start]
    assert cal["train_cases"] == len(kept) < len(train)                      # those whose outcome lands in validation are gone
    assert h_start > v_start and cal["directional"] and cal["reason"] == "directional_edge"


def test_a_direction_that_does_not_hold_on_the_holdout_is_not_used():
    rows = synthetic_rows(n=1500, signal=0.9, seed=4)
    late = rows.index >= int(len(rows) * 0.8)
    rows.loc[late, "realized"] = -0.9 * rows.loc[late, "momentum"] + 0.03 * np.random.default_rng(9).standard_normal(int(late.sum()))
    cal = F.fit(rows, 5)
    assert cal["directional_validation"] and not cal["directional"] and all(v == 0 for v in cal["weights"].values())
    assert "not on the final holdout" in cal["text"]
    rd = F.readiness(cal, 5)
    assert rd["directional"]["status"] == "not_held" and rd["directional"]["label"] == "No reliable directional edge"


def test_too_few_dates_or_cases_are_explicit_outcomes():
    few = synthetic_rows(n=40)
    assert F.fit(few, 5)["reason"] == "too_few_cases"
    many_symbols_one_week = pd.concat([synthetic_rows(n=20, seed=i).assign(symbol=f"S{i}") for i in range(10)])
    cal = F.fit(many_symbols_one_week, 5)
    assert cal["status"] == "insufficient_data" and cal["reason"] == "too_few_dates" and "distinct dates" in cal["text"]
    rd = F.readiness({**cal, "run_at": "2026-11-01T00:00:00Z"}, 5)
    assert rd["reason"] == "insufficient_history" and rd["directional"]["label"] == "Insufficient history"


# --- readiness ----------------------------------------------------------------------------------------------------

def test_readiness_says_each_state_in_words():
    assert F.readiness(None, 5, {"state": "queued"})["label"] == "Five-session calibration pending"
    assert F.readiness(None, 1, {"state": "running"})["label"] == "One-session calibration running"
    failed = F.readiness(None, 5, {"state": "failed", "error": "boom"})
    assert failed["state"] == "failed" and failed["reason"] == "calibration_failed" and "boom" in failed["text"]
    no_edge = F.readiness(F.fit(synthetic_rows(signal=0.0, seed=2), 5), 5)
    assert no_edge["label"] == "No reliable directional edge · range shown" and no_edge["range"]["status"] == "validated"
    assert no_edge["sample"]["holdout_cases"] > 0 and no_edge["sample"]["purged_cases"] > 0
    narrow = F.fit(synthetic_rows(signal=0.0, seed=2), 5)
    narrow.update(status="not_calibrated", coverage=0.62)
    rd = F.readiness(narrow, 5)
    assert rd["range"]["label"] == "Range coverage below target" and "62%" in rd["range"]["text"]
    assert F.readiness({**narrow, "horizon_days": 1}, 5)["state"] == "pending"            # another horizon's is not this one's
    old = {**F.fit(synthetic_rows(signal=0.0, seed=2), 5)}
    old.pop("method")
    assert F.readiness(old, 5)["refresh_due"] and "3.0 fit" in F.readiness(old, 5)["method_note"]


# --- the durable calibration job -------------------------------------------------------------------------------

@pytest.fixture
def app(tmp_path):
    import tradeassist.api as api_mod
    api_mod.TEST_HOSTS = ("testserver",)
    s = Settings(data_mode="demo", db_path=tmp_path / "t.db", desk_worker="off")
    svc = TradeAssistant(s, provider=DemoData(), db=Database(tmp_path / "t.db"))
    yield TestClient(create_app(s, svc)), svc
    svc.db.close()


def _as_mode(svc, mode):
    """The same demo prices, presented as another data source (the demo to live transition)."""
    svc.market.provider.info = dataclasses.replace(svc.market.provider.info, mode=mode, label=f"{mode} (test)")


def test_a_calibration_is_a_durable_job_per_horizon_and_never_overwrites_a_usable_one_with_too_little(app, monkeypatch):
    c, svc = app
    rw = workers_for(svc)
    job = c.post("/v3/forecast/calibrate", json={"horizon_days": 1, "symbols": ["AAPL", "MSFT", "KO"], "years": 3}).json()
    assert job["state"] == "queued" and job["job_id"]
    assert c.post("/v3/forecast/calibrate", json={"horizon_days": 1}).status_code == 400
    assert c.post("/v3/forecast/calibrate", json={"horizon_days": 5, "symbols": ["AAPL", "KO"], "years": 3}).status_code == 200
    assert c.get("/v3/forecast/calibration?horizon_days=1").json()["readiness"]["label"] == "One-session calibration pending"
    done = rw.drain("calibration")
    assert [d["status"] for d in done] == ["done", "done"]
    one = c.get("/v3/forecast/calibration?horizon_days=1").json()
    five = c.get("/v3/forecast/calibration?horizon_days=5").json()
    assert one["job"]["state"] == five["job"]["state"] == "done" and one["calibration"]["horizon_days"] == 1 and five["calibration"]["horizon_days"] == 5
    assert one["readiness"]["state"] == "ready" and one["calibration"]["cutoff"] and one["calibration"]["requested"] == ["AAPL", "MSFT", "KO"]
    # A refresh with stocks that cannot be read is listed stock by stock, and is kept beside the usable result.
    before = svc.db.cache_get("forecast:calibration:demo:1")
    real = svc.market.bars

    def bars(sym, years=3.0, complete=False):
        if sym in ("ZZZZQ", "QQQQZ"):
            raise DataError(f"no history for {sym}")
        return real(sym, years, complete=complete)
    monkeypatch.setattr(svc.market, "bars", bars)
    svc.research3.queue_calibration(1, symbols=["ZZZZQ", "QQQQZ"], years=3)
    rw.drain("calibration")
    after = c.get("/v3/forecast/calibration?horizon_days=1").json()["calibration"]
    assert after["run_at"] == before["run_at"] and after["last_attempt"]["status"] == "insufficient_data"
    assert {f["symbol"] for f in after["last_attempt"]["failed"]} >= {"ZZZZQ", "QQQQZ"}
    monkeypatch.setattr(svc.market, "bars", real)
    # A study made now uses it and says so in two parts; a finished study keeps what it was made with.
    sid = c.post("/v3/studies", json={"symbol": "AAPL", "horizon_days": 1}).json()["id"]
    end = time.time() + 60
    while time.time() < end and c.get(f"/v3/studies/{sid}").json()["status"] == "running":
        time.sleep(0.2)
    rd = c.get(f"/v3/studies/{sid}").json()["study"]["forecast"]["readiness"]
    assert rd["state"] == "ready" and rd["directional"]["status"] in ("edge", "no_edge", "not_held") and rd["sample"]["cases"] > 0


def test_from_demo_to_live_the_calibration_is_a_new_one_and_is_warmed_in_the_background(app):
    c, svc = app
    rw = workers_for(svc)
    assert svc.research3.warm()["skipped"].startswith("demo")                  # demo prices are never calibrated by themselves
    svc.db.cache_put("forecast:calibration:demo:5", {**F.fit(synthetic_rows(signal=0.0, seed=2), 5), "run_at": datetime.now(timezone.utc).isoformat()})
    assert svc.research3.usable_calibration(5) is not None
    _as_mode(svc, "live")
    assert svc.research3.usable_calibration(5) is None                          # demo's is not live's
    got = svc.research3.warm()
    assert got["queued"] == [1, 5] and svc.research3.warm()["queued"] == []    # once: already queued
    keys = [r["dedupe_key"] for r in svc.db.query("SELECT dedupe_key FROM jobs WHERE kind = 'research.calibrate' ORDER BY id")]
    assert len(keys) == 2 and all(k.startswith("research.calibrate:live:") and ":warm:" in k for k in keys)
    assert c.get("/v3/forecast/calibration?horizon_days=5").json()["readiness"]["label"] == "Five-session calibration pending"
    _as_mode(svc, "demo")                                                       # the data source changed before it ran
    out = rw.drain("calibration")
    assert all(o["result"] == {"skipped": "the data source changed"} for o in out)


def test_a_used_up_quota_defers_the_calibration_without_counting_an_attempt(app, monkeypatch):
    c, svc = app
    rw = workers_for(svc)
    reset = time.time() + 120

    def boom(*a, **k):
        raise QuotaExceeded("quota", retry_at=reset)
    monkeypatch.setattr(svc.research3, "calibrate_now", boom)
    job = c.post("/v3/forecast/calibrate", json={"horizon_days": 5}).json()
    out = rw.drain("calibration")
    assert out[0]["status"] == "deferred"
    row = svc.db.one("SELECT status, attempts, due_at FROM jobs WHERE id = ?", (job["job_id"],))
    assert row["status"] == "queued" and row["attempts"] == 0 and datetime.fromisoformat(row["due_at"]).timestamp() == pytest.approx(reset, abs=2)
    view = c.get("/v3/forecast/calibration?horizon_days=5").json()["job"]
    assert view["state"] == "deferred" and view["until"]
    assert rw.drain("calibration") == []                                        # not due until the reset


def test_a_failed_calibration_says_why_and_can_be_retried(app, monkeypatch):
    c, svc = app
    rw = workers_for(svc)
    monkeypatch.setattr(svc.research3, "calibrate_now", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("disk full")))
    c.post("/v3/forecast/calibrate", json={"horizon_days": 5})
    rw.drain("calibration")
    d = c.get("/v3/forecast/calibration?horizon_days=5").json()
    assert d["job"]["state"] == "failed" and "disk full" in d["job"]["error"] and d["readiness"]["reason"] == "calibration_failed"
    assert c.post("/v3/forecast/calibrate", json={"horizon_days": 5}).json()["state"] == "queued"     # Retry


def test_a_long_job_renews_its_lease_and_a_deferred_one_keeps_its_attempts(tmp_path):
    db = Database(tmp_path / "j.db")
    seen = {}

    def slow(payload):
        before = db.one("SELECT lease_until FROM jobs WHERE kind = 'slow'")["lease_until"]
        time.sleep(1.1)
        assert w.touch(600)
        seen["lease"] = (before, db.one("SELECT lease_until FROM jobs WHERE kind = 'slow'")["lease_until"])
        return {"ok": True}

    def later(payload):
        raise Deferred(datetime.now(timezone.utc) + timedelta(minutes=5), "not yet")
    w = Worker(db, {"slow": slow, "later": later}, lease_seconds=60)
    Jobs(db).enqueue("slow", {})
    Jobs(db).enqueue("later", {}, max_attempts=1)
    out = w.drain()
    assert [o["status"] for o in out] == ["done", "deferred"] and seen["lease"][1] > seen["lease"][0]
    r = db.one("SELECT status, attempts FROM jobs WHERE kind = 'later'")
    assert r["status"] == "queued" and r["attempts"] == 0                       # never failed for its quota
    db.close()


# --- Tradier: the quota and the data check ---------------------------------------------------------------------

def test_a_quota_violation_pauses_requests_until_the_reset_and_an_ordinary_400_fails_at_once():
    expiry = int((time.time() + 600) * 1000)
    session = FakeSession([FakeResponse(400, None, {"X-Ratelimit-Expiry": str(expiry)}, text="Quota Violation"),
                           FakeResponse(400, None, {}, text="Invalid symbol")])
    t = TradierData("token-123", sandbox=False, session=session)
    with pytest.raises(QuotaExceeded) as exc:
        t.quotes(["AAPL"])
    assert exc.value.retry_at == pytest.approx(expiry / 1000, abs=1) and len(session.calls) == 1
    with pytest.raises(QuotaExceeded):
        t.quotes(["AAPL"])                                                      # refused here, without asking Tradier again
    assert len(session.calls) == 1 and t.rate_state()["quota_reset_at"] == pytest.approx(expiry / 1000, abs=1)
    t.quota_reset_at, t._paused_until = None, 0.0
    with pytest.raises(DataError) as plain:
        t.quotes(["ZZZZ"])
    assert not isinstance(plain.value, QuotaExceeded) and "Invalid symbol" in str(plain.value) and len(session.calls) == 2


def test_a_quota_violation_is_not_counted_as_a_failing_source(tmp_path):
    session = FakeSession([FakeResponse(400, None, {}, text="Quota Violation")] * 6)
    t = TradierData("token-123", sandbox=False, session=session)
    s = Settings(data_mode="demo", db_path=tmp_path / "m.db")
    m = Market(t, Database(tmp_path / "m.db"), s)
    for _ in range(2):
        with pytest.raises(QuotaExceeded):
            m._call("quote AAPL", lambda: t.quotes(["AAPL"]))
        t.quota_reset_at, t._paused_until = None, 0.0
    assert m.health.consecutive_failures == 0 and not m.health.paused()


def _fake_tradier(status=200):
    now_ms = int(time.time() * 1000)
    day = datetime.now(timezone.utc).date()
    future = (day + timedelta(days=9)).isoformat()
    chain = json.loads(json.dumps(CHAIN_BODY))
    for o in chain["options"]["option"]:
        o["expiration_date"] = future
    chain["options"]["option"][0]["greeks"]["updated_at"] = (datetime.now(timezone.utc) - timedelta(minutes=30)).strftime("%Y-%m-%d %H:%M:%S")
    if status != 200:
        return FakeSession([FakeResponse(status, None, {}, text="Invalid Access Token")] * 6)
    return FakeSession([
        FakeResponse(200, {"quotes": {"quote": {"symbol": "SPY", "last": 600.1, "bid": 600.0, "ask": 600.2, "volume": 1e6,
                                                "trade_date": now_ms - 30_000}}}),
        FakeResponse(200, {"history": {"day": [{"date": (day - timedelta(days=i)).isoformat(), "open": 1, "high": 2, "low": 0.5, "close": 1.5,
                                                "volume": 100} for i in range(10, 0, -1)]}}),
        FakeResponse(200, {"expirations": {"date": [future]}}),
        FakeResponse(200, chain),
        FakeResponse(200, {"clock": {"state": "open", "description": "Market is open"}}),
    ])


def test_check_tradier_data_reports_each_request_and_the_freshness_of_quotes_and_greeks(tmp_path, monkeypatch):
    s = Settings(data_mode="demo", db_path=tmp_path / "t.db", desk_worker="off")
    svc = TradeAssistant(s, provider=DemoData(), db=Database(tmp_path / "t.db"))
    assert check_tradier(svc)["configured"] is False                            # no token: says what to put in .env
    t = TradierData("token-123", sandbox=False, session=_fake_tradier())
    svc.market.provider = t
    r = check_tradier(svc, "spy")
    assert r["configured"] and r["ok"] and r["active"] and r["realtime"] and r["provider"] == "Tradier live data"
    by = {c["key"]: c for c in r["checks"]}
    assert set(by) == {"quote", "history", "expirations", "chain", "clock"}
    assert 0 <= by["quote"]["age_seconds"] < 120 and by["history"]["bars"] == 10 and by["chain"]["contracts"] == 2 and by["chain"]["two_sided"] == 1
    assert 1500 < by["chain"]["greeks_age_seconds"] < 2100 and "refreshes them about hourly" in by["chain"]["text"]
    assert "token-123" not in json.dumps(r)
    svc.market.provider = TradierData("token-123", sandbox=False, session=_fake_tradier(401))
    bad = check_tradier(svc)
    assert not bad["ok"] and all(not c["ok"] for c in bad["checks"]) and "TRADIER_LIVE_TOKEN" in bad["checks"][0]["error"]
    svc.db.close()


def test_the_data_check_route_is_read_only_and_says_when_nothing_is_set_up(app):
    c, svc = app
    r = c.post("/v3/data/tradier-check", json={"symbol": "SPY"}).json()
    assert r["configured"] is False and "TRADIER_LIVE_TOKEN" in r["message"]
    assert c.post("/v3/data/tradier-check", json={"symbol": "SPY; rm"}).status_code == 422
