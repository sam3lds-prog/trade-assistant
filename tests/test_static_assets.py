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
from contextlib import contextmanager
from pathlib import Path

import pytest

STATIC = Path(__file__).resolve().parent.parent / "tradeassist" / "static"
SCRIPTS = ("flow.js", "app.js", "v2.js", "lab.js", "decide.js")                 # in the order index.html loads them
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


@contextmanager
def demo_page(tmp_path):
    """The app on demo data in a real Chromium page; yields (page, script errors)."""
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
            page.base = f"http://127.0.0.1:{port}/"
            yield page, errors
            browser.close()
    finally:
        server.should_exit = True
        thread.join(timeout=10)
        svc.db.close()


@pytest.mark.skipif(_browser() is None, reason="Playwright is not installed")
def test_every_screen_opens_without_a_script_error(tmp_path):
    with demo_page(tmp_path) as (page, errors):
        page.goto(page.base)
        page.wait_for_function("document.querySelectorAll('#main > section').length > 0", timeout=20000)
        views = page.eval_on_selector_all(".rail button.nav:not(.hidden)", "bs => bs.map(b => b.dataset.view)")
        assert {"today", "positions", "portfolio", "health", "lab", "scanner", "market"} <= set(views)
        assert page.evaluate("location.hash") == "#today"            # the landing screen (2.5)
        groups = page.eval_on_selector_all(".rail .nav-group:not(.hidden) .nav-group-label", "ls => ls.map(l => l.textContent)")
        assert groups == ["Today", "Find", "Decide", "Price & simulate", "Hold", "Learn", "Trust"]
        for v in views:
            page.click(f'.rail button.nav[data-view="{v}"]')
            page.wait_for_timeout(400)
        page.click('.rail button.nav[data-view="health"]')
        page.wait_for_selector("text=Data source", timeout=20000)
    assert errors == []


@pytest.mark.skipif(_browser() is None, reason="Playwright is not installed")
def test_one_thesis_grades_finds_a_contract_and_sets_the_limit(tmp_path):
    """2.5: Decide's one form grades the stock and picks contracts; Set the limit opens that contract priced."""
    with demo_page(tmp_path) as (page, errors):
        page.goto(page.base + "#analyze")
        page.click("section:not([hidden]) button.primary:text-is('Analyze')")
        page.wait_for_selector("text=Your thesis", timeout=30000)
        page.click("text=Grade and find a contract")
        page.wait_for_selector(".panel.thesis .pick", timeout=60000)
        assert page.locator(".panel.thesis .grade-title, .panel.thesis .grade").count() >= 1
        # Shares and the best risk-adjusted contract side by side, both on one chart.
        page.wait_for_selector(".compare .chart svg path", timeout=60000)
        assert page.locator(".compare tbody tr").count() == 2
        assert page.locator(".compare .legend span").count() == 2
        assert "unknown" not in page.locator(".compare tbody").inner_text().replace("Unknown", "")
        assert "$0 against" not in page.locator(".compare tbody").inner_text()
        card = page.locator(".panel.thesis .pick").first
        chosen = page.evaluate("""() => {
          const k = state.decision.options.picks[0].contract;
          return { strike: k.strike, expiration: k.expiration, type: k.option_type.toLowerCase(), symbol: state.decision.options.symbol };
        }""")
        card.locator("text=Set the limit").click()
        page.wait_for_selector("text=Simulate this trade", timeout=30000)
        assert page.evaluate("location.hash") == "#price"
        form = page.evaluate("""() => {
          const v = state.views.price, pick = label => [...v.querySelectorAll("label.field, div.field")]
            .find(f => f.firstChild.textContent === label);
          return { symbol: v.querySelector("input.sym").value, expiration: pick("Expiration").querySelector("select").value,
                   strike: Number(pick("Strike").querySelector("select").value),
                   type: v.querySelector('input[name="ptype"]:checked').value, side: v.querySelector('input[name="pside"]:checked').value };
        }""")
        assert form == {"symbol": chosen["symbol"], "expiration": chosen["expiration"], "strike": chosen["strike"],
                        "type": chosen["type"], "side": "buy"}
    assert errors == []


@pytest.mark.skipif(_browser() is None, reason="Playwright is not installed")
def test_today_lists_lead_to_a_graded_thesis_in_that_direction(tmp_path):
    """2.5: a stock on Today's long or short list opens Decide with that direction and grades it."""
    with demo_page(tmp_path) as (page, errors):
        page.goto(page.base)
        page.wait_for_selector(".lean-table tr.click", timeout=60000)
        side = "LONG" if page.locator(".lean-grid > .panel:first-child .lean-table tr.click").count() else "SHORT"
        panel = 1 if side == "LONG" else 2
        row = page.locator(f".lean-grid > .panel:nth-child({panel}) .lean-table tr.click").first
        symbol = row.locator("td strong").first.inner_text().strip()
        row.click()
        page.wait_for_selector(".panel.thesis .grade-title", timeout=60000)
        assert page.evaluate("location.hash") == "#analyze"
        assert page.evaluate("state.thesis.symbol") == symbol and page.evaluate("state.thesis.direction") == side
        checked = page.eval_on_selector('.panel.thesis input[type="radio"]:checked', "i => i.value")
        assert checked == side
        page.wait_for_function(f"state.decision && state.decision.thesis.symbol === '{symbol}'", timeout=60000)
        assert page.evaluate("state.decision.options.thesis.direction") == ("BULLISH" if side == "LONG" else "BEARISH")
    assert errors == []


@pytest.mark.skipif(_browser() is None, reason="Playwright is not installed")
def test_every_scanner_tab_opens_decide(tmp_path):
    with demo_page(tmp_path) as (page, errors):
        page.goto(page.base + "#scanner")
        page.wait_for_selector(".scan-table.tech tr.click", timeout=60000)
        page.locator(".scan-table.tech tr.click").first.click()
        page.wait_for_selector("text=Your thesis", timeout=60000)
        assert page.evaluate("location.hash") == "#analyze"
        page.click('.rail button.nav[data-view="scanner"]')
        page.click('section:not(.hidden) .seg label:has-text("Reversals")')
        page.click('section:not(.hidden) button:has-text("Scan the watchlist")')
        page.wait_for_selector(".reversal-scan tr.click", timeout=60000)
        page.locator(".reversal-scan tr.click").first.click()
        page.wait_for_function("location.hash === '#analyze'", timeout=30000)
        page.click('.rail button.nav[data-view="scanner"]')
        page.click('section:not(.hidden) .seg label:has-text("Options activity")')
        page.wait_for_selector(".scan-table:not(.tech) tr.click", timeout=60000)
        page.locator(".scan-table:not(.tech) tr.click").first.locator("button:has-text('Options lab')").click()
        page.wait_for_function("location.hash === '#lab'", timeout=30000)
    assert errors == []
