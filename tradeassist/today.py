"""Today (2.5): which stocks lean long and which lean short, as a starting list for a decision.

A stock's **lean** is the sign of its trend engine net score on the last finished bar: more bullish
evidence than bearish leans long, more bearish leans short. The lean is then withdrawn ("no lean")
in two cases, so the lists do not push a trade the rest of the app would argue against:

* **Hostile market.** The market regime is at the bottom of the evaluator's fit table for that
  direction (a long in a bear market, a short in a bull market). The same table scores the
  regime in the stock grade (evaluator.REGIME_FIT_LONG / REGIME_FIT_SHORT).
* **The opposite reversal is under way.** A top at candidate or confirmed stage withdraws a long
  lean; a bottom at candidate or confirmed withdraws a short one (reversal.py stages).

A score is a count of evidence, not a chance, and the lists are sorted by the size of the net
score only because a bigger one means more of the evidence agrees. A stock whose score or
reversal could not be read is reported as such: unknown is never a lean and never "no lean".
The universe is the watchlist or the fixed list of large companies (scanner.py); nothing here
reads an account, a balance or a position at a broker.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from .evaluator import REGIME_FIT_LONG, REGIME_FIT_SHORT
from .optionmath import NY
from .providers.base import DataError
from .scanner import universe

LIST_SIZE = 8
HOSTILE_ROWS = 2          # the bottom of each regime-fit table: both bear regimes for longs, both bull ones for shorts
UNDER_WAY = ("CANDIDATE", "CONFIRMED")


def _bottom(table: dict[str, float]) -> frozenset[str]:
    return frozenset(sorted(table, key=table.get)[:HOSTILE_ROWS])


HOSTILE = {"LONG": _bottom(REGIME_FIT_LONG), "SHORT": _bottom(REGIME_FIT_SHORT)}
MAX_NAMES = 100

REGIME_WORDS = {"BULL_CALM": "a calm bull market", "BULL_VOLATILE": "a volatile bull market", "RANGE": "a range-bound market",
                "TRANSITION": "a market in transition", "BEAR_CALM": "an orderly bear market",
                "BEAR_VOLATILE": "a stressed bear market"}


def hostile(direction: str, regime: str | None) -> bool:
    """Whether the market regime sits at the bottom of the fit table for this direction."""
    return regime is not None and regime in HOSTILE[direction]


def lean(net: float | None, regime: str | None, bottom_stage: str | None, top_stage: str | None,
         reversal_read: bool = True) -> dict[str, Any]:
    """The lean for one stock (rule b). Returns {"lean": "LONG" | "SHORT" | None, "measured": bool,
    "pull": the direction the score points before any cap, "capped": reason or None}.

    `net` None means the score could not be measured: the result is unmeasured, not "no lean".
    With `reversal_read` False the reversal check could not be made; the lean stands and the
    caller says the check is missing."""
    if net is None:
        return {"lean": None, "measured": False, "pull": None, "capped": None}
    if net == 0:
        return {"lean": None, "measured": True, "pull": None, "capped": None}
    direction = "LONG" if net > 0 else "SHORT"
    if hostile(direction, regime):
        return {"lean": None, "measured": True, "pull": direction,
                "capped": f"{'Longs' if direction == 'LONG' else 'Shorts'} fight {REGIME_WORDS.get(regime, 'this market')}."}
    if reversal_read:
        against, word = (top_stage, "top") if direction == "LONG" else (bottom_stage, "bottom")
        if against in UNDER_WAY:
            return {"lean": None, "measured": True, "pull": direction,
                    "capped": f"A {word} is {'confirmed' if against == 'CONFIRMED' else 'forming (candidate)'}."}
    return {"lean": direction, "measured": True, "pull": direction, "capped": None}


def rank(rows: list[dict[str, Any]], size: int = LIST_SIZE) -> dict[str, Any]:
    """Split stock rows (each with `lean` from lean() and `net_score`) into the two lists, largest net
    score first, and count the rest."""
    longs = sorted((r for r in rows if r["lean"] == "LONG"), key=lambda r: (-abs(r["net_score"]), r["symbol"]))
    shorts = sorted((r for r in rows if r["lean"] == "SHORT"), key=lambda r: (-abs(r["net_score"]), r["symbol"]))
    capped = [r for r in rows if r["measured"] and r["lean"] is None and r["capped"]]
    flat = [r for r in rows if r["measured"] and r["lean"] is None and not r["capped"]]
    return {"long": longs[:size], "short": shorts[:size], "long_total": len(longs), "short_total": len(shorts),
            "no_lean": {"total": len(capped) + len(flat), "capped": len(capped), "balanced": len(flat),
                        "capped_long": sum(1 for r in capped if r["pull"] == "LONG"),
                        "capped_short": sum(1 for r in capped if r["pull"] == "SHORT"),
                        "symbols": [{"symbol": r["symbol"], "pull": r["pull"], "why": r["capped"]} for r in capped]},
            "not_measured": [r["symbol"] for r in rows if not r["measured"]]}


class Today:
    def __init__(self, svc: Any, analyst: Any, features: dict[str, bool]):
        self.svc = svc
        self.analyst = analyst
        self.features = features

    def candidates(self, universe_name: str | None = "watchlist") -> dict[str, Any]:
        name, names = universe(universe_name, self.svc.settings.watchlist)
        names = list(dict.fromkeys(n.strip().upper() for n in names if n.strip()))
        if not names:
            raise ValueError("Give at least one symbol")
        if len(names) > MAX_NAMES:
            raise ValueError(f"At most {MAX_NAMES} symbols")
        notes: list[str] = []
        regime_key = regime_label = None
        try:
            g = self.analyst.regime()
            regime_key, regime_label = g["confirmed"]["regime"], g["confirmed"]["label"]
        except (DataError, ValueError, KeyError) as exc:
            notes.append(f"The market regime could not be read ({exc}), so no lean is withdrawn for a hostile market.")
        check_reversal = bool(self.features.get("reversal_analysis"))
        if not check_reversal:
            notes.append("Reversal analysis is switched off, so a forming top or bottom does not withdraw a lean.")
        rows, errors, unread = [], [], []
        for sym in names:
            try:
                t = self.analyst.trend(sym)
            except (DataError, ValueError) as exc:
                errors.append({"symbol": sym, "error": str(exc)})
                continue
            day = t.get("confirmed")
            if not day:                     # no finished bar to read: unknown, not a lean and not "no lean"
                errors.append({"symbol": t.get("symbol", sym), "error": "no finished bar to read the trend on"})
                continue
            net = day.get("net_score")
            bottom = top = None
            # The reversal reading is the slow part, so it is read only where it can change the answer.
            read = check_reversal and net not in (None, 0) and not hostile("LONG" if net > 0 else "SHORT", regime_key)
            if read:
                try:
                    r = self.analyst.reversal(sym, history=False)["confirmed"]
                    bottom, top = r["bottom"]["stage"], r["top"]["stage"]
                except (DataError, ValueError, KeyError) as exc:
                    read = False
                    unread.append(f"{t['symbol']} ({exc})")
            verdict = lean(net, regime_key, bottom, top, reversal_read=read)
            hist = t.get("history") or []
            done = [h for h in hist if h.get("complete")]
            close = done[-1]["close"] if done else None
            prev = done[-2]["close"] if len(done) > 1 else None
            rows.append({"symbol": t["symbol"], "date": day["date"], "state": day["state"], "label": day["label"],
                         "net_score": net, "bullish_score": day.get("bullish_score"),
                         "bearish_score": day.get("bearish_score"), "coverage": day.get("coverage"),
                         "close": close, "change_pct": 100 * (close / prev - 1) if close and prev else None,
                         "reversal": {"bottom": bottom, "top": top} if read else None,
                         # A lean kept only because its reversal could not be read says so on its own row.
                         "reversal_unread": bool(check_reversal and verdict["lean"] and not read), **verdict})
        if unread:
            notes.append("The reversal reading failed for " + ", ".join(unread) + ": their leans were not checked "
                         "for a forming top or bottom.")
        if self.svc.market.info.mode == "demo":
            notes.insert(0, "Demo prices are synthetic: these leans say nothing about real stocks.")
        meta = self.analyst.history_meta() if self.features.get("signal_calibration") else None
        size = len(names)
        words = "your watchlist" if name == "watchlist" else "100 large US companies"
        return {"checked_at": datetime.now(NY).isoformat(timespec="seconds"), "universe": name, "scanned": len(rows),
                "universe_size": size,
                "universe_text": f"From {words} ({size} stock{'s' if size != 1 else ''}), on the last finished bar. "
                                 "Change the watchlist with WATCHLIST in .env.",
                "regime": {"regime": regime_key, "label": regime_label}, **rank(rows), "errors": errors,
                "history_built": bool(meta), "history_run_at": (meta or {}).get("run_at"),
                "data_source": self.svc.market.info.mode, "notes": notes,
                "rule": "A stock leans long when its trend evidence is more bullish than bearish, and short the other "
                        "way, unless the market regime works against that side or the opposite reversal is under way. "
                        "Scores count evidence; they are not chances."}


__all__ = ["Today", "lean", "rank", "hostile", "LIST_SIZE", "HOSTILE", "HOSTILE_ROWS"]
