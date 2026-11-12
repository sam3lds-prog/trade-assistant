"""Check Tradier data (4.2): a read-only test of the Tradier token the app is set up with.

Four requests, each reported on its own so one failure does not hide what works:

1. **quote**: a stock quote (SPY unless another symbol is asked for): its last, bid and ask, and how old its last
   trade is, which says whether the data is real time (production) or delayed (the sandbox is 15 minutes behind);
2. **daily history**: the last two weeks of daily bars, the input to studies, analogs, factors and calibration;
3. **option expirations and a chain**: the nearest expiration's chain, with how many contracts carry a two-sided
   quote, an implied volatility and Greeks, and how old the Greeks are (Tradier refreshes them about once an hour,
   separately from bid and ask, so they are dated separately);
4. **market clock**: whether the market is open now.

Nothing is written and no order endpoint is touched: only GET /v1/markets/*. A 401 or 403 means the token is wrong
for the environment (a sandbox token on the production host, or the reverse); the reply says which to check. The
token itself is never returned.
"""

from __future__ import annotations

import time
from datetime import date, datetime, timedelta, timezone
from typing import Any

from .optionmath import NY
from .providers.base import DataError, QuotaExceeded
from .providers.tradier import TradierData

QUOTE_FIELDS = ("last", "bid", "ask", "volume", "trade_time")
CHAIN_FIELDS = ("bid", "ask", "greeks")


def _age_seconds(stamp: Any, now: float) -> float | None:
    if not stamp:
        return None
    try:
        t = datetime.fromisoformat(str(stamp).replace("Z", "+00:00"))
    except ValueError:
        try:
            t = datetime.strptime(str(stamp), "%Y-%m-%d %H:%M:%S").replace(tzinfo=NY)
        except ValueError:
            return None
    if t.tzinfo is None:
        # Tradier's Greeks stamp ("2026-11-12 14:04:22") carries no zone: read as New York time, or as UTC when New York
        # would put it in the future.
        ny = t.replace(tzinfo=NY)
        t = ny if ny.timestamp() <= now + 60 else t.replace(tzinfo=timezone.utc)
    return max(0.0, now - t.timestamp())


def tradier_of(svc: Any) -> tuple[TradierData | None, str]:
    """The Tradier client to check: the app's data source when it is Tradier, the history fallback in Robinhood mode,
    otherwise one made from the token in .env (production first), so the token can be checked before switching."""
    prov = getattr(svc.market, "provider", None)
    if isinstance(prov, TradierData):
        return prov, "the app's data source"
    fb = getattr(prov, "history_fallback", None)
    if isinstance(fb, TradierData):
        return fb, "the price-history fallback behind Robinhood"
    s = svc.settings
    if s.live_token:
        return TradierData(s.live_token, sandbox=False), "TRADIER_LIVE_TOKEN (not the app's data source now: DATA_MODE is " + s.data_mode + ")"
    if s.sandbox_token:
        return TradierData(s.sandbox_token, sandbox=True), "TRADIER_SANDBOX_TOKEN (not the app's data source now: DATA_MODE is " + s.data_mode + ")"
    return None, ""


def check_tradier(svc: Any, symbol: str = "SPY", now: float | None = None) -> dict[str, Any]:
    now = time.time() if now is None else now
    client, which = tradier_of(svc)
    if client is None:
        return {"configured": False, "ok": False, "checks": [],
                "message": "No Tradier token is set up. Put TRADIER_LIVE_TOKEN (production: real-time quotes and Greeks) or "
                           "TRADIER_SANDBOX_TOKEN (delayed 15 minutes) in .env, set DATA_MODE=live (or sandbox) and restart the app."}
    sym = symbol.strip().upper() or "SPY"
    info = client.info
    checks: list[dict[str, Any]] = []

    def run(key: str, label: str, fn) -> Any:
        t0 = time.time()
        try:
            out = fn()
            return out
        except QuotaExceeded as exc:
            checks.append({"key": key, "label": label, "ok": False, "error": str(exc), "quota": True,
                           "retry_at": datetime.fromtimestamp(exc.retry_at, timezone.utc).isoformat(timespec="seconds") if exc.retry_at else None,
                           "seconds": round(time.time() - t0, 2)})
        except (DataError, ValueError) as exc:
            msg = str(exc)
            if "rejected the API token" in msg:
                msg += (" The production token goes in TRADIER_LIVE_TOKEN with DATA_MODE=live; a sandbox token in TRADIER_SANDBOX_TOKEN "
                        "with DATA_MODE=sandbox. Tradier's API settings page shows both.")
            checks.append({"key": key, "label": label, "ok": False, "error": msg[:400], "seconds": round(time.time() - t0, 2)})
        return None

    # 1. A quote.
    t0 = time.time()
    quotes = run("quote", f"{sym} quote", lambda: client.quotes([sym]))
    if quotes is not None:
        q = quotes.get(sym) or {}
        missing = [f for f in QUOTE_FIELDS if q.get(f) in (None, "")]
        age = _age_seconds(q.get("trade_time"), now)
        checks.append({"key": "quote", "label": f"{sym} quote", "ok": bool(q) and q.get("last") is not None, "seconds": round(time.time() - t0, 2),
                       "last": q.get("last"), "bid": q.get("bid"), "ask": q.get("ask"), "trade_time": q.get("trade_time"),
                       "age_seconds": round(age, 1) if age is not None else None, "missing": missing,
                       "text": (f"last {q.get('last')}, bid {q.get('bid')}, ask {q.get('ask')}"
                                + (f"; last trade {age / 60:.1f} minutes ago" if age is not None else "; no trade time")
                                + (f"; missing {', '.join(missing)}" if missing else "")) if q else f"Tradier returned no quote for {sym}"})
    # 2. A short daily history.
    t0 = time.time()
    end = datetime.now(NY).date()
    hist = run("history", f"{sym} daily history", lambda: client.history(sym, (end - timedelta(days=14)).isoformat(), end.isoformat()))
    if hist is not None:
        n = int(len(hist))
        last_day = str(hist["date"].iloc[-1])[:10] if n else None
        checks.append({"key": "history", "label": f"{sym} daily history", "ok": n >= 5, "seconds": round(time.time() - t0, 2), "bars": n,
                       "last_date": last_day, "text": f"{n} daily bars in the last two weeks" + (f", the latest {last_day}" if last_day else "")})
    # 3. Expirations, and the nearest one's chain.
    t0 = time.time()
    exps = run("expirations", f"{sym} option expirations", lambda: client.expirations(sym))
    if exps is not None:
        upcoming = [e for e in exps if e >= date.today().isoformat()]
        checks.append({"key": "expirations", "label": f"{sym} option expirations", "ok": bool(upcoming), "seconds": round(time.time() - t0, 2),
                       "count": len(upcoming), "nearest": upcoming[0] if upcoming else None,
                       "text": f"{len(upcoming)} upcoming expirations" + (f", the nearest {upcoming[0]}" if upcoming else "")})
        if upcoming:
            t0 = time.time()
            chain = run("chain", f"{sym} {upcoming[0]} chain", lambda: client.chain(sym, upcoming[0]))
            if chain is not None:
                two_sided = sum(1 for c in chain if c.get("bid") and c.get("ask") and c["ask"] >= c["bid"] > 0)
                greeks = [c["greeks"] for c in chain if c.get("greeks")]
                with_iv = sum(1 for g in greeks if g.get("mid_iv") or g.get("smv_vol"))
                ages = [a for a in (_age_seconds(g.get("updated_at"), now) for g in greeks) if a is not None]
                g_age = min(ages) if ages else None
                missing = [f for f in CHAIN_FIELDS if not any(c.get(f) for c in chain)]
                checks.append({"key": "chain", "label": f"{sym} {upcoming[0]} chain", "ok": bool(chain) and two_sided > 0,
                               "seconds": round(time.time() - t0, 2), "contracts": len(chain), "two_sided": two_sided,
                               "with_greeks": len(greeks), "with_iv": with_iv,
                               "greeks_age_seconds": round(g_age, 0) if g_age is not None else None, "missing": missing,
                               "text": (f"{len(chain)} contracts, {two_sided} with a two-sided quote, {len(greeks)} with Greeks, {with_iv} with an "
                                        "implied volatility" + (f"; Greeks updated {g_age / 60:.0f} minutes ago (Tradier refreshes them about hourly, "
                                                               "separately from bid and ask)" if g_age is not None else "; Greeks carry no update time")
                                        + (f"; missing {', '.join(missing)}" if missing else ""))})
    # 4. The clock.
    t0 = time.time()
    clock = run("clock", "Market clock", client.clock)
    if clock is not None:
        checks.append({"key": "clock", "label": "Market clock", "ok": clock.get("state") not in (None, "", "unknown"),
                       "seconds": round(time.time() - t0, 2), "state": clock.get("state"), "text": clock.get("description") or clock.get("state")})
    order = {"quote": 0, "history": 1, "expirations": 2, "chain": 3, "clock": 4}
    checks.sort(key=lambda c: order.get(c["key"], 9))
    quote = next((c for c in checks if c["key"] == "quote" and c.get("ok")), None)
    delayed = not info.realtime
    if quote and quote.get("age_seconds") is not None and info.realtime and quote["age_seconds"] > 900:
        status_words = "Real-time host, but the last trade is old (the market may be closed)."
    else:
        status_words = "Delayed 15 minutes (the sandbox)." if delayed else "Real time (production)."
    ok = bool(checks) and all(c.get("ok") for c in checks)
    return {"configured": True, "ok": ok, "provider": info.label, "environment": info.mode, "realtime": info.realtime,
            "delayed": delayed, "status": status_words, "which": which, "active": which == "the app's data source",
            "symbol": sym, "checks": checks, "rate": client.rate_state(), "checked_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "message": ("Every check passed." if ok else "Some checks failed: see each one.")
            + ("" if which == "the app's data source" else f" Checked {which}."),
            "note": "Read-only: only market-data requests were made; the token is not shown."}


__all__ = ["check_tradier", "tradier_of"]
