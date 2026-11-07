"""Regression baselines for the analytics 3.0 builds on (Phase 1 entry condition).

The figures below were recorded on the 3.0 starting point, on the demo market at a fixed moment, after the two
P0 corrections (P&L percentiles of the P&L itself; theoretical and modeled losses apart). A later change that moves
any of them fails here and has to say why: re-record with REGENERATE_BASELINE=1 only for an intended change, and
name it in docs/IMPROVEMENTS.md.
"""

import json
import math
import os
from datetime import datetime
from pathlib import Path

import numpy as np
import pytest
import time_machine

from tradeassist import factors as F
from tradeassist import similar
from tradeassist.analysis import Analyst
from tradeassist.blend import Blender
from tradeassist.chartview import ChartView
from tradeassist.config import Settings
from tradeassist.db import Database
from tradeassist.opportunities import cost_pct, edge_from, make
from tradeassist.optionmath import NY, black76_greeks, black76_price, implied_vol
from tradeassist.providers.demo import DemoData
from tradeassist.service import TradeAssistant
from tradeassist.whatif import WhatIf

BASELINE = Path(__file__).parent / "fixtures" / "regression_baseline.json"
MOMENT = datetime(2026, 10, 22, 17, 0, tzinfo=NY)


def figures(tmp_path) -> dict:
    out: dict = {}
    with time_machine.travel(MOMENT, tick=False):
        # Option maths, pure.
        out["black76_call"] = black76_price(105.0, 100.0, 0.25, 0.04, 0.3, "CALL")
        out["black76_put"] = black76_price(105.0, 100.0, 0.25, 0.04, 0.3, "PUT")
        g = black76_greeks(104.0, 105.0, 100.0, 0.25, 0.04, 0.3, "CALL")
        out["black76_delta"], out["black76_vega"] = g.delta, g.vega
        out["implied_vol"] = implied_vol(out["black76_call"], 105.0, 100.0, 0.25, 0.04, "CALL")

        s = Settings(data_mode="demo", db_path=tmp_path / "t.db", account_size=50_000)
        svc = TradeAssistant(s, provider=DemoData(live=True), db=Database(tmp_path / "t.db"))
        # Similar patterns on the demo's own history.
        frame = svc.market.bars("AAPL", years=2, complete=True).reset_index(drop=True)
        sim = similar.search(frame, len(frame) - 30, len(frame) - 1, "1d")
        out["similar_matches"] = len(sim["matches"])
        out["similar_best_corr"] = sim["best_correlation"]
        h5 = next(r for r in sim["horizons"] if r["bars"] == 5)
        out["similar_h5_median"] = h5["median_pct"]

        # What-if: positions and a combination under one scenario.
        w = WhatIf(svc, ChartView(svc, Analyst(svc)), F.Factors(svc, F.Stooq(enabled=False)))
        for kind, qty in (("shares", 100), ("long_call", 1), ("long_put", 1), ("bull_call_spread", 1), ("long_straddle", 1),
                          ("iron_condor", 1)):
            r = w._public(w.position("AAPL", kind=kind, quantity=qty, expected_pct=2.0, sd_pct=4.0, horizon_bars=5, group="1d"))
            out[f"whatif_{kind}"] = {k: r.get(k) for k in ("expected_pnl", "pnl_p10", "pnl_p50", "pnl_p90", "p_profit",
                                                          "theoretical_max_loss", "modeled_worst_loss", "expected_shortfall",
                                                          "capital_required", "cost")}
        c = w.combo("AAPL", legs=[{"kind": "shares", "quantity": 100}, {"kind": "long_put", "quantity": 1}], expected_pct=2.0,
                    sd_pct=4.0, horizon_bars=5, group="1d")["combined"]
        out["whatif_combo"] = {k: c[k] for k in ("expected_pnl", "pnl_p10", "pnl_p50", "pnl_p90", "theoretical_max_loss",
                                                  "modeled_worst_loss", "expected_shortfall", "capital_required")}

        # The blend: two opportunities, seeded paths.
        def opp(sym, direction):
            px = svc.market.spot(sym)
            atr, d = px * 0.02, (1 if direction == "LONG" else -1)
            return make(source="morning", key=f"m:{sym}:{direction}", symbol=sym, direction=direction, setup="ride",
                        setup_label="Ride", horizon="day", horizon_days=1, entry=px, stop=px - d * 0.6 * atr,
                        target_1=px + d * 0.9 * atr, target_2=px + d * 1.5 * atr, atr=atr, score=50.0,
                        edge=edge_from(0.5, 400, basis="baseline", cost_pct=cost_pct(svc.settings)))
        b = Blender(svc).run([opp("AAPL", "LONG"), opp("XOM", "SHORT")], budget=20_000, goal="balanced", obey_limits=False)
        out["blend"] = {"spent": sum(r.get("dollars") or 0 for r in b["rows"]),
                        **{k: b["combined"][k] for k in ("expected_pnl", "pnl_p10", "pnl_p50", "pnl_p90", "cvar10")}}
        svc.db.close()
    return out


def flatten(d, prefix=""):
    for k, v in d.items():
        if isinstance(v, dict):
            yield from flatten(v, f"{prefix}{k}.")
        else:
            yield f"{prefix}{k}", v


def test_the_analytics_match_the_recorded_baseline(tmp_path):
    got = figures(tmp_path)
    if os.environ.get("REGENERATE_BASELINE") == "1" or not BASELINE.exists():
        BASELINE.parent.mkdir(parents=True, exist_ok=True)
        BASELINE.write_text(json.dumps(got, indent=1, sort_keys=True, default=float) + "\n")
        pytest.skip(f"baseline written to {BASELINE.name}")
    want = dict(flatten(json.loads(BASELINE.read_text())))
    have = dict(flatten(json.loads(json.dumps(got, default=float))))
    assert set(have) == set(want)
    moved = []
    for key, w in want.items():
        h = have[key]
        if w is None or h is None or isinstance(w, (bool, str)):
            if h != w:
                moved.append((key, w, h))
        elif not math.isclose(float(h), float(w), rel_tol=1e-6, abs_tol=1e-6):
            moved.append((key, w, h))
    assert moved == [], "figures moved from the recorded baseline: " + "; ".join(f"{k}: {w} -> {h}" for k, w, h in moved)
    assert np.isfinite([v for v in have.values() if isinstance(v, float)]).all()
