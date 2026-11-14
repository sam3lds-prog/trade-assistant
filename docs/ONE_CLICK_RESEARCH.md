# One-click Research (4.2 to 4.6)

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
| 4.5.1 | Correctness, safety, recovery | Options valued at their own session; data-quality tiers; verified triggers; resumable finishing; full cancel |
| 4.5.2 | MiroFish operations and cost | Self-hosted setup script; readiness levels; profiles; estimates, approval, budgets, usage ledger; circuit breaker |
| 4.5.3 | Forecast and strategy validation | Bias, width, skill, breakdowns; week-cluster intervals; costs per study; profiles compared; strategy outcomes |
| 4.6.0 | Acceptance and release | Scenarios A to H; this guide's deployment and operations section |

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

In Research → the study's **Study settings**, choose **Full study** (the study, then the provider, with one click) or
**Baseline only**; the provider, the profile and *Full study by default* are under **Simulation settings** (4.7), and
can be changed without editing `.env`. The provider's readiness and the next run's estimate are shown on one line.

### What happens on Run study

| Stage | What runs | Where |
| --- | --- | --- |
| Evidence and strategies | The study as before: evidence, forecast, candidate strategies with their exact legs | The study's own thread |
| MiroFish (or the provider) | The packet frozen; the provider's stages, one bounded step at a time | Research's `scenarios` worker |
| Price paths and valuation | Cases checked against the packet, sized by the app's rules, version saved; the candidates priced under each case from one snapshot | Research's `scenarios` worker |
| Findings | A short conclusion, the cases with their triggers and invalidations, the mixes | Saved on the study |

4.7: the study's status line (above its sections) says where it is (simulation queued, running, waiting for your
approval, decision ready, did not finish) with Approve, Cancel and Retry when they apply; the stages, the provider's
progress and its ids are under Forecast & Scenarios → Catalyst and simulation (ids and costs under *Simulation
details*). The cases are drawn on the linked chart from their saved prices as soon as the version exists, with no
button. See [FORECAST_STUDIO.md](FORECAST_STUDIO.md) for the whole 4.7 screen.

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
| *Needs attention* after a timeout | A write's answer never came and its result was not found on the server | Check MiroFish's project list; *Start a new run anyway* (4.5.1 asks you to confirm, since the first may have cost) |
| *Needs attention: no cases in the template* | The report had no JSON cases and no AI is set up to convert it | Set `ANTHROPIC_API_KEY`, or paste the cases under Advanced |
| *Stopped: it took longer than MIROFISH_RUN_MINUTES* | The run's time cap | Raise the cap or lower `MIROFISH_MAX_ROUNDS` |
| *not this study's packet* | The provider answered for another packet | Retry; nothing was applied |

### Try it on demo data

`DATA_MODE=demo`, then in Research's Study settings → *Simulation settings* choose **Demo provider** and tick *Full
study by default*, and click Run the study. The demo provider returns canned cases built from the packet (labelled *Demo provider: nothing was
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
| **Qualified setup** | Every check passes: without a trigger under the Central case (with a validated directional edge or 30+ independent past cases behind it, and inside the loss limit in every case), or under a case whose trigger was verified from its source (4.5.1) | What, when, why it cleared the checks, what invalidates it; Draft it |
| **Watch for trigger** | A Bullish (long) or Bearish (short) case's setup passes on payoff, risk, liquidity and eligibility if its trigger happens | The trigger, the strategy, the date it expires, the failure condition; Draft it; record the trigger |
| **No setup** | Nothing passes | The most material failed check, and how many setups each check stopped |

The checks, in their order of materiality: the valuation worked; the sessions have not passed; the quote is fresh and
real-time (for a Qualified setup; 4.5.1: execution-quality data); an unverified trigger has not expired; every option leg has a two-sided quote within
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
trigger was verified from its source (4.5.1) and recorded before the target session closed. **Research → Forecast record → Forward evidence** shows the scorecard for the chosen
horizon: average miss, coverage, direction, time and token use for each variant, the paired comparisons, and a
verdict. Until there are 30 paired observations of the provider and the base it says there are too few to judge; in
any case the provider is given no weight in the base by this release.

## 5. Correctness and safety (4.5.1)

- **Each structure is valued at its own session.** Shares at the target session; an option at the target session or
  at its first leg's expiration if that comes first, against the case's saved price and dispersion **at that session**.
  Legs of different expirations are closed together at the first one. A leg expiring before the first forecast
  session, or a case whose prices reach past the option model's range, is listed as *cannot be valued reliably*.
  A release's volatility collapse applies only at or after its session.
- **Data quality**: every structure is *execution* (every quote real-time, fresh, two-sided, uncrossed, within the
  spread limit and the open-interest floor), *research* (valued, but not executable data; it can still be a Watch)
  or *unavailable* (not valued). Only execution quality qualifies.

```dotenv
OPPORTUNITY_MIN_OPEN_INTEREST=10   # an option leg with a known open interest below this is research only
FRESHNESS_OPTION_QUOTE_SECONDS=300 # how old an option's bid and ask may be while the market is open (existing setting)
```

- **Triggers are evidence for review.** Record a Bullish or Bearish case's trigger with a link: the app reads the
  page (public addresses only), takes the publication time from the page itself, and verifies it only when that time
  is after the study's cutoff and inside the trigger's window, the page names the stock, and one of its sentences
  affirms what you described and the case's trigger (not negated, not the opposite, not a preview). Only *verified*
  turns a Watch into a Qualified setup, and only for the scenario version it was checked against. Without a link, or when
  the page cannot be read or gives no time, it stays *unverified*; contradicting pages are *rejected*; a window that
  ends first makes it *expired*.
- **Finishing a run is resumable.** If the app stops or a write fails after MiroFish's report arrived, the next start
  (or Retry) finishes it from the saved report: one version, one valuation, one decision, no new run. A new run after
  *needs attention* (a paid request whose answer never came) asks you to confirm.
- **Cancel** stops the provider run, the control beside it, and their queued steps; nothing a canceled attempt sends
  back is applied, and earlier results stay in the study's attempt history.

## 6. MiroFish operations and spending (4.5.2)

### Profiles

| Profile | Agents (at most) | Rounds | Platforms | Time cap | Starts on its own? |
| --- | --- | --- | --- | --- | --- |
| Quick (default) | half of `MIROFISH_MAX_AGENTS`, at most 20 | at most 4 active rounds | both (parallel runner) | at most 20 min | when its estimate allows |
| Standard | `MIROFISH_MAX_AGENTS` | `MIROFISH_MAX_ROUNDS` active rounds | both (parallel runner) | `MIROFISH_RUN_MINUTES` | when its estimate allows |
| Deep | three times Standard | three times | both (parallel runner) | twice | never: always asks |

Since 4.6.1 every profile uses MiroFish's parallel runner (both platforms): it is the only runner that reports round
progress and completion. The app asks for enough rounds that the profile's number fall in hours when the agents are
active (MiroFish's simulated clock starts at midnight); rounds with no active agent are skipped and cost nothing. The
time cap starts when you approve a run that needed approval.

MiroFish makes one agent per entity in the graph it builds and has no agent cap of its own, so the app reads the
entity count after the graph is built and **before any agent is prepared**; above the profile's cap it asks you.
Choose the profile in Research → Study settings → *Simulation settings* (*Profile*), or `SCENARIO_DEFAULT_PROFILE`.

### Spending

```dotenv
SCENARIO_DEFAULT_PROFILE=quick
MIROFISH_MAX_AGENTS=40                  # Standard's agent cap
MIROFISH_COST_PER_RUN_USD=              # your estimate of one Standard run; empty: unknown (asks first)
MIROFISH_DAILY_BUDGET_USD=5.00          # 0: no daily limit
MIROFISH_MONTHLY_BUDGET_USD=50.00
MIROFISH_AUTO_APPROVAL_USD=0.50         # a run whose estimate tops out at or below this starts without asking
MIROFISH_REQUIRE_APPROVAL_ON_UNKNOWN_COST=1
MIROFISH_CIRCUIT_FAILURES=3
MIROFISH_CIRCUIT_COOLDOWN_SECONDS=300
SCENARIO_AI_INPUT_USD_PER_MTOK=         # optional: prices the AI control's measured tokens
SCENARIO_AI_OUTPUT_USD_PER_MTOK=
```

These are starting limits, not measured costs. MiroFish reports **no** token usage, so a run's cost is an estimate
(a range with its basis: `MIROFISH_COST_PER_RUN_USD` scaled by agents × rounds × platforms, or, once you enter actual
costs for finished runs, those) until you enter what your model provider charged for it (Research → Study settings →
Simulation details → *Simulation usage*). An estimate is not a hard cap: the app can refuse to start a run, stop one
at its time cap, and stop at the agent check, but it cannot meter a run while it goes. A failed run is counted by the
stages it reached. An unknown cost is shown as unknown, never as $0.00.

| You see | Meaning | What to do |
| --- | --- | --- |
| *Waiting for your approval* | Unknown cost, an estimate over the automatic limit, Deep, or more agents than the profile | **Approve the simulation** (spending only; no order) or Cancel |
| *Budget exceeded* | Today's or this month's spending (at most) plus the estimate is over the limit | Wait, or raise `MIROFISH_*_BUDGET_USD` |
| *Circuit open* | Several provider failures in a row; new runs paused | Fix MiroFish (`./setup_mirofish.sh logs`), then *Reset the circuit* |
| *Degraded* | `/health` answers but MiroFish's API does not | Restart MiroFish; check its log |
| *Configuration error* | `MIROFISH_URL` unset, or something other than MiroFish answers there | Point `MIROFISH_URL` at MiroFish's backend |

## 7. Forward validation (4.5.3)

Research → Forecast record → **Forward evidence** compares the base forecast, no change, the base with the single
prompt's cases and the base with MiroFish's cases on the same studies: average miss, RMSE, bias, band coverage and
width, direction, skill against no change, and breakdowns by catalyst, market regime and volatility. Each paired
comparison has 80% and 95% intervals from a bootstrap of whole calendar weeks and a verdict (*statistically supported
improvement*, *suggestive improvement*, *no demonstrated improvement*, *worse*, or *insufficient evidence* below 30
pairs over 8 weeks). Costs per study and each profile's own comparison show whether more simulation pays. Qualified
setups' strategy outcomes are graded as *historical hypothetical*, *model-only* or *ungradable*, with fills the
Trading Desk actually recorded shown apart as *observed*. MiroFish gets no weight in the base forecast.

## 8. Deployment and operations on the Mac (4.6)

### Requirements

- Trade Assistant as before (Python 3.11+, its `.venv`), and a Tradier production token for real-time data.
- For MiroFish: `git`, [uv](https://docs.astral.sh/uv/) (`brew install uv`; it fetches Python 3.12 for MiroFish), a
  language-model API key (any OpenAI-compatible endpoint), and a [Zep Cloud](https://app.getzep.com/) key (the
  pinned MiroFish only connects to Zep Cloud). Docker and Node are not needed.

### Install and configure

```bash
cd ~/projects/trade-assistant
git rev-parse --short HEAD           # note the version you are on, in case you roll back
./update.sh                          # pulls the release, installs packages, lists what changed
./setup_mirofish.sh install          # ~/projects/MiroFish at 7657031; makes its .env with FLASK_HOST=127.0.0.1
open -e ~/projects/MiroFish/.env     # LLM_API_KEY, LLM_BASE_URL, LLM_MODEL_NAME, ZEP_API_KEY
./setup_mirofish.sh start            # 127.0.0.1:5001; log in data/logs/mirofish.log
```

In Trade Assistant's `.env` (its own; MiroFish's keys never go here):

```dotenv
DATA_MODE=live
TRADIER_LIVE_TOKEN=...
SCENARIO_PROVIDER=mirofish
SCENARIO_AUTO=1
MIROFISH_URL=http://127.0.0.1:5001
# and the spending settings above
```

Restart the app (`.venv/bin/python run.py`). The first start backs the database up to `data/backups/` and applies
every migration the database is missing (up to 30; 4.5.x's are 28 to 30: columns and tables added, nothing removed).

### Check, then test for real

```bash
.venv/bin/python run.py --scenario-check SPY      # free: Tradier, MiroFish levels A and B, settings, circuit, spending
.venv/bin/python run.py --scenario-smoke SPY      # PAID: one Quick study end to end; asks before spending
```

The functional test reports each stage (Tradier, readiness, evidence and strategies, the MiroFish simulation and
report, the cases, the valuation, the decision, the usage record, and the result read back after reopening the
database) and is shown afterwards by `--scenario-check` and in Research. Enter what your provider charged for it so
the next estimates are based on a real cost.

### Running it day to day

| Task | How |
| --- | --- |
| Start / stop / restart MiroFish | `./setup_mirofish.sh start`, `stop`, `restart` |
| Is it up? | `./setup_mirofish.sh status`, or `check` for the app's view |
| Logs | `./setup_mirofish.sh logs` (`logs -f` to follow); the app's own log as before |
| A failed or stuck study | Research → the study's Scenarios panel: the stage, the reason, Retry or Cancel; *Retry (no new run)* finishes a saved report |
| Interrupted simulation | Restart the app: left-over steps resume from the saved MiroFish ids; an ambiguous write asks you before a new run |
| Usage | Study settings → Simulation details → *Simulation usage*; `GET /v3/scenarios/usage` |
| Back up | Stop the app; copy `data/tradeassist.db` (each start also keeps the newest ten copies in `data/backups/`) |

### Roll back

```bash
# stop the app (Ctrl+C), then go back to the version you noted (333b81a is 4.5.0)
git checkout <that version>
.venv/bin/pip install -q -r requirements.txt
cp data/backups/<the newest copy from before the update> data/tradeassist.db    # optional: the old database exactly
.venv/bin/python run.py
# later, to come forward again:  git checkout main && ./update.sh
```

Migrations 28 to 30 only add columns and tables, so 4.5.0 also runs on the migrated database; it then ignores
verification statuses (it would again count a recorded trigger as observed), the usage ledger and the outcome
records. Restoring the backup undoes the migrations entirely. `./setup_mirofish.sh stop` stops MiroFish either way.

