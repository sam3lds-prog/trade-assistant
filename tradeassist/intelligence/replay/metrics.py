"""Scorecards: what a run's frozen predictions turned out to be worth (4.0).

Daily forecasts are judged against "no change" on the same rows: the gain in absolute error averaged per date, its
Newey-West t, the band's coverage, and how often a forecast that leaned had the direction right. Trades are judged
three ways from the same setups: all of them, the ones the gate took, and the ones it passed, so the scorecard says
whether choosing beat taking everything and beat abstaining. Price accuracy and trading results are kept apart: a
model can improve one without the other.
"""

from __future__ import annotations

import json
from typing import Any

import numpy as np
import pandas as pd

from ..learning.validation import newey_west_t
from .execution import equity_stats


def _frame(store: Any, sql: str, params: tuple) -> pd.DataFrame:
    rows = store.query(sql, params)
    return pd.DataFrame([dict(r) for r in rows]) if rows else pd.DataFrame()


def daily_scores(df: pd.DataFrame, horizon: int) -> dict[str, Any]:
    if not len(df):
        return {"graded": 0}
    y, mu = df["realized_return"].to_numpy(float), df["pred_return"].to_numpy(float)
    gain = pd.Series(np.abs(y) - np.abs(y - mu)).groupby(df["session"].to_numpy()).mean()
    lean = df["direction_hit"].dropna()
    return {"graded": int(len(df)), "dates": int(len(gain)),
            "mae_pct": float(100 * np.abs(y - mu).mean()), "nochange_mae_pct": float(100 * np.abs(y).mean()),
            "gain_bp": float(1e4 * gain.mean()), "t": newey_west_t(gain.to_numpy(), max(0, horizon - 1)),
            "coverage": float(df["covered"].mean()) if df["covered"].notna().any() else None,
            "directional_accuracy": float(lean.mean()) if len(lean) else None, "directional_cases": int(len(lean))}


def scorecard(store: Any, run_id: str) -> dict[str, Any]:
    run = store.one("SELECT * FROM replay_runs WHERE id = ?", (run_id,))
    cfg = json.loads(run["config_json"])
    ds = store.one("SELECT * FROM datasets WHERE id = ?", (run["dataset_id"],)) if run["dataset_id"] else None
    spec = json.loads(ds["spec_json"])["universe"] if ds else {}
    out: dict[str, Any] = {"run": run_id, "kind": run["kind"], "dataset": run["dataset_id"],
                           "period": [run["first_session"], run["last_session"]], "daily": {}, "trade": {}, "notes": []}
    if spec.get("survivorship_biased"):
        out["notes"].append(spec.get("note") or "Survivorship-biased universe.")
    if run["kind"] == "holdout":
        opening = store.one("SELECT ordinal FROM holdout_openings WHERE run_id = ?", (run_id,))
        if opening is not None and opening["ordinal"] == 1:
            out["notes"].insert(0, "The first evaluation of the held-out year: no model or rule was chosen with these sessions in view.")
        elif opening is not None:
            out["notes"].insert(0, f"The held-out year has been opened {opening['ordinal']} times: it is no longer untouched, so this "
                                   "result is development evidence, not a final test.")
        out["holdout_opening"] = opening["ordinal"] if opening is not None else None
    if ds is not None and ds["adjustment"] in ("unknown", "split"):
        out["notes"].append("Prices may not be adjusted for dividends: ex-dividend days add small unforecastable drops.")
    d = _frame(store, """SELECT p.horizon, p.session, p.family, p.regime, p.pred_return, o.realized_return, o.direction_hit, o.covered
                         FROM predictions p JOIN outcomes o ON o.prediction_id = p.id
                         WHERE p.run_id = ? AND p.task = 'daily' AND o.status = 'graded'""", (run_id,))
    pending = {int(r["horizon"]): int(r["n"]) for r in store.query(
        """SELECT p.horizon, COUNT(*) AS n FROM predictions p LEFT JOIN outcomes o ON o.prediction_id = p.id
           WHERE p.run_id = ? AND p.task = 'daily' AND o.prediction_id IS NULL GROUP BY p.horizon""", (run_id,))}
    for h in cfg["horizons"]:
        sub = d[d["horizon"] == h] if len(d) else d
        block = daily_scores(sub, h)
        block["pending"] = pending.get(h, 0)
        if len(sub):
            sub = sub.assign(year=sub["session"].str[:4])
            block["by_year"] = {y: daily_scores(g, h) for y, g in sub.groupby("year")}
            block["by_regime"] = {r: daily_scores(g, h) for r, g in sub.groupby("regime") if len(g) >= 200}
            block["by_family"] = {f: daily_scores(g, h) for f, g in sub.groupby("family")}
        out["daily"][str(h)] = block
    t = _frame(store, """SELECT p.session, p.symbol, p.setup, p.family, p.expected_return, d.take, o.barrier, o.net_return, o.exit_session
                         FROM predictions p JOIN trade_decisions d ON d.prediction_id = p.id
                         JOIN outcomes o ON o.prediction_id = p.id
                         WHERE p.run_id = ? AND p.task = 'trade' AND o.status = 'graded'""", (run_id,))
    if len(t):
        out["trade"] = {
            "all_setups": equity_stats(t["net_return"].tolist(), t["exit_session"].tolist()),
            "taken": equity_stats(t.loc[t["take"] == 1, "net_return"].tolist(), t.loc[t["take"] == 1, "exit_session"].tolist()),
            "passed": equity_stats(t.loc[t["take"] == 0, "net_return"].tolist(), t.loc[t["take"] == 0, "exit_session"].tolist()),
            "barriers": {k: int(v) for k, v in t["barrier"].value_counts().items()},
            "by_setup": {s: equity_stats(g["net_return"].tolist(), g["exit_session"].tolist()) for s, g in t.groupby("setup")},
            "by_year": {y: {"all": equity_stats(g["net_return"].tolist(), g["exit_session"].tolist()),
                            "taken": equity_stats(g.loc[g["take"] == 1, "net_return"].tolist(), g.loc[g["take"] == 1, "exit_session"].tolist())}
                        for y, g in t.assign(year=t["session"].str[:4]).groupby("year")},
        }
        amb = int((t["barrier"] == "ambiguous").sum())
        if amb:
            out["notes"].append(f"{amb} trade(s) reached both barriers within one daily bar; each was scored as the stop.")
    promos = store.query("SELECT task, horizon, decision, COUNT(*) AS n FROM training_runs WHERE run_id = ? GROUP BY task, horizon, decision",
                         (run_id,))
    out["training"] = {}
    for r in promos:
        out["training"].setdefault(f"{r['task']}:{r['horizon']}", {})[r["decision"]] = int(r["n"])
    finals = store.query("SELECT task, horizon, model_version, family FROM run_state WHERE run_id = ?", (run_id,))
    out["champions"] = {f"{r['task']}:{r['horizon']}": {"version": r["model_version"], "family": r["family"]} for r in finals}
    out["headline"] = headline(out)
    return out


def headline(card: dict[str, Any]) -> str:
    """One sentence a person can act on: did the champion beat 'no change', and did the gate beat taking everything."""
    parts = []
    for h, b in card.get("daily", {}).items():
        if not b.get("graded"):
            continue
        word = "beat" if b["t"] > 1.645 and b["gain_bp"] > 0 else "did not beat"
        parts.append(f"{h}-day forecasts {word} 'no change' ({b['gain_bp']:+.2f} bp of error per stock-day, t = {b['t']:.2f}, "
                     f"{b['graded']:,} graded)")
    tr = card.get("trade") or {}
    if tr.get("all_setups", {}).get("trades"):
        a, k = tr["all_setups"], tr["taken"]
        if k.get("trades"):
            parts.append(f"the gate took {k['trades']:,} of {a['trades']:,} setups, averaging {100 * k['expectancy']:+.2f}% net "
                         f"against {100 * a['expectancy']:+.2f}% for taking all")
        else:
            parts.append(f"the gate took none of {a['trades']:,} setups (taking all averaged {100 * a['expectancy']:+.2f}% net)")
    if not parts:
        return "Nothing has been graded yet."
    text = "; ".join(parts) + "."
    return text[0].upper() + text[1:]


__all__ = ["scorecard", "daily_scores", "headline"]
