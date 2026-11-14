"""Forward evidence (4.5): each scenario study's predictions frozen before its target window starts, graded once the
target session's close is final, and compared on the same observations.

For every 1- or 5-session study run with scenarios, up to four variants are frozen (`scenario_forecasts`), each a
median move to the target session with a band:

| variant | what it is |
| --- | --- |
| base | the study's own forecast (its median path's last point and its band) |
| no_change | no move, with the base's band: the benchmark every forecast must beat |
| control | the single-prompt control (SCENARIO_CONTROL=1): one AI call on the same packet, its cases sized by the same rules |
| provider | the scenario provider (MiroFish), its cases sized by the same rules |

A provider's or the control's point forecast follows the rule **catalyst-mean-1**: the equal-weighted mean of its
cases' terminal moves (a scoring convention for comparing like with like, not a probability for any case), with the
band from the lowest case low to the highest case high. A variant is **frozen** only if it is written before the first
forecast session opens (09:30 New York); one written later is kept as **late** and never scored, so nothing that saw
the window can be graded as a forecast of it.

Grading uses the frozen anchor price (the price the paths were drawn from) and the target session's final close:
the realized move, the absolute error, whether it fell inside the band, and whether the median leaned the right way.
A conditional case is scored only when its trigger was recorded as observed with an independent source: its terminal
move against what happened. The scorecard compares the variants **paired** (only the studies where both were frozen
and graded), with coverage, latency and the provider's token use, and the outcome of each Qualified or triggered
setup's direction. It is evidence for a later decision; it gives MiroFish no weight in the base.
"""

from __future__ import annotations

import json
import logging
import math
import statistics
import time
from datetime import date, datetime, timezone
from typing import Any

import pandas as pd

from .. import contracts as C
from ..market import settled_through
from ..optionmath import NY

log = logging.getLogger(__name__)

VARIANTS = ("base", "no_change", "control", "provider")
BOOT = 2000                    # bootstrap draws
MIN_CLUSTERS = 8               # calendar weeks of target dates a comparison needs before it is judged
VERDICTS = {"insufficient": "insufficient evidence", "none": "no demonstrated improvement", "suggestive": "suggestive improvement",
            "supported": "statistically supported improvement", "worse": "worse than the comparison"}
VARIANT_LABELS = {"base": "Base forecast", "no_change": "No change", "control": "Single-prompt control", "provider": "Scenario provider"}
POINT_RULE = "catalyst-mean-1"
GIVE_UP_DAYS = 14
MIN_PAIRED = 30
GRADE = "scenario.grade"


def _f(x: Any) -> float | None:
    try:
        v = float(x)
    except (TypeError, ValueError):
        return None
    return v if math.isfinite(v) else None


def window_start(sessions: list[dict[str, Any]]) -> datetime:
    d = date.fromisoformat(sessions[0]["date"])
    return datetime(d.year, d.month, d.day, 9, 30, tzinfo=NY)


def point_of(version: dict[str, Any]) -> dict[str, Any]:
    """catalyst-mean-1: the equal-weighted mean of the cases' terminal moves, and their outer band."""
    cases = [c for c in version.get("cases") or [] if c.get("visible", True)]
    if not cases:
        raise ValueError("the version has no cases")
    med = sum(float(c["return_pct"][-1]) for c in cases) / len(cases)
    lo = min(float(c["low_pct"][-1]) for c in cases)
    hi = max(float(c["high_pct"][-1]) for c in cases)
    return {"median_pct": med, "low_pct": lo, "high_pct": hi,
            "cases": [{"key": c["key"], "return_pct": float(c["return_pct"][-1]), "low_pct": float(c["low_pct"][-1]),
                       "high_pct": float(c["high_pct"][-1]), "quantified": bool(c.get("quantified")), "trigger": c.get("trigger")} for c in cases]}


def _week(day: str) -> str:
    y, w, _ = date.fromisoformat(day).isocalendar()
    return f"{y}-W{w:02d}"


def _fmt(ci: list[float] | None) -> str:
    return "n/a" if not ci else f"{ci[0]:+.2f} to {ci[1]:+.2f}"


def cluster_ci(diffs: list[float], clusters: list[str], draws: int = BOOT, seed: int = 7) -> dict[str, Any]:
    """Percentile intervals of the mean difference from a bootstrap that draws whole clusters (calendar weeks), so
    studies that share a week (overlapping windows, one market move) are not treated as independent."""
    import numpy as np
    arr = np.asarray(diffs, dtype=float)
    keys = sorted(set(clusters))
    if len(arr) < 2 or len(keys) < 2:
        return {"mean": float(arr.mean()) if len(arr) else None, "ci80": None, "ci95": None, "clusters": len(keys)}
    sums = np.array([arr[[i for i, c in enumerate(clusters) if c == k]].sum() for k in keys])
    counts = np.array([sum(1 for c in clusters if c == k) for k in keys], dtype=float)
    pick = np.random.default_rng(seed).integers(0, len(keys), size=(draws, len(keys)))
    means = sums[pick].sum(axis=1) / counts[pick].sum(axis=1)
    q = np.quantile(means, [0.025, 0.10, 0.90, 0.975])
    return {"mean": float(arr.mean()), "ci80": [float(q[1]), float(q[2])], "ci95": [float(q[0]), float(q[3])], "clusters": len(keys)}


def verdict_of(n: int, ci: dict[str, Any]) -> str:
    """Negative differences mean a smaller miss. Judged only with enough pairs over enough weeks; one smaller average
    is never called significant on its own."""
    if n < MIN_PAIRED or ci.get("clusters", 0) < MIN_CLUSTERS or not ci.get("ci95"):
        return "insufficient"
    lo95, hi95 = ci["ci95"]
    if hi95 < 0:
        return "supported"
    if lo95 > 0:
        return "worse"
    if ci["ci80"] and ci["ci80"][1] < 0:
        return "suggestive"
    return "none"


def accuracy(rs: list[dict[str, Any]]) -> dict[str, Any]:
    n = len(rs)
    if not n:
        return {"n": 0, "mae": None, "rmse": None, "coverage": None, "direction_hits": None, "leaned": 0, "bias": None, "width": None}
    leaned = [r["direction_hit"] for r in rs if r["direction_hit"] is not None]
    signed = [r["signed_error"] if r.get("signed_error") is not None else (r["realized_pct"] - r["median_pct"]) for r in rs]
    widths = [r["width"] if r.get("width") is not None else (r["high_pct"] - r["low_pct"]) for r in rs]
    return {"n": n, "mae": statistics.fmean(r["abs_error"] for r in rs), "rmse": math.sqrt(statistics.fmean(r["abs_error"] ** 2 for r in rs)),
            "coverage": statistics.fmean(r["covered"] for r in rs), "direction_hits": statistics.fmean(leaned) if leaned else None,
            "leaned": len(leaned), "bias": statistics.fmean(signed), "width": statistics.fmean(widths)}


def _by(rs: list[dict[str, Any]], key: Any) -> dict[str, Any]:
    groups: dict[str, list[dict[str, Any]]] = {}
    for r in rs:
        groups.setdefault(str(key(r)), []).append(r)
    return {k: {kk: v for kk, v in accuracy(g).items() if kk in ("n", "mae", "coverage", "direction_hits", "bias")} for k, g in groups.items()}


def _vol_regimes(by_study: dict[str, dict[str, dict[str, Any]]]) -> dict[str, str]:
    """Each study's volatility regime: the tercile of its base band's width among the graded studies (low, mid, high)."""
    widths = {sid: (x["base"].get("width") if x["base"].get("width") is not None else x["base"]["high_pct"] - x["base"]["low_pct"])
              for sid, x in by_study.items() if "base" in x}
    if len(widths) < 3:
        return {sid: "unknown" for sid in widths}
    ordered = sorted(widths.values())
    lo, hi = ordered[len(ordered) // 3], ordered[(2 * len(ordered)) // 3]
    return {sid: ("low" if w < lo else "high" if w >= hi else "mid") for sid, w in widths.items()}


class Forward:
    def __init__(self, svc: Any):
        self.svc = svc

    def _frame(self, study_id: str) -> dict[str, Any] | None:
        from .scenarios import study_frame
        r = self.svc.db.one("SELECT * FROM studies WHERE id = ?", (study_id,))
        if r is None or not r["forecast_json"]:
            return None
        frame = study_frame(r["created_at"], json.loads(r["view_json"] or "{}"))
        if frame is None:
            return None
        h = int(r["horizon_days"])
        frame["sessions"] = frame["sessions"][:h]
        return {"row": r, "frame": frame, "horizon": h, "forecast": json.loads(r["forecast_json"])}

    def _write(self, study_id: str, variant: str, values: dict[str, Any], now: datetime | None = None) -> str:
        """Frozen before the window opens, or kept as late. A variant frozen in time is replaced only by one that is
        also in time (a retry before the window); nothing replaces it once the window has opened."""
        now = now or datetime.now(timezone.utc)
        start = datetime.fromisoformat(values["window_start"])
        status = "frozen" if now < start else "late"
        old = self.svc.db.one("SELECT status FROM scenario_forecasts WHERE study_id = ? AND variant = ?", (study_id, variant))
        if old is not None and (status == "late" or old["status"] not in ("frozen", "late")):
            return old["status"]
        with self.svc.db.tx() as cur:
            cur.execute("DELETE FROM scenario_forecasts WHERE study_id = ? AND variant = ?", (study_id, variant))
            cur.execute("""INSERT INTO scenario_forecasts (id, study_id, mode, symbol, horizon, variant, provider, run_id, version_id, made_at,
                           window_start, target_date, anchor_price, anchor_date, median_pct, low_pct, high_pct, cases_json, rule, latency_seconds,
                           cost_json, status, data_cutoff, model_version, profile, config_json, catalyst_kind, market_regime, band_basis, width)
                           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                        (C.new_id("sfc"), study_id, values["mode"], values["symbol"], values["horizon"], variant, values.get("provider"),
                         values.get("run_id"), values.get("version_id"), now.isoformat(), values["window_start"], values["target_date"],
                         values["anchor_price"], values["anchor_date"], values["median_pct"], values["low_pct"], values["high_pct"],
                         json.dumps(values.get("cases") or []), values.get("rule"), values.get("latency_seconds"),
                         json.dumps(values.get("cost") or {}), status, values.get("data_cutoff"), values.get("model_version"), values.get("profile"),
                         json.dumps(values.get("config")) if values.get("config") is not None else None, values.get("catalyst_kind"),
                         values.get("market_regime"), values.get("band_basis"), float(values["high_pct"]) - float(values["low_pct"])))
        return status

    def freeze_study(self, study_id: str, now: datetime | None = None) -> dict[str, str]:
        """The base and no change, once the study is saved."""
        f = self._frame(study_id)
        if f is None:
            return {}
        r, frame, fc = f["row"], f["frame"], f["forecast"]
        b = fc["base"]
        h = f["horizon"]
        pl = json.loads(r["pipeline_json"] or "{}") if "pipeline_json" in r.keys() and r["pipeline_json"] else {}
        common = {"mode": r["mode"], "symbol": r["symbol"], "horizon": h, "window_start": window_start(frame["sessions"]).isoformat(),
                  "target_date": frame["sessions"][-1]["date"], "anchor_price": float(frame["anchor"]["price"]),
                  "anchor_date": frame["anchor"]["bar_date"], "data_cutoff": r["created_at"], "model_version": fc.get("model_version"),
                  "market_regime": fc.get("regime"), "catalyst_kind": (pl.get("catalyst") or {}).get("kind")}
        lo, hi = float(b["low_pct"][h - 1]), float(b["high_pct"][h - 1])
        return {"base": self._write(study_id, "base", {**common, "median_pct": float(b["path_pct"][h - 1]), "low_pct": lo, "high_pct": hi,
                                                         "rule": f"forecast {fc.get('model_version')}", "band_basis": "own"}, now),
                # 4.5.3: its band is the base's, borrowed: not an independently estimated uncertainty, and labelled so.
                "no_change": self._write(study_id, "no_change", {**common, "median_pct": 0.0, "low_pct": lo, "high_pct": hi,
                                                                   "rule": "no move, with the base's band", "band_basis": "base_band"}, now)}

    def freeze_version(self, study_id: str, version: dict[str, Any], run: Any, role: str, now: datetime | None = None) -> str | None:
        """A provider's or the control's version, by catalyst-mean-1."""
        variant = "control" if role == "control" else "provider"
        try:
            pt = point_of(version)
        except ValueError:
            return None
        ext = json.loads(run["external_json"] or "{}")
        started, ended = ext.get("started_at"), ext.get("report_received_at")
        latency = None
        if started and ended:
            latency = (datetime.fromisoformat(ended.replace("Z", "+00:00")) - datetime.fromisoformat(started.replace("Z", "+00:00"))).total_seconds()
        r = self.svc.db.one("SELECT mode, symbol, forecast_json FROM studies WHERE id = ?", (study_id,))
        seed = self.svc.db.one("SELECT cutoff FROM scenario_seeds WHERE id = ?", (run["seed_id"],)) if "seed_id" in run.keys() else None
        fc = json.loads(r["forecast_json"] or "{}") if r["forecast_json"] else {}
        kind = (version.get("catalyst") or {}).get("kind")
        if kind:
            with self.svc.db.tx() as cur:          # the base and no change learn the catalyst's kind once it is chosen
                cur.execute("UPDATE scenario_forecasts SET catalyst_kind = ? WHERE study_id = ? AND catalyst_kind IS NULL", (kind, study_id))
        values = {"mode": r["mode"], "symbol": r["symbol"], "horizon": int(version["horizon_sessions"]),
                  "data_cutoff": seed["cutoff"] if seed else None, "model_version": f"{ext.get('model') or run['provider']} / {run['prompt_version']}",
                  "profile": ext.get("profile"), "config": json.loads(run["config_json"] or "{}"), "catalyst_kind": kind,
                  "market_regime": fc.get("regime"), "band_basis": "cases",
                  "window_start": window_start(version["sessions"]).isoformat(), "target_date": version["sessions"][-1]["date"],
                  "anchor_price": float(version["anchor"]["price"]), "anchor_date": version["anchor"]["bar_date"], **pt,
                  "provider": run["provider"], "run_id": run["id"], "version_id": version["id"], "rule": POINT_RULE, "latency_seconds": latency,
                  "cost": {"model": ext.get("model"), "usage": ext.get("usage") or {}, "rounds": (ext.get("state") or {}).get("rounds")}}
        return self._write(study_id, variant, values, now)

    # --- grading ---------------------------------------------------------------------------------------------------
    def grade(self, payload: dict[str, Any] | None = None, now: float | None = None, limit: int = 200) -> dict[str, Any]:
        now = time.time() if now is None else now
        settled = settled_through(now)
        mode = self.svc.market.info.mode
        rows = [r for r in self.svc.db.query("SELECT * FROM scenario_forecasts WHERE status IN ('frozen', 'late') AND mode = ? ORDER BY target_date",
                                              (mode,)) if date.fromisoformat(r["target_date"]) <= settled][:limit]
        graded = closed = 0
        missing: list[str] = []
        by_symbol: dict[str, list[Any]] = {}
        for r in rows:
            by_symbol.setdefault(r["symbol"], []).append(r)
        for sym, recs in by_symbol.items():
            try:
                bars = self.svc.market.bars(sym, years=1.0, complete=True)
            except Exception as exc:  # noqa: BLE001 - left for the next run
                missing.append(f"{sym}: {str(exc)[:120]}")
                continue
            closes = pd.Series(bars["close"].to_numpy(float), index=pd.to_datetime(bars["date"]).dt.date)
            for r in recs:
                target = date.fromisoformat(r["target_date"])
                stamp = datetime.now(timezone.utc).isoformat(timespec="seconds")
                if target not in closes.index:
                    missing.append(f"{sym} {target}: no bar")
                    if (settled - target).days > GIVE_UP_DAYS:
                        with self.svc.db.tx() as cur:
                            cur.execute("UPDATE scenario_forecasts SET status = 'ungradable', graded_at = ? WHERE id = ?", (stamp, r["id"]))
                            closed += 1
                    continue
                realized = 100.0 * (float(closes[target]) / float(r["anchor_price"]) - 1.0)
                if r["status"] == "late":              # never scored: it saw the window
                    with self.svc.db.tx() as cur:
                        cur.execute("UPDATE scenario_forecasts SET status = 'late_graded', realized_pct = ?, graded_at = ? WHERE id = ?",
                                    (realized, stamp, r["id"]))
                    continue
                med = float(r["median_pct"])
                hit = None if med == 0 else int((med > 0) == (realized > 0))
                covered = int(float(r["low_pct"]) <= realized <= float(r["high_pct"]))
                scores = self._case_scores(r, realized)
                with self.svc.db.tx() as cur:
                    cur.execute("""UPDATE scenario_forecasts SET status = 'graded', realized_pct = ?, abs_error = ?, covered = ?, direction_hit = ?,
                                   case_scores_json = ?, graded_at = ?, signed_error = ?, width = ? WHERE id = ? AND status = 'frozen'""",
                                (realized, abs(realized - med), covered, hit, json.dumps(scores), stamp, realized - med,
                                 float(r["high_pct"]) - float(r["low_pct"]), r["id"]))
                    graded += cur.rowcount
        outs = getattr(self.svc, "outcomes", None)                 # 4.5.3: the Qualified setups' frozen outcomes too
        strategies = {"graded": 0}
        if outs is not None:
            try:
                strategies = outs.grade(now=now)
            except Exception:  # noqa: BLE001 - left for the next pass
                log.exception("Could not grade the strategy outcomes")
        if graded or strategies.get("graded"):
            self.svc.bus.publish("scenario.graded", None, graded=graded)
        return {"graded": graded, "closed_without_data": closed, "missing": missing[:20], "strategies": strategies}

    def _case_scores(self, r: Any, realized: float) -> list[dict[str, Any]]:
        """A conditional case, scored only when its trigger was **verified** from its source (4.5.1) and recorded before
        the target session closed: one recorded once the outcome was known cannot improve the forecast's score."""
        seen = {x["case_key"]: x for x in self.svc.db.query("SELECT * FROM scenario_triggers WHERE study_id = ?", (r["study_id"],))}
        target_close = datetime.combine(date.fromisoformat(r["target_date"]), datetime.min.time().replace(hour=16), tzinfo=NY)
        out = []
        for c in json.loads(r["cases_json"] or "[]"):
            t = seen.get(c["key"])
            if t is None or ("status" in t.keys() and t["status"] != "verified") or "status" not in t.keys():
                continue
            recorded = datetime.fromisoformat(str(t["recorded_at"]).replace("Z", "+00:00"))
            recorded = recorded if recorded.tzinfo else recorded.replace(tzinfo=timezone.utc)
            if recorded >= target_close:
                continue
            out.append({"key": c["key"], "return_pct": c["return_pct"], "abs_error": abs(realized - c["return_pct"]),
                        "covered": int(c["low_pct"] <= realized <= c["high_pct"]), "observed_at": t["observed_at"],
                        "source": json.loads(t["source_json"]).get("title")})
        return out

    # --- the scorecard ------------------------------------------------------------------------------------------------
    def scorecard(self, horizon: int | None = None) -> dict[str, Any]:
        """The four approaches on the same studies (4.5.3): accuracy, direction, bias, coverage and width; paired
        comparisons with confidence intervals that respect correlated observations; breakdowns; costs; the
        simulation profiles; and the Qualified setups' strategy outcomes by category."""
        mode = self.svc.market.info.mode
        sql, params = "SELECT * FROM scenario_forecasts WHERE mode = ?", [mode]
        if horizon:
            sql += " AND horizon = ?"
            params.append(int(horizon))
        rows = [dict(r) for r in self.svc.db.query(sql, params)]
        graded = [r for r in rows if r["status"] == "graded"]
        by_study: dict[str, dict[str, dict[str, Any]]] = {}
        for r in graded:
            by_study.setdefault(r["study_id"], {})[r["variant"]] = r
        vol = _vol_regimes(by_study)

        def summary(v: str) -> dict[str, Any]:
            rs = [r for r in graded if r["variant"] == v]
            out = {"variant": v, "label": VARIANT_LABELS[v], **accuracy(rs),
                   "frozen": sum(1 for r in rows if r["variant"] == v and r["status"] == "frozen"),
                   "late": sum(1 for r in rows if r["variant"] == v and r["status"] in ("late", "late_graded")),
                   "ungradable": sum(1 for r in rows if r["variant"] == v and r["status"] == "ungradable")}
            lat = [r["latency_seconds"] for r in rs if r["latency_seconds"] is not None]
            tokens = 0
            for r in rs:
                u = (json.loads(r["cost_json"] or "{}").get("usage") or {})
                tokens += int(u.get("input_tokens") or 0) + int(u.get("output_tokens") or 0)
            nc = [(s[v], s["no_change"]) for s in by_study.values() if v in s and "no_change" in s]
            skill = None
            if nc and v != "no_change":
                mae_v = statistics.fmean(a["abs_error"] for a, _ in nc)
                mae_n = statistics.fmean(b["abs_error"] for _, b in nc)
                skill = (1.0 - mae_v / mae_n) if mae_n > 0 else None
            out.update(latency_median_s=statistics.median(lat) if lat else None, tokens=tokens or None, skill_vs_no_change=skill,
                       band_note=("its band is the base's, borrowed: not an independent estimate of uncertainty" if v == "no_change" else
                                  "from its cases' outer band" if v in ("control", "provider") else "the forecast's own band"),
                       by_horizon=_by(rs, lambda r: str(r["horizon"])), by_catalyst=_by(rs, lambda r: r.get("catalyst_kind") or "unknown"),
                       by_market_regime=_by(rs, lambda r: r.get("market_regime") or "unknown"),
                       by_volatility=_by(rs, lambda r: vol.get(r["study_id"], "unknown")))
            return out

        def paired(a: str, b: str, subset: list[dict[str, dict[str, Any]]] | None = None) -> dict[str, Any]:
            studies = subset if subset is not None else list(by_study.values())
            both = [(x[a], x[b]) for x in studies if a in x and b in x]
            n = len(both)
            if not n:
                return {"a": a, "b": b, "n": 0, "verdict": "insufficient", "verdict_label": VERDICTS["insufficient"]}
            ea, eb = [x["abs_error"] for x, _ in both], [y["abs_error"] for _, y in both]
            wins = sum(1 for x, y in zip(ea, eb) if x < y)
            ties = sum(1 for x, y in zip(ea, eb) if x == y)
            diffs = [x - y for x, y in zip(ea, eb)]
            ci = cluster_ci(diffs, [_week(x["target_date"]) for x, _ in both])
            dir_a = [x["direction_hit"] for x, _ in both if x["direction_hit"] is not None]
            dir_b = [y["direction_hit"] for _, y in both if y["direction_hit"] is not None]
            verdict = verdict_of(n, ci)
            return {"a": a, "b": b, "n": n, "mae_a": statistics.fmean(ea), "mae_b": statistics.fmean(eb), "mean_diff": statistics.fmean(diffs),
                    "a_closer": wins, "ties": ties, "ci80": ci["ci80"], "ci95": ci["ci95"], "clusters": ci["clusters"],
                    "symbols": len({x["symbol"] for x, _ in both}), "direction_a": statistics.fmean(dir_a) if dir_a else None,
                    "direction_b": statistics.fmean(dir_b) if dir_b else None, "verdict": verdict, "verdict_label": VERDICTS[verdict],
                    "text": f"{VARIANT_LABELS[a]} was closer than {VARIANT_LABELS[b]} on {wins} of {n} paired observations "
                            f"(average miss {statistics.fmean(ea):.2f} against {statistics.fmean(eb):.2f} points; difference "
                            f"{statistics.fmean(diffs):+.2f}, 95% interval {_fmt(ci['ci95'])} over {ci['clusters']} week(s)): {VERDICTS[verdict]}."}
        pairs = [paired(a, b) for a, b in (("provider", "base"), ("provider", "no_change"), ("provider", "control"), ("control", "base"),
                                           ("base", "no_change"), ("control", "no_change"))]
        cases = [c for r in graded if r["variant"] == "provider" for c in json.loads(r["case_scores_json"] or "[]")]
        setups = self._setups({r["study_id"]: r for r in graded if r["variant"] == "base"})
        pb = next(p for p in pairs if p["a"] == "provider" and p["b"] == "base")
        pc = next(p for p in pairs if p["a"] == "provider" and p["b"] == "control")
        costs = self._costs(by_study)
        profiles = []
        for prof in sorted({r.get("profile") for r in graded if r["variant"] == "provider" and r.get("profile")}):
            sub = [x for x in by_study.values() if "provider" in x and x["provider"].get("profile") == prof]
            pp = paired("provider", "base", sub)
            pcost = [c for sid, c in costs["per_study"].items() if (by_study.get(sid, {}).get("provider") or {}).get("profile") == prof]
            profiles.append({"profile": prof, "n": pp["n"], "mae": pp.get("mae_a"), "vs_base": pp,
                             "average_cost_usd": statistics.fmean(pcost) if pcost else None})
        verdict = self._conclusion(pb, pc, costs, profiles)
        strategies = None
        outs = getattr(self.svc, "outcomes", None)
        if outs is not None:
            strategies = outs.summary(horizon)
        return {"horizon": horizon, "mode": mode, "variants": [summary(v) for v in VARIANTS], "pairs": pairs,
                "cases": {"n": len(cases), "mae": statistics.fmean(c["abs_error"] for c in cases) if cases else None,
                          "coverage": statistics.fmean(c["covered"] for c in cases) if cases else None},
                "setups": setups, "verdict": verdict, "rule": POINT_RULE, "costs": costs, "profiles": profiles, "strategies": strategies,
                "missing": {"late": sum(1 for r in rows if r["status"] in ("late", "late_graded")), "ungradable": sum(1 for r in rows if r["status"] == "ungradable"),
                            "waiting": sum(1 for r in rows if r["status"] == "frozen")},
                "method": ("Paired: only the studies where both were frozen in time and graded. Each difference is one study's miss less the other's; "
                           f"its intervals come from {BOOT} bootstrap draws of whole calendar weeks of target dates (overlapping windows and "
                           "market-wide moves fall in the same week, so correlated studies are not counted as independent experiments). "
                           f"A comparison needs {MIN_PAIRED} pairs over {MIN_CLUSTERS} weeks before it is judged. Only studies run with scenarios "
                           "are included, which is a selection of their own."),
                "note": ("Each variant is frozen before its window opens and graded on the target session's close from the frozen anchor price. "
                         "A provider's point is the equal-weighted mean of its cases (catalyst-mean-1), a convention for comparison, not a "
                         "probability; the cases are conditional scenarios with no probabilities. No change uses the base's band. A wide band "
                         "covers more outcomes without being more accurate. The provider has no weight in the base forecast in this release.")}

    def _costs(self, by_study: dict[str, dict[str, dict[str, Any]]]) -> dict[str, Any]:
        """What each scenario study's provider and control cost (measured, entered, or estimated), from the usage ledger."""
        from .simulation import Ledger
        per, ctrl, conf = {}, {}, set()
        for sid, x in by_study.items():
            for v, bucket in (("provider", per), ("control", ctrl)):
                rid = (x.get(v) or {}).get("run_id")
                if not rid:
                    continue
                u = self.svc.db.one("SELECT * FROM scenario_usage WHERE run_id = ?", (rid,))
                if u is None:
                    continue
                lo, hi, c = Ledger.incurred(dict(u))
                if lo is not None:
                    bucket[sid] = (lo + hi) / 2
                    conf.add(c)
        return {"per_study": per, "provider_average_usd": statistics.fmean(per.values()) if per else None,
                "control_average_usd": statistics.fmean(ctrl.values()) if ctrl else None, "provider_costed": len(per), "control_costed": len(ctrl),
                "confidence": sorted(conf) or ["unknown"],
                "words": ("no costed provider runs yet" if not per else
                          f"{len(per)} provider run(s) costed ({', '.join(sorted(conf))}): {statistics.fmean(per.values()):.2f} USD per study"
                          + (f"; the single-prompt control {statistics.fmean(ctrl.values()):.2f} USD" if ctrl else ""))}

    @staticmethod
    def _conclusion(pb: dict[str, Any], pc: dict[str, Any], costs: dict[str, Any], profiles: list[dict[str, Any]]) -> str:
        words = []
        if pb["verdict"] == "insufficient":
            words.append(f"{pb['n']} paired observation{'s' if pb['n'] != 1 else ''} of the provider and the base so far: insufficient evidence "
                         f"(at least {MIN_PAIRED} over {MIN_CLUSTERS} weeks are needed).")
        else:
            words.append(f"Against the base: {VERDICTS[pb['verdict']]} ({pb['a_closer']} of {pb['n']} closer; difference {pb['mean_diff']:+.2f} points, "
                         f"95% interval {_fmt(pb['ci95'])}).")
        if pc["n"]:
            words.append(f"Against the single-prompt control: {VERDICTS[pc['verdict']]} ({pc['n']} pairs).")
        extra = None
        if costs.get("provider_average_usd") is not None and costs.get("control_average_usd") is not None:
            extra = costs["provider_average_usd"] - costs["control_average_usd"]
            words.append(f"It costs about {extra:+.2f} USD per study more than the control.")
        if pc["n"] and pc["verdict"] in ("none", "worse") and (extra is None or extra > 0):
            words.append("MiroFish has not shown a benefit over the cheaper single prompt; the lower-cost runs remain the default.")
        deep = next((p for p in profiles if p["profile"] == "deep"), None)
        quick = next((p for p in profiles if p["profile"] == "quick"), None)
        if deep and quick and deep["vs_base"]["verdict"] not in ("supported",):
            words.append("Deeper simulations have not shown a supported improvement over the base: Quick stays the default.")
        words.append("The provider gets no weight in the base forecast in this release.")
        return " ".join(words)

    def _setups(self, graded_base: dict[str, dict[str, Any]]) -> dict[str, Any]:
        """The direction of each Qualified (or triggered) setup against the realized move."""
        out = []
        for sid, r in graded_base.items():
            # 4.7: only a decision on an automatic run's own version: a promoted (person-edited) version's is never scored.
            d = self.svc.db.one("""SELECT d.decision_json FROM scenario_decisions d JOIN scenario_versions v ON v.id = d.version_id
                                   WHERE d.study_id = ? AND d.status = 'qualified' AND v.origin = 'auto'
                                   ORDER BY d.created_at DESC LIMIT 1""", (sid,))
            if d is None:
                continue
            s = (json.loads(d["decision_json"]).get("setup") or {})
            sign = 1.0 if s.get("direction") == "LONG" else -1.0 if s.get("direction") == "SHORT" else 0.0
            if sign:
                out.append(sign * float(r["realized_pct"]))
        return {"n": len(out), "went_its_way": statistics.fmean(1.0 if x > 0 else 0.0 for x in out) if out else None,
                "mean_move_its_way_pct": statistics.fmean(out) if out else None}


def install(svc: Any) -> None:
    from ..api_v3 import register
    from ..jobs import Schedule
    from .workers import workers_for
    svc.forward = fw = Forward(svc)
    rw = workers_for(svc)
    rw.add("scenarios", GRADE, fw.grade)
    rw.schedule("scenarios", Schedule(GRADE, 3 * 3600, 3600))
    try:                                        # forecasts whose target has passed: graded soon after start
        today = datetime.now(NY).date().isoformat()
        if svc.db.one("SELECT 1 FROM scenario_forecasts WHERE status IN ('frozen', 'late') AND target_date < ? LIMIT 1", (today,)):
            rw.enqueue("scenarios", GRADE, {}, dedupe_key=f"{GRADE}:start:{today}")
    except Exception:  # noqa: BLE001 - never block start
        log.exception("Could not queue the forward grading")

    def routes(router: Any, svc: Any) -> None:
        @router.get("/scenarios/scorecard")
        def scorecard(horizon: int | None = None) -> dict[str, Any]:
            return fw.scorecard(horizon)

        @router.get("/studies/{study_id}/forward")
        def forecasts(study_id: str) -> dict[str, Any]:
            return {"forecasts": [dict(r) for r in svc.db.query("SELECT * FROM scenario_forecasts WHERE study_id = ? ORDER BY variant", (study_id,))]}

    register(svc, routes)


__all__ = ["Forward", "install", "point_of", "window_start", "VARIANTS", "POINT_RULE"]
_ = _f
