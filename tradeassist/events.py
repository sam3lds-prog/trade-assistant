"""Structured operational events: one line per event, `event=NAME key=value ...`, and the most
recent warnings kept in memory for the data health view.

Events (with the level they are logged at):

    provider_failure          WARNING  a data-source request failed
    stale_market_data         INFO     a stale or undated price was used and labelled
    missing_valuation         INFO     a position could not be priced
    risk_calculation_failed   WARNING  a risk figure could not be worked out
    snapshot_failed           WARNING  lifecycle snapshots could not be brought up to date
    migration_failed          ERROR    a schema migration failed and was rolled back
    migration_applied         INFO     a schema migration ran (with the backup it took first)
    invalid_transition        INFO     a request to change a position was refused (expected; no stack trace)
    correction_recorded       INFO     a fill was corrected, with the reason
    repair_applied            WARNING  stored figures were repaired from the fills

Nothing here logs credentials: values that look like tokens, bearer headers, passwords or
long secrets are replaced with [redacted] before a line is written, and personal trading
details are limited to ids and symbols.
"""

from __future__ import annotations

import logging
import re
import threading
from collections import deque
from datetime import datetime, timezone
from typing import Any

log = logging.getLogger("tradeassist.events")

# What a credential looks like in an error, a URL or a log line. Each rule keeps the name and hides the value.
_KEYS = (r"access[_-]?token|refresh[_-]?token|id[_-]?token|auth[_-]?token|token|api[_-]?key|apikey|client[_-]?secret|"
         r"secret|password|passwd|pwd|authorization|auth|cookie|session[_-]?id|x-api-key")
RULES = (
    # An Authorization header's whole value, with or without a scheme ("Token abc", "Bearer abc").
    (re.compile(r"(?i)(\bauthorization[\"']?\s*[:=]\s*[\"']?)[^\s\"',;}]+(?:\s+(?!\[redacted\])[^\s\"',;}]+)?"),
     r"\1[redacted]"),
    # Authorization schemes anywhere else: "Bearer abc", "Basic abc", however short, with base64 characters.
    (re.compile(r"(?i)\b(bearer|basic)(\s+)(?!\[redacted\])[A-Za-z0-9._~+/=\-]+"), r"\1\2[redacted]"),
    # key=value, key: value and JSON "key": "value", quoted or not.
    (re.compile(r"(?i)([\"']?\b(?:" + _KEYS + r")\b[\"']?\s*[:=]\s*)([\"']?)(?!\[redacted\])[^\s\"'&,;}]+"),
     r"\1\2[redacted]"),
    # A quoted value right after the word token: token 'abc'.
    (re.compile(r"(?i)\b(token\s+)(['\"])[^'\"]+\2"), r"\1\2[redacted]\2"),
    # The word token, then something shaped like one: "sandbox token Kx9Lm2Np4Qr6".
    (re.compile(r"(?i)\b(token\s+)(?=[A-Za-z0-9._\-]*\d)(?=[A-Za-z0-9._\-]*[A-Za-z])[A-Za-z0-9._\-]{12,}"),
     r"\1[redacted]"),
    # Query-string values that are credentials in OAuth and signed URLs.
    (re.compile(r"(?i)([?&](?:code|key|sig|signature)=)[^&\s\"']+"), r"\1[redacted]"),
    # JSON web tokens.
    (re.compile(r"\beyJ[A-Za-z0-9_\-]+\.[A-Za-z0-9_\-]+(?:\.[A-Za-z0-9_\-]*)?"), "[redacted]"),
    # Long runs of letters and digits mixed: the shape of an API key.
    (re.compile(r"\b(?=[A-Za-z0-9]*\d)(?=[A-Za-z0-9]*[A-Za-z])[A-Za-z0-9]{24,}\b"), "[redacted]"),
)
_recent: deque = deque(maxlen=100)
_lock = threading.Lock()


def redact(text: Any) -> str:
    out = str(text)
    for pattern, replacement in RULES:
        out = pattern.sub(replacement, out)
    return out


def _fmt(value: Any) -> str:
    s = redact(value)
    if len(s) > 300:
        s = s[:300] + "..."
    return f'"{s}"' if (" " in s or "=" in s or not s) else s


def emit(event: str, level: int = logging.INFO, **fields: Any) -> None:
    """Log one event as `event=NAME key=value ...` and keep warnings and errors for the health view."""
    line = " ".join([f"event={event}", *(f"{k}={_fmt(v)}" for k, v in fields.items() if v is not None)])
    log.log(level, line)
    if level >= logging.WARNING:
        with _lock:
            _recent.append({"at": datetime.now(timezone.utc).isoformat(timespec="seconds"), "event": event,
                            "level": logging.getLevelName(level), **{k: redact(v) for k, v in fields.items() if v is not None}})


def recent(limit: int = 50) -> list[dict[str, Any]]:
    with _lock:
        return list(_recent)[-limit:][::-1]


class RedactingFilter(logging.Filter):
    """Applied to the app's log handler so that no line, from any module, carries a credential."""

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            msg = record.getMessage()
        except Exception:                   # noqa: BLE001 - a bad format string must not stop logging
            return True
        if redact(msg) != msg:
            # Redact the arguments in place first, so a formatter that reads them (uvicorn's access log
            # unpacks five) still finds them; only if the template itself holds the secret is it flattened.
            if isinstance(record.args, tuple):
                record.args = tuple(redact(a) if isinstance(a, str) else a for a in record.args)
            elif isinstance(record.args, dict):
                record.args = {k: redact(v) if isinstance(v, str) else v for k, v in record.args.items()}
            try:
                still = record.getMessage()
            except Exception:               # noqa: BLE001
                still = msg
            if redact(still) != still:
                record.msg, record.args = redact(still), ()
        # A traceback quotes the exception's message (and sometimes the request); the formatter uses
        # exc_text when it is already set, so set it to the redacted text.
        if record.exc_info and record.exc_info[0] is not None:
            record.exc_text = redact(logging.Formatter().formatException(record.exc_info))
        elif record.exc_text:
            record.exc_text = redact(record.exc_text)
        if record.stack_info:
            record.stack_info = redact(record.stack_info)
        return True


def protect_logging(*loggers: str) -> None:
    """Redact every line of the root handlers and of the named loggers (uvicorn's own loggers do
    not pass through the root handlers). Filters on a logger survive uvicorn's logging setup."""
    flt = RedactingFilter()
    for handler in logging.getLogger().handlers:
        if not any(isinstance(f, RedactingFilter) for f in handler.filters):
            handler.addFilter(flt)
    for name in loggers:
        lg = logging.getLogger(name)
        if not any(isinstance(f, RedactingFilter) for f in lg.filters):
            lg.addFilter(flt)


__all__ = ["emit", "recent", "redact", "RedactingFilter", "protect_logging"]
