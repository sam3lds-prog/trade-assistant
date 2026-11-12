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
