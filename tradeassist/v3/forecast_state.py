"""4.7 One authoritative forecast state for a Research study: the ForecastBundle, and how the study is being looked at.

Before 4.7 a study's forecast reached the screen in pieces: the study (its base forecast and the strategies chosen with
it), the pipeline's view (the official scenario version, its valuation and decision), the manual tools' runs and
versions, and a chart payload assembled in the browser from whichever of these was drawn last. A version edited by
hand could be drawn beside the official decision of another version, and nothing said so.

`GET /v3/studies/{study_id}/result` assembles one read-only result from the saved records:

- the study's frame (anchor price and session dates), its base forecast with what its readiness allows it to claim;
- the official automatic run (provider, status, catalyst, the version it saved, the valuation and decision made from
  it), when the study has one;
- every scenario version of the study, each said to be official, exploratory (edited by a person), the control, or an
  earlier automatic run's, with how the forward scorecard treats it;
- the version and case being looked at (the *selected context*) and, at most, one earlier version pinned for
  comparison, with whether the two can share a chart;
- the valuation of the selected version only, and the decision of the selected version only: the official decision
  for the official version, or the research-only assessment (scenario_lab.py) for an exploratory one. A decision of
  another version is never shown as this one's; the bundle says whose it is instead;
- the strategies as one table under that context, the study's lifecycle in one state, and provenance.

Assembling it reads the database only: no paid simulation, no market data, no repricing.

The *presentation state* (`study_presentation`, migration 31) is a view preference, kept apart from the immutable
records: the selected version and case, the comparison, the chart layers shown and the chart's focus. It is per
study, so hiding the Bearish line on one study never hides it on another, and every version named in it is checked
to belong to the study. An unsaved preview is never stored.
"""

from __future__ import annotations

import json
import logging
from datetime import date, datetime
from typing import Any, Literal

from fastapi import Query
from pydantic import Field

from .. import contracts as C
from ..optionmath import NY
from ..service import RequestError

log = logging.getLogger(__name__)

BUNDLE_VERSION = "forecast-bundle-1"
LAYERS = ("base", "range", "central", "bullish", "bearish", "compare", "marker")
DEFAULT_LAYERS = {k: True for k in LAYERS}
FOCUS = ("base", "cases", "decision", "compare")
CASE_KEYS = ("central", "bullish", "bearish")
KIND_WORDS = {"official": "Official (automatic)", "exploratory": "Exploratory (your edit)", "control": "Single-prompt control",
              "previous": "Earlier automatic run", "promoted": "Official (promoted from your edit)"}
LIFECYCLE_WORDS = {
    "no_study": "No study", "evidence_running": "Collecting the evidence", "study_failed": "The study did not finish",
    "baseline_complete": "Baseline complete", "simulation_queued": "Simulation queued", "simulation_running": "Simulation running",
    "awaiting_approval": "Waiting for your simulation approval", "version_saved": "Scenario version saved",
    "valuing": "Valuing the strategies", "decision_ready": "Decision ready", "simulation_failed": "Simulation did not finish",
    "simulation_canceled": "Simulation canceled", "needs_attention": "Simulation needs attention",
}
HEADLINES = {
    "pending": ("Calibration pending", "No direction is claimed until this horizon's calibration has run; the base and its range are shown."),
    "insufficient": ("Too little history", "Too few past forecasts to judge a direction; the base and its range are shown, unvalidated."),
    "range_only": ("Range available, direction unsupported",
                   "No direction beat assuming no move on held-out dates; the validated range is the useful part."),
    "no_edge": ("No validated directional edge", "No direction beat assuming no move on held-out dates, and the range is not validated."),
    "edge": ("Validated directional edge", "A direction beat assuming no move on held-out dates; the range is not validated."),
    "edge_and_range": ("Forecast and range validated", "A direction beat assuming no move on held-out dates, and the band held its coverage."),
}


class PresentationIn(C.Strict):
    """Only the fields sent are changed; `null` clears one (a pinned comparison, say)."""
    selected_version_id: str | None = Field(default=None, max_length=64)
    selected_case_key: Literal["central", "bullish", "bearish"] | None = None
    compare_version_id: str | None = Field(default=None, max_length=64)
    visible_layers: dict[str, bool] | None = None
    chart_focus: Literal["base", "cases", "decision", "compare"] | None = None


def _f(x: Any) -> float | None:
    try:
        v = float(x)
    except (TypeError, ValueError):
        return None
    return v if v == v and abs(v) != float("inf") else None


def _entry(row: dict[str, Any], legs: list[dict[str, Any]]) -> dict[str, Any] | None:
    """What opening it costs: an option structure's net debit or credit at the legs' midpoints, or the share price."""
    if legs and any(lg.get("option_type") for lg in legs):
        mids = [_f(lg.get("mid")) for lg in legs]
        if any(m is None for m in mids):
            return None
        net = sum((1 if lg.get("side") == "buy" else -1) * m * float(lg.get("contracts") or 1) * 100.0 for lg, m in zip(legs, mids))
        return {"kind": "debit" if net >= 0 else "credit", "value": round(abs(net), 2)}
    px = _f(row.get("entry"))
    return {"kind": "price", "value": px} if px is not None else None


def headline(fc: dict[str, Any] | None) -> dict[str, Any]:
    """What the base forecast may claim (4.7, part IX 10.4): a flat base is never shown as if it were a prediction."""
    if not fc:
        return {"kind": "none", "label": "No forecast", "text": "No forecast could be made: see Evidence.", "flat": False}
    rd = fc.get("readiness") or {}
    d = ((rd.get("directional") or {}).get("status")) or ("pending" if not rd else "insufficient")
    r = ((rd.get("range") or {}).get("status")) or ""
    if d == "pending":
        kind = "pending"
    elif d == "insufficient":
        kind = "insufficient"
    elif d == "edge":
        kind = "edge_and_range" if r == "validated" else "edge"
    else:
        kind = "range_only" if r == "validated" else "no_edge"
    label, text = HEADLINES[kind]
    path = (fc.get("base") or {}).get("path_pct") or []
    last = _f(path[-1]) if path else None
    flat = last is not None and abs(last) < 0.005
    if flat and kind not in ("edge", "edge_and_range"):
        text = f"The base path is flat by design: {text[0].lower()}{text[1:]}"
    return {"kind": kind, "label": label, "text": text, "flat": flat, "directional": d, "range": r or None,
            "readiness_label": rd.get("label")}


def comparable(a: dict[str, Any] | None, b: dict[str, Any] | None) -> tuple[bool, str]:
    """Whether two versions can be drawn on one chart: the same symbol, anchor, sessions and horizon (4.7, 5.5)."""
    if not a or not b:
        return False, "no version to compare"
    if a.get("id") == b.get("id"):
        return False, "it is the version already selected"
    if a.get("symbol") != b.get("symbol"):
        return False, f"it is of another stock ({b.get('symbol')})"
    if int(a.get("horizon_sessions") or 0) != int(b.get("horizon_sessions") or 0):
        return False, "its horizon differs"
    aa, ba = a.get("anchor") or {}, b.get("anchor") or {}
    if aa.get("bar_date") != ba.get("bar_date") or abs((_f(aa.get("price")) or 0) - (_f(ba.get("price")) or 0)) > 1e-9:
        return False, "it starts from another anchor price or date"
    if [s.get("date") for s in a.get("sessions") or []] != [s.get("date") for s in b.get("sessions") or []]:
        return False, "its trading sessions differ"
    return True, ""


class ForecastState:
    def __init__(self, svc: Any):
        self.svc = svc

    # --- ownership ----------------------------------------------------------------------------------------------------
    def versions_of(self, study_id: str) -> list[dict[str, Any]]:
        rows = self.svc.db.query("""SELECT v.id, v.run_id, v.version, v.parent_version, v.created_at, v.origin, r.provider, r.seed_id
                                    FROM scenario_versions v JOIN scenario_runs r ON r.id = v.run_id WHERE r.study_id = ?
                                    ORDER BY v.created_at, v.rowid""", (study_id,))
        return [dict(r) for r in rows]

    def owned(self, study_id: str, version_id: str | None) -> str | None:
        """`version_id` when it is one of the study's own versions; anything else is refused, never silently replaced."""
        if not version_id:
            return None
        r = self.svc.db.one("""SELECT r.study_id FROM scenario_versions v JOIN scenario_runs r ON r.id = v.run_id WHERE v.id = ?""",
                            (version_id,))
        if r is None:
            raise RequestError(f"No scenario version {version_id}")
        if r["study_id"] != study_id:
            raise RequestError("That scenario version belongs to another study: select one of this study's own versions.")
        return version_id

    # --- the presentation state -----------------------------------------------------------------------------------------
    def presentation(self, study_id: str) -> dict[str, Any]:
        r = self.svc.db.one("SELECT state_json, updated_at FROM study_presentation WHERE study_id = ?", (study_id,))
        saved = json.loads(r["state_json"]) if r else {}
        layers = {**DEFAULT_LAYERS, **{k: bool(v) for k, v in (saved.get("visible_layers") or {}).items() if k in LAYERS}}
        return {"study_id": study_id, "selected_version_id": saved.get("selected_version_id"), "selected_case_key": saved.get("selected_case_key"),
                "compare_version_id": saved.get("compare_version_id"), "visible_layers": layers, "chart_focus": saved.get("chart_focus") or "cases",
                "updated_at": r["updated_at"] if r else None, "stored": r is not None}

    def save_presentation(self, study_id: str, body: PresentationIn) -> dict[str, Any]:
        if self.svc.db.one("SELECT 1 FROM studies WHERE id = ?", (study_id,)) is None:
            raise RequestError(f"No study {study_id}")
        given = body.model_fields_set
        r = self.svc.db.one("SELECT state_json FROM study_presentation WHERE study_id = ?", (study_id,))
        saved = json.loads(r["state_json"]) if r else {}
        if "selected_version_id" in given:
            saved["selected_version_id"] = self.owned(study_id, body.selected_version_id)
        if "compare_version_id" in given:
            saved["compare_version_id"] = self.owned(study_id, body.compare_version_id)
        if "selected_case_key" in given:
            saved["selected_case_key"] = body.selected_case_key
        if "chart_focus" in given:
            saved["chart_focus"] = body.chart_focus
        if "visible_layers" in given and body.visible_layers is not None:
            bad = sorted(k for k in body.visible_layers if k not in LAYERS)
            if bad:
                raise RequestError(f"visible_layers: unknown layer(s) {', '.join(bad)}; the layers are {', '.join(LAYERS)}")
            saved["visible_layers"] = {**(saved.get("visible_layers") or {}), **{k: bool(v) for k, v in body.visible_layers.items()}}
        if saved.get("compare_version_id") and saved.get("compare_version_id") == saved.get("selected_version_id"):
            saved["compare_version_id"] = None
        now = C.utc_now()
        with self.svc.db.tx() as cur:
            cur.execute("""INSERT INTO study_presentation (study_id, state_json, updated_at) VALUES (?, ?, ?)
                           ON CONFLICT(study_id) DO UPDATE SET state_json = excluded.state_json, updated_at = excluded.updated_at""",
                        (study_id, json.dumps(saved), now))
        return self.presentation(study_id)

    # --- the bundle -------------------------------------------------------------------------------------------------------
    def _version(self, vid: str | None) -> dict[str, Any] | None:
        sc = getattr(self.svc, "scenarios", None)
        if not vid or sc is None:
            return None
        try:
            return sc.version(vid)
        except RequestError:
            return None

    def _assessment(self, version_id: str) -> dict[str, Any] | None:
        r = self.svc.db.one("SELECT * FROM scenario_assessments WHERE version_id = ? ORDER BY created_at DESC, rowid DESC LIMIT 1", (version_id,))
        if r is None:
            return None
        return {**json.loads(r["assessment_json"]), "id": r["id"], "status": r["status"], "valuation_id": r["valuation_id"],
                "made_at": r["created_at"], "version_id": r["version_id"]}

    def _decision_of_version(self, study_id: str, version_id: str) -> dict[str, Any] | None:
        r = self.svc.db.one("SELECT * FROM scenario_decisions WHERE study_id = ? AND version_id = ? ORDER BY created_at DESC, rowid DESC LIMIT 1",
                            (study_id, version_id))
        if r is None:
            return None
        return {**json.loads(r["decision_json"]), "notice_state": r["notice_state"], "opportunity_id": r["opportunity_id"]}

    def _latest_decision(self, study_id: str) -> dict[str, Any] | None:
        r = self.svc.db.one("SELECT id, version_id FROM scenario_decisions WHERE study_id = ? ORDER BY created_at DESC, rowid DESC LIMIT 1",
                            (study_id,))
        return dict(r) if r else None

    def _valuation_of(self, version_id: str, preferred: str | None = None) -> dict[str, Any] | None:
        from . import valuation as V
        val = None
        if preferred:
            try:
                val = V.load(self.svc, preferred)
            except RequestError:
                val = None
        if val is None or val.get("version_id") != version_id:
            val = V.latest_for_version(self.svc, version_id)
        if val is not None and val.get("version_id") != version_id:          # never another version's valuation
            return None
        return val

    def lifecycle(self, got: dict[str, Any], p: dict[str, Any] | None) -> dict[str, Any]:
        st = got.get("status")
        if st == "running":
            state = "evidence_running"
        elif st == "failed":
            state = "study_failed"
        elif not p:
            state = "baseline_complete"
        else:
            status, stage = p.get("status"), p.get("stage")
            run = p.get("run") or {}
            if status == "awaiting_approval":
                state = "awaiting_approval"
            elif status == "running" and stage in ("evidence", "provider"):
                state = "simulation_running" if run.get("status") == "running" else "simulation_queued"
            elif status == "running" and stage == "paths":
                state = "valuing"
            elif status == "running":
                state = "valuing"
            elif status == "complete":
                state = "decision_ready" if p.get("decision") else "version_saved"
            elif status == "canceled":
                state = "simulation_canceled"
            elif status == "needs_attention":
                state = "needs_attention"
            else:
                state = "simulation_failed"
        running = state in ("evidence_running", "simulation_queued", "simulation_running", "valuing")
        detail = None
        if state in ("simulation_failed", "needs_attention", "simulation_canceled") and p:
            detail = p.get("error")
        elif state == "baseline_complete":
            detail = "The quantitative study is complete; no scenario simulation was run for it."
        return {"state": state, "label": LIFECYCLE_WORDS[state], "detail": detail, "running": running,
                "baseline_ready": st in ("complete", "partial")}

    def strategies(self, view: dict[str, Any], routes: dict[str, Any], val: dict[str, Any] | None) -> list[dict[str, Any]]:
        """One table of the study's candidates under the active context: the study's base-path figures, and the
        selected version's case results when (and only when) its own valuation exists."""
        sg = view.get("strategies") or {}
        by_val = {s["key"]: s for s in (val or {}).get("structures") or []}
        failed = {f.get("key"): f for f in (val or {}).get("failed") or []}
        rows = []
        for c in sg.get("candidates") or []:
            s = by_val.get(c["key"])
            legs = [{k: lg.get(k) for k in ("option_symbol", "option_type", "strike", "expiration", "side", "contracts", "bid", "ask", "mid")}
                    for lg in (s or c).get("legs") or []]
            entry = _entry(s or c, legs)
            rows.append({
                "key": c["key"], "label": c.get("label"), "instrument": c.get("instrument"), "direction": (s or {}).get("direction") or c.get("direction"),
                "legs": legs, "expiration": min((lg["expiration"] for lg in legs if lg.get("expiration")), default=None),
                "entry": entry, "costs": _f((s or c).get("costs")), "best": c["key"] == sg.get("best"),
                "base_pnl": _f((c.get("expected_pnl") or {}).get("base")),
                "by_case": {k: {"net": _f(v.get("expected_pnl")), "worst_tenth": _f(v.get("worst_tenth"))} for k, v in (s or {}).get("by_case", {}).items()},
                "worst_case": _f((s or {}).get("worst_case")),
                "max_loss": _f((s or {}).get("max_loss")) if s else _f(c.get("theoretical_max_loss")),
                "unlimited_loss": bool((s or c).get("unlimited_loss")),
                "capital": _f((s or {}).get("capital")) if s else _f(c.get("capital_required")),
                "quality": ((s or {}).get("quality") or {}).get("tier") if s else None,
                "quality_words": ((s or {}).get("quality") or {}).get("words") or [],
                "eval_date": (s or {}).get("eval_date"), "snapshot_at": (val or {}).get("snapshot_at") if s else None,
                "not_valued": (failed.get(c["key"]) or {}).get("error") if val is not None and s is None else None,
                "route": (routes.get(c["key"]) or {}).get("route"), "route_label": (routes.get(c["key"]) or {}).get("label"),
            })
        return rows

    def bundle(self, study_id: str, version_id: str | None = None, compare_id: str | None = None) -> dict[str, Any]:
        research = getattr(self.svc, "research3", None)
        if research is None:
            raise RequestError("Research is not running in this app")
        got = research.get(study_id)
        pres = self.presentation(study_id)
        out: dict[str, Any] = {"contract": BUNDLE_VERSION, "study_id": study_id, "symbol": got["symbol"], "status": got["status"],
                               "horizon_sessions": got["horizon_days"], "thesis": got.get("thesis"), "direction": got.get("direction"),
                               "created_at": got["created_at"], "error": got.get("error"), "presentation": pres}
        mode_row = self.svc.db.one("SELECT mode FROM studies WHERE id = ?", (study_id,))
        out["data_mode"] = mode_row["mode"] if mode_row else None
        p = got.get("pipeline")
        out["pipeline"] = p
        out["lifecycle"] = self.lifecycle(got, p)
        if "study" not in got:
            out.update(base=None, versions=[], unversioned_runs=[], selected_context={"version_id": None, "case_key": None, "mode": "baseline",
                                                                  "comparison_version_id": None, "official_version_id": None},
                       selected_version=None, comparison=None, valuation=None, decision=None, strategies=[], automatic_run=None,
                       anchor=None, sessions=[], historical=False, evidence_cutoff=None, provenance={})
            return out
        st, view, frame, routes = got["study"], got.get("view") or {}, got.get("frame") or {}, got.get("routes") or {}
        fc = st.get("forecast")
        anchor, sessions = frame.get("anchor"), frame.get("sessions") or []
        today = datetime.now(NY).date()
        historical = bool(sessions) and date.fromisoformat(sessions[-1]["date"]) < today
        out.update(anchor=anchor, sessions=sessions, historical=historical,
                   historical_note=(f"Its last session ({sessions[-1]['date']}) has passed: this is the study's original forecast, at its "
                                    "original anchor and dates. Current prices are on the chart separately." if historical else None))
        a0 = _f((anchor or {}).get("price"))
        price = lambda xs: [a0 * (1 + float(x) / 100.0) for x in xs] if a0 else []     # noqa: E731
        if fc:
            b = fc.get("base") or {}
            hl = headline(fc)
            cal = (fc.get("calibration") or {}).get("status")
            out["base"] = {"path_pct": b.get("path_pct") or [], "low_pct": b.get("low_pct") or [], "high_pct": b.get("high_pct") or [],
                           "price": price(b.get("path_pct") or []), "low_price": price(b.get("low_pct") or []), "high_price": price(b.get("high_pct") or []),
                           "terminal": {"pct": _f((b.get("path_pct") or [None])[-1]), "price": (price(b.get("path_pct") or []) or [None])[-1]},
                           "calibrated": cal == "calibrated",
                           "range_words": "the calibrated 80% band" if cal == "calibrated" else "the model-implied 10th to 90th percentile range",
                           "headline": hl, "directional_readiness": (fc.get("readiness") or {}).get("directional"),
                           "range_readiness": (fc.get("readiness") or {}).get("range"), "model_version": fc.get("model_version")}
        else:
            out["base"] = None
        from .scenarios import SCENARIO_HORIZONS
        sc = getattr(self.svc, "scenarios", None)
        enabled = bool(sc is not None and sc.enabled())
        h = int(got["horizon_days"])
        out["scenarios"] = {"enabled": enabled, "supported": enabled and h in SCENARIO_HORIZONS,
                            "reason": None if enabled and h in SCENARIO_HORIZONS else
                            ("Catalyst Scenarios are switched off (FEATURE_CATALYST_SCENARIOS=0)." if not enabled else
                             f"Scenario simulations run on {' or '.join(map(str, SCENARIO_HORIZONS))}-session studies; this is a "
                             f"{h}-session study, so it stays baseline only.")}
        # The official automatic run and what it produced.
        official = (p or {}).get("version_id")
        decision_row = (p or {}).get("decision") or {}
        out["automatic_run"] = None if not p else {
            "run_id": (p.get("run") or {}).get("id"), "provider": p.get("provider"), "provider_label": p.get("provider_label"),
            "status": p.get("status"), "stage": p.get("stage"), "catalyst": p.get("catalyst"), "catalyst_basis": p.get("catalyst_basis"),
            "official_version_id": official, "valuation_id": (p.get("valuation") or {}).get("id"), "decision_id": decision_row.get("id"),
            "demo": bool(p.get("demo"))}
        pl = getattr(self.svc, "pipeline", None)
        raw_p = (pl.load(study_id) if (pl is not None and p) else None) or {}
        promoted = {x.get("version_id") for x in raw_p.get("promotions") or []}
        # Every version of the study, in the order made, with what it is and how the scorecard treats it.
        raw = self.versions_of(study_id)
        from .forward import window_start
        try:
            opens = window_start(sessions) if sessions else None
        except (ValueError, KeyError):
            opens = None
        versions = []
        for i, r in enumerate(raw, 1):
            origin = r["origin"] or "manual"
            if r["id"] == official:
                kind = "promoted" if r["id"] in promoted else "official"
            elif origin == "control":
                kind = "control"
            elif origin == "auto":
                kind = "previous"
            else:
                kind = "exploratory"
            made = datetime.fromisoformat(r["created_at"].replace("Z", "+00:00"))
            late = bool(opens and made >= opens)
            grading = ("frozen for the forward scorecard" if origin in ("auto", "control") else
                       "not graded: an exploratory version never enters the forward scorecard")
            if late and origin in ("auto", "control"):
                grading = "late: made after the window opened, not scored"
            parent = next((x["id"] for x in raw if x["run_id"] == r["run_id"] and r["parent_version"] and x["version"] == r["parent_version"]), None)
            versions.append({"id": r["id"], "n": i, "label": f"v{i}", "version": r["version"], "run_id": r["run_id"], "origin": origin,
                             "kind": kind, "kind_label": KIND_WORDS[kind], "official": r["id"] == official, "provider": r["provider"],
                             "parent_id": parent, "created_at": r["created_at"], "late": late, "grading": grading})
        out["versions"] = versions
        by_id = {v["id"]: v for v in versions}
        # Cases imported by hand that have no version yet: the Lab starts them from the packet's suggested sizes.
        out["unversioned_runs"] = []
        if sc is not None and enabled:
            have = {v["run_id"] for v in versions}
            for r in self.svc.db.query("SELECT id, seed_id, provider, created_at, cases_json FROM scenario_runs WHERE study_id = ? AND status = 'complete' "
                                       "ORDER BY created_at DESC LIMIT 5", (study_id,)):
                if r["id"] in have:
                    continue
                try:
                    seed = sc._seed(r["seed_id"])
                    cs = json.loads(r["cases_json"] or "[]")
                    out["unversioned_runs"].append({"run_id": r["id"], "provider": r["provider"], "created_at": r["created_at"],
                                                    "cases": [{k: c.get(k) for k in ("key", "label", "trigger", "invalidation", "stock_direction")} for c in cs],
                                                    "suggested": sc.suggested(seed, cs),
                                                    "factors": [{k: x.get(k) for k in ("key", "label", "unit")} for x in (seed.get("factors") or {}).get("rows") or []],
                                                    "sessions": seed.get("sessions"), "catalyst_session": seed.get("catalyst_session"),
                                                    "volatility": {"event_move_pct": (seed.get("volatility") or {}).get("event_move_pct")}})
                except (RequestError, ValueError, KeyError):
                    continue
        # The selected context: the request's, else the stored preference, else the official version. A version that is
        # not the study's own is refused (an explicit request) or dropped (a stale preference).
        want = version_id if version_id is not None else pres.get("selected_version_id")
        if version_id is not None:
            self.owned(study_id, version_id)
        if want and want not in by_id:
            want = None
        sel_id = want or official or (versions[-1]["id"] if versions and not p else None)
        cmp_want = compare_id if compare_id is not None else pres.get("compare_version_id")
        if compare_id:
            self.owned(study_id, compare_id)
        if cmp_want and cmp_want not in by_id:
            cmp_want = None
        selected = self._version(sel_id) if sel_id else None
        meta = by_id.get(sel_id) if sel_id else None
        mode = meta["kind"] if meta else "baseline"
        case_key = pres.get("selected_case_key")
        if selected and case_key not in {c["key"] for c in selected.get("cases") or []}:
            case_key = None
        out["selected_context"] = {"version_id": selected["id"] if selected else None, "case_key": case_key, "mode": mode,
                                   "comparison_version_id": None, "official_version_id": official,
                                   "is_official": bool(selected and selected["id"] == official)}
        seed = None
        if selected:
            seed_row = self.svc.db.one("SELECT packet_json, cutoff FROM scenario_seeds WHERE id = ?", (selected.get("seed_id"),))
            seed = json.loads(seed_row["packet_json"]) if seed_row and seed_row["packet_json"] else None
            out["selected_version"] = {
                "id": selected["id"], "n": meta["n"], "label": meta["label"], "kind": meta["kind"], "kind_label": meta["kind_label"],
                "origin": selected.get("origin"), "version": selected["version"], "run_id": selected["run_id"], "parent_id": meta["parent_id"],
                "created_at": selected["created_at"], "assumptions": selected.get("assumptions") or [], "cases": selected.get("cases") or [],
                "base": selected.get("base"), "catalyst": selected.get("catalyst"), "catalyst_session": selected.get("catalyst_session"),
                "catalyst_timing_note": (seed or {}).get("catalyst_timing_note"),
                "sources": [{k: s.get(k) for k in ("id", "title", "kind", "published_at", "url")} for s in (seed or {}).get("sources") or []],
                "factors": [{k: r.get(k) for k in ("key", "label", "unit", "redundant")} for r in ((seed or {}).get("factors") or {}).get("rows") or []],
                "volatility": {"event_move_pct": ((seed or {}).get("volatility") or {}).get("event_move_pct")},
                "anchor": selected.get("anchor"), "sessions": selected.get("sessions"), "band_note": selected.get("band_note"),
                "stale": selected.get("stale"), "stale_note": selected.get("stale_note"), "calc_version": selected.get("calc_version"),
                "grading": meta["grading"], "late": meta["late"]}
            out["evidence_cutoff"] = seed_row["cutoff"] if seed_row else st["created_at"]
        else:
            out["selected_version"] = None
            out["evidence_cutoff"] = st["created_at"]
        # One earlier version, for a read-only comparison.
        out["comparison"] = None
        if cmp_want and selected and cmp_want != selected["id"]:
            other = self._version(cmp_want)
            ok, why = comparable(selected, other)
            om = by_id[cmp_want]
            out["comparison"] = {"version_id": cmp_want, "label": om["label"], "kind": om["kind"], "kind_label": om["kind_label"], "comparable": ok,
                                 "reason": why or None,
                                 "cases": [{k: c.get(k) for k in ("key", "label", "quantified", "price", "return_pct", "terminal", "trigger")}
                                           for c in (other or {}).get("cases") or []] if ok else [],
                                 "differences": [{"key": c["key"], "label": c["label"], "selected_pct": c["terminal"]["return_pct"],
                                                  "compare_pct": next((o["terminal"]["return_pct"] for o in (other or {}).get("cases") or [] if o["key"] == c["key"]), None)}
                                                 for c in selected.get("cases") or []]}
            out["selected_context"]["comparison_version_id"] = cmp_want
        # The selected version's own valuation and decision only.
        val, decision, note = None, None, None
        if selected:
            preferred = (out["automatic_run"] or {}).get("valuation_id") if selected["id"] == official else None
            val = self._valuation_of(selected["id"], preferred)
            if selected["id"] == official or meta["kind"] == "previous":
                d = self._decision_of_version(study_id, selected["id"])
                if d is not None:
                    latest = self._latest_decision(study_id) or {}
                    current = d.get("id") == latest.get("id") and selected["id"] == official
                    decision = {**d, "kind": "official", "research_only": False, "current": current}
                    if not current:
                        note = "This is an earlier decision, superseded by a later one: it is shown for the record, not as a current setup."
            else:
                a = self._assessment(selected["id"])
                if a is not None:
                    decision = {**a, "kind": "exploratory", "research_only": True, "current": False}
                latest = self._latest_decision(study_id)
                if latest:
                    on = by_id.get(latest["version_id"])
                    note = (f"The official decision belongs to {on['label'] if on else 'another version'} ({(on or {}).get('kind_label', 'official')}), "
                            "not to this one: it is not shown as this version's.")
            if decision is not None:
                s = decision.get("setup") or {}
                focus = None
                if decision.get("status") in ("qualified", "watch") and decision.get("version_id") == selected["id"]:
                    focus = s.get("case") or ("central" if s else None)
                    quantified = {c["key"] for c in selected.get("cases") or [] if c.get("quantified")}
                    if focus not in quantified:
                        focus = None
                decision["focus_case"] = focus
                if val is not None and decision.get("valuation_id") and decision.get("valuation_id") != val.get("id"):
                    # The decision was made from an earlier valuation of the same version: say so, and keep the one it used.
                    try:
                        from . import valuation as V
                        used = V.load(self.svc, decision["valuation_id"])
                        if used.get("version_id") == selected["id"]:
                            decision["valuation_note"] = ("Decided from the valuation of "
                                                          f"{used.get('snapshot_at')}; a later valuation exists for this version.")
                    except RequestError:
                        pass
        out["valuation"] = val
        out["decision"] = decision
        out["decision_note"] = note
        out["strategies"] = self.strategies(view, routes, val)
        out["provenance"] = {"quantitative_model": (fc or {}).get("model_version"), "scenario_provider": (p or {}).get("provider"),
                             "calculation_version": (selected or {}).get("calc_version"), "evidence_cutoff": out.get("evidence_cutoff"),
                             "data_source": view.get("data_source"), "bundle": BUNDLE_VERSION}
        out["presentation"] = {**pres, "selected_version_id": out["selected_context"]["version_id"],
                               "compare_version_id": out["selected_context"]["comparison_version_id"], "selected_case_key": case_key}
        return out


def install(svc: Any) -> None:
    from ..api_v3 import register
    svc.forecast_state = fs = ForecastState(svc)

    def routes(router: Any, svc: Any) -> None:
        @router.get("/studies/{study_id}/result")
        def result(study_id: str, version: str | None = Query(None, max_length=64), compare: str | None = Query(None, max_length=64)) -> dict[str, Any]:
            return fs.bundle(study_id, version_id=version, compare_id=compare)

        @router.get("/studies/{study_id}/presentation")
        def presentation(study_id: str) -> dict[str, Any]:
            if svc.db.one("SELECT 1 FROM studies WHERE id = ?", (study_id,)) is None:
                raise RequestError(f"No study {study_id}")
            return fs.presentation(study_id)

        @router.post("/studies/{study_id}/presentation")
        def presentation_save(study_id: str, body: PresentationIn) -> dict[str, Any]:
            return fs.save_presentation(study_id, body)

    register(svc, routes)


__all__ = ["ForecastState", "PresentationIn", "install", "headline", "comparable", "LAYERS", "BUNDLE_VERSION"]
