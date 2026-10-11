"""Your trade history from Robinhood, read-only (2.6).

**Where it comes from.** Robinhood's *Account activity report*: a CSV with one row per transaction and
the columns "Activity Date", "Process Date", "Settle Date", "Instrument", "Description", "Trans Code",
"Quantity", "Price" and "Amount". Amounts are dollars with a sign: "($848.27)" is money paid, "$673.72"
money received. Option rows describe the contract as "IWM 6/12/2026 Put $232.00"; an expiration as
"Option Expiration for LULU 4/16/2026 Call $310.00". The report lists the newest rows first and gives
dates without times. (The Robinhood connection's order history, when switched on, arrives in the same
shape: see robinhood_account.py.)

**What is kept.** Rows that trade or change a holding:

    Buy, Sell                 shares bought or sold
    BTO, STC                  an option bought to open, sold to close
    STO, BTC                  an option sold to open, bought to close
    OEXP                      an option expired
    OASGN                     an option you sold was assigned (the shares appear as their own Buy or Sell)
    OEXCS, OEXER              an option you held was exercised (likewise)
    SPL                       a stock split: shares added or removed at no cost

Everything else (deposits, transfers, dividends, interest, fees, subscription charges) is counted at
import and not kept. A trade row that cannot be read is reported with its line, never guessed at.

**Duplicates.** A row is identified by its own content plus how many identical rows came before it in the
same file, so importing overlapping reports keeps each real row once, and two genuinely identical fills
in one report are both kept.

Nothing in this module sends anything anywhere or can change an account.
"""

from __future__ import annotations

import csv
import hashlib
import io
import json
import random
import re
from collections import Counter
from dataclasses import dataclass, field
from types import SimpleNamespace
from datetime import date, datetime, timedelta, timezone
from typing import Any, Iterable

REQUIRED = ("Activity Date", "Instrument", "Description", "Trans Code", "Quantity", "Price", "Amount")
OPTIONAL = ("Process Date", "Settle Date")
STOCK_CODES = {"BUY": "Buy", "SELL": "Sell"}
OPTION_CODES = {"BTO", "STC", "STO", "BTC"}
OPTION_EVENTS = {"OEXP", "OASGN", "OEXCS", "OEXER"}
SPLIT_CODES = {"SPL"}
KEPT = set(STOCK_CODES) | OPTION_CODES | OPTION_EVENTS | SPLIT_CODES
# Plain-words names for the rows that are counted but not kept.
IGNORED_WORDS = {"ACH": "bank transfers", "RTP": "instant transfers", "CDIV": "cash dividends", "MDIV": "dividends",
                 "QDIV": "dividends", "DIV": "dividends", "INT": "interest", "GOLD": "Gold subscription fees",
                 "SLIP": "stock lending income", "DTAX": "dividend tax", "AFEE": "fees", "DFEE": "fees",
                 "MINT": "margin interest", "XENT": "transfers", "ACATI": "account transfers in",
                 "ACATO": "account transfers out", "JNLC": "journal entries", "JNLS": "journal entries",
                 "REC": "receipts", "SOFF": "spin-offs", "MRGS": "mergers", "SXCH": "symbol changes",
                 "CONV": "conversions", "BCXL": "cancelled trades", "FUTSWP": "futures sweeps"}
# Corporate actions that change a holding in ways this app does not model: shown, never silently dropped.
UNMODELLED_ACTIONS = {"SOFF", "MRGS", "SXCH", "CONV", "ACATI", "ACATO", "REC", "BCXL"}
MAX_TEXT = 16_000_000
MAX_ROWS = 200_000

OPTION_RE = re.compile(
    r"(?:option\s+(?:expiration|assignment|exercise)\s+for\s+)?"
    r"(?P<sym>[A-Z][A-Z0-9.\-]{0,9})\s+(?P<m>\d{1,2})/(?P<d>\d{1,2})/(?P<y>\d{4})\s+"
    r"(?P<type>call|put)\s+\$(?P<strike>[\d,]+(?:\.\d+)?)", re.IGNORECASE)


class HistoryImportError(ValueError):
    pass


@dataclass
class Row:
    """One kept row, as read."""
    file_order: int
    activity_date: str
    code: str
    kind: str
    symbol: str | None
    description: str
    quantity: float | None
    price: float | None
    amount: float | None
    raw: dict[str, str]
    process_date: str | None = None
    settle_date: str | None = None
    option_type: str | None = None
    strike: float | None = None
    expiration: str | None = None
    short_flag: bool = False
    row_key: str = ""


@dataclass
class Parsed:
    rows: list[Row] = field(default_factory=list)
    read: int = 0
    ignored: Counter = field(default_factory=Counter)
    unreadable: list[dict[str, Any]] = field(default_factory=list)
    unmodelled: list[dict[str, Any]] = field(default_factory=list)


# --- reading values -------------------------------------------------------------------------------

def money(text: str | None) -> float | None:
    """"$1,234.56" -> 1234.56; "($848.27)" -> -848.27; "-$1.00" -> -1.0; "" -> None."""
    s = (text or "").strip()
    if not s:
        return None
    neg = s.startswith("(") and s.endswith(")")
    s = s.strip("()").strip()
    if s.startswith("-"):
        neg, s = True, s[1:].strip()
    s = s.replace("$", "").replace(",", "").strip()
    if not re.fullmatch(r"\d+(?:\.\d+)?|\.\d+", s):
        raise ValueError(f"not an amount: {text!r}")
    v = float(s)
    return -v if neg else v


def quantity(text: str | None) -> tuple[float | None, bool]:
    """"4" -> (4, False); "2S" -> (2, True); "0.810399" -> (0.810399, False); "" -> (None, False).
    The trailing S appears on some expiration rows; it is kept as a hint, not relied on alone."""
    s = (text or "").strip().replace(",", "")
    if not s:
        return None, False
    m = re.fullmatch(r"(\d+(?:\.\d+)?|\.\d+)\s*([A-Za-z]?)", s)
    if not m:
        raise ValueError(f"not a quantity: {text!r}")
    return float(m.group(1)), m.group(2).upper() == "S"


def us_date(text: str | None) -> str | None:
    s = (text or "").strip()
    if not s:
        return None
    for fmt in ("%m/%d/%Y", "%Y-%m-%d", "%m/%d/%y"):
        try:
            return datetime.strptime(s, fmt).date().isoformat()
        except ValueError:
            continue
    raise ValueError(f"not a date: {text!r}")


def option_terms(description: str) -> tuple[str, str, float, str] | None:
    """(underlying, CALL or PUT, strike, expiration ISO) from an option row's description, or None."""
    m = OPTION_RE.search(" ".join((description or "").split()))
    if not m:
        return None
    try:
        exp = date(int(m["y"]), int(m["m"]), int(m["d"])).isoformat()
    except ValueError:
        return None
    return m["sym"].upper(), m["type"].upper(), float(m["strike"].replace(",", "")), exp


def _key(values: Iterable[str], seen: Counter) -> str:
    base = hashlib.sha256("\x1f".join(v.strip() for v in values).encode()).hexdigest()
    seen[base] += 1
    return f"{base[:40]}#{seen[base]}"


# --- the report -----------------------------------------------------------------------------------

def parse_report(text: str) -> Parsed:
    """Read Robinhood's account activity report. Raises HistoryImportError when the file is not that report."""
    if not isinstance(text, str) or not text.strip():
        raise HistoryImportError("The file is empty.")
    if len(text) > MAX_TEXT:
        raise HistoryImportError(f"The file is larger than {MAX_TEXT // 1_000_000} MB. Generate the report for a "
                                 "shorter period and import the parts one after another.")
    text = text.lstrip("﻿")
    reader = csv.reader(io.StringIO(text, newline=""))
    try:
        header = [h.strip() for h in next(reader)]
    except StopIteration as exc:
        raise HistoryImportError("The file is empty.") from exc
    missing = [c for c in REQUIRED if c not in header]
    if missing:
        raise HistoryImportError("This does not look like Robinhood's account activity report: it has no "
                                 f"{', '.join(repr(c) for c in missing)} column{'s' if len(missing) > 1 else ''}. "
                                 "In Robinhood, open Reports and statements, choose Account activity report and "
                                 "download the CSV.")
    col = {name: header.index(name) for name in REQUIRED + OPTIONAL if name in header}
    out, seen = Parsed(), Counter()
    for line_no, cells in enumerate(reader, start=2):
        if not any(c.strip() for c in cells):
            continue
        need = max(col[c] for c in REQUIRED) + 1
        if len(cells) < need or not (cells[col["Activity Date"]].strip() or cells[col["Trans Code"]].strip()):
            # The report ends with a disclaimer line that is not a transaction.
            if len(cells) == 1 or not "".join(cells[:need - 1]).strip():
                continue
            out.unreadable.append({"line": line_no, "code": "", "reason": "the line has fewer columns than the header",
                                   "text": ", ".join(c.strip() for c in cells if c.strip())[:300]})
            continue
        if out.read >= MAX_ROWS:
            raise HistoryImportError(f"More than {MAX_ROWS:,} rows: import the report in parts.")
        out.read += 1
        raw = {name: (cells[col[name]] if col[name] < len(cells) else "") for name in REQUIRED + OPTIONAL if name in col}
        get = raw.get
        code_raw = get("Trans Code", "").strip()
        code = code_raw.upper()
        if code not in KEPT:
            out.ignored[code or "(blank)"] += 1
            if code in UNMODELLED_ACTIONS:
                out.unmodelled.append({"line": line_no, "code": code, "symbol": get("Instrument", "").strip() or None,
                                       "date": get("Activity Date", "").strip(), "description": " ".join(get("Description", "").split())[:160]})
            continue
        try:
            row = _row(code, raw, out.read)
        except ValueError as exc:
            out.unreadable.append({"line": line_no, "code": code_raw, "reason": str(exc),
                                   "text": ", ".join(f"{k}: {v.strip()}" for k, v in raw.items() if v.strip())[:300]})
            continue
        row.row_key = _key([raw.get(c, "") for c in REQUIRED + OPTIONAL], seen)
        out.rows.append(row)
    return out


def _row(code: str, raw: dict[str, str], order: int) -> Row:
    day = us_date(raw.get("Activity Date"))
    if not day:
        raise ValueError("no activity date")
    qty, short = quantity(raw.get("Quantity"))
    price, amount = money(raw.get("Price")), money(raw.get("Amount"))
    desc = " ".join((raw.get("Description") or "").split())
    symbol = (raw.get("Instrument") or "").strip().upper() or None
    row = Row(file_order=order, activity_date=day, code=STOCK_CODES.get(code, code), kind="STOCK", symbol=symbol,
              description=desc, quantity=qty, price=price, amount=amount, raw=raw, short_flag=short,
              process_date=us_date(raw.get("Process Date")), settle_date=us_date(raw.get("Settle Date")))
    if code in OPTION_CODES or code in OPTION_EVENTS:
        terms = option_terms(desc)
        if terms is None:
            raise ValueError("an option row whose description does not name the contract "
                             "(expected like 'AAPL 6/21/2024 Call $190.00')")
        row.kind = "OPTION"
        row.symbol, row.option_type, row.strike, row.expiration = terms
        if code in OPTION_CODES and (qty is None or qty <= 0):
            raise ValueError("an option trade without a quantity")
        if code in OPTION_CODES and amount is None and price is None:
            raise ValueError("an option trade with neither a price nor an amount")
        if code in OPTION_EVENTS and qty is None:
            raise ValueError("an option event without a quantity")
    elif code in STOCK_CODES:
        if not symbol:
            raise ValueError("a share trade without a ticker")
        if qty is None or qty <= 0:
            raise ValueError("a share trade without a quantity")
        if amount is None and price is None:
            raise ValueError("a share trade with neither a price nor an amount")
    elif code in SPLIT_CODES:
        if not symbol or qty is None:
            raise ValueError("a split without a ticker or a quantity")
    return row


# --- storage --------------------------------------------------------------------------------------

def store(db: Any, parsed: Parsed, source: str, filename: str | None) -> dict[str, Any]:
    """Keep the parsed rows (each once) and record the import. Returns what happened, in numbers."""
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    dates = sorted(r.activity_date for r in parsed.rows)
    with db.tx() as cur:
        cur.execute("INSERT INTO broker_imports (source, filename, imported_at, rows_read) VALUES (?, ?, ?, ?)",
                    (source, (filename or "")[:200] or None, now, parsed.read))
        import_id = cur.lastrowid
        added = dup = 0
        for r in parsed.rows:
            cur.execute("""INSERT OR IGNORE INTO broker_activity (import_id, source, row_key, file_order, activity_date,
                           process_date, settle_date, code, symbol, description, kind, option_type, strike, expiration,
                           quantity, short_flag, price, amount, raw_json)
                           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                        (import_id, source, r.row_key, r.file_order, r.activity_date, r.process_date, r.settle_date,
                         r.code, r.symbol, r.description, r.kind, r.option_type, r.strike, r.expiration, r.quantity,
                         int(r.short_flag), r.price, r.amount, json.dumps(r.raw, sort_keys=True)))
            if cur.rowcount:
                added += 1
            else:
                dup += 1
        summary = {"ignored": [{"code": c, "rows": n, "what": IGNORED_WORDS.get(c, "other rows")}
                               for c, n in parsed.ignored.most_common()],
                   "unreadable": parsed.unreadable[:50], "unreadable_total": len(parsed.unreadable),
                   "unmodelled": parsed.unmodelled[:50], "unmodelled_total": len(parsed.unmodelled)}
        cur.execute("""UPDATE broker_imports SET rows_added = ?, rows_duplicate = ?, rows_ignored = ?, rows_unreadable = ?,
                       first_date = ?, last_date = ?, summary_json = ? WHERE id = ?""",
                    (added, dup, sum(parsed.ignored.values()), len(parsed.unreadable),
                     dates[0] if dates else None, dates[-1] if dates else None, json.dumps(summary), import_id))
    return {"import_id": import_id, "source": source, "filename": filename, "rows_read": parsed.read,
            "rows_added": added, "rows_duplicate": dup, "rows_ignored": sum(parsed.ignored.values()),
            "rows_unreadable": len(parsed.unreadable), "first_date": dates[0] if dates else None,
            "last_date": dates[-1] if dates else None, **summary}


def imports(db: Any) -> list[dict[str, Any]]:
    rows = db.query("""SELECT i.*, (SELECT COUNT(*) FROM broker_activity a WHERE a.import_id = i.id) AS rows_kept
                       FROM broker_imports i ORDER BY i.id DESC""")
    out = []
    for r in rows:
        d = dict(r)
        d.update(json.loads(d.pop("summary_json") or "{}"))
        out.append(d)
    return out


def remove_import(db: Any, import_id: int) -> dict[str, Any]:
    with db.tx() as cur:
        cur.execute("SELECT id FROM broker_imports WHERE id = ?", (import_id,))
        if cur.fetchone() is None:
            raise HistoryImportError(f"There is no import {import_id}.")
        cur.execute("DELETE FROM broker_activity WHERE import_id = ?", (import_id,))
        removed = cur.rowcount
        cur.execute("DELETE FROM broker_imports WHERE id = ?", (import_id,))
    return {"import_id": import_id, "rows_removed": removed}


def activity(db: Any) -> list[dict[str, Any]]:
    """Every kept row, oldest first; within one day in the order things happened (the report lists newest
    first, so a later line in the same file is earlier in the day)."""
    rows = db.query("SELECT * FROM broker_activity ORDER BY activity_date ASC, import_id ASC, file_order DESC")
    out = []
    for r in rows:
        d = dict(r)
        d["raw"] = json.loads(d.pop("raw_json"))
        d["short_flag"] = bool(d["short_flag"])
        out.append(d)
    return out


# --- a made-up report, for demo mode only ------------------------------------------------------------

SAMPLE_SYMBOLS = {"AAPL": 190.0, "MSFT": 410.0, "NVDA": 120.0, "AMD": 150.0, "TSLA": 240.0, "SPY": 540.0,
                  "QQQ": 470.0, "KO": 62.0, "JPM": 205.0, "META": 520.0}


def _money_text(v: float) -> str:
    return f"(${abs(v):,.2f})" if v < 0 else f"${v:,.2f}"


def sample_report(today: date, seed: int = 11, months: int = 18,
                  price_on: Any = None) -> str:
    """A made-up account activity report in Robinhood's layout, for learning the screen in demo mode. The
    trades and results are random; they say nothing about anyone's real account. `price_on(symbol, day)`,
    when given, sets each trade near that day's (demo) price, so the sample agrees with the demo charts."""
    rng = random.Random(seed)
    lines: list[tuple[date, list[str]]] = []
    start = today - timedelta(days=int(months * 30.4))

    def add(day: date, instrument: str, desc: str, code: str, qty: str, price: str, amount: str) -> None:
        d = f"{day.month}/{day.day}/{day.year}"
        lines.append((day, [d, d, d, instrument, desc, code, qty, price, amount]))

    def weekday(d: date) -> date:
        while d.weekday() >= 5:
            d += timedelta(days=1)
        return d

    def friday_after(d: date, days: int) -> date:
        e = d + timedelta(days=days)
        while e.weekday() != 4:
            e += timedelta(days=1)
        return e

    def opt(sym: str, exp: date, kind: str, strike: float) -> str:
        return f"{sym} {exp.month}/{exp.day}/{exp.year} {kind} ${strike:,.2f}"

    day = weekday(start)
    while day < today - timedelta(days=3):
        sym = rng.choice(list(SAMPLE_SYMBOLS))
        px = (price_on(sym, day.isoformat()) if price_on else None) or SAMPLE_SYMBOLS[sym] * (1 + rng.uniform(-0.15, 0.15))
        step = 5.0 if px > 100 else 1.0
        atm = round(px / step) * step
        roll = rng.random()
        hold = rng.choice([0, 1, 2, 3, 5, 8, 12, 20, 35])
        exit_day = weekday(day + timedelta(days=hold))
        if exit_day >= today:
            exit_day = None
        if roll < 0.22:                                   # shares
            qty = rng.choice([5, 10, 20, 50])
            move = rng.gauss(0.004 * hold, 0.03 + 0.006 * hold)
            add(day, sym, f"{sym} Inc\nCUSIP: 000000000", "Buy", str(qty), f"${px:,.2f}", _money_text(-qty * px))
            if exit_day:
                out_px = px * (1 + move)
                add(exit_day, sym, f"{sym} Inc\nCUSIP: 000000000", "Sell", str(qty), f"${out_px:,.2f}",
                    _money_text(qty * out_px - 0.02))
        elif roll < 0.62:                                 # a long call or put
            kind = "Call" if rng.random() < 0.65 else "Put"
            dte = rng.choice([1, 2, 4, 7, 14, 30, 45])
            exp = friday_after(day, dte)
            otm = rng.choice([0.0, 0.02, 0.05, 0.08, 0.12])
            strike = round(px * (1 + otm if kind == "Call" else 1 - otm) / step) * step
            prem = max(0.05, px * (0.012 + 0.03 * (dte / 45) ** 0.5) * (1 - 4 * otm))
            qty = rng.choice([1, 1, 2, 3, 5])
            add(day, sym, opt(sym, exp, kind, strike), "BTO", str(qty), f"${prem:,.2f}", _money_text(-qty * prem * 100 - 0.04 * qty))
            p_win = 0.48 - 1.6 * otm - (0.12 if dte <= 2 else 0) + (0.05 if hold <= 3 else 0)
            closing = exit_day if exit_day and exit_day <= exp else (exp if exp < today else None)
            if closing is None:
                pass
            elif closing == exp and rng.random() < 0.55:
                add(exp, sym, f"Option Expiration for {opt(sym, exp, kind, strike)}", "OEXP", f"{qty}S", "", "")
            else:
                out_prem = prem * (1 + rng.uniform(0.2, 1.6)) if rng.random() < p_win else prem * rng.uniform(0.05, 0.8)
                add(closing, sym, opt(sym, exp, kind, strike), "STC", str(qty), f"${out_prem:,.2f}",
                    _money_text(qty * out_prem * 100 - 0.04 * qty))
        elif roll < 0.77:                                 # a short put
            exp = friday_after(day, rng.choice([7, 14, 30, 45]))
            strike = round(px * rng.uniform(0.88, 0.97) / step) * step
            prem = max(0.1, px * rng.uniform(0.006, 0.02))
            qty = 1
            add(day, sym, opt(sym, exp, "Put", strike), "STO", str(qty), f"${prem:,.2f}", _money_text(qty * prem * 100 - 0.04))
            if exp < today:
                r = rng.random()
                if r < 0.62:
                    add(exp, sym, f"Option Expiration for {opt(sym, exp, 'Put', strike)}", "OEXP", f"{qty}S", "", "")
                elif r < 0.85:
                    back = prem * rng.uniform(0.2, 2.8)
                    close_day = weekday(min(exp, day + timedelta(days=hold or 3)))
                    add(close_day, sym, opt(sym, exp, "Put", strike), "BTC", str(qty), f"${back:,.2f}", _money_text(-qty * back * 100 - 0.04))
                else:
                    add(exp, sym, f"Option Assignment for {opt(sym, exp, 'Put', strike)}", "OASGN", f"{qty}S", "", "")
                    add(exp, sym, f"{sym} Inc\nCUSIP: 000000000", "Buy", str(100 * qty), f"${strike:,.2f}", _money_text(-100 * qty * strike))
                    sell_day = weekday(exp + timedelta(days=rng.choice([3, 10, 25])))
                    if sell_day < today:
                        out_px = strike * (1 + rng.gauss(-0.01, 0.05))
                        add(sell_day, sym, f"{sym} Inc\nCUSIP: 000000000", "Sell", str(100 * qty), f"${out_px:,.2f}",
                            _money_text(100 * qty * out_px - 0.05))
        elif roll < 0.92:                                 # a vertical spread
            kind = "Call" if rng.random() < 0.6 else "Put"
            exp = friday_after(day, rng.choice([7, 14, 30]))
            width = step * rng.choice([1, 2])
            long_k = atm
            short_k = atm + width if kind == "Call" else atm - width
            debit = width * rng.uniform(0.3, 0.6)
            qty = rng.choice([1, 2])
            long_p, short_p = debit * 1.6, debit * 0.6
            add(day, sym, opt(sym, exp, kind, long_k), "BTO", str(qty), f"${long_p:,.2f}", _money_text(-qty * long_p * 100 - 0.04 * qty))
            add(day, sym, opt(sym, exp, kind, short_k), "STO", str(qty), f"${short_p:,.2f}", _money_text(qty * short_p * 100 - 0.04 * qty))
            if exp < today:
                win = rng.random() < 0.45
                close_day = weekday(min(exp, day + timedelta(days=max(1, hold))))
                lp = long_p * (rng.uniform(1.2, 2.2) if win else rng.uniform(0.1, 0.7))
                sp = short_p * (rng.uniform(1.1, 1.9) if win else rng.uniform(0.05, 0.6))
                add(close_day, sym, opt(sym, exp, kind, long_k), "STC", str(qty), f"${lp:,.2f}", _money_text(qty * lp * 100 - 0.04 * qty))
                add(close_day, sym, opt(sym, exp, kind, short_k), "BTC", str(qty), f"${sp:,.2f}", _money_text(-qty * sp * 100 - 0.04 * qty))
        else:                                             # an iron condor
            exp = friday_after(day, rng.choice([14, 30, 45]))
            w = step * 2
            ks = [atm - 4 * step, atm - 4 * step - w, atm + 4 * step, atm + 4 * step + w]
            prices = [px * 0.008, px * 0.004, px * 0.008, px * 0.004]
            for (code, k, p, kind) in (("STO", ks[0], prices[0], "Put"), ("BTO", ks[1], prices[1], "Put"),
                                       ("STO", ks[2], prices[2], "Call"), ("BTO", ks[3], prices[3], "Call")):
                amt = p * 100 if code == "STO" else -p * 100
                add(day, sym, opt(sym, exp, kind, k), code, "1", f"${p:,.2f}", _money_text(amt - 0.04))
            if exp < today:
                if rng.random() < 0.6:
                    for k, kind in ((ks[0], "Put"), (ks[1], "Put"), (ks[2], "Call"), (ks[3], "Call")):
                        add(exp, sym, f"Option Expiration for {opt(sym, exp, kind, k)}", "OEXP", "1", "", "")
                else:
                    close_day = weekday(min(exp, day + timedelta(days=max(1, hold))))
                    loss = rng.uniform(0.5, 2.5)
                    for (code, k, p, kind) in (("BTC", ks[0], prices[0] * loss, "Put"), ("STC", ks[1], prices[1] * 0.5, "Put"),
                                               ("BTC", ks[2], prices[2] * 0.6, "Call"), ("STC", ks[3], prices[3] * 0.3, "Call")):
                        amt = p * 100 if code == "STC" else -p * 100
                        add(close_day, sym, opt(sym, exp, kind, k), code, "1", f"${p:,.2f}", _money_text(amt - 0.04))
        if rng.random() < 0.15:
            add(day, "", "ACH Deposit", "ACH", "", "", _money_text(500.0))
        day = weekday(day + timedelta(days=rng.choice([1, 1, 2, 3, 4])))
    # The report lists the newest rows first; rows of one day keep the reverse of the order they were added.
    order = sorted(range(len(lines)), key=lambda i: (lines[i][0], i), reverse=True)
    buf = io.StringIO()
    w = csv.writer(buf, quoting=csv.QUOTE_ALL, lineterminator="\n")
    w.writerow(["Activity Date", "Process Date", "Settle Date", "Instrument", "Description", "Trans Code", "Quantity",
                "Price", "Amount"])
    for i in order:
        w.writerow(lines[i][1])
    buf.write('\n"The data provided is for informational purposes only. This is a made-up sample report for the '
              'demo."\n')
    return buf.getvalue()


# --- the service behind /v2/history ----------------------------------------------------------------

class TradeHistory:
    """Imports, rebuilt trades and their analysis, kept in step with the stored rows."""

    MAX_PRICE_SYMBOLS = 80

    def __init__(self, svc: Any):
        self.svc = svc
        self._book: tuple[tuple, Any, list[dict[str, Any]]] | None = None
        self._closes: dict[str, Any] = {}

    def _version(self) -> tuple:
        r = self.svc.db.one("SELECT COUNT(*) AS n, COALESCE(MAX(id), 0) AS m FROM broker_activity")
        return (r["n"], r["m"])

    def _price_on(self, symbol: str, day: str) -> float | None:
        """The stock's close on `day` (or the last trading day before it), from the app's daily price history.
        None when there is no history for it: the pattern that needs it is then left out for that trade."""
        if symbol not in self._closes:
            if len(self._closes) >= self.MAX_PRICE_SYMBOLS:
                return None
            try:
                first = self._book[1].first_date if self._book else None
                years = 1.0
                if first:
                    years = min(10.0, max(1.0, (date.today() - date.fromisoformat(first)).days / 365.0 + 0.1))
                bars = self.svc.market.bars(symbol, years, complete=True)
                self._closes[symbol] = {d.strftime("%Y-%m-%d"): float(c) for d, c in zip(bars["date"], bars["close"])}
            except Exception:                         # noqa: BLE001 - no history: unknown, not an error
                self._closes[symbol] = None
        series = self._closes.get(symbol)
        if not series:
            return None
        if day in series:
            return series[day]
        earlier = [d for d in series if d <= day]
        return series[max(earlier)] if earlier else None

    def book(self, today: date | None = None) -> tuple[Any, list[dict[str, Any]]]:
        from .tradeanalysis import rebuild, summarize
        v = self._version()
        if self._book is None or self._book[0] != v:
            self._closes = {}
            book = rebuild(activity(self.svc.db))
            self._book = (v, book, [])
            trades = summarize(book, today or date.today(), self._price_on)
            self._book = (v, book, trades)
        return self._book[1], self._book[2]

    def import_report(self, text: str, filename: str | None, source: str = "robinhood_csv") -> dict[str, Any]:
        parsed = parse_report(text)
        if not parsed.rows and not parsed.read:
            raise HistoryImportError("The report has no rows.")
        out = store(self.svc.db, parsed, source, filename)
        self._book = None
        return out

    def load_sample(self) -> dict[str, Any]:
        if self.svc.market.info.mode != "demo":
            raise HistoryImportError("The sample report is for demo mode only, so made-up trades never mix with yours.")
        self._book = (None, SimpleNamespace(first_date=(date.today() - timedelta(days=600)).isoformat()), [])
        text = sample_report(date.today(), price_on=self._price_on)
        self._book = None
        return self.import_report(text, "sample report (made up).csv", source="sample")

    def analysis(self, start: str | None, end: str | None, by: str, basis: str) -> dict[str, Any]:
        from .tradeanalysis import analyse
        for v in (start, end):
            if v is not None:
                try:
                    date.fromisoformat(v)
                except ValueError as exc:
                    raise ValueError(f"Not a date: {v!r} (use YYYY-MM-DD)") from exc
        book, trades = self.book()
        if not book.groups and not book.unmatched:
            return {"empty": True, "imports": imports(self.svc.db), "data_source": self.svc.market.info.mode}
        out = analyse(book, trades, date.today(), start, end, by, basis)
        out["imports"] = imports(self.svc.db)
        out["sample"] = any(i["source"] == "sample" for i in out["imports"])
        out["data_source"] = self.svc.market.info.mode
        return out

    def trade(self, trade_id: int) -> dict[str, Any]:
        book, trades = self.book()
        t = next((x for x in trades if x["id"] == trade_id), None)
        if t is None:
            raise HistoryImportError(f"There is no trade {trade_id}.")
        order = {rid: n for n, rid in enumerate(book.rows)}         # the order things happened
        rows = sorted((book.rows[i] for i in t["row_ids"] if i in book.rows), key=lambda r: order[r["id"]])
        return {**{k: v for k, v in t.items() if k != "open_seq"},
                "rows": [{"id": r["id"], "date": r["activity_date"], "code": r["code"], "description": r["description"],
                          "quantity": r["quantity"], "price": r["price"], "amount": r["amount"], "raw": r["raw"],
                          "import_id": r["import_id"]} for r in rows]}

    def holdings(self) -> dict[str, Any]:
        from .tradeanalysis import open_holdings
        book, _ = self.book()
        items = open_holdings(book)
        for h in items:
            h["price"] = h["value"] = h["unrealized"] = None
            if h["kind"] == "STOCK":
                try:
                    px = float(self.svc.market.spot(h["symbol"]))
                except Exception:                     # noqa: BLE001 - no price: unknown, not zero
                    px = None
                if px:
                    sign = 1 if h["side"] == "long" else -1
                    h["price"], h["value"] = px, sign * px * h["quantity"]
                    h["unrealized"] = sign * (px * h["quantity"] - h["cash"])
        return {"holdings": items, "as_of_history": book.last_date, "first_date": book.first_date,
                "unmatched": len(book.unmatched), "data_source": self.svc.market.info.mode,
                "note": "From your imported history: correct only if the history reaches back to when each of these "
                        "was opened. Stock prices are the app's current quotes; option values are not estimated here "
                        "(price one with Price an option)."}


__all__ = ["parse_report", "store", "imports", "remove_import", "activity", "sample_report", "money", "quantity",
           "us_date", "option_terms", "HistoryImportError", "Parsed", "Row", "TradeHistory"]
