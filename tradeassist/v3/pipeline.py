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
from . import simulation as G
from . import valuation as V
from .scenario_providers import PINNED_UPSTREAM, provider_for

log = logging.getLogger(__name__)

PIPELINE_VERSION = "pipeline-4.3"            # kept: it is part of each run's configuration hash
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
    profile: Literal["quick", "standard", "deep"] | None = None


class ApproveIn(C.Strict):
    profile: Literal["quick", "standard", "deep"] | None = None


class ActualCostIn(C.Strict):
    actual_usd: float
    note: str = ""


class PipelineStartIn(C.Strict):
    catalyst: C.CatalystSpec | None = None


class RetryIn(C.Strict):
    confirm_new_run: bool = False          # 4.5.1: start a new run although the last paid request's outcome is unknown


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


def _round1(x: float | None) -> float | None:
    return None if x is None else round(x, 1)


def cap_seconds(ext: dict[str, Any], end: datetime | None = None) -> float | None:
    """4.6.1: seconds counted against the run's time cap. The clock starts when the run starts, or again when you approve
    it (whichever is later); time paused waiting for an approval is not counted."""
    starts = [t for t in (_ts(ext.get("started_at")), _ts(ext.get("clock_from"))) if t is not None]
    if not starts:
        return None
    end = end or _now()
    out = (end - max(starts)).total_seconds() - float(ext.get("paused_seconds") or 0.0)
    paused = _ts(ext.get("paused_at"))
    if paused is not None and paused < end:
        out -= (end - max(paused, max(starts))).total_seconds()
    return max(0.0, out)


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
        prof = saved.get("profile") or getattr(s, "scenario_default_profile", "quick")
        return {"provider": provider, "auto": auto, "control": bool(s.scenario_control), "profile": prof,
                "profiles": G.profiles(s),
                "mirofish": {"url": s.mirofish_url, "max_rounds": s.mirofish_max_rounds, "run_minutes": s.mirofish_run_minutes,
                             "concurrency": s.mirofish_concurrency, "http_timeout": s.mirofish_http_timeout, "upstream": PINNED_UPSTREAM},
                "source": {"provider": "Research settings" if saved.get("provider") else "SCENARIO_PROVIDER in .env",
                           "auto": "Research settings" if "auto" in saved else "SCENARIO_AUTO in .env",
                           "profile": "Research settings" if saved.get("profile") else "SCENARIO_DEFAULT_PROFILE in .env"},
                "horizons": list(SC.SCENARIO_HORIZONS), "enabled": self.scenarios().enabled()}

    def save_settings(self, provider: str | None, auto: bool | None, profile: str | None = None) -> dict[str, Any]:
        saved = dict(self.svc.db.cache_get(SETTINGS_KEY) or {})
        if profile is not None:
            if profile not in G.PROFILE_KEYS:
                raise RequestError("profile must be quick, standard or deep")
            saved["profile"] = profile
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

    def provider(self, name: str | None = None, profile: str | None = None) -> Any:
        override = getattr(self.svc, "scenario_provider_override", None)        # tests and the demo inject one
        if override is not None:
            return override(name or self.settings()["provider"]) if callable(override) and not hasattr(override, "advance") else override
        return provider_for(self.svc, name or self.settings()["provider"], profile=profile or self.settings()["profile"])

    def readiness(self) -> dict[str, Any]:
        """4.5.2: the provider's state (Ready, Degraded, Unavailable, Configuration error, Circuit open, Budget
        exceeded) with its reason and what to do, from free checks only (levels A and B); the last real functional
        test (level C) is reported, never run here."""
        st = self.settings()
        try:
            p = self.provider(st["provider"])
            r = G.readiness(self.svc, p, getattr(p, "name", st["provider"]), st["profile"])
        except ValueError as exc:
            r = {"ok": False, "detail": str(exc), "state": "config_error", "state_label": G.STATE_WORDS["config_error"], "action": str(exc)}
        return {**r, "provider": st["provider"], "auto": st["auto"], "profile_key": st["profile"]}

    def config_for(self, name: str, profile: str | None = None) -> dict[str, Any]:
        try:
            cfg = dict(self.provider(name, profile).config())
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
        cfg = self.config_for(st["provider"], st["profile"])
        return {"version": PIPELINE_VERSION, "provider": st["provider"], "profile": st["profile"], "config": cfg, "config_hash": C.digest(cfg)[:16],
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
        fw = getattr(self.svc, "forward", None)              # 4.5: the base and no change, frozen before the window opens
        if fw is not None:
            try:
                fw.freeze_study(study_id)
            except Exception:  # noqa: BLE001 - forward evidence never stops the study
                log.exception("Could not freeze the forward forecasts of %s", study_id)
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
        prof_key = p.get("profile") or self.settings()["profile"]
        try:
            prov = self.provider(name, prof_key)
        except ValueError as exc:
            self._fail(study_id, "provider", str(exc))
            return {"failed": str(exc)}
        name = getattr(prov, "name", name)
        governed = name == "mirofish"
        prof = G.profile(self.svc.settings, prof_key) if governed else None
        ready = prov.ready()
        run_id = C.new_id("srun")
        now = C.utc_now()
        external = {"stage": "", "detail": "", "step": 0, "state": {}, "started_at": None, "catalyst_basis": basis, "attempt": p.get("attempt", 1)}
        if governed:
            # Not admitted until admission has run: a crash between here and the decision cannot let a step send it.
            external.update(profile=prof["key"], run_minutes=prof["minutes"], governed=True, admitted=False)
        with self.svc.db.tx() as cur:
            cur.execute("""INSERT INTO scenario_runs (id, seed_id, study_id, provider, status, created_at, schema_version, prompt_version,
                           config_json, external_json) VALUES (?, ?, ?, ?, 'queued', ?, ?, ?, ?, ?)""",
                        (run_id, seed["seed_id"], study_id, name, now, C.SCENARIO_SCHEMA_VERSION, SC.PROMPT_VERSION,
                         json.dumps(p["config"]), json.dumps(external)))
        self._update(study_id, run_id=run_id, seed_id=seed["seed_id"], catalyst_used=spec.model_dump(), catalyst_basis=basis,
                     stages={"provider": {"state": "queued", "detail": f"packet {seed['seed_id']} frozen; waiting for {prov.label}"}})
        self.svc.bus.publish("scenario.run_queued", run_id, study_id=study_id, provider=name)
        if governed:
            gate = self._govern(run_id, study_id, prov, prof, ready)
            if gate is not None:
                return gate
        elif not ready.get("ok"):
            self._finish_run(run_id, "failed", error=f"{prov.label} is not available: {ready.get('detail')}")
            self._fail(study_id, "provider", f"{prov.label} is not available: {ready.get('detail')}")
            return {"failed": ready.get("detail"), "run_id": run_id}
        else:
            G.Ledger(self.svc).open(self._run(run_id), "provider", None, None)
        self._enqueue(STEP, {"run_id": run_id, "n": 0}, f"{STEP}:{run_id}:0")
        control = self._start_control(study_id, seed, name, int(p.get("attempt") or 1))
        return {"run_id": run_id, "seed_id": seed["seed_id"], "control_run_id": control}

    def _govern(self, run_id: str, study_id: str, prov: Any, prof: dict[str, Any], ready: dict[str, Any]) -> dict[str, Any] | None:
        """4.5.2, before a MiroFish run sends anything: the circuit, readiness, then admission (automatic, your
        approval, or refused). None when it may start now."""
        circ = G.Circuit(self.svc)
        cs = circ.state()
        led = G.Ledger(self.svc)
        if cs["state"] == "open":
            why = f"the circuit is open after {cs.get('failures')} provider failures in a row (last: {cs.get('last_error')}) until {cs.get('until')}"
            led.open(self._run(run_id), "provider", prof["key"], None)
            self._finish_run(run_id, "failed", error=f"Not sent: {why}.")
            self._fail(study_id, "provider", f"MiroFish was not called: {why}. The quantitative study stands; reset the circuit once MiroFish works.")
            return {"failed": "circuit open", "run_id": run_id}
        if cs["state"] == "half_open":
            lv = prov.levels() if hasattr(prov, "levels") else {"A": ready}
            ok = bool((lv.get("A") or {}).get("ok")) and bool((lv.get("B") or {"ok": True}).get("ok"))
            if ok:
                circ.success()                         # the one recovery probe (free) passed
            else:
                circ.failure("the recovery probe failed: " + str(((lv.get("B") or lv.get("A") or {}).get("detail")) or "")[:200])
                led.open(self._run(run_id), "provider", prof["key"], None)
                self._finish_run(run_id, "failed", error="Not sent: the circuit's recovery probe failed.")
                self._fail(study_id, "provider", "MiroFish was not called: its recovery check failed, so the circuit is open again.")
                return {"failed": "circuit probe failed", "run_id": run_id}
        if not ready.get("ok"):
            if not ready.get("config_error"):
                circ.failure(str(ready.get("detail"))[:300])
            led.open(self._run(run_id), "provider", prof["key"], None)
            self._finish_run(run_id, "failed", error=f"{prov.label} is not available: {ready.get('detail')}")
            self._fail(study_id, "provider", f"{prov.label} is not available: {ready.get('detail')}")
            return {"failed": ready.get("detail"), "run_id": run_id}
        adm = G.admit(self.svc, prof["key"])
        approval = {"required": adm["decision"] == "approval", "decision": adm["decision"], "reason": adm["reason"], "code": adm["code"],
                    "estimate": adm["estimate"], "profile": prof["key"], "at": C.utc_now()}
        r = self._run(run_id)
        ext = json.loads(r["external_json"] or "{}")
        ext.update(estimate=adm["estimate"], approval=approval, admitted=adm["decision"] == "auto")
        self._save_external(run_id, ext)
        led.open(self._run(run_id), "provider", prof["key"], adm["estimate"], approval)
        if adm["decision"] == "refused":
            self._finish_run(run_id, "failed", error=f"Not sent: {adm['reason']}.")
            self._fail(study_id, "provider", f"MiroFish was not called: {adm['reason']}. The quantitative study stands.")
            self._update(study_id, governance={"state": "budget_exceeded", "reason": adm["reason"], "estimate": adm["estimate"]})
            return {"failed": "budget exceeded", "run_id": run_id}
        if adm["decision"] == "approval":
            self._await(study_id, run_id, approval, f"{prof['label']} run, estimated {adm['estimate']['words']}: waiting for your approval "
                                                    f"({adm['reason']})")
            return {"awaiting_approval": adm["reason"], "run_id": run_id}
        return None

    def _await(self, study_id: str, run_id: str, approval: dict[str, Any], detail: str) -> None:
        self._update(study_id, status="awaiting_approval", stage="provider", governance={"state": "approval", **approval},
                     stages={"provider": {"state": "approval", "detail": detail[:600]}})
        self.svc.bus.publish("scenario.approval", study_id, run_id=run_id, reason=approval.get("reason"))

    def approve(self, study_id: str, profile: str | None = None) -> dict[str, Any]:
        """Approve the simulation spending a waiting run asked for (its estimate, a larger agent count, Deep). It
        approves spending only, never a trade. The budgets are checked again; a different profile may be chosen."""
        p = self.load(study_id)
        if p is None or p.get("status") != "awaiting_approval" or not p.get("run_id"):
            raise RequestError("Nothing is waiting for approval in this study's scenarios.")
        r = self._run(p["run_id"])
        if r["status"] not in ("queued", "running"):
            raise RequestError(f"The run is {r['status']}: nothing to approve.")
        ext = json.loads(r["external_json"] or "{}")
        state = ext.get("state") or {}
        prof_key = profile or ext.get("profile") or self.settings()["profile"]
        if profile and profile != ext.get("profile") and r["status"] != "queued":
            raise RequestError("The run has started: its profile can no longer be changed (cancel, then retry with another profile).")
        prof = G.profile(self.svc.settings, prof_key)
        circ = G.Circuit(self.svc).state()
        if circ["state"] == "open":
            raise RequestError(f"Not approved: the provider's circuit is open after {circ.get('failures')} failures in a row "
                               f"(last: {circ.get('last_error')}). Fix MiroFish and reset the circuit first.")
        adm = G.admit(self.svc, prof["key"], agents=state.get("agents") if (ext.get("approval") or {}).get("code") in ("agents_over_cap", "agents_unknown") else None)
        if adm["decision"] == "refused":
            raise RequestError(f"Not approved: {adm['reason']}.")
        approval = {**(ext.get("approval") or {}), "required": False, "approved": True, "approved_at": C.utc_now(), "profile": prof["key"],
                    "approved_up_to_usd": adm["estimate"].get("high"), "estimate": adm["estimate"], "for": "simulation spending only"}
        if (ext.get("approval") or {}).get("code") == "agents_over_cap":
            state["agents_approved"] = int(state.get("agents") or 0)
            ext["state"] = state
        if (ext.get("approval") or {}).get("code") == "agents_unknown":
            state["agents_unknown_approved"] = True
            ext["state"] = state
        # 4.6.1: the time cap's clock starts again when you approve: the graph built before the approval and the wait
        # for it are not counted against the simulation's time.
        ext.pop("paused_at", None)
        ext["paused_seconds"] = 0.0
        ext["clock_from"] = C.utc_now()
        ext.update(approval=approval, estimate=adm["estimate"], profile=prof["key"], run_minutes=prof["minutes"], admitted=True)
        self._save_external(r["id"], ext)
        G.Ledger(self.svc).open(self._run(r["id"]), "provider", prof["key"], adm["estimate"], approval)
        if profile and profile != p.get("profile"):
            cfg = self.config_for(p["provider"], prof["key"])
            with self.svc.db.tx() as cur:
                cur.execute("UPDATE scenario_runs SET config_json = ? WHERE id = ?", (json.dumps(cfg), r["id"]))
            self._update(study_id, profile=prof["key"], config=cfg)
        self._update(study_id, status="running", governance={"state": "approved", **approval},
                     stages={"provider": {"state": "queued" if r["status"] == "queued" else "running", "detail": "approved; starting"}})
        n = int(ext.get("step") or 0)
        self._enqueue(STEP, {"run_id": r["id"], "n": n}, f"{STEP}:{r['id']}:{n}:approved")
        if not p.get("control_run_id") and r["status"] == "queued":       # the control waits with the provider, then starts beside it
            try:
                self._start_control(study_id, self.scenarios()._seed(r["seed_id"]), p.get("provider") or "mirofish", int(p.get("attempt") or 1))
            except Exception:  # noqa: BLE001 - the control never stops the provider run
                log.exception("Could not start the control of %s", study_id)
        return self.view(study_id)

    def _start_control(self, study_id: str, seed: dict[str, Any], main: str, attempt: int) -> str | None:
        """4.5 (SCENARIO_CONTROL=1): the single-prompt control on the same packet, beside the provider. Its cases are
        sized by the same rules and frozen for the scorecard; they never drive the study's paths, valuation or decision."""
        if not self.svc.settings.scenario_control or main == "ai" or attempt > 1:
            return None
        try:
            prov = self.provider("ai")
        except ValueError:
            return None
        run_id = C.new_id("srun")
        ready = prov.ready()
        ext = {"stage": "", "detail": "", "step": 0, "state": {}, "started_at": None, "role": "control", "attempt": attempt}
        with self.svc.db.tx() as cur:
            cur.execute("""INSERT INTO scenario_runs (id, seed_id, study_id, provider, status, created_at, schema_version, prompt_version,
                           config_json, external_json) VALUES (?, ?, ?, ?, 'queued', ?, ?, ?, ?, ?)""",
                        (run_id, seed["seed_id"], study_id, getattr(prov, "name", "ai"), C.utc_now(), C.SCENARIO_SCHEMA_VERSION,
                         SC.PROMPT_VERSION, json.dumps(self.config_for("ai")), json.dumps(ext)))
        self._update(study_id, control_run_id=run_id)
        G.Ledger(self.svc).open(self._run(run_id), "control", None, None)
        if not ready.get("ok"):
            self._finish_run(run_id, "failed", error=f"The control is not available: {ready.get('detail')}")
            return run_id
        self._enqueue(STEP, {"run_id": run_id, "n": 0}, f"{STEP}:{run_id}:0")
        return run_id

    @staticmethod
    def _role(r: Any) -> str:
        return json.loads(r["external_json"] or "{}").get("role") or "provider"

    # --- 2. the provider's steps --------------------------------------------------------------------------------------
    def _run(self, run_id: str) -> Any:
        r = self.svc.db.one("SELECT * FROM scenario_runs WHERE id = ?", (run_id,))
        if r is None:
            raise RequestError(f"No scenario run {run_id}")
        return r

    def _ctx(self, r: Any) -> dict[str, Any]:
        sc = self.scenarios()
        seed = sc._seed(r["seed_id"])
        run_id = r["id"]

        def checkpoint(state: dict[str, Any]) -> None:
            """4.5.2: saved before a costly write is sent, so a stop mid-request is reconciled, not sent again."""
            row = self._run(run_id)
            ext = json.loads(row["external_json"] or "{}")
            ext["state"] = state
            self._save_external(run_id, ext)
        return {"run_id": r["id"], "checkpoint": checkpoint, "seed": seed, "markdown": SC.markdown(seed, SC.OUTPUT_PROMPT, SC.template_for(seed)),
                "filename": f"{seed['symbol']}-catalyst-evidence-{seed['seed_id'][-8:]}.md", "question": seed["question"],
                "instructions": ("Write the report as Central, Bullish and Bearish cases. End it with one JSON object that follows the "
                                 "packet's output template exactly. No prices, price targets or probabilities.")}

    def _save_external(self, run_id: str, ext: dict[str, Any], status: str | None = None) -> None:
        with self.svc.db.tx() as cur:
            if status:
                cur.execute("UPDATE scenario_runs SET external_json = ?, status = ? WHERE id = ?", (json.dumps(ext, default=str), status, run_id))
            else:
                cur.execute("UPDATE scenario_runs SET external_json = ? WHERE id = ?", (json.dumps(ext, default=str), run_id))
        self._ledger(run_id)

    def _ledger(self, run_id: str) -> None:
        try:
            G.Ledger(self.svc).sync(run_id)
        except Exception:  # noqa: BLE001 - the usage record never stops a run
            log.exception("Could not update the usage record of %s", run_id)

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
        self._ledger(run_id)

    def _fail_run(self, r: Any, stage: str, detail: str, attention: bool = False) -> None:
        """A provider run's failure is the study's; a control run's is only its own."""
        if self._role(r) != "control":
            self._fail(r["study_id"], stage, detail, attention)

    def _stage_of(self, r: Any, **kw: Any) -> None:
        if self._role(r) != "control":
            self._update(r["study_id"], **kw)

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
        prov = self.provider(r["provider"], ext.get("profile"))
        s = self.svc.settings
        if (ext.get("approval") or {}).get("required"):
            return {"skipped": "waiting for your approval"}
        if ext.get("governed") and not ext.get("admitted"):
            # Created but never admitted (the app stopped before admission finished): admit it now, or stop here.
            gate = self._govern(run_id, r["study_id"], prov, G.profile(s, ext.get("profile")), prov.ready())
            if gate is not None:
                return gate
            r = self._run(run_id)
            ext = json.loads(r["external_json"] or "{}")
        if r["status"] == "queued":
            if r["provider"] == "mirofish":
                # A run paused for your approval (after its graph, before its agents) does not hold a MiroFish slot.
                busy = self.svc.db.one("""SELECT COUNT(*) AS n FROM scenario_runs WHERE provider = 'mirofish' AND status = 'running' AND id != ?
                                          AND COALESCE(json_extract(external_json, '$.approval.required'), 0) != 1""", (run_id,))["n"]
                if busy >= s.mirofish_concurrency:
                    self._save_external(run_id, {**ext, "detail": f"waiting for a free MiroFish slot ({busy} running)"})
                    raise Deferred(_now() + timedelta(seconds=WAIT_TURN), "waiting for a free MiroFish slot")
            ext["started_at"] = C.utc_now()
            self._save_external(run_id, ext, status="running")
            self._stage_of(r, stages={"provider": {"state": "running", "detail": f"{prov.label} started"}})
        minutes = float(ext.get("run_minutes") or s.mirofish_run_minutes)
        # Time spent waiting for your approval, and anything before it, is not the run's time.
        if (cap_seconds(ext) or 0.0) > minutes * 60.0:
            return self._stop(r, ext, f"it took longer than its time cap ({minutes:g} minutes; MIROFISH_RUN_MINUTES and the profile)")
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
            self._fail_run(r, "provider", f"{prov.label} failed: {err}")
            return {"failed": err}
        if out.approval:                                          # 4.5.2: the agent check asks first
            ext.update({"state": out.state, "stage": out.stage, "detail": out.detail, "step": n + 1, "last_step_at": C.utc_now()})
            ext["approval"] = {**out.approval, "required": True, "decision": "approval", "at": C.utc_now(), "profile": ext.get("profile")}
            ext["paused_at"] = C.utc_now()
            self._save_external(run_id, ext)
            self._await(r["study_id"], run_id, ext["approval"], f"{out.detail}")
            return {"awaiting_approval": out.approval.get("reason")}
        ext.update({"state": out.state, "stage": out.stage, "detail": out.detail, "step": n + 1, "last_step_at": C.utc_now()})
        if out.model:
            ext["model"] = out.model
        if out.usage:
            ext["usage"] = out.usage
        if out.failed:
            self._save_external(run_id, ext)
            if r["provider"] == "mirofish":
                G.Circuit(self.svc).failure(out.failed)            # 4.5.2: a provider failure (never a cancel or bad cases)
            self._finish_run(run_id, "failed", error=out.failed)
            self._fail_run(r, "provider", f"{prov.label}: {out.failed}")
            return {"failed": out.failed}
        if out.attention:
            ext["ambiguous_outcome"] = True          # 4.5.1: a paid request may have run: a new run needs your confirmation
            self._save_external(run_id, ext)
            self._finish_run(run_id, "needs_attention", error=out.attention)
            self._fail_run(r, "provider", f"{prov.label}: {out.attention}", attention=True)
            return {"attention": out.attention}
        if out.report is not None:
            if r["provider"] == "mirofish":
                G.Circuit(self.svc).success()
            ext["parsed_cases"] = out.cases
            ext["report_received_at"] = C.utc_now()
            with self.svc.db.tx() as cur:
                cur.execute("UPDATE scenario_runs SET external_json = ?, report_text = ?, model_version = ? WHERE id = ?",
                            (json.dumps(ext, default=str), out.report[:REPORT_LIMIT], out.model, run_id))
            self._ledger(run_id)
            self._stage_of(r, stages={"provider": {"state": "done", "detail": f"{prov.label}: report received"}}, stage="paths")
            self._enqueue(FINISH, {"run_id": run_id}, f"{FINISH}:{run_id}")
            return {"report": True}
        self._save_external(run_id, ext)                          # every acknowledged id is saved before the next step is queued
        self._stage_of(r, stages={"provider": {"state": "running", "detail": f"{out.stage}: {out.detail}"[:300]}})
        self._enqueue(STEP, {"run_id": run_id, "n": n + 1}, f"{STEP}:{run_id}:{n + 1}", delay=out.next_in or 0.0)
        return {"next_in": out.next_in, "stage": out.stage}

    def _stop(self, r: Any, ext: dict[str, Any], why: str) -> dict[str, Any]:
        prov = self.provider(r["provider"], ext.get("profile"))
        if r["provider"] == "mirofish":
            G.Circuit(self.svc).failure(f"stopped: {why}")
        said = prov.stop(ext.get("state") or {}) if hasattr(prov, "stop") else "nothing to stop"
        self._finish_run(r["id"], "failed", error=f"Stopped: {why} ({said}).")
        self._fail_run(r, "provider", f"{prov.label} was stopped: {why} ({said}).")
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
            # The report's own cases are wrong (another packet's, or invalid): reading it again would fail the same way.
            ext["cases_unusable"] = str(exc)[:300]
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

    # Finalization (4.5.1): each state is saved before the next begins, and every step reuses what an earlier,
    # interrupted pass already wrote, so a paid report is never lost to a local failure and never paid for again.
    FINAL_STATES = ("report_received", "validated", "version_saved", "valued", "findings_saved", "complete")

    def _obsolete(self, r: Any, p: dict[str, Any] | None) -> str | None:
        """Why a run's results must not be applied to its study (None when they may): the pipeline moved on to another
        attempt, or was canceled."""
        if p is None:
            return "the study has no pipeline"
        if self._role(r) == "control":
            return None if p.get("control_run_id") == r["id"] else "a newer control run replaced it"
        if p.get("run_id") != r["id"]:
            return "a newer attempt replaced it"
        if p.get("status") == "canceled":
            return "the pipeline was canceled"
        return None

    def _finalized(self, r: Any, state: str) -> None:
        ext = json.loads(self._run(r["id"])["external_json"] or "{}")
        ext["finalize"] = {"state": state, "at": C.utc_now()}
        self._save_external(r["id"], ext)
        if self._role(r) != "control":
            self._update(r["study_id"], finalize={"run_id": r["id"], "state": state})

    def _auto_version(self, run_id: str, role: str) -> dict[str, Any] | None:
        origin = "control" if role == "control" else "auto"
        v = self.svc.db.one("SELECT id FROM scenario_versions WHERE run_id = ? AND origin = ? ORDER BY version LIMIT 1", (run_id, origin))
        return self.scenarios().version(v["id"]) if v else None

    def finish(self, payload: dict[str, Any]) -> dict[str, Any]:
        run_id = payload["run_id"]
        r = self._run(run_id)
        resume = bool(payload.get("resume"))
        if r["status"] in ("canceled", "failed") or (r["status"] == "needs_attention" and not resume):
            return {"skipped": f"the run is {r['status']}"}
        study_id = r["study_id"]
        role = self._role(r)
        why_not = self._obsolete(r, self.load(study_id))
        if why_not:
            return {"skipped": f"its results are not applied: {why_not}"}
        sc = self.scenarios()
        seed = sc._seed(r["seed_id"])
        ext = json.loads(r["external_json"] or "{}")
        if not (r["report_text"] or "").strip() and r["status"] != "complete":
            return {"skipped": "no report was received for this run"}
        # 1-2. report_received → validated: the cases parsed and checked against the frozen packet, saved on the run.
        if r["status"] != "complete":
            imp, why = self.cases_from(r, seed, ext)
            excerpt = (r["report_text"] or "").strip()[: SC.MAX_EXCERPT] or None
            if imp is None:
                self._save_external(run_id, ext)
                self._finish_run(run_id, "needs_attention", error=why, report_excerpt=excerpt)
                self._fail_run(r, "paths", why if ext.get("cases_unusable") else f"{why} The report is kept: Retry reads it again without a new run.",
                               attention=True)
                return {"attention": why}
            cases = sc.normalized(imp)
            with self.svc.db.tx() as cur:
                cur.execute("""UPDATE scenario_runs SET status = 'complete', completed_at = ?, response_hash = ?, cases_json = ?, report_excerpt = ?,
                               error = NULL WHERE id = ? AND status IN ('running', 'needs_attention')""",
                            (C.utc_now(), C.digest(imp.model_dump()), json.dumps(cases), excerpt, run_id))
                if cur.rowcount == 0:
                    return {"skipped": "the run changed while its report was read"}
            self._ledger(run_id)
            self._finalized(r, "validated")
            self.svc.bus.publish("scenario.imported", run_id, study_id=study_id, symbol=seed["symbol"], cases=len(cases), provider=r["provider"])
        r = self._run(run_id)
        cases = json.loads(r["cases_json"] or "[]")
        # 3. version_saved: one automatic version per run; a resumed pass finds the one it saved before.
        ver = self._auto_version(run_id, role)
        assumptions, notes = self.auto_assumptions(seed, cases)
        if ver is None:
            try:
                ver = sc.create_version(run_id, C.ScenarioVersionIn(cases=[C.CaseAssumptions(**a) for a in assumptions]),
                                        origin="control" if role == "control" else "auto")
            except (RequestError, ValueError) as exc:
                ext = json.loads(self._run(run_id)["external_json"] or "{}")
                ext["cases_unusable"] = str(exc)[:300]           # the same cases would fail again: Retry starts a new run
                self._save_external(run_id, ext)
                self._fail_run(r, "paths", f"The cases were saved but no price paths could be made from them: {exc}")
                return {"failed": str(exc)}
            except Exception as exc:  # noqa: BLE001 - another pass saved it first (the one-per-run index)
                ver = self._auto_version(run_id, role)
                if ver is None:
                    raise
                log.info("Reused the version another pass saved for %s (%s)", run_id, exc.__class__.__name__)
        self._finalized(r, "version_saved")
        fw = getattr(self.svc, "forward", None)              # 4.5: frozen for the scorecard (late if the window has opened)
        if fw is not None:
            try:
                fw.freeze_version(study_id, ver, self._run(run_id), role)
            except Exception:  # noqa: BLE001 - forward evidence never stops the pipeline
                log.exception("Could not freeze the forward forecast of %s", run_id)
        if role == "control":
            self._finalized(r, "complete")
            return {"version_id": ver["id"], "role": "control"}
        why_not = self._obsolete(r, self.load(study_id))
        if why_not:
            return {"skipped": f"the version was saved; its valuation is not applied: {why_not}", "version_id": ver["id"]}
        self._update(study_id, version_id=ver["id"], sizing_notes=notes,
                     stages={"paths": {"state": "running", "detail": f"version {ver['version']} saved; valuing the strategies"}})
        out = self.revalue(study_id, ver, finalizing=run_id)
        return out

    def revalue(self, study_id: str, ver: dict[str, Any] | None = None, finalizing: str | None = None) -> dict[str, Any]:
        """Price the study's fixed candidates under a version from one fresh snapshot, and write the findings.

        While finishing a run (`finalizing`, 4.5.1), a valuation already saved for this version by an interrupted pass
        is reused rather than made again, and nothing is written if the pipeline was canceled or moved on meanwhile."""
        sc = self.scenarios()
        p = self.load(study_id) or {}
        if not finalizing and p.get("status") == "canceled":
            raise RequestError("This attempt was canceled: nothing from it is valued or decided. Retry to run it again.")
        if ver is None:
            if not p.get("version_id"):
                raise RequestError("This study has no scenario version to value yet.")
            ver = sc.version(p["version_id"])
        study = self.svc.research3.get(study_id)
        res, vid = None, None
        if finalizing:
            # A valuation an interrupted pass saved for this (automatic, this run's own) version is reused, also when
            # the pass stopped before recording it on the pipeline.
            old = None
            try:
                old = V.load(self.svc, p["valuation_id"]) if p.get("valuation_id") else V.latest_for_version(self.svc, ver["id"])
            except RequestError:
                old = None
            if old is not None and old.get("version_id") == ver["id"]:
                res, vid = old, old["id"]
        if res is None:
            try:
                res, snap = V.value(self.svc, study, ver)
                vid = V.save(self.svc, res, snap)
                res["id"] = vid
            except (RequestError, DataError, ValueError) as exc:
                res, vid = {"status": "failed", "problems": [str(exc)[:300]], "structures": [], "mixes": [], "failed": []}, None
        if finalizing:
            r = self._run(finalizing)
            why_not = self._obsolete(r, self.load(study_id))
            if why_not:
                return {"skipped": f"the valuation is not applied: {why_not}", "version_id": ver["id"], "valuation_id": vid}
            if vid:
                self._update(study_id, valuation_id=vid)
                self._finalized(r, "valued")
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
        if finalizing:
            r = self._run(finalizing)
            self._finalized(r, "findings_saved")
            self._finalized(r, "complete")
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
        run = self.svc.db.one("SELECT external_json FROM scenario_runs WHERE id = ?", (ver.get("run_id"),)) if ver.get("run_id") else None
        state = (json.loads(run["external_json"] or "{}").get("state") or {}) if run else {}
        if state.get("stopped_early"):
            words.append(f"The simulation was stopped early ({state.get('rounds') or '?'} of {state.get('total_rounds') or '?'} rounds): "
                         "its cases come from an unfinished run.")
        return {"conclusion": " ".join(words), "cases": cases, "best_mix": best, "valuation_status": val.get("status"),
                "stopped_early": bool(state.get("stopped_early")),
                "readiness": {k: rd.get(k) for k in ("state", "label", "directional", "range")}, "made_at": C.utc_now()}

    # --- cancel, retry, recover ----------------------------------------------------------------------------------------
    def _cancel_run(self, run_id: str | None) -> str | None:
        """Mark one run canceled (if it is still going) and ask its provider to stop; what the provider said."""
        if not run_id:
            return None
        r = self.svc.db.one("SELECT * FROM scenario_runs WHERE id = ?", (run_id,))
        if r is None or r["status"] not in ("queued", "running"):
            return None
        with self.svc.db.tx() as cur:
            cur.execute("UPDATE scenario_runs SET status = 'canceled', completed_at = ?, error = 'canceled by you' "
                        "WHERE id = ? AND status IN ('queued', 'running')", (C.utc_now(), r["id"]))
        self._ledger(r["id"])
        ext = json.loads(r["external_json"] or "{}")
        try:
            prov = self.provider(r["provider"])
            return prov.stop(ext.get("state") or {}) if hasattr(prov, "stop") else "stopped"
        except ValueError:
            return "stopped"

    def cancel(self, study_id: str) -> dict[str, Any]:
        """Stop the attempt: the provider run and the control beside it (each asked to stop on its server), and any
        step of it still queued. A report that arrives later, a valuation or a decision of the canceled attempt is
        never applied. What was already validated (an earlier version, valuation and decision) is kept."""
        p = self.load(study_id)
        if p is None:
            raise RequestError("This study has no scenario pipeline.")
        said = self._cancel_run(p.get("run_id")) or "nothing was running"
        ctrl = self._cancel_run(p.get("control_run_id"))
        if not p.get("run_id") and p.get("status") not in ("running", "awaiting_approval"):
            raise RequestError(f"Nothing to cancel: the pipeline is {p.get('status') or 'idle'}.")
        if p.get("status") not in ("running", "awaiting_approval") and said == "nothing was running" and ctrl is None:
            raise RequestError(f"Nothing to cancel: the pipeline is {p.get('status') or 'idle'}.")
        with self.svc.db.tx() as cur:          # its queued steps: they would only find the run canceled
            cur.execute("UPDATE jobs SET status = 'discarded', finished_at = ?, error = 'the scenario run was canceled' WHERE status = 'queued' "
                        "AND kind IN (?, ?) AND (payload_json LIKE ? OR payload_json LIKE ?)",
                        (C.utc_now(), STEP, FINISH, f'%"{p.get("run_id") or "-"}"%', f'%"{p.get("control_run_id") or "-"}"%'))
        self._update(study_id, status="canceled", error=f"Canceled by you ({said}).", canceled_attempt=int(p.get("attempt") or 1),
                     stages={p.get("stage") or "provider": {"state": "canceled", "detail": f"canceled ({said})"}})
        return self.view(study_id)

    @staticmethod
    def _resumable(p: dict[str, Any], run: Any) -> bool:
        """Whether Retry can finish the saved report again instead of starting a new run."""
        if run is None or p.get("status") == "canceled":
            return False
        rext = json.loads(run["external_json"] or "{}")
        return bool((run["report_text"] or "").strip() and run["status"] in ("complete", "needs_attention", "running")
                    and not rext.get("cases_unusable") and (p.get("finalize") or {}).get("state") != "complete")

    def retry(self, study_id: str, confirm_new_run: bool = False) -> dict[str, Any]:
        """Retry the pipeline, without paying for another run when it need not:

        - only the valuation failed: value again;
        - the provider's report arrived but finishing it failed (it could not be read, a local write failed): finish it
          again from the saved report, with no new run;
        - a paid request's answer never came and could not be reconciled (*needs attention*): a new run only with
          `confirm_new_run`, since the first may have run (and cost) on the server;
        - otherwise a new provider run (a new attempt: a failed or abandoned one is never resumed blindly)."""
        p = self.load(study_id)
        if p is None:
            raise RequestError("This study has no scenario pipeline: run it with scenarios on.")
        if p.get("status") in ("running",):
            raise RequestError("The scenario pipeline is still running.")
        if p.get("status") == "awaiting_approval":
            raise RequestError("This study's run is waiting for your approval: approve it, or cancel it before retrying.")
        if p.get("version_id") and p.get("status") == "complete" and p.get("stages", {}).get("paths", {}).get("state") == "failed":
            self.revalue(study_id)                                     # only the valuation failed: value again
            return self.view(study_id)
        run = self.svc.db.one("SELECT * FROM scenario_runs WHERE id = ?", (p["run_id"],)) if p.get("run_id") else None
        if run is not None and not confirm_new_run and self._resumable(p, run):
            self._update(study_id, status="running", stage="paths", error=None,
                         stages={"paths": {"state": "running", "detail": "finishing the saved report again: no new run"}})
            n = int(json.loads(run["external_json"] or "{}").get("finish_tries") or 0) + 1
            ext = json.loads(run["external_json"] or "{}")
            ext["finish_tries"] = n
            self._save_external(run["id"], ext)
            self._enqueue(FINISH, {"run_id": run["id"], "resume": True}, f"{FINISH}:{run['id']}:resume:{n}")
            return self.view(study_id)
        rext = json.loads(run["external_json"] or "{}") if run is not None else {}
        if run is not None and run["status"] == "needs_attention" and rext.get("ambiguous_outcome") and not confirm_new_run:
            raise RequestError("The last run's paid request may have run on the provider's server without an answer reaching the app. "
                               "Check the server first; to start a new run anyway (it may cost model calls), confirm a new run.")
        attempt = int(p.get("attempt") or 1) + 1
        st = self.settings()
        cfg = self.config_for(st["provider"], st["profile"])
        history = list(p.get("history") or [])
        history.append({k: p.get(k) for k in ("attempt", "run_id", "control_run_id", "version_id", "valuation_id", "decision", "status", "error")})
        p = self._update(study_id, attempt=attempt, provider=st["provider"], profile=st["profile"], governance=None, config=cfg,
                         config_hash=C.digest(cfg)[:16], status="running",
                         stage="provider", error=None, run_id=None, control_run_id=None, version_id=None, valuation_id=None, findings=None,
                         decision=None, finalize=None, history=history[-10:],
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
            if p.get("status") in ("failed", "canceled", "needs_attention", "awaiting_approval"):
                continue
            if p.get("status") == "complete" and (not p.get("finalize") or (p.get("finalize") or {}).get("state") == "complete"):
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
            elif run["status"] == "complete" and ((p.get("finalize") and p["finalize"].get("state") != "complete")
                                                  or (not p.get("finalize") and not p.get("version_id"))):
                # 4.5.1: validated (and perhaps a version saved) when the process stopped: finish it from what is saved.
                n = int(json.loads(run["external_json"] or "{}").get("finish_tries") or 0)
                key = f"{FINISH}:{run['id']}:recover:{n}"
                if job_status(key) is None and n < 5:
                    ext = json.loads(run["external_json"] or "{}")
                    ext["finish_tries"] = n + 1
                    self._save_external(run["id"], ext)
                    self._enqueue(FINISH, {"run_id": run["id"], "resume": True}, f"{FINISH}:{run['id']}:recover:{n + 1}")
                    queued.append(run["id"])
                elif n >= 5:
                    self._fail(r["id"], "paths", "Finishing the provider's report failed after several tries; the report is kept: press Retry.")
        # A study left in the evidence stage by a process that stopped: the study itself is marked failed by its sweep.
        for r in self.svc.db.query("SELECT id, status, pipeline_json FROM studies WHERE pipeline_json IS NOT NULL AND status = 'failed'"):
            p = json.loads(r["pipeline_json"])
            if p.get("status") in (None, "running") and p.get("stage", "evidence") == "evidence":
                self._update(r["id"], status="failed", stage="evidence", stages={"evidence": {"state": "failed", "detail": "the study failed"}})
        return {"queued": queued}

    # --- the view ------------------------------------------------------------------------------------------------
    def _decision(self, study_id: str, p: dict[str, Any]) -> dict[str, Any] | None:
        return _decision_of(self.svc, study_id, p)

    def _has_forward(self) -> bool:
        return getattr(self.svc, "forward", None) is not None

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
                       "cap_seconds": _round1(cap_seconds(ext, _ts(r["completed_at"]))),
                       "run_minutes": ext.get("run_minutes"),
                       "remote": {k: state.get(k) for k in ("project_id", "graph_id", "simulation_id", "report_id") if state.get(k)},
                       "error": r["error"], "model": ext.get("model"), "catalyst_basis": ext.get("catalyst_basis"),
                       "report_excerpt": r["report_excerpt"], "seed_id": r["seed_id"], "rounds": state.get("rounds"),
                       "total_rounds": state.get("total_rounds"), "stopped_early": bool(state.get("stopped_early")),
                       "report_received": bool((r["report_text"] or "").strip()), "ambiguous_outcome": bool(ext.get("ambiguous_outcome"))}
        usage = None
        if p.get("run_id"):
            u = self.svc.db.one("SELECT * FROM scenario_usage WHERE run_id = ?", (p["run_id"],))
            usage = G.Ledger.view(dict(u)) if u else None
        val = None
        if p.get("valuation_id"):
            try:
                val = V.load(self.svc, p["valuation_id"])
            except RequestError:
                val = None
        status = p.get("status") or "running"
        if run is not None and p.get("run_id"):
            rr = self.svc.db.one("SELECT external_json FROM scenario_runs WHERE id = ?", (p["run_id"],))
            rext = json.loads(rr["external_json"] or "{}") if rr else {}
            run.update(profile=rext.get("profile"), estimate=rext.get("estimate"), approval=rext.get("approval"),
                       agents=(rext.get("state") or {}).get("agents"), run_minutes=rext.get("run_minutes"))
        return {"status": status, "stage": p.get("stage"), "provider": prov, "provider_label": stage_label("provider", prov),
                "attempt": p.get("attempt", 1), "stages": stages, "run": run, "seed_id": p.get("seed_id"), "version_id": p.get("version_id"),
                "valuation": val, "findings": p.get("findings"), "decision": self._decision(study_id, p), "error": p.get("error"),
                "catalyst": p.get("catalyst_used"), "catalyst_basis": p.get("catalyst_basis"), "sizing_notes": p.get("sizing_notes") or [],
                "forward": [{k: r[k] for k in ("variant", "status", "median_pct", "low_pct", "high_pct", "realized_pct", "abs_error")}
                            for r in self.svc.db.query("SELECT * FROM scenario_forecasts WHERE study_id = ? ORDER BY variant", (study_id,))]
                if self._has_forward() else [],
                "finalize": p.get("finalize"), "history": p.get("history") or [],
                "profile": p.get("profile"), "governance": p.get("governance"), "usage": usage,
                "can_approve": status == "awaiting_approval",
                "retry_needs_confirmation": bool(run and run["status"] == "needs_attention" and run.get("ambiguous_outcome")),
                "retry_reuses_report": bool(p.get("run_id") and self._resumable(p, self.svc.db.one("SELECT * FROM scenario_runs WHERE id = ?",
                                                                                                   (p["run_id"],)))),
                "can_cancel": status in ("running", "awaiting_approval"), "can_retry": status in ("failed", "needs_attention", "canceled")
                or (status == "complete" and (p.get("stages") or {}).get("paths", {}).get("state") == "failed"),
                "updated_at": p.get("updated_at"), "demo": prov == "demo"}


def _decision_of(svc: Any, study_id: str, p: dict[str, Any]) -> dict[str, Any] | None:
    op = getattr(svc, "opportunities", None)
    if op is None or not p.get("decision"):
        return None
    try:
        return op.latest(study_id)
    except Exception:  # noqa: BLE001 - the view is still readable without its decision
        log.exception("Could not read the decision of %s", study_id)
        return None


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
            out = pl.save_settings(body.provider, body.auto, body.profile)
            return {**out, "readiness": pl.readiness()}

        @router.post("/studies/{study_id}/pipeline/approve")
        def approve(study_id: str, body: ApproveIn | None = None) -> dict[str, Any]:
            return {"pipeline": pl.approve(study_id, body.profile if body else None)}

        @router.get("/scenarios/usage")
        def usage() -> dict[str, Any]:
            return {**G.Ledger(svc).report(), "budgets": G.budgets(svc)}

        @router.post("/scenarios/usage/{run_id}")
        def actual(run_id: str, body: ActualCostIn) -> dict[str, Any]:
            if not (0 <= body.actual_usd <= 10_000):
                raise RequestError("actual_usd must be between 0 and 10,000")
            return {"run": G.Ledger(svc).enter_actual(run_id, body.actual_usd, body.note)}

        @router.get("/scenarios/estimate")
        def estimate(profile: str = "quick") -> dict[str, Any]:
            if profile not in G.PROFILE_KEYS:
                raise RequestError("profile must be quick, standard or deep")
            adm = G.admit(svc, profile)
            return {"profile": adm["profile"], "estimate": adm["estimate"], "budgets": adm["budgets"],
                    "admission": {k: adm[k] for k in ("decision", "code", "reason")}}

        @router.post("/scenarios/circuit/reset")
        def reset_circuit() -> dict[str, Any]:
            return {"circuit": G.Circuit(svc).reset(), "readiness": pl.readiness()}

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
        def retry(study_id: str, body: RetryIn | None = None) -> dict[str, Any]:
            return {"pipeline": pl.retry(study_id, confirm_new_run=bool(body and body.confirm_new_run))}

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
