"use strict";
/* Trade Assistant 3.1: what every widget is for, in one place.

   The shared widget frame (ws-core.js) asks WS.helpButton(w) for the information icon it puts first among every
   widget's header controls, so a new widget cannot be added without one: its help is looked up here by type, and a
   type with no entry is reported (WS.missingHelp(), checked by the tests and logged at boot). Each native widget
   says six things: what it does, what it uses, how to use it, how to read it, its limits, and what it changes.
   Trading widgets also say what is paper, what reaches a broker and what needs your approval. A classic screen
   inside a widget reuses its 2.x text from help.js, with what changed in 3.0.

   The panel opens on hover or keyboard focus as a preview; a click, Enter, Space or a tap keeps it open (and moves
   focus into it); Escape, the close button, a click elsewhere or closing the widget closes it. One is open at a
   time. Nothing here places an order. */
(() => {
  const SECTIONS = [["purpose", "What it does"], ["data", "What it uses"], ["use", "How to use it"], ["read", "How to read it"],
    ["limits", "Limits and freshness"], ["next", "What it changes"], ["trading", "Orders, paper and approvals"]];
  const REQUIRED = SECTIONS.slice(0, 6).map(([k]) => k);

  const H = {
    feed: {
      purpose: "Every opportunity the scans found, each stock, direction and horizon once, ranked on one scale from 0 to 100.",
      data: "The Morning plan, Trade ideas, Live news and the reversal scan (and the technical scanner when you scan the watchlist). Their sightings of one stock, one way and one horizon are merged into one candidate. The score is a weighted sum: historical support 35%, setup quality 25%, catalyst 15%, liquidity 15%, freshness 10%, less execution cost (10%) and a conflict with an opposite candidate (15%).",
      use: "Filter by horizon and direction, or show only autopilot-eligible rows. Refresh the scans to look again. Click a row to see how its score is made, its levels, what found it and why it is or is not autopilot-eligible; Study it runs a Research study bound to the opportunity.",
      read: "Score: higher ranks first; the bar is the score. Edge: the past average of the same trade after costs, pulled toward zero when the record is short ('none' when there is no record). Autopilot: eligible only with a validated edge, no conflict, enough liquidity and real prices; hover 'No' for the reasons.",
      limits: "A score counts evidence; it is not a chance of profit. A swing candidate drops off three trading days after it was last seen. On demo data the prices are made up and nothing is eligible.",
      next: "Picking a row sets the link group's stock, direction, horizon and opportunity: the linked chart, study, ticket and proposals follow. Study it also sets the study, which Research shows." },
    allocator: {
      purpose: "Splits a budget across the best opportunities in the feed, valued together so that stocks which move alike are not counted as separate bets.",
      data: "The feed's top candidates, their after-cost edge and volatility, and the correlations between them, simulated together on correlated price paths. Money the Trading Desk has already reserved for open orders is taken off the budget first.",
      use: "Enter a budget, how many opportunities to spread it across, a horizon, an aim and the instruments allowed, then Split the budget. Click a row to send its stock to the linked widgets.",
      read: "Put to work and kept as cash add up to the budget. Expected result is the simulated average. Expected shortfall is the average of the worst tenth of outcomes. Most it can lose is the contract-terms maximum ('no limit' when a short or an uncovered option is in it). Chance of profit per row is the model's, from the same paths.",
      limits: "The paths come from past volatility and correlation: a regime change, a gap or an event can do worse than any of them. Cash can stay unused when nothing adds enough after costs and risk, or when the caps bind. The capital a broker requires (margin, option buying power) can differ from these figures.",
      next: "Nothing is bought. Clicking a row sets the linked stock and opportunity. To trade a row, write a proposal on the Trading Desk." },
    watchlist: {
      purpose: "Your list of stocks with their latest price and the day's change.",
      data: "Quotes from the data source in the status bar, refreshed about every 20 seconds while the widget is in view.",
      use: "Type a ticker and press Enter to add it; × removes it. Click a row, or press Enter on it, to make it the linked stock.",
      read: "Change is since the last close. The highlighted row is the linked stock.",
      limits: "Delayed 15 minutes unless the status bar says real-time quotes; made up on demo data. Each widget keeps its own list.",
      next: "Selecting a row changes the stock for every widget linked to this one, in every workspace." },
    news: {
      purpose: "Headlines and filings for the linked stock, with their tone.",
      data: "The news sources set up in .env (headlines, press releases, SEC filings) for the last week; tone is a word score of the headlines.",
      use: "Pick a stock in any linked widget. Refresh fetches again.",
      read: "Newest first, each with its source and time. The tone line summarizes the words of the headlines; it is not a forecast.",
      limits: "Only as complete as the sources configured; a story can arrive minutes after the move. Times are when the source published, as reported.",
      next: "Read only: it changes nothing else." },
    chart: {
      purpose: "The linked stock's price chart, with indicators, drawing tools, similar past patterns and the study's forecast drawn ahead of the last bar.",
      data: "Price bars from the data source; indicators computed here. The forecast lines are the linked Research study's, drawn on daily bars only.",
      use: "Choose the range and bar size; fold out Indicators for moving averages, VWAP and the rest. Drag to pan, scroll to zoom. Select a window to find similar patterns. In a study, Draw on the linked chart adds its forecast.",
      read: "Candles are open, high, low and close. The teal line is the study's base path; the dotted teal lines are its range (the calibrated 80% band, or the model-implied 10th to 90th percentile when not calibrated); grey dashed lines are alternative paths: assumptions, not predictions.",
      limits: "Bars are delayed unless the data source is real-time. Similar patterns are past frequencies, not forecasts. The forecast is in trading days, so it is hidden on intraday bars.",
      next: "It offers itself to linked widgets: the study and the scenario explorer draw on it. Moving the crosshair over a day also marks that day on a linked option chart." },
    scenario: {
      purpose: "Advanced: the older what-if factor model, for testing how overseas markets, rates, oil and news shocks would move the stock.",
      data: "The 2.x what-if engine and its factor history. It is a separate model from the Research study's forecast.",
      use: "Link it to a stock chart (same colour) and set the factor shocks; its lines are drawn on that chart.",
      read: "Each line is the stock's path under the shocks you set: an assumption you chose, not a prediction. The position panel values a trade under that path.",
      limits: "Its base line is not the study's forecast, and the two can disagree; when deciding, use the study's forecast and treat these as stress tests. Factor data can lag by a day.",
      next: "Draws on the linked chart; changes nothing else." },
    chain: {
      purpose: "The linked stock's listed options for one expiration: bid, ask, implied volatility and delta at each strike.",
      data: "The option chain from the data source.",
      use: "Pick an expiration and how many strikes to show; show calls, puts, both, or follow the trade's direction. Click a quote to select that contract.",
      read: "The strike column is in the middle, the strike nearest the stock price is marked, and in-the-money cells are shaded. Wide gaps between bid and ask mean a fill will cost more.",
      limits: "Quotes are as fresh as the data source; some strikes have no bid. IV and delta are the source's (or computed here when it has none).",
      next: "Selecting a contract sends it to the linked ticket, option chart and risk panel." },
    strategy: {
      purpose: "Builds a standard option strategy on real listed contracts and shows what it can make and lose.",
      data: "The chain for the linked stock and the 2.x strategy templates (spreads, straddles, condors and the rest).",
      use: "Choose a strategy, days to expiration and contracts, then Build. Show its risk sends it to the risk panel; a one-leg strategy can be priced on the ticket.",
      read: "Debit or credit at the midpoints; most it can lose and make from the contract terms ('no limit' where the loss is unlimited); breakevens at expiration.",
      limits: "Priced at midpoints: real fills are usually worse, more so for several legs. Multi-leg strategies are entered by hand at the broker.",
      next: "Sets the linked strategy (the risk panel) or contract (the ticket)." },
    riskreward: {
      purpose: "The profit and loss of the selected contract or strategy across stock prices and dates, and what an implied-volatility change does to it.",
      data: "The legs from the chain, the strategy builder or the study, valued with Black-76 at today's implied volatility.",
      use: "Select a contract or a strategy in a linked widget; move the slider to shift implied volatility.",
      read: "Each curve is the P&L at a date, the last one at expiration. The theoretical maximum loss comes from the contract terms; modeled downside is what the model's spread of outcomes reaches; neither is the capital your broker will ask for.",
      limits: "A model: volatility, dividends and early exercise can move real prices away from it. Before earnings, implied volatility usually collapses after the release.",
      next: "Read only." },
    ticket: {
      purpose: "The limit price to type for one option contract: a ladder from a fair price worked out from the quote's evidence, and the checks a buyer should pass.",
      data: "The contract's live bid and ask, its recent quotes and the pricer's fill evidence.",
      use: "Select a contract in the chain or the study; choose buy or sell and how many. Pin it to the dock to keep it in view while you research.",
      read: "Patient, fair and most-to-pay prices: for a buyer the fair price never goes above the midpoint. Each check passes, warns or flags a known way option buyers lose money.",
      limits: "Quotes refresh every few seconds; a fair price is an estimate, not a promise that an order fills.",
      next: "Nothing is submitted from here. To trade, write a proposal on the Trading Desk, or type the limit at your broker.",
      trading: "No order is sent. The ticket prices; the Trading Desk proposes; you approve." },
    activity: {
      purpose: "What the app and its background work did, as it happened.",
      data: "The workstation's event stream: proposals, approvals, orders, the daemon, studies, Discover, notifications and the autopilot.",
      use: "Read it; the newest is at the top.",
      read: "Each line has the time and what changed. A failure says what failed.",
      limits: "Shows the last 100 events since the page opened (and the 30 before). The full record is the audit log in the database.",
      next: "Read only.",
      trading: "Nothing is sent from here: it reports what proposals, approvals, the broker, the daemon and the autopilot did, paper and live alike." },
    study: {
      purpose: "One complete, saved investigation of the linked stock: the trend and regime, past situations like today's in its own history and its peers', the options market, one forecast with its record, and every strategy compared under the same scenarios.",
      data: "Price history (the stock, SPY and up to five peers), the options market's implied volatility and earnings move, the trend, regime, signal and news engines, and the forecast model's calibration for the horizon.",
      use: "Pick a horizon, the kind of trade, a direction (or either) and any peers to add, then Run the study. Read it by section: Summary, Evidence, Forecast, Strategies. In Strategies, click a row to send it to the linked widgets, or Draft to hand it to the Trading Desk with its context. Ask the AI to challenge it for the case against. Catalyst Scenarios (Forecast, 1- or 5-day studies): name an event, Export evidence (a Markdown packet downloads and the question is copied), run it in a scenario provider such as the MiroFish dashboard, paste its JSON (or fill in the form) and Import cases, then review each case's assumptions and Apply assumptions to draw them.",
      read: "The forecast's range is only called an 80% band when the calibration for this horizon passed; otherwise it is the model-implied 10th to 90th percentile, not yet validated. Alternative paths are assumptions to test against. In Strategies: expected P&L per scenario after round-trip costs; maximum loss from the contract terms; expected shortfall, the average of the worst tenth; capital needed; estimated fillability, a heuristic from the spread; and the route: the Trading Desk can send it, enter it by hand at the broker, or research only.",
      limits: "Saved with its as-of time, data source and model versions: it does not update itself. What could not be read is listed under Missing. An interrupted study is marked failed and can be run again. A catalyst case is a set of assumptions, not a prediction: it has no probability, its band is the base's model-implied range (plus any extra event uncertainty you assume), and the provider's report is shown as text and never draws a line.",
      next: "Sets the link group's study. Draw on the linked chart draws the forecast at the study's own anchor price and dates; Apply assumptions saves a scenario version and draws its cases, dashed, beside the base; a strategy row sets the linked contract or strategy; Draft sets the linked draft trade that the Trading Desk's Proposals widget shows." },
    optionchart: {
      purpose: "The selected contract's own price history beside the stock's.",
      data: "The contract's traded daily bars when the data source has them; otherwise a theoretical history modeled from the stock's prices and implied volatility.",
      use: "Select a contract in the chain or the study; choose the range.",
      read: "The label says Traded prices or Theoretical, not traded. The grey line is the stock on the left scale.",
      limits: "A theoretical history is a model, not what the contract traded at; thinly traded contracts may have gaps.",
      next: "Read only. A linked stock chart's crosshair marks the same day here." },
    forecastrecord: {
      purpose: "How well the forecast model has done on history at each horizon: its calibration.",
      data: "A walk-forward test over the watchlist's stocks: the model is fitted on the earlier 70% of dates and judged on the later 30% it never saw.",
      use: "Choose a horizon and Run the calibration (it takes a while; progress is shown). Studies at that horizon use the result.",
      read: "Inside the band: how often outcomes fell inside the 80% band (close to 80% is calibrated). Median's miss against 'assuming no move': below it means the model adds something. Direction right: when it leaned, how often it was right. By regime: the same, split by the market's state.",
      limits: "Past coverage does not guarantee future coverage. With too few test cases it says so rather than calling the model calibrated. A calibration interrupted by a restart must be run again.",
      next: "A new calibration applies to studies run after it; open studies keep the one they were made with." },
    lab_runs: {
      purpose: "Starts, stops and resumes replays: history walked one trading session at a time, each forecast frozen when it is made and graded once its window closes.",
      data: "data/replay.db: the frozen datasets (about eleven years of daily bars for the 100 large caps and the market funds) and every replay's record.",
      use: "Build a dataset once (it uses your Tradier token), then New replay… and Start. Stop pauses at the last finished session; Resume continues from there. Click a run to show it in the other Model Lab widgets.",
      read: "Status: Running, Paused, Complete, Failed, or Interrupted (it stopped answering: a closed laptop or a crash; Resume it). At: the last session finished. Result: the run's one-line verdict against 'no change'.",
      limits: "A full ten-year replay takes from minutes to about an hour depending on the computer; monthly retraining is about four times faster. A development replay must end before 1 November 2025: the year after is held out, and is opened once with 'python run.py --replay holdout RUN', never from here.",
      next: "Starts a separate process on this computer that writes to data/replay.db. Nothing here touches the Trading Desk, a broker or the trading database." },
    lab_curve: {
      purpose: "Month by month, whether the models' frozen forecasts beat 'no change', and which model family was champion at the time.",
      data: "The selected run's graded forecasts at the chosen horizon (one or five sessions).",
      use: "Pick the horizon. Hover a bar for that month's band coverage and champion. The run is the one picked in the Replay controller, or the newest finished one.",
      read: "A bar above the line: that month's forecasts had smaller errors than assuming no move, in basis points per stock and day. The line under the chart lists the champions in order: a model replaced only after beating its predecessor on data neither had trained on.",
      limits: "Monthly figures are noisy: judge the run by its whole record (the Result line). The universe is today's large companies, which flatters every model equally; compare models with each other, not with the market.",
      next: "Nothing: it reads." },
    lab_registry: {
      purpose: "The models a replay trained and promoted, and the model sets deployed for the daily shadow forecasts.",
      data: "The run's training decisions (each with every candidate's validation figures) and the deployments in data/replay.db.",
      use: "Deploy these models makes a finished run's final champions the ones the shadow forecasts use from the next close; Make active switches back to an earlier deployment.",
      read: "Final champions: the family each task ended with. Promotions: when a challenger replaced the champion and why (it beat it by more than chance on a validation window, with an honest band, two training days running). Kept: the champion was refitted on the newer data.",
      limits: "A deployment never retrains on its own; to update the models, run a newer replay and deploy it. A model set moved from another computer (python run.py --replay import) is checked against its hashes.",
      next: "Deploy and Make active change which models the shadow forecasts use. They place no order." },
    lab_errors: {
      purpose: "Where the forecasts went wrong: the largest misses, the error in each market regime, and the stocks forecast worst.",
      data: "The selected run's graded forecasts at the chosen horizon.",
      use: "Pick the horizon. Click a stock to make it the linked stock everywhere.",
      read: "By regime: the error against 'no change' and how often outcomes fell outside the 80% band, in each market state (trend and volatility). Largest misses: what the model said, what happened, in which regime, from which model.",
      limits: "A large miss is not by itself a mistake: an earnings day or a market shock is outside any forecast built from prices alone.",
      next: "Clicking a stock changes the link group's stock." },
    lab_trades: {
      purpose: "What the narrow trade would have earned: every qualifying setup, the ones the take/pass gate chose, and the ones it passed.",
      data: "The selected run's graded setups: entry at the next open, a target 1.5 daily ranges up, a stop 1 down, five sessions at most, 5 basis points of costs a side.",
      use: "Read it beside the learning curve: price accuracy and trading results are judged apart.",
      read: "If the taken trades beat every setup and the passed ones, the gate chose well; if they do not, it did not, however good the forecasts look. Profit factor: what the winners made for each dollar the losers lost. Worst run: the deepest fall of the running total, one unit per trade.",
      limits: "Daily bars cannot say whether a day reached the target or the stop first when it reached both; those count as the stop (listed as ambiguous). Simulated fills, not real ones.",
      next: "Nothing: it reads." },
    lab_shadow: {
      purpose: "The forward record: the deployed models' forecasts of each trading day, saved after the close and before the future happened, and how they turned out.",
      data: "The active deployment, the replay (and held-out year) behind it, and the shadow forecasts the app saves after each close.",
      use: "The daily job (in the daemon, or the app when no daemon runs) forecasts the last finished session about 45 minutes after the close; Forecast now does it at once. Show in Discover / Show in Research add the model's widgets there.",
      read: "The tag says what stands behind the models: tested on the held-out year, or replayed only. The headline is the forward record so far against 'no change'.",
      limits: "A session not forecast before the next open is left out rather than forecast late, so a computer asleep in the evening leaves a gap. The forward record grows one session a day: it takes months to mean much.",
      next: "Forecast now saves that session's forecasts to data/replay.db. No order is placed." },
    lab_research: {
      purpose: "How Research's own saved forecasts have fared, now that they are graded.",
      data: "Every Research study's forecast (forecast_records), graded once its target session closed: the move, whether it fell inside the band, whether the median's lean was right.",
      use: "Read it to judge the Research forecast before relying on it; graded hourly.",
      read: "Inside band near 80% is calibrated. Miss below 'No change' means the median added something. Direction: when the median leaned, how often it was right.",
      limits: "Only studies you ran are graded, so the count grows with use. Target sessions count exchange trading days.",
      next: "Nothing: it reads." },
    lab_paper: {
      purpose: "Sends the take/pass gate's setups to the paper desk, and keeps their record: paper fills against the simulation of the same frozen forecasts.",
      data: "The active deployment's shadow forecasts from the last close, the trading policy, quotes, and the paper broker's fills.",
      use: "Send the gate's setups to the paper desk switches it on for the active deployment; the checks list says what still stops it. Each morning in market hours the taken setups become proposals at the ask.",
      read: "Result: the closed trade's return after fees. Simulated: what the replay's barrier rules gave the same forecast. The difference is what real fills, timing and your approvals changed. The setups the gate passed on are shown too: what abstaining earned.",
      limits: "Paper and shadow accounts only: under a live policy it writes nothing. Exits (stop, target, five sessions) are software-monitored every five minutes while the app or the daemon runs, not stop orders at a broker.",
      next: "Writes buy and sell proposals on the Trading Desk, source 'model', through the policy and the risk engine.",
      trading: "Paper or shadow only. Each proposal waits for your approval unless the policy is already capped_autopilot on a paper or shadow account, which then authorizes it within its daily order count. The models cannot change a limit." },
    lab_specialists: {
      purpose: "Which stocks get a correction of their own on top of the general model, and how the specialist families fared as challengers.",
      data: "The selected run's final models (or the active deployment's) and its training decisions.",
      use: "Tick the specialist families in New replay… to let them compete; click a stock to link it.",
      read: "Average miss: how far, in volatility units, the general model missed this stock on rows it never trained on. A correction is kept only when that miss is clearly not chance (|t| of 3 or more: with a hundred stocks, two would correct about five by luck), and it is shrunk toward zero by how many cases there are.",
      limits: "A stock with little history falls back to the general model. A specialist family is only used if it beat the champion under the same promotion rule as every other.",
      next: "Nothing: it reads." },
    model_ideas: {
      purpose: "The deployed models' view of the last close: every qualifying setup with its chances and the gate's decision, and the stocks the next-day forecast leans on hardest.",
      data: "The shadow forecasts saved after the last close by the deployed models (Model Lab).",
      use: "Click a row to make that stock the linked stock: the chart, the study and the ticket follow.",
      read: "Target first / Stop first / Neither: the chances of each outcome of the narrow trade. Expected net: after costs. Take / Pass: the gate's decision. The tag says whether the models were tested on the held-out year or only replayed.",
      limits: "The Discover rank above is unchanged and does not use these forecasts. Models trained on prices alone know nothing of tomorrow's news.",
      next: "Clicking a row changes the link group's stock. It never proposes or sends an order." },
    model_forecast: {
      purpose: "The deployed models' forecast for the linked stock, and how they have done on it.",
      data: "The last close's shadow forecast for the stock, and its graded record (live, or from the replay when there is no live record yet).",
      use: "Pick a stock anywhere in the link group. Why did it change? compares the last two forecasts: for a linear model, exactly how much each input moved it; for trees, which inputs moved most.",
      read: "Forecast: the expected move to the close one or five sessions on, with its 80% band and the chance of a rise. Setup: the narrow trade's chances, if one qualified.",
      limits: "Only stocks in the models' universe (the 100 large caps) with a year of history are forecast. For a tree model no single input can be credited with a change.",
      next: "Nothing: it reads." },
    approvals: {
      purpose: "The proposals waiting for your decision.",
      data: "Proposals in awaiting_approval, each with its order hash, risk checks and the time its approval expires.",
      use: "Approve or Decline; Check again re-runs the risk checks (and asks for a new approval when one expired). Look closer opens it in Proposals.",
      read: "The order, the account, its cost, the thesis, and whether every risk check passes.",
      limits: "An approval is bound to the order's exact terms (its hash): if the order changed after you looked, approving fails. Approvals expire after the policy's minutes.",
      next: "Approving authorizes this one order once; the risk checks run again before it is sent.",
      trading: "In paper mode an approved order fills on paper. Live orders go to Robinhood only with live execution enabled, ready and within LIVE_MAX_ORDER; only share orders are sent automatically." },
    proposals: {
      purpose: "Writes an order proposal for the linked stock, checks it against the policy, and follows every proposal through its states. A draft trade sent from Research appears at the top with its context.",
      data: "The trading policy, the quote, the desk's book (capital in use, today's buying and result) and the linked study and opportunity.",
      use: "Choose buy or sell, shares and a limit (empty uses the ask or bid), write the thesis, then Check and Propose. For a Research draft: Propose this draft when the Trading Desk can send it, otherwise copy the order to enter by hand at your broker.",
      read: "States: waiting for you, approved, sending, working, filled, blocked, cancelled, expired; unknown means the broker's answer was not seen and it is being reconciled. Each proposal lists every risk check and its history.",
      limits: "The desk writes limit orders for shares; option and multi-leg strategies are drafted for manual entry unless the policy allows them and the account's broker can send them.",
      next: "Propose writes the proposal and asks for approval (Approvals). Cancel it withdraws it, or asks the broker to cancel a working order.",
      trading: "Proposing never sends an order: approval does, and only as the policy, the account and the kill switch allow. Paper orders fill on paper; a live order needs live execution set up in .env." },
    deskstatus: {
      purpose: "The desk at a glance: the trading mode, the kill switch, the money in use against the policy's limits, who runs the background jobs, notifications and live readiness.",
      data: "The active policy, the budget ledger, the job queue's heartbeats and the live broker's last check.",
      use: "Kill switch: stops everything (it cancels working orders and refuses approvals); switch it off here. Send a test notification. Check live trading.",
      read: "Meters fill as you near a limit (amber near, red at it). Background jobs: the daemon runs them, or this app while it is open, or nobody yet.",
      limits: "Today's result is measured from marks; 'not measured' when a price is missing. While nobody runs the jobs, approved orders wait.",
      next: "The kill switch affects every proposal, approval and working order in every workspace.",
      trading: "Paper trial capital is pretend money. Live trading is off unless ROBINHOOD_EXECUTION=1 and the account are set in .env, and every live order is capped by LIVE_MAX_ORDER." },
    policy: {
      purpose: "The written rules the desk trades by: mode, account, limits, trading window and which stocks.",
      data: "The active policy version; every change is kept in the audit log.",
      use: "Change the fields and Save the policy. Mode: off, paper, approval required (live), or capped autopilot.",
      read: "Each limit is checked on every proposal: largest order, new buying a day, the daily loss stop, positions at most, the widest spread, the oldest quote, how far a limit may be past the quote, and the minutes to approve.",
      limits: "Long shares only, limit orders, regular hours by default. A policy cannot raise LIVE_MAX_ORDER or enable live execution by itself.",
      next: "Applies to every proposal written or approved after saving; open proposals are rechecked before they are sent.",
      trading: "Approval required sends live orders, each approved by you. Capped autopilot trades within these limits without a click: in the shadow account on paper, live only after its practice record and AUTOPILOT_LIVE=1." },
    deskpositions: {
      purpose: "What the desk's account holds: shares, average cost, the price now and the gain.",
      data: "The desk's own fills in the account the policy trades (paper, shadow or live), marked at the latest quote.",
      use: "Click a row to link its stock; Propose selling writes a sale proposal for the whole holding.",
      read: "Gain is unrealized, from the mark. Today is the change since the last close. Exit says who manages the exit: software-monitored (the autopilot's stop, target or time, checked every few minutes) or yours.",
      limits: "Software-monitored exits are not broker stop orders: if the app and the daemon stop, the Mac sleeps, a quote goes stale or the price gaps, a position can stay open past its stop. Holdings from your own Robinhood trades are on Positions, not here.",
      next: "Propose selling writes a proposal that waits for approval.",
      trading: "Selling reduces risk, so the daily loss stop never blocks it; it is still checked and approved like any order." },
    autopilot: {
      purpose: "The capped autopilot: buys and sells within the written policy with no one clicking approve, only when the policy's mode is capped autopilot.",
      data: "Discover's autopilot-eligible candidates (long only, at or above the policy's minimum score, in its horizons), the policy's limits, and its own practice record in the shadow and paper accounts.",
      use: "Turn it on by setting the policy's mode to Capped autopilot. Run it once now to see a run. It runs every five minutes in market hours while the daemon or this app runs.",
      read: "Each gate passes or fails; a failing gate holds it back. The practice record counts the days it decided and its result. The last run lists what it bought, sold and passed over, with reasons.",
      limits: "Its exits are software-monitored, not broker-protected: checked about every five minutes while the daemon or this app runs. A sleeping Mac, an outage, a stale quote or a gap can leave a position open past its stop or target, and an exit can fill below its stop.",
      next: "Every order it writes goes through the same risk checks and approval record as yours, approved by 'autopilot' on the 'policy' channel.",
      trading: "In the shadow account its orders fill on paper in a book of their own. Live needs AUTOPILOT_LIVE=1, live execution ready, the practice days and no unresolved order; this release does not loosen any of that." },
    holdings: {
      purpose: "Everything you hold, from your recorded fills: shares long and short, calls and puts, with their value now and the gain.",
      data: "Your positions (entered by hand or imported) and their marks from the data source.",
      use: "Click a row to link its stock. Record or correct fills on the classic Positions screen (Classic screens menu).",
      read: "Market value at the mark; unrealized gain against your average cost; realized gain from closed parts. Mark age says how fresh each price is.",
      limits: "Only as complete as the fills recorded; the Trading Desk's paper and shadow fills are on Desk positions. A missing mark shows as not valued.",
      next: "Selecting a row changes the linked stock." },
    exposure: {
      purpose: "The risk across everything you hold: gross and net exposure, open risk, the largest positions and sectors, and a bad-day estimate.",
      data: "Your positions, their marks, betas and correlations from price history, and ACCOUNT_SIZE in .env.",
      use: "Read it; Refresh recomputes. Stress tests are on the classic Portfolio risk screen.",
      read: "Gross is long plus short; net is long less short. Concentration lists the largest share of the book by stock and by sector. The bad-day estimate is a modeled loss on a one-in-twenty day, not a limit.",
      limits: "Betas and correlations come from the past year and change in a crisis. Open risk is on the basis stated beside it (stops where set, the whole premium for options).",
      next: "Read only." },
    unknown: {
      purpose: "Stands in for a widget saved in a layout that this version does not have.",
      data: "Nothing: the saved type name only.",
      use: "Close it, or update the app to the version that has the widget.",
      read: "The message names the missing type.",
      limits: "The rest of the layout is unaffected.",
      next: "Nothing." },
  };

  // What changed in 3.0 for each classic screen, shown under its 2.x help.
  const CLASSIC_NOTES = {
    morning: "In 3.1 the Morning plan is not in Discover's standard layout: its picks reach the Opportunities widget (Refresh the scans), ranked once with every other scan's. Open it here for its trade cards and the 7:00 email.",
    live: "Confirmed news also reaches the Opportunities widget when the scans are refreshed.",
    ideas: "Its ideas reach the Opportunities widget when the scans are refreshed, ranked on Discover's one scale.",
    scanner: "Discover's Scan the watchlist too runs this scanner and ranks what it finds in the Opportunities widget.",
    analyze: "Research's study (3.0) is the one investigation to rely on: this screen's what-if line is a separate model from the study's forecast, so do not read the two as one prediction.",
    price: "The Order ticket widget uses the same pricer.",
    simulate: "The Risk / reward widget values the linked contract or strategy the same way.",
    positions: "The Trading Desk's paper and shadow fills are not here: they are on Desk positions.",
    paper: "Separate from the Trading Desk: its portfolios, fills and results are its own, and the desk's proposals, approvals and policy do not apply to it.",
    portfolio: "The Exposure widget shows the same risk natively; stress tests stay here.",
    trades: "Unchanged in 3.0.",
    journal: "Unchanged in 3.0.",
    research: "Research's study (3.0) gathers these facts and more in one saved investigation.",
  };

  const classicHelp = view => (typeof HELP !== "undefined" ? HELP[view] : null);

  // The help for a widget type: a native entry here, or a classic screen's from help.js.
  function helpFor(type) {
    if (H[type]) return { kind: "native", h: H[type] };
    if (String(type).startsWith("classic:")) {
      const view = type.slice(8), h = classicHelp(view);
      if (h) return { kind: "classic", h, note: CLASSIC_NOTES[view] || null };
    }
    return null;
  }
  // Registered widget types with no help, or native help missing one of the six parts: the tests keep this empty.
  function missingHelp() {
    const out = [];
    for (const type of Object.keys(WS.types())) {
      const got = helpFor(type);
      if (!got) out.push(type);
      else if (got.kind === "native" && REQUIRED.some(k => !got.h[k])) out.push(`${type} (incomplete)`);
      else if (got.kind === "native" && WS.types()[type].category === "Trading" && !got.h.trading) out.push(`${type} (no trading section)`);
    }
    return out;
  }

  // The data source's state, said the same way in every panel.
  function dataLine() {
    const s = (typeof state !== "undefined" && state.status) || null;
    if (!s || !s.data) return null;
    const how = s.data.mode === "demo" ? "made-up demo prices" : s.data.realtime ? "real-time quotes" : "quotes delayed 15 minutes";
    return ["Data", `${s.data.label}: ${how}`];
  }

  /* ---------- the panel ---------- */
  let open = null;               // {w, btn, pop, pinned}
  let uid = 0, quiet = null;     // `quiet`: the icon focus returns to on close, which must not reopen its preview

  function content(w, id) {
    const got = helpFor(w.type);
    const kids = [el("div", { class: "ws-help-top" }, el("h3", { id: `${id}-t`, text: w.def.title }),
      el("button", { class: "ws-wbtn ws-help-x", type: "button", "aria-label": "Close help", title: "Close (Escape)", onclick: () => close(true) }, "×"))];
    if (!got) { kids.push(el("p", { text: "No help is written for this widget yet." })); return kids; }
    if (got.kind === "native") {
      const dl = el("dl", { class: "ws-help-dl" });
      for (const [k, label] of SECTIONS) if (got.h[k]) dl.append(el("dt", { text: label }), el("dd", { text: got.h[k] }));
      kids.push(dl);
    } else {
      kids.push(...helpContent(got.h));
      if (got.note) kids.push(el("p", { class: "ws-help-note" }, el("strong", { text: "In 3.0: " }), got.note));
      kids.push(el("p", { class: "small muted", text: "A classic screen runs inside this widget as it did in 2.x. It is not linked (the dashed dot): it keeps its own stock." }));
    }
    let facts = [];
    try { facts = [dataLine(), ...(w.helpFacts() || [])].filter(Boolean); } catch (e) { console.error(e); }
    if (facts.length) kids.push(el("div", { class: "ws-help-now" }, el("h4", { text: "Right now" }),
      el("ul", { class: "plain small" }, facts.map(([k, v]) => el("li", {}, el("strong", { text: `${k}: ` }), String(v))))));
    return kids;
  }

  // Below the icon, or above it when there is more room there; inside the window either way (a long text scrolls).
  function place(btn, pop) {
    const r = btn.getBoundingClientRect();
    const width = Math.min(420, window.innerWidth - 16);
    pop.style.width = `${width}px`;
    pop.style.left = `${Math.max(8, Math.min(r.right - width, window.innerWidth - width - 8))}px`;
    pop.style.maxHeight = "none"; pop.style.top = "0px"; pop.style.bottom = "";
    const need = pop.scrollHeight, below = window.innerHeight - r.bottom - 12, above = r.top - 12;
    if (need > below && above > below) {
      pop.style.top = ""; pop.style.bottom = `${window.innerHeight - r.top + 6}px`; pop.style.maxHeight = `${Math.max(140, above)}px`;
    } else {
      pop.style.top = `${r.bottom + 6}px`; pop.style.maxHeight = `${Math.max(140, below)}px`;
    }
  }

  function show(w, btn, pinned) {
    if (open && open.btn === btn) {
      if (pinned && !open.pinned) { open.pinned = true; open.pop.classList.add("pinned"); open.pop.focus(); }
      return;
    }
    close(false);
    const id = `ws-help-${++uid}`;
    const pop = el("div", { class: `ws-help-pop${pinned ? " pinned" : ""}`, id, role: "dialog", "aria-modal": "false", "aria-labelledby": `${id}-t`, tabindex: "-1" },
      ...content(w, id));
    pop.addEventListener("pointerdown", e => e.stopPropagation());
    pop.addEventListener("mouseleave", () => { if (open && !open.pinned && !btn.matches(":hover")) close(false); });
    document.body.append(pop);
    place(btn, pop);
    btn.setAttribute("aria-expanded", "true");
    btn.setAttribute("aria-controls", id);
    open = { w, btn, pop, pinned };
    if (pinned) pop.focus();
  }

  function close(returnFocus) {
    if (!open) return;
    const { btn, pop } = open;
    open = null;
    pop.remove();
    btn.setAttribute("aria-expanded", "false");
    btn.removeAttribute("aria-controls");
    if (returnFocus && btn.isConnected) { quiet = btn; btn.focus(); quiet = null; }
  }

  // The icon. It is a button, so Enter and Space click it; the grid does not start a drag from a button.
  function helpButton(w) {
    const label = `About ${w.def.title}`;
    const b = el("button", { class: "ws-wbtn ws-info", type: "button", title: label, "aria-label": label, "aria-haspopup": "dialog", "aria-expanded": "false" }, "i");
    let touch = false, viaKey = false;
    b.addEventListener("pointerdown", e => { touch = e.pointerType !== "mouse"; e.stopPropagation(); });
    b.addEventListener("keydown", e => { if (e.key === "Enter" || e.key === " ") viaKey = true; });
    b.addEventListener("mouseenter", () => { if (!open || !open.pinned) show(w, b, false); });
    b.addEventListener("mouseleave", () => {
      setTimeout(() => { if (open && open.btn === b && !open.pinned && !open.pop.matches(":hover") && document.activeElement !== b) close(false); }, 120);
    });
    b.addEventListener("focus", () => { if (quiet !== b && b.matches(":focus-visible") && (!open || open.btn !== b)) show(w, b, false); });
    b.addEventListener("blur", e => { if (open && open.btn === b && !open.pinned && !(e.relatedTarget && open.pop.contains(e.relatedTarget))) close(false); });
    b.addEventListener("click", e => {
      e.preventDefault(); e.stopPropagation();
      if (open && open.btn === b && open.pinned) close(viaKey);
      else show(w, b, true);
      touch = false; viaKey = false;
    });
    return b;
  }

  // Escape closes the panel first (before it restores a maximized widget); a click or tap elsewhere closes it; so do
  // switching or closing workspaces and closing its widget (ws-core.js calls closeHelp).
  window.addEventListener("keydown", e => { if (e.key === "Escape" && open) { close(true); e.stopPropagation(); e.preventDefault(); } }, true);
  document.addEventListener("pointerdown", e => { if (open && !open.pop.contains(e.target) && !open.btn.contains(e.target)) close(false); });
  document.addEventListener("focusin", e => { if (open && open.pinned && !open.pop.contains(e.target) && e.target !== open.btn) close(false); });
  // Scrolling the page: a preview goes; a panel kept open follows its icon, and goes once the icon is off the screen.
  window.addEventListener("scroll", e => {
    if (!open || (e.target instanceof Node && open.pop.contains(e.target))) return;
    if (!open.pinned) { close(false); return; }
    const r = open.btn.getBoundingClientRect();
    if (!open.btn.isConnected || r.bottom < 0 || r.top > window.innerHeight || (r.width === 0 && r.height === 0)) close(false);
    else place(open.btn, open.pop);
  }, true);
  window.addEventListener("resize", () => { if (open && open.pinned && open.btn.isConnected) place(open.btn, open.pop); else close(false); });

  Object.assign(WS, {
    HELP: H, HELP_SECTIONS: SECTIONS, helpFor, missingHelp, helpButton,
    closeHelp: w => { if (open && (!w || open.w === w)) close(false); },
    helpOpen: () => (open ? { type: open.w.type, pinned: open.pinned } : null),
  });
})();
