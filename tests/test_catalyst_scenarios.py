"""4.1 Catalyst Scenarios: the frozen evidence packet, the provider's cases checked whole against it, reviewed
assumptions priced from the study's own base and its frozen factor fit, and versions in fixed coordinates.

The arithmetic is tested on its own (a zero shock is the base; a sector shock that is only the market's adds nothing;
the issuer's jump lands on the first session after the release; the What-If model's own path is reproduced), then the
whole flow through the API on demo data, with the refusals that keep a late or foreign answer off a study."""

from __future__ import annotations

import copy
import json
import math
import time
from datetime import date
from pathlib import Path

import numpy as np
import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from tradeassist import contracts as C
from tradeassist import factors as Fx
from tradeassist.api import create_app
from tradeassist.config import Settings
from tradeassist.db import Database
from tradeassist.features import FEATURE_DEFAULTS
from tradeassist.providers.demo import DemoData
from tradeassist.service import RequestError, TradeAssistant
from tradeassist.v3 import scenarios as SC

FIXTURE = Path(__file__).resolve().parent / "fixtures" / "catalyst_cases.json"


# --- the arithmetic -------------------------------------------------------------------------------------------------

def _returns(n=250, seed=7):
    """A stock, the market, a sector fund that is mostly the market, and a yield proxy (IEF)."""
    rng = np.random.default_rng(seed)
    m = 0.01 * rng.standard_normal(n)
    sector = 0.9 * m + 0.004 * rng.standard_normal(n)
    ief = -0.2 * m + 0.003 * rng.standard_normal(n)
    stock = 1.1 * m + 0.6 * (sector - 0.9 * m) - 0.3 * ief + 0.008 * rng.standard_normal(n)
    return {"days": n, "stock": stock, "proxies": {"us_market": m, "sector": sector, "yield_10y": ief}, "dropped": {}}


def _rows():
    rows = []
    for f in Fx.FACTORS:
        if f["key"] in ("us_market", "sector", "yield_10y"):
            rows.append({**f, "proxy": f.get("proxy") or "XLK", "on": True, "beta": 1.0})
    return rows


def _fx(returns=None):
    """A frozen factor context in the seed's shape, from synthetic returns."""
    r = returns or _returns()
    rows = _rows()
    order = [x["key"] for x in rows]
    fit = Fx.joint_fit(r["stock"], r["proxies"], order)
    out = []
    for x in rows:
        f = fit[x["key"]]
        out.append({"key": x["key"], "label": x["label"], "unit": x["unit"], "group": x["group"], "proxy": x["proxy"],
                    "inverse": bool(x.get("inverse")), "scale": x.get("scale"), "index_scale": x.get("index_scale"), "note": "",
                    "beta": 1.0, "correlation": 0.5, "beta_days": r["days"], "beta_unique": f["beta_unique"],
                    "explained": f["explained"], "redundant": f["redundant"], "loadings": dict(f["loadings"])})
    return {"basis": "synthetic", "fit_days": r["days"], "rows": out, "reference": {}, "unavailable": []}


def _seed(h=5, base=None, low=None, high=None, fx=None):
    base = base or [0.3 * t for t in range(1, h + 1)]
    # The forecast's band: lognormal around the median, 0.02 log-sd per square-root session.
    low = low or [100 * (math.exp(math.log1p(b / 100) - SC.Z80 * 0.02 * math.sqrt(t)) - 1) for t, b in enumerate(base, 1)]
    high = high or [100 * (math.exp(math.log1p(b / 100) + SC.Z80 * 0.02 * math.sqrt(t)) - 1) for t, b in enumerate(base, 1)]
    return {"seed_id": "seed_x", "study_id": "study_x", "symbol": "XYZ", "horizon_sessions": h,
            "anchor": {"price": 100.0, "at": "2026-10-22T20:00:00Z", "basis": "study_quote", "bar_date": "2026-10-22"},
            "sessions": [{"index": i + 1, "date": f"2026-10-{23 + i:02d}", "close_at": ""} for i in range(h)],
            "catalyst": {"kind": "earnings", "title": "Results"}, "catalyst_session": 2,
            "base": {"return_pct": base, "low_pct": low, "high_pct": high, "log_sd": SC.base_dispersion(low, high),
                     "calibrated": False, "range_words": "the model-implied range"},
            "factors": fx if fx is not None else _fx(), "volatility": {}, "sources": []}


def _case(key="bearish", issuer=None, factors=None, dispersion=None):
    return {"key": key, "visible": True, "issuer": issuer, "factors": factors or [], "dispersion": dispersion}


def test_the_frozen_fit_prices_the_same_terms_as_the_what_if_model():
    r, rows = _returns(), _rows()
    scenario = {"us_market": -1.2, "sector": -2.0, "yield_10y": 15.0}
    live = Fx.factor_terms(rows, scenario, r)
    frozen = Fx.terms_from_fit(rows, Fx.joint_fit(r["stock"], r["proxies"], [x["key"] for x in rows]), scenario, r["days"])
    assert live == frozen and len(live) == 3


def test_a_zero_shock_reproduces_the_base_exactly():
    seed = _seed()
    zero = _case(issuer={"value_pct": 0.0, "session": 2, "source": "user_stress", "source_ids": []},
                 factors=[{"key": "us_market", "value": 0.0, "session": 1}, {"key": "yield_10y", "value": 0.0, "session": 3}])
    for a in (zero, _case()):
        p = SC.case_paths(seed, a)
        assert p["return_pct"] == seed["base"]["return_pct"]                                 # the same numbers, not close ones
        assert p["low_pct"] == pytest.approx(seed["base"]["low_pct"], abs=1e-9)
        assert p["high_pct"] == pytest.approx(seed["base"]["high_pct"], abs=1e-9)
        assert p["price"] == pytest.approx([100.0 * (1 + b / 100) for b in seed["base"]["return_pct"]], abs=1e-12)
        assert all(x == 0.0 for x in p["attribution"]["factor_pct"] + p["attribution"]["issuer_pct"])


def test_a_sector_shock_counts_only_its_own_part_so_the_market_is_not_counted_twice():
    fx = _fx()
    loading = next(r for r in fx["rows"] if r["key"] == "sector")["loadings"]["us_market"]
    market = SC.factor_effect(fx, [{"key": "us_market", "value": -2.0, "session": 1}], 3)
    # The sector falling exactly as much as the market explains adds nothing of its own.
    joint = SC.factor_effect(fx, [{"key": "us_market", "value": -2.0, "session": 1}, {"key": "sector", "value": -2.0 * loading, "session": 1}], 3)
    assert joint["pct"] == pytest.approx(market["pct"], abs=1e-12)
    sector_term = next(t for t in joint["terms"] if t["key"] == "sector")
    assert sector_term["unique_move_pct"] == pytest.approx(0.0, abs=1e-12) and sector_term["shared_pct"] == pytest.approx(-2.0 * loading)
    # The same sector move with the market unshocked is all its own: a different (here larger) term.
    alone = SC.factor_effect(fx, [{"key": "sector", "value": -2.0 * loading, "session": 1}], 3)
    assert next(t for t in alone["terms"] if t["key"] == "sector")["unique_move_pct"] == pytest.approx(-2.0 * loading)
    # Basis points: a rise in the 10-year yield is a fall in IEF of 0.075% per bp.
    y = SC.factor_effect(fx, [{"key": "yield_10y", "value": 20.0, "session": 1}], 1)
    assert y["terms"][0]["proxy_move_pct"] == pytest.approx(-1.5)


def test_the_factor_effect_is_the_what_if_models_path_difference():
    fx = _fx()
    shocks = [{"key": "us_market", "value": -1.5, "session": 1}, {"key": "sector", "value": 0.8, "session": 1}]
    eff = SC.factor_effect(fx, shocks, 5)
    rows, fit = SC._fit_of(fx["rows"])
    path = lambda scen: Fx.model_path(horizon=5, shocks=Fx.terms_from_fit(rows, fit, scen), pattern=None, pattern_weight=0.0,   # noqa: E731
                                      daily_vol=0.01, atr_pct=None, news_term_pct=0.0, analyst_net=0.0, earnings_in=None,
                                      event_move_pct=None)["path_pct"]
    expected = np.array(path({s["key"]: s["value"] for s in shocks})) - np.array(path({}))
    assert eff["pct"] == pytest.approx(list(expected), abs=1e-12)
    # A shock that lands on session 3 is not there before it, and stays after.
    late = SC.factor_effect(fx, [{"key": "us_market", "value": -1.5, "session": 3}], 5)
    assert late["pct"][:2] == [0.0, 0.0] and late["pct"][2:] == pytest.approx([SC.factor_effect(fx, shocks[:1], 1)["pct"][0]] * 3)


def test_the_issuers_jump_and_extra_uncertainty_start_on_their_session():
    seed = _seed()
    p = SC.case_paths(seed, _case(issuer={"value_pct": -3.0, "session": 2, "source": "user_stress", "source_ids": []},
                                  dispersion={"value_pct": 4.0, "session": 2, "source": "user_stress", "source_ids": []}))
    base, sd = seed["base"]["return_pct"], seed["base"]["log_sd"]
    assert p["return_pct"][0] == base[0] and p["return_pct"][1:] == pytest.approx([b - 3.0 for b in base[1:]])
    assert p["log_sd"][0] == pytest.approx(sd[0]) and p["log_sd"][1] == pytest.approx(math.sqrt(sd[1] ** 2 + 0.04 ** 2))
    assert all(lo < r < hi for lo, r, hi in zip(p["low_pct"], p["return_pct"], p["high_pct"]))
    with pytest.raises(RequestError, match="not a usable price path"):
        SC.case_paths(seed, _case(issuer={"value_pct": -60.0, "session": 1, "source": "user_stress", "source_ids": []},
                                  factors=[{"key": "us_market", "value": -50.0, "session": 1}]))


def _sessions(*days):
    return [{"index": i + 1, "date": d} for i, d in enumerate(days)]


def test_the_catalyst_lands_on_the_first_session_it_can_trade_in():
    week = _sessions("2026-11-23", "2026-11-24", "2026-11-25", "2026-11-27", "2026-11-30")      # Thanksgiving, the 26th, is closed
    cat = lambda d, t, hypo=False: C.CatalystSpec(kind="earnings", title="Results", event_date=d, event_time=t, hypothetical=hypo)  # noqa: E731
    assert SC.first_session(cat("2026-11-23", "after_close"), week)[0] == 2
    assert SC.first_session(cat("2026-11-24", "before_open"), week)[0] == 2
    assert SC.first_session(cat("2026-11-25", "after_close"), week)[0] == 4                       # the holiday is skipped
    assert SC.first_session(cat("2026-11-28", "before_open"), week)[0] == 5                       # a Saturday: Monday
    assert SC.first_session(C.CatalystSpec(kind="sector_shock", title="Tariffs", hypothetical=True), week)[0] == 1
    assert SC.first_session(cat("2026-11-20", "after_close"), week)[0] == 1                       # Friday evening: Monday
    with pytest.raises(RequestError, match="already in the anchor price"):
        SC.first_session(cat("2026-11-20", "before_open"), week)
    with pytest.raises(RequestError, match="after the study's horizon"):
        SC.first_session(cat("2026-11-30", "after_close"), week)


def test_the_base_dispersion_is_read_back_from_the_forecasts_band():
    from tradeassist import analogs as A
    from tradeassist import forecast as F
    from tests.test_research3 import frame, walk
    s = A.prepare("XYZ", frame(walk()), None, own=True)
    fc = F.forecast(s, [s], 5, event={"in_days": 2, "move_pct": 6.0})
    sd = SC.base_dispersion(fc["base"]["low_pct"], fc["base"]["high_pct"])
    assert sd[-1] == pytest.approx(math.sqrt(fc["sigma"] ** 2 + 0.06 ** 2) * fc["band_k"])
    assert sd[0] < sd[1]                                                                           # the release widens it from its day


def test_the_contracts_refuse_probabilities_paths_and_bad_numbers():
    good = json.loads(FIXTURE.read_text())
    C.ScenarioImport.model_validate(good)
    for bad in ({**good, "cases": [{**good["cases"][0], "probability": 0.6}]},
                {**good, "cases": [{**good["cases"][0], "path_pct": [1, 2]}]},
                {**good, "schema_version": 2},
                {**good, "cases": [{**good["cases"][0], "key": "wild"}]},
                {**good, "cases": [{**good["cases"][1], "drivers": [{**good["cases"][1]["drivers"][0], "proposed_value": float("nan")}]}]}):
        with pytest.raises(ValidationError):
            C.ScenarioImport.model_validate(bad)


# --- through the API, on demo data ----------------------------------------------------------------------------------

def _wait(c, study_id, timeout=90):
    end = time.time() + timeout
    while time.time() < end:
        s = c.get(f"/v3/studies/{study_id}").json()
        if s["status"] != "running":
            return s
        time.sleep(0.2)
    raise AssertionError("the study did not finish")


@pytest.fixture(scope="module")
def app(tmp_path_factory):
    import tradeassist.api as api_mod
    api_mod.TEST_HOSTS = ("testserver",)
    path = tmp_path_factory.mktemp("catalyst") / "t.db"
    s = Settings(data_mode="demo", db_path=path, desk_worker="off")
    svc = TradeAssistant(s, provider=DemoData(), db=Database(path))
    c = TestClient(create_app(s, svc))
    studies = {}
    for key, sym, h in (("aapl5", "AAPL", 5), ("msft5", "MSFT", 5), ("aapl3", "AAPL", 3), ("ko1", "KO", 1)):
        studies[key] = _wait(c, c.post("/v3/studies", json={"symbol": sym, "horizon_days": h}).json()["id"])
    yield c, svc, studies
    svc.db.close()


def _catalyst(study, day=1, when="after_close"):
    return {"kind": "earnings", "title": "Quarterly results and outlook", "event_date": study["frame"]["sessions"][day - 1]["date"], "event_time": when}


def _fixture(seed_id, **over):
    fx = json.loads(FIXTURE.read_text())
    fx["seed_id"] = seed_id
    fx.update(over)
    return fx


def test_the_study_keeps_its_frame_and_the_seed_freezes_the_evidence(app):
    c, svc, st = app
    s = st["aapl5"]
    frame = s["frame"]
    assert frame["anchor"]["price"] == s["view"]["last"] and frame["anchor"]["bar_date"] == s["view"]["last_bar_date"]
    assert [x["index"] for x in frame["sessions"]] == [1, 2, 3, 4, 5] and frame["anchor"]["bar_date"] < frame["sessions"][0]["date"]
    r = c.post(f"/v3/studies/{s['id']}/scenario-seeds", json={"catalyst": _catalyst(s), "sources": [
        {"title": "Company press release", "published_at": "2026-10-01T08:00", "url": "https://example.com/pr", "excerpt": "Results on the date above."}]})
    assert r.status_code == 200, r.text
    sv = r.json()
    seed = sv["seed"]
    assert seed["symbol"] == "AAPL" and seed["horizon_sessions"] == 5 and seed["data_mode"] == "demo"
    assert seed["anchor"] == frame["anchor"] and seed["sessions"] == frame["sessions"]
    assert seed["catalyst_session"] == 2 and "after the close" in seed["catalyst_timing_note"]
    assert seed["base"]["return_pct"] == s["study"]["forecast"]["base"]["path_pct"]
    assert [x["id"] for x in seed["sources"]] == [f"S{i}" for i in range(1, len(seed["sources"]) + 1)]
    assert seed["sources"][-1]["origin"] == "user" and seed["sources"][-1]["published_at"].startswith("2026-10-01T12:00")
    assert all(x["available_at"] <= seed["cutoff"] for x in seed["sources"])
    rows = {x["key"] for x in seed["factors"]["rows"]}
    assert {"us_market", "sector"} <= rows and "peers" not in rows and f"through {frame['anchor']['bar_date']}" in seed["factors"]["basis"]
    assert sv["template"]["seed_id"] == seed["seed_id"] and [k["key"] for k in sv["template"]["cases"]] == list(C.CASE_KEYS)
    assert seed["question"].startswith("Stock: AAPL. Catalyst: Quarterly results") and SC.QUESTION in seed["question"]
    # The packet carries nothing about the account; the same evidence gives the same seed.
    md = c.get(sv["markdown_url"])
    assert md.status_code == 200 and md.headers["content-type"].startswith("text/markdown") and "attachment" in md.headers["content-disposition"]
    text = md.text.lower()
    assert seed["seed_id"] in md.text and "## output template" in text and "s1" in text
    for word in ("token", "balance", "budget", "account", "position size", "broker"):
        assert word not in text, word
    again = c.post(f"/v3/studies/{s['id']}/scenario-seeds", json={"catalyst": _catalyst(s), "sources": [
        {"title": "Company press release", "published_at": "2026-10-01T08:00", "url": "https://example.com/pr", "excerpt": "Results on the date above."}]}).json()
    assert again["reused"] is True and again["seed"]["seed_id"] == seed["seed_id"]
    assert c.get(f"/v3/scenario-seeds/{seed['seed_id']}").json()["seed"] == seed
    assert svc.db.one("SELECT COUNT(*) AS n FROM scenario_seeds WHERE study_id = ?", (s["id"],))["n"] == 1


def test_the_seed_is_refused_for_what_it_cannot_honour(app):
    c, svc, st = app
    s = st["aapl5"]
    late = c.post(f"/v3/studies/{s['id']}/scenario-seeds", json={"catalyst": _catalyst(s), "sources": [
        {"title": "Tomorrow's article", "published_at": "2030-01-01T00:00Z"}]})
    assert late.status_code == 400 and "after this study's evidence cutoff" in late.json()["error"]
    after = c.post(f"/v3/studies/{s['id']}/scenario-seeds", json={"catalyst": _catalyst(s, day=5)})
    assert after.status_code == 400 and "after the study's horizon" in after.json()["error"]
    three = c.post(f"/v3/studies/{st['aapl3']['id']}/scenario-seeds", json={"catalyst": _catalyst(st["aapl3"])})
    assert three.status_code == 400 and "1 or 5-session study" in three.json()["error"]
    one = c.post(f"/v3/studies/{st['ko1']['id']}/scenario-seeds", json={"catalyst": {"kind": "macro", "title": "CPI release", "hypothetical": True}})
    assert one.status_code == 200 and one.json()["seed"]["catalyst_session"] == 1
    assert c.post("/v3/studies/study_nope/scenario-seeds", json={"catalyst": _catalyst(s)}).status_code == 400
    assert c.post(f"/v3/studies/{s['id']}/scenario-seeds", json={"catalyst": {"kind": "earnings", "title": "x"}}).status_code == 422
    assert c.post(f"/v3/studies/{s['id']}/scenario-seeds", json={"catalyst": {**_catalyst(s), "event_time": "lunch"}}).status_code == 422


def _keys(obj):
    if isinstance(obj, dict):
        return [k for k in obj] + [x for v in obj.values() for x in _keys(v)]
    if isinstance(obj, list):
        return [x for v in obj for x in _keys(v)]
    return []


def _seed_for(c, study, **cat):
    return c.post(f"/v3/studies/{study['id']}/scenario-seeds", json={"catalyst": {**_catalyst(study), **cat}}).json()["seed"]


def test_cases_are_imported_versioned_and_drawn_in_fixed_coordinates(app, monkeypatch):
    c, svc, st = app
    s = st["aapl5"]
    seed = _seed_for(c, s)
    r = c.post(f"/v3/studies/{s['id']}/scenario-runs", json={"seed_id": seed["seed_id"], "result": json.dumps(_fixture(seed["seed_id"])),
                                                              "report_excerpt": "<script>alert(1)</script> The swarm leaned bearish."})
    assert r.status_code == 200, r.text
    run = r.json()
    assert [x["key"] for x in run["run"]["cases"]] == ["central", "bullish", "bearish"] and run["run"]["status"] == "complete"
    assert run["run"]["report_excerpt"].startswith("<script>")                  # kept as text; the screen renders it as text
    bull = run["run"]["cases"][1]
    assert [d["supported"] for d in bull["drivers"]] == [True, False]           # the sector driver cites nothing: a hypothesis
    sug = {a["key"]: a for a in run["suggested"]}
    assert sug["central"]["issuer"] is None and not sug["central"]["factors"]
    assert sug["bullish"]["issuer"] == {"value_pct": 4.0, "session": 2, "source": "provider", "source_ids": ["S1"], "note": "Investors re-rate the growth path"}
    assert sug["bearish"]["factors"][0]["key"] == "us_market" and sug["bearish"]["factors"][0]["session"] == 2
    # A double click: the same answer is one run.
    dup = c.post(f"/v3/studies/{s['id']}/scenario-runs", json={"seed_id": seed["seed_id"], "result": _fixture(seed["seed_id"]),
                                                                "report_excerpt": "<script>alert(1)</script> The swarm leaned bearish."}).json()
    assert dup["reused"] is True and dup["run"]["id"] == run["run"]["id"]

    v1 = c.post(f"/v3/scenario-runs/{run['run']['id']}/versions", json={"cases": run["suggested"]})
    assert v1.status_code == 200, v1.text
    v1 = v1.json()
    assert v1["version"] == 1 and v1["calc_version"] == SC.CALC_VERSION and v1["anchor"] == seed["anchor"] and v1["sessions"] == seed["sessions"]
    cases = {x["key"]: x for x in v1["cases"]}
    base = v1["base"]["return_pct"]
    assert cases["central"]["return_pct"] == base and cases["central"]["quantified"] is False
    assert cases["bullish"]["return_pct"][0] == base[0] and cases["bullish"]["return_pct"][1] == pytest.approx(base[1] + 4.0)
    assert cases["bearish"]["attribution"]["factor_pct"][0] == 0.0 and cases["bearish"]["attribution"]["factor_pct"][1] < 0
    anchor = seed["anchor"]["price"]
    assert cases["bullish"]["price"] == pytest.approx([anchor * (1 + x / 100) for x in cases["bullish"]["return_pct"]])
    assert not [k for k in _keys(v1) if "probab" in k.lower() or "p_profit" in k]      # no case has a probability
    # Reopened, it is the same version and the same points.
    got = c.get(f"/v3/scenario-runs/{run['run']['id']}").json()
    assert got["latest"]["id"] == v1["id"] and got["latest"]["cases"] == v1["cases"] and got["versions"][0]["version"] == 1
    assert c.get(f"/v3/scenario-versions/{v1['id']}").json()["cases"] == v1["cases"]
    listing = c.get(f"/v3/studies/{s['id']}/scenario-runs").json()
    assert listing["runs"][0]["id"] == run["run"]["id"] and listing["runs"][0]["latest_version"]["version"] == 1
    # The market moves on: the chart's close changes, and the same assumptions applied again give the same points.
    real = svc.market.quote

    def moved(sym):
        q = dict(real(sym))
        q["last"] = q["close"] = float(q.get("last") or 100) * 1.25
        return q
    monkeypatch.setattr(svc.market, "quote", moved)
    v2 = c.post(f"/v3/scenario-runs/{run['run']['id']}/versions", json={"parent_version": 1, "cases": v1["assumptions"]}).json()
    assert v2["version"] == 2 and v2["parent_version"] == 1
    assert [x["price"] for x in v2["cases"]] == [x["price"] for x in v1["cases"]] and v2["anchor"] == v1["anchor"]
    # Edits make a new version; zero shocks reproduce the base.
    zero = [{"key": k, "issuer": {"value_pct": 0.0, "session": 1, "source": "user_stress"},
             "factors": [{"key": "us_market", "value": 0.0}, {"key": "sector", "value": 0.0}]} for k in C.CASE_KEYS]
    v3 = c.post(f"/v3/scenario-runs/{run['run']['id']}/versions", json={"cases": zero}).json()
    assert v3["version"] == 3 and all(x["return_pct"] == v3["base"]["return_pct"] for x in v3["cases"])
    assert all(x["low_price"] == pytest.approx(v3["base"]["low_price"]) for x in v3["cases"])
    assert c.get(f"/v3/scenario-versions/{v1['id']}").json()["cases"] == v1["cases"]               # the old one is unchanged


def test_a_result_for_another_study_stock_or_horizon_cannot_land_here(app):
    c, svc, st = app
    a, b = st["aapl5"], st["msft5"]
    seed_a, seed_b = _seed_for(c, a), _seed_for(c, b)
    late = c.post(f"/v3/studies/{b['id']}/scenario-runs", json={"seed_id": seed_a["seed_id"], "result": _fixture(seed_a["seed_id"])})
    assert late.status_code == 400 and "belongs to another study" in late.json()["error"]
    url = f"/v3/studies/{b['id']}/scenario-runs"

    def refused(result, words):
        r = c.post(url, json={"seed_id": seed_b["seed_id"], "result": result})
        assert r.status_code == 400 and words in r.json()["error"], (words, r.json())
        return r.json()["error"]
    refused(_fixture(seed_a["seed_id"]), "not this study's packet")
    refused(_fixture(seed_b["seed_id"], symbol="AAPL"), "is not this study's stock MSFT")
    refused(_fixture(seed_b["seed_id"], horizon_sessions=3), "the study's horizon is 5")
    refused(_fixture(seed_b["seed_id"], data_mode="live"), "packet is on demo data")
    refused(_fixture(seed_b["seed_id"], sessions=["2026-01-02"]), "not the study's forecast sessions")
    two = _fixture(seed_b["seed_id"])
    two["cases"][1] = copy.deepcopy(two["cases"][2])
    refused(two, "may appear once")
    bad = _fixture(seed_b["seed_id"])
    bad["cases"][2]["drivers"][0]["source_ids"] = ["S99"]
    bad["cases"][2]["drivers"][1]["factor"] = "gold"
    bad["cases"][1]["drivers"][0]["session"] = 9
    bad["cases"][1]["drivers"][0]["proposed_unit"] = "%"
    msg = refused(bad, "S99 not in the packet")
    assert "unknown factor 'gold'" in msg and "after the horizon" in msg and "an issuer move is in pp" in msg     # every problem at once
    refused("{not json", "not JSON")
    refused('{"schema_version": 1, "seed_id": "%s", "cases": [{"key": "central", "trigger": "x", "stock_direction": "flat", '
            '"invalidation": "y", "drivers": [{"type": "market", "direction": "up", "reason": "r", "proposed_value": NaN, '
            '"proposed_unit": "%%", "factor": "us_market"}]}]}' % seed_b["seed_id"], "finite")
    refused({**_fixture(seed_b["seed_id"]), "probabilities": {"bullish": 0.5}}, "Extra inputs are not permitted")
    refused({**_fixture(seed_b["seed_id"]), "cases": [{**_fixture(seed_b["seed_id"])["cases"][0], "trigger": "x" * 70_000}]}, "larger than")
    refused([1, 2], "one JSON object")
    assert c.post(url, json={"seed_id": "seed_nope", "result": {}}).status_code == 400
    assert c.post(url, json={"provider": "mirofish", "seed_id": seed_b["seed_id"], "result": {}}).status_code == 400
    assert svc.db.one("SELECT COUNT(*) AS n FROM scenario_runs WHERE study_id = ?", (b["id"],))["n"] == 0


def test_assumptions_are_checked_before_any_path_is_saved(app):
    c, svc, st = app
    s = st["msft5"]
    seed = _seed_for(c, s)
    run = c.post(f"/v3/studies/{s['id']}/scenario-runs", json={"seed_id": seed["seed_id"], "result": _fixture(seed["seed_id"])}).json()
    url = f"/v3/scenario-runs/{run['run']['id']}/versions"

    def refused(cases, words):
        r = c.post(url, json={"cases": cases})
        assert r.status_code in (400, 422) and words in r.json()["error"], (words, r.json())
    refused([{"key": "bearish", "factors": [{"key": "gold", "value": 1}]}], "'gold' is not a factor with a fit")
    refused([{"key": "bearish", "factors": [{"key": "us_market", "value": 80}]}], "outside ±50 %")
    refused([{"key": "bearish", "factors": [{"key": "yield_10y", "value": 250}]}], "outside ±200 bp")
    refused([{"key": "bearish", "factors": [{"key": "us_market", "value": -1}, {"key": "us_market", "value": -2}]}], "shocked once")
    refused([{"key": "bearish", "factors": [{"key": "us_market", "value": -1, "session": 6}]}], "after the horizon")
    refused([{"key": "bearish", "issuer": {"value_pct": -2, "session": 2, "source": "provider"}}], "needs the packet's source ids")
    refused([{"key": "bearish", "issuer": {"value_pct": -2, "session": 2, "source": "user_stress", "source_ids": ["S42"]}}], "S42 not in the packet")
    implied = seed["volatility"]["event_move_pct"]
    if implied is None:
        refused([{"key": "bearish", "issuer": {"value_pct": -2, "session": 2, "source": "options_implied_move"}}], "no options-implied move")
    else:
        refused([{"key": "bearish", "issuer": {"value_pct": -(implied + 1), "session": 2, "source": "options_implied_move"}}], "another size is a user stress")
    refused([{"key": "bearish"}, {"key": "bearish"}], "given twice")
    refused([{"key": "bearish", "issuer": {"value_pct": -2, "session": 2, "source": "user_stress"}, "probability": 0.4}], "Extra inputs")
    assert svc.db.one("SELECT COUNT(*) AS n FROM scenario_versions WHERE run_id = ?", (run["run"]["id"],))["n"] == 0
    # A bearish case that ends above the base is saved, with a warning to check its signs.
    ok = c.post(url, json={"cases": [{"key": "bearish", "issuer": {"value_pct": 2.0, "session": 2, "source": "user_stress"}}]}).json()
    bear = next(x for x in ok["cases"] if x["key"] == "bearish")
    assert any("ends above the base" in w for w in bear["warnings"])
    assert {x["key"] for x in ok["cases"]} == set(C.CASE_KEYS)                    # the cases not given keep the base path
    assert c.post("/v3/scenario-runs/srun_nope/versions", json={"cases": [{"key": "central"}]}).status_code == 400


def test_switched_off_and_read_from_another_site(tmp_path):
    import tradeassist.api as api_mod
    api_mod.TEST_HOSTS = ("testserver",)
    s = Settings(data_mode="demo", db_path=tmp_path / "t.db", desk_worker="off", features={**FEATURE_DEFAULTS, "catalyst_scenarios": False})
    svc = TradeAssistant(s, provider=DemoData(), db=Database(tmp_path / "t.db"))
    c = TestClient(create_app(s, svc))
    study = _wait(c, c.post("/v3/studies", json={"symbol": "KO", "horizon_days": 5}).json()["id"])
    assert study["status"] in ("complete", "partial") and study["frame"]                    # Research itself is unchanged
    r = c.post(f"/v3/studies/{study['id']}/scenario-seeds", json={"catalyst": _catalyst(study)})
    assert r.status_code == 400 and "FEATURE_CATALYST_SCENARIOS=0" in r.json()["error"]
    assert c.get(f"/v3/studies/{study['id']}/scenario-runs").status_code == 400
    assert c.get("/api/status").json()["features"]["catalyst_scenarios"] is False
    # Another site's page cannot read a packet or a result (the browser marks its request cross-site).
    for path in ("/v3/scenario-seeds/seed_x", f"/v3/studies/{study['id']}/scenario-runs", "/v3/scenario-runs/r", "/v3/scenario-versions/v"):
        assert c.get(path, headers={"sec-fetch-site": "cross-site"}).status_code == 403, path
    assert c.get(f"/v3/studies/{study['id']}", headers={"sec-fetch-site": "cross-site"}).status_code == 200      # unchanged
    svc.db.close()


def test_a_study_saved_before_41_still_has_its_frame():
    view = {"last": 101.5, "future_times": [1795813200, 1796072400]}            # 16:00 New York on 2026-11-27 and 2026-11-30
    f = SC.study_frame("2026-11-25T18:00:00+00:00", view)
    assert f["anchor"] == {"price": 101.5, "at": "2026-11-25T18:00:00Z", "basis": "study_quote", "bar_date": "2026-11-25"}
    assert [x["date"] for x in f["sessions"]] == ["2026-11-27", "2026-11-30"]
    assert f["sessions"][0]["close_at"] == "2026-11-27T18:00:00Z"                 # the day after Thanksgiving closes at 1 p.m.
    assert SC.study_frame("2026-11-25T18:00:00+00:00", {"last": None, "future_times": []}) is None
    assert date.fromisoformat(f["sessions"][1]["date"]).weekday() == 0
