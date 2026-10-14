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

## 2.8.0

**Morning plan** (new, the landing screen) reads the stocks that jumped since the last close, says which
are worth riding and which look stretched enough to fade, watches for reversals about to confirm, and
turns each setup into a trade card with the exact limit price to type. Optionally, the same plan arrives
by email every weekday at 7:00 in your computer's own time zone.

- **Requirements.**
  - Migration 13 (`morning_picks`, `morning_watches`); the usual backup is made first.
  - New flag `FEATURE_MORNING`, on by default.
  - New optional settings: `SMTP_USER`, `SMTP_PASSWORD`, `SMTP_HOST`, `SMTP_PORT`, `EMAIL_TO` for the
    email, and `MORNING_MIN_PAYOFF` for the limit cap. No new dependency (email uses Python's own
    `smtplib` over STARTTLS with the server's certificate verified).
- **Safety.** Nothing places an order. The SMTP password stays in `.env`, is never logged, and is cleaned
  out of any error message. Nothing about your account is sent anywhere.

**The movers.** About 290 large optionable US stocks plus your watchlist are read each morning.

- A move counts when it is at least 3% **and** two of that stock's own daily ranges, so 4% in a quiet
  stock counts for more than 4% in a volatile one. Premarket quotes are read against the last finished
  close; a quote still sitting on the close is read close-to-close.
- **Ride or fade.** A mover **with fresh news** (headlines or SEC filings within 36 hours) and at least
  1.5 ranges is read to keep going — the trade goes the move's way. A mover with **no news found**, at
  least 2.5 ranges, and already overextended **before** the move (RSI at 30/70, two ATR from its 20-day
  average, or three one-way days) is read to snap back — the trade fades it. News that could not be
  checked (no news source configured, or one failed) is shown as *not checked* and never treated as
  "no news".
- **The past record.** For each setup, five years of saved history across all these stocks is searched for
  the same pattern, and the card says how often the trade's way was ahead after the horizon — with a
  plain warning that these are frequencies, not forecasts, and that history cannot say which old moves
  had news.

**The trade cards.** Each card is a complete plan: entry, stop, two targets, a horizon, shares sized to
your risk per trade, and the best call or put the budget pays for.

- **The limit price** is capped where reaching target 1 still pays at least `MORNING_MIN_PAYOFF` (default
  1.0×) what the stop loses: cap = (value at target + payoff × value at stop) ÷ (1 + payoff). The
  suggested price is the quote midpoint, never above the cap, rounded down to a valid tick. When no price
  under the cap exists, the card says why and falls back to shares.
- **Order to copy.** One line — "Buy 2 NKE 2026-10-31 $72 Call · Limit $1.62 · Good for day" — with a
  Copy button, so the Robinhood ticket takes seconds.
- At 7:00 options have not traded yet (they trade 9:30–16:00 New York time), so the option prices are the
  previous close, and every card says so. A second 7:45 run with live prices can be scheduled.

**Reversal watch.** Stocks from your watchlist and the movers whose reversal reading is forming or
confirmed are listed with the price that would confirm a turn. Press **Watch** and the app checks the
price about once a minute while the market is open; when it crosses, you get an email (if set up) and the
screen shows the trigger. Watches survive restarts; at most 40 are active at once.

**The 7:00 email.** `./setup_morning.sh` schedules it on a Mac (launchd, local time; weekends skip
themselves; `--also-45` adds the 7:45 run; `--remove` undoes it). Linux gets the cron line to paste. Try
it first without sending: `.venv/bin/python run.py --morning-print`. A weekday holiday still sends — the
plan says the market is closed rather than silently not arriving.

**The scorecard.** Each emailed or sent pick is recorded and graded against the next closes (the pick
day's own close is the first), the trade's way. Option picks are graded by their stock plan — no
connected source has past option prices — and the scorecard says so.

**Before release,** two independent reviews (the finance and arithmetic; the robustness and security)
checked the branch. Among what they found and was fixed:

- An SEC filing date crashed the news check and sank the whole plan; now one stock can never stop the
  morning.
- The email's STARTTLS did not verify the server's certificate, so the SMTP password could have gone to
  whoever answered on a hostile network.
- Dates were read in UTC, so a plan built in the evening was dated tomorrow.
- The past record required overextension *after* the move while the live rule read it *before*; both now
  read the bar before the move.
- A failed send still recorded the picks; the dry run recorded them too. Picks now count only once the
  plan reached you.
- The holiday check would have silently skipped every real morning east of New York.

The reviewer also verified the limit-cap formula against 20,000 random cases. The full list is in
[IMPROVEMENTS.md](IMPROVEMENTS.md).

## 2.7.0

**Trade ideas** (new, under Find) scans up to the 200 largest US companies. It lists the highest-graded
trades to buy and to short, and simulates any one of them.

- **Requirements.**
  - No migration and no new setting.
  - One new flag, `FEATURE_TRADE_IDEAS`, on by default.
  - No new dependency.
- **Safety.** Nothing places an order, and nothing about your account is read or sent anywhere.

**The scan.** Pick the 200 largest, the 100 large companies, or your watchlist, and press Scan.

- **Choosing the side.** Each stock's side, buy or short, comes from the same trend evidence as Today, and
  is held back the same way when the market or a forming reversal works against it.
- **Grading.** The trade on that side is graded as on Decide:
  - an entry at the current price;
  - a stop and two targets from the chart;
  - shares sized so the stop costs at most your risk per trade, and never more than the account.
- **Ranking.** Trades are ranked by grade, then score, then reward to risk. Each row shows what the sized
  trade makes if target 1 is reached and loses if stopped.
- **These are not forecasts.** The dollars are what happens if the price gets there; a grade sums checks.
- **Listed separately:**
  - stocks held back, with the reason;
  - trades graded AVOID;
  - stocks that could not be read.
- **Running a scan.**
  - It runs in the background with a progress bar, and can be stopped.
  - On demo data the 200 take about two minutes. With live data, the first scan of the day fetches each
    stock's history, spaced out so the other screens keep working, which takes several minutes.
  - The last result is kept, also across restarts.

**Simulate.** Any row, or any stock with your own levels, can be walked on 20,000 price paths at the
volatility option prices imply. Each path follows the trade's own rules: out at the stop or at the target,
whichever comes first, or at the close on the horizon date.

- **Exit plans:** all out at target 1, all out at target 2, or half at each.
- **What it shows:**
  - how often each exit happens and what each one makes or loses;
  - the range of results;
  - the spread of the price over the days.
- **With a contract.** The same exits are run with the best call (for a buy) or put (for a short) your risk
  per trade pays for, valued at its own implied volatility.
- **What the average means.** Averaged over every path, a trade comes out close to break-even. What is
  left is the carry in the forward price (interest less expected dividends), not an edge; the screen says
  so.
- **Next steps from a result:**
  - Set the limit on the contract;
  - simulate the contract alone;
  - open the thesis on Decide with the same levels.

**Also in 2.7.0:**

- **Decide's outcome range.** It now says "eight in ten" outcomes fall between the 10th and 90th
  percentiles. 2.5.1 said nine in ten, which was wrong.
- **A short's suggested targets stay above zero.** Before, a wide stop could put target 2 below zero. They
  now stop at a twentieth of the entry, and the levels note says so.
- **Sector comparisons.** 3M (MMM) and the newly listed large companies are compared with their sector
  fund. Before, they were compared with SPY only. MMM's trend score can change slightly.
- **Tradier waits.** When Tradier's request allowance runs out, waiting for it no longer blocks the
  app's other requests.

**Before release,** two independent reviews (the arithmetic and model, and the robustness and screen)
found 31 problems, all fixed or noted. Among them:

- Trade ideas' code replaced the drawing function of Simulate a trade, so that screen drew nothing.
- The contract's average showed the volatility skew as a profit.
- Days where both the stop and a target were touched were all counted as stopped.
- The screen started a 200-stock scan by itself.

The full list is in [IMPROVEMENTS.md](IMPROVEMENTS.md).

## 2.6.0

**My trades** (new, under Learn) reads your Robinhood history and shows how your trading has
gone.

- **Requirements.**
  - Two migrations: 11 and 12. The database is copied before they run.
  - One new flag, `FEATURE_TRADE_HISTORY`, on by default.
  - One new setting, `ROBINHOOD_ACCOUNT_READ`, off by default.
  - No new dependency.
- **Safety.** Nothing can place, change or cancel an order. Nothing about your account is
  sent to the AI.

**Where the history comes from.**

- **Robinhood's account activity report.** This CSV is the complete source: shares and
  options, with fees.
  - To get it in Robinhood: Reports and statements > Account activity report > Generate new
    report. It takes about 2 hours, and up to 24.
  - Import it on My trades. A later report adds only rows not already imported.
  - Rows the app cannot read are listed with the reason, never guessed.
  - Removing an import keeps the rows another import also has.
- **The Robinhood connection** (optional, off unless `ROBINHOOD_ACCOUNT_READ=1` with
  `DATA_MODE=robinhood`).
  - **What it reads:** your filled orders and current holdings. Only filled quantity counts:
    partly filled orders and cancelled ones that filled in part are counted for what filled.
  - **What it can call:** only tools named exactly `get_equity_positions`,
    `get_stock_positions`, `get_option_positions`, `get_equity_orders`, `get_stock_orders`
    and `get_option_orders`. No balance, account number or bank tool fits.
  - **Option orders may be missing.** Robinhood's connection may not offer them; the report
    has them.
  - **Overlap with the report.** On days a report covers, the report's rows are used.

**What it shows, for any dates you pick** (presets from the last 30 days to all history, or
your own):

- **Results by week, month, quarter or year.** Each period shows:
  - the result after fees;
  - how many trades won, with a range;
  - what was opened and the risk it carried.

  Click a bar to look closer.
- **Overall:**
  - the running total and the deepest fall from a high;
  - profit factor;
  - average win and loss;
  - the largest loss.
- **Strategies and the risk each carried.** Strategies (spreads, condors, covered calls,
  short puts and others) are named from the legs and ranked by exposure. Each shows the most
  it could lose as opened, the largest single risk, and dollar-days at risk. Unlimited risk
  (uncovered calls, short shares) is counted on its own, never added in as zero. Also shown:
  open risk over time, and its peak.
- **What went better or worse than usual.** Each kind of trade against your average closed
  trade:
  - days held;
  - days to expiration;
  - how options ended;
  - where the strike was;
  - size against your usual;
  - weekday;
  - right after a loss;
  - how familiar the stock was.

  These are counts of your own past trades with how many each rests on, not forecasts.
- **Verification.** Every trade opens to its legs and the statement rows it was built from,
  exactly as written in your report, to check against Robinhood. Holdings still open are
  compared with what Robinhood's connection says you hold, when that is on.

**Unknown stays unknown.**

- A close of something opened before your history starts is listed and left out.
- An option past expiration with no row saying how it ended is marked unresolved.
- Risk the app cannot bound is "not measured".

**Before release**, two independent reviews (the arithmetic, and security with the screen)
found faults, and all were fixed. A trade split in two by an assignment, risk not shrinking as
a trade was partly closed, a race that could show an empty analysis, and an account guard
broader than needed are among them. The full list is in [IMPROVEMENTS.md](IMPROVEMENTS.md).

## 2.5.1

Six fixes from a review of 2.5. There is no migration and no new setting. Responses only gain
fields.

- **One forward for the stock's chances.** The chance of passing target 1, the chance of
  passing the invalidation, the shaded range and the expected price now all start from the
  same forward price. Before, the two chances started from today's price. On a hard-to-borrow
  stock, where the forward sits well under the stock price, Decide could show two different
  chances for one trade.
- **Shares use your entry.** When you type an entry, every share dollar and the chance the
  shares are ahead start from it. The "if bought at today's price" wording is gone. The
  contract row does not change with the stock entry.
- **Outcomes, not an average of about $0.** The comparison's last column now shows the
  middle outcome on your horizon date and the range nine outcomes in ten fall in, for the
  shares and for the contract. These come from the 10th, 50th and 90th percentile prices
  option prices imply. The average (about what you pay, by construction) moves to the
  footnote.
- **The shaded range names its expiration.** The legend says which expiration's volatility
  it comes from.
- **Today shows past frequency.** Once the signal history is built, each row shows how often
  stocks with a score in the same range were higher 10 days later: the percentage, the
  number of cases and the range. The figure is the same for longs and shorts, and it is a
  past frequency, not a forecast. When there is no history there is no column.
- **One form, one sentence.** The unused "choose a call or put" form is removed. The grade
  card no longer repeats the past-cases sentence; it now appears only on the comparison.

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
