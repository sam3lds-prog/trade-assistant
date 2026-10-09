# Reliability, accounting, risk and analytics improvements (2.1.1 to 2.3)

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
