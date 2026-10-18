"use strict";
/* Trade Assistant 2.10: Live news. Stock-moving news as it arrives (press releases, 8-K filings, headlines),
   the price's answer in the minutes after, and a trade card when the move confirms: buy shares or calls when
   the news sends a stock up, short or buy puts when it sends it down, and an alert on anything you hold.
   Setups, not forecasts, and nothing here places an order. */

const LIVE_POLL_MS = 4000;
const LIVE_STATE_WORDS = { waiting: "Waiting for a price", watching: "Watching", opportunity: "Opportunity",
  no_reaction: "No reaction", stopped: "Stopped out", done: "Checked", skipped: "Not traded" };
const LIVE_SOURCE_WORDS = { streaming: "streaming", polling: "checking", on_demand: "on demand", connecting: "connecting",
  reconnecting: "reconnecting", failed: "failed", off: "off" };
MORNING_SETUP_WORDS.news = "Ride the news (live)";

function liveAgo(iso) {
  if (!iso) return "unknown";
  const s = Math.max(0, (Date.now() - Date.parse(iso)) / 1000);
  if (s < 60) return `${Math.round(s)} s ago`;
  if (s < 3600) return `${Math.round(s / 60)} min ago`;
  if (s < 86400) return `${(s / 3600).toFixed(1)} h ago`;
  return shortDate(iso.slice(0, 10));
}

function liveDelay(sec) {
  if (!isNum(sec)) return "";
  if (sec < 1) return `${Math.round(sec * 1000)} ms after publication`;
  if (sec < 120) return `${sec.toFixed(1)} s after publication`;
  return `${Math.round(sec / 60)} min after publication`;
}

function toneTag(item) {
  const impact = (item.extra || {}).impact;
  if (impact && impact.direction) return el("span", { class: `m-tag ${impact.direction === "bullish" ? "m-ride" : "m-news"}`,
    title: `RTPR impact score ${impact.score ?? "n/a"}${isNum(impact.hit_rate) ? `, past hit rate ${pct(100 * impact.hit_rate, 0)}` : ""}`,
    text: `${impact.direction} ${impact.score ?? ""}`.trim() });
  if (item.kind === "filing") return el("span", { class: "m-tag m-none", text: "filing" });
  if (!isNum(item.tone) || item.tone === 0) return el("span", { class: "m-tag m-none", text: "neutral words" });
  return el("span", { class: `m-tag ${item.tone > 0 ? "m-ride" : "m-news"}`, title: "Tone by word list: it counts words, it does not read meaning.",
    text: item.tone > 0 ? "positive words" : "negative words" });
}

function headlineLink(item) {
  const text = item.headline || "(no headline)";
  return item.url && /^https?:\/\//i.test(item.url) ? el("a", { href: item.url, target: "_blank", rel: "noopener noreferrer", text }) : el("span", { text });
}

function signalMove(sig) {
  if (!isNum(sig.ref_price) || !isNum(sig.last_price)) return el("span", { class: "muted small", text: sig.reason || "" });
  const since = 100 * (sig.last_price / sig.ref_price - 1);
  const day = isNum(sig.prev_close) ? 100 * (sig.last_price / sig.prev_close - 1) : null;
  return el("div", { class: "small" },
    el("strong", { class: since >= 0 ? "pos-text" : "neg-text", text: `${signedPct(since, 2)} since the news` }),
    ` · ${money(sig.ref_price)} → ${money(sig.last_price)}`, isNum(day) ? ` · ${signedPct(day, 1)} on the day` : "");
}

function holdingsBanner(sig) {
  const held = sig.holdings || [];
  if (!held.length) return null;
  const against = held.filter(h => h.news === "against"), with_ = held.filter(h => h.news === "with");
  if (against.length) return banner("fail", `You hold ${against.map(h => h.what).join("; ")}, and the news moved ${sig.symbol} against it. `
    + "Consider selling (or covering), or tightening the stop. Nothing has been ordered.");
  if (with_.length) return banner("pass", `You hold ${with_.map(h => h.what).join("; ")}: the news moved ${sig.symbol} your way. Check your stop and targets.`);
  return banner("info", `You hold ${held.map(h => h.what).join("; ")}.`);
}

// After the hour the screen stops checking an opportunity: a price watch (the Morning plan's) keeps an eye on
// its stop while the app runs, and emails when it is crossed if email is set up.
async function liveWatchStop(card, button) {
  try {
    await api("/v2/morning/watch", { symbol: card.symbol, direction: card.direction,
      kind: card.direction === "LONG" ? "below" : "above", level: Math.round(card.stop * 100) / 100,
      why: `The stop of the ${card.symbol} ${card.side_word} after news (Live news)` });
    button.textContent = "Watching (listed on the Morning plan)";
  } catch (e) { button.textContent = `Not watched: ${e.message}`; }
}

/* 2.11: why is it moving? Stocks that moved a lot since the last close, and what a news search found. */
function moversPanel(d) {
  const rows = d.movers || [];
  const t = d.thresholds || {};
  const intro = d.mover_scan_minutes > 0
    ? `Every ${num(d.mover_scan_minutes, 0)} minutes while trading (pre-market and after hours included), the stocks Live news follows are scanned for moves of ${num(t.late_day_pct, 0)}% and ${num(t.late_day_ranges, 1)} daily ranges since the last close, and the news is searched for those with nothing in the feed. Search finds a story minutes after it is published, so it explains a move rather than coming before it.`
    : "The movers scan is off (LIVE_MOVER_SWEEP_MINUTES=0).";
  const newsCell = m => {
    if (m.found) return el("div", { class: "small" }, headlineLink({ headline: m.headline || "News found", url: m.url }),
      el("span", { class: "muted", text: m.via ? ` (${m.via})` : "" }));
    if (m.found === false) return el("div", { class: "small" }, el("strong", { text: "No news found" }),
      el("span", { class: "muted", text: ` in ${(m.checked || []).join(", ") || "the search"}. A big move with no news found can snap back; check it before riding it.` }));
    if ((m.errors || []).length) return el("span", { class: "small error", text: `Search failed: ${m.errors[0]}` });
    return el("span", { class: "small muted", text: "Not searched yet" });
  };
  const analyze = m => {
    if (!state.openDecide || !featureOn("trend_engine_v2")) return "";
    const b = el("button", { class: "plain small-btn", text: "Analyze" });
    b.addEventListener("click", () => state.openDecide(m.symbol, m.found ? (m.move_pct > 0 ? "LONG" : "SHORT") : null));
    return b;
  };
  return el("div", { class: "panel" }, el("h3", { text: `Why is it moving? (${rows.length})` }),
    el("p", { class: "small muted", text: intro }),
    rows.length ? table([
      { label: "Stock", cell: m => el("div", {}, el("strong", { text: m.symbol }), el("div", { class: "small muted", text: money(m.price) })) },
      { label: "Since the close", num: true, cell: m => el("span", { class: m.move_pct >= 0 ? "pos-text" : "neg-text", text: signedPct(m.move_pct, 1) }) },
      { label: "Daily ranges", num: true, cell: m => num(m.ranges, 1) },
      { label: "News", cell: newsCell },
      { label: "", cell: analyze }], rows)
      : el("p", { class: "small muted", text: !d.running ? "The scan runs while listening." : d.session === "closed" ? "The market is closed; the scan runs while trading (pre-market and after hours included)." : d.mover_scan_minutes > 0 && !(d.search && (d.search.sources || []).length) ? "No news search is on (GOOGLE_NEWS=0 and no BRAVE_API_KEY), so the scan does not run." : "No stock has moved that much yet." }));
}

function opportunityPanel(sig) {
  const news = (sig.news || [])[0] || {};
  const verb = sig.direction === "LONG" ? "Buy" : "Short";
  const head = el("div", { class: "panel-head" },
    el("h3", {}, `${verb} ${sig.symbol}`, el("span", { class: "muted", text: ` · ${LIVE_STATE_WORDS[sig.state] || sig.state}` })),
    el("span", { class: "small muted", text: `${news.source || ""} · ${liveAgo(news.received_at)}${news.delay_seconds !== null && news.delay_seconds !== undefined ? ` · ${liveDelay(news.delay_seconds)}` : ""}` }));
  const box = el("div", { class: `panel live-opp${sig.state === "stopped" ? " live-stopped" : ""}`, id: `live-${sig.id}` }, head,
    el("p", {}, headlineLink(news), " ", news.symbols ? toneTag(news) : ""),
    signalMove(sig), el("p", { class: "small", text: sig.reason || "" }));
  const hb = holdingsBanner(sig);
  if (hb) box.append(hb);
  if (sig.card) box.append(morningCard(sig.card, sig.state === "opportunity" ? { watchStop: liveWatchStop } : {}));
  else if (sig.state === "opportunity") box.append(banner("warn", "No trade card could be built (see above); the move is listed so you can look yourself."));
  if ((sig.news || []).length > 1) box.append(el("details", {}, el("summary", { text: `${sig.news.length - 1} more item${sig.news.length > 2 ? "s" : ""} on ${sig.symbol}` }),
    el("ul", { class: "plain small" }, sig.news.slice(1).map(n => el("li", {}, `${clockTime(n.received_at).slice(0, 8)} `, headlineLink(n), ` (${n.source})`)))));
  return box;
}

function buildLive() {
  const startBtn = el("button", { class: "primary", text: "Start listening" });
  const stopBtn = el("button", { class: "plain hidden", text: "Stop" });
  const notifyBtn = el("button", { class: "plain", text: "Notify me in this browser" });
  const allBox = el("input", { type: "checkbox" });
  const err = el("div", { class: "error", role: "alert" });
  const statusLine = el("div", { class: "small muted" });
  const sourcesBox = el("div", { class: "live-sources" });
  const notesBox = el("div");
  const oppBox = el("div"), heldBox = el("div"), moverBox = el("div"), watchBox = el("div"), feedBox = el("div");
  const seenOpp = new Set();
  let timer = null, failures = 0, firstPaint = true, lastKey = "";

  if (!("Notification" in window)) notifyBtn.classList.add("hidden");
  else if (Notification.permission === "granted") notifyBtn.textContent = "Browser notifications on";
  notifyBtn.addEventListener("click", async () => {
    try { const p = await Notification.requestPermission(); notifyBtn.textContent = p === "granted" ? "Browser notifications on" : "Notifications blocked by the browser"; }
    catch (e) { notifyBtn.textContent = "Notifications unavailable"; }
  });

  function notify(sig) {
    if (!("Notification" in window) || Notification.permission !== "granted") return;
    const news = (sig.news || [])[0] || {};
    const against = (sig.holdings || []).some(h => h.news === "against");
    try {
      const n = new Notification(against ? `News against your ${sig.symbol} position` : `${sig.direction === "LONG" ? "Buy" : "Short"} ${sig.symbol}: the price confirms the news`,
        { body: news.headline || sig.reason || "", tag: `live-${sig.id}` });
      n.onclick = () => { window.focus(); const node = document.getElementById(`live-${sig.id}`); if (node) node.scrollIntoView({ behavior: "smooth" }); };
    } catch (e) { /* some browsers allow notifications only from a service worker; the screen still shows it */ }
  }

  function renderSources(d) {
    sourcesBox.replaceChildren(...(d.sources || []).map(s => el("span", { class: `live-source live-${s.state}`,
      title: [s.note, s.last_error ? `Last error ${clockTime(s.last_error.at).slice(0, 8)}: ${s.last_error.error}` : null].filter(Boolean).join("\n") || null },
      el("strong", { text: s.name }), ` ${LIVE_SOURCE_WORDS[s.state] || s.state}`,
      s.items ? el("span", { class: "muted", text: ` · ${s.items} item${s.items === 1 ? "" : "s"}` }) : "",
      s.name === "Brave" && d.search && d.search.brave_usage ? el("span", { class: "muted",
        text: ` · ${d.search.brave_usage.month} of ${d.search.brave_usage.month_limit} in ${d.search.brave_usage.window_days || 31} days` }) : "")));
    if (!(d.sources || []).length) sourcesBox.replaceChildren(el("span", { class: "small muted", text: "No news source is set up yet." }));
    const notes = [];
    if (d.mode === "demo") notes.push(banner("warn", "Demo data: the news items and the price moves after them are made up, so you can learn the screen on any day."));
    if (d.session === "closed") notes.push(banner("info", "The market is closed: news is collected now, and each stock is priced when trading resumes (pre-market included)."));
    for (const s of d.sources || []) if (s.state === "failed" && s.note) notes.push(banner("fail", s.note));
    if (d.search && d.search.google_paused_until) notes.push(banner("warn", `Google News refused a search, so it is paused until ${clockTime(d.search.google_paused_until).slice(0, 5)} New York time; it resumes by itself.`));
    for (const n of d.setup_notes || []) notes.push(el("p", { class: "small muted", text: n }));
    notesBox.replaceChildren(...notes);
    const t = d.thresholds || {};
    statusLine.textContent = [d.running ? `Listening since ${clockTime(d.started_at).slice(0, 8)} New York time` : d.stopping ? "Stopping (finishing the last pass)" : "Not listening",
      `${d.universe === "all" ? "every ticker at $5 or more trading $20 million a day" : `${num(d.universe_size, 0)} stocks (the large ones, your watchlist and holdings)`}`,
      `a move counts at ${num(t.reaction_pct, 1)}% and ${num(t.reaction_ranges, 2)} daily ranges since the news`,
      d.email ? "opportunities are emailed" : null].filter(Boolean).join(" · ");
    startBtn.classList.toggle("hidden", d.running);
    startBtn.disabled = !!d.stopping;
    stopBtn.classList.toggle("hidden", !d.running);
  }

  function render(d) {
    renderSources(d);
    const sigs = d.signals || [];
    const opps = sigs.filter(s => ["opportunity", "stopped", "done"].includes(s.state) && s.direction);
    const key = JSON.stringify([sigs.map(s => [s.id, s.state, s.last_price, s.checks]), d.last_id, allBox.checked,
      (d.movers || []).map(m => [m.symbol, m.updated_at, m.found])]);
    if (key === lastKey) return;
    lastKey = key;
    for (const s of opps) {
      if (!seenOpp.has(s.id)) { seenOpp.add(s.id); if (!firstPaint && s.state === "opportunity") notify(s); }
    }
    firstPaint = false;
    oppBox.replaceChildren(el("h3", { class: "m-section", text: `Opportunities (${opps.length})` }),
      ...(opps.length ? opps.map(opportunityPanel) : [el("div", { class: "panel" }, el("p", { class: "small muted",
        text: "None yet. An opportunity appears when a stock's price confirms its news: a move since the news of the size shown above, the day's move the same way." }))]));

    const held = sigs.filter(s => (s.holdings || []).length && !opps.includes(s));
    heldBox.replaceChildren(...(held.length ? [el("div", { class: "panel" }, el("h3", { text: "News on stocks you hold" }),
      ...held.map(s => el("div", { class: "live-row" }, el("strong", { text: s.symbol }), " ", headlineLink((s.news || [])[0] || {}),
        el("div", {}, signalMove(s)), holdingsBanner(s))))] : []));

    moverBox.replaceChildren(moversPanel(d));
    const watching = sigs.filter(s => ["waiting", "watching"].includes(s.state));
    const quiet = sigs.filter(s => ["no_reaction", "skipped"].includes(s.state)).slice(0, 25);
    watchBox.replaceChildren(el("div", { class: "panel" }, el("h3", { text: `Watching the price after the news (${watching.length})` }),
      watching.length ? table([
        { label: "Stock", cell: s => el("strong", { text: s.symbol }) },
        { label: "News", cell: s => el("div", { class: "small" }, headlineLink((s.news || [])[0] || {})) },
        { label: "Price since", cell: s => signalMove(s) },
        { label: "Next check", cell: s => el("span", { class: "small muted", text: s.next_check_at ? clockTime(s.next_check_at).slice(0, 8) : "—" }) }], watching)
        : el("p", { class: "small muted", text: "Nothing being watched right now." }),
      quiet.length ? el("details", {}, el("summary", { text: `${quiet.length} item${quiet.length === 1 ? "" : "s"} the price did not answer (or that could not be traded)` }),
        table([{ label: "Stock", cell: s => s.symbol }, { label: "News", cell: s => el("div", { class: "small" }, headlineLink((s.news || [])[0] || {})) },
          { label: "What happened", cell: s => el("span", { class: "small muted", text: `${LIVE_STATE_WORDS[s.state]}: ${s.reason || ""}` }) }], quiet)) : null));

    const items = d.items || [];
    feedBox.replaceChildren(el("div", { class: "panel" },
      el("div", { class: "panel-head" }, el("h3", { text: "News as it arrived" }),
        el("label", { class: "small" }, allBox, " Show every ticker")),
      items.length ? table([
        { label: "Time (New York)", cell: it => el("div", { class: "small" }, clockTime(it.received_at).slice(0, 8), el("div", { class: "muted", text: liveDelay(it.delay_seconds) })) },
        { label: "Stocks", cell: it => el("span", { text: (it.symbols || []).join(", ") || "—" }) },
        { label: "Headline", cell: it => el("div", {}, headlineLink(it), it.summary ? el("div", { class: "small muted", text: it.summary.slice(0, 180) }) : null) },
        { label: "Source", cell: it => el("span", { class: "small", text: it.source }) },
        { label: "Tone", cell: it => toneTag(it) }], items)
        : el("p", { class: "small muted", text: d.running ? "Nothing yet. Items appear here within seconds of arriving." : "Press Start listening." })));
  }

  async function poll() {
    clearTimeout(timer);
    // On another screen (or another tab) the feed keeps being read, more slowly, only when browser
    // notifications are on: they are the point of reading it there.
    const away = view.classList.contains("hidden") || document.hidden;
    const alerts = "Notification" in window && Notification.permission === "granted";
    if (view.classList.contains("hidden") && !alerts) return;
    try {
      const d = await api(`/v2/live?all_symbols=${allBox.checked ? "true" : "false"}`);
      failures = 0;
      err.textContent = "";
      render(d);
    } catch (e) {
      failures += 1;
      err.textContent = `${e.message} (trying again)`;
    }
    timer = setTimeout(poll, failures ? Math.min(30000, LIVE_POLL_MS * 2 ** failures) : away ? 15000 : LIVE_POLL_MS);
  }
  startBtn.addEventListener("click", () => withBusy(startBtn, err, async () => { renderSources(await api("/v2/live/start", {})); lastKey = ""; poll(); }));
  stopBtn.addEventListener("click", () => withBusy(stopBtn, err, async () => { renderSources(await api("/v2/live/stop", {})); }));
  allBox.addEventListener("change", () => { lastKey = ""; poll(); });

  const view = el("section", {}, el("h2", { text: "Live news" }),
    el("p", { class: "lede", text: "Press releases, 8-K filings and headlines as they arrive, the price's answer in the minutes after, and a trade card when the move confirms: shares or calls when the news sends a stock up, a short or puts when it sends it down. A news move is usually fastest in its first seconds; this shows you the news and the price together, with the risk worked out. Nothing here places an order." }),
    el("div", { class: "panel" }, el("div", { class: "row" }, startBtn, stopBtn, notifyBtn), err, sourcesBox, statusLine, notesBox),
    oppBox, heldBox, moverBox, watchBox, feedBox);
  view.onShow = () => poll();
  return view;
}

Object.assign(BUILDERS, { live: buildLive });
