"""4.5.1: trading correctness, safety and recovery.

1. Expiration-aligned valuation: an option is valued against the cases' price at the session it expires (or the target
   session), never the target session's distribution when it expires first; legs of different expirations are closed
   at the first; a release's volatility collapse only from its session on; nothing extrapolated.
2. Data quality: execution, research or unavailable, from each quote's own time, two sides, spread and open interest.
3. Triggers: deterministic verification of a source (publication time from the source, after the cutoff and inside the
   window, naming the stock, supporting the claim), and fetching that refuses private addresses.
4. Idempotent finalization: a crash at every step of finishing a run resumes from what was saved, with one version,
   one valuation and one decision, and never a second provider run.
5. Cancellation and retry: the control is stopped too, late results are not applied, a paid request with an unknown
   outcome is not repeated without confirmation, and earlier validated results are kept."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import numpy as np
import pytest
import requests
from fastapi.testclient import TestClient

from tradeassist.api import create_app
from tradeassist.config import Settings
from tradeassist.db import Database
from tradeassist.providers.demo import DemoData
from tradeassist.service import TradeAssistant
from tradeassist.v3 import triggers as T
from tradeassist.v3 import valuation as V
from tradeassist.v3.scenario_providers import Step
from tests.test_one_click import Scripted, _wait, drain

NY_NOW = datetime(2026, 10, 22, 17, 0, tzinfo=timezone(timedelta(hours=-4)))
DAYS = ["2026-10-23", "2026-10-26", "2026-10-27", "2026-10-28", "2026-10-29"]


# --- 1 and 2. the valuation, on a market the test controls -----------------------------------------------------------------

def _row(sym, strike, kind, exp, bid=1.0, ask=1.1, oi=500.0, when="now", greeks=True, spread=None):
    now = datetime.now(timezone.utc).isoformat() if when == "now" else when
    mid = (bid + ask) / 2 if (bid and ask) else None
    return {"symbol": sym, "strike": strike, "option_type": kind, "expiration": exp, "bid": bid, "ask": ask, "mid": mid, "iv": 0.4,
            "model_vol": 0.4, "spread_pct": spread if spread is not None else (100 * (ask - bid) / mid if mid else None), "open_interest": oi,
            "volume": 10.0, "bid_time": now, "ask_time": now, "trade_time": now,
            "greeks": {"updated_at": datetime.now(timezone.utc).isoformat()} if greeks else {}}


class Market:
    def __init__(self, rows, quote=None, realtime=True, mode="live", clock="open"):
        self.rows, self.realtime, self.mode, self._clock = rows, realtime, mode, clock
        self.info = SimpleNamespace(mode=mode, realtime=realtime, label="Test market")
        self._quote = quote or {"last": 100.0, "bid": 99.95, "ask": 100.05, "trade_time": datetime.now(timezone.utc).isoformat(),
                                "_meta": {"freshness": "FRESH", "age_seconds": 1.0, "realtime": True}}

    def quote(self, sym):
        return dict(self._quote)

    def clock(self):
        return {"state": self._clock}


class FakeSvc:
    def __init__(self, rows, **kw):
        self.settings = Settings(data_mode="demo", opportunity_max_spread_pct=10.0)
        self.market = Market(rows, **kw)
        self.trading = None
        self.simulated = []

    def _chain_for(self, sym, exp):
        return SimpleNamespace(spot=100.0, contracts=[r for r in self.market.rows if r["expiration"] == exp])

    def simulate(self, *, symbol, legs, eval_date, range_pct, iv_shift=0.0):
        self.simulated.append({"eval_date": eval_date, "iv_shift": iv_shift, "legs": legs})
        prices = np.linspace(100 * (1 - range_pct / 100), 100 * (1 + range_pct / 100), 801)
        pnl = np.zeros_like(prices)
        for lg in legs:
            cp = 1 if lg["option_type"] == "call" else -1
            side = 1 if lg["side"] == "buy" else -1
            pnl += side * lg["contracts"] * 100 * (np.maximum(cp * (prices - lg["strike"]), 0.0) - lg["entry_price"])
        return {"curve": {"prices": prices.tolist(), "pnl_at_date": pnl.tolist()}}


def version(bull=(0.0, 0.0, 10.0, 15.0, 20.0), sd=0.01, catalyst_session=None, anchor=100.0):
    def case(key, path):
        return {"key": key, "label": key.title(), "return_pct": list(path), "log_sd": [sd] * 5, "trigger": f"{key} trigger", "quantified": True}
    return {"id": "sver_t", "symbol": "XYZ", "horizon_sessions": 5, "anchor": {"price": anchor, "bar_date": "2026-10-22"},
            "sessions": [{"index": i + 1, "date": d} for i, d in enumerate(DAYS)],
            "catalyst": {"kind": "earnings" if catalyst_session else "other", "event_date": DAYS[catalyst_session - 1] if catalyst_session else None},
            "catalyst_session": catalyst_session,
            "cases": [case("central", [0.0] * 5), case("bullish", bull), case("bearish", [-x for x in bull])]}


def study(*cands):
    return {"study": {"symbol": "XYZ", "id": "study_t"}, "view": {"strategies": {"candidates": list(cands)}}}


def call(exp, strike=110.0, key="long_call", mid=1.05):
    return {"key": key, "label": key, "legs": [{"option_symbol": f"XYZ{exp}C{strike:g}", "option_type": "call", "strike": strike,
                                                 "expiration": exp, "side": "buy", "contracts": 1, "mid": mid}]}


def test_an_option_expiring_inside_the_horizon_is_valued_at_its_own_session():
    """Scenario F: a five-session study, a contract expiring after session two. 4.5.0 valued it against the session-five
    distribution (+20% in the bullish case, a large profit); at its expiration the bullish case has not moved yet."""
    svc = FakeSvc([_row("XYZ2026-10-26C110", 110, "call", "2026-10-26")])
    res, _ = V.value(svc, study(call("2026-10-26")), version())
    s = res["structures"][0]
    assert s["eval_date"] == "2026-10-26" and s["eval_session"] == 2 and svc.simulated[-1]["eval_date"] == "2026-10-26"
    assert s["by_case"]["bullish"]["case_price"] == pytest.approx(100.0)                # session two's price, not 120
    assert s["by_case"]["bullish"]["expected_pnl"] < 0                                   # the 110 call expires worthless there
    assert any("valued at that expiration" in f for f in s["flags"])


def test_a_contract_expiring_on_the_last_session_or_after_is_valued_at_the_target():
    svc = FakeSvc([_row("XYZ2026-10-29C110", 110, "call", "2026-10-29"), _row("XYZ2026-11-06C110", 110, "call", "2026-11-06")])
    on, _ = V.value(svc, study(call("2026-10-29")), version())
    after, _ = V.value(svc, study(call("2026-11-06")), version())
    for res in (on, after):
        s = res["structures"][0]
        assert s["eval_session"] == 5 and s["eval_date"] == "2026-10-29"
        assert s["by_case"]["bullish"]["case_price"] == pytest.approx(120.0) and s["by_case"]["bullish"]["expected_pnl"] > 500


def test_legs_with_different_expirations_are_closed_at_the_first():
    svc = FakeSvc([_row("XYZ2026-11-06C105", 105, "call", "2026-11-06", bid=3.0, ask=3.2), _row("XYZ2026-10-26C110", 110, "call", "2026-10-26")])
    diag = {"key": "bull_call_spread", "label": "diagonal", "legs": [
        {"option_symbol": "XYZ2026-11-06C105", "option_type": "call", "strike": 105, "expiration": "2026-11-06", "side": "buy", "contracts": 1, "mid": 3.1},
        {"option_symbol": "XYZ2026-10-26C110", "option_type": "call", "strike": 110, "expiration": "2026-10-26", "side": "sell", "contracts": 1, "mid": 1.05}]}
    res, _ = V.value(svc, study(diag), version())
    s = res["structures"][0]
    assert s["eval_date"] == "2026-10-26" and s["eval_session"] == 2
    assert any("closed at the first expiration" in f for f in s["flags"])


def test_a_release_collapses_volatility_only_from_its_own_session(monkeypatch):
    """An earnings release at session three, between two expirations: the contract expiring before it keeps today's
    volatility; the one valued after it is valued after the collapse."""
    monkeypatch.setattr(V, "_crush_points", lambda svc, v: 8.0)
    svc = FakeSvc([_row("XYZ2026-10-26C110", 110, "call", "2026-10-26"), _row("XYZ2026-10-28C110", 110, "call", "2026-10-28")])
    before, _ = V.value(svc, study(call("2026-10-26")), version(catalyst_session=3))
    assert svc.simulated[-1]["iv_shift"] == 0.0 and before["structures"][0]["volatility"] == "today's implied volatility"
    after, _ = V.value(svc, study(call("2026-10-28")), version(catalyst_session=3))
    assert svc.simulated[-1]["iv_shift"] == -8.0 and after["structures"][0]["volatility"] == "after the release's expected collapse"


def test_what_cannot_be_valued_reliably_is_refused_not_guessed():
    # Expires before the first forecast session: the cases have no price for it.
    svc = FakeSvc([_row("XYZ2026-10-22C110", 110, "call", "2026-10-22")])
    res, _ = V.value(svc, study(call("2026-10-22")), version())
    assert res["structures"] == [] and res["failed"][0]["code"] == "cannot_value_reliably" and "before the first forecast session" in res["failed"][0]["error"]
    # A leg missing from the chain, and a leg with no midpoint now and none saved.
    svc = FakeSvc([])
    res, _ = V.value(svc, study(call("2026-10-29")), version())
    assert "not in the snapshot" in res["failed"][0]["error"]
    svc = FakeSvc([_row("XYZ2026-10-29C110", 110, "call", "2026-10-29", bid=None, ask=None)])
    res, _ = V.value(svc, study(call("2026-10-29", mid=None)), version())
    assert "no midpoint now and none saved" in res["failed"][0]["error"]
    # A case whose prices reach beyond the model's range is not extrapolated.
    svc = FakeSvc([_row("XYZ2026-10-29C110", 110, "call", "2026-10-29")])
    res, _ = V.value(svc, study(call("2026-10-29")), version(bull=(0, 0, 60, 90, 140), sd=0.3))
    assert res["structures"] == [] and "not extrapolated" in res["failed"][0]["error"]


def test_the_saved_paths_do_not_move_with_the_quote_and_the_anchor_is_the_versions():
    """A version whose anchor (95) differs from the market now (100): the case prices are the version's own."""
    svc = FakeSvc([])
    shares = {"key": "shares", "label": "Shares", "quantity": 10, "legs": []}
    res, _ = V.value(svc, study(shares), version(anchor=95.0))
    s = res["structures"][0]
    assert s["by_case"]["bullish"]["case_price"] == pytest.approx(95.0 * 1.2) and s["entry"] == pytest.approx(100.05)
    svc.market._quote = {**svc.market._quote, "last": 130.0, "bid": 129.9, "ask": 130.1}
    again, _ = V.value(svc, study(shares), version(anchor=95.0))
    assert again["structures"][0]["by_case"]["bullish"]["case_price"] == pytest.approx(95.0 * 1.2)
    assert again["structures"][0]["by_case"]["bullish"]["expected_pnl"] < s["by_case"]["bullish"]["expected_pnl"]


@pytest.mark.parametrize("row, quote, kw, code", [
    (dict(bid=1.2, ask=1.0), None, {}, "crossed_quote"),
    (dict(bid=0.0, ask=0.1), None, {}, "no_two_sided_quote"),
    (dict(when=None), None, {}, "quote_freshness_unknown"),
    (dict(bid=1.0, ask=1.4), None, {}, "wide_spread"),
    (dict(), None, {"realtime": False}, "delayed_quotes"),
    (dict(when="2026-10-20T15:00:00+00:00"), None, {}, "stale_quote"),
    (dict(oi=3.0), None, {}, "low_open_interest"),
    (dict(), None, {"clock": "closed"}, "market_closed"),
])
def test_option_quotes_that_are_not_execution_quality_say_why(row, quote, kw, code):
    when = row.pop("when", "now")
    r = _row("XYZ2026-10-29C110", 110, "call", "2026-10-29", when=when if when else "", **row)
    if when is None:
        for k in ("bid_time", "ask_time", "trade_time"):
            r[k] = None
    svc = FakeSvc([r], **kw)
    res, _ = V.value(svc, study(call("2026-10-29")), version())
    s = res["structures"][0]
    assert s["quality"]["tier"] == "research" and code in s["quality"]["reasons"], s["quality"]


def test_execution_quality_needs_everything_fresh_and_missing_greeks_are_only_flagged():
    svc = FakeSvc([_row("XYZ2026-10-29C110", 110, "call", "2026-10-29", greeks=False)])
    res, _ = V.value(svc, study(call("2026-10-29")), version())
    s = res["structures"][0]
    assert s["quality"]["tier"] == "execution" and any("no Greeks" in n for n in s["notes"])
    # The study's midpoint stands in only as research.
    svc = FakeSvc([_row("XYZ2026-10-29C110", 110, "call", "2026-10-29", bid=None, ask=None)])
    res, _ = V.value(svc, study(call("2026-10-29")), version())
    assert "study_midpoint_used" in res["structures"][0]["quality"]["reasons"]


@pytest.mark.parametrize("meta, code", [({}, "quote_freshness_unknown"), ({"freshness": "STALE", "age_seconds": 900}, "stale_quote"),
                                        ({"freshness": "FRESH", "age_seconds": 60}, "stale_quote"),
                                        ({"freshness": "DELAYED", "age_seconds": 900, "realtime": False}, "delayed_quotes")])
def test_a_stock_quote_of_unknown_or_old_age_is_never_fresh(meta, code):
    q = {"last": 100.0, "bid": 99.95, "ask": 100.05, "_meta": meta}
    svc = FakeSvc([], quote=q)
    res, _ = V.value(svc, study({"key": "shares", "label": "Shares", "quantity": 1, "legs": []}), version())
    assert code in res["structures"][0]["quality"]["reasons"] and res["freshness"]["tier"] == "research"


# --- 3. triggers ---------------------------------------------------------------------------------------------------

CUT = datetime(2026, 10, 22, 21, 0, tzinfo=timezone.utc)
DEAD = datetime(2026, 10, 29, 20, 0, tzinfo=timezone.utc)
NOW = datetime(2026, 10, 24, 12, 0, tzinfo=timezone.utc)


def page(text, published="2026-10-23T13:30:00Z", status=200):
    meta = f'<meta property="article:published_time" content="{published}">' if published else ""
    return {"url": "https://news.example.com/a", "status": status, "text": f"<html><head>{meta}</head><body>{text}</body></html>"}


def verdict(pg, claim="Guidance raised for the year", trigger="The company raises its full-year guidance"):
    return T.check(pg, symbol="XYZ", claim=claim, trigger=trigger, cutoff=CUT, deadline=DEAD, now=NOW)


def test_a_source_is_verified_only_when_it_supports_the_claim_on_its_own_terms():
    good = "XYZ Corp (NASDAQ: XYZ) raised its full-year guidance today."
    v = verdict(page(good))
    assert v["status"] == "verified" and v["published_from"].startswith("meta") and "guidance" in v["excerpt"].lower()
    assert verdict(page(good, published=None))["status"] == "unverified"                                     # no time of its own
    assert verdict(page(good, published="2026-10-22T20:00:00Z"))["status"] == "rejected"                     # before the cutoff
    assert verdict(page(good, published="2026-10-30T13:00:00Z"))["status"] == "rejected"                     # after the window
    assert "does not name XYZ" in verdict(page("Another company raised its full-year guidance."))["reason"]
    assert verdict(page("XYZ said nothing new about its quarter."))["status"] == "rejected"                 # does not support it
    assert verdict(page(good, status=404))["status"] == "unverified"
    # The time is the source's, whatever was typed; a JSON-LD time is read too.
    ld = {"url": "x", "status": 200, "text": '<script type="application/ld+json">{"datePublished": "2026-10-23T14:00:00Z"}</script>' + good}
    assert verdict(ld)["status"] == "verified" and verdict(ld)["published_from"] == "JSON-LD datePublished"
    # A lowercase common word is not the ticker.
    assert not T.names_stock("the frog jumped", "FROG") and T.names_stock("Shares of FROG rose", "FROG") and T.names_stock("$frog up", "FROG")


class Redirecting:
    def __init__(self, to):
        self.to = to

    def get(self, url, **kw):
        return SimpleNamespace(status_code=302, headers={"Location": self.to}, close=lambda: None)


@pytest.mark.parametrize("url", ["http://127.0.0.1/x", "http://localhost:5001/", "http://10.0.0.8/", "http://169.254.169.254/latest",
                                 "file:///etc/passwd", "ftp://example.com/x", "http://user:pw@example.com/", "https://example.com:8443/"])
def test_trigger_links_never_reach_this_computer_or_its_network(url, monkeypatch):
    monkeypatch.setattr(T.socket, "getaddrinfo", lambda host, port: [(None, None, None, None, ("127.0.0.1" if host == "localhost" else host
                                                                                                 if host[0].isdigit() else "93.184.216.34", 0))])
    with pytest.raises(T.Unsafe):
        T.check_url(url)


def test_a_redirect_to_a_private_address_is_refused(monkeypatch):
    monkeypatch.setattr(T.socket, "getaddrinfo", lambda host, port: [(None, None, None, None, ("10.1.2.3" if host == "internal" else "93.184.216.34", 0))])
    with pytest.raises(T.Unsafe):
        T.fetch("https://example.com/a", session=Redirecting("http://internal/admin"))


# --- 4 and 5. the pipeline: crashes, cancel, retry --------------------------------------------------------------------

class Crash(BaseException):
    """The process stopping at this point (not an error a handler could catch)."""


@pytest.fixture(scope="module")
def app(tmp_path_factory):
    path = tmp_path_factory.mktemp("v451") / "t.db"
    s = Settings(data_mode="demo", db_path=path, desk_worker="off", scenario_provider="demo", scenario_auto=False, scenario_control=False)
    svc = TradeAssistant(s, provider=DemoData(), db=Database(path))
    c = TestClient(create_app(s, svc))
    ids = [_wait(c, c.post("/v3/studies", json={"symbol": sym, "horizon_days": 5, "scenarios": False}).json()["id"])["id"]
           for sym in ("AAPL", "MSFT", "KO", "JPM", "NVDA", "AMD", "XOM", "PEP", "WMT", "CVX", "MRK", "ORCL")]
    yield c, svc, ids, s, path
    svc.db.close()


def _restart(svc):
    """What a restart does: the crashed job's lease runs out, and the pipeline looks for work left behind."""
    with svc.db.tx() as cur:
        cur.execute("UPDATE jobs SET lease_until = ? WHERE status = 'running'", ("2000-01-01T00:00:00Z",))
    svc.pipeline.recover()


def _counts(svc, sid):
    runs = svc.db.query("SELECT id FROM scenario_runs WHERE study_id = ?", (sid,))
    ids = [r["id"] for r in runs]
    q = ",".join("?" * len(ids)) or "''"
    versions = svc.db.one(f"SELECT COUNT(*) AS n FROM scenario_versions WHERE run_id IN ({q})", ids)["n"]
    vals = svc.db.one("SELECT COUNT(*) AS n FROM scenario_valuations WHERE study_id = ?", (sid,))["n"]
    decs = svc.db.one("SELECT COUNT(*) AS n FROM scenario_decisions WHERE study_id = ?", (sid,))["n"]
    return {"runs": len(ids), "versions": versions, "valuations": vals, "decisions": decs}


def _crash_once(monkeypatch, obj, name, when=lambda *a, **k: True):
    real = getattr(obj, name)
    state = {"done": False}

    def boom(*a, **k):
        if not state["done"] and when(*a, **k):
            state["done"] = True
            raise Crash(name)
        return real(*a, **k)
    monkeypatch.setattr(obj, name, boom)
    return state


POINTS = ["report", "validated", "version", "version_saved", "valuation", "findings"]


@pytest.mark.parametrize("point", POINTS)
def test_a_crash_while_finishing_a_paid_run_resumes_with_one_result_and_no_new_run(app, monkeypatch, point):
    """Scenario D, at every step of finishing a run: report received, cases validated, before and after the version is
    saved, during the valuation and before the findings are published. After the restart: one provider run, one
    automatic version, one valuation and one decision, and the pipeline complete."""
    c, svc, ids, _, _ = app
    sid = ids[POINTS.index(point)]
    pl = svc.pipeline
    target = {"report": (pl, "cases_from", lambda *a, **k: True),
              "validated": (pl, "_finalized", lambda r, state: state == "validated"),
              "version": (svc.scenarios, "create_version", lambda *a, **k: True),
              "version_saved": (pl, "_finalized", lambda r, state: state == "version_saved"),
              "valuation": (V, "value", lambda *a, **k: True),
              "findings": (svc, "on_scenario_findings", lambda *a, **k: True)}[point]
    hit = _crash_once(monkeypatch, *target)
    assert c.post(f"/v3/studies/{sid}/pipeline/start", json={}).status_code == 200
    with pytest.raises(Crash):
        drain(svc)
    assert hit["done"]
    _restart(svc)
    drain(svc)
    p = svc.pipeline.view(sid)
    assert p["status"] == "complete" and p["version_id"] and p["valuation"] and p["decision"], (point, p["status"], p["error"])
    assert p["finalize"]["state"] == "complete"
    n = _counts(svc, sid)
    assert n == {"runs": 1, "versions": 1, "valuations": 1, "decisions": 1}, (point, n)
    # A late duplicate of the finishing job finds everything done.
    svc.pipeline.finish({"run_id": p["run"]["id"]})
    assert _counts(svc, sid) == n


def test_a_restart_of_the_whole_app_resumes_finishing(app, monkeypatch):
    c, svc, ids, s, path = app
    sid = ids[6]
    _crash_once(monkeypatch, svc.scenarios, "create_version")
    c.post(f"/v3/studies/{sid}/pipeline/start", json={})
    with pytest.raises(Crash):
        drain(svc)
    with svc.db.tx() as cur:
        cur.execute("UPDATE jobs SET lease_until = ? WHERE status = 'running'", ("2000-01-01T00:00:00Z",))
    again = TradeAssistant(s, provider=DemoData(), db=Database(path))
    TestClient(create_app(s, again))                    # the install looks for left-over work
    drain(again)
    p = again.pipeline.view(sid)
    assert p["status"] == "complete" and _counts(again, sid)["runs"] == 1 and _counts(again, sid)["versions"] == 1
    again.db.close()


def test_retry_after_a_failed_finish_reads_the_saved_report_again_without_a_new_run(app, monkeypatch, provider_override=None):
    c, svc, ids, _, _ = app
    sid = ids[7]
    monkeypatch.setattr(svc.pipeline, "cases_from", lambda r, seed, ext: (None, "no cases in the template, and no AI to convert it"))
    c.post(f"/v3/studies/{sid}/pipeline/start", json={})
    drain(svc)
    p = svc.pipeline.view(sid)
    assert p["status"] == "needs_attention" and "Retry reads it again without a new run" in p["error"] and p["run"]["report_received"]
    monkeypatch.undo()
    p = c.post(f"/v3/studies/{sid}/pipeline/retry", json={}).json()["pipeline"]
    assert p["attempt"] == 1 and p["status"] == "running"
    drain(svc)
    p = svc.pipeline.view(sid)
    assert p["status"] == "complete" and _counts(svc, sid)["runs"] == 1


def test_a_paid_request_with_an_unknown_outcome_is_not_repeated_without_confirmation(app):
    c, svc, ids, _, _ = app
    sid = ids[8]
    svc.scenario_provider_override = Scripted(lambda ctx, state: Step(state, attention="The start request timed out and the simulation is not running.",
                                                                      stage="simulation"))
    try:
        c.post(f"/v3/studies/{sid}/pipeline/start", json={})
        drain(svc)
        p = svc.pipeline.view(sid)
        assert p["status"] == "needs_attention" and p["retry_needs_confirmation"]
        r = c.post(f"/v3/studies/{sid}/pipeline/retry", json={})
        assert r.status_code == 400 and "confirm a new run" in r.json()["error"]
        assert _counts(svc, sid)["runs"] == 1
        from tradeassist.v3.scenario_providers import Demo
        svc.scenario_provider_override = Demo()
        p = c.post(f"/v3/studies/{sid}/pipeline/retry", json={"confirm_new_run": True}).json()["pipeline"]
        assert p["attempt"] == 2
        drain(svc)
    finally:
        svc.scenario_provider_override = None
    p = svc.pipeline.view(sid)
    assert p["status"] == "complete" and _counts(svc, sid)["runs"] == 2
    assert p["history"][-1]["attempt"] == 1 and p["history"][-1]["status"] == "needs_attention"


def test_cancel_stops_the_control_too_and_nothing_late_is_applied(app, monkeypatch):
    c, svc, ids, _, _ = app
    sid = ids[9]
    # A finished study first: its validated version, valuation and decision must survive a later canceled attempt.
    c.post(f"/v3/studies/{sid}/pipeline/start", json={})
    drain(svc)
    first = svc.pipeline.view(sid)
    assert first["status"] == "complete"
    calls = []
    monkeypatch.setattr(svc.pipeline, "_start_control", lambda study_id, seed, main, attempt: calls.append(study_id) or None)
    svc.scenario_provider_override = Scripted(lambda ctx, state: Step({**state, "polls": 1}, next_in=60.0, stage="simulation", detail="round 1"))
    try:
        p = c.post(f"/v3/studies/{sid}/pipeline/retry", json={}).json()["pipeline"]
        svc.pipeline.start({"study_id": sid})
        drain(svc, rounds=2)
        p = svc.pipeline.load(sid)
        run_id = p["run_id"]
        # A control run beside it, still going.
        ctrl = "srun_ctrl_test"
        with svc.db.tx() as cur:
            cur.execute("""INSERT INTO scenario_runs (id, seed_id, study_id, provider, status, created_at, schema_version, prompt_version, config_json,
                           external_json) SELECT ?, seed_id, study_id, 'ai', 'running', created_at, schema_version, prompt_version, config_json, ?
                           FROM scenario_runs WHERE id = ?""", (ctrl, json.dumps({"role": "control"}), run_id))
        svc.pipeline._update(sid, control_run_id=ctrl)
        v = c.post(f"/v3/studies/{sid}/pipeline/cancel", json={}).json()["pipeline"]
        assert v["status"] == "canceled"
        assert svc.db.one("SELECT status FROM scenario_runs WHERE id = ?", (ctrl,))["status"] == "canceled"
        assert svc.db.one("SELECT COUNT(*) AS n FROM jobs WHERE status = 'queued' AND payload_json LIKE ?", (f'%{run_id}%',))["n"] == 0
        # A report that arrives now is not applied, and neither is a finishing pass.
        with svc.db.tx() as cur:
            cur.execute("UPDATE scenario_runs SET report_text = 'late' WHERE id = ?", (run_id,))
        assert "skipped" in svc.pipeline.finish({"run_id": run_id})
    finally:
        svc.scenario_provider_override = None
    p = svc.pipeline.view(sid)
    assert p["status"] == "canceled" and p["history"][-1]["version_id"] == first["version_id"]
    # What was validated before is still there.
    assert svc.db.one("SELECT 1 FROM scenario_versions WHERE id = ?", (first["version_id"],))
    assert svc.db.one("SELECT 1 FROM scenario_decisions WHERE id = ?", (first["decision"]["id"],))


def test_a_simulation_stopped_early_on_the_server_says_so():
    from tests.test_one_click import FakeMiroFish, fake_mirofish
    fake = FakeMiroFish()
    mf = fake_mirofish(fake)
    out = mf._run_wait({}, {"stage": "run_wait", "simulation_id": "sim_1"})
    assert out.stage == "simulation" and "stopped early" not in out.detail          # still running: polled
    fake.seen["run"] = 1
    real = fake.request

    def stopped(method, url, **kw):
        if url.endswith("/run-status"):
            from tests.test_one_click import Resp
            return Resp(200, {"success": True, "data": {"runner_status": "stopped", "current_round": 4, "total_rounds": 10}})
        return real(method, url, **kw)
    fake.request = stopped
    out = mf._run_wait({}, {"stage": "run_wait", "simulation_id": "sim_1"})
    assert out.state["stopped_early"] and out.state["rounds"] == 4 and "stopped early, after 4 of 10" in out.detail


def test_the_migration_marks_old_triggers_unverified(tmp_path):
    db = Database(tmp_path / "m.db")
    cols = {r[1] for r in db.query("PRAGMA table_info(scenario_triggers)")}
    assert {"status", "claimed_published_at", "published_at", "event_at", "retrieved_at", "verified_at", "cutoff", "deadline"} <= cols
    idx = {r[1] for r in db.query("PRAGMA index_list(scenario_versions)")}
    assert "idx_scenario_versions_one_auto" in idx
    status = next(r for r in db.query("PRAGMA table_info(scenario_triggers)") if r[1] == "status")
    assert status[4] == "'unverified'"                     # a row 4.5.0 wrote (or writes, after a rollback) is unverified
    db.close()
    _ = requests


# --- regressions from the independent review ------------------------------------------------------------------------

@pytest.mark.parametrize("text, status", [
    ("XYZ misses earnings estimates and cuts its full-year guidance.", "rejected"),
    ("XYZ did not beat earnings estimates and did not raise its guidance.", "rejected"),
    ("Preview: will XYZ beat earnings estimates and raise guidance?", "unverified"),
    ("XYZ is expected to beat earnings estimates and raise guidance next week.", "unverified"),
    ("XYZ beat earnings estimates but cut its full-year guidance.", "rejected"),
    ("XYZ beat earnings estimates and raised its full-year guidance on Thursday.", "verified"),
])
def test_a_source_that_negates_contradicts_or_only_previews_the_trigger_does_not_verify_it(text, status):
    v = verdict(page(text), claim="XYZ beats estimates and raises guidance", trigger="The company beats earnings estimates and raises guidance")
    assert v["status"] == status, (text, v["reason"], v.get("match"))


def test_the_fetch_connects_to_the_address_it_checked_and_shared_addresses_are_refused(monkeypatch):
    answers = iter(["93.184.216.34", "127.0.0.1", "127.0.0.1"])           # a rebinding: public for the check, loopback after
    monkeypatch.setattr(T.socket, "getaddrinfo", lambda host, port: [(None, None, None, None, (next(answers), 0))])
    seen = []

    def pinned(url, ip):
        seen.append(ip)
        return SimpleNamespace(status_code=200, headers={"Content-Type": "text/html"}, iter_content=lambda n: iter([b"<p>ok</p>"]), close=lambda: None)
    monkeypatch.setattr(T, "_pinned_get", pinned)
    out = T.fetch("https://news.example.com/a")
    assert seen == ["93.184.216.34"] and out["status"] == 200                # the name was not resolved a second time
    monkeypatch.setattr(T.socket, "getaddrinfo", lambda host, port: [(None, None, None, None, ("100.64.1.2", 0))])
    with pytest.raises(T.Unsafe):
        T.check_url("https://cgnat.example.com/")


def test_unreported_open_interest_is_not_execution_quality():
    svc = FakeSvc([_row("XYZ2026-10-29C110", 110, "call", "2026-10-29", oi=None)])
    res, _ = V.value(svc, study(call("2026-10-29")), version())
    assert "open_interest_unknown" in res["structures"][0]["quality"]["reasons"]


def test_a_canceled_attempt_is_not_valued_or_decided_again(app):
    c, svc, ids, _, _ = app
    sid = ids[10]
    svc.scenario_provider_override = Scripted(lambda ctx, state: Step({**state, "polls": 1}, next_in=60.0, stage="simulation", detail="round 1"))
    try:
        c.post(f"/v3/studies/{sid}/pipeline/start", json={})
        drain(svc, rounds=2)
        c.post(f"/v3/studies/{sid}/pipeline/cancel", json={})
    finally:
        svc.scenario_provider_override = None
    p = svc.pipeline.load(sid)
    svc.pipeline._update(sid, version_id="sver_from_the_canceled_attempt")       # as if it had been saved just before Cancel
    r = c.post(f"/v3/studies/{sid}/pipeline/revalue", json={})
    assert r.status_code == 400 and "canceled" in r.json()["error"]
    assert svc.pipeline.load(sid)["status"] == "canceled" and p["status"] == "canceled"


def test_retry_says_honestly_whether_it_reuses_the_report(app, monkeypatch):
    c, svc, ids, _, _ = app
    sid = ids[11]
    svc.scenario_provider_override = Scripted(lambda ctx, state: Step({"stage": "done"}, report="x", cases={"cases": "not a list"}, stage="report"))
    try:
        c.post(f"/v3/studies/{sid}/pipeline/start", json={})
        drain(svc)
    finally:
        svc.scenario_provider_override = None
    p = svc.pipeline.view(sid)
    assert p["status"] == "needs_attention" and p["retry_reuses_report"] is False          # its cases are unusable: a new run


def test_a_verified_trigger_does_not_carry_over_to_another_version(app):
    c, svc, ids, _, _ = app
    sid = ids[0]
    p = svc.pipeline.load(sid)
    src = {"title": "Guidance raised", "url": "https://x.example/a"}
    with svc.db.tx() as cur:
        cur.execute("""INSERT INTO scenario_triggers (study_id, case_key, observed_at, recorded_at, source_json, status, verification_json, version_id)
                       VALUES (?, 'bullish', 'x', 'y', ?, 'verified', ?, ?) ON CONFLICT(study_id, case_key) DO UPDATE SET status = 'verified',
                       source_json = excluded.source_json, verification_json = excluded.verification_json, version_id = excluded.version_id""",
                    (sid, json.dumps(src), json.dumps({"checked_url": src["url"]}), p["version_id"]))
    assert svc.triggers.for_study(sid, p["version_id"])["bullish"]["status"] == "verified"
    assert svc.triggers.for_study(sid, "sver_a_newer_one")["bullish"]["status"] == "unverified"
    # A source replaced without a new check (what the 4.5.0 code does after a rollback) is not verified either.
    with svc.db.tx() as cur:
        cur.execute("UPDATE scenario_triggers SET source_json = ? WHERE study_id = ?", (json.dumps({**src, "url": "https://y.example/b"}), sid))
    assert svc.triggers.for_study(sid, p["version_id"])["bullish"]["status"] == "unverified"


def test_a_crash_while_publishing_or_right_after_saving_the_valuation_is_finished_once(app, monkeypatch):
    c, svc, ids, _, _ = app
    calls = {"publish": 0}
    real_publish = svc.opportunities.publish

    def counted(*a, **k):
        calls["publish"] += 1
        if calls["publish"] == 1:
            raise Crash("stopped while publishing")
        return real_publish(*a, **k)
    monkeypatch.setattr(svc.opportunities, "publish", counted)
    sid = ids[1]
    svc.pipeline.retry(sid, confirm_new_run=True)
    with pytest.raises(Crash):
        drain(svc)
    _restart(svc)
    drain(svc)
    p = svc.pipeline.view(sid)
    assert p["status"] == "complete" and calls["publish"] >= 2                # published again on resume (it is idempotent)
    decs = svc.db.query("SELECT * FROM scenario_decisions WHERE study_id = ? AND valuation_id = ?", (sid, p["valuation"]["id"]))
    assert len(decs) == 1
    if decs[0]["status"] in ("qualified", "watch"):
        assert decs[0]["opportunity_id"] and svc.db.one("SELECT COUNT(*) AS n FROM opportunity_observations WHERE source = 'study' AND source_key LIKE ?",
                                                        (f"scenario:{sid}:%",))["n"] == 1
    monkeypatch.undo()
    # Saved, then stopped before the pipeline recorded it: the resumed pass finds that valuation and adds none.
    real_save = V.save
    state = {"n": 0}

    def save_then_crash(*a, **k):
        vid = real_save(*a, **k)
        if not state["n"]:
            state["n"] = 1
            raise Crash("stopped right after saving the valuation")
        return vid
    monkeypatch.setattr(V, "save", save_then_crash)
    sid = ids[2]
    before = svc.db.one("SELECT COUNT(*) AS n FROM scenario_valuations WHERE study_id = ?", (sid,))["n"]
    svc.pipeline.retry(sid, confirm_new_run=True)
    with pytest.raises(Crash):
        drain(svc)
    _restart(svc)
    drain(svc)
    assert svc.pipeline.view(sid)["status"] == "complete"
    assert svc.db.one("SELECT COUNT(*) AS n FROM scenario_valuations WHERE study_id = ?", (sid,))["n"] == before + 1
