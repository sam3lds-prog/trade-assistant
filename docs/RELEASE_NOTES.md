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

## 2.4.0 (in progress)

Built in parts; each part is on `main` once it is finished. No new database migration so far.

**Options lab** (new screen). Pick a ticker to see what its option prices imply: implied
volatility for expirations from a week to a year (chart and table), the expected move and
range for each, the straddle price, put-against-call skew, whether the term structure is
upward, flat or inverted, the move priced in for the next earnings report, and implied
against realized volatility. A Monte Carlo check of any contract simulates 20,000 price
paths with a fixed seed, compares its value with Black-76, and gives the chance of finishing
in the money, of touching the strike (on a close and at any moment), and the spread of
outcomes for a buyer at the midpoint or your price, optionally with the earnings move added.
These are what the market implies under a model, not forecasts, and nothing is recorded.
Switch it off with `FEATURE_OPTIONS_LAB=0`. Details: [V2.md](V2.md#options-lab-24).

**Scanner** (new screen). Scan the watchlist, 100 large US companies or your own tickers for
RSI, MACD, moving-average and 52-week readings, volume spikes and every pattern-lab signal of
the last few days; each signal shows how often it was followed by a move its way before
(once the signal history is built), as a historical frequency, not a forecast. A second tab
lists calls against puts, put/call ratios, premium traded and the contracts trading more
than their open interest. Option volume does not show who bought and who sold, so the
screen never calls it bullish or bearish. Switch it off with `FEATURE_SCANNER=0`. Details:
[V2.md](V2.md#scanner-24).

## 2.3.1

Fixes from an independent review of 2.1.0 to 2.3.0. Every one was reproduced first and has a
regression test; the full list is in [IMPROVEMENTS.md](IMPROVEMENTS.md). Migration 9 runs on
the first start (a copy of the database is taken first).

**Risk checks**

- The trade ticket's open-risk limit now counts open risk on the Positions screen, not only
  the option journal. A position whose risk cannot be measured, or that is priced from a saved
  copy, is named in a note. The per-trade limit is unchanged, so a protective option on shares
  you hold is not blocked by the shares' own risk.
- The daily loss stop never reports a complete, OK day when the positions could not be read,
  when a price is a saved copy from before the data source failed, or when yesterday's close
  would come from history that could not be refreshed and ends before yesterday.
- The bad-day estimate names any holding it leaves out for lack of history, and says it
  understates. A total that nothing could be measured for shows as unknown, not $0.

**Records**

- A position whose fills no longer add up (possible with data from older versions) no longer
  stops the other screens: it is listed on its own with a Correct button for each fill and
  counted as not measured everywhere.
- A split, a voided split or a contract adjustment that would reopen a closed position, or
  turn one position into two trades, asks for confirmation and a reason, and is recorded as a
  correction.
- A split or contract adjustment entered as a date applies to every fill on that date.
  Voiding a split keeps later splits and plan edits.
- A tiny remainder left by rounding or a split (below a millionth of a share) closes at the
  last sale's price, instead of keeping the position open for ever.
- Borrow status is PARTIAL when any night of the short has no recorded fee (tonight is not
  owed yet, and a short covered the same day owes nothing).
- The thesis and notes of an option-journal trade are edited in the journal.
- Recording an option as expired worthless with no known stock price needs a reason.

**Data and security**

- A quote's age comes from its bid and ask times; a time in the future is unknown, not fresh;
  Friday's close is not stale on a Monday holiday.
- Credentials are removed from error messages, the health screens and tracebacks, not only
  from log lines, and more forms of them are recognised.
- The app answers only to this computer's own host names, and refuses changes requested by
  another web page open in the browser.

**Analytics**

- Signal calibration is tested on the earlier 70% of dates only, with a gap of one horizon
  before the held-out dates, so they are a real check. Frequency ranges are wider when one period holds most of the cases.
- Trade reviews leave out the exit day's range when the exit was during the session.
- Research runs fingerprint the benchmark and market-regime prices they used.

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
