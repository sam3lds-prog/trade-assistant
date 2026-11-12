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
  // 4.1 Catalyst Scenarios: each case's colour on the chart, and the daily chart's time for a session's date (its bars
  // are drawn at 09:30 New York written as UTC: chartview.local_shift).
  const CASE_COLORS = { central: "#6c757d", bullish: "#2f9e44", bearish: "#d9480f" };
  const dayTime = d => Date.UTC(+d.slice(0, 4), +d.slice(5, 7) - 1, +d.slice(8, 10), 9, 30) / 1000;
  const catalystOn = () => typeof state !== "undefined" && !!state.status && !!state.status.features && state.status.features.catalyst_scenarios === true;

  /* ---------- the study ----------
     3.1: read in four sections (Summary, Evidence, Forecast, Strategies) instead of one long report; the range is
     called an 80% band only when the calibration for this horizon passed; fillability is called a heuristic; each
     strategy shows where it can go now (the Trading Desk, your broker by hand, or research only) and can be drafted
     for the desk with its context; and an interrupted study says so and can be run again. */
  const ROUTE_TAG = { automatic: "Desk can send", manual: "By hand", research: "Research only" };
  const routeTag = r => (r ? el("span", { class: `ws-route ${r.route}`, title: [r.label, ...(r.reasons || [])].join(". "), text: ROUTE_TAG[r.route] || r.route }) : "");
  const SECTIONS = [["summary", "Summary"], ["evidence", "Evidence"], ["forecast", "Forecast"], ["strategies", "Strategies"]];
  function rangeWords(fc, h, last) {
    const i = fc.base.path_pct.length - 1, lo = fc.base.low_pct[i], hi = fc.base.high_pct[i];
    const prices = isNum(last) ? ` (${money(last * (1 + lo / 100))} to ${money(last * (1 + hi / 100))})` : "";
    return fc.calibration.status === "calibrated"
      ? `The calibrated 80% band: ${pctText(lo, 1)} to ${pctText(hi, 1)} by day ${h}${prices}. It held about 80% of outcomes on history at this horizon.`
      : `Model-implied 10th–90th percentile range: ${pctText(lo, 1)} to ${pctText(hi, 1)} by day ${h}${prices}. Historical coverage has not yet been validated.`;
  }

  // 4.2: whether the forecast is ready, in two parts said separately: directional evidence (did a direction beat
  // "no move" on dates it was not chosen on?) and range validation (did the band hold about 80% on the holdout?).
  const READY_TONE = { edge: "pass", validated: "pass", no_edge: "info", not_held: "info", below_target: "warn", above_target: "warn",
    pending: "warn", insufficient: "warn" };
  function readinessTag(fc) {
    const rd = fc && fc.readiness;
    if (!rd || !rd.label) { const cal = CAL_WORDS[fc.calibration.status] || ["warn", fc.calibration.status]; return statusTag(cal[0], cal[1]); }
    const d = rd.directional || {}, r = rd.range || {};
    const tone = d.status === "edge" && r.status === "validated" ? "pass" : r.status === "validated" ? "info" : "warn";
    return el("span", { class: "ws-readiness-tag", title: rd.text || "" }, statusTag(tone, rd.label));
  }
  function readinessBlock(rd) {
    if (!rd || !rd.directional) return "";
    const d = rd.directional, r = rd.range, sm = rd.sample || {};
    const span = sm.first_date && sm.last_date ? ` from ${sm.first_date} to ${sm.last_date}` : "";
    const sample = isNum(sm.cases) && sm.cases ? `${sm.cases} holdout forecasts${span}` + (isNum(sm.train_cases) ? ` (fitted on ${sm.train_cases}, chosen on ${sm.validation_cases}, ${sm.purged_cases || 0} overlapping purged)` : "")
      + (sm.symbols ? `, ${sm.symbols} stocks` : "") + (sm.checked_at ? `; last checked ${new Date(sm.checked_at).toLocaleString()}` : "") + (sm.data_source ? ` on ${sm.data_source}` : "") + "." : "";
    const err = isNum(d.mae_pct) && isNum(d.no_change_mae_pct) ? ` Average miss ${d.mae_pct.toFixed(2)} points against ${d.no_change_mae_pct.toFixed(2)} for no change.` : "";
    const cov = isNum(r.coverage) ? ` Observed coverage ${Math.round(100 * r.coverage)}% (target 80%).` : "";
    return el("div", { class: "ws-readiness", "data-reason": rd.reason || "" },
      el("p", { class: "small" }, el("strong", { text: "Directional evidence: " }), statusTag(READY_TONE[d.status] || "warn", d.label), ` ${d.text || ""}${err}`),
      el("p", { class: "small" }, el("strong", { text: "Range validation: " }), statusTag(READY_TONE[r.status] || "warn", r.label), ` ${r.text || ""}${cov}`),
      sample ? el("p", { class: "small muted", text: sample }) : "",
      rd.method_note ? el("p", { class: "small muted", text: rd.method_note }) : "");
  }

  WS.register("study", { title: "Research study", category: "Research", size: { w: 5, h: 10 }, minW: 3, create(w) {
    const horizon = el("select", { "aria-label": "Horizon" }, [1, 3, 5, 10, 20].map(h => el("option", { value: String(h), selected: h === (w.config.horizon || 5) }, `${h} trading day${h === 1 ? "" : "s"}`)));
    const thesis = el("select", { "aria-label": "Kind of trade" }, THESES.map(([k, t]) => el("option", { value: k, selected: k === (w.config.thesis || "swing") }, t)));
    const dir = segmented(`wsdir${++uid}`, [["", "Either"], ["LONG", "Long"], ["SHORT", "Short"]], "");
    const peers = el("input", { type: "text", "aria-label": "Peers to add", placeholder: "Peers to add, e.g. AMD, AVGO", value: w.config.peers || "" });
    const run = el("button", { class: "ws-btn primary", type: "button" }, "Run the study");
    const status = el("div", { class: "small muted", "aria-live": "polite" });
    const out = el("div");
    const form = el("details", { class: "ws-more", open: true }, el("summary", { text: "Study settings" }),
      el("div", { class: "ws-row" }, field("Horizon", horizon), field("Kind of trade", thesis)),
      el("div", { class: "ws-row" }, field("Direction", dir), field("Peers", peers)));
    w.body.append(form, el("div", { class: "ws-row" }, run, status), out);
    // A study opens on its Summary; refreshing the same study keeps the section being read.
    let polling = null, shown = null, current = null, section = "summary", rendered = null;
    run.addEventListener("click", start);

    async function start() {
      const c = w.ctx();
      if (!c.symbol) { status.textContent = "Pick a stock first."; return; }
      w.config.horizon = Number(horizon.value); w.config.thesis = thesis.value; w.config.peers = peers.value; w.save();
      run.disabled = true;
      status.textContent = `Studying ${c.symbol}…`;
      try {
        const r = await w.api("/v3/studies", { symbol: c.symbol, horizon_days: Number(horizon.value), thesis: thesis.value,
          direction: dir.value() || c.direction || null, peers_add: peers.value.split(",").map(s => s.trim()).filter(Boolean),
          opportunity_id: c.opportunity && c.opportunity.id ? c.opportunity.id : null });
        poll(r.id);
      } catch (e) { if (e !== WS.GONE) { status.textContent = e.message; run.disabled = false; } }
    }
    function poll(id) {
      clearTimeout(polling);
      w.api(`/v3/studies/${enc(id)}`).then(s => {
        if (s.status === "running") { polling = setTimeout(() => poll(id), 800); return; }
        run.disabled = false;
        if (s.status === "failed") { failed(s); return; }
        status.textContent = "";
        render(s);
        w.set({ study: id, _self: true });
      }).catch(e => { if (e !== WS.GONE) { run.disabled = false; status.textContent = e.message; } });
    }
    // A failed study (an error, or interrupted by a restart or sleep) says why and can be run again as it was.
    function failed(s) {
      shown = s.id; current = null;
      status.textContent = "";
      const again = el("button", { class: "ws-btn primary", type: "button" }, "Run it again");
      const note = el("span", { class: "small muted" });
      again.addEventListener("click", async () => {
        again.disabled = true; note.textContent = "Starting it again…";
        try { const r = await w.api(`/v3/studies/${enc(s.id)}/retry`, {}); run.disabled = true; status.textContent = `Studying ${s.symbol}…`; out.replaceChildren(loading(`Studying ${s.symbol} again…`)); w.set({ study: r.id, _self: true }); poll(r.id); }
        catch (e) { if (e !== WS.GONE) { note.textContent = e.message; again.disabled = false; } }
      });
      out.replaceChildren(el("div", { class: "panel", role: "alert" }, el("p", {}, statusTag("fail", "The study did not finish"), ` ${s.error || "No reason was recorded."}`),
        el("div", { class: "ws-row" }, again, note)));
    }

    function tabs() {
      const bar = el("div", { class: "ws-sections", role: "tablist", "aria-label": "Study sections" });
      const panels = {};
      const pick = k => {
        section = k;
        for (const b of bar.children) { const on = b.dataset.k === k; b.setAttribute("aria-selected", on ? "true" : "false"); b.tabIndex = on ? 0 : -1; }
        for (const [key, panel] of Object.entries(panels)) panel.hidden = key !== k;
      };
      SECTIONS.forEach(([k, label], i) => bar.append(el("button", { type: "button", role: "tab", "data-k": k, "aria-selected": "false", tabindex: "-1",
        onclick: () => pick(k), onkeydown: e => {
          const step = { ArrowRight: 1, ArrowLeft: -1 }[e.key];
          if (!step) return;
          const n = SECTIONS[(i + step + SECTIONS.length) % SECTIONS.length][0];
          pick(n); bar.querySelector(`[data-k="${n}"]`).focus(); e.preventDefault();
        } }, label)));
      for (const [k] of SECTIONS) panels[k] = el("div", { role: "tabpanel", "data-section": k, hidden: true });
      return { bar, panels, pick };
    }

    function render(s) {
      if (s.id !== rendered) section = "summary";
      rendered = s.id; shown = s.id; current = s;
      const st = s.study, v = s.view || {}, fc = st.forecast, routes = s.routes || {};
      const cal = fc ? (CAL_WORDS[fc.calibration.status] || ["warn", fc.calibration.status]) : null;
      const h = st.horizon_days, items = st.evidence.items;
      const group = side => items.filter(i => i.supports === side);
      const evList = (title, list) => list.length ? el("div", {}, el("h4", { text: `${title} (${list.length})` }),
        el("ul", { class: "plain small" }, list.map(i => el("li", {}, el("strong", { text: `${i.label}: ` }), i.summary)))) : "";
      const t = tabs();
      const head = [el("h3", { text: `${st.symbol}, ${h} trading day${h === 1 ? "" : "s"}${st.direction ? `, ${st.direction === "LONG" ? "long" : "short"}` : ""}` }),
        el("p", { class: "small muted", text: `As of ${new Date(st.created_at).toLocaleString()} on ${v.data_source || "the data source"}; ${st.status === "complete" ? "complete" : "partial: see what is missing (Evidence)"}.` })];
      const actions = fc ? el("div", { class: "ws-row" }, el("button", { class: "ws-btn", type: "button", onclick: () => draw(st, s.frame) }, "Draw on the linked chart"),
        el("button", { class: "ws-btn", type: "button", onclick: e => challenge(st.id, e.currentTarget) }, "Ask the AI to challenge it")) : "";
      const sg = v.strategies || {};

      // Summary: the forecast in one line with what its range is, the best strategies and their routes, and counts.
      const sum = t.panels.summary;
      if (fc) {
        const last = fc.base.path_pct.length - 1;
        sum.append(el("div", { class: "panel" }, el("p", {}, readinessTag(fc), " ", el("strong", { text: `Base ${pctText(fc.base.path_pct[last])} by day ${h}` })),
          el("p", { class: "small", text: rangeWords(fc, h, v.last) })));
      }
      const top = [...(sg.candidates || [])].sort((a, b) => (b.risk_adjusted || -1e9) - (a.risk_adjusted || -1e9)).slice(0, 3);
      if (top.length) {
        sum.append(el("h4", { text: "Strategies under the same scenarios" }),
          el("ul", { class: "plain small" }, top.map(c => el("li", {}, el("strong", { text: `${c.label}${c.key === sg.best ? " ✓" : ""}: ` }),
            `${money(c.expected_pnl.base, 0)} expected under the base path after costs `, routeTag(routes[c.key])))),
          sg.why ? el("p", { class: "small", text: sg.why }) : "",
          el("button", { class: "ws-btn", type: "button", onclick: () => t.pick("strategies") }, "Compare them all"));
      }
      sum.append(el("p", { class: "small muted", text: `Evidence: ${group("for").length} for, ${group("against").length} against, ${group("neutral").length} context; ${st.evidence.missing.length} missing or not yet trusted.` }), actions);

      // Evidence: what supports it and what does not, what is missing, and the past situations like today's.
      const evd = t.panels.evidence;
      evd.append(evList("For the trade", group("for")), evList("Against it", group("against")), evList("Context", group("neutral")));
      if (st.evidence.missing.length) evd.append(el("div", {}, el("h4", { text: "Missing or not yet trusted" }),
        el("ul", { class: "plain small" }, st.evidence.missing.map(x => el("li", { text: x })))));
      const an = v.analogs || {};
      if ((an.analogs || []).length) {
        evd.append(el("h4", { text: `Similar past situations: ${an.kind_label || an.kind}` }), table([
          { label: "Day", cell: a => a.date }, { label: "Stock", cell: a => a.symbol },
          { label: "Likeness", num: true, cell: a => `${Math.round(100 * a.similarity)}%` },
          { label: `${h} days later`, num: true, cell: a => el("span", { class: a.forward_pct[String(h)] >= 0 ? "pos-text" : "neg-text", text: pctText(a.forward_pct[String(h)], 1) }) },
          { label: "Counts", cell: a => (a.independent ? "yes" : "no (overlaps)") }], an.analogs.slice(0, 12)));
        for (const n of an.notes || []) evd.append(el("p", { class: "small muted", text: n }));
      }

      // Forecast: the record behind it, how it is made, the paths to test against, and what would prove it wrong.
      const fcp = t.panels.forecast;
      if (fc) {
        fcp.append(el("p", {}, readinessTag(fc), " ", el("span", { class: "small", text: fc.calibration.text })),
          readinessBlock(fc.readiness),
          el("p", { class: "small", text: rangeWords(fc, h, v.last) }),
          el("ul", { class: "plain small" }, fc.explanation.map(x => el("li", { text: x }))),
          fc.alternatives.length ? el("div", {}, el("h4", { text: "Other paths to test against (assumptions, not predictions)" }),
            el("ul", { class: "plain small" }, fc.alternatives.map(a => el("li", {}, el("strong", { text: `${a.label}: ${pctText(a.path_pct[a.path_pct.length - 1])}. ` }), a.assumption)))) : "",
          el("h4", { text: "What would prove it wrong" }), el("ul", { class: "plain small" }, fc.invalidators.map(x => el("li", { text: x }))), actionsFor(st, s.frame));
        // 4.1: one event's Central, Bullish and Bearish reactions, reviewed and drawn beside the base.
        if (catalystOn()) fcp.append(catalystSection(w, s, () => current === s && !w.disposed));
      } else fcp.append(WS.empty("No forecast could be made: see Evidence for what is missing."));

      // Strategies: every candidate, its risk in separate terms, its estimated fillability and its route now.
      const stp = t.panels.strategies;
      if ((sg.candidates || []).length) {
        const alts = (sg.scenarios || []).slice(1);
        const draftNote = el("div", { class: "small", "aria-live": "polite" });
        stp.append(el("h4", { text: "Every strategy under the same scenarios" }), table([
          { label: "Strategy", cell: c => el("span", {}, el("strong", { text: c.label }), c.key === sg.best ? " ✓" : "") },
          { label: "Base", num: true, cell: c => money(c.expected_pnl.base, 0) },
          ...alts.map(a => ({ label: a.label, num: true, cell: c => money(c.expected_pnl[a.key], 0) })),
          { label: "Most it can lose", num: true, cell: c => (c.unlimited_loss ? "no limit" : isNum(c.theoretical_max_loss) ? money(c.theoretical_max_loss, 0) : "not fixed") },
          { label: "Expected shortfall", num: true, cell: c => (isNum(c.expected_shortfall) ? money(c.expected_shortfall, 0) : "") },
          { label: "Capital", num: true, cell: c => (isNum(c.capital_required) ? money(c.capital_required, 0) : "") },
          { label: "IV crush", num: true, cell: c => (isNum(c.iv_sensitivity.crush) ? money(c.iv_sensitivity.crush - (c.costs || 0), 0) : "") },
          { label: "Est. fillability", num: true, cell: c => (isNum(c.fill_likelihood) ? el("span", { title: "A heuristic from the quoted spread, not a measured chance of execution" }, `${Math.round(100 * c.fill_likelihood)}%`) : "") },
          { label: "Route", cell: c => routeTag(routes[c.key]) },
          { label: "", cell: c => el("button", { class: "ws-btn ws-btn-mini", type: "button", title: "Hand this strategy to the Trading Desk with its context",
            onclick: e => { e.stopPropagation(); draft(st, c, draftNote, e.currentTarget); } }, "Draft") }], sg.candidates, { onRow: pickStrategy }),
          draftNote,
          sg.why ? el("p", { class: "small", text: sg.why }) : "",
          el("p", { class: "small muted", text: "Most it can lose is from the contract terms; expected shortfall is the model's average of its worst tenth; capital is what it ties up here (a broker's requirement can differ). Route is where it can go under the policy and broker in force now." }),
          el("p", { class: "small muted", text: sg.note || "" }));
      } else stp.append(WS.empty("No strategy could be valued: see Evidence for what is missing."));

      out.replaceChildren(...head, t.bar, ...Object.values(t.panels));
      t.pick(SECTIONS.some(([k]) => k === section) ? section : "summary");
      form.open = false;
    }
    function actionsFor(st, frame) {
      return el("div", { class: "ws-row" }, el("button", { class: "ws-btn", type: "button", onclick: () => draw(st, frame) }, "Draw on the linked chart"));
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

    // A draft trade (v3/drafts.py) for the linked Trading Desk: the strategy with its context, sizing, risk and route.
    async function draft(st, c, note, btn) {
      btn.disabled = true;
      note.replaceChildren(loading(`Drafting ${c.label}…`));
      try {
        const d = await w.api("/v3/drafts", { study_id: st.id, strategy: c.key });
        w.set({ draft: d, _self: true });
        note.replaceChildren(el("p", {}, `Drafted ${c.label}: `, routeTag(d.execution), ` ${d.execution.label}.`),
          el("button", { class: "ws-btn", type: "button", onclick: () => WS.goWorkspace("desk") }, "Open the Trading Desk"));
      } catch (e) { if (e !== WS.GONE) note.replaceChildren(el("p", { class: "error", text: e.message })); }
      btn.disabled = false;
    }

    // 4.1: drawn from the study's own anchor price and session dates (its frame), so a chart that refreshes to a newer
    // close draws an old study where it was made instead of moving it.
    function draw(st, frame) {
      const fc = st.forecast;
      w.set({ scenario: { kind: "forecast", study: st.id, symbol: st.symbol, horizon: st.horizon_days, base: fc.base, calibrated: fc.calibration.status === "calibrated",
        anchor: frame ? frame.anchor : null, sessions: frame ? frame.sessions : null,
        alternatives: fc.alternatives.map(a => ({ key: a.key, label: a.label, path_pct: a.path_pct })) } });
      if (!WS.find(w, "chart")) WS.toast("Link a stock chart to this widget (the same colour) to see the forecast on it.");
    }

    async function challenge(id, btn) {
      const box = el("div", { class: "panel" }, loading("Asking for the case against…"));
      btn.after(box);
      try {
        const r = await w.api(`/v3/studies/${enc(id)}/challenge`, { provider: "auto" });
        const part = (title, list) => (list.length ? el("div", {}, el("h4", { text: title }), el("ul", { class: "plain small" }, list.map(x => el("li", { text: x })))) : "");
        box.replaceChildren(part("Supports it", r.supporting), part("Contradicts it", r.contradicting), part("Missing", r.missing),
          part("Would prove it wrong", r.invalidators), part("Questions to answer first", r.questions), el("p", { class: "small muted", text: r.note }));
      } catch (e) { if (e !== WS.GONE) box.replaceChildren(el("p", { class: "error", text: e.message })); }
    }

    async function loadFor(c) {
      const mine = w.ticket();
      if (c.study && c.study !== shown) {
        try {
          const s = await w.api(`/v3/studies/${enc(c.study)}`);
          if (!w.current(mine)) return;
          if (s.symbol === c.symbol && s.study) { render(s); return; }
          if (s.symbol === c.symbol && s.status === "failed") { failed(s); return; }
          if (s.symbol === c.symbol && s.status === "running") {      // started elsewhere (Discover's "Study it"): follow it
            horizon.value = String(s.horizon_days); thesis.value = s.thesis;
            run.disabled = true; status.textContent = `Studying ${s.symbol}…`; shown = s.id;
            out.replaceChildren(loading(`Studying ${s.symbol}: the trend, past situations, peers, the options market and the forecast…`));
            poll(s.id);
            return;
          }
        } catch (e) { if (e === WS.GONE) return; /* gone */ }
      }
      if (!c.study) {
        clearTimeout(polling); run.disabled = false; status.textContent = "";
        out.replaceChildren(WS.empty(c.symbol ? `Run a study of ${c.symbol}: the trend, its past situations and its peers', the options market, one forecast and every strategy compared.` : "Pick a stock first."));
        shown = null; current = null; form.open = true;
      }
    }
    w.on("study.calibrated", () => { if (shown) WS.toast("The forecast was recalibrated: run the study again to use it."); });
    return { onContext(c, changed) { if (changed.includes("symbol") || changed.includes("study")) loadFor(c); },
      refresh() { if (shown) poll(shown); }, dispose() { clearTimeout(polling); },
      config: () => ({ horizon: Number(horizon.value), thesis: thesis.value, peers: peers.value }),
      helpFacts() {
        if (!current) return [];
        const st = current.study, fc = st.forecast, v = current.view || {};
        return [["Study", `${st.id}, as of ${new Date(st.created_at).toLocaleString()}`], ["Source", v.data_source || "n/a"],
          ...(fc ? [["Model", `${fc.model_version}; ${(CAL_WORDS[fc.calibration.status] || ["", fc.calibration.status])[1].toLowerCase()} for ${st.horizon_days} days`]] : [])];
      } };
  } });

  /* ---------- 4.1 Catalyst Scenarios: one event's Central, Bullish and Bearish reactions inside the study ----------
     Export evidence freezes the study into a packet (downloaded as Markdown, its question copied) for a scenario
     provider such as the MiroFish dashboard; Import cases takes the provider's JSON (or a form filled in from its
     report); each case's assumptions are reviewed here, with their sources, and Apply assumptions saves a version whose
     price paths are drawn, dashed, beside the base. The provider writes no number the chart draws: the app computes
     every path from the study's base, its frozen factor fit and the sizes you accept. Every reply is checked against the
     study it was asked for, so a late answer never lands on another stock. Report text is shown as text. */
  const CATALYST_KINDS = [["earnings", "Earnings"], ["guidance", "Guidance"], ["sector_shock", "Sector shock"], ["macro", "Macro"],
    ["product", "Product"], ["regulatory", "Regulatory"], ["analyst", "Analyst call"], ["other", "Other"]];
  const EVENT_TIMES = [["after_close", "After the close"], ["before_open", "Before the open"], ["during_session", "During the session"], ["unknown", "Time unknown"]];
  const DRIVER_TYPES = ["issuer_event", "market", "sector", "macro", "positioning", "sentiment", "other"];
  const TIMINGS = ["first_session_after_release", "before_event", "spread_over_horizon", "by_horizon", "unknown"];
  const SOURCE_WORDS = { user_stress: "Your stress", provider: "Provider (cited)", options_implied_move: "Options-implied move" };
  const DIR_WORDS = { up: "Up", down: "Down", flat: "Flat", mixed: "Mixed" };
  const words = k => String(k).replace(/_/g, " ");
  const ids = text => String(text || "").split(/[\s,]+/).map(x => x.trim().toUpperCase()).filter(Boolean);
  const choose = (label, options, value) => el("select", { "aria-label": label }, options.map(([v, t]) => el("option", { value: v, selected: v === value }, t)));
  async function copy(text, note, what) {
    try { await navigator.clipboard.writeText(text); note.textContent = `${what} copied.`; }
    catch (e) { note.textContent = `${what} could not be copied here: select it below and copy it.`; }
  }
  function download(name, text) {
    const a = el("a", { href: URL.createObjectURL(new Blob([text], { type: "text/markdown" })), download: name });
    document.body.append(a); a.click(); a.remove();
    setTimeout(() => URL.revokeObjectURL(a.href), 2000);
  }

  function catalystSection(w, s, alive) {
    const st = s.study, h = st.horizon_days;
    const body = el("div"), note = el("div", { class: "small", "aria-live": "polite" });
    const box = el("section", { class: "ws-catalyst", "aria-label": "Catalyst Scenarios", "data-study": st.id },
      el("h4", { text: "Catalyst Scenarios" }),
      el("p", { class: "small muted", text: "Pick an event, export the study's evidence for a scenario provider (the MiroFish dashboard, say), import its Central, Bullish and Bearish cases, review their assumptions and draw them beside the base. The provider proposes conditions and directions; the app computes every path. No case has a probability." }),
      note, body);
    let tick = 0;
    const turn = () => ++tick;
    const fresh = n => n === tick && alive();              // the latest request, for the study still shown
    const fail = (e, n) => { if (e !== WS.GONE && fresh(n)) note.replaceChildren(el("p", { class: "error", role: "alert", text: e.message })); };
    if (![1, 5].includes(h)) {
      body.append(WS.empty(`Catalyst Scenarios use a 1- or 5-session study in this release; this one is ${h} sessions. Run the study with a horizon of 1 or 5 trading days.`));
      return box;
    }
    load();
    return box;

    async function load() {
      const n = turn();
      body.replaceChildren(loading("Looking for this study's scenarios…"));
      try {
        const r = await w.api(`/v3/studies/${enc(st.id)}/scenario-runs`);
        if (!fresh(n)) return;
        if (r.runs.length) return openRun(r.runs[0].id);
        if (r.seeds.length) {
          const sv = await w.api(`/v3/scenario-seeds/${enc(r.seeds[0].id)}`);
          if (!fresh(n)) return;
          return importPanel(sv);
        }
        catalystForm();
      } catch (e) { fail(e, n); body.replaceChildren(); }
    }

    // 1. The catalyst, and Export evidence.
    function catalystForm(prev) {
      const c = (prev && prev.catalyst) || {};
      const kind = choose("Kind of catalyst", CATALYST_KINDS, c.kind || "earnings");
      const title = el("input", { type: "text", "aria-label": "Catalyst", placeholder: "e.g. Q3 results and outlook", value: c.title || "", maxlength: "200" });
      const when = el("input", { type: "date", "aria-label": "Event date", value: c.event_date || (s.frame && s.frame.sessions[0] ? s.frame.sessions[0].date : "") });
      const time = choose("Event time", EVENT_TIMES, c.event_time || "after_close");
      const hypo = el("input", { type: "checkbox", "aria-label": "Hypothetical", checked: !!c.hypothetical });
      const desc = el("textarea", { rows: "2", "aria-label": "What is expected", placeholder: "What is expected, in a sentence or two (optional)", maxlength: "1500" }, c.description || "");
      const sources = el("div", { class: "ws-cat-sources" });
      const addSource = () => {
        if (sources.children.length >= 6) return;
        const row = el("div", { class: "ws-row ws-cat-source" },
          el("input", { type: "text", "aria-label": "Source title", placeholder: "Title", "data-f": "title", maxlength: "200" }),
          el("input", { type: "datetime-local", "aria-label": "Published", "data-f": "published_at" }),
          el("input", { type: "url", "aria-label": "Address", placeholder: "https://…", "data-f": "url", maxlength: "500" }),
          el("textarea", { rows: "2", "aria-label": "Excerpt", placeholder: "The public excerpt", "data-f": "excerpt", maxlength: "1200" }),
          el("button", { class: "ws-btn ws-btn-mini", type: "button", onclick: e => e.currentTarget.parentElement.remove() }, "Remove"));
        sources.append(row);
      };
      const go = el("button", { class: "ws-btn primary", type: "button" }, "Export evidence");
      go.addEventListener("click", async () => {
        const n = turn();
        go.disabled = true; note.replaceChildren(loading("Freezing the study's evidence…"));
        const srcs = [...sources.children].map(r => Object.fromEntries([...r.querySelectorAll("[data-f]")].map(i => [i.dataset.f, i.value.trim()])))
          .filter(x => x.title).map(x => ({ title: x.title, published_at: x.published_at, url: x.url || null, excerpt: x.excerpt }));
        try {
          const sv = await w.api(`/v3/studies/${enc(st.id)}/scenario-seeds`, { catalyst: { kind: kind.value, title: title.value.trim(), description: desc.value.trim(),
            hypothetical: hypo.checked, event_date: when.value || null, event_time: time.value }, sources: srcs });
          if (!fresh(n)) return;
          note.replaceChildren();
          importPanel(sv, true);
        } catch (e) { fail(e, n); }
        go.disabled = false;
      });
      body.replaceChildren(
        el("div", { class: "ws-row" }, field("Kind", kind), field("Catalyst", title)),
        el("div", { class: "ws-row" }, field("Date", when), field("Time", time), el("label", { class: "check" }, hypo, " Hypothetical")),
        field("What is expected", desc),
        el("details", { class: "ws-more" }, el("summary", { text: "Public source excerpts (optional, published before the study ran)" }), sources,
          el("button", { class: "ws-btn ws-btn-mini", type: "button", onclick: addSource }, "Add a source")),
        el("div", { class: "ws-row" }, go),
        el("p", { class: "small muted", text: `The packet holds the study's evidence as of ${new Date(st.created_at).toLocaleString()}, its anchor price and sessions, and the sources you add. Nothing about your account, positions or budget goes in it.` }));
    }

    // 2. The packet, and Import cases (pasted JSON, or a form filled in from the report).
    function importPanel(sv, justMade) {
      const seed = sv.seed;
      const getPacket = async () => (await fetch(sv.markdown_url)).text();
      if (justMade) {
        getPacket().then(t => { if (alive()) download(sv.filename, t); }).catch(() => {});
        copy(sv.question, note, "The forecasting question");
      }
      const result = el("textarea", { rows: "7", class: "ws-cat-json", "aria-label": "Result JSON", placeholder: "Paste the provider's JSON result here" });
      const excerpt = el("textarea", { rows: "3", "aria-label": "Report excerpt", placeholder: "The report's text, or a part of it (optional; kept as text)", maxlength: "8000" });
      const go = el("button", { class: "ws-btn primary", type: "button" }, "Import cases");
      const err = el("div", { class: "small", "aria-live": "polite" });
      const sendImport = async payload => {
        const n = turn();
        go.disabled = true; err.replaceChildren(loading("Checking the cases against the packet…"));
        try {
          const r = await w.api(`/v3/studies/${enc(st.id)}/scenario-runs`, { provider: "manual", seed_id: seed.seed_id, result: payload, report_excerpt: excerpt.value.trim() || null });
          if (!fresh(n)) return;
          note.replaceChildren(); showRun(r);
        } catch (e) { if (e !== WS.GONE && fresh(n)) err.replaceChildren(el("p", { class: "error", role: "alert", text: e.message })); }
        go.disabled = false;
      };
      go.addEventListener("click", () => {
        if (!result.value.trim()) { err.textContent = "Paste the result first, or fill in the form."; return; }
        sendImport(result.value);
      });
      const tpl = JSON.stringify(sv.template, null, 2);
      body.replaceChildren(
        el("div", { class: "panel" },
          el("p", { class: "small" }, el("strong", { text: "Evidence packet " }), el("code", { text: seed.seed_id }), ` · hash ${seed.seed_hash.slice(0, 12)} · cutoff ${seed.cutoff}`),
          el("p", { class: "small", text: `${seed.catalyst.title}: ${seed.catalyst_timing_note} Anchor ${money(seed.anchor.price)} (read ${new Date(seed.anchor.at).toLocaleString()}); sessions ${seed.sessions.map(x => x.date).join(", ")}.` }),
          el("details", { class: "ws-more" }, el("summary", { text: `Sources in the packet (${seed.sources.length})` }),
            el("ul", { class: "plain small" }, seed.sources.map(x => el("li", {}, el("strong", { text: `${x.id} ` }), `${x.title}: ${x.summary}`)))),
          seed.missing.length ? el("details", { class: "ws-more" }, el("summary", { text: `Missing or not yet trusted (${seed.missing.length})` }),
            el("ul", { class: "plain small" }, seed.missing.map(x => el("li", { text: x })))) : "",
          el("div", { class: "ws-row" },
            el("button", { class: "ws-btn", type: "button", onclick: () => getPacket().then(t => download(sv.filename, t)) }, "Download the packet"),
            el("button", { class: "ws-btn", type: "button", onclick: () => copy(sv.question, note, "The forecasting question") }, "Copy the question"),
            el("button", { class: "ws-btn", type: "button", onclick: () => catalystForm(seed) }, "Change the catalyst"))),
        el("ol", { class: "small" }, el("li", { text: "In the MiroFish dashboard, choose New Scenario, upload the packet and paste the question (it is on your clipboard)." }),
          el("li", { text: "Run the simulation, then paste its JSON result below. A text-only report: copy the output prompt and template, have them turn the report into JSON, or fill in the form." })),
        field("Result", result), field("Report excerpt", excerpt),
        el("div", { class: "ws-row" }, go,
          el("button", { class: "ws-btn", type: "button", onclick: () => copy(`${sv.output_prompt}\n\n${tpl}`, note, "The output prompt and template") }, "Copy the output prompt"),
          el("button", { class: "ws-btn", type: "button", onclick: () => copy(tpl, note, "The JSON template") }, "Copy the JSON template"),
          el("button", { class: "ws-btn", type: "button", onclick: () => caseForm(seed, sendImport, err) }, "Fill in a form instead")),
        err);
    }

    function caseForm(seed, send, err) {
      const blocks = [["central", "flat"], ["bullish", "up"], ["bearish", "down"]].map(([key, dir]) => {
        const f = {
          trigger: el("input", { type: "text", "aria-label": `${key} trigger`, maxlength: "400" }),
          direction: choose(`${key} direction`, ["up", "down", "flat", "mixed"].map(d => [d, DIR_WORDS[d]]), dir),
          reason: el("input", { type: "text", "aria-label": `${key} why`, maxlength: "500" }),
          type: choose(`${key} channel`, DRIVER_TYPES.map(t => [t, words(t)]), "issuer_event"),
          timing: choose(`${key} timing`, TIMINGS.map(t => [t, words(t)]), "first_session_after_release"),
          sources: el("input", { type: "text", "aria-label": `${key} sources`, placeholder: "S1, S3" }),
          priced: el("input", { type: "text", "aria-label": `${key} priced in`, maxlength: "600" }),
          invalid: el("input", { type: "text", "aria-label": `${key} invalidation`, maxlength: "400" }),
          missing: el("input", { type: "text", "aria-label": `${key} missing evidence`, placeholder: "Separate with ;" }),
        };
        const node = el("fieldset", { class: "ws-case-form", "data-case": key }, el("legend", { text: words(key).replace(/^./, x => x.toUpperCase()) }),
          el("div", { class: "ws-row" }, field("Trigger", f.trigger), field("Direction", f.direction)),
          el("div", { class: "ws-row" }, field("Why it could move the stock", f.reason), field("Channel", f.type), field("Timing", f.timing), field("Source IDs", f.sources)),
          el("div", { class: "ws-row" }, field("What may be priced in", f.priced), field("What would invalidate it", f.invalid), field("Missing evidence", f.missing)));
        return { key, f, node };
      });
      const save = el("button", { class: "ws-btn primary", type: "button" }, "Save these cases");
      save.addEventListener("click", () => {
        const cases = blocks.filter(b => b.f.trigger.value.trim()).map(({ key, f }) => ({ key, trigger: f.trigger.value.trim(), stock_direction: f.direction.value,
          drivers: f.reason.value.trim() ? [{ type: f.type.value, direction: f.direction.value === "mixed" ? "flat" : f.direction.value, timing: f.timing.value,
            reason: f.reason.value.trim(), source_ids: ids(f.sources.value) }] : [],
          priced_in: f.priced.value.trim(), invalidation: f.invalid.value.trim() || "Not stated",
          missing_evidence: f.missing.value.split(";").map(x => x.trim()).filter(Boolean), hypotheses: [] }));
        if (!cases.length) { err.textContent = "Write at least one case's trigger."; return; }
        send({ schema_version: 1, seed_id: seed.seed_id, symbol: seed.symbol, horizon_sessions: seed.horizon_sessions, cases });
      });
      body.append(el("div", { class: "panel ws-cat-form" }, ...blocks.map(b => b.node), el("div", { class: "ws-row" }, save)));
    }

    // 3. The cases, their reviewed assumptions, and Apply assumptions.
    async function openRun(runId) {
      const n = turn();
      try {
        const r = await w.api(`/v3/scenario-runs/${enc(runId)}`);
        if (fresh(n)) showRun(r);
      } catch (e) { fail(e, n); }
    }

    function showRun(r) {
      const run = r.run, seed = r.seed, sources = Object.fromEntries(seed.sources.map(x => [x.id, x]));
      const start = r.latest ? r.latest.assumptions : r.suggested;
      const byKey = Object.fromEntries((start || []).map(a => [a.key, a]));
      const implied = seed.volatility && isNum(seed.volatility.event_move_pct) ? seed.volatility.event_move_pct : null;
      const sessionPick = (label, v) => choose(label, seed.sessions.map(x => [String(x.index), `${x.index} (${x.date})`]), String(v || seed.catalyst_session || 1));
      const sourcePick = (label, v, allowImplied) => choose(label, Object.entries(SOURCE_WORDS).filter(([k]) => allowImplied || k !== "options_implied_move"), v || "user_stress");
      const editors = [];
      const cards = run.cases.map(c => {
        const a = byKey[c.key] || { key: c.key, visible: true, issuer: null, factors: [], dispersion: null };
        const iss = a.issuer || {};
        const ed = { key: c.key,
          visible: el("input", { type: "checkbox", "aria-label": `Draw ${c.label}`, checked: a.visible !== false }),
          jump: el("input", { type: "number", step: "0.1", "aria-label": `${c.label} stock-specific move`, value: isNum(iss.value_pct) ? String(iss.value_pct) : "" }),
          jumpSession: sessionPick(`${c.label} move session`, iss.session),
          jumpSource: sourcePick(`${c.label} move source`, iss.source, implied !== null),
          jumpIds: el("input", { type: "text", "aria-label": `${c.label} move sources`, placeholder: "S1", value: (iss.source_ids || []).join(", ") }),
          disp: el("input", { type: "number", step: "0.1", min: "0", "aria-label": `${c.label} extra uncertainty`, value: a.dispersion ? String(a.dispersion.value_pct) : "" }),
          factors: el("div", { class: "ws-cat-factors" }) };
        const addFactor = f => {
          f = f || {};
          const row = el("div", { class: "ws-row ws-cat-factor" },
            choose(`${c.label} factor`, seed.factors.map(x => [x.key, `${x.label} (${x.unit})`]), f.key || (seed.factors[0] || {}).key),
            el("input", { type: "number", step: "0.1", "aria-label": `${c.label} factor shock`, value: isNum(f.value) ? String(f.value) : "" }),
            sessionPick(`${c.label} factor session`, f.session || 1), sourcePick(`${c.label} factor source`, f.source, false),
            el("input", { type: "text", "aria-label": `${c.label} factor sources`, placeholder: "S2", value: (f.source_ids || []).join(", ") }),
            el("button", { class: "ws-btn ws-btn-mini", type: "button", onclick: e => e.currentTarget.parentElement.remove() }, "Remove"));
          ed.factors.append(row);
        };
        for (const f of a.factors || []) addFactor(f);
        editors.push(ed);
        const useImplied = implied === null ? "" : el("button", { class: "ws-btn ws-btn-mini", type: "button", title: "The move options price for the event, as a size reference",
          onclick: () => { ed.jump.value = String((c.stock_direction === "down" ? -1 : 1) * implied); ed.jumpSource.value = "options_implied_move"; ed.jumpSession.value = String(seed.catalyst_session || 1); } },
          `Use ±${implied.toFixed(1)}% (options)`);
        const driver = d => el("li", {}, `${d.reason} `, el("span", { class: "muted", text: `(${words(d.type)}, ${words(d.direction)}, ${words(d.timing)}${d.factor ? `, ${d.factor}` : ""}${isNum(d.proposed_value) ? `, proposed ${d.proposed_value}${d.proposed_unit}` : ""}) ` }),
          d.supported ? el("span", { class: "small", title: d.source_ids.map(x => (sources[x] ? `${x}: ${sources[x].title}` : x)).join("; "), text: d.source_ids.join(", ") })
            : statusTag("warn", "Hypothesis: no exported source"));
        return el("article", { class: `ws-case ${c.key}`, "data-case": c.key, style: `border-left-color: ${CASE_COLORS[c.key]}` },
          el("h5", {}, `${c.label} `, el("span", { class: "muted small", text: DIR_WORDS[c.stock_direction] || c.stock_direction })),
          el("p", { class: "small" }, el("strong", { text: "Trigger: " }), c.trigger),
          c.drivers.length ? el("div", { class: "small" }, el("strong", { text: "Why it could move the stock" }), el("ul", { class: "plain" }, c.drivers.map(driver))) : "",
          c.priced_in ? el("p", { class: "small" }, el("strong", { text: "What may be priced in: " }), c.priced_in) : "",
          el("p", { class: "small" }, el("strong", { text: "Invalidated if: " }), c.invalidation),
          c.missing_evidence.length ? el("p", { class: "small muted" }, "Missing: ", c.missing_evidence.join("; ")) : "",
          c.hypotheses.length ? el("p", { class: "small" }, statusTag("warn", "Hypotheses"), ` ${c.hypotheses.join("; ")}`) : "",
          isNum(c.provider_confidence) ? el("p", { class: "small muted", text: `The provider's own confidence ${Math.round(100 * c.provider_confidence)}% (its metadata; not a probability).` }) : "",
          el("details", { class: "ws-more ws-cat-assume", open: c.key !== "central" || !!(a.issuer || (a.factors || []).length) },
            el("summary", { text: "Assumptions (sizes, timing and sources)" }),
            el("div", { class: "ws-row" }, field("Stock-specific move, pp", ed.jump), field("From session", ed.jumpSession), field("Source", ed.jumpSource), field("Source IDs", ed.jumpIds), useImplied),
            el("div", { class: "small muted", text: "Factor shocks, in each factor's unit, from their session on. A factor left out moves only as the shocked ones above it imply." }),
            ed.factors, seed.factors.length ? el("button", { class: "ws-btn ws-btn-mini", type: "button", onclick: () => addFactor() }, "Add a factor shock")
              : el("p", { class: "small muted", text: "No factor fit in this packet." }),
            el("div", { class: "ws-row" }, field("Extra event uncertainty, pp (from the move's session)", ed.disp), el("label", { class: "check" }, ed.visible, " Draw on the chart"))));
      });
      const apply = el("button", { class: "ws-btn primary", type: "button" }, "Apply assumptions");
      const err = el("div", { class: "small", "aria-live": "polite" });
      const out = el("div");
      apply.addEventListener("click", async () => {
        const n = turn();
        const num = i => (i.value.trim() === "" ? null : Number(i.value));
        const cases = editors.map(ed => {
          const jump = num(ed.jump), disp = num(ed.disp);
          return { key: ed.key, visible: ed.visible.checked,
            issuer: jump === null || jump === 0 ? null : { value_pct: jump, session: Number(ed.jumpSession.value), source: ed.jumpSource.value, source_ids: ids(ed.jumpIds.value) },
            factors: [...ed.factors.children].map(row => { const [k, v, ss, src, idl] = row.querySelectorAll("select, input");
              return { key: k.value, value: num(v), session: Number(ss.value), source: src.value, source_ids: ids(idl.value) }; }).filter(f => f.value !== null && f.value !== 0),
            dispersion: disp ? { value_pct: disp, session: Number(ed.jumpSession.value), source: ed.jumpSource.value === "options_implied_move" ? "user_stress" : ed.jumpSource.value, source_ids: ids(ed.jumpIds.value) } : null };
        });
        apply.disabled = true; err.replaceChildren(loading("Computing the paths…"));
        try {
          const v = await w.api(`/v3/scenario-runs/${enc(run.id)}/versions`, { parent_version: r.latest ? r.latest.version : null, cases });
          if (!fresh(n)) return;
          err.replaceChildren();
          r.latest = v; r.versions.push({ id: v.id, version: v.version, created_at: v.created_at });
          showVersion(v, out, r);
          drawVersion(v);
        } catch (e) { if (e !== WS.GONE && fresh(n)) err.replaceChildren(el("p", { class: "error", role: "alert", text: e.message })); }
        apply.disabled = false;
      });
      body.replaceChildren(
        el("p", { class: "small muted", text: `${seed.catalyst.title} · cases imported ${new Date(run.created_at).toLocaleString()} (${run.provider}) · packet ${seed.seed_id} · ${seed.catalyst_timing_note}` }),
        el("div", { class: "ws-cases" }, cards),
        run.report_excerpt ? el("details", { class: "ws-more" }, el("summary", { text: "The provider's report (excerpt, shown as text)" }), el("pre", { class: "ws-cat-report", text: run.report_excerpt })) : "",
        el("div", { class: "ws-row" }, apply, el("button", { class: "ws-btn", type: "button", onclick: () => reimport(seed.seed_id) }, "Import other cases")),
        err, out);
      if (r.latest) showVersion(r.latest, out, r);
    }
    async function reimport(seedId) {
      const n = turn();
      try { const sv = await w.api(`/v3/scenario-seeds/${enc(seedId)}`); if (fresh(n)) importPanel(sv); } catch (e) { fail(e, n); }
    }

    function showVersion(v, out, r) {
      const last = v.horizon_sessions - 1;
      const drawn = () => { const sc = w.ctx().scenario; return !!(sc && sc.catalyst && sc.catalyst.version_id === v.id); };
      const pick = r.versions.length > 1 ? choose("Version", [...r.versions].reverse().map(x => [x.id, `Version ${x.version}, ${new Date(x.created_at).toLocaleString()}`]), v.id) : null;
      if (pick) pick.addEventListener("change", async () => {
        const n = turn();
        try { const old = await w.api(`/v3/scenario-versions/${enc(pick.value)}`); if (fresh(n)) showVersion(old, out, r); } catch (e) { fail(e, n); }
      });
      const rows = [{ key: "base", label: "Base forecast", return_pct: v.base.return_pct, price: v.base.price, low_price: v.base.low_price, high_price: v.base.high_price,
        attribution: null, terminal: { vs_base_pp: 0 } }, ...v.cases];
      const status = el("span", { class: "small muted", text: drawn() ? "Drawn on the linked chart." : "" });
      out.replaceChildren(el("div", { class: "panel ws-cat-version", "data-version": String(v.version) },
        el("div", { class: "ws-row" }, el("strong", { text: `Version ${v.version}` }), pick ? field("Show", pick) : "",
          el("button", { class: "ws-btn", type: "button", onclick: () => { drawVersion(v); status.textContent = "Drawn on the linked chart."; } }, "Draw on the linked chart"), status),
        v.stale ? el("p", { class: "small" }, statusTag("info", "Past"), ` ${v.stale_note}`) : "",
        table([{ label: "Case", cell: c => el("span", {}, el("span", { class: "ws-swatch", style: `background: ${c.key === "base" ? FORECAST_COLOR : CASE_COLORS[c.key]}` }), ` ${c.label}`) },
          { label: `By session ${v.horizon_sessions}`, num: true, cell: c => pctText(c.return_pct[last]) },
          { label: "Price", num: true, cell: c => money(c.price[last]) },
          { label: "Vs base", num: true, cell: c => (c.key === "base" ? "" : `${c.terminal.vs_base_pp >= 0 ? "+" : ""}${c.terminal.vs_base_pp.toFixed(2)} pts`) },
          { label: "Factors", num: true, cell: c => (c.attribution ? `${c.attribution.factor_pct[last] >= 0 ? "+" : ""}${c.attribution.factor_pct[last].toFixed(2)} pts` : "") },
          { label: "Stock-specific", num: true, cell: c => (c.attribution ? `${c.attribution.issuer_pct[last] >= 0 ? "+" : ""}${c.attribution.issuer_pct[last].toFixed(2)} pts` : "") },
          { label: "Range", num: true, cell: c => `${money(c.low_price[last])} to ${money(c.high_price[last])}` }], rows),
        ...v.cases.flatMap(c => c.warnings.map(x => el("p", { class: "small" }, statusTag("warn", c.label), ` ${x}`))),
        ...v.cases.filter(c => c.attribution.terms.length).map(c => el("p", { class: "small muted", text: `${c.label} factors: ${c.attribution.terms.map(t => `${t.label} ${t.value >= 0 ? "+" : ""}${t.value}${t.unit} from session ${t.session} (its own part ${t.unique_move_pct.toFixed(2)}% × ${t.beta_unique.toFixed(2)} = ${t.contribution_pp >= 0 ? "+" : ""}${t.contribution_pp.toFixed(2)} pts)`).join("; ")}.` })),
        el("p", { class: "small muted", text: `${v.band_note} Anchor ${money(v.anchor.price)}, read ${new Date(v.anchor.at).toLocaleString()}; calculation ${v.calc_version}.` })));
    }

    function drawVersion(v) {
      const fc = st.forecast;
      w.set({ scenario: { kind: "forecast", study: st.id, symbol: st.symbol, horizon: v.horizon_sessions, calibrated: v.base.calibrated,
        base: { path_pct: v.base.return_pct, low_pct: v.base.low_pct, high_pct: v.base.high_pct }, anchor: v.anchor, sessions: v.sessions,
        alternatives: fc ? fc.alternatives.map(a => ({ key: a.key, label: a.label, path_pct: a.path_pct })) : [],
        catalyst: { run_id: v.run_id, version_id: v.id, version: v.version,
          cases: v.cases.filter(c => c.visible && c.quantified).map(c => ({ key: c.key, label: c.label, price: c.price, trigger: c.trigger })) } } });
      if (!WS.find(w, "chart")) WS.toast("Link a stock chart to this widget (the same colour) to see the scenarios on it.");
    }
  }

  /* ---------- the option's own price chart ---------- */
  WS.register("optionchart", { title: "Option chart", category: "Options", size: { w: 4, h: 8 }, minW: 3, create(w) {
    let chart = null, ticket = 0, lastKind = null, premium = null, stock = null, unsync = null;
    const range = el("select", { "aria-label": "Range" }, [["30", "1 month"], ["90", "3 months"], ["180", "6 months"]].map(([v, t]) => el("option", { value: v, selected: v === String(w.config.days || 90) }, t)));
    const note = el("div", { class: "small" });
    const box = el("div", { class: "lw-chart", style: "height: 240px" });
    w.body.append(el("div", { class: "ws-row" }, field("Range", range)), note, box);
    range.addEventListener("change", () => { w.config.days = Number(range.value); w.save(); load(); });
    function dispose() { if (unsync) { unsync(); unsync = null; } if (chart) { chart.remove(); chart = null; } premium = stock = null; }
    async function load() {
      const c = w.ctx(), k = c.contract && c.contract.symbol === c.symbol ? c.contract : null, mine = ++ticket;
      dispose();
      if (!k || !k.option_symbol) { w.setBadge(c.symbol || ""); note.replaceChildren(WS.empty("Select a contract in the options chain to see its own price history.")); return; }
      w.setBadge(`${k.expiration} ${k.strike} ${String(k.option_type).toUpperCase()}`);
      note.replaceChildren(loading("Loading the contract's history…"));
      try {
        const d = await w.api(`/v3/options/history?option_symbol=${enc(k.option_symbol)}&days=${enc(range.value)}`);
        if (mine !== ticket) return;
        const actual = d.kind === "actual";
        lastKind = `${actual ? "traded prices" : "theoretical (not traded)"} from ${d.source}`;
        note.replaceChildren(statusTag(actual ? "pass" : "warn", actual ? "Traded prices" : "Theoretical, not traded"), ` ${d.source}. `, ...d.notes.map(n => el("span", { class: "muted", text: `${n} ` })));
        const lib = window.LightweightCharts, col = chartColors();
        chart = lib.createChart(box, { autoSize: true, layout: { background: { color: col.bg }, textColor: col.muted, attributionLogo: false },
          grid: { vertLines: { color: col.line }, horzLines: { color: col.line } }, leftPriceScale: { visible: true }, rightPriceScale: { visible: true },
          timeScale: { borderColor: col.line } });
        const toTime = t => t;   // seconds, the contract's day at 16:00 New York
        if (actual && d.bars.length && isNum(d.bars[0].open)) {
          premium = chart.addSeries(lib.CandlestickSeries, { upColor: col.up, downColor: col.down, borderVisible: false, wickUpColor: col.up, wickDownColor: col.down, title: "Premium" });
          premium.setData(d.bars.map(b => ({ time: toTime(b.t), open: b.open, high: b.high, low: b.low, close: b.close })));
        } else {
          premium = chart.addSeries(lib.LineSeries, { color: col.warn, lineWidth: 2, lineStyle: actual ? 0 : 2, title: actual ? "Premium" : "Theoretical" });
          premium.setData(d.bars.map(b => ({ time: toTime(b.t), value: b.close })));
        }
        if (d.underlying_bars.length) {
          stock = chart.addSeries(lib.LineSeries, { color: col.muted, lineWidth: 1, priceScaleId: "left", title: d.underlying, lastValueVisible: true, priceLineVisible: false });
          stock.setData(d.underlying_bars.map(b => ({ time: toTime(b.t), value: b.close })));
        }
        chart.timeScale().fitContent();
        sync(d);
      } catch (e) { if (mine === ticket && e !== WS.GONE) note.replaceChildren(el("p", { class: "error", text: e.message })); }
    }
    // 3.1: the linked stock chart's crosshair, by day: hovering a day on either chart marks the same day on the other.
    function sync(d) {
      if (unsync) { unsync(); unsync = null; }
      const days = new Map(d.bars.map(b => [WS.dayOf(b.t), b]));
      const onStock = day => {
        if (!chart || !premium) return;
        const b = day && days.get(day);
        if (b) chart.setCrosshairPosition(b.close, b.t, premium); else chart.clearCrosshairPosition();
      };
      let placing = false;
      const offHub = WS.crosshair.listen(w, "option", day => { placing = true; try { onStock(day); } finally { placing = false; } });
      const move = p => { if (placing) return; WS.crosshair.say(w, "option", p && p.time ? WS.dayOf(p.time) : null); };
      chart.subscribeCrosshairMove(move);
      unsync = () => { offHub(); if (chart) chart.unsubscribeCrosshairMove(move); };
    }
    return { onContext(c, changed) { if (["symbol", "contract"].some(x => changed.includes(x))) load(); }, refresh: load, dispose,
      config: () => ({ days: Number(range.value) }), helpFacts: () => (lastKind ? [["History", lastKind]] : []) };
  } });

  /* ---------- the forecast's record: calibration on history ---------- */
  WS.register("forecastrecord", { title: "Forecast record", category: "Research", size: { w: 6, h: 9 }, symbolBadge: false, create(w) {
    const horizon = el("select", { "aria-label": "Horizon" }, [1, 3, 5, 10, 20].map(h => el("option", { value: String(h), selected: h === (w.config.horizon || 5) }, `${h} day${h === 1 ? "" : "s"}`)));
    const go = el("button", { class: "ws-btn", type: "button" }, "Run the calibration");
    const check = el("button", { class: "ws-btn", type: "button", title: "Read-only: a quote, two weeks of daily bars, an option chain and the market clock" }, "Check Tradier data");
    const checkOut = el("div", { class: "ws-datacheck", "aria-live": "polite" });
    const out = el("div");
    let timer = null;
    w.body.append(el("div", { class: "ws-row" }, field("Horizon", horizon), go, check), checkOut, out);
    horizon.addEventListener("change", () => { w.config.horizon = Number(horizon.value); w.save(); load(); });
    const runIt = async () => {
      try { await api("/v3/forecast/calibrate", { horizon_days: Number(horizon.value) }); load(); }
      catch (e) { out.prepend(el("p", { class: "error", text: e.message })); }
    };
    go.addEventListener("click", runIt);
    // 4.2: Check Tradier data, read-only, each request reported on its own.
    check.addEventListener("click", async () => {
      check.disabled = true; checkOut.replaceChildren(loading("Checking Tradier…"));
      try {
        const r = await w.api("/v3/data/tradier-check", { symbol: w.ctx().symbol || "SPY" });
        checkOut.replaceChildren(el("div", { class: "panel" },
          el("p", {}, statusTag(r.ok ? "pass" : r.configured ? "warn" : "info", r.ok ? "Tradier data works" : r.configured ? "Tradier data: some checks failed" : "Tradier is not set up"),
            ` ${r.configured ? `${r.provider}: ${r.status} ` : ""}${r.message}`),
          r.checks && r.checks.length ? el("ul", { class: "plain small" }, r.checks.map(c => el("li", {}, statusTag(c.ok ? "pass" : "fail", c.label), ` ${c.ok ? c.text : c.error || c.text}`))) : "",
          r.rate && isNum(r.rate.available) ? el("p", { class: "small muted", text: `Requests left in this minute's allowance: ${r.rate.available}.` }) : "",
          el("p", { class: "small muted", text: r.note || "" })));
      } catch (e) { if (e !== WS.GONE) checkOut.replaceChildren(el("p", { class: "error", text: e.message })); }
      check.disabled = false;
    });
    async function load() {
      clearTimeout(timer);
      if (w.disposed) return;
      try {
        const d = await w.api(`/v3/forecast/calibration?horizon_days=${enc(horizon.value)}`);
        const c = d.contract, cal = d.calibration, job = d.job, rd = d.readiness;
        const word = CAL_WORDS[c.status] || ["warn", c.status];
        const kids = [el("p", {}, rd && rd.label ? statusTag(rd.state === "ready" ? (rd.range && rd.range.status === "validated" ? "pass" : "warn") : "warn", rd.label) : statusTag(word[0], word[1]), " ", c.text)];
        if (rd) kids.push(readinessBlock(rd));
        if (job && (job.state === "running" || job.state === "queued" || job.state === "deferred")) {
          const words = job.state === "running" ? `Testing forecasts on history: ${job.done || 0} of ${job.total || "?"} stocks${job.current ? ` (${job.current})` : ""}…`
            : job.state === "deferred" ? `Waiting for the data source's quota to reset (${job.until ? new Date(job.until).toLocaleTimeString() : "soon"}); it then carries on.`
              : `Queued${job.reason === "warm" ? " (kept fresh in the background)" : ""}${job.note ? `: ${job.note}` : ""}…`;
          kids.unshift(loading(words));
          timer = setTimeout(load, 1500);
        } else if (job && job.state === "failed") {
          kids.unshift(el("div", { class: "ws-row" }, el("p", { class: "error", text: `The calibration failed: ${job.error}` }),
            el("button", { class: "ws-btn", type: "button", onclick: runIt }, "Retry")));
        }
        if (cal) {
          kids.push(el("div", { class: "metrics" },
            metric(isNum(cal.coverage) ? `${Math.round(100 * cal.coverage)}%` : "n/a", "of holdout outcomes inside the band"),
            metric(isNum(cal.mae_pct) ? `${cal.mae_pct.toFixed(2)} pts` : "n/a", "median's average miss"),
            metric(isNum(cal.flat_mae_pct || cal.baseline_mae_pct) ? `${(cal.flat_mae_pct || cal.baseline_mae_pct).toFixed(2)} pts` : "n/a", "assuming no move"),
            metric(isNum(cal.directional_accuracy) ? `${Math.round(100 * cal.directional_accuracy)}%` : "n/a", "direction right when it leaned")));
          const regimes = Object.entries(cal.by_regime || {});
          if (regimes.length) kids.push(el("h4", { text: "By the market's regime" }), table([
            { label: "Regime", cell: r => r[0] }, { label: "Cases", num: true, cell: r => String(r[1].cases) },
            { label: "Inside the band", num: true, cell: r => (isNum(r[1].coverage) ? `${Math.round(100 * r[1].coverage)}%` : "n/a") },
            { label: "Average miss", num: true, cell: r => (isNum(r[1].mae_pct) ? r[1].mae_pct.toFixed(2) : "n/a") }], regimes));
          if ((cal.failed || []).length) kids.push(el("details", { class: "ws-more" }, el("summary", { text: `Stocks that could not be used (${cal.failed.length})` }),
            el("ul", { class: "plain small" }, cal.failed.map(f => el("li", { text: `${f.symbol}: ${f.reason}` })))));
          if (cal.last_attempt) kids.push(el("p", { class: "small muted", text: `The last refresh (${cal.last_attempt.run_at ? new Date(cal.last_attempt.run_at).toLocaleString() : ""}) found too little history and was kept beside this calibration, not over it: ${cal.last_attempt.text || ""}` }));
          kids.push(el("p", { class: "small muted", text: `Weights: ${Object.entries(cal.weights).map(([k, v]) => `${k} ${v.toFixed(2)}`).join(", ")}. ${cal.symbols ? `${cal.symbols.length} stocks, ` : ""}${cal.years || ""} years${cal.cutoff ? ` to ${cal.cutoff}` : ""}, run ${cal.run_at ? new Date(cal.run_at).toLocaleString() : ""} on ${cal.data_source || ""}; method ${cal.method || "cal-3.0"}.` }));
        }
        out.replaceChildren(...kids);
      } catch (e) { if (e !== WS.GONE) out.replaceChildren(el("p", { class: "error", text: e.message })); }
    }
    load();
    w.on("study.calibrated", load);
    return { refresh: load, dispose() { clearTimeout(timer); }, config: () => ({ horizon: Number(horizon.value) }) };
  } });

  /* ---------- the stock chart draws a study's forecast, and its Catalyst Scenarios ----------
     4.1: in the study's fixed coordinates when it has them (its anchor price, read when the study ran, and its sessions
     by date), never the chart's newest close; a scenario's cases are drawn from their stored prices. Daily bars only:
     the paths are in trading sessions. Older saved contexts without a frame still draw from the chart, as before. */
  WS.chartOverlays = WS.chartOverlays || [];
  WS.chartOverlays.push((chart, c) => {
    const sc = c.scenario;
    if (!sc || sc.kind !== "forecast" || sc.symbol !== c.symbol) { chart.setLines("forecast", []); return; }
    const a = chart.chartArgs();
    if (!a || a.group !== "1d") { chart.setLines("forecast", []); return; }   // the forecast is in trading days: daily bars only
    const fixed = sc.anchor && isNum(sc.anchor.price) && Array.isArray(sc.sessions) && sc.sessions.length >= sc.horizon;
    let t0, p0, times;
    if (fixed) { t0 = dayTime(sc.anchor.bar_date); p0 = sc.anchor.price; times = sc.sessions.map(x => dayTime(x.date)); }
    else {
      if (!a.future_times || a.future_times.length < sc.horizon || !isNum(a.last_close)) { chart.setLines("forecast", []); return; }
      t0 = a.last_time; p0 = a.last_close; times = a.future_times;
    }
    const pts = arr => [{ time: t0, value: p0 }].concat(arr.map((p, i) => ({ time: times[i], value: p0 * (1 + p / 100) })));
    const day = i => (fixed ? ` · session ${i + 1}, ${sc.sessions[i].date} close` : ` · session ${i + 1}`);
    const tipsOf = (label, prices, why) => Object.fromEntries(prices.map((v, i) => [times[i], `${label}: ${money(v)} (${pctText(100 * (v / p0 - 1))})${day(i)}${why ? ` · if ${why}` : ""}`]));
    const lines = [
      { key: "forecast:base", points: pts(sc.base.path_pct), color: FORECAST_COLOR, width: 2, style: 0, title: sc.catalyst ? "Base" : "Forecast",
        tips: tipsOf("Base forecast", sc.base.path_pct.map(p => p0 * (1 + p / 100))) },
      { key: "forecast:low", points: pts(sc.base.low_pct), color: FORECAST_COLOR + "99", width: 1, style: 3, title: sc.calibrated ? "80% band" : "Model P10" },
      { key: "forecast:high", points: pts(sc.base.high_pct), color: FORECAST_COLOR + "99", width: 1, style: 3, title: sc.calibrated ? "" : "Model P90" },
    ];
    if (sc.catalyst && fixed) {
      for (const k of sc.catalyst.cases || []) {
        lines.push({ key: `forecast:case:${k.key}`, points: [{ time: t0, value: p0 }].concat(k.price.map((v, i) => ({ time: times[i], value: v }))),
          color: CASE_COLORS[k.key] || ALT_COLOR, width: 2, style: 2, title: `${k.label} (v${sc.catalyst.version})`, tips: tipsOf(k.label, k.price, k.trigger) });
      }
    } else lines.push(...(sc.alternatives || []).map(alt => ({ key: `forecast:${alt.key}`, points: pts(alt.path_pct), color: ALT_COLOR, width: 1, style: 2, title: alt.label })));
    chart.setLines("forecast", lines);
    if (chart.showAhead) chart.showAhead(sc.horizon);
  });

  /* ---------- Research's standard layout, 3.1 ----------
     The chart is the largest thing on the page, with the study beside it in sections; the options chain and the
     option's own chart sit together under it, with the risk panel; the strategy builder, the news and the forecast's
     record below. The what-if Scenario explorer is an advanced panel (Add widget, Advanced), not part of it: its line
     is a different model from the study's forecast. */
  WS.DEFAULT_LAYOUTS.research = [
    { type: "chart", x: 0, y: 0, w: 7, h: 10, group: "blue" },
    { type: "study", x: 7, y: 0, w: 5, h: 10, group: "blue" },
    { type: "chain", x: 0, y: 10, w: 4, h: 8, group: "blue" },
    { type: "optionchart", x: 4, y: 10, w: 4, h: 8, group: "blue" },
    { type: "riskreward", x: 8, y: 10, w: 4, h: 8, group: "blue" },
    { type: "strategy", x: 0, y: 18, w: 4, h: 7, group: "blue" },
    { type: "news", x: 4, y: 18, w: 4, h: 7, group: "blue" },
    { type: "forecastrecord", x: 8, y: 18, w: 4, h: 7, group: "blue" },
  ];
})();
