"""The Robinhood sign-in path, run for real against a local server that requires OAuth.

This covers what the stand-in tests cannot: client registration, the browser hand-off,
the localhost callback, token storage and reuse, and the refusal to open a browser from
the running web app. The local server is the MCP library's own OAuth implementation,
not Robinhood's, so it shows the app follows the standard flow correctly; it cannot
show that Robinhood's server accepts it.
"""

import secrets
import socket
import stat
import threading
import time
import urllib.request

import pytest
import uvicorn
from mcp.server.auth.provider import AccessToken, AuthorizationCode, RefreshToken, construct_redirect_uri
from mcp.server.auth.settings import AuthSettings, ClientRegistrationOptions
from mcp.shared.auth import OAuthToken

from tests.robinhood_standin import Standin
from tradeassist.providers.mcp_bridge import FileTokenStorage, SignInRequired
from tradeassist.providers.robinhood import make_robinhood


class LocalAuth:
    """An OAuth server that approves every sign-in at once (there is no person to click Allow)."""
    def __init__(self):
        self.clients, self.codes, self.access, self.refresh = {}, {}, {}, {}
        self.authorizations = 0

    async def get_client(self, client_id):
        return self.clients.get(client_id)

    async def register_client(self, client_info):
        self.clients[client_info.client_id] = client_info

    async def authorize(self, client, params):
        self.authorizations += 1
        code = secrets.token_urlsafe(16)
        self.codes[code] = AuthorizationCode(
            code=code, scopes=params.scopes or [], expires_at=time.time() + 300, client_id=client.client_id,
            code_challenge=params.code_challenge, redirect_uri=params.redirect_uri,
            redirect_uri_provided_explicitly=params.redirect_uri_provided_explicitly, resource=params.resource)
        return construct_redirect_uri(str(params.redirect_uri), code=code, state=params.state)

    async def load_authorization_code(self, client, authorization_code):
        return self.codes.get(authorization_code)

    def _issue(self, client_id, scopes, resource=None):
        access, refresh = secrets.token_urlsafe(16), secrets.token_urlsafe(16)
        self.access[access] = AccessToken(token=access, client_id=client_id, scopes=scopes,
                                          expires_at=int(time.time()) + 3600, resource=resource)
        self.refresh[refresh] = RefreshToken(token=refresh, client_id=client_id, scopes=scopes)
        return OAuthToken(access_token=access, token_type="Bearer", expires_in=3600, refresh_token=refresh,
                          scope=" ".join(scopes) or None)

    async def exchange_authorization_code(self, client, authorization_code):
        self.codes.pop(authorization_code.code, None)
        return self._issue(client.client_id, authorization_code.scopes, authorization_code.resource)

    async def load_refresh_token(self, client, refresh_token):
        return self.refresh.get(refresh_token)

    async def exchange_refresh_token(self, client, refresh_token, scopes):
        self.refresh.pop(refresh_token.token, None)
        return self._issue(client.client_id, scopes or refresh_token.scopes)

    async def load_access_token(self, token):
        return self.access.get(token)

    async def revoke_token(self, token):
        self.access.pop(token.token, None)
        self.refresh.pop(token.token, None)


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture()
def secured_server():
    from mcp.server.mcpserver import MCPServer
    port = free_port()
    base = f"http://127.0.0.1:{port}"
    auth = LocalAuth()
    standin = Standin("a")
    server = MCPServer("secured-standin", auth_server_provider=auth, auth=AuthSettings(
        issuer_url=base, resource_server_url=f"{base}/mcp",
        client_registration_options=ClientRegistrationOptions(enabled=True), validate_token_resource=False))
    for tool in standin.server._tool_manager.list_tools():       # reuse the stand-in's tools on the secured server
        server._tool_manager._tools[tool.name] = tool
    config = uvicorn.Config(server.streamable_http_app(), host="127.0.0.1", port=port, log_level="error")
    runner = uvicorn.Server(config)
    thread = threading.Thread(target=runner.run, daemon=True)
    thread.start()
    for _ in range(100):
        if runner.started:
            break
        time.sleep(0.05)
    yield f"{base}/mcp", auth, standin
    runner.should_exit = True
    thread.join(timeout=5)


def test_sign_in_stores_tokens_reuses_them_and_never_opens_a_browser_from_the_app(secured_server, tmp_path, monkeypatch, capsys):
    url, auth, standin = secured_server
    token_file = tmp_path / "data" / "robinhood_signin.json"
    callback_port = free_port()
    opened = []

    def fake_browser(address):
        """Stand in for the person's browser: load the sign-in page and follow it back to the app."""
        opened.append(address)
        threading.Thread(target=lambda: urllib.request.urlopen(address, timeout=10).read(), daemon=True).start()
        return True
    monkeypatch.setattr("tradeassist.providers.mcp_bridge.webbrowser.open", fake_browser)

    # 1. The web app (not interactive) with no stored sign-in: a clear error, and no browser.
    app_side = make_robinhood(token_file, callback_port, interactive=False, history_fallback=None, url=url)
    with pytest.raises(SignInRequired, match="--robinhood-check"):
        app_side.expirations("AAPL")
    app_side.bridge.close()
    assert opened == [] and auth.authorizations == 0

    # 2. The check command (interactive): registers, signs in through the browser, stores the tokens.
    cli_side = make_robinhood(token_file, callback_port, interactive=True, history_fallback=None, url=url)
    assert cli_side.expirations("AAPL") == standin.demo.expirations("AAPL")
    cli_side.bridge.close()
    assert len(opened) == 1 and "code_challenge=" in opened[0] and auth.authorizations == 1
    assert f"localhost%3A{callback_port}%2Fcallback" in opened[0] or f"localhost:{callback_port}/callback" in opened[0]
    assert "paste this address" in capsys.readouterr().out
    assert FileTokenStorage(token_file).exists()
    assert stat.S_IMODE(token_file.stat().st_mode) == 0o600               # readable by the owner only
    stored = token_file.read_text()
    assert "access_token" in stored and "refresh_token" in stored and "client_id" in stored

    # 3. The web app again: the stored sign-in is used silently.
    app_side = make_robinhood(token_file, callback_port, interactive=False, history_fallback=None, url=url)
    quote = app_side.quotes(["AAPL"])["AAPL"]
    assert quote["last"] == pytest.approx(standin.demo.quotes(["AAPL"])["AAPL"]["last"])
    app_side.bridge.close()
    assert len(opened) == 1 and auth.authorizations == 1                  # no second sign-in
    assert standin.forbidden_calls == []

    # 4. Signing out removes the stored sign-in, after which the app asks for a new one.
    FileTokenStorage(token_file).clear()
    assert not token_file.exists()
    again = make_robinhood(token_file, callback_port, interactive=False, history_fallback=None, url=url)
    with pytest.raises(SignInRequired):
        again.expirations("AAPL")
    again.bridge.close()


def test_changing_the_callback_port_forces_a_new_registration(tmp_path):
    import asyncio
    from mcp.shared.auth import OAuthClientInformationFull
    path = tmp_path / "signin.json"
    info = OAuthClientInformationFull(client_id="abc", redirect_uris=["http://localhost:8421/callback"])
    asyncio.run(FileTokenStorage(path, "http://localhost:8421/callback").set_client_info(info))
    same = asyncio.run(FileTokenStorage(path, "http://localhost:8421/callback").get_client_info())
    moved = asyncio.run(FileTokenStorage(path, "http://localhost:9000/callback").get_client_info())
    assert same.client_id == "abc" and moved is None


def test_busy_callback_port_is_explained():
    import asyncio
    from tradeassist.providers.mcp_bridge import wait_for_callback
    port = free_port()
    with socket.socket() as blocker:
        blocker.bind(("127.0.0.1", port))
        blocker.listen(1)
        with pytest.raises(SignInRequired, match="ROBINHOOD_CALLBACK_PORT"):
            asyncio.run(wait_for_callback(port, timeout=1))


def test_sign_in_that_is_declined_or_never_finished_is_reported():
    import asyncio
    from tradeassist.providers.mcp_bridge import wait_for_callback
    port = free_port()

    async def declined():
        task = asyncio.create_task(wait_for_callback(port, timeout=5))
        await asyncio.sleep(0.2)
        await asyncio.to_thread(lambda: urllib.request.urlopen(
            f"http://127.0.0.1:{port}/callback?error=access_denied&error_description=You+declined", timeout=5).read())
        return await task
    with pytest.raises(SignInRequired, match="You declined"):
        asyncio.run(declined())
    with pytest.raises(SignInRequired, match="within five minutes"):
        asyncio.run(wait_for_callback(free_port(), timeout=0.2))


def test_an_expired_sign_in_is_renewed_without_a_browser(tmp_path, monkeypatch):
    """The stored sign-in lasts three seconds here. The web app must renew it quietly, not ask again."""
    from mcp.server.mcpserver import MCPServer

    class ShortLived(LocalAuth):
        renewals = 0

        def _issue(self, client_id, scopes, resource=None):
            token = super()._issue(client_id, scopes, resource)
            self.access[token.access_token].expires_at = int(time.time()) + 3
            token.expires_in = 3
            return token

        async def exchange_refresh_token(self, client, refresh_token, scopes):
            ShortLived.renewals += 1
            return await super().exchange_refresh_token(client, refresh_token, scopes)

    port = free_port()
    base = f"http://127.0.0.1:{port}"
    auth, standin = ShortLived(), Standin("a")
    server = MCPServer("short-lived", auth_server_provider=auth, auth=AuthSettings(
        issuer_url=base, resource_server_url=f"{base}/mcp",
        client_registration_options=ClientRegistrationOptions(enabled=True), validate_token_resource=False))
    for tool in standin.server._tool_manager.list_tools():
        server._tool_manager._tools[tool.name] = tool
    runner = uvicorn.Server(uvicorn.Config(server.streamable_http_app(), host="127.0.0.1", port=port, log_level="error"))
    thread = threading.Thread(target=runner.run, daemon=True)
    thread.start()
    for _ in range(100):
        if runner.started:
            break
        time.sleep(0.05)
    try:
        opened = []

        def fake_browser(address):
            opened.append(address)
            threading.Thread(target=lambda: urllib.request.urlopen(address, timeout=10).read(), daemon=True).start()
            return True
        monkeypatch.setattr("tradeassist.providers.mcp_bridge.webbrowser.open", fake_browser)
        token_file, callback_port = tmp_path / "robinhood_signin.json", free_port()
        cli = make_robinhood(token_file, callback_port, interactive=True, history_fallback=None, url=f"{base}/mcp")
        assert cli.expirations("AAPL")
        cli.bridge.close()
        storage = FileTokenStorage(token_file)
        assert storage.expires_at() == pytest.approx(time.time() + 3, abs=2)   # the end time is remembered
        first = storage._read()["tokens"]["access_token"]
        time.sleep(4)
        app = make_robinhood(token_file, callback_port, interactive=False, history_fallback=None, url=f"{base}/mcp")
        assert app.expirations("AAPL") == standin.demo.expirations("AAPL")
        app.bridge.close()
        assert ShortLived.renewals == 1 and len(opened) == 1 and auth.authorizations == 1
        assert storage._read()["tokens"]["access_token"] != first               # the renewed sign-in was saved
        assert stat.S_IMODE(token_file.stat().st_mode) == 0o600
    finally:
        runner.should_exit = True
        thread.join(timeout=5)


def test_only_the_awaited_return_ends_the_wait():
    """Other pages can reach a port on this computer. Only /callback with this sign-in's state counts."""
    import asyncio
    from tradeassist.providers.mcp_bridge import wait_for_callback
    port = free_port()

    def get(path):
        try:
            return urllib.request.urlopen(f"http://127.0.0.1:{port}{path}", timeout=5).status
        except urllib.error.HTTPError as exc:
            return exc.code

    async def scenario():
        task = asyncio.create_task(wait_for_callback(port, timeout=10, expected_state="right-state"))
        await asyncio.sleep(0.2)
        idle = socket.create_connection(("127.0.0.1", port))      # connects and never sends anything
        strays = ["/favicon.ico", "/favicon.ico?code=junk&state=right-state", "/callback",
                  "/callback?code=junk&state=wrong", "/callback?code=junk",
                  "/anything?error=x&error_description=Call+1-800-SCAM&state=right-state",
                  "/callback?error=access_denied&error_description=Call+1-800-SCAM&state=guess"]
        codes = [await asyncio.to_thread(get, path) for path in strays]
        await asyncio.sleep(0.2)
        assert not task.done() and codes == [404] * len(strays)
        started = time.time()
        assert await asyncio.to_thread(get, "/callback?code=real-code&state=right-state") == 200
        result = await asyncio.wait_for(task, 4)                   # not held up by the idle connection
        idle.close()
        return result, time.time() - started
    result, took = asyncio.run(scenario())
    assert result.code == "real-code" and result.state == "right-state" and took < 4


def test_the_sign_in_file_is_private_whatever_was_there_before(tmp_path):
    import asyncio
    path = tmp_path / "robinhood_signin.json"
    old_tmp = tmp_path / "robinhood_signin.tmp"                   # a leftover with loose permissions
    old_tmp.write_text("{}")
    old_tmp.chmod(0o644)
    elsewhere = tmp_path / "elsewhere.txt"
    (tmp_path / "robinhood_signin.link.part").symlink_to(elsewhere)
    storage = FileTokenStorage(path)
    token = OAuthToken(access_token="A" * 20, token_type="Bearer", expires_in=600, refresh_token="R" * 20,
                       scope="market_data trading")
    asyncio.run(storage.set_tokens(token))
    assert stat.S_IMODE(path.stat().st_mode) == 0o600 and not elsewhere.exists()
    assert storage.scope() == "market_data trading" and storage.expires_at() > time.time() + 500
    assert asyncio.run(storage.get_tokens()).access_token == "A" * 20
    storage.clear()
    assert not path.exists() and not list(tmp_path.glob("robinhood_signin.*.part"))


def test_a_short_sign_in_is_not_treated_as_expired_the_moment_it_is_stored(tmp_path):
    import asyncio
    storage = FileTokenStorage(tmp_path / "s.json")
    asyncio.run(storage.set_tokens(OAuthToken(access_token="a" * 20, token_type="Bearer", expires_in=10)))
    assert time.time() + 3 < storage.renew_at() < time.time() + 7           # half its life, not 30 s before the end
    asyncio.run(storage.set_tokens(OAuthToken(access_token="a" * 20, token_type="Bearer", expires_in=3600)))
    assert storage.renew_at() == pytest.approx(time.time() + 3570, abs=5)
    asyncio.run(storage.set_tokens(OAuthToken(access_token="a" * 20, token_type="Bearer")))
    assert storage.renew_at() is None
