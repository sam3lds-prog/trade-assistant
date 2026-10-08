"""HTTP API for Trade Assistant V2, under /v2. The classic /api routes are unchanged.

Every response that carries an analysis includes the versions of the logic that produced
it (see versions.py). Areas behind a feature flag answer 404 when the flag is off.
"""

from __future__ import annotations

from typing import Any, Literal

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, ConfigDict, Field

from .positions import PositionError


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


class PlanUpdate(Strict):
    thesis: str | None = None
    setup_type: str | None = None
    intended_horizon: int | None = Field(None, ge=1, le=1000)
    invalidation_price: float | None = Field(None, gt=0)
    target_1: float | None = Field(None, gt=0)
    target_2: float | None = Field(None, gt=0)
    risk_amount: float | None = Field(None, gt=0)
    notes: str | None = None


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


class ContractRef(Strict):
    option_type: Literal["CALL", "PUT", "call", "put"]
    strike: float = Field(..., gt=0)
    expiration: str


class OptionsCompare(OptionsEvaluate):
    contracts_to_compare: list[ContractRef] = Field(..., min_length=1, max_length=8)


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
        return clean(assess(sym, bars, svc.market.bar_info(sym)))

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

    @router.post("/reversal/options")
    def reversal_options(req: ReversalOptions):
        need("reversal_analysis")
        try:
            return clean(analyst.reversal_options(req.model_dump()))
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

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
        return clean(desk.update(position_id, {k: v for k, v in req.model_dump().items() if v is not None}))

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
