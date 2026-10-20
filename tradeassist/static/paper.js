"use strict";
/* Trade Assistant 2.15: Paper trading. Pretend money on the app's recommendations (automated: it picks, splits,
   rotates and closes) or on your own trades (manual), valued while the app runs, with each portfolio's value over
   time against SPY, and a replay of any stored recommendation run over a past horizon. Paper only: nothing here
   reaches your real positions or limits, and nothing places an order. */

const PAPER_STATUS = { PENDING: ["warn", "Order resting"], OPEN: ["pass", "Open"], CLOSED: ["info", "Closed"], CANCELLED: ["info", "Cancelled"] };
const PAPER_EXIT = { STOP: "Stopped out", TARGET: "Target 1", HORIZON: "Its horizon", ROTATE: "Rotated out", MANUAL: "Closed by you",
  CLOSE: "Portfolio closed", EXPIRED: "Expired", CANCELLED: "Cancelled" };
const PAPER_SOURCES = [["all", "The best of all three"], ["morning", "Morning plan"], ["live", "News digest"], ["ideas", "Trade ideas"]];
const PAPER_COLORS = ["--series-1", "--series-2", "--series-3", "--series-4"];

// UTC ISO time -> seconds that display as New York wall-clock time on a chart axis drawn in UTC.
function nySeconds(iso) {
  const p = Object.fromEntries(new Intl.DateTimeFormat("en-US", { timeZone: "America/New_York", year: "numeric", month: "2-digit",
    day: "2-digit", hour: "2-digit", minute: "2-digit", second: "2-digit", hour12: false }).formatToParts(new Date(iso)).map(x => [x.type, x.value]));
  return Date.UTC(+p.year, +p.month - 1, +p.day, +p.hour % 24, +p.minute, +p.second) / 1000;
}

// Lines of percent returns over time (each series: name, color variable, points of {at, v}).
function equityChart(series, note) {
  const box = el("div", { class: "eq-chart" });
  const wrap = el("div", { class: "chart eq-wrap" }, box,
    el("div", { class: "legend" }, series.map(s => el("span", {}, el("i", { style: `background: var(${s.color})` }), s.name))),
    note ? el("p", { class: "small muted", text: note }) : null);
  const lib = window.LightweightCharts;
  const clean = s => {
    const seen = new Map();
    for (const p of s.points) if (isNum(p.v)) seen.set(nySeconds(p.at), p.v);
    return [...seen.entries()].sort((a, b) => a[0] - b[0]).map(([time, value]) => ({ time, value }));
  };
  const data = series.map(s => ({ ...s, data: clean(s) })).filter(s => s.data.length);
  if (!data.length || data.every(s => s.data.length < 2)) { box.replaceWith(el("p", { class: "small muted", text: "The chart fills in as the portfolio is valued (every minute while the market is open and the app runs)." })); return wrap; }
  if (!lib) {
    const all = data.flatMap(s => s.data);
    const t0 = Math.min(...all.map(p => p.time)), t1 = Math.max(...all.map(p => p.time));
    const lo = Math.min(0, ...all.map(p => p.value)), hi = Math.max(0, ...all.map(p => p.value));
    const W = 760, H = 240, X = t => 50 + 690 * (t - t0) / Math.max(t1 - t0, 1), Y = v => 10 + 210 * (1 - (v - lo) / Math.max(hi - lo, 1e-9));
    box.replaceWith(svg("svg", { viewBox: `0 0 ${W} ${H}`, role: "img", "aria-label": "Returns over time" },
      svg("line", { class: "grid", x1: 50, x2: 740, y1: Y(0), y2: Y(0) }),
      ...data.map(s => svg("path", { d: `M${s.data.map(p => `${X(p.time).toFixed(1)} ${Y(p.value).toFixed(1)}`).join(" L")}`, fill: "none", stroke: `var(${s.color})`, "stroke-width": 2 }))));
    return wrap;
  }
  requestAnimationFrame(() => {
    const c = chartColors();
    const chart = lib.createChart(box, { autoSize: true,
      layout: { background: { type: "solid", color: c.bg }, textColor: c.ink, attributionLogo: false },
      grid: { vertLines: { color: c.line }, horzLines: { color: c.line } }, rightPriceScale: { borderColor: c.line },
      timeScale: { borderColor: c.line, timeVisible: true, secondsVisible: false },
      localization: { priceFormatter: v => `${v >= 0 ? "+" : ""}${v.toFixed(2)}%` } });
    for (const s of data) {
      const line = chart.addSeries(lib.LineSeries, { color: c[`s${(PAPER_COLORS.indexOf(s.color) + 1) || 1}`] || c.accent, lineWidth: 2,
        priceLineVisible: false, lastValueVisible: true, lineStyle: s.dashed ? 2 : 0 });
      line.setData(s.data);
    }
    chart.timeScale().fitContent();
    wrap.dispose = () => chart.remove();
  });
  return wrap;
}

function marksSeries(marks, budget, name, color) {
  return { name, color, points: marks.map(m => ({ at: m.at, v: 100 * (m.equity / budget - 1) })) };
}

function spySeries(marks) {
  const first = marks.find(m => isNum(m.spy));
  if (!first) return null;
  return { name: "SPY", color: "--series-4", dashed: true, points: marks.filter(m => isNum(m.spy)).map(m => ({ at: m.at, v: 100 * (m.spy / first.spy - 1) })) };
}

function paperCreatePanel(onCreated) {
  const b = (state.prefs && state.prefs.blend) || {};
  const mode = segmented(`pmode${++uid}`, [["AUTO", "Automated"], ["MANUAL", "Manual"]], "AUTO");
  const name = el("input", { type: "text", placeholder: "optional", maxlength: 80 });
  const budget = el("input", { type: "number", min: 100, step: 100, value: String(Math.round(b.budget || 10000)) });
  const source = el("select", {}, PAPER_SOURCES.map(([v, t]) => el("option", { value: v, text: t })));
  const horizon = segmented(`phz${++uid}`, [["day", "Day trades"], ["swing", "Swing trades"]], "day");
  const ends = el("select", {}, [["day", "After one session"], ["week", "After a week (5 sessions)"], ["month", "After a month (21 sessions)"], ["custom", "On a date"], ["open", "When I close it"]]
    .map(([v, t]) => el("option", { value: v, text: t })));
  const endsOn = el("input", { type: "date", class: "hidden", "aria-label": "End date" });
  const goal = segmented(`pgoal${++uid}`, BLEND_GOALS, b.goal || "balanced");
  const tail = el("input", { type: "number", min: 0.25, max: 20, step: 0.25, value: String(b.tail_pct || 2) });
  const topN = el("input", { type: "number", min: 1, max: 25, value: "10" });
  const maxPos = el("input", { type: "number", min: 1, max: 25, value: "10" });
  const fill = el("select", {}, [["REALISTIC", "Realistic"], ["CONSERVATIVE", "Conservative"], ["MIDPOINT", "Midpoint (optimistic)"]].map(([v, t]) => el("option", { value: v, text: t })));
  const rotate = el("input", { type: "checkbox", checked: true });
  const refresh = el("select", {}, [5, 10, 15, 30, 60, 120].map(m => el("option", { value: m, text: `${m} min`, selected: m === 15 })));
  const limits = el("input", { type: "checkbox", checked: true });
  const instBoxes = BLEND_INSTRUMENTS.map(([v, t]) => el("label", { class: "check" }, el("input", { type: "checkbox", value: v, checked: true }), t));
  const go = el("button", { class: "primary", text: "Open the portfolio" });
  const err = el("div", { class: "error", role: "alert" });
  const autoOnly = [];
  const sync = () => {
    const auto = mode.value() === "AUTO";
    for (const n of autoOnly) n.classList.toggle("hidden", !auto);
    endsOn.classList.toggle("hidden", ends.value !== "custom");
    if (horizon.value() === "swing" && ends.value === "day") ends.value = "month";
    if (horizon.value() === "day" && refresh.value === "120") refresh.value = "15";
  };
  for (const n of [mode, horizon, ends]) n.addEventListener("change", sync);
  go.addEventListener("click", () => withBusy(go, err, async () => {
    const body = { mode: mode.value(), name: name.value.trim(), budget: Number(budget.value), horizon: horizon.value(), ends: ends.value,
      goal: goal.value(), tail_pct: Number(tail.value), top_n: Number(topN.value), max_positions: Number(maxPos.value), fill_model: fill.value,
      rotate: rotate.checked, refresh_minutes: Number(refresh.value), obey_limits: limits.checked,
      instruments: instBoxes.map(l => l.querySelector("input")).filter(x => x.checked).map(x => x.value) };
    if (ends.value === "custom") body.ends_on = endsOn.value;
    if (body.mode === "AUTO") body.source = source.value;
    const out = await api("/v2/paper/portfolios", body);
    if (out.errors && out.errors.length) err.textContent = out.errors.join(" ");
    onCreated(out.portfolio.id);
  }));
  const srcField = field("Take trades from", source), goalField = infoField("Goal", "goal", goal), tailField = infoField("Worst tenth may lose (%)", "tail", tail),
    topField = field("Opportunities to consider", topN), rotField = el("div", { class: "field" }, el("span", {}, "Rotation ", infoIcon("rotate")), el("label", { class: "check" }, rotate, "Sell what has faded")),
    refField = field("Look again every", refresh);
  autoOnly.push(srcField, goalField, tailField, topField, rotField, refField);
  sync();
  return el("details", { class: "panel" }, el("summary", { text: "Open a new paper portfolio" }),
    el("p", { class: "small muted", text: "Automated: it takes the best opportunities, splits the budget with the blend optimizer and trades on paper, looking again every few minutes. Manual: you place the trades (here, from any blend, or from Analyze opportunity)." }),
    el("div", { class: "inline-form" }, field("Mode", mode), field("Name", name), infoField("Budget ($)", "budget", budget), srcField, infoField("Trades", "horizon", horizon),
      el("div", { class: "field" }, el("span", { text: "Ends" }), el("div", { class: "row nowrap" }, ends, endsOn)), goalField, tailField, topField,
      field("Positions at most", maxPos),
      el("div", { class: "field" }, el("span", {}, "Instruments ", infoIcon("instruments")), el("div", { class: "inst-checks" }, instBoxes)),
      infoField("Fills", "fill", fill), rotField, refField,
      el("div", { class: "field" }, el("span", {}, "Limits ", infoIcon("limits")), el("label", { class: "check" }, limits, "Apply my real-money limits")), go),
    err);
}

function paperTradeForm(pid, onDone) {
  const sym = el("input", { class: "sym", type: "text", placeholder: "AAPL", "aria-label": "Ticker" });
  const inst = el("select", {}, BLEND_INSTRUMENTS.map(([v, t]) => el("option", { value: v, text: t === "Shorts" ? "Short" : t.replace(/s$/, "") })));
  const qty = el("input", { type: "number", min: 1, step: 1, value: "10" });
  const exp = el("input", { type: "date", "aria-label": "Expiration" });
  const strike = numInput({ placeholder: "strike" });
  const limit = numInput({ placeholder: "ask" });
  const stop = numInput({ placeholder: "none" }), t1 = numInput({ placeholder: "none" });
  const go = el("button", { class: "primary", text: "Place the paper order" });
  const err = el("div", { class: "error", role: "alert" });
  const optFields = [field("Expiration", exp), field("Strike", strike), field("Limit price", limit)];
  const sync = () => { const o = inst.value === "call" || inst.value === "put"; for (const f of optFields) f.classList.toggle("hidden", !o); };
  inst.addEventListener("change", sync);
  sync();
  go.addEventListener("click", () => withBusy(go, err, async () => {
    const body = { symbol: sym.value.trim().toUpperCase(), instrument: inst.value, quantity: Math.round(Number(qty.value)), why: "placed by hand" };
    if (inst.value === "call" || inst.value === "put") {
      body.option = { strike: Number(strike.value), expiration: exp.value };
      if (limit.value) body.limit_price = Number(limit.value);
    }
    if (stop.value) body.stop = Number(stop.value);
    if (t1.value) body.target_1 = Number(t1.value);
    onDone(await api(`/v2/paper/portfolios/${pid}/trades`, body));
  }));
  return el("details", { class: "panel" }, el("summary", { text: "Place a trade by hand" }),
    el("div", { class: "inline-form" }, field("Ticker", sym), field("Instrument", inst), field("Quantity (shares or contracts)", qty), ...optFields,
      field("Stop (stock price)", stop), field("Target 1 (stock price)", t1), go),
    el("p", { class: "small muted", text: "Shares and shorts fill at the next price in the regular session; a call or put is a limit order at the price you give (the ask if you leave it blank) and fills when the market reaches it. Stops and targets are on the stock's price." }), err);
}

function paperDetail(d, actions) {
  const p = d.portfolio, st = d.stats;
  const frag = el("div");
  const err = el("div", { class: "error", role: "alert" });
  const status = p.status === "ACTIVE" ? statusTag("pass", "Active") : statusTag("info", p.mode === "REPLAY" ? "Replay" : "Closed");
  frag.append(el("div", { class: "panel" },
    el("div", { class: "panel-head" }, el("h3", {}, `${p.name} `, status),
      el("span", { class: "small muted", text: `${{ AUTO: "Automated", MANUAL: "Manual", REPLAY: "Replay" }[p.mode]} · ${p.horizon === "day" ? "day trades" : "swing trades"} · ${p.fill_model.toLowerCase()} fills${p.ends_at ? ` · ends ${timeText(p.ends_at)}` : ""}` })),
    el("div", { class: "metrics" },
      metric(money(st.equity, 0), `Value (from ${money(st.budget, 0)})`),
      metric(el("span", { class: st.pnl >= 0 ? "pos-text" : "neg-text", text: `${signedMoney(st.pnl, 0)} (${signedPct(st.return_pct, 2)})` }), "Result so far"),
      metric(isNum(st.spy_return_pct) ? signedPct(st.spy_return_pct, 2) : "n/a", "SPY over the same time"),
      metric(pct(st.max_drawdown_pct, 2), "Deepest drop from a high"),
      metric(isNum(st.win_rate) ? pct(100 * st.win_rate, 0) : "n/a", `Winners (${st.closed_trades} closed)`),
      metric(isNum(st.average_r) ? `${num(st.average_r, 2)} R` : "n/a", "Average result per unit of risk")),
    st.modelled_marks ? el("p", { class: "small muted", text: `${st.modelled_marks} of the values include an option valued by the model (no quote, or the app was closed).` }) : null,
    el("p", { class: "small muted", text: d.note })));
  const series = [marksSeries(d.marks, p.budget, p.name, "--series-1")];
  const spy = spySeries(d.marks);
  if (spy) series.push(spy);
  frag.append(el("div", { class: "panel" }, el("h3", { text: "Value over time" }), equityChart(series,
    "Percent change from the starting budget, against SPY's own percent change; New York time.")));
  const row = el("div", { class: "row" });
  const btn = (text, fn, cls = "plain") => { const b = el("button", { class: cls, text }); b.addEventListener("click", () => withBusy(b, err, fn)); row.append(b); return b; };
  if (p.status === "ACTIVE") {
    btn("Value it now", () => actions.call(`/v2/paper/portfolios/${p.id}/mark`, {}));
    if (p.mode === "AUTO") {
      btn("Run the automated pass now", () => actions.call(`/v2/paper/portfolios/${p.id}/step`, {}));
      btn(p.rotate ? "Stop rotating" : "Rotate again", () => actions.call(`/v2/paper/portfolios/${p.id}/settings`, { rotate: !p.rotate }));
    }
    btn("Close the portfolio", () => actions.call(`/v2/paper/portfolios/${p.id}/close`, {}));
  }
  btn("Delete", async () => { await api(`/v2/paper/portfolios/${p.id}/delete`, {}); actions.deleted(); });
  frag.append(el("div", { class: "panel" }, row, err));
  const positions = d.positions;
  frag.append(el("div", { class: "panel" }, el("h3", { text: `Positions (${positions.length})` }), positions.length ? table([
    { label: "Trade", cell: x => el("div", {}, el("strong", { text: x.symbol }), ` ${x.multiplier > 1 ? `${x.expiration} $${num(x.strike, x.strike % 1 ? 2 : 0)} ${x.instrument.toLowerCase()}` : INSTRUMENT_WORDS[x.instrument]}`,
      el("div", { class: "small muted", text: `${num(x.quantity, 0)}${x.open_quantity && x.open_quantity !== x.quantity ? ` (${num(x.open_quantity, 0)} open)` : ""} · ${x.setup_label || x.source || ""}` })) },
    { label: "Status", cell: x => tagOf(PAPER_STATUS, x.status) },
    { label: "Entry", num: true, cell: x => x.entry_price ? el("div", {}, money(x.entry_price), el("div", { class: "small muted", text: timeText(x.entry_at) })) : x.limit_price ? `limit ${money(x.limit_price)}` : "—" },
    { label: "Now / exit", num: true, cell: x => x.status === "CLOSED" ? el("div", {}, money(x.exit_price), el("div", { class: "small muted", text: PAPER_EXIT[x.exit_reason] || x.exit_reason || "" }))
      : isNum(x.last_price) ? el("div", {}, money(x.last_price), el("div", { class: "small muted", text: x.mark_basis === "quote" ? "" : (x.mark_basis || "") })) : "—" },
    { label: "Result", num: true, cell: x => isNum(x.total_pnl) && x.status !== "PENDING" && x.status !== "CANCELLED" ? el("span", { class: x.total_pnl >= 0 ? "pos-text" : "neg-text", text: signedMoney(x.total_pnl, 2) }) : "—" },
    { label: "Stop / target", num: true, cell: x => `${x.stop ? money(x.stop) : "—"} / ${x.target_1 ? money(x.target_1) : "—"}` },
    { label: "", cell: x => {
      if (p.status !== "ACTIVE" || (x.status !== "OPEN" && x.status !== "PENDING")) return "";
      const q = el("input", { type: "number", min: 1, max: x.open_quantity || x.quantity, step: 1, value: String(x.open_quantity || x.quantity), "aria-label": "Quantity to close", class: "qty" });
      const b = el("button", { class: "plain small-btn", text: x.status === "PENDING" ? "Cancel" : "Close" });
      b.addEventListener("click", () => withBusy(b, err, () => actions.call(`/v2/paper/positions/${x.id}/close`, x.status === "PENDING" ? {} : { quantity: Math.round(Number(q.value)) })));
      return el("div", { class: "row nowrap" }, x.status === "OPEN" ? q : "", b);
    } }], positions) : el("p", { class: "small muted", text: "No trades yet." })));
  if (p.status === "ACTIVE") {
    frag.append(paperTradeForm(p.id, out => actions.show(out)));
    const src = el("select", {}, PAPER_SOURCES.map(([v, t]) => el("option", { value: v, text: t })));
    const holder = el("div");
    const open = el("button", { class: "plain", text: "Show the split" });
    open.addEventListener("click", () => {
      holder.replaceChildren(blendPanel({ source: src.value, horizon: p.horizon, analyze: false, addToPortfolio: p.id,
        onAdded: out => actions.show(out), title: "Add a blend to this portfolio" }));
    });
    frag.append(el("details", { class: "panel" }, el("summary", { text: "Add a blend of opportunities" }),
      el("div", { class: "row" }, field("From", src), open), holder));
  }
  if ((st.by_source || []).length || (st.by_instrument || []).length) {
    const grp = (title, rows) => el("div", {}, el("h4", { text: title }), table([{ label: "", cell: g => g.key }, { label: "Trades", num: true, cell: g => num(g.trades, 0) },
      { label: "Result", num: true, cell: g => el("span", { class: g.pnl >= 0 ? "pos-text" : "neg-text", text: signedMoney(g.pnl, 0) }) },
      { label: "Ahead", num: true, cell: g => `${g.wins} of ${g.trades}` }], rows));
    frag.append(el("div", { class: "panel two-col" }, grp("By source", st.by_source), grp("By instrument", st.by_instrument)));
  }
  frag.append(el("details", { class: "panel" }, el("summary", { text: `The log (${d.events.length})` }),
    table([{ label: "When (New York)", cell: e => timeText(e.at) }, { label: "", cell: e => el("span", { class: "small muted", text: e.kind.toLowerCase().replace("_", " ") }) },
      { label: "What", cell: e => e.text }], d.events)));
  return frag;
}

function replayPanel(onSaved) {
  const run = el("select", { "aria-label": "Run to replay" }, el("option", { value: "", text: "Loading…" }));
  const budget = el("input", { type: "number", min: 100, step: 100, value: String(Math.round(((state.prefs || {}).blend || {}).budget || 10000)) });
  const goal = segmented(`rgoal${++uid}`, BLEND_GOALS, "balanced");
  const days = el("select", {}, [["", "The run's own (a session for day trades)"], ["1", "1 session"], ["5", "5 sessions"], ["10", "10 sessions"], ["20", "20 sessions"]].map(([v, t]) => el("option", { value: v, text: t })));
  const topN = el("input", { type: "number", min: 1, max: 25, value: "10" });
  const save = el("input", { type: "checkbox" });
  const go = el("button", { class: "primary", text: "Replay it" });
  const err = el("div", { class: "error", role: "alert" });
  const out = el("div");
  const load = async () => {
    try {
      const r = await api("/v2/paper/runs");
      run.replaceChildren(...r.runs.map(x => el("option", { value: `run:${x.id}`, text: `${x.label} (${x.count})` })),
        ...r.picks_days.map(day => el("option", { value: `picks:${day}`, text: `Morning plan picks of ${shortDate(day)} (before 2.15)` })));
      if (!run.options.length) run.append(el("option", { value: "", text: "No stored runs yet: build a Morning plan, a news digest or an ideas scan first" }));
    } catch (e) { err.textContent = e.message; }
  };
  go.addEventListener("click", () => withBusy(go, err, async () => {
    if (!run.value) throw new Error("Choose a run to replay.");
    const [kind, id] = [run.value.split(":")[0], run.value.slice(run.value.indexOf(":") + 1)];
    const body = { budget: Number(budget.value), goal: goal.value(), top_n: Number(topN.value), save: save.checked };
    if (kind === "run") body.run_id = Number(id); else body.picks_day = id;
    if (days.value) body.horizon_days = Number(days.value);
    const r = await api("/v2/paper/replay", body);
    const series = [{ name: "This run", color: "--series-1", points: r.curve.map(c => ({ at: c.at, v: 100 * (c.equity / r.budget - 1) })) }];
    const first = r.curve.find(c => isNum(c.spy));
    if (first) series.push({ name: "SPY", color: "--series-4", dashed: true, points: r.curve.filter(c => isNum(c.spy)).map(c => ({ at: c.at, v: 100 * (c.spy / first.spy - 1) })) });
    for (const w of out.querySelectorAll(".eq-wrap")) if (w.dispose) w.dispose();      // the last replay's chart
    out.replaceChildren(
      el("div", { class: "metrics" },
        metric(el("span", { class: r.return_pct >= 0 ? "pos-text" : "neg-text", text: signedPct(r.return_pct, 2) }), `${money(r.budget, 0)} became ${money(r.final, 0)}`),
        metric(isNum(r.spy_return_pct) ? signedPct(r.spy_return_pct, 2) : "n/a", "SPY over the same time"),
        metric(pct(r.max_drawdown_pct, 2), "Deepest drop"), metric(isNum(r.win_rate) ? pct(100 * r.win_rate, 0) : "n/a", "Winners")),
      ...r.notes.map(n => el("p", { class: "small muted", text: n })),
      equityChart(series, `${r.label}: ${timeText(r.started)} to ${timeText(r.ended)}${r.modelled ? "; options valued by the model" : ""}.`),
      table([{ label: "Trade", cell: x => x.error ? el("span", { class: "small error", text: `${x.symbol}: ${x.error}` }) : `${x.symbol} ${INSTRUMENT_WORDS[x.instrument]} ×${num(x.quantity, 0)}` },
        { label: "Entry", num: true, cell: x => x.entry_price ? money(x.entry_price) : "" },
        { label: "Exit", num: true, cell: x => x.exit_price !== undefined ? el("div", {}, money(x.exit_price), el("div", { class: "small muted", text: PAPER_EXIT[x.exit_reason] || "" })) : "" },
        { label: "Result", num: true, cell: x => isNum(x.pnl) ? el("span", { class: x.pnl >= 0 ? "pos-text" : "neg-text", text: signedMoney(x.pnl, 2) }) : "" }], r.trades),
      r.portfolio_id ? el("p", { class: "small" }, "Kept as a closed portfolio in the list above.") : "");
    if (r.portfolio_id && onSaved) onSaved(r.portfolio_id);
  }));
  const node = el("details", { class: "panel" }, el("summary", {}, "Replay a past run ", infoIcon("replay")),
    el("p", { class: "small muted", text: "What a stored recommendation list would have done: the blend optimizer splits the budget as it would have then, each trade enters at the open of the first bar after the run (never earlier), and its stop, target and horizon play out on price history." }),
    el("div", { class: "inline-form" }, field("Run", run), infoField("Budget ($)", "budget", budget), infoField("Goal", "goal", goal), field("Horizon", days),
      field("Opportunities to take", topN), el("label", { class: "check" }, save, "Keep it as a closed portfolio"), go), err, out);
  node.addEventListener("toggle", () => { if (node.open) load(); });
  return node;
}

function buildPaper() {
  const err = el("div", { class: "error", role: "alert" });
  const listBox = el("div");
  const compareBox = el("div");
  const detailBox = el("div");
  const compare = new Set();
  let openId = null, timer = null, listing = [];
  const call = async (path, body) => show_(await api(path, body));
  const dispose = box => { for (const w of box.querySelectorAll(".eq-wrap")) if (w.dispose) w.dispose(); };
  let shownSig = "";
  const sigOf = d => JSON.stringify([d.portfolio.id, d.portfolio.status, d.portfolio.rotate, d.marks.length, (d.marks[d.marks.length - 1] || {}).at,
    d.positions.map(x => [x.id, x.status, x.last_price, x.open_quantity]), d.events.length]);
  const show_ = (d, background) => {
    const sig = sigOf(d);
    // A background refresh never redraws under the pointer of someone typing, and never redraws what has not changed.
    if (background && (sig === shownSig || detailBox.contains(document.activeElement))) return;
    shownSig = sig;
    dispose(detailBox);
    detailBox.replaceChildren(paperDetail(d, { call, show: show_, deleted: () => { openId = null; dispose(detailBox); detailBox.replaceChildren(); refreshList(); } }));
    if (d.errors && d.errors.length) detailBox.prepend(banner("warn", d.errors.join(" ")));
    if (!background) refreshList();
  };
  async function open(pid) {
    openId = pid;
    try { show_(await api(`/v2/paper/portfolios/${pid}`)); detailBox.scrollIntoView({ behavior: "smooth", block: "start" }); }
    catch (e) { err.textContent = e.message; }
  }
  async function drawCompare() {
    if (compare.size < 1) { dispose(compareBox); compareBox.replaceChildren(); return; }
    const details = await Promise.all([...compare].map(id => api(`/v2/paper/portfolios/${id}`).catch(() => null)));
    const series = details.filter(Boolean).map((d, i) => marksSeries(d.marks, d.portfolio.budget, d.portfolio.name, PAPER_COLORS[i % 3]));
    const spy = details.filter(Boolean).map(d => spySeries(d.marks)).find(Boolean);
    if (spy) series.push(spy);
    dispose(compareBox);
    compareBox.replaceChildren(el("div", { class: "panel" }, el("h3", { text: "Side by side" }), equityChart(series, "Each portfolio's percent change from its own budget, and SPY's.")));
  }
  async function refreshList() {
    try {
      const r = await api("/v2/paper");
      listing = r.portfolios;
      listBox.replaceChildren(el("div", { class: "panel" }, el("h3", { text: `Portfolios (${listing.length})` }),
        listing.length ? table([
          { label: "", cell: x => { const b = el("input", { type: "checkbox", "aria-label": "Compare", checked: compare.has(x.id) });
            b.addEventListener("click", e => e.stopPropagation());
            b.addEventListener("change", () => { if (b.checked) compare.add(x.id); else compare.delete(x.id); drawCompare(); }); return b; } },
          { label: "Portfolio", cell: x => el("div", {}, el("strong", { text: x.name }), el("div", { class: "small muted", text: `${{ AUTO: "Automated", MANUAL: "Manual", REPLAY: "Replay" }[x.mode]} · ${x.horizon} · ${x.source}` })) },
          { label: "Status", cell: x => x.status === "ACTIVE" ? statusTag("pass", "Active") : statusTag("info", "Closed") },
          { label: "Value", num: true, cell: x => money(x.equity, 0) },
          { label: "Result", num: true, cell: x => el("span", { class: x.return_pct >= 0 ? "pos-text" : "neg-text", text: signedPct(x.return_pct, 2) }) },
          { label: "Open", num: true, cell: x => num(x.open_positions, 0) },
          { label: "Opened", cell: x => timeText(x.created_at) }], listing, { onRow: x => open(x.id) })
          : el("p", { class: "small muted", text: "None yet. Open one below, or paper trade a blend from the Morning plan, Live news, Trade ideas or Analyze opportunity." }),
        r.clockwork && !r.clockwork.running && listing.some(x => x.status === "ACTIVE")
          ? el("p", { class: "small muted", text: "The background valuing is starting." }) : null,
        el("p", { class: "small muted", text: "Tick portfolios to draw them side by side. Active portfolios are valued every minute while the market is open and this app is running; run ./setup_paper.sh to keep the app running on market days." })));
    } catch (e) { err.textContent = e.message; }
  }
  async function poll() {
    clearTimeout(timer);
    if (view.classList.contains("hidden")) return;
    await refreshList();
    if (openId && listing.some(x => x.id === openId && x.status === "ACTIVE")) {
      try { const d = await api(`/v2/paper/portfolios/${openId}`); if (openId === d.portfolio.id) show_(d, true); } catch (e) { /* shown next time */ }
    }
    timer = setTimeout(poll, 30000);
  }
  const view = el("section", {}, el("h2", { text: "Paper trading" }),
    el("p", { class: "lede", text: "Pretend money on the app's recommendations or your own trades: automated portfolios pick, split, rotate and close by themselves; manual ones are yours to run. Each is valued while the app runs, against SPY, over the horizon you choose. Paper only: it never touches your real positions or limits, and nothing here places an order." }),
    err, paperCreatePanel(pid => { open(pid); refreshList(); }), listBox, compareBox, detailBox, replayPanel(pid => refreshList()));
  view.onShow = () => { loadPrefs(); poll(); };
  view.open = open;
  return view;
}

state.openPaper = pid => { show("paper"); if (state.views.paper) state.views.paper.open(pid); };

Object.assign(BUILDERS, { paper: buildPaper });
