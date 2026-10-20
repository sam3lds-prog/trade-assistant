"use strict";
/* Trade Assistant 2.10: Live news. Stock-moving news as it arrives (press releases, 8-K filings, headlines),
   the price's answer in the minutes after, and a trade card when the move confirms: buy shares or calls when
   the news sends a stock up, short or buy puts when it sends it down, and an alert on anything you hold.
   2.15: a digest every 15, 30 or 60 minutes (or over the session) ranks the news's best opportunities, with a
   budget split across them (blend.js). 2.16: each item is judged in five steps (what it is, tradable now, the way it
   should push the price, how much is already in the price, the signal), and the replay measures what price an order
   could really have got 100 ms, 500 ms, 1 s, 5 s and 30 s after a headline. Setups, not forecasts, and nothing here
   places an order. */

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

/* ---------- 2.16: the five steps ---------- */
const INTEL_STEP_WORDS = { detected: "Detected", tradable: "Tradable", effect: "Effect", priced_in: "Priced in", signal: "Signal" };
const INTEL_STATE_CLASS = { pass: "intel-pass", warn: "intel-warn", fail: "intel-fail", info: "intel-info" };

function catalystTag(cat) {
  if (!cat) return "";
  return el("span", { class: `m-tag ${cat.major ? "m-news" : "m-none"}`, title: `${cat.severity}/100, read from the ${cat.via}. ${cat.why}`,
    text: `${cat.label}${cat.major ? " · major" : ""}` });
}

function intelStrip(intel) {
  if (!intel || !(intel.steps || []).length) return null;
  return el("div", { class: "intel-strip" }, intel.steps.map(st => el("span", { class: `intel-step ${INTEL_STATE_CLASS[st.state] || "intel-info"}`,
    title: `${INTEL_STEP_WORDS[st.step] || st.step}: ${st.title}. ${st.detail || ""}` },
    el("b", { text: INTEL_STEP_WORDS[st.step] || st.step }), ` ${st.title}`)));
}

function intelPanel(intel) {
  if (!intel || !(intel.steps || []).length) return null;
  const sig = intel.signal || {};
  const leg = intel.second_leg || {};
  const box = el("div", { class: "intel-box" }, intelStrip(intel),
    el("p", { class: "small" }, el("strong", { text: `${{ enter: "Enter", wait: "Wait", pass: "Pass" }[sig.action] || "Signal"}${sig.direction ? ` ${sig.direction === "LONG" ? "long" : "short"}` : ""}: ` }), sig.why || ""),
    el("details", {}, el("summary", { class: "small", text: "The five steps in full" }),
      el("ol", { class: "intel-list small" }, intel.steps.map(st => el("li", { class: INTEL_STATE_CLASS[st.state] || "" },
        el("strong", { text: `${INTEL_STEP_WORDS[st.step] || st.step}: ${st.title}. ` }), st.detail || "")),
        leg.text ? el("li", {}, el("strong", { text: "The record: " }), leg.text) : null)));
  return box;
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
    if (m.unclear) {
      const n = m.unclear, only = m.move_only === n;
      const text = only ? ` The only headline${n === 1 ? "" : "s"} found ${n === 1 ? "says" : "say"} that it moved, not why.`
        : ` ${n} headline${n === 1 ? "" : "s"} came back that may be about it but could not be tied to it.`;
      return el("div", { class: "small" }, el("strong", { text: "News not ruled out." }), el("span", { class: "muted", text }),
        m.search_url && /^https:\/\/news\.google\.com\//.test(m.search_url) ? el("span", {}, " ", el("a", { href: m.search_url, target: "_blank", rel: "noopener noreferrer", text: "Read them on Google News" })) : "");
    }
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
    signalMove(sig), el("p", { class: "small", text: sig.reason || "" }), intelPanel(sig.intel));
  const hb = holdingsBanner(sig);
  if (hb) box.append(hb);
  if (sig.card) box.append(morningCard(sig.card, sig.state === "opportunity" ? { watchStop: liveWatchStop } : {}));
  else if (sig.state === "opportunity") box.append(banner("warn", "No trade card could be built (see above); the move is listed so you can look yourself."));
  if ((sig.news || []).length > 1) box.append(el("details", {}, el("summary", { text: `${sig.news.length - 1} more item${sig.news.length > 2 ? "s" : ""} on ${sig.symbol}` }),
    el("ul", { class: "plain small" }, sig.news.slice(1).map(n => el("li", {}, `${clockTime(n.received_at).slice(0, 8)} `, headlineLink(n), ` (${n.source})`)))));
  return box;
}

/* ---------- 2.15: the digest: the news's best opportunities every so often, ranked, with a budget split ---------- */
function digestSection() {
  const picked = new Set();
  const choice = el("select", { "aria-label": "Digest every" }, [["15", "Every 15 minutes"], ["30", "Every 30 minutes"], ["60", "Every hour"], ["session", "The whole session"]]
    .map(([v, t]) => el("option", { value: v, text: t })));
  const buildBtn = el("button", { class: "plain", text: "Build the digest now" });
  const err = el("div", { class: "error", role: "alert" });
  const head = el("p", { class: "small muted" });
  const body = el("div");
  const cardsBox = el("div");
  const blend = blendPanel({ source: "live", collapsed: true, horizon: "day", title: "Split a budget across the news's opportunities",
    keys: () => [...picked] });
  const budgetBtn = el("button", { class: "primary small-btn", text: "Set budget" });
  budgetBtn.addEventListener("click", () => blend.toggle());
  if (!featureOn("opportunity_blend")) { budgetBtn.classList.add("hidden"); blend.classList.add("hidden"); }   // FEATURE_OPPORTUNITY_BLEND=0
  let shown = "";
  const render = d => {
    if (!d) { head.textContent = "No digest yet. It is built at the interval above while Live news is listening, or press Build the digest now."; body.replaceChildren(); cardsBox.replaceChildren(); return; }
    const key = `${d.built_at}|${d.count}`;
    if (key === shown) return;
    shown = key;
    head.textContent = `Built ${new Date(d.built_at).toLocaleTimeString("en-US", { timeZone: "America/New_York", hour: "2-digit", minute: "2-digit" })} New York time, covering ${d.window}: ${d.count} opportunit${d.count === 1 ? "y" : "ies"}.${d.record ? ` The record: ${num(d.record.cases, 0)} stock-days of 5-minute bars.` : ""}`;
    const notes = (d.notes || []).map(n => el("p", { class: "small muted", text: n }));
    const opps = d.opportunities || [];
    body.replaceChildren(...notes, opps.length ? table([
      { label: "", cell: o => pickBox(picked, o.key) },
      { label: "#", num: true, cell: o => num(o.rank, 0) },
      { label: "Stock", cell: o => el("div", {}, el("strong", { text: o.symbol }), el("div", { class: "small muted", text: money(o.price) })) },
      { label: "News", cell: o => el("div", { class: "small" }, headlineLink({ headline: o.headline || "News", url: o.url }), el("span", { class: "muted", text: o.news_source ? ` (${o.news_source})` : "" })) },
      { label: "Trade", cell: o => el("div", {}, el("span", { class: `m-tag ${o.direction === "LONG" ? "m-ride" : "m-fade"}`, text: o.direction === "LONG" ? "Buy" : "Short" }),
        o.option ? el("div", { class: "small muted", text: `or a ${o.option.option_type.toLowerCase()} at $${num(o.option.premium, 2)}` }) : null) },
      { label: "On the day", num: true, cell: o => isNum(o.move_pct) ? el("span", { class: o.move_pct >= 0 ? "pos-text" : "neg-text", text: signedPct(o.move_pct, 1) }) : "n/a" },
      { label: "Edge", num: true, cell: edgeCell },
      { label: "Score", num: true, cell: scoreCell },
      { label: "", cell: o => state.openOpportunity && featureOn("trend_engine_v2") ? analyzeButton(o) : "" }], opps)
      : el("p", { class: "small muted", text: "Nothing in this window." }));
    const byKey = Object.fromEntries(opps.map(o => [o.card_key, o]));
    cardsBox.replaceChildren(...(d.cards || []).map(c => morningCard(c, { watchStop: liveWatchStop, opportunity: byKey[c.key],
      pick: byKey[c.key] ? () => { const k = byKey[c.key].key; if (picked.has(k)) picked.delete(k); else picked.add(k); } : null,
      picked: byKey[c.key] ? () => picked.has(byKey[c.key].key) : null })));
  };
  const refresh = async () => {
    try { const r = await api("/v2/live/digest"); choice.value = r.interval; render(r.digest); err.textContent = ""; }
    catch (e) { err.textContent = e.message; }
  };
  choice.addEventListener("change", () => api("/v2/prefs", { live: { digest: choice.value } }).then(p => { state.prefs = p; }).catch(e => { err.textContent = e.message; }));
  buildBtn.addEventListener("click", () => withBusy(buildBtn, err, async () => { shown = ""; render((await api("/v2/live/digest", { interval: choice.value })).digest); }));
  const node = el("div", {},
    el("div", { class: "panel" },
      el("div", { class: "panel-head" }, el("h3", {}, "The digest: the news's best opportunities ", infoIcon("digest", "About the digest")), budgetBtn),
      el("div", { class: "row" }, field("Rank", choice), buildBtn), head, err, body),
    blend, cardsBox);
  return { node, refresh };
}

/* ---------- 2.16: the replay: what price an order could really have got after a headline ---------- */
const REPLAY_POLL_MS = 2500;
const offsetWord = s => s < 1 ? `${Math.round(s * 1000)} ms` : `${num(s, 0)} s`;
const horizonWord = h => ({ "60": "1 min", "300": "5 min", "900": "15 min", "1800": "30 min", "3600": "1 hour", close: "close" })[h] || `${h} s`;

function parseManualEvents(text) {
  const out = [], bad = [];
  for (const raw of (text || "").split("\n")) {
    const line = raw.trim();
    if (!line) continue;
    const parts = line.split(",").map(x => x.trim());
    if (parts.length < 2 || !parts[0] || !parts[1]) { bad.push(line); continue; }
    const last = (parts[parts.length - 1] || "").toUpperCase();
    const direction = ["LONG", "SHORT"].includes(last) ? last : null;
    const headline = parts.slice(2, direction ? parts.length - 1 : parts.length).join(", ");
    out.push({ symbol: parts[0].toUpperCase(), published_at: parts[1], headline, direction });
  }
  return { events: out, bad };
}

function replayTable(r) {
  const keys = r.horizons || [];
  const cell = (row, k) => {
    const c = (row.horizons || {})[k];
    if (!c || !isNum(c.median)) return el("span", { class: "muted", text: "—" });
    return el("div", { class: "small" }, el("strong", { class: c.median >= 0 ? "pos-text" : "neg-text", text: signedPct(c.median, 2) }),
      el("div", { class: "muted", text: `${isNum(c.hit_rate) ? pct(100 * c.hit_rate, 0) : "n/a"} up · ${c.n}` }));
  };
  return table([
    { label: "Order sent", cell: row => el("div", {}, el("strong", { text: `${offsetWord(row.offset_s)} after receipt` }),
      el("div", { class: "small muted", text: `${row.n} fill${row.n === 1 ? "" : "s"}${row.halted ? `, ${row.halted} halted` : ""}` })) },
    { label: "Fill vs pre-news", num: true, cell: row => isNum(row.since_ref_median_pct) ? el("span", { class: "small", text: signedPct(row.since_ref_median_pct, 2) }) : "—" },
    { label: "First minute caught", num: true, cell: row => isNum(row.captured_median) ? el("span", { class: row.captured_median > 0 ? "pos-text" : "neg-text", text: pct(100 * row.captured_median, 0) }) : "—" },
    { label: "Fill lag", num: true, cell: row => isNum(row.lag_median_s) ? `${num(row.lag_median_s, 1)} s` : "—" },
    ...keys.map(k => ({ label: `Held to ${horizonWord(k)}`, num: true, cell: row => cell(row, k) }))], r.table || []);
}

function replayEventsTable(results) {
  const rows = (results || []).slice(0, 150);
  const net = (res, o, h) => {
    const row = (res.offsets || []).find(x => x.offset_s === o && x.fill);
    const v = row ? (row.net || {})[h] : null;
    return isNum(v) ? el("span", { class: v >= 0 ? "pos-text" : "neg-text", text: signedPct(v, 2) }) : el("span", { class: "muted", text: "—" });
  };
  return table([
    { label: "Stock", cell: e => el("div", {}, el("strong", { text: e.symbol }), el("div", { class: "small muted", text: e.published_at ? `${clockTime(e.published_at).slice(0, 8)} ${shortDate(e.published_at.slice(0, 10))}` : "" })) },
    { label: "Headline", cell: e => el("div", { class: "small" }, e.headline || "(typed in)", el("div", { class: "muted", text: `${e.catalyst_label || e.catalyst} · ${e.direction > 0 ? "long" : "short"} · ${e.source || ""}` })) },
    { label: "Pre-news → 1 min", num: true, cell: e => e.result && !e.result.skipped ? el("span", { class: (e.result.jump_1m_pct || 0) >= 0 ? "pos-text" : "neg-text", text: `${money(e.result.ref_price)} ${signedPct(e.result.jump_1m_pct, 2)}` }) : el("span", { class: "small muted", text: e.result ? e.result.skipped : "" }) },
    { label: "100 ms → 30 min", num: true, cell: e => e.result && !e.result.skipped ? net(e.result, 0.1, "1800") : "" },
    { label: "30 s → 30 min", num: true, cell: e => e.result && !e.result.skipped ? net(e.result, 30, "1800") : "" },
    { label: "30 s → close", num: true, cell: e => e.result && !e.result.skipped ? net(e.result, 30, "close") : "" },
    { label: "Notes", cell: e => el("span", { class: "small muted", text: e.result && !e.result.skipped ? [e.result.halted ? "halted" : null,
      e.result.spread_estimated ? `spread ${num(e.result.spread_bps, 0)} bps (estimated)` : `spread ${num(e.result.spread_bps, 0)} bps`,
      `${e.result.prints} prints`].filter(Boolean).join(" · ") : "" }) }], rows);
}

function replaySection() {
  const source = el("select", { "aria-label": "Events to replay" }, [["feed", "This week's feed"], ["both", "The feed and the events typed in"], ["manual", "Only the events typed in"]]
    .map(([v, t]) => el("option", { value: v, text: t })));
  const latency = el("input", { type: "number", step: "0.1", min: "0", value: "1.0", style: "width: 6em" });
  const spread = el("input", { type: "number", step: "1", min: "0", placeholder: "estimate", style: "width: 6em" });
  const slippage = el("input", { type: "number", step: "1", min: "0", value: "5", style: "width: 6em" });
  const limit = el("input", { type: "number", step: "1", min: "1", max: "150", value: "60", style: "width: 5em" });
  const manual = el("textarea", { rows: "3", class: "intel-manual", placeholder: "One event a line: SYMBOL, time, headline, LONG or SHORT\ne.g. ACME, 2026-10-19T08:30:00-04:00, Acme to be acquired for $40 a share, LONG" });
  const runBtn = el("button", { class: "primary", text: "Run the replay" });
  const stopBtn = el("button", { class: "plain hidden", text: "Stop" });
  const err = el("div", { class: "error", role: "alert" });
  const status = el("p", { class: "small muted" });
  const verdicts = el("div");
  const body = el("div");
  let timer = null, shown = "";

  const render = d => {
    const job = d.job || {};
    const r = d.result;
    const reach = d.reach || {};
    const lines = [reach.note || ""];
    if (isNum(d.feed_events)) lines.push(`${d.feed_events} event${d.feed_events === 1 ? "" : "s"} in the feed can be replayed.`);
    if (job.state === "running") lines.push(`Replaying ${job.done} of ${job.total}${job.current ? ` (${job.current})` : ""}…`);
    else if (job.state === "failed") lines.push(`The last run failed: ${job.error}`);
    else if (job.state === "stopped") lines.push("The last run was stopped.");
    status.textContent = lines.filter(Boolean).join(" ");
    runBtn.classList.toggle("hidden", job.state === "running");
    stopBtn.classList.toggle("hidden", job.state !== "running");
    if (!r) { verdicts.replaceChildren(); body.replaceChildren(el("p", { class: "small muted", text: "No replay yet." })); shown = ""; return; }
    const key = `${r.built_at}|${r.run_id}`;
    if (key === shown) return;
    shown = key;
    const p = r.params || {};
    verdicts.replaceChildren(el("p", { class: "small muted", text: `Built ${new Date(r.built_at).toLocaleTimeString("en-US", { timeZone: "America/New_York", hour: "2-digit", minute: "2-digit" })} New York time: ${r.events} event${r.events === 1 ? "" : "s"} replayed${r.skipped ? `, ${r.skipped} skipped` : ""}; feed latency ${num(p.feed_latency_s, 1)} s, spread ${isNum(p.spread_bps) ? `${num(p.spread_bps, 0)} bps` : "estimated from the prints before each headline"}, slippage ${num(p.slippage_bps, 0)} bps, each paid on the way in and out.` }),
      ...(r.verdicts || []).map(v => banner(v.state === "pass" ? "pass" : v.state === "fail" ? "fail" : v.state === "warn" ? "warn" : "info", v.text)));
    const kinds = (r.kinds || []).length ? el("details", {}, el("summary", { text: "By kind of news: entering at 30 s, holding 30 minutes" }),
      table([{ label: "Kind", cell: k => k.label }, { label: "Events", num: true, cell: k => num(k.events, 0) },
        { label: "First-minute move", num: true, cell: k => isNum(k.median_jump_pct) ? signedPct(k.median_jump_pct, 2) : "—" },
        { label: "Median after costs", num: true, cell: k => isNum(k.median_net_pct) ? el("span", { class: k.median_net_pct >= 0 ? "pos-text" : "neg-text", text: signedPct(k.median_net_pct, 2) }) : "—" },
        { label: "Positive", num: true, cell: k => isNum(k.hit_rate) ? pct(100 * k.hit_rate, 0) : "—" }], r.kinds)) : null;
    body.replaceChildren(el("h4", { class: "small", text: "Median result after costs, by when the order was sent and how long it was held" }),
      replayTable(r), kinds,
      el("details", {}, el("summary", { text: `Every event (${(r.results || []).length})` }), replayEventsTable(r.results)));
  };
  const refresh = async () => {
    clearTimeout(timer);
    try { const d = await api("/v2/live/replay"); render(d); err.textContent = "";
      if ((d.job || {}).state === "running") timer = setTimeout(refresh, REPLAY_POLL_MS); }
    catch (e) { err.textContent = e.message; }
  };
  runBtn.addEventListener("click", () => withBusy(runBtn, err, async () => {
    const parsed = parseManualEvents(manual.value);
    if (parsed.bad.length) throw new Error(`Could not read: ${parsed.bad[0]} (SYMBOL, time, headline, LONG or SHORT)`);
    if (source.value === "manual" && !parsed.events.length) throw new Error("Type in at least one event, or replay the feed.");
    const body = { events: source.value, manual: parsed.events, feed_latency_s: Number(latency.value) || 0,
      slippage_bps: Number(slippage.value) || 0, limit: Math.max(1, Math.min(150, Number(limit.value) || 60)) };
    if (spread.value !== "") body.spread_bps = Number(spread.value);
    render(await api("/v2/live/replay", body));
    timer = setTimeout(refresh, REPLAY_POLL_MS);
  }));
  stopBtn.addEventListener("click", () => withBusy(stopBtn, err, async () => { render(await api("/v2/live/replay/stop", {})); timer = setTimeout(refresh, REPLAY_POLL_MS); }));
  const node = el("details", { class: "panel" }, el("summary", {}, "The replay: what was really available after a headline ", infoIcon("newsreplay", "About the replay")),
    el("p", { class: "small muted", text: "For each headline the feed collected (or that you type in), the trade prints around it say what price a marketable order would have got 100 ms, 500 ms, 1 s, 5 s and 30 s after the app received it, after the feed's latency, half the spread, slippage and any halt, and what that trade made held to a minute, five, fifteen, thirty, an hour and the close. Each event is traded the way the news should push it, decided before the price moved. The answer to \"is a sub-second feed worth paying for\" is the gap between the 100 ms row and the 30 s row; the trade to build on is the second leg, if the 30 s row is positive." }),
    el("div", { class: "row" }, field("Events", source), field("Feed latency (s)", latency), field("Spread (bps)", spread), field("Slippage (bps)", slippage), field("At most", limit), runBtn, stopBtn),
    field("Events typed in", manual), err, status, verdicts, body);
  return { node, refresh };
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
        { label: "The five steps", cell: s => s.intel ? el("div", {}, intelStrip(s.intel), el("div", { class: "small muted", text: ((s.intel.signal || {}).why || "").slice(0, 160) })) : el("span", { class: "small muted", text: "Assessed at the first price" }) },
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
        { label: "Kind", cell: it => catalystTag(it.catalyst) },
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

  const digest = digestSection();
  const replay = replaySection();
  let digestTimer = null;
  const digestPoll = async () => {
    clearTimeout(digestTimer);
    if (view.classList.contains("hidden")) return;
    await digest.refresh();
    digestTimer = setTimeout(digestPoll, 30000);
  };
  const view = el("section", {}, el("h2", { text: "Live news" }),
    el("p", { class: "lede", text: "Press releases, 8-K filings and headlines as they arrive, the price's answer in the minutes after, and a trade card when the move confirms: shares or calls when the news sends a stock up, a short or puts when it sends it down. Each item is judged in five steps: what it is, whether its stock can be traded right now, the way the news should push it, how much of that push is already in the price, and only then a signal. Every 15, 30 or 60 minutes the digest ranks the news's best opportunities, ready to blend under a budget. A news move is usually fastest in its first seconds, and the replay below measures what was really left for an order sent after the headline. Nothing here places an order." }),
    el("div", { class: "panel" }, el("div", { class: "row" }, startBtn, stopBtn, notifyBtn), err, sourcesBox, statusLine, notesBox),
    digest.node, oppBox, heldBox, moverBox, watchBox, feedBox, replay.node);
  view.onShow = () => { poll(); digestPoll(); replay.refresh(); };
  return view;
}

Object.assign(BUILDERS, { live: buildLive });
