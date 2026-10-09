# Release notes

The app only advises. No version places, changes or cancels an order, and the Robinhood
connection stays read-only. The engineering record behind these notes, with the tests for
each change, is [IMPROVEMENTS.md](IMPROVEMENTS.md).

## Upgrading

1. Stop the app, update it (`git pull` or the zip), and start it again.
2. The first start migrates the database. It copies the database to `data/backups/` first
   (the newest ten copies are kept) and records the copy in the log
   (`event=migration_applied ... backup=...`). If a migration fails, nothing is changed and
   the app stops with the error.
3. Open **Data health** and press **Show the reconciliation report**. A database from an
   older version normally shows positions reopened after closing (kept as recorded),
   snapshots built with hindsight, and missing snapshots. Press **Rebuild missing
   snapshots** to fill what the saved price history allows.
4. To go back, stop the app and put the copy from `data/backups/` in place of
   `data/tradeassist.db` (or the file `TRADEASSIST_DB` names), then run the older version.

## 2.3.0

**Fixed**

- 2.2.0 shipped with a page script the browser refused, so the Analyze, Positions and
  Portfolio risk screens were missing and the Version 2 parts of the Journal, Pattern lab
  and Backtest screens did not appear. Fixed, with tests that load every screen in a
  browser.
- The positions list counted a position with no price as $0 in its unrealized total. It now
  leaves it out and says "+ unknown".
- Journal analytics counted a habit that could not be checked (no price history, no entry
  context) as "did not happen", which flattered the comparison groups.
- Signal frequency intervals treated overlapping cases as independent and were too narrow.

**New**

- **Data health** screen: the data source and its failures, how fresh each open position's
  price is, what could not be valued, saved price history, snapshots, the schema version and
  the copy taken before the last upgrade, and recent problems.
- **Reconciliation report**: every stored figure checked against the fills it comes from,
  plus reopened positions, duplicate-looking fills, journal entries that differ from their
  position, expired options still open, and SQLite's integrity check. Read-only.
- **Repair from the fills**: after you confirm, copies the database and recomputes the
  figures that differ. It never changes, merges or deletes a fill.
- **Trade review** on a closed position: planned, done, known at entry, thesis and targets,
  the result after fees and borrow costs, and what is missing.
- **Portfolio summary** strip and all warnings grouped Critical, Warning and For
  information. The position screen shows the cost of the open position and its worth now.
- Journal analytics use one definition of a trade and its net result (trading plus borrow
  fees and dividends), with definitions and coverage on the screen.
- Signal calibration gets a held-out check and a calibration status; signal frequencies are
  labelled as history, not forecasts.
- Backtests, walk-forwards, pattern runs and signal histories record their settings and a
  fingerprint of their data, so two runs can be compared and repeated.
- The database refuses impossible rows (a zero quantity, an option without a strike, a fill
  without a time). Errors say what to do; a busy database asks you to try again.
- Logs are structured (`event=... key=value`) and never carry a token or password.

**Known limitations**

- MAE and MFE use daily bars, so the entry day's own range is not counted.
- Calibration and signal histories built before 2.3 keep their old intervals until rebuilt.
- Database rules apply to new writes; older rows that break them are reported, not changed.
- The recent-problems list starts empty when the app restarts.

## 2.2.0

**Risk**

- Gross and net exposure side by side, by stock and by sector; open risk measured on a
  stated basis (to invalidation, option premium, or the dollar risk you entered) with what
  could not be measured named.
- Marks use the bid and ask: unrealized profit at the mark and an estimate of what closing
  now would get. A long option with no bid is worth $0 to close.
- Betas, correlations and bad-day figures no longer treat a missing day as a flat day; a
  beta needs 40 days both traded. Historical bad day and expected shortfall beside the
  normal estimate.

**Shorts and options**

- Borrow fees, borrow rates, dividends paid and cash adjustments, kept apart from trading
  profit; net economic profit is both. Unknown borrow costs are said to be unknown.
- Stock splits rescale earlier fills when replayed; the fills themselves are kept.
- How a bought option ended: expired worthless, exercised (with the shares), or adjusted
  by a corporate action. An expired option still open is flagged.

**Market data**

- Every price has a freshness tag: live, delayed, latest session (market closed), stale or
  time unknown. Stale prices are never shown as current; pricing and tickets never use a
  saved copy.
- After three failures in a row the app pauses requests to the source for 30 seconds.

## 2.1.1

- A closed position stays closed. A new trade in the same stock is a new position.
  Positions reopened by older versions are kept as recorded and flagged.
- Portfolio risk and the daily loss stop say what they could not measure instead of counting
  it as zero; "within the stop" is never shown when part of the account was not measured.
  With `INCOMPLETE_RISK_POLICY=BLOCK` the trade ticket treats an incompletely measured day
  as a broken limit that has to be overridden on purpose (the default, WARN, shows a note).
- No NaN or infinite number can enter the accounts.
- A wrong fill is corrected with a reason; the original is kept in an audit record.
- Imported fills with a broker id are recorded once.
- Every trade, option-journal ones included, gets entry and exit snapshots, labelled by how
  they were made; a snapshot made after the fact uses the prior session's close, never later
  data. Clearing a plan field (sending `null`) now clears it.
- The database is copied before every upgrade.
