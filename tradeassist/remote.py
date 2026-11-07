"""Phone approvals (3.0 phase 5): a few plain pages, served by the daemon on your Tailscale address only.

What it takes to approve from a phone:

1. being on your tailnet (the pages listen on REMOTE_BIND, this computer's Tailscale address, and answer only to the
   host names in REMOTE_HOSTS);
2. the PIN (REMOTE_PIN, six digits or more), which opens a session for 12 hours; five wrong PINs in 15 minutes lock
   the pages for 15 minutes and send a critical notification;
3. the link from the approval's notification: a one-time token bound to that approval, which is bound to the exact
   order (its hash). The link expires with the approval and works once.

These pages can approve or decline an order, ask for a fresh link, and switch the kill switch on (not off: that is
done on this computer). Nothing else of the app is reachable through them. No scripts run on them; the token never
leaves in a Referer header; nothing is cached.
"""

from __future__ import annotations

import hashlib
import hmac
import html
import secrets
import threading
import time
from datetime import datetime, timedelta, timezone
from typing import Any
from urllib.parse import parse_qs, urlsplit

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from starlette.middleware.trustedhost import TrustedHostMiddleware

from .optionmath import NY
from .service import RequestError

SESSION_HOURS = 12
COOKIE = "ta_remote"
MAX_FAILS = 5
LOCK_MINUTES = 15
CSP = "default-src 'none'; style-src 'unsafe-inline'; form-action 'self'; frame-ancestors 'none'; base-uri 'none'"

STYLE = """
:root { color-scheme: light dark; --ink: #17202b; --muted: #566372; --line: #d5dbe2; --bg: #f4f6f8; --card: #fff;
  --pass: #1a7046; --fail: #b3261e; --accent: #0b5c7a; }
@media (prefers-color-scheme: dark) { :root { --ink: #e7ecf1; --muted: #9aa7b4; --line: #2b3640; --bg: #0f151b; --card: #161d25;
  --pass: #4cc38a; --fail: #ff8a80; --accent: #4fa3c7; } }
body { margin: 0; font: 16px/1.45 -apple-system, system-ui, sans-serif; background: var(--bg); color: var(--ink); }
main { max-width: 34rem; margin: 0 auto; padding: 16px; }
h1 { font-size: 1.15rem; margin: 8px 0 14px; }
.card { background: var(--card); border: 1px solid var(--line); border-radius: 10px; padding: 14px; margin-bottom: 12px; }
.big { font-size: 1.25rem; font-weight: 650; margin: 0 0 6px; }
.muted { color: var(--muted); font-size: .92rem; }
ul { padding-left: 1.1rem; margin: 8px 0; } li { margin: 2px 0; }
.pass { color: var(--pass); } .fail { color: var(--fail); }
button, input { font: inherit; }
input[type=password] { width: 100%; box-sizing: border-box; padding: 12px; border: 1px solid var(--line); border-radius: 8px;
  background: var(--card); color: var(--ink); letter-spacing: .2em; }
.row { display: flex; gap: 10px; margin-top: 12px; }
.btn { flex: 1; padding: 13px 12px; border-radius: 8px; border: 1px solid var(--line); background: var(--card); color: var(--ink); font-weight: 600; }
.btn.go { background: var(--accent); border-color: var(--accent); color: #fff; }
.btn.stop { color: var(--fail); }
a { color: var(--accent); }
"""


def _page(title: str, body: str, status: int = 200) -> HTMLResponse:
    doc = (f"<!doctype html><html lang='en'><head><meta charset='utf-8'><meta name='viewport' content='width=device-width, initial-scale=1'>"
           f"<meta name='referrer' content='no-referrer'><title>{html.escape(title)}</title><style>{STYLE}</style></head>"
           f"<body><main><h1>{html.escape(title)}</h1>{body}</main></body></html>")
    return HTMLResponse(doc, status_code=status)


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def _iso(t: datetime) -> str:
    return t.astimezone(timezone.utc).isoformat(timespec="seconds")


async def _form(request: Request) -> dict[str, str]:
    raw = (await request.body())[:8192].decode("utf-8", "replace")
    return {k: v[0] for k, v in parse_qs(raw, keep_blank_values=True).items()}


class Gate:
    """The PIN and the sessions it opens."""

    def __init__(self, svc: Any):
        self.svc = svc
        self.db = svc.db
        self._fails: list[float] = []
        self._locked_until = 0.0
        self._lock = threading.Lock()

    def locked(self) -> float:
        return max(0.0, self._locked_until - time.time())

    def login(self, pin: str, label: str) -> str | None:
        with self._lock:
            if self.locked():
                return None
            if hmac.compare_digest(pin.encode(), self.svc.settings.remote_pin.encode()):
                self._fails.clear()
                token = secrets.token_urlsafe(32)
                now = datetime.now(timezone.utc)
                with self.db.tx() as cur:
                    cur.execute("INSERT INTO remote_sessions (token_hash, created_at, expires_at, last_seen_at, label) VALUES (?, ?, ?, ?, ?)",
                                (_hash(token), _iso(now), _iso(now + timedelta(hours=SESSION_HOURS)), _iso(now), label[:80]))
                    cur.execute("INSERT INTO audit_log (at, actor, action, subject, data_json) VALUES (?, 'phone', 'remote.login', ?, NULL)",
                                (_iso(now), label[:80]))
                return token
            now = time.time()
            self._fails = [t for t in self._fails if now - t < LOCK_MINUTES * 60] + [now]
            if len(self._fails) >= MAX_FAILS:
                self._locked_until = now + LOCK_MINUTES * 60
                self._fails.clear()
                desk = getattr(self.svc, "trading", None)
                if desk is not None:
                    desk.notifier.enqueue("remote.locked", "Phone approvals locked after wrong PINs",
                                          f"{MAX_FAILS} wrong PINs were entered on the approval pages ({label}). They are locked for {LOCK_MINUTES} minutes.",
                                          severity="critical")
            return None

    def session(self, request: Request) -> bool:
        token = request.cookies.get(COOKIE)
        if not token:
            return False
        r = self.db.one("SELECT expires_at, revoked_at FROM remote_sessions WHERE token_hash = ?", (_hash(token),))
        now = _iso(datetime.now(timezone.utc))
        if r is None or r["revoked_at"] or r["expires_at"] <= now:
            return False
        with self.db.tx() as cur:
            cur.execute("UPDATE remote_sessions SET last_seen_at = ? WHERE token_hash = ?", (now, _hash(token)))
        return True

    def logout(self, request: Request) -> None:
        token = request.cookies.get(COOKIE)
        if token:
            with self.db.tx() as cur:
                cur.execute("UPDATE remote_sessions SET revoked_at = ? WHERE token_hash = ?", (_iso(datetime.now(timezone.utc)), _hash(token)))


def create_remote_app(settings: Any, svc: Any) -> FastAPI:
    from . import api as main_api
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
    hosts = [h for h in [*settings.remote_hosts, settings.remote_bind] if h] + list(main_api.TEST_HOSTS)
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=hosts or ["invalid.invalid"])
    gate = Gate(svc)
    app.state.gate = gate
    secure = settings.remote_base_url.startswith("https://")

    @app.middleware("http")
    async def _guard(request: Request, call_next):
        if request.method == "POST":
            origin = request.headers.get("origin")
            if request.headers.get("sec-fetch-site") == "cross-site" or (origin is not None and urlsplit(origin).netloc != request.headers.get("host", "")):
                return _page("Refused", "<p>This request came from another page.</p>", 403)
        resp: Response = await call_next(request)
        resp.headers["Content-Security-Policy"] = CSP
        resp.headers["Referrer-Policy"] = "no-referrer"
        resp.headers["X-Content-Type-Options"] = "nosniff"
        resp.headers["Cache-Control"] = "no-store"
        resp.headers["X-Frame-Options"] = "DENY"
        return resp

    def desk() -> Any:
        return svc.trading

    def login_page(next_path: str, message: str = "", status: int = 200) -> HTMLResponse:
        locked = gate.locked()
        note = (f"<p class='fail'>Locked for {int(locked // 60) + 1} more minutes after wrong PINs.</p>" if locked else
                f"<p class='fail'>{html.escape(message)}</p>" if message else "")
        return _page("Trade Assistant approvals",
                     f"{note}<form method='post' action='/remote/login' class='card'>"
                     f"<input type='hidden' name='next' value='{html.escape(next_path)}'>"
                     "<label for='pin'>PIN</label><input id='pin' type='password' name='pin' inputmode='numeric' autocomplete='current-password' autofocus>"
                     "<div class='row'><button class='btn go' type='submit'>Sign in</button></div></form>", status)

    def need_session(request: Request) -> HTMLResponse | None:
        if gate.session(request):
            return None
        nxt = request.url.path + (f"?{request.url.query}" if request.url.query else "")
        return login_page(nxt, "", 401)

    @app.get("/remote")
    def index(request: Request):
        if (r := need_session(request)) is not None:
            return r
        d = desk()
        items = []
        for p in d.listing("awaiting_approval", 20):
            pa = p.get("pending_approval")
            ny = datetime.fromisoformat(pa["expires_at"]).astimezone(NY) if pa else None
            items.append(f"<div class='card'><p class='big'>{html.escape(p['describe'])}</p>"
                         f"<p class='muted'>{html.escape(p['account_kind'])} account; "
                         + (f"approval open until {ny:%H:%M} New York. Open it from its notification, or ask for a new link." if pa else
                            "its approval expired.") + "</p>"
                         f"<form method='post' action='/remote/p/{html.escape(p['id'])}/link'><button class='btn' type='submit'>Send me a new link</button></form></div>")
        ks = d.kill_switch()
        kill = ("<p class='fail'>The kill switch is on: nothing is sent.</p>" if ks.get("on") else
                "<form method='post' action='/remote/kill'><button class='btn stop' type='submit'>Switch the kill switch on</button></form>")
        return _page("Waiting for you", ("".join(items) or "<p class='muted'>Nothing is waiting for approval.</p>")
                     + f"<div class='card'>{kill}<p class='muted'>Switching it off is done on the computer.</p></div>"
                     "<form method='post' action='/remote/logout'><button class='btn' type='submit'>Sign out</button></form>")

    @app.post("/remote/login")
    async def login(request: Request):
        f = await _form(request)
        nxt = f.get("next") or "/remote"
        if not nxt.startswith("/remote"):
            nxt = "/remote"
        token = gate.login(f.get("pin", ""), (request.client.host if request.client else "?"))
        if token is None:
            return login_page(nxt, "That PIN is not right." if not gate.locked() else "", 401)
        resp = RedirectResponse(nxt, status_code=303)
        resp.set_cookie(COOKIE, token, max_age=SESSION_HOURS * 3600, httponly=True, samesite="strict", secure=secure, path="/remote")
        return resp

    @app.post("/remote/logout")
    def logout(request: Request):
        gate.logout(request)
        resp = RedirectResponse("/remote", status_code=303)
        resp.delete_cookie(COOKIE, path="/remote")
        return resp

    @app.get("/remote/a/{aid}")
    def approval(aid: str, request: Request, t: str = ""):
        if (r := need_session(request)) is not None:
            return r
        d = desk()
        a = d.db.one("SELECT * FROM approvals WHERE id = ?", (aid,))
        if a is None or not t or not hmac.compare_digest(_hash(t), a["token_hash"]):
            return _page("Link not valid", "<p>This approval link is not valid. Use the newest notification, or ask for a new link.</p>"
                                           "<p><a href='/remote'>What is waiting</a></p>", 404)
        p = d.get(a["proposal_id"])
        live = a["decision"] == "pending" and a["expires_at"] > _iso(datetime.now(timezone.utc)) and p["status"] == "awaiting_approval"
        risk = p.get("risk") or {}
        checks = "".join(f"<li class='{'pass' if c['status'] == 'pass' else 'fail'}'>{html.escape(c['label'])}: {html.escape(str(c['text']))}</li>"
                         for c in risk.get("checks") or [])
        ny = datetime.fromisoformat(a["expires_at"]).astimezone(NY)
        terms = p["terms"]
        cost = float(terms["quantity"]) * float(terms.get("limit_price") or 0) * (float(terms["option"]["multiplier"]) if terms.get("option") else 1)
        body = (f"<div class='card'><p class='big'>{html.escape(p['describe'])}</p>"
                f"<p>{html.escape(p['account_kind'].title())} account; ${cost:,.2f}. {html.escape(risk.get('text', ''))}</p>"
                + (f"<p class='muted'>Why: {html.escape(p['thesis'])}</p>" if p.get("thesis") else "")
                + f"<p class='muted'>Order {html.escape(p['order_hash'][:12])}; this approval {'is open until ' + f'{ny:%H:%M} New York' if live else 'is closed'}.</p>"
                f"<ul>{checks}</ul></div>")
        if live:
            body += (f"<form method='post' action='/remote/a/{html.escape(aid)}' class='card'>"
                     f"<input type='hidden' name='t' value='{html.escape(t)}'><input type='hidden' name='order_hash' value='{html.escape(p['order_hash'])}'>"
                     "<div class='row'><button class='btn' name='action' value='decline' type='submit'>Decline</button>"
                     "<button class='btn go' name='action' value='approve' type='submit'>Approve</button></div></form>")
        else:
            body += f"<p>It is {html.escape(p['status'].replace('_', ' '))}. <a href='/remote'>What is waiting</a></p>"
        return _page("Approve this order?" if live else "This approval is closed", body)

    @app.post("/remote/a/{aid}")
    async def decide(aid: str, request: Request):
        if (r := need_session(request)) is not None:
            return r
        f = await _form(request)
        try:
            p = desk().decide(aid, f.get("action") == "approve", f.get("order_hash", ""), actor="phone", channel="remote", token=f.get("t", ""))
        except RequestError as exc:
            return _page("Not done", f"<p class='fail'>{html.escape(str(exc))}</p><p><a href='/remote'>What is waiting</a></p>", 409)
        word = {"authorized": "Approved: it will be sent.", "cancelled": "Declined.", "blocked": "Approved, but a risk check now fails, so it is blocked."}
        return _page(word.get(p["status"], p["status"]), f"<div class='card'><p class='big'>{html.escape(p['describe'])}</p>"
                     f"<p>{html.escape((p.get('risk') or {}).get('text', ''))}</p></div><p><a href='/remote'>What is waiting</a></p>")

    @app.post("/remote/p/{pid}/link")
    def new_link(pid: str, request: Request):
        if (r := need_session(request)) is not None:
            return r
        d = desk()
        try:
            p = d.get(pid)
            if p["status"] != "awaiting_approval":
                raise RequestError(f"It is {p['status'].replace('_', ' ')}, not waiting for approval.")
            d._ask_approval(pid)
        except RequestError as exc:
            return _page("Not done", f"<p class='fail'>{html.escape(str(exc))}</p><p><a href='/remote'>What is waiting</a></p>", 409)
        return _page("A new link is on its way", "<p>Open it from the notification.</p><p><a href='/remote'>What is waiting</a></p>")

    @app.post("/remote/kill")
    def kill(request: Request):
        if (r := need_session(request)) is not None:
            return r
        desk().set_kill_switch(True, "switched on from the phone", actor="phone")
        return _page("Kill switch on", "<p>Nothing will be sent, and working orders are being cancelled. Switch it off on the computer.</p>"
                                       "<p><a href='/remote'>What is waiting</a></p>")

    @app.get("/")
    def root():
        return RedirectResponse("/remote", status_code=303)

    return app


def serve_in_thread(settings: Any, svc: Any) -> threading.Thread:
    """Start the approval pages on REMOTE_BIND:REMOTE_PORT in a thread of the daemon."""
    import uvicorn
    config = uvicorn.Config(create_remote_app(settings, svc), host=settings.remote_bind, port=settings.remote_port, log_level="warning",
                            proxy_headers=False, server_header=False)
    server = uvicorn.Server(config)
    t = threading.Thread(target=server.run, name="remote-approvals", daemon=True)
    t.start()
    return t


__all__ = ["create_remote_app", "serve_in_thread", "Gate", "COOKIE"]
