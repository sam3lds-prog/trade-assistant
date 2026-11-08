"""The 3.0 domain contracts: one object per decision, from opportunity to reviewed trade (Trade Assistant 3.0).

Every analysis and decision has a durable identity and a traceable parent:

    Opportunity (a candidate)                         opportunity_candidates
      └── StudyRun                                    studies
            ├── EvidenceSnapshot                      studies.evidence_json
            ├── ForecastVersion                       studies.forecast_json, forecast_records
            └── StrategyCandidate                     studies.strategies_json
                   └── TradeProposal                  trade_proposals, proposal_events
                          ├── RiskAssessment          trade_proposals.risk_json
                          ├── Approval / policy       approvals, trading_policies
                          └── BrokerOrder             broker_orders
                                 ├── Fill             broker_fills
                                 └── review           (Portfolio, forecast_records)

The models are versioned (CONTRACT_VERSION) and strict: unknown fields are refused, numbers must be finite, and
the proposal's exact order terms are hashed (order_hash) so an approval authorises those terms and nothing else.
The scanners, the research study, the strategy panel and the Trading Desk all hand these objects to each other,
so the trade idea never has to be rebuilt by hand.
"""

from __future__ import annotations

import hashlib
import json
import secrets
import time
from datetime import datetime, timezone
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

CONTRACT_VERSION = "3.0"

Direction = Literal["LONG", "SHORT"]
Horizon = Literal["intraday", "swing", "event"]
OpportunityKind = Literal["reversal", "breakout", "earnings", "news", "pullback", "gap", "momentum", "fade", "other"]
Source = Literal["morning", "ideas", "scanner", "reversal", "live", "earnings", "manual", "study"]
Thesis = Literal["intraday_bounce", "swing", "earnings_reaction", "reversal", "investment"]
AccountKind = Literal["paper", "live", "shadow"]
Instrument = Literal["equity", "option"]
Side = Literal["buy", "sell", "sell_short", "buy_to_cover", "buy_to_open", "sell_to_close", "sell_to_open", "buy_to_close"]
OrderType = Literal["limit", "market", "stop", "stop_limit"]
TimeInForce = Literal["day", "gtc"]
OperatingMode = Literal["disabled", "paper", "approval_required", "capped_autopilot"]

PROPOSAL_STATES = ("draft", "awaiting_approval", "authorized", "submitting", "submitted", "partially_filled", "filled",
                   "cancel_requested", "cancelled", "rejected", "expired", "blocked", "unknown", "failed", "closed")
TERMINAL_STATES = frozenset({"filled", "cancelled", "rejected", "expired", "failed", "closed"})
# The only moves a proposal can make. `unknown` is the state after a submission whose outcome was not seen (a
# timeout, a crash): only reconciliation with the broker leaves it, never a retry.
TRANSITIONS: dict[str, frozenset[str]] = {
    "draft": frozenset({"awaiting_approval", "authorized", "blocked", "expired", "cancelled"}),
    "awaiting_approval": frozenset({"authorized", "blocked", "expired", "cancelled"}),
    "blocked": frozenset({"awaiting_approval", "authorized", "expired", "cancelled"}),
    "authorized": frozenset({"submitting", "blocked", "expired", "cancelled"}),
    "submitting": frozenset({"submitted", "unknown", "rejected", "failed", "filled", "partially_filled"}),
    "unknown": frozenset({"submitted", "partially_filled", "filled", "rejected", "cancelled", "failed"}),
    "submitted": frozenset({"partially_filled", "filled", "cancel_requested", "cancelled", "rejected", "expired", "unknown"}),
    "partially_filled": frozenset({"filled", "cancel_requested", "cancelled", "expired", "unknown"}),
    "cancel_requested": frozenset({"cancelled", "filled", "partially_filled", "unknown"}),
    "filled": frozenset({"closed"}),
    "cancelled": frozenset(), "rejected": frozenset(), "expired": frozenset(), "failed": frozenset(), "closed": frozenset(),
}
ORDER_STATES = ("intent", "submitting", "submitted", "unknown", "accepted", "partially_filled", "filled", "cancel_requested",
                "cancelled", "rejected", "expired", "failed")
OPEN_ORDER_STATES = frozenset({"intent", "submitting", "submitted", "unknown", "accepted", "partially_filled", "cancel_requested"})


def new_id(prefix: str) -> str:
    """A time-ordered id that two processes cannot both make: milliseconds then 40 random bits."""
    return f"{prefix}_{int(time.time() * 1000):012x}{secrets.token_hex(5)}"


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def canonical(obj: Any) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), default=str)


def digest(obj: Any) -> str:
    return hashlib.sha256(canonical(obj).encode()).hexdigest()


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False, validate_assignment=True)


class Provenance(Strict):
    """Where a figure came from: the data, the engines and the moment, so a result can be reproduced."""
    data_source: str
    as_of: str
    engines: dict[str, str] = Field(default_factory=dict)
    inputs_hash: str | None = None
    notes: list[str] = Field(default_factory=list)


class RankComponent(Strict):
    """One visible part of an opportunity's rank: what it measures, its normalised value (0-1), its weight."""
    key: Literal["historical_support", "setup_quality", "catalyst", "liquidity", "execution_cost", "freshness", "conflict"]
    label: str
    value: float = Field(ge=0.0, le=1.0)
    weight: float = Field(ge=-1.0, le=1.0)
    text: str = ""


class Edge(Strict):
    """The historical after-cost record behind a trade, with its uncertainty. known=False means none."""
    known: bool
    expected_pct: float
    low_pct: float | None = None
    high_pct: float | None = None
    cases: int = 0
    independent_cases: int | None = None
    validated: bool = False            # out-of-sample, after costs; the autopilot needs it
    basis: str = ""


class Observation(Strict):
    """What one scanner saw: a source-specific signal that feeds a shared candidate."""
    source: Source
    source_key: str
    symbol: str
    direction: Direction
    horizon: Horizon
    kind: OpportunityKind
    observed_at: str
    raw_score: float | None = None
    entry: float | None = Field(default=None, gt=0)
    invalidation: float | None = Field(default=None, gt=0)
    target: float | None = Field(default=None, gt=0)
    catalyst: str | None = None
    edge: Edge | None = None
    payload: dict[str, Any] = Field(default_factory=dict)


class Opportunity(Strict):
    """One candidate trade: a symbol, a direction and a horizon, with every observation behind it merged."""
    contract_version: str = CONTRACT_VERSION
    id: str
    symbol: str
    direction: Direction
    horizon: Horizon
    kinds: list[OpportunityKind]
    status: Literal["active", "expired", "retired"] = "active"
    first_seen_at: str
    updated_at: str
    expires_at: str
    rank_score: float
    components: list[RankComponent]
    sources: list[Source]
    catalyst: str | None = None
    next_event: dict[str, Any] | None = None
    entry: float | None = None
    invalidation: float | None = None
    target: float | None = None
    edge: Edge
    liquidity: dict[str, Any] = Field(default_factory=dict)
    data_status: Literal["reliable", "partial", "stale", "unknown"] = "unknown"
    eligible_strategies: list[str] = Field(default_factory=list)
    conflicts: list[str] = Field(default_factory=list)
    autopilot_eligible: bool = False
    autopilot_reasons: list[str] = Field(default_factory=list)
    study_id: str | None = None
    version: int = 1


class EvidenceItem(Strict):
    key: str
    label: str
    kind: Literal["history", "analog", "peer", "factor", "catalyst", "news", "options", "regime", "technical", "ai"]
    summary: str
    as_of: str | None = None
    source: str | None = None
    cases: int | None = None
    supports: Literal["for", "against", "neutral"] = "neutral"
    data: dict[str, Any] = Field(default_factory=dict)


class EvidenceSnapshot(Strict):
    """What the study knew when it ran. Saved, so a trade is judged later on what was believed at the time."""
    as_of: str
    items: list[EvidenceItem]
    missing: list[str] = Field(default_factory=list)
    provenance: Provenance


class ScenarioPath(Strict):
    key: str                            # base, bull, bear, or a named shock
    label: str
    assumption: str                     # what has to be true for this path
    path_pct: list[float]
    low_pct: list[float] | None = None
    high_pct: list[float] | None = None
    probability: float | None = Field(default=None, ge=0, le=1)  # only when estimated and validated


class Calibration(Strict):
    status: Literal["calibrated", "in_sample_only", "not_calibrated", "insufficient_data"]
    horizon_days: int
    cases: int = 0
    interval: float = 0.8
    coverage: float | None = None       # the share of realised outcomes inside the interval
    directional_accuracy: float | None = None
    mae_pct: float | None = None
    baseline_mae_pct: float | None = None
    by_regime: dict[str, dict[str, Any]] = Field(default_factory=dict)
    text: str = ""


class ForecastVersion(Strict):
    model_version: str
    horizon_days: int = Field(gt=0)
    base: ScenarioPath
    alternatives: list[ScenarioPath] = Field(default_factory=list)
    components: dict[str, dict[str, Any]] = Field(default_factory=dict)   # each engine's own reading and weight
    weights: dict[str, float] = Field(default_factory=dict)
    calibration: Calibration
    explanation: list[str] = Field(default_factory=list)
    invalidators: list[str] = Field(default_factory=list)


class StrategyCandidate(Strict):
    key: str
    label: str
    instrument: Literal["shares", "short", "option", "multi_leg"]
    legs: list[dict[str, Any]] = Field(default_factory=list)
    cost: float
    credit: bool = False
    expected_pnl: dict[str, float] = Field(default_factory=dict)   # by scenario key
    pnl_p10: float | None = None
    pnl_p50: float | None = None
    pnl_p90: float | None = None
    p_profit: float | None = None
    theoretical_max_loss: float | None = None
    unlimited_loss: bool = False
    modeled_worst_loss: float | None = None
    expected_shortfall: float | None = None
    capital_required: float | None = None
    breakevens: list[float] = Field(default_factory=list)
    iv_sensitivity: dict[str, float] = Field(default_factory=dict)
    fill_likelihood: float | None = Field(default=None, ge=0, le=1)
    executable: bool = False             # single-leg and supported by the selected account
    notes: list[str] = Field(default_factory=list)


class StudyRun(Strict):
    contract_version: str = CONTRACT_VERSION
    id: str
    symbol: str
    created_at: str
    horizon_days: int = Field(gt=0)
    thesis: Thesis
    direction: Direction | None = None
    objective: str | None = None
    opportunity_id: str | None = None
    status: Literal["running", "complete", "partial", "failed"]
    evidence: EvidenceSnapshot
    forecast: ForecastVersion | None = None
    strategies: list[StrategyCandidate] = Field(default_factory=list)
    fingerprint: str


class OptionTerms(Strict):
    option_symbol: str
    underlying: str
    option_type: Literal["CALL", "PUT"]
    strike: float = Field(gt=0)
    expiration: str
    multiplier: float = 100.0


class OrderTerms(Strict):
    """Exactly what would be sent. An approval is bound to the hash of these and nothing else."""
    account_kind: AccountKind
    account_ref: str | None = None
    symbol: str
    instrument: Instrument
    side: Side
    quantity: float = Field(gt=0)
    order_type: OrderType = "limit"
    limit_price: float | None = Field(default=None, gt=0)
    time_in_force: TimeInForce = "day"
    extended_hours: bool = False
    option: OptionTerms | None = None

    @model_validator(mode="after")
    def _consistent(self) -> "OrderTerms":
        if self.order_type in ("limit", "stop_limit") and self.limit_price is None:
            raise ValueError("a limit order needs a limit price")
        if self.instrument == "option" and self.option is None:
            raise ValueError("an option order needs its contract")
        if self.instrument == "equity" and self.side not in ("buy", "sell", "sell_short", "buy_to_cover"):
            raise ValueError("a share order is buy, sell, sell_short or buy_to_cover")
        if self.instrument == "option" and self.side not in ("buy_to_open", "sell_to_close", "sell_to_open", "buy_to_close"):
            raise ValueError("an option order opens or closes: buy_to_open, sell_to_close, sell_to_open or buy_to_close")
        return self

    def order_hash(self) -> str:
        return digest(self.model_dump(mode="json"))

    def notional(self) -> float:
        px = self.limit_price or 0.0
        mult = self.option.multiplier if self.option else 1.0
        return float(self.quantity * px * mult)


class ExitPlan(Strict):
    stop: float | None = Field(default=None, gt=0)
    target: float | None = Field(default=None, gt=0)
    time_exit: str | None = None          # ISO time, or a rule like "15:55"
    rules: list[str] = Field(default_factory=list)


class RiskCheck(Strict):
    key: str
    label: str
    status: Literal["pass", "warn", "fail", "unknown"]
    value: Any = None
    limit: Any = None
    text: str = ""


class RiskAssessment(Strict):
    """The deterministic risk engine's verdict on one proposal at one moment. `ok` is False when any check fails
    or (for live execution) cannot be measured: an unknown is never counted as a pass."""
    assessed_at: str
    ok: bool
    checks: list[RiskCheck]
    fail_closed: bool = True
    theoretical_max_loss: float | None = None
    capital_required: float | None = None
    quote_age_seconds: float | None = None
    text: str = ""


class TradeProposal(Strict):
    contract_version: str = CONTRACT_VERSION
    id: str
    created_at: str
    updated_at: str
    status: str
    source: Literal["research", "discover", "autopilot", "manual", "blend", "model"]   # 4.0: the deployed models
    terms: OrderTerms
    order_hash: str
    study_id: str | None = None
    opportunity_id: str | None = None
    thesis: str = ""
    evidence_hash: str | None = None
    exit_plan: ExitPlan = Field(default_factory=ExitPlan)
    risk: RiskAssessment | None = None
    policy_id: str | None = None
    authorization: dict[str, Any] | None = None
    expires_at: str
    version: int = 1

    @field_validator("status")
    @classmethod
    def _known(cls, v: str) -> str:
        if v not in PROPOSAL_STATES:
            raise ValueError(f"unknown proposal state {v}")
        return v

    @model_validator(mode="after")
    def _hash_matches(self) -> "TradeProposal":
        if self.order_hash != self.terms.order_hash():
            raise ValueError("the order hash does not match the order terms")
        return self


class Fill(Strict):
    at: str
    quantity: float = Field(gt=0)
    price: float = Field(ge=0)
    fees: float = Field(default=0.0, ge=0)
    external_id: str | None = None


class BrokerOrder(Strict):
    id: str
    proposal_id: str
    broker: Literal["paper", "robinhood", "fake"]
    account_kind: AccountKind
    client_order_id: str
    broker_order_id: str | None = None
    state: str
    terms: OrderTerms
    filled_quantity: float = 0.0
    average_fill_price: float | None = None
    fills: list[Fill] = Field(default_factory=list)

    @field_validator("state")
    @classmethod
    def _known(cls, v: str) -> str:
        if v not in ORDER_STATES:
            raise ValueError(f"unknown order state {v}")
        return v


def can_move(current: str, target: str) -> bool:
    return target in TRANSITIONS.get(current, frozenset())


CONTRACTS = {m.__name__: m for m in (Provenance, RankComponent, Edge, Observation, Opportunity, EvidenceItem, EvidenceSnapshot,
                                      ScenarioPath, Calibration, ForecastVersion, StrategyCandidate, StudyRun, OptionTerms,
                                      OrderTerms, ExitPlan, RiskCheck, RiskAssessment, TradeProposal, Fill, BrokerOrder)}


def schemas() -> dict[str, Any]:
    """Every contract's JSON schema, for /v3/contracts and for anyone building against the app."""
    return {"version": CONTRACT_VERSION, "models": {name: m.model_json_schema() for name, m in CONTRACTS.items()},
            "proposal_states": list(PROPOSAL_STATES), "transitions": {k: sorted(v) for k, v in TRANSITIONS.items()},
            "order_states": list(ORDER_STATES)}
