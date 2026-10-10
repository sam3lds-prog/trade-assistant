"""The page's scripts load together.

app.js, v2.js and lab.js are classic scripts sharing one global scope, so a name declared at the top
level of two of them (`function ageText` in one, `const ageText` in the other) makes the browser
reject the second script whole, and every V2 screen with it. 2.2.0 shipped that way; these
checks keep it from happening again. The browser test runs only where Playwright and Chromium
are installed.
"""

from __future__ import annotations

import re
import shutil
import socket
import subprocess
import threading
import time
from pathlib import Path

import pytest

STATIC = Path(__file__).resolve().parent.parent / "tradeassist" / "static"
SCRIPTS = ("app.js", "v2.js", "lab.js")                 # in the order index.html loads them
TOP_LEVEL = re.compile(r"^(?:async\s+)?(?:const|let|var|function\*?|class)\s+([A-Za-z_$][\w$]*)", re.M)


def top_level(name: str) -> set[str]:
    return set(TOP_LEVEL.findall((STATIC / name).read_text()))


def test_no_name_is_declared_at_the_top_of_two_scripts():
    for i, a in enumerate(SCRIPTS):
        for b in SCRIPTS[i + 1:]:
            assert top_level(a) & top_level(b) == set(), (a, b)


def test_index_loads_every_script_in_order_with_a_fresh_address():
    from tradeassist import api
    html = (STATIC / "index.html").read_text()
    found = re.findall(r'<script src="/(\w+\.js)"', html)
    assert tuple(found) == SCRIPTS
    assert all(f'"{name}"' in Path(api.__file__).read_text() for name in SCRIPTS)


def test_every_navigation_button_has_a_screen():
    html = (STATIC / "index.html").read_text()
    views = set(re.findall(r'data-view="(\w+)"', html))
    js = "".join((STATIC / name).read_text() for name in SCRIPTS)
    registered = set()
    for block in re.findall(r"BUILDERS = \{([^}]*)\}|Object\.assign\(BUILDERS, \{([^}]*)\}\)", js):
        for part in block:
            registered |= set(re.findall(r"(\w+)\s*:", part))
    registered |= set(re.findall(r"BUILDERS\.(\w+)\s*=", js))
    assert views and views <= registered, views - registered


@pytest.mark.skipif(shutil.which("node") is None, reason="Node.js is not installed")
def test_the_scripts_compile_as_one_page(tmp_path):
    # One script made of both files: a clashing declaration is an early SyntaxError when compiled.
    joined = tmp_path / "page.js"
    joined.write_text("\n;\n".join((STATIC / name).read_text() for name in SCRIPTS))
    check = "const vm = require('vm'); new vm.Script(require('fs').readFileSync(process.argv[1], 'utf8'));"
    out = subprocess.run(["node", "-e", check, str(joined)], capture_output=True, text=True, timeout=60)
    assert out.returncode == 0, out.stderr[-2000:]


def _browser():
    try:
        from playwright.sync_api import sync_playwright  # noqa: F401
    except ImportError:
        return None
    path = Path("/opt/pw-browsers/chromium")
    return str(path) if path.exists() else ""


@pytest.mark.skipif(_browser() is None, reason="Playwright is not installed")
def test_every_screen_opens_without_a_script_error(tmp_path):
    import uvicorn
    from playwright.sync_api import sync_playwright

    from tradeassist.api import create_app
    from tradeassist.config import Settings
    from tradeassist.db import Database
    from tradeassist.providers.demo import DemoData
    from tradeassist.service import TradeAssistant

    settings = Settings(data_mode="demo", db_path=tmp_path / "t.db")
    svc = TradeAssistant(settings, provider=DemoData(), db=Database(tmp_path / "t.db"))
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(create_app(settings, svc), host="127.0.0.1", port=port, log_level="error"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    for _ in range(100):
        if server.started:
            break
        time.sleep(0.1)
    errors: list[str] = []
    try:
        with sync_playwright() as p:
            exe = _browser()
            try:
                browser = p.chromium.launch(executable_path=exe or None)
            except Exception as exc:  # noqa: BLE001 - no browser binary here
                pytest.skip(f"Chromium could not start: {exc}")
            page = browser.new_page(viewport={"width": 1280, "height": 900})
            page.on("pageerror", lambda e: errors.append(str(e)))
            page.goto(f"http://127.0.0.1:{port}/")
            page.wait_for_function("document.querySelectorAll('#main > section').length > 0", timeout=20000)
            views = page.eval_on_selector_all(".rail button.nav:not(.hidden)", "bs => bs.map(b => b.dataset.view)")
            assert {"positions", "portfolio", "health", "lab", "scanner", "market"} <= set(views)
            for v in views:
                page.click(f'.rail button.nav[data-view="{v}"]')
                page.wait_for_timeout(400)
            page.click('.rail button.nav[data-view="health"]')
            page.wait_for_selector("text=Data source", timeout=20000)
            browser.close()
    finally:
        server.should_exit = True
        thread.join(timeout=10)
        svc.db.close()
    assert errors == []
