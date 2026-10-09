# Reliability, accounting, risk and analytics improvements (2.1.1 to 2.3.1)

This is the engineering record for the improvement programme that follows 2.1.0: the
baseline it started from, what was found in the code, what each sprint changed, the tests
that prove it, and what is still open. User-facing behaviour is described in the README and
in [V2.md](V2.md); this file is the evidence behind it.

## Baseline

| Item | Value |
| --- | --- |
| Starting commit | `d614c7af0df7ddd71cf93f501044e0efa832e822` (main, "Reversals: fixes from the independent review") |
| App version | 2.1.0 |
| Schema version | 4 (`schema_migrations`; migrations in `tradeassist/schema_v2.py`) |
| Python | 3.13.16 here; the code also compiles on 3.11 (pandas 3 needs 3.11 or later) |
| Dependencies | fastapi, uvicorn, pandas 3, numpy 2, scipy, requests, python-dotenv, pydantic 2, mcp 2.2; tests: pytest, httpx |
| CI | none |
| Test suite | **348 passed, 2 skipped, 1 warning** in 84 s |
| Skips | `tests/test_optionmath.py:31` twice: "price too small to invert" (expected: implied volatility cannot be solved from a near-zero price) |
| Warning | starlette `anyio.abc.BlockingPortal` deprecation, from the test client, not from the app |
| Foreign keys | `PRAGMA foreign_keys = ON` on the app's one connection (verified by a test) |

### Defects reported in the review, checked against the code

Each was reproduced on the starting commit before anything was changed.

| Report | Finding on `d614c7a` | Status |
| --- | --- | --- |
| 1.1 Portfolio risk crashes when a price is missing | `portfolio.exposure()` returned early without `beta_dollars`; `analyse()` raised `KeyError: 'beta_dollars'` | **Confirmed** |
| 1.2 A closed position can be reopened | `account()` accepted BUY after a full SELL; status went CLOSED → PARTIALLY_CLOSED on the same id | **Confirmed** |
| 1.3 Daily loss shows OK with positions it could not measure | unmeasured positions added 0 and were listed in `unknown`, but `status` stayed `OK` | **Confirmed** |
| 1.4 Non-finite fees | the ledger accepted `NaN` and `inf` fees (realized P&L became NaN or −inf). The HTTP API already refused them: both `Strict` request models set `allow_inf_nan=False` | **Confirmed at the domain layer only** |
| 2.1 Classic journal trades have no snapshots | `sync_legacy_trade` never takes snapshots; fixture databases from 2.0.0 and 2.1.0 show none for any mirrored trade | **Confirmed** |
| 2.2 Plan fields cannot be cleared | `/v2/positions/{id}/plan` dropped every `null` before saving | **Confirmed** |
| 2.3 Backdated snapshots use the fill day's close | `_past_snapshot_values` used bars up to and including the fill date, stamped 4 p.m. | **Confirmed** |

Found during the audit, not in the report:

| Finding | Status |
| --- | --- |
| Unpriced positions counted as $0 in gross, net, long, short and market-value totals | **Confirmed** |
| Stress tests used a multiplier of 100 for every option instead of the position's own | **Confirmed** (only wrong for adjusted contracts, which the app cannot yet record) |
| `sync_legacy_trade` deletes and re-inserts the mirror's executions on every journal change, so execution ids change | **Confirmed**; it blocks audited corrections and import de-duplication |
| Daily returns are forward-filled before beta and correlation, so a missing day becomes a zero return | **Confirmed** |
| Classic journal records `entry_at`/`exit_at` as the time the attempt was logged, not the fill time | **Confirmed**; documented, not changed (the journal has no field for the broker's fill time) |

### Already in place (audited rather than rebuilt)

Numbered migrations in transactions; feature flags; foreign keys on; Tradier retries with
backoff on 429 and 5xx; bounded timeouts on the Robinhood bridge; backtest slippage,
commission, short borrow fee and a seeded Monte Carlo; walk-forward with separate train and
test windows; causal trend and reversal engines with truncation tests; option backtests off
until a historical option source exists.

## Implementation map

| Sprint | Requirements | Main files | Depends on |
| --- | --- | --- | --- |
| 0 Safety net | 6.3 migration tests, 8.2 CI, backups | `backup.py`, `db.py`, `tests/test_migrations.py`, `tests/fixtures/`, `.github/workflows/tests.yml`, `ruff.toml` | none |
| 1 P0 defects (2.1.1) | 1.1, 1.2, 1.3, 1.4, $0 totals, stress multiplier | `portfolio.py`, `positions.py`, `analysis.py`, `service.py`, `alerts.py`, `v2.js` | 0 |
| 2 History integrity (2.1.1) | 2.1 to 2.5 | `positions.py`, `snapshots.py` (new), `desk.py`, `journal.py`, `api.py`, `api_v2.py`, `schema_v2.py` | 1 |
| 3 Exposure and risk (2.2) | 3.1, 3.2, 3.5, 3.6 | `portfolio.py`, `desk.py`, `analysis.py`, `v2.js` | 1 |
| 4 Accounting (2.2) | 3.3, 3.4 | `positions.py`, `schema_v2.py`, `desk.py`, `portfolio.py`, `api_v2.py` | 2 |
| 5 Market data (2.2) | 4.1, 4.2, 4.3 | `freshness.py` (new), `market.py`, `desk.py`, providers, `v2.js` | 3 |
| 6 Analytics (2.3) | 5.1 to 5.5 | `analytics.py`, `signalbook.py`, `reversal.py`, `backtest*.py`, `research.py` | 2, 4 |
| 7 Diagnostics, UX, release (2.3) | 6.1, 6.2, 6.4, 7.1 to 7.5, 8.3, 8.4 | `reconcile.py` (new), `api_v2.py`, `v2.js`, docs | all |

## Sprint log

### Sprint 0: safety net

No change to how the app behaves, except that an existing database file is now copied to
`data/backups/` before any migration runs on it.

- `tradeassist/backup.py`: consistent online copies (`sqlite3.Connection.backup`), named
  `<db>-<label>-<UTC time>.db`, newest ten automatic copies kept. `Database.backup(label)`
  for manual copies.
- `db.py`: an existing file with migrations pending is copied before the schema script or any
  migration touches it; `Database.backup_before_migration` names the copy.
- Fixtures made by the released code of 0.4.0, 2.0.0 and 2.1.0 (`tests/fixtures/*.sql`, built by
  `make_legacy_dbs.py`): classic journal entries in every state, an expired-worthless
  trade, stock long with a partial exit, a closed short, a planned and a cancelled plan, and
  a stock position closed and then bought again (the 1.2 defect, as real data).
- `tests/test_migrations.py`: each fixture migrates to the latest version with every journal
  entry, attempt, fill, option leg, snapshot and plan unchanged; reopening changes nothing;
  a failing migration leaves the file as it was; foreign keys are enforced; backups and
  pruning.
- CI (`.github/workflows/tests.yml`): Python 3.11 and 3.13, compile and import checks, ruff
  (`ruff.toml`: syntax, undefined names, unused imports, likely bugs, no eval/exec),
  migration tests, the full suite with branch coverage, and a non-blocking `pip-audit`.
  `requirements-dev.txt` adds pytest-cov, hypothesis and ruff.
- Lint fixes: four unused imports in tests, one unused loop variable.

### Sprint 1: P0 accounting and risk defects (2.1.1)

| Requirement | Done | Where | Tests |
| --- | --- | --- | --- |
| 1.1 Portfolio risk with unpriced positions | Same fields for every position; unknown figures are `None`; `priced`, `missing`, `missing_fields`; totals over known figures with `coverage` and `unknown_counts`; incomplete-prices warning; stress tests list `excluded` | `portfolio.py` | `test_p0_defects.py::test_1_1_*` (6) |
| $0 totals for unpriced positions | Fixed with 1.1: no unknown figure enters a total | `portfolio.py` | same |
| Stress multiplier | Options scaled by their own multiplier | `portfolio.stress` | `test_1_1_stress_uses_the_position_multiplier_not_always_100` |
| 1.2 Closed positions terminal | `account()` refuses any fill after a full close; `add_execution` refuses closed positions and back-dated fills that would split a trade; `recompute` refuses status changes outside `TRANSITIONS`; positions reopened by older versions stay readable, are flagged in `lifecycle_issues`, and can still be closed | `positions.py`, `v2.js` | `test_1_2_*` (7), property tests |
| 1.3 Daily-loss completeness | `loss_status` and `coverage_status` separate; `known_total`, `missing` with reasons, `positions_evaluated_pct`, `capital_evaluated_pct`; `status` is INCOMPLETE rather than OK when anything is unmeasured; realized losses now count in REALIZED_ONLY mode without a previous close (a further defect found while fixing this); `INCOMPLETE_RISK_POLICY` WARN/BLOCK for the ticket; ticket never says "inside all of your limits" when it could not check; alerts DAILY_LOSS_UNKNOWN and OPEN_RISK_UNKNOWN | `portfolio.py`, `service.py`, `alerts.py`, `config.py`, `app.js`, `v2.js` | `test_1_3_*` (8) |
| 1.4 Finite numbers | `positions.number()`; fills, plans, strikes, multipliers, ledger results, journal prices and settings checked at the domain layer; minimum quantity 1e-6; zero prices allowed | `positions.py`, `journal.py`, `config.py` | `test_1_4_*` (14), `test_ledger_properties.py` (4 properties, 300 examples each) |

API changes (backward compatible): exposure figures for an unpriced position are `null`
instead of `0`; `daily_loss.status` can be `INCOMPLETE`; new fields `coverage`,
`totals.complete`, `totals.unknown_counts`, `totals.open_risk_complete`,
`stress[].excluded`, `stress[].complete`, `daily_loss.loss_status`, `coverage_status`,
`known_total`, `missing`, `positions_evaluated_pct`, `capital_evaluated_pct`,
`unrealized_complete`, ticket `risk_notes`, `within_limits`, `incomplete_risk_policy`.
The front end was updated for all of them.

Behaviour that needs your attention: a stock position without an invalidation price now
raises an OPEN_RISK_UNKNOWN alert, because its risk cannot be measured.

Checked on screen (demo data, 1280 px and 390 px): portfolio with an unpriced option shows
"unknown" in its row, "Partial" on the totals, "Not known if within the stop" on the daily
stop, and "(1 left out)" on each stress test; no horizontal scrolling.

Tests: 390 passed, 2 skipped before the property tests; 394 passed, 2 skipped with them.

### Sprint 2: position history and journal integrity (2.1.1)

Migration 5 adds snapshot provenance columns, `execution_corrections`,
`snapshot_reconstruction`, `executions.external_id` with a unique index per source, and a
unique index allowing one effective PLAN, ENTRY and EXIT per position. It infers provenance
for existing snapshots and marks duplicates superseded; it changes no fill.

| Requirement | Done | Where | Tests (`test_history_integrity.py`) |
| --- | --- | --- | --- |
| 2.1 Snapshots for journal trades | Shared rules in `snapshots.py` used by the desk and, through journal listeners, by the classic journal; PLAN from the ticket's saved quote; idempotent `reconcile` and `backfill` with status per attempt; `/v2/snapshots/backfill` and `/status`; "Rebuild missing snapshots" button | `snapshots.py`, `desk.py`, `journal.py`, `api_v2.py`, `v2.js` | `test_2_1_*` (3), migration 5 test |
| Stable journal mirror (found in audit) | `sync_legacy_trade` reconciles fills in place; ids stable; changes recorded as JOURNAL_SYNC corrections; runs on migration-1 tables only, since migration 1 calls it | `positions.py` | `test_journal_sync_is_idempotent_and_keeps_fill_ids`, `test_resyncing_a_2_1_database_changes_no_fill` |
| 2.2 Clearing plan fields | `exclude_unset`; null clears nullable fields, omitted keeps, text fields refuse null; `initial_risk_per_unit` added to the API; the plan form sends null for a cleared box | `api_v2.py`, `positions.py`, `v2.js` | `test_2_2_*` (3) |
| 2.3 Time-accurate snapshots | LIVE within 15 minutes of the fill, else PRIOR_BAR from the last finished bar before it; fill price as the stock price; trend and regime extras use the same cutoff; `uses_later_data`; analytics ignore hindsight snapshots and report how many trades lack entry context | `desk.py`, `snapshots.py`, `analytics.py` | `test_2_3_*` (3), hindsight replacement test |
| 2.4 Audited corrections | `correct_execution` with reason, source, before/after values and ledger figures in one transaction; may change status (the one sanctioned way); dependent snapshots superseded and rebuilt; journal mirrors refused; UI form and history | `positions.py`, `desk.py`, `api_v2.py`, `v2.js` | `test_2_4_*` (6) |
| 2.5 Import idempotency | `external_id` per source: repeat is a no-op, a conflicting repeat is refused; fills without an id are never merged | `positions.py`, `api_v2.py` | `test_2_5_*` (2) |

Version set to 2.1.1. Tests: 415 passed, 2 skipped.

Known limitations: there is no broker import in the app, so 2.5 covers the recording side
only. Journal fill times are when the attempt was logged. Rebuilt snapshots have no option
quotes or Greeks, because no connected source has their history.

### Sprint 3: exposure, open risk, marks and risk models (2.2)

| Requirement | Done | Where | Tests (`test_risk_models.py`) |
| --- | --- | --- | --- |
| 3.1 Gross and net | Totals, `by_symbol` and `by_sector` with net, gross, long, short; concentration warnings on gross; options keep delta dollars, premium, notional and liquidation value apart; `definitions` | `portfolio.py`, `v2.js` | `test_3_1_*` (2) |
| 3.2 Open-risk completeness | `risk_basis` per position (INVALIDATION, PREMIUM, CONFIGURED, BEYOND_INVALIDATION, UNKNOWN); `risk_coverage` with bases, unknowns and a limit conclusion that says whether it covers everything; stock past its invalidation is no longer counted as zero risk; alerts use `open_risk` | `portfolio.py`, `analysis.py`, `v2.js` | `test_3_2_*` (2) |
| 3.5 Mark-to-market | Last, bid, ask, mid, spread, analytical mark with its basis, liquidation price (bid for long, ask for short, zero with no bid); liquidation P&L per position and in total; shown on the position and portfolio screens | `market.py`, `desk.py`, `portfolio.py`, `v2.js` | `test_3_5_*` (2) |
| 3.6 Risk models | Returns without forward fill; beta and correlation on overlapping days with a 40-day minimum and sample sizes; return-quality flags (short history, missing days, stale runs); historical 1-in-20 loss, expected shortfall and worst day beside the normal approximation; stress floors stock at zero; reference tests (least squares beta, hand-computed covariance and quantiles, Hull's Black-Scholes example, vega units) | `portfolio.py`, `v2.js` | `test_3_6_*` (8) |

Confirmed and fixed here: forward-filled closes made missing days zero returns (2.1 beta and
correlation were biased toward zero for stocks with gaps); a stock through its invalidation
price counted as zero open risk; a beta of 3 in a 50% crash took a stock below zero in
stress tests.

Tests: 429 passed, 2 skipped.

### Sprint 4: short-sale financing and the option lifecycle (2.2)

Migration 6 adds `position_adjustments` and `option_events`. It changes no fill.

| Requirement | Done | Where | Tests (`test_short_and_options.py`) |
| --- | --- | --- | --- |
| 3.3 Short accounting | Borrow fees (several, with periods and rates), borrow-rate entries, dividends owed and received, cash adjustments, voiding with a reason, import ids; trading profit, financing and net economic profit kept apart; borrow status UNKNOWN / PARTIAL / RECORDED with a labelled estimate that is never added; partial covers covered; BORROW_UNKNOWN alert | `positions.py`, `desk.py`, `api_v2.py`, `alerts.py`, `v2.js` | `test_3_3_*` (5) |
| Corporate actions | Stock splits replay earlier fills in post-split units and rescale the plan (voidable); daily loss uses the adjusted fills | `positions.py`, `portfolio.py` | split and daily-loss tests |
| 3.4 Option lifecycle | Expired worthless (refused before expiry; reason required if in the money), exercised (intrinsic close plus shares at the same price, new or existing stock position, linked both ways), contract adjustments (original terms kept, value preserved), UNRESOLVED state with alert and unknown risk; journal mirrors refused; assignment refused by the schema | `positions.py`, `desk.py`, `portfolio.py`, `alerts.py`, `v2.js` | `test_3_4_*` (9) |

Tests: 443 passed, 2 skipped.

Known limitations: no broker feed for borrow fees or dividends, so they are entered by hand
(or posted with an import id). Mergers, spin-offs and cash-settled adjustments are not
modelled. Exercised shares are contracts x multiplier.

### Sprint 5: market-data freshness and failure recovery (2.2)

| Requirement | Done | Where | Tests (`test_freshness.py`) |
| --- | --- | --- | --- |
| 4.1 Freshness and quality | `observe()`: instrument, provider, observed and retrieved times, session, FRESH / DELAYED / LAST_SESSION / STALE / UNKNOWN, completeness and warnings; thresholds by kind, configurable; weekends and the latest session are not stale; daily bars checked against the last finished session | `freshness.py`, `market.py`, `config.py` | `test_4_1_*` (5) |
| 4.2 Failures | Source health with a pause after three consecutive failures (requests are not sent while paused); quotes never silently replaced, stale fallback only for read-only views and always marked; history refresh failures keep saved bars and say so; invalid and crossed quotes flagged | `freshness.py`, `market.py`, `desk.py` | `test_4_2_*` (4) |
| 4.3 Shown in the UI | Freshness tags on position prices and marks, the positions list, the portfolio (STALE_PRICES warning and per-row tag), the Analyze quote and data checks; `/v2/data/{symbol}` and `/v2/source/health` | `v2.js`, `api_v2.py`, `portfolio.py` | `test_4_3_*` (2) |

Already in place and kept: Tradier retries with backoff on 429 and 5xx and honours its
rate-limit headers; the Robinhood bridge has call timeouts.

Version set to 2.2.0. Tests: 454 passed, 2 skipped.

Known limitations: no market-holiday calendar (one extra day is tolerated); option chains
are not cached, so a failed chain shows as no price rather than a stale one.

### Sprint 6: analytics, signals and backtests validated (2.3)

Migration 7 adds `research_runs`.

| Requirement | Done | Where | Tests (`test_validation.py` unless noted) |
| --- | --- | --- | --- |
| 5.1 Journal analytics | One definition: a trade is one position lifecycle; result is net economic profit (trading plus financing); definitions in the response; habits are True, False or not checked, and unchecked trades are left out of both comparison groups (a confirmed defect: 2.1 counted trades without price history as "did not hold past invalidation"); coverage of entry context, excursions, R and borrow costs; notes for unknown borrow costs and reopened positions | `analytics.py`, `v2.js` | `test_5_1_*` (3) |
| 5.2 Trend and reversal causality | Audit: the engines are causal. New harness on three random walks and three cut points checks indicators, patterns, trend states, reversal stages and events and backtest conditions against a truncated history, and recorded signals and their outcomes | tests | `test_5_2_*` (4) plus existing truncation tests in `test_trend.py`, `test_reversal.py`, `test_regime.py`, `test_signals.py`, `test_backtest.py`, `test_backtest_v2.py` |
| 5.3 Calibration | Dependence-adjusted intervals on separate periods (a confirmed defect: 2.1's intervals treated overlapping cases as independent, so they were too narrow); naive interval kept for reference; held-out check on the latest 30% of dates and `calibration_status`; frequencies labelled as historical, not forecasts | `signalbook.py`, `v2.js` | `test_5_3_*` (2) |
| 5.4 Backtest | Audit (all confirmed in place, with tests): next-open entries, short direction, borrow fees, stop before target, gap fills, slippage and commission, holding and end-of-test exits, cooldown, cash and position limits, survivorship note, options refused. New: walk-forward choices unchanged when every price after the training window is scrambled; Monte Carlo identical with the same seed; position limits under constant signals | `backtest.py`, `backtest_v2.py` | `test_5_4_*` (3) plus `test_backtest.py`, `test_backtest_v2.py` |
| 5.5 Reproducibility | `runs.py`: settings, data and result hashes for backtests, walk-forwards, pattern runs and signal histories; stored, listed and compared; identical inputs give identical results | `runs.py`, `analysis.py`, `service.py`, `api_v2.py`, UI | `test_5_5_*` (2) |

Tests: 468 passed, 2 skipped.

Known limitations: MAE and MFE use daily bars, so the entry day's own range is not counted.
Calibration and signal histories built before 2.3 keep their old intervals until rebuilt.

### Sprint 7: diagnostics, data health, logging and release (2.3)

Migration 8 adds the database rules (triggers) and `maintenance_log`.

| Requirement | Done | Where | Tests (`test_diagnostics.py` unless noted) |
| --- | --- | --- | --- |
| 6.1 Database integrity | Triggers refuse a planned quantity or multiplier at or below zero, an option leg without a positive strike and multiplier or a date for its expiration, a fill without a full time stamp, and a split without a positive ratio. An update is refused only when it makes a good row bad, so rows from before 2.3 can still be recomputed; they are reported under `rule_breaks`. Foreign keys checked on | `schema_v2.py`, `reconcile.py` | `test_6_1_*` (2) |
| 6.2 Reconciliation | `diagnose()`: every position recomputed from its fills and compared field by field; reopened positions, invalid states, possible duplicate fills (not merged), journal mirrors that differ, expired options still open, missing price history, snapshot problems, rule breaks, `PRAGMA integrity_check` and `foreign_key_check` | `reconcile.py`, `GET /v2/diagnostics/reconciliation` | `test_6_4_a_clean_book_reconciles`, `test_6_4_journal_drift_*` |
| 6.4 Repair | `repair()` only with `{"confirm": true}`; copies the database first and refuses without the copy; re-reads mismatched journal entries; recomputes mismatched positions with `correction=True`; never changes, merges or deletes a fill; leaves reopened positions as recorded; logs to `maintenance_log`; a second run changes nothing | `reconcile.py`, `POST /v2/diagnostics/repair`, Data health screen | `test_6_2_*`, `test_6_4_a_2_1_database_report` |
| 7.1 Portfolio summary | A headline strip (positions, gross and net exposure, open risk with what is unmeasured, today against the stop, how many prices are current) and every warning in one panel, grouped Critical, Warning and For information. The positions list no longer adds an unknown unrealized figure in as $0 | `v2.js` | `test_static_assets.py`, screenshots |
| 7.2 Position detail | Cost (or proceeds) of the open position and its worth at the mark; unknown when there is no price | `v2.js` | screenshots |
| 7.3 Trade review | `analytics.review()`: planned, executed, known at entry (never hindsight), thesis invalidated, targets reached, result after fees and financing, habits checked and not checked, what happened, what is missing; descriptive, not causal | `analytics.py`, `v2.js` | `test_7_3_*` |
| 7.4 Data health | `GET /v2/health/data` and a Data health screen: source health, price freshness of open positions, positions not valued, option chains missing the contract, saved history, snapshots (with rebuild), schema and backups, reconciliation summary and report, repairs, recent problems; no credentials | `api_v2.py`, `v2.js`, `index.html` | `test_7_4_*`, `test_static_assets.py` |
| 7.5 Error messages | Refused changes stay plain 400s; database rule or uniqueness clashes are 409 "Nothing was saved"; a busy database is 503; a stale option quote says "The quote for this contract is stale. Refresh the chain before evaluating it." | `api.py`, `service.py` | `test_7_5_*`, `test_position_errors_stay_plain` |
| 8.3 Logging | `events.py`: `event=NAME key=value` lines for provider failures, stale data, missing valuations, risk failures, snapshot failures, migrations (with the backup), refused transitions, corrections and repairs; credential-shaped values redacted, also in every other module's log lines; recent warnings kept for the health screen | `events.py`, `market.py`, `desk.py`, `db.py`, `api.py`, `analysis.py`, `run.py` | `test_8_3_*` (2) |
| 8.4 Release validation | Checklist below | | |

Found and fixed during this sprint:

| Finding | Status |
| --- | --- |
| **2.2.0 (Sprint 5) shipped a page script that the browser rejected.** `v2.js` declared `const ageText`, which `app.js` already declares as a function; scripts share one global scope, so the browser refused all of `v2.js`. The Analyze, Positions and Portfolio risk buttons were hidden and the Version 2 additions to the Journal, Pattern lab and Backtest screens did not appear. The classic screens kept working, which is why the API tests did not notice. | **Confirmed and fixed** (renamed to `ageShort`). New `test_static_assets.py`: no top-level name in both scripts, every navigation button has a screen, both scripts compile as one page (Node), and every screen opens in Chromium without a script error (Playwright; skipped where not installed). All three fail on the 2.2.0 code |
| The positions list added positions with no price into the unrealized total as $0 | **Confirmed and fixed**: left out, counted, and the total marked "+ unknown" |
| A trigger that refused any update to a row that broke a rule would have frozen rows written by older versions | Fixed before release: updates are refused only when they make a good row bad |
| Test databases left open for the garbage collector | `conftest.py` closes every database a test opens |

Version set to 2.3.0. Tests: 484 passed, 2 skipped.

Known limitations: the database rules cover new writes only; existing rows that break them
are reported, not changed. Duplicate-looking fills are reported, never merged, because two
real orders can fill alike. The recent-problems list is kept in memory and starts empty when
the app restarts; the full log is in the terminal running the app.

## Independent review and 2.3.1

Two reviewers that had not seen the code being written checked everything from 2.1.0 to
2.3.0: one for accounting and data integrity, one for risk figures, look-ahead, market-data
freshness and security. Each finding was reproduced with a script before it was fixed, and
each now has a regression test in `tests/test_review_fixes.py` (named by its id below) that
fails on 2.3.0 and passes on 2.3.1. Migration 9 is new; migration 8 was left as shipped.

| Id | Severity | Finding (confirmed by reproduction) | Fix |
| --- | --- | --- | --- |
| B1 | High | The ticket's open-risk limit counted only option-journal premium: a stock position over the limit left the check passing | Open risk of every other position on its stated basis is added (`TradeAssistant.book_risk`); unmeasured risk is a note, or a limit under `INCOMPLETE_RISK_POLICY=BLOCK` |
| B2 | High | When positions could not be read, the daily loss fell back to the journal and reported COMPLETE and OK | It is UNAVAILABLE and INCOMPLETE, with the reason |
| B3 | High | A stale saved quote counted as a measurement in the daily loss, so the ticket could show the day as complete and OK | A stale price does not measure the day; the position is listed as not measured |
| A1 | High | One position whose fills no longer account (possible in 2.1.0 data) made the positions list, portfolio risk, reconciliation, data health and repair all fail | Listed apart with its fills and a Correct button, counted as not measured everywhere; the fill can be corrected; repair works item by item |
| B4 | High/medium | The bad-day figures silently left out a held stock with no history | `left_out`, `complete`, the share of exposure measured, a BAD_DAY_PARTIAL warning, and the UI names it |
| B5 | Medium | Credentials could reach the health endpoints, error replies and tracebacks; the redaction rules missed short bearer tokens, JSON keys and other forms | Source errors are redacted where they arise; broader rules; tracebacks and uvicorn's loggers redacted |
| B6 | Medium | The calibration's held-out dates were part of the test they were meant to confirm | The check uses the earlier 70% of dates only |
| B7 | Medium | Yesterday's close for the daily loss could come from history that failed to refresh (days old) | Not used; the position is listed with the reason |
| B8 | Medium | No Host check (DNS rebinding could reach the API); a body-less cross-site POST was accepted | Host names limited to this computer; writes from another page refused (403) |
| A2 | Medium | A split, a voided split or a contract adjustment could reopen a closed position with no audit record, leaving a stale exit snapshot | Refused unless confirmed with a reason; recorded as a correction; snapshots reconciled |
| A3 | Medium | A split entered as a date was applied again to fills made earlier that day | A plain date is the ex-date (start of day) |
| A4 | Medium | A remainder below the smallest recordable quantity (from rounding or a split) could never be closed | Closed with the fill that left it (`rounded_off`); an over-close by less than that amount closes what is open |
| A5 | Medium | Borrow status said RECORDED when fees covered only some days | Coverage by calendar day; PARTIAL names the uncovered days; the estimate covers them |
| B9 | Medium/low | Research fingerprints missed the benchmark and regime bars, so changed inputs looked like non-determinism | Every bar set a run reads is fingerprinted (`Market.recording`) |
| B10 | Medium/low | Freshness was judged from the last trade's time before the bid and ask time | The older of the bid and ask times first |
| A6 | Low/medium | Voiding a split restored stale plan levels; the planned size was not rescaled for a position recorded after the fact | The split's own scaling is undone on today's values; the size rule uses the first fill too |
| A7 | Low | Repair re-read journal mirrors (changing their fills) while saying no fill was changed; the journal check folded extra fills | Wording corrected; fills compared as lists |
| A8 | Low | A back-dated fill could move a reopening on a position reopened by an older version | Refused unless it removes one |
| A9 | Low | Thesis and notes edited on a journal mirror were overwritten at the next journal change | Edited in the journal; refused on the mirror with the reason |
| A10 | Low | Expiring an option worthless with no stock price known skipped the in-the-money check | A reason is required |
| A11 | Low | Migration 8 checked an option leg's three fields in one rule, so a leg with one old fault could get another | Migration 9: one rule per field |
| B11 | Low | A quote time in the future was FRESH | UNKNOWN |
| B12 | Low | A Monday holiday made Friday's close stale (safe side) | The allowance reaches the previous weekday's session |
| B13 | Low | Trade reviews used the exit day's range after an intraday exit | Left out unless the exit was at the close |
| B14 | Low | The dependence-adjusted interval was too narrow when one period held most cases | Periods weighted by size: (sum of sizes)^2 / (sum of squared sizes) |
| B15 | Low | A total nothing could be measured for showed $0 | `null`, shown as unknown |
| B16 | Low | A stress test of a bought option without a volatility could lose more than its value | Capped at its value |

Checked and found sound by the reviewers: ledger replay for adds, partial exits, fees, shorts
closed with BUY or BUY_TO_COVER, splits after a partial close and exercise; migrations 5 to 8
on all three fixtures (no original value changed, re-running changes nothing, backups taken,
rollback on failure); snapshot cut-offs and idempotent backfills; import ids; returns without
forward fill, the 40-day beta minimum, VaR and expected-shortfall signs and units; the
pause logic of the source health; no endpoint that places, changes or cancels an order; no
stale quote in option pricing; deterministic research hashes.

Version set to 2.3.1. Tests: 513 passed, 2 skipped.

## Test evidence (deliverable C)

Run on Python 3.13.16 with pandas 3.0.5, numpy 2.5.3, FastAPI 0.143.0 and pydantic 2.13.5:
`python -m pytest --cov=tradeassist`.

| Point | Passed | Skipped |
| --- | --- | --- |
| Baseline, 2.1.0 (`d614c7a`) | 348 | 2 |
| Sprint 0 | 355 | 2 |
| Sprint 1 | 394 | 2 |
| Sprint 2 (2.1.1) | 415 | 2 |
| Sprint 3 | 429 | 2 |
| Sprint 4 | 443 | 2 |
| Sprint 5 (2.2.0) | 454 | 2 |
| Sprint 6 | 468 | 2 |
| Sprint 7 (2.3.0) | 484 | 2 |
| **Review fixes (2.3.1)** | **513** | **2** |

The two skips are the same as at the baseline (`test_optionmath.py`: implied volatility
cannot be solved from a near-zero price). The one warning is the test client's own
deprecation notice.

New test files in this programme: `test_migrations.py` (7), `test_p0_defects.py` (35),
`test_ledger_properties.py` (4, property-based with hypothesis, derandomized),
`test_history_integrity.py` (21), `test_risk_models.py` (14), `test_short_and_options.py`
(14), `test_freshness.py` (11), `test_validation.py` (14), `test_diagnostics.py` (12),
`test_static_assets.py` (4), `test_review_fixes.py` (29). Test names begin with the requirement they prove
(`test_1_2_*`, `test_6_4_*`, ...).

Line coverage of the package: **93%** (10,889 statements). The modules this programme
changed most:

| Module | Coverage |
| --- | --- |
| `positions.py` | 94% |
| `portfolio.py` | 97% |
| `snapshots.py` | 90% |
| `analytics.py` | 94% |
| `signalbook.py` | 95% |
| `freshness.py` | 89% |
| `market.py` | 93% |
| `desk.py` | 88% |
| `reconcile.py` | 90% |
| `events.py` | 90% |
| `runs.py` | 92% |
| `backup.py` | 97% |
| `db.py` | 99% |
| `schema_v2.py` | 95% |
| `api_v2.py` | 95% |

Lint: `ruff check .` passes (rules in `ruff.toml`). Screens were checked in Chromium at
1280 and 390 pixels wide: no horizontal scroll and no script errors (the only console
messages are the web font, which this build environment cannot reach, and a missing
favicon).

## Migration and reconciliation report (deliverable D)

Each fixture is a database written by the released code of that version
(`tests/fixtures/make_legacy_dbs.py`), with journal entries in every state and, from 2.0.0
on, unified positions including one reopened after closing. Each was opened by 2.3.0, which
migrated it, then reconciled and repaired (2.3.1 adds migration 9, which only replaces the
option-leg rule, and gives the same results):

| | 0.4.0 | 2.0.0 | 2.1.0 |
| --- | --- | --- | --- |
| Schema | none → 8 (9 in 2.3.1) | 3 → 8 (9) | 4 → 8 (9) |
| Copy taken before migrating | yes (`…-before-migration-1-…`) | yes (`…-before-migration-4-…`) | yes (`…-before-migration-5-…`) |
| Journal entries / attempts kept | 5 / 4 | 5 / 4 | 5 / 4 |
| Fills | 0 → 5 (journal mirrored) | 12, unchanged | 12, unchanged |
| Positions (open, closed, planned, cancelled) | 5 (1, 2, 1, 1) | 10 (3, 3, 2, 2) | 10 (3, 3, 2, 2) |
| Stored figures that differ from the fills | 0 | 0 | 0 |
| Reopened after closing | 0 | 1 (KO, position 8) | 1 (KO, position 8) |
| Snapshots built with hindsight | 0 | 5 | 5 |
| Snapshots missing | 10 | 10 | 10 |
| Open positions without saved history | 1 | 3 | 3 |
| Rule breaks, duplicates, journal drift, invalid states | 0 | 0 | 0 |
| SQLite integrity and foreign keys | ok | ok | ok |
| Repair | nothing to change; copy taken | nothing to change; copy taken | nothing to change; copy taken |

What this means for a real database:

- Upgrading changes no fill and loses no journal entry; the copy in `data/backups/` is the
  way back.
- A position reopened by an older version is kept exactly as recorded, flagged on its
  screen and in the report, and its figures still add up its fills. New trades in the same
  stock now go on a new position.
- Snapshots that 2.0 and 2.1 built from the fill day's close (defect 2.3) are marked as
  hindsight and are not used to judge an entry; "Rebuild missing snapshots" replaces them
  from the prior session's close where the saved history allows, and records the ones it
  cannot rebuild.
- Missing snapshots and history gaps in these fixtures are because the test has no price
  history; on a real database the first rebuild fills most of them.

## Release validation checklist (8.4)

| Check | Result |
| --- | --- |
| Full test suite | 513 passed, 2 skipped (2.3.1) |
| Lint | `ruff check .` clean |
| Independent review | two reviewers; 27 findings reproduced and fixed with regression tests (above) |
| Migrations from 0.4.0, 2.0.0 and 2.1.0 databases, with a copy first | pass (`test_migrations.py`, report above) |
| A failed migration rolls back and leaves the copy | pass (`test_migrations.py`) |
| Ledger property tests | pass (`test_ledger_properties.py`) |
| No endpoint places, changes or cancels an order; Robinhood tools are read-only | unchanged and re-checked by `test_robinhood.py` (order and account tools refused) |
| No made-up historical option prices | option backtests stay off (`options_backtest` flag) and refuse to run |
| Unknown never shown as zero | portfolio totals, daily loss, open risk, analytics and the positions list (tests in `test_p0_defects.py`, `test_risk_models.py`, `test_validation.py`) |
| No credential in logs, health endpoints, error replies or the health screen | pass (`test_8_3_*`, `test_7_4_*`, `test_B5_*`) |
| Requests from other web pages cannot change data | pass (`test_B8_*`) |
| Every screen opens without a script error, desktop and phone width | pass (`test_static_assets.py`; screenshots) |
| Version numbers | `tradeassist.__version__` 2.3.1; the page warns when it was loaded from an older version |
| CI | the workflow is in `.github/workflows/tests.yml`, but GitHub Actions has not run on this repository (no runs recorded; it appears to be switched off in the repository's settings) |
