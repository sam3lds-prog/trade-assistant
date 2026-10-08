"""Trade journal: planned and executed trades, limit-price attempts, and what they teach.

Trades are entered at the broker by hand; this records what was planned, what was
actually done, and how it turned out, so the app's suggestions can be checked
against results.
"""

from __future__ import annotations

import json
import math
import statistics
from datetime import datetime, timezone
from typing import Any

from .db import Database
from .positions import sync_legacy_trade
from .optionmath import NY
from .pricer import BUCKETS, bucket_for

MIN_CALIBRATION_ATTEMPTS = 10
TARGET_FILL_RATE = 0.70


class JournalError(ValueError):
    pass


def _finite(name: str, value: Any) -> float:
    """The journal's numbers are prices and seconds the user typed: refuse NaN and infinity here
    too, not only in the HTTP layer, so nothing can reach the ledger without being checked."""
    try:
        f = float(value)
    except (TypeError, ValueError) as exc:
        raise JournalError(f"{name} must be a number") from exc
    if not math.isfinite(f):
        raise JournalError(f"{name} must be a finite number")
    return f


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _row_to_trade(row) -> dict[str, Any]:
    trade = dict(row)
    for key in ("plan_json", "quote_json", "scorecard_json", "signals_json", "overrides_json"):
        trade[key.removesuffix("_json")] = json.loads(trade.pop(key) or "null")
    return trade


class Journal:
    def __init__(self, db: Database):
        self.db = db

    # --- writing ---------------------------------------------------------
    def create(self, *, data_source: str, symbol: str, option_symbol: str, option_type: str, strike: float,
               expiration: str, contracts: int, thesis: str, planned_limit: float, max_limit: float,
               plan: dict[str, Any], quote: dict[str, Any], scorecard: list[dict[str, Any]],
               signals: list[Any], overrides: list[str]) -> int:
        if isinstance(contracts, bool) or not isinstance(contracts, int) or contracts < 1:
            raise JournalError("contracts must be a whole number, at least 1")
        strike = _finite("strike", strike)
        for name, value in (("planned limit", planned_limit), ("most you will pay", max_limit)):
            if value is not None and _finite(name, value) <= 0:
                raise JournalError(f"{name} must be above zero")
        with self.db.tx() as cur:
            cur.execute(
                "INSERT INTO trades (created_at, data_source, symbol, option_symbol, option_type, strike, "
                "expiration, contracts, thesis, status, planned_limit, max_limit, plan_json, quote_json, "
                "scorecard_json, signals_json, overrides_json) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'planned', ?, ?, ?, ?, ?, ?, ?)",
                (_now(), data_source, symbol, option_symbol, option_type, strike, expiration, contracts,
                 thesis, planned_limit, max_limit, json.dumps(plan), json.dumps(quote), json.dumps(scorecard),
                 json.dumps(signals), json.dumps(overrides)))
            trade_id = int(cur.lastrowid)
            sync_legacy_trade(cur, trade_id)          # keep the unified positions in step
            return trade_id

    def get(self, trade_id: int) -> dict[str, Any]:
        row = self.db.one("SELECT * FROM trades WHERE id = ?", (trade_id,))
        if row is None:
            raise JournalError(f"No journal entry {trade_id}")
        trade = _row_to_trade(row)
        trade["attempts"] = [dict(r) for r in self.db.query(
            "SELECT * FROM attempts WHERE trade_id = ? ORDER BY id", (trade_id,))]
        return trade

    def list(self, limit: int = 200) -> list[dict[str, Any]]:
        rows = self.db.query("SELECT * FROM trades ORDER BY id DESC LIMIT ?", (limit,))
        trades = [_row_to_trade(r) for r in rows]
        counts = {r["trade_id"]: r["n"] for r in self.db.query(
            "SELECT trade_id, COUNT(*) AS n FROM attempts GROUP BY trade_id")}
        for t in trades:
            t["attempt_count"] = counts.get(t["id"], 0)
        return trades

    def log_attempt(self, trade_id: int, *, side: str, bid: float, ask: float, limit_price: float,
                    filled: bool, seconds_to_fill: float | None = None) -> dict[str, Any]:
        """Record one limit price tried at the broker and whether it filled.

        A filled buy opens the trade at that price; a filled sell closes it.
        """
        trade = self.get(trade_id)
        if side not in ("buy", "sell"):
            raise JournalError("side must be buy or sell")
        bid, ask, limit_price = _finite("bid", bid), _finite("ask", ask), _finite("limit price", limit_price)
        if seconds_to_fill is not None and _finite("seconds to fill", seconds_to_fill) < 0:
            raise JournalError("seconds to fill cannot be negative")
        if ask < bid or ask <= 0 or limit_price <= 0:
            raise JournalError("Enter the bid, ask and limit price you saw when you placed the order")
        if side == "buy" and limit_price > ask + 0.005:
            raise JournalError("Your limit is above the ask you entered. A buy fills at the ask or better, "
                               "so check the three prices.")
        if side == "sell" and limit_price < bid - 0.005:
            raise JournalError("Your limit is below the bid you entered. A sale fills at the bid or better, "
                               "so check the three prices.")
        if side == "buy" and trade["status"] != "planned":
            raise JournalError("This trade is already open or finished; buy attempts belong to planned trades")
        if side == "sell" and trade["status"] != "open":
            raise JournalError("Only an open trade can be sold")
        position = 1.0 if ask == bid else (limit_price - bid) / (ask - bid)
        if side == "sell":
            position = 1.0 - position
        position = min(max(position, -0.5), 1.5)
        overrides = list(trade["overrides"])
        if filled and side == "buy":
            # The journal records what happened at the broker, so a fill outside the plan is
            # accepted, but it is marked as a broken rule so the results show what it cost.
            if trade["max_limit"] is not None and limit_price > trade["max_limit"] + 0.005:
                overrides.append("paid_above_plan")
            overrides = sorted(set(overrides))
        with self.db.tx() as cur:
            cur.execute("INSERT INTO attempts (trade_id, at, side, bid, ask, limit_price, position, filled, "
                        "seconds_to_fill) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                        (trade_id, _now(), side, bid, ask, limit_price, position, int(filled), seconds_to_fill))
            if filled and side == "buy":
                cur.execute("UPDATE trades SET status = 'open', entry_price = ?, entry_at = ?, "
                            "overrides_json = ? WHERE id = ?",
                            (limit_price, _now(), json.dumps(overrides), trade_id))
            if filled and side == "sell":
                pnl = (limit_price - trade["entry_price"]) * 100 * trade["contracts"]
                cur.execute("UPDATE trades SET status = 'closed', exit_price = ?, exit_at = ?, pnl = ? "
                            "WHERE id = ?", (limit_price, _now(), pnl, trade_id))
            if filled:
                sync_legacy_trade(cur, trade_id)
        return self.get(trade_id)

    def close_at(self, trade_id: int, exit_price: float, note: str = "") -> dict[str, Any]:
        """Close an open trade without logging a limit attempt (for example, it expired worthless)."""
        trade = self.get(trade_id)
        if trade["status"] != "open":
            raise JournalError("Only an open trade can be closed")
        exit_price = _finite("exit price", exit_price)
        if exit_price < 0:
            raise JournalError("exit price cannot be negative")
        pnl = (exit_price - trade["entry_price"]) * 100 * trade["contracts"]
        with self.db.tx() as cur:
            cur.execute("UPDATE trades SET status = 'closed', exit_price = ?, exit_at = ?, pnl = ?, "
                        "notes = TRIM(notes || ' ' || ?) WHERE id = ?",
                        (exit_price, _now(), pnl, note, trade_id))
            sync_legacy_trade(cur, trade_id)
        return self.get(trade_id)

    def cancel(self, trade_id: int, note: str = "") -> dict[str, Any]:
        trade = self.get(trade_id)
        if trade["status"] != "planned":
            raise JournalError("Only a planned trade can be cancelled")
        with self.db.tx() as cur:
            cur.execute("UPDATE trades SET status = 'cancelled', notes = TRIM(notes || ' ' || ?) WHERE id = ?",
                        (note, trade_id))
            sync_legacy_trade(cur, trade_id)
        return self.get(trade_id)

    # --- reading for limits ----------------------------------------------
    def open_risk(self) -> float:
        """Premium paid on trades that are still open: the most they can still lose."""
        row = self.db.one("SELECT COALESCE(SUM(entry_price * 100 * contracts), 0) AS risk "
                          "FROM trades WHERE status = 'open'")
        return float(row["risk"])

    def planned_risk(self, symbol: str | None = None) -> float:
        """Premium committed by saved plans that have not been filled or cancelled, at the most
        each plan allows paying. Plans on options that have already expired no longer count."""
        today = datetime.now(NY).date().isoformat()
        sql = ("SELECT COALESCE(SUM(max_limit * 100 * contracts), 0) AS risk FROM trades "
               "WHERE status = 'planned' AND expiration >= ?")
        params: list[Any] = [today]
        if symbol:
            sql += " AND symbol = ?"
            params.append(symbol)
        return float(self.db.one(sql, params)["risk"])

    def committed_risk(self, symbol: str | None = None) -> float:
        """Open positions plus live plans: what the next ticket must fit beside. With `symbol`,
        only the part in that underlying."""
        if symbol is None:
            return self.open_risk() + self.planned_risk()
        row = self.db.one("SELECT COALESCE(SUM(entry_price * 100 * contracts), 0) AS risk "
                          "FROM trades WHERE status = 'open' AND symbol = ?", (symbol,))
        return float(row["risk"]) + self.planned_risk(symbol)

    def realized_today(self) -> float:
        """Realised profit or loss on trades closed today (New York date)."""
        today = datetime.now(NY).date()
        total = 0.0
        for row in self.db.query("SELECT exit_at, pnl FROM trades WHERE status = 'closed' AND pnl IS NOT NULL"):
            closed = datetime.fromisoformat(row["exit_at"]).astimezone(NY).date()
            if closed == today:
                total += row["pnl"]
        return total

    # --- learning from results --------------------------------------------
    def calibration(self, side: str = "buy") -> dict[str, Any]:
        """Fill rates by how far across the spread the limit sat."""
        buckets = [{"label": label, "attempts": 0, "fills": 0, "fill_rate": None, "median_seconds": None,
                    "_secs": []} for _, _, label in BUCKETS]
        for row in self.db.query("SELECT position, filled, seconds_to_fill FROM attempts WHERE side = ?", (side,)):
            b = buckets[bucket_for(row["position"])]
            b["attempts"] += 1
            if row["filled"]:
                b["fills"] += 1
                if row["seconds_to_fill"] is not None:
                    b["_secs"].append(row["seconds_to_fill"])
        suggestion = None
        for b in buckets:
            secs = sorted(b.pop("_secs"))
            if b["attempts"]:
                b["fill_rate"] = b["fills"] / b["attempts"]
            if secs:
                b["median_seconds"] = statistics.median(secs)
            if (suggestion is None and b["attempts"] >= MIN_CALIBRATION_ATTEMPTS
                    and b["fill_rate"] >= TARGET_FILL_RATE):
                suggestion = b["label"]
        total = sum(b["attempts"] for b in buckets)
        if suggestion:
            note = (f"In your own orders, limits placed {suggestion} filled at least "
                    f"{TARGET_FILL_RATE:.0%} of the time. That is the cheapest zone that has worked for you.")
        elif total:
            note = (f"{total} attempt{'s' if total != 1 else ''} logged. A zone needs "
                    f"{MIN_CALIBRATION_ATTEMPTS} attempts before its fill rate means much.")
        else:
            note = "No attempts logged yet. Log each limit price you try, filled or not, to see what works."
        return {"side": side, "buckets": buckets, "total_attempts": total, "note": note}

    def stats(self) -> dict[str, Any]:
        closed = [_row_to_trade(r) for r in self.db.query(
            "SELECT * FROM trades WHERE status = 'closed' AND pnl IS NOT NULL ORDER BY id")]

        def summarise(trades: list[dict[str, Any]]) -> dict[str, Any]:
            if not trades:
                return {"trades": 0, "wins": 0, "win_rate": None, "total_pnl": 0.0, "avg_pnl": None}
            wins = sum(1 for t in trades if t["pnl"] > 0)
            total = sum(t["pnl"] for t in trades)
            return {"trades": len(trades), "wins": wins, "win_rate": wins / len(trades),
                    "total_pnl": total, "avg_pnl": total / len(trades)}

        by_signal: dict[str, list[dict[str, Any]]] = {}
        for t in closed:
            for sig in t.get("signals") or []:
                name = sig["name"] if isinstance(sig, dict) else str(sig)
                by_signal.setdefault(name, []).append(t)
        with_override = [t for t in closed if t.get("overrides")]
        without_override = [t for t in closed if not t.get("overrides")]
        status_counts = {r["status"]: r["n"] for r in self.db.query(
            "SELECT status, COUNT(*) AS n FROM trades GROUP BY status")}
        return {
            "overall": summarise(closed),
            "followed_rules": summarise(without_override),
            "overrode_a_limit": summarise(with_override),
            "by_signal": [{"signal": name, **summarise(ts)} for name, ts in sorted(by_signal.items())],
            "status_counts": status_counts,
            "open_risk": self.open_risk(),
            "planned_risk": self.planned_risk(),
            "realized_today": self.realized_today(),
        }
