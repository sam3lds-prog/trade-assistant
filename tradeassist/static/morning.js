"use strict";
/* Trade Assistant 2.8: the Morning plan. Big movers since yesterday's close read as ride-the-news or
   fade-the-move, reversal triggers to watch, and a trade card per setup with the limit price to type.
   Three steps: pick, read the card, copy the order into Robinhood. Nothing here places an order. */

const MORNING_SETUP_WORDS = { fade: "Fade the move", ride: "Ride the news", reversal: "Reversal" };

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
  const sub = [MORNING_SETUP_WORDS[card.setup] || card.setup, card.news && card.news.label,
    `${card.horizon_days} trading days`].filter(Boolean).join(" · ");
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
    const b = el("button", { class: "plain", text: "Open on Decide" });
    b.addEventListener("click", () => {
      state.thesis = FLOW.thesis({ symbol: card.symbol, direction: card.direction, entry: card.entry,
        invalidation: card.stop, target1: card.target_1, target2: card.target_2, horizonDays: card.horizon_days });
      state.openDecide(card.symbol, card.direction);
    });
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

/* ---------- 2.9: the trade card and the email on Analyze a stock ---------- */
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
function buildMorning() {
  const scan = el("button", { class: "primary", text: "Build the plan" });
  const stopBtn = el("button", { class: "plain hidden", text: "Stop" });
  const emailBtn = el("button", { class: "plain hidden", text: "Email me this plan" });
  const bar = el("progress", { max: 100, value: 0, class: "scan-progress", "aria-label": "Plan progress" });
  const barText = el("span", { class: "small muted" });
  const progress = el("div", { class: "row scan-row hidden" }, bar, barText);
  const err = el("div", { class: "error", role: "alert" });
  const headline = el("p", { class: "small muted" });
  const out = el("div");
  const watchesBox = el("div");
  let timer = null, shownJob, planDay = "";

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

  function moversTable(plan) {
    const cardBtn = r => {
      if (!r.card_key) return "";
      const b = el("button", { class: "plain small-btn", text: "Trade card" });
      b.addEventListener("click", () => { const n = document.getElementById(`card-${r.card_key}`); if (n) n.scrollIntoView({ behavior: "smooth", block: "start" }); });
      return b;
    };
    return table([
      { label: "Stock", cell: r => el("div", {}, el("strong", { text: r.symbol }), el("div", { class: "small muted", text: `${morningMoney(r.price)} (${r.move_is})` })) },
      { label: "Move", num: true, cell: r => el("span", { class: r.move_pct > 0 ? "pos-text" : "neg-text", text: signedPct(r.move_pct, 1) }) },
      { label: "Size", num: true, cell: r => el("span", { title: "The move in this stock's own daily ranges" }, `${num(r.ranges, 1)} ranges`) },
      { label: "News", cell: r => newsTag(r.news) },
      { label: "Setup", cell: r => el("div", {}, setupTag(r.setup), el("div", { class: "small muted", text: r.setup.why })) },
      { label: "Past record of this setup", cell: r => r.setup.kind ? recordText(r.record, r.setup.direction) : el("span", { class: "muted", text: "—" }) },
      { label: "", cell: cardBtn }], plan.movers);
  }

  function reversalTable(plan) {
    const rows = plan.reversal || [];
    if (!rows.length) return el("p", { class: "small muted", text: "No stock on the watchlist (or among the movers) shows a forming or confirmed reversal." });
    return table([
      { label: "Stock", cell: r => el("div", {}, el("strong", { text: r.symbol }), el("div", { class: "small" }, statusTag(r.confirmed ? "pass" : "warn", r.confirmed ? "Confirmed" : "Forming"))) },
      { label: "Signs of a turn", cell: r => el("div", { class: "small" }, `${r.side === "bottom" ? "Potential bottom" : "Potential top"} (score ${num(r.score, 0)}). `, (r.evidence || []).join(" ")) },
      { label: "Trigger to act on", cell: r => r.confirmed ? el("span", {}, "Already confirmed") :
        isNum(r.trigger) ? el("span", {}, el("strong", { text: `${r.trigger_kind === "above" ? "Above" : "Below"} ${money(r.trigger)}` })) : el("span", { class: "muted", text: "No clean trigger" }) },
      { label: "Trade if it turns", cell: r => r.direction === "LONG" ? "Calls or shares" : "Puts or short" },
      { label: "", cell: r => {
        if (r.card_key) { const b = el("button", { class: "plain small-btn", text: "Trade card" });
          b.addEventListener("click", () => { const n = document.getElementById(`card-${r.card_key}`); if (n) n.scrollIntoView({ behavior: "smooth" }); }); return b; }
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
    const plan = st.result;
    const frag = el("div");
    for (const n of plan.notes || []) frag.append(banner(n.startsWith("Demo") ? "warn" : "info", n));
    const m = plan.market || {};
    headline.textContent = [
      `Built ${shortDate(plan.day)} ${plan.checked_at.slice(11, 16)} New York time`,
      m.regime || null,
      isNum(m.spy_move_pct) ? `SPY ${signedPct(m.spy_move_pct, 1)} (${m.spy_move_is})` : null,
      plan.price_basis].filter(Boolean).join(" · ");
    frag.append(el("div", { class: "panel" }, el("h3", { text: "Big movers since the last close" }),
      plan.movers.length ? moversTable(plan) : el("p", { class: "small muted", text: "No stock in the universe moved 3% and two of its daily ranges. A quiet open; the reversal watch below may still have triggers." }),
      el("p", { class: "small muted", text: `From ${num(plan.universe_size, 0)} large US stocks plus your watchlist (${num(plan.read, 0)} read). A move is measured in the stock's own daily ranges, so 4% in a quiet stock counts for more than 4% in a volatile one. ${plan.rule}` })));
    frag.append(el("div", { class: "panel" }, el("h3", { text: "Reversal watch" }), reversalTable(plan),
      el("p", { class: "small muted", text: "Signs, not certainties: a true bottom or top is only known afterwards. Watch a trigger and the app checks it about once a minute while the market is open." })));
    if (plan.cards && plan.cards.length) {
      frag.append(el("h3", { class: "m-section", text: `Trade cards (${plan.cards.length})` }));
      planDay = plan.day || "";
      for (const c of plan.cards) frag.append(morningCard(c, { watchStop }));
    } else {
      frag.append(el("div", { class: "panel" }, el("p", { class: "small", text: "No mover supports a setup and no reversal confirmed: not trading is the plan today." })));
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
          el("p", { class: "small muted", text: "It reads about 290 large US stocks plus your watchlist: a couple of minutes on demo data; with live data the first run of the day fetches each stock's history, spaced out so the rest of the app keeps working. For the 7:00 email every market morning, run ./setup_morning.sh on this computer (see the README)." })));
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
      if (running) timer = setTimeout(poll, 1500);
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
    el("span", { class: "m-step" }, el("b", { text: "1" }), " Pick a mover or a reversal"),
    el("span", { class: "m-step" }, el("b", { text: "2" }), " Read its trade card"),
    el("span", { class: "m-step" }, el("b", { text: "3" }), " Copy the order into Robinhood"));
  const view = el("section", {}, el("h2", { text: "Morning plan" }),
    el("p", { class: "lede", text: "Stocks that jumped since the last close, read as ride-the-news or fade-the-move; reversals about to confirm; and a trade card with the limit price to type. Setups and past frequencies, not forecasts — and nothing here places an order." }),
    steps,
    el("div", { class: "panel" }, el("div", { class: "row" }, scan, stopBtn, emailBtn), progress, err, headline),
    out, watchesBox);
  view.onShow = () => poll();
  return view;
}

Object.assign(BUILDERS, { morning: buildMorning });
