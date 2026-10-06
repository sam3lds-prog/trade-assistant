"""Options contract evaluator: scenarios, rankings, the avoid list and the API."""

from datetime import date, datetime

import pytest
from fastapi.testclient import TestClient

from tradeassist.api import create_app
from tradeassist.config import Settings
from tradeassist.db import Database
from tradeassist.optionmath import NY
from tradeassist.options import evaluator as oe
from tradeassist.providers.demo import DemoData
from tradeassist.service import TradeAssistant


def test_horizon_and_expiration_choice():
    assert oe.horizon_date(date(2026, 10, 6), 20) == date(2026, 11, 3)
    exps = ["2026-10-09", "2026-10-16", "2026-11-06", "2026-11-13", "2026-11-20", "2026-12-18", "2027-01-15", "2027-03-19"]
    chosen = oe.pick_expirations(exps, date(2026, 10, 6), date(2026, 11, 3))
    assert 1 <= len(chosen) <= 4 and chosen[0] == "2026-11-13" and all(c >= "2026-11-10" for c in chosen)
    assert oe.pick_expirations(["2026-10-09"], date(2026, 10, 6), date(2026, 11, 3)) == []


@pytest.fixture(scope="module")
def chains():
    settings = Settings(data_mode="demo")
    svc = TradeAssistant(settings, provider=DemoData(today=date(2026, 10, 6)), db=Database(":memory:"))
    exps = svc.market.expirations("AAPL")
    return svc, [svc._chain_for("AAPL", e) for e in exps]


def run(chains, direction=1, **kw):
    svc, analyses = chains
    spot = analyses[0].spot
    args = dict(symbol="AAPL", direction=direction, spot=spot, analyses=analyses,
                target_1=spot * (1 + 0.08 * direction), target_2=spot * (1 + 0.12 * direction),
                invalidation=spot * (1 - 0.04 * direction), horizon=15, rate=0.04,
                now=datetime(2026, 10, 6, 12, 0, tzinfo=NY))
    args.update(kw)
    return oe.evaluate(**args)


def test_rankings_follow_their_definitions(chains):
    r = run(chains)
    picks = {p["category"]: p["contract"] for p in r["picks"]}
    assert set(picks) <= set(oe.CATEGORIES) and "BEST THESIS MATCH" in picks
    usable = [k for k in r["contracts"] if k["usable"]]
    assert all(k["option_type"] == "CALL" for k in r["contracts"])
    assert picks["LOWEST COST"]["premium"] == min(k["premium"] for k in usable)
    assert picks["BEST THESIS MATCH"]["return_on_premium"] == max(k["return_on_premium"] for k in usable)
    if "BEST RISK-ADJUSTED" in picks:
        assert picks["BEST RISK-ADJUSTED"]["reward_to_risk"] == max(k["reward_to_risk"] or 0 for k in usable)
    if "MOST CONSERVATIVE" in picks:
        assert picks["MOST CONSERVATIVE"]["dte"] >= 2 * r["thesis"]["horizon_calendar_days"]
    for k in usable:
        assert k["at_target"] > 0 and k["if_invalidated"] >= -k["premium"] - 1e-6
        assert len(k["scenarios"]) == 6
        base = {(m["when"], m["iv_shift"]): m for m in k["scenarios"]}
        assert base[("horizon", 0.05)]["target_1"] >= base[("horizon", -0.05)]["target_1"]     # long vega
        assert base[("horizon", 0.0)]["target_2"] >= base[("horizon", 0.0)]["target_1"]
    assert "closes below" in r["exit_rule"] and r["market"]["reach_target_1_by_horizon"] < 0.5


def test_puts_for_a_bearish_thesis_and_the_avoid_list(chains):
    r = run(chains, direction=-1, budget=150)
    assert all(k["option_type"] == "PUT" for k in r["contracts"])
    assert any("budget" in " ".join(a["reasons"]) for a in r["avoid"])
    assert all(k["premium"] <= 150 for k in r["contracts"] if k["usable"])
    early = [k for k in r["contracts"] if k["expiration"] < r["thesis"]["horizon_date"]]
    assert early and all(not k["usable"] and any("before the" in x for x in k["avoid"]) for k in early)
    with pytest.raises(oe.OptionsEvaluationError):
        run(chains, direction=-1, target_1=chains[1][0].spot * 1.1)
    with pytest.raises(oe.OptionsEvaluationError):
        run(chains, invalidation=chains[1][0].spot * 1.01)


def test_options_evaluator_through_the_api(tmp_path):
    settings = Settings(data_mode="demo", db_path=tmp_path / "t.db")
    c = TestClient(create_app(settings, TradeAssistant(settings, provider=DemoData(), db=Database(":memory:"))))
    r = c.post("/v2/options/evaluate", json={"symbol": "MSFT", "direction": "BULLISH", "horizon_days": 10}).json()
    assert r["thesis"]["contract_type"] == "CALL" and r["levels_source"]["target_1"] == "suggested"
    assert r["stock_grade"]["grade"] and len(r["expirations"]) <= 4
    k = next(x for x in r["contracts"] if x["usable"])
    cmp = c.post("/v2/options/compare", json={"symbol": "MSFT", "direction": "BULLISH", "horizon_days": 10,
                                              "contracts_to_compare": [{"option_type": "CALL", "strike": k["strike"],
                                                                        "expiration": k["expiration"]}]}).json()
    assert len(cmp["contracts"]) == 1 and cmp["contracts"][0]["at_target"] == pytest.approx(k["at_target"], rel=1e-6)
    wrong = c.post("/v2/options/compare", json={"symbol": "MSFT", "contracts_to_compare": [
        {"option_type": "PUT", "strike": k["strike"], "expiration": k["expiration"]}]})
    assert wrong.status_code == 400
    missing = c.post("/v2/options/compare", json={"symbol": "MSFT", "contracts_to_compare": [
        {"option_type": "CALL", "strike": 1.5, "expiration": k["expiration"]}]})
    assert missing.status_code == 400 and "Not in the chain" in missing.json()["error"]
