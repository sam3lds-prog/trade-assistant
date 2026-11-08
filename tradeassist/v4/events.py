"""Reading the deployed models' stocks' news into structured events, after each close (4.0, phase 7).

Off unless MODEL_EVENTS=1 and the AI is set up. For each stock in the active deployment's universe with headlines
since the last read, one request asks the model to classify them (intelligence/events.py checks every answer
against the fixed vocabulary). It stops at the AI's daily limit, which it shares with the rest of the app.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from . import has_replays, replay_store

MAX_ITEMS = 12


def run_events(svc: Any, limit_stocks: int = 120) -> dict[str, Any]:
    from ..intelligence import events as E
    from ..intelligence.learning.deploy import active
    if not svc.settings.model_events:
        return {"skipped": "off (MODEL_EVENTS=1 turns it on)"}
    ai, news = getattr(svc, "ai", None), getattr(svc, "news", None)
    if ai is None or not ai.configured or news is None:
        return {"skipped": "the AI or the news source is not set up"}
    if not has_replays(svc):
        return {"skipped": "no replays yet"}
    store = replay_store(svc)
    dep = active(store)
    if dep is None:
        return {"skipped": "nothing deployed"}
    stocks = dep["manifest"]["universe"]["stocks"][:limit_stocks]
    out: dict[str, Any] = {"stocks": 0, "events": 0, "errors": []}
    since = datetime.now(timezone.utc) - timedelta(days=2)
    for sym in stocks:
        try:
            got = news.company(sym, days=2)
        except Exception as exc:  # noqa: BLE001 - one stock's news failing is reported, the rest go on
            out["errors"].append(f"{sym}: {str(exc)[:120]}")
            continue
        items = []
        for it in got.get("items") or []:
            pub = E.utc(it.get("published"))
            if not pub or datetime.fromisoformat(pub) < since or not it.get("headline"):
                continue
            sha = E.headline_sha(sym, it["headline"], pub)
            if store.one("SELECT 1 FROM events WHERE headline_sha = ?", (sha,)):
                continue
            items.append({"headline": it["headline"], "published": pub, "source": it.get("source")})
        if not items:
            continue
        items = items[:MAX_ITEMS]
        try:
            ans = ai.ask(purpose="model-events", prompt=E.prompt(sym, items), system=E.SYSTEM, max_tokens=900,
                         cache_key=f"events:{sym}:{items[0]['published']}:{len(items)}")
        except Exception as exc:  # noqa: BLE001 - the daily limit, or the API failing: stop asking for today
            out["errors"].append(f"{sym}: {str(exc)[:160]}")
            break
        evs = E.parse(ans.get("text") or "", sym, items, svc.settings.ai_model)
        out["events"] += E.store_events(store, evs)
        out["stocks"] += 1
    return out


def for_symbol(svc: Any, symbol: str, days: int = 7) -> dict[str, Any]:
    from ..intelligence import events as E
    if not has_replays(svc):
        return {"symbol": symbol, "events": [], "on": bool(svc.settings.model_events)}
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    evs = E.point_in_time(replay_store(svc), symbol, now, days)
    return {"symbol": symbol, "on": bool(svc.settings.model_events), "events": evs, "summary": E.summary(evs)}


__all__ = ["run_events", "for_symbol"]
