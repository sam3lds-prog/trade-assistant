"""Company research from SEC EDGAR: recent filings, reported results, and an
estimate of the next earnings date.

EDGAR's JSON endpoints are free and need no key, but the SEC requires a
User-Agent that identifies the caller with contact details and limits clients
to 10 requests a second. Set SEC_USER_AGENT in .env to switch this on.
"""

from __future__ import annotations

import logging
import statistics
import threading
import time
from datetime import date, timedelta
from typing import Any

import requests

from .db import Database

log = logging.getLogger(__name__)

TICKER_MAP_URL = "https://www.sec.gov/files/company_tickers.json"
SUBMISSIONS_URL = "https://data.sec.gov/submissions/CIK{cik:010d}.json"
FACTS_URL = "https://data.sec.gov/api/xbrl/companyfacts/CIK{cik:010d}.json"
ARCHIVE_URL = "https://www.sec.gov/Archives/edgar/data/{cik}/{accession}/{document}"
MIN_INTERVAL = 0.15  # seconds between requests: well under the SEC's 10 per second

FORMS_OF_INTEREST = ("10-K", "10-Q", "8-K")
ITEM_LABELS = {
    "1.01": "Entered a material agreement",
    "1.02": "Ended a material agreement",
    "1.03": "Bankruptcy or receivership",
    "2.01": "Completed an acquisition or sale of assets",
    "2.02": "Results of operations (earnings release)",
    "2.03": "Took on a material financial obligation",
    "2.05": "Restructuring or exit costs",
    "2.06": "Material impairment",
    "3.01": "Listing notice or delisting",
    "4.01": "Changed auditor",
    "4.02": "Earlier financial statements can no longer be relied on",
    "5.02": "Director or officer change",
    "5.03": "Bylaw or fiscal-year change",
    "5.07": "Shareholder vote results",
    "7.01": "Regulation FD disclosure",
    "8.01": "Other events",
    "9.01": "Financial statements and exhibits",
}
REVENUE_TAGS = ("RevenueFromContractWithCustomerExcludingAssessedTax", "Revenues", "SalesRevenueNet",
                "RevenueFromContractWithCustomerIncludingAssessedTax")
FACT_TAGS = {"net_income": ("NetIncomeLoss",), "eps_diluted": ("EarningsPerShareDiluted",)}


class ResearchError(RuntimeError):
    pass


class SecClient:
    def __init__(self, user_agent: str, db: Database, session: requests.Session | None = None):
        self.user_agent = user_agent
        self.db = db
        self._session = session or requests.Session()
        self._lock = threading.Lock()
        self._last_request = 0.0

    @property
    def enabled(self) -> bool:
        return bool(self.user_agent)

    def _get_json(self, url: str, cache_key: str | None, max_age: float) -> Any:
        cached = self.db.cache_get(cache_key, max_age) if cache_key else None
        if cached is not None:
            return cached
        if not self.enabled:
            raise ResearchError("Set SEC_USER_AGENT in .env (for example 'Your Name you@example.com') to "
                                "load SEC filings. The SEC requires callers to identify themselves.")
        with self._lock:
            wait = MIN_INTERVAL - (time.time() - self._last_request)
            if wait > 0:
                time.sleep(wait)
            try:
                resp = self._session.get(url, headers={"User-Agent": self.user_agent,
                                                       "Accept-Encoding": "gzip, deflate"}, timeout=15)
            except requests.RequestException as exc:
                raise ResearchError(f"Could not reach the SEC: {exc.__class__.__name__}") from exc
            finally:
                self._last_request = time.time()
        if resp.status_code == 404:
            raise ResearchError("The SEC has no record at that address (the company may not file with the SEC).")
        if resp.status_code == 403:
            raise ResearchError("The SEC refused the request. Check that SEC_USER_AGENT names you and gives "
                                "a contact email.")
        if resp.status_code != 200:
            raise ResearchError(f"SEC request failed: HTTP {resp.status_code}")
        try:
            data = resp.json()
        except ValueError as exc:
            raise ResearchError("The SEC returned something that is not JSON") from exc
        if cache_key:
            self.db.cache_put(cache_key, data)
        return data

    def cik_for(self, ticker: str) -> tuple[int, str]:
        wanted = ticker.upper().replace(".", "-").replace("/", "-")  # SEC writes BRK-B
        table = self._get_json(TICKER_MAP_URL, "sec:tickers", 7 * 86400)
        rows = table.values() if isinstance(table, dict) else table
        for row in rows:
            if isinstance(row, dict) and str(row.get("ticker", "")).upper() == wanted:
                try:
                    return int(row["cik_str"]), str(row.get("title", ""))
                except (KeyError, TypeError, ValueError) as exc:
                    raise ResearchError("The SEC's ticker list has an unreadable entry for this symbol") from exc
        raise ResearchError(f"{ticker} is not in the SEC's ticker list (funds and some foreign companies are not).")

    def submissions(self, cik: int) -> dict[str, Any]:
        return self._get_json(SUBMISSIONS_URL.format(cik=cik), f"sec:submissions:{cik}", 6 * 3600)

    def company_facts(self, cik: int) -> dict[str, Any]:
        # Several megabytes for large companies, so the raw reply is not cached; the
        # summary built from it is.
        return self._get_json(FACTS_URL.format(cik=cik), None, 0)


def _filing_rows(submissions: dict[str, Any]) -> list[dict[str, str]]:
    """The parallel arrays under filings.recent as a list of rows, tolerant of missing or
    uneven columns and of non-text values."""
    filings = submissions.get("filings") if isinstance(submissions, dict) else None
    recent = filings.get("recent") if isinstance(filings, dict) else None
    if not isinstance(recent, dict):
        return []
    columns = ("form", "filingDate", "reportDate", "accessionNumber", "primaryDocument", "items",
               "primaryDocDescription")
    arrays = {c: recent.get(c) if isinstance(recent.get(c), list) else [] for c in columns}
    rows = []
    for i in range(len(arrays["form"])):
        row = {}
        for c in columns:
            value = arrays[c][i] if i < len(arrays[c]) else ""
            row[c] = value if isinstance(value, str) else ""
        rows.append(row)
    return rows


def _iso_date(text: str) -> date | None:
    try:
        return date.fromisoformat(text)
    except (TypeError, ValueError):
        return None


def recent_filings(submissions: dict[str, Any], cik: int, limit: int = 12) -> list[dict[str, Any]]:
    out = []
    for row in _filing_rows(submissions):
        if row["form"] not in FORMS_OF_INTEREST or _iso_date(row["filingDate"]) is None:
            continue
        items = [x.strip() for x in row["items"].split(",") if x.strip()]
        accession, document = row["accessionNumber"], row["primaryDocument"]
        out.append({
            "form": row["form"], "filed": row["filingDate"], "period": row["reportDate"],
            "items": [{"code": code, "label": ITEM_LABELS.get(code, f"Item {code}")} for code in items],
            "description": row["primaryDocDescription"],
            "url": ARCHIVE_URL.format(cik=cik, accession=accession.replace("-", ""), document=document)
            if document and accession else f"https://www.sec.gov/cgi-bin/browse-edgar?action=getcompany&CIK={cik}",
        })
        if len(out) >= limit:
            break
    return out


def earnings_release_dates(submissions: dict[str, Any]) -> list[date]:
    """Filing dates of 8-Ks carrying item 2.02 (results of operations), newest first."""
    out = set()
    for row in _filing_rows(submissions):
        when = _iso_date(row["filingDate"])
        if row["form"] == "8-K" and when and "2.02" in [x.strip() for x in row["items"].split(",")]:
            out.add(when)
    return sorted(out, reverse=True)


REPORTED_WINDOW_DAYS = 7    # an actual release this close to a projected date counts as that report
OVERDUE_LIMIT_DAYS = 45     # after this long with no release, assume the filing list is incomplete


def estimate_next_earnings(release_dates: list[date], today: date | None = None) -> dict[str, Any] | None:
    """Project the next earnings date from the rhythm of past releases.

    Companies report on a steady calendar, so each quarter's release falls close to one
    year after the same quarter's release the year before. Every regular past release
    therefore projects a date 52 weeks on. The estimate is the earliest projection that
    has not been met by an actual release within a week of it. A projection that has
    already passed without a matching release is reported as overdue rather than skipped:
    the report is late, not cancelled, and can arrive any day.

    Item 2.02 filings off the quarterly rhythm (preliminary results, restatements) are
    ignored when they sit closer than about a quarter to their neighbours, and a filing
    more than a week before a projected date is not accepted as that quarter's report.
    This errs toward expecting a report that has in fact been filed early; entering the
    confirmed date settles it. The result is an estimate, not a confirmed date.
    """
    today = today or date.today()
    dates = sorted(set(release_dates))
    if len(dates) < 4:
        return None
    # A regular release has another release about a quarter before or after it.
    regular = [d for d in dates if any(75 <= abs((d - other).days) <= 110 for other in dates)]
    if len(regular) < 4:
        return None
    last = regular[-1]
    projected = None
    for candidate in sorted(d + timedelta(days=364) for d in regular):
        if candidate < today - timedelta(days=OVERDUE_LIMIT_DAYS):
            continue
        if any(abs((candidate - actual).days) <= REPORTED_WINDOW_DAYS for actual in dates):
            continue                                   # that quarter's report has been filed
        projected = candidate
        break
    if projected is None:
        # History stops more than a year back or has holes: step forward a quarter at a time.
        gaps = [(b - a).days for a, b in zip(regular, regular[1:]) if 75 <= (b - a).days <= 110]
        step = round(statistics.median(gaps)) if gaps else 91
        projected = last + timedelta(days=step)
        while projected < today:
            projected += timedelta(days=step)
    return {"date": projected.isoformat(), "estimated": True, "overdue": projected < today,
            "source": f"estimated from {len(regular)} past earnings releases filed with the SEC",
            "last_release": dates[-1].isoformat()}


def _quarterly_series(facts: dict[str, Any], tags: tuple[str, ...], unit: str) -> list[dict[str, Any]]:
    """Single-quarter values for the first tag that has data, newest first.

    Uses the SEC's `frame` label (for example CY2025Q3), which marks one de-duplicated
    value per calendar quarter and so excludes year-to-date figures.
    """
    def child(node: Any, key: str) -> dict:
        value = node.get(key) if isinstance(node, dict) else None
        return value if isinstance(value, dict) else {}

    gaap = child(child(facts, "facts"), "us-gaap")
    best: list[dict[str, Any]] = []
    for tag in tags:
        entries = child(child(gaap, tag), "units").get(unit)
        rows = {}
        for e in entries if isinstance(entries, list) else []:
            if not isinstance(e, dict):
                continue
            frame = e.get("frame") if isinstance(e.get("frame"), str) else ""
            value = e.get("val")
            if (len(frame) == 8 and frame.startswith("CY") and frame[6] == "Q" and frame[2:6].isdigit()
                    and frame[7] in "1234" and isinstance(value, (int, float)) and not isinstance(value, bool)):
                rows[frame] = {"quarter": f"{frame[2:6]} Q{frame[7]}", "end": str(e.get("end") or ""),
                               "value": float(value), "form": e.get("form"), "filed": e.get("filed")}
        series = [rows[k] for k in sorted(rows, reverse=True)]
        # Prefer the tag whose newest quarter is the most recent (companies change tags over time).
        if series and (not best or series[0]["quarter"] > best[0]["quarter"]):
            best = series
    return best


def fundamentals(facts: dict[str, Any], quarters: int = 6) -> dict[str, Any]:
    out: dict[str, Any] = {}
    series = {"revenue": _quarterly_series(facts, REVENUE_TAGS, "USD"),
              "net_income": _quarterly_series(facts, FACT_TAGS["net_income"], "USD"),
              "eps_diluted": _quarterly_series(facts, FACT_TAGS["eps_diluted"], "USD/shares")}
    for name, rows in series.items():
        by_quarter = {r["quarter"]: r["value"] for r in rows}
        trimmed = []
        for r in rows[:quarters]:
            year, q = r["quarter"].split(" ")
            prior = by_quarter.get(f"{int(year) - 1} {q}")
            change = (r["value"] / prior - 1.0) if prior not in (None, 0) and prior > 0 else None
            trimmed.append({**r, "year_over_year": change})
        out[name] = trimmed
    out["note"] = ("Single-quarter figures as tagged in SEC filings. Fourth quarters are often missing "
                   "because companies report them only inside the annual total.")
    return out


def build_company_research(client: SecClient, ticker: str, today: date | None = None) -> dict[str, Any]:
    """Filings, fundamentals and an earnings estimate. Each part degrades separately."""
    result: dict[str, Any] = {"available": False, "filings": [], "fundamentals": None, "earnings": None,
                              "company": None, "errors": []}
    if not client.enabled:
        result["errors"].append("SEC filings are switched off. Set SEC_USER_AGENT in .env to load them.")
        return result
    try:
        cik, title = client.cik_for(ticker)
        subs = client.submissions(cik)
    except ResearchError as exc:
        result["errors"].append(str(exc))
        return result
    if not isinstance(subs, dict):
        result["errors"].append("The SEC returned filings in a form this app does not understand.")
        return result
    result["available"] = True
    result["company"] = {"name": subs.get("name") or title, "cik": cik,
                         "industry": subs.get("sicDescription") or "",
                         "fiscal_year_end": subs.get("fiscalYearEnd") or "",
                         "filings_url": f"https://www.sec.gov/cgi-bin/browse-edgar?action=getcompany&CIK={cik}"}
    result["filings"] = recent_filings(subs, cik)
    result["earnings"] = estimate_next_earnings(earnings_release_dates(subs), today)
    cache_key = f"sec:fundamentals:{cik}"
    result["fundamentals"] = client.db.cache_get(cache_key, 24 * 3600)
    if result["fundamentals"] is None:
        try:
            result["fundamentals"] = fundamentals(client.company_facts(cik))
            client.db.cache_put(cache_key, result["fundamentals"])
        except ResearchError as exc:
            result["errors"].append(f"Reported results unavailable: {exc}")
    return result
