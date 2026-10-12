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
SCRIPTS = ("flow.js", "app.js", "v2.js", "lab.js", "decide.js", "trades.js", "ideas.js")     # in the order index.html loads them
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


def test_analyze_has_one_thesis_form_and_says_the_trend_turned_sentence_once():
    """2.5.1: the old call-or-put form is gone, and the analog sentence lives on the comparison only."""
    v2 = (STATIC / "v2.js").read_text()
    assert "function optionsSection" not in v2 and "optionsSection(" not in v2
    for keep in ("function renderOptionsEval", "function limitButton", "function thesisSection"):
        assert keep in v2
    joined = "".join((STATIC / name).read_text() for name in SCRIPTS)
    assert joined.count("trend turned to") == 1


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
def demo_page(tmp_path, features=None):
    """The app on demo data in a real Chromium page; yields (page, script errors). `features` overrides flags."""
    import uvicorn
    from playwright.sync_api import sync_playwright

    from tradeassist.api import create_app
    from tradeassist.config import Settings
    from tradeassist.db import Database
    from tradeassist.providers.demo import DemoData
    from tradeassist.service import TradeAssistant

    from tradeassist.features import FEATURE_DEFAULTS
    settings = Settings(data_mode="demo", db_path=tmp_path / "t.db", features={**FEATURE_DEFAULTS, **(features or {})})
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
        assert {"today", "positions", "portfolio", "health", "lab", "scanner", "market", "trades"} <= set(views)
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
        page.wait_for_selector(".panel.thesis", timeout=30000)
        page.click("text=Grade and find a contract")
        page.wait_for_selector(".panel.thesis .pick", timeout=60000)
        assert page.locator(".panel.thesis .grade-title, .panel.thesis .grade").count() >= 1
        # Shares and the best risk-adjusted contract side by side, both on one chart.
        page.wait_for_selector(".compare .chart svg path", timeout=60000)
        assert page.locator(".compare tbody tr").count() == 2
        assert page.locator(".compare .legend span").count() == 2
        # The trend chart now carries the plan: the axis runs to the horizon with the levels on it.
        page.wait_for_selector(".trend-chart h3:has-text('your plan')", timeout=30000)
        assert page.locator(".trend-chart svg path.band").count() == 1
        assert "Invalidation" in page.locator(".trend-chart .legend").inner_text()
        assert "expiration" in page.locator(".trend-chart .legend").inner_text()
        assert "unknown" not in page.locator(".compare tbody").inner_text().replace("Unknown", "")
        body = page.locator(".compare tbody").inner_text()
        assert "$0 against" not in body and "if bought" not in body
        assert body.count("middle outcome") == 2 and body.count("Eight in ten between") == 2
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
        # Before the signal history exists there is no frequency column, not a column of blanks.
        assert "Higher 10 days later" not in page.locator(".lean-grid").inner_text()
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
        page.wait_for_selector(".panel.thesis", timeout=60000)
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


@pytest.mark.skipif(_browser() is None, reason="Playwright is not installed")
def test_without_the_trend_engine_the_app_lands_on_price_and_keeps_the_reversal_scan_reachable(tmp_path):
    """Today needs the trend engine too; with the Scanner off the reversal scan stays on Analyze."""
    with demo_page(tmp_path, {"trend_engine_v2": False}) as (page, errors):
        page.goto(page.base)
        page.wait_for_function("document.querySelectorAll('#main > section').length > 0", timeout=20000)
        assert page.evaluate("location.hash") == "#price"
        assert page.locator('.rail button.nav[data-view="today"]').is_hidden()
        assert page.locator('.rail .nav-group[data-group="decide"]').is_hidden()
    assert errors == []
    with demo_page(tmp_path / "b", {"scanner": False}) as (page, errors):
        page.goto(page.base + "#analyze")
        page.wait_for_selector(".reversal-scan", timeout=20000)
        # 2.7: Find also holds Trade ideas, so the group stays; the Scanner's own button is what goes.
        assert page.locator('.rail button.nav[data-view="scanner"]').is_hidden()
        assert page.locator('.rail button.nav[data-view="ideas"]').is_visible()
    assert errors == []


@pytest.mark.skipif(_browser() is None, reason="Playwright is not installed")
def test_turning_the_thesis_round_drops_its_prices_and_a_new_ticker_drops_the_earnings_date(tmp_path):
    with demo_page(tmp_path) as (page, errors):
        page.goto(page.base + "#analyze")
        page.wait_for_function("typeof state !== 'undefined' && state.views.analyze", timeout=30000)
        page.evaluate("state.thesis = FLOW.thesis({symbol: 'AAPL', direction: 'SHORT', invalidation: 210, target1: 180, horizonDays: 30, contracts: 3})")
        page.evaluate("state.openDecide('AAPL', 'LONG', 'manual')")
        th = page.evaluate("state.thesis")
        assert (th["direction"], th["invalidation"], th["target1"], th["horizonDays"], th["contracts"]) == ("LONG", None, None, 30, 3)
        # An earnings date typed for one stock is not carried to another by Set the limit.
        page.click('.rail button.nav[data-view="price"]')
        page.wait_for_selector("section:not(.hidden) input[type=date]", timeout=20000)
        page.fill("section:not(.hidden) input[type=date]", "2026-10-20")
        k = page.evaluate("""async () => {
          const r = await api("/v2/options/evaluate", { symbol: "MSFT", direction: "BULLISH" });
          return r.picks[0].contract;
        }""")
        page.evaluate(f"state.openPricing(FLOW.pricingRequest('MSFT', {{option_type: '{k['option_type']}', strike: {k['strike']}, expiration: '{k['expiration']}'}}, 1))")
        page.wait_for_selector("text=Simulate this trade", timeout=30000)
        assert page.input_value("section:not(.hidden) input[type=date]") == ""
        assert page.input_value("section:not(.hidden) input.sym") == "MSFT"
    assert errors == []


@pytest.mark.skipif(_browser() is None, reason="Playwright is not installed")
def test_my_trades_imports_a_report_picks_dates_and_shows_the_statement_rows(tmp_path):
    """2.6: import a report file, narrow the dates, look closer at a period, open a trade's statement rows."""
    report = tmp_path / "activity.csv"
    report.write_text('"Activity Date","Process Date","Settle Date","Instrument","Description","Trans Code","Quantity","Price","Amount"\n'
                      '"2/2/2026","2/2/2026","2/4/2026","AAPL","AAPL 2/20/2026 Call $190.00","STC","1","$5.00","$499.96"\n'
                      '"1/5/2026","1/5/2026","1/6/2026","AAPL","AAPL 2/20/2026 Call $190.00","BTO","1","$3.00","($300.04)"\n'
                      '"1/2/2026","1/2/2026","1/2/2026","","ACH Deposit","ACH","","","$1,000.00"\n'
                      '\n"The data provided is for informational purposes only."\n')
    with demo_page(tmp_path) as (page, errors):
        page.goto(page.base + "#trades")
        page.wait_for_selector("text=Nothing imported yet", timeout=30000)
        page.set_input_files('input[type="file"]', str(report))
        page.click("text=Import the report")
        page.wait_for_selector("text=In this range", timeout=30000)
        assert "2 trade rows added" in page.inner_text("section:not(.hidden) .panel >> nth=0")
        assert "+$199.92" in page.inner_text(".trade-table")
        # Dates: a range that leaves the trade out, then one that holds it.
        page.select_option('select[aria-label="Dates"]', "custom")
        page.fill('input[aria-label="From"]', "2026-03-01")
        page.fill('input[aria-label="To"]', "2026-03-31")
        page.click("section:not(.hidden) button:text-is('Show')")
        page.wait_for_selector("text=0 trades.", timeout=30000)
        page.fill('input[aria-label="From"]', "2026-01-01")
        page.click("section:not(.hidden) button:text-is('Show')")
        page.wait_for_selector("text=1 trade.", timeout=30000)
        # Look closer at the month: the range becomes that month, grouped by week.
        page.locator(".chart rect.click").last.click()
        page.wait_for_function("document.querySelector('select[aria-label=\"Group by\"]').value === 'week'", timeout=30000)
        assert page.input_value('input[aria-label="From"]') == "2026-02-01"
        # The statement rows behind the trade, as written in the report.
        page.locator(".trade-table tbody tr.click").first.click()
        page.wait_for_selector(".trade-detail", timeout=20000)
        detail = page.inner_text(".trade-detail")
        assert "($300.04)" in detail and "$499.96" in detail and "BTO" in detail and "STC" in detail
        # All history stays all of it: no dates are filled in, and the whole history is shown again.
        page.select_option('select[aria-label="Dates"]', "all")
        page.wait_for_function("[...document.querySelectorAll('section:not(.hidden) p.small > strong')].some(e => e.textContent.startsWith('Jan 5, 2026 to'))", timeout=30000)
        assert page.input_value('input[aria-label="From"]') == "" and page.input_value('input[aria-label="To"]') == ""
    assert errors == []


@pytest.mark.skipif(_browser() is None, reason="Playwright is not installed")
def test_trade_ideas_lists_simulate_a_trade_and_hand_it_to_decide(tmp_path):
    """2.7: the scan's lists, a simulation of one trade with its exits, and the same levels on Decide."""
    with demo_page(tmp_path) as (page, errors):
        # The screen does not scan by itself; scan the watchlist through the API first.
        page.request.post(page.base + "v2/ideas/scan", data={"universe": "watchlist"})
        for _ in range(400):
            if page.request.get(page.base + "v2/ideas").json()["job"]["state"] != "running":
                break
            time.sleep(0.3)
        page.goto(page.base + "#ideas")
        page.wait_for_selector("h3:has-text('Highest-graded trades to buy')", timeout=30000)
        assert "Highest-graded trades to short" in page.inner_text("section:not(.hidden)")
        assert page.input_value('select[aria-label="Stocks"]') == "watchlist"         # the select follows the scan
        row = page.locator(".idea-table tbody tr").first
        symbol = row.locator("td strong").first.inner_text().strip()
        row.locator("button:text('Simulate')").click()
        page.wait_for_selector(".outcome-tiles", timeout=60000)
        body = page.inner_text(".sim-panel")
        assert "Stopped out" in body and "20,000 paths" in body and "not a forecast" in body
        assert page.locator(".sim-panel svg[role=img]").count() == 2
        # Another exit plan: half out at each target.
        page.select_option('select[aria-label="Exit plan"]', "split")
        page.click(".sim-panel button:text-is('Simulate')")
        page.wait_for_selector("text=Half out at target 1, the rest at target 2", timeout=60000)
        stop = float(page.input_value('.sim-panel input[aria-label="Stop"]'))
        page.click("text=Open the thesis on Decide")
        page.wait_for_function(f"state.thesis && state.thesis.symbol === '{symbol}' && location.hash === '#analyze'", timeout=30000)
        assert page.evaluate("state.thesis.invalidation") == pytest.approx(stop, abs=0.01)
    assert errors == []



@pytest.mark.skipif(_browser() is None, reason="Playwright is not installed")
def test_simulate_a_trade_still_draws_after_trade_ideas_loads(tmp_path):
    """2.7 review: a function of the same name in a later script replaced the simulator's renderer."""
    with demo_page(tmp_path) as (page, errors):
        page.goto(page.base + "#simulate")
        page.wait_for_selector('.legs select[aria-label="Strike"] option[value]', state="attached", timeout=30000)
        page.click("section:not(.hidden) button:text-is('Simulate')")
        page.wait_for_selector("section:not(.hidden) .sim-controls:not(.hidden)", timeout=30000)
        assert all(not t.strip() for t in page.locator("section:not(.hidden) .error").all_inner_texts())
        assert page.locator("section:not(.hidden) svg[role=img]").count() >= 1
        # Trade ideas waits to be asked: opening it starts nothing.
        page.click('.rail button.nav[data-view="ideas"]')
        page.wait_for_selector("text=Not scanned yet", timeout=30000)
        assert page.request.get(page.base + "v2/ideas?brief=true").json()["job"]["state"] == "idle"
    assert errors == []
