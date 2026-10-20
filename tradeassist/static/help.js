"use strict";
/* Trade Assistant 2.15: an info icon beside every screen's title (and beside the new controls) that says what the
   screen is for and what each part of it does. It opens when the pointer is on the icon or the icon is clicked
   (or focused from the keyboard), and closes when the pointer leaves the icon, on Escape, or on a tap elsewhere.
   Loaded last: show() in app.js calls attachHelp when a screen is first built. */

const HELP = {
  morning: { lead: "The trades the market's open offers, ranked: the 250 largest US stocks read before the open and through the morning session, the most volatile hours.",
    items: [
      ["Markets before the open", "Overseas markets overnight (Asia closed, Europe trading), the US index funds and the sector funds before the open. Context: the overseas move is already in the US funds' prices."],
      ["All 250, ranked", "Every stock with its move since the close (in its own daily ranges), its own part of the move beyond what SPY explains, its volume, news for the biggest movers, a direction, the past record behind it, and a score from 0 to 100. Hover a score for the points behind it."],
      ["After the open", "In the session the sharpest swings since the open are found and judged by the rest-of-the-day record; VWAP and the opening range are checked for the biggest ones. Turn on Refresh in the morning session to rescan every few minutes until the end of the morning (11:30 unless set)."],
      ["Day trade or swing", "Day trades are out by 15:55 the same session. Swing trades are held up to five trading days."],
      ["Set budget", "Splits money across the best opportunities with the blend optimizer: most dollars, best return for the risk, or lowest risk. Take one card, or the whole blend, to Analyze opportunity or to paper trading."],
      ["Trade cards", "The best 20 get a card: entry, stop, targets, shares sized to your risk per trade, the best call or put and the exact limit price to type."],
      ["Reversal watch, scorecard, email", "Stocks about to confirm a turn (with a price to watch), how recent picks did, and the 7:00 email."]],
    foot: "Scores and records are past frequencies, not forecasts. Nothing here places an order." },
  live: { lead: "Stock-moving news as it arrives, the price's answer, and every so often a ranked digest of the opportunities the news has made.",
    items: [
      ["Start listening", "Press releases (RTPR), 8-K filings (SEC) and headlines (Finnhub), as they arrive. Each one's stock is watched for a few minutes; when the price confirms the news, it becomes an opportunity with a trade card."],
      ["The digest", "Every 15, 30 or 60 minutes (or over the whole session so far) the confirmed news and the movers with news found are ranked as day trades, each scored on the reaction's size, speed, tone, volume and the rest-of-the-day record."],
      ["Set budget", "Split money across the digest's best with the blend optimizer, and take one or the blend to Analyze opportunity or to paper trading."],
      ["Why is it moving?", "Stocks that moved with nothing in the feed are searched for news; a big move with no news found is flagged."],
      ["Your holdings", "News on a stock you hold that moves against you is flagged, and can be sent by browser notification or email."]],
    foot: "News moves are fastest in their first seconds; this shows the news and the price together. Nothing here places an order." },
  today: { lead: "Which way the market leans and which stocks lean long or short, before you pick one to analyze.",
    items: [["The regime", "The market's state and what it means for new trades."], ["Your limits", "The daily loss stop and how much risk is open."],
      ["Leaning stocks", "Up to eight leaning long and eight leaning short, from the trend engine. Pick one to analyze it."]] },
  market: { lead: "The market's make-up and the news.",
    items: [["Make-up", "Indexes, the VIX, the regime, sectors, breadth and the put/call ratio."], ["Headlines", "Market and stock headlines with their tone."],
      ["AI briefs", "With an Anthropic key, Claude writes a market brief with sources (counts against your daily AI limit)."]] },
  ideas: { lead: "The highest-graded trades to buy and to short across up to 250 large companies, each a 20-day swing trade.",
    items: [["Scan", "Each stock's trend, reversal and trade grade are read in turn; the best long and short trades are listed with stop, targets and size."],
      ["Analyze opportunity", "Opens any idea on Analyze opportunity with its levels filled in."],
      ["Simulate", "How often the stop and targets are reached on 20,000 price paths, and what each outcome makes or loses, with shares and with a contract."],
      ["Set budget", "Splits money across the best ideas (or the ones you tick) with the blend optimizer, and takes the blend to Analyze opportunity or paper trading."]],
    foot: "Grades count evidence; they are not chances. Nothing here places an order." },
  scanner: { lead: "Technical signals across the watchlist or 100 large companies, and unusual options activity.",
    items: [["Signals", "RSI, MACD, moving averages, 52-week levels and volume, each with how often it worked before (a past frequency, not a forecast)."],
      ["Options flow", "Contracts trading unusually against their open interest."], ["Open", "Any row opens on Analyze opportunity."]] },
  analyze: { lead: "One stock, or a blend of several: which way each is trending and why, your thesis graded, the calls or puts that fit, and for a blend the split, its range of results and its risk.",
    items: [
      ["The basket", "Opportunities sent from the Morning plan, Live news or Trade ideas (or added by you) appear as chips. Click a stock for its full analysis with its levels filled in; click Blend for the whole basket."],
      ["The blend", "The split across the stocks, its expected profit and range, the chance of a profit, the average loss in the worst tenth of outcomes, how the stocks move together, sectors, what a market drop would do. Edit the units and evaluate, or re-optimize; send it to paper trading."],
      ["Trend and thesis", "The trend engine's reading with its evidence; your thesis (direction, invalidation, targets, holding days) graded, and contracts that fit it."],
      ["Chart and what-if", "The interactive chart with similar patterns and possible outcomes; the what-if model of overseas markets, rates, oil and news, with a position valued under its scenario."]],
    foot: "Scores count evidence; they are not chances. Nothing here places an order." },
  price: { lead: "The limit price to type for an option: a ladder from a fair price worked out from the quote's evidence, never above the midpoint for a buyer.",
    items: [["The ladder", "Patient, fair and most-to-pay prices with the evidence behind the fair price."], ["Checks", "The trade against a list of known ways option buyers lose money."],
      ["Live quotes", "Refreshed every few seconds from your data source."]] },
  simulate: { lead: "A position's profit or loss across stock prices, dates and volatility changes, and the strategy builder.",
    items: [["Profit and loss", "Up to four legs valued with Black-76."], ["Strategy builder", "Type \"AAPL iron condor 45 days 16 delta 5 wide\" or pick a template; real listed contracts are used."]] },
  lab: { lead: "What option prices imply: volatility by expiration and strike, the expected move, skew, the earnings move, and a Monte Carlo check of any contract.",
    items: [["Volatility", "Implied against realized volatility and the term structure."], ["Monte Carlo", "A contract's value, chance of finishing in the money and of touching the strike."]] },
  positions: { lead: "Stock you are long or short and the calls and puts you hold, with correct accounting from their fills.",
    items: [["Record", "Enter fills by hand or import them; corrections keep a reason."], ["Marks", "Each position valued now, with what closing would get and how fresh the price is."]],
    foot: "Paper trades never appear here: they live on Paper trading." },
  portfolio: { lead: "The risk across everything you hold: exposure, open risk on a stated basis, betas, correlations, a bad-day estimate and stress tests.",
    items: [["Exposure", "Gross and net, long and short."], ["Stress", "What given market moves would do to the book."]] },
  paper: { lead: "Trade the recommendations, or your own ideas, with pretend money, and see how they do over the horizon you choose.",
    items: [
      ["Automated", "Give a budget: it takes the best opportunities from the Morning plan, the news digest, Trade ideas or all three, splits the money with the blend optimizer and trades on paper. Every few minutes it sells what has faded when something fresher scores well clear of it, and puts idle cash to work. Every action is logged with its reason."],
      ["Manual", "Place trades yourself: shares, a short, a call or a put, or import a blend. Close all or part whenever you like; stops, targets and horizons still apply."],
      ["Fills", "Realistic: shares cross the spread with your trading cost, an option fills only when the ask reaches the card's limit. Conservative: options buy at the ask. Midpoint: optimistic."],
      ["Results", "Each portfolio's value over time against SPY, automated and manual side by side; return, drawdown, win rate, average R, by source and instrument; the full trade log."],
      ["Replay", "Run a stored recommendation run over a past horizon on price history, without waiting."],
      ["While the app is closed", "Portfolios are valued only while the app runs; on restart the gap is replayed from price history to catch stops and targets hit meanwhile."]],
    foot: "Paper money only. It never touches your real positions or risk limits, and nothing here places an order. Real fills are usually worse." },
  trades: { lead: "Your Robinhood history, imported from the account activity report and analysed on this computer.",
    items: [["Results", "By week, month, quarter or year."], ["Strategies and risk", "The strategies you used and the risk each carried."], ["Better or worse", "The kinds of trade that went better or worse than your average."]] },
  journal: { lead: "The option journal: what was planned, what was done, and how it turned out.",
    items: [["Attempts", "Each limit tried and whether it filled."], ["Closes", "Exits with their result."]] },
  patterns: { lead: "Chart patterns tested on real history before you rely on them.", items: [["Frequencies", "How often each pattern was followed by a move, with intervals."]] },
  backtest: { lead: "Condition-based backtests and walk-forward tests, with slippage, commission and a seeded Monte Carlo.", items: [["Runs", "Settings and data are recorded so two runs can be compared."]] },
  research: { lead: "The facts worth knowing before you look at an option chain: price, volatility, the market, earnings and filings.", items: [] },
  health: { lead: "Whether the numbers can be trusted: the data source, how fresh each price is, what could not be valued, and a reconciliation of every stored figure against its fills.", items: [] },
};

// Help for individual controls (the i beside a field).
const HELP_BITS = {
  budget: "The money to split. It can be any amount; your per-trade and open-risk limits are measured against ACCOUNT_SIZE in .env.",
  goal: "Most dollars: the most expected profit the caps allow. Best return for the risk: the most expected profit while the average loss in the worst tenth of outcomes stays within the tail limit. Lowest risk: a much tighter tail limit (0.5% of the budget), preferring trades that add the least tail risk for their profit.",
  tail: "For Best return for the risk: the most the average loss in the worst tenth of the model's outcomes may be, as a share of the budget.",
  maxShare: "The most of the budget any one trade may take.",
  limits: "Keep each trade's loss at the stop (the whole premium for an option) within your risk per trade, and everything open within your open-risk limit, as on a real ticket. Real-money splits always do; a paper portfolio may lift them, measured on its own positions.",
  use: "Blend the best-scored opportunities, or only the rows you have ticked.",
  instruments: "Which instruments the split may use. Calls and puts need a contract: the best ones are priced on demand (a few seconds each).",
  edge: "The past average of the same kind of trade, the trade's way, after the round trip's cost and pulled toward zero when the record has few cases: the only drift the model is given. Hover a row's edge for its record.",
  digest: "How often the digest is rebuilt while Live news is listening. Session ranks everything since the open, refreshed every 30 minutes.",
  horizon: "Day trades are out by 15:55 New York time the same session; swing trades are held to their horizon date.",
  refresh: "Rebuild the plan every few minutes from the open to the end of the morning session, while the app runs. The rescan reuses each stock's history and the cards built in the last 15 minutes.",
  fill: "How paper orders fill. Realistic: shares cross the spread, an option waits for the ask to reach its limit. Conservative: options buy at the ask at once. Midpoint: everything at the midpoint (optimistic).",
  rotate: "Automated portfolios sell a position whose opportunity has faded when a fresher one scores at least 15 points higher.",
  replay: "Runs a stored list over a past horizon on price history: entries at the open of the first bar after the run (the next session's open on daily bars), stops and targets on bars, options valued by the model.",
};

let INFO_UID = 0;
let INFO_OPEN = null;

function helpContent(h) {
  if (typeof h === "string") return [el("p", { text: h })];
  const out = [el("p", { class: "info-lead", text: h.lead })];
  if (h.items && h.items.length) out.push(el("ul", {}, h.items.map(([k, v]) => el("li", {}, el("strong", { text: /[.?!]$/.test(k) ? `${k} ` : `${k}. ` }), v))));
  if (h.foot) out.push(el("p", { class: "info-foot", text: h.foot }));
  return out;
}

// Below the icon, or above it when the text needs more room than is left below and there is more above; never
// taller than the room on that side (a long text scrolls inside it).
function placeInfo(btn, pop) {
  const r = btn.getBoundingClientRect();
  const w = Math.min(440, window.innerWidth - 24);
  pop.style.width = `${w}px`;
  pop.style.left = `${Math.max(12, Math.min(r.left + r.width / 2 - 24, window.innerWidth - w - 12))}px`;
  pop.style.maxHeight = "none";
  pop.style.bottom = "";
  pop.style.top = "0px";
  const need = pop.scrollHeight;
  const below = window.innerHeight - r.bottom - 20, above = r.top - 20;
  if (need > below && above > below) {
    pop.style.top = "";
    pop.style.bottom = `${window.innerHeight - r.top + 8}px`;
    pop.style.maxHeight = `${Math.max(120, above)}px`;
  } else {
    pop.style.top = `${r.bottom + 8}px`;
    pop.style.maxHeight = `${Math.max(120, below)}px`;
  }
}

function closeInfo() {
  if (!INFO_OPEN) return;
  INFO_OPEN.pop.classList.remove("open", "pinned");
  INFO_OPEN.btn.setAttribute("aria-expanded", "false");
  INFO_OPEN = null;
}

// The icon: `content` is a HELP entry, a HELP_BITS key or text. With a mouse it opens on hover (or a click) and goes as
// soon as the pointer leaves the icon. A tap on a touch screen (no hover there) pins it open, so a long text can be
// scrolled; a tap anywhere else, Escape or scrolling the page closes it.
function infoIcon(content, label = "What this does") {
  const h = typeof content === "string" && HELP_BITS[content] ? HELP_BITS[content] : content;
  const id = `info-${++INFO_UID}`;
  const btn = el("button", { class: "info-btn", type: "button", "aria-label": label, "aria-expanded": "false", "aria-describedby": id }, "i");
  const pop = el("div", { class: "info-pop", role: "tooltip", id, tabindex: "-1" }, helpContent(h));
  let touch = false;
  const open = (pin = false) => {
    if (INFO_OPEN && INFO_OPEN.btn !== btn) closeInfo();
    if (!pop.isConnected) document.body.append(pop);
    pop.classList.add("open");
    pop.classList.toggle("pinned", pin);
    placeInfo(btn, pop);
    btn.setAttribute("aria-expanded", "true");
    INFO_OPEN = { btn, pop, pinned: pin };
  };
  const pinned = () => INFO_OPEN && INFO_OPEN.btn === btn && INFO_OPEN.pinned;
  btn.addEventListener("pointerdown", e => { touch = e.pointerType !== "mouse"; });
  btn.addEventListener("mouseenter", () => { if (!pinned()) open(); });
  btn.addEventListener("mouseleave", () => { if (!pinned()) closeInfo(); });   // it goes as soon as the pointer leaves the icon
  btn.addEventListener("focus", () => { if (!INFO_OPEN || INFO_OPEN.btn !== btn) open(); });
  btn.addEventListener("blur", e => { if (pinned() && e.relatedTarget && pop.contains(e.relatedTarget)) return; closeInfo(); });
  pop.addEventListener("blur", e => { if (e.relatedTarget !== btn) closeInfo(); });
  btn.addEventListener("click", e => {
    e.preventDefault();
    e.stopPropagation();
    if (touch) { if (pinned()) closeInfo(); else open(true); return; }
    if (pop.classList.contains("open") && !btn.matches(":hover")) closeInfo(); else open();
  });
  return el("span", { class: "info-wrap" }, btn);
}

document.addEventListener("keydown", e => { if (e.key === "Escape") closeInfo(); });
document.addEventListener("click", e => { if (INFO_OPEN && !INFO_OPEN.btn.contains(e.target) && !INFO_OPEN.pop.contains(e.target)) closeInfo(); });
window.addEventListener("scroll", e => { if (INFO_OPEN && e.target instanceof Node && INFO_OPEN.pop.contains(e.target)) return; closeInfo(); }, true);
window.addEventListener("resize", closeInfo);

// Beside a screen's title, once, when the screen is first built.
function attachHelp(name, view) {
  const h = HELP[name];
  if (!h || !view || view.dataset.help) return;
  const title = view.querySelector(":scope > h2, :scope > .panel-head > h2, :scope > .title-row > h2");
  if (!title) return;
  view.dataset.help = "1";
  title.append(" ", infoIcon(h, `About ${title.textContent.trim()}`));
}

// A label with its info icon, for the new controls.
function withInfo(text, key) { return el("span", { class: "with-info" }, text, " ", infoIcon(key)); }
