"""The trading sessions a replay walks through (4.0).

marketdays.py lists the exchange's holidays for 2025 to 2028 only. A replay back to 2015 takes its sessions from
the days the market itself traded: the days SPY has a bar in the frozen dataset. Where marketdays.py covers the
year, the two must agree; a disagreement is a data-quality failure recorded with the dataset, not papered over.

Horizons count sessions, never weekdays: a one-day forecast made on the Wednesday before Thanksgiving is graded at
Friday's close (pandas business days, which forecast.record_target used before 4.0, would say Thursday).
"""

from __future__ import annotations

import bisect
from datetime import date, datetime, time
from typing import Iterable, Sequence

from ... import marketdays
from ...optionmath import NY

CUTOFF_DELAY_HOURS = 1        # predictions stand for one hour after the close: the daily bar is final by then


class CalendarError(ValueError):
    pass


class Calendar:
    def __init__(self, sessions: Iterable[date]):
        self.sessions: tuple[date, ...] = tuple(sorted(set(sessions)))
        if not self.sessions:
            raise CalendarError("A calendar needs at least one session")
        self._index = {d: i for i, d in enumerate(self.sessions)}

    def __len__(self) -> int:
        return len(self.sessions)

    def __contains__(self, d: date) -> bool:
        return d in self._index

    @property
    def first(self) -> date:
        return self.sessions[0]

    @property
    def last(self) -> date:
        return self.sessions[-1]

    def index(self, d: date) -> int:
        try:
            return self._index[d]
        except KeyError:
            raise CalendarError(f"{d} is not a session in this calendar") from None

    def add(self, d: date, n: int) -> date | None:
        """The session n sessions after (n > 0) or before (n < 0) session d; None past either end."""
        i = self.index(d) + n
        return self.sessions[i] if 0 <= i < len(self.sessions) else None

    def on_or_after(self, d: date) -> date | None:
        i = bisect.bisect_left(self.sessions, d)
        return self.sessions[i] if i < len(self.sessions) else None

    def on_or_before(self, d: date) -> date | None:
        i = bisect.bisect_right(self.sessions, d) - 1
        return self.sessions[i] if i >= 0 else None

    def between(self, start: date, end: date) -> list[date]:
        lo, hi = bisect.bisect_left(self.sessions, start), bisect.bisect_right(self.sessions, end)
        return list(self.sessions[lo:hi])

    def _next(self, d: date) -> date:
        """The next session: from this calendar, or past its end from the exchange calendar (weekdays past 2028)."""
        nxt = self.add(d, 1)
        return nxt if nxt is not None else marketdays.next_trading_day(d)

    def is_week_end(self, d: date) -> bool:
        """The last session of its week (Friday, or Thursday before a Good Friday)."""
        return self._next(d).isocalendar()[:2] != d.isocalendar()[:2]

    def is_month_end(self, d: date) -> bool:
        nxt = self._next(d)
        return (nxt.year, nxt.month) != (d.year, d.month)

    def close_time(self, d: date) -> time:
        return marketdays.close_time(d) if marketdays.covered(d) else marketdays.CLOSE

    def cutoff_at(self, d: date) -> str:
        """The moment a prediction made from session d's bar stands for, New York time."""
        close = self.close_time(d)
        return datetime.combine(d, time(close.hour + CUTOFF_DELAY_HOURS, close.minute), NY).isoformat()


def from_market_dates(dates: Sequence[date]) -> tuple[Calendar, list[dict]]:
    """The calendar from the days the market proxy traded, and its disagreements with marketdays.py."""
    cal = Calendar(dates)
    problems: list[dict] = []
    covered = [d for d in cal.sessions if marketdays.covered(d)]
    if covered:
        lo, hi = covered[0], covered[-1]
        listed = set(marketdays.trading_days_between(lo, hi))
        have = set(covered)
        for d in sorted(listed - have):
            problems.append({"level": "fail", "code": "CALENDAR_MISSING_SESSION",
                             "text": f"{d} is a trading day on the exchange calendar but the market proxy has no bar for it."})
        for d in sorted(have - listed):
            problems.append({"level": "fail", "code": "CALENDAR_EXTRA_SESSION",
                             "text": f"The market proxy has a bar on {d}, which the exchange calendar lists as closed."})
    return cal, problems


__all__ = ["Calendar", "CalendarError", "from_market_dates", "CUTOFF_DELAY_HOURS"]
