"""Regression tests for the review of 2.9.0.

Each test is one case from the review table: short stock in the imported history, shares an
assignment or an exercise delivers, the risk a position actually took, money recorded outside
the fills, an option plan's levels, a quote stamp without a time zone, and a locked quote.
docs/IMPROVEMENTS.md lists the findings behind them.
"""

from __future__ import annotations

import csv
import io
import math
from datetime import date, datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from tradeassist import portfolio
from tradeassist.api import create_app
from tradeassist.chain import analyze_chain
from tradeassist.config import Settings
from tradeassist.db import Database
from tradeassist.optionmath import NY, black76_price, years_to_expiry
from tradeassist.positions import add_adjustment, add_execution, create, read
from tradeassist.pricer import quote_age_seconds
from tradeassist.providers.demo import DemoData
from tradeassist.service import TradeAssistant
from tradeassist.tradeanalysis import rebuild, summarize
from tradeassist.tradehistory import activity, parse_report, store

HEADER = ["Activity Date", "Process Date", "Settle Date", "Instrument", "Description", "Trans Code",
          "Quantity", "Price", "Amount"]
TODAY = date(2026, 10, 16)


def report(*rows: tuple) -> str:
    """Rows oldest first, as (date, instrument, description, code, qty, price, amount); written newest first."""
    buf = io.StringIO()
    w = csv.writer(buf, quoting=csv.QUOTE_ALL, lineterminator="\n")
    w.writerow(HEADER)
    for d, inst, desc, code, qty, price, amount in reversed(rows):
        w.writerow([d, d, d, inst, desc, code, qty, price, amount])
    buf.write('\n"The data provided is for informational purposes only."\n')
    return buf.getvalue()


@pytest.fixture
def db(tmp_path):
    d = Database(tmp_path / "t.db")
    yield d
    d.close()


@pytest.fixture
def memdb():
    d = Database(":memory:")
    yield d
    d.close()


@pytest.fixture
def client(tmp_path):
    settings = Settings(data_mode="demo", db_path=tmp_path / "t.db")
    svc = TradeAssistant(settings, provider=DemoData(), db=Database(":memory:"))
    return TestClient(create_app(settings, svc)), svc


def book_of(db, text):
    store(db, parse_report(text), "robinhood_csv", "test.csv")
    b = rebuild(activity(db))
    return b, summarize(b, TODAY)


# --- stock sold short --------------------------------------------------------------------------------

def test_a_sale_marked_short_and_the_buy_that_covers_it_are_one_trade(db):
    """A short round trip: the proceeds against the cover, and no long position invented by the cover."""
    b, trades = book_of(db, report(
        ("5/1/2026", "GME", "GameStop", "Sell", "100S", "$30.00", "$2,999.00"),
        ("5/8/2026", "GME", "GameStop", "Buy", "100", "$24.00", "($2,400.00)")))
    assert not b.unmatched
    (t,) = trades                                           # one trade, not a phantom long beside it
    assert t["strategy"] == "Shares (short)" and t["status"] == "CLOSED"
    assert t["realized_pnl"] == pytest.approx(2_999.00 - 2_400.00)      # (sold - covered) x 100
    assert t["risk"] is None and t["unlimited"] is True
    assert not b.open_lots


def test_a_sell_with_nothing_open_and_no_short_mark_is_still_unmatched(db):
    """Only a sale the report marks short opens a short lot; an unexplained sale is listed, not guessed at."""
    b, trades = book_of(db, report(
        ("5/1/2026", "NVDA", "NVIDIA", "Sell", "5", "$120.00", "$600.00")))
    assert trades == [] and [u["code"] for u in b.unmatched] == ["Sell"]
    assert not any(lot.side < 0 for lot in b.open_lots)


def test_a_reverse_split_scales_a_short_lot_and_leaves_its_result_alone(db):
    b, trades = book_of(db, report(
        ("1/2/2026", "XYZ", "XYZ Corp", "Sell", "100S", "$1.00", "$100.00"),
        ("2/2/2026", "XYZ", "XYZ 1 for 10 reverse split", "SPL", "-90", "", ""),
        ("3/2/2026", "XYZ", "XYZ Corp", "Buy", "10", "$5.00", "($50.00)")))
    assert not b.unmatched
    assert any("scaled by 0.1" in n for n in b.notes)       # 100 shares at $1 became 10 at $10
    (t,) = trades
    assert t["strategy"] == "Shares (short)" and t["status"] == "CLOSED"
    assert t["realized_pnl"] == pytest.approx(50.0)         # $100 in, $50 out, whichever units they are in


# --- shares an assignment or an exercise delivers ------------------------------------------------------

def test_an_uncovered_call_assigned_keeps_the_stock_cash_in_the_option_trade(db):
    """Nothing was held to deliver, so the sale leaves the trade short - and is not left unmatched."""
    b, trades = book_of(db, report(
        ("6/1/2026", "AMD", "AMD 6/19/2026 Call $150.00", "STO", "1", "$3.00", "$299.95"),
        ("6/19/2026", "AMD", "Option Assignment for AMD 6/19/2026 Call $150.00", "OASGN", "1S", "", ""),
        ("6/19/2026", "AMD", "AMD", "Sell", "100", "$150.00", "$15,000.00")))
    assert not b.unmatched
    (t,) = trades
    assert t["strategy"] == "Short call (uncovered)" and t["ended"] == "Assigned"
    assert t["from_assignment"] is True and t["status"] == "PARTLY_CLOSED"
    delivered = [leg for leg in t["legs"] if leg["side"].startswith("sold (assignment")]
    assert len(delivered) == 1 and delivered[0]["cash"] == pytest.approx(15_000.0)


def test_a_covered_call_assigned_delivers_its_own_shares_not_an_older_parcel(db):
    b, trades = book_of(db, report(
        ("1/5/2026", "KO", "Coca-Cola", "Buy", "100", "$50.00", "($5,000.00)"),      # held for something else
        ("6/1/2026", "KO", "Coca-Cola", "Buy", "100", "$60.00", "($6,000.00)"),      # the shares written against
        ("6/1/2026", "KO", "KO 6/19/2026 Call $65.00", "STO", "1", "$1.00", "$99.95"),
        ("6/19/2026", "KO", "Option Assignment for KO 6/19/2026 Call $65.00", "OASGN", "1S", "", ""),
        ("6/19/2026", "KO", "Coca-Cola", "Sell", "100", "$65.00", "$6,500.00")))
    assert not b.unmatched
    by = {t["open_date"]: t for t in trades}
    older, write = by["2026-01-05"], by["2026-06-01"]
    assert older["status"] == "OPEN" and older["realized_pnl"] is None        # the older lot is not touched
    assert write["strategy"] == "Buy-write (covered call)" and write["status"] == "CLOSED"
    assert write["ended"] == "Assigned"
    assert write["realized_pnl"] == pytest.approx((6_500.00 - 6_000.00) + 99.95)


def test_a_long_put_exercised_sells_its_shares_into_the_option_trade(db):
    b, trades = book_of(db, report(
        ("6/1/2026", "F", "Ford", "Buy", "100", "$12.00", "($1,200.00)"),
        ("6/1/2026", "F", "F 6/19/2026 Put $11.00", "BTO", "1", "$0.50", "($50.05)"),
        ("6/19/2026", "F", "Option Exercise for F 6/19/2026 Put $11.00", "OEXER", "1S", "", ""),
        ("6/19/2026", "F", "Ford", "Sell", "100", "$11.00", "$1,100.00")))
    assert not b.unmatched
    (t,) = trades
    assert t["ended"] == "Exercised" and t["status"] == "CLOSED" and t["from_assignment"] is True
    assert t["realized_pnl"] == pytest.approx(1_100.00 - 1_200.00 - 50.05)


# --- money outside the fills, and the risk a position took ---------------------------------------------

def test_a_borrow_fee_recorded_today_counts_against_the_daily_stop_but_an_estimate_never_does(memdb):
    now = datetime.now(NY)
    opened = (now - timedelta(days=15)).replace(hour=10, minute=0, second=0, microsecond=0)
    with memdb.tx() as cur:
        pid = create(cur, symbol="CRWD", instrument_type="STOCK", direction="SHORT", quantity=100)
        add_execution(cur, pid, side="SELL_SHORT", quantity=100, price=50.0,
                      executed_at=opened.isoformat(timespec="minutes"))
        add_adjustment(cur, pid, "BORROW_RATE", rate_pct=9.0, effective_at=opened.isoformat(timespec="minutes"))
        before = view(read(cur, pid))
    assert before["financing"]["borrow_estimate"] is not None               # shown, labelled, never added
    plain = portfolio.daily_loss([before], "REALIZED_PLUS_UNREALIZED", 100_000, 3.0, now)
    with memdb.tx() as cur:
        add_adjustment(cur, pid, "BORROW_FEE", amount=-12.50,
                       effective_at=now.replace(microsecond=0).isoformat(timespec="minutes"),
                       period_start=opened.isoformat(timespec="minutes"),
                       period_end=now.replace(microsecond=0).isoformat(timespec="minutes"))
        after = view(read(cur, pid))
    charged = portfolio.daily_loss([after], "REALIZED_PLUS_UNREALIZED", 100_000, 3.0, now)
    assert charged["known_total"] == pytest.approx(plain["known_total"] - 12.50)
    assert charged["coverage_status"] == "COMPLETE"
    # A fee dated before today is that day's cost, not today's.
    backdated = view(read_copy(after, (now - timedelta(days=4)).isoformat()))
    earlier = portfolio.daily_loss([backdated], "REALIZED_PLUS_UNREALIZED", 100_000, 3.0, now)
    assert earlier["known_total"] == pytest.approx(plain["known_total"])


def view(pos: dict) -> dict:
    """A position as the daily-loss figure takes it: with a mark and yesterday's close."""
    return {**pos, "description": "Short 100 CRWD shares", "mark": {"price": 50.0}, "prev_close": 50.0}


def read_copy(pos: dict, effective_at: str) -> dict:
    """The same position with every recorded amount dated `effective_at`."""
    return {**pos, "adjustments": [{**a, "effective_at": effective_at} for a in pos["adjustments"]]}


def test_initial_risk_uses_the_cost_basis_at_the_largest_the_position_was(memdb):
    """Selling part and then adding higher up does not rewrite the risk that was taken at the start."""
    with memdb.tx() as cur:
        pid = create(cur, symbol="MSFT", instrument_type="STOCK", direction="LONG", quantity=100,
                     invalidation_price=90.0)
        add_execution(cur, pid, side="BUY", quantity=100, price=100.0, executed_at="2026-09-01T10:00")
        add_execution(cur, pid, side="SELL", quantity=50, price=110.0, executed_at="2026-09-05T10:00")
        add_execution(cur, pid, side="BUY", quantity=50, price=120.0, executed_at="2026-09-08T10:00")
        led = read(cur, pid)["ledger"]
    assert led["max_open_quantity"] == pytest.approx(100.0)
    assert led["basis_at_max_quantity"] == pytest.approx(100.0)
    assert led["average_entry_price"] == pytest.approx((100 * 100 + 50 * 120) / 150)   # still reported as it was
    assert led["initial_risk"] == pytest.approx((100.0 - 90.0) * 100)                   # $10 a share on 100 shares


# --- an option plan's levels ---------------------------------------------------------------------------

def test_a_long_put_plan_with_its_invalidation_below_the_stock_is_refused(client):
    """The levels are stock prices: a bought put is proved wrong by a rise, so the invalidation
    sits above the stock. Below it, the plan would be invalid the moment it was recorded."""
    c, svc = client
    spot = svc.desk._spot("AAPL")
    assert spot and spot > 1
    body = {"symbol": "AAPL", "instrument_type": "OPTION", "quantity": 1, "thesis": "t" * 20,
            "option": {"option_type": "PUT", "strike": round(spot),
                       "expiration": (datetime.now(NY) + timedelta(days=45)).date().isoformat()},
            "invalidation_price": round(spot * 0.95, 2)}
    bad = c.post("/v2/positions", json=body)
    assert bad.status_code == 400 and "wrong from the start" in bad.json()["error"]
    good = c.post("/v2/positions", json={**body, "invalidation_price": round(spot * 1.05, 2)})
    assert good.status_code == 200, good.text
    assert good.json()["invalidation_price"] == pytest.approx(round(spot * 1.05, 2))
    # A bought call is read the other way round: the invalidation goes below the stock.
    call = {**body, "option": {**body["option"], "option_type": "CALL"},
            "invalidation_price": round(spot * 1.05, 2)}
    assert c.post("/v2/positions", json=call).status_code == 400
    assert c.post("/v2/positions", json={**call, "invalidation_price": round(spot * 0.95, 2)}).status_code == 200


# --- quotes ---------------------------------------------------------------------------------------------

def test_a_quote_stamp_without_a_time_zone_still_gives_an_age():
    now = datetime(2026, 10, 16, 14, 0, tzinfo=timezone.utc)
    age = quote_age_seconds({"bid_time": "2026-10-16T13:58:00", "ask_time": "2026-10-16T13:59:00+00:00"}, now)
    assert isinstance(age, float) and age == pytest.approx(120.0)
    assert quote_age_seconds({"bid_time": "not a time"}, now) is None


def _chain(locked_call: float | None = None) -> tuple[list[dict], datetime]:
    """Six strikes of calls and puts priced off a mild smile, each quoted a cent either side of its
    model price. `locked_call` names a call strike quoted at one price on both sides."""
    now = datetime(2026, 10, 16, 15, 0, tzinfo=timezone.utc)
    spot, rate, expiration = 100.0, 0.04, "2026-12-18"
    t = years_to_expiry(expiration, now)
    forward = spot * math.exp(rate * t)
    out = []
    for strike in (90.0, 95.0, 100.0, 105.0, 110.0, 115.0):
        k = math.log(strike / forward)
        vol = 0.30 - 0.45 * k + 1.5 * k * k
        for kind in ("call", "put"):
            mid = round(black76_price(forward, strike, t, rate, vol, kind), 2)
            bid, ask = round(mid - 0.01, 2), round(mid + 0.01, 2)
            if kind == "call" and locked_call is not None and strike == locked_call:
                bid = ask = mid
            out.append({"symbol": f"TEST{strike:g}{kind[0].upper()}", "option_type": kind, "strike": strike,
                        "bid": bid, "ask": ask})
    return out, now


def test_a_strike_quoted_at_one_price_on_both_sides_stays_in_the_smile():
    """A spread of zero is the tightest quote there is, not a missing one."""
    loose, now = _chain()
    locked, _ = _chain(locked_call=110.0)
    a = analyze_chain("TEST", "2026-12-18", loose, 100.0, 0.04, now)
    b = analyze_chain("TEST", "2026-12-18", locked, 100.0, 0.04, now)
    assert a.smile is not None and b.smile is not None
    assert b.find_by_terms("call", 110.0)["spread_pct"] == pytest.approx(0.0)
    assert b.smile.n_points == a.smile.n_points                      # the strike is still one of them
    assert b.find_by_terms("call", 110.0)["model_source"] == "smile fitted across strikes"
