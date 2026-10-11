"""Your trades, rebuilt from the imported history and analysed (2.6). Read-only, deterministic, local.

**Rebuilding trades.** The kept rows (tradehistory.py) are replayed oldest first. Opening rows (Buy, BTO,
STO) open lots; closing rows close them first in, first out: Sell and STC close bought lots, BTC closes
sold ones; an expiration closes the contract's lots at no value; an assignment closes the sold option at
no value (the shares it delivers come as their own Buy or Sell row); an exercise does the same for a
bought option. A split scales the open lots of that stock. Every closed piece has a result from the cash
actually paid and received (the Amount column, fees included), never from a price guess, unless the row
had no amount (then price x quantity, marked as estimated).

A closing row with nothing open to close was opened before the imported history starts. Its result is
unknown and is left out and listed, never counted as zero.

**Strategies.** Legs opened on the same day in the same stock are read as one trade (the report has no
times, so two separate ideas opened the same day in one stock are read as one). The legs name it: shares,
a long call or put, a short put, a covered or uncovered call, a debit or credit vertical, a calendar or
diagonal, a straddle or strangle, an iron condor or butterfly, a butterfly, a buy-write; anything else is
"Multi-leg (other)".

**Risk at the open.** The most the trade could lose as opened, from its legs: the cost of shares, the
premium or debit paid, a credit spread's width less its credit, a short put's strike less its premium (the
stock going to zero). An uncovered short call or short shares have no limit and are shown as unlimited,
never as a number. Anything this app cannot bound is "not measured".

**Analysis.** For any date range: results by week, month, quarter or year; by strategy, with the risk each
carried (in total, the largest single one, and dollar-days at risk); the peak of everything open at once;
and the patterns in which trades did better or worse than your average. These are counts of your own past
trades, with how many each figure rests on. They are not forecasts, and a small count is marked as such.
"""

from __future__ import annotations

import math
import statistics
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Any, Callable, Iterable

MULT = 100
EPS = 0.005
MIN_PATTERN_TRADES = 5
OPENING = {"Buy", "BTO", "STO"}
WEEKDAYS = ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday")


@dataclass
class Lot:
    key: tuple
    side: int                 # +1 bought, -1 sold
    qty: float
    unit_cash: float          # per share or per contract: cost if bought, proceeds if sold (positive)
    open_date: str
    group: int
    row_id: int
    estimated: bool = False


@dataclass
class Group:
    id: int
    symbol: str
    open_date: str
    open_seq: int
    legs: list[dict[str, Any]] = field(default_factory=list)
    pieces: list[dict[str, Any]] = field(default_factory=list)
    lots: list[Lot] = field(default_factory=list)
    row_ids: list[int] = field(default_factory=list)
    from_assignment: bool = False

    @property
    def open_qty(self) -> float:
        return sum(lot.qty for lot in self.lots)

    @property
    def closed(self) -> bool:
        return self.open_qty <= 1e-9 and bool(self.pieces)


@dataclass
class Book:
    groups: list[Group]
    unmatched: list[dict[str, Any]]
    notes: list[str]
    rows: dict[int, dict[str, Any]]
    first_date: str | None
    last_date: str | None
    open_lots: list[Lot]


def _key(r: dict[str, Any]) -> tuple:
    if r["kind"] == "OPTION":
        return ("OPTION", r["symbol"], r["option_type"], float(r["strike"]), r["expiration"])
    return ("STOCK", r["symbol"])


def _unit_cash(r: dict[str, Any], paying: bool) -> tuple[float, bool]:
    """Cash per share or contract for a trade row, from Amount (fees included), else price x size."""
    qty = r["quantity"]
    if r.get("amount") is not None and qty:
        return abs(r["amount"]) / qty, False
    mult = MULT if r["kind"] == "OPTION" else 1
    return abs(r["price"] or 0.0) * mult, True


def rebuild(rows: Iterable[dict[str, Any]]) -> Book:
    """Replay the rows (oldest first, as tradehistory.activity returns them) into trades."""
    rows = list(rows)
    lots: dict[tuple, list[Lot]] = defaultdict(list)
    groups: list[Group] = []
    by_open: dict[tuple[str, str], Group] = {}
    unmatched: list[dict[str, Any]] = []
    notes: list[str] = []
    events_today: dict[tuple[str, str], set[str]] = defaultdict(set)   # (date, symbol) -> OASGN / OEXCS seen

    def group_for(r: dict[str, Any], seq: int) -> Group:
        k = (r["activity_date"], r["symbol"])
        g = by_open.get(k)
        if g is None or g.closed:
            g = Group(id=len(groups) + 1, symbol=r["symbol"], open_date=r["activity_date"], open_seq=seq)
            groups.append(g)
            by_open[k] = g
        return g

    def shares_held(symbol: str) -> float:
        return sum(lot.qty for lot in lots[("STOCK", symbol)] if lot.side > 0)

    def open_lot(r: dict[str, Any], side: int, seq: int) -> None:
        key = _key(r)
        g = group_for(r, seq)
        unit, est = _unit_cash(r, paying=side > 0)
        lot = Lot(key, side, float(r["quantity"]), unit, r["activity_date"], g.id, r["id"], est)
        lots[key].append(lot)
        g.lots.append(lot)
        g.row_ids.append(r["id"])
        leg = {"kind": r["kind"], "side": side, "qty": float(r["quantity"]), "cash": -side * unit * r["quantity"],
               "option_type": r.get("option_type"), "strike": r.get("strike"), "expiration": r.get("expiration"),
               "row_id": r["id"], "estimated": est}
        if r["kind"] == "OPTION" and side < 0 and r["option_type"] == "CALL":
            # Shares held when the call was sold (this day's earlier buys included): covered or not.
            leg["covered_shares"] = shares_held(r["symbol"])
        g.legs.append(leg)
        if r["kind"] == "STOCK" and side > 0 and events_today.get((r["activity_date"], r["symbol"])):
            g.from_assignment = True

    def close(r: dict[str, Any], side_to_close: int, how: str, seq: int, value: float | None = None) -> None:
        """Close `quantity` of the lots of `side_to_close` FIFO. `value` is cash per unit at the close (None:
        from the row's amount)."""
        key = _key(r)
        want = float(r["quantity"] or 0)
        if value is None:
            unit, est = _unit_cash(r, paying=side_to_close < 0)
        else:
            unit, est = value, False
        open_ = [lot for lot in lots[key] if lot.side == side_to_close and lot.qty > 1e-9]
        for lot in open_:
            if want <= 1e-9:
                break
            take = min(lot.qty, want)
            pnl = (unit - lot.unit_cash) * take if lot.side > 0 else (lot.unit_cash - unit) * take
            g = groups[lot.group - 1]
            g.pieces.append({"key": key, "side": lot.side, "qty": take, "open_date": lot.open_date,
                             "close_date": r["activity_date"], "open_unit": lot.unit_cash, "close_unit": unit,
                             "pnl": pnl, "how": how, "open_row_id": lot.row_id, "close_row_id": r["id"], "seq": seq,
                             "estimated": est or lot.estimated})
            if r["id"] not in g.row_ids:
                g.row_ids.append(r["id"])
            lot.qty -= take
            want -= take
        lots[key] = [lot for lot in lots[key] if lot.qty > 1e-9]
        if want > 1e-9:
            unmatched.append({"row_id": r["id"], "date": r["activity_date"], "code": r["code"], "symbol": r["symbol"],
                              "contract": _contract(key), "quantity": want, "amount": r.get("amount"),
                              "reason": "nothing open to close in the imported history: opened before it starts"})

    for seq, r in enumerate(rows):
        code = r["code"]
        if code == "SPL":
            held = [lot for lot in lots[("STOCK", r["symbol"])] if lot.side > 0]
            before = sum(lot.qty for lot in held)
            delta = float(r["quantity"]) * (-1 if (r.get("amount") or 0) < 0 else 1)
            if before > 0 and before + delta > 0:
                factor = (before + delta) / before
                for lot in held:
                    lot.qty *= factor
                    lot.unit_cash /= factor
                notes.append(f"{r['symbol']} split on {r['activity_date']}: open shares scaled by {factor:g}.")
            else:
                notes.append(f"{r['symbol']} split on {r['activity_date']} with no open shares in the history: not applied.")
            continue
        if code == "Buy":
            if any(lot.side < 0 for lot in lots[_key(r)]):
                close(r, -1, "bought back", seq)
            else:
                open_lot(r, +1, seq)
        elif code == "Sell":
            close(r, +1, "sold", seq)
        elif code == "BTO":
            open_lot(r, +1, seq)
        elif code == "STO":
            open_lot(r, -1, seq)
        elif code == "STC":
            close(r, +1, "sold to close", seq)
        elif code == "BTC":
            close(r, -1, "bought to close", seq)
        elif code == "OEXP":
            key = _key(r)
            longs = any(lot.side > 0 for lot in lots[key])
            shorts = any(lot.side < 0 for lot in lots[key])
            side = (-1 if r.get("short_flag") else +1) if longs and shorts else (-1 if shorts else +1)
            close(r, side, "expired", seq, value=0.0)
        elif code == "OASGN":
            events_today[(r["activity_date"], r["symbol"])].add(code)
            close(r, -1, "assigned", seq, value=0.0)
        elif code in ("OEXCS", "OEXER"):
            events_today[(r["activity_date"], r["symbol"])].add(code)
            close(r, +1, "exercised", seq, value=0.0)
    dates = [r["activity_date"] for r in rows]
    open_lots = [lot for ls in lots.values() for lot in ls if lot.qty > 1e-9]
    return Book(groups=groups, unmatched=unmatched, notes=notes, rows={r["id"]: r for r in rows},
                first_date=min(dates) if dates else None, last_date=max(dates) if dates else None, open_lots=open_lots)


def _contract(key: tuple) -> str:
    if key[0] == "STOCK":
        return f"{key[1]} shares"
    _, sym, typ, strike, exp = key
    d = date.fromisoformat(exp)
    return f"{sym} {d:%b} {d.day}, {d.year} {strike:g} {typ.lower()}"


# --- strategies and their risk ------------------------------------------------------------------------

def classify(g: Group) -> tuple[str, float | None, str]:
    """(strategy, most it could lose as opened or None, how that was worked out)."""
    legs = g.legs
    net = sum(leg["cash"] for leg in legs)          # + received, - paid
    stock = [leg for leg in legs if leg["kind"] == "STOCK"]
    opts = [leg for leg in legs if leg["kind"] == "OPTION"]

    def bounded(v: float, basis: str) -> tuple[float | None, str]:
        return (v, basis) if v > EPS else (None, "could not be measured from these legs")

    if stock and not opts:
        if all(leg["side"] > 0 for leg in stock):
            return ("Shares (long)", *bounded(-net, "the cost of the shares"))
        if all(leg["side"] < 0 for leg in stock):
            return "Shares (short)", None, "unlimited: a short sale has no ceiling"
        return "Shares (other)", None, "not measured"
    if stock and opts:
        shares = sum(leg["qty"] for leg in stock if leg["side"] > 0)
        calls = [leg for leg in opts if leg["option_type"] == "CALL" and leg["side"] < 0]
        if len(opts) == len(calls) and all(leg["side"] > 0 for leg in stock) and shares >= 100 * sum(c["qty"] for c in calls):
            return ("Buy-write (covered call)", *bounded(-net, "the cost of the shares less the premium received"))
        return "Shares and options (other)", None, "not measured"
    if len(opts) == 1:
        o = opts[0]
        typ, qty = o["option_type"], o["qty"]
        if o["side"] > 0:
            return (f"Long {typ.lower()}", *bounded(-net, "the premium paid"))
        if typ == "PUT":
            return ("Short put", *bounded(o["strike"] * MULT * qty - net, "the strike if the stock fell to zero, less the premium"))
        if o.get("covered_shares", 0) >= MULT * qty - 1e-9:
            return "Covered call", None, "covered by shares you held: their risk is counted with the shares"
        return "Short call (uncovered)", None, "unlimited: an uncovered call has no ceiling"
    qtys = {leg["qty"] for leg in opts}
    exps = {leg["expiration"] for leg in opts}
    calls = [leg for leg in opts if leg["option_type"] == "CALL"]
    puts = [leg for leg in opts if leg["option_type"] == "PUT"]
    if len(opts) == 2 and len(qtys) == 1:
        a, b = opts
        q = a["qty"]
        if a["option_type"] == b["option_type"] and a["side"] != b["side"]:
            word = a["option_type"].lower().capitalize()
            if len(exps) == 1:
                width = abs(a["strike"] - b["strike"])
                if net < 0:
                    return (f"{word} debit spread", *bounded(-net, "the debit paid"))
                return (f"{word} credit spread", *bounded(width * MULT * q - net, "the width between strikes less the credit"))
            if net < 0:
                return ("Calendar or diagonal", *bounded(-net, "the debit paid (close to the most it can lose)"))
            return "Calendar or diagonal (credit)", None, "not measured"
        if {a["option_type"], b["option_type"]} == {"CALL", "PUT"} and a["side"] == b["side"] and len(exps) == 1:
            name = "straddle" if a["strike"] == b["strike"] else "strangle"
            if a["side"] > 0:
                return (f"Long {name}", *bounded(-net, "the premium paid"))
            return f"Short {name}", None, "unlimited: the call side has no ceiling"
    if len(opts) == 4 and len(qtys) == 1 and len(exps) == 1 and len(calls) == 2 and len(puts) == 2 \
            and {leg["side"] for leg in calls} == {1, -1} and {leg["side"] for leg in puts} == {1, -1}:
        q = opts[0]["qty"]
        sc = next(leg for leg in calls if leg["side"] < 0)
        sp = next(leg for leg in puts if leg["side"] < 0)
        wc = abs(calls[0]["strike"] - calls[1]["strike"])
        wp = abs(puts[0]["strike"] - puts[1]["strike"])
        name = "Iron butterfly" if sc["strike"] == sp["strike"] else "Iron condor"
        if net > 0:
            return (name, *bounded(max(wc, wp) * MULT * q - net, "the wider side's width less the credit"))
        return (name, *bounded(-net, "the debit paid"))
    if len(opts) == 3 and len(exps) == 1 and len({leg["option_type"] for leg in opts}) == 1:
        ks = sorted(opts, key=lambda leg: leg["strike"])
        if ks[0]["side"] > 0 and ks[2]["side"] > 0 and ks[1]["side"] < 0 and ks[1]["qty"] == ks[0]["qty"] + ks[2]["qty"] \
                and ks[0]["qty"] == ks[2]["qty"] and net < 0:
            return ("Butterfly", *bounded(-net, "the debit paid"))
    return "Multi-leg (other)", None, "not measured"


# --- one row per trade ---------------------------------------------------------------------------------

def summarize(book: Book, today: date, price_on: Callable[[str, str], float | None] | None = None) -> list[dict[str, Any]]:
    """Each trade with its strategy, risk, dates, result and the features the patterns use."""
    out = []
    for g in book.groups:
        strategy, risk, basis = classify(g)
        realized = sum(p["pnl"] for p in g.pieces)
        status = "CLOSED" if g.closed else ("PARTLY_CLOSED" if g.pieces else "OPEN")
        close_date = max(p["close_date"] for p in g.pieces) if g.closed else None
        opts = [leg for leg in g.legs if leg["kind"] == "OPTION"]
        hows = {p["how"] for p in g.pieces if p["key"][0] == "OPTION"}
        if not opts:
            ended = "Sold" if g.closed else None
        elif "assigned" in hows:
            ended = "Assigned"
        elif "exercised" in hows:
            ended = "Exercised"
        elif g.closed and hows == {"expired"}:
            ended = "Expired"
        elif g.closed and "expired" in hows:
            ended = "Partly expired"
        elif g.closed:
            ended = "Closed before expiration"
        else:
            ended = None
        end_day = date.fromisoformat(close_date) if close_date else today
        held = (end_day - date.fromisoformat(g.open_date)).days
        dte = min((date.fromisoformat(leg["expiration"]) - date.fromisoformat(g.open_date)).days for leg in opts) if opts else None
        moneyness = None
        if price_on and len(opts) == 1 and not [leg for leg in g.legs if leg["kind"] == "STOCK"]:
            px = price_on(g.symbol, g.open_date)
            if px:
                o = opts[0]
                # Positive: out of the money by that share of the stock price.
                moneyness = (o["strike"] - px) / px if o["option_type"] == "CALL" else (px - o["strike"]) / px
        out.append({
            "id": g.id, "symbol": g.symbol, "strategy": strategy, "status": status, "open_date": g.open_date,
            "close_date": close_date, "days_held": held, "risk": risk, "risk_basis": basis,
            "unlimited": risk is None and basis.startswith("unlimited"),
            "net_open_cash": sum(leg["cash"] for leg in g.legs), "realized_pnl": realized if g.pieces else None,
            "result": None if not g.closed else ("WIN" if realized > EPS else "LOSS" if realized < -EPS else "FLAT"),
            "ended": ended, "dte_at_open": dte, "moneyness_at_open": moneyness, "from_assignment": g.from_assignment,
            "estimated": any(leg["estimated"] for leg in g.legs) or any(p["estimated"] for p in g.pieces),
            "legs": [{"contract": _contract(("OPTION", g.symbol, leg["option_type"], leg["strike"], leg["expiration"]))
                      if leg["kind"] == "OPTION" else f"{g.symbol} shares", "side": "bought" if leg["side"] > 0 else "sold",
                      "qty": leg["qty"], "cash": leg["cash"], "row_id": leg["row_id"]} for leg in g.legs],
            "closes": [{"contract": _contract(p["key"]), "qty": p["qty"], "date": p["close_date"], "how": p["how"],
                        "pnl": p["pnl"], "row_id": p["close_row_id"]} for p in g.pieces],
            "row_ids": sorted(g.row_ids), "open_seq": g.open_seq,
        })
    # Features that need the whole list: size against your usual, a loss just before, how often the stock is traded.
    risks = [t["risk"] for t in out if t["risk"]]
    usual = statistics.median(risks) if risks else None
    per_symbol = defaultdict(int)
    for t in out:
        per_symbol[t["symbol"]] += 1
    losing_closes = sorted((p["seq"], p["close_date"]) for g in book.groups for p in g.pieces
                           if g.closed and max(q["seq"] for q in g.pieces) == p["seq"]
                           and sum(q["pnl"] for q in g.pieces) < -EPS)
    for t in out:
        t["size_vs_usual"] = (t["risk"] / usual) if (t["risk"] and usual) else None
        t["symbol_trades"] = per_symbol[t["symbol"]]
        start = (date.fromisoformat(t["open_date"]) - timedelta(days=2)).isoformat()
        t["after_loss"] = any(start <= d <= t["open_date"] and seq < t["open_seq"] for seq, d in losing_closes)
    return out


# --- date ranges and periods --------------------------------------------------------------------------

def period_key(day: str, by: str) -> str:
    d = date.fromisoformat(day)
    if by == "week":
        return (d - timedelta(days=d.weekday())).isoformat()
    if by == "quarter":
        return f"{d.year}-Q{(d.month - 1) // 3 + 1}"
    if by == "year":
        return str(d.year)
    return f"{d.year}-{d.month:02d}"


def period_label(key: str, by: str) -> str:
    if by == "week":
        d = date.fromisoformat(key)
        return f"Week of {d:%b} {d.day}, {d.year}"
    if by == "month":
        y, m = key.split("-")
        return f"{date(int(y), int(m), 1):%b %Y}"
    return key.replace("-", " ")


def period_bounds(key: str, by: str) -> tuple[str, str]:
    if by == "week":
        d = date.fromisoformat(key)
        return key, (d + timedelta(days=6)).isoformat()
    if by == "quarter":
        y, q = key.split("-Q")
        start = date(int(y), 3 * (int(q) - 1) + 1, 1)
        end = date(int(y) + (1 if q == "4" else 0), 1 if q == "4" else 3 * int(q) + 1, 1) - timedelta(days=1)
        return start.isoformat(), end.isoformat()
    if by == "year":
        return f"{key}-01-01", f"{key}-12-31"
    y, m = (int(x) for x in key.split("-"))
    end = date(y + (m == 12), 1 if m == 12 else m + 1, 1) - timedelta(days=1)
    return f"{y}-{m:02d}-01", end.isoformat()


def in_range(day: str | None, start: str | None, end: str | None) -> bool:
    return day is not None and (start is None or day >= start) and (end is None or day <= end)


def wilson(k: int, n: int, z: float = 1.96) -> tuple[float | None, float | None]:
    if n <= 0:
        return None, None
    p = k / n
    den = 1 + z * z / n
    mid = (p + z * z / (2 * n)) / den
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / den
    return max(0.0, mid - half), min(1.0, mid + half)


def scorecard(trades: list[dict[str, Any]]) -> dict[str, Any]:
    """Results of closed trades: counts, win rate with its range, totals, averages, profit factor."""
    closed = [t for t in trades if t["status"] == "CLOSED"]
    wins = [t["realized_pnl"] for t in closed if t["result"] == "WIN"]
    losses = [t["realized_pnl"] for t in closed if t["result"] == "LOSS"]
    n = len(closed)
    lo, hi = wilson(len(wins), n)
    gross_win, gross_loss = sum(wins), -sum(losses)
    return {"trades": n, "wins": len(wins), "losses": len(losses), "flat": n - len(wins) - len(losses),
            "win_rate": len(wins) / n if n else None, "win_rate_low": lo, "win_rate_high": hi,
            "net": sum(t["realized_pnl"] for t in closed) if n else None,
            "average": sum(t["realized_pnl"] for t in closed) / n if n else None,
            "average_win": gross_win / len(wins) if wins else None,
            "average_loss": -gross_loss / len(losses) if losses else None,
            "profit_factor": gross_win / gross_loss if gross_loss > EPS else None,
            "largest_win": max(wins) if wins else None, "largest_loss": min(losses) if losses else None,
            "enough": n >= MIN_PATTERN_TRADES}


def risk_of(trades: list[dict[str, Any]], today: date) -> dict[str, Any]:
    """Risk carried: the total and largest measured risk at the open, dollar-days at risk, unlimited and
    unmeasured counts (never added in as zero), the worst loss and what share of its risk it was."""
    measured = [t for t in trades if t["risk"]]
    worst = min((t for t in trades if t["result"] == "LOSS"), key=lambda t: t["realized_pnl"], default=None)
    return {"measured": len(measured), "of": len(trades),
            "unlimited": sum(1 for t in trades if t["unlimited"]),
            "not_measured": sum(1 for t in trades if not t["risk"] and not t["unlimited"]),
            "total_at_open": sum(t["risk"] for t in measured) if measured else None,
            "largest": max((t["risk"] for t in measured), default=None),
            "dollar_days": sum(t["risk"] * max(1, t["days_held"]) for t in measured) if measured else None,
            "worst_loss": worst["realized_pnl"] if worst else None, "worst_loss_trade": worst["id"] if worst else None,
            "worst_loss_share_of_risk": (abs(worst["realized_pnl"]) / worst["risk"]) if worst and worst["risk"] else None}


def exposure_series(trades: list[dict[str, Any]], start: str, end: str) -> list[dict[str, Any]]:
    """For each day in [start, end]: the measured risk of everything open that day, and how many open trades
    had unlimited or unmeasured risk (counted, not added)."""
    d0, d1 = date.fromisoformat(start), date.fromisoformat(end)
    if d1 < d0 or (d1 - d0).days > 3660:
        return []
    n = (d1 - d0).days + 1
    risk = [0.0] * n
    unl = [0] * n
    unm = [0] * n
    count = [0] * n
    for t in trades:
        a = max(date.fromisoformat(t["open_date"]), d0)
        b = min(date.fromisoformat(t["close_date"]) if t["close_date"] else d1, d1)
        if b < a:
            continue
        for i in range((a - d0).days, (b - d0).days + 1):
            count[i] += 1
            if t["risk"]:
                risk[i] += t["risk"]
            elif t["unlimited"]:
                unl[i] += 1
            else:
                unm[i] += 1
    return [{"date": (d0 + timedelta(days=i)).isoformat(), "risk": risk[i], "open": count[i], "unlimited": unl[i],
             "not_measured": unm[i]} for i in range(n) if (d0 + timedelta(days=i)).weekday() < 5]


# --- patterns -------------------------------------------------------------------------------------------

def _bucket(v: float | None, edges: list[tuple[float, str]], top: str) -> str | None:
    if v is None:
        return None
    for edge, name in edges:
        if v <= edge:
            return name
    return top


FEATURES: list[tuple[str, str, Callable[[dict[str, Any]], str | None]]] = [
    ("strategy", "Strategy", lambda t: t["strategy"]),
    ("held", "How long it was held", lambda t: "Same day" if t["days_held"] == 0 else _bucket(
        t["days_held"], [(3, "1 to 3 days"), (10, "4 to 10 days"), (30, "11 to 30 days")], "Over 30 days")),
    ("dte", "Days to expiration when opened", lambda t: _bucket(
        t["dte_at_open"], [(2, "0 to 2 days"), (7, "3 to 7 days"), (30, "8 to 30 days"), (60, "31 to 60 days")], "Over 60 days")),
    ("ended", "How the option trade ended", lambda t: t["ended"] if t["ended"] and t["ended"] != "Sold" else None),
    ("moneyness", "Where the strike was when opened (one-leg options)", lambda t: None if t["moneyness_at_open"] is None else
        ("Out of the money by over 5%" if t["moneyness_at_open"] > 0.05 else
         "In the money by over 5%" if t["moneyness_at_open"] < -0.05 else "Within 5% of the stock price")),
    ("size", "Size against your usual risk", lambda t: None if t["size_vs_usual"] is None else
        ("Under half your usual" if t["size_vs_usual"] < 0.5 else "Over twice your usual" if t["size_vs_usual"] > 2 else "About your usual")),
    ("weekday", "Day it was opened", lambda t: WEEKDAYS[date.fromisoformat(t["open_date"]).weekday()]),
    ("after_loss", "Opened soon after a losing trade", lambda t: "Within 2 days of a loss" if t["after_loss"] else "Not just after a loss"),
    ("familiarity", "How often you trade the stock", lambda t: "Stocks you traded 5 or more times" if t["symbol_trades"] >= 5
        else "Stocks you traded fewer than 5 times"),
]


def patterns(trades: list[dict[str, Any]], min_trades: int = MIN_PATTERN_TRADES) -> dict[str, Any]:
    """How each kind of trade did against your average trade. `edge` is the total result less what the same
    number of average trades would have made: positive did better than usual, negative worse."""
    closed = [t for t in trades if t["status"] == "CLOSED"]
    overall = scorecard(closed)
    avg = overall["average"]
    table = []
    for key, title, fn in FEATURES:
        groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for t in closed:
            v = fn(t)
            if v is not None:
                groups[v].append(t)
        for value, ts in groups.items():
            sc = scorecard(ts)
            edge = sc["net"] - avg * sc["trades"] if avg is not None and sc["net"] is not None else None
            table.append({"feature": key, "title": title, "value": value, **sc, "edge": edge,
                          "per_trade_vs_average": (sc["average"] - avg) if avg is not None and sc["average"] is not None else None,
                          "trade_ids": [t["id"] for t in ts]})
    ranked = [r for r in table if r["trades"] >= min_trades and r["edge"] is not None and len(closed) > r["trades"]]
    success = sorted((r for r in ranked if r["edge"] > EPS), key=lambda r: -r["edge"])[:6]
    failure = sorted((r for r in ranked if r["edge"] < -EPS), key=lambda r: r["edge"])[:6]
    return {"overall": overall, "success": success, "failure": failure, "table": table, "min_trades": min_trades}


# --- the whole analysis for a range ----------------------------------------------------------------

def analyse(book: Book, trades: list[dict[str, Any]], today: date, start: str | None = None, end: str | None = None,
            by: str = "month", basis: str = "close") -> dict[str, Any]:
    if by not in ("week", "month", "quarter", "year"):
        raise ValueError("Group by week, month, quarter or year")
    if basis not in ("close", "open"):
        raise ValueError("The date range applies to when trades were opened or closed")
    first, last = book.first_date, book.last_date
    start = start or first
    end = end or (today.isoformat() if last else None)
    if start and end and start > end:
        raise ValueError("The start date is after the end date")

    def inside(t: dict[str, Any]) -> bool:
        return in_range(t["close_date"] if basis == "close" else t["open_date"], start, end)

    chosen = [t for t in trades if inside(t)]
    closed = [t for t in chosen if t["status"] == "CLOSED"]
    pieces = [(p, g) for g in book.groups for p in g.pieces if in_range(p["close_date"], start, end)]
    realized = sum(p["pnl"] for p, _ in pieces) if pieces else None

    # Periods: realized results by close date of each closed piece; trades counted when they finish.
    per: dict[str, dict[str, Any]] = {}

    def slot(key: str) -> dict[str, Any]:
        if key not in per:
            lo, hi = period_bounds(key, by)
            per[key] = {"key": key, "label": period_label(key, by), "start": lo, "end": hi, "realized": 0.0,
                        "trades": [], "opened": [], "by_strategy": defaultdict(lambda: {"trades": 0, "net": 0.0})}
        return per[key]

    for p, _ in pieces:
        slot(period_key(p["close_date"], by))["realized"] += p["pnl"]
    for t in closed:
        s = slot(period_key(t["close_date"], by))
        s["trades"].append(t)
        s["by_strategy"][t["strategy"]]["trades"] += 1
        s["by_strategy"][t["strategy"]]["net"] += t["realized_pnl"]
    for t in trades:
        if in_range(t["open_date"], start, end):
            slot(period_key(t["open_date"], by))["opened"].append(t)
    periods = []
    for key in sorted(per):
        s = per[key]
        sc = scorecard(s["trades"])
        opened = s["opened"]
        periods.append({"key": key, "label": s["label"], "start": s["start"], "end": s["end"], "realized": s["realized"],
                        **{k: sc[k] for k in ("trades", "wins", "losses", "win_rate", "profit_factor", "largest_loss")},
                        "opened": len(opened), "risk_opened": sum(t["risk"] for t in opened if t["risk"]) or None,
                        "risk_opened_measured": sum(1 for t in opened if t["risk"]),
                        "unlimited_opened": sum(1 for t in opened if t["unlimited"]),
                        "by_strategy": {k: v for k, v in sorted(s["by_strategy"].items(), key=lambda kv: kv[1]["net"])},
                        "trade_ids": [t["id"] for t in s["trades"]]})

    # Strategies: results and the risk each carried.
    by_strat: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for t in chosen:
        by_strat[t["strategy"]].append(t)
    strategies = []
    for name, ts in by_strat.items():
        sc, rk = scorecard(ts), risk_of(ts, today)
        total = rk["total_at_open"]
        strategies.append({"strategy": name, **sc, "risk": rk,
                           "return_on_risk": (sc["net"] / sum(t["risk"] for t in ts if t["risk"] and t["status"] == "CLOSED"))
                           if sc["net"] is not None and any(t["risk"] and t["status"] == "CLOSED" for t in ts) else None,
                           "open": sum(1 for t in ts if t["status"] != "CLOSED"),
                           "first": min(t["open_date"] for t in ts), "last": max(t["open_date"] for t in ts),
                           "trade_ids": [t["id"] for t in ts], "_total": total or 0.0})
    # Most exposure first: unlimited risk is its own, highest class; then dollar-days at risk, then total.
    strategies.sort(key=lambda s: (-(s["risk"]["unlimited"] > 0), -(s["risk"]["dollar_days"] or 0), -s["_total"]))
    for s in strategies:
        s.pop("_total")

    daily: dict[str, float] = defaultdict(float)
    for p, _ in pieces:
        daily[p["close_date"]] += p["pnl"]
    curve, total = [], 0.0
    for day in sorted(daily):
        total += daily[day]
        curve.append({"date": day, "cumulative": total})
    peak = 0.0
    dd = 0.0
    dd_from = dd_to = None
    run_peak_date = None
    for pt in curve:
        if pt["cumulative"] >= peak:
            peak, run_peak_date = pt["cumulative"], pt["date"]
        drop = peak - pt["cumulative"]
        if drop > dd:
            dd, dd_from, dd_to = drop, run_peak_date, pt["date"]
    exposure = exposure_series([t for t in trades if t["open_date"] <= (end or today.isoformat())
                                and (t["close_date"] or "9999") >= (start or "0000")], start, end) if start and end else []
    peak_exp = max(exposure, key=lambda x: x["risk"], default=None)

    pats = patterns(closed)
    unmatched = [u for u in book.unmatched if in_range(u["date"], start, end)]
    notes = list(book.notes)
    if book.unmatched:
        notes.append(f"{len(book.unmatched)} closing row{'s' if len(book.unmatched) != 1 else ''} had nothing open to close: "
                     f"the trade{'s were' if len(book.unmatched) != 1 else ' was'} opened before the imported history "
                     f"starts ({first}). Their results are left out, not counted as zero. Import an earlier report to "
                     "include them.")
    if any(t["estimated"] for t in chosen):
        notes.append("Some rows had no amount, so their cash was worked out from price times size (fees left out).")
    return {"range": {"start": start, "end": end, "by": by, "basis": basis, "first_date": first, "last_date": last},
            "summary": {**scorecard(chosen), "realized": realized, "pieces": len(pieces),
                        "open_in_range": sum(1 for t in chosen if t["status"] != "CLOSED"),
                        "open_now": sum(1 for t in trades if t["status"] != "CLOSED"),
                        "risk": risk_of(chosen, today),
                        "max_drawdown": dd if curve else None, "drawdown_from": dd_from, "drawdown_to": dd_to,
                        "peak_exposure": peak_exp},
            "periods": periods, "strategies": strategies, "patterns": pats, "curve": curve, "exposure": exposure,
            "trades": [{k: v for k, v in t.items() if k not in ("legs", "closes", "row_ids", "open_seq")} for t in chosen],
            "unmatched": unmatched, "unmatched_total": len(book.unmatched), "notes": notes}


def open_holdings(book: Book) -> list[dict[str, Any]]:
    """What the history says is still open: per stock or contract, side, size and the cash it took."""
    agg: dict[tuple, dict[str, Any]] = {}
    for lot in book.open_lots:
        k = (lot.key, lot.side)
        a = agg.setdefault(k, {"key": lot.key, "side": lot.side, "qty": 0.0, "cash": 0.0, "since": lot.open_date})
        a["qty"] += lot.qty
        a["cash"] += lot.qty * lot.unit_cash
        a["since"] = min(a["since"], lot.open_date)
    out = []
    for a in agg.values():
        key = a["key"]
        item = {"kind": key[0], "symbol": key[1], "contract": _contract(key), "side": "long" if a["side"] > 0 else "short",
                "quantity": a["qty"], "average": a["cash"] / a["qty"] if a["qty"] else None, "cash": a["cash"], "since": a["since"]}
        if key[0] == "OPTION":
            item.update({"option_type": key[2], "strike": key[3], "expiration": key[4]})
        out.append(item)
    return sorted(out, key=lambda x: (x["symbol"], x["contract"]))


__all__ = ["rebuild", "summarize", "analyse", "classify", "open_holdings", "patterns", "scorecard", "risk_of",
           "exposure_series", "period_key", "period_bounds", "wilson", "Book", "Group"]
