"""Scenario providers for the one-click Research pipeline (4.3).

A provider turns a frozen evidence packet (v3/scenarios.py) into a report with Central, Bullish and Bearish cases.
Each one is driven in short, bounded steps (`advance`): the pipeline's job runs one step, saves what the provider
acknowledged (its project, task and simulation ids) **before** queuing the next, and so a restart resumes from the
recorded ids instead of starting another paid run.

- **MiroFish** (`mirofish`): the self-hosted open-source server, pinned at upstream commit 7657031, on a loopback
  address (MIROFISH_URL). Its stages, in its own routes:

      ontology   POST /api/graph/ontology/generate   (multipart: the packet as a .md file, the question)
      build      POST /api/graph/build, then GET /api/graph/task/{task_id}
      simulate   POST /api/simulation/create, POST /api/simulation/prepare, POST /api/simulation/prepare/status,
                 POST /api/simulation/start (max_rounds), GET /api/simulation/{id}/run-status
      report     POST /api/report/generate, POST /api/report/generate/status, GET /api/report/by-simulation/{id}

  A reply with `"success": false` is a failure even when its HTTP status is 200. A server error or a dropped
  connection is retried with growing waits (a few times). A write whose reply never came (a timeout) may have run on
  the server, so it is never simply sent again: the step reconciles by what it can find (the project by its name, the
  simulation by its project, the run's status) and, when it cannot, stops at **needs attention** for a person to decide.
  The server calls its own language model and Zep with its own keys; it is sent only the shareable packet.

- **AI** (`ai`): one call to the AI set up in .env with the same packet and template: the single-prompt control the
  simulation is compared against (4.5).

- **Demo** (`demo`): canned cases built from the packet, at once, for trying the workflow on demo data. Labelled as
  such everywhere: it simulates nothing.

Every provider proposes conditions, directions and reasons only. The app validates the cases against the packet and
sizes them with its own deterministic rules; nothing a provider writes becomes a price or a probability.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from typing import Any

import requests

PINNED_UPSTREAM = "7657031ac01184afe2cb220f5ee3545573b5e843"
TRANSIENT_TRIES = 5


@dataclass
class Step:
    state: dict[str, Any]
    next_in: float | None = None                 # seconds until the next step
    report: str | None = None                    # done: the provider's report, as text
    cases: dict[str, Any] | None = None          # done: the cases, when the provider answered in the schema itself
    failed: str | None = None                    # stopped: why
    attention: str | None = None                 # stopped for a person: an unanswered write that could not be reconciled
    stage: str = ""
    detail: str = ""
    model: str | None = None
    usage: dict[str, Any] = field(default_factory=dict)
    approval: dict[str, Any] | None = None       # 4.5.2: paused for your approval (more agents than the profile allows)


class Transient(Exception):
    """The server is unreachable or answered with a server error: try the same step again later."""


class Ambiguous(Exception):
    """A write's reply never came: it may have run on the server."""


class Failed(Exception):
    """The server answered that the request failed."""


def _never_connected(exc: Exception) -> bool:
    text = f"{exc!r} {exc}"
    return any(w in text for w in ("NewConnectionError", "Connection refused", "Failed to establish", "Name or service not known",
                                   "nodename nor servname", "No route to host"))


def backoff(polls: int, first: float = 5.0, most: float = 30.0) -> float:
    return float(min(most, first * (1.5 ** max(0, polls))))


WRITES = ("ontology", "sim_create", "start")      # 4.5.2: writes the server does not make safe to repeat


class MiroFish:
    name = "mirofish"
    label = "MiroFish"

    def __init__(self, url: str, *, session: Any = None, timeout: float = 300.0, max_rounds: int = 10, poll_first: float = 5.0,
                 poll_most: float = 30.0, max_agents: int | None = None, platform: str = "parallel", parallel_profiles: int = 5,
                 profile: str | None = None):
        self.url = url.rstrip("/")
        self.http = session or requests.Session()
        self.timeout = float(timeout)
        self.max_rounds = int(max_rounds)
        self.poll_first, self.poll_most = poll_first, poll_most
        self.max_agents = int(max_agents) if max_agents else None
        self.platform = platform if platform in ("parallel", "reddit", "twitter") else "parallel"
        self.parallel_profiles = max(1, int(parallel_profiles))
        self.profile = profile

    def config(self) -> dict[str, Any]:
        out = {"provider": self.name, "url": self.url, "max_rounds": self.max_rounds, "upstream": PINNED_UPSTREAM}
        if self.profile:                       # 4.5.2 (a 4.5.0 run's configuration had none, so its key is unchanged)
            out.update(profile=self.profile, max_agents=self.max_agents, platform=self.platform, parallel_profiles=self.parallel_profiles)
        return out

    def ready(self) -> dict[str, Any]:
        """Level A, connection: the server answers /health as MiroFish's backend does. Free (no model call)."""
        if not self.url:
            return {"ok": False, "config_error": True, "detail": "MIROFISH_URL is not set: point it at your self-hosted MiroFish server (http://127.0.0.1:5001).",
                    "action": "Set MIROFISH_URL=http://127.0.0.1:5001 in .env and restart."}
        try:
            r = self.http.get(f"{self.url}/health", timeout=5)
        except requests.RequestException as exc:
            return {"ok": False, "detail": f"MiroFish at {self.url} did not answer ({exc.__class__.__name__}). Start its backend."}
        if r.status_code >= 300:
            return {"ok": False, "detail": f"MiroFish at {self.url} answered HTTP {r.status_code} to /health."}
        try:
            body = r.json()
        except ValueError:
            body = None
        if isinstance(body, dict) and body.get("service") and "mirofish" not in str(body.get("service")).lower():
            return {"ok": False, "config_error": True, "detail": f"Something answered at {self.url}, but it is {body.get('service')!r}, not MiroFish.",
                    "action": "Point MIROFISH_URL at MiroFish's backend (its default port is 5001)."}
        return {"ok": True, "detail": f"MiroFish at {self.url} is up.", "service": (body or {}).get("service") if isinstance(body, dict) else None}

    def levels(self) -> dict[str, Any]:
        """Level A (connection) and level B (dependencies the server lets the app see, all free): its project and
        simulation lists answer in its own format, which also shows its storage works. Its language model and Zep keys:
        MiroFish's backend refuses to start without both, so a server that answers has them set; whether they work is
        shown only by a functional test (level C, `--scenario-smoke`, which costs model calls)."""
        a = self.ready()
        if not a.get("ok"):
            return {"A": a}
        problems = []
        for path in ("/api/graph/project/list", "/api/simulation/list"):
            try:
                self._req("GET", path, write=False, params={"limit": 1}, timeout=10)
            except (Transient, Failed, Ambiguous) as exc:
                problems.append(f"{path}: {exc}")
        b = ({"ok": True, "detail": "Its project and simulation lists answer in MiroFish's format; its model and Zep keys are set (it does not "
                                    "start without them)."}
             if not problems else {"ok": False, "detail": "MiroFish answers /health but not its API: " + "; ".join(problems)[:400]})
        return {"A": a, "B": b}

    # --- transport ------------------------------------------------------------------------------------------------
    def _req(self, method: str, path: str, *, write: bool, timeout: float | None = None, **kw: Any) -> dict[str, Any]:
        try:
            r = self.http.request(method, f"{self.url}{path}", timeout=timeout or self.timeout, **kw)
        except requests.Timeout as exc:
            if write and not isinstance(exc, requests.ConnectTimeout):
                raise Ambiguous(f"{method} {path} timed out: it may have run on the server") from exc
            raise Transient(f"{method} {path} timed out") from exc
        except requests.RequestException as exc:
            # 4.5.2: a write may have run unless it provably never reached the server (no connection was made): a
            # broken connection, a reply cut off mid-body or one that could not be decoded are all ambiguous.
            if write and not (isinstance(exc, requests.ConnectionError) and _never_connected(exc)):
                raise Ambiguous(f"{method} {path} did not complete ({exc.__class__.__name__}): it may have run on the server") from exc
            raise Transient(f"MiroFish did not answer {method} {path} ({exc.__class__.__name__})") from exc
        try:
            body = r.json()
        except ValueError:
            body = None
        if r.status_code >= 500:
            msg = f"{method} {path}: HTTP {r.status_code}" + (f" ({str(body.get('error'))[:300]})" if isinstance(body, dict) and body.get("error") else "")
            if write:
                raise Failed(msg)            # the server ran it and failed: a write is not sent again blindly (it may cost)
            raise Transient(msg)
        if not isinstance(body, dict):
            raise Failed(f"{method} {path}: HTTP {r.status_code} with a reply that is not JSON")
        if body.get("success") is False or r.status_code >= 400:
            raise Failed(f"{method} {path}: {str(body.get('error') or f'HTTP {r.status_code}')[:300]}")
        data = body.get("data")
        return data if isinstance(data, dict) else {"items": data} if isinstance(data, list) else {}

    def _poll(self, state: dict[str, Any], stage: str, detail: str) -> Step:
        n = int(state.get("polls") or 0)
        return Step({**state, "polls": n + 1}, next_in=backoff(n, self.poll_first, self.poll_most), stage=stage, detail=detail)

    @staticmethod
    def _go(state: dict[str, Any], stage: str, **ids: Any) -> dict[str, Any]:
        return {**{k: v for k, v in state.items() if k != "sending"}, **ids, "stage": stage, "polls": 0, "transient": 0}

    # --- the stages -----------------------------------------------------------------------------------------------
    def advance(self, ctx: dict[str, Any], state: dict[str, Any]) -> Step:
        stage = state.get("stage") or "ontology"
        try:
            if stage in WRITES:
                # 4.5.2: a write that may cost is recorded as being sent before it is sent. Finding that mark again means
                # the process stopped mid-request: it may have run on the server, so it is reconciled, never sent again.
                if state.get("sending") == stage:
                    raise Ambiguous(f"the {stage} request was being sent when the app stopped: it may have run on the server")
                checkpoint = ctx.get("checkpoint")
                if callable(checkpoint):
                    checkpoint({**state, "sending": stage})
            step = getattr(self, f"_{stage}")(ctx, state)
        except Transient as exc:
            tries = int(state.get("transient") or 0) + 1
            if tries > TRANSIENT_TRIES:
                return Step(state, failed=f"MiroFish was unreachable or failing at the {stage} stage after {TRANSIENT_TRIES} tries: {exc}",
                            stage=stage)
            return Step({**state, "transient": tries}, next_in=backoff(tries, 10.0, 120.0), stage=stage, detail=f"retrying: {exc}")
        except Ambiguous as exc:
            return Step({**{k: v for k, v in state.items() if k != "sending"}, "stage": f"{stage}_reconcile", "reconcile_tries": 0, "ambiguous": str(exc)},
                        next_in=10.0, stage=stage, detail=str(exc))
        except Failed as exc:
            return Step(state, failed=str(exc), stage=stage)
        return step

    def _ontology(self, ctx: dict[str, Any], state: dict[str, Any]) -> Step:
        name = f"trade-assistant {ctx['run_id']}"
        files = {"files": (ctx["filename"], ctx["markdown"].encode("utf-8"), "text/markdown")}
        data = self._req("POST", "/api/graph/ontology/generate", write=True, files=files,
                         data={"simulation_requirement": ctx["question"], "project_name": name, "additional_context": ctx["instructions"]})
        pid = data.get("project_id")
        if not pid:
            raise Failed("the ontology step returned no project id")
        return Step(self._go(state, "build", project_id=pid, project_name=name), next_in=0.0, stage="ontology", detail=f"project {pid}")

    def _ontology_reconcile(self, ctx: dict[str, Any], state: dict[str, Any]) -> Step:
        name = f"trade-assistant {ctx['run_id']}"
        items = self._req("GET", "/api/graph/project/list", write=False, params={"limit": 100}, timeout=30).get("items") or []
        hit = next((p for p in items if isinstance(p, dict) and p.get("name") == name), None)
        if hit and hit.get("status") in ("ontology_generated", "graph_building", "graph_completed"):
            return Step(self._go(state, "build", project_id=hit["project_id"], project_name=name), next_in=0.0, stage="ontology",
                        detail=f"found project {hit['project_id']} after the timeout")
        if hit and hit.get("status") == "failed":
            raise Failed(f"the ontology step failed on the server: {hit.get('error') or 'no reason given'}")
        tries = int(state.get("reconcile_tries") or 0) + 1
        if tries > 6:
            return Step(state, attention=("The ontology request timed out and its project was not found on the MiroFish server. Retry "
                                          "starts a new run (which may cost model calls); check the server's project list first."),
                        stage="ontology")
        return Step({**state, "reconcile_tries": tries}, next_in=30.0, stage="ontology", detail="looking for the project after a timeout")

    def _build(self, ctx: dict[str, Any], state: dict[str, Any]) -> Step:
        # Building again is safe: the server returns the running or finished build of the same project.
        try:
            data = self._req("POST", "/api/graph/build", write=True, json={"project_id": state["project_id"]}, timeout=60)
        except Ambiguous:
            return Step(self._go(state, "build"), next_in=15.0, stage="graph", detail="the build request timed out; asking again")
        return Step(self._go(state, "build_wait", task_id=data.get("task_id"), graph_id=data.get("graph_id")), next_in=self.poll_first,
                    stage="graph", detail="building the graph")

    def _build_wait(self, ctx: dict[str, Any], state: dict[str, Any]) -> Step:
        task = self._req("GET", f"/api/graph/task/{state['task_id']}", write=False, timeout=30) if state.get("task_id") else {"status": "completed"}
        st = task.get("status")
        if st == "failed":
            raise Failed(f"the graph build failed: {task.get('error') or task.get('message') or 'no reason given'}")
        if st != "completed":
            return self._poll(state, "graph", f"building the graph ({task.get('progress') or 0}%)")
        proj = self._req("GET", f"/api/graph/project/{state['project_id']}", write=False, timeout=30)
        gid = proj.get("graph_id") or (task.get("result") or {}).get("graph_id") or state.get("graph_id")
        return Step(self._go(state, "agents_check", graph_id=gid), next_in=0.0, stage="graph", detail="graph built")

    def _agents_check(self, ctx: dict[str, Any], state: dict[str, Any]) -> Step:
        """4.5.2: MiroFish makes one agent per filtered graph entity and has no cap of its own. The count is read (free)
        before any agent is prepared; over the profile's cap, the run waits for your approval instead."""
        n = None
        if state.get("graph_id"):
            data = self._req("GET", f"/api/simulation/entities/{state['graph_id']}", write=False, params={"enrich": "false"}, timeout=60)
            n = data.get("filtered_count")
            n = int(n) if isinstance(n, (int, float)) else (len(data.get("entities") or []) if isinstance(data.get("entities"), list) else None)
        if n is None and self.max_agents and not state.get("agents_unknown_approved"):
            return Step({**state, "agents": None}, approval={"code": "agents_unknown", "agents": None, "cap": self.max_agents,
                                                             "reason": "the server did not report how many agents its graph will make"},
                        stage="agents", detail="agent count not reported; waiting for your approval")
        if n is not None and self.max_agents and n > self.max_agents and int(state.get("agents_approved") or 0) < n:
            return Step({**state, "agents": n}, approval={"code": "agents_over_cap", "agents": n, "cap": self.max_agents,
                                                          "reason": f"the graph has {n} agents, more than the profile's {self.max_agents}"},
                        stage="agents", detail=f"{n} agents: more than {self.max_agents}; waiting for your approval")
        return Step(self._go(state, "sim_create", agents=n), next_in=0.0, stage="agents",
                    detail=f"{n} agents to prepare" if n is not None else "agent count not reported")

    def _sim_create(self, ctx: dict[str, Any], state: dict[str, Any]) -> Step:
        body = {"project_id": state["project_id"]}
        if state.get("graph_id"):
            body["graph_id"] = state["graph_id"]
        data = self._req("POST", "/api/simulation/create", write=True, json=body, timeout=60)
        sid = data.get("simulation_id")
        if not sid:
            raise Failed("the simulation was not created (no simulation id)")
        return Step(self._go(state, "prepare", simulation_id=sid), next_in=0.0, stage="agents", detail=f"simulation {sid}")

    def _sim_create_reconcile(self, ctx: dict[str, Any], state: dict[str, Any]) -> Step:
        items = self._req("GET", "/api/simulation/list", write=False, params={"project_id": state["project_id"]}, timeout=30).get("items") or []
        sims = [x for x in items if isinstance(x, dict) and x.get("simulation_id")]
        if len(sims) == 1:
            return Step(self._go(state, "prepare", simulation_id=sims[0]["simulation_id"]), next_in=0.0, stage="agents",
                        detail="found the simulation after a timeout")
        if len(sims) > 1:
            return Step(state, attention="The simulation request timed out and the project has several simulations; choose one on the server.",
                        stage="agents")
        tries = int(state.get("reconcile_tries") or 0) + 1
        if tries > 4:
            return Step(state, attention="The simulation request timed out and no simulation was found for the project.", stage="agents")
        return Step({**state, "reconcile_tries": tries}, next_in=20.0, stage="agents", detail="looking for the simulation after a timeout")

    def _prepare(self, ctx: dict[str, Any], state: dict[str, Any]) -> Step:
        # Preparing again is safe: the server detects a finished preparation and returns it.
        try:
            data = self._req("POST", "/api/simulation/prepare", write=True, json={"simulation_id": state["simulation_id"],
                                                                                   "parallel_profile_count": self.parallel_profiles}, timeout=60)
        except Ambiguous:
            return Step(self._go(state, "prepare_wait"), next_in=15.0, stage="agents", detail="the prepare request timed out; checking")
        if data.get("already_prepared") or data.get("status") == "ready":
            return Step(self._go(state, "start"), next_in=0.0, stage="agents", detail="agents ready")
        return Step(self._go(state, "prepare_wait", prepare_task_id=data.get("task_id")), next_in=self.poll_first, stage="agents",
                    detail="preparing the agents")

    def _prepare_wait(self, ctx: dict[str, Any], state: dict[str, Any]) -> Step:
        body = {"simulation_id": state["simulation_id"]}
        if state.get("prepare_task_id"):
            body["task_id"] = state["prepare_task_id"]
        data = self._req("POST", "/api/simulation/prepare/status", write=False, json=body, timeout=30)
        st = data.get("status")
        if data.get("already_prepared") or st in ("completed", "ready"):
            try:                                   # the agents actually prepared, for the usage record
                sim = self._req("GET", f"/api/simulation/{state['simulation_id']}", write=False, timeout=30)
                made = sim.get("profiles_count") or sim.get("entities_count")
            except (Transient, Failed):
                made = None
            return Step(self._go(state, "start", agents=made or state.get("agents")), next_in=0.0, stage="agents",
                        detail=f"{made} agents ready" if made else "agents ready")
        if st == "failed":
            raise Failed(f"preparing the agents failed: {data.get('error') or data.get('message') or 'no reason given'}")
        return self._poll(state, "agents", f"preparing the agents ({data.get('progress') or 0}%)")

    def _start(self, ctx: dict[str, Any], state: dict[str, Any]) -> Step:
        self._req("POST", "/api/simulation/start", write=True, timeout=60,
                  json={"simulation_id": state["simulation_id"], "max_rounds": self.max_rounds, "enable_graph_memory_update": False,
                        "platform": self.platform})
        return Step(self._go(state, "run_wait", started=True), next_in=self.poll_first, stage="simulation", detail="simulation started")

    def _start_reconcile(self, ctx: dict[str, Any], state: dict[str, Any]) -> Step:
        st = self._req("GET", f"/api/simulation/{state['simulation_id']}/run-status", write=False, timeout=30)
        if st.get("runner_status") in ("starting", "running", "completed", "stopped"):
            return Step(self._go(state, "run_wait", started=True), next_in=self.poll_first, stage="simulation", detail="it had started")
        tries = int(state.get("reconcile_tries") or 0) + 1
        if tries > 4:
            return Step(state, attention="The start request timed out and the simulation is not running.", stage="simulation")
        return Step({**state, "reconcile_tries": tries}, next_in=15.0, stage="simulation", detail="checking whether it started")

    def _run_wait(self, ctx: dict[str, Any], state: dict[str, Any]) -> Step:
        st = self._req("GET", f"/api/simulation/{state['simulation_id']}/run-status", write=False, timeout=30)
        rs = st.get("runner_status")
        if rs == "failed":
            raise Failed(f"the simulation failed: {st.get('error') or 'no reason given'}")
        if rs in ("completed", "stopped"):
            # 4.5.1: a simulation stopped on the server before its last round is not a finished one, and says so.
            cur, total = st.get("current_round"), st.get("total_rounds")
            early = rs == "stopped" or (isinstance(cur, int) and isinstance(total, int) and total > 0 and cur < total)
            words = (f"simulation stopped early, after {cur or '?'} of {total or '?'} rounds" if early
                     else f"simulation finished after {cur or '?'} rounds")
            return Step(self._go(state, "report", rounds=cur, total_rounds=total, stopped_early=early), next_in=0.0, stage="simulation",
                        detail=words)
        return self._poll({**state, "total_rounds": st.get("total_rounds"), "rounds": st.get("current_round")}, "simulation",
                          f"round {st.get('current_round') or 0} of {st.get('total_rounds') or '?'}")

    def _report(self, ctx: dict[str, Any], state: dict[str, Any]) -> Step:
        try:
            data = self._req("POST", "/api/report/generate", write=True, json={"simulation_id": state["simulation_id"]}, timeout=60)
        except Ambiguous:
            return Step(self._go(state, "report_wait"), next_in=15.0, stage="report", detail="the report request timed out; checking")
        return Step(self._go(state, "report_wait", report_task_id=data.get("task_id")), next_in=self.poll_first, stage="report",
                    detail="writing the report")

    def _report_wait(self, ctx: dict[str, Any], state: dict[str, Any]) -> Step:
        body = {"simulation_id": state["simulation_id"]}
        if state.get("report_task_id"):
            body["task_id"] = state["report_task_id"]
        data = self._req("POST", "/api/report/generate/status", write=False, json=body, timeout=30)
        st = data.get("status")
        if st == "failed":
            raise Failed(f"the report failed: {data.get('error') or data.get('message') or 'no reason given'}")
        if st != "completed":
            return self._poll(state, "report", f"writing the report ({data.get('progress') or 0}%)")
        rep = self._req("GET", f"/api/report/by-simulation/{state['simulation_id']}", write=False, timeout=60)
        text = rep.get("markdown_content") or ""
        if not text.strip():
            raise Failed("the report came back empty")
        return Step(self._go(state, "done", report_id=rep.get("report_id")), report=text, stage="report", detail="report received")

    def stop(self, state: dict[str, Any]) -> str:
        """Ask the server to stop a running simulation (cancel, or the run's time budget ran out)."""
        if not state.get("simulation_id") or not state.get("started"):
            return "nothing was running on the server"
        try:
            self._req("POST", "/api/simulation/stop", write=True, json={"simulation_id": state["simulation_id"]}, timeout=30)
            return "the server stopped the simulation"
        except (Transient, Ambiguous, Failed) as exc:
            return f"the stop request did not succeed: {exc}"


class SingleAI:
    """The single-prompt control: the same packet and template, one call to the AI set up in .env."""
    name = "ai"
    label = "Single AI call"

    def __init__(self, svc: Any):
        self.svc = svc

    def config(self) -> dict[str, Any]:
        ai = getattr(self.svc, "ai", None)
        return {"provider": self.name, "model": getattr(getattr(self.svc, "settings", None), "ai_model", None) if ai else None}

    def ready(self) -> dict[str, Any]:
        ai = getattr(self.svc, "ai", None)
        if ai is None or not ai.configured:
            return {"ok": False, "detail": "No AI is set up: ANTHROPIC_API_KEY in .env."}
        return {"ok": True, "detail": "The AI set up in .env answers in one call."}

    def advance(self, ctx: dict[str, Any], state: dict[str, Any]) -> Step:
        from ..ai import AIError, extract_json
        ai = getattr(self.svc, "ai", None)
        if ai is None or not ai.configured:
            return Step(state, failed="No AI is set up (ANTHROPIC_API_KEY).", stage="report")
        prompt = (f"{ctx['question']}\n\n{ctx['instructions']}\n\nAnswer with the JSON object only.\n\n--- EVIDENCE PACKET ---\n{ctx['markdown']}")
        try:
            reply = ai.ask(purpose="scenario_control", prompt=prompt, system="You map how investors could react to an event. You never "
                           "give prices, price targets or probabilities.", max_tokens=3000, web_search=False)
        except AIError as exc:
            return Step(state, failed=f"The AI call failed: {exc}", stage="report")
        parsed = extract_json(reply.get("text") or "")
        return Step({**state, "stage": "done"}, report=reply.get("text") or "", cases=parsed if isinstance(parsed, dict) else None,
                    stage="report", detail="one AI call", model=reply.get("model"), usage=reply.get("usage") or {})


class Demo:
    """Canned cases for trying the workflow on demo data: not a simulation, and labelled so everywhere."""
    name = "demo"
    label = "Demo provider (canned cases, not a simulation)"

    def config(self) -> dict[str, Any]:
        return {"provider": self.name}

    def ready(self) -> dict[str, Any]:
        return {"ok": True, "detail": "The demo provider answers at once with canned cases built from the packet."}

    def advance(self, ctx: dict[str, Any], state: dict[str, Any]) -> Step:
        seed = ctx["seed"]
        ids = [s["id"] for s in seed["sources"]]
        cite = ids[:1]
        cat = seed["catalyst"]["title"]

        def case(key: str, direction: str, trigger: str, reason: str, invalid: str, market: str | None) -> dict[str, Any]:
            drivers = [{"type": "issuer_event", "direction": direction, "timing": "first_session_after_release", "reason": reason,
                        "source_ids": cite}]
            if market:
                drivers.append({"type": "market", "direction": market, "timing": "first_session_after_release", "factor": "us_market",
                                "reason": "The broad market moves the same way and carries the stock with it.", "source_ids": cite})
            return {"key": key, "trigger": trigger, "stock_direction": direction, "drivers": drivers,
                    "priced_in": "Part of the reaction may already be in the price.", "invalidation": invalid,
                    "missing_evidence": [], "hypotheses": []}
        cases = {"schema_version": 1, "seed_id": seed["seed_id"], "symbol": seed["symbol"], "horizon_sessions": seed["horizon_sessions"],
                 "cases": [case("central", "flat", f"{cat} lands close to expectations", "Little new information.",
                                "A clear surprise in either direction", None),
                           case("bullish", "up", f"{cat} beats expectations", "Investors re-rate the outlook.",
                                "The beat comes with a weaker outlook", "up"),
                           case("bearish", "down", f"{cat} disappoints", "Investors focus on the weaker outlook.",
                                "The outlook is maintained or raised", "down")]}
        report = ("Demo provider: canned cases built from the packet for trying the one-click workflow. Nothing was simulated.\n\n"
                  "```json\n" + json.dumps(cases, indent=1) + "\n```")
        return Step({**state, "stage": "done", "at": time.time()}, report=report, cases=cases, stage="report", detail="canned cases")


def provider_for(svc: Any, name: str | None = None, session: Any = None, profile: str | None = None) -> Any:
    s = svc.settings
    name = name or s.scenario_provider
    if name == "mirofish":
        from .simulation import profile as profile_of
        p = profile_of(s, profile or getattr(s, "scenario_default_profile", "quick"))
        return MiroFish(s.mirofish_url, session=session, timeout=s.mirofish_http_timeout, max_rounds=p["rounds"], max_agents=p["agents"],
                        platform=p["platform"], parallel_profiles=p["parallel_profiles"], profile=p["key"])
    if name == "ai":
        return SingleAI(svc)
    if name == "demo":
        return Demo()
    raise ValueError(f"unknown scenario provider {name!r}")


__all__ = ["MiroFish", "SingleAI", "Demo", "Step", "provider_for", "PINNED_UPSTREAM", "Transient", "Ambiguous", "Failed"]
