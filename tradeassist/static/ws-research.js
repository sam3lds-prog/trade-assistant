"use strict";
/* Research 3.0 widgets: the study (one complete investigation), the option premium chart, and the forecast's
   record. The study's forecast is drawn on the linked stock chart; its strategies feed the linked ticket and risk
   panel. Nothing here places an order. */
(() => {
  const enc = encodeURIComponent;
  const loading = text => el("p", { class: "loadnote" }, el("span", { class: "spinner", "aria-hidden": "true" }), text || "Loading…");
  const pctText = (v, d = 2) => (isNum(v) ? `${v > 0 ? "+" : ""}${v.toFixed(d)}%` : "n/a");
  const CAL_WORDS = { calibrated: ["pass", "Calibrated"], in_sample_only: ["warn", "Fitted, not checked"], not_calibrated: ["warn", "Not calibrated"],
    insufficient_data: ["warn", "Too little history"] };
  const THESES = [["swing", "Multi-day swing"], ["intraday_bounce", "Intraday bounce"], ["earnings_reaction", "Earnings reaction"],
    ["reversal", "Reversal"], ["investment", "Longer-term investment"]];
  const FORECAST_COLOR = "#2a9d8f", ALT_COLOR = "#8a96a3";

  /* ---------- the study ---------- */
  WS.register("study", { title: "Research study", category: "Research", size: { w: 4, h: 18 }, minW: 3, create(w) {
    const horizon = el("select", { "aria-label": "Horizon" }, [1, 3, 5, 10, 20].map(h => el("option", { value: String(h), selected: h === (w.config.horizon || 5) }, `${h} trading day${h === 1 ? "" : "s"}`)));
    const thesis = el("select", { "aria-label": "Kind of trade" }, THESES.map(([k, t]) => el("option", { value: k, selected: k === (w.config.thesis || "swing") }, t)));
    const dir = segmented(`wsdir${++uid}`, [["", "Either"], ["LONG", "Long"], ["SHORT", "Short"]], "");
    const peers = el("input", { type: "text", "aria-label": "Peers to add", placeholder: "Peers to add, e.g. AMD, AVGO", value: w.config.peers || "" });
    const run = el("button", { class: "ws-btn primary", type: "button" }, "Run the study");
    const status = el("div", { class: "small muted" });
    const out = el("div");
    w.body.append(el("div", { class: "ws-row" }, field("Horizon", horizon), field("Kind of trade", thesis)),
      el("div", { class: "ws-row" }, field("Direction", dir), field("Peers", peers)), el("div", { class: "ws-row" }, run, status), out);
    let polling = null, shown = null;
    run.addEventListener("click", start);

    async function start() {
      const c = w.ctx();
      if (!c.symbol) { status.textContent = "Pick a stock first."; return; }
      w.config.horizon = Number(horizon.value); w.config.thesis = thesis.value; w.config.peers = peers.value; w.save();
      run.disabled = true;
      status.textContent = `Studying ${c.symbol}…`;
      try {
        const r = await api("/v3/studies", { symbol: c.symbol, horizon_days: Number(horizon.value), thesis: thesis.value,
          direction: dir.value() || c.direction || null, peers_add: peers.value.split(",").map(s => s.trim()).filter(Boolean),
          opportunity_id: c.opportunity && c.opportunity.id ? c.opportunity.id : null });
        poll(r.id);
      } catch (e) { status.textContent = e.message; run.disabled = false; }
    }
    function poll(id) {
      clearTimeout(polling);
      api(`/v3/studies/${enc(id)}`).then(s => {
        if (s.status === "running") { polling = setTimeout(() => poll(id), 800); return; }
        run.disabled = false;
        if (s.status === "failed") { status.textContent = `The study failed: ${s.error || "see the log"}`; return; }
        status.textContent = "";
        render(s);
        w.set({ study: id, _self: true });
      }).catch(e => { run.disabled = false; status.textContent = e.message; });
    }

    function render(s) {
      shown = s.id;
      const st = s.study, v = s.view || {}, fc = st.forecast;
      const cal = fc ? (CAL_WORDS[fc.calibration.status] || ["warn", fc.calibration.status]) : null;
      const h = st.horizon_days, items = st.evidence.items;
      const group = side => items.filter(i => i.supports === side);
      const evList = (title, list) => list.length ? el("div", {}, el("h4", { text: `${title} (${list.length})` }),
        el("ul", { class: "plain small" }, list.map(i => el("li", {}, el("strong", { text: `${i.label}: ` }), i.summary)))) : "";
      const kids = [
        el("h3", { text: `${st.symbol}, ${h} trading day${h === 1 ? "" : "s"}${st.direction ? `, ${st.direction === "LONG" ? "long" : "short"}` : ""}` }),
        el("p", { class: "small muted", text: `As of ${new Date(st.created_at).toLocaleString()} on ${v.data_source || "the data source"}; ${st.status === "complete" ? "complete" : "partial: see what is missing"}.` }),
      ];
      if (fc) {
        const last = fc.base.path_pct.length - 1;
        kids.push(el("div", { class: "panel" },
          el("div", {}, statusTag(cal[0], cal[1]), " ", el("strong", { text: `Base ${pctText(fc.base.path_pct[last])}` }),
            el("span", { class: "muted", text: ` with 80% of outcomes between ${pctText(fc.base.low_pct[last], 1)} and ${pctText(fc.base.high_pct[last], 1)} by day ${h}` + (isNum(v.last) ? ` (${money(v.last * (1 + fc.base.low_pct[last] / 100))} to ${money(v.last * (1 + fc.base.high_pct[last] / 100))}).` : ".") })),
          el("p", { class: "small", text: fc.calibration.text }),
          el("ul", { class: "plain small" }, fc.explanation.map(t => el("li", { text: t }))),
          fc.alternatives.length ? el("div", {}, el("h4", { text: "Other paths to test against (assumptions, not predictions)" }),
            el("ul", { class: "plain small" }, fc.alternatives.map(a => el("li", {}, el("strong", { text: `${a.label}: ${pctText(a.path_pct[a.path_pct.length - 1])}. ` }), a.assumption)))) : "",
          el("h4", { text: "What would prove it wrong" }), el("ul", { class: "plain small" }, fc.invalidators.map(t => el("li", { text: t }))),
          el("div", { class: "ws-row" }, el("button", { class: "ws-btn", type: "button", onclick: () => draw(st) }, "Draw on the linked chart"),
            el("button", { class: "ws-btn", type: "button", onclick: e => challenge(st.id, e.currentTarget) }, "Ask the AI to challenge it"))));
      }
      kids.push(evList("For the trade", group("for")), evList("Against it", group("against")), evList("Context", group("neutral")));
      if (st.evidence.missing.length) kids.push(el("div", {}, el("h4", { text: "Missing or not yet trusted" }),
        el("ul", { class: "plain small" }, st.evidence.missing.map(t => el("li", { text: t })))));
      const an = v.analogs || {};
      if ((an.analogs || []).length) {
        kids.push(el("h4", { text: `Similar past situations: ${an.kind_label || an.kind}` }), table([
          { label: "Day", cell: a => a.date }, { label: "Stock", cell: a => a.symbol },
          { label: "Likeness", num: true, cell: a => `${Math.round(100 * a.similarity)}%` },
          { label: `${h} days later`, num: true, cell: a => el("span", { class: a.forward_pct[String(h)] >= 0 ? "pos-text" : "neg-text", text: pctText(a.forward_pct[String(h)], 1) }) },
          { label: "Counts", cell: a => (a.independent ? "yes" : "no (overlaps)") }], an.analogs.slice(0, 12)));
        for (const n of an.notes || []) kids.push(el("p", { class: "small muted", text: n }));
      }
      const sg = v.strategies || {};
      if ((sg.candidates || []).length) {
        const alts = (sg.scenarios || []).slice(1);
        kids.push(el("h4", { text: "Strategies under the same scenarios" }), table([
          { label: "Strategy", cell: c => el("span", {}, el("strong", { text: c.label }), c.key === sg.best ? " ✓" : "") },
          { label: "Base", num: true, cell: c => money(c.expected_pnl.base, 0) },
          ...alts.map(a => ({ label: a.label, num: true, cell: c => money(c.expected_pnl[a.key], 0) })),
          { label: "Most it can lose", num: true, cell: c => (c.unlimited_loss ? "no limit" : isNum(c.theoretical_max_loss) ? money(c.theoretical_max_loss, 0) : "not fixed") },
          { label: "IV crush", num: true, cell: c => (isNum(c.iv_sensitivity.crush) ? money(c.iv_sensitivity.crush - (c.costs || 0), 0) : "") },
          { label: "Fill chance", num: true, cell: c => (isNum(c.fill_likelihood) ? `${Math.round(100 * c.fill_likelihood)}%` : "") },
          { label: "Desk can send", cell: c => (c.executable ? "yes" : "by hand") }], sg.candidates, { onRow: pickStrategy }),
          sg.why ? el("p", { class: "small", text: sg.why }) : "", el("p", { class: "small muted", text: sg.note || "" }));
      }
      out.replaceChildren(...kids);
    }

    function pickStrategy(c) {
      const sym = w.ctx().symbol;
      if (c.legs && c.legs.length === 1 && c.instrument === "option") {
        const l = c.legs[0];
        w.set({ contract: { symbol: sym, option_symbol: l.option_symbol, option_type: String(l.option_type).toLowerCase(), strike: l.strike, expiration: l.expiration, bid: l.bid, ask: l.ask, mid: l.mid } });
      } else if (c.legs && c.legs.length) {
        w.set({ strategy: { key: c.key, label: c.label, symbol: sym, legs: c.legs.map(l => ({ expiration: l.expiration, option_type: l.option_type, strike: l.strike, side: l.side, contracts: l.contracts, entry_price: l.mid || undefined })) } });
      } else {
        w.set({ strategy: { key: c.key, label: c.label, symbol: sym, legs: [] }, contract: null });
      }
      WS.toast(`${c.label} sent to the linked widgets.`);
    }

    function draw(st) {
      const fc = st.forecast;
      w.set({ scenario: { kind: "forecast", study: st.id, symbol: st.symbol, horizon: st.horizon_days, base: fc.base,
        alternatives: fc.alternatives.map(a => ({ key: a.key, label: a.label, path_pct: a.path_pct })) } });
      if (!WS.find(w, "chart")) WS.toast("Link a stock chart to this widget (the same colour) to see the forecast on it.");
    }

    async function challenge(id, btn) {
      const box = el("div", { class: "panel" }, loading("Asking for the case against…"));
      btn.after(box);
      try {
        const r = await api(`/v3/studies/${enc(id)}/challenge`, { provider: "auto" });
        const part = (t, list) => (list.length ? el("div", {}, el("h4", { text: t }), el("ul", { class: "plain small" }, list.map(x => el("li", { text: x })))) : "");
        box.replaceChildren(part("Supports it", r.supporting), part("Contradicts it", r.contradicting), part("Missing", r.missing),
          part("Would prove it wrong", r.invalidators), part("Questions to answer first", r.questions), el("p", { class: "small muted", text: r.note }));
      } catch (e) { box.replaceChildren(el("p", { class: "error", text: e.message })); }
    }

    async function loadFor(c) {
      if (c.study && c.study !== shown) {
        try { const s = await api(`/v3/studies/${enc(c.study)}`); if (s.symbol === c.symbol && s.study) { render(s); return; } } catch (e) { /* gone */ }
      }
      if (!c.study) {
        out.replaceChildren(WS.empty(c.symbol ? `Run a study of ${c.symbol}: the trend, its past situations and its peers', the options market, one forecast and every strategy compared.` : "Pick a stock first."));
        shown = null;
      }
    }
    WS.onEvent("study.calibrated", () => { if (shown) WS.toast("The forecast was recalibrated: run the study again to use it."); });
    return { onContext(c, changed) { if (changed.includes("symbol") || changed.includes("study")) loadFor(c); },
      refresh() { if (shown) poll(shown); }, dispose() { clearTimeout(polling); },
      config: () => ({ horizon: Number(horizon.value), thesis: thesis.value, peers: peers.value }) };
  } });

  /* ---------- the option's own price chart ---------- */
  WS.register("optionchart", { title: "Option chart", category: "Options", size: { w: 4, h: 8 }, minW: 3, create(w) {
    let chart = null, ticket = 0;
    const range = el("select", { "aria-label": "Range" }, [["30", "1 month"], ["90", "3 months"], ["180", "6 months"]].map(([v, t]) => el("option", { value: v, selected: v === String(w.config.days || 90) }, t)));
    const note = el("div", { class: "small" });
    const box = el("div", { class: "lw-chart", style: "height: 240px" });
    w.body.append(el("div", { class: "ws-row" }, field("Range", range)), note, box);
    range.addEventListener("change", () => { w.config.days = Number(range.value); w.save(); load(); });
    function dispose() { if (chart) { chart.remove(); chart = null; } }
    async function load() {
      const c = w.ctx(), k = c.contract && c.contract.symbol === c.symbol ? c.contract : null, mine = ++ticket;
      dispose();
      if (!k || !k.option_symbol) { w.setBadge(c.symbol || ""); note.replaceChildren(WS.empty("Select a contract in the options chain to see its own price history.")); return; }
      w.setBadge(`${k.expiration} ${k.strike} ${String(k.option_type).toUpperCase()}`);
      note.replaceChildren(loading("Loading the contract's history…"));
      try {
        const d = await api(`/v3/options/history?option_symbol=${enc(k.option_symbol)}&days=${enc(range.value)}`);
        if (mine !== ticket) return;
        const actual = d.kind === "actual";
        note.replaceChildren(statusTag(actual ? "pass" : "warn", actual ? "Traded prices" : "Theoretical, not traded"), ` ${d.source}. `, ...d.notes.map(n => el("span", { class: "muted", text: `${n} ` })));
        const lib = window.LightweightCharts, col = chartColors();
        chart = lib.createChart(box, { autoSize: true, layout: { background: { color: col.bg }, textColor: col.muted, attributionLogo: false },
          grid: { vertLines: { color: col.line }, horzLines: { color: col.line } }, leftPriceScale: { visible: true }, rightPriceScale: { visible: true },
          timeScale: { borderColor: col.line } });
        const toTime = t => t;   // seconds, the contract's day at 16:00 New York
        if (actual && d.bars.length && isNum(d.bars[0].open)) {
          const s = chart.addSeries(lib.CandlestickSeries, { upColor: col.up, downColor: col.down, borderVisible: false, wickUpColor: col.up, wickDownColor: col.down, title: "Premium" });
          s.setData(d.bars.map(b => ({ time: toTime(b.t), open: b.open, high: b.high, low: b.low, close: b.close })));
        } else {
          const s = chart.addSeries(lib.LineSeries, { color: col.warn, lineWidth: 2, lineStyle: actual ? 0 : 2, title: actual ? "Premium" : "Theoretical" });
          s.setData(d.bars.map(b => ({ time: toTime(b.t), value: b.close })));
        }
        if (d.underlying_bars.length) {
          const u = chart.addSeries(lib.LineSeries, { color: col.muted, lineWidth: 1, priceScaleId: "left", title: d.underlying, lastValueVisible: true, priceLineVisible: false });
          u.setData(d.underlying_bars.map(b => ({ time: toTime(b.t), value: b.close })));
        }
        chart.timeScale().fitContent();
      } catch (e) { if (mine === ticket) note.replaceChildren(el("p", { class: "error", text: e.message })); }
    }
    return { onContext(c, changed) { if (["symbol", "contract"].some(x => changed.includes(x))) load(); }, refresh: load, dispose,
      config: () => ({ days: Number(range.value) }) };
  } });

  /* ---------- the forecast's record: calibration on history ---------- */
  WS.register("forecastrecord", { title: "Forecast record", category: "Research", size: { w: 6, h: 9 }, symbolBadge: false, create(w) {
    const horizon = el("select", { "aria-label": "Horizon" }, [1, 3, 5, 10, 20].map(h => el("option", { value: String(h), selected: h === (w.config.horizon || 5) }, `${h} day${h === 1 ? "" : "s"}`)));
    const go = el("button", { class: "ws-btn", type: "button" }, "Run the calibration");
    const out = el("div");
    let timer = null;
    w.body.append(el("div", { class: "ws-row" }, field("Horizon", horizon), go), out);
    horizon.addEventListener("change", () => { w.config.horizon = Number(horizon.value); w.save(); load(); });
    go.addEventListener("click", async () => {
      try { await api("/v3/forecast/calibrate", { horizon_days: Number(horizon.value) }); load(); }
      catch (e) { out.prepend(el("p", { class: "error", text: e.message })); }
    });
    async function load() {
      clearTimeout(timer);
      try {
        const d = await api(`/v3/forecast/calibration?horizon_days=${enc(horizon.value)}`);
        const c = d.contract, cal = d.calibration, job = d.job;
        const word = CAL_WORDS[c.status] || ["warn", c.status];
        const kids = [el("p", {}, statusTag(word[0], word[1]), " ", c.text)];
        if (job && job.state === "running") {
          kids.unshift(loading(`Testing forecasts on history: ${job.done} of ${job.total || "?"} stocks${job.current ? ` (${job.current})` : ""}…`));
          timer = setTimeout(load, 1500);
        } else if (job && job.state === "failed") kids.unshift(el("p", { class: "error", text: `The calibration failed: ${job.error}` }));
        if (cal) {
          kids.push(el("div", { class: "metrics" },
            metric(isNum(cal.coverage) ? `${Math.round(100 * cal.coverage)}%` : "n/a", "of outcomes inside the 80% band"),
            metric(isNum(cal.mae_pct) ? `${cal.mae_pct.toFixed(2)} pts` : "n/a", "median's average miss"),
            metric(isNum(cal.baseline_mae_pct) ? `${cal.baseline_mae_pct.toFixed(2)} pts` : "n/a", "assuming no move"),
            metric(isNum(cal.directional_accuracy) ? `${Math.round(100 * cal.directional_accuracy)}%` : "n/a", "direction right when it leaned")));
          const regimes = Object.entries(cal.by_regime || {});
          if (regimes.length) kids.push(el("h4", { text: "By the market's regime" }), table([
            { label: "Regime", cell: r => r[0] }, { label: "Cases", num: true, cell: r => String(r[1].cases) },
            { label: "Inside the band", num: true, cell: r => (isNum(r[1].coverage) ? `${Math.round(100 * r[1].coverage)}%` : "n/a") },
            { label: "Average miss", num: true, cell: r => (isNum(r[1].mae_pct) ? r[1].mae_pct.toFixed(2) : "n/a") }], regimes));
          kids.push(el("p", { class: "small muted", text: `Weights: ${Object.entries(cal.weights).map(([k, v]) => `${k} ${v.toFixed(2)}`).join(", ")}. ${cal.symbols ? `${cal.symbols.length} stocks, ` : ""}${cal.years || ""} years, run ${cal.run_at ? new Date(cal.run_at).toLocaleString() : ""} on ${cal.data_source || ""}.` }));
        }
        out.replaceChildren(...kids);
      } catch (e) { out.replaceChildren(el("p", { class: "error", text: e.message })); }
    }
    load();
    WS.onEvent("study.calibrated", load);
    return { refresh: load, dispose() { clearTimeout(timer); }, config: () => ({ horizon: Number(horizon.value) }) };
  } });

  /* ---------- the stock chart draws a study's forecast ---------- */
  WS.chartOverlays = WS.chartOverlays || [];
  WS.chartOverlays.push((chart, c) => {
    const sc = c.scenario;
    if (!sc || sc.kind !== "forecast" || sc.symbol !== c.symbol) { chart.setLines("forecast", []); return; }
    const a = chart.chartArgs();
    if (!a || !a.future_times || a.future_times.length < sc.horizon || !isNum(a.last_close)) { chart.setLines("forecast", []); return; }
    if (a.group !== "1d") { chart.setLines("forecast", []); return; }   // the forecast is in trading days: daily bars only
    const pts = arr => [{ time: a.last_time, value: a.last_close }].concat(arr.map((p, i) => ({ time: a.future_times[i], value: a.last_close * (1 + p / 100) })));
    const lines = [
      { key: "forecast:base", points: pts(sc.base.path_pct), color: FORECAST_COLOR, width: 2, style: 0, title: "Forecast" },
      { key: "forecast:low", points: pts(sc.base.low_pct), color: FORECAST_COLOR + "99", width: 1, style: 3 },
      { key: "forecast:high", points: pts(sc.base.high_pct), color: FORECAST_COLOR + "99", width: 1, style: 3 },
      ...sc.alternatives.map(alt => ({ key: `forecast:${alt.key}`, points: pts(alt.path_pct), color: ALT_COLOR, width: 1, style: 2, title: alt.label })),
    ];
    chart.setLines("forecast", lines);
    if (chart.showAhead) chart.showAhead(sc.horizon);
  });

  /* ---------- Research's standard layout, 3.0 ---------- */
  WS.DEFAULT_LAYOUTS.research = [
    { type: "chart", x: 0, y: 0, w: 8, h: 9, group: "blue" },
    { type: "study", x: 8, y: 0, w: 4, h: 18, group: "blue" },
    { type: "optionchart", x: 0, y: 9, w: 4, h: 8, group: "blue" },
    { type: "chain", x: 4, y: 9, w: 4, h: 8, group: "blue" },
    { type: "riskreward", x: 0, y: 17, w: 8, h: 8, group: "blue" },
    { type: "strategy", x: 8, y: 18, w: 4, h: 8, group: "blue" },
    { type: "scenario", x: 0, y: 25, w: 8, h: 10, group: "blue" },
    { type: "news", x: 8, y: 26, w: 4, h: 9, group: "blue" },
  ];
})();
