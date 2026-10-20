"""2.15: paper trading: fills, stops, targets and horizons, partial closes, shorts, option limit orders, buying power,
catching up after the app was closed, automated steps with rotation, replay, and isolation from real positions.

The rules pinned here: shares buy at the ask and sell at the bid with the trading cost; a stop sells at the bid once
the stock trades through it and a target at the target or better; an option limit order fills only when the ask
reaches it (REALISTIC); a portfolio never spends money it does not have; and nothing paper ever reaches the real
positions, the journal or the risk limits."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pandas as pd
import pytest

from tradeassist import paper as P
from tradeassist.blend import Blender
from tradeassist.config import Settings
from tradeassist.db import Database
from tradeassist.opportunities import Runs, cost_pct, edge_from, make
from tradeassist.providers.demo import DemoData
from tradeassist.service import TradeAssistant


@pytest.fixture
def svc(tmp_path):
    s = Settings(data_mode="demo", db_path=tmp_path / "p.db", account_size=100_000, stock_cost_bps=5)
    service = TradeAssistant(s, provider=DemoData(live=True), db=Database(tmp_path / "p.db"))
    yield service
    service.db.close()


class Prices:
    """Quotes the test controls: a stock at `p` has a bid a cent under and an ask a cent over."""

    def __init__(self, **prices):
        self.p = dict(prices)
        self.option = {"bid": 2.0, "ask": 2.1}

    def stocks(self, symbols):
        return {s: {"bid": self.p[s] - 0.01, "ask": self.p[s] + 0.01, "last": self.p[s]} for s in symbols if s in self.p}

    def contract(self, pos, under):
        b, a = self.option["bid"], self.option["ask"]
        return {"bid": b, "ask": a, "mid": (a + b) / 2, "basis": "quote"}


@pytest.fixture
def book(svc, monkeypatch):
    prices = Prices(AAPL=100.0, XOM=50.0, SPY=500.0)
    b = P.PaperBook(svc)
    monkeypatch.setattr(b, "_stock_quotes", prices.stocks)
    monkeypatch.setattr(b, "_option_quote", prices.contract)
    b.prices = prices
    return b


def manual(book, budget=10_000, fill="REALISTIC", horizon="swing", ends="open"):
    return book.create({"mode": "MANUAL", "budget": budget, "fill_model": fill, "horizon": horizon, "ends": ends})["portfolio"]["id"]


def pos_of(book, pid, symbol):
    return next(x for x in book._positions(pid) if x["symbol"] == symbol)


def test_shares_fill_at_the_ask_and_a_stop_sells_at_the_bid(book):
    pid = manual(book)
    book.add_trade(pid, {"symbol": "AAPL", "instrument": "shares", "quantity": 10, "stop": 95, "target_1": 110})
    x = pos_of(book, pid, "AAPL")
    assert x["status"] == "OPEN" and x["entry_price"] == pytest.approx(100.01)
    fee = 100.01 * 10 * 0.0005
    assert book._portfolio(pid)["cash"] == pytest.approx(10_000 - 1000.1 - fee)
    book.prices.p["AAPL"] = 101.0
    m = book.mark(pid)
    assert m["equity"] == pytest.approx(10_000 - 1000.1 - fee + 10 * 100.99)
    book.prices.p["AAPL"] = 94.0
    book.mark(pid)
    x = pos_of(book, pid, "AAPL")
    assert x["status"] == "CLOSED" and x["exit_reason"] == "STOP" and x["exit_price"] == pytest.approx(93.99)
    assert x["realized_pnl"] == pytest.approx(10 * (93.99 - 100.01))
    assert any("stop" in e["text"] for e in book.detail(pid)["events"])


def test_a_target_sells_at_the_target_or_better_and_part_can_be_closed(book):
    pid = manual(book)
    book.add_trade(pid, {"symbol": "AAPL", "instrument": "shares", "quantity": 10, "stop": 95, "target_1": 110})
    x = pos_of(book, pid, "AAPL")
    book.close_position(x["id"], quantity=4)
    x = pos_of(book, pid, "AAPL")
    assert x["status"] == "OPEN" and x["open_quantity"] == 6 and x["realized_pnl"] == pytest.approx(4 * (99.99 - 100.01))
    with pytest.raises(P.PaperError, match="whole number"):
        book.close_position(x["id"], quantity=7)
    book.prices.p["AAPL"] = 112.0
    book.mark(pid)
    x = pos_of(book, pid, "AAPL")
    assert x["status"] == "CLOSED" and x["exit_reason"] == "TARGET"
    # The second sale (6 at 111.99, above the 110 target) and the first (4 at 99.99) average out.
    assert x["exit_price"] == pytest.approx((4 * 99.99 + 6 * 111.99) / 10)


def test_a_short_is_collateral_and_its_stop_buys_back_at_the_ask(book):
    pid = manual(book)
    book.add_trade(pid, {"symbol": "XOM", "instrument": "short", "quantity": 20, "stop": 52, "target_1": 46})
    x = pos_of(book, pid, "XOM")
    assert x["entry_price"] == pytest.approx(49.99) and x["direction"] == "SHORT"
    p = book._portfolio(pid)
    assert p["cash"] == pytest.approx(10_000 + 20 * 49.99 - 20 * 49.99 * 0.0005)
    # The proceeds sit in the cash, and the same again is held: a short ties up its whole value (no leverage).
    assert book._buying_power(p) == pytest.approx(p["cash"] - 2 * 20 * 49.99)
    assert book._buying_power(p) == pytest.approx(10_000 - 20 * 49.99 - 20 * 49.99 * 0.0005)
    book.prices.p["XOM"] = 52.5
    book.mark(pid)
    x = pos_of(book, pid, "XOM")
    assert x["exit_reason"] == "STOP" and x["exit_price"] == pytest.approx(52.51)
    assert x["realized_pnl"] == pytest.approx(20 * (49.99 - 52.51))


def test_an_option_limit_waits_for_the_ask_and_conservative_buys_at_once(book, svc):
    exp = sorted(svc.market.expirations("AAPL"))[3]
    pid = manual(book)
    book.add_trade(pid, {"symbol": "AAPL", "instrument": "call", "quantity": 2, "option": {"strike": 100, "expiration": exp, "iv": 0.3},
                         "limit_price": 2.0, "stop": 95, "target_1": 110})
    x = pos_of(book, pid, "AAPL")
    assert x["status"] == "PENDING" and x["multiplier"] == 100
    book.mark(pid)
    assert pos_of(book, pid, "AAPL")["status"] == "PENDING"            # the ask (2.10) is above the limit
    book.prices.option = {"bid": 1.85, "ask": 1.95}
    book.mark(pid)
    x = pos_of(book, pid, "AAPL")
    assert x["status"] == "OPEN" and x["entry_price"] == pytest.approx(1.95)
    assert book._portfolio(pid)["cash"] == pytest.approx(10_000 - 2 * 100 * 1.95)
    pid2 = manual(book, fill="CONSERVATIVE")
    book.prices.option = {"bid": 2.0, "ask": 2.2}
    book.add_trade(pid2, {"symbol": "AAPL", "instrument": "call", "quantity": 1, "option": {"strike": 100, "expiration": exp},
                          "limit_price": 2.0})
    assert pos_of(book, pid2, "AAPL")["entry_price"] == pytest.approx(2.2)


def test_a_portfolio_never_spends_money_it_does_not_have(book):
    pid = manual(book, budget=1_000)
    with pytest.raises(P.PaperError, match="free in this portfolio"):
        book.add_trade(pid, {"symbol": "AAPL", "instrument": "shares", "quantity": 20})
    with pytest.raises(P.PaperError, match="already through the stop"):
        book.add_trade(pid, {"symbol": "AAPL", "instrument": "shares", "quantity": 1, "stop": 101})
    with pytest.raises(P.PaperError, match="whole number"):
        book.add_trade(pid, {"symbol": "AAPL", "instrument": "shares", "quantity": 1.5})


def test_paper_never_reaches_the_real_positions_or_limits(book, svc):
    pid = manual(book)
    book.add_trade(pid, {"symbol": "AAPL", "instrument": "shares", "quantity": 10})
    assert svc.db.one("SELECT COUNT(*) AS n FROM positions")["n"] == 0
    assert svc.book_risk()["total"] == 0 and svc.journal.committed_risk() == 0


def test_the_horizon_and_the_end_close_everything(book):
    pid = manual(book)
    book.add_trade(pid, {"symbol": "AAPL", "instrument": "shares", "quantity": 5,
                         "horizon_end": (datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat()})
    book.mark(pid)
    assert pos_of(book, pid, "AAPL")["exit_reason"] == "HORIZON"
    book.add_trade(pid, {"symbol": "XOM", "instrument": "shares", "quantity": 5})
    out = book.close_portfolio(pid)
    assert out["portfolio"]["status"] == "CLOSED" and all(x["status"] == "CLOSED" for x in out["positions"])
    assert book.listing()["portfolios"][0]["status"] == "CLOSED"
    book.delete(pid)
    assert book.listing()["portfolios"] == []


def test_ends_are_worked_out_on_trading_days():
    start = datetime(2026, 11, 24, 15, 0, tzinfo=timezone.utc)        # a Tuesday morning in New York
    assert P.end_of("day", start, "day").date().isoformat() == "2026-11-24"
    assert P.end_of("week", start, "swing").date().isoformat() == "2026-12-01"   # Thanksgiving skipped
    assert P.end_of("open", start, "swing") is None
    with pytest.raises(P.PaperError):
        P.end_of("custom", start, "swing", "2026-11-01")


def test_catch_up_replays_stops_and_targets_from_bars(book, monkeypatch):
    pid = manual(book)
    book.add_trade(pid, {"symbol": "AAPL", "instrument": "shares", "quantity": 10, "stop": 95, "target_1": 110})
    book.add_trade(pid, {"symbol": "XOM", "instrument": "shares", "quantity": 10, "stop": 48, "target_1": 55})
    since = datetime.now(timezone.utc) - timedelta(hours=2)
    t0 = int(since.timestamp()) + 300

    def bars(symbol, a, b):
        rows = {"AAPL": [(100, 101, 99, 100), (99, 99.5, 94, 95.5), (95, 96, 94, 95)],     # the low touches the stop
                "XOM": [(50, 51, 49.5, 50.5), (56, 57, 55, 56), (56, 56, 55, 55)],          # opens beyond the target
                "SPY": [(500, 501, 499, 500)] * 3}[symbol]
        return pd.DataFrame([{"t0": t0 + 300 * (i - 1), "t": t0 + 300 * i, "open": o, "high": h, "low": lo, "close": c, "volume": 1}
                             for i, (o, h, lo, c) in enumerate(rows)]), "5m"
    monkeypatch.setattr(book, "_bars", bars)
    n = book.catch_up(pid, since, datetime.now(timezone.utc))
    assert n == 3
    a, x = pos_of(book, pid, "AAPL"), pos_of(book, pid, "XOM")
    assert a["exit_reason"] == "STOP" and a["exit_price"] == pytest.approx(95.0)
    assert x["exit_reason"] == "TARGET" and x["exit_price"] == pytest.approx(56.0)    # filled at the open beyond it
    assert any(e["kind"] == "CATCH_UP" for e in book.detail(pid)["events"])
    assert len(book.detail(pid)["marks"]) >= 3


class Hub:
    def __init__(self, opps):
        self.opps = opps

    def list(self, source):
        return self.opps, {}

    def fresh(self, source, now=None):
        return self.opps, []

    def ensure_options(self, opps, limit=12):
        return []


def _opp(svc, sym, score, edge=0.6):
    px = svc.market.spot(sym)
    atr = px * 0.02
    return make(source="morning", key=f"morning:t:{sym}", symbol=sym, direction="LONG", setup="ride", setup_label="Ride",
                horizon="day", horizon_days=1, entry=px, stop=px - 0.6 * atr, target_1=px + 0.9 * atr, target_2=px + 1.5 * atr,
                atr=atr, score=score, edge=edge_from(edge, 500, basis="t", cost_pct=cost_pct(svc.settings)))


def test_an_automated_portfolio_picks_splits_and_rotates(svc, monkeypatch):
    opps = [_opp(svc, s, 80 - i) for i, s in enumerate(["AAPL", "MSFT", "NVDA", "XOM"])]
    hub = Hub(opps)
    b = P.PaperBook(svc, hub, Blender(svc))
    monkeypatch.setattr(P, "LAST_ENTRY", (24, 0))                   # whatever the clock says, entries are allowed
    out = b.create({"mode": "AUTO", "budget": 20_000, "source": "morning", "horizon": "day", "goal": "dollars",
                    "fill_model": "MIDPOINT", "instruments": ["shares"], "max_positions": 2, "obey_limits": False})
    pid = out["portfolio"]["id"]
    held = {x["symbol"] for x in out["positions"] if x["status"] in ("OPEN", "PENDING")}
    assert held and len(held) <= 2
    # The held trades fall off the list; a fresh one scores well clear of them.
    hub.opps = [_opp(svc, "JPM", 90), _opp(svc, "CAT", 85)]
    b.step(pid, force=True)
    events = [e["text"] for e in b.detail(pid)["events"]]
    assert any("rotated out" in t for t in events)
    assert any(x["exit_reason"] == "ROTATE" for x in b._positions(pid))
    assert {x["symbol"] for x in b._positions(pid, ("OPEN", "PENDING"))} & {"JPM", "CAT"}


def test_the_background_pass_marks_and_steps(svc, monkeypatch):
    b = P.PaperBook(svc, Hub([_opp(svc, "AAPL", 70)]), Blender(svc))
    monkeypatch.setattr(P, "LAST_ENTRY", (24, 0))
    pid = b.create({"mode": "AUTO", "budget": 10_000, "source": "morning", "goal": "dollars", "fill_model": "MIDPOINT",
                    "instruments": ["shares"], "obey_limits": False})["portfolio"]["id"]
    with svc.db.tx() as cur:
        cur.execute("UPDATE paper_portfolios SET last_marked_at = NULL, last_step_at = NULL WHERE id = ?", (pid,))
    done = b.tick()
    assert pid in done["marked"] and pid in done["stepped"]


def test_a_stored_run_replays_over_a_past_session(svc):
    o = _opp(svc, "AAPL", 70)
    o2 = _opp(svc, "MSFT", 60)
    rid = Runs(svc.db).save("morning", [o, o2], {"label": "test run"})
    past = datetime(2026, 9, 15, 13, 0, tzinfo=timezone.utc)           # before the open, a month ago
    with svc.db.tx() as cur:
        cur.execute("UPDATE opportunity_runs SET created_at = ? WHERE id = ?", (past.isoformat(), rid))
    b = P.PaperBook(svc, None, Blender(svc))
    if datetime.now(timezone.utc) < past + timedelta(days=2):
        pytest.skip("the replay's day has not happened yet on this clock")
    r = b.replay({"run_id": rid, "budget": 10_000, "goal": "dollars", "instruments": ["shares"], "save": True})
    assert r["label"] == "test run" and r["curve"] and r["trades"]
    closed = [t for t in r["trades"] if "pnl" in t]
    assert closed and all(t["exit_reason"] in ("STOP", "TARGET", "HORIZON") for t in closed)
    assert r["final"] == pytest.approx(10_000 + sum(t["pnl"] for t in closed), abs=0.05)
    saved = b.detail(r["portfolio_id"])
    assert saved["portfolio"]["mode"] == "REPLAY" and saved["portfolio"]["status"] == "CLOSED" and saved["marks"]
    with pytest.raises(P.PaperError, match="has not happened yet"):
        future = Runs(svc.db).save("morning", [o], {"label": "now"})
        b.replay({"run_id": future, "budget": 10_000, "horizon_days": 20})


# --- from the independent review of 2.15 ----------------------------------------------------------------------

def test_shorts_and_waiting_orders_hold_buying_power(book):
    """A short ties up its whole value, and an order waiting for the open holds its cost: a $10,000 portfolio can
    neither short $50,000 nor queue $30,000 of orders."""
    pid = manual(book)
    book.add_trade(pid, {"symbol": "XOM", "instrument": "short", "quantity": 199})
    with pytest.raises(P.PaperError, match="free in this portfolio"):
        book.add_trade(pid, {"symbol": "XOM", "instrument": "short", "quantity": 199})
    pid2 = manual(book)
    book._session_open = lambda when: False                            # outside the session: orders wait
    x = book.add_trade(pid2, {"symbol": "AAPL", "instrument": "shares", "quantity": 99})
    assert x["status"] == "PENDING" and x["reserve"] == pytest.approx(99 * 100.01 * 1.0005)
    with pytest.raises(P.PaperError, match="free in this portfolio"):
        book.add_trade(pid2, {"symbol": "AAPL", "instrument": "shares", "quantity": 99})
    book._session_open = lambda when: True
    book.mark(pid2)
    p = book._portfolio(pid2)
    assert p["cash"] >= 0 and pos_of(book, pid2, "AAPL")["reserve"] == 0


def test_a_sale_is_applied_once_when_a_close_meets_a_valuation(book):
    pid = manual(book)
    book.add_trade(pid, {"symbol": "AAPL", "instrument": "shares", "quantity": 10, "stop": 95, "target_1": 110})
    stale = pos_of(book, pid, "AAPL")                                   # what a racing caller read before the stop
    cash = book._portfolio(pid)["cash"]
    book.prices.p["AAPL"] = 94.0
    book.mark(pid)                                                      # the stop sells all 10
    with pytest.raises(P.PaperError, match="no longer open"):
        book._exit(book._portfolio(pid), stale, book._stock_quotes(["AAPL"]), "MANUAL", "closed by you")
    assert book._portfolio(pid)["cash"] == pytest.approx(cash + 10 * 93.99 - 10 * 93.99 * 0.0005)
    assert sum(1 for e in book.detail(pid)["events"] if e["kind"] == "CLOSE") == 1


def test_levels_wait_for_the_session_and_a_horizon_after_it_takes_the_close(book, monkeypatch):
    pid = manual(book)
    book.add_trade(pid, {"symbol": "AAPL", "instrument": "shares", "quantity": 10, "stop": 95, "target_1": 110})
    book._session_open = lambda when: False
    book.prices.p["AAPL"] = 90.0                                        # an after-hours quote through the stop
    book.mark(pid)
    assert pos_of(book, pid, "AAPL")["status"] == "OPEN"
    with book.db.tx() as cur:
        cur.execute("UPDATE paper_positions SET horizon_end = ? WHERE portfolio_id = ?",
                    ((datetime.now(timezone.utc) - timedelta(hours=1)).isoformat(), pid))
    monkeypatch.setattr(book, "_close_on", lambda symbol, day: 97.5)
    book.mark(pid)
    x = pos_of(book, pid, "AAPL")
    assert x["exit_reason"] == "HORIZON" and x["exit_price"] == pytest.approx(97.5)


def test_an_expired_option_settles_on_its_expiration_days_close(book, svc, monkeypatch):
    pid = manual(book)
    exp = sorted(svc.market.expirations("AAPL"))[0]
    book.add_trade(pid, {"symbol": "AAPL", "instrument": "call", "quantity": 1, "option": {"strike": 95, "expiration": exp, "iv": 0.3},
                         "limit_price": 2.2})
    x = pos_of(book, pid, "AAPL")
    assert x["status"] == "OPEN"
    monkeypatch.setattr(book, "_close_on", lambda symbol, day: 101.0)
    after = datetime.combine(datetime.fromisoformat(exp).date(), datetime.min.time(), P.NY).replace(hour=17)
    book.mark(pid, after.astimezone(timezone.utc))
    x = pos_of(book, pid, "AAPL")
    assert x["exit_reason"] == "EXPIRED" and x["exit_price"] == pytest.approx(6.0)       # 101 - 95 at that close
    with pytest.raises(P.PaperError, match="must be a date"):
        book.add_trade(pid, {"symbol": "AAPL", "instrument": "call", "quantity": 1, "option": {"strike": 95, "expiration": "garbage"}})


def test_the_end_closes_at_the_last_valuation_when_no_price_can_be_had(book):
    pid = manual(book)
    book.add_trade(pid, {"symbol": "AAPL", "instrument": "shares", "quantity": 5})
    book._stock_quotes = lambda symbols: {}
    book._finish(book._portfolio(pid), datetime.now(timezone.utc))
    x = pos_of(book, pid, "AAPL")
    assert x["status"] == "CLOSED" and x["exit_price"] == pytest.approx(x["last_price"])
    assert book._portfolio(pid)["status"] == "CLOSED"


def test_a_custom_end_on_a_closed_day_is_the_next_sessions_close():
    friday_evening = datetime(2026, 10, 23, 21, 0, tzinfo=timezone.utc)
    assert P.end_of("custom", friday_evening, "swing", "2026-10-25").date().isoformat() == "2026-10-26"
    with pytest.raises(P.PaperError, match="must be a date"):
        P.end_of("custom", friday_evening, "swing", "soon")


def test_a_swing_order_leaves_at_the_cards_horizon(book):
    from tradeassist import marketdays
    pid = manual(book)
    x = book.add_trade(pid, {"symbol": "AAPL", "instrument": "shares", "quantity": 1, "horizon_days": 5})
    assert P.parse(x["horizon_end"]) == marketdays.horizon_close(P.parse(x["created_at"]), 5)


def test_a_bar_takes_the_stop_before_the_horizon_and_a_later_bar_leaves_at_its_open():
    end = datetime(2026, 10, 20, 20, 0, tzinfo=timezone.utc)
    pos = {"direction": "LONG", "stop": 95.0, "target_1": 110.0, "horizon_end": end.isoformat()}
    day_open, day_close = datetime(2026, 10, 20, 13, 30, tzinfo=timezone.utc), end
    assert P.PaperBook._bar_exit(pos, 100, 101, 94, 99, day_close, start=day_open)[0] == "STOP"
    assert P.PaperBook._bar_exit(pos, 100, 101, 96, 99, day_close, start=day_open)[:3:2] == ("HORIZON", 99)
    later = P.PaperBook._bar_exit(pos, 98, 99, 90, 91, end + timedelta(days=1), start=end + timedelta(hours=17))
    assert later[0] == "HORIZON" and later[2] == 98


def test_a_replay_enters_at_the_first_bar_after_the_run(svc, monkeypatch):
    """No look-ahead: a run made at 14:00 enters at the 14:00 bar's open, not the 13:55 bar's; on daily bars a run made
    during a session enters at the next session's open, and a day trade there cannot be replayed."""
    o = _opp(svc, "AAPL", 70)
    o.update(horizon="swing", horizon_days=2)
    rid = Runs(svc.db).save("morning", [o], {"label": "in the session"})
    started = datetime(2026, 9, 15, 18, 0, tzinfo=timezone.utc)                  # 14:00 in New York
    with svc.db.tx() as cur:
        cur.execute("UPDATE opportunity_runs SET created_at = ? WHERE id = ?", (started.isoformat(), rid))
    b = P.PaperBook(svc, None, Blender(svc))
    s0 = int(started.timestamp())

    def five(symbol, since, until):
        rows = [{"t0": s0 + 300 * k, "t": s0 + 300 * (k + 1), "open": 100 + k, "high": 101 + k, "low": 99.5 + k,
                 "close": 100.5 + k, "volume": 1} for k in range(-1, 6)]
        df = pd.DataFrame(rows)
        return df[(df["t"] > since.timestamp()) & (df["t0"] < until.timestamp())].reset_index(drop=True), "5m"
    monkeypatch.setattr(b, "_bars", five)
    r = b.replay({"run_id": rid, "budget": 10_000, "goal": "dollars", "instruments": ["shares"], "obey_limits": False})
    t = next(x for x in r["trades"] if "pnl" in x)
    assert t["entry_underlying"] == 100 and P.parse(t["entry_at"]) == started     # the bar that starts at 14:00

    def daily(symbol, since, until):
        days = [datetime(2026, 9, 15, 13, 30, tzinfo=timezone.utc), datetime(2026, 9, 16, 13, 30, tzinfo=timezone.utc),
                datetime(2026, 9, 17, 13, 30, tzinfo=timezone.utc), datetime(2026, 9, 18, 13, 30, tzinfo=timezone.utc)]
        df = pd.DataFrame([{"t0": int(d.timestamp()), "t": int(d.timestamp()) + 23_400, "open": 200 + i, "high": 201 + i,
                            "low": 199.5 + i, "close": 200.5 + i, "volume": 1} for i, d in enumerate(days)])
        return df[(df["t"] > since.timestamp()) & (df["t0"] < until.timestamp())].reset_index(drop=True), "1d"
    monkeypatch.setattr(b, "_bars", daily)
    r = b.replay({"run_id": rid, "budget": 10_000, "goal": "dollars", "instruments": ["shares"], "obey_limits": False})
    t = next(x for x in r["trades"] if "pnl" in x)
    assert t["entry_underlying"] == 201                                          # Sept 16's open, not Sept 15's
    o.update(horizon="day", horizon_days=1)
    rid2 = Runs(svc.db).save("morning", [o], {"label": "a day trade"})
    with svc.db.tx() as cur:
        cur.execute("UPDATE opportunity_runs SET created_at = ? WHERE id = ?", (started.isoformat(), rid2))
    with pytest.raises(P.PaperError, match="5-minute bars"):
        b.replay({"run_id": rid2, "budget": 10_000, "goal": "dollars", "instruments": ["shares"], "obey_limits": False})


def test_an_automated_portfolio_trades_only_fresh_lists():
    from tradeassist.hub import Hub as RealHub
    h = RealHub(svc=None, morning=None, ideas=None, digest=None)
    now = datetime(2026, 10, 19, 10, 0, tzinfo=P.NY)                            # a Monday morning
    assert "Fri Oct 16" in h.stale("morning", {"built_at": "2026-10-16T09:20:00-04:00"}, now)
    assert h.stale("morning", {"built_at": "2026-10-19T09:20:00-04:00"}, now) is None
    assert h.stale("live", {"built_at": "2026-10-19T07:00:00-04:00"}, now) is not None
    assert h.stale("live", {"built_at": "2026-10-19T09:30:00-04:00"}, now) is None
    assert h.stale("ideas", {"built_at": "2026-10-16T18:00:00-04:00"}, now) is None             # the last trading day
    assert h.stale("ideas", {"built_at": "2026-10-15T18:00:00-04:00"}, now) is not None
    assert "has not been built" in h.stale("morning", {}, now)
