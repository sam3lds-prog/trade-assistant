"use strict";
/* The workstation's widgets. Each one reads its link group's context (WS.contextOf) and updates it when the user
   picks something, so a stock clicked in the opportunity feed, or a contract clicked in the chain, reaches every
   linked widget at once. Most reuse the classic screens' renderers and endpoints; nothing here places an order. */
(() => {
  const nums = (v, d = 2) => (isNum(v) ? v.toLocaleString("en-US", { minimumFractionDigits: d, maximumFractionDigits: d }) : "");
  const signed = (v, d = 2) => (isNum(v) ? `${v > 0 ? "+" : ""}${v.toFixed(d)}%` : "");
  const enc = encodeURIComponent;
  const loading = text => el("p", { class: "loadnote" }, el("span", { class: "spinner", "aria-hidden": "true" }), text || "Loading…");
  const contractName = c => `${c.symbol} ${c.expiration} ${nums(c.strike, c.strike % 1 ? 2 : 0)} ${String(c.option_type).toUpperCase()}`;

  /* ---------- Stock chart ---------- */
  WS.register("chart", { title: "Stock chart", category: "Research", size: { w: 8, h: 9 }, minW: 4, needs: ["analyze_interactive"], create(w) {
    let chart = null, shown = null;
    function mount(symbol) {
      if (chart) { chart.dispose(); chart = null; WS.provide(w, "chart", null); }
      shown = symbol;
      if (!symbol) { w.body.replaceChildren(WS.empty("Pick a stock: press Ctrl K and type a ticker, or click one in a linked widget.")); return; }
      if (!window.LightweightCharts) { w.body.replaceChildren(WS.empty("The chart library did not load. Reload the page.")); return; }
      chart = interactiveChart(symbol);
      // The indicator switches fold away, so the chart itself gets the room.
      const toggles = chart.panel.querySelector(".toggles");
      if (toggles) {
        const fold = el("details", { class: "ws-more" }, el("summary", { text: "Indicators" }));
        toggles.replaceWith(fold); fold.append(toggles);
      }
      const more = el("details", { class: "ws-more" }, el("summary", { text: "Similar patterns, possible outcomes and the chart's structure" }),
        ...(chart.extras || []), chart.structure || "");
      w.body.replaceChildren(chart.panel, more);
      WS.provide(w, "chart", chart);
      // Lines other widgets ask for (a study's forecast), redrawn whenever the chart reloads.
      chart.subscribe(what => { if (what === "load") overlays(); });
    }
    function overlays() {
      if (!chart) return;
      for (const fn of WS.chartOverlays || []) { try { fn(chart, w.ctx()); } catch (e) { console.error(e); } }
    }
    return {
      onContext(c, changed) {
        if (changed.includes("symbol") && c.symbol !== shown) mount(c.symbol);
        else if (!chart && c.symbol && shown !== c.symbol) mount(c.symbol);
        else if (changed.includes("scenario")) overlays();
      },
      refresh() { mount(w.ctx().symbol); },
      onResize() { if (chart && chart.redraw) chart.redraw(); },
      dispose() { if (chart) chart.dispose(); },
    };
  } });

  /* ---------- Scenario explorer: the what-if model drawn on the linked chart ---------- */
  WS.register("scenario", { title: "Scenario explorer", category: "Research", size: { w: 8, h: 9 }, minW: 4, needs: ["what_if", "analyze_interactive"], create(w) {
    let widget = null, forChart = null, forSymbol = null;
    function mount() {
      const symbol = w.ctx().symbol, chart = WS.find(w, "chart");
      if (widget && chart === forChart && symbol === forSymbol) return;
      if (widget) { widget.dispose(); widget = null; }
      forChart = chart; forSymbol = symbol;
      if (!symbol) { w.body.replaceChildren(WS.empty("Pick a stock to model its scenarios.")); return; }
      if (!chart) { w.body.replaceChildren(WS.empty("Link a stock chart to this widget (the same link colour): the scenarios are drawn on it.")); return; }
      widget = whatIfWidget(symbol, chart);
      w.body.replaceChildren(widget.widget, widget.position);
    }
    return { onContext(c, changed) { if (changed.includes("symbol")) mount(); }, onProvider() { mount(); },
      refresh() { if (widget) widget.dispose(); widget = null; forChart = null; mount(); }, dispose() { if (widget) widget.dispose(); } };
  } });

  /* ---------- Options chain ---------- */
  WS.register("chain", { title: "Options chain", category: "Options", size: { w: 4, h: 9 }, minW: 3, create(w) {
    let symbol = null, exps = [], ticket = 0, rows = [];
    const expSel = el("select", { "aria-label": "Expiration" });
    const width = el("select", { "aria-label": "Strikes shown" }, [["8", "Near the money"], ["15", "Within 15%"], ["40", "Within 40%"]]
      .map(([v, t]) => el("option", { value: v, selected: v === String(w.config.width || "8") }, t)));
    const box = el("div");
    let side = w.config.side || "auto", lastChain = null;
    const sideBtns = el("span", { class: "ws-seg-mini", role: "group", "aria-label": "Calls or puts" },
      [["auto", "Follow the trade"], ["call", "Calls"], ["put", "Puts"], ["both", "Both"]].map(([v, t]) =>
        el("button", { type: "button", "data-side": v, "aria-pressed": v === side ? "true" : "false", onclick: () => {
          side = v; w.config.side = v; w.save();
          for (const b of sideBtns.children) b.setAttribute("aria-pressed", b.dataset.side === v ? "true" : "false");
          if (lastChain) draw(lastChain);
        } }, t)));
    w.body.append(el("div", { class: "ws-row" }, field("Expiration", expSel), field("Strikes", width)), el("div", { class: "ws-tools-row" }, sideBtns), box);
    expSel.addEventListener("change", () => { w.config.expiration = expSel.value; w.save(); loadChain(); });
    width.addEventListener("change", () => { w.config.width = width.value; w.save(); loadChain(); });
    async function loadExps() {
      const mine = ++ticket;
      box.replaceChildren(loading("Loading expirations…"));
      try {
        const d = await api(`/api/expirations?symbol=${enc(symbol)}`);
        if (mine !== ticket) return;
        exps = d.expirations;
        const keep = exps.find(e => e.date === w.config.expiration);
        const pick = keep || exps.find(e => e.dte >= 25) || exps[exps.length - 1];
        expSel.replaceChildren(...exps.map(e => el("option", { value: e.date, selected: pick && e.date === pick.date }, `${e.date} (${e.dte} days)`)));
        await loadChain();
      } catch (e) { if (mine === ticket) box.replaceChildren(el("p", { class: "error", text: e.message })); }
    }
    async function loadChain() {
      if (!expSel.value) return;
      const mine = ++ticket;
      box.classList.add("refetching");
      try {
        const d = await api(`/api/chain?symbol=${enc(symbol)}&expiration=${enc(expSel.value)}&width_pct=${enc(width.value)}`);
        if (mine !== ticket) return;
        rows = d.contracts; lastChain = d;
        draw(d);
      } catch (e) { if (mine === ticket) box.replaceChildren(el("p", { class: "error", text: e.message })); }
      finally { box.classList.remove("refetching"); }
    }
    function draw(d) {
      const strikes = [...new Set(rows.map(r => r.strike))].sort((a, b) => a - b);
      const at = (k, t) => rows.find(r => r.strike === k && String(r.option_type).toLowerCase() === t);
      const sel = w.ctx().contract;
      const atm = strikes.reduce((b, k) => (Math.abs(k - d.spot) < Math.abs(b - d.spot) ? k : b), strikes[0]);
      const cell = (c, key, digits) => {
        if (!c) return el("td", { class: "num" }, "");
        const chosen = sel && sel.option_symbol === c.symbol;
        const itm = c.option_type.toLowerCase() === "call" ? c.strike < d.spot : c.strike > d.spot;
        return el("td", { class: `num q${chosen ? " sel" : ""}${itm ? " itm" : ""}`, title: `${contractName({ ...c, symbol })}: bid ${nums(c.bid)} ask ${nums(c.ask)}`,
          onclick: () => pick(c) }, key === "iv" ? (isNum(c.iv) ? `${(100 * c.iv).toFixed(0)}%` : "") : nums(c[key], digits));
      };
      // Which side to show: both, or the side the linked trade leans (calls for a long, puts for a short).
      const dir = w.ctx().direction;
      const show = side === "auto" ? (dir === "SHORT" ? ["put"] : dir === "LONG" ? ["call"] : ["call", "put"]) : side === "both" ? ["call", "put"] : [side];
      const cols = t => (show.includes(t) ? ["Bid", "Ask", "IV", "Δ"].map(h => el("th", { class: "num", text: `${t === "call" ? "Call" : "Put"} ${h}` })) : []);
      const cells = (c, t) => (show.includes(t) ? [cell(c, "bid"), cell(c, "ask"), cell(c, "iv"), cell(c, "delta")] : []);
      const thead = el("thead", {}, el("tr", {}, cols("call"), el("th", { class: "num", text: "Strike" }), cols("put")));
      const tbody = el("tbody", {}, strikes.map(k => {
        const c = at(k, "call"), p = at(k, "put");
        return el("tr", { class: k === atm ? "atm" : null }, cells(c, "call"), el("td", { class: "k", text: nums(k, k % 1 ? 2 : 0) }), cells(p, "put"));
      }));
      box.replaceChildren(el("p", { class: "small muted", text: `${symbol} ${money(d.spot)}; ${d.dte} days to ${d.expiration}. Click a quote to select that contract.` }),
        el("div", { class: "scroll" }, el("table", { class: "ws-table ws-chain" }, thead, tbody)));
    }
    function pick(c) {
      w.set({ contract: { symbol, option_symbol: c.symbol, option_type: String(c.option_type).toLowerCase(), strike: c.strike, expiration: c.expiration,
        bid: c.bid, ask: c.ask, mid: c.mid, iv: c.iv, delta: c.delta }, strategy: null, _self: true });
    }
    return {
      onContext(c, changed) {
        if (changed.includes("symbol") && c.symbol !== symbol) { symbol = c.symbol; if (symbol) loadExps(); else box.replaceChildren(WS.empty("Pick a stock to see its options.")); }
        else if (changed.includes("contract") && rows.length) loadChain();
        else if (changed.includes("direction") && lastChain) draw(lastChain);
      },
      refresh() { if (symbol) loadExps(); },
      config: () => ({ expiration: expSel.value || w.config.expiration, width: width.value, side }),
    };
  } });

  /* ---------- Opportunity feed ---------- */
  WS.register("feed", { title: "Opportunities", category: "Discover", size: { w: 7, h: 8 }, symbolBadge: false, needs: ["opportunity_blend"], create(w) {
    const box = el("div");
    const src = el("select", { "aria-label": "Source" }, [["all", "Every scan"], ["morning", "Morning plan"], ["ideas", "Trade ideas"], ["live", "Live news"]]
      .map(([v, t]) => el("option", { value: v, selected: v === (w.config.source || "all") }, t)));
    w.body.append(el("div", { class: "ws-row" }, field("From", src)), box);
    src.addEventListener("change", () => { w.config.source = src.value; w.save(); load(); });
    async function load() {
      box.replaceChildren(loading("Ranking opportunities…"));
      try {
        const d = await api(`/v2/opportunities/${enc(src.value)}?limit=60`);
        if (!d.opportunities.length) {
          box.replaceChildren(WS.empty("Nothing ranked yet. Build the Morning plan or scan Trade ideas, and their opportunities appear here."),
            el("div", { class: "ws-row" }, BUILDERS.morning ? el("button", { class: "ws-btn", type: "button", onclick: () => WS.routeClassic("morning") }, "Open the Morning plan") : "",
              BUILDERS.ideas ? el("button", { class: "ws-btn", type: "button", onclick: () => WS.routeClassic("ideas") }, "Open Trade ideas") : ""));
          return;
        }
        const sel = w.ctx().symbol;
        box.replaceChildren(table([
          { label: "Stock", cell: o => el("strong", { text: o.symbol }) },
          { label: "Way", cell: o => (o.direction === "LONG" ? "Long" : "Short") },
          { label: "Setup", cell: o => o.setup_label || o.setup },
          { label: "Horizon", cell: o => (o.horizon === "day" ? "Day" : `${o.horizon_days} days`) },
          { label: "Score", num: true, cell: o => num(o.score, 0) },
          { label: "Edge after costs", num: true, cell: o => (o.edge && o.edge.known ? signedPct(o.edge.expected_pct, 2) : "no record") },
          { label: "From", cell: o => o.source },
        ], d.opportunities, { onRow: (o, tr) => {
          for (const r of box.querySelectorAll("tr.sel")) r.classList.remove("sel");
          tr.classList.add("sel");
          w.set({ symbol: o.symbol, direction: o.direction, horizon: o.horizon === "day" ? "intraday" : "swing", opportunity: { key: o.key, source: o.source } });
        } }));
        for (const tr of box.querySelectorAll("tbody tr")) if (tr.querySelector("strong") && tr.querySelector("strong").textContent === sel) tr.classList.add("sel");
        const note = Object.entries(d.meta || {}).map(([k, m]) => (m && m.stale ? `${k}: ${m.stale}` : null)).filter(Boolean);
        if (note.length) box.append(el("p", { class: "small muted", text: note.join(" ") }));
      } catch (e) { box.replaceChildren(el("p", { class: "error", text: e.message })); }
    }
    load();
    WS.onEvent("opportunity.", () => load());
    return { refresh: load, config: () => ({ source: src.value }) };
  } });

  /* ---------- Watchlist ---------- */
  WS.register("watchlist", { title: "Watchlist", category: "Discover", size: { w: 4, h: 5 }, symbolBadge: false, create(w) {
    let timer = null;
    const box = el("div");
    const add = el("input", { type: "text", "aria-label": "Add a ticker", placeholder: "Add a ticker", size: "10", autocomplete: "off" });
    add.addEventListener("keydown", e => {
      if (e.key !== "Enter") return;
      const s = add.value.trim().toUpperCase();
      if (/^[A-Z]{1,5}(\.[A-Z])?$/.test(s) && !list().includes(s)) { w.config.symbols = [...list(), s]; w.save(); load(); }
      add.value = "";
    });
    w.body.append(el("div", { class: "ws-row" }, add), box);
    const list = () => (w.config.symbols && w.config.symbols.length ? w.config.symbols : ((state.status && state.status.watchlist) || ["AAPL", "MSFT", "NVDA"]).slice(0, 20));
    async function load() {
      clearTimeout(timer);
      try {
        const d = await api(`/v3/quotes?symbols=${enc(list().join(","))}`);
        const sel = w.ctx().symbol;
        box.replaceChildren(el("div", { class: "scroll" }, el("table", { class: "ws-table" },
          el("thead", {}, el("tr", {}, el("th", { text: "Stock" }), el("th", { class: "num", text: "Last" }), el("th", { class: "num", text: "Change" }), el("th"))),
          el("tbody", {}, d.quotes.map(q => el("tr", { class: `click${q.symbol === sel ? " sel" : ""}`, tabindex: "0",
            onclick: () => w.set({ symbol: q.symbol }), onkeydown: e => { if (e.key === "Enter") w.set({ symbol: q.symbol }); } },
            el("td", {}, el("strong", { text: q.symbol })), el("td", { class: "num", text: nums(q.last) }),
            el("td", { class: `num ${q.change_pct > 0 ? "ws-up" : q.change_pct < 0 ? "ws-down" : ""}`, text: signed(q.change_pct) }),
            el("td", {}, el("button", { class: "ws-wbtn", type: "button", title: `Remove ${q.symbol}`, "aria-label": `Remove ${q.symbol}`, text: "×",
              onclick: e => { e.stopPropagation(); w.config.symbols = list().filter(s => s !== q.symbol); w.save(); load(); } }))))))));
      } catch (e) { box.replaceChildren(el("p", { class: "error", text: e.message })); }
      timer = setTimeout(() => { if (!document.hidden && w.node.isConnected && w.node.offsetParent !== null) load(); else timer = setTimeout(load, 20000); }, 20000);
    }
    load();
    return { onContext(c, changed) { if (changed.includes("symbol")) for (const tr of box.querySelectorAll("tbody tr")) tr.classList.toggle("sel", tr.querySelector("strong").textContent === c.symbol); },
      refresh: load, dispose() { clearTimeout(timer); }, config: () => ({ symbols: list() }) };
  } });

  /* ---------- News and events ---------- */
  WS.register("news", { title: "News & events", category: "Research", size: { w: 4, h: 7 }, needs: ["market_news"], create(w) {
    let ticket = 0;
    async function load() {
      const symbol = w.ctx().symbol, mine = ++ticket;
      if (!symbol) { w.body.replaceChildren(WS.empty("Pick a stock to see its headlines and filings.")); return; }
      w.body.replaceChildren(loading("Loading headlines…"));
      try {
        const n = await api(`/v2/news/${enc(symbol)}`);
        if (mine !== ticket) return;
        w.body.replaceChildren(...(n.notes || []).slice(0, 1).map(t => el("p", { class: "small muted", text: t })),
          toneLine(n.tone, "words") || "", headlineList(n.items, { limit: 14, empty: "No headlines or filings in the last week." }));
      } catch (e) { if (mine === ticket) w.body.replaceChildren(el("p", { class: "error", text: e.message })); }
    }
    return { onContext(c, changed) { if (changed.includes("symbol")) load(); }, refresh: load };
  } });

  /* ---------- Strategy builder ---------- */
  WS.register("strategy", { title: "Strategy builder", category: "Options", size: { w: 4, h: 9 }, minW: 3, needs: ["strategy_builder"], create(w) {
    const tpl = el("select", { "aria-label": "Strategy" });
    const dte = el("input", { type: "number", min: "1", max: "720", value: String(w.config.dte || 30), "aria-label": "Days to expiration", style: "width:5em" });
    const qty = el("input", { type: "number", min: "1", max: "100", value: String(w.config.contracts || 1), "aria-label": "Contracts", style: "width:4em" });
    const go = el("button", { class: "ws-btn primary", type: "button" }, "Build");
    const out = el("div");
    w.body.append(el("div", { class: "ws-row" }, field("Strategy", tpl), field("Days", dte), field("Contracts", qty), go), out);
    api("/v2/strategies").then(d => {
      tpl.replaceChildren(...d.templates.map(t => el("option", { value: t.key, selected: t.key === (w.config.strategy || "bull_call_spread") }, t.label)));
    }).catch(e => out.replaceChildren(el("p", { class: "error", text: e.message })));
    go.addEventListener("click", build);
    async function build() {
      const symbol = w.ctx().symbol;
      if (!symbol) { out.replaceChildren(WS.empty("Pick a stock first.")); return; }
      w.config.strategy = tpl.value; w.config.dte = Number(dte.value); w.config.contracts = Number(qty.value); w.save();
      out.replaceChildren(loading("Choosing the contracts…"));
      try {
        const b = await api("/v2/strategies/build", { symbol, strategy: tpl.value, contracts: Number(qty.value) || 1, dte: Number(dte.value) || 30 });
        const legs = b.legs.map(l => ({ expiration: l.expiration, option_type: l.option_type, strike: l.strike, side: l.side, contracts: l.contracts, entry_price: l.mid || undefined }));
        out.replaceChildren(
          el("p", {}, el("strong", { text: b.label }), ` ${b.expiration}: ${b.credit ? "credit" : "debit"} ${money(Math.abs(b.net_mid) , 0)}; most it can lose ${b.unlimited_loss ? "no limit" : money(Math.abs(b.max_loss), 0)}, make ${b.unlimited_gain ? "no limit" : money(b.max_gain, 0)}.`),
          el("ul", { class: "plain small" }, b.legs.map(l => el("li", { text: `${l.side === "buy" ? "Buy" : "Sell"} ${l.contracts} ${nums(l.strike, l.strike % 1 ? 2 : 0)} ${l.option_type} ${l.expiration} at ${money(l.mid)}` }))),
          b.breakevens && b.breakevens.length ? el("p", { class: "small", text: `Breakeven ${b.breakevens.map(x => money(x)).join(" and ")}.` }) : "",
          b.risk_check ? el("p", { class: "small" }, statusTag(b.risk_check.status, b.risk_check.status === "pass" ? "Within limits" : "Over a limit"), ` ${b.risk_check.text || ""}`) : "",
          el("div", { class: "ws-row" },
            el("button", { class: "ws-btn", type: "button", onclick: () => w.set({ strategy: { key: b.strategy, label: b.label, legs, symbol } }) }, "Show its risk"),
            b.legs.length === 1 ? el("button", { class: "ws-btn", type: "button", onclick: () => {
              const l = b.legs[0];
              w.set({ contract: { symbol, option_symbol: l.option_symbol, option_type: String(l.option_type).toLowerCase(), strike: l.strike, expiration: l.expiration, bid: l.bid, ask: l.ask, mid: l.mid } });
            } }, "Price it on the ticket") : el("span", { class: "small muted", text: "Several legs: priced together here; the ticket prices one contract." })),
          ...(b.notes || []).slice(0, 3).map(t => el("p", { class: "small muted", text: t })));
      } catch (e) { out.replaceChildren(el("p", { class: "error", text: e.message })); }
    }
    return { onContext(c, changed) { if (changed.includes("symbol")) out.replaceChildren(c.symbol ? WS.empty(`Choose a strategy for ${c.symbol} and press Build.`) : WS.empty("Pick a stock first.")); },
      config: () => ({ strategy: tpl.value, dte: Number(dte.value), contracts: Number(qty.value) }) };
  } });

  /* ---------- Risk and reward of the selected contract or strategy ---------- */
  WS.register("riskreward", { title: "Risk / reward", category: "Options", size: { w: 8, h: 8 }, minW: 4, create(w) {
    let ticket = 0;
    const shift = el("input", { type: "range", min: "-20", max: "40", step: "1", value: "0", "aria-label": "Implied volatility change, points" });
    const shiftOut = el("span", { class: "small", text: "IV +0 pts" });
    const out = el("div");
    w.body.append(el("div", { class: "ws-row" }, el("label", { class: "field" }, el("span", { text: "If implied volatility moves" }), shift), shiftOut), out);
    let timer = null;
    shift.addEventListener("input", () => { shiftOut.textContent = `IV ${Number(shift.value) >= 0 ? "+" : ""}${shift.value} pts`; clearTimeout(timer); timer = setTimeout(load, 200); });
    function legsOf(c) {
      if (c.strategy && c.strategy.legs && c.strategy.symbol === c.symbol) return [c.strategy.label, c.strategy.legs];
      if (c.contract && c.contract.symbol === c.symbol) {
        const k = c.contract;
        return [contractName(k), [{ expiration: k.expiration, option_type: k.option_type, strike: k.strike, side: "buy", contracts: 1, entry_price: k.mid > 0 ? k.mid : undefined }]];
      }
      return [null, null];
    }
    async function load() {
      const c = w.ctx(), mine = ++ticket;
      const [label, legs] = legsOf(c);
      if (!legs) { w.setBadge(c.symbol || ""); out.replaceChildren(WS.empty("Select a contract in the options chain, or build a strategy, to see what it can make and lose.")); return; }
      w.setBadge(label);
      out.classList.add("refetching");
      try {
        const s = await api("/api/simulate", { symbol: c.symbol, legs, iv_shift: Number(shift.value) || 0 });
        if (mine === ticket) out.replaceChildren(renderSimulation(s));
      } catch (e) { if (mine === ticket) out.replaceChildren(el("p", { class: "error", text: e.message })); }
      finally { out.classList.remove("refetching"); }
    }
    return { onContext(c, changed) { if (["symbol", "contract", "strategy"].some(k => changed.includes(k))) load(); }, refresh: load };
  } });

  /* ---------- Order ticket: the limit to type, the checks, and (from phase 5) the proposal ---------- */
  WS.register("ticket", { title: "Order ticket", category: "Trading", size: { w: 4, h: 10 }, minW: 3, create(w) {
    let ticket = 0, priced = null;
    const side = segmented(`wtside${++uid}`, [["buy", "Buy"], ["sell", "Sell"]], w.config.side || "buy");
    const qty = el("input", { type: "number", min: "1", max: "1000", value: String(w.config.contracts || 1), "aria-label": "Contracts", style: "width:5em" });
    const head = el("div", { class: "ws-row" }, field("Side", side), field("Contracts", qty));
    const out = el("div");
    w.body.append(head, out);
    side.addEventListener("change", () => { w.config.side = side.value(); w.save(); load(); });
    qty.addEventListener("change", () => { w.config.contracts = Number(qty.value) || 1; w.save(); load(); });
    async function load() {
      const c = w.ctx(), mine = ++ticket;
      const k = c.contract && c.contract.symbol === c.symbol ? c.contract : null;
      if (!k) {
        priced = null; w.setBadge(c.symbol || "");
        out.replaceChildren(WS.empty(c.symbol ? `Select one of ${c.symbol}'s contracts in the options chain to price it here.` : "Pick a stock, then a contract."));
        if (WS.ticketExtras) for (const f of WS.ticketExtras) { const n = f(w, null, c); if (n) out.append(n); }
        return;
      }
      w.setBadge(contractName(k));
      out.replaceChildren(loading("Pricing…"));
      try {
        const req = FLOW.pricingRequest(k.symbol, k, Number(qty.value) || 1, side.value());
        const data = await api("/api/price", req);
        if (mine !== ticket) return;
        priced = { request: req, data };
        out.replaceChildren(renderPrice(data, extra => ({ ...req, ...extra })));   // the classic pricer asks for its request by function
        if (WS.ticketExtras) for (const f of WS.ticketExtras) { const n = f(w, priced, c); if (n) out.prepend(n); }
      } catch (e) { if (mine === ticket) out.replaceChildren(el("p", { class: "error", text: e.message })); }
    }
    return { onContext(c, changed) { if (["symbol", "contract"].some(x => changed.includes(x))) load(); }, refresh: load,
      priced: () => priced, config: () => ({ side: side.value(), contracts: Number(qty.value) || 1 }) };
  } });

  /* ---------- Activity: what changed, from the shared event stream ---------- */
  WS.register("activity", { title: "Activity", category: "Trading", size: { w: 4, h: 7 }, symbolBadge: false, create(w) {
    const list = el("ul", { class: "ws-activity" });
    const words = { "proposal.": "Proposal", "desk.": "Trading Desk", "daemon.": "Daemon", "study.": "Research", "opportunity.": "Discover",
      "workspace.": "Workspace", "notification.": "Notification", "order.": "Order", "policy.": "Policy" };
    w.body.append(list, el("p", { class: "small muted", text: "What the app and its background work change, as it happens." }));
    function add(ev) {
      const area = Object.entries(words).find(([p]) => ev.topic.startsWith(p));
      const t = new Date(ev.at);
      const text = ev.data && ev.data.text ? ev.data.text : `${area ? area[1] : ev.topic}: ${ev.topic.split(".").slice(1).join(" ")}${ev.subject ? ` (${ev.subject})` : ""}`;
      list.prepend(el("li", {}, el("time", { datetime: ev.at, text: isNaN(t) ? "" : t.toLocaleTimeString() }), text));
      while (list.children.length > 100) list.lastChild.remove();
    }
    api("/v3/events").then(d => (d.latest > 0 ? api(`/v3/events?after=${Math.max(0, d.latest - 30)}`) : { events: [] }))
      .then(d => d.events.forEach(add)).catch(() => {});
    WS.onEvent("", ev => { if (w.node.isConnected) add(ev); });
    return {};
  } });

  /* ---------- A classic screen inside a widget ---------- */
  const CLASSIC = { morning: ["Morning plan", "Discover"], live: ["Live news", "Discover"], today: ["Today", "Discover"], market: ["Market & news", "Discover"],
    ideas: ["Trade ideas", "Discover"], scanner: ["Scanner", "Discover"], analyze: ["Analyze opportunity", "Research"], price: ["Price an option", "Options"],
    simulate: ["Simulate a trade", "Options"], lab: ["Options lab", "Options"], positions: ["Positions", "Portfolio"], portfolio: ["Portfolio risk", "Portfolio"],
    paper: ["Paper trading", "Trading"], trades: ["My trades", "Portfolio"], journal: ["Journal", "Portfolio"], patterns: ["Pattern lab", "Learn"],
    backtest: ["Backtest", "Learn"], research: ["Research a stock", "Research"], health: ["Data health", "Learn"] };
  for (const [view, [title, category]] of Object.entries(CLASSIC)) {
    WS.register(`classic:${view}`, { title, category: `${category} (classic screens)`, size: { w: 12, h: 10 }, minW: 4, group: "none",
      symbolBadge: false, hidden: !BUILDERS[view], create(w) {
        if (!BUILDERS[view]) { w.body.replaceChildren(WS.empty(`${title} is switched off in .env.`)); return {}; }
        const owner = WS.allWidgets().find(o => o !== w && o.type === w.type && o.impl);
        if (owner && state.views[view] && state.views[view].isConnected) {
          w.body.replaceChildren(WS.empty(`${title} is already open in another widget. A classic screen can be open once.`),
            el("button", { class: "ws-btn", type: "button", onclick: () => WS.routeClassic(view) }, "Show it"));
          return {};
        }
        w.body.classList.add("ws-classic");
        const node = BUILDERS[view]();
        state.views[view] = node;
        node.classList.remove("hidden");
        w.body.replaceChildren(node);
        if (view === "journal" && state.refreshJournal) state.refreshJournal();
        if (node.onShow) node.onShow();
        return { onShow() { if (node.onShow) node.onShow(); }, dispose() { if (state.views[view] === node) delete state.views[view]; } };
      } });
  }

  /* ---------- a saved widget whose type this version does not know ---------- */
  WS.register("unknown", { title: "Unavailable widget", category: "Other", hidden: true, symbolBadge: false, create(w) {
    w.body.replaceChildren(WS.empty(`This layout has a widget (${w.config.missing || "unknown"}) this version does not have. Close it, or update the app.`));
    return {};
  } });

  /* ---------- the standard layouts ---------- */
  WS.DEFAULT_LAYOUTS = {
    discover: [
      { type: "feed", x: 0, y: 0, w: 7, h: 8, group: "blue" },
      { type: "watchlist", x: 7, y: 0, w: 5, h: 4, group: "blue" },
      { type: "news", x: 7, y: 4, w: 5, h: 4, group: "blue" },
      { type: "chart", x: 0, y: 8, w: 12, h: 9, group: "blue" },
      { type: "classic:morning", x: 0, y: 17, w: 12, h: 11, group: "none" },
    ],
    research: [
      { type: "chart", x: 0, y: 0, w: 8, h: 9, group: "blue" },
      { type: "chain", x: 8, y: 0, w: 4, h: 9, group: "blue" },
      { type: "scenario", x: 0, y: 9, w: 8, h: 10, group: "blue" },
      { type: "strategy", x: 8, y: 9, w: 4, h: 10, group: "blue" },
      { type: "riskreward", x: 0, y: 19, w: 8, h: 9, group: "blue" },
      { type: "news", x: 8, y: 19, w: 4, h: 9, group: "blue" },
    ],
    desk: [
      { type: "watchlist", x: 0, y: 0, w: 4, h: 5, group: "blue" },
      { type: "activity", x: 0, y: 5, w: 4, h: 7, group: "none" },
      { type: "classic:paper", x: 4, y: 0, w: 8, h: 12, group: "none" },
    ],
    portfolio: [
      { type: "classic:positions", x: 0, y: 0, w: 12, h: 9, group: "none" },
      { type: "classic:portfolio", x: 0, y: 9, w: 6, h: 10, group: "none" },
      { type: "classic:journal", x: 6, y: 9, w: 6, h: 10, group: "none" },
      { type: "classic:trades", x: 0, y: 19, w: 12, h: 10, group: "none" },
    ],
  };
  WS.DEFAULT_DOCK = { type: "ticket", group: "blue" };
})();

document.addEventListener("DOMContentLoaded", () => { if (document.body.dataset.app === "workstation") WS.boot(); });
