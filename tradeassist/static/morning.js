"use strict";
/* Trade Assistant 2.8: the Morning plan. Big movers since yesterday's close read as ride-the-news or
   fade-the-move, reversal triggers to watch, and a trade card per setup with the limit price to type.
   2.15: the 250 largest stocks ranked as opportunities before the open and through the morning session, the
   markets overseas, day trades or swing trades, and a budget split across the best (blend.js).
   Three steps: pick, read the card, copy the order into Robinhood. Nothing here places an order. */

const MORNING_SETUP_WORDS = { fade: "Fade the move", ride: "Ride the news", reversal: "Reversal", swing: "Morning swing", gap: "The move", news: "News" };

function morningMoney(v, d = 2) { return isNum(v) ? money(v, d) : "n/a"; }

/* ---------- small pieces ---------- */
function newsTag(news) {
  const cls = news.state === "news" ? "m-news" : news.state === "no_news" ? "m-nonews" : "m-unchecked";
  return el("span", { class: `m-tag ${cls}`, title: (news.items || []).map(i => i.headline).join("\n") || null, text: news.label });
}

function setupTag(setup) {
  if (!setup || !setup.kind) return el("span", { class: "m-tag m-none", text: "No setup" });
  const word = `${MORNING_SETUP_WORDS[setup.kind]} → ${setup.direction === "LONG" ? "buy" : "short"}`;
  return el("span", { class: `m-tag ${setup.kind === "ride" ? "m-ride" : "m-fade"}`, text: word });
}

function recordText(rec, direction) {
  const h = (rec && rec.horizons) || {};
  const pick = h["3"] || h["5"] || h["1"];
  if (!pick) return el("span", { class: "muted small", text: "No past cases" });
  const days = Object.keys(h).find(k => h[k] === pick);
  const word = direction === "LONG" ? "higher" : "lower";
  return el("span", { class: "small", title: "What followed the same price pattern on these stocks over five years. A past frequency, not a forecast; history cannot say which past moves had news." },
    el("strong", { text: `${pct(100 * pick.frequency, 0)} ${word} ${days} days later` }),
    el("div", { class: "muted" }, `${num(pick.cases, 0)} past cases, ${pct(100 * pick.low, 0)}–${pct(100 * pick.high, 0)}${pick.enough ? "" : " (few)"}`));
}

// Stop, entry, break-even and target on one line, placed by price.
function levelBar(card) {
  const opt = card.option, lm = opt && opt.limits;
  const pts = [{ v: card.stop, name: "Stop", cls: "neg" }, { v: card.entry, name: "Entry", cls: "mid" },
    { v: card.target_1, name: "Target 1", cls: "pos" }];
  if (lm && lm.viable && isNum(opt.strike)) {
    const be = opt.option_type === "CALL" ? opt.strike + lm.suggested : opt.strike - lm.suggested;
    pts.push({ v: be, name: "Break-even at expiry", cls: "mid", top: true });
  }
  const lo = Math.min(...pts.map(p => p.v)), hi = Math.max(...pts.map(p => p.v));
  const X = v => 4 + 92 * (v - lo) / Math.max(hi - lo, 1e-9);
  return el("div", { class: "m-levels" }, pts.map(p => el("span", { class: `m-level ${p.cls}${p.top ? " top" : ""}`, style: `left:${X(p.v).toFixed(1)}%` },
    el("i"), el("b", { text: `${p.name} ${morningMoney(p.v)}` }))));
}

async function copyText(text, button) {
  try { await navigator.clipboard.writeText(text); } catch (e) {
    const box = el("textarea", { style: "position:fixed;opacity:0" });
    box.value = text; document.body.append(box); box.select();
    try { document.execCommand("copy"); } catch (e2) { /* the order text stays visible to copy by hand */ }
    box.remove();
  }
  const was = button.textContent;
  button.textContent = "Copied";
  setTimeout(() => { button.textContent = was; }, 1500);
}

/* ---------- the trade card ---------- */
function morningCard(card, actions) {
  const opt = card.option, lm = opt && opt.limits, viable = !!(lm && lm.viable);
  const box = el("div", { class: "panel m-card", id: `card-${card.key || card.symbol}` });
  const sub = [card.setup_label || MORNING_SETUP_WORDS[card.setup] || card.setup, card.news && card.news.label,
    card.day_trade ? "day trade, out by 15:55" : `${card.horizon_days} trading days`,
    isNum(card.score) ? `score ${num(card.score, 0)}` : null].filter(Boolean).join(" · ");
  box.append(el("div", { class: "panel-head" },
    el("h3", {}, `${card.symbol}: ${card.side_word === "buy" ? "buy" : "short"}`,
      viable ? ` with a ${opt.option_type === "CALL" ? "call" : "put"}` : " the shares"),
    el("span", { class: "small muted", text: sub })));
  const plan = el("p", { class: "small" }, el("strong", { text: "The stock plan: " }),
    `enter about ${morningMoney(card.entry)}, out if ${card.symbol} trades ${card.direction === "LONG" ? "below" : "above"} `,
    el("strong", { text: morningMoney(card.stop) }), `, first target `, el("strong", { text: morningMoney(card.target_1) }),
    ` by ${shortDate(card.horizon_date)}. `, el("span", { class: "muted", text: card.levels_why || "" }));
  const left = el("div", {}, plan, levelBar(card));
  const sh = card.shares;
  const metrics = el("div", { class: "metrics m-metrics" });
  if (viable) {
    metrics.append(metric(money(opt.cost, 0), `Cost for ${opt.contracts} (the most it can lose)`),
      metric(signedMoney(opt.at_target_1, 0), `If ${card.symbol} reaches ${morningMoney(card.target_1)} by ${shortDate(card.horizon_date)}`),
      metric(signedMoney(opt.if_stopped, 0), "If stopped (model value then)"),
      metric(isNum(opt.chance_profit_horizon) ? pct(100 * opt.chance_profit_horizon, 0) : "n/a",
        "Chance of a profit by the horizon that option prices imply (not a forecast)"));
  } else if (sh && sh.count) {
    metrics.append(metric(num(sh.count, 0), "Shares (sized to your risk per trade)"),
      metric(signedMoney(sh.at_target_1, 0), "If target 1 is reached"),
      metric(signedMoney(sh.at_stop, 0), "If stopped"));
  }
  left.append(metrics);
  const rec = recordText(card.record, card.direction);
  if (rec) left.append(el("p", { class: "small" }, rec));

  const right = el("div", {});
  if (viable) {
    right.append(el("div", { class: "m-limit" }, el("div", { class: "small muted", text: "Limit price to type" }),
      el("div", { class: "m-big", text: money(lm.suggested) }),
      el("div", { class: "small" }, `Fair price ${money(lm.fair)}${isNum(lm.mid) && Math.abs(lm.mid - lm.fair) >= 0.005 ? ` (midpoint ${money(lm.mid)})` : ""}. Start at ${money(lm.patient)} if you can wait; never above `,
        el("strong", { text: money(lm.cap) }), ` — above that, reaching target 1 pays less than ${num(lm.min_payoff, 1)}× what the stop loses.`),
      lm.fair_basis ? el("div", { class: "small muted", style: "margin-top:4px", text: lm.fair_basis }) : null,
      el("div", { class: "m-ladder" }, [[lm.repriced ? "Last session's bid" : "Bid", opt.bid], ["Patient", lm.patient], ["Fair", lm.fair],
        [lm.repriced ? "Last session's ask" : "Ask", opt.ask], ["Most to pay", lm.cap]]
        .filter(([, v]) => isNum(v)).map(([k, v]) => el("span", { text: `${k} ${money(v)}` }))),
      lm.below_bid ? el("div", { class: "small muted", style: "margin-top:6px",
        text: "This limit is below the current bid, so the order rests until the price comes down to it \u2014 it may not fill." }) : null,
      opt.iv_note ? el("div", { class: "small muted", style: "margin-top:6px", text: opt.iv_note } ) : null,
      el("div", { class: "small muted", style: "margin-top:4px", text: `Prices: ${card.price_basis}.` })));
  } else if (opt && opt.limits && opt.limits.why) {
    right.append(banner("warn", opt.limits.why));
  }
  for (const w of card.warnings || []) right.append(banner("warn", w));
  for (const n of (card.notes || []).filter(n => !/^The shares carry/.test(n))) right.append(el("p", { class: "small muted", text: n }));
  right.append(el("p", { class: "small", style: "margin-bottom:4px" }, el("strong", { text: "Order to copy into Robinhood" })),
    el("div", { class: "m-order", text: card.order_text }));
  const row = el("div", { class: "row", style: "margin-top:10px" });
  const copy = el("button", { class: "primary", text: "Copy order" });
  copy.addEventListener("click", () => copyText(card.order_text, copy));
  row.append(copy);
  if (viable && BUILDERS.price) {
    const b = el("button", { class: "plain", text: "Price it live" });
    b.addEventListener("click", () => state.openPricing(FLOW.pricingRequest(card.symbol,
      { option_type: opt.option_type.toLowerCase(), strike: opt.strike, expiration: opt.expiration }, opt.contracts, "buy")));
    row.append(b);
  }
  if (state.openDecide && featureOn("trend_engine_v2") && !(actions && actions.hideDecide)) {
    const b = el("button", { class: "plain", text: "Analyze opportunity" });
    b.addEventListener("click", () => {
      if (actions && actions.opportunity && state.openOpportunity) { state.openOpportunity(actions.opportunity); return; }
      state.thesis = FLOW.thesis({ symbol: card.symbol, direction: card.direction, entry: card.entry,
        invalidation: card.stop, target1: card.target_1, target2: card.target_2, horizonDays: card.horizon_days });
      state.openDecide(card.symbol, card.direction);
    });
    row.append(b);
  }
  if (actions && actions.pick && featureOn("opportunity_blend")) {
    const b = el("button", { class: "plain", text: actions.picked && actions.picked() ? "In the blend" : "Add to the blend" });
    b.addEventListener("click", () => { actions.pick(); b.textContent = actions.picked() ? "In the blend" : "Add to the blend"; });
    row.append(b);
  }
  if (actions && actions.watchStop) {
    const b = el("button", { class: "plain", text: "Watch the stop" });
    b.addEventListener("click", () => actions.watchStop(card, b));
    row.append(b);
  }
  right.append(row);
  box.append(el("div", { class: "m-card-grid" }, left, right));
  return box;
}

/* ---------- 2.9: the trade card and the email on Analyze opportunity ---------- */
// After "Grade and find a contract" runs, the same card the Morning plan builds — the best contract for
// the risk budget, the capped limit price and the copy-ready order — is built for the graded thesis, with
// a button that emails the whole analysis. The card is server-built from the thesis levels.
if (typeof DECIDE_AFTER !== "undefined") DECIDE_AFTER.push((out, dec) => {
  if (!featureOn("morning") || !dec || !dec.grade) return;
  const g = dec.grade, lv = g.levels, t = dec.thesis || {};
  const req = { symbol: g.symbol, direction: g.direction, entry: g.entry, stop: lv.invalidation,
    target_1: lv.target_1, target_2: lv.target_2, setup: "analysis",
    horizon_days: Math.max(1, Math.min(260, Number(t.horizonDays) || 5)) };
  if (!(req.entry > 0 && req.stop > 0 && req.target_1 > 0)) return;
  const err = el("div", { class: "error", role: "alert" });
  const body = el("div", {}, el("div", { class: "loadnote small muted" },
    el("span", { class: "spinner" }), el("span", { text: "Building the trade card\u2026" })));
  out.append(el("div", { class: "panel" },
    el("h3", { text: "The trade card" }),
    el("p", { class: "small muted", text: "Your thesis as a Morning-plan card: the best contract for your risk per trade, the limit price capped where target 1 still pays the risk, and the order to copy." }),
    err, body));
  const watchStop = async (card, button) => {
    await withBusy(button, err, async () => {
      await api("/v2/morning/watch", { symbol: card.symbol, direction: card.direction,
        kind: card.direction === "LONG" ? "below" : "above", level: Math.round(card.stop * 100) / 100,
        why: `The stop of your ${card.symbol} ${card.side_word} thesis from Analyze` });
      button.textContent = "Watching (listed on the Morning plan)";
    });
  };
  (async () => {
    try {
      const card = await api("/v2/morning/card", req);
      const mail = el("button", { class: "plain", text: "Email me this analysis" });
      mail.addEventListener("click", () => withBusy(mail, err, async () => {
        const r = await api("/v2/analysis/email", req);
        mail.textContent = `Sent to ${r.sent_to}`;
      }));
      body.replaceChildren(morningCard(card, { watchStop, hideDecide: true }),
        el("div", { class: "row", style: "margin-top:8px" }, mail));
    } catch (e) {
      body.replaceChildren(el("p", { class: "small error", text: `No trade card: ${e.message}` }));
    }
  })();
});

/* ---------- the screen ---------- */
const OPP_SHOWN = 25;

// The markets before the open: overseas (Asia closed, Europe trading), the US index funds and the sector funds.
function morningContext(ctx, market) {
  if (!ctx) return null;
  const move = v => isNum(v) ? el("span", { class: v >= 0 ? "pos-text" : "neg-text", text: signedPct(v, 2) }) : el("span", { class: "muted", text: "n/a" });
  const block = (title, rows) => el("div", { class: "ctx-block" }, el("h4", { text: title }),
    el("ul", { class: "plain small" }, rows.map(([label, v, tip]) => el("li", { title: tip || null }, el("span", { text: label }), " ", move(v)))));
  const asia = ctx.overseas.filter(o => o.region === "Asia"), europe = ctx.overseas.filter(o => o.region === "Europe");
  const sectors = [...ctx.sectors].filter(s => isNum(s.move_pct)).sort((a, b) => b.move_pct - a.move_pct);
  return el("div", { class: "panel" },
    el("h3", { text: "Markets" }),
    el("div", { class: "ctx-grid" },
      block(`Asia (average ${isNum(ctx.asia_avg) ? signedPct(ctx.asia_avg, 2) : "n/a"})`, asia.map(o => [o.label, o.move_pct, o.source])),
      block(`Europe (average ${isNum(ctx.europe_avg) ? signedPct(ctx.europe_avg, 2) : "n/a"})`, europe.map(o => [o.label, o.move_pct, o.source])),
      block("United States", ctx.us.map(u => [u.label, u.move_pct])),
      block("Sectors, strongest first", sectors.map(s => [`${s.label} (${s.fund})`, s.move_pct]))),
    el("p", { class: "small muted" }, market && market.regime ? `Regime: ${market.regime}. ` : "", ctx.note,
      ctx.stooq && ctx.stooq.enabled ? "" : " Overseas readings are the US-listed funds that track each market (stooq's real indexes are off with demo data or STOOQ_QUOTES=0)."));
}

function opportunitiesTable(plan, picked, onCard) {
  const rows = plan.opportunities || [];
  const session = plan.phase === "session";
  const wrap = el("div");
  let filter = "all", all = false;
  const filters = segmented(`ofilter${++uid}`, [["all", "All"], ["LONG", "Long"], ["SHORT", "Short"], ["card", "With a card"]], "all");
  const more = el("button", { class: "plain small-btn" });
  const body = el("div");
  const draw = () => {
    const list = rows.filter(o => o.tradable !== false || filter === "all")
      .filter(o => filter === "all" || (filter === "card" ? o.card_key : o.direction === filter));
    const shown = all ? list : list.slice(0, OPP_SHOWN);
    more.textContent = all ? `Show the best ${OPP_SHOWN}` : `Show all ${list.length}`;
    more.classList.toggle("hidden", list.length <= OPP_SHOWN);
    body.replaceChildren(table([
      { label: "", cell: o => o.tradable === false ? "" : pickBox(picked, o.key) },
      { label: "#", num: true, cell: o => num(o.rank, 0) },
      { label: "Stock", cell: o => el("div", {}, el("strong", { text: o.symbol }), el("div", { class: "small muted", text: `${money(o.price)}${o.sector ? ` · ${o.sector}` : ""}` })) },
      { label: session ? "Since the close / open" : "Move", num: true, cell: o => el("div", {},
        el("span", { class: o.move_pct >= 0 ? "pos-text" : "neg-text", text: signedPct(o.move_pct, 1) }),
        el("div", { class: "small muted", text: `${num(o.ranges, 1)} ranges${isNum(o.since_open_ranges) ? ` · open ${o.since_open_ranges >= 0 ? "+" : ""}${num(o.since_open_ranges, 1)}` : ""}` })) },
      { label: "Its own part", num: true, cell: o => isNum(o.own_pct) ? el("span", { title: `Beyond SPY's move times its beta (${num(o.beta, 2)})`, text: signedPct(o.own_pct, 1) }) : "n/a" },
      { label: "Volume", num: true, cell: o => isNum(o.volume_ratio) ? el("span", { title: "Volume so far against a normal day's", text: `${num(100 * o.volume_ratio, 0)}%` }) : el("span", { class: "muted", text: "—" }) },
      { label: "News", cell: o => o.news && o.news.state !== "not_checked" ? newsTag(o.news) : el("span", { class: "small muted", text: "not checked" }) },
      { label: "Trade", cell: o => o.tradable === false ? el("span", { class: "muted small", text: "Quiet" }) :
        el("div", {}, el("span", { class: `m-tag ${o.direction === "LONG" ? "m-ride" : "m-fade"}`, text: `${o.direction === "LONG" ? "Buy" : "Short"}` }), " ",
          el("span", { class: "small", text: o.setup_label }), o.option ? el("div", { class: "small muted", text: `or a ${o.option.option_type.toLowerCase()} at $${num(o.option.premium, 2)}` }) : null) },
      { label: "Edge", num: true, cell: edgeCell },
      { label: "Score", num: true, cell: scoreCell },
      { label: "", cell: o => {
        const box = el("div", { class: "row nowrap" });
        if (o.card_key) { const b = el("button", { class: "plain small-btn", text: "Card" }); b.addEventListener("click", e => { e.stopPropagation(); onCard(o.card_key); }); box.append(b); }
        if (o.tradable !== false && state.openOpportunity && featureOn("trend_engine_v2")) box.append(analyzeButton(o));
        return box;
      } }], shown));
  };
  filters.addEventListener("change", () => { filter = filters.value(); draw(); });
  more.addEventListener("click", () => { all = !all; draw(); });
  draw();
  wrap.append(el("div", { class: "row", style: "margin-bottom:8px" }, filters, more), body);
  return wrap;
}

function buildMorning() {
  const scan = el("button", { class: "primary", text: "Build the plan" });
  const stopBtn = el("button", { class: "plain hidden", text: "Stop" });
  const emailBtn = el("button", { class: "plain hidden", text: "Email me this plan" });
  const budgetBtn = el("button", { class: "plain", text: "Set budget" });
  const horizon = segmented(`mhz${++uid}`, [["day", "Day trades"], ["swing", "Swing (5 days)"]], "day");
  const auto = el("input", { type: "checkbox" });
  const every = el("select", { "aria-label": "Refresh every" }, [5, 10, 15, 30].map(m => el("option", { value: m, text: `${m} min` })));
  const until = el("input", { type: "time", value: "11:30", min: "09:45", max: "16:00", "aria-label": "Until (New York time)" });
  const bar = el("progress", { max: 100, value: 0, class: "scan-progress", "aria-label": "Plan progress" });
  const barText = el("span", { class: "small muted" });
  const progress = el("div", { class: "row scan-row hidden" }, bar, barText);
  const err = el("div", { class: "error", role: "alert" });
  const headline = el("p", { class: "small muted" });
  const out = el("div");
  const watchesBox = el("div");
  const picked = new Set();
  let timer = null, shownJob, planDay = "", plan = null;
  const blend = blendPanel({ source: "morning", collapsed: true, title: "Split a budget across the morning's opportunities",
    keys: () => [...picked], horizon: () => horizon.value() });

  async function settings() {
    const p = await loadPrefs();
    if (!p) return;
    horizon.querySelector(`input[value="${p.morning.horizon}"]`).checked = true;
    auto.checked = !!p.morning.auto_refresh;
    every.value = String(p.morning.refresh_minutes);
    until.value = p.morning.am_end;
  }
  horizon.addEventListener("change", () => savePrefs({ morning: { horizon: horizon.value() } }));
  auto.addEventListener("change", () => api("/v2/prefs", { morning: { auto_refresh: auto.checked } }).then(p => { state.prefs = p; }).catch(e => { err.textContent = e.message; }));
  every.addEventListener("change", () => savePrefs({ morning: { refresh_minutes: Number(every.value) } }));
  until.addEventListener("change", () => { if (until.value) savePrefs({ morning: { am_end: until.value } }); });
  budgetBtn.addEventListener("click", () => blend.toggle());
  const blendOn = featureOn("opportunity_blend");
  if (!blendOn) { budgetBtn.classList.add("hidden"); blend.classList.add("hidden"); }

  async function watchStop(card, button) {
    await withBusy(button, err, async () => {
      const r = await api("/v2/morning/watch", { symbol: card.symbol, direction: card.direction,
        kind: card.direction === "LONG" ? "below" : "above", level: Math.round(card.stop * 100) / 100,
        why: `The stop of the ${card.setup} ${card.side_word} plan${planDay ? ` from ${shortDate(planDay)}` : ""}` });
      renderWatches(r.watches, r.email_configured);
      button.textContent = "Watching";
    });
  }
  async function addTriggerWatch(r, button) {
    await withBusy(button, err, async () => {
      const got = await api("/v2/morning/watch", { symbol: r.symbol, direction: r.direction, kind: r.trigger_kind,
        level: Math.round(r.trigger * 100) / 100, why: r.headline || `${r.side} forming` });
      renderWatches(got.watches, got.email_configured);
      button.textContent = "Watching";
    });
  }

  function renderWatches(watches, emailConfigured) {
    if (!watches || !watches.length) { watchesBox.replaceChildren(); return; }
    const rows = watches.map(w => el("div", { class: "row m-watch" },
      w.triggered_at ? statusTag("warn", "Triggered") : statusTag("info", "Watching"),
      el("span", {}, el("strong", { text: w.symbol }), ` ${w.kind} ${money(w.level)}`,
        w.triggered_at ? ` — hit ${money(w.triggered_price)} at ${w.triggered_at.slice(11, 16)}${w.emailed ? ", emailed" : ""}` : ""),
      el("span", { class: "small muted grow", text: w.why || "" }),
      (() => { if (w.triggered_at) return ""; const b = el("button", { class: "plain small-btn", text: "Remove" });
        b.addEventListener("click", () => withBusy(b, err, async () => {
          renderWatches((await api(`/v2/morning/watches/${w.id}/remove`, {})).watches, emailConfigured); })); return b; })()));
    watchesBox.replaceChildren(el("div", { class: "panel" }, el("h3", { text: "Watched triggers" }), ...rows,
      el("p", { class: "small muted", text: emailConfigured
        ? "Checked about once a minute while the market is open and this app is running; a trigger emails you."
        : "Checked about once a minute while the market is open and this app is running. Set up email in .env (SMTP_USER and SMTP_PASSWORD) to be emailed when one triggers." })));
  }

  const toCard = key => { const n = document.getElementById(`card-${key}`); if (n) n.scrollIntoView({ behavior: "smooth", block: "start" }); };

  function moversTable(p) {
    const cardBtn = r => {
      if (!r.card_key) return "";
      const b = el("button", { class: "plain small-btn", text: "Trade card" });
      b.addEventListener("click", () => toCard(r.card_key));
      return b;
    };
    return table([
      { label: "Stock", cell: r => el("div", {}, el("strong", { text: r.symbol }), el("div", { class: "small muted", text: `${morningMoney(r.price)} (${r.move_is})` })) },
      { label: "Move", num: true, cell: r => el("span", { class: r.move_pct > 0 ? "pos-text" : "neg-text", text: signedPct(r.move_pct, 1) }) },
      { label: "Size", num: true, cell: r => el("span", { title: "The move in this stock's own daily ranges" }, `${num(r.ranges, 1)} ranges`) },
      { label: "News", cell: r => newsTag(r.news) },
      { label: "Setup", cell: r => el("div", {}, setupTag(r.setup), el("div", { class: "small muted", text: r.setup.why })) },
      { label: "Past record of this setup", cell: r => r.setup.kind ? recordText(r.record, r.setup.direction) : el("span", { class: "muted", text: "—" }) },
      { label: "", cell: cardBtn }], p.movers);
  }

  function reversalTable(p) {
    const rows = p.reversal || [];
    if (!rows.length) return el("p", { class: "small muted", text: "No stock on the watchlist (or among the movers) shows a forming or confirmed reversal." });
    return table([
      { label: "Stock", cell: r => el("div", {}, el("strong", { text: r.symbol }), el("div", { class: "small" }, statusTag(r.confirmed ? "pass" : "warn", r.confirmed ? "Confirmed" : "Forming"))) },
      { label: "Signs of a turn", cell: r => el("div", { class: "small" }, `${r.side === "bottom" ? "Potential bottom" : "Potential top"} (score ${num(r.score, 0)}). `, (r.evidence || []).join(" ")) },
      { label: "Trigger to act on", cell: r => r.confirmed ? el("span", {}, "Already confirmed") :
        isNum(r.trigger) ? el("span", {}, el("strong", { text: `${r.trigger_kind === "above" ? "Above" : "Below"} ${money(r.trigger)}` })) : el("span", { class: "muted", text: "No clean trigger" }) },
      { label: "Trade if it turns", cell: r => r.direction === "LONG" ? "Calls or shares" : "Puts or short" },
      { label: "", cell: r => {
        if (r.card_key) { const b = el("button", { class: "plain small-btn", text: "Trade card" });
          b.addEventListener("click", () => toCard(r.card_key)); return b; }
        if (!r.confirmed && isNum(r.trigger)) { const b = el("button", { class: "plain small-btn", text: "Watch" });
          b.addEventListener("click", () => addTriggerWatch(r, b)); return b; }
        return "";
      } }], rows);
  }

  function scorecardPanel(score) {
    if (!score || !score.picks || !score.picks.length) return null;
    const graded = score.picks.filter(p => p.after && p.after["3"]);
    return el("details", { class: "panel" }, el("summary", { text: `Recent picks: ${score.profitable_3d} of ${score.graded_3d} in profit after 3 days` }),
      el("p", { class: "small muted", text: score.note }),
      table([{ label: "Day", cell: p => shortDate(p.day) }, { label: "Pick", cell: p => `${p.direction === "LONG" ? "Buy" : "Short"} ${p.symbol} (${p.setup}, ${p.instrument.toLowerCase()})` },
        { label: "Entry", num: true, cell: p => morningMoney(p.entry) },
        { label: "After 1 day", num: true, cell: p => p.after["1"] ? signedPct(p.after["1"].move_pct, 1) : el("span", { class: "muted", text: "pending" }) },
        { label: "After 3 days", num: true, cell: p => p.after["3"] ? signedPct(p.after["3"].move_pct, 1) : el("span", { class: "muted", text: "pending" }) },
        { label: "After 5 days", num: true, cell: p => p.after["5"] ? signedPct(p.after["5"].move_pct, 1) : el("span", { class: "muted", text: "pending" }) }],
        score.picks), graded.length ? null : el("p", { class: "small muted", text: "Grades appear as the days pass." }));
  }

  function render(st) {
    plan = st.result;
    const frag = el("div");
    for (const n of plan.notes || []) frag.append(banner(n.startsWith("Demo") ? "warn" : "info", n));
    const m = plan.market || {};
    const phaseWord = { pre: "before the open", session: "in the session", closed: "after the close" }[plan.phase] || "";
    headline.textContent = [
      `Built ${shortDate(plan.day)} ${plan.checked_at.slice(11, 16)} New York time${phaseWord ? ` (${phaseWord}${plan.rescan ? ", a rescan" : ""})` : ""}`,
      m.regime || null,
      isNum(m.spy_move_pct) ? `SPY ${signedPct(m.spy_move_pct, 1)} (${m.spy_move_is})` : null,
      plan.horizon ? (plan.horizon === "day" ? "day trades" : "swing trades") : null,
      plan.price_basis].filter(Boolean).join(" · ");
    const ctx = morningContext(plan.context, plan.market);
    if (ctx) frag.append(ctx);
    const opps = plan.opportunities || [];
    if (opps.length) {
      const tradable = opps.filter(o => o.tradable !== false).length;
      const sr = plan.session_record;
      frag.append(el("div", { class: "panel" },
        el("div", { class: "panel-head" }, el("h3", {}, `Opportunities: ${opps.length} stocks ranked`, " ", infoIcon("edge", "About the edge")),
          el("div", { class: "row" }, blendOn ? (() => { const b = el("button", { class: "primary small-btn", text: "Set budget" }); b.addEventListener("click", () => blend.toggle()); return b; })() : null)),
        el("p", { class: "small muted", text: `${tradable} could be traded (a move of half a daily range or more). Tick rows to blend only those. Hover a score for the points behind it, an edge for its record.${sr ? ` After the open, moves are judged by ${num(sr.cases, 0)} stock-days of 5-minute bars from ${sr.stocks} of today's movers.` : ""}` }),
        opportunitiesTable(plan, picked, toCard)));
    }
    frag.append(blend);
    if ((plan.movers || []).length) {
      frag.append(el("details", { class: "panel" }, el("summary", { text: `Big movers since the last close (${plan.movers.length})` }),
        moversTable(plan),
        el("p", { class: "small muted", text: `From ${num(plan.universe_size, 0)} large US stocks plus your watchlist (${num(plan.read, 0)} read). A move is measured in the stock's own daily ranges, so 4% in a quiet stock counts for more than 4% in a volatile one. ${plan.rule}` })));
    }
    frag.append(el("div", { class: "panel" }, el("h3", { text: "Reversal watch" }), reversalTable(plan),
      el("p", { class: "small muted", text: "Signs, not certainties: a true bottom or top is only known afterwards. Watch a trigger and the app checks it about once a minute while the market is open." })));
    if (plan.cards && plan.cards.length) {
      frag.append(el("h3", { class: "m-section", text: `Trade cards (${plan.cards.length})` }));
      planDay = plan.day || "";
      const byKey = Object.fromEntries(opps.map(o => [o.card_key, o]));
      for (const c of plan.cards) {
        const opp = byKey[c.key];
        frag.append(morningCard(c, { watchStop, opportunity: opp,
          pick: opp ? () => { if (picked.has(opp.key)) picked.delete(opp.key); else picked.add(opp.key); } : null,
          picked: opp ? () => picked.has(opp.key) : null }));
      }
    } else {
      frag.append(el("div", { class: "panel" }, el("p", { class: "small", text: "No opportunity supports a card and no reversal confirmed: not trading is the plan today." })));
    }
    const sc = scorecardPanel(st.scorecard);
    if (sc) frag.append(sc);
    out.replaceChildren(frag);
    renderWatches(st.watches, st.email_configured);
    emailBtn.classList.toggle("hidden", !st.email_configured);
  }

  async function paint(st) {
    const job = st.job, running = job.state === "running";
    scan.disabled = running;
    scan.textContent = st.result_at ? "Refresh the plan" : "Build the plan";
    stopBtn.classList.toggle("hidden", !running);
    progress.classList.toggle("hidden", !running);
    if (running) {
      bar.value = job.total ? Math.round(100 * job.done / job.total) : 0;
      barText.textContent = `Reading ${job.current || "the market"} (${Math.min(job.done + 1, job.total)} of ${job.total})`;
    }
    if (job.state === "failed") err.textContent = `The plan stopped: ${job.error}`;
    const key = `${st.result_job}|${st.result_at}`;
    if (key !== shownJob) {
      const full = st.result !== undefined ? st : await api("/v2/morning");
      shownJob = `${full.result_job}|${full.result_at}`;
      if (full.result) render(full);
      else {
        out.replaceChildren(el("div", { class: "panel" },
          el("p", {}, "No plan yet today. Press ", el("strong", { text: "Build the plan" }), "."),
          el("p", { class: "small muted", text: "It reads the 250 largest US stocks plus your watchlist, the markets overseas and the sector funds: a minute or two on demo data; with live data the first run of the day fetches each stock's history, spaced out so the rest of the app keeps working. For the 7:00 email every market morning, run ./setup_morning.sh on this computer (see the README)." })),
          blend);
        renderWatches(full.watches, full.email_configured);
      }
    }
    return running;
  }
  let failures = 0;
  async function poll() {
    clearTimeout(timer);
    if (view.classList.contains("hidden")) return;
    try {
      const running = await paint(await api("/v2/morning?brief=true"));
      failures = 0;
      // While the morning session refreshes by itself, look for a new plan every half minute.
      if (running) timer = setTimeout(poll, 1500);
      else if (auto.checked) timer = setTimeout(poll, 30000);
    } catch (e) {
      failures += 1;
      err.textContent = `${e.message} (trying again)`;
      timer = setTimeout(poll, Math.min(15000, 1500 * 2 ** failures));
    }
  }
  scan.addEventListener("click", () => withBusy(scan, err, async () => {
    err.textContent = "";
    await paint(await api("/v2/morning/scan", {}));
    timer = setTimeout(poll, 800);
  }));
  stopBtn.addEventListener("click", () => withBusy(stopBtn, err, async () => { await api("/v2/morning/stop", {}); poll(); }));
  emailBtn.addEventListener("click", () => withBusy(emailBtn, err, async () => {
    const r = await api("/v2/morning/email", {});
    err.textContent = "";
    emailBtn.textContent = `Sent to ${r.sent_to}`;
    setTimeout(() => { emailBtn.textContent = "Email me this plan"; }, 4000);
  }));

  const steps = el("div", { class: "m-steps" },
    el("span", { class: "m-step" }, el("b", { text: "1" }), " Pick an opportunity, or set a budget for a blend"),
    el("span", { class: "m-step" }, el("b", { text: "2" }), " Read its card, or analyze it"),
    el("span", { class: "m-step" }, el("b", { text: "3" }), " Copy the order into Robinhood, or paper trade it"));
  const view = el("section", {}, el("h2", { text: "Morning plan" }),
    el("p", { class: "lede", text: "The morning hours move the most. Before the open and through the morning session, the 250 largest US stocks ranked as opportunities: overseas markets, the moves since the close and since the open, news, and the past record behind each trade; a trade card with the limit price to type for the best, and a budget split across them. Setups and past frequencies, not forecasts — and nothing here places an order." }),
    steps,
    el("div", { class: "panel" },
      el("div", { class: "row" }, scan, stopBtn, emailBtn, budgetBtn),
      el("div", { class: "row", style: "margin-top:10px" },
        infoField("Trades", "horizon", horizon),
        el("div", { class: "field" }, el("span", {}, "Refresh in the morning session ", infoIcon("refresh")),
          el("div", { class: "row nowrap" }, el("label", { class: "check" }, auto, "every"), every, el("span", { class: "small", text: "until" }), until))),
      progress, err, headline),
    out, watchesBox);
  view.onShow = () => { settings(); poll(); };
  return view;
}

Object.assign(BUILDERS, { morning: buildMorning });
