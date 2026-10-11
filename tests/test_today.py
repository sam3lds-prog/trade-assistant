"""2.5 Today: the lean rule and the candidate lists.

Rule (b): a stock leans the way its trend net score points on the last finished bar, unless the market
regime is at the bottom of the fit table for that side, or the opposite reversal is under way (a top
at candidate or confirmed withdraws a long lean, a bottom a short one). Unknown is never a lean.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from tradeassist.api import create_app
from tradeassist.config import Settings
from tradeassist.db import Database
from tradeassist.evaluator import REGIME_FIT_LONG, REGIME_FIT_SHORT
from tradeassist.providers.demo import DemoData
from tradeassist.service import TradeAssistant
from tradeassist.today import HOSTILE, Today, hostile, lean, rank


def test_the_sign_of_the_net_score_sets_the_lean():
    assert lean(42.0, "RANGE", "NONE", "NONE")["lean"] == "LONG"
    assert lean(-17.5, "RANGE", "NONE", "NONE")["lean"] == "SHORT"
    flat = lean(0.0, "RANGE", "NONE", "NONE")
    assert flat == {"lean": None, "measured": True, "pull": None, "capped": None}


def test_an_unmeasured_score_is_unknown_not_no_lean():
    v = lean(None, "BULL_CALM", "NONE", "NONE")
    assert v == {"lean": None, "measured": False, "pull": None, "capped": None}
    out = rank([{"symbol": "X", "net_score": None, **v}])
    assert out["not_measured"] == ["X"] and out["no_lean"]["total"] == 0


@pytest.mark.parametrize("regime", ["BEAR_CALM", "BEAR_VOLATILE"])
def test_a_bear_market_withdraws_long_leans_only(regime):
    v = lean(80.0, regime, "NONE", "NONE")
    assert v["lean"] is None and v["pull"] == "LONG" and "bear market" in v["capped"]
    assert lean(-80.0, regime, "NONE", "NONE")["lean"] == "SHORT"


@pytest.mark.parametrize("regime", ["BULL_CALM", "BULL_VOLATILE"])
def test_a_bull_market_withdraws_short_leans_only(regime):
    v = lean(-80.0, regime, "NONE", "NONE")
    assert v["lean"] is None and v["pull"] == "SHORT" and "bull market" in v["capped"]
    assert lean(80.0, regime, "NONE", "NONE")["lean"] == "LONG"


def test_hostile_is_the_bottom_of_the_evaluator_fit_tables():
    """The cap uses the same tables that score the regime in the stock grade: their two lowest rows."""
    for table, side in ((REGIME_FIT_LONG, "LONG"), (REGIME_FIT_SHORT, "SHORT")):
        worst = sorted(table, key=table.get)[:2]
        assert HOSTILE[side] == set(worst)
        assert all(table[r] < table[x] for r in worst for x in table if x not in worst)
    assert {r for r in REGIME_FIT_LONG if hostile("LONG", r)} == {"BEAR_CALM", "BEAR_VOLATILE"}
    assert {r for r in REGIME_FIT_SHORT if hostile("SHORT", r)} == {"BULL_CALM", "BULL_VOLATILE"}
    assert not hostile("LONG", None) and not hostile("SHORT", "SOMETHING_NEW")


@pytest.mark.parametrize("stage, withdrawn", [("NONE", False), ("WATCH", False), ("FAILED", False),
                                              ("CANDIDATE", True), ("CONFIRMED", True)])
def test_a_top_under_way_withdraws_a_long_lean(stage, withdrawn):
    v = lean(60.0, "TRANSITION", "NONE", stage)
    assert (v["lean"] is None) == withdrawn
    if withdrawn:
        assert v["pull"] == "LONG" and v["capped"].startswith("A top is")


@pytest.mark.parametrize("stage, withdrawn", [("NONE", False), ("WATCH", False), ("CANDIDATE", True), ("CONFIRMED", True)])
def test_a_bottom_under_way_withdraws_a_short_lean(stage, withdrawn):
    v = lean(-60.0, "TRANSITION", stage, "NONE")
    assert (v["lean"] is None) == withdrawn
    if withdrawn:
        assert v["capped"].startswith("A bottom is")


def test_a_reversal_on_the_same_side_does_not_withdraw_the_lean():
    """Rule (b) caps only for the opposite reversal: a bottom under way does not stop a long lean."""
    assert lean(60.0, "TRANSITION", "CONFIRMED", "NONE")["lean"] == "LONG"
    assert lean(-60.0, "TRANSITION", "NONE", "CONFIRMED")["lean"] == "SHORT"


def test_an_unread_reversal_leaves_the_lean_and_is_not_a_cap():
    v = lean(60.0, "TRANSITION", None, None, reversal_read=False)
    assert v["lean"] == "LONG" and v["capped"] is None


def _row(symbol, net, **kw):
    return {"symbol": symbol, "net_score": net, **lean(net, kw.get("regime", "RANGE"), kw.get("bottom", "NONE"), kw.get("top", "NONE"))}


def test_lists_are_sorted_by_the_size_of_the_net_score_and_capped_at_eight():
    rows = [_row(f"L{i}", 10.0 + i) for i in range(11)] + [_row("S1", -5.0), _row("S2", -70.0), _row("Z", 0.0),
                                                           _row("T", 90.0, top="CONFIRMED")]
    out = rank(rows)
    assert [r["symbol"] for r in out["long"]] == ["L10", "L9", "L8", "L7", "L6", "L5", "L4", "L3"]
    assert out["long_total"] == 11 and out["short_total"] == 2
    assert [r["symbol"] for r in out["short"]] == ["S2", "S1"]
    assert out["no_lean"] == {"total": 2, "capped": 1, "balanced": 1, "capped_long": 1, "capped_short": 0,
                              "symbols": [{"symbol": "T", "pull": "LONG", "why": "A top is confirmed."}]}


@pytest.fixture
def client(tmp_path):
    settings = Settings(data_mode="demo", db_path=tmp_path / "t.db", watchlist=["AAPL", "MSFT", "NVDA", "JPM", "KO"])
    svc = TradeAssistant(settings, provider=DemoData(), db=Database(tmp_path / "t.db"))
    with TestClient(create_app(settings, svc)) as c:
        yield c
    svc.db.close()


def test_the_endpoint_returns_both_lists_on_one_universe(client):
    r = client.get("/v2/today/candidates")
    assert r.status_code == 200
    d = r.json()
    assert d["universe_size"] == 5 and d["scanned"] + len(d["errors"]) == 5
    listed = d["long"] + d["short"]
    assert all(x["lean"] == "LONG" for x in d["long"]) and all(x["lean"] == "SHORT" for x in d["short"])
    assert all(x["net_score"] > 0 for x in d["long"]) and all(x["net_score"] < 0 for x in d["short"])
    assert len(listed) + d["no_lean"]["total"] + len(d["not_measured"]) == d["scanned"]
    assert d["regime"]["regime"] in REGIME_FIT_LONG
    assert "not chances" in d["rule"] and d["history_built"] is False
    assert any(n.startswith("Demo") for n in d["notes"])


def test_the_universe_is_the_watchlist_or_the_large_company_list(client):
    assert client.get("/v2/today/candidates?universe=sp500").status_code == 400
    # Tickers of one's own choosing are not a Today universe: the list stays the watchlist.
    d = client.get("/v2/today/candidates?symbols=ZZZZ,FOO").json()
    assert d["universe"] == "watchlist" and d["universe_size"] == 5


class _Stub:
    """An analyst whose readings are set by the test."""
    def __init__(self, trends, reversals=None, regime="RANGE"):
        self.trends, self.reversals, self.reg = trends, reversals or {}, regime

    def regime(self):
        return {"confirmed": {"regime": self.reg, "label": self.reg}}

    def trend(self, sym):
        return self.trends[sym]

    def reversal(self, sym, history=False):
        if sym not in self.reversals:
            raise ValueError("no reversal reading")
        return {"confirmed": {"bottom": {"stage": self.reversals[sym][0]}, "top": {"stage": self.reversals[sym][1]}}}

    def history_meta(self):
        return None


class _Svc:
    def __init__(self, names):
        from types import SimpleNamespace
        self.settings = SimpleNamespace(watchlist=names)
        self.market = SimpleNamespace(info=SimpleNamespace(mode="tradier"))


def _trend(sym, net):
    day = None if net == "no bar" else {"date": "2026-10-09", "state": "UP", "label": "Uptrend", "net_score": net,
                                          "bullish_score": 50, "bearish_score": 10, "coverage": 0.9}
    return {"symbol": sym, "confirmed": day, "history": []}


def test_a_stock_without_a_finished_bar_is_reported_not_crashed():
    t = Today(_Svc(["A", "B", "C"]), _Stub({"A": _trend("A", 40.0), "B": _trend("B", "no bar"), "C": _trend("C", None)},
                                         {"A": ("NONE", "NONE")}), {"reversal_analysis": True})
    d = t.candidates()
    assert [r["symbol"] for r in d["long"]] == ["A"]
    assert d["errors"] == [{"symbol": "B", "error": "no finished bar to read the trend on"}]
    assert d["not_measured"] == ["C"] and d["no_lean"]["total"] == 0


def test_a_lean_kept_without_its_reversal_reading_says_so_on_its_row():
    t = Today(_Svc(["A", "B"]), _Stub({"A": _trend("A", 40.0), "B": _trend("B", 30.0)}, {"B": ("NONE", "NONE")}),
              {"reversal_analysis": True})
    d = t.candidates()
    rows = {r["symbol"]: r for r in d["long"]}
    assert rows["A"]["reversal_unread"] is True and rows["B"]["reversal_unread"] is False
    assert any("reversal reading failed for A" in n for n in d["notes"])


def test_today_has_its_own_flag(tmp_path, monkeypatch):
    monkeypatch.setenv("FEATURE_TODAY", "0")
    from tradeassist.features import load_features
    settings = Settings(data_mode="demo", db_path=tmp_path / "t.db", features=load_features())
    svc = TradeAssistant(settings, provider=DemoData(), db=Database(tmp_path / "t.db"))
    with TestClient(create_app(settings, svc)) as c:
        assert c.get("/v2/today/candidates").status_code == 404
        assert c.get("/v2/regime").status_code == 200          # Decide's own path stays on
        assert c.get("/api/status").json()["features"]["today"] is False
    svc.db.close()
