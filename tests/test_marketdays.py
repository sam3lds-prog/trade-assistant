"""2.15: the exchange calendar: holidays, early closes and where a moment sits in the trading day."""

from __future__ import annotations

from datetime import date, datetime, time

from tradeassist import marketdays as md
from tradeassist.optionmath import NY


def test_the_published_holidays_and_early_closes():
    assert not md.is_trading_day(date(2026, 11, 26))               # Thanksgiving
    assert not md.is_trading_day(date(2026, 7, 3))                  # Independence Day observed (a Friday)
    assert not md.is_trading_day(date(2027, 12, 24))                # Christmas observed
    assert md.is_trading_day(date(2027, 12, 31))                    # no New Year's holiday for 2028 is observed early
    assert not md.is_trading_day(date(2025, 1, 9))                  # the national day of mourning
    assert md.close_time(date(2026, 11, 27)) == time(13, 0) and md.close_time(date(2026, 11, 25)) == time(16, 0)
    assert not md.is_trading_day(date(2026, 10, 24))                # a Saturday
    assert md.covered(date(2026, 1, 1)) and not md.covered(date(2031, 1, 1))


def test_stepping_over_weekends_and_holidays():
    assert md.next_trading_day(date(2026, 11, 25)) == date(2026, 11, 27)
    assert md.previous_trading_day(date(2026, 11, 30)) == date(2026, 11, 27)
    assert md.add_trading_days(date(2026, 11, 24), 2) == date(2026, 11, 27)
    assert md.add_trading_days(date(2026, 10, 24), 0) == date(2026, 10, 26)   # a Saturday rolls to Monday
    assert md.trading_days_between(date(2026, 12, 23), date(2026, 12, 28)) == [date(2026, 12, 23), date(2026, 12, 24),
                                                                               date(2026, 12, 28)]
    opens, closes = md.session(date(2026, 12, 24))
    assert (opens.hour, opens.minute, closes.hour) == (9, 30, 13)
    assert md.session(date(2026, 12, 25)) is None


def test_the_phase_of_the_day():
    at = lambda h, m, d=date(2026, 10, 20): datetime(d.year, d.month, d.day, h, m, tzinfo=NY)   # noqa: E731
    assert md.phase(at(3, 59)) == "closed" and md.phase(at(4, 0)) == "pre" and md.phase(at(9, 29)) == "pre"
    assert md.phase(at(9, 30)) == "open" and md.phase(at(15, 59)) == "open" and md.phase(at(16, 0)) == "post"
    assert md.phase(at(20, 0)) == "closed" and md.phase(at(10, 0, date(2026, 11, 26))) == "closed"
    assert md.phase(at(13, 30, date(2026, 11, 27))) == "post"       # an early close
    assert md.minutes_to_close(at(15, 30)) == 30 and md.minutes_to_close(at(8, 0)) is None
