"""What-if analysis on the interactive chart (2.13).

Three things, in one widget under the chart:

1. The model. The factors in `factors.py` (overseas markets, the 10-year yield, oil, the dollar, the VIX, the
   sector fund, foreign peers), the news and analyst calls, earnings ahead, and the similar-pattern path are
   combined into an expected path over a horizon with a 10-90 band, drawn on the chart. Every input is a
   number the user can change; the line moves with it. The arithmetic is in `factors.model_path`.
2. The AI's scenario. All of the above (readings, betas, headlines, the pattern's record, the volatility
   figures, the model's own path) goes to ChatGPT or Claude, whichever the user picks, which answers as JSON
   with its own path and band, the variable settings it would use, whether to lean on the pattern, and a
   strategy. Its line is drawn in another colour, and its settings can be applied to the model with one click.
3. The position. For a chosen quantity, shares, a short, or any option strategy from the strategy builder is
   valued at the horizon under the model's (or the AI's) distribution of prices: the profit or loss at the
   10th, 50th and 90th percentiles, the chance of a profit, the most it can lose, and a rank across all of
   them. The AI's pick is marked; the user can switch.

None of it is a forecast or advice, and nothing here places an order.
"""

from __future__ import annotations

import copy
import json
import logging
import math
import re
import time
from datetime import datetime
from typing import Any

import numpy as np

from . import similar as similar_engine
from .ai import AIError, BASE_SYSTEM, extract_json
from .chartview import RequestError
from .factors import NON_PRICE_FACTORS, Factors, earnings_days, factor_terms, model_path
from .optionmath import NY
from .options.evaluator import horizon_date
from .options.strategies import TEMPLATES, Strategies, StrategyError
from .providers.base import DataError

log = logging.getLogger(__name__)

# Trading days per bar, by bar size.
BAR_DAYS = {"1m": 1 / 390, "5m": 1 / 78, "15m": 1 / 26, "1h": 1 / 6.5, "1d": 1.0, "1w": 5.0, "1M": 21.0, "1Q": 63.0}
Z10 = 1.2816
PROVIDERS = {"chatgpt": "ChatGPT", "claude": "Claude"}
UPGRADE_WORDS = ("upgrade", "upgrades", "upgraded", "raises price target", "raises target", "lifts price target",
                 "initiates with buy", "initiated with buy", "outperform", "overweight", "to buy")
DOWNGRADE_WORDS = ("downgrade", "downgrades", "downgraded", "cuts price target", "lowers price target", "cuts target",
                   "lowers target", "underperform", "underweight", "to sell", "to hold", "to neutral")
STRATEGY_KINDS = ["shares", "short"] + list(TEMPLATES)
SETUP_TTL = 120.0
MAX_SHARES = 100_000
MAX_CONTRACTS = 100            # the strategy builder's limit
PEER_TICKER = re.compile(r"^[A-Z]{1,5}(\.[A-Z])?$")

AI_SYSTEM = BASE_SYSTEM + (
    " You are asked for a scenario, not a prediction: the user wants a reasoned path to test their own view against. "
    "Answer with one JSON object and nothing else."
)


def _num(v: Any, default: float | None = None) -> float | None:
    try:
        f = float(v)
    except (TypeError, ValueError):
        return default
    return f if math.isfinite(f) else default


def money_text(v: float) -> str:
    return f"${v:,.0f}"


def horizon_label(bars: int, group: str) -> str:
    for h, label in similar_engine.HORIZONS.get(group, ()):
        if h == bars:
            return label
    days = bars * BAR_DAYS[group]
    if days < 1:
        return f"{bars} bars"
    return f"{days:.0f} trading day{'s' if days != 1 else ''}"


def lognormal_band(path_pct: list[float], sd_pct: list[float]) -> tuple[list[float], list[float]]:
    lo, hi = [], []
    for p, s in zip(path_pct, sd_pct):
        centre = math.log(max(1e-6, 1 + p / 100.0))
        lo.append(100.0 * (math.exp(centre - Z10 * s / 100.0) - 1))
        hi.append(100.0 * (math.exp(centre + Z10 * s / 100.0) - 1))
    return lo, hi


def scenario_distribution(last: float, expected_pct: float, sd_pct: float, points: int = 241) -> tuple[np.ndarray, np.ndarray]:
    """Prices at the horizon and their probability weights: lognormal around the expected move with the
    band's standard deviation."""
    mu = math.log(max(1e-6, 1 + expected_pct / 100.0))
    s = max(1e-4, sd_pct / 100.0)
    z = np.linspace(-4, 4, points)
    w = np.exp(-0.5 * z * z)
    w /= w.sum()
    return last * np.exp(mu + s * z), w


def analyst_calls(items: list[dict[str, Any]]) -> dict[str, Any]:
    """Upgrades and downgrades read off the headlines (counted once per distinct headline)."""
    ups, downs = [], []
    seen: set[str] = set()
    for it in items:
        head = " ".join(str(it.get("headline") or "").lower().split())
        if not head or head in seen:
            continue
        seen.add(head)
        text = f"{head} {it.get('summary') or ''}".lower()
        if any(w in text for w in UPGRADE_WORDS) and not any(w in text for w in ("downgrade", "cuts price target")):
            ups.append(it)
        elif any(w in text for w in DOWNGRADE_WORDS):
            downs.append(it)
    return {"upgrades": len(ups), "downgrades": len(downs), "net": len(ups) - len(downs),
            "items": [{"headline": it.get("headline"), "source": it.get("source"), "published": it.get("published"),
                       "url": it.get("url") if str(it.get("url") or "").startswith(("http://", "https://")) else None,
                       "call": "upgrade"} for it in ups[:4]]
            + [{"headline": it.get("headline"), "source": it.get("source"), "published": it.get("published"),
                "url": it.get("url") if str(it.get("url") or "").startswith(("http://", "https://")) else None,
                "call": "downgrade"} for it in downs[:4]]}


class WhatIf:
    def __init__(self, svc: Any, chart_view: Any, factors: Factors | None = None, chatgpt: Any = None):
        self.svc = svc
        self.chart = chart_view
        self.factors = factors or Factors(svc)
        self.chatgpt = chatgpt
        self._setup_cache: dict[tuple, tuple[float, list[dict[str, Any]]]] = {}
        self._full_cache: dict[tuple, tuple[float, dict[str, Any]]] = {}
        self._returns: dict[tuple, dict[str, Any] | None] = {}      # the aligned daily returns the joint fit uses

    # --- providers -----------------------------------------------------------------------------------------------
    def providers(self) -> list[dict[str, Any]]:
        out = []
        gpt = self.chatgpt.status() if self.chatgpt is not None else {"configured": False, "reason": "not wired", "model": None}
        out.append({"key": "chatgpt", "label": "ChatGPT", "configured": bool(gpt.get("configured")),
                    "reason": gpt.get("reason"), "model": gpt.get("model")})
        ai = getattr(self.svc, "ai", None)
        st = ai.status() if ai is not None else {"configured": False, "reason": "not wired", "model": None}
        out.append({"key": "claude", "label": "Claude", "configured": bool(st.get("configured")),
                    "reason": st.get("reason"), "model": st.get("model")})
        return out

    # --- the inputs ---------------------------------------------------------------------------------------------
    def _factor_rows(self, symbol: str, peers: list[str], refresh: bool = False) -> list[dict[str, Any]]:
        key = (symbol, tuple(peers), self.svc.market.info.mode)
        hit = self._setup_cache.get(key)
        if hit and not refresh and time.time() - hit[0] < SETUP_TTL:
            return [dict(r) for r in hit[1]]
        rows = self.factors.readings(symbol, peers)
        self._returns[key] = self.factors.betas(symbol, rows)
        self._setup_cache[key] = (time.time(), [dict(r) for r in rows])
        return rows

    def _returns_for(self, symbol: str, peers: list[str]) -> dict[str, Any] | None:
        key = (symbol, tuple(peers), self.svc.market.info.mode)
        if key not in self._returns:
            self._factor_rows(symbol, peers)
        return self._returns.get(key)

    def _vol(self, symbol: str) -> dict[str, Any]:
        out: dict[str, Any] = {"daily_vol": None, "atr_pct": None, "iv30": None, "event_move_pct": None, "last": None}
        try:
            bars = self.svc.market.bars(symbol, 1.3, complete=True)
            out["daily_vol"] = Factors.daily_vol(bars)
            c = bars["close"].to_numpy(dtype=float)
            h, lo = bars["high"].to_numpy(dtype=float), bars["low"].to_numpy(dtype=float)
            if len(c) >= 15:
                tr = np.maximum(h[1:] - lo[1:], np.maximum(abs(h[1:] - c[:-1]), abs(lo[1:] - c[:-1])))
                out["atr_pct"] = float(100.0 * tr[-14:].mean() / c[-1])
        except (DataError, KeyError, IndexError) as exc:
            log.info("What-if: no daily bars for %s: %s", symbol, exc)
        lab = getattr(self.svc, "lab", None)
        if lab is not None:
            try:
                v = lab.volatility(symbol, max_expirations=8)
                out["iv30"] = v.get("iv30")
                ev = v.get("event") or {}
                out["event_move_pct"] = ev.get("move_pct") if ev.get("available") else None
            except Exception as exc:                        # noqa: BLE001 - no chain is not an error here
                log.info("What-if: no volatility profile for %s: %s", symbol, exc)
        return out

    def setup(self, symbol: str, *, range_key: str = "6m", group: str | None = None, start: str | None = None,
              end: str | None = None, window_start: int | None = None, window_end: int | None = None,
              horizon: int | None = None, min_corr: float = similar_engine.MIN_CORRELATION,
              peers: list[str] | None = None, refresh: bool = False) -> dict[str, Any]:
        """Everything the widget starts from: the factors with today's readings and betas, the news, the
        analyst calls, earnings, the pattern's record, the volatility figures, and the model at the default
        settings (every factor at its reading, the pattern weighted by how many cases it has)."""
        symbol = self.svc._symbol(symbol)
        peers = self._peers(symbol, peers)
        key = (symbol, range_key, group, start, end, window_start, window_end, horizon, min_corr, tuple(peers),
               self.svc.market.info.mode)
        hit = self._full_cache.get(key)
        if hit and not refresh and time.time() - hit[0] < SETUP_TTL:
            return copy.deepcopy(hit[1])
        if len(self._full_cache) > 40:
            self._full_cache.clear()
        out = self._setup(symbol, range_key, group, start, end, window_start, window_end, horizon, min_corr, peers, refresh)
        self._full_cache[key] = (time.time(), copy.deepcopy(out))
        return out

    def _setup(self, symbol: str, range_key: str, group: str | None, start: str | None, end: str | None,
               window_start: int | None, window_end: int | None, horizon: int | None, min_corr: float,
               peers: list[str], refresh: bool) -> dict[str, Any]:
        outlook = self.chart.outlook(symbol, range_key, group, start, end, window_start, window_end, horizon, min_corr)
        g = outlook["group"]
        horizons = [h["bars"] for h in outlook["horizons"]] or [h for h, _ in similar_engine.HORIZONS.get(g, similar_engine.HORIZONS["1d"])]
        chosen = int(horizon) if horizon else (outlook["horizon"]["bars"] if outlook.get("horizon") else horizons[min(2, len(horizons) - 1)])
        max_h = max(h for h, _ in similar_engine.HORIZONS.get(g, similar_engine.HORIZONS["1d"]))
        if not 1 <= chosen <= max_h:
            raise RequestError(f"horizon must be between 1 and {max_h} bars for {g} bars")
        rows = self._factor_rows(symbol, peers, refresh)
        news = next((r for r in outlook["readings"] if r["key"] == "news"), {})
        items = list(news.get("items") or [])
        calls = analyst_calls(self._week_headlines(symbol, items))
        earnings = earnings_days(self.svc, symbol)
        vol = self._vol(symbol)
        found = outlook["similar"]
        last = float(found["window"].get("last_close") or 0) or self._last_close(symbol, outlook)
        pattern = self._pattern(found, outlook, chosen)
        if window_start is None or window_end is None:
            # Without a window chosen on the chart, the search ran on the whole range; that shape is not what the
            # chart's pattern panel shows, so the path is not used until Find similar patterns is pressed.
            pattern.update(available=False, default_weight=0.0, matches=0, cases=0, path_pct=[], up_frequency=None,
                           median_pct=None, p25_pct=None, p75_pct=None, min_correlation=None, lowered_from=None, enough=False,
                           note="Press Find similar patterns on the chart (or select a window) to add the pattern's path.")
        sim_times = (found.get("future_times") or [])[:chosen]
        for r in rows:
            r["on"] = r.get("beta") is not None and r.get("reading") is not None
        news_tone = news.get("tone")
        # Before the open, today's readings are what the stock has yet to price, so they are the starting
        # scenario; once it is trading (or after the close) they are already in the price, and the scenario
        # starts at "no further move" with the readings shown beside it.
        premarket = self._premarket()
        scenario = {r["key"]: (r["reading"] if (premarket and r["reading"] is not None) else 0.0) for r in rows}
        scenario.update({"news": round(float(news_tone), 2) if news_tone is not None else 0.0,
                         "analysts": calls["net"], "pattern_weight": pattern["default_weight"]})
        include = {r["key"]: bool(r["on"]) for r in rows}
        include.update({"news": bool(items), "analysts": calls["net"] != 0, "pattern": pattern["available"]})
        model = self._model(rows, scenario, include, chosen, g, pattern, vol, news, earnings, last, sim_times,
                            self._returns_for(symbol, peers))
        return {"symbol": symbol, "group": g, "price": last, "horizon": chosen, "horizon_label": horizon_label(chosen, g),
                "horizons": [{"bars": h, "label": horizon_label(h, g)} for h in horizons],
                "factors": rows, "non_price_factors": NON_PRICE_FACTORS,
                "news": {"items": items[:8], "shown": min(len(items), 8), "tone": news_tone, "move": news.get("move"),
                         "text": news.get("text"),
                         "record": {k: news.get(k) for k in ("frequency", "low", "high", "cases", "direction", "why")}},
                "analysts": calls, "earnings": earnings, "volatility": vol, "pattern": pattern,
                "scenario": scenario, "include": include, "model": model, "future_times": sim_times, "premarket": premarket,
                "providers": self.providers(), "strategies": self.strategy_choices(),
                "stooq": {"enabled": self.factors.stooq.enabled, "last_error": self.factors.stooq.last_error},
                "window": found["window"], "data_source": self.svc.market.info.mode,
                "note": ("The model is arithmetic on the assumptions shown: each factor's own part of its move (what the factors "
                         "above it do not already explain) times how much this stock "
                         "has moved with it, plus the pattern's path and the news record. Change any number and the line "
                         "moves. It is not a forecast, and no order is placed.")}

    def _peers(self, symbol: str, peers: list[str] | None) -> list[str]:
        out = []
        for p in peers or []:
            try:
                sym = self.svc._symbol(p)
            except (RequestError, ValueError):
                continue                                       # not a ticker: left out rather than sent to the source
            if not PEER_TICKER.match(sym):
                continue
            if sym != symbol and sym not in out:
                out.append(sym)
        return out[:6]

    @staticmethod
    def _premarket() -> bool:
        now = datetime.now(NY)
        return now.weekday() < 5 and (now.hour, now.minute) < (9, 30)

    def _last_close(self, symbol: str, outlook: dict[str, Any]) -> float:
        price = outlook.get("price")
        if price:
            return float(price)
        try:
            q = self.svc.market.quote(symbol)
            return float(q.get("last") or q.get("close") or 0)
        except Exception:                                   # noqa: BLE001
            return 0.0

    def _week_headlines(self, symbol: str, recent: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """A week of headlines for the analyst-call count (the news reading keeps only 36 hours)."""
        news = getattr(self.svc, "news", None)
        if news is None:
            return recent
        try:
            got = news.company(symbol, days=7)
            seen = {it.get("id") for it in recent}
            return recent + [it for it in got.get("items") or [] if it.get("id") not in seen]
        except Exception:                                   # noqa: BLE001
            return recent

    @staticmethod
    def _pattern(found: dict[str, Any], outlook: dict[str, Any], horizon: int) -> dict[str, Any]:
        overlay = found.get("overlay") or {}
        base = overlay.get("from_close")
        median = overlay.get("median") or []
        path = [None if (v is None or not base) else 100.0 * (v / base - 1) for v in median]
        row = next((h for h in found.get("horizons") or [] if h["bars"] == horizon), None) or outlook.get("horizon")
        cases = int(row["cases"]) if row else 0
        available = bool(path) and any(v is not None for v in path)
        weight = 1.0 if cases >= similar_engine.MIN_CASES else 0.5 if cases >= 5 else 0.25 if cases else 0.0
        return {"available": available, "matches": len(found.get("matches") or []), "cases": cases,
                "path_pct": path, "up_frequency": row.get("up_frequency") if row else None,
                "median_pct": row.get("median_pct") if row else None, "p25_pct": row.get("p25_pct") if row else None,
                "p75_pct": row.get("p75_pct") if row else None, "enough": bool(row and row.get("enough")),
                "min_correlation": found.get("min_correlation"), "lowered_from": found.get("lowered_from"),
                "default_weight": weight if available else 0.0, "note": found.get("note")}

    # --- the model ----------------------------------------------------------------------------------------------
    def _model(self, rows: list[dict[str, Any]], scenario: dict[str, Any], include: dict[str, Any], horizon: int,
               group: str, pattern: dict[str, Any], vol: dict[str, Any], news: dict[str, Any], earnings: dict | None,
               last: float, times: list[int], returns: dict[str, Any] | None) -> dict[str, Any]:
        for r in rows:
            r["on"] = bool(include.get(r["key"], r.get("on"))) and r.get("beta") is not None
        # The factor terms: a joint fit of the factors that are on, in the list's order, so each counts only the
        # part of its move the factors above it do not already explain (2.14).
        shocks = factor_terms(rows, scenario, returns)
        by_key = {t["key"]: t for t in shocks}
        for r in rows:
            t = by_key.get(r["key"])
            r["in_model"] = t is not None
            r["beta_unique_now"] = t["beta_unique"] if t else None
            r["explained_now"] = t["explained"] if t else None
            r["redundant_now"] = t["redundant"] if t else None
        bar_days = BAR_DAYS[group]
        daily_vol = vol.get("daily_vol") or 0.02
        bar_vol = daily_vol * math.sqrt(bar_days)
        atr_bar = (vol.get("atr_pct") or 2.0) * math.sqrt(bar_days)
        # The news term: this stock's record of moves with news behind them, in the tone's direction.
        tone = _num(scenario.get("news"), 0.0) or 0.0
        news_term = 0.0
        if include.get("news") and tone:
            # The record counts only when it is the continuation rate of moves with news behind them; a record of
            # moves without news (how often they snapped back) says nothing about the news now assumed.
            rec_freq = news.get("frequency")
            with_news = news.get("why") == "a move with news behind it"
            edge = (rec_freq - 0.5) * 2 if with_news and rec_freq is not None and (news.get("cases") or 0) >= similar_engine.MIN_CASES else 0.3
            move = (news.get("move") or {}).get("pct")
            size = abs(move) if move else atr_bar
            news_term = float(np.clip(tone, -1, 1)) * max(0.0, edge) * size
        weight = float(np.clip(_num(scenario.get("pattern_weight"), pattern["default_weight"]) or 0.0, 0.0, 1.0))
        p_path = [v for v in pattern.get("path_pct") or []] if include.get("pattern", True) and pattern.get("available") else None
        if p_path:
            p_path = [float(v) if v is not None else float("nan") for v in p_path]
            p_path = list(np.nan_to_num(np.array(p_path), nan=np.nanmean(p_path) if np.isfinite(np.nanmean(p_path)) else 0.0))
        # Each net call: half an average range over the first bars, capped at two either way.
        analysts = float(np.clip(_num(scenario.get("analysts"), 0.0) or 0.0, -2, 2)) * 0.5 if include.get("analysts") else 0.0
        e_in = None
        if earnings and earnings.get("trading_days") is not None and bar_days > 0:
            e_in = int(math.ceil(earnings["trading_days"] / bar_days)) if earnings["trading_days"] >= 0 else None
        out = model_path(horizon=horizon, shocks=shocks, pattern=p_path, pattern_weight=weight,
                         daily_vol=bar_vol, atr_pct=atr_bar, news_term_pct=news_term, analyst_net=analysts or 0.0,
                         earnings_in=e_in, event_move_pct=vol.get("event_move_pct") if include.get("earnings", True) else None,
                         news_tone=tone)
        out["low_pct"], out["high_pct"] = lognormal_band(out["path_pct"], out["sd_pct"])
        out.update(self._in_prices(out, last, times))
        out["pattern_weight"] = weight
        out["news_term_pct"] = news_term
        return out

    @staticmethod
    def _in_prices(m: dict[str, Any], last: float, times: list[int]) -> dict[str, Any]:
        n = min(len(times), len(m["path_pct"]))
        return {"last": last, "times": times[:n],
                "path": [last * (1 + v / 100.0) for v in m["path_pct"][:n]],
                "low": [last * (1 + v / 100.0) for v in m["low_pct"][:n]],
                "high": [last * (1 + v / 100.0) for v in m["high_pct"][:n]],
                "expected_price": last * (1 + m["expected_pct"] / 100.0)}

    def model(self, symbol: str, *, scenario: dict[str, Any], include: dict[str, Any], horizon: int, peers: list[str] | None = None,
              **chart_args: Any) -> dict[str, Any]:
        """The model again with the user's settings. The chart arguments pick the pattern window."""
        base = self.setup(symbol, horizon=horizon, peers=peers, **chart_args)
        merged = {**base["scenario"], **{k: _num(v, base["scenario"].get(k)) for k, v in (scenario or {}).items()}}
        inc = {**base["include"], **{k: bool(v) for k, v in (include or {}).items()}}
        model = self._model(base["factors"], merged, inc, base["horizon"], base["group"], base["pattern"],
                            base["volatility"], {**base["news"], **base["news"]["record"]},
                            base["earnings"], base["price"], base["future_times"],
                            self._returns_for(base["symbol"], self._peers(base["symbol"], peers)))
        return {"symbol": symbol, "group": base["group"], "horizon": base["horizon"], "scenario": merged, "include": inc,
                "model": model, "price": base["price"], "future_times": base["future_times"]}

    # --- the AI's scenario --------------------------------------------------------------------------------------
    def _prompt(self, s: dict[str, Any], current: dict[str, Any]) -> str:
        g = s["group"]
        lines = [f"Stock: {s['symbol']}. Last price {s['price']:.2f}. Chart bars: {g}. Horizon: {s['horizon']} bars "
                 f"({s['horizon_label']})." + (" The data is demo data, made up." if s["data_source"] == "demo" else ""),
                 "", "FACTORS, in the order they are fitted (the US market first; each factor's term in the app's model is "
                 "only the part of its move the factors above it do not already explain, so nothing is counted twice). "
                 "beta alone = this stock's % move per 1% of the proxy over the last year; beta own = per 1% of the factor's "
                 "own part given the factors above it; explained = how much of its moves follow the factors above:"]
        for r in s["factors"]:
            rd = "no reading" if r.get("reading") is None else f"{r['reading']:+.2f} {r['unit']} ({r.get('reading_source')})"
            if r.get("beta") is None:
                b = "no beta"
            else:
                b = f"beta alone {r['beta']:+.2f}, corr {r.get('correlation', 0):+.2f} over {r.get('beta_days')} days"
                bu = r.get("beta_unique_now") if r.get("in_model") else None
                if bu is not None:
                    b += f"; beta own {bu:+.2f}, explained {r.get('explained_now', 0):.0%}" + (" (redundant: no own part)" if r.get("redundant_now") else "")
                elif not r.get("on"):
                    b += "; switched off"
                elif r.get("fit_note"):
                    b += f"; {r['fit_note']}"
            lines.append(f"- {r['key']}: {r['label']}: {rd}; {b}")
        lines += ["", f"NEWS (last 36 hours, tone -1..+1): average tone {s['news'].get('tone')}; {s['news'].get('text') or ''}"]
        for it in s["news"]["items"][:10]:
            lines.append(f"- [{it.get('published')}] {it.get('headline')} ({it.get('source')}; tone {it.get('tone')})")
        a = s["analysts"]
        lines += ["", f"ANALYST CALLS in the last week's headlines: {a['upgrades']} upgrades, {a['downgrades']} downgrades."]
        for it in a["items"]:
            lines.append(f"- {it['call']}: {it['headline']} ({it['source']})")
        e = s["earnings"]
        lines += ["", "EARNINGS: " + (f"next report {e['date']} ({e['days']} days away{', estimated' if e.get('estimated') else ''})" if e else "no date known")]
        v = s["volatility"]
        r2 = lambda x, n=2: "unknown" if x is None else f"{float(x):.{n}f}"      # noqa: E731
        lines += ["", f"VOLATILITY: daily realised {100 * (v.get('daily_vol') or 0):.2f}%; 14-day average range {r2(v.get('atr_pct'))}% "
                  f"of price; iv30 {r2(v.get('iv30'), 3)}; option-implied earnings move {r2(v.get('event_move_pct'))}%."]
        p = s["pattern"]
        lines += ["", f"SIMILAR PATTERNS (this stock's own history, windows shaped like the selected one): {p['matches']} matches at "
                  f"{r2(p.get('min_correlation'))} correlation or better; at this horizon {p['cases']} cases, up {r2(p.get('up_frequency'))}, "
                  f"median {r2(p.get('median_pct'))}%, quartiles {r2(p.get('p25_pct'))} / {r2(p.get('p75_pct'))}%"
                  + ("." if p.get("enough") else " (fewer than 10 cases: weak evidence).")]
        if p.get("path_pct"):
            lines.append("Median path after the matches, % from the last close, bar by bar: "
                         + ", ".join("?" if x is None else f"{x:+.2f}" for x in p["path_pct"][:s["horizon"]]))
        m = current
        lines += ["", f"THE APP'S MODEL at the current settings (the joint fit's terms: each factor's own part of its move x its beta "
                  f"own, plus pattern x weight {m.get('pattern_weight')}, plus news and analyst terms; fitted on "
                  f"{(m['terms'][0].get('fit_days') if m.get('terms') and m['terms'][0].get('fit_days') else 'n/a')} shared days): "
                  f"expected {m['expected_pct']:+.2f}% at the horizon; path "
                  + ", ".join(f"{x:+.2f}" for x in m["path_pct"]) + "; 10-90 band at the horizon "
                  f"{m['low_pct'][-1]:+.2f}% to {m['high_pct'][-1]:+.2f}%.",
                  "Current scenario values: " + json.dumps({k: (round(v, 3) if isinstance(v, float) else v) for k, v in s["scenario"].items()}),
                  "", "STRATEGIES available (key: description): " + "; ".join(f"{k}: {t['label']}" for k, t in self.strategy_choices().items()),
                  "", "Answer with one JSON object with exactly these keys:",
                  f'  "path_pct": {s["horizon"]} numbers, your expected % move from the last close at each of the next {s["horizon"]} bars;',
                  '  "low_pct" and "high_pct": the same length, a 10-90 range around it;',
                  '  "expected_pct": the last value of path_pct; "confidence": 0 to 1;',
                  '  "variables": an object keyed by the factor keys above (and "news" in tone -1..1, "analysts" as net calls, '
                  '"pattern_weight" 0..1) with the scenario values you would assume, in the units shown;',
                  '  "include": an object keyed the same way with true/false for which factors matter here;',
                  '  "pattern": {"use": true/false, "weight": 0..1, "why": "..."};',
                  '  "strategy": {"kind": one of the strategy keys above or "shares" or "short", "direction": "up"/"down"/"flat", '
                  '"combination": up to three strategy keys that work together (the kind first; omit if one is enough), '
                  '"why": "..."}; pick what fits your path and band best (defined risk when the band is wide);',
                  '  "rationale": 3 to 6 plain sentences on why, naming the inputs that matter most;',
                  '  "risks": a list of 2 to 5 short strings.',
                  "Do not invent data beyond what is above. The model's path is one input, not the answer."]
        return "\n".join(lines)

    def ask_ai(self, symbol: str, *, provider: str, scenario: dict[str, Any] | None = None, include: dict[str, Any] | None = None,
               horizon: int, peers: list[str] | None = None, **chart_args: Any) -> dict[str, Any]:
        provider = (provider or "chatgpt").lower()
        if provider not in PROVIDERS:
            raise RequestError("provider must be chatgpt or claude")
        current = self.model(symbol, scenario=scenario or {}, include=include or {}, horizon=horizon, peers=peers, **chart_args)
        s = self.setup(symbol, horizon=horizon, peers=peers, **chart_args)
        s["scenario"] = current["scenario"]
        prompt = self._prompt(s, current["model"])
        started = time.time()
        if provider == "chatgpt":
            if self.chatgpt is None or not self.chatgpt.configured:
                raise AIError("ChatGPT is not set up: " + ((self.chatgpt.status()["reason"] if self.chatgpt else None) or "OPENAI_API_KEY is not set"))
            reply = self.chatgpt.ask_json(purpose="what_if", prompt=prompt, system=AI_SYSTEM, max_tokens=2500)
            parsed, model_name, usage = reply["json"], reply["model"], reply["usage"]
        else:
            ai = getattr(self.svc, "ai", None)
            if ai is None or not ai.configured:
                raise AIError("Claude is not set up: " + ((ai.status()["reason"] if ai else None) or "ANTHROPIC_API_KEY is not set"))
            reply = ai.ask(purpose="what_if", prompt=prompt, system=AI_SYSTEM, max_tokens=2500, web_search=False)
            parsed, model_name, usage = extract_json(reply["text"]), reply["model"], reply["usage"]
            if not isinstance(parsed, dict):
                raise AIError("Claude's answer was not the JSON this app asked for. Try again.")
        return {"provider": provider, "label": PROVIDERS[provider], "model": model_name, "usage": usage,
                "seconds": round(time.time() - started, 1), "scenario": self._read_ai(parsed, s, current["model"]),
                "prompt_chars": len(prompt), "sent": {"factors": len(s["factors"]), "headlines": len(s["news"]["items"][:10]),
                                                       "analyst_calls": len(s["analysts"]["items"])},
                "note": f"{PROVIDERS[provider]}'s scenario is what a language model wrote from the inputs shown; it can be wrong. "
                        "Billed to your own API key; counts against AI_DAILY_REQUESTS."}

    def _read_ai(self, p: dict[str, Any], s: dict[str, Any], current: dict[str, Any]) -> dict[str, Any]:
        """The AI's answer, checked: the paths clipped to a sane size and padded to the horizon, the variables
        read as numbers, the strategy checked against the list."""
        h = s["horizon"]

        def path(key: str, fallback: list[float]) -> list[float]:
            raw = p.get(key)
            vals = [_num(v) for v in raw] if isinstance(raw, list) else []
            vals = [float(np.clip(v, -60, 100)) for v in vals if v is not None][:h]
            if not vals:
                return list(fallback)
            while len(vals) < h:
                vals.append(vals[-1])
            return vals
        path_pct = path("path_pct", current["path_pct"])
        low = path("low_pct", current["low_pct"])
        high = path("high_pct", current["high_pct"])
        low = [min(a, b) for a, b in zip(low, path_pct)]
        high = [max(a, b) for a, b in zip(high, path_pct)]
        sd = [max(0.05, (hi - lo) / (2 * Z10)) for lo, hi in zip(low, high)]
        keys = {r["key"] for r in s["factors"]} | {"news", "analysts", "pattern_weight"}
        variables = {k: _num(v) for k, v in (p.get("variables") or {}).items() if k in keys and _num(v) is not None} \
            if isinstance(p.get("variables"), dict) else {}
        include = {k: bool(v) for k, v in (p.get("include") or {}).items() if k in keys | {"pattern", "earnings"}} \
            if isinstance(p.get("include"), dict) else {}
        pat = p.get("pattern") if isinstance(p.get("pattern"), dict) else {}
        if pat:
            include["pattern"] = bool(pat.get("use", True))
            if _num(pat.get("weight")) is not None:
                variables["pattern_weight"] = float(np.clip(_num(pat.get("weight")), 0, 1))
        strat = p.get("strategy") if isinstance(p.get("strategy"), dict) else {}
        kind = str(strat.get("kind") or strat.get("template") or "").lower()
        if kind not in STRATEGY_KINDS:
            kind = "shares" if path_pct[-1] >= 0 else "long_put"
        combination = [str(k).lower() for k in strat.get("combination") or [] if isinstance(k, str)] \
            if isinstance(strat.get("combination"), list) else []
        combination = [k for k in dict.fromkeys([kind] + combination) if k in STRATEGY_KINDS][:3]
        risks = [str(x)[:200] for x in (p.get("risks") or []) if isinstance(x, (str, int, float))][:6] \
            if isinstance(p.get("risks"), list) else []
        out = {"path_pct": path_pct, "low_pct": low, "high_pct": high, "sd_pct": sd, "expected_pct": path_pct[-1],
               "confidence": float(np.clip(_num(p.get("confidence"), 0.5) or 0.5, 0, 1)),
               "rationale": str(p.get("rationale") or "")[:2000], "risks": risks, "variables": variables, "include": include,
               "pattern": {"use": include.get("pattern", True), "weight": variables.get("pattern_weight"), "why": str(pat.get("why") or "")[:400]},
               "strategy": {"kind": kind, "combination": combination, "direction": str(strat.get("direction") or "")[:10],
                            "why": str(strat.get("why") or "")[:600]}}
        out.update(self._in_prices(out, s["price"], s["future_times"]))
        return out

    # --- the position -------------------------------------------------------------------------------------------
    @staticmethod
    def strategy_choices() -> dict[str, dict[str, Any]]:
        out = {"shares": {"label": "Buy shares", "group": "bullish", "defined_risk": True},
               "short": {"label": "Short shares", "group": "bearish", "defined_risk": False}}
        for k, t in TEMPLATES.items():
            out[k] = {"label": t.label, "group": t.group, "defined_risk": t.defined_risk}
        return out

    def position(self, symbol: str, *, kind: str, quantity: int, expected_pct: float, sd_pct: float, horizon_bars: int,
                 group: str, last: float | None = None, dte: int | None = None) -> dict[str, Any]:
        """One position valued at the horizon under the scenario: P&L at the 10th, 50th and 90th percentile prices,
        the expected P&L, the chance of a profit, the most it can lose, and what it costs."""
        return self._public(self._position(symbol, kind=kind, quantity=quantity, expected_pct=expected_pct, sd_pct=sd_pct,
                                           horizon_bars=horizon_bars, group=group, last=last, dte=dte))

    def _position(self, symbol: str, *, kind: str, quantity: int, expected_pct: float, sd_pct: float, horizon_bars: int,
                  group: str, last: float | None = None, dte: int | None = None) -> dict[str, Any]:
        symbol = self.svc._symbol(symbol)
        kind = (kind or "").lower()
        if kind not in STRATEGY_KINDS:
            raise RequestError(f"kind must be one of {', '.join(STRATEGY_KINDS)}")
        quantity = int(quantity or 1)
        if not 1 <= quantity <= (MAX_SHARES if kind in ("shares", "short") else MAX_CONTRACTS):
            raise RequestError(f"quantity must be between 1 and {MAX_SHARES:,} shares, or 1 and {MAX_CONTRACTS} contracts")
        if group not in BAR_DAYS:
            raise RequestError("group is not a bar size")
        sd_pct = max(0.05, float(sd_pct))
        if last is None:
            # The live price: the chain's strikes and the simulator's axis are laid out around it, and the scenario's
            # move is applied from here, even when the chart's window ended earlier.
            q = self.svc.market.quote(symbol)
            last = float(q.get("last") or q.get("close") or 0)
        if not last or last <= 0:
            raise RequestError(f"No price for {symbol}")
        days = max(1, int(math.ceil(horizon_bars * BAR_DAYS[group])))
        today = datetime.now(NY).date()
        when = horizon_date(today, days)
        prices, weights = scenario_distribution(last, expected_pct, sd_pct)
        p10, p50, p90 = (last * math.exp(math.log(1 + expected_pct / 100.0) + sd_pct / 100.0 * z) for z in (-Z10, 0.0, Z10))
        out: dict[str, Any] = {"symbol": symbol, "kind": kind, "label": self.strategy_choices()[kind]["label"], "quantity": quantity,
                               "last": last, "horizon_days": days, "eval_date": when.isoformat(),
                               "scenario": {"expected_pct": expected_pct, "sd_pct": sd_pct, "p10": p10, "p50": p50, "p90": p90}}
        if kind in ("shares", "short"):
            sign = 1 if kind == "shares" else -1
            pnl = sign * quantity * (prices - last)
            out.update({"cost": quantity * last, "cost_label": "shares bought" if sign > 0 else "proceeds of the short (margin not modelled)",
                        "pnl_p10": sign * quantity * (p10 - last), "pnl_p50": sign * quantity * (p50 - last),
                        "pnl_p90": sign * quantity * (p90 - last),
                        "max_loss": quantity * last if sign > 0 else None, "unlimited_loss": sign < 0,
                        "breakevens": [last], "legs": [], "notes": [] if sign > 0 else
                        ["A short's loss has no limit: it grows with the price. Borrow fees and margin are not modelled."]})
        else:
            dte = dte or max(int(self.svc.settings.min_dte), int(days * 7 / 5) + 10)
            try:
                built = Strategies(self.svc).build({"symbol": symbol, "strategy": kind, "contracts": quantity, "dte": dte})
                legs = [{"expiration": l["expiration"], "option_type": l["option_type"], "strike": l["strike"], "side": l["side"],
                         "contracts": l["contracts"], "entry_price": l["mid"]} for l in built["legs"]]
                sim = self.svc.simulate(symbol=symbol, legs=legs, eval_date=min(when, datetime.strptime(built["expiration"], "%Y-%m-%d").date()).isoformat(),
                                        range_pct=min(90.0, max(10.0, 4 * sd_pct + abs(expected_pct) + 5)))
            except (StrategyError, DataError, ValueError, RequestError) as exc:
                raise RequestError(f"{self.strategy_choices()[kind]['label']}: {exc}") from exc
            cp, cv = np.array(sim["curve"]["prices"], dtype=float), np.array(sim["curve"]["pnl_at_date"], dtype=float)
            pnl = np.interp(prices, cp, cv)
            at = lambda px: float(np.interp(px, cp, cv))      # noqa: E731
            out.update({"cost": abs(built["net_mid"]), "cost_label": "debit paid" if not built["credit"] else "credit received",
                        "net_mid": built["net_mid"], "credit": built["credit"],
                        "pnl_p10": at(p10), "pnl_p50": at(p50), "pnl_p90": at(p90),
                        "max_loss": abs(built["max_loss"]) if built["max_loss"] is not None and not built["unlimited_loss"] else None,
                        "unlimited_loss": built["unlimited_loss"], "max_gain": built["max_gain"],
                        "unlimited_gain": built["unlimited_gain"], "breakevens": built["breakevens"], "expiration": built["expiration"],
                        "dte": built["dte"], "strikes": built["strikes"], "legs": built["legs"],
                        "legs_text": [f"{'buy' if l['side'] == 'buy' else 'sell'} {l['strike']:g} {l['option_type']} {l['expiration']}" for l in built["legs"]],
                        "risk_check": built.get("risk_check"), "notes": built.get("notes") or [],
                        "p_profit_at_expiry": built.get("p_profit_at_expiry")})
        out["expected_pnl"] = float((weights * pnl).sum())
        out["p_profit"] = float(weights[pnl > 0].sum())
        out["p_loss_over_half"] = float(weights[pnl < -0.5 * (out["max_loss"] or out["cost"] or 1)].sum()) if (out.get("max_loss") or out.get("cost")) else None
        if out.get("max_loss"):
            risk, out["risk_basis"] = float(out["max_loss"]), "the most it can lose"
        else:
            tail = -min(float(np.interp(p10, prices, pnl)), float(np.interp(p90, prices, pnl)))
            risk = max(1.0, tail)
            out["risk_basis"] = ("the loss at the worse of the 10th and 90th percentile prices (no fixed limit)" if tail >= 1.0
                                 else "no loss inside the 10-90 range; one dollar stands in (no fixed limit)")
        out["risk"] = risk
        out["score"] = out["expected_pnl"] / risk if risk > 0 else None
        out["pnl_curve"] = {"prices": [float(x) for x in prices[::8]], "pnl": [float(x) for x in pnl[::8]]}
        out["note"] = ("P&L at the horizon under the scenario's 10-90 range of prices (a lognormal around the expected move), "
                       "options valued by the simulator at the midpoint. Not advice; no order is placed.")
        out["_grid"] = (prices, weights, np.asarray(pnl, dtype=float))     # for combinations; stripped from replies
        return out

    @staticmethod
    def _public(row: dict[str, Any]) -> dict[str, Any]:
        return {k: v for k, v in row.items() if not k.startswith("_")}

    @staticmethod
    def _stats(prices: np.ndarray, weights: np.ndarray, pnl: np.ndarray, p10: float, p50: float, p90: float) -> dict[str, Any]:
        """The figures of one P&L curve under the scenario: the percentile P&Ls, the expected P&L, the chance of
        a profit, and the average loss in the worst tenth of outcomes (CVaR 10%, a positive number)."""
        order = np.argsort(pnl)
        cum = np.cumsum(weights[order])
        tail = order[cum <= 0.10 + 1e-12]
        if len(tail) == 0:
            tail = order[:1]
        tail_w = weights[tail]
        cvar = -float((pnl[tail] * tail_w).sum() / tail_w.sum())
        return {"pnl_p10": float(np.interp(p10, prices, pnl)), "pnl_p50": float(np.interp(p50, prices, pnl)),
                "pnl_p90": float(np.interp(p90, prices, pnl)), "expected_pnl": float((weights * pnl).sum()),
                "p_profit": float(weights[pnl > 0].sum()), "cvar10": max(0.0, cvar),
                "worst_in_range": float(-pnl.min()), "best_in_range": float(pnl.max())}

    def combo(self, symbol: str, *, legs: list[dict[str, Any]], expected_pct: float, sd_pct: float, horizon_bars: int,
              group: str) -> dict[str, Any]:
        """A combination of positions (each a kind and a quantity) valued together under the scenario: every
        position's own figures, and the combination's P&L at the percentiles, its expected result, its chance
        of a profit, its cost and the most it can lose."""
        symbol = self.svc._symbol(symbol)
        if not 1 <= len(legs) <= 6:
            raise RequestError("A combination has one to six positions")
        rows, failed, total = [], [], None
        prices = weights = None
        last = self._live_price(symbol)                        # one price for every leg, so the curves share a grid
        for leg in legs:
            try:
                r = self._position(symbol, kind=str(leg.get("kind") or ""), quantity=int(leg.get("quantity") or 1), expected_pct=expected_pct,
                                  sd_pct=sd_pct, horizon_bars=horizon_bars, group=group, last=last)
            except (RequestError, DataError, ValueError) as exc:
                failed.append({"kind": leg.get("kind"), "error": str(exc)[:200]})
                continue
            prices, weights, pnl = r["_grid"]
            total = pnl.copy() if total is None else total + pnl
            rows.append(self._public(r))
        if not rows:
            raise RequestError("None of the positions could be valued: " + "; ".join(f["error"] for f in failed))
        sc = rows[0]["scenario"]
        stats = self._stats(prices, weights, total, sc["p10"], sc["p50"], sc["p90"])
        defined = all(not r.get("unlimited_loss") for r in rows)
        cost = float(sum(r["cost"] for r in rows if not r.get("credit")))
        credit = float(sum(r["cost"] for r in rows if r.get("credit")))
        capital = float(sum(self._capital(r) for r in rows))
        return {"symbol": symbol, "rows": rows, "failed": failed, "scenario": sc, "last": rows[0]["last"],
                "eval_date": rows[0]["eval_date"], "combined": {**stats, "cost": cost, "credit": credit, "capital": capital,
                                                                 "defined_risk": defined,
                                                                 "max_loss": stats["worst_in_range"] if defined else None,
                                                                 "unlimited_loss": not defined,
                                                                 "return_on_capital": stats["expected_pnl"] / capital if capital > 0 else None},
                "note": ("The positions are valued together across the same range of prices, so hedges offset. The most it can "
                         "lose is the worst point of the combined curve across the scenario's range when every position has a "
                         "limit, else there is none. Not advice; no order is placed.")}

    def _live_price(self, symbol: str) -> float:
        q = self.svc.market.quote(symbol)
        last = float(q.get("last") or q.get("close") or 0)
        if not last or last <= 0:
            raise RequestError(f"No price for {symbol}")
        return last

    @staticmethod
    def _capital(r: dict[str, Any]) -> float:
        """The money a position ties up: what it costs for a debit or shares, the most it can lose for a credit."""
        if r["kind"] == "short":
            return float(r["cost"])                                        # the short's proceeds stand in for its margin
        if r.get("credit"):
            return float(r["max_loss"] or 0) or float(r["cost"])
        return float(r["cost"])

    def allocate(self, symbol: str, *, budget: float, risk: float, expected_pct: float, sd_pct: float, horizon_bars: int, group: str,
                 kinds: list[str] | None = None, max_share: float = 0.5) -> dict[str, Any]:
        """Split a budget across strategies under the scenario: the units of each that maximise the expected P&L
        minus `risk` times the average loss in the worst tenth of outcomes (CVaR 10%), with no strategy over
        `max_share` of the budget and only strategies whose loss has a limit (so the capital each unit ties up is
        known). A greedy search in blocks, then single units."""
        symbol = self.svc._symbol(symbol)
        if not 1 <= budget <= 10_000_000:
            raise RequestError("budget must be between $1 and $10,000,000")
        if not 0 <= risk <= 10:
            raise RequestError("risk must be between 0 (all-out) and 10 (very cautious)")
        max_share = float(np.clip(max_share, 0.05, 1.0))
        asked = list(dict.fromkeys(k for k in (kinds or STRATEGY_KINDS) if k in STRATEGY_KINDS))
        if kinds is not None and not asked:
            raise RequestError("None of the strategies asked for is known")
        units_of, skipped = [], []
        if "short" in asked:
            skipped.append({"kind": "short", "why": "its loss has no limit, so the capital it ties up is not known"})
        wanted = [k for k in asked if k != "short"]
        prices = weights = None
        last = self._live_price(symbol)
        for k in wanted:
            try:
                r = self._position(symbol, kind=k, quantity=1, expected_pct=expected_pct, sd_pct=sd_pct, horizon_bars=horizon_bars, group=group,
                                   last=last)
            except (RequestError, DataError, ValueError) as exc:
                skipped.append({"kind": k, "why": str(exc)[:160]})
                continue
            if r.get("unlimited_loss"):
                skipped.append({"kind": k, "why": "its loss has no limit, so the capital it ties up is not known"})
                continue
            capital = self._capital(r)
            if capital <= 0 or capital > budget * max_share:
                skipped.append({"kind": k, "why": f"one unit ties up {money_text(capital)}, more than {max_share:.0%} of the budget"})
                continue
            prices, weights, pnl = r["_grid"]
            # Units are capped as the position panel caps them (the strategy builder's 100 contracts).
            most = MAX_SHARES if k == "shares" else MAX_CONTRACTS
            units_of.append({"row": self._public(r), "capital": capital, "pnl": pnl, "units": 0, "most": most, "capped": False})
        if not units_of:
            raise RequestError("Nothing fits the budget: " + ("; ".join(f"{x['kind']}: {x['why']}" for x in skipped[:4]) or "no strategy to split across"))
        lam = float(risk)

        def objective(total: np.ndarray) -> float:
            order = np.argsort(total)
            cum = np.cumsum(weights[order])
            tail = order[cum <= 0.10 + 1e-12]
            if len(tail) == 0:
                tail = order[:1]
            cvar = max(0.0, -float((total[tail] * weights[tail]).sum() / weights[tail].sum()))
            return float((weights * total).sum()) - lam * cvar

        total = np.zeros_like(weights)
        spent = 0.0
        best_obj = objective(total)
        blocks = [max(1, int(0.10 * budget / u["capital"])) for u in units_of]
        steps = 0
        while steps < 4000:
            steps += 1
            # The block that improves the objective most per dollar it spends; blocks differ in size.
            best_i, best_rate, best_gain, best_block = None, 1e-12, 0.0, 0
            for i, u in enumerate(units_of):
                b = blocks[i]
                if b < 1:
                    continue
                if u["units"] + b > u["most"]:
                    u["capped"] = u["units"] >= u["most"]
                    continue
                if spent + b * u["capital"] > budget + 1e-9 or (u["units"] + b) * u["capital"] > max_share * budget + 1e-9:
                    continue
                gain = objective(total + b * u["pnl"]) - best_obj
                rate = gain / (b * u["capital"])
                if gain > 1e-9 and rate > best_rate:
                    best_i, best_rate, best_gain, best_block = i, rate, gain, b
            if best_i is None:
                if all(b <= 1 for b in blocks):
                    break
                blocks = [max(1, b // 2) if b > 1 else 1 for b in blocks]   # finer steps before giving up
                continue
            u = units_of[best_i]
            u["units"] += best_block
            total = total + best_block * u["pnl"]
            spent += best_block * u["capital"]
            best_obj += best_gain
        capped = [u["row"]["label"] for u in units_of if u["units"] >= u["most"]]
        sc = units_of[0]["row"]["scenario"]
        stats = self._stats(prices, weights, total, sc["p10"], sc["p50"], sc["p90"]) if spent > 0 else None
        rows = []
        for u in units_of:
            if u["units"] <= 0:
                continue
            r = u["row"]
            dollars = u["units"] * u["capital"]
            rows.append({"kind": r["kind"], "label": r["label"], "units": int(u["units"]),
                         "unit": "shares" if r["kind"] == "shares" else "contracts", "capital_per_unit": u["capital"],
                         "dollars": dollars, "share": dollars / budget, "cost_label": r["cost_label"],
                         "expected_pnl": float((weights * (u["units"] * u["pnl"])).sum()),
                         "p_profit": float(weights[u["units"] * u["pnl"] > 0].sum()),
                         "legs_text": r.get("legs_text") or [], "expiration": r.get("expiration")})
        rows.sort(key=lambda x: -x["dollars"])
        return {"symbol": symbol, "budget": budget, "risk": lam, "max_share": max_share, "rows": rows, "skipped": skipped, "capped": capped,
                "spent": spent, "left": budget - spent, "combined": {**stats, "return_on_budget": stats["expected_pnl"] / budget,
                                                                    "max_loss": stats["worst_in_range"]} if stats else None,
                "scenario": sc, "last": units_of[0]["row"]["last"], "eval_date": units_of[0]["row"]["eval_date"],
                "note": (f"The split that maximises the expected P&L minus {lam:g} times the average loss in the worst tenth of "
                         f"outcomes, with no strategy over {max_share:.0%} of the budget and only strategies whose loss has a "
                         "limit (a credit spread ties up the most it can lose)."
                         + (f" {', '.join(capped)} stopped at {MAX_CONTRACTS} contracts, the most the builder values." if capped else "")
                         + " A model under one scenario, not advice; no order is placed.")}

    def rank(self, symbol: str, *, quantity: int, expected_pct: float, sd_pct: float, horizon_bars: int, group: str,
             last: float | None = None, kinds: list[str] | None = None, ai_pick: str | None = None,
             contracts: int | None = None) -> dict[str, Any]:
        """Every strategy (or the ones asked for) under the scenario, ranked by expected P&L per dollar at risk;
        the best by the model and the AI's pick are both marked."""
        wanted = [k for k in (kinds or STRATEGY_KINDS) if k in STRATEGY_KINDS]
        rows, failed = [], []
        for k in wanted:
            qty = quantity if k in ("shares", "short") else (contracts or max(1, min(100, quantity // 100 or 1)))
            try:
                rows.append(self._position(symbol, kind=k, quantity=qty, expected_pct=expected_pct, sd_pct=sd_pct,
                                          horizon_bars=horizon_bars, group=group, last=last))
            except (RequestError, DataError, ValueError) as exc:
                failed.append({"kind": k, "error": str(exc)[:200]})
        rows = [self._public(r) for r in rows]
        scored = [r for r in rows if r.get("score") is not None]
        scored.sort(key=lambda r: (-(r["score"]), -(r["p_profit"] or 0)))
        for i, r in enumerate(scored, 1):
            r["rank"] = i
        best = scored[0]["kind"] if scored else None
        return {"symbol": symbol, "rows": rows, "failed": failed, "best": best, "ai_pick": ai_pick if ai_pick in wanted else None,
                "note": "Ranked by expected P&L per dollar at risk under the scenario; a rank is not a recommendation."}


__all__ = ["WhatIf", "BAR_DAYS", "PROVIDERS", "STRATEGY_KINDS", "analyst_calls", "scenario_distribution", "lognormal_band",
           "horizon_label"]
