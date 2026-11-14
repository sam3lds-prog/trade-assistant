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
| `versions` | every version of the study, each `official`, `promoted`, `exploratory` (your edit), `control` (the single-prompt control) or `previous` (an earlier automatic run), with its parent and how the forward scorecard treats it |
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
- A Trading Desk draft (`POST /v3/drafts`) with a scenario reference is refused when the decision belongs to another
  version, was made from another valuation, has been superseded, or is an exploratory assessment; and when the
  version's sessions have passed. A draft from an exploratory version is allowed but says it is research only. The
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

**Promotion is the only way an edit becomes official**, and it has two conditions. You must confirm it, and the
version must meet the standard the automatic run holds itself to. Every size must come from the packet's own rules
(the options-implied move, the comparable situations' tail, the options-implied standard deviation, a factor's own
volatility). Each must also be for a driver of that case that cites the packet, in that driver's direction. A user
stress, a provider's own number or an uncited driver stays exploratory. Promotion then values the strategies again
from a fresh snapshot. It decides through the normal official checks: real-time, fresh quotes, and triggers verified
for that version. The forward scorecard is not changed by it: it keeps what the automatic run predicted before the
window opened.

## 4. Data integrity

- **No paid run for an edit.** Previews, saved versions and promotions never call MiroFish or any provider.
- **Forward evaluation stays clean.** Only the automatic run's and the control's versions are frozen for the forward
  scorecard (unchanged from 4.5); an automatic version made after the window opened is late and not scored; an
  exploratory version is never scored, saved or not.
- **Old studies stay as they were.** A study reopened later shows its original anchor, sessions and paths; nothing is
  regenerated to look current.
