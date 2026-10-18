# Reliability, accounting, risk and analytics improvements (2.1.1 to 2.8)

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

## 2.7: trade ideas and a simulation of one trade

Requested: scan the top 200 stocks, show the best trades to buy and to short, and take any of them into a
simulation that shows the money it can make.

Read as follows:

- **Top 200:** the 200 largest US companies, as a fixed list.
- **Best trades:** the highest-graded trades on the side the trend evidence points, with what each makes
  and loses at its levels.
- **Simulate:** a model of the trade's own exits on paths at the market-implied volatility, with shares
  and with a contract.

The rules were kept throughout:

- no order path;
- nothing about the account read or sent;
- a grade and a score are not chances;
- the simulation is labelled a model with no view on direction, whose average is about break-even;
- unknown is never zero.

Built in one part (#30), then the review fixes:

- `scanner.TOP_200`: the 100 large caps plus 100 more, each mapped to a sector fund.
- `today.read_stock`: Today's per-stock reading, now shared with the scan.
- `ideas.py`: the background scan, ranking and arithmetic.
- `tradesim.py`: the simulation.
- `/v2/ideas`, `/v2/ideas/scan`, `/v2/ideas/stop`, `/v2/ideas/simulate`, behind `FEATURE_TRADE_IDEAS`.
- The Trade ideas screen (`static/ideas.js`).

### Independent review of 2.7 and fixes

Two reviewers checked the release before it shipped, without having seen it written. One covered the
arithmetic, statistics and model; the other covered robustness, concurrency, security and the screen in a
real browser.

| # | Finding | Severity | Fix | Test |
| --- | --- | --- | --- | --- |
| H1 | `ideas.js` declared `renderSimulation`, the name of the simulator's own renderer in `app.js`, so Simulate a trade (and Analyze's reversal options) drew nothing | High | Renamed `renderTradeSim` | `test_no_name_is_declared_at_the_top_of_two_scripts`, `test_simulate_a_trade_still_draws_after_trade_ideas_loads` |
| F1 | The contract was valued at its own volatility on paths walked at the at-the-money one, so the skew showed as an average profit on calls (+8%) and loss on puts (−12%) | High | The contract walks on the same random numbers at its own volatility and carry | `test_a_contract_off_the_money_is_not_a_profit_or_loss_by_its_skew_alone` |
| F2 | A day where both the stop and a target could be touched was given to the stop, and independent draws overstated double touches; with tight levels the target came first 42% of the time against 50% | Medium | Such a day is walked again in 32 bridge steps to find which came first | `test_a_day_where_both_levels_could_be_touched_is_walked_again...` |
| F3 | A short's fallback target 2 could be below zero (CVX: −$3.34), and its Simulate button failed | Medium | Suggested short targets never go below a twentieth of the entry (evaluator 1.1); a plan checks only the levels it uses | `test_a_short_s_suggested_targets_never_fall_to_zero`, `test_levels_on_the_wrong_side_are_refused` |
| F4 | A trade too large for one share simulated as $0 with a "0%" chance | Medium | One share, with a note; zero shares are refused | `test_with_no_whole_share_inside_the_limit_one_share_is_simulated_and_said` |
| F5, M4 | A chosen contract expiring before the horizon was valued as alive afterwards (+80% on cost) | Medium | Refused with the reason; its other problems become notes | `test_simulation_requests_that_cannot_be_right_are_refused` |
| F6 | "Simulate the contract alone" passed a model price for an entry away from the stock | Medium | Passes today's midpoint | browser test |
| M1 | Opening the screen started a 200-stock scan after every restart, with no way to stop it | Medium | No automatic scan; Stop button; the last result is saved | `test_a_scan_can_be_stopped_and_the_last_result_survives_a_restart`, browser test |
| M2 | Live data: two history requests per stock (3 then 5 years) and no pacing; Tradier's rate-limit wait held the provider lock | Medium | Five years fetched once; the scan's requests spaced at about half the allowance (`Market.paced`); the wait happens outside the lock | `test_a_background_job_spaces_its_own_requests_only` |
| M3 | One unexpected exception discarded the whole scan, and its raw text reached the screen | Medium | Each stock is caught and listed with the kind of error; job errors are redacted | `test_one_stock_that_fails_unexpectedly_does_not_stop_the_scan` |
| F7 | Contracts were valued at the close of the exit day, losing extra time decay | Low | Valued at the middle of the day a level is touched | `test_the_contract_is_sold_on_the_same_exits_at_its_own_value` |
| F8 | The split plan sold half a contract or half a share | Low | Whole units, the odd one at target 1; one unit is sold at target 1, said | `test_the_split_plan_sells_whole_units...` |
| F9 | The carry mixed two spot quotes and a different time floor | Low | Read against the evaluator's own spot and time | (review check) |
| F10 | When the levels failed the options check, the note blamed missing option prices | Low | Says the real reason; the chain's at-the-money volatility is still used | `test_without_option_prices_the_stock_s_own_volatility_is_used_and_said` |
| F11 | Realized volatility (per trading year) stepped in calendar time | Low | Steps in trading days in that case | (review check) |
| F12 | The share average is the carry, not zero | Low | Shown as "the carry, not an edge", with its error | (screen) |
| F13 | Paths out at target 1 and then stopped were drawn as "neither"; "middle path" was a daily median | Low | Legend and colours per exit; "median price each day" | (screen) |
| F14 | The contract count used the midpoint and the cost note the repriced premium | Low | Both use the repriced premium; the note counts the contracts | (screen) |
| F15 | Earnings inside the horizon used calendar days in the simulation and trading days in the list | Low | One rule, trading days | (code) |
| F16 | A stock read but not graded was counted as read and listed as not read | Low | Listed apart as "read but not graded" | `test_one_stock_that_fails_unexpectedly...` |
| F17 | Size was not capped by the account | Low | Never more than the account, said | (code) |
| L1 | Reading all stocks before grading any outran the 60-second cache, so trends were built twice | Low | Read and grade each stock in one pass | (timing) |
| L2 | Polling continued after leaving the screen, carried the whole result each time, and stopped for good on one error | Low | Brief polls, stopped when hidden, retried with backoff | browser test |
| L3 | Enter on a row's Simulate button ran two simulations; the progress bar had no name and flooded screen readers | Low | The button keeps its Enter; the bar is labelled and only the finish is announced | (screen) |
| L4 | No range checks on prices; target 2 on the wrong side accepted; a put for a long accepted | Low | Levels between a twentieth and twenty times the entry; target 2 beyond target 1; a long takes a call, a short a put | `test_simulation_requests_that_cannot_be_right_are_refused` |
| L5 | The Stocks select did not follow the scan shown | Low | It does; a second start says one is already running | `test_the_scan_runs_in_the_background_one_at_a_time` |
| L6 | "Best trades" and a bold green "At target 1" read as a forecast | Low | "Highest-graded", "If target 1 is reached", not bold | browser test |
| L7 | The flag test expected Find to vanish with the Scanner off | Low | Checks the Scanner's own button | browser test |
| N1 | The chain cache was evicted without a lock | Note | Locked | (code) |
| N2 | Unused names, `__version__`, ties decided by rounding noise, the Decide handoff's contract count | Note | Fixed | (code) |
| N3 | Two freshness tests failed during market hours (demo quotes read DELAYED), on main as well | Note | They accept DELAYED for a source that is not real-time | `test_freshness.py` |

Noted and left as they are:

- **The lean and the grade during market hours.** The lean reads the last finished bar; the grade reads
  today's provisional bar when there is one, as on Decide.
- **The time grid.** Run before the close, the first step covers the rest of today. Market holidays count
  as trading days, as elsewhere in the app.
- **Charts on a phone.** At 390 px the charts scroll sideways, the app's convention for wide charts.
- **The keyboard.** Space does not open a table row; the shared table helper answers Enter only.

The reviewers checked these and found them sound:

- the Brownian-bridge formula for log prices with drift over uneven steps;
- the closed form for touching one level before another;
- the shared draw for the two targets;
- the split plan's logic and labels;
- the time grid;
- the side following Today's rule for every row checked;
- entries equal to Decide's price;
- the dollar arithmetic for shorts;
- determinism;
- one scan at a time, with the result swapped under the lock;
- shutdown mid-scan;
- flags off;
- dark mode;
- no page-level horizontal scroll at 390 px;
- every handoff carrying the same contract or levels.

Tests: 855 passed, 2 skipped, including the Chromium tests; ruff is clean.


## 2.8: the Morning plan

Requested: in the morning, find the stocks that made really big jumps, judge overextended against
unfavorably sold, and buy the stock or the option to ride the wave — with the price really close to break
even, paying a little more premium for how far it can go; watch stocks for a sign of reversal and jump on
it; a very simple workflow; and the whole thing by email at 7:00 before the market opens.

Read as follows, with the defaults stated on the screen:

- **Big jumps:** at least 3% and two of the stock's own daily ranges, across about 290 large optionable
  US stocks plus the watchlist (movers cannot be fetched from any connected source, so they are computed).
- **Ride or fade:** a move **with fresh news** is read to keep going; a move **with no news found**,
  already overextended **before** it, is read to snap back — the split the drift/reversal evidence
  supports (Chan 2003; Savor 2012; Lehmann 1990; Jegadeesh 1990). News that could not be checked is
  "not checked", never "no news".
- **The price:** the limit is the midpoint capped at `P ≤ (V_target + r·V_stop)/(1 + r)` — the highest
  price where reaching target 1 still pays `r` (default 1.0×) times what the stop loses.
- **Reversal:** the 2.1 reversal reading, with a once-a-minute price watch on the confirming level.
- **7:00:** launchd on the Mac, local time, `run.py --morning-email`; `mailer.py` over verified STARTTLS.

The rules were kept throughout: no order path; nothing about the account read or sent; frequencies are
never forecasts; unknown is never zero; at 7:00 option prices are the previous close and every card says
so.

Built in one part (migration 13, `morning.py`, `morning_email.py`, `mailer.py`, `background.py` — the job
slot shared with Trade ideas — `setup_morning.sh`, `static/morning.js`, `/v2/morning...` behind
`FEATURE_MORNING`), then the review fixes.

### Independent review of 2.8 and fixes

Two reviewers checked the branch before it shipped, without having seen it written. One covered the
finance, arithmetic and statistics; the other covered robustness, concurrency, security and the screen.
The first re-verified the limit-cap formula on 20,000 random cases and the past record's Wilson interval.

| # | Finding | Severity | Fix | Test |
| --- | --- | --- | --- | --- |
| M1 | An SEC date-only `filingDate` compared against an aware cutoff raised `TypeError`, which sank the whole plan in the live+SEC configuration | High | `_fresh()` reads date-only values by New York date, gives naive times a zone, and treats unreadable as not fresh; each mover and reversal card is built in its own `try/except`, so one stock never stops the morning | `test_unknown_news_is_never_read_as_no_news`; plan-level containment in the API test |
| M2 | `starttls()` used the default context, which does not verify the server's certificate — the SMTP password could have gone to whoever answered on a hostile network | High | `ssl.create_default_context()` | the fake SMTP server asserts a verifying context |
| M3 | `date.today()` on a UTC machine dated an evening plan tomorrow (plan day, demo news, record cache, scorecard and watch windows) | High | New York dates everywhere: `datetime.now(NY).date()` | screenshots at 23:07 New York; existing date tests |
| M4 | The past record required overextension at the post-move close while the live rule read the bar before the move, so the record described a different pattern than the one traded | High | both condition on the bar before the move | `test_the_past_record_counts_what_followed_the_same_pattern` |
| M5 | The email run skipped whenever the clock said "closed" — true every evening and before 7:00 in New York, so a 7:00 east of New York would never have sent; a holiday was indistinguishable | High | only weekends skip (New York calendar); a weekday holiday sends a plan that says the market is closed | `test_the_morning_email_run_skips_weekends` |
| M6 | A failed send still recorded the picks, and `--morning-print` recorded them too, so the scorecard graded plans nobody saw | Medium | picks recorded only after `mailer.send` returns; a dry run records nothing; the screen's send route the same | `test_a_dry_run_records_nothing_and_a_send_records_only_after_it_works` |
| M7 | The screen could email yesterday's plan under today's subject, and the subject was dated from the clock rather than the plan | Medium | `/v2/morning/email` refuses a plan whose day is not today; the subject takes the plan's own day | `test_the_screen_refuses_to_email_a_stale_plan_and_caps_the_watches` |
| M8 | A watch added between the checker's empty look and its thread ending was orphaned until the next add; the first check also waited a minute | Medium | the checker checks at once, and before ending re-checks under its own lock | `test_a_watch_triggers_once_marks_itself_and_survives_no_email` |
| M9 | No cap on active watches, and the watch quotes went out in one call however many there were | Medium | at most 40 active (the route says why); quotes chunked at 50 | the same API test |
| M10 | `MailError` in the trigger alert was logged as "an unexpected MailError", hiding the (already scrubbed) guidance | Low | logged by its own message | code read |
| M11 | The job progress bar could pass 100% (`done` exceeded `total` by the chunk remainder) | Low | `done` clamped to `total` in `OneJob` | code read |
| M12 | A suggested limit below the bid looked immediate but rests until the price comes to it | Low | the card says so | browser test renders the card |
| M13 | "Watch the stop" described the plan by its horizon date where its own day was meant | Low | the watch's reason names the plan's day | code read |
| M14 | Two different non-viable cases shared one message (no price pays at all, and a cap under a cent) | Low | split into their own sentences | `test_the_card_caps_the_limit_and_falls_back_to_shares` |
| M15 | Zero shares (entry above the budget) dropped silently from the email | Low | a warning on the card, carried into the email's Caution line | `test_without_a_chain_the_shares_carry_the_plan` |
| M16 | No ceiling on contracts: a cheap option could suggest dozens | Low | at most 10 | `test_the_card_caps_the_limit_and_falls_back_to_shares` |
| M17 | `setup_morning.sh`: the Linux cron hint broke on paths with spaces; a path with `&`, `<`, `>` or quotes broke the plist XML silently; a 7:45 job from an earlier `--also-45` lingered | Low | the hint is quoted; hostile paths are refused with the reason; the stale job is removed | `bash -n`; manual run |
| M18 | The scorecard said "days" where it counts closes, the pick day's close first | Low | wording fixed on the screen, in the email and in the docstring | `test_picks_are_recorded_once_and_graded_by_the_stock_plan` |
| M19 | A plan-build failure in the email run printed the raw exception, which could carry a URL with a token | Low | printed through `redact()`, truncated | code read |
| M20 | The progress total double-counted the quote pass | Low | one tick per name plus a fixed tail | API test reaches "done" |

The reviewers checked these and found them sound: the limit-cap algebra and its tick rounding; the payoff
inequality at and below the cap; the Wilson interval; the ATR-ranges move measure and the premarket/stale
split; the fade's IV settle applied to the scenario values; the one-shot trigger claim
(`UPDATE … WHERE active = 1`); the kept-result restart path shared with Trade ideas; the flag off; the
email's escaping of whatever lands in the plan; and that no route places an order.

Tests: 883 passed, 2 skipped, including the Chromium tests; ruff is clean.

## 2.9.1: the review of 2.9.0

Eleven cases from the review of 2.9.0, each reproduced on `9da3402` before anything was changed.
Six are in the trade history's rebuild of your trades, two in what a position's own accounts say,
and three in the plan, the quote and the chain. Every case is now a test in
`tests/test_review_2_9.py`, and ten of the eleven fail on the commit before the fixes (the
eleventh pins behaviour the review asked to keep).

| # | Finding on `9da3402` | Severity | Fix | Test |
| --- | --- | --- | --- | --- |
| R1 | A sale the report marks short never opened a lot: `act()` sent every `Sell` to `close(+1, …)`, so the sale was listed as unmatched and the buy that covered it opened a **phantom long**, with the short's profit lost entirely | High | a marked `Sell` with nothing held opens a short lot; the covering `Buy` closes it, and its result is `(sold − covered) × quantity` under **Shares (short)** | `test_a_sale_marked_short_and_the_buy_that_covers_it_are_one_trade` |
| R2 | — (checked, not a defect) an unmarked sale with nothing open must stay unmatched rather than be guessed into a short | — | the short lot opens only on the report's own mark | `test_a_sell_with_nothing_open_and_no_short_mark_is_still_unmatched` |
| R3 | An uncovered short call assigned: the delivery was registered but only `open_lot()` consumed it, which `Sell` never reaches. The share sale fell through to a FIFO close, found nothing open, and was reported unmatched — the strike's cash never reached the option's trade | High | `delivered_sale()` consumes the delivery on the `Sell` row; with nothing held, the shares leave the trade short, and the stock cash is one of its legs | `test_an_uncovered_call_assigned_keeps_the_stock_cash_in_the_option_trade` |
| R4 | A covered call assigned closed shares **first in, first out across every lot**, so an older parcel held for something else was closed instead of the shares the call was written against | High | `close()` takes a `prefer_group`: the delivering trade's own shares go first, the rest stay in order behind them | `test_a_covered_call_assigned_delivers_its_own_shares_not_an_older_parcel` |
| R5 | A long put exercised: the same gap as R3, so the delivered sale was unmatched and the trade never closed | High | the same path; the put, the shares and the sale are one trade | `test_a_long_put_exercised_sells_its_shares_into_the_option_trade` |
| R6 | `split()` scaled only lots with `side > 0`, so a split over a short lot left the quantity and cost per share as they were and noted that it "could not be applied" | Medium | every open stock lot scales, long or short: the quantity by the factor, the cash per share against it, so the result is unchanged | `test_a_reverse_split_scales_a_short_lot_and_leaves_its_result_alone` |
| R7 | `day_pnl()` read the fills only. A borrow fee, a dividend owed or a cash adjustment dated today is cash that moved today, and none of it reached the daily loss stop | Medium | `_financing_today()` adds the recorded amounts dated today; a trade closed earlier with money dated today is no longer filtered out of the day. The borrow **estimate** is still an estimate and stays out | `test_a_borrow_fee_recorded_today_counts_against_the_daily_stop_but_an_estimate_never_does` |
| R8 | `initial_risk()` measured from `average_entry_price`, the average of every opening fill. Closing part and adding higher up rewrote the risk the trade had taken, and with it every R multiple | Medium | the ledger records `basis_at_max_quantity`, the cost of the quantity open when the position was at its largest, and the risk is measured from that. `average_entry_price` is unchanged | `test_initial_risk_uses_the_cost_basis_at_the_largest_the_position_was` |
| R9 | `desk.create()` checked a plan's levels for stock only. An option plan's levels are stock prices read by `bias_of()`, so a long put with its invalidation below the stock was accepted and `thesis_check` called it INVALIDATED straight away | Medium | `check_option_levels()` checks them against the stock now — the invalidation above it for a bought put, below for a bought call. A plan is never refused because the market could not be reached | `test_a_long_put_plan_with_its_invalidation_below_the_stock_is_refused` |
| R10 | `quote_age_seconds()` caught `ValueError` only, so a `bid_time` without an offset raised `TypeError` from `aware − naive` and took down everything that priced a contract | High | a stamp without a zone is read in the same zone as `now`; an unreadable one is skipped as before | `test_a_quote_stamp_without_a_time_zone_still_gives_an_age` |
| R11 | A locked quote (the same price on both sides) was thrown out of the smile twice over: `two_sided()` required `ask > bid`, and the smile filter read a `spread_pct` of `0.0` as missing. The tightest quote on the chain was the one dropped | Medium | `two_sided()` accepts `ask >= bid` (only a crossed quote is thrown out, as in `freshness.quote_completeness`); the smile filter tests `spread_pct is None` | `test_a_strike_quoted_at_one_price_on_both_sides_stays_in_the_smile` |

Checked and found sound: the duplicate-row key; the same-day retry that puts a close after that day's
opens; `_unit_cash()` reading the amount with its fees; the assignment and exercise delivery sides
(`buys = (PUT) == (assigned)`); `financing()`'s uncovered-nights count and its refusal to add the
estimate; and that no route places an order.

Tests: 911 passed, 2 skipped; ruff is clean.

## 2.10: the fair price and Live news

**The defect that started it.** The user reported that the option prices the app suggests are above what
Robinhood fills at. Two causes were found in `a80c8c7`.

| # | Finding on `a80c8c7` | Fix | Test |
| --- | --- | --- | --- |
| P1 | The ladder anchored on the midpoint and two of its three prices sat above it by design: `balanced` at 65% and `fast` at 80% of the way to the ask, and the walk ran to `fast`. The ticket's "Most you will pay" defaulted to `fast` | the ladder centres on `fair_value`'s anchor (the median of the usable evidence, never above the midpoint for a buyer); the walk and the ticket's cap stop there; `fast` is 30% past it | `test_plan_example_twenty_cent_spread`, `test_the_buyers_walk_never_passes_the_fair_price_or_the_midpoint` |
| P2 | Before the open the Morning card's limit started from the previous close's midpoint, made with the stock at its close. For a fade (puts after a gap up, calls after a gap down) that is well above the open | `entry_estimate` reprices the contract at the stock's price now (Black-76, its own volatility, forward moved with the stock) when it has moved 0.25% or more since the close | `test_before_the_open_a_gapped_stock_reprices_the_contract_instead_of_using_the_old_midpoint` |
| P3 | Robinhood's clock says `pre`/`post`; the price watches checked only `open`/`premarket`, so with `DATA_MODE=robinhood` watches were not checked before the open, and freshness read those hours as unknown | both accept Robinhood's words | `test_before_the_open_a_gapped_stock_...` (the `pre` case) |
| P4 | `test_a_dry_run_records_nothing_...` and `test_a_failed_plan_emails_...` failed on weekends: the email run skips weekends by the clock | the two tests fix their own weekday | both tests |

**Live news** is new (`livenews.py`, migration 14). Its tests are in `tests/test_live_news.py` (sources'
formats, `judge`, `levels_for`, the signal life cycle on demo data, holdings, email, the stream's control
frames, the key's destinations, the API and settings) and the Chromium test
`test_live_news_listens_shows_the_feed_and_turns_a_confirmed_move_into_a_card`.

Not verified against the real services, which this build environment cannot reach:

- **RTPR.** The stream, the archive and the article text are written to RTPR's published documentation.
- **The SEC current-filings feed.** Written to its published Atom format.
- **Robinhood's fill-likelihood fields.** Read only when the connection's option quotes carry them. The
  option-quote reply has still not been captured from the real server.

### The independent review of 2.10

An independent reviewer read the change, fuzzed the pricer (100,000 on-grid quotes) and reported 15
findings and 6 suspicions. Each finding was fixed, and those that can be tested are.

| # | Finding | Severity | Fix | Test |
| --- | --- | --- | --- | --- |
| V1 | A stop followed by a start could leave two loop threads running: the old one read the new stop event. That meant duplicate opportunities and emails | High | each start hands its own event to its threads; a start while the last loop is finishing is refused (409); check results are written only if the row is unchanged; each email is claimed on the row before sending | `test_a_stop_then_start_never_runs_two_loops`, `test_a_check_that_lost_a_race_writes_nothing`, `test_each_opportunity_is_emailed_once_whatever_calls_it` |
| V2 | Pre-open repricing used the close only with a parity forward. A gap over about 10% (parity rejected) on Robinhood data (the chain's stock price is the pre-market trade) was not repriced: exactly the case 2.10 is for | High | outside regular hours the reference is the confirmed close of the session the quotes belong to, whatever the forward method; the volatility is re-implied from the contract's last midpoint against that close's forward | `test_a_big_gap_on_robinhood_data_is_still_repriced` |
| V3 | With the clock unreadable during regular hours, or between 16:00 and 16:10, yesterday's close was taken as the reference and the day's move was counted twice | Medium | the close is used only when the saved history reaches the session the quotes belong to (`last_session_date`); otherwise, or with an unknown clock, nothing is repriced and the card says so (`unchecked`) | `test_without_a_confirmed_close_or_clock_nothing_is_repriced_and_it_says_so`, `test_the_session_whose_closing_quotes_are_shown` |
| V4 | No bid and a one-step offer put every rung, and the "fair" price, at the ask | Medium | said plainly (the ask is the only price; `fair.limit` is None); the property test's escape clause is gone | `test_no_bid_and_a_one_step_offer_says_the_ask_is_the_only_price` |
| V5 | An opportunity confirmed on the last news check was never checked again and stayed "active" for a week | Medium | opportunities are checked every 10 minutes for an hour from `opportunity_at` (migration 14 gains the column; it had not shipped), then end as done | `test_opportunities_have_their_own_hour_and_old_signals_end_by_age` |
| V6 | Signals carried across a closed market and could judge an overnight gap as a reaction | Medium | `_expire` ends signals by age every pass, whatever the session | same test |
| V7 | A short on a cheap stock could get a negative target | Low | a short's targets stay above a twentieth of the entry | `test_the_stop_sits_beyond_the_pre_news_price_and_targets_beyond_one_risk` |
| V8 | Holdings alerts gave the planned size, not what is still held | Low | `open_quantity` | `test_a_stock_you_hold_gets_an_alert_when_the_news_goes_against_it` |
| V9 | The card and the evaluator worked in pennies while the ladder used the chain's increments; `floor_tick` could give $2.04 in a nickel class | Low | the chain's tick rule is passed to `fair_value`, `limit_prices` and the evaluator | `test_limits_sit_on_the_chains_own_increments` |
| V10 | Decide's "fair entry" ignored the clock and suggested an entry from last session's quotes before the open | Low | no fair entry outside regular hours | the Decide picks render it only when given |
| V11 | Old items were pruned only when listening started | Low | pruned hourly in the loop | — |
| V12 | RTPR headlines were read inside the loop, so a burst held up price checks for minutes | Low | read by a two-thread pool; the item is stored and priced at once | the Chromium Live news test |
| V13 | The key would be sent over plain http to an `rtpr.io` address, and any address in a frame was read | Low | https on `rtpr.io` only | `test_the_rtpr_key_is_sent_only_to_rtpr_over_https` |
| V14 | The screen and the docstring said more than the code (the universe's volume floor, "a close through the stop", "the card stands until the stop") | Low | reworded to what the code does; **Watch the stop** added to live cards | — |
| V15 | Quotes off the class's increments could give a $0.00 rung | Low | the ask is the only price then | same as V4 |

Suspicions handled:

- **S2.** A failed refresh leaves the history short of the session. The session-date check stops that
  from repricing silently.
- **S3.** RTPR's stream and archive could store the same release twice under different ids. The release
  address is now the id for both.
- **S6.** The `websockets` debug lines carry the key. That logger is held at WARNING.

Still open:

- **S1.** Whether Tradier's quotes before the open carry pre-market trades. Noted in the README.
- **S4.** A news halt: the open-session price has no age check.
- **S5.** The loop's failed quotes count toward the source's pause.

## 2.11: news found by searching

**The request.** The user asked for news pulled in through a free Google or Brave search, as an AI client
would through a search MCP server, instead of a paid news feed.

**What was built, and why it differs from the request.**

- **No language model in the loop.** An MCP search server is a tool an AI model calls. Putting Claude in
  the app's refresh loop would spend API tokens on every refresh, which defeats the point of a $0 source.
  The app makes the same searches itself.
- **Google.** Google's Custom Search JSON API is closed to new users and ends on 1 January 2027, so the
  free Google route is the Google News search feed. It needs no key. It is unofficial, so the app spaces
  its requests and pauses after a refusal.
- **Brave.** Brave's free plan ended in February 2026. The $5 monthly credit (about 1,000 searches) needs
  a card and is billed past it, so Brave is optional, asked only as a fallback, and capped below the
  credit.
- **Latency.** A search engine indexes a story minutes after publication. Search explains moves and
  checks "no news"; it does not replace RTPR's stream for speed.

| Part | Where | Tests |
| --- | --- | --- |
| Google News feed and Brave results read from their formats; DOCTYPE and ENTITY refused anywhere; a feed that does not read is a failure; non-http links dropped | `searchnews.parse_google_news`, `parse_brave`, `_safe_xml` | `test_google_news_feed_is_read_and_anything_odd_in_it_is_refused`, `test_brave_results_are_read_with_their_times` |
| A headline counts only when it names the company the way news does | `NEWS_NAMES`, `clean_name`, `name_forms`, `name_patterns` | `test_a_headline_counts_for_a_stock_only_when_it_names_the_company` (45 cases), `test_google_results_count_only_when_recent_and_the_headline_names_the_stock` |
| Brave only as a fallback, never past its caps (any 31 days, the day, two processes), no redirects, a cap is "not asked" | `SearchNews.search`, `_brave_claim`, `brave` | `test_brave_is_a_capped_fallback_and_a_cap_is_not_a_failure`, `test_a_reserve_keeps_part_of_braves_day_for_the_morning_plan`, `test_brave_never_passes_its_limit_from_two_copies_of_the_app_at_once` |
| Google's pause and spacing shared through the database | `SearchNews.google`, `_google_turn`, `_google_refused` | `test_google_refusing_pauses_it_for_every_copy_and_an_unreadable_feed_is_a_failure`, `test_google_requests_wait_their_turn_and_a_turn_too_far_off_is_skipped` |
| Analyze's news and the Morning plan's check search too; a doubt is never "no news" | `NewsClient.company`, `Morning._news_state` | `test_the_news_client_adds_search_and_only_fresh_items_keep_the_deep_check_from_brave`, `test_the_morning_news_check_never_reads_a_doubt_as_no_news` (5 cases) |
| Live news: holdings first; the movers scan on its own thread; the day's close; one opportunity a day; duplicates | `LiveFeed._rotate`, `_search_run`, `_scan_movers`, `_day_close`, `_opportunity_today`, `_duplicate` | `test_google_news_reads_your_holdings_first_and_waits_out_a_pause_quietly`, `test_movers_get_a_search_and_news_found_becomes_a_signal`, `test_the_movers_list_is_todays_and_brave_is_asked_once_a_stock_a_day`, `test_after_the_close_the_day_is_measured_from_todays_close`, `test_one_move_is_traded_once_a_day`, `test_the_same_headline_from_two_sources_is_kept_once_but_never_two_filings`, `test_the_searches_run_on_their_own_thread` |
| The "Why is it moving?" panel | `static/live.js` `moversPanel` | Chromium: `test_live_news_lists_movers_with_the_news_search_found` |
| Settings | `config.py` | `test_settings_read_the_search_options` |

Not verified against the real services, which this build environment cannot reach: Google News's search
feed and Brave's news API are written to their published formats.

### The independent review of 2.11

An independent reviewer read the change before it was committed and ran reproductions for each finding
(46 real headlines, two processes claiming Brave searches, a padded feed, an after-hours price path). It
reported 15 findings. All were fixed.

| # | Finding | Severity | Fix | Test |
| --- | --- | --- | --- | --- |
| R1 | Demo quotes ("AAPL (demo)") left the ticker cached as the company name for 30 days, in a cache live data shares, so live AAPL was searched as "AAPL" and ON and ALL matched "On Tuesday" and "All eyes" | High | demo searches never look a name up; names are never cached on demo data; a name equal to the ticker or carrying "(demo)" is refused | `test_a_demo_quote_never_leaves_a_name_behind_for_live_data` |
| R2 | After 4 p.m. a signal's day was measured from the quote's previous close, which is yesterday's, so a stock falling on after-hours news after a strong session read as a LONG day move; the movers scan listed the session's movers as after-hours movers | High | `_day_close`: the last finished session's close (the bars when they reach it, else the quote's close after 4 p.m., else its previous close before); unknown after 4 p.m. means the price at the news stands in | `test_after_the_close_the_day_is_measured_from_todays_close` |
| R3 | Matching missed brand names (Disney, Google, Lilly, McDonald's, Citi, BofA, P&G, onsemi, BRK-B, eBay) and SEC names with "/MN" or "\DE\" suffixes, so a stock with news could read "no news" and be offered as a fade | High | `NEWS_NAMES` for the ~290 stocks followed; suffixes without a space; lower-case names in any case; ticker spellings; two-word short names; headlines that came back but did not match leave the Morning check *not ruled out* | the 45-case test; `test_the_morning_news_check_never_reads_a_doubt_as_no_news` |
| R4 | Unrelated stories counted: two-letter tickers ("UK PM", "BA cancels", "HD TV"), word names (Southern California, "Dow jumps", "student visa"), first words alone (King Charles, J.P. Morgan for MS, Harrison Ford), and the publisher's name in Google's description | Medium | headline only; one- and two-letter tickers only as `$PM` or `(NYSE: PM)`; word names need a business word next to them; risky first words never count alone | the 45-case test; `test_google_results_count_only_...` (a publisher named like the company) |
| R5 | An unreadable feed, or one with a DOCTYPE, counted as a search that found nothing, and was cached as "no news" | Medium | it is a failed search (an error, not `checked`) | `test_google_refusing_pauses_it_..._unreadable_feed_is_a_failure` |
| R6 | Two processes (the server and the 07:00 email) could both pass the Brave cap: 410 granted against 300 in the reviewer's run; Google's pause and spacing were per process | Medium | the count is read and raised in one `BEGIN IMMEDIATE` transaction; Google's next turn and its pause live in the database | `test_brave_never_passes_its_limit_from_two_copies_of_the_app_at_once`, `test_google_requests_wait_their_turn_...` |
| R7 | A Brave cap reached made Morning movers "unchecked" for the day, and the movers scan re-asked Brave hourly per mover, using up the day | Medium | a cap is "not asked" (`skipped`), not a failure; the scan asks Brave once a stock a day and leaves half the day for the Morning plan | `test_brave_is_a_capped_fallback_...`, `test_a_reserve_keeps_part_...`, `test_the_movers_list_is_todays_...` |
| R8 | The Morning check counted 20-day-old filings as found, so Brave was not asked and the plan read "no news" | Medium | only items inside the 36 hours count (`fresh_hours`) | `test_the_news_client_adds_search_and_only_fresh_items_...` |
| R9 | The movers scan and the Google rotation ran inside the Live news loop, holding up price checks and RTPR items (11 s per scan in the reviewer's run) | Medium | they run on their own thread and feed the inbox | `test_the_searches_run_on_their_own_thread` |
| R10 | The duplicate check dropped separate filings with the same generated title (a second 6-K, the next day's 8-K) | Medium | only across sources, never for filings | `test_the_same_headline_from_two_sources_is_kept_once_but_never_two_filings` |
| R11 | Yesterday's movers stayed listed and were reused (a found row never searched again); a failed search waited an hour | Low | the list is the New York day's; a failed search is retried after 15 minutes | `test_the_movers_list_is_todays_...` |
| R12 | Each later story on the same move could open a new day-move opportunity and email hours after the move | Low | one opportunity a stock a day; the later story is listed as skipped | `test_one_move_is_traded_once_a_day` |
| R13 | The DOCTYPE check read only the first 4,096 characters | Low | the whole feed | `test_google_news_feed_is_read_and_anything_odd_in_it_is_refused` |
| R14 | The Brave key would follow a redirect to another host (requests drops Authorization, not custom headers) | Low | `allow_redirects=False` | `test_brave_is_a_capped_fallback_...` |
| R15 | Pacing gaps: a waiting request still went after another's 429; the 30 s sleep cap let requests jump the queue; the rotation ran all night; each paused name logged two warnings | Low | the pause is re-checked after waiting; a turn too far off is skipped instead; 30 minutes between rotations while closed; a paused rotation stops quietly | `test_google_requests_wait_their_turn_...`, `test_google_news_reads_your_holdings_first_and_waits_out_a_pause_quietly` |

The docs were corrected where they claimed more than the code (R16 in the report), and its suspicions
were handled as follows:

- **Brave's billing month.** It may not be the calendar month, so the limit counts any 31 days.
- **Google's refusals.** A 403 now pauses it like a 429.
- **Hour windows.** Google's window is in hours up to a day and in days beyond it.
- **Movers changing while being read.** The movers are copied before they are returned.

#### The second look

The reviewer re-ran its reproductions on the fixes. Twelve findings were fixed and three only partly
(R3, R4, R7). It also found seven new problems. All were fixed.

| # | Finding | Severity | Fix | Test |
| --- | --- | --- | --- | --- |
| N1 | The movers panel said "No news found" when headlines came back that could not be tied to the stock (two real Oracle headlines), and asked Brave anyway | Medium | such a row is *news not ruled out*, with a link to read them on Google News; the scan skips Brave then (`brave_if_unmatched=False`) | `test_a_mover_whose_headlines_cannot_be_tied_to_it_is_not_ruled_out`; Chromium `test_live_news_lists_movers_...` |
| N2 | The name list matched 24 headlines it should not have: "$3M seed round", "Jets fire GM", "the best buy in tech", "nuclear waste management", Paris Hilton, Klaus Schwab, Citi Field, Capital One Arena, Chevron deference, Merck KGaA, intelligence chiefs, meta-analysis, the Amazon Rainforest, "FANG stocks", "Dow stock futures", research notes from Goldman, Moody's and S&P Global, and Title Case headlines | Medium | each word's capital; `EXCLUDE` for other meanings and research notes; `=GM+`; FANG and other word tickers in `COMMON_WORDS`; in Title Case only a business noun counts; commas end the context; possessives with a business noun count (the misses from R3) | the 74-case test; on the reviewer's 69 headlines: 30 of 32 matched (the two misses only say the stock moved) and 36 of 37 rejected (the one left, "Dollar General" for DG, is the company's own name, listed by mistake) |
| N3 | Any result not tied to the stock left the Morning check unchecked, which with Google on would rarely leave a fade | Medium | only a headline that names the stock in some form but does not match counts (`maybe`); a roundup that Google matched on its body counts as nothing. Headlines that only say the stock moved count as `move` | `test_google_results_count_only_...`, `test_a_headline_that_only_says_the_stock_moved_...` |
| N4 | `_brave_claim` and `_google_turn` took this object's lock and then the database's; a caller inside a transaction takes them the other way round, and the two threads hung | Low | the database transaction alone orders them | `test_a_claim_from_inside_a_transaction_never_deadlocks` |
| N5 | Robinhood's quote carries no close, so after 4 p.m. the day could not be measured until the bars caught up | Low | its last regular-hours trade, made that day, is the close (`regular_last`, `regular_trade_time`) | `test_after_the_close_the_day_is_measured_from_todays_close` |
| N6 | On early-close days, 1 p.m. to 4 p.m. used the previous session's close | Low | `_session_day`: from 1 p.m. on a weekday with the market no longer open, today's session has closed | same test (the day after Thanksgiving) |
| N7 | A stock that left the movers list and came back lost its row, and Brave was asked again | Low-Medium | `_mover_memory` keeps the day's searches | `test_the_movers_list_is_todays_and_brave_is_asked_once_a_stock_a_day` |

The name cache moved to `search:name:<SYM>`, so a name cached by the first draft of 2.11 is not read.

Still open:

- whether Brave's `page_age` is UTC (it is read as UTC);
- whether Tradier's after-hours quotes carry the day's `close` (if not, a stock is measured from the bars,
  or left out until they reach the session).

## 2.12: the interactive chart

**The request.** Intraday analysis and news on Analyze a stock; possible outcomes with the percentage of each
happening; similar patterns found and overlaid on the current range; the time range and grouping (hours, days,
weeks, months, quarters) under the user's control; the chart itself manipulable, with indicators added and
removed; and whether a top or bottom is forming.

**What was built.** `intraday.py` (bars within the day from Tradier, Robinhood and demo, kept in SQLite,
grouped on the session's grid; weekly, monthly and quarterly bars from daily ones), `similar.py` (the
normalised-shape search in the stock's own past, with what followed each match), `chartview.py` (the chart,
similar and outlook calls), `static/chart.js` on TradingView's Lightweight Charts (vendored, Apache-2.0), three
`/v2/chart` routes, migration 15 and the `analyze_interactive` flag.

**Decisions.**

- *Percentages are frequencies with intervals.* Each reading says how often the same thing on this stock was
  followed by a rise, with a 95% Wilson interval and its count, and a reading with fewer than ten cases carries
  no vote. The lean is a count of readings, never a blended number.
- *Patterns from this stock's own past only* (the user's choice): more honest for bars within the day, where
  that is all the history there is, and simpler.
- *The library over the hand-drawn SVG* (the user's choice): zoom, pan and a draggable time scale are the
  library's; the overlay, the window selection and the readout are the app's.
- *Whole series to the page.* The chart loads the series held (up to 6,000 bars) with the range marked, so
  panning and zooming past the range need no request.

| Part | Where | Tests (`tests/test_chartview.py` unless said) |
| --- | --- | --- |
| Demo bars within the day pinned to each day's open, high, low and close | `intraday.demo_intraday` | `test_demo_bars_within_the_day_follow_each_days_open_high_low_and_close` |
| Grouping on the session's grid; weekly, monthly, quarterly | `group_intraday`, `group_daily` | `test_bars_are_grouped_on_the_sessions_grid_and_never_across_a_day`, `test_daily_bars_group_by_week_month_and_quarter_dated_on_their_first_day` |
| Kept in SQLite, refreshed sparingly, a failed refresh kept and reported | `Intraday.bars` | `test_bars_within_the_day_are_kept_and_refreshed_sparingly` |
| Tradier timesales and Robinhood 5-minute bars read from their shapes | `providers.tradier.intraday`, `providers.robinhood.find_intraday_bars` | `test_tradier_timesales_are_read_from_their_documented_shape`, `test_robinhood_bars_within_the_day_keep_their_times_and_skip_daily_ones` |
| The pattern search: planted shapes found, no overlap, the bar lowered and said | `similar.search` | `test_a_planted_shape_is_found_in_the_past_but_never_in_itself`, `test_the_bar_is_lowered_a_step_at_a_time_...` |
| The chart, custom ranges, news on the bars, future times, the outlook | `chartview.ChartView` | `test_the_chart_covers_the_range_...`, `test_custom_ranges_are_checked_and_honoured`, `test_the_news_sits_on_the_bars_of_the_range`, `test_similar_takes_the_window_on_the_screen_...`, `test_the_outlook_reads_patterns_a_top_or_bottom_...` |
| The routes and the flag | `api_v2.py`, `features.py` | `test_the_chart_routes_answer_and_check_their_arguments`, `test_settings_know_the_chart_feature` |
| The page's helpers; the screen in Chromium | `static/chart.js` | `test_the_pages_helpers_clamp_windows_build_overlays_and_lay_out_scenarios`; `tests/test_static_assets.py::test_analyze_has_an_interactive_chart_with_patterns_and_outcomes` |

**Found while building.**

- The library's production build does not check that a series' times are unique and ascending; a repeated
  time at the junction of a match and its continuation mis-indexed the whole time scale. The overlay keeps
  one point per time.
- The library lays its panes out with a `<table>`, and the app's table styles (`td:first-child { min-width }`,
  cell padding) pushed the canvases right of where the library measured them, so coordinates from its API did
  not match the picture. `app.css` keeps those styles off the chart.
- A visible range set on a chart already drawn is laid out a frame late and measured against the old data
  edge; any change of view redraws the chart with the range set in the same tick.

Not verified against the real services, which this build environment cannot reach: Tradier's timesales and
Robinhood's bars within the day are written to their published formats.

### The independent review of 2.12

An independent reviewer read the change and ran reproductions. It reported 16 findings; all were fixed.

| # | Finding | Severity | Fix | Test |
| --- | --- | --- | --- | --- |
| C1 | "Top or bottom forming" never worked: the series kept only the indicator columns, the reversal engine needed the pattern columns, and the failure was swallowed | High | the enriched frame is kept whole and the engine's result cached with it; a failure is logged | `test_the_chart_covers_the_range_...` asserts the reading and its markers |
| C2 | On live sources every call refetched the whole span: freshness was judged by the first bar held, which sits at 9:30 of a trading day, so a span's first calendar day often had none | High | `asked_from` (the span last asked) is kept in `intraday_meta` and judged instead; the lock is per symbol and interval and not held across the fetch | `test_a_live_source_is_asked_once_for_the_span_then_only_for_the_last_day` |
| C3 | Readings said "enough" at ten cases but voted by their sources' thirty, so only the pattern reading could vote | Medium | one rule for every reading: ten cases, and its way at least 55% of the time; each reading's `frequency` is now the frequency of its own way | `test_the_outlook_reads_patterns_...` recomputes the lean from the readings |
| C4 | A match ending just before the window had the window's own bars as "what followed" | Medium | neither a match nor the bars read after it may reach into the window | `test_a_planted_shape_is_found_...` (a shape repeated back to back) |
| C5 | The default horizon fell through to the shortest | Medium | the third horizon (a week on daily bars) | same test as C3 |
| C6 | Each Analyze left the last chart and its theme listener alive | Medium | one live instance: a new one disposes the last | — (page) |
| C7 | The series cache grew without bound and keyed daily series by the range's days | Medium | keyed by the years fetched for daily and coarser bars; entries older than a minute evicted | — |
| C8 | Two searches per press, and the horizon select ran both again | Low | the outlook carries the search; one request | — (page) |
| C9 | Scenario labels said "daily range" on five-minute bars | Low | the unit follows the grouping | — |
| C10 | A pre-market headline sat on the day before's bar | Low | a headline before the open goes on that day's first bar | `test_the_news_sits_on_the_bars_of_the_range` |
| C11 | A weekend custom range or a holiday answered 502 | Low | a 400 that says to pick trading days; 1D is the last day that has bars | `test_custom_ranges_are_checked_and_honoured` |
| C12 | Demo bars for the rest of today were already there | Low | nothing after the present moment is fetched or returned | — |
| C13 | The outlook's headline links were not scheme-checked | Low | http(s) only, as on the chart | same test as C3 |
| C14 | The distance was presented as a second measure though it is a function of the correlation | Low | reported, not used; the docs say so | — |
| C15 | Tests that asserted tautologies | Low | rewritten to assert the readings, the markers, the vote rule and the lowered bar | `tests/test_chartview.py` |
| C16 | A swing-size change left the structure panel on the old size; unused range config; a searched window stuck for later presses | Low | both reload; the config is gone; each press reads the screen afresh unless a window was selected | — |

Its second look confirmed all sixteen and found two more, both fixed: the `asked_from` column had been added to migration 15 after that migration could have run (now migration 16), and a series too short for the window plus its after-bars said "lower the bar" when no window was eligible at all (it now says the history is too short, and `eligible_windows` is reported).
