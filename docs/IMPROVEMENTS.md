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
