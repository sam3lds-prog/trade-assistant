"""3.1 in a real browser: every widget's information icon works by mouse, keyboard and touch; widgets come and go
without leaving their event subscriptions behind; the standard layouts; the tabs and the save state; a Research
strategy drafted, handed to the Trading Desk and proposed in one workflow (and one the desk cannot send, labelled
and blocked from it); an interrupted study offered again; and a new stock never showing the old one's study.
Runs where Playwright and Chromium are installed (CI installs them)."""

from __future__ import annotations

import pytest

from tests.test_static_assets import _browser, demo_page
from tests.test_workstation import open_research, saved, widget

pytestmark = pytest.mark.skipif(_browser() is None, reason="Playwright is not installed")
NATIVE_SECTIONS = ["What it does", "What it uses", "How to use it", "How to read it", "Limits and freshness", "What it changes"]


def visible(kind: str) -> str:
    return f"section.ws-tabset:not(.hidden) .ws-widget[data-type='{kind}']"


def test_every_widget_type_has_help_and_every_native_widget_opens_it_in_six_parts(tmp_path):
    with demo_page(tmp_path) as (page, errors):
        page.goto(page.root + "#portfolio")
        page.wait_for_selector(visible("holdings"), timeout=30000)
        assert page.evaluate("WS.missingHelp()") == []
        native = page.evaluate("WS.catalog().filter(d => !d.type.startsWith('classic:')).map(d => d.type)")
        assert len(native) >= 22                                                     # the 20 of 3.0, and Holdings and Exposure
        page.click("#ws-layout")
        page.click(".ws-menu button:text-is('New Research')")                       # a fresh tab to add them to
        page.wait_for_timeout(500)
        for kind in native:
            page.evaluate(f"WS.addToActive('{kind}')")
            w = page.locator(visible(kind)).last
            icon = w.locator(".ws-whead .ws-wtools > .ws-info")
            assert icon.count() == 1, kind
            assert page.evaluate(f"""document.querySelectorAll("{visible(kind)}")[document.querySelectorAll("{visible(kind)}").length - 1]
                .querySelector('.ws-wtools').firstElementChild.classList.contains('ws-info')"""), kind          # first among the controls
            icon.click()
            page.wait_for_selector(".ws-help-pop.pinned", timeout=5000)
            assert page.eval_on_selector_all(".ws-help-pop dt", "d => d.map(x => x.textContent)")[:6] == NATIVE_SECTIONS, kind
            assert page.locator(".ws-help-pop").count() == 1                         # one at a time
            page.keyboard.press("Escape")
            assert page.locator(".ws-help-pop").count() == 0
        # A classic screen's widget reuses its 2.x help, with what changed in 3.0.
        page.evaluate("WS.addToActive('classic:paper')")
        page.locator(visible("classic:paper")).locator(".ws-info").click()
        pop = page.locator(".ws-help-pop")
        assert "pretend money" in pop.inner_text() and "In 3.0:" in pop.inner_text() and "Separate from the Trading Desk" in pop.inner_text()
        # Collapsed and docked widgets keep the icon.
        page.keyboard.press("Escape")
        w = page.locator(visible("news")).last
        w.locator("button[aria-label='Collapse']").click()
        assert w.locator(".ws-info").is_visible()
        assert page.locator("#ws-dock .ws-widget .ws-info").count() == 1
    assert errors == []


def test_help_works_from_the_keyboard_closes_with_its_widget_and_fits_a_phone(tmp_path):
    with demo_page(tmp_path) as (page, errors):
        open_research(page)
        icon = widget(page, "chain").locator(".ws-info")
        icon.focus()
        page.keyboard.press("Shift+Tab")
        page.keyboard.press("Tab")                                                    # keyboard focus: a preview
        assert page.evaluate("WS.helpOpen()") == {"type": "chain", "pinned": False}
        page.keyboard.press("Enter")                                                  # Enter keeps it open, focus moves in
        assert page.evaluate("WS.helpOpen()") == {"type": "chain", "pinned": True}
        assert page.evaluate("document.activeElement.classList.contains('ws-help-pop')")
        assert page.get_attribute(".ws-help-pop", "role") == "dialog" and icon.get_attribute("aria-expanded") == "true"
        page.keyboard.press("Escape")                                                 # Escape closes and returns focus
        assert page.evaluate("WS.helpOpen()") is None and page.evaluate("document.activeElement.getAttribute('aria-label')") == "About Options chain"
        assert icon.get_attribute("aria-expanded") == "false"
        # Escape closes help before it restores a maximized widget.
        widget(page, "chart").locator("button[aria-label='Maximize']").click()
        page.locator(".grid-stack-item.ws-max .ws-info").click()
        page.keyboard.press("Escape")
        assert page.locator(".grid-stack-item.ws-max").count() == 1 and page.evaluate("WS.helpOpen()") is None
        page.keyboard.press("Escape")
        assert page.locator(".grid-stack-item.ws-max").count() == 0
        # Closing a widget closes its help; opening another's replaces it.
        widget(page, "news").locator(".ws-info").click()
        widget(page, "news").locator("button[aria-label='Close']").click()
        assert page.locator(".ws-help-pop").count() == 0
        # A phone: a tap opens it pinned, inside the screen.
        page.set_viewport_size({"width": 390, "height": 844})
        page.wait_for_timeout(600)
        widget(page, "chain").locator(".ws-info").click()
        page.wait_for_selector(".ws-help-pop", timeout=5000)
        box = page.eval_on_selector(".ws-help-pop", "p => { const r = p.getBoundingClientRect(); return [r.left, r.right, r.top, r.bottom]; }")
        assert box[0] >= 0 and box[1] <= 390 and box[2] >= 0 and box[3] <= 844
        page.mouse.click(5, 5)                                                         # a tap elsewhere closes it
        assert page.locator(".ws-help-pop").count() == 0
    assert errors == []


def test_widgets_closed_and_reopened_leave_no_event_subscriptions_behind(tmp_path):
    with demo_page(tmp_path) as (page, errors):
        page.goto(page.root + "#desk")
        page.wait_for_selector(visible("proposals"), timeout=30000)
        page.wait_for_timeout(800)
        before = page.evaluate("WS.handlerCount()")
        for _ in range(4):
            for kind in ("feed", "activity", "approvals", "proposals", "deskstatus", "deskpositions", "autopilot", "study", "forecastrecord"):
                page.evaluate(f"WS.addToActive('{kind}')")
            page.wait_for_timeout(300)
            assert page.evaluate("WS.handlerCount()") > before
            for kind in ("feed", "activity", "approvals", "proposals", "deskstatus", "deskpositions", "autopilot", "study", "forecastrecord"):
                page.locator(visible(kind)).last.locator("button[aria-label='Close']").click()
            assert page.evaluate("WS.handlerCount()") == before
        # Docking and undocking moves a widget without leaving its old self subscribed.
        page.locator(visible("approvals")).locator("button[aria-label='Pin to the dock']").click()
        page.locator("#ws-dock .ws-widget[data-type='approvals'] button[aria-label='Move into the workspace']").click()
        assert page.evaluate("WS.handlerCount()") == before
    assert errors == []


def test_the_31_standard_layouts(tmp_path):
    with demo_page(tmp_path) as (page, errors):
        page.goto(page.root)
        page.wait_for_selector(".ws-tab-btn", timeout=30000)
        layouts = page.evaluate("Object.fromEntries(Object.entries(WS.DEFAULT_LAYOUTS).map(([k, v]) => [k, v.map(w => w.type)]))")
        assert "classic:morning" not in layouts["discover"] and layouts["discover"][:2] == ["feed", "allocator"]
        assert "scenario" not in layouts["research"] and layouts["research"][:2] == ["chart", "study"]
        assert {"chain", "optionchart"} <= set(layouts["research"]) and "forecastrecord" in layouts["research"]
        assert layouts["desk"][:3] == ["approvals", "proposals", "deskstatus"] and "classic:paper" not in layouts["desk"]
        assert layouts["portfolio"][:2] == ["holdings", "exposure"]
        policy = next(w for w in page.evaluate("WS.DEFAULT_LAYOUTS.desk") if w["type"] == "policy")
        assert policy["collapsed"] is True
        # Every standard layout fits in under 30 rows (3.0's ran to 46): less scrolling.
        assert max(max(w["y"] + w["h"] for w in v) for v in page.evaluate("Object.values(WS.DEFAULT_LAYOUTS)")) < 30
        # The scenario explorer is an advanced panel in the catalog.
        cat = page.evaluate("WS.catalog().filter(d => d.type === 'scenario').map(d => [d.category, d.title])")
        assert cat == [["Advanced", "Scenario explorer (advanced)"]]
        # The status bar keeps a kill switch in view.
        assert page.locator("#ws-kill").count() == 1
    assert errors == []


def test_tabs_are_keyboard_tabs_with_a_separate_close_button_and_the_save_state_shows(tmp_path):
    with demo_page(tmp_path) as (page, errors):
        page.goto(page.root + "#discover")
        page.wait_for_selector(visible("feed"), timeout=30000)
        assert page.locator(".ws-tab-btn .ws-x, .ws-tab-btn button, .ws-tab-btn [role=button]").count() == 0     # nothing nested
        assert page.locator(".ws-tab > button.ws-x").count() == 4
        page.focus(".ws-tab-btn[aria-selected='true']")
        page.keyboard.press("ArrowRight")
        assert page.get_attribute(".ws-tab-btn[aria-selected='true']", "data-workspace") == "research"
        assert page.evaluate("document.activeElement.dataset.workspace") == "research"
        page.keyboard.press("End")
        assert page.get_attribute(".ws-tab-btn[aria-selected='true']", "data-workspace") == "portfolio"
        page.keyboard.press("Delete")                                               # Delete closes the focused workspace
        assert page.locator(".ws-tab-btn").count() == 3
        page.wait_for_selector("#ws-save[data-state='saved']", timeout=10000)
        assert page.inner_text("#ws-save") == "Saved"
        # A failed save says so, and is retried.
        page.route("**/v3/workspaces/session", lambda r: r.fulfill(status=500, body='{"error": "disk full"}', content_type="application/json"))
        page.evaluate("WS.set('blue', { symbol: 'KO' })")
        page.wait_for_selector("#ws-save[data-state='error']", timeout=10000)
        assert "Not saved" in page.inner_text("#ws-save") and "disk full" in page.get_attribute("#ws-save", "title")
        page.unroute("**/v3/workspaces/session")
        page.click("#ws-save button:text-is('Retry')")
        page.wait_for_selector("#ws-save[data-state='saved']", timeout=10000)
        # Saves are sent one after another, the newest last.
        page.evaluate("Promise.all([WS.saveNow(), (WS.set('blue', { symbol: 'PEP' }), WS.saveNow())])")
        saved(page)
        page.reload()
        page.wait_for_selector(".ws-tab-btn", timeout=30000)
        assert page.evaluate("WS.ctx.blue.symbol") == "PEP"
    assert errors == []


def test_a_session_from_30_is_offered_the_new_layouts_once(tmp_path):
    def old_session(svc):
        svc.db.cache_put("workstation:session:v1", {"tabs": [{"id": "t1", "workspace": "discover", "name": "Discover", "widgets": [
            {"id": "w1", "type": "feed", "x": 0, "y": 0, "w": 8, "h": 11, "group": "blue"},
            {"id": "w2", "type": "classic:morning", "x": 0, "y": 11, "w": 12, "h": 11, "group": "none"}]}],
            "active": "t1", "dock": {}, "context": {}})
    with demo_page(tmp_path, prepare=old_session) as (page, errors):
        page.goto(page.root)
        page.wait_for_selector(".ws-notice button:text-is('Use the new layouts')", timeout=30000)
        assert page.locator(visible("classic:morning")).count() == 1                 # left as it was until asked
        page.click(".ws-notice button:text-is('Use the new layouts')")
        page.wait_for_selector(visible("allocator"), timeout=20000)
        assert page.locator(visible("classic:morning")).count() == 0
        saved(page)
        page.reload()
        page.wait_for_selector(visible("feed"), timeout=30000)
        assert page.locator(".ws-notice").count() == 0
    assert errors == []


@pytest.mark.clock("2026-10-22T10:30")
def test_a_strategy_goes_from_the_study_to_a_trading_desk_proposal_with_its_context(tmp_path):
    with demo_page(tmp_path) as (page, errors):
        open_research(page)
        page.evaluate("WS.set('blue', { symbol: 'AMZN' })")
        study = widget(page, "study")
        page.wait_for_function("document.querySelector(\"section.ws-tabset:not(.hidden) .ws-widget[data-type='study'] .ws-badge\").textContent === 'AMZN'")
        study.locator("button:text-is('Run the study')").click()
        page.wait_for_selector(visible("study") + " .ws-sections", timeout=90000)
        study.locator(".ws-sections button:text-is('Strategies')").click()
        rows = study.locator("[data-section='strategies'] tbody tr")
        names = [t.replace("✓", "").strip() for t in rows.locator("td:first-child").all_inner_texts()]       # ✓ marks the best
        routes = dict(zip(names, rows.locator(".ws-route").all_inner_texts()))
        assert routes["Buy shares"] == "Desk can send" and routes["Long call"] == "Research only"        # demo prices
        assert all(v in ("Desk can send", "By hand", "Research only") for v in routes.values())
        # An option strategy on demo prices: drafted, labelled research only, and there is nothing to propose.
        rows.filter(has_text="Long call").first.locator("button:text-is('Draft')").click()
        page.wait_for_function("WS.ctx.blue.draft && WS.ctx.blue.draft.strategy.key === 'long_call'", timeout=20000)
        study.locator("button:text-is('Open the Trading Desk')").click()
        draft = page.locator(visible("proposals") + " .ws-draft")
        draft.wait_for(timeout=20000)
        assert draft.get_attribute("data-route") == "research" and draft.locator("button:text-is('Propose this draft')").count() == 0
        assert "demo" in draft.inner_text()
        # The shares strategy: sized to the policy, its context carried, proposed in one click, approved, filled.
        page.click(".ws-tab-btn[data-workspace='research']")
        study.locator("[data-section='strategies'] tbody tr").filter(has_text="Buy shares").first.locator("button:text-is('Draft')").click()
        page.wait_for_function("WS.ctx.blue.draft && WS.ctx.blue.draft.strategy.key === 'shares'", timeout=20000)
        page.click(".ws-tab-btn[data-workspace='desk']")
        draft = page.locator(visible("proposals") + " .ws-draft[data-route='automatic']")
        draft.wait_for(timeout=20000)
        text = draft.inner_text()
        for words in ("Desk can send", "study_", "fits the policy", "Most it can lose", "Modeled downside", "Capital it ties up",
                      "not an order", "Would pass", "model-implied 10th to 90th percentile"):
            assert words in text, words
        draft.locator("button:text-is('Propose this draft')").click()
        page.wait_for_selector(visible("approvals") + " button:text-is('Approve')", timeout=20000)
        props = page.evaluate("fetch('/v3/proposals?limit=5').then(r => r.json()).then(d => d.proposals)")
        study_id = page.evaluate("WS.ctx.blue.study")
        assert props[0]["source"] == "research" and props[0]["study_id"] == study_id and props[0]["symbol"] == "AMZN"
        widget(page, "approvals").locator("button:text-is('Approve')").first.click()
        page.wait_for_selector(visible("deskpositions") + " :text('AMZN')", timeout=30000)
        assert "Yours" in widget(page, "deskpositions").inner_text()              # not software-monitored: your own exit
        # The draft survives a reload.
        saved(page)
        page.goto(page.root + "#desk")
        page.reload()
        page.wait_for_selector(visible("proposals") + " .ws-draft", timeout=30000)
        assert page.evaluate("WS.ctx.blue.draft.proposed") == props[0]["id"]
    assert errors == []


def test_an_interrupted_study_says_so_and_runs_again_and_a_new_stock_never_shows_the_old_study(tmp_path):
    def orphan(svc):
        from tradeassist import contracts as C
        from tradeassist.v3 import research as R
        req = R.StudyIn(symbol="MSFT", horizon_days=5, thesis="swing")
        with svc.db.tx() as cur:
            cur.execute("""INSERT INTO studies (id, mode, symbol, created_at, as_of, horizon_days, thesis, status, engine_version,
                           contract_version, inputs_json, evidence_json, fingerprint, worker, heartbeat_at)
                           VALUES ('study_cut_off', ?, 'MSFT', ?, ?, 5, 'swing', 'running', ?, ?, ?, '{}', '', 'gone-mac:1', '2026-01-01T00:00:00+00:00')""",
                        (svc.market.info.mode, C.utc_now(), C.utc_now(), R.ENGINE_VERSION, C.CONTRACT_VERSION, req.model_dump_json()))
    with demo_page(tmp_path, prepare=orphan) as (page, errors):
        open_research(page)
        page.evaluate("WS.set('blue', { symbol: 'MSFT', study: 'study_cut_off' })")
        study = widget(page, "study")
        page.wait_for_selector(visible("study") + " :text('The study did not finish')", timeout=20000)
        assert "Interrupted" in study.inner_text()
        study.locator("button:text-is('Run it again')").click()
        page.wait_for_selector(visible("study") + " .ws-sections", timeout=90000)
        new_id = page.evaluate("WS.ctx.blue.study")
        assert new_id.startswith("study_") and new_id != "study_cut_off"
        assert "MSFT" in study.locator("h3").first.inner_text()
        # Another stock: the MSFT study is not shown for it.
        page.evaluate("WS.set('blue', { symbol: 'KO' })")
        page.wait_for_selector(visible("study") + " :text('Run a study of KO')", timeout=10000)
        assert study.locator(".ws-sections").count() == 0 and page.evaluate("WS.ctx.blue.study") is None
        assert page.evaluate("WS.ctx.blue.draft") is None
    assert errors == []
