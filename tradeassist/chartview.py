"""The interactive chart (2.12): the bars for a range and grouping with their indicators, the swings, levels
and trendlines, where a top or bottom may be forming, the news on the bars, the similar patterns in the
stock's own past, and the possible outcomes with how often each has happened.

Three calls, one screen:

    chart(symbol, range, group, start, end)   the bars and everything drawn on them
    similar(symbol, ..., window)              matches for the window on the screen, and what followed them
    outlook(symbol, ..., window, horizon)     the outcomes: pattern matches, a top or bottom forming, recent
                                              signals' past record, the news, each with its own frequency

Every percentage here is a frequency: how often this happened before on this stock, with a 95% interval,
never a forecast. Nothing here places an order.
"""

from __future__ import annotations

import logging
import math
import time
from datetime import datetime, timedelta, timezone
from typing import Any

import numpy as np
import pandas as pd

from . import reversal as reversal_engine
from . import similar as similar_engine
from .indicators import enrich
from .intraday import GROUPS, RANGES, Intraday, local_shift
from .morning import MOVE_MIN_RANGES, features_of, overextended, past_record
from .optionmath import NY
from .providers.base import DataError
from .structure import SWING_ATR
from .structure import analyse as analyse_structure

log = logging.getLogger(__name__)

MAX_BARS = 6000
NEWS_DAYS_MAX = 30
INDICATOR_COLUMNS = ("ema20", "sma50", "sma200", "bb_low", "bb_mid", "bb_high", "rsi", "macd", "macd_signal", "macd_hist",
                     "atr", "vwap")


class RequestError(ValueError):
    """A bad request for the chart: the caller's mistake, not the data's."""


def _range_days(range_key: str, start: str | None, end: str | None) -> tuple[float, datetime, datetime, str]:
    """The window a range word or a custom start/end covers: (calendar days, start, end, label)."""
    now = datetime.now(NY)
    if range_key == "custom":
        if not start:
            raise RequestError("A custom range needs a start date (YYYY-MM-DD)")
        try:
            def moment(text: str) -> datetime:
                d = datetime.fromisoformat(text)
                return d.replace(tzinfo=NY) if d.tzinfo is None else d.astimezone(NY)
            s = moment(start)
            e = (moment(end) + timedelta(days=1) if end and len(end) == 10 else moment(end) if end else now)
        except ValueError as exc:
            raise RequestError(f"Dates must be YYYY-MM-DD: {exc}") from None
        if e <= s:
            raise RequestError("The end of a custom range must come after its start")
        if e > now + timedelta(days=1):
            e = now
        shown = min(e, now) - (timedelta(days=1) if end and len(end) == 10 and e <= now else timedelta(0))
        return (e - s).total_seconds() / 86400, s, e, f"{s:%b %-d, %Y} to {shown:%b %-d, %Y}"
    if range_key not in RANGES:
        raise RequestError(f"range must be one of {', '.join(RANGES)} or custom")
    days = RANGES[range_key]["days"]
    if range_key == "1d":
        # The last trading day: today when it has traded, else the last weekday.
        day = now
        while day.weekday() >= 5 or (day.date() == now.date() and (now.hour, now.minute) < (9, 30) and day.date() == now.date()):
            day -= timedelta(days=1)
            day = day.replace(hour=23, minute=59)
        s = day.replace(hour=0, minute=0, second=0, microsecond=0)
        return max(days, (now - s).total_seconds() / 86400), s, now, RANGES[range_key]["label"]
    return days, now - timedelta(days=days), now, RANGES[range_key]["label"]


def _r(v: Any, places: int = 4) -> float | None:
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return round(f, places) if math.isfinite(f) else None


class ChartView:
    def __init__(self, svc: Any, analyst: Any):
        self.svc = svc
        self.analyst = analyst
        self.intraday = Intraday(svc.market, svc.db)
        self._series_cache: dict[tuple, tuple[float, pd.DataFrame, dict[str, Any]]] = {}

    # --- the series ------------------------------------------------------------------------------------------
    def series(self, symbol: str, group: str, days: float) -> tuple[pd.DataFrame, dict[str, Any]]:
        """The whole available series of `group` bars (not just the range), enriched, with `t` and `tl` on
        every row; and notes about the source. Cached a minute so the three calls share one build."""
        g = GROUPS[group]
        years = 10 if group in ("1M", "1Q") else max(5.0, days / 365.25 + 0.3) if g["kind"] != "intraday" else 0
        key = (symbol, group, round(days, 1) if g["kind"] == "intraday" else round(years, 1), self.svc.market.info.mode)
        now = time.time()
        for k in [k for k, v in self._series_cache.items() if now - v[0] >= 60]:
            self._series_cache.pop(k, None)
        hit = self._series_cache.get(key)
        if hit:
            return hit[1], hit[2]
        notes: dict[str, Any] = {"group": group, "kind": g["kind"], "label": g["label"]}
        if g["kind"] == "intraday":
            reach = self.intraday.offered()
            notes["reach_days"] = reach.get(g["source"], 0)
            notes["source_note"] = reach.get("note")
            # The whole reach is fetched so the pattern search has the most history there is.
            frame = self.intraday.series(symbol, group, max(days, notes["reach_days"] or days))
            if frame.empty:
                raise DataError(f"No {g['label']} bars for {symbol}")
            frame = frame.rename(columns={"t": "t"})
            frame["date"] = pd.to_datetime(frame["t"], unit="s", utc=True)
        else:
            # Daily and grouped bars: five years, or ten for monthly and quarterly, so the search has history.
            frame = self.intraday.series(symbol, group, years * 365.25)
            if frame.empty:
                raise DataError(f"No bars for {symbol}")
            opens = pd.to_datetime(frame["date"]).dt.tz_localize(NY) + pd.Timedelta(hours=9, minutes=30)
            frame["t"] = np.array([int(x.timestamp()) for x in opens], dtype="int64")
        frame = frame.reset_index(drop=True)
        frame["tl"] = [local_shift(int(t)) for t in frame["t"]]
        if len(frame) > 20:
            # The whole enriched frame is kept (the reversal engine needs its pattern columns), with `complete`
            # and `tl` carried over; the chart reads the indicator columns off it.
            rich = enrich(frame.rename(columns={"complete": "bar_complete"}))
            rich["complete"] = frame["complete"].to_numpy()
            rich["tl"] = frame["tl"].to_numpy()
            frame = rich
        self._series_cache[key] = (time.time(), frame, notes)
        return frame, notes

    def _reversal_eval(self, frame: pd.DataFrame) -> dict[str, Any] | None:
        """The reversal engine on the series, run once per cached series (about a second on 2,500 bars)."""
        if len(frame) < 80 or "atr" not in frame.columns:
            return None
        cached = frame.attrs.get("reversal")
        if cached is not None:
            return cached
        try:
            ev = reversal_engine.evaluate(frame)
        except Exception:                                      # noqa: BLE001 - the engine needs the pattern columns
            log.warning("Reversal engine failed on the chart series", exc_info=True)
            return None
        frame.attrs["reversal"] = ev
        return ev

    def _window(self, frame: pd.DataFrame, start: datetime, end: datetime, range_key: str = "") -> tuple[int, int]:
        """Bar positions of the first and last bar inside [start, end]. For 1D, the last day that has bars (a
        holiday, or the minutes before the open, would otherwise leave it empty)."""
        t = frame["t"].to_numpy(dtype="int64")
        if range_key == "1d":
            last_day = datetime.fromtimestamp(int(t[-1]), NY).date()
            days = [datetime.fromtimestamp(int(x), NY).date() for x in t[-500:]]
            first = len(t) - len(days) + days.index(last_day)
            return int(first), int(len(t) - 1)
        lo, hi = int(start.timestamp()), int(end.timestamp())
        idx = np.flatnonzero((t >= lo) & (t <= hi))
        if len(idx) == 0:
            raise RequestError("No bars fall inside that range: pick dates the market traded on")
        return int(idx[0]), int(idx[-1])

    def resolve(self, symbol: str, range_key: str, group: str | None, start: str | None, end: str | None) -> dict[str, Any]:
        """The range and group settled: which groups the range allows, and the group used."""
        days, s, e, label = _range_days(range_key, start, end)
        allowed = self.intraday.available_groups(days)
        if not allowed:
            allowed = ["1d"]
        wanted = group or (RANGES.get(range_key, {}).get("default") if range_key != "custom" else None)
        if wanted not in allowed:
            # The nearest group the range allows: the range's own default, else the finest daily-or-coarser.
            wanted = RANGES.get(range_key, {}).get("default") if RANGES.get(range_key, {}).get("default") in allowed \
                else next((g for g in ("1d", "1h", "15m", "5m", "1w", "1M", "1Q", "1m") if g in allowed), allowed[0])
        return {"range": range_key, "days": days, "start": s, "end": e, "label": label, "group": wanted,
                "groups": allowed}

    # --- the chart ---------------------------------------------------------------------------------------------
    def chart(self, symbol: str, range_key: str = "6m", group: str | None = None, start: str | None = None,
              end: str | None = None, swing_atr: float = SWING_ATR) -> dict[str, Any]:
        symbol = self.svc._symbol(symbol)
        r = self.resolve(symbol, range_key, group, start, end)
        frame, notes = self.series(symbol, r["group"], r["days"])
        i0, i1 = self._window(frame, r["start"], r["end"], r["range"])
        if i1 - i0 + 1 > MAX_BARS:
            raise RequestError(f"That range holds {i1 - i0 + 1} bars at this grouping; group them more coarsely")
        # The whole series goes to the chart (the last MAX_BARS of it), with the range marked as the window,
        # so the chart can be panned and zoomed past the range without another request.
        offset = max(0, len(frame) - MAX_BARS, i1 + 1 - MAX_BARS)
        view = frame.iloc[offset:]
        g = GROUPS[r["group"]]
        cols = ["open", "high", "low", "close", "volume"] + [c for c in INDICATOR_COLUMNS if c in view.columns]
        view = view.assign(complete=view["complete"].astype(bool))
        bars = []
        for row in view[["t", "tl", "complete", *cols]].itertuples(index=False):
            d = {"t": int(row.t), "tl": int(row.tl), "complete": bool(row.complete)}
            for c, v in zip(cols, row[3:]):
                d[c] = _r(v)
            bars.append(d)
        ahead = max(h for h, _ in similar_engine.HORIZONS.get(r["group"], similar_engine.HORIZONS["1d"]))
        out: dict[str, Any] = {
            "symbol": symbol, "range": {k: v for k, v in r.items() if k not in ("start", "end")},
            # Chart times for the bars after the last one, so a projection can be drawn past it.
            "future_times": self._future_times(frame, len(frame) - 1, r["group"], ahead),
            "range_start": r["start"].isoformat(timespec="seconds"), "range_end": r["end"].isoformat(timespec="seconds"),
            "group": {"key": r["group"], **g}, "bars": bars,
            "window": {"start": i0, "end": i1, "offset": offset, "bars": i1 - i0 + 1},
            "history_bars": int(len(frame)), "source": notes, "atr": _r(frame["atr"].iloc[i1]) if "atr" in frame.columns else None,
            "warnings": [],
        }
        if self.svc.market.info.mode == "demo":
            out["warnings"].append("Demo prices are made up; bars within the day are drawn from each day's made-up open, "
                                   "high, low and close.")
        err = self.intraday.last_error.get(f"{symbol}:{g.get('source')}")
        if err:
            out["warnings"].append(f"The newest bars could not be refreshed: {err}")
        # Structure: swings, support and resistance, trendlines, on the series the chart shows.
        if len(frame) >= 60:
            try:
                structure = analyse_structure(frame, swing_atr)
                tl = frame["tl"].to_numpy(dtype="int64")
                swings = [{**sw, "tl": int(tl[sw["index"]]), "tl_confirmed": int(tl[sw["confirmed"]])}
                          for sw in structure["swings"] if sw["index"] >= offset]
                lines = [{**ln, "tl_from": int(tl[ln["from_index"]]), "tl_through": int(tl[ln["through_index"]]),
                          "tl_to": int(tl[ln["to_index"]])} for ln in structure.get("trendlines") or []]
                levels = {side: [{**lv, "tl_first": int(tl[lv["first_index"]]), "tl_last": int(tl[lv["last_index"]])}
                                 for lv in structure["levels"][side]] for side in ("support", "resistance")}
                out["structure"] = {"swings": swings, "levels": levels, "trendlines": lines,
                                    "state": structure.get("state"), "headline": structure.get("headline"),
                                    "detail": structure.get("detail"), "swing_atr": swing_atr}
            except Exception as exc:                          # noqa: BLE001 - structure is a bonus on the chart
                out["warnings"].append(f"Swings and levels could not be read: {exc.__class__.__name__}")
        # Where a top or bottom may be forming, bar by bar on this series.
        out["reversal"] = self._reversal_on(frame, offset, len(frame) - 1)
        # The news on these bars.
        out["news"] = self._news_markers(symbol, frame, i0, i1, g)
        return out

    def _reversal_on(self, frame: pd.DataFrame, i0: int, i1: int) -> dict[str, Any] | None:
        ev = self._reversal_eval(frame)
        if ev is None:
            return None
        markers = []
        tl = frame["tl"].to_numpy()
        for side in reversal_engine.SIDES:
            st = ev[side]["stage"]
            for t in range(max(1, i0), i1 + 1):
                if st[t] == "CANDIDATE" and st[t - 1] not in ("CANDIDATE", "CONFIRMED"):
                    markers.append({"tl": int(tl[t]), "side": side, "stage": "CANDIDATE", "price": float(frame["close"].iloc[t])})
                elif st[t] == "CONFIRMED" and st[t - 1] != "CONFIRMED":
                    markers.append({"tl": int(tl[t]), "side": side, "stage": "CONFIRMED", "price": float(frame["close"].iloc[t])})
                elif st[t] == "FAILED" and st[t - 1] not in ("FAILED",):
                    markers.append({"tl": int(tl[t]), "side": side, "stage": "FAILED", "price": float(frame["close"].iloc[t])})
        last = len(frame) - 1
        now: dict[str, Any] = {}
        for side in reversal_engine.SIDES:
            stage = str(ev[side]["stage"][last])
            entry = {"stage": stage, "score": int(ev[side]["score"][last]),
                     "evidence": [reversal_engine.EVIDENCE_WORDS[k] for k, pts in ev[side]["points"].items()
                                  if pts[last] > 0]}
            if stage in ("WATCH", "CANDIDATE", "CONFIRMED"):
                try:
                    lv = reversal_engine.levels(frame, ev, side, last)
                    entry["levels"] = {k: (_r(v) if isinstance(v, (int, float)) else v) for k, v in lv.items()}
                except Exception:                              # noqa: BLE001
                    pass
            now[side] = entry
        return {"markers": sorted(markers, key=lambda m: m["tl"]), "now": now,
                "note": "Stages read bar by bar on the bars shown (WATCH, CANDIDATE, CONFIRMED, FAILED), each from that "
                        "bar and earlier ones only. Points are evidence, not probabilities."}

    def _news_markers(self, symbol: str, frame: pd.DataFrame, i0: int, i1: int, g: dict[str, Any]) -> dict[str, Any]:
        t = frame["t"].to_numpy(dtype="int64")
        tl = frame["tl"].to_numpy(dtype="int64")
        lo, hi = int(t[i0]), int(t[i1]) + max(int(g["seconds"]) or 86400, 1)
        if i1 == len(t) - 1:
            # The range ends at the newest bar: news since it (a weekend's, a pre-market morning's) sits on that bar.
            hi = max(hi, int(time.time()))
        span_days = (hi - lo) / 86400
        items: list[dict[str, Any]] = []
        errors: list[str] = []
        news = getattr(self.svc, "news", None)
        if news is not None and span_days <= NEWS_DAYS_MAX + 1:
            try:
                got = news.company(symbol, days=min(NEWS_DAYS_MAX, max(1, math.ceil(span_days))))
                items += got.get("items") or []
                errors += got.get("errors") or []
            except Exception as exc:                           # noqa: BLE001
                errors.append(f"news: {exc.__class__.__name__}")
        try:
            rows = self.svc.db.query("SELECT headline, source, url, published_at, received_at, tone FROM live_news WHERE "
                                     "symbols LIKE ? AND received_at >= ? ORDER BY id DESC LIMIT 200",
                                     (f'%"{symbol}"%', datetime.fromtimestamp(lo, timezone.utc).isoformat()))
            for r in rows:
                items.append({"headline": r["headline"], "source": r["source"], "url": r["url"],
                              "published": r["published_at"] or r["received_at"], "tone": r["tone"], "kind": "live"})
        except Exception:                                      # noqa: BLE001 - no table yet
            pass
        seen: set[str] = set()
        markers = []
        for it in items:
            when = it.get("published")
            if not when:
                continue
            try:
                moment = datetime.fromisoformat(str(when).replace("Z", "+00:00"))
                if moment.tzinfo is None:
                    moment = moment.replace(tzinfo=timezone.utc)
            except ValueError:
                continue
            ts = int(moment.timestamp())
            if ts < lo or ts > hi:
                continue
            key = (it.get("headline") or "").strip().lower()[:80]
            if not key or key in seen:
                continue
            seen.add(key)
            pos = int(np.searchsorted(t, ts, side="right") - 1)
            pos = min(max(pos, i0), i1)
            # A headline before the open belongs to that day's bars, not the day before's.
            day = datetime.fromtimestamp(ts, NY).date()
            if pos + 1 <= i1 and datetime.fromtimestamp(int(t[pos]), NY).date() < day <= \
                    datetime.fromtimestamp(int(t[pos + 1]), NY).date():
                pos += 1
            markers.append({"t": ts, "tl": int(tl[pos]), "headline": it.get("headline"), "source": it.get("source"),
                            "url": it.get("url") if str(it.get("url") or "").startswith(("http://", "https://")) else None,
                            "tone": it.get("tone"), "kind": it.get("kind") or "news", "via": it.get("via")})
        markers.sort(key=lambda m: m["t"])
        out: dict[str, Any] = {"items": markers[-120:], "errors": errors[:3]}
        if span_days > NEWS_DAYS_MAX + 1:
            out["note"] = f"News is shown for ranges up to {NEWS_DAYS_MAX} days; the sources keep no more."
        return out

    # --- similar patterns ---------------------------------------------------------------------------------------
    def similar(self, symbol: str, range_key: str, group: str | None, start: str | None, end: str | None,
                window_start: int | None = None, window_end: int | None = None,
                min_corr: float = similar_engine.MIN_CORRELATION) -> dict[str, Any]:
        symbol = self.svc._symbol(symbol)
        r = self.resolve(symbol, range_key, group, start, end)
        frame, _ = self.series(symbol, r["group"], r["days"])
        i0, i1 = self._window(frame, r["start"], r["end"], r["range"])
        last = len(frame) - 1
        ws = i0 if window_start is None else max(0, min(int(window_start), last))
        we = i1 if window_end is None else max(ws, min(int(window_end), last))
        if not 0.3 <= min_corr <= 0.99:
            raise RequestError("min_correlation must be between 0.3 and 0.99")
        atr = float(frame["atr"].iloc[we]) if "atr" in frame.columns and math.isfinite(float(frame["atr"].iloc[we])) else None
        found = similar_engine.search(frame, ws, we, r["group"], atr=atr, min_corr=min_corr)
        tl = frame["tl"].to_numpy(dtype="int64")
        t = frame["t"].to_numpy(dtype="int64")
        for m in found["matches"]:
            m["tl_start"], m["tl_end"] = int(tl[m["start"]]), int(tl[m["end"]])
            m["date_start"] = datetime.fromtimestamp(int(t[m["start"]]), NY).isoformat(timespec="minutes")
            m["date_end"] = datetime.fromtimestamp(int(t[m["end"]]), NY).isoformat(timespec="minutes")
        found["window"].update({"tl_start": int(tl[ws]), "tl_end": int(tl[we]), "last_close": float(frame["close"].iloc[we])})
        found["window_times"] = [int(x) for x in tl[ws:we + 1]]
        found["future_times"] = self._future_times(frame, we, r["group"], len((found.get("overlay") or {}).get("median") or [])
                                                   or max(h for h, _ in similar_engine.HORIZONS.get(r["group"], similar_engine.HORIZONS["1d"])))
        found["group"] = r["group"]
        return found

    @staticmethod
    def _future_times(frame: pd.DataFrame, pos: int, group: str, count: int) -> list[int]:
        """Chart times for `count` bars after bar `pos`: real later bars when the series has them, then the
        grid continued (trading days only for daily and coarser, the session grid within the day)."""
        tl = frame["tl"].to_numpy(dtype="int64")
        t = frame["t"].to_numpy(dtype="int64")
        out = [int(x) for x in tl[pos + 1:pos + 1 + count]]
        if len(out) >= count:
            return out
        g = GROUPS[group]
        last = datetime.fromtimestamp(int(t[pos + len(out)] if pos + len(out) < len(t) else t[-1]), timezone.utc).astimezone(NY)
        while len(out) < count:
            if g["kind"] == "intraday":
                last = last + timedelta(seconds=g["seconds"])
                if (last.hour, last.minute) >= (16, 0):
                    last = (last + timedelta(days=1)).replace(hour=9, minute=30, second=0)
                while last.weekday() >= 5:
                    last += timedelta(days=1)
            elif group == "1d":
                last += timedelta(days=1)
                while last.weekday() >= 5:
                    last += timedelta(days=1)
            elif group == "1w":
                last += timedelta(days=7)
            elif group == "1M":
                last = (last.replace(day=1) + timedelta(days=32)).replace(day=1)
            else:
                last = (last.replace(day=1) + timedelta(days=95)).replace(day=1)
            out.append(local_shift(int(last.timestamp())))
        return out

    # --- the outlook --------------------------------------------------------------------------------------------
    def outlook(self, symbol: str, range_key: str, group: str | None, start: str | None, end: str | None,
                window_start: int | None = None, window_end: int | None = None, horizon: int | None = None,
                min_corr: float = similar_engine.MIN_CORRELATION) -> dict[str, Any]:
        symbol = self.svc._symbol(symbol)
        found = self.similar(symbol, range_key, group, start, end, window_start, window_end, min_corr)
        group_key = found["group"]
        horizons = found["horizons"]
        chosen = None
        if horizons:
            if horizon is None:
                chosen = horizons[min(2, len(horizons) - 1)]          # the middle horizon: a week on daily bars
            else:
                chosen = next((h for h in horizons if h["bars"] == horizon), None) or \
                    next((h for h in horizons if h["bars"] >= horizon), horizons[-1])
        readings: list[dict[str, Any]] = []
        votes: list[tuple[str, int, str]] = []            # (reading, direction, why)
        price = None
        try:
            q = self.svc.market.quote(symbol)
            price = q.get("last") or q.get("close")
        except Exception:                                  # noqa: BLE001
            q = {}
        # 1. The pattern matches.
        if chosen:
            freq = chosen["up_frequency"]
            d = 1 if freq >= 0.55 else -1 if freq <= 0.45 else 0
            lo_f, hi_f = (chosen["low"], chosen["high"]) if d >= 0 else (1 - chosen["high"], 1 - chosen["low"])
            readings.append({"key": "patterns", "label": f"Similar patterns, {chosen['label']} on",
                             "direction": d, "frequency": freq if d >= 0 else 1 - freq, "low": lo_f, "high": hi_f,
                             "cases": chosen["cases"], "why": "the pattern matches",
                             "text": f"{len(found['matches'])} past matches of this shape: {chosen['up']} of {chosen['cases']} were "
                                     f"higher {chosen['label']} later (median {chosen['median_pct']:+.1f}%).",
                             "scenarios": chosen.get("scenarios")})
        else:
            readings.append({"key": "patterns", "label": "Similar patterns", "direction": 0, "frequency": None,
                             "cases": 0, "enough": False, "text": found.get("note") or "No matches."})
        # 2. A top or bottom forming, from the daily reversal engine with its past record on this stock.
        try:
            rev = self.analyst.reversal(symbol)
            conf = rev.get("confirmed") or {}
            lead = conf.get("leading")
            side = conf.get(lead) if lead in ("bottom", "top") else None
            stage = (side or {}).get("stage")
            d = 0
            past_text = ""
            if stage in ("CANDIDATE", "CONFIRMED") and lead:
                d = 1 if lead == "bottom" else -1
                key = f"reversal:{lead}_{'candidate' if stage == 'CANDIDATE' else 'confirmed'}"
                for sig in (rev.get("past") or {}).get("signals") or []:
                    if sig["key"] == key:
                        mine = next((s for s in sig["this_stock"] if s["horizon"] == 10), None)
                        if mine and mine["cases"]:
                            past_text = (f" On this stock, past {sig['label'].lower()} readings were followed by a "
                                         f"{'rise' if d > 0 else 'fall'} two weeks later {mine['frequency']:.0%} of the time "
                                         f"({mine['cases']} cases).")
                            readings.append({"key": "reversal", "label": f"{'Bottom' if d > 0 else 'Top'} forming ({stage.lower()})",
                                             "direction": d, "frequency": mine["frequency"], "low": mine["frequency_low"],
                                             "high": mine["frequency_high"], "cases": mine["cases"],
                                             "why": f"a {lead} {stage.lower()}",
                                             "text": (conf.get("headline") or "") + past_text})
                            break
                else:
                    readings.append({"key": "reversal", "label": f"{'Bottom' if d > 0 else 'Top'} forming ({stage.lower()})",
                                     "direction": d, "frequency": None, "cases": 0, "enough": False,
                                     "text": conf.get("headline") or ""})
            else:
                readings.append({"key": "reversal", "label": "Top or bottom forming", "direction": 0, "frequency": None,
                                 "cases": 0, "enough": False,
                                 "text": conf.get("headline") or "Neither a top nor a bottom is forming on the daily bars."})
        except Exception as exc:                           # noqa: BLE001 - the engine needs 80 daily bars
            readings.append({"key": "reversal", "label": "Top or bottom forming", "direction": 0, "frequency": None,
                             "cases": 0, "enough": False, "text": f"Not read ({exc.__class__.__name__})."})
        # 3. Recent signals and their past record on this stock.
        try:
            sig = self.analyst.signals(symbol)
            for s in [x for x in sig.get("signals") or [] if isinstance(x, dict)][:4]:
                summary = (s.get("history") or {}).get("this_stock") or {}
                if not summary.get("cases"):
                    continue
                d = int(s.get("direction") or 0)
                readings.append({"key": f"signal:{s.get('key')}", "label": f"Signal: {s.get('label') or s.get('key')}",
                                 "direction": d, "frequency": summary.get("frequency"), "low": summary.get("frequency_low"),
                                 "high": summary.get("frequency_high"), "cases": summary.get("cases"),
                                 "why": f"the {s.get('label') or s.get('key')} signal",
                                 "text": f"Fired {s.get('date') or 'recently'}; on this stock it went its way two weeks later "
                                         f"{summary['frequency']:.0%} of the time ({summary['cases']} cases)."})
        except Exception:                                  # noqa: BLE001 - signals are a bonus
            pass
        # 4. The news: is today's move one with news behind it, and how did such moves on this stock go on?
        readings.append(self._news_reading(symbol, q))
        # One bar for every reading: MIN_CASES cases to vote, and the vote's way at least 55% of them. Each
        # reading's `frequency` is how often its `direction` followed; `enough` is the same test for all.
        for r in readings:
            r["enough"] = bool(r.get("cases")) and int(r["cases"]) >= similar_engine.MIN_CASES
            r["votes"] = bool(r["enough"] and r.get("direction") and (r.get("frequency") or 0) >= 0.55)
        votes = [(r["key"], int(r["direction"]), r.get("why") or r["label"]) for r in readings if r["votes"]]
        # The lean: a simple count of the readings that vote, never more than that.
        up = sum(1 for _, d, _ in votes if d > 0)
        down = sum(1 for _, d, _ in votes if d < 0)
        lean = "higher" if up > down else "lower" if down > up else "no lean"
        why = [w for _, d, w in votes if (d > 0) == (lean == "higher")] if lean != "no lean" else []
        return {"symbol": symbol, "group": group_key, "price": price, "horizon": chosen, "horizons": horizons,
                "readings": readings, "lean": {"direction": lean, "for": up if lean == "higher" else down,
                                               "against": down if lean == "higher" else up, "of": len(votes),
                                               "why": why},
                "window": found["window"], "matches": len(found["matches"]), "similar": found,
                "note": ("Each reading is how often the same thing on this stock was followed by a rise or a fall, "
                         "with a 95% interval; readings with fewer than 10 cases carry no vote. The lean counts the "
                         "readings, nothing more. None of this is a forecast, and no order is placed.")}

    def _news_reading(self, symbol: str, q: dict[str, Any]) -> dict[str, Any]:
        news = getattr(self.svc, "news", None)
        items: list[dict[str, Any]] = []
        tone = None
        try:
            if news is not None:
                got = news.company(symbol, days=3)
                since = datetime.now(timezone.utc) - timedelta(hours=36)
                for it in got.get("items") or []:
                    try:
                        when = datetime.fromisoformat(str(it.get("published")).replace("Z", "+00:00"))
                        when = when if when.tzinfo else when.replace(tzinfo=timezone.utc)
                    except ValueError:
                        continue
                    if when >= since:
                        items.append(it)
                tones = [it["tone"] for it in items if isinstance(it.get("tone"), (int, float))]
                tone = float(np.mean(tones)) if tones else None
        except Exception:                                  # noqa: BLE001
            pass
        for it in items:
            if not str(it.get("url") or "").startswith(("http://", "https://")):
                it["url"] = None
        reading: dict[str, Any] = {"key": "news", "label": "News", "direction": 0, "frequency": None, "cases": 0,
                                   "enough": False, "items": items[:5]}
        # Today's move, in this stock's daily ranges.
        try:
            daily = self.svc.market.bars(symbol, 5.0, complete=True)
            f = features_of(daily)
            last, prev = q.get("last") or q.get("close"), q.get("prevclose")
            atr = float(f["atr"][-1]) if f is not None and np.isfinite(f["atr"][-1]) else None
            if last and prev and atr and f is not None:
                ranges = (float(last) - float(prev)) / atr
                pct = 100 * (float(last) / float(prev) - 1)
                d = 1 if ranges > 0 else -1
                reading["move"] = {"ranges": round(ranges, 2), "pct": round(pct, 2)}
                if abs(ranges) >= MOVE_MIN_RANGES:
                    over, why = overextended(f, len(f["close"]) - 1, d)
                    rec = past_record({symbol: f}, move_direction=d, min_ranges=MOVE_MIN_RANGES, need_overext=False,
                                      trade_direction=d, horizons=(1, 3, 5))
                    h3 = rec["horizons"].get("3")
                    with_news = bool(items)
                    reading["text"] = (f"Today's move is {abs(ranges):.1f} daily ranges {'up' if d > 0 else 'down'} "
                                       f"({pct:+.1f}%), {'with' if with_news else 'without'} news found in the last 36 hours"
                                       + (", and the stock was already overextended" if over else "") + ".")
                    if h3 and h3["cases"]:
                        cont = h3["frequency"]
                        reading["text"] += (f" On this stock, moves this size {'up' if d > 0 else 'down'} kept going three "
                                            f"days later {cont:.0%} of the time ({h3['cases']} cases); moves with news tend to "
                                            f"keep going and moves without it to snap back, which the past record alone "
                                            f"cannot tell apart.")
                        # The reading's way: with news, the move's (how often it kept going); without, the other
                        # (how often it snapped back).
                        if with_news:
                            reading.update(frequency=cont, low=h3["low"], high=h3["high"], direction=d,
                                           why="a move with news behind it")
                        else:
                            reading.update(frequency=1 - cont, low=1 - h3["high"], high=1 - h3["low"], direction=-d,
                                           why="a move with no news found")
                        reading.update(cases=h3["cases"])
                else:
                    reading["text"] = (f"No big move today ({pct:+.1f}%, {abs(ranges):.1f} ranges). "
                                       + (f"{len(items)} headline{'s' if len(items) != 1 else ''} in the last 36 hours"
                                          + (f", tone {'positive' if tone > 0.2 else 'negative' if tone < -0.2 else 'mixed'}." if tone is not None else ".")
                                          if items else "No news found in the last 36 hours."))
            else:
                reading["text"] = "Today's move could not be measured."
        except Exception as exc:                           # noqa: BLE001
            reading["text"] = f"Today's move could not be measured ({exc.__class__.__name__})."
        reading["tone"] = tone
        return reading


__all__ = ["ChartView", "RequestError", "MAX_BARS", "NEWS_DAYS_MAX"]
