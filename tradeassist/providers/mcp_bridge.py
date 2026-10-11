"""A small synchronous bridge to an MCP server, restricted to a fixed list of read-only tools.

Robinhood exposes its market data to outside programs through a Model Context
Protocol (MCP) server. The same server also has tools that place and cancel
orders and tools that read the account. This bridge is the only place in the app
that talks to that server, and it will only call a tool whose exact name is on
the list it was built with. The list is fixed in code (see providers/robinhood.py)
and is checked when the bridge is created, so a name that looks like anything
other than a plain read cannot be put on it. A tool the server itself marks as
destructive, or as not read-only, is refused even if its name is on the list.

The MCP library is asynchronous and the rest of the app is not, so the bridge
keeps one connection open on a background thread and hands calls across to it.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import tempfile
import threading
import time
import webbrowser
from contextlib import AbstractAsyncContextManager, suppress
from pathlib import Path
from typing import Any, Callable
from urllib.parse import parse_qs, urlparse

from .base import DataError

log = logging.getLogger(__name__)

# A name may be put on a bridge's list only if it starts with this ...
READ_PREFIX = "get_"
# ... and contains none of these, in any letter case. This guards the list itself against a
# careless edit; which tools are callable is decided by the exact names on the list.
FORBIDDEN_FRAGMENTS = ("place", "cancel", "review", "order", "approv", "decline", "transfer", "withdraw",
                       "deposit", "add", "remove", "create", "update", "delete", "follow", "reset", "set_",
                       "submit", "buy", "sell", "trade", "exercise", "execute", "close", "send", "and_", "or_")
# ... nor any of these: tools about the account only read, but they are not market data.
ACCOUNT_FRAGMENTS = ("account", "portfolio", "position", "holding", "held", "balance", "buying", "cash", "pnl",
                     "watchlist", "margin", "alert", "user", "profile", "document", "statement", "tax", "bank",
                     "card", "activity", "transaction", "dividend", "fill", "_me", "my_")

# Account reads (2.6, only when ROBINHOOD_ACCOUNT_READ=1): a separate, exact list. A name on it must still
# start with get_, contain none of the action words above, and may mention orders only as a list it reads
# (ending in _orders). The account words are allowed here, and only here.
ACTION_FRAGMENTS = tuple(f for f in FORBIDDEN_FRAGMENTS if f != "order")

CALL_TIMEOUT = 20.0
SIGN_IN_TIMEOUT = 300.0


class SignInRequired(DataError):
    """The stored sign-in is missing or no longer valid and a person has to sign in again."""


def acceptable_tool_name(name: str) -> bool:
    """Whether a name may appear on a bridge's list of callable tools at all."""
    lowered = name.lower()
    return (name == lowered and lowered.startswith(READ_PREFIX)
            and re.fullmatch(r"[a-z0-9_]+", lowered) is not None
            and not any(fragment in lowered for fragment in FORBIDDEN_FRAGMENTS)
            and not any(fragment in lowered for fragment in ACCOUNT_FRAGMENTS))


ACCOUNT_READ_NAME = re.compile(r"get_(equity|stock|option)_(positions|orders)")


def acceptable_account_tool_name(name: str) -> bool:
    """Whether a name may be on the account-read list at all: only a read of share or option positions or of
    past share or option orders, by one of these exact shapes. Account numbers, balances, buying power, bank
    links, documents and anything that acts never fit."""
    return ACCOUNT_READ_NAME.fullmatch(name) is not None and not any(f in name for f in ACTION_FRAGMENTS)


def is_read_only(name: str, annotations: Any = None, allowed: frozenset[str] = frozenset()) -> bool:
    """True only for a tool whose exact name is in `allowed` and which the server does not mark as
    destructive or as not read-only."""
    if name not in allowed or not acceptable_tool_name(name):
        return False
    if annotations is not None:
        if getattr(annotations, "destructive_hint", None) is True:
            return False
        if getattr(annotations, "read_only_hint", None) is False:
            return False
    return True


def parse_tool_result(result: Any) -> Any:
    """The payload of a tool reply as plain data. Raises DataError for a tool-reported error."""
    texts = [getattr(part, "text", None) for part in (getattr(result, "content", None) or [])]
    text = next((t for t in texts if isinstance(t, str) and t.strip()), None)
    if getattr(result, "is_error", False):
        raise DataError(f"The data source reported an error: {(text or 'no details')[:300]}")
    payload: Any = None
    if text is not None:
        try:
            payload = json.loads(text)
        except ValueError:
            payload = None
    if payload is None:
        structured = getattr(result, "structured_content", None)
        if isinstance(structured, dict) and set(structured) == {"result"} and isinstance(structured["result"], str):
            try:
                structured = json.loads(structured["result"])
            except ValueError:
                pass
        payload = structured
    if payload is None:
        raise DataError("The data source sent a reply this app could not read")
    return payload


class _Session:
    """One connection: its event loop, its thread and the connected client."""

    def __init__(self) -> None:
        self.loop = asyncio.new_event_loop()
        self.thread: threading.Thread | None = None
        self.client: Any = None
        self.stop: asyncio.Event | None = None
        self.ready = threading.Event()
        self.error: BaseException | None = None

    def alive(self) -> bool:
        return self.client is not None and self.thread is not None and self.thread.is_alive()

    def abandon(self) -> None:
        """Stop this connection without waiting for it."""
        def cancel_everything() -> None:
            if self.stop is not None:
                self.stop.set()
            for task in asyncio.all_tasks(self.loop):
                task.cancel()
        with suppress(RuntimeError):          # the loop has already closed
            self.loop.call_soon_threadsafe(cancel_everything)
        self.client = None


class ReadOnlyBridge:
    """Keeps one MCP connection open and exposes `tools()` and `call()` to ordinary code."""

    def __init__(self, connect: Callable[[], AbstractAsyncContextManager[Any]], allowed: frozenset[str],
                 label: str = "the data source", first_call_timeout: float = CALL_TIMEOUT,
                 call_timeout: float = CALL_TIMEOUT, account_reads: frozenset[str] = frozenset()):
        """`connect` returns an async context manager that yields a connected MCP client.
        `allowed` is the complete list of market-data tool names this bridge will ever call;
        `account_reads` (empty unless the person switched account reads on) the complete list of
        account tools it may read, each a plain read of holdings or past orders."""
        bad = sorted(name for name in allowed if not acceptable_tool_name(name))
        bad += sorted(name for name in account_reads if not acceptable_account_tool_name(name))
        if bad:
            raise ValueError(f"These tool names may not be called by a read-only bridge: {', '.join(bad)}")
        self._account = frozenset(account_reads)
        self._allowed = frozenset(allowed) | self._account
        self._connect = connect
        self.label = label
        self._first_call_timeout = first_call_timeout
        self._call_timeout = call_timeout
        self._lock = threading.RLock()
        self._session: _Session | None = None
        self._tools: dict[str, Any] | None = None
        self.calls: list[str] = []        # names of tools called, newest last (for diagnostics and tests)

    @property
    def allowed(self) -> frozenset[str]:
        """The complete list of tools this bridge may call. Fixed when the bridge is built."""
        return self._allowed

    # --- connection ------------------------------------------------------
    async def _run(self, session: _Session) -> None:
        try:
            async with self._connect() as client:
                session.stop = asyncio.Event()
                session.client = client
                session.ready.set()
                await session.stop.wait()
        except BaseException as exc:      # noqa: BLE001 - reported to the calling thread
            session.error = exc
        finally:
            session.client = None
            session.ready.set()

    def _start(self) -> _Session:
        session = _Session()
        self._tools = None

        def runner() -> None:
            asyncio.set_event_loop(session.loop)
            try:
                session.loop.run_until_complete(self._run(session))
            finally:
                session.loop.close()

        session.thread = threading.Thread(target=runner, name="mcp-bridge", daemon=True)
        session.thread.start()
        if not session.ready.wait(self._first_call_timeout + 5):
            session.abandon()
            raise DataError(f"Timed out connecting to {self.label}")
        if session.client is None:
            raise self._translate(session.error)
        self._session = session
        return session

    def _translate(self, exc: BaseException | None) -> DataError:
        nested = _flatten(exc)
        for inner in nested:
            if isinstance(inner, DataError):
                return inner
        # The most specific failure is the innermost one; exception groups say nothing themselves.
        leaf = next((e for e in reversed(nested) if not getattr(e, "exceptions", None)), exc)
        detail = f"{leaf.__class__.__name__}: {leaf}" if leaf else "unknown error"
        return DataError(f"Could not connect to {self.label} ({detail[:200]})")

    def close(self) -> None:
        with self._lock:
            session, self._session = self._session, None
            if session is None:
                return
            thread = session.thread
            if session.stop is not None and thread is not None and thread.is_alive():
                with suppress(RuntimeError):
                    session.loop.call_soon_threadsafe(session.stop.set)
                thread.join(timeout=10)
                if thread.is_alive():
                    session.abandon()
            session.client = None

    def _submit(self, make_coro: Callable[[Any], Any], timeout: float) -> Any:
        """Run one coroutine against the client.

        A request is repeated once only when it failed on a connection that was already open (it
        may simply have gone stale). A request that gets no answer in time, or that the server
        answers with an error, is not repeated."""
        from mcp import MCPError, types
        dropped = (types.CONNECTION_CLOSED, types.REQUEST_TIMEOUT)
        with self._lock:
            for attempt in (1, 2):
                session = self._session
                reused = session is not None and session.alive()
                if not reused:
                    session = self._start()
                assert session is not None
                future = None
                try:
                    future = asyncio.run_coroutine_threadsafe(make_coro(session.client), session.loop)
                    return future.result(timeout)
                except Exception as exc:          # noqa: BLE001 - network and protocol failures of any kind
                    if future is not None and not future.done():         # no answer in time
                        future.cancel()
                        session.abandon()
                        self._session = None
                        raise DataError(f"{self.label} did not answer within {timeout:.0f} seconds") from exc
                    nested = _flatten(exc)
                    known = next((e for e in nested if isinstance(e, DataError)), None)
                    if known is not None:
                        raise known from exc
                    refused = next((e for e in nested if isinstance(e, MCPError)), None)
                    if refused is not None and getattr(refused, "code", None) not in dropped:
                        raise DataError(f"{self.label} did not accept the request: {str(refused)[:300]}") from exc
                    self.close()
                    if attempt == 2 or not reused:
                        raise self._translate(exc) from exc
                    log.warning("%s call failed on an open connection, reconnecting once: %s", self.label, exc)
        raise DataError(f"Could not reach {self.label}")      # not reached

    # --- tools -----------------------------------------------------------
    def tools(self) -> dict[str, Any]:
        """Every tool the server offers, keyed by name."""
        with self._lock:
            if self._tools is None or self._session is None or not self._session.alive():
                async def fetch(client: Any) -> list[Any]:
                    out, cursor = [], None
                    for _ in range(20):
                        page = await client.list_tools(cursor=cursor) if cursor else await client.list_tools()
                        out.extend(page.tools)
                        cursor = getattr(page, "next_cursor", None)
                        if not cursor:
                            break
                    return out
                listed = self._submit(fetch, self._first_call_timeout)
                self._tools = {t.name: t for t in listed}
            return self._tools

    def _permitted(self, name: str, annotations: Any) -> bool:
        if name in self._account:
            if not acceptable_account_tool_name(name):
                return False
            if annotations is not None and (getattr(annotations, "destructive_hint", None) is True
                                            or getattr(annotations, "read_only_hint", None) is False):
                return False
            return True
        return is_read_only(name, annotations, self.allowed)

    @property
    def account_reads(self) -> frozenset[str]:
        return self._account

    def may_call(self, name: str) -> bool:
        tool = self.tools().get(name)
        return tool is not None and self._permitted(name, getattr(tool, "annotations", None))

    def call(self, name: str, arguments: dict[str, Any] | None = None) -> Any:
        """Call one listed read-only tool and return its payload. Anything else is refused."""
        if name not in self.allowed:
            raise PermissionError(f"Refusing to call {name!r}: it is not on this app's list of tools to read. "
                                  "This app only reads data and never calls tools that can change an account.")
        tool = self.tools().get(name)
        if tool is None:
            raise DataError(f"{self.label} has no tool named {name}")
        if not self._permitted(name, getattr(tool, "annotations", None)):
            raise PermissionError(f"Refusing to call {name}: the server does not describe it as a read-only tool.")
        self.calls.append(name)
        del self.calls[:-500]
        result = self._submit(lambda client: client.call_tool(name, arguments or {}), self._call_timeout)
        return parse_tool_result(result)


def _flatten(exc: BaseException | None) -> list[BaseException]:
    """An exception with everything nested inside it (exception groups and causes)."""
    if exc is None:
        return []
    out = [exc]
    for inner in getattr(exc, "exceptions", ()) or ():
        out.extend(_flatten(inner))
    if exc.__cause__ is not None:
        out.extend(_flatten(exc.__cause__))
    return out


# --- signing in ---------------------------------------------------------------

class FileTokenStorage:
    """Keeps the sign-in tokens in a local file readable only by the current user."""

    def __init__(self, path: Path, redirect_uri: str | None = None):
        """`redirect_uri` is the address this app currently listens on for the sign-in return. A
        stored registration made for a different address is ignored, so that changing the
        callback port leads to a fresh registration instead of a refused sign-in."""
        self.path = Path(path)
        self.redirect_uri = redirect_uri

    def _read(self) -> dict[str, Any]:
        try:
            data = json.loads(self.path.read_text())
        except (OSError, ValueError):
            return {}
        return data if isinstance(data, dict) else {}

    def _write(self, data: dict[str, Any]) -> None:
        """Replace the file in one step. The new copy is private from the moment it is created."""
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        fd, tmp = tempfile.mkstemp(dir=self.path.parent, prefix=self.path.stem + ".", suffix=".part")
        try:
            with suppress(OSError, AttributeError):      # not available on Windows
                os.fchmod(fd, 0o600)
            with os.fdopen(fd, "w") as handle:
                json.dump(data, handle)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tmp, self.path)
        except BaseException:
            with suppress(OSError):
                os.unlink(tmp)
            raise

    def exists(self) -> bool:
        return bool(self._read().get("tokens"))

    def scope(self) -> str | None:
        """What Robinhood says the stored sign-in permits, in its own words."""
        tokens = self._read().get("tokens") or {}
        scope = tokens.get("scope")
        return scope if isinstance(scope, str) and scope.strip() else None

    def expires_at(self) -> float | None:
        value = self._read().get("tokens_expire_at")
        return float(value) if isinstance(value, (int, float)) else None

    def renew_at(self) -> float | None:
        """When to renew: shortly before the end, but never so early that a short sign-in looks expired at once."""
        end, lifetime = self.expires_at(), self._read().get("tokens_lifetime")
        if end is None:
            return None
        margin = min(30.0, float(lifetime) / 2) if isinstance(lifetime, (int, float)) and lifetime > 0 else 30.0
        return end - margin

    def clear(self) -> None:
        with suppress(FileNotFoundError):
            self.path.unlink()
        for leftover in self.path.parent.glob(self.path.stem + ".*.part"):
            with suppress(OSError):
                leftover.unlink()

    async def get_tokens(self):
        from mcp.shared.auth import OAuthToken
        raw = self._read().get("tokens")
        return OAuthToken.model_validate(raw) if raw else None

    async def set_tokens(self, tokens) -> None:
        data = self._read()
        data["tokens"] = tokens.model_dump(mode="json", exclude_none=True)
        lifetime = getattr(tokens, "expires_in", None)
        data["tokens_expire_at"] = time.time() + float(lifetime) if lifetime else None
        data["tokens_lifetime"] = float(lifetime) if lifetime else None
        self._write(data)

    async def get_client_info(self):
        from mcp.shared.auth import OAuthClientInformationFull
        raw = self._read().get("client")
        if not raw:
            return None
        if self.redirect_uri and self.redirect_uri not in [str(u) for u in raw.get("redirect_uris") or []]:
            return None
        return OAuthClientInformationFull.model_validate(raw)

    async def set_client_info(self, client_info) -> None:
        data = self._read()
        data["client"] = client_info.model_dump(mode="json", exclude_none=True)
        self._write(data)


def _plain(text: str, limit: int = 200) -> str:
    """Text from outside, made safe to print: printable characters only, and not too long."""
    return "".join(ch for ch in text if ch.isprintable())[:limit]


class CallbackListener:
    """Listens on this computer for the browser's return from the sign-in page.

    Only a request to /callback that carries the `state` value of the sign-in this app started is
    accepted. Anything else (another page probing the port, a favicon request) gets a "not found"
    reply and does not end the wait."""

    PATH = "/callback"

    def __init__(self, port: int):
        self.port = port
        self.expected_state: str | None = None
        self._server: asyncio.AbstractServer | None = None
        self._done: asyncio.Future[dict[str, str]] | None = None
        self._writers: set[asyncio.StreamWriter] = set()

    async def start(self, expected_state: str | None = None) -> None:
        await self.close()
        self.expected_state = expected_state
        self._done = asyncio.get_running_loop().create_future()
        try:
            try:      # "localhost" can mean the IPv4 or the IPv6 loopback address, depending on the browser
                self._server = await asyncio.start_server(self._handle, ["127.0.0.1", "::1"], self.port)
            except OSError:
                self._server = await asyncio.start_server(self._handle, "127.0.0.1", self.port)
        except OSError as exc:
            raise SignInRequired(f"Cannot listen on port {self.port} for the sign-in to return "
                                 f"({exc.strerror or exc}). Close whatever is using it, or set "
                                 "ROBINHOOD_CALLBACK_PORT in .env to another port, and try again.") from exc

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        self._writers.add(writer)
        try:
            try:
                raw = await asyncio.wait_for(reader.readline(), 5)
            except (asyncio.TimeoutError, ValueError, OSError):
                return                                    # a connection that sends nothing, or junk
            parts = raw.decode("latin-1", "replace").split(" ")
            target = urlparse(parts[1]) if len(parts) > 1 else None
            params: dict[str, str] = {}
            if target is not None and target.path == self.PATH:
                params = {k: v[0] for k, v in parse_qs(target.query).items() if v}
            ours = bool(params) and ("code" in params or "error" in params) and (
                self.expected_state is None or params.get("state") == self.expected_state)
            if ours:
                status = "200 OK"
                body = ("<h2>Signed in.</h2><p>You can close this tab and return to the terminal.</p>"
                        if "code" in params else
                        "<h2>Sign-in did not complete.</h2><p>Return to the terminal for details.</p>")
            else:
                status, body = "404 Not Found", "<p>Nothing here.</p>"
            page = f"<!doctype html><meta charset='utf-8'><title>Trade Assistant</title><body>{body}</body>"
            writer.write((f"HTTP/1.1 {status}\r\nContent-Type: text/html; charset=utf-8\r\n"
                          f"Content-Length: {len(page.encode())}\r\nConnection: close\r\n\r\n{page}").encode())
            with suppress(OSError):
                await writer.drain()
            if ours and self._done is not None and not self._done.done():
                self._done.set_result(params)
        finally:
            self._writers.discard(writer)
            with suppress(Exception):
                writer.close()

    async def wait(self, timeout: float = SIGN_IN_TIMEOUT):
        from mcp.client.auth import AuthorizationCodeResult
        if self._done is None:
            await self.start()
        assert self._done is not None
        try:
            params = await asyncio.wait_for(self._done, timeout)
        except asyncio.TimeoutError as exc:
            raise SignInRequired("Sign-in was not completed in the browser within five minutes") from exc
        finally:
            await self.close()
        if "code" not in params:
            reason = _plain(params.get("error_description") or params.get("error") or "no authorization code returned")
            raise SignInRequired(f"Sign-in was refused: {reason}")
        return AuthorizationCodeResult(code=params["code"], state=params.get("state"), iss=params.get("iss"))

    async def close(self) -> None:
        server, self._server = self._server, None
        if server is None:
            return
        server.close()
        for writer in list(self._writers):        # do not wait for idle connections to go away by themselves
            with suppress(Exception):
                writer.close()
        with suppress(asyncio.TimeoutError, Exception):
            await asyncio.wait_for(server.wait_closed(), 2)


async def wait_for_callback(port: int, timeout: float = SIGN_IN_TIMEOUT, expected_state: str | None = None):
    """Listen once on localhost for the browser's return from the sign-in page."""
    listener = CallbackListener(port)
    await listener.start(expected_state)
    return await listener.wait(timeout)


def check_server_url(url: str) -> str:
    """The sign-in travels to this address, so it must be encrypted unless it is this computer."""
    parsed = urlparse(url)
    local = parsed.hostname in ("localhost", "127.0.0.1", "::1")
    if parsed.scheme != "https" and not (parsed.scheme == "http" and local):
        raise ValueError(f"ROBINHOOD_MCP_URL must be an https address, got {url!r}")
    return url


def oauth_connector(url: str, token_path: Path, callback_port: int, interactive: bool,
                    client_name: str = "Trade Assistant (local app)"
                    ) -> Callable[[], AbstractAsyncContextManager[Any]]:
    """A `connect` function for ReadOnlyBridge that signs in to an MCP server with OAuth.

    With `interactive` the browser is opened for sign-in when needed. Without it (the
    running web app) a missing or expired sign-in raises SignInRequired instead, so the
    app never opens a browser by surprise. A stored sign-in that has run out is renewed
    without a browser when the server allows it.
    """
    from contextlib import asynccontextmanager

    check_server_url(url)

    @asynccontextmanager
    async def connect():
        from mcp import Client
        from mcp.client.auth import OAuthClientProvider
        from mcp.client.streamable_http import create_mcp_http_client, streamable_http_client
        from mcp.shared.auth import OAuthClientMetadata

        redirect_uri = f"http://localhost:{callback_port}/callback"
        storage = FileTokenStorage(token_path, redirect_uri)
        listener = CallbackListener(callback_port)

        class Provider(OAuthClientProvider):
            async def _initialize(self) -> None:
                # The library does not remember when a stored sign-in runs out, so it would use a
                # stale one, be refused, and ask for a full browser sign-in. Tell it the real time,
                # or, when that is unknown and a renewal is possible, to renew first.
                await super()._initialize()
                tokens = self.context.current_tokens
                if tokens is not None:
                    known = storage.renew_at()
                    if known is not None:
                        self.context.token_expiry_time = known
                    elif tokens.refresh_token:
                        self.context.token_expiry_time = time.time() - 1

        async def open_browser(auth_url: str) -> None:
            if not interactive:
                raise SignInRequired("The Robinhood sign-in is missing or has expired. Stop the app, run "
                                     "'python run.py --robinhood-check' to sign in again, then restart.")
            state = (parse_qs(urlparse(auth_url).query).get("state") or [None])[0]
            await listener.start(state)           # before the browser opens, so a busy port is reported at once
            print("\nOpening your browser to sign in to Robinhood. If it does not open, paste this address "
                  f"into it:\n\n  {auth_url}\n")
            try:
                webbrowser.open(auth_url)
            except Exception:      # noqa: BLE001 - the printed address is the fallback
                pass

        async def receive():
            return await listener.wait(SIGN_IN_TIMEOUT)

        provider = Provider(
            server_url=url,
            client_metadata=OAuthClientMetadata(
                client_name=client_name,
                redirect_uris=[redirect_uri],
                grant_types=["authorization_code", "refresh_token"],
                response_types=["code"],
            ),
            storage=storage,
            redirect_handler=open_browser,
            callback_handler=receive,
        )
        try:
            async with create_mcp_http_client(auth=provider) as http:
                async with Client(streamable_http_client(url, http_client=http)) as client:
                    yield client
        finally:
            await listener.close()

    return connect
