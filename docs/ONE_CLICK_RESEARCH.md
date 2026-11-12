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

## 2. The one-click pipeline (4.3)

### Set it up once

```dotenv
SCENARIO_PROVIDER=mirofish          # or ai (one AI call: the control), or demo (canned cases, demo data only)
SCENARIO_AUTO=1                     # run it after every 1- or 5-session study (also a switch in Research)
MIROFISH_URL=http://127.0.0.1:5001  # your own MiroFish server; must be this computer
MIROFISH_MAX_ROUNDS=10              # the simulation's round cap
MIROFISH_RUN_MINUTES=45             # the most one run may take, every stage included
MIROFISH_CONCURRENCY=1              # runs at once
MIROFISH_HTTP_TIMEOUT=300           # one request (the ontology step waits on the server's model)
SCENARIO_BUDGET=0                   # mixes are sized to this; 0: the Trading Desk policy's trial capital
SCENARIO_MAX_LOSS=0                 # the loss limit a setup must stay inside; 0: the policy's daily loss stop
```

MiroFish is run from its own repository at the pinned commit
[7657031](https://github.com/666ghj/MiroFish/tree/7657031ac01184afe2cb220f5ee3545573b5e843) with its own `.env` (its
language model and Zep keys stay there) and `FLASK_HOST=127.0.0.1`, so it listens on this computer only. Trade
Assistant sends it only the shareable packet: no credentials, holdings, orders or budgets. The hosted MiroFish
dashboard's API is not used.

In Research → the study's **Study settings**, *Run scenarios after the study* (and *On by default*) and the provider
can be changed without editing `.env`; the provider's readiness is shown beside them.

### What happens on Run study

| Stage | What runs | Where |
| --- | --- | --- |
| Evidence and strategies | The study as before: evidence, forecast, candidate strategies with their exact legs | The study's own thread |
| MiroFish (or the provider) | The packet frozen; the provider's stages, one bounded step at a time | Research's `scenarios` worker |
| Price paths and valuation | Cases checked against the packet, sized by the app's rules, version saved; the candidates priced under each case from one snapshot | Research's `scenarios` worker |
| Findings | A short conclusion, the cases with their triggers and invalidations, the mixes | Saved on the study |

The panel at the top of the study's Summary follows the stages, the provider's progress (its project and simulation
ids as they arrive) and the elapsed time, with Cancel while it runs and Retry after a failure. The cases are drawn on
the linked chart from their saved prices as soon as the version exists.

### What the numbers are

- **The provider does not size anything.** It proposes conditions, directions, timing and reasons, citing the packet's
  sources. The app sizes the stock's own move with the options-implied move for an earnings release, else the
  comparable situations' tail (at least 20 independent cases), else one options-implied standard deviation to the
  horizon; and a cited market, sector or macro driver with one standard deviation of that factor's own move over the
  horizon. A driver that cites nothing stays a hypothesis, listed with the case.
- **The cases have no probabilities.** The mixes are ranked by their worst case across the cases, and each case's
  P&L is shown on its own.
- **The valuation is of the study's own candidates**, with the legs they were chosen with, priced from one quote
  snapshot taken when the cases arrive. The cases' prices are the version's saved ones: a later quote changes what the
  legs cost, never where the case says the stock ends.

### When something goes wrong

| Shown | Meaning | What to do |
| --- | --- | --- |
| *MIROFISH_URL is not set* / *did not answer* | The provider is not reachable | Start MiroFish, then Retry |
| *Needs attention* after a timeout | A write's answer never came and its result was not found on the server | Check MiroFish's project list, then Retry (a new run) |
| *Needs attention: no cases in the template* | The report had no JSON cases and no AI is set up to convert it | Set `ANTHROPIC_API_KEY`, or paste the cases under Advanced |
| *Stopped: it took longer than MIROFISH_RUN_MINUTES* | The run's time cap | Raise the cap or lower `MIROFISH_MAX_ROUNDS` |
| *not this study's packet* | The provider answered for another packet | Retry; nothing was applied |

### Try it on demo data

`DATA_MODE=demo`, then in Research's Study settings choose **Demo provider** and tick *On by default*, and click Run
the study. The demo provider returns canned cases built from the packet (labelled *Demo provider: nothing was
simulated*), and the rest of the pipeline is the real one.

## 3. Opportunities and reporting (4.4)

```dotenv
OPPORTUNITY_MIN_NET_PCT=1        # modeled net upside after spreads and fees, % of the capital it ties up
OPPORTUNITY_MAX_SPREAD_PCT=10    # the widest option spread, % of the midpoint
SCENARIO_NOTIFY=0                # 1: also send new or changed setups through the notification channels
SCENARIO_BUDGET=0                # the budget setups are sized to (0: the Trading Desk policy's trial capital)
SCENARIO_MAX_LOSS=0              # the loss limit (0: the policy's daily loss stop; the trial policy's is $25)
```

After every valuation the study's Scenarios panel opens with a decision card:

| Decision | When | What it shows |
| --- | --- | --- |
| **Qualified setup** | Every check passes: without a trigger under the Central case (with a validated directional edge or 30+ independent past cases behind it, and inside the loss limit in every case), or under a case whose trigger was recorded as observed | What, when, why it cleared the checks, what invalidates it; Draft it |
| **Watch for trigger** | A Bullish (long) or Bearish (short) case's setup passes on payoff, risk, liquidity and eligibility if its trigger happens | The trigger, the strategy, the date it expires, the failure condition; Draft it; record the trigger |
| **No setup** | Nothing passes | The most material failed check, and how many setups each check stopped |

The checks, in their order of materiality: the valuation worked; the sessions have not passed; the quote is fresh and
real-time (for a Qualified setup); an unobserved trigger has not expired; every option leg has a two-sided quote within
the spread limit; the payoff clears the minimum after costs (*cost eroded* when it would have before them); the base is
supported (no trigger); the modeled worst tenth and the contract's most-it-can-lose are within the loss limit; the loss
has a limit; it fits the budget; its route is not research-only; its direction is the case's.

With the trial policy's $25 loss limit nearly everything is *No setup: the most it can lose is over the loss limit*.
Set `SCENARIO_MAX_LOSS` (and `SCENARIO_BUDGET`) to what you would actually risk on one idea.

A Qualified or Watch decision also appears in **Discover** as a scenario observation (the *Setup* filter shows them)
and as a notice at the top of the workstation. A projected case payoff is not a historical edge: these are never
eligible for the autopilot. Demo and sandbox results are labelled as examples.

## 4. Forward evidence (4.5)

```dotenv
SCENARIO_CONTROL=1   # also run the single-prompt control (needs ANTHROPIC_API_KEY); default 0
```

For every 1- or 5-session study run with scenarios, these are frozen before the first forecast session opens:

| Variant | Median | Band |
| --- | --- | --- |
| Base forecast | the study's base path at the target session | the study's band |
| No change | 0% | the base's band |
| Single-prompt control | the mean of its cases' terminal moves (catalyst-mean-1) | lowest case low to highest case high |
| Scenario provider | the same rule on the provider's cases | the same |

They are graded on the target session's close from the frozen anchor price. A conditional case is scored only when its
trigger was recorded as observed. **Research → Forecast record → Forward evidence** shows the scorecard for the chosen
horizon: average miss, coverage, direction, time and token use for each variant, the paired comparisons, and a
verdict. Until there are 30 paired observations of the provider and the base it says there are too few to judge; in
any case the provider is given no weight in the base by this release.
