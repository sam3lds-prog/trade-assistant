"""Trade ideas (2.7): the highest-graded stock trades to buy and to short across up to 200 large companies.

For each stock the side comes from Today's lean rule (today.read_stock): the trend engine's net score on
the last finished bar, withdrawn when the market regime works against that side or the opposite reversal
is under way. The trade on that side is then graded the way Decide grades it (analysis.evaluate_position):
an entry at the current price, a stop (the invalidation price) and two targets from the chart, a size from
the per-trade risk limit (never more than the account), and a grade from A to AVOID.

The lists are ranked by grade, then by the grade's score, then by reward to risk. A grade sums checks; it
is not a chance, and the dollar figures are what the sized trade makes at each target and loses at the
stop *if* the price gets there, not what it is expected to make. How often it gets there is what the trade
simulation (tradesim.py) models.

Stocks whose lean was withdrawn are graded too, on the side the evidence pointed, and listed apart as held
back, with the reason. A stock that could not be read or graded is listed as such, with the reason; nothing
unknown is ranked, and one stock's failure never stops the scan.

A scan of 200 stocks takes a while (trend, reversal and grade for each), so it runs in the background,
reports progress and can be stopped. One scan runs at a time. Its requests to the data source are spaced
out so the screens keep most of the source's allowance. The latest result is kept in the database, so it
is still there after a restart.
"""

from __future__ import annotations

import logging
import time
from datetime import date, datetime
from typing import Any, Callable

import numpy as np

from .background import OneJob
from .background import why as _why
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
REVERSAL_YEARS = 5              # analysis.REVERSAL_YEARS: fetched once up front so the stock's history is read once
# Seconds between the scan's own requests to each source: about half of Tradier's allowance (120 a minute
# live, 60 in the sandbox); Robinhood publishes no limit, so a cautious pace. Demo data needs none.
SCAN_PACE = {"live": 1.0, "sandbox": 2.0, "robinhood": 0.5}
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


def idea_row(lean_row: dict[str, Any], grade: dict[str, Any], account_size: float | None = None) -> dict[str, Any]:
    """One trade idea from the stock's lean reading and its grade. Shares never cost more than the account."""
    direction = 1 if grade["direction"] == "LONG" else -1
    lv, sz = grade["levels"], grade["sizing"]
    entry = float(grade["entry"])
    earn = grade.get("earnings") or {}
    shares = int(sz["shares"])
    capped = bool(account_size and shares * entry > account_size)
    if capped:
        shares = int(account_size // entry)
    out = {"symbol": grade["symbol"], "direction": grade["direction"], "grade": grade["grade"], "score": grade["score"],
           "caps": [c["reason"] for c in grade.get("caps") or []],
           "components": {c["key"]: c["score"] for c in grade.get("components") or []},
           "trend": {"state": lean_row["state"], "label": lean_row["label"], "net_score": lean_row["net_score"],
                     "date": lean_row["date"]},
           "entry": entry, "entry_source": grade.get("entry_source"), "as_of": grade.get("as_of"),
           "stop": lv["invalidation"], "target_1": lv["target_1"], "target_2": lv["target_2"], "atr": lv.get("atr"),
           "level_notes": lv.get("notes") or [],
           "reward_risk_1": grade["reward_risk"]["target_1"], "reward_risk_2": grade["reward_risk"]["target_2"],
           "risk_budget": sz["risk_budget"], "position_value": shares * entry, "capped_by_account": capped,
           **money_at(entry, lv["invalidation"], lv["target_1"], lv["target_2"], direction, shares),
           "earnings": {"date": earn.get("date"), "estimated": earn.get("estimated")} if earn.get("date") else None,
           "earnings_in_horizon": earnings_within(earn.get("date"), HORIZON_DAYS),
           "past_10d": lean_row.get("past_10d"), "reversal_unread": lean_row.get("reversal_unread", False)}
    if direction < 0:
        out["short_warnings"] = [r["text"] for r in grade.get("short_sale_risk") or [] if r["level"] in ("warn", "fail")]
    return out


def sort_key(row: dict[str, Any]) -> tuple:
    """Best first: grade, then the grade's score, then reward to risk at target 1 (to two places, so ties are
    ties and not floating-point noise), then the ticker."""
    return (GRADE_ORDER.index(row["grade"]), -round(row["score"] or 0, 1), -round(row["reward_risk_1"] or 0, 2),
            row["symbol"])


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
        self._result: dict[str, Any] | None = None
        self._loaded = False
        self._job = OneJob("trade-ideas", self._keep)

    def _key(self) -> str:
        return f"ideas:last:{self.svc.market.info.mode}"

    # --- the background job (background.OneJob holds the thread, progress and stop flag) ---------------
    def _keep(self, result: dict[str, Any], job: dict[str, Any]) -> None:
        result["job_id"] = job["id"]
        try:
            from .service import clean
            self.svc.db.cache_put(self._key(), clean(result))
        except Exception as exc:                      # noqa: BLE001 - kept in memory even if it cannot be saved
            log.warning("Trade ideas result not saved: %s", exc)
        self._result = result

    def start(self, universe_name: str | None) -> dict[str, Any]:
        name, names = self._names(universe_name)

        def work(progress):
            with self.svc.market.paced(SCAN_PACE.get(self.svc.market.info.mode, 0.0)):
                return self.scan(names, name, progress)
        out = self._job.start({"universe": name}, len(names), work)
        return {**out, **self._result_line()}

    def stop(self) -> dict[str, Any]:
        return {**self._job.stop(), **self._result_line()}

    def _result_line(self) -> dict[str, Any]:
        r = self._result
        return {"result_at": r.get("checked_at") if r else None, "result_job": r.get("job_id") if r else None}

    def status(self, brief: bool = False) -> dict[str, Any]:
        if not self._loaded:
            self._loaded = True
            try:
                saved = self.svc.db.cache_get(self._key())
            except Exception:                         # noqa: BLE001 - a missing or damaged copy is just no result
                saved = None
            if self._result is None and saved:
                self._result = saved
        out = {**self._job.status_brief(), **self._result_line()}
        if not brief:
            out["result"] = self._result
        return out

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
             progress: Callable[[int, int, str | None], None] | None = None) -> dict[str, Any]:
        """Read each stock's lean and grade the trade on its side, one stock at a time (so each stock's
        readings are still cached when it is graded); `progress` hears of each and may stop the scan."""
        started = time.time()
        total = len(names)
        if progress:
            progress(0, total, None)
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
        prices: dict[str, float] = {}
        account = float(self.svc.settings.account_size)

        leans, ideas, held, errors, unread, no_price = [], [], [], [], [], []
        for i, sym in enumerate(names):
            if progress:
                progress(i, total, sym)
            if i % QUOTE_CHUNK == 0:                  # current prices a batch at a time, as the scan reaches them
                prices.update(self._prices(names[i:i + QUOTE_CHUNK]))
            try:
                if check_reversal:
                    # The reversal reads five years; fetching them first means one request for the stock's history.
                    try:
                        self.svc.market.bars(sym, REVERSAL_YEARS)
                    except DataError:
                        pass                          # read_stock reports what cannot be read
                row, error, missed = read_stock(self.analyst, sym, regime_key, check_reversal, buckets)
            except Exception as exc:                  # noqa: BLE001 - one stock never stops the scan
                log.warning("Trade ideas: %s could not be read: %r", sym, exc)
                row, error, missed = None, {"symbol": sym, "error": _why(exc)}, None
            if error:
                errors.append({**error, "stage": "read"})
            if missed:
                unread.append(missed)
            if not row:
                continue
            leans.append(row)
            if not row["pull"]:
                continue
            entry = prices.get(row["symbol"])
            if entry is None:
                entry = row["close"]
                no_price.append(row["symbol"])
            if not entry:
                errors.append({"symbol": row["symbol"], "error": "no price to enter at", "stage": "grade"})
                continue
            try:
                grade = self.analyst.evaluate_position({"symbol": row["symbol"], "direction": row["pull"],
                                                        "entry_price": float(entry), "horizon_days": HORIZON_DAYS})
                grade["entry_source"] = "last close" if row["symbol"] in no_price else "current price"
                idea = idea_row(row, grade, account)
            except Exception as exc:                  # noqa: BLE001 - one stock never stops the scan
                log.warning("Trade ideas: %s could not be graded: %r", row["symbol"], exc)
                errors.append({"symbol": row["symbol"], "error": _why(exc), "stage": "grade"})
                continue
            if row["lean"]:
                ideas.append(idea)
            else:
                idea["held_back"] = row["capped"]
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
                **rank(ideas, held), "balanced": balanced, "not_measured": not_measured,
                "errors": [e for e in errors if e["stage"] == "read"],
                "ungraded": [e for e in errors if e["stage"] == "grade"],
                "history_built": bool(meta), "data_source": self.svc.market.info.mode, "notes": notes,
                "account": {"size": s.account_size, "risk_pct": s.max_risk_per_trade_pct,
                            "risk_budget": s.account_size * s.max_risk_per_trade_pct / 100.0},
                "horizon_days": HORIZON_DAYS,
                "rule": "The side comes from the trend evidence (as on Today); the trade on that side is graded as on "
                        "Decide, with the stop and targets from the chart and shares sized to your per-trade risk "
                        "(never more than the account). Ranked by grade, then score, then reward to risk. A grade sums "
                        "checks: it is not a chance, and the dollar figures are what happens if the price gets there, "
                        "not what to expect."}


__all__ = ["TradeIdeas", "idea_row", "money_at", "rank", "sort_key", "earnings_within", "MAX_NAMES", "HORIZON_DAYS",
           "SCAN_PACE"]
