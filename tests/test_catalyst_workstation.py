"""4.1 Catalyst Scenarios in a real browser: a study's evidence exported (the packet downloads), the fixture's cases
imported, their assumptions applied, the cases drawn dashed beside the base at the stored prices and dates, the same
points after the chart refreshes and after a reload, and a new stock never showing the old one's scenarios.
Runs where Playwright and Chromium are installed (CI installs them)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from tests.test_static_assets import _browser, demo_page
from tests.test_workstation import open_research, saved, widget

pytestmark = pytest.mark.skipif(_browser() is None, reason="Playwright is not installed")
FIXTURE = Path(__file__).resolve().parent / "fixtures" / "catalyst_cases.json"
SHOWN = "section.ws-tabset:not(.hidden)"
# The linked chart's forecast lines, read through the chart's own API (a copy).
LINES = """(() => {
  const sw = WS.allWidgets().find(w => w.type === 'study' && !w.disposed && w.node.closest('section.ws-tabset:not(.hidden)'));
  const ch = sw && WS.find(sw, 'chart');
  return ch ? ch.lines('forecast') : null;
})()"""


def _points(lines, key):
    return [(p["time"], round(p["value"], 6)) for p in lines[key]["points"]]


def test_catalyst_scenarios_export_import_apply_draw_refresh_and_reload(tmp_path):
    with demo_page(tmp_path) as (page, errors):
        open_research(page)
        study = widget(page, "study")
        study.locator("button:text-is('Run the study')").click()
        page.wait_for_selector(f"{SHOWN} .ws-widget[data-type='study'] .ws-sections", timeout=60000)
        study.locator(".ws-sections button:text-is('Forecast')").click()
        cat = study.locator(".ws-catalyst")
        cat.locator("input[aria-label='Catalyst']").wait_for(timeout=15000)
        assert "No case has a probability" in cat.inner_text()
        cat.locator("input[aria-label='Catalyst']").fill("Quarterly results and outlook")
        # Export evidence: the packet downloads, and the packet's id is shown for the import.
        with page.expect_download() as dl:
            cat.locator("button:text-is('Export evidence')").click()
        assert dl.value.suggested_filename.endswith(".md") and "-catalyst-evidence-" in dl.value.suggested_filename
        packet = Path(dl.value.path()).read_text()
        assert "## Output template" in packet and "## Sources" in packet
        seed_id = cat.locator(".panel code").first.inner_text()
        assert seed_id.startswith("seed_") and seed_id in packet
        # A result for another packet is refused with the reason, and nothing changes.
        wrong = json.loads(FIXTURE.read_text())
        wrong["seed_id"] = "seed_someone_else"
        cat.locator("textarea[aria-label='Result JSON']").fill(json.dumps(wrong))
        cat.locator("button:text-is('Import cases')").click()
        page.wait_for_selector(f"{SHOWN} .ws-catalyst .error:has-text('not this study')", timeout=10000)
        # The fixture's cases: three cards, the uncited driver labelled a hypothesis, the report kept as text.
        good = json.loads(FIXTURE.read_text())
        good["seed_id"] = seed_id
        cat.locator("textarea[aria-label='Result JSON']").fill(json.dumps(good))
        cat.locator("textarea[aria-label='Report excerpt']").fill("<b>bold?</b> The swarm leaned bearish.")
        cat.locator("button:text-is('Import cases')").click()
        page.wait_for_selector(f"{SHOWN} .ws-catalyst .ws-case[data-case='bearish']", timeout=15000)
        assert cat.locator(".ws-case").count() == 3
        text = cat.inner_text()
        assert "Hypothesis: no exported source" in text and "Invalidated if: Management maintains or raises the outlook" in text
        assert cat.locator(".ws-cat-report").text_content().startswith("<b>bold?</b>") and cat.locator(".ws-cat-report b").count() == 0
        # The provider's cited sizes start the review; Apply assumptions saves version 1 and draws it.
        bull = cat.locator(".ws-case[data-case='bullish']")
        assert bull.locator("input[aria-label='Bullish stock-specific move']").input_value() == "4"
        cat.locator("button:text-is('Apply assumptions')").click()
        page.wait_for_function("WS.ctx.blue.scenario && WS.ctx.blue.scenario.catalyst && WS.ctx.blue.scenario.catalyst.version === 1", timeout=15000)
        page.wait_for_selector(f"{SHOWN} .ws-catalyst .ws-cat-version[data-version='1']", timeout=10000)
        page.wait_for_function(f"(() => {{ const l = {LINES}; return l && l['forecast:case:bullish']; }})()", timeout=15000)
        lines = page.evaluate(LINES)
        assert {"forecast:base", "forecast:case:bullish", "forecast:case:bearish"} <= set(lines)
        assert "forecast:case:central" not in lines                     # no quantified assumption: it is the base
        assert lines["forecast:case:bearish"]["style"] == 2 and lines["forecast:base"]["style"] == 0     # dashed beside solid
        run_id = page.evaluate("WS.ctx.blue.scenario.catalyst.run_id")
        v = page.evaluate(f"api('/v3/scenario-runs/{run_id}').then(r => r.latest)")
        anchor_time = page.evaluate(f"Date.UTC(...'{v['anchor']['bar_date']}'.split('-').map((x, i) => i === 1 ? x - 1 : +x), 9, 30) / 1000")
        for key in ("bullish", "bearish"):
            case = next(c for c in v["cases"] if c["key"] == key)
            pts = lines[f"forecast:case:{key}"]["points"]
            assert pts[0] == {"time": anchor_time, "value": v["anchor"]["price"]}
            assert [p["value"] for p in pts[1:]] == pytest.approx(case["price"])
        tips = lines["forecast:case:bearish"]["tips"]
        assert any("Bearish: $" in t and "session 5" in t and "if Management's outlook" in t for t in tips.values())
        before = {k: _points(lines, k) for k in ("forecast:base", "forecast:case:bullish", "forecast:case:bearish")}
        # The chart reloads its data: the stored points do not move.
        widget(page, "chart").locator("button[aria-label='Refresh']").click()
        page.wait_for_timeout(1500)
        page.wait_for_function(f"(() => {{ const l = {LINES}; return l && l['forecast:case:bullish']; }})()", timeout=15000)
        assert {k: _points(page.evaluate(LINES), k) for k in before} == before
        # A reload: the study, its scenario version and the same points come back.
        saved(page)
        page.reload()
        open_research(page)
        page.wait_for_selector(f"{SHOWN} .ws-widget[data-type='study'] .ws-sections", timeout=30000)
        widget(page, "study").locator(".ws-sections button:text-is('Forecast')").click()
        page.wait_for_selector(f"{SHOWN} .ws-catalyst .ws-cat-version[data-version='1']", timeout=20000)
        page.wait_for_function(f"(() => {{ const l = {LINES}; return l && l['forecast:case:bearish']; }})()", timeout=20000)
        assert {k: _points(page.evaluate(LINES), k) for k in before} == before
        # Another stock: its chart has no scenario lines and its study widget shows none of the old study's.
        page.evaluate("WS.set('blue', { symbol: 'MSFT' })")
        page.wait_for_function("WS.ctx.blue.symbol === 'MSFT' && !WS.ctx.blue.scenario && !WS.ctx.blue.study", timeout=10000)
        page.wait_for_selector(f"{SHOWN} .ws-widget[data-type='study'] :text('Run a study of MSFT')", timeout=20000)
        assert widget(page, "study").locator(".ws-catalyst").count() == 0
        page.wait_for_function(f"(() => {{ const l = {LINES}; return l && Object.keys(l).length === 0; }})()", timeout=20000)
    assert errors == []


def _demo_provider(svc):
    """Research settings: the demo provider, run automatically (as a person would set once in the study's settings)."""
    svc.db.cache_put("research:scenario_settings", {"provider": "demo", "auto": True})


def test_one_click_run_study_draws_the_cases_and_values_the_strategies(tmp_path):
    """4.3: one click on Run the study, nothing else: the study, then the provider in the background, then the cases
    drawn on the linked chart from their saved prices and the study's strategies valued under each case."""
    with demo_page(tmp_path, prepare=_demo_provider) as (page, errors):
        open_research(page)
        study = widget(page, "study")
        study.locator(".ws-scen-settings input[aria-label='Run scenarios']").wait_for(timeout=15000)
        assert study.locator("input[aria-label='Run scenarios']").is_checked()
        assert study.locator("select[aria-label='Scenario provider']").input_value() == "demo"
        study.locator("button:text-is('Run the study')").click()
        page.wait_for_selector(f"{SHOWN} .ws-widget[data-type='study'] .ws-pipeline[data-status='complete']", timeout=90000)
        panel = study.locator(".ws-pipeline")
        assert panel.locator(".ws-stage").count() == 4 and panel.locator(".ws-stage.done").count() == 4
        text = panel.inner_text()
        assert "Demo provider" in text and "nothing was simulated" in text and "Findings:" in text and "None has a probability" in text
        assert panel.locator(".ws-valuation table").count() >= 1 and "Cash" in panel.locator(".ws-valuation").inner_text()
        # 4.4: the decision, above the findings: here (the trial policy's $25 loss limit) No setup, with its reason.
        card = panel.locator(".ws-decision")
        assert card.count() == 1 and card.get_attribute("data-status") in ("qualified", "watch", "no_setup")
        assert "Demo example" in card.inner_text()
        if card.get_attribute("data-status") == "no_setup":
            assert "Why not:" in card.inner_text()
        else:
            assert "Invalidated if" in card.inner_text()
        # The saved version is drawn without an Apply click: dashed cases beside the solid base, at the stored prices.
        page.wait_for_function(f"(() => {{ const l = {LINES}; return l && l['forecast:case:bullish'] && l['forecast:case:bearish']; }})()", timeout=20000)
        lines = page.evaluate(LINES)
        assert lines["forecast:case:bullish"]["style"] == 2 and lines["forecast:base"]["style"] == 0
        vid = page.evaluate("WS.ctx.blue.scenario.catalyst.version_id")
        v = page.evaluate(f"api('/v3/scenario-versions/{vid}')")
        assert v["origin"] == "auto"
        bull = next(c for c in v["cases"] if c["key"] == "bullish")
        assert [p["value"] for p in lines["forecast:case:bullish"]["points"][1:]] == pytest.approx(bull["price"])
        # The manual tools are still there, under Advanced.
        study.locator(".ws-sections button:text-is('Forecast')").click()
        adv = study.locator("details.ws-advanced")
        assert adv.count() == 1 and adv.get_attribute("open") is None
        before = _points(lines, "forecast:case:bearish")
        # A reload: the study comes back finished, and the same points are drawn.
        saved(page)
        page.reload()
        open_research(page)
        page.wait_for_selector(f"{SHOWN} .ws-widget[data-type='study'] .ws-pipeline[data-status='complete']", timeout=30000)
        page.wait_for_function(f"(() => {{ const l = {LINES}; return l && l['forecast:case:bearish']; }})()", timeout=20000)
        assert _points(page.evaluate(LINES), "forecast:case:bearish") == before
    assert errors == []


def _demo_provider_wide_limits(svc):
    _demo_provider(svc)
    from dataclasses import replace
    svc.settings = replace(svc.settings, scenario_budget=5000.0, scenario_max_loss=1000.0)


def test_a_watch_decision_is_noticed_and_opens_its_study(tmp_path):
    """4.4: with room in the loss limit the demo study's cases make a Watch for trigger: a card with what, when, why and
    what invalidates it; a notice at the top of the workstation that opens the study; and Discover's scenario filter."""
    with demo_page(tmp_path, prepare=_demo_provider_wide_limits) as (page, errors):
        open_research(page)
        study = widget(page, "study")
        study.locator(".ws-scen-settings input[aria-label='Run scenarios']").wait_for(timeout=15000)
        study.locator("button:text-is('Run the study')").click()
        page.wait_for_selector(f"{SHOWN} .ws-widget[data-type='study'] .ws-decision[data-status='watch']", timeout=90000)
        card = study.locator(".ws-decision")
        text = card.inner_text()
        for words in ("Watch for trigger", "What", "When", "Why", "Invalidated if", "Only if"):
            assert words in text, words
        assert card.locator("button:text-is('Draft it')").count() == 1
        page.wait_for_selector("#ws-scen-notice [data-decision]", timeout=70000)
        assert "Watch for trigger" in page.locator("#ws-scen-notice").inner_text()
        page.locator("#ws-scen-notice button:text-is('Dismiss')").first.click()
        page.wait_for_selector("#ws-scen-notice", state="detached", timeout=10000)
        # Discover: the scenario observation, labelled, and never eligible for the autopilot.
        page.evaluate("WS.goWorkspace('discover')")
        feed = widget(page, "feed")
        feed.locator("select[aria-label='Setup']").wait_for(timeout=30000)
        feed.locator("select[aria-label='Setup']").select_option("scenario")
        page.wait_for_selector("section.ws-tabset:not(.hidden) .ws-widget[data-type='feed'] tbody tr", timeout=20000)
        feed.locator("tbody tr").first.click()
        page.wait_for_selector("section.ws-tabset:not(.hidden) .ws-opp .ws-decision", timeout=20000)
        detail = feed.locator(".ws-opp").inner_text()
        assert "not a historical edge" in detail and "Not eligible: scenario evidence only" in detail
    assert errors == []
