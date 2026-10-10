"""The AI plug-in: requests to Anthropic's Messages API, with a daily limit, a cache and a record
of usage.

The key is read from ANTHROPIC_API_KEY and sent only in the request header to
api.anthropic.com. It is never stored, logged, returned by any endpoint or sent to the browser.
What is sent is what the caller puts in the prompt: tickers, prices, indicators, option
figures and headlines. Account size, positions, the journal and every other key stay here.

With web search on, the model can look up the latest news itself (Anthropic's server-side
web search tool); the pages it used come back as sources, shown with every answer.
Answers are what a language model wrote from those inputs: they can be wrong, and the
screens say so and show the sources to check.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import time
from datetime import datetime, timezone
from typing import Any

import requests

from .events import emit, redact
from .optionmath import NY

API_URL = "https://api.anthropic.com/v1/messages"
API_VERSION = "2023-06-01"
WEB_SEARCH_TOOL = "web_search_20250305"
MAX_CONTINUATIONS = 3          # a long web-search turn can pause; it is resumed at most this many times
TIMEOUT = (10, 150)

BASE_SYSTEM = (
    "You write for an individual options trader inside a research tool that never places orders. "
    "Use the data you are given and, when you search, reputable recent sources. Never invent a number, a date, "
    "a quote or a source; when something is not known, say so. Do not tell the reader to buy, sell or hold, and "
    "do not give personal financial advice: describe what the evidence shows, what it implies, and the risks. "
    "Prices implied by options are what the market prices in, not forecasts. Be concise and plain."
)


class AIError(RuntimeError):
    """The AI request could not be made or did not succeed. The message is safe to show."""


def extract_json(text: str) -> Any | None:
    """The last JSON object in `text` (fenced or bare), or None."""
    if not text:
        return None
    fenced = re.findall(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.S)
    for chunk in reversed(fenced):
        try:
            return json.loads(chunk)
        except json.JSONDecodeError:
            continue
    # The last complete top-level object in the text, read with the JSON parser itself so braces
    # inside strings do not confuse it.
    decoder, found, i = json.JSONDecoder(), None, text.find("{")
    while i != -1:
        try:
            obj, end = decoder.raw_decode(text, i)
        except json.JSONDecodeError:
            i = text.find("{", i + 1)
            continue
        if isinstance(obj, dict):
            found = obj
        i = text.find("{", end)
    if found is not None:
        return found
    return None


def _texts(value: Any, limit: int = 8, size: int = 400) -> list[str]:
    """A list of short strings from whatever the model put there."""
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, list):
        return []
    return [str(v).strip()[:size] for v in value if isinstance(v, (str, int, float)) and str(v).strip()][:limit]


def _text(value: Any, size: int = 1500) -> str | None:
    return str(value).strip()[:size] if isinstance(value, (str, int, float)) and str(value).strip() else None


def parse_response(body: dict[str, Any]) -> dict[str, Any]:
    """Text, sources and usage from a Messages API response."""
    parts, sources, seen = [], [], {}
    searches = []
    for block in body.get("content") or []:
        kind = block.get("type")
        if kind == "text":
            parts.append(block.get("text") or "")
            for c in block.get("citations") or []:
                url = c.get("url")
                if isinstance(url, str) and url.startswith(("https://", "http://")):
                    s = seen.get(url)
                    if s is None:
                        s = seen[url] = {"url": url, "title": c.get("title") or url, "cited": True, "quotes": []}
                        sources.append(s)
                    s["cited"] = True
                    if c.get("cited_text") and len(s["quotes"]) < 2:
                        s["quotes"].append(str(c["cited_text"])[:300])
        elif kind == "server_tool_use" and block.get("name") == "web_search":
            q = (block.get("input") or {}).get("query")
            if q:
                searches.append(str(q)[:200])
        elif kind == "web_search_tool_result":
            content = block.get("content")
            if isinstance(content, list):
                for r in content:
                    url = r.get("url") if isinstance(r, dict) else None
                    if isinstance(url, str) and url.startswith(("https://", "http://")) and url not in seen:
                        seen[url] = {"url": url, "title": r.get("title") or url, "cited": False, "quotes": [],
                                     "page_age": r.get("page_age")}
                        sources.append(seen[url])
    usage = body.get("usage") or {}
    sources.sort(key=lambda s: not s["cited"])
    return {"text": "".join(parts).strip(), "sources": sources, "searches": searches,
            "usage": {"input_tokens": int(usage.get("input_tokens") or 0),
                      "output_tokens": int(usage.get("output_tokens") or 0),
                      "web_searches": int((usage.get("server_tool_use") or {}).get("web_search_requests") or 0)},
            "stop_reason": body.get("stop_reason")}


class AIClient:
    def __init__(self, settings: Any, db: Any, session: requests.Session | None = None):
        self.settings = settings
        self.db = db
        self.session = session or requests.Session()
        self._last_error: dict[str, Any] | None = None

    # --- state ---------------------------------------------------------------------------------
    @property
    def configured(self) -> bool:
        return bool(self.settings.anthropic_api_key) and self.settings.features.get("ai_assistant", True) \
            and self.settings.ai_daily_requests > 0

    def _today(self) -> str:
        return datetime.now(NY).date().isoformat()

    def used_today(self) -> dict[str, int]:
        row = self.db.one("SELECT COUNT(*) AS n, COALESCE(SUM(input_tokens), 0) AS i, COALESCE(SUM(output_tokens), 0) AS o, "
                          "COALESCE(SUM(web_searches), 0) AS w, COALESCE(SUM(1 - ok), 0) AS f FROM ai_usage WHERE day = ?",
                          (self._today(),))
        return {"requests": int(row["n"]), "input_tokens": int(row["i"]), "output_tokens": int(row["o"]),
                "web_searches": int(row["w"]), "failed": int(row["f"])}

    def status(self) -> dict[str, Any]:
        """What the screens and the health view show. Never the key."""
        s = self.settings
        used = self.used_today()
        reason = None
        if not s.anthropic_api_key:
            reason = "ANTHROPIC_API_KEY is not set in .env"
        elif not s.features.get("ai_assistant", True):
            reason = "switched off (FEATURE_AI_ASSISTANT=0)"
        elif s.ai_daily_requests <= 0:
            reason = "switched off (AI_DAILY_REQUESTS=0)"
        return {"configured": self.configured, "reason": reason, "model": s.ai_model, "web_search": s.ai_web_search,
                "daily_limit": s.ai_daily_requests, "used_today": used,
                "remaining_today": max(s.ai_daily_requests - used["requests"], 0), "last_error": self._last_error,
                "provider": "Anthropic Messages API"}

    def _record(self, purpose: str, usage: dict[str, int], ok: bool, error: str | None = None) -> None:
        with self.db.tx() as cur:
            cur.execute("INSERT INTO ai_usage (at, day, purpose, model, input_tokens, output_tokens, web_searches, ok, error) "
                        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                        (datetime.now(timezone.utc).isoformat(timespec="seconds"), self._today(), purpose,
                         self.settings.ai_model, usage.get("input_tokens", 0), usage.get("output_tokens", 0),
                         usage.get("web_searches", 0), 1 if ok else 0, redact(error)[:300] if error else None))

    # --- requests ------------------------------------------------------------------------------
    def ask(self, *, purpose: str, prompt: str, system: str = BASE_SYSTEM, max_tokens: int = 1500,
            web_search: bool = False, cache_key: str | None = None, max_age: float = 1800,
            refresh: bool = False, max_searches: int = 4) -> dict[str, Any]:
        """One question to the model. Returns text, sources and usage; raises AIError with a safe message."""
        if not self.configured:
            raise AIError("AI is not set up: " + (self.status()["reason"] or "unavailable"))
        web_search = bool(web_search and self.settings.ai_web_search)
        key = None
        if cache_key is not None:
            digest = hashlib.sha256(f"{self.settings.ai_model}|{web_search}|{cache_key}".encode()).hexdigest()[:24]
            key = f"ai:{purpose}:{digest}"
            if not refresh:
                hit = self.db.cache_get(key, max_age)
                if hit is not None:
                    return {**hit, "cached": True}
        if self.used_today()["requests"] >= self.settings.ai_daily_requests:
            raise AIError(f"Today's AI limit of {self.settings.ai_daily_requests} requests is used up "
                          "(AI_DAILY_REQUESTS in .env). It resets at midnight New York time.")
        body: dict[str, Any] = {"model": self.settings.ai_model, "max_tokens": int(max_tokens), "system": system,
                                "messages": [{"role": "user", "content": prompt}]}
        if web_search:
            body["tools"] = [{"type": WEB_SEARCH_TOOL, "name": "web_search", "max_uses": int(max_searches)}]
        total = {"input_tokens": 0, "output_tokens": 0, "web_searches": 0}
        content: list[dict[str, Any]] = []
        started = time.time()
        try:
            for _ in range(MAX_CONTINUATIONS + 1):
                reply = self._post(body)
                parsed_part = parse_response(reply)
                for k in total:
                    total[k] += parsed_part["usage"][k]
                content.extend(reply.get("content") or [])
                if reply.get("stop_reason") != "pause_turn":
                    break
                body = {**body, "messages": [{"role": "user", "content": prompt},
                                             {"role": "assistant", "content": content}]}
        except AIError as exc:
            self._record(purpose, total, False, str(exc))
            self._last_error = {"at": datetime.now(timezone.utc).isoformat(timespec="seconds"), "error": str(exc)}
            emit("ai_request_failed", logging.WARNING, purpose=purpose, error=exc)
            raise
        parsed = parse_response({"content": content, "usage": {}, "stop_reason": None})
        self._record(purpose, total, True)
        out = {"text": parsed["text"], "sources": parsed["sources"], "searches": parsed["searches"], "usage": total,
               "model": self.settings.ai_model, "web_search": web_search,
               "at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
               "seconds": round(time.time() - started, 1), "cached": False}
        emit("ai_request", purpose=purpose, model=self.settings.ai_model, input_tokens=total["input_tokens"],
             output_tokens=total["output_tokens"], web_searches=total["web_searches"])
        if key:
            self.db.cache_put(key, out)
        return out

    def _post(self, body: dict[str, Any]) -> dict[str, Any]:
        headers = {"x-api-key": self.settings.anthropic_api_key, "anthropic-version": API_VERSION,
                   "content-type": "application/json"}
        try:
            r = self.session.post(API_URL, headers=headers, json=body, timeout=TIMEOUT)
        except requests.Timeout as exc:
            raise AIError("The AI request timed out. Try again, or switch web search off for a quicker answer.") from exc
        except requests.RequestException as exc:
            raise AIError(f"Could not reach the Anthropic API ({exc.__class__.__name__}).") from exc
        if r.status_code == 200:
            try:
                return r.json()
            except ValueError as exc:
                raise AIError("The Anthropic API sent a reply this app could not read.") from exc
        try:
            detail = (r.json().get("error") or {}).get("message") or ""
        except ValueError:
            detail = ""
        detail = redact(detail)[:300]
        if r.status_code in (401, 403):
            raise AIError("The Anthropic API refused the key. Check ANTHROPIC_API_KEY in .env.")
        if r.status_code == 404:
            raise AIError(f"The Anthropic API does not know the model {self.settings.ai_model!r}. Check AI_MODEL in .env."
                          + (f" ({detail})" if detail else ""))
        if r.status_code == 429:
            raise AIError("The Anthropic API is rate-limiting this key. Try again in a minute."
                          + (f" ({detail})" if detail else ""))
        if r.status_code in (500, 502, 503, 529):
            raise AIError("The Anthropic API is busy or unavailable right now. Try again shortly.")
        raise AIError(f"The Anthropic API refused the request ({r.status_code})" + (f": {detail}" if detail else "."))


__all__ = ["AIClient", "AIError", "extract_json", "parse_response", "BASE_SYSTEM"]
