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

    def trend_for_snapshot(symbol: str, values: dict[str, Any]) -> dict[str, Any]:
        return analyst.snapshot_extra(symbol, values) if svc.settings.features.get("trend_engine_v2") else {}
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
