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

## 2.5.0

One path from "which way?" to "what limit do I type at my broker?". There is no migration, no
new key and no new dependency. There is one new flag, `FEATURE_TODAY`, which is on by default.
Everything stays advisory: nothing places, changes or cancels an order, or reads your broker's
balances or positions.

**Today** (new, and the screen the app opens on). It shows:

- **Market:** the market regime in one line.
- **Your limits:** your daily stop and open risk, worded as on Portfolio risk. If some
  positions could not be measured it says so, never $0.
- **Leaning long and leaning short:** up to eight stocks each from your watchlist (or the 100
  large companies), largest score first. A stock leans the way its trend evidence points on
  the last finished bar. The lean is held back when the market regime works against that
  side, or when a top is forming under a long lean (a bottom under a short one). Each
  held-back stock is listed with its reason.

Choose a stock and Decide opens with that direction, graded. Scores count evidence; they are
not chances. Switch it off with `FEATURE_TODAY=0`.

**One thesis on Analyze (Decide).** One form holds the direction, the levels, the holding days,
contracts and an option budget. One button, **Grade and find a contract**, grades the stock
trade, then finds calls or puts on the same levels. Arriving from Today or the Scanner fills
the direction and runs it.

**Shares or a contract.** The sized shares and the best risk-adjusted contract, side by side.
For each it shows:

- what you pay;
- what it makes at target 1 and loses if the idea is wrong;
- the chance option prices imply of a profit on your horizon date;
- what option prices imply it is worth in expectation, next to what you pay.

Both are drawn on one chart. The expected figure is worked out over every stock price, so it
comes out close to what you pay: options are priced fairly against themselves. It is not an
edge, and the screen says so. Past cases appear only once the signal history is built (until
then the screen says so and offers to build it), and **Show the path range** runs the Monte
Carlo as the same model. The three are never blended into one number.

**The plan on the chart.** After a grade, the trend chart runs on to your horizon date. It adds
the targets, the invalidation, the range option prices imply by then and where the contract
breaks even.

**Set the limit.** Every contract card has it. It opens Price an option on that contract
(ticker, call or put, expiration, strike, buy, contracts), already priced, with the limit
ladder ready. If the contract is no longer listed it says so rather than pricing another.

**Grouped menu.** The menu is grouped into Today, Find, Decide, Price & simulate, Hold, Learn
and Trust. The Scanner's rows all open Decide: technical rows with the direction their signals
agree on, options-activity rows with none (volume does not show who bought) plus an *Options
lab* link. The reversal scan moved from Analyze to a **Reversals** tab of the Scanner. It stays
on Analyze if the Scanner is switched off.

**Checked before release.** Two reviewers who had not seen the code being written checked it:
one for the calculations and these rules, one for the screens. Their findings were fixed, with
tests. The main ones:

- **Skew read as an edge.** The expected figure was first worked out on the at-the-money
  volatility, which made out-of-the-money calls look like bargains and puts look dear. Each
  contract is now read on its own volatility.
- **A typed entry below the market** showed as an expected gain on shares. Shares are now
  valued from today's price.
- **A slow analysis** could replace the stock you had just opened.
- **Turning a thesis round** kept the old side's levels.
- **An earnings date** typed for one stock followed Set the limit to another.
- **A failed second grade** left the first plan on the chart.

The details are in [IMPROVEMENTS.md](IMPROVEMENTS.md#25-one-decision-path).

**Not in this release:**

- a third row in the comparison for a multi-leg structure from the strategy builder;
- a phone layout for the new screens (they work at phone width, but the lists scroll
  sideways).

## 2.4.0

Options analytics, a scanner, a strategy builder, and an AI plug-in for news and the market's
make-up. Migration 10 (a table of AI requests) runs on the first start; a copy of the database
is taken first. The two new keys (`FINNHUB_API_KEY`, `ANTHROPIC_API_KEY`) are optional: see
`.env.example`. Not verified against the live Anthropic and Finnhub services from here (the
tests use fake replies in their documented shapes), so check the first brief on screen: it
shows its sources, model and usage.

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

**Strategy builder** (on the Simulate screen). Describe a trade in plain words ("AAPL iron
condor 45 days 16 delta 5 wide") or pick one of 16 strategies: verticals, straddles,
strangles, iron condors and butterflies, butterflies and calendars. The app picks real listed
contracts by delta and width, shows the net debit or credit, the most it can make and lose,
breakevens, the market-implied chance of a profit and whether it fits your per-trade risk
limit, lets you move the strikes or wings one strike at a time, and loads it into the
simulator. Modelled only: nothing is placed or recorded. Switch it off with
`FEATURE_STRATEGY_BUILDER=0`. Details: [V2.md](V2.md#strategy-builder-24).

**Market & news, and AI briefs** (new screen; migration 10). The market's make-up from the
app's own prices: the main indexes, VIX (or realized volatility), the regime, sector funds
against SPY, breadth over 100 large companies, and SPY's put/call balance. Headlines for the
market and any stock from Finnhub (`FINNHUB_API_KEY`, free) with SEC filings, each with a
word-list tone. With `ANTHROPIC_API_KEY`, Claude writes a market brief and stock briefs from
those figures and headlines, searches the web for the latest news, shows its sources and
exactly what was sent, reads headline tone, and reads trade descriptions the rules cannot.
AI requests are billed by Anthropic to that key's account and capped at `AI_DAILY_REQUESTS`
a day (40 by default). The key never leaves the server and is never logged; nothing about
your account is sent. The Analyze screen gets a News panel, and Data health shows AI and
news status. Details: [V2.md](V2.md#market--news-and-the-ai-plug-in-24).

**Fixed before release** (independent review; details and tests in
[IMPROVEMENTS.md](IMPROVEMENTS.md#independent-review-of-24-and-fixes)):

- The trade reader no longer flips a trade: "sell AAPL put spread" is a credit spread and
  "short the straddle" a short straddle; "at 30 delta" is not a strike of 30; "25/30 spread"
  is not 30 contracts; dates such as "next week" or "Oct" and lone strikes ("230 calls") are
  read correctly; trades that are not templates are refused with the reason.
- The Monte Carlo's earnings jump is timed on the option's own clock and its variance is
  removed only when the jump is added.
- The app no longer answers to the test client's host name outside the tests, and another web
  page cannot set off scans, the make-up or news through the browser.
- Concurrent AI requests cannot pass the daily limit; every call (including resumed web-search
  turns) counts; odd replies from the AI or Finnhub can no longer cause errors or be cached.
- Unknown breadth and options-activity figures are shown as n/a, never 0.

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
