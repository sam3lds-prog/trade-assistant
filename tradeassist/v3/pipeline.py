"""The one-click Research pipeline (4.3): Run study → evidence and strategies → the scenario provider → price paths and
valuation → findings, with no export, upload, paste or apply step.

A study started with scenarios on (the Research setting, or `scenarios` on the request) records that intent when it
is created, with the request key of the click (a browser retry of the same click finds the same study). When the
quantitative study and its candidate strategies are saved, a continuation is queued on Research's own `scenarios`
worker (never in front of the Trading Desk), keyed by study, provider and configuration, so it runs once:

1. **start**: the evidence packet is frozen with `create_seed()` for an automatic catalyst (a verified event inside the
   horizon, else a recent sourced headline, else a clearly named hypothetical current-setup case) and a few words of
   strategy context (which kinds of strategy are compared and which moves matter to them; never contracts,
   quantities, the budget or the account), and a provider run is recorded;
2. **step**: the provider advances one bounded step at a time; every id it acknowledges is saved before the next step
   is queued, so a restart resumes from the recorded ids instead of paying for another run. A run that takes longer
   than MIROFISH_RUN_MINUTES is stopped; MIROFISH_CONCURRENCY runs go at once, the rest wait their turn;
3. **finish**: the report is normalized to the 4.1 case schema and checked whole against the frozen seed (a result
   for another packet is refused), the raw report and the parsed cases are kept apart, each case is sized only by the
   app's deterministic rules (`scenarios.rule_value`: the options-implied move, the analogs' tail, the options-implied
   standard deviation, a factor's own volatility), one version is saved automatically (origin "auto"), and the study's
   fixed candidate strategies are revalued under it from one quote snapshot (v3/valuation.py).

A provider failure leaves the finished quantitative study as it is, with the reason and a Retry. Cancel stops the
provider (MiroFish is asked to stop its simulation) and nothing that arrives later is applied. The 4.1 manual tools
stay available under Advanced, and any edit there is another version.
"""

from __future__ import annotations

import json
import logging
import math
import time
from datetime import date, datetime, timedelta, timezone
from typing import Any, Literal

from .. import contracts as C
from ..jobs import Deferred, Jobs
from ..optionmath import NY
from ..providers.base import DataError
from ..service import RequestError
from . import scenarios as SC
from . import valuation as V
from .scenario_providers import PINNED_UPSTREAM, provider_for

log = logging.getLogger(__name__)

PIPELINE_VERSION = "pipeline-4.3"
STAGES = ("evidence", "provider", "paths", "findings")
SETTINGS_KEY = "research:scenario_settings"
START, STEP, FINISH, RECOVER = "scenario.start", "scenario.step", "scenario.finish", "scenario.recover"
TERMINAL = ("complete", "failed", "canceled", "needs_attention")
MAX_STEPS = 2000
REPORT_LIMIT = 200_000
WAIT_TURN = 30.0                         # seconds a run waits for a free MiroFish slot before looking again


class ScenarioSettingsIn(C.Strict):
    provider: Literal["mirofish", "ai", "demo"] | None = None
    auto: bool | None = None


class PipelineStartIn(C.Strict):
    catalyst: C.CatalystSpec | None = None


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _ts(s: str | None) -> datetime | None:
    if not s:
        return None
    try:
        dt = datetime.fromisoformat(str(s).replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def stage_label(key: str, provider: str) -> str:
    return {"evidence": "Evidence and strategies", "provider": {"mirofish": "MiroFish", "ai": "Single AI call", "demo": "Demo provider"}.get(provider, provider),
            "paths": "Price paths and valuation", "findings": "Findings"}[key]


class Pipeline:
    def __init__(self, svc: Any):
        self.svc = svc

    # --- settings (Research → Scenario provider) -----------------------------------------------------------------
    def settings(self) -> dict[str, Any]:
        """The provider and the automatic-run switch: .env's SCENARIO_PROVIDER and SCENARIO_AUTO unless changed in
        Research settings. The MiroFish address, its caps and concurrency stay in .env (the address must be loopback)."""
        s = self.svc.settings
        saved = self.svc.db.cache_get(SETTINGS_KEY) or {}
        provider = saved.get("provider") or s.scenario_provider
        auto = bool(saved["auto"]) if "auto" in saved else bool(s.scenario_auto)
        return {"provider": provider, "auto": auto, "control": bool(s.scenario_control),
                "mirofish": {"url": s.mirofish_url, "max_rounds": s.mirofish_max_rounds, "run_minutes": s.mirofish_run_minutes,
                             "concurrency": s.mirofish_concurrency, "http_timeout": s.mirofish_http_timeout, "upstream": PINNED_UPSTREAM},
                "source": {"provider": "Research settings" if saved.get("provider") else "SCENARIO_PROVIDER in .env",
                           "auto": "Research settings" if "auto" in saved else "SCENARIO_AUTO in .env"},
                "horizons": list(SC.SCENARIO_HORIZONS), "enabled": self.scenarios().enabled()}

    def save_settings(self, provider: str | None, auto: bool | None) -> dict[str, Any]:
        saved = dict(self.svc.db.cache_get(SETTINGS_KEY) or {})
        if provider is not None:
            if provider not in ("mirofish", "ai", "demo"):
                raise RequestError("provider must be mirofish, ai or demo")
            if provider == "demo" and self.svc.market.info.mode != "demo":
                raise RequestError("The demo provider simulates nothing: it is only offered on demo data.")
            saved["provider"] = provider
        if auto is not None:
            saved["auto"] = bool(auto)
        self.svc.db.cache_put(SETTINGS_KEY, saved)
        return self.settings()

    def provider(self, name: str | None = None) -> Any:
        override = getattr(self.svc, "scenario_provider_override", None)        # tests and the demo inject one
        if override is not None:
            return override(name or self.settings()["provider"]) if callable(override) and not hasattr(override, "advance") else override
        return provider_for(self.svc, name or self.settings()["provider"])

    def readiness(self) -> dict[str, Any]:
        st = self.settings()
        try:
            p = self.provider(st["provider"])
            r = p.ready()
        except ValueError as exc:
            r = {"ok": False, "detail": str(exc)}
        return {**r, "provider": st["provider"], "auto": st["auto"]}

    def config_for(self, name: str) -> dict[str, Any]:
        try:
            cfg = dict(self.provider(name).config())
        except ValueError:
            cfg = {"provider": name}
        return {**cfg, "pipeline": PIPELINE_VERSION, "prompt": SC.PROMPT_VERSION}

    def scenarios(self) -> Any:
        sc = getattr(self.svc, "scenarios", None)
        return sc if sc is not None else SC.Scenarios(self.svc)

    # --- the intent, at study start --------------------------------------------------------------------------------
    def intent(self, horizon: int, wanted: bool | None, catalyst: dict[str, Any] | None) -> dict[str, Any] | None:
        """What the study promises to do once it is saved, or None (other horizons keep the study flow as it was)."""
        st = self.settings()
        on = st["auto"] if wanted is None else bool(wanted)
        if not on or horizon not in SC.SCENARIO_HORIZONS or not st["enabled"]:
            return None
        cfg = self.config_for(st["provider"])
        return {"version": PIPELINE_VERSION, "provider": st["provider"], "config": cfg, "config_hash": C.digest(cfg)[:16],
                "catalyst": catalyst, "attempt": 1, "requested_at": C.utc_now(),
                "stages": {"evidence": {"state": "running", "at": C.utc_now()}, "provider": {"state": "pending"},
                           "paths": {"state": "pending"}, "findings": {"state": "pending"}}}

    # --- the study's pipeline record --------------------------------------------------------------------------------
    def load(self, study_id: str) -> dict[str, Any] | None:
        r = self.svc.db.one("SELECT pipeline_json FROM studies WHERE id = ?", (study_id,))
        if r is None or not r["pipeline_json"]:
            return None
        return json.loads(r["pipeline_json"])

    def _update(self, study_id: str, **changes: Any) -> dict[str, Any] | None:
        stages = changes.pop("stages", None)
        with self.svc.db.tx() as cur:
            row = cur.execute("SELECT pipeline_json FROM studies WHERE id = ?", (study_id,)).fetchone()
            if row is None or not row["pipeline_json"]:
                return None
            p = json.loads(row["pipeline_json"])
            p.update(changes)
            for k, v in (stages or {}).items():
                p.setdefault("stages", {}).setdefault(k, {}).update({**v, "at": C.utc_now()})
            p["updated_at"] = C.utc_now()
            cur.execute("UPDATE studies SET pipeline_json = ? WHERE id = ?", (json.dumps(p, default=str), study_id))
        self.svc.bus.publish("study.pipeline", study_id, stage=p.get("stage"), status=p.get("status"))
        return p

    def workers(self) -> Any:
        from .workers import workers_for
        return workers_for(self.svc)

    def _enqueue(self, kind: str, payload: dict[str, Any], key: str, delay: float = 0.0) -> int | None:
        return self.workers().enqueue("scenarios", kind, payload, dedupe_key=key, max_attempts=3,
                                      due_at=_now() + timedelta(seconds=delay) if delay > 0 else None)

    def _start_key(self, study_id: str, p: dict[str, Any]) -> str:
        return f"{START}:{study_id}:{p['provider']}:{p['config_hash']}:{int(p.get('attempt') or 1)}"

    def after_study(self, study_id: str) -> int | None:
        """The study and its candidates are saved: queue the continuation (once)."""
        p = self.load(study_id)
        if p is None:
            return None
        r = self.svc.db.one("SELECT status FROM studies WHERE id = ?", (study_id,))
        if r is None or r["status"] not in ("complete", "partial"):
            self._update(study_id, status="failed", stage="evidence", stages={"evidence": {"state": "failed", "detail": "the study did not finish"}})
            return None
        self._update(study_id, status="running", stage="provider", stages={"evidence": {"state": "done"},
                                                                            "provider": {"state": "queued", "detail": "waiting to start"}})
        return self._enqueue(START, {"study_id": study_id}, self._start_key(study_id, p))

    # --- 1. start: the seed and the run ------------------------------------------------------------------------------
    def auto_catalyst(self, study: dict[str, Any], sessions: list[dict[str, Any]]) -> tuple[C.CatalystSpec, str]:
        """A verified event inside the horizon, else a recent sourced catalyst, else a named hypothetical case."""
        sym, h = study["symbol"], int(study["horizon_days"])
        tries: list[tuple[C.CatalystSpec, str]] = []
        try:
            e = self.svc._earnings(sym, None)
        except Exception:  # noqa: BLE001 - the earnings date is optional
            e = None
        if e and e.get("date"):
            if e.get("estimated"):
                tries.append((C.CatalystSpec(kind="earnings", title=f"{sym} quarterly results (estimated date {e['date']})", hypothetical=True,
                                             event_date=e["date"], description=f"The date is an estimate ({e.get('source') or 'from the past pattern'})."),
                              "an estimated earnings date inside the horizon"))
            else:
                tries.append((C.CatalystSpec(kind="earnings", title=f"{sym} quarterly results", event_date=e["date"],
                                             description=f"Earnings date from {e.get('source') or 'the earnings calendar'}."),
                              "the verified earnings date inside the horizon"))
        ev = study.get("evidence") or {}
        news = next((i for i in ev.get("items") or [] if i.get("kind") == "news"), None)
        if news and ":" in str(news.get("summary") or ""):
            head = str(news["summary"]).split(":", 1)[1].split("|")[0].strip()
            if len(head) >= 3:
                tries.append((C.CatalystSpec(kind="other", title=f"Recent news: {head}"[:200], description=str(news["summary"])[:1500]),
                              "the most recent sourced headline in the study's news"))
        tries.append((C.CatalystSpec(kind="other", hypothetical=True,
                                     title=f"Current setup: how {sym} could move over the next {h} session{'s' if h != 1 else ''} with no scheduled event"[:200]),
                      "no verified event or recent sourced catalyst inside the horizon: a hypothetical current-setup case"))
        for spec, basis in tries:
            try:
                SC.first_session(spec, sessions)
                return spec, basis
            except RequestError:
                continue
        return tries[-1]

    @staticmethod
    def strategy_context(view: dict[str, Any], horizon: int) -> list[str]:
        """What the compared strategies depend on, in words: no contracts, quantities, budget or account."""
        cands = (view.get("strategies") or {}).get("candidates") or []
        kinds = {c.get("key") for c in cands}
        out = []
        groups = [("shares or a short sale", {"shares", "short"}), ("single options (calls or puts)", {"long_call", "long_put"}),
                  ("option spreads", {"bull_call_spread", "bull_put_spread", "bear_put_spread", "bear_call_spread"}),
                  ("volatility structures (a straddle or an iron condor)", {"long_straddle", "iron_condor"})]
        named = [w for w, ks in groups if kinds & ks]
        if named:
            out.append(f"The study compares {', '.join(named)} over {horizon} trading session{'s' if horizon != 1 else ''}.")
        if kinds & {"long_call", "long_put", "long_straddle", "bull_call_spread", "bear_put_spread"}:
            out.append("Bought options gain only if the move is large and soon enough to beat their premium; they lose value if "
                       "implied volatility falls after a scheduled release.")
        if kinds & {"iron_condor", "bull_put_spread", "bear_call_spread"}:
            out.append("Sold-premium structures gain when the stock stays inside a range and lose on a large move either way.")
        out.append("What matters most: the direction and size of the first reaction, whether it holds to the last session, and "
                   "whether the broad market or the sector moves the same way.")
        return out

    def start(self, payload: dict[str, Any]) -> dict[str, Any]:
        study_id = payload["study_id"]
        p = self.load(study_id)
        if p is None or p.get("status") in ("canceled",):
            return {"skipped": "no pipeline, or it was canceled"}
        if p.get("run_id"):
            run = self.svc.db.one("SELECT status FROM scenario_runs WHERE id = ?", (p["run_id"],))
            if run is not None and run["status"] not in TERMINAL:
                return {"skipped": "a run is already going", "run_id": p["run_id"]}
        sc = self.scenarios()
        try:
            st = sc._study(study_id)
        except RequestError as exc:
            self._fail(study_id, "provider", str(exc))
            return {"failed": str(exc)}
        row, view = st["row"], st["view"]
        frame = SC.study_frame(row["created_at"], view)
        if frame is None:
            self._fail(study_id, "provider", "This study has no anchor price or forecast sessions saved.")
            return {"failed": "no frame"}
        sessions = frame["sessions"][: int(row["horizon_days"])]
        if p.get("catalyst"):
            spec, basis = C.CatalystSpec.model_validate(p["catalyst"]), "the catalyst chosen for this study"
        else:
            spec, basis = self.auto_catalyst({"symbol": row["symbol"], "horizon_days": row["horizon_days"], "evidence": st["evidence"]}, sessions)
        try:
            seed_view = sc.create_seed(study_id, SC.SeedIn(catalyst=spec), strategy_context=self.strategy_context(view, int(row["horizon_days"])))
        except RequestError as exc:
            self._fail(study_id, "provider", f"The evidence packet could not be frozen: {exc}")
            return {"failed": str(exc)}
        seed = seed_view["seed"]
        name = p["provider"]
        try:
            prov = self.provider(name)
        except ValueError as exc:
            self._fail(study_id, "provider", str(exc))
            return {"failed": str(exc)}
        ready = prov.ready()
        name = getattr(prov, "name", name)
        run_id = C.new_id("srun")
        now = C.utc_now()
        external = {"stage": "", "detail": "", "step": 0, "state": {}, "started_at": None, "catalyst_basis": basis, "attempt": p.get("attempt", 1)}
        with self.svc.db.tx() as cur:
            cur.execute("""INSERT INTO scenario_runs (id, seed_id, study_id, provider, status, created_at, schema_version, prompt_version,
                           config_json, external_json) VALUES (?, ?, ?, ?, 'queued', ?, ?, ?, ?, ?)""",
                        (run_id, seed["seed_id"], study_id, name, now, C.SCENARIO_SCHEMA_VERSION, SC.PROMPT_VERSION,
                         json.dumps(p["config"]), json.dumps(external)))
        self._update(study_id, run_id=run_id, seed_id=seed["seed_id"], catalyst_used=spec.model_dump(), catalyst_basis=basis,
                     stages={"provider": {"state": "queued", "detail": f"packet {seed['seed_id']} frozen; waiting for {prov.label}"}})
        self.svc.bus.publish("scenario.run_queued", run_id, study_id=study_id, provider=name)
        if not ready.get("ok"):
            self._finish_run(run_id, "failed", error=f"{prov.label} is not available: {ready.get('detail')}")
            self._fail(study_id, "provider", f"{prov.label} is not available: {ready.get('detail')}")
            return {"failed": ready.get("detail"), "run_id": run_id}
        self._enqueue(STEP, {"run_id": run_id, "n": 0}, f"{STEP}:{run_id}:0")
        return {"run_id": run_id, "seed_id": seed["seed_id"]}

    # --- 2. the provider's steps --------------------------------------------------------------------------------------
    def _run(self, run_id: str) -> Any:
        r = self.svc.db.one("SELECT * FROM scenario_runs WHERE id = ?", (run_id,))
        if r is None:
            raise RequestError(f"No scenario run {run_id}")
        return r

    def _ctx(self, r: Any) -> dict[str, Any]:
        sc = self.scenarios()
        seed = sc._seed(r["seed_id"])
        return {"run_id": r["id"], "seed": seed, "markdown": SC.markdown(seed, SC.OUTPUT_PROMPT, SC.template_for(seed)),
                "filename": f"{seed['symbol']}-catalyst-evidence-{seed['seed_id'][-8:]}.md", "question": seed["question"],
                "instructions": ("Write the report as Central, Bullish and Bearish cases. End it with one JSON object that follows the "
                                 "packet's output template exactly. No prices, price targets or probabilities.")}

    def _save_external(self, run_id: str, ext: dict[str, Any], status: str | None = None) -> None:
        with self.svc.db.tx() as cur:
            if status:
                cur.execute("UPDATE scenario_runs SET external_json = ?, status = ? WHERE id = ?", (json.dumps(ext, default=str), status, run_id))
            else:
                cur.execute("UPDATE scenario_runs SET external_json = ? WHERE id = ?", (json.dumps(ext, default=str), run_id))

    def _finish_run(self, run_id: str, status: str, *, error: str | None = None, **cols: Any) -> None:
        sets, vals = ["status = ?", "completed_at = ?"], [status, C.utc_now()]
        if error is not None:
            sets.append("error = ?")
            vals.append(error[:1000])
        for k, v in cols.items():
            sets.append(f"{k} = ?")
            vals.append(v)
        with self.svc.db.tx() as cur:
            cur.execute(f"UPDATE scenario_runs SET {', '.join(sets)} WHERE id = ? AND status NOT IN ('complete', 'canceled')", (*vals, run_id))

    def _fail(self, study_id: str, stage: str, detail: str, attention: bool = False) -> None:
        self._update(study_id, status="needs_attention" if attention else "failed", stage=stage, error=detail[:1000],
                     stages={stage: {"state": "attention" if attention else "failed", "detail": detail[:600]}})
        self.svc.bus.publish("scenario.failed", study_id, stage=stage, detail=detail[:200])

    def _sync(self, r: Any) -> None:
        """A run that ended outside the pipeline (canceled elsewhere, say): the study's pipeline says so too."""
        p = self.load(r["study_id"]) or {}
        row = self.svc.db.one("SELECT status, error FROM scenario_runs WHERE id = ?", (r["id"],))
        if p.get("run_id") == r["id"] and p.get("status") == "running" and row["status"] in ("canceled", "failed", "needs_attention"):
            self._update(r["study_id"], status=row["status"], error=row["error"] or f"the run was {row['status']}",
                         stages={p.get("stage") or "provider": {"state": {"canceled": "canceled", "failed": "failed"}.get(row["status"], "attention"),
                                                                "detail": row["error"] or row["status"]}})

    def step(self, payload: dict[str, Any]) -> dict[str, Any]:
        run_id, n = payload["run_id"], int(payload.get("n") or 0)
        r = self._run(run_id)
        if r["status"] in TERMINAL:
            self._sync(r)
            return {"skipped": f"the run is {r['status']}"}
        ext = json.loads(r["external_json"] or "{}")
        if int(ext.get("step") or 0) != n:
            return {"skipped": f"step {n} is not the run's next step ({ext.get('step')})"}
        if n >= MAX_STEPS:
            return self._stop(r, ext, "the run took too many steps")
        prov = self.provider(r["provider"])
        s = self.svc.settings
        if r["status"] == "queued":
            if r["provider"] == "mirofish":
                busy = self.svc.db.one("SELECT COUNT(*) AS n FROM scenario_runs WHERE provider = 'mirofish' AND status = 'running' AND id != ?",
                                       (run_id,))["n"]
                if busy >= s.mirofish_concurrency:
                    self._save_external(run_id, {**ext, "detail": f"waiting for a free MiroFish slot ({busy} running)"})
                    raise Deferred(_now() + timedelta(seconds=WAIT_TURN), "waiting for a free MiroFish slot")
            ext["started_at"] = C.utc_now()
            self._save_external(run_id, ext, status="running")
            self._update(r["study_id"], stages={"provider": {"state": "running", "detail": f"{prov.label} started"}})
        started = _ts(ext.get("started_at")) or _now()
        budget = s.mirofish_run_minutes * 60.0
        if (_now() - started).total_seconds() > budget:
            return self._stop(r, ext, f"it took longer than MIROFISH_RUN_MINUTES ({s.mirofish_run_minutes:g} minutes)")
        try:
            out = prov.advance(self._ctx(r), ext.get("state") or {})
        except Exception as exc:  # noqa: BLE001 - a provider bug must not leave the run hanging
            log.exception("Scenario provider step failed")
            out = None
            err = f"{exc.__class__.__name__}: {str(exc)[:300]}"
        # The run may have been canceled while the step was out: nothing it brought back is applied.
        now_status = self.svc.db.one("SELECT status FROM scenario_runs WHERE id = ?", (run_id,))["status"]
        if now_status in TERMINAL:
            if out is not None and out.state and getattr(prov, "stop", None) and now_status == "canceled":
                prov.stop(out.state)
            self._sync(r)
            return {"skipped": f"the run became {now_status} while the step ran; its result was not applied"}
        if out is None:
            self._finish_run(run_id, "failed", error=err)
            self._fail(r["study_id"], "provider", f"{prov.label} failed: {err}")
            return {"failed": err}
        ext.update({"state": out.state, "stage": out.stage, "detail": out.detail, "step": n + 1, "last_step_at": C.utc_now()})
        if out.model:
            ext["model"] = out.model
        if out.usage:
            ext["usage"] = out.usage
        if out.failed:
            self._save_external(run_id, ext)
            self._finish_run(run_id, "failed", error=out.failed)
            self._fail(r["study_id"], "provider", f"{prov.label}: {out.failed}")
            return {"failed": out.failed}
        if out.attention:
            self._save_external(run_id, ext)
            self._finish_run(run_id, "needs_attention", error=out.attention)
            self._fail(r["study_id"], "provider", f"{prov.label}: {out.attention}", attention=True)
            return {"attention": out.attention}
        if out.report is not None:
            ext["parsed_cases"] = out.cases
            ext["report_received_at"] = C.utc_now()
            with self.svc.db.tx() as cur:
                cur.execute("UPDATE scenario_runs SET external_json = ?, report_text = ?, model_version = ? WHERE id = ?",
                            (json.dumps(ext, default=str), out.report[:REPORT_LIMIT], out.model, run_id))
            self._update(r["study_id"], stages={"provider": {"state": "done", "detail": f"{prov.label}: report received"}},
                         stage="paths")
            self._enqueue(FINISH, {"run_id": run_id}, f"{FINISH}:{run_id}")
            return {"report": True}
        self._save_external(run_id, ext)                          # every acknowledged id is saved before the next step is queued
        self._update(r["study_id"], stages={"provider": {"state": "running", "detail": f"{out.stage}: {out.detail}"[:300]}})
        self._enqueue(STEP, {"run_id": run_id, "n": n + 1}, f"{STEP}:{run_id}:{n + 1}", delay=out.next_in or 0.0)
        return {"next_in": out.next_in, "stage": out.stage}

    def _stop(self, r: Any, ext: dict[str, Any], why: str) -> dict[str, Any]:
        prov = self.provider(r["provider"])
        said = prov.stop(ext.get("state") or {}) if hasattr(prov, "stop") else "nothing to stop"
        self._finish_run(r["id"], "failed", error=f"Stopped: {why} ({said}).")
        self._fail(r["study_id"], "provider", f"{prov.label} was stopped: {why} ({said}).")
        return {"failed": why}

    # --- 3. finish: cases, an automatic version, the valuation, the findings --------------------------------------
    def cases_from(self, r: Any, seed: dict[str, Any], ext: dict[str, Any]) -> Any:
        """The provider's cases as a checked ScenarioImport, or a reason why not."""
        from ..ai import AIError, extract_json
        raw = ext.get("parsed_cases")
        text = r["report_text"] or ""
        if not isinstance(raw, dict) or "cases" not in raw:
            got = extract_json(text)
            raw = got if isinstance(got, dict) and "cases" in got else None
        if raw is None:
            ai = getattr(self.svc, "ai", None)
            if ai is None or not ai.configured:
                return None, ("The report has no cases in the packet's template, and no AI is set up to convert it (ANTHROPIC_API_KEY). "
                              "Open Advanced to paste its cases, or retry.")
            prompt = (f"{SC.OUTPUT_PROMPT}\n\nTemplate:\n```json\n{json.dumps(SC.template_for(seed), indent=1)}\n```\n\n"
                      f"Report:\n{text[:60_000]}")
            try:
                reply = ai.ask(purpose="scenario_convert", prompt=prompt, system="You convert reports into JSON. You never add prices or probabilities.",
                               max_tokens=3000, web_search=False)
            except AIError as exc:
                return None, f"The report could not be converted to cases: {exc}"
            got = extract_json(reply.get("text") or "")
            raw = got if isinstance(got, dict) else None
            if raw is None:
                return None, "The report could not be converted to cases (the converter gave no JSON object)."
        raw = dict(raw)
        raw.setdefault("seed_id", seed["seed_id"])               # the run was started for this packet; a different id is refused
        raw.setdefault("schema_version", C.SCENARIO_SCHEMA_VERSION)
        try:
            return self.scenarios().check_import(seed, raw), None
        except RequestError as exc:
            return None, str(exc)

    def auto_assumptions(self, seed: dict[str, Any], cases: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], list[str]]:
        """Each case sized only by the deterministic rules: a provider's direction and timing, the app's magnitudes."""
        h = int(seed["horizon_sessions"])
        cat_session = int(seed.get("catalyst_session") or 1)
        vol = seed.get("volatility") or {}
        srcs = seed.get("sources") or []
        opt_ids = [s["id"] for s in srcs if s.get("kind") == "options"][:1]
        rows = {r["key"]: r for r in (seed.get("factors") or {}).get("rows") or []}
        notes: list[str] = []
        out = []
        for c in cases:
            a: dict[str, Any] = {"key": c["key"], "visible": True, "issuer": None, "factors": [], "dispersion": None}
            issuer = next((d for d in c["drivers"] if d["type"] == "issuer_event" and d["direction"] in ("up", "down") and d.get("source_ids")), None)
            if issuer is not None:
                sess = min(h, int(issuer.get("session") or cat_session))
                size = None
                if seed["catalyst"].get("kind") == "earnings" and _f(vol.get("event_move_pct")) and seed.get("catalyst_session"):
                    size = SC.rule_value(seed, "options_implied_move", issuer["direction"])
                    src, ids, how = "options_implied_move", opt_ids, f"the move options price for the release (±{vol['event_move_pct']:.2f}%)"
                if size is None:
                    size = SC.rule_value(seed, "analog_tail", issuer["direction"])
                    src, ids, how = "analog_tail", SC.analog_source_ids(seed), "the comparable situations' tail (90th or 10th percentile less the median)"
                if size is None:
                    size = SC.rule_value(seed, "implied_sensitivity", issuer["direction"])
                    src, ids, how = "implied_sensitivity", opt_ids, "one standard deviation of the move options imply to the horizon"
                if size is not None:
                    a["issuer"] = {"value_pct": round(max(-60.0, min(60.0, size)), 4), "session": sess, "source": src, "source_ids": ids,
                                   "note": f"Sized by {how}; direction and timing from the provider: {issuer['reason']}"[:300]}
                else:
                    notes.append(f"{C.CASE_LABELS[c['key']]}: no rule could size the stock's own move; it stays qualitative.")
            elif any(d["type"] == "issuer_event" and d["direction"] in ("up", "down") for d in c["drivers"]):
                notes.append(f"{C.CASE_LABELS[c['key']]}: its issuer driver cites no evidence in the packet; it stays a hypothesis.")
            for d in c["drivers"]:
                if d["type"] not in ("market", "sector", "macro") or d["direction"] not in ("up", "down") or not d.get("source_ids"):
                    continue
                row = rows.get(d.get("factor") or "")
                if row is None or row.get("redundant") or any(f["key"] == row["key"] for f in a["factors"]):
                    continue
                size = SC.rule_value(seed, "factor_volatility", "up", row["key"])
                if not size:
                    continue
                sess = min(h, int(d.get("session") or (cat_session if d["timing"] in ("first_session_after_release", "unknown") else 1)))
                signed = self._factor_sign(seed, row["key"], abs(size), sess, d["direction"])
                if signed is None:
                    continue
                a["factors"].append({"key": row["key"], "value": signed, "session": sess, "source": "factor_volatility",
                                     "source_ids": list(d["source_ids"])[:10],
                                     "note": f"One standard deviation of {row['label']} over {h} sessions; the provider: {d['reason']}"[:300]})
            out.append(a)
        return out, notes

    @staticmethod
    def _factor_sign(seed: dict[str, Any], key: str, size: float, session: int, direction: str) -> float | None:
        """The factor shock's sign that moves the stock the driver's way (yields up can push a stock down)."""
        probe = SC.case_paths(seed, {"key": "probe", "issuer": None, "dispersion": None,
                                     "factors": [{"key": key, "value": size, "session": session, "source": "factor_volatility"}]})
        effect = float(probe["attribution"]["factor_pct"][-1])
        if abs(effect) < 1e-6:
            return None
        want = 1.0 if direction == "up" else -1.0
        return round(size if effect * want > 0 else -size, 4)

    def finish(self, payload: dict[str, Any]) -> dict[str, Any]:
        run_id = payload["run_id"]
        r = self._run(run_id)
        if r["status"] in TERMINAL:
            return {"skipped": f"the run is {r['status']}"}
        study_id = r["study_id"]
        sc = self.scenarios()
        seed = sc._seed(r["seed_id"])
        ext = json.loads(r["external_json"] or "{}")
        imp, why = self.cases_from(r, seed, ext)
        excerpt = (r["report_text"] or "").strip()[: SC.MAX_EXCERPT] or None
        if imp is None:
            self._finish_run(run_id, "needs_attention", error=why, report_excerpt=excerpt)
            self._fail(study_id, "paths", why, attention=True)
            return {"attention": why}
        cases = sc.normalized(imp)
        with self.svc.db.tx() as cur:
            cur.execute("""UPDATE scenario_runs SET status = 'complete', completed_at = ?, response_hash = ?, cases_json = ?, report_excerpt = ?
                           WHERE id = ? AND status = 'running'""",
                        (C.utc_now(), C.digest(imp.model_dump()), json.dumps(cases), excerpt, run_id))
            if cur.rowcount == 0:
                return {"skipped": "the run changed while its report was read"}
        self.svc.bus.publish("scenario.imported", run_id, study_id=study_id, symbol=seed["symbol"], cases=len(cases), provider=r["provider"])
        assumptions, notes = self.auto_assumptions(seed, cases)
        try:
            ver = sc.create_version(run_id, C.ScenarioVersionIn(cases=[C.CaseAssumptions(**a) for a in assumptions]), origin="auto")
        except (RequestError, ValueError) as exc:
            self._fail(study_id, "paths", f"The cases were saved but no price paths could be made from them: {exc}")
            return {"failed": str(exc)}
        self._update(study_id, version_id=ver["id"], sizing_notes=notes,
                     stages={"paths": {"state": "running", "detail": f"version {ver['version']} saved; valuing the strategies"}})
        return self.revalue(study_id, ver)

    def revalue(self, study_id: str, ver: dict[str, Any] | None = None) -> dict[str, Any]:
        """Price the study's fixed candidates under a version from one fresh snapshot, and write the findings."""
        sc = self.scenarios()
        p = self.load(study_id) or {}
        if ver is None:
            if not p.get("version_id"):
                raise RequestError("This study has no scenario version to value yet.")
            ver = sc.version(p["version_id"])
        study = self.svc.research3.get(study_id)
        try:
            res, snap = V.value(self.svc, study, ver)
            vid = V.save(self.svc, res, snap)
            res["id"] = vid
        except (RequestError, DataError, ValueError) as exc:
            res, vid = {"status": "failed", "problems": [str(exc)[:300]], "structures": [], "mixes": [], "failed": []}, None
        detail = {"complete": "every candidate valued under every case", "partial": "valued, with gaps (listed)",
                  "failed": "the strategies could not be valued: " + "; ".join(res.get("problems") or [])[:300]}[res["status"]]
        findings = self.findings(study, ver, res)
        self._update(study_id, valuation_id=vid, findings=findings, status="complete", stage="findings", error=None,
                     stages={"paths": {"state": "done" if res["status"] != "failed" else "failed", "detail": detail},
                             "findings": {"state": "done", "detail": f"{len(findings['cases'])} cases; best mix by its worst case: "
                                                                     f"{(findings['best_mix'] or {}).get('label') or 'Cash'}"}})
        hook = getattr(self.svc, "on_scenario_findings", None)        # 4.4: the opportunity evaluator
        if callable(hook):
            try:
                hook(study_id, ver, res)
            except Exception:  # noqa: BLE001 - the findings are saved whatever a later step does
                log.exception("The findings hook failed")
        self.svc.bus.publish("scenario.findings", study_id, version_id=ver["id"], valuation_id=vid)
        return {"version_id": ver["id"], "valuation_id": vid, "status": res["status"]}

    def findings(self, study: dict[str, Any], ver: dict[str, Any], val: dict[str, Any]) -> dict[str, Any]:
        st = study.get("study") or {}
        fc = st.get("forecast") or {}
        rd = fc.get("readiness") or {}
        sym = st.get("symbol") or study.get("symbol")
        base = ver["base"]
        cases = [{"key": c["key"], "label": c["label"], "direction": c["direction"], "trigger": c["trigger"], "invalidation": c["invalidation"],
                  "quantified": c["quantified"], "return_pct": c["terminal"]["return_pct"], "price": c["terminal"]["price"],
                  "vs_base_pp": c["terminal"]["vs_base_pp"]} for c in ver["cases"]]
        best = next((m for m in val.get("mixes") or [] if m["key"] == val.get("best_mix")), None)
        words = [f"{sym}, {ver['horizon_sessions']} session{'s' if ver['horizon_sessions'] != 1 else ''} to {ver['sessions'][-1]['date']}: "
                 f"the base forecast ends at {base['return_pct'][-1]:+.2f}% ({rd.get('label') or base['range_words']})."]
        moves = ", ".join(f"{c['label']} {c['return_pct']:+.2f}%" for c in cases)
        words.append(f"Conditional cases, by their own triggers: {moves}. None has a probability.")
        if best and best["key"] != "cash":
            words.append(f"Best budgeted mix by its worst case across the cases: {best['label']} (worst case {best['worst_case']:+.0f}, "
                         f"capital {best['capital']:.0f}).")
        elif val.get("status") != "failed":
            words.append("No budgeted mix beats holding cash in its worst case across the cases.")
        else:
            words.append("The strategies could not be valued: " + "; ".join(val.get("problems") or [])[:200])
        return {"conclusion": " ".join(words), "cases": cases, "best_mix": best, "valuation_status": val.get("status"),
                "readiness": {k: rd.get(k) for k in ("state", "label", "directional", "range")}, "made_at": C.utc_now()}

    # --- cancel, retry, recover ----------------------------------------------------------------------------------------
    def cancel(self, study_id: str) -> dict[str, Any]:
        p = self.load(study_id)
        if p is None:
            raise RequestError("This study has no scenario pipeline.")
        said = "nothing was running"
        if p.get("run_id"):
            r = self._run(p["run_id"])
            if r["status"] in ("queued", "running"):
                with self.svc.db.tx() as cur:
                    cur.execute("UPDATE scenario_runs SET status = 'canceled', completed_at = ?, error = 'canceled by you' "
                                "WHERE id = ? AND status IN ('queued', 'running')", (C.utc_now(), r["id"]))
                ext = json.loads(r["external_json"] or "{}")
                try:
                    prov = self.provider(r["provider"])
                    said = prov.stop(ext.get("state") or {}) if hasattr(prov, "stop") else "stopped"
                except ValueError:
                    said = "stopped"
        elif p.get("status") not in ("running",):
            raise RequestError(f"Nothing to cancel: the pipeline is {p.get('status') or 'idle'}.")
        self._update(study_id, status="canceled", error=f"Canceled by you ({said}).",
                     stages={p.get("stage") or "provider": {"state": "canceled", "detail": f"canceled ({said})"}})
        return self.view(study_id)

    def retry(self, study_id: str) -> dict[str, Any]:
        """A new provider run for the same study (a new attempt: a failed or abandoned one is never resumed blindly)."""
        p = self.load(study_id)
        if p is None:
            raise RequestError("This study has no scenario pipeline: run it with scenarios on.")
        if p.get("status") == "running":
            raise RequestError("The scenario pipeline is still running.")
        if p.get("version_id") and p.get("status") == "complete" and p.get("stages", {}).get("paths", {}).get("state") == "failed":
            self.revalue(study_id)                                     # only the valuation failed: value again
            return self.view(study_id)
        attempt = int(p.get("attempt") or 1) + 1
        st = self.settings()
        cfg = self.config_for(st["provider"])
        p = self._update(study_id, attempt=attempt, provider=st["provider"], config=cfg, config_hash=C.digest(cfg)[:16], status="running",
                         stage="provider", error=None, run_id=None, version_id=None, valuation_id=None, findings=None,
                         stages={"provider": {"state": "queued", "detail": "retrying"}, "paths": {"state": "pending", "detail": ""},
                                 "findings": {"state": "pending", "detail": ""}}) or {}
        self._enqueue(START, {"study_id": study_id}, self._start_key(study_id, p))
        return self.view(study_id)

    def enable(self, study_id: str, catalyst: dict[str, Any] | None = None) -> dict[str, Any]:
        """Run the pipeline for a finished study that was made without it (Research → Run scenarios)."""
        r = self.svc.db.one("SELECT horizon_days, status, pipeline_json FROM studies WHERE id = ?", (study_id,))
        if r is None:
            raise RequestError(f"No study {study_id}")
        if r["pipeline_json"]:
            return self.retry(study_id)
        it = self.intent(int(r["horizon_days"]), True, catalyst)
        if it is None:
            raise RequestError(f"Scenarios run on {' or '.join(map(str, SC.SCENARIO_HORIZONS))}-session studies (and need "
                               "FEATURE_CATALYST_SCENARIOS on).")
        with self.svc.db.tx() as cur:
            cur.execute("UPDATE studies SET pipeline_json = ? WHERE id = ? AND pipeline_json IS NULL", (json.dumps(it), study_id))
        self.after_study(study_id)
        return self.view(study_id)

    def recover(self, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        """Work a restart left behind: finished studies whose continuation was never queued, and runs with no job."""
        jobs = Jobs(self.svc.db)

        def job_status(key: str) -> str | None:
            d = jobs.state(key)
            return d["status"] if d else None
        queued = []
        for r in self.svc.db.query("SELECT id, status, pipeline_json FROM studies WHERE pipeline_json IS NOT NULL AND status IN ('complete', 'partial')"):
            p = json.loads(r["pipeline_json"])
            if p.get("status") in ("complete", "failed", "canceled", "needs_attention"):
                continue
            if not p.get("run_id"):
                key = self._start_key(r["id"], p)
                if job_status(key) in (None, "failed", "discarded"):
                    if p.get("stages", {}).get("evidence", {}).get("state") != "done":
                        self._update(r["id"], status="running", stage="provider", stages={"evidence": {"state": "done"}})
                    if job_status(key) is None:
                        self._enqueue(START, {"study_id": r["id"]}, key)
                        queued.append(r["id"])
                    else:
                        self._fail(r["id"], "provider", "The scenario run could not start after several tries; press Retry.")
                continue
            run = self.svc.db.one("SELECT * FROM scenario_runs WHERE id = ?", (p["run_id"],))
            if run is None:
                continue
            if run["status"] in ("queued", "running"):
                ext = json.loads(run["external_json"] or "{}")
                n = int(ext.get("step") or 0)
                if ext.get("report_received_at"):
                    key, kind, pl = f"{FINISH}:{run['id']}", FINISH, {"run_id": run["id"]}
                else:
                    key, kind, pl = f"{STEP}:{run['id']}:{n}", STEP, {"run_id": run["id"], "n": n}
                state = job_status(key)
                if state is None:
                    self._enqueue(kind, pl, key)
                    queued.append(run["id"])
                elif state in ("failed", "discarded"):
                    self._finish_run(run["id"], "failed", error="Its background step failed after several tries.")
                    self._fail(r["id"], "provider", "The scenario run's background step failed after several tries; press Retry.")
            elif run["status"] == "complete" and not p.get("version_id") and job_status(f"{FINISH}:{run['id']}") in ("failed", "discarded"):
                self._fail(r["id"], "paths", "Reading the provider's cases failed after several tries; press Retry.")
        # A study left in the evidence stage by a process that stopped: the study itself is marked failed by its sweep.
        for r in self.svc.db.query("SELECT id, status, pipeline_json FROM studies WHERE pipeline_json IS NOT NULL AND status = 'failed'"):
            p = json.loads(r["pipeline_json"])
            if p.get("status") in (None, "running") and p.get("stage", "evidence") == "evidence":
                self._update(r["id"], status="failed", stage="evidence", stages={"evidence": {"state": "failed", "detail": "the study failed"}})
        return {"queued": queued}

    # --- the view ------------------------------------------------------------------------------------------------
    def view(self, study_id: str) -> dict[str, Any] | None:
        p = self.load(study_id)
        if p is None:
            return None
        prov = p.get("provider") or "mirofish"
        stages = [{"key": k, "label": stage_label(k, prov), **(p.get("stages") or {}).get(k, {"state": "pending"})} for k in STAGES]
        run = None
        if p.get("run_id"):
            r = self.svc.db.one("SELECT * FROM scenario_runs WHERE id = ?", (p["run_id"],))
            if r is not None:
                ext = json.loads(r["external_json"] or "{}")
                state = ext.get("state") or {}
                started = _ts(ext.get("started_at"))
                run = {"id": r["id"], "provider": r["provider"], "status": r["status"], "stage": ext.get("stage"), "detail": ext.get("detail"),
                       "created_at": r["created_at"], "started_at": ext.get("started_at"), "completed_at": r["completed_at"],
                       "elapsed_seconds": round(((_ts(r["completed_at"]) or _now()) - started).total_seconds(), 1) if started else None,
                       "remote": {k: state.get(k) for k in ("project_id", "graph_id", "simulation_id", "report_id") if state.get(k)},
                       "error": r["error"], "model": ext.get("model"), "catalyst_basis": ext.get("catalyst_basis"),
                       "report_excerpt": r["report_excerpt"], "seed_id": r["seed_id"], "rounds": state.get("rounds")}
        val = None
        if p.get("valuation_id"):
            try:
                val = V.load(self.svc, p["valuation_id"])
            except RequestError:
                val = None
        status = p.get("status") or "running"
        return {"status": status, "stage": p.get("stage"), "provider": prov, "provider_label": stage_label("provider", prov),
                "attempt": p.get("attempt", 1), "stages": stages, "run": run, "seed_id": p.get("seed_id"), "version_id": p.get("version_id"),
                "valuation": val, "findings": p.get("findings"), "decision": p.get("decision"), "error": p.get("error"),
                "catalyst": p.get("catalyst_used"), "catalyst_basis": p.get("catalyst_basis"), "sizing_notes": p.get("sizing_notes") or [],
                "can_cancel": status == "running", "can_retry": status in ("failed", "needs_attention", "canceled")
                or (status == "complete" and (p.get("stages") or {}).get("paths", {}).get("state") == "failed"),
                "updated_at": p.get("updated_at"), "demo": prov == "demo"}


def _f(x: Any) -> float | None:
    try:
        v = float(x)
    except (TypeError, ValueError):
        return None
    return v if math.isfinite(v) else None


def install(svc: Any) -> None:
    from ..api_v3 import register
    from ..jobs import Schedule
    from .workers import workers_for
    svc.pipeline = pl = Pipeline(svc)
    rw = workers_for(svc)
    rw.leases["scenarios"] = max(180.0, float(svc.settings.mirofish_http_timeout) + 120.0)   # a step may wait on one long request
    rw.add("scenarios", START, pl.start)
    rw.add("scenarios", STEP, pl.step)
    rw.add("scenarios", FINISH, pl.finish)
    rw.add("scenarios", RECOVER, pl.recover)
    rw.schedule("scenarios", Schedule(RECOVER, 600, 300))
    try:
        pl.recover()
        rw.resume()
    except Exception:  # noqa: BLE001 - never block start
        log.exception("The scenario pipeline could not check for left-over work")

    def routes(router: Any, svc: Any) -> None:
        @router.get("/scenarios/settings")
        def get_settings() -> dict[str, Any]:
            return {**pl.settings(), "readiness": pl.readiness()}

        @router.post("/scenarios/settings")
        def set_settings(body: ScenarioSettingsIn) -> dict[str, Any]:
            out = pl.save_settings(body.provider, body.auto)
            return {**out, "readiness": pl.readiness()}

        @router.post("/scenarios/check")
        def check() -> dict[str, Any]:
            return pl.readiness()

        @router.get("/studies/{study_id}/pipeline")
        def view(study_id: str) -> dict[str, Any]:
            return {"pipeline": pl.view(study_id)}

        @router.post("/studies/{study_id}/pipeline/cancel")
        def cancel(study_id: str) -> dict[str, Any]:
            return {"pipeline": pl.cancel(study_id)}

        @router.post("/studies/{study_id}/pipeline/retry")
        def retry(study_id: str) -> dict[str, Any]:
            return {"pipeline": pl.retry(study_id)}

        @router.post("/studies/{study_id}/pipeline/start")
        def enable(study_id: str, body: PipelineStartIn | None = None) -> dict[str, Any]:
            cat = body.catalyst.model_dump() if body is not None and body.catalyst is not None else None
            return {"pipeline": pl.enable(study_id, cat)}

        @router.post("/studies/{study_id}/pipeline/revalue")
        def revalue(study_id: str) -> dict[str, Any]:
            pl.revalue(study_id)
            return {"pipeline": pl.view(study_id)}

        @router.get("/scenario-valuations/{valuation_id}")
        def valuation(valuation_id: str) -> dict[str, Any]:
            return V.load(svc, valuation_id)

    register(svc, routes)


__all__ = ["Pipeline", "install", "PIPELINE_VERSION", "STAGES"]
_ = (date, NY, time)
