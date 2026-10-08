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
from dataclasses import asdict, dataclass, field
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
MIN_QUANTITY = 1e-6                    # smallest fill recorded: well above EPS, below any broker's fractional share
CLOSED_MESSAGE = "This position is already closed. Create a new position for a new trade."
# How a position's status may change when fills are added. A closed or cancelled position is finished:
# a new trade in the same stock is a new position with its own id. Fixing a mistake in a finished
# position's history goes through the audited correction workflow (positions.correct_execution).
TRANSITIONS = {
    "PLANNED": ("OPEN", "CLOSED", "CANCELLED"),
    "OPEN": ("PARTIALLY_CLOSED", "CLOSED"),
    "PARTIALLY_CLOSED": ("CLOSED",),
    "CLOSED": (),
    "CANCELLED": (),
}


class PositionError(ValueError):
    """A request about a position that cannot be carried out; the message is shown to the user."""


def number(name: str, value: Any, *, above: float | None = None, minimum: float | None = None) -> float:
    """`value` as a finite float, or a PositionError naming the field. NaN and infinity are never
    accepted: a single one would turn every total it touches into nonsense."""
    if isinstance(value, bool):
        raise PositionError(f"{name} must be a number")
    try:
        f = float(value)
    except (TypeError, ValueError) as exc:
        raise PositionError(f"{name} must be a number") from exc
    if not math.isfinite(f):
        raise PositionError(f"{name} must be a finite number")
    if above is not None and not f > above:
        raise PositionError(f"{name} must be above {above:g}")
    if minimum is not None and f < minimum:
        raise PositionError(f"{name} cannot be below {minimum:g}")
    return f


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
    # Fills that came after the position had closed completely. Only older databases can hold these
    # (versions before 2.1.1 allowed it); they are reported, never created now.
    lifecycle_issues: list[dict[str, Any]] = field(default_factory=list)

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


def account(direction: str, multiplier: float, fills: Iterable[Fill], allow_reopen: bool = False) -> Ledger:
    """Replay fills in order and return the position's accounts.

    Raises PositionError if a fill would close more than is open, if any number is not finite,
    or if a fill comes after the position has closed completely. With `allow_reopen` (reading
    an older database that already holds such fills) those fills are accounted as before and
    listed in `lifecycle_issues` instead."""
    if direction not in DIRECTIONS:
        raise PositionError("direction must be LONG or SHORT")
    multiplier = number("multiplier", multiplier, above=0)
    led = Ledger(direction=direction, multiplier=multiplier)
    open_cost = 0.0                       # sum of price x quantity for the open quantity, at average cost
    entry_value = exit_value = 0.0
    for f in fills:
        if not (isinstance(f.quantity, (int, float)) and math.isfinite(f.quantity) and f.quantity > 0):
            raise PositionError("Every execution needs a quantity above zero")
        if f.quantity < MIN_QUANTITY:
            raise PositionError(f"Quantities below {MIN_QUANTITY:g} are too small to record")
        if not (isinstance(f.price, (int, float)) and math.isfinite(f.price)):
            raise PositionError("Every execution needs a finite price")
        if not (isinstance(f.fees, (int, float)) and math.isfinite(f.fees)):
            raise PositionError("Fees must be a finite number")
        if f.price < 0 or f.fees < 0:
            raise PositionError("Prices cannot be negative, and nor can fees")
        kind = classify(direction, f.side)
        if led.opened_quantity > EPS and led.open_quantity <= EPS:
            # The position had closed completely before this fill.
            if not allow_reopen:
                raise PositionError(CLOSED_MESSAGE)
            if kind == "OPEN":
                led.lifecycle_issues.append({"problem": "REOPENED_AFTER_CLOSE", "execution_id": f.id,
                                             "executed_at": f.executed_at, "closed_at": led.closed_at})
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
    figures = (led.realized_pnl, led.gross_realized, led.fees, led.open_quantity, led.cost_basis,
               led.average_entry_price, led.average_exit_price)
    if any(v is not None and not math.isfinite(v) for v in figures):
        raise PositionError("These numbers are too large to account for accurately")
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

def _raw_fills(cur: sqlite3.Cursor, position_id: int) -> list[Fill]:
    rows = cur.execute("SELECT id, side, quantity, price, fees, executed_at FROM executions WHERE position_id = ? "
                       "ORDER BY executed_at, id", (position_id,)).fetchall()
    return [Fill(r[1], r[2], r[3], r[4], r[5], r[0]) for r in rows]


def _fills(cur: sqlite3.Cursor, position_id: int) -> list[Fill]:
    """The position's fills in today's units: splits and contract adjustments applied (unit_adjustments)."""
    return adjusted_fills(_raw_fills(cur, position_id), unit_adjustments(cur, position_id))


def unit_adjustments(cur: sqlite3.Cursor, position_id: int) -> list[dict[str, Any]]:
    """Changes of unit that apply to fills before they happened: a stock split (ratio r: r times
    the shares at 1/r of the price) or an option contract adjustment (f times the contracts, each
    worth old multiplier / (f x new multiplier) of the old price). The recorded fills are never
    changed; these are applied when they are replayed."""
    out: list[dict[str, Any]] = []
    if _has_table(cur, "position_adjustments"):
        for r in cur.execute("SELECT id, effective_at, ratio FROM position_adjustments WHERE position_id = ? AND kind = 'SPLIT' "
                             "AND voided_at IS NULL ORDER BY effective_at, id", (position_id,)).fetchall():
            out.append({"kind": "SPLIT", "id": r[0], "effective_at": r[1], "quantity_factor": r[2], "price_factor": 1.0 / r[2]})
    if _has_table(cur, "option_events"):
        for r in cur.execute("SELECT id, event_at, previous_json, new_json FROM option_events WHERE position_id = ? AND "
                             "event = 'CONTRACT_ADJUSTED' ORDER BY event_at, id", (position_id,)).fetchall():
            before, after = json.loads(r[2]), json.loads(r[3])
            f = float(after.get("quantity_factor") or 1.0)
            out.append({"kind": "CONTRACT_ADJUSTED", "id": r[0], "effective_at": r[1], "quantity_factor": f,
                        "price_factor": float(before["multiplier"]) / (f * float(after["multiplier"]))})
    return sorted(out, key=lambda a: a["effective_at"])


def adjusted_fills(fills: Iterable[Fill], adjustments: Sequence[dict[str, Any]]) -> list[Fill]:
    out = []
    for f in fills:
        qf = pf = 1.0
        for a in adjustments:
            if f.executed_at < a["effective_at"]:
                qf *= a["quantity_factor"]
                pf *= a["price_factor"]
        out.append(f if qf == 1.0 and pf == 1.0 else Fill(f.side, f.quantity * qf, f.price * pf, f.fees, f.executed_at, f.id))
    return out


def _row(cur: sqlite3.Cursor, position_id: int) -> dict[str, Any]:
    cur.execute("SELECT * FROM positions WHERE id = ?", (position_id,))
    row = cur.fetchone()
    if row is None:
        raise PositionError(f"No position {position_id}")
    names = [d[0] for d in cur.description]
    return dict(zip(names, row))


def recompute(cur: sqlite3.Cursor, position_id: int, correction: bool = False) -> Ledger:
    """Recalculate a position's stored figures from its executions. A change of status that
    TRANSITIONS does not allow (a closed position opening again, say) is refused unless this is
    an audited correction."""
    pos = _row(cur, position_id)
    led = account(pos["direction"], pos["multiplier"], _fills(cur, position_id), allow_reopen=True)
    status = led.status(cancelled=pos["status"] == "CANCELLED")
    if not correction and status != pos["status"] and status not in TRANSITIONS.get(pos["status"], ()):
        if pos["status"] == "CLOSED":
            raise PositionError(CLOSED_MESSAGE)
        raise PositionError(f"A position that is {pos['status'].lower().replace('_', ' ')} cannot become "
                            f"{status.lower().replace('_', ' ')}")
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
    quantity = number("quantity", quantity, above=0)
    if quantity < MIN_QUANTITY:
        raise PositionError(f"Quantities below {MIN_QUANTITY:g} are too small to record")
    if instrument_type == "OPTION":
        if option is None:
            raise PositionError("An option position needs its type, strike and expiration")
        if direction == "SHORT":
            raise PositionError("Selling options to open is not supported yet: only bought calls and puts")
        if option.option_type.upper() not in ("CALL", "PUT"):
            raise PositionError("option_type must be CALL or PUT")
        option.strike = number("strike", option.strike, above=0)
        option.multiplier = number("contract multiplier", option.multiplier, above=0)
        if float(quantity) != int(quantity):
            raise PositionError("Options trade in whole contracts")
    for name, value in (("invalidation_price", invalidation_price), ("target_1", target_1), ("target_2", target_2),
                        ("risk_amount", risk_amount), ("initial_risk_per_unit", initial_risk_per_unit)):
        if value is not None:
            try:
                number(name, value, above=0)
            except PositionError as exc:
                raise PositionError(f"{name} must be a positive number") from exc
    if intended_horizon is not None and (isinstance(intended_horizon, bool) or not isinstance(intended_horizon, int)
                                         or not 1 <= intended_horizon <= 1000):
        raise PositionError("intended_horizon must be a whole number of trading days from 1 to 1000")
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
                  fees: float = 0.0, executed_at: str | None = None, source: str = "MANUAL", note: str = "",
                  external_id: str | None = None) -> Ledger:
    """Record one fill. Nothing is written unless the whole history, with this fill in its place,
    still accounts: no closing more than is open, no fill after the position has closed, every
    number finite. A closed or cancelled position takes no more fills.

    `external_id` is the broker's id for the fill. Recording the same id from the same source
    again changes nothing (an import can be repeated safely); the same id with different
    details is refused, because one of the two records is wrong. Fills without an id are never
    merged, since two real fills can have the same size, price and time."""
    pos = _row(cur, position_id)
    if external_id is not None:
        external_id = str(external_id).strip()
        if not external_id:
            raise PositionError("external_id cannot be empty")
        seen = cur.execute("SELECT id, position_id, side, quantity, price, fees, executed_at FROM executions "
                           "WHERE source = ? AND external_id = ?", (str(source).upper(), external_id)).fetchone()
        if seen is not None:
            same = (seen[1] == position_id and seen[2] == str(side).upper() and seen[3] == float(quantity)
                    and seen[4] == float(price) and seen[5] == float(fees or 0.0)
                    and (executed_at is None or seen[6] == parse_time(executed_at)))
            if not same:
                raise PositionError(f"Fill {external_id} from {str(source).lower()} is already recorded with different "
                                    "details. Correct the recorded fill instead of adding it again.")
            return account(pos["direction"], pos["multiplier"], _fills(cur, position_id), allow_reopen=True)
    if pos["status"] == "CANCELLED":
        raise PositionError("This position was cancelled")
    if pos["status"] == "CLOSED":
        raise PositionError(CLOSED_MESSAGE)
    side, source = str(side).upper(), str(source).upper()
    if side not in SIDES:
        raise PositionError(f"side must be one of {', '.join(SIDES)}")
    if source not in SOURCES:
        raise PositionError(f"source must be one of {', '.join(SOURCES)}")
    quantity = number("quantity", quantity, above=0)
    price = number("price", price, minimum=0)
    fees = number("fees", 0.0 if fees is None else fees, minimum=0)
    if pos["instrument_type"] == "OPTION" and quantity != int(quantity):
        raise PositionError("Options trade in whole contracts")
    stamp = parse_time(executed_at)
    candidate = Fill(side, quantity, price, fees, stamp)
    raw = _raw_fills(cur, position_id)
    units = unit_adjustments(cur, position_id)
    before = account(pos["direction"], pos["multiplier"], adjusted_fills(raw, units), allow_reopen=True)
    fills = adjusted_fills(sorted(raw + [candidate], key=lambda f: (f.executed_at, f.id or 10**12)), units)
    after = account(pos["direction"], pos["multiplier"], fills, allow_reopen=True)   # raises before anything is written
    if len(after.lifecycle_issues) > len(before.lifecycle_issues):
        raise PositionError("That fill would close the position completely before a later fill opens it again, and a "
                            "closed position is finished. Record a new trade as a new position, or correct the history.")
    cur.execute("INSERT INTO executions (position_id, side, quantity, price, fees, executed_at, source, note, created_at, "
                "external_id) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (position_id, side, quantity, price, fees, stamp, source, str(note or "").strip(), now_iso(), external_id))
    return recompute(cur, position_id)


CORRECTABLE = ("side", "quantity", "price", "fees", "executed_at")
CORRECTION_SOURCES = ("USER", "JOURNAL_SYNC", "REPAIR")


def _execution(cur: sqlite3.Cursor, position_id: int, execution_id: int) -> dict[str, Any]:
    cur.execute("SELECT * FROM executions WHERE id = ? AND position_id = ?", (execution_id, position_id))
    row = cur.fetchone()
    if row is None:
        raise PositionError(f"Position {position_id} has no fill {execution_id}")
    return dict(zip([d[0] for d in cur.description], row))


def _ledger_summary(led: Ledger, status: str) -> dict[str, Any]:
    return {"status": status, "open_quantity": led.open_quantity, "realized_pnl": led.realized_pnl, "fees": led.fees,
            "average_entry_price": led.average_entry_price, "average_exit_price": led.average_exit_price}


def record_correction(cur: sqlite3.Cursor, position_id: int, execution_id: int | None, action: str,
                      previous: dict[str, Any], new: dict[str, Any], reason: str, source: str,
                      before: dict[str, Any], after: dict[str, Any]) -> int:
    cur.execute("INSERT INTO execution_corrections (position_id, execution_id, action, previous_json, new_json, reason, "
                "source, ledger_before, ledger_after, changed_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (position_id, execution_id, action, json.dumps(previous, default=str), json.dumps(new, default=str),
                 reason, source, json.dumps(before, default=str), json.dumps(after, default=str), now_iso()))
    return int(cur.lastrowid)


def correct_execution(cur: sqlite3.Cursor, position_id: int, execution_id: int, changes: dict[str, Any],
                      reason: str, source: str = "USER") -> dict[str, Any]:
    """Fix a fill that was entered wrongly: its side, quantity, price, fees or time.

    The fill is changed in place, and the correction is recorded with the values before and
    after, the reason, who made it and the position's figures before and after, in the same
    transaction as the change. The whole history must still account afterwards (nothing closes
    more than is open, every number finite); otherwise nothing is written. A correction may
    change the position's status, including opening a closed position again when the fix shows
    it was never fully closed: that is the one way a finished position changes. Snapshots tied
    to a fill whose time changed are marked superseded by the caller (snapshots.py)."""
    reason = str(reason or "").strip()
    if not reason:
        raise PositionError("Say why the fill is being corrected")
    source = str(source).upper()
    if source not in CORRECTION_SOURCES:
        raise PositionError(f"source must be one of {', '.join(CORRECTION_SOURCES)}")
    pos = _row(cur, position_id)
    if pos.get("legacy_trade_id") and source != "JOURNAL_SYNC":
        raise PositionError("This position mirrors an entry in the option journal, which is its record. Its fills "
                            "follow the journal and cannot be corrected here.")
    unknown = [k for k in changes if k not in CORRECTABLE]
    if unknown:
        raise PositionError(f"{', '.join(unknown)} cannot be corrected; a fill's side, quantity, price, fees and time can")
    if not changes:
        raise PositionError("Nothing to correct")
    row = _execution(cur, position_id, execution_id)
    new = {k: row[k] for k in CORRECTABLE}
    for key, value in changes.items():
        if key == "side":
            value = str(value).upper()
            if value not in SIDES:
                raise PositionError(f"side must be one of {', '.join(SIDES)}")
            classify(pos["direction"], value)
        elif key == "quantity":
            value = number("quantity", value, above=0)
            if pos["instrument_type"] == "OPTION" and value != int(value):
                raise PositionError("Options trade in whole contracts")
        elif key == "price":
            value = number("price", value, minimum=0)
        elif key == "fees":
            value = number("fees", 0.0 if value is None else value, minimum=0)
        elif key == "executed_at":
            value = parse_time(value)
        new[key] = value
    previous = {k: row[k] for k in CORRECTABLE}
    if new == previous:
        raise PositionError("Those are the values already recorded")
    raw = _raw_fills(cur, position_id)
    units = unit_adjustments(cur, position_id)
    led_before = account(pos["direction"], pos["multiplier"], adjusted_fills(raw, units), allow_reopen=True)
    fills_after = sorted([Fill(new["side"], new["quantity"], new["price"], new["fees"], new["executed_at"], f.id)
                          if f.id == execution_id else f for f in raw],
                         key=lambda f: (f.executed_at, f.id or 10**12))
    led_after = account(pos["direction"], pos["multiplier"], adjusted_fills(fills_after, units), allow_reopen=True)
    if len(led_after.lifecycle_issues) > len(led_before.lifecycle_issues):
        raise PositionError("With that correction the position would close completely before a later fill, which "
                            "would make it two trades. Record the later fills on a new position instead.")
    cur.execute("UPDATE executions SET side = ?, quantity = ?, price = ?, fees = ?, executed_at = ? WHERE id = ?",
                (new["side"], new["quantity"], new["price"], new["fees"], new["executed_at"], execution_id))
    after = recompute(cur, position_id, correction=True)
    status_after = _row(cur, position_id)["status"]
    cid = record_correction(cur, position_id, execution_id, "UPDATE", previous, new, reason, source,
                            _ledger_summary(led_before, pos["status"]), _ledger_summary(after, status_after))
    return {"correction_id": cid, "execution_id": execution_id, "previous": previous, "new": new,
            "status_before": pos["status"], "status_after": status_after,
            "time_changed": previous["executed_at"] != new["executed_at"]}


def corrections(cur: sqlite3.Cursor, position_id: int) -> list[dict[str, Any]]:
    cur.execute("SELECT * FROM execution_corrections WHERE position_id = ? ORDER BY changed_at, id", (position_id,))
    names = [d[0] for d in cur.description]
    out = []
    for r in cur.fetchall():
        c = dict(zip(names, r))
        for key in ("previous_json", "new_json", "ledger_before", "ledger_after"):
            c[key.removesuffix("_json")] = json.loads(c.pop(key) or "{}")
        out.append(c)
    return out


def cancel(cur: sqlite3.Cursor, position_id: int, note: str = "") -> None:
    pos = _row(cur, position_id)
    if pos["status"] != "PLANNED":
        raise PositionError("Only a planned position with no executions can be cancelled")
    cur.execute("UPDATE positions SET status = 'CANCELLED', notes = TRIM(notes || ' ' || ?), updated_at = ? WHERE id = ?",
                (note, now_iso(), position_id))


PLAN_FIELDS = ("thesis", "setup_type", "intended_horizon", "invalidation_price", "target_1", "target_2", "risk_amount",
               "initial_risk_per_unit", "notes")


NULLABLE_PLAN_FIELDS = ("intended_horizon", "invalidation_price", "target_1", "target_2", "risk_amount",
                        "initial_risk_per_unit")
TEXT_PLAN_FIELDS = ("thesis", "setup_type", "notes")


def update_plan(cur: sqlite3.Cursor, position_id: int, changes: dict[str, Any]) -> None:
    """Change the plan fields present in `changes`; fields left out keep their values.

    A nullable field (levels, risk, horizon) set to None is cleared. Thesis, setup and notes
    cannot be None (send an empty string to clear them). Initial risk and R multiples are worked
    out when read (initial_risk), so they follow whatever remains of the plan."""
    _row(cur, position_id)
    sets, values = [], []
    for key, value in changes.items():
        if key not in PLAN_FIELDS:
            raise PositionError(f"{key} cannot be changed")
        if value is None and key in TEXT_PLAN_FIELDS:
            raise PositionError(f"{key} cannot be null; send an empty string to clear it")
        if key in TEXT_PLAN_FIELDS and not isinstance(value, str):
            raise PositionError(f"{key} must be text")
        if key in ("invalidation_price", "target_1", "target_2", "risk_amount", "initial_risk_per_unit") and value is not None:
            try:
                value = number(key, value, above=0)
            except PositionError as exc:
                raise PositionError(f"{key} must be a positive number") from exc
        if key == "intended_horizon" and value is not None and (isinstance(value, bool) or not isinstance(value, int)
                                                                or not 1 <= value <= 1000):
            raise PositionError("intended_horizon must be a whole number of trading days from 1 to 1000")
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


PROVENANCE_COLUMNS = ("execution_id", "executed_at", "observation_cutoff", "captured_at", "method", "capture_timing",
                      "uses_later_data")


def add_snapshot(cur: sqlite3.Cursor, position_id: int, snapshot_type: str, values: dict[str, Any],
                 raw: dict[str, Any] | None = None, timestamp: str | None = None,
                 provenance: dict[str, Any] | None = None) -> int:
    """Store one snapshot. `provenance` holds the PROVENANCE_COLUMNS (which fill it is about, when
    its data is from, how it was made) and anything else, kept in provenance_json. Rules for when
    snapshots are taken, and which one is effective, are in snapshots.py."""
    _row(cur, position_id)
    if snapshot_type not in SNAPSHOT_TYPES:
        raise PositionError(f"snapshot_type must be one of {', '.join(SNAPSHOT_TYPES)}")
    prov = dict(provenance or {})
    meta = {k: prov.pop(k) for k in PROVENANCE_COLUMNS if k in prov}
    if "uses_later_data" in meta and meta["uses_later_data"] is not None:
        meta["uses_later_data"] = int(bool(meta["uses_later_data"]))
    meta.setdefault("captured_at", now_iso())
    cols = [c for c in SNAPSHOT_COLUMNS if c in values]
    names = [*cols, *meta, "provenance_json"]
    cur.execute(f"INSERT INTO position_snapshots (position_id, snapshot_type, timestamp, {', '.join(names)}, raw_json) "
                f"VALUES (?, ?, ?, {', '.join('?' for _ in names)}, ?)",
                (position_id, snapshot_type, timestamp or now_iso(), *[values[c] for c in cols], *meta.values(),
                 json.dumps(prov, default=str), json.dumps(raw or {}, default=str)))
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
        snap["provenance"] = json.loads(snap.pop("provenance_json", None) or "{}")
        snap["effective"] = snap.get("superseded_at") is None
        if snap.get("uses_later_data") is not None:
            snap["uses_later_data"] = bool(snap["uses_later_data"])
        snaps.append(snap)
    pos["snapshots"] = snaps
    has_audit = cur.execute("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'execution_corrections'").fetchone()
    pos["corrections"] = corrections(cur, position_id) if has_audit else []
    pos["unit_adjustments"] = unit_adjustments(cur, position_id)
    led = account(pos["direction"], pos["multiplier"],
                  adjusted_fills([Fill(e["side"], e["quantity"], e["price"], e["fees"], e["executed_at"], e["id"])
                                  for e in pos["executions"]], pos["unit_adjustments"]),
                  allow_reopen=True)
    pos["ledger"] = asdict(led)
    pos["ledger"]["initial_risk"] = initial_risk(pos, led)
    pos["ledger"]["units_adjusted"] = bool(pos["unit_adjustments"])
    pos["adjustments"] = adjustments(cur, position_id) if _has_table(cur, "position_adjustments") else []
    pos["option_events"], pos["derived_from"] = ([], [])
    if _has_table(cur, "option_events"):
        pos["option_events"] = _events(cur, "position_id", position_id)
        pos["derived_from"] = _events(cur, "resulting_position_id", position_id)
    pos["financing"] = financing(pos)
    pos["lifecycle"] = lifecycle(pos)
    return pos


def list_ids(cur: sqlite3.Cursor, statuses: Sequence[str] | None = None) -> list[int]:
    if statuses:
        marks = ", ".join("?" for _ in statuses)
        cur.execute(f"SELECT id FROM positions WHERE status IN ({marks}) ORDER BY id DESC", tuple(statuses))
    else:
        cur.execute("SELECT id FROM positions ORDER BY id DESC")
    return [r[0] for r in cur.fetchall()]


# --- money outside the fills: borrow fees, dividends, splits ------------------------------------

ADJUSTMENT_KINDS = ("BORROW_FEE", "BORROW_RATE", "DIVIDEND_OWED", "DIVIDEND_RECEIVED", "CASH_ADJUSTMENT", "SPLIT")
MONEY_KINDS = ("BORROW_FEE", "DIVIDEND_OWED", "DIVIDEND_RECEIVED", "CASH_ADJUSTMENT")


def adjustments(cur: sqlite3.Cursor, position_id: int) -> list[dict[str, Any]]:
    cur.execute("SELECT * FROM position_adjustments WHERE position_id = ? ORDER BY effective_at, id", (position_id,))
    names = [d[0] for d in cur.description]
    out = []
    for r in cur.fetchall():
        a = dict(zip(names, r))
        a["details"] = json.loads(a.pop("details_json") or "{}")
        out.append(a)
    return out


def add_adjustment(cur: sqlite3.Cursor, position_id: int, kind: str, *, amount: float | None = None,
                   effective_at: str | None = None, rate_pct: float | None = None, quantity: float | None = None,
                   per_share: float | None = None, ratio: float | None = None, period_start: str | None = None,
                   period_end: str | None = None, note: str = "", source: str = "MANUAL",
                   external_id: str | None = None) -> int:
    """Record money that moved outside the fills, or a stock split.

    BORROW_FEE      a fee charged for borrowing shares sold short: a cost (amount below zero),
                    optionally with the yearly rate and the days it covers
    BORROW_RATE     the borrow rate from a date on (no money moves; used only to say what an
                    unrecorded fee might be, clearly as an estimate)
    DIVIDEND_OWED   a dividend paid on shares sold short (a cost): amount, or per_share x quantity
    DIVIDEND_RECEIVED a dividend on shares held long
    CASH_ADJUSTMENT any other cash the broker booked against the position (either sign)
    SPLIT           a stock split: `ratio` new shares per old (2 for 2-for-1, 0.1 for 1-for-10);
                    fills before it are replayed in post-split units and the plan's price
                    levels are rescaled, with the old levels kept in `details`

    Amounts come from the broker; nothing is estimated here. A broker's id (`external_id`) makes
    importing the same entry twice a no-op."""
    pos = _row(cur, position_id)
    kind = str(kind).upper()
    if kind not in ADJUSTMENT_KINDS:
        raise PositionError(f"kind must be one of {', '.join(ADJUSTMENT_KINDS)}")
    source = str(source).upper()
    if source not in SOURCES:
        raise PositionError(f"source must be one of {', '.join(SOURCES)}")
    stock, short = pos["instrument_type"] == "STOCK", pos["direction"] == "SHORT"
    if kind in ("BORROW_FEE", "BORROW_RATE", "DIVIDEND_OWED") and not (stock and short):
        raise PositionError("Borrow fees, borrow rates and dividends owed apply to stock sold short")
    if kind == "DIVIDEND_RECEIVED" and not (stock and not short):
        raise PositionError("Dividends received apply to stock held long")
    if kind == "SPLIT" and not stock:
        raise PositionError("Record a split on the stock position; for an option, record the contract adjustment")
    if pos.get("legacy_trade_id"):
        raise PositionError("This position mirrors an entry in the option journal; record adjustments on its own positions")
    stamp = parse_time(effective_at)
    vals: dict[str, Any] = {"rate_pct": None, "quantity": None, "per_share": None, "ratio": None}
    for name, value in (("rate_pct", rate_pct), ("quantity", quantity), ("per_share", per_share), ("ratio", ratio)):
        if value is not None:
            vals[name] = number(name, value, above=0)
    if amount is not None:
        amount = number("amount", amount)
    details: dict[str, Any] = {}
    if kind == "BORROW_FEE":
        if amount is None or amount >= 0:
            raise PositionError("A borrow fee is a cost: enter it as a negative amount, as the broker charged it")
    elif kind == "BORROW_RATE":
        if vals["rate_pct"] is None:
            raise PositionError("Enter the yearly borrow rate in percent")
        amount = 0.0
    elif kind in ("DIVIDEND_OWED", "DIVIDEND_RECEIVED"):
        if amount is None:
            if vals["per_share"] is None or vals["quantity"] is None:
                raise PositionError("Enter the dividend amount, or the dividend per share and the number of shares")
            amount = vals["per_share"] * vals["quantity"]
            amount = -amount if kind == "DIVIDEND_OWED" else amount
        if kind == "DIVIDEND_OWED" and amount > 0 or kind == "DIVIDEND_RECEIVED" and amount < 0:
            raise PositionError("A dividend owed on a short is a cost (negative); one received on a long is income")
    elif kind == "CASH_ADJUSTMENT":
        if amount is None or amount == 0:
            raise PositionError("Enter the amount the broker booked, negative for a charge")
    elif kind == "SPLIT":
        if vals["ratio"] is None or vals["ratio"] == 1:
            raise PositionError("Enter the split ratio: new shares per old share")
        amount = 0.0
        levels = {k: pos.get(k) for k in ("invalidation_price", "target_1", "target_2") if pos.get(k) is not None}
        details = {"plan_levels_before": levels,
                   "plan_levels_after": {k: v / vals["ratio"] for k, v in levels.items()},
                   "planned_quantity_before": pos["quantity"]}
    if period_start is not None:
        period_start = parse_time(period_start)
    if period_end is not None:
        period_end = parse_time(period_end)
    if period_start and period_end and period_end < period_start:
        raise PositionError("The period ends before it starts")
    if external_id is not None:
        external_id = str(external_id).strip()
        seen = cur.execute("SELECT id, position_id, kind, amount, effective_at FROM position_adjustments "
                           "WHERE source = ? AND external_id = ?", (source, external_id)).fetchone()
        if seen is not None:
            if (seen[1], seen[2], seen[3], seen[4]) != (position_id, kind, amount, stamp):
                raise PositionError(f"Entry {external_id} from {source.lower()} is already recorded with different details")
            return int(seen[0])
    cur.execute("INSERT INTO position_adjustments (position_id, kind, amount, rate_pct, quantity, per_share, ratio, "
                "effective_at, period_start, period_end, source, external_id, note, details_json, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (position_id, kind, amount, vals["rate_pct"], vals["quantity"], vals["per_share"], vals["ratio"], stamp,
                 period_start, period_end, source, external_id, str(note or "").strip(), json.dumps(details), now_iso()))
    aid = int(cur.lastrowid)
    if kind == "SPLIT":
        account(pos["direction"], pos["multiplier"], _fills(cur, position_id), allow_reopen=True)   # still accounts
        sets = {k: v for k, v in details["plan_levels_after"].items()}
        # The planned size was in pre-split shares if the position was planned before the split.
        sets["quantity"] = pos["quantity"] * vals["ratio"] if pos["created_at"] < stamp else pos["quantity"]
        cur.execute(f"UPDATE positions SET {', '.join(f'{k} = ?' for k in sets)} WHERE id = ?", (*sets.values(), position_id))
        recompute(cur, position_id, correction=True)
    return aid


def void_adjustment(cur: sqlite3.Cursor, position_id: int, adjustment_id: int, reason: str) -> None:
    """Mark a mistaken adjustment void. It stays on record with the reason; a voided split's plan
    levels are put back."""
    reason = str(reason or "").strip()
    if not reason:
        raise PositionError("Say why the entry is being voided")
    row = cur.execute("SELECT kind, voided_at, details_json FROM position_adjustments WHERE id = ? AND position_id = ?",
                      (adjustment_id, position_id)).fetchone()
    if row is None:
        raise PositionError(f"Position {position_id} has no adjustment {adjustment_id}")
    if row[1]:
        raise PositionError("That entry is already void")
    cur.execute("UPDATE position_adjustments SET voided_at = ?, void_reason = ? WHERE id = ?", (now_iso(), reason, adjustment_id))
    if row[0] == "SPLIT":
        details = json.loads(row[2] or "{}")
        sets = {**details.get("plan_levels_before", {}), "quantity": details.get("planned_quantity_before")}
        sets = {k: v for k, v in sets.items() if v is not None}
        if sets:
            cur.execute(f"UPDATE positions SET {', '.join(f'{k} = ?' for k in sets)} WHERE id = ?", (*sets.values(), position_id))
        recompute(cur, position_id, correction=True)


def financing(pos: dict[str, Any], now: datetime | None = None) -> dict[str, Any]:
    """Money outside the fills, kept apart from trading profit.

    `trading_pnl` is what the fills made (after their fees); `financing_total` the borrow fees,
    dividends and cash adjustments recorded; `net_economic_pnl` the two together. For stock
    sold short, `borrow_status` says whether borrow fees are recorded for the whole time the
    shares were borrowed: UNKNOWN when none are recorded, PARTIAL when they stop before the
    position closed (or before today), RECORDED otherwise. An unrecorded fee is never filled in:
    `borrow_estimate` (from a rate you entered) is shown beside it, labelled, and not added."""
    now = now or datetime.now(timezone.utc)
    active = [a for a in pos.get("adjustments", []) if not a.get("voided_at")]
    by_kind = {k: sum(a["amount"] for a in active if a["kind"] == k) for k in MONEY_KINDS}
    total = sum(by_kind.values())
    out: dict[str, Any] = {"by_kind": by_kind, "financing_total": total, "entries": len(active),
                           "trading_realized": pos["ledger"]["realized_pnl"]}
    short_stock = pos["instrument_type"] == "STOCK" and pos["direction"] == "SHORT" and pos["ledger"]["fills"] > 0
    if short_stock:
        fees = [a for a in active if a["kind"] == "BORROW_FEE"]
        end = datetime.fromisoformat(pos["ledger"]["closed_at"]) if pos["ledger"].get("closed_at") else now
        start = datetime.fromisoformat(pos["ledger"]["opened_at"]) if pos["ledger"].get("opened_at") else end
        through = max((datetime.fromisoformat(a["period_end"] or a["effective_at"]) for a in fees), default=None)
        if not fees:
            status, text = "UNKNOWN", "No borrow fees are recorded, so the cost of borrowing these shares is not known."
        elif through is not None and (end - through).days >= 1:
            status, text = "PARTIAL", f"Borrow fees are recorded through {through.date():%b %d, %Y}; later days are not known."
        else:
            status, text = "RECORDED", "Borrow fees are recorded for the whole time the shares were borrowed."
        rates = [a for a in active if a["kind"] == "BORROW_RATE" or (a["kind"] == "BORROW_FEE" and a.get("rate_pct"))]
        estimate = None
        if status != "RECORDED" and rates and pos["ledger"].get("cost_basis"):
            rate = rates[-1]["rate_pct"]
            days = max((end - (through or start)).total_seconds() / 86400, 0.0)
            shares = pos["ledger"]["open_quantity"] or pos["ledger"]["max_open_quantity"]
            estimate = -rate / 100 * shares * pos["ledger"]["cost_basis"] * days / 360
        out.update({"borrow_status": status, "borrow_text": text, "borrow_recorded_through": through.isoformat() if through else None,
                    "borrow_estimate": estimate,
                    "borrow_estimate_note": "An estimate from the rate you entered over the days without a recorded "
                                            "fee, at cost. Not a charge, and not included in any profit figure."
                    if estimate is not None else None})
    return out


def _events(cur: sqlite3.Cursor, column: str, position_id: int) -> list[dict[str, Any]]:
    cur.execute(f"SELECT * FROM option_events WHERE {column} = ? ORDER BY event_at, id", (position_id,))
    names = [d[0] for d in cur.description]
    out = []
    for r in cur.fetchall():
        e = dict(zip(names, r))
        e["previous"] = json.loads(e.pop("previous_json") or "{}")
        e["new"] = json.loads(e.pop("new_json") or "{}")
        out.append(e)
    return out


# --- how a bought option ends ------------------------------------------------------------------

def expiration_close(expiration: str) -> datetime:
    from .optionmath import NY
    return datetime.combine(datetime.fromisoformat(expiration).date(), datetime.min.time().replace(hour=16), tzinfo=NY)


def _option(cur: sqlite3.Cursor, position_id: int) -> tuple[dict[str, Any], dict[str, Any], Ledger]:
    pos = _row(cur, position_id)
    if pos["instrument_type"] != "OPTION":
        raise PositionError("That is not an option position")
    if pos.get("legacy_trade_id"):
        raise PositionError("This position mirrors an entry in the option journal: close it there (at 0 if it expired "
                            "worthless)")
    cur.execute("SELECT * FROM option_contracts WHERE position_id = ? ORDER BY leg LIMIT 1", (position_id,))
    leg = dict(zip([d[0] for d in cur.description], cur.fetchone()))
    led = account(pos["direction"], pos["multiplier"], _fills(cur, position_id), allow_reopen=True)
    return pos, leg, led


def _intrinsic(leg: dict[str, Any], underlying: float) -> float:
    return max((underlying - leg["strike"]) if leg["option_type"] == "CALL" else (leg["strike"] - underlying), 0.0)


def _record_event(cur: sqlite3.Cursor, position_id: int, event: str, quantity: float, event_at: str, **kw: Any) -> int:
    cur.execute("INSERT INTO option_events (position_id, event, quantity, event_at, underlying_price, underlying_price_source, "
                "settlement_price, execution_id, resulting_position_id, resulting_execution_id, previous_json, new_json, "
                "reason, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (position_id, event, quantity, event_at, kw.get("underlying_price"), kw.get("underlying_price_source"),
                 kw.get("settlement_price"), kw.get("execution_id"), kw.get("resulting_position_id"),
                 kw.get("resulting_execution_id"), json.dumps(kw.get("previous") or {}), json.dumps(kw.get("new") or {}),
                 kw.get("reason") or "", now_iso()))
    return int(cur.lastrowid)


def _last_execution(cur: sqlite3.Cursor, position_id: int) -> int:
    return int(cur.execute("SELECT MAX(id) FROM executions WHERE position_id = ?", (position_id,)).fetchone()[0])


def expire_worthless(cur: sqlite3.Cursor, position_id: int, *, underlying_price: float | None = None,
                     underlying_price_source: str | None = None, reason: str = "",
                     now: datetime | None = None) -> dict[str, Any]:
    """Record that a bought option expired with nothing left: the open contracts are closed at 0
    at the 4 p.m. close on the expiration date. Refused before then. If the stock price at
    expiration is known and the option was in the money by a cent or more, brokers normally
    exercise it automatically, so a reason is required to record it as worthless."""
    pos, leg, led = _option(cur, position_id)
    if led.open_quantity <= EPS:
        raise PositionError("Nothing is open on this option")
    close = expiration_close(leg["expiration"])
    if (now or datetime.now(timezone.utc)) < close:
        raise PositionError(f"This option has not expired yet: it expires at the close on {leg['expiration']}")
    itm = None
    if underlying_price is not None:
        underlying_price = number("stock price at expiration", underlying_price, above=0)
        itm = _intrinsic(leg, underlying_price)
        if itm >= 0.01 and not str(reason or "").strip():
            raise PositionError(f"At {underlying_price:,.2f} this option was ${itm:,.2f} in the money at expiration, and "
                                "brokers normally exercise that automatically. Record the exercise, or give the reason "
                                "it expired worthless (for example, you told the broker not to exercise).")
    stamp = close.astimezone(timezone.utc).isoformat(timespec="seconds")
    add_execution(cur, position_id, side="SELL", quantity=led.open_quantity, price=0.0, executed_at=stamp,
                  note="expired worthless")
    eid = _last_execution(cur, position_id)
    ev = _record_event(cur, position_id, "EXPIRED_WORTHLESS", led.open_quantity, stamp, underlying_price=underlying_price,
                       underlying_price_source=underlying_price_source, settlement_price=0.0, execution_id=eid,
                       reason=reason)
    return {"event_id": ev, "execution_id": eid, "in_the_money_by": itm}


def exercise(cur: sqlite3.Cursor, position_id: int, *, underlying_price: float, underlying_price_source: str = "entered",
             quantity: float | None = None, event_at: str | None = None, apply_to_position_id: int | None = None,
             reason: str = "") -> dict[str, Any]:
    """Record exercising bought calls or puts (early, or automatically at expiration).

    The option leg is closed at its intrinsic value against `underlying_price` (the stock price
    at exercise), and the shares are recorded at that same price: bought for a call, sold for a
    put. Together that is buying (or selling) the shares at the strike after paying the premium,
    and each side's profit is shown where it belongs. The shares go on `apply_to_position_id`
    (an open stock position in the same stock) or on a new stock position, long for a call and
    short for a put, linked to the option. Shares delivered are contracts x the contract
    multiplier; adjusted contracts that deliver cash or other securities are not supported."""
    pos, leg, led = _option(cur, position_id)
    s_price = number("stock price at exercise", underlying_price, above=0)
    qty = led.open_quantity if quantity is None else number("contracts", quantity, above=0)
    if qty != int(qty):
        raise PositionError("Options are exercised in whole contracts")
    if qty > led.open_quantity + EPS:
        raise PositionError(f"Only {led.open_quantity:g} contracts are open")
    close = expiration_close(leg["expiration"])
    stamp = parse_time(event_at) if event_at else \
        min(datetime.now(timezone.utc), close.astimezone(timezone.utc)).isoformat(timespec="seconds")
    if datetime.fromisoformat(stamp) > close:
        raise PositionError(f"An option cannot be exercised after it expires ({leg['expiration']})")
    if led.opened_at and stamp < led.opened_at:
        raise PositionError("That is before the option was bought")
    intrinsic = _intrinsic(leg, s_price)
    if intrinsic <= 0:
        raise PositionError(f"At {s_price:,.2f} this {leg['option_type'].lower()} is out of the money: exercising it would "
                            "lose money. Check the stock price, or record it as expired worthless.")
    add_execution(cur, position_id, side="SELL", quantity=qty, price=intrinsic, executed_at=stamp,
                  note=f"exercised at a stock price of {s_price:,.2f}")
    eid = _last_execution(cur, position_id)
    shares = qty * pos["multiplier"]
    buying = leg["option_type"] == "CALL"
    if apply_to_position_id is not None:
        target = _row(cur, int(apply_to_position_id))
        if target["instrument_type"] != "STOCK" or target["symbol"] != pos["symbol"]:
            raise PositionError(f"The shares must go on a {pos['symbol']} stock position")
        side = ("BUY" if target["direction"] == "LONG" else "BUY_TO_COVER") if buying else             ("SELL" if target["direction"] == "LONG" else "SELL_SHORT")
        sid = int(target["id"])
    else:
        sid = create(cur, symbol=pos["symbol"], instrument_type="STOCK", direction="LONG" if buying else "SHORT",
                     quantity=shares, thesis=f"Shares from exercising position {position_id} "
                                              f"({leg['strike']:g} {leg['option_type'].lower()}, {leg['expiration']})",
                     setup_type="option exercise", data_source=pos.get("data_source") or "",
                     invalidation_price=pos.get("invalidation_price"), target_1=pos.get("target_1"),
                     target_2=pos.get("target_2"))
        side = "BUY" if buying else "SELL_SHORT"
    add_execution(cur, sid, side=side, quantity=shares, price=s_price, executed_at=stamp,
                  note=f"from exercising position {position_id}")
    stock_eid = _last_execution(cur, sid)
    ev = _record_event(cur, position_id, "EXERCISED", qty, stamp, underlying_price=s_price,
                       underlying_price_source=underlying_price_source, settlement_price=intrinsic, execution_id=eid,
                       resulting_position_id=sid, resulting_execution_id=stock_eid, reason=reason)
    return {"event_id": ev, "execution_id": eid, "stock_position_id": sid, "stock_execution_id": stock_eid,
            "shares": shares, "intrinsic": intrinsic}


def adjust_contract(cur: sqlite3.Cursor, position_id: int, *, effective_at: str, new_strike: float, new_multiplier: float,
                    quantity_factor: float = 1.0, new_option_symbol: str | None = None, reason: str) -> dict[str, Any]:
    """Record a contract adjustment after a split or other corporate action: the strike, the
    multiplier (shares delivered per contract) and how many contracts each old one became. The
    original terms are kept in the event; fills before it are replayed in the new units, so
    every figure stays in step and nothing recorded is changed."""
    pos, leg, led = _option(cur, position_id)
    reason = str(reason or "").strip()
    if not reason:
        raise PositionError("Say what corporate action adjusted the contract")
    strike = number("new strike", new_strike, above=0)
    mult = number("new multiplier", new_multiplier, above=0)
    factor = number("contracts per old contract", quantity_factor, above=0)
    stamp = parse_time(effective_at)
    previous = {"strike": leg["strike"], "multiplier": pos["multiplier"], "option_symbol": leg["option_symbol"],
                "planned_quantity": pos["quantity"]}
    new = {"strike": strike, "multiplier": mult, "option_symbol": new_option_symbol or leg["option_symbol"],
           "quantity_factor": factor, "planned_quantity": pos["quantity"] * factor}
    ev = _record_event(cur, position_id, "CONTRACT_ADJUSTED", max(led.open_quantity, pos["quantity"]), stamp,
                       previous=previous, new=new, reason=reason)
    cur.execute("UPDATE option_contracts SET strike = ?, multiplier = ?, option_symbol = ? WHERE position_id = ? AND leg = ?",
                (strike, mult, new["option_symbol"], position_id, leg["leg"]))
    cur.execute("UPDATE positions SET multiplier = ?, quantity = ? WHERE id = ?", (mult, new["planned_quantity"], position_id))
    after = account(pos["direction"], mult, _fills(cur, position_id), allow_reopen=True)
    if after.open_quantity != int(round(after.open_quantity, 6)):
        raise PositionError("That adjustment leaves a fraction of a contract open; check the contracts per old contract")
    recompute(cur, position_id, correction=True)
    return {"event_id": ev, "previous": previous, "new": new}


def lifecycle(pos: dict[str, Any], now: datetime | None = None) -> dict[str, Any]:
    """How a bought option stands: OPEN, CLOSED (sold), EXPIRED_WORTHLESS, EXERCISED, or
    UNRESOLVED when it is past expiration with contracts still open and no outcome recorded.
    An unresolved expiration is never assumed to be worthless."""
    if pos["instrument_type"] != "OPTION" or not pos.get("legs"):
        return {"state": None}
    events = [e["event"] for e in pos.get("option_events", []) if e["event"] != "CONTRACT_ADJUSTED"]
    expired = (now or datetime.now(timezone.utc)) >= expiration_close(pos["legs"][0]["expiration"])
    open_qty = pos["ledger"]["open_quantity"]
    if open_qty > EPS:
        if expired:
            return {"state": "UNRESOLVED", "text": "This option is past its expiration with contracts still open. Record "
                                                   "how it ended: expired worthless, or exercised."}
        return {"state": "OPEN"}
    if "EXERCISED" in events:
        return {"state": "EXERCISED"}
    if "EXPIRED_WORTHLESS" in events:
        return {"state": "EXPIRED_WORTHLESS"}
    return {"state": "CLOSED" if pos["ledger"]["fills"] else "PLANNED"}


# --- the classic option journal ------------------------------------------------------------------

LEGACY_STATUS = {"planned": "PLANNED", "open": "OPEN", "closed": "CLOSED", "cancelled": "CANCELLED"}


def _has_table(cur: sqlite3.Cursor, name: str) -> bool:
    return cur.execute("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (name,)).fetchone() is not None


def sync_legacy_trade(cur: sqlite3.Cursor, trade_id: int) -> int | None:
    """Mirror one classic journal entry (a bought call or put) as a unified position, so the
    Positions screen, portfolio risk and analytics include it. The classic entry stays the
    record.

    The mirror's fills are reconciled with the journal, not rebuilt: a fill that already matches
    is left alone, so its id and anything tied to it (snapshots, corrections) stay valid, and
    running this any number of times changes nothing once the two agree. A fill whose details
    differ from the journal is updated in place and the change is recorded as a JOURNAL_SYNC
    correction. Uses only the tables of migration 1, because migration 1 runs it on older
    databases; snapshots for mirrors are added afterwards by snapshots.py."""
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
        cur.execute("UPDATE positions SET thesis = ?, notes = ? WHERE id = ? AND (thesis != ? OR notes != ?)",
                    (t.get("thesis") or "", t.get("notes") or "", pid, t.get("thesis") or "", t.get("notes") or ""))
    wanted = []
    if t.get("entry_price") is not None:
        wanted.append(("BUY", float(t["contracts"]), float(t["entry_price"]), t.get("entry_at") or t["created_at"]))
        if t.get("exit_price") is not None:
            wanted.append(("SELL", float(t["contracts"]), float(t["exit_price"]), t.get("exit_at") or now_iso()))
    have = {r[1]: r for r in cur.execute("SELECT id, side, quantity, price, fees, executed_at FROM executions "
                                         "WHERE position_id = ? ORDER BY executed_at, id", (pid,)).fetchall()}
    audit = _has_table(cur, "execution_corrections")
    pos = _row(cur, pid)
    led_before = account(pos["direction"], pos["multiplier"], _fills(cur, pid), allow_reopen=True)
    changes = []
    for side, qty, price, at in wanted:
        old = have.pop(side, None)
        if old is None:
            cur.execute("INSERT INTO executions (position_id, side, quantity, price, fees, executed_at, source, note, "
                        "created_at) VALUES (?, ?, ?, ?, 0, ?, 'IMPORT', 'from the option journal', ?)",
                        (pid, side, qty, price, at, now_iso()))
        elif (old[2], old[3], old[4], old[5]) != (qty, price, 0.0, at):
            cur.execute("UPDATE executions SET quantity = ?, price = ?, fees = 0, executed_at = ? WHERE id = ?",
                        (qty, price, at, old[0]))
            changes.append(("UPDATE", old[0], {"side": side, "quantity": old[2], "price": old[3], "fees": old[4],
                                               "executed_at": old[5]},
                            {"side": side, "quantity": qty, "price": price, "fees": 0.0, "executed_at": at}))
    for side, old in have.items():                     # the journal no longer has this fill
        cur.execute("DELETE FROM executions WHERE id = ?", (old[0],))
        changes.append(("DELETE", old[0], {"side": side, "quantity": old[2], "price": old[3], "fees": old[4],
                                           "executed_at": old[5]}, {}))
    if t["status"] == "cancelled" and pos["status"] == "PLANNED" and not wanted:
        cur.execute("UPDATE positions SET status = 'CANCELLED' WHERE id = ?", (pid,))
    led = recompute(cur, pid, correction=bool(changes))
    if changes and audit:
        after = _row(cur, pid)["status"]
        for action, eid, previous, new in changes:
            record_correction(cur, pid, eid, action, previous, new, "The option journal entry changed", "JOURNAL_SYNC",
                              _ledger_summary(led_before, pos["status"]), _ledger_summary(led, after))
    if plan.get("stop_loss_pct") and t.get("entry_price"):
        cur.execute("UPDATE positions SET notes = TRIM(notes || ' ' || ?) WHERE id = ? AND notes NOT LIKE '%Exit plan:%'",
                    (f"Exit plan: take profit at +{plan.get('profit_target_pct', 50):g}%, stop at -{plan['stop_loss_pct']:g}% "
                     "of the premium.", pid))
    return pid
