"""The options lab: volatility across expirations, Monte Carlo checks, and (strategies.py)
multi-leg strategies built from listed contracts. It reads chains through the service's
short-lived chain cache, so a screen that asks for several things at once costs one request
per expiration."""

from __future__ import annotations

import math
from datetime import date, datetime
from typing import Any

from ..optionmath import NY, days_to_expiry, years_to_expiry
from ..providers.base import DataError
from . import volatility

TARGET_DTES = (7, 14, 30, 45, 60, 90, 180, 365)
MAX_EXPIRATIONS = 10


def pick_expirations(dates: list[str], today: date, limit: int = 8) -> list[str]:
    """A spread of expirations: the nearest two at least a day out, then the ones closest to each
    target horizon (a week, two weeks, a month, ... a year), at most `limit`."""
    dated = sorted((d, (date.fromisoformat(d) - today).days) for d in dates)
    dated = [(d, n) for d, n in dated if 1 <= n <= 800]
    if not dated:
        return []
    chosen: list[str] = [d for d, _ in dated[:2]]
    for target in TARGET_DTES:
        best = min(dated, key=lambda x: abs(x[1] - target))
        if best[0] not in chosen and abs(best[1] - target) <= max(5, target * 0.35):
            chosen.append(best[0])
    return sorted(chosen)[:limit]


class OptionsLab:
    def __init__(self, svc: Any):
        self.svc = svc

    def _earnings(self, symbol: str) -> tuple[date | None, dict[str, Any] | None]:
        try:
            e = self.svc._earnings(symbol, None)
        except Exception:                  # noqa: BLE001 - an estimate that cannot be made is just missing
            return None, None
        if not e or not e.get("date"):
            return None, None
        return date.fromisoformat(e["date"]), e

    def volatility(self, symbol: str, max_expirations: int = 8) -> dict[str, Any]:
        symbol = self.svc._symbol(symbol)
        today = datetime.now(NY).date()
        dates = self.svc.market.expirations(symbol)
        if not dates:
            raise ValueError(f"No listed options found for {symbol}")
        picked = pick_expirations(dates, today, max(2, min(int(max_expirations), MAX_EXPIRATIONS)))
        analyses, failed = [], []
        for exp in picked:
            try:
                analyses.append(self.svc._chain_for(symbol, exp))
            except (DataError, ValueError) as exc:
                failed.append({"expiration": exp, "error": str(exc)})
        if not analyses:
            raise ValueError(f"No option chain could be read for {symbol}: " + "; ".join(f["error"] for f in failed))
        earnings_day, earnings = self._earnings(symbol)
        closes = None
        try:
            closes = self.svc.market.bars(symbol, 1.0, complete=True)["close"].tolist()
        except DataError:
            pass
        out = volatility.profile(analyses, today, earnings_day, closes)
        out.update({"symbol": symbol, "spot": analyses[0].spot, "earnings": earnings, "failed": failed,
                    "iv_rank": self.svc.iv_rank(symbol), "data_source": self.svc.market.info.mode,
                    "listed_expirations": len(dates)})
        return out

    def monte_carlo(self, req: dict[str, Any]) -> dict[str, Any]:
        symbol = self.svc._symbol(req["symbol"])
        option_type = str(req["option_type"]).lower()
        strike, expiration = float(req["strike"]), str(req["expiration"])
        analysis = self.svc._chain_for(symbol, expiration)
        row = analysis.find_by_terms(option_type, strike)
        if row is None:
            raise ValueError(f"{symbol} {expiration} {strike:g} {option_type} is not in the chain")
        sigma = row.get("model_vol") or row.get("iv")
        if not sigma:
            raise ValueError("This contract has no usable volatility (no two-sided quotes near it)")
        event_move = event_t = None
        jump_note = None
        if req.get("earnings_jump"):
            prof = self.volatility(symbol, 6)
            ev = prof.get("event") or {}
            when = (prof.get("earnings") or {}).get("date")
            # The jump is placed at the close of the report day, on the same clock as the option's own time
            # (to 4 pm New York on expiration). A report on the expiration day itself is left out, as the
            # implied move is: whether it lands before the option ends depends on the time of the report.
            t_event = years_to_expiry(when) if when else None
            if not (ev.get("available") and when):
                jump_note = "no implied earnings move could be measured before this expiration"
            elif when >= expiration:
                jump_note = "the report is not before this expiration"
            elif t_event is None or not 0 < t_event < analysis.t or date.fromisoformat(when) < datetime.now(NY).date():
                jump_note = "the report date has passed"
            else:
                move = ev["move_pct"] / 100
                # Remove the event's variance from this strike's volatility, so it is not counted twice.
                base_var = sigma * sigma * analysis.t - move ** 2
                if base_var <= 0:
                    jump_note = "this strike's volatility is smaller than the implied earnings move"
                else:
                    event_move, event_t = move, t_event
                    sigma = math.sqrt(base_var / analysis.t)
        premium = req.get("premium")
        if premium is None:
            premium = row.get("mid")
        result = volatility.monte_carlo(spot=analysis.spot, forward=analysis.forward, t=analysis.t, rate=analysis.rate,
                                        sigma=float(sigma), strike=strike, option_type=option_type,
                                        premium=premium, paths=int(req.get("paths") or 20_000),
                                        seed=int(req.get("seed") or volatility.MC_SEED),
                                        event_move=event_move, event_t=event_t,
                                        contracts=int(req.get("contracts") or 1))
        result.update({"symbol": symbol, "expiration": expiration, "strike": strike, "option_type": option_type,
                       "dte": days_to_expiry(expiration), "spot": analysis.spot, "volatility": sigma,
                       "volatility_source": row.get("model_source"), "bid": row.get("bid"), "ask": row.get("ask"),
                       "mid": row.get("mid"), "premium_source": "you" if req.get("premium") is not None else "midpoint",
                       "data_source": self.svc.market.info.mode})
        if req.get("earnings_jump") and result.get("event") is None:
            result["notes"].append(f"No earnings jump was added: {jump_note or 'it would fall outside the option life'}.")
        return result


__all__ = ["OptionsLab", "pick_expirations"]
