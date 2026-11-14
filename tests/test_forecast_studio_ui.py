"""4.7 Forecast Studio in a real browser: the study's one result on the linked chart (the legend as the one layer
control, per study; hidden lines silent on hover), the Scenario Lab's unsaved preview and saved exploratory version,
one comparison, back to the official version, the decision's case emphasised, a new stock clearing everything,
daily bars only, the What-If explorer's lines kept out of the way, and the Trading Desk's link back to the study.
Runs where Playwright and Chromium are installed (CI installs them)."""

from __future__ import annotations

import pytest

from tests.test_static_assets import _browser, demo_page
from tests.test_workstation import open_research, widget

pytestmark = pytest.mark.skipif(_browser() is None, reason="Playwright is not installed")
SHOWN = "section.ws-tabset:not(.hidden)"
CHART = """(() => { const sw = WS.allWidgets().find(w => w.type === 'study' && !w.disposed && w.node.closest('section.ws-tabset:not(.hidden)'));
  return sw && WS.find(sw, 'chart'); })()"""
LINES = f"(() => {{ const ch = {CHART}; return ch ? ch.lines('forecast') : null; }})()"


def _demo_full(svc):
    """A full study by default with the demo provider, and room in the limits for setups (as the 4.4 tests)."""
    from dataclasses import replace
    svc.db.cache_put("research:scenario_settings", {"provider": "demo", "auto": True})
    svc.settings = replace(svc.settings, scenario_budget=5000.0, scenario_max_loss=1000.0)


def _run_full(page):
    open_research(page)
    study = widget(page, "study")
    study.locator(".ws-study-kind input[value='full']").wait_for(timeout=15000, state="attached")
    study.locator("button:text-is('Run the study')").click()
    page.wait_for_selector(f"{SHOWN} .ws-widget[data-type='study'] .ws-lifecycle[data-state='decision_ready']", timeout=90000)
    page.wait_for_function(f"(() => {{ const l = {LINES}; return l && l['forecast:case:bullish']; }})()", timeout=20000)
    return study


def _last_session_time(page):
    return page.evaluate("(() => { const s = WS.ctx.blue.scenario.sessions; const d = s[s.length - 1].date; "
                         "return Date.UTC(+d.slice(0, 4), +d.slice(5, 7) - 1, +d.slice(8, 10), 9, 30) / 1000; })()")


def test_one_click_result_the_legend_controls_layers_per_study_and_hidden_lines_are_silent(tmp_path):
    """Tests 1, 6, 7 and 15: one click draws the base, its range and the cases with no Draw button; the official
    decision's case is emphasised; hiding Bearish hides its line and its hover note, is kept for that study only, and
    changes nothing saved."""
    with demo_page(tmp_path, prepare=_demo_full) as (page, errors):
        study = _run_full(page)
        assert study.locator("button:text-is('Draw on the linked chart')").count() == 0
        lines = page.evaluate(LINES)
        assert {"forecast:base", "forecast:low", "forecast:high", "forecast:case:bullish", "forecast:case:bearish"} <= set(lines)
        assert lines["forecast:low"]["style"] == 1 and lines["forecast:case:bullish"]["style"] == 2 and lines["forecast:base"]["style"] == 0
        assert lines["forecast:note:central"]["noLine"] and lines["forecast:note:central"]["legend"] == "Central = Base"
        b = page.evaluate("api('/v3/studies/' + WS.ctx.blue.study + '/result')")
        d = b["decision"]
        assert d["kind"] == "official" and d["version_id"] == b["selected_context"]["version_id"]
        if d.get("focus_case"):
            assert lines[f"forecast:case:{d['focus_case']}"]["width"] == 3 and "◆ decision" in lines[f"forecast:case:{d['focus_case']}"]["legend"]
            others = [k for k in ("bullish", "bearish") if k != d["focus_case"]]
            assert all("◆" not in lines[f"forecast:case:{k}"]["legend"] for k in others)
        legend = widget(page, "chart").locator(".lw-legend")
        assert "Catalyst v1 · official" in legend.inner_text()
        # Hide Bearish from the legend: its line and its hover note go; the saved version is untouched.
        t = _last_session_time(page)
        page.evaluate(f"(() => {{ const ch = {CHART}; ch.hover({t}); }})()")
        assert "Bearish: $" in page.evaluate(f"{CHART}.readout()")
        legend.locator(".lw-legend-item[data-line='forecast:case:bearish']").click()
        sid = page.evaluate("WS.ctx.blue.study")
        assert page.evaluate(f"{CHART}.shown('forecast:case:bearish', '{sid}')") is False
        page.evaluate(f"(() => {{ const ch = {CHART}; ch.hover({t}); }})()")
        ro = page.evaluate(f"{CHART}.readout()")
        assert "Bearish: $" not in ro and "Bullish: $" in ro and "Base forecast: $" in ro
        page.wait_for_timeout(500)
        assert page.evaluate(f"api('/v3/studies/{sid}/presentation')")["visible_layers"]["bearish"] is False
        assert page.evaluate(f"api('/v3/scenario-versions/{b['selected_context']['version_id']}')")["cases"] == b["selected_version"]["cases"]
        # A second study of the same stock starts with its own preferences: Bearish shown.
        study.locator("details.ws-more > summary:text-is('Study settings')").click()
        study.locator("button:text-is('Run the study')").click()
        page.wait_for_function(f"WS.ctx.blue.study && WS.ctx.blue.study !== '{sid}'", timeout=20000)
        page.wait_for_selector(f"{SHOWN} .ws-widget[data-type='study'] .ws-lifecycle[data-state='decision_ready']", timeout=90000)
        page.wait_for_function(f"(() => {{ const l = {LINES}; return l && l['forecast:case:bearish'] && WS.ctx.blue.scenario.study !== '{sid}'; }})()", timeout=20000)
        sid2 = page.evaluate("WS.ctx.blue.study")
        assert page.evaluate(f"{CHART}.shown('forecast:case:bearish', '{sid2}')") is True
    assert errors == []


def test_preview_save_compare_and_return_to_the_official_version(tmp_path):
    """Tests 8, 9, 10, 11 and 12: an edit is an unsaved dotted line (no version, nothing in the saved context); Save
    version makes an exploratory version, valued and assessed as research only, drawn as the selected one; one
    comparison is drawn fainter; Back to the official version restores the official decision."""
    with demo_page(tmp_path, prepare=_demo_full) as (page, errors):
        study = _run_full(page)
        sid = page.evaluate("WS.ctx.blue.study")
        n_versions = lambda: len(page.evaluate(f"api('/v3/studies/{sid}/result')")["versions"])      # noqa: E731
        study.locator(".ws-sections button:text-is('Forecast & Scenarios')").click()
        study.locator(".ws-lab input[value='bullish']").check()
        page.wait_for_selector(f"{SHOWN} .ws-lab input[aria-label='Bullish stock-specific move']", timeout=10000)
        study.locator(".ws-lab input[aria-label='Bullish stock-specific move']").fill("9")
        page.wait_for_function(f"(() => {{ const l = {LINES}; return l && l['forecast:preview:bullish']; }})()", timeout=15000)
        pv = page.evaluate(LINES)["forecast:preview:bullish"]
        assert pv["style"] == 1 and pv["legend"] == "Bullish (unsaved)" and "unsaved" in pv["legendGroup"]
        assert "Unsaved preview" in study.locator(".ws-lab-status").inner_text()
        assert "preview" not in page.evaluate("JSON.stringify(WS.ctx.blue)") and n_versions() == 1
        # Discard: the preview goes, nothing was saved.
        study.locator(".ws-lab button:text-is('Discard')").click()
        page.wait_for_function(f"(() => {{ const l = {LINES}; return l && l['forecast:case:bullish'] && !l['forecast:preview:bullish']; }})()", timeout=15000)
        assert n_versions() == 1
        # Again, and Save version.
        page.wait_for_selector(f"{SHOWN} .ws-lab input[aria-label='Bullish stock-specific move']", timeout=10000)
        study.locator(".ws-lab input[aria-label='Bullish stock-specific move']").fill("9")
        page.wait_for_function(f"(() => {{ const l = {LINES}; return l && l['forecast:preview:bullish']; }})()", timeout=15000)
        study.locator(".ws-lab button:text-is('Save version')").click()
        page.wait_for_function("WS.ctx.blue.scenario && WS.ctx.blue.scenario.catalyst && WS.ctx.blue.scenario.catalyst.kind === 'exploratory'", timeout=30000)
        page.wait_for_function(f"(() => {{ const l = {LINES}; return l && !l['forecast:preview:bullish']; }})()", timeout=15000)
        lines = page.evaluate(LINES)
        assert lines["forecast:case:bullish"]["legendGroup"] == "Catalyst v2 · exploratory"
        assert all("◆" not in lines[k].get("legend", "") for k in lines)             # an exploration is never the decision's case
        study.locator(".ws-sections button:text-is('Summary')").click()
        card = study.locator("[data-section='summary'] .ws-decision")
        assert card.get_attribute("data-kind") == "exploratory" and "Research only" in card.inner_text()
        assert "under these assumptions" in card.inner_text() and card.locator("button:text-is('Review trade')").count() == 0
        # Compare with v1: drawn fainter, read only.
        study.locator(".ws-sections button:text-is('Forecast & Scenarios')").click()
        study.locator(".ws-versions select[aria-label='Compare with']").select_option(index=1)
        page.wait_for_function(f"(() => {{ const l = {LINES}; return l && l['forecast:compare:bullish']; }})()", timeout=15000)
        cmp_ = page.evaluate(LINES)["forecast:compare:bullish"]
        assert cmp_["width"] == 1 and cmp_["legendGroup"] == "Compare: v1"
        # Back to the official version: its decision, no comparison of itself.
        study.locator(".ws-versions button:text-is('Back to the official version')").click()
        page.wait_for_function("WS.ctx.blue.scenario.catalyst.kind === 'official'", timeout=15000)
        study.locator(".ws-sections button:text-is('Summary')").click()
        assert study.locator("[data-section='summary'] .ws-decision").get_attribute("data-kind") == "official"
    assert errors == []


def test_a_new_stock_clears_every_forecast_layer_and_session_paths_stay_on_daily_bars(tmp_path):
    """Tests 13 and 23: switching stocks clears the forecast and an unsaved preview at once; on weekly bars the chart
    says the forecast is in sessions and offers the daily view instead of drawing it."""
    with demo_page(tmp_path, prepare=_demo_full) as (page, errors):
        study = _run_full(page)
        # Weekly bars: no forecast lines, a notice with the way back.
        chart = widget(page, "chart")
        chart.locator("select[aria-label='Group bars by']").select_option("1w")
        page.wait_for_selector(f"{SHOWN} .ws-widget[data-type='chart'] .lw-notice:not([hidden])", timeout=20000)
        assert "trading sessions" in chart.locator(".lw-notice").inner_text()
        page.wait_for_function(f"(() => {{ const l = {LINES}; return l && !l['forecast:base']; }})()", timeout=10000)
        chart.locator(".lw-notice button:text-is('Show daily bars')").click()
        page.wait_for_function(f"(() => {{ const l = {LINES}; return l && l['forecast:base']; }})()", timeout=20000)
        assert chart.locator(".lw-notice").is_hidden()
        # An unsaved preview, then another stock: nothing of the old study is drawn or kept.
        study.locator(".ws-sections button:text-is('Forecast & Scenarios')").click()
        study.locator(".ws-lab input[value='bearish']").dispatch_event("click")
        page.wait_for_selector(f"{SHOWN} .ws-lab input[aria-label='Bearish stock-specific move']", timeout=10000)
        study.locator(".ws-lab input[aria-label='Bearish stock-specific move']").fill("-8")
        page.wait_for_function(f"(() => {{ const l = {LINES}; return l && l['forecast:preview:bearish']; }})()", timeout=15000)
        page.evaluate("WS.set('blue', { symbol: 'MSFT' })")
        page.wait_for_function("WS.ctx.blue.symbol === 'MSFT' && !WS.ctx.blue.scenario && !WS.ctx.blue.study", timeout=10000)
        page.wait_for_function(f"(() => {{ const l = {LINES}; return l && Object.keys(l).length === 0; }})()", timeout=20000)
        page.wait_for_selector(f"{SHOWN} .ws-widget[data-type='study'] :text('Run a study of MSFT')", timeout=20000)
        assert study.locator(".ws-lab").count() == 0
    assert errors == []


def test_the_what_if_lines_stay_out_of_the_way_and_the_desk_links_back_to_the_study(tmp_path):
    """Part V 6.6 and part VIII 9.2: the advanced What-If explorer does not draw its orange and purple lines beside a
    study's paths unless asked, and takes them away when closed; a draft from the study names its version and opens the
    study again at that version."""
    with demo_page(tmp_path, prepare=_demo_full) as (page, errors):
        study = _run_full(page)
        sid = page.evaluate("WS.ctx.blue.study")
        page.evaluate("WS.addToActive('scenario', 'blue')")
        sc = widget(page, "scenario")
        sc.locator("input[aria-label='Draw the What-If paths on the chart']").wait_for(timeout=20000)
        assert not sc.locator("input[aria-label='Draw the What-If paths on the chart']").is_checked()
        page.wait_for_timeout(2500)
        assert page.evaluate(f"Object.keys({CHART}.lines('whatif'))") == []
        sc.locator("input[aria-label='Draw the What-If paths on the chart']").check()
        page.wait_for_function(f"Object.keys({CHART}.lines('whatif')).length > 0", timeout=30000)
        sc.locator("button[aria-label='Close']").click()
        page.wait_for_function(f"Object.keys({CHART}.lines('whatif')).length === 0", timeout=10000)
        assert page.evaluate(f"Object.keys({CHART}.lines('forecast')).length") > 0
        # A strategy drafted under the official version; the desk shows what it was chosen under and links back.
        b = page.evaluate(f"api('/v3/studies/{sid}/result')")
        study.locator(".ws-sections button:text-is('Strategies')").click()
        study.locator("[data-section='strategies'] tbody tr").filter(has_text="Buy shares").first.locator("button:text-is('Draft')").click()
        page.wait_for_function("WS.ctx.blue.draft && WS.ctx.blue.draft.strategy.key === 'shares'", timeout=20000)
        ref = page.evaluate("WS.ctx.blue.draft.source.scenario")
        assert ref["version_id"] == b["selected_context"]["version_id"] and ref["official"] is True and ref["valuation_id"] == b["valuation"]["id"]
        page.click(".ws-tab-btn[data-workspace='desk']")
        draft = page.locator(f"{SHOWN} .ws-widget[data-type='proposals'] .ws-draft")
        draft.wait_for(timeout=20000)
        assert "Chosen under" in draft.inner_text() and "official" in draft.inner_text()
        draft.locator("button:text-is('Open its study')").click()
        page.wait_for_selector(".ws-tab-btn[aria-selected='true'][data-workspace='research']", timeout=10000)
        page.wait_for_function(f"WS.ctx.blue.study === '{sid}' && WS.ctx.blue.scenario && WS.ctx.blue.scenario.catalyst.version_id === '{ref['version_id']}'",
                               timeout=20000)
    assert errors == []
