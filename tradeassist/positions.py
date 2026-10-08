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
    existing = _fills(cur, position_id)
    before = account(pos["direction"], pos["multiplier"], existing, allow_reopen=True)
    fills = sorted(existing + [candidate], key=lambda f: (f.executed_at, f.id or 10**12))
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
    fills_before = _fills(cur, position_id)
    led_before = account(pos["direction"], pos["multiplier"], fills_before, allow_reopen=True)
    fills_after = sorted([Fill(new["side"], new["quantity"], new["price"], new["fees"], new["executed_at"], f.id)
                          if f.id == execution_id else f for f in fills_before],
                         key=lambda f: (f.executed_at, f.id or 10**12))
    led_after = account(pos["direction"], pos["multiplier"], fills_after, allow_reopen=True)
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
    led = account(pos["direction"], pos["multiplier"], [Fill(e["side"], e["quantity"], e["price"], e["fees"],
                                                              e["executed_at"], e["id"]) for e in pos["executions"]],
                  allow_reopen=True)
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
