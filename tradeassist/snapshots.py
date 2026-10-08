"""Lifecycle snapshots: which ones a position should have, which one counts, and how each was made.

A snapshot records what the market looked like at a point in a position's life:

    PLAN     when the trade was planned (taken then, or the classic journal's ticket quote)
    ENTRY    the first opening fill
    EXIT     the fill that closed the position, once it is CLOSED
    REVIEW   whenever you ask for one

Each PLAN, ENTRY and EXIT is tied to the fill it is about (`execution_id`, `executed_at`) and
says how its data was obtained:

    method            LIVE: market data at the moment it was taken
                      PRIOR_BAR: rebuilt later from the last finished daily bar before the fill
                      END_OF_DAY: rebuilt from the fill day's close (versions before 2.1.1)
                      JOURNAL_QUOTE: the quote saved with a classic journal ticket
    capture_timing    CONTEMPORANEOUS (taken at the time) or RETROSPECTIVE (rebuilt later)
    observation_cutoff the latest moment of market data it uses
    uses_later_data   true when that is after the fill: hindsight. Such a snapshot is shown but
                      never used as what was known at entry (analytics.entry_context).

A fill recorded within LIVE_WINDOW of when it happened gets a LIVE snapshot. A fill recorded
later gets a PRIOR_BAR one: the indicators, trend and regime as of the last daily bar that had
finished before the fill (the fill day's own bar only if the fill was after the 4 p.m. close).
Daily bars are the only history the app has, so a 10 a.m. entry is read from the previous
close, never from that day's 4 p.m. close. Option quotes and Greeks have no history here and
are left out of rebuilt snapshots; the stock price of a rebuilt stock ENTRY or EXIT is the
fill price itself. Nothing missing is made up: if the bars cannot be had, the snapshot is
recorded as UNAVAILABLE in `snapshot_reconstruction`, not stored empty.

One PLAN, ENTRY and EXIT per position is effective (`superseded_at` is null; a unique index
enforces it). A snapshot is superseded, never deleted, when its fill's time is corrected, when
the position is no longer closed (EXIT), or when a hindsight snapshot is replaced by a
point-in-time one. `reconcile` brings one position's lifecycle snapshots up to date and is safe
to run any number of times; `backfill` runs it over every position. Market data is fetched
outside any database transaction; each write is a short transaction that checks again that the
snapshot is still needed.
"""

from __future__ import annotations

import json
import logging
import sqlite3
from datetime import date, datetime, time, timedelta, timezone
from typing import Any, Callable

from .optionmath import NY
from .positions import OPENING, PositionError, add_snapshot, now_iso, read

log = logging.getLogger(__name__)

LIFECYCLE = ("PLAN", "ENTRY", "EXIT")
LIVE_WINDOW = timedelta(minutes=15)
MARKET_CLOSE = time(16, 0)


def when(text: str | None) -> datetime | None:
    if not text:
        return None
    try:
        moment = datetime.fromisoformat(str(text).replace("Z", "+00:00"))
    except ValueError:
        return None
    return moment if moment.tzinfo else moment.replace(tzinfo=timezone.utc)


def cutoff_session(executed: datetime) -> date:
    """The last trading date whose daily bar had finished before `executed`: the same day if the
    fill came at or after the 4 p.m. close in New York, else the day before (the bar filter then
    picks the latest trading day on or before it)."""
    ny = executed.astimezone(NY)
    return ny.date() if ny.time() >= MARKET_CLOSE else ny.date() - timedelta(days=1)


def close_of(day: date) -> str:
    return datetime.combine(day, MARKET_CLOSE, tzinfo=NY).astimezone(timezone.utc).isoformat(timespec="seconds")


def anchors(pos: dict[str, Any]) -> dict[str, dict[str, Any] | None]:
    """The fill each lifecycle snapshot is about: ENTRY the first opening fill, EXIT the last
    closing fill once the position is CLOSED (None otherwise)."""
    opening = OPENING[pos["direction"]]
    execs = pos.get("executions") or []
    first_open = next((e for e in execs if e["side"] in opening), None)
    closes = [e for e in execs if e["side"] not in opening]
    return {"PLAN": None, "ENTRY": first_open,
            "EXIT": closes[-1] if pos["status"] == "CLOSED" and closes else None}


def effective(pos: dict[str, Any], kind: str) -> dict[str, Any] | None:
    for s in pos.get("snapshots") or []:
        if s["snapshot_type"] == kind and s.get("superseded_at") is None:
            return s
    return None


def needs(pos: dict[str, Any], upgrade_hindsight: bool = False, journal_plan: bool = True) -> list[dict[str, Any]]:
    """What reconcile would do for one position: snapshots to create and effective ones to
    supersede, each with its reason."""
    out = []
    marks = anchors(pos)
    for kind in ("ENTRY", "EXIT"):
        anchor, current = marks[kind], effective(pos, kind)
        if anchor is None:
            if current is not None:
                out.append({"kind": kind, "action": "SUPERSEDE", "snapshot_id": current["id"],
                            "reason": "the position is no longer closed" if kind == "EXIT" else "the position has no fills"})
            continue
        if current is None:
            out.append({"kind": kind, "action": "CREATE", "execution": anchor, "reason": "missing"})
        elif current.get("execution_id") not in (None, anchor["id"]) or \
                (current.get("executed_at") and current["executed_at"] != anchor["executed_at"]):
            out.append({"kind": kind, "action": "REPLACE", "execution": anchor, "snapshot_id": current["id"],
                        "reason": "its fill was corrected or replaced"})
        elif upgrade_hindsight and current.get("uses_later_data"):
            out.append({"kind": kind, "action": "REPLACE", "execution": anchor, "snapshot_id": current["id"],
                        "reason": "it uses market data from after the fill", "keep_on_failure": True})
    if journal_plan and pos.get("legacy_trade_id") and effective(pos, "PLAN") is None:
        out.append({"kind": "PLAN", "action": "CREATE", "execution": None, "reason": "the journal ticket's quote"})
    return out


def supersede(cur: sqlite3.Cursor, snapshot_id: int, reason: str) -> None:
    cur.execute("UPDATE position_snapshots SET superseded_at = ?, superseded_reason = ? WHERE id = ? AND superseded_at IS NULL",
                (now_iso(), reason, snapshot_id))


def record_status(cur: sqlite3.Cursor, position_id: int, kind: str, execution_id: int | None, status: str,
                  snapshot_id: int | None, reason: str) -> None:
    cur.execute("INSERT INTO snapshot_reconstruction (position_id, snapshot_type, execution_id, status, snapshot_id, reason, "
                "attempts, updated_at) VALUES (?, ?, ?, ?, ?, ?, 1, ?) ON CONFLICT (position_id, snapshot_type, execution_id) "
                "DO UPDATE SET status = excluded.status, snapshot_id = excluded.snapshot_id, reason = excluded.reason, "
                "attempts = attempts + 1, updated_at = excluded.updated_at",
                (position_id, kind, execution_id or 0, status, snapshot_id, reason, now_iso()))


def journal_quote(cur: sqlite3.Cursor, pos: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]] | None:
    """A PLAN snapshot from the quote a classic journal ticket saved when it was planned: bid,
    ask, midpoint, implied volatility, delta and the stock price at that moment."""
    row = cur.execute("SELECT created_at, data_source, quote_json, expiration FROM trades WHERE id = ?",
                      (pos["legacy_trade_id"],)).fetchone()
    if row is None:
        return None
    quote = json.loads(row[2] or "{}") or {}
    if quote.get("spot") is None and quote.get("bid") is None:
        return None
    bid, ask, mid = quote.get("bid"), quote.get("ask"), quote.get("mid")
    at = quote.get("at") or row[0]
    values = {"underlying_price": quote.get("spot"), "bid": bid, "ask": ask, "mid": mid, "iv": quote.get("iv"),
              "delta": quote.get("delta"), "data_source": row[1], "data_timestamp": str(at)[:10]}
    if bid is not None and ask is not None and mid:
        values["spread_pct"] = 100.0 * (ask - bid) / mid
    planned = when(at)
    if planned is not None:
        values["dte"] = (date.fromisoformat(row[3]) - planned.astimezone(NY).date()).days
    provenance = {"method": "JOURNAL_QUOTE", "capture_timing": "CONTEMPORANEOUS", "uses_later_data": False,
                  "observation_cutoff": at, "captured_at": at, "origin": "option journal ticket"}
    return values, {"journal_quote": quote, "errors": []}, provenance


Capture = Callable[[dict[str, Any], dict[str, Any] | None], tuple[dict[str, Any], dict[str, Any], dict[str, Any]]]


class SnapshotService:
    """Takes and reconciles lifecycle snapshots. `capture(pos, execution)` returns the values, raw
    details and provenance for a snapshot about that fill (or about now, with no fill); the desk
    supplies it, because it needs market data."""

    def __init__(self, db: Any, capture: Capture):
        self.db = db
        self.capture = capture

    def _read(self, position_id: int) -> dict[str, Any]:
        with self.db.tx() as cur:
            return read(cur, position_id)

    def take(self, position_id: int, kind: str) -> dict[str, Any]:
        """A PLAN or REVIEW snapshot of the market now."""
        pos = self._read(position_id)
        values, raw, prov = self.capture(pos, None)
        with self.db.tx() as cur:
            if kind == "PLAN":
                current = effective(read(cur, position_id), "PLAN")
                if current is not None:
                    supersede(cur, current["id"], "a newer plan snapshot")
            sid = add_snapshot(cur, position_id, kind, values, raw, provenance=prov)
        return {"id": sid, "snapshot_type": kind, **values, "errors": raw.get("errors", [])}

    def reconcile(self, position_id: int, upgrade_hindsight: bool = False, dry_run: bool = False) -> dict[str, Any]:
        """Bring one position's PLAN, ENTRY and EXIT snapshots in line with its fills."""
        pos = self._read(position_id)
        todo = needs(pos, upgrade_hindsight)
        report = {"position_id": position_id, "created": [], "superseded": [], "unavailable": [], "planned": todo}
        if dry_run:
            return report
        for item in todo:
            kind, ex = item["kind"], item.get("execution")
            if item["action"] == "SUPERSEDE":
                with self.db.tx() as cur:
                    supersede(cur, item["snapshot_id"], item["reason"])
                report["superseded"].append({"kind": kind, "snapshot_id": item["snapshot_id"], "reason": item["reason"]})
                continue
            try:
                if kind == "PLAN":
                    with self.db.tx() as cur:
                        made = journal_quote(cur, pos)
                    if made is None:
                        with self.db.tx() as cur:
                            record_status(cur, position_id, kind, None, "UNAVAILABLE", None,
                                          "the journal ticket saved no quote")
                        report["unavailable"].append({"kind": kind, "reason": "the journal ticket saved no quote"})
                        continue
                else:
                    made = self.capture(pos, ex)
            except (PositionError, ValueError, KeyError) as exc:
                made, failure = None, str(exc)
            else:
                failure = ""
            values, raw, prov = made if made else ({}, {"errors": [failure]}, {})
            usable = bool(made) and (prov.get("method") in ("LIVE", "JOURNAL_QUOTE") or values.get("data_timestamp")
                                     or values.get("underlying_price") is not None)
            with self.db.tx() as cur:
                now = read(cur, position_id)
                still = [n for n in needs(now, upgrade_hindsight) if n["kind"] == kind and n["action"] != "SUPERSEDE"]
                if not still:
                    continue                        # done meanwhile: nothing to do
                if not usable:
                    why = "; ".join(raw.get("errors") or []) or "no market data for that date"
                    record_status(cur, position_id, kind, ex["id"] if ex else None, "UNAVAILABLE", None, why)
                    report["unavailable"].append({"kind": kind, "execution_id": ex["id"] if ex else None, "reason": why})
                    old = effective(now, kind)
                    if item["action"] == "REPLACE" and not item.get("keep_on_failure") and old is not None:
                        # Tied to a fill time that no longer holds: wrong, so not kept as effective.
                        supersede(cur, old["id"], item["reason"])
                        report["superseded"].append({"kind": kind, "snapshot_id": old["id"], "reason": item["reason"]})
                    continue
                old = effective(now, kind)
                if old is not None:
                    supersede(cur, old["id"], item["reason"])
                    report["superseded"].append({"kind": kind, "snapshot_id": old["id"], "reason": item["reason"]})
                if ex is not None:
                    prov = {**prov, "execution_id": ex["id"], "executed_at": ex["executed_at"]}
                stamp = prov.get("observation_cutoff") if prov.get("method") != "LIVE" else None
                sid = add_snapshot(cur, position_id, kind, values, raw, stamp,
                                   {**prov, "origin": prov.get("origin") or "reconcile", "reason": item["reason"]})
                record_status(cur, position_id, kind, ex["id"] if ex else None, "CREATED", sid, item["reason"])
                report["created"].append({"kind": kind, "snapshot_id": sid, "method": prov.get("method"),
                                          "execution_id": ex["id"] if ex else None, "reason": item["reason"]})
        return report

    def backfill(self, position_ids: list[int] | None = None, upgrade_hindsight: bool = True,
                 dry_run: bool = False) -> dict[str, Any]:
        """Reconcile every position (or those given). Repeatable: a second run finds nothing to do,
        except retrying what could not be rebuilt before."""
        if position_ids is None:
            position_ids = [r[0] for r in self.db.query("SELECT id FROM positions ORDER BY id")]
        reports = []
        for pid in position_ids:
            try:
                reports.append(self.reconcile(pid, upgrade_hindsight, dry_run))
            except PositionError as exc:
                reports.append({"position_id": pid, "error": str(exc), "created": [], "superseded": [], "unavailable": [],
                                "planned": []})
        created = sum(len(r["created"]) for r in reports)
        unavailable = sum(len(r["unavailable"]) for r in reports)
        return {"dry_run": dry_run, "positions_checked": len(reports), "created": created,
                "superseded": sum(len(r["superseded"]) for r in reports), "unavailable": unavailable,
                "planned": sum(len(r["planned"]) for r in reports),
                "positions": [r for r in reports if r["created"] or r["superseded"] or r["unavailable"] or r["planned"]
                              or r.get("error")]}


def status(db: Any) -> dict[str, Any]:
    """Counts for the data health view: positions missing a lifecycle snapshot they should have,
    hindsight snapshots in use, and what could not be rebuilt."""
    missing, hindsight = [], []
    tried = {(r["position_id"], r["snapshot_type"], r["execution_id"])
             for r in db.query("SELECT position_id, snapshot_type, execution_id FROM snapshot_reconstruction "
                               "WHERE status = 'UNAVAILABLE'")}
    for (pid,) in db.query("SELECT id FROM positions ORDER BY id"):
        with db.tx() as cur:
            pos = read(cur, pid)
        for n in needs(pos):
            ex = (n.get("execution") or {}).get("id") or 0
            if n["action"] in ("CREATE", "REPLACE") and (pid, n["kind"], ex) not in tried:
                missing.append({"position_id": pid, "symbol": pos["symbol"], "kind": n["kind"], "reason": n["reason"]})
        for kind in ("ENTRY", "EXIT"):
            s = effective(pos, kind)
            if s is not None and s.get("uses_later_data"):
                hindsight.append({"position_id": pid, "symbol": pos["symbol"], "kind": kind, "method": s.get("method")})
    unavailable = [dict(r) for r in db.query("SELECT position_id, snapshot_type, execution_id, reason, attempts, updated_at "
                                             "FROM snapshot_reconstruction WHERE status = 'UNAVAILABLE' ORDER BY position_id")]
    return {"missing": missing, "hindsight": hindsight, "unavailable": unavailable,
            "status": "OK" if not missing and not hindsight else "ATTENTION"}


__all__ = ["SnapshotService", "anchors", "effective", "needs", "cutoff_session", "close_of", "LIVE_WINDOW", "status"]
