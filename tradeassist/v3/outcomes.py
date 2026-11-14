"""Strategy outcomes of Qualified setups (4.5.3): the decision frozen when it is made, graded after its horizon, in
three categories that are never mixed.

When a Qualified setup is recorded (a new or materially changed one), its full decision record is frozen in
`scenario_strategy_outcomes`: the stock, study, scenario version and valuation, when it was recommended, the
entry policy (bought legs at the snapshot's ask, sold legs at its bid; shares at the ask, a short at the bid), the
exit policy (held to the session it was valued at, then closed), each leg's option symbol, strike, expiration, side,
contracts, bid and ask, the units, the entry cost, the round-trip cost estimate, the most it can lose, the capital,
the projected net P&L in each case, the data quality and liquidity, and the setup's direction.

After the exit session has settled it is graded:

| category | when | how |
| --- | --- | --- |
| **historical hypothetical** | every leg has a tradable historical price | shares: the exit session's close, less STOCK_COST_BPS each way; an option that expired by then: its intrinsic value at its expiration's close (exact); one still open: the contract's own daily close that session from Tradier's history, less half the entry spread (bought) or plus it (sold) |
| **model-only** | an open option leg has no trade that session (or the data source has no option history, as on demo data) | that leg is valued by Black-76 at the realized stock close with its entry volatility: a theoretical number, labelled so |
| **ungradable** | the stock's close for a needed session is missing two weeks after it | nothing is invented |

Separately, **observed execution**: fills actually recorded by the Trading Desk (paper or live) for the study after
the recommendation, with their realized result when every position they opened was closed. A hypothetical or
model-only P&L is never called realized.
"""

from __future__ import annotations

import json
import logging
import math
import statistics
import time
from datetime import date, datetime, timezone
from typing import Any

import pandas as pd

from .. import contracts as C
from ..market import settled_through
from ..optionmath import black76_price

log = logging.getLogger(__name__)

CATEGORIES = ("observed", "historical_hypothetical", "model_only", "ungradable")
CATEGORY_LABELS = {"observed": "Observed execution", "historical_hypothetical": "Historical hypothetical", "model_only": "Model-only",
                   "ungradable": "Ungradable"}
GIVE_UP_DAYS = 14
MIN_OUTCOMES = 20


def _f(x: Any) -> float | None:
    try:
        v = float(x)
    except (TypeError, ValueError):
        return None
    return v if math.isfinite(v) else None


class Outcomes:
    def __init__(self, svc: Any):
        self.svc = svc

    # --- freezing the decision ------------------------------------------------------------------------------------
    def freeze(self, study_id: str, decision: dict[str, Any], valuation: dict[str, Any], version: dict[str, Any]) -> str | None:
        """Freeze a Qualified setup's decision record (once per decision)."""
        if decision.get("status") != "qualified" or not decision.get("setup"):
            return None
        if version.get("origin") not in (None, "auto"):
            return None                     # 4.7: a promoted (person-edited) version's setup is not part of the outcome record
        s = decision["setup"]
        by_key = {x["key"]: x for x in valuation.get("structures") or []}
        parts = []
        for key in s.get("structures") or []:
            st = by_key.get(key)
            if st is None:
                return None
            units = int((s.get("units") or {}).get(key) or 1)
            legs = [{k: lg.get(k) for k in ("option_symbol", "option_type", "strike", "expiration", "side", "contracts", "bid", "ask", "mid",
                                               "spread_pct", "iv", "open_interest", "volume", "quote_time")} for lg in st.get("legs") or []]
            entry_cash = None
            if legs:
                entry_cash = 0.0
                for lg in legs:
                    px = _f(lg["ask"]) if lg["side"] == "buy" else _f(lg["bid"])
                    if px is None:
                        return None                                     # no executable entry price: not frozen as a trade
                    lg["entry_price"] = px
                    entry_cash += (1 if lg["side"] == "buy" else -1) * px * float(lg.get("contracts") or 1) * 100.0 * units
            parts.append({"key": key, "label": st.get("label"), "units": units, "quantity": st.get("quantity"), "entry": st.get("entry"),
                          "legs": legs, "entry_cash": entry_cash, "eval_date": st.get("eval_date"), "eval_session": st.get("eval_session"),
                          "costs": st.get("costs"), "capital": st.get("capital"), "max_loss": st.get("max_loss"),
                          "quality": (st.get("quality") or {}).get("tier")})
        if not parts:
            return None
        exit_date = max(p["eval_date"] for p in parts if p.get("eval_date"))
        row = self.svc.db.one("SELECT symbol, mode, horizon_days, pipeline_json FROM studies WHERE id = ?", (study_id,))
        pl = json.loads(row["pipeline_json"] or "{}") if row else {}
        oid = C.new_id("sout")
        rec = {"entry_policy": "bought legs at the snapshot's ask, sold legs at its bid; shares at the ask, a short sale at the bid",
               "exit_policy": f"held to the session it was valued at ({exit_date}), then closed at that session's close",
               "parts": parts, "case": s.get("case"), "mode": s.get("mode"), "projected": s.get("by_case"),
               "net_pnl_projected": s.get("net_pnl"), "costs_estimated": s.get("costs"), "capital": s.get("capital"),
               "max_loss": s.get("max_loss"), "data_quality": s.get("data_quality"), "snapshot_at": valuation.get("snapshot_at"),
               "anchor": (version.get("anchor") or {}).get("price")}
        with self.svc.db.tx() as cur:
            cur.execute("""INSERT OR IGNORE INTO scenario_strategy_outcomes (id, decision_id, study_id, version_id, valuation_id, symbol, mode, profile,
                           catalyst_kind, recommended_at, direction, strategy, exit_date, horizon, record_json, status, created_at)
                           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'open', ?)""",
                        (oid, decision["id"], study_id, version.get("id"), valuation.get("id"), row["symbol"] if row else version.get("symbol"),
                         row["mode"] if row else self.svc.market.info.mode, pl.get("profile"), (version.get("catalyst") or {}).get("kind"),
                         decision.get("made_at") or C.utc_now(), s.get("direction"), "+".join(p["key"] for p in parts), exit_date,
                         int(row["horizon_days"]) if row else int(version.get("horizon_sessions") or 0), json.dumps(rec, default=str), C.utc_now()))
        return oid

    # --- grading ----------------------------------------------------------------------------------------------------
    def grade(self, now: float | None = None, limit: int = 200) -> dict[str, Any]:
        now = time.time() if now is None else now
        settled = settled_through(now)
        mode = self.svc.market.info.mode
        rows = [r for r in self.svc.db.query("SELECT * FROM scenario_strategy_outcomes WHERE status = 'open' AND mode = ? ORDER BY exit_date", (mode,))
                if date.fromisoformat(r["exit_date"]) <= settled][:limit]
        graded = 0
        for r in rows:
            try:
                out = self.grade_one(dict(r), settled)
            except Exception as exc:  # noqa: BLE001 - left for the next pass
                log.warning("Could not grade outcome %s: %s", r["id"], exc)
                continue
            if out is None:
                continue
            with self.svc.db.tx() as cur:
                cur.execute("""UPDATE scenario_strategy_outcomes SET status = ?, category = ?, pnl = ?, return_on_risk = ?, outcome_json = ?, graded_at = ?
                               WHERE id = ? AND status = 'open'""",
                            (out["status"], out["category"], out.get("pnl"), out.get("return_on_risk"), json.dumps(out, default=str),
                             C.utc_now(), r["id"]))
                graded += cur.rowcount
        return {"graded": graded}

    def _closes(self, symbol: str) -> pd.Series:
        bars = self.svc.market.bars(symbol, years=1.0, complete=True)
        return pd.Series(bars["close"].to_numpy(float), index=pd.to_datetime(bars["date"]).dt.date)

    def _option_close(self, option_symbol: str, day: date) -> float | None:
        if self.svc.market.info.mode == "demo":
            return None
        fetch = getattr(self.svc.market.provider, "option_history", None)
        if fetch is None:
            return None
        try:
            frame = fetch(option_symbol, day.isoformat(), day.isoformat())
        except Exception:  # noqa: BLE001 - no trade that day is the common case
            return None
        if frame is None or not len(frame):
            return None
        hit = frame[pd.to_datetime(frame["date"]).dt.date == day]
        return _f(hit["close"].iloc[-1]) if len(hit) else None

    def grade_one(self, r: dict[str, Any], settled: date) -> dict[str, Any] | None:
        rec = json.loads(r["record_json"])
        exit_day = date.fromisoformat(r["exit_date"])
        try:
            closes = self._closes(r["symbol"])
        except Exception as exc:  # noqa: BLE001
            if (settled - exit_day).days > GIVE_UP_DAYS:
                return {"status": "ungradable", "category": "ungradable", "reason": f"no price history: {str(exc)[:160]}"}
            return None
        bps = float(self.svc.settings.stock_cost_bps) / 1e4
        rate = float(self.svc.settings.risk_free_rate)
        category = "historical_hypothetical"
        notes: list[str] = []
        total = 0.0
        gross_cost = 0.0
        all_bought_expired_worthless = None
        for p in rec["parts"]:
            day = date.fromisoformat(p["eval_date"])
            if day not in closes.index:
                if (settled - day).days > GIVE_UP_DAYS:
                    return {"status": "ungradable", "category": "ungradable", "reason": f"no close for {r['symbol']} on {day}"}
                return None
            close = float(closes[day])
            units = int(p.get("units") or 1)
            if not p["legs"]:
                qty = float(p.get("quantity") or 1) * units
                entry = float(p["entry"])
                sign = 1.0 if p["key"] == "shares" else -1.0
                cost = bps * qty * (entry + close)
                total += sign * qty * (close - entry) - cost
                gross_cost += cost
                continue
            for lg in p["legs"]:
                exp = date.fromisoformat(str(lg["expiration"]))
                n = float(lg.get("contracts") or 1) * units * 100.0
                side = 1.0 if lg["side"] == "buy" else -1.0
                cp = 1.0 if lg["option_type"] == "call" else -1.0
                strike = float(lg["strike"])
                entry = float(lg["entry_price"])
                half = 0.5 * max(0.0, (_f(lg.get("ask")) or entry) - (_f(lg.get("bid")) or entry))
                gross_cost += 2 * half * n
                if exp <= day:
                    at = exp if exp in closes.index else day
                    value = max(0.0, cp * (float(closes[at]) - strike))
                    if side > 0:
                        all_bought_expired_worthless = (all_bought_expired_worthless is not False) and value == 0.0
                    notes.append(f"{lg['option_symbol']}: expired {exp}, intrinsic {value:.2f}")
                else:
                    px = self._option_close(str(lg["option_symbol"]), day)
                    if px is not None:
                        value = max(0.0, px - side * half)
                        notes.append(f"{lg['option_symbol']}: closed {day} at {px:.2f}, {'less' if side > 0 else 'plus'} half the entry spread")
                        if side > 0:
                            all_bought_expired_worthless = False
                    else:
                        iv = _f(lg.get("iv"))
                        if iv is None:
                            if (settled - day).days > GIVE_UP_DAYS:
                                return {"status": "ungradable", "category": "ungradable", "reason": f"{lg['option_symbol']}: no price and no volatility"}
                            return None
                        t = max(0.0, ((exp - day).days) / 365.0)
                        value = black76_price(close * math.exp(rate * t), strike, t, rate, iv, lg["option_type"])
                        category = "model_only"
                        notes.append(f"{lg['option_symbol']}: no trade on {day}; Black-76 at the realized close {close:.2f} and the entry "
                                     f"volatility {iv:.2f} (model-only)")
                        if side > 0:
                            all_bought_expired_worthless = False
                total += side * n * (value - entry)
        risk = _f(rec.get("max_loss")) or _f(rec.get("capital"))
        anchor = _f(rec.get("anchor"))
        moved = None
        if anchor and exit_day in closes.index:
            moved = 100.0 * (float(closes[exit_day]) / anchor - 1.0)
        want = 1.0 if r["direction"] == "LONG" else -1.0 if r["direction"] == "SHORT" else 0.0
        observed = self.observed(r)
        return {"status": "graded", "category": category, "pnl": round(total, 2), "return_on_risk": (total / risk) if risk else None,
                "gross_pnl": round(total + gross_cost, 2), "costs": round(gross_cost, 2), "risk_basis": "most it can lose" if rec.get("max_loss") else "capital",
                "stock_move_pct": moved, "direction_right": (moved * want > 0) if (moved is not None and want) else None,
                "expired_worthless": bool(all_bought_expired_worthless) if all_bought_expired_worthless is not None else None,
                "notes": notes, "observed": observed,
                "label": CATEGORY_LABELS[category] + (": a theoretical number, not a price anyone traded at" if category == "model_only" else
                                                      ": the frozen entry and exit rules on recorded prices, not a realized profit")}

    def observed(self, r: dict[str, Any]) -> dict[str, Any] | None:
        """Fills the Trading Desk recorded for this study after the recommendation (paper or live), and their realized
        result when every position they opened was closed again."""
        rows = self.svc.db.query("""SELECT p.id AS proposal_id, p.account_kind, p.instrument, p.side, p.terms_json, f.quantity, f.price, f.fees, f.at
                                    FROM trade_proposals p JOIN broker_orders o ON o.proposal_id = p.id JOIN broker_fills f ON f.order_id = o.id
                                    WHERE p.study_id = ? AND p.created_at >= ? ORDER BY f.at""", (r["study_id"], r["recommended_at"]))
        if not rows:
            return None
        pos: dict[str, float] = {}
        cash = fees = 0.0
        for x in rows:
            terms = json.loads(x["terms_json"] or "{}")
            key = terms.get("option_symbol") or terms.get("symbol") or x["instrument"]
            mult = 100.0 if x["instrument"] == "option" else 1.0
            sign = 1.0 if str(x["side"]).lower().startswith("buy") else -1.0
            pos[key] = pos.get(key, 0.0) + sign * float(x["quantity"])
            cash -= sign * float(x["quantity"]) * float(x["price"]) * mult
            fees += float(x["fees"] or 0.0)
        closed = all(abs(v) < 1e-9 for v in pos.values())
        return {"fills": len(rows), "accounts": sorted({x["account_kind"] for x in rows}), "closed": closed,
                "realized_pnl": round(cash - fees, 2) if closed else None,
                "words": (f"{len(rows)} recorded fill(s); realized {cash - fees:+,.2f} after fees" if closed else
                          f"{len(rows)} recorded fill(s); a position is still open, so nothing is realized yet")}

    # --- the summary ------------------------------------------------------------------------------------------------
    def summary(self, horizon: int | None = None) -> dict[str, Any]:
        mode = self.svc.market.info.mode
        sql, params = "SELECT * FROM scenario_strategy_outcomes WHERE mode = ?", [mode]
        if horizon:
            sql += " AND horizon = ?"
            params.append(int(horizon))
        rows = [dict(r) for r in self.svc.db.query(sql + " ORDER BY exit_date, created_at", params)]
        out: dict[str, Any] = {"counts": {"open": sum(1 for r in rows if r["status"] == "open"),
                                          **{c: sum(1 for r in rows if r.get("category") == c) for c in CATEGORIES if c != "observed"}},
                               "categories": {}, "min_outcomes": MIN_OUTCOMES}
        for cat in ("historical_hypothetical", "model_only"):
            g = [r for r in rows if r["status"] == "graded" and r["category"] == cat]
            out["categories"][cat] = {"label": CATEGORY_LABELS[cat], **metrics(g)}
        obs = []
        for r in rows:
            o = (json.loads(r["outcome_json"] or "{}").get("observed") or {}) if r.get("outcome_json") else {}
            if o.get("closed") and o.get("realized_pnl") is not None:
                obs.append({"pnl": o["realized_pnl"], "return_on_risk": None, "outcome_json": json.dumps({}), **{k: r[k] for k in ("strategy", "direction",
                            "catalyst_kind", "horizon", "exit_date")}})
        out["categories"]["observed"] = {"label": CATEGORY_LABELS["observed"], **metrics(obs, observed=True)}
        out["note"] = ("Three categories, never added together: fills actually recorded (observed), the frozen entry and exit rules on recorded "
                       "historical prices (historical hypothetical), and a theoretical value where no price traded (model-only). Neither of the last "
                       f"two is a realized profit. Below {MIN_OUTCOMES} outcomes a category is too small to judge.")
        return out


def metrics(rows: list[dict[str, Any]], observed: bool = False) -> dict[str, Any]:
    n = len(rows)
    if not n:
        return {"n": 0}
    pnl = [float(r["pnl"]) for r in rows]
    wins = [x for x in pnl if x > 0]
    losses = [x for x in pnl if x <= 0]
    cum, peak, dd = 0.0, 0.0, 0.0
    for x in pnl:
        cum += x
        peak = max(peak, cum)
        dd = min(dd, cum - peak)
    ror = [float(r["return_on_risk"]) for r in rows if r.get("return_on_risk") is not None]
    outs = [json.loads(r.get("outcome_json") or "{}") for r in rows]
    gross = [o.get("gross_pnl") for o in outs if o.get("gross_pnl") is not None]
    costs = [o.get("costs") for o in outs if o.get("costs") is not None]
    worthless = [o.get("expired_worthless") for o in outs if o.get("expired_worthless") is not None]
    dir_right_lost = sum(1 for o, x in zip(outs, pnl) if o.get("direction_right") is True and x <= 0)

    def by(key: str) -> dict[str, Any]:
        groups: dict[str, list[float]] = {}
        for r in rows:
            groups.setdefault(str(r.get(key) or "unknown"), []).append(float(r["pnl"]))
        return {k: {"n": len(v), "net_pnl": round(sum(v), 2), "win_rate": sum(1 for x in v if x > 0) / len(v)} for k, v in groups.items()}
    return {"n": n, "enough": n >= MIN_OUTCOMES, "win_rate": len(wins) / n, "net_pnl": round(sum(pnl), 2),
            "return_on_risk": statistics.fmean(ror) if ror else None, "average_win": statistics.fmean(wins) if wins else None,
            "average_loss": statistics.fmean(losses) if losses else None, "max_drawdown": round(dd, 2), "worst_loss": round(min(pnl), 2),
            "cost_to_gross": (sum(costs) / sum(abs(g) for g in gross)) if gross and sum(abs(g) for g in gross) > 0 else None,
            "expired_worthless_rate": (sum(1 for w in worthless if w) / len(worthless)) if worthless else None,
            "direction_right_strategy_lost": dir_right_lost,
            "by_strategy": by("strategy"), "by_direction": by("direction"), "by_catalyst": by("catalyst_kind"), "by_horizon": by("horizon")}


def install(svc: Any) -> None:
    svc.outcomes = Outcomes(svc)


__all__ = ["Outcomes", "install", "metrics", "CATEGORIES", "CATEGORY_LABELS"]
_ = (datetime, timezone)
