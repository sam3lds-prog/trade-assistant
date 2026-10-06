"""Position evaluator: suggested levels, components, grades, caps and short-sale risks."""

from datetime import date

import pytest
from fastapi.testclient import TestClient

from tradeassist import evaluator, trend
from tradeassist.api import create_app
from tradeassist.config import Settings
from tradeassist.db import Database
from tradeassist.indicators import enrich
from tradeassist.providers.demo import DemoData
from tradeassist.service import TradeAssistant
from test_trend import walk

BULL = {"regime": "BULL_CALM", "label": "Bull market, calm", "confirmed": {"text": "Calm uptrend."}}


@pytest.fixture(scope="module")
def rising():
    df = enrich(walk(n=500, drift=0.002, seed=3))
    return df, trend.analyse(df)


def grade(df, t, direction, **kw):
    args = dict(symbol="TEST", direction=direction, df=df, entry=float(df["close"].iloc[-1]), trend=t, regime=BULL,
                data_quality={"status": "ok"}, earnings={"date": "2027-03-01", "estimated": True}, signals=None,
                account_size=50_000, risk_pct=1.0, today=date(2026, 10, 6))
    args.update(kw)
    return evaluator.evaluate(**args)


def test_grades_and_caps():
    assert [evaluator.grade_for(x) for x in (85, 70, 55, 40, 10)] == ["A", "B", "C", "D", "AVOID"]
    assert evaluator.cap("A", "C") == "C" and evaluator.cap("D", "C") == "D" and evaluator.cap("B", "AVOID") == "AVOID"


def test_suggested_levels_sit_on_the_right_sides(rising):
    df, _ = rising
    entry = float(df["close"].iloc[-1])
    long_ = evaluator.suggest_levels(df, 1, entry)
    short = evaluator.suggest_levels(df, -1, entry)
    assert long_["invalidation"] < entry < long_["target_1"] <= long_["target_2"]
    assert short["target_2"] <= short["target_1"] < entry < short["invalidation"]


def test_a_long_with_the_trend_grades_well_and_a_short_against_it_is_capped(rising):
    df, t = rising
    state = (t["provisional"] or t["confirmed"])["state"]
    ev = grade(df, t, 1)
    assert ev["grade"] in ("A", "B", "C") and ev["direction"] == "LONG" and "short_sale_risk" not in ev
    assert sum(c["weight"] for c in ev["components"]) == 100
    sz = ev["sizing"]
    assert sz["shares"] == int(500 // sz["risk_per_share"]) and sz["risk_budget"] == 500
    short = grade(df, t, -1)
    assert evaluator.GRADE_ORDER.index(short["grade"]) > evaluator.GRADE_ORDER.index(ev["grade"])
    if state == "STRONG_UP":
        assert any(c["grade"] == "D" for c in short["caps"]) and short["grade"] in ("D", "AVOID")
    texts = " ".join(r["text"] for r in short["short_sale_risk"])
    assert "Borrow availability" in texts and "no ceiling" in texts
    assert "not a probability" in ev["note"]


def test_hard_stops(rising):
    df, t = rising
    entry = float(df["close"].iloc[-1])
    wrong = grade(df, t, 1, invalidation=entry * 1.05)
    assert wrong["grade"] == "AVOID" and "wrong side" in wrong["caps"][0]["reason"]
    thin = grade(df, t, 1, invalidation=entry * 0.95, target_1=entry * 1.02)
    assert thin["grade"] == "AVOID" and thin["reward_risk"]["target_1"] == pytest.approx(0.4)
    soon = grade(df, t, 1, earnings={"date": "2026-10-07", "estimated": False})
    assert evaluator.GRADE_ORDER.index(soon["grade"]) >= evaluator.GRADE_ORDER.index("C")
    assert {c["key"]: c for c in soon["components"]}["catalysts"]["score"] == 10
    bad = grade(df, t, 1, data_quality={"status": "fail"})
    assert bad["grade"] == "AVOID"
    mine = grade(df, t, 1, invalidation=entry * 0.9, target_1=entry * 1.3, target_2=entry * 1.5)
    assert mine["levels"]["invalidation_source"] == "yours" and mine["reward_risk"]["target_1"] == pytest.approx(3.0)
    with pytest.raises(evaluator.EvaluationError):
        grade(df, t, 0)


def test_evaluate_through_the_api(tmp_path):
    settings = Settings(data_mode="demo", db_path=tmp_path / "t.db")
    c = TestClient(create_app(settings, TradeAssistant(settings, provider=DemoData(), db=Database(":memory:"))))
    ev = c.post("/v2/position/evaluate", json={"symbol": "MSFT", "direction": "SHORT"}).json()
    assert ev["grade"] in evaluator.GRADE_ORDER and ev["entry_source"] == "current price" and ev["short_sale_risk"]
    p = c.post("/v2/positions", json={"symbol": "MSFT", "quantity": 10, "invalidation_price": 1.0, "target_1": 5000,
                                      "entry": {"price": 250}}).json()
    ev2 = c.post("/v2/position/evaluate", json={"position_id": p["id"]}).json()
    assert ev2["entry"] == 250 and ev2["levels"]["invalidation"] == 1.0 and ev2["position_id"] == p["id"]
    exps = c.get("/api/expirations", params={"symbol": "MSFT"}).json()["expirations"]
    o = c.post("/v2/positions", json={"symbol": "MSFT", "instrument_type": "OPTION", "quantity": 1,
                                      "option": {"option_type": "CALL", "strike": 250, "expiration": exps[-1]["date"]}}).json()
    assert c.post("/v2/position/evaluate", json={"position_id": o["id"]}).status_code == 400
    assert c.post("/v2/position/evaluate", json={}).status_code == 400
