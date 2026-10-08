"""The V2 position desk: unified positions with live marks, plans, snapshots and thesis checks.

This sits beside the classic TradeAssistant service (service.py) and uses its market data,
database and settings. Accounting lives in positions.py; this module adds what needs
market data: current marks, the snapshot of the environment at entry and exit, and whether
the trade's thesis still holds.
"""

from __future__ import annotations

import inspect
import logging
import math
from datetime import date, datetime, timezone
from typing import Any, Callable

import pandas as pd

from .optionmath import NY, days_to_expiry
from .market import complete_only
from .positions import (DIRECTIONS, OptionLeg, PositionError, add_execution, cancel, correct_execution, create,
                        list_ids, now_iso, read, update_plan)
from .providers.base import DataError
from .snapshots import LIVE_WINDOW, SnapshotService, close_of, cutoff_session, when
from .structure import STATE_WORDS
from .versions import SIGNAL_ENGINE_VERSION

log = logging.getLogger(__name__)

OPEN_STATES = ("OPEN", "PARTIALLY_CLOSED")


def finite(v: Any) -> float | None:
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if math.isfinite(f) else None


def bias_of(pos: dict[str, Any]) -> int:
    """+1 when the position profits from the stock rising, -1 when it profits from a fall."""
    if pos["instrument_type"] == "OPTION" and pos.get("legs"):
        return 1 if pos["legs"][0]["option_type"] == "CALL" else -1
    return 1 if pos["direction"] == "LONG" else -1


def describe(pos: dict[str, Any]) -> str:
    qty = pos["quantity"]
    if pos["instrument_type"] == "OPTION" and pos.get("legs"):
        leg = pos["legs"][0]
        return (f"Long {qty:g} {pos['symbol']} {leg['expiration']} {leg['strike']:g} "
                f"{leg['option_type'].lower()}{'s' if qty != 1 else ''}")
    verb = "Long" if pos["direction"] == "LONG" else "Short"
    return f"{verb} {qty:,.0f} {pos['symbol']} shares" if qty == int(qty) else f"{verb} {qty:g} {pos['symbol']} shares"


def check_levels(direction: str, entry: float, invalidation: float | None, t1: float | None, t2: float | None) -> None:
    """A stock plan's levels must sit on the right sides of the entry: for a long, the invalidation
    below and the targets above; for a short, the other way round."""
    sign = 1 if direction == "LONG" else -1
    word = "long" if sign > 0 else "short"
    if invalidation is not None and sign * (entry - invalidation) <= 0:
        raise PositionError(f"For a {word} the invalidation price must be {'below' if sign > 0 else 'above'} the entry "
                            f"({entry:,.2f}); {invalidation:,.2f} would mean it is wrong from the start.")
    for name, level in (("target 1", t1), ("target 2", t2)):
        if level is not None and sign * (level - entry) <= 0:
            raise PositionError(f"For a {word}, {name} must be {'above' if sign > 0 else 'below'} the entry ({entry:,.2f}).")


class Desk:
    def __init__(self, svc: Any):
        self.svc = svc
        self.db = svc.db
        self.market = svc.market
        # Later parts of V2 add to the entry snapshot (trend state, regime, scores) through these.
        self.snapshot_extras: list[Callable[[str, dict[str, Any]], dict[str, Any]]] = []
        self.snapshots = SnapshotService(self.db, self.capture)
        journal = getattr(svc, "journal", None)
        if journal is not None and hasattr(journal, "listeners"):
            journal.listeners[:] = [f for f in journal.listeners if getattr(f, "__name__", "") != "_journal_changed"]
            journal.listeners.append(self._journal_changed)

    def _journal_changed(self, trade_id: int) -> None:
        """A classic journal entry changed: bring its mirror's snapshots up to date (snapshots.py)."""
        row = self.db.one("SELECT id FROM positions WHERE legacy_trade_id = ?", (trade_id,))
        if row is not None:
            self._reconcile(row["id"])

    def _reconcile(self, position_id: int) -> dict[str, Any] | None:
        """Snapshots never decide whether a fill is recorded: a failure here is logged and shows
        up in the data health view, and the fill stands."""
        try:
            return self.snapshots.reconcile(position_id)
        except Exception as exc:          # noqa: BLE001 - market data can fail in many ways
            log.warning("Snapshots for position %s could not be brought up to date: %s", position_id, exc)
            return None

    # --- marks ---------------------------------------------------------------------
    def mark(self, pos: dict[str, Any]) -> dict[str, Any]:
        """The current price of what the position holds, and of the stock underneath."""
        symbol = pos["symbol"]
        if pos["instrument_type"] == "STOCK":
            q = self.market.quote(symbol)
            price = self.market.spot_from_quote(q, symbol)
            return {"price": price, "underlying": price, "prev_close": finite(q.get("prevclose")),
                    "as_of": q.get("trade_time") or q.get("bid_time"), "source": self.market.info.label}
        leg = pos["legs"][0]
        if leg["expiration"] < datetime.now(NY).date().isoformat():
            q = self.market.quote(symbol)
            spot = self.market.spot_from_quote(q, symbol)
            intrinsic = max((spot - leg["strike"]) if leg["option_type"] == "CALL" else (leg["strike"] - spot), 0.0)
            return {"price": intrinsic, "underlying": spot, "prev_close": None, "as_of": None, "expired": True,
                    "source": "intrinsic value: this option has expired, so record how it ended"}
        analysis = self.svc._chain_for(symbol, leg["expiration"])
        row = analysis.find_by_terms(leg["option_type"].lower(), float(leg["strike"]))
        if row is None:
            raise DataError(f"{symbol} {leg['expiration']} {leg['strike']:g} {leg['option_type'].lower()} is not in the chain")
        price, source = row.get("mid"), "midpoint"
        if price is None:
            price, source = row.get("model_value"), "model value (no two-sided quote)"
        return {"price": finite(price), "underlying": analysis.spot, "prev_close": finite(row.get("prevclose")),
                "as_of": row.get("bid_time"), "source": source, "bid": row.get("bid"), "ask": row.get("ask"),
                "delta": row.get("delta"), "theta": row.get("theta"), "vega": row.get("vega"), "gamma": row.get("gamma"),
                "iv": row.get("iv"), "dte": days_to_expiry(leg["expiration"]),
                # What the model needs to reprice this contract consistently with the chain.
                "model_vol": finite(row.get("model_vol") or row.get("iv")),
                "growth": (math.log(analysis.forward / analysis.spot) / analysis.t) if analysis.t > 0 else 0.0,
                "rate": analysis.rate}

    # --- views ------------------------------------------------------------------------
    def view(self, position_id: int, with_mark: bool = True) -> dict[str, Any]:
        with self.db.tx() as cur:
            pos = read(cur, position_id)
        pos["description"] = describe(pos)
        pos["bias"] = bias_of(pos)
        led = pos["ledger"]
        pos["mark"] = None
        if with_mark and pos["status"] in OPEN_STATES:
            try:
                pos["mark"] = self.mark(pos)
            except (DataError, ValueError) as exc:
                pos["mark"] = {"price": None, "error": str(exc)}
        price = pos["mark"]["price"] if pos["mark"] else None
        sign = 1 if pos["direction"] == "LONG" else -1
        unreal = None
        if led["open_quantity"] > 0 and price is not None and led["cost_basis"] is not None:
            unreal = sign * (price - led["cost_basis"]) * led["open_quantity"] * pos["multiplier"]
        elif led["open_quantity"] == 0:
            unreal = 0.0
        total = led["realized_pnl"] + unreal if unreal is not None else None
        risk = led.get("initial_risk")
        pos["pnl"] = {"realized": led["realized_pnl"], "unrealized": unreal, "total": total, "fees": led["fees"],
                      "r_multiple": (total / risk) if (total is not None and risk) else None, "initial_risk": risk,
                      "market_value": (sign * price * led["open_quantity"] * pos["multiplier"]) if price is not None else None}
        pos["thesis_check"] = self.thesis_check(pos)
        if pos["opened_at"]:
            end = pos["closed_at"] or now_iso()
            pos["days_held"] = (datetime.fromisoformat(end) - datetime.fromisoformat(pos["opened_at"])).days
        return pos

    def thesis_check(self, pos: dict[str, Any]) -> dict[str, Any]:
        """Whether the stock is still on the right side of the plan's invalidation and targets."""
        underlying = (pos.get("mark") or {}).get("underlying")
        inv, t1, t2, b = pos.get("invalidation_price"), pos.get("target_1"), pos.get("target_2"), bias_of(pos)
        if pos["status"] not in OPEN_STATES + ("PLANNED",):
            return {"status": "CLOSED" if pos["status"] == "CLOSED" else pos["status"], "text": ""}
        if underlying is None:
            return {"status": "UNKNOWN", "text": "No current price to check the plan against."}
        side = "above" if b > 0 else "below"
        if inv is None:
            return {"status": "NO_INVALIDATION", "text": "No invalidation price is set. Add the stock price that would "
                                                         "prove the idea wrong."}
        if b * (underlying - inv) <= 0:
            return {"status": "INVALIDATED", "text": f"{pos['symbol']} at {underlying:,.2f} is through the invalidation "
                                                     f"price of {inv:,.2f}. The plan says to exit on a daily close there."}
        for name, level in (("TARGET_2", t2), ("TARGET_1", t1)):
            if level is not None and b * (underlying - level) >= 0:
                return {"status": f"{name}_REACHED", "text": f"{pos['symbol']} at {underlying:,.2f} has reached "
                                                              f"{name.replace('_', ' ').lower()} ({level:,.2f})."}
        room = abs(underlying - inv) / underlying * 100
        return {"status": "INTACT", "text": f"{pos['symbol']} at {underlying:,.2f} is {room:.1f}% {side} the invalidation "
                                            f"price of {inv:,.2f}."}

    def list(self, status: str = "all") -> dict[str, Any]:
        groups = {"open": OPEN_STATES, "planned": ("PLANNED",), "closed": ("CLOSED",), "cancelled": ("CANCELLED",),
                  "all": None}
        if status not in groups:
            raise PositionError("status must be open, planned, closed, cancelled or all")
        with self.db.tx() as cur:
            ids = list_ids(cur, groups[status])
        return {"positions": [self.view(pid) for pid in ids]}

    # --- changes -------------------------------------------------------------------------
    def create(self, req: dict[str, Any]) -> dict[str, Any]:
        symbol = self.svc._symbol(req["symbol"])
        instrument = str(req.get("instrument_type", "STOCK")).upper()
        direction = str(req.get("direction", "LONG")).upper()
        if direction not in DIRECTIONS:
            raise PositionError("direction must be LONG or SHORT")
        leg = None
        if instrument == "OPTION":
            opt = req.get("option") or {}
            kind = str(opt.get("option_type", "")).upper()
            if kind not in ("CALL", "PUT") or not opt.get("expiration") or not opt.get("strike"):
                raise PositionError("An option position needs option_type (CALL or PUT), strike and expiration")
            try:
                date.fromisoformat(str(opt["expiration"]))
            except ValueError as exc:
                raise PositionError("expiration must look like 2026-11-20") from exc
            from .providers.robinhood import occ_symbol
            leg = OptionLeg(kind, float(opt["strike"]), str(opt["expiration"]),
                            occ_symbol(symbol, str(opt["expiration"]), kind.lower(), float(opt["strike"])))
        entry = req.get("entry") or None
        if instrument == "STOCK" and entry:
            check_levels(direction, float(entry["price"]), req.get("invalidation_price"), req.get("target_1"),
                         req.get("target_2"))
        with self.db.tx() as cur:
            pid = create(cur, symbol=symbol, instrument_type=instrument, direction=direction,
                         quantity=float(req["quantity"]), thesis=req.get("thesis") or "",
                         setup_type=req.get("setup_type") or "", intended_horizon=req.get("intended_horizon"),
                         invalidation_price=req.get("invalidation_price"), target_1=req.get("target_1"),
                         target_2=req.get("target_2"), risk_amount=req.get("risk_amount"),
                         initial_risk_per_unit=req.get("initial_risk_per_unit"), option=leg,
                         data_source=self.market.info.mode, notes=req.get("notes") or "")
            if entry:
                side = entry.get("side") or ("BUY" if direction == "LONG" else "SELL_SHORT")
                add_execution(cur, pid, side=side, quantity=float(entry.get("quantity") or req["quantity"]),
                              price=float(entry["price"]), fees=float(entry.get("fees") or 0),
                              executed_at=entry.get("executed_at"), source="MANUAL", note=entry.get("note") or "")
        if entry:
            self._reconcile(pid)
        else:
            try:
                self.snapshots.take(pid, "PLAN")
            except Exception as exc:      # noqa: BLE001
                log.warning("Plan snapshot for position %s failed: %s", pid, exc)
        return self.view(pid)

    def add_execution(self, position_id: int, req: dict[str, Any]) -> dict[str, Any]:
        with self.db.tx() as cur:
            before = read(cur, position_id)
            if before.get("legacy_trade_id"):
                raise PositionError("This position mirrors an entry in the option journal. Record its fills there, "
                                    "so the two stay in step.")
            if before["instrument_type"] == "STOCK" and before["ledger"]["fills"] == 0:
                check_levels(before["direction"], float(req["price"]), before.get("invalidation_price"),
                             before.get("target_1"), before.get("target_2"))
            add_execution(cur, position_id, side=req["side"], quantity=req["quantity"], price=req["price"],
                          fees=req.get("fees") or 0, executed_at=req.get("executed_at"),
                          source=req.get("source") or "MANUAL", note=req.get("note") or "",
                          external_id=req.get("external_id"))
        self._reconcile(position_id)
        return self.view(position_id)

    def correct(self, position_id: int, execution_id: int, changes: dict[str, Any], reason: str) -> dict[str, Any]:
        """An audited correction of one fill (positions.correct_execution), then the snapshots that
        depended on it (snapshots.py)."""
        with self.db.tx() as cur:
            done = correct_execution(cur, position_id, execution_id, changes, reason, "USER")
        self._reconcile(position_id)
        out = self.view(position_id)
        out["correction"] = done
        return out

    def cancel(self, position_id: int, note: str = "") -> dict[str, Any]:
        with self.db.tx() as cur:
            pos = read(cur, position_id)
            if pos.get("legacy_trade_id"):
                raise PositionError("Cancel this one in the option journal, where it was planned.")
            cancel(cur, position_id, note)
        return self.view(position_id, with_mark=False)

    def update(self, position_id: int, changes: dict[str, Any]) -> dict[str, Any]:
        with self.db.tx() as cur:
            pos = read(cur, position_id)
            basis = pos["ledger"]["cost_basis"]
            if pos["instrument_type"] == "STOCK" and basis and pos["ledger"]["open_quantity"] > 0:
                merged = {k: changes.get(k, pos.get(k)) for k in ("invalidation_price", "target_1", "target_2")}
                check_levels(pos["direction"], basis, merged["invalidation_price"], merged["target_1"], merged["target_2"])
            update_plan(cur, position_id, changes)
        return self.view(position_id)

    # --- snapshots ---------------------------------------------------------------------------
    def capture(self, pos: dict[str, Any], execution: dict[str, Any] | None) -> tuple[dict, dict, dict]:
        """Values, raw details and provenance for a snapshot (snapshots.py). With no fill, or a fill
        recorded within LIVE_WINDOW of when it happened, the market now; for a fill recorded later,
        the market as of the last daily bar that had finished before it."""
        leg = pos["legs"][0] if pos.get("legs") else None
        now = datetime.now(timezone.utc)
        executed = when(execution["executed_at"]) if execution else None
        if executed is None or now - executed <= LIVE_WINDOW:
            values, raw = self._live_values(pos["symbol"], leg)
            stamp = now.isoformat(timespec="seconds")
            return values, raw, {"method": "LIVE", "capture_timing": "CONTEMPORANEOUS", "uses_later_data": False,
                                 "observation_cutoff": stamp, "captured_at": stamp, "origin": "desk"}
        session = cutoff_session(executed)
        values, raw = self._point_in_time_values(pos["symbol"], session)
        if execution is not None and pos["instrument_type"] == "STOCK":
            values["underlying_price"] = float(execution["price"])          # observed at the fill itself
            raw["underlying_price_basis"] = "the fill price"
        elif "underlying_price" in values:
            raw["underlying_price_basis"] = "the last close before the fill"
        used = values.get("data_timestamp")
        cutoff = close_of(date.fromisoformat(used)) if used else close_of(session)
        return values, raw, {"method": "PRIOR_BAR", "capture_timing": "RETROSPECTIVE",
                             "uses_later_data": bool(when(cutoff) and when(cutoff) > executed),
                             "observation_cutoff": cutoff, "captured_at": now.isoformat(timespec="seconds"),
                             "origin": "desk", "session_cutoff": session.isoformat()}

    def _live_values(self, symbol: str, leg: dict[str, Any] | None) -> tuple[dict[str, Any], dict[str, Any]]:
        """What the market looks like now."""
        values: dict[str, Any] = {"signal_engine_version": SIGNAL_ENGINE_VERSION, "data_source": self.market.info.mode}
        raw: dict[str, Any] = {"errors": [], "taken_at": now_iso()}
        try:
            q = self.market.quote(symbol)
            values["underlying_price"] = self.market.spot_from_quote(q, symbol)
        except (DataError, ValueError) as exc:
            raw["errors"].append(f"quote: {exc}")
        try:
            df = self.market.enriched(symbol, 3)
            last = df.iloc[-1]
            for col, key in INDICATOR_KEYS:
                values[key] = finite(last.get(col))
            values["structure_state"] = STATE_WORDS.get(int(last.get("struct_state", 0)), "range")
            values["data_timestamp"] = last["date"].strftime("%Y-%m-%d")
            if "bar_complete" in df.columns:
                values["bar_status"] = "COMPLETE" if bool(last["bar_complete"]) else "PARTIAL"
            raw["indicators"] = {k: values.get(k) for _, k in INDICATOR_KEYS}
        except (DataError, ValueError, KeyError, IndexError) as exc:
            raw["errors"].append(f"history: {exc}")
        for etf, key in (("SPY", "spy_price"), ("QQQ", "qqq_price")):
            try:
                values[key] = self.market.spot(etf)
            except (DataError, ValueError) as exc:
                raw["errors"].append(f"{etf}: {exc}")
        try:
            vix = self.market.provider.quotes(["VIX"]).get("VIX")
            values["vix"] = finite((vix or {}).get("last"))
        except (DataError, ValueError):
            values["vix"] = None
        if leg is not None:
            try:
                analysis = self.svc._chain_for(symbol, leg["expiration"])
                row = analysis.find_by_terms(leg["option_type"].lower(), float(leg["strike"])) or {}
                for key in ("iv", "delta", "gamma", "theta", "vega", "bid", "ask", "mid", "spread_pct"):
                    values[key] = finite(row.get(key))
                values["dte"] = days_to_expiry(leg["expiration"])
                values["iv_rank"] = finite(self.svc.iv_rank(symbol).get("rank"))
            except (DataError, ValueError) as exc:
                raw["errors"].append(f"option: {exc}")
        for extra in self.snapshot_extras:
            try:
                more = extra(symbol, values)
                raw.setdefault("analysis", {}).update(more.pop("_raw", {}))
                values.update(more)
            except (DataError, ValueError, KeyError) as exc:
                raw["errors"].append(f"{getattr(extra, '__name__', 'extra')}: {exc}")
        return values, raw

    def _point_in_time_values(self, symbol: str, session: date) -> tuple[dict[str, Any], dict[str, Any]]:
        """The market as of the close of `session` (or the last trading day before it), from
        finished daily bars only: indicators, swing structure, SPY and QQQ, and the trend and
        regime as the engines would have read them then. Option quotes and Greeks have no
        history here and are left out."""
        values: dict[str, Any] = {"signal_engine_version": SIGNAL_ENGINE_VERSION, "data_source": self.market.info.mode,
                                  "bar_status": "COMPLETE"}
        raw: dict[str, Any] = {"errors": [], "taken_at": now_iso(), "as_of": session.isoformat(),
                               "note": "Rebuilt after the fact from the last daily bar that had finished before the fill."}
        years = min(10.0, max(3.0, (datetime.now(NY).date() - session).days / 365 + 1.5))
        try:
            df = complete_only(self.market.enriched(symbol, years))
            df = df[df["date"] <= pd.Timestamp(session)]
            last = df.iloc[-1]
            values["underlying_price"] = finite(last["close"])
            for col, key in INDICATOR_KEYS:
                values[key] = finite(last.get(col))
            values["structure_state"] = STATE_WORDS.get(int(last.get("struct_state", 0)), "range")
            values["data_timestamp"] = last["date"].strftime("%Y-%m-%d")
        except (DataError, ValueError, KeyError, IndexError) as exc:
            raw["errors"].append(f"history: {exc}")
        as_of = date.fromisoformat(values["data_timestamp"]) if values.get("data_timestamp") else session
        for etf, key in (("SPY", "spy_price"), ("QQQ", "qqq_price")):
            try:
                b = complete_only(self.market.bars(etf, years))
                b = b[b["date"] <= pd.Timestamp(as_of)]
                values[key] = finite(b["close"].iloc[-1])
            except (DataError, IndexError) as exc:
                raw["errors"].append(f"{etf}: {exc}")
        if values.get("data_timestamp"):
            for extra in self.snapshot_extras:
                if len(inspect.signature(extra).parameters) < 3:
                    continue                          # an extra that only knows about now
                try:
                    more = extra(symbol, values, as_of)
                    raw.setdefault("analysis", {}).update(more.pop("_raw", {}))
                    values.update(more)
                except (DataError, ValueError, KeyError) as exc:
                    raw["errors"].append(f"{getattr(extra, '__name__', 'extra')}: {exc}")
        return values, raw

    def take_snapshot(self, position_id: int, snapshot_type: str = "REVIEW") -> dict[str, Any]:
        return self.snapshots.take(position_id, snapshot_type)

    def backfill_snapshots(self, dry_run: bool = False, upgrade_hindsight: bool = True) -> dict[str, Any]:
        return self.snapshots.backfill(upgrade_hindsight=upgrade_hindsight, dry_run=dry_run)


INDICATOR_KEYS = (("rsi", "rsi_14"), ("macd", "macd"), ("macd_signal", "macd_signal"), ("macd_hist", "macd_histogram"),
                  ("sma20", "sma_20"), ("sma50", "sma_50"), ("sma200", "sma_200"), ("ema20", "ema_20"), ("adx", "adx"),
                  ("atr", "atr"), ("vol_ratio", "volume_ratio"))
