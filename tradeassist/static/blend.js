"use strict";
/* Trade Assistant 2.15: the budget split, shared by the Morning plan, Live news, Trade ideas, Analyze opportunity and
   Paper trading. A budget, a goal (most dollars, best return for the risk, lowest risk) and limits go to the blend
   optimizer (/v2/blend); back come the trades, how many of each, the blend's range of results, its tail risk, how
   its stocks move together and what a market drop would do. Nothing here places an order. */

const BLEND_GOALS = [["dollars", "Most dollars"], ["balanced", "Best return for the risk"], ["low_risk", "Lowest risk"]];
const BLEND_INSTRUMENTS = [["shares", "Shares"], ["short", "Shorts"], ["call", "Calls"], ["put", "Puts"]];
const INSTRUMENT_WORDS = { shares: "Shares", short: "Short", call: "Call", put: "Put", SHARES: "Shares", SHORT: "Short", CALL: "Call", PUT: "Put" };

async function loadPrefs(force) {
  if (!state.prefs || force) {
    try { state.prefs = await api("/v2/prefs"); } catch (e) { state.prefs = null; }
  }
  return state.prefs;
}

let PREFS_TIMER = null;
function savePrefs(patch) {
  // Merged now so every screen sees it, saved a moment later so typing does not send a request per key.
  if (state.prefs) for (const [g, v] of Object.entries(patch)) state.prefs[g] = { ...(state.prefs[g] || {}), ...v };
  clearTimeout(PREFS_TIMER);
  PREFS_TIMER = setTimeout(() => api("/v2/prefs", patch).then(p => { state.prefs = p; }).catch(() => {}), 600);
}

// A field whose label carries an info icon (labels must not wrap the icon's button, so this is a div).
function infoField(label, key, control) {
  if (control.setAttribute && !control.getAttribute("aria-label")) control.setAttribute("aria-label", label);
  return el("div", { class: "field" }, el("span", {}, label, " ", infoIcon(key)), control);
}

// The blend's profit or loss over the horizon: the 10-90 band, the 25-75 band and the middle.
function fanChart(fan, budget) {
  if (!fan || fan.length < 2) return el("p", { class: "small muted", text: "No path to draw." });
  const W = 760, H = 220, pad = { l: 64, r: 16, t: 12, b: 28 };
  const lo = Math.min(0, ...fan.map(f => f.p10)), hi = Math.max(0, ...fan.map(f => f.p90));
  const span = (hi - lo) || 1;
  const X = i => pad.l + (W - pad.l - pad.r) * i / (fan.length - 1);
  const Y = v => pad.t + (H - pad.t - pad.b) * (1 - (v - lo) / span);
  const area = (a, b) => `M${fan.map((f, i) => `${X(i).toFixed(1)} ${Y(f[a]).toFixed(1)}`).join(" L")} L${fan.map((f, i) => [i, f]).reverse().map(([i, f]) => `${X(i).toFixed(1)} ${Y(f[b]).toFixed(1)}`).join(" L")} Z`;
  const root = svg("svg", { viewBox: `0 0 ${W} ${H}`, role: "img", "aria-label": `The blend's profit or loss over the horizon: middle ${money(fan[fan.length - 1].p50, 0)} at the end, 10th to 90th percentile ${money(fan[fan.length - 1].p10, 0)} to ${money(fan[fan.length - 1].p90, 0)}` });
  for (const v of [lo, 0, hi]) {
    root.append(svg("line", { class: "grid", x1: pad.l, x2: W - pad.r, y1: Y(v), y2: Y(v) }),
      svg("text", { x: pad.l - 8, y: Y(v) + 4, "text-anchor": "end", text: money(v, 0) }));
  }
  root.append(svg("path", { d: area("p10", "p90"), fill: "var(--series-1)", opacity: 0.16 }),
    svg("path", { d: area("p25", "p75"), fill: "var(--series-1)", opacity: 0.26 }),
    svg("path", { d: `M${fan.map((f, i) => `${X(i).toFixed(1)} ${Y(f.p50).toFixed(1)}`).join(" L")}`, fill: "none", stroke: "var(--series-1)", "stroke-width": 2 }));
  const when = s => { const d = new Date(s); return fan.length > 30 || (new Date(fan[fan.length - 1].at) - new Date(fan[0].at)) > 864e5 * 1.5
    ? d.toLocaleDateString("en-US", { timeZone: "America/New_York", month: "short", day: "numeric" })
    : d.toLocaleTimeString("en-US", { timeZone: "America/New_York", hour: "2-digit", minute: "2-digit", hour12: false }); };
  const every = Math.max(1, Math.floor(fan.length / 6));
  for (let i = 0; i < fan.length; i += every) root.append(svg("text", { x: X(i), y: H - 8, "text-anchor": "middle", text: when(fan[i].at) }));
  return el("div", { class: "chart fan" }, root,
    el("div", { class: "legend" }, el("span", {}, el("i", { style: "background: var(--series-1)" }), "Middle outcome"),
      el("span", {}, el("i", { style: "background: color-mix(in oklab, var(--series-1) 30%, transparent)" }), "Half of outcomes"),
      el("span", {}, el("i", { style: "background: color-mix(in oklab, var(--series-1) 16%, transparent)" }), "Eight in ten")));
}

function corrTable(c) {
  if (!c || !c.symbols || c.symbols.length < 2) return null;
  const cell = v => el("td", { class: "num corr", style: `background: color-mix(in oklab, ${v >= 0 ? "var(--div-neg)" : "var(--div-pos)"} ${Math.round(Math.min(1, Math.abs(v)) * 60)}%, var(--div-mid))`, text: num(v, 2) });
  return el("div", { class: "scroll" }, el("table", { class: "corr-table" },
    el("thead", {}, el("tr", {}, el("th", { text: "" }), c.symbols.map(s => el("th", { class: "num", text: s })))),
    el("tbody", {}, c.matrix.map((row, i) => el("tr", {}, el("th", { text: c.symbols[i] }), row.map(cell))))));
}

// Basket legs for Analyze opportunity from a split's opportunities.
function blendBasket(res, extra = {}) {
  const used = new Set(res.rows.map(r => r.key));
  const opps = (res.opportunities || []).filter(o => used.has(o.key) || !res.rows.length);
  return { source: res.source || "basket", opportunities: opps.length ? opps : res.opportunities || [], units: res.rows.length ? FLOW_BLEND.units(res.rows) : null,
    budget: res.budget, goal: res.goal, tail_pct: res.tail_pct, horizon: res.horizon, ...extra };
}

const FLOW_BLEND = {
  units: rows => Object.fromEntries(rows.map(r => [`${r.key}|${r.instrument}`, r.units])),
  orders: rows => rows.map(r => `${r.label}${r.instrument === "shares" || r.instrument === "short" ? ` · stop $${r.stop.toFixed(2)}, target $${r.target_1.toFixed(2)}` : ""}`).join("\n"),
};

async function paperFromBlend(res, req, err, button) {
  await withBusy(button, err, async () => {
    const body = { mode: "MANUAL", budget: res.budget, horizon: res.horizon || "day", ends: res.horizon === "swing" ? "month" : "day",
      blend: { source: req.source, units: FLOW_BLEND.units(res.rows) } };
    if (req.legs) body.blend.legs = req.legs; else body.blend.keys = res.rows.map(r => r.key);
    const out = await api("/v2/paper/portfolios", body);
    if (state.openPaper) state.openPaper(out.portfolio.id);
  });
}

function renderBlend(res, req, opts = {}) {
  const frag = el("div", { class: "blend-result" });
  const err = el("div", { class: "error", role: "alert" });
  if (res.given && (res.breaches || []).length) {
    frag.append(banner("warn", `These units break ${res.breaches.length === 1 ? "a cap or limit" : `${res.breaches.length} caps or limits`}: ${res.breaches.join("; ")}. Plan the split again for one that fits.`));
  }
  for (const n of res.notes || []) if (!(res.given && n.startsWith("These units break"))) frag.append(el("p", { class: "small muted", text: n }));
  const show = (r, label, study) => {
    const box = el("div", { class: study ? "blend-study" : "" });
    if (label) box.append(el("h4", { text: label }));
    const c = r.combined;
    if (!r.rows.length || !c) {
      box.append(banner("info", "The split keeps the cash: no trade adds expected profit within the goal's limits."));
      return box;
    }
    box.append(el("div", { class: "metrics" },
      metric(signedMoney(c.expected_pnl, 0), `Expected result (${pct(100 * c.return_on_budget, 2)} of the budget)`),
      metric(pct(100 * c.p_profit, 0), "Chance of a profit (model)"),
      metric(`${signedMoney(c.pnl_p10, 0)} to ${signedMoney(c.pnl_p90, 0)}`, "Eight in ten outcomes"),
      metric(money(-c.cvar10, 0), "Average of the worst tenth"),
      metric(money(-c.loss_if_every_stop, 0), "If every stop is hit"),
      metric(`${money(r.spent, 0)} / ${money(r.left, 0)}`, "Spent / left in cash")));
    if (c.unlimited_loss) box.append(banner("warn", "A short's loss has no limit if the stock gaps through its stop; the model assumes the stop fills."));
    box.append(table([
      { label: "Trade", cell: x => el("div", {}, el("strong", { text: x.label }), el("div", { class: "small muted", text: `${x.setup_label || ""}${x.score !== null && x.score !== undefined ? ` · score ${num(x.score, 0)}` : ""}${x.source ? ` · ${x.source}` : ""}` })) },
      { label: "Dollars", num: true, cell: x => el("div", {}, money(x.dollars, 0), el("div", { class: "small muted", text: pct(100 * x.share, 0) })) },
      { label: "At the stop", num: true, cell: x => el("span", { class: "neg-text", text: money(-x.risk_at_stop, 0) }) },
      { label: "Expected", num: true, cell: x => el("span", { class: x.expected_pnl >= 0 ? "pos-text" : "neg-text", text: signedMoney(x.expected_pnl, 0) }) },
      { label: "Chance of a profit", num: true, cell: x => pct(100 * x.p_profit, 0) },
      { label: "In the worst tenth", num: true, cell: x => signedMoney(x.tail_contribution, 0) },
      { label: "Edge", num: true, cell: x => isNum(x.edge_pct) ? el("span", { title: "After costs and the pull toward zero: the only drift the model is given", text: signedPct(x.edge_pct, 2) }) : "none" }],
    r.rows));
    box.append(el("h4", { text: "Profit or loss over the horizon" }), fanChart(r.fan, r.budget));
    const side = el("div", { class: "blend-side" });
    if (r.sectors && r.sectors.length) side.append(el("div", {}, el("h4", { text: "Sectors" }),
      el("ul", { class: "plain small" }, r.sectors.map(s => el("li", {}, `${s.sector}: ${money(s.dollars, 0)} (${pct(100 * s.share, 0)})`)))));
    if (r.stress && r.stress.length) side.append(el("div", {}, el("h4", { text: "If the market moved at once" }),
      el("ul", { class: "plain small" }, r.stress.map(s => el("li", {}, `SPY ${signedPct(s.market_pct, 0)}: `, el("strong", { class: s.pnl >= 0 ? "pos-text" : "neg-text", text: signedMoney(s.pnl, 0) }))))));
    const ct = corrTable(r.correlation);
    if (ct) side.append(el("div", {}, el("h4", { text: "How they move together" }), ct));
    box.append(side);
    return box;
  };
  frag.append(el("p", { class: "small" }, el("strong", { text: `${res.goal_label}` }),
    res.tail_cap ? ` · worst tenth within ${money(res.tail_cap, 0)} (${num(res.tail_pct, 2)}% of the budget)` : "",
    ` · ${res.horizon === "day" ? "day trades" : "swing trades"} · ${res.paths.toLocaleString()} paths · ${shortDate(res.start)} to ${new Date(res.end).toLocaleString("en-US", { timeZone: "America/New_York", month: "short", day: "numeric", hour: "2-digit", minute: "2-digit" })} ET`,
    res.given && (res.breaches || []).length ? " · these units do not fit the caps and limits (see above)"
      : res.limits && res.limits.obey ? ` · your limits applied (${money(res.limits.per_trade, 0)} a trade, ${money(res.limits.open_total - res.limits.open_now, 0)} of open risk free)`
      : " · your real-money limits lifted (paper only)"));
  frag.append(show(res, null, false));
  if (res.fallback) {
    const fb = show(res.fallback, "For study: the same risk in each of the best-scored trades (no edge behind it)", true);
    frag.append(fb);
  }
  if ((res.skipped || []).length) frag.append(el("details", {}, el("summary", { text: `${res.skipped.length} left out` }),
    el("ul", { class: "plain small" }, res.skipped.map(s => el("li", {}, el("strong", { text: s.symbol }), `: ${s.why}`)))));
  const actions = el("div", { class: "row", style: "margin-top:10px" });
  const rowsFor = () => res.rows.length ? res.rows : (res.fallback ? res.fallback.rows : []);
  if (rowsFor().length) {
    const effective = res.rows.length ? res : { ...res, rows: res.fallback.rows };
    if (opts.analyze !== false && state.openBasket && featureOn("trend_engine_v2")) {
      const b = el("button", { class: "primary", text: "Analyze this blend" });
      b.addEventListener("click", () => state.openBasket(blendBasket(effective)));
      actions.append(b);
    }
    if (opts.paper !== false && featureOn("paper_trading")) {
      const b = el("button", { class: "plain", text: opts.addToPortfolio ? "Add to this portfolio" : "Paper trade this blend" });
      b.addEventListener("click", async () => {
        if (opts.addToPortfolio) {
          await withBusy(b, err, async () => {
            const body = { source: req.source, units: FLOW_BLEND.units(effective.rows) };
            if (req.legs) body.legs = req.legs; else body.keys = effective.rows.map(r => r.key);
            const out = await api(`/v2/paper/portfolios/${opts.addToPortfolio}/import`, body);
            if (opts.onAdded) opts.onAdded(out);
          });
        } else await paperFromBlend(effective, req, err, b);
      });
      actions.append(b);
    }
    const copy = el("button", { class: "plain", text: "Copy orders" });
    copy.addEventListener("click", () => copyText(FLOW_BLEND.orders(effective.rows), copy));
    actions.append(copy);
  }
  frag.append(actions, err, el("p", { class: "small muted", text: res.model }));
  return frag;
}

// The panel: `opts.source` (morning, live, ideas, all, basket), `opts.keys()` (ticked rows, or null for the best
// `top_n`), `opts.legs()` (a basket's legs), `opts.horizon` (day or swing), `opts.title`. Real-money splits always
// obey your risk limits; only a paper portfolio's panel (`opts.addToPortfolio`) can lift them.
function blendPanel(opts) {
  const p = (state.prefs && state.prefs.blend) || {};
  const budget = el("input", { type: "number", min: 100, step: 100, value: String(Math.round(p.budget || 10000)) });
  const goal = segmented(`bgoal${++uid}`, BLEND_GOALS, p.goal || "balanced");
  const tail = el("input", { type: "number", min: 0.25, max: 20, step: 0.25, value: String(p.tail_pct || 2) });
  const maxShare = el("select", {}, [[0.1, "10%"], [0.15, "15%"], [0.2, "20%"], [0.25, "25%"], [0.33, "33%"], [0.5, "50%"], [1, "No cap"]]
    .map(([v, t]) => el("option", { value: v, text: t, selected: Math.abs((p.max_share || 0.2) - v) < 1e-6 })));
  const maxPos = el("input", { type: "number", min: 1, max: 25, step: 1, value: String(p.max_positions || 10) });
  const topN = el("input", { type: "number", min: 1, max: 25, step: 1, value: String(p.top_n || 10) });
  const paperOnly = Boolean(opts.addToPortfolio);
  const limits = el("input", { type: "checkbox", checked: true });
  const inst = new Set(p.instruments || BLEND_INSTRUMENTS.map(x => x[0]));
  const instBoxes = BLEND_INSTRUMENTS.map(([v, t]) => {
    const box = el("input", { type: "checkbox", value: v, checked: inst.has(v) });
    return el("label", { class: "check" }, box, t);
  });
  const useSel = opts.keys ? el("select", {}, el("option", { value: "top", text: "The best-scored" }), el("option", { value: "ticked", text: "The rows I ticked" })) : null;
  const go = el("button", { class: "primary", text: "Plan the split" });
  const err = el("div", { class: "error", role: "alert" });
  const out = el("div");
  const tailField = infoField("Worst tenth may lose (% of budget)", "tail", tail);
  const syncGoal = () => tailField.classList.toggle("hidden", goal.value() !== "balanced");
  goal.addEventListener("change", () => { syncGoal(); savePrefs({ blend: { goal: goal.value() } }); });
  syncGoal();
  const instruments = () => instBoxes.map(l => l.querySelector("input")).filter(b => b.checked).map(b => b.value);
  for (const [node, key, read] of [[budget, "budget", () => Number(budget.value)], [tail, "tail_pct", () => Number(tail.value)],
    [maxShare, "max_share", () => Number(maxShare.value)], [maxPos, "max_positions", () => Number(maxPos.value)], [topN, "top_n", () => Number(topN.value)]]) {
    node.addEventListener("change", () => { const v = read(); if (isNum(v) && v > 0) savePrefs({ blend: { [key]: v } }); });
  }
  for (const l of instBoxes) l.querySelector("input").addEventListener("change", () => { const v = instruments(); if (v.length) savePrefs({ blend: { instruments: v } }); });
  let last = null;
  const request = () => {
    const req = { source: opts.source, budget: Number(budget.value), goal: goal.value(), tail_pct: Number(tail.value),
      max_share: Number(maxShare.value), max_positions: Number(maxPos.value), top_n: Number(topN.value), instruments: instruments(),
      obey_limits: paperOnly ? limits.checked : true, paper: paperOnly };
    if (opts.horizon) req.horizon = typeof opts.horizon === "function" ? opts.horizon() : opts.horizon;
    if (opts.legs) { const legs = opts.legs(); if (legs) req.legs = legs; }
    else if (opts.keys && useSel && useSel.value === "ticked") {
      const keys = opts.keys();
      if (!keys || !keys.length) throw new Error("Tick at least one row first (the box at the start of each row).");
      req.keys = keys;
    }
    return req;
  };
  const run = (units) => withBusy(go, err, async () => {
    const req = request();
    if (units) req.units = units;
    out.replaceChildren(el("div", { class: "loadnote small muted" }, el("span", { class: "spinner" }), el("span", { text: "Pricing contracts and walking 4,000 paths…" })));
    try {
      const res = await api("/v2/blend", req);
      last = { res, req };
      out.replaceChildren(renderBlend(res, req, opts));
      if (opts.onResult) opts.onResult(res, req);
    } catch (e) { out.replaceChildren(); throw e; }
  });
  go.addEventListener("click", () => run());
  const fields = el("div", { class: "inline-form blend-form" },
    infoField("Budget ($)", "budget", budget), infoField("Goal", "goal", goal), tailField,
    infoField("Most in one trade", "maxShare", maxShare), field("Trades at most", maxPos),
    opts.legs ? null : field("Opportunities to blend", topN), useSel ? infoField("Use", "use", useSel) : null,
    el("div", { class: "field" }, el("span", {}, "Instruments ", infoIcon("instruments")), el("div", { class: "inst-checks" }, instBoxes)),
    paperOnly ? el("div", { class: "field" }, el("span", {}, "Limits ", infoIcon("limits")), el("label", { class: "check" }, limits, "Apply my real-money limits")) : null,
    go);
  const panel = el("div", { class: "panel blend-panel" + (opts.collapsed ? " hidden" : "") },
    el("h3", {}, opts.title || "Set a budget and split it", " ", infoIcon("goal", "About the split")),
    el("p", { class: "small muted", text: opts.intro || "The optimizer walks the opportunities together on 4,000 paths (their own volatility, how they have moved together, each trade's stop and target) and picks how much of each to buy for the goal. Each trade's only drift is its past record's edge, after costs and pulled toward zero when the record is short." }),
    fields, err, out);
  panel.run = run;
  panel.last = () => last;
  panel.toggle = () => { panel.classList.toggle("hidden"); if (!panel.classList.contains("hidden")) panel.scrollIntoView({ behavior: "smooth", block: "start" }); };
  return panel;
}

// A tick box for a row, kept in `set` by the opportunity's key.
function pickBox(set, key) {
  const box = el("input", { type: "checkbox", "aria-label": "Add to the blend", checked: set.has(key) });
  box.addEventListener("click", e => e.stopPropagation());
  box.addEventListener("change", () => { if (box.checked) set.add(key); else set.delete(key); });
  return box;
}

// Opens one opportunity on Analyze opportunity with its levels filled in.
function analyzeButton(opp, text = "Analyze") {
  const b = el("button", { class: "plain small-btn", text });
  b.addEventListener("click", e => { e.stopPropagation(); if (state.openOpportunity) state.openOpportunity(opp); });
  b.addEventListener("keydown", e => e.stopPropagation());
  return b;
}

function scoreCell(o) {
  const parts = (o.score_parts || []).map(x => `${x.label}: ${x.points > 0 ? "+" : ""}${num(x.points, 1)} of ${x.max} (${x.text})`).join("\n");
  const e = o.edge || {};
  const t = `${parts}\n\nEdge: ${e.basis || ""}${e.known ? `. ${e.cases} cases, weight ${pct(100 * e.weight, 0)}${isNum(e.frequency) ? `, the trade's way ${pct(100 * e.frequency, 0)} of the time` : ""}` : ""}`;
  return el("span", { class: "score-chip", title: t, tabindex: "0", text: num(o.score, 0) });
}

function edgeCell(o) {
  const e = o.edge || {};
  if (!e.known) return el("span", { class: "muted small", title: e.basis || "", text: "no record" });
  return el("span", { title: `${e.basis}. ${e.cases} cases; the past average ${signedPct(e.raw_pct, 2)} counts ${pct(100 * e.weight, 0)}, less ${pct(e.cost_pct, 2)} of costs.`,
    class: e.expected_pct > 0 ? "pos-text" : "neg-text", text: signedPct(e.expected_pct, 2) });
}
