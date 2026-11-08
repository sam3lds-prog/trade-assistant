"""Shadow forecasts: the deployed models, run every trading day on live data, frozen before the future happens (4.0,
phase 4).

No replay, however careful, substitutes for a record of forecasts saved before their outcomes existed. After each
session's close, `shadow_step`:

1. builds the same features as the replay from the last two years of finished daily bars, ending at the last
   settled session T (the function it is handed fetches them; this package never touches a data source);
2. grades every earlier shadow forecast whose window had closed by T;
3. freezes one forecast per horizon for every eligible stock, and a target/stop/timeout forecast and a take/pass
   decision for every setup, with the active deployment's models, the same code the replay runs (runner.predict).

It never retrains: models change only by deploying a newer run. A session is predicted once; asking again does
nothing. A session whose next open has already passed is not predicted at all: a forecast made after the market
has moved on is not a forecast, so a computer that slept through an evening leaves a gap in the record, not a
backdated entry. The feature values are kept with each shadow forecast, for "why did it change".
"""

from __future__ import annotations

import json
from datetime import date, datetime, timedelta, timezone
from typing import Any, Callable

import pandas as pd

from .. import marketdays
from ..optionmath import NY
from .config import ReplayConfig
from .data.calendar import Calendar
from .data.dataset import BAR_COLUMNS, Dataset, _clean
from .data.features import build_panel
from .data.universe import MARKET, Universe
from .learning import deploy as DEP
from .replay.clock import AsOf
from .replay.labels import LabelBook
from .replay.runner import grade, predict

Fetch = Callable[[str, str, str], pd.DataFrame]
LIVE_YEARS = 2.2


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def universe_of(manifest: dict[str, Any]) -> Universe:
    u = manifest["universe"]
    return Universe(name=u["name"], stocks=tuple(u["stocks"]), context=tuple(u["context"]),
                    survivorship_biased=bool(u.get("survivorship_biased", True)), note=u.get("note", ""))


def live_dataset(fetch: Fetch, universe: Universe, last: date, years: float = LIVE_YEARS) -> Dataset:
    """The latest finished bars, ending at session `last`, shaped like a frozen dataset (but not stored)."""
    first = last - timedelta(days=int(years * 365.25))
    bars, checks = {}, []
    for sym in universe.symbols:
        try:
            df = _clean(fetch(sym, first.isoformat(), last.isoformat()))
        except Exception as exc:  # noqa: BLE001 - one stock failing leaves it out today, said in the result
            checks.append({"symbol": sym, "level": "warn", "code": "FETCH_FAILED", "text": f"{sym}: {str(exc)[:160]}"})
            continue
        df = df[(df["date"] >= pd.Timestamp(first)) & (df["date"] <= pd.Timestamp(last))].reset_index(drop=True)
        if len(df):
            bars[sym] = df[BAR_COLUMNS]
    if MARKET not in bars:
        raise RuntimeError(f"No {MARKET} bars: the shadow forecast needs the market's trading days")
    cal = Calendar(d.date() for d in bars[MARKET]["date"])
    return Dataset(id=f"live-{last.isoformat()}", universe=universe, bars=bars, calendar=cal, meta={"source": "live"}, checks=checks)


def next_open(session: date) -> datetime:
    nxt = marketdays.next_trading_day(session)
    return datetime.combine(nxt, marketdays.OPEN, NY)


def shadow_run(store: Any, dep: dict[str, Any]) -> str:
    """The deployment's perpetual shadow run (created the first time)."""
    if dep.get("shadow_run"):
        return dep["shadow_run"]
    cfg = ReplayConfig.from_dict(dep["manifest"]["config"]).with_(kind="shadow")
    run_id = "shadow-" + dep["id"].removeprefix("dep-")
    today = datetime.now(NY).date().isoformat()
    with store.tx() as cur:
        cur.execute("""INSERT INTO replay_runs (id, kind, dataset_id, parent_run, created_at, config_json, config_sha256, status,
                       first_session, last_session) VALUES (?, 'shadow', NULL, NULL, ?, ?, ?, 'running', ?, '2099-12-31')""",
                    (run_id, _now(), json.dumps(cfg.to_dict()), cfg.sha256(), today))
        cur.execute("UPDATE deployments SET shadow_run = ? WHERE id = ?", (run_id, dep["id"]))
    return run_id


def shadow_step(store: Any, fetch: Fetch, settled: date, now: datetime | None = None) -> dict[str, Any]:
    """Grade what has matured and forecast session `settled` (the last session whose daily bar is final)."""
    now = (now or datetime.now(timezone.utc)).astimezone(NY)
    dep = DEP.active(store)
    if dep is None:
        return {"skipped": "no deployment: deploy a finished run first (python run.py --replay deploy RUN)"}
    if not marketdays.is_trading_day(settled):
        settled = marketdays.previous_trading_day(settled)
    run_id = shadow_run(store, dep)
    manifest = dep["manifest"]
    cfg = ReplayConfig.from_dict(manifest["config"]).with_(kind="shadow")
    ds = live_dataset(fetch, universe_of(manifest), settled)
    t = ds.calendar.last
    out: dict[str, Any] = {"deployment": dep["id"], "run": run_id, "session": t.isoformat(), "graded": 0, "predicted": None,
                           "notes": [c["text"] for c in ds.checks][:10]}
    if t < settled:
        out["notes"].append(f"The source has no bar for {settled} yet: waiting for it.")
    panel = build_panel(ds)
    labels = LabelBook(panel, ds.calendar, cfg.horizons, cfg.trade)
    view = AsOf(panel, labels, ds.calendar, t)
    state = DEP.models(store, dep["id"])
    with store.tx() as cur:
        out["graded"] = grade(cur, run_id, view, labels, final=False)
        cur.execute("SELECT COUNT(*) FROM predictions WHERE run_id = ? AND session = ?", (run_id, t.isoformat()))
        already = cur.fetchone()[0] > 0
        if already:
            out["predicted"] = "already"
        elif now >= next_open(t):
            out["predicted"] = "missed"
            out["notes"].append(f"{t}'s forecast was not made before the next open ({next_open(t):%a %H:%M} New York): "
                                "it is left out of the record rather than made late.")
        else:
            out["predicted"] = predict(cur, run_id, view, ds.calendar, cfg, state, keep_features=True,
                                       extra={"made_at": now.isoformat(timespec="seconds"), "deployment": dep["id"]})
        cur.execute("UPDATE replay_runs SET checkpoint = MAX(COALESCE(checkpoint, ''), ?), heartbeat_at = ?, worker = ? WHERE id = ?",
                    (t.isoformat(), _now(), "shadow", run_id))
    return out


def after_close(now: datetime) -> bool:
    """Whether today's session has closed and its bar has had time to settle (the shadow job's window)."""
    ny = now.astimezone(NY)
    s = marketdays.session(ny.date())
    if s is None:
        return True
    return ny >= s[1] + timedelta(minutes=45)


__all__ = ["shadow_step", "shadow_run", "live_dataset", "next_open", "after_close", "universe_of", "LIVE_YEARS"]
