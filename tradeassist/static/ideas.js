"use strict";
/* Trade Assistant 2.7: Trade ideas. The best-graded trades to buy and to short across up to 250 large
   companies, and a simulation of any one of them: how often its stop and targets are reached on paths at
   the volatility option prices imply, and what each outcome makes or loses, with shares and with a
   contract. Nothing here places an order. Loaded last. */

const IDEA_UNIVERSES = [["top250", "250 largest US companies"], ["top200", "200 largest US companies"], ["large_caps", "100 large US companies"], ["watchlist", "Your watchlist"]];
const IDEA_SHOWN = 10;
// How each way out of a trade reads: in profit, at a loss, or wherever the horizon left it.
// Part out at a target and part elsewhere reads as "other": it can end either way.
const EXIT_TONE = { target: "pos", both: "pos", t1_horizon: "mid", t1_stop: "mid", stop: "neg", horizon: "mid" };
const levelText = v => isNum(v) ? money(v) : "n/a";

function gradeChip(g, score) {
  return el("span", { class: `grade-chip g-${g}`, title: isNum(score) ? `Score ${num(score, 0)} of 100: a sum of checks, not a chance` : null, text: g });
}

/* ---------- the lists ---------- */
// The row opens the simulation too; the button's own Enter must not also reach the row.
function simButton(r, onPick) {
  const b = el("button", { class: "plain small-btn", text: "Simulate" });
  b.addEventListener("click", e => { e.stopPropagation(); onPick(r); });
  b.addEventListener("keydown", e => e.stopPropagation());
  return b;
}

// 2.15: an idea as an opportunity (its key matches the server's), for Analyze opportunity and the blend.
function ideaOpportunity(r, day) {
  return { key: `ideas:${day}:${r.symbol}:${r.direction}`, source: "ideas", symbol: r.symbol, direction: r.direction,
    setup: "idea", setup_label: `Grade ${r.grade} ${r.direction === "LONG" ? "buy" : "short"}`, horizon: "swing", horizon_days: 20,
    entry: r.entry, stop: r.stop, target_1: r.target_1, target_2: r.target_2, atr: r.atr, score: r.score };
}

function ideaTable(rows, onPick, extra = {}) {
  const lead = extra.picked ? [{ label: "", cell: r => pickBox(extra.picked, extra.keyOf(r)) }] : [];
  const wrap = table([...lead,
    { label: "Stock", cell: r => el("div", {}, el("strong", { text: r.symbol }), el("div", { class: "small" }, trendTag(r.trend.state)),
      r.earnings_in_horizon ? el("div", { class: "small warn-text", text: `Earnings ${shortDate(r.earnings.date)}${r.earnings.estimated ? " (est.)" : ""}` }) : null) },
    { label: "Grade", cell: r => el("span", { class: "nowrap" }, gradeChip(r.grade, r.score), el("span", { class: "small muted", text: ` ${num(r.score, 0)}` })) },
    { label: "Entry", num: true, cell: r => el("span", { title: r.entry_source === "last close" ? "No current price: the last finished close" : "The current price" }, levelText(r.entry)) },
    { label: "Stop", num: true, cell: r => levelText(r.stop) },
    { label: "Target 1", num: true, cell: r => el("div", {}, levelText(r.target_1), el("div", { class: "small muted", text: `${num(r.reward_risk_1, 1)} × the risk` })) },
    { label: "Shares", num: true, cell: r => r.shares ? el("div", {}, num(r.shares, 0), r.capped_by_account ? el("div", { class: "small muted", text: "cut to the account" }) : null)
      : el("span", { class: "small muted", title: "One share risks more than your per-trade limit", text: "too large" }) },
    { label: "If target 1 is reached", num: true, cell: r => isNum(r.at_target_1) ? el("span", { class: "pos-text", text: signedMoney(r.at_target_1, 0) }) : "n/a" },
    { label: "If stopped", num: true, cell: r => isNum(r.at_stop) ? el("span", { class: "neg-text", text: signedMoney(r.at_stop, 0) }) : "n/a" },
    { label: "", cell: r => el("div", { class: "row nowrap" }, simButton(r, onPick),
      extra.day && state.openOpportunity && featureOn("trend_engine_v2") ? analyzeButton(ideaOpportunity(r, extra.day)) : "") }],
  rows, { onRow: r => onPick(r) });
  wrap.classList.add("idea-table");
  return wrap;
}

function ideaPanel(rows, side, onPick, extra = {}) {
  const word = side === "LONG" ? "buy" : "short";
  let all = false;
  const body = el("div");
  const draw = () => {
    const list = all ? rows : rows.slice(0, IDEA_SHOWN);
    const more = rows.length > IDEA_SHOWN ? el("button", { class: "plain small-btn", text: all ? `Show the top ${IDEA_SHOWN}` : `Show all ${rows.length}` }) : null;
    if (more) more.addEventListener("click", () => { all = !all; draw(); });
    body.replaceChildren(list.length ? ideaTable(list, onPick, extra) : el("p", { class: "muted small", text: `No stock leans ${side === "LONG" ? "long" : "short"} with a grade better than AVOID right now.` }), more || "");
  };
  draw();
  return el("div", { class: "panel" }, el("h3", { text: `Highest-graded trades to ${word}${rows.length > IDEA_SHOWN ? ` (top ${IDEA_SHOWN} of ${rows.length})` : rows.length ? ` (${rows.length})` : ""}` }), body);
}

function ideasSummary(d) {
  const a = d.account;
  return el("p", { class: "small" }, el("strong", { text: `${d.scanned} of ${d.universe_size} read` }),
    ` (${d.universe_text}) on ${shortDate(d.checked_at.slice(0, 10))} at ${d.checked_at.slice(11, 16)} New York time, in ${num(d.seconds, 0)} seconds. `,
    d.regime.words ? `The market: ${d.regime.words}. ` : "",
    `Shares are sized so the stop costs at most ${money(a.risk_budget, 0)} (${num(a.risk_pct, 1)}% of ${money(a.size, 0)}), held up to ${d.horizon_days} trading days.`);
}

function ideasRest(d, onPick) {
  const box = el("div", { class: "panel" });
  if (d.held_back.length) box.append(el("details", {}, el("summary", { text: `Held back: ${d.held_back.length} whose lean was withdrawn` }),
    el("p", { class: "small muted", text: "As on Today, a lean is withdrawn when the market regime works against that side or the opposite reversal is under way. These are graded on the side their evidence points, and left out of the lists above for the reason given." }),
    table([{ label: "Stock", cell: r => el("strong", { text: r.symbol }) }, { label: "Side", cell: r => r.direction === "LONG" ? "Buy" : "Short" },
      { label: "Grade", cell: r => gradeChip(r.grade, r.score) }, { label: "Why held back", cell: r => r.held_back },
      { label: "", cell: r => simButton(r, onPick) }], d.held_back)));
  if (d.avoid.length) box.append(el("details", {}, el("summary", { text: `Graded AVOID: ${d.avoid.length}` }),
    el("ul", { class: "plain small" }, d.avoid.map(r => el("li", {}, el("strong", { text: `${r.symbol} (${r.direction === "LONG" ? "buy" : "short"}): ` }), r.caps.join(" ") || "the checks add up to under 35 of 100.")))));
  const parts = [];
  if (d.balanced) parts.push(`${d.balanced} with the evidence balanced`);
  if (d.not_measured.length) parts.push(`${d.not_measured.length} with too little evidence to score (${d.not_measured.join(", ")})`);
  if (parts.length) box.append(el("p", { class: "small", text: `No side: ${parts.join("; ")}.` }));
  if (d.errors.length) box.append(el("p", { class: "small muted", text: `Not read: ${d.errors.map(e => `${e.symbol} (${e.error})`).join("; ")}.` }));
  if ((d.ungraded || []).length) box.append(el("p", { class: "small muted", text: `Read but not graded: ${d.ungraded.map(e => `${e.symbol} (${e.error})`).join("; ")}.` }));
  box.append(el("p", { class: "small muted", text: d.rule }));
  return box;
}

/* ---------- the simulation's charts ---------- */
// The stock's price over the horizon: the middle 80% and 50% of paths as bands, the median, a few paths in
// the colour of how they ended, and the stop and targets.
function pathChart(s) {
  const W = 760, H = 300, pad = { l: 64, r: 92, t: 12, b: 28 };
  const n = s.fan.length;
  const X = i => pad.l + (W - pad.l - pad.r) * i / n;
  const levels = [{ v: s.stop, name: "Stop", tone: "neg" }, { v: s.target_1, name: "Target 1", tone: "pos" },
    ...(s.plan !== "target_1" ? [{ v: s.target_2, name: "Target 2", tone: "pos" }] : []), { v: s.entry, name: "Entry", tone: "mid" }];
  const vals = [...s.fan.flatMap(f => [f.p10, f.p90]), ...levels.map(l => l.v), ...s.samples.flatMap(p => p.prices)];
  const ys = niceScale(Math.min(...vals), Math.max(...vals), 5);
  const Y = v => pad.t + (H - pad.t - pad.b) * (1 - (v - ys.lo) / (ys.hi - ys.lo));
  const root = svg("svg", { viewBox: `0 0 ${W} ${H}`, role: "img", "aria-label": `${s.symbol}: the range of simulated prices to ${shortDate(s.horizon_date)}, with the stop and targets` });
  for (const v of ys.ticks) root.append(svg("line", { class: "grid", x1: pad.l, x2: W - pad.r, y1: Y(v), y2: Y(v) }),
    svg("text", { x: pad.l - 8, y: Y(v) + 4, "text-anchor": "end", text: money(v, ys.step >= 1 ? 0 : 2) }));
  const band = (lo, hi, cls) => {
    const top = s.fan.map((f, i) => `${X(i + 1)},${Y(f[hi])}`), bot = s.fan.map((f, i) => `${X(i + 1)},${Y(f[lo])}`).reverse();
    return svg("polygon", { class: cls, points: [`${X(0)},${Y(s.entry)}`, ...top, ...bot].join(" ") });
  };
  root.append(band("p10", "p90", "fan-outer"), band("p25", "p75", "fan-inner"));
  for (const p of s.samples.slice(0, 14)) {
    const tone = EXIT_TONE[p.ended] || "mid";
    root.append(svg("polyline", { class: `sample-path ${tone}`, points: p.prices.map((v, i) => `${X(i)},${Y(v)}`).join(" ") }));
  }
  root.append(svg("polyline", { class: "fan-mid", points: [`${X(0)},${Y(s.entry)}`, ...s.fan.map((f, i) => `${X(i + 1)},${Y(f.p50)}`)].join(" ") }));
  const labels = spreadLabels(levels.map(l => ({ ...l, y: Y(l.v) + 4 })), 13, pad.t + 4, H - pad.b);
  for (const l of levels) root.append(svg("line", { class: `level-line ${l.tone}`, x1: pad.l, x2: W - pad.r, y1: Y(l.v), y2: Y(l.v) }));
  for (const l of labels) root.append(halo({ x: W - pad.r + 6, y: l.y, class: "lbl", text: `${l.name} ${money(l.v, l.v >= 1000 ? 0 : 2)}` }));
  const every = Math.max(1, Math.ceil(n / 6));
  s.fan.forEach((f, i) => { if ((i + 1) % every === 0 || i === n - 1) root.append(svg("text", { x: X(i + 1), y: H - 8, "text-anchor": "middle", text: shortDate(f.date).replace(/, \d{4}$/, "") })); });
  // Hover: the day's range across paths.
  const tip = el("div", { class: "tip" }), cross = svg("line", { class: "hover-line", y1: pad.t, y2: H - pad.b, x1: -10, x2: -10 });
  const hit = svg("rect", { x: pad.l, y: pad.t, width: W - pad.l - pad.r, height: H - pad.t - pad.b, fill: "transparent" });
  root.append(cross, hit);
  const move = e => {
    const box = root.getBoundingClientRect(), xv = (e.clientX - box.left) * W / box.width;
    const i = Math.max(1, Math.min(n, Math.round((xv - pad.l) / ((W - pad.l - pad.r) / n))));
    const f = s.fan[i - 1];
    cross.setAttribute("x1", X(i)); cross.setAttribute("x2", X(i));
    tip.replaceChildren(el("div", { class: "tip-head", text: shortDate(f.date) }),
      el("div", { text: `Median ${money(f.p50)}` }), el("div", { class: "tip-sub", text: `Half between ${money(f.p25)} and ${money(f.p75)}` }),
      el("div", { class: "tip-sub", text: `Eight in ten between ${money(f.p10)} and ${money(f.p90)}` }));
    tooltipAt(tip, box, X(i), W);
  };
  hit.addEventListener("pointermove", move);
  hit.addEventListener("pointerleave", () => { tip.style.display = "none"; cross.setAttribute("x1", -10); cross.setAttribute("x2", -10); });
  const legend = el("div", { class: "legend" }, el("span", {}, el("i", { class: "band-key fan-key" }), "Eight in ten prices each day"),
    el("span", {}, el("i", { class: "band-key fan-key inner" }), "Half the prices each day"), el("span", {}, el("i", { style: "background: var(--ink)" }), "Median price each day"),
    el("span", {}, el("i", { style: "background: var(--div-pos)" }), "A path out at a target"), el("span", {}, el("i", { style: "background: var(--div-neg)" }), "A path stopped out"),
    el("span", {}, el("i", { style: "background: var(--muted)" }), "Other (neither, or part each way)"));
  return el("div", { class: "chart" }, legend, el("div", { class: "chart-scroll" }, root, tip));
}

// Shares against the contract on one dollar axis: what 1 in 10 did worse than, the middle half, the middle
// result and what 1 in 10 did better than, with the average marked.
function rangeChart(rows) {
  const W = 760, rowH = 46, pad = { l: 110, r: 20, t: 10, b: 26 }, H = pad.t + pad.b + rows.length * rowH;
  const vals = rows.flatMap(r => [r.s.p10, r.s.p90, r.s.mean, 0]);
  const xs = niceScale(Math.min(...vals), Math.max(...vals), 6);
  const X = v => pad.l + (W - pad.l - pad.r) * (v - xs.lo) / (xs.hi - xs.lo);
  const root = svg("svg", { viewBox: `0 0 ${W} ${H}`, role: "img", "aria-label": "The spread of results, with shares and with the contract" });
  for (const v of xs.ticks) root.append(svg("line", { class: v === 0 ? "zero" : "grid", x1: X(v), x2: X(v), y1: pad.t, y2: H - pad.b }),
    svg("text", { x: X(v), y: H - 8, "text-anchor": "middle", text: signedMoney(v, 0) }));
  const tip = el("div", { class: "tip" });
  rows.forEach((r, i) => {
    const y = pad.t + i * rowH + rowH / 2, s = r.s;
    const g = svg("g", { class: "range-row" });
    g.append(svg("text", { x: pad.l - 10, y: y + 4, "text-anchor": "end", class: "lbl", text: r.name }),
      svg("line", { class: "range-whisker", x1: X(s.p10), x2: X(s.p90), y1: y, y2: y }),
      svg("rect", { class: "range-box", x: X(s.p25), y: y - 9, width: Math.max(2, X(s.p75) - X(s.p25)), height: 18, rx: 4 }),
      svg("line", { class: "range-mid", x1: X(s.p50), x2: X(s.p50), y1: y - 12, y2: y + 12 }),
      svg("circle", { class: "range-mean", cx: X(s.mean), cy: y, r: 4.5 }));
    const hit = svg("rect", { x: pad.l, y: y - rowH / 2, width: W - pad.l - pad.r, height: rowH, fill: "transparent" });
    hit.addEventListener("pointerenter", () => {
      tip.replaceChildren(el("div", { class: "tip-head", text: r.name }), el("div", { text: `Middle result ${signedMoney(s.p50, 0)}` }),
        el("div", { class: "tip-sub", text: `Half between ${signedMoney(s.p25, 0)} and ${signedMoney(s.p75, 0)}` }),
        el("div", { class: "tip-sub", text: `Eight in ten between ${signedMoney(s.p10, 0)} and ${signedMoney(s.p90, 0)}` }),
        el("div", { class: "tip-sub", text: `Average ${signedMoney(s.mean, 0)}` }));
      tooltipAt(tip, root.getBoundingClientRect(), X(s.p50), W);
    });
    hit.addEventListener("pointerleave", () => { tip.style.display = "none"; });
    root.append(g, hit);
  });
  const legend = el("div", { class: "legend" }, el("span", {}, el("i", { class: "key-whisker" }), "Eight in ten results"),
    el("span", {}, el("i", { class: "key-box range-key" }), "Half the results"), el("span", {}, el("i", { style: "background: var(--ink); width: 3px; height: 12px" }), "Middle result"),
    el("span", {}, el("i", { class: "key-dot" }), "Average"));
  return el("div", { class: "chart" }, legend, el("div", { class: "chart-scroll" }, root, tip));
}

/* ---------- the simulation panel ---------- */
const chance = p => !isNum(p) ? "n/a" : p > 0 && p < 0.005 ? "under 1%" : p < 1 && p > 0.995 ? "over 99%" : pct(100 * p, 0);

function outcomeTiles(s) {
  const by = s.with_shares.by_exit, c = s.with_contract ? Object.fromEntries(s.with_contract.by_exit.map(x => [x.key, x])) : null;
  return el("div", { class: "outcome-tiles" }, by.map(x => el("div", { class: `outcome ${EXIT_TONE[x.key] || "mid"}` },
    el("div", { class: "outcome-share", text: chance(x.share) }), el("div", { class: "outcome-label", text: x.label }),
    el("div", { class: "outcome-money" }, el("strong", { text: signedMoney(x.average, 0) }), el("span", { class: "muted", text: " with shares" }),
      x.key === "horizon" || x.key === "t1_horizon" ? el("span", { class: "small muted", text: ` (from ${signedMoney(x.low, 0)} to ${signedMoney(x.high, 0)})` }) : null),
    c && c[x.key] ? el("div", { class: "small" }, el("strong", { text: signedMoney(c[x.key].average, 0) }), el("span", { class: "muted", text: " with the contract, on average" })) : null)));
}

function compareTable(s) {
  const sh = s.with_shares, ct = s.with_contract, spec = s.contract_spec;
  const cols = [{ label: "", cell: r => el("span", { class: "small", text: r.what }) }, { label: "Shares", num: true, cell: r => r.shares }];
  if (ct) cols.push({ label: `${spec.option_type === "CALL" ? "Call" : "Put"} contract`, num: true, cell: r => r.contract });
  const rowsOf = [
    { what: "What it takes", shares: s.direction === "LONG" ? money(s.position_value, 0) : `${money(s.position_value, 0)} sold short`, contract: ct ? money(ct.cost, 0) : null },
    { what: ct ? "If stopped out (the contract: on average)" : "If stopped out", shares: signedMoney(sh.fixed.at_stop, 0), contract: ct ? avgOf(ct, "stop") : null },
    { what: `${s.plan === "split" ? "If both targets are reached" : `If ${s.plan === "target_2" ? "target 2" : "target 1"} is reached`}${ct ? " (the contract: on average)" : ""}`,
      shares: signedMoney(s.plan === "split" ? sh.fixed.at_split : s.plan === "target_2" ? sh.fixed.at_target_2 : sh.fixed.at_target_1, 0),
      contract: ct ? avgOf(ct, s.plan === "split" ? "both" : "target") : null },
    { what: "1 in 10 did worse than", shares: signedMoney(sh.p10, 0), contract: ct ? signedMoney(ct.p10, 0) : null },
    { what: "Middle result", shares: signedMoney(sh.p50, 0), contract: ct ? signedMoney(ct.p50, 0) : null },
    { what: "1 in 10 did better than", shares: signedMoney(sh.p90, 0), contract: ct ? signedMoney(ct.p90, 0) : null },
    { what: "Paths that made money", shares: chance(sh.chance_profit), contract: ct ? chance(ct.chance_profit) : null },
    { what: "Average over all paths (the carry, not an edge)", shares: `${signedMoney(sh.mean, 0)} ±${money(2 * sh.standard_error, 0)}`, contract: ct ? `${signedMoney(ct.mean, 0)} ±${money(2 * ct.standard_error, 0)}` : null },
  ];
  return table(cols, rowsOf);
}
const avgOf = (r, key) => { const x = r.by_exit.find(b => b.key === key); return x ? signedMoney(x.average, 0) : "n/a"; };

function renderTradeSim(s) {
  const frag = el("div");
  for (const n of s.notes) frag.append(banner(n.startsWith("Demo") ? "warn" : "info", n));
  const dir = s.direction === "LONG" ? "Buy" : "Short";
  frag.append(el("div", { class: "sim-head" }, gradeChip(s.grade.grade, s.grade.score),
    el("div", {}, el("strong", { text: `${dir} ${num(s.shares, 0)} ${s.symbol} at ${money(s.entry)}` }),
      el("div", { class: "small muted", text: `Stop ${money(s.stop)}, target 1 ${money(s.target_1)}${s.plan !== "target_1" ? `, target 2 ${money(s.target_2)}` : ""}. ${s.plan_label}; otherwise out at the close on ${shortDate(s.horizon_date)} (${s.horizon_days} trading days).` }))));
  const vol = s.volatility;
  frag.append(el("p", { class: "small" }, `${num(s.paths, 0)} paths at ${pct(100 * vol.value, 0)} a year, `,
    vol.source === "implied" ? `the at-the-money volatility option prices imply (${shortDate(vol.expiration)} expiration).` : `the stock's own volatility over the last 60 trading days (no option prices).`));
  frag.append(el("h4", { text: "How the trade ends, and what each way out makes" }), outcomeTiles(s));
  frag.append(el("h4", { text: "Where the price goes" }), pathChart(s));
  frag.append(el("h4", { text: s.with_contract ? "With shares or with a contract" : "The spread of results" }),
    rangeChart([{ name: "Shares", s: s.with_shares }, ...(s.with_contract ? [{ name: `${s.contract_spec.option_type === "CALL" ? "Call" : "Put"} contract`, s: s.with_contract }] : [])]),
    compareTable(s));
  const spec = s.contract_spec;
  if (spec) {
    const contractWords = `${spec.contracts} × ${s.symbol} ${shortDate(spec.expiration)} ${num(spec.strike, spec.strike % 1 ? 2 : 0)} ${spec.option_type === "CALL" ? "call" : "put"} at ${money(spec.premium)} a share, ${money(spec.cost, 0)} in all`;
    const actions = el("div", { class: "row sim-actions" });
    if (BUILDERS.price) { const b = el("button", { class: "plain", text: "Set the limit" }); b.addEventListener("click", () => state.openPricing(FLOW.pricingRequest(s.symbol, spec, spec.contracts, "buy"))); actions.append(b); }
    if (state.openSimulation) { const b = el("button", { class: "plain", text: "Simulate the contract alone" }); b.addEventListener("click", () => state.openSimulation(s.symbol, [{ expiration: spec.expiration, option_type: spec.option_type.toLowerCase(), strike: spec.strike, side: "buy", contracts: spec.contracts, entry_price: spec.midpoint, entry_source: "you" }])); actions.append(b); }
    frag.append(el("p", { class: "small" }, el("strong", { text: "The contract: " }), contractWords,
      spec.category ? `. Chosen as the ${spec.category.toLowerCase()}: ${spec.why.charAt(0).toLowerCase()}${spec.why.slice(1)}` : ".",
      spec.contracts_source === "risk budget" ? " As many as your risk per trade pays for, at least one." : ""),
      ...(spec.flags || []).map(f => banner("warn", f)));
    frag.append(actions);
  }
  if (state.openDecide && featureOn("trend_engine_v2")) {
    const b = el("button", { class: "plain", text: "Analyze opportunity" });
    b.addEventListener("click", () => {
      state.thesis = FLOW.thesis({ symbol: s.symbol, direction: s.direction, entry: s.entry, invalidation: s.stop, target1: s.target_1, target2: s.target_2, horizonDays: s.horizon_days, contracts: spec ? spec.contracts : 1 });
      state.openDecide(s.symbol, s.direction);
    });
    const row = frag.querySelector(".sim-actions") || el("div", { class: "row sim-actions" });
    row.append(b);
    if (!row.parentNode) frag.append(row);
  }
  frag.append(el("p", { class: "small muted", text: s.model }));
  return frag;
}

function simPanel() {
  const head = el("h3", { text: "Simulate a trade idea" });
  const sym = el("input", { class: "sym", type: "text", "aria-label": "Ticker", autocomplete: "off", spellcheck: "false" });
  const dir = el("select", { "aria-label": "Side" }, el("option", { value: "LONG", text: "Buy" }), el("option", { value: "SHORT", text: "Short" }));
  const entry = numInput({ placeholder: "now", "aria-label": "Entry" }), stop = numInput({ placeholder: "suggest", "aria-label": "Stop" });
  const t1 = numInput({ placeholder: "suggest", "aria-label": "Target 1" }), t2 = numInput({ placeholder: "suggest", "aria-label": "Target 2" });
  const shares = el("input", { type: "number", min: 1, step: 1, placeholder: "sized", "aria-label": "Shares" });
  const days = el("input", { type: "number", min: 1, max: 120, step: 1, value: 20, "aria-label": "Trading days" });
  const plan = el("select", { "aria-label": "Exit plan" }, el("option", { value: "target_1", text: "All out at target 1" }),
    el("option", { value: "target_2", text: "All out at target 2" }), el("option", { value: "split", text: "Half at target 1, half at target 2" }));
  const withContract = el("select", { "aria-label": "Contract" }, el("option", { value: "best", text: "Shares and the best contract" }), el("option", { value: "none", text: "Shares only" }));
  const run = el("button", { class: "primary", text: "Simulate" });
  const err = el("div", { class: "error", role: "alert" });
  const out = el("div");
  const box = el("div", { class: "panel sim-panel hidden" }, head,
    el("div", { class: "inline-form" }, field("Stock", sym), field("Side", dir), field("Entry", entry), field("Stop", stop), field("Target 1", t1),
      field("Target 2", t2), field("Shares", shares), field("Trading days", days), field("Exit plan", plan), field("With", withContract), run), err, out);
  const val = input => input.value === "" ? null : Number(input.value);
  let ticket = 0;
  async function simulate() {
    const mine = ++ticket;
    err.textContent = "";
    const body = { symbol: sym.value.trim().toUpperCase(), direction: dir.value, horizon_days: Math.round(Number(days.value) || 20), exit_plan: plan.value };
    if (!body.symbol) { err.textContent = "Enter a ticker."; return; }
    for (const [k, input] of [["entry_price", entry], ["stop", stop], ["target_1", t1], ["target_2", t2]]) { const v = val(input); if (v !== null) body[k] = v; }
    if (shares.value !== "") body.shares = Math.max(1, Math.round(Number(shares.value)));
    if (withContract.value === "none") body.contract = "none";
    out.classList.add("refetching"); run.disabled = true;
    if (!out.firstChild) out.replaceChildren(el("div", { class: "loadnote small muted" }, el("span", { class: "spinner" }), el("span", { text: "Simulating 20,000 paths…" })));
    try {
      const s = await api("/v2/ideas/simulate", body);
      if (mine !== ticket) return;
      // Show what was used, so a second run starts from the same trade.
      for (const [input, v] of [[entry, s.entry], [stop, s.stop], [t1, s.target_1], [t2, s.target_2]]) input.value = String(Math.round(v * 100) / 100);
      if (shares.value === "") shares.placeholder = `sized: ${s.shares}`;
      out.replaceChildren(renderTradeSim(s));
    } catch (e) { if (mine === ticket) { err.textContent = e.message; if (out.querySelector(".loadnote")) out.replaceChildren(); } }
    finally { if (mine === ticket) { out.classList.remove("refetching"); run.disabled = false; } }
  }
  run.addEventListener("click", simulate);
  box.open = idea => {
    box.classList.remove("hidden");
    head.textContent = `Simulate: ${idea.direction === "LONG" ? "buy" : "short"} ${idea.symbol}`;
    sym.value = idea.symbol; dir.value = idea.direction;
    entry.value = String(Math.round(idea.entry * 100) / 100); stop.value = String(Math.round(idea.stop * 100) / 100);
    t1.value = String(Math.round(idea.target_1 * 100) / 100); t2.value = String(Math.round(idea.target_2 * 100) / 100);
    shares.value = ""; shares.placeholder = idea.shares ? `sized: ${idea.shares}` : "sized";
    out.replaceChildren();
    box.scrollIntoView({ behavior: "smooth", block: "start" });
    simulate();
  };
  return box;
}

/* ---------- the screen ---------- */
function buildIdeas() {
  const uni = el("select", { "aria-label": "Stocks" }, IDEA_UNIVERSES.map(([v, t]) => el("option", { value: v, text: t })));
  const scan = el("button", { class: "primary", text: "Scan" });
  const stopBtn = el("button", { class: "plain hidden", text: "Stop" });
  const err = el("div", { class: "error", role: "alert" });
  const bar = el("progress", { max: 100, value: 0, class: "scan-progress", "aria-label": "Scan progress" });
  const barText = el("span", { class: "small muted" });
  const done = el("span", { class: "visually-hidden", "aria-live": "polite" });
  const progress = el("div", { class: "row scan-row hidden" }, bar, barText);
  const out = el("div");
  const sim = simPanel();
  let timer = null, shownJob = undefined, running = false, failures = 0;

  const picked = new Set();
  const blend = featureOn("opportunity_blend") ? blendPanel({ source: "ideas", collapsed: true, horizon: "swing",
    title: "Split a budget across the best ideas", keys: () => [...picked],
    intro: "Each idea is a 20-day swing trade. The optimizer walks them together on 4,000 paths and picks how much of each to buy for the goal; an idea's only drift is the signal history's record for its trend score (none without the signal history), after costs and pulled toward zero when the record is short." }) : null;
  const budgetBtn = el("button", { class: "plain", text: "Set budget" });
  budgetBtn.addEventListener("click", () => blend && blend.toggle());
  if (!blend) budgetBtn.classList.add("hidden");
  const render = d => {
    const frag = el("div");
    for (const n of d.notes) frag.append(banner(n.startsWith("Demo") ? "warn" : "info", n));
    const day = String(d.checked_at || "").slice(0, 10);
    const extra = blend ? { picked, keyOf: r => `ideas:${day}:${r.symbol}:${r.direction}`, day } : { day };
    frag.append(ideasSummary(d), ideaPanel(d.long, "LONG", sim.open, extra), ideaPanel(d.short, "SHORT", sim.open, extra));
    if (blend) frag.append(blend);
    frag.append(ideasRest(d, sim.open));
    out.replaceChildren(frag);
  };
  const empty = () => out.replaceChildren(el("div", { class: "panel" },
    el("p", {}, "Not scanned yet. Pick the stocks and press ", el("strong", { text: "Scan" }), "."),
    el("p", { class: "small muted", text: "Each stock's trend, reversal and trade grade are read in turn. On demo data the 250 largest take about two and a half minutes. With live data the first scan of the day also fetches each stock's price history, spaced out so the other screens keep working, which takes several minutes; later scans reuse it. You can keep using the app meanwhile." })));
  // The latest status: the progress while a scan runs, and the result once there is a new one.
  async function paint(st) {
    const job = st.job;
    running = job.state === "running";
    scan.disabled = uni.disabled = running;
    stopBtn.classList.toggle("hidden", !running);
    progress.classList.toggle("hidden", !running);
    if (job.universe) uni.value = job.universe;
    if (running) {
      bar.value = job.total ? Math.round(100 * job.done / job.total) : 0;
      barText.textContent = job.current ? `Reading and grading ${job.current} (${Math.min(job.done + 1, job.total)} of ${job.total})`
        : `Reading the market regime and prices (${job.total} stocks to go)`;
    }
    if (job.state === "failed") err.textContent = `The scan stopped: ${job.error}`;
    if (job.state === "stopped") err.textContent = "The scan was stopped. The lists below are from the last finished scan.";
    // A result is known by its scan and its time (scan numbers restart with the app; the saved result does not).
    const key = `${st.result_job}|${st.result_at}`;
    if (key !== shownJob) {
      const full = st.result !== undefined ? st : await api("/v2/ideas");
      shownJob = `${full.result_job}|${full.result_at}`;
      if (full.result) { render(full.result); if (job.state === "done") done.textContent = "The scan has finished."; }
      else empty();
    }
  }
  // Polls only while a scan runs and the screen is showing; a failed poll is tried again a little later.
  async function poll() {
    clearTimeout(timer);
    if (view.classList.contains("hidden")) return;
    try {
      await paint(await api("/v2/ideas?brief=true"));
      failures = 0;
      if (running) timer = setTimeout(poll, 1500);
    } catch (e) {
      failures += 1;
      err.textContent = `${e.message} (trying again)`;
      timer = setTimeout(poll, Math.min(15000, 1500 * 2 ** failures));
    }
  }
  scan.addEventListener("click", () => withBusy(scan, err, async () => {
    err.textContent = "";
    await paint(await api("/v2/ideas/scan", { universe: uni.value }));
    timer = setTimeout(poll, 800);
  }));
  stopBtn.addEventListener("click", () => withBusy(stopBtn, err, async () => { await paint(await api("/v2/ideas/stop", {})); poll(); }));
  const view = el("section", {}, el("h2", { text: "Trade ideas" }),
    el("p", { class: "lede", text: "The highest-graded trades to buy and to short across up to 250 large companies, each with its stop, targets, size, and what it makes or loses if the price gets there. Simulate any one to see how often each happens, analyze it, or split a budget across the best." }),
    el("div", { class: "panel" }, el("div", { class: "row" }, field("Stocks", uni), scan, stopBtn, budgetBtn), progress, done, err), out, sim);
  view.onShow = () => {
    uni.options[2].textContent = `Your watchlist (${((state.status && state.status.watchlist) || []).length})`;
    poll();
  };
  return view;
}

Object.assign(BUILDERS, { ideas: buildIdeas });
