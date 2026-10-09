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

# Bearer headers, key=value secrets, and long runs of letters and digits mixed (the shape of an API token).
SECRET = re.compile(r"(?i)(bearer\s+[A-Za-z0-9._\-]+|(?:token|password|secret|api[_-]?key|authorization)\s*[=:]\s*\S+"
                    r"|\b(?=[A-Za-z0-9]*\d)(?=[A-Za-z0-9]*[A-Za-z])[A-Za-z0-9]{24,}\b)")
_recent: deque = deque(maxlen=100)
_lock = threading.Lock()


def redact(text: Any) -> str:
    return SECRET.sub("[redacted]", str(text))


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
        clean = redact(msg)
        if clean != msg:
            record.msg, record.args = clean, ()
        return True


__all__ = ["emit", "recent", "redact", "RedactingFilter"]
