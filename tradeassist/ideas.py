"""Trade ideas (2.7): the best-graded stock trades to buy and to short across up to 200 large companies.

For each stock the side comes from Today's lean rule (today.read_stock): the trend engine's net score on
the last finished bar, withdrawn when the market regime works against that side or the opposite reversal
is under way. The trade on that side is then graded the way Decide grades it (analysis.evaluate_position):
an entry at the current price, a stop (the invalidation price) and two targets from the chart, a size from
the per-trade risk limit, and a grade from A to AVOID.

The lists are ranked by grade, then by the grade's score, then by reward to risk. A grade sums checks; it
is not a chance, and the dollar figures are what the sized trade makes at each target and loses at the
stop *if* the price gets there, not what it is expected to make. How often it gets there is what the trade
simulation (tradesim.py) models.

Stocks whose lean was withdrawn are graded too, on the side the evidence pointed, and listed apart as held
back, with the reason. A stock that could not be read is listed as not read; nothing unknown is ranked.

A scan of 200 stocks takes a while (trend, reversal and grade for each), so it runs in the background and
reports progress. One scan runs at a time; its result is kept until the next one.
"""

from __future__ import annotations

import logging
import threading
import time
from datetime import date, datetime
from typing import Any, Callable

import numpy as np

from .evaluator import GRADE_ORDER
from .optionmath import NY
from .providers.base import DataError
from .scanner import universe
from .signalbook import MAIN_HORIZON
from .today import REGIME_WORDS, read_stock

log = logging.getLogger(__name__)

MAX_NAMES = 200
QUOTE_CHUNK = 50
HORIZON_DAYS = 20               # the holding period a grade assumes (evaluator default), in trading days
UNIVERSE_WORDS = {"top200": "the 200 largest US companies", "large_caps": "100 large US companies",
                  "watchlist": "your watchlist", "custom": "the symbols given"}


def money_at(entry: float, stop: float, t1: float, t2: float, direction: int, shares: int) -> dict[str, Any]:
    """What the sized trade makes at each target and loses at the stop, if the price gets there. Per share
    too, so a trade too large for the risk limit (0 shares) still shows its arithmetic."""
    per = {"target_1": direction * (t1 - entry), "target_2": direction * (t2 - entry), "stop": direction * (stop - entry)}
    return {"per_share": per, "shares": shares,
            "at_target_1": per["target_1"] * shares if shares else None,
            "at_target_2": per["target_2"] * shares if shares else None,
            "at_stop": per["stop"] * shares if shares else None}


def earnings_within(when: str | None, days: int, today: date | None = None) -> bool | None:
    """Whether the next earnings date falls within `days` trading days; None when the date is unknown."""
    if not when:
        return None
    today = today or datetime.now(NY).date()
    d = date.fromisoformat(when)
    return today <= d and int(np.busday_count(today, d)) <= days


def idea_row(lean_row: dict[str, Any], grade: dict[str, Any]) -> dict[str, Any]:
    """One trade idea from the stock's lean reading and its grade."""
    direction = 1 if grade["direction"] == "LONG" else -1
    lv, sz = grade["levels"], grade["sizing"]
    entry = float(grade["entry"])
    earn = grade.get("earnings") or {}
    out = {"symbol": grade["symbol"], "direction": grade["direction"], "grade": grade["grade"], "score": grade["score"],
           "caps": [c["reason"] for c in grade.get("caps") or []],
           "components": {c["key"]: c["score"] for c in grade.get("components") or []},
           "trend": {"state": lean_row["state"], "label": lean_row["label"], "net_score": lean_row["net_score"],
                     "date": lean_row["date"]},
           "entry": entry, "entry_source": grade.get("entry_source"), "as_of": grade.get("as_of"),
           "stop": lv["invalidation"], "target_1": lv["target_1"], "target_2": lv["target_2"], "atr": lv.get("atr"),
           "reward_risk_1": grade["reward_risk"]["target_1"], "reward_risk_2": grade["reward_risk"]["target_2"],
           "risk_budget": sz["risk_budget"], "position_value": sz["position_value"],
           **money_at(entry, lv["invalidation"], lv["target_1"], lv["target_2"], direction, int(sz["shares"])),
           "earnings": {"date": earn.get("date"), "estimated": earn.get("estimated")} if earn.get("date") else None,
           "earnings_in_horizon": earnings_within(earn.get("date"), HORIZON_DAYS),
           "past_10d": lean_row.get("past_10d"), "reversal_unread": lean_row.get("reversal_unread", False)}
    if direction < 0:
        out["short_warnings"] = [r["text"] for r in grade.get("short_sale_risk") or [] if r["level"] in ("warn", "fail")]
    return out


def sort_key(row: dict[str, Any]) -> tuple:
    """Best first: grade, then the grade's score, then reward to risk at target 1."""
    return (GRADE_ORDER.index(row["grade"]), -(row["score"] or 0), -(row["reward_risk_1"] or 0), row["symbol"])


def rank(ideas: list[dict[str, Any]], held: list[dict[str, Any]]) -> dict[str, Any]:
    good = [r for r in ideas if r["grade"] != "AVOID"]
    return {"long": sorted((r for r in good if r["direction"] == "LONG"), key=sort_key),
            "short": sorted((r for r in good if r["direction"] == "SHORT"), key=sort_key),
            "avoid": sorted((r for r in ideas if r["grade"] == "AVOID"), key=sort_key),
            "held_back": sorted(held, key=sort_key)}


class TradeIdeas:
    """The scan, run in the background, and its latest result."""

    def __init__(self, svc: Any, analyst: Any, features: dict[str, bool]):
        self.svc = svc
        self.analyst = analyst
        self.features = features
        self._lock = threading.Lock()
        self._job: dict[str, Any] | None = None
        self._result: dict[str, Any] | None = None
        self._runs = 0

    # --- the background job -------------------------------------------------------------------------
    def start(self, universe_name: str | None) -> dict[str, Any]:
        name, names = self._names(universe_name)
        with self._lock:
            if self._job and self._job["state"] == "running":
                return self._status_locked()
            self._runs += 1
            job = {"id": self._runs, "state": "running", "universe": name, "phase": "reading", "total": len(names),
                   "done": 0, "current": None, "started_at": datetime.now(NY).isoformat(timespec="seconds"), "error": None}
            self._job = job
        thread = threading.Thread(target=self._run, args=(job, name, names), name="trade-ideas", daemon=True)
        thread.start()
        return self.status()

    def _run(self, job: dict[str, Any], name: str, names: list[str]) -> None:
        def progress(phase: str, done: int, total: int, current: str | None) -> None:
            job.update(phase=phase, done=done, total=total, current=current)
        try:
            result = self.scan(names, name, progress)
            with self._lock:
                self._result = result
                job.update(state="done", done=job["total"], current=None,
                           finished_at=datetime.now(NY).isoformat(timespec="seconds"))
        except Exception as exc:                      # noqa: BLE001 - reported on the screen, not raised
            log.exception("Trade ideas scan failed")
            with self._lock:
                job.update(state="failed", error=str(exc)[:500], current=None)

    def status(self) -> dict[str, Any]:
        with self._lock:
            return self._status_locked()

    def _status_locked(self) -> dict[str, Any]:
        job = dict(self._job) if self._job else {"state": "idle"}
        return {"job": job, "result": self._result}

    def _names(self, universe_name: str | None) -> tuple[str, list[str]]:
        name, names = universe(universe_name or "top200", self.svc.settings.watchlist)
        names = list(dict.fromkeys(n.strip().upper() for n in names if n.strip()))
        if not names:
            raise ValueError("Give at least one symbol")
        if len(names) > MAX_NAMES:
            raise ValueError(f"At most {MAX_NAMES} symbols")
        return name, names

    # --- the scan itself (synchronous; tests call it directly) -------------------------------------
    def _prices(self, symbols: list[str]) -> dict[str, float]:
        """Current prices for the trades' entries, a few requests for all of them. A stock left out here is
        entered at its last finished close instead, and says so."""
        market, out = self.svc.market, {}
        for i in range(0, len(symbols), QUOTE_CHUNK):
            chunk = symbols[i:i + QUOTE_CHUNK]
            try:
                quotes = market._call("quotes for trade ideas", lambda c=chunk: market.provider.quotes(c))
            except DataError as exc:
                log.info("Quotes for trade ideas failed: %s", exc)
                continue
            for sym in chunk:
                q = quotes.get(sym)
                if not q:
                    continue
                try:
                    out[sym] = market.spot_from_quote(q, sym)
                except DataError:
                    pass
        return out

    def scan(self, names: list[str], name: str = "custom",
             progress: Callable[[str, int, int, str | None], None] | None = None) -> dict[str, Any]:
        """Read every stock's lean ("reading"), then grade the trades ("grading"); `progress` hears of each."""
        started = time.time()
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
        meta = self.analyst.history_meta() if self.features.get("signal_calibration") else None
        cal = ((meta or {}).get("calibration") or {}).get(str(MAIN_HORIZON)) or {}
        buckets = cal.get("buckets") if meta else None

        # 1. Each stock's lean.
        leans, errors, unread = [], [], []
        total = len(names)
        for i, sym in enumerate(names):
            if progress:
                progress("reading", i, total, sym)
            row, error, missed = read_stock(self.analyst, sym, regime_key, check_reversal, buckets)
            if error:
                errors.append(error)
            if missed:
                unread.append(missed)
            if row:
                leans.append(row)
        # 2. Grade the trade on the side the evidence points, entered at the current price.
        pointing = [r for r in leans if r["pull"]]
        prices = self._prices([r["symbol"] for r in pointing])
        ideas, held, no_price = [], [], []
        for k, r in enumerate(pointing):
            if progress:
                progress("grading", k, len(pointing), r["symbol"])
            entry = prices.get(r["symbol"])
            if entry is None:
                entry = r["close"]
                no_price.append(r["symbol"])
            if not entry:
                errors.append({"symbol": r["symbol"], "error": "no price to enter at"})
                continue
            try:
                grade = self.analyst.evaluate_position({"symbol": r["symbol"], "direction": r["pull"],
                                                        "entry_price": float(entry), "horizon_days": HORIZON_DAYS})
            except (DataError, ValueError) as exc:
                errors.append({"symbol": r["symbol"], "error": f"could not be graded: {exc}"})
                continue
            grade["entry_source"] = "current price" if r["symbol"] not in no_price else "last close"
            idea = idea_row(r, grade)
            if r["lean"]:
                ideas.append(idea)
            else:
                idea["held_back"] = r["capped"]
                held.append(idea)
        if unread:
            notes.append("The reversal reading failed for " + ", ".join(unread) + ": their leans were not checked "
                         "for a forming top or bottom.")
        if no_price:
            notes.append(f"No current price for {', '.join(no_price[:10])}{'…' if len(no_price) > 10 else ''}: "
                         "entered at the last finished close.")
        if self.svc.market.info.mode == "demo":
            notes.insert(0, "Demo prices are synthetic: these trades say nothing about real stocks.")
        balanced = sum(1 for r in leans if r["measured"] and not r["pull"])
        not_measured = [r["symbol"] for r in leans if not r["measured"]]
        s = self.svc.settings
        return {"checked_at": datetime.now(NY).isoformat(timespec="seconds"), "seconds": round(time.time() - started, 1),
                "universe": name, "universe_size": total, "universe_text": UNIVERSE_WORDS.get(name, name),
                "scanned": len(leans), "regime": {"regime": regime_key, "label": regime_label,
                                                   "words": REGIME_WORDS.get(regime_key or "", None)},
                **rank(ideas, held), "balanced": balanced, "not_measured": not_measured, "errors": errors,
                "history_built": bool(meta), "data_source": self.svc.market.info.mode, "notes": notes,
                "account": {"size": s.account_size, "risk_pct": s.max_risk_per_trade_pct,
                            "risk_budget": s.account_size * s.max_risk_per_trade_pct / 100.0},
                "horizon_days": HORIZON_DAYS,
                "rule": "The side comes from the trend evidence (as on Today); the trade on that side is graded as on "
                        "Decide, with the stop and targets from the chart and shares sized to your per-trade risk. "
                        "Ranked by grade, then score, then reward to risk. A grade sums checks: it is not a chance, "
                        "and the dollar figures are what happens if the price gets there, not what to expect."}


__all__ = ["TradeIdeas", "idea_row", "money_at", "rank", "sort_key", "MAX_NAMES", "HORIZON_DAYS"]
