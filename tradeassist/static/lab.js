"use strict";
/* Trade Assistant 2.4 screens: the options lab (volatility and Monte Carlo), the scanner, and the
   strategy builder that feeds the simulator. Loaded after app.js and v2.js, whose helpers it uses. */

const ivPct = v => isNum(v) ? `${(100 * v).toFixed(1)}%` : "n/a";
const points = v => isNum(v) ? `${v > 0 ? "+" : ""}${v.toFixed(1)} pts` : "n/a";
// Another screen can open the lab on a ticker: openInLab("MSFT").
const LAB_NEXT = { symbol: null };
function openInLab(symbol) { LAB_NEXT.symbol = symbol; show("lab"); }
const SHAPE_TAG = { UPWARD: ["pass", "Upward sloping"], FLAT: ["info", "Flat"], INVERTED: ["warn", "Inverted: event priced soon"] };

/* ---------- chart: implied volatility by days to expiration ---------- */
function termChart(p) {
  const rows = p.expirations.filter(r => isNum(r.atm_iv));
  const wrap = el("div", { class: "chart chart-scroll" });
  if (rows.length < 2) { wrap.append(el("p", { class: "muted", text: "Not enough expirations with quotes to draw." })); return wrap; }
  const W = 760, H = 280, pad = { l: 60, r: 110, t: 18, b: 34 };
  const series = [
    { key: "atm_iv", name: "At the money", color: "--series-1" },
    { key: "iv_25d_put", name: "25-delta put", color: "--series-2" },
    { key: "iv_25d_call", name: "25-delta call", color: "--series-3" },
  ].filter(s => rows.filter(r => isNum(r[s.key])).length >= 2);
  const all = rows.flatMap(r => series.map(s => r[s.key]).filter(isNum));
  const ys = niceScale(Math.min(...all), Math.max(...all), 5);
  const xs = niceScale(0, Math.max(...rows.map(r => r.dte)), 6);
  const X = d => pad.l + (W - pad.l - pad.r) * (d - xs.lo) / (xs.hi - xs.lo);
  const Y = v => pad.t + (H - pad.t - pad.b) * (1 - (v - ys.lo) / (ys.hi - ys.lo));
  const root = svg("svg", { viewBox: `0 0 ${W} ${H}`, role: "img", "aria-label": "Implied volatility by days to expiration" });
  for (const v of ys.ticks) root.append(svg("line", { class: "grid", x1: pad.l, x2: W - pad.r, y1: Y(v), y2: Y(v) }),
    svg("text", { x: pad.l - 8, y: Y(v) + 4, "text-anchor": "end", text: ivPct(v) }));
  for (const d of xs.ticks) if (d <= xs.hi) root.append(svg("text", { x: X(d), y: H - 14, "text-anchor": "middle", text: String(d) }));
  root.append(svg("text", { x: (pad.l + W - pad.r) / 2, y: H, "text-anchor": "middle", text: "Days to expiration" }));
  if (p.earnings && p.earnings.date) {
    const days = Math.round((new Date(p.earnings.date) - new Date(p.expirations[0].expiration)) / 86400000) + p.expirations[0].dte;
    if (days >= xs.lo && days <= xs.hi) root.append(svg("line", { class: "now-line", x1: X(days), x2: X(days), y1: pad.t, y2: H - pad.b, "stroke-dasharray": "4 4" }),
      halo({ x: X(days) + 5, y: pad.t + 12, class: "lbl", text: `Earnings${p.earnings.estimated ? " (estimated)" : ""}` }));
  }
  const ends = [];
  for (const s of series) {
    const pts = rows.filter(r => isNum(r[s.key]));
    root.append(svg("path", { d: pts.map((r, i) => `${i ? "L" : "M"}${X(r.dte).toFixed(1)} ${Y(r[s.key]).toFixed(1)}`).join(" "),
      fill: "none", stroke: `var(${s.color})`, "stroke-width": 2, "stroke-linejoin": "round" }));
    for (const r of pts) root.append(svg("circle", { cx: X(r.dte), cy: Y(r[s.key]), r: 4, fill: `var(${s.color})`, stroke: "var(--panel)", "stroke-width": 2 }));
    ends.push({ text: s.name, y: Y(pts[pts.length - 1][s.key]) + 4 });
  }
  for (const e of spreadLabels(ends, 13, pad.t + 4, H - pad.b)) root.append(svg("text", { x: W - pad.r + 8, y: e.y, text: e.text }));
  const hover = svg("line", { class: "hover-line", y1: pad.t, y2: H - pad.b, visibility: "hidden" });
  root.append(hover);
  const tip = el("div", { class: "tip" });
  root.addEventListener("pointermove", evt => {
    const box = root.getBoundingClientRect();
    const px = (evt.clientX - box.left) * W / box.width;
    const r = rows.reduce((a, b) => Math.abs(X(b.dte) - px) < Math.abs(X(a.dte) - px) ? b : a, rows[0]);
    hover.setAttribute("x1", X(r.dte)); hover.setAttribute("x2", X(r.dte)); hover.setAttribute("visibility", "visible");
    tip.replaceChildren(el("strong", { text: `${shortDate(r.expiration)} · ${r.dte} days` }),
      ...series.map(s => tipRow(ivPct(r[s.key]), s.name, s.color)),
      tipRow(isNum(r.one_sd_move) ? `±${money(r.one_sd_move)}` : "n/a", "one standard deviation", null));
    tooltipAt(tip, box, X(r.dte), W);
  });
  root.addEventListener("pointerleave", () => { hover.setAttribute("visibility", "hidden"); tip.style.display = "none"; });
  if (series.length > 1) wrap.append(el("div", { class: "legend" }, series.map(s => el("span", {}, el("i", { style: `background: var(${s.color})` }), s.name))));
  wrap.append(root, tip);
  return wrap;
}

/* ---------- chart: where the simulated stock price ends ---------- */
function outcomeChart(m) {
  const h = m.histogram, W = 760, H = 240, pad = { l: 56, r: 24, t: 22, b: 34 };
  const n = h.counts.length, total = h.counts.reduce((a, b) => a + b, 0) || 1;
  const lo = h.edges[0], hi = h.edges[n];
  const X = v => pad.l + (W - pad.l - pad.r) * (v - lo) / (hi - lo);
  const share = h.counts.map(c => 100 * c / total);
  const ys = niceScale(0, Math.max(...share), 4);
  const Y = v => pad.t + (H - pad.t - pad.b) * (1 - v / ys.hi);
  const root = svg("svg", { viewBox: `0 0 ${W} ${H}`, role: "img", "aria-label": "Share of simulated paths by stock price at expiration" });
  for (const v of ys.ticks) root.append(svg("line", { class: "grid", x1: pad.l, x2: W - pad.r, y1: Y(v), y2: Y(v) }),
    svg("text", { x: pad.l - 8, y: Y(v) + 4, "text-anchor": "end", text: `${v.toFixed(v < 1 ? 1 : 0)}%` }));
  const bars = [];
  for (let i = 0; i < n; i++) {
    const x0 = X(h.edges[i]) + 1, x1 = X(h.edges[i + 1]) - 1;
    const itm = m.option_type === "call" ? h.edges[i] >= h.strike : h.edges[i + 1] <= h.strike;
    const bar = svg("rect", { x: x0, y: Y(share[i]), width: Math.max(1, x1 - x0), height: Math.max(0, Y(0) - Y(share[i])), rx: 2,
      fill: `var(${itm ? "--series-1" : "--muted"})`, "fill-opacity": itm ? 1 : 0.35 });
    bars.push(bar); root.append(bar);
  }
  const xt = niceScale(lo, hi, 6);
  for (const v of xt.ticks) if (v >= lo && v <= hi) root.append(svg("text", { x: X(v), y: H - 14, "text-anchor": "middle", text: money(v, xt.step < 1 ? 2 : 0) }));
  root.append(svg("text", { x: (pad.l + W - pad.r) / 2, y: H, "text-anchor": "middle", text: "Stock price at expiration" }));
  for (const [v, word] of [[h.strike, `Strike ${money(h.strike)}`], [m.spot, `Now ${money(m.spot)}`]]) {
    if (v < lo || v > hi) continue;
    root.append(svg("line", { class: "now-line", x1: X(v), x2: X(v), y1: pad.t, y2: H - pad.b, "stroke-dasharray": v === h.strike ? "4 4" : null }),
      halo({ x: X(v) + 5, y: pad.t + (v === h.strike ? 12 : 26), class: "lbl", text: word }));
  }
  const tip = el("div", { class: "tip" });
  root.addEventListener("pointermove", evt => {
    const box = root.getBoundingClientRect();
    const px = (evt.clientX - box.left) * W / box.width;
    const i = Math.max(0, Math.min(n - 1, Math.floor((px - pad.l) / (W - pad.l - pad.r) * n)));
    bars.forEach((b, k) => b.setAttribute("opacity", k === i ? 1 : 0.55));
    tip.replaceChildren(el("strong", { text: `${money(h.edges[i])} to ${money(h.edges[i + 1])}` }), el("div", { text: `${share[i].toFixed(1)}% of paths` }));
    tooltipAt(tip, box, X(h.edges[i]), W);
  });
  root.addEventListener("pointerleave", () => { bars.forEach(b => b.setAttribute("opacity", 1)); tip.style.display = "none"; });
  return el("div", { class: "chart chart-scroll" }, el("div", { class: "legend" },
    el("span", {}, el("i", { style: "background: var(--series-1)" }), "Finishes in the money"),
    el("span", {}, el("i", { style: "background: var(--muted); opacity: 0.35" }), "Finishes worthless")), root, tip);
}

function renderVolatility(p) {
  const frag = el("div");
  if (p.data_source === "demo") frag.append(banner("warn", "Demo data: these prices are made up."));
  const rv = p.realized || {}, rank = p.iv_rank || {};
  frag.append(el("div", { class: "metrics" },
    metric(ivPct(p.iv30), "Implied volatility, 30 days (at the money)"),
    metric(ivPct(rv.hv20), "Realized, last 20 trading days"),
    metric(points(p.iv_premium_points), "Implied minus realized"),
    metric(isNum(rank.rank) ? `${rank.rank.toFixed(0)} of 100` : "n/a", isNum(rank.rank) ? `IV rank over ${rank.days} recorded days${rank.fresh ? "" : " (not recorded today)"}` : "IV rank: not enough recorded days yet"),
    metric(p.skew ? points(p.skew.points) : "n/a", p.skew ? `Skew, 25-delta put minus call (${shortDate(p.skew.expiration)})` : "Skew"),
    metric(p.shape && p.shape.shape ? tagOf(SHAPE_TAG, p.shape.shape) : "n/a", "Term structure")));
  if (p.shape && p.shape.text) frag.append(el("p", { class: "small", text: p.shape.text }));
  if (p.skew) frag.append(el("p", { class: "small", text: p.skew.text }));
  const ev = p.event;
  if (p.earnings && p.earnings.date) {
    const when = `${shortDate(p.earnings.date)}${p.earnings.estimated ? " (estimated from past reports)" : ""}`;
    frag.append(ev && ev.available
      ? banner("info", `Earnings ${when}: option prices imply a move of about ±${ev.move_pct.toFixed(1)}%${isNum(ev.move_dollars) ? ` (±${money(ev.move_dollars)})` : ""} on the report, one standard deviation, measured from ${shortDate(ev.expiration)} (${ev.method}). ${ev.note}`)
      : el("p", { class: "small muted", text: `Earnings ${when}. Implied earnings move: not measured (${ev ? ev.reason : "no expiration spans it"}).` }));
  }
  frag.append(el("h4", { text: "Implied volatility by expiration" }), termChart(p));
  frag.append(table([
    { label: "Expiration", cell: r => el("span", {}, shortDate(r.expiration), r.earnings === "INCLUDES" ? el("span", { class: "small muted", text: " · includes earnings" }) : null) },
    { label: "Days", num: true, cell: r => String(r.dte) },
    { label: "At the money", num: true, cell: r => ivPct(r.atm_iv) },
    { label: "Expected move (1 sd)", num: true, cell: r => isNum(r.one_sd_move) ? `±${money(r.one_sd_move)} (${pct(r.one_sd_pct, 1)})` : "n/a" },
    { label: "Range", num: true, cell: r => isNum(r.range_low) ? `${money(r.range_low)} to ${money(r.range_high)}` : "n/a" },
    { label: "Straddle", num: true, cell: r => r.straddle ? `${money(r.straddle.price)} (${pct(r.straddle_pct, 1)})` : "n/a" },
    { label: "Skew", num: true, cell: r => isNum(r.skew_25d) ? points(100 * r.skew_25d) : "n/a" }], p.expirations));
  frag.append(el("p", { class: "small muted", text: "The expected move is one standard deviation of the stock price by expiration from the at-the-money volatility: about two times in three the stock ends inside the range, if option prices are right. The straddle (the at-the-money call plus put at their midpoints) is what the market charges for a move either way, about 0.8 of the standard deviation. Skew is the 25-delta put's volatility minus the 25-delta call's." }));
  if (p.failed && p.failed.length) frag.append(banner("warn", `Not read: ${p.failed.map(f => `${shortDate(f.expiration)} (${f.error})`).join("; ")}.`));
  return frag;
}

function renderMonteCarlo(m) {
  const frag = el("div");
  if (m.data_source === "demo") frag.append(banner("warn", "Demo data: these prices are made up."));
  const b = m.buyer;
  frag.append(el("div", { class: "metrics" },
    metric(money(m.price), `Simulated value (±${money(m.standard_error)} standard error)`),
    metric(money(m.black76), "Black-76 value from the same inputs"),
    metric(m.consistent ? statusTag("pass", "Agree") : statusTag("warn", "Differ"), m.consistent ? "Within three standard errors" : `Off by ${money(m.difference)}`),
    metric(pct(100 * m.p_finish_itm, 0), `Finishes in the money (formula ${pct(100 * m.p_finish_itm_formula, 0)})`),
    metric(pct(100 * m.p_touch_close, 0), "Closes at or past the strike on some day"),
    metric(isNum(m.p_touch_any_time) ? pct(100 * m.p_touch_any_time, 0) : "n/a", "Touches the strike at any moment")));
  if (b) frag.append(el("h4", { text: `Buying ${m.contracts || 1} at ${money(b.premium)} (${m.premium_source === "you" ? "your price" : "the midpoint"})` }),
    el("div", { class: "metrics" },
      metric(pct(100 * b.p_profit, 0), "Paths that end with a profit"),
      metric(signedMoney(b.pnl.median), "Middle outcome"),
      metric(`${signedMoney(b.pnl.p5)} to ${signedMoney(b.pnl.p95)}`, "Nine in ten outcomes between"),
      metric(signedMoney(b.model_edge_per_share * 100, 2), "Model value minus price, per contract")),
    el("p", { class: "small muted", text: b.note }));
  frag.append(el("h4", { text: "Where the stock ends" }), outcomeChart(m),
    el("p", { class: "small muted", text: `${num(m.paths, 0)} paths, ${m.steps} checks each, seed ${m.seed}: the same inputs always give the same result. Volatility ${ivPct(m.volatility)} (${m.volatility_source || "chain"}).${m.event ? ` An earnings jump of ±${m.event.move_pct.toFixed(1)}% is added on the report date.` : ""}` }),
    el("ul", { class: "plain small muted" }, m.notes.map(t => el("li", { text: t }))));
  return frag;
}

function buildOptionsLab() {
  const sym = el("input", { class: "sym", type: "text", value: "AAPL", "aria-label": "Ticker", autocomplete: "off", spellcheck: "false" });
  const go = el("button", { class: "primary", text: "Load" });
  const error = el("div", { class: "error", role: "alert" });
  const volBox = el("div");
  const exp = el("select", { "aria-label": "Expiration" });
  const kind = segmented("mckind", [["call", "Call"], ["put", "Put"]], "call");
  const strike = el("select", { "aria-label": "Strike" });
  const premium = numInput({ placeholder: "mid", "aria-label": "Price per share" });
  const qty = el("input", { type: "number", min: 1, max: 500, step: 1, value: 1, "aria-label": "Contracts" });
  const jump = el("input", { type: "checkbox", "aria-label": "Add the implied earnings move" });
  const run = el("button", { class: "primary", text: "Simulate 20,000 paths" });
  const mcError = el("div", { class: "error", role: "alert" });
  const mcBox = el("div");
  const mcPanel = el("div", { class: "panel hidden" }, el("h3", { text: "Monte Carlo check of one contract" }),
    el("p", { class: "small muted", text: "Simulates the stock with the chain's own volatility and forward, then values the option on those paths. The value should match the formula; the simulation adds the chance of touching the strike and the spread of outcomes for a buyer." }),
    el("div", { class: "inline-form" }, field("Expiration", exp), field("Type", kind), field("Strike", strike), field("Price you would pay", premium),
      field("Contracts", qty), el("label", { class: "check" }, jump, " Add the implied earnings move"), run), mcError, mcBox);
  let loaded = null;

  async function fillStrikes() {
    strike.replaceChildren(el("option", { text: "Loading…" }));
    const chain = await api(`/api/chain?symbol=${encodeURIComponent(loaded.symbol)}&expiration=${exp.value}&width_pct=40`);
    const rows = chain.contracts.filter(c => c.option_type === kind.value());
    strike.replaceChildren(...rows.map(c => el("option", { value: c.strike, text: `${num(c.strike, c.strike % 1 ? 2 : 0)}  ${isNum(c.bid) ? `bid ${num(c.bid)} / ask ${num(c.ask)}` : "no quote"}` })));
    const near = rows.reduce((a, c) => Math.abs(c.strike - chain.spot) < Math.abs(a.strike - chain.spot) ? c : a, rows[0]);
    if (near) strike.value = near.strike;
  }
  async function load() {
    error.textContent = ""; volBox.classList.add("refetching");
    try {
      const symbol = sym.value.trim().toUpperCase();
      if (!symbol) throw new Error("Enter a ticker.");
      const p = await api(`/v2/options/${encodeURIComponent(symbol)}/volatility`);
      loaded = p; sym.value = p.symbol;
      volBox.replaceChildren(renderVolatility(p));
      exp.replaceChildren(...p.expirations.map(r => el("option", { value: r.expiration, text: `${shortDate(r.expiration)} (${r.dte} days)` })));
      const pick = p.expirations.find(r => r.dte >= 25) || p.expirations[p.expirations.length - 1];
      exp.value = pick.expiration; mcBox.replaceChildren(); mcPanel.classList.remove("hidden");
      await fillStrikes();
    } catch (e) { error.textContent = e.message; }
    finally { volBox.classList.remove("refetching"); }
  }
  run.addEventListener("click", () => withBusy(run, mcError, async () => {
    if (!loaded) throw new Error("Load a ticker first.");
    const body = { symbol: loaded.symbol, expiration: exp.value, option_type: kind.value(), strike: Number(strike.value),
      premium: optNum(premium), contracts: Math.max(1, Math.round(Number(qty.value) || 1)), earnings_jump: jump.checked };
    mcBox.classList.add("refetching");
    try { mcBox.replaceChildren(renderMonteCarlo({ ...(await api("/v2/options/montecarlo", body)), contracts: body.contracts })); }
    finally { mcBox.classList.remove("refetching"); }
  }));
  exp.addEventListener("change", () => fillStrikes().catch(e => { mcError.textContent = e.message; }));
  kind.addEventListener("change", () => fillStrikes().catch(e => { mcError.textContent = e.message; }));
  go.addEventListener("click", load);
  sym.addEventListener("keydown", e => { if (e.key === "Enter") load(); });
  const view = el("section", {}, el("h2", { text: "Options lab" }),
    el("p", { class: "lede", text: "What option prices say about a stock: implied volatility across expirations and strikes, the move they price in, earnings, how implied compares with what the stock has actually done, and a simulation of any contract. These are what the market implies under a model, not forecasts." }),
    el("div", { class: "panel" }, el("div", { class: "row" }, field("Ticker", sym), go), error),
    el("div", { class: "panel" }, volBox), mcPanel);
  view.onShow = () => {
    if (LAB_NEXT.symbol) { sym.value = LAB_NEXT.symbol; LAB_NEXT.symbol = null; load(); }
    else if (!loaded) load();
  };
  return view;
}

/* ---------- scanner: technical signals and options activity ---------- */
const bigMoney = v => {
  if (!isNum(v)) return "n/a";
  const a = Math.abs(v);
  return a >= 1e9 ? `$${(v / 1e9).toFixed(1)}B` : a >= 1e6 ? `$${(v / 1e6).toFixed(1)}M` : a >= 1e3 ? `$${(v / 1e3).toFixed(0)}K` : money(v, 0);
};
const ratioText = v => isNum(v) ? v.toFixed(2) : "n/a";
const LEAN_TAG = { CALL_HEAVY: ["info", "Call-heavy"], PUT_HEAVY: ["info", "Put-heavy"], BALANCED: ["info", "Balanced"] };
const RSI_WORDS = { OVERSOLD: "oversold", OVERBOUGHT: "overbought", NEUTRAL: "" };
const MACD_WORDS = { ABOVE_SIGNAL: "Above signal", BELOW_SIGNAL: "Below signal" };
const AVERAGE_WORDS = { ABOVE_BOTH: "Above both", BELOW_BOTH: "Below both", MIXED: "Between" };
const WHEN_WORDS = n => n === 0 ? "last bar" : n === 1 ? "1 bar ago" : `${n} bars ago`;

function openStock(symbol) {
  if (BUILDERS.analyze) { show("analyze"); state.views.analyze.analyze(symbol); }
  else openInLab(symbol);
}

function signalList(row) {
  if (!row.signals.length) return el("span", { class: "muted", text: "none" });
  return el("ul", { class: "signal-list" }, row.signals.map(s => el("li", {},
    el("span", { class: s.direction > 0 ? "dir up" : "dir down", "aria-label": s.direction > 0 ? "expects a rise" : "expects a fall", text: s.direction > 0 ? "▲" : "▼" }),
    " ", el("strong", { text: s.label }), s.status === "PROVISIONAL" ? el("span", {}, " ", statusTag("warn", "Provisional")) : null,
    el("div", { class: "small muted" }, WHEN_WORDS(s.bars_ago),
      s.past ? el("span", {}, " · past cases ", freqText(s.past)) : null,
      s.lab && s.lab.length ? ` · lab: ${s.lab.map(v => `${v.horizon}d ${VERDICT_WORDS[v.verdict] || v.verdict}`).join(", ")}` : null))));
}

function renderTechnicalScan(d, reload) {
  const frag = el("div");
  for (const n of d.notes) frag.append(banner(n.startsWith("Demo") ? "warn" : "info", n));
  if (!d.history_built) frag.append(buildHistoryButton(reload));
  frag.append(el("p", { class: "small", text: `${d.matched} of ${d.scanned} stocks shown · signals from the last ${d.lookback} bar${d.lookback === 1 ? "" : "s"} · checked ${new Date(d.checked_at).toLocaleTimeString("en-US", { hour: "numeric", minute: "2-digit" })}. Select a row to analyze the stock.` }));
  if (d.errors.length) frag.append(banner("warn", `Not scanned: ${d.errors.map(e => `${e.symbol} (${e.error})`).join("; ")}.`));
  if (!d.rows.length) { frag.append(el("p", { class: "muted", text: "No stock matches these filters." })); return frag; }
  // Each stock is a row of readings with its signals on a full-width line underneath.
  const cols = [
    { label: "Stock", cell: r => el("strong", { text: r.symbol }) },
    { label: "Close", num: true, cell: r => el("span", {}, money(r.close), r.bar_finished ? null : el("span", { class: "small muted", text: " so far" })) },
    { label: "Day", num: true, cell: r => signedPct(r.change_pct, 1) },
    { label: "RSI", num: true, cell: r => isNum(r.rsi) ? `${r.rsi.toFixed(0)}${RSI_WORDS[r.rsi_zone] ? ` ${RSI_WORDS[r.rsi_zone]}` : ""}` : "n/a" },
    { label: "MACD", cell: r => MACD_WORDS[r.macd] || "n/a" },
    { label: "50 & 200-day avg", cell: r => AVERAGE_WORDS[r.averages] || "n/a" },
    { label: "52-week range", num: true, cell: r => isNum(r.range_52w_pct) ? `${r.range_52w_pct.toFixed(0)}%` : "n/a" },
    { label: "Volume vs 20-day", num: true, cell: r => isNum(r.volume_ratio) ? el("span", {}, `${r.volume_ratio.toFixed(1)}×`, r.volume_spike ? el("span", {}, " ", statusTag("info", "Spike")) : null) : "n/a" }];
  const tbody = el("tbody");
  for (const r of d.rows) {
    const open = () => openStock(r.symbol);
    const main = el("tr", { class: "click", tabindex: "0" }, cols.map(c => el("td", { class: c.num ? "num" : null }, c.cell(r))));
    const sub = el("tr", { class: "click sub" }, el("td", { colspan: cols.length }, signalList(r)));
    for (const tr of [main, sub]) tr.addEventListener("click", open);
    main.addEventListener("keydown", e => { if (e.key === "Enter") open(); });
    tbody.append(main, sub);
  }
  frag.append(el("div", { class: "scroll scan-table tech" }, el("table", {},
    el("thead", {}, el("tr", {}, cols.map(c => el("th", { class: c.num ? "num" : null, text: c.label })))), tbody)));
  frag.append(el("p", { class: "small muted", text: "RSI is the 14-day relative strength index (30 or under oversold, 70 or over overbought). MACD is 12/26 against its 9-day signal line. The 52-week range is where the close sits between the year's low (0%) and high (100%). A volume spike is at least twice the 20-day average on a finished bar." }));
  return frag;
}

function renderFlowScan(d) {
  const frag = el("div");
  for (const n of d.notes) frag.append(banner(n.startsWith("Demo") ? "warn" : "info", n));
  if (d.errors.length) frag.append(banner("warn", `Not scanned: ${d.errors.map(e => `${e.symbol} (${e.error})`).join("; ")}.`));
  const st = d.settings;
  frag.append(el("p", { class: "small", text: `${d.rows.length} stocks · the nearest ${st.expirations} expiration${st.expirations === 1 ? "" : "s"} within ${st.max_dte} days · unusual means at least ${num(st.min_volume, 0)} contracts traded and volume at least ${st.min_vol_oi}× open interest. Select a row to open the stock in the options lab.` }));
  frag.append(el("h4", { text: "Calls against puts" }), table([
    { label: "Stock", cell: r => el("strong", { text: r.symbol }) },
    { label: "Calls", num: true, cell: r => num(r.call_volume, 0) },
    { label: "Puts", num: true, cell: r => num(r.put_volume, 0) },
    { label: "P/C volume", num: true, cell: r => ratioText(r.put_call_volume) },
    { label: "P/C open int.", num: true, cell: r => ratioText(r.put_call_open_interest) },
    { label: "Call premium", num: true, cell: r => bigMoney(r.call_premium) },
    { label: "Put premium", num: true, cell: r => bigMoney(r.put_premium) },
    { label: "Activity", cell: r => r.lean ? tagOf(LEAN_TAG, r.lean) : "n/a" },
    { label: "Unusual", num: true, cell: r => String(r.unusual_count) }], d.rows, { onRow: r => openInLab(r.symbol) }));
  frag.lastChild.classList.add("scan-table");
  frag.append(el("h4", { text: `Unusually active contracts${d.unusual_total > d.unusual.length ? ` (largest ${d.unusual.length} of ${d.unusual_total})` : ""}` }));
  if (!d.unusual.length) frag.append(el("p", { class: "muted", text: "None at these thresholds." }));
  else frag.append(table([
    { label: "Stock", cell: u => el("strong", { text: u.symbol }) },
    { label: "Contract", cell: u => `${shortDate(u.expiration)} ${num(u.strike, u.strike % 1 ? 2 : 0)} ${u.option_type}` },
    { label: "Days", num: true, cell: u => String(u.dte) },
    { label: "Volume", num: true, cell: u => num(u.volume, 0) },
    { label: "Open int.", num: true, cell: u => num(u.open_interest, 0) },
    { label: "Volume / OI", num: true, cell: u => isNum(u.volume_to_oi) ? `${u.volume_to_oi.toFixed(1)}×` : "new" },
    { label: "Premium", num: true, cell: u => bigMoney(u.premium) },
    { label: "Strike vs price", num: true, cell: u => signedPct(u.moneyness_pct, 1) },
    { label: "Delta", num: true, cell: u => isNum(u.delta) ? u.delta.toFixed(2) : "n/a" },
    { label: "IV", num: true, cell: u => ivPct(u.iv) }], d.unusual, { onRow: u => openInLab(u.symbol) }));
  if (d.unusual.length) frag.lastChild.classList.add("scan-table");
  frag.append(el("p", { class: "small muted", text: "Premium traded is volume times the bid/ask midpoint times 100, so it is approximate. P/C is puts divided by calls: above 1 means more puts than calls. Activity is call-heavy when calls take 65% or more of the premium traded, put-heavy at 35% or less." }));
  return frag;
}

function buildScanner() {
  const tab = segmented("scantab", [["tech", "Technical signals"], ["flow", "Options activity"]], "tech");
  const watchCount = () => ((state.status && state.status.watchlist) || []).length;
  // technical
  const uni = el("select", { "aria-label": "Stocks to scan" }, el("option", { value: "watchlist", text: "Watchlist" }),
    el("option", { value: "large_caps", text: "100 large US companies" }), el("option", { value: "custom", text: "These tickers" }));
  const syms = el("input", { type: "text", placeholder: "AAPL, MSFT, NVDA", autocomplete: "off", spellcheck: "false", "aria-label": "Tickers" });
  const symsField = field("Tickers", syms); symsField.classList.add("hidden");
  const look = el("select", { "aria-label": "Signals within" }, [1, 3, 5, 10].map(n => el("option", { value: n, text: n === 1 ? "the last bar" : `the last ${n} bars`, selected: n === 5 })));
  const dir = el("select", { "aria-label": "Direction" }, el("option", { value: "", text: "Any" }), el("option", { value: "bullish", text: "Expects a rise" }), el("option", { value: "bearish", text: "Expects a fall" }));
  const rsiSel = el("select", { "aria-label": "RSI" }, el("option", { value: "", text: "Any" }), el("option", { value: "oversold", text: "Oversold (30 or under)" }), el("option", { value: "overbought", text: "Overbought (70 or over)" }));
  const sig = el("select", { "aria-label": "Signal" }, el("option", { value: "", text: "Any signal" }));
  const spike = el("input", { type: "checkbox" });
  const onlySig = el("input", { type: "checkbox" });
  const runTech = el("button", { class: "primary", text: "Scan" });
  const techErr = el("div", { class: "error", role: "alert" });
  const techOut = el("div");
  const techPanel = el("div", {},
    el("div", { class: "panel" }, el("div", { class: "inline-form" }, field("Stocks", uni), symsField, field("Signals within", look),
      field("Direction", dir), field("RSI", rsiSel), field("Signal", sig),
      el("label", { class: "check" }, spike, " Volume at least twice its average"),
      el("label", { class: "check" }, onlySig, " Only stocks with a signal"), runTech), techErr),
    el("div", { class: "panel" }, techOut));
  uni.addEventListener("change", () => symsField.classList.toggle("hidden", uni.value !== "custom"));
  const techQuery = () => {
    const q = new URLSearchParams({ universe: uni.value === "custom" ? "watchlist" : uni.value, lookback: look.value });
    if (uni.value === "custom") { if (!syms.value.trim()) throw new Error("Enter at least one ticker."); q.set("symbols", syms.value); }
    if (dir.value) q.set("direction", dir.value);
    if (rsiSel.value) q.set("rsi", rsiSel.value);
    if (sig.value) q.set("signal", sig.value);
    if (spike.checked) q.set("min_volume_ratio", "2");
    if (onlySig.checked) q.set("only_signals", "true");
    return q;
  };
  const scanTech = () => withBusy(runTech, techErr, async () => {
    techOut.classList.add("refetching");
    try {
      const d = await api(`/v2/scan/technical?${techQuery()}`);
      if (sig.options.length === 1) sig.append(...d.signals.map(s => el("option", { value: s.key, text: `${s.direction > 0 ? "▲" : "▼"} ${s.label}` })));
      techOut.replaceChildren(renderTechnicalScan(d, scanTech));
    } finally { techOut.classList.remove("refetching"); }
  });
  runTech.addEventListener("click", scanTech);
  // options activity
  const fUni = el("select", { "aria-label": "Stocks to scan" }, el("option", { value: "watchlist", text: "Watchlist" }), el("option", { value: "custom", text: "These tickers" }));
  const fSyms = el("input", { type: "text", placeholder: "AAPL, MSFT, NVDA", autocomplete: "off", spellcheck: "false", "aria-label": "Tickers" });
  const fSymsField = field("Tickers", fSyms); fSymsField.classList.add("hidden");
  const maxDte = el("select", { "aria-label": "Expirations within" }, [14, 30, 60, 90].map(n => el("option", { value: n, text: `${n} days`, selected: n === 60 })));
  const nExp = el("select", { "aria-label": "Expirations" }, [1, 2, 3, 4, 6].map(n => el("option", { value: n, text: String(n), selected: n === 3 })));
  const minVol = el("input", { type: "number", min: 1, step: 50, value: 250, "aria-label": "Minimum volume" });
  const minRatio = el("input", { type: "number", min: 0, step: 0.5, value: 1, "aria-label": "Volume to open interest" });
  const runFlow = el("button", { class: "primary", text: "Scan options" });
  const flowErr = el("div", { class: "error", role: "alert" });
  const flowOut = el("div");
  const flowPanel = el("div", { class: "hidden" },
    el("div", { class: "panel" }, el("div", { class: "inline-form" }, field("Stocks", fUni), fSymsField, field("Expirations within", maxDte),
      field("How many", nExp), field("At least this volume", minVol), field("Volume / open interest", minRatio), runFlow), flowErr,
      el("p", { class: "small muted", text: "Reads up to 30 stocks, each with its nearest expirations: about one request per expiration." })),
    el("div", { class: "panel" }, flowOut));
  fUni.addEventListener("change", () => fSymsField.classList.toggle("hidden", fUni.value !== "custom"));
  const scanFlow = () => withBusy(runFlow, flowErr, async () => {
    const q = new URLSearchParams({ universe: "watchlist", max_dte: maxDte.value, expirations: nExp.value,
      min_volume: Math.max(1, Math.round(Number(minVol.value) || 250)), min_vol_oi: Math.max(0, Number(minRatio.value) || 0) });
    if (fUni.value === "custom") { if (!fSyms.value.trim()) throw new Error("Enter at least one ticker."); q.set("symbols", fSyms.value); }
    flowOut.classList.add("refetching");
    try { flowOut.replaceChildren(renderFlowScan(await api(`/v2/scan/flow?${q}`))); }
    finally { flowOut.classList.remove("refetching"); }
  });
  runFlow.addEventListener("click", scanFlow);
  let flowRan = false;
  tab.addEventListener("change", () => {
    const flow = tab.value() === "flow";
    techPanel.classList.toggle("hidden", flow); flowPanel.classList.toggle("hidden", !flow);
    if (flow && !flowRan) { flowRan = true; scanFlow(); }
  });
  const view = el("section", {}, el("h2", { text: "Scanner" }),
    el("p", { class: "lede", text: "Technical signals across many stocks, each with how the same signal turned out before, and options trading unusually heavily against its open interest. Past cases are historical frequencies, not forecasts; options volume does not show who bought and who sold." }),
    el("div", { class: "row" }, tab), techPanel, flowPanel);
  let ran = false;
  view.onShow = () => {
    uni.options[0].textContent = `Watchlist (${watchCount()})`; fUni.options[0].textContent = `Watchlist (${watchCount()})`;
    if (!ran) { ran = true; scanTech(); }
  };
  return view;
}

/* ---------- strategy builder, on the Simulate screen ---------- */
const GROUP_WORDS = { bullish: "Expect a rise", bearish: "Expect a fall", neutral: "Expect a range", volatile: "Expect a big move", time: "Time decay" };
const ROLE_SIDE = { buy: "Buy", sell: "Sell" };

function renderStrategy(r, parsed, actions) {
  const frag = el("div");
  for (const n of r.notes) frag.append(banner(n.startsWith("Demo") ? "warn" : "info", n));
  if (parsed) frag.append(el("p", { class: "small" }, el("strong", { text: "Read as: " }), parsed.understood.join(", ") || "nothing specific",
    parsed.assumed.length ? el("span", { class: "muted", text: ` · assumed: ${parsed.assumed.join(", ")}` }) : null));
  const credit = r.net_mid < 0;
  const rc = r.risk_check;
  frag.append(...[el("h4", { text: `${r.label}: ${r.symbol} ${shortDate(r.expiration)}${r.far_expiration ? ` / ${shortDate(r.far_expiration)}` : ""} (${r.dte} days)` }),
    el("p", { class: "small muted", text: r.description }),
    table([
      { label: "Leg", cell: l => l.role },
      { label: "Action", cell: l => `${ROLE_SIDE[l.side]} ${l.contracts}` },
      { label: "Contract", cell: l => `${shortDate(l.expiration)} ${num(l.strike, l.strike % 1 ? 2 : 0)} ${l.option_type}` },
      { label: "Bid / ask", num: true, cell: l => `${num(l.bid)} / ${num(l.ask)}` },
      { label: "Midpoint", num: true, cell: l => num(l.mid) },
      { label: "Delta", num: true, cell: l => isNum(l.delta) ? l.delta.toFixed(2) : "n/a" }], r.legs),
    el("div", { class: "metrics" },
      metric(money(Math.abs(r.net_mid), 0), `${credit ? "Credit received" : "Debit paid"} at the midpoints (${money(Math.abs(r.net_natural), 0)} at the bid and ask)`),
      metric(r.unlimited_gain ? "No limit" : money(r.max_gain, 0), "Most it can make at expiration"),
      metric(r.unlimited_loss ? "No limit" : money(isNum(r.max_loss) ? -r.max_loss : null, 0), "Most it can lose at expiration"),
      metric(r.breakevens.length ? r.breakevens.map(b => money(b)).join(" and ") : "none", "Breakeven at expiration"),
      metric(pct(100 * r.p_profit_at_expiry, 0), "Chance of a profit at expiration, as option prices imply"),
      metric(rc.status === "pass" ? statusTag("pass", "Within your limit") : rc.status === "fail" ? statusTag("fail", "Over your limit") : statusTag("info", "Not measured"), rc.text)),
    r.exact ? null : el("p", { class: "small muted", text: "The legs expire on different days: the most it can make and lose are read from the model at the first expiration, not worked out exactly." }),
    el("ul", { class: "plain small muted" }, r.choices.map(c => el("li", { text: c })))].filter(Boolean));
  frag.append(actions);
  return frag;
}

function strategyPanel(view) {
  const symInput = view.querySelector("input.sym");
  const text = el("input", { type: "text", style: "width: 100%", placeholder: "AAPL iron condor 45 days 16 delta, 5 wide", autocomplete: "off", "aria-label": "Describe the trade" });
  const read = el("button", { class: "primary", text: "Build it" });
  const tpl = el("select", { "aria-label": "Strategy" }, el("option", { text: "Loading…" }));
  const exp = el("select", { "aria-label": "Expiration" }, el("option", { value: "", text: "Automatic" }));
  const delta = el("input", { type: "number", min: 2, max: 98, step: 1, placeholder: "auto", "aria-label": "Delta of the main strike" });
  const width = el("input", { type: "number", min: 0.5, step: 0.5, placeholder: "auto", "aria-label": "Width in dollars" });
  const center = el("input", { type: "number", min: 0.01, step: 0.5, placeholder: "auto", "aria-label": "Middle strike" });
  const qty = el("input", { type: "number", min: 1, max: 100, step: 1, value: 1, "aria-label": "How many" });
  const build = el("button", { class: "plain", text: "Build" });
  const fDelta = field("Delta (×100)", delta), fWidth = field("Wing width, $", width), fCenter = field("Middle strike", center);
  const error = el("div", { class: "error", role: "alert" });
  const out = el("div");
  let templates = [], expFor = "";
  const shownFor = () => templates.find(t => t.key === tpl.value);
  const syncFields = () => {
    const t = shownFor(); if (!t) return;
    fDelta.classList.toggle("hidden", !t.uses_delta); fWidth.classList.toggle("hidden", !t.has_width);
    fCenter.classList.toggle("hidden", !(t.key.includes("butterfly") || t.key.includes("straddle") || t.calendar || t.key.startsWith("long_c") || t.key.startsWith("long_p")));
    delta.placeholder = t.defaults.delta ? String(Math.round(100 * t.defaults.delta)) : "auto";
  };
  async function loadExpirations() {
    const symbol = symInput.value.trim().toUpperCase();
    if (!symbol || symbol === expFor) return;
    const d = await api(`/api/expirations?symbol=${encodeURIComponent(symbol)}`);
    expFor = d.symbol;
    exp.replaceChildren(el("option", { value: "", text: "Automatic" }), ...d.expirations.filter(e => e.dte >= 1).map(e => el("option", { value: e.date, text: `${shortDate(e.date)} (${e.dte} days)` })));
  }
  const actions = r => {
    const t = templates.find(x => x.key === r.strategy) || {};
    const move = (op, label) => { const b = el("button", { class: "plain small-btn", text: label }); b.addEventListener("click", () => send({ ...r.request, adjust: { op } }, b)); return b; };
    const sim = el("button", { class: "primary", text: "Simulate this" });
    sim.addEventListener("click", () => view.loadPosition(r.symbol, r.legs.map(l => ({ expiration: l.expiration, option_type: l.option_type, strike: l.strike,
      side: l.side, contracts: l.contracts, entry_price: l.mid, entry_source: "midpoint" }))));
    return el("div", { class: "row" }, sim, move("down", "Strikes down"), move("up", "Strikes up"),
      t.has_wings ? move("narrower", "Narrower") : null, t.has_wings ? move("wider", "Wider") : null,
      el("span", { class: "small muted", text: "Modelled only: the app never places orders." }));
  };
  async function send(body, button, parsed) {
    return withBusy(button, error, async () => {
      out.classList.add("refetching");
      try {
        const r = parsed === undefined ? await api("/v2/strategies/build", body) : body;
        out.replaceChildren(renderStrategy(r, parsed, actions(r)));
      } finally { out.classList.remove("refetching"); }
    });
  }
  build.addEventListener("click", () => {
    const symbol = symInput.value.trim().toUpperCase();
    if (!symbol) { error.textContent = "Enter a ticker above."; return; }
    const body = { symbol, strategy: tpl.value, contracts: Math.max(1, Math.round(Number(qty.value) || 1)) };
    if (exp.value) body.expiration = exp.value;
    if (delta.value && !fDelta.classList.contains("hidden")) body.delta = Number(delta.value);
    if (width.value && !fWidth.classList.contains("hidden")) body.width = Number(width.value);
    if (center.value && !fCenter.classList.contains("hidden")) body.center = Number(center.value);
    send(body, build);
  });
  const describe = () => withBusy(read, error, async () => {
    if (!text.value.trim()) throw new Error("Describe the trade first.");
    out.classList.add("refetching");
    try {
      const d = await api("/v2/strategies/parse", { text: text.value, symbol: symInput.value.trim() || null });
      if (d.result.symbol !== symInput.value.trim().toUpperCase()) { symInput.value = d.result.symbol; symInput.dispatchEvent(new Event("change")); }
      out.replaceChildren(renderStrategy(d.result, d.parsed, actions(d.result)));
    } finally { out.classList.remove("refetching"); }
  });
  read.addEventListener("click", describe);
  text.addEventListener("keydown", e => { if (e.key === "Enter") describe(); });
  tpl.addEventListener("change", syncFields);
  symInput.addEventListener("change", () => loadExpirations().catch(() => {}));
  api("/v2/strategies").then(d => {
    templates = d.templates;
    const groups = {};
    for (const t of templates) (groups[t.group] = groups[t.group] || []).push(t);
    tpl.replaceChildren(...Object.entries(groups).map(([g, ts]) => el("optgroup", { label: GROUP_WORDS[g] || g },
      ts.map(t => el("option", { value: t.key, text: `${t.label}${t.defined_risk ? "" : " (loss not limited)"}`, selected: t.key === "iron_condor" })))));
    syncFields();
  }).catch(e => { error.textContent = e.message; });
  setTimeout(() => loadExpirations().catch(() => {}), 0);
  return el("div", { class: "panel" }, el("h3", { text: "Build a strategy" }),
    el("p", { class: "small muted", text: "Describe a trade in plain words, or pick a strategy. The legs are real listed contracts, picked by delta and width from the chain, and can be loaded into the simulator below." }),
    el("div", { class: "row" }, (() => { const f = field("Describe the trade", text); f.classList.add("grow"); return f; })(), read),
    el("div", { class: "inline-form" }, field("Or pick a strategy", tpl), field("Expiration", exp), fDelta, fWidth, fCenter, field("How many", qty), build),
    error, out);
}

if (BUILDERS.simulate) {
  const classicSimulate = BUILDERS.simulate;
  BUILDERS.simulate = () => {
    const view = classicSimulate();
    if (featureOn("strategy_builder")) view.insertBefore(strategyPanel(view), view.children[2]);
    return view;
  };
}

Object.assign(BUILDERS, { lab: buildOptionsLab, scanner: buildScanner });
