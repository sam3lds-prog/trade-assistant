"""Paper trading (2.15): trade the app's recommendations, or your own, with pretend money, and see how they do.

**Isolation.** Paper portfolios, their positions, fills and values live in tables of their own (migration 17).
Nothing here touches the Positions screen, the journal, portfolio risk or the daily loss stop, and nothing places
an order anywhere.

**A portfolio** has a budget (its starting cash), a goal for the blend optimizer, a horizon (day trades, out by
15:55; or swing trades held to their horizon dates), an end (a day, a week, a month, a date, or none), and a mode:

- **Automated**: it takes the best `top_n` opportunities from the Morning plan, the news digest, Trade ideas or the
  best of all three, splits the cash with the blend optimizer (blend.py) and places the trades on paper. Every
  `refresh_minutes` it looks again: a position whose opportunity has dropped out of the list (or fallen below
  FADED_SCORE) is sold when a fresher one scores at least ROTATE_HURDLE points higher, and idle cash is put to work
  on new ones. Day-trade portfolios open nothing after 15:00. Every action is logged with its reason.
- **Manual**: you place the trades, from any opportunity or blend or by hand (shares, a short, a call or a put), and
  close them, all or part, whenever you like. The app still applies each trade's stop, target and horizon.

**Fills**, by the portfolio's fill model:

    REALISTIC     shares buy at the ask and sell at the bid, plus settings.stock_cost_bps each way; an option is a
                  limit order at the card's suggested price that fills only when the ask comes down to it (orders
                  not filled by the close are cancelled), and sells at the bid. Orders placed outside regular hours
                  wait for the open.
    CONSERVATIVE  as REALISTIC, except an option buys at the ask at once.
    MIDPOINT      everything at the bid/ask midpoint (optimistic: real fills are usually worse).

A stop sells at the bid once the stock trades through it (a gap costs more); a target sells at the target or
better. Options leave when their stock reaches the plan's stop or target, at the option's bid.

**Marking.** While the app runs, open portfolios are valued every minute in the session (every five before and
after it) at what closing would get (bid for longs, ask for shorts; midpoints with the MIDPOINT model), with SPY
alongside as the benchmark. When the app was closed or the computer asleep, the next pass replays the gap from
5-minute bars (daily bars beyond their reach) to find stops and targets hit meanwhile; options in that stretch are
valued with Black-76 at their own volatility, and those values are marked as modelled.

**Replay** (`replay`) runs a stored recommendation run (opportunities.Runs, kept from 2.15 on, or the Morning plan's
picks before that) over a past horizon on price history: stocks from 5-minute or daily bars, options modelled. It
answers "what would this list have done" without waiting.

Paper results flatter real trading: real fills are worse in fast markets, trading halts are not modelled, borrow
fees on shorts are not charged, and option prices between quotes are a model's. They are a test of the
recommendations, not a promise.
"""

from __future__ import annotations

import json
import logging
import math
import threading
from datetime import date, datetime, timedelta, timezone
from datetime import time as dtime
from typing import Any

import numpy as np
import pandas as pd

from . import marketdays
from .opportunities import DAY_EXIT, GOALS, INSTRUMENTS, make
from .optionmath import NY
from .providers.base import DataError
from .simulate import MULTIPLIER, Leg, leg_value

log = logging.getLogger(__name__)

FILL_MODELS = ("REALISTIC", "CONSERVATIVE", "MIDPOINT")
MODES = ("AUTO", "MANUAL")
SOURCES = ("morning", "live", "ideas", "all", "manual")
MARK_SECONDS_OPEN = 60
MARK_SECONDS_EXTENDED = 300
ROTATE_HURDLE = 15.0
FADED_SCORE = 35.0
IDLE_SHARE = 0.05              # automated portfolios invest idle cash once it is this share of the budget
LAST_ENTRY = (15, 0)           # day-trade portfolios open nothing after 15:00
CATCH_UP_POINTS = 400
EVENTS_SHOWN = 300
MARKS_SHOWN = 1500
ENDS = {"day": 1, "week": 5, "month": 21}
DAY_CLOSE = dtime(16, 0)          # an expiration day the calendar does not know: options stop at 16:00


class PaperError(ValueError):
    pass


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def iso(t: datetime) -> str:
    return t.astimezone(timezone.utc).isoformat(timespec="seconds")


def parse(ts: str | None) -> datetime | None:
    if not ts:
        return None
    t = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
    return t if t.tzinfo else t.replace(tzinfo=timezone.utc)


def _f(v: Any) -> float | None:
    try:
        x = float(v)
    except (TypeError, ValueError):
        return None
    return x if math.isfinite(x) else None


def end_of(choice: str | None, start: datetime, horizon: str, custom: str | None = None) -> datetime | None:
    """When a portfolio ends: after the session (day), 5 trading days (week), 21 (month), on a date, or never."""
    start_ny = start.astimezone(NY)
    if choice in (None, "", "open"):
        return None
    if choice == "custom":
        if not custom:
            raise PaperError("Give the end date")
        try:
            d = date.fromisoformat(custom[:10])
        except ValueError as exc:
            raise PaperError("The end date must be a date (YYYY-MM-DD)") from exc
        if d <= start_ny.date():
            raise PaperError("The end date must be after today")
        if not marketdays.is_trading_day(d):
            d = marketdays.next_trading_day(d)          # a weekend or holiday: the next session's close, never an earlier one
        return marketdays.session(d)[1]
    if choice not in ENDS:
        raise PaperError("The end must be day, week, month, custom or open")
    day = start_ny.date()
    s = marketdays.session(day)
    if s is None or start_ny >= s[1]:
        day = marketdays.next_trading_day(day)
    last = marketdays.add_trading_days(day, ENDS[choice] - 1) if ENDS[choice] > 1 else day
    return marketdays.session(last)[1]


def market_phase(svc: Any, now: datetime | None = None) -> str:
    """open, pre, post or closed now, by the data source's clock (demo data keeps its own hours), falling back to
    the exchange calendar when the clock cannot be read."""
    state = (svc.market.clock() or {}).get("state")
    if state == "open":
        return "open"
    if state in ("premarket", "pre"):
        return "pre"
    if state in ("postmarket", "post"):
        return "post"
    if state == "closed":
        return "closed"
    return marketdays.phase(now)


def day_exit(day: date) -> datetime:
    s = marketdays.session(day)
    if s is None:
        return datetime.combine(day, DAY_EXIT, NY)
    return min(datetime.combine(day, DAY_EXIT, NY), s[1] - timedelta(minutes=5))


class PaperBook:
    def __init__(self, svc: Any, hub: Any = None, blender: Any = None):
        self.svc = svc
        self.hub = hub
        self.blender = blender
        self.db = svc.db
        self._lock = threading.RLock()

    # --- reading --------------------------------------------------------------------------------------------
    def _portfolio(self, pid: int) -> dict[str, Any]:
        r = self.db.one("SELECT * FROM paper_portfolios WHERE id = ?", (pid,))
        if r is None:
            raise PaperError(f"No paper portfolio {pid}")
        p = dict(r)
        p["instruments"] = json.loads(p.get("instruments") or "[]")
        return p

    def _positions(self, pid: int, statuses: tuple[str, ...] | None = None) -> list[dict[str, Any]]:
        if statuses:
            rows = self.db.query(f"SELECT * FROM paper_positions WHERE portfolio_id = ? AND status IN "
                                 f"({','.join('?' * len(statuses))}) ORDER BY id", (pid, *statuses))
        else:
            rows = self.db.query("SELECT * FROM paper_positions WHERE portfolio_id = ? ORDER BY id", (pid,))
        return [dict(r) for r in rows]

    def _event(self, cur: Any, pid: int, kind: str, text: str, *, position_id: int | None = None, price: float | None = None,
               quantity: float | None = None, at: datetime | None = None, extra: dict[str, Any] | None = None) -> None:
        cur.execute("INSERT INTO paper_events (portfolio_id, position_id, at, kind, text, price, quantity, extra_json) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (pid, position_id, iso(at or utc_now()), kind, text[:500], price, quantity,
                     json.dumps(extra) if extra else None))

    # --- portfolios -----------------------------------------------------------------------------------------
    def create(self, req: dict[str, Any]) -> dict[str, Any]:
        mode = str(req.get("mode") or "MANUAL").upper()
        if mode not in MODES:
            raise PaperError("mode must be AUTO or MANUAL")
        budget = _f(req.get("budget"))
        if not budget or not 100 <= budget <= 10_000_000:
            raise PaperError("The budget must be between $100 and $10,000,000")
        source = str(req.get("source") or ("all" if mode == "AUTO" else "manual"))
        if source not in SOURCES or (mode == "AUTO" and source == "manual"):
            raise PaperError("An automated portfolio takes its trades from morning, live, ideas or all")
        goal = str(req.get("goal") or "balanced")
        if goal not in GOALS:
            raise PaperError(f"goal must be one of {', '.join(GOALS)}")
        horizon = str(req.get("horizon") or "day")
        if horizon not in ("day", "swing"):
            raise PaperError("horizon must be day or swing")
        fill = str(req.get("fill_model") or "REALISTIC").upper()
        if fill not in FILL_MODELS:
            raise PaperError(f"fill_model must be one of {', '.join(FILL_MODELS)}")
        inst = [i for i in dict.fromkeys(req.get("instruments") or list(INSTRUMENTS)) if i in INSTRUMENTS]
        if not inst:
            raise PaperError("Allow at least one instrument")
        refresh = int(req.get("refresh_minutes") or (15 if horizon == "day" else 60))
        if refresh not in (5, 10, 15, 30, 60, 120, 240):
            raise PaperError("The refresh must be 5, 10, 15, 30, 60, 120 or 240 minutes")
        now = utc_now()
        ends = end_of(req.get("ends") or ("day" if horizon == "day" else "month"), now, horizon, req.get("ends_on"))
        name = str(req.get("name") or "").strip()[:80] or (
            f"{'Automated' if mode == 'AUTO' else 'Manual'} {horizon} trades from {now.astimezone(NY):%b %d %H:%M}")
        with self._lock, self.db.tx() as cur:
            cur.execute("""INSERT INTO paper_portfolios (name, mode, status, created_at, starts_at, ends_at, budget, cash, source,
                           goal, tail_pct, top_n, horizon, fill_model, obey_limits, rotate, refresh_minutes, instruments,
                           max_share, max_positions, sector_cap, data_source, notes)
                           VALUES (?, ?, 'ACTIVE', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                        (name, mode, iso(now), iso(now), iso(ends) if ends else None, budget, budget, source, goal,
                         float(req.get("tail_pct") or 2.0), int(req.get("top_n") or 10), horizon, fill,
                         int(bool(req.get("obey_limits", True))), int(bool(req.get("rotate", True))), refresh,
                         json.dumps(inst), float(req.get("max_share") or 0.2), int(req.get("max_positions") or 10),
                         float(req.get("sector_cap") or 0.4), self.svc.market.info.mode, str(req.get("notes") or "")[:500]))
            pid = int(cur.lastrowid)
            self._event(cur, pid, "NOTE", f"Opened with ${budget:,.0f} of paper money ({fill.lower()} fills"
                        + (", automated" if mode == "AUTO" else "") + ").")
        trades = req.get("trades") or []
        errors = []
        for t in trades:
            try:
                self.add_trade(pid, t)
            except (PaperError, DataError, ValueError) as exc:
                errors.append(f"{t.get('symbol')}: {exc}")
        if mode == "AUTO":
            try:
                self.step(pid, force=True)
            except (PaperError, DataError, ValueError) as exc:
                errors.append(f"First pick: {exc}")
        self.mark(pid)
        out = self.detail(pid)
        out["errors"] = errors
        return out

    def listing(self) -> dict[str, Any]:
        rows = [dict(r) for r in self.db.query("SELECT * FROM paper_portfolios ORDER BY status, id DESC")]
        out = []
        for p in rows:
            last = self.db.one("SELECT * FROM paper_marks WHERE portfolio_id = ? ORDER BY at DESC LIMIT 1", (p["id"],))
            n_open = self.db.one("SELECT COUNT(*) AS n FROM paper_positions WHERE portfolio_id = ? AND status IN ('OPEN', 'PENDING')",
                                 (p["id"],))["n"]
            equity = float(last["equity"]) if last else float(p["budget"])
            out.append({"id": p["id"], "name": p["name"], "mode": p["mode"], "status": p["status"], "source": p["source"],
                        "horizon": p["horizon"], "budget": p["budget"], "equity": equity,
                        "return_pct": 100 * (equity / p["budget"] - 1), "open_positions": n_open, "created_at": p["created_at"],
                        "ends_at": p["ends_at"], "closed_at": p["closed_at"], "last_marked_at": p["last_marked_at"],
                        "fill_model": p["fill_model"], "goal": p["goal"]})
        return {"portfolios": out, "data_source": self.svc.market.info.mode}

    def detail(self, pid: int) -> dict[str, Any]:
        p = self._portfolio(pid)
        positions = self._positions(pid)
        events = [dict(r) for r in self.db.query("SELECT * FROM paper_events WHERE portfolio_id = ? ORDER BY id DESC LIMIT ?",
                                                 (pid, EVENTS_SHOWN))]
        marks = [dict(r) for r in self.db.query("SELECT * FROM paper_marks WHERE portfolio_id = ? ORDER BY at", (pid,))]
        if len(marks) > MARKS_SHOWN:
            step = math.ceil(len(marks) / MARKS_SHOWN)
            marks = marks[::step] + ([marks[-1]] if (len(marks) - 1) % step else [])
        for pos in positions:
            pos["market_value"], pos["unrealized_pnl"] = self._value(pos)
            pos["total_pnl"] = (pos["realized_pnl"] or 0.0) + (pos["unrealized_pnl"] or 0.0) - (pos["fees"] or 0.0)
        return {"portfolio": p, "positions": positions, "events": events, "marks": marks, "stats": self.stats(p, positions, marks),
                "data_source": self.svc.market.info.mode,
                "note": "Paper money only: nothing here places an order, and these figures never reach your real positions "
                        "or risk limits."}

    def update(self, pid: int, patch: dict[str, Any]) -> dict[str, Any]:
        p = self._portfolio(pid)
        if p["status"] != "ACTIVE":
            raise PaperError("This portfolio is closed")
        sets, vals = [], []
        if "rotate" in patch:
            sets.append("rotate = ?")
            vals.append(int(bool(patch["rotate"])))
        if "refresh_minutes" in patch:
            r = int(patch["refresh_minutes"])
            if r not in (5, 10, 15, 30, 60, 120, 240):
                raise PaperError("The refresh must be 5, 10, 15, 30, 60, 120 or 240 minutes")
            sets.append("refresh_minutes = ?")
            vals.append(r)
        if "name" in patch:
            sets.append("name = ?")
            vals.append(str(patch["name"]).strip()[:80] or p["name"])
        if not sets:
            return self.detail(pid)
        with self.db.tx() as cur:
            cur.execute(f"UPDATE paper_portfolios SET {', '.join(sets)} WHERE id = ?", (*vals, pid))
            self._event(cur, pid, "NOTE", "Settings changed: " + ", ".join(f"{k} {v}" for k, v in patch.items()))
        return self.detail(pid)

    def close_portfolio(self, pid: int, reason: str = "closed by you") -> dict[str, Any]:
        with self._lock:
            p = self._portfolio(pid)
            if p["status"] == "CLOSED":
                return self.detail(pid)
            quotes = self._stock_quotes([x["symbol"] for x in self._positions(pid, ("OPEN", "PENDING"))])
            for pos in self._positions(pid, ("OPEN", "PENDING")):
                if pos["status"] == "PENDING":
                    self._cancel(pos, "the portfolio closed")
                else:
                    self._exit_or_last(p, pos, quotes, "CLOSE", f"the portfolio closed ({reason})")
            self.mark(pid)
            with self.db.tx() as cur:
                cur.execute("UPDATE paper_portfolios SET status = 'CLOSED', closed_at = ? WHERE id = ?", (iso(utc_now()), pid))
                self._event(cur, pid, "NOTE", f"Closed: {reason}.")
            return self.detail(pid)

    def delete(self, pid: int) -> dict[str, Any]:
        with self._lock:
            self._portfolio(pid)
            with self.db.tx() as cur:
                cur.execute("DELETE FROM paper_portfolios WHERE id = ?", (pid,))
        return self.listing()

    # --- prices -----------------------------------------------------------------------------------------------
    def _stock_quotes(self, symbols: list[str]) -> dict[str, dict[str, Any]]:
        out: dict[str, dict[str, Any]] = {}
        names = list(dict.fromkeys(s for s in symbols if s))
        market = self.svc.market
        for i in range(0, len(names), 50):
            chunk = names[i:i + 50]
            try:
                out.update(market._call("paper quotes", lambda c=chunk: market.provider.quotes(c)))
            except DataError as exc:
                log.info("Paper quotes failed: %s", exc)
        return out

    def _option_quote(self, pos: dict[str, Any], underlying: float | None) -> dict[str, Any]:
        """The contract's bid, ask and midpoint from its chain, or its model value at the stock price now."""
        if pos["expiration"] < datetime.now(NY).date().isoformat():
            intrinsic = max((underlying - pos["strike"]) if pos["instrument"] == "CALL" else (pos["strike"] - underlying), 0.0) \
                if underlying else None
            return {"bid": intrinsic, "ask": intrinsic, "mid": intrinsic, "expired": True, "basis": "intrinsic value at expiration"}
        try:
            a = self.svc._chain_for(pos["symbol"], pos["expiration"])
            row = a.find_by_terms(pos["instrument"].lower(), float(pos["strike"]))
        except (DataError, ValueError) as exc:
            row, a = None, None
            log.info("Paper: no chain for %s: %s", pos["symbol"], exc)
        if row is not None and row.get("bid") is not None and row.get("ask") is not None and row["ask"] >= row["bid"] >= 0:
            return {"bid": float(row["bid"]), "ask": float(row["ask"]), "mid": float(row.get("mid") or (row["bid"] + row["ask"]) / 2),
                    "basis": "quote", "underlying": a.spot}
        value = self._model_value(pos, underlying, datetime.now(NY))
        return {"bid": value, "ask": value, "mid": value, "basis": "model (no two-sided quote)", "modelled": True}

    def _model_value(self, pos: dict[str, Any], underlying: float | None, when: datetime) -> float | None:
        if not underlying or not pos.get("iv"):
            return None
        leg = Leg(pos["instrument"].lower(), float(pos["strike"]), pos["expiration"], 1, 1, 0.0, float(pos["iv"]),
                  float(self.svc.settings.risk_free_rate))
        return float(leg_value(leg, np.array([float(underlying)]), when, float(self.svc.settings.risk_free_rate))[0])

    @staticmethod
    def _sides(q: dict[str, Any]) -> tuple[float | None, float | None, float | None, float | None]:
        bid, ask, last = _f(q.get("bid")), _f(q.get("ask")), _f(q.get("last"))
        if bid and ask and ask >= bid > 0:
            mid = (bid + ask) / 2
        else:
            mid = last
            bid = bid if bid and bid > 0 else last
            ask = ask if ask and ask > 0 else last
        return bid, ask, mid, last

    def _value(self, pos: dict[str, Any]) -> tuple[float | None, float | None]:
        """Market value (negative for a short) and unrealized profit at the position's last mark."""
        if pos["status"] != "OPEN" or pos["last_price"] is None:
            return (0.0 if pos["status"] in ("CLOSED", "CANCELLED") else None), (0.0 if pos["status"] != "OPEN" else None)
        q, mult = pos["open_quantity"], pos["multiplier"]
        if pos["instrument"] == "SHORT":
            return -q * pos["last_price"], q * (pos["entry_price"] - pos["last_price"])
        return q * mult * pos["last_price"], q * mult * (pos["last_price"] - pos["entry_price"])

    # --- placing and closing --------------------------------------------------------------------------------
    def add_trade(self, pid: int, t: dict[str, Any], *, at: datetime | None = None, quote: dict[str, Any] | None = None) -> dict[str, Any]:
        """Place one paper order. `t`: symbol, instrument (shares, short, call, put), quantity, and optionally the
        contract (option_type, strike, expiration, iv), limit_price, stop, target_1, target_2, horizon_end, opp_key,
        source, setup_label and why. Under the book's lock, so the buying power checked is the buying power used."""
        with self._lock:
            return self._add_trade(pid, t, at=at, quote=quote)

    def _add_trade(self, pid: int, t: dict[str, Any], *, at: datetime | None, quote: dict[str, Any] | None) -> dict[str, Any]:
        p = self._portfolio(pid)
        if p["status"] != "ACTIVE":
            raise PaperError("This portfolio is closed")
        sym = self.svc._symbol(str(t.get("symbol") or ""))
        inst = str(t.get("instrument") or "shares").upper()
        if inst not in ("SHARES", "SHORT", "CALL", "PUT"):
            raise PaperError("instrument must be shares, short, call or put")
        qty = _f(t.get("quantity"))
        if not qty or qty <= 0 or qty != int(qty):
            raise PaperError("The quantity must be a whole number above zero")
        direction = "SHORT" if inst in ("SHORT", "PUT") else "LONG"
        stop, t1, t2 = _f(t.get("stop")), _f(t.get("target_1")), _f(t.get("target_2"))
        d = 1 if direction == "LONG" else -1
        now = at or utc_now()
        mult = MULTIPLIER if inst in ("CALL", "PUT") else 1
        strike = expiration = iv = None
        if mult > 1:
            opt = t.get("option") or t
            strike, expiration, iv = _f(opt.get("strike")), opt.get("expiration"), _f(opt.get("iv"))
            if not strike or strike <= 0 or not expiration:
                raise PaperError("A call or put needs its strike and expiration")
            try:
                expiration = date.fromisoformat(str(expiration)[:10]).isoformat()
            except ValueError as exc:
                raise PaperError("The expiration must be a date (YYYY-MM-DD)") from exc
            if expiration < now.astimezone(NY).date().isoformat():
                raise PaperError("That contract has expired")
        q = quote if quote is not None else self._stock_quotes([sym]).get(sym)
        if not q:
            raise PaperError(f"No quote for {sym}")
        bid, ask, mid, last = self._sides(q)
        under = mid or last
        if not under:
            raise PaperError(f"No usable price for {sym}")
        if stop is not None and d * (under - stop) <= 0:
            raise PaperError(f"{sym} is already through the stop (${stop:,.2f}; now ${under:,.2f})")
        if t1 is not None and d * (t1 - under) <= 0:
            raise PaperError(f"{sym} is already beyond target 1 (${t1:,.2f}; now ${under:,.2f})")
        horizon_end = t.get("horizon_end")
        if not horizon_end:
            if p["horizon"] == "day":
                horizon_end = iso(day_exit(self._trade_day(now)))
            elif t.get("horizon_days"):
                # The trade card's own horizon date: the close N trading days after today.
                horizon_end = iso(marketdays.horizon_close(now, int(t["horizon_days"])))
            else:
                horizon_end = p["ends_at"]
        limit = _f(t.get("limit_price"))
        est = None
        if mult > 1:
            oq = self._option_quote({"symbol": sym, "instrument": inst, "strike": strike, "expiration": expiration, "iv": iv},
                                    under)
            est = oq.get("ask") if p["fill_model"] != "MIDPOINT" else oq.get("mid")
            if limit is None:
                limit = est
            if not limit or limit <= 0:
                raise PaperError("No price for that contract; give a limit price")
            need = qty * mult * limit
            risk = need
        else:
            px = ask if inst == "SHARES" else bid
            # A long holds its cost; a short its value once more (its proceeds arrive in the cash at the fill, and
            # buying power counts an open short twice).
            need = qty * (px or under) * (1 + float(self.svc.settings.stock_cost_bps) / 10_000.0)
            risk = qty * abs(under - stop) if stop else None
        free = self._buying_power(p)
        if need > free + 1e-6:
            raise PaperError(f"That costs about ${need:,.0f}; ${free:,.0f} is free in this portfolio")
        with self.db.tx() as cur:
            cur.execute("""INSERT INTO paper_positions (portfolio_id, opp_key, source, setup_label, symbol, instrument, direction,
                           option_symbol, strike, expiration, iv, multiplier, quantity, open_quantity, status, limit_price,
                           reserve, stop, target_1, target_2, horizon_end, initial_risk, why, created_at, updated_at)
                           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, 'PENDING', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                        (pid, t.get("opp_key"), t.get("source") or "manual", t.get("setup_label"), sym, inst, direction,
                         t.get("option_symbol"), strike, expiration, iv, mult, qty, limit, need, stop, t1, t2, horizon_end, risk,
                         str(t.get("why") or "")[:300], iso(now), iso(now)))
            pos_id = int(cur.lastrowid)
            what = (f"{qty:g} {sym} {expiration} ${strike:g} {inst.lower()} at a ${limit:.2f} limit" if mult > 1 else
                    f"{'buy' if inst == 'SHARES' else 'short'} {qty:g} {sym}")
            self._event(cur, pid, "ORDER", f"Order: {what}" + (f" ({t['why']})" if t.get("why") else ""),
                        position_id=pos_id, price=limit, quantity=qty, at=now)
        pos = dict(self.db.one("SELECT * FROM paper_positions WHERE id = ?", (pos_id,)))
        self._try_fill(p, pos, q, now)
        return dict(self.db.one("SELECT * FROM paper_positions WHERE id = ?", (pos_id,)))

    @staticmethod
    def _trade_day(now: datetime) -> date:
        ny = now.astimezone(NY)
        s = marketdays.session(ny.date())
        if s and ny < s[1]:
            return ny.date()
        return marketdays.next_trading_day(ny.date())

    def _buying_power(self, p: dict[str, Any]) -> float:
        """Cash less twice each open short's value now, less what pending orders hold. A short's proceeds sit in the
        cash and the same again is held from the portfolio's own money: paper portfolios are never leveraged, so
        what is long plus what is short never exceeds what the portfolio is worth. A pending order holds its
        estimated cost (`reserve`) until it fills or is cancelled."""
        cash = float(self.db.one("SELECT cash FROM paper_portfolios WHERE id = ?", (p["id"],))["cash"])
        short = sum(x["open_quantity"] * (x["last_price"] or x["entry_price"] or 0.0) for x in self._positions(p["id"], ("OPEN",))
                    if x["instrument"] == "SHORT")
        pending = sum(float(x["reserve"] or 0.0) for x in self._positions(p["id"], ("PENDING",)))
        return cash - 2.0 * short - pending

    def _session_open(self, when: datetime) -> bool:
        """Whether the regular session trades at `when`: the data source's own clock for now (so demo data, whose
        market keeps its own hours, behaves like the rest of the app), the exchange calendar for any other moment."""
        if abs((utc_now() - when).total_seconds()) < 120:
            state = (self.svc.market.clock() or {}).get("state")
            if state and state != "unknown":
                return state == "open"
        s = marketdays.session(when.astimezone(NY).date())
        return bool(s and s[0] <= when.astimezone(NY) < s[1])

    def phase(self, now: datetime | None = None) -> str:
        """open, pre, post or closed: the data source's clock when it answers, else the exchange calendar."""
        return market_phase(self.svc, now)

    def _try_fill(self, p: dict[str, Any], pos: dict[str, Any], q: dict[str, Any], now: datetime,
                  bar: dict[str, float] | None = None) -> bool:
        """Fill a pending order if it can be filled now. Shares and shorts fill only while the regular session
        trades; an option's limit order fills when the ask reaches the limit (at once for CONSERVATIVE and
        MIDPOINT). With `bar` (catch-up and replay), the order fills at the bar's open."""
        if pos["status"] != "PENDING":
            return False
        if not self._session_open(now):
            return False
        cost_rate = float(self.svc.settings.stock_cost_bps) / 10_000.0
        model = p["fill_model"]
        fees = 0.0
        if pos["multiplier"] == 1:
            if bar is not None:
                px = bar["open"]
            else:
                bid, ask, mid, _ = self._sides(q)
                px = mid if model == "MIDPOINT" else (ask if pos["instrument"] == "SHARES" else bid)
            if not px:
                return False
            fees = 0.0 if model == "MIDPOINT" else px * pos["quantity"] * cost_rate
            underlying = px
        else:
            underlying = bar["open"] if bar is not None else self._sides(q)[2]
            if bar is not None:
                value = self._model_value(pos, underlying, now)
                if value is None:
                    return False
                oq = {"bid": value, "ask": value * 1.02, "mid": value * 1.01, "basis": "model"}
            else:
                oq = self._option_quote(pos, underlying)
            if oq.get("ask") is None:
                return False
            if model == "REALISTIC":
                if oq.get("basis") != "quote" and bar is None:
                    return False                       # a limit order rests until there is a real quote
                if oq["ask"] > pos["limit_price"] + 1e-9:
                    return False
                px = min(pos["limit_price"], oq["ask"])
            elif model == "CONSERVATIVE":
                px = oq["ask"]
            else:
                px = oq["mid"]
            if not px or px <= 0:
                return False
        sign = -1 if pos["instrument"] == "SHORT" else 1
        cash_change = -sign * pos["quantity"] * pos["multiplier"] * px - fees
        risk = pos["initial_risk"]
        if pos["multiplier"] == 1 and pos["stop"]:
            risk = pos["quantity"] * abs(px - pos["stop"])
        elif pos["multiplier"] > 1:
            risk = pos["quantity"] * pos["multiplier"] * px
        with self.db.tx() as cur:
            cur.execute("""UPDATE paper_positions SET status = 'OPEN', open_quantity = quantity, entry_price = ?, entry_at = ?,
                           entry_underlying = ?, last_price = ?, last_underlying = ?, last_at = ?, fees = fees + ?, initial_risk = ?,
                           mark_basis = ?, reserve = 0, updated_at = ? WHERE id = ? AND status = 'PENDING'""",
                        (px, iso(now), underlying, px, underlying, iso(now), fees, risk,
                         "bar" if bar is not None else "quote", iso(now), pos["id"]))
            if cur.rowcount:
                cur.execute("UPDATE paper_portfolios SET cash = cash + ? WHERE id = ?", (cash_change, p["id"]))
                self._event(cur, p["id"], "FILL", f"Filled: {pos['quantity']:g} {pos['symbol']}"
                            + (f" {pos['instrument'].lower()}s" if pos["multiplier"] > 1 else "") + f" at ${px:,.2f}",
                            position_id=pos["id"], price=px, quantity=pos["quantity"], at=now)
        return True

    def _cancel(self, pos: dict[str, Any], why: str, at: datetime | None = None) -> None:
        with self.db.tx() as cur:
            cur.execute("UPDATE paper_positions SET status = 'CANCELLED', exit_reason = 'CANCELLED', reserve = 0, updated_at = ? "
                        "WHERE id = ? AND status = 'PENDING'", (iso(at or utc_now()), pos["id"]))
            if cur.rowcount:
                self._event(cur, pos["portfolio_id"], "CANCEL", f"Cancelled: {pos['symbol']} order ({why})",
                            position_id=pos["id"], at=at)

    def close_position(self, position_id: int, quantity: float | None = None, reason: str = "closed by you") -> dict[str, Any]:
        with self._lock:                 # never at the same moment as a background valuation exiting it at a level
            return self._close_position(position_id, quantity, reason)

    def _close_position(self, position_id: int, quantity: float | None, reason: str) -> dict[str, Any]:
        r = self.db.one("SELECT * FROM paper_positions WHERE id = ?", (position_id,))
        if r is None:
            raise PaperError(f"No paper position {position_id}")
        pos = dict(r)
        p = self._portfolio(pos["portfolio_id"])
        if pos["status"] == "PENDING":
            self._cancel(pos, reason)
            return self.detail(p["id"])
        if pos["status"] != "OPEN":
            raise PaperError("That position is not open")
        if quantity is not None and (quantity <= 0 or quantity != int(quantity) or quantity > pos["open_quantity"]):
            raise PaperError(f"Close a whole number from 1 to {pos['open_quantity']:g}")
        if not self._session_open(utc_now()) and pos["multiplier"] > 1:
            raise PaperError("Options trade 9:30 to 16:00 New York time; close it then")
        self._exit(p, pos, self._stock_quotes([pos["symbol"]]), "MANUAL", reason, quantity=quantity)
        self.mark(p["id"])
        return self.detail(p["id"])

    def _exit(self, p: dict[str, Any], pos: dict[str, Any], quotes: dict[str, dict[str, Any]], kind: str, why: str, *,
              quantity: float | None = None, at: datetime | None = None, price: float | None = None,
              underlying: float | None = None, modelled: bool = False) -> None:
        """Sell (or buy back) all or part of a position at what closing gets now, or at `price` (catch-up, levels).
        The position is read again inside the write: one already closed (or with fewer left) meanwhile is refused,
        so a sale is never applied twice."""
        now = at or utc_now()
        cost_rate = float(self.svc.settings.stock_cost_bps) / 10_000.0
        model = p["fill_model"]
        fees = 0.0
        if price is None:
            q = quotes.get(pos["symbol"]) or {}
            bid, ask, mid, last = self._sides(q)
            underlying = mid or last
            if pos["multiplier"] == 1:
                price = mid if model == "MIDPOINT" else (bid if pos["instrument"] == "SHARES" else ask)
            else:
                oq = self._option_quote(pos, underlying)
                price = oq["mid"] if model == "MIDPOINT" else oq["bid"]
                modelled = modelled or bool(oq.get("modelled"))
        if price is None or price < 0:
            raise PaperError(f"No price to close {pos['symbol']}")
        sign = -1 if pos["instrument"] == "SHORT" else 1
        with self.db.tx() as cur:
            row = cur.execute("SELECT status, quantity, open_quantity, exit_price, entry_price FROM paper_positions WHERE id = ?",
                              (pos["id"],)).fetchone()
            open_now = float(row["open_quantity"]) if row is not None and row["status"] == "OPEN" else 0.0
            qty = float(quantity or open_now)
            if open_now <= 1e-9 or qty > open_now + 1e-9:
                raise PaperError(f"{pos['symbol']} is no longer open for that many (closed meanwhile)")
            if pos["multiplier"] == 1 and model != "MIDPOINT":
                fees = price * qty * cost_rate
            pnl = sign * qty * pos["multiplier"] * (price - row["entry_price"])
            cash_change = sign * qty * pos["multiplier"] * price - fees
            left = open_now - qty
            sold_before = float(row["quantity"]) - open_now
            avg_exit = ((row["exit_price"] or 0) * sold_before + price * qty) / (sold_before + qty)
            cur.execute("""UPDATE paper_positions SET open_quantity = ?, status = ?, exit_price = ?, exit_at = ?, exit_reason = ?,
                           realized_pnl = realized_pnl + ?, fees = fees + ?, last_price = ?, last_underlying = COALESCE(?, last_underlying),
                           last_at = ?, updated_at = ? WHERE id = ? AND status = 'OPEN' AND open_quantity = ?""",
                        (left, "OPEN" if left > 1e-9 else "CLOSED", avg_exit, iso(now), kind, pnl, fees, price, underlying,
                         iso(now), iso(now), pos["id"], open_now))
            if not cur.rowcount:
                raise PaperError(f"{pos['symbol']} changed meanwhile; nothing was sold")
            cur.execute("UPDATE paper_portfolios SET cash = cash + ? WHERE id = ?", (cash_change, p["id"]))
            self._event(cur, p["id"], "CLOSE" if kind != "ROTATE" else "ROTATE",
                        f"{'Sold' if sign > 0 else 'Bought back'} {qty:g} {pos['symbol']}"
                        + (f" {pos['instrument'].lower()}s" if pos["multiplier"] > 1 else "")
                        + f" at ${price:,.2f} ({why}): {'+' if pnl >= 0 else '-'}${abs(pnl):,.2f}"
                        + (" (option valued by the model)" if modelled else ""),
                        position_id=pos["id"], price=price, quantity=qty, at=now, extra={"pnl": pnl, "kind": kind})

    # --- marking ----------------------------------------------------------------------------------------------
    def mark(self, pid: int, now: datetime | None = None) -> dict[str, Any] | None:
        """Value the portfolio now: fill what can be filled, apply stops, targets, horizons and expirations, and
        keep a mark (equity, cash, what is invested, profits, SPY)."""
        with self._lock:
            p = self._portfolio(pid)
            if p["status"] != "ACTIVE":
                return None
            now = now or utc_now()
            live = self._positions(pid, ("OPEN", "PENDING"))
            quotes = self._stock_quotes([x["symbol"] for x in live] + ["SPY"])
            modelled = False
            in_session = self._session_open(now)
            for pos in live:
                q = quotes.get(pos["symbol"])
                if pos["status"] == "PENDING":
                    if q:
                        self._try_fill(p, pos, q, now)
                    pos = dict(self.db.one("SELECT * FROM paper_positions WHERE id = ?", (pos["id"],)))
                    if pos["status"] == "PENDING" and self._past_session_end(pos, now):
                        self._cancel(pos, "not filled by the close" if pos["multiplier"] > 1 else "the session ended", now)
                    continue
                if not q:
                    continue
                bid, ask, mid, last = self._sides(q)
                under = last or mid
                reason = self._exit_reason(pos, under, now)
                if reason and not in_session and reason[0] in ("STOP", "TARGET"):
                    # Stops and targets are judged on regular-session prices (as catch-up's bars are); a pre- or
                    # after-hours quote only values the position, and a gap through a level is taken at the open.
                    reason = None
                if reason:
                    kind, why, level = reason
                    price = None
                    if kind == "TARGET" and pos["multiplier"] == 1:
                        price = max(level, bid or level) if pos["instrument"] == "SHARES" else min(level, ask or level)
                    elif kind == "EXPIRED":
                        at_expiry = self._close_on(pos["symbol"], date.fromisoformat(pos["expiration"])) or under
                        price = max((at_expiry - pos["strike"]) if pos["instrument"] == "CALL" else (pos["strike"] - at_expiry), 0.0) \
                            if at_expiry else None
                        why = f"the contract expired (the stock closed at ${at_expiry:,.2f} that day)" if at_expiry else why
                        under = at_expiry
                    elif kind == "HORIZON" and not in_session and pos["multiplier"] == 1:
                        close = self._close_on(pos["symbol"], parse(pos["horizon_end"]).astimezone(NY).date())
                        if close:
                            price, under = close, close
                            why = f"{why} (at that session's close)"
                    if pos["multiplier"] > 1 and not in_session and kind != "EXPIRED":
                        continue                       # options wait for the session to sell
                    try:
                        self._exit(p, pos, quotes, kind, why, at=now, price=price, underlying=under)
                    except PaperError as exc:
                        log.info("Paper exit deferred: %s", exc)
                    continue
                if pos["multiplier"] == 1:
                    px = mid if p["fill_model"] == "MIDPOINT" else (bid if pos["instrument"] == "SHARES" else ask)
                    basis = "quote"
                else:
                    oq = self._option_quote(pos, under)
                    px = oq["mid"] if p["fill_model"] == "MIDPOINT" else oq["bid"]
                    basis = oq.get("basis")
                    modelled = modelled or bool(oq.get("modelled"))
                if px is not None:
                    with self.db.tx() as cur:
                        cur.execute("UPDATE paper_positions SET last_price = ?, last_underlying = ?, last_at = ?, mark_basis = ? "
                                    "WHERE id = ?", (px, under, iso(now), basis, pos["id"]))
            spy = self._sides(quotes.get("SPY") or {})[3] if quotes.get("SPY") else None
            out = self._write_mark(p, now, spy, modelled)
            p = self._portfolio(pid)
            if p["ends_at"] and now >= parse(p["ends_at"]):
                self._finish(p, now)
            return out

    def _write_mark(self, p: dict[str, Any], now: datetime, spy: float | None, modelled: bool) -> dict[str, Any]:
        positions = self._positions(p["id"])
        cash = float(self.db.one("SELECT cash FROM paper_portfolios WHERE id = ?", (p["id"],))["cash"])
        invested = unrealized = 0.0
        for pos in positions:
            mv, un = self._value(pos)
            if pos["status"] == "OPEN" and mv is not None:
                invested += mv
                unrealized += un or 0.0
        realized = sum((x["realized_pnl"] or 0.0) - (x["fees"] or 0.0) for x in positions)
        equity = cash + invested
        row = {"at": iso(now), "equity": equity, "cash": cash, "invested": invested, "realized": realized,
               "unrealized": unrealized, "spy": spy, "modelled": int(modelled)}
        with self.db.tx() as cur:
            cur.execute("""INSERT INTO paper_marks (portfolio_id, at, equity, cash, invested, realized, unrealized, spy, modelled)
                           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?) ON CONFLICT(portfolio_id, at) DO UPDATE SET equity = excluded.equity,
                           cash = excluded.cash, invested = excluded.invested, realized = excluded.realized,
                           unrealized = excluded.unrealized, spy = excluded.spy, modelled = excluded.modelled""",
                        (p["id"], row["at"], equity, cash, invested, realized, unrealized, spy, int(modelled)))
            cur.execute("UPDATE paper_portfolios SET last_marked_at = ? WHERE id = ?", (row["at"], p["id"]))
        return row

    @staticmethod
    def _past_session_end(pos: dict[str, Any], now: datetime) -> bool:
        created = parse(pos["created_at"]).astimezone(NY)
        day = created.date()
        s = marketdays.session(day)
        if s is None or created >= s[1]:
            day = marketdays.next_trading_day(day)
        return now.astimezone(NY) >= marketdays.session(day)[1]

    def _close_on(self, symbol: str, day: date) -> float | None:
        """The stock's regular-session close on `day` (daily bars), or None when the bars do not have it."""
        try:
            daily = self.svc.market.bars(symbol, max(0.05, (datetime.now(NY).date() - day).days / 365 + 0.03), complete=False)
        except (DataError, ValueError) as exc:
            log.info("Paper: no daily bars for %s: %s", symbol, exc)
            return None
        if daily is None or daily.empty:
            return None
        days = pd.to_datetime(daily["date"]).dt.date
        hit = daily[days == day]
        return _f(hit["close"].iloc[-1]) if not hit.empty else None

    def _exit_or_last(self, p: dict[str, Any], pos: dict[str, Any], quotes: dict[str, dict[str, Any]], kind: str, why: str,
                      at: datetime | None = None) -> None:
        """Close at what closing gets now; with no price to be had, at the position's last valuation (said so), so a
        closed portfolio never keeps an open position."""
        try:
            self._exit(p, pos, quotes, kind, why, at=at)
        except PaperError as exc:
            last = pos.get("last_price") if pos.get("last_price") is not None else pos.get("entry_price")
            if last is None or "no longer open" in str(exc) or "changed meanwhile" in str(exc):
                log.info("Paper: could not close %s: %s", pos["symbol"], exc)
                return
            self._exit(p, pos, quotes, kind, f"{why}; no price now, so at its last valuation", at=at, price=float(last))

    @staticmethod
    def _exit_reason(pos: dict[str, Any], under: float | None, now: datetime) -> tuple[str, str, float | None] | None:
        if pos["expiration"]:
            exp = date.fromisoformat(pos["expiration"])
            s = marketdays.session(exp)
            if now >= (s[1] if s else datetime.combine(exp, DAY_CLOSE, NY)):
                return "EXPIRED", "the contract expired", None
        if pos["horizon_end"] and now >= parse(pos["horizon_end"]):
            return "HORIZON", "its horizon ended", None
        if under is None:
            return None
        d = 1 if pos["direction"] == "LONG" else -1
        if pos["stop"] and d * (under - pos["stop"]) <= 0:
            return "STOP", f"the stock traded through the stop (${pos['stop']:,.2f})", pos["stop"]
        if pos["target_1"] and d * (under - pos["target_1"]) >= 0:
            return "TARGET", f"the stock reached target 1 (${pos['target_1']:,.2f})", pos["target_1"]
        return None

    def _finish(self, p: dict[str, Any], now: datetime) -> None:
        quotes = self._stock_quotes([x["symbol"] for x in self._positions(p["id"], ("OPEN",))])
        for pos in self._positions(p["id"], ("PENDING",)):
            self._cancel(pos, "the portfolio's end came", now)
        for pos in self._positions(p["id"], ("OPEN",)):
            self._exit_or_last(p, pos, quotes, "HORIZON", "the portfolio's end came", at=now)
        self._write_mark(p, now, self._sides(self._stock_quotes(["SPY"]).get("SPY") or {})[3], False)
        with self.db.tx() as cur:
            cur.execute("UPDATE paper_portfolios SET status = 'CLOSED', closed_at = ? WHERE id = ? AND status = 'ACTIVE'",
                        (iso(now), p["id"]))
            self._event(cur, p["id"], "NOTE", "Its end came: everything was closed.", at=now)

    # --- automation --------------------------------------------------------------------------------------------
    def step(self, pid: int, force: bool = False) -> dict[str, Any]:
        """One automated pass: sell what has faded when something fresher scores well clear of it, then put idle cash
        to work on the best new opportunities, split by the blend optimizer."""
        with self._lock:
            p = self._portfolio(pid)
            if p["mode"] != "AUTO" or p["status"] != "ACTIVE":
                return {"skipped": "not an active automated portfolio"}
            now = utc_now()
            ny = now.astimezone(NY)
            if p["horizon"] == "day" and self._session_open(now) and (ny.hour, ny.minute) >= LAST_ENTRY:
                self._touch_step(pid, now)
                return {"skipped": "day trades open nothing after 15:00"}
            if self.hub is None or self.blender is None:
                raise PaperError("Automation needs the opportunity lists")
            opps, stale = self.hub.fresh(p["source"], now)
            opps = [o for o in opps if o.get("tradable", True) and o["horizon"] == p["horizon"]]
            ranked = sorted(opps, key=lambda o: -(o.get("score") or 0))
            held = self._positions(pid, ("OPEN", "PENDING"))
            held_syms = {x["symbol"] for x in held}
            notes = []
            if stale and not ranked:
                notes.append("Nothing fresh to trade: " + "; ".join(stale) + ".")
            # 1. Rotation.
            if p["rotate"] and held:
                top = ranked[:2 * p["top_n"]]
                current = {(o["symbol"], o["direction"]): o for o in top}
                fresh = [o for o in ranked[:p["top_n"]] if o["symbol"] not in held_syms]
                best_new = fresh[0]["score"] if fresh else None
                quotes = None
                for pos in [x for x in held if x["status"] == "OPEN" and x["source"] != "manual"]:
                    now_opp = current.get((pos["symbol"], pos["direction"]))
                    score = now_opp["score"] if now_opp else None
                    faded = score is None or score < FADED_SCORE
                    if faded and best_new is not None and best_new >= (score or 0) + ROTATE_HURDLE:
                        if pos["multiplier"] > 1 and not self._session_open(now):
                            continue
                        quotes = quotes or self._stock_quotes([x["symbol"] for x in held])
                        why = ("its opportunity has left the list" if score is None else f"its score fell to {score:.0f}")
                        try:
                            self._exit(p, pos, quotes, "ROTATE", f"rotated out: {why}; {fresh[0]['symbol']} scores {best_new:.0f}")
                            notes.append(f"Sold {pos['symbol']}: {why}.")
                            held_syms.discard(pos["symbol"])
                        except PaperError as exc:
                            notes.append(f"Could not rotate out of {pos['symbol']}: {exc}")
            # 2. Idle cash.
            free = self._buying_power(p)
            slots = p["max_positions"] - len(self._positions(pid, ("OPEN", "PENDING")))
            if free >= max(IDLE_SHARE * p["budget"], 100) and slots > 0:
                cands = [o for o in ranked if o["symbol"] not in held_syms][:p["top_n"]]
                if cands:
                    if any(i in p["instruments"] for i in ("call", "put")):
                        notes += self.hub.ensure_options(cands, limit=min(len(cands), 10))
                    held_sectors, held_risk, worth = self._holdings(pid)
                    try:
                        # The caps hold for the whole portfolio: shares and sectors against what it is worth, counting
                        # what it already holds, and the open-risk limit counting its own open risk.
                        res = self.blender.run(cands, budget=min(free, 10_000_000), goal=p["goal"], tail_pct=p["tail_pct"],
                                               max_share=p["max_share"], max_positions=slots, sector_cap=p["sector_cap"],
                                               instruments=p["instruments"], obey_limits=bool(p["obey_limits"]),
                                               horizon=p["horizon"], real_open_risk=False, cap_base=max(worth, free),
                                               held_sectors=held_sectors, held_risk=held_risk)
                    except ValueError as exc:
                        res = None
                        notes.append(f"No split: {exc}")
                    if res:
                        by_key = {o["key"]: o for o in cands}
                        placed = 0
                        for row in res["rows"]:
                            o = by_key.get(row["key"])
                            if o is None:
                                continue
                            try:
                                self.add_trade(pid, self.trade_from(o, row))
                                placed += 1
                            except (PaperError, DataError, ValueError) as exc:
                                notes.append(f"{row['symbol']}: {exc}")
                        if not res["rows"]:
                            notes.append("The optimizer kept the cash: " + (res["notes"][-1] if res.get("notes") else "nothing fits"))
                        elif placed:
                            notes.append(f"Placed {placed} order{'s' if placed != 1 else ''} with ${res['spent']:,.0f}.")
            if notes:
                with self.db.tx() as cur:
                    self._event(cur, pid, "NOTE", "Automated pass: " + " ".join(notes)[:480], at=now)
            self._touch_step(pid, now)
            return {"notes": notes}

    def _holdings(self, pid: int) -> tuple[dict[str, float], float, float]:
        """What the portfolio holds, for the blend's caps: dollars in each sector (by the sector fund the app
        compares each stock with), the risk still open to the stops (pending orders included), and what the
        portfolio is worth at its last valuation."""
        from .sectors import sector_fund
        sectors: dict[str, float] = {}
        risk = 0.0
        for x in self._positions(pid, ("OPEN", "PENDING")):
            qty = x["open_quantity"] if x["status"] == "OPEN" else x["quantity"]
            px = x["last_price"] or x["entry_price"] or x["limit_price"] or 0.0
            dollars = float(x["reserve"] or 0.0) if x["status"] == "PENDING" else qty * x["multiplier"] * px
            sec = sector_fund(x["symbol"]) or f"none:{x['symbol']}"
            sectors[sec] = sectors.get(sec, 0.0) + dollars
            if x["initial_risk"]:
                risk += float(x["initial_risk"]) * (qty / x["quantity"] if x["quantity"] else 1.0)
        last = self.db.one("SELECT equity FROM paper_marks WHERE portfolio_id = ? ORDER BY at DESC LIMIT 1", (pid,))
        worth = float(last["equity"]) if last else float(self._portfolio(pid)["budget"])
        return sectors, risk, worth

    def _touch_step(self, pid: int, now: datetime) -> None:
        with self.db.tx() as cur:
            cur.execute("UPDATE paper_portfolios SET last_step_at = ? WHERE id = ?", (iso(now), pid))

    @staticmethod
    def trade_from(o: dict[str, Any], row: dict[str, Any]) -> dict[str, Any]:
        """A paper order for one row of a blend (blend.py) on its opportunity."""
        t = {"symbol": o["symbol"], "instrument": row["instrument"], "quantity": int(row["units"]), "stop": o["stop"],
             "target_1": o["target_1"], "target_2": o.get("target_2"), "opp_key": o["key"], "source": o.get("source"),
             "setup_label": o.get("setup_label"), "why": f"{o.get('setup_label') or o.get('setup')} (score {o.get('score', 0):.0f})",
             "horizon_days": o.get("horizon_days") if o.get("horizon") == "swing" else None}
        if row["instrument"] in ("call", "put"):
            opt = o["option"]
            t.update({"option": {"strike": opt["strike"], "expiration": opt["expiration"], "iv": opt.get("iv")},
                      "limit_price": opt["premium"]})
        return t

    def tick(self) -> dict[str, Any]:
        """One pass of the background loop over every active portfolio: catch up after a gap, mark when due, take
        an automated step when due. Returns what it did (for the log and the tests)."""
        done = {"marked": [], "stepped": [], "caught_up": []}
        now = utc_now()
        phase = self.phase(now)
        for r in self.db.query("SELECT id, mode, last_marked_at, last_step_at, refresh_minutes, ends_at FROM paper_portfolios "
                               "WHERE status = 'ACTIVE'"):
            pid = r["id"]
            try:
                last = parse(r["last_marked_at"])
                if last is not None and (now - last).total_seconds() > 3 * MARK_SECONDS_EXTENDED and self._traded_between(last, now):
                    n = self.catch_up(pid, last, now)
                    if n:
                        done["caught_up"].append(pid)
                due = MARK_SECONDS_OPEN if phase == "open" else MARK_SECONDS_EXTENDED if phase in ("pre", "post") else None
                ends = parse(r["ends_at"])
                if (due and (last is None or (now - last).total_seconds() >= due - 1)) or (ends and now >= ends) \
                        or (phase == "closed" and last is not None and self._traded_between(last, now)):
                    self.mark(pid, now)
                    done["marked"].append(pid)
                if r["mode"] == "AUTO" and phase in ("open", "pre"):
                    ls = parse(r["last_step_at"])
                    if ls is None or (now - ls).total_seconds() >= 60 * int(r["refresh_minutes"]) - 1:
                        self.step(pid)
                        done["stepped"].append(pid)
            except Exception:                         # noqa: BLE001 - one portfolio never stops the others
                log.exception("Paper portfolio %s: the pass failed", pid)
        return done

    @staticmethod
    def _traded_between(a: datetime, b: datetime) -> bool:
        """Whether a regular session (or its close) fell between two moments."""
        d = a.astimezone(NY).date()
        end = b.astimezone(NY).date()
        while d <= end:
            s = marketdays.session(d)
            if s and s[1] > a and s[0] < b:
                return True
            d += timedelta(days=1)
        return False

    # --- catching up after the app was closed --------------------------------------------------------------------
    def _bars(self, symbol: str, since: datetime, until: datetime) -> tuple[pd.DataFrame, str]:
        """Bars between two moments, oldest first, with `t` (UTC seconds of the bar's end) and `t0` (of its start):
        5-minute bars when the source reaches back that far, else daily bars (from the session's open to its close).
        A bar is kept when it ends after `since` and starts before `until`."""
        # The stores count days back from now, so the reach needed is from now to the start of the span.
        days = max((utc_now() - since).total_seconds() / 86400.0, 0.0)
        store = getattr(getattr(self.svc, "chart_view", None), "intraday", None)
        if store is None:
            from .intraday import Intraday
            store = Intraday(self.svc.market, self.svc.db)
        reach = store.offered().get("5m", 0)
        if days <= max(reach - 1, 0):
            try:
                df = store.bars(symbol, "5m", days + 1)
                df = df.assign(t0=df["t"].astype("int64"), t=df["t"].astype("int64") + 300)
                df = df[(df["t"] > since.timestamp()) & (df["t0"] < until.timestamp())]
                return df.reset_index(drop=True), "5m"
            except DataError as exc:
                log.info("Paper: no 5-minute bars for %s: %s", symbol, exc)
        daily = self.svc.market.bars(symbol, max(0.2, days / 365 + 0.1), complete=False)
        spans = []
        for d in pd.to_datetime(daily["date"]):
            sess = marketdays.session(d.date())
            if sess is None:
                sess = (datetime.combine(d.date(), marketdays.OPEN, NY), datetime.combine(d.date(), DAY_CLOSE, NY))
            spans.append((sess[0].timestamp(), sess[1].timestamp()))
        df = daily.assign(t0=np.array([a for a, _ in spans], dtype="int64"), t=np.array([b for _, b in spans], dtype="int64"))
        df = df[(df["t"] > since.timestamp()) & (df["t0"] < until.timestamp())]
        return df.reset_index(drop=True), "1d"

    def catch_up(self, pid: int, since: datetime, until: datetime) -> int:
        """Replay what happened while the app was not running: each open position's bars from the last mark, its
        stop, target and horizon checked bar by bar (the stop first when one bar touches both; a bar that opens
        beyond a level fills at its open), and a mark at each bar. Returns how many bars were replayed."""
        with self._lock:
            p = self._portfolio(pid)
            opens = self._positions(pid, ("OPEN",))
            if not opens:
                return 0
            series: dict[str, pd.DataFrame] = {}
            kinds = set()
            for sym in {x["symbol"] for x in opens} | {"SPY"}:
                try:
                    df, kind = self._bars(sym, since, until)
                except (DataError, ValueError) as exc:
                    log.info("Paper catch-up: no bars for %s: %s", sym, exc)
                    continue
                series[sym] = df
                kinds.add(kind)
            times = sorted({int(t) for df in series.values() for t in df["t"]})
            if not times:
                return 0
            if len(times) > CATCH_UP_POINTS:
                keep = set(times[:: math.ceil(len(times) / CATCH_UP_POINTS)]) | {times[-1]}
                times = [t for t in times if t in keep]
            index = {sym: df.set_index("t") for sym, df in series.items()}
            with self.db.tx() as cur:
                self._event(cur, pid, "CATCH_UP", f"Caught up on {len(times)} bars from {since.astimezone(NY):%b %d %H:%M} to "
                            f"{until.astimezone(NY):%b %d %H:%M} New York time (the app was not running).", at=until)
            for t in times:
                when = datetime.fromtimestamp(t, timezone.utc)
                modelled = False
                for pos in self._positions(pid, ("OPEN",)):
                    df = index.get(pos["symbol"])
                    if df is None or t not in df.index:
                        continue
                    bar = df.loc[t]
                    if isinstance(bar, pd.DataFrame):
                        bar = bar.iloc[-1]
                    hit = self._bar_exit(pos, float(bar["open"]), float(bar["high"]), float(bar["low"]), float(bar["close"]), when,
                                         start=datetime.fromtimestamp(int(bar["t0"]), timezone.utc))
                    if hit:
                        kind, why, under = hit
                        price = under if pos["multiplier"] == 1 else self._model_value(pos, under, when)
                        if price is not None:
                            self._exit(p, pos, {}, kind, why + " (caught up from price history)", at=when, price=float(price),
                                       underlying=under, modelled=pos["multiplier"] > 1)
                            modelled = modelled or pos["multiplier"] > 1
                        continue
                    close = float(bar["close"])
                    px = close if pos["multiplier"] == 1 else self._model_value(pos, close, when)
                    modelled = modelled or pos["multiplier"] > 1
                    if px is not None:
                        with self.db.tx() as cur:
                            cur.execute("UPDATE paper_positions SET last_price = ?, last_underlying = ?, last_at = ?, "
                                        "mark_basis = ? WHERE id = ?",
                                        (px, close, iso(when), "bar" if pos["multiplier"] == 1 else "model", pos["id"]))
                spy_df = index.get("SPY")
                spy = float(spy_df.loc[t]["close"]) if spy_df is not None and t in spy_df.index and not isinstance(spy_df.loc[t], pd.DataFrame) else None
                self._write_mark(p, when, spy, modelled)
            return len(times)

    @staticmethod
    def _bar_exit(pos: dict[str, Any], o: float, h: float, lo: float, c: float, when: datetime,
                  start: datetime | None = None) -> tuple[str, str, float] | None:
        """Whether a bar (from `start` to `when`) takes the position out, how, and at what stock price. A bar that
        begins at or after the horizon leaves at its open; otherwise the stop, then the target, are checked on the
        bar before the horizon, which takes the bar's close (a daily bar's stop is not lost to its horizon)."""
        end = parse(pos["horizon_end"]) if pos.get("horizon_end") else None
        if end is not None and start is not None and start >= end:
            return "HORIZON", "its horizon ended", o
        d = 1 if pos["direction"] == "LONG" else -1
        stop, tgt = pos["stop"], pos["target_1"]
        if stop:
            if d * (o - stop) <= 0:
                return "STOP", f"the stock opened through the stop (${stop:,.2f})", o
            if (lo <= stop) if d > 0 else (h >= stop):
                return "STOP", f"the stock traded through the stop (${stop:,.2f})", stop
        if tgt:
            if d * (o - tgt) >= 0:
                return "TARGET", f"the stock opened beyond target 1 (${tgt:,.2f})", o
            if (h >= tgt) if d > 0 else (lo <= tgt):
                return "TARGET", f"the stock reached target 1 (${tgt:,.2f})", tgt
        if end is not None and when >= end:
            return "HORIZON", "its horizon ended", c
        return None

    # --- statistics --------------------------------------------------------------------------------------------
    def stats(self, p: dict[str, Any], positions: list[dict[str, Any]], marks: list[dict[str, Any]]) -> dict[str, Any]:
        budget = float(p["budget"])
        equity = float(marks[-1]["equity"]) if marks else budget
        eq = np.array([m["equity"] for m in marks], dtype=float) if marks else np.array([budget])
        peak = np.maximum.accumulate(np.concatenate([[budget], eq]))
        dd = float(np.max((peak - np.concatenate([[budget], eq])) / peak)) if len(eq) else 0.0
        closed = [x for x in positions if x["status"] == "CLOSED"]
        pnls = [(x["realized_pnl"] or 0) - (x["fees"] or 0) for x in closed]
        rs = [((x["realized_pnl"] or 0) - (x["fees"] or 0)) / x["initial_risk"] for x in closed if x.get("initial_risk")]
        spys = [m["spy"] for m in marks if m.get("spy")]

        def group(key: str) -> list[dict[str, Any]]:
            out: dict[str, dict[str, Any]] = {}
            for x in positions:
                if x["status"] not in ("OPEN", "CLOSED"):
                    continue
                k = x.get(key) or "manual"
                g = out.setdefault(k, {"key": k, "trades": 0, "pnl": 0.0, "wins": 0})
                mv, un = self._value(x)
                total = (x["realized_pnl"] or 0) - (x["fees"] or 0) + (un or 0)
                g["trades"] += 1
                g["pnl"] += total
                g["wins"] += total > 0
            return sorted(out.values(), key=lambda g: -g["pnl"])
        return {"budget": budget, "equity": equity, "return_pct": 100 * (equity / budget - 1), "pnl": equity - budget,
                "max_drawdown_pct": 100 * dd, "closed_trades": len(closed),
                "win_rate": (sum(1 for v in pnls if v > 0) / len(pnls)) if pnls else None,
                "average_r": float(np.mean(rs)) if rs else None,
                "best": max(pnls) if pnls else None, "worst": min(pnls) if pnls else None,
                "spy_return_pct": 100 * (spys[-1] / spys[0] - 1) if len(spys) >= 2 and spys[0] else None,
                "by_source": group("source"), "by_instrument": group("instrument"),
                "open": sum(1 for x in positions if x["status"] == "OPEN"),
                "pending": sum(1 for x in positions if x["status"] == "PENDING"),
                "modelled_marks": sum(1 for m in marks if m.get("modelled"))}

    # --- replay ---------------------------------------------------------------------------------------------------
    def replay(self, req: dict[str, Any]) -> dict[str, Any]:
        """What a stored recommendation run would have done over a past horizon: the blend optimizer splits the budget
        across its best `top_n` as it would have then, each trade enters at the open of the first bar that begins after
        the run (no look-ahead: on daily bars a run made during a session enters at the next session's open), and its
        stop, target and horizon are applied bar by bar. Options are valued with
        Black-76 at the card's volatility (modelled). With `save`, it is kept as a closed REPLAY portfolio."""
        from .opportunities import Runs
        budget = _f(req.get("budget"))
        if not budget or not 100 <= budget <= 10_000_000:
            raise PaperError("The budget must be between $100 and $10,000,000")
        if req.get("run_id"):
            run = Runs(self.db).get(int(req["run_id"]))
            if run is None:
                raise PaperError("No stored run with that number")
            opps, started, label = run["opportunities"], parse(run["created_at"]), run["meta"].get("label") or run["source"]
        elif req.get("picks_day"):
            opps, started, label = self._picks_as_opportunities(str(req["picks_day"]))
        else:
            raise PaperError("Choose a stored run or a day of Morning plan picks")
        opps = [o for o in opps if o.get("tradable", True)]
        if not opps:
            raise PaperError("That run has no tradable opportunities")
        horizon = opps[0].get("horizon") or "day"
        days = int(req.get("horizon_days") or (1 if horizon == "day" else 20))
        top_n = int(req.get("top_n") or 10)
        cands = sorted(opps, key=lambda o: -(o.get("score") or 0))[:top_n]
        if self.blender is None:
            raise PaperError("Replay needs the blend optimizer")
        started_ny = started.astimezone(NY)
        res = self.blender.run(cands, budget=budget, goal=str(req.get("goal") or "balanced"),
                               tail_pct=float(req.get("tail_pct") or 2.0), max_share=float(req.get("max_share") or 0.2),
                               max_positions=int(req.get("max_positions") or 10), instruments=req.get("instruments"),
                               obey_limits=bool(req.get("obey_limits", False)), horizon=horizon, now=started_ny,
                               real_open_risk=False)
        rows = res["rows"]
        used_fallback = False
        if not rows and res.get("fallback"):
            rows, used_fallback = res["fallback"]["rows"], True
        if not rows:
            raise PaperError("The optimizer would have kept the cash: " + "; ".join(res.get("notes") or []))
        # The horizon's end: the session for day trades, `days` trading days for swing.
        first_day = started_ny.date()
        s = marketdays.session(first_day)
        if s is None or started_ny >= s[1]:
            first_day = marketdays.next_trading_day(first_day)
        end = day_exit(first_day) if horizon == "day" else marketdays.horizon_close(started, days)
        if end > utc_now():
            raise PaperError(f"That horizon ends {end.astimezone(NY):%b %d %H:%M}, which has not happened yet: replay an older "
                             "run, a shorter horizon, or paper trade it forward instead")
        by_key = {o["key"]: o for o in cands}
        trades = []
        cost_rate = float(self.svc.settings.stock_cost_bps) / 10_000.0
        spent = 0.0
        times_all: set[int] = set()
        series = {}
        for row in rows:
            o = by_key[row["key"]]
            try:
                df, kind = self._bars(o["symbol"], started, end)
            except (DataError, ValueError) as exc:
                trades.append({"symbol": o["symbol"], "error": str(exc)})
                continue
            # No look-ahead: the first bar is one that begins at or after the run (on daily bars, a run made during
            # or after a session enters at the next session's open).
            df = df[(df["t0"] >= int(started.timestamp())) & (df["t0"] < int(end.timestamp()))].reset_index(drop=True)
            if df.empty:
                trades.append({"symbol": o["symbol"], "error": "no bar begins between the run and its horizon" + (
                    " (a day trade from a run made during the session needs 5-minute bars, and they do not reach back "
                    "that far)" if horizon == "day" and kind == "1d" else "")})
                continue
            series[row["key"]] = (o, row, df, kind)
            times_all |= set(int(t) for t in df["t"])
        try:
            spy_df, _ = self._bars("SPY", started - timedelta(minutes=1), end)
        except (DataError, ValueError):
            spy_df = pd.DataFrame(columns=["t", "close"])
        times = sorted(times_all)
        if not times:
            why = "; ".join(f"{t['symbol']}: {t['error']}" for t in trades if t.get("error"))[:400]
            raise PaperError("No price history covers that span" + (f" ({why})" if why else ""))
        cash = budget
        open_pos = []
        for o, row, df, _kind in series.values():
            first = df.iloc[0]
            entry_under = float(first["open"])
            when = datetime.fromtimestamp(int(first["t0"]), timezone.utc)
            qty = int(row["units"])
            pos = {"symbol": o["symbol"], "direction": o["direction"], "instrument": row["instrument"].upper(),
                   "multiplier": MULTIPLIER if row["instrument"] in ("call", "put") else 1, "stop": o["stop"],
                   "target_1": o["target_1"], "horizon_end": iso(end), "expiration": None, "strike": None, "iv": None,
                   "quantity": qty, "opp_key": o["key"], "setup_label": o.get("setup_label"), "source": o.get("source")}
            if pos["multiplier"] > 1:
                opt = o.get("option") or {}
                pos.update({"strike": opt.get("strike"), "expiration": opt.get("expiration"), "iv": opt.get("iv")})
                entry = float(opt.get("premium") or 0) or (self._model_value(pos, entry_under, when) or 0)
            else:
                entry = entry_under
            pos["entry_price"], pos["entry_at"], pos["entry_underlying"] = entry, iso(when), entry_under
            fee = 0.0 if pos["multiplier"] > 1 else entry * qty * cost_rate
            sign = -1 if pos["instrument"] == "SHORT" else 1
            cash -= sign * qty * pos["multiplier"] * entry + fee
            pos["fees"] = fee
            spent += qty * pos["multiplier"] * entry
            pos["df"] = df.set_index("t")
            pos["open"] = True
            pos["last"] = entry
            open_pos.append(pos)
        curve = []
        for t in times:
            when = datetime.fromtimestamp(t, timezone.utc)
            for pos in open_pos:
                if not pos["open"] or t not in pos["df"].index:
                    continue
                bar = pos["df"].loc[t]
                if isinstance(bar, pd.DataFrame):
                    bar = bar.iloc[-1]
                hit = self._bar_exit(pos, float(bar["open"]), float(bar["high"]), float(bar["low"]), float(bar["close"]), when,
                                     start=datetime.fromtimestamp(int(bar["t0"]), timezone.utc))
                under = float(bar["close"]) if not hit else hit[2]
                px = under if pos["multiplier"] == 1 else (self._model_value(pos, under, when) or 0.0)
                pos["last"] = px
                if hit:
                    fee = 0.0 if pos["multiplier"] > 1 else px * pos["quantity"] * cost_rate
                    sign = -1 if pos["instrument"] == "SHORT" else 1
                    cash += sign * pos["quantity"] * pos["multiplier"] * px - fee
                    pos["fees"] += fee
                    pos.update({"open": False, "exit_price": px, "exit_at": iso(when), "exit_reason": hit[0], "why": hit[1]})
            value = 0.0
            for pos in open_pos:
                if pos["open"]:
                    sign = -1 if pos["instrument"] == "SHORT" else 1
                    value += sign * pos["quantity"] * pos["multiplier"] * pos["last"]
            spy = None
            if not spy_df.empty:
                sp = spy_df[spy_df["t"] <= t]
                spy = float(sp["close"].iloc[-1]) if not sp.empty else None
            curve.append({"at": iso(when), "equity": cash + value, "spy": spy})
        for pos in open_pos:
            if pos["open"]:
                fee = 0.0 if pos["multiplier"] > 1 else pos["last"] * pos["quantity"] * cost_rate
                sign = -1 if pos["instrument"] == "SHORT" else 1
                cash += sign * pos["quantity"] * pos["multiplier"] * pos["last"] - fee
                pos.update({"open": False, "exit_price": pos["last"], "exit_at": iso(end), "exit_reason": "HORIZON",
                            "why": "its horizon ended", "fees": pos["fees"] + fee})
            sign = -1 if pos["instrument"] == "SHORT" else 1
            pos["pnl"] = sign * pos["quantity"] * pos["multiplier"] * (pos["exit_price"] - pos["entry_price"]) - pos["fees"]
            trades.append({k: v for k, v in pos.items() if k not in ("df", "open", "last")})
        final = cash
        eq = np.array([c["equity"] for c in curve] or [budget])
        peak = np.maximum.accumulate(np.concatenate([[budget], eq]))
        spys = [c["spy"] for c in curve if c["spy"]]
        closed = [x for x in trades if "pnl" in x]
        out = {"label": label, "started": iso(started), "ended": iso(end), "horizon": horizon, "horizon_days": days,
               "budget": budget, "spent": spent, "final": final, "return_pct": 100 * (final / budget - 1),
               "max_drawdown_pct": 100 * float(np.max((peak - np.concatenate([[budget], eq])) / peak)),
               "spy_return_pct": 100 * (spys[-1] / spys[0] - 1) if len(spys) >= 2 else None,
               "win_rate": (sum(1 for x in closed if x["pnl"] > 0) / len(closed)) if closed else None,
               "trades": trades, "curve": curve, "fallback": used_fallback, "blend": {k: res[k] for k in ("goal_label", "spent")},
               "modelled": any(x["instrument"] in ("CALL", "PUT") for x in closed), "bars": sorted({k for *_, k in series.values()}),
               "notes": (["No record in this run beat its costs, so the equal-risk split (no edge behind it) is what was "
                          "replayed."] if used_fallback else []) +
                        ["Entries at the open of the first bar that begins after the run (on daily bars, the next session's "
                         "open for a run made during or after a session); stops and targets on bars, the stop first when a bar "
                         "touches both, the horizon at its bar's close; options valued by the model at the card's volatility."]}
        if req.get("save"):
            out["portfolio_id"] = self._save_replay(out, req)
        return out

    def _picks_as_opportunities(self, day: str) -> tuple[list[dict[str, Any]], datetime, str]:
        rows = self.db.query("SELECT * FROM morning_picks WHERE day = ? ORDER BY id", (day,))
        if not rows:
            raise PaperError(f"No Morning plan picks on {day}")
        opps = []
        for r in rows:
            opt = json.loads(r["option_json"]) if r["option_json"] else None
            o = make(source="morning", key=f"pick:{r['id']}", symbol=r["symbol"], direction=r["direction"], setup=r["setup"],
                     setup_label=f"Morning pick ({r['setup']})", horizon="swing" if r["horizon_days"] > 1 else "day",
                     horizon_days=int(r["horizon_days"]), entry=float(r["entry"]), stop=float(r["stop"]),
                     target_1=float(r["target_1"]), target_2=r["target_2"], score=50.0, tradable=True)
            if opt and r["limit_price"]:
                o["option"] = {"option_type": str(opt["option_type"]).upper(), "strike": float(opt["strike"]),
                               "expiration": opt["expiration"], "premium": float(r["limit_price"]), "iv": None}
            opps.append(o)
        started = datetime.combine(date.fromisoformat(day), datetime.min.time(), NY).replace(hour=9, minute=0)
        return opps, started.astimezone(timezone.utc), f"Morning plan picks of {day}"

    def _save_replay(self, out: dict[str, Any], req: dict[str, Any]) -> int:
        now = utc_now()
        with self.db.tx() as cur:
            cur.execute("""INSERT INTO paper_portfolios (name, mode, status, created_at, closed_at, starts_at, ends_at, budget, cash,
                           source, goal, top_n, horizon, fill_model, obey_limits, rotate, run_id, data_source, notes)
                           VALUES (?, 'REPLAY', 'CLOSED', ?, ?, ?, ?, ?, ?, 'run', ?, ?, ?, 'MIDPOINT', ?, 0, ?, ?, ?)""",
                        (f"Replay: {out['label']}"[:80], iso(now), iso(now), out["started"], out["ended"], out["budget"], out["final"],
                         str(req.get("goal") or "balanced"), int(req.get("top_n") or 10), out["horizon"],
                         int(bool(req.get("obey_limits", False))), req.get("run_id"), self.svc.market.info.mode,
                         " ".join(out["notes"])[:500]))
            pid = int(cur.lastrowid)
            for x in out["trades"]:
                if "pnl" not in x:
                    continue
                cur.execute("""INSERT INTO paper_positions (portfolio_id, opp_key, source, setup_label, symbol, instrument, direction,
                               strike, expiration, iv, multiplier, quantity, open_quantity, status, entry_price, entry_at,
                               entry_underlying, exit_price, exit_at, exit_reason, stop, target_1, horizon_end, last_price,
                               realized_pnl, fees, why, created_at, updated_at)
                               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, 'CLOSED', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                            (pid, x.get("opp_key"), x.get("source"), x.get("setup_label"), x["symbol"], x["instrument"],
                             x["direction"], x.get("strike"), x.get("expiration"), x.get("iv"), x["multiplier"], x["quantity"],
                             x["entry_price"], x["entry_at"], x["entry_underlying"], x["exit_price"], x["exit_at"],
                             x["exit_reason"], x["stop"], x["target_1"], x["horizon_end"], x["exit_price"],
                             x["pnl"] + x["fees"], x["fees"], x.get("why") or "", iso(now), iso(now)))
            for c in out["curve"]:
                cur.execute("INSERT OR REPLACE INTO paper_marks (portfolio_id, at, equity, cash, invested, realized, unrealized, "
                            "spy, modelled) VALUES (?, ?, ?, 0, 0, 0, 0, ?, ?)",
                            (pid, c["at"], c["equity"], c["spy"], int(out["modelled"])))
            self._event(cur, pid, "NOTE", f"Replayed {out['label']} from {out['started'][:16]} to {out['ended'][:16]} UTC.", at=now)
        return pid


__all__ = ["PaperBook", "PaperError", "FILL_MODELS", "end_of", "day_exit", "market_phase"]
