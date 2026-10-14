"""HTTP API for Trade Assistant V2, under /v2. The classic /api routes are unchanged.

Every response that carries an analysis includes the versions of the logic that produced
it (see versions.py). Areas behind a feature flag answer 404 when the flag is off.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Literal

from fastapi import APIRouter, HTTPException
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
    setup: Literal["fade", "ride", "reversal", "manual"] = "manual"
    entry: float | None = Field(None, gt=0)
    stop: float = Field(..., gt=0)
    target_1: float = Field(..., gt=0)
    target_2: float | None = Field(None, gt=0)
    horizon_days: int = Field(5, ge=1, le=60)


class WatchCreate(Strict):
    symbol: str = Field(..., min_length=1, max_length=12)
    direction: Literal["LONG", "SHORT"]
    kind: Literal["above", "below"]
    level: float = Field(..., gt=0)
    why: str = Field("", max_length=300)


class IdeasScan(Strict):
    universe: Literal["top200", "large_caps", "watchlist"] = "top200"


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
            return clean(morning.card(symbol=svc._symbol(req.symbol), direction=req.direction, entry=float(entry),
                                      stop=req.stop, target_1=req.target_1,
                                      target_2=req.target_2 if req.target_2 is not None else req.target_1,
                                      horizon_days=req.horizon_days, setup=req.setup))
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
        from . import morning_email as renderer
        st = morning.status()
        if not st.get("result"):
            raise HTTPException(status_code=400, detail="Build the morning plan first (Scan), then send it.")
        plan = st["result"]
        record_picks(svc.db, plan, "screen")
        subject, text, html = renderer.render(plan, scorecard(svc))
        try:
            to = mailer.send(svc.settings, subject, text, html)
        except mailer.MailError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return clean({"sent_to": to, "subject": subject})

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
    news = svc.news if getattr(svc, "news", None) else NewsClient(svc.settings, svc.db, svc.sec)
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
