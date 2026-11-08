"""Structured events from headlines and filings (4.0, phase 7).

A price model cannot know that a company cut its guidance this afternoon. A language model can read the headline and
say so in a fixed vocabulary: the kind of event, its direction, its size. This module is the vocabulary and its
checks; the app (v4/events.py) supplies the headlines and asks the model.

What it is for, and what it is not:

- Every event is kept with the moment it was **published** and the moment it was **read**, and `point_in_time`
  hands a forecast only events published before its cutoff. A model trained on these later can never see an event
  its forecast could not have known.
- Historical headlines are not available for the ten-year replay, so no model is trained on events yet. They are
  collected from today on, shown beside each forecast ("what the price model does not see"), and a challenger that
  uses them can be added once there is a year of them, judged by the same promotion rule as every other.
- The language model never decides a trade, changes a limit or produces a price target; it fills in four fields,
  and anything outside the vocabulary is discarded.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timedelta, timezone
from typing import Any

KINDS = ("earnings", "guidance", "analyst", "deal", "legal", "product", "management", "capital", "macro", "other")
DIRECTIONS = ("positive", "negative", "mixed", "unclear")
MAGNITUDES = ("minor", "moderate", "major")

SYSTEM = ("You classify financial news about one company into a fixed vocabulary. You never give advice, price targets or "
          "predictions. Answer with JSON only.")


def utc(moment: str | None) -> str | None:
    """An ISO moment in UTC (stored and compared that way: text comparison across time-zone offsets would be wrong)."""
    if not moment:
        return None
    try:
        t = datetime.fromisoformat(str(moment).replace("Z", "+00:00"))
    except ValueError:
        return None
    return (t if t.tzinfo else t.replace(tzinfo=timezone.utc)).astimezone(timezone.utc).isoformat(timespec="seconds")


def headline_sha(symbol: str, headline: str, published: str) -> str:
    return hashlib.sha256(f"{symbol.upper()}|{headline.strip()}|{published}".encode()).hexdigest()


def prompt(symbol: str, items: list[dict[str, Any]]) -> str:
    lines = [f"{i + 1}. [{it['published']}] {it['headline'].strip()[:300]}" for i, it in enumerate(items)]
    return (f"Company: {symbol}\nHeadlines and filings, newest first:\n" + "\n".join(lines) + "\n\n"
            "For each numbered item that is news about this company (skip duplicates and items that are not about it), "
            "return one object with: n (its number), kind (one of " + ", ".join(KINDS) + "), direction for the company's "
            "shareholders (one of " + ", ".join(DIRECTIONS) + "), magnitude (one of " + ", ".join(MAGNITUDES) + "), and "
            "summary (at most 20 words, facts only). Answer as a JSON list, [] when nothing qualifies.")


def parse(text: str, symbol: str, items: list[dict[str, Any]], model: str) -> list[dict[str, Any]]:
    """The valid events in a model's answer. Anything outside the vocabulary, or about an item not asked about, is
    dropped: the answer is data to check, not instructions."""
    got = _json_list(text)
    if got is None:
        return []
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    out, seen = [], set()
    for e in got:
        if not isinstance(e, dict):
            continue
        try:
            n = int(e.get("n"))
        except (TypeError, ValueError):
            continue
        if not 1 <= n <= len(items) or n in seen:
            continue
        kind, direction, magnitude = e.get("kind"), e.get("direction"), e.get("magnitude")
        if kind not in KINDS or direction not in DIRECTIONS or magnitude not in MAGNITUDES:
            continue
        summary = " ".join(str(e.get("summary") or "").split())[:200]
        if not summary:
            continue
        it = items[n - 1]
        published = utc(it.get("published"))
        if published is None:
            continue
        seen.add(n)
        out.append({"symbol": symbol.upper(), "published_at": published, "extracted_at": now, "kind": kind,
                    "direction": direction, "magnitude": magnitude, "summary": summary, "source": str(it.get("source") or "")[:80],
                    "headline_sha": headline_sha(symbol, it["headline"], it["published"]), "model": model})
    return out


def _json_list(text: str) -> list[Any] | None:
    """The first JSON list in a reply (bare, or inside a code fence or prose)."""
    text = (text or "").strip()
    for candidate in (text, text[text.find("["): text.rfind("]") + 1] if "[" in text and "]" in text else ""):
        if not candidate:
            continue
        try:
            got = json.loads(candidate)
        except ValueError:
            continue
        if isinstance(got, list):
            return got
    return None


def store_events(store: Any, events: list[dict[str, Any]]) -> int:
    n = 0
    with store.tx() as cur:
        for e in events:
            cur.execute("""INSERT OR IGNORE INTO events (symbol, published_at, extracted_at, kind, direction, magnitude, summary, source,
                           headline_sha, model) VALUES (?,?,?,?,?,?,?,?,?,?)""",
                        (e["symbol"], e["published_at"], e["extracted_at"], e["kind"], e["direction"], e["magnitude"], e["summary"],
                         e["source"], e["headline_sha"], e["model"]))
            n += cur.rowcount
    return n


def point_in_time(store: Any, symbol: str, cutoff: str, days: int = 7) -> list[dict[str, Any]]:
    """The stock's events published before `cutoff` (an ISO moment), newest first, over the last `days` days."""
    cut = utc(cutoff)
    since = (datetime.fromisoformat(cut) - timedelta(days=days)).isoformat()
    rows = store.query("""SELECT symbol, published_at, kind, direction, magnitude, summary, source FROM events
                          WHERE symbol = ? AND published_at <= ? AND published_at >= ? ORDER BY published_at DESC""",
                       (symbol.upper(), cut, since))
    return [dict(r) for r in rows]


def summary(events: list[dict[str, Any]]) -> dict[str, Any]:
    """Counts a future model could read: how many events of each direction, and whether any was major."""
    out = {d: 0 for d in DIRECTIONS}
    for e in events:
        out[e["direction"]] += 1
    out["major"] = sum(1 for e in events if e["magnitude"] == "major")
    out["total"] = len(events)
    return out


__all__ = ["utc", "KINDS", "DIRECTIONS", "MAGNITUDES", "SYSTEM", "prompt", "parse", "store_events", "point_in_time", "summary", "headline_sha"]
