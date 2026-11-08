"""The frozen dataset a replay reads (4.0).

The app's bar cache (market.py) is not this: it deletes and rewrites a stock's rows on every refresh and keeps about
three years. A replay needs bars that can never change under it, so a dataset is fetched once, written to
replay.db, and named by the hash of its content (`ds-` + 16 hex). Two runs on the same dataset id read the same
numbers, byte for byte; a re-fetch that differs in a single price is a different dataset.

Prices are stored as the source supplied them (Tradier: adjusted for splits; whether for dividends the app records
as `unknown`). An unadjusted split would show as a one-day move of more than 40%, and is reported among the checks,
as are later listings, gaps and days of zero volume.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from typing import Any, Callable

import numpy as np
import pandas as pd

from .calendar import Calendar, from_market_dates
from .universe import MARKET, Universe

BAR_COLUMNS = ["date", "open", "high", "low", "close", "volume"]
SUSPECT_MOVE = 0.40
Fetch = Callable[[str, str, str], pd.DataFrame]


class DatasetError(RuntimeError):
    pass


@dataclass
class Dataset:
    id: str
    universe: Universe
    bars: dict[str, pd.DataFrame]
    calendar: Calendar
    meta: dict[str, Any] = field(default_factory=dict)
    checks: list[dict[str, Any]] = field(default_factory=list)

    @property
    def stocks(self) -> tuple[str, ...]:
        return tuple(s for s in self.universe.stocks if s in self.bars)

    def through(self, last: date) -> "Dataset":
        """The same data as it would have stood after session `last`: everything later removed. Used to prove that
        a prediction for a session never depends on what came after it (tests/test_v4_replay.py)."""
        cut = pd.Timestamp(last)
        bars = {s: df[df["date"] <= cut].reset_index(drop=True) for s, df in self.bars.items()}
        bars = {s: df for s, df in bars.items() if len(df)}
        cal = Calendar([d for d in self.calendar.sessions if d <= last])
        return Dataset(id=f"{self.id}@{last.isoformat()}", universe=self.universe, bars=bars, calendar=cal,
                       meta={**self.meta, "through": last.isoformat()}, checks=list(self.checks))

    def has_failures(self) -> bool:
        return any(c["level"] == "fail" for c in self.checks)


def _clean(df: pd.DataFrame) -> pd.DataFrame:
    """Canonical bars: dates as days, prices to 6 decimals, whole-share volume, sorted and unique."""
    if df is None or not len(df):
        return pd.DataFrame(columns=BAR_COLUMNS)
    out = df[BAR_COLUMNS].copy()
    out["date"] = pd.to_datetime(out["date"]).dt.normalize()
    for c in ("open", "high", "low", "close"):
        out[c] = out[c].astype(float).round(6)
    out["volume"] = out["volume"].astype(float).fillna(0.0).round(0)
    out = out.dropna(subset=["open", "high", "low", "close"])
    out = out[(out["close"] > 0) & (out["open"] > 0) & (out["high"] >= out["low"])]
    return out.sort_values("date").drop_duplicates("date", keep="last").reset_index(drop=True)


def content_hash(bars: dict[str, pd.DataFrame]) -> str:
    h = hashlib.sha256()
    for sym in sorted(bars):
        df = bars[sym]
        h.update(f"#{sym}\n".encode())
        for d, o, hi, lo, c, v in df[BAR_COLUMNS].itertuples(index=False, name=None):
            h.update(f"{pd.Timestamp(d).date()},{o:.6f},{hi:.6f},{lo:.6f},{c:.6f},{int(v)}\n".encode())
    return h.hexdigest()


def assess(sym: str, df: pd.DataFrame, cal: Calendar) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    if df.empty:
        return [{"symbol": sym, "level": "warn", "code": "NO_DATA", "text": f"No bars for {sym}: it is left out."}]
    first = df["date"].iloc[0].date()
    if first > cal.first:
        out.append({"symbol": sym, "level": "info", "code": "LATER_START",
                    "text": f"{sym}'s history starts on {first}: it joins the replay once it has a year of bars."})
    moves = df["close"].pct_change().abs()
    for d in df.loc[moves > SUSPECT_MOVE, "date"].head(5):
        out.append({"symbol": sym, "level": "warn", "code": "SUSPECT_MOVE",
                    "text": f"{sym} moved more than {SUSPECT_MOVE:.0%} on {d.date()}: possibly a split the source did not adjust."})
    have = set(df["date"].dt.date)
    expected = [d for d in cal.sessions if d >= first and d <= df["date"].iloc[-1].date()]
    missing = [d for d in expected if d not in have]
    if missing:
        level = "warn" if len(missing) > 5 else "info"
        out.append({"symbol": sym, "level": level, "code": "MISSING_SESSIONS",
                    "text": f"{sym} has no bar on {len(missing)} session(s) (first {missing[0]}): it is not predicted on those days."})
    zero = int((df["volume"] <= 0).sum())
    if zero:
        out.append({"symbol": sym, "level": "info", "code": "ZERO_VOLUME", "text": f"{sym} has {zero} day(s) with zero volume."})
    return out


def build(store: Any, fetch: Fetch, universe: Universe, first: date, last: date, *, source: str, adjustment: str,
          progress: Callable[[int, int, str], None] | None = None) -> Dataset:
    """Fetch every symbol of `universe` from `first` to `last` (the last finished session) and freeze it."""
    bars: dict[str, pd.DataFrame] = {}
    failures: list[dict[str, Any]] = []
    symbols = universe.symbols
    for n, sym in enumerate(symbols, 1):
        if progress:
            progress(n, len(symbols), sym)
        try:
            raw = fetch(sym, first.isoformat(), last.isoformat())
        except Exception as exc:  # noqa: BLE001 - one symbol failing is recorded, not fatal
            failures.append({"symbol": sym, "level": "warn", "code": "FETCH_FAILED", "text": f"{sym}: {str(exc)[:200]}"})
            continue
        df = _clean(raw)
        df = df[(df["date"] >= pd.Timestamp(first)) & (df["date"] <= pd.Timestamp(last))].reset_index(drop=True)
        if len(df):
            bars[sym] = df
    return freeze(store, bars, universe, source=source, adjustment=adjustment, extra_checks=failures)


def freeze(store: Any, bars: dict[str, pd.DataFrame], universe: Universe, *, source: str, adjustment: str,
           extra_checks: list[dict[str, Any]] | None = None) -> Dataset:
    """Write bars already in hand as a dataset (build() after fetching; tests with synthetic bars)."""
    bars = {s: _clean(df) for s, df in bars.items()}
    bars = {s: df for s, df in bars.items() if len(df)}
    if MARKET not in bars:
        raise DatasetError(f"The dataset needs {MARKET}: its trading days are the replay's calendar.")
    cal, cal_problems = from_market_dates([d.date() for d in bars[MARKET]["date"]])
    if source == "demo":
        for p in cal_problems:
            p["level"] = "info"
            p["text"] += " (synthetic demo prices trade every weekday)"
    checks: list[dict[str, Any]] = [dict(p, symbol=None) for p in cal_problems] + list(extra_checks or [])
    for sym in universe.symbols:
        checks += assess(sym, bars.get(sym, pd.DataFrame(columns=BAR_COLUMNS)), cal)
    if adjustment == "unknown":
        checks.append({"symbol": None, "level": "info", "code": "ADJUSTMENT_UNKNOWN",
                       "text": "The source does not say whether its prices are adjusted for dividends. If not, each ex-dividend "
                               "day shows as a small drop (usually under 1%) that no model can foresee."})
    digest = content_hash(bars)
    ds_id = "ds-" + digest[:16]
    spec = {"universe": {"name": universe.name, "stocks": list(universe.stocks), "context": list(universe.context),
                         "survivorship_biased": universe.survivorship_biased, "note": universe.note}}
    existing = store.one("SELECT id FROM datasets WHERE content_sha256 = ?", (digest,))
    if existing is None:
        now = datetime.now(timezone.utc).isoformat(timespec="seconds")
        with store.tx() as cur:
            cur.execute("""INSERT INTO datasets (id, created_at, source, adjustment, universe, first_date, last_date, symbols_json,
                           sessions, bars, content_sha256, spec_json) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
                        (ds_id, now, source, adjustment, universe.name, cal.first.isoformat(), cal.last.isoformat(),
                         json.dumps(sorted(bars)), len(cal), int(sum(len(df) for df in bars.values())), digest, json.dumps(spec)))
            for sym in sorted(bars):
                df = bars[sym]
                cur.executemany("INSERT INTO dataset_bars (dataset_id, symbol, date, open, high, low, close, volume) VALUES (?,?,?,?,?,?,?,?)",
                                [(ds_id, sym, pd.Timestamp(d).date().isoformat(), o, h, lo, c, v)
                                 for d, o, h, lo, c, v in df[BAR_COLUMNS].itertuples(index=False, name=None)])
            cur.executemany("INSERT INTO dataset_checks (dataset_id, symbol, level, code, text) VALUES (?,?,?,?,?)",
                            [(ds_id, c.get("symbol"), c["level"], c["code"], c["text"]) for c in checks])
    return load(store, ds_id)


def load(store: Any, dataset_id: str) -> Dataset:
    row = store.one("SELECT * FROM datasets WHERE id = ?", (dataset_id,))
    if row is None:
        raise DatasetError(f"No dataset {dataset_id}")
    spec = json.loads(row["spec_json"])["universe"]
    universe = Universe(name=spec["name"], stocks=tuple(spec["stocks"]), context=tuple(spec["context"]),
                        survivorship_biased=bool(spec["survivorship_biased"]), note=spec["note"])
    frame = pd.DataFrame([dict(r) for r in store.query(
        "SELECT symbol, date, open, high, low, close, volume FROM dataset_bars WHERE dataset_id = ? ORDER BY symbol, date", (dataset_id,))])
    bars: dict[str, pd.DataFrame] = {}
    if len(frame):
        frame["date"] = pd.to_datetime(frame["date"])
        for sym, df in frame.groupby("symbol", sort=True):
            bars[str(sym)] = df[BAR_COLUMNS].reset_index(drop=True)
    if content_hash(bars) != row["content_sha256"]:
        raise DatasetError(f"Dataset {dataset_id} does not match its recorded hash: the file has been altered.")
    cal = Calendar(d.date() for d in bars[MARKET]["date"])
    checks = [dict(r) for r in store.query("SELECT symbol, level, code, text FROM dataset_checks WHERE dataset_id = ? ORDER BY id", (dataset_id,))]
    meta = {k: row[k] for k in ("created_at", "source", "adjustment", "first_date", "last_date", "sessions", "bars")}
    return Dataset(id=dataset_id, universe=universe, bars=bars, calendar=cal, meta=meta, checks=checks)


def summaries(store: Any) -> list[dict[str, Any]]:
    out = []
    for r in store.query("SELECT * FROM datasets ORDER BY created_at DESC"):
        counts = {c["level"]: c["n"] for c in store.query(
            "SELECT level, COUNT(*) AS n FROM dataset_checks WHERE dataset_id = ? GROUP BY level", (r["id"],))}
        out.append({"id": r["id"], "created_at": r["created_at"], "source": r["source"], "adjustment": r["adjustment"],
                    "universe": r["universe"], "first_date": r["first_date"], "last_date": r["last_date"],
                    "symbols": len(json.loads(r["symbols_json"])), "sessions": r["sessions"], "bars": r["bars"],
                    "checks": {"fail": counts.get("fail", 0), "warn": counts.get("warn", 0), "info": counts.get("info", 0)}})
    return out


def as_arrays(df: pd.DataFrame) -> dict[str, np.ndarray]:
    return {c: df[c].to_numpy(float) for c in ("open", "high", "low", "close", "volume")}


__all__ = ["Dataset", "DatasetError", "build", "freeze", "load", "summaries", "content_hash", "BAR_COLUMNS"]
