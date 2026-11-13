"""4.4 opportunities from scenarios: the deterministic evaluator (Qualified setup, Watch for trigger, No setup) on
synthetic valuations, then the whole path through the API on demo data: the decision recorded with its reasons, one
scenario observation in Discover (never eligible for the autopilot), one notice and one notification per material
change, a trigger recorded with its independent source, and drafts that carry the scenario they came from."""

from __future__ import annotations

import json
from datetime import date, datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from tradeassist.api import create_app
from tradeassist.config import Settings
from tradeassist.db import Database
from tradeassist.providers.demo import DemoData
from tradeassist.service import TradeAssistant
from tradeassist.v3.opportunity import evaluate, fingerprint
from tests.test_one_click import _wait, drain

# --- the evaluator, on synthetic valuations ------------------------------------------------------------------------------

DAYS = ["2026-10-23", "2026-10-26", "2026-10-27", "2026-10-28", "2026-10-29"]
TH = {"budget": 1000.0, "loss_limit": 200.0, "min_net_pct": 5.0, "max_spread_pct": 10.0}
TODAY = date(2026, 10, 22)


def struct(key, direction, cap, ml, cases, costs=5.0, spread=2.0, two_sided=True, unlimited=False, quality=()):
    # 4.5.1: each structure carries its data quality; execution quality unless the test names what keeps it research only.
    return {"key": key, "label": key.replace("_", " "), "direction": direction, "quantity": 1, "costs": costs, "capital": cap,
            "max_loss": None if unlimited else ml, "unlimited_loss": unlimited,
            "legs": [{"option_symbol": f"X{key}", "bid": 1.0 if two_sided else None, "ask": 1.1, "spread_pct": spread}],
            "by_case": {k: {"expected_pnl": v[0], "worst_tenth": v[1]} for k, v in cases.items()}, "flags": [],
            "quality": {"tier": "research" if quality else "execution", "reasons": list(quality)}}


VERIFIED = "verified"


def val(*structs, stale=False):
    return {"id": "sval_x", "status": "complete", "structures": list(structs), "mixes": [{"key": "cash", "parts": []}],
            "freshness": {"stale": stale}}


def ver(event_date=None, base_end=0.5):
    return {"id": "sver_x", "sessions": [{"index": i + 1, "date": d} for i, d in enumerate(DAYS)],
            "catalyst": {"kind": "earnings" if event_date else "other", "title": "Results", "event_date": event_date}, "catalyst_session": 1,
            "base": {"return_pct": [base_end * (i + 1) / 5 for i in range(5)]},
            "cases": [{"key": "central", "label": "Central", "direction": "flat", "trigger": "in line", "invalidation": "a surprise", "quantified": False},
                      {"key": "bullish", "label": "Bullish", "direction": "up", "trigger": "the outlook is raised", "invalidation": "margins fall", "quantified": True},
                      {"key": "bearish", "label": "Bearish", "direction": "down", "trigger": "the outlook is cut", "invalidation": "the outlook holds", "quantified": True}]}


def run(v, *, routes=None, today=TODAY, **kw):
    routes = routes or {s["key"]: "manual" for s in v["structures"]}
    return evaluate(symbol="XYZ", version=kw.pop("version", ver()), valuation=v, thresholds=kw.pop("thresholds", TH), routes=routes, today=today, **kw)


EDGE = {"directional": {"status": "edge"}}


def test_spreads_that_eat_the_payoff_are_named_as_such():
    # Two units fit the loss limit: 6 of modeled net on 200 of capital is 3%; before 10 of costs it would be 8%.
    d = run(val(struct("long_call", "LONG", 100, 100, {"central": (-5, 60), "bullish": (3, 20), "bearish": (-60, 90)})))
    assert d["status"] == "no_setup" and d["reason"]["code"] == "cost_eroded"
    assert d["setup"]["case"] == "bullish" and d["setup"]["units"] == {"long_call": 2}
    assert d["setup"]["gross_pnl"] == pytest.approx(16) and d["setup"]["net_pnl"] == pytest.approx(6)
    assert "spreads and fees consume" in d["reason"]["text"]


def test_a_setup_must_stay_inside_the_loss_limit_by_contract_and_by_model():
    over_contract = struct("long_call", "LONG", 300, 300, {"central": (0, 100), "bullish": (200, 50), "bearish": (-300, 300)})
    d = run(val(over_contract))
    assert d["status"] == "no_setup" and "contract_loss_over_limit" in d["counts"] and d["setup"]["units"] == {"long_call": 1}
    over_model = struct("bull_put_spread", "LONG", 100, 100, {"central": (5, 50), "bullish": (40, 120), "bearish": (-90, 95)})
    d = run(val(over_model))     # two units: 240 of modeled worst tenth in the bullish case, over 200
    assert d["status"] == "no_setup" and d["reason"]["code"] == "scenario_downside_over_limit"
    fits = struct("bull_call_spread", "LONG", 100, 100, {"central": (-5, 50), "bullish": (40, 60), "bearish": (-95, 99)})
    d = run(val(over_contract, fits))
    assert d["status"] == "watch" and d["setup"]["key"] == "single:bull_call_spread" and d["setup"]["max_loss"] <= TH["loss_limit"]
    assert d["counts"]["contract_loss_over_limit"] >= 1                               # the other one is still counted


def test_a_bearish_case_carries_a_short_direction():
    put = struct("long_put", "SHORT", 100, 100, {"central": (-5, 40), "bullish": (-90, 99), "bearish": (60, 30)})
    call = struct("long_call", "LONG", 100, 100, {"central": (-5, 40), "bullish": (-90, 99), "bearish": (500, 10)})   # contrived
    d = run(val(put, call))
    assert d["status"] == "watch" and d["setup"]["case"] == "bearish" and d["setup"]["direction"] == "SHORT"
    assert d["setup"]["key"] == "single:long_put" and d["counts"]["direction_mismatch"] >= 1
    assert d["answers"]["what"].endswith("short") and d["answers"]["when"].startswith("Only if the outlook is cut")
    assert d["answers"]["invalidates"] == "the outlook holds"


def test_a_trigger_that_passed_unobserved_expires_and_an_observed_one_qualifies():
    call = struct("long_call", "LONG", 100, 100, {"central": (-5, 60), "bullish": (60, 20), "bearish": (-90, 99)})
    dated = ver(event_date="2026-10-23")
    d = run(val(call), version=dated, today=date(2026, 10, 22))
    assert d["status"] == "watch" and d["setup"]["expires_on"] == "2026-10-23"
    d = run(val(call), version=dated, today=date(2026, 10, 26))
    assert d["status"] == "no_setup" and d["reason"]["code"] == "expired_trigger"
    # 4.5.1: a reported trigger alone is evidence for review: it keeps the Watch (and expires like one), it never qualifies.
    reported = {"bullish": {"title": "Company release: outlook raised", "published_at": "2026-10-23T12:00:00+00:00", "status": "unverified"}}
    d = run(val(call), version=dated, today=date(2026, 10, 22), observed=reported)
    assert d["status"] == "watch" and d["setup"]["trigger_evidence"]["status"] == "unverified" and "only once verified" in d["answers"]["when"]
    d = run(val(call), version=dated, today=date(2026, 10, 26), observed=reported)
    assert d["status"] == "no_setup" and d["reason"]["code"] == "expired_trigger"
    for not_yet in ("verification_pending", "rejected", "expired", None):
        d = run(val(call), version=dated, today=date(2026, 10, 22), observed={"bullish": {**reported["bullish"], "status": not_yet}})
        assert d["status"] == "watch", not_yet
    seen = {"bullish": {**reported["bullish"], "status": VERIFIED}}
    d = run(val(call), version=dated, today=date(2026, 10, 26), observed=seen)
    assert d["status"] == "qualified" and d["setup"]["mode"] == "triggered" and "outlook raised" in d["answers"]["when"]
    d = run(val(call), version=dated, today=date(2026, 10, 30), observed=seen)
    assert d["status"] == "no_setup" and d["reason"]["code"] == "expired_session"


def test_without_a_trigger_a_setup_needs_a_supported_base_and_stays_safe_in_every_case():
    shares = struct("shares", "LONG", 100, 100, {"central": (12, 30), "bullish": (40, 20), "bearish": (-30, 60)})
    d = run(val(shares))
    assert d["status"] == "watch"                                      # no support for the base: only the trigger's case
    d = run(val(shares), readiness=EDGE)
    assert d["status"] == "qualified" and d["setup"]["case"] is None and "validated on its holdout" in d["setup"]["support"]
    assert d["setup"]["data_quality"] == "execution"
    d = run(val(shares), analogs={"independent": 45, "median_pct": 0.8})
    assert d["status"] == "qualified" and "45 independent past cases" in d["setup"]["support"]
    d = run(val(shares), readiness=EDGE, version=ver(base_end=-0.5))     # the edge points the other way
    assert d["status"] == "watch"
    # Ranked by the worst case across the cases.
    safer = struct("bull_put_spread", "LONG", 100, 100, {"central": (10, 20), "bullish": (15, 10), "bearish": (2, 40)})
    d = run(val(shares, safer), readiness=EDGE)
    assert d["status"] == "qualified" and d["setup"]["key"] == "single:bull_put_spread"
    assert [q["key"] for q in d["qualified"]][:2] == ["single:bull_put_spread", "single:shares"]


def test_quotes_and_liquidity_and_eligibility_are_checked():
    good = {"central": (-5, 40), "bullish": (60, 20), "bearish": (-90, 99)}
    assert run(val(struct("long_call", "LONG", 100, 100, good, spread=15)))["reason"]["code"] == "wide_spread"
    assert run(val(struct("long_call", "LONG", 100, 100, good, two_sided=False)))["reason"]["code"] == "no_two_sided_quote"
    d = run(val(struct("long_call", "LONG", 100, 100, good)), routes={"long_call": "research"})
    assert d["reason"]["code"] == "not_eligible"
    d = run(val(struct("short", "SHORT", 100, None, {"central": (-5, 40), "bullish": (-90, 99), "bearish": (60, 20)}, unlimited=True)))
    assert d["status"] == "no_setup" and "unlimited_loss" in d["counts"]
    assert run(val(struct("long_call", "LONG", 100, 100, good), stale=True))["status"] == "watch"       # a Watch is rechecked later
    seen = {"bullish": {"title": "release", "status": VERIFIED}}
    d = run(val(struct("long_call", "LONG", 100, 100, good), stale=True), observed=seen)
    assert d["status"] == "no_setup" and d["reason"]["code"] == "stale_quote"
    # 4.5.1: research-quality data never qualifies (it still supports a Watch); its reason is named.
    for code in ("market_closed", "quote_freshness_unknown", "crossed_quote", "study_midpoint_used"):
        rq = struct("long_call", "LONG", 100, 100, good, quality=(code,))
        assert run(val(rq))["status"] == "watch"
        d = run(val(rq), observed=seen)
        assert d["status"] == "no_setup" and d["reason"]["code"] == code, code
    legacy = {k: v for k, v in struct("long_call", "LONG", 100, 100, good).items() if k != "quality"}     # valued before 4.5.1
    d = run(val(legacy), observed=seen)
    assert d["status"] == "no_setup" and d["reason"]["code"] == "quote_freshness_unknown"
    thin = struct("long_call", "LONG", 100, 100, good)
    thin["legs"][0]["quality"] = {"tier": "research", "reasons": ["low_open_interest"]}
    thin["quality"] = {"tier": "research", "reasons": ["low_open_interest"]}
    assert run(val(thin))["reason"]["code"] == "low_open_interest"                                   # liquidity: a Watch too
    d = run(val(struct("long_call", "LONG", 100, 100, good)), observed=seen, mode="sandbox", realtime=False)
    assert d["status"] == "no_setup" and d["reason"]["code"] == "delayed_quotes" and d["example"]
    d = run(val(struct("long_call", "LONG", 100, 100, good)), mode="demo")
    assert d["status"] == "watch" and d["example"] and "made up" in d["example_words"]
    assert run({**val(), "status": "failed"})["reason"]["code"] == "valuation_failed"


def test_the_fingerprint_changes_only_when_the_decision_does():
    call = struct("long_call", "LONG", 100, 100, {"central": (-5, 60), "bullish": (60, 20), "bearish": (-90, 99)})
    a = run(val(call))
    b = run(val({**call, "by_case": {**call["by_case"], "bullish": {"expected_pnl": 61, "worst_tenth": 21}}}))
    assert fingerprint(a) == fingerprint(b)
    assert fingerprint(run(val(call), observed={"bullish": {"title": "r", "status": "unverified"}})) == fingerprint(a)   # still a Watch
    c = run(val(call), observed={"bullish": {"title": "r", "status": VERIFIED}})
    assert fingerprint(c) != fingerprint(a)


# --- through the API, on demo data ---------------------------------------------------------------------------------------

@pytest.fixture(scope="module")
def app(tmp_path_factory):
    import tradeassist.api as api_mod
    api_mod.TEST_HOSTS = ("testserver",)
    path = tmp_path_factory.mktemp("opps") / "t.db"
    s = Settings(data_mode="demo", db_path=path, desk_worker="off", scenario_provider="demo", scenario_auto=True, scenario_budget=5000.0,
                 scenario_max_loss=1000.0, scenario_notify=True)
    svc = TradeAssistant(s, provider=DemoData(), db=Database(path))
    c = TestClient(create_app(s, svc))
    one = _wait(c, c.post("/v3/studies", json={"symbol": "AAPL", "horizon_days": 5, "request_key": "opp-click-0001"}).json()["id"])
    two = _wait(c, c.post("/v3/studies", json={"symbol": "MSFT", "horizon_days": 5}).json()["id"])
    drain(svc)
    yield c, svc, {"one": one, "two": two}
    svc.db.close()


def _count(svc, sql, *args):
    return svc.db.one(sql, args)["n"]


def mine_n(c, sid):
    return [n for n in c.get("/v3/scenarios/notices").json()["notices"] if n["study_id"] == sid]


def notes_of(svc, sid):
    return _count(svc, "SELECT COUNT(*) AS n FROM notifications WHERE kind = 'scenario.decision' AND dedupe_key LIKE ?", f"scenario:{sid}:%")


def test_the_decision_is_recorded_published_and_noticed_once(app):
    c, svc, st = app
    sid = st["one"]["id"]
    p = c.get(f"/v3/studies/{sid}/pipeline").json()["pipeline"]
    d = p["decision"]
    assert p["status"] == "complete" and d is not None and d["status"] in ("qualified", "watch", "no_setup")
    assert d["example"] is True and d["version_id"] == p["version_id"] and d["valuation_id"] == p["valuation"]["id"]
    assert d["thresholds"] == {"budget": 5000.0, "loss_limit": 1000.0, "min_net_pct": 1.0, "max_spread_pct": 10.0}
    assert d["status"] == "watch", d["reason"]            # demo AAPL: the bullish and bearish cases pay; the base has no support
    s = d["setup"]
    assert s["case"] in ("bullish", "bearish") and s["direction"] == {"bullish": "LONG", "bearish": "SHORT"}[s["case"]]
    assert all(d["answers"][k] for k in ("what", "when", "why", "invalidates"))
    # One scenario observation in Discover, linked to the study, never eligible for the autopilot.
    feed = c.get("/v3/discover", params={"kind": "scenario"}).json()["candidates"]
    mine = [x for x in feed if x["study_id"] == sid]
    assert len(mine) == 1 and mine[0]["direction"] == s["direction"] and "scenario" in mine[0]["kinds"] and mine[0]["sources"] == ["study"]
    assert not mine[0]["autopilot_eligible"] and any("scenario evidence only" in r for r in mine[0]["autopilot_reasons"])
    assert mine[0]["edge"]["known"] is False
    full = c.get(f"/v3/discover/{mine[0]['id']}").json()
    assert full["observations"][0]["scenario"]["decision_id"] == d["id"] and full["observations"][0]["scenario"]["status"] == "watch"
    # One notice and one notification (the outbox is on; with no channel set up it is recorded as suppressed).
    notices = mine_n(c, sid)
    assert len(notices) == 1 and notices[0]["status"] == "watch"
    assert notes_of(svc, sid) == 1
    # Value again: the same decision, so no second observation, notice or notification.
    c.post(f"/v3/studies/{sid}/pipeline/revalue", json={})
    again = c.get(f"/v3/studies/{sid}/decision").json()["decision"]
    assert again["id"] != d["id"] and again["fingerprint"] == d["fingerprint"] and again["changed"] is False
    assert _count(svc, "SELECT COUNT(*) AS n FROM opportunity_observations WHERE source = 'study' AND source_key LIKE ?", f"scenario:{sid}:%") == 1
    assert len(mine_n(c, sid)) == 1
    assert notes_of(svc, sid) == 1
    # Dismissed: gone, and an unchanged repeat does not bring it back.
    n_id = mine_n(c, sid)[0]["id"]
    assert [n for n in c.post(f"/v3/scenarios/notices/{n_id}/seen", json={}).json()["notices"] if n["study_id"] == sid] == []
    c.post(f"/v3/studies/{sid}/pipeline/revalue", json={})
    assert mine_n(c, sid) == []


def test_a_draft_carries_its_scenario_and_goes_the_cases_way(app):
    c, svc, st = app
    sid = st["one"]["id"]
    d = c.get(f"/v3/studies/{sid}/decision").json()["decision"]
    s = d["setup"]
    ref = {"version_id": d["version_id"], "case": s["case"], "valuation_id": d["valuation_id"], "decision_id": d["id"]}
    r = c.post("/v3/drafts", json={"study_id": sid, "strategy": s["structures"][0], "scenario": ref})
    assert r.status_code == 200, r.text
    dr = r.json()
    assert dr["source"]["scenario"]["case"] == s["case"] and dr["source"]["scenario"]["decision_id"] == d["id"]
    assert dr["direction"] == s["direction"] and "Scenario: the" in dr["thesis"]
    assert any("Conditional on:" in x for x in dr["exit_plan"]["rules"]) and any("Wrong if" in x for x in dr["exit_plan"]["rules"])
    assert any("Conditional" in x for x in dr["notes"])
    # The other way round, another study's version, and another study's decision are refused.
    wrong_way = "long_call" if s["case"] == "bearish" else "long_put"
    bad = c.post("/v3/drafts", json={"study_id": sid, "strategy": wrong_way, "scenario": ref})
    assert bad.status_code == 400 and "direction strategy" in bad.json()["error"]
    other = st["two"]["id"]
    assert c.post("/v3/drafts", json={"study_id": other, "strategy": "shares", "scenario": {"version_id": d["version_id"]}}).status_code == 400
    p2 = c.get(f"/v3/studies/{other}/pipeline").json()["pipeline"]
    r = c.post("/v3/drafts", json={"study_id": other, "strategy": "shares", "scenario": {"version_id": p2["version_id"], "decision_id": d["id"]}})
    assert r.status_code == 400 and "another study" in r.json()["error"]


def test_scenario_evidence_never_reaches_execution(app):
    c, svc, st = app
    before = _count(svc, "SELECT COUNT(*) AS n FROM trade_proposals")
    sid = st["one"]["id"]
    obs = svc.db.one("SELECT id, payload_json, symbol, direction, horizon FROM opportunity_observations WHERE source = 'study' AND source_key LIKE ?",
                     (f"scenario:{sid}:%",))
    # Even a payload that claims a validated record is read as no record.
    payload = json.loads(obs["payload_json"])
    payload["edge"] = {"known": True, "expected_pct": 2.0, "cases": 400, "validated": True, "basis": "forged"}
    with svc.db.tx() as cur:
        cur.execute("UPDATE opportunity_observations SET payload_json = ? WHERE id = ?", (json.dumps(payload), obs["id"]))
    svc.discover3._rebuild(obs["symbol"], obs["direction"], obs["horizon"])
    cand = next(x for x in c.get("/v3/discover", params={"kind": "scenario"}).json()["candidates"] if x["study_id"] == sid)
    assert not cand["autopilot_eligible"] and cand["edge"]["known"] is False
    assert c.get("/v3/discover", params={"eligible_only": "true", "kind": "scenario"}).json()["candidates"] == []
    assert _count(svc, "SELECT COUNT(*) AS n FROM trade_proposals") == before == 0
    assert _count(svc, "SELECT COUNT(*) AS n FROM broker_orders") == 0


def test_no_setup_withdraws_the_observation(app, monkeypatch):
    c, svc, st = app
    sid = st["one"]["id"]
    monkeypatch.setattr(svc.opportunities, "thresholds", lambda: {"budget": 5000.0, "loss_limit": 1.0, "min_net_pct": 1.0, "max_spread_pct": 10.0})
    c.post(f"/v3/studies/{sid}/pipeline/revalue", json={})
    d = c.get(f"/v3/studies/{sid}/decision").json()["decision"]
    assert d["status"] == "no_setup" and d["reason"]["code"] in ("contract_loss_over_limit", "scenario_downside_over_limit")
    assert _count(svc, "SELECT COUNT(*) AS n FROM opportunity_observations WHERE source = 'study' AND source_key LIKE ?", f"scenario:{sid}:%") == 0
    assert [x for x in c.get("/v3/discover", params={"kind": "scenario"}).json()["candidates"] if x["study_id"] == sid] == []


# Last in the file: it runs on a later clock (the next session, market open), so nothing after it sees its records as
# being from the future.

def _page(symbol, published, *words):
    return {"status": 200, "content_type": "text/html", "text": (
        f'<html><head><meta property="article:published_time" content="{published.isoformat()}"></head>'
        f'<body><h1>{symbol} news</h1><p>({symbol}) {" ".join(words)}</p></body></html>')}


@pytest.mark.clock("2026-10-23T11:00")
def test_a_reported_trigger_qualifies_the_setup_only_once_its_source_verifies_it(app):
    """4.5.1: a trigger you record is evidence for review. Without a link it stays unverified (a Watch); with one,
    the app reads the source and only a verified report, on execution-quality quotes, makes the setup qualified."""
    c, svc, st = app
    sid = st["one"]["id"]
    d = c.post(f"/v3/studies/{sid}/pipeline/revalue", json={}).json()["pipeline"]["decision"]      # on this session's quotes
    assert d["status"] == "watch", d["reasons"]
    case = d["setup"]["case"]
    created = datetime.fromisoformat(st["one"]["created_at"].replace("Z", "+00:00"))
    early = c.post(f"/v3/studies/{sid}/decision/trigger", json={"case": case, "title": "An old article", "published_at": (created - timedelta(hours=1)).isoformat()})
    assert early.status_code == 400 and "before the study's evidence cutoff" in early.json()["error"]
    future = c.post(f"/v3/studies/{sid}/decision/trigger", json={"case": case, "title": "Tomorrow", "published_at": "2030-01-01T00:00:00+00:00"})
    assert future.status_code == 400
    when = (datetime.now(timezone.utc) - timedelta(minutes=5)).isoformat()
    # 1. No link: recorded, unverified, and the setup is still a Watch.
    r = c.post(f"/v3/studies/{sid}/decision/trigger", json={"case": case, "title": "Company release: the outlook changed", "published_at": when})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["decision"]["status"] == "watch" and body["triggers"][case]["status"] == "unverified"
    assert "only once verified" in body["decision"]["answers"]["when"]
    # 2. A link whose page does not name the stock: rejected, still a Watch.
    ver = svc.scenarios.version(svc.pipeline.load(sid)["version_id"])
    trig = next(x for x in ver["cases"] if x["key"] == case)["trigger"]
    pub = datetime.now(timezone.utc) - timedelta(minutes=3)
    svc.trigger_fetcher = lambda url: {"url": url, **_page("ZZZZ", pub, "the outlook changed", trig.replace("AAPL", "the company"))}
    try:
        r = c.post(f"/v3/studies/{sid}/decision/trigger", json={"case": case, "title": "Company release: the outlook changed", "published_at": when,
                                                                "url": "https://example.com/release"})
        assert r.json()["triggers"][case]["status"] == "verification_pending" and r.json()["decision"]["status"] == "watch"
        drain(svc)
        t = c.get(f"/v3/studies/{sid}/triggers").json()
        assert t["triggers"][case]["status"] == "rejected" and "does not name AAPL" in t["triggers"][case]["reason"]
        assert c.get(f"/v3/studies/{sid}/decision").json()["decision"]["status"] == "watch"
        before = notes_of(svc, sid)
        # 3. A page that names the stock, was published after the cutoff and supports the claim and the trigger: verified.
        svc.trigger_fetcher = lambda url: {"url": url, **_page("AAPL", pub, "Company release: the outlook changed.", trig)}
        r = c.post(f"/v3/studies/{sid}/decision/trigger", json={"case": case, "title": "Company release: the outlook changed", "published_at": when,
                                                                "url": "https://example.com/release"})
        drain(svc)
    finally:
        svc.trigger_fetcher = None
    t = c.get(f"/v3/studies/{sid}/triggers").json()
    rec = t["triggers"][case]
    assert rec["status"] == "verified" and rec["verified_at"] and rec["published_at"] and rec["recorded_at"] and rec["cutoff"]
    assert [h["status"] for h in t["history"]][-2:] == ["verification_pending", "verified"]
    q = c.get(f"/v3/studies/{sid}/decision").json()["decision"]
    assert q["status"] == "qualified" and q["setup"]["mode"] == "triggered" and q["changed"] is True, q["reasons"]
    assert q["setup"]["data_quality"] == "execution" and "Company release" in q["answers"]["when"]
    feed = [x for x in c.get("/v3/discover", params={"kind": "scenario"}).json()["candidates"] if x["study_id"] == sid]
    assert len(feed) == 1 and not feed[0]["autopilot_eligible"]
    assert [n["status"] for n in mine_n(c, sid)] == ["qualified"]
    assert notes_of(svc, sid) == before + 1                                   # one notification for the change to Qualified
