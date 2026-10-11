"use strict";
/* Trade Assistant 2.5: the decision path. Today (where the market stands, how the day is going against your
   limits, which stocks lean which way) leads to Decide (Analyze a stock, with one thesis). Loaded last. */

/* ---------- view: today ---------- */
const SIDE_LEAN_WORDS = { LONG: "Leaning long", SHORT: "Leaning short" };

// One stock in a lean list: its trend, and its net score as a bar (length is the score's size, out of 100).
function leanTable(rows, side) {
  const bar = r => el("span", { class: "lean-score", title: `Bullish ${num(r.bullish_score, 0)}, bearish ${num(r.bearish_score, 0)} evidence points` },
    el("span", { class: "score-track mini" }, el("i", { class: side === "LONG" ? "pos" : "neg", style: `width:${Math.min(100, Math.abs(r.net_score))}%` })),
    el("strong", { text: `${r.net_score > 0 ? "+" : ""}${num(r.net_score, 0)}` }));
  const wrap = table([
    { label: "Stock and trend", cell: r => el("div", {}, el("strong", { text: r.symbol }), el("div", { class: "small" }, trendTag(r.state))) },
    { label: "Net score", cell: bar },
    { label: "Day", num: true, cell: r => isNum(r.change_pct) ? signedPct(r.change_pct, 1) : null }],
  rows, { onRow: r => state.openDecide(r.symbol, side) });
  wrap.classList.add("lean-table");
  return wrap;
}

function leanPanel(d, side) {
  const rows = side === "LONG" ? d.long : d.short, total = side === "LONG" ? d.long_total : d.short_total;
  const box = el("div", { class: "panel" }, el("h3", { text: `${SIDE_LEAN_WORDS[side]}${total > rows.length ? ` (top ${rows.length} of ${total})` : ""}` }));
  if (!rows.length) box.append(el("p", { class: "muted small", text: side === "LONG" ? "No stock leans long right now." : "No stock leans short right now." }));
  else box.append(leanTable(rows, side), el("p", { class: "small muted", text: `Select a stock to grade a ${side === "LONG" ? "long" : "short"} thesis and find ${side === "LONG" ? "calls" : "puts"} for it.` }));
  return box;
}

function renderCandidates(d, reload) {
  const frag = el("div");
  for (const n of d.notes) frag.append(banner(n.startsWith("Demo") ? "warn" : "info", n));
  frag.append(el("div", { class: "lean-grid" }, leanPanel(d, "LONG"), leanPanel(d, "SHORT")));
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
  } else if (d.history_built) rest.append(el("p", { class: "small muted", text: `Past cases come from the signal history built ${shortDate(d.history_run_at)}; Decide shows them for each stock.` }));
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
  const loadLists = () => withBusy(go, err, async () => {
    out.classList.add("refetching");
    try { out.replaceChildren(renderCandidates(await api(`/v2/today/candidates?universe=${uni.value}`), loadLists)); }
    finally { out.classList.remove("refetching"); }
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

// What option prices imply a position is worth in expectation, next to what it costs. `pnl` is that worth
// less the cost, from the whole distribution (server side); null is unknown, never $0.
function expectationText(pnl, paid, received) {
  if (!isNum(pnl) || !isNum(paid)) return el("span", { class: "muted", text: "unknown" });
  const worth = received ? paid - pnl : paid + pnl;
  const near = Math.abs(pnl) < Math.max(1, 0.01 * Math.abs(paid));
  const gap = near ? "about $0" : signedMoney(pnl, 0);
  return el("span", {}, received ? `${money(worth, 0)} to buy back against ${money(paid, 0)} received` : `${money(worth, 0)} against ${money(paid, 0)} paid`,
    el("span", { class: "muted", text: ` (${gap})` }));
}

// A chance that rounds to 0% or 100% is still a measured one: say how small or large, not "0%".
const chanceText = p => p < 0.005 ? "under 1%" : p > 0.995 ? "over 99%" : pct(100 * p, 0);

function compareRows(dec) {
  const g = dec.grade, o = dec.options, m = o.market, lv = g.levels, entry = g.entry;
  const dir = g.direction === "LONG" ? 1 : -1, n = g.sizing.shares, date = shortDate(o.thesis.horizon_date);
  const rows = [];
  const pastTarget = m && isNum(m.reach_target_1_by_horizon) ? `${chanceText(m.reach_target_1_by_horizon)} chance past target 1 on ${date}` : null;
  rows.push(n > 0 ? {
    kind: "shares", name: `${num(n, 0)} share${n === 1 ? "" : "s"} ${dir > 0 ? "long" : "short"} at ${money(entry)}`,
    sub: `Sized for ${money(g.sizing.risk_budget, 0)} at risk to the invalidation`,
    pay: n * entry, received: dir < 0, atTarget: dir * n * (lv.target_1 - entry), ifInvalid: dir * n * (lv.invalidation - entry),
    chance: pastTarget, ev: m && isNum(m.expected_price_at_horizon) ? dir * n * (m.expected_price_at_horizon - entry) : null,
    line: p => dir * n * (p - entry),
  } : { kind: "shares", name: "Shares", sub: `No whole share fits the ${money(g.sizing.risk_budget, 0)} risk budget: ${money(g.sizing.risk_per_share)} at risk per share.`, empty: true });
  const pick = o.picks.find(x => x.category === "BEST RISK-ADJUSTED") || o.picks[0];
  if (pick) {
    const k = pick.contract, qty = o.thesis.contracts;
    rows.push({ kind: "contract", k, name: contractName(k), sub: `${qty} contract${qty === 1 ? "" : "s"} at the ${money(k.mid)} mid · the ${pick.category.toLowerCase()} pick below`,
      pay: k.premium, received: false, atTarget: k.at_target, ifInvalid: k.if_invalidated,
      chance: isNum(k.market_implied_profit_at_horizon) ? `${chanceText(k.market_implied_profit_at_horizon)} chance of a profit on ${date}` : null,
      ev: k.implied_expected_pnl_at_horizon });
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
  const W = 760, H = 290, pad = { l: 72, r: 92, t: 26, b: 44 };
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
  const marks = [[lv.invalidation, "Invalidation"], [o.thesis.spot, "Now"], [lv.target_1, "Target 1"]].filter(([v]) => isNum(v) && v > xlo && v < xhi);
  for (const [v, word] of marks) root.append(svg("line", { class: word === "Now" ? "now-line" : "grid", x1: X(v), x2: X(v), y1: pad.t, y2: H - pad.b, "stroke-dasharray": word === "Now" ? null : "4 3" }),
    halo({ x: X(v), y: pad.t - 8, "text-anchor": "middle", class: "lbl", text: `${word} ${money(v, 0)}` }));
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
  const h = g.analogs && g.analogs.history
    ? (g.analogs.history.same_regime && g.analogs.history.same_regime.cases ? g.analogs.history.same_regime : g.analogs.history.all) : null;
  if (!g.history_built) {
    const box = el("div", {}, el("p", { class: "small" }, el("strong", { text: "Past cases: " }), "the signal history has not been built, so there is no past frequency to show."));
    if (featureOn("signal_calibration")) box.append(buildHistoryButton(reload));
    return box;
  }
  if (!h || !h.cases || !isNum(h.frequency)) return el("p", { class: "small" }, el("strong", { text: "Past cases: " }), "none like this in the signal history.");
  return el("p", { class: "small" }, el("strong", { text: "Past cases: " }),
    `when a stock's trend turned to ${trendWord(g.trend_state).toLowerCase()} before, it moved the expected way over 10 days in ${pct(100 * h.frequency, 0)} of ${num(h.cases, 0)} cases.`);
}

function comparePanel(dec, rerun) {
  const rows = compareRows(dec), o = dec.options, date = shortDate(o.thesis.horizon_date);
  const box = el("div", { class: "compare" }, el("h3", { text: "Shares or a contract" }),
    el("p", { class: "small muted", text: `Both on your levels, valued on ${date}. Chances and the expected figure are what option prices imply; past cases are counted apart. None of them is a forecast.` }));
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
    { label: "What option prices imply it is worth in expectation", cell: r => r.empty ? "" : expectationText(r.ev, r.pay, r.received) },
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
  box.append(el("p", { class: "small muted", text: "Close to what you pay by construction: option prices price options. A gap of a few percent comes from valuing each contract on its own volatility and the stock on the at-the-money one; it is not an edge." }));
  return box;
}

DECIDE_AFTER.push((out, dec) => {
  const card = out.firstChild;
  const rerun = () => { const go = out.parentNode && out.parentNode.querySelector("button.primary"); if (go) go.click(); };
  out.insertBefore(comparePanel(dec, rerun), card ? card.nextSibling : null);
});

Object.assign(BUILDERS, { today: buildToday });
