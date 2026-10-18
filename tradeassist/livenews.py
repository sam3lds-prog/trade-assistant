"""Live news and moves (2.10): stock-moving news as it arrives, what the price does next, and a trade card
when the move confirms.

**Where the news comes from.** Each source is used when it is set up, and several can run at once:

    RTPR       press releases from Business Wire, PR Newswire, GlobeNewswire and AccessWire, pushed over a
               WebSocket a fraction of a second after they cross the wire (RTPR_API_KEY, a paid plan at
               rtpr.io; the free plan is five minutes late). In RTPR's dashboard, create the "All articles"
               rule (or a rule for your stocks): the stream sends what your rules match.
    SEC EDGAR  8-K filings (earnings releases, deals, guidance, executive changes) from the SEC's list of
               current filings, read once a minute (SEC_USER_AGENT; free). Usually minutes behind the wire.
    Finnhub    headlines tagged with a ticker, read once a minute (FINNHUB_API_KEY; free key).
    Demo       made-up items for learning the screen, on demo data only.

**What happens to an item.** For each stock it names that is in the universe (by default the ~330 large
stocks the Morning plan reads, plus the watchlist and anything you hold; LIVE_UNIVERSE=all for every
ticker priced at $5 or more), the stock is quoted when the item arrives and again 1, 3, 5, 15, 30 and 60
minutes later. A **signal** confirms when the price has moved since the item by at least
LIVE_MIN_REACTION_PCT percent and LIVE_MIN_REACTION_ATR of the stock's daily ranges, with the day's move the
same way (path "reaction"); or, when the item reached the app two minutes or more after it was published,
when the day's move is at least LIVE_MIN_DAY_ATR ranges and LIVE_MIN_DAY_PCT percent (path "day_move").
The trade is the way the price went: moves with news have tended to keep drifting (Chan 2003; Savor
2012), which is the Morning plan's "ride" setup run in real time. The headline's tone is shown, and a tone
that disagrees with the price is flagged; the price is what is traded, not the words.

A confirmed signal gets the Morning plan's trade card: entry at the current price, a stop just beyond where
the stock stood before the news (or the chart's level), targets, shares sized to the risk per trade, and the
best call or put the budget pays for at its fair price (pricer.fair_value). The opportunity is checked
against its stop every ten minutes for an hour after it confirms: a price through the stop at one of those
checks marks it "stopped"; after the hour nothing watches it (set a watch or your own stop order). A stock
you hold gets an alert when the news moves it against you (sell or cover to consider) or with you. Signals
end by age (a watched stock after 90 minutes, an opportunity an hour after it confirmed), so nothing is
judged across a closed market.

**What it is not.** Large trading firms read the same wires with machines in microseconds; the first move on
a big release is usually over before any person can act. What this gives you is the news and the price's
answer to it within seconds, and a plan with the risk worked out. Advisory only: nothing here places an
order, and every opportunity is a setup with past frequencies behind it, not a forecast.
"""

from __future__ import annotations

import hashlib
import html
import json
import logging
import math
import queue
import re
import threading
import time
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from typing import Any, Callable
from urllib.parse import urlparse

import requests

from .events import emit, redact
from .news import word_tone
from .optionmath import NY
from .providers.base import DataError
from .research import ITEM_LABELS

log = logging.getLogger(__name__)
# The websockets package logs its handshake (the address, with the key in it) at debug level; never let it.
logging.getLogger("websockets").setLevel(logging.WARNING)

RTPR_API = "https://api.rtpr.io"
RTPR_WS = "wss://ws.rtpr.io/ws-alerts"
SEC_CURRENT = ("https://www.sec.gov/cgi-bin/browse-edgar?action=getcurrent&type=8-K&company=&dateb=&owner=include"
               "&start=0&count=40&output=atom")
FINNHUB = "https://finnhub.io/api/v1"

LOOP_SECONDS = 2.0
POLL_SECONDS = {"SEC EDGAR": 60.0, "Finnhub": 60.0, "RTPR": 300.0, "Demo": 35.0, "Google News": 300.0}
GOOGLE_NAMES_PER_POLL = 4        # holdings, then the watchlist, searched in turn: each held stock about every 5 min
MOVER_LOOKUPS_PER_SCAN = 6       # "why is it moving?" searches in one scan of the movers
MOVER_RECHECK_MINUTES = 60       # a mover with no news found is searched again after this long
MOVERS_KEPT = 40                 # movers listed on the screen
CHECK_DELAYS = (60, 120, 120, 600, 900, 1800)   # seconds between price checks: at 1, 3, 5, 15, 30, 60 minutes
MERGE_MINUTES = 30               # a later item on a stock within this long joins the open signal
STALE_HOURS = 18                 # an item this old when its stock can first be priced is listed, not traded
LATE_SECONDS = 120               # an item that reached the app this long after publication may follow the move
KEEP_DAYS = 7
MIN_PRICE = 5.0
MIN_DOLLAR_VOLUME = 20e6         # LIVE_UNIVERSE=all: average daily dollar volume a stock needs to be traded
QUOTE_FRESH_SECONDS = 15 * 60    # outside regular hours, a quote with no trade this recent is not a price
MAX_EMAILS_A_DAY = 20
FINNHUB_COMPANY_PER_POLL = 8     # watchlist names asked for company news each minute (free keys: 60 a minute)
LIVE_HORIZON = 5                 # trading days a news trade is given, as the Morning plan's ride
MAX_HEADLINE_JOBS = 60           # RTPR headlines being read at once; beyond it, items keep a plain headline
WATCH_LIMIT_MINUTES = 90         # a watched stock not judged by then (no usable price) ends as no reaction
OPPORTUNITY_CHECK_SECONDS = 600  # an opportunity is checked against its stop this often...
OPPORTUNITY_WATCH_MINUTES = 60   # ...for this long after it confirmed
DEMO_MOVE_SECONDS = 120.0        # demo only: a made-up item's price move plays out over this long
STOP_BEYOND_ATR = 0.25           # the stop: this many daily ranges beyond where the stock stood before the news
STOP_MIN_ATR, STOP_MAX_ATR = 0.5, 2.5

OPEN_STATES = ("open",)
EXTENDED_STATES = ("premarket", "pre", "postmarket", "post")
ACTIVE_STATES = ("waiting", "watching", "opportunity")


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def iso(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).isoformat(timespec="milliseconds")


def parse_time(value: Any) -> datetime | None:
    """An ISO time (a trailing Z, an offset, or none, read as UTC), or Unix seconds; None when unreadable."""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        if not math.isfinite(value) or value <= 0:
            return None
        try:
            return datetime.fromtimestamp(value / 1000 if value > 1e11 else value, timezone.utc)
        except (OverflowError, OSError, ValueError):
            return None
    try:
        out = datetime.fromisoformat(str(value).strip().replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    return out if out.tzinfo else out.replace(tzinfo=timezone.utc)


TICKER = re.compile(r"^[A-Z][A-Z0-9]{0,5}(\.[A-Z])?$")


def clean_symbols(values: Any) -> list[str]:
    """Tickers in the app's spelling (BRK.B), upper case, de-duplicated; anything that is not one is dropped."""
    if isinstance(values, str):
        values = re.split(r"[,\s;]+", values)
    out: list[str] = []
    for v in values if isinstance(values, (list, tuple)) else []:
        if not isinstance(v, str):
            continue
        s = v.strip().upper().replace("/", ".").replace("-", ".")
        if ":" in s:                                  # "NASDAQ:AAPL"
            s = s.split(":")[-1]
        if TICKER.match(s) and s not in out:
            out.append(s)
    return out


def tone_of(direction: Any, text: str) -> float:
    """+1, -1 or 0: a source's stated direction when it gives one, else the word list on the text."""
    if isinstance(direction, str):
        d = direction.strip().lower()
        if d in ("bullish", "positive", "up", "long"):
            return 1.0
        if d in ("bearish", "negative", "down", "short"):
            return -1.0
    return float(word_tone(text))


def make_item(source: str, external_id: str, symbols: list[str], headline: str, *, url: str | None = None,
              published: datetime | None = None, summary: str = "", kind: str = "news",
              extra: dict[str, Any] | None = None, tone: float | None = None) -> dict[str, Any]:
    headline = " ".join((headline or "").split())[:300] or "(no headline)"
    summary = " ".join((summary or "").split())[:600]
    if url and not re.match(r"https?://", url, re.I):
        url = None
    received = now_utc()
    return {"source": source, "external_id": str(external_id)[:200], "symbols": clean_symbols(symbols),
            "headline": headline, "summary": summary, "url": url, "kind": kind,
            "published_at": iso(published) if published else None, "received_at": iso(received),
            "tone": tone if tone is not None else float(word_tone(f"{headline} {summary[:200]}")),
            "extra": extra or {}}


# --- RTPR ----------------------------------------------------------------------------------------------------

def rtpr_article(frame: dict[str, Any]) -> dict[str, Any] | None:
    """One RTPR article or alert frame as an item. Frames are body-free (ticker, time, link), so the headline
    is read from the article itself afterwards (RtprClient.article)."""
    if not isinstance(frame, dict):
        return None
    url = frame.get("article_url") or frame.get("url")
    url = url if isinstance(url, str) else None
    symbols = clean_symbols([frame.get("ticker")] if isinstance(frame.get("ticker"), str) else frame.get("tickers") or [])
    # The release's own address (without the signed query) identifies it in both the stream and the archive.
    ident = urlparse(url).path.rstrip("/").split("/")[-1] if url else None
    ident = ident or frame.get("id") or frame.get("article_id")
    if not ident or not symbols:
        return None
    published = parse_time(frame.get("article_published_at") or frame.get("published_at") or frame.get("created"))
    extra: dict[str, Any] = {}
    rules = frame.get("rules")
    if isinstance(rules, list):
        extra["rules"] = [r.get("rule_name") for r in rules if isinstance(r, dict) and r.get("rule_name")][:10]
    if frame.get("alert_kind") == "high_impact":
        extra["impact"] = {k: frame.get(f"impact_{k}") for k in ("score", "tier", "direction")}
        extra["impact"].update({"event_type": frame.get("event_type"), "hit_rate": frame.get("band_hit_rate")})
    title = frame.get("title") or frame.get("headline")
    item = make_item("RTPR", str(ident), symbols, title if isinstance(title, str) else "", url=url,
                     published=published, kind="press_release", extra=extra,
                     tone=tone_of((extra.get("impact") or {}).get("direction"), title or "")
                     if (extra.get("impact") or {}).get("direction") else None)
    item["needs_headline"] = not (isinstance(title, str) and title.strip())
    return item


class RtprClient:
    """RTPR's REST archive and WebSocket stream. The key is sent only to rtpr.io hosts, in the Authorization
    header for REST and the address for the stream, and never written to a log or an error."""

    def __init__(self, key: str, session: requests.Session | None = None):
        self.key = key
        self.session = session or requests.Session()

    def _scrub(self, text: str) -> str:
        return redact(text.replace(self.key, "[redacted]")) if self.key else redact(text)

    def recent(self, limit: int = 50) -> list[dict[str, Any]]:
        try:
            r = self.session.get(f"{RTPR_API}/feed/articles", params={"limit": max(1, min(limit, 500))},
                                 headers={"Authorization": f"Bearer {self.key}"}, timeout=(5, 15))
        except requests.RequestException as exc:
            raise RuntimeError(f"Could not reach RTPR ({exc.__class__.__name__})") from None
        if r.status_code == 401:
            raise RuntimeError("RTPR refused the key: check RTPR_API_KEY in .env")
        if r.status_code == 403:
            raise RuntimeError("RTPR says this key's plan does not include the API (the REST feed needs Pro)")
        if r.status_code != 200:
            raise RuntimeError(f"RTPR returned {r.status_code}")
        try:
            body = r.json()
        except ValueError:
            raise RuntimeError("RTPR sent a reply this app could not read") from None
        rows = body.get("articles") if isinstance(body, dict) else body
        return [x for x in rows if isinstance(x, dict)] if isinstance(rows, list) else []

    def article(self, url: str) -> tuple[str, str]:
        """The headline and the start of a release's text. Only https addresses on rtpr.io are read, so the key
        (sent in the Authorization header) never leaves RTPR or travels unencrypted, and a frame cannot point
        the app at another site."""
        parts = urlparse(url)
        host = (parts.hostname or "").lower()
        if parts.scheme != "https" or not (host == "rtpr.io" or host.endswith(".rtpr.io")):
            raise RuntimeError("the release address is not an https address on rtpr.io, so it was not read")
        headers = {"Authorization": f"Bearer {self.key}"}
        try:
            r = self.session.get(url, params={"format": "text"}, headers=headers, timeout=(4, 8))
        except requests.RequestException as exc:
            raise RuntimeError(f"Could not read the release ({exc.__class__.__name__})") from None
        if r.status_code != 200:
            raise RuntimeError(f"Reading the release returned {r.status_code}")
        return headline_from_text(r.text)

    def stream(self, on_frame: Callable[[dict[str, Any]], None], stop: threading.Event,
               on_state: Callable[[str, str | None], None]) -> str:
        """Read the alert stream until `stop` is set. Returns "stopped", "fatal" (the key or plan was refused:
        reconnecting cannot help), "poll" (the stream keeps failing: fall back to the REST archive) or
        "no_ws" (the websockets package is missing)."""
        try:
            from websockets.exceptions import ConnectionClosed
            from websockets.sync.client import connect
        except ImportError:
            on_state("polling", "The websockets package is not installed (pip install -r requirements.txt), so RTPR "
                                "is read every five minutes instead of streamed.")
            return "no_ws"
        delay, failures = 2.0, 0
        while not stop.is_set():
            reason = "it closed"
            try:
                with connect(f"{RTPR_WS}?apiKey={self.key}", open_timeout=10, close_timeout=2,
                             max_size=2 ** 20) as ws:
                    on_state("streaming", None)
                    delay, failures = 2.0, 0
                    while not stop.is_set():
                        try:
                            raw = ws.recv(timeout=5)
                        except TimeoutError:
                            continue
                        try:
                            msg = json.loads(raw)
                        except (TypeError, ValueError):
                            continue
                        if not isinstance(msg, dict):
                            continue
                        if msg.get("type") == "ping":
                            ws.send(json.dumps({"type": "pong"}))
                            continue
                        on_frame(msg)
                    return "stopped"
            except ConnectionClosed as exc:
                code = getattr(getattr(exc, "rcvd", None), "code", None)
                if code == 4001:
                    on_state("failed", "RTPR refused the API key: check RTPR_API_KEY in .env.")
                    return "fatal"
                if code == 4004:
                    on_state("failed", "RTPR says the subscription is not active: the stream needs a paid plan.")
                    return "fatal"
                reason = f"it closed (code {code})" if code else "it closed"
            except Exception as exc:                  # noqa: BLE001 - any failure here is a reconnect, never a crash
                reason = self._scrub(f"{exc.__class__.__name__}")
            failures += 1
            if failures >= 8:
                on_state("polling", f"The RTPR stream failed {failures} times in a row ({reason}); reading the "
                                    "archive every five minutes instead.")
                return "poll"
            on_state("reconnecting", f"The RTPR stream dropped ({reason}); reconnecting in {delay:.0f} s.")
            if stop.wait(delay):
                break
            delay = min(delay * 2, 60.0)
        return "stopped"


def headline_from_text(text: str) -> tuple[str, str]:
    """The first real line of a release (its headline) and the next few hundred characters."""
    lines = [ln.strip().lstrip("#").strip() for ln in (text or "").splitlines()]
    lines = [ln for ln in lines if ln and not re.match(r"^(https?://|\(?\w+ ?wire\)?$)", ln, re.I)]
    if not lines:
        return "", ""
    head = lines[0][:300]
    return head, " ".join(lines[1:])[:600]


# --- SEC EDGAR current 8-K filings ---------------------------------------------------------------------------

ATOM = "{http://www.w3.org/2005/Atom}"
SEC_TITLE = re.compile(r"^(?P<form>\S+)\s+-\s+(?P<name>.+?)\s+\((?P<cik>\d{1,10})\)\s+\((?P<role>[^)]*)\)\s*$")
ACCESSION = re.compile(r"\b(\d{10}-\d{2}-\d{6})\b")
ITEM_CODE = re.compile(r"Item\s+(\d\.\d\d)")


def parse_sec_current(text: str) -> list[dict[str, Any]]:
    """Entries of the SEC's current-filings Atom feed: form, company, CIK, accession number, link, time and
    8-K item codes. Entries that do not read are skipped, never guessed."""
    try:
        root = ET.fromstring(text)
    except ET.ParseError:
        return []
    out = []
    for entry in root.findall(f"{ATOM}entry"):
        title = (entry.findtext(f"{ATOM}title") or "").strip()
        m = SEC_TITLE.match(title)
        if not m or m.group("role").strip().lower() not in ("filer", ""):
            continue
        link = entry.find(f"{ATOM}link")
        href = link.get("href") if link is not None else None
        summary = html.unescape(re.sub(r"<[^>]+>", " ", entry.findtext(f"{ATOM}summary") or ""))
        acc = ACCESSION.search(entry.findtext(f"{ATOM}id") or "") or ACCESSION.search(summary)
        if not acc:
            continue
        out.append({"form": m.group("form"), "company": m.group("name").strip(), "cik": int(m.group("cik")),
                    "accession": acc.group(1), "url": href, "updated": parse_time(entry.findtext(f"{ATOM}updated")),
                    "items": list(dict.fromkeys(ITEM_CODE.findall(summary)))})
    return out


def sec_headline(f: dict[str, Any]) -> str:
    items = [ITEM_LABELS.get(code, f"Item {code}") for code in f["items"] if code != "9.01"]
    return f"{f['form']}: {f['company']}" + (f" — {'; '.join(items)}" if items else "")


# --- Finnhub -------------------------------------------------------------------------------------------------

def finnhub_items(rows: Any, symbol: str | None = None) -> list[dict[str, Any]]:
    out = []
    for r in rows if isinstance(rows, list) else []:
        if not isinstance(r, dict) or not isinstance(r.get("headline"), str) or not r["headline"].strip():
            continue
        symbols = clean_symbols(r.get("related") or "") or ([symbol] if symbol else [])
        ident = r.get("id") or hashlib.sha1(f"{r['headline']}|{r.get('url')}".encode()).hexdigest()[:16]
        out.append(make_item("Finnhub", str(ident), symbols, r["headline"], url=r.get("url"),
                             published=parse_time(r.get("datetime")), summary=r.get("summary") or "",
                             extra={"publisher": r.get("source")}))
    return out


# --- demo ----------------------------------------------------------------------------------------------------

DEMO_TEMPLATES = (
    ("{s} raises full-year outlook after record quarter", 0.045),
    ("{s} receives FDA approval for its lead treatment", 0.06),
    ("{s} announces $2 billion share buyback", 0.025),
    ("{s} cuts guidance, citing weaker demand", -0.05),
    ("{s} chief executive to step down", -0.03),
    ("{s} wins multi-year government contract", 0.035),
    ("{s} discloses regulatory probe into sales practices", -0.04),
    ("{s} to be acquired at a 30% premium", 0.08),
    ("{s} schedules quarterly results call", 0.0),
)


def demo_item(symbols: list[str], moment: datetime) -> dict[str, Any]:
    """One made-up item, the same for a given minute. `demo_move` is the price move the demo prices will make
    over the next two minutes, so the screen can be learned on any day."""
    seed = int(hashlib.sha256(moment.strftime("%Y-%m-%d %H:%M").encode()).hexdigest()[:8], 16)
    sym = symbols[seed % len(symbols)]
    text, move = DEMO_TEMPLATES[(seed // 7) % len(DEMO_TEMPLATES)]
    item = make_item("Demo", f"{sym}-{moment:%Y%m%d%H%M}", [sym], f"{text.format(s=sym)} (made up)",
                     published=moment - timedelta(seconds=1), kind="press_release",
                     summary="A demo item: the headline and the price move after it are made up.")
    item["extra"]["demo_move"] = move
    return item


# --- storage -------------------------------------------------------------------------------------------------

def store_item(db: Any, item: dict[str, Any]) -> int | None:
    """Keep an item once (source and its own id). Returns the new row's id, or None for one already kept."""
    extra = {k: v for k, v in (item.get("extra") or {}).items() if v not in (None, [], {})}
    with db.tx() as cur:
        cur.execute("""INSERT INTO live_news (source, external_id, symbols, headline, summary, url, kind,
                       published_at, received_at, tone, extra_json) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                       ON CONFLICT(source, external_id) DO NOTHING""",
                    (item["source"], item["external_id"], json.dumps(item["symbols"]), item["headline"],
                     item.get("summary") or "", item.get("url"), item.get("kind") or "news", item.get("published_at"),
                     item["received_at"], item.get("tone"), json.dumps(extra) if extra else None))
        return int(cur.lastrowid) if cur.rowcount else None


def item_row(r: Any) -> dict[str, Any]:
    d = dict(r)
    d["symbols"] = json.loads(d.get("symbols") or "[]")
    d["extra"] = json.loads(d.pop("extra_json") or "{}")
    pub, rec = parse_time(d.get("published_at")), parse_time(d.get("received_at"))
    d["delay_seconds"] = (rec - pub).total_seconds() if pub and rec else None
    return d


def signal_row(r: Any) -> dict[str, Any]:
    d = dict(r)
    d["news_ids"] = json.loads(d.get("news_ids") or "[]")
    d["checks_list"] = json.loads(d.pop("checks_json") or "[]")
    d["card"] = json.loads(d.pop("card_json")) if d.get("card_json") else None
    d["holdings"] = json.loads(d.pop("holdings_json")) if d.get("holdings_json") else []
    return d


def prune(db: Any, days: int = KEEP_DAYS) -> None:
    cutoff = iso(now_utc() - timedelta(days=days))
    with db.tx() as cur:
        cur.execute("DELETE FROM live_signals WHERE opened_at < ?", (cutoff,))
        cur.execute("DELETE FROM live_news WHERE received_at < ? AND id NOT IN (SELECT news_id FROM live_signals)",
                    (cutoff,))


# --- the judgement -------------------------------------------------------------------------------------------

def judge(*, ref: float, price: float, prev_close: float, atr: float, late: bool, settings: Any) -> dict[str, Any]:
    """Whether the price has answered the news, and which way. Pure arithmetic, so it can be tested alone."""
    r_pct, r_atr = 100 * (price / ref - 1), (price - ref) / atr
    d_pct, d_atr = 100 * (price / prev_close - 1), (price - prev_close) / atr
    out = {"since_news_pct": r_pct, "since_news_ranges": r_atr, "day_pct": d_pct, "day_ranges": d_atr,
           "direction": None, "path": None}
    against = r_atr * d_atr < 0 and abs(d_atr) >= 0.25            # the day still points the other way
    if abs(r_pct) >= settings.live_min_reaction_pct and abs(r_atr) >= settings.live_min_reaction_atr and not against:
        out.update(direction="LONG" if r_atr > 0 else "SHORT", path="reaction")
    elif (late and abs(d_atr) >= settings.live_min_day_atr and abs(d_pct) >= settings.live_min_day_pct
          and not (r_atr * d_atr < 0 and abs(r_atr) >= settings.live_min_reaction_atr)):
        out.update(direction="LONG" if d_atr > 0 else "SHORT", path="day_move")
    return out


def levels_for(direction: str, entry: float, ref: float, atr: float, suggested: dict[str, Any] | None,
               path: str) -> tuple[float, float, float, str]:
    """Stop and targets for a news trade. The stop sits a quarter of a daily range beyond where the stock stood
    before the news, when that is between half a range and two and a half ranges away: back there, the news
    has been undone. Otherwise the chart's level, kept within 2.5 ranges. Targets are the chart's levels at
    least one risk away, else two and three times the risk."""
    d = 1 if direction == "LONG" else -1
    stop, why = None, ""
    if path == "reaction":
        cand = ref - d * STOP_BEYOND_ATR * atr
        if STOP_MIN_ATR * atr <= d * (entry - cand) <= STOP_MAX_ATR * atr:
            stop = cand
            why = (f"Stop a quarter of a daily range {'below' if d > 0 else 'above'} ${ref:,.2f}, where the stock "
                   "stood when the news arrived: back there, the news has been undone.")
    if stop is None and suggested:
        cand = float(suggested["invalidation"])
        if d * (entry - cand) > 0:
            stop = entry - d * min(d * (entry - cand), STOP_MAX_ATR * atr)
            why = "Stop at the chart's level" + (", held to 2.5 daily ranges." if stop != cand else ".")
    if stop is None or d * (entry - stop) < STOP_MIN_ATR * atr:
        stop = entry - d * 1.2 * atr
        why = "Stop 1.2 daily ranges from the entry (no usable chart level)."
    risk = abs(entry - stop)
    cands = [float(suggested[k]) for k in ("target_1", "target_2") if suggested and suggested.get(k)]
    cands = sorted([t for t in cands if d * (t - entry) >= risk], key=lambda t: d * (t - entry))
    t1 = cands[0] if cands else entry + d * 2 * risk
    t2 = cands[1] if len(cands) > 1 else entry + d * 3 * risk
    if d * (t2 - t1) <= 0:                            # a chart target 1 beyond three times the risk
        t2 = t1 + d * risk
    if d < 0:                                         # a short's targets stay above a twentieth of the entry
        from .evaluator import SHORT_TARGET_FLOOR
        floor = SHORT_TARGET_FLOOR * entry
        t1 = max(t1, floor * 2)
        t2 = max(t2, floor)
        if t2 >= t1:
            t2 = 0.5 * (t1 + floor)
    why += (" Targets at the chart's levels in the way." if cands else " Targets at two and three times the risk.")
    return stop, t1, t2, why


# --- the feed ------------------------------------------------------------------------------------------------

class LiveFeed:
    """Listens to every configured source in the background, keeps the items, and checks the price after each.
    Started from the screen (or at startup with LIVE_NEWS_AUTOSTART=1, or when it was on when the app last
    stopped); one background thread plus one for the RTPR stream."""

    def __init__(self, svc: Any, morning: Any, analyst: Any = None):
        self.svc = svc
        self.morning = morning
        self.analyst = analyst
        self.settings = svc.settings
        self.db = svc.db
        self._lock = threading.Lock()
        self._life = threading.Lock()                  # start and stop, one at a time
        self._thread: threading.Thread | None = None
        self._ws_thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._inbox: queue.Queue = queue.Queue(maxsize=5000)
        self._next_poll: dict[str, float] = {}
        self._finnhub_min_id = 0
        self._finnhub_cursor = 0
        self._universe: tuple[float, set[str]] | None = None
        self._rtpr = RtprClient(self.settings.rtpr_api_key) if self.settings.rtpr_api_key else None
        self._session = requests.Session()
        self._bars: dict[str, tuple[float, dict[str, Any] | None]] = {}
        self._demo_runs: dict[int, tuple[float, float]] = {}   # demo only: signal id -> (start, made-up move)
        self._headlines: Any = None                    # a small pool reading RTPR headlines, made when first needed
        self._google_cursor = 0
        self._next_scan = 0.0
        self._movers: dict[str, dict[str, Any]] = {}
        self._headline_jobs = 0
        self.started_at: str | None = None
        self.sources: dict[str, dict[str, Any]] = {name: {"name": name, "state": "off", "note": None,
                                                          "last_item_at": None, "last_ok_at": None,
                                                          "last_error": None, "items": 0}
                                                   for name in self.source_names()}

    # --- which sources -------------------------------------------------------------------------------------
    def source_names(self) -> list[str]:
        names = []
        if self.settings.rtpr_api_key:
            names.append("RTPR")
        if self.svc.sec and self.svc.sec.enabled:
            names.append("SEC EDGAR")
        if self.settings.finnhub_api_key:
            names.append("Finnhub")
        search = self.search
        if search.google_on:
            names.append("Google News")
        if search.brave_on:
            names.append("Brave")
        if self.svc.market.info.mode == "demo":
            names.append("Demo")
        return names

    @property
    def search(self) -> Any:
        from .searchnews import for_service
        return for_service(self.svc)

    def setup_notes(self) -> list[str]:
        notes = []
        if not self.settings.rtpr_api_key:
            notes.append("RTPR is not set up: add RTPR_API_KEY to .env (rtpr.io; the paid plan streams press releases "
                         "within a second, the free one is five minutes late) and create the \"All articles\" rule "
                         "in RTPR's dashboard.")
        if not (self.svc.sec and self.svc.sec.enabled):
            notes.append("SEC 8-K filings are off: set SEC_USER_AGENT in .env (your name and email; free).")
        if not self.settings.finnhub_api_key:
            notes.append("Finnhub headlines are off: set FINNHUB_API_KEY in .env (a free key from finnhub.io).")
        if not getattr(self.settings, "google_news", True):
            notes.append("Google News search is off (GOOGLE_NEWS=0): it is free and needs no key.")
        if not getattr(self.settings, "brave_api_key", ""):
            notes.append("Brave search is optional (BRAVE_API_KEY): a key comes with $5 of credit a month, about 1,000 "
                         "searches, and a card that is charged past it. The app stops at BRAVE_MONTHLY_LIMIT (900) so "
                         "the credit is never exceeded.")
        return notes

    def _source(self, name: str, state: str | None = None, note: str | None = None, error: str | None = None,
                ok: bool = False) -> None:
        with self._lock:
            src = self.sources.setdefault(name, {"name": name, "state": "off", "note": None, "last_item_at": None,
                                                 "last_ok_at": None, "last_error": None, "items": 0})
            if state:
                src["state"] = state
            if note is not None:
                src["note"] = note
            if error is not None:
                src["last_error"] = {"at": iso(now_utc()), "error": redact(error)[:300]}
                emit("news_failed", logging.WARNING, what=f"live {name}", error=redact(error)[:300])
            if ok:
                src["last_ok_at"] = iso(now_utc())

    # --- start and stop ------------------------------------------------------------------------------------
    # Each start makes its own stop event and hands it to its threads, which loop on that event alone: a
    # thread still finishing its last pass after a stop can never be woken again by the next start. A start
    # while that thread is still alive is refused until it has ended, so two loops never run at once.
    def _alive(self) -> bool:
        return any(t is not None and t.is_alive() for t in (self._thread, self._ws_thread))

    @property
    def running(self) -> bool:
        return self._alive() and not self._stop.is_set()

    @property
    def stopping(self) -> bool:
        return self._alive() and self._stop.is_set()

    def start(self, remember: bool = True) -> dict[str, Any]:
        with self._life:
            if self.stopping:
                raise RuntimeError("Live news is still finishing its last pass after Stop; try again in a few seconds.")
            if not self.running:
                stop = threading.Event()
                self._stop = stop
                self.started_at = iso(now_utc())
                self._next_poll = {}
                self._thread = threading.Thread(target=self._run, args=(stop,), name="live-news", daemon=True)
                self._ws_thread = (threading.Thread(target=self._stream, args=(stop,), name="live-news-rtpr",
                                                   daemon=True) if self._rtpr else None)
                self._thread.start()
                if self._ws_thread:
                    self._ws_thread.start()
            if remember:
                self._remember(True)
        return self.status()

    def stop(self, remember: bool = True, wait: float = 2.0) -> dict[str, Any]:
        with self._life:
            self._stop.set()
            for t in (self._thread, self._ws_thread):
                if t is not None:
                    t.join(timeout=wait)
            with self._lock:
                for src in self.sources.values():
                    src["state"] = "off"
            if remember:
                self._remember(False)
        return self.status()

    def _remember(self, on: bool) -> None:
        try:
            self.db.cache_put(f"live:on:{self.svc.market.info.mode}", {"on": on, "at": iso(now_utc())})
        except Exception:                             # noqa: BLE001 - only a convenience for the next start
            log.info("Could not remember whether live news was on")

    def resume_if_wanted(self) -> None:
        """At startup: listen again if LIVE_NEWS_AUTOSTART=1 or if it was on when the app last stopped."""
        try:
            was = (self.db.cache_get(f"live:on:{self.svc.market.info.mode}") or {}).get("on")
        except Exception:                             # noqa: BLE001
            was = False
        if self.settings.live_news_autostart or was:
            self.start(remember=False)

    # --- the loop ------------------------------------------------------------------------------------------
    def _stream(self, stop: threading.Event) -> None:
        def on_state(state: str, note: str | None) -> None:
            if stop.is_set():
                return
            self._source("RTPR", state, note, ok=state == "streaming")
            if state in ("failed", "reconnecting") and note:
                self._source("RTPR", error=note)

        def on_frame(frame: dict[str, Any]) -> None:
            item = rtpr_article(frame)
            if item:
                try:
                    self._inbox.put_nowait(item)
                except queue.Full:
                    log.warning("Live news inbox is full; an RTPR item was dropped")
        try:
            outcome = self._rtpr.stream(on_frame, stop, on_state)
        except Exception as exc:                      # noqa: BLE001 - the stream thread must end cleanly
            outcome = "poll"
            self._source("RTPR", "polling", error=exc.__class__.__name__)
        if outcome in ("poll", "no_ws") and not stop.is_set():
            self._next_poll["RTPR"] = 0.0             # the main loop reads the archive from now on

    def _run(self, stop: threading.Event) -> None:
        for name in self.source_names():
            if name == "Brave":
                self._source(name, "on_demand", "Asked only when a mover has no news from the free sources.")
            elif name != "RTPR":
                self._source(name, "polling", None)
        if self._rtpr:
            self._source("RTPR", "connecting", None)
            self._next_poll["RTPR"] = 0.0             # read the recent archive once, then stream
        last_prune = 0.0
        while not stop.is_set():
            try:
                if time.time() - last_prune > 3600:   # old items go hourly, not only when listening starts
                    prune(self.db)
                    last_prune = time.time()
                self._poll_due()
                self._drain(stop)
                self._expire()
                if not stop.is_set():
                    self._check_due()
                if not stop.is_set() and self._scan_due():
                    self._scan_movers(stop)
            except Exception:                         # noqa: BLE001 - the loop must survive anything
                log.exception("Live news loop failed a pass")
            stop.wait(LOOP_SECONDS)

    def _poll_due(self) -> None:
        now = time.time()
        for name in self.source_names():
            if name == "RTPR" and "RTPR" not in self._next_poll:
                continue                              # streaming: the archive is read only as a fallback
            if name == "Brave":
                continue                              # asked on demand, by the movers scan
            if self._next_poll.get(name, 0.0) > now:
                continue
            self._next_poll[name] = now + POLL_SECONDS[name]
            try:
                items = self._fetch(name)
                self._source(name, ok=True)
                if name == "RTPR" and self._ws_thread and self._ws_thread.is_alive():
                    self._next_poll.pop("RTPR", None)  # the archive was read once at the start; the stream does the rest
            except Exception as exc:                  # noqa: BLE001 - one source failing never stops the others
                self._source(name, error=redact(str(exc)) or exc.__class__.__name__)
                continue
            for it in items:
                try:
                    self._inbox.put_nowait(it)
                except queue.Full:
                    break

    def _fetch(self, name: str) -> list[dict[str, Any]]:
        if name == "RTPR":
            return [x for x in (rtpr_article(f) for f in self._rtpr.recent(50)) if x]
        if name == "SEC EDGAR":
            out = []
            for f in parse_sec_current(self.svc.sec.get_text(SEC_CURRENT)):
                if f["form"] not in ("8-K", "8-K/A", "6-K"):
                    continue
                try:
                    symbols = self.svc.sec.tickers_for_cik(f["cik"])
                except Exception:                     # noqa: BLE001 - no ticker list: the filing has no symbols
                    symbols = []
                if not symbols:
                    continue
                out.append(make_item("SEC EDGAR", f["accession"], symbols, sec_headline(f), url=f["url"],
                                     published=f["updated"], kind="filing", tone=0.0,
                                     extra={"items": f["items"], "form": f["form"]}))
            return out
        if name == "Finnhub":
            return self._finnhub()
        if name == "Demo":
            return [demo_item(sorted(self.universe())[:60] or ["AAPL"], now_utc())]
        if name == "Google News":
            return self._google_rotation()
        return []

    def _google_rotation(self) -> list[dict[str, Any]]:
        """The next few of your holdings, then your watchlist, searched on Google News (the last six hours)."""
        held = [h for h in self._held_symbols()]
        names = list(dict.fromkeys([*held, *(s.upper() for s in self.settings.watchlist)]))
        if not names:
            return []
        out, errors = [], []
        for _ in range(min(GOOGLE_NAMES_PER_POLL, len(names))):
            sym = names[self._google_cursor % len(names)]
            self._google_cursor += 1
            got = self.search.search(sym, hours=6, brave="never")
            errors += got["errors"]
            out += [self._search_item(sym, it) for it in got["items"]]
        if errors and not out:
            raise RuntimeError(errors[-1])
        return out

    def _held_symbols(self) -> list[str]:
        try:
            return [r["symbol"] for r in self.db.query(
                "SELECT DISTINCT symbol FROM positions WHERE status IN ('OPEN', 'PARTIALLY_CLOSED') ORDER BY symbol")]
        except Exception:                             # noqa: BLE001 - no positions table yet
            return []

    @staticmethod
    def _search_item(symbol: str, it: dict[str, Any]) -> dict[str, Any]:
        source = "Brave" if it.get("via") == "Brave" else "Demo" if it.get("via") == "Demo search" else "Google News"
        return make_item(source, it["id"], [symbol], it["headline"], url=it.get("url"),
                         published=parse_time(it.get("published")), summary=it.get("summary") or "",
                         extra={"publisher": it.get("source"), "found_by": "search"})

    # --- why is it moving? ---------------------------------------------------------------------------------
    def _scan_due(self) -> bool:
        minutes = float(getattr(self.settings, "live_mover_sweep_minutes", 0) or 0)
        if minutes <= 0 or not self.search.available or time.time() < self._next_scan:
            return False
        if self.session() == "closed":
            return False
        self._next_scan = time.time() + minutes * 60
        return True

    def _has_news(self, symbol: str, hours: float) -> dict[str, Any] | None:
        row = self.db.one("SELECT id, headline, url, source FROM live_news WHERE received_at >= ? AND symbols LIKE ? "
                          "ORDER BY id DESC LIMIT 1", (iso(now_utc() - timedelta(hours=hours)), f'%"{symbol}"%'))
        return dict(row) if row else None

    def _scan_movers(self, stop: threading.Event | None = None) -> None:
        """Quote the universe, find the stocks that moved at least LIVE_MIN_DAY_PCT percent and LIVE_MIN_DAY_ATR of
        their daily ranges since the last close, and search the news for those the feed has nothing on. News found
        goes into the feed (and is judged like any late item); none found is listed as such."""
        session = self.session()
        names = sorted(self.universe())
        quotes: dict[str, Any] = {}
        for i in range(0, len(names), 50):
            if stop is not None and stop.is_set():
                return
            chunk = names[i:i + 50]
            try:
                quotes.update(self.svc.market._call("live movers", lambda c=chunk: self.svc.market.provider.quotes(c)))
            except DataError as exc:
                log.info("Live news: movers quotes failed: %s", exc)
        found: list[tuple[str, float, float, float, dict[str, Any]]] = []
        for sym, q in quotes.items():
            price, _ = self._price(q, session)
            prev = q.get("prevclose")
            if not price or not isinstance(prev, (int, float)) or prev <= 0:
                continue
            pct = 100 * (price / prev - 1)
            if abs(pct) >= self.settings.live_min_day_pct:
                found.append((sym, price, float(prev), pct, q))
        found.sort(key=lambda r: -abs(r[3]))
        now = now_utc()
        lookups = 0
        for sym, price, prev, pct, q in found[:MOVERS_KEPT]:
            if stop is not None and stop.is_set():
                break
            daily = self._daily(sym)
            if not daily or not daily.get("atr"):
                continue
            ranges = (price - prev) / daily["atr"]
            if abs(ranges) < self.settings.live_min_day_atr:
                continue
            row = self._movers.get(sym) or {"symbol": sym, "first_seen": iso(now), "checked_at": None,
                                            "found": None, "checked": [], "errors": []}
            row.update(price=price, prev_close=prev, move_pct=pct, ranges=ranges, updated_at=iso(now),
                       name=q.get("description") or None)
            have = self._has_news(sym, STALE_HOURS)
            if have:
                row.update(found=True, headline=have["headline"], url=have["url"], via=have["source"])
            else:
                last = parse_time(row.get("checked_at"))
                due = last is None or (not row.get("found") and now - last >= timedelta(minutes=MOVER_RECHECK_MINUTES))
                if due and lookups < MOVER_LOOKUPS_PER_SCAN:
                    lookups += 1
                    try:
                        got = self.search.search(sym, hours=STALE_HOURS, name_hint=q.get("description"),
                                                 brave="fallback")
                    except Exception as exc:          # noqa: BLE001 - one search failing never stops the scan
                        got = {"items": [], "checked": [], "errors": [redact(str(exc)) or exc.__class__.__name__]}
                    row.update(checked_at=iso(now), checked=got["checked"], errors=got["errors"][:2],
                               found=bool(got["items"]) if got["checked"] else None)
                    for it in got["items"][:5]:
                        self.ingest(self._search_item(sym, it))
                    if got["items"]:
                        top = got["items"][0]
                        row.update(headline=top["headline"], url=top.get("url"), via=top.get("via"))
            self._movers[sym] = row
        cutoff = iso(now - timedelta(hours=12))
        self._movers = {k: v for k, v in sorted(self._movers.items(), key=lambda kv: -abs(kv[1]["move_pct"]))
                        if v["updated_at"] >= cutoff}
        self._movers = dict(list(self._movers.items())[:MOVERS_KEPT])
        try:
            self.db.cache_put(self._movers_key(), {"day": datetime.now(NY).date().isoformat(), "rows": self._movers})
        except Exception:                             # noqa: BLE001 - kept in memory regardless
            log.info("Live news: movers not saved")

    def _movers_key(self) -> str:
        return f"live:movers:{self.svc.market.info.mode}"

    def movers(self) -> list[dict[str, Any]]:
        if not self._movers:
            try:
                saved = self.db.cache_get(self._movers_key()) or {}
                if saved.get("day") == datetime.now(NY).date().isoformat():
                    self._movers = saved.get("rows") or {}
            except Exception:                         # noqa: BLE001
                pass
        return sorted(self._movers.values(), key=lambda r: -abs(r.get("move_pct") or 0))

    def _finnhub(self) -> list[dict[str, Any]]:
        key = self.settings.finnhub_api_key

        def get(path: str, params: dict[str, Any]) -> Any:
            try:
                r = self._session.get(f"{FINNHUB}/{path}", params={**params, "token": key}, timeout=(5, 15))
            except requests.RequestException as exc:
                raise RuntimeError(f"Could not reach Finnhub ({exc.__class__.__name__})") from None
            if r.status_code == 429:
                raise RuntimeError("Finnhub's rate limit was reached (free keys allow about 60 requests a minute)")
            if r.status_code != 200:
                raise RuntimeError(f"Finnhub returned {r.status_code}")
            try:
                return r.json()
            except ValueError:
                raise RuntimeError("Finnhub sent a reply this app could not read") from None
        params = {"category": "general"}
        if self._finnhub_min_id:
            params["minId"] = self._finnhub_min_id
        rows = get("news", params)
        if isinstance(rows, list):
            ids = [r.get("id") for r in rows if isinstance(r, dict) and isinstance(r.get("id"), int)]
            if ids:
                self._finnhub_min_id = max(ids)
        out = finnhub_items(rows)
        names = list(dict.fromkeys(s.upper() for s in self.settings.watchlist))
        today = datetime.now(NY).date().isoformat()
        for _ in range(min(FINNHUB_COMPANY_PER_POLL, len(names))):
            sym = names[self._finnhub_cursor % len(names)]
            self._finnhub_cursor += 1
            out += finnhub_items(get("company-news", {"symbol": sym, "from": today, "to": today}), sym)
        fresh_cut = now_utc() - timedelta(hours=STALE_HOURS)
        return [it for it in out if not it["published_at"] or parse_time(it["published_at"]) >= fresh_cut]

    # --- taking items in -----------------------------------------------------------------------------------
    def _drain(self, stop: threading.Event | None = None) -> None:
        for _ in range(200):
            if stop is not None and stop.is_set():
                return
            try:
                item = self._inbox.get_nowait()
            except queue.Empty:
                return
            self.ingest(item)

    def ingest(self, item: dict[str, Any]) -> int | None:
        """Keep one item and start (or join) a signal for each stock it names that can be traded here.

        An RTPR item carries no headline: it is kept at once with a plain one, its stocks are priced at once
        (that first price is what the reaction is measured from), and the headline is read in the background
        and written in when it arrives, so a burst of releases never holds up the price checks."""
        relevant = [s for s in item["symbols"] if self.in_universe(s)]
        fetch = bool(item.get("needs_headline") and self._rtpr and item.get("url")
                     and (relevant or self.settings.live_universe == "all"))
        if item.get("needs_headline") and (not item["headline"] or item["headline"] == "(no headline)"):
            item["headline"] = f"Press release: {', '.join(item['symbols'])}"
        elif self._duplicate(item):
            return None                               # the same story already came in from another source
        news_id = store_item(self.db, item)
        if news_id is None:
            return None
        with self._lock:
            src = self.sources.get(item["source"])
            if src is not None:
                src["items"] += 1
                src["last_item_at"] = item["received_at"]
        if fetch:
            self._fetch_headline(news_id, item)
        published = parse_time(item.get("published_at"))
        if published and now_utc() - published > timedelta(hours=STALE_HOURS):
            return news_id                            # an old item from an archive read: listed, not traded
        for sym in relevant[:5]:
            self._open_signal(sym, news_id, item)
        return news_id

    def _duplicate(self, item: dict[str, Any]) -> bool:
        """Whether the same headline on one of the same stocks arrived in the last two days, from any source."""
        if not item["symbols"]:
            return False
        rows = self.db.query("SELECT symbols FROM live_news WHERE received_at >= ? AND lower(headline) = lower(?)",
                             (iso(now_utc() - timedelta(days=2)), item["headline"]))
        return any(set(json.loads(r["symbols"] or "[]")) & set(item["symbols"]) for r in rows)

    def _fetch_headline(self, news_id: int, item: dict[str, Any]) -> None:
        with self._lock:
            if self._headline_jobs >= MAX_HEADLINE_JOBS:
                return                                # a burst: the plain headline stands for the rest
            self._headline_jobs += 1
            if self._headlines is None:
                from concurrent.futures import ThreadPoolExecutor
                self._headlines = ThreadPoolExecutor(max_workers=2, thread_name_prefix="live-news-headline")

        def job() -> None:
            try:
                head, summary = self._rtpr.article(item["url"])
                if head:
                    impact = (item.get("extra") or {}).get("impact") or {}
                    tone = item["tone"] if impact.get("direction") else float(word_tone(f"{head} {summary[:200]}"))
                    with self.db.tx() as cur:
                        cur.execute("UPDATE live_news SET headline = ?, summary = ?, tone = ? WHERE id = ?",
                                    (" ".join(head.split())[:300], " ".join(summary.split())[:600], tone, news_id))
            except Exception as exc:                  # noqa: BLE001 - the plain headline stands
                self._source("RTPR", error=f"headline not read: {exc.__class__.__name__}: {exc}")
            finally:
                with self._lock:
                    self._headline_jobs -= 1
        self._headlines.submit(job)

    def universe(self) -> set[str]:
        hit = self._universe
        if hit and time.time() - hit[0] < 600:
            return hit[1]
        from .morning import movers_universe
        names = set(movers_universe(self.settings.watchlist))
        try:
            names |= {r["symbol"] for r in self.db.query(
                "SELECT DISTINCT symbol FROM positions WHERE status IN ('OPEN', 'PARTIALLY_CLOSED')")}
        except Exception:                             # noqa: BLE001 - no positions table yet
            pass
        self._universe = (time.time(), names)
        return names

    def in_universe(self, symbol: str) -> bool:
        return self.settings.live_universe == "all" or symbol in self.universe()

    def _open_signal(self, symbol: str, news_id: int, item: dict[str, Any]) -> None:
        now = now_utc()
        since = iso(now - timedelta(minutes=MERGE_MINUTES))
        with self.db.tx() as cur:
            cur.execute("SELECT id, news_ids FROM live_signals WHERE symbol = ? AND opened_at >= ? AND state IN "
                        "('waiting', 'watching', 'opportunity') ORDER BY id DESC LIMIT 1", (symbol, since))
            row = cur.fetchone()
            if row:
                ids = json.loads(row["news_ids"] or "[]")
                if news_id not in ids:
                    ids.append(news_id)
                cur.execute("UPDATE live_signals SET news_ids = ?, updated_at = ? WHERE id = ?",
                            (json.dumps(ids[-20:]), iso(now), row["id"]))
                return
            cur.execute("""INSERT INTO live_signals (symbol, news_id, news_ids, opened_at, state, next_check_at,
                           updated_at) VALUES (?, ?, ?, ?, 'waiting', ?, ?)""",
                        (symbol, news_id, json.dumps([news_id]), iso(now), iso(now), iso(now)))

    # --- prices --------------------------------------------------------------------------------------------
    def session(self) -> str:
        """"open", "extended" or "closed". Demo prices are made up, so the demo is always open."""
        if self.svc.market.info.mode == "demo":
            return "open"
        state = (self.svc.market.clock() or {}).get("state")
        if state in OPEN_STATES:
            return "open"
        if state in EXTENDED_STATES:
            return "extended"
        return "closed"

    def _price(self, q: dict[str, Any], session: str,
               demo: tuple[float, float] | None = None) -> tuple[float | None, str | None]:
        last, bid, ask = q.get("last"), q.get("bid"), q.get("ask")
        price = None
        if session == "open" and bid and ask and ask >= bid > 0 and (ask - bid) / ask < 0.01:
            price = 0.5 * (bid + ask)
        elif last and last > 0:
            if session != "open":
                trade = parse_time(q.get("trade_time"))
                if trade is None or (now_utc() - trade).total_seconds() > QUOTE_FRESH_SECONDS:
                    return None, "no trade in the last 15 minutes outside regular hours"
            price = float(last)
        if price is None or not math.isfinite(price) or price <= 0:
            return None, "no usable price"
        if demo is not None and self.svc.market.info.mode == "demo":
            start, move = demo                         # the demo item's made-up move, spread over two minutes
            price *= 1 + move * min(1.0, (time.time() - start) / DEMO_MOVE_SECONDS)
        return price, None

    def _daily(self, symbol: str) -> dict[str, Any] | None:
        """Yesterday's close (the last finished bar), ATR(14) and average dollar volume, cached for an hour."""
        hit = self._bars.get(symbol)
        if hit and time.time() - hit[0] < 3600:
            return hit[1]
        from .indicators import atr as atr_series
        try:
            df = self.svc.market.bars(symbol, 1.0, complete=True)
        except Exception as exc:                      # noqa: BLE001 - no history: the stock is skipped, with why
            log.info("Live news: no bars for %s: %s", symbol, exc)
            self._bars[symbol] = (time.time(), None)
            return None
        if len(df) < 30:
            out = None
        else:
            atr = float(atr_series(df["high"], df["low"], df["close"]).iloc[-1])
            tail = df.tail(20)
            out = {"prev_close": float(df["close"].iloc[-1]), "atr": atr if math.isfinite(atr) and atr > 0 else None,
                   "dollar_volume": float((tail["close"] * tail["volume"]).mean()), "bars": len(df)}
        self._bars[symbol] = (time.time(), out)
        return out

    # --- checking signals ----------------------------------------------------------------------------------
    def _check_due(self) -> None:
        session = self.session()
        if session == "closed":
            return
        now = now_utc()
        due = [signal_row(r) for r in self.db.query(
            "SELECT * FROM live_signals WHERE state IN ('waiting', 'watching', 'opportunity') AND next_check_at <= ? "
            "ORDER BY next_check_at LIMIT 50", (iso(now),))]
        if not due:
            return
        symbols = sorted({s["symbol"] for s in due})
        quotes: dict[str, Any] = {}
        for i in range(0, len(symbols), 50):
            chunk = symbols[i:i + 50]
            try:
                quotes.update(self.svc.market._call("live news quotes", lambda c=chunk: self.svc.market.provider.quotes(c)))
            except DataError as exc:
                log.info("Live news quotes failed: %s", exc)
        for sig in due:
            try:
                self.check(sig, quotes.get(sig["symbol"]) or {}, session)
            except Exception as exc:                  # noqa: BLE001 - one stock never stops the rest
                log.warning("Live news: %s could not be checked: %r", sig["symbol"], exc)
                self._update(sig["id"], expect={"state": sig["state"], "checks": sig["checks"]},
                             next_check_at=iso(now + timedelta(seconds=120)),
                             reason=f"Check failed ({exc.__class__.__name__}); trying again.")

    def _update(self, signal_id: int, expect: dict[str, Any] | None = None, **fields: Any) -> bool:
        """Write fields onto a signal. With `expect` (column: value), only if the row still has those values,
        so a check that raced another never overwrites it. True when the row was written."""
        fields["updated_at"] = iso(now_utc())
        cols = ", ".join(f"{k} = ?" for k in fields)
        where = "id = ?" + "".join(f" AND {k} = ?" for k in (expect or {}))
        with self.db.tx() as cur:
            cur.execute(f"UPDATE live_signals SET {cols} WHERE {where}",
                        (*fields.values(), signal_id, *(expect or {}).values()))
            return cur.rowcount > 0

    def _expire(self) -> None:
        """End signals by their age, whatever the session (2.10 review): a watched stock not judged within
        WATCH_LIMIT_MINUTES, and an opportunity OPPORTUNITY_WATCH_MINUTES after it confirmed, so nothing is ever
        judged across a closed market (an overnight gap is not a reaction to last evening's news)."""
        now = now_utc()
        watch_cut = iso(now - timedelta(minutes=WATCH_LIMIT_MINUTES))
        opp_cut = iso(now - timedelta(minutes=OPPORTUNITY_WATCH_MINUTES))
        with self.db.tx() as cur:
            cur.execute("UPDATE live_signals SET state = 'no_reaction', next_check_at = NULL, updated_at = ?, "
                        "reason = 'No move worth trading within ' || ? || ' minutes of the first price (or no usable "
                        "price since).' WHERE state = 'watching' AND ref_at < ?",
                        (iso(now), str(WATCH_LIMIT_MINUTES), watch_cut))
            cur.execute("UPDATE live_signals SET state = 'done', next_check_at = NULL, updated_at = ?, "
                        "reason = 'Watched for ' || ? || ' minutes after the move confirmed; last $' || "
                        "printf('%.2f', last_price) || '. Nothing watches the stop after this: set a watch or "
                        "your own stop order.' WHERE state = 'opportunity' AND opportunity_at < ?",
                        (iso(now), str(OPPORTUNITY_WATCH_MINUTES), opp_cut))

    def check(self, sig: dict[str, Any], quote: dict[str, Any], session: str) -> None:
        now = now_utc()
        sym = sig["symbol"]
        same = {"state": sig["state"], "checks": sig["checks"]}      # written only if no other check got there first
        news = self.db.one("SELECT * FROM live_news WHERE id = ?", (sig["news_id"],))
        item = item_row(news) if news else {"extra": {}, "published_at": None, "received_at": sig["opened_at"],
                                            "tone": 0.0, "headline": ""}
        if sig["state"] == "waiting":
            published = parse_time(item.get("published_at")) or parse_time(item.get("received_at"))
            if published and now - published > timedelta(hours=STALE_HOURS):
                self._update(sig["id"], same, state="skipped", next_check_at=None,
                             reason=f"The news was over {STALE_HOURS} hours old by the time {sym} could be priced.")
                return
            daily = self._daily(sym)
            if not daily or not daily.get("atr"):
                self._update(sig["id"], same, state="skipped", next_check_at=None,
                             reason=f"No daily price history for {sym}, so its daily range is unknown.")
                return
            if self.settings.live_universe == "all" and sym not in self.universe() and \
                    daily["dollar_volume"] < MIN_DOLLAR_VOLUME:
                self._update(sig["id"], same, state="skipped", next_check_at=None,
                             reason=f"{sym} trades about ${daily['dollar_volume'] / 1e6:,.0f} million a day, under the "
                                    f"${MIN_DOLLAR_VOLUME / 1e6:,.0f} million this screen needs to call a move.")
                return
            price, why = self._price(quote, session)
            if price is None:
                self._update(sig["id"], same, next_check_at=iso(now + timedelta(seconds=60)),
                             reason=f"Waiting for a price: {why}.")
                return
            if price < MIN_PRICE:
                self._update(sig["id"], same, state="skipped", next_check_at=None,
                             reason=f"{sym} is under ${MIN_PRICE:g}: too thin and too easily pushed to trade on news.")
                return
            if self.svc.market.info.mode == "demo" and "demo_move" in (item.get("extra") or {}):
                self._demo_runs[sig["id"]] = (time.time(), float(item["extra"]["demo_move"]))
            # The day is measured from the quote's previous close when it gives one (the same figure the movers
            # scan uses); otherwise from the last finished daily bar.
            prev = quote.get("prevclose")
            prev = float(prev) if isinstance(prev, (int, float)) and prev > 0 else daily["prev_close"]
            self._update(sig["id"], same, state="watching", ref_price=price, ref_at=iso(now),
                         prev_close=prev, atr=daily["atr"], last_price=price, last_at=iso(now),
                         next_check_at=iso(now + timedelta(seconds=CHECK_DELAYS[0])),
                         reason="Watching how the price answers the news.",
                         holdings_json=json.dumps(self.holdings(sym)))
            return

        price, why = self._price(quote, session, self._demo_runs.get(sig["id"]))
        if price is None:
            self._update(sig["id"], same, next_check_at=iso(now + timedelta(seconds=60)), reason=f"No price: {why}.")
            return
        received = parse_time(item.get("received_at"))
        published = parse_time(item.get("published_at"))
        late = bool(received and published and (received - published).total_seconds() >= LATE_SECONDS)
        verdict = judge(ref=sig["ref_price"], price=price, prev_close=sig["prev_close"], atr=sig["atr"], late=late,
                        settings=self.settings)
        checks = sig["checks_list"] + [{"at": iso(now), "price": price, "since_news_pct": verdict["since_news_pct"],
                                        "day_pct": verdict["day_pct"]}]
        n = sig["checks"] + 1
        next_at = iso(now + timedelta(seconds=CHECK_DELAYS[n])) if n < len(CHECK_DELAYS) else None
        fields: dict[str, Any] = {"last_price": price, "last_at": iso(now), "checks": n,
                                  "checks_json": json.dumps(checks[-12:]), "next_check_at": next_at}
        if sig["state"] == "watching":
            if verdict["direction"]:
                fields.update(self._opportunity(sig, item, verdict, price))
                # An opportunity has its own schedule: checked against its stop every ten minutes for an hour,
                # whichever of the news checks it confirmed on (the last of them included).
                fields.update(opportunity_at=iso(now),
                              next_check_at=iso(now + timedelta(seconds=OPPORTUNITY_CHECK_SECONDS)))
            elif next_at is None:
                fields.update(state="no_reaction", reason=(
                    f"No move worth trading after an hour: {verdict['since_news_pct']:+.1f}% since the news, "
                    f"{verdict['day_pct']:+.1f}% on the day."))
            else:
                fields["reason"] = (f"{verdict['since_news_pct']:+.1f}% since the news ({verdict['since_news_ranges']:+.2f} "
                                    f"daily ranges), {verdict['day_pct']:+.1f}% on the day: not yet a move.")
        elif sig["state"] == "opportunity":
            card = sig.get("card") or {}
            stop = card.get("stop")
            d = 1 if sig["direction"] == "LONG" else -1
            confirmed = parse_time(sig.get("opportunity_at")) or now
            if stop and d * (price - stop) <= 0:
                fields.update(state="stopped", next_check_at=None,
                              reason=f"{sym} traded at ${price:,.2f}, through the stop (${stop:,.2f}), at a check after the "
                                     "move confirmed: the move after the news has failed, and the idea with it.")
            elif now - confirmed >= timedelta(minutes=OPPORTUNITY_WATCH_MINUTES):
                fields.update(state="done", next_check_at=None,
                              reason=f"Watched for {OPPORTUNITY_WATCH_MINUTES} minutes after the move confirmed; last "
                                     f"${price:,.2f}. Nothing watches the stop after this: set a watch or your own "
                                     "stop order.")
            else:
                fields["next_check_at"] = iso(now + timedelta(seconds=OPPORTUNITY_CHECK_SECONDS))
        written = self._update(sig["id"], same, **fields)
        if written and fields.get("state") == "opportunity":
            self._notify(sig["id"])

    def _opportunity(self, sig: dict[str, Any], item: dict[str, Any], verdict: dict[str, Any],
                     price: float) -> dict[str, Any]:
        sym, direction = sig["symbol"], verdict["direction"]
        d = 1 if direction == "LONG" else -1
        suggested = None
        try:
            from .evaluator import suggest_levels
            suggested = suggest_levels(self.svc.market.enriched(sym, 3), d, price)
        except Exception as exc:                      # noqa: BLE001 - chart levels are a bonus
            log.info("Live news: no chart levels for %s: %s", sym, exc)
        stop, t1, t2, why = levels_for(direction, price, sig["ref_price"], sig["atr"], suggested, verdict["path"])
        tone = item.get("tone") or 0.0
        notes = []
        if tone and (tone > 0) != (d > 0):
            notes.append(f"The headline reads {'positive' if tone > 0 else 'negative'} but the price went "
                         f"{'up' if d > 0 else 'down'}: the price is what is traded, but the two disagree.")
        if verdict["path"] == "day_move":
            notes.append("This item reached the app after the stock had already moved: the move may have come from "
                         "this news or from something earlier.")
        card = None
        try:
            card = self.morning.card(symbol=sym, direction=direction, entry=price, stop=stop, target_1=t1,
                                     target_2=t2, horizon_days=LIVE_HORIZON, setup="news", levels_why=why)
            card["key"] = f"live-{sig['id']}"
            card["news"] = {"state": "news", "label": item.get("headline", "")[:120], "items": []}
            card["notes"] = notes + (card.get("notes") or [])
        except (DataError, ValueError) as exc:
            notes.append(f"No trade card: {exc}.")
        holdings = self.holdings(sym)
        for h in holdings:
            h["news"] = "with" if h["bias"] == d else "against"
        move = (f"{verdict['since_news_pct']:+.1f}% since the news ({verdict['since_news_ranges']:+.2f} daily ranges)"
                if verdict["path"] == "reaction" else f"{verdict['day_pct']:+.1f}% on the day "
                                                      f"({verdict['day_ranges']:+.2f} daily ranges)")
        return {"state": "opportunity", "direction": direction, "path": verdict["path"],
                "card_json": json.dumps(_clean(card)) if card else None, "holdings_json": json.dumps(holdings),
                "reason": f"{sym} moved {move}: moves with news have tended to keep drifting, so the trade is "
                          f"{'long' if d > 0 else 'short'}. " + " ".join(notes)}

    def holdings(self, symbol: str) -> list[dict[str, Any]]:
        """Open positions in the stock, each with the way it profits (+1 rising, -1 falling)."""
        try:
            rows = self.db.query("SELECT p.id, p.instrument_type, p.direction, p.quantity, p.open_quantity, "
                                 "oc.option_type, oc.strike, "
                                 "oc.expiration FROM positions p LEFT JOIN option_contracts oc ON oc.position_id = p.id "
                                 "AND oc.leg = 1 WHERE p.symbol = ? AND p.status IN ('OPEN', 'PARTIALLY_CLOSED')",
                                 (symbol,))
        except Exception:                             # noqa: BLE001 - no positions table: nothing held
            return []
        out = []
        for r in rows:
            qty = r["open_quantity"] if r["open_quantity"] else r["quantity"]      # what is still held, not the plan
            if r["instrument_type"] == "OPTION" and r["option_type"]:
                bias = 1 if r["option_type"] == "CALL" else -1
                what = f"{qty:g} {r['option_type'].lower()}{'s' if qty != 1 else ''} {r['strike']:g} {r['expiration']}"
            else:
                bias = 1 if r["direction"] == "LONG" else -1
                what = f"{'long' if bias > 0 else 'short'} {qty:g} shares"
            out.append({"position_id": r["id"], "bias": bias, "what": what})
        return out

    def _notify(self, signal_id: int) -> None:
        if not self.settings.live_news_email:
            return
        from . import mailer
        if not mailer.configured(self.settings):
            return
        today = datetime.now(NY).date().isoformat()
        sent = self.db.query("SELECT COUNT(*) AS n FROM live_signals WHERE emailed = 1 AND opened_at >= ?",
                             (iso(datetime.combine(datetime.now(NY).date(), datetime.min.time(), NY)),))[0]["n"]
        if sent >= MAX_EMAILS_A_DAY:
            log.info("Live news: %d emails today already; no more until tomorrow", sent)
            return
        # Claim the email first, so however many checks reach here, one email goes out.
        with self.db.tx() as cur:
            cur.execute("UPDATE live_signals SET emailed = 1 WHERE id = ? AND emailed = 0", (signal_id,))
            if cur.rowcount == 0:
                return
        row = self.db.one("SELECT * FROM live_signals WHERE id = ?", (signal_id,))
        if not row:
            return
        sig = signal_row(row)
        news = self.db.one("SELECT * FROM live_news WHERE id = ?", (sig["news_id"],))
        headline = news["headline"] if news else ""
        card = sig.get("card") or {}
        held = [h for h in sig["holdings"] if h.get("news") == "against"]
        verb = "Buy" if sig["direction"] == "LONG" else "Short"
        subject = (f"News against your {sig['symbol']} position" if held else
                   f"{verb} {sig['symbol']}: price confirms the news")
        lines = [f"{sig['symbol']}: {headline}", "", sig.get("reason") or ""]
        if held:
            lines += ["", "You hold: " + "; ".join(h["what"] for h in held) + ". The news moved the stock against it: "
                      "consider selling (or covering) or tightening the stop."]
        if card.get("order_text"):
            lines += ["", f"Order to consider: {card['order_text']}"]
        lines += ["", f"Open the Live news screen for the full card. ({today}) Nothing has been ordered."]
        try:
            mailer.send(self.settings, subject, "\n".join(lines))
        except Exception as exc:                      # noqa: BLE001 - a failed email never stops the feed
            log.warning("Live news email failed: %s", redact(str(exc))[:200])
            with self.db.tx() as cur:                 # not sent: it does not count toward the day's emails
                cur.execute("UPDATE live_signals SET emailed = 0 WHERE id = ?", (signal_id,))

    # --- what the screen reads -----------------------------------------------------------------------------
    def status(self) -> dict[str, Any]:
        with self._lock:
            sources = [dict(v) for k, v in self.sources.items() if k in self.source_names()]
        return {"running": self.running, "stopping": self.stopping,
                "started_at": self.started_at if self.running else None,
                "sources": sources, "setup_notes": self.setup_notes(), "session": self.session(),
                "universe": self.settings.live_universe, "universe_size": len(self.universe()),
                "email": bool(self.settings.live_news_email), "mode": self.svc.market.info.mode,
                "search": self.search.status(),
                "mover_scan_minutes": float(getattr(self.settings, "live_mover_sweep_minutes", 0) or 0),
                "thresholds": {"reaction_pct": self.settings.live_min_reaction_pct,
                               "reaction_ranges": self.settings.live_min_reaction_atr,
                               "late_day_ranges": self.settings.live_min_day_atr,
                               "late_day_pct": self.settings.live_min_day_pct}}

    def snapshot(self, after: int = 0, limit: int = 150, all_symbols: bool = False) -> dict[str, Any]:
        rows = self.db.query("SELECT * FROM live_news WHERE id > ? ORDER BY id DESC LIMIT ?", (after, max(1, limit)))
        items = [item_row(r) for r in rows]
        universe = self.universe()
        for it in items:
            it["tradable"] = [s for s in it["symbols"] if self.settings.live_universe == "all" or s in universe]
        if not all_symbols:
            items = [it for it in items if it["tradable"]]
        since = iso(now_utc() - timedelta(hours=24))
        signals = [signal_row(r) for r in self.db.query(
            "SELECT * FROM live_signals WHERE opened_at >= ? OR state IN ('waiting', 'watching', 'opportunity') "
            "ORDER BY id DESC LIMIT 200", (since,))]
        heads = {}
        ids = sorted({i for s in signals for i in s["news_ids"]})
        for i in range(0, len(ids), 500):
            chunk = ids[i:i + 500]
            for r in self.db.query(f"SELECT * FROM live_news WHERE id IN ({','.join('?' * len(chunk))})", chunk):
                heads[r["id"]] = item_row(r)
        for s in signals:
            s["news"] = [heads[i] for i in s["news_ids"] if i in heads]
        last = self.db.one("SELECT MAX(id) AS m FROM live_news")
        return {**self.status(), "items": items, "signals": signals, "movers": self.movers(),
                "last_id": (last["m"] if last else 0) or 0, "checked_at": iso(now_utc())}


def _clean(value: Any) -> Any:
    from .service import clean
    return clean(value)


__all__ = ["LiveFeed", "RtprClient", "rtpr_article", "parse_sec_current", "sec_headline", "finnhub_items",
           "judge", "levels_for", "make_item", "store_item", "clean_symbols", "headline_from_text", "demo_item"]
