"""News found by searching (2.11): the answer to "why is this stock moving?", for $0.

Two search sources, used on demand rather than streamed:

    Google News   the search feed behind news.google.com (RSS). Free, no key, no card; Google's terms allow
                  personal, non-commercial reading of its feeds. It has no published rate limit, so requests are
                  spaced out and the source backs off for 15 minutes after a refusal. On by default
                  (GOOGLE_NEWS=0 switches it off).
    Brave         the Brave Search API's news endpoint (BRAVE_API_KEY). Brave's free tier ended in February 2026:
                  a key now comes with $5 of credit a month (about 1,000 searches at $5 per 1,000) and a card that
                  is charged automatically past it. So the app keeps a hard cap of its own, BRAVE_MONTHLY_LIMIT
                  (900 by default) and BRAVE_DAILY_LIMIT (40), counted before each request, and Brave is asked
                  only when Google News found nothing, so the credit lasts.

A search engine indexes news minutes after it is published, not seconds: this is for knowing why a stock moved
(and that nothing was found when nothing was), not for being first. The fastest source is still RTPR.

Search results carry no ticker, so a result counts for a stock only when its headline or summary names the
company or (for a ticker that is not an everyday word) the ticker itself. Company names come from the SEC's
ticker list when SEC_USER_AGENT is set, else from the data source's quote, and are kept for 30 days.
"""

from __future__ import annotations

import email.utils
import hashlib
import html
import logging
import re
import threading
import time
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from typing import Any, Callable
from urllib.parse import urlencode

import requests

from .events import emit, redact
from .news import word_tone

log = logging.getLogger(__name__)

GOOGLE_NEWS_RSS = "https://news.google.com/rss/search"
BRAVE_NEWS = "https://api.search.brave.com/res/v1/news/search"
GOOGLE_GAP_SECONDS = 2.0          # between two Google News requests
GOOGLE_BACKOFF_SECONDS = 900      # after Google refuses (429, 503 or a block page)
BRAVE_GAP_SECONDS = 1.1           # Brave allows about one request a second on the smallest plans
NAME_DAYS = 30
MAX_RESULTS = 20
USER_AGENT = "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_0) TradeAssistant/2.11 (personal news reader)"

# Words that are also tickers: searched for only by the company's name, never by the ticker alone.
COMMON_WORDS = {"A", "AI", "ALL", "AN", "ARE", "BE", "BIG", "C", "CAN", "CAR", "CAT", "D", "DAY", "DE", "DO", "EAT",
                "F", "FAST", "FOR", "FUN", "GO", "GOOD", "HAS", "HE", "HOLD", "HOME", "IT", "K", "KEY", "L", "LOW",
                "M", "MA", "MAN", "MO", "NEW", "NOW", "O", "OK", "ON", "ONE", "OPEN", "OR", "OUT", "PAY", "PLAY", "R",
                "RE", "RUN", "SAVE", "SEE", "SO", "T", "TEAM", "TECH", "TRUE", "TWO", "UP", "V", "VERY", "WELL", "X",
                "YOU", "Z"}
NAME_SUFFIXES = re.compile(
    r"(,?\s+(inc\.?|incorporated|corp\.?|corporation|co\.?|company|companies|ltd\.?|limited|plc|n\.?v\.?|s\.?a\.?|"
    r"ag|se|holdings?|group|class [a-c]|cl [a-c]|common stock|ordinary shares|shares|adr|the|/[a-z]{2,4}/?|"
    r"\(the\)|\(new\)|new|de|hldgs?))+\s*$", re.I)
KEEP_SECOND_WORD = {"news", "booking", "block", "match", "gap", "global", "general", "united", "first"}
GENERIC_FIRST_WORDS = {"american", "general", "united", "first", "international", "national", "bank", "the", "global",
                       "new", "southern", "northern", "western", "eastern", "public", "capital", "texas",
                       "royal", "advanced", "applied", "consolidated", "digital", "energy"}


def clean_name(raw: str | None) -> str | None:
    """A company's name as news writes it: "NVIDIA Corp" -> "NVIDIA", "Coca-Cola Co (The)" -> "Coca-Cola"."""
    if not raw or not isinstance(raw, str):
        return None
    name = re.sub(r"\s*\((demo|the)\)\s*$", "", raw.strip(), flags=re.I)
    for _ in range(4):
        trimmed = NAME_SUFFIXES.sub("", name).strip(" ,.-")
        if trimmed == name:
            break
        name = trimmed
    name = re.sub(r"^the\s+", "", name, flags=re.I).strip()
    name = re.sub(r"\s+(&|and)$", "", name, flags=re.I).strip()
    if name.lower() in KEEP_SECOND_WORD:                 # "News Corp", not "News"
        words = re.sub(r"[,.]", "", re.sub(r"\s*\((demo|the)\)\s*$", "", raw.strip(), flags=re.I)).split()
        name = " ".join(words[:2]) if len(words) >= 2 else name
    return name if len(name) >= 2 else None


def name_patterns(symbol: str, name: str | None) -> list[re.Pattern]:
    """What a headline must contain to be about this stock: the company's name (and its first word when that word
    is distinctive, so "Alphabet" matches "Alphabet Inc"), or the ticker when it is not an everyday word."""
    pats: list[re.Pattern] = []

    def word(text: str) -> re.Pattern:
        # A one-word name must start with a capital ("Booking" the company, not "booking" a flight); the rest
        # of it in any case, since news writes "Nvidia" for "NVIDIA".
        if " " not in text and text[:1].isalpha():
            return re.compile(r"(?<![\w])" + re.escape(text[0].upper()) + "(?i:" + re.escape(text[1:]) + r")(?![\w])")
        return re.compile(r"(?<![\w])" + re.escape(text) + r"(?![\w])", re.I)
    if name:
        pats.append(word(name))
        first = name.split()[0].strip(",.")
        if len(first) >= 4 and first.lower() not in GENERIC_FIRST_WORDS | KEEP_SECOND_WORD \
                and first.lower() != name.lower():
            pats.append(word(first))
    sym = symbol.upper()
    if sym not in COMMON_WORDS and len(sym.replace(".", "")) >= 2:
        pats.append(re.compile(r"(?<![A-Za-z0-9])\$?" + re.escape(sym) + r"(?![A-Za-z0-9])"))     # case-sensitive
    return pats


def mentions(text: str, patterns: list[re.Pattern]) -> bool:
    return any(p.search(text or "") for p in patterns)


def _safe_xml(text: str) -> ET.Element | None:
    """Parse a feed, refusing anything with a document type or entity declarations (no entity tricks)."""
    head = text[:4096].lower()
    if "<!doctype" in head or "<!entity" in head:
        return None
    try:
        return ET.fromstring(text)
    except ET.ParseError:
        return None


def parse_google_news(text: str) -> list[dict[str, Any]]:
    """Items of a Google News RSS search feed: headline (without the trailing " - Publisher"), link, publisher,
    publication time. Items that do not read are skipped."""
    root = _safe_xml(text)
    if root is None:
        return []
    out = []
    for it in root.iter("item"):
        title = (it.findtext("title") or "").strip()
        link = (it.findtext("link") or "").strip()
        src = it.find("source")
        publisher = (src.text or "").strip() if src is not None and src.text else ""
        if publisher and title.endswith(f" - {publisher}"):
            title = title[: -len(publisher) - 3].strip()
        try:
            published = email.utils.parsedate_to_datetime(it.findtext("pubDate") or "")
            if published.tzinfo is None:
                published = published.replace(tzinfo=timezone.utc)
        except (TypeError, ValueError, IndexError):
            published = None
        if not title or not re.match(r"https?://", link, re.I):
            continue
        desc = html.unescape(re.sub(r"<[^>]+>", " ", it.findtext("description") or ""))
        out.append({"title": title, "url": link, "publisher": publisher, "published": published,
                    "summary": " ".join(desc.split())[:600], "id": (it.findtext("guid") or link)[:200]})
    return out


def parse_brave(body: Any) -> list[dict[str, Any]]:
    """Results of Brave's news search: title, url, description, page_age (ISO, read as UTC when it has no zone)."""
    rows = body.get("results") if isinstance(body, dict) else None
    out = []
    for r in rows if isinstance(rows, list) else []:
        if not isinstance(r, dict) or not isinstance(r.get("title"), str) or not isinstance(r.get("url"), str):
            continue
        if not re.match(r"https?://", r["url"], re.I):
            continue
        published = None
        if isinstance(r.get("page_age"), str):
            try:
                published = datetime.fromisoformat(r["page_age"].replace("Z", "+00:00"))
                if published.tzinfo is None:
                    published = published.replace(tzinfo=timezone.utc)
            except ValueError:
                published = None
        meta = r.get("meta_url") if isinstance(r.get("meta_url"), dict) else {}
        profile = r.get("profile") if isinstance(r.get("profile"), dict) else {}
        publisher = profile.get("name") or meta.get("hostname") or ""
        desc = html.unescape(re.sub(r"<[^>]+>", " ", r.get("description") or ""))
        out.append({"title": html.unescape(re.sub(r"<[^>]+>", "", r["title"])).strip(), "url": r["url"],
                    "publisher": str(publisher)[:80], "published": published,
                    "summary": " ".join(desc.split())[:600], "id": r["url"][:200]})
    return out


def _month(now: datetime) -> str:
    return now.astimezone(timezone.utc).strftime("%Y-%m")


def _day(now: datetime) -> str:
    return now.astimezone(timezone.utc).strftime("%Y-%m-%d")


class SearchNews:
    """Searches news for one stock at a time. Shared by the Live news feed, the news panels and the Morning plan
    (one per app: `for_service`)."""

    def __init__(self, settings: Any, db: Any, *, sec: Any = None, quote: Callable[[str], dict] | None = None,
                 demo: bool = False, session: requests.Session | None = None):
        self.settings = settings
        self.db = db
        self.sec = sec
        self.quote = quote
        self.demo = demo
        self.session = session or requests.Session()
        self._lock = threading.Lock()
        self._google_next = 0.0
        self._google_blocked_until = 0.0
        self._brave_next = 0.0
        self.last_error: dict[str, Any] | None = None
        self.last_ok: dict[str, str] = {}
        self.brave_header: str | None = None

    # --- what is switched on --------------------------------------------------------------------------------
    @property
    def google_on(self) -> bool:
        return bool(getattr(self.settings, "google_news", True)) and not self.demo

    @property
    def brave_on(self) -> bool:
        return bool(getattr(self.settings, "brave_api_key", "")) and not self.demo

    @property
    def available(self) -> bool:
        return self.demo or self.google_on or self.brave_on

    def sources(self) -> list[str]:
        if self.demo:
            return ["Demo search"]
        return [n for n, on in (("Google News", self.google_on), ("Brave", self.brave_on)) if on]

    # --- company names --------------------------------------------------------------------------------------
    def company_name(self, symbol: str, hint: str | None = None) -> str | None:
        symbol = symbol.upper()
        key = f"names:{symbol}"
        try:
            hit = self.db.cache_get(key, NAME_DAYS * 86400)
        except Exception:                             # noqa: BLE001 - no cache: look it up
            hit = None
        if hit and hit.get("name"):
            return hit["name"]
        name = clean_name(hint)
        if not name and self.sec is not None and getattr(self.sec, "enabled", False):
            try:
                name = clean_name(self.sec.cik_for(symbol)[1])
            except Exception:                         # noqa: BLE001 - funds and foreign names are not listed
                name = None
        if not name and self.quote is not None:
            try:
                name = clean_name((self.quote(symbol) or {}).get("description"))
            except Exception:                         # noqa: BLE001 - no quote: search by ticker only
                name = None
        if name:
            try:
                self.db.cache_put(key, {"name": name})
            except Exception:                         # noqa: BLE001
                pass
        return name

    # --- Brave's budget -------------------------------------------------------------------------------------
    def brave_usage(self, now: datetime | None = None) -> dict[str, Any]:
        now = now or datetime.now(timezone.utc)
        month = (self.db.cache_get(f"brave:month:{_month(now)}") or {}).get("n", 0)
        day = (self.db.cache_get(f"brave:day:{_day(now)}") or {}).get("n", 0)
        return {"month": month, "month_limit": self.settings.brave_monthly_limit, "day": day,
                "day_limit": self.settings.brave_daily_limit, "header": self.brave_header}

    def _brave_claim(self, now: datetime) -> str | None:
        """Count one Brave request before it is sent; None when it may go, else why not."""
        with self._lock:
            u = self.brave_usage(now)
            if u["month"] >= u["month_limit"]:
                return (f"Brave's monthly limit ({u['month_limit']} searches, BRAVE_MONTHLY_LIMIT) is used up; it keeps "
                        "the $5 monthly credit from being exceeded")
            if u["day"] >= u["day_limit"]:
                return f"Brave's daily limit ({u['day_limit']} searches, BRAVE_DAILY_LIMIT) is used up"
            self.db.cache_put(f"brave:month:{_month(now)}", {"n": u["month"] + 1})
            self.db.cache_put(f"brave:day:{_day(now)}", {"n": u["day"] + 1})
            return None

    # --- the searches ---------------------------------------------------------------------------------------
    def _wait(self, attr: str, gap: float) -> None:
        with self._lock:
            wait = getattr(self, attr) - time.time()
            setattr(self, attr, max(time.time(), getattr(self, attr)) + gap)
        if wait > 0:
            time.sleep(min(wait, 30.0))

    def _fail(self, source: str, error: str) -> str:
        msg = redact(error)
        key = getattr(self.settings, "brave_api_key", "")
        if key:
            msg = msg.replace(key, "[redacted]")
        self.last_error = {"at": datetime.now(timezone.utc).isoformat(timespec="seconds"), "source": source,
                           "error": msg[:300]}
        emit("news_failed", logging.WARNING, what=f"search {source}", error=msg[:300])
        return msg

    def google(self, query: str, hours: int) -> list[dict[str, Any]]:
        if time.time() < self._google_blocked_until:
            left = int(self._google_blocked_until - time.time())
            raise RuntimeError(f"Google News refused requests; pausing it for {left // 60 + 1} more minutes")
        window = f"when:{max(1, min(int(hours), 100))}h" if hours <= 100 else f"when:{max(1, int(hours) // 24)}d"
        params = {"q": f"{query} {window}", "hl": "en-US", "gl": "US", "ceid": "US:en"}
        self._wait("_google_next", GOOGLE_GAP_SECONDS)
        try:
            r = self.session.get(f"{GOOGLE_NEWS_RSS}?{urlencode(params)}", headers={"User-Agent": USER_AGENT},
                                 timeout=(5, 15))
        except requests.RequestException as exc:
            raise RuntimeError(f"Could not reach Google News ({exc.__class__.__name__})") from None
        if r.status_code in (429, 503) or (r.status_code == 200 and "<rss" not in r.text[:2000].lower()):
            self._google_blocked_until = time.time() + GOOGLE_BACKOFF_SECONDS
            raise RuntimeError(f"Google News refused the request ({r.status_code}); pausing it for 15 minutes")
        if r.status_code != 200:
            raise RuntimeError(f"Google News returned {r.status_code}")
        self.last_ok["Google News"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
        return parse_google_news(r.text)

    def brave(self, query: str, hours: int) -> list[dict[str, Any]]:
        why = self._brave_claim(datetime.now(timezone.utc))
        if why:
            raise RuntimeError(why)
        self._wait("_brave_next", BRAVE_GAP_SECONDS)
        params = {"q": query, "count": MAX_RESULTS, "freshness": "pd" if hours <= 24 else "pw", "country": "US",
                  "search_lang": "en", "spellcheck": 0, "safesearch": "off"}
        try:
            r = self.session.get(BRAVE_NEWS, params=params, timeout=(5, 15),
                                 headers={"X-Subscription-Token": self.settings.brave_api_key,
                                          "Accept": "application/json"})
        except requests.RequestException as exc:
            raise RuntimeError(f"Could not reach Brave ({exc.__class__.__name__})") from None
        remaining = r.headers.get("X-RateLimit-Remaining") if hasattr(r, "headers") else None
        if remaining:
            self.brave_header = str(remaining)[:60]
        if r.status_code in (401, 403):
            raise RuntimeError("Brave refused the key: check BRAVE_API_KEY in .env (and that the plan includes news)")
        if r.status_code == 429:
            raise RuntimeError("Brave's rate limit or monthly credit was reached")
        if r.status_code != 200:
            raise RuntimeError(f"Brave returned {r.status_code}")
        try:
            body = r.json()
        except ValueError:
            raise RuntimeError("Brave sent a reply this app could not read") from None
        self.last_ok["Brave"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
        return parse_brave(body)

    def search(self, symbol: str, *, hours: int = 24, name_hint: str | None = None,
               brave: str = "fallback", have: int = 0) -> dict[str, Any]:
        """Recent news about one stock. `brave`: "never", "fallback" (only when nothing was found: not by Google
        News, and not the `have` items the caller already found elsewhere) or "always". Returns {"items",
        "checked", "errors", "name"}; items are the shared headline shape."""
        symbol = symbol.upper()
        name = self.company_name(symbol, name_hint)
        pats = name_patterns(symbol, name)
        if self.demo:
            return self._demo(symbol, name, hours)
        if not pats:
            return {"items": [], "checked": [], "name": name,
                    "errors": [f"No company name is known for {symbol}, and its ticker is an everyday word, so news "
                               "about it cannot be told apart from other news. Set SEC_USER_AGENT for names."]}
        if name:
            query = f'"{name}"' + (f' OR "{symbol}"' if symbol not in COMMON_WORDS and len(symbol) >= 3 else "")
        else:
            query = f'"{symbol}" stock'
        checked, errors, found = [], [], []
        cutoff = datetime.now(timezone.utc) - timedelta(hours=hours)

        def keep(rows: list[dict[str, Any]], via: str) -> None:
            for row in rows:
                if row["published"] is not None and row["published"] < cutoff:
                    continue
                if not mentions(f"{row['title']} {row['summary']}", pats):
                    continue
                found.append({**row, "via": via})
        if self.google_on:
            try:
                keep(self.google(query, hours), "Google News")
                checked.append("Google News")
            except RuntimeError as exc:
                errors.append(self._fail("Google News", str(exc)))
        if self.brave_on and (brave == "always" or (brave == "fallback" and not found and not have)):
            try:
                keep(self.brave(f"{name or symbol} stock", hours), "Brave")
                checked.append("Brave")
            except RuntimeError as exc:
                errors.append(self._fail("Brave", str(exc)))
        seen, items = set(), []
        for row in sorted(found, key=lambda r: r["published"] or datetime.min.replace(tzinfo=timezone.utc),
                          reverse=True):
            k = re.sub(r"\W+", " ", row["title"].lower()).strip()[:90]
            if k in seen:
                continue
            seen.add(k)
            items.append(self._as_item(symbol, row))
        return {"items": items[:MAX_RESULTS], "checked": checked, "errors": errors, "name": name}

    @staticmethod
    def _as_item(symbol: str, row: dict[str, Any]) -> dict[str, Any]:
        ident = hashlib.sha1(f"{row['via']}|{row['id']}".encode()).hexdigest()[:16]
        return {"id": ident, "headline": row["title"][:300], "summary": row["summary"], "url": row["url"],
                "source": row["publisher"] or row["via"], "via": row["via"],
                "published": row["published"].isoformat(timespec="seconds") if row["published"] else None,
                "kind": "news", "related": symbol, "tone": word_tone(row["title"]), "tone_method": "words"}

    def _demo(self, symbol: str, name: str | None, hours: int) -> dict[str, Any]:
        """Made-up results on demo data, the same for a stock and day: two stocks in three have news."""
        now = datetime.now(timezone.utc)
        seed = int(hashlib.sha256(f"{symbol}|{now.date()}".encode()).hexdigest()[:6], 16)
        if seed % 3 == 0:
            return {"items": [], "checked": ["Demo search"], "errors": [], "name": name}
        head = f"{name or symbol} shares move after an analyst note (made up)"
        row = {"title": head, "url": f"https://example.com/demo/{symbol.lower()}", "publisher": "Demo",
               "published": now - timedelta(minutes=20 + seed % 90), "summary": "A demo search result.",
               "id": f"{symbol}-{now.date()}", "via": "Demo search"}
        return {"items": [self._as_item(symbol, row)], "checked": ["Demo search"], "errors": [], "name": name}

    def status(self) -> dict[str, Any]:
        out: dict[str, Any] = {"sources": self.sources(), "google_news": self.google_on, "brave": self.brave_on,
                               "last_ok": dict(self.last_ok), "last_error": self.last_error}
        if self.brave_on:
            try:
                out["brave_usage"] = self.brave_usage()
            except Exception:                         # noqa: BLE001
                out["brave_usage"] = None
        if time.time() < self._google_blocked_until:
            out["google_paused_until"] = datetime.fromtimestamp(self._google_blocked_until, timezone.utc).isoformat(
                timespec="seconds")
        return out


def for_service(svc: Any) -> SearchNews:
    """The app's one SearchNews, made on first use."""
    found = getattr(svc, "search_news", None)
    if found is None:
        def quote(symbol: str) -> dict:
            return svc.market.quote(symbol)
        found = SearchNews(svc.settings, svc.db, sec=getattr(svc, "sec", None), quote=quote,
                           demo=svc.market.info.mode == "demo")
        svc.search_news = found
    return found


__all__ = ["SearchNews", "for_service", "parse_google_news", "parse_brave", "clean_name", "name_patterns",
           "mentions"]
