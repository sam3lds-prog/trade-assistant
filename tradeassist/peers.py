"""Comparable companies (3.0 Research): the stock's sector peers, ranked by how closely their daily returns have
moved with it over the last year, with the user's own additions and removals.

What-if (2.13) takes peers as a market factor. Research uses them as a second history: how did comparable
companies behave after the same kind of event (analogs.py)?
"""

from __future__ import annotations

import re
from typing import Any, Iterable

import numpy as np
import pandas as pd

from .providers.base import DataError
from .sectors import SECTOR_FUNDS, TICKER_FUND, sector_fund

TICKER = re.compile(r"^[A-Z]{1,5}(\.[A-Z])?$")
MAX_CANDIDATES = 18
MIN_CORRELATION = 0.15


def _returns(bars: pd.DataFrame) -> pd.Series:
    s = pd.Series(bars["close"].astype(float).to_numpy(), index=pd.to_datetime(bars["date"]).dt.normalize())
    return np.log(s).diff().dropna()


def peers_for(svc: Any, symbol: str, *, n: int = 5, add: Iterable[str] = (), remove: Iterable[str] = ()) -> dict[str, Any]:
    symbol = str(symbol).upper()
    added = [t.strip().upper() for t in add if t and t.strip()]
    for t in added:
        if not TICKER.match(t):
            raise ValueError(f"{t!r} is not a ticker")
    removed = {t.strip().upper() for t in remove if t}
    fund = sector_fund(symbol)
    notes: list[str] = []
    candidates = [t for t, f in TICKER_FUND.items() if f == fund and t != symbol and t not in removed][:MAX_CANDIDATES]
    if not fund:
        notes.append(f"{symbol} is not in the app's sector lists: name its peers by hand.")
    try:
        mine = _returns(svc.market.bars(symbol, 1.2, complete=True)).iloc[-250:]
    except DataError as exc:
        return {"symbol": symbol, "sector_fund": fund, "sector": SECTOR_FUNDS.get(fund) if fund else None, "peers": [],
                "notes": [f"No price history for {symbol}: {exc}"]}
    rows: list[dict[str, Any]] = []
    for t in dict.fromkeys(candidates + added):
        if t in removed or t == symbol:
            continue
        try:
            theirs = _returns(svc.market.bars(t, 1.2, complete=True)).iloc[-250:]
        except DataError as exc:
            if t in added:
                notes.append(f"{t}: no price history ({exc}).")
            continue
        both = pd.concat([mine, theirs], axis=1, join="inner").dropna()
        corr = float(both.iloc[:, 0].corr(both.iloc[:, 1])) if len(both) >= 60 else None
        rows.append({"symbol": t, "correlation": corr, "days": int(len(both)), "source": "manual" if t in added else "auto",
                     "sector_fund": TICKER_FUND.get(t)})
    auto = sorted([r for r in rows if r["source"] == "auto" and r["correlation"] is not None and r["correlation"] >= MIN_CORRELATION],
                  key=lambda r: -r["correlation"])[:n]
    manual = [r for r in rows if r["source"] == "manual"]
    chosen = manual + [r for r in auto if r["symbol"] not in {m["symbol"] for m in manual}]
    for r in chosen:
        c = r["correlation"]
        r["why"] = ("named by you" if r["source"] == "manual" else
                    f"same sector ({SECTOR_FUNDS.get(fund, fund)}); daily returns correlated {c:.2f} over {r['days']} days")
    if fund and not auto:
        notes.append(f"No sector peer moved with {symbol} closely enough (correlation {MIN_CORRELATION} or more).")
    return {"symbol": symbol, "sector_fund": fund, "sector": SECTOR_FUNDS.get(fund) if fund else None, "peers": chosen,
            "removed": sorted(removed), "notes": notes}


__all__ = ["peers_for", "TICKER"]
