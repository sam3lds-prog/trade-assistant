"""Unified positions: stocks bought or sold short, and options bought, with every fill recorded.

A position is one idea in one instrument. Its executions (fills) are the source of truth;
everything else here (quantity still open, average prices, realised and unrealised profit
and loss, status) is worked out from them, so scaling in, partial exits and fees are always
consistent.

Opening and closing depend on the position's direction, never on the word "buy" alone:

    LONG   opened by BUY,                  closed by SELL
    SHORT  opened by SELL_SHORT (or SELL),  closed by BUY_TO_COVER (or BUY)

Average cost is used: each opening fill moves the cost of the open quantity to the weighted
average, and each closing fill realises (exit - cost) x quantity x multiplier, with the sign
flipped for a short. Fees are subtracted from realised profit when they are paid. When a
position is fully closed, its realised profit equals the cash it brought in minus the cash
it took, minus fees, exactly.
"""

from __future__ import annotations

import json
import math
import sqlite3
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from typing import Any, Iterable, Sequence

INSTRUMENTS = ("STOCK", "OPTION", "OPTION_SPREAD")
DIRECTIONS = ("LONG", "SHORT")
STATUSES = ("PLANNED", "OPEN", "PARTIALLY_CLOSED", "CLOSED", "CANCELLED")
SIDES = ("BUY", "SELL", "SELL_SHORT", "BUY_TO_COVER")
SOURCES = ("MANUAL", "ROBINHOOD", "IMPORT")
SNAPSHOT_TYPES = ("PLAN", "ENTRY", "EXIT", "REVIEW")
OPENING = {"LONG": ("BUY",), "SHORT": ("SELL_SHORT", "SELL")}
CLOSING = {"LONG": ("SELL",), "SHORT": ("BUY_TO_COVER", "BUY")}
OPTION_MULTIPLIER = 100
EPS = 1e-9


class PositionError(ValueError):
    """A request about a position that cannot be carried out; the message is shown to the user."""


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def parse_time(value: str | None) -> str:
    """An ISO timestamp in UTC. Empty means now; a plain date means that day's close in New York."""
    if not value:
        return now_iso()
    text = str(value).strip()
    try:
        if len(text) == 10:
            from .optionmath import NY
            day = datetime.fromisoformat(text).date()
            moment = datetime.combine(day, datetime.min.time().replace(hour=16), tzinfo=NY)
            if day == datetime.now(NY).date() and moment > datetime.now(NY):
                moment = datetime.now(NY).replace(microsecond=0)     # today, before the close: now
        else:
            moment = datetime.fromisoformat(text.replace("Z", "+00:00"))
            if moment.tzinfo is None:
                from .optionmath import NY
                moment = moment.replace(tzinfo=NY)
    except ValueError as exc:
        raise PositionError(f"Times must look like 2026-10-06 or 2026-10-06T10:31:00, got {value!r}") from exc
    if moment.astimezone(timezone.utc) > datetime.now(timezone.utc).replace(microsecond=0):
        raise PositionError("An execution cannot be in the future")
    return moment.astimezone(timezone.utc).isoformat(timespec="seconds")


# --- accounting ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Fill:
    side: str
    quantity: float
    price: float
    fees: float = 0.0
    executed_at: str = ""
    id: int | None = None


@dataclass
class Ledger:
    direction: str
    multiplier: float
    open_quantity: float = 0.0
    cost_basis: float | None = None            # average cost of the quantity still open
    average_entry_price: float | None = None   # weighted average of every opening fill
    average_exit_price: float | None = None    # weighted average of every closing fill
    opened_quantity: float = 0.0
    closed_quantity: float = 0.0
    max_open_quantity: float = 0.0
    gross_realized: float = 0.0
    fees: float = 0.0
    realized_pnl: float = 0.0                  # gross realised minus every fee paid so far
    opened_at: str | None = None
    closed_at: str | None = None
    fills: int = 0

    @property
    def sign(self) -> int:
        return 1 if self.direction == "LONG" else -1

    def status(self, cancelled: bool = False) -> str:
        if cancelled and self.fills == 0:
            return "CANCELLED"
        if self.fills == 0:
            return "PLANNED"
        if self.open_quantity > EPS:
            return "PARTIALLY_CLOSED" if self.closed_quantity > EPS else "OPEN"
        return "CLOSED"

    def unrealized(self, mark: float | None) -> float | None:
        if self.open_quantity <= EPS:
            return 0.0
        if mark is None or self.cost_basis is None:
            return None
        return self.sign * (mark - self.cost_basis) * self.open_quantity * self.multiplier

    def market_value(self, mark: float | None) -> float | None:
        """Signed value of the open quantity: positive for long, negative for short."""
        if self.open_quantity <= EPS:
            return 0.0
        return None if mark is None else self.sign * mark * self.open_quantity * self.multiplier


def classify(direction: str, side: str) -> str:
    if side in OPENING[direction]:
        return "OPEN"
    if side in CLOSING[direction]:
        return "CLOSE"
    want = " or ".join(OPENING[direction] + CLOSING[direction])
    raise PositionError(f"A {direction.lower()} position takes {want}, not {side}")


def account(direction: str, multiplier: float, fills: Iterable[Fill]) -> Ledger:
    """Replay fills in order and return the position's accounts. Raises PositionError if a fill
    would close more than is open."""
    if direction not in DIRECTIONS:
        raise PositionError("direction must be LONG or SHORT")
    led = Ledger(direction=direction, multiplier=multiplier)
    open_cost = 0.0                       # sum of price x quantity for the open quantity, at average cost
    entry_value = exit_value = 0.0
    for f in fills:
        if not (f.quantity > 0 and math.isfinite(f.quantity)):
            raise PositionError("Every execution needs a quantity above zero")
        if not (f.price >= 0 and math.isfinite(f.price)) or f.fees < 0:
            raise PositionError("Prices cannot be negative, and nor can fees")
        kind = classify(direction, f.side)
        led.fills += 1
        led.fees += f.fees
        if kind == "OPEN":
            if led.open_quantity <= EPS and led.opened_at is None:
                led.opened_at = f.executed_at or None
            open_cost += f.price * f.quantity
            led.open_quantity += f.quantity
            led.opened_quantity += f.quantity
            entry_value += f.price * f.quantity
            led.cost_basis = open_cost / led.open_quantity
            led.max_open_quantity = max(led.max_open_quantity, led.open_quantity)
            led.closed_at = None
        else:
            if f.quantity > led.open_quantity + EPS:
                have = f"{led.open_quantity:g}" if led.open_quantity > EPS else "nothing"
                raise PositionError(f"That closes {f.quantity:g} but only {have} is open at that time")
            basis = led.cost_basis if led.cost_basis is not None else 0.0
            led.gross_realized += led.sign * (f.price - basis) * f.quantity * multiplier
            led.open_quantity -= f.quantity
            led.closed_quantity += f.quantity
            exit_value += f.price * f.quantity
            open_cost = basis * led.open_quantity
            if led.open_quantity <= EPS:
                led.open_quantity = 0.0
                open_cost = 0.0
                led.closed_at = f.executed_at or None
    led.realized_pnl = led.gross_realized - led.fees
    led.average_entry_price = entry_value / led.opened_quantity if led.opened_quantity > EPS else None
    led.average_exit_price = exit_value / led.closed_quantity if led.closed_quantity > EPS else None
    if led.open_quantity <= EPS:
        led.cost_basis = None if led.fills == 0 else led.cost_basis
    return led


def initial_risk(pos: dict[str, Any], led: Ledger) -> float | None:
    """Dollars at risk when the position was opened, for R multiples.

    Uses, in order: the risk amount entered; the risk per unit entered; the distance from the
    average entry to the invalidation price; for a bought option, the premium paid."""
    # The size actually held at most, once there are fills; the planned size before that.
    size = led.max_open_quantity if led.fills else float(pos.get("quantity") or 0)
    mult = float(pos.get("multiplier") or 1)
    if pos.get("risk_amount"):
        return float(pos["risk_amount"])
    if pos.get("initial_risk_per_unit"):
        return float(pos["initial_risk_per_unit"]) * size * mult
    entry = led.average_entry_price
    inv = pos.get("invalidation_price")
    if pos.get("instrument_type") == "STOCK" and entry and inv:
        distance = (entry - inv) if pos["direction"] == "LONG" else (inv - entry)
        return distance * size * mult if distance > 0 else None
    if pos.get("instrument_type") == "OPTION" and pos["direction"] == "LONG" and entry:
        return entry * size * mult
    return None


# --- storage ----------------------------------------------------------------------------------

def _fills(cur: sqlite3.Cursor, position_id: int) -> list[Fill]:
    rows = cur.execute("SELECT id, side, quantity, price, fees, executed_at FROM executions WHERE position_id = ? "
                       "ORDER BY executed_at, id", (position_id,)).fetchall()
    return [Fill(r[1], r[2], r[3], r[4], r[5], r[0]) for r in rows]


def _row(cur: sqlite3.Cursor, position_id: int) -> dict[str, Any]:
    cur.execute("SELECT * FROM positions WHERE id = ?", (position_id,))
    row = cur.fetchone()
    if row is None:
        raise PositionError(f"No position {position_id}")
    names = [d[0] for d in cur.description]
    return dict(zip(names, row))


def recompute(cur: sqlite3.Cursor, position_id: int) -> Ledger:
    """Recalculate a position's stored figures from its executions."""
    pos = _row(cur, position_id)
    led = account(pos["direction"], pos["multiplier"], _fills(cur, position_id))
    status = led.status(cancelled=pos["status"] == "CANCELLED")
    cur.execute("UPDATE positions SET status = ?, open_quantity = ?, average_entry_price = ?, average_exit_price = ?, "
                "realized_pnl = ?, fees = ?, opened_at = ?, closed_at = ?, updated_at = ? WHERE id = ?",
                (status, led.open_quantity, led.average_entry_price, led.average_exit_price, led.realized_pnl,
                 led.fees, led.opened_at, led.closed_at if status == "CLOSED" else None, now_iso(), position_id))
    return led


@dataclass
class OptionLeg:
    option_type: str          # CALL or PUT
    strike: float
    expiration: str
    option_symbol: str = ""
    ratio: int = 1
    multiplier: float = OPTION_MULTIPLIER


def create(cur: sqlite3.Cursor, *, symbol: str, instrument_type: str, direction: str, quantity: float,
           thesis: str = "", setup_type: str = "", intended_horizon: int | None = None,
           invalidation_price: float | None = None, target_1: float | None = None, target_2: float | None = None,
           risk_amount: float | None = None, initial_risk_per_unit: float | None = None,
           option: OptionLeg | None = None, data_source: str = "", notes: str = "",
           legacy_trade_id: int | None = None, created_at: str | None = None) -> int:
    instrument_type, direction = instrument_type.upper(), direction.upper()
    if instrument_type not in INSTRUMENTS:
        raise PositionError("instrument_type must be STOCK or OPTION")
    if instrument_type == "OPTION_SPREAD":
        raise PositionError("Option spreads are not supported yet; record each leg as its own position for now")
    if direction not in DIRECTIONS:
        raise PositionError("direction must be LONG or SHORT")
    if not (quantity and quantity > 0 and math.isfinite(quantity)):
        raise PositionError("quantity must be above zero")
    if instrument_type == "OPTION":
        if option is None:
            raise PositionError("An option position needs its type, strike and expiration")
        if direction == "SHORT":
            raise PositionError("Selling options to open is not supported yet: only bought calls and puts")
        if option.option_type.upper() not in ("CALL", "PUT"):
            raise PositionError("option_type must be CALL or PUT")
        if float(quantity) != int(quantity):
            raise PositionError("Options trade in whole contracts")
    for name, value in (("invalidation_price", invalidation_price), ("target_1", target_1), ("target_2", target_2),
                        ("risk_amount", risk_amount), ("initial_risk_per_unit", initial_risk_per_unit)):
        if value is not None and not (math.isfinite(value) and value > 0):
            raise PositionError(f"{name} must be a positive number")
    mult = option.multiplier if instrument_type == "OPTION" and option else 1.0
    stamp = created_at or now_iso()
    cur.execute(
        "INSERT INTO positions (symbol, instrument_type, direction, status, quantity, multiplier, thesis, setup_type, "
        "intended_horizon, invalidation_price, target_1, target_2, risk_amount, initial_risk_per_unit, data_source, "
        "legacy_trade_id, notes, created_at, updated_at) VALUES (?, ?, ?, 'PLANNED', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (symbol.upper(), instrument_type, direction, float(quantity), mult, thesis.strip(), setup_type.strip(),
         intended_horizon, invalidation_price, target_1, target_2, risk_amount, initial_risk_per_unit, data_source,
         legacy_trade_id, notes.strip(), stamp, stamp))
    pid = int(cur.lastrowid)
    if instrument_type == "OPTION" and option is not None:
        cur.execute("INSERT INTO option_contracts (position_id, leg, underlying_symbol, option_symbol, option_type, strike, "
                    "expiration, multiplier, ratio) VALUES (?, 1, ?, ?, ?, ?, ?, ?, ?)",
                    (pid, symbol.upper(), option.option_symbol, option.option_type.upper(), float(option.strike),
                     option.expiration, option.multiplier, option.ratio))
    return pid


def add_execution(cur: sqlite3.Cursor, position_id: int, *, side: str, quantity: float, price: float,
                  fees: float = 0.0, executed_at: str | None = None, source: str = "MANUAL", note: str = "") -> Ledger:
    pos = _row(cur, position_id)
    if pos["status"] == "CANCELLED":
        raise PositionError("This position was cancelled")
    side, source = side.upper(), source.upper()
    if side not in SIDES:
        raise PositionError(f"side must be one of {', '.join(SIDES)}")
    if source not in SOURCES:
        raise PositionError(f"source must be one of {', '.join(SOURCES)}")
    if pos["instrument_type"] == "OPTION" and float(quantity) != int(quantity):
        raise PositionError("Options trade in whole contracts")
    stamp = parse_time(executed_at)
    candidate = Fill(side, float(quantity), float(price), float(fees or 0.0), stamp)
    fills = sorted(_fills(cur, position_id) + [candidate], key=lambda f: (f.executed_at, f.id or 10**12))
    account(pos["direction"], pos["multiplier"], fills)          # raises before anything is written
    cur.execute("INSERT INTO executions (position_id, side, quantity, price, fees, executed_at, source, note, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (position_id, side, float(quantity), float(price), float(fees or 0.0), stamp, source, note.strip(), now_iso()))
    return recompute(cur, position_id)


def cancel(cur: sqlite3.Cursor, position_id: int, note: str = "") -> None:
    pos = _row(cur, position_id)
    if pos["status"] != "PLANNED":
        raise PositionError("Only a planned position with no executions can be cancelled")
    cur.execute("UPDATE positions SET status = 'CANCELLED', notes = TRIM(notes || ' ' || ?), updated_at = ? WHERE id = ?",
                (note, now_iso(), position_id))


PLAN_FIELDS = ("thesis", "setup_type", "intended_horizon", "invalidation_price", "target_1", "target_2", "risk_amount",
               "initial_risk_per_unit", "notes")


def update_plan(cur: sqlite3.Cursor, position_id: int, changes: dict[str, Any]) -> None:
    _row(cur, position_id)
    sets, values = [], []
    for key, value in changes.items():
        if key not in PLAN_FIELDS:
            raise PositionError(f"{key} cannot be changed")
        if key in ("invalidation_price", "target_1", "target_2", "risk_amount", "initial_risk_per_unit") and value is not None:
            if not (math.isfinite(float(value)) and float(value) > 0):
                raise PositionError(f"{key} must be a positive number")
        sets.append(f"{key} = ?")
        values.append(value.strip() if isinstance(value, str) else value)
    if sets:
        cur.execute(f"UPDATE positions SET {', '.join(sets)}, updated_at = ? WHERE id = ?", (*values, now_iso(), position_id))


SNAPSHOT_COLUMNS = (
    "underlying_price", "spy_price", "qqq_price", "vix", "rsi_14", "macd", "macd_signal", "macd_histogram", "sma_20",
    "sma_50", "sma_200", "ema_20", "adx", "atr", "volume_ratio", "structure_state", "trend_state",
    "relative_strength_spy", "relative_strength_sector", "bullish_score", "bearish_score", "market_regime", "iv",
    "iv_rank", "delta", "gamma", "theta", "vega", "bid", "ask", "mid", "spread_pct", "dte", "signal_engine_version",
    "data_timestamp", "data_source", "bar_status")


def add_snapshot(cur: sqlite3.Cursor, position_id: int, snapshot_type: str, values: dict[str, Any],
                 raw: dict[str, Any] | None = None, timestamp: str | None = None) -> int:
    _row(cur, position_id)
    if snapshot_type not in SNAPSHOT_TYPES:
        raise PositionError(f"snapshot_type must be one of {', '.join(SNAPSHOT_TYPES)}")
    cols = [c for c in SNAPSHOT_COLUMNS if c in values]
    cur.execute(f"INSERT INTO position_snapshots (position_id, snapshot_type, timestamp, {', '.join(cols)}, raw_json) "
                f"VALUES (?, ?, ?, {', '.join('?' for _ in cols)}, ?)",
                (position_id, snapshot_type, timestamp or now_iso(), *[values[c] for c in cols],
                 json.dumps(raw or {}, default=str)))
    return int(cur.lastrowid)


def read(cur: sqlite3.Cursor, position_id: int) -> dict[str, Any]:
    """A position with its option leg, executions, snapshots and accounts."""
    pos = _row(cur, position_id)
    cur.execute("SELECT * FROM option_contracts WHERE position_id = ? ORDER BY leg", (position_id,))
    names = [d[0] for d in cur.description]
    pos["legs"] = [dict(zip(names, r)) for r in cur.fetchall()]
    cur.execute("SELECT * FROM executions WHERE position_id = ? ORDER BY executed_at, id", (position_id,))
    names = [d[0] for d in cur.description]
    pos["executions"] = [dict(zip(names, r)) for r in cur.fetchall()]
    cur.execute("SELECT * FROM position_snapshots WHERE position_id = ? ORDER BY timestamp, id", (position_id,))
    names = [d[0] for d in cur.description]
    snaps = []
    for r in cur.fetchall():
        snap = dict(zip(names, r))
        snap["raw"] = json.loads(snap.pop("raw_json") or "{}")
        snaps.append(snap)
    pos["snapshots"] = snaps
    led = account(pos["direction"], pos["multiplier"], [Fill(e["side"], e["quantity"], e["price"], e["fees"],
                                                              e["executed_at"], e["id"]) for e in pos["executions"]])
    pos["ledger"] = asdict(led)
    pos["ledger"]["initial_risk"] = initial_risk(pos, led)
    return pos


def list_ids(cur: sqlite3.Cursor, statuses: Sequence[str] | None = None) -> list[int]:
    if statuses:
        marks = ", ".join("?" for _ in statuses)
        cur.execute(f"SELECT id FROM positions WHERE status IN ({marks}) ORDER BY id DESC", tuple(statuses))
    else:
        cur.execute("SELECT id FROM positions ORDER BY id DESC")
    return [r[0] for r in cur.fetchall()]


# --- the classic option journal ------------------------------------------------------------------

LEGACY_STATUS = {"planned": "PLANNED", "open": "OPEN", "closed": "CLOSED", "cancelled": "CANCELLED"}


def sync_legacy_trade(cur: sqlite3.Cursor, trade_id: int) -> int | None:
    """Mirror one classic journal entry (a bought call or put) as a unified position, so the
    Positions screen, portfolio risk and analytics include it. The classic entry stays the
    record: this rebuilds the mirror's executions from it each time it changes."""
    cur.execute("SELECT * FROM trades WHERE id = ?", (trade_id,))
    row = cur.fetchone()
    if row is None:
        return None
    t = dict(zip([d[0] for d in cur.description], row))
    cur.execute("SELECT id FROM positions WHERE legacy_trade_id = ?", (trade_id,))
    found = cur.fetchone()
    plan = json.loads(t.get("plan_json") or "{}")
    if found is None:
        pid = create(cur, symbol=t["symbol"], instrument_type="OPTION", direction="LONG", quantity=t["contracts"],
                     thesis=t.get("thesis") or "", setup_type="option journal",
                     option=OptionLeg(t["option_type"].upper(), t["strike"], t["expiration"], t["option_symbol"]),
                     data_source=t.get("data_source") or "", legacy_trade_id=trade_id, created_at=t["created_at"],
                     risk_amount=(t["max_limit"] * 100 * t["contracts"]) if t.get("max_limit") else None)
    else:
        pid = int(found[0])
        cur.execute("UPDATE positions SET thesis = ?, notes = ? WHERE id = ?", (t.get("thesis") or "", t.get("notes") or "", pid))
    cur.execute("DELETE FROM executions WHERE position_id = ?", (pid,))
    if t.get("entry_price") is not None:
        cur.execute("INSERT INTO executions (position_id, side, quantity, price, fees, executed_at, source, note, created_at) "
                    "VALUES (?, 'BUY', ?, ?, 0, ?, 'IMPORT', 'from the option journal', ?)",
                    (pid, t["contracts"], t["entry_price"], t.get("entry_at") or t["created_at"], now_iso()))
    if t.get("exit_price") is not None and t.get("entry_price") is not None:
        cur.execute("INSERT INTO executions (position_id, side, quantity, price, fees, executed_at, source, note, created_at) "
                    "VALUES (?, 'SELL', ?, ?, 0, ?, 'IMPORT', 'from the option journal', ?)",
                    (pid, t["contracts"], t["exit_price"], t.get("exit_at") or now_iso(), now_iso()))
    status = LEGACY_STATUS.get(t["status"], "PLANNED")
    cur.execute("UPDATE positions SET status = ? WHERE id = ?", ("CANCELLED" if status == "CANCELLED" else "PLANNED", pid))
    recompute(cur, pid)
    if plan.get("stop_loss_pct") and t.get("entry_price"):
        cur.execute("UPDATE positions SET notes = TRIM(notes || ' ' || ?) WHERE id = ? AND notes NOT LIKE '%Exit plan:%'",
                    (f"Exit plan: take profit at +{plan.get('profit_target_pct', 50):g}%, stop at -{plan['stop_loss_pct']:g}% "
                     "of the premium.", pid))
    return pid
