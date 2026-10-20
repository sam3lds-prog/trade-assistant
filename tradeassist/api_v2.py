"""HTTP API for Trade Assistant V2, under /v2. The classic /api routes are unchanged.

Every response that carries an analysis includes the versions of the logic that produced
it (see versions.py). Areas behind a feature flag answer 404 when the flag is off.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Annotated, Any, Literal

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field

import logging

from .positions import PositionError
from .providers.base import DataError

log = logging.getLogger(__name__)


class Strict(BaseModel):
    model_config = ConfigDict(allow_inf_nan=False, extra="forbid")


class OptionSpec(Strict):
    option_type: Literal["CALL", "PUT", "call", "put"]
    strike: float = Field(..., gt=0)
    expiration: str


class EntrySpec(Strict):
    price: float = Field(..., ge=0)
    quantity: float | None = Field(None, gt=0)
    fees: float = Field(0.0, ge=0)
    executed_at: str | None = None
    side: str | None = None
    note: str = ""


class PositionCreate(Strict):
    symbol: str
    instrument_type: Literal["STOCK", "OPTION"] = "STOCK"
    direction: Literal["LONG", "SHORT"] = "LONG"
    quantity: float = Field(..., gt=0)
    thesis: str = ""
    setup_type: str = ""
    intended_horizon: int | None = Field(None, ge=1, le=1000)
    invalidation_price: float | None = Field(None, gt=0)
    target_1: float | None = Field(None, gt=0)
    target_2: float | None = Field(None, gt=0)
    risk_amount: float | None = Field(None, gt=0)
    initial_risk_per_unit: float | None = Field(None, gt=0)
    option: OptionSpec | None = None
    entry: EntrySpec | None = None
    notes: str = ""


class ExecutionCreate(Strict):
    side: Literal["BUY", "SELL", "SELL_SHORT", "BUY_TO_COVER"]
    quantity: float = Field(..., gt=0)
    price: float = Field(..., ge=0)
    fees: float = Field(0.0, ge=0)
    executed_at: str | None = None
    note: str = ""
    # For fills copied from a broker: its id makes recording the same fill twice a no-op.
    source: Literal["MANUAL", "IMPORT", "ROBINHOOD"] = "MANUAL"
    external_id: str | None = Field(None, min_length=1, max_length=200)


class ExecutionCorrection(Strict):
    """Only the fields sent are corrected. `reason` is required and kept with the change."""
    side: Literal["BUY", "SELL", "SELL_SHORT", "BUY_TO_COVER"] | None = None
    quantity: float | None = Field(None, gt=0)
    price: float | None = Field(None, ge=0)
    fees: float | None = Field(None, ge=0)
    executed_at: str | None = None
    reason: str = Field(..., min_length=3, max_length=500)


class PlanUpdate(Strict):
    """Only the fields sent change. Send null to clear a level, the risk amount or the horizon;
    leave a field out to keep it. Thesis, setup and notes take an empty string to clear."""
    thesis: str | None = None
    setup_type: str | None = None
    intended_horizon: int | None = Field(None, ge=1, le=1000)
    invalidation_price: float | None = Field(None, gt=0)
    target_1: float | None = Field(None, gt=0)
    target_2: float | None = Field(None, gt=0)
    risk_amount: float | None = Field(None, gt=0)
    initial_risk_per_unit: float | None = Field(None, gt=0)
    notes: str | None = None


class AdjustmentCreate(Strict):
    kind: Literal["BORROW_FEE", "BORROW_RATE", "DIVIDEND_OWED", "DIVIDEND_RECEIVED", "CASH_ADJUSTMENT", "SPLIT"]
    amount: float | None = None                     # dollars; negative is a cost
    effective_at: str | None = None
    rate_pct: float | None = Field(None, gt=0, le=1000)
    quantity: float | None = Field(None, gt=0)
    per_share: float | None = Field(None, gt=0)
    ratio: float | None = Field(None, gt=0, le=1000)
    period_start: str | None = None
    period_end: str | None = None
    note: str = ""
    source: Literal["MANUAL", "IMPORT", "ROBINHOOD"] = "MANUAL"
    external_id: str | None = Field(None, min_length=1, max_length=200)
    # A split that would leave shares open on a closed position is refused unless confirmed (reason in `note`).
    confirm_reopen: bool = False


class Void(Strict):
    reason: str = Field(..., min_length=3, max_length=500)
    confirm_reopen: bool = False


class OptionExpire(Strict):
    underlying_price: float | None = Field(None, gt=0)
    reason: str = ""


class OptionExercise(Strict):
    underlying_price: float | None = Field(None, gt=0)
    quantity: int | None = Field(None, ge=1)
    executed_at: str | None = None
    apply_to_position_id: int | None = None
    reason: str = ""


class OptionAdjust(Strict):
    effective_at: str
    new_strike: float = Field(..., gt=0)
    new_multiplier: float = Field(..., gt=0)
    quantity_factor: float = Field(1.0, gt=0)
    new_option_symbol: str | None = None
    reason: str = Field(..., min_length=3, max_length=500)
    confirm_reopen: bool = False


class RepairRequest(Strict):
    confirm: bool = False


class SnapshotBackfill(Strict):
    dry_run: bool = False
    upgrade_hindsight: bool = True


class HistoryBuild(Strict):
    symbols: list[str] | None = Field(None, max_length=60)
    years: float = Field(5.0, ge=1.5, le=10)


class PositionEvaluate(Strict):
    symbol: str | None = None
    direction: Literal["LONG", "SHORT"] = "LONG"
    entry_price: float | None = Field(None, gt=0)
    invalidation_price: float | None = Field(None, gt=0)
    target_1: float | None = Field(None, gt=0)
    target_2: float | None = Field(None, gt=0)
    horizon_days: int | None = Field(None, ge=1, le=260)
    earnings_date: str | None = None
    position_id: int | None = None


class OptionsEvaluate(Strict):
    symbol: str
    direction: Literal["BULLISH", "BEARISH"] = "BULLISH"
    target_price: float | None = Field(None, gt=0)
    target_2: float | None = Field(None, gt=0)
    invalidation_price: float | None = Field(None, gt=0)
    horizon_days: int = Field(20, ge=1, le=260)
    contracts: int = Field(1, ge=1, le=500)
    budget: float | None = Field(None, gt=0)
    expirations: list[str] | None = Field(None, max_length=6)
    earnings_date: str | None = None
    entry_price: float | None = Field(None, gt=0)      # 2.5.1: the share entry, for the chance the shares are ahead


class MorningCard(Strict):
    symbol: str = Field(..., min_length=1, max_length=12)
    direction: Literal["LONG", "SHORT"] = "LONG"
    setup: Literal["fade", "ride", "reversal", "manual", "analysis"] = "manual"
    entry: float | None = Field(None, gt=0)
    stop: float = Field(..., gt=0)
    target_1: float = Field(..., gt=0)
    target_2: float | None = Field(None, gt=0)
    horizon_days: int = Field(5, ge=1, le=260)


class WatchCreate(Strict):
    symbol: str = Field(..., min_length=1, max_length=12)
    direction: Literal["LONG", "SHORT"]
    kind: Literal["above", "below"]
    level: float = Field(..., gt=0)
    why: str = Field("", max_length=300)


class IdeasScan(Strict):
    universe: Literal["top250", "top200", "large_caps", "watchlist"] = "top250"


class HistoryImport(Strict):
    filename: str | None = Field(None, max_length=2000)          # kept to its first 200 characters
    text: str = Field(..., min_length=1, max_length=16_000_000)


class ContractRef(Strict):
    option_type: Literal["CALL", "PUT", "call", "put"]
    strike: float = Field(..., gt=0)
    expiration: str


class TradeSimulate(Strict):
    symbol: str = Field(..., min_length=1, max_length=12)
    direction: Literal["LONG", "SHORT"] = "LONG"
    entry_price: float | None = Field(None, gt=0)
    stop: float | None = Field(None, gt=0)
    target_1: float | None = Field(None, gt=0)
    target_2: float | None = Field(None, gt=0)
    shares: int | None = Field(None, ge=1, le=10_000_000)
    horizon_days: int = Field(20, ge=1, le=120)
    exit_plan: Literal["target_1", "target_2", "split"] = "target_1"
    contract: ContractRef | Literal["none"] | None = None
    contracts: int | None = Field(None, ge=1, le=500)


class OptionsCompare(OptionsEvaluate):
    contracts_to_compare: list[ContractRef] = Field(..., min_length=1, max_length=8)


class MonteCarloRequest(Strict):
    symbol: str = Field(..., min_length=1, max_length=12)
    expiration: str
    option_type: Literal["call", "put"]
    strike: float = Field(..., gt=0)
    premium: float | None = Field(None, ge=0)
    contracts: int = Field(1, ge=1, le=500)
    paths: int = Field(20_000, ge=2_000, le=100_000)
    seed: int = Field(7, ge=0, le=2**31 - 1)
    earnings_jump: bool = False


class StrategyAdjust(Strict):
    op: Literal["up", "down", "wider", "narrower", "leg"]
    leg: int | None = Field(None, ge=0, le=3)
    by: int | None = Field(None, ge=-1, le=1)


class StrategyBuild(Strict):
    symbol: str = Field(..., min_length=1, max_length=12)
    strategy: str = Field(..., min_length=1, max_length=40)
    expiration: str | None = None
    dte: int | None = Field(None, ge=1, le=1000)
    far_expiration: str | None = None
    far_days: int | None = Field(None, ge=7, le=400)
    contracts: int = Field(1, ge=1, le=100)
    delta: float | None = Field(None, gt=0, lt=100)
    width: float | None = Field(None, gt=0)
    strikes: list[float] | None = Field(None, min_length=1, max_length=4)
    center: float | None = Field(None, gt=0)
    after_earnings: bool = False
    adjust: StrategyAdjust | None = None


class StrategyParse(Strict):
    text: str = Field(..., min_length=1, max_length=500)
    symbol: str | None = Field(None, max_length=12)


class BriefRequest(Strict):
    web_search: bool = True
    refresh: bool = False


class ToneRequest(Strict):
    symbol: str | None = Field(None, max_length=12)


class ReversalOptions(Strict):
    symbol: str
    side: Literal["auto", "bottom", "top"] = "auto"
    target_price: float | None = Field(None, gt=0)
    target_2: float | None = Field(None, gt=0)
    invalidation_price: float | None = Field(None, gt=0)
    horizon_days: int = Field(20, ge=1, le=260)
    contracts: int = Field(1, ge=1, le=500)
    budget: float | None = Field(None, gt=0)
    expirations: list[str] | None = Field(None, max_length=6)
    earnings_date: str | None = None
    contracts_to_score: list[ContractRef] | None = Field(None, min_length=1, max_length=8)


class ChartArgs(Strict):
    """Which chart window the what-if (2.13) reads its pattern from: the same arguments as GET /v2/chart."""
    range: str = Field("6m", max_length=12)
    group: str | None = Field(None, max_length=4)
    start: str | None = Field(None, max_length=32)
    end: str | None = Field(None, max_length=32)
    window_start: int | None = Field(None, ge=0)
    window_end: int | None = Field(None, ge=0)
    min_correlation: float = Field(0.75, ge=0.3, le=0.99)


class WhatIfModel(ChartArgs):
    symbol: str = Field(..., min_length=1, max_length=12)
    horizon: int = Field(..., ge=1, le=400)
    scenario: dict[str, float] = Field(default_factory=dict)
    include: dict[str, bool] = Field(default_factory=dict)
    peers: list[Annotated[str, Field(max_length=12)]] = Field(default_factory=list, max_length=6)


class WhatIfAsk(WhatIfModel):
    provider: Literal["chatgpt", "claude"] = "chatgpt"


class WhatIfPosition(Strict):
    symbol: str = Field(..., min_length=1, max_length=12)
    kind: str = Field(..., min_length=1, max_length=24)
    quantity: int = Field(100, ge=1, le=100000)
    expected_pct: float = Field(..., ge=-90, le=300)
    sd_pct: float = Field(..., ge=0, le=200)
    horizon_bars: int = Field(..., ge=1, le=400)
    group: str = Field("1d", max_length=4)


class WhatIfLeg(Strict):
    kind: str = Field(..., min_length=1, max_length=24)
    quantity: int = Field(1, ge=1, le=100000)


class WhatIfCombo(Strict):
    symbol: str = Field(..., min_length=1, max_length=12)
    legs: list[WhatIfLeg] = Field(..., min_length=1, max_length=6)
    expected_pct: float = Field(..., ge=-90, le=300)
    sd_pct: float = Field(..., ge=0, le=200)
    horizon_bars: int = Field(..., ge=1, le=400)
    group: str = Field("1d", max_length=4)


class WhatIfAllocate(Strict):
    symbol: str = Field(..., min_length=1, max_length=12)
    budget: float = Field(..., ge=1, le=10_000_000)
    risk: float = Field(1.0, ge=0, le=10)
    max_share: float = Field(0.5, ge=0.05, le=1.0)
    kinds: list[Annotated[str, Field(max_length=24)]] | None = Field(None, max_length=24)
    expected_pct: float = Field(..., ge=-90, le=300)
    sd_pct: float = Field(..., ge=0, le=200)
    horizon_bars: int = Field(..., ge=1, le=400)
    group: str = Field("1d", max_length=4)


class WhatIfRank(Strict):
    symbol: str = Field(..., min_length=1, max_length=12)
    quantity: int = Field(100, ge=1, le=100000)
    contracts: int | None = Field(None, ge=1, le=100)
    expected_pct: float = Field(..., ge=-90, le=300)
    sd_pct: float = Field(..., ge=0, le=200)
    horizon_bars: int = Field(..., ge=1, le=400)
    group: str = Field("1d", max_length=4)
    kinds: list[Annotated[str, Field(max_length=24)]] | None = Field(None, max_length=24)
    ai_pick: str | None = Field(None, max_length=24)


class Scenario(Strict):
    name: str = ""
    market_move_pct: float = Field(0.0, ge=-90, le=200)
    iv_shift_points: float = Field(0.0, ge=-50, le=150)
    largest_gap_pct: float = Field(0.0, ge=0, le=90)
    moves: dict[str, float] | None = None


class StressRequest(Strict):
    scenarios: list[Scenario] = Field(..., min_length=1, max_length=20)


class Condition(Strict):
    field: str
    op: str
    value: float | str | list[str] | None = None


class StrategySpec(Strict):
    long: list[Condition] = Field(default_factory=list, max_length=8)
    short: list[Condition] = Field(default_factory=list, max_length=8)
    regimes: list[str] = Field(default_factory=list, max_length=6)


class BacktestV2(Strict):
    symbols: list[str] | None = Field(None, max_length=40)
    years: float = Field(3.0, ge=1, le=10)
    preset: str | None = None
    regimes: list[str] | None = None
    strategy: StrategySpec | None = None
    params: dict[str, float | int | bool] | None = None
    monte_carlo: bool = True
    n_trials: int = Field(1, ge=1, le=10_000)
    instrument: Literal["STOCK", "OPTION"] = "STOCK"


class WalkForwardV2(BacktestV2):
    years: float = Field(4.0, ge=2, le=10)
    grid: dict[str, list[float]] | None = None
    train_days: int = Field(252, ge=60, le=1000)
    test_days: int = Field(63, ge=20, le=500)
    anchored: bool = False


class Note(Strict):
    note: str = ""


# --- 2.15: opportunities, blends and paper trading -----------------------------------------------------------
Instrument = Literal["shares", "short", "call", "put"]
ALL_INSTRUMENTS: list[Instrument] = ["shares", "short", "call", "put"]


class PrefsUpdate(Strict):
    morning: dict[str, Any] | None = None
    live: dict[str, Any] | None = None
    blend: dict[str, Any] | None = None


class BlendLeg(Strict):
    key: str | None = Field(None, max_length=200)
    symbol: str = Field(..., min_length=1, max_length=12)
    direction: Literal["LONG", "SHORT"] | None = None
    horizon: Literal["day", "swing"] | None = None
    horizon_days: int | None = Field(None, ge=1, le=260)
    entry: float | None = Field(None, gt=0)
    stop: float | None = Field(None, gt=0)
    target_1: float | None = Field(None, gt=0)
    target_2: float | None = Field(None, gt=0)


class BlendRequest(Strict):
    """Which opportunities (a source's best `top_n`, these keys, or basket legs) and how to split the budget."""
    source: Literal["morning", "live", "ideas", "all", "basket"] = "morning"
    keys: list[str] | None = Field(None, max_length=40)
    legs: list[BlendLeg] | None = Field(None, max_length=25)
    top_n: int = Field(10, ge=1, le=25)
    budget: float = Field(..., ge=100, le=10_000_000)
    goal: Literal["dollars", "balanced", "low_risk"] = "balanced"
    tail_pct: float = Field(2.0, ge=0.25, le=20)
    max_share: float = Field(0.2, ge=0.05, le=1)
    max_positions: int = Field(10, ge=1, le=25)
    sector_cap: float = Field(0.4, ge=0.1, le=1)
    instruments: list[Instrument] = Field(default_factory=lambda: list(ALL_INSTRUMENTS))
    obey_limits: bool = True          # honoured only with `paper`: a real-money split always obeys your limits
    paper: bool = False               # a split for a paper portfolio: measured on its own, limits may be lifted
    horizon: Literal["day", "swing"] | None = None
    units: dict[str, int] | None = None
    with_options: bool = True


class DigestBuild(Strict):
    interval: Literal["15", "30", "60", "session"] | None = None


class ReplayEvent(Strict):
    """2.16: a news event typed in by hand for the replay: the stock, when the headline was published (ISO time with
    its offset, or New York time), the headline, and the way to trade it when the headline does not say."""
    symbol: str = Field(..., min_length=1, max_length=12)
    published_at: str = Field(..., min_length=10, max_length=40)
    headline: str = Field("", max_length=300)
    direction: Literal["LONG", "SHORT"] | None = None


class ReplayRequest(Strict):
    events: Literal["feed", "manual", "both"] = "feed"
    manual: list[ReplayEvent] = Field(default_factory=list, max_length=50)
    days: float | None = Field(None, gt=0, le=60)
    feed_latency_s: float = Field(1.0, ge=0, le=3600)
    spread_bps: float | None = Field(None, ge=0, le=1000)      # None: estimated from the prints before the news
    slippage_bps: float = Field(5.0, ge=0, le=500)
    limit: int = Field(60, ge=1, le=150)


class PaperOption(Strict):
    option_type: Literal["CALL", "PUT", "call", "put"] | None = None
    strike: float = Field(..., gt=0)
    expiration: str
    iv: float | None = Field(None, gt=0, le=10)


class PaperTrade(Strict):
    symbol: str = Field(..., min_length=1, max_length=12)
    instrument: Instrument = "shares"
    quantity: int = Field(..., ge=1, le=1_000_000)
    option: PaperOption | None = None
    limit_price: float | None = Field(None, gt=0)
    stop: float | None = Field(None, gt=0)
    target_1: float | None = Field(None, gt=0)
    target_2: float | None = Field(None, gt=0)
    horizon_days: int | None = Field(None, ge=1, le=260)
    opp_key: str | None = Field(None, max_length=200)
    source: str | None = Field(None, max_length=20)
    setup_label: str | None = Field(None, max_length=120)
    why: str = Field("", max_length=300)


class PaperBlendImport(Strict):
    """A split from the blend, taken into a paper portfolio: the same opportunities and units."""
    source: Literal["morning", "live", "ideas", "all", "basket"] = "morning"
    keys: list[str] | None = Field(None, max_length=40)
    legs: list[BlendLeg] | None = Field(None, max_length=25)
    units: dict[str, int] = Field(..., min_length=1)


class PaperCreate(Strict):
    name: str = Field("", max_length=80)
    mode: Literal["AUTO", "MANUAL"] = "MANUAL"
    budget: float = Field(..., ge=100, le=10_000_000)
    source: Literal["morning", "live", "ideas", "all", "manual"] | None = None
    goal: Literal["dollars", "balanced", "low_risk"] = "balanced"
    tail_pct: float = Field(2.0, ge=0.25, le=20)
    top_n: int = Field(10, ge=1, le=25)
    horizon: Literal["day", "swing"] = "day"
    ends: Literal["day", "week", "month", "custom", "open"] | None = None
    ends_on: str | None = None
    fill_model: Literal["REALISTIC", "CONSERVATIVE", "MIDPOINT"] = "REALISTIC"
    obey_limits: bool = True
    rotate: bool = True
    refresh_minutes: int | None = None
    instruments: list[Instrument] = Field(default_factory=lambda: list(ALL_INSTRUMENTS))
    max_share: float = Field(0.2, ge=0.05, le=1)
    max_positions: int = Field(10, ge=1, le=25)
    sector_cap: float = Field(0.4, ge=0.1, le=1)
    trades: list[PaperTrade] | None = Field(None, max_length=25)
    blend: PaperBlendImport | None = None
    notes: str = Field("", max_length=500)


class PaperClose(Strict):
    quantity: int | None = Field(None, ge=1)
    reason: str = Field("closed by you", max_length=200)


class PaperSettings(Strict):
    rotate: bool | None = None
    refresh_minutes: int | None = None
    name: str | None = Field(None, max_length=80)


class PaperReplay(Strict):
    run_id: int | None = Field(None, ge=1)
    picks_day: str | None = None
    budget: float = Field(..., ge=100, le=10_000_000)
    goal: Literal["dollars", "balanced", "low_risk"] = "balanced"
    tail_pct: float = Field(2.0, ge=0.25, le=20)
    top_n: int = Field(10, ge=1, le=25)
    horizon_days: int | None = Field(None, ge=1, le=60)
    max_share: float = Field(0.2, ge=0.05, le=1)
    max_positions: int = Field(10, ge=1, le=25)
    instruments: list[Instrument] = Field(default_factory=lambda: list(ALL_INSTRUMENTS))
    obey_limits: bool = False
    save: bool = False


def build_router(svc: Any) -> APIRouter:
    from .desk import Desk
    from .service import clean

    from .analysis import Analyst

    router = APIRouter(prefix="/v2")
    desk = svc.desk if getattr(svc, "desk", None) else Desk(svc)
    svc.desk = desk
    analyst = svc.analyst if getattr(svc, "analyst", None) else Analyst(svc)
    svc.analyst = analyst

    def trend_for_snapshot(symbol: str, values: dict[str, Any], as_of: Any = None) -> dict[str, Any]:
        return analyst.snapshot_extra(symbol, values, as_of) if svc.settings.features.get("trend_engine_v2") else {}
    trend_for_snapshot.__name__ = "trend"
    if not any(getattr(f, "__name__", "") == "trend" for f in desk.snapshot_extras):
        desk.snapshot_extras.append(trend_for_snapshot)

    def need(flag: str) -> None:
        if not svc.settings.features.get(flag, False):
            raise HTTPException(status_code=404, detail=f"The {flag.replace('_', ' ')} feature is switched off "
                                                        f"(FEATURE_{flag.upper()}=0 in .env).")

    @router.get("/features")
    def features():
        return svc.settings.features

    # --- data quality --------------------------------------------------------------------
    @router.get("/data/{symbol}")
    def data_quality(symbol: str, years: float = 3.0):
        from .dataquality import assess
        sym = svc._symbol(symbol)
        bars = svc.market.bars(sym, max(1.0, min(years, 10.0)))
        out = assess(sym, bars, svc.market.bar_info(sym))
        out["bars_freshness"] = svc.market.bars_freshness(sym)
        try:
            out["quote_freshness"] = svc.market.quote(sym).get("_meta")
        except DataError as exc:
            out["quote_freshness"] = {"freshness": "UNKNOWN", "completeness": "MISSING", "warnings": [str(exc)]}
        return clean(out)

    @router.get("/diagnostics/reconciliation")
    def reconciliation():
        """Read-only: every stored figure checked against the fills, and what needs a look (reconcile.py)."""
        need("unified_positions")
        from .reconcile import diagnose
        return clean(diagnose(svc.db, svc.market))

    @router.post("/diagnostics/repair")
    def repair(req: RepairRequest):
        """Recompute derived figures from the fills, after copying the database. Never changes a fill."""
        need("unified_positions")
        if not req.confirm:
            raise PositionError("Repair changes stored figures. Send {\"confirm\": true} after reading the "
                                "reconciliation report; a copy of the database is taken first.")
        from .reconcile import repair as run_repair
        return clean(run_repair(svc.db, svc.market))

    @router.get("/health/data")
    def data_health():
        """What the data health screen shows. No credentials or settings values are included."""
        from . import events
        from .reconcile import diagnose
        from .snapshots import status as snapshot_status
        info, health = svc.market.info, svc.market.health.summary()
        out: dict[str, Any] = {"source": {"label": info.label, "mode": info.mode, "realtime": info.realtime, **health}}
        positions, unreadable = [], []
        if svc.settings.features.get("unified_positions"):
            listed = desk.list("open")
            positions, unreadable = listed["positions"], listed.get("invalid", [])
        fresh_counts: dict[str, int] = {}
        missing, chains = [], []
        for p in positions:
            mk = p.get("mark") or {}
            f = (mk.get("freshness") or {}).get("freshness") or ("NONE" if mk.get("price") is None else "UNKNOWN")
            fresh_counts[f] = fresh_counts.get(f, 0) + 1
            if mk.get("price") is None:
                missing.append({"id": p["id"], "description": p["description"], "error": mk.get("error")})
                if "chain" in str(mk.get("error") or "").lower():
                    chains.append({"id": p["id"], "description": p["description"], "error": mk.get("error")})
        symbols = sorted({p["symbol"] for p in positions} | set(svc.settings.watchlist[:20]))
        bars = []
        for sym in symbols:
            meta = svc.db.one("SELECT last_date, fetched_at, source FROM bars_meta WHERE symbol = ?", (sym,))
            row = {"symbol": sym, "held": any(p["symbol"] == sym for p in positions)}
            if meta is None:
                bars.append({**row, "status": "NOT_LOADED"})
                continue
            f = svc.market.bars_freshness(sym)
            bars.append({**row, "status": f["freshness"], "last_finished_bar": f.get("last_finished_bar"),
                         "fetched_at": f.get("fetched_at"), "refresh_failed": svc.market.bar_info(sym).get("refresh_failed")})
        last = svc.db.one("SELECT version, name, applied_at FROM schema_migrations ORDER BY version DESC LIMIT 1")
        repairs = [dict(r) for r in svc.db.query("SELECT id, action, at, backup_path FROM maintenance_log "
                                                 "ORDER BY id DESC LIMIT 5")]
        recon = diagnose(svc.db, svc.market) if svc.settings.features.get("unified_positions") else None
        out.update({
            "quotes": {"open_positions": len(positions), "by_freshness": fresh_counts,
                       "stale": fresh_counts.get("STALE", 0) + fresh_counts.get("UNKNOWN", 0)},
            "positions_missing_valuations": missing, "missing_option_chains": chains, "history": bars,
            "unreadable_positions": [{"id": u["id"], "description": u["description"], "error": u["error"]}
                                     for u in unreadable],
            "snapshots": snapshot_status(svc.db) if svc.settings.features.get("unified_positions") else None,
            "migration": {"schema_version": svc.db.schema_version(), "latest": dict(last) if last else None,
                          "backup_before_last_migration": svc.db.backup_before_migration.name
                          if svc.db.backup_before_migration else None},
            "reconciliation": {"summary": recon["summary"], "needs_repair": recon["needs_repair"],
                               "needs_attention": recon["needs_attention"], "database_ok": recon["database"]["ok"]}
            if recon else None,
            "repairs": repairs, "recent_problems": events.recent(30),
            "ai": svc.ai.status() if getattr(svc, "ai", None) else None,
            "news": svc.news.status() if getattr(svc, "news", None) else None,
            "checked_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        })
        return clean(out)

    @router.get("/research/runs")
    def research_runs(kind: str | None = None, limit: int = 50):
        from . import runs
        return clean({"runs": runs.listing(svc.db, kind, max(1, min(limit, 500)))})

    @router.get("/research/runs/compare")
    def compare_runs(a: int, b: int):
        from . import runs
        ra, rb = runs.load(svc.db, a), runs.load(svc.db, b)
        if ra is None or rb is None:
            raise HTTPException(status_code=404, detail="No such run")
        return clean(runs.compare(ra, rb))

    @router.get("/research/runs/{run_id}")
    def research_run(run_id: int):
        from . import runs
        r = runs.load(svc.db, run_id)
        if r is None:
            raise HTTPException(status_code=404, detail=f"No run {run_id}")
        return clean(r)

    @router.get("/source/health")
    def source_health():
        """The data source's recent successes and failures (freshness.SourceHealth). No credentials."""
        info = svc.market.info
        return clean({"source": info.label, "mode": info.mode, "realtime": info.realtime, "note": info.note,
                      **svc.market.health.summary()})

    # --- trend and analysis ------------------------------------------------------------------
    @router.get("/trend/{symbol}")
    def trend(symbol: str):
        need("trend_engine_v2")
        try:
            return clean(analyst.trend(symbol))
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @router.get("/regime")
    def regime():
        need("trend_engine_v2")
        try:
            return clean(analyst.regime())
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @router.get("/analysis/{symbol}")
    def analysis(symbol: str):
        need("trend_engine_v2")
        try:
            return clean(analyst.analysis(symbol))
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    # --- signals, calibration and patterns -----------------------------------------------------
    @router.post("/signals/history/build")
    def build_signal_history(req: HistoryBuild):
        need("signal_calibration")
        try:
            return clean(analyst.build_history(req.symbols, req.years))
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @router.get("/signals/calibration")
    def calibration():
        need("signal_calibration")
        return clean(analyst.calibration())

    @router.get("/signals/{symbol}")
    def signals(symbol: str):
        need("signal_calibration")
        try:
            return clean(analyst.signals(symbol))
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @router.get("/patterns/{symbol}")
    def patterns(symbol: str):
        need("signal_calibration")
        return clean(analyst.patterns(symbol))

    # --- evaluators ---------------------------------------------------------------------------
    @router.post("/position/evaluate")
    def evaluate_position(req: PositionEvaluate):
        need("trend_engine_v2")
        if not req.symbol and not req.position_id:
            raise HTTPException(status_code=400, detail="Give a symbol or a position_id")
        try:
            return clean(analyst.evaluate_position(req.model_dump()))
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @router.post("/options/evaluate")
    def evaluate_options(req: OptionsEvaluate):
        need("trend_engine_v2")
        try:
            return clean(analyst.evaluate_options(req.model_dump()))
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @router.post("/options/compare")
    def compare_options(req: OptionsCompare):
        need("trend_engine_v2")
        body = req.model_dump()
        only = body.pop("contracts_to_compare")
        kind = "CALL" if body["direction"] == "BULLISH" else "PUT"
        if any(c["option_type"].upper() != kind for c in only):
            raise HTTPException(status_code=400, detail=f"A {body['direction'].lower()} thesis compares {kind.lower()}s only")
        try:
            return clean(analyst.evaluate_options(body, only=only))
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    # --- reversals: potential bottoms and tops, and options to trade them ---------------------------
    @router.get("/reversal/scan")
    def reversal_scan(symbols: str | None = None):
        need("reversal_analysis")
        names = [s for s in (symbols or "").split(",") if s.strip()] or None
        try:
            return clean(analyst.reversal_scan(names))
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @router.get("/reversal/{symbol}")
    def reversal(symbol: str):
        need("reversal_analysis")
        try:
            return clean(analyst.reversal(symbol))
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    # --- the interactive chart (2.12) ---------------------------------------------------------------------
    from .chartview import ChartView
    from .chartview import RequestError as ChartRequestError
    chart_view = svc.chart_view if getattr(svc, "chart_view", None) else ChartView(svc, analyst)
    svc.chart_view = chart_view

    def chart_call(fn):
        need("analyze_interactive")
        try:
            return clean(fn())
        except (ChartRequestError, ValueError) as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @router.get("/chart/{symbol}")
    def chart(symbol: str, range: str = "6m", group: str | None = None, start: str | None = None,
              end: str | None = None, swing_atr: float = 2.0):
        """The bars for a range and grouping with indicators, swings, levels, trendlines, where a top or bottom
        may be forming, and the news on the bars."""
        if swing_atr not in (1.5, 2.0, 3.0):
            raise HTTPException(status_code=400, detail="swing_atr must be 1.5, 2 or 3")
        return chart_call(lambda: chart_view.chart(symbol, range, group, start, end, swing_atr))

    @router.get("/chart/{symbol}/similar")
    def chart_similar(symbol: str, range: str = "6m", group: str | None = None, start: str | None = None,
                      end: str | None = None, window_start: int | None = None, window_end: int | None = None,
                      min_correlation: float = 0.75):
        """Where the shape of the window appeared before in this stock's own history, and what followed."""
        return chart_call(lambda: chart_view.similar(symbol, range, group, start, end, window_start, window_end,
                                                     min_correlation))

    @router.get("/chart/{symbol}/outlook")
    def chart_outlook(symbol: str, range: str = "6m", group: str | None = None, start: str | None = None,
                      end: str | None = None, window_start: int | None = None, window_end: int | None = None,
                      horizon: int | None = None, min_correlation: float = 0.75):
        """The possible outcomes over a horizon and how often each happened before on this stock."""
        return chart_call(lambda: chart_view.outlook(symbol, range, group, start, end, window_start, window_end,
                                                     horizon, min_correlation))

    @router.post("/reversal/options")
    def reversal_options(req: ReversalOptions):
        need("reversal_analysis")
        try:
            return clean(analyst.reversal_options(req.model_dump()))
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    # --- Today (2.5): which stocks lean long or short, from the trend engine; read only ---------------
    from .today import Today
    today = Today(svc, analyst, svc.settings.features)

    @router.get("/today/candidates")
    def today_candidates(universe: str = "watchlist"):
        need("today")
        need("trend_engine_v2")
        try:
            return clean(today.candidates(universe))
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    # --- trade ideas (2.7): the best-graded trades across up to 200 stocks, and a simulation of any one ----
    from .ideas import TradeIdeas
    from .tradesim import TradeSimError, simulate_trade
    ideas = svc.ideas if getattr(svc, "ideas", None) else TradeIdeas(svc, analyst, svc.settings.features)
    svc.ideas = ideas

    @router.post("/ideas/scan")
    def ideas_scan(req: IdeasScan):
        need("trade_ideas")
        need("trend_engine_v2")
        try:
            return clean(ideas.start(req.universe))
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @router.get("/ideas")
    def ideas_status(brief: bool = False):
        """The scan's progress, and its latest result unless `brief` (the screen polls briefly while it runs)."""
        need("trade_ideas")
        need("trend_engine_v2")
        return clean(ideas.status(brief))

    @router.post("/ideas/stop")
    def ideas_stop():
        need("trade_ideas")
        need("trend_engine_v2")
        return clean(ideas.stop())

    @router.post("/ideas/simulate")
    def ideas_simulate(req: TradeSimulate):
        need("trade_ideas")
        need("trend_engine_v2")
        body = req.model_dump()
        if isinstance(req.contract, ContractRef):
            body["contract"] = req.contract.model_dump()
        try:
            return clean(simulate_trade(svc, analyst, body))
        except (TradeSimError, ValueError) as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    # --- the morning plan (2.8): movers, setups, reversal triggers, the email -----------------------------
    from . import mailer
    from .morning import Morning, WatchChecker, add_watch, list_watches, record_picks, remove_watch, scorecard
    morning = svc.morning if getattr(svc, "morning", None) else Morning(svc, analyst, svc.settings.features)
    svc.morning = morning
    watcher = svc.watcher if getattr(svc, "watcher", None) else WatchChecker(svc, morning)
    svc.watcher = watcher
    if svc.settings.features.get("morning"):
        try:
            if any(w["active"] for w in list_watches(svc.db)):
                watcher.ensure_running()               # watches left from the last run keep being checked
        except Exception:                              # noqa: BLE001 - a damaged table must not stop the app
            log.exception("Morning watches could not be read at startup")

    def need_morning() -> None:
        need("morning")
        need("trend_engine_v2")

    @router.get("/morning")
    def morning_status(brief: bool = False):
        need_morning()
        out = morning.status(brief)
        out["watches"] = list_watches(svc.db)
        out["email_configured"] = mailer.configured(svc.settings)
        out["email_to"] = mailer.to_address(svc.settings) if mailer.configured(svc.settings) else None
        if not brief:
            out["scorecard"] = scorecard(svc)
        return clean(out)

    @router.post("/morning/scan")
    def morning_scan():
        need_morning()
        return clean(morning.start())

    @router.post("/morning/stop")
    def morning_stop():
        need_morning()
        return clean(morning.stop())

    @router.post("/morning/card")
    def morning_card(req: MorningCard):
        need_morning()
        try:
            entry = req.entry if req.entry is not None else svc.market.spot(svc._symbol(req.symbol))
            why = "Your thesis levels (the grade above settled them)." if req.setup == "analysis" else ""
            return clean(morning.card(symbol=svc._symbol(req.symbol), direction=req.direction, entry=float(entry),
                                      stop=req.stop, target_1=req.target_1,
                                      target_2=req.target_2 if req.target_2 is not None else req.target_1,
                                      horizon_days=req.horizon_days, setup=req.setup, levels_why=why))
        except (DataError, ValueError) as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @router.post("/morning/watch")
    def morning_watch(req: WatchCreate):
        need_morning()
        d = 1 if req.direction == "LONG" else -1
        # A watch that is already true would fire on the next tick; refuse it so a typo is caught here.
        try:
            now = svc.market.spot(svc._symbol(req.symbol))
        except DataError:
            now = None
        if now is not None and ((req.kind == "above" and now >= req.level) or (req.kind == "below" and now <= req.level)):
            raise HTTPException(status_code=400, detail=f"{req.symbol} is already {req.kind} ${req.level:,.2f} "
                                                        f"(now ${now:,.2f}); there is nothing to wait for.")
        if sum(1 for w in list_watches(svc.db) if w["active"]) >= 40:
            raise HTTPException(status_code=400, detail="40 price watches are already active; remove one first "
                                                        "(each minute's check quotes every watched stock).")
        wid = add_watch(svc.db, symbol=svc._symbol(req.symbol), direction=req.direction, kind=req.kind,
                        level=req.level, why=req.why or f"{'Bottom' if d > 0 else 'Top'} watch")
        watcher.ensure_running()
        return clean({"id": wid, "watches": list_watches(svc.db),
                      "email_configured": mailer.configured(svc.settings),
                      "note": "Checked about once a minute while the market is open (pre-market: stock price only), "
                              "while the app is running on this computer."})

    @router.post("/morning/watches/{watch_id}/remove")
    def morning_watch_remove(watch_id: int):
        need_morning()
        remove_watch(svc.db, watch_id)
        return clean({"watches": list_watches(svc.db)})

    @router.post("/morning/email")
    def morning_email_now():
        need_morning()
        from datetime import datetime as _dt

        from . import morning_email as renderer
        from .optionmath import NY as _NY
        st = morning.status()
        if not st.get("result"):
            raise HTTPException(status_code=400, detail="Build the morning plan first (Scan), then send it.")
        plan = st["result"]
        if plan.get("day") != _dt.now(_NY).date().isoformat():
            raise HTTPException(status_code=400, detail=f"This plan is from {plan.get('day')}. Build today's "
                                                        "first (Refresh the plan), then send it.")
        subject, text, html = renderer.render(plan, scorecard(svc))
        try:
            to = mailer.send(svc.settings, subject, text, html)
        except mailer.MailError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        record_picks(svc.db, plan, "screen")           # picks count only once the plan reached you
        return clean({"sent_to": to, "subject": subject})

    @router.post("/analysis/email")
    def analysis_email(req: MorningCard):
        """2.9: email the analyzed stock's trade card (built fresh here, never from the browser's copy)."""
        need("trend_engine_v2")
        need_morning()
        from . import morning_email as renderer
        sym = svc._symbol(req.symbol)
        try:
            entry = req.entry if req.entry is not None else svc.market.spot(sym)
            card = morning.card(symbol=sym, direction=req.direction, entry=float(entry), stop=req.stop,
                                target_1=req.target_1,
                                target_2=req.target_2 if req.target_2 is not None else req.target_1,
                                horizon_days=req.horizon_days, setup="analysis",
                                levels_why="Your thesis levels from Analyze opportunity.")
        except (DataError, ValueError) as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        trend = None
        try:
            trend = analyst.trend(sym)
        except Exception:                             # noqa: BLE001 - the email is about the trade; no trend line is fine
            log.info("Analysis email: no trend line for %s", sym)
        subject, text, html = renderer.render_analysis(sym, trend, card, demo=svc.market.info.mode == "demo")
        try:
            to = mailer.send(svc.settings, subject, text, html)
        except mailer.MailError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return clean({"sent_to": to, "subject": subject})

    # --- live news and moves (2.10): news as it arrives, the price's answer, a card when it confirms ----------
    from .livenews import LiveFeed
    live = svc.live_feed if getattr(svc, "live_feed", None) else LiveFeed(svc, morning, analyst)
    svc.live_feed = live                              # not svc.live: that is the live option quote (/api/live)
    if svc.settings.features.get("live_news"):
        try:
            live.resume_if_wanted()                   # LIVE_NEWS_AUTOSTART=1, or it was on when the app last stopped
        except Exception:                             # noqa: BLE001 - the app starts whatever happens here
            log.exception("Live news could not be resumed at startup")

    @router.get("/live")
    def live_snapshot(after: int = 0, all_symbols: bool = False, limit: int = 150):
        need("live_news")
        return clean(live.snapshot(after=max(0, after), limit=max(1, min(limit, 500)), all_symbols=all_symbols))

    @router.post("/live/start")
    def live_start():
        need("live_news")
        if not live.source_names():
            raise HTTPException(status_code=400, detail="No news source is set up. " + " ".join(live.setup_notes()))
        try:
            out = live.start()
        except RuntimeError as exc:                   # still finishing the pass it was on when stopped
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        wake()                                        # the digest is built on its interval while Live news listens
        return clean(out)

    @router.post("/live/stop")
    def live_stop():
        need("live_news")
        return clean(live.stop())

    # --- 2.16: the news replay: what was really available after a headline ---------------------------------------
    from .newsreplay import NewsReplay
    replay = svc.replay if getattr(svc, "replay", None) else NewsReplay(svc, live)
    svc.replay = replay
    live.replay = replay                              # the live assessment reads the replay's record

    @router.get("/live/replay")
    def live_replay_status():
        need("live_news")
        return clean(replay.status())

    @router.post("/live/replay")
    def live_replay_start(req: ReplayRequest):
        need("live_news")
        from .livenews import parse_time
        events: list[dict[str, Any]] = []
        for m in req.manual:
            raw = m.published_at.strip()
            try:
                when = datetime.fromisoformat(raw.replace("Z", "+00:00"))
            except ValueError:
                when = parse_time(raw)                # Unix seconds, say
            if when is None:
                raise HTTPException(status_code=400, detail=f"{m.symbol}: the time {m.published_at!r} could not be read "
                                                            "(use ISO 8601, e.g. 2026-10-19T08:30:00-04:00)")
            if when.tzinfo is None:
                from .optionmath import NY
                when = when.replace(tzinfo=NY)        # a bare time is New York time, as the wires print it
            if when > datetime.now(timezone.utc):
                raise HTTPException(status_code=400, detail=f"{m.symbol}: {m.published_at} is in the future")
            events.append({"symbol": svc._symbol(m.symbol), "published_at": when.isoformat(), "headline": m.headline,
                           "source": "manual", "direction": {"LONG": 1, "SHORT": -1}.get(m.direction or "", 0)})
        if req.events in ("feed", "both"):
            events += replay.events_from_feed(req.days, req.limit)
        try:
            out = replay.start(events[:req.limit + len(req.manual)], feed_latency_s=req.feed_latency_s,
                               spread_bps=req.spread_bps, slippage_bps=req.slippage_bps)
        except (ValueError, DataError) as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return clean({**out, "events": len(events)})

    @router.post("/live/replay/stop")
    def live_replay_stop():
        need("live_news")
        return clean(replay.stop())

    # --- 2.15: saved settings, opportunities in one shape, blends, the news digest, paper trading -----------------
    from .blend import BlendError, Blender
    from .clockwork import Clockwork
    from .digest import NewsDigest
    from .hub import Hub
    from .opportunities import Runs, compact, load_prefs, save_prefs
    from .paper import PaperBook, PaperError
    digest = svc.digest if getattr(svc, "digest", None) else NewsDigest(svc, live, morning)
    svc.digest = digest
    hub = svc.hub if getattr(svc, "hub", None) else Hub(svc, morning, ideas, digest)
    svc.hub = hub
    blender = svc.blender if getattr(svc, "blender", None) else Blender(svc)
    svc.blender = blender
    paper = svc.paper if getattr(svc, "paper", None) else PaperBook(svc, hub, blender)
    svc.paper = paper
    clockwork = svc.clockwork if getattr(svc, "clockwork", None) else Clockwork(svc, paper=paper, morning=morning,
                                                                                 digest=digest, feed=live)
    svc.clockwork = clockwork
    try:
        if clockwork.needed():
            clockwork.ensure_running()               # active paper portfolios keep being valued after a restart
    except Exception:                                # noqa: BLE001 - the app starts whatever happens here
        log.exception("The timed work could not be started")

    def wake() -> None:
        try:
            if clockwork.needed():
                clockwork.ensure_running()
        except Exception:                            # noqa: BLE001
            log.exception("The timed work could not be started")

    @router.get("/prefs")
    def prefs_get():
        return clean(load_prefs(svc.db, svc.settings))

    @router.post("/prefs")
    def prefs_set(req: PrefsUpdate):
        try:
            out = save_prefs(svc.db, svc.settings, req.model_dump(exclude_none=True))
        except (ValueError, TypeError) as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        wake()
        return clean(out)

    @router.get("/opportunities/{source}")
    def opportunities_list(source: str, limit: int = 250):
        need("opportunity_blend")
        try:
            opps, meta = hub.list(source)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return clean({"source": source, "opportunities": [compact(o) for o in opps[:max(1, min(limit, 500))]], "meta": meta})

    def resolve(source: str, keys: list[str] | None, legs: list[BlendLeg] | None, top_n: int) -> list[dict[str, Any]]:
        if legs:
            return hub.from_legs([leg.model_dump() for leg in legs])
        if keys:
            found = hub.find(keys)
            if not found:
                raise ValueError("None of those opportunities is in the latest lists (they may have been rebuilt)")
            return found
        if source == "basket":
            raise ValueError("A basket needs its legs")
        opps, _ = hub.list(source)
        opps = [o for o in opps if o.get("tradable", True)]
        if not opps:
            raise ValueError({"morning": "Build the Morning plan first.", "live": "No news digest yet: listen to Live news and "
                              "build a digest.", "ideas": "Scan Trade ideas first.", "all": "No opportunities yet: build the "
                              "Morning plan, a news digest or a Trade ideas scan first."}[source])
        return opps[:top_n]

    @router.post("/blend")
    def blend_split(req: BlendRequest):
        need("opportunity_blend")
        try:
            opps = resolve(req.source, req.keys, req.legs, req.top_n)
            notes = hub.ensure_options(opps) if req.with_options and any(i in req.instruments for i in ("call", "put")) else []
            out = blender.run(opps, budget=req.budget, goal=req.goal, tail_pct=req.tail_pct, max_share=req.max_share,
                              max_positions=req.max_positions, sector_cap=req.sector_cap, instruments=list(req.instruments),
                              obey_limits=req.obey_limits if req.paper else True, horizon=req.horizon, units=req.units,
                              real_open_risk=not req.paper)
        except (BlendError, ValueError, DataError) as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        out["notes"] = list(out.get("notes") or []) + notes
        out["opportunities"] = [compact(o) for o in opps]
        out["source"] = req.source
        return clean(out)

    @router.get("/live/digest")
    def live_digest():
        need("live_news")
        return clean({"digest": digest.latest(), "interval": load_prefs(svc.db, svc.settings)["live"]["digest"]})

    @router.post("/live/digest")
    def live_digest_build(req: DigestBuild):
        need("live_news")
        choice = req.interval or load_prefs(svc.db, svc.settings)["live"]["digest"]
        try:
            return clean({"digest": digest.build(choice), "interval": choice})
        except (ValueError, DataError) as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    def paper_call(fn):
        need("paper_trading")
        try:
            return clean(fn())
        except (PaperError, BlendError, ValueError, DataError) as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @router.get("/paper")
    def paper_list():
        return paper_call(lambda: {**paper.listing(), "clockwork": clockwork.status()})

    def blend_trades(b: PaperBlendImport) -> list[dict[str, Any]]:
        opps = {o["key"]: o for o in resolve(b.source, b.keys, b.legs, 25)}
        trades = []
        for k, n in b.units.items():
            key, _, inst = k.rpartition("|")
            o = opps.get(key)
            if o is None or n < 1:
                continue
            if inst in ("call", "put") and not o.get("option"):
                hub.ensure_options([o], limit=1)
                if not o.get("option"):
                    raise ValueError(f"{o['symbol']}: its contract could not be found again")
            trades.append(PaperBook.trade_from(o, {"instrument": inst, "units": n}))
        if not trades:
            raise ValueError("None of the blend's trades could be found again (the lists may have been rebuilt)")
        return trades

    @router.post("/paper/portfolios")
    def paper_create(req: PaperCreate):
        def run():
            body = req.model_dump(exclude={"blend", "trades"})
            trades = [t.model_dump() for t in req.trades or []]
            if req.blend is not None:
                trades += blend_trades(req.blend)
            body["trades"] = trades
            out = paper.create(body)
            wake()
            return out
        return paper_call(run)

    @router.get("/paper/portfolios/{pid}")
    def paper_detail(pid: int):
        return paper_call(lambda: paper.detail(pid))

    @router.post("/paper/portfolios/{pid}/trades")
    def paper_trade(pid: int, req: PaperTrade):
        def run():
            paper.add_trade(pid, req.model_dump())
            return paper.detail(pid)
        return paper_call(run)

    @router.post("/paper/portfolios/{pid}/import")
    def paper_import(pid: int, req: PaperBlendImport):
        def run():
            errors = []
            for t in blend_trades(req):
                try:
                    paper.add_trade(pid, t)
                except (PaperError, DataError, ValueError) as exc:
                    errors.append(f"{t['symbol']}: {exc}")
            return {**paper.detail(pid), "errors": errors}
        return paper_call(run)

    @router.post("/paper/positions/{position_id}/close")
    def paper_close_position(position_id: int, req: PaperClose):
        return paper_call(lambda: paper.close_position(position_id, req.quantity, req.reason))

    @router.post("/paper/portfolios/{pid}/close")
    def paper_close(pid: int):
        return paper_call(lambda: paper.close_portfolio(pid))

    @router.post("/paper/portfolios/{pid}/mark")
    def paper_mark(pid: int):
        def run():
            from datetime import timezone as _tz
            p = paper._portfolio(pid)
            last = datetime.fromisoformat(p["last_marked_at"]) if p.get("last_marked_at") else None
            now = datetime.now(_tz.utc)
            if last is not None and (now - last).total_seconds() > 900 and paper._traded_between(last, now):
                paper.catch_up(pid, last, now)
            paper.mark(pid)
            return paper.detail(pid)
        return paper_call(run)

    @router.post("/paper/portfolios/{pid}/step")
    def paper_step(pid: int):
        def run():
            out = paper.step(pid, force=True)
            paper.mark(pid)
            return {**paper.detail(pid), "step": out}
        return paper_call(run)

    @router.post("/paper/portfolios/{pid}/settings")
    def paper_settings(pid: int, req: PaperSettings):
        return paper_call(lambda: paper.update(pid, req.model_dump(exclude_none=True)))

    @router.post("/paper/portfolios/{pid}/delete")
    def paper_delete(pid: int):
        return paper_call(lambda: paper.delete(pid))

    @router.get("/paper/runs")
    def paper_runs():
        def run():
            picks = [r["day"] for r in svc.db.query("SELECT DISTINCT day FROM morning_picks ORDER BY day DESC LIMIT 30")]
            return {"runs": Runs(svc.db).listing(), "picks_days": picks}
        return paper_call(run)

    @router.post("/paper/replay")
    def paper_replay(req: PaperReplay):
        return paper_call(lambda: paper.replay(req.model_dump()))

    # --- your trade history (2.6): imported read-only, analysed locally ---------------------------------
    from .tradehistory import HistoryImportError, TradeHistory
    history = svc.history if getattr(svc, "history", None) else TradeHistory(svc)
    svc.history = history

    def history_call(fn):
        need("trade_history")
        try:
            return clean(fn())
        except (HistoryImportError, ValueError) as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @router.post("/history/import")
    def history_import(req: HistoryImport):
        return history_call(lambda: history.import_report(req.text, req.filename))

    @router.post("/history/sample")
    def history_sample():
        return history_call(history.load_sample)

    @router.get("/history/imports")
    def history_imports():
        from .tradehistory import imports
        return history_call(lambda: {"imports": imports(svc.db)})

    @router.post("/history/imports/{import_id}/remove")
    def history_remove(import_id: int):

        return history_call(lambda: history.remove(import_id))

    @router.get("/history/analysis")
    def history_analysis(start: str | None = None, end: str | None = None, by: str = "month", basis: str = "close"):
        return history_call(lambda: history.analysis(start or None, end or None, by, basis))

    @router.post("/history/pull")
    def history_pull():
        return history_call(history.pull)

    @router.get("/history/link")
    def history_link():
        return history_call(history.link_status)

    @router.get("/history/trade/{trade_id}")
    def history_trade(trade_id: int):
        return history_call(lambda: history.trade(trade_id))

    @router.get("/history/holdings")
    def history_holdings():
        return history_call(history.holdings)

    # --- options lab (2.4) ------------------------------------------------------------------------
    from .options.lab import OptionsLab
    lab = svc.lab if getattr(svc, "lab", None) else OptionsLab(svc)
    svc.lab = lab

    @router.get("/options/{symbol}/volatility")
    def option_volatility(symbol: str, expirations: int = 8):
        need("options_lab")
        try:
            return clean(lab.volatility(symbol, expirations))
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @router.post("/options/montecarlo")
    def option_monte_carlo(req: MonteCarloRequest):
        need("options_lab")
        try:
            return clean(lab.monte_carlo(req.model_dump()))
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    # --- scanners (2.4): technical signals and options activity; read only -----------------------
    from .scanner import Scanner
    scanner = Scanner(svc, analyst)

    def _symbols(raw: str | None) -> list[str] | None:
        return [x for x in (raw or "").replace(" ", ",").split(",") if x.strip()] or None

    @router.get("/scan/technical")
    def scan_technical(universe: str = "watchlist", symbols: str | None = None, lookback: int = 5,
                       direction: str | None = None, signal: str | None = None, rsi: str | None = None,
                       min_volume_ratio: float | None = None, only_signals: bool = False):
        need("scanner")
        try:
            return clean(scanner.technical(universe_name=universe, symbols=_symbols(symbols), lookback=lookback,
                                           direction=direction, signal=signal, rsi=rsi,
                                           min_volume_ratio=min_volume_ratio, only_signals=only_signals))
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @router.get("/scan/flow")
    def scan_flow(universe: str = "watchlist", symbols: str | None = None, max_dte: int = 60, expirations: int = 3,
                  min_volume: int = 250, min_vol_oi: float = 1.0, min_premium: float = 0.0):
        need("scanner")
        if min_volume < 1 or min_vol_oi < 0 or min_premium < 0:
            raise HTTPException(status_code=400, detail="Thresholds must be positive")
        try:
            return clean(scanner.flow(universe_name=universe, symbols=_symbols(symbols), max_dte=max_dte,
                                      expirations=expirations, min_volume=min_volume, min_vol_oi=min_vol_oi,
                                      min_premium=min_premium))
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    # --- strategy builder (2.4): multi-leg trades from listed contracts; models only -------------
    from .options.strategies import Strategies
    strategies = Strategies(svc)
    svc.strategies = strategies

    @router.get("/strategies")
    def strategy_templates():
        need("strategy_builder")
        return {"templates": strategies.templates()}

    @router.post("/strategies/build")
    def strategy_build(req: StrategyBuild):
        need("strategy_builder")
        body = req.model_dump(exclude_none=True)
        try:
            return clean(strategies.build(body))
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @router.post("/strategies/parse")
    def strategy_parse(req: StrategyParse):
        need("strategy_builder")
        try:
            return clean(strategies.parse_and_build(req.text, req.symbol, getattr(svc, "strategy_fallback", None)))
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    # --- market makeup, news and the AI plug-in (2.4) -------------------------------------------
    # Anything that calls the AI model is a POST, so another web page cannot spend the key's budget
    # through the browser (the same-origin check refuses its writes).
    from .ai import AIClient, AIError
    from .briefs import Briefs
    from .makeup import MarketMakeup
    from .news import NewsClient
    ai = svc.ai if getattr(svc, "ai", None) else AIClient(svc.settings, svc.db)
    from .searchnews import for_service as search_for
    news = svc.news if getattr(svc, "news", None) else NewsClient(svc.settings, svc.db, svc.sec, search=search_for(svc))
    makeup = MarketMakeup(svc, analyst)
    briefs = Briefs(ai, svc, news, makeup, lab, analyst)
    svc.ai, svc.news, svc.makeup, svc.briefs = ai, news, makeup, briefs
    if getattr(svc, "strategy_fallback", None) is None:
        svc.strategy_fallback = briefs.read_strategy

    def ai_error(exc: AIError) -> HTTPException:
        return HTTPException(status_code=503, detail=str(exc))

    @router.get("/ai/status")
    def ai_status():
        return clean({**ai.status(), "news": news.status()})

    @router.get("/market/makeup")
    def market_makeup(breadth: str = "large_caps", refresh: bool = False):
        need("market_news")
        try:
            return clean(makeup.build(breadth, refresh))
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @router.get("/news/market")
    def market_news():
        need("market_news")
        return clean(news.market())

    @router.get("/news/{symbol}")
    def company_news(symbol: str, days: int = 7):
        need("market_news")
        return clean(news.company(svc._symbol(symbol), days))

    @router.post("/ai/market-brief")
    def ai_market_brief(req: BriefRequest):
        need("market_news")
        try:
            return clean(briefs.market(web_search=req.web_search, refresh=req.refresh))
        except AIError as exc:
            raise ai_error(exc) from exc

    @router.post("/ai/brief/{symbol}")
    def ai_symbol_brief(symbol: str, req: BriefRequest):
        need("market_news")
        try:
            return clean(briefs.symbol(symbol, web_search=req.web_search, refresh=req.refresh))
        except AIError as exc:
            raise ai_error(exc) from exc

    @router.post("/ai/tone")
    def ai_tone(req: ToneRequest):
        need("market_news")
        try:
            return clean(briefs.headline_tone(req.symbol))
        except AIError as exc:
            raise ai_error(exc) from exc

    # --- what-if analysis on the chart (2.13): a factor model, an AI scenario, a position under it --------------
    from .chatgpt import ChatGPT
    from .whatif import WhatIf
    chatgpt = svc.chatgpt if getattr(svc, "chatgpt", None) else ChatGPT(svc.settings, ai)
    whatif = svc.whatif if getattr(svc, "whatif", None) else WhatIf(svc, chart_view, chatgpt=chatgpt)
    svc.chatgpt, svc.whatif = chatgpt, whatif

    def whatif_call(fn):
        need("what_if")
        need("analyze_interactive")
        try:
            return clean(fn())
        except (ChartRequestError, ValueError) as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except AIError as exc:
            raise ai_error(exc) from exc

    def chart_kwargs(req: ChartArgs) -> dict[str, Any]:
        return {"range_key": req.range, "group": req.group, "start": req.start, "end": req.end,
                "window_start": req.window_start, "window_end": req.window_end, "min_corr": req.min_correlation}

    @router.get("/whatif/{symbol}")
    def whatif_setup(symbol: str, range: str = "6m", group: str | None = None, start: str | None = None,
                     end: str | None = None, window_start: int | None = None, window_end: int | None = None,
                     horizon: int | None = None, min_correlation: float = 0.75, peers: str = Query("", max_length=120),
                     refresh: bool = False):
        """The factors with today's readings and betas, the news, analyst calls, earnings, the pattern's record,
        and the model at its default settings."""
        return whatif_call(lambda: whatif.setup(symbol, range_key=range, group=group, start=start, end=end,
                                                window_start=window_start, window_end=window_end, horizon=horizon,
                                                min_corr=min_correlation, peers=peers.split(","), refresh=refresh))

    @router.post("/whatif/model")
    def whatif_model(req: WhatIfModel):
        """The model again with the settings in the widget. A model, nothing recorded."""
        return whatif_call(lambda: whatif.model(req.symbol, scenario=req.scenario, include=req.include, horizon=req.horizon,
                                                peers=req.peers, **chart_kwargs(req)))

    @router.post("/whatif/ask")
    def whatif_ask(req: WhatIfAsk):
        """ChatGPT's or Claude's scenario from the same inputs. Calls the AI; usage counts, nothing recorded."""
        return whatif_call(lambda: whatif.ask_ai(req.symbol, provider=req.provider, scenario=req.scenario, include=req.include,
                                                 horizon=req.horizon, peers=req.peers, **chart_kwargs(req)))

    @router.post("/whatif/position")
    def whatif_position(req: WhatIfPosition):
        """One position (shares, a short, or an option strategy) valued at the horizon under the scenario."""
        return whatif_call(lambda: whatif.position(req.symbol, kind=req.kind, quantity=req.quantity, expected_pct=req.expected_pct,
                                                   sd_pct=req.sd_pct, horizon_bars=req.horizon_bars, group=req.group))

    @router.post("/whatif/combo")
    def whatif_combo(req: WhatIfCombo):
        """A combination of positions valued together under the scenario (2.14). Models only; no order is placed."""
        return whatif_call(lambda: whatif.combo(req.symbol, legs=[l.model_dump() for l in req.legs], expected_pct=req.expected_pct,
                                                sd_pct=req.sd_pct, horizon_bars=req.horizon_bars, group=req.group))

    @router.post("/whatif/allocate")
    def whatif_allocate(req: WhatIfAllocate):
        """A budget split across strategies under the scenario (2.14): the units that maximise the expected P&L minus
        a risk weight times the worst-tenth loss. Models only; no order is placed."""
        return whatif_call(lambda: whatif.allocate(req.symbol, budget=req.budget, risk=req.risk, max_share=req.max_share, kinds=req.kinds,
                                                   expected_pct=req.expected_pct, sd_pct=req.sd_pct, horizon_bars=req.horizon_bars,
                                                   group=req.group))

    @router.post("/whatif/rank")
    def whatif_rank(req: WhatIfRank):
        """Every strategy under the scenario, ranked. Models only; no order is placed."""
        return whatif_call(lambda: whatif.rank(req.symbol, quantity=req.quantity, contracts=req.contracts, expected_pct=req.expected_pct,
                                               sd_pct=req.sd_pct, horizon_bars=req.horizon_bars, group=req.group,
                                               kinds=req.kinds, ai_pick=req.ai_pick))

    # --- portfolio risk ------------------------------------------------------------------------
    @router.get("/portfolio/risk")
    def portfolio_risk():
        need("portfolio_risk")
        return clean(analyst.portfolio())

    @router.post("/portfolio/stress")
    def portfolio_stress(req: StressRequest):
        need("portfolio_risk")
        for sc in req.scenarios:
            if sc.moves and any(not (-90 <= v <= 300) for v in sc.moves.values()):
                raise HTTPException(status_code=400, detail="Each stock move must be between -90% and +300%")
        out = analyst.portfolio([sc.model_dump() for sc in req.scenarios])
        return clean({"stress": out["stress"], "account_size": out["account_size"], "count": out["count"]})

    # --- backtest V2 ----------------------------------------------------------------------------
    @router.get("/backtest/fields")
    def backtest_fields():
        need("trend_engine_v2")
        from .backtest_v2 import CATEGORY_FIELDS, NUMERIC_FIELDS, OPS, PRESETS
        from .patterns import PATTERNS as P
        from .regime import REGIMES
        from .trend import STATES
        return {"numeric": NUMERIC_FIELDS, "categories": {"trend_state": list(STATES), "regime": list(REGIMES)},
                "category_words": CATEGORY_FIELDS, "ops": list(OPS), "presets": PRESETS,
                "patterns": {k: p.label for k, p in P.items()},
                "options_backtest": svc.settings.features.get("options_backtest", False)}

    def _bt(req: Any, fn):
        need("trend_engine_v2")
        if req.instrument == "OPTION":
            need("options_backtest")
        body = req.model_dump()
        if body.get("strategy"):
            body["strategy"] = {k: v for k, v in body["strategy"].items()}
        try:
            return clean(fn(body))
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @router.post("/backtest")
    def backtest_v2(req: BacktestV2):
        return _bt(req, analyst.backtest)

    @router.post("/backtest/walkforward")
    def backtest_walk_forward(req: WalkForwardV2):
        return _bt(req, analyst.backtest_walk_forward)

    # --- alerts (alerts only: nothing is acted on) ----------------------------------------------
    @router.get("/alerts")
    def alerts():
        need("unified_positions")
        return clean(analyst.alerts())

    @router.post("/alerts/check")
    def alerts_check():
        need("unified_positions")
        return clean(analyst.alerts())

    # --- positions ---------------------------------------------------------------------
    @router.get("/positions")
    def positions(status: str = "all"):
        need("unified_positions")
        return clean(desk.list(status))

    @router.post("/positions")
    def create_position(req: PositionCreate):
        need("unified_positions")
        body = req.model_dump()
        if body.get("option"):
            body["option"]["option_type"] = body["option"]["option_type"].upper()
        return clean(desk.create(body))

    @router.get("/positions/{position_id}")
    def position(position_id: int):
        need("unified_positions")
        return clean(desk.view(position_id))

    @router.get("/positions/{position_id}/analytics")
    def position_analytics(position_id: int):
        need("unified_positions")
        return clean(analyst.position_analytics(position_id))

    @router.get("/journal/stats")
    def journal_stats():
        need("unified_positions")
        return clean(analyst.journal_stats())

    @router.post("/positions/{position_id}/executions")
    def add_execution(position_id: int, req: ExecutionCreate):
        need("unified_positions")
        return clean(desk.add_execution(position_id, req.model_dump()))

    @router.post("/positions/{position_id}/plan")
    def update_plan(position_id: int, req: PlanUpdate):
        need("unified_positions")
        # Fields the request sent, including nulls: a null clears, a field left out is kept.
        return clean(desk.update(position_id, req.model_dump(exclude_unset=True)))

    @router.post("/positions/{position_id}/executions/{execution_id}/correct")
    def correct_execution(position_id: int, execution_id: int, req: ExecutionCorrection):
        need("unified_positions")
        changes = req.model_dump(exclude_unset=True, exclude={"reason"})
        if any(v is None for v in changes.values()):
            raise PositionError("A correction needs a value for each field it changes")
        return clean(desk.correct(position_id, execution_id, changes, req.reason))

    @router.get("/positions/{position_id}/corrections")
    def position_corrections(position_id: int):
        need("unified_positions")
        from .positions import corrections, read
        with svc.db.tx() as cur:
            read(cur, position_id)                       # 400 if there is no such position
            return clean({"position_id": position_id, "corrections": corrections(cur, position_id)})

    @router.post("/positions/{position_id}/adjustments")
    def add_adjustment(position_id: int, req: AdjustmentCreate):
        need("unified_positions")
        return clean(desk.add_adjustment(position_id, req.model_dump()))

    @router.post("/positions/{position_id}/adjustments/{adjustment_id}/void")
    def void_adjustment(position_id: int, adjustment_id: int, req: Void):
        need("unified_positions")
        return clean(desk.void_adjustment(position_id, adjustment_id, req.reason, req.confirm_reopen))

    @router.post("/positions/{position_id}/option/expire")
    def expire_option(position_id: int, req: OptionExpire):
        need("unified_positions")
        return clean(desk.expire_option(position_id, req.model_dump()))

    @router.post("/positions/{position_id}/option/exercise")
    def exercise_option(position_id: int, req: OptionExercise):
        need("unified_positions")
        return clean(desk.exercise_option(position_id, req.model_dump()))

    @router.post("/positions/{position_id}/option/adjust")
    def adjust_option(position_id: int, req: OptionAdjust):
        need("unified_positions")
        return clean(desk.adjust_option(position_id, req.model_dump()))

    @router.post("/snapshots/backfill")
    def backfill_snapshots(req: SnapshotBackfill):
        need("unified_positions")
        return clean(desk.backfill_snapshots(req.dry_run, req.upgrade_hindsight))

    @router.get("/snapshots/status")
    def snapshot_status():
        need("unified_positions")
        from .snapshots import status
        return clean(status(svc.db))

    @router.post("/positions/{position_id}/cancel")
    def cancel_position(position_id: int, req: Note):
        need("unified_positions")
        return clean(desk.cancel(position_id, req.note))

    @router.post("/positions/{position_id}/snapshot")
    def snapshot(position_id: int):
        need("unified_positions")
        return clean(desk.take_snapshot(position_id, "REVIEW"))

    return router


__all__ = ["build_router", "PositionError"]
