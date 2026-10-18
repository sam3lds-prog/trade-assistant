"""News found by searching (2.11): Google News search feeds and capped Brave, the company-name matching, the
news client and Morning plan check, and the Live news "why is it moving?" scan."""

from __future__ import annotations

import time
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from tradeassist import livenews as L
from tradeassist import searchnews as S
from tradeassist.analysis import Analyst
from tradeassist.config import Settings
from tradeassist.db import Database
from tradeassist.morning import Morning
from tradeassist.news import NewsClient
from tradeassist.providers.demo import DemoData
from tradeassist.service import TradeAssistant


def rss(items: list[tuple[str, str, str, datetime]]) -> str:
    """A Google News search feed: (headline, publisher, link, published)."""
    body = "".join(
        f"<item><title>{t} - {p}</title><link>{u}</link><guid isPermaLink=\"false\">{u}</guid>"
        f"<pubDate>{d.strftime('%a, %d %b %Y %H:%M:%S GMT')}</pubDate><description>&lt;a href=\"x\"&gt;{t}&lt;/a&gt;"
        f"</description><source url=\"https://{p.lower()}.com\">{p}</source></item>" for t, p, u, d in items)
    return f'<?xml version="1.0" encoding="UTF-8"?><rss version="2.0"><channel><title>x</title>{body}</channel></rss>'


class FakeSession:
    """Answers Google News with `google` (text or (status, text)) and Brave with `brave` (json or status)."""

    def __init__(self, google=None, brave=None):
        self.google, self.brave, self.calls = google, brave, []

    def get(self, url, params=None, headers=None, timeout=None):
        self.calls.append((url, params, headers or {}))
        if "news.google.com" in url:
            status, text = self.google if isinstance(self.google, tuple) else (200, self.google or rss([]))
            return SimpleNamespace(status_code=status, text=text, headers={})
        status, body = self.brave if isinstance(self.brave, tuple) else (200, self.brave or {"results": []})
        return SimpleNamespace(status_code=status, text="", json=lambda: body, headers={"X-RateLimit-Remaining": "0, 899"})

    def hosts(self):
        return ["google" if "google" in u else "brave" for u, _, _ in self.calls]


def make(tmp_path, session, **kw):
    s = Settings(data_mode="demo", db_path=tmp_path / "t.db", **kw)
    db = Database(tmp_path / "t.db")
    search = S.SearchNews(s, db, session=session, quote=lambda sym: {"description": {"NVDA": "NVIDIA Corp",
                                                                                    "V": "Visa Inc."}.get(sym, "")})
    search._wait = lambda attr, gap: None                    # no pacing in tests
    return search, db


NOW = datetime.now(timezone.utc)


# --- formats and matching ------------------------------------------------------------------------------------

def test_google_news_feed_is_read_and_the_publisher_taken_off_the_headline():
    rows = S.parse_google_news(rss([("Nvidia unveils a new chip", "Reuters", "https://news.google.com/rss/articles/a",
                                     NOW - timedelta(minutes=30)),
                                    ("Bad link", "X", "javascript:alert(1)", NOW)]))
    assert len(rows) == 1
    r = rows[0]
    assert r["title"] == "Nvidia unveils a new chip" and r["publisher"] == "Reuters"
    assert r["published"].tzinfo is not None and abs((r["published"] - (NOW - timedelta(minutes=30))).total_seconds()) < 2
    assert S.parse_google_news("<!DOCTYPE x [<!ENTITY a 'b'>]><rss/>") == [] and S.parse_google_news("not xml") == []


def test_brave_results_are_read_with_their_times():
    body = {"results": [{"title": "<strong>Nvidia</strong> soars", "url": "https://x.test/a", "page_age": "2026-10-16T14:30:00",
                         "description": "Shares &amp; more", "meta_url": {"hostname": "x.test"}},
                        {"title": "no url"}, "junk"]}
    (r,) = S.parse_brave(body)
    assert r["title"] == "Nvidia soars" and r["summary"] == "Shares & more" and r["publisher"] == "x.test"
    assert r["published"] == datetime(2026, 10, 16, 14, 30, tzinfo=timezone.utc)
    assert S.parse_brave(None) == []


def test_company_names_and_what_counts_as_a_mention():
    assert S.clean_name("NVIDIA Corp") == "NVIDIA" and S.clean_name("Coca-Cola Co (The)") == "Coca-Cola"
    assert S.clean_name("JPMorgan Chase & Co") == "JPMorgan Chase" and S.clean_name("News Corp") == "News Corp"
    assert S.clean_name("Alphabet Inc. Class A") == "Alphabet" and S.clean_name("") is None
    visa = S.name_patterns("V", "Visa")                   # an everyday-word ticker: by name only
    assert S.mentions("Visa beats estimates", visa) and not S.mentions("V shares jump", visa)
    nv = S.name_patterns("NVDA", "NVIDIA")
    assert S.mentions("Nvidia unveils", nv) and S.mentions("Why NVDA is up", nv) and not S.mentions("nvidia lower", nv)
    booking = S.name_patterns("BKNG", "Booking Holdings")
    assert not S.mentions("booking a flight", booking) and S.mentions("Booking Holdings beats", booking)
    assert not S.mentions("Booking a flight? Fares rise", booking)      # an ambiguous first word alone is not enough


# --- the searches --------------------------------------------------------------------------------------------

def test_google_results_are_kept_only_when_recent_and_about_the_stock(tmp_path):
    feed = rss([("Nvidia unveils a new chip", "Reuters", "https://news.google.com/a", NOW - timedelta(hours=1)),
                ("Old Nvidia story", "AP", "https://news.google.com/b", NOW - timedelta(hours=30)),
                ("Markets drift as yields rise", "CNBC", "https://news.google.com/c", NOW - timedelta(minutes=5))])
    session = FakeSession(google=feed)
    search, _ = make(tmp_path, session)
    search.demo = False
    got = search.search("NVDA", hours=18)
    assert [i["headline"] for i in got["items"]] == ["Nvidia unveils a new chip"]
    assert got["checked"] == ["Google News"] and got["name"] == "NVIDIA"
    q = session.calls[0][0]
    assert "when%3A18h" in q and "%22NVIDIA%22" in q                     # by name, last 18 hours


def test_brave_is_asked_only_when_nothing_was_found_and_never_past_its_caps(tmp_path):
    hit = {"results": [{"title": "Nvidia jumps on deal", "url": "https://x.test/a", "page_age": NOW.isoformat()}]}
    session = FakeSession(google=rss([]), brave=hit)
    search, db = make(tmp_path, session, brave_api_key="secret-brave", brave_monthly_limit=2, brave_daily_limit=5)
    search.demo = False
    assert search.search("NVDA", brave="never")["items"] == [] and session.hosts() == ["google"]
    got = search.search("NVDA", brave="fallback")
    assert got["checked"] == ["Google News", "Brave"] and got["items"][0]["via"] == "Brave"
    assert session.calls[-1][2]["X-Subscription-Token"] == "secret-brave"
    assert search.search("NVDA", brave="fallback", have=3)["checked"] == ["Google News"]   # found elsewhere already
    search.search("NVDA", brave="always")                                                   # the second of two
    n = len(session.calls)
    over = search.search("NVDA", brave="always")
    assert len(session.calls) == n + 1 and session.hosts()[-1] == "google"                 # Brave not called
    assert any("monthly limit" in e for e in over["errors"]) and all("secret-brave" not in e for e in over["errors"])
    assert search.brave_usage()["month"] == 2


def test_google_refusing_pauses_it_and_says_so(tmp_path):
    session = FakeSession(google=(429, "slow down"))
    search, _ = make(tmp_path, session)
    search.demo = False
    first = search.search("NVDA")
    assert first["checked"] == [] and "pausing it for 15 minutes" in first["errors"][0]
    again = search.search("NVDA")
    assert len(session.calls) == 1 and "more minutes" in again["errors"][0]
    (tmp_path / "b").mkdir()
    consent, _ = make(tmp_path / "b", FakeSession(google=(200, "<html>Before you continue</html>")))
    assert "refused" in consent.search("NVDA")["errors"][0]          # a consent page, not a feed


def test_a_ticker_that_is_a_word_and_has_no_name_is_not_searched(tmp_path):
    session = FakeSession()
    search, _ = make(tmp_path, session)
    search.demo = False
    search.quote = lambda sym: {}
    got = search.search("ON")
    assert got["items"] == [] and session.calls == [] and "everyday word" in got["errors"][0]


# --- the news client and the Morning plan ----------------------------------------------------------------------

def test_the_news_client_adds_search_results_and_only_a_deep_check_asks_brave(tmp_path):
    hit = {"results": [{"title": "Nvidia jumps", "url": "https://x.test/a", "page_age": NOW.isoformat()}]}
    session = FakeSession(google=rss([("Nvidia wins contract", "AP", "https://news.google.com/a", NOW)]), brave=hit)
    search, db = make(tmp_path, session, brave_api_key="k")
    search.demo = False
    s = Settings(data_mode="live", live_token="t", db_path=tmp_path / "t.db", brave_api_key="k")
    client = NewsClient(s, db, None, search=search)
    assert client.searchable and not client.configured
    got = client.company("NVDA", days=1)
    assert [i["headline"] for i in got["items"]] == ["Nvidia wins contract"] and got["sources"] == ["Google News"]
    session.google = rss([])
    deep = client.company("NVDA", days=3, deep=True)
    assert deep["sources"] == ["Google News", "Brave"] and deep["items"][0]["via"] == "Brave"
    assert "Google News results are matched" in " ".join(got["notes"])


def test_the_morning_news_check_uses_search_when_nothing_else_is_set(tmp_path, monkeypatch):
    s = Settings(data_mode="demo", db_path=tmp_path / "t.db")
    svc = TradeAssistant(s, provider=DemoData(), db=Database(tmp_path / "t.db"))
    morning = Morning(svc, Analyst(svc), s.features)
    calls = []

    class Client:
        configured, searchable = False, True

        def company(self, symbol, days=7, deep=False):
            calls.append(deep)
            return {"items": [{"headline": "Acme news", "source": "AP", "published": NOW.isoformat(), "url": None}],
                    "sources": ["Google News"], "errors": []}
    morning._news = Client()
    monkeypatch.setattr(type(svc.market), "info", property(lambda self: SimpleNamespace(mode="live")))
    svc.sec = None
    state = morning._news_state("ACME")
    assert state["state"] == "news" and calls == [True]


# --- Live news: holdings, movers, duplicates -------------------------------------------------------------------

@pytest.fixture
def feed(tmp_path):
    s = Settings(data_mode="demo", db_path=tmp_path / "t.db", live_min_day_atr=1.0, live_min_day_pct=3.0)
    svc = TradeAssistant(s, provider=DemoData(), db=Database(tmp_path / "t.db"))
    analyst = Analyst(svc)
    f = L.LiveFeed(svc, Morning(svc, analyst, s.features), analyst)
    import tradeassist.morning as morning_mod
    f._universe = (time.time(), set(morning_mod.TOP_200[:80]))
    return f


def test_movers_get_a_search_and_news_found_becomes_a_signal(feed):
    feed._scan_movers()
    movers = feed.movers()
    assert movers, "the demo has stocks that moved 3% and a daily range"
    assert all(abs(m["move_pct"]) >= 3 and abs(m["ranges"]) >= 1.0 for m in movers)
    searched = [m for m in movers if m["checked"]]
    assert 0 < len(searched) <= L.MOVER_LOOKUPS_PER_SCAN
    with_news = [m for m in searched if m["found"]]
    without = [m for m in searched if m["found"] is False]
    assert with_news and all(m["headline"] for m in with_news)
    syms = {r["symbol"] for r in feed.db.query("SELECT symbol FROM live_signals")}
    assert {m["symbol"] for m in with_news} <= syms and not ({m["symbol"] for m in without} & syms)
    # A second scan does not search the same movers again within the hour.
    checked_at = {m["symbol"]: m["checked_at"] for m in searched}
    feed._scan_movers()
    assert all(m["checked_at"] == checked_at[m["symbol"]] for m in feed.movers() if m["symbol"] in checked_at)
    snap = feed.snapshot()
    assert snap["movers"] and "search" in snap and snap["mover_scan_minutes"] == 5.0


def test_the_same_headline_from_two_sources_is_kept_once(feed):
    a = L.make_item("Finnhub", "1", ["NVDA"], "Nvidia wins a contract", published=L.now_utc())
    b = L.make_item("Google News", "x9", ["NVDA"], "Nvidia wins a contract", published=L.now_utc())
    c = L.make_item("Google News", "x10", ["AMD"], "Nvidia wins a contract", published=L.now_utc())
    assert feed.ingest(a) is not None and feed.ingest(b) is None and feed.ingest(c) is not None


def test_google_news_reads_your_holdings_first(tmp_path):
    s = Settings(data_mode="demo", db_path=tmp_path / "t.db", watchlist=["AAPL", "MSFT"])
    svc = TradeAssistant(s, provider=DemoData(), db=Database(tmp_path / "t.db"))
    analyst = Analyst(svc)
    f = L.LiveFeed(svc, Morning(svc, analyst, s.features), analyst)
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    with svc.db.tx() as cur:
        cur.execute("""INSERT INTO positions (symbol, instrument_type, direction, status, quantity, multiplier,
                       created_at, updated_at) VALUES ('NVDA', 'STOCK', 'LONG', 'OPEN', 5, 1, ?, ?)""", (now, now))
    asked = []
    svc.search_news = SimpleNamespace(google_on=True, brave_on=False, available=True,
                                      search=lambda sym, **kw: asked.append(sym) or {"items": [], "checked": ["Google News"],
                                                                                      "errors": []},
                                      status=lambda: {}, sources=lambda: ["Google News"])
    assert "Google News" in f.source_names()
    f._google_rotation()
    assert asked == ["NVDA", "AAPL", "MSFT"]


def test_settings_read_the_search_options(monkeypatch, tmp_path):
    from tradeassist.config import load_settings
    for k, v in {"GOOGLE_NEWS": "0", "BRAVE_API_KEY": "bk", "BRAVE_MONTHLY_LIMIT": "500", "BRAVE_DAILY_LIMIT": "10",
                 "LIVE_MOVER_SWEEP_MINUTES": "0"}.items():
        monkeypatch.setenv(k, v)
    s = load_settings(tmp_path / "none.env")
    assert (s.google_news, s.brave_api_key, s.brave_monthly_limit, s.brave_daily_limit, s.live_mover_sweep_minutes) \
        == (False, "bk", 500, 10, 0.0)
    assert "bk" not in repr(s)
