"use strict";
/* Discover 3.0 widgets: the one opportunity feed (each stock, direction and horizon once, ranked on one scale whose
   parts are shown) and the budget split across it. Picking an opportunity sets the link group's stock, direction,
   horizon and opportunity, so the chart, the study and the ticket follow; "Study it" starts a Research study bound
   to the opportunity. Nothing here places an order. */
(() => {
  const enc = encodeURIComponent;
  const loading = text => el("p", { class: "loadnote" }, el("span", { class: "spinner", "aria-hidden": "true" }), text || "Loading…");
  const pctText = (v, d = 2) => (isNum(v) ? `${v > 0 ? "+" : ""}${v.toFixed(d)}%` : "n/a");
  const HORIZON_WORDS = { intraday: "Today", swing: "Over days", event: "Around an event" };
  const SHORT_HORIZON = { intraday: "today", swing: "days", event: "event" };
  const SOURCE_WORDS = { morning: "Morning plan", ideas: "Trade ideas", live: "Live news", reversal: "Reversals", scanner: "Scanner", study: "Research scenarios" };
  const KIND_WORDS = { reversal: "Reversal", fade: "Fade", news: "News", breakout: "Breakout", pullback: "Pullback", gap: "Gap",
    earnings: "Earnings", momentum: "Momentum", scenario: "Scenario", other: "Other" };
  const words = (list, map) => list.map(k => map[k] || k).join(", ");
  const when = iso => (iso ? new Date(iso).toLocaleString([], { month: "short", day: "numeric", hour: "numeric", minute: "2-digit" }) : "");
  const select = (label, options, value) => el("select", { "aria-label": label }, options.map(([v, t]) => el("option", { value: v, selected: v === (value || "") }, t)));
  // What a study of an opportunity looks at: today's move for an intraday one, a week for the rest. 4.7: Discover's
  // horizons are categories and Research's are trading sessions; the one map between them is WS.horizonSessions.
  const studyOf = c => ({ horizon_days: WS.horizonSessions(c.horizon) || 5,
    thesis: c.kinds.includes("reversal") ? "reversal" : c.horizon === "intraday" ? "intraday_bounce"
      : c.next_event && c.next_event.kind === "earnings" && c.next_event.in_trading_days <= 5 ? "earnings_reaction" : "swing" });
  // The opportunity as the linked widgets receive it: its category horizon, its sessions and kind of trade for a study,
  // where it came from, and its catalyst when it has one.
  const oppOf = c => { const s = studyOf(c); return { id: c.id, symbol: c.symbol, direction: c.direction, horizon: c.horizon, horizon_sessions: s.horizon_days,
    thesis: s.thesis, source: (c.sources || [])[0] || null, catalyst: c.next_event ? `${c.next_event.kind}${c.next_event.date ? ` on ${c.next_event.date}` : ""}` : null }; };

  async function startStudy(w, c, key) {
    const s = studyOf(c);
    const r = await api("/v3/studies", { symbol: c.symbol, direction: c.direction, horizon_days: s.horizon_days, thesis: s.thesis, opportunity_id: c.id,
      request_key: key });
    w.set({ symbol: c.symbol, direction: c.direction, horizon: c.horizon, opportunity: oppOf(c), study: r.id });
    return r.id;
  }

  /* ---------- the opportunity feed ---------- */
  WS.register("feed", { title: "Opportunities", category: "Discover", size: { w: 8, h: 11 }, symbolBadge: false, create(w) {
    const horizon = select("Horizon", [["", "Any horizon"], ["intraday", "Today"], ["swing", "Over days"]], w.config.horizon);
    const way = select("Direction", [["", "Either way"], ["LONG", "Long"], ["SHORT", "Short"]], w.config.direction);
    // 4.4: scenario observations (a study's Qualified or Watch decision) on their own.
    const kindSel = select("Setup", [["", "Any setup"], ["scenario", "Scenario observations"]], w.config.kind);
    const eligible = el("input", { type: "checkbox", checked: !!w.config.eligible });
    const refresh = el("button", { class: "ws-btn", type: "button" }, "Refresh the scans");
    const scan = el("button", { class: "ws-btn", type: "button", title: "Also run the technical scanner over the watchlist: slower" }, "Scan the watchlist too");
    const status = el("span", { class: "small muted" });
    const list = el("div"), detail = el("div", { class: "ws-opp" });
    w.body.append(el("div", { class: "ws-row" }, field("Horizon", horizon), field("Direction", way), field("Setup", kindSel),
      el("label", { class: "ws-check" }, eligible, "Autopilot-eligible only")), el("div", { class: "ws-row" }, refresh, scan, status), list, detail);
    let rows = [], picked = w.config.picked || null, ticket = 0, weights = {}, lastMode = null, loadedAt = null;
    for (const c of [horizon, way, eligible, kindSel]) c.addEventListener("change", () => { w.save(); load(); });

    async function load() {
      const mine = ++ticket;
      if (!rows.length) list.replaceChildren(loading("Ranking opportunities…"));
      try {
        const q = new URLSearchParams({ limit: "60" });
        if (horizon.value) q.set("horizon", horizon.value);
        if (way.value) q.set("direction", way.value);
        if (kindSel.value) q.set("kind", kindSel.value);
        if (eligible.checked) q.set("eligible_only", "true");
        const d = await w.api(`/v3/discover?${q}`);
        if (mine !== ticket) return;
        rows = d.candidates; weights = d.weights || {}; lastMode = d.data_mode; loadedAt = new Date();
        render(d);
      } catch (e) { if (mine === ticket && e !== WS.GONE) list.replaceChildren(el("p", { class: "error", text: e.message })); }
    }

    function render(d) {
      if (!rows.length) {
        list.replaceChildren(WS.empty(eligible.checked ? "Nothing is eligible for the autopilot: that takes a validated record, positive after costs, on liquid stock with real prices."
          : "No opportunities yet. Refresh the scans, or build the Morning plan or Trade ideas: whatever they find appears here once, ranked on one scale."));
        detail.replaceChildren();
        return;
      }
      list.replaceChildren(table([
        { label: "#", num: true, cell: c => String(c.rank) },
        { label: "Stock", cell: c => el("strong", { text: c.symbol }) },
        { label: "Trade", cell: c => `${c.direction === "LONG" ? "Long" : "Short"}, ${SHORT_HORIZON[c.horizon] || c.horizon}` },
        { label: "Setup", cell: c => el("span", { title: `Found by ${words(c.sources, SOURCE_WORDS)}` },
          words(c.kinds.slice(0, 2), KIND_WORDS) + (c.kinds.length > 2 ? ` +${c.kinds.length - 2}` : "") + (c.sources.length > 1 ? ` (${c.sources.length} scans)` : "")) },
        { label: "Score", num: true, cell: c => el("span", { class: "ws-score", title: "Of 100, from the parts shown when you pick it" },
          el("span", { class: "ws-score-bar", style: `width:${Math.max(2, Math.min(100, c.rank_score))}%` }), num(c.rank_score, 0)) },
        { label: "Edge", num: true, cell: c => (c.edge.known ? el("span", { title: `${c.edge.cases} past cases, after costs` }, pctText(c.edge.expected_pct)) : el("span", { class: "muted" }, "none")) },
        { label: "Autopilot", cell: c => (c.autopilot_eligible ? statusTag("pass", "Eligible")
          : el("span", { class: "muted", title: c.autopilot_reasons.join("; ") }, "No")) },
      ], rows, { onRow: pick }));
      list.append(el("p", { class: "small muted", text: `${rows.length} ${rows.length === 1 ? "opportunity" : "opportunities"}${d.data_mode === "demo" ? " on made-up demo prices" : ""}. Each stock, direction and horizon appears once, however many scans found it.` }));
      const sel = rows.find(c => c.id === picked);
      if (sel) { mark(sel); showDetail(sel); } else detail.replaceChildren();
    }

    function mark(c) {
      const i = rows.indexOf(c);
      list.querySelectorAll("tbody tr").forEach((tr, j) => tr.classList.toggle("sel", j === i));
    }

    function pick(c) {
      picked = c.id; w.save();
      mark(c);
      w.set({ symbol: c.symbol, direction: c.direction, horizon: c.horizon, opportunity: oppOf(c), ...(c.study_id ? { study: c.study_id } : {}) });
      showDetail(c);
    }

    async function showDetail(c) {
      detail.replaceChildren(loading(`Loading ${c.symbol}…`));
      let full = c;
      try { full = await w.api(`/v3/discover/${enc(c.id)}`); } catch (e) { if (e === WS.GONE) return; /* the summary row is enough to show */ }
      if (picked !== c.id) return;
      const total = Object.values(weights).filter(x => x > 0).reduce((a, b) => a + b, 0) || 1;
      const parts = full.components.map(p => {
        const points = 100 * p.weight * p.value / total;
        return el("li", { class: `ws-part${p.weight < 0 ? " neg" : ""}` },
          el("span", { class: "ws-part-label", text: p.label }),
          el("span", { class: "ws-part-bar", "aria-hidden": "true" }, el("span", { style: `width:${Math.round(100 * p.value)}%` })),
          el("span", { class: "ws-part-pts num", text: `${points >= 0 ? "+" : ""}${points.toFixed(1)}` }),
          el("span", { class: "ws-part-text small muted", text: p.text }));
      });
      const kids = [
        el("h3", { text: `${full.symbol}, ${full.direction === "LONG" ? "long" : "short"}, ${(HORIZON_WORDS[full.horizon] || full.horizon).toLowerCase()}` }),
        el("p", { class: "small muted", text: `Score ${num(full.rank_score, 1)} of 100. First seen ${when(full.first_seen_at)}, last ${when(full.updated_at)}; drops off ${when(full.expires_at)} unless a scan sees it again.` }),
        el("h4", { text: "How the score is made" }), el("ul", { class: "plain ws-parts" }, parts),
      ];
      const levels = [["Entry", full.entry], ["Wrong below", full.direction === "LONG" ? full.invalidation : null], ["Wrong above", full.direction === "SHORT" ? full.invalidation : null], ["Target", full.target]]
        .filter(([, v]) => isNum(v));
      if (levels.length) kids.push(el("div", { class: "metrics" }, levels.map(([k, v]) => metric(money(v), k))));
      if (full.catalyst) kids.push(el("p", { class: "small" }, el("strong", { text: "Catalyst: " }), full.catalyst));
      const scen = (full.observations || []).map(o => o.scenario).find(Boolean);
      if (scen) kids.push(el("div", { class: `ws-decision ${scen.status}` }, el("h4", {}, statusTag(scen.status === "qualified" ? "pass" : "info", scen.label), ` ${scen.setup || ""}`,
          scen.example ? el("span", {}, " ", statusTag("warn", "Example")) : ""),
        el("p", { class: "small" }, scen.trigger ? `If ${scen.trigger}, by ${scen.expires_on}. ` : `Until ${scen.expires_on}. `,
          `Modeled net ${money(scen.net_pnl, 0)}; most it can lose ${isNum(scen.max_loss) ? money(scen.max_loss, 0) : "no limit"}.`),
        el("p", { class: "small muted", text: "A scenario observation: a projected case payoff from a study, not a historical edge. Never eligible for the autopilot." })));
      kids.push(el("h4", { text: "Autopilot" }), full.autopilot_eligible
        ? el("p", {}, statusTag("pass", "Eligible"), " It meets the bar for automatic trading, if you turn that on; the Trading Desk's limits still apply.")
        : el("ul", { class: "plain small" }, full.autopilot_reasons.map(r => el("li", { text: `Not eligible: ${r}.` }))));
      if ((full.observations || []).length) {
        kids.push(el("h4", { text: `What found it (${full.observations.length})` }), table([
          { label: "Scan", cell: o => SOURCE_WORDS[o.source] || o.source },
          { label: "When", cell: o => when(o.observed_at) },
          { label: "Its score", num: true, cell: o => el("span", { title: `On that scan's own scale: ${o.source_score_scale || "0-100"}` }, num(o.raw_score, 0)) },
          { label: "What it saw", cell: o => o.setup_label || o.signal || o.headline || (o.stage ? `${o.stage.toLowerCase()} reversal` : "") || KIND_WORDS[o.kind] || o.kind }],
          full.observations.slice(0, 12)));
      }
      const go = el("button", { class: "ws-btn primary", type: "button" }, full.study_id ? "Study it again" : "Study it");
      const open = full.study_id ? el("button", { class: "ws-btn", type: "button", onclick: () => { w.set({ study: full.study_id }); WS.goWorkspace("research"); } }, "Open its study") : "";
      const note = el("span", { class: "small muted" });
      // 4.7: Study it starts exactly one study (a repeat of the same click finds it) and opens Research on it.
      let key = null;
      go.addEventListener("click", async () => {
        go.disabled = true; note.textContent = "Starting the study…";
        key = key || (window.crypto && crypto.randomUUID ? crypto.randomUUID() : `k${Date.now()}${Math.random().toString(16).slice(2)}`);
        try {
          await startStudy(w, full, key);
          key = null;
          note.textContent = "Study started: Research follows it.";
          WS.goWorkspace("research");
        } catch (e) { note.textContent = e.message; }
        go.disabled = false;
      });
      kids.push(el("div", { class: "ws-row" }, go, open, note));
      detail.replaceChildren(...kids);
    }

    async function rescan(withScanner) {
      refresh.disabled = scan.disabled = true;
      status.textContent = withScanner ? "Scanning the watchlist…" : "Refreshing the scans…";
      try {
        const r = await api("/v3/discover/refresh", { sources: ["morning", "ideas", "live", "reversal", ...(withScanner ? ["scanner"] : [])] });
        status.textContent = `${r.observations} sightings, ${r.candidates} opportunities updated.${r.notes.length ? ` ${r.notes.join(" ")}` : ""}`;
        load();
      } catch (e) { status.textContent = e.message; }
      refresh.disabled = scan.disabled = false;
    }
    refresh.addEventListener("click", () => rescan(false));
    scan.addEventListener("click", () => rescan(true));

    load();
    w.on("opportunity.", () => load());
    return { refresh: load, config: () => ({ horizon: horizon.value, direction: way.value, kind: kindSel.value, eligible: eligible.checked, picked }),
      helpFacts: () => (loadedAt ? [["Shown", `${rows.length} ${rows.length === 1 ? "opportunity" : "opportunities"}, ranked ${loadedAt.toLocaleTimeString()}${lastMode === "demo" ? " on demo prices" : ""}`]] : []) };
  } });

  /* ---------- the budget split across the feed ---------- */
  WS.register("allocator", { title: "Budget split", category: "Discover", size: { w: 4, h: 11 }, symbolBadge: false, needs: ["opportunity_blend"], create(w) {
    const cfg = w.config;
    const budget = el("input", { type: "number", min: "100", step: "100", value: String(cfg.budget || 5000), "aria-label": "Budget in dollars" });
    const top = select("How many", [["3", "Top 3"], ["5", "Top 5"], ["8", "Top 8"], ["12", "Top 12"]], String(cfg.top_n || 8));
    const horizon = select("Horizon", [["", "Any horizon"], ["intraday", "Today"], ["swing", "Over days"]], cfg.horizon);
    const goal = select("Aim", [["balanced", "Best return for the risk"], ["low_risk", "Lowest risk"], ["dollars", "Most dollars"]], cfg.goal || "balanced");
    const INSTR = [["shares", "Shares"], ["short", "Short sales"], ["call", "Calls"], ["put", "Puts"]];
    const chosen = new Set(cfg.instruments || ["shares", "short", "call", "put"]);
    const boxes = INSTR.map(([k, t]) => {
      const b = el("input", { type: "checkbox", value: k, checked: chosen.has(k) });
      return el("label", { class: "ws-check" }, b, t);
    });
    const go = el("button", { class: "ws-btn primary", type: "button" }, "Split the budget");
    const out = el("div");
    w.body.append(el("div", { class: "ws-row" }, field("Budget ($)", budget), field("Across", top), field("Horizon", horizon)),
      el("div", { class: "ws-row" }, field("Aim", goal)), el("div", { class: "ws-row ws-checks", role: "group", "aria-label": "Instruments" }, boxes),
      el("div", { class: "ws-row" }, go), out);
    out.replaceChildren(WS.empty("Split a budget across the best opportunities in the feed, valued together on correlated price paths. Money the Trading Desk has reserved for open orders is taken off first."));
    const instruments = () => boxes.map(l => l.querySelector("input")).filter(b => b.checked).map(b => b.value);

    go.addEventListener("click", async () => {
      const b = Number(budget.value);
      if (!(b > 0)) { out.replaceChildren(el("p", { class: "error", text: "Enter a budget in dollars." })); return; }
      if (!instruments().length) { out.replaceChildren(el("p", { class: "error", text: "Pick at least one instrument." })); return; }
      w.save();
      go.disabled = true;
      out.replaceChildren(loading("Valuing the opportunities together…"));
      try {
        const r = await w.api("/v3/discover/allocate", { budget: b, top_n: Number(top.value), goal: goal.value, instruments: instruments(), horizon: horizon.value || null });
        render(r);
      } catch (e) { if (e !== WS.GONE) out.replaceChildren(el("p", { class: "error", text: e.message })); }
      go.disabled = false;
    });

    function render(r) {
      const c = r.combined;
      const kids = [el("div", { class: "metrics" },
        metric(money(r.budget - r.left, 0), `put to work of ${money(r.budget, 0)}`),
        metric(money(r.left, 0), "kept as cash"),
        r.reserved ? metric(money(r.reserved, 0), "already reserved by open orders") : "",
        c ? metric(money(c.expected_pnl, 0), "expected result") : "",
        c && isNum(c.expected_shortfall) ? metric(money(c.expected_shortfall, 0), "expected shortfall: the average of the worst tenth (modeled)") : "",
        metric(r.theoretical_max_loss === null || r.theoretical_max_loss === undefined ? "no limit" : money(r.theoretical_max_loss, 0), "most it can lose (contract terms)"))];
      if (r.rows.length) {
        kids.push(table([
          { label: "Stock", cell: x => el("strong", { text: x.symbol }) },
          { label: "Way", cell: x => (x.direction === "LONG" ? "Long" : x.direction === "SHORT" ? "Short" : "") },
          { label: "How", cell: x => x.label || x.instrument },
          { label: "Units", num: true, cell: x => `${x.units} ${x.unit || ""}`.trim() },
          { label: "Dollars", num: true, cell: x => money(x.dollars, 0) },
          { label: "Expected", num: true, cell: x => money(x.expected_pnl, 0) },
          { label: "Modeled chance of profit", num: true, cell: x => (isNum(x.p_profit) ? `${Math.round(100 * x.p_profit)}%` : "") }],
          r.rows, { onRow: x => w.set({ symbol: x.symbol, direction: x.direction, opportunity: x.candidate_id ? { id: x.candidate_id, symbol: x.symbol, direction: x.direction } : null }) }));
      } else {
        kids.push(el("p", { text: "It keeps the cash: none of these opportunities adds enough after costs and risk." }));
      }
      for (const n of r.notes || []) kids.push(el("p", { class: "small", text: n }));
      kids.push(el("p", { class: "small muted", text: "Modeled figures come from simulated price paths; what a broker requires to hold these (margin, option buying power) can differ." }));
      if ((r.skipped || []).length) kids.push(el("p", { class: "small muted", text: `Left out: ${r.skipped.map(s => `${s.symbol || s.id} (${s.why})`).join("; ")}.` }));
      if (r.note) kids.push(el("p", { class: "small muted", text: r.note }));
      out.replaceChildren(...kids);
    }
    return { config: () => ({ budget: Number(budget.value), top_n: Number(top.value), horizon: horizon.value, goal: goal.value, instruments: instruments() }) };
  } });

  /* ---------- Discover's standard layout, 3.1 ----------
     The ranked feed is the page; the budget split, the chart, the watchlist and the news sit beside and under it so
     the whole of it fits about one screen. The classic Morning plan is in the catalog (Add widget, Discover (classic
     screens)) rather than here: its picks already reach the feed. */
  WS.DEFAULT_LAYOUTS.discover = [
    { type: "feed", x: 0, y: 0, w: 7, h: 10, group: "blue" },
    { type: "allocator", x: 7, y: 0, w: 5, h: 10, group: "blue" },
    { type: "chart", x: 0, y: 10, w: 7, h: 8, group: "blue" },
    { type: "watchlist", x: 7, y: 10, w: 5, h: 4, group: "blue" },
    { type: "news", x: 7, y: 14, w: 5, h: 4, group: "blue" },
  ];
})();
