"""HTTP API and static UI. Binds to localhost; there is no endpoint that places an order."""

from __future__ import annotations

import logging
import sqlite3
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from starlette.middleware.trustedhost import TrustedHostMiddleware
from pydantic import BaseModel, ConfigDict, Field

from . import __version__
from .config import Settings, load_settings
from .events import emit, redact
from .journal import JournalError
from .positions import PositionError
from .providers.base import DataError
from .service import RequestError, TradeAssistant, clean

log = logging.getLogger(__name__)
STATIC = Path(__file__).parent / "static"


class Strict(BaseModel):
    """Request bodies: numbers must be finite (no NaN or infinity)."""
    model_config = ConfigDict(allow_inf_nan=False)


class PriceRequest(Strict):
    symbol: str
    expiration: str
    option_type: str
    strike: float
    side: str = "buy"
    contracts: int = Field(1, ge=1, le=1000)
    earnings_date: str | None = None
    limit_price: float | None = None


class SimLeg(Strict):
    expiration: str
    option_type: str
    strike: float
    side: str = "buy"
    contracts: int = Field(1, ge=1, le=1000)
    entry_price: float | None = Field(None, gt=0)


class SimulateRequest(Strict):
    symbol: str
    legs: list[SimLeg] = Field(min_length=1, max_length=4)
    iv_shift: float = Field(0.0, ge=-50, le=100)
    eval_date: str | None = None
    range_pct: float | None = Field(None, ge=2, le=90)


class LiveRequest(Strict):
    symbol: str
    expiration: str
    option_type: str
    strike: float
    side: str = "buy"
    contracts: int = Field(1, ge=1, le=1000)


class TicketRequest(Strict):
    symbol: str
    expiration: str
    option_type: str
    strike: float
    contracts: int = Field(1, ge=1, le=1000)
    limit_price: float
    max_limit: float | None = None
    thesis: str = ""
    earnings_date: str | None = None
    profit_target_pct: float = 50.0
    stop_loss_pct: float = 50.0
    exit_days_before_expiry: int = 7
    overrides: list[str] = []
    save: bool = False


class StudyRequest(Strict):
    symbols: list[str] | None = None
    years: float = Field(5.0, ge=1.5, le=20)
    horizons: list[int] | None = None


class BacktestRequest(Strict):
    symbols: list[str] | None = None
    years: float = Field(5.0, ge=1.5, le=20)
    params: dict[str, Any] = {}
    n_trials: int = Field(1, ge=1, le=100000)
    train_days: int = 252
    test_days: int = 63


class AttemptRequest(Strict):
    side: str = "buy"
    bid: float
    ask: float
    limit_price: float
    filled: bool
    seconds_to_fill: float | None = None


class CloseRequest(Strict):
    exit_price: float = Field(..., ge=0)
    note: str = ""


class NoteRequest(Strict):
    note: str = ""


class SnapshotRequest(Strict):
    symbols: list[str] | None = None


# Host names the app answers to: itself on this computer ("testserver" is the test client's).
ALLOWED_HOSTS = ("127.0.0.1", "localhost", "::1", "[::1]", "testserver")


def create_app(settings: Settings | None = None, assistant: TradeAssistant | None = None) -> FastAPI:
    settings = settings or load_settings()
    svc = assistant or TradeAssistant(settings)
    app = FastAPI(title="Trade Assistant", docs_url="/api/docs", openapi_url="/api/openapi.json")
    app.state.assistant = svc

    # The app listens on 127.0.0.1 only. A web page elsewhere can still try to reach it through the
    # browser: by pointing its own host name at 127.0.0.1 (DNS rebinding), refused by checking the Host
    # header, or by sending a request from its own page, refused for anything that changes data by
    # checking the Origin the browser attaches.
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=list(ALLOWED_HOSTS))

    @app.middleware("http")
    async def _same_origin_writes(request: Request, call_next):
        if request.method not in ("GET", "HEAD", "OPTIONS"):
            origin = request.headers.get("origin")
            site = request.headers.get("sec-fetch-site")
            foreign = site == "cross-site" or (origin is not None and (
                origin == "null" or urlsplit(origin).netloc != request.headers.get("host", "")))
            if foreign:
                emit("cross_site_refused", logging.WARNING, path=request.url.path, origin=origin)
                return JSONResponse(status_code=403, content={"error": "Refused: this request came from another web "
                                                                       "page. Use the app's own page."})
        return await call_next(request)

    @app.exception_handler(RequestValidationError)
    async def _invalid(_: Request, exc: RequestValidationError):
        # Build the message without echoing the input back: a NaN in the request cannot be
        # written into a JSON reply.
        problems = [f"{'.'.join(str(x) for x in e.get('loc', [])[1:]) or 'request'}: {e.get('msg', 'invalid')}"
                    for e in exc.errors()]
        return JSONResponse(status_code=422, content={"error": "; ".join(problems)})

    @app.exception_handler(RequestError)
    async def _bad_request(_: Request, exc: RequestError):
        return JSONResponse(status_code=400, content={"error": str(exc)})

    @app.exception_handler(JournalError)
    async def _journal_error(_: Request, exc: JournalError):
        return JSONResponse(status_code=400, content={"error": str(exc)})

    @app.exception_handler(PositionError)
    async def _position_error(request: Request, exc: PositionError):
        # Expected: the user asked for something the records do not allow. Logged without a stack trace.
        emit("invalid_transition", path=request.url.path, error=exc)
        return JSONResponse(status_code=400, content={"error": str(exc)})

    @app.exception_handler(sqlite3.IntegrityError)
    async def _integrity(_: Request, exc: sqlite3.IntegrityError):
        emit("integrity_refused", logging.WARNING, error=exc)
        return JSONResponse(status_code=409, content={"error": "That would break a rule the database keeps (" + str(exc) +
                                                               "). Nothing was saved. Check the values, or the record "
                                                               "it refers to."})

    @app.exception_handler(sqlite3.OperationalError)
    async def _db_busy(_: Request, exc: sqlite3.OperationalError):
        emit("database_error", logging.WARNING, error=exc)
        if "locked" in str(exc).lower() or "busy" in str(exc).lower():
            return JSONResponse(status_code=503, content={"error": "The database is busy with another request. Nothing was "
                                                                   "saved; try again in a moment."})
        return JSONResponse(status_code=500, content={"error": f"The database could not complete that ({exc}). Nothing was "
                                                               "saved. The data health screen can check the file."})

    @app.exception_handler(HTTPException)
    async def _http_error(_: Request, exc: HTTPException):
        return JSONResponse(status_code=exc.status_code, content={"error": str(exc.detail)})

    from .api_v2 import build_router
    app.include_router(build_router(svc))

    @app.exception_handler(DataError)
    async def _data_error(_: Request, exc: DataError):
        return JSONResponse(status_code=502, content={"error": redact(str(exc))})

    @app.exception_handler(Exception)
    async def _unexpected(_: Request, exc: Exception):
        log.exception("Unhandled error")
        return JSONResponse(status_code=500, content={"error": f"Unexpected error: {exc.__class__.__name__}. "
                                                               "See the server log for details."})

    @app.get("/api/status")
    def status():
        return clean(svc.status())

    @app.get("/api/expirations")
    def expirations(symbol: str):
        return clean(svc.expirations(symbol))

    @app.get("/api/chain")
    def chain(symbol: str, expiration: str, width_pct: float = 15.0):
        return clean(svc.chain(symbol, expiration, width_pct))

    @app.post("/api/price")
    def price(req: PriceRequest):
        return svc.price(**req.model_dump())

    @app.post("/api/live")
    def live(req: LiveRequest):
        return svc.live(**req.model_dump())

    @app.post("/api/ticket")
    def ticket(req: TicketRequest):
        return svc.ticket(**req.model_dump())

    @app.get("/api/research/{symbol}")
    def research(symbol: str):
        return svc.research(symbol)

    @app.get("/api/regime")
    def regime():
        return clean(svc.regime())

    @app.get("/api/patterns")
    def patterns():
        return {"patterns": svc.patterns(), "last_run": svc.last_pattern_run()}

    @app.post("/api/patterns/run")
    def run_patterns(req: StudyRequest):
        return svc.run_patterns(req.symbols, req.years, req.horizons)

    @app.get("/api/technicals/{symbol}")
    def technicals(symbol: str, bars: int = 504, swing_atr: float = 2.0):
        return svc.technicals(symbol, bars, swing_atr)

    @app.post("/api/simulate")
    def simulate(req: SimulateRequest):
        return svc.simulate(symbol=req.symbol, legs=[leg.model_dump() for leg in req.legs], iv_shift=req.iv_shift,
                            eval_date=req.eval_date, range_pct=req.range_pct)

    @app.get("/api/chart/{symbol}")
    def chart(symbol: str, pattern: str | None = None):
        return svc.chart(symbol, pattern)

    @app.post("/api/backtest")
    def backtest(req: BacktestRequest):
        return svc.backtest(req.symbols, req.years, req.params, req.n_trials)

    @app.post("/api/walkforward")
    def walkforward(req: BacktestRequest):
        return svc.walk_forward(req.symbols, req.years, req.params, req.train_days, req.test_days)

    @app.post("/api/snapshot")
    def snapshot(req: SnapshotRequest):
        return svc.snapshot(req.symbols)

    @app.get("/api/journal")
    def journal():
        return clean({"trades": svc.journal.list(), "stats": svc.journal.stats(),
                      "calibration": svc.journal.calibration("buy")})

    @app.get("/api/journal/{trade_id}")
    def journal_entry(trade_id: int):
        return clean(svc.journal.get(trade_id))

    @app.post("/api/journal/{trade_id}/attempt")
    def journal_attempt(trade_id: int, req: AttemptRequest):
        return clean(svc.journal.log_attempt(trade_id, **req.model_dump()))

    @app.post("/api/journal/{trade_id}/close")
    def journal_close(trade_id: int, req: CloseRequest):
        return clean(svc.journal.close_at(trade_id, req.exit_price, req.note))

    @app.post("/api/journal/{trade_id}/cancel")
    def journal_cancel(trade_id: int, req: NoteRequest):
        return clean(svc.journal.cancel(trade_id, req.note))

    # The browser must never run an old copy of the page against a newer app: the page is sent
    # with "check with the server first", and its script and style addresses change whenever
    # those files do, so an update shows up on an ordinary reload.
    fresh = {"Cache-Control": "no-cache"}

    @app.get("/")
    def index():
        html = (STATIC / "index.html").read_text()
        for name in ("app.css", "app.js", "v2.js", "lab.js"):
            stamp = f"{__version__}-{int((STATIC / name).stat().st_mtime)}"
            html = html.replace(f'"/{name}"', f'"/{name}?v={stamp}"')
        return HTMLResponse(html, headers=fresh)

    @app.get("/{name}")
    def static_file(name: str):
        path = (STATIC / name).resolve()
        if path.parent != STATIC.resolve() or not path.is_file():
            raise HTTPException(status_code=404, detail="Not found")
        return FileResponse(path, headers=fresh)

    return app
