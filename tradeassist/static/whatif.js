/* What-if analysis on the interactive chart (2.13).

   whatIfWidget(symbol, chartApi) returns { widget, position, dispose }: two panels for the Analyze screen. The
   widget lists the factors (overseas markets, the 10-year yield, oil, the dollar, the VIX, the sector fund, peers
   you name), the news and analyst calls, earnings and the similar-pattern path, each with today's reading, this
   stock's sensitivity and a scenario value you can change; the model's path and 10-90 band are drawn on the
   chart (orange) and remodelled as you type. "Ask ChatGPT" or "Ask Claude" sends the same inputs to the AI you
   pick and draws its path in purple, with its reasoning and the settings it would use, which one click applies.
   The position panel values shares, a short or any option strategy at the horizon under either scenario, for the
   quantity you set, and ranks them all. Models only: nothing here places an order. */

const WI_MODEL_COLOR = "#e67e22", WI_AI_COLOR = "#9b59b6";
const WI_NUM_FIELDS = [["pnl_p10", "Downside (10th pct)"], ["pnl_p50", "Middle (50th)"], ["pnl_p90", "Upside (90th)"]];

// Pure helpers (tested in Node): the scenario lines from a model, the scenario figures for the position call.
const WHATIFX = (() => {
  function pathLines(prefix, m, color, title, dashed) {
    if (!m || !m.times || !m.times.length) return [];
    const start = { time: m.last_time, value: m.last };
    const pts = key => (m.times.length && isNum(m.last_time) ? [start] : []).concat(m.times.map((t, i) => ({ time: t, value: m[key][i] })));
    return [
      { key: `${prefix}:path`, points: pts("path"), color, width: 2, style: dashed ? 2 : 0, title },
      { key: `${prefix}:low`, points: pts("low"), color: color + "99", width: 1, style: dashed ? 1 : 3 },
      { key: `${prefix}:high`, points: pts("high"), color: color + "99", width: 1, style: dashed ? 1 : 3 },
    ];
  }
  function scenarioOf(m) {
    if (!m || !isNum(m.expected_pct)) return null;
    const sd = Array.isArray(m.sd_pct) && m.sd_pct.length ? m.sd_pct[m.sd_pct.length - 1] : null;
    return { expected_pct: m.expected_pct, sd_pct: isNum(sd) ? sd : 2 };
  }
  function quantities(kind, qty) {
    const shares = kind === "shares" || kind === "short";
    return shares ? { quantity: qty, contracts: Math.max(1, Math.round(qty / 100)) } : { quantity: qty * 100, contracts: qty };
  }
  return { pathLines, scenarioOf, quantities };
})();

function whatIfWidget(symbol, chartApi) {
  const st = { setup: null, scenario: {}, include: {}, horizon: null, peers: "", provider: "chatgpt", ai: null, model: null, modelPending: 0,
    source: "model", kind: "shares", quantity: 100, request: 0, timer: null, rank: null, lastArgs: null, disposed: false };
  const err = el("div", { class: "error", role: "alert" });
  const status = el("div", { class: "small muted" });
  const horizonSel = el("select", { "aria-label": "What-if horizon" });
  const peersIn = el("input", { type: "text", placeholder: "e.g. TSM, ASML", "aria-label": "Foreign peers", size: 14 });
  const peersBtn = el("button", { class: "plain small-btn", text: "Use peers" });
  const resetBtn = el("button", { class: "plain small-btn", text: "Reset to the defaults" });
  const refreshBtn = el("button", { class: "plain small-btn", text: "Refresh readings" });
  const rowsBox = el("div", { class: "wi-rows" });
  const summary = el("div", { class: "wi-summary" });
  const providerSel = el("select", { "aria-label": "AI to ask" });
  const askBtn = el("button", { class: "primary small-btn", text: "Ask ChatGPT" });
  const aiBox = el("div", { class: "wi-ai" });
  const widget = el("div", { class: "panel wi-panel" },
    el("h3", { text: "What if?" }),
    el("p", { class: "small muted", text: "Each factor's move times how much this stock has moved with it, plus the pattern's path and the news record, makes the orange line on the chart with its 10-90 band. Change any number to remodel. Ask ChatGPT or Claude for its own scenario (purple), then apply its settings if you like them." }),
    err, status,
    el("div", { class: "row" }, field("Horizon", horizonSel), field("Foreign peers", el("span", {}, peersIn, " ", peersBtn)), resetBtn, refreshBtn),
    rowsBox, summary,
    el("div", { class: "row wi-ask" }, field("AI", providerSel), askBtn),
    aiBox);

  // --- the position panel -------------------------------------------------------------------------------------
  const sourceSel = el("select", { "aria-label": "Scenario for the position" }, el("option", { value: "model", text: "The model's scenario" }), el("option", { value: "ai", text: "The AI's scenario", disabled: true }));
  const kindSel = el("select", { "aria-label": "Strategy" });
  const qtyIn = el("input", { type: "number", min: "1", step: "1", value: "100", "aria-label": "Quantity" });
  const qtyLabel = el("span", { class: "small muted", text: "shares" });
  const rankBtn = el("button", { class: "plain small-btn", text: "Rank every strategy" });
  const posBox = el("div", { class: "wi-position" });
  const rankBox = el("div");
  const position = el("div", { class: "panel wi-pos-panel" },
    el("h3", { text: "A position under the scenario" }),
    el("p", { class: "small muted", text: "Shares, a short, or an option strategy from the builder, valued at the horizon across the scenario's 10-90 range of prices. Set the quantity to see the downside and upside in dollars. A model only: no order is placed." }),
    el("div", { class: "row" }, field("Scenario", sourceSel), field("Strategy", kindSel), field("Quantity", el("span", {}, qtyIn, " ", qtyLabel)), rankBtn),
    posBox, rankBox);

  // --- helpers ------------------------------------------------------------------------------------------------
  const fmtReading = r => r.reading === null || r.reading === undefined ? "no reading" : `${r.reading > 0 ? "+" : ""}${num(r.reading, r.unit === "bp" ? 0 : 2)} ${r.unit}`;
  function args() {
    const a = chartApi.chartArgs();
    if (!a) return null;
    const out = { symbol, range: a.range, group: a.group, start: a.start, end: a.end, window_start: a.window_start, window_end: a.window_end,
      min_correlation: a.min_correlation, horizon: st.horizon || a.horizon || undefined, peers: st.peers ? st.peers.split(",").map(x => x.trim()).filter(Boolean) : [] };
    for (const k of Object.keys(out)) if (out[k] === null || out[k] === undefined) delete out[k];
    return out;
  }
  function lineModel(m, a) {
    // The server's model in chart terms: its future times, and the close the path starts from.
    if (!m || !a) return null;
    return { ...m, last_time: a.last_time, last: isNum(m.last) ? m.last : a.last_close, times: m.times && m.times.length ? m.times : (a.future_times || []).slice(0, m.path.length) };
  }
  function drawModel() { chartApi.setLines("whatif:model", WHATIFX.pathLines("whatif:model", st.model, WI_MODEL_COLOR, "model", false)); }
  function drawAI() { chartApi.setLines("whatif:ai", st.ai ? WHATIFX.pathLines("whatif:ai", st.ai.line, WI_AI_COLOR, st.ai.label, true) : []); }

  // --- loading the setup -------------------------------------------------------------------------------------
  async function load(refresh, reset) {
    const a = args();
    if (!a) return;
    const mine = ++st.request;
    ++st.modelPending; ++posPending;             // replies to the old question are dropped
    err.textContent = "";
    status.textContent = "Reading today's factors…";
    const p = new URLSearchParams();
    for (const [k, v] of Object.entries(a)) if (k !== "symbol" && k !== "peers" && v !== undefined) p.set(k, String(v));
    if (a.peers.length) p.set("peers", a.peers.join(","));
    if (refresh) p.set("refresh", "1");
    try {
      const s = await api(`/v2/whatif/${encodeURIComponent(symbol)}?${p}`);
      if (mine !== st.request || st.disposed) return;
      const sameQuestion = st.setup && st.setup.group === s.group && !reset && !refresh;
      // The user's values survive a change of horizon or peers; a new grouping, a refresh or the reset button start over.
      st.scenario = sameQuestion ? { ...s.scenario, ...st.scenario } : { ...s.scenario };
      st.include = sameQuestion ? { ...s.include, ...st.include } : { ...s.include };
      if (st.ai && (st.ai.horizon !== s.horizon || st.ai.window !== JSON.stringify([a.group, a.window_start, a.window_end]))) clearAI();
      st.setup = s;
      st.horizon = s.horizon;
      st.lastArgs = a;
      renderSetup(s);
      if (sameQuestion) remodelNow(); else { st.model = lineModel(s.model, chartApi.chartArgs()); renderModel(s.model); drawModel(); }
      drawAI();
      chartApi.showAhead(s.horizon);
      renderPosition();
    } catch (e) {
      if (mine !== st.request) return;
      err.textContent = e.message;
      status.textContent = "";
    }
  }
  function renderSetup(s) {
    const src = s.stooq && s.stooq.enabled ? (s.stooq.last_error ? "stooq.com did not answer, so the funds stand in for the real indexes" : "real indexes from stooq.com where it answered, the funds otherwise") : "readings from the proxy funds";
    status.textContent = `${s.premarket ? "Before the open: today's readings are the starting scenario, since the stock has not priced them yet." : "The stock is trading or closed: today's readings are already in its price, so the scenario starts at no further move; the readings are shown for reference."} ${src.charAt(0).toUpperCase() + src.slice(1)}. Price ${money(s.price)}; ${s.data_source === "demo" ? "demo data, made up." : "sensitivities from the last year of daily bars."}`;
    horizonSel.replaceChildren(...s.horizons.map(h => el("option", { value: String(h.bars), text: h.label, selected: h.bars === s.horizon })));
    const prov = s.providers || [];
    providerSel.replaceChildren(...prov.map(pv => el("option", { value: pv.key, text: `${pv.label}${pv.configured ? ` (${pv.model})` : " (not set up)"}`, selected: pv.key === st.provider })));
    const chosen = prov.find(pv => pv.key === providerSel.value) || prov[0];
    askBtn.textContent = `Ask ${chosen ? chosen.label : "the AI"}`;
    askBtn.disabled = !(chosen && chosen.configured);
    askBtn.title = chosen && !chosen.configured ? chosen.reason || "" : "";
    kindSel.replaceChildren(...Object.entries(s.strategies || {}).map(([k, t]) => el("option", { value: k, text: `${t.label}${t.group ? ` · ${t.group}` : ""}`, selected: k === st.kind })));
    // The rows: one per factor, then the non-price ones.
    const rows = [];
    for (const r of s.factors) {
      const input = el("input", { type: "number", step: r.unit === "bp" ? "1" : "0.1", value: String(Math.round((st.scenario[r.key] || 0) * 100) / 100), "aria-label": `${r.label} scenario` });
      const inc = el("input", { type: "checkbox", checked: !!st.include[r.key], disabled: r.beta === null || r.beta === undefined, "aria-label": `Include ${r.label}` });
      input.addEventListener("input", () => { st.scenario[r.key] = Number(input.value) || 0; remodel(); });
      inc.addEventListener("change", () => { st.include[r.key] = inc.checked; remodel(); });
      rows.push(el("div", { class: "wi-row", "data-factor": r.key, title: r.note || "" },
        el("div", { class: "wi-label" }, el("strong", { text: r.label }), r.key === "peers" && !(r.peers || []).length ? el("div", { class: "small muted", text: "Name peers above." }) : ""),
        el("div", { class: "wi-reading small" }, el("span", { text: fmtReading(r) }), el("div", { class: "muted", text: r.reading_source || "" })),
        el("div", { class: "wi-beta small" }, isNum(r.beta) ? el("span", { text: `β ${r.beta > 0 ? "+" : ""}${num(r.beta, 2)}` }) : el("span", { class: "muted", text: "no history" }),
          isNum(r.correlation) ? el("div", { class: "muted", text: `corr ${num(r.correlation, 2)}, ${r.beta_days} days` }) : ""),
        el("div", { class: "wi-input" }, input, el("span", { class: "small muted", text: ` ${r.unit}` })),
        el("div", { class: "wi-include" }, el("label", { class: "check" }, inc, "use"))));
    }
    const news = s.news || {};
    const toneIn = el("input", { type: "range", min: "-1", max: "1", step: "0.1", value: String(st.scenario.news ?? 0), "aria-label": "News tone scenario" });
    const toneOut = el("span", { class: "small", text: num(st.scenario.news ?? 0, 1) });
    const toneInc = el("input", { type: "checkbox", checked: !!st.include.news, "aria-label": "Include news" });
    toneIn.addEventListener("input", () => { st.scenario.news = Number(toneIn.value); toneOut.textContent = num(st.scenario.news, 1); remodel(); });
    toneInc.addEventListener("change", () => { st.include.news = toneInc.checked; remodel(); });
    rows.push(el("div", { class: "wi-row", "data-factor": "news" },
      el("div", { class: "wi-label" }, el("strong", { text: "News and press releases" }), el("div", { class: "small muted", text: news.text || "" }),
        news.items && news.items.length ? el("ul", { class: "plain small" }, news.items.slice(0, 4).map(n => el("li", {}, n.url ? el("a", { href: n.url, target: "_blank", rel: "noopener noreferrer", text: n.headline }) : el("span", { text: n.headline }), el("span", { class: "muted", text: ` · ${n.source || ""}${isNum(n.tone) ? ` · tone ${n.tone > 0 ? "positive" : n.tone < 0 ? "negative" : "neutral"}` : ""}` })))) : ""),
      el("div", { class: "wi-reading small" }, el("span", { text: isNum(news.tone) ? `tone ${num(news.tone, 2)}` : "no headlines" }), el("div", { class: "muted", text: `latest ${(news.items || []).length} of 36 hours` })),
      el("div", { class: "wi-beta small" }, isNum(news.record && news.record.frequency) ? el("span", { text: news.record.why === "a move with news behind it" ? `moves with news kept going ${Math.round(100 * news.record.frequency)}%` : `moves without news snapped back ${Math.round(100 * news.record.frequency)}% (not used for the tone)` }) : el("span", { class: "muted", text: "record: not measured; a 0.3 edge is assumed" }), news.record && news.record.cases ? el("div", { class: "muted", text: `${news.record.cases} cases` }) : ""),
      el("div", { class: "wi-input" }, toneIn, toneOut, el("span", { class: "small muted", text: " tone" })),
      el("div", { class: "wi-include" }, el("label", { class: "check" }, toneInc, "use"))));
    const an = s.analysts || { upgrades: 0, downgrades: 0, net: 0, items: [] };
    const anIn = el("input", { type: "number", step: "1", min: "-5", max: "5", value: String(st.scenario.analysts ?? 0), "aria-label": "Analyst calls scenario" });
    const anInc = el("input", { type: "checkbox", checked: !!st.include.analysts, "aria-label": "Include analyst calls" });
    anIn.addEventListener("input", () => { st.scenario.analysts = Number(anIn.value) || 0; remodel(); });
    anInc.addEventListener("change", () => { st.include.analysts = anInc.checked; remodel(); });
    rows.push(el("div", { class: "wi-row", "data-factor": "analysts" },
      el("div", { class: "wi-label" }, el("strong", { text: "Analyst upgrades and downgrades" }), an.items.length ? el("ul", { class: "plain small" }, an.items.slice(0, 4).map(n => el("li", {}, el("span", { class: n.call === "upgrade" ? "pos-text" : "neg-text", text: `${n.call}: ` }), n.url ? el("a", { href: n.url, target: "_blank", rel: "noopener noreferrer", text: n.headline }) : el("span", { text: n.headline })))) : el("div", { class: "small muted", text: "None found in a week of headlines." })),
      el("div", { class: "wi-reading small" }, el("span", { text: `${an.upgrades} up, ${an.downgrades} down` }), el("div", { class: "muted", text: "last week's headlines" })),
      el("div", { class: "wi-beta small" }, el("span", { text: "half an average range each" }), el("div", { class: "muted", text: "over the first bars, at most two" })),
      el("div", { class: "wi-input" }, anIn, el("span", { class: "small muted", text: " net calls" })),
      el("div", { class: "wi-include" }, el("label", { class: "check" }, anInc, "use"))));
    const e = s.earnings, v = s.volatility || {};
    const eInc = el("input", { type: "checkbox", checked: st.include.earnings !== false, "aria-label": "Include earnings" });
    eInc.addEventListener("change", () => { st.include.earnings = eInc.checked; remodel(); });
    rows.push(el("div", { class: "wi-row", "data-factor": "earnings" },
      el("div", { class: "wi-label" }, el("strong", { text: "Earnings ahead" }), el("div", { class: "small muted", text: e ? `${e.date}${e.estimated ? " (estimated)" : ""}, ${e.days} days away.` : "No date known." })),
      el("div", { class: "wi-reading small" }, el("span", { text: e ? `${e.days} days` : "unknown" })),
      el("div", { class: "wi-beta small" }, el("span", { text: isNum(v.event_move_pct) ? `implied move ±${num(v.event_move_pct, 1)}%` : "no implied move" }), el("div", { class: "muted", text: "widens the band inside the horizon" })),
      el("div", { class: "wi-input small muted", text: "—" }),
      el("div", { class: "wi-include" }, el("label", { class: "check" }, eInc, "use"))));
    const pat = s.pattern || {};
    const wIn = el("input", { type: "range", min: "0", max: "1", step: "0.05", value: String(st.scenario.pattern_weight ?? 0), "aria-label": "Pattern weight" });
    const wOut = el("span", { class: "small", text: num(st.scenario.pattern_weight ?? 0, 2) });
    const pInc = el("input", { type: "checkbox", checked: !!st.include.pattern, disabled: !pat.available, "aria-label": "Include similar patterns" });
    wIn.addEventListener("input", () => { st.scenario.pattern_weight = Number(wIn.value); wOut.textContent = num(st.scenario.pattern_weight, 2); remodel(); });
    pInc.addEventListener("change", () => { st.include.pattern = pInc.checked; remodel(); });
    rows.push(el("div", { class: "wi-row", "data-factor": "pattern" },
      el("div", { class: "wi-label" }, el("strong", { text: "Similar patterns" }), el("div", { class: "small muted", text: pat.available ? `${pat.matches} matches; at this horizon ${pat.cases} cases, up ${isNum(pat.up_frequency) ? Math.round(100 * pat.up_frequency) : "?"}% of the time, median ${signedPct(pat.median_pct, 1)}${pat.enough ? "" : " (too few cases to lean on)"}.` : (pat.note || "Press Find similar patterns on the chart to add the pattern's path.") })),
      el("div", { class: "wi-reading small" }, el("span", { text: isNum(pat.median_pct) ? `median ${signedPct(pat.median_pct, 1)}` : "no match" }), el("div", { class: "muted", text: isNum(pat.min_correlation) ? `at ${Math.round(100 * pat.min_correlation)}% likeness` : "" })),
      el("div", { class: "wi-beta small" }, el("span", { text: "the median path, times the weight" })),
      el("div", { class: "wi-input" }, wIn, wOut, el("span", { class: "small muted", text: " weight" })),
      el("div", { class: "wi-include" }, el("label", { class: "check" }, pInc, "use"))));
    rowsBox.replaceChildren(el("div", { class: "wi-head small muted" }, el("span", { text: "Factor" }), el("span", { text: "Today's reading" }), el("span", { text: "This stock's sensitivity" }), el("span", { text: "Scenario" }), el("span", { text: "" })), ...rows);
  }
  function renderModel(m) {
    if (!m) { summary.replaceChildren(); return; }
    const last = m.path_pct.length - 1;
    const terms = (m.terms || []).filter(t => isNum(t.contribution_pct) && Math.abs(t.contribution_pct) >= 0.005).sort((a, b) => Math.abs(b.contribution_pct) - Math.abs(a.contribution_pct));
    summary.replaceChildren(
      el("div", { class: "wi-model" }, el("span", { class: "wi-swatch", style: `background:${WI_MODEL_COLOR}` }),
        el("strong", { text: `Model: ${signedPct(m.expected_pct, 2)} at the horizon` }),
        el("span", { class: "muted", text: ` (${money(m.expected_price)}); 10-90 range ${signedPct(m.low_pct[last], 1)} to ${signedPct(m.high_pct[last], 1)}.` })),
      terms.length ? el("div", { class: "wi-terms small" }, terms.map(t => el("span", { class: `wi-term ${t.contribution_pct >= 0 ? "pos-text" : "neg-text"}`, text: `${t.label} ${signedPct(t.contribution_pct, 2)}` }))) : el("div", { class: "small muted", text: "Every term is zero: set a scenario value or include a factor." }),
      el("p", { class: "small muted", text: m.note || "" }));
  }
  function remodel() {
    clearTimeout(st.timer);
    st.timer = setTimeout(remodelNow, 250);
  }
  async function remodelNow() {
    const a = args();
    if (!a || !st.setup) return;
    const mine = ++st.modelPending;
    try {
      const r = await api("/v2/whatif/model", { ...a, horizon: st.horizon || a.horizon || st.setup.horizon, scenario: st.scenario, include: st.include });
      if (mine !== st.modelPending || st.disposed) return;
      st.model = lineModel(r.model, chartApi.chartArgs());
      renderModel(r.model);
      drawModel();
      if (st.source === "model") renderPosition();
    } catch (e) { if (mine === st.modelPending) err.textContent = e.message; }
  }
  horizonSel.addEventListener("change", () => { st.horizon = Number(horizonSel.value); load(false); });
  peersBtn.addEventListener("click", () => { st.peers = peersIn.value.trim().toUpperCase(); load(false); });
  peersIn.addEventListener("keydown", e => { if (e.key === "Enter") peersBtn.click(); });
  resetBtn.addEventListener("click", () => load(false, true));
  refreshBtn.addEventListener("click", () => load(true));
  providerSel.addEventListener("change", () => { st.provider = providerSel.value; if (st.setup) renderSetup(st.setup); });

  // --- the AI's scenario ---------------------------------------------------------------------------------------
  askBtn.addEventListener("click", async () => {
    const a = args();
    if (!a || !st.setup) return;
    askBtn.disabled = true;
    const question = st.request, horizon = st.horizon || a.horizon || st.setup.horizon;
    aiBox.replaceChildren(el("p", { class: "small muted", text: `Asking ${providerSel.selectedOptions[0] ? providerSel.selectedOptions[0].text : st.provider}… this sends the readings, betas, headlines, pattern record and the model's path; it is billed to your own key and counts against the daily AI limit.` }));
    try {
      const r = await api("/v2/whatif/ask", { ...a, horizon, scenario: st.scenario, include: st.include, provider: st.provider });
      if (st.disposed) return;
      if (question !== st.request) { aiBox.replaceChildren(el("p", { class: "small muted", text: "The chart changed while the AI was answering; ask again for this window." })); return; }
      const sc = r.scenario;
      st.ai = { ...r, symbol, horizon, window: JSON.stringify([a.group, a.window_start, a.window_end]),
        line: lineModel({ ...sc, times: sc.times, path: sc.path, low: sc.low, high: sc.high, last: sc.last }, chartApi.chartArgs()) };
      drawAI();
      renderAI(r);
      sourceSel.querySelector("option[value=ai]").disabled = false;
      chartApi.showAhead(st.setup.horizon);
    } catch (e) { aiBox.replaceChildren(el("p", { class: "error", text: e.message })); }
    finally { askBtn.disabled = false; }
  });
  function renderAI(r) {
    const sc = r.scenario, last = sc.path_pct.length - 1;
    const vars = Object.entries(sc.variables || {});
    const useBtn = el("button", { class: "primary small-btn", text: `Use ${r.label}'s settings` });
    useBtn.addEventListener("click", () => {
      for (const [k, v] of vars) st.scenario[k] = v;
      for (const [k, v] of Object.entries(sc.include || {})) st.include[k] = v;
      renderSetup(st.setup); remodelNow();
      if (sc.strategy && sc.strategy.kind && st.setup.strategies[sc.strategy.kind]) setKind(sc.strategy.kind);
    });
    const clearBtn = el("button", { class: "plain small-btn", text: "Clear the AI line" });
    clearBtn.addEventListener("click", () => { clearAI(); drawAI(); renderPosition(); });
    aiBox.replaceChildren(
      el("div", { class: "wi-model" }, el("span", { class: "wi-swatch", style: `background:${WI_AI_COLOR}` }),
        el("strong", { text: `${r.label} (${r.model}): ${signedPct(sc.expected_pct, 2)} at the horizon` }),
        el("span", { class: "muted", text: ` (${money(sc.expected_price)}); its 10-90 range ${signedPct(sc.low_pct[last], 1)} to ${signedPct(sc.high_pct[last], 1)}; confidence ${Math.round(100 * sc.confidence)}%.` })),
      el("p", { class: "small", text: sc.rationale || "(no rationale given)" }),
      sc.risks && sc.risks.length ? el("ul", { class: "plain small" }, sc.risks.map(x => el("li", { text: `Risk: ${x}` }))) : "",
      el("p", { class: "small" }, el("strong", { text: "Its strategy: " }), el("span", { text: `${(st.setup.strategies[sc.strategy.kind] || {}).label || sc.strategy.kind}${sc.strategy.direction ? ` (${sc.strategy.direction})` : ""}. ${sc.strategy.why || ""}` })),
      el("p", { class: "small" }, el("strong", { text: "Its settings: " }), el("span", { class: "muted", text: vars.length ? vars.map(([k, v]) => `${k} ${isNum(v) ? (v > 0 ? "+" : "") + num(v, 2) : v}`).join(", ") + (sc.pattern ? `; pattern ${sc.pattern.use ? `weight ${num(sc.pattern.weight ?? st.scenario.pattern_weight, 2)}` : "off"}${sc.pattern.why ? ` (${sc.pattern.why})` : ""}` : "") : "none given" })),
      el("div", { class: "row" }, useBtn, clearBtn, el("span", { class: "small muted", text: `${r.usage ? `${r.usage.input_tokens + r.usage.output_tokens} tokens, ` : ""}${r.seconds}s. ${r.note || ""}` })));
  }

  // The AI's scenario belongs to one window and horizon; it goes when they change.
  function clearAI() {
    st.ai = null; aiBox.replaceChildren();
    sourceSel.querySelector("option[value=ai]").disabled = true;
    if (st.source === "ai") { st.source = "model"; sourceSel.value = "model"; }
  }

  // --- the position -------------------------------------------------------------------------------------------
  function syncQtyLabel() { qtyLabel.textContent = st.kind === "shares" || st.kind === "short" ? "shares" : "contracts"; }
  function scenarioNow() {
    if (st.source === "ai" && st.ai) return { ...WHATIFX.scenarioOf(st.ai.scenario), label: `${st.ai.label}'s scenario` };
    return st.model ? { ...WHATIFX.scenarioOf(st.model), label: "the model's scenario" } : null;
  }
  let posPending = 0;
  async function renderPosition() {
    const sc = scenarioNow(), a = chartApi.chartArgs();
    if (!sc || !a || !st.setup) { posBox.replaceChildren(el("p", { class: "small muted", text: "Waiting for a scenario." })); return; }
    const mine = ++posPending;
    try {
      const r = await api("/v2/whatif/position", { symbol, kind: st.kind, quantity: st.quantity, expected_pct: sc.expected_pct, sd_pct: sc.sd_pct,
        horizon_bars: st.setup.horizon, group: a.group });
      if (mine !== posPending || st.disposed) return;
      posBox.replaceChildren(positionView(r, sc.label));
    } catch (e) { if (mine === posPending) posBox.replaceChildren(el("p", { class: "error", text: e.message })); }
  }
  function positionView(r, label) {
    const pl = v => el("span", { class: v >= 0 ? "pos-text" : "neg-text", text: `${v >= 0 ? "+" : "−"}${money(Math.abs(v), 0)}` });
    const kids = [el("div", { class: "wi-pos-head" }, el("strong", { text: `${r.label}, ${r.quantity.toLocaleString()} ${r.kind === "shares" || r.kind === "short" ? "shares" : "contract" + (r.quantity === 1 ? "" : "s")}` }),
      el("span", { class: "muted small", text: ` under ${label}: ${signedPct(r.scenario.expected_pct, 2)} expected from the last price ${money(r.last)}, prices ${money(r.scenario.p10)} – ${money(r.scenario.p90)} (10th–90th) by ${r.eval_date}${r.expiration ? `; options expiring ${r.expiration}` : ""}.` })),
      el("div", { class: "wi-ranges" }, ...WI_NUM_FIELDS.map(([k, lab]) => el("div", { class: "wi-range" }, el("div", { class: "small muted", text: lab }), el("div", { class: "wi-big" }, pl(r[k])))),
        el("div", { class: "wi-range" }, el("div", { class: "small muted", text: "Expected" }), el("div", { class: "wi-big" }, pl(r.expected_pnl))),
        el("div", { class: "wi-range" }, el("div", { class: "small muted", text: "Chance of a profit" }), el("div", { class: "wi-big", text: `${Math.round(100 * r.p_profit)}%` }))),
      el("p", { class: "small" }, el("span", { text: `${r.cost_label.charAt(0).toUpperCase() + r.cost_label.slice(1)}: ${money(r.cost, 0)}. Most it can lose: ${r.unlimited_loss ? "no limit" : money(r.max_loss, 0)}${isNum(r.max_gain) && !r.unlimited_gain ? `; most it can make: ${money(r.max_gain, 0)}` : ""}${r.breakevens && r.breakevens.length ? `; breakeven ${r.breakevens.map(b => money(b)).join(" and ")}` : ""}.` }))];
    if (r.legs && r.legs.length) kids.push(el("ul", { class: "plain small" }, r.legs.map(l => el("li", { text: `${l.side === "buy" ? "Buy" : "Sell"} ${l.contracts} ${l.expiration} ${l.strike} ${l.option_type} at ${money(l.mid)} (${l.role})` }))));
    for (const n of r.notes || []) kids.push(el("p", { class: "small muted", text: n }));
    kids.push(el("p", { class: "small muted", text: r.note }));
    return el("div", {}, ...kids);
  }
  sourceSel.addEventListener("change", () => { st.source = sourceSel.value; renderPosition(); });
  // Shares to contracts and back: 100 shares is one contract's worth.
  function setKind(kind, quantity) {
    const wasShares = st.kind === "shares" || st.kind === "short", isShares = kind === "shares" || kind === "short";
    if (isNum(quantity)) st.quantity = quantity;
    else if (wasShares && !isShares) st.quantity = Math.max(1, Math.round(st.quantity / 100));
    else if (!wasShares && isShares) st.quantity = st.quantity * 100;
    qtyIn.value = String(st.quantity);
    st.kind = kind; kindSel.value = kind; syncQtyLabel(); renderPosition();
  }
  kindSel.addEventListener("change", () => setKind(kindSel.value));
  qtyIn.addEventListener("input", () => { st.quantity = Math.max(1, Math.floor(Number(qtyIn.value) || 1)); clearTimeout(st.qtyTimer); st.qtyTimer = setTimeout(renderPosition, 300); });
  rankBtn.addEventListener("click", async () => {
    const sc = scenarioNow(), a = chartApi.chartArgs();
    if (!sc || !a || !st.setup) return;
    rankBtn.disabled = true;
    rankBox.replaceChildren(el("p", { class: "small muted", text: "Valuing every strategy under the scenario…" }));
    try {
      const q = WHATIFX.quantities(st.kind, st.quantity);
      const r = await api("/v2/whatif/rank", { symbol, quantity: q.quantity, contracts: q.contracts, expected_pct: sc.expected_pct, sd_pct: sc.sd_pct,
        horizon_bars: st.setup.horizon, group: a.group, ai_pick: st.ai && st.ai.scenario.strategy ? st.ai.scenario.strategy.kind : null });
      if (st.disposed) return;
      st.rank = r;
      const rows = r.rows.slice().sort((x, y) => (x.rank || 99) - (y.rank || 99));
      rankBox.replaceChildren(
        el("h4", { text: `Every strategy under ${sc.label}` }),
        el("p", { class: "small muted", text: `${r.best ? `Best by the model: ${(st.setup.strategies[r.best] || {}).label || r.best}.` : ""} ${r.ai_pick ? `${st.ai.label}'s pick: ${(st.setup.strategies[r.ai_pick] || {}).label || r.ai_pick}.` : ""} ${r.note} Click a row to size it.` }),
        table([
          { label: "#", num: true, cell: x => x.rank ? el("span", { text: `${x.rank}${x.kind === r.best ? " ★" : ""}${x.kind === r.ai_pick ? " AI" : ""}` }) : "—" },
          { label: "Strategy", cell: x => el("span", { text: `${x.label} × ${x.quantity}` }) },
          { label: "Cost", num: true, cell: x => money(x.cost, 0) },
          { label: "Downside (10th)", num: true, cell: x => el("span", { class: x.pnl_p10 >= 0 ? "pos-text" : "neg-text", text: money(x.pnl_p10, 0) }) },
          { label: "Middle", num: true, cell: x => el("span", { class: x.pnl_p50 >= 0 ? "pos-text" : "neg-text", text: money(x.pnl_p50, 0) }) },
          { label: "Upside (90th)", num: true, cell: x => el("span", { class: x.pnl_p90 >= 0 ? "pos-text" : "neg-text", text: money(x.pnl_p90, 0) }) },
          { label: "Chance of profit", num: true, cell: x => `${Math.round(100 * x.p_profit)}%` },
          { label: "Most it can lose", num: true, cell: x => x.unlimited_loss ? "no limit" : money(x.max_loss, 0) },
          { label: "Expected per $ at risk", num: true, cell: x => isNum(x.score) ? num(x.score, 2) : "n/a" },
        ], rows, { onRow: x => setKind(x.kind, x.quantity) }),
        r.failed.length ? el("p", { class: "small muted", text: `Could not value: ${r.failed.map(f => `${f.kind} (${f.error})`).join("; ")}` }) : "");
    } catch (e) { rankBox.replaceChildren(el("p", { class: "error", text: e.message })); }
    finally { rankBtn.disabled = false; }
  });

  chartApi.subscribe((what, a) => {
    // A new range or grouping, or a new pattern search: the setup is read again for that window.
    if (st.disposed || !a) return;
    const prev = st.lastArgs;
    const now = args();
    if (what === "load") { st.horizon = null; if (st.ai) { clearAI(); drawAI(); } }
    if (!prev || JSON.stringify({ ...prev, horizon: 0 }) !== JSON.stringify({ ...now, horizon: 0 }) || what === "load") load(false);
  });
  if (chartApi.chartArgs()) load(false);        // the chart was already loaded when the widget was made
  const dispose = () => { st.disposed = true; clearTimeout(st.timer); };
  return { widget, position, dispose };
}
