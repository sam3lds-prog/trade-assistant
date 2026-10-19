"""What-if factors (2.13): the things besides the chart that bear on a stock's next move, each with a reading
today, a sensitivity measured on this stock's own history, and a scenario value the user can change.

    reading     what the factor did today (or overnight, before the open): the move of a proxy fund quoted by
                the data source (Japan EWJ, Europe VGK, China FXI, bonds TLT, Brent BNO, the dollar UUP,
                volatility VIXY, the stock's sector fund) or, when stooq.com answers, the real index (Nikkei,
                DAX, FTSE, Hang Seng, the 10-year yield, Brent, the VIX);
    beta        how much the stock has moved per 1% move of the proxy, from a least-squares fit of a year of
                daily returns, one factor at a time (a joint fit of eight correlated factors on 250 days is
                noise; one at a time is honest about what each alone has carried);
    scenario    the move the user wants to assume; the reading is the starting value.

The model: the stock's expected move over the horizon is the sum of beta x scenario over the chosen factors,
plus the similar-pattern path weighted as the user chooses, plus a news term from this stock's record of
moves with news behind them. The band around it is the stock's own realised volatility over the horizon.
None of it is a forecast: it is arithmetic on what the user assumes, shown so the assumptions are visible.
Nothing here places an order.
"""

from __future__ import annotations

import csv
import io
import logging
import math
import time
from datetime import datetime, timezone
from typing import Any

import numpy as np
import pandas as pd
import requests

from .optionmath import NY
from .providers.base import DataError
from .sectors import SECTOR_FUNDS, sector_fund

log = logging.getLogger(__name__)

STOOQ_URL = "https://stooq.com/q/l/"
STOOQ_TIMEOUT = (4, 8)
STOOQ_CACHE_SECONDS = 300
BETA_DAYS = 250
MIN_BETA_DAYS = 60

# Each factor: the proxy fund the data source quotes, the stooq symbol for the real thing (or None), how the
# proxy relates to the thing itself, and the unit the scenario is set in.
FACTORS: list[dict[str, Any]] = [
    {"key": "overseas_japan", "label": "Japan (Nikkei)", "proxy": "EWJ", "stooq": "^nkx", "unit": "%", "group": "overseas",
     "note": "How Tokyo closed, or is trading, before New York opens."},
    {"key": "overseas_europe", "label": "Europe (DAX)", "proxy": "VGK", "stooq": "^dax", "unit": "%", "group": "overseas",
     "note": "Frankfurt's move; Europe trades through the New York morning."},
    {"key": "overseas_china", "label": "China (Hang Seng)", "proxy": "FXI", "stooq": "^hsi", "unit": "%", "group": "overseas",
     "note": "Hong Kong's move overnight."},
    {"key": "us_market", "label": "US market (S&P 500)", "proxy": "SPY", "stooq": None, "unit": "%", "group": "market",
     "note": "SPY before the open or so far today."},
    {"key": "yield_10y", "label": "10-year Treasury yield", "proxy": "IEF", "stooq": "10usy.b", "unit": "bp", "group": "macro",
     "inverse": True, "scale": 0.075,
     "note": "In basis points. The proxy is the IEF 7-10 year Treasury fund, which moves the other way with a duration "
             "of about 7.5: a 10 bp rise in the yield is about a 0.75% fall in IEF."},
    {"key": "oil_brent", "label": "Oil (Brent)", "proxy": "BNO", "stooq": "cb.f", "unit": "%", "group": "macro",
     "note": "Brent crude; the proxy is the BNO fund."},
    {"key": "dollar", "label": "US dollar", "proxy": "UUP", "stooq": None, "unit": "%", "group": "macro",
     "note": "The UUP dollar fund."},
    {"key": "volatility", "label": "Volatility (VIX)", "proxy": "VIXY", "stooq": "^vix", "unit": "%", "group": "macro",
     "index_scale": 0.5,
     "note": "The VIX index's move. The proxy is the VIXY fund, which holds short-term VIX futures and moves about half "
             "as much as the index; readings and scenarios are in the index's terms."},
    {"key": "sector", "label": "Sector fund", "proxy": None, "stooq": None, "unit": "%", "group": "sector",
     "note": "The SPDR fund of the stock's own sector."},
    {"key": "peers", "label": "Foreign peers", "proxy": None, "stooq": None, "unit": "%", "group": "sector",
     "note": "Tickers you name (US-listed shares of overseas companies in the same business, say TSM or ASML "
             "for a chip maker); their average move."},
]
NON_PRICE_FACTORS: list[dict[str, Any]] = [
    {"key": "news", "label": "News and press releases", "unit": "tone", "group": "news",
     "note": "Headlines in the last 36 hours and their tone, -1 to +1. On this stock, moves with news behind "
             "them have tended to keep going; the term is that record times the tone you assume."},
    {"key": "analysts", "label": "Analyst upgrades and downgrades", "unit": "net", "group": "news",
     "note": "Upgrades minus downgrades in the last week's headlines. Each counts as half an average range in its "
             "direction over the first bars, at most two either way."},
    {"key": "earnings", "label": "Earnings ahead", "unit": "days", "group": "events",
     "note": "Days to the next report. Inside the horizon it widens the band by the move option prices imply."},
    {"key": "pattern", "label": "Similar patterns", "unit": "weight", "group": "pattern",
     "note": "The median path of this stock's own past matches, weighted 0 to 1."},
]


def _num(v: Any) -> float | None:
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if math.isfinite(f) else None


def pct_move(q: dict[str, Any]) -> tuple[float | None, str]:
    """A quote's move against its previous close, in percent, and what the move was read from."""
    prev = _num(q.get("prevclose"))
    last = _num(q.get("last"))
    if not prev or prev <= 0 or not last:
        return None, "no quote"
    when = q.get("trade_time") or ""
    try:
        stamp = datetime.fromisoformat(str(when).replace("Z", "+00:00"))
        stamp = stamp if stamp.tzinfo else stamp.replace(tzinfo=timezone.utc)
        how = f"last trade {stamp.astimezone(NY):%a %H:%M} NY"
    except ValueError:
        how = "last trade"
    return 100.0 * (last / prev - 1.0), how


class Stooq:
    """Real indexes from stooq.com's free CSV quotes (unofficial, about 15 minutes delayed), cached five
    minutes; any failure means the proxy funds are used instead, and the reading says which."""

    def __init__(self, session: requests.Session | None = None, enabled: bool = True):
        self.session = session or requests.Session()
        self.enabled = enabled
        self._cache: tuple[float, frozenset[str], dict[str, dict[str, Any]]] | None = None   # (when, asked, rows)
        self.last_error: str | None = None
        self._failed_at = 0.0

    def quotes(self, symbols: list[str]) -> dict[str, dict[str, Any]]:
        if not self.enabled or not symbols:
            return {}
        now = time.time()
        asked = frozenset(symbols)
        # The cache is by the set asked, so a symbol stooq has no row for (N/D) is not asked again and again.
        if self._cache and now - self._cache[0] < STOOQ_CACHE_SECONDS and asked <= self._cache[1]:
            return {s: self._cache[2][s] for s in symbols if s in self._cache[2]}
        if now - self._failed_at < STOOQ_CACHE_SECONDS:
            return {}                                        # it failed a moment ago; the funds stand in meanwhile
        try:
            r = self.session.get(STOOQ_URL, params={"s": ",".join(symbols), "f": "sd2t2ohlcv", "h": "", "e": "csv"},
                                 timeout=STOOQ_TIMEOUT, headers={"User-Agent": "TradeAssistant/2.13 (personal)"})
            if r.status_code != 200:
                raise DataError(f"stooq answered {r.status_code}")
            rows = parse_stooq(r.text)
            if not rows:
                raise DataError("stooq sent no quotes (its daily limit, or a page instead of the CSV)")
        except (requests.RequestException, DataError, ValueError) as exc:
            self.last_error = f"{exc.__class__.__name__}: {str(exc)[:120]}"
            self._failed_at = now
            log.info("stooq quotes failed; using the proxy funds: %s", self.last_error)
            return {}
        self.last_error = None
        self._cache = (now, asked, rows)
        return {s: rows[s] for s in symbols if s in rows}


def parse_stooq(text: str) -> dict[str, dict[str, Any]]:
    """stooq's CSV: Symbol,Date,Time,Open,High,Low,Close,Volume; a row with N/D means no data."""
    out: dict[str, dict[str, Any]] = {}
    for row in csv.DictReader(io.StringIO(text)):
        sym = (row.get("Symbol") or "").strip().lower()
        close, open_ = _num(row.get("Close")), _num(row.get("Open"))
        if not sym or close is None:
            continue
        out[sym] = {"symbol": sym, "close": close, "open": open_, "high": _num(row.get("High")),
                    "low": _num(row.get("Low")), "date": row.get("Date"), "time": row.get("Time")}
    return out


def beta_of(stock: pd.DataFrame, proxy: pd.DataFrame, days: int = BETA_DAYS) -> dict[str, Any] | None:
    """The stock's daily return per 1% of the proxy's, by least squares on the last `days` finished days they
    share, with the correlation and how many days went in. None with too few days."""
    if stock.empty or proxy.empty:
        return None
    a = stock[["date", "close"]].copy()
    b = proxy[["date", "close"]].copy()
    a["date"], b["date"] = pd.to_datetime(a["date"]).dt.normalize(), pd.to_datetime(b["date"]).dt.normalize()
    both = a.merge(b, on="date", suffixes=("_s", "_p")).tail(days + 1)
    if len(both) < MIN_BETA_DAYS:
        return None
    rs = np.diff(np.log(both["close_s"].to_numpy(dtype=float)))
    rp = np.diff(np.log(both["close_p"].to_numpy(dtype=float)))
    ok = np.isfinite(rs) & np.isfinite(rp)
    rs, rp = rs[ok], rp[ok]
    if len(rs) < MIN_BETA_DAYS or rp.std() <= 1e-9:
        return None
    beta = float(np.cov(rs, rp, bias=True)[0, 1] / rp.var())
    corr = float(np.corrcoef(rs, rp)[0, 1])
    return {"beta": beta, "correlation": corr, "days": int(len(rs))}


def model_path(*, horizon: int, factors: list[dict[str, Any]], scenario: dict[str, Any], pattern: list[float] | None,
               pattern_weight: float, daily_vol: float, atr_pct: float | None, news_term_pct: float,
               analyst_net: float, earnings_in: int | None, event_move_pct: float | None) -> dict[str, Any]:
    """The expected percent move of the stock at each of the next `horizon` bars, with a band.

    Factor shocks land on the first bar (overnight news is priced at the open) and stay; the pattern's median
    path and the news term are spread across the horizon; analyst calls land over the first three bars. The
    band is ±1.28 daily volatilities times the square root of the bars so far (a 10-90 range), widened by the
    event move when earnings fall inside the horizon."""
    h = max(1, int(horizon))
    bars = np.arange(1, h + 1, dtype=float)
    terms: list[dict[str, Any]] = []
    shock_total = 0.0
    for f in factors:
        if not f.get("on") or f.get("beta") is None:
            continue
        value = _num(scenario.get(f["key"], f.get("scenario")))
        if value is None:
            continue
        proxy_move = value * (-f["scale"] if f.get("inverse") else 1.0) if f.get("unit") == "bp" else value * f.get("index_scale", 1.0)
        contrib = f["beta"] * proxy_move
        shock_total += contrib
        terms.append({"key": f["key"], "label": f["label"], "value": value, "unit": f["unit"], "proxy_move_pct": proxy_move,
                      "beta": f["beta"], "contribution_pct": contrib})
    path = np.full(h, shock_total)
    if pattern and pattern_weight > 0:
        p = np.array(pattern[:h], dtype=float)
        p = np.concatenate([p, np.full(h - len(p), p[-1] if len(p) else 0.0)]) if len(p) < h else p
        path = path + pattern_weight * np.nan_to_num(p)
        terms.append({"key": "pattern", "label": "Similar patterns", "value": pattern_weight, "unit": "weight",
                      "contribution_pct": float(pattern_weight * np.nan_to_num(p)[-1])})
    if news_term_pct:
        path = path + news_term_pct * np.minimum(1.0, bars / min(3.0, h))
        terms.append({"key": "news", "label": "News", "value": scenario.get("news"), "unit": "tone",
                      "contribution_pct": news_term_pct})
    if analyst_net and atr_pct:
        step = analyst_net * atr_pct * np.minimum(1.0, bars / min(3.0, h))
        path = path + step
        terms.append({"key": "analysts", "label": "Analyst calls", "value": analyst_net, "unit": "net",
                      "contribution_pct": float(step[-1])})
    sd = daily_vol * 100.0 * np.sqrt(bars)
    if earnings_in is not None and 0 <= earnings_in <= h and event_move_pct:
        sd = np.sqrt(sd ** 2 + np.where(bars >= max(1, earnings_in), event_move_pct ** 2, 0.0))
    z = 1.2816
    return {"horizon": h, "path_pct": [float(v) for v in path], "low_pct": [float(v) for v in path - z * sd],
            "high_pct": [float(v) for v in path + z * sd], "sd_pct": [float(v) for v in sd],
            "expected_pct": float(path[-1]), "terms": terms,
            "note": "The band is the stock's own realised volatility, a 10-90 range; it says nothing about which way."}


class Factors:
    """Readings, betas and the model for one stock."""

    def __init__(self, svc: Any, stooq: Stooq | None = None):
        self.svc = svc
        # Real indexes only with real data: demo data is made up, and the test suite must not reach the network.
        self.stooq = stooq if stooq is not None else Stooq(enabled=bool(getattr(svc.settings, "stooq_quotes", True))
                                                           and svc.market.info.mode != "demo")
        self._beta_cache: dict[tuple, tuple[float, Any]] = {}

    # --- readings ---------------------------------------------------------------------------------------------
    def _quotes(self, symbols: list[str]) -> dict[str, dict[str, Any]]:
        wanted = [s for s in dict.fromkeys(symbols) if s]
        if not wanted:
            return {}
        try:
            return self.svc.market._call("what-if quotes", lambda: self.svc.market.provider.quotes(wanted))
        except DataError as exc:
            log.info("What-if: proxy quotes failed: %s", exc)
            return {}

    def readings(self, symbol: str, peers: list[str] | None = None) -> list[dict[str, Any]]:
        """Every price factor with today's reading: the real index from stooq when it answered, else the proxy."""
        symbol = symbol.upper()
        fund = sector_fund(symbol)
        peers = [p.strip().upper() for p in (peers or []) if p.strip() and p.strip().upper() != symbol][:6]
        rows = []
        for f in FACTORS:
            row = {k: v for k, v in f.items()}
            if f["key"] == "sector":
                row["proxy"] = fund
                row["label"] = f"Sector fund ({fund}, {SECTOR_FUNDS.get(fund, '')})" if fund else "Sector fund (none known)"
            if f["key"] == "peers":
                row["peers"] = peers
            rows.append(row)
        proxies = [r["proxy"] for r in rows if r.get("proxy")] + peers
        quotes = self._quotes(proxies)
        stooq = self.stooq.quotes([r["stooq"] for r in rows if r.get("stooq")]) if self.stooq.enabled else {}
        for r in rows:
            r["reading"], r["reading_source"], r["proxy_move_pct"] = None, None, None
            if r["key"] == "peers":
                moves = [pct_move(quotes[p])[0] for p in r["peers"] if p in quotes and pct_move(quotes[p])[0] is not None]
                if moves:
                    r["reading"], r["reading_source"] = float(np.mean(moves)), f"average of {', '.join(r['peers'])}"
                    r["proxy_move_pct"] = r["reading"]
                r["scenario"] = r["reading"] if r["reading"] is not None else 0.0
                continue
            if r.get("proxy") and r["proxy"] in quotes:
                move, how = pct_move(quotes[r["proxy"]])
                if move is not None:
                    r["proxy_move_pct"] = move
                    r["reading"] = move / r.get("index_scale", 1.0)
                    r["reading_source"] = f"{r['proxy']} {how}" + (f" ({move:+.2f}%, about half the index's move)" if r.get("index_scale") else "")
                    if r.get("unit") == "bp" and r.get("inverse"):
                        r["reading"] = -move / r["scale"]
                        r["reading_source"] = f"from {r['proxy']} ({move:+.2f}%)"
            s = stooq.get(r["stooq"]) if r.get("stooq") else None
            if s and s.get("close") and s.get("open"):
                # stooq's row gives the session's open and latest: the move from the open, not from the last close.
                if r.get("unit") == "bp":
                    r["reading"] = 100.0 * (s["close"] - s["open"])            # yield points to basis points
                    r["reading_source"] = f"stooq {r['stooq']} {s['close']:.2f}%, from the open ({s.get('date')} {s.get('time')})"
                else:
                    r["reading"] = 100.0 * (s["close"] / s["open"] - 1.0)
                    r["reading_source"] = f"stooq {r['stooq']}, from the open ({s.get('date')} {s.get('time')})"
            r["scenario"] = r["reading"] if r["reading"] is not None else 0.0
        return rows

    # --- betas ------------------------------------------------------------------------------------------------
    def betas(self, symbol: str, rows: list[dict[str, Any]]) -> None:
        """Fill each row's beta from a year of daily bars (cached an hour)."""
        symbol = symbol.upper()
        try:
            stock = self.svc.market.bars(symbol, 1.3, complete=True)
        except DataError:
            stock = pd.DataFrame()
        for r in rows:
            r["beta"], r["correlation"], r["beta_days"] = None, None, None
            key = (symbol, r["key"], tuple(r.get("peers") or ()), self.svc.market.info.mode)
            hit = self._beta_cache.get(key)
            if hit and time.time() - hit[0] < 3600:
                r.update(hit[1])
                continue
            fit = None
            try:
                if r["key"] == "peers" and r.get("peers"):
                    frames = []
                    for p in r["peers"]:
                        try:
                            frames.append(self.svc.market.bars(p, 1.3, complete=True)[["date", "close"]].rename(columns={"close": p}))
                        except DataError:
                            continue
                    if frames:
                        merged = frames[0]
                        for fr in frames[1:]:
                            merged = merged.merge(fr, on="date")
                        cols = [c for c in merged.columns if c != "date"]
                        basket = merged[["date"]].assign(close=np.exp(np.log(merged[cols].astype(float)).mean(axis=1)))
                        fit = beta_of(stock, basket)
                elif r.get("proxy"):
                    fit = beta_of(stock, self.svc.market.bars(r["proxy"], 1.3, complete=True))
            except DataError as exc:
                log.info("What-if: no bars for %s's proxy %s: %s", symbol, r.get("proxy"), exc)
            if fit:
                r.update(beta=fit["beta"], correlation=fit["correlation"], beta_days=fit["days"])
                self._beta_cache[key] = (time.time(), {"beta": fit["beta"], "correlation": fit["correlation"],
                                                       "beta_days": fit["days"]})
        return None

    @staticmethod
    def daily_vol(stock: pd.DataFrame, days: int = 60) -> float | None:
        c = stock["close"].to_numpy(dtype=float)[-(days + 1):]
        if len(c) < 21:
            return None
        r = np.diff(np.log(c))
        r = r[np.isfinite(r)]
        return float(r.std()) if len(r) >= 20 else None


def earnings_days(svc: Any, symbol: str) -> dict[str, Any] | None:
    try:
        est = svc._earnings(symbol, None)
    except Exception:                                      # noqa: BLE001 - no SEC contact, or a demo date
        return None
    if not est or not est.get("date"):
        return None
    try:
        when = datetime.fromisoformat(est["date"]).date()
    except ValueError:
        return None
    today = datetime.now(NY).date()
    days = (when - today).days
    trading = int(np.busday_count(today, when)) if days >= 0 else -int(np.busday_count(when, today))
    return {"date": est["date"], "days": days, "trading_days": trading, "source": est.get("source"),
            "estimated": est.get("estimated")}


__all__ = ["FACTORS", "NON_PRICE_FACTORS", "Factors", "Stooq", "parse_stooq", "beta_of", "model_path", "pct_move",
           "earnings_days"]
