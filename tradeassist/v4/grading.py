"""Research's live forecasts, graded once their horizon has passed (4.0).

Since 3.0 every Research study has saved its forecast to `forecast_records` (median, band, horizon, the date it was
made from) with empty columns for what happened. Nothing filled them, so how Research's forecasts actually fared was
never known. This job fills them, once each, when the target session's daily bar is final:

    realized_pct   the stock's move from the as-of close to the target close, in percent
    covered        whether that move fell inside the forecast's band
    direction_hit  whether the move went the way the median leaned (empty when the median was flat)

The target session is counted on the exchange calendar (marketdays.py). Records made before 4.0 stored a target from
pandas business days, which lands on a market holiday when the horizon crosses one; grading uses the corrected
session, and the stored target is left as it was written.
"""

from __future__ import annotations

import logging
import time
from datetime import date, datetime, timezone
from typing import Any

import pandas as pd

from .. import marketdays
from ..market import settled_through

log = logging.getLogger(__name__)


def target_session(as_of: date, horizon: int) -> date:
    return marketdays.add_trading_days(as_of, horizon)


def grade_research(svc: Any, now: float | None = None, limit: int = 500) -> dict[str, Any]:
    now = time.time() if now is None else now
    settled = settled_through(now)
    mode = svc.market.info.mode
    rows = svc.db.query("""SELECT id, symbol, as_of_date, horizon_days, median_pct, low_pct, high_pct FROM forecast_records
                           WHERE evaluated_at IS NULL AND kind = 'live' AND mode = ? ORDER BY as_of_date LIMIT ?""", (mode, limit))
    due = [r for r in rows if target_session(date.fromisoformat(r["as_of_date"]), int(r["horizon_days"])) <= settled]
    graded, missing = 0, []
    by_symbol: dict[str, list[Any]] = {}
    for r in due:
        by_symbol.setdefault(r["symbol"], []).append(r)
    for sym, recs in by_symbol.items():
        try:
            bars = svc.market.bars(sym, years=3.0, complete=True)
        except Exception as exc:  # noqa: BLE001 - one stock's data failing leaves its records for the next run
            missing.append(f"{sym}: {str(exc)[:120]}")
            continue
        closes = pd.Series(bars["close"].to_numpy(float), index=pd.to_datetime(bars["date"]).dt.date)
        for r in recs:
            as_of = date.fromisoformat(r["as_of_date"])
            target = target_session(as_of, int(r["horizon_days"]))
            if as_of not in closes.index or target not in closes.index:
                missing.append(f"{sym} {as_of}->{target}: no bar")
                continue
            realized = 100.0 * (float(closes[target]) / float(closes[as_of]) - 1.0)
            median = float(r["median_pct"])
            hit = None if median == 0 else int((median > 0) == (realized > 0))
            covered = int(float(r["low_pct"]) <= realized <= float(r["high_pct"]))
            with svc.db.tx() as cur:
                cur.execute("""UPDATE forecast_records SET realized_pct = ?, covered = ?, direction_hit = ?, evaluated_at = ?
                               WHERE id = ? AND evaluated_at IS NULL""",
                            (realized, covered, hit, datetime.now(timezone.utc).isoformat(timespec="seconds"), r["id"]))
                graded += cur.rowcount
    return {"graded": graded, "waiting": len(rows) - len(due), "missing": missing[:20]}


def research_record(svc: Any) -> dict[str, Any]:
    """How Research's graded live forecasts have done, by horizon (for Model Lab and the Portfolio)."""
    out: dict[str, Any] = {}
    for r in svc.db.query("""SELECT horizon_days AS h, model_version, COUNT(*) AS n, AVG(covered) AS coverage, AVG(direction_hit) AS hits,
                             SUM(CASE WHEN direction_hit IS NOT NULL THEN 1 ELSE 0 END) AS leaned,
                             AVG(ABS(realized_pct - median_pct)) AS mae, AVG(ABS(realized_pct)) AS nochange_mae
                             FROM forecast_records WHERE evaluated_at IS NOT NULL AND kind = 'live' AND mode = ?
                             GROUP BY horizon_days, model_version ORDER BY horizon_days""", (svc.market.info.mode,)):
        out.setdefault(str(r["h"]), []).append({k: r[k] for k in r.keys() if k != "h"})
    return out


__all__ = ["grade_research", "research_record", "target_session"]
