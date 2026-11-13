"""Simulation governance for the scenario provider (4.5.2): profiles, the cost estimate and approval, spending limits,
the usage ledger, the circuit breaker and layered readiness.

**What the pinned MiroFish (7657031) can and cannot be told.** It takes a round cap (`max_rounds`), the platforms to
simulate (`reddit`, `twitter` or `parallel`, both), and how many agent profiles to write at once
(`parallel_profile_count`). It has **no agent-count parameter**: its agents are the graph's filtered entities, one
each, decided by the graph it builds from the packet. And it **reports no token usage or cost**. So:

- the agent cap is enforced **before any agent is prepared**: once the graph is built, the app reads how many entities
  it has (`GET /api/simulation/entities/{graph_id}`, a read) and, if there are more than the profile allows, the run
  waits for your approval instead of preparing them (the graph build itself has already run);
- a run's cost is an **estimate** (a range, with its basis) unless you enter what your model provider charged, or the
  call is one of the app's own AI calls (the single-prompt control, the report converter), whose tokens are measured
  and priced at SCENARIO_AI_*_USD_PER_MTOK when you set them. The estimate is never a cap: the app can only refuse to
  **start** a run (admission), stop one that runs past its time cap, and stop at the agent check.

**Profiles.** Quick (the default) is the smallest useful run; Standard is MIROFISH_MAX_ROUNDS and MIROFISH_MAX_AGENTS;
Deep is three times both and always asks first. A deeper run is never chosen for you because a setup looks
attractive: whether depth pays is measured in the forward scorecard (4.5.3).

**Spending.** A run is admitted automatically when MiroFish is ready, its estimate's upper end is within
MIROFISH_AUTO_APPROVAL_USD, and today's and this month's spending (measured, entered, or else estimated, by the
stages each run reached) plus that upper end stays within MIROFISH_DAILY_BUDGET_USD and MIROFISH_MONTHLY_BUDGET_USD.
Otherwise it waits for your approval (an unknown cost, with MIROFISH_REQUIRE_APPROVAL_ON_UNKNOWN_COST=1; an estimate
over the automatic limit; Deep; a larger agent count), or is refused (over a budget). Approval is for simulation
spending only; it has nothing to do with trading.

**The circuit breaker.** After MIROFISH_CIRCUIT_FAILURES consecutive provider failures (the server unreachable or
failing; not a cancel, and not a report whose cases were unusable), the circuit opens for
MIROFISH_CIRCUIT_COOLDOWN_SECONDS: new runs are not sent, Research goes on without scenarios and says why. After the
cooldown the next run first makes one free readiness probe (half open); it passes and the circuit closes, or it fails
and the circuit opens again. It can be reset by hand. The demo provider is never substituted.
"""

from __future__ import annotations

import json
import logging
import statistics
from datetime import datetime, timedelta, timezone
from typing import Any

from .. import contracts as C
from ..optionmath import NY

log = logging.getLogger(__name__)

PROFILE_KEYS = ("quick", "standard", "deep")
PROFILE_LABELS = {"quick": "Quick", "standard": "Standard", "deep": "Deep"}
FIXED_SHARE = 0.3            # the part of a run's cost that does not grow with agents x rounds: ontology, graph, report
RANGE = (0.5, 2.0)           # a scaled estimate's range around its middle
CIRCUIT_KEY = "scenario:circuit:mirofish"
SMOKE_KEY = "scenario:smoke:mirofish"
# The share of a run's cost spent by the time it reaches each MiroFish stage (an estimate's own shape, for failed and
# interrupted runs): the ontology and graph are a small part, preparing the agents more, the simulation most.
STAGE_SHARE = {"ontology": 0.0, "ontology_reconcile": 0.05, "build": 0.05, "build_wait": 0.10, "agents_check": 0.15, "sim_create": 0.15,
               "sim_create_reconcile": 0.15, "prepare": 0.15, "prepare_wait": 0.30, "start": 0.40, "start_reconcile": 0.40, "run_wait": 0.85,
               "report": 0.90, "report_wait": 0.95, "done": 1.0}
STATE_WORDS = {"ready": "Ready", "degraded": "Degraded", "unavailable": "Unavailable", "config_error": "Configuration error",
               "circuit_open": "Circuit open", "budget_exceeded": "Budget exceeded"}


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _ts(s: Any) -> datetime | None:
    if not s:
        return None
    try:
        d = datetime.fromisoformat(str(s).replace("Z", "+00:00"))
    except ValueError:
        return None
    return d if d.tzinfo else d.replace(tzinfo=timezone.utc)


def day_month(at: datetime | None = None) -> tuple[str, str]:
    d = (at or _now()).astimezone(NY).date()
    return d.isoformat(), d.isoformat()[:7]


def money(x: float | None) -> str:
    return "unknown" if x is None else f"${x:,.2f}"


# --- profiles -------------------------------------------------------------------------------------------------------

def profile(settings: Any, key: str | None) -> dict[str, Any]:
    key = key if key in PROFILE_KEYS else "quick"
    s = settings
    rounds, agents, minutes = int(s.mirofish_max_rounds), int(s.mirofish_max_agents), float(s.mirofish_run_minutes)
    if key == "quick":
        p = {"rounds": max(1, min(4, rounds)), "agents": max(2, min(20, agents // 2 or 2)), "minutes": max(2.0, min(20.0, minutes)),
             "platform": "reddit", "parallel_profiles": 3, "always_ask": False}
    elif key == "standard":
        p = {"rounds": rounds, "agents": agents, "minutes": minutes, "platform": "parallel", "parallel_profiles": 5, "always_ask": False}
    else:
        p = {"rounds": min(200, 3 * rounds), "agents": min(500, 3 * agents), "minutes": min(1440.0, 2 * minutes), "platform": "parallel",
             "parallel_profiles": 5, "always_ask": True}
    p.update(key=key, label=PROFILE_LABELS[key], load=p["agents"] * p["rounds"] * (2 if p["platform"] == "parallel" else 1))
    p["words"] = (f"up to {p['agents']} agents, {p['rounds']} rounds on {'both platforms' if p['platform'] == 'parallel' else 'one platform'}, "
                  f"{p['minutes']:g} minutes")
    return p


def profiles(settings: Any) -> list[dict[str, Any]]:
    return [profile(settings, k) for k in PROFILE_KEYS]


# --- the ledger ------------------------------------------------------------------------------------------------------

def _f(x: Any) -> float | None:
    try:
        return None if x is None else float(x)
    except (TypeError, ValueError):
        return None


class Ledger:
    def __init__(self, svc: Any):
        self.svc = svc

    def row(self, run_id: str) -> dict[str, Any] | None:
        r = self.svc.db.one("SELECT * FROM scenario_usage WHERE run_id = ?", (run_id,))
        return dict(r) if r else None

    def open(self, run: Any, role: str, prof: str | None, est: dict[str, Any] | None, approval: dict[str, Any] | None = None) -> None:
        """The run's row, written when it is created: the estimate made before anything was sent."""
        day, month = day_month()
        now = C.utc_now()
        with self.svc.db.tx() as cur:
            cur.execute("""INSERT INTO scenario_usage (run_id, study_id, provider, role, profile, created_at, status, day, month, est_low_usd, est_high_usd,
                           est_basis, cost_confidence, approval_json, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                           ON CONFLICT(run_id) DO UPDATE SET profile = excluded.profile, est_low_usd = excluded.est_low_usd,
                           est_high_usd = excluded.est_high_usd, est_basis = excluded.est_basis, approval_json = excluded.approval_json,
                           cost_confidence = CASE WHEN scenario_usage.measured_usd IS NULL THEN excluded.cost_confidence ELSE scenario_usage.cost_confidence END,
                           updated_at = excluded.updated_at""",
                        (run["id"], run["study_id"], run["provider"], role, prof, now, run["status"], day, month,
                         (est or {}).get("low"), (est or {}).get("high"), (est or {}).get("basis"),
                         "estimate" if (est or {}).get("known") else "unknown", json.dumps(approval or {}), now))

    def sync(self, run_id: str) -> None:
        """Bring the run's row up to date from the run itself: its ids, agents, rounds, times, status, tokens."""
        r = self.svc.db.one("SELECT * FROM scenario_runs WHERE id = ?", (run_id,))
        if r is None:
            return
        ext = json.loads(r["external_json"] or "{}")
        st = ext.get("state") or {}
        usage = ext.get("usage") or {}
        if self.row(run_id) is None:
            self.open(r, ext.get("role") or "provider", ext.get("profile"), ext.get("estimate"), ext.get("approval"))
        inp, out = usage.get("input_tokens"), usage.get("output_tokens")
        measured = None
        s = self.svc.settings
        if r["provider"] == "ai" and inp is not None and s.scenario_ai_input_usd_per_mtok is not None and s.scenario_ai_output_usd_per_mtok is not None:
            measured = (float(inp) * s.scenario_ai_input_usd_per_mtok + float(out or 0) * s.scenario_ai_output_usd_per_mtok) / 1e6
        with self.svc.db.tx() as cur:
            cur.execute("""UPDATE scenario_usage SET status = ?, model = COALESCE(?, model), project_id = COALESCE(?, project_id),
                           simulation_id = COALESCE(?, simulation_id), agents = COALESCE(?, agents), rounds = COALESCE(?, rounds),
                           total_rounds = COALESCE(?, total_rounds), started_at = COALESCE(?, started_at), ended_at = COALESCE(?, ended_at),
                           stage_reached = COALESCE(?, stage_reached), input_tokens = COALESCE(?, input_tokens), output_tokens = COALESCE(?, output_tokens),
                           retries = ?, stopped_early = ?,
                           measured_usd = CASE WHEN cost_confidence = 'user_entered' THEN measured_usd ELSE COALESCE(?, measured_usd) END,
                           cost_confidence = CASE WHEN cost_confidence = 'user_entered' THEN cost_confidence WHEN ? IS NOT NULL THEN 'measured'
                                                  ELSE cost_confidence END,
                           updated_at = ? WHERE run_id = ?""",
                        (r["status"], ext.get("model"), st.get("project_id"), st.get("simulation_id"), st.get("agents"), st.get("rounds"),
                         st.get("total_rounds"), ext.get("started_at"), r["completed_at"], st.get("stage") or ext.get("stage"), inp, out,
                         int(st.get("transient") or 0) + int(ext.get("finish_tries") or 0), int(bool(st.get("stopped_early"))), measured, measured,
                         C.utc_now(), run_id))

    def enter_actual(self, run_id: str, usd: float, note: str = "") -> dict[str, Any]:
        """What your model provider actually charged for this run: it replaces the estimate and calibrates the next."""
        from ..service import RequestError
        if self.row(run_id) is None:
            self.sync(run_id)
        if self.row(run_id) is None:
            raise RequestError(f"No scenario run {run_id}")
        with self.svc.db.tx() as cur:
            cur.execute("UPDATE scenario_usage SET measured_usd = ?, cost_confidence = 'user_entered', note = ?, updated_at = ? WHERE run_id = ?",
                        (float(usd), note.strip()[:300] or None, C.utc_now(), run_id))
        return self.view(self.row(run_id) or {})

    @staticmethod
    def incurred(row: dict[str, Any]) -> tuple[float | None, float | None, str]:
        """(low, high, confidence) of what a run has cost or may have cost: its measured or entered cost, else its
        estimate scaled to the stages it reached; (None, None, 'unknown') when nothing is known."""
        m = _f(row.get("measured_usd"))
        if m is not None:
            return m, m, row.get("cost_confidence") or "measured"
        lo, hi = _f(row.get("est_low_usd")), _f(row.get("est_high_usd"))
        if lo is None or hi is None:
            return None, None, "unknown"
        share = 1.0 if row.get("status") == "complete" else STAGE_SHARE.get(str(row.get("stage_reached") or ""), 0.0)
        if not row.get("started_at"):                   # nothing was sent: nothing was spent
            share = 0.0
        return lo * share, hi * share, "estimate"

    def spent(self, period: str, key: str, provider: str = "mirofish") -> dict[str, Any]:
        col = "day" if period == "day" else "month"
        rows = [dict(r) for r in self.svc.db.query(f"SELECT * FROM scenario_usage WHERE provider = ? AND {col} = ?", (provider, key))]
        lo = hi = 0.0
        measured = 0.0
        unknown = 0
        for r in rows:
            a, b, conf = self.incurred(r)
            if a is None:
                if r.get("started_at"):                 # it sent something, at a cost nobody knows
                    unknown += 1
                continue
            lo += a
            hi += b
            if conf in ("measured", "user_entered"):
                measured += a
        return {"runs": len(rows), "low_usd": lo, "high_usd": hi, "measured_usd": measured, "unknown_runs": unknown}

    @staticmethod
    def view(r: dict[str, Any]) -> dict[str, Any]:
        lo, hi, conf = Ledger.incurred(r)
        return {**{k: r.get(k) for k in ("run_id", "study_id", "provider", "role", "profile", "model", "project_id", "simulation_id", "agents",
                                         "rounds", "total_rounds", "created_at", "started_at", "ended_at", "status", "stage_reached",
                                         "est_low_usd", "est_high_usd", "est_basis", "measured_usd", "cost_confidence", "input_tokens",
                                         "output_tokens", "retries", "note")},
                "stopped_early": bool(r.get("stopped_early")), "approval": json.loads(r.get("approval_json") or "{}"),
                "incurred_low_usd": lo, "incurred_high_usd": hi, "incurred_confidence": conf,
                "words": (f"{money(lo)}" if lo is not None and lo == hi else f"{money(lo)} to {money(hi)} ({conf})" if lo is not None
                          else "unknown (MiroFish reports no usage; enter what your provider charged)")}

    def report(self) -> dict[str, Any]:
        day, month = day_month()
        rows = [dict(r) for r in self.svc.db.query("SELECT * FROM scenario_usage ORDER BY created_at DESC LIMIT 500")]
        mf = [r for r in rows if r["provider"] == "mirofish"]
        done = [r for r in mf if r["status"] == "complete"]
        costs = [Ledger.incurred(r) for r in done]
        known = [(a + b) / 2 for a, b, _ in costs if a is not None]
        lat = []
        for r in done:
            a, b = _ts(r.get("started_at")), _ts(r.get("ended_at"))
            if a and b:
                lat.append((b - a).total_seconds())
        ended = [r for r in mf if r["status"] in ("complete", "failed", "needs_attention")]
        return {"today": self.spent("day", day), "month": self.spent("month", month),
                "ai_today": self.spent("day", day, provider="ai"),
                "average_successful_usd": statistics.fmean(known) if known else None, "average_basis": f"{len(known)} of {len(done)} completed runs",
                "average_latency_seconds": statistics.fmean(lat) if lat else None,
                "failure_rate": (sum(1 for r in ended if r["status"] != "complete") / len(ended)) if ended else None,
                "runs": [self.view(r) for r in rows[:20]],
                "note": ("MiroFish reports no token usage: its runs' costs are estimates (a range, by the stages each reached) unless you enter "
                         "what your model provider charged. An unknown cost is shown as unknown, never as $0.00.")}


# --- the estimate and admission -----------------------------------------------------------------------------------------

def estimate(svc: Any, key: str) -> dict[str, Any]:
    s = svc.settings
    p = profile(s, key)
    hist = [float(r["measured_usd"]) for r in svc.db.query(
        "SELECT measured_usd FROM scenario_usage WHERE provider = 'mirofish' AND profile = ? AND measured_usd IS NOT NULL AND status = 'complete' "
        "ORDER BY created_at DESC LIMIT 20", (p["key"],))]
    if hist:
        mid = statistics.median(hist)
        return {"known": True, "low": min(hist) * 0.8, "high": max(hist) * 1.25, "mid": mid, "profile": p["key"],
                "basis": f"{len(hist)} recorded run{'s' if len(hist) != 1 else ''} of the {p['label']} profile",
                "words": f"{money(min(hist) * 0.8)} to {money(max(hist) * 1.25)}, from {len(hist)} recorded run{'s' if len(hist) != 1 else ''}"}
    base = s.mirofish_cost_per_run_usd
    if base is None:
        return {"known": False, "low": None, "high": None, "mid": None, "profile": p["key"],
                "basis": "unknown: MIROFISH_COST_PER_RUN_USD is not set and no run's actual cost has been entered",
                "words": "unknown (set MIROFISH_COST_PER_RUN_USD, or enter a finished run's actual cost)"}
    std = profile(s, "standard")
    mid = float(base) * (FIXED_SHARE + (1 - FIXED_SHARE) * p["load"] / max(1, std["load"]))
    lo, hi = mid * RANGE[0], mid * RANGE[1]
    return {"known": True, "low": lo, "high": hi, "mid": mid, "profile": p["key"],
            "basis": "MIROFISH_COST_PER_RUN_USD for a Standard run, scaled by agents x rounds x platforms (a rough range, not a measurement)",
            "words": f"{money(lo)} to {money(hi)} (rough: from MIROFISH_COST_PER_RUN_USD)"}


def budgets(svc: Any) -> dict[str, Any]:
    s = svc.settings
    led = Ledger(svc)
    day, month = day_month()
    today, mon = led.spent("day", day), led.spent("month", month)

    def left(limit: float, spent: dict[str, Any]) -> float | None:
        return None if not limit else max(0.0, limit - spent["high_usd"])
    return {"daily_limit": s.mirofish_daily_budget_usd or None, "monthly_limit": s.mirofish_monthly_budget_usd or None,
            "today": today, "month": mon, "daily_left": left(s.mirofish_daily_budget_usd, today),
            "monthly_left": left(s.mirofish_monthly_budget_usd, mon), "auto_approval_usd": s.mirofish_auto_approval_usd,
            "require_approval_on_unknown": bool(s.mirofish_require_approval_on_unknown_cost)}


def admit(svc: Any, key: str, *, agents: int | None = None) -> dict[str, Any]:
    """Whether a MiroFish run of this profile may start now: `auto`, `approval` (and why) or `refused` (and why)."""
    s = svc.settings
    p = profile(s, key)
    est = estimate(svc, p["key"])
    b = budgets(svc)
    reasons: list[str] = []
    hi = est["high"]
    for name, limit, spent in (("daily", b["daily_limit"], b["today"]), ("monthly", b["monthly_limit"], b["month"])):
        if not limit:
            continue
        if spent["high_usd"] >= limit - 1e-9:
            return {"decision": "refused", "code": "budget_exceeded", "estimate": est, "budgets": b, "profile": p,
                    "reason": f"the {name} simulation budget ({money(limit)}) is used up ({money(spent['high_usd'])} at most spent): raise "
                              f"MIROFISH_{name.upper()}_BUDGET_USD or wait"}
        if hi is not None and spent["high_usd"] + hi > limit + 1e-9:
            return {"decision": "refused", "code": "budget_exceeded", "estimate": est, "budgets": b, "profile": p,
                    "reason": f"its estimate (up to {money(hi)}) would take the {name} spending past MIROFISH_{name.upper()}_BUDGET_USD "
                              f"({money(spent['high_usd'])} of {money(limit)} at most spent)"}
        if spent["unknown_runs"]:
            reasons.append(f"{spent['unknown_runs']} run{'s' if spent['unknown_runs'] != 1 else ''} of unknown cost this "
                           f"{'day' if name == 'daily' else 'month'}: the {name} budget cannot be checked exactly")
    if p["always_ask"]:
        reasons.append("the Deep profile always asks first")
    if not est["known"] and s.mirofish_require_approval_on_unknown_cost:
        reasons.append("its cost is unknown (MIROFISH_REQUIRE_APPROVAL_ON_UNKNOWN_COST=1)")
    if hi is not None and hi > s.mirofish_auto_approval_usd + 1e-9:
        reasons.append(f"its estimate (up to {money(hi)}) is over MIROFISH_AUTO_APPROVAL_USD ({money(s.mirofish_auto_approval_usd)})")
    if agents is not None and agents > p["agents"]:
        reasons.append(f"its graph has {agents} agents, more than the {p['label']} profile's {p['agents']}")
    out = {"estimate": est, "budgets": b, "profile": p}
    if reasons:
        return {**out, "decision": "approval", "code": "approval_required", "reason": "; ".join(reasons)}
    return {**out, "decision": "auto", "code": "auto", "reason": "within the automatic approval limit and the budgets"}


# --- the circuit breaker ---------------------------------------------------------------------------------------------

class Circuit:
    def __init__(self, svc: Any):
        self.svc = svc

    def _get(self) -> dict[str, Any]:
        return self.svc.db.cache_get(CIRCUIT_KEY) or {"state": "closed", "failures": 0}

    def _put(self, st: dict[str, Any]) -> None:
        self.svc.db.cache_put(CIRCUIT_KEY, st)

    def state(self) -> dict[str, Any]:
        st = dict(self._get())
        s = self.svc.settings
        if st.get("state") == "open":
            until = _ts(st.get("opened_at"))
            until = until + timedelta(seconds=s.mirofish_circuit_cooldown_seconds) if until else None
            st["until"] = until.isoformat() if until else None
            if until and _now() >= until:
                st["state"] = "half_open"
        st["threshold"] = s.mirofish_circuit_failures
        st["cooldown_seconds"] = s.mirofish_circuit_cooldown_seconds
        return st

    def failure(self, error: str) -> dict[str, Any]:
        st = self.state()
        n = int(st.get("failures") or 0) + 1
        opened = st.get("state") == "half_open" or n >= self.svc.settings.mirofish_circuit_failures
        new = {"state": "open" if opened else "closed", "failures": n, "last_error": error[:300], "last_failure_at": C.utc_now(),
               "opened_at": C.utc_now() if opened else st.get("opened_at")}
        self._put(new)
        if opened and st.get("state") != "open":
            self.svc.bus.publish("scenario.circuit", None, state="open", failures=n)
        return self.state()

    def success(self) -> None:
        st = self._get()
        if st.get("failures") or st.get("state") != "closed":
            self._put({"state": "closed", "failures": 0, "closed_at": C.utc_now(), "last_error": st.get("last_error")})
            self.svc.bus.publish("scenario.circuit", None, state="closed")

    def reset(self) -> dict[str, Any]:
        self._put({"state": "closed", "failures": 0, "closed_at": C.utc_now(), "reset_by": "you"})
        return self.state()


# --- readiness ----------------------------------------------------------------------------------------------------------

def readiness(svc: Any, provider: Any, name: str, prof_key: str) -> dict[str, Any]:
    """Level A (connection) and B (dependencies) for MiroFish, both free; the circuit, the budgets and whether a real
    functional test (level C) has passed. A state, its reason and what to do."""
    if name != "mirofish":
        r = provider.ready()
        return {**r, "state": "ready" if r.get("ok") else "unavailable", "state_label": STATE_WORDS["ready" if r.get("ok") else "unavailable"],
                "levels": {}, "action": None if r.get("ok") else r.get("detail")}
    lv = provider.levels() if hasattr(provider, "levels") else {"A": provider.ready()}
    a, b = lv.get("A") or {}, lv.get("B") or {}
    circ = Circuit(svc).state()
    adm = admit(svc, prof_key)
    smoke = svc.db.cache_get(SMOKE_KEY)
    if a.get("config_error"):
        state, action = "config_error", a.get("action") or a.get("detail")
    elif not a.get("ok"):
        state, action = "unavailable", "Start MiroFish (./setup_mirofish.sh start), then check again."
    elif circ["state"] == "open":
        state, action = "circuit_open", (f"After {circ.get('failures')} failures in a row new runs are paused until {circ.get('until')}; "
                                         "reset the circuit once MiroFish is fixed.")
    elif adm["decision"] == "refused":
        state, action = "budget_exceeded", adm["reason"]
    elif b and not b.get("ok"):
        state, action = "degraded", b.get("detail")
    else:
        state, action = "ready", None
    ok = state in ("ready", "degraded")
    detail = {"ready": a.get("detail"), "degraded": b.get("detail"), "unavailable": a.get("detail"), "config_error": a.get("detail"),
              "circuit_open": f"The circuit is open: {circ.get('last_error')}", "budget_exceeded": adm["reason"]}[state]
    return {"ok": ok, "state": state, "state_label": STATE_WORDS[state], "detail": detail, "action": action, "levels": lv,
            "circuit": circ, "admission": {k: adm[k] for k in ("decision", "code", "reason")}, "estimate": adm["estimate"],
            "budgets": adm["budgets"], "profile": adm["profile"], "checked_at": C.utc_now(),
            "functional": smoke or {"ok": None, "words": "No real end-to-end run has been checked yet (python run.py --scenario-smoke SPY)."}}


__all__ = ["profile", "profiles", "estimate", "admit", "budgets", "Ledger", "Circuit", "readiness", "PROFILE_KEYS", "SMOKE_KEY", "STATE_WORDS",
           "money", "day_month"]
