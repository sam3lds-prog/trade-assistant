# Reliability, accounting, risk and analytics improvements (2.1.1 to 2.6)

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

The two reviewers then checked the fixes twice more. They confirmed all 27 and found
problems in the fixes themselves, all now fixed with tests (`test_followup_*`); the third
check found only the last three rows:

| Severity | Problem in the first fixes | Fix |
| --- | --- | --- |
| Medium | Adding the stock's open risk to the per-trade limit blocked hedges (a protective put on held shares failed a non-overridable check) | The per-trade limit counts only the journal's risk in the stock, as before; total open risk includes the Positions screen |
| Medium | The ticket's open risk could be measured from a saved quote | A position priced from a saved copy is unknown, with the reason |
| Medium | The Positions screen could no longer save any plan field on a journal mirror (it always sends thesis and notes) | Only a change to thesis or notes is refused |
| Medium | Contract adjustments entered as a date still missed same-day fills | A plain date is the ex-date here too |
| Medium/low | An adjustment could still turn an open position into two trades, and a correction on an unreadable position could create a reopening | Both refused unless confirmed (adjustments) or at all (corrections) |
| Low | Repair caught only position errors and logged only at the end | Database errors are reported per item; the log is always written |
| Low | Snapping a remainder mid-history turned a later purchase into a "reopening" on 2.3.0 data | A remainder is closed only at the end of the replay |
| Low | An open short was PARTIAL every day (tonight's fee is never booked yet) | Coverage counts nights, not including tonight |
| Low | Redacting uvicorn's access lines broke its formatter | Arguments are redacted in place |
| Low | The exit-day rule also cut today's bar from open positions | Applied only to an actual exit |
| Low | Any failed history refresh dropped yesterday's close | Kept when the saved history reaches the weekday before today |
| Low | An unreadable position closed weeks ago kept today's loss UNAVAILABLE | Only open ones, or ones with a fill today, count |
| Low | No gap between the calibration check and the held-out dates | One horizon left out before the cut |
| Low | Short tokens after the word "token" were not redacted; `state=` in SEC URLs was | Both corrected |
| Low (pre-existing) | A saved quote's age was measured to when it was saved ("0 seconds old") | Measured to now; `saved_at` added |
| Medium/low (third round) | Keeping saved history after a failed refresh could take a bar saved mid-session as yesterday's close | Finished bars only |
| Low (third round) | A position from an older version that was reopened and also has a bad fill could not be corrected | Reopenings are compared with those in the fills that still account |
| Low (third round) | A short opened today read "Covered the same day" | Its own wording |

Possibly more INCOMPLETE days in live trading (suspected by a reviewer, not reproducible on
demo data) was addressed by the second row's rule: only a saved copy is unmeasured; a quote
the source gives now counts and is labelled, however long since it changed.

Left as they are, by choice: the redaction errs toward hiding, so an ordinary word after
"authorization:" or "token:" in an error message is also hidden; a split recorded by 2.2 or
2.3.0 on a position that later had a second split may not have its planned size restored when
voided (its levels are), because those versions did not record whether they rescaled it.

Version set to 2.3.1. Tests: 527 passed, 2 skipped.

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
| **Review fixes (2.3.1)** | **527** | **2** |

The two skips are the same as at the baseline (`test_optionmath.py`: implied volatility
cannot be solved from a near-zero price). The one warning is the test client's own
deprecation notice.

New test files in this programme: `test_migrations.py` (7), `test_p0_defects.py` (35),
`test_ledger_properties.py` (4, property-based with hypothesis, derandomized),
`test_history_integrity.py` (21), `test_risk_models.py` (14), `test_short_and_options.py`
(14), `test_freshness.py` (11), `test_validation.py` (14), `test_diagnostics.py` (12),
`test_static_assets.py` (4), `test_review_fixes.py` (43). Test names begin with the requirement they prove
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
| Full test suite | 527 passed, 2 skipped (2.3.1) |
| Lint | `ruff check .` clean |
| Independent review | two reviewers; 27 findings reproduced and fixed with regression tests, then the fixes re-checked twice by the same reviewers and their 18 follow-up problems fixed (above) |
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

## 2.4: options analytics, scanner, strategies, AI and news

Requested: implied volatility and pricing analytics (Black-Scholes and Monte Carlo), sentiment
from news and market flow, scanners for unusual activity and technical signals, strategy
modelling for multi-leg trades described in plain language, and an AI plug-in that pulls
the latest news and the market's make-up. Built in five parts, each merged into `main`
separately, under the same rules as before: the app never places an order, unknown values
are never shown as zero, historical frequencies are labelled as such, and new capabilities
have a feature flag.

### Part 1: options lab (volatility and pricing)

| Requirement | Done | Where | Tests (`test_options_lab.py` unless noted) |
| --- | --- | --- | --- |
| Implied volatility across expirations | At-the-money volatility, 1-sd expected move and range, straddle, 25-delta call and put volatility, skew and butterfly for up to eight expirations spread from a week to a year; term-structure shape; 30-day volatility by total-variance interpolation | `options/volatility.py`, `options/lab.py` | `test_iv_at_delta_*`, `test_expiration_row_*`, `test_shape_*`, `test_iv30_*`, `test_pick_expirations_*` |
| Implied earnings move | From the extra variance after the report, with either an expiration before it or two after it; report-day expirations left out; reasons when not measurable | `options/volatility.py` | `test_implied_earnings_move_*` (3) |
| Implied against realized | 10, 20 and 60-day realized volatility from finished bars; premium and ratio against the 20-day figure, not shown when the stock has not moved | `options/volatility.py` | `test_realized_*`, `test_profile_*` |
| Monte Carlo | Risk-neutral paths, antithetic, seed 7, bounded size; value with standard error checked against Black-76; finish and touch odds (closing price simulated, BGK and reflection formulas); outcome histogram; buyer's profit share and percentiles at a given price; optional earnings jump with its variance removed from the contract's volatility | `options/volatility.py`, `options/lab.py` | `test_monte_carlo_*` (8), `test_touch_*` (2), `test_an_earnings_jump_*`, `test_the_buyer_block_*`, `test_histogram_*` |
| API and screen | `GET /v2/options/{symbol}/volatility`, `POST /v2/options/montecarlo` (a model: nothing recorded); Options lab screen with the term-structure chart, expirations table and the simulation; `options_lab` flag | `api_v2.py`, `features.py`, `static/lab.js`, `index.html`, `api.py` | `test_volatility_endpoint_*`, `test_monte_carlo_endpoint_*`, `test_the_options_lab_can_be_switched_off`, `test_static_assets.py` (now covers three scripts) |

Found and fixed during this part:

| Finding | Status |
| --- | --- |
| A stock with no movement gave a realized volatility of about 1e-16 rather than exactly zero, so implied over realized came out in the trillions | Fixed: no ratio below 0.01 volatility points; test added |
| `test_followup_an_option_on_a_held_stock_is_not_blocked_by_the_shares_risk` failed on main from October 2026: the demo chain now puts a 5% out-of-the-money put above the $200 per-trade limit by itself, so the test no longer tested what its name says | Test now picks a put that fits the limit on its own; the check it guards (the shares' risk is not added to the option's) is unchanged and passes |

Known limitations: one volatility per path (no fat tails or volatility changes); the implied
earnings move assumes the base volatility is the same before and after the report; American
early exercise is not modelled (the chain's Black-76 prices are European on the forward).

### Part 2: scanner (technical signals and options activity)

| Requirement | Done | Where | Tests (`test_scanner.py` unless noted) |
| --- | --- | --- | --- |
| Scan many stocks for technical signals | RSI, MACD against its signal, 50 and 200-day averages, 52-week range, volume against its 20-day average and every pattern-lab pattern in the last 1 to 10 bars, for the watchlist, 100 large US companies or typed tickers (at most 120); filters; most recent signals first | `scanner.py`, `GET /v2/scan/technical` | `test_universes`, `test_readings_*`, `test_unknown_readings_*`, `test_a_volume_spike_*`, `test_technical_row_*`, `test_filters_and_order`, `test_technical_scan_endpoint` |
| Signals with honest odds | Each signal carries the signal history's pooled 10-day frequency with the overlap-aware interval and the lab's verdicts, labelled as a historical frequency; nothing is shown before the history is built | `scanner.py`, `signalbook.py` | `test_technical_scan_attaches_past_cases_*`, `test_technical_row_*` |
| Unusual options activity | Call and put volume, open interest, put/call ratios, approximate premium traded, call-heavy / put-heavy / balanced, and contracts trading at least their open interest; at most 30 stocks and six expirations each; never labelled bullish or bearish, and unknown volume stays unknown | `scanner.py`, `GET /v2/scan/flow` | `test_flow_summary_*`, `test_flow_thresholds_*`, `test_flow_with_no_volume_*`, `test_flow_expirations_*`, `test_options_activity_endpoint` |
| Screen and flag | Scanner screen with both scans, linked to Analyze and the Options lab; `scanner` flag | `static/lab.js`, `index.html`, `app.css`, `features.py` | `test_the_scanner_can_be_switched_off`, `test_static_assets.py` |
| Demo data | The occasional demo contract trades more than its open interest, so the scan has something to find | `providers/demo.py` | `test_demo_chains_now_and_then_*` |

Known limitations: the large-company list is fixed; volume and open interest are the
provider's (open interest is usually as of the previous close), and there is no record of
each contract's usual volume, so "unusual" is measured against open interest only.

### Part 3: strategy builder

| Requirement | Done | Where | Tests (`test_strategies.py` unless noted) |
| --- | --- | --- | --- |
| Multi-leg strategies (spreads, iron condors, straddles, butterflies, calendars) | 16 templates resolved to listed, two-sided contracts by delta, width, a middle strike or strikes you give; strike order checked; strikes far from the price refused | `options/strategies.py`, `POST /v2/strategies/build` | `test_contract_pickers`, `test_pick_expiration`, `test_default_strikes` (10), `test_credit_spreads_*`, `test_a_butterfly_*`, `test_strike_order_*`, `test_every_template_builds_*` |
| Risk, profit zones and limits | Net debit or credit at midpoints and natural; maximum gain and loss and breakevens from the simulator (checked against the payoff by hand); market-implied chance of profit; per-trade limit check with how many fit; unlimited loss always fails | `options/strategies.py`, `simulate.py` | `test_prices_and_the_risk_check`, `test_the_numbers_match_the_payoff`, `test_the_risk_check_uses_the_settings` |
| Adjusting | Whole trade up or down, wings wider or narrower, one leg, one listed strike at a time, refused with a reason when strikes would cross | `options/strategies.py` | `test_adjustments_*`, `test_given_strikes_adjustments_and_errors` |
| Describe the trade in plain language | Rule-based reader for ticker, strategy (named or from a view), expiration (days, weeks, months, month names, dates, this/next week, next month, earnings), delta, width, count and strikes, saying what it read and assumed; a pluggable fallback reader whose answer must be a template and is validated against the chain | `options/strategies.py`, `POST /v2/strategies/parse` | `test_strategy_words` (27), `test_settings_in_a_description`, `test_strikes_and_dates_*`, `test_tickers_*`, `test_parse_endpoint`, `test_a_fallback_reader_*` |
| Screen and flag | Build a strategy panel on the Simulate screen, loading the legs into the simulator; `strategy_builder` flag; models only, nothing recorded | `static/lab.js`, `features.py`, `api_v2.py` | `test_the_strategy_builder_can_be_switched_off`, `test_static_assets.py` |

Known limitations: four legs at most (the simulator's limit), so no broken-wing condors with
extra legs; covered calls and protective puts need shares, which the simulator does not
model; early assignment of sold options is not modelled.

### Part 4: AI plug-in, news, sentiment and the market's make-up

Migration 10 adds `ai_usage`.

| Requirement | Done | Where | Tests (`test_ai_news.py` unless noted) |
| --- | --- | --- | --- |
| AI plug-in | Anthropic Messages API through `requests` (no new dependency): key in the header only, configurable model, server-side web search with its sources, paused turns resumed, daily request limit, cache, usage table, safe error messages; POST-only routes behind the same-origin check | `ai.py`, `config.py`, `schema_v2.py`, `api_v2.py` | `test_parse_response_*`, `test_extract_json` (7), `test_a_request_carries_the_key_*`, `test_web_search_can_be_switched_off`, `test_answers_are_cached_*`, `test_a_paused_web_search_turn_*`, `test_failures_have_safe_messages_*` (8), `test_without_a_key_*`, `test_another_web_page_cannot_spend_*`, `test_migration_10_*` |
| Latest news | Finnhub company and market headlines and SEC filings, deduplicated, cached, failures reported and not cached; demo headlines in demo mode | `news.py` | `test_company_news_from_finnhub`, `test_news_failures_*` (3), `test_demo_headlines_*`, `test_sec_filings_*` |
| Sentiment | Word-list tone for every headline (labelled as counting words), AI tone from -1 to +1 with reasons when set up; both labelled as the tone of headlines, not forecasts. Options-flow sentiment comes from the scanner's put/call figures (part 2) | `news.py`, `briefs.py` | `test_word_tone`, `test_headline_tone_by_ai` |
| Market make-up | Indexes, VIX or realized volatility, regime, sector funds against SPY, breadth over 100 large companies or the watchlist (unknowns left out), SPY and QQQ put/call | `makeup.py` | `test_changes_over_each_horizon`, `test_breadth_counts_*`, `test_market_makeup_endpoint` |
| Briefs | Market and stock briefs from the app's figures and headlines, with web search, sources, usage and the exact prompt shown; nothing about the account is sent; malformed replies shown as written | `briefs.py`, `static/lab.js` | `test_market_brief_sends_figures_but_nothing_about_the_account`, `test_stock_brief_and_a_reply_that_is_not_json` |
| Plain-language trades | The strategy builder's fallback reader: AI answers must be a template, a ticker and in range, then are built and checked like any other | `briefs.py`, `options/strategies.py` | `test_the_strategy_reader_*`, `test_strategy_reader_refuses_*` |
| Screens, health, flags | Market & news screen, News panel on Analyze, AI and news on Data health; `market_news` and `ai_assistant` flags; keys never in any reply | `static/lab.js`, `static/v2.js`, `api_v2.py`, `features.py` | `test_health_shows_ai_and_news_without_keys`, `test_ai_routes_without_a_key_*`, `test_market_news_can_be_switched_off`, `test_settings_read_the_ai_and_news_keys`, `test_static_assets.py` |

Not verified here: live calls to the Anthropic API and Finnhub. This workspace cannot reach
either, so every test uses recorded-shape fake replies; the request format follows Anthropic's
published Messages API (version 2023-06-01, web search tool `web_search_20250305`). The first
real use should be checked on the screen (the brief shows its sources, usage and model), and
`AI_MODEL` changed if the account uses another model name.

Known limitations: tone by word list is crude by design; the AI reads headlines, not
articles (web search reads pages); Finnhub's free tier limits requests and its company news
covers US listings best; breadth covers 100 large companies, not the whole market.

### Independent review of 2.4 and fixes

Two reviewers that had not seen the code being written checked 2.4 before release: one for the
financial calculations (the options lab, Monte Carlo, strategy templates and the trade reader,
the scanner and the make-up), one for security and integration (keys, prompts, cross-site
requests, the UI, reply shapes, limits). Every finding was reproduced by the reviewer with a
script, then fixed with a regression test in `tests/test_2_4_review.py` that fails on the code
before the fix. The reviewers' scripts were run again on the fixed code.

| # | Finding | Severity | Fix |
| --- | --- | --- | --- |
| S1 | The test client's host name `testserver` was accepted in production, so a page on a hostile local network that could claim that name passed the host and origin checks (and could spend the AI budget) | High | Production hosts are 127.0.0.1, localhost and ::1 only; the test suite adds `testserver` through `api.TEST_HOSTS` |
| H1 | "sell" or "short" not directly before the structure built the opposite trade ("sell AAPL put spread" became a debit spread, "short the AAPL straddle" a long straddle) | High | The reader finds the structure, then whether it is bought or sold from words anywhere in the sentence ("short-term" excepted). Sold single options, sold butterflies and calendars, reverse condors and sentences with both buy and sell and no structure are refused with the reason, never flipped |
| H2 | "at 30 delta" or "near 30 dte" was read as a strike of 30, building a deep in-the-money option | High | Numbers followed by delta, days, weeks, months, wide, points, dollars or % are not prices; the builder refuses a middle strike outside half to one and a half times the stock price |
| H3 | The second strike of "25/30 spread" was read as 30 contracts; "5 point wide" as 5 contracts | High | A number after a slash or before a width word is not a count |
| H4 | The Monte Carlo's earnings jump was timed in whole days from midnight while the option's time runs to 4 pm, so a report today (or late on a day) dropped the jump while its variance was still taken out of the volatility: the option was valued about 28% low | High | The jump is timed on the option's own clock (the report day's close); variance is removed only when the jump is added, and the result says why when it is not (report passed, on or after expiration, not measurable) |
| S2 | Numbers such as `1e999`, `Infinity` or a list where a word belongs in the model's JSON caused a server error, and the bad answer was cached, so the error repeated for up to 24 hours | Medium | Every number from the model is checked for type, finiteness and range; an answer is cached only when it can be used |
| S3 | A Messages API reply in an unexpected shape caused a server error, and the call was never counted against the daily limit | Medium | Replies of any shape are read without an error; every call is counted, failed or not |
| S4 | One Finnhub row with a number for a headline or an out-of-range time stopped all news and the stock brief | Medium | Rows that are not as documented are skipped; times in milliseconds are read; only http(s) links are kept |
| S5 | Requests made at the same moment could all pass the daily AI limit (10 of 10 with a limit of 2) | Medium | Each call is counted before it is made, under a lock |
| S6, M4 | Breadth reported 0 new highs and lows when no stock had a year of history (and sent that to the AI); options activity showed unknown put volume and open interest as 0 | Medium | Measures no stock could give are unknown (n/a on screen, "unknown" in prompts); calls and puts keep their own known flags; a contract without open interest is not called unusual |
| S7 | Another web page could set off costly reads through the browser (an `<img>` pointing at a 30-stock options scan or the make-up refresh) and use up the data and news quotas | Medium | API reads marked by the browser as coming from another site (`Sec-Fetch-Site: cross-site` or `same-site`) are refused; news is cached for five fixed windows |
| M1 | A price given for a credit spread, strangle or condor was silently ignored | Medium | It is reported as not used, with the delta the strategy uses |
| M2 | Small strike pairs were taken as dates ("10/12" as Oct 12) | Medium | A slash pair is a date only when strikes are written elsewhere, it is today or later, and preferably after "exp", "for", "on" or "by"; three-number groups are dates only with a year |
| M3 | "Next week" from a weekend skipped a week; "Oct" after October's monthly built a one-day trade; "25Δ" was not read; a lone strike ("230 calls") was dropped | Medium | Fixed; "next/this Friday" read; the understood line shows the date chosen |
| M5 | Moving a butterfly up or down across uneven strike spacing unbalanced its wings without saying so | Medium | Moved butterflies keep even wings (wider if the chain has no strike at the old distance); uneven ones are noted |
| S8, S9 | A web-search turn that kept pausing cost four calls counted as one and returned partial text; an unreadable tone answer was cached for six hours | Low | Each call counts; a turn still paused after three calls is an error; unreadable answers are not cached |
| L1 | A Monte Carlo over more than a year checked every other day but said "daily" | Low | The result says how often paths were checked |
| L2 | The daily-check touch formula is inaccurate within a step or two of the strike | Low | Not shown there |
| L3 | The buyer's outcomes are at expiration while the model value is today's | Low | Said in the note |
| L4 | 30-day implied volatility silently used the nearest expiration when none was on the other side of 30 days | Low | The screen says which expiration it came from |
| L5 | A stock with one bar, or a missing last close, counted as unchanged in breadth | Low | Counted as unreadable |
| S10 | Absurdly deep JSON raised an uncaught error; links such as `httpx:` were accepted | Low | Caught; only http(s) links |

Checked and found sound by the reviewers: neither key appears in any reply, log, the database or
`repr(Settings)`, including when upstream errors quote them; no account data (account size,
positions, theses, notes, journal) appears in any prompt; the write guard refuses other origins,
`null`, other ports and method overrides; the UI never uses innerHTML and opens links with
`noopener`; option prices, touch odds, the antithetic standard error, the jump's martingale
correction, all 16 templates' legs, sides, ratios and payoffs, the implied earnings move, iv30,
realized volatility, the expected move, skew, premium and put/call arithmetic.

Version set to 2.4.0.

## 2.5: one decision path

Requested in a product review:

- one path from which way to lean, to which stock, to shares or an option, to what it might
  make or lose, to the limit to type at the broker;
- one thesis carried across the screens;
- a comparison with an honest expected value;
- a Today screen;
- a grouped menu.

The review's diagnosis was checked against the code first. All nine points held. Two
corrections: the reversal scan lived on Analyze, not the Scanner; and Market & news was
missing from the proposed menu groups, so it went under Today.

The lean rule needed a choice. Capping a lean whenever the reversal reading was "none" on the
same side would have emptied the long list. Rule (b) was chosen: hold back a lean only when
the *opposite* reversal is under way.

Built in four parts, each merged into `main` separately: #20, #21, #22, then part 4 with the
review fixes. The rules were kept throughout:

- nothing places an order or reads the broker;
- unknown is never shown as zero;
- a score is never called a chance;
- the three chances are never blended;
- no option backtests;
- no share legs in the simulator.

### Part 1: one thesis form and Set the limit (#20)

- `static/flow.js`: pure mappings, run under Node in `tests/test_flow.py` and checked
  against the server's request models.
  - `pricingRequest`: contract card to `/api/price` body.
  - `thesis`: blanks stay null.
  - `gradeRequest`.
  - `optionsRequest`: on the grade's levels.
- Analyze: one form replaced the separate grade and option-picker forms.
- Price an option: gained `loadContract`, and Set the limit went on every contract card.

### Part 2: Today and the grouped menu (#21)

- `today.py` and `GET /v2/today/candidates`: the lean rule above, with lists of 8 sorted by
  |net score|.
- Unknown scores are "not measured", never a lean and never "no lean".
- A new `FEATURE_TODAY` flag.
- The grouped menu, with Today as the landing screen.
- All scanner tabs open Decide, and the reversal scan became a Scanner tab.

### Part 3: shares or a contract (#22)

- `simulate.implied_expectation` and `market_odds.expected_pnl`: the expectation over the
  whole distribution, with the same weights.
- New response fields, listed in [V2.md](V2.md#today-and-decide-25).
- The comparison panel and chart.
- `history_built` on the grade, so a missing frequency reads "not built".

### Part 4: the plan on the chart

The trend chart on Decide runs on to the horizon date with the targets, the invalidation, the
one-standard-deviation range option prices imply (widening with time) and the contract's
breakeven.

### Independent review of 2.5 and fixes

Two reviewers checked the release before it shipped: one for the calculations and the
product rules, one for the screens, in a real browser with every flag combination. Neither
had seen the code being written.

| # | Finding | Severity | Fix |
| --- | --- | --- | --- |
| M1 | The contract's expected figure used the at-the-money volatility for the stock and each strike's own volatility for the option. With a normal equity skew that alone showed out-of-the-money calls worth up to 40% more than their price and puts up to 64% less, beside a note saying the gap was a few percent | High | Each contract's expected figure and horizon chance are read at its own implied volatility, the one its price implies. Gaps are now about 1% (carry and the smile's fit), and the test bound went from 25% of the premium to 3% |
| F1 | Opening Decide while an earlier analysis was loading could leave the earlier stock on screen, or use up the new stock's hint so it was not graded. A button could be left reading "Working…" | High | Analyze, the Today lists and Price an option draw only their newest response. The hint belongs to one ticker. `withBusy` keeps the button's label when calls overlap |
| M2 | Shares' expected figure counted from a typed entry while the distribution starts at today's price, so an entry below the market showed as an expected gain | Medium | Counted from today's price, said on the row when the entry differs |
| F2 | A second grade that failed left the first plan on the chart and the first decision in memory | Medium | Each run clears the comparison, the chart plan and the decision first |
| F3, M10 | Opening Decide in the other direction kept the typed levels and graded them on the wrong side (a 400 error) | Medium | Turning a thesis round drops its prices and keeps days, contracts and budget |
| F4 | Set the limit carried an earnings date typed for one ticker to another, which raised a false earnings flag | Medium | Cleared when the ticker changes. The last ladder is also cleared, and a stop for any reason says why |
| M3 | Past cases on Decide used the same-regime figure with as few as one case, without the "Few" tag or range. They read "not built" when only this stock's signals failed | Medium | Same-regime figure only with enough cases, shown with its range and tag. `history_built` is read from the history itself and is `null` when switched off |
| M4 | The shares row showed the chance of passing target 1 and the contract row the chance of a profit, so the two did not compare | Medium-low | Both show the chance of a profit on the horizon date. Shares add the target-1 chance after it (`market.stock_profit_at_horizon` is new) |
| F5, M7 | With the trend engine off the app still landed on Today, which could not load (its spinner never stopped) | Medium-low | `data-feature` takes several flags and Today needs both. A failed list replaces the spinner with the error |
| F6 | Switching the Today stock menu quickly could leave the other universe's list on screen | Low | The newest request wins |
| F7 | The labels above the comparison chart overlapped | Low | Labels go on a second row when they would touch |
| F8, M7b | With the Scanner off, the reversal scan was gone (it moved to a Scanner tab) | Low | It stays on Analyze when the Scanner is off |
| M5 | The market block used two different times to the horizon | Low | One time and one forward for every horizon figure |
| M6 | `/v2/today/candidates` accepted any tickers | Low | Watchlist or the 100 large names only |
| M8 | One stock without a finished bar made the whole Today list fail | Low | That stock is listed under "Not read" |
| M9 | The plan on the chart could land a day early before the open | Low | The horizon is counted in weekdays from the last bar drawn |
| M11 | The hostile-regime cap was a hard-coded 25 | Note | Derived as the two lowest rows of each fit table. A lean kept without its reversal reading says so on its row |
| F10 | With the trend engine and the options lab both off, a scanner row raised a script error (older than 2.5) | Low | A no-op |

The reviewers checked these and found them sound:

- Advisory only: Set the limit fills and prices a form, nothing more. "Your limits" reads the
  app's own positions, not the broker's.
- Scores are never called chances.
- The expected figure matches closed-form Black-76:
  - at expiration, within 3e-5 per share;
  - before it, within 3e-7;
  - across carry from −2% to +3%, volatility from 15% to 80%, and in- and out-of-the-money
    calls and puts.
- The one-standard-deviation range holds 15.8% of the distribution on each side.
- The expected price equals the forward.
- Responses only gained fields.
- The lean rule uses the last finished bar's regime and reversal.
- Every screen opens without a script error with each flag off.
- Keyboard access works on rows and on the link inside a row.
- The new charts have `role="img"` and labels.
- No missing value is shown as $0 or 0%.

Tests: 767 passed, 2 skipped, including the Chromium tests; ruff is clean.

### 2.5.1: six review fixes

| # | Finding | Fix | Test |
| --- | --- | --- | --- |
| 1 | `reach_target_1_by_horizon` and `beyond_invalidation_at_horizon` used the stock price as the forward, while the range, expected price and share chance used the forward | `stock_outlook` computes the forward once and every stock figure uses it | `test_2_5_1.py`: a forward 10% under the stock with a target between them |
| 2 | Share chance and average were measured from today's price while the share dollars used the entry | The typed entry goes to the picker as `entry_price`; only the share-ahead chance uses it; the "if bought" wording is gone | the chance moves with a typed entry, nothing else does; the contracts are unchanged |
| 3 | The expectation column read "about $0" by construction | The 10th, 50th and 90th percentile P&L per contract and stock percentiles in `market`; the column shows the middle outcome and the nine-in-ten band; the average stays in the footnote | finite band with a volatility, none without; puts and calls ordered correctly |
| 4 | The cone did not say which expiration's volatility it used | The legend and hover name `atm_expiration` | browser test |
| 5 | Today rows had no past frequency | `past_10d` from the stored 10-day buckets, read once per response; a column only when the history is built | the bucket matches `bucket_for`, a short is not flipped, an empty bucket is null |
| 6 | The unused call-or-put form and a repeated past-cases sentence | `optionsSection` deleted; the sentence is on the comparison only | static test |

Tests: 777 passed, 2 skipped; ruff clean.

## 2.6: your trade history

Requested: pull the Robinhood portfolio; analyse past trades; find what went well and what
went badly; a time-based analysis by strategy, with the risk each carried; dates you can pick
and verify.

Checked first: what Robinhood offers.

- **Account activity report.** It has every trade, options included, with fees, as a CSV, and
  takes 2 to 24 hours to generate. This became the primary source.
- **The Robinhood connection.** Public tool lists name order and position reads, which may be
  equity only, and also order placement. This became an opt-in second source, off by default,
  with its own exact-name guard.

The rules were kept throughout:

- nothing places an order;
- account reads only when switched on;
- nothing about the account goes to the AI;
- unknown is never zero;
- patterns are counts, not forecasts;
- every figure opens to its statement rows.

Built in three parts, each merged separately, then the review fixes:

- **Part 1 (#25):** `tradehistory.py` and `tradeanalysis.py`, with the parser, storage,
  rebuild, strategies, risk and analysis. It added migration 11, the `/v2/history` routes and
  the `trade_history` flag.
- **Part 2 (#26):** the My trades screen (`static/trades.js`): the range bar, period bars,
  the running total, strategies and risk, open risk over time, patterns, trades with their
  statement rows, and holdings.
- **Part 3 (#27):** `robinhood_account.py`, the connection reads behind
  `ROBINHOOD_ACCOUNT_READ`, and reply shapes in the check command.
- **Review fixes (#28),** listed below.

### Independent review of 2.6 and fixes

Two reviewers checked 2.6 before release without having seen it written. One covered the
arithmetic and the trade rebuilding. The other covered security, robustness and the screen.

| # | Finding | Fix | Test |
| --- | --- | --- | --- |
| A1 | A leg filled in two rows was read as two legs, so a debit spread could be named "Multi-leg (other)" with no risk | `merged_legs` merges by contract and side before naming | `test_a_leg_filled_in_several_rows_is_one_leg...` |
| A2 | Connection rows were dropped on every day up to the report's last day, losing trades before the report started | Dropped only on days some report covers | `test_the_report_wins_only_on_the_days_it_covers...` |
| A3 | Removing one of two overlapping imports deleted the rows both had | Migration 12 links each row to every import that had it; removal keeps shared rows | `test_removing_an_import_keeps_the_rows...` |
| A4 | An assignment closed the option as one trade and opened the delivered shares as another, so the short put's outcome was split | The same day's delivered shares join the option's trade | `test_options_expire_assign_and_close...` |
| A5 | Peak exposure ranked days by measured risk only, so a day with only unlimited risk open showed $0. Exposure did not shrink with partial closes | Exposure follows the open fraction. The peak ranks by risk, then unlimited, then unmeasured, and is `null` when nothing was open | `test_open_risk_shrinks...`, `test_a_day_with_only_unlimited_risk...` |
| A6 | An option past expiration with no closing row stayed "open" forever and counted in open risk | `UNRESOLVED`: result unknown, risk ends at expiration, a note says so | `test_an_option_past_expiration...` |
| A7 | With dates meaning "opened", results still fell in the period they closed | Periods keyed by open date | `test_a_date_range_picks_trades...` |
| A8 | A reverse split (negative quantity) was refused | Applied as a signed change in shares | `test_a_reverse_split...` |
| A9 | A same-day close listed before its open was dropped as unmatched | Retried after that day's opens | `test_a_close_listed_before_its_open...` |
| A10 | Two-kind features were listed twice, as mirror images ("after a loss" better, "not after a loss" worse) | Listed once, on the side of the larger effect | `test_patterns_compare_each_kind...` |
| A11 | Proceeds below the fees (a negative amount on a sale) fell back to an estimate | The signed amount is used | `test_proceeds_smaller_than_the_fees...` |
| S1 | Two requests could rebuild at once and one could read a half-built history (an empty analysis) | One rebuild at a time, set in one assignment | `test_the_analysis_is_never_read_half_built...` |
| S2 | "After a loss" was found by scanning every earlier trade, which is quadratic | Binary search | the existing pattern tests |
| S3 | A malformed CSV raised a 500 | `csv.Error` becomes a 400 with what to do | `test_a_malformed_file...` |
| S4 | A file name over 200 characters was refused, and an amount of 400 digits became infinity | The name is cut to 200 characters; an amount over 15 digits makes its row unreadable | `test_a_malformed_file...`, `test_numbers_that_cannot_be_real...` |
| S5 | Each load of the screen read the connection's holdings and tools live | Cached for 5 minutes; a pull refreshes them | (behaviour) |
| S6 | The account-name guard allowed any `get_` name without action words, so `get_portfolio` (balances) was on the list | Only `get_(equity\|stock\|option)_(positions\|orders)`; `get_portfolio` dropped | `test_the_account_list_takes_plain_reads_only` |
| S7 | An order with no state was taken as filled at its ordered quantity and limit price. A cancelled order with fills was dropped, and a partly filled one was kept at its first read | Only filled quantity counts, whatever the state. No state and no fills is reported. A fill that grew updates its row | `test_only_what_filled_counts...`, `test_an_order_that_filled_further...` |
| S8 | Reply shapes kept keys that are values (an account number used as a key) and list counts | Such keys replaced, counts dropped | `test_the_check_records_reply_shapes_never_values` |
| S9 | The check command listed account reads it had called as `may_be_called: false` | Marked true when they are on the account list and permitted | `test_the_check_lists_the_account_reads...` |
| S10 | "All history" filled the date boxes with the first and last dates, so a later, earlier-reaching import was cut off | All history sends no dates and leaves the boxes empty | browser test |
| S11 | The screen showed "$0" as the peak when only unlimited risk was open | "unlimited", "not measured" or "nothing open" | (screen; A5 covers the data) |
| S12 | Unreadable connection rows showed "Line undefined" | Shown with what they were, why, and their field names | (screen) |
| S13 | The link status was written after an await without checking it was still the newest request | Fetched with the rest and checked once | (screen) |
| S14 | The screen said the file was "never sent anywhere", but tickers go to the market-data source for prices | It says only tickers leave the computer, and why | (screen) |

Also pinned by tests:

- with account reads off, no account tool is called;
- with them on, order tools are still never placed or reviewed;
- a listed read marked destructive is refused;
- FIFO results come from the Amount column, fees included;
- each strategy has its risk basis;
- a trade's statement rows come back exactly as written in the report.

Tests: 827 passed, 2 skipped, including the Chromium tests; ruff is clean.
