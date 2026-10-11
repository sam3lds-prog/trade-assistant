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
      listBox.replaceChildren(...(d.invalid || []).map(u => unreadablePanel(u, async () => { await refresh(); })), positionTable(d.positions, p => openDetail(p.id)));
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

  const rebuild = el("button", { class: "plain small", text: "Rebuild missing snapshots" });
  const rebuildNote = el("span", { class: "small muted", "aria-live": "polite" });
  rebuild.addEventListener("click", () => withBusy(rebuild, error, async () => {
    const r = await api("/v2/snapshots/backfill", { dry_run: false, upgrade_hindsight: true });
    rebuildNote.textContent = ` ${r.created} added, ${r.superseded} replaced, ${r.unavailable} not available from the price history (${r.positions_checked} positions checked).`;
    await refresh(); if (selected) openDetail(selected);
  }));
  const view = el("section", {},
    el("h2", { text: "Positions" }),
    el("p", { class: "lede", text: "Stock you are long or short, and the calls and puts you hold, in one place. Each position is built from its fills, so partial entries and exits, average cost, fees and profit or loss come out right. Record what you did at your broker here; this app never places orders." }),
    error,
    el("div", { class: "panel" }, el("div", { class: "panel-head" }, el("h3", { text: "Your positions" }), filter), summary, listBox,
      el("p", { class: "small", style: "margin-top:8px" }, rebuild, rebuildNote)),
    detailBox, form);
  view.onShow = () => { refresh(); if (selected) openDetail(selected); };
  return view;
}

// A position whose fills no longer add up (data from an older version, say): left out of every figure,
// shown with its fills so the wrong one can be corrected.
function unreadablePanel(u, onChange) {
  const box = el("div", { class: "panel" }, el("h3", {}, `${u.description} `, statusTag("fail", "Fills do not add up")), banner("fail", u.text));
  const formBox = el("div");
  box.append(table([
    { label: "When", cell: x => timeText(x.executed_at) },
    { label: "Action", cell: x => SIDE_WORDS[x.side] || x.side },
    // As stored, however small: a quantity too small to show is often what is wrong.
    { label: "Quantity", num: true, cell: x => isNum(x.quantity) && x.quantity > 0 && x.quantity < 0.0001 ? x.quantity.toExponential(1) : qtyText(x.quantity) },
    { label: "Price", num: true, cell: x => money(x.price) },
    { label: "Fees", num: true, cell: x => isNum(x.fees) ? money(x.fees) : String(x.fees) },
    { label: "", cell: x => { if (u.legacy_trade_id) return ""; const b = el("button", { class: "plain small", text: "Correct" }); b.addEventListener("click", () => formBox.replaceChildren(correctionForm(u, x, onChange))); return b; } }], u.executions), formBox);
  if (u.legacy_trade_id) box.append(el("p", { class: "small muted", text: "It mirrors an option-journal entry: fix the entry in the journal, or run Repair on the Data health screen to re-read it." }));
  return box;
}

function positionSummary(rows, which) {
  if (!rows.length) return el("div");
  // A position whose figure is unknown is left out of the sum and counted, never added in as $0.
  const sum = key => rows.reduce((a, p) => a + (isNum(p.pnl[key]) ? p.pnl[key] : 0), 0);
  const unknown = key => rows.filter(p => p.status !== "PLANNED" && !isNum(p.pnl[key])).length;
  const total = key => unknown(key) ? el("span", {}, signedMoney(sum(key)), el("span", { class: "small muted", text: " + unknown" })) : signedMoney(sum(key));
  const counted = rows.filter(p => p.status !== "PLANNED").length;
  const invalid = rows.filter(p => p.thesis_check.status === "INVALIDATED").length;
  const longs = rows.filter(p => p.bias > 0 && p.status !== "PLANNED").length, shorts = rows.filter(p => p.bias < 0 && p.status !== "PLANNED").length;
  return el("div", { class: "metrics", style: "margin-bottom:12px" },
    metric(String(rows.length), which === "all" ? "Positions" : `${which[0].toUpperCase()}${which.slice(1)} positions`),
    metric(`${longs} / ${shorts}`, "Gain if the stock rises / falls"),
    metric(total("unrealized"), `Unrealized profit or loss${partialNote(unknown("unrealized"), counted)}`),
    metric(total("realized"), "Realized, after fees"),
    metric(String(invalid), "Through their invalidation price"));
}

function positionTable(rows, onRow) {
  if (!rows.length) return el("p", { class: "muted", text: "None here. Add a position below, or record a trade in the option journal and it appears here too." });
  return table([
    { label: "Position", cell: p => el("span", {}, el("strong", { text: p.description }), p.legacy_trade_id ? el("span", { class: "small muted", text: " (journal)" }) : null) },
    { label: "Status", cell: p => tagOf(POS_STATUS, p.status) },
    { label: "Open", num: true, cell: p => qtyText(p.ledger.open_quantity) },
    { label: "Average entry", num: true, cell: p => money(p.ledger.average_entry_price) },
    { label: "Now", num: true, cell: p => p.mark ? (isNum(p.mark.price) ? el("span", {}, money(p.mark.price), freshTag(p.mark.freshness, true) ? " " : "", freshTag(p.mark.freshness, true)) : el("span", { class: "muted", title: p.mark.error || "", text: "no price" })) : "" },
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
    led.open_quantity > 0 ? metric(isNum(led.cost_basis) ? money(led.cost_basis * led.open_quantity * p.multiplier, 0) : "n/a", p.direction === "SHORT" ? "Received for the open position" : "Cost of the open position") : null,
    led.open_quantity > 0 ? metric(isNum(pnl.market_value) ? money(Math.abs(pnl.market_value), 0) : unknownText(mk.error || ""), p.direction === "SHORT" ? "To buy it back at the mark" : "Worth at the mark") : null,
    metric(signedMoney(pnl.unrealized), "Unrealized"),
    metric(signedMoney(pnl.realized), "Realized, after fees"),
    metric(money(pnl.fees), "Fees paid"),
    metric(isNum(pnl.r_multiple) ? `${pnl.r_multiple > 0 ? "+" : ""}${num(pnl.r_multiple, 2)}R` : "n/a", isNum(pnl.initial_risk) ? `Total in units of the ${money(pnl.initial_risk, 0)} risked` : "R multiple (set an invalidation or risk amount)"),
    isNum(p.days_held) ? metric(String(p.days_held), "Days held") : null));
  if (mk.error) box.append(el("p", { class: "small error", text: `No current price: ${mk.error}` }));
  if (leg && isNum(mk.delta)) box.append(el("p", { class: "small muted", text: `Option now: bid ${money(mk.bid)}, ask ${money(mk.ask)}, delta ${num(mk.delta, 2)}, theta ${money(mk.theta)} a day, implied volatility ${pct(100 * mk.iv, 1)}, ${mk.dte} days to expiration. Stock ${money(mk.underlying)}.` }));
  if (p.mark && isNum(mk.price) && led.open_quantity > 0) box.append(markLine(mk, pnl));
  box.append(...lifecycleBlock(p, onChange));
  const tc = p.thesis_check;
  if (tc.text) box.append(banner(tc.status === "INVALIDATED" ? "fail" : tc.status === "INTACT" || tc.status.startsWith("TARGET") ? "pass" : "warn", tc.text));
  if (led.lifecycle_issues && led.lifecycle_issues.length) box.append(banner("warn", `This position was opened again after it had closed (${led.lifecycle_issues.map(i => timeText(i.executed_at)).join(", ")}), which versions before 2.1.1 allowed. Its fills are kept exactly as recorded and its figures still add them up, but it mixes two trades. New fills after a close now go on a new position.`));

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
      const v = optNum(input);
      // A blank box clears a value that was set (sent as null); a box that was already blank is left out.
      if (v !== null) body[k] = k === "intended_horizon" ? Math.round(v) : v;
      else if (p[k] !== null && p[k] !== undefined) body[k] = null;
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
    { label: "Recorded from", cell: x => ({ MANUAL: "you", IMPORT: p.legacy_trade_id ? "option journal" : "import", ROBINHOOD: "Robinhood" })[x.source] || x.source },
    { label: "Note", cell: x => x.note || "" },
    ...(p.legacy_trade_id ? [] : [{ label: "", cell: x => { const b = el("button", { class: "plain small", text: "Correct" }); b.addEventListener("click", ev => { ev.stopPropagation(); fixBox.replaceChildren(correctionForm(p, x, onChange)); }); return b; } }])],
    p.executions) : el("p", { class: "small muted", text: "No fills yet: this is a plan." }));
  const fixBox = el("div");
  box.append(fixBox);
  if (p.corrections && p.corrections.length) {
    box.append(el("h4", { text: "Corrections" }), table([
      { label: "When", cell: c => timeText(c.changed_at) },
      { label: "Fill", num: true, cell: c => String(c.execution_id ?? "") },
      { label: "Changed", cell: c => Object.keys(c.new || {}).filter(k => JSON.stringify(c.new[k]) !== JSON.stringify((c.previous || {})[k])).map(k => `${k.replace("_", " ")}: ${fillValue(k, c.previous[k])} → ${fillValue(k, c.new[k])}`).join("; ") || c.action.toLowerCase() },
      { label: "Why", cell: c => c.reason },
      { label: "By", cell: c => ({ USER: "you", JOURNAL_SYNC: "option journal", REPAIR: "repair" })[c.source] || c.source }], p.corrections),
      el("p", { class: "small muted", text: "Fills are corrected in place and every change is kept here with the values before and after." }));
  }
  if (p.legacy_trade_id) {
    box.append(el("p", { class: "small muted", text: "This position mirrors an entry in the option journal. Log its fills there and they appear here." }));
  } else if (p.status === "CLOSED") {
    box.append(el("p", { class: "small muted", text: "This position is closed and takes no more fills. A new trade in the same stock is a new position." }));
  } else if (p.status !== "CANCELLED") {
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
    { label: "Snapshot · market as of", cell: s => `${s.snapshot_type[0]}${s.snapshot_type.slice(1).toLowerCase()} · ${timeText(s.timestamp)}` },
    { label: "Stock", num: true, cell: s => money(s.underlying_price) },
    { label: "RSI", num: true, cell: s => num(s.rsi_14, 0) },
    { label: "MACD hist.", num: true, cell: s => num(s.macd_histogram, 2) },
    { label: "50-day", num: true, cell: s => money(s.sma_50) },
    { label: "Swings", cell: s => s.structure_state || "n/a" },
    { label: "Trend", cell: s => s.trend_state ? s.trend_state.replaceAll("_", " ").toLowerCase() : "n/a" },
    { label: "Market", cell: s => s.market_regime ? s.market_regime.replaceAll("_", " ").toLowerCase() : "n/a" },
    ...(leg ? [{ label: "Delta", num: true, cell: s => num(s.delta, 2) }, { label: "IV", num: true, cell: s => isNum(s.iv) ? pct(100 * s.iv, 0) : "n/a" }] : []),
    { label: "Daily bar", cell: s => s.bar_status ? `${s.bar_status.toLowerCase()} · ${s.data_timestamp}` : (s.data_timestamp || "n/a") },
    { label: "Source", cell: snapshotSource },
  ], p.snapshots) : el("p", { class: "small muted", text: "None yet." }));
  if (p.snapshots.some(s => s.effective !== false && s.uses_later_data)) box.append(banner("warn", "A snapshot here was built from market data after the fill (marked hindsight). It is shown, but analytics do not use it as what was known at entry. Rebuilding snapshots replaces it with one from the last close before the fill."));
  if (p.executions.length && !p.snapshots.some(s => s.snapshot_type === "ENTRY" && s.effective !== false)) box.append(el("p", { class: "small muted", text: "Historical entry-time market data is unavailable for this position so far. Rebuild snapshots on the Positions screen to add it where the price history allows." }));

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
  if (p.instrument_type === "STOCK" && p.executions.length && !p.legacy_trade_id) box.append(adjustmentsBlock(p, onChange));
  if (p.executions.length) box.append(positionAnalyticsBox(p));
  box.append(buttons, error, gradeBox);
  return box;
}

/* ---------- financing and option lifecycle (2.2) ---------- */
const LIFE_TAG = { OPEN: ["pass", "Open"], CLOSED: ["info", "Sold"], EXPIRED_WORTHLESS: ["info", "Expired worthless"], EXERCISED: ["info", "Exercised"], UNRESOLVED: ["warn", "Expired: outcome not recorded"] };
const ADJ_WORDS = { BORROW_FEE: "Borrow fee", BORROW_RATE: "Borrow rate", DIVIDEND_OWED: "Dividend owed", DIVIDEND_RECEIVED: "Dividend received", CASH_ADJUSTMENT: "Cash adjustment", SPLIT: "Stock split" };

function lifecycleBlock(p, onChange) {
  const out = [];
  const fin = p.financing || {};
  if (fin.entries || fin.borrow_status) {
    out.push(el("div", { class: "metrics" },
      metric(signedMoney(p.pnl.trading_total), "Trading profit (the fills)"),
      metric(signedMoney(p.pnl.financing), "Borrow fees, dividends and other cash"),
      metric(signedMoney(p.pnl.net_economic), "Net after both")));
    if (fin.borrow_status && fin.borrow_status !== "RECORDED") out.push(banner("warn", `${fin.borrow_text} Unknown financing cost: net profit leaves it out.${isNum(fin.borrow_estimate) ? ` At the rate you entered it would be about ${signedMoney(fin.borrow_estimate)}: an estimate, not a charge, and not included.` : ""}`));
  }
  if (p.ledger.units_adjusted) out.push(el("p", { class: "small muted", text: "A split or contract adjustment applies to this position: the fills below are shown as recorded, and every figure uses today's units." }));
  for (const d of p.derived_from || []) out.push(el("p", { class: "small muted", text: `These shares came from exercising position ${d.position_id} at a stock price of ${money(d.underlying_price)} (${d.underlying_price_source}).` }));
  const life = p.lifecycle || {};
  if (p.instrument_type !== "OPTION" || !life.state) return out;
  out.push(el("p", { class: "small" }, "Option: ", tagOf(LIFE_TAG, life.state)));
  if (life.text) out.push(banner("warn", life.text));
  if ((p.option_events || []).length) out.push(table([
    { label: "When", cell: e => timeText(e.event_at) },
    { label: "What", cell: e => ({ EXPIRED_WORTHLESS: "Expired worthless", EXERCISED: "Exercised", CONTRACT_ADJUSTED: "Contract adjusted" })[e.event] },
    { label: "Stock price", num: true, cell: e => isNum(e.underlying_price) ? el("span", { title: e.underlying_price_source || "" }, money(e.underlying_price)) : "" },
    { label: "Detail", cell: e => e.event === "CONTRACT_ADJUSTED" ? `strike ${e.previous.strike} → ${e.new.strike}, multiplier ${e.previous.multiplier} → ${e.new.multiplier}${e.new.quantity_factor !== 1 ? `, ${e.new.quantity_factor} per old contract` : ""}` : e.resulting_position_id ? `shares on position ${e.resulting_position_id}` : "" },
    { label: "Why", cell: e => e.reason || "" }], p.option_events));
  if (p.legacy_trade_id || p.ledger.open_quantity <= 0) return out;
  const error = el("div", { class: "error", role: "alert" });
  const act = (b, fn) => withBusy(b, error, async () => onChange(await fn()));
  const sPrice = numInput({ placeholder: "from the daily close" }), reason = textInput({ placeholder: "optional" });
  const when = timeInput(), applyTo = textInput({ placeholder: "new position", "aria-label": "Stock position id" });
  const expire = el("button", { class: "plain", text: "It expired worthless" });
  const exer = el("button", { class: "plain", text: "It was exercised" });
  expire.addEventListener("click", () => act(expire, () => api(`/v2/positions/${p.id}/option/expire`, { underlying_price: optNum(sPrice), reason: reason.value })));
  exer.addEventListener("click", () => act(exer, () => api(`/v2/positions/${p.id}/option/exercise`, { underlying_price: optNum(sPrice), executed_at: when.value || null, reason: reason.value,
    apply_to_position_id: applyTo.value.trim() ? Number(applyTo.value) : null })));
  out.push(el("h4", { text: "How it ended" }),
    el("p", { class: "small muted", text: "Record the outcome rather than letting it be assumed. Worthless closes the contracts at $0 at the expiration close; if the stock finished in the money you will be asked why it was not exercised. Exercise closes the contracts at their value against the stock price and records the shares at that price, on a new stock position or on the one you name." }),
    el("div", { class: "inline-form" }, field("Stock price", sPrice), field("Exercised when (blank: now or at expiration)", when), field("Shares go on position", applyTo), field("Reason", reason), expire, exer), error);
  return out;
}

// A split (or voiding one) that would leave shares open on a closed position is refused until confirmed:
// the refusal is shown with a button that sends the same request again, confirmed, with the reason given.
async function withReopen(button, errorBox, send) {
  errorBox.replaceChildren();
  try { await withBusy(button, errorBox, () => send(false)); }
  finally {
    if (/confirm_reopen/.test(errorBox.textContent)) {
      const again = el("button", { class: "plain small", text: "Record it and reopen the position" });
      again.addEventListener("click", () => withBusy(again, errorBox, () => send(true)));
      errorBox.append(" ", again);
    }
  }
}

function adjustmentsBlock(p, onChange) {
  const box = el("div");
  const error = el("div", { class: "error", role: "alert" });
  const kinds = p.direction === "SHORT" ? ["BORROW_FEE", "DIVIDEND_OWED", "BORROW_RATE", "CASH_ADJUSTMENT", "SPLIT"] : ["DIVIDEND_RECEIVED", "CASH_ADJUSTMENT", "SPLIT"];
  box.append(el("h4", { text: p.direction === "SHORT" ? "Borrow fees, dividends and other cash" : "Dividends and other cash" }));
  if ((p.adjustments || []).length) box.append(table([
    { label: "Date", cell: a => timeText(a.effective_at) },
    { label: "What", cell: a => el("span", { class: a.voided_at ? "muted" : "" }, ADJ_WORDS[a.kind] || a.kind, a.voided_at ? el("span", { title: a.void_reason }, " (void)") : "") },
    { label: "Amount", num: true, cell: a => a.kind === "SPLIT" ? `${a.ratio} for 1` : a.kind === "BORROW_RATE" ? `${a.rate_pct}% a year` : signedMoney(a.amount, 2) },
    { label: "Covers", cell: a => a.period_start ? `${shortDate(a.period_start)} to ${shortDate(a.period_end)}` : "" },
    { label: "Note", cell: a => a.note || "" },
    { label: "", cell: a => { if (a.voided_at) return ""; const b = el("button", { class: "plain small", text: "Void" }); b.addEventListener("click", ev => { ev.stopPropagation(); const why = prompt("Why is this entry wrong?"); if (why) withReopen(b, error, async confirm => onChange(await api(`/v2/positions/${p.id}/adjustments/${a.id}/void`, { reason: why, confirm_reopen: confirm }))); }); return b; } }], p.adjustments));
  const kind = el("select", { "aria-label": "Kind" }, kinds.map(k => el("option", { value: k, text: ADJ_WORDS[k] })));
  const amount = el("input", { type: "number", step: "0.01", placeholder: "-12.50 for a charge" }), date = timeInput();
  const perShare = numInput({ placeholder: "dividend per share" }), shares = numInput({ step: "any", placeholder: "shares" });
  const rate = numInput({ placeholder: "% a year" }), ratio = numInput({ step: "any", placeholder: "new per old" });
  const from = timeInput(), to = timeInput(), note = textInput({ placeholder: "optional" });
  const add = el("button", { class: "plain", text: "Record it" });
  add.addEventListener("click", () => withReopen(add, error, async confirm => {
    const body = { kind: kind.value, effective_at: date.value || null, note: note.value, confirm_reopen: confirm };
    for (const [k, input] of [["amount", amount], ["per_share", perShare], ["quantity", shares], ["rate_pct", rate], ["ratio", ratio]]) { const v = optNum(input); if (v !== null) body[k] = v; }
    if (from.value) body.period_start = from.value; if (to.value) body.period_end = to.value;
    onChange(await api(`/v2/positions/${p.id}/adjustments`, body));
  }));
  box.append(el("div", { class: "inline-form" }, field("Kind", kind), field("Amount ($)", amount), field("Date", date), field("Per share", perShare), field("Shares", shares),
      field("Rate", rate), field("Split ratio", ratio), field("Fee covers from", from), field("to", to), field("Note", note), add),
    el("p", { class: "small muted", text: "Enter what your broker booked; nothing here is estimated. A split replays earlier fills in post-split shares and rescales the plan's prices; the fills themselves are kept as recorded." }), error);
  return box;
}

function markLine(mk, pnl) {
  const parts = [`Marked at ${money(mk.price)} (${mk.mark_basis || mk.source || "mark"})`];
  if (isNum(mk.bid) && isNum(mk.ask)) parts.push(`bid ${money(mk.bid)}, ask ${money(mk.ask)}${isNum(mk.spread_pct) ? `, spread ${pct(mk.spread_pct, 1)} of the midpoint` : ""}`);
  if (isNum(mk.last) && !(mk.mark_basis || "").startsWith("last")) parts.push(`last trade ${money(mk.last)}`);
  if (isNum(pnl.liquidation_unrealized)) parts.push(`closing now at ${mk.liquidation_basis ? mk.liquidation_basis.split(":")[0] : "the quote"} would be about ${signedMoney(pnl.liquidation_unrealized)} unrealized, an estimate rather than a fill`);
  else parts.push("no two-sided quote, so no estimate of what closing now would get");
  if (mk.as_of) parts.push(`quote ${timeText(mk.as_of)}`);
  const line = el("p", { class: "small muted", text: parts.join("; ") + ". " });
  const tag = freshTag(mk.freshness);
  if (tag) line.append(tag);
  if (mk.freshness && mk.freshness.freshness === "STALE") line.append(el("span", { class: "error", text: ` ${(mk.freshness.warnings || []).join("; ")}. Refresh before relying on this price.` }));
  return line;
}

// How old a price is, as a tag. FRESH and the latest session's price need no tag in tight spaces (`quiet`).
const FRESH_TAG = { FRESH: ["pass", "Live"], DELAYED: ["info", "Delayed"], LAST_SESSION: ["info", "Market closed"], STALE: ["warn", "Stale"], UNKNOWN: ["warn", "Time unknown"] };
function freshTag(obs, quiet = false) {
  if (!obs || !obs.freshness) return null;
  if (quiet && (obs.freshness === "FRESH" || obs.freshness === "LAST_SESSION")) return null;
  const [lvl, word] = FRESH_TAG[obs.freshness] || ["info", obs.freshness];
  const age = isNum(obs.age_seconds) ? ageShort(obs.age_seconds) : "";
  const tag = statusTag(lvl, word + (age && obs.freshness !== "LAST_SESSION" ? `, ${age}` : ""));
  tag.title = [obs.observed_at ? `price time ${timeText(obs.observed_at)}` : "", `from ${obs.provider || "the source"}`, ...(obs.warnings || [])].filter(Boolean).join(" · ");
  return tag;
}
const ageShort = s => s < 90 ? `${Math.round(s)}s` : s < 5400 ? `${Math.round(s / 60)} min` : s < 172800 ? `${(s / 3600).toFixed(1)} h` : `${(s / 86400).toFixed(1)} days`;

const SNAP_METHOD = { LIVE: "Live", PRIOR_BAR: "Rebuilt: last close before the fill", END_OF_DAY: "Rebuilt: fill day's close", JOURNAL_QUOTE: "Journal ticket quote" };
function snapshotSource(s) {
  const parts = [SNAP_METHOD[s.method] || (s.method ? s.method.toLowerCase() : "not recorded")];
  if (s.capture_timing === "RETROSPECTIVE" && s.method === "LIVE") parts.push("taken later");
  const node = el("span", { title: [s.provenance && s.provenance.note, s.observation_cutoff ? `data up to ${timeText(s.observation_cutoff)}` : "", s.superseded_reason ? `replaced: ${s.superseded_reason}` : ""].filter(Boolean).join(" · ") }, parts.join(", "));
  if (s.uses_later_data) node.append(" ", statusTag("warn", "hindsight"));
  if (s.effective === false) { node.append(" ", statusTag("info", "replaced")); node.classList.add("muted"); }
  return node;
}
const fillValue = (k, v) => v === null || v === undefined ? "n/a" : k === "executed_at" ? timeText(v) : k === "side" ? (SIDE_WORDS[v] || v) : k === "price" || k === "fees" ? money(v) : String(v);

function correctionForm(p, x, onChange) {
  const error = el("div", { class: "error", role: "alert" });
  const sides = p.direction === "LONG" ? [["BUY", "Buy"], ["SELL", "Sell"]] : [["SELL_SHORT", "Sell short"], ["BUY_TO_COVER", "Buy to cover"], ["SELL", "Sell (opens)"], ["BUY", "Buy (covers)"]];
  const side = el("select", { "aria-label": "Action" }, sides.map(([v, t]) => el("option", { value: v, text: t }))); side.value = x.side;
  const qty = el("input", { type: "number", min: 0, step: p.instrument_type === "OPTION" ? 1 : "any", value: x.quantity });
  const price = numInput({ value: x.price }), fees = numInput({ value: x.fees });
  const local = new Date(x.executed_at).toLocaleString("sv-SE", { timeZone: "America/New_York" }).replace(" ", "T").slice(0, 16);
  const when = timeInput(); when.value = local;
  const reason = textInput({ placeholder: "What was wrong, for the record" });
  const save = el("button", { class: "primary", text: "Save the correction" });
  save.addEventListener("click", () => withBusy(save, error, async () => {
    const body = { reason: reason.value.trim() };
    if (side.value !== x.side) body.side = side.value;
    if (Number(qty.value) !== x.quantity) body.quantity = Number(qty.value);
    if (Number(price.value) !== x.price) body.price = Number(price.value);
    if (Number(fees.value) !== x.fees) body.fees = Number(fees.value);
    if (when.value && when.value !== local) body.executed_at = when.value;
    if (Object.keys(body).length === 1) throw new Error("Change at least one value.");
    if (body.reason.length < 3) throw new Error("Say why the fill is being corrected.");
    onChange(await api(`/v2/positions/${p.id}/executions/${x.id}/correct`, body));
  }));
  return el("div", { class: "panel", style: "margin-top:8px" }, el("h4", { text: `Correct the fill of ${timeText(x.executed_at)}` }),
    el("p", { class: "small muted", text: "Fix what was entered wrongly. The fill is changed in place, the position's figures are worked out again, and the old and new values are kept with your reason." }),
    el("div", { class: "inline-form" }, field("Action", side), field("Quantity", qty), field("Price", price), field("Fees", fees), field("When (New York time)", when)),
    field("Reason", reason), el("div", { class: "row", style: "margin-top:8px" }, save), error);
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
    el("p", { class: "lede", text: "Which way the stock is trending and why, then your thesis: the stock trade graded, and calls or puts that fit it. Scores count evidence; they are not chances." }),
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
      isNum(q.last) ? el("span", { text: `${money(q.last)}${isNum(q.prevclose) ? ` (${signedPct(100 * (q.last / q.prevclose - 1))} today)` : ""} ` }) : null,
      freshTag(q.freshness),
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
  if (dq) frag.append(el("div", { class: "panel" }, el("h3", {}, "Data checks ", dq.bars_freshness ? el("span", { class: "small muted", text: `daily bars through ${dq.bars_freshness.last_finished_bar || "n/a"} ` }) : "", dq.bars_freshness ? freshTag(dq.bars_freshness, true) : ""),
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

// The closing price over the last year on its trend state. With a `plan` (2.5, from the thesis on Decide) the
// axis runs on to the horizon date and adds the plan: target and invalidation lines, the range option prices
// imply by then (one standard deviation, widening with time), and where the chosen contract breaks even then.
function trendHistoryChart(t, plan) {
  const rows = t.history, n = rows.length;
  const wrap = el("div", { class: "panel trend-chart" }, el("h3", { text: plan ? `Trend state over the last year, and your plan to ${shortDate(plan.horizonDate)}` : "Trend state over the last year" }));
  wrap.trend = t;
  if (n < 2) return wrap;
  const f = plan ? Math.max(1, plan.horizonDays) : 0, last = rows[n - 1].close;
  const W = 760, H = plan ? 290 : 250, pad = { l: 58, r: plan ? 118 : 16, t: plan ? 18 : 10, b: 54 }, L = pad.l, R = W - pad.r, bandTop = H - pad.b + 8, bandH = 14;
  const X = i => L + (R - L) * i / (n - 1 + f);
  const levels = plan ? plan.levels.filter(lv => isNum(lv.price)) : [];
  const band = plan && plan.band && isNum(plan.band.low) && isNum(plan.band.high) ? plan.band : null;
  const be = plan && plan.breakeven && isNum(plan.breakeven.price) ? plan.breakeven : null;
  const span = [...rows.map(r => r.close), ...levels.map(lv => lv.price), ...(band ? [band.low, band.high] : []), ...(be ? [be.price] : [])];
  const ys = niceScale(Math.min(...span), Math.max(...span), 5);
  const Y = v => pad.t + (H - pad.b - pad.t) * (1 - (v - ys.lo) / (ys.hi - ys.lo));
  const root = svg("svg", { viewBox: `0 0 ${W} ${H}`, role: "img", "aria-label": `${t.symbol} closing price with its trend state on each day${plan ? ", and the plan to the horizon date" : ""}` });
  let start = 0;
  for (let i = 1; i <= n; i++) {
    if (i === n || rows[i].state !== rows[start].state) {
      const x0 = start === 0 ? L : (X(start - 1) + X(start)) / 2, x1 = i === n ? (plan ? X(n - 1) : R) : (X(i - 1) + X(i)) / 2;
      root.append(svg("rect", { x: x0, y: pad.t, width: Math.max(0.5, x1 - x0), height: H - pad.b - pad.t, fill: TREND_FILL[rows[start].state], opacity: 0.45 }),
        svg("rect", { x: x0, y: bandTop, width: Math.max(0.5, x1 - x0), height: bandH, fill: TREND_FILL[rows[start].state] }));
      start = i;
    }
  }
  for (const v of ys.ticks) root.append(svg("line", { class: "grid", x1: L, x2: R, y1: Y(v), y2: Y(v) }),
    svg("text", { x: L - 8, y: Y(v) + 4, "text-anchor": "end", text: money(v, ys.step < 1 ? 2 : 0) }));
  rows.forEach((r, i) => { if (i > 0 && r.date.slice(5, 7) !== rows[i - 1].date.slice(5, 7) && X(i) < (plan ? X(n - 1) - 24 : R - 24)) root.append(svg("text", { x: X(i), y: bandTop + bandH + 14, "text-anchor": "middle", text: monthLabel(r.date) })); });
  if (plan) {
    const x0 = X(n - 1), x1 = X(n - 1 + f);
    if (band) {
      // The range widens with the square root of time, from today's close to the band on the horizon date.
      const up = Math.log(band.high / last), dn = Math.log(band.low / last), steps = 24;
      const pts = k => { const u = k / steps; return [x0 + (x1 - x0) * u, Math.sqrt(u)]; };
      const top = [...Array(steps + 1).keys()].map(k => { const [x, r] = pts(k); return `${x.toFixed(1)} ${Y(last * Math.exp(up * r)).toFixed(1)}`; });
      const bot = [...Array(steps + 1).keys()].reverse().map(k => { const [x, r] = pts(k); return `${x.toFixed(1)} ${Y(last * Math.exp(dn * r)).toFixed(1)}`; });
      root.append(svg("path", { class: "band", d: `M${top.join(" L")} L${bot.join(" L")} Z` }));
    }
    root.append(svg("line", { class: "now-line", x1: x0, x2: x0, y1: pad.t, y2: H - pad.b }),
      svg("line", { class: "grid", x1: x1, x2: x1, y1: pad.t, y2: H - pad.b, "stroke-dasharray": "4 3" }),
      svg("text", { x: x1, y: bandTop + bandH + 14, "text-anchor": "middle", text: shortDate(plan.horizonDate).replace(/, \d{4}$/, "") }));
    const labels = [];
    for (const lv of levels) {
      root.append(svg("line", { x1: L, x2: x1, y1: Y(lv.price), y2: Y(lv.price), stroke: `var(${lv.color})`, "stroke-width": 1.5, "stroke-dasharray": lv.dash || null }));
      labels.push({ text: `${lv.label} ${money(lv.price, 0)}`, y: Y(lv.price) + 4 });
    }
    if (be) {
      root.append(svg("path", { d: `M${x1} ${Y(be.price) - 6} L${x1 + 6} ${Y(be.price)} L${x1} ${Y(be.price) + 6} L${x1 - 6} ${Y(be.price)} Z`, fill: "var(--series-2)", stroke: "var(--panel)", "stroke-width": 1.5 }));
      labels.push({ text: `${be.label} ${money(be.price, 0)}`, y: Y(be.price) + 4 });
    }
    for (const e of spreadLabels(labels, 13, pad.t + 4, H - pad.b)) root.append(halo({ x: x1 + 10, y: e.y, class: "lbl", text: e.text }));
  }
  root.append(svg("path", { d: rows.map((r, i) => `${i ? "L" : "M"}${X(i).toFixed(1)} ${Y(r.close).toFixed(1)}`).join(" "), fill: "none", stroke: "var(--ink)", "stroke-width": 1.6 }));
  if (!rows[n - 1].complete) root.append(svg("circle", { cx: X(n - 1), cy: Y(rows[n - 1].close), r: 3.5, fill: "var(--panel)", stroke: "var(--ink)", "stroke-dasharray": "2 1.5" }));
  const hover = svg("line", { class: "hover-line", y1: pad.t, y2: bandTop + bandH, visibility: "hidden" });
  root.append(hover);
  const tip = el("div", { class: "tip" });
  root.addEventListener("pointermove", evt => {
    const box = root.getBoundingClientRect();
    const i = Math.max(0, Math.min(n - 1 + f, Math.round(((evt.clientX - box.left) * W / box.width - L) / (R - L) * (n - 1 + f))));
    if (i > n - 1) {
      hover.setAttribute("x1", X(n - 1 + f)); hover.setAttribute("x2", X(n - 1 + f)); hover.setAttribute("visibility", "visible");
      tip.replaceChildren(el("div", { class: "tip-head", text: `${shortDate(plan.horizonDate)}, the horizon` }),
        ...levels.map(lv => el("div", { text: `${lv.label}: ${money(lv.price)}` })),
        band ? el("div", { text: `Option prices imply a typical range of ${money(band.low)} to ${money(band.high)} (one standard deviation)` }) : null,
        be ? el("div", { text: `${be.legend || be.label}: ${money(be.price)}` }) : null);
      tooltipAt(tip, box, X(n - 1 + f), W);
      return;
    }
    hover.setAttribute("x1", X(i)); hover.setAttribute("x2", X(i)); hover.setAttribute("visibility", "visible");
    tip.replaceChildren(el("div", { class: "tip-head", text: shortDate(rows[i].date) + (rows[i].complete ? "" : " (so far)") }),
      el("div", { text: `${money(rows[i].close)} · ${trendWord(rows[i].state)}` }), el("div", { class: "tip-sub", text: `Net ${num(rows[i].net, 0)} points` }));
    tooltipAt(tip, box, X(i), W);
  });
  root.addEventListener("pointerleave", () => { hover.setAttribute("visibility", "hidden"); tip.style.display = "none"; });
  const present = TREND_ORDER.filter(s => rows.some(r => r.state === s));
  wrap.append(el("div", { class: "chart" }, root, tip),
    el("div", { class: "legend" }, present.map(s => el("span", {}, el("b", { class: "key-box", style: `background:${TREND_FILL[s]}` }), trendWord(s))),
      plan ? levels.map(lv => el("span", {}, el("i", { style: `background: var(${lv.color})` }), lv.label)) : null,
      band ? el("span", {}, el("b", { class: "key-box band-key" }), "Range option prices imply (±1 sd)") : null,
      be ? el("span", {}, el("i", { style: "background: var(--series-2)" }), be.legend || be.label) : null));
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
  const periods = isNum(h.independent_periods) ? ` ${num(h.independent_periods, 0)} separate periods; the range allows for cases that overlap.` : "";
  return el("span", { title: `Historical frequency, not a forecast. Average ${signedPct(100 * h.mean_return, 1)} over ${h.horizon} days, ${signedPct(100 * (h.mean_excess_vs_spy || 0), 1)} against SPY.${periods}` },
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
      box.append(el("p", { class: "small" }, calTag(h.calibration_status, sc.calibrated), ` ${h.verdict}`));
      if (b && b.cases > 0 && isNum(b.frequency)) box.append(el("p", { class: "small", text: `Net score ${num(sc.net_score_for_calibration, 0)} on ${shortDate(sc.date)} is in the ${num(b.low, 0)} to ${num(b.high, 0)} range. On ${num(b.cases, 0)} past days in that range across the stocks in the history, the stock was higher 10 days later ${pct(100 * b.frequency, 0)} of the time (${pct(100 * b.frequency_low, 0)}–${pct(100 * b.frequency_high, 0)})` +
        (h.same_regime && h.same_regime.cases > 0 ? `; ${pct(100 * h.same_regime.frequency, 0)} of ${num(h.same_regime.cases, 0)} in the same market regime.` : ".") }));
      else box.append(el("p", { class: "small muted", text: `Net score ${num(sc.net_score_for_calibration, 0)}: no past days in that range to compare with.` }));
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
      el("p", { class: "small" }, calTag(cal.calibration_status, cal.calibrated), ` ${cal.verdict}`),
      cal.interval_method ? el("p", { class: "small muted", text: `Ranges: ${cal.interval_method}.` }) : null,
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
  const analog = ev.analogs && ev.analogs.history
    ? (ev.analogs.history.same_regime && ev.analogs.history.same_regime.cases ? ev.analogs.history.same_regime : ev.analogs.history.all) : null;
  if (analog && analog.cases > 0 && isNum(analog.frequency)) {
    const h = analog;
    box.append(el("p", { class: "small", text: `Analogs: when a stock's trend turned to ${trendWord(ev.trend_state).toLowerCase()} before, it moved the expected way over 10 days in ${pct(100 * h.frequency, 0)} of ${num(h.cases, 0)} cases.` }));
  }
  if (ev.what_would_change_it.length) box.append(el("h4", { text: "What would improve it" }), el("ul", { class: "plain small" }, ev.what_would_change_it.map(t => el("li", { text: t }))));
  box.append(el("p", { class: "small muted", text: ev.note }));
  return box;
}

// One thesis for the whole decision (2.5): Today and the scanner set the ticker and direction, this form
// carries the levels, and the stock grade and the contract picker both read it. Kept in the page only.
state.thesis = null;
// Open Decide on a stock. With a direction (from Today, or a scanner row that has one) the thesis is set to
// it and graded straight away; without one the form starts from the stock's trend and waits.
state.openDecide = (symbol, direction, hint) => {
  symbol = String(symbol || "").trim().toUpperCase();
  const kept = state.thesis && state.thesis.symbol === symbol ? state.thesis : {};
  if (direction) { state.thesis = FLOW.thesis({ ...kept, symbol, direction }); state.decideHint = hint || "run"; }
  else { state.thesis = kept.symbol ? state.thesis : null; state.decideHint = null; }
  show("analyze");
  state.views.analyze.analyze(symbol);
};

function thesisSection(d) {
  const now = d.trend.provisional || d.trend.confirmed;
  const kept = state.thesis && state.thesis.symbol === d.symbol ? state.thesis : null;
  const th = kept || FLOW.thesis({ symbol: d.symbol, direction: now.state.includes("DOWN") ? "SHORT" : "LONG" });
  const dir = segmented(`thdir${++uid}`, [["LONG", "Long (shares or calls)"], ["SHORT", "Short (shares or puts)"]], th.direction);
  const box = (v, placeholder) => numInput({ placeholder, value: v === null ? "" : String(v) });
  const entry = box(th.entry, "now"), inv = box(th.invalidation, "suggest"), t1 = box(th.target1, "suggest"), t2 = box(th.target2, "suggest");
  const horizon = el("input", { type: "number", min: 1, max: 260, step: 1, value: th.horizonDays });
  const qty = el("input", { type: "number", min: 1, max: 500, step: 1, value: th.contracts });
  const budget = box(th.budget, "none");
  budget.step = "1";
  const go = el("button", { class: "primary", text: "Grade and find a contract" });
  const err = el("div", { class: "error", role: "alert" });
  const out = el("div");
  const read = () => FLOW.thesis({ symbol: d.symbol, direction: dir.value(), entry: optNum(entry), invalidation: optNum(inv),
    target1: optNum(t1), target2: optNum(t2), horizonDays: horizon.value, contracts: qty.value, budget: optNum(budget) });
  const remember = () => { state.thesis = read(); };
  for (const input of [entry, inv, t1, t2, horizon, qty, budget]) input.addEventListener("change", remember);
  dir.addEventListener("change", () => {
    // Suggested levels belong to one direction; typed ones are kept.
    for (const input of [inv, t1, t2]) input.placeholder = "suggest";
    remember();
  });
  const showSuggested = lv => {
    for (const [input, v] of [[inv, lv.invalidation], [t1, lv.target_1], [t2, lv.target_2]]) {
      input.placeholder = input.value === "" && isNum(v) ? `${num(v, 2)} suggested` : "suggest";
    }
  };
  go.addEventListener("click", () => withBusy(go, err, async () => {
    const t = read();
    state.thesis = t;
    out.classList.add("refetching");
    try {
      // The grade settles the levels (yours, or suggested from the chart); the picker then uses the same ones.
      const grade = await api("/v2/position/evaluate", FLOW.gradeRequest(t));
      showSuggested(grade.levels);
      const card = el("div", {}, gradeCard(grade));
      const picks = el("div", {}, el("div", { class: "loadnote small muted" }, el("span", { class: "spinner" }), el("span", { text: "Valuing contracts across expirations…" })));
      out.replaceChildren(card, el("h4", { text: `${t.direction === "LONG" ? "Calls" : "Puts"} for this thesis` }), picks);
      try {
        const opts = await api("/v2/options/evaluate", FLOW.optionsRequest(t, grade.levels));
        // The picker was handed the grade's levels; say where they came from, as the grade does.
        if (opts.levels_source) Object.assign(opts.levels_source, { target_1: grade.levels.targets_source, invalidation: grade.levels.invalidation_source });
        picks.replaceChildren(renderOptionsEval(opts));
        state.decision = { thesis: t, grade, options: opts };
        if (typeof DECIDE_AFTER !== "undefined") for (const hook of DECIDE_AFTER) { try { hook(out, state.decision); } catch (e) { console.error(e); } }
      } catch (e) { picks.replaceChildren(el("p", { class: "small error", text: `Contracts: ${e.message}` })); }
    } finally { out.classList.remove("refetching"); }
  }));
  const panel = el("div", { class: "panel thesis" }, el("h3", { text: "Your thesis" }),
    el("p", { class: "small muted", text: "Which way, where it is wrong, where it should get to, and by when. Leave a price blank and it is suggested from the chart. One button grades the stock trade and finds calls or puts for the same levels." }),
    el("div", { class: "inline-form" }, field("Direction", dir), field("Entry", entry), field("Invalidation", inv), field("Target 1", t1), field("Target 2", t2),
      field("Holding days", horizon), field("Contracts", qty), field("Option budget ($)", budget), go), err, out);
  // Arriving from Today or the scanner with a direction set: run it straight away and bring it into view.
  const hint = state.decideHint;
  state.decideHint = null;
  if (kept && hint) {
    setTimeout(() => { go.click(); panel.scrollIntoView({ behavior: "smooth", block: "start" }); }, 0);
  }
  return panel;
}
ANALYSIS_SECTIONS.push(thesisSection);

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
// 2.5: the thesis form above replaces this form on the page; renderOptionsEval is shared.

// Opens Price an option on this contract, already priced, on the limit ladder (2.5).
function limitButton(symbol, k, contracts) {
  const b = el("button", { class: "primary small-btn", text: "Set the limit" });
  b.addEventListener("click", () => state.openPricing(FLOW.pricingRequest(symbol, k, contracts, "buy")));
  return b;
}

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
    const limit = limitButton(r.symbol, k, th.contracts);
    cards.append(el("div", { class: "pick" }, el("div", { class: "pick-cat", text: pk.category }), el("div", { class: "pick-name", text: contractName(k) }),
      el("div", { class: "small", text: `${money(k.mid)} mid · ${money(k.premium, 0)} for ${th.contracts} · delta ${num(k.delta, 2)}` }),
      el("div", { class: "small", text: `At target: ${signedMoney(k.at_target)} · if invalidated: ${signedMoney(k.if_invalidated)}` }),
      el("p", { class: "small muted", text: pk.why }), k.flags.length ? el("ul", { class: "plain small" }, k.flags.map(f => el("li", {}, statusTag("warn", "Caution"), ` ${f}`))) : null,
      el("div", { class: "row" }, limit, sim, track), note));
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

// A figure that could not be measured is shown as unknown, never as $0.
const unknownText = (title = "") => el("span", { class: "muted", title, text: "unknown" });
const knownMoney = (v, d = 0, title = "") => isNum(v) ? money(v, d) : unknownText(title);
const knownSigned = (v, d = 0, title = "") => isNum(v) ? signedMoney(v, d) : unknownText(title);
// "of 4" after a total that leaves positions out, so a partial sum never reads as the whole.
const partialNote = (unknown, total) => unknown ? ` (${unknown} of ${total} not measured)` : "";
// Keyed by the combined status: an OK that could not cover every position is INCOMPLETE, never green.
const LOSS_TAG = { OK: ["pass", "Within the stop"], INCOMPLETE: ["warn", "Not known if within the stop"], WARN: ["warn", "Near the stop"], STOP: ["fail", "Stop reached"] };
const COVERAGE_TAG = { COMPLETE: ["pass", "Complete"], PARTIAL: ["warn", "Partial"], UNAVAILABLE: ["fail", "Not measured"] };

function dailyLossPanel(dl) {
  const loss = dl.loss_status || dl.status, cov = dl.coverage_status || "COMPLETE";
  const parts = [el("h3", {}, "Today against the daily stop ", tagOf(LOSS_TAG, dl.status), " ", tagOf(COVERAGE_TAG, cov)),
    el("div", { class: "metrics" },
      metric(signedMoney(dl.known_total ?? dl.counted), `${cov === "COMPLETE" ? "Today" : "Measured so far today"}, ${dl.mode === "REALIZED_PLUS_UNREALIZED" ? "closed and open" : "closed trades only"}`),
      metric(signedMoney(dl.realized_today), "Realized today"),
      metric(dl.unrealized_complete === false ? el("span", {}, signedMoney(dl.unrealized_change_today), el("span", { class: "small muted", text: " + unknown" })) : signedMoney(dl.unrealized_change_today), "Change in open positions"),
      metric(money(dl.stop, 0), `Daily stop (${dl.stop_pct}% of the account)`),
      isNum(dl.positions_evaluated_pct) ? metric(`${dl.positions_evaluated} of ${dl.positions}`, "Positions measured") : null,
      isNum(dl.capital_evaluated_pct) && cov !== "COMPLETE" ? metric(pct(dl.capital_evaluated_pct, 0), "Of capital at cost measured") : null),
    banner(loss === "STOP" ? "fail" : loss === "WARN" || cov !== "COMPLETE" ? "warn" : "pass", dl.text)];
  if (dl.missing && dl.missing.length) parts.push(el("ul", { class: "small" }, dl.missing.map(m => el("li", { text: `${m.description}: ${m.reason}` }))));
  parts.push(el("p", { class: "small muted", text: `${dl.note} Set DAILY_LOSS_MODE in .env to change this.` }));
  return el("div", { class: "panel" }, ...parts);
}

function renderPortfolio(r) {
  const frag = el("div"), t = r.totals, dl = r.daily_loss, u = t.unknown_counts || {}, n = r.count;
  if (r.data_source === "demo") frag.append(banner("warn", "Demo data: the prices are made up."));
  if (r.count) frag.append(portfolioStrip(r));
  if (r.warnings.length) frag.append(groupedWarnings(r.warnings));
  if (!r.count) { frag.append(el("div", { class: "panel" }, el("p", { class: "muted", text: "No open positions. Add them on the Positions screen (option-journal trades appear there automatically)." }))); }
  const acct = r.account_size, share = v => acct ? ` (${pct(100 * v / acct, 0)} of the account)` : "";
  const cov = r.coverage || { status: "COMPLETE" };
  const defs = r.definitions || {};
  const def = (key, label) => el("span", { title: defs[key] || "" }, label);
  frag.append(el("div", { class: "panel" }, el("h3", {}, "Exposure ", cov.status !== "COMPLETE" ? tagOf(COVERAGE_TAG, cov.status) : ""),
    el("p", { class: "small muted", text: "In delta-equivalent stock dollars. Gross adds longs and shorts; net lets them offset, so a hedged book shows both how big it is and which way it leans." }),
    el("div", { class: "metrics" },
      metric(knownMoney(t.gross_exposure, 0), `Gross${isNum(t.gross_exposure) ? share(t.gross_exposure) : ""}${partialNote(u.exposure, n)}`),
      metric(knownSigned(t.net_exposure), `Net${isNum(t.net_exposure) ? share(Math.abs(t.net_exposure)) : ""}${partialNote(u.exposure, n)}`),
      metric(isNum(t.long_exposure) ? `${signedMoney(t.long_exposure)} / ${signedMoney(t.short_exposure)}` : unknownText(), "Long / short"),
      metric(knownSigned(t.spy_equivalent), `SPY-equivalent (beta-weighted)${partialNote(u.exposure, n)}`),
      metric(knownSigned(t.theta_per_day), `Time decay per day (options)${partialNote(u.theta, n)}`),
      metric(knownSigned(t.vega_per_point), `Per point of implied volatility${partialNote(u.vega, n)}`)),
    el("h4", { text: "Value" }),
    el("div", { class: "metrics" },
      metric(knownSigned(t.market_value), `Market value at the mark${partialNote(u.market_value, n)}`),
      metric(knownSigned(t.estimated_liquidation_value), `Closing now, at the bid or ask (estimate)${partialNote(u.liquidation, n)}`),
      isNum(t.option_premium_at_risk) && t.option_premium_at_risk > 0 ? metric(money(t.option_premium_at_risk, 0), "Option premium at risk") : null),
    cov.status !== "COMPLETE" ? el("p", { class: "small muted", text: "Totals add up the positions that could be measured; see the note at the top of the page." }) : null));
  if (r.risk_coverage) frag.append(riskPanel(r.risk_coverage));
  if (r.bad_day) frag.append(badDayPanel(r.bad_day));
  if (dl) frag.append(dailyLossPanel(dl));

  if (r.positions.length) frag.append(el("div", { class: "panel" }, el("h3", { text: "By position" }), table([
    { label: "Position", cell: x => el("span", {}, x.description, x.priced === false ? el("span", { class: "small error", text: " · no current price" }) : null,
      ["STALE", "UNKNOWN"].includes(x.quote_freshness) ? el("span", { class: "small error", text: x.quote_freshness === "STALE" ? " · stale price" : " · price time unknown" }) : null) },
    { label: "Sector", cell: x => x.sector || "" },
    { label: "Exposure", num: true, cell: x => knownSigned(x.dollar_delta, 0, (x.missing || []).join("; ")) },
    { label: "Beta", num: true, cell: x => isNum(x.beta) ? el("span", { title: `${x.beta_samples} days` }, num(x.beta, 2)) : el("span", { class: "muted", title: "Not enough overlapping history: counted as 1" }, "n/a") },
    { label: "SPY-equivalent", num: true, cell: x => knownSigned(x.beta_dollars, 0, (x.missing || []).join("; ")) },
    { label: "Open risk", num: true, cell: x => knownMoney(x.open_risk, 0, x.risk_reason || (x.missing || []).join("; ")) },
    { label: "Measured by", cell: x => el("span", { class: "small", title: (r.risk_coverage && r.risk_coverage.basis_words[x.risk_basis]) || "" }, RISK_BASIS_WORDS[x.risk_basis] || x.risk_basis) },
    { label: "Theta / day", num: true, cell: x => x.instrument_type === "OPTION" ? knownSigned(x.theta) : "" },
    { label: "Plan", cell: x => tagOf(THESIS, x.thesis_status) }], r.positions),
    el("p", { class: "small muted", text: "Exposure is in stock dollars: shares times price, or delta times the contract multiplier times contracts times the stock price for options. Beta to SPY from six months of daily moves on the days both traded; with fewer than 40 such days there is no beta and the position counts one for one." })));

  for (const [key, title, rows] of [["sector", "By sector", r.by_sector], ["symbol", "By stock", r.by_symbol]]) {
    if (!rows || !rows.length) continue;
    const max = Math.max(...rows.map(x => x.gross), 1);
    frag.append(el("div", { class: "panel" }, el("h3", { text: title }), el("div", { class: "bars" }, rows.map(x => el("div", { class: "bar-row" },
      el("span", { text: x[key] }), el("div", { class: "score-track", title: `Gross ${money(x.gross, 0)}: long ${money(x.long, 0)}, short ${money(-x.short, 0)}` },
        el("i", { class: x.net >= 0 ? "pos" : "neg", style: `width:${100 * x.gross / max}%` })),
      el("strong", { text: Math.abs(x.net) < x.gross - 0.5 ? `${signedMoney(x.net)} net · ${money(x.gross, 0)} gross` : signedMoney(x.net) })))),
      el("p", { class: "small muted", text: "Bar length is gross exposure; its colour is the direction of the net." })));
  }
  if (r.correlation) {
    const c = r.correlation;
    const cell = v => isNum(v) ? el("td", { class: "num cell", style: `background: color-mix(in oklab, var(${v >= 0 ? "--div-pos" : "--div-neg"}) ${Math.round(70 * Math.abs(v))}%, var(--div-mid))`, text: num(v, 2) })
      : el("td", { class: "num cell muted", title: "Fewer than 40 days both traded", text: "n/a" });
    frag.append(el("div", { class: "panel" }, el("h3", { text: "How the holdings move together" }),
      el("div", { class: "scroll" }, el("table", { class: "pnl-grid" }, el("thead", {}, el("tr", {}, el("th", { text: "" }), c.symbols.map(s2 => el("th", { class: "num", text: s2 })))),
        el("tbody", {}, c.symbols.map((a2, i) => el("tr", {}, el("td", { text: a2 }), c.matrix[i].map(cell)))))),
      el("p", { class: "small muted", text: "Correlation of daily returns over the last six months: 1 moves exactly together, 0 unrelated, -1 opposite." })));
  }
  frag.append(stressPanel(r));
  for (const n of r.notes || []) frag.append(banner("info", n));
  return frag;
}

// The page's headline figures in one row; anything not measured says so instead of adding in as $0.
function portfolioStrip(r) {
  const t = r.totals, dl = r.daily_loss, n = r.count, u = t.unknown_counts || {};
  const stale = r.positions.filter(x => ["STALE", "UNKNOWN"].includes(x.quote_freshness)).length;
  const unpriced = r.positions.filter(x => x.priced === false).length;
  const riskUnknown = t.open_risk_unknown || 0;
  return el("div", { class: "panel" }, el("div", { class: "metrics" },
    metric(String(n), "Open positions"),
    metric(knownMoney(t.gross_exposure, 0), isNum(t.net_exposure) ? `Gross exposure, net ${signedMoney(t.net_exposure)}${partialNote(u.exposure, n)}` : "Exposure: no position could be measured"),
    metric(riskUnknown ? el("span", {}, money(t.open_risk, 0), el("span", { class: "small muted", text: " + unknown" })) : money(t.open_risk, 0),
      riskUnknown ? `Open risk (${riskUnknown} of ${n} not measured)` : `Open risk${isNum(t.open_risk_limit) ? ` of ${money(t.open_risk_limit, 0)} allowed` : ""}`),
    dl ? metric(el("span", {}, signedMoney(dl.known_total ?? dl.counted), dl.status === "OK" ? "" : el("span", { class: "small muted", text: dl.status === "INCOMPLETE" ? " so far" : "" })),
      `Today: ${(LOSS_TAG[dl.status] || ["", dl.status])[1].toLowerCase()} (${money(dl.stop, 0)})`) : null,
    metric(unpriced || stale ? statusTag("warn", `${n - unpriced - stale} of ${n} current`) : statusTag("pass", "All current"),
      unpriced || stale ? `Prices: ${[unpriced ? `${unpriced} missing` : "", stale ? `${stale} stale or undated` : ""].filter(Boolean).join(", ")}` : "Prices")));
}

// Every warning on the page, most serious first, so nothing that matters sits below the fold.
const WARNING_GROUPS = [["fail", "Critical"], ["warn", "Warning"], ["info", "For information"]];
function groupedWarnings(list) {
  const box = el("div", { class: "panel" }, el("h3", { text: "Warnings" }));
  for (const [level, title] of WARNING_GROUPS) {
    const items = list.filter(w => (w.level === "fail" ? "fail" : w.level === "warn" ? "warn" : "info") === level);
    if (!items.length) continue;
    const body = el("div", {}, items.map(w => banner(level, w.text)));
    box.append(level === "info" && items.length > 2
      ? el("details", {}, el("summary", {}, statusTag(level, `${title} (${items.length})`)), body)
      : el("div", {}, el("h4", {}, statusTag(level, `${title} (${items.length})`)), body));
  }
  return box;
}

const RISK_BASIS_WORDS = { INVALIDATION: "to invalidation", PREMIUM: "premium", CONFIGURED: "your dollar risk", BEYOND_INVALIDATION: "past invalidation", UNKNOWN: "unknown" };
function riskPanel(rc) {
  const level = rc.within_limit === false ? "fail" : rc.within_limit === null ? "warn" : "pass";
  return el("div", { class: "panel" }, el("h3", {}, "Open risk ", tagOf(COVERAGE_TAG, rc.status)),
    banner(level, rc.conclusion),
    el("div", { class: "metrics" },
      metric(money(rc.bases.INVALIDATION, 0), "To invalidation prices"),
      metric(money(rc.bases.PREMIUM, 0), "Option premium (no invalidation, or past it)"),
      metric(money(rc.bases.CONFIGURED, 0), "Dollar risk you entered (your assumption)"),
      metric(`${rc.measured} of ${rc.positions}`, "Positions measured")),
    rc.unknown.length ? el("ul", { class: "small" }, rc.unknown.map(u => el("li", { text: `${u.description}: ${u.reason}` }))) : null);
}

function badDayPanel(b) {
  if (!b.available) return el("div", { class: "panel" }, el("h3", { text: "A bad day" }), el("p", { class: "small muted", text: `Not estimated: ${b.reason}.` }));
  return el("div", { class: "panel" }, el("h3", { text: "A bad day" }), el("div", { class: "metrics" },
    metric(money(b.parametric_95, 0), "1 day in 20, normal approximation"),
    metric(money(b.historical_95, 0), "1 day in 20, from the past days themselves"),
    metric(isNum(b.expected_shortfall_95) ? money(b.expected_shortfall_95, 0) : "n/a", "Average of the days worse than that"),
    metric(money(b.worst_day, 0), "Worst day in the sample")),
    (b.left_out || []).length ? banner("warn", `Leaves out ${b.left_out.map(x => `${x.symbol} (${signedMoney(x.exposure)} exposure)`).join(", ")}: no usable price history, so these figures understate a bad day.`) : null,
    el("p", { class: "small muted", text: b.note }));
}

function stressPanel(r) {
  const box = el("div", { class: "panel" }, el("h3", { text: "Stress tests" }));
  const rows = el("div");
  const show = list => rows.replaceChildren(table([
    { label: "Scenario", cell: x => x.name },
    { label: "Profit or loss", num: true, cell: x => x.complete === false ? el("span", { title: `Not repriced: ${x.excluded.join("; ")}` }, signedMoney(x.pnl), el("span", { class: "small muted", text: ` (${x.excluded.length} left out)` })) : signedMoney(x.pnl) },
    { label: "Of the account", num: true, cell: x => isNum(x.share_of_account) ? signedPct(100 * x.share_of_account, 1) : "n/a" },
    { label: "Hit hardest", cell: x => x.worst && x.worst.pnl < 0 ? `${x.worst.description} (${signedMoney(x.worst.pnl)})${x.worst.clamped ? ", to zero" : ""}` : "" }], list));
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

const CAL_TAG = { CALIBRATED: ["pass", "Calibrated, confirmed on held-out dates"], CALIBRATED_IN_SAMPLE_ONLY: ["warn", "Not confirmed on held-out dates"],
  NOT_CALIBRATED: ["warn", "Not calibrated"], INSUFFICIENT_DATA: ["info", "Too little history"] };
// Histories built before 2.3 have no status: fall back to the yes/no they recorded.
const calTag = (status, calibrated) => status ? tagOf(CAL_TAG, status) : (calibrated ? statusTag("pass", "Calibrated on this data") : statusTag("warn", "Not calibrated"));

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
      { label: "Per trade, without it", num: true, cell: b => b.without.trades ? `${signedMoney(b.without.expectancy)} (${rText(b.without.expectancy_r)})` : "n/a" },
      { label: "Not checked", num: true, cell: b => isNum(b.not_checked) && b.not_checked ? el("span", { title: "No price history, entry context or risk to check these trades against: left out of both columns" }, num(b.not_checked, 0)) : "" }], r.behaviour),
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
    if (a.review && p.status !== "PLANNED" && p.status !== "CANCELLED") box.append(reviewBlock(a.review, p));
    if (a.compare.length && a.compared_with) box.append(el("h4", { text: `At entry and at the ${a.compared_with.toLowerCase()} snapshot` }), table([
      { label: "", cell: c => FIELD_WORDS[c.field] || c.field.replaceAll("_", " ") },
      { label: "Entry", cell: c => fieldValue(c.field, c.entry) },
      { label: a.compared_with[0] + a.compared_with.slice(1).toLowerCase(), cell: c => fieldValue(c.field, c.latest) }], a.compare));
  }).catch(e => box.replaceChildren(el("p", { class: "small error", text: e.message })));
  return box;
}

// A trade explained from the records: plan, fills, what was known at entry, what happened, the result, what is missing.
const yesNo = (v, yes, no) => v === true ? statusTag("warn", yes) : v === false ? statusTag("pass", no) : statusTag("info", "Not known");
function reviewBlock(rv, p) {
  const pl = rv.planned, ex = rv.executed, at = rv.at_entry, res = rv.result, closed = p.status === "CLOSED";
  const habits = Object.values(rv.habits || {});
  const box = el("div", {}, el("h4", { text: closed ? "Trade review" : "Trade review so far" }));
  box.append(el("dl", { class: "pairs small" },
    el("dt", { text: "Planned" }), el("dd", { text: [pl.thesis || "no thesis written", pl.setup ? `${pl.setup} setup` : "", isNum(pl.invalidation_price) ? `wrong beyond ${money(pl.invalidation_price)}` : "no invalidation", isNum(pl.target_1) ? `target ${money(pl.target_1)}${isNum(pl.target_2) ? ` then ${money(pl.target_2)}` : ""}` : "no target", isNum(pl.initial_risk) ? `risking ${money(pl.initial_risk, 0)}` : ""].filter(Boolean).join(" · ") }),
    el("dt", { text: "Done" }), el("dd", { text: `${ex.fills} fill${ex.fills === 1 ? "" : "s"}, in at ${money(ex.average_entry_price)}${isNum(ex.average_exit_price) ? `, out at ${money(ex.average_exit_price)}` : ""}${isNum(ex.days_held) ? ` over ${ex.days_held} day${ex.days_held === 1 ? "" : "s"}` : ""}${ex.corrections ? `; ${ex.corrections} fill correction${ex.corrections === 1 ? "" : "s"} recorded` : ""}` }),
    el("dt", { text: "Known at entry" }), el("dd", { text: at.available ? [at.trend_state ? `trend ${at.trend_state.replaceAll("_", " ").toLowerCase()}` : "", at.market_regime ? `${at.market_regime.replaceAll("_", " ").toLowerCase()} market` : "", isNum(at.rsi_14) ? `RSI ${num(at.rsi_14, 0)}` : "", at.method === "PRIOR_BAR" ? "(from the prior day's close)" : at.method === "LIVE" ? "(taken at the time)" : ""].filter(Boolean).join(", ") || "a snapshot with no readings" : `not available: ${at.reason}` }),
    rv.signals_at_entry.length ? el("dt", { text: "Signals at entry" }) : null, rv.signals_at_entry.length ? el("dd", { text: rv.signals_at_entry.join(", ") }) : null,
    el("dt", { text: "Thesis" }), el("dd", {}, !isNum(pl.invalidation_price) ? statusTag("info", "No invalidation set") : yesNo(rv.thesis_invalidated, "Invalidated", "Not invalidated")),
    el("dt", { text: "Targets" }), el("dd", {}, !isNum(pl.target_1) ? statusTag("info", "No target set") : yesNo(rv.target_1_reached === null ? null : !rv.target_1_reached, "Target 1 not reached", "Target 1 reached"),
      rv.target_2_reached === null ? "" : " ", rv.target_2_reached === null ? "" : yesNo(!rv.target_2_reached, "Target 2 not reached", "Target 2 reached")),
    el("dt", { text: "Result" }), el("dd", { text: `${isNum(res.net) ? signedMoney(res.net) : "unknown"} net${res.financing ? ` (trading ${signedMoney(res.trading)}, financing ${signedMoney(res.financing)})` : ""}, after ${money(res.fees)} in fees${isNum(res.r_multiple) ? ` · ${rText(res.r_multiple)}` : ""}${!closed && isNum(res.unrealized) ? ` · includes ${signedMoney(res.unrealized)} not yet realized` : ""}` }),
    el("dt", { text: "Habits" }), el("dd", { text: `${habits.filter(v => v === true).length} of ${habits.filter(v => v !== null).length} checked happened; ${habits.filter(v => v === null).length} could not be checked` })));
  if (rv.what_happened.length) box.append(el("ul", { class: "small" }, rv.what_happened.map(t => el("li", { text: t }))));
  if (rv.missing.length) box.append(el("div", { class: "small" }, el("strong", { text: "Missing: " }), rv.missing.join(" ")));
  box.append(el("p", { class: "small muted", text: rv.note }));
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
  const line = runLine(r.run);
  if (line) frag.append(line);
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
  const wfLine = runLine(w.run);
  if (wfLine) frag.append(wfLine);
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

/* ---------- alerts (alerts only) ---------- */
function alertsPanel() {
  const box = el("div", { class: "panel" }, el("h3", { text: "Alerts" }), el("p", { class: "small muted", text: "Checking…" }));
  const check = el("button", { class: "plain small-btn", text: "Check again" });
  const load = () => api("/v2/alerts/check", {}).then(a => {
    box.replaceChildren(el("div", { class: "panel-head" }, el("h3", { text: `Alerts (${a.alerts.length})` }), check));
    if (!a.alerts.length) box.append(el("p", { class: "small muted", text: a.open_positions ? "Nothing needs a look right now." : "No open positions." }));
    else box.append(el("ul", { class: "plain small" }, a.alerts.map(x => el("li", {}, statusTag(x.level, { fail: "Act", warn: "Look", info: "Note" }[x.level]),
      x.status === "PROVISIONAL" ? el("span", { class: "muted", text: " (provisional)" }) : null, ` ${x.text}`))));
    box.append(el("p", { class: "small muted", text: `${a.note} Checked ${a.checked_at.slice(11, 16)} New York time.` }));
    updateAlertBadge(a.alerts.filter(x => x.level !== "info").length);
  }).catch(e => box.replaceChildren(el("h3", { text: "Alerts" }), el("p", { class: "small error", text: e.message })));
  check.addEventListener("click", () => withBusy(check, null, load));
  box.reload = load;
  load();
  return box;
}
function updateAlertBadge(n) {
  for (const btn of document.querySelectorAll('.rail button.nav[data-view="positions"]')) {
    let b2 = btn.querySelector(".badge");
    if (!b2) { b2 = el("span", { class: "badge" }); btn.append(b2); }
    b2.textContent = n ? String(n) : "";
    b2.classList.toggle("hidden", !n);
    btn.setAttribute("aria-label", n ? `Positions, ${n} alert${n === 1 ? "" : "s"}` : "Positions");
  }
}
{
  const classicPositions = buildPositions;
  BUILDERS.positions = () => {
    const view = classicPositions();
    const panel = alertsPanel();
    view.insertBefore(panel, view.children[3] || null);
    const shown = view.onShow;
    view.onShow = () => { shown(); panel.reload(); };
    return view;
  };
  // A quiet check every ten minutes keeps the badge current while the page is open.
  setInterval(() => { if (featureOn("unified_positions")) api("/v2/alerts").then(a => updateAlertBadge(a.alerts.filter(x => x.level !== "info").length)).catch(() => {}); }, 600000);
}

/* ---------- trend reversals: potential bottoms and tops, and options to trade them ---------- */
const REV_LEVEL = { CONFIRMED: "pass", CANDIDATE: "warn", WATCH: "info", FAILED: "fail", NONE: "info" };
const revWord = (side, stage) => ({
  CONFIRMED: side === "bottom" ? "Reversal up confirmed" : "Reversal down confirmed", CANDIDATE: `Potential ${side}`,
  WATCH: "Stretched, no sign yet", FAILED: `Failed ${side}`, NONE: `No ${side} in sight` })[stage] || stage;
const revTag = (side, stage) => statusTag(REV_LEVEL[stage] || "info", revWord(side, stage));
const REV_STRIP = { NONE: "var(--sunk)", FAILED: "color-mix(in oklab, var(--ink) 28%, var(--div-mid))" };
const revFill = (side, stage) => REV_STRIP[stage] ||
  `color-mix(in oklab, var(${side === "bottom" ? "--div-pos" : "--div-neg"}) ${({ WATCH: 18, CANDIDATE: 45, CONFIRMED: 80 })[stage]}%, var(--div-mid))`;
const monthDay = iso => iso ? new Date(iso + "T12:00:00").toLocaleDateString("en-US", { month: "short", day: "numeric" }) : "";
const VERDICT_TAG = { GOOD: "pass", FAIR: "warn", POOR: "fail", AVOID: "fail" };
const verdictTag = v => statusTag(VERDICT_TAG[v] || "info", v === "GOOD" ? "Good" : v === "FAIR" ? "Fair" : v === "POOR" ? "Poor" : "Avoid");

function reversalSection(d) {
  if (!featureOn("reversal_analysis")) return null;
  const head = () => el("h3", { text: "Trend reversal: a potential bottom or top?" });
  const box = el("div", { class: "panel" }, head(), el("div", { class: "loadnote small muted" }, el("span", { class: "spinner" }), el("span", { text: "Looking for exhaustion and confirmation…" })));
  const load = () => api(`/v2/reversal/${encodeURIComponent(d.symbol)}`).then(r => box.replaceChildren(head(), renderReversal(r, load)))
    .catch(e => box.replaceChildren(head(), el("p", { class: "small error", text: e.message })));
  load();
  return box;
}

function renderReversal(r, reload) {
  const frag = el("div"), day = r.confirmed, prov = r.provisional;
  for (const n of r.notes || []) frag.append(banner(n.startsWith("Demo") ? "warn" : "info", n));
  frag.append(el("p", {}, el("strong", { text: day.headline })));
  if (prov) {
    const moved = ["bottom", "top"].filter(s => prov[s].stage !== day[s].stage);
    frag.append(el("p", { class: "small muted" }, moved.length
      ? ["Read on the last finished bar (", shortDate(day.date), "). Today's bar so far would make it ", ...moved.flatMap((s, i) => [i ? " and " : "", revTag(s, prov[s].stage)]), "; that is provisional until the close."]
      : `Read on the last finished bar (${shortDate(day.date)}); today's bar so far does not change it.`));
  }
  const sides = el("div", { class: "rev-sides" });
  for (const s of ["bottom", "top"]) {
    const v = day[s], lv = v.levels, conf = s === "bottom" ? lv.confirms_above : lv.confirms_below;
    sides.append(el("div", { class: `rev-side${day.leading === s ? " lead" : ""}` },
      el("h4", { text: s === "bottom" ? "Bottom: the end of a fall?" : "Top: the end of a rise?" }),
      el("div", { class: "trend-state" }, revTag(s, v.stage), el("span", { class: "small muted", text: `since ${shortDate(v.since)}` })),
      el("div", { class: "scores", style: "margin-top:8px" },
        el("div", { class: "score-row" }, el("span", { class: "score-k", text: "Exhaustion" }), el("div", { class: "score-track" }, el("i", { class: s === "bottom" ? "pos" : "neg", style: `width:${Math.min(100, 100 * v.exhaustion / 60)}%` })), el("strong", { class: "score-v", text: num(v.exhaustion, 0) })),
        el("div", { class: "score-row" }, el("span", { class: "score-k", text: "Confirmation" }), el("div", { class: "score-track" }, el("i", { class: s === "bottom" ? "pos" : "neg", style: `width:${Math.min(100, 100 * v.confirmation / 40)}%` })), el("strong", { class: "score-v", text: num(v.confirmation, 0) }))),
      el("p", { class: "small muted", text: `${num(v.score, 0)} of 100 evidence points: exhaustion out of 60, confirmation out of 40.` }),
      el("p", { class: "small", text: v.text }),
      STAGE_ACTIVE(v.stage) ? el("div", { class: "metrics" },
        metric(money(lv.invalidation), `Wrong on a close ${s === "bottom" ? "below" : "above"}`),
        metric(money(lv.target_1), `Target 1: ${lv.target_1_is}`), metric(money(lv.target_2), `Target 2: ${lv.target_2_is}`),
        conf && v.stage !== "CONFIRMED" ? metric(money(conf), `Confirms on a close ${s === "bottom" ? "above" : "below"}`) : null) : null));
  }
  frag.append(sides, reversalChart(r));
  const ev = el("div");
  for (const s of ["bottom", "top"]) {
    const v = day[s];
    ev.append(el("details", { open: day.leading === s || (!day.leading && s === "bottom") ? true : null }, el("summary", { text: `The evidence for a ${s} on ${shortDate(day.date)}` }), table([
      { label: "Kind", cell: e => e.kind === "exhaustion" ? "Exhaustion" : "Confirmation" },
      { label: "Evidence", cell: e => e.name },
      { label: "Seen", cell: e => e.met ? statusTag("pass", "Yes") : el("span", { class: "muted", text: "No" }) },
      { label: "Points", num: true, cell: e => `${num(e.points, 0)} / ${e.weight}` },
      { label: "What it means", cell: e => e.text }], v.evidence)));
  }
  frag.append(ev);
  if (r.past) frag.append(reversalPast(r, reload));
  frag.append(reversalOptionsPanel(r));
  frag.append(el("p", { class: "small muted", text: `${r.note} ${r.version}.` }));
  return frag;
}
const STAGE_ACTIVE = stage => stage === "CANDIDATE" || stage === "CONFIRMED" || stage === "WATCH";

function reversalChart(r) {
  const rows = r.daily, n = rows.length;
  const wrap = el("div", {}, el("h4", { text: "Stages over the last year" }));
  if (n < 2) return wrap;
  const W = 760, H = 270, pad = { l: 58, r: 16, t: 10, b: 74 }, L = pad.l, R = W - pad.r;
  const stripTop = H - pad.b + 8, stripH = 11, gap = 4;
  const X = i => L + (R - L) * i / (n - 1);
  const ys = niceScale(Math.min(...rows.map(x => x.close)), Math.max(...rows.map(x => x.close)), 5);
  const Y = v => pad.t + (H - pad.b - pad.t) * (1 - (v - ys.lo) / (ys.hi - ys.lo));
  const root = svg("svg", { viewBox: `0 0 ${W} ${H}`, role: "img", "aria-label": `${r.symbol} closing price with its bottom and top stages on each day` });
  for (const v of ys.ticks) root.append(svg("line", { class: "grid", x1: L, x2: R, y1: Y(v), y2: Y(v) }),
    svg("text", { x: L - 8, y: Y(v) + 4, "text-anchor": "end", text: money(v, ys.step < 1 ? 2 : 0) }));
  ["bottom", "top"].forEach((side, k) => {
    const y0 = stripTop + k * (stripH + gap);
    root.append(svg("text", { x: L - 8, y: y0 + stripH - 1, "text-anchor": "end", class: "small-label", text: side === "bottom" ? "Bottom" : "Top" }));
    let start = 0;
    for (let i = 1; i <= n; i++) {
      if (i === n || rows[i][side] !== rows[start][side]) {
        const x0 = start === 0 ? L : (X(start - 1) + X(start)) / 2, x1 = i === n ? R : (X(i - 1) + X(i)) / 2;
        root.append(svg("rect", { x: x0, y: y0, width: Math.max(0.5, x1 - x0), height: stripH, fill: revFill(side, rows[start][side]) }));
        start = i;
      }
    }
  });
  const monthsY = stripTop + 2 * (stripH + gap) + 12;
  rows.forEach((x, i) => { if (i > 0 && x.date.slice(5, 7) !== rows[i - 1].date.slice(5, 7) && X(i) < R - 24) root.append(svg("text", { x: X(i), y: monthsY, "text-anchor": "middle", text: monthLabel(x.date) })); });
  root.append(svg("path", { d: rows.map((x, i) => `${i ? "L" : "M"}${X(i).toFixed(1)} ${Y(x.close).toFixed(1)}`).join(" "), fill: "none", stroke: "var(--ink)", "stroke-width": 1.6 }));
  // A marker on the day each potential bottom or top appeared, and each confirmation.
  const active = s => s === "CANDIDATE" || s === "CONFIRMED";
  rows.forEach((x, i) => {
    if (!i) return;
    for (const side of ["bottom", "top"]) {
      const now = x[side], was = rows[i - 1][side];
      if (!(active(now) && (now !== was) && !(was === "CONFIRMED"))) continue;
      const cx = X(i), cy = Y(x.close) + (side === "bottom" ? 9 : -9), up = side === "bottom";
      const s2 = now === "CONFIRMED" ? 6 : 4.5;
      // Today's unfinished bar: hollow and dashed, never drawn as a confirmed event.
      root.append(svg("path", { d: up ? `M${cx} ${cy - s2} L${cx + s2} ${cy + s2} L${cx - s2} ${cy + s2} Z` : `M${cx} ${cy + s2} L${cx + s2} ${cy - s2} L${cx - s2} ${cy - s2} Z`,
        fill: now === "CONFIRMED" && !x.provisional ? `var(${up ? "--div-pos" : "--div-neg"})` : "var(--panel)", stroke: `var(${up ? "--div-pos" : "--div-neg"})`,
        "stroke-width": 1.5, "stroke-dasharray": x.provisional ? "2 1.5" : null }));
    }
  });
  const hover = svg("line", { class: "hover-line", y1: pad.t, y2: stripTop + 2 * stripH + gap, visibility: "hidden" });
  root.append(hover);
  const tip = el("div", { class: "tip" });
  root.addEventListener("pointermove", evt => {
    const box = root.getBoundingClientRect();
    const i = Math.max(0, Math.min(n - 1, Math.round(((evt.clientX - box.left) * W / box.width - L) / (R - L) * (n - 1))));
    hover.setAttribute("x1", X(i)); hover.setAttribute("x2", X(i)); hover.setAttribute("visibility", "visible");
    tip.replaceChildren(el("div", { class: "tip-head", text: shortDate(rows[i].date) + (rows[i].provisional ? " (so far, provisional)" : "") }), el("div", { text: money(rows[i].close) }),
      el("div", { class: "tip-sub", text: `Bottom: ${revWord("bottom", rows[i].bottom).toLowerCase()} (${num(rows[i].bottom_score, 0)} pts)` }),
      el("div", { class: "tip-sub", text: `Top: ${revWord("top", rows[i].top).toLowerCase()} (${num(rows[i].top_score, 0)} pts)` }));
    tooltipAt(tip, box, X(i), W);
  });
  root.addEventListener("pointerleave", () => { hover.setAttribute("visibility", "hidden"); tip.style.display = "none"; });
  const key = (fill, text) => el("span", {}, el("b", { class: "key-box", style: `background:${fill}` }), text);
  wrap.append(el("div", { class: "chart" }, root, tip), el("div", { class: "legend" },
    key(revFill("bottom", "CANDIDATE"), "Potential bottom"), key(revFill("bottom", "CONFIRMED"), "Reversal up confirmed"),
    key(revFill("top", "CANDIDATE"), "Potential top"), key(revFill("top", "CONFIRMED"), "Reversal down confirmed"),
    key(revFill("bottom", "WATCH"), "Stretched (watch)"), key(REV_STRIP.FAILED, "Failed"),
    el("span", { class: "small muted", text: "Triangles mark the day each one appeared; filled when confirmed; dashed on today's unfinished bar." })));
  return wrap;
}

function reversalPast(r, reload) {
  const p = r.past, box = el("div", {}, el("h4", { text: "How the same readings turned out before" }));
  if (!p.history_built) box.append(buildHistoryButton(reload));
  const cols = [{ label: "Reading", cell: x => el("span", {}, el("strong", { text: x.label }), el("span", { class: "small muted", text: x.direction > 0 ? " · expects a rise" : " · expects a fall" })) }];
  [5, 10, 20].forEach((h, i) => cols.push({ label: `${r.symbol}, ${h} days`, cell: x => freqText(x.this_stock[i]) }));
  cols.push({ label: "All stocks, 10 days", cell: x => x.all_stocks ? freqText(x.all_stocks[1]) : el("span", { class: "muted", text: "build the history" }) });
  box.append(table(cols, p.signals));
  box.append(el("p", { class: "small muted", text: `How often the stock moved the expected way after each new reading, entering at the next day's open, on ${r.symbol} over ${p.years} years${p.history_built ? ` and on the ${p.history_symbols} stocks in the signal history (${shortDate(p.history_run_at)})` : ""}, with the 95% range. Counts from the past, not forecasts.` }));
  const recent = p.signals.flatMap(x => x.recent.map(c => ({ ...c, label: x.label, direction: x.direction }))).sort((a, b) => b.date.localeCompare(a.date)).slice(0, 10);
  if (recent.length) box.append(el("details", {}, el("summary", { text: `The latest readings on ${r.symbol} and what followed` }), table([
    { label: "Date", cell: c => shortDate(c.date) }, { label: "Reading", cell: c => c.label }, { label: "Close", num: true, cell: c => money(c.close) },
    { label: "10 days later", num: true, cell: c => isNum(c.fwd_10) ? signedPct(100 * c.fwd_10, 1) : "not yet" },
    { label: "20 days later", num: true, cell: c => isNum(c.fwd_20) ? signedPct(100 * c.fwd_20, 1) : "not yet" }], recent)));
  return box;
}

function reversalOptionsPanel(r) {
  const day = r.confirmed;
  const pick = day.leading || (day.bottom.score >= day.top.score ? "bottom" : "top");
  const side = segmented(`rvside${++uid}`, [["auto", "Auto"], ["bottom", "Bottom: calls"], ["top", "Top: puts"]], "auto");
  const horizon = el("input", { type: "number", min: 1, max: 260, step: 1, value: 20 });
  const qty = el("input", { type: "number", min: 1, max: 500, step: 1, value: 1 });
  const budget = numInput({ placeholder: "none", step: "1" });
  const target = numInput({ placeholder: "reversal's" }), inv = numInput({ placeholder: "reversal's" });
  const myType = el("select", {}, el("option", { value: "", text: "none" }), el("option", { value: "call", text: "Call" }), el("option", { value: "put", text: "Put" }));
  const myExp = el("input", { type: "date" }), myStrike = numInput({ placeholder: "strike" });
  const go = el("button", { class: "primary", text: "Score options" });
  const err = el("div", { class: "error", role: "alert" });
  const out = el("div");
  go.addEventListener("click", () => withBusy(go, err, async () => {
    const body = { symbol: r.symbol, side: side.value(), horizon_days: Math.round(Number(horizon.value) || 20), contracts: Math.max(1, Math.round(Number(qty.value) || 1)) };
    for (const [k, input] of [["target_price", target], ["invalidation_price", inv], ["budget", budget]]) { const v = optNum(input); if (v !== null) body[k] = v; }
    if (myType.value || myExp.value || myStrike.value) {
      if (!(myType.value && myExp.value && optNum(myStrike) !== null)) throw new Error("To score your own contract, give its type, expiration and strike.");
      body.contracts_to_score = [{ option_type: myType.value, expiration: myExp.value, strike: optNum(myStrike) }];
    }
    out.replaceChildren(el("div", { class: "loadnote small muted" }, el("span", { class: "spinner" }), el("span", { text: "Valuing, scoring and simulating contracts…" })));
    try { out.replaceChildren(renderReversalOptions(await api("/v2/reversal/options", body))); }
    catch (e) { out.replaceChildren(); throw e; }
  }));
  return el("div", { class: "rev-options" }, el("h4", { text: "Score a call or put for this reversal" }),
    el("p", { class: "small muted", text: `Calls for a potential bottom, puts for a potential top. Each contract is valued with the reversal's own levels (target and invalidation), scored out of 100 and given a verdict, and the best one is simulated. Auto uses the ${day.leading ? "leading" : "stronger"} side, now the ${pick}.` }),
    el("div", { class: "inline-form" }, field("Side", side), field("Stock target", target), field("Invalidation (stock)", inv), field("By (trading days)", horizon), field("Contracts", qty), field("Budget ($)", budget), go),
    el("div", { class: "inline-form" }, el("span", { class: "small muted", text: "Or score your own contract:" }), field("Type", myType), field("Expiration", myExp), field("Strike", myStrike)),
    err, out);
}

function renderReversalOptions(o) {
  const th = o.thesis, frag = el("div"), su = o.setup;
  if (o.data_source === "demo") frag.append(banner("warn", "Demo data: these option prices are made up."));
  frag.append(el("p", { class: "small" }, revTag(o.side, su.stage), " ",
    el("strong", { text: `${o.contract_type === "CALL" ? "Calls" : "Puts"} on ${o.symbol} at ${money(th.spot)}: ` }),
    `target ${money(th.target_1)} (${th.target_1_is}) by ${shortDate(th.horizon_date)}, wrong on a close ${o.contract_type === "CALL" ? "below" : "above"} ${money(th.invalidation)} (${th.invalidation_is}).`,
    su.today ? el("span", { class: "muted", text: ` Today's bar so far reads ${revWord(o.side, su.today).toLowerCase()}; the stage used is from the last finished bar.` }) : null));
  const vc = o.verdict_counts;
  frag.append(el("div", { class: "metrics" }, metric(String(vc.GOOD), "Good (70 or more)"), metric(String(vc.FAIR), "Fair (50 to 69)"),
    metric(String(vc.POOR), "Poor (under 50)"), metric(String(vc.AVOID), "Avoid"),
    o.market ? metric(pct(100 * o.market.reach_target_1_by_horizon, 0), `Market-implied chance past the target on ${shortDate(th.horizon_date)}`) : null));
  frag.append(banner("info", `Exit rule: ${o.exit_rule}`));
  if (o.own && o.own.length) {
    frag.append(el("h4", { text: o.own.length === 1 ? "Your contract" : "Your contracts" }));
    for (const k of o.own) frag.append(scoredContractCard(k, o, true));
  } else if (o.best) {
    frag.append(el("h4", { text: "The best-scoring contract" }), scoredContractCard(o.best, o, true));
  } else frag.append(banner("warn", "No contracts to score for this thesis."));
  const detail = el("div");
  if (o.contracts.length > 1) {
    frag.append(el("details", {}, el("summary", { text: `All ${o.contracts.length} contracts, best score first` }),
      el("p", { class: "small muted", text: "Select a row to see how it was scored." }),
      table([
        { label: "Contract", cell: k => contractName(k) }, { label: "Verdict", cell: k => verdictTag(k.reversal_score.verdict) },
        { label: "Score", num: true, cell: k => num(k.reversal_score.score, 0) }, { label: "Cost", num: true, cell: k => money(k.premium, 0) },
        { label: "At target", num: true, cell: k => signedMoney(k.at_target) }, { label: "If invalidated", num: true, cell: k => signedMoney(k.if_invalidated) },
        { label: "Flat stock", num: true, cell: k => signedMoney(k.if_unchanged) }, { label: "Delta", num: true, cell: k => num(k.delta, 2) },
        { label: "Spread", num: true, cell: k => pct(k.spread_pct, 0) }], o.contracts,
        { onRow: k => detail.replaceChildren(scoredContractCard(k, o, false)) }), detail));
  }
  frag.append(el("ul", { class: "plain small muted" }, o.notes.map(n => el("li", { text: n }))));
  return frag;
}

function scoredContractCard(k, o, full) {
  const th = o.thesis, sc = k.reversal_score, box = el("div", { class: "grade-card" });
  box.append(el("div", { class: "grade-head" }, el("div", { class: `grade-letter word g-${sc.verdict}`, text: sc.verdict }),
    el("div", {}, el("div", { class: "grade-title", text: `${contractName(k)} on ${o.symbol}` }),
      el("div", { class: "small muted", text: `Score ${num(sc.score, 0)} of 100 · ${money(k.mid)} midpoint, ${money(k.premium, 0)} for ${th.contracts} · delta ${num(k.delta, 2)} · ${k.dte} days to expiration` }))));
  for (const t of sc.reasons || []) box.append(banner("fail", `Avoid: ${t}`));
  for (const t of sc.caps || []) box.append(banner("warn", t));
  for (const t of sc.cautions || []) box.append(banner("info", t));
  if (sc.earnings_note) box.append(banner("warn", sc.earnings_note));
  for (const f of k.flags || []) box.append(banner("warn", f));
  if (sc.components.length) box.append(el("h4", { text: "How it was scored" }), table([
    { label: "Check", cell: c => c.name }, { label: "Points", num: true, cell: c => `${num(c.points, 1)} / ${c.weight}` },
    { label: "", cell: c => el("div", { class: "score-track mini" }, el("i", { class: c.points >= c.weight / 2 ? "pos" : "neg", style: `width:${Math.max(2, 100 * c.points / c.weight)}%` })) },
    { label: "Why", cell: c => c.why }], sc.components));
  if (k.outcomes && k.outcomes.length) box.append(el("h4", { text: "Potential gain or loss" }), table([
    { label: "If the stock…", cell: x => x.case }, { label: "Stock at", num: true, cell: x => money(x.stock_price) },
    { label: "Gain or loss", num: true, cell: x => el("strong", { class: x.pnl >= 0 ? "pos-text" : "neg-text", text: signedMoney(x.pnl) }) },
    { label: "Of the premium", num: true, cell: x => isNum(x.share_of_premium) ? signedPct(100 * x.share_of_premium, 0) : "n/a" }], k.outcomes),
    el("p", { class: "small muted", text: "Black-76 values with each strike's own implied volatility, at the midpoint; the cost of crossing the spread is extra. A loss on invalidation assumes you exit when the stock closes there." }));
  const pc = k.past_cases;
  if (pc && !pc.error) box.append(pastCasesBox(pc));
  else if (pc && pc.error) box.append(el("p", { class: "small error", text: pc.error }));
  const buttons = el("div", { class: "row" });
  const simBtn = el("button", { class: "plain small-btn", text: "Open in the simulator" });
  simBtn.addEventListener("click", () => state.openSimulation(o.symbol, [{ side: "buy", contracts: th.contracts, option_type: k.option_type.toLowerCase(), expiration: k.expiration, strike: k.strike, entry_price: k.mid }]));
  const track = el("button", { class: "plain small-btn", text: "Save as a plan" });
  const note = el("span", { class: "small muted" });
  track.addEventListener("click", () => withBusy(track, note, async () => {
    const p = await api("/v2/positions", { symbol: o.symbol, instrument_type: "OPTION", quantity: th.contracts, setup_type: "reversal",
      option: { option_type: k.option_type, strike: k.strike, expiration: k.expiration }, invalidation_price: th.invalidation, target_1: th.target_1, target_2: th.target_2,
      thesis: `${revWord(o.side, o.setup.stage)} on ${o.symbol}: to ${money(th.target_1)} (${th.target_1_is}) by ${shortDate(th.horizon_date)}. Scored ${num(sc.score, 0)} (${sc.verdict}). ${o.exit_rule}` });
    note.textContent = `Saved as planned position ${p.id}.`;
  }));
  buttons.append(limitButton(o.symbol, k, th.contracts), simBtn, track);
  box.append(buttons, note);
  if (full && k.simulation && !k.simulation.error) {
    box.append(el("h4", { text: `Simulation to ${shortDate(k.simulation.eval_date)} and at expiration` }), renderSimulation(k.simulation));
  } else if (full && k.simulation && k.simulation.error) box.append(el("p", { class: "small error", text: k.simulation.error }));
  return box;
}

function pastCasesBox(pc) {
  const box = el("div", {}, el("h4", { text: `If the stock moved as it did ${pc.source.replace(/^after /, "after past ")}` }));
  box.append(el("div", { class: "metrics" }, metric(num(pc.cases, 0), "Past cases"), metric(pct(100 * pc.profitable, 0), "Would have been profitable"),
    metric(pct(100 * pc.stopped_out, 0), "Reached the invalidation (exit)"), metric(signedMoney(pc.median), "Median result"),
    metric(`${signedMoney(pc.p10)} to ${signedMoney(pc.p90)}`, "8 in 10 cases between")));
  const h = pc.histogram, n = h.counts.length, max = Math.max(1, ...h.counts);
  const W = 760, H = 160, pad = { l: 40, r: 12, t: 18, b: 30 }, bw = (W - pad.l - pad.r) / n;
  const root = svg("svg", { viewBox: `0 0 ${W} ${H}`, role: "img", "aria-label": "How many past cases ended in each range of gain or loss" });
  h.counts.forEach((c, i) => {
    const mid = (h.edges[i] + h.edges[i + 1]) / 2, bh = (H - pad.t - pad.b) * c / max;
    root.append(svg("rect", { x: pad.l + i * bw + 1, y: H - pad.b - bh, width: Math.max(1, bw - 2), height: bh, fill: `var(${mid >= 0 ? "--div-pos" : "--div-neg"})`, opacity: 0.8 },
      svg("title", { text: `${money(h.edges[i], 0)} to ${money(h.edges[i + 1], 0)}: ${c} case${c === 1 ? "" : "s"}` })));
    if (c) root.append(svg("text", { x: pad.l + (i + 0.5) * bw, y: H - pad.b - bh - 3, "text-anchor": "middle", text: String(c) }));
  });
  for (const i of [0, Math.floor(n / 2), n]) root.append(svg("text", { x: pad.l + i * bw, y: H - 10, "text-anchor": i === 0 ? "start" : i === n ? "end" : "middle", text: money(h.edges[i], 0) }));
  root.append(svg("line", { class: "zero", x1: pad.l, x2: W - pad.r, y1: H - pad.b, y2: H - pad.b }));
  box.append(el("div", { class: "chart" }, root), el("p", { class: "small muted", text: pc.note }));
  return box;
}
ANALYSIS_SECTIONS.splice(ANALYSIS_SECTIONS.indexOf(signalsSection) + 1, 0, reversalSection);

// The reversal scan, a tab of the Scanner (2.5; it sat on Analyze before). `open(row)` takes a row to Decide.
function reversalScanPanel(open) {
  const box = el("div", { class: "panel reversal-scan" }, el("h3", { text: "Reversal scan of the watchlist" }),
    el("p", { class: "small muted", text: "Which stocks show a potential bottom or top, or a confirmed reversal, on the last finished bar. Select a row to build a thesis on it: long at a bottom, short at a top." }));
  const go = el("button", { class: "plain", text: "Scan the watchlist" });
  const err = el("div", { class: "error", role: "alert" });
  const out = el("div");
  go.addEventListener("click", () => withBusy(go, err, async () => {
    const s = await api("/v2/reversal/scan");
    const rows = s.rows;
    out.replaceChildren(
      s.data_source === "demo" ? banner("warn", "Demo prices are synthetic.") : "",
      table([
        { label: "Stock", cell: x => el("strong", { text: x.symbol }) },
        { label: "Reading", cell: x => x.leading ? revTag(x.side, x.stage) : el("span", { class: "muted", text: "Nothing in sight" }) },
        { label: "Points", num: true, cell: x => num(x.score, 0) },
        { label: "Since", cell: x => x.leading ? el("span", { class: "nowrap", text: monthDay(x.since) }) : "" },
        { label: "Close", num: true, cell: x => money(x.close) },
        { label: "Wrong beyond", num: true, cell: x => x.leading ? money(x.invalidation) : "" },
        { label: "Target 1", num: true, cell: x => x.leading ? money(x.target_1) : "" },
        { label: "Trend", cell: x => trendTag(x.trend_state) },
        { label: "Today so far", cell: x => x.today ? revTag(x.side, x.today.stage) : "" }], rows, { onRow: open }),
      s.errors.length ? el("p", { class: "small muted", text: `Not scanned: ${s.errors.join("; ")}` }) : "",
      el("p", { class: "small muted", text: s.note }));
  }));
  box.append(el("div", { class: "row" }, go), err, out);
  return box;
}
BUILDERS.analyze = buildAnalyze;

/* ---------- view: data health (2.3) ---------- */
const SOURCE_TAG = { OK: ["pass", "Working"], DEGRADED: ["warn", "Failing"], PAUSED: ["fail", "Paused after repeated failures"], NOT_USED_YET: ["info", "Not used yet"] };
const FRESH_WORDS = { FRESH: "Live", DELAYED: "Delayed", LAST_SESSION: "Latest session", STALE: "Stale", UNKNOWN: "Time unknown", NONE: "No price" };
const BARS_TAG = { FRESH: ["pass", "Up to date"], LAST_SESSION: ["pass", "Up to date"], DELAYED: ["pass", "Up to date"], STALE: ["warn", "Stale"], UNKNOWN: ["warn", "Time unknown"], NOT_LOADED: ["info", "Not loaded yet"] };
const RECON_WORDS = {
  ledger_mismatches: "Stored figures that differ from the fills", lifecycle_issues: "Positions opened again after closing",
  invalid_states: "Positions whose fills no longer add up", possible_duplicate_fills: "Fills that look duplicated",
  journal_mismatches: "Journal entries whose position differs", unresolved_options: "Options past expiration with contracts open",
  market_data_gaps: "Open positions with no price history", rule_breaks: "Rows the database rules would refuse",
  missing_snapshots: "Snapshots a position should have", hindsight_snapshots: "Snapshots built from later data",
  stale_snapshots: "Snapshots that no longer apply",
  unavailable_snapshots: "Snapshots the price history cannot rebuild" };
// What each finding means for the numbers on the other screens, in order of how much it matters.
const RECON_LEVEL = { ledger_mismatches: "fail", invalid_states: "fail", rule_breaks: "fail", journal_mismatches: "warn",
  lifecycle_issues: "warn", possible_duplicate_fills: "warn", unresolved_options: "warn", market_data_gaps: "warn",
  hindsight_snapshots: "info", missing_snapshots: "info", unavailable_snapshots: "info", stale_snapshots: "warn" };

function buildHealth() {
  const error = el("div", { class: "error", role: "alert" });
  const out = el("div", {}, el("p", { class: "muted", text: "Loading…" }));
  const refresh = el("button", { class: "plain", text: "Check again" });
  // `done` is a line to keep on top after an action reloads the screen.
  async function load(done) {
    out.classList.add("refetching");
    try { out.replaceChildren(typeof done === "string" ? banner("pass", done) : "", renderHealth(await api("/v2/health/data"), load)); error.textContent = ""; }
    catch (e) { error.textContent = e.message; }
    finally { out.classList.remove("refetching"); }
  }
  refresh.addEventListener("click", () => withBusy(refresh, error, () => load()));
  const view = el("section", {}, el("div", { class: "panel-head" }, el("h2", { text: "Data health" }), refresh),
    el("p", { class: "lede", text: "Whether the numbers on the other screens can be trusted: the data source, how fresh the prices are, which positions could not be valued, the saved price history, the lifecycle snapshots, and the database itself. Nothing here shows a token or a password." }),
    error, out);
  view.onShow = load;
  return view;
}

function renderHealth(h, reload) {
  const frag = el("div"), src = h.source, q = h.quotes, rc = h.reconciliation;
  // One line on top that says whether anything needs a look.
  const problems = [];
  if (src.status === "PAUSED" || src.status === "DEGRADED") problems.push(["fail", `The data source is ${src.status === "PAUSED" ? "paused after repeated failures" : "failing"}: ${src.last_error || "no detail given"}.`]);
  if ((h.unreadable_positions || []).length) problems.push(["fail", `${h.unreadable_positions.length} open position${h.unreadable_positions.length === 1 ? "'s" : "s'"} fills no longer add up, so ${h.unreadable_positions.length === 1 ? "it is" : "they are"} left out of every figure: ${h.unreadable_positions.map(u => `${u.description} (${u.error})`).join("; ")}. Correct the fill on the Positions screen.`]);
  if (h.positions_missing_valuations.length) problems.push(["warn", `${h.positions_missing_valuations.length} open position${h.positions_missing_valuations.length === 1 ? "" : "s"} could not be valued, so totals that include them are incomplete.`]);
  if (q.stale) problems.push(["warn", `${q.stale} open position${q.stale === 1 ? " is" : "s are"} priced from a stale quote or one with no time.`]);
  if (rc && rc.needs_repair) problems.push(["fail", "Some stored figures differ from the fills they come from. Read the reconciliation report below before repairing."]);
  if (rc && !rc.database_ok) problems.push(["fail", "SQLite's own integrity check found a problem. Copy the database file somewhere safe before changing anything."]);
  frag.append(problems.length ? el("div", {}, problems.map(([lvl, t]) => banner(lvl, t))) : banner("pass", "No problems found with the data source, the prices of open positions or the stored figures."));

  frag.append(el("div", { class: "panel" }, el("h3", {}, "Data source ", tagOf(SOURCE_TAG, src.status)),
    el("div", { class: "metrics" },
      metric(src.label, src.realtime ? "Real-time quotes" : src.mode === "demo" ? "Made-up prices" : "Delayed quotes"),
      metric(src.last_success ? timeText(src.last_success) : "never", "Last good answer"),
      metric(src.last_failure ? timeText(src.last_failure) : "none", "Last failure"),
      metric(`${src.successes} / ${src.failures}`, "Requests answered / failed since the app started"),
      src.paused_for_seconds ? metric(`${num(src.paused_for_seconds, 0)} s`, "Paused for") : null),
    src.last_error ? el("p", { class: "small error", text: `Last error: ${src.last_error}` }) : null,
    el("p", { class: "small muted", text: "After three failures in a row the app waits 30 seconds before asking again. Position and portfolio screens may then show the last price received, marked stale. Pricing an option never uses a saved price, and the daily loss stop never counts a stale price as measured." })));

  const fresh = Object.entries(q.by_freshness || {});
  frag.append(el("div", { class: "panel" }, el("h3", { text: "Prices of open positions" }),
    el("div", { class: "metrics" }, metric(String(q.open_positions), "Open positions"),
      ...fresh.map(([k, n]) => metric(String(n), FRESH_WORDS[k] || k))),
    h.positions_missing_valuations.length ? el("div", {}, el("h4", { text: "Could not be valued" }),
      el("ul", { class: "small" }, h.positions_missing_valuations.map(m => el("li", { text: `${m.description}: ${m.error || "no price"}` })))) : null,
    h.missing_option_chains.length ? el("p", { class: "small", text: `${h.missing_option_chains.length === 1 ? "One is an option" : `${h.missing_option_chains.length} are options`} whose chain did not include the contract: check the strike and expiration, or whether the contract was adjusted.` }) : null,
    !q.open_positions ? el("p", { class: "small muted", text: "No open positions." }) : null));

  // Stocks not loaded yet are listed in one line, so the table shows only what is saved.
  const loaded = h.history.filter(b => b.status !== "NOT_LOADED"), notLoaded = h.history.filter(b => b.status === "NOT_LOADED");
  if (h.history.length) frag.append(el("div", { class: "panel" }, el("h3", { text: "Saved daily price history" }), !loaded.length ? el("p", { class: "small muted", text: "None saved yet." }) : table([
    { label: "Stock", cell: b => el("span", {}, el("strong", { text: b.symbol }), b.held ? el("span", { class: "small muted", text: " · held" }) : null) },
    { label: "Status", cell: b => tagOf(BARS_TAG, b.status) },
    { label: "Last finished day", cell: b => b.last_finished_bar ? shortDate(b.last_finished_bar) : "" },
    { label: "Fetched", cell: b => b.fetched_at ? timeText(b.fetched_at) : "" },
    { label: "", cell: b => b.refresh_failed ? el("span", { class: "small error", text: `Refresh failed: ${b.refresh_failed}` }) : "" }], loaded),
    notLoaded.length ? el("p", { class: "small", text: `Not loaded yet: ${notLoaded.map(b => b.symbol + (b.held ? " (held)" : "")).join(", ")}.` }) : null,
    el("p", { class: "small muted", text: "Stocks you hold and your watchlist. A stock not loaded yet loads the first time a screen needs it." })));

  if (h.snapshots) {
    const s = h.snapshots;
    const rebuild = el("button", { class: "plain small", text: "Rebuild missing snapshots" });
    const err = el("div", { class: "error", role: "alert" });
    rebuild.addEventListener("click", () => withBusy(rebuild, err, async () => {
      const r = await api("/v2/snapshots/backfill", { dry_run: false, upgrade_hindsight: true });
      await reload(`Snapshots rebuilt: ${r.created} added, ${r.superseded} replaced, ${r.unavailable} not available from the price history.`);
    }));
    frag.append(el("div", { class: "panel" }, el("h3", {}, "Lifecycle snapshots ", s.status === "OK" ? statusTag("pass", "Complete") : statusTag("warn", "Needs a look")),
      el("div", { class: "metrics" }, metric(String(s.missing.length), "Missing"), metric(String(s.hindsight.length), "Built from later data"),
        metric(String(s.unavailable.length), "Cannot be rebuilt")),
      el("p", { class: "small muted", text: "A snapshot records what the market looked like at a plan, an entry or an exit. One built after the fact from the daily close is labelled; one built from data after the fill is never used to judge the entry. Rebuilding uses the saved price history only and never overwrites a snapshot taken at the time." }),
      el("p", { class: "small" }, rebuild), err));
  }

  const m = h.migration;
  const dbPanel = el("div", { class: "panel" }, el("h3", {}, "Database ", rc ? (rc.database_ok ? statusTag("pass", "Integrity check passed") : statusTag("fail", "Integrity problem")) : ""),
    el("div", { class: "metrics" }, metric(String(m.schema_version), "Schema version"),
      m.latest ? metric(timeText(m.latest.applied_at), "Latest migration applied") : null),
    el("p", { class: "small muted", text: `${m.latest ? `Latest migration: ${m.latest.name}. ` : ""}${m.backup_before_last_migration ? `A copy of the database was saved as ${m.backup_before_last_migration} before migrating at this start.` : "No copy was taken at this start: no migration ran on existing data, or the database was new."}` }));
  if (rc) dbPanel.append(reconciliationBlock(rc, reload));
  if (h.repairs.length) dbPanel.append(el("h4", { text: "Repairs" }), el("ul", { class: "small" }, h.repairs.map(r => el("li", { text: `${timeText(r.at)}: ${r.action.toLowerCase()}, copy saved as ${(r.backup_path || "").split(/[\\/]/).pop() || "none"}` }))));
  frag.append(dbPanel);

  // 2.4: the AI plug-in and the news sources. Whether they are set up, never the keys.
  if (h.ai || h.news) {
    const a = h.ai || {}, n = h.news || {}, u = a.used_today || {};
    frag.append(el("div", { class: "panel" }, el("h3", {}, "AI and news ", a.configured ? statusTag("pass", "AI set up") : statusTag("info", "AI off")),
      el("div", { class: "metrics" },
        metric(a.configured ? a.model : "off", a.configured ? `AI model${a.web_search ? ", with web search" : ", no web search"}` : (a.reason || "AI")),
        metric(`${u.requests || 0} of ${a.daily_limit || 0}`, `AI requests today${u.failed ? ` (${u.failed} failed)` : ""}`),
        metric(`${num((u.input_tokens || 0) + (u.output_tokens || 0), 0)}`, `Tokens today${u.web_searches ? `, ${u.web_searches} web searches` : ""}`),
        metric(n.finnhub ? "Finnhub" : n.demo_headlines ? "Demo headlines" : "none", "Headlines from"),
        metric(n.sec ? "on" : "off", "SEC filings")),
      a.last_error ? el("p", { class: "small error", text: `Last AI error (${timeText(a.last_error.at)}): ${a.last_error.error}` }) : null,
      n.last_error ? el("p", { class: "small error", text: `Last news error (${timeText(n.last_error.at)}, ${n.last_error.what}): ${n.last_error.error}` }) : null,
      el("p", { class: "small muted", text: "Keys are read from .env and never shown or logged. AI requests are billed by Anthropic to the key's account; the daily limit is AI_DAILY_REQUESTS." })));
  }

  frag.append(el("div", { class: "panel" }, el("h3", { text: "Recent problems" }),
    h.recent_problems.length ? table([
      { label: "When", cell: e => el("span", { class: "nowrap", text: timeText(e.at) }) },
      { label: "What", cell: e => e.event.replaceAll("_", " ") },
      { label: "Detail", cell: e => el("span", { class: "small", text: Object.entries(e).filter(([k]) => !["at", "event", "level"].includes(k)).map(([k, v]) => `${k.replaceAll("_", " ")}: ${v}`).join(" · ") }) }], h.recent_problems)
      : el("p", { class: "small muted", text: "None since the app started." }),
    el("p", { class: "small muted", text: "Warnings the app logged since it started (data-source failures, snapshots that could not be taken, repairs). The full log is in the terminal running the app." })));
  return frag;
}

function reconciliationBlock(summary, reload) {
  const box = el("div");
  const err = el("div", { class: "error", role: "alert" });
  const details = el("div");
  const counts = Object.entries(summary.summary || {}).filter(([, n]) => n);
  box.append(el("h4", { text: "Stored figures against the fills" }),
    counts.length ? el("ul", { class: "plain small" }, counts.map(([k, n]) => el("li", {}, statusTag(RECON_LEVEL[k] || "info", String(n)), ` ${RECON_WORDS[k] || k}`)))
      : el("p", { class: "small", text: "Every position's figures match its fills." }));
  const show = el("button", { class: "plain small", text: "Show the reconciliation report" });
  show.addEventListener("click", () => withBusy(show, err, async () => details.replaceChildren(reconciliationDetail(await api("/v2/diagnostics/reconciliation"), reload))));
  box.append(el("p", { class: "small" }, show), err, details,
    el("p", { class: "small muted", text: "The report only reads. Fills are the record; status, open quantity, average prices, realized profit and fees are worked out from them and can be recomputed." }));
  return box;
}

function reconciliationDetail(r, reload) {
  const box = el("div");
  const rows = [];
  for (const key of Object.keys(RECON_WORDS)) for (const item of r[key] || []) rows.push({ key, item });
  // Money as money, quantities as quantities, times as times; never a raw float like 209.99999999999997.
  const figure = (f, v) => v === null || v === undefined ? "none" : f.endsWith("_at") ? timeText(v)
    : f === "status" ? String(v) : f === "open_quantity" ? qtyText(v) : isNum(v) ? money(v) : String(v);
  const what = ({ key, item }) => {
    if (key === "ledger_mismatches") return Object.entries(item.differences).map(([f, d]) => `${f.replaceAll("_", " ")}: stored ${figure(f, d.stored)}, from the fills ${figure(f, d.from_fills)}`).join("; ");
    if (key === "lifecycle_issues") return `bought or sold again on ${timeText(item.executed_at)} after closing; ${item.action}`;
    if (key === "possible_duplicate_fills") return `${item.count} × ${SIDE_WORDS[item.side] || item.side} ${qtyText(item.quantity)} at ${money(item.price)} on ${timeText(item.executed_at)}. ${item.note}`;
    if (key === "journal_mismatches") return item.problem || "the journal's prices differ from the position's fills";
    if (key === "unresolved_options") return `expired ${shortDate(item.expiration)} with ${qtyText(item.open_contracts)} open: record how it ended`;
    if (key === "hindsight_snapshots") return `${(item.kind || "").toLowerCase()} snapshot made from ${(item.method || "later").toLowerCase().replaceAll("_", " ")} data after the fill`;
    if (key.endsWith("_snapshots")) return `${(item.kind || item.snapshot_type || "").toLowerCase()}: ${item.reason || ""}`;
    return item.problem || "";
  };
  box.append(rows.length ? table([
    { label: "", cell: x => statusTag(RECON_LEVEL[x.key] || "info", "") },
    { label: "Finding", cell: x => RECON_WORDS[x.key] },
    { label: "Position", cell: x => { const i = x.item; const id = i.id ?? i.position_id; return `${i.symbol || ""}${id ? ` (${id})` : i.trade_id ? ` journal ${i.trade_id}` : ""}`; } },
    { label: "Detail", cell: x => el("span", { class: "small", text: what(x) }) }], rows) : el("p", { class: "small", text: "Nothing to report." }));
  box.append(el("p", { class: "small muted", text: `Checked ${timeText(r.checked_at)}: ${r.counts.positions} positions (${r.counts.open} open, ${r.counts.closed} closed, ${r.counts.planned} planned, ${r.counts.cancelled} cancelled), ${r.counts.journal_mirrors} from the option journal. Schema version ${r.schema_version}.` }));
  if (r.needs_repair) box.append(repairControl(r, reload));
  return box;
}

// Repair asks twice: the first press says exactly what will change, the second does it.
function repairControl(r, reload) {
  const err = el("div", { class: "error", role: "alert" });
  const start = el("button", { class: "plain", text: "Repair from the fills…" });
  const n = r.ledger_mismatches.length, j = r.journal_mismatches.length;
  const go = el("button", { class: "primary", text: "Copy the database, then repair" });
  const cancel = el("button", { class: "plain", text: "Cancel" });
  const ask = el("div", { class: "panel hidden" },
    el("p", { text: `This recomputes ${n} position${n === 1 ? "'s figures from its" : "s' figures from their"} fills${j ? ` and re-reads ${j} journal entr${j === 1 ? "y" : "ies"}` : ""}. A copy of the database is saved first. No fill you recorded is changed, merged or deleted${j ? "; the journal entries' fills are re-read from the journal, which is their record, and each change is kept as a correction" : ""}. Positions opened again after closing stay as recorded.` }),
    el("div", { class: "row" }, go, cancel));
  start.addEventListener("click", () => { ask.classList.remove("hidden"); start.disabled = true; });
  cancel.addEventListener("click", () => { ask.classList.add("hidden"); start.disabled = false; });
  go.addEventListener("click", () => withBusy(go, err, async () => {
    const done = await api("/v2/diagnostics/repair", { confirm: true });
    await reload(`Repaired ${done.recomputed.length} position${done.recomputed.length === 1 ? "" : "s"} and ${done.journal_resynced.length} journal entr${done.journal_resynced.length === 1 ? "y" : "ies"} from the fills.${done.failed && done.failed.length ? ` ${done.failed.length} could not be repaired because their fills do not add up: correct them on the Positions screen.` : ""} The copy taken first is ${(done.backup || "").split(/[\\/]/).pop() || "not needed (in-memory database)"}.`);
  }));
  return el("div", {}, el("p", {}, start), ask, err);
}

Object.assign(BUILDERS, { portfolio: buildPortfolio, health: buildHealth });
