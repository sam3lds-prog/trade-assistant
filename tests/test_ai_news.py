"""2.4 AI plug-in, news and the market's make-up. Every outside service is replaced by a fake
here: these tests check the requests the app makes and what it does with the replies, not the
live services."""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone

import numpy as np
import pandas as pd
import pytest
import requests
from fastapi.testclient import TestClient

from tradeassist import makeup as mk
from tradeassist import news as nw
from tradeassist.ai import AIClient, AIError, extract_json, parse_response
from tradeassist.api import create_app
from tradeassist.config import Settings, load_settings
from tradeassist.db import Database
from tradeassist.features import FEATURE_DEFAULTS
from tradeassist.providers.demo import DemoData
from tradeassist.service import TradeAssistant

KEY = "sk-ant-api03-TESTKEYabcdefghijklmnopqrstuvwxyz0123456789"


class Reply:
    def __init__(self, status: int, body: object):
        self.status_code, self._body = status, body

    def json(self):
        if isinstance(self._body, Exception):
            raise self._body
        return self._body


def answer(text: str, *, searches: int = 0, sources=(), stop: str = "end_turn", tokens=(100, 50)) -> dict:
    content = []
    if searches:
        content.append({"type": "server_tool_use", "id": "s1", "name": "web_search", "input": {"query": "markets today"}})
        content.append({"type": "web_search_tool_result", "tool_use_id": "s1",
                        "content": [{"type": "web_search_result", "url": u, "title": t, "page_age": "2 hours"} for u, t in sources]})
    content.append({"type": "text", "text": text,
                    "citations": [{"type": "web_search_result_location", "url": sources[0][0], "title": sources[0][1],
                                   "cited_text": "quoted"}] if sources else None})
    return {"content": content, "stop_reason": stop,
            "usage": {"input_tokens": tokens[0], "output_tokens": tokens[1], "server_tool_use": {"web_search_requests": searches}}}


class FakeAnthropic:
    """Replies in turn from `replies` (a list of Reply or exceptions), recording each request."""

    def __init__(self, *replies):
        self.replies = list(replies)
        self.calls: list[dict] = []

    def post(self, url, headers=None, json=None, timeout=None):
        self.calls.append({"url": url, "headers": headers, "json": json})
        r = self.replies.pop(0) if len(self.replies) > 1 else self.replies[0]
        if isinstance(r, Exception):
            raise r
        return r


def settings(tmp_path, **kw) -> Settings:
    return Settings(data_mode="demo", db_path=tmp_path / "t.db", anthropic_api_key=kw.pop("key", KEY), **kw)


# --- reading replies -----------------------------------------------------------------------

def test_parse_response_collects_text_sources_and_usage():
    body = answer("Stocks rose.", searches=2, sources=[("https://a.example/1", "A"), ("https://b.example/2", "B")])
    body["content"].append({"type": "web_search_tool_result", "content": [{"url": "javascript:alert(1)", "title": "x"}]})
    p = parse_response(body)
    assert p["text"] == "Stocks rose." and p["searches"] == ["markets today"]
    assert [s["url"] for s in p["sources"]] == ["https://a.example/1", "https://b.example/2"]   # cited first, no script links
    assert p["sources"][0]["cited"] is True and p["sources"][0]["quotes"] == ["quoted"] and p["sources"][1]["cited"] is False
    assert p["usage"] == {"input_tokens": 100, "output_tokens": 50, "web_searches": 2}


@pytest.mark.parametrize("text,expected", [
    ('{"a": 1}', {"a": 1}),
    ('Here you go:\n```json\n{"a": {"b": [1, 2]}}\n```\nThanks', {"a": {"b": [1, 2]}}),
    ('I searched. {"x": "y"} and then {"a": 2}', {"a": 2}),
    ('{"a": "brace } inside"}', {"a": "brace } inside"}),
    ("no json here", None), ('{"broken": ', None), ("", None),
])
def test_extract_json(text, expected):
    assert extract_json(text) == expected


# --- the client ------------------------------------------------------------------------------

def test_a_request_carries_the_key_only_in_its_header(tmp_path):
    db = Database(tmp_path / "t.db")
    fake = FakeAnthropic(Reply(200, answer("ok", searches=1, sources=[("https://n.example", "N")])))
    ai = AIClient(settings(tmp_path), db, session=fake)
    out = ai.ask(purpose="test", prompt="hello", web_search=True)
    call = fake.calls[0]
    assert call["url"] == "https://api.anthropic.com/v1/messages"
    assert call["headers"]["x-api-key"] == KEY and call["headers"]["anthropic-version"] == "2023-06-01"
    assert KEY not in json.dumps(call["json"]) and call["json"]["model"] == "claude-sonnet-5-5"
    assert call["json"]["tools"] == [{"type": "web_search_20250305", "name": "web_search", "max_uses": 4}]
    assert out["text"] == "ok" and out["sources"][0]["url"] == "https://n.example" and out["cached"] is False
    row = db.one("SELECT purpose, model, input_tokens, output_tokens, web_searches, ok FROM ai_usage")
    assert dict(row) == {"purpose": "test", "model": "claude-sonnet-5-5", "input_tokens": 100, "output_tokens": 50,
                         "web_searches": 1, "ok": 1}
    assert KEY not in json.dumps(ai.status()) and repr(ai.settings).count(KEY) == 0
    db.close()


def test_web_search_can_be_switched_off(tmp_path):
    db = Database(tmp_path / "t.db")
    fake = FakeAnthropic(Reply(200, answer("ok")))
    ai = AIClient(settings(tmp_path, ai_web_search=False), db, session=fake)
    out = ai.ask(purpose="test", prompt="hello", web_search=True)
    assert "tools" not in fake.calls[0]["json"] and out["web_search"] is False
    db.close()


def test_answers_are_cached_and_the_daily_limit_holds(tmp_path):
    db = Database(tmp_path / "t.db")
    fake = FakeAnthropic(Reply(200, answer("first")))
    ai = AIClient(settings(tmp_path, ai_daily_requests=2), db, session=fake)
    a = ai.ask(purpose="t", prompt="p", cache_key="k")
    b = ai.ask(purpose="t", prompt="p", cache_key="k")
    assert b["cached"] is True and b["text"] == "first" and len(fake.calls) == 1
    ai.ask(purpose="t", prompt="p", cache_key="k", refresh=True)
    assert len(fake.calls) == 2 and ai.status()["remaining_today"] == 0
    with pytest.raises(AIError, match="limit of 2"):
        ai.ask(purpose="t", prompt="other")
    assert ai.ask(purpose="t", prompt="p", cache_key="k")["cached"] is True     # saved answers still come back
    assert a["text"] == "first"
    db.close()


def test_a_paused_web_search_turn_is_resumed(tmp_path):
    db = Database(tmp_path / "t.db")
    first = answer("Searching", searches=1, sources=[("https://a.example", "A")], stop="pause_turn")
    fake = FakeAnthropic(Reply(200, first), Reply(200, answer(" done", tokens=(30, 20))))
    ai = AIClient(settings(tmp_path), db, session=fake)
    out = ai.ask(purpose="t", prompt="p", web_search=True)
    assert len(fake.calls) == 2 and fake.calls[1]["json"]["messages"][1]["role"] == "assistant"
    assert out["text"] == "Searching done" and out["usage"]["input_tokens"] == 130 and out["usage"]["web_searches"] == 1
    db.close()


@pytest.mark.parametrize("reply,message", [
    (Reply(401, {"error": {"message": f"invalid x-api-key {KEY}"}}), "refused the key"),
    (Reply(404, {"error": {"message": "model: nope"}}), "does not know the model"),
    (Reply(429, {"error": {"message": "slow down"}}), "rate-limiting"),
    (Reply(529, {"error": {"message": "overloaded"}}), "busy"),
    (Reply(400, {"error": {"message": f"bad request with key {KEY}"}}), r"refused the request \(400\)"),
    (Reply(200, ValueError("not json")), "could not read"),
    (requests.Timeout("slow"), "timed out"),
    (requests.ConnectionError("down"), "Could not reach"),
])
def test_failures_have_safe_messages_and_are_recorded(tmp_path, caplog, reply, message):
    db = Database(tmp_path / "t.db")
    ai = AIClient(settings(tmp_path), db, session=FakeAnthropic(reply))
    with caplog.at_level(logging.INFO), pytest.raises(AIError, match=message) as err:
        ai.ask(purpose="t", prompt="p")
    assert KEY not in str(err.value) and KEY not in caplog.text and "TESTKEY" not in caplog.text
    row = db.one("SELECT ok, error FROM ai_usage")
    assert row["ok"] == 0 and KEY not in (row["error"] or "")
    assert ai.status()["last_error"]["error"] == str(err.value) and ai.status()["used_today"]["failed"] == 1
    db.close()


def test_without_a_key_nothing_is_sent(tmp_path):
    db = Database(tmp_path / "t.db")
    fake = FakeAnthropic(Reply(200, answer("x")))
    ai = AIClient(settings(tmp_path, key=""), db, session=fake)
    assert ai.configured is False and "ANTHROPIC_API_KEY" in ai.status()["reason"]
    with pytest.raises(AIError, match="not set up"):
        ai.ask(purpose="t", prompt="p")
    off = AIClient(settings(tmp_path, ai_daily_requests=0), db, session=fake)
    assert off.configured is False and "AI_DAILY_REQUESTS=0" in off.status()["reason"]
    assert fake.calls == []
    db.close()


def test_settings_read_the_ai_and_news_keys(tmp_path, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", KEY)
    monkeypatch.setenv("FINNHUB_API_KEY", "finnhubkey123")
    monkeypatch.setenv("AI_MODEL", "claude-opus-5-5")
    monkeypatch.setenv("AI_WEB_SEARCH", "0")
    monkeypatch.setenv("AI_DAILY_REQUESTS", "7")
    s = load_settings(tmp_path / "none.env")
    assert (s.anthropic_api_key, s.finnhub_api_key, s.ai_model, s.ai_web_search, s.ai_daily_requests) == \
        (KEY, "finnhubkey123", "claude-opus-5-5", False, 7)
    assert KEY not in repr(s) and "finnhubkey123" not in repr(s)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "your_anthropic_key_here")
    assert load_settings(tmp_path / "none.env").anthropic_api_key == ""


# --- news ------------------------------------------------------------------------------------

class FakeFinnhub:
    def __init__(self, status=200, body=None):
        self.status, self.body, self.calls = status, body, []

    def get(self, url, params=None, timeout=None):
        self.calls.append({"url": url, "params": params})
        return Reply(self.status, self.body)


def finnhub_rows():
    ts = int(datetime(2026, 10, 9, 15, tzinfo=timezone.utc).timestamp())
    return [{"headline": "Acme beats estimates and raises guidance", "summary": "s", "source": "Reuters",
             "url": "https://news.example/1", "datetime": ts, "related": "ACME"},
            {"headline": "Acme faces probe over pricing", "summary": "", "source": "WSJ", "url": "https://news.example/2",
             "datetime": ts - 3600},
            {"headline": "ACME BEATS ESTIMATES AND RAISES GUIDANCE", "source": "Copy", "url": "https://news.example/3",
             "datetime": ts - 7200},
            {"headline": "Acme holds annual meeting", "url": "javascript:bad()", "datetime": ts - 9000},
            {"summary": "no headline"}]


def test_word_tone():
    assert nw.word_tone("Acme beats estimates, raises outlook") == 1
    assert nw.word_tone("Acme misses and cuts its forecast after a probe") == -1
    assert nw.word_tone("Acme holds its annual meeting") == 0
    t = nw.summarize_tone([{"tone": 1}, {"tone": -1}, {"tone": 0}, {"tone": 1}, {"tone": None}])
    assert (t["count"], t["positive"], t["negative"], t["neutral"], t["lean"]) == (4, 2, 1, 1, "POSITIVE")
    assert nw.summarize_tone([])["average"] is None


def test_company_news_from_finnhub(tmp_path):
    db = Database(tmp_path / "t.db")
    fake = FakeFinnhub(body=finnhub_rows())
    client = nw.NewsClient(settings(tmp_path, finnhub_api_key="fh-secret-1"), db, None, session=fake)
    out = client.company("acme", days=5)
    assert fake.calls[0]["url"].endswith("/company-news") and fake.calls[0]["params"]["symbol"] == "ACME"
    assert fake.calls[0]["params"]["token"] == "fh-secret-1"
    heads = [i["headline"] for i in out["items"]]
    assert len(heads) == 3 and heads[0] == "Acme beats estimates and raises guidance"     # duplicate dropped, newest first
    assert out["items"][0]["tone"] == 1 and out["items"][1]["tone"] == -1
    assert out["items"][2]["url"] is None                                                   # not a web link
    assert out["sources"] == ["Finnhub"] and out["errors"] == [] and out["demo"] is False
    assert client.company("acme", days=5)["cached"] is True and len(fake.calls) == 1
    db.close()


@pytest.mark.parametrize("status,message", [(401, "refused the key"), (403, "may not use company-news"),
                                            (429, "rate limit"), (500, "returned 500")])
def test_news_failures_are_reported_without_the_key(tmp_path, caplog, status, message):
    db = Database(tmp_path / "t.db")
    client = nw.NewsClient(settings(tmp_path, finnhub_api_key="fh-secret-1"), db, None,
                           session=FakeFinnhub(status=status, body={"error": "token=fh-secret-1 bad"}))
    with caplog.at_level(logging.WARNING):
        out = client.company("ACME")
    assert out["items"] == [] and message in out["errors"][0] and "fh-secret-1" not in caplog.text
    assert client.status()["last_error"]["what"] == "company news"
    assert client.company("ACME")["cached"] is False                  # failures are not cached
    db.close()


def test_demo_headlines_and_no_source(tmp_path):
    db = Database(tmp_path / "t.db")
    demo = nw.NewsClient(settings(tmp_path), db, None, session=FakeFinnhub())
    out = demo.company("KO")
    assert out["demo"] is True and len(out["items"]) == 5 and all(i["source"] == "Demo" for i in out["items"])
    assert out["notes"][0].startswith("Demo headlines")
    assert demo.company("KO")["items"] == out["items"]                 # the same for the day
    live = nw.NewsClient(Settings(data_mode="live", live_token="t", db_path=tmp_path / "t.db"), db, None, session=FakeFinnhub())
    none = live.market()
    assert none["items"] == [] and "FINNHUB_API_KEY" in none["notes"][0]
    db.close()


def test_sec_filings_join_the_headlines(tmp_path):
    db = Database(tmp_path / "t.db")
    today = datetime.now(timezone.utc).date().isoformat()

    class Sec:
        enabled = True

        def cik_for(self, t):
            if t == "SPY":
                raise RuntimeError("SPY is not in the SEC's ticker list (funds and some foreign companies are not).")
            return 320193, "Acme"

        def submissions(self, cik):
            return {"filings": {"recent": {"form": ["8-K", "4"], "filingDate": [today, "2001-01-01"],
                                           "reportDate": [today, ""], "items": ["2.02", ""],
                                           "accessionNumber": ["0000320193-26-000001", "x"],
                                           "primaryDocument": ["a.htm", "b.htm"],
                                           "primaryDocDescription": ["8-K", "4"]}}}

    client = nw.NewsClient(Settings(data_mode="live", live_token="t", db_path=tmp_path / "t.db"), db, Sec(), session=FakeFinnhub())
    out = client.company("ACME")
    assert len(out["items"]) == 1 and out["items"][0]["kind"] == "filing" and out["items"][0]["tone"] is None
    assert out["items"][0]["headline"].startswith("8-K filed: Current report") and out["sources"] == ["SEC EDGAR"]
    assert client.company("SPY")["errors"] == []                        # a fund that does not file is not a failure
    db.close()


# --- the market's make-up ------------------------------------------------------------------------

def _frame(closes, highs=None, lows=None, sma50=None, sma200=None):
    n = len(closes)
    df = pd.DataFrame({"date": pd.bdate_range("2025-01-02", periods=n), "close": closes,
                       "high": highs if highs is not None else closes, "low": lows if lows is not None else closes})
    df["sma50"] = sma50 if sma50 is not None else np.nan
    df["sma200"] = sma200 if sma200 is not None else np.nan
    df["rsi"] = 50.0
    return df


def test_changes_over_each_horizon():
    closes = [100.0 + i for i in range(70)]
    c = mk.changes(_frame(closes, sma50=150.0, sma200=np.nan))
    assert c["close"] == 169 and c["day_pct"] == pytest.approx(100 * (169 / 168 - 1))
    assert c["week_pct"] == pytest.approx(100 * (169 / 164 - 1)) and c["month_pct"] == pytest.approx(100 * (169 / 148 - 1))
    assert c["quarter_pct"] == pytest.approx(100 * (169 / 106 - 1)) and c["vs_50d_pct"] == pytest.approx(100 * (169 / 150 - 1))
    assert c["vs_200d_pct"] is None
    assert mk.changes(_frame([100.0, 101.0]))["month_pct"] is None


def test_breadth_counts_and_leaves_unknowns_out():
    up = _frame([100.0] * 252 + [110.0], sma50=105.0, sma200=100.0)               # a new high, above both
    down = _frame([100.0] * 252 + [90.0], sma50=95.0, sma200=np.nan)              # a new low, 200-day unknown
    short = _frame([50.0, 49.0])
    b = mk.breadth({"UP": up, "DOWN": down, "SHORT": short})
    assert (b["stocks"], b["advancing"], b["declining"]) == (3, 1, 2)
    assert (b["above_50d"], b["measured_50d"], b["above_200d"], b["measured_200d"]) == (1, 2, 1, 1)
    assert b["above_200d_pct"] == 100 and (b["new_highs"], b["new_lows"], b["measured_52w"]) == (1, 1, 2)
    assert mk.breadth({})["above_50d_pct"] is None


# --- through the API on demo data ----------------------------------------------------------------

def make_app(tmp_path, *, ai_replies=None, **kw):
    s = settings(tmp_path, **kw)
    svc = TradeAssistant(s, provider=DemoData(), db=Database(tmp_path / "t.db"))
    fake = FakeAnthropic(*(ai_replies or [Reply(200, answer("{}"))]))
    svc.ai = AIClient(s, svc.db, session=fake)
    return TestClient(create_app(s, svc)), svc, fake


def test_market_makeup_endpoint(tmp_path):
    c, svc, _ = make_app(tmp_path)
    r = c.get("/v2/market/makeup")
    assert r.status_code == 200, r.text
    m = r.json()
    assert [i["symbol"] for i in m["indexes"]] == ["SPY", "QQQ", "IWM", "DIA"] and len(m["sectors"]) == 11
    assert m["breadth"]["stocks"] + m["breadth"]["unreadable"] == 100 and m["regime"]["regime"]
    assert {o["symbol"] for o in m["options"]} == {"SPY", "QQQ"}
    months = [s["month_pct"] for s in m["sectors"]]
    assert months == sorted(months, reverse=True) and m["notes"][0].startswith("Demo")
    assert c.get("/v2/market/makeup").json()["cached"] is True
    w = c.get("/v2/market/makeup", params={"breadth": "watchlist"}).json()["breadth"]
    assert w["universe"] == "watchlist" and w["asked"] == len(svc.settings.watchlist)
    assert c.get("/v2/market/makeup", params={"breadth": "everything"}).status_code == 400
    svc.db.close()


def test_market_brief_sends_figures_but_nothing_about_the_account(tmp_path):
    brief = ('```json\n{"headline": "Calm day", "summary": "Demo figures show a quiet market.", "drivers": ["a", "b"], '
             '"risks": ["c"], "watch": ["d"], "tone": "sideways"}\n```')
    c, svc, fake = make_app(tmp_path, account_size=123457.0,
                            ai_replies=[Reply(200, answer(brief, searches=1, sources=[("https://n.example/x", "N")]))])
    spot = svc.market.spot("KO")
    c.post("/v2/positions", json={"symbol": "KO", "quantity": 37, "invalidation_price": round(spot * 0.8, 2),
                                  "entry": {"price": round(spot, 2)}})
    r = c.post("/v2/ai/market-brief", json={"web_search": True})
    assert r.status_code == 200, r.text
    b = r.json()
    assert b["brief"]["headline"] == "Calm day" and b["brief"]["tone"] is None          # not one of the allowed words
    assert b["brief"]["drivers"] == ["a", "b"] and b["sources"][0]["url"] == "https://n.example/x"
    sent = fake.calls[0]["json"]["messages"][0]["content"]
    assert sent == b["sent"] and "SPY (S&P 500)" in sent and "Breadth over" in sent and "made-up demo data" in sent
    assert "Recent market headlines" in sent and "Search the web" in sent
    assert "123457" not in json.dumps(fake.calls) and "123,457" not in json.dumps(fake.calls) and KEY not in b["sent"]
    assert c.post("/v2/ai/market-brief", json={}).json()["cached"] is True and len(fake.calls) == 1
    svc.db.close()


def test_stock_brief_and_a_reply_that_is_not_json(tmp_path):
    c, svc, fake = make_app(tmp_path, ai_replies=[Reply(200, answer("Just prose, no braces."))])
    r = c.post("/v2/ai/brief/aapl", json={"web_search": False})
    assert r.status_code == 200, r.text
    b = r.json()
    assert b["brief"] is None and b["text"] == "Just prose, no braces." and b["symbol"] == "AAPL"
    sent = fake.calls[0]["json"]["messages"][0]["content"]
    assert "tools" not in fake.calls[0]["json"] and "Stock: AAPL" in sent and "30-day implied volatility" in sent
    assert "Recent headlines and filings" in sent and "Use only the figures" in sent
    svc.db.close()


def test_headline_tone_by_ai(tmp_path):
    scores = '{"items": [{"i": 1, "tone": 3, "why": "big"}, {"i": 2, "tone": -0.4, "why": "meh"}, {"i": 99, "tone": 1}], "summary": "Mixed."}'
    c, svc, fake = make_app(tmp_path, ai_replies=[Reply(200, answer(scores))])
    t = c.post("/v2/ai/tone", json={"symbol": "KO"}).json()
    assert [i["tone"] for i in t["items"]] == [1.0, -0.4] and t["items"][0]["tone_method"] == "ai"   # clamped, unknown index dropped
    assert t["summary"] == "Mixed." and t["tone"]["count"] == 2
    c2, svc2, _ = make_app(tmp_path / "b", ai_replies=[Reply(200, answer("no scores"))])
    bad = c2.post("/v2/ai/tone", json={"symbol": "KO"})
    assert bad.status_code == 503 and "could not be read" in bad.json()["error"]
    svc.db.close()
    svc2.db.close()


def test_ai_routes_without_a_key_say_how_to_set_it_up(tmp_path):
    c, svc, fake = make_app(tmp_path, key="")
    r = c.post("/v2/ai/market-brief", json={})
    assert r.status_code == 503 and "ANTHROPIC_API_KEY" in r.json()["error"] and fake.calls == []
    st = c.get("/v2/ai/status").json()
    assert st["configured"] is False and st["news"]["demo_headlines"] is True
    assert c.get("/v2/news/KO").status_code == 200 and c.get("/v2/news/market").status_code == 200
    svc.db.close()


def test_another_web_page_cannot_spend_the_ai_budget(tmp_path):
    c, svc, fake = make_app(tmp_path)
    r = c.post("/v2/ai/market-brief", json={}, headers={"Origin": "https://evil.example"})
    assert r.status_code == 403 and fake.calls == []
    svc.db.close()


def test_the_strategy_reader_uses_the_ai_only_for_what_the_rules_miss(tmp_path):
    reply = '{"strategy": "long_strangle", "symbol": "ko", "dte": 5000, "delta": 0.25, "contracts": 2, "strikes": [1, "x"]}'
    c, svc, fake = make_app(tmp_path, ai_replies=[Reply(200, answer(reply))])
    d = c.post("/v2/strategies/parse", json={"text": "a wide bet on Coca-Cola moving a lot"}).json()
    assert d["parsed"]["source"] == "ai" and d["result"]["strategy"] == "long_strangle" and d["result"]["symbol"] == "KO"
    assert d["result"]["contracts"] == 2 and "dte" not in d["parsed"]["spec"]                 # out of range: dropped
    assert "strikes" not in d["parsed"]["spec"]
    c.post("/v2/strategies/parse", json={"text": "KO iron condor 30 days"})
    assert len(fake.calls) == 1                                                              # the rules handled it
    svc.db.close()


def test_strategy_reader_refuses_answers_that_are_not_templates(tmp_path):
    c, svc, fake = make_app(tmp_path, ai_replies=[Reply(200, answer('{"strategy": "jade_lizard", "symbol": "KO"}'))])
    assert svc.briefs.read_strategy("a jade lizard on KO") is None
    c2, svc2, _ = make_app(tmp_path / "b", ai_replies=[Reply(200, answer('{"strategy": "iron_condor", "symbol": "DROP TABLE"}'))])
    got = svc2.briefs.read_strategy("condor")
    assert got == {"strategy": "iron_condor"}
    svc.db.close()
    svc2.db.close()


def test_health_shows_ai_and_news_without_keys(tmp_path):
    c, svc, _ = make_app(tmp_path, finnhub_api_key="fh-secret-1")
    h = c.get("/v2/health/data").json()
    assert h["ai"]["configured"] is True and h["news"]["finnhub"] is True
    assert KEY not in json.dumps(h) and "fh-secret-1" not in json.dumps(h)
    assert KEY not in c.get("/api/status").text and KEY not in c.get("/v2/ai/status").text
    svc.db.close()


def test_market_news_can_be_switched_off(tmp_path):
    c, svc, _ = make_app(tmp_path, features={**FEATURE_DEFAULTS, "market_news": False})
    assert c.get("/v2/market/makeup").status_code == 404 and c.get("/v2/news/KO").status_code == 404
    assert c.post("/v2/ai/brief/KO", json={}).status_code == 404
    svc.db.close()


def test_migration_10_adds_the_usage_table(tmp_path):
    db = Database(tmp_path / "t.db")
    cols = [r["name"] for r in db.query("PRAGMA table_info(ai_usage)")]
    assert cols == ["id", "at", "day", "purpose", "model", "input_tokens", "output_tokens", "web_searches", "ok", "error"]
    assert db.schema_version() >= 10
    db.close()


def test_news_refusals_carry_finnhubs_reason_without_the_key(tmp_path):
    db = Database(tmp_path / "t.db")
    for status, body, words in ((401, {"error": "Invalid API key fh-secret-1"}, "Finnhub said: Invalid API key [redacted]"),
                                (403, {"error": "You don't have access to this resource."}, "You don't have access")):
        client = nw.NewsClient(settings(tmp_path, finnhub_api_key="fh-secret-1"), db, None,
                               session=FakeFinnhub(status=status, body=body))
        err = client.company("ACME")["errors"][0]
        assert words in err and "fh-secret-1" not in err
    db.close()


# --- replies with citation tags, and replies cut off at the length limit -----------------------------

CUT_OFF = ('{"headline":"Stocks close the week higher","summary":"Today is Saturday. <cite index=\\"15-3,15-4\\">The '
           'Nasdaq rose 0.64%.</cite> Breadth is narrow.","drivers":["Tech rebounded.","<cite index=\\"5-6\\">Yields '
           'climbed.</cite>"],"risks":["CPI on Wednesday."],"watch":["Tue 13 Oct: banks report.","Weds 28 Oct, 2:00 pm '
           'ET: <cite index=\\"23')


def test_a_reply_cut_off_mid_answer_is_still_read():
    from tradeassist.ai import _repair
    j = extract_json(CUT_OFF)
    assert j["headline"] == "Stocks close the week higher" and j["drivers"][1] == "Yields climbed."
    assert "<cite" not in j["summary"] and j["summary"] == "Today is Saturday. The Nasdaq rose 0.64%. Breadth is narrow."
    assert j["watch"][0] == "Tue 13 Oct: banks report." and len(j["watch"]) == 2
    assert _repair('{"a": "x", "unfinished') == {"a": "x"} and _repair('{"a": ["x", "y') == {"a": ["x", "y"]}
    assert _repair('{"a": 1, "b":') == {"a": 1} and _repair('{"a": {"b": [1, 2') == {"a": {"b": [1, 2]}}
    assert _repair('{"a": 1}') is None and extract_json('{"a": tr') is None


def test_a_cut_off_brief_is_shown_cleanly_and_asked_again_next_time(tmp_path):
    c, svc, fake = make_app(tmp_path, ai_replies=[Reply(200, {**answer(CUT_OFF), "stop_reason": "max_tokens"})])
    b = c.post("/v2/ai/market-brief", json={"web_search": False}).json()
    assert b["truncated"] is True and b["brief"]["headline"] == "Stocks close the week higher"
    assert all("<cite" not in x for x in [b["brief"]["summary"], *b["brief"]["drivers"], *b["brief"]["watch"]])
    assert b["brief"]["watch"][-1] == "Weds 28 Oct, 2:00 pm ET:"
    c.post("/v2/ai/market-brief", json={"web_search": False})
    assert len(fake.calls) == 2                                          # not served from the cache
    assert fake.calls[0]["json"]["max_tokens"] == 4000 and "no citation tags" in fake.calls[0]["json"]["messages"][0]["content"]
    svc.db.close()
