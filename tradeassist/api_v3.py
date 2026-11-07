"""The 3.0 routes, under /v3. The 2.x routes under /api and /v2 stay as they are while the workstation replaces the
screens that use them; nothing here changes their behaviour.

Each 3.0 area registers its own routes through `extend(router, svc)` in its module, so this file only wires
them: the event stream and the contracts here, the workspaces, research studies, the opportunity feed and the
Trading Desk in theirs.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Query

from . import __version__
from .bus import bus_for
from .contracts import CONTRACT_VERSION, schemas


def build_router_v3(svc: Any) -> APIRouter:
    router = APIRouter(prefix="/v3")
    bus = bus_for(svc)

    @router.get("/contracts")
    def contracts() -> dict[str, Any]:
        return schemas()

    @router.get("/events")
    def events(after: int | None = Query(None, ge=0), topics: str = "",
               wait: float = Query(0.0, ge=0, le=25)) -> dict[str, Any]:
        """What changed after event `after`. With `wait`, the reply is held up to that many seconds for an event.
        Without `after`, only the latest id: where a new reader starts."""
        wanted = [t.strip() for t in topics.split(",") if t.strip()] or None
        if after is None:
            return {"events": [], "latest": bus.latest_id()}
        got = bus.wait(after, wanted, wait) if wait else bus.since(after, wanted)
        return {"events": got, "latest": got[-1]["id"] if got else max(after, bus.latest_id() if not wanted else after)}

    @router.get("/status")
    def status() -> dict[str, Any]:
        info = svc.market.info
        out: dict[str, Any] = {"version": __version__, "contract_version": CONTRACT_VERSION,
                               "data": {"mode": info.mode, "label": info.label, "realtime": info.realtime},
                               "schema_version": svc.db.schema_version(), "latest_event": bus.latest_id()}
        for name, provider in getattr(svc, "status_parts", {}).items():
            try:
                out[name] = provider()
            except Exception as exc:  # noqa: BLE001 - one part failing must not hide the rest
                out[name] = {"error": str(exc)[:200]}
        return out

    for extend in getattr(svc, "v3_extensions", []):
        extend(router, svc)
    return router


def register(svc: Any, extend) -> None:
    """Add an area's routes (called before the app is built)."""
    if not hasattr(svc, "v3_extensions"):
        svc.v3_extensions = []
    svc.v3_extensions.append(extend)


def status_part(svc: Any, name: str, provider) -> None:
    """Add a section to /v3/status (the workstation's status bar reads it)."""
    if not hasattr(svc, "status_parts"):
        svc.status_parts = {}
    svc.status_parts[name] = provider


__all__ = ["build_router_v3", "register", "status_part"]
