"""2.6: importing your Robinhood history and rebuilding your trades from it.

The report's layout (columns, money in parentheses, option descriptions, the trailing S on some
expirations, the newest-first order, the closing disclaimer) follows what Robinhood's account activity
report contains; each rule below is pinned with a small hand-made report.
"""

from __future__ import annotations

import csv
import io
from datetime import date

import pytest
from fastapi.testclient import TestClient

from tradeassist.api import create_app
from tradeassist.config import Settings
from tradeassist.db import Database
from tradeassist.providers.demo import DemoData
from tradeassist.service import TradeAssistant
from tradeassist.tradeanalysis import analyse, period_bounds, period_key, rebuild, summarize, wilson
from tradeassist.tradehistory import HistoryImportError, activity, money, option_terms, parse_report, quantity, store

HEADER = ["Activity Date", "Process Date", "Settle Date", "Instrument", "Description", "Trans Code", "Quantity", "Price", "Amount"]


def report(*rows: tuple, footer: bool = True) -> str:
    """Rows oldest first, as (date, instrument, description, code, qty, price, amount); written newest first."""
    buf = io.StringIO()
    w = csv.writer(buf, quoting=csv.QUOTE_ALL, lineterminator="\n")
    w.writerow(HEADER)
    for d, inst, desc, code, qty, price, amount in reversed(rows):
        w.writerow([d, d, d, inst, desc, code, qty, price, amount])
    if footer:
        buf.write('\n"The data provided is for informational purposes only."\n')
    return buf.getvalue()


@pytest.fixture
def db(tmp_path):
    d = Database(tmp_path / "t.db")
    yield d
    d.close()


def book_of(db, text):
    store(db, parse_report(text), "robinhood_csv", "test.csv")
    b = rebuild(activity(db))
    return b, summarize(b, date(2026, 10, 11))


# --- reading the report ------------------------------------------------------------------------------

def test_money_quantity_and_contract_terms_are_read_as_robinhood_writes_them():
    assert money("($848.27)") == -848.27 and money("$2,275.40") == 2275.40 and money("-$1.00") == -1.0
    assert money("") is None
    with pytest.raises(ValueError):
        money("n/a")
    assert quantity("2S") == (2.0, True) and quantity("0.810399") == (0.810399, False) and quantity("") == (None, False)
    assert option_terms("IWM 6/12/2026 Put $232.00") == ("IWM", "PUT", 232.0, "2026-06-12")
    assert option_terms("Option Expiration for LULU 4/16/2026 Call $310.00") == ("LULU", "CALL", 310.0, "2026-04-16")
    assert option_terms("BRK.B 1/15/2027 Call $1,000.00") == ("BRK.B", "CALL", 1000.0, "2027-01-15")
    assert option_terms("Apple\nCUSIP: 037833100") is None


def test_a_file_that_is_not_the_report_is_refused_with_what_to_do():
    with pytest.raises(HistoryImportError, match="Account activity report"):
        parse_report("Date,Symbol,Qty\n1/2/2026,AAPL,1\n")
    with pytest.raises(HistoryImportError, match="empty"):
        parse_report("")


def test_trades_are_kept_other_rows_counted_and_unreadable_ones_reported_not_guessed():
    text = report(("1/5/2026", "", "ACH Deposit", "ACH", "", "", "$500.00"),
                  ("1/6/2026", "AAPL", "Apple\nCUSIP: 037833100", "Buy", "10", "$180.00", "($1,800.00)"),
                  ("1/7/2026", "AAPL", "AAPL 2/20/2026 Call $190.00", "BTO", "1", "$3.10", "($310.04)"),
                  ("1/8/2026", "AAPL", "Cash Div: R/D 2026-01-02", "CDIV", "", "", "$2.40"),
                  ("1/9/2026", "AAPL", "AAPL somewhere Call", "STC", "1", "$4.00", "$399.96"),
                  ("1/9/2026", "XYZ", "XYZ Merger", "MRGS", "5", "", ""))
    p = parse_report(text)
    assert p.read == 6
    assert sorted(r.code for r in p.rows) == ["BTO", "Buy"]
    assert dict(p.ignored) == {"ACH": 1, "CDIV": 1, "MRGS": 1}
    assert len(p.unreadable) == 1 and "does not name the contract" in p.unreadable[0]["reason"]
    assert p.unmodelled and p.unmodelled[0]["code"] == "MRGS"


def test_the_same_rows_imported_twice_are_kept_once_but_identical_fills_in_one_report_both_count(db):
    fill = ("1/6/2026", "AAPL", "AAPL 2/20/2026 Call $190.00", "BTO", "1", "$3.10", "($310.04)")
    text = report(fill, fill)
    first = store(db, parse_report(text), "robinhood_csv", "a.csv")
    second = store(db, parse_report(text), "robinhood_csv", "a.csv")
    assert (first["rows_added"], first["rows_duplicate"]) == (2, 0)
    assert (second["rows_added"], second["rows_duplicate"]) == (0, 2)
    assert len(activity(db)) == 2


# --- rebuilding trades --------------------------------------------------------------------------------

def test_shares_close_first_in_first_out_from_the_cash_actually_paid(db):
    b, trades = book_of(db, report(
        ("1/2/2026", "AAPL", "Apple", "Buy", "10", "$100.00", "($1,000.00)"),
        ("1/5/2026", "AAPL", "Apple", "Buy", "10", "$110.00", "($1,100.00)"),
        ("1/9/2026", "AAPL", "Apple", "Sell", "15", "$120.00", "$1,799.95")))
    first, second = trades
    assert first["status"] == "CLOSED" and first["realized_pnl"] == pytest.approx(10 * 119.9967 - 1000, abs=0.01)
    assert second["status"] == "PARTLY_CLOSED" and second["realized_pnl"] == pytest.approx(5 * (1799.95 / 15 - 110), abs=0.01)
    assert first["strategy"] == "Shares (long)" and first["risk"] == pytest.approx(1000)


def test_options_expire_assign_and_close_with_their_own_results(db):
    b, trades = book_of(db, report(
        ("3/2/2026", "TSLA", "TSLA 3/20/2026 Call $250.00", "BTO", "2", "$4.00", "($800.08)"),
        ("3/2/2026", "KO", "KO 3/20/2026 Put $60.00", "STO", "1", "$0.90", "$89.96"),
        ("3/9/2026", "SPY", "SPY 3/13/2026 Put $500.00", "BTO", "1", "$2.00", "($200.04)"),
        ("3/13/2026", "SPY", "Option Expiration for SPY 3/13/2026 Put $500.00", "OEXP", "1", "", ""),
        ("3/20/2026", "TSLA", "Option Expiration for TSLA 3/20/2026 Call $250.00", "OEXP", "2", "", ""),
        ("3/20/2026", "KO", "Option Assignment for KO 3/20/2026 Put $60.00", "OASGN", "1S", "", ""),
        ("3/20/2026", "KO", "Coca-Cola", "Buy", "100", "$60.00", "($6,000.00)")))
    by = {(t["symbol"], t["strategy"]): t for t in trades}
    call = by[("TSLA", "Long call")]
    assert call["ended"] == "Expired" and call["realized_pnl"] == pytest.approx(-800.08) and call["risk"] == pytest.approx(800.08)
    put = by[("KO", "Short put")]
    assert put["ended"] == "Assigned" and put["realized_pnl"] == pytest.approx(89.96)
    assert put["risk"] == pytest.approx(60 * 100 - 89.96)                   # to zero, less the premium
    # The shares the assignment delivered belong to the same trade, which stays open until they are sold.
    assert put["status"] == "PARTLY_CLOSED" and put["from_assignment"] is True and put["result"] is None
    assert ("KO", "Shares (long)") not in by
    assert by[("SPY", "Long put")]["dte_at_open"] == 4


def test_a_close_with_nothing_open_is_left_out_and_listed_not_counted_as_zero(db):
    b, trades = book_of(db, report(
        ("2/2/2026", "NVDA", "NVDA 2/20/2026 Call $130.00", "STC", "1", "$5.00", "$499.96"),
        ("2/3/2026", "NVDA", "NVDA", "Sell", "5", "$120.00", "$600.00")))
    assert trades == []
    assert [u["code"] for u in b.unmatched] == ["STC", "Sell"]
    out = analyse(b, trades, date(2026, 10, 11))
    assert out["summary"]["net"] is None and out["summary"]["realized"] is None
    assert "left out, not counted as zero" in " ".join(out["notes"])


def test_the_report_order_puts_a_same_day_close_after_its_open(db):
    """Newest first in the file: within one day, the later line happened earlier."""
    b, trades = book_of(db, report(
        ("4/1/2026", "AMD", "AMD 4/1/2026 Call $150.00", "BTO", "3", "$1.00", "($300.12)"),
        ("4/1/2026", "AMD", "AMD 4/1/2026 Call $150.00", "STC", "3", "$1.50", "$449.88")))
    assert not b.unmatched
    (t,) = trades
    assert t["days_held"] == 0 and t["realized_pnl"] == pytest.approx(149.76)


def test_a_split_scales_the_open_shares_and_their_cost(db):
    b, trades = book_of(db, report(
        ("1/2/2026", "NVDA", "NVIDIA", "Buy", "10", "$1,000.00", "($10,000.00)"),
        ("2/1/2026", "NVDA", "Forward split", "SPL", "90", "", ""),
        ("3/1/2026", "NVDA", "NVIDIA", "Sell", "100", "$110.00", "$11,000.00")))
    (t,) = trades
    assert t["status"] == "CLOSED" and t["realized_pnl"] == pytest.approx(1000.0)
    assert any("split" in n for n in b.notes)


# --- strategies and risk --------------------------------------------------------------------------

LEGS = {
    "Call debit spread": ([("AAPL 5/15/2026 Call $190.00", "BTO", "$5.00", "($500.00)"), ("AAPL 5/15/2026 Call $200.00", "STO", "$2.00", "$200.00")], 300.0),
    "Put credit spread": ([("AAPL 5/15/2026 Put $180.00", "STO", "$3.00", "$300.00"), ("AAPL 5/15/2026 Put $175.00", "BTO", "$1.00", "($100.00)")], 500 - 200.0),
    "Long straddle": ([("AAPL 5/15/2026 Call $190.00", "BTO", "$5.00", "($500.00)"), ("AAPL 5/15/2026 Put $190.00", "BTO", "$4.00", "($400.00)")], 900.0),
    "Short strangle": ([("AAPL 5/15/2026 Call $200.00", "STO", "$2.00", "$200.00"), ("AAPL 5/15/2026 Put $180.00", "STO", "$2.00", "$200.00")], None),
    "Iron condor": ([("AAPL 5/15/2026 Put $170.00", "STO", "$2.00", "$200.00"), ("AAPL 5/15/2026 Put $165.00", "BTO", "$1.00", "($100.00)"),
                     ("AAPL 5/15/2026 Call $210.00", "STO", "$2.00", "$200.00"), ("AAPL 5/15/2026 Call $220.00", "BTO", "$0.50", "($50.00)")], 1000 - 250.0),
    "Calendar or diagonal": ([("AAPL 6/18/2026 Call $190.00", "BTO", "$7.00", "($700.00)"), ("AAPL 5/15/2026 Call $190.00", "STO", "$4.00", "$400.00")], 300.0),
    "Butterfly": ([("AAPL 5/15/2026 Call $180.00", "BTO", "$12.00", "($1,200.00)"), ("AAPL 5/15/2026 Call $190.00", "STO", "$6.00", "$1,200.00"),
                   ("AAPL 5/15/2026 Call $200.00", "BTO", "$2.50", "($250.00)")], 250.0),
    "Short call (uncovered)": ([("AAPL 5/15/2026 Call $210.00", "STO", "$2.00", "$200.00")], None),
}


@pytest.mark.parametrize("name", list(LEGS))
def test_each_strategy_is_named_from_its_legs_with_its_most_it_can_lose(db, name):
    legs, risk = LEGS[name]
    rows = []
    for desc, code, price, amount in legs:
        qty = "2" if name == "Butterfly" and "190" in desc else "1"
        rows.append(("4/14/2026", "AAPL", desc, code, qty, price, amount))
    b, trades = book_of(db, report(*rows))
    (t,) = trades
    assert t["strategy"] == name
    if risk is None:
        assert t["risk"] is None and t["unlimited"] is True and t["risk_basis"].startswith("unlimited")
    else:
        assert t["risk"] == pytest.approx(risk)


def test_a_call_sold_against_shares_held_is_covered_and_one_against_none_is_not(db):
    b, trades = book_of(db, report(
        ("1/2/2026", "KO", "Coca-Cola", "Buy", "100", "$60.00", "($6,000.00)"),
        ("1/9/2026", "KO", "KO 2/20/2026 Call $65.00", "STO", "1", "$0.80", "$80.00"),
        ("1/12/2026", "PEP", "PEP 2/20/2026 Call $180.00", "STO", "1", "$1.00", "$100.00"),
        ("1/12/2026", "JPM", "JPMorgan", "Buy", "100", "$200.00", "($20,000.00)"),
        ("1/12/2026", "JPM", "JPM 2/20/2026 Call $210.00", "STO", "1", "$2.00", "$200.00")))
    names = {t["symbol"]: (t["strategy"], t["risk"], t["risk_basis"]) for t in trades if t["symbol"] != "KO" or "call" in t["strategy"].lower()}
    assert names["KO"][0] == "Covered call" and names["KO"][1] is None and "covered" in names["KO"][2]
    assert names["PEP"][0] == "Short call (uncovered)" and names["PEP"][1] is None
    assert names["JPM"] == ("Buy-write (covered call)", pytest.approx(20000 - 200), "the cost of the shares less the premium received")


# --- the analysis ------------------------------------------------------------------------------------

def test_periods_split_on_their_own_bounds():
    assert period_key("2026-03-31", "quarter") == "2026-Q1" and period_bounds("2026-Q4", "quarter") == ("2026-10-01", "2026-12-31")
    assert period_key("2026-10-11", "week") == "2026-10-05" and period_bounds("2026-10-05", "week") == ("2026-10-05", "2026-10-11")
    assert period_bounds("2024-02", "month") == ("2024-02-01", "2024-02-29")
    lo, hi = wilson(7, 10)
    assert lo < 0.7 < hi and wilson(0, 0) == (None, None)


def test_a_date_range_picks_trades_by_when_they_closed_or_opened(db):
    b, trades = book_of(db, report(
        ("1/5/2026", "AAPL", "AAPL 2/20/2026 Call $190.00", "BTO", "1", "$3.00", "($300.00)"),
        ("2/2/2026", "AAPL", "AAPL 2/20/2026 Call $190.00", "STC", "1", "$5.00", "$500.00"),
        ("2/10/2026", "MSFT", "MSFT 3/20/2026 Put $400.00", "BTO", "1", "$4.00", "($400.00)"),
        ("3/3/2026", "MSFT", "MSFT 3/20/2026 Put $400.00", "STC", "1", "$1.00", "$100.00")))
    feb_close = analyse(b, trades, date(2026, 10, 11), "2026-02-01", "2026-02-28", "month", "close")
    assert [t["symbol"] for t in feb_close["trades"] if t["in_results"]] == ["AAPL"] and feb_close["summary"]["net"] == pytest.approx(200)
    # MSFT was open in February: not in its results, but in its risk.
    assert [t["symbol"] for t in feb_close["trades"] if not t["in_results"]] == ["MSFT"]
    assert feb_close["summary"]["risk"]["of"] == 2
    feb_open = analyse(b, trades, date(2026, 10, 11), "2026-02-01", "2026-02-28", "month", "open")
    assert [t["symbol"] for t in feb_open["trades"] if t["in_results"]] == ["MSFT"] and feb_open["summary"]["net"] == pytest.approx(-300)
    # By open date, the result belongs to the month it was opened, and nothing falls outside the range.
    assert [(p["key"], round(p["realized"]), p["trades"]) for p in feb_open["periods"]] == [("2026-02", -300, 1)]
    assert feb_open["summary"]["realized"] == pytest.approx(-300)
    whole = analyse(b, trades, date(2026, 10, 11), None, None, "month", "close")
    assert [(p["key"], round(p["realized"])) for p in whole["periods"] if p["trades"]] == [("2026-02", 200), ("2026-03", -300)]
    assert whole["summary"]["max_drawdown"] == pytest.approx(300)
    with pytest.raises(ValueError, match="after the end"):
        analyse(b, trades, date(2026, 10, 11), "2026-03-01", "2026-02-01")


def test_patterns_compare_each_kind_of_trade_with_your_average_and_need_enough_trades(db):
    rows = []
    for i in range(6):        # six AMD calls two days from expiration that lose 80, six NVDA 40-day calls that win 100
        d1, d2 = f"1/{5 + i}/2026", f"1/{6 + i}/2026"
        rows += [(d1, "AMD", f"AMD 1/{7 + i}/2026 Call $150.00", "BTO", "1", "$1.00", "($100.00)"),
                 (d2, "AMD", f"AMD 1/{7 + i}/2026 Call $150.00", "STC", "1", "$0.20", "$20.00"),
                 (d1, "NVDA", "NVDA 2/20/2026 Call $" + str(160 + i) + ".00", "BTO", "1", "$3.00", "($300.00)"),
                 (d2, "NVDA", "NVDA 2/20/2026 Call $" + str(160 + i) + ".00", "STC", "1", "$4.00", "$400.00")]
    b, trades = book_of(db, report(*rows))
    assert len(trades) == 12
    pats = analyse(b, trades, date(2026, 10, 11))["patterns"]
    assert pats["overall"]["average"] == pytest.approx(10)
    # Two kinds of one feature are one fact seen from both sides: it is listed once, not as a mirrored pair.
    listed = [r for r in pats["failure"] + pats["success"] if r["feature"] == "dte"]
    assert len(listed) == 1 and abs(listed[0]["edge"]) == pytest.approx(540)
    table = {r["value"]: r for r in pats["table"] if r["feature"] == "dte"}
    assert table["0 to 2 days"]["edge"] == pytest.approx(-480 - 60) and table["31 to 60 days"]["edge"] == pytest.approx(600 - 60)
    # A value every trade shares (all long calls) says nothing by contrast, so it is not ranked.
    assert all(r["feature"] != "strategy" for r in pats["success"] + pats["failure"])
    few = analyse(b, trades[:4], date(2026, 10, 11))["patterns"]
    assert few["success"] == [] and few["failure"] == []       # under 5 trades a kind, nothing is ranked


def test_dollar_days_and_peak_exposure_never_add_unlimited_risk_as_zero(db):
    b, trades = book_of(db, report(
        ("1/5/2026", "PEP", "PEP 2/20/2026 Call $180.00", "STO", "1", "$1.00", "$100.00"),
        ("1/5/2026", "AAPL", "AAPL 2/20/2026 Call $190.00", "BTO", "1", "$3.00", "($300.00)"),
        ("1/15/2026", "AAPL", "AAPL 2/20/2026 Call $190.00", "STC", "1", "$5.00", "$500.00"),
        ("1/20/2026", "PEP", "PEP 2/20/2026 Call $180.00", "BTC", "1", "$0.50", "($50.00)")))
    out = analyse(b, trades, date(2026, 10, 11), "2026-01-01", "2026-01-31")
    rk = out["summary"]["risk"]
    assert rk["unlimited"] == 1 and rk["measured"] == 1 and rk["total_at_open"] == pytest.approx(300)
    assert out["strategies"][0]["strategy"] == "Short call (uncovered)"        # unlimited risk ranks first
    day = next(x for x in out["exposure"] if x["date"] == "2026-01-07")
    assert day["risk"] == pytest.approx(300) and day["unlimited"] == 1


# --- the API -----------------------------------------------------------------------------------------

@pytest.fixture
def client(tmp_path):
    settings = Settings(data_mode="demo", db_path=tmp_path / "t.db")
    svc = TradeAssistant(settings, provider=DemoData(), db=Database(tmp_path / "t.db"))
    with TestClient(create_app(settings, svc)) as c:
        yield c
    svc.db.close()


def test_import_analyse_verify_and_remove_through_the_api(client):
    text = report(("1/5/2026", "AAPL", "AAPL 2/20/2026 Call $190.00", "BTO", "1", "$3.00", "($300.00)"),
                  ("2/2/2026", "AAPL", "AAPL 2/20/2026 Call $190.00", "STC", "1", "$5.00", "$500.00"))
    r = client.post("/v2/history/import", json={"filename": "activity.csv", "text": text})
    assert r.status_code == 200 and r.json()["rows_added"] == 2
    a = client.get("/v2/history/analysis?by=month").json()
    (t,) = a["trades"]
    assert t["realized_pnl"] == pytest.approx(200) and a["imports"][0]["filename"] == "activity.csv"
    d = client.get(f"/v2/history/trade/{t['id']}").json()
    assert [row["code"] for row in d["rows"]] == ["BTO", "STC"]
    assert d["rows"][0]["raw"]["Amount"] == "($300.00)"            # the statement's own text, to check against
    assert client.get("/v2/history/analysis?start=2026-13-01").status_code == 400
    imp = a["imports"][0]["id"]
    assert client.post(f"/v2/history/imports/{imp}/remove").json()["rows_removed"] == 2
    assert client.get("/v2/history/analysis").json()["empty"] is True


def test_the_sample_is_for_demo_mode_and_the_flag_switches_it_all_off(tmp_path, client):
    assert client.post("/v2/history/sample").status_code == 200
    a = client.get("/v2/history/analysis").json()
    assert a["sample"] is True and len(a["trades"]) > 50
    from tradeassist.features import FEATURE_DEFAULTS
    settings = Settings(data_mode="demo", db_path=tmp_path / "o.db", features={**FEATURE_DEFAULTS, "trade_history": False})
    svc = TradeAssistant(settings, provider=DemoData(), db=Database(tmp_path / "o.db"))
    with TestClient(create_app(settings, svc)) as c:
        assert c.get("/v2/history/analysis").status_code == 404
    svc.db.close()


# --- review fixes: each pinned -----------------------------------------------------------------------

def test_a_leg_filled_in_several_rows_is_one_leg_and_the_strategy_is_named_from_it(db):
    b, trades = book_of(db, report(
        ("4/14/2026", "AAPL", "AAPL 5/15/2026 Call $190.00", "BTO", "1", "$5.00", "($500.00)"),
        ("4/14/2026", "AAPL", "AAPL 5/15/2026 Call $190.00", "BTO", "1", "$5.10", "($510.00)"),
        ("4/14/2026", "AAPL", "AAPL 5/15/2026 Call $200.00", "STO", "2", "$2.00", "$400.00")))
    (t,) = trades
    assert t["strategy"] == "Call debit spread" and t["risk"] == pytest.approx(1010 - 400)


def test_a_close_listed_before_its_open_on_the_same_day_still_finds_it(db):
    """The report has no times: when one day's lines put a close before its open, the close is tried again
    after that day's opens instead of being dropped."""
    b, trades = book_of(db, report(
        ("4/1/2026", "AMD", "AMD 4/17/2026 Call $150.00", "STC", "2", "$1.50", "$299.92"),
        ("4/1/2026", "AMD", "AMD 4/17/2026 Call $150.00", "BTO", "2", "$1.00", "($200.08)")))
    assert not b.unmatched
    (t,) = trades
    assert t["status"] == "CLOSED" and t["realized_pnl"] == pytest.approx(99.84)


def test_proceeds_smaller_than_the_fees_are_a_negative_amount_not_an_estimate(db):
    b, trades = book_of(db, report(
        ("5/1/2026", "F", "F 5/15/2026 Call $14.00", "BTO", "1", "$0.50", "($50.04)"),
        ("5/14/2026", "F", "F 5/15/2026 Call $14.00", "STC", "1", "$0.01", "($0.02)")))
    (t,) = trades
    assert t["realized_pnl"] == pytest.approx(-50.06) and t["estimated"] is False


def test_a_reverse_split_takes_shares_away_and_keeps_the_cost(db):
    b, trades = book_of(db, report(
        ("1/2/2026", "XYZ", "XYZ Corp", "Buy", "100", "$1.00", "($100.00)"),
        ("2/2/2026", "XYZ", "XYZ 1 for 10 reverse split", "SPL", "-90", "", ""),
        ("3/2/2026", "XYZ", "XYZ Corp", "Sell", "10", "$12.00", "$120.00")))
    assert not b.unmatched
    (t,) = trades
    assert t["status"] == "CLOSED" and t["realized_pnl"] == pytest.approx(20.0)


def test_an_option_past_expiration_with_no_closing_row_is_unresolved_not_open_and_not_zero(db):
    b, trades = book_of(db, report(
        ("3/2/2026", "TSLA", "TSLA 3/20/2026 Call $250.00", "BTO", "1", "$4.00", "($400.04)"),
        ("3/2/2026", "KO", "Coca-Cola", "Buy", "1", "$60.00", "($60.00)")))
    t = next(x for x in trades if x["symbol"] == "TSLA")
    assert t["status"] == "UNRESOLVED" and t["result"] is None and t["realized_pnl"] is None
    assert t["ended"] == "No record after expiration"
    out = analyse(b, trades, date(2026, 10, 11), "2026-03-01", "2026-04-30")
    assert out["summary"]["unresolved"] == 1 and out["summary"]["open_now"] == 1          # the KO shares only
    assert "passed expiration" in " ".join(out["notes"])
    after = next(x for x in out["exposure"] if x["date"] == "2026-03-23")
    assert after["risk"] == pytest.approx(60)                                          # its risk ends at expiration


def test_open_risk_shrinks_as_a_trade_is_partly_closed(db):
    b, trades = book_of(db, report(
        ("1/5/2026", "AAPL", "Apple", "Buy", "10", "$100.00", "($1,000.00)"),
        ("1/12/2026", "AAPL", "Apple", "Sell", "5", "$110.00", "$550.00"),
        ("1/20/2026", "AAPL", "Apple", "Sell", "5", "$90.00", "$450.00")))
    out = analyse(b, trades, date(2026, 10, 11), "2026-01-01", "2026-01-31")
    on = {x["date"]: x for x in out["exposure"]}
    assert on["2026-01-07"]["risk"] == pytest.approx(1000) and on["2026-01-14"]["risk"] == pytest.approx(500)
    assert on["2026-01-26"]["open"] == 0
    assert out["summary"]["peak_exposure"]["risk"] == pytest.approx(1000)
    # Dollar-days: 1000 a day for Jan 5 to 12 (8 days), then 500 a day for Jan 13 to 20 (8 days).
    assert out["summary"]["risk"]["dollar_days"] == pytest.approx(8 * 1000 + 8 * 500)


def test_a_day_with_only_unlimited_risk_open_is_the_peak_not_a_zero(db):
    b, trades = book_of(db, report(
        ("1/5/2026", "PEP", "PEP 2/20/2026 Call $180.00", "STO", "1", "$1.00", "$100.00"),
        ("1/9/2026", "PEP", "PEP 2/20/2026 Call $180.00", "BTC", "1", "$0.50", "($50.00)")))
    peak = analyse(b, trades, date(2026, 10, 11), "2026-01-01", "2026-01-31")["summary"]["peak_exposure"]
    assert peak["risk"] == 0 and peak["unlimited"] == 1 and peak["open"] == 1
    empty = analyse(b, trades, date(2026, 10, 11), "2026-03-01", "2026-03-31")["summary"]
    assert empty["peak_exposure"] is None                                            # nothing open: no peak at all


def test_removing_an_import_keeps_the_rows_another_import_also_has(db):
    from tradeassist.tradehistory import imports, remove_import
    jan = ("1/5/2026", "AAPL", "Apple", "Buy", "10", "$100.00", "($1,000.00)")
    feb = ("2/5/2026", "AAPL", "Apple", "Sell", "10", "$110.00", "$1,100.00")
    mar = ("3/5/2026", "MSFT", "Microsoft", "Buy", "1", "$400.00", "($400.00)")
    a = store(db, parse_report(report(jan, feb)), "robinhood_csv", "a.csv")
    b = store(db, parse_report(report(feb, mar)), "robinhood_csv", "b.csv")
    assert b["rows_added"] == 1 and b["rows_duplicate"] == 1
    out = remove_import(db, a["import_id"])
    assert out == {"import_id": a["import_id"], "rows_removed": 1, "rows_kept_by_other_imports": 1}
    assert [(r["activity_date"], r["code"]) for r in activity(db)] == [("2026-02-05", "Sell"), ("2026-03-05", "Buy")]
    assert [i["filename"] for i in imports(db)] == ["b.csv"]
    assert remove_import(db, b["import_id"])["rows_removed"] == 2 and activity(db) == []


def test_numbers_that_cannot_be_real_are_refused_and_signed_quantities_kept():
    with pytest.raises(ValueError):
        money("$1" + "0" * 20)
    assert quantity("-90") == (-90.0, False)


def test_a_malformed_file_and_a_long_file_name_are_answered_not_crashed(client):
    huge = report(("1/5/2026", "AAPL", "x" * 200_000, "Buy", "1", "$1.00", "($1.00)"))
    r = client.post("/v2/history/import", json={"filename": "a.csv", "text": huge})
    assert r.status_code == 400 and "could not be read" in r.text.lower()
    ok = report(("1/5/2026", "AAPL", "Apple", "Buy", "1", "$1.00", "($1.00)"))
    r = client.post("/v2/history/import", json={"filename": "n" * 1500 + ".csv", "text": ok})
    assert r.status_code == 200
    assert len(client.get("/v2/history/analysis").json()["imports"][0]["filename"]) == 200


def test_the_analysis_is_never_read_half_built_while_an_import_lands(client):
    import threading
    rows = [(f"1/{d}/2026", "AAPL", "Apple", code, "1", "$100.00", amt) for d in range(5, 25)
            for code, amt in (("Buy", "($100.00)"), ("Sell", "$101.00"))]
    seen, errors = [], []
    history = client.app.state.assistant.history

    def read():
        try:
            for _ in range(15):
                a = history.analysis(None, None, "month", "close")
                seen.append(0 if a.get("empty") else len(a["trades"]))
        except Exception as exc:             # noqa: BLE001
            errors.append(exc)

    readers = [threading.Thread(target=read) for _ in range(4)]
    for t in readers:
        t.start()
    history.import_report(report(*rows), "a.csv")
    for t in readers:
        t.join()
    assert errors == [] and set(seen) <= {0, 20}
