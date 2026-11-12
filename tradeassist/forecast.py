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

**Saying what the band is (3.1).** The band is only called an 80% band, containing 80% of outcomes, when the
calibration for that horizon and model version passed its coverage check. Otherwise it is the model-implied 10th to
90th percentile range, and the forecast says that its historical coverage has not been validated (`range_words`).

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
RANGE_WORDS = {"calibrated": "the calibrated 80% band",
               "unvalidated": "the model-implied 10th to 90th percentile range (historical coverage not yet validated)"}
NOMINAL = 0.80
COVERAGE_TOLERANCE = 0.07
MIN_TEST_CASES = 30
# 4.2: how a calibration is made. "cal-4.2" fits on the earliest 60% of dates, decides on the next 20% and reports on
# the last 20%, with outcomes that overlap the next period purged; artifacts without a method came from the 3.0 fit
# (70/30, no purge) and stay usable until a refresh replaces them.
CALIBRATION_METHOD = "cal-4.2"
SPLITS = (0.6, 0.8)
MIN_DATES = 12
HORIZON_WORDS = {1: "One-session", 3: "Three-session", 5: "Five-session", 10: "Ten-session", 20: "Twenty-session"}


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


def _label_ends(dates: np.ndarray, horizon: int) -> dict[Any, pd.Timestamp]:
    """Each forecast date's label end: the session its outcome is read at, `horizon` trading sessions later."""
    from .marketdays import add_trading_days
    out = {}
    for d in dates:
        day = pd.Timestamp(d).date()
        out[d] = pd.Timestamp(add_trading_days(day, horizon))
    return out


def _gain_t(y: np.ndarray, mu: np.ndarray, dates: np.ndarray) -> tuple[float, float, int]:
    """The median's gain in absolute error over "no move", averaged per date (one day across stocks is one market
    event, not many): its mean in percentage points, its t statistic, and the number of dates."""
    gain = pd.Series(np.abs(y) - np.abs(y - mu)).groupby(dates).mean()
    n = len(gain)
    sd = float(gain.std(ddof=1)) if n > 2 else 0.0
    t = float(gain.mean() / (sd / math.sqrt(n))) if n > 2 and sd > 0 else 0.0
    return float(100 * gain.mean()) if n else 0.0, t, n


def _band_k(y: np.ndarray, mu: np.ndarray, s: np.ndarray) -> float:
    z = np.abs(np.log1p(y) - np.log1p(mu)) / s
    return float(np.quantile(z, NOMINAL) / Z80) if len(z) else 1.0


def fit(rows: pd.DataFrame, horizon: int, split: tuple[float, float] = SPLITS) -> dict[str, Any]:
    """Weights chosen before the final holdout; every figure reported is from the holdout (4.2).

    The dates are split three ways: the earliest 60% fit the weights (ridge), the next 20% decide whether those weights
    beat "no move" (validation), and the last 20% are the final holdout the reported figures come from. A row whose
    label (its outcome, `horizon` sessions on) ends inside the next period is purged, so no fitting row has seen the
    future of the rows it is judged on. The band's width is set from the fitting and validation rows only. A direction
    is used only when it beat "no move" on the validation dates (one-sided 95%) and still beat it on the holdout;
    otherwise the weights are zero and the base path is flat."""
    rows = rows.dropna(subset=["realized", "sigma"]).sort_values("date").reset_index(drop=True)
    rows = rows[rows["sigma"] > 0].reset_index(drop=True)
    dates = np.sort(rows["date"].unique()) if len(rows) else np.array([])
    diag = {"rows": int(len(rows)), "symbols": int(rows["symbol"].nunique()) if "symbol" in rows and len(rows) else 0,
            "dates": int(len(dates)), "first_date": str(pd.Timestamp(dates[0]).date()) if len(dates) else None,
            "last_date": str(pd.Timestamp(dates[-1]).date()) if len(dates) else None}
    out: dict[str, Any] = {"model_version": MODEL_VERSION, "method": CALIBRATION_METHOD, "horizon_days": horizon, "interval": NOMINAL,
                           "weights": {c: 0.0 for c in COMPONENTS}, "band_k": 1.0, "status": "insufficient_data", "cases": int(len(rows)),
                           "by_regime": {}, "text": "", "directional": False, "reason": None, "diagnostics": diag}
    if len(rows) < MIN_TEST_CASES * 3:
        out.update(reason="too_few_cases",
                   text=f"{len(rows)} historical forecasts could be checked; at least {MIN_TEST_CASES * 3} are needed (fit, validation and a holdout).")
        return out
    if len(dates) < MIN_DATES * 3:
        out.update(reason="too_few_dates",
                   text=f"The forecasts fall on only {len(dates)} distinct dates; at least {MIN_DATES * 3} are needed, since one day across "
                        "many stocks is one market event, not many.")
        return out
    v_start, h_start = dates[int(len(dates) * split[0])], dates[int(len(dates) * split[1])]
    ends = rows["date"].map(_label_ends(dates, horizon))
    train = rows[(rows["date"] < v_start) & (ends < v_start)]
    val = rows[(rows["date"] >= v_start) & (rows["date"] < h_start) & (ends < h_start)]
    hold = rows[rows["date"] >= h_start]
    purged = int(len(rows) - len(train) - len(val) - len(hold))
    hold_dates = int(hold["date"].nunique())
    diag.update(train_cases=int(len(train)), validation_cases=int(len(val)), holdout_cases=int(len(hold)), purged_cases=purged,
                holdout_dates=hold_dates, validation_start=str(pd.Timestamp(v_start).date()), holdout_start=str(pd.Timestamp(h_start).date()))
    if min(len(train), len(val), len(hold)) < MIN_TEST_CASES or hold_dates < MIN_DATES:
        out.update(reason="too_few_cases",
                   text=f"After purging overlapping outcomes, {len(train)} fitting, {len(val)} validation and {len(hold)} holdout forecasts "
                        f"({hold_dates} holdout dates) remain; each needs at least {MIN_TEST_CASES} (and {MIN_DATES} dates).")
        return out
    X = list(COMPONENTS)

    def arrays(part: pd.DataFrame) -> tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
        return part[X].to_numpy(float), part["realized"].to_numpy(float), part["sigma"].to_numpy(float), part["date"].to_numpy()
    x_tr, y_tr, s_tr, _ = arrays(train)
    x_va, y_va, s_va, d_va = arrays(val)
    x_ho, y_ho, s_ho, d_ho = arrays(hold)
    w = _ridge(x_tr, y_tr)
    _, t_val, _ = _gain_t(y_va, x_va @ w, d_va)
    chosen = bool(t_val > 1.645)                                 # decided on the validation dates, before the holdout
    zero = np.zeros_like(w)
    pre_x, pre_y, pre_s = np.vstack([x_tr, x_va]), np.concatenate([y_tr, y_va]), np.concatenate([s_tr, s_va])
    k_flat = _band_k(pre_y, np.zeros_like(pre_y), pre_s)
    gain_h, t_hold, _ = _gain_t(y_ho, x_ho @ w, d_ho)
    directional = chosen and gain_h > 0                          # the holdout is a pass/fail gate, never a refit
    use = w if directional else zero
    k = _band_k(pre_y, pre_x @ use, pre_s) if directional else k_flat
    m = _metrics(y_ho, x_ho @ use, s_ho, k)
    flat = _metrics(y_ho, np.zeros_like(y_ho), s_ho, k_flat)
    m["edge_t"] = t_val
    out.update({"weights": {c: float(v) for c, v in zip(COMPONENTS, use)}, "band_k": k, "train_cases": int(len(train)),
                "validation_cases": int(len(val)), "test_cases": int(len(hold)), "purged_cases": purged,
                "split_date": str(pd.Timestamp(h_start).date()), "validation_start": str(pd.Timestamp(v_start).date()),
                "directional": directional, "directional_validation": chosen, "holdout_edge_t": t_hold, "holdout_gain_pct": gain_h,
                "flat_mae_pct": flat["mae_pct"], **m})
    for regime, sub in hold.groupby("regime"):
        if len(sub) >= 10:
            out["by_regime"][str(regime)] = _metrics(sub["realized"].to_numpy(float), sub[X].to_numpy(float) @ use,
                                                     sub["sigma"].to_numpy(float), k)
    cov = m["coverage"]
    if abs(cov - NOMINAL) <= COVERAGE_TOLERANCE:
        out["status"], out["reason"] = "calibrated", ("directional_edge" if directional else "no_directional_edge")
    else:
        out["status"] = "not_calibrated"
        out["reason"] = "coverage_below_target" if cov < NOMINAL else "coverage_above_target"
    parts = [f"Fitted on {len(train)} earlier forecasts, chosen on {len(val)} later ones, and checked on {len(hold)} later forecasts it "
             f"never saw ({hold_dates} dates; {purged} whose outcome overlapped the next period were purged)."]
    parts.append(f"The band held {100 * cov:.0f}% of outcomes on the holdout (the target is 80%).")
    if directional:
        acc = m["directional_accuracy"]
        parts.append(f"The median was {m['mae_pct']:.2f} points off on average against {flat['mae_pct']:.2f} for 'no move'"
                     + (f"; it had the direction right {100 * acc:.0f}% of the time it leaned." if acc is not None else "."))
    elif chosen:
        parts.append("Its median beat assuming no move on the validation dates but not on the final holdout, so the base path is flat: "
                     "a direction is used only when it holds up on dates it was not chosen on.")
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


def readiness(cal: dict[str, Any] | None, horizon: int, job: dict[str, Any] | None = None) -> dict[str, Any]:
    """Whether this horizon's forecast is ready, in two separate parts (4.2): **directional evidence** (did a direction
    beat assuming no move on dates it was not chosen on?) and **range validation** (did the band hold about 80% of
    outcomes on the holdout?), each with its reason, the sample behind it and when it was last checked. A flat base
    path with a validated range is an honest outcome, not a failure: "No reliable directional edge · range shown"."""
    words = HORIZON_WORDS.get(horizon, f"{horizon}-session")
    job = job or {}
    state = job.get("state")
    usable = bool(cal) and cal.get("horizon_days") == horizon and cal.get("model_version", MODEL_VERSION) == MODEL_VERSION
    sample: dict[str, Any] = {}
    if usable:
        d = cal.get("diagnostics") or {}
        sample = {"cases": int(cal.get("test_cases") or cal.get("cases") or 0), "train_cases": cal.get("train_cases"),
                  "validation_cases": cal.get("validation_cases"), "holdout_cases": cal.get("test_cases"), "purged_cases": cal.get("purged_cases"),
                  "symbols": len(cal.get("symbols") or []) or d.get("symbols"), "failed_symbols": cal.get("failed") or [],
                  "first_date": d.get("first_date"), "last_date": d.get("last_date"), "cutoff": cal.get("cutoff"),
                  "checked_at": cal.get("run_at"), "data_source": cal.get("data_source"), "method": cal.get("method") or "cal-3.0"}
    if not usable:
        pending = state in ("queued", "running", "deferred")
        label = f"{words} calibration " + ("running" if state == "running" else "pending" if pending else
                                            "failed" if state == "failed" else "not run yet")
        text = ({"running": "Testing this horizon's forecasts on history now.", "queued": "Queued: it runs in the background.",
                 "deferred": f"Waiting for the data source's request quota to reset ({job.get('until') or 'soon'})."}.get(state)
                or (f"The last attempt failed: {job.get('error')}" if state == "failed" else
                    "Not checked yet: it runs in the background once real market data is connected, or run it from the Forecast record."))
        part = {"status": "pending", "label": "Pending", "text": text}
        return {"horizon_days": horizon, "state": "failed" if state == "failed" else "pending", "reason": "calibration_failed" if state == "failed"
                else "calibration_pending", "label": label, "text": text, "directional": dict(part), "range": dict(part), "sample": {},
                "job": job or None, "refresh_due": True}
    status, cov = cal.get("status"), cal.get("coverage")
    reason = cal.get("reason")
    if status == "insufficient_data":
        code = reason or "too_few_cases"
        text = cal.get("text") or "Too little history to check this horizon's forecasts."
        part = {"status": "insufficient", "label": "Insufficient history", "text": text}
        return {"horizon_days": horizon, "state": "ready", "reason": "insufficient_history", "detail": code,
                "label": f"Insufficient history for the {words.lower()} calibration", "text": text, "directional": dict(part),
                "range": dict(part), "sample": sample, "job": job or None, "refresh_due": state not in ("queued", "running")}
    directional = bool(cal.get("directional"))
    if directional:
        d_part = {"status": "edge", "label": "Directional edge validated",
                  "text": f"The median beat assuming no move on the holdout ({cal.get('mae_pct', 0):.2f} against {cal.get('flat_mae_pct') or cal.get('baseline_mae_pct') or 0:.2f} "
                          "points of average error)."}
    elif cal.get("directional_validation"):
        d_part = {"status": "not_held", "label": "No reliable directional edge",
                  "text": "A direction beat assuming no move on the validation dates but not on the final holdout, so the base path is flat."}
    else:
        d_part = {"status": "no_edge", "label": "No reliable directional edge",
                  "text": "On history the engines did no better than assuming no move, so the base path is flat; the range is still shown."}
    d_part.update(mae_pct=cal.get("mae_pct"), no_change_mae_pct=cal.get("flat_mae_pct") or cal.get("baseline_mae_pct"),
                  directional_accuracy=cal.get("directional_accuracy"), validation_t=cal.get("edge_t"), holdout_gain_pct=cal.get("holdout_gain_pct"))
    if status == "calibrated":
        r_part = {"status": "validated", "label": "Range validated", "text": f"The band held {100 * cov:.0f}% of holdout outcomes (target 80%)."}
    elif cov is not None and cov < NOMINAL:
        r_part = {"status": "below_target", "label": "Range coverage below target",
                  "text": f"The band held {100 * cov:.0f}% of holdout outcomes, below the 80% target: treat the range as too narrow."}
    elif cov is not None:
        r_part = {"status": "above_target", "label": "Range coverage above target",
                  "text": f"The band held {100 * cov:.0f}% of holdout outcomes, above the 80% target: the range is wider than it needs to be."}
    else:
        r_part = {"status": "insufficient", "label": "Range not checked", "text": cal.get("text") or ""}
    r_part.update(coverage=cov, target=NOMINAL, tolerance=COVERAGE_TOLERANCE)
    if not directional and r_part["status"] == "validated":
        label = "No reliable directional edge · range shown"
    elif not directional:
        label = f"No reliable directional edge · {r_part['label'].lower()}"
    else:
        label = f"Directional edge validated · {r_part['label'].lower()}"
    method_old = (cal.get("method") or "cal-3.0") != CALIBRATION_METHOD
    return {"horizon_days": horizon, "state": "ready", "reason": d_part["status"] if r_part["status"] == "validated" else r_part["status"],
            "label": label, "text": f"{d_part['text']} {r_part['text']}", "directional": d_part, "range": r_part, "sample": sample,
            "job": job or None, "refresh_due": method_old,
            "method_note": "Made with the 3.0 fit (no purge between fit and test); a refresh with the 4.2 method is due." if method_old else None}


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
    calibrated = bool(cal) and cal.get("status") == "calibrated"
    if calibrated:
        explanation.append("The band's width comes from " + ", ".join(sources) + f", widened by {k:.2f} so that it held 80% of outcomes on history.")
    elif cal:
        explanation.append("The band's width comes from " + ", ".join(sources) + f", scaled by {k:.2f} from a calibration that did not pass its "
                           "coverage check: the range is the model's 10th to 90th percentile, and how often it held on history is not validated.")
    else:
        explanation.append("The band's width comes from " + ", ".join(sources) + ": the model-implied 10th to 90th percentile, "
                           "not yet checked against history.")
    if ev_move:
        explanation.append(f"An earnings release in {ev_days} trading days adds its priced move to the band from that day.")
    invalidators = []
    if last_price:
        invalidators.append(f"A close below ${last_price * (1 + low[0] / 100):.2f} on the first day, or ${last_price * (1 + low[-1] / 100):.2f} by "
                            f"the horizon: the bottom of {RANGE_WORDS['calibrated' if calibrated else 'unvalidated']}.")
    if ev_move:
        invalidators.append("The earnings release: a move beyond the band on the day is outside this forecast.")
    invalidators.append(f"The market's regime changing from '{comp['regime']}'.")
    return {"model_version": MODEL_VERSION, "horizon_days": horizon,
            "range_words": RANGE_WORDS["calibrated" if calibrated else "unvalidated"], "range_calibrated": calibrated,
            "base": {"key": "base", "label": "Base", "assumption": "today's conditions continue; the engines' weighted readings",
                     "path_pct": path, "low_pct": low, "high_pct": high},
            "alternatives": alternatives, "components": contributions, "weights": {c: float(weights.get(c, 0.0)) for c in COMPONENTS},
            "calibration": calibration_contract(cal, horizon), "explanation": explanation, "invalidators": invalidators,
            "median_pct": path[-1], "low_pct": low[-1], "high_pct": high[-1], "sigma": sigma, "band_k": k,
            "regime": comp["regime"], "analog_search": comp.get("analog_search")}


def record_target(as_of: date, horizon: int) -> date:
    """The session a forecast made from `as_of`'s close is judged at: `horizon` trading sessions later on the exchange
    calendar (4.0; before, pandas business days, which counted market holidays as sessions)."""
    from .marketdays import add_trading_days
    return add_trading_days(as_of, horizon)


__all__ = ["RANGE_WORDS", "MODEL_VERSION", "COMPONENTS", "components_at", "fit", "walk_forward", "forecast", "calibration_contract",
           "record_target", "readiness", "Z80", "CALIBRATION_METHOD"]
