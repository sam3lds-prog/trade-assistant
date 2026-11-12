# Catalyst Scenarios (4.1)

Catalyst Scenarios is a section of the saved Research study (Research → study → **Forecast**). You pick an event,
export the study's evidence, run it in a scenario provider such as the [MiroFish dashboard](https://miro-fish.org/dashboard),
import the provider's **Central**, **Bullish** and **Bearish** reactions, review the assumptions behind each, and draw
them on the stock chart beside the study's base forecast.

This release is **PR 1** of the build plan (*Trade Assistant + MiroFish build plan*, 11 November 2026): the manual
pilot. The provider is a person pasting a report. The plan's later steps reuse the same input and output contracts:

| Step | What it adds | In 4.1 |
| --- | --- | --- |
| 1. Manual scenarios | Seed export, manual import, reviewed assumptions, versioned paths on the chart | **Yes** |
| 2. Strategy comparison | The same instruments valued under each case; a budgeted mix; cross-case downside | Later |
| 3. Provider integration | A single-call AI and an opt-in MiroFish sidecar adapter, with progress, timeout and cancel | Later |
| 4. Forward evaluation and drafts | A paired scorecard; drafts that carry a scenario version and valuation | Later |

No step places an order, and in 4.1 nothing leaves the app unless you upload the packet yourself.

## The workflow

1. **Run a study** with a horizon of **1 or 5 trading days** (one horizon per study in the pilot).
2. In **Forecast**, under *Catalyst Scenarios*, choose the kind of catalyst, name it and give its date and time
   (before the open, during the session, after the close), or mark it hypothetical. Optionally add public source
   excerpts (a press release, an article) published before the study ran.
3. **Export evidence.** A Markdown packet downloads and the forecasting question is copied to the clipboard. The
   packet holds the study's evidence as of its cutoff: the anchor price and when it was read, the exact exchange
   sessions, the session the catalyst's reaction first trades in, the base forecast, the comparable situations and
   their sample sizes, the factor fit, the options-implied figures, the sources with stable ids (S1, S2 ...), what is
   missing, and an output template. It holds nothing about your account: no broker token, balance, order, position or
   budget. Exporting the same evidence again gives the same packet (the same seed id).
4. **In the MiroFish dashboard**, choose *New Scenario*, upload the packet and paste the question. Run it.
5. **Import cases.** Paste the JSON result. A text-only report: *Copy the output prompt* (with the template) and have
   it turned into JSON, or *Fill in a form instead*. A report excerpt can be kept beside the cases; it is shown as text.
6. **Review the cases.** Each card shows the trigger, why it could move the stock (with its cited sources; a claim
   citing nothing is labelled *Hypothesis: no exported source*), what may be priced in, what would invalidate it, and
   what evidence is missing. Under *Assumptions*, set the sizes:
   - a **stock-specific move** in percentage points, from a session (the catalyst's session by default), with its
     source: *your stress*, *provider (cited)* or the *options-implied move* when the packet has one;
   - **factor shocks** in each What-If factor's own unit (% or bp), from a session;
   - optional **extra event uncertainty**, in percentage points of log-return standard deviation.
7. **Apply assumptions.** A new version is saved and drawn: the base solid, each quantified case dashed in its colour.
   Hovering a future session shows each line's price, move from the anchor, session and trigger. Every edit is a new
   version; older versions stay viewable at their original dates.

The Advanced Scenario Explorer is unchanged and still useful for free-form stress tests.

## The arithmetic

Every path starts at the study's own anchor price and its session dates, never the chart's newest close:

```text
case_return_pct[t] = base_return_pct[t]
                   + (shocked_whatif_pct[t] - reference_whatif_pct[t])     factor shocks, incremental
                   + issuer_residual_pct[t]                                 the stock's own jump, from its session on

case_price[t] = anchor_price * (1 + case_return_pct[t] / 100)
```

- **Base.** The study's saved forecast and its stored anchor. Whether its range is calibrated is said wherever it is
  shown.
- **Factor effect.** The What-If factors' joint fit (market first, then the sector fund, overseas markets and the
  macro factors) is refitted on daily closes up to the study's last complete bar, never later, and frozen in the
  packet. A case's factor term is the What-If arithmetic (`factors.terms_from_fit`, the same code as `factor_terms`)
  under the shocked values, less the same under the reference (no shock). So a sector shock counts only the part of its
  move the market does not already explain, Treasury shocks are in basis points, and a zero shock is the base exactly.
  A factor left unshocked has no move of its own: it moves only as the shocked factors above it imply.
- **Issuer move.** A jump from its session on (the first tradable session after the event by default). One per case,
  so one catalyst contributes once. The base forecast has no news-tone term, so nothing is counted twice.
- **Band.** The base's own band, carried around the case's median in log space, widened in quadrature by any extra
  event uncertainty. If the base already priced an earnings move inside the horizon, that uncertainty is in it once.
  The band is the model-implied range unless the base's calibration passed. No case has a probability.
- **Fixed coordinates.** Each version stores the anchor, the session dates and the dollar points. A chart that
  refreshes, or a study reopened later, draws the same points at the same dates. The study's own *Draw on the linked
  chart* now uses the same fixed coordinates.

## What the provider may and may not send

The import is one JSON object (`contracts.ScenarioImport`), validated whole on the server. It is refused, with every
problem listed, when:

- it is for another packet, stock, horizon, set of sessions or data mode (a late answer cannot land on another study);
- a case appears twice, or is not central, bullish or bearish;
- a citation is not one of the packet's source ids, a factor is unknown, a unit does not match its factor (an issuer
  move is in pp), or a session is past the horizon;
- a number is not finite, a field is unknown (a probability, a path, a price target), or the payload is too large.

Proposed sizes are only starting points for review, and only when they cite the packet's evidence. Assumptions are
checked again when applied: values within each unit's bounds, sources that exist, the options-implied move used only
at its own size.

## Records

Trading-database migration 24 adds three tables:

| Table | One row per |
| --- | --- |
| `scenario_seeds` | a frozen packet: study, symbol, horizon, cutoff, schema version, seed hash, the packet |
| `scenario_runs` | a provider's answer for one seed: provider, status, versions of the prompt and schema, response hash, the normalised cases, the report excerpt (statuses for a queued provider are allowed already) |
| `scenario_versions` | one reviewed set of assumptions and the paths computed from it; edits add a version, none is changed |

## Routes

| Route | Purpose |
| --- | --- |
| `POST /v3/studies/{study_id}/scenario-seeds` | Freeze a shareable packet; returns its id, the question, the template and the Markdown link |
| `GET /v3/scenario-seeds/{seed_id}?format=json\|markdown` | The stored packet |
| `POST /v3/studies/{study_id}/scenario-runs` | Save a manual import |
| `GET /v3/studies/{study_id}/scenario-runs`, `GET /v3/scenario-runs/{run_id}` | Reopen runs: cases, suggested assumptions, versions |
| `POST /v3/scenario-runs/{run_id}/versions` | Validate assumptions and save the case paths |
| `GET /v3/scenario-versions/{version_id}` | One version, as it was saved |

Reads are refused to another site's page, as the app's other research reads are. `FEATURE_CATALYST_SCENARIOS=0` in
`.env` hides the section and switches the routes off; the rest of Research is unchanged.

## Testing it

On demo data (no token in `.env`), or on your own data:

1. Start the app (`.venv/bin/python run.py`) and open Research.
2. Run a study of a stock with a **5-day** horizon. Open **Forecast**; *Catalyst Scenarios* is at the bottom.
3. Name a catalyst (say *Quarterly results and outlook*, after the close on the first session's date) and press
   **Export evidence**. The packet downloads; open it and check the sources, sessions and template.
4. Copy `tests/fixtures/catalyst_cases.json` from the repository, replace `<exported-seed-id>` with the packet's seed id
   (shown under *Evidence packet*), paste it into *Result* and press **Import cases**. Three cards appear; the bullish
   sector driver is labelled a hypothesis.
5. Press **Apply assumptions**. The bullish (+4 pp from the catalyst's session) and bearish (the market −1% from the same
   session) cases are drawn dashed on the chart; the table shows each case's price, its difference from the base, and
   how much of it is factors and how much stock-specific.
6. Refresh the chart, then reload the page: the lines stay at the same prices and dates. Switch to another stock: its
   chart and study show none of them.
7. Try a wrong packet id, a source id like `S99`, or a factor like `gold`: the import is refused with the reasons.

Then the real pilot: the same export, uploaded to the MiroFish dashboard as described above.

Automated: `tests/test_catalyst_scenarios.py` (the arithmetic, the API and every refusal) and
`tests/test_catalyst_workstation.py` (the whole workflow in a browser: export, import, apply, chart, refresh, reload,
symbol switch).

## What this does not claim

The upstream MiroFish project describes social simulation and report generation; it publishes no measured
price-prediction accuracy for this workflow, and the hosted dashboard's relationship to it is unverified. The
scenarios here are assumptions to test against. Simulated agent votes and provider confidence never become forecast
probabilities; a case gains influence over the base only after it beats frozen baselines on forward evidence (PR 4).
