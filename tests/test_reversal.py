"""Trend reversals: potential bottoms and tops, their stages, causality, option scoring and the API."""

import math

import numpy as np
import pandas as pd
import pytest
from fastapi.testclient import TestClient

from tradeassist import reversal, signalbook, trend
from tradeassist.api import create_app
from tradeassist.config import Settings
from tradeassist.db import Database
from tradeassist.features import FEATURE_DEFAULTS
from tradeassist.indicators import enrich
from tradeassist.options import reversal_score
from tradeassist.providers.demo import DemoData
from tradeassist.service import TradeAssistant


def frame(returns, seed=7, start="2023-01-02", climax_at=None):
    """Daily bars from a list of daily returns, with a heavy-volume, long-tailed day at `climax_at`."""
    rng = np.random.default_rng(seed)
    n = len(returns)
    close = 100 * np.exp(np.cumsum(returns))
    open_ = np.r_[close[0], close[:-1]] * (1 + rng.normal(0, 0.002, n))
    high = np.maximum(open_, close) * (1 + np.abs(rng.normal(0, 0.006, n)))
    low = np.minimum(open_, close) * (1 - np.abs(rng.normal(0, 0.006, n)))
    volume = rng.integers(800_000, 1_200_000, n).astype(float)
    if climax_at is not None:
        low[climax_at] = min(open_[climax_at], close[climax_at]) * 0.93      # a long lower tail
        volume[climax_at] *= 3.5
    return pd.DataFrame({"date": pd.bdate_range(start, periods=n), "open": open_, "high": high, "low": low,
                         "close": close, "volume": volume})


def v_shape(seed=7):
    """A gentle rise, a long fall to a climax low, then a recovery."""
    rng = np.random.default_rng(seed)
    r = np.r_[rng.normal(0.0008, 0.008, 220), rng.normal(-0.012, 0.008, 60), rng.normal(0.009, 0.008, 70)]
    return enrich(frame(r, seed=seed, climax_at=279))


@pytest.fixture()
def client():
    settings = Settings(data_mode="demo", db_path=":memory:")
    svc = TradeAssistant(settings, provider=DemoData(), db=Database(":memory:"))
    return TestClient(create_app(settings, svc))


def test_a_v_shaped_fall_and_recovery_reads_as_a_bottom_then_a_confirmed_reversal():
    df = v_shape()
    ev = reversal.evaluate(df, trend.state_series(df))
    st = ev["bottom"]["stage"]
    keys = reversal.events(ev)
    bottoms = [i for i, k in keys if k == "reversal:bottom_candidate" and 270 <= i <= 295]
    confirmed = [i for i, k in keys if k == "reversal:bottom_confirmed" and 280 <= i <= 330]
    assert bottoms and confirmed and min(bottoms) < min(confirmed)
    # No confirmed reversal up in the middle of the fall, and no top during it.
    assert not any(st[t] == "CONFIRMED" for t in range(230, 270))
    assert not any(ev["top"]["stage"][t] == "CONFIRMED" for t in range(240, 280))
    # Points are evidence: each side is within 0 to 100 on every bar.
    for side in reversal.SIDES:
        assert np.all((ev[side]["score"] >= 0) & (ev[side]["score"] <= 100.0 + 1e-9))
    assert sum(reversal.WEIGHTS.values()) == 100


def test_the_reading_on_any_day_uses_nothing_after_it():
    df = v_shape(seed=11)
    full = reversal.evaluate(df, trend.state_series(df))
    for k in (240, 281, 300, 349):
        cut = enrich(df.iloc[:k][["date", "open", "high", "low", "close", "volume"]])
        part = reversal.evaluate(cut, trend.state_series(cut))
        for side in reversal.SIDES:
            assert part[side]["stage"].tolist() == full[side]["stage"][:k].tolist()
            assert np.allclose(part[side]["score"], full[side]["score"][:k])


def test_levels_put_the_stop_beyond_the_turn_and_the_targets_ahead():
    df = v_shape()
    ev = reversal.evaluate(df, trend.state_series(df))
    t = next(i for i, k in reversal.events(ev) if k == "reversal:bottom_confirmed" and i >= 280)
    lv = reversal.levels(df, ev, "bottom", t)
    assert lv["invalidation"] < lv["close"] < lv["target_1"] < lv["target_2"]
    assert lv["risk_per_share"] == pytest.approx(lv["close"] - lv["invalidation"])
    assert lv["target_1"] - lv["close"] >= 0.5 * lv["risk_per_share"] - 1e-9
    assert "beyond" in lv["invalidation_is"]


def test_an_unfinished_bar_is_provisional_and_the_stage_comes_from_the_last_finished_bar():
    df = v_shape()
    df["bar_complete"] = True
    df.loc[df.index[-1], "bar_complete"] = False
    out = reversal.analyse(df, "TEST", trend.state_series(df))
    assert out["provisional"] is not None and out["provisional"]["status"] == "PROVISIONAL"
    assert out["confirmed"]["date"] == df["date"].iloc[-2].strftime("%Y-%m-%d")
    assert out["status"] == "PROVISIONAL" and "not chances" in out["note"]
    assert {e["kind"] for e in out["confirmed"]["bottom"]["evidence"]} == {"exhaustion", "confirmation"}
    with pytest.raises(ValueError):
        reversal.analyse(df.iloc[:60], "TEST")


def test_reversal_events_are_labelled_signals_with_a_direction():
    assert signalbook.signal_label("reversal:bottom_candidate") == "Potential bottom"
    assert signalbook.signal_direction("reversal:bottom_confirmed") == 1
    assert signalbook.signal_direction("reversal:top_candidate") == -1
    df = v_shape()
    rows = signalbook.collect("TEST", df, None, None, pd.DataFrame({"trend_state": ["NEUTRAL"] * len(df),
                                                                     "net_score": np.zeros(len(df))}))
    rev = [r for r in rows if r["signal_key"].startswith("reversal:")]
    assert rev and all(r["atr_pct"] is None or 0 < r["atr_pct"] < 1 for r in rev)


# --- scoring a contract ------------------------------------------------------------------------------

def contract(**over):
    k = {"premium": 500.0, "at_target": 600.0, "if_invalidated": -250.0, "if_unchanged": -40.0,
         "reward_to_risk": 2.4, "return_on_premium": 1.2, "breakeven_at_horizon": 102.0, "iv": 0.30,
         "spread_pct": 2.0, "open_interest": 5000, "avoid": [], "usable": True}
    k.update(over)
    return k


def side(stage="CONFIRMED", score=55.0):
    return {"side": "bottom", "stage": stage, "score": score, "exhaustion": 25.0, "confirmation": score - 25.0,
            "levels": {"atr": 2.0}}


THESIS = {"spot": 100.0, "target_1": 108.0, "target_2": 112.0, "invalidation": 96.0, "horizon": 20, "atm_iv": 0.30}


def test_a_well_set_up_contract_scores_good_and_the_parts_add_up():
    sc = reversal_score.score_contract(contract(), side(), THESIS, hv20=0.30, iv_rank=30, earnings_before_expiry=False)
    assert sc["verdict"] == "GOOD" and sc["score"] >= 70 and not sc["caps"]
    assert sum(c["weight"] for c in sc["components"]) == 100
    assert sc["score"] == pytest.approx(sum(c["points"] for c in sc["components"]), abs=0.2)
    assert "not a probability" in sc["note"]


def test_the_verdict_is_capped_by_the_reversal_and_by_the_contract():
    none = reversal_score.score_contract(contract(), side("NONE", 10), THESIS, 0.30, 30, False)
    assert none["verdict"] == "POOR" and "no reversal to trade" in none["caps"][0]
    watch = reversal_score.score_contract(contract(), side("WATCH", 40), THESIS, 0.30, 30, False)
    assert watch["verdict"] in ("FAIR", "POOR")
    decays = reversal_score.score_contract(contract(if_unchanged=-300.0), side(), THESIS, 0.30, 30, False)
    assert decays["verdict"] != "GOOD" and any("goes nowhere" in c for c in decays["caps"])
    far = reversal_score.score_contract(contract(), side(), {**THESIS, "target_1": 130.0}, 0.30, 30, False)
    assert far["verdict"] != "GOOD" and any("expected moves away" in c for c in far["caps"])
    late = reversal_score.score_contract(contract(), side(), {**THESIS, "invalidation": 85.0}, 0.30, 30, False)
    assert late["verdict"] != "GOOD" and any("daily ranges away" in c for c in late["caps"])
    avoid = reversal_score.score_contract(contract(avoid=["Wide spread"]), side(), THESIS, 0.30, 30, False)
    assert avoid["verdict"] == "AVOID"
    plain = reversal_score.score_contract(contract(), side(), THESIS, 0.30, 30, False)
    earn = reversal_score.score_contract(contract(), side(), THESIS, 0.30, 30, True)
    assert earn["score"] == pytest.approx(plain["score"] - 10, abs=0.11) and earn["earnings_note"]
    unpriced = reversal_score.score_contract(contract(premium=None), side(), THESIS, 0.30, 30, False)
    assert unpriced["verdict"] == "AVOID" and unpriced["score"] == 0


def test_past_cases_cap_the_verdict_only_when_there_are_enough():
    poor = {"cases": 40, "profitable": 0.2, "stopped_out": 0.7, "source": "after potential bottoms"}
    sc = reversal_score.score_contract(contract(), side(), THESIS, 0.30, 30, False)
    reversal_score.weigh_past_cases(sc, poor, signalbook.MIN_CASES)
    assert sc["verdict"] == "FAIR" and any("past cases" in c for c in sc["caps"])
    few = reversal_score.score_contract(contract(), side(), THESIS, 0.30, 30, False)
    reversal_score.weigh_past_cases(few, {**poor, "cases": 9}, signalbook.MIN_CASES)
    assert few["verdict"] == "GOOD" and few["cautions"]


# --- the API -----------------------------------------------------------------------------------------

def test_the_reversal_reading_and_its_history_through_the_api(client):
    r = client.get("/v2/reversal/AAPL")
    assert r.status_code == 200
    d = r.json()
    assert d["version"] == "reversal_v1.0" and d["confirmed"]["bottom"]["stage"] in reversal.STAGES
    assert len(d["daily"]) > 200 and {"bottom", "top", "close"} <= set(d["daily"][-1])
    keys = [p["key"] for p in d["past"]["signals"]]
    assert keys == ["reversal:bottom_candidate", "reversal:bottom_confirmed", "reversal:top_candidate",
                    "reversal:top_confirmed"]
    assert [h["horizon"] for h in d["past"]["signals"][0]["this_stock"]] == [5, 10, 20]
    assert d["past"]["history_built"] is False and any("Demo" in n for n in d["notes"])


def test_options_are_scored_and_the_best_one_simulated(client):
    r = client.post("/v2/reversal/options", json={"symbol": "AMZN", "horizon_days": 15})
    assert r.status_code == 200, r.text
    o = r.json()
    assert o["side"] in ("bottom", "top") and o["contract_type"] == ("CALL" if o["side"] == "bottom" else "PUT")
    best = o["best"]
    assert best["reversal_score"]["verdict"] in ("GOOD", "FAIR", "POOR", "AVOID")
    assert {x["key"] for x in best["outcomes"]} >= {"target_1", "unchanged", "invalidated"}
    assert best["simulation"]["curve"]["prices"] and best["simulation"]["eval_date"] <= best["expiration"]
    assert sum(o["verdict_counts"].values()) == len(o["contracts"])
    order = [reversal_score.VERDICT_ORDER.index(c["reversal_score"]["verdict"]) for c in o["contracts"]]
    assert order == sorted(order, reverse=True)
    assert all("scenarios" not in c for c in o["contracts"])
    # Score your own contract: one of those listed.
    k = o["contracts"][-1]
    mine = client.post("/v2/reversal/options", json={"symbol": "AMZN", "contracts_to_score": [
        {"option_type": k["option_type"], "strike": k["strike"], "expiration": k["expiration"]}]}).json()
    assert len(mine["own"]) == 1 and mine["own"][0]["strike"] == k["strike"]
    # Calls and puts at once make no sense for one reversal.
    mixed = client.post("/v2/reversal/options", json={"symbol": "AMZN", "contracts_to_score": [
        {"option_type": "call", "strike": k["strike"], "expiration": k["expiration"]},
        {"option_type": "put", "strike": k["strike"], "expiration": k["expiration"]}]})
    assert mixed.status_code == 400 and "not both" in mixed.json()["error"]
    wrong = client.post("/v2/reversal/options", json={"symbol": "AMZN", "side": "top", "contracts_to_score": [
        {"option_type": "call", "strike": k["strike"], "expiration": k["expiration"]}]})
    assert wrong.status_code == 400


def test_past_cases_come_from_the_signal_history_once_it_is_built(client):
    assert client.post("/v2/signals/history/build", json={"symbols": ["AAPL", "AMZN", "MSFT", "NVDA", "JPM", "KO"]}).status_code == 200
    d = client.get("/v2/reversal/AMZN").json()
    assert d["past"]["history_built"] and any(p["all_stocks"][1]["cases"] > 0 for p in d["past"]["signals"])
    o = client.post("/v2/reversal/options", json={"symbol": "AMZN"}).json()
    pc = o["best"]["past_cases"]
    assert pc and pc["cases"] > 0 and 0 <= pc["profitable"] <= 1 and sum(pc["histogram"]["counts"]) == pc["cases"]
    assert "not a forecast" in pc["note"]


def test_the_scan_and_the_feature_flag(client, tmp_path):
    s = client.get("/v2/reversal/scan?symbols=AAPL,AMZN,TOOLONGNAME")
    assert s.status_code == 200
    out = s.json()
    assert {r["symbol"] for r in out["rows"]} == {"AAPL", "AMZN"} and any("TOOLONGNAME" in e for e in out["errors"])
    off = Settings(data_mode="demo", db_path=tmp_path / "r.db", features={**FEATURE_DEFAULTS, "reversal_analysis": False})
    c = TestClient(create_app(off, TradeAssistant(off, provider=DemoData(), db=Database(":memory:"))))
    assert c.get("/v2/reversal/AAPL").status_code == 404
    assert c.get("/v2/trend/AAPL").status_code == 200
