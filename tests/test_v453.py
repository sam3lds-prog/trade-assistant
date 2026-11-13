"""4.5.3: forecast validation and strategy performance.

The four approaches compared on the same studies with accuracy, bias, coverage, width and skill against no change;
confidence intervals from a bootstrap of whole calendar weeks (correlated studies are not independent experiments);
verdicts that need enough pairs over enough weeks; breakdowns; costs and the simulation profiles; Scenario H (more
expensive, not better); and Qualified setups' frozen outcome records graded as historical hypothetical, model-only
or ungradable, apart from observed fills."""

from __future__ import annotations

import json
from datetime import date, datetime, timedelta, timezone

import pandas as pd
import pytest
from fastapi.testclient import TestClient

from tradeassist import contracts as C
from tradeassist.api import create_app
from tradeassist.config import Settings
from tradeassist.db import Database
from tradeassist.optionmath import NY
from tradeassist.providers.demo import DemoData
from tradeassist.service import TradeAssistant
from tradeassist.v3 import forward as F
from tradeassist.v3.outcomes import metrics
from tests.test_one_click import _wait


@pytest.fixture(scope="module")
def app(tmp_path_factory):
    path = tmp_path_factory.mktemp("v453") / "t.db"
    s = Settings(data_mode="demo", db_path=path, desk_worker="off", scenario_provider="demo", scenario_auto=False)
    svc = TradeAssistant(s, provider=DemoData(), db=Database(path))
    c = TestClient(create_app(s, svc))
    sid = _wait(c, c.post("/v3/studies", json={"symbol": "AAPL", "horizon_days": 5, "scenarios": False}).json()["id"])["id"]
    yield c, svc, sid
    svc.db.close()


# --- statistics -------------------------------------------------------------------------------------------------------

def test_correlated_observations_are_not_counted_as_independent():
    rng = __import__("numpy").random.default_rng(1)
    # 40 pairs in 4 weeks, each week moving together: the interval is far wider than 40 independent pairs would give.
    weeks = [f"2026-W{w}" for w in (40, 41, 42, 43) for _ in range(10)]
    shift = {w: rng.normal(0, 1.0) for w in set(weeks)}
    diffs = [shift[w] + rng.normal(0, 0.05) for w in weeks]
    clustered = F.cluster_ci(diffs, weeks)
    independent = F.cluster_ci(diffs, [str(i) for i in range(40)])
    width = lambda ci: ci["ci95"][1] - ci["ci95"][0]          # noqa: E731
    assert clustered["clusters"] == 4 and width(clustered) > 2 * width(independent)
    assert F.verdict_of(40, clustered) == "insufficient"                      # 40 pairs, but only 4 weeks


@pytest.mark.parametrize("diffs, verdict", [([-0.5] * 40, "supported"), ([0.5] * 40, "worse"), ([0.0] * 40, "none")])
def test_a_verdict_needs_enough_pairs_over_enough_weeks(diffs, verdict):
    weeks = [f"W{i % 10}" for i in range(len(diffs))]
    noisy = [d + (0.05 if i % 2 else -0.05) for i, d in enumerate(diffs)]
    assert F.verdict_of(len(noisy), F.cluster_ci(noisy, weeks)) == verdict
    assert F.verdict_of(29, F.cluster_ci(noisy[:29], weeks[:29])) == "insufficient"


def test_a_smaller_average_alone_is_not_significant():
    import numpy as np
    rng = np.random.default_rng(3)
    diffs = list(rng.normal(-0.05, 1.0, 40))                                # a little smaller on average, lots of noise
    weeks = [f"W{i % 10}" for i in range(40)]
    ci = F.cluster_ci(diffs, weeks)
    assert np.mean(diffs) < 0 and F.verdict_of(40, ci) in ("none", "suggestive")


# --- the scorecard ------------------------------------------------------------------------------------------------------

def _forecast(svc, sid, variant, target, median, realized, lo=-3.0, hi=3.0, profile=None, run_id=None, kind="earnings", regime="bull"):
    with svc.db.tx() as cur:
        cur.execute("""INSERT INTO scenario_forecasts (id, study_id, mode, symbol, horizon, variant, provider, run_id, made_at, window_start, target_date,
                       anchor_price, anchor_date, median_pct, low_pct, high_pct, status, realized_pct, abs_error, covered, direction_hit, signed_error,
                       width, profile, catalyst_kind, market_regime, band_basis)
                       VALUES (?, ?, 'demo', ?, 5, ?, 'x', ?, ?, ?, ?, 100, ?, ?, ?, ?, 'graded', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (C.new_id("sfc"), sid, f"S{sid[-3:]}", variant, run_id, "2026-01-01T00:00:00Z", "2026-01-02T14:30:00+00:00", target, target,
                     median, lo, hi, realized, abs(realized - median), int(lo <= realized <= hi),
                     None if median == 0 else int((median > 0) == (realized > 0)), realized - median, hi - lo, profile, kind, regime,
                     "base_band" if variant == "no_change" else "own"))


def _studies(svc, n):
    ids = []
    with svc.db.tx() as cur:
        for _ in range(n):
            sid = C.new_id("study")
            cur.execute("""INSERT INTO studies (id, mode, symbol, created_at, as_of, horizon_days, thesis, status, engine_version, contract_version,
                           inputs_json, evidence_json, forecast_json, strategies_json, fingerprint)
                           VALUES (?, 'demo', 'XYZ', '2026-01-01T00:00:00Z', '2026-01-01T00:00:00Z', 5, 'swing', 'complete', 't', 't', '{}', '{}', '{}', '{}', ?)""",
                        (sid, sid))
            ids.append(sid)
    return ids


def _usage(svc, run_id, provider, usd, profile=None):
    with svc.db.tx() as cur:
        cur.execute("""INSERT INTO scenario_usage (run_id, study_id, provider, profile, created_at, started_at, status, day, month, measured_usd,
                       cost_confidence, updated_at) VALUES (?, 's', ?, ?, '2026-01-01T00:00:00Z', '2026-01-01T00:00:00Z', 'complete', '2026-01-01',
                       '2026-01', ?, 'user_entered', '2026-01-01T00:00:00Z')""", (run_id, provider, profile, usd))


def test_the_scorecard_compares_the_four_approaches_and_says_what_the_evidence_supports(app):
    """Scenario G: 40 studies over 10 weeks where the provider misses by about 0.6 points less than the base."""
    c, svc, _ = app
    ids = _studies(svc, 40)
    import numpy as np
    rng = np.random.default_rng(5)
    for i, sid in enumerate(ids):
        target = (date(2026, 3, 2) + timedelta(weeks=i % 10, days=i % 5)).isoformat()
        realized = float(rng.normal(0.5, 2.0))
        _forecast(svc, sid, "base", target, 0.0 + float(rng.normal(0, 1.5)) + (1.5 if i % 2 else -1.5) * 0 + 1.0, realized)
        _forecast(svc, sid, "no_change", target, 0.0, realized)
        _forecast(svc, sid, "provider", target, realized + float(rng.normal(0, 0.3)), realized, lo=-4, hi=4, profile="quick" if i % 2 else "standard",
                  run_id=f"srun_p{i}")
        _forecast(svc, sid, "control", target, realized + float(rng.normal(0, 0.35)), realized, lo=-4, hi=4, run_id=f"srun_c{i}")
        _usage(svc, f"srun_p{i}", "mirofish", 0.40, "quick" if i % 2 else "standard")
        _usage(svc, f"srun_c{i}", "ai", 0.02)
    sc = c.get("/v3/scenarios/scorecard", params={"horizon": 5}).json()
    by = {v["variant"]: v for v in sc["variants"]}
    assert by["provider"]["n"] == by["base"]["n"] == 40
    for v in by.values():
        assert {"mae", "rmse", "bias", "coverage", "width", "direction_hits", "by_catalyst", "by_market_regime", "by_volatility"} <= set(v)
    assert "borrowed" in by["no_change"]["band_note"] and by["provider"]["skill_vs_no_change"] > 0
    assert set(by["provider"]["by_volatility"]) <= {"low", "mid", "high", "unknown"}
    pb = next(p for p in sc["pairs"] if p["a"] == "provider" and p["b"] == "base")
    assert pb["n"] == 40 and pb["clusters"] == 10 and pb["ci95"][1] < 0 and pb["verdict"] == "supported"
    assert "statistically supported improvement" in sc["verdict"]
    pc = next(p for p in sc["pairs"] if p["a"] == "provider" and p["b"] == "control")
    assert pc["verdict"] in ("none", "suggestive", "insufficient", "supported", "worse")
    assert sc["costs"]["provider_average_usd"] == pytest.approx(0.40) and sc["costs"]["control_average_usd"] == pytest.approx(0.02)
    assert {p["profile"] for p in sc["profiles"]} == {"quick", "standard"} and all(p["average_cost_usd"] == pytest.approx(0.40) for p in sc["profiles"])
    assert "bootstrap" in sc["method"] and "weeks" in sc["method"]
    with svc.db.tx() as cur:
        cur.execute("DELETE FROM scenario_forecasts WHERE study_id IN (%s)" % ",".join("?" * len(ids)), ids)


def test_scenario_h_more_expensive_and_not_better_is_reported_as_such(app):
    c, svc, _ = app
    ids = _studies(svc, 36)
    for i, sid in enumerate(ids):
        target = (date(2026, 5, 4) + timedelta(weeks=i % 9, days=i % 5)).isoformat()
        realized = 1.0 if i % 2 else -1.0
        _forecast(svc, sid, "base", target, 0.2, realized)
        _forecast(svc, sid, "no_change", target, 0.0, realized)
        _forecast(svc, sid, "provider", target, 0.3 if i % 3 else -0.3, realized, run_id=f"srun_hp{i}", profile="deep")
        _forecast(svc, sid, "control", target, 0.3 if i % 3 else -0.3, realized, run_id=f"srun_hc{i}")
        _usage(svc, f"srun_hp{i}", "mirofish", 2.50, "deep")
        _usage(svc, f"srun_hc{i}", "ai", 0.02)
    sc = c.get("/v3/scenarios/scorecard", params={"horizon": 5}).json()
    pc = next(p for p in sc["pairs"] if p["a"] == "provider" and p["b"] == "control")
    assert pc["n"] == 36 and pc["verdict"] == "none"
    assert "has not shown a benefit over the cheaper single prompt" in sc["verdict"] and "no weight in the base" in sc["verdict"]
    assert "+2.48 USD per study more" in sc["verdict"]
    with svc.db.tx() as cur:
        cur.execute("DELETE FROM scenario_forecasts WHERE study_id IN (%s)" % ",".join("?" * len(ids)), ids)


def test_frozen_forecasts_keep_their_provenance(app):
    c, svc, sid = app
    svc.forward.freeze_study(sid, now=datetime(2026, 1, 1, tzinfo=timezone.utc))
    rows = {r["variant"]: dict(r) for r in svc.db.query("SELECT * FROM scenario_forecasts WHERE study_id = ?", (sid,))}
    assert rows["base"]["data_cutoff"] and rows["base"]["model_version"] and rows["base"]["band_basis"] == "own"
    assert rows["no_change"]["band_basis"] == "base_band" and rows["base"]["width"] == pytest.approx(rows["base"]["high_pct"] - rows["base"]["low_pct"])


# --- strategy outcomes ------------------------------------------------------------------------------------------------

def _decision(units, direction="LONG", structures=("long_call",)):
    return {"id": C.new_id("sdec"), "status": "qualified", "made_at": "2026-10-22T20:00:00+00:00",
            "setup": {"structures": list(structures), "units": units, "direction": direction, "case": None, "mode": "base", "net_pnl": 40.0,
                      "costs": 10.0, "capital": 210.0, "max_loss": 210.0, "data_quality": "execution", "by_case": {"central": {"net": 40.0}}}}


def _valuation(*structs):
    return {"id": "sval_o", "snapshot_at": "2026-10-22T20:00:00Z", "structures": list(structs)}


VERSION = {"id": "sver_o", "anchor": {"price": 100.0}, "catalyst": {"kind": "earnings"}, "horizon_sessions": 5}


def _call(exp, strike=100.0, bid=2.0, ask=2.1, iv=0.4, eval_date="2026-10-29"):
    return {"key": "long_call", "label": "Long call", "quantity": 1, "eval_date": eval_date, "eval_session": 5, "costs": 10.0, "capital": 210.0,
            "max_loss": 210.0, "quality": {"tier": "execution"},
            "legs": [{"option_symbol": f"XYZ{exp}C{strike:g}", "option_type": "call", "strike": strike, "expiration": exp, "side": "buy",
                      "contracts": 1, "bid": bid, "ask": ask, "mid": (bid + ask) / 2, "iv": iv}]}


def _bars(monkeypatch, svc, closes):
    frame = pd.DataFrame({"date": pd.to_datetime(list(closes)), "open": list(closes.values()), "high": list(closes.values()),
                          "low": list(closes.values()), "close": list(closes.values()), "volume": [1e6] * len(closes)})
    monkeypatch.setattr(svc.market, "bars", lambda sym, years=1.0, complete=True: frame)


AFTER = datetime(2026, 10, 30, 20, 0, tzinfo=NY).timestamp()


def test_an_option_held_to_its_expiration_is_graded_at_its_intrinsic_value(app, monkeypatch):
    c, svc, sid = app
    d = _decision({"long_call": 1})
    oid = svc.outcomes.freeze(sid, d, _valuation(_call("2026-10-29")), VERSION)
    rec = json.loads(svc.db.one("SELECT record_json FROM scenario_strategy_outcomes WHERE id = ?", (oid,))["record_json"])
    assert rec["parts"][0]["legs"][0]["entry_price"] == 2.1 and "ask" in rec["entry_policy"] and rec["parts"][0]["entry_cash"] == pytest.approx(210)
    assert svc.outcomes.freeze(sid, d, _valuation(_call("2026-10-29")), VERSION) is not None   # INSERT OR IGNORE: one per decision
    assert svc.db.one("SELECT COUNT(*) AS n FROM scenario_strategy_outcomes WHERE decision_id = ?", (d["id"],))["n"] == 1
    _bars(monkeypatch, svc, {"2026-10-22": 100.0, "2026-10-29": 105.0})
    svc.outcomes.grade(now=AFTER)
    r = svc.db.one("SELECT * FROM scenario_strategy_outcomes WHERE id = ?", (oid,))
    out = json.loads(r["outcome_json"])
    assert r["status"] == "graded" and r["category"] == "historical_hypothetical"
    assert r["pnl"] == pytest.approx(100 * (5.0 - 2.1)) and out["direction_right"] is True and out["expired_worthless"] is False
    assert "not a realized profit" in out["label"]


def test_an_open_option_uses_its_own_traded_close_or_is_labelled_model_only(app, monkeypatch):
    c, svc, sid = app
    _bars(monkeypatch, svc, {"2026-10-22": 100.0, "2026-10-29": 103.0})
    # Its own close that session (Tradier's history), less half the entry spread.
    a = svc.outcomes.freeze(sid, _decision({"long_call": 2}), _valuation(_call("2026-11-20")), VERSION)
    monkeypatch.setattr(svc.outcomes, "_option_close", lambda sym, day: 3.5)
    svc.outcomes.grade(now=AFTER)
    r = svc.db.one("SELECT * FROM scenario_strategy_outcomes WHERE id = ?", (a,))
    assert r["category"] == "historical_hypothetical" and r["pnl"] == pytest.approx(2 * 100 * ((3.5 - 0.05) - 2.1))
    # No trade that session: valued by the model, and labelled model-only.
    b = svc.outcomes.freeze(sid, _decision({"long_call": 1}), _valuation(_call("2026-11-20")), VERSION)
    monkeypatch.setattr(svc.outcomes, "_option_close", lambda sym, day: None)
    svc.outcomes.grade(now=AFTER)
    r = svc.db.one("SELECT * FROM scenario_strategy_outcomes WHERE id = ?", (b,))
    out = json.loads(r["outcome_json"])
    assert r["category"] == "model_only" and "theoretical" in out["label"] and any("model-only" in n for n in out["notes"])


def test_shares_pay_their_costs_and_a_missing_close_is_ungradable_not_invented(app, monkeypatch):
    c, svc, sid = app
    shares = {"key": "shares", "label": "Shares", "quantity": 10, "entry": 100.05, "eval_date": "2026-10-29", "eval_session": 5, "costs": 1.0,
              "capital": 1000.5, "max_loss": 1000.5, "quality": {"tier": "execution"}, "legs": []}
    a = svc.outcomes.freeze(sid, _decision({"shares": 1}, structures=("shares",)), _valuation(shares), VERSION)
    _bars(monkeypatch, svc, {"2026-10-22": 100.0, "2026-10-29": 102.0})
    svc.outcomes.grade(now=AFTER)
    r = svc.db.one("SELECT * FROM scenario_strategy_outcomes WHERE id = ?", (a,))
    bps = svc.settings.stock_cost_bps / 1e4
    assert r["pnl"] == pytest.approx(10 * (102.0 - 100.05) - bps * 10 * (100.05 + 102.0), abs=0.01)
    b = svc.outcomes.freeze(sid, _decision({"shares": 1}, structures=("shares",)), _valuation(shares), VERSION)
    _bars(monkeypatch, svc, {"2026-10-22": 100.0})
    svc.outcomes.grade(now=AFTER)
    assert svc.db.one("SELECT status FROM scenario_strategy_outcomes WHERE id = ?", (b,))["status"] == "open"       # waits for the bar
    svc.outcomes.grade(now=AFTER + 20 * 86400)
    assert svc.db.one("SELECT status, category FROM scenario_strategy_outcomes WHERE id = ?", (b,))["category"] == "ungradable"


def test_the_strategy_metrics_keep_the_categories_apart(app):
    c, svc, sid = app
    s = svc.outcomes.summary()
    assert set(s["categories"]) == {"historical_hypothetical", "model_only", "observed"}
    hh = s["categories"]["historical_hypothetical"]
    assert hh["n"] >= 2 and not hh["enough"] and "never added together" in s["note"]
    m = metrics([{"pnl": 10, "return_on_risk": 0.1, "strategy": "a", "direction": "LONG", "catalyst_kind": "x", "horizon": 5,
                  "outcome_json": json.dumps({"gross_pnl": 12, "costs": 2, "expired_worthless": False, "direction_right": True})},
                 {"pnl": -30, "return_on_risk": -0.3, "strategy": "a", "direction": "SHORT", "catalyst_kind": "x", "horizon": 5,
                  "outcome_json": json.dumps({"gross_pnl": -28, "costs": 2, "expired_worthless": True, "direction_right": True})},
                 {"pnl": 5, "return_on_risk": 0.05, "strategy": "b", "direction": "LONG", "catalyst_kind": "y", "horizon": 1,
                  "outcome_json": json.dumps({"gross_pnl": 6, "costs": 1, "expired_worthless": None, "direction_right": False})}])
    assert m["n"] == 3 and m["win_rate"] == pytest.approx(2 / 3) and m["net_pnl"] == -15 and m["max_drawdown"] == -30 and m["worst_loss"] == -30
    assert m["expired_worthless_rate"] == 0.5 and m["direction_right_strategy_lost"] == 1 and m["cost_to_gross"] == pytest.approx(5 / 46)
    assert set(m["by_strategy"]) == {"a", "b"} and m["by_direction"]["SHORT"]["n"] == 1


def test_observed_fills_are_reported_apart_and_only_realized_when_closed(app):
    c, svc, sid = app
    r = {"study_id": sid, "recommended_at": "2026-10-22T20:00:00+00:00"}
    assert svc.outcomes.observed(r) is None
    now = "2026-10-23T15:00:00+00:00"
    with svc.db.tx() as cur:
        for pid, side, price in (("prop_buy", "buy", 2.10), ("prop_sell", "sell", 2.90)):
            cur.execute("""INSERT INTO trade_proposals (id, mode, account_kind, created_at, updated_at, source, status, symbol, instrument, side, quantity,
                           order_type, limit_price, terms_json, order_hash, study_id, expires_at) VALUES (?, 'demo', 'paper', ?, ?, 'research', 'filled', 'XYZ',
                           'option', ?, 1, 'limit', ?, ?, 'h', ?, ?)""",
                        (pid, now, now, side, price, json.dumps({"option_symbol": "XYZ261120C100"}), sid, "2026-10-30T00:00:00+00:00"))
            cur.execute("""INSERT INTO broker_orders (id, proposal_id, mode, account_kind, broker, client_order_id, state, created_at, updated_at, terms_json,
                           filled_quantity) VALUES (?, ?, 'demo', 'paper', 'paper', ?, 'filled', ?, ?, '{}', 1)""", (f"o_{pid}", pid, f"c_{pid}", now, now))
            cur.execute("INSERT INTO broker_fills (order_id, at, quantity, price, fees, external_id) VALUES (?, ?, 1, ?, 0.65, ?)", (f"o_{pid}", now, price, f"x_{pid}"))
    o = svc.outcomes.observed(r)
    assert o["fills"] == 2 and o["closed"] and o["realized_pnl"] == pytest.approx(100 * (2.90 - 2.10) - 1.30) and o["accounts"] == ["paper"]


def test_the_migration_adds_the_validation_columns_and_table(tmp_path):
    db = Database(tmp_path / "m.db")
    cols = {r[1] for r in db.query("PRAGMA table_info(scenario_forecasts)")}
    assert {"data_cutoff", "model_version", "profile", "config_json", "catalyst_kind", "market_regime", "band_basis", "signed_error", "width"} <= cols
    assert db.one("SELECT name FROM sqlite_master WHERE name = 'scenario_strategy_outcomes'")
    db.close()
