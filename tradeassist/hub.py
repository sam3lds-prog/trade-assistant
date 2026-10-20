"""Where the latest opportunities are (2.15): the Morning plan's, the news digest's and Trade ideas', in one shape.

The blend, Analyze opportunity and paper trading all ask here, by source ("morning", "live", "ideas" or "all", the
best of the three), or by the keys a screen sent. A basket built by hand on Analyze opportunity arrives as legs
(a stock, a direction and levels); a leg whose key is a known opportunity keeps that opportunity's edge and
contract, and one typed in fresh gets the grade's suggested levels and no edge (it has no record behind it).
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any

from . import marketdays
from .opportunities import cost_pct, edge_from, from_idea, make, option_of
from .optionmath import NY
from .providers.base import DataError

log = logging.getLogger(__name__)

SOURCES = ("morning", "live", "ideas", "all")
OPTION_CARDS = 12
LIVE_MAX_HOURS = 2.0              # an automated portfolio trades a news digest built in the last two hours


def _when(value: Any) -> datetime | None:
    """A stored time as New York time (a time without a zone is New York's)."""
    if not value:
        return None
    try:
        t = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return (t if t.tzinfo else t.replace(tzinfo=NY)).astimezone(NY)


class Hub:
    def __init__(self, svc: Any, morning: Any, ideas: Any, digest: Any):
        self.svc = svc
        self.morning = morning
        self.ideas = ideas
        self.digest = digest

    # --- the lists ------------------------------------------------------------------------------------------
    def morning_list(self) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        st = self.morning.status(brief=False) if self.morning is not None else {}
        r = (st or {}).get("result") or {}
        cards = {c.get("key"): c for c in r.get("cards") or []}
        opps = []
        for o in r.get("opportunities") or []:
            if not o.get("tradable", True):
                continue
            o = dict(o)
            if o.get("card_key") and o["card_key"] in cards:
                o["card"] = cards[o["card_key"]]
            opps.append(o)
        return opps, {"built_at": r.get("checked_at"), "day": r.get("day"), "phase": r.get("phase"), "horizon": r.get("horizon")}

    def live_list(self) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        d = self.digest.latest() if self.digest is not None else None
        if not d:
            return [], {"built_at": None}
        cards = {c.get("key"): c for c in d.get("cards") or []}
        opps = []
        for o in d.get("opportunities") or []:
            o = dict(o)
            if o.get("card_key") in cards:
                o["card"] = cards[o["card_key"]]
            opps.append(o)
        return opps, {"built_at": d.get("built_at"), "window": d.get("window"), "interval": d.get("interval")}

    def ideas_list(self) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        st = self.ideas.status(brief=False) if self.ideas is not None else {}
        r = (st or {}).get("result") or {}
        day = (r.get("checked_at") or datetime.now(NY).isoformat())[:10]
        opps = []
        for row in list(r.get("long") or []) + list(r.get("short") or []):
            try:
                opps.append(from_idea(row, settings=self.svc.settings, run_day=day))
            except (ValueError, KeyError, TypeError) as exc:
                log.info("Ideas row %s is not an opportunity: %s", row.get("symbol"), exc)
        opps.sort(key=lambda o: (-o["score"], o["symbol"]))
        for i, o in enumerate(opps, 1):
            o["rank"] = i
        return opps, {"built_at": r.get("checked_at"), "universe": r.get("universe_text")}

    def list(self, source: str) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        if source not in SOURCES:
            raise ValueError(f"source must be one of {', '.join(SOURCES)}")
        if source == "morning":
            return self.morning_list()
        if source == "live":
            return self.live_list()
        if source == "ideas":
            return self.ideas_list()
        merged, meta = [], {}
        for name in ("morning", "live", "ideas"):
            got, m = self.list(name)
            merged += got
            meta[name] = m
        merged.sort(key=lambda o: (-o["score"], o["symbol"]))
        return merged, meta

    def stale(self, name: str, meta: dict[str, Any], now: datetime | None = None) -> str | None:
        """Why a list is too old to trade from now, or None when it is fresh: the Morning plan must be today's, the
        digest from the last LIVE_MAX_HOURS, Trade ideas from today or the last trading day. Automated paper
        portfolios trade only fresh lists, so a Friday plan never fills on Monday."""
        now = (now or datetime.now(NY)).astimezone(NY)
        label = {"morning": "the Morning plan", "live": "the news digest", "ideas": "Trade ideas"}[name]
        built = _when(meta.get("built_at"))
        if built is None:
            return f"{label} has not been built"
        if name == "morning" and built.date() != now.date():
            return f"{label} is from {built:%a %b %d}"
        if name == "live" and (now - built).total_seconds() > LIVE_MAX_HOURS * 3600:
            return f"{label} is from {built:%a %b %d %H:%M}"
        if name == "ideas" and built.date() < marketdays.previous_trading_day(now.date()):
            return f"{label} are from {built:%a %b %d}"
        return None

    def fresh(self, source: str, now: datetime | None = None) -> tuple[list[dict[str, Any]], list[str]]:
        """The opportunities of `source` ("all": the best of the three) from lists fresh enough to trade now, best
        first, and why each list left out was left out."""
        names = ("morning", "live", "ideas") if source == "all" else (source,)
        out, why = [], []
        for name in names:
            opps, meta = self.list(name)
            reason = self.stale(name, meta, now)
            if reason:
                why.append(reason)
            else:
                out += opps
        out.sort(key=lambda o: (-o["score"], o["symbol"]))
        return out, why

    def find(self, keys: list[str]) -> list[dict[str, Any]]:
        """The opportunities with these keys, from whichever list holds them."""
        wanted = set(keys)
        out, seen = [], set()
        for name in ("morning", "live", "ideas"):
            for o in self.list(name)[0]:
                if o["key"] in wanted and o["key"] not in seen:
                    out.append(o)
                    seen.add(o["key"])
        return out

    # --- a hand-built basket ------------------------------------------------------------------------------
    def from_legs(self, legs: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Opportunities for basket legs. A known key keeps its edge, score and contract (with the levels given,
        when they are given); a new stock gets the grade's levels when none are given, and no edge."""
        known = {o["key"]: o for o in self.find([leg["key"] for leg in legs if leg.get("key")])}
        out = []
        for leg in legs:
            base = known.get(leg.get("key") or "")
            sym = self.svc._symbol(leg["symbol"])
            direction = leg.get("direction") or (base or {}).get("direction") or "LONG"
            horizon = leg.get("horizon") or (base or {}).get("horizon") or "swing"
            days = int(leg.get("horizon_days") or (base or {}).get("horizon_days") or (1 if horizon == "day" else 20))
            entry, stop, t1, t2 = leg.get("entry"), leg.get("stop"), leg.get("target_1"), leg.get("target_2")
            if base is not None:
                entry = entry or base["entry"]
                stop, t1, t2 = stop or base["stop"], t1 or base["target_1"], t2 or base.get("target_2")
            if not (entry and stop and t1):
                grade = self.svc.analyst.evaluate_position({"symbol": sym, "direction": direction, "entry_price": entry,
                                                            "invalidation_price": stop, "target_1": t1, "target_2": t2,
                                                            "horizon_days": days if horizon == "swing" else 5})
                lv = grade["levels"]
                entry = float(grade["entry"])
                stop, t1, t2 = stop or lv["invalidation"], t1 or lv["target_1"], t2 or lv["target_2"]
            if base is not None:
                o = dict(base)
                o.update({"entry": float(entry), "stop": float(stop), "target_1": float(t1),
                          "target_2": float(t2) if t2 else base.get("target_2")})
                d = 1 if o["direction"] == "LONG" else -1
                if not (d * (o["entry"] - o["stop"]) > 0 and d * (o["target_1"] - o["entry"]) > 0):
                    raise ValueError(f"{sym}: the stop must sit behind the entry and target 1 ahead of it")
                out.append(o)
                continue
            out.append(make(source="manual", key=leg.get("key") or f"manual:{sym}:{direction}:{horizon}", symbol=sym,
                            direction=direction, setup="manual", setup_label="Added by you", horizon=horizon,
                            horizon_days=days, entry=float(entry), stop=float(stop), target_1=float(t1),
                            target_2=float(t2) if t2 else None, score=0.0,
                            edge=edge_from(None, 0, cost_pct=cost_pct(self.svc.settings),
                                           basis="Added by hand: no record behind it, so no edge is assumed"),
                            price=float(entry), tradable=True))
        return out

    # --- contracts on demand ---------------------------------------------------------------------------------
    def ensure_options(self, opps: list[dict[str, Any]], limit: int = OPTION_CARDS) -> list[str]:
        """A trade card (and so a contract) for the best `limit` opportunities that have none yet, so a blend can
        weigh calls and puts against shares. Returns notes on what could not be priced."""
        notes = []
        todo = [o for o in sorted(opps, key=lambda o: -(o.get("score") or 0)) if not o.get("option")][:limit]
        for o in todo:
            row = {"symbol": o["symbol"], "atr": o.get("atr") or abs(o["entry"] - o["stop"]) / 0.6,
                   "reference": o.get("reference") or o["entry"]}
            try:
                if o["horizon"] == "day":
                    card = self.morning._card_for(o, row, "day")
                else:
                    card = self.morning.card(symbol=o["symbol"], direction=o["direction"], entry=o["entry"], stop=o["stop"],
                                             target_1=o["target_1"], target_2=o.get("target_2") or o["target_1"],
                                             horizon_days=int(o.get("horizon_days") or 20), setup="analysis",
                                             levels_why=o.get("levels_why") or "")
            except (DataError, ValueError) as exc:
                notes.append(f"{o['symbol']}: no contract ({exc})")
                continue
            if card is not None:
                o["card"] = card
                o["option"] = option_of(card)
        return notes


__all__ = ["Hub", "SOURCES"]
