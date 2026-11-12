# One-click Research (4.2 to 4.5)

The plan (*Trade Assistant one-click Research build plan*, 12 November 2026) turns **Run study** into a complete
decision report: the stock's evidence and candidate strategies, an evidence packet run through a scenario provider
(MiroFish, self-hosted), the returned cases turned into price paths, the same strategies revalued under each case,
and an opportunity highlighted only when one clears the configured checks. It is built in four steps:

| Version | Step | What it adds |
| --- | --- | --- |
| 4.2 | Tradier and forecast readiness | Check Tradier data; quota handling; durable, honest calibrations |
| 4.3 | One-click pipeline | Run study → MiroFish → price paths → revaluation, in the background |
| 4.4 | Opportunities and reporting | Qualified setup, Watch for trigger or No setup; Discover; notices; drafts |
| 4.5 | Forward evidence | Frozen predictions scored against what happened, base vs no change vs single prompt vs MiroFish |

Nothing in any step places an order; scenario evidence never qualifies anything for the autopilot.

## 1. Tradier and forecast readiness (4.2)

### Connect Tradier

Tradier is the app's primary market-data provider. In `.env`:

```dotenv
DATA_MODE=live
TRADIER_LIVE_TOKEN=<your Tradier production token>
```

(For the sandbox, `DATA_MODE=sandbox` and `TRADIER_SANDBOX_TOKEN`; its quotes are delayed 15 minutes and it has no
Greeks.) Restart the app. In Research, the **Forecast record** widget has **Check Tradier data**: a quote, two weeks of
daily bars, the nearest option chain and the market clock, each reported with what came back, the quote's age, the
Greeks' age and anything missing. A rejected token (401/403) says which variable and environment to check. The check
uses only GET `/v1/markets/*` and never shows the token.

Requests are batched and cached as before; Tradier's HTTP 400 "Quota Violation" now pauses requests until the reset
and defers background work to it.

### Readiness: directional evidence and range validation

The flat five-session line has several possible causes, and each is now said plainly in the study's Summary and
Forecast sections and in the Forecast record:

| Shown | Meaning |
| --- | --- |
| *Five-session calibration pending* (or running) | No calibration for this horizon on this data source yet; it is queued |
| *Insufficient history* | Too few forecasts or too few distinct dates to judge; failed stocks are listed |
| *Range coverage below target* | The band held fewer than about 80% of holdout outcomes: treat it as too narrow |
| *No reliable directional edge · range shown* | No direction beat assuming no move; the base path is flat and the validated range is shown |
| *Directional edge validated* | A direction beat no move on validation dates and still did on the holdout |

Each comes with the sample (holdout forecasts, the dates they span, how many were fitted, chosen on and purged), when it
was last checked, the error against no change and the observed coverage.

A calibration is fitted on the earliest 60% of dates, its direction is decided on the next 20%, and every figure is
reported from the last 20%; outcomes overlapping the next period are purged. The band's width is set before the
holdout. Any options-implied or earnings overlay that was not in the calibration sample stays model-implied.

### Calibrations in the background

Calibrations are durable jobs on Research's own worker, keyed by data mode and horizon (switching from demo or the
sandbox to live Tradier starts a new calibration: each data source has its own). They show progress, survive a
refresh and a restart, wait for a used-up quota to reset, show Retry when they fail, and never replace a usable
calibration with one that found too little history. With real data connected, the 1- and 5-session calibrations are
queued automatically when missing, older than a week or made with the 3.0 method (checked at start and every six
hours). Run study never queues one: a study made while one is pending says so and uses the baseline.

The V4 replay (Model Lab) is separate: `python run.py --replay dataset build`, check it, run the development replay and
inspect its report before any holdout or deployment. Its shadow forecasts do not feed Research's calibration.
