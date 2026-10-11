"use strict";
/* Trade Assistant 2.6: My trades. Your Robinhood history, imported read-only: results by period, the
   strategies you used and the risk each carried, what went better or worse than usual, and every figure
   traceable to the statement rows behind it. Loaded last. */

const ZOOM = { year: "quarter", quarter: "month", month: "week", week: "week" };
const BY_WORDS = { week: "Week", month: "Month", quarter: "Quarter", year: "Year" };
const isoDay = d => `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}-${String(d.getDate()).padStart(2, "0")}`;
const winText = (k, n) => n ? `${k} of ${n}` : "none";
// A share that rounds to 0% or 100% is still measured: say how small or large.
const shareText = p => !isNum(p) ? "n/a" : p > 0 && p < 0.005 ? "under 1%" : p < 1 && p > 0.995 ? "over 99%" : pct(100 * p, 0);
const rateText = s => isNum(s.win_rate) ? el("span", { class: "nowrap", title: `${winText(s.wins, s.trades)} won${isNum(s.win_rate_low) ? `; 95% range ${pct(100 * s.win_rate_low, 0)} to ${pct(100 * s.win_rate_high, 0)}` : ""}` },
  s.enough === false ? el("span", {}, statusTag("warn", "Few"), " ") : null, `${shareText(s.win_rate)} of ${s.trades}`) : el("span", { class: "muted", text: "no closed trades" });

function presetRange(name) {
  const now = new Date(), back = days => { const d = new Date(now); d.setDate(d.getDate() - days); return isoDay(d); };
  return ({ all: [null, null], d30: [back(30), isoDay(now)], d90: [back(90), isoDay(now)], ytd: [`${now.getFullYear()}-01-01`, isoDay(now)],
    m12: [back(365), isoDay(now)], last_year: [`${now.getFullYear() - 1}-01-01`, `${now.getFullYear() - 1}-12-31`] })[name] || null;
}

/* ---------- charts ---------- */
// Realized result per period: one bar each, blue above zero and red below; hover for the period, click to zoom in.
function periodBars(periods, onPick) {
  const wrap = el("div", { class: "chart" });
  if (!periods.length) return wrap;
  const W = 760, H = 230, pad = { l: 70, r: 12, t: 12, b: 34 }, n = periods.length;
  const ys = niceScale(Math.min(0, ...periods.map(p => p.realized)), Math.max(0, ...periods.map(p => p.realized)), 5);
  const Y = v => pad.t + (H - pad.t - pad.b) * (1 - (v - ys.lo) / (ys.hi - ys.lo));
  const band = (W - pad.l - pad.r) / n, bw = Math.max(2, Math.min(40, band - 2));
  const X = i => pad.l + band * i + (band - bw) / 2;
  const root = svg("svg", { viewBox: `0 0 ${W} ${H}`, role: "img", "aria-label": "Realized profit or loss in each period" });
  for (const v of ys.ticks) root.append(svg("line", { class: v === 0 ? "zero" : "grid", x1: pad.l, x2: W - pad.r, y1: Y(v), y2: Y(v) }),
    svg("text", { x: pad.l - 8, y: Y(v) + 4, "text-anchor": "end", text: money(v, 0) }));
  const every = Math.ceil(n / 10);
  const tip = el("div", { class: "tip" });
  periods.forEach((p, i) => {
    const y0 = Y(0), y1 = Y(p.realized), top = Math.min(y0, y1), h = Math.max(1, Math.abs(y1 - y0));
    const bar = svg("rect", { x: X(i), y: top, width: bw, height: h, rx: Math.min(3, bw / 3), class: "pbar", fill: `var(${p.realized >= 0 ? "--div-pos" : "--div-neg"})` });
    const hit = svg("rect", { x: pad.l + band * i, y: pad.t, width: band, height: H - pad.t - pad.b, fill: "transparent", class: "click" });
    const showTip = () => {
      tip.replaceChildren(el("div", { class: "tip-head", text: p.label }), el("div", { text: `Realized ${signedMoney(p.realized, 0)}` }),
        el("div", { class: "tip-sub", text: `${p.trades} trade${p.trades === 1 ? "" : "s"} closed${isNum(p.win_rate) ? `, ${shareText(p.win_rate)} won` : ""}. Click to look closer.` }));
      tooltipAt(tip, root.getBoundingClientRect(), X(i) + bw / 2, W);
    };
    hit.addEventListener("pointerenter", showTip);
    hit.addEventListener("pointerleave", () => { tip.style.display = "none"; });
    hit.addEventListener("click", () => onPick(p));
    root.append(bar, hit);
    if (i % every === 0) root.append(svg("text", { x: X(i) + bw / 2, y: H - 12, "text-anchor": "middle", text: p.label.replace(/^Week of /, "").replace(/, \d{4}$/, "") }));
  });
  wrap.append(el("div", { class: "chart-scroll" }, root, tip));
  return wrap;
}

function riskBars(strategies) {
  const rows = strategies.filter(s => isNum(s.risk.dollar_days) && s.risk.dollar_days > 0);
  if (!rows.length) return null;
  const max = Math.max(...rows.map(s => s.risk.dollar_days));
  return el("div", { class: "hbars", role: "list", "aria-label": "Dollar-days at risk by strategy" }, rows.map(s =>
    el("div", { class: "hbar", role: "listitem", title: `${s.strategy}: ${money(s.risk.dollar_days, 0)} dollar-days (risk at the open times days held), from ${s.risk.measured} of ${s.risk.of} trades` },
      el("span", { class: "hbar-name", text: s.strategy }),
      el("span", { class: "hbar-track" }, el("b", { class: "up", style: `left:0; width: ${Math.max(0.4, 100 * s.risk.dollar_days / max).toFixed(2)}%` })),
      el("span", { class: "hbar-value", text: compactMoney(s.risk.dollar_days) }))));
}

const compactMoney = v => !isNum(v) ? "n/a" : Math.abs(v) >= 1e6 ? `$${(v / 1e6).toFixed(1)}M` : Math.abs(v) >= 1e4 ? `$${Math.round(v / 1e3)}K` : money(v, 0);

/* ---------- the screen ---------- */
function buildTrades() {
  const filters = { start: null, end: null, by: "month", basis: "close" };
  let current = null, listFilter = null, shown = 50, alsoOpen = false;

  // --- import ---
  const file = el("input", { type: "file", accept: ".csv,text/csv", "aria-label": "Account activity report (CSV)" });
  const importBtn = el("button", { class: "primary", text: "Import the report" });
  const sampleBtn = el("button", { class: "plain", text: "Load a made-up sample" });
  const importErr = el("div", { class: "error", role: "alert" });
  const importOut = el("div");
  const importsBox = el("div");
  const linkBox = el("div");
  // The Robinhood connection: reads past orders only when switched on in .env (ROBINHOOD_ACCOUNT_READ=1).
  function renderLink(link) {
    if (!link) return linkBox.replaceChildren();
    if (!link.enabled || !link.using_robinhood) {
      linkBox.replaceChildren(el("p", { class: "small muted", text: link.enabled
        ? "Reading your account through the Robinhood connection is switched on, but the app is not using that connection now (DATA_MODE=robinhood)."
        : "The app can also read your past orders and holdings through the Robinhood connection: add ROBINHOOD_ACCOUNT_READ=1 to .env (with DATA_MODE=robinhood) and restart. It is off now. The report above has your option trades too." }));
      return;
    }
    const off = link.offered || {};
    const pull = el("button", { class: "plain", text: "Read my orders from the Robinhood connection" });
    const words = Object.entries(off).map(([job, name]) => `${job} ${name ? "offered" : "not offered"}`).join(", ");
    pull.addEventListener("click", () => withBusy(pull, importErr, async () => { importOut.replaceChildren(importSummary(await api("/v2/history/pull", {}))); await load(); }));
    linkBox.replaceChildren(el("div", { class: "row", style: "margin-top:8px" }, pull,
      el("span", { class: "small muted", text: link.error ? `Connection: ${link.error}` : `Robinhood's connection: ${words}. Reads only. Days your report already covers use the report.` })));
  }
  importBtn.addEventListener("click", () => withBusy(importBtn, importErr, async () => {
    const f = file.files && file.files[0];
    if (!f) throw new Error("Choose the CSV file you downloaded from Robinhood first.");
    if (f.size > 16e6) throw new Error("That file is larger than 16 MB. Generate the report for a shorter period and import the parts one after another.");
    const r = await api("/v2/history/import", { filename: f.name, text: await f.text() });
    importOut.replaceChildren(importSummary(r));
    file.value = "";
    await load();
  }));
  sampleBtn.addEventListener("click", () => withBusy(sampleBtn, importErr, async () => {
    importOut.replaceChildren(importSummary(await api("/v2/history/sample", {})));
    await load();
  }));
  const how = el("details", {}, el("summary", { class: "small", text: "How to get the report from Robinhood" }),
    el("ol", { class: "small" }, el("li", { text: "In Robinhood, open Reports and statements and choose Account activity report." }),
      el("li", { text: "Select Generate new report, pick the account and the dates (from when you started trading is best), and generate it." }),
      el("li", { text: "Robinhood says it usually takes about 2 hours, and up to 24. Download the CSV when it is ready and import it here." }),
      el("li", { text: "Importing a later report adds only the rows that are new; the same row is never counted twice." })),
    el("p", { class: "small muted", text: "Read-only. The file stays on this computer, in the app's own database, and nothing about your account is sent to the AI. Only ticker symbols leave it, to look up prices through your market data connection (the stock's close on the day you opened, and the price now of what is still open). Nothing here can place, change or cancel an order." }));
  const importPanel = el("div", { class: "panel" }, el("h3", { text: "Your history" }),
    el("div", { class: "row" }, field("Account activity report (CSV)", file), importBtn, state.status && state.status.data && state.status.data.mode === "demo" ? sampleBtn : null),
    importErr, linkBox, importOut, how, importsBox);

  // --- the range: one row above the results ---
  const preset = el("select", { "aria-label": "Dates" }, [["all", "All history"], ["d30", "Last 30 days"], ["d90", "Last 90 days"], ["ytd", "This year"],
    ["m12", "Last 12 months"], ["last_year", "Last year"], ["custom", "Pick dates"]].map(([v, t]) => el("option", { value: v, text: t })));
  const from = el("input", { type: "date", "aria-label": "From" }), to = el("input", { type: "date", "aria-label": "To" });
  const by = el("select", { "aria-label": "Group by" }, Object.entries(BY_WORDS).map(([v, t]) => el("option", { value: v, text: t, selected: v === "month" })));
  const basis = el("select", { "aria-label": "Dates mean" }, el("option", { value: "close", text: "When trades closed" }), el("option", { value: "open", text: "When trades opened" }));
  const apply = el("button", { class: "primary", text: "Show" });
  const rangeErr = el("div", { class: "error", role: "alert" });
  const rangeBar = el("div", { class: "panel date-range hidden" }, el("div", { class: "inline-form" },
    field("Dates", preset), field("From", from), field("To", to), field("Group by", by), field("Dates mean", basis), apply), rangeErr);
  preset.addEventListener("change", () => {
    const r = presetRange(preset.value);
    if (r) { from.value = r[0] || ""; to.value = r[1] || ""; apply.click(); }
  });
  for (const input of [from, to]) input.addEventListener("change", () => { preset.value = "custom"; });
  apply.addEventListener("click", () => {
    // "All history" stays all of it: no dates are sent, so a later import that reaches further back is included.
    const all = preset.value === "all";
    Object.assign(filters, { start: all ? null : from.value || null, end: all ? null : to.value || null, by: by.value, basis: basis.value });
    listFilter = null;
    load();
  });

  const out = el("div");
  let ticket = 0;
  async function load() {
    const mine = ++ticket;
    out.classList.add("refetching");
    try {
      const q = new URLSearchParams({ by: filters.by, basis: filters.basis });
      if (filters.start) q.set("start", filters.start);
      if (filters.end) q.set("end", filters.end);
      const [a, h, link] = await Promise.all([api(`/v2/history/analysis?${q}`), api("/v2/history/holdings").catch(() => null),
        api("/v2/history/link").catch(() => null)]);
      if (mine !== ticket) return;                     // a newer request is on its way: leave the screen to it
      current = a;
      importsBox.replaceChildren(importsList(a.imports || []));
      renderLink(a.link || link);
      rangeBar.classList.toggle("hidden", !!a.empty);
      if (a.empty) { out.replaceChildren(el("div", { class: "panel" }, el("p", { class: "muted", text: "Nothing imported yet. Download your account activity report from Robinhood and import it above." }))); return; }
      out.replaceChildren(render(a, h));
      rangeErr.textContent = "";
    } catch (e) { if (mine === ticket) rangeErr.textContent = e.message; }
    finally { if (mine === ticket) out.classList.remove("refetching"); }
  }

  function importsList(list) {
    if (!list.length) return "";
    return el("details", {}, el("summary", { class: "small", text: `${list.length} import${list.length === 1 ? "" : "s"}` }), table([
      { label: "File", cell: i => el("span", {}, i.filename || i.source, i.source === "sample" ? el("span", {}, " ", statusTag("warn", "Made up")) : null) },
      { label: "Covers", cell: i => i.first_date ? `${shortDate(i.first_date)} to ${shortDate(i.last_date)}` : "no trades" },
      { label: "Rows kept", num: true, cell: i => num(i.rows_kept, 0) },
      { label: "Already had", num: true, cell: i => num(i.rows_duplicate, 0) + (i.rows_updated ? `, ${num(i.rows_updated, 0)} updated` : "") },
      { label: "Not trades", num: true, cell: i => num(i.rows_ignored, 0) },
      { label: "Unreadable", num: true, cell: i => i.rows_unreadable ? el("strong", { text: num(i.rows_unreadable, 0) }) : "0" },
      { label: "Imported", cell: i => shortDate(i.imported_at.slice(0, 10)) },
      { label: "", cell: i => { const b = el("button", { class: "plain small-btn", text: "Remove" }); b.addEventListener("click", () => withBusy(b, importErr, async () => { await api(`/v2/history/imports/${i.id}/remove`, {}); await load(); })); return b; } }],
    list));
  }

  // Show the trades behind a figure, in the trades panel, and bring it into view.
  const tradesPanel = el("div", { class: "panel" });
  function showTrades(f) {
    listFilter = f; shown = 50;
    tradesPanel.replaceChildren(...tradesView(current));
    tradesPanel.scrollIntoView({ behavior: "smooth", block: "start" });
  }
  const showBtn = (label, f) => { const b = el("button", { class: "plain small-btn", text: label }); b.addEventListener("click", e => { e.stopPropagation(); showTrades(f); }); return b; };

  function zoom(p) {
    preset.value = "custom"; from.value = p.start; to.value = p.end;
    by.value = ZOOM[filters.by]; apply.click();
  }

  function render(a, h) {
    const frag = el("div"), s = a.summary, rk = s.risk, r = a.range;
    if (a.sample) frag.append(banner("warn", "This includes the made-up sample report. Remove it under Your history before importing your own."));
    for (const n of a.notes) frag.append(banner("info", n));
    frag.append(el("p", { class: "small" }, el("strong", { text: `${shortDate(r.start)} to ${shortDate(r.end)}` }),
      `, trades counted when they ${r.basis === "close" ? "closed" : "opened"}. Your history runs from ${shortDate(r.first_date)} to ${shortDate(r.last_date)}.`));
    frag.append(el("div", { class: "panel" }, el("h3", { text: "In this range" }), el("div", { class: "metrics" },
      metric(signedMoney(s.realized, 0), "Realized result (cash in less cash out, fees included)"),
      metric(rateText(s), "Trades won"),
      metric(isNum(s.profit_factor) ? num(s.profit_factor, 2) : s.losses === 0 && s.wins ? "no losses" : "n/a", "Profit factor (won ÷ lost)"),
      metric(isNum(s.average_win) ? `${signedMoney(s.average_win, 0)} / ${signedMoney(s.average_loss, 0)}` : "n/a", "Average win / average loss"),
      metric(isNum(s.largest_loss) ? signedMoney(s.largest_loss, 0) : "none", "Largest loss"),
      metric(isNum(s.max_drawdown) ? money(-s.max_drawdown, 0) : "n/a", s.drawdown_from ? `Deepest fall from a high, ${shortDate(s.drawdown_from)} to ${shortDate(s.drawdown_to)}` : "Deepest fall from a high"),
      peakMetric(s.peak_exposure, a.exposure),
      metric(String(s.open_now), s.unresolved ? `Trades still open; ${s.unresolved} more ended with no record of how` : "Trades still open"))));

    // By period
    const per = el("div", { class: "panel" }, el("h3", { text: `By ${BY_WORDS[r.by].toLowerCase()}` }),
      el("p", { class: "small muted", text: "Realized result in each period: closed pieces counted on the day they closed. Click a bar to look closer at that period." }),
      periodBars(a.periods, zoom),
      table([
        { label: BY_WORDS[r.by], cell: p => p.label },
        { label: "Realized", num: true, cell: p => signedMoney(p.realized, 0) },
        { label: "Closed", num: true, cell: p => String(p.trades) },
        { label: "Won", cell: p => p.trades ? `${shareText(p.win_rate)} of ${p.trades}` : el("span", { class: "muted", text: "none closed" }) },
        { label: "Opened", num: true, cell: p => String(p.opened) },
        { label: "Risk opened", num: true, cell: p => el("span", { title: `Most these could lose as opened, from ${p.risk_opened_measured} of ${p.opened}${p.unlimited_opened ? `; ${p.unlimited_opened} unlimited` : ""}` },
          isNum(p.risk_opened) ? compactMoney(p.risk_opened) : "n/a", p.unlimited_opened ? el("span", { class: "small error", text: ` +${p.unlimited_opened} unlimited` }) : null) },
        { label: "Strategies closed", cell: p => Object.entries(p.by_strategy).sort((x, y) => y[1].trades - x[1].trades).slice(0, 3).map(([k, v]) => `${k} ${v.trades}`).join(", ") || "" },
        { label: "", cell: p => el("span", { class: "btn-pair" }, p.trades ? showBtn("Trades", { kind: "period", label: p.label, ids: p.trade_ids }) : "",
          r.by !== "week" ? (() => { const b = el("button", { class: "plain small-btn", text: "Look closer" }); b.addEventListener("click", () => zoom(p)); return b; })() : "") }],
        [...a.periods].reverse()));
    frag.append(per);
    if (a.curve.length > 1) frag.append(el("div", { class: "panel" }, el("h3", { text: "Running total" }),
      lineChart({ series: [{ name: "Realized", color: "--series-1", points: a.curve.map(c => ({ x: c.date, y: c.cumulative })) }], yFormat: v => money(v, 0), height: 220 }),
      el("p", { class: "small muted", text: `Realized results added up day by day in this range${isNum(s.max_drawdown) && s.max_drawdown > 0 ? `; the deepest fall from a high was ${money(s.max_drawdown, 0)}, from ${shortDate(s.drawdown_from)} to ${shortDate(s.drawdown_to)}` : ""}.` })));

    // Strategies and risk
    const top = a.strategies[0];
    const most = top ? (top.risk.unlimited ? `${top.strategy}: ${top.risk.unlimited} trade${top.risk.unlimited === 1 ? "" : "s"} with no limit to the loss.`
      : `${top.strategy}: ${compactMoney(top.risk.dollar_days)} dollar-days at risk (the most it could lose, times days held).`) : "";
    frag.append(el("div", { class: "panel" }, el("h3", { text: "Strategies and the risk each carried" }),
      top ? el("p", {}, el("strong", { text: "Most exposure: " }), most) : null,
      el("p", { class: "small muted", text: "Most exposure first: strategies with unlimited risk, then by dollar-days at risk. Risk is the most a trade could lose as opened, worked out from its legs; unlimited and unmeasured trades are counted, never added in as zero." }),
      riskBars(a.strategies),
      table([
        { label: "Strategy", cell: x => el("strong", { text: x.strategy }) },
        { label: "Trades", num: true, cell: x => `${x.trades}${x.open ? ` +${x.open} open` : ""}` },
        { label: "Won", cell: x => rateText(x) },
        { label: "Net", num: true, cell: x => isNum(x.net) ? signedMoney(x.net, 0) : "n/a" },
        { label: "Worst loss", num: true, cell: x => isNum(x.risk.worst_loss) ? el("span", { title: isNum(x.risk.worst_loss_share_of_risk) ? `${pct(100 * x.risk.worst_loss_share_of_risk, 0)} of what it could have lost` : "" }, signedMoney(x.risk.worst_loss, 0)) : "none" },
        { label: "Risk at the open", num: true, cell: x => el("span", {}, isNum(x.risk.total_at_open) ? compactMoney(x.risk.total_at_open) : "n/a",
          x.risk.unlimited ? el("div", { class: "small error", text: `${x.risk.unlimited} unlimited` }) : null,
          x.risk.not_measured ? el("div", { class: "small muted", text: `${x.risk.not_measured} not measured` }) : null) },
        { label: "Largest single", num: true, cell: x => isNum(x.risk.largest) ? compactMoney(x.risk.largest) : "n/a" },
        { label: "Dollar-days", num: true, cell: x => isNum(x.risk.dollar_days) ? compactMoney(x.risk.dollar_days) : "n/a" },
        { label: "Return on risk", num: true, cell: x => isNum(x.return_on_risk) ? el("span", { title: "Net result over the risk of the closed trades" }, (x.return_on_risk > 0 ? "+" : "") + pct(100 * x.return_on_risk, 1)) : "n/a" },
        { label: "", cell: x => showBtn("Trades", { kind: "strategy", label: x.strategy, ids: x.trade_ids }) }], a.strategies)));
    if (a.exposure.length > 1) frag.append(el("div", { class: "panel" }, el("h3", { text: "Open risk over time" }),
      lineChart({ series: [{ name: "At risk", color: "--series-1", points: a.exposure.map(e => ({ x: e.date, y: e.risk })) }], yFormat: v => compactMoney(v), height: 200 }),
      el("p", { class: "small muted", text: `The most all open trades could have lost, each weekday.${a.exposure.some(e => e.unlimited) ? ` On ${a.exposure.filter(e => e.unlimited).length} days you also held trades with no limit to the loss, which no line can show.` : ""}` })));

    // Patterns
    const pat = a.patterns, avg = pat.overall.average;
    const patLine = x => el("li", {}, el("strong", { text: `${x.title}: ${x.value}. ` }),
      `${winText(x.wins, x.trades)} won, ${signedMoney(x.net, 0)} in all, ${signedMoney(x.per_trade_vs_average, 0)} a trade against your average. `,
      x.enough ? null : statusTag("warn", "Few"), " ", showBtn(`The ${x.trades} trades`, { kind: "pattern", label: `${x.title}: ${x.value}`, ids: x.trade_ids }));
    frag.append(el("div", { class: "panel" }, el("h3", { text: "What went better and worse than usual" }),
      el("p", { class: "small muted", text: `Each kind of trade against your average closed trade (${isNum(avg) ? signedMoney(avg, 0) : "n/a"}) in this range, ranked by how much better or worse it did in total. Only kinds with ${pat.min_trades} or more trades are ranked. These are your own past trades, not a forecast; a kind of trade can do well or badly by chance, more so with few trades.` }),
      el("div", { class: "pattern-cols" },
        el("div", {}, el("h4", { text: "Better than usual" }), pat.success.length ? el("ul", { class: "plain pattern-list" }, pat.success.map(patLine)) : el("p", { class: "small muted", text: "Nothing stands out above your average." })),
        el("div", {}, el("h4", { text: "Worse than usual" }), pat.failure.length ? el("ul", { class: "plain pattern-list" }, pat.failure.map(patLine)) : el("p", { class: "small muted", text: "Nothing stands out below your average." }))),
      el("details", {}, el("summary", { class: "small", text: "Every pattern" }), table([
        { label: "What", cell: x => x.title }, { label: "Kind", cell: x => x.value },
        { label: "Trades", num: true, cell: x => String(x.trades) }, { label: "Won", cell: x => rateText(x) },
        { label: "Net", num: true, cell: x => signedMoney(x.net, 0) }, { label: "Against average", num: true, cell: x => isNum(x.edge) ? signedMoney(x.edge, 0) : "n/a" },
        { label: "", cell: x => showBtn("Trades", { kind: "pattern", label: `${x.title}: ${x.value}`, ids: x.trade_ids }) }], pat.table))));

    tradesPanel.replaceChildren(...tradesView(a));
    frag.append(tradesPanel);
    if (a.unmatched.length || a.unmatched_total) frag.append(el("details", { class: "panel" }, el("summary", { text: `Not counted: ${a.unmatched_total} closing row${a.unmatched_total === 1 ? "" : "s"} with nothing open to close` }),
      el("p", { class: "small muted", text: "These were opened before your imported history starts, so what they cost is unknown. They are left out of every figure, not counted as zero. Import an earlier report to include them." }),
      table([{ label: "Date", cell: u => shortDate(u.date) }, { label: "Row", cell: u => u.code }, { label: "What", cell: u => u.contract },
        { label: "Quantity", num: true, cell: u => qtyText(u.quantity) }, { label: "Amount", num: true, cell: u => isNum(u.amount) ? signedMoney(u.amount, 2) : "" }], a.unmatched)));
    if (h && (h.holdings.length || (h.connection && (h.connection.error || (h.connection.compare || []).length)))) frag.append(holdingsPanel(h));
    return frag;
  }

  function tradesView(a) {
    const ids = listFilter ? new Set(listFilter.ids) : null;
    // By default the trades the results above count; the others were only open during the range (their risk counts).
    const counted = a.trades.filter(t => t.in_results), others = a.trades.filter(t => !t.in_results);
    const all = ids ? a.trades.filter(t => ids.has(t.id)) : alsoOpen ? a.trades : counted;
    const list = all.slice().sort((x, y) => (y.close_date || y.open_date).localeCompare(x.close_date || x.open_date));
    const redraw = () => tradesPanel.replaceChildren(...tradesView(current));
    const btn = (text, fn) => { const b = el("button", { class: "plain small-btn", text }); b.addEventListener("click", () => { fn(); shown = 50; redraw(); }); return b; };
    const head = el("div", { class: "row" }, el("h3", { text: listFilter ? `Trades: ${listFilter.label}` : alsoOpen ? "Trades in this range, and others open during it" : "Trades in this range" }),
      listFilter ? btn("Show every trade", () => { listFilter = null; })
        : others.length ? btn(alsoOpen ? "Only the trades counted above" : `Also the ${others.length} open during the range`, () => { alsoOpen = !alsoOpen; }) : null);
    const tbody = el("tbody");
    for (const t of list.slice(0, shown)) {
      const tr = el("tr", { class: "click", tabindex: "0" },
        el("td", { text: shortDate(t.open_date) }), el("td", { text: t.close_date ? shortDate(t.close_date) : "open" }),
        el("td", {}, el("strong", { text: t.symbol })), el("td", { text: t.strategy }),
        el("td", { class: "num" }, isNum(t.realized_pnl) ? signedMoney(t.realized_pnl, 2) : t.status === "UNRESOLVED"
          ? el("span", { title: "Past expiration with no row saying how it ended: the result is unknown, not zero." }, statusTag("warn", "Unknown"))
          : el("span", { class: "muted", text: "open" })),
        el("td", { class: "num" }, isNum(t.risk) ? money(t.risk, 0) : el("span", { class: t.unlimited ? "small error" : "muted", text: t.unlimited ? "unlimited" : "not measured" })),
        el("td", { class: "num", text: String(t.days_held) }), el("td", { text: t.ended || "" }));
      const detail = el("tr", { class: "hidden detail-row" }, el("td", { colspan: 8 }));
      const toggle = async () => {
        if (!detail.classList.contains("hidden")) { detail.classList.add("hidden"); return; }
        detail.classList.remove("hidden");
        if (!detail.dataset.loaded) {
          detail.firstChild.replaceChildren(el("span", { class: "small muted", text: "Loading the statement rows…" }));
          try { detail.firstChild.replaceChildren(tradeDetail(await api(`/v2/history/trade/${t.id}`))); detail.dataset.loaded = "1"; }
          catch (e) { detail.firstChild.replaceChildren(el("span", { class: "small error", text: e.message })); }
        }
      };
      tr.addEventListener("click", toggle);
      tr.addEventListener("keydown", e => { if (e.key === "Enter") toggle(); });
      tbody.append(tr, detail);
    }
    const more = list.length > shown ? (() => { const b = el("button", { class: "plain", text: `Show ${Math.min(50, list.length - shown)} more of ${list.length - shown}` }); b.addEventListener("click", () => { shown += 50; tradesPanel.replaceChildren(...tradesView(current)); }); return b; })() : null;
    return [head, el("p", { class: "small muted", text: `${list.length} trade${list.length === 1 ? "" : "s"}${!ids && !alsoOpen && others.length ? ` (and ${others.length} more open during the range, whose risk is counted above)` : ""}. Select one to see its legs and the statement rows it was built from, exactly as in your report, to check against Robinhood.` }),
      el("div", { class: "scroll" }, el("table", { class: "trade-table" }, el("thead", {}, el("tr", {}, ["Opened", "Closed", "Stock", "Strategy", "Result", "Risk at open", "Days", "Ended"].map((h, i) => el("th", { class: i === 4 || i === 5 || i === 6 ? "num" : null, text: h })))), tbody)), more];
  }

  function holdingsPanel(h) {
    const c = h.connection;
    const compare = c && c.compare ? el("div", {}, el("h4", { text: "Against what Robinhood's connection says you hold now" }),
      table([{ label: "Holding", cell: x => x.what }, { label: "Side", cell: x => x.side },
        { label: "By your history", num: true, cell: x => isNum(x.history) ? qtyText(x.history) : el("span", { class: "muted", text: "none" }) },
        { label: "At Robinhood", num: true, cell: x => isNum(x.robinhood) ? qtyText(x.robinhood) : el("span", { class: "muted", text: "none" }) },
        { label: "", cell: x => x.agrees ? statusTag("pass", "Agrees") : statusTag("warn", "Differs") }], c.compare),
      el("p", { class: "small muted", text: "A difference usually means the imported history does not reach back to when the holding was opened, or a trade since the last import." }))
      : c && c.error ? el("p", { class: "small error", text: `Robinhood's connection: ${c.error}` }) : null;
    return el("div", { class: "panel" }, el("h3", { text: "Still open, by your history" }), compare,
      el("p", { class: "small muted", text: h.note }),
      table([
        { label: "Holding", cell: x => el("strong", { text: x.contract }) },
        { label: "Side", cell: x => x.side },
        { label: "Size", num: true, cell: x => qtyText(x.quantity) },
        { label: "Since", cell: x => shortDate(x.since) },
        { label: "Cash in it", num: true, cell: x => money(x.cash, 2) },
        { label: "Now", num: true, cell: x => isNum(x.value) ? signedMoney(x.unrealized, 0) : el("span", { class: "muted", text: x.kind === "OPTION" ? "not valued here" : "unknown" }) },
        { label: "", cell: x => x.kind === "OPTION" && BUILDERS.price ? (() => {
          const b = el("button", { class: "plain small-btn", text: "Price it to close" });
          b.addEventListener("click", () => state.openPricing(FLOW.pricingRequest(x.symbol, x, Math.round(x.quantity), x.side === "long" ? "sell" : "buy")));
          return b; })() : "" }], h.holdings));
  }

  const view = el("section", {}, el("h2", { text: "My trades" }),
    el("p", { class: "lede", text: "Your Robinhood trades, rebuilt from your account activity report: how each period went, the strategies you used and the risk each carried, and what went better or worse than usual. Every figure opens the trades and statement rows behind it." }),
    importPanel, rangeBar, out);
  let ran = false;
  view.onShow = () => { if (!ran) { ran = true; load(); } };
  return view;
}

// Most at risk at once: a day with only unlimited or unmeasured risk open is never shown as $0.
function peakMetric(p, exposure) {
  const label = "Most at risk at once";
  if (!p) return metric(exposure && exposure.length ? "nothing open" : "n/a", label);
  const extra = [p.unlimited ? `${p.unlimited} unlimited` : "", p.not_measured ? `${p.not_measured} not measured` : ""].filter(Boolean).join(", ");
  const when = `${label}, ${shortDate(p.date)} (${p.open} trade${p.open === 1 ? "" : "s"} open${extra ? `; ${extra}` : ""})`;
  if (p.risk > 0) return metric(p.unlimited ? `${compactMoney(p.risk)} + unlimited` : compactMoney(p.risk), when);
  return metric(p.unlimited ? "unlimited" : "not measured", when);
}

function importSummary(r) {
  const parts = [banner(r.rows_unreadable ? "warn" : "pass",
    `${r.filename || "Import"}: ${r.rows_added} trade row${r.rows_added === 1 ? "" : "s"} added${r.rows_updated ? `, ${r.rows_updated} updated (filled further since the last read)` : ""}${r.rows_duplicate ? `, ${r.rows_duplicate} already imported` : ""}${r.first_date ? `, covering ${shortDate(r.first_date)} to ${shortDate(r.last_date)}` : ""}.`)];
  if (r.ignored && r.ignored.length) parts.push(el("p", { class: "small muted", text: `Not trades, counted and not kept: ${r.ignored.map(i => `${i.rows} ${i.what} (${i.code})`).join(", ")}.` }));
  if (r.unmodelled_total) parts.push(banner("warn", `${r.unmodelled_total} row${r.unmodelled_total === 1 ? " is a corporate action" : "s are corporate actions"} (mergers, spin-offs, symbol changes) this app does not model; trades in those stocks may not add up. ${r.unmodelled.slice(0, 5).map(u => `${u.date} ${u.code} ${u.symbol || ""}`).join("; ")}`));
  if (r.rows_unreadable) parts.push(el("details", {}, el("summary", { class: "small", text: `${r.rows_unreadable} row${r.rows_unreadable === 1 ? "" : "s"} could not be read and were left out` }),
    el("ul", { class: "plain small" }, (r.unreadable || []).map(u => isNum(u.line)
      ? el("li", {}, el("strong", { text: `Line ${u.line}: ` }), `${u.reason}. `, el("span", { class: "muted", text: u.text || "" }))
      // from the Robinhood connection: what it was, why, and the field names it had (never their values)
      : el("li", {}, el("strong", { text: `${u.what || "A row"}: ` }), `${u.reason}. `, u.fields ? el("span", { class: "muted", text: `Fields: ${u.fields.join(", ")}` }) : null)))));
  return el("div", {}, ...parts);
}

function tradeDetail(t) {
  return el("div", { class: "trade-detail" },
    el("p", { class: "small" }, el("strong", { text: `${t.strategy} in ${t.symbol}. ` }),
      isNum(t.risk) ? `Most it could lose as opened: ${money(t.risk, 2)} (${t.risk_basis}). ` : `Risk at the open: ${t.risk_basis}. `,
      isNum(t.dte_at_open) ? `${t.dte_at_open} days to expiration when opened. ` : "",
      isNum(t.moneyness_at_open) ? `Strike ${Math.abs(100 * t.moneyness_at_open).toFixed(1)}% ${t.moneyness_at_open > 0 ? "out of" : "in"} the money at that day's close. ` : "",
      t.estimated ? "Some cash was worked out from price times size, because the row had no amount." : ""),
    table([{ label: "Opened", cell: l => l.contract }, { label: "", cell: l => l.side }, { label: "Size", num: true, cell: l => qtyText(l.qty) },
      { label: "Cash", num: true, cell: l => signedMoney(l.cash, 2) }], t.legs),
    t.closes.length ? table([{ label: "Closed", cell: c => c.contract }, { label: "How", cell: c => c.how }, { label: "On", cell: c => shortDate(c.date) },
      { label: "Size", num: true, cell: c => qtyText(c.qty) }, { label: "Result", num: true, cell: c => signedMoney(c.pnl, 2) }], t.closes) : null,
    el("h4", { text: "Statement rows, as in your report" }),
    table([{ label: "Activity Date", cell: r => r.raw["Activity Date"] }, { label: "Instrument", cell: r => r.raw["Instrument"] },
      { label: "Description", cell: r => r.raw["Description"] }, { label: "Trans Code", cell: r => r.raw["Trans Code"] },
      { label: "Quantity", num: true, cell: r => r.raw["Quantity"] }, { label: "Price", num: true, cell: r => r.raw["Price"] },
      { label: "Amount", num: true, cell: r => r.raw["Amount"] }], t.rows));
}

Object.assign(BUILDERS, { trades: buildTrades });
