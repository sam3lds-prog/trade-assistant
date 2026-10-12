"""Scanners: technical signals across many stocks, and unusual options activity.

**Technical scan.** For each stock, the readings on its latest bar (RSI, MACD against its
signal line, the 50- and 200-day averages, where the price sits in its 52-week range, volume
against its 20-day average) and every pattern from the pattern lab that completed in the last
few bars. Each signal carries how the same signal turned out before, across the stocks in the
signal history: a historical frequency with its interval, not a forecast or a win rate. A
pattern on today's unfinished bar is marked provisional, since it can still disappear by the
close.

**Options activity.** For each stock, the volume and open interest of its calls and puts over
the nearest expirations, put/call ratios, the premium traded (volume x midpoint x 100), and
the contracts whose volume today is large against their open interest: a sign that new
positions are being opened. The data does not say whether a trade was a purchase or a sale, so
call-heavy or put-heavy activity is reported as such, never as bullish or bearish for certain.
Volume is the latest session's: today's so far while the market is open.

Both scans read only. They are bounded (symbols, expirations) so one press cannot turn into
thousands of requests.
"""

from __future__ import annotations

import math
from datetime import date, datetime
from typing import Any, Iterable, Sequence

import pandas as pd

from . import signalbook
from .optionmath import NY
from .patterns import PATTERNS, recent_events
from .providers.base import DataError

# 100 large US companies across every sector: a fixed list, close to the S&P 100 in mid-2026. It is
# not updated automatically when the index changes.
LARGE_CAPS = (
    "AAPL MSFT NVDA AMZN GOOGL META BRK.B AVGO TSLA LLY JPM V UNH XOM MA COST HD PG JNJ WMT "
    "NFLX ABBV BAC CRM ORCL CVX KO MRK AMD PEP TMO ADBE LIN MCD CSCO ACN WFC ABT PM IBM "
    "GE DIS INTU CAT QCOM TXN VZ AMGN NOW ISRG GS DHR T CMCSA NEE PFE SPGI RTX UNP MS "
    "LOW HON AMAT BKNG BLK COP PLTR AXP C SCHW DE LMT GILD MDT BMY SBUX ADP MO MDLZ CL "
    "UPS BA USB SO DUK MMM EMR GD CVS TGT FDX F GM INTC AIG MET COF NKE PYPL TMUS"
).split()

# 2.7: the next 100, so LARGE_CAPS + NEXT_100 is close to the 200 largest US companies by market value in
# mid-2026. A fixed list like the first: not updated when companies grow, shrink, merge or leave. A ticker
# the data source no longer knows is reported as not read, never dropped silently.
NEXT_100 = (
    "MU LRCX KLAC ADI PANW CRWD ANET SNPS CDNS APH FTNT ADSK WDAY ROP MSI TEL GLW DELL APP DDOG "
    "UBER ABNB DASH HOOD COIN TJX ROST ORLY AZO CMG YUM MAR HLT KR MNST "
    "ELV CI HCA MCK COR BSX SYK VRTX REGN ZTS BDX CB PGR AON ICE CME MCO KKR BX APO PNC BK TRV ALL AJG "
    "WM RSG CTAS ITW ETN PH TT JCI CARR NOC LHX TDG GEV HWM CSX SHW ECL APD FCX NEM "
    "EOG SLB PSX MPC VLO OXY WMB KMI AEP SRE EXC PCG CEG VST PLD AMT EQIX WELL SPG DLR"
).split()
TOP_200 = tuple(LARGE_CAPS) + tuple(NEXT_100)

MAX_TECHNICAL_SYMBOLS = 120
MAX_FLOW_SYMBOLS = 30
MAX_FLOW_EXPIRATIONS = 6
VOLUME_SPIKE = 2.0                 # volume at least twice its 20-day average
RSI_LOW, RSI_HIGH = 30.0, 70.0
CALL_HEAVY, PUT_HEAVY = 0.65, 0.35  # share of premium traded in calls
TOP_UNUSUAL = 40

FREQUENCY_NOTE = ("Past cases: of the earlier times this signal fired on the stocks in the signal history, how many "
                  "were followed by a move its way over 10 trading days, with a 95% range. A historical frequency, "
                  "not a forecast or a win rate.")
FLOW_NOTE = ("Volume and open interest do not say who bought and who sold. Heavy call volume can be buyers betting on a "
             "rise or holders selling calls against shares; heavy put volume can be protection or a bet on a fall. "
             "Volume larger than open interest suggests positions being opened today.")


def universe(name: str | None, watchlist: Sequence[str], symbols: Iterable[str] | None = None) -> tuple[str, list[str]]:
    """The symbols to scan: `symbols` when given, otherwise the named universe."""
    given = [s.strip().upper() for s in (symbols or []) if s and s.strip()]
    if given:
        return "custom", list(dict.fromkeys(given))
    key = (name or "watchlist").strip().lower()
    if key == "watchlist":
        return key, list(dict.fromkeys(s.upper() for s in watchlist))
    if key in ("large_caps", "sp100"):
        return "large_caps", list(LARGE_CAPS)
    if key in ("top200", "top_200"):
        return "top200", list(TOP_200)
    raise ValueError("universe must be watchlist, large_caps or top200, or give symbols")


def _f(x: Any) -> float | None:
    try:
        v = float(x)
    except (TypeError, ValueError):
        return None
    return v if math.isfinite(v) else None


# --- technical ------------------------------------------------------------------------------------

def readings(df: pd.DataFrame) -> dict[str, Any]:
    """The indicator readings on the last bar of an enriched frame (see indicators.enrich)."""
    last = df.iloc[-1]
    prev = df.iloc[-2] if len(df) > 1 else None
    close = _f(last["close"])
    prev_close = _f(prev["close"]) if prev is not None else None
    rsi, hist = _f(last.get("rsi")), _f(last.get("macd_hist"))
    s50, s200 = _f(last.get("sma50")), _f(last.get("sma200"))
    finished = bool(last["bar_complete"]) if "bar_complete" in df.columns else True
    vol_ratio = _f(last.get("vol_ratio"))
    if close is None or s50 is None or s200 is None:
        averages = None
    elif close > s50 and close > s200:
        averages = "ABOVE_BOTH"
    elif close < s50 and close < s200:
        averages = "BELOW_BOTH"
    else:
        averages = "MIXED"
    return {
        "date": pd.Timestamp(last["date"]).strftime("%Y-%m-%d"), "bar_finished": finished,
        "close": close,
        "change_pct": 100 * (close / prev_close - 1) if close and prev_close else None,
        "rsi": rsi,
        "rsi_zone": None if rsi is None else "OVERSOLD" if rsi <= RSI_LOW else "OVERBOUGHT" if rsi >= RSI_HIGH else "NEUTRAL",
        "macd": None if hist is None else "ABOVE_SIGNAL" if hist > 0 else "BELOW_SIGNAL",
        "macd_hist": hist, "sma50": s50, "sma200": s200, "averages": averages,
        "sma50_vs_200": None if s50 is None or s200 is None else "ABOVE" if s50 > s200 else "BELOW",
        "range_52w_pct": _f(last.get("pos52")),
        "volume_ratio": vol_ratio,
        # Today's volume is only part of a session until the close, so a spike is called on finished bars.
        "volume_spike": bool(vol_ratio is not None and vol_ratio >= VOLUME_SPIKE and finished),
        "adx": _f(last.get("adx")), "hv20": _f(last.get("hv20")),
    }


def technical_row(symbol: str, df: pd.DataFrame, lookback: int, history: dict[str, dict[str, Any]],
                  validated: dict[str, list[dict[str, Any]]] | None = None) -> dict[str, Any]:
    """One stock's readings and the signals that completed in its last `lookback` bars, each with
    how it turned out before (`history`: signal key -> signalbook.summarize result)."""
    if len(df) < 30:
        raise ValueError("fewer than 30 bars of price history")
    signals = []
    for e in recent_events(df, lookback=lookback):
        key = f"pattern:{e['key']}"
        signals.append({**e, "signal_key": key, "past": history.get(key),
                        "lab": (validated or {}).get(e["key"], [])})
    out = {"symbol": symbol, **readings(df), "signals": signals,
           "bullish": sum(1 for s in signals if s["direction"] > 0),
           "bearish": sum(1 for s in signals if s["direction"] < 0)}
    return out


def matches(row: dict[str, Any], *, direction: str | None = None, signal: str | None = None, rsi: str | None = None,
            min_volume_ratio: float | None = None, only_signals: bool = False) -> bool:
    """Whether a scan row passes the filters (all optional)."""
    sigs = row["signals"]
    if signal:
        sigs = [s for s in sigs if s["key"] == signal]
        if not sigs:
            return False
    if direction in ("bullish", "bearish"):
        want = 1 if direction == "bullish" else -1
        if not any(s["direction"] == want for s in sigs):
            return False
    if only_signals and not sigs:
        return False
    if rsi in ("oversold", "overbought") and (row["rsi_zone"] or "").lower() != rsi:
        return False
    if min_volume_ratio is not None and not (row["volume_ratio"] is not None and row["volume_ratio"] >= min_volume_ratio):
        return False
    return True


def sort_key(row: dict[str, Any]) -> tuple:
    """Stocks with signals first, the most recent signal first, then the most signals."""
    if not row["signals"]:
        return (1, 0, 0, row["symbol"])
    return (0, min(s["bars_ago"] for s in row["signals"]), -len(row["signals"]), row["symbol"])


# --- options activity -----------------------------------------------------------------------------

def _mid(c: dict[str, Any]) -> float | None:
    m = _f(c.get("mid"))
    return m if m is not None and m > 0 else None


def _add(total: float | None, value: float | None) -> float | None:
    """Running sum that stays None (unknown) until a value is known."""
    return total if value is None else (value if total is None else total + value)


def _ratio(a: float | None, b: float | None) -> float | None:
    return a / b if a is not None and b else None


def flow_summary(analyses: Sequence[Any], *, min_volume: int = 250, min_vol_oi: float = 1.0,
                 min_premium: float = 0.0) -> dict[str, Any]:
    """Calls against puts and the unusually active contracts across the chains in `analyses`.

    Each figure is kept per side and per expiration, and stays None (unknown) when no contract
    reported it; a contract whose open interest is not reported cannot be judged unusual."""
    sides = {k: {"volume": None, "open_interest": None, "premium": None, "unpriced_volume": 0.0} for k in ("call", "put")}
    by_exp, unusual = [], []
    unmeasured = 0
    for a in analyses:
        row = {"expiration": a.expiration, "call_volume": None, "put_volume": None, "call_oi": None, "put_oi": None}
        for c in a.contracts:
            kind = c.get("option_type")
            if kind not in sides:
                continue
            vol, oi = _f(c.get("volume")), _f(c.get("open_interest"))
            t = sides[kind]
            mid = _mid(c)
            t["volume"] = _add(t["volume"], vol)
            row[f"{kind}_volume"] = _add(row[f"{kind}_volume"], vol)
            if vol is not None:
                if mid is None:
                    t["unpriced_volume"] += vol
                else:
                    t["premium"] = _add(t["premium"], vol * mid * 100)
            t["open_interest"] = _add(t["open_interest"], oi)
            row[f"{kind}_oi"] = _add(row[f"{kind}_oi"], oi)
            if vol is None or vol < min_volume:
                continue
            if oi is None:
                unmeasured += 1                        # cannot tell whether this is unusual
                continue
            ratio = vol / oi if oi > 0 else None
            if ratio is not None and ratio < min_vol_oi:
                continue
            premium = vol * mid * 100 if mid is not None else None
            if min_premium and (premium is None or premium < min_premium):
                continue
            unusual.append({"expiration": a.expiration, "option_type": kind, "strike": c["strike"], "volume": vol,
                            "open_interest": oi, "volume_to_oi": ratio, "mid": mid, "bid": c.get("bid"), "ask": c.get("ask"),
                            "premium": premium, "moneyness_pct": _f(c.get("moneyness_pct")), "delta": _f(c.get("delta")),
                            "iv": _f(c.get("iv")),
                            "why": "no open interest before today" if ratio is None else f"volume {ratio:.1f} times open interest"})
        row["put_call_volume"] = _ratio(row["put_volume"], row["call_volume"])
        by_exp.append(row)
    cv, pv = sides["call"]["volume"], sides["put"]["volume"]
    coi, poi = sides["call"]["open_interest"], sides["put"]["open_interest"]
    cp, pp = sides["call"]["premium"], sides["put"]["premium"]
    share = cp / (cp + pp) if cp is not None and pp is not None and cp + pp > 0 else None
    unusual.sort(key=lambda u: (u["premium"] or 0, u["volume"]), reverse=True)
    both_vol = cv + pv if cv is not None and pv is not None else None
    both_oi = coi + poi if coi is not None and poi is not None else None
    return {
        "call_volume": cv, "put_volume": pv, "call_open_interest": coi, "put_open_interest": poi,
        "put_call_volume": _ratio(pv, cv), "put_call_open_interest": _ratio(poi, coi),
        "call_premium": cp, "put_premium": pp,
        "unpriced_volume": sides["call"]["unpriced_volume"] + sides["put"]["unpriced_volume"],
        "call_premium_share": share,
        "lean": (None if share is None else "CALL_HEAVY" if share >= CALL_HEAVY else
                 "PUT_HEAVY" if share <= PUT_HEAVY else "BALANCED"),
        "turnover": _ratio(both_vol, both_oi),
        "unusual": unusual, "unusual_unmeasured": unmeasured, "expirations": by_exp,
    }


def flow_expirations(dates: Iterable[str], today: date, max_dte: int, count: int) -> list[str]:
    """The nearest `count` expirations at least a day and at most `max_dte` days out."""
    near = sorted(d for d in dates if 1 <= (date.fromisoformat(d) - today).days <= max_dte)
    return near[:count]


class Scanner:
    def __init__(self, svc: Any, analyst: Any | None = None):
        self.svc = svc
        self.analyst = analyst

    @property
    def market(self):
        return self.svc.market

    def _history(self) -> tuple[bool, dict[str, dict[str, Any]]]:
        """How each pattern turned out before, pooled across the signal history (10 trading days)."""
        meta = self.analyst.history_meta() if self.analyst else None
        if not meta:
            return False, {}
        source = self.market.info.mode
        out = {}
        for key, pat in PATTERNS.items():
            rows = signalbook.load(self.svc.db, source, f"pattern:{key}")
            out[f"pattern:{key}"] = signalbook.summarize(rows, pat.direction)
        return True, out

    def technical(self, *, universe_name: str | None = None, symbols: Iterable[str] | None = None, lookback: int = 5,
                  direction: str | None = None, signal: str | None = None, rsi: str | None = None,
                  min_volume_ratio: float | None = None, only_signals: bool = False) -> dict[str, Any]:
        name, names = universe(universe_name, self.svc.settings.watchlist, symbols)
        if not names:
            raise ValueError("Give at least one symbol")
        if len(names) > MAX_TECHNICAL_SYMBOLS:
            raise ValueError(f"At most {MAX_TECHNICAL_SYMBOLS} symbols per scan")
        if signal and signal.removeprefix("pattern:") not in PATTERNS:
            raise ValueError(f"Unknown signal {signal}")
        signal = signal.removeprefix("pattern:") if signal else None
        lookback = max(1, min(int(lookback), 20))
        built, history = self._history()
        validated = self.analyst.validated() if self.analyst else {}
        rows, errors = [], []
        for sym in names:
            try:
                sym = self.svc._symbol(sym)
                df = self.market.enriched(sym, 2)
                rows.append(technical_row(sym, df, lookback, history, validated))
            except (DataError, ValueError) as exc:
                errors.append({"symbol": sym, "error": str(exc)})
        shown = [r for r in rows if matches(r, direction=direction, signal=signal, rsi=rsi,
                                             min_volume_ratio=min_volume_ratio, only_signals=only_signals)]
        shown.sort(key=sort_key)
        notes = [FREQUENCY_NOTE]
        if not built:
            notes.append("The signal history has not been built, so the signals have no past cases yet. Build it on the "
                         "Pattern lab screen.")
        if self.market.info.mode == "demo":
            notes.insert(0, "Demo prices are random: signals and past cases say nothing about real markets.")
        if any(not r["bar_finished"] for r in rows):
            notes.append("Today's bar is not finished: readings and signals on it are provisional and can change by "
                         "the close.")
        return {"checked_at": datetime.now(NY).isoformat(timespec="seconds"), "universe": name, "scanned": len(rows),
                "matched": len(shown), "lookback": lookback, "rows": shown, "errors": errors,
                "history_built": built, "data_source": self.market.info.mode, "notes": notes,
                "signals": [{"key": k, "label": p.label, "direction": p.direction} for k, p in PATTERNS.items()]}

    def flow(self, *, universe_name: str | None = None, symbols: Iterable[str] | None = None, max_dte: int = 60,
             expirations: int = 3, min_volume: int = 250, min_vol_oi: float = 1.0,
             min_premium: float = 0.0) -> dict[str, Any]:
        name, names = universe(universe_name, self.svc.settings.watchlist, symbols)
        if not names:
            raise ValueError("Give at least one symbol")
        if len(names) > MAX_FLOW_SYMBOLS:
            raise ValueError(f"At most {MAX_FLOW_SYMBOLS} symbols per options scan: each reads up to "
                             f"{MAX_FLOW_EXPIRATIONS} option chains")
        max_dte = max(1, min(int(max_dte), 400))
        expirations = max(1, min(int(expirations), MAX_FLOW_EXPIRATIONS))
        today = datetime.now(NY).date()
        rows, unusual, errors = [], [], []
        for sym in names:
            try:
                sym = self.svc._symbol(sym)
                picked = flow_expirations(self.market.expirations(sym), today, max_dte, expirations)
                if not picked:
                    raise ValueError(f"no expiration within {max_dte} days")
                analyses, failed = [], []
                for exp in picked:
                    try:
                        analyses.append(self.svc._chain_for(sym, exp))
                    except (DataError, ValueError) as exc:
                        failed.append(f"{exp}: {exc}")
                if not analyses:
                    raise ValueError("; ".join(failed))
                s = flow_summary(analyses, min_volume=min_volume, min_vol_oi=min_vol_oi, min_premium=min_premium)
                for u in s["unusual"]:
                    u["symbol"] = sym
                    u["dte"] = (date.fromisoformat(u["expiration"]) - today).days
                unusual.extend(s["unusual"])
                rows.append({"symbol": sym, "spot": analyses[0].spot, "expirations_read": picked[:len(analyses)],
                             "failed": failed, **{k: v for k, v in s.items() if k != "unusual"},
                             "unusual_count": len(s["unusual"])})
            except (DataError, ValueError) as exc:
                errors.append({"symbol": sym, "error": str(exc)})
        unusual.sort(key=lambda u: (u["premium"] or 0, u["volume"]), reverse=True)
        rows.sort(key=lambda r: (-(r["unusual_count"]), -((r["call_premium"] or 0) + (r["put_premium"] or 0))))
        clock = self.market.clock()
        notes = [FLOW_NOTE]
        if clock.get("state") != "open":
            notes.append("The market is not open: volume is the last session's.")
        if self.market.info.mode == "demo":
            notes.insert(0, "Demo data: volumes and open interest are made up.")
        return {"checked_at": datetime.now(NY).isoformat(timespec="seconds"), "universe": name, "rows": rows,
                "unusual": unusual[:TOP_UNUSUAL], "unusual_total": len(unusual), "errors": errors,
                "settings": {"max_dte": max_dte, "expirations": expirations, "min_volume": min_volume,
                             "min_vol_oi": min_vol_oi, "min_premium": min_premium},
                "data_source": self.market.info.mode, "market": clock.get("state"), "notes": notes}


__all__ = ["Scanner", "LARGE_CAPS", "NEXT_100", "TOP_200", "universe", "readings", "technical_row", "matches", "sort_key", "flow_summary",
           "flow_expirations"]
