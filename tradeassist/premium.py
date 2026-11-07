"""An option contract's own price history (3.0 Research): what the contract actually traded at, day by day.

This is not the simulated value of an option under a hypothetical stock price (simulate.py, the risk panel):
it is the contract's market record. Where it comes from:

- **Tradier** (live or sandbox token): `/markets/history` for the contract's OCC symbol.
- **Robinhood** (DATA_MODE=robinhood): `get_option_historicals`, a read-only market-data tool.
- Neither: the contract's **theoretical** value on each past day, from the stock's close that day, the days left
  and today's implied volatility (Black-76). It is labelled theoretical everywhere it appears, drawn differently,
  and never called a price: no one traded at it.

Demo data is made up either way, and says so.
"""

from __future__ import annotations

import math
import re
from datetime import date, datetime, timedelta
from typing import Any

import pandas as pd

from .optionmath import NY, black76_price
from .providers.base import DataError

OCC = re.compile(r"^(?P<root>[A-Z.]{1,6})(?P<yy>\d{2})(?P<mm>\d{2})(?P<dd>\d{2})(?P<cp>[CP])(?P<strike>\d{8})$")


def parse_occ(symbol: str) -> dict[str, Any]:
    m = OCC.match(str(symbol).strip().upper().replace(" ", ""))
    if not m:
        raise ValueError(f"{symbol!r} is not an option symbol (OCC form, like AAPL261218C00200000)")
    return {"underlying": m["root"], "expiration": f"20{m['yy']}-{m['mm']}-{m['dd']}",
            "option_type": "CALL" if m["cp"] == "C" else "PUT", "strike": int(m["strike"]) / 1000.0}


def _epoch(d: Any) -> int:
    ts = pd.Timestamp(d)
    if ts.tzinfo is None:
        ts = ts.tz_localize(NY)
    return int(ts.replace(hour=16, minute=0).timestamp())


def history(svc: Any, option_symbol: str, days: int = 90) -> dict[str, Any]:
    terms = parse_occ(option_symbol)
    sym = option_symbol.strip().upper()
    days = max(5, min(int(days), 730))
    end = datetime.now(NY).date()
    start = end - timedelta(days=days)
    info = svc.market.info
    out: dict[str, Any] = {"option_symbol": sym, **terms, "kind": None, "source": None, "bars": [], "underlying_bars": [],
                           "notes": [], "data_mode": info.mode}
    provider = svc.market.provider
    fetch = getattr(provider, "option_history", None)
    actual_error = None
    if fetch is not None and info.mode != "demo":
        try:
            frame = fetch(sym, start.isoformat(), end.isoformat())
            if frame is not None and len(frame):
                out.update(kind="actual", source=info.label)
                out["bars"] = [{"t": _epoch(r.date), "date": str(pd.Timestamp(r.date).date()), "open": float(r.open), "high": float(r.high),
                                "low": float(r.low), "close": float(r.close), "volume": float(r.volume or 0)} for r in frame.itertuples()]
                out["notes"].append("Traded prices: days with no trade in the contract have no bar.")
            else:
                actual_error = "no trades recorded in this contract over the range"
        except (DataError, ValueError) as exc:
            actual_error = str(exc)
    try:
        bars = svc.market.bars(terms["underlying"], max(0.5, days / 365.0 + 0.1), complete=False)
    except DataError as exc:
        out["notes"].append(f"No stock history for {terms['underlying']}: {exc}")
        bars = None
    if bars is not None and len(bars):
        b = bars[pd.to_datetime(bars["date"]).dt.date >= start]
        out["underlying_bars"] = [{"t": _epoch(r.date), "close": float(r.close)} for r in b.itertuples()]
    if out["kind"] is None:
        out.update(kind="theoretical", source="Black-76 from the stock's closes and today's implied volatility")
        if actual_error:
            out["notes"].append(f"No traded history from {info.label}: {actual_error}.")
        elif info.mode == "demo":
            out["notes"].append("Demo data is made up; with a Tradier token or the Robinhood connection this shows the contract's traded prices.")
        else:
            out["notes"].append(f"{info.label} does not provide option price history.")
        out["notes"].append("Theoretical values: what the model says the contract was worth each day, not prices anyone traded at.")
        if bars is not None and len(bars):
            out["bars"] = _theoretical(svc, terms, sym, b)
    return out


def _theoretical(svc: Any, terms: dict[str, Any], sym: str, bars: pd.DataFrame) -> list[dict[str, Any]]:
    """The contract's model value on each past day: that day's close, the time left then, today's volatility."""
    iv = None
    try:
        chain = svc.market.chain(terms["underlying"], terms["expiration"])
        row = chain.find(sym) if hasattr(chain, "find") else None
        if row is None and hasattr(chain, "find_by_terms"):
            row = chain.find_by_terms(terms["option_type"], terms["strike"])
        iv = (row or {}).get("iv") or getattr(chain, "atm_iv", lambda: None)()
    except (DataError, ValueError, AttributeError):
        iv = None
    iv = float(iv) if iv and math.isfinite(float(iv)) and iv > 0 else 0.30
    rate = float(svc.settings.risk_free_rate)
    expiry = date.fromisoformat(terms["expiration"])
    out = []
    for r in bars.itertuples():
        d = pd.Timestamp(r.date).date()
        t = max((expiry - d).days, 0) / 365.0
        spot = float(r.close)
        if t <= 0:
            value = max(0.0, (spot - terms["strike"]) if terms["option_type"] == "CALL" else (terms["strike"] - spot))
        else:
            value = black76_price(spot * math.exp(rate * t), terms["strike"], t, rate, iv, terms["option_type"])
        out.append({"t": _epoch(d), "date": str(d), "close": round(float(value), 4), "iv": iv})
    return out


__all__ = ["parse_occ", "history", "OCC"]
