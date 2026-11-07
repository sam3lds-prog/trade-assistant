"""3.0 phase 2: the workstation in a real browser. A stock or a contract picked in one widget reaches every widget
linked to it; widgets move, resize, collapse, maximize and close; the layout and the shared context survive a
reload; two charts show two stocks side by side; classic screens open as widgets. Runs where Playwright and
Chromium are installed (CI installs them)."""

from __future__ import annotations

import pytest

from tests.test_static_assets import _browser, demo_page

pytestmark = pytest.mark.skipif(_browser() is None, reason="Playwright is not installed")


def widget(page, kind: str, nth: int = 0):
    return page.locator(f"section.ws-tabset:not(.hidden) .ws-widget[data-type='{kind}'], #ws-dock .ws-widget[data-type='{kind}']").nth(nth)


def badge(page, kind: str, nth: int = 0) -> str:
    return widget(page, kind, nth).locator(".ws-badge").inner_text()


def open_research(page):
    page.goto(page.root + "#research")
    page.wait_for_selector(".ws-tab-btn[aria-selected='true'][data-workspace='research']", timeout=30000)
    page.wait_for_selector("section.ws-tabset:not(.hidden) .ws-widget[data-type='chain'] .ws-chain td.q", timeout=40000)


def palette(page, text: str):
    page.keyboard.press("Control+k")
    page.wait_for_selector("#ws-palette:not(.hidden) input", timeout=5000)
    page.fill("#ws-palette input", text)
    page.keyboard.press("Enter")


def saved(page):
    """Wait for the workstation's debounced save to reach the server."""
    page.wait_for_timeout(1200)
    page.evaluate("WS.saveNow()")


def test_the_workstation_opens_on_four_workspaces_with_the_status_strip(tmp_path):
    with demo_page(tmp_path) as (page, errors):
        page.goto(page.root)
        page.wait_for_selector(".ws-tab-btn", timeout=30000)
        assert page.eval_on_selector_all(".ws-tab-btn", "bs => bs.map(b => b.firstChild.textContent)") == \
            ["Discover", "Research", "Trading Desk", "Portfolio"]
        status = page.inner_text("#ws-status")
        assert "Made-up prices" in status and "Advisory: no orders are sent" in status
        page.wait_for_selector("#ws-live:has-text('Live updates')", timeout=15000)
        for kind in ("research", "desk", "portfolio", "discover"):
            page.click(f".ws-tab-btn[data-workspace='{kind}']")
            page.wait_for_selector(f"section.ws-tabset[data-workspace='{kind}']:not(.hidden) .ws-widget", timeout=20000)
            page.wait_for_timeout(600)
        assert page.locator("#ws-dock .ws-widget[data-type='ticket']").count() == 1      # the ticket stays in view
        assert page.locator("section.ws-tabset:not(.hidden) .ws-widget[data-type='classic:morning']").count() == 1
    assert errors == []


def test_a_stock_and_a_contract_picked_anywhere_reach_every_linked_widget(tmp_path):
    with demo_page(tmp_path) as (page, errors):
        open_research(page)
        palette(page, "MSFT")
        for kind in ("chart", "chain", "news"):
            page.wait_for_function(f"document.querySelector(\"section.ws-tabset:not(.hidden) .ws-widget[data-type='{kind}'] .ws-badge\").textContent === 'MSFT'",
                                   timeout=20000)
        page.wait_for_function("document.querySelector(\"#ws-dock .ws-widget[data-type='ticket'] .ws-badge\").textContent === 'MSFT'", timeout=20000)
        page.wait_for_selector("section.ws-tabset:not(.hidden) .ws-widget[data-type='chain'] .ws-chain td.q", timeout=30000)
        # A contract clicked in the chain is priced on the ticket and its risk drawn beside it.
        widget(page, "chain").locator(".ws-chain td.q").nth(1).click()
        page.wait_for_selector("#ws-dock .ws-widget[data-type='ticket'] :text('Fair price')", timeout=30000)
        assert badge(page, "ticket").startswith("MSFT ") and badge(page, "ticket").endswith(("CALL", "PUT"))
        page.wait_for_function("document.querySelector(\"section.ws-tabset:not(.hidden) .ws-widget[data-type='riskreward'] .ws-badge\").textContent.startsWith('MSFT ')",
                               timeout=20000)
        page.wait_for_selector("section.ws-tabset:not(.hidden) .ws-widget[data-type='riskreward'] svg[role=img]", timeout=30000)
        # The chosen contract is marked across its four cells (bid, ask, IV, delta), on one row.
        assert widget(page, "chain").locator("td.q.sel").count() == 4
        assert widget(page, "chain").locator("tr:has(td.q.sel)").count() == 1
        # The strategy builder's risk replaces the single contract on the risk panel.
        st = widget(page, "strategy")
        st.locator("button:text-is('Build')").click()
        st.locator("button:text-is('Show its risk')").click()
        page.wait_for_function("document.querySelector(\"section.ws-tabset:not(.hidden) .ws-widget[data-type='riskreward'] .ws-badge\").textContent === 'Bull call spread (debit)'",
                               timeout=30000)
        # Another workspace shares the link: Discover's chart is on MSFT too.
        page.click(".ws-tab-btn[data-workspace='discover']")
        page.wait_for_function("document.querySelector(\"section.ws-tabset:not(.hidden) .ws-widget[data-type='chart'] .ws-badge\").textContent === 'MSFT'",
                               timeout=20000)
    assert errors == []


def test_widgets_move_resize_collapse_maximize_and_close_and_the_layout_survives_a_reload(tmp_path):
    with demo_page(tmp_path) as (page, errors):
        open_research(page)
        before = page.locator("section.ws-tabset:not(.hidden) .ws-widget").count()
        page.click("#ws-add")
        page.click(".ws-menu button:text-is('Watchlist')")
        page.wait_for_selector("section.ws-tabset:not(.hidden) .ws-widget[data-type='watchlist'] tbody tr", timeout=20000)
        assert page.locator("section.ws-tabset:not(.hidden) .ws-widget").count() == before + 1
        # Collapse and expand.
        news = widget(page, "news")
        news.locator("button[aria-label='Collapse']").click()
        assert "collapsed" in news.get_attribute("class")
        news.locator("button[aria-label='Collapse']").click()
        assert "collapsed" not in news.get_attribute("class")
        # Maximize, then Escape restores.
        chart = widget(page, "chart")
        chart.locator("button[aria-label='Maximize']").click()
        page.wait_for_selector(".grid-stack-item.ws-max .ws-widget[data-type='chart']", timeout=5000)
        page.keyboard.press("Escape")
        assert page.locator(".grid-stack-item.ws-max").count() == 0
        # Resize by layout (the grid's own API: dragging handles is the library's, tested there).
        page.evaluate("""() => { const t = WS.tabs().get(WS.active()); const item = document.querySelector(
            "section.ws-tabset:not(.hidden) .ws-widget[data-type='strategy']").closest('.grid-stack-item'); t.grid.update(item, {w: 5, h: 7}); }""")
        # Close the news widget; put the chart on the green link.
        news.locator("button[aria-label='Close']").click()
        chart.locator(".ws-link").click()
        page.click(".ws-menu button:text-is('Green')")
        assert chart.get_attribute("data-group") == "green"
        saved(page)
        page.reload()
        open_research(page)
        types = page.eval_on_selector_all("section.ws-tabset:not(.hidden) .ws-widget", "ws => ws.map(w => w.dataset.type)")
        assert "news" not in types and "watchlist" in types and len(types) == before
        assert widget(page, "chart").get_attribute("data-group") == "green"
        size = page.evaluate("""() => { const i = document.querySelector("section.ws-tabset:not(.hidden) .ws-widget[data-type='strategy']")
            .closest('.grid-stack-item'); return [i.getAttribute('gs-w'), i.getAttribute('gs-h')]; }""")
        assert size == ["5", "7"]
    assert errors == []


def test_two_charts_show_two_stocks_side_by_side(tmp_path):
    with demo_page(tmp_path) as (page, errors):
        open_research(page)
        palette(page, "AAPL")
        page.click("#ws-add")
        page.click(".ws-menu button:text-is('Stock chart')")
        second = widget(page, "chart", 1)
        second.locator(".ws-link").click()
        page.click(".ws-menu button:text-is('Plum')")
        second.locator(".ws-whead").click()                      # focus it: the palette sends the stock to its link
        palette(page, "NVDA")
        page.wait_for_function("document.querySelectorAll(\"section.ws-tabset:not(.hidden) .ws-widget[data-type='chart'] .lw-chart canvas\").length >= 2",
                               timeout=30000)
        page.wait_for_function("[...document.querySelectorAll(\"section.ws-tabset:not(.hidden) .ws-widget[data-type='chart'] .ws-badge\")].map(b => b.textContent).join() === 'AAPL,NVDA'",
                               timeout=30000)
        assert badge(page, "chain") == "AAPL"                      # the chain follows the blue link, not the plum one
    assert errors == []


def test_classic_screens_open_as_widgets_and_analyze_opens_research(tmp_path):
    with demo_page(tmp_path) as (page, errors):
        page.goto(page.root + "#desk")
        page.wait_for_selector("section.ws-tabset[data-workspace='desk']:not(.hidden) .ws-widget[data-type='classic:paper'] h3",
                               timeout=30000)
        page.click("#ws-classic")
        page.click(".ws-menu button:text-is('Scanner')")
        page.wait_for_selector("section.ws-tabset:not(.hidden) .ws-widget[data-type='classic:scanner'] .ws-classic section", timeout=20000)
        # A classic screen can be open once; asking again shows the one that is open.
        page.click("#ws-classic")
        page.click(".ws-menu button:text-is('Scanner')")
        assert page.locator(".ws-widget[data-type='classic:scanner']").count() == 1
        # "Analyze" in a classic screen opens Research on that stock.
        page.evaluate("state.openDecide('KO', 'SHORT', 'manual')")
        page.wait_for_selector(".ws-tab-btn[aria-selected='true'][data-workspace='research']", timeout=10000)
        page.wait_for_function("document.querySelector(\"section.ws-tabset:not(.hidden) .ws-widget[data-type='chart'] .ws-badge\").textContent === 'KO'",
                               timeout=20000)
        assert page.evaluate("WS.ctx.blue.direction") == "SHORT"
        # And the 2.x screens are still a page of their own.
        page.goto(page.base)
        page.wait_for_function("document.querySelectorAll('#main > section').length > 0", timeout=20000)
    assert errors == []


def test_a_saved_layout_opens_as_a_new_workspace_tab(tmp_path):
    with demo_page(tmp_path) as (page, errors):
        open_research(page)
        page.click("#ws-layout")
        page.fill(".ws-menu input[aria-label='Layout name']", "Earnings study")
        page.click(".ws-menu button:text-is('Save as')")
        page.wait_for_selector(".ws-tab-btn:has-text('Earnings study')", timeout=10000)
        page.click("#ws-layout")
        page.click(".ws-menu button:text-is('New Portfolio')")
        page.wait_for_function("document.querySelectorAll('.ws-tab-btn').length === 5", timeout=10000)
        page.click("#ws-layout")
        page.wait_for_selector(".ws-menu button:has-text('Earnings study (Research)')", timeout=10000)
        page.click(".ws-menu button:has-text('Earnings study (Research)')")
        page.wait_for_function("document.querySelectorAll('.ws-tab-btn').length === 6", timeout=10000)
        assert page.locator("section.ws-tabset:not(.hidden) .ws-widget[data-type='chain']").count() == 1
    assert errors == []


def test_a_study_runs_draws_its_forecast_and_sends_a_contract_to_the_ticket_and_option_chart(tmp_path):
    """3.0 phase 3: one study of the linked stock; its forecast goes to the linked chart, a strategy row to the
    ticket and the option chart, and the study is still there after a reload."""
    with demo_page(tmp_path) as (page, errors):
        open_research(page)
        study = widget(page, "study")
        study.locator("button:text-is('Run the study')").click()
        page.wait_for_selector("section.ws-tabset:not(.hidden) .ws-widget[data-type='study'] button:text-is('Draw on the linked chart')", timeout=60000)
        text = study.inner_text()
        for words in ("Not calibrated", "Other paths to test against", "What would prove it wrong", "Strategies under the same scenarios",
                      "Missing or not yet trusted", "Similar past situations"):
            assert words in text, words
        study.locator("button:text-is('Draw on the linked chart')").click()
        page.wait_for_function("WS.ctx.blue.scenario && WS.ctx.blue.scenario.kind === 'forecast' && WS.ctx.blue.scenario.base.path_pct.length === 5",
                               timeout=10000)
        study.locator("tbody tr:has-text('Long call')").first.click()
        page.wait_for_function("document.querySelector(\"#ws-dock .ws-widget[data-type='ticket'] .ws-badge\").textContent.endsWith('CALL')", timeout=30000)
        page.wait_for_selector("section.ws-tabset:not(.hidden) .ws-widget[data-type='optionchart'] :text('Theoretical, not traded')", timeout=30000)
        assert widget(page, "optionchart").locator("canvas").count() >= 1
        study_id = page.evaluate("WS.ctx.blue.study")
        assert study_id and study_id.startswith("study_")
        saved(page)
        page.reload()
        open_research(page)
        page.wait_for_selector("section.ws-tabset:not(.hidden) .ws-widget[data-type='study'] :text('Strategies under the same scenarios')", timeout=30000)
        assert page.evaluate("WS.ctx.blue.study") == study_id
    assert errors == []


def test_discover_ranks_once_explains_the_score_splits_a_budget_and_studies_an_opportunity(tmp_path):
    """3.0 phase 4: the one feed, a picked opportunity's parts and autopilot reasons, the budget split across the
    feed, and "Study it", which Research then shows."""
    with demo_page(tmp_path) as (page, errors):
        page.goto(page.root + "#discover")
        feed = widget(page, "feed")
        feed.wait_for(timeout=30000)
        feed.locator("button:text-is('Refresh the scans')").click()
        page.wait_for_selector("section.ws-tabset:not(.hidden) .ws-widget[data-type='feed'] tbody tr", timeout=90000)
        rows = feed.locator("tbody tr")
        keys = page.eval_on_selector_all("section.ws-tabset:not(.hidden) .ws-widget[data-type='feed'] tbody tr",
                                         "trs => trs.map(t => t.children[1].textContent + t.children[2].textContent + t.children[3].textContent)")
        assert len(keys) == len(set(keys))                                   # each stock, direction and horizon once
        rows.first.click()
        page.wait_for_selector("section.ws-tabset:not(.hidden) .ws-widget[data-type='feed'] :text('How the score is made')", timeout=20000)
        text = feed.inner_text()
        for words in ("Historical support", "Setup quality", "Liquidity", "Conflict", "Not eligible: demo prices are made up", "What found it"):
            assert words in text, words
        opp = page.evaluate("WS.ctx.blue.opportunity")
        assert opp["id"].startswith("opp_") and page.evaluate("WS.ctx.blue.symbol") == opp["symbol"]
        alloc = widget(page, "allocator")
        alloc.locator("button:text-is('Split the budget')").click()
        page.wait_for_selector("section.ws-tabset:not(.hidden) .ws-widget[data-type='allocator'] :text('most it can lose')", timeout=60000)
        feed.locator("button:text-is('Study it')").click()
        page.wait_for_function("WS.ctx.blue.study && WS.ctx.blue.study.startsWith('study_')", timeout=20000)
        page.click(".ws-tab-btn[data-workspace='research']")
        page.wait_for_selector("section.ws-tabset:not(.hidden) .ws-widget[data-type='study'] :text('Strategies under the same scenarios')", timeout=90000)
        assert opp["symbol"] in widget(page, "study").locator("h3").first.inner_text()
    assert errors == []
