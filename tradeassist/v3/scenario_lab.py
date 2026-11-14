"""4.7 Scenario Lab: one editor for conditional paths, with a preview that saves nothing and an exploratory evaluation that
publishes nothing.

A study's cases (the automatic MiroFish run's, another provider's, or imported by hand) are edited here, always against
the study's own frozen evidence and base forecast, with the same checks a saved version gets
(`Scenarios.checked`: units, sessions, the packet's sources and factor fit, sizes credited to a rule being the rule's own).

- **Preview** (`POST /v3/studies/{id}/scenario-lab/preview`) computes the paths and returns them. It writes nothing:
  no version, no valuation, no decision or assessment, no notice, no job, no provider request. Every reply echoes the
  caller's `generation`, so a late preview cannot overwrite newer inputs on the screen.
- **Save version** (`.../scenario-lab/versions`) keeps the assumptions as a new immutable version (origin "manual", its
  parent recorded), values the study's own candidate strategies under it from one fresh quote snapshot, and runs the
  Opportunity Engine's checks on that valuation as an **exploratory assessment** (migration 31): research only. It is
  not written to `scenario_decisions`, not published to Discover, not notified, not frozen for the forward scorecard,
  and it never changes the study's official version or decision. If the valuation fails, the version is kept and its
  financial comparison is marked incomplete.
- **Promote** (`.../scenario-lab/promote`) is the only way an edited version can become official, and only when asked
  with `confirm`. A version qualifies for promotion only when it meets the standard the automatic run holds itself to:
  every size comes from the packet's own deterministic rules (the options-implied move, the comparable situations'
  tail, the options-implied standard deviation, a factor's own volatility) for a driver of that case that cites the
  packet, in that driver's direction. A user stress, a provider's own number or an uncited driver stays exploratory.
  Promotion then revalues the strategies from a fresh snapshot and decides through the normal official path (fresh
  quote checks, verified triggers for that version). It is never frozen for the forward scorecard: that record stays
  what the automatic run predicted before the window opened.

Nothing here places an order.
"""

from __future__ import annotations

import json
import logging
from datetime import date, datetime
from typing import Any

from pydantic import Field

from .. import contracts as C
from ..optionmath import NY
from ..providers.base import DataError
from ..service import RequestError

log = logging.getLogger(__name__)

RULE_SOURCES = ("options_implied_move", "analog_tail", "implied_sensitivity", "factor_volatility")
ASSESS_LABELS = {"qualified": "Would qualify under these assumptions", "watch": "Would be a watch under these assumptions",
                 "no_setup": "No setup under these assumptions", "incomplete": "Not assessed: the strategies could not be valued"}
RESEARCH_NOTE = ("Research only: an exploratory version's checks show what would pass or fail under its assumptions. It is not a "
                 "decision: it is never published to Discover, never notified, never scored on the forward record, and it "
                 "changes nothing official.")


class PreviewIn(C.Strict):
    from_version_id: str = Field(min_length=1, max_length=64)
    cases: list[C.CaseAssumptions] = Field(min_length=1, max_length=3)
    generation: int | None = Field(default=None, ge=0)


class LabSaveIn(C.Strict):
    from_version_id: str = Field(min_length=1, max_length=64)
    cases: list[C.CaseAssumptions] = Field(min_length=1, max_length=3)


class PromoteIn(C.Strict):
    version_id: str = Field(min_length=1, max_length=64)
    confirm: bool = False


def promotion_problems(seed: dict[str, Any], cases: dict[str, dict[str, Any]], assumptions: list[dict[str, Any]]) -> list[str]:
    """Why a version may not become official: every size must come from a deterministic rule for a cited driver of its
    case, going that driver's way, as the automatic run's do (pipeline.auto_assumptions). Empty when it may."""
    out: list[str] = []
    for a in assumptions:
        c = cases.get(a["key"]) or {}
        label = C.CASE_LABELS.get(a["key"], a["key"])
        drivers = [d for d in c.get("drivers") or [] if d.get("source_ids")]
        iss = a.get("issuer")
        if iss:
            want = "up" if float(iss["value_pct"]) > 0 else "down"
            if iss.get("source") not in RULE_SOURCES[:3]:
                out.append(f"{label}: the stock's own move is {str(iss.get('source')).replace('_', ' ')}, not one of the packet's rules")
            elif not any(d["type"] == "issuer_event" and d.get("direction") == want for d in drivers):
                out.append(f"{label}: no cited issuer driver of this case goes {want}")
        for f in a.get("factors") or []:
            if f.get("source") != "factor_volatility":
                out.append(f"{label}: the {f['key']} shock is {str(f.get('source')).replace('_', ' ')}, not the factor's own volatility")
            elif not any(d["type"] in ("market", "sector", "macro") and d.get("factor") == f["key"] for d in drivers):
                out.append(f"{label}: no cited driver of this case names the {f['key']} factor")
        disp = a.get("dispersion")
        if disp and disp.get("source") not in ("implied_sensitivity", "options_implied_move"):
            out.append(f"{label}: its extra uncertainty is {str(disp.get('source')).replace('_', ' ')}, not from the options market")
        if disp and not disp.get("source_ids"):
            out.append(f"{label}: its extra uncertainty cites no source in the packet")
    return out


class ScenarioLab:
    def __init__(self, svc: Any):
        self.svc = svc

    @property
    def sc(self) -> Any:
        sc = getattr(self.svc, "scenarios", None)
        if sc is None:
            raise RequestError("Catalyst Scenarios are not running in this app")
        sc._on()
        return sc

    def _source(self, study_id: str, version_id: str) -> tuple[Any, dict[str, Any]]:
        """The run and version an edit starts from, both the study's own."""
        self.svc.forecast_state.owned(study_id, version_id)
        v = self.sc.version(version_id)
        r = self.sc._run_row(v["run_id"])
        if r["study_id"] != study_id:
            raise RequestError("That scenario version belongs to another study.")
        return r, v

    # --- preview: nothing is written ------------------------------------------------------------------------------------
    def preview(self, study_id: str, body: PreviewIn) -> dict[str, Any]:
        r, v = self._source(study_id, body.from_version_id)
        seed, cases, assumptions = self.sc.checked(r, C.ScenarioVersionIn(parent_version=v["version"], cases=body.cases))
        result = self.sc.compute(seed, cases, assumptions)
        return {"preview": True, "saved": False, "generation": body.generation, "study_id": study_id, "run_id": r["id"],
                "from_version_id": v["id"], "assumptions": assumptions, **result,
                "note": "Unsaved preview: computed from the study's frozen evidence and base. Nothing is stored, valued, published or "
                        "sent to the provider; Save version keeps it."}

    # --- save: a version, its valuation, and a research-only assessment ---------------------------------------------------
    def save(self, study_id: str, body: LabSaveIn) -> dict[str, Any]:
        r, v = self._source(study_id, body.from_version_id)
        ver = self.sc.create_version(r["id"], C.ScenarioVersionIn(parent_version=v["version"], cases=body.cases), origin="manual")
        self.svc.forecast_state.save_presentation(study_id, _sel(ver["id"]))
        out = self.assess(study_id, ver)
        return {"version": ver, **out}

    def value_again(self, study_id: str, version_id: str) -> dict[str, Any]:
        """A fresh valuation and assessment of an exploratory version. The official version is valued again through the
        pipeline (Research → Value again), never here."""
        _, ver = self._source(study_id, version_id)
        p = self.svc.pipeline.load(study_id) if getattr(self.svc, "pipeline", None) else None
        if (p or {}).get("version_id") == version_id:
            raise RequestError("That is the official version: value it again from the study's Scenarios panel (one official path).")
        if ver.get("origin") in ("auto", "control"):
            raise RequestError("An automatic run's version is valued by its own run; edit it to explore other assumptions.")
        return {"version": ver, **self.assess(study_id, ver)}

    def assess(self, study_id: str, ver: dict[str, Any]) -> dict[str, Any]:
        from . import valuation as V
        study = self.svc.research3.get(study_id)
        val, vid, problems = None, None, []
        try:
            val, snap = V.value(self.svc, study, ver)
            vid = V.save(self.svc, val, snap)
            val["id"] = vid
        except (RequestError, DataError, ValueError) as exc:
            problems.append(str(exc)[:300])
        if val is None or val.get("status") == "failed":
            a = {"status": "incomplete", "label": ASSESS_LABELS["incomplete"], "problems": problems or list((val or {}).get("problems") or []),
                 "setup": None, "reasons": [], "answers": None}
        else:
            a = self.svc.opportunities.evaluate_study(study_id, ver, val)
            a["label"] = ASSESS_LABELS[a["status"]]
        a.update(research_only=True, exploratory=True, note=RESEARCH_NOTE, version_id=ver["id"], valuation_id=vid)
        aid = C.new_id("sasm")
        with self.svc.db.tx() as cur:
            cur.execute("""INSERT INTO scenario_assessments (id, study_id, version_id, valuation_id, created_at, status, assessment_json)
                           VALUES (?, ?, ?, ?, ?, ?, ?)""", (aid, study_id, ver["id"], vid, C.utc_now(), a["status"], json.dumps(a, default=str)))
        self.svc.bus.publish("scenario.assessed", study_id, version_id=ver["id"], valuation_id=vid, status=a["status"])
        complete = bool(vid) and (val or {}).get("status") != "failed"
        return {"valuation": val if vid else None, "valuation_complete": complete,
                "valuation_problems": [] if complete else (problems or list((val or {}).get("problems") or [])),
                "assessment": {**a, "id": aid}}

    # --- promote: the explicit, checked way to make an edit official ------------------------------------------------------
    def promotion(self, study_id: str, version_id: str) -> dict[str, Any]:
        """What promoting `version_id` would need, without doing it."""
        r, ver = self._source(study_id, version_id)
        seed = self.sc._seed(r["seed_id"])
        cases = {c["key"]: c for c in json.loads(r["cases_json"] or "[]")}
        problems = []
        p = self.svc.pipeline.load(study_id) if getattr(self.svc, "pipeline", None) else None
        if p is None:
            problems.append("this study has no automatic scenario run: start one (Run scenarios), then promote")
        elif p.get("status") in ("running", "awaiting_approval"):
            problems.append("the automatic run is still going: wait for it to finish")
        elif p.get("status") == "canceled":
            problems.append("the automatic run was canceled: Retry it first")
        elif p.get("version_id") == version_id:
            problems.append("it is already the official version")
        if ver.get("origin") != "manual":
            problems.append("only a version you edited can be promoted; an automatic run's version is official only for its own run")
        last = date.fromisoformat(ver["sessions"][-1]["date"])
        if last < datetime.now(NY).date():
            problems.append(f"its last session ({last.isoformat()}) has passed")
        problems += promotion_problems(seed, cases, ver.get("assumptions") or [])
        return {"version_id": version_id, "allowed": not problems, "problems": problems,
                "will": ["revalue the study's strategies from a fresh quote snapshot",
                         "decide through the Opportunity Engine's official checks (fresh, real-time quotes; verified triggers for this version)",
                         "publish or withdraw the Discover observation and notify as an automatic decision would",
                         "keep the forward scorecard as it is: only the automatic run's predictions are scored"]}

    def promote(self, study_id: str, body: PromoteIn) -> dict[str, Any]:
        check = self.promotion(study_id, body.version_id)
        if not body.confirm:
            return {"promotion": check, "pipeline": None}
        if not check["allowed"]:
            raise RequestError("This version cannot become official: " + "; ".join(check["problems"]) + ".")
        pl = self.svc.pipeline
        p = pl.load(study_id) or {}
        ver = self.sc.version(body.version_id)
        promotions = list(p.get("promotions") or []) + [{"version_id": ver["id"], "replaced": p.get("version_id"), "at": C.utc_now()}]
        pl._update(study_id, version_id=ver["id"], promotions=promotions, valuation_id=None)
        self.svc.forecast_state.save_presentation(study_id, _sel(ver["id"]))
        pl.revalue(study_id, ver)
        self.svc.bus.publish("scenario.promoted", study_id, version_id=ver["id"])
        return {"promotion": {**check, "done": True}, "pipeline": pl.view(study_id)}


def _sel(version_id: str) -> Any:
    from .forecast_state import PresentationIn
    return PresentationIn(selected_version_id=version_id)


def install(svc: Any) -> None:
    from ..api_v3 import register
    svc.scenario_lab = lab = ScenarioLab(svc)

    def routes(router: Any, svc: Any) -> None:
        @router.post("/studies/{study_id}/scenario-lab/preview")
        def preview(study_id: str, body: PreviewIn) -> dict[str, Any]:
            return lab.preview(study_id, body)

        @router.post("/studies/{study_id}/scenario-lab/versions")
        def save(study_id: str, body: LabSaveIn) -> dict[str, Any]:
            return lab.save(study_id, body)

        @router.post("/studies/{study_id}/scenario-lab/versions/{version_id}/value")
        def value(study_id: str, version_id: str) -> dict[str, Any]:
            return lab.value_again(study_id, version_id)

        @router.get("/studies/{study_id}/scenario-lab/promotion")
        def promotion(study_id: str, version_id: str) -> dict[str, Any]:
            return lab.promotion(study_id, version_id)

        @router.post("/studies/{study_id}/scenario-lab/promote")
        def promote(study_id: str, body: PromoteIn) -> dict[str, Any]:
            return lab.promote(study_id, body)

    register(svc, routes)


__all__ = ["ScenarioLab", "PreviewIn", "LabSaveIn", "PromoteIn", "promotion_problems", "install", "RULE_SOURCES"]
