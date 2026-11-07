"""Discover 3.0: one opportunity feed, deduplicated and ranked on one scale.

2.x had five lists (the Morning plan, Trade ideas, Live news, the Scanner, Reversals), each scored its own way, and
the hub sorted them together as though the scores were comparable; a stock flagged by three of them appeared three
times. 3.0 keeps the scanners and changes what they produce: each one now records an **observation**, and the
observations of one stock, one direction and one horizon are merged into one **candidate**
(`opportunity_candidates`, at most one active per stock, direction and horizon).

A candidate's rank is the weighted sum of components that mean the same whichever scanner found it, each shown:

| component | weight | what it measures |
| --- | --- | --- |
| historical support | 0.35 | the after-cost record of the same trade (the edge), discounted when the record is short |
| setup quality | 0.25 | the best scanner score among its observations, on that scanner's own 0-100 scale |
| catalyst | 0.15 | news behind the move now (1), an earnings release inside the horizon (0.6) |
| liquidity | 0.15 | the stock's average dollar volume |
| freshness | 0.10 | how recently the newest observation was made, against the horizon |
| execution cost | -0.10 | the quoted spread |
| conflict | -0.15 | an active candidate in the opposite direction on the same stock and horizon |

A candidate without a validated historical edge can rank and be researched, but is never eligible for the
autopilot; neither is one with a conflict, poor liquidity, or prices that are made up (demo data).
"""

from __future__ import annotations

import json
import logging
import math
import threading
from datetime import datetime, timedelta
from typing import Any

from fastapi import APIRouter
from pydantic import BaseModel, ConfigDict, Field

from .. import contracts as C
from ..marketdays import add_trading_days
from ..optionmath import NY
from ..providers.base import DataError
from ..service import RequestError

log = logging.getLogger(__name__)

WEIGHTS = {"historical_support": 0.35, "setup_quality": 0.25, "catalyst": 0.15, "liquidity": 0.15, "freshness": 0.10,
           "execution_cost": -0.10, "conflict": -0.15}
LABELS = {"historical_support": "Historical support", "setup_quality": "Setup quality", "catalyst": "Catalyst",
          "liquidity": "Liquidity", "freshness": "Freshness", "execution_cost": "Execution cost", "conflict": "Conflict"}
SWING_DAYS_ALIVE = 3                    # a swing candidate lives three trading days past its newest observation
MIN_DOLLAR_VOLUME = 20e6                # below this a stock is too thin for the autopilot
VALIDATED_CASES = 30


class RefreshIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    sources: list[str] = Field(default_factory=lambda: ["morning", "ideas", "live", "reversal"])
    scan_symbols: list[str] | None = Field(default=None, max_length=100)


class AllocateIn(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)
    budget: float = Field(gt=0, le=10_000_000)
    candidate_ids: list[str] | None = Field(default=None, max_length=40)
    top_n: int = Field(default=8, ge=1, le=40)
    goal: str = "balanced"
    tail_pct: float = Field(default=2.0, ge=0.1, le=20)
    max_share: float = Field(default=0.25, gt=0, le=1)
    max_positions: int = Field(default=8, ge=1, le=40)
    sector_cap: float = Field(default=0.4, gt=0, le=1)
    instruments: list[str] = Field(default_factory=lambda: ["shares", "short", "call", "put"])
    horizon: str | None = None


def _now() -> datetime:
    return datetime.now(NY)


def _clip(x: float, lo: float = 0.0, hi: float = 1.0) -> float:
    return float(min(hi, max(lo, x)))


def _horizon(h: str | None) -> str:
    return {"day": "intraday", "intraday": "intraday", "swing": "swing", "event": "event"}.get(str(h or "swing"), "swing")


def _kind_of(setup: str | None, source: str) -> str:
    s = str(setup or "").lower()
    if "fade" in s:
        return "fade"
    if "revers" in s or source == "reversal":
        return "reversal"
    if "ride" in s or "news" in s or source == "live":
        return "news"
    if "breakout" in s or "high" in s:
        return "breakout"
    if "pullback" in s:
        return "pullback"
    if "gap" in s:
        return "gap"
    if "earn" in s:
        return "earnings"
    return "momentum" if source in ("ideas", "scanner") else "other"


def _edge(edge: dict[str, Any] | None) -> dict[str, Any]:
    e = edge or {}
    known = bool(e.get("known"))
    cases = int(e.get("cases") or 0)
    exp = float(e.get("expected_pct") or 0.0)
    validated = known and cases >= VALIDATED_CASES and exp > 0 and bool(e.get("enough", cases >= VALIDATED_CASES))
    return {"known": known, "expected_pct": exp, "cases": cases, "low_pct": None, "high_pct": None, "validated": validated,
            "independent_cases": None, "basis": str(e.get("basis") or ("no record" if not known else ""))[:300]}


class Discover:
    def __init__(self, svc: Any):
        self.svc = svc
        self._lock = threading.RLock()
        self._liquidity: dict[str, tuple[float, dict[str, Any]]] = {}
        self._caught_up = 0.0

    @property
    def mode(self) -> str:
        return self.svc.market.info.mode

    # --- observations from each scanner ------------------------------------------
    def _from_opportunity(self, o: dict[str, Any], source: str) -> dict[str, Any]:
        """A hub opportunity (Morning plan, Trade ideas, Live news) as an observation."""
        news = o.get("news") or {}
        catalyst = None
        if source == "live":
            catalyst = o.get("headline")
        elif isinstance(news, dict) and news.get("state") in ("fresh", "found", "news") and news.get("items"):
            catalyst = (news["items"][0] or {}).get("headline")
        return {"source": source, "source_key": o["key"], "symbol": o["symbol"], "direction": o["direction"],
                "horizon": _horizon(o.get("horizon")), "kind": _kind_of(o.get("setup_label") or o.get("setup"), source),
                "raw_score": float(o.get("score") or 0.0), "entry": o.get("entry"), "invalidation": o.get("stop"),
                "target": o.get("target_1"), "catalyst": catalyst, "edge": _edge(o.get("edge")),
                "payload": {"setup_label": o.get("setup_label"), "why": (o.get("why") or [])[:4], "score_parts": o.get("score_parts"),
                            "horizon_days": o.get("horizon_days"), "target_2": o.get("target_2"), "atr": o.get("atr"),
                            "option": o.get("option"), "built_at": o.get("built_at"), "source_score_scale": "0-100"}}

    def observe_hub(self, sources: list[str]) -> tuple[list[dict[str, Any]], list[str]]:
        hub = getattr(self.svc, "hub", None)
        obs, notes = [], []
        if hub is None:
            return obs, ["The scanners are not running in this process."]
        for source, fn in (("morning", "morning_list"), ("ideas", "ideas_list"), ("live", "live_list")):
            if source not in sources:
                continue
            try:
                got = getattr(hub, fn)()
                opps = got[0] if isinstance(got, tuple) else got
            except Exception as exc:  # noqa: BLE001 - one scanner failing must not stop the rest
                notes.append(f"{source}: {str(exc)[:160]}")
                continue
            for o in opps or []:
                try:
                    obs.append(self._from_opportunity(o, source))
                except (KeyError, TypeError, ValueError):
                    continue
        return obs, notes

    def observe_reversals(self, symbols: list[str] | None = None) -> tuple[list[dict[str, Any]], list[str]]:
        analyst = getattr(self.svc, "analyst", None)
        if analyst is None:
            return [], ["Reversals: the analyst is not running."]
        try:
            r = analyst.reversal_scan(symbols)
        except (ValueError, DataError) as exc:
            return [], [f"Reversals: {exc}"]
        day = _now().date().isoformat()
        obs = []
        for row in r.get("rows") or []:
            if row.get("stage") not in ("CONFIRMED", "CANDIDATE") or not row.get("leading"):
                continue
            d = "LONG" if row["side"] == "bottom" else "SHORT"
            obs.append({"source": "reversal", "source_key": f"reversal:{day}:{row['symbol']}:{row['side']}:{row['stage']}",
                        "symbol": row["symbol"], "direction": d, "horizon": "swing", "kind": "reversal", "raw_score": float(row.get("score") or 0),
                        "entry": row.get("close"), "invalidation": row.get("invalidation"), "target": row.get("target_1"),
                        "catalyst": None, "edge": _edge(None),
                        "payload": {"stage": row["stage"], "headline": row.get("headline"), "since": row.get("since"), "source_score_scale": "0-100"}})
        return obs, [f"Reversals: {e}" for e in (r.get("errors") or [])[:3]]

    def observe_scanner(self, symbols: list[str]) -> tuple[list[dict[str, Any]], list[str]]:
        from ..scanner import Scanner
        analyst = getattr(self.svc, "analyst", None)
        try:
            r = Scanner(self.svc, analyst).technical(symbols=symbols, only_signals=True)
        except (ValueError, DataError) as exc:
            return [], [f"Scanner: {exc}"]
        from ..opportunities import cost_pct, edge_from
        obs = []
        for row in r.get("rows") or []:
            for sig in row.get("signals") or []:
                try:
                    d = int(sig.get("direction") or 0)
                except (TypeError, ValueError):
                    continue
                if d == 0:
                    continue
                past = sig.get("past") or {}
                excess, cases = past.get("mean_excess_vs_spy"), past.get("independent_periods") or past.get("cases")
                # As Trade Ideas reads it (2.7): the average move against SPY ten days on, the signal's way, scaled to a
                # five-day swing and pulled toward zero by its independent periods, less trading costs.
                edge = (edge_from(100 * d * float(excess), int(cases), cost_pct=cost_pct(self.svc.settings), scale=0.5,
                                  frequency=past.get("frequency"), basis=f"{sig.get('label')}: the signal's past record across stocks")
                        if excess is not None and cases else None)
                obs.append({"source": "scanner", "source_key": f"scanner:{row['date']}:{row['symbol']}:{sig.get('signal_key') or sig.get('key')}",
                            "symbol": row["symbol"], "direction": "LONG" if d > 0 else "SHORT", "horizon": "swing",
                            "kind": _kind_of(sig.get("key"), "scanner"), "raw_score": 60.0 if sig.get("status") == "confirmed" else 45.0,
                            "entry": row.get("close"), "invalidation": None, "target": None, "catalyst": None, "edge": _edge(edge),
                            "payload": {"signal": sig.get("label"), "status": sig.get("status"), "bars_ago": sig.get("bars_ago"),
                                        "rsi": row.get("rsi"), "source_score_scale": "45 (forming) or 60 (confirmed)"}})
        return obs, [f"Scanner: {e}" for e in (r.get("errors") or [])[:3]]

    # --- merging and ranking -----------------------------------------------------------
    def _liquidity_of(self, symbol: str) -> dict[str, Any]:
        hit = self._liquidity.get(symbol)
        if hit and (datetime.now().timestamp() - hit[0]) < 1800:
            return hit[1]
        out: dict[str, Any] = {"dollar_volume": None, "spread_bps": None}
        try:
            bars = self.svc.market.bars(symbol, 0.25, complete=True).tail(20)
            out["dollar_volume"] = float((bars["close"] * bars["volume"]).mean())
        except (DataError, KeyError, ValueError):
            pass
        try:
            q = self.svc.market.quote(symbol)
            bid, ask = q.get("bid"), q.get("ask")
            if bid and ask and ask >= bid > 0:
                out["spread_bps"] = float(1e4 * (ask - bid) / ((ask + bid) / 2))
        except (DataError, ValueError):
            pass
        self._liquidity[symbol] = (datetime.now().timestamp(), out)
        return out

    def _expires(self, horizon: str, newest: datetime) -> datetime:
        if horizon == "intraday":
            return newest.replace(hour=16, minute=0, second=0, microsecond=0) if newest.hour < 16 else newest + timedelta(hours=1)
        d = add_trading_days(newest.date(), SWING_DAYS_ALIVE)
        return datetime(d.year, d.month, d.day, 16, 0, tzinfo=NY)

    def _components(self, obs: list[dict[str, Any]], horizon: str, liq: dict[str, Any], conflict: bool,
                    earnings_in: int | None) -> tuple[list[dict[str, Any]], float, dict[str, Any]]:
        edges = [o["edge"] for o in obs if o["edge"]["known"]]
        best_edge = max(edges, key=lambda e: (e["validated"], e["cases"], e["expected_pct"])) if edges else _edge(None)
        if best_edge["known"]:
            conf = best_edge["cases"] / (best_edge["cases"] + 50.0)
            support = _clip(0.5 + best_edge["expected_pct"] / 2.0) * conf if best_edge["expected_pct"] > 0 else 0.0
        else:
            support = 0.0
        quality = _clip(max(float(o.get("raw_score") or 0.0) for o in obs) / 100.0)
        cat = 1.0 if any(o.get("catalyst") for o in obs) else 0.0
        horizon_days = 1 if horizon == "intraday" else 5
        if not cat and earnings_in is not None and 0 <= earnings_in <= horizon_days:
            cat = 0.6
        dv = liq.get("dollar_volume")
        liquidity = _clip((math.log10(dv) - 6.0) / 3.0) if dv and dv > 0 else 0.3
        newest = max(datetime.fromisoformat(o["observed_at"]) for o in obs)
        age_h = max(0.0, (_now() - newest).total_seconds() / 3600.0)
        life_h = 6.5 if horizon == "intraday" else 24.0 * SWING_DAYS_ALIVE
        fresh = _clip(1.0 - age_h / life_h)
        spread = liq.get("spread_bps")
        cost = _clip(spread / 50.0) if spread is not None else 0.2
        vals = {"historical_support": support, "setup_quality": quality, "catalyst": cat, "liquidity": liquidity, "freshness": fresh,
                "execution_cost": cost, "conflict": 1.0 if conflict else 0.0}
        texts = {
            "historical_support": (f"{best_edge['expected_pct']:+.2f}% after costs over {best_edge['cases']} past cases"
                                   + ("" if best_edge["validated"] else " (not enough to validate)")) if best_edge["known"] else "no past record of this trade",
            "setup_quality": f"best scanner score {100 * quality:.0f} of 100",
            "catalyst": "news behind it now" if cat == 1.0 else "earnings inside the horizon" if cat else "no catalyst",
            "liquidity": f"${dv / 1e6:,.0f}M a day traded" if dv else "trading volume unknown",
            "freshness": f"newest sighting {age_h:.1f} hours ago",
            "execution_cost": f"spread {spread:.1f} basis points" if spread is not None else "spread unknown",
            "conflict": "the opposite trade is also flagged" if conflict else "none"}
        comps = [{"key": k, "label": LABELS[k], "value": round(v, 4), "weight": WEIGHTS[k], "text": texts[k]} for k, v in vals.items()]
        score = 100.0 * _clip(sum(WEIGHTS[k] * v for k, v in vals.items()) / sum(w for w in WEIGHTS.values() if w > 0))
        return comps, round(score, 2), best_edge

    def ingest(self, observations: list[dict[str, Any]]) -> dict[str, Any]:
        """Record observations and rebuild the candidates they touch."""
        now = _now()
        stamp = now.isoformat(timespec="seconds")
        touched: set[tuple[str, str, str]] = set()
        with self._lock, self.svc.db.tx() as cur:
            for o in observations:
                o = {**o, "symbol": str(o["symbol"]).upper(), "observed_at": stamp}
                C.Observation(**{k: o.get(k) for k in ("source", "source_key", "symbol", "direction", "horizon", "kind", "observed_at",
                                                       "raw_score", "catalyst")}, payload={})      # shape check
                existing = cur.execute("SELECT id, payload_json FROM opportunity_observations WHERE mode = ? AND source = ? AND source_key = ?",
                                       (self.mode, o["source"], o["source_key"])).fetchone()
                payload = json.dumps({**o["payload"], "entry": o.get("entry"), "invalidation": o.get("invalidation"), "target": o.get("target"),
                                      "catalyst": o.get("catalyst"), "edge": o["edge"]}, default=str)
                if existing:
                    # Seen again: fresh time and score. If the candidate it belonged to has expired, it starts a new one.
                    cur.execute("""UPDATE opportunity_observations SET observed_at = ?, raw_score = ?, payload_json = ?,
                                   candidate_id = (SELECT id FROM opportunity_candidates WHERE id = opportunity_observations.candidate_id
                                                   AND status = 'active') WHERE id = ?""",
                                (stamp, o.get("raw_score"), payload, existing["id"]))
                else:
                    cur.execute("""INSERT INTO opportunity_observations (mode, source, source_key, symbol, direction, horizon, kind, observed_at,
                                   raw_score, payload_json) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                                (self.mode, o["source"], o["source_key"], o["symbol"], o["direction"], o["horizon"], o["kind"], stamp,
                                 o.get("raw_score"), payload))
                touched.add((o["symbol"], o["direction"], o["horizon"]))
        self.expire()
        changed = [self._rebuild(*k) for k in sorted(touched)]
        for sym in {k[0] for k in touched}:                       # a new direction can create or clear a conflict
            for d in ("LONG", "SHORT"):
                for h in ("intraday", "swing", "event"):
                    if (sym, d, h) not in touched:
                        self._rebuild(sym, d, h, only_existing=True)
        if changed:
            self.svc.bus.publish("opportunity.ranked", None, count=len(changed))
        return {"observations": len(observations), "candidates": len([c for c in changed if c])}

    def _rebuild(self, symbol: str, direction: str, horizon: str, only_existing: bool = False) -> str | None:
        db = self.svc.db
        live = db.one("SELECT * FROM opportunity_candidates WHERE mode = ? AND symbol = ? AND direction = ? AND horizon = ? AND status = 'active'",
                      (self.mode, symbol, direction, horizon))
        if only_existing and live is None:
            return None
        # Its own observations and any not yet placed; never those of a candidate that has expired.
        live_id = live["id"] if live else None
        rows = db.query("""SELECT * FROM opportunity_observations WHERE mode = ? AND symbol = ? AND direction = ? AND horizon = ?
                           AND (candidate_id IS NULL OR candidate_id = ?) ORDER BY observed_at DESC LIMIT 40""",
                        (self.mode, symbol, direction, horizon, live_id))
        if not rows:
            return None
        obs = []
        for r in rows:
            p = json.loads(r["payload_json"])
            obs.append({"source": r["source"], "source_key": r["source_key"], "kind": r["kind"], "raw_score": r["raw_score"],
                        "observed_at": r["observed_at"], "catalyst": p.get("catalyst"), "edge": p.get("edge") or _edge(None),
                        "entry": p.get("entry"), "invalidation": p.get("invalidation"), "target": p.get("target"), "payload": p, "id": r["id"]})
        opposite = db.one("SELECT 1 FROM opportunity_candidates WHERE mode = ? AND symbol = ? AND direction = ? AND horizon = ? AND status = 'active'",
                          (self.mode, symbol, "SHORT" if direction == "LONG" else "LONG", horizon))
        liq = self._liquidity_of(symbol)
        earnings_in = None
        try:
            from ..factors import earnings_days
            e = earnings_days(self.svc, symbol)
            earnings_in = e["trading_days"] if e else None
        except Exception:  # noqa: BLE001 - no earnings date is not an error
            earnings_in = None
        comps, score, edge = self._components(obs, horizon, liq, opposite is not None, earnings_in)
        newest = max(datetime.fromisoformat(o["observed_at"]) for o in obs)
        levels = next((o for o in obs if o.get("entry") and o.get("invalidation") and o.get("target")), None) or obs[0]
        kinds = list(dict.fromkeys(o["kind"] for o in obs))
        sources = list(dict.fromkeys(o["source"] for o in obs))
        reasons = []
        if not edge["validated"]:
            reasons.append("no validated historical edge (30 or more past cases, positive after costs)")
        if opposite is not None:
            reasons.append("the opposite trade is also flagged")
        dv = liq.get("dollar_volume")
        if not dv or dv < MIN_DOLLAR_VOLUME:
            reasons.append("too thin to trade automatically")
        if self.mode == "demo":
            reasons.append("demo prices are made up")
        eligible = ["shares" if direction == "LONG" else "short", "long_call" if direction == "LONG" else "long_put"]
        data_status = "reliable" if self.mode in ("live", "robinhood") else "partial" if self.mode == "sandbox" else "unknown"
        catalyst = next((o["catalyst"] for o in obs if o.get("catalyst")), None)
        values = dict(kinds_json=json.dumps(kinds), sources_json=json.dumps(sources), updated_at=_now().isoformat(timespec="seconds"),
                      expires_at=self._expires(horizon, newest).isoformat(timespec="seconds"), rank_score=score,
                      components_json=json.dumps(comps), edge_json=json.dumps(edge), catalyst=catalyst,
                      next_event_json=json.dumps({"kind": "earnings", "in_trading_days": earnings_in}) if earnings_in is not None and earnings_in >= 0 else None,
                      entry=levels.get("entry"), invalidation=levels.get("invalidation"), target=levels.get("target"),
                      liquidity_json=json.dumps(liq), data_status=data_status, eligible_json=json.dumps(eligible),
                      conflicts_json=json.dumps(["opposite direction flagged"] if opposite is not None else []),
                      autopilot_eligible=int(not reasons), autopilot_json=json.dumps(reasons))
        with self.svc.db.tx() as cur:
            if live:
                sets = ", ".join(f"{k} = ?" for k in values)
                cur.execute(f"UPDATE opportunity_candidates SET {sets}, version = version + 1 WHERE id = ?", (*values.values(), live["id"]))
                cid = live["id"]
            else:
                cid = C.new_id("opp")
                cols = ", ".join(values)
                cur.execute(f"""INSERT INTO opportunity_candidates (id, mode, symbol, direction, horizon, status, first_seen_at, {cols})
                               VALUES (?, ?, ?, ?, ?, 'active', ?, {', '.join('?' for _ in values)})""",
                            (cid, self.mode, symbol, direction, horizon, values["updated_at"], *values.values()))
            cur.execute("UPDATE opportunity_observations SET candidate_id = ? WHERE id IN (%s)" % ",".join("?" * len(obs)),
                        (cid, *[o["id"] for o in obs]))
        return cid

    def expire(self) -> int:
        now = _now().isoformat(timespec="seconds")
        with self.svc.db.tx() as cur:
            cur.execute("UPDATE opportunity_candidates SET status = 'expired' WHERE mode = ? AND status = 'active' AND expires_at < ?",
                        (self.mode, now))
            return cur.rowcount

    def refresh(self, req: RefreshIn) -> dict[str, Any]:
        obs, notes = self.observe_hub(req.sources)
        if "reversal" in req.sources:
            o2, n2 = self.observe_reversals()
            obs += o2
            notes += n2
        if "scanner" in req.sources or req.scan_symbols:
            o3, n3 = self.observe_scanner(req.scan_symbols or list(self.svc.settings.watchlist))
            obs += o3
            notes += n3
        got = self.ingest(obs)
        self._caught_up = datetime.now().timestamp()
        return {**got, "notes": notes, "feed": self.feed()}

    # --- reading -------------------------------------------------------------------------
    @staticmethod
    def _row(r: Any) -> dict[str, Any]:
        out = {"id": r["id"], "symbol": r["symbol"], "direction": r["direction"], "horizon": r["horizon"], "status": r["status"],
               "kinds": json.loads(r["kinds_json"]), "sources": json.loads(r["sources_json"]), "first_seen_at": r["first_seen_at"],
               "updated_at": r["updated_at"], "expires_at": r["expires_at"], "rank_score": r["rank_score"],
               "components": json.loads(r["components_json"]), "edge": json.loads(r["edge_json"]), "catalyst": r["catalyst"],
               "next_event": json.loads(r["next_event_json"]) if r["next_event_json"] else None, "entry": r["entry"],
               "invalidation": r["invalidation"], "target": r["target"], "liquidity": json.loads(r["liquidity_json"]),
               "data_status": r["data_status"], "eligible_strategies": json.loads(r["eligible_json"]),
               "conflicts": json.loads(r["conflicts_json"]), "autopilot_eligible": bool(r["autopilot_eligible"]),
               "autopilot_reasons": json.loads(r["autopilot_json"]), "study_id": r["study_id"], "version": r["version"]}
        return out

    def feed(self, *, horizon: str | None = None, direction: str | None = None, kind: str | None = None, min_score: float = 0.0,
             eligible_only: bool = False, limit: int = 100) -> list[dict[str, Any]]:
        self.expire()
        rows = self.svc.db.query("SELECT * FROM opportunity_candidates WHERE mode = ? AND status = 'active' ORDER BY rank_score DESC, symbol",
                                 (self.mode,))
        out = []
        for r in rows:
            c = self._row(r)
            if (horizon and c["horizon"] != horizon) or (direction and c["direction"] != direction) or (kind and kind not in c["kinds"]):
                continue
            if c["rank_score"] < min_score or (eligible_only and not c["autopilot_eligible"]):
                continue
            out.append(c)
            if len(out) >= limit:
                break
        for i, c in enumerate(out, 1):
            c["rank"] = i
        return out

    def candidate(self, cid: str) -> dict[str, Any]:
        r = self.svc.db.one("SELECT * FROM opportunity_candidates WHERE id = ?", (cid,))
        if r is None:
            raise RequestError(f"No opportunity {cid}")
        c = self._row(r)
        c["observations"] = [{"source": o["source"], "source_key": o["source_key"], "kind": o["kind"], "observed_at": o["observed_at"],
                              "raw_score": o["raw_score"], **{k: v for k, v in json.loads(o["payload_json"]).items() if k in
                                                              ("setup_label", "why", "catalyst", "edge", "signal", "stage", "headline", "entry",
                                                               "invalidation", "target", "source_score_scale")}}
                             for o in self.svc.db.query("SELECT * FROM opportunity_observations WHERE candidate_id = ? ORDER BY observed_at DESC", (cid,))]
        C.Opportunity(**{k: c[k] for k in ("id", "symbol", "direction", "horizon", "kinds", "status", "first_seen_at", "updated_at",
                                            "expires_at", "rank_score", "sources", "catalyst", "next_event", "entry", "invalidation", "target",
                                            "liquidity", "data_status", "eligible_strategies", "conflicts", "autopilot_eligible",
                                            "autopilot_reasons", "study_id", "version")},
                      components=[C.RankComponent(**x) for x in c["components"]], edge=C.Edge(**c["edge"]))     # the contract holds
        return c

    def link_study(self, cid: str, study_id: str, symbol: str) -> bool:
        """Record the study made of an opportunity (the latest one), if the opportunity is of that stock."""
        with self.svc.db.tx() as cur:
            cur.execute("UPDATE opportunity_candidates SET study_id = ? WHERE id = ? AND symbol = ?", (study_id, cid, symbol.upper()))
            return cur.rowcount > 0

    def catch_up(self, max_age_s: float = 120.0) -> None:
        """Take in what the Morning plan, Trade ideas and Live news have built since the last look. They keep their
        lists in memory, so this reads and does not rescan; the reversal scan and the scanner run on a refresh (or,
        from phase 5, on the daemon's schedule)."""
        now = datetime.now().timestamp()
        if now - self._caught_up < max_age_s:
            return
        self._caught_up = now
        try:
            obs, _ = self.observe_hub(["morning", "ideas", "live"])
            if obs:
                self.ingest(obs)
        except Exception:  # noqa: BLE001 - reading the feed must work even if a scanner's list is broken
            log.exception("Discover could not take in the scanners' lists")

    # --- capital ---------------------------------------------------------------------------
    def allocate(self, req: AllocateIn) -> dict[str, Any]:
        from ..allocator import allocate_candidates
        cands = [self.candidate(i) for i in req.candidate_ids] if req.candidate_ids else self.feed(limit=req.top_n, horizon=_horizon(req.horizon) if req.horizon else None)
        if not cands:
            raise RequestError("No opportunities to allocate across: refresh Discover first.")
        return allocate_candidates(self.svc, cands, budget=req.budget, goal=req.goal, tail_pct=req.tail_pct, max_share=req.max_share,
                                   max_positions=req.max_positions, sector_cap=req.sector_cap, instruments=req.instruments)


def install(svc: Any) -> None:
    from ..api_v3 import register
    svc.discover3 = Discover(svc)

    def routes(router: APIRouter, svc: Any) -> None:
        d = svc.discover3

        @router.get("/discover")
        def feed(horizon: str | None = None, direction: str | None = None, kind: str | None = None, min_score: float = 0.0,
                 eligible_only: bool = False, limit: int = 100) -> dict[str, Any]:
            d.catch_up()
            return {"candidates": d.feed(horizon=horizon, direction=direction, kind=kind, min_score=min_score, eligible_only=eligible_only,
                                         limit=max(1, min(limit, 300))), "weights": WEIGHTS, "labels": LABELS, "data_mode": d.mode}

        @router.post("/discover/refresh")
        def refresh(body: RefreshIn) -> dict[str, Any]:
            unknown = set(body.sources) - {"morning", "ideas", "live", "reversal", "scanner"}
            if unknown:
                raise RequestError(f"Unknown sources: {', '.join(sorted(unknown))}")
            return d.refresh(body)

        @router.get("/discover/{cid}")
        def one(cid: str) -> dict[str, Any]:
            return d.candidate(cid)

        @router.post("/discover/allocate")
        def allocate(body: AllocateIn) -> dict[str, Any]:
            return d.allocate(body)

    register(svc, routes)


__all__ = ["Discover", "install", "WEIGHTS", "RefreshIn", "AllocateIn"]
