"""One forecast, validated before it is trusted (3.0 Research).

2.x could show three competing lines on the chart: the similar-pattern median, the what-if factor model and an
AI's own path. 3.0 draws one base forecast with an uncertainty band, plus clearly labelled alternative paths
(an earnings beat, a guidance cut, a market sell-off), and says how reliable forecasts of this kind have been.

The base forecast combines the readings of separate engines at the horizon:

- **analog**: the weighted median of what followed the most similar past situations (analogs.py);
- **momentum**: the last twenty days' return carried forward;
- **mean reversion**: the pull back toward the 50-day average;
- **drift**: the stock's own average daily return over the last year.

Their weights are *learned*, not averaged: a walk-forward test over many stocks and dates fits them on the earlier
70% of dates (ridge regression that shrinks toward "no move") and judges them on the later 30%, which they never
saw. The band's width is fitted the same way so that an 80% band contains about 80% of later outcomes. If the
combined median does no better than assuming no move out of sample, the weights are set to zero and the base path
is flat: the forecast says so instead of drawing a direction it cannot support. Probabilities are never shown for
the alternative paths: they are assumptions to test against, not predictions.

Options set the width when they can (implied volatility, and the move priced for an earnings release); without
them the stock's realised volatility does. The AI never draws a path: it can explain, find evidence and challenge
the thesis (research study), and the numbers are computed here.
"""

from __future__ import annotations

import math
from datetime import date
from typing import Any, Callable, Sequence

import numpy as np
import pandas as pd

from . import analogs as A

MODEL_VERSION = "forecast-3.0"
COMPONENTS = ("analog", "momentum", "meanrev", "drift")
COMPONENT_LABELS = {"analog": "Similar past situations", "momentum": "Recent trend carried forward",
                    "meanrev": "Pull back toward the 50-day average", "drift": "The stock's own average drift"}
Z80 = 1.2815516
NOMINAL = 0.80
COVERAGE_TOLERANCE = 0.07
MIN_TEST_CASES = 30


def components_at(target: A.Series, pool: Sequence[A.Series], i: int, horizon: int, *, with_analogs: bool = True) -> dict[str, Any]:
    """Each engine's reading (a fraction) at day `i` of `target`, from data up to day i only."""
    f = target.feats
    ret1 = f["ret1"].iloc[max(1, i - 249): i + 1].to_numpy(dtype=float)
    ret1 = ret1[np.isfinite(ret1)]
    drift = float(ret1.mean() * horizon) if len(ret1) >= 60 else 0.0
    ret20, dist50, rvol = (float(f.at[i, k]) if np.isfinite(f.at[i, k]) else 0.0 for k in ("ret20", "dist50", "rvol"))
    out: dict[str, Any] = {"momentum": ret20 * horizon / 20.0, "meanrev": -dist50 * horizon / 50.0, "drift": drift,
                           "analog": 0.0, "analog_cases": 0, "sigma": rvol * math.sqrt(horizon) if rvol > 0 else None,
                           "regime": A.regime_of(f.iloc[i])}
    if with_analogs:
        found = A.search(target, pool, horizon=horizon, as_of=i, limit=20)
        h = found["summary"].get("by_horizon", {}).get(str(horizon))
        if h and h["independent"] >= 5:
            out["analog"] = h["median_pct"] / 100.0
            out["analog_cases"] = int(h["independent"])
        out["analog_search"] = found
    return out


def _ridge(x: np.ndarray, y: np.ndarray, strength: float = 0.25) -> np.ndarray:
    """Least squares shrunk toward zero (toward "no move"), the shrinkage in proportion to the inputs' scale."""
    xtx = x.T @ x
    lam = strength * np.trace(xtx) / max(1, x.shape[1])
    return np.linalg.solve(xtx + lam * np.eye(x.shape[1]), x.T @ y)


def _metrics(y: np.ndarray, mu: np.ndarray, s: np.ndarray, k: float) -> dict[str, Any]:
    z = np.abs(np.log1p(y) - np.log1p(mu)) / s
    sized = np.abs(mu) > 0.25 * s
    hit = (np.sign(mu[sized]) == np.sign(y[sized])) if sized.any() else np.array([])
    return {"cases": int(len(y)), "coverage": float((z <= Z80 * k).mean()) if len(y) else None,
            "mae_pct": float(100 * np.abs(y - mu).mean()) if len(y) else None,
            "baseline_mae_pct": float(100 * np.abs(y).mean()) if len(y) else None,
            "directional_accuracy": float(hit.mean()) if len(hit) else None, "directional_cases": int(len(hit))}


def fit(rows: pd.DataFrame, horizon: int, split: float = 0.7) -> dict[str, Any]:
    """Weights and band width from the earlier dates; every figure reported is from the later ones."""
    rows = rows.dropna(subset=["realized", "sigma"]).sort_values("date").reset_index(drop=True)
    rows = rows[rows["sigma"] > 0]
    out: dict[str, Any] = {"model_version": MODEL_VERSION, "horizon_days": horizon, "interval": NOMINAL, "weights": {c: 0.0 for c in COMPONENTS},
                           "band_k": 1.0, "status": "insufficient_data", "cases": int(len(rows)), "by_regime": {}, "text": ""}
    if len(rows) < MIN_TEST_CASES * 2:
        out["text"] = f"{len(rows)} historical forecasts could be checked; at least {MIN_TEST_CASES * 2} are needed."
        return out
    dates = np.sort(rows["date"].unique())
    cut = dates[int(len(dates) * split)]
    train, test = rows[rows["date"] < cut], rows[rows["date"] >= cut]
    x_tr, y_tr = train[list(COMPONENTS)].to_numpy(float), train["realized"].to_numpy(float)
    w = _ridge(x_tr, y_tr)
    mu_tr = x_tr @ w
    z_tr = np.abs(np.log1p(y_tr) - np.log1p(mu_tr)) / train["sigma"].to_numpy(float)
    k = float(np.quantile(z_tr, NOMINAL) / Z80)
    x_te, y_te, s_te = test[list(COMPONENTS)].to_numpy(float), test["realized"].to_numpy(float), test["sigma"].to_numpy(float)
    mu_te = x_te @ w
    m = _metrics(y_te, mu_te, s_te, k)
    flat = _metrics(y_te, np.zeros_like(y_te), s_te, float(np.quantile(np.abs(np.log1p(y_tr)) / train["sigma"].to_numpy(float), NOMINAL) / Z80))
    # The median must beat "no move" by more than chance: the gain in absolute error, averaged per date (the same day
    # across stocks is one market event, not many), must be positive with one-sided 95% confidence.
    gain = pd.Series(np.abs(y_te) - np.abs(y_te - mu_te)).groupby(test["date"].to_numpy()).mean()
    t_stat = float(gain.mean() / (gain.std(ddof=1) / math.sqrt(len(gain)))) if len(gain) > 2 and gain.std(ddof=1) > 0 else 0.0
    directional = bool(t_stat > 1.645)
    m["edge_t"] = t_stat
    if not directional:
        w = np.zeros_like(w)                                      # no direction it can support out of sample
        m = flat
        k = float(np.quantile(np.abs(np.log1p(y_tr)) / train["sigma"].to_numpy(float), NOMINAL) / Z80)
    out.update({"weights": {c: float(v) for c, v in zip(COMPONENTS, w)}, "band_k": k, "train_cases": int(len(train)),
                "test_cases": int(len(test)), "split_date": str(pd.Timestamp(cut).date()), "directional": bool(directional), **m})
    for regime, sub in test.groupby("regime"):
        if len(sub) >= 10:
            out["by_regime"][str(regime)] = _metrics(sub["realized"].to_numpy(float), sub[list(COMPONENTS)].to_numpy(float) @ w,
                                                     sub["sigma"].to_numpy(float), k)
    cov = m["coverage"]
    if len(test) < MIN_TEST_CASES:
        out["status"] = "insufficient_data"
    elif abs(cov - NOMINAL) <= COVERAGE_TOLERANCE:
        out["status"] = "calibrated"
    else:
        out["status"] = "not_calibrated"
    parts = [f"Checked on {len(test)} later forecasts it never saw ({len(train)} earlier ones fitted it)."]
    parts.append(f"The 80% band held {100 * cov:.0f}% of outcomes.")
    if directional:
        acc = m["directional_accuracy"]
        parts.append(f"The median was {m['mae_pct']:.2f} points off on average against {flat['mae_pct']:.2f} for 'no move'"
                     + (f"; it had the direction right {100 * acc:.0f}% of the time it leaned." if acc is not None else "."))
    else:
        parts.append("Its median did not beat assuming no move by more than chance, so the base path is flat: the evidence "
                     "below may still lean, but the forecast does not claim a direction.")
    out["text"] = " ".join(parts)
    return out


def walk_forward(prepared: dict[str, A.Series], peers: dict[str, Sequence[str]], horizon: int, *, step: int = 5, start: int = 260,
                 progress: Callable[[int, int, str], None] | None = None) -> pd.DataFrame:
    """Forecast every `step` days on every stock from data up to that day, and record what followed."""
    rows: list[dict[str, Any]] = []
    total = len(prepared)
    for n, (sym, target) in enumerate(prepared.items(), 1):
        if progress:
            progress(n, total, sym)
        pool = [target] + [prepared[p] for p in peers.get(sym, []) if p in prepared and p != sym]
        f = target.feats
        for i in range(start, len(f) - horizon, step):
            comp = components_at(target, pool, i, horizon)
            if comp["sigma"] is None:
                continue
            rows.append({"symbol": sym, "date": f.at[i, "date"], "realized": float(f.at[i + horizon, "close"] / f.at[i, "close"] - 1),
                         **{c: comp[c] for c in COMPONENTS}, "sigma": comp["sigma"], "regime": comp["regime"]})
    return pd.DataFrame(rows)


def calibration_contract(cal: dict[str, Any] | None, horizon: int) -> dict[str, Any]:
    if not cal:
        return {"status": "not_calibrated", "horizon_days": horizon, "cases": 0, "interval": NOMINAL,
                "text": "Not checked yet: run the calibration (Research, Forecast) to test this kind of forecast on history."}
    if cal.get("horizon_days") != horizon:
        return {"status": "not_calibrated", "horizon_days": horizon, "cases": 0, "interval": NOMINAL,
                "text": f"The calibration was run for {cal.get('horizon_days')} trading days, not {horizon}: run it for this horizon."}
    return {"status": cal["status"], "horizon_days": horizon, "cases": int(cal.get("test_cases") or cal.get("cases") or 0), "interval": NOMINAL,
            "coverage": cal.get("coverage"), "directional_accuracy": cal.get("directional_accuracy"), "mae_pct": cal.get("mae_pct"),
            "baseline_mae_pct": cal.get("baseline_mae_pct"), "by_regime": cal.get("by_regime") or {}, "text": cal.get("text") or ""}


def forecast(target: A.Series, pool: Sequence[A.Series], horizon: int, *, calibration: dict[str, Any] | None = None,
             implied_sigma: float | None = None, event: dict[str, Any] | None = None, beta: float | None = None,
             market_sigma: float | None = None, last_price: float | None = None) -> dict[str, Any]:
    """The base forecast, its band and the alternative paths at `horizon` trading days, from today's data.

    `implied_sigma` is the options market's standard deviation of the log return to the horizon; `event` is
    {"in_days": trading days to an earnings release, "move_pct": the move options price for it}."""
    i = len(target.feats) - 1
    comp = components_at(target, pool, i, horizon)
    cal = calibration if calibration and calibration.get("horizon_days") == horizon else None
    weights = (cal or {}).get("weights") or {c: 0.0 for c in COMPONENTS}
    k = float((cal or {}).get("band_k") or 1.0)
    mu = float(sum(weights.get(c, 0.0) * comp[c] for c in COMPONENTS))
    realized = comp["sigma"] or 0.02 * math.sqrt(horizon)
    sources = [f"realised volatility ({100 * realized:.1f}% to the horizon)"]
    sigma = realized
    if implied_sigma and implied_sigma > 0:
        sigma = math.sqrt(0.5 * realized ** 2 + 0.5 * implied_sigma ** 2)
        sources.append(f"implied volatility ({100 * implied_sigma:.1f}%)")
    ev_days = ev_move = None
    if event and event.get("in_days") is not None and 0 <= event["in_days"] <= horizon and event.get("move_pct"):
        ev_days, ev_move = int(event["in_days"]), float(event["move_pct"]) / 100.0
        sources.append(f"the earnings move options price ({100 * ev_move:.1f}%)")
    path, low, high = [], [], []
    for t in range(1, horizon + 1):
        m_t = mu * t / horizon
        var = (sigma ** 2) * t / horizon + ((ev_move ** 2) if ev_move and t >= max(1, ev_days) else 0.0)
        s_t = math.sqrt(var)
        centre = math.log1p(m_t)
        path.append(100 * m_t)
        low.append(100 * (math.exp(centre - Z80 * k * s_t) - 1))
        high.append(100 * (math.exp(centre + Z80 * k * s_t) - 1))
    alternatives = []
    if ev_move:
        for key, sign, label, assumption in (("earnings_beat", 1, "Earnings beat", "the release lands well and the stock moves up by the move options price"),
                                             ("guidance_cut", -1, "Guidance cut", "the release disappoints and the stock falls by the move options price")):
            alt = [p + (100 * sign * ev_move if t >= max(1, ev_days) else 0.0) for t, p in enumerate(path, 1)]
            alternatives.append({"key": key, "label": label, "assumption": assumption, "path_pct": alt})
    if beta is not None and market_sigma:
        for key, sign, label in (("market_up", 1, "Market rally"), ("market_down", -1, "Market sell-off")):
            shock = sign * Z80 * market_sigma
            alt = [p + 100 * beta * shock * t / horizon for t, p in enumerate(path, 1)]
            alternatives.append({"key": key, "label": label, "path_pct": alt,
                                 "assumption": f"the market {'rises' if sign > 0 else 'falls'} {100 * Z80 * market_sigma:.1f}% by the horizon (a one-in-ten move) and the stock follows its beta of {beta:.2f}"})
    contributions = {c: {"label": COMPONENT_LABELS[c], "reading_pct": 100 * comp[c], "weight": float(weights.get(c, 0.0)),
                         "contribution_pct": 100 * weights.get(c, 0.0) * comp[c]} for c in COMPONENTS}
    contributions["analog"]["cases"] = comp["analog_cases"]
    explanation = []
    if not cal:
        explanation.append("No calibration has been run for this horizon, so no engine is given weight: the base path is flat "
                           "and the band is the volatility alone. Run the calibration to learn the weights from history.")
    elif not any(abs(w) > 0 for w in weights.values()):
        explanation.append("On history, the combined engines did no better than assuming no move, so the base path is flat.")
    else:
        top = sorted(contributions.items(), key=lambda kv: -abs(kv[1]["contribution_pct"]))
        explanation.append("The base path is the weighted sum of the engines' readings: " + "; ".join(
            f"{v['label'].lower()} {v['reading_pct']:+.2f}% × {v['weight']:.2f}" for _, v in top if abs(v["weight"]) > 0) + ".")
    explanation.append("The band's width comes from " + ", ".join(sources) + (f", widened by {k:.2f} so that it held 80% of outcomes on history." if cal else "."))
    if ev_move:
        explanation.append(f"An earnings release in {ev_days} trading days adds its priced move to the band from that day.")
    invalidators = []
    if last_price:
        invalidators.append(f"A close below ${last_price * (1 + low[0] / 100):.2f} on the first day, or ${last_price * (1 + low[-1] / 100):.2f} by "
                            f"the horizon: the bottom of the 80% band.")
    if ev_move:
        invalidators.append("The earnings release: a move beyond the band on the day is outside this forecast.")
    invalidators.append(f"The market's regime changing from '{comp['regime']}'.")
    return {"model_version": MODEL_VERSION, "horizon_days": horizon,
            "base": {"key": "base", "label": "Base", "assumption": "today's conditions continue; the engines' weighted readings",
                     "path_pct": path, "low_pct": low, "high_pct": high},
            "alternatives": alternatives, "components": contributions, "weights": {c: float(weights.get(c, 0.0)) for c in COMPONENTS},
            "calibration": calibration_contract(cal, horizon), "explanation": explanation, "invalidators": invalidators,
            "median_pct": path[-1], "low_pct": low[-1], "high_pct": high[-1], "sigma": sigma, "band_k": k,
            "regime": comp["regime"], "analog_search": comp.get("analog_search")}


def record_target(as_of: date, horizon: int) -> date:
    return (pd.Timestamp(as_of) + pd.offsets.BDay(horizon)).date()


__all__ = ["MODEL_VERSION", "COMPONENTS", "components_at", "fit", "walk_forward", "forecast", "calibration_contract", "record_target", "Z80"]
