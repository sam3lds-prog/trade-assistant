"""Headlines for a stock and for the market, and their tone.

Headlines come from Finnhub (FINNHUB_API_KEY, free at finnhub.io) and the SEC's list of a
company's recent filings (SEC_USER_AGENT). With demo market data and no Finnhub key, made-up
demo headlines fill the screen, marked as such.

Tone is read two ways:
- by a word list (always available): headlines with words such as "beats", "raises" or
  "upgrade" count as positive, "misses", "cuts", "downgrade" or "probe" as negative. It counts
  words; it does not understand them.
- by the AI model, when it is set up (briefs.py): a score from -1 to +1 for each headline
  with a reason.

Either way it is the tone of recent headlines, not a forecast of the price.
"""

from __future__ import annotations

import hashlib
import logging
import re
from datetime import datetime, timedelta, timezone
from typing import Any

import requests

from .events import emit, redact
from .optionmath import NY

FINNHUB = "https://finnhub.io/api/v1"
CACHE_SECONDS = 600
MAX_ITEMS = 40

POSITIVE = ("beat", "beats", "tops", "raises", "raised", "upgrade", "upgraded", "record", "surge", "surges", "soar",
            "soars", "jumps", "rally", "rallies", "gains", "strong", "growth", "approval", "approved", "wins", "win",
            "outperform", "buyback", "boosts", "expands", "profit", "rebound", "rebounds", "bullish", "higher", "climbs")
NEGATIVE = ("miss", "misses", "missed", "cuts", "cut", "downgrade", "downgraded", "lawsuit", "sues", "probe",
            "investigation", "recall", "plunge", "plunges", "slump", "slumps", "falls", "drops", "tumbles", "sinks",
            "weak", "warning", "warns", "layoffs", "fraud", "loss", "losses", "bearish", "lower", "decline", "declines",
            "halt", "halts", "delay", "delays", "fine", "fined", "breach", "bankruptcy", "default")
_POS = re.compile(r"\b(" + "|".join(POSITIVE) + r")\b", re.I)
_NEG = re.compile(r"\b(" + "|".join(NEGATIVE) + r")\b", re.I)

FORM_WORDS = {"8-K": "Current report", "10-Q": "Quarterly report", "10-K": "Annual report", "6-K": "Foreign issuer report",
              "S-1": "Registration", "4": "Insider transaction", "SC 13D": "Ownership over 5%", "SC 13G": "Ownership over 5%",
              "DEF 14A": "Proxy statement"}


def word_tone(text: str) -> int:
    """+1, 0 or -1 from the word list."""
    pos, neg = len(_POS.findall(text or "")), len(_NEG.findall(text or ""))
    return (pos > neg) - (neg > pos)


def summarize_tone(items: list[dict[str, Any]], key: str = "tone") -> dict[str, Any]:
    """Counts and the average tone of the items that have one."""
    scored = [i[key] for i in items if isinstance(i.get(key), (int, float))]
    if not scored:
        return {"count": 0, "positive": 0, "negative": 0, "neutral": 0, "average": None, "lean": None}
    pos = sum(1 for t in scored if t > 0.15)
    neg = sum(1 for t in scored if t < -0.15)
    avg = sum(scored) / len(scored)
    lean = "POSITIVE" if avg > 0.15 else "NEGATIVE" if avg < -0.15 else "MIXED"
    return {"count": len(scored), "positive": pos, "negative": neg, "neutral": len(scored) - pos - neg,
            "average": avg, "lean": lean}


def _item(headline: str, summary: str, source: str, url: str | None, published: str | None, kind: str,
          related: str | None = None) -> dict[str, Any]:
    headline = (headline or "").strip()
    ident = hashlib.sha1(f"{headline}|{url}|{published}".encode()).hexdigest()[:12]
    return {"id": ident, "headline": headline[:300], "summary": (summary or "").strip()[:600], "source": source,
            "url": url, "published": published, "kind": kind, "related": related, "tone": word_tone(headline),
            "tone_method": "words"}


def demo_headlines(symbol: str | None, today: datetime) -> list[dict[str, Any]]:
    """Made-up headlines for demo mode, the same for a given symbol and day."""
    subject = symbol or "Stocks"
    if symbol is None:
        templates = [
            ("Stocks edge higher as bond yields ease", "Treasury yields slipped after softer data. (Demo headline.)"),
            ("Tech shares lead a broad rally", "Chipmakers rose the most. (Demo headline.)"),
            ("Oil falls as supply worries fade", "Crude dropped for a third day. (Demo headline.)"),
            ("Investors await inflation figures due next week", "Economists expect a small rise. (Demo headline.)"),
            ("Bank stocks slip after a regulator's warning", "Lenders fell on capital concerns. (Demo headline.)"),
            ("Small caps record their best week in a month", "The Russell 2000 gained. (Demo headline.)"),
        ]
    else:
        templates = [
            ("{s} shares rise after an analyst upgrade", "A broker raised its rating, citing demand. (Demo headline.)"),
            ("{s} faces a regulatory probe into its pricing", "Regulators asked for documents. (Demo headline.)"),
            ("{s} to report quarterly results later this month", "The company confirmed the date. (Demo headline.)"),
            ("Options traders pile into {s} calls ahead of the event", "Volume ran above open interest. (Demo headline.)"),
            ("{s} slips as sector peers fall", "Shares tracked weakness across the group. (Demo headline.)"),
            ("{s} announces a share buyback", "The board approved a repurchase plan. (Demo headline.)"),
        ]
    seed = int(hashlib.sha256(f"{subject}|{today.date()}".encode()).hexdigest()[:8], 16)
    out = []
    for i in range(5):
        h, s = templates[(seed + i * 7) % len(templates)]
        when = (today - timedelta(hours=3 + 9 * i)).astimezone(timezone.utc).isoformat(timespec="seconds")
        out.append(_item(h.format(s=subject), s, "Demo", None, when, "news", symbol))
    return out


class NewsClient:
    def __init__(self, settings: Any, db: Any, sec: Any = None, session: requests.Session | None = None):
        self.settings = settings
        self.db = db
        self.sec = sec
        self.session = session or requests.Session()
        self._last_error: dict[str, Any] | None = None

    @property
    def configured(self) -> bool:
        return bool(self.settings.finnhub_api_key)

    @property
    def demo(self) -> bool:
        return not self.configured and self.settings.resolved_mode == "demo"

    def status(self) -> dict[str, Any]:
        return {"finnhub": self.configured, "sec": bool(self.sec and self.sec.enabled), "demo_headlines": self.demo,
                "last_error": self._last_error}

    def _finnhub(self, path: str, params: dict[str, Any]) -> Any:
        try:
            r = self.session.get(f"{FINNHUB}/{path}", params={**params, "token": self.settings.finnhub_api_key},
                                 timeout=(5, 20))
        except requests.RequestException as exc:
            raise RuntimeError(f"Could not reach Finnhub ({exc.__class__.__name__})") from exc
        if r.status_code in (401, 403):
            raise RuntimeError("Finnhub refused the key. Check FINNHUB_API_KEY in .env.")
        if r.status_code == 429:
            raise RuntimeError("Finnhub's rate limit was reached (free keys allow about 60 requests a minute).")
        if r.status_code != 200:
            raise RuntimeError(f"Finnhub returned {r.status_code}")
        try:
            return r.json()
        except ValueError as exc:
            raise RuntimeError("Finnhub sent a reply this app could not read") from exc

    def _failed(self, what: str, exc: Exception) -> str:
        msg = redact(str(exc))
        self._last_error = {"at": datetime.now(timezone.utc).isoformat(timespec="seconds"), "what": what, "error": msg}
        emit("news_failed", logging.WARNING, what=what, error=msg)
        return msg

    @staticmethod
    def _from_finnhub(rows: Any, symbol: str | None) -> list[dict[str, Any]]:
        out = []
        for r in rows if isinstance(rows, list) else []:
            if not isinstance(r, dict) or not r.get("headline"):
                continue
            ts = r.get("datetime")
            when = datetime.fromtimestamp(ts, timezone.utc).isoformat(timespec="seconds") if isinstance(ts, (int, float)) and ts > 0 else None
            url = r.get("url") if isinstance(r.get("url"), str) and r["url"].startswith("http") else None
            out.append(_item(r["headline"], r.get("summary") or "", r.get("source") or "Finnhub", url, when, "news",
                             symbol or (r.get("related") or None)))
        return out

    def _filings(self, symbol: str, days: int) -> list[dict[str, Any]]:
        if not (self.sec and self.sec.enabled):
            return []
        from .research import recent_filings
        cik, _ = self.sec.cik_for(symbol)
        rows = recent_filings(self.sec.submissions(cik), cik, limit=20)
        cutoff = (datetime.now(NY).date() - timedelta(days=max(days, 30))).isoformat()
        out = []
        for f in rows:
            if f["filed"] < cutoff:
                continue
            items = ", ".join(i["label"] for i in f.get("items") or [])
            head = f"{f['form']} filed: {FORM_WORDS.get(f['form'], f.get('description') or 'filing')}" + (f" ({items})" if items else "")
            it = _item(head, f.get("description") or "", "SEC EDGAR", f["url"], f["filed"], "filing", symbol)
            it["tone"], it["tone_method"] = None, None           # a filing's title says nothing about its tone
            out.append(it)
        return out

    def company(self, symbol: str, days: int = 7) -> dict[str, Any]:
        symbol = symbol.upper()
        days = max(1, min(int(days), 30))
        key = f"news:company:{symbol}:{days}:{self.configured}:{self.demo}"
        hit = self.db.cache_get(key, CACHE_SECONDS)
        if hit is not None:
            return {**hit, "cached": True}
        items, errors, sources = [], [], []
        today = datetime.now(NY)
        if self.configured:
            try:
                rows = self._finnhub("company-news", {"symbol": symbol,
                                                      "from": (today.date() - timedelta(days=days)).isoformat(),
                                                      "to": today.date().isoformat()})
                items += self._from_finnhub(rows, symbol)
                sources.append("Finnhub")
            except RuntimeError as exc:
                errors.append(self._failed("company news", exc))
        elif self.demo:
            items += demo_headlines(symbol, today)
            sources.append("Demo")
        try:
            filings = self._filings(symbol, days)
            if filings:
                sources.append("SEC EDGAR")
            items += filings
        except Exception as exc:                         # noqa: BLE001 - filings are a bonus; their failure is reported
            if "ticker list" not in str(exc):            # funds and some foreign companies do not file: not a failure
                errors.append(self._failed("SEC filings", exc))
        items = self._dedupe(items)[:MAX_ITEMS]
        out = {"symbol": symbol, "days": days, "items": items, "tone": summarize_tone(items), "sources": sources,
               "errors": errors, "finnhub": self.configured, "demo": self.demo,
               "checked_at": datetime.now(timezone.utc).isoformat(timespec="seconds"), "notes": self._notes()}
        if not errors:
            self.db.cache_put(key, out)
        return {**out, "cached": False}

    def market(self, category: str = "general") -> dict[str, Any]:
        key = f"news:market:{category}:{self.configured}:{self.demo}"
        hit = self.db.cache_get(key, CACHE_SECONDS)
        if hit is not None:
            return {**hit, "cached": True}
        items, errors = [], []
        if self.configured:
            try:
                items = self._from_finnhub(self._finnhub("news", {"category": category}), None)
            except RuntimeError as exc:
                errors.append(self._failed("market news", exc))
        elif self.demo:
            items = demo_headlines(None, datetime.now(NY))
        items = self._dedupe(items)[:MAX_ITEMS]
        out = {"category": category, "items": items, "tone": summarize_tone(items), "errors": errors,
               "finnhub": self.configured, "demo": self.demo, "notes": self._notes(),
               "checked_at": datetime.now(timezone.utc).isoformat(timespec="seconds")}
        if not errors:
            self.db.cache_put(key, out)
        return {**out, "cached": False}

    def _notes(self) -> list[str]:
        notes = ["Tone by word list counts words such as \"beats\" or \"downgrade\"; it does not read meaning. It is the "
                 "tone of headlines, not a forecast."]
        if self.demo:
            notes.insert(0, "Demo headlines: made up. Set FINNHUB_API_KEY in .env for real ones.")
        elif not self.configured:
            notes.insert(0, "No news source is set: add FINNHUB_API_KEY to .env (a free key from finnhub.io) for "
                            "headlines. SEC filings still appear when SEC_USER_AGENT is set.")
        return notes

    @staticmethod
    def _dedupe(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
        seen, out = set(), []
        for it in sorted(items, key=lambda i: i.get("published") or "", reverse=True):
            k = re.sub(r"\W+", " ", it["headline"].lower()).strip()
            if k in seen:
                continue
            seen.add(k)
            out.append(it)
        return out


__all__ = ["NewsClient", "word_tone", "summarize_tone", "demo_headlines"]
