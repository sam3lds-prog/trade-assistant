"""2.5.1: six fixes from the review of 2.5.

1. The chances about the target and the invalidation use the same forward as the range and the
   expected price (`options.evaluator.stock_outlook`).
2. The share trade has one price: a typed entry moves the chance the shares are ahead; nothing else.
3. The comparison shows the outcome band (10th, 50th and 90th percentile), not an average near $0.
5. A Today row carries how often its score range was higher 10 days later, once the history exists.
"""

from __future__ import annotations

import math
from datetime import date, datetime
from types import SimpleNamespace

import numpy as np
import pytest
from fastapi.testclient import TestClient

from tradeassist.api import create_app
from tradeassist.config import Settings
from tradeassist.db import Database
from tradeassist.optionmath import NY, prob_finish_beyond
from tradeassist.options.evaluator import PERCENTILE_Z, stock_outlook, value_contract
from tradeassist.providers.demo import DemoData
from tradeassist.service import TradeAssistant
from tradeassist.signalbook import MAIN_HORIZON, SCORE_BUCKETS, bucket_for
from tradeassist.simulate import WEIGHTS, Z
from tradeassist.today import Today, past_bucket

T, VOL, SPOT = 30 / 365, 0.35, 100.0
HARD_TO_BORROW = math.log(0.9) / T          # a forward 10% under the stock


# --- 1. one forward ---------------------------------------------------------------------------------

def test_the_chance_past_the_target_uses_the_forward_not_the_stock_price():
    """A bearish target between the forward (90) and the stock (100): on the spot the target looks far
    away, on the forward it is nearly a coin flip. The chance must be the forward's."""
    out = stock_outlook(spot=SPOT, direction=-1, target_1=95.0, invalidation=110.0, vol=VOL, growth=HARD_TO_BORROW, t=T)
    fwd = out["forward_at_horizon"]
    assert fwd == pytest.approx(90.0)
    on_fwd = prob_finish_beyond(fwd, 95.0, T, VOL, above=False)
    on_spot = prob_finish_beyond(SPOT, 95.0, T, VOL, above=False)
    assert out["reach_target_1_by_horizon"] == pytest.approx(on_fwd)
    assert abs(on_fwd - on_spot) > 0.2
    assert out["beyond_invalidation_at_horizon"] == pytest.approx(prob_finish_beyond(fwd, 110.0, T, VOL, above=True))
    # One distribution: the weighted prices agree with the chance, the range and the percentiles.
    sd = VOL * math.sqrt(T)
    prices = fwd * np.exp(-0.5 * sd * sd + sd * Z)
    assert float(WEIGHTS[prices < 95.0].sum()) == pytest.approx(on_fwd, abs=2e-3)
    assert out["expected_price_at_horizon"] == pytest.approx(fwd, rel=1e-6)
    assert out["stock_percentiles_at_horizon"]["p50"] < fwd < out["range_1sd_at_horizon"]["high"]


# --- 2. one price for the shares --------------------------------------------------------------------

def test_a_typed_entry_moves_only_the_chance_the_shares_are_ahead():
    base = dict(spot=SPOT, direction=1, target_1=110.0, invalidation=92.0, vol=VOL, growth=0.0, t=T, expiration="2026-12-18")
    at_spot = stock_outlook(**base)
    cheaper = stock_outlook(**base, entry=95.0)
    assert at_spot["stock_profit_basis"] == "spot" and at_spot["stock_profit_price"] == SPOT
    assert cheaper["stock_profit_basis"] == "entry" and cheaper["stock_profit_price"] == 95.0
    assert cheaper["stock_profit_at_horizon"] > at_spot["stock_profit_at_horizon"] + 0.1
    for k in ("reach_target_1_by_horizon", "beyond_invalidation_at_horizon", "expected_price_at_horizon"):
        assert cheaper[k] == at_spot[k]
    assert cheaper["range_1sd_at_horizon"] == at_spot["range_1sd_at_horizon"]


@pytest.fixture
def client(tmp_path):
    settings = Settings(data_mode="demo", db_path=tmp_path / "t.db")
    svc = TradeAssistant(settings, provider=DemoData(), db=Database(tmp_path / "t.db"))
    with TestClient(create_app(settings, svc)) as c:
        yield c
    svc.db.close()


def test_the_option_picker_takes_the_share_entry_for_that_chance_only(client):
    plain = client.post("/v2/options/evaluate", json={"symbol": "AAPL", "direction": "BULLISH"}).json()
    spot = plain["thesis"]["spot"]
    typed = client.post("/v2/options/evaluate", json={"symbol": "AAPL", "direction": "BULLISH", "entry_price": round(spot * 0.95, 2)}).json()
    a, b = plain["market"], typed["market"]
    assert a["stock_profit_basis"] == "spot" and b["stock_profit_basis"] == "entry"
    assert b["stock_profit_at_horizon"] > a["stock_profit_at_horizon"]
    assert b["reach_target_1_by_horizon"] == pytest.approx(a["reach_target_1_by_horizon"])
    # The contracts do not move with the stock entry: their own mid and implied volatility.
    pa = {(k["expiration"], k["strike"]): k for k in plain["contracts"] if k["usable"]}
    for k in (k for k in typed["contracts"] if k["usable"]):
        old = pa[(k["expiration"], k["strike"])]
        assert (k["premium"], k["at_target"], k["market_implied_profit_at_horizon"]) == pytest.approx(
            (old["premium"], old["at_target"], old["market_implied_profit_at_horizon"]))


# --- 3. the outcome band ---------------------------------------------------------------------------

NOW = datetime(2026, 10, 12, 10, 0, tzinfo=NY)
THESIS = {"symbol": "X", "direction": 1, "spot": 100.0, "target_1": 110.0, "target_2": 115.0, "invalidation": 95.0,
          "horizon": 20, "horizon_date": date(2026, 11, 9), "horizon_calendar_days": 28, "atm_iv": 0.3}
CHAIN = SimpleNamespace(expiration="2026-12-18", forward=100.4, spot=100.0, t=(date(2026, 12, 18) - date(2026, 10, 12)).days / 365)


def _row(**kw):
    return {"symbol": "X261218C00105000", "option_type": "call", "strike": 105.0, "mid": 3.1, "bid": 3.0, "ask": 3.2,
            "iv": 0.3, "delta": 0.4, "open_interest": 500, **kw}


def test_each_contract_carries_its_outcome_band_on_the_horizon_date():
    out = value_contract(_row(), CHAIN, THESIS, NOW, 0.04, 2, None, None)
    p10, p50, p90 = (out[f"implied_pnl_{n}_at_horizon"] for n, _ in PERCENTILE_Z)
    assert all(isinstance(v, float) and math.isfinite(v) for v in (p10, p50, p90))
    assert p10 < p50 < p90                              # a bought call gains with the stock
    assert p10 >= -out["premium"] - 1e-6               # a bought option cannot lose more than it cost
    assert "implied_expected_pnl_at_horizon" in out    # kept


def test_without_a_volatility_there_is_no_band_rather_than_a_zero():
    out = value_contract(_row(iv=None, model_vol=None), CHAIN, THESIS, NOW, 0.04, 1, None, None)
    assert out["usable"] is False
    assert all(out.get(f"implied_pnl_{n}_at_horizon") is None for n, _ in PERCENTILE_Z)


def test_the_market_block_names_the_expiration_its_percentiles_come_from(client):
    d = client.post("/v2/options/evaluate", json={"symbol": "AAPL", "direction": "BEARISH"}).json()
    pc = d["market"]["stock_percentiles_at_horizon"]
    assert pc["expiration"] == d["market"]["atm_expiration"]
    assert pc["p10"] < pc["p50"] < pc["p90"]
    for k in (k for k in d["contracts"] if k["usable"]):
        band = [k[f"implied_pnl_{n}_at_horizon"] for n, _ in PERCENTILE_Z]
        assert all(math.isfinite(v) for v in band)
        assert band[0] > band[2]                        # a bought put gains as the stock falls


# --- 5. past frequency on a Today row --------------------------------------------------------------

def _buckets():
    out = []
    for i, (lo, hi) in enumerate(SCORE_BUCKETS):
        cases = 0 if i == 0 else 40 + i
        rose = int(cases * (0.30 + 0.07 * i))
        out.append({"low": lo, "high": min(hi, 100.0), "cases": cases, "rose": rose,
                    "frequency": rose / cases if cases else None, "frequency_low": 0.2, "frequency_high": 0.7,
                    "independent_periods": 9, "mean_return": 0.01, "mean_excess_vs_spy": 0.0, "by_regime": {}})
    return out


class _Analyst:
    def __init__(self, nets, meta):
        self.nets, self.meta = nets, meta

    def regime(self):
        return {"confirmed": {"regime": "RANGE", "label": "Range"}}

    def trend(self, sym):
        return {"symbol": sym, "history": [], "confirmed": {"date": "2026-10-09", "state": "UP", "label": "Uptrend",
                                                            "net_score": self.nets[sym], "bullish_score": 50,
                                                            "bearish_score": 10, "coverage": 0.9}}

    def reversal(self, sym, history=False):
        return {"confirmed": {"bottom": {"stage": "NONE"}, "top": {"stage": "NONE"}}}

    def history_meta(self):
        return self.meta


def _today(nets, meta):
    svc = SimpleNamespace(settings=SimpleNamespace(watchlist=list(nets)),
                          market=SimpleNamespace(info=SimpleNamespace(mode="tradier")))
    return Today(svc, _Analyst(nets, meta), {"reversal_analysis": True, "signal_calibration": True})


def test_a_today_row_carries_its_score_range_and_a_short_is_not_flipped():
    buckets = _buckets()
    meta = {"run_at": "2026-10-01", "calibration": {str(MAIN_HORIZON): {"buckets": buckets}}}
    d = _today({"L": 40.0, "S": -30.0, "Z": -80.0}, meta).candidates()
    rows = {r["symbol"]: r for r in d["long"] + d["short"]}
    for sym, net in (("L", 40.0), ("S", -30.0)):
        lo, hi = bucket_for(net)
        stored = next(b for b in buckets if b["low"] == lo)
        got = rows[sym]["past_10d"]
        assert (got["low"], got["high"], got["cases"]) == (lo, min(hi, 100.0), stored["cases"])
        assert got["frequency"] == stored["frequency"]          # the share higher, also for the short
        assert got["horizon"] == MAIN_HORIZON
    assert rows["Z"]["past_10d"] is None                         # its bucket has no cases: unknown, not 0%
    assert d["history_built"] is True


def test_no_history_means_no_frequency_on_any_row():
    d = _today({"L": 40.0, "S": -30.0}, None).candidates()
    assert d["history_built"] is False
    assert all(r["past_10d"] is None for r in d["long"] + d["short"])
    assert past_bucket(None, _buckets()) is None and past_bucket(float("nan"), _buckets()) is None
