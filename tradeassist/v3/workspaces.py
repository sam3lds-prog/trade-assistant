"""Saved workspaces (3.0 phase 2): the workstation's layouts, the context its widgets share, and the open tabs.

A layout is the grid (each widget's place, size, type, link group and its own settings) plus the context of its
link groups (the selected stock, contract, horizon, scenario and draft proposal), so a workspace reopens as it was
left, drafts included. The session (which workspaces are open as tabs, which is in front, what the dock holds)
is kept apart, so a layout can be shared between sessions without carrying one session's tabs.

Layouts are the user's own data and say nothing the server acts on: they are validated for shape and size only.
"""

from __future__ import annotations

import json
import re
from typing import Any

from fastapi import APIRouter
from pydantic import BaseModel, ConfigDict, Field

from ..contracts import new_id, utc_now
from ..service import RequestError

WORKSPACES = ("discover", "research", "desk", "portfolio")
WIDGET_TYPE = re.compile(r"^[a-z][a-z0-9_:-]{0,48}$")
GROUPS = ("blue", "green", "amber", "plum", "none")
MAX_WIDGETS = 40
MAX_BYTES = 200_000
SESSION_KEY = "workstation:session:v1"


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)


class WidgetPlacement(Strict):
    id: str = Field(min_length=1, max_length=64, pattern=r"^[A-Za-z0-9_-]+$")
    type: str
    x: int = Field(ge=0, le=48)
    y: int = Field(ge=0, le=10_000)
    w: int = Field(ge=1, le=12)
    h: int = Field(ge=1, le=60)
    group: str = "blue"
    collapsed: bool = False
    config: dict[str, Any] = Field(default_factory=dict)


class LayoutIn(Strict):
    id: str | None = Field(default=None, max_length=64)
    workspace: str
    name: str = Field(min_length=1, max_length=60)
    widgets: list[WidgetPlacement] = Field(max_length=MAX_WIDGETS)
    context: dict[str, Any] = Field(default_factory=dict)
    is_default: bool = False


class SessionIn(Strict):
    tabs: list[dict[str, Any]] = Field(default_factory=list, max_length=12)
    active: str | None = None
    dock: dict[str, Any] = Field(default_factory=dict)
    context: dict[str, Any] = Field(default_factory=dict)     # each link group's selected stock, contract, drafts
    layouts_version: int | None = Field(default=None, ge=0, le=1000)   # 3.1: which standard layouts the session last saw


def _check(layout: LayoutIn) -> None:
    if layout.workspace not in WORKSPACES:
        raise RequestError(f"workspace must be one of {', '.join(WORKSPACES)}")
    seen = set()
    for w in layout.widgets:
        if not WIDGET_TYPE.match(w.type):
            raise RequestError(f"{w.type!r} is not a widget type")
        if w.group not in GROUPS:
            raise RequestError(f"link group must be one of {', '.join(GROUPS)}")
        if w.id in seen:
            raise RequestError(f"widget id {w.id} appears twice")
        seen.add(w.id)
    if len(json.dumps(layout.model_dump(), default=str)) > MAX_BYTES:
        raise RequestError("That layout is too large to save (more than 200 kB of settings)")


def _row(r: Any) -> dict[str, Any]:
    layout = json.loads(r["layout_json"])
    return {"id": r["id"], "workspace": r["workspace"], "name": r["name"], "widgets": layout.get("widgets", []),
            "context": json.loads(r["state_json"] or "{}"), "is_default": bool(r["is_default"]),
            "created_at": r["created_at"], "updated_at": r["updated_at"]}


class Workspaces:
    def __init__(self, svc: Any):
        self.svc = svc
        self.db = svc.db

    def listing(self) -> dict[str, Any]:
        rows = self.db.query("SELECT * FROM workspace_layouts ORDER BY workspace, is_default DESC, updated_at DESC")
        return {"layouts": [_row(r) for r in rows], "session": self.session(), "workspaces": list(WORKSPACES),
                "groups": list(GROUPS)}

    def get(self, layout_id: str) -> dict[str, Any]:
        r = self.db.one("SELECT * FROM workspace_layouts WHERE id = ?", (layout_id,))
        if r is None:
            raise RequestError(f"No saved layout {layout_id}")
        return _row(r)

    def save(self, layout: LayoutIn) -> dict[str, Any]:
        _check(layout)
        now = utc_now()
        body = json.dumps({"widgets": [w.model_dump() for w in layout.widgets]}, default=str)
        context = json.dumps(layout.context, default=str)
        with self.db.tx() as cur:
            existing = None
            if layout.id:
                existing = cur.execute("SELECT id FROM workspace_layouts WHERE id = ?", (layout.id,)).fetchone()
            if existing is None:
                existing = cur.execute("SELECT id FROM workspace_layouts WHERE workspace = ? AND name = ?",
                                       (layout.workspace, layout.name)).fetchone()
            if layout.is_default:
                cur.execute("UPDATE workspace_layouts SET is_default = 0 WHERE workspace = ?", (layout.workspace,))
            if existing is None:
                layout_id = layout.id or new_id("ws")
                cur.execute("""INSERT INTO workspace_layouts (id, workspace, name, layout_json, state_json, is_default, created_at,
                               updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                            (layout_id, layout.workspace, layout.name, body, context, int(layout.is_default), now, now))
            else:
                layout_id = existing["id"]
                cur.execute("""UPDATE workspace_layouts SET workspace = ?, name = ?, layout_json = ?, state_json = ?,
                               is_default = CASE WHEN ? THEN 1 ELSE is_default END, updated_at = ? WHERE id = ?""",
                            (layout.workspace, layout.name, body, context, int(layout.is_default), now, layout_id))
        saved = self.get(layout_id)
        self.svc.bus.publish("workspace.saved", layout_id, workspace=layout.workspace, name=layout.name)
        return saved

    def delete(self, layout_id: str) -> dict[str, Any]:
        with self.db.tx() as cur:
            cur.execute("DELETE FROM workspace_layouts WHERE id = ?", (layout_id,))
            gone = cur.rowcount
        if not gone:
            raise RequestError(f"No saved layout {layout_id}")
        self.svc.bus.publish("workspace.deleted", layout_id)
        return {"deleted": layout_id}

    def session(self) -> dict[str, Any]:
        return self.db.cache_get(SESSION_KEY) or {"tabs": [], "active": None, "dock": {}, "context": {}}

    def save_session(self, s: SessionIn) -> dict[str, Any]:
        value = s.model_dump()
        if value.get("layouts_version") is None:          # a session from before 3.1 is kept exactly as it was sent
            value.pop("layouts_version", None)
        if len(json.dumps(value, default=str)) > MAX_BYTES:
            raise RequestError("That session is too large to save")
        self.db.cache_put(SESSION_KEY, value)
        return value


def symbols(svc: Any) -> dict[str, Any]:
    """Tickers for the command palette: the watchlist, the large-company universe and the sector funds."""
    from ..sectors import SECTOR_FUNDS
    names: list[str] = list(svc.settings.watchlist)
    try:
        from ..morning import TOP_200
        names += list(TOP_200)
    except ImportError:                       # pragma: no cover - the universe moved
        pass
    names += ["SPY", "QQQ", "IWM", "DIA", *SECTOR_FUNDS]
    seen: dict[str, None] = {}
    for n in names:
        seen.setdefault(str(n).upper(), None)
    return {"symbols": list(seen), "watchlist": list(svc.settings.watchlist)}


def install(svc: Any) -> None:
    from ..api_v3 import register
    svc.workspaces = Workspaces(svc)

    def routes(router: APIRouter, svc: Any) -> None:
        ws = svc.workspaces

        @router.get("/workspaces")
        def listing() -> dict[str, Any]:
            return ws.listing()

        @router.get("/workspaces/{layout_id}")
        def one(layout_id: str) -> dict[str, Any]:
            return ws.get(layout_id)

        @router.post("/workspaces")
        def save(body: LayoutIn) -> dict[str, Any]:
            return ws.save(body)

        @router.post("/workspaces/{layout_id}/delete")
        def delete(layout_id: str) -> dict[str, Any]:
            return ws.delete(layout_id)

        @router.post("/workspaces/session")
        def session(body: SessionIn) -> dict[str, Any]:
            return ws.save_session(body)

        @router.get("/symbols")
        def syms() -> dict[str, Any]:
            return symbols(svc)

        @router.get("/quotes")
        def quotes(symbols: str) -> dict[str, Any]:
            """Last, change and freshness for up to 50 tickers (the watchlist widget)."""
            wanted = [s.strip().upper() for s in symbols.split(",") if s.strip()][:50]
            if not wanted:
                raise RequestError("Name at least one ticker")
            got = svc.market.provider.quotes(wanted)
            out = []
            for sym in wanted:
                q = got.get(sym) or {}
                last, prev = q.get("last"), q.get("prevclose")
                chg = (100 * (last / prev - 1)) if last and prev else None
                out.append({"symbol": sym, "last": last, "bid": q.get("bid"), "ask": q.get("ask"), "prevclose": prev,
                            "change_pct": chg, "description": q.get("description"), "volume": q.get("volume")})
            return {"quotes": out, "data_source": svc.market.info.mode}

    register(svc, routes)


__all__ = ["Workspaces", "LayoutIn", "SessionIn", "install", "symbols", "WORKSPACES", "GROUPS"]
