"""2.4 scanners: technical signals across many stocks and unusual options activity."""

from __future__ import annotations

import math

import numpy as np
import pandas as pd
import pytest
from fastapi.testclient import TestClient

from tradeassist import scanner
from tradeassist.api import create_app
from tradeassist.config import Settings
from tradeassist.db import Database
from tradeassist.features import FEATURE_DEFAULTS
from tradeassist.indicators import enrich
from tradeassist.providers.demo import DemoData
from tradeassist.service import TradeAssistant


# --- universes ------------------------------------------------------------------------------------

def test_universes():
    assert scanner.universe(None, ["aapl", "MSFT", "AAPL"]) == ("watchlist", ["AAPL", "MSFT"])
    name, names = scanner.universe("large_caps", ["AAPL"])
    assert name == "large_caps" and len(names) == len(set(names)) == 100
    assert scanner.universe("sp100", [])[0] == "large_caps"
    assert scanner.universe("watchlist", ["AAPL"], [" ko", "pep ", "", "KO"]) == ("custom", ["KO", "PEP"])
    with pytest.raises(ValueError, match="universe"):
        scanner.universe("everything", [])


# --- readings and signals -------------------------------------------------------------------------

def _frame(**last):
    base = {"date": pd.Timestamp("2026-10-09"), "close": 100.0, "rsi": 50.0, "macd_hist": 0.2, "sma50": 95.0,
            "sma200": 90.0, "pos52": 80.0, "vol_ratio": 1.0, "adx": 20.0, "hv20": 0.2, "bar_complete": True}
    prev = {**base, "date": pd.Timestamp("2026-10-08"), "close": 98.0}
    return pd.DataFrame([prev, {**base, **last}])


def test_readings_on_the_last_bar():
    r = scanner.readings(_frame())
    assert r["date"] == "2026-10-09" and r["close"] == 100 and r["change_pct"] == pytest.approx(100 * (100 / 98 - 1))
    assert (r["rsi_zone"], r["macd"], r["averages"], r["sma50_vs_200"]) == ("NEUTRAL", "ABOVE_SIGNAL", "ABOVE_BOTH", "ABOVE")
    assert scanner.readings(_frame(rsi=30.0))["rsi_zone"] == "OVERSOLD"
    assert scanner.readings(_frame(rsi=71.0))["rsi_zone"] == "OVERBOUGHT"
    assert scanner.readings(_frame(close=80.0))["averages"] == "BELOW_BOTH"
    assert scanner.readings(_frame(close=92.0))["averages"] == "MIXED"
    assert scanner.readings(_frame(macd_hist=-0.1))["macd"] == "BELOW_SIGNAL"


def test_unknown_readings_stay_unknown():
    r = scanner.readings(_frame(rsi=np.nan, sma200=np.nan, macd_hist=np.nan, vol_ratio=np.nan))
    assert r["rsi"] is None and r["rsi_zone"] is None and r["averages"] is None and r["macd"] is None
    assert r["volume_ratio"] is None and r["volume_spike"] is False


def test_a_volume_spike_is_called_on_a_finished_bar_only():
    assert scanner.readings(_frame(vol_ratio=2.5))["volume_spike"] is True
    unfinished = scanner.readings(_frame(vol_ratio=2.5, bar_complete=False))
    assert unfinished["volume_spike"] is False and unfinished["bar_finished"] is False
    assert scanner.readings(_frame(vol_ratio=1.9))["volume_spike"] is False


def _bars(closes, finished=True):
    closes = np.asarray(closes, dtype=float)
    dates = pd.bdate_range("2024-01-02", periods=len(closes))
    df = pd.DataFrame({"date": dates, "open": closes * 0.999, "high": closes * 1.004, "low": closes * 0.996,
                       "close": closes, "volume": np.full(len(closes), 1e6)})
    df["bar_complete"] = True
    if not finished:
        df.loc[df.index[-1], "bar_complete"] = False
    return enrich(df)


def test_technical_row_carries_each_signal_with_its_past_cases():
    rng = np.random.default_rng(3)
    closes = 100 * np.exp(np.cumsum(rng.normal(0, 0.01, 300)))
    closes[-1] = closes[:-1].max() * 1.03                              # a new 52-week high on the last bar
    df = _bars(closes)
    past = {"pattern:new_52w_high": {"cases": 80, "frequency": 0.55, "enough": True}}
    lab = {"new_52w_high": [{"horizon": 10, "verdict": "no_evidence"}]}
    row = scanner.technical_row("TEST", df, 5, past, lab)
    hit = next(s for s in row["signals"] if s["key"] == "new_52w_high")
    assert hit["bars_ago"] == 0 and hit["status"] == "CONFIRMED" and hit["direction"] == 1
    assert hit["signal_key"] == "pattern:new_52w_high" and hit["past"] == past["pattern:new_52w_high"]
    assert hit["lab"] == lab["new_52w_high"]
    assert row["bullish"] == sum(1 for s in row["signals"] if s["direction"] > 0)
    others = [s for s in row["signals"] if s["key"] != "new_52w_high"]
    assert all(s["past"] is None for s in others)                       # no history: nothing made up
    provisional = scanner.technical_row("TEST", _bars(closes, finished=False), 5, {}, {})
    assert next(s for s in provisional["signals"] if s["key"] == "new_52w_high")["status"] == "PROVISIONAL"
    with pytest.raises(ValueError, match="30 bars"):
        scanner.technical_row("TEST", df.tail(20), 5, {}, {})


def _row(symbol, signals=(), rsi_zone="NEUTRAL", volume_ratio=1.0):
    return {"symbol": symbol, "rsi_zone": rsi_zone, "volume_ratio": volume_ratio,
            "signals": [{"key": k, "direction": d, "bars_ago": b} for k, d, b in signals]}


def test_filters_and_order():
    up = _row("UP", [("macd_bull_cross", 1, 0)], volume_ratio=2.4)
    down = _row("DOWN", [("death_cross", -1, 2), ("macd_bear_cross", -1, 3)], rsi_zone="OVERBOUGHT")
    quiet = _row("QUIET", volume_ratio=None)
    rows = [quiet, down, up]
    pick = lambda **kw: [r["symbol"] for r in rows if scanner.matches(r, **kw)]  # noqa: E731
    assert pick() == ["QUIET", "DOWN", "UP"]
    assert pick(direction="bullish") == ["UP"] and pick(direction="bearish") == ["DOWN"]
    assert pick(signal="death_cross") == ["DOWN"] and pick(signal="death_cross", direction="bullish") == []
    assert pick(rsi="overbought") == ["DOWN"] and pick(rsi="oversold") == []
    assert pick(min_volume_ratio=2.0) == ["UP"]                         # unknown volume does not pass
    assert pick(only_signals=True) == ["DOWN", "UP"]
    assert [r["symbol"] for r in sorted(rows, key=scanner.sort_key)] == ["UP", "DOWN", "QUIET"]


# --- options activity -----------------------------------------------------------------------------

class _Chain:
    def __init__(self, expiration, contracts, spot=100.0):
        self.expiration, self.contracts, self.spot = expiration, contracts, spot


def _k(kind, strike, volume, oi, mid=1.0):
    return {"option_type": kind, "strike": strike, "volume": volume, "open_interest": oi, "mid": mid,
            "bid": None if mid is None else mid - 0.05, "ask": None if mid is None else mid + 0.05,
            "moneyness_pct": strike - 100.0, "delta": 0.5 if kind == "call" else -0.5, "iv": 0.3}


def test_flow_summary_counts_calls_against_puts():
    a = _Chain("2026-10-16", [_k("call", 100, 1000, 500, 2.0), _k("call", 110, 100, 5000, 0.5),
                              _k("put", 95, 400, 2000, 1.5), _k("put", 90, 300, 0, None)])
    b = _Chain("2026-10-23", [_k("call", 105, 600, 300, 1.0), _k("put", 100, 200, 1000, 2.5)])
    s = scanner.flow_summary([a, b], min_volume=250, min_vol_oi=1.0)
    assert s["call_volume"] == 1700 and s["put_volume"] == 900
    assert s["call_open_interest"] == 5800 and s["put_open_interest"] == 3000
    assert s["put_call_volume"] == pytest.approx(900 / 1700) and s["put_call_open_interest"] == pytest.approx(3000 / 5800)
    assert s["call_premium"] == pytest.approx((1000 * 2 + 100 * 0.5 + 600 * 1) * 100)
    assert s["put_premium"] == pytest.approx((400 * 1.5 + 200 * 2.5) * 100)
    assert s["unpriced_volume"] == 300                                   # no midpoint: volume counted, premium not
    assert s["turnover"] == pytest.approx(2600 / 8800)
    share = s["call_premium"] / (s["call_premium"] + s["put_premium"])
    assert s["call_premium_share"] == pytest.approx(share) and s["lean"] == "CALL_HEAVY"
    unusual = [(u["expiration"], u["option_type"], u["strike"]) for u in s["unusual"]]
    assert unusual == [("2026-10-16", "call", 100), ("2026-10-23", "call", 105), ("2026-10-16", "put", 90)]
    assert s["unusual"][0]["volume_to_oi"] == 2.0 and s["unusual"][0]["premium"] == 200_000
    assert s["unusual"][2]["volume_to_oi"] is None and "no open interest" in s["unusual"][2]["why"]
    assert [e["put_call_volume"] for e in s["expirations"]] == [pytest.approx(700 / 1100), pytest.approx(200 / 600)]


def test_flow_thresholds_and_leans():
    chain = _Chain("2026-10-16", [_k("call", 100, 300, 250, 1.0), _k("put", 100, 3000, 1000, 1.0)])
    assert scanner.flow_summary([chain])["lean"] == "PUT_HEAVY"
    assert [u["option_type"] for u in scanner.flow_summary([chain], min_vol_oi=2.0)["unusual"]] == ["put"]
    assert scanner.flow_summary([chain], min_volume=5000)["unusual"] == []
    assert scanner.flow_summary([chain], min_premium=100_000)["unusual"][0]["option_type"] == "put"
    even = _Chain("2026-10-16", [_k("call", 100, 1000, 900, 1.0), _k("put", 100, 1000, 900, 1.0)])
    assert scanner.flow_summary([even])["lean"] == "BALANCED"


def test_flow_with_no_volume_reported_is_unknown_not_zero():
    chain = _Chain("2026-10-16", [_k("call", 100, None, None, 1.0), _k("put", 100, None, None, 1.0)])
    s = scanner.flow_summary([chain])
    assert s["call_volume"] is None and s["put_volume"] is None and s["put_call_volume"] is None
    assert s["call_premium"] is None and s["lean"] is None and s["turnover"] is None and s["unusual"] == []


def test_flow_expirations_are_the_nearest_within_reach():
    from datetime import date
    dates = ["2026-10-10", "2026-10-16", "2026-10-23", "2026-11-20", "2027-01-15"]
    assert scanner.flow_expirations(dates, date(2026, 10, 10), 60, 3) == ["2026-10-16", "2026-10-23", "2026-11-20"]
    assert scanner.flow_expirations(dates, date(2026, 10, 10), 10, 3) == ["2026-10-16"]


# --- the API on demo data -------------------------------------------------------------------------

@pytest.fixture()
def app(tmp_path):
    settings = Settings(data_mode="demo", db_path=tmp_path / "t.db")
    svc = TradeAssistant(settings, provider=DemoData(), db=Database(tmp_path / "t.db"))
    yield TestClient(create_app(settings, svc)), svc
    svc.db.close()


def test_technical_scan_endpoint(app):
    c, svc = app
    r = c.get("/v2/scan/technical")
    assert r.status_code == 200, r.text
    d = r.json()
    assert d["universe"] == "watchlist" and d["scanned"] == len(svc.settings.watchlist) and d["errors"] == []
    assert d["history_built"] is False and any("not been built" in n for n in d["notes"])
    assert d["rows"] == sorted(d["rows"], key=scanner.sort_key)
    assert all(s["past"] is None for row in d["rows"] for s in row["signals"])
    bull = c.get("/v2/scan/technical", params={"direction": "bullish"}).json()
    assert all(any(s["direction"] > 0 for s in row["signals"]) for row in bull["rows"]) and bull["matched"] <= d["matched"]
    mine = c.get("/v2/scan/technical", params={"symbols": "ko, pep,NOT-A-TICKER!", "lookback": 1}).json()
    assert {row["symbol"] for row in mine["rows"]} == {"KO", "PEP"} and mine["universe"] == "custom"
    assert len(mine["errors"]) == 1 and mine["lookback"] == 1
    assert c.get("/v2/scan/technical", params={"universe": "moon"}).status_code == 400
    assert c.get("/v2/scan/technical", params={"signal": "telepathy"}).status_code == 400
    many = ",".join(f"A{i}" for i in range(scanner.MAX_TECHNICAL_SYMBOLS + 1))
    assert c.get("/v2/scan/technical", params={"symbols": many}).status_code == 400
    assert "NaN" not in r.text


def test_technical_scan_attaches_past_cases_once_the_history_is_built(app):
    c, svc = app
    svc.analyst.build_history(["AAPL", "KO"], 2)
    d = c.get("/v2/scan/technical", params={"symbols": "AAPL,KO,MSFT", "lookback": 10}).json()
    assert d["history_built"] is True
    sigs = [s for row in d["rows"] for s in row["signals"]]
    assert sigs and all(s["past"] is not None and s["past"]["kind"].startswith("historical frequency") for s in sigs)


def test_options_activity_endpoint(app):
    c, svc = app
    r = c.get("/v2/scan/flow", params={"symbols": "AAPL,MSFT,KO", "expirations": 2})
    assert r.status_code == 200, r.text
    d = r.json()
    assert {row["symbol"] for row in d["rows"]} == {"AAPL", "MSFT", "KO"} and d["errors"] == []
    assert all(len(row["expirations_read"]) == 2 for row in d["rows"])
    assert all(row["put_call_volume"] is None or row["put_call_volume"] > 0 for row in d["rows"])
    premiums = [u["premium"] or 0 for u in d["unusual"]]
    assert premiums == sorted(premiums, reverse=True)
    assert all(u["volume"] >= 250 and (u["volume_to_oi"] is None or u["volume_to_oi"] >= 1) for u in d["unusual"])
    assert any("do not say who bought" in n for n in d["notes"]) and d["notes"][0].startswith("Demo")
    assert c.get("/v2/scan/flow", params={"universe": "large_caps"}).status_code == 400
    assert c.get("/v2/scan/flow", params={"min_vol_oi": -1}).status_code == 400
    assert svc.journal.list() == [] and svc.db.one("SELECT COUNT(*) FROM positions")[0] == 0


def test_the_scanner_can_be_switched_off(tmp_path):
    settings = Settings(data_mode="demo", db_path=tmp_path / "o.db", features={**FEATURE_DEFAULTS, "scanner": False})
    svc = TradeAssistant(settings, provider=DemoData(), db=Database(tmp_path / "o.db"))
    c = TestClient(create_app(settings, svc))
    assert c.get("/v2/scan/technical").status_code == 404
    assert c.get("/v2/scan/flow").json()["error"].endswith("(FEATURE_SCANNER=0 in .env).")
    svc.db.close()


def test_demo_chains_now_and_then_show_volume_over_open_interest():
    demo = DemoData()
    # About one contract in sixty near the money, so across ten stocks there are always some.
    rows = [c for sym in ("AAPL", "MSFT", "KO", "PEP", "JPM", "V", "MA", "HD", "PG", "NVDA")
            for exp in demo.expirations(sym)[:3] for c in demo.chain(sym, exp)]
    over = [c for c in rows if c["open_interest"] and c["volume"] > c["open_interest"]]
    assert 0 < len(over) < len(rows) / 10 and all(math.isfinite(c["volume"]) for c in rows)
