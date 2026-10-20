"use strict";
/* Trade Assistant 2.15: Analyze opportunity. The screen that was Analyze a stock takes one opportunity or a basket of
   them: chips for each stock (its full analysis, with its levels filled in, as before) and a Blend chip for the
   whole basket, split under a budget with its range of results, tail risk, correlations and stress (blend.js).
   Opportunities arrive from the Morning plan, Live news and Trade ideas, or are added here by hand.
   Nothing here places an order. */

state.basket = null;   // { source, opportunities, units, budget, goal, tail_pct, horizon }

function opportunityThesis(o) {
  return FLOW.thesis({ symbol: o.symbol, direction: o.direction, entry: o.entry, invalidation: o.stop, target1: o.target_1,
    target2: o.target_2, horizonDays: o.horizon === "day" ? 1 : (o.horizon_days || 20),
    contracts: o.option && o.option.contracts ? o.option.contracts : 1 });
}

// One opportunity: it becomes the basket's only stock and opens graded, with its levels filled in.
state.openOpportunity = opp => {
  state.basket = { source: opp.source, opportunities: [opp], units: null, horizon: opp.horizon };
  state.thesis = opportunityThesis(opp);
  state.openDecide(opp.symbol, opp.direction);
  const v = state.views.analyze;
  if (v && v.basketBar) v.basketBar.render(opp.key, true);
};

// A blend: the basket opens on its Blend view, evaluated as split (or split again).
state.openBasket = basket => {
  state.basket = { ...basket, opportunities: [...(basket.opportunities || [])] };
  show("analyze");
  const v = state.views.analyze;
  if (v && v.basketBar) v.basketBar.render("blend");
};

function basketLegs(b) {
  return b.opportunities.map(o => {
    const leg = { key: o.key, symbol: o.symbol, direction: o.direction, horizon: o.horizon || null,
      horizon_days: o.horizon_days || null, entry: o.entry || null, stop: o.stop || null, target_1: o.target_1 || null,
      target_2: o.target_2 || null };
    return Object.fromEntries(Object.entries(leg).filter(([, v]) => v !== null && v !== undefined));
  });
}

function basketBar(view, stockParts) {
  const node = el("div", { class: "panel basket hidden" });
  const blendBox = el("div", { class: "hidden" });
  view.append(blendBox);
  let active = null;
  const sym = el("input", { class: "sym", type: "text", placeholder: "Ticker", "aria-label": "Add a stock", autocomplete: "off", spellcheck: "false" });
  const dir = el("select", { "aria-label": "Direction" }, el("option", { value: "LONG", text: "Long" }), el("option", { value: "SHORT", text: "Short" }));
  const add = el("button", { class: "plain small-btn", text: "Add to the basket" });
  const clear = el("button", { class: "plain small-btn", text: "Clear the basket" });
  const err = el("div", { class: "error", role: "alert" });

  add.addEventListener("click", () => {
    const s = sym.value.trim().toUpperCase();
    if (!/^[A-Z][A-Z.]{0,9}$/.test(s)) { err.textContent = "Type a ticker."; return; }
    const b = state.basket || { source: "basket", opportunities: [], horizon: "swing" };
    if (b.opportunities.some(o => o.symbol === s)) { err.textContent = `${s} is already in the basket.`; return; }
    b.opportunities.push({ key: `manual:${s}:${dir.value}:${b.horizon || "swing"}`, source: "manual", symbol: s, direction: dir.value,
      horizon: b.horizon || "swing", horizon_days: b.horizon === "day" ? 1 : 20, setup_label: "Added by you", score: 0 });
    b.units = null;
    state.basket = b;
    sym.value = "";
    err.textContent = "";
    render(active === "blend" ? "blend" : s);
  });
  sym.addEventListener("keydown", e => { if (e.key === "Enter") add.click(); });
  clear.addEventListener("click", () => { state.basket = null; render(null); });

  function showStock(o) {
    blendBox.classList.add("hidden");
    for (const n of stockParts) n.classList.remove("hidden");
    state.thesis = opportunityThesis(o);
    state.decideHint = o.stop ? { symbol: o.symbol, hint: "run" } : null;
    view.analyze(o.symbol);
  }

  function showBlend() {
    for (const n of stockParts) n.classList.add("hidden");
    blendBox.classList.remove("hidden");
    const b = state.basket;
    const editor = el("div");
    const panel = blendPanel({ source: "basket", legs: () => basketLegs(state.basket),
      horizon: () => (state.basket.opportunities.some(o => o.horizon !== "day") ? "swing" : "day"),
      title: `The blend: ${b.opportunities.length} stock${b.opportunities.length === 1 ? "" : "s"}`,
      intro: "The basket split under the budget, walked together on 4,000 paths: each stock's own volatility, how they have moved together, each trade's stop and target. Change the units below and evaluate them, or plan the split again.",
      onResult: res => { state.basket.units = FLOW_BLEND.units(res.rows); renderEditor(res); } });
    function renderEditor(res) {
      const rows = res.rows.length ? res.rows : (res.fallback ? res.fallback.rows : []);
      const opps = res.opportunities || [];
      const inputs = {};
      const lines = [];
      for (const o of opps) {
        const kinds = [o.direction === "LONG" ? "shares" : "short"].concat(o.option ? [o.option.option_type === "CALL" ? "call" : "put"] : []);
        for (const k of kinds) {
          const r = rows.find(x => x.key === o.key && x.instrument === k);
          const box = el("input", { type: "number", min: 0, step: 1, value: String(r ? r.units : 0), "aria-label": `${o.symbol} ${k}` });
          inputs[`${o.key}|${k}`] = box;
          lines.push(el("tr", {}, el("td", {}, el("strong", { text: o.symbol }), ` ${o.direction === "LONG" ? "long" : "short"}`),
            el("td", { text: INSTRUMENT_WORDS[k] + (k === "call" || k === "put" ? ` ${o.option.expiration} $${o.option.strike}` : "") }),
            el("td", { class: "num" }, box)));
        }
      }
      if (!lines.length) { editor.replaceChildren(); return; }
      const evalBtn = el("button", { class: "plain", text: "Evaluate these units" });
      evalBtn.addEventListener("click", () => {
        const units = Object.fromEntries(Object.entries(inputs).map(([k, b]) => [k, Math.max(0, Math.round(Number(b.value) || 0))]));
        panel.run(units);
      });
      editor.replaceChildren(el("div", { class: "panel" }, el("h3", { text: "Edit the units" }),
        el("div", { class: "scroll" }, el("table", {}, el("thead", {}, el("tr", {}, el("th", { text: "Stock" }), el("th", { text: "Instrument" }), el("th", { class: "num", text: "Units" }))),
          el("tbody", {}, lines))),
        el("div", { class: "row", style: "margin-top:8px" }, evalBtn)));
    }
    const perStock = el("div", { class: "panel" }, el("h3", { text: "The stocks" }),
      table([{ label: "Stock", cell: o => el("strong", { text: o.symbol }) },
        { label: "Trade", cell: o => `${o.direction === "LONG" ? "Long" : "Short"} · ${o.setup_label || o.setup || ""}` },
        { label: "Entry", num: true, cell: o => isNum(o.entry) ? money(o.entry) : "now" },
        { label: "Stop", num: true, cell: o => isNum(o.stop) ? money(o.stop) : "suggested" },
        { label: "Target 1", num: true, cell: o => isNum(o.target_1) ? money(o.target_1) : "suggested" },
        { label: "Horizon", cell: o => o.horizon === "day" ? "Out by 15:55" : `${o.horizon_days || 20} days` },
        { label: "Edge", num: true, cell: o => o.edge ? edgeCell(o) : el("span", { class: "muted small", text: "—" }) },
        { label: "", cell: o => { const btn = el("button", { class: "plain small-btn", text: "Full analysis" }); btn.addEventListener("click", () => render(o.key)); return btn; } }],
      b.opportunities));
    blendBox.replaceChildren(perStock, panel, editor);
    if (b.budget && panel.querySelector("input[type=number]")) panel.querySelector("input[type=number]").value = String(Math.round(b.budget));
    panel.run(b.units || undefined);
  }

  function render(which, quiet) {
    const b = state.basket;
    if (!b || !b.opportunities.length) {
      node.classList.add("hidden");
      blendBox.classList.add("hidden");
      for (const n of stockParts) n.classList.remove("hidden");
      active = null;
      return;
    }
    node.classList.remove("hidden");
    const target = which === "blend" ? "blend" : (b.opportunities.find(o => o.key === which || o.symbol === which) || null);
    active = target === "blend" ? "blend" : target ? target.key : active;
    const chip = (label, key, title, onClick, removable) => {
      const c = el("button", { class: `chip${active === key ? " on" : ""}`, title: title || null, "aria-pressed": String(active === key) }, label);
      c.addEventListener("click", onClick);
      if (!removable) return c;
      const x = el("button", { class: "chip-x", "aria-label": `Remove ${label} from the basket`, text: "×" });
      x.addEventListener("click", e => {
        e.stopPropagation();
        b.opportunities = b.opportunities.filter(o => o.key !== key);
        b.units = null;
        render(active === key ? (b.opportunities[0] ? b.opportunities[0].key : null) : active);
      });
      return el("span", { class: "chip-wrap" }, c, x);
    };
    node.replaceChildren(
      el("div", { class: "row basket-row" },
        el("strong", { text: "Basket" }),
        ...b.opportunities.map(o => chip(`${o.symbol} ${o.direction === "LONG" ? "▲" : "▼"}`, o.key, `${o.setup_label || ""}${isNum(o.score) ? ` · score ${num(o.score, 0)}` : ""}`,
          () => render(o.key), true)),
        chip(`Blend (${b.opportunities.length})`, "blend", "The whole basket under a budget", () => render("blend"), false)),
      el("div", { class: "row", style: "margin-top:8px" }, sym, dir, add, clear), err);
    if (target === "blend") { showBlend(); return; }
    if (target && quiet) {                            // the analysis is already on its way: just show it
      blendBox.classList.add("hidden");
      for (const n of stockParts) n.classList.remove("hidden");
      return;
    }
    if (target) showStock(target);
  }
  return { node, render };
}

// The screen itself is built by v2.js (the per-stock analysis); this adds the basket above it.
(() => {
  const build = BUILDERS.analyze;
  if (!build) return;
  BUILDERS.analyze = () => {
    const view = build();
    const parts = [...view.children].slice(2);       // the ticker form, the reversal scan if shown, the analysis
    const bar = basketBar(view, parts);
    view.insertBefore(bar.node, view.children[2] || null);
    view.basketBar = bar;
    const shown = view.onShow;
    view.onShow = () => { if (shown) shown(); if (state.basket && state.basket.opportunities.length && !bar.node.classList.contains("hidden")) return;
      bar.render(null, true); };
    return view;
  };
})();
