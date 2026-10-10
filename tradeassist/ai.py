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
import math
import re
import threading
import time
from datetime import datetime, timezone
from typing import Any, Callable

import requests

from .events import emit, redact
from .optionmath import NY

API_URL = "https://api.anthropic.com/v1/messages"
API_VERSION = "2023-06-01"
WEB_SEARCH_TOOL = "web_search_20250305"
MAX_CONTINUATIONS = 2          # a long web-search turn can pause; it is resumed at most this many times
TIMEOUT = (10, 150)
MAX_SECONDS = 300              # one question may take at most this long, however many calls it needs
IN_PROGRESS = "in progress"
_reserve_lock = threading.Lock()

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
    try:
        return _extract_json(text)
    except RecursionError:              # absurdly deep nesting is not an answer
        return None


CITE_TAG = re.compile(r"</?cite\b[^>]*>")
CITE_TAIL = re.compile(r"</?cite\b[^>]*$")        # a tag cut off at the end of a reply


def strip_markup(text: str) -> str:
    """Citation tags the model sometimes writes into its text (the sources are listed separately)."""
    return CITE_TAIL.sub("", CITE_TAG.sub("", text))


def _repair(s: str) -> Any | None:
    """A JSON object cut off part-way (a reply that ran out of length), closed so it can be read:
    the open string is ended, a dangling key or trailing comma dropped, and open brackets closed."""
    stack: list[str] = []
    in_str = esc = False
    for ch in s:
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch in "{[":
            stack.append(ch)
        elif ch in "}]":
            if not stack:
                return None
            stack.pop()
            if not stack:
                return None                         # complete, so not a cut-off object
    if not stack:
        return None
    t = s[:-1] if (in_str and esc) else s
    if in_str:
        t += '"'
    t = t.rstrip()
    for _ in range(3):
        before = t
        t = re.sub(r",\s*$", "", t)
        if stack[-1] == "{":
            t = re.sub(r'([{,])\s*"(?:[^"\\]|\\.)*"\s*:?\s*$', r"\1", t)    # a key with no value yet
            t = re.sub(r",\s*$", "", t)
        if t == before:
            break
    t += "".join("}" if c == "{" else "]" for c in reversed(stack))
    try:
        obj = json.loads(t)
    except json.JSONDecodeError:
        return None
    return obj if isinstance(obj, dict) and obj else None


def _extract_json(text: str) -> Any | None:
    text = CITE_TAG.sub("", text)
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
            # An object that runs to the end of the text was cut off: close it and use it.
            fixed = _repair(re.sub(r"\s*```\s*$", "", text[i:]))
            if fixed is not None:
                return fixed
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
    out = [strip_markup(str(v)).strip()[:size] for v in value if isinstance(v, (str, int, float))]
    return [v for v in out if v][:limit]


def _text(value: Any, size: int = 1500) -> str | None:
    if not isinstance(value, (str, int, float)):
        return None
    v = strip_markup(str(value)).strip()[:size]
    return v or None


def _count(value: Any) -> int:
    """A token or search count from the reply; anything that is not a sensible number counts as 0."""
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value < 0:
        return 0
    return int(min(value, 10**9))


def _web_url(value: Any) -> str | None:
    return value if isinstance(value, str) and re.match(r"https?://", value, re.I) else None


def parse_response(body: Any) -> dict[str, Any]:
    """Text, sources and usage from a Messages API response, whatever its shape: blocks, citations
    and results that are not what the API documents are skipped."""
    body = body if isinstance(body, dict) else {}
    parts, sources, seen = [], [], {}
    searches = []
    blocks = body.get("content")
    for block in blocks if isinstance(blocks, list) else []:
        if not isinstance(block, dict):
            continue
        kind = block.get("type")
        if kind == "text":
            if isinstance(block.get("text"), str):
                parts.append(block["text"])
            cites = block.get("citations")
            for c in cites if isinstance(cites, list) else []:
                if not isinstance(c, dict) or not (url := _web_url(c.get("url"))):
                    continue
                src = seen.get(url)
                if src is None:
                    src = seen[url] = {"url": url, "title": _text(c.get("title"), 300) or url, "cited": True, "quotes": []}
                    sources.append(src)
                src["cited"] = True
                if _text(c.get("cited_text")) and len(src["quotes"]) < 2:
                    src["quotes"].append(_text(c["cited_text"], 300))
        elif kind == "server_tool_use" and block.get("name") == "web_search":
            q = block.get("input").get("query") if isinstance(block.get("input"), dict) else None
            if _text(q):
                searches.append(_text(q, 200))
        elif kind == "web_search_tool_result":
            results = block.get("content")
            for r in results if isinstance(results, list) else []:
                url = _web_url(r.get("url")) if isinstance(r, dict) else None
                if url and url not in seen:
                    seen[url] = {"url": url, "title": _text(r.get("title"), 300) or url, "cited": False, "quotes": [],
                                 "page_age": _text(r.get("page_age"), 60)}
                    sources.append(seen[url])
    usage = body.get("usage") if isinstance(body.get("usage"), dict) else {}
    tools = usage.get("server_tool_use") if isinstance(usage.get("server_tool_use"), dict) else {}
    sources.sort(key=lambda src: not src["cited"])
    return {"text": "".join(parts).strip(), "sources": sources, "searches": searches,
            "usage": {"input_tokens": _count(usage.get("input_tokens")), "output_tokens": _count(usage.get("output_tokens")),
                      "web_searches": _count(tools.get("web_search_requests"))},
            "stop_reason": body.get("stop_reason") if isinstance(body.get("stop_reason"), str) else None,
            "content": [b for b in blocks if isinstance(b, dict)] if isinstance(blocks, list) else []}


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
                          "COALESCE(SUM(web_searches), 0) AS w, "
                          "COALESCE(SUM(CASE WHEN ok = 0 AND COALESCE(error, '') != ? THEN 1 ELSE 0 END), 0) AS f "
                          "FROM ai_usage WHERE day = ?", (IN_PROGRESS, self._today()))
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

    def _reserve(self, purpose: str) -> int:
        """Count a call against today's limit before it is made, so requests that overlap cannot pass
        the limit together. The row is filled in when the call ends."""
        with _reserve_lock:
            if self.used_today()["requests"] >= self.settings.ai_daily_requests:
                raise AIError(f"Today's AI limit of {self.settings.ai_daily_requests} requests is used up "
                              "(AI_DAILY_REQUESTS in .env). It resets at midnight New York time.")
            with self.db.tx() as cur:
                cur.execute("INSERT INTO ai_usage (at, day, purpose, model, ok, error) VALUES (?, ?, ?, ?, 0, ?)",
                            (datetime.now(timezone.utc).isoformat(timespec="seconds"), self._today(), purpose,
                             self.settings.ai_model, IN_PROGRESS))
                return int(cur.lastrowid)

    def _settle(self, row: int, usage: dict[str, int], ok: bool, error: str | None = None) -> None:
        with self.db.tx() as cur:
            cur.execute("UPDATE ai_usage SET input_tokens = ?, output_tokens = ?, web_searches = ?, ok = ?, error = ? "
                        "WHERE id = ?", (usage.get("input_tokens", 0), usage.get("output_tokens", 0),
                                         usage.get("web_searches", 0), 1 if ok else 0,
                                         redact(error)[:300] if error else None, row))

    # --- requests ------------------------------------------------------------------------------
    def ask(self, *, purpose: str, prompt: str, system: str = BASE_SYSTEM, max_tokens: int = 1500,
            web_search: bool = False, cache_key: str | None = None, max_age: float = 1800,
            refresh: bool = False, max_searches: int = 4,
            accept: Callable[[str], bool] | None = None) -> dict[str, Any]:
        """One question to the model. Returns text, sources and usage; raises AIError with a safe message.

        Every call to the API counts against the daily limit, including the calls that resume a paused
        web-search turn. An answer is saved for reuse only when `accept` (if given) takes its text, so a
        reply that could not be read is asked again next time rather than repeated from the cache."""
        if not self.configured:
            raise AIError("AI is not set up: " + (self.status()["reason"] or "unavailable"))
        web_search = bool(web_search and self.settings.ai_web_search)
        key = None
        if cache_key is not None:
            digest = hashlib.sha256(f"{self.settings.ai_model}|{web_search}|{cache_key}".encode()).hexdigest()[:24]
            key = f"ai:{purpose}:{digest}"
            if not refresh:
                hit = self.db.cache_get(key, max_age)
                if isinstance(hit, dict) and isinstance(hit.get("text"), str):
                    return {**hit, "cached": True}
        body: dict[str, Any] = {"model": self.settings.ai_model, "max_tokens": int(max_tokens), "system": system,
                                "messages": [{"role": "user", "content": prompt}]}
        if web_search:
            body["tools"] = [{"type": WEB_SEARCH_TOOL, "name": "web_search", "max_uses": int(max_searches)}]
        total = {"input_tokens": 0, "output_tokens": 0, "web_searches": 0}
        content: list[dict[str, Any]] = []
        started = time.time()
        finished = False
        stop = None
        for _ in range(MAX_CONTINUATIONS + 1):
            row = self._reserve(purpose)
            usage = {"input_tokens": 0, "output_tokens": 0, "web_searches": 0}
            try:
                if time.time() - started > MAX_SECONDS:
                    raise AIError("The AI request took too long and was stopped. Try again, or switch web search off.")
                part = parse_response(self._post(body))
                usage = part["usage"]
                content.extend(part["content"])
                stop = part["stop_reason"]
                finished = stop != "pause_turn"
                self._settle(row, usage, True)
            except Exception as exc:                   # noqa: BLE001 - every failure is recorded, then shown safely
                err = exc if isinstance(exc, AIError) else AIError(
                    f"The Anthropic API's reply could not be used ({exc.__class__.__name__}).")
                self._settle(row, usage, False, str(err))
                self._last_error = {"at": datetime.now(timezone.utc).isoformat(timespec="seconds"), "error": str(err)}
                emit("ai_request_failed", logging.WARNING, purpose=purpose, error=err)
                raise err from exc
            for k in total:
                total[k] += usage[k]
            if finished:
                break
            body = {**body, "messages": [{"role": "user", "content": prompt}, {"role": "assistant", "content": content}]}
        if not finished:
            err = AIError("The model was still searching after several rounds; its answer is incomplete. Try again, "
                          "or switch web search off.")
            self._last_error = {"at": datetime.now(timezone.utc).isoformat(timespec="seconds"), "error": str(err)}
            emit("ai_request_failed", logging.WARNING, purpose=purpose, error=err)
            raise err
        parsed = parse_response({"content": content})
        out = {"text": parsed["text"], "sources": parsed["sources"], "searches": parsed["searches"], "usage": total,
               "model": self.settings.ai_model, "web_search": web_search,
               "at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
               "seconds": round(time.time() - started, 1), "cached": False,
               "truncated": stop == "max_tokens"}
        emit("ai_request", purpose=purpose, model=self.settings.ai_model, input_tokens=total["input_tokens"],
             output_tokens=total["output_tokens"], web_searches=total["web_searches"])
        # A reply cut off at the length limit is shown, but asked again next time rather than kept.
        if key and not out["truncated"] and (accept is None or _accepts(accept, out["text"])):
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


def _accepts(accept: Callable[[str], bool], text: str) -> bool:
    try:
        return bool(accept(text))
    except Exception:                                  # noqa: BLE001 - a checker that fails means "do not keep it"
        return False


__all__ = ["AIClient", "AIError", "extract_json", "parse_response", "BASE_SYSTEM"]
