# Trade Assistant

A local web app that helps you decide what limit price to set on an option, checks
the trade against a list of known ways option buyers lose money, tests chart
patterns on real history before you rely on them, and keeps a journal so you can
see whether any of it is working.

It is advisory only. There is no code in it that places an order: you enter trades
in Robinhood yourself. It is not financial advice, and passing every check does not
make a trade safe. Buying an option always puts the whole premium at risk.

Version 2 adds position intelligence: stock you are long or short and the calls and puts
you hold, in one place with correct accounting; a trend engine that shows its evidence;
the market regime; signals with their past record; trade grades; an option picker for a
stock thesis; portfolio risk and stress tests; journal analytics; condition-based
backtests; and alerts. Version 2.1 adds trend reversals: whether a stock is at a potential
bottom or top, or has confirmed a reversal, and a score for the calls or puts to trade it,
with the gain or loss in each case. See "Version 2" below and `docs/V2.md` for the details.

Version 2.1.1 makes the records and risk figures trustworthy before adding anything new: a
closed position stays closed, portfolio risk and the daily loss stop say plainly what they
could not measure instead of counting it as zero, wrong fills are corrected with a recorded
reason, every trade (option-journal ones too) gets entry and exit snapshots that use only
data available at the time, and no NaN or infinite number can enter the accounts. The
engineering record is `docs/IMPROVEMENTS.md`.

Version 2.2 makes the risk figures more accurate: gross and net exposure side by side, open
risk measured on a stated basis with what is unknown named, bid/ask marks with an estimate
of what closing would get, betas and correlations that no longer treat missing days as flat
days, a historical bad-day estimate beside the normal one, borrow fees and dividends for
shorts (kept apart from trading profit), stock splits, how options ended (expired worthless,
exercised, adjusted), and a freshness tag on every price so an old quote is never shown as
current.

Version 2.3 makes the history and the research checkable. Journal analytics use one
definition of a trade and its result (trading profit plus borrow fees and dividends), and a
habit that could not be checked is no longer counted as "did not happen". Signal frequencies
get honest intervals for overlapping cases and a held-out check; backtests and studies record
the settings and data they ran on, so two runs can be compared. A new **Data health** screen
shows the data source, how fresh each open position's price is, what could not be valued,
the saved price history, the snapshots and the database, with a read-only reconciliation
report of every stored figure against its fills and a repair (after a copy of the database)
when one is wrong. The database itself now refuses impossible rows, a closed trade gets a
plain-words review, errors say what to do, and the log never carries a credential. 2.3 also
fixes a 2.2.0 fault that kept the Version 2 part of the page from loading, so the Analyze,
Positions and Portfolio risk buttons were missing and the Version 2 additions to the other
screens did not appear. Version 2.3.1 fixes what an independent review of 2.1.0 to 2.3.0
found, among them a ticket open-risk check that ignored the Positions screen and a daily loss
stop that could report a complete day from a stale price. Release notes: `docs/RELEASE_NOTES.md`.

## Start it

Needs Python 3.11 or newer (built and tested on 3.13 with pandas 3 and numpy 2).

```bash
cd trade-assistant
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env        # then edit .env
python run.py               # open http://127.0.0.1:8000
```

What to put in `.env`:

| Setting | Why it matters |
|---|---|
| `TRADIER_LIVE_TOKEN` | Real-time option quotes and Greeks. Without it you cannot trust a limit price. Used for market data only. |
| `TRADIER_SANDBOX_TOKEN` | Works for research and backtests, but quotes are 15 minutes late and the app says so on every price. |
| `ACCOUNT_SIZE` and the limits | The sizing checks are percentages of this. The default is a placeholder of $10,000. |
| `SEC_USER_AGENT` | Your name and email, which the SEC requires. Switches on filings and the earnings-date estimate. |

With no token the app runs on made-up demo data so you can learn the screens.
Demo data is stored in a separate database file and never mixes with real history.

## Keep it up to date from GitHub

If you installed the app by cloning its GitHub repository, update it with one
command and then restart it:

```bash
cd ~/projects/trade-assistant
./update.sh
```

It downloads the newest version, installs any new packages, and lists what changed.
Your `.env` and `data` folder are not part of the repository, so updating never
touches your settings, journal or Robinhood sign-in. If you have edited files in the
folder yourself, it stops instead of overwriting them.

## Use your Robinhood account for the data

Robinhood offers a connection for outside programs (the Robinhood Trading MCP
server). The app can take stock quotes, option chains, option quotes and Greeks
from it, so the numbers you price against are the ones Robinhood itself shows.

```bash
pip install -r requirements.txt      # again: the connection needs one new package
python run.py --robinhood-check      # or: python run.py --robinhood-check NVDA
```

The check opens Robinhood's sign-in page in your browser. After you approve, it
runs every data call the app makes and prints OK or FAILED for each: stock quote,
expirations, option chain and Greeks, one live contract quote, price history. If
all are OK, set `DATA_MODE=robinhood` in `.env` and start the app as usual.

What the connection can and cannot do:

- The app can call these tools on Robinhood's server, by exact name, and no
  others: `get_equity_quotes`, `get_option_chains`, `get_option_instruments`,
  `get_option_quotes`, for price history `get_equity_historicals` (or
  `get_stock_historicals`), and `get_earnings_results`. The last is read by the
  check command only, so its layout is known before the app relies on it. The
  list is fixed in `tradeassist/providers/robinhood.py`. Robinhood's server
  offers 84 tools in all, including ones that place and cancel orders, exercise
  options, and read your accounts, positions and orders. None of those is on the
  list, so the app cannot call them. A listed tool that Robinhood itself marks
  as changing something is refused as well. The tests run the app against a
  stand-in server stocked with order tools, account tools and look-alike names
  and confirm none is ever called.
- If Robinhood names one of those tools differently, that step of the check
  fails and says which similar names the server has. The app does not guess: a
  name is only added to the list by hand, after reading what the tool does.
- **The sign-in may permit more than the app uses.** Robinhood decides what its
  approval page grants, and that may include trading in the separate account it
  sets up for connected programs. The app's code cannot use that, but the
  sign-in file could be used by another program that got hold of it. So: read
  Robinhood's approval page before you agree, do not fund or enable trading for
  that separate account unless you want it for something else, never share
  `data/robinhood_signin.json`, and run `python run.py --robinhood-signout` (then
  disconnect the app in your Robinhood settings) when you stop using this. The
  check prints what Robinhood says the sign-in permits.
- The sign-in file is readable only by your user account. It holds a permission
  token, not your password. An expired sign-in is renewed without a browser
  when Robinhood allows it.
- The running app never opens a browser. If the sign-in is missing or can no
  longer be renewed, the screens say so and ask you to run the check again.

**How far this has been checked against Robinhood's real server.** Robinhood
does not publish its request and reply formats, and the build environment cannot
reach it, so the connection is being matched to the real server from the capture
files the check writes. As of the capture of 6 October 2026:

| Part | Status |
|---|---|
| Sign-in, and the list of 84 tools with the arguments each accepts | Seen on the real server |
| Stock quote reply | Seen; the app reads it (regular and after-hours last trade, bid, ask, their times, prior close) |
| Option expirations reply, including Robinhood's stated price steps | Seen; the app reads it |
| Price history request (full start and end times) | Corrected to what the server asked for; the reply has not been seen |
| Option contracts, option quotes and Greeks, earnings replies | Not yet seen: written from the two replies that were seen, and may need another adjustment |

The connection reads each tool's own description to decide how to call it,
accepts several spellings of each field, and stops with a clear message instead
of guessing when something does not look right. If the check shows a FAILED
step, send the file it writes, `data/robinhood_capture.json`. That file holds the
server's tool names and argument names and a few sample market-data replies.
Email addresses, account references, token-like text and values under keys that
could identify a person are filtered out before it is written, but a filter is
not a guarantee: read the file before sending it. Run the check during market
hours at least once: after the close some quotes are empty or stale.

Safeguards against showing wrong numbers:

- A quote is matched to its contract by Robinhood's contract id, never by its
  position in a reply.
- Only standard 100-share contracts are used. Contracts adjusted after a split
  or merger are left out, and if the app cannot tell the two apart it stops.
- Robinhood's implied volatility and Greeks are compared across each chain with
  the app's own calculation. If they are in other units (theta per year, say),
  they are set aside, the app's own are shown, and a warning says so.
- If Robinhood gives no time for a quote, the pricing screen says its age is
  unknown instead of presenting it as fresh.
- Price history must be one bar per day, on weekdays. Anything else is refused.
- Valid price increments come from Robinhood's own statement for the option
  class (for example $0.01 under $3 and $0.05 above), unless a live quote
  contradicts it, in which case they are worked out from the quotes as before.
- The stock price used is the newer of the last regular-hours trade and the last
  after-hours trade, as Robinhood's notes advise.

Other things to know:

- Before trusting a limit price, compare one contract's bid, ask and delta in the
  app with the Robinhood buy screen.
- Live updating asks for a new quote every 3 seconds by default
  (`ROBINHOOD_POLL_SECONDS`). Robinhood does not publish a request limit; if you
  see errors about too many requests, raise the number.
- Daily price history for charts, the pattern lab and backtests comes from
  Robinhood when it supplies at least 80% of the days asked for, and otherwise
  from Tradier if a Tradier token is in `.env`. The check tells you which one the
  app will use, and the terminal log notes each time Tradier is used instead.
- Without a Tradier token the app tells market hours from the clock and does not
  know market holidays.
- Every listed strike of an expiration is loaded and offered, as on Robinhood's own
  screen. Only an unusually long ladder (over 600 contracts) is cut to strikes
  within 50% of the stock price.
- Earnings dates, news and your positions are not read from Robinhood.

## How to use it with Robinhood

1. Open the option you are considering in Robinhood.
2. In **Price an option**, pick the same ticker, expiration and strike. Check that the
   bid and ask match what Robinhood shows.
3. Read the three suggested prices and the walk plan. Start at the patient price;
   if it has not filled in about 25 seconds, edit the order to the next price; stop
   at the cap.
4. Read the risk checks. Red flags are reasons to skip or fix the trade.
5. Fill in the **trade ticket**: starting price, the most you will pay, exit rules and
   a one-line thesis. Save it to the journal.
6. Place the order in Robinhood. In **Journal**, log each price you tried and whether
   it filled. After ten or so attempts in a zone the app shows your own fill rates.

## The screens

**Price an option.** The limit-price ladder sits on valid price increments, which
are worked out from the quotes in the chain (penny classes: $0.01 under $3 and $0.05
above; other classes $0.05 and $0.10; a few ETFs $0.01 everywhere). A model value
is computed from the volatility of neighbouring strikes, so a stale quote on one
side shows up as a midpoint that disagrees with the model, and the patient price
starts from the model instead. Nobody can tell you the chance that a price inside
the spread fills; the app does not pretend to, and uses your logged attempts instead.

Under the form a line says which ticker, expiration and type the strike list
belongs to and when it was loaded, and shows a spinner while a new one is being
fetched. While the ticker box holds a symbol that has not been loaded, the lists
are greyed out and the line says so.

**Live updating.** After you press Price it, the limit section keeps itself current:
once a second it fetches the newest quote for that one contract and the stock in a
single small request and rebuilds the ladder. It shows the time each side of the
quote last changed and the time it was fetched, to the millisecond, in New York
time. The times come from the data source, so they are as fresh as that source:
real-time with a live Tradier token, 15 minutes late with a sandbox token (updated
every 3 seconds, within the sandbox's request limit), whatever Robinhood reports
when it is the data source (every 3 seconds by default), and made up in demo mode. It
pauses when the tab is hidden and after 15 minutes. The risk checks are not re-run
on each update; press Price it again for those. This is polling, not a pushed
stream: a quote can be up to a second old when you read it.

**Risk checks.** Spread, open interest, volume, implied against actual volatility,
implied-volatility rank, earnings before expiration, daily time decay, days left,
move needed to break even against the move the market is pricing, trend indicators,
and position size against your limits. Thresholds are in `tradeassist/scorecard.py`.

**Trade ticket.** Sized and checked at the most you are willing to pay. Limits:

| Limit | Can be overridden? |
|---|---|
| Premium per idea (all tickets in one stock) above `MAX_RISK_PER_TRADE_PCT` | No. Reduce size or change `.env`. |
| Total premium in open trades and saved plans above `MAX_OPEN_RISK_PCT` | No |
| Limit price above the ask; missing thesis | No |
| Spread too wide, too few days left, earnings before expiration, daily loss stop, an entered earnings date that contradicts past filings | Yes, by ticking a box. The override is recorded and the journal reports results with and without overrides. |

The journal records what actually happened at the broker. If you log a fill above
your plan it is accepted and marked as a broken rule; it is not refused.

**Simulate a trade.** What a position could be worth at different stock prices, on
any day up to expiration, and if implied volatility rises or falls. One to four
legs on the same stock, bought or sold, so spreads, straddles and condors work. Open
it from the Simulate screen, or press **Simulate this trade** after pricing an
option, which carries over the contract and the balanced limit price.

- The chart shows profit and loss against the stock price on the chosen day and at
  expiration, the breakevens, today's price and the typical range by expiration.
- Move the date slider to see time decay, and the volatility slider for scenarios
  such as the drop in implied volatility after earnings (try -10 to -20 points).
- The grid shows profit and loss for a range of stock prices on several dates up to
  expiration, blue for profit and red for loss.
- Also shown: the cost, the most you can lose and make, the breakevens at
  expiration, the position's delta, theta and vega today, and the chance of profit
  that option prices imply.

Each leg is valued with the same model as the pricing screen, at the volatility its
own strike trades at. The price used for each leg is the midpoint unless you enter
your own; after the first run it is kept fixed, so moving the sliders never moves
the starting point. The chances are what option prices imply (a lognormal spread at
the at-the-money implied volatility), not a forecast, and they understate the
chance of large gaps such as earnings moves. Early exercise, assignment and fees
are not modelled, and dates skip weekends but not market holidays.

**Research a stock.** Price against its 50- and 200-day averages, 52-week range,
actual and implied volatility, market trend (SPY), indicator readings, recently
completed patterns with their lab verdicts, SEC filings with links, reported
quarterly results, and an estimated next earnings date. Not included: news, analyst
estimates, written summaries of filings.

The **chart** on this screen shows daily candles, or a closing line for a year or
more. It can also show:

- the 20-day, 50-day and 200-day averages;
- support (S) and resistance (R) levels;
- swing labels and trendlines;
- MACD (12, 26, 9) in its own panel;
- a strip coloured by the trend structure on each day.

The tooltip shows every value for the day under the pointer. On a phone the chart
scrolls sideways.

**Price structure** reads the swings:

- **Uptrend:** higher highs and higher lows.
- **Downtrend:** lower highs and lower lows.
- **No clear trend:** the swings are mixed, or level with the ones before.

It also says what would break the trend, such as a close below the last higher low.

- **Swings.** A swing is a move of at least 2 times the average true range (ATR), so
  a quiet stock needs a smaller move than a volatile one. Choose 1.5 or 3 ATR for
  smaller or larger swings. A swing high is recognised only once the price has
  fallen that far from it, so the label appears a few days after the high itself.
  The structure on any past day uses only swings already recognised by then.
- **Support and resistance.** Levels come from the swings of the last year. Swings
  within half an ATR of each other count as one level, and "×2" means two swings
  formed it. The nearest three above the price are resistance and the nearest three
  below are support.
- **Trendlines.** A trendline runs through the last two swing lows when they rise,
  or the last two swing highs when they fall.

The swing structure also counts in the pricing screen's trend check.

**Pattern lab.** Tests 20 patterns at several holding periods. Four of them are swing
structure events: swings turning to higher highs and higher lows, swings turning to
lower highs and lower lows, an uptrend closing below its last higher low, and a
downtrend closing above its last lower high. Each is dated on the day it became
known, not on the day of the swing. For each past
occurrence it enters at the next open and compares the result with what the other
stocks in the run did over the same days. A pattern is called supported only if it
beats chance in the first 60% of the history after correcting for the number of
tests, does so again in the held-out final 40%, and makes money after costs. Use at
least five symbols; fewer get numbers against SPY but no verdict. On data with no
real effects it gave a false verdict in roughly 1 to 2% of runs in testing.

**Backtest.** Runs the original scanner's 52-week mean-reversion score over history
with next-day entries, stops, targets and costs, against buy-and-hold. The
walk-forward test tunes settings on one year, trades the next quarter with them,
and repeats; only the held-out quarters are reported.

**Journal.** Planned, open and closed trades; limit attempts and fill rates by how
far across the spread you bid; results by pattern present at entry; results with
and without rule overrides.

## Version 2

Six screens are new or extended. Each part can be switched off in `.env` (see the end of
`.env.example`); the classic screens work as before.

**Analyze a stock.** The trend engine gives one of seven states (strong uptrend to strong
downtrend, with "uptrend weakening" and "downtrend developing" between), the bullish and
bearish evidence behind it point by point (swing structure, moving averages, momentum,
strength against SPY and the stock's sector fund, volume, and patterns that passed the
pattern lab), how long the state has held and how much confidence it deserves. Scores are
evidence points, not chances. While the market is open, today's bar is still forming: the
reading from it is marked provisional and the confirmed reading from yesterday's close is
shown beside it. Below that: the market regime and what RSI, the 52-week range and the
Bollinger bands usually mean in it; the signals of the last ten days with how often they
worked before (once the signal history is built); grading a stock trade, long or short,
from A to AVOID with suggested invalidation, targets and size; and choosing a call or put
for the thesis, with the best match, cheapest, best risk-adjusted and most conservative
contract and the ones to avoid.

**Trend reversals (2.1).** On the Analyze screen, a section reads both sides at once: is a
fall ending in a bottom, or a rise in a top? Exhaustion (a long move, RSI at an extreme, a
stretch from the 20-day average, divergences, a climax day, reversal candles) and
confirmation (a higher low, a close above the last lower high, the trend engine turning, the
averages taken back, volume) are scored out of 100 points, and each side gets a stage: no
move to reverse, stretched (watch), potential bottom or top, confirmed, or failed. A true
bottom or top is only known afterwards, so these are signs, not certainties; the screen
shows where the idea is wrong, the targets, a year of past stages, and how often each
reading worked before on this stock and across the signal history. Below it, "Score a call
or put for this reversal" values calls (for a bottom) or puts (for a top) with those levels,
scores each out of 100 as GOOD, FAIR, POOR or AVOID with the reasons, and for the best one
(or your own contract) shows the gain or loss at each target, with no move and at the stop,
a full simulation, and what it would have made after the past cases of the same reading.
The score is a summary of checks, not a probability. "Reversal scan of the watchlist" at the
top of the screen lists every stock's reading, strongest first.

**Positions.** Record what you did at your broker: stock long or short, calls and puts,
with every fill. Average cost, partial exits, fees, realized and unrealized profit and R
multiples come out of the fills. Each position keeps a snapshot of the market at entry and
exit. Option-journal trades appear here automatically. Alerts at the top say what needs a
look (a stop level crossed, a target reached, the trend turning against you, an option
about to expire, earnings coming, the daily loss stop). Alerts only: nothing is ever
bought or sold.

**Portfolio risk.** Exposure in stock dollars and in SPY terms, Greeks, risk to each
position's invalidation price against your limit, sector concentration, how the holdings
move together, a bad-day estimate, stress tests (including your own), and where today
stands against the daily loss stop. `DAILY_LOSS_MODE` decides whether open losses count
toward that stop (they do by default, which is the safer choice).

**Journal** adds analytics across all closed positions: win rate, profit factor,
expectancy in dollars and R, drawdown, the best and worst the stock moved while each trade
was open, results by setup, trend and regime at entry, and what habits such as holding past
the stop have cost.

**Pattern lab** adds the signal history: five years of every pattern and trend change on
your watchlist with what followed, used for live signals and to check whether the trend
score is calibrated (it says plainly when it is not).

**Backtest** adds condition strategies: rules on indicators, the trend state and the
market regime, with shorts that pay a borrow fee, walk-forward testing and a Monte Carlo
range. Option strategies are not backtested, because no connected source has past option
prices and faking them would only look precise.

**Data health (2.3).** Whether the numbers on the other screens can be trusted: the data
source and its recent failures, how fresh each open position's price is and which could not
be valued, the saved daily history of the stocks you hold and watch, missing or hindsight
snapshots (with a button to rebuild them from the saved history), the schema version and
the copy taken before the last upgrade, and recent problems from the log. "Show the
reconciliation report" checks every position's stored figures against the fills they come
from, and lists positions reopened by older versions, fills that look duplicated, journal
entries that differ from their position, expired options still open and SQLite's own
integrity check. It only reads. When a figure is wrong, "Repair from the fills" says what it
will change, copies the database, and recomputes the figures; it never changes, merges or
deletes a fill you recorded (an option-journal trade's fills are re-read from the journal,
which is their record, and each change is kept as a correction).

**Trade review (2.3).** A closed position's analytics end with what was planned, what was
done, what was known at entry (from a snapshot taken at the time or from the prior day's
close, never from later data), whether the thesis was invalidated or the targets reached,
the result after fees and borrow costs, and what is missing. It describes what happened; it
does not claim to know why.

The database is upgraded in place the first time a new version starts. Since 2.1.1 the app
copies it to `data/backups/` before any upgrade (the newest ten copies are kept), so there is
always a way back.

## Implied-volatility rank

The rank needs a daily record of each stock's implied volatility. The app records
it the first time you price a symbol on a trading day. To build history for the
whole watchlist, run this once a day after the open (for example from cron):

```bash
python run.py --snapshot
```

The rank appears after 60 recorded days.

## What was checked, and what was not

Checked by the automated tests (`python -m pytest`) and an independent review:

- Option prices, Greeks and implied volatility against textbook formulas and scipy.
- RSI, ATR and ADX against independent implementations of Wilder's definitions.
- Every suggested price is a valid increment inside the quote, for buys and sells,
  across $3.00 (exhaustive over about 360,000 cases in review).
- No indicator, pattern, signal, backtest or walk-forward step uses future data.
- Backtest accounting: final balance equals starting capital plus the sum of trade
  results, for longs and shorts, with costs.
- The pattern lab on pure-noise data, and on data with a planted effect.
- The simulator against an independent Black-Scholes calculation, about 9,000 random
  legs with no difference above 0.0000001. The maximum gain, maximum loss and
  breakevens at expiration were exact on 20,000 random positions, and the chances
  matched closed-form results.
- The swing structure has no look-ahead. On 240 random price histories with gaps
  and flat stretches, cutting the history at any day never changed anything computed
  for the days before. Every structure event falls on the day its swing was
  recognised.

- Since 2.1.1 (details and test names in `docs/IMPROVEMENTS.md`): a closed position cannot
  be reopened; unknown prices, risks and losses are never counted as zero; the ledger replays
  random fill sequences (property tests) to the same figures; databases from 0.4.0, 2.0.0
  and 2.1.0 upgrade with their records intact; snapshots never use data after their fill;
  betas, correlations and bad-day figures skip missing days instead of filling them; borrow
  fees and dividends stay apart from trading profit; signal intervals allow for overlapping
  cases; the same research inputs give the same results; reconciliation finds a planted
  mismatch, repair fixes it after a copy without touching a fill, and running it twice
  changes nothing; the log and the data health screen never show a credential.
- 2.3.1: two independent reviewers checked 2.1.0 to 2.3.0 for accounting, data loss,
  look-ahead and security; the 27 findings were reproduced, fixed and given regression tests
  (`tests/test_review_fixes.py`; listed in `docs/IMPROVEMENTS.md`).

Not checked, because this build environment could not reach any of these services:

- **Robinhood.** The connection, the sign-in and the check command were tested
  end to end against stand-in servers written for the tests, including one that
  requires the same kind of browser sign-in. An independent review then tried to
  make the app call an order or account tool; the weaknesses it found were
  fixed, and on its second attempt it could not. One stand-in copies the argument layouts and the two replies seen
  on Robinhood's real server; the replies not yet seen (see the table above) are
  unconfirmed, as are the units of its Greeks and its request limits.
- **Live Tradier calls.** The client is written to the documented reply formats and
  tested against them, but has not been run against the real API. The first thing
  to do is compare one option's bid, ask and Greeks with what Robinhood shows.
- **Live SEC calls.** Same: tested against the documented format only.

Known limitations:

- Model values use a European formula. Early exercise is ignored, which matters for
  deep in-the-money puts and calls just before an ex-dividend date. Deep
  in-the-money contracts are flagged.
- Dividends are not looked up; the forward price is implied from put-call parity.
- The earnings date is an estimate from past filing dates unless you enter the
  confirmed one. It errs toward expecting a report. For very heavy SEC filers such
  as large banks the filing list may not reach back far enough, and the date shows
  as unknown.
- Studies and backtests use today's symbol list, so failed and dropped companies
  are missing (survivorship bias), which flatters results.
- Price history is not checked for dividend adjustment. One-day moves over 40% are
  flagged as possible unadjusted splits.
- Tick sizes are inferred from quotes. If no quote in an expiration proves a finer
  increment, the coarser one is assumed and the app says so.
- The backtest covers the stock strategy, not options. There is no option backtest.

## What changed from the earlier project files

The scoring in `tradier_backend.py` is kept (see `tradeassist/signals.py`), with
these corrections:

- The backtest command printed fixed example numbers. It now runs on real signals.
- Short trades lost money when the price fell. Fixed.
- Equity dropped to cash on weekends and the Sharpe ratio counted calendar days.
  Only trading days are simulated now.
- RSI and ADX used rolling averages, so they did not match broker charts. They now
  use Wilder's smoothing.
- `BRK.B` is sent to Tradier as `BRK/B`.
- Signals at a day's close are traded at the next open, not the same close.

## Files

```
run.py                     start the app, record the daily snapshot, or check Robinhood
update.sh                  update from GitHub (macOS and Linux)
tradeassist/
  config.py                settings from .env
  providers/               Tradier, Robinhood and demo data, all read-only
    mcp_bridge.py          the Robinhood link: sign-in, and the guard that calls listed tools only
    robinhood.py           the list of callable tools; turns replies into quotes and chains
  robinhood_check.py       the --robinhood-check steps and capture file
  market.py                quotes, cached daily bars, option chains
  optionmath.py chain.py   pricing, Greeks, implied volatility, model values
  pricer.py                tick sizes and the limit-price ladder
  scorecard.py             risk checks
  indicators.py patterns.py signals.py
  structure.py             swings, higher highs and lows, support and resistance, trendlines
  simulate.py              the option simulator: P&L by price, date and volatility
  eventstudy.py            the pattern lab's statistics
  backtest.py              backtester and walk-forward test
  research.py              SEC filings, results, earnings estimate
  journal.py db.py         journal and SQLite storage
  service.py api.py        the operations behind each screen, and the HTTP API
  features.py              version 2 feature switches (FEATURE_... in .env)
  migrations.py schema_v2.py   numbered database upgrades
  positions.py desk.py     unified positions: accounting from fills, marks, snapshots
  snapshots.py             lifecycle snapshots: when and how each was made, rebuilding
  freshness.py             how fresh and complete each price is; data-source health
  reconcile.py             stored figures checked against the fills; repair after a copy
  runs.py                  settings and data fingerprints of research runs
  events.py                structured log lines, with credentials redacted
  backup.py                copies of the database before upgrades and repairs
  dataquality.py           checks on daily bars; finished versus unfinished bars
  trend.py regime.py       trend engine and market regime
  reversal.py              potential bottoms and tops, and confirmed reversals
  sectors.py               the sector fund for relative strength
  signalbook.py            signal history and score calibration
  evaluator.py             trade grades for stock ideas
  options/                 the option picker, reversal option scores; historical option data (none connected)
  portfolio.py analytics.py alerts.py   portfolio risk, journal analytics, alerts
  backtest_v2.py           condition strategies, Monte Carlo
  analysis.py api_v2.py    the version 2 operations and the /v2 HTTP API
  versions.py              version labels recorded with every result
  static/                  the web page (app.js classic screens, v2.js version 2 screens)
docs/V2.md                 version 2 in detail: rules, formulas, database and API
docs/IMPROVEMENTS.md       the 2.1.1 to 2.3 engineering record: defects found, tests, reports
docs/RELEASE_NOTES.md      what changed in each release, and how to upgrade
tests/                     automated tests
```

The app listens on 127.0.0.1 only, so it is not reachable from other machines.
