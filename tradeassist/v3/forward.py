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
                           cost_json, status) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                        (C.new_id("sfc"), study_id, values["mode"], values["symbol"], values["horizon"], variant, values.get("provider"),
                         values.get("run_id"), values.get("version_id"), now.isoformat(), values["window_start"], values["target_date"],
                         values["anchor_price"], values["anchor_date"], values["median_pct"], values["low_pct"], values["high_pct"],
                         json.dumps(values.get("cases") or []), values.get("rule"), values.get("latency_seconds"),
                         json.dumps(values.get("cost") or {}), status))
        return status

    def freeze_study(self, study_id: str, now: datetime | None = None) -> dict[str, str]:
        """The base and no change, once the study is saved."""
        f = self._frame(study_id)
        if f is None:
            return {}
        r, frame, fc = f["row"], f["frame"], f["forecast"]
        b = fc["base"]
        h = f["horizon"]
        common = {"mode": r["mode"], "symbol": r["symbol"], "horizon": h, "window_start": window_start(frame["sessions"]).isoformat(),
                  "target_date": frame["sessions"][-1]["date"], "anchor_price": float(frame["anchor"]["price"]),
                  "anchor_date": frame["anchor"]["bar_date"]}
        lo, hi = float(b["low_pct"][h - 1]), float(b["high_pct"][h - 1])
        return {"base": self._write(study_id, "base", {**common, "median_pct": float(b["path_pct"][h - 1]), "low_pct": lo, "high_pct": hi,
                                                         "rule": f"forecast {fc.get('model_version')}"}, now),
                "no_change": self._write(study_id, "no_change", {**common, "median_pct": 0.0, "low_pct": lo, "high_pct": hi,
                                                                   "rule": "no move, with the base's band"}, now)}

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
        r = self.svc.db.one("SELECT mode, symbol FROM studies WHERE id = ?", (study_id,))
        values = {"mode": r["mode"], "symbol": r["symbol"], "horizon": int(version["horizon_sessions"]),
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
                                   case_scores_json = ?, graded_at = ? WHERE id = ? AND status = 'frozen'""",
                                (realized, abs(realized - med), covered, hit, json.dumps(scores), stamp, r["id"]))
                    graded += cur.rowcount
        if graded:
            self.svc.bus.publish("scenario.graded", None, graded=graded)
        return {"graded": graded, "closed_without_data": closed, "missing": missing[:20]}

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

        def summary(v: str) -> dict[str, Any]:
            rs = [r for r in graded if r["variant"] == v]
            n = len(rs)
            leaned = [r["direction_hit"] for r in rs if r["direction_hit"] is not None]
            lat = [r["latency_seconds"] for r in rs if r["latency_seconds"] is not None]
            tokens = 0
            for r in rs:
                u = (json.loads(r["cost_json"] or "{}").get("usage") or {})
                tokens += int(u.get("input_tokens") or 0) + int(u.get("output_tokens") or 0)
            return {"variant": v, "label": VARIANT_LABELS[v], "n": n, "frozen": sum(1 for r in rows if r["variant"] == v and r["status"] == "frozen"),
                    "late": sum(1 for r in rows if r["variant"] == v and r["status"] in ("late", "late_graded")),
                    "mae": statistics.fmean(r["abs_error"] for r in rs) if n else None,
                    "rmse": math.sqrt(statistics.fmean(r["abs_error"] ** 2 for r in rs)) if n else None,
                    "coverage": statistics.fmean(r["covered"] for r in rs) if n else None,
                    "direction_hits": statistics.fmean(leaned) if leaned else None, "leaned": len(leaned),
                    "latency_median_s": statistics.median(lat) if lat else None, "tokens": tokens or None}

        def paired(a: str, b: str) -> dict[str, Any]:
            both = [(s[a], s[b]) for s in by_study.values() if a in s and b in s]
            n = len(both)
            if not n:
                return {"a": a, "b": b, "n": 0}
            ea, eb = [x["abs_error"] for x, _ in both], [y["abs_error"] for _, y in both]
            wins = sum(1 for x, y in zip(ea, eb) if x < y)
            ties = sum(1 for x, y in zip(ea, eb) if x == y)
            return {"a": a, "b": b, "n": n, "mae_a": statistics.fmean(ea), "mae_b": statistics.fmean(eb),
                    "mean_diff": statistics.fmean(x - y for x, y in zip(ea, eb)), "a_closer": wins, "ties": ties,
                    "text": f"{VARIANT_LABELS[a]} was closer than {VARIANT_LABELS[b]} on {wins} of {n} paired observations "
                            f"(average miss {statistics.fmean(ea):.2f} against {statistics.fmean(eb):.2f} points)."}
        pairs = [paired(a, b) for a, b in (("provider", "base"), ("provider", "no_change"), ("provider", "control"), ("control", "base"),
                                           ("base", "no_change"), ("control", "no_change"))]
        cases = [c for r in graded if r["variant"] == "provider" for c in json.loads(r["case_scores_json"] or "[]")]
        setups = self._setups({r["study_id"]: r for r in graded if r["variant"] == "base"})
        pb = next(p for p in pairs if p["a"] == "provider" and p["b"] == "base")
        if pb["n"] < MIN_PAIRED:
            verdict = (f"{pb['n']} paired observation{'s' if pb['n'] != 1 else ''} of the provider and the base so far; at least {MIN_PAIRED} are "
                       "needed before judging. The provider gets no weight in the base.")
        elif pb["mean_diff"] < 0 and pb["a_closer"] > pb["n"] / 2:
            verdict = (f"The provider was closer than the base on {pb['a_closer']} of {pb['n']} paired observations. That is evidence for "
                       "a later decision; the base is unchanged.")
        else:
            verdict = f"The provider was not closer than the base ({pb['a_closer']} of {pb['n']} paired observations). The base is unchanged."
        return {"horizon": horizon, "mode": mode, "variants": [summary(v) for v in VARIANTS], "pairs": pairs,
                "cases": {"n": len(cases), "mae": statistics.fmean(c["abs_error"] for c in cases) if cases else None,
                          "coverage": statistics.fmean(c["covered"] for c in cases) if cases else None},
                "setups": setups, "verdict": verdict, "rule": POINT_RULE,
                "note": ("Each variant is frozen before its window opens and graded on the target session's close from the frozen anchor price. "
                         "Paired comparisons use only the studies where both were frozen and graded. A provider's point is the equal-weighted mean "
                         "of its cases (catalyst-mean-1), a convention for comparison, not a probability.")}

    def _setups(self, graded_base: dict[str, dict[str, Any]]) -> dict[str, Any]:
        """The direction of each Qualified (or triggered) setup against the realized move."""
        out = []
        for sid, r in graded_base.items():
            d = self.svc.db.one("SELECT decision_json FROM scenario_decisions WHERE study_id = ? AND status = 'qualified' "
                                "ORDER BY created_at DESC LIMIT 1", (sid,))
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
