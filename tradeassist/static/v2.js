"use strict";
/* Trade Assistant V2 screens. Loaded after app.js, whose helpers (el, api, field, table, money, ...) it uses.
   Each screen registers itself in BUILDERS; app.js starts the page once both scripts have run. */

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
  if (p.status === "PLANNED" && !p.legacy_trade_id) {
    const cancel = el("button", { class: "plain", text: "Cancel this plan" });
    cancel.addEventListener("click", () => act(cancel, () => api(`/v2/positions/${p.id}/cancel`, { note: "" })));
    buttons.append(cancel);
  }
  box.append(buttons, error);
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

Object.assign(BUILDERS, { positions: buildPositions });
