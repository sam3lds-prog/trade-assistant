"""What Model Lab, Discover and Research show from a run's ledger (4.0, phase 5).

- `learning_curve`: month by month, the champion's gain over "no change", the band's coverage and which family
  was champion, so it is visible when learning helped and when it stopped helping.
- `errors`: the largest misses, and the error by market regime and by stock.
- `symbol_record`: how forecasts of one stock have fared.
- `why_changed`: two consecutive frozen forecasts of a stock compared: whether the model changed, which inputs moved
  most, and for a linear model exactly how much each input moved the forecast. For trees no attribution is claimed;
  only the inputs that moved most are listed.
"""

from __future__ import annotations

import json
import math
from typing import Any

import pandas as pd

from ..data.features import FACTORS, FEATURES
from .metrics import daily_scores


def _df(store: Any, sql: str, params: tuple) -> pd.DataFrame:
    rows = store.query(sql, params)
    return pd.DataFrame([dict(r) for r in rows]) if rows else pd.DataFrame()


def learning_curve(store: Any, run_id: str, horizon: int) -> list[dict[str, Any]]:
    d = _df(store, """SELECT p.session, p.family, p.pred_return, o.realized_return, o.covered, o.direction_hit
                      FROM predictions p JOIN outcomes o ON o.prediction_id = p.id
                      WHERE p.run_id = ? AND p.task = 'daily' AND p.horizon = ? AND o.status = 'graded'""", (run_id, horizon))
    if not len(d):
        return []
    d["month"] = d["session"].str[:7]
    out = []
    for month, g in d.groupby("month"):
        s = daily_scores(g, horizon)
        fam = g["family"].value_counts()
        out.append({"month": month, "graded": s["graded"], "gain_bp": s["gain_bp"], "mae_pct": s["mae_pct"],
                    "nochange_mae_pct": s["nochange_mae_pct"], "coverage": s["coverage"],
                    "directional_accuracy": s["directional_accuracy"], "family": fam.index[0], "families": fam.to_dict()})
    return out


def errors(store: Any, run_id: str, horizon: int, limit: int = 40) -> dict[str, Any]:
    d = _df(store, """SELECT p.session, p.symbol, p.family, p.regime, p.pred_return, p.low_return, p.high_return, o.realized_return,
                      o.abs_error, o.covered FROM predictions p JOIN outcomes o ON o.prediction_id = p.id
                      WHERE p.run_id = ? AND p.task = 'daily' AND p.horizon = ? AND o.status = 'graded'""", (run_id, horizon))
    if not len(d):
        return {"largest": [], "by_regime": {}, "by_symbol": {}}
    d["nochange_err"] = d["realized_return"].abs()
    d["outside"] = d["covered"] == 0
    largest = d.sort_values("abs_error", ascending=False).head(limit)
    by_regime = {r: {"graded": int(len(g)), "mae_pct": float(100 * g["abs_error"].mean()), "nochange_mae_pct": float(100 * g["nochange_err"].mean()),
                     "outside_band": float(g["outside"].mean())} for r, g in d.groupby("regime")}
    sym = d.groupby("symbol").agg(graded=("abs_error", "size"), mae=("abs_error", "mean"), nochange=("nochange_err", "mean"))
    sym["gain_bp"] = 1e4 * (sym["nochange"] - sym["mae"])
    sym = sym[sym["graded"] >= 30].sort_values("gain_bp")
    def rows(frame: pd.DataFrame) -> list[dict[str, Any]]:
        return [{"symbol": s, "graded": int(r["graded"]), "gain_bp": float(r["gain_bp"]), "mae_pct": float(100 * r["mae"])}
                for s, r in frame.iterrows()]
    return {"largest": largest.drop(columns=["nochange_err", "outside"]).to_dict("records"), "by_regime": by_regime,
            "by_symbol": {"worst": rows(sym.head(10)), "best": rows(sym.tail(10).iloc[::-1])}}


def symbol_record(store: Any, run_id: str, symbol: str) -> dict[str, Any]:
    out: dict[str, Any] = {"symbol": symbol, "daily": {}, "trade": {}}
    d = _df(store, """SELECT p.horizon, p.session, p.pred_return, o.realized_return, o.direction_hit, o.covered
                      FROM predictions p JOIN outcomes o ON o.prediction_id = p.id
                      WHERE p.run_id = ? AND p.symbol = ? AND p.task = 'daily' AND o.status = 'graded'""", (run_id, symbol))
    if len(d):
        for h, g in d.groupby("horizon"):
            out["daily"][str(h)] = daily_scores(g, int(h))
    t = _df(store, """SELECT o.net_return, d.take FROM predictions p JOIN outcomes o ON o.prediction_id = p.id
                      JOIN trade_decisions d ON d.prediction_id = p.id
                      WHERE p.run_id = ? AND p.symbol = ? AND p.task = 'trade' AND o.status = 'graded'""", (run_id, symbol))
    if len(t):
        out["trade"] = {"setups": int(len(t)), "mean_net": float(t["net_return"].mean()),
                        "taken": int(t["take"].sum()), "taken_mean_net": float(t.loc[t["take"] == 1, "net_return"].mean()) if t["take"].any() else None}
    return out


def latest(store: Any, run_id: str, symbol: str | None = None) -> list[dict[str, Any]]:
    """The newest session's frozen forecasts (all stocks, or one), with their decisions."""
    r = store.one("SELECT MAX(session) AS s FROM predictions WHERE run_id = ?", (run_id,))
    if r is None or r["s"] is None:
        return []
    sql = """SELECT p.id, p.task, p.horizon, p.symbol, p.session, p.cutoff_at, p.target_session, p.model_version, p.family, p.last_close, p.pred_return,
             p.low_return, p.high_return, p.p_up, p.sigma, p.setup, p.p_target, p.p_stop, p.p_timeout, p.expected_return, p.regime,
             d.take, d.reason FROM predictions p LEFT JOIN trade_decisions d ON d.prediction_id = p.id WHERE p.run_id = ? AND p.session = ?"""
    args: list[Any] = [run_id, r["s"]]
    if symbol:
        sql += " AND p.symbol = ?"
        args.append(symbol)
    return [dict(x) for x in store.query(sql + " ORDER BY p.task, p.horizon, p.symbol", args)]


def why_changed(store: Any, run_id: str, symbol: str, horizon: int, model: Any = None) -> dict[str, Any]:
    rows = store.query("""SELECT id, session, model_version, family, pred_return, p_up, sigma, payload_json FROM predictions
                          WHERE run_id = ? AND symbol = ? AND task = 'daily' AND horizon = ? ORDER BY session DESC LIMIT 2""",
                       (run_id, symbol, horizon))
    if len(rows) < 2:
        return {"symbol": symbol, "horizon": horizon, "text": "There is only one frozen forecast of this stock so far: nothing to compare."}
    new, old = dict(rows[0]), dict(rows[1])
    pn, po = json.loads(new["payload_json"]), json.loads(old["payload_json"])
    out: dict[str, Any] = {"symbol": symbol, "horizon": horizon, "from": old["session"], "to": new["session"],
                           "before_pct": 100 * (old["pred_return"] or 0), "after_pct": 100 * (new["pred_return"] or 0),
                           "model_changed": new["model_version"] != old["model_version"],
                           "models": [old["model_version"], new["model_version"]], "inputs": [], "attribution": None}
    xn, xo = pn.get("x"), po.get("x")
    if not xn or not xo:
        out["text"] = "These forecasts were made without their inputs kept (replay forecasts keep only a hash of them)."
        return out
    moves = []
    for k in FEATURES + FACTORS:
        a, b = xo.get(k), xn.get(k)
        if a is None or b is None or not (math.isfinite(a) and math.isfinite(b)):
            continue
        moves.append({"feature": k, "before": a, "after": b, "change": b - a})
    if model is not None and getattr(model, "family", None) in ("ridge", "ridge_f") and not out["model_changed"]:
        sigma = new["sigma"] or 0.0
        cols = list(getattr(model, "columns", FEATURES))
        moves = [m for m in moves if m["feature"] in cols]
        for m in moves:
            i = cols.index(m["feature"])
            m["effect_pct"] = 100 * float(model.coef[i]) * m["change"] / float(model.scale[i]) * sigma
        moves.sort(key=lambda m: -abs(m.get("effect_pct", 0.0)))
        out["attribution"] = "exact (a linear model: each input's change times its weight)"
    else:
        scale = {m["feature"]: abs(m["before"]) + abs(m["after"]) + 1e-9 for m in moves}
        moves.sort(key=lambda m: -abs(m["change"]) / scale[m["feature"]])
        out["attribution"] = None if not out["model_changed"] else "the model itself changed between the two forecasts"
    out["inputs"] = moves[:8]
    parts = [f"The {horizon}-day forecast moved from {out['before_pct']:+.2f}% to {out['after_pct']:+.2f}%."]
    if out["model_changed"]:
        parts.append(f"The model was retrained or replaced in between ({old['model_version']} to {new['model_version']}).")
    if out["attribution"] and out["attribution"].startswith("exact"):
        top = moves[0] if moves else None
        if top:
            parts.append(f"The largest contribution: {top['feature']} ({top['effect_pct']:+.2f} points).")
    elif moves:
        parts.append("The inputs that moved most are listed; for a tree model no single input can be credited with the change.")
    out["text"] = " ".join(parts)
    return out


__all__ = ["learning_curve", "errors", "symbol_record", "latest", "why_changed"]
