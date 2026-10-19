"""ChatGPT for the what-if widget (2.13): requests to OpenAI's Chat Completions API, counted against the
same daily limit, in the same `ai_usage` table, as the Anthropic requests.

The key is read from OPENAI_API_KEY and sent only in the request header to api.openai.com. It is never
stored, logged, returned by any endpoint or sent to the browser. What is sent is what the caller puts in the
prompt: tickers, prices, indicators, factor readings, option figures and headlines. Account size, positions,
the journal and every other key stay here. Answers are what a language model wrote from those inputs: they
can be wrong, and the screen says so and shows the inputs.
"""

from __future__ import annotations

import logging
import time
from datetime import datetime, timezone
from typing import Any

import requests

from .ai import AIClient, AIError
from .events import emit, redact

API_URL = "https://api.openai.com/v1/chat/completions"
TIMEOUT = (10, 150)
DEFAULT_MODEL = "gpt-4.1-mini"


class ChatGPT:
    """One question at a time, as JSON. `ai` is the app's AIClient: its reservation and settlement of a
    request are reused so both models share one daily cap."""

    def __init__(self, settings: Any, ai: AIClient, session: requests.Session | None = None):
        self.settings = settings
        self.ai = ai
        self.session = session or requests.Session()
        self._last_error: dict[str, Any] | None = None

    @property
    def model(self) -> str:
        return getattr(self.settings, "openai_model", "") or DEFAULT_MODEL

    @property
    def configured(self) -> bool:
        return bool(getattr(self.settings, "openai_api_key", "")) and self.settings.features.get("ai_assistant", True) \
            and self.settings.ai_daily_requests > 0

    def status(self) -> dict[str, Any]:
        reason = None
        if not getattr(self.settings, "openai_api_key", ""):
            reason = "OPENAI_API_KEY is not set in .env"
        elif not self.settings.features.get("ai_assistant", True):
            reason = "switched off (FEATURE_AI_ASSISTANT=0)"
        elif self.settings.ai_daily_requests <= 0:
            reason = "switched off (AI_DAILY_REQUESTS=0)"
        return {"configured": self.configured, "reason": reason, "model": self.model, "provider": "OpenAI Chat Completions",
                "last_error": self._last_error}

    def ask_json(self, *, purpose: str, prompt: str, system: str, max_tokens: int = 2500) -> dict[str, Any]:
        """One question answered as a JSON object. Returns {"json", "text", "usage", "model", ...}; raises
        AIError with a message that is safe to show."""
        if not self.configured:
            raise AIError("ChatGPT is not set up: " + (self.status()["reason"] or "unavailable"))
        row = self.ai._reserve(purpose, model=self.model)
        usage = {"input_tokens": 0, "output_tokens": 0, "web_searches": 0}
        started = time.time()
        try:
            body = {"model": self.model, "max_completion_tokens": int(max_tokens),
                    "response_format": {"type": "json_object"},
                    "messages": [{"role": "system", "content": system}, {"role": "user", "content": prompt}]}
            reply = self._post(body)
            choice = (reply.get("choices") or [{}])[0]
            text = ((choice.get("message") or {}).get("content") or "").strip()
            u = reply.get("usage") or {}
            usage = {"input_tokens": int(u.get("prompt_tokens") or 0), "output_tokens": int(u.get("completion_tokens") or 0),
                     "web_searches": 0}
            if choice.get("finish_reason") == "length":
                raise AIError("ChatGPT's answer was cut off at the length limit. Try again.")
            from .ai import extract_json
            parsed = extract_json(text)
            if not isinstance(parsed, dict):
                raise AIError("ChatGPT's answer was not the JSON this app asked for. Try again.")
            self.ai._settle(row, usage, True)
        except Exception as exc:                           # noqa: BLE001 - every failure is recorded, then shown safely
            err = exc if isinstance(exc, AIError) else AIError(f"ChatGPT's reply could not be used ({exc.__class__.__name__}).")
            self.ai._settle(row, usage, False, str(err))
            self._last_error = {"at": datetime.now(timezone.utc).isoformat(timespec="seconds"), "error": str(err)}
            emit("ai_request_failed", logging.WARNING, purpose=purpose, error=err, provider="openai")
            raise err from exc
        emit("ai_request", purpose=purpose, model=self.model, input_tokens=usage["input_tokens"],
             output_tokens=usage["output_tokens"], web_searches=0, provider="openai")
        return {"json": parsed, "text": text, "usage": usage, "model": self.model, "provider": "openai",
                "at": datetime.now(timezone.utc).isoformat(timespec="seconds"), "seconds": round(time.time() - started, 1)}

    def _post(self, body: dict[str, Any]) -> dict[str, Any]:
        headers = {"Authorization": f"Bearer {self.settings.openai_api_key}", "content-type": "application/json"}
        try:
            r = self.session.post(API_URL, headers=headers, json=body, timeout=TIMEOUT)
        except requests.Timeout as exc:
            raise AIError("The ChatGPT request timed out. Try again.") from exc
        except requests.RequestException as exc:
            raise AIError(f"Could not reach the OpenAI API ({exc.__class__.__name__}).") from exc
        if r.status_code == 200:
            try:
                return r.json()
            except ValueError as exc:
                raise AIError("The OpenAI API sent a reply this app could not read.") from exc
        try:
            detail = (r.json().get("error") or {}).get("message") or ""
        except ValueError:
            detail = ""
        detail = redact(detail)[:300]
        if r.status_code in (401, 403):
            raise AIError("The OpenAI API refused the key. Check OPENAI_API_KEY in .env.")
        if r.status_code == 404:
            raise AIError(f"The OpenAI API does not know the model {self.model!r}. Check OPENAI_MODEL in .env."
                          + (f" ({detail})" if detail else ""))
        if r.status_code == 429:
            raise AIError("The OpenAI API is rate-limiting this key, or its credit is used up. Try again in a minute."
                          + (f" ({detail})" if detail else ""))
        if r.status_code in (500, 502, 503):
            raise AIError("The OpenAI API is busy or unavailable right now. Try again shortly.")
        raise AIError(f"The OpenAI API refused the request ({r.status_code})" + (f": {detail}" if detail else "."))


__all__ = ["ChatGPT", "DEFAULT_MODEL"]
