"use strict";
/* Trade Assistant 2.4 screens: the options lab (volatility and Monte Carlo), the scanner, and the
   strategy builder that feeds the simulator. Loaded after app.js and v2.js, whose helpers it uses. */

const ivPct = v => isNum(v) ? `${(100 * v).toFixed(1)}%` : "n/a";
const points = v => isNum(v) ? `${v > 0 ? "+" : ""}${v.toFixed(1)} pts` : "n/a";
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
  view.onShow = () => { if (!loaded) load(); };
  return view;
}

Object.assign(BUILDERS, { lab: buildOptionsLab });
