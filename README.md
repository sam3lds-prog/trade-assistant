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

Version 2.4 adds options analytics and AI help. The **Options lab** shows what option prices
imply (implied volatility by expiration and strike, the expected move, the straddle, skew,
the move priced for earnings, implied against realized volatility) and checks any contract
with a seeded Monte Carlo (value against Black-76, the chance of finishing in the money and of
touching the strike, the spread of outcomes for a buyer). The **Scanner** finds RSI, MACD,
moving-average, 52-week and volume signals across the watchlist or 100 large companies, each
with how often it worked before (a historical frequency, not a forecast), and options trading
unusually against open interest. The **strategy builder** on the Simulate screen turns "AAPL
iron condor 45 days 16 delta 5 wide", or a picked template (spreads, straddles, strangles,
condors, butterflies, calendars), into real listed contracts with the debit or credit, the
most it can make and lose, breakevens and a risk-limit check. **Market & news** shows the
market's make-up (indexes, VIX, regime, sectors, breadth, put/call) and headlines with their
tone, and, with an Anthropic API key, Claude writes market and stock briefs that pull in the
latest news with their sources. Everything is modelled only: the app still places no orders.

Version 2.5 joins the screens into one path, from which way to lean to the limit to type at
your broker. The app opens on **Today**: the market regime, your daily stop and open risk, and
up to eight stocks leaning long and eight leaning short. Pick one and **Analyze a stock**
(Decide) opens with that direction, grades the stock trade and finds calls or puts on the same
levels from one thesis form. It sets the sized shares beside the best risk-adjusted contract:
what each costs, makes at the target and loses if wrong, the chance option prices imply, and
the middle outcome with the range eight in ten outcomes fall in on your horizon date. The trend chart
then runs on to your horizon date with the levels, the range option prices imply and where
the contract breaks even. **Set the limit** on any contract opens Price an option on it,
already priced on the limit ladder. Scanner rows open Decide too. The menu is grouped Today,
Find, Decide, Price & simulate, Hold, Learn and Trust.

Version 2.6 adds **My trades** (under Learn): your Robinhood history, imported from the
account activity report and analysed on this computer. For any dates you pick it shows:

- the results by week, month, quarter or year;
- the strategies you used and the risk each carried, with the riskiest first;
- open risk over time;
- the kinds of trade that went better or worse than your average.

Every trade opens to the statement rows it was built from, so each figure can be checked
against Robinhood. Optionally, with `ROBINHOOD_ACCOUNT_READ=1`, it also reads your filled
orders and holdings through the Robinhood connection, read-only.

Version 2.13 adds **what-if analysis** under the chart on Analyze a stock.

- **A model you can steer.** Overseas markets (Nikkei, DAX, Hang Seng), the US market, the 10-year yield,
  Brent, the dollar, the VIX, the stock's sector fund and foreign peers you name, each with today's reading
  (real indexes from stooq.com when it answers, the funds that track them otherwise) and this stock's
  sensitivity to it from a year of daily bars; the last 36 hours of news and their tone; analyst upgrades
  and downgrades in a week of headlines; earnings ahead; and the similar-pattern path. Change any value,
  switch any factor off, and the model's path and 10-90 band redraw on the chart in orange.
- **An AI's scenario, in purple.** ChatGPT (new `OPENAI_API_KEY`) or Claude (`ANTHROPIC_API_KEY`), picked in
  the widget, gets every input and the model's path and answers with its own path and band, the variable
  settings it would use, whether to lean on the pattern, a strategy and its reasons. One click applies its
  settings to the model. Both share the daily AI limit.
- **A position under the scenario.** Shares, a short, or any of the sixteen option strategies from the
  builder, valued at the horizon across the scenario's range of prices for the quantity you set: the
  downside and upside (10th and 90th percentile), the expected result, the chance of a profit, the most it
  can lose. Rank every strategy and the best by the model and the AI's pick are marked. Models only.

Version 2.12 makes **Analyze a stock** interactive.

- **The chart** is drawn with TradingView's Lightweight Charts (Apache-2.0, served by the app, no CDN):
  drag to pan, scroll or pinch to zoom, hover for every figure of a bar, click a swing, a news marker or a
  top-or-bottom marker to read it.
- **Any range and grouping.** 1D, 5D, 1M, 3M, 6M, 1Y, 2Y, 5Y, 10Y or custom dates; bars of 1, 5 or 15
  minutes, an hour, a day, a week, a month or a quarter. Bars within the day come from Tradier (about 40 days
  of 5-minute bars, 20 of 1-minute) or Robinhood (a week of 5-minute bars), made up on demo data.
- **Indicators on and off:** candles or a line, volume, 20-bar EMA, 50- and 200-bar averages, Bollinger
  bands, VWAP, support and resistance, swings, trendlines, MACD and RSI panes, news markers, tops and
  bottoms forming.
- **Similar patterns.** Select a window on the chart (or use what is on screen) and the app finds where the
  same shape appeared in this stock's own history, overlays each match on the window at its scale, draws what
  followed each past the last bar, and gives the median path with its middle half.
- **Possible outcomes.** For each horizon, how often the matches went on higher, stayed within one daily
  range, or fell more than one, each with a 95% interval and the count behind it. Beside them: whether a
  top or bottom is forming and how that reading went before on this stock, recent signals' past record,
  and today's move with its news. A lean counts the readings with enough cases; nothing is a forecast.

Version 2.11 finds news by searching, at no cost, so the news checks no longer depend on a paid feed.

- **Google News search** (free, no key, on by default) is searched for each stock by the names news uses
  for it (Google for Alphabet, BofA for Bank of America) and its ticker. A result counts only when its
  headline names the company, so a student-visa story is not Visa news and "UK PM" is not Philip Morris.
  A headline that only says the stock moved ("Why Oracle stock is soaring today") is not counted as news
  behind the move. It feeds the news on Analyze, the Morning plan's ride-or-fade check and Live news.
- **Brave news search** (optional, `BRAVE_API_KEY`) is asked only when nothing else found news. Brave no
  longer has a free plan: its $5 monthly credit buys about 1,000 searches. The app counts each search in
  its database before sending it and stops at 900 in any 31 days (`BRAVE_MONTHLY_LIMIT`), so no billing
  month can pass the credit.
- **"Why is it moving?"** on Live news: every five minutes the app looks for stocks that moved 3% and
  1.5 daily ranges since the close with nothing in the feed, searches the news for them, and lists what
  it found. News found joins the feed and is judged like any other item. A big move with no news found is
  flagged, because those snap back more often.
- **No AI in the loop.** The searches are plain requests the app makes itself, so they cost nothing and
  use no Anthropic tokens.
- **What search cannot do.** A search engine indexes a story minutes after it is published. Search
  explains a move; RTPR's stream is still the source that can come before one.

Version 2.10 changes how the app prices an option you want to buy, and adds **Live news**.

- **A fair price, not the midpoint.** The limit ladder used to start at the midpoint and walk up to 80%
  of the way to the ask, which on a wide option spread paid more than Robinhood fills at. It now centres
  on a fair price worked out from the evidence that is there: the midpoint, the model value from the
  neighbouring strikes, the midpoint weighted by the size waiting on each side, and a trade in the last
  15 minutes inside the quote. A buyer's fair price is never above the midpoint. The walk stops at it,
  and "Most you will pay" defaults to it. If the Robinhood connection sends its own estimate of a
  price likely to fill, it is shown beside the evidence for reference.
- **Morning cards before the open.** Before 9:30 option quotes are the last session's, made when the
  stock stood at its close. When the stock has gapped since, the card now reprices the contract at the
  pre-market stock price instead of using the old midpoint. That old midpoint priced fades well above
  where they open: puts after a gap up, calls after a gap down.
- **Live news** (under Today) listens for stock-moving news and checks the price after each item: press
  releases from RTPR (streamed within about a second, `RTPR_API_KEY`), 8-K filings from the SEC
  (`SEC_USER_AGENT`) and Finnhub headlines (`FINNHUB_API_KEY`).
  - When a stock's price confirms its news, the screen shows an opportunity with the Morning plan's trade
    card: buy shares or calls when the news sends it up, short or buy puts when it sends it down.
  - A stock you hold gets an alert when the news moves it against you, so you can consider selling or
    covering.
  - Optionally, opportunities and alerts arrive by browser notification or email.

Version 2.8 adds the **Morning plan**, the new landing screen. Each morning it reads about 290 large US
stocks plus your watchlist, finds the ones that jumped since the last close, and says which look worth
riding (fresh news behind the move) and which look stretched enough to fade (no news found, already
overextended before the move). Each setup becomes a trade card: entry, stop, targets, shares sized to
your risk, the best call or put the budget pays for, and the exact limit price to type — capped where
reaching target 1 still pays at least what the stop loses. A reversal watch lists stocks about to confirm
a turn and can watch the trigger price for you. Optionally the whole plan arrives by **email every
weekday at 7:00** (`./setup_morning.sh` schedules it on a Mac). Setups and past frequencies, not
forecasts — and nothing places an order.

Version 2.7 added **Trade ideas** (under Find): the 200 largest US companies scanned for the
highest-graded trades to buy and to short, any of which can be simulated on 20,000 price paths following
the trade's own stop and targets.

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
| `FINNHUB_API_KEY` | Optional (free at finnhub.io). Company and market headlines on the Market & news and Analyze screens. |
| `GOOGLE_NEWS` | On unless set to 0. Searches Google News for each stock's news (free, no key; an unofficial personal-use feed, asked once every two seconds at most, by the app and its morning email together). |
| `BRAVE_API_KEY` | Optional (brave.com/search/api; needs a card, $5 of searches free each month, billed past that). Asked only when nothing else found news, and stopped at `BRAVE_MONTHLY_LIMIT` (900 in any 31 days) and `BRAVE_DAILY_LIMIT` (40) so it stays inside the credit. |
| `RTPR_API_KEY` | Optional (rtpr.io; the paid plan, about $139 a month, streams press releases within about a second, and the free plan is five minutes late). The fastest source for Live news. In RTPR's dashboard, create the "All articles" rule: the stream sends what your rules match. Never sent anywhere but rtpr.io, and never logged. |
| `ANTHROPIC_API_KEY` | Optional. Lets Claude write market and stock briefs with web sources, read headline tone, and read trade descriptions. Billed by Anthropic to that API account; capped by `AI_DAILY_REQUESTS`. Never sent to the browser or logged. |
| `SMTP_USER`, `SMTP_PASSWORD` | Optional. The 7:00 morning email and trigger alerts. With Gmail, an app password from myaccount.google.com/apppasswords (2-Step Verification must be on). Any SMTP provider works through `SMTP_HOST`/`SMTP_PORT`; `EMAIL_TO` if the destination differs. The password never leaves `.env` except to the verified mail server. |

With no token the app runs on made-up demo data so you can learn the screens.
Demo data is stored in a separate database file and never mixes with real history.

## Keep it up to date from GitHub

If you installed the app by cloning its GitHub repository, update it with one
command and then restart it:

```bash
cd ~/projects/trade-assistant
./update.sh
```

It downloads the newest version, installs any new packages, and lists what changed. If a review copy
sits beside the app as a git worktree at `../trade-assistant-review` (for a read-only code review), the
script then moves it to the same version, but only when it is clean, and says when it skipped. Quit any
review session first.
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
- Earnings dates and news are not read from Robinhood. Your past orders and holdings are
  read only if you set `ROBINHOOD_ACCOUNT_READ=1` (for My trades; see below).

**Reading your orders and holdings (2.6, optional, off by default).**

- **Switching it on.** With `ROBINHOOD_ACCOUNT_READ=1` in `.env` (and `DATA_MODE=robinhood`),
  My trades can read your filled orders and current holdings through the connection.
- **What it may call.** Only tools named exactly `get_equity_positions`,
  `get_stock_positions`, `get_option_positions`, `get_equity_orders`, `get_stock_orders` and
  `get_option_orders`. Balances, account numbers, bank links, documents and every tool that
  acts are still refused by name.
- **What counts.** Only filled quantity counts. Days your imported report covers use the
  report, which has the fees.
- **Option orders may be missing.** Robinhood's connection may offer share orders only. The
  account activity report has your option trades.
- **The check.** Run `python run.py --robinhood-check` with it on, and the check records the
  shape of each account reply (field names and types, never values) so the reader can be
  matched to it.

## How to use it with Robinhood

1. Open the option you are considering in Robinhood.
2. In **Price an option**, pick the same ticker, expiration and strike. Check that the
   bid and ask match what Robinhood shows.
3. Read the fair price, the evidence behind it and the walk plan. Start at the patient
   price; if it has not filled in about 25 seconds, edit the order to the next price;
   stop at the fair price. The fast price, past it, buys speed, not value.
4. Read the risk checks. Red flags are reasons to skip or fix the trade.
5. Fill in the **trade ticket**: starting price, the most you will pay, exit rules and
   a one-line thesis. Save it to the journal.
6. Place the order in Robinhood. In **Journal**, log each price you tried and whether
   it filled. After ten or so attempts in a zone the app shows your own fill rates.

## The screens

**Price an option.** The limit-price ladder sits on valid price increments, which
are worked out from the quotes in the chain (penny classes: $0.01 under $3 and $0.05
above; other classes $0.05 and $0.10; a few ETFs $0.01 everywhere).

Since 2.10 it centres on a **fair price**: the middle (median) of the evidence that is
there. The box under the ruler lists each piece, used or not, and why.

| Evidence | Used when |
|---|---|
| Midpoint | always |
| Model value (the contract at the volatility of neighbouring strikes) | it sits inside the bid and ask |
| Size-weighted midpoint | both quote sizes are given and the spread is wider than one price step |
| Last trade | it printed inside the quote within 15 minutes, while the market is open |
| Robinhood's own fill estimates | never: shown for reference, because how Robinhood works them out is not published |

- **Which way the evidence can move it.** A buyer's fair price is never above the midpoint and a
  seller's never below: the evidence can lower what you pay, never raise it.
- **The three prices.**
  - The **patient** price sits 40% of the way from the fair price back to your own side of the quote.
  - The **fair** price is where the walk stops.
  - The **fast** price, 30% of the way on toward the far side, is there for when a fill matters more
    than the cents.
- **What changed from 2.9.** The ladder used to run from the midpoint to 80% of the way to the ask.
- **A stale quote** on one side shows up as a midpoint a quarter of the spread or more from the model.
  Then the midpoint, and the size-weighted midpoint built from the same quote, are left out, and the
  model value (with a recent trade) carries the fair price.
- **No fill chances.** Nobody can tell you the chance that a price inside the spread fills; the app does
  not pretend to, and uses your logged attempts instead.

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

## Live news (2.10)

Under Today. Press **Start listening**. The app then follows every news source that is set up, in the
background, and keeps doing so while it runs. It remembers that it was on and resumes after a restart.

**Sources.**

| Source | What | How fast | Setting |
|---|---|---|---|
| RTPR | press releases from Business Wire, PR Newswire, GlobeNewswire and AccessWire | pushed over a WebSocket, about a second after the wire (paid plan) | `RTPR_API_KEY`, plus the "All articles" rule in RTPR's dashboard |
| SEC EDGAR | 8-K filings: earnings releases, deals, guidance, executive changes | read once a minute; usually minutes behind the wire | `SEC_USER_AGENT` (free) |
| Finnhub | headlines tagged with a ticker, general and for your watchlist | read once a minute | `FINNHUB_API_KEY` (free) |
| Google News (2.11) | search results for your holdings, then your watchlist, four stocks every five minutes (every 30 while the market is closed; the last six hours), and for movers with no news | minutes behind publication: a search engine has to index the story first | on unless `GOOGLE_NEWS=0` (free) |
| Brave (2.11) | news search, only for a mover that nothing else found news for | as Google News | `BRAVE_API_KEY`, capped by `BRAVE_MONTHLY_LIMIT` and `BRAVE_DAILY_LIMIT` |
| Demo | made-up items whose made-up price moves let you learn the screen on any day | every 35 seconds | demo data only |

**What happens to an item.** Each stock it names that is in the universe is quoted when the item arrives,
then 1, 3, 5, 15, 30 and 60 minutes later. By default the universe is the ~330 large stocks the Morning
plan reads, plus your watchlist and anything you hold; `LIVE_UNIVERSE=all` admits any ticker at $5 or more
that trades at least $20 million a day. A second item on the same stock within 30 minutes joins the
first. Outside regular hours a stock is priced only from a trade in the last 15 minutes. With the market
closed, items are collected and the stocks are priced when trading resumes (pre-market included). An item
over 18 hours old by then is listed, not traded.

**When a move confirms.** One of two rules has to hold:

- **The reaction.** Since the item, the stock has moved at least `LIVE_MIN_REACTION_PCT` (1%) and
  `LIVE_MIN_REACTION_ATR` (0.3) of its own daily ranges, with the day's move the same way.
- **The day's move, for news that arrived late.** When the item reached the app two minutes or more after
  it was published, the day's move is at least `LIVE_MIN_DAY_ATR` (1.5) ranges and `LIVE_MIN_DAY_PCT`
  (3%).

The trade is the way the price went. Moves with news have tended to keep drifting, which is the Morning
plan's "ride" setup run in real time. The headline's tone (RTPR's impact direction when it sends one,
otherwise the word list) is shown, and a tone that disagrees with the price is flagged: the price is
what is traded.

**The opportunity** carries the Morning plan's trade card:

- the entry at the current price;
- the stop a quarter of a daily range beyond where the stock stood when the news arrived (back there,
  the news has been undone), or the chart's level, kept within 2.5 ranges;
- targets at the chart's levels at least one risk away, otherwise two and three times the risk;
- shares sized to your risk per trade;
- the best call (long) or put (short) the budget pays for, at its fair price, with the order to copy.

**After it confirms.** The opportunity is checked against its stop every ten minutes for an hour. A price
through the stop at one of those checks marks it **stopped**. After the hour nothing watches it: press
**Watch the stop** on the card (a Morning plan price watch, emailed when crossed) or set your own stop
order.

**Ending by age.** A watched stock that has not moved enough within 90 minutes ends as **no reaction**,
so nothing is ever judged across a closed market.

**Why is it moving? (2.11).** Every `LIVE_MOVER_SWEEP_MINUTES` (5) minutes during regular and extended hours,
the app quotes the universe and lists the stocks that have moved at least `LIVE_MIN_DAY_PCT` (3%) and
`LIVE_MIN_DAY_ATR` (1.5) daily ranges since the last close. For each one with nothing in the feed it
searches the news, six stocks a scan, and a stock where nothing was found is searched again after an hour.

- **News found.** The headline is shown with where it was found, and it joins the feed as a late item, so
  the day's-move rule above can turn it into an opportunity.
- **No news found.** Said in so many words. A big move with no news found can snap back (the Morning
  plan's fade), so check it before riding it.
- **News not ruled out.** Headlines came back that may be about the stock but could not be tied to it, or
  that only say it moved. A link opens them on Google News to read.
- **Brave** is asked at most once a stock a day here, and half of `BRAVE_DAILY_LIMIT` is kept for the
  Morning plan.
- **Analyze** opens the stock in Decide, leaning the way it moved.
- The list is the day's: it starts empty each morning. The searches run on their own thread, so a slow
  search never delays the price checks.

**One move, one opportunity.** A stock that already had an opportunity today gets no second one from a
later story about the same move: search results and late headlines keep arriving for hours after a big
move, and each would otherwise read as news behind it. The later story is listed, not traded.

**After the close.** Once the session has closed (4 p.m., or 1 p.m. on an early-close day), the day's move
is measured from today's close, not yesterday's, so after-hours news is judged by what the stock did after
the session.

**Stocks you hold** get an alert: **against** you (consider selling, covering or tightening the stop) or
**with** you.

**Being told.** **Notify me in this browser** shows a desktop notification for each new opportunity
while the screen is open. `LIVE_NEWS_EMAIL=1` emails them, using the morning email's SMTP settings, at
most 20 a day.

**What it is not.** Large trading firms read the same wires with machines in microseconds, so the first
move on a big release is usually over before anyone can click. What this gives you is the news and the
price's answer to it within seconds, with the risk worked out:

- for releases outside market hours;
- for stocks that take longer to absorb news;
- for news on stocks you hold.

Nothing here places an order. Switch the screen off with `FEATURE_LIVE_NEWS=0`.

## Version 2

**Morning plan (2.8).** The landing screen. Switch it off with `FEATURE_MORNING=0`.

1. Press **Build the plan** (or let the 7:00 email run it). About 290 large optionable US stocks plus
   your watchlist are read. A move counts when it is at least 3% and two of that stock's own daily
   ranges, so 4% in a quiet stock counts for more than 4% in a volatile one.
2. **Ride or fade.** A mover with fresh news (headlines or SEC filings within 36 hours) is read to keep
   going; a mover with no news found that was already overextended before the move is read to snap back.
   Since 2.11 the check also searches Google News, and Brave when it is set up and nothing else found news
   in the 36 hours. Headlines that came back but could not be tied to the stock, or that only say it moved,
   leave the news *not ruled out*, so the fade does not apply. News that could not be checked says *not checked* — it is never treated as "no news". Each setup shows
   its five-year past record on these stocks: how often the trade's way was ahead after the horizon.
   Frequencies, not forecasts, and history cannot say which old moves had news.
3. **The trade card** has the whole plan: entry, stop, two targets, shares sized to your risk per trade,
   the best call or put the budget pays for, and the limit price to type — the quote midpoint, never
   above the cap where reaching target 1 still pays at least `MORNING_MIN_PAYOFF` (default 1.0×) what the
   stop loses. One line is ready to copy into Robinhood. At 7:00, option prices are the previous close
   (options trade 9:30–16:00 New York time) and the card says so; **Price it live** reprices after the
   open, and a 7:45 second email can be scheduled.
4. **Reversal watch.** Stocks about to confirm a bottom or top are listed with the trigger price. Press
   **Watch** and the app checks about once a minute while the market is open, then emails you when it
   crosses (watches survive restarts; at most 40 at once).
5. **The scorecard** grades each sent pick against the next closes (the pick day's close is the first),
   the trade's way. Option picks are graded by their stock plan: no connected source has past option
   prices, and the scorecard says so.
6. **The email.** `./setup_morning.sh` schedules every weekday at 7:00 in this computer's own time zone
   (macOS launchd; `--also-45` adds a 7:45 run at live prices; `--remove` undoes it; Linux gets the cron
   line). The Mac must be awake — the script prints the `pmset` line that wakes it. Try it without
   sending: `.venv/bin/python run.py --morning-print`.

**The trade card on Analyze a stock (2.9).** After **Grade and find a contract**, your thesis gets the
same card the Morning plan builds: the best contract for your risk per trade, the capped limit price, the
order to copy, **Watch the stop**, and **Email me this analysis** — the trend reading and the card sent to
your inbox, built on the server from your own levels.

**Trade ideas (2.7).** Under Find. Switch it off with `FEATURE_TRADE_IDEAS=0`.

1. Pick the stocks: the 200 largest US companies (a fixed list, not updated automatically), the 100
   large ones, or your watchlist. Press **Scan**. The scan runs in the background with a progress bar and
   can be stopped.
   - On demo data the 200 take about two minutes.
   - With live data, the first scan of the day also fetches each stock's price history, spaced out so the
     other screens keep working; that takes several minutes. Later scans reuse it.
   - The last result is kept, also across restarts.
2. **Highest-graded trades to buy** and **to short**. Each stock's side comes from its trend evidence, as
   on Today, and is held back the same way. The trade on that side is graded as on Decide: entry at the
   current price, a stop and two targets from the chart, and shares sized so the stop costs at most your
   risk per trade (never more than the account). Ranked by grade, then score, then reward to risk. "If
   target 1 is reached" and "If stopped" are the sized trade's dollars at those prices, not what to
   expect. Held-back, AVOID and unread stocks are listed below with the reason.
3. **Simulate** on any row, or type your own levels. The trade is walked over 20,000 price paths to the
   horizon (20 trading days unless you change it), at the at-the-money volatility option prices imply
   (or the stock's own when there are none). On each path it exits at the stop or the target, whichever
   the price touches first, or at the close on the horizon date. Exit plans: all at target 1, all at
   target 2, or half at each.

   The simulation shows:
   - how often each way out happens and what each makes or loses;
   - the price's range day by day, with a few paths;
   - the spread of results with shares, and with the best call (buy) or put (short) your risk per trade
     pays for.

   The contract is valued at its own implied volatility, on the same exits. Averaged over every path a
   trade comes out close to break-even: what is left is the carry in the forward price, not an edge. The
   model has no view on direction, so any edge is your own view of why the target is likelier than that.
   Fills are at the levels; a gap through the stop costs more, and a short's borrow fee is not included.
4. From a result: **Set the limit** on the contract, **Simulate the contract alone**, or **Open the
   thesis on Decide** with the same levels.

**My trades (2.6).** Your Robinhood trading history, analysed. Switch it off with
`FEATURE_TRADE_HISTORY=0`.

1. In Robinhood, open Reports and statements > Account activity report > Generate new report.
   Pick the account and the dates; from when you started trading is best. Robinhood says it
   takes about 2 hours, and up to 24.
2. Download the CSV and import it on My trades. A later report adds only the rows not already
   there.
3. Pick the dates: a preset (the last 30 or 90 days, this year, the last 12 months, last year,
   all history) or your own. Then choose how to group, and whether the dates mean when trades
   closed or when they opened.

The screen then shows:

- **The range:** the result after fees, trades won (with a range), profit factor, the deepest
  fall from a high, and the most at risk at once.
- **By period:** a bar for each period. Click one to look closer.
- **Strategies and the risk each carried:** most exposure first. Unlimited risk (an uncovered
  call, short shares) is counted on its own, never as zero. Dollar-days at risk weigh risk by
  how long it was held.
- **Open risk over time.**
- **What went better and worse than usual:** each kind of trade against your average, by:
  - strategy;
  - days held;
  - days to expiration;
  - how options ended;
  - where the strike was;
  - size against your usual;
  - weekday;
  - right after a loss;
  - how familiar the stock was.

  These are counts of your own trades with how many each rests on, not forecasts.
- **Every trade:** select one to see its legs and the statement rows exactly as in the report.
- **What the history says you still hold:** compared with Robinhood's own list when the
  connection is on. An option on it has **Price it to close**.

Rebuilding the trades from the report:

- Lots close first in, first out.
- Each result comes from the cash in the report, fees included.
- An assignment's shares stay with the option's trade.
- Splits and reverse splits are applied.

What is unknown stays unknown:

- A close of something opened before the history starts is listed and left out.
- An option past expiration with no row saying how it ended is marked unresolved.
- Rows the app cannot read are listed with the reason, never guessed.

The file stays on this computer, in the app's database. Nothing about the account goes to
the AI. Only ticker symbols are used, to look up prices. In demo mode a made-up sample report
can be loaded to try the screen.

**Today (2.5).** The landing screen, unless the address names another one. It shows:

- **The market** in one line: the regime on the last finished bar.
- **Your limits**: the daily stop and open risk, worded as on Portfolio risk. When some
  positions could not be measured it says so and does not show $0.
- **Leaning long and leaning short**: up to eight stocks each from the watchlist (or the 100
  large companies), largest net score first, with a bar for the score.

A stock leans the way its trend evidence points on the last finished bar. The lean is held
back when the market regime works against that side (a long in a bear market, a short in a
bull market) or when the opposite reversal is under way (a top at "potential" or "confirmed"
holds back a long; a bottom holds back a short). Held-back stocks are counted, with the
reason for each. Scores count evidence; they are not chances. Choosing a stock opens
Decide with that direction. When the signal history is not built yet, the screen says so and
offers to build it; once it is built, each row adds how often stocks with a score in the same
range were higher 10 days later (a past frequency for longs and shorts alike, not a forecast).
Switch it off with `FEATURE_TODAY=0`.

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
worked before (once the signal history is built), and **your thesis** (2.5). The thesis form
holds the direction, entry, invalidation, two targets, holding days, contracts and an
option budget; leave a price blank and it is suggested from the chart. One button, **Grade
and find a contract**:

1. Grades the stock trade from A to AVOID, with the size for your per-trade risk.
2. Finds calls (long) or puts (short) on the same levels: the best match, cheapest, best
   risk-adjusted and most conservative contracts, and the ones to avoid.
3. Sets **shares or a contract** side by side: what you pay, the result at target 1 and if
   invalidated, the chance option prices imply of a profit on the horizon date, and the
   outcome then: the middle one, and the range eight in ten fall in.

The share figures all start from your entry (or today's price when you leave it blank). The
outcomes come from the 10th, 50th and 90th percentile stock prices of one distribution, never
from the target and the stop alone. Averaged over every outcome, a contract is worth about
what you pay, since options are priced fairly against themselves; the panel says so once, and
that it is not an edge. Past cases (once the signal history is built) and the Monte
Carlo path range (the same model) are shown apart, never blended into one number. The
trend chart then runs on to the horizon date with the targets, the invalidation, the range
option prices imply by then and where the contract breaks even. **Set the limit** on any
contract opens Price an option on it with the limit ladder ready.

**The interactive chart (2.12).** At the foot of Analyze a stock, with `FEATURE_ANALYZE_INTERACTIVE=1`
(the default).

1. **Range and grouping.** Press 1D to 10Y, or Custom and type two dates. The grouping follows the range
   (five-minute bars for a day, daily bars for six months, weekly for five years) and can be changed to
   any the data allows: 1, 5 or 15 minutes, an hour, a day, a week, a month or a quarter. Bars within the
   day reach back about 40 days on Tradier and a week on Robinhood; the chart says what it loaded and how
   much more it can pan through.
2. **Move about.** Drag to pan, scroll or pinch to zoom, drag the time axis to stretch it. **Fit range**
   returns to the range; **Latest** jumps to the newest bar. Hover and the bar's open, high, low, close,
   volume and indicators read out underneath.
3. **Indicators.** Tick what to draw: candles or a close line, volume, 20-bar EMA, 50- and 200-bar
   averages, Bollinger bands, VWAP (within the day), support and resistance (with the number of touches),
   swings, trendlines, MACD and RSI panes, news and tops or bottoms forming. The chart redraws at once.
4. **Click a marker** to read it: a swing's price and when it was confirmed, a headline with its source
   and tone (and the link), or where a top or bottom began to form.
5. **Find similar patterns.** The last 40 bars on screen, or a window you select with two clicks, are
   matched against every window of the same length in this stock's own past at this grouping. Each match
   is drawn over the window at the window's scale, and what followed it past the last bar; the thick line
   is the median of what followed, the dashed lines the middle half. The table lists each match with its
   likeness and what came one, three and five bars later; **Go there** pans to it. The likeness bar
   (correlation) starts at 75% and is lowered a step at a time when nothing matches, which the note says.
6. **Possible outcomes.** For the horizon you pick (one bar to twenty, in that grouping's time; a week on
   daily bars to start): how often the matches went on to rise more than one average range of those bars,
   stay within one, or fall more than one, and how often they were simply higher, each with a 95% interval
   and the count. Then the other readings, each
   with its own count: a top or bottom forming on the daily bars and how that reading went before on this
   stock; the signals of the last ten bars and their record; today's move with the news behind it, and how
   moves this size on this stock went on. **Lean** counts the readings with ten cases or more whose way
   followed at least 55% of the time; the others are shown but do not count.

What this is not. A shape that looked the same before had other news and another market behind it, and
eight matches say little: with 8 cases the interval runs from about 15% to 85%. These are frequencies on
one stock's own past, never forecasts, and nothing places an order.

**What if? (2.13).** Under the chart, with `FEATURE_WHAT_IF=1` (the default; it needs the interactive
chart).

1. **The factors.** One row each: Japan (Nikkei), Europe (DAX), China (Hang Seng), the US market (SPY),
   the 10-year Treasury yield (in basis points), oil (Brent), the US dollar, volatility (the VIX), the
   stock's sector fund, and foreign peers you type in (US-listed shares of overseas companies in the same
   business, say TSM and ASML for a chip maker). Each shows **today's reading** and where it came from (the
   real index from stooq.com, about 15 minutes late, when it answers; else the fund that tracks it: EWJ,
   VGK, FXI, IEF, BNO, UUP, VIXY; the VIX reads in the index's terms, VIXY moving about half as much), **this stock's sensitivity** (the beta of its daily returns to the
   proxy's over the last year, with the correlation and the day count), a **scenario** box and a **use**
   tick. Then the news of the last 36 hours with its tone (a slider from -1 to +1) and this stock's record
   of moves with news behind them, the analyst upgrades and downgrades found in a week of headlines (net
   calls, half an average range each over the first bars), earnings ahead (which widens the band by the
   move option prices imply when the report falls inside the horizon), and the similar-pattern path with a
   weight from 0 to 1.
2. **Before the open and after.** Before 9:30 New York the readings are the starting scenario: the stock
   has not priced them yet. Once it trades, or after the close, they are already in its price, so the
   scenario starts at no further move and the readings stand beside it for reference. stooq's readings are
   from the session's open; the funds' are against their previous close. **Reset to the defaults** restores
   them; **Refresh readings** fetches them again. Your values survive a change of horizon or peers.
3. **The model.** The expected move at each bar of the horizon is the sum of beta times scenario over the
   factors ticked (landing on the first bar, as overnight news is priced at the open), plus the pattern's
   median path times its weight, plus the news and analyst terms spread over the first bars. The band is
   ±1.28 of the stock's own realised volatility scaled to the bars so far: a 10-90 range that says nothing
   about direction. The orange line and band on the chart redraw as you type; the terms are listed under
   the model with their sizes. The horizon is the outlook's (a week on daily bars to start) and can be
   changed.
4. **Ask ChatGPT, or ask Claude.** Pick the one you have a key for (`OPENAI_API_KEY`, `ANTHROPIC_API_KEY`;
   both share `AI_DAILY_REQUESTS`). It is sent every reading, beta, headline, the analyst calls, earnings,
   the volatility figures, the pattern's record and median path, the model's own path and the list of
   strategies, and answers as JSON: its path and 10-90 band (purple, dashed, on the chart), its confidence,
   a rationale, risks, the scenario values it would set, whether to use the pattern and at what weight, and
   a strategy with its reasons. **Use its settings** applies all of that to the model (and selects its
   strategy below); **Clear the AI line** removes it. Its answer is what a language model wrote from those
   inputs: check it against them.
5. **A position under the scenario.** Choose the scenario (the model's or the AI's), a strategy (buy
   shares, short shares, or any of the sixteen option strategies, built from the listed chain at the
   midpoint with an expiration a little past the horizon) and the quantity (shares, or contracts). The
   position is valued at the horizon date from the live price (the scenario's move is applied from there,
   even when the chart's window ended earlier) across a lognormal range of prices around the scenario's
   expected move with the band's spread: the profit or loss at the 10th, 50th and 90th percentile prices,
   the expected result, the chance of a profit, the most it can lose (or "no limit"), the cost and the
   breakevens, with the legs. **Rank every strategy** values all eighteen under the same scenario, orders
   them by expected result per dollar at risk, marks the best (★) and the AI's pick (AI), and a click on a
   row sizes it. The quantity converts between shares and contracts (100 shares to one contract).

What this is not. The model is arithmetic on the assumptions in the boxes: a beta is what a year of daily
returns showed, not a law, and eight factors with overlapping moves do not add independently. The AI's
scenario is a language model's reading of the same inputs. The position figures are simulator values at
the midpoint under a made-up distribution. None of it is a forecast or advice, and nothing places an order.

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
The score is a summary of checks, not a probability. The **Reversals** tab of the Scanner (2.5;
it was at the top of Analyze before, and stays there when the Scanner is switched off) lists
every watchlist stock's reading, strongest first. Choosing one opens Decide: long at a bottom,
short at a top.

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
- 2.12 (the interactive chart):
  - made-up bars within the day follow each day's open, high, low and close and are the same every time;
    bars group on the session's grid (9:30, 10:30, ...) and never across a day; daily bars group by week,
    month and quarter, dated on their first day, with unfinished periods marked;
  - bars within the day are kept in the database and refetched sparingly; a failed refresh keeps what is
    held and is reported;
  - Tradier's timesales and Robinhood's 5-minute historicals are read from their documented shapes;
  - a planted shape is found three times in 3,000 bars of noise and never in itself or in another match;
    the likeness bar is lowered a step at a time when nothing matches, and the result says so;
  - the chart covers the range in the grouping asked for, loads the history around it, checks custom
    dates, and puts the news on the bars of the range;
  - the outlook's readings each carry a count and an interval, only readings with 10 cases vote, and the
    API refuses bad ranges and swing sizes;
  - the page's helpers are tested under Node, and in Chromium the chart draws, the grouping and range change
    the bars, a window is selected by two clicks, and the patterns and outcomes render.

  Not checked against the real services, which this build environment cannot reach: Tradier's timesales and
  Robinhood's bars within the day are written to their published formats.
- 2.13 (what-if analysis):
  - the beta of a synthetic stock built as 1.5 times its proxy comes back as 1.5 with a correlation over
    0.9; too few days or a proxy that never moves gives none;
  - the model adds beta times scenario (a yield in basis points through the bond fund's inverse), the
    pattern's path at its weight (held when it runs out), the news term over three bars, the analyst term
    capped at two calls, and widens the band by the implied earnings move only inside the horizon;
  - stooq's CSV is read from its published shape (N/D rows skipped), the real index replaces the fund's
    reading and a yield reads in basis points, a failed request falls back to the funds and is not retried
    for five minutes, and demo data never asks stooq;
  - before the open the readings are the starting scenario and once trading they are not; a factor switched
    off leaves the model; the setup is cached for two minutes and the cached copy is not shared;
  - a fake ChatGPT and a fake Claude get a prompt naming every input, and their answers are checked: paths
    padded to the horizon and clipped, unknown variables dropped, an unusable strategy replaced, risks that
    are not strings dropped; ChatGPT's real client sends the key only to api.openai.com, asks for JSON,
    records the model in `ai_usage`, shares the daily limit with Claude, and never shows the key in an
    error;
  - shares and shorts are valued at the scenario's percentiles exactly and mirror each other; a bull call
    spread is built from the demo chain with an expiration past the horizon; the ranking numbers every
    strategy, marks the AI's pick, and prefers the bearish side under a falling scenario; bad kinds and
    quantities are refused; the feature flag switches the routes off;
  - the widget's helpers are tested under Node, and in Chromium the widget lists the factors, remodels as a
    value changes, asks a fake ChatGPT and shows its scenario, applies its settings, sizes a spread under the
    AI's scenario and ranks every strategy.

  Not checked against the real services, which this build environment cannot reach: stooq.com's quotes
  and OpenAI's API are written to their published formats.
- 2.11 (news search):
  - Google News search feeds and Brave results are read from their published shapes; a feed carrying a
    DOCTYPE or ENTITY declaration anywhere is refused, a feed that does not read is a failed check (never
    "no news"), and a link that is not http(s) is dropped;
  - 74 real-style headlines are matched or not as a reader would: brand names (Google, BofA, P&G,
    onsemi), possessives ("Lilly's obesity pill"), SEC names ("WELLS FARGO & COMPANY/MN"), and the traps
    (student visa, UK PM, King Charles, Harrison Ford, Paris Hilton, Citi Field, "Moody's downgrades US",
    "Goldman Sachs raises its S&P 500 target", Title Case headlines, GE Vernova for GE, the Amazon
    rainforest, Nvidia's RTX cards); headlines that only say the stock moved are told apart;
  - Brave is asked only when nothing fresh was found, never past its limit in any 31 days or its daily
    limit, even from two copies of the app at once, never follows a redirect with the key, and its key
    never appears in an error; a limit reached is "not asked", not a failure;
  - Google refusing a search (403, 429, 503 or a page that is not a feed) pauses it for 15 minutes for
    every copy of the app, and searches wait their turn two seconds apart across copies;
  - the Morning plan's check reads headlines it could not tie to the stock, a failed search, or a refused
    one as not "no news";
  - on demo data the movers scan finds the stocks that moved, searches at most six a scan, feeds the news
    found into Live news, lists the rest as no news found, does not search them again within the hour,
    asks Brave once a stock a day, and starts a fresh list each day;
  - after 4 p.m. the day's move is measured from today's close, and a stock is traded on once a day;
  - the same headline from two sources is kept once, but two filings with the same title are both kept;
  - in Chromium: the "Why is it moving?" panel lists movers with their news and the Analyze button.

  Not checked against the real services, which this build environment cannot reach: Google News's feed and
  Brave's API are written to their published formats.
- 2.10 (the fair price and Live news):
  - every ladder price is a valid increment inside the quote, a buyer's fair price never passes the
    midpoint (a seller's never falls under it), and the walk never passes the fair price, on 3,000 random
    quotes with random evidence;
  - the one exception, no bid and an offer one price step up, is said in so many words (the ask is the
    only price) instead of being shown as a fair price;
  - a quote stale on one side (its midpoint a quarter of the spread from the model value) leaves the
    midpoint out;
  - each piece of evidence is used only under its rule (a trade only when recent, inside the quote and
    with the market open), and Robinhood's estimates are never used;
  - a gapped stock's contract is repriced before the open, a flat one keeps its quote;
  - the SEC feed, RTPR's REST items, alert and impact frames, and Finnhub rows are read from their
    documented shapes, and odd entries are skipped;
  - the stream answers RTPR's pings and stops on a refused key, and the key is sent only to rtpr.io;
  - a demo news move becomes an opportunity with a card and is then stopped out, a quiet stock ends as no
    reaction, items outside the universe or over 18 hours old are not traded, and a stock you hold is
    flagged when the news goes against it;
  - in Chromium: the Live news screen listens, shows the feed and turns a move into a card, and the
    pricing screen shows the fair price.

  - an independent review found 15 problems; all were fixed and have tests (`docs/IMPROVEMENTS.md`).

  Not checked against the real services, which this build environment cannot reach:
  - RTPR's stream and archive (written to its documentation);
  - the SEC feed (written to its published format);
  - Robinhood's fill estimates (read only when its replies carry them);
  - whether Tradier's quotes before the open carry pre-market trades. If they do not, Live news prices
    a stock only from 9:30 on Tradier data.
- 2.8 (Morning plan):
  - the limit cap is checked against its closed form, against a strict payoff inequality, and on random
    cases (the independent reviewer re-verified it on 20,000);
  - ride needs news and fade needs no-news plus pre-move overextension; unchecked news never counts as
    "no news";
  - the past record and the live rule condition on the same pre-move bar;
  - picks are recorded only after a send succeeds, never for a dry run, and the scorecard grades them the
    trade's way;
  - a watch triggers exactly once, survives restarts, and waits while the market is closed;
  - the mailer refuses to send without a verified STARTTLS certificate, and the password never appears in
    an error;
  - dates are New York dates even on a UTC machine; weekends skip; a holiday still sends;
  - one stock's failure never stops the plan; the email escapes whatever lands in it.

  Two independent reviews (finance and arithmetic; robustness and security) checked the branch before
  release; what they found is listed in `docs/IMPROVEMENTS.md`.
- 2.7 (Trade ideas):
  - each listed trade's side matches Today's rule and its grade, levels and size match Decide's;
  - the simulation's chance of touching one level before another matches the closed form;
  - a day where both levels could be touched is checked against a 64-steps-a-day grid;
  - with no drift a trade averages break-even, and so does a contract off the money;
  - the scan runs one at a time, can be stopped, survives a restart, and survives one stock's failure.

  Two independent reviews found 31 problems, all fixed or noted (`docs/IMPROVEMENTS.md`).
- 2.6 (My trades), on hand-made reports in Robinhood's layout:
  - FIFO results from the cash columns;
  - expirations, assignments, exercises, splits and reverse splits;
  - each strategy's risk;
  - date ranges by close and by open;
  - overlapping imports kept once and removed safely;
  - the connection reads refused unless switched on, and never an order tool.

  Two independent reviews found 25 problems, all fixed with tests (`docs/IMPROVEMENTS.md`).
  The real Robinhood report and the connection's real order replies have not been seen by
  the tests: if an import lists unreadable rows, or the check shows unexpected reply shapes,
  send the message or the capture file.

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
  options/                 the option picker, reversal option scores; historical option data (none connected);
                           volatility.py lab.py (options lab), strategies.py (strategy builder)
  scanner.py               technical signals and options activity across many stocks
  today.py                 which stocks lean long or short today, and why some are held back (2.5)
  tradehistory.py          your Robinhood history: the report's parser, imports, the sample (2.6)
  tradeanalysis.py         trades rebuilt from it; strategies, risk, periods and patterns (2.6)
  robinhood_account.py     optional read of your orders and holdings through the connection (2.6)
  ideas.py                 trade ideas: the background scan of up to 200 stocks, graded and ranked (2.7)
  morning.py               the morning plan: movers, ride/fade setups, limit caps, past records, watches (2.8)
  livenews.py              live news: RTPR stream, SEC 8-K and Finnhub polling, price reactions, signals (2.10);
                           the "why is it moving?" movers scan (2.11)
  searchnews.py            news found by searching: Google News search feeds and capped Brave news search (2.11)
  intraday.py              bars within the day (Tradier, Robinhood, demo), kept in SQLite, grouped by hour/week/month (2.12)
  similar.py               similar patterns in the stock's own past, and what followed them (2.12)
  chartview.py             the interactive chart: bars, structure, tops and bottoms, news, similar, outlook (2.12)
  factors.py               what-if factors: proxy funds and stooq readings, betas, the model's arithmetic (2.13)
  whatif.py                what-if analysis: the setup, the model, the AI's scenario, a position and its ranking (2.13)
  chatgpt.py               ChatGPT (OpenAI Chat Completions) sharing the AI daily limit (2.13)
  morning_email.py         the plan rendered as the 7:00 email, text and HTML (2.8)
  mailer.py                SMTP over verified STARTTLS; the password scrubbed from every error (2.8)
  background.py            the shared background-job slot used by Trade ideas and the Morning plan (2.8)
  tradesim.py              the simulation of one trade: paths, stop and target exits, shares and a contract (2.7)
  makeup.py news.py        the market's make-up; headlines and their tone
  ai.py briefs.py          the AI plug-in (Anthropic API) and what it is asked
  portfolio.py analytics.py alerts.py   portfolio risk, journal analytics, alerts
  backtest_v2.py           condition strategies, Monte Carlo
  analysis.py api_v2.py    the version 2 operations and the /v2 HTTP API
  versions.py              version labels recorded with every result
  static/                  the web page: flow.js (2.5 pure helpers, tested under Node), app.js classic screens,
                           v2.js version 2 screens, lab.js 2.4 screens, decide.js Today and Decide (2.5),
                           trades.js My trades (2.6), ideas.js Trade ideas (2.7), morning.js Morning plan (2.8),
                           live.js Live news (2.10), chart.js the interactive chart (2.12), whatif.js the
                           what-if widget (2.13), lwcharts.js TradingView Lightweight Charts 5.2.1
                           (Apache-2.0; LICENSE-lightweight-charts.txt)
docs/V2.md                 version 2 in detail: rules, formulas, database and API
docs/IMPROVEMENTS.md       the 2.1.1 to 2.13 engineering record: defects found, tests, reports
docs/RELEASE_NOTES.md      what changed in each release, and how to upgrade
tests/                     automated tests
```

The app listens on 127.0.0.1 only, so it is not reachable from other machines.
