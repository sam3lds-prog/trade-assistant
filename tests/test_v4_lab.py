"""4.0 phase 5 in a real browser: Model Lab, Model opportunities in Discover and Model forecast in Research, over a
finished replay, its deployment and a shadow forecast. Runs where Playwright and Chromium are installed (CI installs
them)."""

from __future__ import annotations

from datetime import date, datetime

import pandas as pd
import pytest

from tests import v4_fixtures as fx
from tests.test_static_assets import _browser, demo_page
from tests.test_workstation import saved, widget

pytestmark = pytest.mark.skipif(_browser() is None, reason="Playwright is not installed")


def _world(svc):
    from tradeassist.intelligence.data import dataset as DS
    from tradeassist.intelligence.learning.deploy import deploy
    from tradeassist.intelligence.live import shadow_step
    from tradeassist.intelligence.replay.runner import Replay, create_run
    from tradeassist.optionmath import NY
    from tradeassist.v4 import replay_store
    store = replay_store(svc)
    bars = fx.calendar_market(first="2022-01-03", last="2026-03-31", reversion=0.5)
    ds = DS.freeze(store, bars, fx.universe(), source="test", adjustment="none")
    run_id = create_run(store, ds.id, fx.config(date(2023, 3, 1), date(2025, 10, 31), families=("ridge",), retrain="monthly"))
    Replay(store, run_id).run()
    deploy(store, run_id, "browser test")

    def fetch(sym, start, end):
        df = bars[sym]
        return df[(df["date"] >= pd.Timestamp(start)) & (df["date"] <= pd.Timestamp(end))]
    for d in (date(2026, 3, 26), date(2026, 3, 27), date(2026, 3, 30)):
        shadow_step(store, fetch, d, datetime(d.year, d.month, d.day, 17, 30, tzinfo=NY))
    svc._test_run = run_id


def test_model_lab_shows_the_replay_its_lessons_and_the_forward_record(tmp_path):
    holder = {}

    def prepare(svc):
        _world(svc)
        holder["svc"] = svc
    with demo_page(tmp_path, prepare=prepare) as (page, errors):
        page.goto(page.root)
        page.wait_for_selector(".ws-tab-btn", timeout=30000)
        assert page.locator(".ws-tab > button.ws-x").count() == 4                    # Model Lab is not forced open
        page.click("#ws-lab-open")
        page.wait_for_selector(".ws-tab-btn[aria-selected='true'][data-workspace='lab']", timeout=10000)
        for kind in ("lab_runs", "lab_shadow", "lab_curve", "lab_registry", "lab_errors", "lab_trades"):
            assert widget(page, kind).count() == 1, kind
        page.wait_for_selector("section.ws-tabset:not(.hidden) .ws-widget[data-type='lab_runs'] table", timeout=20000)
        runs = widget(page, "lab_runs").inner_text()
        assert "2023-03-01 to 2025-10-31" in runs and "Complete" in runs and "Result:" in runs
        page.wait_for_selector("section.ws-tabset:not(.hidden) .ws-widget[data-type='lab_curve'] svg.ws-curve rect", timeout=20000)
        assert "months beat" in widget(page, "lab_curve").inner_text()
        page.wait_for_selector("section.ws-tabset:not(.hidden) .ws-widget[data-type='lab_registry'] table", timeout=20000)
        reg = widget(page, "lab_registry").inner_text()
        assert "Active" in reg and "Final champions" in reg
        page.wait_for_selector("section.ws-tabset:not(.hidden) .ws-widget[data-type='lab_shadow'] h4", timeout=20000)
        shadow = widget(page, "lab_shadow").inner_text()
        assert "Replayed only" in shadow and "2026-03-30" in shadow and "graded" in shadow
        page.wait_for_selector("section.ws-tabset:not(.hidden) .ws-widget[data-type='lab_errors'] table", timeout=20000)
        page.wait_for_selector("section.ws-tabset:not(.hidden) .ws-widget[data-type='lab_trades'] table", timeout=20000)
        # The session with Model Lab open is saved like any other.
        saved(page)
        # Discover and Research get the models' widgets from the forward record.
        page.click("section.ws-tabset:not(.hidden) .ws-widget[data-type='lab_shadow'] button:text-is('Show in Discover')")
        page.wait_for_selector(".ws-tab-btn[aria-selected='true'][data-workspace='discover']", timeout=10000)
        page.wait_for_selector("section.ws-tabset:not(.hidden) .ws-widget[data-type='model_ideas'] table", timeout=20000)
        ideas = widget(page, "model_ideas").inner_text()
        assert "Frozen after the close of 2026-03-30" in ideas and "Leaning hardest" in ideas
        widget(page, "model_ideas").locator("table").last.locator("tbody tr").first.click()
        picked = page.evaluate("WS.ctx.blue.symbol")
        page.click(".ws-tab-btn[data-workspace='lab']")
        page.wait_for_selector(".ws-tab-btn[aria-selected='true'][data-workspace='lab']", timeout=10000)
        page.click("section.ws-tabset:not(.hidden) .ws-widget[data-type='lab_shadow'] button:text-is('Show in Research')")
        page.wait_for_selector(".ws-tab-btn[aria-selected='true'][data-workspace='research']", timeout=10000)
        page.wait_for_selector("section.ws-tabset:not(.hidden) .ws-widget[data-type='model_forecast'] table", timeout=20000)
        fc = widget(page, "model_forecast").inner_text()
        assert "After the close of 2026-03-30" in fc and "1 day" in fc and "5 days" in fc
        assert picked in fx.STOCKS
        widget(page, "model_forecast").locator("button:text-is('Why did it change?')").first.click()
        page.wait_for_selector("section.ws-tabset:not(.hidden) .ws-widget[data-type='model_forecast'] p.small:has-text('forecast moved')", timeout=10000)
        assert "Moved the forecast" in widget(page, "model_forecast").inner_text()           # a ridge model: exact attribution
    assert errors == []


def test_model_lab_starts_and_stops_a_replay_without_running_it_in_the_app(tmp_path, monkeypatch):
    from tradeassist.v4 import runs
    calls = []
    monkeypatch.setattr(runs, "SPAWN", lambda args, log: calls.append((args, str(log))))

    def prepare(svc):
        from tradeassist.intelligence.data import dataset as DS
        from tradeassist.v4 import replay_store
        DS.freeze(replay_store(svc), fx.calendar_market(first="2022-01-03", last="2025-06-30"), fx.universe(), source="test", adjustment="none")
    with demo_page(tmp_path, prepare=prepare) as (page, errors):
        page.goto(page.root + "#lab")
        page.wait_for_selector(".ws-tab-btn[aria-selected='true'][data-workspace='lab']", timeout=30000)
        lab = "section.ws-tabset:not(.hidden) .ws-widget[data-type='lab_runs']"
        page.wait_for_function(f"""document.querySelector("{lab}").innerText.includes('Newest dataset')""", timeout=20000)
        page.click(f"{lab} button:text-is('New replay…')")
        page.fill(f"{lab} input[aria-label='First session']", "2023-03-01")
        page.fill(f"{lab} input[aria-label='Last session']", "2025-03-31")
        page.click(f"{lab} button:text-is('Start the replay')")
        page.wait_for_selector(f"{lab} table", timeout=10000)
        assert calls and calls[0][0][0] == "resume" and calls[0][0][1].startswith("run-")
        run_id = calls[0][0][1]
        assert "Started" in page.inner_text(lab)
        # A development replay into the held-out year is refused before anything starts.
        page.click(f"{lab} button:text-is('New replay…')")
        page.evaluate(f"""document.querySelector("{lab} input[aria-label='Last session']").removeAttribute('max')""")
        page.fill(f"{lab} input[aria-label='Last session']", "2025-12-31")
        page.click(f"{lab} button:text-is('Start the replay')")
        page.wait_for_function(f"""document.querySelector("{lab}").innerText.includes('held out')""", timeout=10000)
        assert len(calls) == 1
        # Resume of the queued run starts it again; Stop asks it to pause.
        from tradeassist.v4 import replay_store  # noqa: F401
        page.request.post(page.root + f"v4/replay/runs/{run_id}/stop", headers={"Origin": page.root.rstrip('/')})
    assert errors == []
