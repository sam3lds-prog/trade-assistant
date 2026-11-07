"use strict";
/* Trading Desk 3.0 widgets: the approval queue, proposals (write, check, follow), the desk's state (policy, budget,
   kill switch, who runs its jobs, notifications), the policy itself, and the desk's positions. Approving here
   sends the order's hash with the decision, so an order changed after you looked at it cannot be approved by the
   same click. Orders go where the policy says: the paper broker in phase 5. */
(() => {
  const enc = encodeURIComponent;
  const loading = text => el("p", { class: "loadnote" }, el("span", { class: "spinner", "aria-hidden": "true" }), text || "Loading…");
  const STATUS = { draft: ["", "Draft"], awaiting_approval: ["warn", "Waiting for you"], blocked: ["fail", "Blocked"], authorized: ["pass", "Approved"],
    submitting: ["warn", "Sending"], submitted: ["pass", "Working"], partially_filled: ["pass", "Part filled"], filled: ["pass", "Filled"],
    cancel_requested: ["warn", "Cancelling"], cancelled: ["", "Cancelled"], rejected: ["fail", "Rejected"], expired: ["", "Expired"],
    unknown: ["fail", "Unknown: reconciling"], failed: ["fail", "Failed"], closed: ["", "Closed"] };
  const tag = s => statusTag(...(STATUS[s] || ["", s]));
  const until = iso => (iso ? new Date(iso).toLocaleTimeString([], { hour: "numeric", minute: "2-digit" }) : "");
  const signedMoney = v => (isNum(v) ? `${v > 0 ? "+" : v < 0 ? "−" : ""}${money(Math.abs(v))}` : "n/a");

  function checksList(risk, onlyBad) {
    const rows = (risk && risk.checks) || [];
    const shown = onlyBad ? rows.filter(c => c.status !== "pass") : rows;
    if (!shown.length) return "";
    return el("ul", { class: "plain small ws-checks-list" }, shown.map(c => el("li", { class: c.status === "pass" ? "pos-text" : c.status === "warn" ? "" : "neg-text" },
      el("strong", { text: `${c.label}: ` }), String(c.text))));
  }

  async function act(path, body, note) {
    try { return await api(path, body || {}); } catch (e) { if (note) note.textContent = e.message; else WS.toast(e.message); return null; }
  }

  /* ---------- the approval queue ---------- */
  WS.register("approvals", { title: "Approvals", category: "Trading", size: { w: 4, h: 8 }, symbolBadge: false, create(w) {
    const box = el("div");
    w.body.append(box);
    async function load() {
      try {
        const d = await api("/v3/proposals?status=awaiting_approval&limit=20");
        if (!d.proposals.length) { box.replaceChildren(WS.empty("Nothing is waiting for your approval.")); return; }
        box.replaceChildren(...d.proposals.map(card));
      } catch (e) { box.replaceChildren(el("p", { class: "error", text: e.message })); }
    }
    function card(p) {
      const note = el("p", { class: "small" });
      const pa = p.pending_approval;
      const approve = el("button", { class: "ws-btn primary", type: "button", disabled: !pa }, "Approve");
      const decline = el("button", { class: "ws-btn", type: "button", disabled: !pa }, "Decline");
      const again = el("button", { class: "ws-btn", type: "button" }, "Check again");
      approve.addEventListener("click", async () => { approve.disabled = decline.disabled = true; const r = await act(`/v3/approvals/${enc(pa.id)}/approve`, { order_hash: p.order_hash }, note); if (r) { WS.toast(r.status === "authorized" ? `Approved: ${r.describe}` : `Blocked: ${(r.risk || {}).text || ""}`); load(); } else approve.disabled = decline.disabled = false; });
      decline.addEventListener("click", async () => { approve.disabled = decline.disabled = true; if (await act(`/v3/approvals/${enc(pa.id)}/decline`, { order_hash: p.order_hash }, note)) load(); });
      again.addEventListener("click", async () => { if (await act(`/v3/proposals/${enc(p.id)}/recheck`, {}, note)) load(); });
      return el("div", { class: "panel ws-approval", "data-id": p.id },
        el("p", { class: "ws-approval-what" }, el("strong", { text: p.describe })),
        el("p", { class: "small muted", text: `${p.account_kind} account, ${money(p.terms.quantity * p.terms.limit_price * (p.terms.option ? p.terms.option.multiplier : 1))}. ${pa ? `Approve by ${until(pa.expires_at)}.` : "Its approval expired: check it again for a new one."}` }),
        p.thesis ? el("p", { class: "small", text: p.thesis }) : "",
        el("p", { class: "small" }, statusTag((p.risk || {}).ok ? "pass" : "fail", (p.risk || {}).ok ? "Risk checks pass" : "A check fails"), " ", el("span", { class: "muted", text: `Order ${p.order_hash.slice(0, 10)}` })),
        el("div", { class: "ws-row" }, pa ? approve : again, pa ? decline : "", el("button", { class: "ws-btn", type: "button", onclick: () => w.set({ symbol: p.symbol, proposal: p.id }) }, "Look closer")),
        note);
    }
    load();
    WS.onEvent("proposal.", () => load());
    return { refresh: load };
  } });

  /* ---------- proposals: write one, and follow them all ---------- */
  WS.register("proposals", { title: "Proposals", category: "Trading", size: { w: 8, h: 12 }, minW: 4, symbolBadge: true, create(w) {
    const side = segmented(`wsside${++uid}`, [["buy", "Buy"], ["sell", "Sell"]], "buy");
    const qty = el("input", { type: "number", min: "1", step: "1", value: "1", "aria-label": "Shares" });
    const limit = el("input", { type: "number", min: "0.01", step: "0.01", placeholder: "the ask", "aria-label": "Limit price" });
    const thesis = el("input", { type: "text", placeholder: "Why, and what would prove it wrong", "aria-label": "Thesis", maxlength: "500" });
    const check = el("button", { class: "ws-btn", type: "button" }, "Check");
    const propose = el("button", { class: "ws-btn primary", type: "button" }, "Propose");
    const note = el("div", { class: "small" });
    const list = el("div"), detail = el("div", { class: "ws-prop-detail" });
    const filter = el("select", { "aria-label": "Show" }, [["open", "Open"], ["", "All recent"], ["filled", "Filled"], ["blocked", "Blocked"]].map(([v, t]) => el("option", { value: v }, t)));
    w.body.append(el("div", { class: "ws-row" }, field("Side", side), field("Shares", qty), field("Limit ($)", limit)),
      el("div", { class: "ws-row" }, el("label", { class: "field ws-grow" }, el("span", { text: "Thesis" }), thesis)),
      el("div", { class: "ws-row" }, check, propose, note), el("div", { class: "ws-row" }, field("Show", filter)), list, detail);
    let picked = null;
    const body = () => {
      const c = w.ctx();
      return { symbol: c.symbol, side: side.value(), quantity: Number(qty.value), limit_price: Number(limit.value) || null, thesis: thesis.value.trim(),
        study_id: c.study || null, opportunity_id: c.opportunity && c.opportunity.id ? c.opportunity.id : null,
        source: c.study ? "research" : c.opportunity ? "discover" : "manual" };
    };
    check.addEventListener("click", async () => {
      if (!w.ctx().symbol) { note.textContent = "Pick a stock first."; return; }
      note.replaceChildren(loading("Checking…"));
      const r = await act("/v3/proposals/preview", body(), note);
      if (!r) return;
      if (!limit.value) limit.value = r.terms.limit_price;
      note.replaceChildren(el("p", {}, statusTag(r.risk.ok ? "pass" : "fail", r.risk.ok ? "Would pass" : "Would be blocked"), ` ${r.describe}. ${r.risk.text}`), checksList(r.risk, true));
    });
    propose.addEventListener("click", async () => {
      if (!w.ctx().symbol) { note.textContent = "Pick a stock first."; return; }
      propose.disabled = true;
      const r = await act("/v3/proposals", body(), note);
      propose.disabled = false;
      if (!r) return;
      note.replaceChildren(el("p", {}, tag(r.status), ` ${r.describe}. ${r.status === "awaiting_approval" ? "It waits for your approval." : (r.risk || {}).text || ""}`));
      picked = r.id; w.set({ proposal: r.id, _self: true }); load();
    });
    filter.addEventListener("change", load);

    async function load() {
      try {
        const d = await api(`/v3/proposals?limit=40${filter.value ? `&status=${enc(filter.value)}` : ""}`);
        if (!d.proposals.length) { list.replaceChildren(WS.empty(filter.value === "open" ? "No open proposals. Write one above for the linked stock." : "None.")); }
        else list.replaceChildren(table([
          { label: "What", cell: p => p.describe },
          { label: "State", cell: p => tag(p.status) },
          { label: "Account", cell: p => p.account_kind },
          { label: "Updated", cell: p => until(p.updated_at) },
        ], d.proposals, { onRow: p => { picked = p.id; w.set({ symbol: p.symbol, proposal: p.id, _self: true }); show(p.id); } }));
        if (picked) show(picked);
      } catch (e) { list.replaceChildren(el("p", { class: "error", text: e.message })); }
    }
    async function show(id) {
      let p;
      try { p = await api(`/v3/proposals/${enc(id)}`); } catch (e) { detail.replaceChildren(el("p", { class: "error", text: e.message })); return; }
      if (picked !== id) return;
      const dnote = el("p", { class: "small" });
      const buttons = [];
      if (["awaiting_approval", "blocked"].includes(p.status)) buttons.push(el("button", { class: "ws-btn", type: "button", onclick: async () => { if (await act(`/v3/proposals/${enc(id)}/recheck`, {}, dnote)) load(); } }, "Check again"));
      if (["draft", "awaiting_approval", "blocked", "authorized", "submitted", "partially_filled"].includes(p.status))
        buttons.push(el("button", { class: "ws-btn", type: "button", onclick: async () => { if (await act(`/v3/proposals/${enc(id)}/cancel`, {}, dnote)) load(); } }, "Cancel it"));
      const o = p.orders[0];
      detail.replaceChildren(
        el("h3", {}, tag(p.status), " ", p.describe),
        el("p", { class: "small muted", text: `${p.account_kind} account; written ${new Date(p.created_at).toLocaleString()} (${p.source}); order ${p.order_hash.slice(0, 12)}, version ${p.version}; expires ${until(p.expires_at)}.` }),
        p.thesis ? el("p", { class: "small", text: p.thesis }) : "",
        p.risk ? el("div", {}, el("h4", { text: `Risk: ${p.risk.text}` }), checksList(p.risk, true),
          el("details", {}, el("summary", { class: "small", text: `All ${p.risk.checks.length} checks, ${new Date(p.risk.assessed_at).toLocaleTimeString()}` }), checksList(p.risk, false))) : "",
        o ? el("div", {}, el("h4", { text: `At the ${o.broker} broker` }),
          el("p", { class: "small", text: `${o.state.replace("_", " ")}; ${o.filled_quantity} filled${isNum(o.average_fill_price) ? ` at ${money(o.average_fill_price)}` : ""}. Client id ${o.client_order_id}.${o.error ? ` ${o.error}` : ""}` })) : "",
        el("h4", { text: "What happened" }),
        el("ol", { class: "plain small ws-timeline" }, p.events.map(e => el("li", {}, el("time", { text: new Date(e.at).toLocaleTimeString() }), ` ${e.from_status ? `${e.from_status.replace("_", " ")} → ` : ""}${e.to_status.replace("_", " ")}, by ${e.actor}${e.reason ? `: ${e.reason}` : ""}`))),
        el("div", { class: "ws-row" }, ...buttons), dnote);
    }
    load();
    WS.onEvent("proposal.", () => load());
    return { onContext(c, changed) {
      if (changed.includes("symbol")) { limit.value = ""; note.textContent = ""; }
      if (changed.includes("proposal") && c.proposal && c.proposal !== picked) { picked = c.proposal; show(picked); }
    }, refresh: load };
  } });

  /* ---------- the desk: policy in force, money, kill switch, who runs the jobs ---------- */
  WS.register("deskstatus", { title: "Desk", category: "Trading", size: { w: 4, h: 12 }, symbolBadge: false, create(w) {
    const box = el("div");
    w.body.append(box);
    function meter(label, used, cap, money_ = true) {
      const pct = cap > 0 ? Math.max(0, Math.min(100, (100 * used) / cap)) : 0;
      return el("div", { class: "ws-meter" }, el("div", { class: "ws-meter-top" }, el("span", { text: label }),
        el("span", { class: "num", text: money_ ? `${money(used)} of ${money(cap, 0)}` : `${used} of ${cap}` })),
        el("div", { class: "ws-meter-bar", role: "meter", "aria-valuemin": "0", "aria-valuemax": String(cap), "aria-valuenow": String(used), "aria-label": label },
          el("span", { class: pct >= 100 ? "full" : pct >= 80 ? "near" : "", style: `width:${pct}%` })));
    }
    async function load() {
      let d;
      try { d = await api("/v3/desk/status"); } catch (e) { box.replaceChildren(el("p", { class: "error", text: e.message })); return; }
      const b = d.budget, ks = d.kill_switch, wk = d.worker, n = d.notifications;
      const reason = el("input", { type: "text", placeholder: "Why (optional)", "aria-label": "Why", maxlength: "200" });
      const knote = el("span", { class: "small" });
      const toggle = el("button", { class: `ws-btn ${ks.on ? "" : "danger"}`, type: "button" }, ks.on ? "Switch the kill switch off" : "Kill switch: stop everything");
      toggle.addEventListener("click", async () => { if (await act("/v3/desk/kill", { on: !ks.on, reason: reason.value }, knote)) load(); });
      const test = el("button", { class: "ws-btn", type: "button", disabled: !n.channels.length }, "Send a test");
      const tnote = el("span", { class: "small muted" });
      test.addEventListener("click", async () => { const r = await act("/v3/notifications/test", {}, tnote); if (r) tnote.textContent = `Sent to ${r.channels.join(", ")}.`; });
      const runs = { daemon: ["pass", `The daemon runs them (heartbeat ${Math.round(wk.daemon.age_seconds)} s ago)`], app: ["warn", "This app runs them while it is open"],
        nobody: ["", "Idle until there is work"] }[wk.runs_jobs];
      box.replaceChildren(
        el("p", {}, statusTag(ks.on ? "fail" : d.policy.policy.mode === "disabled" ? "" : "pass", d.mode_words)),
        el("p", { class: "small muted", text: d.summary }),
        b.error ? el("p", { class: "error", text: b.error }) : el("div", { class: "ws-meters" },
          meter("Capital in use", b.in_use, b.capital), meter("New buying today", b.today_new_buys, b.daily_buys_cap),
          meter("Positions", b.positions, b.max_positions, false),
          el("div", { class: "ws-meter" }, el("div", { class: "ws-meter-top" }, el("span", { text: "Today's result" }),
            el("span", { class: `num ${isNum(b.day_pnl) && b.day_pnl < 0 ? "neg-text" : ""}`, text: b.day_pnl === null ? "not measured" : `${signedMoney(b.day_pnl)} (stop at −${money(b.daily_loss_stop, 0)})` })))),
        el("h4", { text: "Kill switch" }),
        el("p", { class: "small", text: ks.on ? `On since ${new Date(ks.at).toLocaleString()} (${ks.by})${ks.reason ? `: ${ks.reason}` : ""}. Nothing is sent; approvals are refused.` : "Off. Switching it on cancels working orders and refuses every approval until it is off again." }),
        el("div", { class: "ws-row" }, ks.on ? "" : reason, toggle), knote,
        el("h4", { text: "Background jobs" }), el("p", { class: "small" }, statusTag(runs[0], runs[1])),
        el("p", { class: "small muted", text: `${Object.entries(wk.jobs).map(([k, v]) => `${v} ${k}`).join(", ") || "no jobs yet"}.${wk.app_worker.last_error ? ` Last error: ${wk.app_worker.last_error}` : ""}` }),
        el("h4", { text: "Notifications" }),
        el("p", { class: "small", text: n.channels.length ? `To ${n.channels.join(", ")}.` : "No channel is set up: SMTP, NTFY_TOPIC or a Telegram bot in .env." }),
        el("div", { class: "ws-row" }, test, tnote),
        el("h4", { text: "Live trading" }),
        d.live.enabled ? el("div", {}, el("p", { class: "small" }, statusTag(d.live.ready ? "pass" : "warn", d.live.ready ? `Ready: Robinhood account ${d.live.account}` : "Not ready"),
          ` Every live order is capped at ${money(d.live.ceiling, 0)} (LIVE_MAX_ORDER).`),
          d.live.ready ? "" : el("ul", { class: "plain small" }, d.live.reasons.map(r => el("li", { text: r }))),
          el("div", { class: "ws-row" }, el("button", { class: "ws-btn", type: "button", onclick: async e => { e.currentTarget.disabled = true; await act("/v3/desk/live/check", {}); load(); } }, "Check live trading")))
          : el("p", { class: "small muted", text: "Off. Orders go to the paper broker. Live trading needs ROBINHOOD_EXECUTION=1 and the account in .env (docs/V3.md, phase 6)." }),
        el("h4", { text: "Phone approvals" }),
        el("p", { class: "small", text: d.remote.enabled ? `On: the daemon serves them at ${d.remote.base_url || "REMOTE_BASE_URL (not set: links cannot be sent)"}.` : "Off. REMOTE_APPROVALS=1 with a PIN and your Tailscale address turns them on (see docs/V3.md)." }));
    }
    load();
    WS.onEvent("desk.", load); WS.onEvent("proposal.", load); WS.onEvent("daemon.", load);
    const timer = setInterval(() => { if (w.node.isConnected) load(); }, 30000);
    return { refresh: load, dispose() { clearInterval(timer); } };
  } });

  /* ---------- the policy ---------- */
  WS.register("policy", { title: "Trading policy", category: "Trading", size: { w: 4, h: 12 }, symbolBadge: false, create(w) {
    const box = el("div");
    w.body.append(box);
    const FIELDS = [["trial_capital", "Capital ($)"], ["max_order", "Largest order ($)"], ["max_daily_new_buys", "New buying a day ($)"],
      ["max_daily_loss", "Daily loss stop ($)"], ["max_open_positions", "Positions at most"], ["max_spread_bps", "Widest spread (bp)"],
      ["max_quote_age_seconds", "Oldest quote (s)"], ["max_limit_through_bps", "Limit past the quote (bp)"], ["approval_minutes", "Minutes to approve"],
      ["proposal_minutes", "Minutes a proposal lives"]];
    async function load() {
      let d;
      try { d = await api("/v3/desk/policy"); } catch (e) { box.replaceChildren(el("p", { class: "error", text: e.message })); return; }
      const p = d.policy;
      const mode = el("select", { "aria-label": "Mode" }, Object.entries(d.modes).map(([k, t]) => el("option", { value: k, selected: k === p.mode }, t)));
      const inputs = Object.fromEntries(FIELDS.map(([k]) => [k, el("input", { type: "number", step: "any", value: String(p[k]), "aria-label": k })]));
      const ws = el("input", { type: "text", value: p.window_start, size: "5", "aria-label": "From" }), we = el("input", { type: "text", value: p.window_end, size: "5", "aria-label": "Until" });
      const allow = el("input", { type: "text", value: p.symbols_allowed.join(", "), placeholder: "any stock", "aria-label": "Only these stocks" });
      const block = el("input", { type: "text", value: p.symbols_blocked.join(", "), placeholder: "none", "aria-label": "Never these stocks" });
      const save = el("button", { class: "ws-btn primary", type: "button" }, "Save the policy");
      const note = el("p", { class: "small" });
      save.addEventListener("click", async () => {
        const body = { ...p, mode: mode.value, account_kind: mode.value === "approval_required" ? "live" : mode.value === "paper" ? "paper" : p.account_kind,
          window_start: ws.value.trim(), window_end: we.value.trim(),
          symbols_allowed: allow.value.split(",").map(s => s.trim()).filter(Boolean), symbols_blocked: block.value.split(",").map(s => s.trim()).filter(Boolean) };
        for (const [k] of FIELDS) body[k] = Number(inputs[k].value);
        try {
          const r = await fetch("/v3/desk/policy", { method: "PUT", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
          const j = await r.json();
          if (!r.ok) throw new Error(j.error || r.statusText);
          note.replaceChildren(statusTag("pass", `Saved: version ${j.version}`));
          WS.toast("Policy saved.");
        } catch (e) { note.replaceChildren(el("span", { class: "error", text: e.message })); }
      });
      box.replaceChildren(field("Mode", mode),
        el("div", { class: "ws-policy-grid" }, FIELDS.map(([k, t]) => field(t, inputs[k]))),
        el("div", { class: "ws-row" }, field("From (New York)", ws), field("Until", we)),
        el("div", { class: "ws-row" }, el("label", { class: "field ws-grow" }, el("span", { text: "Only these stocks" }), allow)),
        el("div", { class: "ws-row" }, el("label", { class: "field ws-grow" }, el("span", { text: "Never these stocks" }), block)),
        el("p", { class: "small muted", text: `Long shares only, limit orders, regular hours. Version ${d.version}, changed ${new Date(d.updated_at).toLocaleString()}. Every change is kept in the audit log.` }),
        el("div", { class: "ws-row" }, save), note);
    }
    load();
    return { refresh: load };
  } });

  /* ---------- the desk's positions ---------- */
  WS.register("deskpositions", { title: "Desk positions", category: "Trading", size: { w: 8, h: 6 }, symbolBadge: false, create(w) {
    const box = el("div");
    w.body.append(box);
    async function load() {
      try {
        const d = await api("/v3/desk/positions");
        if (!d.positions.length) { box.replaceChildren(WS.empty(`The ${d.account_kind} account holds nothing. Today's realized result: ${signedMoney(d.realized_today)}.`)); return; }
        box.replaceChildren(table([
          { label: "Stock", cell: p => el("strong", { text: p.key }) },
          { label: "Shares", num: true, cell: p => String(p.quantity) },
          { label: "Average cost", num: true, cell: p => money(p.avg_cost) },
          { label: "Price", num: true, cell: p => (isNum(p.mark) ? money(p.mark) : "no quote") },
          { label: "Gain", num: true, cell: p => el("span", { class: isNum(p.unrealized) && p.unrealized < 0 ? "neg-text" : "pos-text", text: signedMoney(p.unrealized) }) },
          { label: "Today", num: true, cell: p => signedMoney(p.today) },
          { label: "", cell: p => el("button", { class: "ws-btn", type: "button", onclick: async e => {
            e.stopPropagation();
            const r = await act("/v3/proposals", { symbol: p.symbol, side: "sell", quantity: p.quantity, thesis: "Closing the position", source: "manual" });
            if (r) WS.toast(r.status === "awaiting_approval" ? `${r.describe}: waiting for your approval.` : `Blocked: ${(r.risk || {}).text || ""}`);
          } }, "Propose selling") },
        ], d.positions, { onRow: p => w.set({ symbol: p.symbol }) }),
        el("p", { class: "small muted", text: `${d.account_kind} account. Today: ${d.day_pnl === null ? "not measured" : signedMoney(d.day_pnl)}, of which realized ${signedMoney(d.realized_today)}.` }));
      } catch (e) { box.replaceChildren(el("p", { class: "error", text: e.message })); }
    }
    load();
    WS.onEvent("proposal.", load);
    return { refresh: load };
  } });

  /* ---------- the Trading Desk's standard layout, 3.0 ---------- */
  WS.DEFAULT_LAYOUTS.desk = [
    { type: "approvals", x: 0, y: 0, w: 4, h: 9, group: "blue" },
    { type: "proposals", x: 4, y: 0, w: 8, h: 13, group: "blue" },
    { type: "deskstatus", x: 0, y: 9, w: 4, h: 14, group: "none" },
    { type: "deskpositions", x: 4, y: 13, w: 8, h: 6, group: "blue" },
    { type: "activity", x: 4, y: 19, w: 4, h: 7, group: "none" },
    { type: "policy", x: 8, y: 19, w: 4, h: 13, group: "none" },
    { type: "classic:paper", x: 0, y: 32, w: 12, h: 12, group: "none" },
  ];
})();
