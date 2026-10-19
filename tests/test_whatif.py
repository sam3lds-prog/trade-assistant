"""What-if analysis (2.13): the factors and their betas, the model's arithmetic, stooq readings, the AI
scenario (with a fake ChatGPT and a fake Claude, sharing one daily limit), the position and its ranking,
the API, and the page's pure helpers under Node."""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pandas as pd
import pytest
from fastapi.testclient import TestClient

from tradeassist import factors as F
from tradeassist.ai import AIClient, AIError
from tradeassist.analysis import Analyst
from tradeassist.api import create_app
from tradeassist.chartview import ChartView, RequestError
from tradeassist.chatgpt import ChatGPT
from tradeassist.config import Settings
from tradeassist.db import Database
from tradeassist.news import NewsClient
from tradeassist.options.lab import OptionsLab
from tradeassist.providers.demo import DemoData
from tradeassist.service import TradeAssistant
from tradeassist.whatif import STRATEGY_KINDS, WhatIf, analyst_calls, horizon_label, scenario_distribution

STATIC = Path(__file__).resolve().parents[1] / "tradeassist" / "static"


def service(tmp_path, **kw):
    s = Settings(data_mode="demo", db_path=tmp_path / "t.db", **kw)
    svc = TradeAssistant(s, provider=DemoData(), db=Database(tmp_path / "t.db"))
    svc.news = NewsClient(s, svc.db, None)
    svc.lab = OptionsLab(svc)
    svc.ai = AIClient(s, svc.db)
    return svc


def whatif(svc, chatgpt=None, stooq=None):
    return WhatIf(svc, ChartView(svc, Analyst(svc)), F.Factors(svc, stooq or F.Stooq(enabled=False)), chatgpt=chatgpt)


# --- the factors ------------------------------------------------------------------------------------------------------

def frame(closes, start="2025-01-02"):
    dates = pd.bdate_range(start, periods=len(closes))
    c = np.asarray(closes, dtype=float)
    return pd.DataFrame({"date": dates, "open": c, "high": c * 1.01, "low": c * 0.99, "close": c, "volume": 1_000_000})


def test_the_beta_is_the_stocks_move_per_percent_of_the_proxy():
    rng = np.random.default_rng(3)
    rp = rng.normal(0, 0.01, 300)
    rs = 1.5 * rp + rng.normal(0, 0.003, 300)
    proxy, stock = frame(100 * np.exp(np.cumsum(rp))), frame(50 * np.exp(np.cumsum(rs)))
    fit = F.beta_of(stock, proxy)
    assert abs(fit["beta"] - 1.5) < 0.1 and fit["correlation"] > 0.9 and fit["days"] == 250
    assert F.beta_of(stock.head(30), proxy) is None                              # too few days
    assert F.beta_of(stock, frame([100.0] * 300)) is None                        # a proxy that never moves


def test_the_joint_fit_counts_each_factors_own_part_only():
    """A sector that is mostly the market, and a stock that follows both: the one-at-a-time betas double count,
    the joint fit does not."""
    rng = np.random.default_rng(7)
    m = rng.normal(0, 0.01, 400)
    e = rng.normal(0, 0.006, 400)
    sector = 0.8 * m + e
    stock = 1.0 * m + 0.5 * e + rng.normal(0, 0.002, 400)
    fit = F.joint_fit(stock, {"us_market": m, "sector": sector}, ["us_market", "sector"])
    assert fit["us_market"]["beta_unique"] == pytest.approx(1.0, abs=0.05)
    assert fit["sector"]["beta_unique"] == pytest.approx(0.5, abs=0.08)
    assert fit["sector"]["loadings"]["us_market"] == pytest.approx(0.8, abs=0.05) and 0.5 < fit["sector"]["explained"] < 0.8
    # A scenario where the sector moves exactly as the market implies: the sector's own part is nil.
    u = F.unique_moves(fit, {"us_market": 2.0, "sector": 1.6}, ["us_market", "sector"])
    assert u["us_market"] == 2.0 and u["sector"] == pytest.approx(0.0, abs=0.1)
    rows = [{"key": "us_market", "label": "M", "unit": "%", "beta": 1.0, "on": True, "scenario": 2.0},
            {"key": "sector", "label": "S", "unit": "%", "beta": 0.9, "on": True, "scenario": 1.6}]
    terms = F.factor_terms(rows, {}, {"stock": stock, "proxies": {"us_market": m, "sector": sector}, "days": 400})
    total = sum(t["contribution_pct"] for t in terms)
    assert total == pytest.approx(2.0, abs=0.15)                                  # not 2.0 + 0.9 x 1.6
    assert terms[1]["shared_pct"] == pytest.approx(1.6, abs=0.1)
    # With the market off, the sector carries its whole move (the fit is of the factors that are on).
    rows[0]["on"] = False
    alone = F.factor_terms(rows, {}, {"stock": stock, "proxies": {"us_market": m, "sector": sector}, "days": 400})
    assert [t["key"] for t in alone] == ["sector"] and alone[0]["unique_move_pct"] == 1.6
    assert F.factor_terms(rows, {}, None) == []
    # The returns are aligned on shared days (closes joined first, then differenced); a proxy whose history
    # would cut the shared sample short is left out and named, rather than shrinking the fit for every factor.
    r = F.aligned_returns(frame(100 * np.exp(np.cumsum(m))), {"a": frame(50 * np.exp(np.cumsum(sector))), "b": frame([1.0] * 10),
                                                              "c": frame(50 * np.exp(np.cumsum(sector[:100])))})
    assert r["days"] == 250 and set(r["proxies"]) == {"a"} and len(r["stock"]) == 250 and r["dropped"] == {"b": 9, "c": 99}
    gap = frame(50 * np.exp(np.cumsum(sector))).drop(index=[200])                     # a missing day pairs no two-day move
    r2 = F.aligned_returns(frame(100 * np.exp(np.cumsum(m))), {"a": gap})
    assert r2["days"] == 250 and abs(r2["proxies"]["a"]).max() < 0.1
    # A factor the ones above it almost fully explain (a peers basket that tracks the market) has no own part.
    twin = m + rng.normal(0, 0.0001, 400)
    fit3 = F.joint_fit(stock, {"us_market": m, "peers": twin, "sector": sector}, ["us_market", "peers", "sector"])
    assert fit3["peers"]["redundant"] and fit3["peers"]["beta_unique"] == 0.0 and fit3["peers"]["explained"] > 0.99
    assert fit3["sector"]["beta_unique"] == pytest.approx(0.5, abs=0.08) and "peers" not in fit3["sector"]["loadings"]
    assert F.unique_moves(fit3, {"us_market": 1.0, "peers": 1.1, "sector": 0.8}, ["us_market", "peers", "sector"])["peers"] == 0.0


def test_the_model_adds_the_shocks_the_pattern_and_the_news_and_widens_for_earnings():
    shocks = [{"key": "a", "label": "A", "value": 1.5, "unit": "%", "contribution_pct": 3.0},
              {"key": "y", "label": "Y", "value": 10.0, "unit": "bp", "contribution_pct": -0.32}]
    m = F.model_path(horizon=4, shocks=shocks, pattern=[1.0, 2.0], pattern_weight=0.5,
                     daily_vol=0.01, atr_pct=2.0, news_term_pct=0.6, analyst_net=0.0, earnings_in=None, event_move_pct=None, news_tone=0.5)
    # Shocks land on the first bar.
    assert abs(m["path_pct"][0] - (3 - 0.32 + 0.5 * 1.0 + 0.6 / 3)) < 1e-9
    # The pattern runs out after two bars and holds; the news term is spread over three bars.
    assert abs(m["path_pct"][3] - (3 - 0.32 + 0.5 * 2.0 + 0.6)) < 1e-9
    assert [t["key"] for t in m["terms"]] == ["a", "y", "pattern", "news"] and m["terms"][-1]["value"] == 0.5
    assert m["sd_pct"][0] == pytest.approx(1.0) and m["sd_pct"][3] == pytest.approx(2.0)
    wide = F.model_path(horizon=4, shocks=shocks, pattern=None, pattern_weight=0.0, daily_vol=0.01, atr_pct=2.0,
                        news_term_pct=0.0, analyst_net=1.0, earnings_in=2, event_move_pct=5.0)
    assert wide["sd_pct"][0] == pytest.approx(1.0) and wide["sd_pct"][1] == pytest.approx(np.sqrt(2 + 25))
    assert wide["terms"][-1]["key"] == "analysts" and wide["path_pct"][-1] == pytest.approx(3.0 - 0.32 + 2.0)   # one range x 1


def test_stooq_quotes_are_read_from_its_csv_and_fall_back_to_the_funds_when_it_fails():
    text = "Symbol,Date,Time,Open,High,Low,Close,Volume\n^NKX,2026-10-19,15:00:00,40000,40500,39800,40400,0\n" \
           "10USY.B,2026-10-19,09:00:00,4.20,4.25,4.18,4.30,0\nCB.F,2026-10-19,09:00:00,N/D,N/D,N/D,N/D,0\n"
    rows = F.parse_stooq(text)
    assert rows["^nkx"]["close"] == 40400 and rows["10usy.b"]["open"] == 4.2 and "cb.f" not in rows

    class Session:
        def __init__(self, status=200, body=text):
            self.status, self.body, self.calls = status, body, 0

        def get(self, url, **kw):
            self.calls += 1
            assert url.startswith("https://stooq.com/") and kw["params"]["e"] == "csv"
            return SimpleNamespace(status_code=self.status, text=self.body)
    ok = F.Stooq(Session())
    got = ok.quotes(["^nkx", "10usy.b", "cb.f"])
    assert set(got) == {"^nkx", "10usy.b"} and ok.last_error is None
    ok.quotes(["^nkx"])
    ok.quotes(["cb.f", "^nkx"])
    assert ok.session.calls == 1                                                  # cached by the set asked, N/D included
    ok.quotes(["^dax"])
    assert ok.session.calls == 2                                                  # a symbol not asked before
    empty = F.Stooq(Session(body="<html>daily hits limit</html>"))
    assert empty.quotes(["^nkx"]) == {} and "no quotes" in empty.last_error
    bad = F.Stooq(Session(status=503))
    assert bad.quotes(["^nkx"]) == {} and "503" in bad.last_error
    bad.quotes(["^nkx"])
    assert bad.session.calls == 1                                                 # not retried for a while
    assert F.Stooq(Session(), enabled=False).quotes(["^nkx"]) == {}


def test_readings_use_the_real_index_when_stooq_answers_and_the_fund_otherwise(tmp_path):
    svc = service(tmp_path)
    text = "Symbol,Date,Time,Open,High,Low,Close,Volume\n^NKX,2026-10-19,15:00:00,40000,40500,39800,40400,0\n" \
           "10USY.B,2026-10-19,09:00:00,4.20,4.25,4.18,4.30,0\n"
    session = SimpleNamespace(get=lambda url, **kw: SimpleNamespace(status_code=200, text=text))
    rows = F.Factors(svc, F.Stooq(session)).readings("AAPL", peers=["TSM", "aapl"])
    by = {r["key"]: r for r in rows}
    assert by["overseas_japan"]["reading"] == pytest.approx(1.0) and "stooq ^nkx" in by["overseas_japan"]["reading_source"]
    assert by["yield_10y"]["reading"] == pytest.approx(10.0) and by["yield_10y"]["unit"] == "bp"       # 4.20 -> 4.30 = 10 bp
    assert by["overseas_europe"]["reading"] is not None and "VGK" in by["overseas_europe"]["reading_source"]
    assert by["volatility"]["reading"] == pytest.approx(by["volatility"]["proxy_move_pct"] / 0.5)   # VIXY moves half the index
    assert by["yield_10y"]["proxy"] == "IEF" and by["yield_10y"]["scale"] == 0.075
    assert by["sector"]["proxy"] == "XLK" and "XLK" in by["sector"]["label"]
    assert by["peers"]["peers"] == ["TSM"] and "TSM" in by["peers"]["reading_source"]   # the stock itself is not a peer
    assert all(isinstance(r["scenario"], float) for r in rows)
    F.Factors(svc, F.Stooq(enabled=False)).betas("AAPL", rows)
    assert all(r["beta"] is not None and r["beta_days"] >= 60 for r in rows if r["key"] != "peers" or r["peers"])


def test_demo_data_never_asks_stooq(tmp_path):
    svc = service(tmp_path)
    assert F.Factors(svc).stooq.enabled is False


def test_analyst_calls_are_counted_once_per_headline():
    items = [{"headline": "Broker upgrades AAPL to buy"}, {"headline": "Broker upgrades AAPL to buy"},
             {"headline": "Bank cuts price target on AAPL"}, {"headline": "AAPL opens a store"}, {"headline": ""}]
    calls = analyst_calls(items)
    assert (calls["upgrades"], calls["downgrades"], calls["net"]) == (1, 1, 0)
    assert [c["call"] for c in calls["items"]] == ["upgrade", "downgrade"]


# --- the setup and the model ------------------------------------------------------------------------------------------

def test_the_setup_reads_every_factor_and_models_the_path_in_prices(tmp_path, monkeypatch):
    svc = service(tmp_path)
    w = whatif(svc)
    monkeypatch.setattr(WhatIf, "_premarket", staticmethod(lambda: True))
    s = w.setup("AAPL", peers=["TSM"])
    assert s["group"] == "1d" and s["horizon"] == 5 and s["horizon_label"] == "1 week"
    assert [h["bars"] for h in s["horizons"]] == [1, 3, 5, 10, 20]
    keys = [r["key"] for r in s["factors"]]
    assert keys == [f["key"] for f in F.FACTORS] and keys[0] == "us_market" and all(r["beta"] is not None for r in s["factors"])
    assert all(isinstance(r["beta_unique"], float) and 0 <= r["explained"] <= 1 for r in s["factors"])
    assert s["factors"][0]["beta_unique"] == pytest.approx(s["factors"][0]["beta"])        # the first factor has nothing above it
    assert all("unique_move_pct" in t for t in s["model"]["terms"] if t["key"] in keys)
    assert all(r["in_fit"] and r["fit_days"] >= 60 and r["in_model"] and isinstance(r["beta_unique_now"], float) for r in s["factors"])
    assert s["scenario"]["us_market"] == s["factors"][0]["reading"]              # before the open: the readings
    m = s["model"]
    assert len(m["path_pct"]) == len(m["times"]) == 5 and len(m["path"]) == 5
    assert m["path"][-1] == pytest.approx(s["price"] * (1 + m["expected_pct"] / 100))
    assert all(lo < p < hi for lo, p, hi in zip(m["low"], m["path"], m["high"]))
    assert s["news"]["items"] and 0 < s["earnings"]["trading_days"] <= s["earnings"]["days"] and s["volatility"]["daily_vol"] > 0
    assert s["pattern"]["available"] is False and "Find similar patterns" in s["pattern"]["note"]   # no window chosen yet
    assert [p["key"] for p in s["providers"]] == ["chatgpt", "claude"] and not s["providers"][0]["configured"]
    assert set(s["strategies"]) == set(STRATEGY_KINDS)
    assert "no order is placed" in s["note"]
    # Once the stock is trading, the readings are already priced: the scenario starts at no further move.
    monkeypatch.setattr(WhatIf, "_premarket", staticmethod(lambda: False))
    s2 = w.setup("AAPL", peers=["TSM"], refresh=True)
    assert s2["scenario"]["us_market"] == 0.0 and s2["factors"][0]["reading"] == s["factors"][0]["reading"]
    assert s2["premarket"] is False
    # The setup is cached for a while; the cached copy is not shared with callers.
    again = w.setup("AAPL", peers=["TSM"])
    again["factors"][0]["beta"] = -99
    assert w.setup("AAPL", peers=["TSM"])["factors"][0]["beta"] != -99


def test_the_model_moves_by_beta_times_the_change_and_drops_excluded_factors(tmp_path):
    svc = service(tmp_path)
    w = whatif(svc)
    base = w.model("AAPL", scenario={}, include={}, horizon=5)
    beta = next(r["beta"] for r in w.setup("AAPL")["factors"] if r["key"] == "us_market")
    up = w.model("AAPL", scenario={"us_market": base["scenario"]["us_market"] + 2.0}, include={}, horizon=5)
    # The market is the first factor, so its own part is its whole move: the change is beta x 2 exactly, and the
    # other factors' terms move the other way by what the market now explains of them (no double counting).
    terms_up = {t["key"]: t for t in up["model"]["terms"]}
    assert terms_up["us_market"]["contribution_pct"] - {t["key"]: t for t in base["model"]["terms"]}["us_market"]["contribution_pct"] == pytest.approx(2.0 * beta, abs=1e-6)
    assert terms_up["sector"]["shared_pct"] != {t["key"]: t for t in base["model"]["terms"]}["sector"]["shared_pct"]
    off = w.model("AAPL", scenario={"us_market": 50.0}, include={"us_market": False}, horizon=5)
    assert "us_market" not in [t["key"] for t in off["model"]["terms"]]
    # With the market off, the fit is of the factors that remain: the sector is first and carries its whole move.
    sector_off = next(t for t in off["model"]["terms"] if t["key"] == "sector")
    assert sector_off["shared_pct"] == 0.0 and sector_off["unique_move_pct"] == sector_off["proxy_move_pct"]
    assert w.model("AAPL", scenario={}, include={}, horizon=10)["model"]["horizon"] == 10
    with pytest.raises(RequestError):
        w.setup("AAPL", horizon=99)


def test_horizons_follow_the_bar_size():
    assert horizon_label(5, "1d") == "1 week" and horizon_label(7, "1h") == "1 day" and horizon_label(2, "1w") == "2 weeks"
    prices, w = scenario_distribution(100.0, 10.0, 5.0)
    assert abs(w.sum() - 1) < 1e-9 and abs(float((prices * w).sum()) - 110) < 1.0


# --- the AI's scenario ------------------------------------------------------------------------------------------------

class FakeGPT:
    """A ChatGPT that answers with whatever JSON it is given, and remembers the prompt."""

    def __init__(self, answer):
        self.answer, self.prompts, self.configured = answer, [], True

    def status(self):
        return {"configured": True, "reason": None, "model": "fake-gpt"}

    def ask_json(self, *, purpose, prompt, system, max_tokens):
        self.prompts.append(prompt)
        return {"json": self.answer, "text": json.dumps(self.answer), "usage": {"input_tokens": 10, "output_tokens": 5},
                "model": "fake-gpt", "provider": "openai"}


def test_the_ai_is_sent_every_input_and_its_answer_is_read_and_checked(tmp_path):
    svc = service(tmp_path)
    answer = {"path_pct": [1, 2, 3], "low_pct": [0, 0, 0], "high_pct": [2, 5, 9999], "expected_pct": 3, "confidence": 0.7,
              "variables": {"us_market": 1.5, "news": 0.5, "bogus": 3, "pattern_weight": 0.8}, "include": {"oil_brent": False},
              "pattern": {"use": True, "weight": 0.9, "why": "ten cases"},
              "strategy": {"kind": "bull_call_spread", "direction": "up", "why": "capped risk", "combination": ["shares", "moon", "bull_call_spread", 3]},
              "rationale": "Because.", "risks": ["earnings", 42, {"not": "a string"}]}
    gpt = FakeGPT(answer)
    w = whatif(svc, chatgpt=gpt)
    out = w.ask_ai("AAPL", provider="chatgpt", horizon=5, peers=["TSM"])
    prompt = gpt.prompts[0]
    for word in ("FACTORS", "us_market", "NEWS", "ANALYST CALLS", "EARNINGS", "VOLATILITY", "SIMILAR PATTERNS", "THE APP'S MODEL",
                 "STRATEGIES available", "demo data, made up", "Foreign peers"):
        assert word in prompt, word
    sc = out["scenario"]
    assert out["provider"] == "chatgpt" and out["model"] == "fake-gpt" and out["usage"]["input_tokens"] == 10
    assert sc["path_pct"] == [1, 2, 3, 3, 3] and sc["expected_pct"] == 3                     # padded to the horizon
    assert sc["high_pct"][2] == 100 and sc["low_pct"] == [0, 0, 0, 0, 0] and len(sc["sd_pct"]) == 5   # clipped
    assert sc["variables"] == {"us_market": 1.5, "news": 0.5, "pattern_weight": 0.9}        # unknown keys dropped, pattern weight wins
    assert sc["include"] == {"oil_brent": False, "pattern": True}
    assert sc["strategy"]["kind"] == "bull_call_spread" and sc["risks"] == ["earnings", "42"]
    assert sc["strategy"]["combination"] == ["bull_call_spread", "shares"]                  # the kind first; unknowns dropped
    assert len(sc["path"]) == 5 and len(sc["times"]) == 5 and sc["path"][-1] == pytest.approx(sc["last"] * 1.03)
    # An unusable strategy falls back to one that fits the path's direction; a non-list path is replaced by the model's.
    gpt.answer = {"path_pct": "no", "strategy": {"kind": "moon"}}
    sc2 = w.ask_ai("AAPL", provider="chatgpt", horizon=5)["scenario"]
    assert sc2["strategy"]["kind"] in ("shares", "long_put") and len(sc2["path_pct"]) == 5
    with pytest.raises(RequestError):
        w.ask_ai("AAPL", provider="gemini", horizon=5)
    with pytest.raises(AIError, match="Claude is not set up"):
        w.ask_ai("AAPL", provider="claude", horizon=5)


def test_claude_answers_the_same_question_through_the_existing_client(tmp_path):
    svc = service(tmp_path)
    asked = {}

    def ask(**kw):
        asked.update(kw)
        return {"text": "Here you go:\n```json\n{\"path_pct\": [2], \"expected_pct\": 2, \"strategy\": {\"kind\": \"long_call\"}}\n```",
                "model": "claude-x", "usage": {"input_tokens": 1, "output_tokens": 1}}
    svc.ai = SimpleNamespace(ask=ask, configured=True, status=lambda: {"configured": True, "reason": None, "model": "claude-x"})
    out = whatif(svc).ask_ai("AAPL", provider="claude", horizon=3)
    assert asked["purpose"] == "what_if" and asked["web_search"] is False and "FACTORS" in asked["prompt"]
    assert out["label"] == "Claude" and out["scenario"]["path_pct"] == [2, 2, 2] and out["scenario"]["strategy"]["kind"] == "long_call"
    assert out["scenario"]["strategy"]["combination"] == ["long_call"]


def test_chatgpt_requests_share_the_daily_limit_and_never_send_the_key_anywhere_else(tmp_path):
    s = Settings(data_mode="demo", db_path=tmp_path / "t.db", anthropic_api_key="sk-ant-secret", openai_api_key="sk-openai-secret",
                 ai_daily_requests=2)
    db = Database(tmp_path / "t.db")
    ai = AIClient(s, db)
    posted = []

    class Session:
        def post(self, url, headers, json, timeout):
            posted.append((url, headers, json))
            return SimpleNamespace(status_code=200, json=lambda: {"choices": [{"message": {"content": "{\"a\": 1}"}, "finish_reason": "stop"}],
                                                                  "usage": {"prompt_tokens": 7, "completion_tokens": 3}})
    gpt = ChatGPT(s, ai, session=Session())
    assert gpt.configured and gpt.status()["provider"].startswith("OpenAI")
    out = gpt.ask_json(purpose="what_if", prompt="hi", system="sys")
    assert out["json"] == {"a": 1} and out["usage"] == {"input_tokens": 7, "output_tokens": 3, "web_searches": 0}
    url, headers, body = posted[0]
    assert url == "https://api.openai.com/v1/chat/completions" and headers["Authorization"] == "Bearer sk-openai-secret"
    assert body["response_format"] == {"type": "json_object"} and body["model"] == "gpt-4.1-mini" and "temperature" not in body
    used = ai.used_today()
    assert used["requests"] == 1 and used["input_tokens"] == 7
    row = db.one("SELECT model, purpose, ok FROM ai_usage")
    assert (row["model"], row["purpose"], row["ok"]) == ("gpt-4.1-mini", "what_if", 1)
    gpt.ask_json(purpose="what_if", prompt="hi", system="sys")
    with pytest.raises(AIError, match="limit of 2"):
        ai.ask(purpose="brief", prompt="x")                                      # Claude's turn is counted too
    with pytest.raises(AIError, match="limit of 2"):
        gpt.ask_json(purpose="what_if", prompt="hi", system="sys")

    class Refuses:
        def post(self, url, headers, json, timeout):
            return SimpleNamespace(status_code=401, json=lambda: {"error": {"message": "bad key sk-openai-secret"}})
    s2 = Settings(data_mode="demo", db_path=tmp_path / "u.db", openai_api_key="sk-openai-secret", ai_daily_requests=5)
    gpt2 = ChatGPT(s2, AIClient(s2, Database(tmp_path / "u.db")), session=Refuses())
    with pytest.raises(AIError) as exc:
        gpt2.ask_json(purpose="what_if", prompt="hi", system="sys")
    assert "refused the key" in str(exc.value) and "secret" not in str(exc.value)
    assert "secret" not in json.dumps(gpt2.status())
    s3 = Settings(data_mode="demo", db_path=tmp_path / "v.db")
    assert not ChatGPT(s3, AIClient(s3, Database(tmp_path / "v.db"))).configured


# --- the position -----------------------------------------------------------------------------------------------------

def test_shares_and_shorts_are_valued_at_the_scenarios_percentiles(tmp_path):
    w = whatif(service(tmp_path))
    r = w.position("AAPL", kind="shares", quantity=50, expected_pct=4.0, sd_pct=3.0, horizon_bars=5, group="1d", last=200.0)
    sc = r["scenario"]
    assert sc["p50"] == pytest.approx(208.0) and sc["p10"] < 208 < sc["p90"]
    assert r["pnl_p50"] == pytest.approx(50 * 8.0) and r["pnl_p10"] == pytest.approx(50 * (sc["p10"] - 200))
    assert r["cost"] == 10000 and r["max_loss"] == 10000 and r["p_profit"] > 0.9 and r["score"] > 0 and r["risk_basis"] == "the most it can lose"
    assert r["horizon_days"] == 5 and r["eval_date"] > "2026"
    sh = w.position("AAPL", kind="short", quantity=50, expected_pct=4.0, sd_pct=3.0, horizon_bars=5, group="1d", last=200.0)
    assert sh["pnl_p50"] == pytest.approx(-r["pnl_p50"]) and sh["unlimited_loss"] and sh["max_loss"] is None
    assert sh["p_profit"] == pytest.approx(1 - r["p_profit"], abs=0.01) and sh["score"] < 0
    assert sh["risk"] == pytest.approx(50 * (sc["p90"] - 200)) and "worse of the 10th and 90th" in sh["risk_basis"]
    live = w.position("AAPL", kind="shares", quantity=1, expected_pct=0.0, sd_pct=1.0, horizon_bars=1, group="1d")
    assert live["last"] > 0 and live["scenario"]["p50"] == pytest.approx(live["last"])            # the live quote by default
    hourly = w.position("AAPL", kind="shares", quantity=1, expected_pct=1.0, sd_pct=1.0, horizon_bars=7, group="1h", last=200.0)
    assert hourly["horizon_days"] == 2                                            # 7 hourly bars: a little over a day
    with pytest.raises(RequestError):
        w.position("AAPL", kind="lottery", quantity=1, expected_pct=1, sd_pct=1, horizon_bars=1, group="1d")
    with pytest.raises(RequestError):
        w.position("AAPL", kind="long_call", quantity=101, expected_pct=1, sd_pct=1, horizon_bars=1, group="1d")


def test_option_strategies_are_built_from_the_chain_and_ranked_under_the_scenario(tmp_path):
    w = whatif(service(tmp_path))
    r = w.position("AAPL", kind="bull_call_spread", quantity=2, expected_pct=5.0, sd_pct=3.0, horizon_bars=5, group="1d")
    assert len(r["legs"]) == 2 and r["max_loss"] > 0 and r["max_loss"] == pytest.approx(r["cost"]) and r["expiration"] > r["eval_date"]
    assert r["pnl_p10"] <= r["pnl_p50"] <= r["pnl_p90"] and 0 < r["p_profit"] < 1 and r["score"] is not None
    ranked = w.rank("AAPL", quantity=200, contracts=2, expected_pct=5.0, sd_pct=3.0, horizon_bars=5, group="1d", ai_pick="long_call")
    assert len(ranked["rows"]) == len(STRATEGY_KINDS) and ranked["failed"] == [] and ranked["ai_pick"] == "long_call"
    ranks = sorted(x["rank"] for x in ranked["rows"] if x.get("rank"))
    assert ranks == list(range(1, len(ranks) + 1)) and ranked["best"] == next(x["kind"] for x in ranked["rows"] if x.get("rank") == 1)
    shares = next(x for x in ranked["rows"] if x["kind"] == "shares")
    assert shares["quantity"] == 200 and next(x for x in ranked["rows"] if x["kind"] == "long_call")["quantity"] == 2
    # A rising scenario favours the bullish side; a falling one the bearish side.
    down = w.rank("AAPL", quantity=100, expected_pct=-6.0, sd_pct=3.0, horizon_bars=5, group="1d", kinds=["shares", "short", "long_put", "long_call"])
    assert down["best"] in ("short", "long_put")


def test_a_combination_is_valued_together_so_hedges_offset(tmp_path):
    w = whatif(service(tmp_path))
    shares = w.position("AAPL", kind="shares", quantity=100, expected_pct=3.0, sd_pct=3.0, horizon_bars=5, group="1d")
    assert "_grid" not in shares
    c = w.combo("AAPL", legs=[{"kind": "shares", "quantity": 100}, {"kind": "long_put", "quantity": 1}], expected_pct=3.0, sd_pct=3.0,
                horizon_bars=5, group="1d")
    assert [r["kind"] for r in c["rows"]] == ["shares", "long_put"] and c["failed"] == []
    cb = c["combined"]
    assert cb["pnl_p50"] == pytest.approx(c["rows"][0]["pnl_p50"] + c["rows"][1]["pnl_p50"], abs=1e-6)
    assert cb["defined_risk"] and cb["max_loss"] < shares["max_loss"]                 # the put caps the shares' loss
    assert cb["pnl_p10"] > shares["pnl_p10"] and cb["pnl_p90"] < shares["pnl_p90"] and 0 < cb["p_profit"] < 1
    assert cb["cvar10"] >= 0 and cb["capital"] == pytest.approx(c["rows"][0]["cost"] + c["rows"][1]["cost"])
    assert "no order is placed" in c["note"]
    with_short = w.combo("AAPL", legs=[{"kind": "short", "quantity": 10}], expected_pct=3.0, sd_pct=3.0, horizon_bars=5, group="1d")
    assert with_short["combined"]["unlimited_loss"] and with_short["combined"]["max_loss"] is None
    with pytest.raises(RequestError):
        w.combo("AAPL", legs=[{"kind": "lottery", "quantity": 1}], expected_pct=3.0, sd_pct=3.0, horizon_bars=5, group="1d")
    with pytest.raises(RequestError):
        w.combo("AAPL", legs=[], expected_pct=3.0, sd_pct=3.0, horizon_bars=5, group="1d")


def test_a_budget_is_split_by_expected_gain_minus_the_worst_tenth_within_the_caps(tmp_path):
    w = whatif(service(tmp_path))
    a = w.allocate("AAPL", budget=10000, risk=1.0, expected_pct=3.0, sd_pct=3.0, horizon_bars=5, group="1d")
    assert 0 < a["spent"] <= 10000 and a["left"] == pytest.approx(10000 - a["spent"])
    assert all(r["dollars"] <= 5000 + 1e-6 and r["units"] >= 1 for r in a["rows"])            # the 50% cap
    assert a["combined"]["expected_pnl"] == pytest.approx(sum(r["expected_pnl"] for r in a["rows"]), rel=1e-6)
    assert a["combined"]["return_on_budget"] == pytest.approx(a["combined"]["expected_pnl"] / 10000)
    assert {x["kind"] for x in a["skipped"]} >= {"short_straddle", "short_strangle"} and "short" not in [r["kind"] for r in a["rows"]]
    # All-out puts more at risk than cautious; cautious may keep it all in cash.
    bold = w.allocate("AAPL", budget=10000, risk=0.0, expected_pct=3.0, sd_pct=3.0, horizon_bars=5, group="1d")
    careful = w.allocate("AAPL", budget=10000, risk=4.0, expected_pct=3.0, sd_pct=3.0, horizon_bars=5, group="1d")
    assert bold["combined"]["cvar10"] >= (careful["combined"]["cvar10"] if careful["combined"] else 0)
    assert careful["combined"] is None or careful["combined"]["expected_pnl"] <= bold["combined"]["expected_pnl"]
    # One strategy only, all in one allowed: the whole budget can go to it.
    one = w.allocate("AAPL", budget=10000, risk=0.0, expected_pct=3.0, sd_pct=3.0, horizon_bars=5, group="1d", kinds=["shares"], max_share=1.0)
    assert [r["kind"] for r in one["rows"]] == ["shares"] and one["rows"][0]["units"] == int(10000 // one["last"])
    with pytest.raises(RequestError):
        w.allocate("AAPL", budget=10, risk=1.0, expected_pct=3.0, sd_pct=3.0, horizon_bars=5, group="1d", kinds=["long_straddle"])
    with pytest.raises(RequestError, match="None of the strategies"):
        w.allocate("AAPL", budget=1000, risk=1.0, expected_pct=3.0, sd_pct=3.0, horizon_bars=5, group="1d", kinds=["moon"])
    # Options stop at the builder's 100 contracts, so a big budget leaves money over and says so; a short is named.
    big = w.allocate("AAPL", budget=2_000_000, risk=0.0, expected_pct=3.0, sd_pct=3.0, horizon_bars=5, group="1d",
                     kinds=["long_call", "short", "long_call"], max_share=1.0)
    assert big["rows"][0]["units"] == 100 and big["capped"] == ["Long call"] and "100 contracts" in big["note"]
    assert [x["kind"] for x in big["skipped"]] == ["short"] and len(big["rows"]) == 1


# --- the API ----------------------------------------------------------------------------------------------------------

@pytest.fixture
def client(tmp_path):
    s = Settings(data_mode="demo", db_path=tmp_path / "t.db")
    svc = TradeAssistant(s, provider=DemoData(), db=Database(tmp_path / "t.db"))
    svc.chatgpt = FakeGPT({"path_pct": [1, 1, 1, 1, 1], "expected_pct": 1, "strategy": {"kind": "shares"}, "rationale": "Up a bit."})
    return TestClient(create_app(s, svc)), s


def test_the_api_serves_the_setup_the_model_the_ai_and_the_position(client):
    c, _ = client
    s = c.get("/v2/whatif/AAPL?peers=TSM,ASML&horizon=10").json()
    peers_row = next(r for r in s["factors"] if r["key"] == "peers")
    assert s["horizon"] == 10 and len(s["model"]["path"]) == 10 and peers_row["peers"] == ["TSM", "ASML"]
    assert s["stooq"]["enabled"] is False
    m = c.post("/v2/whatif/model", json={"symbol": "AAPL", "horizon": 10, "scenario": {"us_market": 2.5}, "include": {"news": False}}).json()
    assert m["scenario"]["us_market"] == 2.5 and m["include"]["news"] is False and "news" not in [t["key"] for t in m["model"]["terms"]]
    a = c.post("/v2/whatif/ask", json={"symbol": "AAPL", "horizon": 5, "provider": "chatgpt"})
    assert a.status_code == 200 and a.json()["scenario"]["rationale"] == "Up a bit." and a.json()["label"] == "ChatGPT"
    assert c.post("/v2/whatif/ask", json={"symbol": "AAPL", "horizon": 5, "provider": "claude"}).status_code == 503
    p = c.post("/v2/whatif/position", json={"symbol": "AAPL", "kind": "shares", "quantity": 10, "expected_pct": 2, "sd_pct": 3,
                                            "horizon_bars": 5, "group": "1d"}).json()
    assert p["pnl_p50"] == pytest.approx(10 * 0.02 * p["last"]) and p["last"] == pytest.approx(s["price"], rel=0.05)
    assert c.post("/v2/whatif/position", json={"symbol": "AAPL", "kind": "shares", "quantity": 10, "expected_pct": 2, "sd_pct": 3,
                                               "horizon_bars": 5, "group": "1d", "last": 100}).status_code == 422   # the live price, always
    assert next(r for r in c.get("/v2/whatif/AAPL?peers=../x,B C,TSM").json()["factors"] if r["key"] == "peers")["peers"] == ["TSM"]
    r = c.post("/v2/whatif/rank", json={"symbol": "AAPL", "quantity": 100, "expected_pct": 2, "sd_pct": 3, "horizon_bars": 5, "group": "1d",
                                        "kinds": ["shares", "long_call"]}).json()
    assert [x["kind"] for x in r["rows"]] == ["shares", "long_call"]
    assert c.get("/v2/whatif/AAPL?horizon=999").status_code == 400
    assert c.post("/v2/whatif/position", json={"symbol": "AAPL", "kind": "x", "quantity": 1, "expected_pct": 1, "sd_pct": 1,
                                               "horizon_bars": 1, "group": "1d"}).status_code == 400
    assert c.post("/v2/whatif/model", json={"symbol": "AAPL", "horizon": 5, "extra": 1}).status_code == 422
    cb = c.post("/v2/whatif/combo", json={"symbol": "AAPL", "legs": [{"kind": "shares", "quantity": 10}, {"kind": "long_call"}],
                                          "expected_pct": 2, "sd_pct": 3, "horizon_bars": 5, "group": "1d"})
    assert cb.status_code == 200 and len(cb.json()["rows"]) == 2 and cb.json()["rows"][1]["quantity"] == 1
    al = c.post("/v2/whatif/allocate", json={"symbol": "AAPL", "budget": 5000, "risk": 1, "expected_pct": 2, "sd_pct": 3,
                                             "horizon_bars": 5, "group": "1d", "kinds": ["shares", "long_call"], "max_share": 1})
    assert al.status_code == 200 and al.json()["spent"] <= 5000 and al.json()["budget"] == 5000
    assert c.post("/v2/whatif/allocate", json={"symbol": "AAPL", "budget": 0, "risk": 1, "expected_pct": 2, "sd_pct": 3,
                                               "horizon_bars": 5, "group": "1d"}).status_code == 422


def test_the_feature_can_be_switched_off(tmp_path):
    s = Settings(data_mode="demo", db_path=tmp_path / "t.db")
    s.features["what_if"] = False
    svc = TradeAssistant(s, provider=DemoData(), db=Database(tmp_path / "t.db"))
    c = TestClient(create_app(s, svc))
    assert c.get("/v2/whatif/AAPL").status_code == 404
    assert c.get("/v2/chart/AAPL").status_code == 200


# --- the page's pure helpers ------------------------------------------------------------------------------------------

RUNNER = r"""
const fs = require("fs"), vm = require("vm");
const ctx = vm.createContext({ window: {}, document: {}, isNum: v => typeof v === "number" && isFinite(v) });
const src = fs.readFileSync(process.argv[1], "utf8");
const start = src.indexOf("const WHATIFX = ");
const end = src.indexOf("function whatIfWidget(");
vm.runInContext(src.slice(start, end) + "\n;globalThis.WHATIFX = WHATIFX;", ctx);
const calls = JSON.parse(process.argv[2]);
process.stdout.write(JSON.stringify(calls.map(([fn, args]) => { try { return { ok: ctx.WHATIFX[fn](...args) }; } catch (e) { return { error: String(e.message) }; } })));
"""


@pytest.mark.skipif(shutil.which("node") is None, reason="Node.js is not installed")
def test_the_widgets_helpers_draw_the_paths_and_size_the_position():
    m = {"last_time": 100, "last": 50.0, "times": [110, 120], "path": [51.0, 52.0], "low": [49.0, 48.0], "high": [53.0, 56.0],
         "expected_pct": 4.0, "sd_pct": [1.0, 2.0]}
    done = subprocess.run(["node", "-e", RUNNER, str(STATIC / "whatif.js"), json.dumps([
        ["pathLines", ["whatif:model", m, "#e67e22", "model", False]], ["pathLines", ["x", None, "#000", "", True]],
        ["scenarioOf", [m]], ["scenarioOf", [{"expected_pct": 1}]], ["quantities", ["shares", 250]], ["quantities", ["long_call", 3]],
        ["riskWord", [0]], ["riskWord", [4]]])],
        capture_output=True, text=True, timeout=60)
    assert done.returncode == 0, done.stderr
    lines, none, sc, sc2, q1, q2, r0, r4 = [x["ok"] for x in json.loads(done.stdout)]
    assert (r0, r4) == ("all-out", "cautious")
    assert [ln["key"] for ln in lines] == ["whatif:model:path", "whatif:model:low", "whatif:model:high"]
    assert [p["time"] for p in lines[0]["points"]] == [100, 110, 120] and lines[0]["points"][0]["value"] == 50
    assert lines[0]["title"] == "model" and lines[1]["color"].endswith("99")
    assert none == [] and sc == {"expected_pct": 4.0, "sd_pct": 2.0} and sc2 == {"expected_pct": 1, "sd_pct": 2}
    assert q1 == {"quantity": 250, "contracts": 3} and q2 == {"quantity": 300, "contracts": 3}
