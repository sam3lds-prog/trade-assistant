# Forecast Studio (4.7)

4.7 makes Research one forecasting surface instead of several overlapping ones. A study's quantitative base, its
MiroFish (or other provider's) scenarios, the edits you try, the strategies valued under them and the one decision that
comes out are assembled into one result. The chart shows that result through its legend, and an edit is either an
unsaved preview or a saved, research-only version. The four workspaces (Discover → Research → Trading Desk →
Portfolio), Model Lab and the classic screens are unchanged as places.

It was built from the *Trade Assistant 4.7: Unified Forecasting, Scenario UX and Trading Workflow* plan, reviewed
against the repository at `2edf51c` (4.6.2). Nothing in it places an order, and no setting that allows trading is
added or changed.

## 1. Audit: Research before 4.7 (PR 0)

Recorded at `2edf51c`, with the full suite green (1,611 passed, 2 skipped).

### Where a forecast reached the chart

| Entry point | What it drew | Chart line keys | What it left out |
| --- | --- | --- | --- |
| Study → Summary → *Draw on the linked chart* | the base, its band, the study's alternative paths | `forecast:base`, `forecast:low/high`, `forecast:<alt>` | any scenario version |
| Study → Forecast → *Draw on the linked chart* | the same as above | the same | the same |
| Scenarios panel (one-click) → drawn on completion, and *Draw on the linked chart* | the official automatic version's cases | `forecast:case:*` | the decision's case |
| Forecast → Advanced → Catalyst Scenarios → *Apply assumptions* and *Draw on the linked chart* | a hand-applied version | `forecast:case:*` (replacing the official one) | its valuation and decision: none were made |
| Scenario explorer (Advanced widget, the 2.x What-If model) | its own base and the AI's path | `whatif:model`, `whatif:ai` | anything of the study's; the lines stayed when the widget closed |

So three buttons drew the same thing; a hand-applied version replaced the official version's lines while the decision
card above it still showed the official version's decision; and the What-If lines, from another model, could sit
beside the study's.

### Duplicated controls and competing answers

- Three *Draw on the linked chart* buttons, and a fourth path that drew on its own.
- Two version pickers: the pipeline's (implicitly the official version) and the manual tools'.
- Three "best" answers: the study's top three strategies (Summary), the best budgeted mix (Findings), and the decision's
  setup.
- Scenario settings in the study form: *Run scenarios*, the provider, the profile, *On by default*, then readiness,
  estimate and spending under *Simulation details*.

### The two scenario lifecycles

| | Automatic (one-click, 4.3–4.6) | Manual (4.1 tools) |
| --- | --- | --- |
| Steps | seed → provider run → cases → version (`origin` auto) → valuation → findings → decision | seed → import → version (`origin` manual) |
| Valued | yes, from one quote snapshot | no |
| Decided | yes: `scenario_decisions`, published to Discover, notified, frozen for outcomes | no |
| Forward record | frozen before the window (`scenario_forecasts`) | never |

### The chart's line ownership and visibility (4.6.1)

- Lines are kept by key in the chart (`state_.lines`), set by prefix (`setLines("forecast", …)`), cleared when the
  chart reloads its bars (each owner redraws its own).
- Hiding a line from the legend kept its key in `state_.hidden`, the same set for every study on that chart, so
  hiding *Bearish* on one study hid it on the next study of the same stock.
- The hover readout read every line's notes, hidden lines included.

### API contracts kept as they were

Every 4.6 route is unchanged and backward compatible: `/v3/studies`, `/v3/studies/{id}`, `/v3/studies/{id}/pipeline`
(and `approve`, `cancel`, `retry`, `start`, `revalue`), `/v3/scenario-*` (seeds, runs, versions, valuations),
`/v3/studies/{id}/decision` and `/decision/trigger`, `/v3/scenarios/*` (settings, usage, estimate, circuit, check,
notices, scorecard) and `/v3/drafts`. 4.7 only adds routes (section 3).

## 2. One authoritative forecast state (PR 1)

### The ForecastBundle

`GET /v3/studies/{study_id}/result` (`v3/forecast_state.py`) is the study's whole forecasting result, assembled from the
saved records only: it never starts a simulation, downloads market history or reprices an option.

| Part | What it holds |
| --- | --- |
| `anchor`, `sessions`, `historical` | the study's frame (its anchor price and trading sessions); a study whose last session passed is said to be historical, shown at its original prices and dates |
| `lifecycle` | one state: `evidence_running`, `baseline_complete`, `simulation_queued`, `simulation_running`, `awaiting_approval`, `version_saved`, `valuing`, `decision_ready`, `simulation_failed`, `simulation_canceled`, `needs_attention`, `study_failed` |
| `base` | the base path, range and prices, and its `headline`: what the readiness lets it claim (below) |
| `scenarios` | whether this study can have scenario simulations (1- or 5-session studies with the feature on), and why not |
| `automatic_run` | the official run: provider, status, catalyst, the version it saved, its valuation and decision |
| `versions` | every version of the study, each `official`, `promoted`, `exploratory` (your edit), `control` (the single-prompt control), `previous` (an earlier attempt's automatic run) or `replaced` (the automatic version a promotion replaced), with its parent and how the forward scorecard treats it |
| `selected_context` | the version and case being looked at, and the one version pinned for comparison |
| `selected_version` | its assumptions, case paths, catalyst and timing, and the packet's sources and factors |
| `comparison` | the pinned version's cases, only when it shares the symbol, anchor, sessions and horizon; otherwise why not |
| `valuation` | **the selected version's own** valuation, never another version's |
| `decision` | for the official version, its official decision; for an exploratory one, its research-only assessment; never another version's (`decision_note` says whose it is) |
| `strategies` | one table of the study's candidates under that context (legs, entry, each case's net P&L, worst case, most it can lose, capital, data quality, snapshot, route) |
| `provenance`, `presentation` | the models, provider, calculation and data source; the view preferences |

**Base versus Central.** The base is the quantitative forecast; Central is a conditional catalyst case. Central equals
the base when it has no quantified assumption, and it is never put in the base's place or called the most likely
outcome.

**A neutral base says what it is.** The headline is one of: *Calibration pending*, *Too little history*, *Range
available, direction unsupported*, *No validated directional edge*, *Validated directional edge*, *Forecast and range
validated*. A flat base without a validated edge says it is flat by design, never an unexplained "Base 0.00%".

### The presentation state

`GET` and `POST /v3/studies/{study_id}/presentation` keep how a study is being looked at: `selected_version_id`,
`selected_case_key`, `compare_version_id`, `visible_layers` (`base`, `range`, `central`, `bullish`, `bearish`,
`compare`, `marker`) and `chart_focus`. It is stored per study (`study_presentation`, migration 31), apart from the
research records; only the fields sent change. Every version named must belong to the study: another study's is
refused, not quietly replaced. An unsaved preview is never stored.

### Version, valuation and decision belong together

- A valuation is shown only with the version it valued.
- An official decision is shown only for its own version, and an earlier (superseded) decision is marked as history.
- A Trading Desk draft (`POST /v3/drafts`) with a scenario reference is refused in these cases: the decision belongs to
  another version, was made from another valuation, has been superseded, is about another strategy or another case,
  or is an exploratory assessment; or the version's sessions have passed. A draft from an exploratory version is allowed but says it is research only. The
  draft carries the version's kind (`official`, `exploratory`) and its quote snapshot time, for the return link.

## 3. Scenario Lab, preview and exploratory evaluation (PR 4, backend)

`v3/scenario_lab.py`. One editor for conditional paths, whatever the cases came from (MiroFish, the single-prompt
control, another provider, a manual import), always against the study's frozen evidence and base, with the checks a
saved version gets (units, sessions, the packet's sources and factor fit, sizes credited to a rule being the rule's
own).

| Route | What it does | What it writes |
| --- | --- | --- |
| `POST /v3/studies/{id}/scenario-lab/preview` | the paths for the edited assumptions; echoes `generation` so a late reply cannot overwrite newer inputs | nothing |
| `POST /v3/studies/{id}/scenario-lab/versions` | saves a new version (its parent recorded), selects it, values the study's strategies under it from one fresh snapshot, and assesses it | the version, its valuation, a research-only assessment |
| `POST /v3/studies/{id}/scenario-lab/versions/{vid}/value` | a fresh valuation and assessment of an exploratory version | the same |
| `GET /v3/studies/{id}/scenario-lab/promotion?version_id=` | whether the version could become official, and what promotion would do | nothing |
| `POST /v3/studies/{id}/scenario-lab/promote` | with `confirm`: makes it the official version, values it again and decides through the official path | the official pipeline's records |

**Exploration never contaminates what is official.** An exploratory assessment runs the Opportunity Engine's checks
on the exploratory valuation, so you can see what would pass or fail under your assumptions. It is labelled research
only and kept in `scenario_assessments`. It is not written to `scenario_decisions`, not published to Discover, not
notified, not frozen for outcomes or for the forward scorecard. The study's official version and decision don't change.

**Promotion is the only way an edit becomes official**, and every one of these must hold:

- **You confirm it.**
- **The official run finished.** It is complete: not running, waiting for approval, canceled, failed or needing
  attention.
- **It edits the official run's own cases.** Their triggers and invalidations are the provider's, from the frozen
  packet. A manual import, the single-prompt control or an earlier attempt can't be promoted.
- **Every size is one the automatic run could have made itself:**
  - The stock's own move comes from one of the packet's rules (the options-implied move, the comparable situations'
    tail, the options-implied standard deviation). It must be for a cited issuer driver of that case, in that
    driver's direction and on its session.
  - A factor shock is one standard deviation of a factor that a cited driver of that case names. It must be on that
    driver's session, with the sign that moves the stock the driver's way.
  - There is no extra uncertainty (the automatic run never adds any).

A user stress, a provider's own number or an uncited driver stays exploratory.

Promotion then values the strategies again from a fresh snapshot and decides through the normal official checks:
real-time, fresh quotes, and triggers verified for that version. It changes neither the forward scorecard nor the
strategy outcome record: both count only decisions on the automatic run's own versions.

## 4. Data integrity

- **No paid run for an edit.** Previews, saved versions and promotions never call MiroFish or any provider.
- **Forward evaluation stays clean.** Only the automatic run's and the control's versions are frozen for the forward
  scorecard (unchanged from 4.5); an automatic version made after the window opened is late and not scored; an
  exploratory version is never scored, saved or not.
- **Old studies stay as they were.** A study reopened later shows its original anchor, sessions and paths; nothing is
  regenerated to look current.

## 5. The Research study (PR 2)

**Study settings.** Horizon, kind of trade, direction, peers, and **Full study** or **Baseline only**. A full study
runs the quantitative study and then, with no second click, the simulation, the paths, the valuation and the decision
(1- and 5-session studies). A baseline-only study stops after the quantitative study; *Run scenarios* starts the rest
later. 3-, 10- and 20-session studies are baseline only. One line shows the provider's readiness, the profile and the
next run's estimate, and whether it starts on its own or asks first. The provider, the profile and *Full study by
default* are under **Simulation settings**, with the readiness levels, the circuit breaker, the spending and *Simulation
usage*.

**The status line**, above the sections, says where the study is: *Collecting the evidence*, *Baseline complete*,
*Simulation queued*, *Simulation running*, *Waiting for your simulation approval*, *Valuing the strategies*, *Decision
ready*, *Simulation did not finish*, *Simulation canceled* or *needs attention*. It has Approve the simulation (spending
only, never a trade), Cancel and Retry (or *Start a new run anyway* when a paid request's outcome is unknown) when they
apply. The quantitative study is readable while the simulation runs; its progress updates in place, so nothing you are
editing is redrawn.

| Section | What it shows |
| --- | --- |
| **Summary** | the base headline (its move and price, what its readiness lets it claim, the range); the selected version's cases (each with its move and price; *not probabilities*), click one to select it; the one decision card; the next step |
| **Evidence** | evidence for and against, context, what is missing, comparable past situations; the evidence cutoff, data source, model and provider; *Ask the AI to challenge it* |
| **Forecast & Scenarios** | **Base** (readiness, directional evidence and range validation, the explanation, other paths, what would prove it wrong); **Catalyst and simulation** (the catalyst, hypothetical events labelled as such, the simulation's stages and details, the cases' triggers and invalidations, the findings); **Scenario Lab**; **Versions**; under Advanced, *import cases by hand* |
| **Strategies** | one table: strategy, direction, legs and expiry, entry (debit, credit or share price), each case's net P&L (the selected case marked ▸), worst case, most it can lose, capital, data quality, route, Draft; the decision's setup is marked ◆; payoff details, volatility sensitivity and the budgeted mixes are folded |

**The decision card** is the selected version's own:

| Decision | Primary action |
| --- | --- |
| Qualified setup (official, current) | **Review trade**: a draft with its version, case, valuation and decision, opened on the Trading Desk, which checks it again; nothing is sent |
| Watch for trigger | **Check this source**: paste the link that shows the trigger happened. The app reads the page; only a verified source can qualify the setup. Submitted, being checked, verified, rejected and expired are shown as different states |
| No setup | **Refresh research**: one fresh quote snapshot, the same strategies and cases, decided again |
| An exploratory version's assessment | labelled *Research only*; *Back to the official version*, *Make it official…* |

**Scenario Lab.** Pick a case. Edit its stock-specific move (percentage points from a session, with its source and
the packet's source ids), its factor shocks (in each factor's unit, through the study's frozen factor fit) and any
extra uncertainty. Each change is previewed after a short pause as a dotted purple line, labelled *unsaved*. The
preview costs no simulation and writes nothing, and a late answer never overwrites a newer edit. A size typed by hand
over a rule's size becomes your stress. *Save version* keeps it, values the strategies under it and shows its
research-only assessment. *Discard* drops the edit. For an exploratory version there are *Value again* and *Make it
official…*: the promotion check lists what promotion would do, or why it stays exploratory. Cases imported by hand
start from the packet's suggested sizes. The What-If explorer's factor values can be offered here, as your stress.

**Versions.** *Showing* picks the version (official, exploratory, control, earlier). *Back to the official version*
returns to it. *Compare with* pins one other version: drawn fainter beside the selected one when the two share an
anchor, sessions and horizon, and listed as differences otherwise. A comparison is read only: the selected version's
valuation and decision do not change.

## 6. The chart (PR 3)

| Layer | Drawn as |
| --- | --- |
| Price history | candles or a line, as before |
| Base forecast | solid, the forecast colour |
| Range | dotted edges (one legend entry: *80% band* when calibrated, else *Model P10–P90*) |
| Central, Bullish, Bearish | dashed in each case's colour; the case the official decision names is thicker and marked ◆; the selected case is thicker |
| A case with no quantified assumption | not drawn; a legend note, *Central = Base* |
| Unsaved preview | dotted, purple, in the legend group *Exploration · unsaved preview* |
| Comparison | fainter, thinner dashes; one legend entry, *vN (comparison)*, switches all of them |
| Catalyst | a small marker on the base line at the reaction session, only when the event's date and time of day are known (before the open, during the session, after the close); never at an invented time |

- **The legend is the one layer control.** Each entry shows its end price; click it to hide or show it. The choice
  is the study's (kept on the server), so two studies of the same stock keep their own.
- **Hover** shows, for a future session, the session date, the base's price and move, each visible case's price, move
  and trigger, the preview, and the comparison. A hidden line says nothing.
- **Daily bars only.** The forecasts are in trading sessions. On another grouping the chart draws none of them and
  says so, with *Show daily bars*.
- **Fixed coordinates.** Every path starts at the study's anchor price and is drawn at its session dates, so a chart
  that refreshes, or a study reopened weeks later, draws the same points. A study whose last session has passed is
  labelled historical.
- **Switching** stocks clears every forecast layer at once, and an unsaved preview with it. Starting a new study of
  the same stock clears the old one's forecast until the new result exists. A late answer for another study is
  dropped.
- **The What-If explorer's lines** (orange and purple, another model) are drawn only when you tick *Draw the What-If
  paths on the chart* in that widget, and are removed when it closes.

## 7. Handoffs (PR 6)

- **Discover → Research.** Discover's horizons are categories (today, an event, over days); a study's are trading
  sessions. One explicit map joins them (`WS.horizonSessions`: today 1, event 5, over days 5). Picking an opportunity
  sets the study's horizon, direction and kind of trade, and carries its id, source and catalyst. *Study it* starts
  exactly one study (a repeat of the same click finds it) and opens Research, which follows it.
- **Research → Trading Desk.** *Review trade* and *Draft* carry the study, the strategy and its legs, the version,
  the case, the valuation, the decision, the quote snapshot, the trigger, the invalidation and the sizing. The desk
  shows *Chosen under* and still checks quote freshness, the policy, the broker and the limits; nothing is submitted
  from Research.
- **Trading Desk → Research.** The draft's *Open its study* reopens the study at the version the draft was chosen
  under.
- **Reopened studies** show what they forecast then; current prices are on the chart separately.

## 8. Updating, restarting and operating

The update adds migration 31 (two tables, nothing removed). The first start backs the database up to `data/backups/`
first, as every migration does. MiroFish itself is unchanged by 4.7 (still the pinned `7657031`). It only needs a
restart if you want a clean process, and its settings are where they were.

```bash
# 1. Stop the app (Ctrl+C in its terminal) and close Grok if it is reviewing.
cd ~/projects/trade-assistant
git rev-parse --short HEAD          # note it, in case you roll back
./update.sh                         # pulls 4.7.0, installs packages, lists what changed

# 2. Restart MiroFish (optional; it is unchanged) and check it answers.
./setup_mirofish.sh restart
./setup_mirofish.sh status

# 3. Start the app; the first start applies migration 31 after a backup.
.venv/bin/python run.py

# 4. Free checks: Tradier, MiroFish's levels, settings, circuit, spending.
.venv/bin/python run.py --scenario-check SPY
```

Then in the browser, reload the workstation (Cmd+Shift+R once, so the new scripts load). No `.env` change is needed:
*Full study* follows `SCENARIO_AUTO` until you change it in Simulation settings.

**Roll back**: stop the app; `git checkout <the version you noted>`; `.venv/bin/pip install -q -r requirements.txt`;
start it. 4.6.2 runs on the migrated database and ignores the two new tables. To undo the migration as well, put the
newest `data/backups/` copy from before the update in place of `data/tradeassist.db`.

## 9. Independent review

A separate reviewer, who had not seen the work, read the change against this design before release. The findings and
their fixes are recorded in [IMPROVEMENTS.md](IMPROVEMENTS.md#47-independent-review), each with a test. The most
serious: promotion was not tied to the official run's own cases (a manual import sized like the official version could
be promoted); it now is.

## 10. Acceptance

The plan's 24 tests are mapped in `tests/test_acceptance_47.py`. The new tests are:

- `tests/test_forecast_studio.py`: the bundle, presentation, preview, save, assessment, comparison, focus, promotion
  and drafts.
- `tests/test_forecast_studio_ui.py`: four browser tests.
- `tests/test_acceptance_47.py`: tests 4, 11, 14, 16, 17, 18, 21 and 24.

The older browser tests were updated to the new screen. The whole suite runs in CI on Python 3.11 and 3.13 with
Chromium.

**Not done by these tests, by design:** a paid MiroFish run (`run.py --scenario-smoke`, which asks before spending)
and live-market checks; both are for you to run on the Mac when you choose.
