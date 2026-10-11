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

Object.assign(BUILDERS, { today: buildToday });
