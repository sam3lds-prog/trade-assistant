"use strict";
/* Trade Assistant 2.5: the decision path. Today (where the market stands, how the day is going against your
   limits, which stocks lean which way) leads to Decide (Analyze a stock, with one thesis). Loaded last. */

/* ---------- view: today ---------- */
const SIDE_LEAN_WORDS = { LONG: "Leaning long", SHORT: "Leaning short" };

// One stock in a lean list: its trend, and its net score as a bar (length is the score's size, out of 100).
function leanTable(rows, side, historyBuilt) {
  const bar = r => el("span", { class: "lean-score", title: `Bullish ${num(r.bullish_score, 0)}, bearish ${num(r.bearish_score, 0)} evidence points` },
    el("span", { class: "score-track mini" }, el("i", { class: side === "LONG" ? "pos" : "neg", style: `width:${Math.min(100, Math.abs(r.net_score))}%` })),
    el("strong", { text: `${r.net_score > 0 ? "+" : ""}${num(r.net_score, 0)}` }));
  const wrap = table([
    { label: "Stock and trend", cell: r => el("div", {}, el("strong", { text: r.symbol }), el("div", { class: "small" }, trendTag(r.state)),
      r.reversal_unread ? el("div", { class: "small muted", text: "Reversal not checked" }) : null) },
    { label: "Net score", cell: bar },
    { label: "Day", num: true, cell: r => isNum(r.change_pct) ? signedPct(r.change_pct, 1) : null },
    // Once the signal history exists: how often stocks with a net score in this range were higher 10 days
    // later (either side; a past frequency, not a forecast). No column at all before that.
    ...(historyBuilt ? [{ label: "Higher 10 days later", num: true, cell: r => freqText(r.past_10d) }] : [])],
  rows, { onRow: r => state.openDecide(r.symbol, side) });
  wrap.classList.add("lean-table");
  if (historyBuilt) wrap.classList.add("with-past");
  return wrap;
}

function leanPanel(d, side) {
  const rows = side === "LONG" ? d.long : d.short, total = side === "LONG" ? d.long_total : d.short_total;
  const box = el("div", { class: "panel" }, el("h3", { text: `${SIDE_LEAN_WORDS[side]}${total > rows.length ? ` (top ${rows.length} of ${total})` : ""}` }));
  if (!rows.length) box.append(el("p", { class: "muted small", text: side === "LONG" ? "No stock leans long right now." : "No stock leans short right now." }));
  else box.append(leanTable(rows, side, d.history_built), el("p", { class: "small muted", text: `Select a stock to grade a ${side === "LONG" ? "long" : "short"} thesis and find ${side === "LONG" ? "calls" : "puts"} for it.` }));
  return box;
}

function renderCandidates(d, reload) {
  const frag = el("div");
  for (const n of d.notes) frag.append(banner(n.startsWith("Demo") ? "warn" : "info", n));
  frag.append(el("div", { class: d.history_built ? "lean-grid with-past" : "lean-grid" }, leanPanel(d, "LONG"), leanPanel(d, "SHORT")));
  const nl = d.no_lean, rest = el("div", { class: "panel" });
  const parts = [];
  if (nl.balanced) parts.push(`${nl.balanced} with evidence balanced`);
  if (nl.capped_long) parts.push(`${nl.capped_long} that would lean long, held back`);
  if (nl.capped_short) parts.push(`${nl.capped_short} that would lean short, held back`);
  rest.append(el("p", { class: "small" }, el("strong", { text: `No lean: ${nl.total} stock${nl.total === 1 ? "" : "s"}` }), parts.length ? ` (${parts.join("; ")}).` : "."));
  if (nl.symbols.length) rest.append(el("details", {}, el("summary", { class: "small", text: "Which were held back, and why" }),
    el("ul", { class: "plain small" }, nl.symbols.map(x => el("li", {}, el("strong", { text: `${x.symbol}: ` }), x.why)))));
  if (d.not_measured.length) rest.append(el("p", { class: "small", text: `Not measured (too little evidence to score): ${d.not_measured.join(", ")}.` }));
  if (d.errors.length) rest.append(el("p", { class: "small muted", text: `Not read: ${d.errors.map(e => `${e.symbol} (${e.error})`).join("; ")}.` }));
  if (!d.history_built && featureOn("signal_calibration")) {
    rest.append(el("p", { class: "small", text: "Past cases: the signal history has not been built, so there are no past frequencies to show yet." }), buildHistoryButton(reload));
  } else if (d.history_built) rest.append(el("p", { class: "small muted", text: `"Higher 10 days later" is how often stocks in the signal history (built ${shortDate(d.history_run_at)}) were higher 10 days after a day with a net score in the same range: a past frequency for longs and shorts alike, not a forecast.` }));
  rest.append(el("p", { class: "small muted", text: `${d.universe_text} ${d.rule}` }));
  frag.append(rest);
  return frag;
}

function buildToday() {
  const regimeBox = el("div", {}, el("p", { class: "small muted", text: "Loading…" }));
  const limitsBox = el("div", {}, el("p", { class: "small muted", text: "Loading…" }));
  const uni = el("select", { "aria-label": "Stocks" }, el("option", { value: "watchlist", text: "Watchlist" }),
    el("option", { value: "large_caps", text: "100 large US companies" }));
  const go = el("button", { class: "plain", text: "Refresh" });
  const err = el("div", { class: "error", role: "alert" });
  const out = el("div", {}, el("div", { class: "loadnote small muted" }, el("span", { class: "spinner" }), el("span", { text: "Reading the trend of each stock…" })));

  async function loadRegime() {
    if (!featureOn("trend_engine_v2")) { regimeBox.replaceChildren(el("p", { class: "small muted", text: "The trend engine is switched off." })); return; }
    try {
      const g = await api("/v2/regime"), now = g.provisional || g.confirmed;
      const changed = g.provisional && g.provisional.regime !== g.confirmed.regime;
      regimeBox.replaceChildren(...[el("p", {}, regimeTag(now.regime), " ", statusWord(now.status), ` ${now.text}`),
        changed ? el("p", { class: "small muted", text: `On the last finished bar it was ${REGIME_WORDS[g.confirmed.regime].toLowerCase()}; the lists below use that.` }) : null].filter(Boolean));
    } catch (e) { regimeBox.replaceChildren(el("p", { class: "small error", text: `Market regime: ${e.message}` })); }
  }
  async function loadLimits() {
    if (!featureOn("portfolio_risk")) { limitsBox.replaceChildren(el("p", { class: "small muted", text: "Portfolio risk is switched off." })); return; }
    try {
      const r = await api("/v2/portfolio/risk"), dl = r.daily_loss, rc = r.risk_coverage;
      const lines = [];
      if (dl) {
        const cov = dl.coverage_status || "COMPLETE";
        lines.push(el("p", {}, el("strong", { text: "Daily stop: " }), tagOf(LOSS_TAG, dl.status), cov !== "COMPLETE" ? el("span", {}, " ", tagOf(COVERAGE_TAG, cov)) : null, ` ${dl.text}`));
      }
      if (rc) lines.push(el("p", {}, el("strong", { text: "Open risk: " }), rc.status !== "COMPLETE" ? el("span", {}, tagOf(COVERAGE_TAG, rc.status), " ") : null, rc.conclusion));
      for (const w of (r.warnings || []).filter(w => w.level === "fail" && w.code !== "DAILY_LOSS").slice(0, 2)) lines.push(banner("fail", w.text));
      limitsBox.replaceChildren(...lines);
    } catch (e) { limitsBox.replaceChildren(el("p", { class: "small error", text: `Your limits: ${e.message}` })); }
  }
  let ticket = 0;                       // the list on screen is always the one the menu names
  const loadLists = () => withBusy(go, err, async () => {
    const mine = ++ticket;
    out.classList.add("refetching");
    try {
      const d = await api(`/v2/today/candidates?universe=${uni.value}`);
      if (mine === ticket) out.replaceChildren(renderCandidates(d, loadLists));
    } catch (e) {
      if (mine !== ticket) return;
      out.replaceChildren();
      throw e;
    } finally { if (mine === ticket) out.classList.remove("refetching"); }
  });
  go.addEventListener("click", loadLists);
  uni.addEventListener("change", loadLists);
  const more = (name, text) => BUILDERS[name] ? el("button", { class: "plain small-btn", text, onclick: () => show(name) }) : null;
  const view = el("section", {}, el("h2", { text: "Today" }),
    el("p", { class: "lede", text: "Where the market stands, how your day is going against your limits, and which stocks lean long or short. Pick one to build a thesis." }),
    el("div", { class: "today-top" },
      el("div", { class: "panel" }, el("h3", { text: "Market" }), regimeBox, more("market", "Market & news")),
      el("div", { class: "panel" }, el("h3", { text: "Your limits" }), limitsBox, more("portfolio", "Portfolio risk"))),
    el("div", { class: "row", style: "margin:4px 0 12px" }, field("Stocks", uni), go, el("span", { class: "small muted", text: "The 100 large companies can take a minute the first time." })), err, out);
  let ran = false;
  view.onShow = () => {
    uni.options[0].textContent = `Watchlist (${((state.status && state.status.watchlist) || []).length})`;
    if (!ran) { ran = true; loadRegime(); loadLists(); }
    loadLimits();       // your day changes as you trade, so it is read each time Today opens
  };
  return view;
}

/* ---------- Decide: shares or a contract, side by side ---------- */
// Run by the thesis form on Analyze once the grade and the contracts are in (v2.js thesisSection).
const DECIDE_AFTER = [];

// The spread of outcomes on the horizon date that option prices imply (2.5.1): the profit or loss at the
// 10th, 50th and 90th percentile stock price of one distribution. The middle one, and the range nine in ten
// fall in. Any missing piece makes the whole band unknown, never $0.
function bandText(pnls) {
  if (!pnls || pnls.length !== 3 || !pnls.every(isNum)) return el("span", { class: "muted", text: "unknown" });
  const [a, mid, b] = pnls, lo = Math.min(a, b), hi = Math.max(a, b);
  return el("div", { class: "band-cell" }, el("strong", { text: signedMoney(mid, 0) }), el("span", { class: "muted", text: " middle outcome" }),
    el("div", { class: "small", text: `Nine in ten between ${signedMoney(lo, 0)} and ${signedMoney(hi, 0)}` }));
}

// A chance that rounds to 0% or 100% is still a measured one: say how small or large, not "0%".
const chanceText = p => p < 0.005 ? "under 1%" : p > 0.995 ? "over 99%" : pct(100 * p, 0);

function compareRows(dec) {
  const g = dec.grade, o = dec.options, m = o.market, lv = g.levels, entry = g.entry;
  const dir = g.direction === "LONG" ? 1 : -1, n = g.sizing.shares, date = shortDate(o.thesis.horizon_date);
  const rows = [];
  // The share trade has one price, the grade's entry (yours, or today's when the box was blank): every share
  // dollar and the chance of being ahead are measured from it (the option request carries a typed entry).
  const ahead = m && isNum(m.stock_profit_at_horizon) ? `${chanceText(m.stock_profit_at_horizon)} chance of a profit on ${date}` : null;
  const past = m && isNum(m.reach_target_1_by_horizon) ? `${chanceText(m.reach_target_1_by_horizon)} past target 1` : null;
  const pc = m && m.stock_percentiles_at_horizon;
  rows.push(n > 0 ? {
    kind: "shares", name: `${num(n, 0)} share${n === 1 ? "" : "s"} ${dir > 0 ? "long" : "short"} at ${money(entry)}`,
    sub: `Sized for ${money(g.sizing.risk_budget, 0)} at risk to the invalidation`,
    pay: n * entry, received: dir < 0, atTarget: dir * n * (lv.target_1 - entry), ifInvalid: dir * n * (lv.invalidation - entry),
    chance: ahead ? `${ahead}${past ? `; ${past}` : ""}` : null,
    band: pc && ["p10", "p50", "p90"].every(k => isNum(pc[k])) ? ["p10", "p50", "p90"].map(k => dir * n * (pc[k] - entry)) : null,
    line: p => dir * n * (p - entry),
  } : { kind: "shares", name: "Shares", sub: `No whole share fits the ${money(g.sizing.risk_budget, 0)} risk budget: ${money(g.sizing.risk_per_share)} at risk per share.`, empty: true });
  const pick = o.picks.find(x => x.category === "BEST RISK-ADJUSTED") || o.picks[0];
  if (pick) {
    const k = pick.contract, qty = o.thesis.contracts;
    rows.push({ kind: "contract", k, name: contractName(k), sub: `${qty} contract${qty === 1 ? "" : "s"} at the ${money(k.mid)} mid · the ${pick.category.toLowerCase()} pick below`,
      pay: k.premium, received: false, atTarget: k.at_target, ifInvalid: k.if_invalidated,
      chance: isNum(k.market_implied_profit_at_horizon) ? `${chanceText(k.market_implied_profit_at_horizon)} chance of a profit on ${date}` : null,
      band: [k.implied_pnl_p10_at_horizon, k.implied_pnl_p50_at_horizon, k.implied_pnl_p90_at_horizon] });
  }
  return rows;
}

// Profit or loss on the horizon date against the stock price then, for both rows on one dollar axis.
function compareChart(rows, sim, dec) {
  const g = dec.grade, o = dec.options, lv = g.levels;
  const xs = sim.curve.prices, n = xs.length, shares = rows.find(r => r.kind === "shares" && !r.empty);
  const series = [];
  if (shares) series.push({ name: "Shares", color: "--series-1", ys: xs.map(shares.line) });
  series.push({ name: "Contract", color: "--series-2", ys: sim.curve.pnl_at_date });
  const W = 760, H = 304, pad = { l: 72, r: 92, t: 40, b: 44 };
  const all = series.flatMap(s2 => s2.ys);
  const ys = niceScale(Math.min(0, ...all), Math.max(0, ...all), 5);
  const xlo = xs[0], xhi = xs[n - 1];
  const X = v => pad.l + (W - pad.l - pad.r) * (v - xlo) / (xhi - xlo);
  const Y = v => pad.t + (H - pad.t - pad.b) * (1 - (v - ys.lo) / (ys.hi - ys.lo));
  const root = svg("svg", { viewBox: `0 0 ${W} ${H}`, role: "img", "aria-label": `Profit or loss on ${shortDate(o.thesis.horizon_date)} of the shares and the contract against the stock price` });
  for (const v of ys.ticks) root.append(svg("line", { class: v === 0 ? "zero" : "grid", x1: pad.l, x2: W - pad.r, y1: Y(v), y2: Y(v) }),
    svg("text", { x: pad.l - 8, y: Y(v) + 4, "text-anchor": "end", text: money(v, 0) }));
  const xt = niceScale(xlo, xhi, 6);
  for (const v of xt.ticks) if (v >= xlo && v <= xhi) root.append(svg("text", { x: X(v), y: H - pad.b + 18, "text-anchor": "middle", text: money(v, xt.step < 1 ? 2 : 0) }));
  root.append(svg("text", { x: (pad.l + W - pad.r) / 2, y: H - 4, "text-anchor": "middle", text: `Stock price on ${shortDate(o.thesis.horizon_date)}` }));
  const marks = [[lv.invalidation, "Invalidation"], [o.thesis.spot, "Now"], [lv.target_1, "Target 1"]].filter(([v]) => isNum(v) && v > xlo && v < xhi)
    .sort((a, b) => a[0] - b[0]);
  let lastEnd = [-Infinity, -Infinity];               // right edge of the last label on each of two rows
  for (const [v, word] of marks) {
    const text = `${word} ${money(v, 0)}`, half = 3.4 * text.length, x = X(v);
    const row = x - half > lastEnd[0] + 6 ? 0 : 1;      // ~6.8 px a character at this size
    lastEnd[row] = x + half;
    root.append(svg("line", { class: word === "Now" ? "now-line" : "grid", x1: x, x2: x, y1: pad.t, y2: H - pad.b, "stroke-dasharray": word === "Now" ? null : "4 3" }),
      halo({ x, y: pad.t - 22 + 14 * row, "text-anchor": "middle", class: "lbl", text }));
  }
  const ends = [];
  for (const sr of series) {
    root.append(svg("path", { d: sr.ys.map((v, i) => `${i ? "L" : "M"}${X(xs[i]).toFixed(1)} ${Y(v).toFixed(1)}`).join(" "), fill: "none", stroke: `var(${sr.color})`, "stroke-width": 2, "stroke-linejoin": "round" }));
    ends.push({ text: sr.name, y: Y(sr.ys[n - 1]) + 4 });
  }
  for (const e of spreadLabels(ends, 13, pad.t + 4, H - pad.b)) root.append(svg("text", { x: W - pad.r + 8, y: e.y, text: e.text }));
  const hover = svg("line", { class: "hover-line", y1: pad.t, y2: H - pad.b, visibility: "hidden" });
  const dots = series.map(sr => svg("circle", { r: 4, fill: `var(${sr.color})`, stroke: "var(--panel)", "stroke-width": 2, visibility: "hidden" }));
  root.append(hover, ...dots);
  const tip = el("div", { class: "tip" });
  root.addEventListener("pointermove", evt => {
    const box = root.getBoundingClientRect();
    const px = (evt.clientX - box.left) * W / box.width;
    const i = Math.max(0, Math.min(n - 1, Math.round((px - pad.l) / (W - pad.l - pad.r) * (n - 1))));
    hover.setAttribute("x1", X(xs[i])); hover.setAttribute("x2", X(xs[i])); hover.setAttribute("visibility", "visible");
    series.forEach((sr, k) => { dots[k].setAttribute("cx", X(xs[i])); dots[k].setAttribute("cy", Y(sr.ys[i])); dots[k].setAttribute("visibility", "visible"); });
    tip.replaceChildren(el("div", { class: "tip-head", text: `Stock ${money(xs[i])} on ${shortDate(o.thesis.horizon_date)}` }),
      ...series.map(sr => tipRow(signedMoney(sr.ys[i], 0), sr.name.toLowerCase(), sr.color)));
    tooltipAt(tip, box, X(xs[i]), W);
  });
  root.addEventListener("pointerleave", () => { hover.setAttribute("visibility", "hidden"); dots.forEach(d => d.setAttribute("visibility", "hidden")); tip.style.display = "none"; });
  return el("div", { class: "chart" }, el("div", { class: "chart-scroll" }, root, tip),
    el("div", { class: "legend" }, series.map(sr => el("span", {}, el("i", { style: `background: var(${sr.color})` }), sr.name))));
}

function pastCasesLine(g, reload) {
  const head = el("strong", { text: "Past cases: " });
  if (g.history_built === null || g.history_built === undefined) return el("p", { class: "small" }, head, "the signal history is switched off here, so there are none to show.");
  if (!g.history_built) return el("div", {}, el("p", { class: "small" }, head, "the signal history has not been built, so there is no past frequency to show."), buildHistoryButton(reload));
  const hist = g.analogs && g.analogs.history;
  // The same-regime figure only when it has enough cases on its own, as in the grade; otherwise all stocks.
  const h = hist ? (hist.same_regime && hist.same_regime.enough ? hist.same_regime : hist.all) : null;
  if (!h || !h.cases || !isNum(h.frequency)) return el("p", { class: "small" }, head, "the trend has not changed recently, so there is no matching past signal to count.");
  return el("p", { class: "small" }, head, `when a stock's trend turned to ${trendWord(g.trend_state).toLowerCase()} before, it moved the expected way over 10 days in `,
    freqText(h), ` cases${h === hist.same_regime ? " in the same market regime" : ""}.`);
}

function comparePanel(dec, rerun) {
  const rows = compareRows(dec), o = dec.options, date = shortDate(o.thesis.horizon_date);
  const box = el("div", { class: "compare" }, el("h3", { text: "Shares or a contract" }),
    el("p", { class: "small muted", text: `Both on your levels, valued on ${date}. The chances and the outcomes are what option prices imply; past cases are counted apart. None of them is a forecast.` }));
  const actions = r => {
    if (r.kind !== "contract") return "";
    return limitButton(o.symbol, r.k, o.thesis.contracts);
  };
  box.append(table([
    { label: "", cell: r => el("div", {}, el("strong", { text: r.name }), el("div", { class: "small muted", text: r.sub })) },
    { label: "You pay", num: true, cell: r => r.empty ? "" : r.received ? el("span", {}, money(r.pay, 0), el("div", { class: "small muted", text: "received (short)" })) : money(r.pay, 0) },
    { label: "At target 1", num: true, cell: r => r.empty ? "" : signedMoney(r.atTarget, 0) },
    { label: "If invalidated", num: true, cell: r => r.empty ? "" : signedMoney(r.ifInvalid, 0) },
    { label: "Option prices imply", cell: r => r.empty ? "" : r.chance || el("span", { class: "muted", text: "unknown" }) },
    { label: `Outcome on ${date}, option prices imply`, cell: r => r.empty ? "" : bandText(r.band) },
    { label: "", cell: actions }], rows));
  const chart = el("div", {}, el("div", { class: "loadnote small muted" }, el("span", { class: "spinner" }), el("span", { text: "Drawing both on one chart…" })));
  box.append(chart);
  const c = rows.find(r => r.kind === "contract");
  if (c) {
    api("/api/simulate", { symbol: o.symbol, eval_date: o.thesis.horizon_date, legs: [{ expiration: c.k.expiration, option_type: c.k.option_type.toLowerCase(),
      strike: c.k.strike, side: "buy", contracts: o.thesis.contracts, entry_price: c.k.mid }] })
      .then(sim => chart.replaceChildren(compareChart(rows, sim, dec)))
      .catch(e => chart.replaceChildren(el("p", { class: "small error", text: `Chart: ${e.message}` })));
  } else chart.replaceChildren(el("p", { class: "small muted", text: "No contract passed the checks, so there is nothing to compare the shares with." }));
  const one = rows.filter(r => !r.empty);
  if (one.length === 2) {
    const [sh, ct] = one;
    box.append(el("p", {}, `The shares risk ${money(Math.abs(sh.ifInvalid), 0)} to make ${money(sh.atTarget, 0)} at target 1; the contract risks ${money(Math.abs(ct.ifInvalid), 0)} to make ${money(ct.atTarget, 0)} if the stock gets there by ${date}.`));
  }
  box.append(pastCasesLine(dec.grade, rerun));
  if (c && featureOn("options_lab")) {
    const mcBtn = el("button", { class: "plain", text: "Show the path range" });
    const mcErr = el("div", { class: "error", role: "alert" });
    const mcOut = el("div");
    mcBtn.addEventListener("click", () => withBusy(mcBtn, mcErr, async () => {
      const body = { symbol: o.symbol, expiration: c.k.expiration, option_type: c.k.option_type.toLowerCase(), strike: c.k.strike, contracts: o.thesis.contracts };
      mcOut.replaceChildren(el("h4", { text: "Model: simulated paths to expiration" }),
        el("p", { class: "small muted", text: "The same model as the option prices above, run as paths: it shows the spread of outcomes, not a second opinion on the chances." }),
        renderMonteCarlo({ ...(await api("/v2/options/montecarlo", body)), contracts: body.contracts }));
    }));
    box.append(el("div", { class: "row", style: "margin-top:8px" }, mcBtn), mcErr, mcOut);
  }
  // On average a contract is worth about what it costs, by construction; said once here, not as a column.
  const k = c && c.k;
  const avg = k && isNum(k.implied_value_at_horizon) ? ` Averaged over every outcome, option prices imply the ${contractName(k)} is worth ${money(k.implied_value_at_horizon, 0)} on ${date} against ${money(k.premium, 0)} paid:` : " Averaged over every outcome, a contract is worth";
  box.append(el("p", { class: "small muted", text: `The middle outcome is the 50th percentile, and nine in ten outcomes fall between the 10th and the 90th.${avg} about what you pay, by construction, since each contract is read on the volatility its own price implies. It is not an edge.` }));
  return box;
}

DECIDE_AFTER.push((out, dec) => {
  const card = out.firstChild;
  const rerun = () => { const go = out.parentNode && out.parentNode.querySelector("button.primary"); if (go) go.click(); };
  out.insertBefore(comparePanel(dec, rerun), card ? card.nextSibling : null);
});

// The trend chart above the thesis carries the plan once it is graded (2.5): levels, the range option prices
// imply by the horizon, and where the compared contract breaks even then.
DECIDE_AFTER.push((out, dec) => {
  const section = out.closest("section"), old = section && section.querySelector(".trend-chart");
  if (!old || !old.trend) return;
  const g = dec.grade, o = dec.options, m = o.market;
  const pick = o.picks.find(x => x.category === "BEST RISK-ADJUSTED") || o.picks[0];
  const k = pick && pick.contract;
  const plan = { horizonDate: o.thesis.horizon_date, horizonDays: dec.thesis.horizonDays,
    levels: [{ label: "Target 1", price: g.levels.target_1, color: "--div-pos" }, { label: "Target 2", price: g.levels.target_2, color: "--div-pos", dash: "5 4" },
      { label: "Invalidation", price: g.levels.invalidation, color: "--div-neg" }],
    band: m && m.range_1sd_at_horizon, atmExpiration: m && m.atm_expiration, breakeven: k && isNum(k.breakeven_at_horizon) ? { label: "Breakeven", legend: `Where the ${contractName(k)} breaks even then`, price: k.breakeven_at_horizon } : null };
  old.replaceWith(trendHistoryChart(old.trend, plan));
});

Object.assign(BUILDERS, { today: buildToday });
