"""New York Stock Exchange trading days (2.15): holidays and early closes.

Before 2.15 the app knew weekends only ("market holidays are not known here"). Paper trading and the morning
session's rescans need to know when the market is shut, so this is the exchange's own published calendar:
NYSE Group's announcements of December 2024 (for 2025) and December 2025 (for 2026 to 2028). Regular hours are
9:30 to 16:00 New York time; on an early-close day trading stops at 13:00.

It is a fixed list. A day the exchange closes unexpectedly (a national day of mourning, as on 9 January 2025) is
only known once it is added here; past the last year listed, weekdays count as trading days and `covered()` says
so.
"""

from __future__ import annotations

from datetime import date, datetime, time, timedelta

from .optionmath import NY

HOLIDAYS: frozenset[date] = frozenset(date.fromisoformat(d) for d in (
    # 2025
    "2025-01-01", "2025-01-09", "2025-01-20", "2025-02-17", "2025-04-18", "2025-05-26", "2025-06-19",
    "2025-07-04", "2025-09-01", "2025-11-27", "2025-12-25",
    # 2026
    "2026-01-01", "2026-01-19", "2026-02-16", "2026-04-03", "2026-05-25", "2026-06-19", "2026-07-03",
    "2026-09-07", "2026-11-26", "2026-12-25",
    # 2027
    "2027-01-01", "2027-01-18", "2027-02-15", "2027-03-26", "2027-05-31", "2027-06-18", "2027-07-05",
    "2027-09-06", "2027-11-25", "2027-12-24",
    # 2028 (no New Year's Day holiday: 1 January 2028 is a Saturday)
    "2028-01-17", "2028-02-21", "2028-04-14", "2028-05-29", "2028-06-19", "2028-07-04", "2028-09-04",
    "2028-11-23", "2028-12-25",
))
EARLY_CLOSES: frozenset[date] = frozenset(date.fromisoformat(d) for d in (
    "2025-07-03", "2025-11-28", "2025-12-24",
    "2026-11-27", "2026-12-24",
    "2027-11-26",
    "2028-07-03", "2028-11-24",
))
FIRST_YEAR, LAST_YEAR = 2025, 2028
OPEN = time(9, 30)
CLOSE = time(16, 0)
EARLY_CLOSE = time(13, 0)


def covered(day: date) -> bool:
    """Whether `day` falls in the years this calendar lists (outside them only weekends are known)."""
    return FIRST_YEAR <= day.year <= LAST_YEAR


def is_trading_day(day: date) -> bool:
    return day.weekday() < 5 and day not in HOLIDAYS


def close_time(day: date) -> time:
    """The regular session's closing time on `day` (13:00 on an early-close day)."""
    return EARLY_CLOSE if day in EARLY_CLOSES else CLOSE


def session(day: date) -> tuple[datetime, datetime] | None:
    """The regular session's open and close on `day`, New York time; None on a weekend or holiday."""
    if not is_trading_day(day):
        return None
    return datetime.combine(day, OPEN, NY), datetime.combine(day, close_time(day), NY)


def next_trading_day(day: date) -> date:
    """The first trading day after `day`."""
    d = day + timedelta(days=1)
    while not is_trading_day(d):
        d += timedelta(days=1)
    return d


def previous_trading_day(day: date) -> date:
    """The last trading day before `day`."""
    d = day - timedelta(days=1)
    while not is_trading_day(d):
        d -= timedelta(days=1)
    return d


def add_trading_days(day: date, n: int) -> date:
    """The trading day `n` trading days after `day` (n >= 1), or `day` itself for n = 0 when it trades."""
    if n <= 0:
        return day if is_trading_day(day) else next_trading_day(day)
    d = day
    for _ in range(n):
        d = next_trading_day(d)
    return d


def trading_days_between(start: date, end: date) -> list[date]:
    """Every trading day from `start` to `end`, both included."""
    out, d = [], start
    while d <= end:
        if is_trading_day(d):
            out.append(d)
        d += timedelta(days=1)
    return out


def horizon_close(now: datetime, days: int) -> datetime:
    """The end of a `days`-day swing trade begun at `now`: the close `days` trading days after `now`'s New York date.
    One rule for the trade card's horizon date, a paper order's horizon and the blend's grid (2.15), so a trade
    entered before the open, during the session or after it leaves at the same close everywhere."""
    last = add_trading_days(now.astimezone(NY).date(), max(1, int(days)))
    return session(last)[1]


def phase(now: datetime | None = None) -> str:
    """Where `now` sits in the trading day, New York time: "pre" (4:00 to the open on a trading day), "open",
    "post" (the close to 20:00) or "closed" (overnight, weekends and holidays)."""
    now = (now or datetime.now(NY)).astimezone(NY)
    s = session(now.date())
    if s is None:
        return "closed"
    opens, closes = s
    if opens <= now < closes:
        return "open"
    if datetime.combine(now.date(), time(4, 0), NY) <= now < opens:
        return "pre"
    if closes <= now < datetime.combine(now.date(), time(20, 0), NY):
        return "post"
    return "closed"


def minutes_to_close(now: datetime | None = None) -> float | None:
    """Minutes left in today's regular session; None when it is not open now."""
    now = (now or datetime.now(NY)).astimezone(NY)
    s = session(now.date())
    if s is None or not (s[0] <= now < s[1]):
        return None
    return (s[1] - now).total_seconds() / 60.0


__all__ = ["HOLIDAYS", "EARLY_CLOSES", "covered", "is_trading_day", "close_time", "session", "next_trading_day",
           "previous_trading_day", "add_trading_days", "trading_days_between", "horizon_close", "phase",
           "minutes_to_close"]
