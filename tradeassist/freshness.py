"""How fresh and complete a market-data observation is, and whether the data source is healthy.

Every important observation (a stock quote, an option quote, the daily bars, an index level)
can be described by `observe()`:

    instrument, kind, provider       what it is and where it came from
    observed_at                      the time the source gave for the price itself
    retrieved_at                     when the app received it
    session                          OPEN, CLOSED or UNKNOWN: the market clock when retrieved
    freshness                        FRESH        recent enough for its kind while the market is open
                                     DELAYED      the source is delayed by design (Tradier sandbox: 15 min)
                                     LAST_SESSION the market is closed and this is the latest session's price:
                                                  old, but legitimately the most recent there is
                                     STALE        older than its kind allows, or a cached copy used because
                                                  the source failed
                                     UNKNOWN      the source gave no time for it
    completeness                     COMPLETE, PARTIAL (a needed field missing) or MISSING
    warnings                         what is wrong with it, in words

Thresholds differ by kind and can be changed in .env (FRESHNESS_*_SECONDS). A weekend or a
holiday makes the latest session's price old without making it stale; a price older than the
latest session while the market is closed, or older than the threshold while it is open, is
stale. Market holidays are not in a calendar here: a price from the session before a one-day
holiday is allowed one extra day before it is called stale.

`SourceHealth` counts successes and failures of the data source. After repeated failures it
pauses requests for a short time instead of hammering a source that is down; callers that may
show cached data then get the cache, marked STALE with the reason.
"""

from __future__ import annotations

import math
import os
import threading
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Mapping

from .optionmath import NY

KINDS = ("STOCK_QUOTE", "OPTION_QUOTE", "INDEX_QUOTE", "DAILY_BARS", "RESEARCH")
# Seconds a price may age while the market is open before it is stale.
DEFAULT_SECONDS = {"STOCK_QUOTE": 120, "OPTION_QUOTE": 300, "INDEX_QUOTE": 300, "DAILY_BARS": 0, "RESEARCH": 7 * 86400}
DELAYED_SECONDS = 20 * 60            # a delayed source's quote is stale beyond this
FAILURES_TO_PAUSE = 3                # consecutive failures before requests pause
PAUSE_SECONDS = 30.0
MARKET_OPEN, MARKET_CLOSE = (9, 30), (16, 0)


def thresholds(env: Mapping[str, str] | None = None) -> dict[str, float]:
    env = os.environ if env is None else env
    out = dict(DEFAULT_SECONDS)
    for kind in KINDS:
        raw = (env.get(f"FRESHNESS_{kind}_SECONDS") or "").strip()
        if raw:
            try:
                value = float(raw)
            except ValueError as exc:
                raise ValueError(f"FRESHNESS_{kind}_SECONDS must be a number of seconds, got {raw!r}") from exc
            if not math.isfinite(value) or value < 0:
                raise ValueError(f"FRESHNESS_{kind}_SECONDS must be zero or more")
            out[kind] = value
    return out


def parse(stamp: Any) -> datetime | None:
    if not stamp:
        return None
    try:
        moment = datetime.fromisoformat(str(stamp).replace("Z", "+00:00"))
    except ValueError:
        return None
    return moment if moment.tzinfo else moment.replace(tzinfo=timezone.utc)


def quote_time(q: Mapping[str, Any]) -> Any:
    """The time a quote's price is from. A bid and ask are what a mark and a fill depend on, so
    their time comes first, the older of the two (as the pricer measures quote age); the last
    trade's time only when the source gives no bid or ask time. An option that has not traded for
    days can have a live bid and ask, and a fresh trade does not make an old bid and ask fresh."""
    sides = [parse(q.get(k)) for k in ("bid_time", "ask_time")]
    sides = [t for t in sides if t is not None]
    if sides:
        return min(sides).isoformat()
    return q.get("trade_time")


def last_session_close(now: datetime) -> datetime:
    """The 4 p.m. New York close of the latest weekday session that has ended by `now`."""
    ny = now.astimezone(NY)
    day = ny.date()
    if ny.time() < datetime.min.time().replace(hour=MARKET_CLOSE[0]):
        day -= timedelta(days=1)
    while day.weekday() >= 5:
        day -= timedelta(days=1)
    return datetime.combine(day, datetime.min.time().replace(hour=MARKET_CLOSE[0]), tzinfo=NY)


def session_state(clock: Mapping[str, Any] | None, now: datetime) -> str:
    state = str((clock or {}).get("state") or "").lower()
    if state == "open":
        return "OPEN"
    if state in ("closed", "premarket", "postmarket"):
        return "CLOSED"
    ny = now.astimezone(NY)          # no clock: weekday regular hours
    if ny.weekday() < 5 and MARKET_OPEN <= (ny.hour, ny.minute) < MARKET_CLOSE:
        return "OPEN"
    return "CLOSED" if ny.weekday() >= 5 else "UNKNOWN"


def _num_ok(v: Any) -> bool:
    try:
        f = float(v)
    except (TypeError, ValueError):
        return False
    return math.isfinite(f) and f > 0


def quote_completeness(q: Mapping[str, Any] | None, needs_two_sided: bool = True) -> tuple[str, list[str]]:
    """COMPLETE when there is a usable price and, if wanted, a sensible bid and ask."""
    if not q:
        return "MISSING", ["no quote returned"]
    warnings = []
    price_ok = any(_num_ok(q.get(k)) for k in ("last", "close", "mid", "bid", "ask"))
    bid, ask = q.get("bid"), q.get("ask")
    for name in ("last", "bid", "ask"):
        v = q.get(name)
        if v is not None and not (isinstance(v, (int, float)) and math.isfinite(v) and v >= 0):
            warnings.append(f"invalid {name} {v!r}")
    two_sided = _num_ok(bid) and _num_ok(ask)
    if two_sided and float(bid) > float(ask):
        warnings.append(f"crossed quote: bid {bid} above ask {ask}")
        two_sided = False
    if not price_ok:
        return "MISSING", warnings + ["no usable price"]
    if needs_two_sided and not two_sided:
        return "PARTIAL", warnings + ["no two-sided bid and ask"]
    return "COMPLETE", warnings


def observe(kind: str, instrument: str, provider: str, observed_at: Any, *, retrieved_at: datetime | None = None,
            session: str = "UNKNOWN", realtime: bool = True, limits: Mapping[str, float] | None = None,
            completeness: str = "COMPLETE", warnings: list[str] | None = None, from_cache: bool = False,
            cache_reason: str = "") -> dict[str, Any]:
    """Describe one observation's freshness (see the module notes)."""
    limits = limits or DEFAULT_SECONDS
    retrieved = retrieved_at or datetime.now(timezone.utc)
    seen = parse(observed_at)
    warnings = list(warnings or [])
    age = (retrieved - seen).total_seconds() if seen else None
    if age is not None and age < -120:
        warnings.append("the source's time is in the future: its clock or time zone is wrong")
    limit = limits.get(kind, DEFAULT_SECONDS.get(kind, 300))
    if seen is None:
        freshness = "UNKNOWN"
        warnings.append("the source gave no time for this price")
    elif age < -120:
        freshness = "UNKNOWN"               # a time in the future says nothing about how old the price is
    elif session == "OPEN":
        if not realtime:
            freshness = "DELAYED" if age <= DELAYED_SECONDS else "STALE"
        else:
            freshness = "FRESH" if age <= max(limit, 1) else "STALE"
    else:
        latest = last_session_close(retrieved)
        # Allowed: anything from the latest session on, and from the weekday session before it, for a market
        # holiday (there is no holiday calendar here, so a Monday holiday must reach back to Friday).
        before = latest - timedelta(days=1)
        while before.weekday() >= 5:
            before -= timedelta(days=1)
        floor = before - timedelta(hours=7)
        freshness = "LAST_SESSION" if seen >= floor else "STALE"
    if from_cache:
        freshness = "STALE"
        warnings.append(f"a saved copy, because {cache_reason or 'the source did not answer'}")
    if freshness == "STALE" and age is not None and not from_cache:
        warnings.append(f"{describe_age(age)} old")
    return {"instrument": instrument, "kind": kind, "provider": provider,
            "observed_at": seen.isoformat(timespec="seconds") if seen else None,
            "retrieved_at": retrieved.isoformat(timespec="seconds"), "age_seconds": age, "session": session,
            "freshness": freshness, "completeness": completeness, "warnings": warnings, "from_cache": from_cache,
            "realtime": realtime}


def describe_age(seconds: float) -> str:
    if seconds < 90:
        return f"{seconds:.0f} seconds"
    if seconds < 5400:
        return f"{seconds / 60:.0f} minutes"
    if seconds < 2 * 86400:
        return f"{seconds / 3600:.1f} hours"
    return f"{seconds / 86400:.1f} days"


def label(obs: Mapping[str, Any] | None) -> str:
    """A short phrase for the screen."""
    if not obs:
        return "no data"
    f = obs["freshness"]
    age = obs.get("age_seconds")
    if f == "FRESH":
        return f"live, {describe_age(age)} old" if age is not None else "live"
    if f == "DELAYED":
        return f"delayed, {describe_age(age)} old" if age is not None else "delayed"
    if f == "LAST_SESSION":
        return "market closed: latest session's price"
    if f == "STALE":
        return "stale" + (f": {describe_age(age)} old" if age is not None else "")
    return "time unknown"


class SourceHealth:
    """Successes and failures of one data source, and a short pause after repeated failures."""

    def __init__(self, failures_to_pause: int = FAILURES_TO_PAUSE, pause_seconds: float = PAUSE_SECONDS,
                 clock=time.time):
        self._lock = threading.Lock()
        self.failures_to_pause, self.pause_seconds, self._clock = failures_to_pause, pause_seconds, clock
        self.consecutive_failures = 0
        self.total_failures = 0
        self.total_successes = 0
        self.last_success: float | None = None
        self.last_failure: float | None = None
        self.last_error = ""
        self.paused_until = 0.0
        self.recent: list[dict[str, Any]] = []        # the last few failures, for the data health view

    def paused(self) -> float:
        """Seconds left in a pause, or 0."""
        with self._lock:
            return max(0.0, self.paused_until - self._clock())

    def success(self) -> None:
        with self._lock:
            self.consecutive_failures = 0
            self.total_successes += 1
            self.last_success = self._clock()

    def failure(self, what: str, error: str) -> None:
        from .events import redact
        error = redact(error)                # the health view and its endpoints never show a credential
        with self._lock:
            now = self._clock()
            self.consecutive_failures += 1
            self.total_failures += 1
            self.last_failure, self.last_error = now, error
            self.recent = (self.recent + [{"at": datetime.fromtimestamp(now, timezone.utc).isoformat(timespec="seconds"),
                                           "what": what, "error": error[:300]}])[-20:]
            if self.consecutive_failures >= self.failures_to_pause:
                self.paused_until = now + self.pause_seconds

    def summary(self) -> dict[str, Any]:
        with self._lock:
            iso = lambda t: datetime.fromtimestamp(t, timezone.utc).isoformat(timespec="seconds") if t else None  # noqa: E731
            left = max(0.0, self.paused_until - self._clock())
            status = "PAUSED" if left else ("DEGRADED" if self.consecutive_failures else "OK")
            if not self.total_successes and not self.total_failures:
                status = "NOT_USED_YET"
            return {"status": status, "consecutive_failures": self.consecutive_failures,
                    "successes": self.total_successes, "failures": self.total_failures,
                    "last_success": iso(self.last_success), "last_failure": iso(self.last_failure),
                    "last_error": self.last_error, "paused_for_seconds": round(left, 1), "recent_failures": list(self.recent)}


__all__ = ["observe", "thresholds", "quote_completeness", "label", "describe_age", "SourceHealth", "session_state",
           "last_session_close", "quote_time", "KINDS"]
