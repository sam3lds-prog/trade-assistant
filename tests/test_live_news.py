"""Live news and moves (2.10): the sources' formats, the judgement of a price's answer, the levels, the feed's
life cycle on demo data, holdings alerts, the API and the safety of the RTPR key."""

from __future__ import annotations

import json
import threading
import time
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from tradeassist import livenews as L
from tradeassist.analysis import Analyst
from tradeassist.api import create_app
from tradeassist.config import Settings
from tradeassist.db import Database
from tradeassist.features import FEATURE_DEFAULTS
from tradeassist.morning import Morning
from tradeassist.providers.demo import DemoData
from tradeassist.service import TradeAssistant

LIMITS = SimpleNamespace(live_min_reaction_pct=1.0, live_min_reaction_atr=0.3, live_min_day_atr=1.5,
                         live_min_day_pct=3.0)

SEC_ATOM = """<?xml version="1.0" encoding="ISO-8859-1" ?>
<feed xmlns="http://www.w3.org/2005/Atom">
<title>Latest Filings - Fri, 16 Oct 2026 16:31:02 EDT</title>
<entry>
<title>8-K - Apple Inc. (0000320193) (Filer)</title>
<link rel="alternate" type="text/html" href="https://www.sec.gov/Archives/edgar/data/320193/000032019326000080/0000320193-26-000080-index.htm"/>
<summary type="html"> &lt;b&gt;Filed:&lt;/b&gt; 2026-10-16 &lt;b&gt;AccNo:&lt;/b&gt; 0000320193-26-000080 &lt;b&gt;Size:&lt;/b&gt; 512 KB&lt;br&gt;Item 2.02: Results of Operations and Financial Condition&lt;br&gt;Item 9.01: Financial Statements and Exhibits</summary>
<updated>2026-10-16T16:30:12-04:00</updated>
<category scheme="https://www.sec.gov/" label="form type" term="8-K"/>
<id>urn:tag:sec.gov,2008:accession-number=0000320193-26-000080</id>
</entry>
<entry>
<title>4 - Someone Person (0001234567) (Reporting)</title>
<link rel="alternate" type="text/html" href="https://www.sec.gov/x"/>
<summary type="html">Filed: 2026-10-16 AccNo: 0001234567-26-000001</summary>
<updated>2026-10-16T16:29:00-04:00</updated>
<id>urn:tag:sec.gov,2008:accession-number=0001234567-26-000001</id>
</entry>
<entry><title>not a filing title</title></entry>
</feed>"""


# --- the sources' formats ------------------------------------------------------------------------------------

def test_sec_current_filings_are_read_and_odd_entries_skipped():
    rows = L.parse_sec_current(SEC_ATOM)
    assert len(rows) == 1                                     # the reporting-owner Form 4 and the junk are skipped
    f = rows[0]
    assert (f["form"], f["company"], f["cik"], f["accession"]) == ("8-K", "Apple Inc.", 320193, "0000320193-26-000080")
    assert f["items"] == ["2.02", "9.01"] and f["updated"] == datetime(2026, 10, 16, 20, 30, 12, tzinfo=timezone.utc)
    assert L.sec_headline(f) == "8-K: Apple Inc. — Results of operations (earnings release)"
    assert L.parse_sec_current("<not xml") == []


def test_rtpr_rest_items_alerts_and_impact_frames_become_items():
    rest = L.rtpr_article({"ticker": "AAPL", "article_published_at": "2026-06-29T18:30:00Z",
                           "article_url": "https://rtpr.io/a/acme_n123"})
    assert rest["symbols"] == ["AAPL"] and rest["external_id"] == "acme_n123" and rest["needs_headline"]
    assert rest["published_at"].startswith("2026-06-29T18:30:00") and rest["kind"] == "press_release"
    alert = L.rtpr_article({"type": "alert", "ticker": "AAPL", "rules": [{"rule_name": "Apple guidance"}],
                            "article_published_at": "2026-06-29T18:30:00Z",
                            "article_url": "https://rtpr.io/a/acme_n123?exp=1777497120&sig=ab12"})
    assert alert["external_id"] == "acme_n123" and alert["extra"]["rules"] == ["Apple guidance"]
    with_id = L.rtpr_article({"ticker": "AAPL", "id": 991, "article_url": "https://rtpr.io/a/acme_n123?sig=x"})
    assert with_id["external_id"] == "acme_n123"              # the same release from the stream and the archive
    impact = L.rtpr_article({"type": "alert", "alert_kind": "high_impact", "ticker": "BMRA", "impact_score": 92,
                             "impact_tier": "high", "event_type": "fda_approval", "impact_direction": "bullish",
                             "article_published_at": "2026-06-29T18:30:00Z", "band_hit_rate": 0.41,
                             "article_url": "https://rtpr.io/a/acme_n456?exp=1&sig=cd34"})
    assert impact["tone"] == 1.0 and impact["extra"]["impact"]["score"] == 92
    assert impact["extra"]["impact"]["event_type"] == "fda_approval"
    assert L.rtpr_article({"article_url": "https://rtpr.io/a/x"}) is None          # no ticker: nothing to trade
    assert L.rtpr_article({"ticker": "not a ticker!", "article_url": "https://rtpr.io/a/x"}) is None
    assert L.rtpr_article("nonsense") is None


def test_headline_is_the_first_real_line_of_the_release():
    text = "# Acme Raises Full-Year Outlook\n\nBusiness Wire\nNEW YORK -- Acme Corp. (NYSE: ACME) today raised its outlook."
    head, summary = L.headline_from_text(text)
    assert head == "Acme Raises Full-Year Outlook" and summary.startswith("NEW YORK")
    assert L.headline_from_text("") == ("", "")


def test_finnhub_rows_and_tickers_are_cleaned():
    rows = [{"id": 7, "headline": "Acme beats estimates", "related": "ACME,BRK-B", "datetime": 1760640000,
             "url": "https://x.test/a", "source": "Reuters"},
            {"id": 8, "headline": "   ", "related": "ACME"}, "junk"]
    items = L.finnhub_items(rows)
    assert len(items) == 1 and items[0]["symbols"] == ["ACME", "BRK.B"] and items[0]["tone"] == 1.0
    assert L.clean_symbols(["NASDAQ:aapl", "aapl", "toolongticker", 5]) == ["AAPL"]


def test_an_item_is_kept_once(tmp_path):
    db = Database(tmp_path / "t.db")
    item = L.make_item("Finnhub", "1", ["AAPL"], "Apple wins a contract")
    assert L.store_item(db, item) is not None and L.store_item(db, item) is None
    assert L.make_item("X", "1", [], "h", url="javascript:alert(1)")["url"] is None


# --- the judgement and the levels ----------------------------------------------------------------------------

def test_a_reaction_since_the_news_confirms_the_way_it_went():
    up = L.judge(ref=100, price=102, prev_close=100.5, atr=3.0, late=False, settings=LIMITS)
    assert up["direction"] == "LONG" and up["path"] == "reaction"
    down = L.judge(ref=100, price=98.5, prev_close=100, atr=3.0, late=False, settings=LIMITS)
    assert down["direction"] == "SHORT"
    small = L.judge(ref=100, price=100.8, prev_close=100, atr=3.0, late=False, settings=LIMITS)
    assert small["direction"] is None                                 # under 1%
    quiet_range = L.judge(ref=100, price=101.2, prev_close=100, atr=6.0, late=False, settings=LIMITS)
    assert quiet_range["direction"] is None                           # 1.2% is only 0.2 of this stock's ranges
    against = L.judge(ref=100, price=101.5, prev_close=104, atr=3.0, late=False, settings=LIMITS)
    assert against["direction"] is None                               # up since the news, still down on the day


def test_a_late_item_can_only_ride_a_move_already_made():
    moved = dict(ref=110, price=110.5, prev_close=105, atr=3.0, settings=LIMITS)
    assert L.judge(late=False, **moved)["direction"] is None
    late = L.judge(late=True, **moved)
    assert late["direction"] == "LONG" and late["path"] == "day_move"
    fading = L.judge(ref=112, price=110, prev_close=105, atr=3.0, late=True, settings=LIMITS)
    assert fading["direction"] is None                                # falling since the item: no ride


def test_the_stop_sits_beyond_the_pre_news_price_and_targets_beyond_one_risk():
    stop, t1, t2, why = L.levels_for("LONG", 105.0, 100.0, 4.0, None, "reaction")
    assert stop == pytest.approx(99.0) and "news arrived" in why
    assert t1 == pytest.approx(105 + 2 * 6) and t2 == pytest.approx(105 + 3 * 6)
    stop, t1, t2, _ = L.levels_for("SHORT", 95.0, 100.0, 4.0, None, "reaction")
    assert stop == pytest.approx(101.0) and t1 < 95 and t2 < t1
    # Too close to the news price: the chart's level, held to 2.5 ranges.
    stop, t1, t2, why = L.levels_for("LONG", 100.4, 100.0, 4.0, {"invalidation": 80.0, "target_1": 101.0,
                                                                 "target_2": 125.0}, "reaction")
    assert stop == pytest.approx(100.4 - 10.0) and "chart" in why
    assert t1 == pytest.approx(125.0) and t2 > t1                     # 101 is under one risk away
    stop, _, _, why = L.levels_for("LONG", 100.0, 100.0, 4.0, None, "day_move")
    assert stop == pytest.approx(100 - 4.8) and "1.2" in why
    stop, t1, t2, _ = L.levels_for("SHORT", 5.5, 7.5, 1.0, None, "reaction")     # review finding: no negative target
    assert stop > 5.5 > t1 > t2 > 0


# --- the feed on demo data -----------------------------------------------------------------------------------

@pytest.fixture
def feed(tmp_path):
    # The movers scan (2.11) is tested in tests/test_search_news.py; off here so these signals are the tests' own.
    s = Settings(data_mode="demo", db_path=tmp_path / "t.db", live_mover_sweep_minutes=0)
    svc = TradeAssistant(s, provider=DemoData(), db=Database(tmp_path / "t.db"))
    analyst = Analyst(svc)
    return L.LiveFeed(svc, Morning(svc, analyst, s.features), analyst)


def _signal(feed, symbol=None):
    rows = feed.db.query("SELECT * FROM live_signals" + (" WHERE symbol = ?" if symbol else "") + " ORDER BY id",
                         (symbol,) if symbol else ())
    return [L.signal_row(r) for r in rows]


def _quote(feed, symbol):
    return feed.svc.market.provider.quotes([symbol])[symbol]


def test_a_news_move_becomes_an_opportunity_with_a_card_and_a_failed_move_is_stopped(feed):
    item = L.make_item("Demo", "x1", ["NVDA"], "NVDA raises its outlook", published=L.now_utc(), kind="press_release")
    item["extra"]["demo_move"] = 0.05
    nid = feed.ingest(item)
    (sig,) = _signal(feed)
    assert sig["state"] == "waiting" and sig["news_id"] == nid
    feed.check(sig, _quote(feed, "NVDA"), "open")
    (sig,) = _signal(feed)
    assert sig["state"] == "watching" and sig["ref_price"] > 0 and sig["atr"] > 0
    feed._demo_runs[sig["id"]] = (time.time() - 130, 0.05)               # two minutes on: the made-up move is done
    feed.check(sig, _quote(feed, "NVDA"), "open")
    (sig,) = _signal(feed)
    assert sig["state"] == "opportunity" and sig["direction"] == "LONG" and sig["path"] == "reaction"
    card = sig["card"]
    assert card["setup"] == "news" and card["entry"] == pytest.approx(sig["last_price"])
    assert card["stop"] < sig["ref_price"] < card["entry"] < card["target_1"] < card["target_2"]
    assert card["order_text"]
    # The move fails: back through the stop.
    feed._demo_runs[sig["id"]] = (time.time() - 130, card["stop"] / sig["ref_price"] - 1 - 0.01)
    feed.check(sig, _quote(feed, "NVDA"), "open")
    (sig,) = _signal(feed)
    assert sig["state"] == "stopped" and "stop" in sig["reason"] and sig["next_check_at"] is None


def test_no_move_in_an_hour_is_no_reaction(feed):
    feed.ingest(L.make_item("Demo", "x2", ["KO"], "KO schedules its results call", published=L.now_utc()))
    (sig,) = _signal(feed)
    feed.check(sig, _quote(feed, "KO"), "open")
    for _ in range(len(L.CHECK_DELAYS)):
        (sig,) = _signal(feed)
        if sig["state"] != "watching":
            break
        feed.check(sig, _quote(feed, "KO"), "open")
    (sig,) = _signal(feed)
    assert sig["state"] == "no_reaction" and sig["next_check_at"] is None and sig["checks"] == len(L.CHECK_DELAYS)


def test_items_outside_the_universe_old_items_and_repeats(feed):
    feed.ingest(L.make_item("Demo", "x3", ["ZZZQ"], "A tiny company does something", published=L.now_utc()))
    feed.ingest(L.make_item("Demo", "x4", ["AAPL"], "Old news", published=L.now_utc() - timedelta(hours=20)))
    assert _signal(feed) == []                                       # not in the universe; too old to trade
    snap = feed.snapshot()
    assert [i["headline"] for i in snap["items"]] == ["Old news"]     # the other ticker is hidden by default
    assert len(feed.snapshot(all_symbols=True)["items"]) == 2
    feed.ingest(L.make_item("Demo", "x5", ["AAPL"], "Apple news", published=L.now_utc()))
    feed.ingest(L.make_item("Demo", "x6", ["AAPL"], "More Apple news", published=L.now_utc()))
    (sig,) = _signal(feed, "AAPL")                                   # the second item joins the first's signal
    assert len(sig["news_ids"]) == 2
    snap = feed.snapshot()
    assert [n["headline"] for n in snap["signals"][0]["news"]] == ["Apple news", "More Apple news"]


def test_outside_regular_hours_a_price_needs_a_recent_trade(feed):
    old = (L.now_utc() - timedelta(hours=1)).isoformat()
    assert feed._price({"last": 50.0, "trade_time": old}, "extended") == (None, "no trade in the last 15 minutes outside regular hours")
    assert feed._price({"last": 50.0, "trade_time": L.now_utc().isoformat()}, "extended") == (50.0, None)
    assert feed._price({"last": 50.0, "bid": 49.98, "ask": 50.02}, "open")[0] == pytest.approx(50.0)
    assert feed._price({"last": None}, "open")[0] is None


def test_a_stock_you_hold_gets_an_alert_when_the_news_goes_against_it(feed):
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    with feed.db.tx() as cur:
        cur.execute("""INSERT INTO positions (symbol, instrument_type, direction, status, quantity, multiplier,
                       created_at, updated_at) VALUES ('NVDA', 'STOCK', 'LONG', 'OPEN', 50, 1, ?, ?)""", (now, now))
    assert feed.holdings("NVDA") == [{"position_id": 1, "bias": 1, "what": "long 50 shares"}]
    with feed.db.tx() as cur:
        cur.execute("UPDATE positions SET open_quantity = 20 WHERE id = 1")
    assert feed.holdings("NVDA")[0]["what"] == "long 20 shares"            # what is still held, not the plan
    item = L.make_item("Demo", "x7", ["NVDA"], "NVDA cuts guidance", published=L.now_utc())
    item["extra"]["demo_move"] = -0.05
    feed.ingest(item)
    (sig,) = _signal(feed)
    feed.check(sig, _quote(feed, "NVDA"), "open")
    (sig,) = _signal(feed)
    feed._demo_runs[sig["id"]] = (time.time() - 130, -0.05)
    feed.check(sig, _quote(feed, "NVDA"), "open")
    (sig,) = _signal(feed)
    assert sig["state"] == "opportunity" and sig["direction"] == "SHORT"
    assert sig["holdings"][0]["news"] == "against"


def test_a_confirmed_signal_is_emailed_once_when_asked_for(tmp_path, monkeypatch):
    from tradeassist import mailer
    s = Settings(data_mode="demo", db_path=tmp_path / "t.db", live_news_email=True,
                 smtp_user="me@example.com", smtp_password="pw")
    svc = TradeAssistant(s, provider=DemoData(), db=Database(tmp_path / "t.db"))
    analyst = Analyst(svc)
    feed = L.LiveFeed(svc, Morning(svc, analyst, s.features), analyst)
    sent = []
    monkeypatch.setattr(mailer, "send", lambda settings, subject, text, html=None: sent.append((subject, text)) or "me")
    item = L.make_item("Demo", "x8", ["MSFT"], "MSFT wins a contract", published=L.now_utc())
    item["extra"]["demo_move"] = 0.04
    feed.ingest(item)
    (sig,) = _signal(feed)
    feed.check(sig, _quote(feed, "MSFT"), "open")
    (sig,) = _signal(feed)
    feed._demo_runs[sig["id"]] = (time.time() - 130, 0.04)
    feed.check(sig, _quote(feed, "MSFT"), "open")
    assert len(sent) == 1 and sent[0][0] == "Buy MSFT: price confirms the news"
    assert "Nothing has been ordered" in sent[0][1]
    (sig,) = _signal(feed)
    assert sig["emailed"] == 1


def test_the_loop_starts_stops_and_is_remembered(feed):
    feed.start()
    try:
        deadline = time.time() + 10
        while time.time() < deadline and not feed.db.query("SELECT id FROM live_news"):
            time.sleep(0.2)
        assert feed.running and feed.db.query("SELECT id FROM live_news")       # the demo source produced an item
    finally:
        feed.stop()
    assert not feed.running
    assert feed.db.cache_get(f"live:on:{feed.svc.market.info.mode}")["on"] is False


# --- RTPR: the key stays on rtpr.io, and the stream's control frames ------------------------------------------

class _Session:
    def __init__(self, status=200, text="Headline\nBody", body=None):
        self.calls = []
        self.status, self.text, self.body = status, text, body

    def get(self, url, params=None, headers=None, timeout=None):
        self.calls.append((url, params, headers or {}))
        return SimpleNamespace(status_code=self.status, text=self.text, json=lambda: self.body)


def test_the_rtpr_key_is_sent_only_to_rtpr_over_https():
    session = _Session()
    client = L.RtprClient("secret-key", session)
    assert client.article("https://rtpr.io/a/x?exp=1&sig=2") == ("Headline", "Body")
    assert session.calls[-1][2] == {"Authorization": "Bearer secret-key"}
    client.article("https://cdn.rtpr.io/a/x")
    assert session.calls[-1][2] == {"Authorization": "Bearer secret-key"}
    for bad in ("https://evil.example/rtpr.io/a/x", "https://rtpr.io.evil.example/a", "http://rtpr.io/a/x"):
        with pytest.raises(RuntimeError, match="not an https address on rtpr.io"):
            client.article(bad)
    assert len(session.calls) == 2                            # nothing else was ever requested


def test_rtpr_rest_errors_say_what_to_fix_without_the_key():
    for status, words in ((401, "RTPR_API_KEY"), (403, "Pro"), (500, "500")):
        with pytest.raises(RuntimeError, match=words) as info:
            L.RtprClient("secret-key", _Session(status=status)).recent()
        assert "secret-key" not in str(info.value)
    body = {"count": 1, "articles": [{"ticker": "AAPL", "article_url": "https://rtpr.io/a/n1"}], "next_token": None}
    assert L.RtprClient("k", _Session(body=body)).recent() == body["articles"]


def test_the_stream_answers_pings_passes_alerts_on_and_stops_on_a_refused_key(monkeypatch):
    from websockets.exceptions import ConnectionClosed
    from websockets.frames import Close

    sent, frames, states = [], [], []

    class FakeSocket:
        def __init__(self):
            self.queue = [json.dumps({"type": "ping"}),
                          json.dumps({"type": "alert", "ticker": "AAPL", "article_url": "https://rtpr.io/a/n9"})]

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def recv(self, timeout=None):
            if self.queue:
                return self.queue.pop(0)
            raise ConnectionClosed(Close(4001, "invalid key"), None)

        def send(self, text):
            sent.append(text)

    import websockets.sync.client as wsc
    monkeypatch.setattr(wsc, "connect", lambda url, **kw: FakeSocket())
    out = L.RtprClient("k").stream(frames.append, threading.Event(), lambda s, n: states.append((s, n)))
    assert out == "fatal" and sent == ['{"type": "pong"}'] and frames[0]["ticker"] == "AAPL"
    assert states[0] == ("streaming", None) and "RTPR_API_KEY" in states[-1][1]


# --- through the API -------------------------------------------------------------------------------------------

def test_the_api_lists_starts_and_stops_and_the_flag_switches_it_off(tmp_path):
    s = Settings(data_mode="demo", db_path=tmp_path / "t.db")
    svc = TradeAssistant(s, provider=DemoData(), db=Database(tmp_path / "t.db"))
    with TestClient(create_app(s, svc)) as client:
        snap = client.get("/v2/live").json()
        assert snap["running"] is False and [x["name"] for x in snap["sources"]] == ["Demo"]
        assert any("RTPR_API_KEY" in n for n in snap["setup_notes"])
        assert client.post("/v2/live/start", json={}).json()["running"] is True
        assert client.post("/v2/live/stop", json={}).json()["running"] is False
        assert callable(svc.live)                       # the live option quote is not shadowed by the feed
    off = Settings(data_mode="demo", db_path=tmp_path / "u.db", features={**FEATURE_DEFAULTS, "live_news": False})
    svc2 = TradeAssistant(off, provider=DemoData(), db=Database(tmp_path / "u.db"))
    with TestClient(create_app(off, svc2)) as client:
        assert client.get("/v2/live").status_code == 404


def test_settings_read_the_live_options(monkeypatch, tmp_path):
    from tradeassist.config import load_settings
    for k, v in {"RTPR_API_KEY": "abc", "LIVE_UNIVERSE": "all", "LIVE_NEWS_EMAIL": "1", "LIVE_MIN_REACTION_PCT": "2"}.items():
        monkeypatch.setenv(k, v)
    s = load_settings(tmp_path / "none.env")
    assert (s.rtpr_api_key, s.live_universe, s.live_news_email, s.live_min_reaction_pct) == ("abc", "all", True, 2.0)
    assert "abc" not in repr(s)
    monkeypatch.setenv("LIVE_UNIVERSE", "everything")
    with pytest.raises(ValueError, match="LIVE_UNIVERSE"):
        load_settings(tmp_path / "none.env")



# --- the review of 2.10 ------------------------------------------------------------------------------------------

def _confirm(feed, symbol, move):
    item = L.make_item("Demo", f"r-{symbol}-{move}", [symbol], f"{symbol} news", published=L.now_utc())
    item["extra"]["demo_move"] = move
    feed.ingest(item)
    (sig,) = _signal(feed, symbol)
    feed.check(sig, _quote(feed, symbol), "open")
    (sig,) = _signal(feed, symbol)
    feed._demo_runs[sig["id"]] = (time.time() - 130, move)
    return sig


def test_a_stop_then_start_never_runs_two_loops(feed, monkeypatch):
    entered, release = threading.Event(), threading.Event()

    def slow_pass():
        entered.set()
        release.wait(10)                                       # a long pass: chains, a slow source
    monkeypatch.setattr(feed, "_check_due", slow_pass)
    feed.start()
    assert entered.wait(10)
    feed.stop(wait=0.1)
    assert feed.stopping and not feed.running
    with pytest.raises(RuntimeError, match="still finishing"):
        feed.start()
    release.set()
    for t in (feed._thread, feed._search_thread):
        if t is not None:
            t.join(10)
    feed.start()
    try:
        loops = [t for t in threading.enumerate() if t.name == "live-news" and t.is_alive()]
        assert len(loops) == 1
    finally:
        feed.stop()


def test_opportunities_have_their_own_hour_and_old_signals_end_by_age(feed):
    sig = _confirm(feed, "NVDA", 0.05)
    with feed.db.tx() as cur:                                 # confirm on what would be the last news check
        cur.execute("UPDATE live_signals SET checks = ? WHERE id = ?", (len(L.CHECK_DELAYS) - 1, sig["id"]))
    (sig,) = _signal(feed, "NVDA")
    feed.check(sig, _quote(feed, "NVDA"), "open")
    (sig,) = _signal(feed, "NVDA")
    assert sig["state"] == "opportunity" and sig["next_check_at"] is not None and sig["opportunity_at"]
    feed.check(sig, _quote(feed, "NVDA"), "open")             # a check inside the hour keeps it going
    (sig,) = _signal(feed, "NVDA")
    assert sig["state"] == "opportunity" and sig["next_check_at"] is not None
    old = L.iso(L.now_utc() - timedelta(minutes=L.OPPORTUNITY_WATCH_MINUTES + 1))
    with feed.db.tx() as cur:
        cur.execute("UPDATE live_signals SET opportunity_at = ? WHERE id = ?", (old, sig["id"]))
    feed._expire()
    (sig,) = _signal(feed, "NVDA")
    assert sig["state"] == "done" and sig["next_check_at"] is None and "Nothing watches the stop" in sig["reason"]
    # A watched stock not judged in 90 minutes (overnight, or never a usable price) ends, unjudged.
    feed.ingest(L.make_item("Demo", "w1", ["KO"], "KO news", published=L.now_utc()))
    (ko,) = _signal(feed, "KO")
    feed.check(ko, _quote(feed, "KO"), "open")
    with feed.db.tx() as cur:
        cur.execute("UPDATE live_signals SET ref_at = ? WHERE symbol = 'KO'",
                    (L.iso(L.now_utc() - timedelta(minutes=L.WATCH_LIMIT_MINUTES + 1)),))
    feed._expire()
    (ko,) = _signal(feed, "KO")
    assert ko["state"] == "no_reaction" and ko["next_check_at"] is None


def test_a_check_that_lost_a_race_writes_nothing(feed):
    sig = _confirm(feed, "MSFT", 0.04)
    feed.check(sig, _quote(feed, "MSFT"), "open")             # the first check confirms
    stale_copy = sig                                           # a second check still holding the old row
    feed.check(stale_copy, _quote(feed, "MSFT"), "open")
    (now,) = _signal(feed, "MSFT")
    assert now["state"] == "opportunity" and now["checks"] == 1             # the stale second check wrote nothing


def test_each_opportunity_is_emailed_once_whatever_calls_it(tmp_path, monkeypatch):
    from tradeassist import mailer
    s = Settings(data_mode="demo", db_path=tmp_path / "t.db", live_news_email=True,
                 smtp_user="me@example.com", smtp_password="pw")
    svc = TradeAssistant(s, provider=DemoData(), db=Database(tmp_path / "t.db"))
    analyst = Analyst(svc)
    feed = L.LiveFeed(svc, Morning(svc, analyst, s.features), analyst)
    sent = []
    monkeypatch.setattr(mailer, "send", lambda settings, subject, text, html=None: sent.append(subject) or "me")
    sig = _confirm(feed, "AAPL", 0.05)
    feed.check(sig, _quote(feed, "AAPL"), "open")
    feed._notify(sig["id"])
    feed._notify(sig["id"])
    assert len(sent) == 1
    monkeypatch.setattr(mailer, "send", lambda *a, **k: (_ for _ in ()).throw(mailer.MailError("down")))
    sig2 = _confirm(feed, "KO", 0.05)
    feed.check(sig2, _quote(feed, "KO"), "open")
    (ko,) = _signal(feed, "KO")
    assert ko["emailed"] == 0                                  # a failed send does not count as sent
