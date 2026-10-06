from datetime import date

import pytest

from tradeassist import research as sec
from tradeassist.db import Database


def submissions():
    """Shaped like data.sec.gov/submissions/CIK##########.json (parallel arrays under filings.recent)."""
    rows = [
        ("8-K", "2026-07-30", "2026-07-30", "0000320193-26-000070", "a8k.htm", "2.02,9.01"),
        ("10-Q", "2026-07-31", "2026-06-27", "0000320193-26-000071", "a10q.htm", ""),
        ("4", "2026-06-02", "2026-06-01", "0000320193-26-000060", "form4.xml", ""),
        ("8-K", "2026-05-14", "2026-05-12", "0000320193-26-000055", "b8k.htm", "5.02"),
        ("8-K", "2026-04-30", "2026-04-30", "0000320193-26-000050", "c8k.htm", "2.02,9.01"),
        ("8-K", "2026-01-29", "2026-01-29", "0000320193-26-000010", "d8k.htm", "2.02,9.01"),
        ("10-K", "2025-10-31", "2025-09-27", "0000320193-25-000090", "a10k.htm", ""),
        ("8-K", "2025-10-30", "2025-10-30", "0000320193-25-000089", "e8k.htm", "2.02,9.01"),
        ("8-K", "2025-07-31", "2025-07-31", "0000320193-25-000070", "f8k.htm", "2.02,9.01"),
    ]
    keys = ("form", "filingDate", "reportDate", "accessionNumber", "primaryDocument", "items")
    recent = {k: [r[i] for r in rows] for i, k in enumerate(keys)}
    recent["primaryDocDescription"] = [r[0] for r in rows]
    return {"cik": "320193", "name": "Apple Inc.", "sicDescription": "Electronic Computers",
            "fiscalYearEnd": "0926", "filings": {"recent": recent}}


def facts():
    def q(frame, end, val, form="10-Q"):
        return {"start": "x", "end": end, "val": val, "form": form, "filed": end, "frame": frame}
    revenue = [q("CY2025Q2", "2025-06-28", 94.0e9), q("CY2025Q3", "2025-09-27", 102.5e9, "10-K"),
               q("CY2026Q1", "2026-03-28", 100.0e9), q("CY2026Q2", "2026-06-27", 103.4e9),
               {"end": "2026-06-27", "val": 330e9, "form": "10-Q"},                    # year-to-date: no frame
               q("CY2025", "2025-09-27", 416e9, "10-K")]                              # annual frame
    return {"facts": {"us-gaap": {
        "Revenues": {"units": {"USD": [q("CY2019Q1", "2019-03-30", 58e9)]}},           # stale tag: must lose
        "RevenueFromContractWithCustomerExcludingAssessedTax": {"units": {"USD": revenue}},
        "NetIncomeLoss": {"units": {"USD": [q("CY2026Q2", "2026-06-27", 25.1e9)]}},
        "EarningsPerShareDiluted": {"units": {"USD/shares": [q("CY2026Q2", "2026-06-27", 1.68)]}},
    }}}


def test_recent_filings_are_filtered_linked_and_decoded():
    out = sec.recent_filings(submissions(), 320193)
    assert [f["form"] for f in out][:3] == ["8-K", "10-Q", "8-K"]
    assert all(f["form"] in ("8-K", "10-Q", "10-K") for f in out)
    first = out[0]
    assert first["url"] == "https://www.sec.gov/Archives/edgar/data/320193/000032019326000070/a8k.htm"
    assert first["items"][0] == {"code": "2.02", "label": "Results of operations (earnings release)"}
    assert out[2]["items"][0]["label"] == "Director or officer change"


def test_earnings_dates_and_estimate():
    dates = sec.earnings_release_dates(submissions())
    assert dates[0] == date(2026, 7, 30) and len(dates) == 5
    est = sec.estimate_next_earnings(dates, today=date(2026, 10, 3))
    assert est["estimated"] is True and est["last_release"] == "2026-07-30"
    assert est["date"] == "2026-10-29"                       # one year (52 weeks) after 2025-10-30
    assert sec.estimate_next_earnings(dates[:3], today=date(2026, 10, 3)) is None      # too little history


def test_off_cycle_results_filings_do_not_move_the_estimate():
    """A preliminary-results or stray 8-K with item 2.02 must not be mistaken for the quarterly report."""
    regular = sec.earnings_release_dates(submissions())
    today = date(2026, 10, 10)
    assert sec.estimate_next_earnings(regular, today)["date"] == "2026-10-29"
    assert sec.estimate_next_earnings(regular + [date(2026, 10, 8)], today)["date"] == "2026-10-29"
    assert sec.estimate_next_earnings(regular + [date(2026, 9, 10)], today)["date"] == "2026-10-29"
    # Once the real report is filed, the estimate moves to the following quarter.
    assert sec.estimate_next_earnings(regular + [date(2026, 10, 29)], date(2026, 11, 5))["date"] == "2027-01-28"
    assert sec.estimate_next_earnings(regular + [date(2026, 10, 22)], date(2026, 10, 23))["date"] == "2027-01-28"


def test_malformed_sec_data_degrades_instead_of_crashing():
    subs = submissions()
    recent = subs["filings"]["recent"]
    recent["filingDate"][0] = ""                                            # an unreadable date
    recent["items"] = recent["items"][:3]                                   # a column shorter than the others
    recent["primaryDocument"][1] = None
    assert len(sec.earnings_release_dates(subs)) == 0                       # only rows with a date and items count
    filings = sec.recent_filings(subs, 320193)
    assert filings and all(f["filed"] for f in filings)
    assert sec.recent_filings({"filings": "nope"}, 1) == [] and sec.earnings_release_dates({}) == []
    bad_facts = {"facts": {"us-gaap": {"Revenues": {"units": {"USD": [
        None, {"frame": "CY2026Q2", "end": None, "val": 5.0}, {"frame": "CY20X6Q2", "val": 1.0},
        {"frame": "CY2026Q1", "end": "2026-03-28", "val": "n/a"}]}}}}}
    out = sec.fundamentals(bad_facts)
    assert [r["quarter"] for r in out["revenue"]] == ["2026 Q2"]
    assert sec.fundamentals({"facts": None})["revenue"] == []


def test_a_late_report_stays_pending_instead_of_being_skipped():
    """With no filing near the projected date the report is late, not done: keep blocking."""
    regular = sec.earnings_release_dates(submissions())
    for today in (date(2026, 11, 1), date(2026, 11, 2), date(2026, 11, 20), date(2026, 12, 10)):
        est = sec.estimate_next_earnings(regular, today)
        assert est["date"] == "2026-10-29" and est["overdue"] is True
    # After 45 days with nothing filed the list is assumed incomplete and the next quarter is projected.
    est = sec.estimate_next_earnings(regular, date(2026, 12, 20))
    assert est["date"] == "2027-01-28" and est["overdue"] is False


def test_a_preliminary_filing_two_weeks_early_is_not_taken_for_the_report():
    regular = sec.earnings_release_dates(submissions())
    for prelim in (date(2026, 10, 13), date(2026, 10, 15), date(2026, 10, 20)):
        est = sec.estimate_next_earnings(regular + [prelim], prelim + __import__("datetime").timedelta(days=1))
        assert est["date"] == "2026-10-29", prelim
    # A filing within a week of the projected date is that quarter's report.
    assert sec.estimate_next_earnings(regular + [date(2026, 10, 24)], date(2026, 10, 25))["date"] == "2027-01-28"


def test_stale_history_never_returns_a_long_past_date():
    dates = sec.earnings_release_dates(submissions())
    est = sec.estimate_next_earnings(dates, today=date(2027, 3, 1))
    assert est["date"] == "2027-01-28" and est["overdue"]                    # 32 days late: still pending
    est = sec.estimate_next_earnings(dates, today=date(2028, 6, 1))
    assert date.fromisoformat(est["date"]) >= date(2028, 6, 1) and not est["overdue"]


def test_fundamentals_use_single_quarter_frames_only():
    f = sec.fundamentals(facts())
    quarters = [r["quarter"] for r in f["revenue"]]
    assert quarters == ["2026 Q2", "2026 Q1", "2025 Q3", "2025 Q2"]       # newest first; YTD and annual excluded
    assert f["revenue"][0]["value"] == 103.4e9
    assert f["revenue"][0]["year_over_year"] == pytest.approx(103.4 / 94.0 - 1)
    assert f["revenue"][1]["year_over_year"] is None
    assert f["eps_diluted"][0]["value"] == 1.68 and f["net_income"][0]["quarter"] == "2026 Q2"


class FakeResponse:
    def __init__(self, status, body):
        self.status_code, self._body = status, body

    def json(self):
        return self._body


class FakeSession:
    def __init__(self, routes):
        self.routes, self.calls = routes, []

    def get(self, url, headers=None, timeout=None):
        self.calls.append((url, headers))
        for fragment, reply in self.routes.items():
            if fragment in url:
                return reply
        return FakeResponse(404, None)


def test_client_identifies_itself_caches_and_builds_the_brief(monkeypatch):
    monkeypatch.setattr(sec, "MIN_INTERVAL", 0.0)
    session = FakeSession({
        "company_tickers.json": FakeResponse(200, {"0": {"cik_str": 320193, "ticker": "AAPL", "title": "Apple Inc."},
                                                   "1": {"cik_str": 1067983, "ticker": "BRK-B", "title": "Berkshire"}}),
        "submissions/CIK0000320193.json": FakeResponse(200, submissions()),
        "companyfacts/CIK0000320193.json": FakeResponse(200, facts()),
    })
    client = sec.SecClient("Test User test@example.com", Database(":memory:"), session=session)
    brief = sec.build_company_research(client, "AAPL", today=date(2026, 10, 3))
    assert brief["available"] and brief["company"]["name"] == "Apple Inc." and brief["errors"] == []
    assert brief["earnings"]["date"] == "2026-10-29" and brief["fundamentals"]["revenue"]
    assert all(h["User-Agent"] == "Test User test@example.com" for _, h in session.calls)
    n = len(session.calls)
    sec.build_company_research(client, "AAPL", today=date(2026, 10, 3))
    assert len(session.calls) == n                                         # second build served from cache
    assert client.cik_for("BRK.B") == (1067983, "Berkshire")
    with pytest.raises(sec.ResearchError, match="not in the SEC"):
        client.cik_for("ZZZZ")


def test_disabled_without_a_user_agent_and_degrades_on_errors(monkeypatch):
    monkeypatch.setattr(sec, "MIN_INTERVAL", 0.0)
    off = sec.SecClient("", Database(":memory:"), session=FakeSession({}))
    brief = sec.build_company_research(off, "AAPL")
    assert not brief["available"] and "SEC_USER_AGENT" in brief["errors"][0]
    session = FakeSession({"company_tickers.json": FakeResponse(403, None)})
    blocked = sec.SecClient("x y@z.com", Database(":memory:"), session=session)
    brief = sec.build_company_research(blocked, "AAPL")
    assert not brief["available"] and "refused" in brief["errors"][0]
