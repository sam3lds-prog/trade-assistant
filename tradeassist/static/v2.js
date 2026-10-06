"use strict";
/* Trade Assistant V2 screens. Loaded after app.js, whose helpers (el, api, field, table, money, ...) it uses.
   Each screen registers itself in BUILDERS; app.js starts the page once both scripts have run. */

const featureOn = name => !!(state.status && state.status.features && state.status.features[name]);
const signedMoney = (v, d = 0) => isNum(v) ? (v > 0 ? "+" : "") + money(v, d) : "n/a";
const qtyText = v => isNum(v) ? v.toLocaleString("en-US", { maximumFractionDigits: 4 }) : "n/a";
const optNum = input => input.value === "" ? null : Number(input.value);
const timeText = iso => {
  if (!iso) return "n/a";
  return new Date(iso).toLocaleString("en-US", { timeZone: "America/New_York", month: "short", day: "numeric", year: "numeric", hour: "numeric", minute: "2-digit" }) + " ET";
};
const POS_STATUS = { PLANNED: ["info", "Planned"], OPEN: ["pass", "Open"], PARTIALLY_CLOSED: ["warn", "Partly closed"], CLOSED: ["info", "Closed"], CANCELLED: ["info", "Cancelled"] };
const THESIS = { INTACT: ["pass", "Intact"], INVALIDATED: ["fail", "Invalidated"], TARGET_1_REACHED: ["pass", "Target 1 reached"],
  TARGET_2_REACHED: ["pass", "Target 2 reached"], NO_INVALIDATION: ["warn", "No invalidation set"], UNKNOWN: ["info", "No price"] };
const SIDE_WORDS = { BUY: "Buy", SELL: "Sell", SELL_SHORT: "Sell short", BUY_TO_COVER: "Buy to cover" };
const SETUPS = ["breakout", "pullback", "trend continuation", "reversal", "range", "earnings", "mean reversion", "other"];
const tagOf = (map, key) => { const [s, w] = map[key] || ["info", key || "n/a"]; return statusTag(s, w); };
const textInput = (attrs = {}) => el("input", { type: "text", autocomplete: "off", ...attrs });
const numInput = (attrs = {}) => el("input", { type: "number", step: "0.01", min: "0", ...attrs });
// <input type="datetime-local"> gives a time with no zone; the server reads that as New York time.
const timeInput = () => el("input", { type: "datetime-local", "aria-label": "When (New York time)" });

/* ---------- view: positions ---------- */
function buildPositions() {
  const error = el("div", { class: "error", role: "alert" });
  const summary = el("div");
  const listBox = el("div", {}, el("p", { class: "muted", text: "Loading…" }));
  const detailBox = el("div");
  const filter = segmented("posfilter", [["open", "Open"], ["planned", "Planned"], ["closed", "Closed"], ["cancelled", "Cancelled"], ["all", "All"]], "open");
  let selected = null;

  async function refresh() {
    listBox.classList.add("refetching");
    try {
      const d = await api(`/v2/positions?status=${filter.value()}`);
      error.textContent = "";
      summary.replaceChildren(positionSummary(d.positions, filter.value()));
      listBox.replaceChildren(positionTable(d.positions, p => openDetail(p.id)));
    } catch (e) { error.textContent = e.message; }
    finally { listBox.classList.remove("refetching"); }
  }
  async function openDetail(id) {
    selected = id;
    detailBox.replaceChildren(el("div", { class: "panel" }, el("p", { class: "muted", text: "Loading position…" })));
    try { showDetail(await api(`/v2/positions/${id}`)); }
    catch (e) { detailBox.replaceChildren(el("div", { class: "panel" }, el("p", { class: "error", text: e.message }))); }
  }
  function showDetail(p) {
    selected = p.id;
    detailBox.replaceChildren(positionDetail(p, async next => { showDetail(next); await refresh(); }));
    detailBox.scrollIntoView({ block: "nearest", behavior: "smooth" });
  }
  filter.addEventListener("change", refresh);
  const form = newPositionForm(async p => { await refresh(); showDetail(p); });

  const view = el("section", {},
    el("h2", { text: "Positions" }),
    el("p", { class: "lede", text: "Stock you are long or short, and the calls and puts you hold, in one place. Each position is built from its fills, so partial entries and exits, average cost, fees and profit or loss come out right. Record what you did at your broker here; this app never places orders." }),
    error,
    el("div", { class: "panel" }, el("div", { class: "panel-head" }, el("h3", { text: "Your positions" }), filter), summary, listBox),
    detailBox, form);
  view.onShow = () => { refresh(); if (selected) openDetail(selected); };
  return view;
}

function positionSummary(rows, which) {
  if (!rows.length) return el("div");
  const sum = key => rows.reduce((a, p) => a + (isNum(p.pnl[key]) ? p.pnl[key] : 0), 0);
  const invalid = rows.filter(p => p.thesis_check.status === "INVALIDATED").length;
  const longs = rows.filter(p => p.bias > 0 && p.status !== "PLANNED").length, shorts = rows.filter(p => p.bias < 0 && p.status !== "PLANNED").length;
  return el("div", { class: "metrics", style: "margin-bottom:12px" },
    metric(String(rows.length), which === "all" ? "Positions" : `${which[0].toUpperCase()}${which.slice(1)} positions`),
    metric(`${longs} / ${shorts}`, "Gain if the stock rises / falls"),
    metric(signedMoney(sum("unrealized")), "Unrealized profit or loss"),
    metric(signedMoney(sum("realized")), "Realized, after fees"),
    metric(String(invalid), "Through their invalidation price"));
}

function positionTable(rows, onRow) {
  if (!rows.length) return el("p", { class: "muted", text: "None here. Add a position below, or record a trade in the option journal and it appears here too." });
  return table([
    { label: "Position", cell: p => el("span", {}, el("strong", { text: p.description }), p.legacy_trade_id ? el("span", { class: "small muted", text: " (journal)" }) : null) },
    { label: "Status", cell: p => tagOf(POS_STATUS, p.status) },
    { label: "Open", num: true, cell: p => qtyText(p.ledger.open_quantity) },
    { label: "Average entry", num: true, cell: p => money(p.ledger.average_entry_price) },
    { label: "Now", num: true, cell: p => p.mark ? (isNum(p.mark.price) ? money(p.mark.price) : el("span", { class: "muted", title: p.mark.error || "", text: "no price" })) : "" },
    { label: "Unrealized", num: true, cell: p => p.status === "PLANNED" ? "" : signedMoney(p.pnl.unrealized) },
    { label: "Realized", num: true, cell: p => p.status === "PLANNED" ? "" : signedMoney(p.pnl.realized) },
    { label: "R", num: true, cell: p => isNum(p.pnl.r_multiple) ? `${p.pnl.r_multiple > 0 ? "+" : ""}${num(p.pnl.r_multiple, 2)}R` : "" },
    { label: "Plan", cell: p => ["CLOSED", "CANCELLED"].includes(p.thesis_check.status) ? "" : tagOf(THESIS, p.thesis_check.status) },
  ], rows, { onRow });
}

function positionDetail(p, onChange) {
  const error = el("div", { class: "error", role: "alert" });
  const act = (button, fn) => withBusy(button, error, async () => onChange(await fn()));
  const led = p.ledger, pnl = p.pnl, leg = p.legs[0], unit = p.instrument_type === "OPTION" ? "contract" : "share";
  const box = el("div", { class: "panel" });
  box.append(el("div", { class: "panel-head" }, el("h3", {}, `${p.description} `, tagOf(POS_STATUS, p.status)),
    el("span", { class: "small muted", text: `Position ${p.id} · opened ${p.opened_at ? timeText(p.opened_at) : "not yet"}${p.closed_at ? ` · closed ${timeText(p.closed_at)}` : ""}` })));
  if (p.data_source === "demo") box.append(banner("warn", "Recorded while the app was on demo data: the prices it saw were made up."));
  const mk = p.mark || {};
  box.append(el("div", { class: "metrics" },
    metric(qtyText(led.open_quantity), `Open ${unit}s (of ${qtyText(led.max_open_quantity || p.quantity)} at most)`),
    metric(money(led.average_entry_price), "Average entry price"),
    metric(isNum(led.average_exit_price) ? money(led.average_exit_price) : "n/a", "Average exit price"),
    metric(isNum(mk.price) ? money(mk.price) : "n/a", mk.source ? `Now · ${mk.source}` : "Now"),
    metric(signedMoney(pnl.unrealized), "Unrealized"),
    metric(signedMoney(pnl.realized), "Realized, after fees"),
    metric(money(pnl.fees), "Fees paid"),
    metric(isNum(pnl.r_multiple) ? `${pnl.r_multiple > 0 ? "+" : ""}${num(pnl.r_multiple, 2)}R` : "n/a", isNum(pnl.initial_risk) ? `Total in units of the ${money(pnl.initial_risk, 0)} risked` : "R multiple (set an invalidation or risk amount)"),
    isNum(p.days_held) ? metric(String(p.days_held), "Days held") : null));
  if (mk.error) box.append(el("p", { class: "small error", text: `No current price: ${mk.error}` }));
  if (leg && isNum(mk.delta)) box.append(el("p", { class: "small muted", text: `Option now: bid ${money(mk.bid)}, ask ${money(mk.ask)}, delta ${num(mk.delta, 2)}, theta ${money(mk.theta)} a day, implied volatility ${pct(100 * mk.iv, 1)}, ${mk.dte} days to expiration. Stock ${money(mk.underlying)}.` }));
  const tc = p.thesis_check;
  if (tc.text) box.append(banner(tc.status === "INVALIDATED" ? "fail" : tc.status === "INTACT" || tc.status.startsWith("TARGET") ? "pass" : "warn", tc.text));

  // The plan: why, where it is wrong, where it pays.
  const thesis = el("textarea", { "aria-label": "Thesis", rows: 2 }); thesis.value = p.thesis || "";
  const inv = numInput({ value: p.invalidation_price ?? "" }), t1 = numInput({ value: p.target_1 ?? "" }), t2 = numInput({ value: p.target_2 ?? "" });
  const risk = numInput({ value: p.risk_amount ?? "", step: "1" }), horizon = el("input", { type: "number", min: 1, step: 1, value: p.intended_horizon ?? "" });
  const setup = textInput({ value: p.setup_type || "", list: "setup-types" });
  const notes = el("textarea", { "aria-label": "Notes", rows: 2 }); notes.value = p.notes || "";
  const savePlan = el("button", { class: "plain", text: "Save the plan" });
  savePlan.addEventListener("click", () => act(savePlan, () => {
    const body = { thesis: thesis.value, setup_type: setup.value, notes: notes.value };
    for (const [k, input] of [["invalidation_price", inv], ["target_1", t1], ["target_2", t2], ["risk_amount", risk], ["intended_horizon", horizon]]) {
      const v = optNum(input); if (v !== null) body[k] = k === "intended_horizon" ? Math.round(v) : v;
    }
    return api(`/v2/positions/${p.id}/plan`, body);
  }));
  const stockWord = p.instrument_type === "OPTION" ? "Stock price that proves it wrong" : "Invalidation price";
  box.append(el("h4", { text: "Plan" }),
    el("div", { class: "inline-form" }, field(stockWord, inv), field("Target 1", t1), field("Target 2", t2), field("Dollars at risk", risk),
      field("Horizon (trading days)", horizon), field("Setup", setup)),
    field("Thesis", thesis), field("Notes", notes), el("div", { class: "row", style: "margin-top:8px" }, savePlan));

  // Fills.
  box.append(el("h4", { text: "Fills" }));
  box.append(p.executions.length ? table([
    { label: "When", cell: x => timeText(x.executed_at) },
    { label: "Action", cell: x => SIDE_WORDS[x.side] || x.side },
    { label: "Quantity", num: true, cell: x => qtyText(x.quantity) },
    { label: "Price", num: true, cell: x => money(x.price) },
    { label: "Fees", num: true, cell: x => money(x.fees) },
    { label: "Recorded from", cell: x => ({ MANUAL: "you", IMPORT: "option journal", ROBINHOOD: "Robinhood" })[x.source] || x.source },
    { label: "Note", cell: x => x.note || "" }], p.executions) : el("p", { class: "small muted", text: "No fills yet: this is a plan." }));
  if (p.legacy_trade_id) {
    box.append(el("p", { class: "small muted", text: "This position mirrors an entry in the option journal. Log its fills there and they appear here." }));
  } else if (p.status !== "CANCELLED" && p.status !== "CLOSED") {
    const sides = p.direction === "LONG" ? [["BUY", "Buy"], ["SELL", "Sell"]] : [["SELL_SHORT", "Sell short"], ["BUY_TO_COVER", "Buy to cover"]];
    const side = el("select", { "aria-label": "Action" }, sides.map(([v, t]) => el("option", { value: v, text: t })));
    if (led.open_quantity > 0) side.value = sides[1][0];
    const qty = el("input", { type: "number", min: 0, step: p.instrument_type === "OPTION" ? 1 : "any", value: led.open_quantity > 0 ? led.open_quantity : p.quantity });
    const price = numInput({ placeholder: isNum(mk.price) ? mk.price.toFixed(2) : "" }), fees = numInput({ value: 0 }), when = timeInput(), note = textInput({ placeholder: "optional" });
    const add = el("button", { class: "primary", text: "Record the fill" });
    add.addEventListener("click", () => act(add, () => {
      if (price.value === "") throw new Error("Enter the price you got.");
      return api(`/v2/positions/${p.id}/executions`, { side: side.value, quantity: Number(qty.value), price: Number(price.value),
        fees: Number(fees.value || 0), executed_at: when.value || null, note: note.value });
    }));
    box.append(el("div", { class: "inline-form" }, field("Action", side), field(`${unit[0].toUpperCase()}${unit.slice(1)}s`, qty),
      field(p.instrument_type === "OPTION" ? "Price (per share)" : "Price per share", price), field("Fees", fees), field("When (blank for now)", when), field("Note", note), add));
  }

  // What the market looked like at plan, entry, exit and reviews.
  box.append(el("h4", { text: "Snapshots" }));
  box.append(p.snapshots.length ? table([
    { label: "Taken", cell: s => `${s.snapshot_type[0]}${s.snapshot_type.slice(1).toLowerCase()} · ${timeText(s.timestamp)}` },
    { label: "Stock", num: true, cell: s => money(s.underlying_price) },
    { label: "RSI", num: true, cell: s => num(s.rsi_14, 0) },
    { label: "MACD hist.", num: true, cell: s => num(s.macd_histogram, 2) },
    { label: "50-day", num: true, cell: s => money(s.sma_50) },
    { label: "Swings", cell: s => s.structure_state || "n/a" },
    { label: "Trend", cell: s => s.trend_state ? s.trend_state.replaceAll("_", " ").toLowerCase() : "n/a" },
    { label: "Market", cell: s => s.market_regime ? s.market_regime.replaceAll("_", " ").toLowerCase() : "n/a" },
    ...(leg ? [{ label: "Delta", num: true, cell: s => num(s.delta, 2) }, { label: "IV", num: true, cell: s => isNum(s.iv) ? pct(100 * s.iv, 0) : "n/a" }] : []),
    { label: "Daily bar", cell: s => s.bar_status ? `${s.bar_status.toLowerCase()} · ${s.data_timestamp}` : (s.data_timestamp || "n/a") },
  ], p.snapshots) : el("p", { class: "small muted", text: "None yet." }));

  const buttons = el("div", { class: "row", style: "margin-top:12px" });
  const snap = el("button", { class: "plain", text: "Take a review snapshot now" });
  snap.addEventListener("click", () => act(snap, async () => { await api(`/v2/positions/${p.id}/snapshot`, {}); return api(`/v2/positions/${p.id}`); }));
  buttons.append(snap);
  if (leg && led.open_quantity > 0 && state.openSimulation) {
    const sim = el("button", { class: "plain", text: "Simulate what is open" });
    sim.addEventListener("click", () => state.openSimulation(p.symbol, [{ side: "buy", contracts: led.open_quantity, option_type: leg.option_type.toLowerCase(),
      expiration: leg.expiration, strike: leg.strike, entry_price: led.cost_basis }]));
    buttons.append(sim);
  }
  const gradeBox = el("div");
  if (p.instrument_type === "STOCK" && ["PLANNED", "OPEN", "PARTIALLY_CLOSED"].includes(p.status) && featureOn("trend_engine_v2")) {
    const grade = el("button", { class: "plain", text: "Grade this position" });
    grade.addEventListener("click", () => withBusy(grade, error, async () => { gradeBox.replaceChildren(gradeCard(await api("/v2/position/evaluate", { position_id: p.id }))); }));
    buttons.append(grade);
  }
  if (p.status === "PLANNED" && !p.legacy_trade_id) {
    const cancel = el("button", { class: "plain", text: "Cancel this plan" });
    cancel.addEventListener("click", () => act(cancel, () => api(`/v2/positions/${p.id}/cancel`, { note: "" })));
    buttons.append(cancel);
  }
  if (p.executions.length) box.append(positionAnalyticsBox(p));
  box.append(buttons, error, gradeBox);
  return box;
}

function newPositionForm(onSaved) {
  const error = el("div", { class: "error", role: "alert" });
  const kind = segmented("npkind", [["STOCK", "Stock"], ["OPTION", "Call or put"]], "STOCK");
  const dir = segmented("npdir", [["LONG", "Long"], ["SHORT", "Short"]], "LONG");
  const dirField = field("Direction", dir);
  const sym = el("input", { class: "sym", type: "text", "aria-label": "Ticker", autocomplete: "off", spellcheck: "false", placeholder: "AAPL" });
  const qty = el("input", { type: "number", min: 0, step: "any", value: 100 });
  const qtyField = field("Shares", qty);
  const otype = segmented("npotype", [["CALL", "Call"], ["PUT", "Put"]], "CALL");
  const exp = el("select", { "aria-label": "Expiration" }), strike = el("select", { "aria-label": "Strike" });
  const optRow = el("div", { class: "row hidden", style: "margin-top:10px" }, field("Type", otype), field("Expiration", exp), field("Strike", strike));
  const loadNote = el("div", { class: "loadnote small muted", "aria-live": "polite" });
  const price = numInput(), fees = numInput({ value: 0 }), when = timeInput();
  const inv = numInput(), t1 = numInput(), t2 = numInput(), risk = numInput({ step: "1" }), horizon = el("input", { type: "number", min: 1, step: 1 });
  const setup = textInput({ list: "setup-types" });
  const thesis = el("textarea", { "aria-label": "Thesis", rows: 2, placeholder: "Why this should work, in a sentence or two" });
  const save = el("button", { class: "primary", text: "Save the position" });
  let loadedFor = "", expirations = [];

  const isOption = () => kind.value() === "OPTION";
  function syncKind() {
    optRow.classList.toggle("hidden", !isOption());
    dirField.classList.toggle("hidden", isOption());
    qtyField.querySelector("span").textContent = isOption() ? "Contracts" : "Shares";
    qty.step = isOption() ? "1" : "any";
    if (isOption() && Number(qty.value) > 50) qty.value = 1;
    if (!isOption() && Number(qty.value) < 10) qty.value = 100;
    if (isOption()) loadExpirations().catch(e => { error.textContent = e.message; });
  }
  async function loadExpirations() {
    const symbol = sym.value.trim().toUpperCase();
    if (!symbol || symbol === loadedFor) return;
    loadNote.replaceChildren(el("span", { class: "spinner" }), el("span", { text: `Loading ${symbol} expirations…` }));
    try {
      const d = await api(`/api/expirations?symbol=${encodeURIComponent(symbol)}`);
      loadedFor = d.symbol || symbol; expirations = d.expirations;
      exp.replaceChildren(...expirations.map(e => el("option", { value: e.date, text: `${shortDate(e.date)} (${e.dte} days)` })));
      exp.value = (expirations.find(e => e.dte >= 30) || expirations[expirations.length - 1] || {}).date || "";
      await loadStrikes();
    } finally { loadNote.replaceChildren(); }
  }
  async function loadStrikes() {
    if (!exp.value) return;
    const keep = strike.value;
    strike.replaceChildren(el("option", { text: "Loading…" })); strike.disabled = true;
    const chain = await api(`/api/chain?symbol=${encodeURIComponent(loadedFor)}&expiration=${exp.value}&width_pct=1000`);
    const rows = chain.contracts.filter(c => c.option_type === otype.value().toLowerCase());
    strike.replaceChildren(...rows.map(c => el("option", { value: c.strike, text: `${num(c.strike, c.strike % 1 ? 2 : 0)}   ${c.bid == null && c.ask == null ? "no quote" : `bid ${num(c.bid)} / ask ${num(c.ask)}`}` })));
    const ks = rows.map(c => c.strike);
    strike.value = ks.includes(Number(keep)) ? keep : ks.reduce((a, k) => Math.abs(k - chain.spot) < Math.abs(a - chain.spot) ? k : a, ks[0]);
    strike.disabled = !ks.length;
  }
  kind.addEventListener("change", syncKind);
  sym.addEventListener("change", () => { if (isOption()) loadExpirations().catch(e => { error.textContent = e.message; }); });
  exp.addEventListener("change", () => loadStrikes().catch(e => { error.textContent = e.message; }));
  otype.addEventListener("change", () => loadStrikes().catch(e => { error.textContent = e.message; }));

  save.addEventListener("click", () => withBusy(save, error, async () => {
    const symbol = sym.value.trim().toUpperCase();
    if (!symbol) throw new Error("Enter a ticker.");
    if (!(Number(qty.value) > 0)) throw new Error("Enter how many shares or contracts.");
    const body = { symbol, instrument_type: kind.value(), direction: isOption() ? "LONG" : dir.value(), quantity: Number(qty.value),
      thesis: thesis.value, setup_type: setup.value };
    if (isOption()) {
      if (loadedFor !== symbol) await loadExpirations();
      if (!exp.value || strike.disabled || !strike.value) throw new Error("Choose an expiration and a strike.");
      body.option = { option_type: otype.value(), expiration: exp.value, strike: Number(strike.value) };
    }
    for (const [k, input] of [["invalidation_price", inv], ["target_1", t1], ["target_2", t2], ["risk_amount", risk], ["intended_horizon", horizon]]) {
      const v = optNum(input); if (v !== null) body[k] = k === "intended_horizon" ? Math.round(v) : v;
    }
    if (price.value !== "") body.entry = { price: Number(price.value), fees: Number(fees.value || 0), executed_at: when.value || null };
    const p = await api("/v2/positions", body);
    for (const input of [price, inv, t1, t2, risk, horizon, setup, thesis, when]) input.value = "";
    fees.value = 0;
    await onSaved(p);
  }));

  return el("div", { class: "panel" },
    el("h3", { text: "Add a position" }),
    el("datalist", { id: "setup-types" }, SETUPS.map(s => el("option", { value: s }))),
    el("div", { class: "row" }, field("What", kind), dirField, field("Ticker", sym), qtyField), optRow, loadNote,
    el("h4", { text: "Entry fill (leave the price blank to save a plan)" }),
    el("div", { class: "inline-form" }, field("Price", price), field("Fees", fees), field("When (blank for now)", when)),
    el("h4", { text: "Plan" }),
    el("div", { class: "inline-form" }, field("Invalidation price (of the stock)", inv), field("Target 1", t1), field("Target 2", t2),
      field("Dollars at risk", risk), field("Horizon (trading days)", horizon), field("Setup", setup)),
    field("Thesis", thesis),
    el("p", { class: "small muted", text: "The invalidation price is where the stock would prove the idea wrong. Without a dollars-at-risk figure, risk is the distance from your entry to that price times the size, or the whole premium for a call or put. Short calls and puts and spreads come later." }),
    el("div", { class: "row" }, save), error);
}

/* ---------- view: analyze a stock ---------- */
const TREND_TAG = { STRONG_UP: "pass", UP: "pass", UP_WEAKENING: "warn", NEUTRAL: "info", DOWN_DEVELOPING: "warn", DOWN: "fail", STRONG_DOWN: "fail" };
const TREND_FILL = { STRONG_UP: "color-mix(in oklab, var(--div-pos) 70%, var(--div-mid))", UP: "color-mix(in oklab, var(--div-pos) 42%, var(--div-mid))",
  UP_WEAKENING: "color-mix(in oklab, var(--div-pos) 18%, var(--div-mid))", NEUTRAL: "var(--div-mid)",
  DOWN_DEVELOPING: "color-mix(in oklab, var(--div-neg) 18%, var(--div-mid))", DOWN: "color-mix(in oklab, var(--div-neg) 42%, var(--div-mid))",
  STRONG_DOWN: "color-mix(in oklab, var(--div-neg) 70%, var(--div-mid))" };
const TREND_ORDER = ["STRONG_UP", "UP", "UP_WEAKENING", "NEUTRAL", "DOWN_DEVELOPING", "DOWN", "STRONG_DOWN"];
const trendWord = s => ({ STRONG_UP: "Strong uptrend", UP: "Uptrend", UP_WEAKENING: "Uptrend weakening", NEUTRAL: "No trend",
  DOWN_DEVELOPING: "Downtrend developing", DOWN: "Downtrend", STRONG_DOWN: "Strong downtrend" })[s] || s;
const trendTag = s => statusTag(TREND_TAG[s] || "info", trendWord(s));
const statusWord = s => s === "PROVISIONAL" ? statusTag("warn", "Provisional") : statusTag("pass", "Confirmed");

function buildAnalyze() {
  const sym = el("input", { class: "sym", type: "text", value: "AAPL", "aria-label": "Ticker", autocomplete: "off", spellcheck: "false" });
  const go = el("button", { class: "primary", text: "Analyze" });
  const error = el("div", { class: "error", role: "alert" });
  const out = el("div");
  const run = () => withBusy(go, error, async () => {
    out.classList.add("refetching");
    try {
      const d = await api(`/v2/analysis/${encodeURIComponent(sym.value.trim())}`);
      sym.value = d.symbol;
      out.replaceChildren(renderAnalysis(d));
    } finally { out.classList.remove("refetching"); }
  });
  go.addEventListener("click", run);
  sym.addEventListener("keydown", e => { if (e.key === "Enter") run(); });
  const view = el("section", {}, el("h2", { text: "Analyze a stock" }),
    el("p", { class: "lede", text: "The trend state and every piece of evidence behind it, how settled it is, and how the stock is doing against the market and its sector. Scores count evidence; they are not chances of anything." }),
    el("div", { class: "panel" }, el("div", { class: "row" }, field("Ticker", sym), go), error), out);
  view.analyze = symbol => { sym.value = symbol; run(); };
  return view;
}

// Later parts of V2 add sections to the analysis screen through this list.
const ANALYSIS_SECTIONS = [];

function renderAnalysis(d) {
  const frag = el("div"), t = d.trend;
  for (const n of t.notes || []) frag.append(banner(n.startsWith("Demo") ? "warn" : "info", n));
  const now = t.provisional || t.confirmed, conf = t.confirmed;
  const q = d.quote || {};
  const head = el("div", { class: "panel" },
    el("div", { class: "price-head" }, el("span", { class: "name", text: `${d.symbol}${q.description ? " " + q.description : ""}` }),
      isNum(q.last) ? el("span", { text: `${money(q.last)}${isNum(q.prevclose) ? ` (${signedPct(100 * (q.last / q.prevclose - 1))} today)` : ""}` }) : null,
      el("span", { class: "muted small", text: `Trend engine ${t.version}` })),
    el("div", { class: "trend-head" }, el("div", { class: "trend-state" }, trendTag(now.state), " ", statusWord(now.status)),
      t.provisional ? el("p", { class: "small" }, t.changed_today
        ? `Today's bar so far would change the state. On the last finished bar (${shortDate(conf.date)}) it is `
        : `Today's bar is still forming. On the last finished bar (${shortDate(conf.date)}) the state is the same: `, trendTag(conf.state), ".") : null),
    scoreBars(now),
    el("div", { class: "metrics", style: "margin-top:12px" },
      metric(`${now.confirmation.level[0]}${now.confirmation.level.slice(1).toLowerCase()}`, `In this state ${now.confirmation.days_in_state} day${now.confirmation.days_in_state === 1 ? "" : "s"}, since ${shortDate(now.confirmation.since)}`),
      metric(now.confidence.level[0] + now.confidence.level.slice(1).toLowerCase(), "Confidence in the reading"),
      metric(pct(100 * now.coverage, 0), "Of the evidence that could be scored"),
      metric(isNum(now.volatility.atr_pct) ? pct(now.volatility.atr_pct, 1) : "n/a", `Typical daily range${now.volatility.high ? " (high for this stock)" : now.volatility.squeeze ? " (bands squeezed)" : ""}`)),
    now.confidence.reasons.length ? el("ul", { class: "plain small muted" }, now.confidence.reasons.map(r => el("li", { text: r }))) : null);
  frag.append(head, trendHistoryChart(t));

  const rs = t.relative_strength || {};
  const rsCell = (k, label) => metric(isNum(rs[k]) ? `${rs[k] > 0 ? "+" : ""}${num(rs[k], 1)} pts` : "n/a", label);
  frag.append(el("div", { class: "panel" }, el("h3", { text: "Against the market" }),
    el("div", { class: "metrics" }, rsCell("spy_1m", "vs SPY, one month"), rsCell("spy_3m", "vs SPY, three months"),
      rsCell("qqq_3m", "vs QQQ, three months"), rsCell("sector_3m", rs.sector_fund ? `vs ${rs.sector_fund}, three months` : "Sector not known")),
    el("p", { class: "small muted", text: "The stock's return less the fund's, in percentage points, to the last finished bar." })));

  for (const section of ANALYSIS_SECTIONS) { try { const node = section(d); if (node) frag.append(node); } catch (e) { console.error(e); } }

  const ev = el("div", { class: "panel" }, el("h3", { text: `Evidence on ${shortDate(now.date)}` }));
  ev.append(table([
    { label: "Kind", cell: c => c.name },
    { label: "Bullish", num: true, cell: c => `${num(c.bullish, 1)}` },
    { label: "Bearish", num: true, cell: c => `${num(c.bearish, 1)}` },
    { label: "Of", num: true, cell: c => c.available ? `${num(c.available, 0)} of ${c.weight}` : `0 of ${c.weight} (no data)` }], now.components));
  ev.append(el("h4", { text: "Each reading" }), table([
    { label: "Reading", cell: e => e.name },
    { label: "Leans", cell: e => e.side === "bullish" ? statusTag("pass", "Bullish") : e.side === "bearish" ? statusTag("fail", "Bearish") : e.side === "unavailable" ? el("span", { class: "muted", text: "No data" }) : el("span", { class: "muted", text: "Neither" }) },
    { label: "Points", num: true, cell: e => `${num(e.points, 1)} / ${e.weight}` },
    { label: "What it shows", cell: e => e.text }], now.evidence));
  ev.append(el("p", { class: "small muted", text: `Each side's score is the points it won as a share of the points that could be scored. A state is entered at ${t.thresholds.enter} points net and kept down to ${t.thresholds.stay}; a strong state needs ${t.thresholds.strong} and the swings and averages to agree.` }));
  frag.append(ev);

  if (t.transitions.length) {
    frag.append(el("div", { class: "panel" }, el("h3", { text: "Recent changes of state" }), table([
      { label: "Date", cell: r => shortDate(r.date) }, { label: "From", cell: r => trendTag(r.from) }, { label: "To", cell: r => trendTag(r.to) },
      { label: "Net score", num: true, cell: r => num(r.net_score, 0) }, { label: "Close", num: true, cell: r => money(r.close) }], t.transitions)));
  }
  const dq = d.data_quality;
  if (dq) frag.append(el("div", { class: "panel" }, el("h3", { text: "Data checks" }),
    el("ul", { class: "plain small" }, dq.checks.map(c => el("li", {}, statusTag(c.level, c.level === "info" ? "Note" : c.level === "warn" ? "Caution" : "Problem"), ` ${c.text}`)))));
  const tech = techPanel(d.symbol);
  frag.append(tech.panel, tech.structure);
  return frag;
}

function scoreBars(day) {
  const bar = (v, side) => el("div", { class: "score-row" }, el("span", { class: "score-k", text: side === "pos" ? "Bullish" : "Bearish" }),
    el("div", { class: "score-track" }, el("i", { class: side, style: `width:${Math.max(0, Math.min(100, v || 0))}%` })),
    el("strong", { class: "score-v", text: isNum(v) ? num(v, 0) : "n/a" }));
  return el("div", { class: "scores" }, bar(day.bullish_score, "pos"), bar(day.bearish_score, "neg"),
    el("p", { class: "small muted", text: `Net ${isNum(day.net_score) ? (day.net_score > 0 ? "+" : "") + num(day.net_score, 0) : "n/a"} points. Evidence points out of 100 on each side, not probabilities.` }));
}

function trendHistoryChart(t) {
  const rows = t.history, n = rows.length;
  const wrap = el("div", { class: "panel" }, el("h3", { text: "Trend state over the last year" }));
  if (n < 2) return wrap;
  const W = 760, H = 250, pad = { l: 58, r: 16, t: 10, b: 54 }, L = pad.l, R = W - pad.r, bandTop = H - pad.b + 8, bandH = 14;
  const X = i => L + (R - L) * i / (n - 1);
  const ys = niceScale(Math.min(...rows.map(r => r.close)), Math.max(...rows.map(r => r.close)), 5);
  const Y = v => pad.t + (H - pad.b - pad.t) * (1 - (v - ys.lo) / (ys.hi - ys.lo));
  const root = svg("svg", { viewBox: `0 0 ${W} ${H}`, role: "img", "aria-label": `${t.symbol} closing price with its trend state on each day` });
  let start = 0;
  for (let i = 1; i <= n; i++) {
    if (i === n || rows[i].state !== rows[start].state) {
      const x0 = start === 0 ? L : (X(start - 1) + X(start)) / 2, x1 = i === n ? R : (X(i - 1) + X(i)) / 2;
      root.append(svg("rect", { x: x0, y: pad.t, width: Math.max(0.5, x1 - x0), height: H - pad.b - pad.t, fill: TREND_FILL[rows[start].state], opacity: 0.45 }),
        svg("rect", { x: x0, y: bandTop, width: Math.max(0.5, x1 - x0), height: bandH, fill: TREND_FILL[rows[start].state] }));
      start = i;
    }
  }
  for (const v of ys.ticks) root.append(svg("line", { class: "grid", x1: L, x2: R, y1: Y(v), y2: Y(v) }),
    svg("text", { x: L - 8, y: Y(v) + 4, "text-anchor": "end", text: money(v, ys.step < 1 ? 2 : 0) }));
  rows.forEach((r, i) => { if (i > 0 && r.date.slice(5, 7) !== rows[i - 1].date.slice(5, 7) && X(i) < R - 24) root.append(svg("text", { x: X(i), y: bandTop + bandH + 14, "text-anchor": "middle", text: monthLabel(r.date) })); });
  root.append(svg("path", { d: rows.map((r, i) => `${i ? "L" : "M"}${X(i).toFixed(1)} ${Y(r.close).toFixed(1)}`).join(" "), fill: "none", stroke: "var(--ink)", "stroke-width": 1.6 }));
  if (!rows[n - 1].complete) root.append(svg("circle", { cx: X(n - 1), cy: Y(rows[n - 1].close), r: 3.5, fill: "var(--panel)", stroke: "var(--ink)", "stroke-dasharray": "2 1.5" }));
  const hover = svg("line", { class: "hover-line", y1: pad.t, y2: bandTop + bandH, visibility: "hidden" });
  root.append(hover);
  const tip = el("div", { class: "tip" });
  root.addEventListener("pointermove", evt => {
    const box = root.getBoundingClientRect();
    const i = Math.max(0, Math.min(n - 1, Math.round(((evt.clientX - box.left) * W / box.width - L) / (R - L) * (n - 1))));
    hover.setAttribute("x1", X(i)); hover.setAttribute("x2", X(i)); hover.setAttribute("visibility", "visible");
    tip.replaceChildren(el("div", { class: "tip-head", text: shortDate(rows[i].date) + (rows[i].complete ? "" : " (so far)") }),
      el("div", { text: `${money(rows[i].close)} · ${trendWord(rows[i].state)}` }), el("div", { class: "tip-sub", text: `Net ${num(rows[i].net, 0)} points` }));
    tooltipAt(tip, box, X(i), W);
  });
  root.addEventListener("pointerleave", () => { hover.setAttribute("visibility", "hidden"); tip.style.display = "none"; });
  const present = TREND_ORDER.filter(s => rows.some(r => r.state === s));
  wrap.append(el("div", { class: "chart" }, root, tip),
    el("div", { class: "legend" }, present.map(s => el("span", {}, el("b", { class: "key-box", style: `background:${TREND_FILL[s]}` }), trendWord(s)))));
  return wrap;
}

/* ---------- market regime and indicators read in context ---------- */
const REGIME_TAG = { BULL_CALM: "pass", BULL_VOLATILE: "warn", RANGE: "info", TRANSITION: "warn", BEAR_CALM: "fail", BEAR_VOLATILE: "fail" };
const REGIME_WORDS = { BULL_CALM: "Bull market, calm", BULL_VOLATILE: "Bull market, volatile", RANGE: "Range-bound market",
  TRANSITION: "Market in transition", BEAR_CALM: "Bear market, orderly", BEAR_VOLATILE: "Bear market, stressed" };
const regimeTag = r => statusTag(REGIME_TAG[r] || "info", REGIME_WORDS[r] || r);
const VOL_WORDS = { CALM: "calm", NORMAL: "normal", ELEVATED: "elevated", STRESSED: "stressed" };

function regimeSection(d) {
  const g = d.regime;
  if (!g) return null;
  const now = g.provisional || g.confirmed;
  const etf = (name, e) => e ? el("li", {}, el("strong", { text: `${name}: ` }), trendTag(e.state),
    ` ${signedPct(e.vs_200d_pct)} from its 200-day average, ${signedPct(e.off_52w_high_pct)} from its 52-week high.`) : null;
  const v = now.volatility;
  const box = el("div", { class: "panel" }, el("h3", { text: "Market regime" }),
    el("p", {}, regimeTag(now.regime), " ", statusWord(now.status), ` ${now.text}`),
    el("ul", { class: "plain small" }, etf("SPY", now.spy), etf("QQQ", now.qqq),
      el("li", {}, el("strong", { text: "Volatility: " }), `${VOL_WORDS[v.bucket]}, ` +
        (v.source === "VIX" ? `VIX ${num(v.vix, 1)}` : `SPY's 20-day realized volatility ${pct(100 * v.spy_realized_20d, 0)}, higher than on ${pct(100 * v.spy_realized_rank, 0)} of the past year's days`) + ".")),
    g.changes.length ? el("p", { class: "small muted", text: "Recent changes: " + g.changes.slice(0, 4).map(c => `${shortDate(c.date)} ${REGIME_WORDS[c.from]} to ${REGIME_WORDS[c.to].toLowerCase()}`).join("; ") + "." }) : null,
    el("ul", { class: "plain small muted" }, g.notes.map(n => el("li", { text: n }))));
  const it = d.interpretation;
  if (it && it.readings.length) {
    const words = trendWord(it.stock_state).toLowerCase();
    box.append(el("h4", { text: `Reading the indicators for a stock with ${words === "no trend" ? "no trend" : (/^[aeiou]/.test(words) ? "an " : "a ") + words} in a ${(REGIME_WORDS[it.regime] || "").toLowerCase()}` }),
      table([{ label: "Indicator", cell: r => r.indicator }, { label: "Now", cell: r => r.value },
        { label: "Leans", cell: r => r.lean === "bullish" ? statusTag("pass", "Bullish") : r.lean === "bearish" ? statusTag("fail", "Bearish") : el("span", { class: "muted", text: "Neither" }) },
        { label: "What it usually means here", cell: r => r.reading }], it.readings),
      el("p", { class: "small muted", text: it.note }));
  }
  return box;
}
ANALYSIS_SECTIONS.push(regimeSection);

/* ---------- signals and how they turned out before ---------- */
const VERDICT_WORDS = { supported: "passed", contradicted: "worked the wrong way", no_evidence: "no evidence", insufficient: "too few cases" };
const freqText = h => {
  if (!h || !h.cases) return el("span", { class: "muted", text: "no past cases" });
  const range = isNum(h.frequency_low) ? ` (${pct(100 * h.frequency_low, 0)}–${pct(100 * h.frequency_high, 0)})` : "";
  return el("span", { title: `Average ${signedPct(100 * h.mean_return, 1)} over ${h.horizon} days, ${signedPct(100 * (h.mean_excess_vs_spy || 0), 1)} against SPY` },
    h.enough ? "" : statusTag("warn", "Few"), `${h.enough ? "" : " "}${pct(100 * h.frequency, 0)} of ${num(h.cases, 0)}${range}`);
};

function signalsSection(d) {
  if (!featureOn("signal_calibration")) return null;
  const box = el("div", { class: "panel" }, el("h3", { text: "Signals and how they turned out before" }), el("p", { class: "small muted", text: "Loading past cases…" }));
  const load = () => api(`/v2/signals/${encodeURIComponent(d.symbol)}`).then(g => renderSignals(box, g, load))
    .catch(e => box.replaceChildren(el("h3", { text: "Signals and how they turned out before" }), el("p", { class: "small error", text: e.message })));
  load();
  return box;
}

function renderSignals(box, g, reload) {
  box.replaceChildren(el("h3", { text: "Signals and how they turned out before" }));
  for (const n of g.notes) box.append(banner(n.startsWith("Demo") ? "warn" : "info", n));
  if (!g.history_built) box.append(buildHistoryButton(reload));
  if (!g.signals.length) box.append(el("p", { class: "small muted", text: "No pattern or change of trend state in the last ten trading days." }));
  else box.append(table([
    { label: "Signal", cell: x => el("span", {}, el("strong", { text: x.label }), x.direction ? el("span", { class: "small muted", text: x.direction > 0 ? " · expects a rise" : " · expects a fall" }) : null) },
    { label: "When", cell: x => el("span", {}, shortDate(x.date), " ", x.status === "PROVISIONAL" ? statusTag("warn", "Provisional") : null) },
    { label: "All stocks", cell: x => freqText(x.history.all) },
    { label: g.regime ? `In a ${(REGIME_WORDS[g.regime] || "").toLowerCase()}` : "Same market", cell: x => freqText(x.history.same_regime) },
    { label: "This stock", cell: x => freqText(x.history.this_stock) },
    { label: "Pattern lab", cell: x => x.lab.length ? x.lab.map(v => `${v.horizon}d ${VERDICT_WORDS[v.verdict] || v.verdict}`).join(", ") : el("span", { class: "muted", text: x.key.startsWith("pattern:") ? "not tested" : "not a pattern" }) }], g.signals));
  box.append(el("p", { class: "small muted", text: `How often each signal was followed by a move its way over the next 10 trading days (entry at the next day's open), in the past cases ${g.history_built ? `recorded on ${g.history_symbols} stocks (${shortDate(g.history_run_at)})` : "once the history is built"}, with the 95% range. These are counts from the past, not forecasts; overlapping cases are not independent.` }));
  const sc = g.score;
  if (sc && isNum(sc.net_score_for_calibration)) {
    const h = sc.h10, b = h && h.bucket;
    box.append(el("h4", { text: "Is the evidence score calibrated?" }));
    if (!g.history_built) box.append(el("p", { class: "small muted", text: "Build the signal history to check the score against what followed." }));
    else {
      box.append(el("p", { class: "small" }, sc.calibrated ? statusTag("pass", "Calibrated on this data") : statusTag("warn", "Not calibrated"), ` ${h.verdict}`));
      if (b) box.append(el("p", { class: "small", text: `Net score ${num(sc.net_score_for_calibration, 0)} on ${shortDate(sc.date)} is in the ${num(b.low, 0)} to ${num(b.high, 0)} range. On ${num(b.cases, 0)} past days in that range across the stocks in the history, the stock was higher 10 days later ${pct(100 * b.frequency, 0)} of the time (${pct(100 * b.frequency_low, 0)}–${pct(100 * b.frequency_high, 0)})` +
        (h.same_regime ? `; ${pct(100 * h.same_regime.frequency, 0)} of ${num(h.same_regime.cases, 0)} in the same market regime.` : ".") }));
      box.append(el("p", { class: "small muted", text: sc.note }));
    }
  }
}

function buildHistoryButton(after) {
  const btn = el("button", { class: "plain", text: "Build the signal history now" });
  const err = el("div", { class: "error", role: "alert" });
  btn.addEventListener("click", () => withBusy(btn, err, async () => { await api("/v2/signals/history/build", {}); if (after) await after(); }));
  return el("div", { class: "row", style: "margin:8px 0" }, btn, el("span", { class: "small muted", text: "Uses five years of your watchlist; takes a little while." }), err);
}

function signalHistoryPanel() {
  const box = el("div", { class: "panel" }, el("h3", { text: "Signal history and score calibration" }));
  const body = el("div", {}, el("p", { class: "small muted", text: "Loading…" }));
  const btn = el("button", { class: "primary", text: "Build or refresh the signal history" });
  const err = el("div", { class: "error", role: "alert" });
  const show = c => {
    if (!c.built) { body.replaceChildren(el("p", { class: "small muted", text: "Not built yet. It records every pattern and change of trend state on your watchlist over five years, with what followed, so live signals can be compared with their past cases and the trend score can be checked against outcomes." })); return; }
    const cal = c.calibration["10"];
    body.replaceChildren(el("p", { class: "small", text: `Built ${shortDate(c.run_at)} from ${c.symbols.length} stocks (${c.data_source} data, ${c.trend_engine_version}).` }),
      el("p", { class: "small" }, cal.calibrated ? statusTag("pass", "Calibrated") : statusTag("warn", "Not calibrated"), ` ${cal.verdict}`),
      table([{ label: "Net score range", cell: b => `${num(b.low, 0)} to ${num(b.high, 0)}` }, { label: "Days", num: true, cell: b => num(b.cases, 0) },
        { label: "Higher 10 days later", num: true, cell: b => isNum(b.frequency) ? `${pct(100 * b.frequency, 0)} (${pct(100 * b.frequency_low, 0)}–${pct(100 * b.frequency_high, 0)})` : "n/a" },
        { label: "Average return", num: true, cell: b => isNum(b.mean_return) ? signedPct(100 * b.mean_return, 2) : "n/a" },
        { label: "Against SPY", num: true, cell: b => isNum(b.mean_excess_vs_spy) ? signedPct(100 * b.mean_excess_vs_spy, 2) : "n/a" }], cal.buckets));
  };
  const load = () => api("/v2/signals/calibration").then(show).catch(e => { err.textContent = e.message; });
  btn.addEventListener("click", () => withBusy(btn, err, async () => { await api("/v2/signals/history/build", {}); await load(); }));
  load();
  box.append(body, el("div", { class: "row", style: "margin-top:10px" }, btn), err);
  return box;
}

ANALYSIS_SECTIONS.push(signalsSection);

/* ---------- grading a trade idea ---------- */
const GRADE_TAG = { A: "pass", B: "pass", C: "warn", D: "fail", AVOID: "fail" };
function gradeCard(ev) {
  const box = el("div", { class: "grade-card" });
  const lv = ev.levels, rr = ev.reward_risk, sz = ev.sizing;
  box.append(el("div", { class: "grade-head" }, el("div", { class: `grade-letter g-${ev.grade}`, text: ev.grade }),
    el("div", {}, el("div", { class: "grade-title", text: `${ev.direction === "LONG" ? "Long" : "Short"} ${ev.symbol} at ${money(ev.entry)}` }),
      el("div", { class: "small muted", text: `Score ${num(ev.score, 0)} of 100 · ${ev.entry_source === "yours" ? "your entry" : "entry at the current price"} · ${ev.version}${ev.status === "PROVISIONAL" ? " · provisional (today's bar is not finished)" : ""}` }))));
  for (const c of ev.caps) box.append(banner(c.grade === "AVOID" ? "fail" : "warn", `Capped at ${c.grade}: ${c.reason}`));
  box.append(el("div", { class: "metrics" },
    metric(money(lv.invalidation), `Invalidation (${lv.invalidation_source})`), metric(money(lv.target_1), `Target 1 (${lv.targets_source})`),
    metric(money(lv.target_2), "Target 2"), metric(`${num(rr.target_1, 2)} : 1`, "Reward to risk, target 1"),
    metric(`${num(sz.shares, 0)} sh`, `Size for ${money(sz.risk_budget, 0)} at risk`), metric(money(sz.risk_per_share), "Risk per share")));
  if (lv.invalidation_source === "suggested" || lv.targets_source === "suggested") box.append(el("ul", { class: "plain small muted" }, lv.notes.map(n => el("li", { text: n }))));
  box.append(el("h4", { text: "How it was graded" }), table([
    { label: "Check", cell: c => c.name }, { label: "Weight", num: true, cell: c => `${c.weight}%` },
    { label: "Score", cell: c => el("div", { class: "score-track mini" }, el("i", { class: c.score >= 50 ? "pos" : "neg", style: `width:${c.score}%` })) },
    { label: "", num: true, cell: c => num(c.score, 0) }, { label: "Why", cell: c => c.why.join(" ") }], ev.components));
  if (ev.short_sale_risk) box.append(el("h4", { text: "Short-sale risks" }),
    el("ul", { class: "plain small" }, ev.short_sale_risk.map(r => el("li", {}, statusTag(r.level, r.level === "fail" ? "Danger" : r.level === "warn" ? "Caution" : "Note"), ` ${r.text}`))));
  if (ev.analogs && ev.analogs.history) {
    const h = ev.analogs.history.same_regime && ev.analogs.history.same_regime.cases ? ev.analogs.history.same_regime : ev.analogs.history.all;
    box.append(el("p", { class: "small", text: `Analogs: when a stock's trend turned to ${trendWord(ev.trend_state).toLowerCase()} before, it moved the expected way over 10 days in ${pct(100 * h.frequency, 0)} of ${num(h.cases, 0)} cases.` }));
  }
  if (ev.what_would_change_it.length) box.append(el("h4", { text: "What would improve it" }), el("ul", { class: "plain small" }, ev.what_would_change_it.map(t => el("li", { text: t }))));
  box.append(el("p", { class: "small muted", text: ev.note }));
  return box;
}

function evaluateSection(d) {
  const dir = segmented(`evdir${++uid}`, [["LONG", "Long"], ["SHORT", "Short"]], (d.trend.provisional || d.trend.confirmed).state.includes("DOWN") ? "SHORT" : "LONG");
  const entry = numInput({ placeholder: "now" }), inv = numInput({ placeholder: "suggest" }), t1 = numInput({ placeholder: "suggest" }), t2 = numInput({ placeholder: "suggest" });
  const horizon = el("input", { type: "number", min: 1, max: 260, step: 1, value: 20 });
  const go = el("button", { class: "primary", text: "Grade it" });
  const err = el("div", { class: "error", role: "alert" });
  const out = el("div");
  go.addEventListener("click", () => withBusy(go, err, async () => {
    const body = { symbol: d.symbol, direction: dir.value(), horizon_days: Math.round(Number(horizon.value) || 20) };
    for (const [k, input] of [["entry_price", entry], ["invalidation_price", inv], ["target_1", t1], ["target_2", t2]]) { const v = optNum(input); if (v !== null) body[k] = v; }
    out.replaceChildren(gradeCard(await api("/v2/position/evaluate", body)));
  }));
  return el("div", { class: "panel" }, el("h3", { text: "Grade a stock trade" }),
    el("p", { class: "small muted", text: "Leave a price blank and it is suggested from the chart: the invalidation beyond the last swing, targets at the support or resistance in the way." }),
    el("div", { class: "inline-form" }, field("Direction", dir), field("Entry", entry), field("Invalidation", inv), field("Target 1", t1), field("Target 2", t2), field("Holding days", horizon), go), err, out);
}
ANALYSIS_SECTIONS.push(evaluateSection);

/* ---------- choosing a call or put for the thesis ---------- */
function optionsSection(d) {
  const bearish = (d.trend.provisional || d.trend.confirmed).state.includes("DOWN");
  const dir = segmented(`opdir${++uid}`, [["BULLISH", "Bullish (calls)"], ["BEARISH", "Bearish (puts)"]], bearish ? "BEARISH" : "BULLISH");
  const target = numInput({ placeholder: "suggest" }), inv = numInput({ placeholder: "suggest" });
  const horizon = el("input", { type: "number", min: 1, max: 260, step: 1, value: 20 });
  const qty = el("input", { type: "number", min: 1, max: 500, step: 1, value: 1 });
  const budget = numInput({ placeholder: "none", step: "1" });
  const go = el("button", { class: "primary", text: "Find contracts" });
  const err = el("div", { class: "error", role: "alert" });
  const out = el("div");
  go.addEventListener("click", () => withBusy(go, err, async () => {
    const body = { symbol: d.symbol, direction: dir.value(), horizon_days: Math.round(Number(horizon.value) || 20), contracts: Math.max(1, Math.round(Number(qty.value) || 1)) };
    for (const [k, input] of [["target_price", target], ["invalidation_price", inv], ["budget", budget]]) { const v = optNum(input); if (v !== null) body[k] = v; }
    out.replaceChildren(el("div", { class: "loadnote small muted" }, el("span", { class: "spinner" }), el("span", { text: "Valuing contracts across expirations…" })));
    try { out.replaceChildren(renderOptionsEval(await api("/v2/options/evaluate", body))); }
    catch (e) { out.replaceChildren(); throw e; }
  }));
  return el("div", { class: "panel" }, el("h3", { text: "Choose a call or put for the thesis" }),
    el("p", { class: "small muted", text: "The thesis is about the stock: where it should get to, by when, and the price that would prove it wrong. Leave prices blank to use the suggested levels." }),
    el("div", { class: "inline-form" }, field("Thesis", dir), field("Stock target", target), field("Invalidation (stock)", inv), field("By (trading days)", horizon), field("Contracts", qty), field("Budget ($)", budget), go), err, out);
}
ANALYSIS_SECTIONS.push(optionsSection);

function contractName(k) { return `${shortDate(k.expiration)} ${num(k.strike, k.strike % 1 ? 2 : 0)} ${k.option_type.toLowerCase()}`; }

function renderOptionsEval(r) {
  const th = r.thesis, frag = el("div");
  if (r.data_source === "demo") frag.append(banner("warn", "Demo data: these option prices are made up."));
  frag.append(el("p", { class: "small" }, el("strong", { text: `${th.direction === "BULLISH" ? "Bullish" : "Bearish"} on ${r.symbol} at ${money(th.spot)}: ` }),
    `target ${money(th.target_1)} (${r.levels_source.target_1}) by ${shortDate(th.horizon_date)}, wrong ${th.direction === "BULLISH" ? "below" : "above"} ${money(th.invalidation)} (${r.levels_source.invalidation}). `,
    r.stock_grade && r.stock_grade.grade ? el("span", {}, "The stock trade itself grades ", el("strong", { text: r.stock_grade.grade }), ".") : null));
  if (r.market) frag.append(el("p", { class: "small muted", text: `Market-implied: about ${pct(100 * r.market.reach_target_1_by_horizon, 0)} chance the stock is past the target on ${shortDate(th.horizon_date)}, ${pct(100 * r.market.beyond_invalidation_at_horizon, 0)} past the invalidation price. ${r.market.note}` }));
  frag.append(banner("info", `Exit rule: ${r.exit_rule}`));
  if (!r.picks.length) frag.append(banner("warn", "No contract both fits the thesis and passes the checks. See the list of contracts to avoid below."));
  const cards = el("div", { class: "pick-grid" });
  for (const pk of r.picks) {
    const k = pk.contract;
    const sim = el("button", { class: "plain small-btn", text: "Simulate" });
    sim.addEventListener("click", () => state.openSimulation(r.symbol, [{ side: "buy", contracts: th.contracts, option_type: k.option_type.toLowerCase(), expiration: k.expiration, strike: k.strike, entry_price: k.mid }]));
    const track = el("button", { class: "plain small-btn", text: "Save as a plan" });
    const note = el("span", { class: "small muted" });
    track.addEventListener("click", () => withBusy(track, note, async () => {
      const p = await api("/v2/positions", { symbol: r.symbol, instrument_type: "OPTION", quantity: th.contracts, setup_type: "option for a stock thesis",
        option: { option_type: k.option_type, strike: k.strike, expiration: k.expiration }, invalidation_price: th.invalidation, target_1: th.target_1, target_2: th.target_2,
        thesis: `${th.direction === "BULLISH" ? "Bullish" : "Bearish"}: ${r.symbol} to ${money(th.target_1)} by ${shortDate(th.horizon_date)}. ${r.exit_rule}` });
      note.textContent = `Saved as planned position ${p.id}.`;
    }));
    cards.append(el("div", { class: "pick" }, el("div", { class: "pick-cat", text: pk.category }), el("div", { class: "pick-name", text: contractName(k) }),
      el("div", { class: "small", text: `${money(k.mid)} mid · ${money(k.premium, 0)} for ${th.contracts} · delta ${num(k.delta, 2)}` }),
      el("div", { class: "small", text: `At target: ${signedMoney(k.at_target)} · if invalidated: ${signedMoney(k.if_invalidated)}` }),
      el("p", { class: "small muted", text: pk.why }), k.flags.length ? el("ul", { class: "plain small" }, k.flags.map(f => el("li", {}, statusTag("warn", "Caution"), ` ${f}`))) : null,
      el("div", { class: "row" }, sim, track), note));
  }
  frag.append(cards);
  const usable = r.contracts.filter(k => k.usable);
  if (usable.length) frag.append(el("details", {}, el("summary", { text: `All ${usable.length} contracts that pass the checks` }), table([
    { label: "Contract", cell: k => contractName(k) }, { label: "Mid", num: true, cell: k => money(k.mid) },
    { label: "Cost", num: true, cell: k => money(k.premium, 0) }, { label: "Delta", num: true, cell: k => num(k.delta, 2) },
    { label: "At target", num: true, cell: k => signedMoney(k.at_target) }, { label: "Return", num: true, cell: k => isNum(k.return_on_premium) ? signedPct(100 * k.return_on_premium, 0) : "n/a" },
    { label: "If invalidated", num: true, cell: k => signedMoney(k.if_invalidated) }, { label: "Reward : risk", num: true, cell: k => isNum(k.reward_to_risk) ? num(k.reward_to_risk, 2) : "n/a" },
    { label: "Flat stock", num: true, cell: k => signedMoney(k.if_unchanged) }, { label: "Breakeven by horizon", num: true, cell: k => money(k.breakeven_at_horizon) },
    { label: "Spread", num: true, cell: k => pct(k.spread_pct, 0) }], usable)));
  if (r.avoid.length) frag.append(el("details", {}, el("summary", { text: `${r.avoid.length} contracts to avoid, and why` }),
    el("ul", { class: "plain small" }, r.avoid.map(a => el("li", {}, el("strong", { text: `${shortDate(a.expiration)} ${num(a.strike, a.strike % 1 ? 2 : 0)}: ` }), a.reasons.join(" "))))));
  frag.append(el("ul", { class: "plain small muted" }, r.notes.map(n => el("li", { text: n }))));
  return frag;
}
{
  const classicPatterns = BUILDERS.patterns;
  BUILDERS.patterns = () => {
    const view = classicPatterns();
    if (featureOn("signal_calibration")) view.append(signalHistoryPanel());
    return view;
  };
}

/* ---------- view: portfolio risk ---------- */
function buildPortfolio() {
  const error = el("div", { class: "error", role: "alert" });
  const out = el("div", {}, el("p", { class: "muted", text: "Loading…" }));
  const refresh = el("button", { class: "plain", text: "Refresh" });
  async function load() {
    out.classList.add("refetching");
    try { out.replaceChildren(renderPortfolio(await api("/v2/portfolio/risk"))); error.textContent = ""; }
    catch (e) { error.textContent = e.message; }
    finally { out.classList.remove("refetching"); }
  }
  refresh.addEventListener("click", () => withBusy(refresh, error, load));
  const view = el("section", {}, el("div", { class: "panel-head" }, el("h2", { text: "Portfolio risk" }), refresh),
    el("p", { class: "lede", text: "Everything open, added up: how much rides on the market, what a bad day or a crash would cost, where the risk is concentrated, and where you stand against your daily loss stop." }),
    error, out);
  view.onShow = load;
  return view;
}

function renderPortfolio(r) {
  const frag = el("div"), t = r.totals, dl = r.daily_loss;
  if (r.data_source === "demo") frag.append(banner("warn", "Demo data: the prices are made up."));
  for (const w of r.warnings) frag.append(banner(w.level === "fail" ? "fail" : w.level === "warn" ? "warn" : "info", w.text));
  if (!r.count) { frag.append(el("div", { class: "panel" }, el("p", { class: "muted", text: "No open positions. Add them on the Positions screen (option-journal trades appear there automatically)." }))); }
  const acct = r.account_size, share = v => acct ? ` (${pct(100 * v / acct, 0)} of the account)` : "";
  frag.append(el("div", { class: "panel" }, el("h3", { text: "Totals" }), el("div", { class: "metrics" },
    metric(signedMoney(t.net_exposure), `Net exposure in stock dollars${share(Math.abs(t.net_exposure))}`),
    metric(money(t.gross_exposure, 0), "Gross exposure"),
    metric(signedMoney(t.spy_equivalent), "SPY-equivalent dollars (beta-weighted)"),
    metric(money(t.open_risk, 0), `Risk to invalidation, limit ${money(t.open_risk_limit, 0)}${t.open_risk_unknown ? `; ${t.open_risk_unknown} unknown` : ""}`),
    metric(isNum(t.one_day_move_95) ? money(t.one_day_move_95, 0) : "n/a", "A bad day (1 in 20), from the last six months"),
    metric(signedMoney(t.theta_per_day), "Time decay per day (options)"),
    metric(signedMoney(t.vega_per_point), "Per point of implied volatility"),
    metric(signedMoney(dl.counted), `Today, ${dl.mode === "REALIZED_PLUS_UNREALIZED" ? "closed and open" : "closed trades only"}; stop at ${money(-dl.stop, 0)}`)),
    el("p", { class: "small muted", text: `${dl.note}${dl.unknown.length ? ` No previous close for: ${dl.unknown.join("; ")}, so their change today is not counted.` : ""} Set DAILY_LOSS_MODE in .env to change this.` })));

  if (r.positions.length) frag.append(el("div", { class: "panel" }, el("h3", { text: "By position" }), table([
    { label: "Position", cell: x => x.description }, { label: "Sector", cell: x => x.sector || "" },
    { label: "Exposure", num: true, cell: x => signedMoney(x.dollar_delta) }, { label: "Beta", num: true, cell: x => isNum(x.beta) ? num(x.beta, 2) : "n/a" },
    { label: "SPY-equivalent", num: true, cell: x => signedMoney(x.beta_dollars) },
    { label: "Risk to invalidation", num: true, cell: x => isNum(x.risk_to_invalidation) ? money(x.risk_to_invalidation, 0) : "unknown" },
    { label: "Theta / day", num: true, cell: x => x.instrument_type === "OPTION" ? signedMoney(x.theta) : "" },
    { label: "Plan", cell: x => tagOf(THESIS, x.thesis_status) }], r.positions),
    el("p", { class: "small muted", text: "Exposure is in stock dollars: shares times price, or delta times 100 times contracts times the stock price for options. Beta to SPY from six months of daily moves." })));

  if (r.by_sector.length) {
    const max = Math.max(...r.by_sector.map(x => Math.abs(x.exposure)), 1);
    frag.append(el("div", { class: "panel" }, el("h3", { text: "By sector" }), el("div", { class: "bars" }, r.by_sector.map(x => el("div", { class: "bar-row" },
      el("span", { text: x.sector }), el("div", { class: "score-track" }, el("i", { class: x.exposure >= 0 ? "pos" : "neg", style: `width:${100 * Math.abs(x.exposure) / max}%` })),
      el("strong", { text: signedMoney(x.exposure) }))))));
  }
  if (r.correlation) {
    const c = r.correlation;
    const cell = v => el("td", { class: "num cell", style: `background: color-mix(in oklab, var(${v >= 0 ? "--div-pos" : "--div-neg"}) ${Math.round(70 * Math.abs(v))}%, var(--div-mid))`, text: num(v, 2) });
    frag.append(el("div", { class: "panel" }, el("h3", { text: "How the holdings move together" }),
      el("div", { class: "scroll" }, el("table", { class: "pnl-grid" }, el("thead", {}, el("tr", {}, el("th", { text: "" }), c.symbols.map(s2 => el("th", { class: "num", text: s2 })))),
        el("tbody", {}, c.symbols.map((a2, i) => el("tr", {}, el("td", { text: a2 }), c.matrix[i].map(cell)))))),
      el("p", { class: "small muted", text: "Correlation of daily returns over the last six months: 1 moves exactly together, 0 unrelated, -1 opposite." })));
  }
  frag.append(stressPanel(r));
  for (const n of r.notes || []) frag.append(banner("info", n));
  return frag;
}

function stressPanel(r) {
  const box = el("div", { class: "panel" }, el("h3", { text: "Stress tests" }));
  const rows = el("div");
  const show = list => rows.replaceChildren(table([
    { label: "Scenario", cell: x => x.name }, { label: "Profit or loss", num: true, cell: x => signedMoney(x.pnl) },
    { label: "Of the account", num: true, cell: x => isNum(x.share_of_account) ? signedPct(100 * x.share_of_account, 1) : "n/a" },
    { label: "Hit hardest", cell: x => x.worst && x.worst.pnl < 0 ? `${x.worst.description} (${signedMoney(x.worst.pnl)})` : "" }], list));
  show(r.stress);
  const mkt = el("input", { type: "number", step: 1, value: -15, min: -90, max: 200 }), iv = el("input", { type: "number", step: 1, value: 12, min: -50, max: 150 });
  const sym = el("input", { class: "sym", type: "text", placeholder: "optional", "aria-label": "One stock" }), move = el("input", { type: "number", step: 1, value: -25 });
  const go = el("button", { class: "plain", text: "Run this scenario" });
  const err = el("div", { class: "error", role: "alert" });
  go.addEventListener("click", () => withBusy(go, err, async () => {
    const sc = { name: `Market ${signedPct(Number(mkt.value), 0)}, volatility ${Number(iv.value) >= 0 ? "+" : ""}${iv.value} points${sym.value.trim() ? `, ${sym.value.trim().toUpperCase()} ${signedPct(Number(move.value), 0)}` : ""}`,
      market_move_pct: Number(mkt.value), iv_shift_points: Number(iv.value) };
    if (sym.value.trim()) sc.moves = { [sym.value.trim().toUpperCase()]: Number(move.value) };
    const res = await api("/v2/portfolio/stress", { scenarios: [sc] });
    show([...res.stress, ...r.stress]);
  }));
  box.append(rows, el("h4", { text: "Your own scenario" }),
    el("div", { class: "inline-form" }, field("Market move %", mkt), field("Volatility change (points)", iv), field("One stock", sym), field("Its move %", move), go), err,
    el("p", { class: "small muted", text: "Each stock moves its beta times the market move unless named; calls and puts are repriced with the simulator's model at the new price and volatility, as of now." }));
  return box;
}

/* ---------- journal analytics ---------- */
const rText = v => isNum(v) ? `${v > 0 ? "+" : ""}${num(v, 2)}R` : "n/a";
function statCells(g) {
  return [{ label: "Trades", num: true, cell: x => num(x.trades, 0) },
    { label: "Win rate", num: true, cell: x => isNum(x.win_rate) ? pct(100 * x.win_rate, 0) : "n/a" },
    { label: "Profit factor", num: true, cell: x => isNum(x.profit_factor) ? num(x.profit_factor, 2) : (x.losses === 0 && x.wins ? "no losses" : "n/a") },
    { label: "Per trade", num: true, cell: x => signedMoney(x.expectancy) },
    { label: "Per trade (R)", num: true, cell: x => rText(x.expectancy_r) },
    { label: "Total", num: true, cell: x => signedMoney(x.total) }];
}

function journalAnalyticsPanel() {
  const box = el("div", { class: "panel" }, el("h3", { text: "Analytics across all closed positions" }), el("p", { class: "small muted", text: "Loading…" }));
  const load = () => api("/v2/journal/stats").then(r => renderJournalStats(box, r)).catch(e => box.replaceChildren(el("h3", { text: "Analytics" }), el("p", { class: "error", text: e.message })));
  box.reload = load;
  load();
  return box;
}

function renderJournalStats(box, r) {
  const o = r.overall;
  box.replaceChildren(el("h3", { text: "Analytics across all closed positions" }));
  if (!o.trades) { box.append(el("p", { class: "small muted", text: "No closed positions yet. Stock trades from the Positions screen and option-journal trades both count once closed." })); return; }
  for (const n of r.notes) box.append(banner("info", n));
  box.append(el("div", { class: "metrics" },
    metric(`${num(o.trades, 0)}`, `Closed (${o.wins} won, ${o.losses} lost)`), metric(pct(100 * o.win_rate, 0), "Win rate"),
    metric(isNum(o.profit_factor) ? num(o.profit_factor, 2) : "n/a", "Profit factor (wins ÷ losses)"),
    metric(signedMoney(o.expectancy), "Expectancy per trade"), metric(rText(o.expectancy_r), `Per trade in R (${o.r_trades} with a risk)`),
    metric(`${signedMoney(o.average_win)} / ${signedMoney(o.average_loss)}`, "Average win / loss"),
    metric(signedMoney(r.max_drawdown), "Deepest drawdown of closed results"), metric(String(o.longest_losing_streak), "Longest losing streak"),
    metric(`${rText(o.average_mfe_r)} / ${rText(o.average_mae_r)}`, "Average best / worst move while held (stock, in R)")));
  if (r.equity_curve.length > 1) box.append(lineChart({ series: [{ name: "Closed results", color: "--series-1",
    points: r.equity_curve.map(p2 => ({ x: p2.date, y: p2.equity })) }], yFormat: v => money(v, 0), height: 200 }));
  const groups = [["By setup", r.by_setup], ["Bet on a rise or a fall", r.by_direction], ["Stock or options", r.by_instrument],
    ["Trend at entry", r.by_trend_at_entry.map(g => ({ ...g, group: g.group === "not recorded" ? g.group : trendWord(g.group) }))],
    ["Market regime at entry", r.by_regime_at_entry.map(g => ({ ...g, group: REGIME_WORDS[g.group] || g.group }))], ["Holding time", r.by_holding_time]];
  for (const [title, rows] of groups) {
    if (rows.length > 1 || (rows.length === 1 && !["not given", "not recorded"].includes(rows[0].group)))
      box.append(el("h4", { text: title }), table([{ label: "", cell: x => x.group }, ...statCells()], rows));
  }
  if (r.behaviour.length) {
    box.append(el("h4", { text: "What your habits cost or earned" }), table([
      { label: "Habit", cell: b => b.label }, { label: "Trades", num: true, cell: b => num(b.with.trades, 0) },
      { label: "Per trade, with it", num: true, cell: b => `${signedMoney(b.with.expectancy)} (${rText(b.with.expectancy_r)})` },
      { label: "Per trade, without it", num: true, cell: b => b.without.trades ? `${signedMoney(b.without.expectancy)} (${rText(b.without.expectancy_r)})` : "n/a" }], r.behaviour),
      el("p", { class: "small muted", text: "MAE and MFE are the worst and best the stock moved while each trade was open, from daily highs and lows after the entry day; for calls and puts they are measured on the stock." }));
  }
}
{
  const classicJournal = BUILDERS.journal;
  BUILDERS.journal = () => {
    const view = classicJournal();
    if (featureOn("unified_positions")) {
      const panel = journalAnalyticsPanel();
      view.append(panel);
      const old = state.refreshJournal;
      state.refreshJournal = async () => { await old(); panel.reload(); };
    }
    return view;
  };
}

const FIELD_WORDS = { underlying_price: "Stock price", trend_state: "Trend", market_regime: "Market regime", bullish_score: "Bullish score",
  bearish_score: "Bearish score", rsi_14: "RSI (14)", structure_state: "Swings", relative_strength_spy: "Against SPY, 3 months (points)", iv: "Implied volatility", delta: "Delta" };
function fieldValue(field, v) {
  if (v === null || v === undefined) return "n/a";
  if (field === "trend_state") return trendWord(v);
  if (field === "market_regime") return REGIME_WORDS[v] || v;
  if (field === "underlying_price") return money(v);
  if (field === "iv") return pct(100 * v, 0);
  return typeof v === "number" ? num(v, field === "delta" ? 2 : 1) : v;
}

function positionAnalyticsBox(p) {
  const box = el("div", {}, el("p", { class: "small muted", text: "Loading analytics…" }));
  api(`/v2/positions/${p.id}/analytics`).then(a => {
    const x = a.excursions;
    box.replaceChildren(el("h4", { text: "While it was held" }));
    if (x.available) box.append(el("p", { class: "small", text: `The stock went ${signedPct(x.mfe_pct, 1)} the trade's way at best${isNum(x.mfe_r) ? ` (${rText(x.mfe_r)})` : ""} and ${signedPct(x.mae_pct, 1)} at worst${isNum(x.mae_r) ? ` (${rText(x.mae_r)})` : ""}, over ${x.days} daily bars from ${money(x.underlying_entry)}.` + (x.closed_beyond_invalidation ? " It closed through the invalidation price on one of those days." : "") + (x.reached_target_1 ? " It reached target 1." : "") }));
    else box.append(el("p", { class: "small muted", text: `No excursions yet: ${x.reason || "no daily bars since the entry"}.` }));
    if (a.behaviour.length) box.append(el("ul", { class: "plain small" }, a.behaviour.map(b => el("li", {}, statusTag("warn", "Habit"), ` ${b.label}`))));
    if (a.compare.length && a.compared_with) box.append(el("h4", { text: `At entry and at the ${a.compared_with.toLowerCase()} snapshot` }), table([
      { label: "", cell: c => FIELD_WORDS[c.field] || c.field.replaceAll("_", " ") },
      { label: "Entry", cell: c => fieldValue(c.field, c.entry) },
      { label: a.compared_with[0] + a.compared_with.slice(1).toLowerCase(), cell: c => fieldValue(c.field, c.latest) }], a.compare));
  }).catch(e => box.replaceChildren(el("p", { class: "small error", text: e.message })));
  return box;
}

/* ---------- backtest V2: condition strategies ---------- */
const OP_WORDS = { ">": "is above", ">=": "is at least", "<": "is below", "<=": "is at most", crosses_above: "crosses above",
  crosses_below: "crosses below", in: "is one of", not_in: "is not one of", fired: "fired today" };

function conditionStrategyPanel() {
  const box = el("div", { class: "panel" }, el("h3", { text: "Condition strategies" }),
    el("p", { class: "small muted", text: "Write a strategy as conditions on indicators, the trend engine's state and the market regime. Signals are read at each close from finished bars and traded at the next open, with the same stops, targets and costs as above. Shorts pay a borrow fee." }));
  const err = el("div", { class: "error", role: "alert" });
  const out = el("div");
  let meta = null;
  const rows = { long: el("div", { class: "conds" }), short: el("div", { class: "conds" }) };
  const preset = el("select", { "aria-label": "Start from" });
  const regimeBox = el("div", { class: "toggles" });
  const years = el("input", { type: "number", min: 1, max: 10, step: 1, value: 3 });
  const stop = el("input", { type: "number", min: 0.5, max: 50, step: 0.5, value: 6 }), target = el("input", { type: "number", min: 1, max: 200, step: 1, value: 15 });
  const hold = el("input", { type: "number", min: 1, max: 365, step: 1, value: 60 }), borrow = el("input", { type: "number", min: 0, max: 200, step: 0.25, value: 0.5 });
  const syms = el("input", { type: "text", placeholder: "watchlist", "aria-label": "Symbols", style: "width: 260px" });
  const mc = el("input", { type: "checkbox", checked: true });
  const run = el("button", { class: "primary", text: "Run the backtest" }), wf = el("button", { class: "plain", text: "Walk-forward test" });

  function condRow(side, c = {}) {
    const fieldSel = el("select", { "aria-label": "Field" },
      el("optgroup", { label: "States" }, Object.entries(meta.category_words).map(([k, v]) => el("option", { value: k, text: v }))),
      el("option", { value: "pattern", text: "A pattern" }),
      el("optgroup", { label: "Numbers" }, Object.entries(meta.numeric).map(([k, v]) => el("option", { value: k, text: v }))));
    fieldSel.value = c.field || "rsi";
    const opSel = el("select", { "aria-label": "Test" });
    const valBox = el("span");
    const remove = el("button", { class: "plain small-btn", type: "button", text: "Remove" });
    const node = el("div", { class: "cond row" }, fieldSel, opSel, valBox, remove);
    let valueGet = () => null;
    function sync(initial) {
      const f = fieldSel.value;
      const ops = f === "pattern" ? ["fired"] : meta.categories[f] ? ["in", "not_in"] : [">", ">=", "<", "<=", "crosses_above", "crosses_below"];
      opSel.replaceChildren(...ops.map(o => el("option", { value: o, text: OP_WORDS[o] })));
      opSel.value = initial && ops.includes(c.op) ? c.op : ops[0];
      if (f === "pattern") {
        const sel = el("select", { "aria-label": "Pattern" }, Object.entries(meta.patterns).map(([k, v]) => el("option", { value: k, text: v })));
        if (initial && c.value) sel.value = c.value;
        valBox.replaceChildren(sel); valueGet = () => sel.value;
      } else if (meta.categories[f]) {
        const words = f === "trend_state" ? trendWord : (x => REGIME_WORDS[x] || x);
        const boxes = meta.categories[f].map(v => { const b2 = el("input", { type: "checkbox", value: v, checked: initial && Array.isArray(c.value) && c.value.includes(v) }); return el("label", { class: "check" }, b2, words(v)); });
        valBox.replaceChildren(el("span", { class: "toggles" }, boxes));
        valueGet = () => [...valBox.querySelectorAll("input:checked")].map(b2 => b2.value);
      } else {
        const kind = el("select", { "aria-label": "Compare with" }, el("option", { value: "num", text: "a number" }), el("option", { value: "field", text: "another field" }));
        const n = el("input", { type: "number", step: "any", value: typeof c.value === "number" ? c.value : 50 });
        const other = el("select", { "aria-label": "Other field" }, Object.entries(meta.numeric).map(([k, v]) => el("option", { value: k, text: v })));
        if (initial && typeof c.value === "string") { kind.value = "field"; other.value = c.value; }
        const show2 = () => { n.classList.toggle("hidden", kind.value !== "num"); other.classList.toggle("hidden", kind.value === "num"); };
        kind.addEventListener("change", show2); show2();
        valBox.replaceChildren(kind, n, other);
        valueGet = () => kind.value === "num" ? Number(n.value) : other.value;
      }
    }
    fieldSel.addEventListener("change", () => sync(false));
    sync(true);
    node.value = () => ({ field: fieldSel.value, op: opSel.value, value: valueGet() });
    remove.addEventListener("click", () => node.remove());
    rows[side].append(node);
  }
  function load(name) {
    const pr = meta.presets[name];
    for (const side of ["long", "short"]) { rows[side].replaceChildren(); for (const c of pr[side]) condRow(side, c); }
    for (const b2 of regimeBox.querySelectorAll("input")) b2.checked = pr.regimes.includes(b2.value);
  }
  function body() {
    const strategy = { long: [...rows.long.children].map(n => n.value()), short: [...rows.short.children].map(n => n.value()),
      regimes: [...regimeBox.querySelectorAll("input:checked")].map(b2 => b2.value) };
    const symbols = syms.value.split(/[ ,]+/).map(x => x.trim()).filter(Boolean);
    return { strategy, years: Number(years.value) || 3, symbols: symbols.length ? symbols : null, monte_carlo: mc.checked,
      params: { stop_loss_pct: Number(stop.value) / 100, take_profit_pct: Number(target.value) / 100, holding_period_days: Math.round(Number(hold.value)), short_borrow_rate_pct: Number(borrow.value) } };
  }
  run.addEventListener("click", () => withBusy(run, err, async () => { out.replaceChildren(renderBacktestV2(await api("/v2/backtest", body()))); }));
  wf.addEventListener("click", () => withBusy(wf, err, async () => { const b2 = body(); b2.years = Math.max(4, b2.years); out.replaceChildren(renderWalkForwardV2(await api("/v2/backtest/walkforward", b2))); }));
  api("/v2/backtest/fields").then(m => {
    meta = m;
    preset.replaceChildren(...Object.entries(m.presets).map(([k, v]) => el("option", { value: k, text: v.label })));
    regimeBox.replaceChildren(...m.categories.regime.map(r2 => el("label", { class: "check" }, el("input", { type: "checkbox", value: r2 }), REGIME_WORDS[r2] || r2)));
    preset.addEventListener("change", () => load(preset.value));
    load(preset.value);
    const addL = el("button", { class: "plain small-btn", type: "button", text: "Add a long condition" }), addS = el("button", { class: "plain small-btn", type: "button", text: "Add a short condition" });
    addL.addEventListener("click", () => condRow("long")); addS.addEventListener("click", () => condRow("short"));
    box.append(el("div", { class: "row" }, field("Start from", preset), field("Symbols (blank for the watchlist)", syms)),
      el("h4", { text: "Go long when all of these hold" }), rows.long, addL,
      el("h4", { text: "Go short when all of these hold" }), rows.short, addS,
      el("h4", { text: "Only in these market regimes (none ticked: any)" }), regimeBox,
      el("div", { class: "inline-form" }, field("Years", years), field("Stop %", stop), field("Target %", target), field("Hold up to (days)", hold), field("Borrow fee % a year", borrow),
        el("label", { class: "check" }, mc, "Monte Carlo"), run, wf), err, out,
      m.options_backtest ? null : el("p", { class: "small muted", text: "Option strategies are not backtested: no connected source has past option prices, and pricing them from today's volatility would only look precise." }));
  }).catch(e => { err.textContent = e.message; box.append(err); });
  return box;
}

function renderBacktestV2(r) {
  const frag = el("div");
  if (r.data_source === "demo") frag.append(banner("warn", "Demo prices are random: this result says nothing about real markets."));
  for (const w of r.data_warnings || []) frag.append(banner("warn", w));
  const b = r.benchmark;
  frag.append(el("div", { class: "metrics" },
    metric(signedPct(r.total_return_pct, 1), `Return (buy and hold ${signedPct(b.total_return_pct, 1)})`),
    metric(isNum(r.sharpe) ? num(r.sharpe, 2) : "n/a", `Sharpe (buy and hold ${isNum(b.sharpe) ? num(b.sharpe, 2) : "n/a"})`),
    metric(pct(r.max_drawdown_pct, 1), "Deepest drawdown"), metric(num(r.num_trades, 0), "Trades"),
    metric(isNum(r.win_rate_pct) ? pct(r.win_rate_pct, 0) : "n/a", "Winners"), metric(isNum(r.profit_factor) ? num(r.profit_factor, 2) : "n/a", "Profit factor"),
    metric(money(r.borrow_cost, 0), "Paid to borrow for shorts"),
    metric(isNum(r.significance.prob_sharpe_positive) ? pct(100 * r.significance.prob_sharpe_positive, 0) : "n/a", "Chance the Sharpe is above zero")));
  frag.append(lineChart({ series: [{ name: "Strategy", color: "--series-1", points: r.equity_curve.map(p2 => ({ x: p2.date, y: p2.equity })) },
    { name: "Buy and hold", color: "--series-2", points: r.equity_curve.map(p2 => ({ x: p2.date, y: p2.benchmark })) }], yFormat: v => money(v, 0), height: 240 }));
  const m = r.monte_carlo;
  if (m) frag.append(el("h4", { text: "Monte Carlo" }), el("p", { class: "small", text: `Across ${m.paths} reshuffles of the daily returns, the return ran from ${signedPct(m.return_pct.p5, 1)} to ${signedPct(m.return_pct.p95, 1)} (5th to 95th percentile, median ${signedPct(m.return_pct.p50, 1)}) and the deepest drawdown from ${pct(m.max_drawdown_pct.p5, 1)} to ${pct(m.max_drawdown_pct.p95, 1)}. It lost money in ${pct(100 * m.chance_of_loss, 0)} of them.` }),
    el("p", { class: "small muted", text: m.note }));
  frag.append(el("p", { class: "small muted", text: `Signal days: ${num(r.signals.long_days, 0)} long, ${num(r.signals.short_days, 0)} short${r.signals.blocked_by_regime ? `, ${num(r.signals.blocked_by_regime, 0)} skipped for the regime` : ""}.` }),
    el("ul", { class: "plain small muted" }, r.notes.map(n => el("li", { text: n }))));
  if (r.trades.length) frag.append(el("details", {}, el("summary", { text: `Trades (${r.trades.length}${r.trades_truncated ? ` of ${r.trades_truncated}` : ""})` }), table([
    { label: "Symbol", cell: t => t.symbol }, { label: "Side", cell: t => t.direction === "LONG" ? "Long" : "Short" },
    { label: "In", cell: t => shortDate(t.entry_date) }, { label: "Out", cell: t => shortDate(t.exit_date) },
    { label: "Result", num: true, cell: t => signedMoney(t.pnl) }, { label: "Why it ended", cell: t => t.exit_reason.replaceAll("_", " ").toLowerCase() }], r.trades.slice().reverse())));
  return frag;
}

function renderWalkForwardV2(w) {
  const o = w.out_of_sample, frag = el("div");
  if (w.data_source === "demo") frag.append(banner("warn", "Demo prices are random: this result says nothing about real markets."));
  frag.append(el("div", { class: "metrics" }, metric(signedPct(o.total_return_pct, 1), "Out-of-sample return"),
    metric(isNum(o.sharpe) ? num(o.sharpe, 2) : "n/a", "Out-of-sample Sharpe"), metric(pct(o.max_drawdown_pct, 1), "Deepest drawdown"),
    metric(`${o.positive_folds} of ${o.folds}`, "Test windows that made money"),
    metric(`${isNum(w.avg_train_sharpe) ? num(w.avg_train_sharpe, 2) : "n/a"} / ${isNum(w.avg_test_sharpe) ? num(w.avg_test_sharpe, 2) : "n/a"}`, "Average Sharpe, training / test")),
    table([{ label: "Trained on", cell: f => `${shortDate(f.train_start)} to ${shortDate(f.train_end)}` }, { label: "Tested on", cell: f => `${shortDate(f.test_start)} to ${shortDate(f.test_end)}` },
      { label: "Chosen", cell: f => Object.entries(f.chosen).map(([k, v]) => `${k.replace("_pct", "").replaceAll("_", " ")} ${k.endsWith("_pct") ? pct(100 * v, 0) : v}`).join(", ") },
      { label: "Test return", num: true, cell: f => signedPct(f.test_return_pct, 1) }, { label: "Trades", num: true, cell: f => num(f.test_trades, 0) }], w.folds),
    el("ul", { class: "plain small muted" }, w.notes.map(n => el("li", { text: n }))));
  return frag;
}
{
  const classicBacktest = BUILDERS.backtest;
  BUILDERS.backtest = () => {
    const view = classicBacktest();
    if (featureOn("trend_engine_v2")) view.append(conditionStrategyPanel());
    return view;
  };
}

Object.assign(BUILDERS, { positions: buildPositions, analyze: buildAnalyze, portfolio: buildPortfolio });
