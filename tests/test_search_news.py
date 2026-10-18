"""News found by searching (2.11): Google News search feeds and capped Brave, the company-name matching, the
news client and Morning plan check, and the Live news "why is it moving?" scan."""

from __future__ import annotations

import threading
import time
from datetime import date, datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from tradeassist import livenews as L
from tradeassist import searchnews as S
from tradeassist.analysis import Analyst
from tradeassist.config import Settings
from tradeassist.db import Database
from tradeassist.morning import NY, Morning
from tradeassist.news import NewsClient
from tradeassist.providers.demo import DemoData
from tradeassist.service import TradeAssistant

NOW = datetime.now(timezone.utc)


def rss(items: list[tuple[str, str, str, datetime]]) -> str:
    """A Google News search feed: (headline, publisher, link, published), with Google's own description (the
    headline as a link, then the publisher in a <font>)."""
    body = "".join(
        f"<item><title>{t} - {p}</title><link>{u}</link><guid isPermaLink=\"false\">{u}</guid>"
        f"<pubDate>{d.strftime('%a, %d %b %Y %H:%M:%S GMT')}</pubDate><description>&lt;a href=\"{u}\"&gt;{t}&lt;/a&gt;"
        f"&amp;nbsp;&amp;nbsp;&lt;font color=\"#6f6f6f\"&gt;{p}&lt;/font&gt;</description>"
        f"<source url=\"https://{p.lower().replace(' ', '')}.com\">{p}</source></item>" for t, p, u, d in items)
    return f'<?xml version="1.0" encoding="UTF-8"?><rss version="2.0"><channel><title>x</title>{body}</channel></rss>'


class FakeSession:
    """Answers Google News with `google` (text or (status, text)) and Brave with `brave` (json or (status, json))."""

    def __init__(self, google=None, brave=None):
        self.google, self.brave, self.calls = google, brave, []

    def get(self, url, params=None, headers=None, timeout=None, allow_redirects=True):
        self.calls.append((url, params, headers or {}, allow_redirects))
        if "news.google.com" in url:
            status, text = self.google if isinstance(self.google, tuple) else (200, self.google or rss([]))
            return SimpleNamespace(status_code=status, text=text, headers={})
        status, body = self.brave if isinstance(self.brave, tuple) else (200, self.brave or {"results": []})
        return SimpleNamespace(status_code=status, text="", json=lambda: body, headers={"X-RateLimit-Remaining": "0, 899"})

    def hosts(self):
        return ["google" if "google" in c[0] else "brave" for c in self.calls]


def make(path, session, **kw):
    s = Settings(data_mode="demo", db_path=path / "t.db", **kw)
    db = Database(path / "t.db")
    search = S.SearchNews(s, db, session=session,
                          quote=lambda sym: {"description": {"ACME": "Acme Widgets Corp"}.get(sym, "")})
    search._google_turn = lambda: None                 # no spacing in tests (tested on its own below)
    return search, db


# --- formats ---------------------------------------------------------------------------------------------------

def test_google_news_feed_is_read_and_anything_odd_in_it_is_refused():
    rows = S.parse_google_news(rss([("Nvidia unveils a new chip", "Reuters", "https://news.google.com/rss/articles/a",
                                     NOW - timedelta(minutes=30)),
                                    ("Bad link", "X", "javascript:alert(1)", NOW)]))
    assert len(rows) == 1
    r = rows[0]
    assert r["title"] == "Nvidia unveils a new chip" and r["publisher"] == "Reuters"
    assert abs((r["published"] - (NOW - timedelta(minutes=30))).total_seconds()) < 2
    for bad in ("<!DOCTYPE x [<!ENTITY a 'b'>]><rss/>",
                "<!--" + "x" * 5000 + "--><!DOCTYPE r [<!ENTITY e 'boom'>]><rss><channel><item><title>&e;</title>"
                "</item></channel></rss>",                         # a declaration past the first 4,096 characters
                "not xml", "<html><body>Before you continue</body></html>"):
        with pytest.raises(ValueError):
            S.parse_google_news(bad)


def test_brave_results_are_read_with_their_times():
    body = {"results": [{"title": "<strong>Nvidia</strong> soars", "url": "https://x.test/a",
                         "page_age": "2026-10-16T14:30:00", "description": "Shares &amp; more",
                         "meta_url": {"hostname": "x.test"}},
                        {"title": "no url"}, "junk", {"title": "js", "url": "javascript:x"}]}
    (r,) = S.parse_brave(body)
    assert r["title"] == "Nvidia soars" and r["summary"] == "Shares & more" and r["publisher"] == "x.test"
    assert r["published"] == datetime(2026, 10, 16, 14, 30, tzinfo=timezone.utc)
    assert S.parse_brave(None) == []


# --- matching a headline to a stock ------------------------------------------------------------------------------

@pytest.mark.parametrize("symbol,raw,headline,expected", [
    ("DIS", "WALT DISNEY CO", "Disney shares jump after earnings", True),
    ("GOOGL", "Alphabet Inc. Class A", "Google unveils a new AI model", True),
    ("LLY", "ELI LILLY & Co", "Lilly shares climb on obesity data", True),
    ("MCD", "MCDONALDS CORP", "McDonald's sales rise", True),
    ("C", "CITIGROUP INC", "Citi beats estimates", True),
    ("BAC", "BANK OF AMERICA CORP", "BofA beats on trading", True),
    ("PG", "PROCTER & GAMBLE Co", "P&G beats on price increases", True),
    ("ON", "ON SEMICONDUCTOR CORP", "Onsemi shares tumble", True),
    ("BRK.B", "BERKSHIRE HATHAWAY", "Why BRK-B is up today", True),
    ("EBAY", "EBAY INC", "eBay raises its outlook", True),
    ("GE", "GE Aerospace", "GE raises full-year outlook", True),
    ("GE", "GE Aerospace", "GE Vernova jumps on orders", False),
    ("GEV", "GE Vernova", "GE Vernova jumps on orders", True),
    ("TGT", "TARGET CORP", "Target Shares Fall After Weak Forecast", True),
    ("TGT", "TARGET CORP", "Missiles target bases in the region", False),
    ("F", "FORD MOTOR CO", "Ford shares fall on a recall", True),
    ("F", "FORD MOTOR CO", "Harrison Ford stars in a new film", False),
    ("V", "VISA INC", "Trump Suspends Student Visa Interviews", False),
    ("V", "VISA INC", "Visa Shares Rise After Earnings", True),
    ("PM", "PHILIP MORRIS", "UK PM Starmer visits Washington", False),
    ("PM", "PHILIP MORRIS", "$PM rallies on smoke-free sales", True),
    ("MS", "MORGAN STANLEY", "J.P. Morgan says rates will fall", False),
    ("JPM", "JPMORGAN CHASE & CO", "J.P. Morgan says rates will fall", True),
    ("KO", "COCA COLA CO", "Wins by KO in round three", False),
    ("HD", "HOME DEPOT", "New HD TV deals this week", False),
    ("SO", "SOUTHERN CO", "Southern California wildfire spreads", False),
    ("SCHW", "SCHWAB CHARLES CORP", "King Charles visits Canada", False),
    ("DG", "DOLLAR GENERAL", "Dollar rises against the yen", False),
    ("MPC", "MARATHON PETROLEUM", "Boston Marathon winner named", False),
    ("AMZN", "AMAZON COM INC", "Amazon rainforest fires spread", False),
    ("AMZN", "AMAZON COM INC", "Amazon to cut 10,000 jobs", True),
    ("RTX", "RTX Corp", "Nvidia RTX 5090 review", False),
    ("ICE", "INTERCONTINENTAL EXCHANGE", "ICE raids in Chicago", False),
    ("NVDA", "NVIDIA CORP", "nvidia lower", False),
    ("NVDA", "NVIDIA CORP", "Stocks to watch (NASDAQ:NVDA)", True),
    # Names the app's list does not have: the SEC's name, cleaned.
    ("WFCX", "WELLS FARGO & COMPANY/MN", "Wells Fargo beats", True),
    ("PGRX", "PROGRESSIVE CORP/OH/", "Progressive beats estimates", True),
    ("PGRX", "PROGRESSIVE CORP/OH/", "Progressive Era politics, explained", False),
    ("USBX", "US BANCORP \\DE\\", "US Bancorp profit rises", True),
    ("GAPX", "Gap Inc", "Gap shares soar on sales", True),
    ("GAPX", "Gap Inc", "Mind the gap: a guide", False),
    ("XYZ", "Block, Inc.", "Block shares surge after earnings", True),
    ("XYZ", "Block, Inc.", "Lawmakers block the bill", False),
    ("TSMX", "TAIWAN SEMICONDUCTOR MANUFACTURING CO LTD", "Taiwan Semiconductor beats", True),
    ("TSMX", "TAIWAN SEMICONDUCTOR MANUFACTURING CO LTD", "Taiwan holds drills", False),
])
def test_a_headline_counts_for_a_stock_only_when_it_names_the_company(symbol, raw, headline, expected):
    assert S.mentions(headline, S.name_patterns(symbol, S.clean_name(raw))) is expected


def test_company_names_are_cleaned_and_demo_names_never_used():
    assert S.clean_name("NVIDIA Corp") == "NVIDIA" and S.clean_name("Coca-Cola Co (The)") == "Coca-Cola"
    assert S.clean_name("WELLS FARGO & COMPANY/MN") == "WELLS FARGO" and S.clean_name("US BANCORP \\DE\\") == "US BANCORP"
    assert S.clean_name("Alphabet Inc. Class A") == "Alphabet" and S.clean_name("AAPL (demo)") is None
    assert S.clean_name("") is None


def test_a_demo_quote_never_leaves_a_name_behind_for_live_data(tmp_path):
    db = Database(tmp_path / "t.db")
    demo = S.SearchNews(Settings(data_mode="demo", db_path=tmp_path / "t.db"), db, demo=True,
                        quote=lambda sym: {"description": f"{sym} (demo)"})
    assert demo.search("ACMX", name_hint="ACMX (demo)")["checked"] == ["Demo search"]
    assert demo.company_name("ACMX", "ACMX (demo)") is None
    assert db.cache_get("names:ACMX") is None
    live = S.SearchNews(Settings(data_mode="demo", db_path=tmp_path / "t.db"), db,
                        quote=lambda sym: {"description": "ACMX"})                # a quote whose name is its ticker
    assert live.company_name("ACMX") is None


# --- the searches ---------------------------------------------------------------------------------------------

def test_google_results_count_only_when_recent_and_the_headline_names_the_stock(tmp_path):
    feed = rss([("Acme Widgets wins a contract", "Reuters", "https://news.google.com/a", NOW - timedelta(hours=1)),
                ("Old Acme Widgets story", "AP", "https://news.google.com/b", NOW - timedelta(hours=30)),
                ("Markets drift as yields rise", "Acme Widgets Daily", "https://news.google.com/c",
                 NOW - timedelta(minutes=5))])                       # the publisher's name is not the stock's news
    session = FakeSession(google=feed)
    search, _ = make(tmp_path, session)
    got = search.search("ACME", hours=18)
    assert [i["headline"] for i in got["items"]] == ["Acme Widgets wins a contract"]
    assert got["checked"] == ["Google News"] and got["unmatched"] == 1 and got["errors"] == []
    q = session.calls[0][0]
    assert "when%3A18h" in q and "%22Acme+Widgets%22" in q
    search.search("ACME", hours=72)
    assert "when%3A3d" in session.calls[-1][0]                         # beyond a day, Google's window is in days


def test_brave_is_a_capped_fallback_and_a_cap_is_not_a_failure(tmp_path):
    hit = {"results": [{"title": "Acme Widgets jumps on a deal", "url": "https://x.test/a", "page_age": NOW.isoformat()}]}
    session = FakeSession(google=rss([]), brave=hit)
    search, db = make(tmp_path, session, brave_api_key="secret-brave", brave_monthly_limit=3, brave_daily_limit=5)
    assert search.search("ACME", brave="never")["items"] == [] and session.hosts() == ["google"]
    got = search.search("ACME", brave="fallback")
    assert got["checked"] == ["Google News", "Brave"] and got["items"][0]["via"] == "Brave"
    assert session.calls[-1][2]["X-Subscription-Token"] == "secret-brave"
    assert session.calls[-1][3] is False                                                   # no redirects followed
    assert search.search("ACME", brave="fallback", have=3)["checked"] == ["Google News"]   # found elsewhere already
    # The window is any 31 days: a search 20 days ago, last month, counts against this one.
    with db.tx() as cur:
        S.SearchNews._put(cur, f"brave:day:{S._day(NOW - timedelta(days=20))}", {"n": 1})
    search.search("ACME", brave="always")                                                 # the third of three
    n = len(session.calls)
    over = search.search("ACME", brave="always")
    assert len(session.calls) == n + 1 and session.hosts()[-1] == "google"               # Brave not called
    assert over["errors"] == [] and "31 days" in over["skipped"][0] and over["checked"] == ["Google News"]
    assert search.brave_usage()["month"] == 3 and search.brave_usage()["day"] == 2
    assert all("secret-brave" not in str(v) for v in (over, search.status()))


def test_a_reserve_keeps_part_of_braves_day_for_the_morning_plan(tmp_path):
    session = FakeSession(google=rss([]), brave={"results": []})
    search, _ = make(tmp_path, session, brave_api_key="k", brave_daily_limit=4)
    for _ in range(2):
        assert "Brave" in search.search("ACME", brave="fallback", brave_reserve=2)["checked"]
    held = search.search("ACME", brave="fallback", brave_reserve=2)
    assert held["checked"] == ["Google News"] and "Morning plan" in held["skipped"][0]
    assert "Brave" in search.search("ACME", brave="fallback")["checked"]                 # the Morning plan's own


def test_brave_never_passes_its_limit_from_two_copies_of_the_app_at_once(tmp_path):
    session = FakeSession(google=rss([]), brave={"results": []})
    a, _ = make(tmp_path, session, brave_api_key="k", brave_monthly_limit=40, brave_daily_limit=1000)
    b, _ = make(tmp_path, session, brave_api_key="k", brave_monthly_limit=40, brave_daily_limit=1000)
    granted = []

    def claim(s):
        for _ in range(60):
            if s._brave_claim(datetime.now(timezone.utc)) is None:
                granted.append(1)
    threads = [threading.Thread(target=claim, args=(x,)) for x in (a, b)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(30)
    assert len(granted) == 40 and a.brave_usage()["month"] == 40


def test_google_refusing_pauses_it_for_every_copy_and_an_unreadable_feed_is_a_failure(tmp_path):
    session = FakeSession(google=(429, "slow down"))
    search, _ = make(tmp_path, session)
    first = search.search("ACME")
    assert first["checked"] == [] and "pausing it for 15 minutes" in first["errors"][0]
    other, _ = make(tmp_path, FakeSession(google=rss([])))           # another copy of the app, same database
    again = other.search("ACME")
    assert other.session.calls == [] and "paused" in again["errors"][0] and other.status()["google_paused_until"]
    assert other.last_error is None                                   # a pause is not logged as a new failure
    for i, reply in enumerate([(403, "forbidden"), (200, "<rss><channel><item><title>cut off")]):
        (tmp_path / f"c{i}").mkdir()
        s, _ = make(tmp_path / f"c{i}", FakeSession(google=reply))
        got = s.search("ACME")
        assert got["checked"] == [] and got["errors"], reply            # never "searched, nothing found"


def test_google_requests_wait_their_turn_and_a_turn_too_far_off_is_skipped(tmp_path, monkeypatch):
    monkeypatch.setattr(S, "GOOGLE_GAP_SECONDS", 0.3)
    db = Database(tmp_path / "t.db")
    a = S.SearchNews(Settings(data_mode="demo", db_path=tmp_path / "t.db"), db)
    b = S.SearchNews(Settings(data_mode="demo", db_path=tmp_path / "t.db"), Database(tmp_path / "t.db"))
    t0 = time.time()
    a._google_turn()
    b._google_turn()                                                  # the other copy waits for the gap
    assert time.time() - t0 >= 0.28
    db.cache_put("search:google_next", {"at": time.time() + 600})
    with pytest.raises(S.Refused):
        a._google_turn()


def test_a_ticker_that_is_a_word_and_has_no_name_is_not_searched(tmp_path):
    session = FakeSession()
    search, _ = make(tmp_path, session)
    search.quote = lambda sym: {}
    got = search.search("OK")
    assert got["items"] == [] and session.calls == [] and "mistaken for a word" in got["errors"][0]


# --- the news client and the Morning plan ------------------------------------------------------------------------

def test_the_news_client_adds_search_and_only_fresh_items_keep_the_deep_check_from_brave(tmp_path):
    hit = {"results": [{"title": "Acme Widgets jumps", "url": "https://x.test/a", "page_age": NOW.isoformat()}]}
    session = FakeSession(google=rss([("Acme Widgets wins a contract", "AP", "https://news.google.com/a", NOW)]),
                          brave=hit)
    search, db = make(tmp_path, session, brave_api_key="k")
    s = Settings(data_mode="live", live_token="t", db_path=tmp_path / "t.db", brave_api_key="k")
    client = NewsClient(s, db, None, search=search)
    assert client.searchable and not client.configured
    got = client.company("ACME", days=1)
    assert [i["headline"] for i in got["items"]] == ["Acme Widgets wins a contract"] and got["sources"] == ["Google News"]
    assert "Google News results are matched" in " ".join(got["notes"])
    session.google = rss([])
    old = {"headline": "10-Q: Acme Widgets", "source": "SEC EDGAR", "published": (NOW - timedelta(days=20)).isoformat(),
           "url": None, "kind": "filing", "tone": 0.0}
    client._filings = lambda symbol, days: [old]                      # a 20-day-old filing is not news behind a move
    deep = client.company("ACME", days=3, deep=True, fresh_hours=36)
    assert "Brave" in deep["sources"] and any(i.get("via") == "Brave" for i in deep["items"])


class _Client:
    configured, searchable = False, True

    def __init__(self, reply):
        self.reply, self.calls = reply, []

    def company(self, symbol, days=7, deep=False, fresh_hours=None):
        self.calls.append((deep, fresh_hours))
        return self.reply


@pytest.fixture
def morning(tmp_path, monkeypatch):
    s = Settings(data_mode="demo", db_path=tmp_path / "t.db")
    svc = TradeAssistant(s, provider=DemoData(), db=Database(tmp_path / "t.db"))
    m = Morning(svc, Analyst(svc), s.features)
    monkeypatch.setattr(type(svc.market), "info", property(lambda self: SimpleNamespace(mode="live")))
    svc.sec = None
    return m


@pytest.mark.parametrize("reply,state", [
    ({"items": [{"headline": "Acme news", "source": "AP", "published": NOW.isoformat(), "url": None}],
      "sources": ["Google News"], "errors": []}, "news"),
    ({"items": [], "sources": ["Google News"], "errors": [], "unmatched": 0}, "no_news"),
    ({"items": [], "sources": ["Google News"], "errors": [], "unmatched": 3}, "unchecked"),
    ({"items": [], "sources": [], "errors": ["Google News refused the request (429)"]}, "unchecked"),
    ({"items": [], "sources": ["Google News"], "errors": [], "skipped": ["Brave's daily limit is used up"]}, "no_news"),
])
def test_the_morning_news_check_never_reads_a_doubt_as_no_news(morning, reply, state):
    morning._news = _Client(reply)
    got = morning._news_state("ACME")
    assert got["state"] == state, got
    assert morning._news.calls == [(True, 36)]
    if state == "unchecked" and reply.get("unmatched"):
        assert "may be about ACME" in got["label"]


# --- Live news -------------------------------------------------------------------------------------------------

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
    feed._drain()                                                     # what the search thread found, ingested
    syms = {r["symbol"] for r in feed.db.query("SELECT symbol FROM live_signals")}
    assert {m["symbol"] for m in with_news} <= syms and not ({m["symbol"] for m in without} & syms)
    checked_at = {m["symbol"]: m["checked_at"] for m in searched}
    feed._scan_movers()                                               # not searched again within the hour
    assert all(m["checked_at"] == checked_at[m["symbol"]] for m in feed.movers() if m["symbol"] in checked_at)
    rows = feed.movers()
    rows[0]["move_pct"] = 999                                         # a copy: the feed's own rows are untouched
    assert feed.movers()[0]["move_pct"] != 999
    snap = feed.snapshot()
    assert snap["movers"] and "search" in snap and snap["mover_scan_minutes"] == 5.0


def test_the_movers_list_is_todays_and_brave_is_asked_once_a_stock_a_day(feed, monkeypatch):
    feed._movers, feed._movers_day = {"OLD": {"symbol": "OLD", "move_pct": 9.0, "found": True}}, "2000-01-01"
    assert feed.movers() == []
    calls = []

    def search(sym, **kw):
        calls.append((sym, kw["brave"], kw["brave_reserve"]))
        return {"items": [], "checked": ["Google News", "Brave"], "errors": []}
    feed.svc.search_news = SimpleNamespace(available=True, google_on=True, brave_on=True, google_paused=False,
                                           search=search, status=lambda: {}, sources=lambda: ["Google News", "Brave"])
    monkeypatch.setattr(L, "MOVER_RECHECK_MINUTES", 0)
    feed._scan_movers()
    first = {s for s, _, _ in calls}
    assert first and all(b == "fallback" and r == feed.settings.brave_daily_limit // 2 for _, b, r in calls)
    calls.clear()
    feed._scan_movers()
    assert calls and all(b == "never" for s, b, _ in calls if s in first)


def test_after_the_close_the_day_is_measured_from_todays_close(feed, monkeypatch):
    monkeypatch.setattr(type(feed.svc.market), "info", property(lambda self: SimpleNamespace(mode="live")))

    class At(datetime):
        moment = datetime(2026, 10, 14, 17, 0, tzinfo=NY)              # a Wednesday, after the close

        @classmethod
        def now(cls, tz=None):
            return cls.moment.astimezone(tz) if tz else cls.moment
    monkeypatch.setattr(L, "datetime", At)
    import tradeassist.morning as morning_mod
    monkeypatch.setattr(morning_mod, "datetime", At)
    wed, tue, mon = date(2026, 10, 14), date(2026, 10, 13), date(2026, 10, 12)
    q = {"prevclose": 100.0, "close": 106.0}
    assert feed._day_close(q, {"prev_close": 106.0, "last_date": wed}) == 106.0
    assert feed._day_close(q, {"prev_close": 100.0, "last_date": tue}) == 106.0       # bars not refreshed yet
    assert feed._day_close({"prevclose": 100.0}, {"prev_close": 100.0, "last_date": tue}) is None
    assert feed._day_close({"prevclose": 100.0, "official_close": 105.5}, None) == 105.5   # Robinhood's
    assert feed._quote_close(q) == 106.0
    # The case that read as a rally: closed at 106 from 100, news at 105, now 104.6 after hours.
    v = L.judge(ref=105.0, price=104.6, prev_close=feed._day_close(q, None), atr=2.0, late=True, settings=feed.settings)
    assert v["direction"] != "LONG"
    At.moment = datetime(2026, 10, 14, 10, 0, tzinfo=NY)                # the same day, before the close
    assert feed._day_close({"prevclose": 106.0}, {"prev_close": 106.0, "last_date": tue}) == 106.0
    assert feed._day_close({"prevclose": 106.0}, {"prev_close": 100.0, "last_date": mon}) == 106.0   # stale bars


def test_one_move_is_traded_once_a_day(feed):
    now = L.iso(L.now_utc())
    news_id = L.store_item(feed.db, L.make_item("Finnhub", "a1", ["NVDA"], "Nvidia wins a contract",
                                                published=L.now_utc()))
    with feed.db.tx() as cur:
        cur.execute("""INSERT INTO live_signals (symbol, news_id, news_ids, opened_at, state, updated_at, opportunity_at)
                       VALUES ('NVDA', ?, '[]', ?, 'done', ?, ?)""", (news_id, now, now, now))
    assert feed._opportunity_today("NVDA", 999) and not feed._opportunity_today("AMD", 999)


def test_the_same_headline_from_two_sources_is_kept_once_but_never_two_filings(feed):
    def item(source, ident, sym, head, kind="news"):
        return L.make_item(source, ident, [sym], head, published=L.now_utc(), kind=kind)
    assert feed.ingest(item("Finnhub", "1", "NVDA", "Nvidia wins a contract")) is not None
    assert feed.ingest(item("Google News", "x9", "NVDA", "Nvidia wins a contract")) is None       # another source
    assert feed.ingest(item("Google News", "x10", "AMD", "Nvidia wins a contract")) is not None   # another stock
    assert feed.ingest(item("Finnhub", "2", "NVDA", "Nvidia wins a contract")) is not None       # the same source
    head = "6-K: Taiwan Semiconductor Manufacturing Co Ltd"
    assert feed.ingest(item("SEC EDGAR", "acc1", "TSM", head, "filing")) is not None
    assert feed.ingest(item("SEC EDGAR", "acc2", "TSM", head, "filing")) is not None             # a second filing


def test_google_news_reads_your_holdings_first_and_waits_out_a_pause_quietly(tmp_path):
    s = Settings(data_mode="demo", db_path=tmp_path / "t.db", watchlist=["AAPL", "MSFT"])
    svc = TradeAssistant(s, provider=DemoData(), db=Database(tmp_path / "t.db"))
    analyst = Analyst(svc)
    f = L.LiveFeed(svc, Morning(svc, analyst, s.features), analyst)
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    with svc.db.tx() as cur:
        cur.execute("""INSERT INTO positions (symbol, instrument_type, direction, status, quantity, multiplier,
                       created_at, updated_at) VALUES ('NVDA', 'STOCK', 'LONG', 'OPEN', 5, 1, ?, ?)""", (now, now))
    asked = []
    svc.search_news = SimpleNamespace(google_on=True, brave_on=False, available=True, google_paused=False,
                                      search=lambda sym, **kw: asked.append(sym) or {"items": [],
                                                                                      "checked": ["Google News"],
                                                                                      "errors": []},
                                      status=lambda: {}, sources=lambda: ["Google News"])
    assert "Google News" in f.source_names()
    f._rotate()
    assert asked == ["NVDA", "AAPL", "MSFT"]
    svc.search_news.google_paused = True
    f._rotate()
    assert asked == ["NVDA", "AAPL", "MSFT"] and "Paused" in f.sources["Google News"]["note"]
    assert f.sources["Google News"]["last_error"] is None


def test_the_searches_run_on_their_own_thread(feed):
    import dataclasses
    feed.settings = dataclasses.replace(feed.settings, live_mover_sweep_minutes=0)
    feed.start(remember=False)
    try:
        assert feed._search_thread is not None and feed._search_thread.is_alive()
        assert feed._search_thread is not feed._thread
    finally:
        feed.stop(remember=False, wait=5)
    assert not feed.running


def test_settings_read_the_search_options(monkeypatch, tmp_path):
    from tradeassist.config import load_settings
    for k, v in {"GOOGLE_NEWS": "0", "BRAVE_API_KEY": "bk", "BRAVE_MONTHLY_LIMIT": "500", "BRAVE_DAILY_LIMIT": "10",
                 "LIVE_MOVER_SWEEP_MINUTES": "0"}.items():
        monkeypatch.setenv(k, v)
    s = load_settings(tmp_path / "none.env")
    assert (s.google_news, s.brave_api_key, s.brave_monthly_limit, s.brave_daily_limit, s.live_mover_sweep_minutes) \
        == (False, "bk", 500, 10, 0.0)
    assert "bk" not in repr(s)
