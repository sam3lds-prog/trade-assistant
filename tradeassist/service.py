"""Application services: the operations behind the API, one method per user action."""

from __future__ import annotations

import hashlib
import json
import logging
import math
import threading
import time
import uuid
from datetime import date, datetime, timedelta, timezone
from typing import Any

import numpy as np
import pandas as pd

from . import __version__
from . import research as sec
from .backtest import BacktestParams, run_backtest, walk_forward
from .chain import atm_iv
from .config import Settings
from .db import Database
from .eventstudy import MIN_SYMBOLS, run_study
from .journal import Journal
from .market import CONFIRMED, PROVISIONAL, Market, complete_only, signal_status
from .optionmath import NY, black76_price, days_to_expiry, years_to_expiry
from .patterns import PATTERNS, detect_all, recent_events
from .pricer import build_ladder, quote_age_seconds, tick_rule
from .providers import make_provider
from .providers.base import DataError, MarketData, display_symbol
from .scorecard import _earnings_check, build_scorecard, trend_snapshot
from .simulate import Leg, simulate as run_simulation
from .structure import SWING_ATR, analyse as analyse_structure

log = logging.getLogger(__name__)

SIZING_KEYS = {"trade_risk", "open_risk"}
MIN_THESIS_LENGTH = 15
LIVE_CHAIN_SECONDS = 60   # how long a live view reuses one full chain before fetching it again
WARMUP_YEARS = 1.1   # the 52-week range needs a year of bars before the first signal


class RequestError(ValueError):
    """The request cannot be carried out as asked; the message is shown to the user."""


def clean(value: Any) -> Any:
    """Make a structure JSON-safe: numpy scalars to Python, NaN and infinity to None."""
    if isinstance(value, dict):
        return {str(k): clean(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [clean(v) for v in value]
    if isinstance(value, np.ndarray):
        return [clean(v) for v in value.tolist()]
    if isinstance(value, (np.bool_,)):
        return bool(value)
    if isinstance(value, np.integer):
        return int(value)
    if isinstance(value, (float, np.floating)):
        f = float(value)
        return f if math.isfinite(f) else None
    if isinstance(value, (pd.Timestamp, datetime, date)):
        return value.isoformat()
    return value


class TradeAssistant:
    def __init__(self, settings: Settings, provider: MarketData | None = None, db: Database | None = None):
        self.settings = settings
        provider = provider or make_provider(settings)
        if db is None:
            path = settings.db_path
            if provider.info.mode == "demo":
                # Synthetic data gets its own file so it can never mix with real history.
                path = path.with_name(path.stem + ".demo" + path.suffix)
            db = Database(path)
        self.db = db
        self.market = Market(provider, db, settings)
        self.journal = Journal(db)
        self._chains: dict[tuple[str, str], dict[str, Any]] = {}   # last analysed chain per symbol and expiration
        self._chains_lock = threading.Lock()
        self.sec = sec.SecClient(settings.sec_user_agent, db)

    # --- status ------------------------------------------------------------
    def status(self) -> dict[str, Any]:
        info = self.market.info
        s = self.settings
        return {
            "version": __version__,
            "data": {"mode": info.mode, "label": info.label, "realtime": info.realtime,
                     "has_greeks": info.has_greeks, "note": info.note},
            "clock": self.market.clock(),
            "limits": {"account_size": s.account_size, "max_risk_per_trade_pct": s.max_risk_per_trade_pct,
                       "max_open_risk_pct": s.max_open_risk_pct, "daily_loss_stop_pct": s.daily_loss_stop_pct,
                       "max_spread_pct": s.max_spread_pct, "min_dte": s.min_dte},
            "watchlist": s.watchlist,
            "sec_enabled": self.sec.enabled,
            "places_orders": False,
            "features": s.features,
            "schema_version": self.db.schema_version(),
        }

    # --- option chain ------------------------------------------------------
    def expirations(self, symbol: str) -> dict[str, Any]:
        symbol = self._symbol(symbol)
        dates = self.market.expirations(symbol)
        if not dates:
            raise RequestError(f"No listed options found for {symbol}")
        return {"symbol": symbol, "spot": self.market.spot(symbol),
                "expirations": [{"date": d, "dte": days_to_expiry(d)} for d in dates]}

    def chain(self, symbol: str, expiration: str, width_pct: float = 15.0) -> dict[str, Any]:
        symbol = self._symbol(symbol)
        analysis = self.market.chain(symbol, expiration)
        rows = [self._contract_view(c) for c in analysis.contracts
                if abs(c["moneyness_pct"]) <= width_pct]
        return {"symbol": symbol, "expiration": expiration, "spot": analysis.spot,
                "forward": analysis.forward, "dte": days_to_expiry(expiration), "contracts": rows}

    @staticmethod
    def _contract_view(c: dict[str, Any]) -> dict[str, Any]:
        keys = ("symbol", "option_type", "strike", "expiration", "bid", "ask", "bidsize", "asksize", "mid",
                "spread", "spread_pct", "last", "volume", "open_interest", "iv", "iv_source", "delta", "gamma",
                "theta", "vega", "greeks_source", "model_value", "model_vol", "model_source", "moneyness_pct",
                "intrinsic", "bid_time", "ask_time")
        return {k: c.get(k) for k in keys}

    # --- pricing an option -------------------------------------------------
    def price(self, *, symbol: str, expiration: str, option_type: str, strike: float, side: str = "buy",
              contracts: int = 1, earnings_date: str | None = None, limit_price: float | None = None
              ) -> dict[str, Any]:
        symbol = self._symbol(symbol)
        option_type = option_type.lower()
        if option_type not in ("call", "put"):
            raise RequestError("option_type must be call or put")
        if side not in ("buy", "sell"):
            raise RequestError("side must be buy or sell")
        if contracts < 1 or contracts > 1000:
            raise RequestError("contracts must be between 1 and 1000")
        if limit_price is not None and not (math.isfinite(limit_price) and limit_price > 0):
            raise RequestError("limit_price must be a positive number")
        if not math.isfinite(float(strike)):
            raise RequestError("strike must be a number")
        analysis = self.market.chain(symbol, expiration)
        row = analysis.find_by_terms(option_type, float(strike))
        if row is None:
            raise RequestError(f"No {symbol} {expiration} {strike:g} {option_type} in the chain")

        chain_quotes = self._remember_chain(symbol, expiration, analysis)["quotes"]
        rule = tick_rule(chain_quotes, row.get("min_ticks"))
        calibration = self.journal.calibration(side)
        ladder = build_ladder(row.get("bid"), row.get("ask"), rule, side=side,
                              model_value=row.get("model_value"), contracts=contracts, calibration=calibration)
        warnings = self._data_warnings(row)
        warnings += [{"level": "warn", "text": note} for note in analysis.notes]
        asksize = row.get("asksize") if side == "buy" else row.get("bidsize")
        if asksize and contracts > asksize:
            warnings.append({"level": "warn", "text": (
                f"The quote shows only {asksize:.0f} contracts on the other side and you want {contracts}. "
                "A larger order may fill in pieces or move the price.")})

        result: dict[str, Any] = {
            "symbol": symbol, "side": side, "contracts": contracts,
            "underlying": {"spot": analysis.spot, "forward": analysis.forward,
                           "forward_method": analysis.forward_method},
            "contract": self._contract_view(row), "ladder": ladder, "warnings": warnings,
            "calibration_note": calibration["note"],
            "data": {"mode": self.market.info.mode, "realtime": self.market.info.realtime,
                     "quote_age_seconds": quote_age_seconds(row),
                     "poll_seconds": self.market.info.poll_seconds,
                     "received_at": datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
                     "market_state": self.market.clock().get("state")},
        }
        if side == "sell" or not ladder["ok"]:
            return clean(result)

        enriched = self.market.enriched(symbol, 3)
        earnings = self._earnings(symbol, earnings_date)
        if earnings and earnings.get("note"):
            warnings.append({"level": "warn", "text": earnings["note"]})
        chosen = limit_price if limit_price is not None else _rung(ladder, "balanced")
        validated = self._validated_patterns()
        self._record_iv_if_missing(symbol)
        book = self.book_risk(symbol)
        card = build_scorecard(row, analysis, enriched, self.settings, chosen, contracts, earnings=earnings,
                               iv_rank=self.iv_rank(symbol), open_risk=self.journal.committed_risk() + book["total"],
                               today=datetime.now(NY).date(), trend_validated=False,
                               # Not the Positions screen's risk in this stock: an option on a stock you hold is often
                               # a hedge, and counting the shares' risk against it would block the trade that cuts risk.
                               symbol_risk=self.journal.committed_risk(symbol))
        events = recent_events(enriched)
        for e in events:
            e["verdicts"] = validated.get(e["key"], [])
            e["supported"] = any(v["verdict"] == "supported" for v in e["verdicts"])
            e["with_trade"] = (e["direction"] == 1) == (option_type == "call")
        result.update({"scorecard": card, "earnings": earnings, "recent_patterns": events, "book_risk": book,
                       "assumed_limit": chosen,
                       "bars_as_of": enriched["date"].iloc[-1].strftime("%Y-%m-%d"),
                       "data_warnings": Market.data_warnings(symbol, enriched)})
        return clean(result)

    # --- live quote for the contract on screen -----------------------------
    def _remember_chain(self, symbol: str, expiration: str, analysis: Any) -> dict[str, Any]:
        entry = {"at": time.time(), "analysis": analysis, "calibration": {},
                 "quotes": [q for c in analysis.contracts for q in (c.get("bid"), c.get("ask"))]}
        with self._chains_lock:                          # 2.7: requests and the background scan share it
            self._chains[(symbol, expiration)] = entry
            if len(self._chains) > 50:                   # keep the newest few; this is one user's screen
                oldest = min(self._chains, key=lambda k: self._chains[k]["at"])
                del self._chains[oldest]
        return entry

    def live(self, *, symbol: str, expiration: str, option_type: str, strike: float, side: str = "buy",
             contracts: int = 1) -> dict[str, Any]:
        """The newest quote for one contract and the limit-price ladder rebuilt from it.

        Made to be called every second or so: it asks the data source for just the option and
        the stock in a single request, and reuses the last full chain (tick sizes and the
        model volatility for this strike) for up to a minute. The risk checks are not re-run
        here; they come from a full pricing.
        """
        symbol = self._symbol(symbol)
        option_type = option_type.lower()
        if option_type not in ("call", "put") or side not in ("buy", "sell"):
            raise RequestError("option_type must be call or put, and side buy or sell")
        if contracts < 1 or contracts > 1000 or not math.isfinite(float(strike)):
            raise RequestError("contracts must be between 1 and 1000 and strike a number")
        entry = self._chains.get((symbol, expiration))
        if entry is None or time.time() - entry["at"] > LIVE_CHAIN_SECONDS:
            entry = self._remember_chain(symbol, expiration, self.market.chain(symbol, expiration))
        analysis = entry["analysis"]
        row = analysis.find_by_terms(option_type, float(strike))
        if row is None:
            raise RequestError(f"No {symbol} {expiration} {strike:g} {option_type} in the chain")

        started = time.perf_counter()
        quotes = self.market.provider.quotes([symbol, row["symbol"]])
        fetch_ms = (time.perf_counter() - started) * 1000.0
        received = datetime.now(timezone.utc)
        option = quotes.get(row["symbol"])
        if not option:
            raise DataError(f"The data source returned no quote for {row['symbol']}")
        stock = quotes.get(symbol)
        spot = self.market.spot_from_quote(stock, symbol) if stock else analysis.spot

        model = None
        if row.get("model_vol") and analysis.spot > 0:
            forward = analysis.forward * spot / analysis.spot      # carry the stock's move into the forward
            model = black76_price(forward, row["strike"], years_to_expiry(expiration), analysis.rate,
                                  row["model_vol"], option_type)
        rule = tick_rule(entry["quotes"] + [option.get("bid"), option.get("ask")], row.get("min_ticks"))
        if side not in entry["calibration"]:
            entry["calibration"][side] = self.journal.calibration(side)
        ladder = build_ladder(option.get("bid"), option.get("ask"), rule, side=side, model_value=model,
                              contracts=contracts, calibration=entry["calibration"][side])

        stamps = []
        for key in ("bid_time", "ask_time"):
            try:
                stamps.append(datetime.fromisoformat(option[key]))
            except (KeyError, TypeError, ValueError):
                pass
        newest = max(stamps) if stamps else None
        info = self.market.info
        return clean({
            "symbol": symbol, "option_symbol": row["symbol"], "side": side, "contracts": contracts,
            "spot": spot, "bid": option.get("bid"), "ask": option.get("ask"),
            "bidsize": option.get("bidsize"), "asksize": option.get("asksize"), "last": option.get("last"),
            "bid_time": option.get("bid_time"), "ask_time": option.get("ask_time"),
            "received_at": received.isoformat(timespec="milliseconds"),
            "quote_age_ms": (received - newest).total_seconds() * 1000.0 if newest else None,
            "fetch_ms": fetch_ms, "ladder": ladder,
            "mode": info.mode, "realtime": info.realtime, "poll_seconds": info.poll_seconds,
            "market_state": self.market.clock().get("state"),
            "chain_age_seconds": time.time() - entry["at"],
        })

    def _data_warnings(self, row: dict[str, Any]) -> list[dict[str, str]]:
        info = self.market.info
        out: list[dict[str, str]] = []
        if info.mode == "demo":
            out.append({"level": "fail", "text": "Demo data: these prices are made up. Use them to learn the "
                                                 "screen, not to trade."})
        elif not info.realtime:
            out.append({"level": "fail", "text": "These quotes are delayed 15 minutes. Do not copy a limit "
                                                 "price from them into a live order; add a live Tradier token "
                                                 "for real-time quotes."})
        state = self.market.clock().get("state")
        if state not in ("open", "unknown"):
            out.append({"level": "warn", "text": "The market is closed. These are the last quotes of the "
                                                 "session and will change at the open."})
        elif info.realtime:
            age = quote_age_seconds(row)
            if age is None:
                out.append({"level": "warn", "text": f"{info.label} gave no time for this quote, so the app "
                                                     "cannot tell how fresh it is. Check the bid and ask "
                                                     "against your broker's screen before using these prices."})
            elif age > 300:
                out.append({"level": "warn", "text": "The quote for this contract is stale. Refresh the chain "
                                                     f"before evaluating it. It last changed {age / 60:.0f} "
                                                     "minutes ago: either few market makers are active in "
                                                     "it or the data is out of date; check it against your "
                                                     "broker's screen."})
        return out

    def _earnings(self, symbol: str, entered: str | None) -> dict[str, Any] | None:
        """The earnings date to check against: the one the user entered if it is still ahead,
        otherwise an estimate. A date that disagrees with the estimate is kept but flagged."""
        today = datetime.now(NY).date()
        estimate = self._estimated_earnings(symbol, today)
        if not entered:
            return estimate
        try:
            when = date.fromisoformat(entered)
        except ValueError as exc:
            raise RequestError("earnings_date must look like 2026-11-05") from exc
        if when < today:
            # A past date cannot be the next report. Do not let it switch the check off.
            if estimate:
                return {**estimate, "note": f"The earnings date you entered ({when.isoformat()}) has passed, "
                                            f"so the estimate {estimate['date']} is used instead."}
            return {"date": when.isoformat(), "estimated": False, "source": "entered by you"}
        result = {"date": when.isoformat(), "estimated": False, "source": "entered by you"}
        if estimate and abs((date.fromisoformat(estimate["date"]) - when).days) > 21:
            result["estimate"] = estimate
            result["note"] = (f"The earnings date you entered ({when.isoformat()}) is more than three weeks from "
                              f"the date suggested by past filings ({estimate['date']}). Check it against the "
                              "company's investor-relations page.")
        return result

    def _estimated_earnings(self, symbol: str, today: date) -> dict[str, Any] | None:
        if self.market.info.mode == "demo":
            offset = 5 + int(hashlib.sha256(symbol.encode()).hexdigest()[:6], 16) % 80
            return {"date": (today + timedelta(days=offset)).isoformat(), "estimated": True,
                    "source": "made-up demo date"}
        if not self.sec.enabled:
            return None
        try:
            cik, _ = self.sec.cik_for(symbol)
            return sec.estimate_next_earnings(sec.earnings_release_dates(self.sec.submissions(cik)), today)
        except (sec.ResearchError, ValueError, TypeError, KeyError, IndexError, AttributeError) as exc:
            log.warning("Earnings estimate unavailable for %s: %s", symbol, exc)
            return None

    # --- trade ticket ------------------------------------------------------
    def ticket(self, *, symbol: str, expiration: str, option_type: str, strike: float, contracts: int,
               limit_price: float, max_limit: float | None, thesis: str, earnings_date: str | None = None,
               profit_target_pct: float = 50.0, stop_loss_pct: float = 50.0, exit_days_before_expiry: int = 7,
               overrides: list[str] | None = None, save: bool = False) -> dict[str, Any]:
        if limit_price is None or not math.isfinite(limit_price) or limit_price <= 0:
            raise RequestError("Enter the limit price you plan to start with")
        if max_limit is not None and not math.isfinite(max_limit):
            raise RequestError("The most you will pay must be a number")
        for name, value in (("profit target", profit_target_pct), ("stop", stop_loss_pct)):
            if not math.isfinite(value):
                raise RequestError(f"The {name} must be a number")
        max_limit = max_limit if max_limit and max_limit > 0 else limit_price
        if max_limit < limit_price:
            raise RequestError("The most you will pay cannot be below your starting limit")
        if not 0 < stop_loss_pct <= 100 or profit_target_pct <= 0 or exit_days_before_expiry < 0:
            raise RequestError("Check the exit plan: stop between 0 and 100%, target above 0, days not negative")
        overrides = sorted(set(overrides or []))
        # Size and score against the most you are willing to pay, not the hoped-for price.
        priced = self.price(symbol=symbol, expiration=expiration, option_type=option_type, strike=strike,
                            side="buy", contracts=contracts, earnings_date=earnings_date, limit_price=max_limit)
        if "scorecard" not in priced:
            raise RequestError("This contract cannot be priced right now: " +
                               " ".join(f["text"] for f in priced["ladder"]["flags"]))
        card = priced["scorecard"]
        violations: list[dict[str, Any]] = []
        for check in card["checks"]:
            if check["blocking"]:
                violations.append({"key": check["key"], "label": check["label"], "text": check["detail"],
                                   "value": check["value"], "overridable": check["key"] not in SIZING_KEYS})
        # An entered earnings date that clears the check while past filings point to a report
        # before expiration needs a deliberate, recorded decision.
        entered = priced.get("earnings") or {}
        estimate = entered.get("estimate")
        if estimate and not any(v["key"] == "earnings" for v in violations):
            probe = _earnings_check({"expiration": expiration}, estimate, datetime.now(NY).date())
            if probe["blocking"]:
                violations.append({
                    "key": "earnings_estimate", "label": "Earnings date disagrees with past filings",
                    "value": f"you entered {entered['date']}, filings suggest {estimate['date']}",
                    "text": "The date you entered puts earnings after expiration, but the company's past "
                            "filings point to a report before it. Confirm the date with the company, then "
                            "override this if you are sure.", "overridable": True})
        loss_cap = self.settings.account_size * self.settings.daily_loss_stop_pct / 100.0
        today = self.daily_loss_status()
        realized, counted_as = today["counted"], today["counted_as"]
        risk_notes: list[dict[str, Any]] = []
        if today["loss_status"] == "STOP":
            violations.append({"key": "daily_loss", "label": "Daily loss stop",
                               "value": f"${-realized:,.0f} lost today ({counted_as})",
                               "text": f"Your rule is to stop for the day after losing "
                                       f"{self.settings.daily_loss_stop_pct:g}% (${loss_cap:,.0f}).",
                               "overridable": True})
        elif today["coverage_status"] != "COMPLETE":
            # A partial measurement is not a pass: say so, and with INCOMPLETE_RISK_POLICY=BLOCK treat it
            # as a limit broken until it is overridden on purpose.
            note = {"key": "daily_loss_incomplete", "label": "Daily loss not fully measured",
                    "value": (f"{today['positions_evaluated']} of {today['positions']} positions measured"
                              if today.get("positions") is not None else "positions could not be read"),
                    "text": today["text"], "overridable": True}
            if self.settings.incomplete_risk_policy == "BLOCK":
                violations.append(note)
            else:
                risk_notes.append(note)
        unknown_risk = (priced.get("book_risk") or {}).get("unknown") or []
        if unknown_risk:
            n = len(unknown_risk)
            note = {"key": "open_risk_incomplete", "label": "Open risk not fully measured",
                    "value": f"{n} position{'s' if n != 1 else ''} not measured",
                    "text": "Total risk above counts only what could be measured. Not measured: "
                            + "; ".join(f"{u['description']} ({u['reason']})" for u in unknown_risk)
                            + ". It is not known whether the whole is inside your open-risk limit.", "overridable": True}
            if self.settings.incomplete_risk_policy == "BLOCK":
                violations.append(note)
            else:
                risk_notes.append(note)
        ask = priced["contract"].get("ask")
        if ask and max_limit > ask + 1e-9:
            violations.append({"key": "above_ask", "label": "Limit above the ask", "value": f"${max_limit:.2f}",
                               "text": f"The most you will pay is above the current ask (${ask:.2f}). "
                                       "There is no reason to bid more than the offer.", "overridable": False})
        thesis = (thesis or "").strip()
        if len(thesis) < MIN_THESIS_LENGTH:
            violations.append({"key": "thesis", "label": "Thesis", "value": "missing",
                               "text": "Write one or two sentences: what you expect to happen, by when, and "
                                       "what would prove you wrong.", "overridable": False})
        unknown = [o for o in overrides if o not in {v["key"] for v in violations if v["overridable"]}]
        blocking = [v for v in violations if not v["overridable"] or v["key"] not in overrides]

        expiry = date.fromisoformat(expiration)
        plan = {
            "start_limit": limit_price, "max_limit": max_limit, "contracts": contracts,
            "premium_at_risk": max_limit * 100 * contracts,
            "profit_target_pct": profit_target_pct, "profit_target_price": round(max_limit * (1 + profit_target_pct / 100), 2),
            "stop_loss_pct": stop_loss_pct, "stop_price": round(max_limit * (1 - stop_loss_pct / 100), 2),
            "exit_by": (expiry - timedelta(days=exit_days_before_expiry)).isoformat(),
            "max_loss_if_stop_holds": max_limit * stop_loss_pct / 100 * 100 * contracts,
        }
        out = {"priced": priced, "violations": violations, "blocking": blocking, "plan": plan,
               "can_save": not blocking, "ignored_overrides": unknown, "saved_id": None,
               "risk_notes": risk_notes, "within_limits": not violations and not risk_notes,
               "incomplete_risk_policy": self.settings.incomplete_risk_policy}
        if save:
            if blocking:
                raise RequestError("This ticket breaks a limit and cannot be saved: " +
                                   "; ".join(v["label"] for v in blocking))
            used = [o for o in overrides if o not in unknown]
            signals = [{"name": e["label"], "key": e["key"], "date": e["date"], "supported": e["supported"]}
                       for e in priced.get("recent_patterns", []) if e["with_trade"]]
            out["saved_id"] = self.journal.create(
                data_source=self.market.info.mode, symbol=priced["symbol"],
                option_symbol=priced["contract"]["symbol"], option_type=option_type.lower(), strike=float(strike),
                expiration=expiration, contracts=contracts, thesis=thesis, planned_limit=limit_price,
                max_limit=max_limit, plan=plan,
                quote={"bid": priced["contract"]["bid"], "ask": priced["contract"]["ask"],
                       "mid": priced["contract"]["mid"], "model_value": priced["contract"]["model_value"],
                       "spot": priced["underlying"]["spot"], "iv": priced["contract"]["iv"],
                       "delta": priced["contract"]["delta"], "earnings": priced.get("earnings"),
                       "at": datetime.now(timezone.utc).isoformat()},
                scorecard=[{"key": c["key"], "status": c["status"], "value": c["value"]} for c in card["checks"]],
                signals=signals, overrides=used)
        return clean(out)

    # --- implied volatility history ----------------------------------------
    def atm_iv_30d(self, symbol: str) -> dict[str, Any]:
        """At-the-money implied volatility interpolated to 30 days, from the nearest usable
        expiration on each side of 30 days."""
        symbol = self._symbol(symbol)
        dated = [(d, days_to_expiry(d)) for d in self.market.expirations(symbol)]
        dated = [(d, n) for d, n in dated if n >= 7]
        if not dated:
            raise RequestError(f"No {symbol} expirations at least a week out")
        before = sorted([x for x in dated if x[1] <= 30], key=lambda x: -x[1])
        after = sorted([x for x in dated if x[1] > 30], key=lambda x: x[1])
        spot = None

        def first_usable(candidates: list[tuple[str, int]]) -> tuple[int, float] | None:
            nonlocal spot
            for exp, dte in candidates[:3]:      # some listed dates have no standard contracts
                try:
                    analysis = self.market.chain(symbol, exp)
                except DataError:
                    continue
                iv = atm_iv(analysis)
                if iv:
                    spot = analysis.spot
                    return dte, iv
            return None

        points = [p for p in (first_usable(before), first_usable(after)) if p]
        if not points:
            raise RequestError(f"Could not compute an at-the-money implied volatility for {symbol}")
        if len(points) == 2 and points[0][0] != points[1][0]:
            (d1, v1), (d2, v2) = points
            w1, w2 = v1 * v1 * d1, v2 * v2 * d2          # total variance is roughly linear in time
            w30 = w1 + (w2 - w1) * (30 - d1) / (d2 - d1)
            iv30 = math.sqrt(max(w30, 1e-12) / 30)
        else:
            iv30 = points[0][1]
        return {"symbol": symbol, "atm_iv": iv30, "spot": spot, "from": points}

    def _record_iv(self, symbol: str, today: str) -> dict[str, Any]:
        point = self.atm_iv_30d(symbol)
        bars = self.market.enriched(symbol, 2)
        hv = bars["hv20"].iloc[-1]
        with self.db.tx() as cur:
            cur.execute("INSERT INTO iv_history (symbol, date, atm_iv, hv20, spot) VALUES (?, ?, ?, ?, ?) "
                        "ON CONFLICT(symbol, date) DO UPDATE SET atm_iv = excluded.atm_iv, "
                        "hv20 = excluded.hv20, spot = excluded.spot",
                        (symbol, today, point["atm_iv"], None if pd.isna(hv) else float(hv), point["spot"]))
        return point

    def _record_iv_if_missing(self, symbol: str) -> None:
        """Record today's implied volatility the first time a symbol is priced each day, so the
        history behind IV rank builds for the stocks actually being traded."""
        today = datetime.now(NY).date().isoformat()
        if not self._traded_today():
            return
        if self.db.one("SELECT 1 FROM iv_history WHERE symbol = ? AND date = ?", (symbol, today)):
            return
        try:
            self._record_iv(symbol, today)
        except (DataError, RequestError, ValueError) as exc:
            log.info("Could not record implied volatility for %s: %s", symbol, exc)

    def _traded_today(self) -> bool:
        """True once today's regular session has begun (or finished). On weekends, holidays and
        before the open the option quotes are the previous session's, and recording them again
        would pad the history with repeats."""
        now = datetime.now(NY)
        if now.weekday() >= 5:
            return False
        state = self.market.clock().get("state")
        if state in ("open", "post", "postmarket"):
            return True
        if state in ("pre", "premarket"):
            return False
        try:   # "closed": the evening of a trading day, or a holiday. Today's bar tells them apart.
            last_bar = self.market.bars("SPY", 1)["date"].iloc[-1].date()
        except (DataError, IndexError):
            return False
        return last_bar == now.date()

    def snapshot(self, symbols: list[str] | None = None) -> dict[str, Any]:
        """Record today's implied volatility for each symbol. Run once a day, after the open,
        to build IV rank."""
        today = datetime.now(NY).date().isoformat()
        saved, failed = [], []
        if not self._traded_today():
            return {"date": today, "saved": [], "failed": [],
                    "skipped": "The market has not traded today, so there is nothing new to record."}
        for raw in symbols or self.settings.watchlist:
            symbol = display_symbol(raw)
            try:
                saved.append({"symbol": symbol, "atm_iv": self._record_iv(symbol, today)["atm_iv"]})
            except (DataError, RequestError, ValueError) as exc:
                failed.append({"symbol": symbol, "error": str(exc)})
        return clean({"date": today, "saved": saved, "failed": failed})

    def iv_rank(self, symbol: str) -> dict[str, Any]:
        """Where the newest recorded implied volatility sits in the recorded range. `fresh` says
        whether that newest record is from today; a rank from an old record describes an old price."""
        rows = self.db.query("SELECT date, atm_iv FROM iv_history WHERE symbol = ? ORDER BY date DESC LIMIT 252",
                             (display_symbol(symbol),))
        if not rows:
            return {"days": 0, "rank": None, "current": None, "fresh": False}
        values = [r["atm_iv"] for r in rows]
        lo, hi, current = min(values), max(values), values[0]
        rank = 100.0 * (current - lo) / (hi - lo) if hi > lo else None
        return {"days": len(values), "rank": rank, "current": current, "low": lo, "high": hi,
                "as_of": rows[0]["date"], "fresh": rows[0]["date"] == datetime.now(NY).date().isoformat()}

    # --- research ----------------------------------------------------------
    def research(self, symbol: str) -> dict[str, Any]:
        symbol = self._symbol(symbol)
        quote = self.market.quote(symbol)
        enriched = self.market.enriched(symbol, 3)
        last = enriched.iloc[-1]
        validated = self._validated_patterns()
        events = recent_events(enriched, lookback=10)
        for e in events:
            e["verdicts"] = validated.get(e["key"], [])
        company = sec.build_company_research(self.sec, symbol, datetime.now(NY).date())
        if self.market.info.mode == "demo":
            company["errors"].insert(0, "Demo prices are synthetic; any SEC data shown is real and will not "
                                        "match them.")
        earnings = self._earnings(symbol, None)
        tail = enriched.tail(252)
        chart = [{"date": d.strftime("%Y-%m-%d"), "close": c, "sma50": s50, "sma200": s200}
                 for d, c, s50, s200 in zip(tail["date"], tail["close"], tail["sma50"], tail["sma200"])]
        iv_now = None
        try:
            iv_now = self.atm_iv_30d(symbol)["atm_iv"]
        except (DataError, RequestError, ValueError):
            pass
        return clean({
            "symbol": symbol, "description": quote.get("description"), "quote": quote,
            "as_of": last["date"].strftime("%Y-%m-%d"),
            "range": {"low": float(tail["low"].min()), "high": float(tail["high"].max()),
                      "position": last["pos52"]},
            "volatility": {"realized_20d": last["hv20"], "implied_30d": iv_now, "iv_rank": self.iv_rank(symbol)},
            "trend": trend_snapshot(enriched), "recent_patterns": events, "last_bar": self._last_bar(symbol, enriched),
            "earnings": earnings, "company": company, "regime": self.regime(),
            "chart": chart, "data_warnings": Market.data_warnings(symbol, enriched),
            "not_included": ["News headlines (Tradier has no news feed)",
                             "Analyst estimates and guidance",
                             "Written summaries of filings: open the linked filings to read them"],
        })

    def book_risk(self, symbol: str | None = None) -> dict[str, Any]:
        """Open risk on the Positions screen that the option journal does not already count: every
        open position except journal mirrors (their premium is in the journal's own figure), each
        on its stated basis (portfolio.exposure). Positions whose risk cannot be measured, or whose
        fills no longer add up, are listed in `unknown` and never counted as zero."""
        out: dict[str, Any] = {"total": 0.0, "symbol": 0.0, "positions": 0, "unknown": []}
        desk = getattr(self, "desk", None)
        if desk is None or not self.settings.features.get("unified_positions"):
            return out
        from .portfolio import exposure
        try:
            listing = desk.list("open")
        except (DataError, ValueError) as exc:
            out["unknown"].append({"description": "Open positions", "reason": f"could not be read ({exc})"})
            return out
        for u in listing.get("invalid", []):
            if not u.get("legacy_trade_id"):
                out["unknown"].append({"id": u["id"], "description": u["description"], "reason": "its fills no longer add up"})
        for v in listing["positions"]:
            if v.get("legacy_trade_id"):
                continue
            out["positions"] += 1
            fresh = (v.get("mark") or {}).get("freshness") or {}
            if fresh.get("from_cache"):
                # A saved quote from before the source failed can make the risk look smaller (or larger) than it is.
                out["unknown"].append({"id": v["id"], "description": v["description"],
                                       "reason": "its price is a saved copy from before the data source failed"})
                continue
            x = exposure(v, None)
            if x["open_risk"] is None:
                out["unknown"].append({"id": v["id"], "description": v["description"],
                                       "reason": x.get("risk_reason") or "not measured"})
                continue
            out["total"] += x["open_risk"]
            if symbol and v["symbol"] == symbol:
                out["symbol"] += x["open_risk"]          # reported, not counted toward the per-trade limit
        return out

    def daily_loss_status(self) -> dict[str, Any]:
        """Today's result for the daily loss stop, with how much of it could be measured. With
        portfolio risk on, it covers every position and follows DAILY_LOSS_MODE (portfolio.daily_loss);
        otherwise it is the option journal's closed trades, which are always fully known."""
        analyst = getattr(self, "analyst", None)
        if analyst is not None and self.settings.features.get("portfolio_risk"):
            try:
                d = dict(analyst.daily_loss())
                how = "closed and open" if d["mode"] == "REALIZED_PLUS_UNREALIZED" else "closed trades"
                if d["missing"]:
                    n = len(d["missing"])
                    how += f"; {n} position{'s' if n != 1 else ''} could not be measured and {'are' if n != 1 else 'is'} not counted"
                d["counted_as"] = how
                return d
            except (DataError, ValueError) as exc:
                log.warning("Daily loss from positions unavailable, using the journal only: %s", exc)
                failed = str(exc)
        else:
            failed = None
        realized = self.journal.realized_today()
        cap = self.settings.account_size * self.settings.daily_loss_stop_pct / 100.0
        loss = "STOP" if realized <= -cap else "WARN" if realized <= -0.75 * cap else "OK"
        if failed is not None:
            # The positions could not be read, so only the journal's closed trades are known: never a complete day.
            status = loss if loss != "OK" else "INCOMPLETE"
            return {"mode": "REALIZED_ONLY", "counted": realized, "known_total": realized,
                    "counted_as": "closed option-journal trades only; positions could not be read",
                    "loss_status": loss, "coverage_status": "UNAVAILABLE", "status": status, "positions": None,
                    "positions_evaluated": 0, "missing": [{"id": None, "description": "Positions", "reason": failed}],
                    "unknown": ["Positions"],
                    "text": f"Today's result could not be measured: the positions could not be read ({failed}). Closed "
                            f"option-journal trades alone come to {'+' if realized >= 0 else '-'}${abs(realized):,.0f}, so "
                            "it is not known whether the account is within the daily stop."}
        return {"mode": "REALIZED_ONLY", "counted": realized, "known_total": realized, "counted_as": "closed trades",
                "loss_status": loss, "coverage_status": "COMPLETE", "status": loss, "positions": 0,
                "positions_evaluated": 0, "missing": [], "unknown": [],
                "text": f"Closed option-journal trades today: {'+' if realized >= 0 else '-'}${abs(realized):,.0f}."}

    def loss_today(self) -> tuple[float, str]:
        """Today's measured result and what it counts (see daily_loss_status)."""
        d = self.daily_loss_status()
        return d["counted"], d["counted_as"]

    def regime(self) -> dict[str, Any]:
        try:
            spy = self.market.enriched("SPY", 3)
        except DataError as exc:
            return {"available": False, "note": f"SPY history unavailable: {exc}"}
        last = spy.iloc[-1]
        if pd.isna(last["sma200"]):
            return {"available": False, "note": "Not enough SPY history for a 200-day average"}
        above200, above50 = last["close"] > last["sma200"], last["close"] > last["sma50"]
        stacked = last["sma50"] > last["sma200"]
        if above200 and stacked:
            label, text = "uptrend", "SPY is above its 200-day average and the 50-day is above the 200-day."
        elif not above200 and not stacked:
            label, text = "downtrend", "SPY is below its 200-day average and the 50-day is below the 200-day."
        else:
            label, text = "mixed", "SPY's price and moving averages disagree: no clear trend."
        high = spy["high"].tail(252).max()
        out = {"available": True, "label": label, "text": text, "as_of": last["date"].strftime("%Y-%m-%d"),
               "spy_close": last["close"], "vs_200d_pct": 100 * (last["close"] / last["sma200"] - 1),
               "vs_50d_pct": 100 * (last["close"] / last["sma50"] - 1), "above_50d": bool(above50),
               "realized_vol_20d": last["hv20"], "off_52w_high_pct": 100 * (last["close"] / high - 1),
               "vix": None}
        try:
            vix = self.market.provider.quotes(["VIX"]).get("VIX")
            if vix and vix.get("last"):
                out["vix"] = vix["last"]
        except DataError:
            pass
        return out

    # --- technical chart and price structure -------------------------------
    def technicals(self, symbol: str, bars: int = 504, swing_atr: float = SWING_ATR) -> dict[str, Any]:
        """Daily bars with moving averages, MACD, swings, support and resistance and trendlines."""
        symbol = self._symbol(symbol)
        if swing_atr not in (1.5, 2.0, 3.0):
            raise RequestError("swing size must be 1.5, 2 or 3 average true ranges")
        bars = max(60, min(int(bars), 1260))
        enriched = self.market.enriched(symbol, max(3.0, bars / 252 + 1.1))
        structure = analyse_structure(enriched, swing_atr)
        start = max(0, len(enriched) - bars)
        tail = enriched.iloc[start:]
        cols = ["open", "high", "low", "close", "volume", "ema20", "sma50", "sma200", "macd", "macd_signal",
                "macd_hist", "rsi"]
        def r4(v: Any) -> Any:
            return round(float(v), 4) if v is not None and math.isfinite(float(v)) else None
        rows = [{"date": d.strftime("%Y-%m-%d"), **{c: r4(v) for c, v in zip(cols, vals)}, "state": int(st)}
                for d, *vals, st in zip(tail["date"], *(tail[c] for c in cols),
                                        structure["state_series"][start:])]
        first_date = rows[0]["date"] if rows else None
        swings = [s for s in structure["swings"] if first_date and s["date"] >= first_date]
        validated = self._validated_patterns()
        lab = {key: validated.get(key, []) for key in ("structure_up", "structure_down", "structure_up_break",
                                                         "structure_down_break")}
        keys = ("state", "headline", "detail", "watch", "state_since", "broke", "reversal_atr")
        bar = self._last_bar(symbol, enriched)
        confirmed = None
        if bar["status"] == PROVISIONAL:
            # What the structure says on finished bars alone, beside what today's bar so far suggests.
            done = analyse_structure(complete_only(enriched), swing_atr)
            confirmed = {k: done.get(k) for k in keys}
        return clean({
            "symbol": symbol, "as_of": rows[-1]["date"] if rows else None, "bars": rows, "swings": swings,
            "levels": structure["levels"], "trendlines": structure["trendlines"],
            "structure": {k: structure[k] for k in keys}, "structure_status": bar["status"],
            "confirmed_structure": confirmed, "last_bar": bar,
            "atr": float(enriched["atr"].iloc[-1]), "lab": lab,
            "data_warnings": Market.data_warnings(symbol, enriched),
        })

    def _last_bar(self, symbol: str, bars: pd.DataFrame) -> dict[str, Any]:
        """The date and state of the last daily bar: finished, or today's bar so far."""
        info = self.market.bar_info(symbol)
        status = signal_status(bars)
        out = {"date": bars["date"].iloc[-1].strftime("%Y-%m-%d") if len(bars) else None,
               "complete": status == CONFIRMED, "status": status, "fetched_at": info.get("fetched_at"),
               "adjustment": info.get("adjustment")}
        if status == PROVISIONAL:
            done = complete_only(bars)
            out["last_complete_date"] = done["date"].iloc[-1].strftime("%Y-%m-%d") if len(done) else None
            out["note"] = (f"Today's bar is still forming (prices as of {info.get('fetched_text', 'the last fetch')}). "
                           "Anything read from it is provisional until the close.")
        return out

    # --- option simulator ----------------------------------------------------
    def _chain_for(self, symbol: str, expiration: str) -> Any:
        entry = self._chains.get((symbol, expiration))
        if entry is None or time.time() - entry["at"] > LIVE_CHAIN_SECONDS:
            entry = self._remember_chain(symbol, expiration, self.market.chain(symbol, expiration))
        return entry["analysis"]

    def simulate(self, *, symbol: str, legs: list[dict[str, Any]], iv_shift: float = 0.0,
                 eval_date: str | None = None, range_pct: float | None = None) -> dict[str, Any]:
        """Profit and loss of a position across stock prices, dates and volatility changes.

        `iv_shift` is in volatility points (5 = five points higher) and `range_pct` in percent."""
        symbol = self._symbol(symbol)
        if not 1 <= len(legs) <= 4:
            raise RequestError("A position has one to four legs")
        if not -50 <= iv_shift <= 100:
            raise RequestError("The volatility change must be between -50 and +100 points")
        if range_pct is not None and not 2 <= range_pct <= 90:
            raise RequestError("The price range must be between 2% and 90%")
        day = None
        if eval_date:
            try:
                day = date.fromisoformat(eval_date)
            except ValueError as exc:
                raise RequestError("eval_date must look like 2026-11-05") from exc
        today = datetime.now(NY).date()
        resolved, details, analyses = [], [], {}
        for i, raw in enumerate(legs, 1):
            kind = str(raw.get("option_type", "")).lower()
            side = str(raw.get("side", "buy")).lower()
            expiration = str(raw.get("expiration", ""))
            if kind not in ("call", "put") or side not in ("buy", "sell"):
                raise RequestError(f"Leg {i}: option_type must be call or put, and side buy or sell")
            try:
                expires = date.fromisoformat(expiration)
            except ValueError as exc:
                raise RequestError(f"Leg {i}: expiration must look like 2026-11-20") from exc
            if expires < today:
                raise RequestError(f"Leg {i}: {expiration} has already expired")
            contracts = int(raw.get("contracts") or 1)
            if not 1 <= contracts <= 1000:
                raise RequestError(f"Leg {i}: contracts must be between 1 and 1000")
            strike = float(raw.get("strike"))
            analysis = analyses.get(expiration) or self._chain_for(symbol, expiration)
            analyses[expiration] = analysis
            row = analysis.find_by_terms(kind, strike)
            if row is None:
                raise RequestError(f"Leg {i}: no {symbol} {expiration} {strike:g} {kind} in the chain")
            vol = row.get("model_vol") or row.get("iv")
            if not vol:
                raise RequestError(f"Leg {i}: the {strike:g} {kind} has no quote to read a volatility from")
            entry = raw.get("entry_price")
            if entry is None:
                entry = row.get("mid")
            if entry is None or not math.isfinite(float(entry)) or float(entry) <= 0:
                raise RequestError(f"Leg {i}: the {strike:g} {kind} has no two-sided quote. Enter the price you "
                                   "expect to trade at.")
            growth = math.log(analysis.forward / analysis.spot) / analysis.t if analysis.t > 0 else 0.0
            leg = Leg(kind, strike, expiration, 1 if side == "buy" else -1, contracts, float(entry), float(vol), growth)
            resolved.append(leg)
            details.append({"description": leg.describe(), "option_symbol": row["symbol"], "option_type": kind,
                            "strike": strike, "expiration": expiration, "side": side, "contracts": contracts,
                            "entry_price": leg.entry, "entry_source": "you" if raw.get("entry_price") else "midpoint",
                            "bid": row.get("bid"), "ask": row.get("ask"), "mid": row.get("mid"),
                            "vol": leg.vol, "model_source": row.get("model_source"), "delta": row.get("delta")})
        nearest = analyses[min(analyses)]
        atm = atm_iv(nearest) or resolved[0].vol
        try:
            result = run_simulation(resolved, nearest.spot, self.settings.risk_free_rate, atm, datetime.now(NY),
                                    eval_day=day, iv_shift=iv_shift / 100.0,
                                    range_pct=range_pct / 100.0 if range_pct else None)
        except ValueError as exc:
            raise RequestError(str(exc)) from exc
        result.update({"symbol": symbol, "legs": details, "data": {"mode": self.market.info.mode,
                                                                  "label": self.market.info.label}})
        return clean(result)

    # --- pattern lab ---------------------------------------------------------
    def patterns(self) -> list[dict[str, Any]]:
        return [{"key": p.key, "label": p.label, "direction": p.direction, "description": p.description}
                for p in PATTERNS.values()]

    def _frames(self, symbols: list[str] | None, years: float) -> tuple[dict[str, pd.DataFrame], list[str]]:
        symbols = [display_symbol(s) for s in (symbols or self.settings.watchlist) if s.strip()]
        if not symbols:
            raise RequestError("Give at least one symbol")
        if len(symbols) > 60:
            raise RequestError("At most 60 symbols per run")
        frames, warnings = {}, []
        for symbol in dict.fromkeys(symbols):
            try:
                # Studies only see finished bars: today's bar so far is neither an event nor an outcome.
                frames[symbol] = self.market.enriched(symbol, years, complete=True)
                warnings.extend(Market.data_warnings(symbol, frames[symbol]))
            except DataError as exc:
                warnings.append(f"{symbol}: {exc}")
        if not frames:
            raise RequestError("No price history could be loaded for those symbols")
        return frames, warnings

    def run_patterns(self, symbols: list[str] | None = None, years: float = 5.0,
                     horizons: list[int] | None = None) -> dict[str, Any]:
        horizons = sorted({int(h) for h in (horizons or [5, 10, 20]) if 1 <= int(h) <= 60})
        if not horizons:
            raise RequestError("Horizons must be between 1 and 60 trading days")
        frames, warnings = self._frames(symbols, years)
        benchmark = None
        if len(frames) < MIN_SYMBOLS:
            try:
                benchmark = self.market.bars("SPY", years, complete=True)
            except DataError as exc:
                raise RequestError(f"A run with fewer than {MIN_SYMBOLS} symbols is shown against SPY, and SPY "
                                   f"history could not be loaded: {exc}") from exc
        try:
            study = run_study(frames, horizons, self.settings.stock_cost_bps, benchmark=benchmark,
                              benchmark_label="SPY")
        except ValueError as exc:
            raise RequestError(str(exc)) from exc
        study["data_warnings"] = warnings
        study["data_source"] = self.market.info.mode
        if self.market.info.mode == "demo":
            study["caveats"].insert(0, "This run used synthetic demo prices, which are random by construction. "
                                       "Its verdicts say nothing about real markets.")
        run_id = uuid.uuid4().hex[:12]
        run_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
        study = clean(study)
        with self.db.tx() as cur:
            for r in study["results"]:
                cur.execute("INSERT INTO pattern_results (run_id, run_at, source, pattern, horizon, verdict, "
                            "result_json) VALUES (?, ?, ?, ?, ?, ?, ?)",
                            (run_id, run_at, self.market.info.mode, r["pattern"], r["horizon"], r["verdict"],
                             json.dumps(r)))
            meta = {k: v for k, v in study.items() if k != "results"}
            cur.execute("INSERT INTO kv_cache (key, value, fetched_at) VALUES ('patterns:last_run', ?, ?) "
                        "ON CONFLICT(key) DO UPDATE SET value = excluded.value, fetched_at = excluded.fetched_at",
                        (json.dumps({"run_id": run_id, "run_at": run_at, **meta}), datetime.now().timestamp()))
        study.update({"run_id": run_id, "run_at": run_at})
        from . import runs
        runs.attach(self.db, study, "PATTERNS", {"symbols": sorted(frames), "years": years, "horizons": horizons,
                                                 "cost_bps": self.settings.stock_cost_bps},
                    {**frames, **({"SPY": benchmark} if benchmark is not None else {})}, self.market.info.mode, None,
                    ("caveats",))
        return study

    def last_pattern_run(self) -> dict[str, Any] | None:
        meta = self.db.cache_get("patterns:last_run")
        if not meta:
            return None
        rows = self.db.query("SELECT result_json FROM pattern_results WHERE run_id = ? ORDER BY id",
                             (meta["run_id"],))
        return {**meta, "results": [json.loads(r["result_json"]) for r in rows]}

    def _validated_patterns(self) -> dict[str, list[dict[str, Any]]]:
        """Latest lab verdicts per pattern, from this database's most recent run. Demo runs
        live in a separate database file, so they never appear beside real data."""
        last = self.last_pattern_run()
        if not last:
            return {}
        out: dict[str, list[dict[str, Any]]] = {}
        for r in last["results"]:
            out.setdefault(r["pattern"], []).append({"horizon": r["horizon"], "verdict": r["verdict"]})
        for verdicts in out.values():
            verdicts.sort(key=lambda v: v["horizon"])
        return out

    def chart(self, symbol: str, pattern: str | None = None, bars: int = 260) -> dict[str, Any]:
        symbol = self._symbol(symbol)
        if pattern and pattern not in PATTERNS:
            raise RequestError(f"Unknown pattern {pattern}")
        enriched = self.market.enriched(symbol, 5)
        hits = detect_all(enriched)[pattern] if pattern else None
        tail = enriched.tail(bars)
        rows = [{"date": d.strftime("%Y-%m-%d"), "open": o, "high": h, "low": l, "close": c}
                for d, o, h, l, c in zip(tail["date"], tail["open"], tail["high"], tail["low"], tail["close"])]
        events = [enriched["date"].iloc[i].strftime("%Y-%m-%d") for i in tail.index if hits is not None and hits.iloc[i]]
        return clean({"symbol": symbol, "pattern": pattern, "bars": rows, "events": events,
                      "direction": PATTERNS[pattern].direction if pattern else 0})

    # --- backtests -----------------------------------------------------------
    def backtest(self, symbols: list[str] | None, years: float, params: dict[str, Any], n_trials: int = 1
                 ) -> dict[str, Any]:
        frames, warnings = self._frames(symbols, years + WARMUP_YEARS)
        start = (datetime.now(NY).date() - timedelta(days=int(years * 365.25))).isoformat()
        try:
            result = run_backtest(frames, self._params(params), start=start, n_trials=n_trials)
        except (ValueError, TypeError) as exc:
            raise RequestError(str(exc)) from exc
        result["data_warnings"] = warnings
        result["data_source"] = self.market.info.mode
        from dataclasses import asdict
        from . import runs
        runs.attach(self.db, result, "BACKTEST", {"params": asdict(self._params(params)), "symbols": sorted(frames),
                                                  "years": years, "start": start, "n_trials": n_trials},
                    frames, self.market.info.mode, None,
                    ("total_return_pct", "sharpe", "max_drawdown_pct", "num_trades", "start_date", "end_date"))
        if len(result["trades"]) > 500:
            result["trades_truncated"] = len(result["trades"])
            result["trades"] = result["trades"][-500:]
        return clean(result)

    def walk_forward(self, symbols: list[str] | None, years: float, params: dict[str, Any],
                     train_days: int = 252, test_days: int = 63) -> dict[str, Any]:
        if not 60 <= train_days <= 1000 or not 20 <= test_days <= 500:
            raise RequestError("Training window must be 60 to 1000 days and test window 20 to 500 days")
        frames, warnings = self._frames(symbols, years + WARMUP_YEARS)
        try:
            result = walk_forward(frames, self._params(params), train_days=train_days, test_days=test_days)
        except (ValueError, TypeError) as exc:
            raise RequestError(str(exc)) from exc
        result["data_warnings"] = warnings
        result["data_source"] = self.market.info.mode
        from dataclasses import asdict
        from . import runs
        runs.attach(self.db, result, "WALK_FORWARD", {"params": asdict(self._params(params)), "symbols": sorted(frames),
                                                      "years": years, "train_days": train_days, "test_days": test_days},
                    frames, self.market.info.mode, None, ("out_of_sample", "avg_train_sharpe", "avg_test_sharpe"))
        return clean(result)

    def _params(self, overrides: dict[str, Any]) -> BacktestParams:
        allowed = set(BacktestParams.__dataclass_fields__)
        unknown = set(overrides) - allowed
        if unknown:
            raise RequestError(f"Unknown backtest settings: {', '.join(sorted(unknown))}")
        base = BacktestParams(slippage_bps=self.settings.stock_cost_bps)
        values = {**base.__dict__, **{k: v for k, v in overrides.items() if v is not None}}
        return BacktestParams(**values)

    # --- helpers -------------------------------------------------------------
    @staticmethod
    def _symbol(symbol: str) -> str:
        symbol = display_symbol(symbol or "")
        if not symbol or len(symbol) > 8 or not all(ch.isalnum() or ch == "." for ch in symbol):
            raise RequestError("Enter a ticker such as AAPL")
        return symbol


def _rung(ladder: dict[str, Any], name: str) -> float:
    for rung in ladder["rungs"]:
        if name in rung["name"]:
            return rung["price"]
    return ladder["rungs"][-1]["price"]
