"""Reconciliation: check every stored figure against the fills it comes from, and repair on request.

Fills (executions), with the adjustments and option events applied when they are replayed, are
the accounting record. A position's status, open quantity, average prices, realized profit,
fees and open and close times are a cache of them. `diagnose()` is read-only: it recomputes
each position from its fills and lists every difference, together with everything else a
person should look at before trusting the history:

    ledger_mismatches        stored figures that differ from the fills
    lifecycle_issues         positions opened again after closing (allowed before 2.1.1)
    invalid_states           a status the fills cannot produce, or a fill that no longer accounts
    missing_snapshots        lifecycle snapshots a position should have (snapshots.status)
    stale_snapshots          effective snapshots that no longer apply (an exit on a position open again)
    hindsight_snapshots      effective snapshots built from data after their fill
    possible_duplicate_fills fills with the same position, side, size, price and time and no broker id
    journal_mismatches       classic journal entries whose mirror's fills differ from them
    unresolved_options       bought options past expiration with contracts open
    market_data_gaps         open positions with no saved price history
    rule_breaks              rows that the 2.3 database rules would refuse
    database                 SQLite's own integrity and foreign-key checks

`repair()` changes derived figures, never a fill you recorded: it first copies the database
(backup.py; no copy, no repair), re-reads journal mirrors from the journal (their record; each
fill changed that way is kept as a JOURNAL_SYNC correction), recomputes each mismatched
position from its fills, and records what it changed in `maintenance_log`. Each item is
repaired in its own transaction, so one that cannot be (its fills no longer account) is
reported in `failed` and the rest go ahead. Positions opened again after closing are reported
and left as they are. Running it twice changes nothing the second time.
"""

from __future__ import annotations

import json
import logging
import math
from datetime import datetime, timezone
from typing import Any

from .events import emit
from .positions import (PositionError, _fills, _has_table, _row, account, lifecycle, read, recompute,
                        sync_legacy_trade)

STORED = ("status", "open_quantity", "average_entry_price", "average_exit_price", "realized_pnl", "fees", "opened_at",
          "closed_at")
TOLERANCE = 1e-6                      # dollars and shares; anything larger is a real difference


def _differs(a: Any, b: Any) -> bool:
    if isinstance(a, (int, float)) and isinstance(b, (int, float)):
        return not (math.isclose(float(a), float(b), rel_tol=1e-9, abs_tol=TOLERANCE))
    return a != b


def expected(cur, pid: int) -> dict[str, Any]:
    """What the stored figures should be, from the fills."""
    pos = _row(cur, pid)
    led = account(pos["direction"], pos["multiplier"], _fills(cur, pid), allow_reopen=True)
    status = led.status(cancelled=pos["status"] == "CANCELLED")
    return {"status": status, "open_quantity": led.open_quantity, "average_entry_price": led.average_entry_price,
            "average_exit_price": led.average_exit_price, "realized_pnl": led.realized_pnl, "fees": led.fees,
            "opened_at": led.opened_at, "closed_at": led.closed_at if status == "CLOSED" else None,
            "_issues": led.lifecycle_issues}


def diagnose(db: Any, market: Any = None) -> dict[str, Any]:
    """The read-only reconciliation report (see the module notes)."""
    from . import snapshots
    out: dict[str, list[dict[str, Any]]] = {k: [] for k in (
        "ledger_mismatches", "lifecycle_issues", "invalid_states", "possible_duplicate_fills", "journal_mismatches",
        "unresolved_options", "market_data_gaps", "rule_breaks")}
    counts = {"positions": 0, "open": 0, "closed": 0, "planned": 0, "cancelled": 0, "journal_mirrors": 0}
    ids = [r[0] for r in db.query("SELECT id FROM positions ORDER BY id")]
    with db.tx() as cur:
        for pid in ids:
            pos = _row(cur, pid)
            counts["positions"] += 1
            key = {"OPEN": "open", "PARTIALLY_CLOSED": "open", "CLOSED": "closed", "PLANNED": "planned",
                   "CANCELLED": "cancelled"}[pos["status"]]
            counts[key] += 1
            counts["journal_mirrors"] += 1 if pos.get("legacy_trade_id") else 0
            label = {"id": pid, "symbol": pos["symbol"], "status": pos["status"]}
            try:
                want = expected(cur, pid)
            except PositionError as exc:
                out["invalid_states"].append({**label, "problem": str(exc)})
                continue
            diffs = {k: {"stored": pos[k], "from_fills": want[k]} for k in STORED if _differs(pos[k], want[k])}
            if diffs:
                out["ledger_mismatches"].append({**label, "differences": diffs})
            for issue in want["_issues"]:
                out["lifecycle_issues"].append({**label, **issue,
                                                "action": "kept as recorded; record new trades as new positions"})
            if pos["quantity"] is None or pos["quantity"] <= 0 or pos["multiplier"] is None or pos["multiplier"] <= 0:
                out["rule_breaks"].append({**label, "problem": "planned quantity and multiplier must be above zero"})
        # The other 2.3 rules (schema_v2 migration 8), for rows written before they existed.
        for sql, problem in (
            ("SELECT position_id AS id FROM option_contracts WHERE strike IS NULL OR strike <= 0 OR multiplier IS NULL "
             "OR multiplier <= 0 OR length(expiration) != 10", "an option leg needs a strike and multiplier above zero "
                                                               "and a date for its expiration"),
            ("SELECT position_id AS id FROM executions WHERE executed_at IS NULL OR length(executed_at) < 19",
             "a fill has no full time stamp"),
            ("SELECT position_id AS id FROM position_adjustments WHERE kind = 'SPLIT' AND (ratio IS NULL OR ratio <= 0)",
             "a split has no ratio above zero")):
            if not _has_table(cur, sql.split(" FROM ")[1].split()[0]):
                continue
            for r in db.query(sql):
                pos = _row(cur, r["id"])
                out["rule_breaks"].append({"id": r["id"], "symbol": pos["symbol"], "status": pos["status"],
                                           "problem": problem})
        for r in db.query("SELECT position_id, side, quantity, price, executed_at, COUNT(*) AS n FROM executions "
                          "WHERE external_id IS NULL GROUP BY position_id, side, quantity, price, executed_at HAVING n > 1"):
            out["possible_duplicate_fills"].append({"position_id": r["position_id"], "side": r["side"],
                                                    "quantity": r["quantity"], "price": r["price"],
                                                    "executed_at": r["executed_at"], "count": r["n"],
                                                    "note": "Identical fills can be real (two orders filled alike), so "
                                                            "they are not merged. Correct or keep them yourself."})
        for t in db.query("SELECT t.id AS trade_id, t.status, t.entry_price, t.exit_price, t.contracts, p.id AS pid "
                          "FROM trades t LEFT JOIN positions p ON p.legacy_trade_id = t.id"):
            if t["pid"] is None:
                out["journal_mismatches"].append({"trade_id": t["trade_id"], "problem": "no mirror position"})
                continue
            # Compared as lists, so an extra fill on either side is seen, not folded into one.
            have = sorted((r["side"], r["quantity"], r["price"], r["fees"]) for r in db.query(
                "SELECT side, quantity, price, fees FROM executions WHERE position_id = ?", (t["pid"],)))
            want = []
            if t["entry_price"] is not None:
                want.append(("BUY", float(t["contracts"]), float(t["entry_price"]), 0.0))
                if t["exit_price"] is not None:
                    want.append(("SELL", float(t["contracts"]), float(t["exit_price"]), 0.0))
            want.sort()
            if have != want:
                out["journal_mismatches"].append({"trade_id": t["trade_id"], "position_id": t["pid"], "journal": want,
                                                  "mirror": have})
    unreadable = {x["id"] for x in out["invalid_states"]}
    for pid in ids:
        if pid in unreadable:
            continue                        # listed under invalid_states; nothing else about it can be read
        with db.tx() as cur:
            pos = read(cur, pid)
        if lifecycle(pos).get("state") == "UNRESOLVED":
            out["unresolved_options"].append({"id": pid, "symbol": pos["symbol"], "expiration": pos["legs"][0]["expiration"],
                                              "open_contracts": pos["ledger"]["open_quantity"]})
        if pos["status"] in ("OPEN", "PARTIALLY_CLOSED"):
            meta = db.one("SELECT last_date FROM bars_meta WHERE symbol = ?", (pos["symbol"],))
            failed = getattr(market, "_stale_bars", {}).get(pos["symbol"]) if market is not None else None
            if meta is None or failed:
                out["market_data_gaps"].append({"id": pid, "symbol": pos["symbol"],
                                                "problem": failed or "no price history saved yet"})
    snap = snapshots.status(db)
    integrity = [r[0] for r in db.query("PRAGMA integrity_check")]
    fk = [dict(zip(("table", "rowid", "parent", "fkid"), tuple(r))) for r in db.query("PRAGMA foreign_key_check")]
    report = {"checked_at": datetime.now(timezone.utc).isoformat(timespec="seconds"), "counts": counts, **out,
              "missing_snapshots": snap["missing"], "hindsight_snapshots": snap["hindsight"],
              "unavailable_snapshots": snap["unavailable"], "stale_snapshots": snap.get("stale", []),
              "database": {"integrity": integrity, "foreign_key_problems": fk,
                           "ok": integrity == ["ok"] and not fk},
              "schema_version": db.schema_version()}
    report["summary"] = {k: len(v) for k, v in report.items() if isinstance(v, list)}
    report["needs_repair"] = bool(out["ledger_mismatches"] or out["journal_mismatches"])
    report["needs_attention"] = any(report["summary"].get(k) for k in (
        "ledger_mismatches", "lifecycle_issues", "invalid_states", "possible_duplicate_fills", "journal_mismatches",
        "unresolved_options", "missing_snapshots", "hindsight_snapshots", "stale_snapshots", "rule_breaks")) \
        or not report["database"]["ok"]
    return report


def repair(db: Any, market: Any = None, label: str = "before-repair") -> dict[str, Any]:
    """Recompute derived figures from the fills after copying the database. Never changes a fill."""
    try:
        backup = db.backup(label) if db.path != ":memory:" else None
    except Exception as exc:                  # noqa: BLE001 - no copy, no repair
        raise RuntimeError(f"The database could not be copied first, so nothing was repaired: {exc}") from exc
    before = diagnose(db, market)
    changed, resynced, failed = [], [], []
    # Each item in its own transaction: one that cannot be repaired is reported and the rest go ahead.
    for m in before["journal_mismatches"]:
        if m.get("trade_id") is None:
            continue
        try:
            with db.tx() as cur:
                sync_legacy_trade(cur, m["trade_id"])
            resynced.append(m["trade_id"])
        except PositionError as exc:
            failed.append({"trade_id": m["trade_id"], "problem": str(exc)})
    for m in before["ledger_mismatches"]:
        try:
            with db.tx() as cur:
                recompute(cur, m["id"], correction=True)
            changed.append({"id": m["id"], "symbol": m["symbol"], "differences": m["differences"]})
        except PositionError as exc:
            failed.append({"id": m["id"], "symbol": m["symbol"], "problem": str(exc)})
    with db.tx() as cur:
        if _has_table(cur, "maintenance_log"):
            cur.execute("INSERT INTO maintenance_log (action, at, backup_path, details_json) VALUES (?, ?, ?, ?)",
                        ("REPAIR", datetime.now(timezone.utc).isoformat(timespec="seconds"),
                         str(backup) if backup else None,
                         json.dumps({"recomputed": changed, "journal_resynced": resynced, "failed": failed}, default=str)))
    after = diagnose(db, market)
    if changed or resynced:
        emit("repair_applied", logging.WARNING, positions=len(changed), journal_entries=len(resynced),
             backup=backup.name if backup else None)
    return {"backup": str(backup) if backup else None, "recomputed": changed, "journal_resynced": resynced,
            "failed": failed, "left_as_recorded": before["lifecycle_issues"], "invalid_states": before["invalid_states"],
            "before": before["summary"], "after": after["summary"],
            "remaining": {k: v for k, v in after["summary"].items() if v},
            "note": "Figures worked out from the fills were recomputed. Fills you recorded were not changed. A position "
                    "mirrored from the option journal had its fills re-read from the journal, which is its record; each "
                    "fill changed that way is kept as a JOURNAL_SYNC correction. Positions opened again after closing "
                    "are kept as recorded, and positions whose fills no longer add up are listed for you to correct."}


__all__ = ["diagnose", "repair", "expected"]
