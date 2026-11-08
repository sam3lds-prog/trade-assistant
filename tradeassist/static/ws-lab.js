"use strict";
/* Model Lab 4.0: the replays, what the models learned, and their forward record. Widgets read the engine's
   results through /v4; a replay is started here but runs in its own process, and stopping it pauses it at its last
   finished session. The held-out year is not opened from here (one command, on purpose). Discover gets "Model
   opportunities" and Research "Model forecast" from the deployed models' shadow forecasts. Nothing here places an
   order. */
(() => {
  const enc = encodeURIComponent;
  const loading = text => el("p", { class: "loadnote" }, el("span", { class: "spinner", "aria-hidden": "true" }), text || "Loading…");
  const pct = (v, d = 2) => (isNum(v) ? `${v > 0 ? "+" : ""}${(100 * v).toFixed(d)}%` : "n/a");
  const share = v => (isNum(v) ? `${Math.round(100 * v)}%` : "n/a");
  const bp = v => (isNum(v) ? `${v > 0 ? "+" : ""}${v.toFixed(2)} bp` : "n/a");
  const num = (v, d = 2) => (isNum(v) ? v.toFixed(d) : "n/a");
  const when = iso => (iso ? new Date(iso).toLocaleString([], { month: "short", day: "numeric", hour: "numeric", minute: "2-digit" }) : "");
  const STATUS = { complete: ["pass", "Complete"], running: ["info", "Running"], paused: ["warn", "Paused"], failed: ["fail", "Failed"],
    queued: ["info", "Queued"], interrupted: ["warn", "Interrupted"] };
  const VALIDATION = { holdout_tested: ["pass", "Tested on the held-out year"], holdout_reopened: ["warn", "Held-out year reopened"],
    replayed: ["info", "Replayed only: not yet tested on the held-out year"] };
  const FAMILY = { no_change: "No change", drift: "Drift", engines30: "3.0 engines", ridge: "Ridge", gbm: "Boosted trees",
    base_rate: "Base rates", gbm_cls: "Boosted classifier" };
  const famWord = f => FAMILY[f] || f || "n/a";
  const statusOf = r => (r.interrupted ? STATUS.interrupted : STATUS[r.status] || ["info", r.status]);

  // The run every Model Lab widget in a link group shows: the one picked in the controller, else the newest finished one.
  async function runOf(w) {
    const c = w.ctx();
    if (c.run) return c.run;
    const d = await w.api("/v4/replay/runs?limit=20");
    const done = (d.runs || []).find(r => r.status === "complete");
    return done ? done.id : null;
  }
  const noRun = () => WS.empty("No finished replay yet. Build a dataset and start one in the Replay controller, or run 'python run.py --replay run'.");

  /* ---------- the replay controller ---------- */
  WS.register("lab_runs", { title: "Replay controller", category: "Model Lab", size: { w: 7, h: 10 }, minW: 4, symbolBadge: false, group: "green", create(w) {
    const out = el("div"), form = el("div", { class: "hidden" });
    const start = el("input", { type: "date", "aria-label": "First session", value: "2016-11-01" });
    const end = el("input", { type: "date", "aria-label": "Last session", value: "2025-10-31", max: "2025-10-31" });
    const retrain = el("select", { "aria-label": "Retraining" }, [["weekly", "Weekly"], ["monthly", "Monthly (faster)"], ["never", "Never"]].map(([v, t]) => el("option", { value: v }, t)));
    const fams = ["drift", "engines30", "ridge", "gbm"].map(f => el("label", { class: "ws-check" }, el("input", { type: "checkbox", value: f, checked: true }), famWord(f)));
    const go = el("button", { class: "ws-btn primary", type: "button" }, "Start the replay");
    form.append(el("div", { class: "ws-row" }, field("From", start), field("To", end), field("Retraining", retrain)),
      el("div", { class: "ws-row ws-checks" }, el("span", { class: "small muted", text: "Challengers:" }), ...fams), el("div", { class: "ws-row" }, go),
      el("p", { class: "small muted", text: "Runs in its own process on this computer; the app stays responsive. A development replay must end before 1 November 2025: the year after is held out." }));
    const newBtn = el("button", { class: "ws-btn", type: "button" }, "New replay…");
    const buildBtn = el("button", { class: "ws-btn", type: "button", title: "Fetch about eleven years of daily bars for the 100 large caps and the market funds, and freeze them" }, "Build a dataset");
    const msg = el("p", { class: "small", role: "status" });
    w.body.append(el("div", { class: "ws-row" }, newBtn, buildBtn), form, msg, out);
    newBtn.addEventListener("click", () => form.classList.toggle("hidden"));
    buildBtn.addEventListener("click", async () => {
      try { const r = await api("/v4/replay/datasets/build", {}); msg.textContent = `Building in the background; its log: ${r.log}`; }
      catch (e) { msg.textContent = e.message; }
    });
    go.addEventListener("click", async () => {
      const families = fams.map(l => l.querySelector("input")).filter(i => i.checked).map(i => i.value);
      try {
        const r = await api("/v4/replay/runs", { start: start.value || null, end: end.value || null, retrain: retrain.value, families });
        form.classList.add("hidden"); msg.textContent = `Started ${r.run}.`; w.set({ run: r.run, _self: true });
      } catch (e) { msg.textContent = e.message; }
    });
    let timer = null;
    async function act(path, r) {
      try { await api(path, {}); load(); } catch (e) { msg.textContent = e.message; }
      return r;
    }
    async function load() {
      clearTimeout(timer);
      if (w.disposed) return;
      try {
        const [runs, sets] = await Promise.all([w.api("/v4/replay/runs?limit=30"), w.api("/v4/replay/datasets")]);
        const kids = [];
        const ds = sets.datasets || [];
        kids.push(ds.length
          ? el("p", { class: "small muted" }, `Newest dataset ${ds[0].id}: ${ds[0].symbols} symbols, ${ds[0].first_date} to ${ds[0].last_date}, `,
              ds[0].checks.fail ? statusTag("fail", `${ds[0].checks.fail} failed checks`) : statusTag("pass", "checks passed"),
              ds[0].checks.warn ? ` ${ds[0].checks.warn} warnings.` : ".")
          : WS.empty("No dataset yet: Build a dataset first (it uses your Tradier token)."));
        const list = runs.runs || [];
        if (list.length) {
          const picked = w.ctx().run;
          kids.push(table([
            { label: "Run", cell: r => el(r.id === picked ? "strong" : "span", { text: `${r.kind === "holdout" ? "Held-out year · " : ""}${r.first_session} to ${r.last_session}` }) },
            { label: "Status", cell: r => statusTag(...statusOf(r)) },
            { label: "At", cell: r => r.checkpoint || "–" },
            { label: "", cell: r => (r.status === "complete" ? "" : r.status === "running" && !r.interrupted
              ? el("button", { class: "ws-btn ws-btn-mini", type: "button", onclick: e => { e.stopPropagation(); act(`/v4/replay/runs/${enc(r.id)}/stop`, r); } }, "Stop")
              : el("button", { class: "ws-btn ws-btn-mini", type: "button", onclick: e => { e.stopPropagation(); act(`/v4/replay/runs/${enc(r.id)}/start`, r); } }, "Resume")) },
          ], list, { onRow: r => w.set({ run: r.id, _self: true }) }));
          const sel = list.find(r => r.id === picked) || list[0];
          if (sel && sel.headline) kids.push(el("p", { class: "small" }, el("strong", { text: "Result: " }), sel.headline));
          if (sel && sel.error) kids.push(el("p", { class: "error", text: sel.error }));
          if (list.some(r => r.status === "running" && !r.interrupted)) timer = setTimeout(load, 5000);
        } else kids.push(WS.empty("No replays yet."));
        out.replaceChildren(...kids);
      } catch (e) { if (e !== WS.GONE) out.replaceChildren(el("p", { class: "error", text: e.message })); }
    }
    load();
    return { refresh: load, dispose: () => clearTimeout(timer), onContext: (_c, changed) => { if (changed.includes("run")) load(); },
      helpFacts: () => [["Where it runs", "A separate process on this computer"]] };
  } });

  /* ---------- the learning curve ---------- */
  function curveChart(months) {
    const W = 640, H = 170, pad = { l: 44, r: 8, t: 10, b: 22 };
    const vals = months.map(m => m.gain_bp || 0);
    const lim = Math.max(1, ...vals.map(Math.abs));
    const bw = (W - pad.l - pad.r) / Math.max(1, months.length);
    const y = v => pad.t + (H - pad.t - pad.b) * (1 - (v + lim) / (2 * lim));
    const s = svg("svg", { viewBox: `0 0 ${W} ${H}`, class: "ws-curve", role: "img", "aria-label": "Monthly gain over no change, in basis points" });
    s.append(svg("line", { x1: pad.l, x2: W - pad.r, y1: y(0), y2: y(0), stroke: "currentColor", "stroke-opacity": 0.4 }));
    for (const [v, label] of [[lim, bp(lim)], [-lim, bp(-lim)]]) s.append(svg("text", { x: 2, y: y(v) + 4, "font-size": 10, fill: "currentColor" }, label));
    months.forEach((m, i) => {
      const v = m.gain_bp || 0, top = Math.min(y(v), y(0)), h = Math.max(1, Math.abs(y(v) - y(0)));
      const bar = svg("rect", { x: pad.l + i * bw + 0.5, y: top, width: Math.max(1, bw - 1), height: h, fill: v >= 0 ? "var(--pass)" : "var(--fail)" });
      bar.append(svg("title", {}, `${m.month}: ${bp(v)}; band held ${share(m.coverage)}; champion ${famWord(m.family)}`));
      s.append(bar);
      if (m.month.endsWith("-01")) s.append(svg("text", { x: pad.l + i * bw, y: H - 6, "font-size": 10, fill: "currentColor" }, m.month.slice(0, 4)));
    });
    return s;
  }
  WS.register("lab_curve", { title: "Learning curve", category: "Model Lab", size: { w: 7, h: 9 }, minW: 4, symbolBadge: false, group: "green", create(w) {
    const horizon = el("select", { "aria-label": "Horizon" }, [1, 5].map(h => el("option", { value: String(h), selected: h === (w.config.horizon || 1) }, `${h} day${h === 1 ? "" : "s"}`)));
    const out = el("div");
    w.body.append(el("div", { class: "ws-row" }, field("Horizon", horizon)), out);
    horizon.addEventListener("change", () => { w.config.horizon = Number(horizon.value); w.save(); load(); });
    async function load() {
      const mine = w.ticket();
      try {
        const run = await runOf(w);
        if (!w.current(mine)) return;
        if (!run) { out.replaceChildren(noRun()); return; }
        const d = await w.api(`/v4/replay/runs/${enc(run)}/curve?horizon=${horizon.value}`);
        if (!w.current(mine)) return;
        const months = d.months || [];
        if (!months.length) { out.replaceChildren(WS.empty("Nothing graded yet in this run.")); return; }
        const pos = months.filter(m => (m.gain_bp || 0) > 0).length;
        const fams = [...new Set(months.map(m => m.family))];
        out.replaceChildren(curveChart(months),
          el("p", { class: "small" }, `${pos} of ${months.length} months beat "no change". Champions over the run: ${fams.map(famWord).join(" → ")}.`),
          el("p", { class: "small muted", text: "Each bar: the month's average gain in error over 'no change', per stock and day, from forecasts frozen before their outcomes. Hover a bar for its band coverage and champion." }));
      } catch (e) { if (e !== WS.GONE) out.replaceChildren(el("p", { class: "error", text: e.message })); }
    }
    load();
    return { refresh: load, onContext: (_c, changed) => { if (changed.includes("run")) load(); } };
  } });

  /* ---------- the model registry and deployments ---------- */
  WS.register("lab_registry", { title: "Model registry", category: "Model Lab", size: { w: 5, h: 9 }, minW: 3, symbolBadge: false, group: "green", create(w) {
    const out = el("div"), msg = el("p", { class: "small", role: "status" });
    w.body.append(msg, out);
    async function load() {
      const mine = w.ticket();
      try {
        const [run, deps] = await Promise.all([runOf(w), w.api("/v4/models/deployments")]);
        if (!w.current(mine)) return;
        const kids = [el("h4", { text: "Deployed models" })];
        const list = deps.deployments || [];
        if (list.length) kids.push(table([
          { label: "", cell: d => (d.active ? statusTag("pass", "Active") : "") },
          { label: "From", cell: d => `${d.source_run} (${d.run_kind || "?"})` },
          { label: "Models", cell: d => d.models.map(m => `${m.task === "trade" ? "trade" : `${m.horizon}d`} ${famWord(m.family)}`).join(", ") },
          { label: "", cell: d => (d.active ? "" : el("button", { class: "ws-btn ws-btn-mini", type: "button", onclick: async () => {
            try { await api(`/v4/models/deployments/${enc(d.id)}/activate`, {}); load(); } catch (e) { msg.textContent = e.message; } } }, "Make active")) },
        ], list));
        else kids.push(WS.empty("Nothing deployed: the shadow forecasts start once a finished replay is deployed."));
        if (run) {
          const [st, lin] = await Promise.all([w.api(`/v4/replay/runs/${enc(run)}`), w.api(`/v4/replay/runs/${enc(run)}/lineage`)]);
          if (!w.current(mine)) return;
          kids.push(el("h4", { text: `Run ${run}` }));
          const champs = (st.summary && st.summary.champions) || {};
          if (Object.keys(champs).length) kids.push(el("p", { class: "small" }, "Final champions: ",
            Object.entries(champs).map(([k, v]) => `${k.replace("daily:", "").replace("trade:", "trade ")}${k.startsWith("daily") ? "-day" : ""} ${famWord(v.family)}`).join("; "), "."));
          if (st.status === "complete" && st.kind !== "shadow") kids.push(el("button", { class: "ws-btn", type: "button", onclick: async () => {
            try { const r = await api("/v4/models/deploy", { run }); msg.textContent = `Deployed as ${r.deployment}: tomorrow's shadow forecasts use it.`; load(); }
            catch (e) { msg.textContent = e.message; } } }, "Deploy these models"));
          const promoted = (lin.training || []).filter(t => t.decision === "promoted");
          const kept = (lin.training || []).filter(t => t.decision === "kept").length;
          kids.push(el("p", { class: "small muted", text: `${(lin.training || []).length} training days: ${promoted.length} promotions, ${kept} kept the champion (refitted).` }));
          if (promoted.length) kids.push(table([
            { label: "Session", cell: t => t.session }, { label: "Task", cell: t => (t.task === "trade" ? "trade" : `${t.horizon}-day`) },
            { label: "Now", cell: t => famWord(t.champion_after.split(".")[0]) }, { label: "Why", cell: t => el("span", { class: "small", text: t.reason }) },
          ], promoted.slice(-12).reverse()));
        }
        out.replaceChildren(...kids);
      } catch (e) { if (e !== WS.GONE) out.replaceChildren(el("p", { class: "error", text: e.message })); }
    }
    load();
    return { refresh: load, onContext: (_c, changed) => { if (changed.includes("run")) load(); } };
  } });

  /* ---------- the error explorer ---------- */
  WS.register("lab_errors", { title: "Error explorer", category: "Model Lab", size: { w: 6, h: 9 }, minW: 4, symbolBadge: false, group: "green", create(w) {
    const horizon = el("select", { "aria-label": "Horizon" }, [1, 5].map(h => el("option", { value: String(h), selected: h === (w.config.horizon || 1) }, `${h} day${h === 1 ? "" : "s"}`)));
    const out = el("div");
    w.body.append(el("div", { class: "ws-row" }, field("Horizon", horizon)), out);
    horizon.addEventListener("change", () => { w.config.horizon = Number(horizon.value); w.save(); load(); });
    async function load() {
      const mine = w.ticket();
      try {
        const run = await runOf(w);
        if (!w.current(mine)) return;
        if (!run) { out.replaceChildren(noRun()); return; }
        const d = await w.api(`/v4/replay/runs/${enc(run)}/errors?horizon=${horizon.value}&limit=15`);
        if (!w.current(mine)) return;
        if (!d.largest.length) { out.replaceChildren(WS.empty("Nothing graded yet in this run.")); return; }
        const pick = r => w.set({ symbol: r.symbol });
        out.replaceChildren(
          el("h4", { text: "By the market's regime" }),
          table([{ label: "Regime", cell: r => r[0] }, { label: "Graded", num: true, cell: r => String(r[1].graded) },
            { label: "Error", num: true, cell: r => `${r[1].mae_pct.toFixed(2)}%` }, { label: "No change", num: true, cell: r => `${r[1].nochange_mae_pct.toFixed(2)}%` },
            { label: "Outside band", num: true, cell: r => share(r[1].outside_band) }], Object.entries(d.by_regime)),
          el("h4", { text: "Stocks it forecast worst" }),
          table([{ label: "Stock", cell: r => r.symbol }, { label: "vs no change", num: true, cell: r => bp(r.gain_bp) }, { label: "Graded", num: true, cell: r => String(r.graded) }],
            d.by_symbol.worst || [], { onRow: pick }),
          el("h4", { text: "Largest misses" }),
          table([{ label: "Session", cell: r => r.session }, { label: "Stock", cell: r => r.symbol }, { label: "Said", num: true, cell: r => pct(r.pred_return) },
            { label: "Was", num: true, cell: r => pct(r.realized_return) }, { label: "Regime", cell: r => r.regime }, { label: "Model", cell: r => famWord(r.family) }],
            d.largest, { onRow: pick }));
      } catch (e) { if (e !== WS.GONE) out.replaceChildren(el("p", { class: "error", text: e.message })); }
    }
    load();
    return { refresh: load, onContext: (_c, changed) => { if (changed.includes("run")) load(); } };
  } });

  /* ---------- trade outcomes ---------- */
  const tradeRow = (label, s) => ({ label, s: s || {} });
  WS.register("lab_trades", { title: "Trade outcomes", category: "Model Lab", size: { w: 6, h: 9 }, minW: 4, symbolBadge: false, group: "green", create(w) {
    const out = el("div");
    w.body.append(out);
    async function load() {
      const mine = w.ticket();
      try {
        const run = await runOf(w);
        if (!w.current(mine)) return;
        if (!run) { out.replaceChildren(noRun()); return; }
        const card = await w.api(`/v4/replay/runs/${enc(run)}/scorecard`);
        if (!w.current(mine)) return;
        const t = card.trade || {};
        if (!t.all_setups) { out.replaceChildren(WS.empty("No setup has been graded yet in this run.")); return; }
        const rows = [tradeRow("Every setup", t.all_setups), tradeRow("Taken by the gate", t.taken), tradeRow("Passed by the gate", t.passed)];
        out.replaceChildren(
          table([{ label: "", cell: r => r.label }, { label: "Trades", num: true, cell: r => String(r.s.trades || 0) },
            { label: "Win rate", num: true, cell: r => share(r.s.win_rate) }, { label: "Average net", num: true, cell: r => pct(r.s.expectancy) },
            { label: "Profit factor", num: true, cell: r => num(r.s.profit_factor) }, { label: "Worst run", num: true, cell: r => pct(r.s.max_drawdown, 1) }], rows),
          el("p", { class: "small" }, `Barriers reached: ${Object.entries(t.barriers || {}).map(([k, v]) => `${k} ${v}`).join(", ")}.`),
          el("p", { class: "small muted", text: "One unit of capital per trade, after 5 bp of costs a side. 'Ambiguous' means a day's range reached both the target and the stop: daily bars cannot say which came first, so it counts as the stop. Price accuracy and these trade results are judged apart." }));
      } catch (e) { if (e !== WS.GONE) out.replaceChildren(el("p", { class: "error", text: e.message })); }
    }
    load();
    return { refresh: load, onContext: (_c, changed) => { if (changed.includes("run")) load(); } };
  } });

  /* ---------- the forward record (shadow forecasts) ---------- */
  WS.register("lab_shadow", { title: "Forward record", category: "Model Lab", size: { w: 5, h: 10 }, minW: 3, symbolBadge: false, group: "green", create(w) {
    const out = el("div"), msg = el("p", { class: "small", role: "status" });
    const now = el("button", { class: "ws-btn", type: "button", title: "Forecast the last finished session now, instead of waiting for the half-hourly job" }, "Forecast now");
    const toDiscover = el("button", { class: "ws-btn", type: "button" }, "Show in Discover");
    const toResearch = el("button", { class: "ws-btn", type: "button" }, "Show in Research");
    w.body.append(el("div", { class: "ws-row" }, now, toDiscover, toResearch), msg, out);
    // Discover's and Research's standard widgets share the blue link group: the model's widget joins them there.
    const show = (kind, type) => {
      WS.goWorkspace(kind);
      const t = WS.tabs().get(WS.active());
      if ([...t.widgets.values()].some(x => x.type === type)) return;
      const added = WS.addToActive(type);
      if (added.group !== "blue") added.setGroup("blue");
    };
    toDiscover.addEventListener("click", () => show("discover", "model_ideas"));
    toResearch.addEventListener("click", () => show("research", "model_forecast"));
    now.addEventListener("click", async () => {
      try { const r = await api("/v4/shadow/run", {}); msg.textContent = r.skipped || `Session ${r.session}: ${typeof r.predicted === "object" ? `${r.predicted.daily} forecasts, ${r.predicted.trade} setups` : r.predicted}; ${r.graded} graded.`; load(); }
      catch (e) { msg.textContent = e.message; }
    });
    async function load() {
      try {
        const d = await w.api("/v4/shadow");
        if (!d.deployment) { out.replaceChildren(WS.empty(d.text)); return; }
        const v = d.validation || {};
        const kids = [el("p", {}, statusTag(...(VALIDATION[v.status] || ["info", v.status])), ` Models from ${d.deployment.source_run}${d.deployment.label ? ` (${d.deployment.label})` : ""}.`)];
        if (!v.learned) kids.push(el("p", { class: "small" }, statusTag("warn", "Baselines only"), " Nothing learned beat the baselines in that replay: these forecasts are 'no change' and history's base rates."));
        if (v.holdout_headline) kids.push(el("p", { class: "small" }, el("strong", { text: "Held-out year: " }), v.holdout_headline));
        else if (v.replay_headline) kids.push(el("p", { class: "small" }, el("strong", { text: "Replay: " }), v.replay_headline));
        kids.push(el("p", { class: "small muted", text: `Last forecast session ${d.last_session || "none yet"}${d.last_run_at ? `, run ${when(d.last_run_at)}` : ""}.` }));
        const rec = d.record || {};
        kids.push(el("h4", { text: "Forecasts saved before the future happened" }), el("p", {}, rec.headline || "Nothing graded yet: a one-day forecast is graded the next evening."));
        out.replaceChildren(...kids);
      } catch (e) { if (e !== WS.GONE) out.replaceChildren(el("p", { class: "error", text: e.message })); }
    }
    load();
    return { refresh: load };
  } });

  /* ---------- Research's own forecasts, graded ---------- */
  WS.register("lab_research", { title: "Research forecasts graded", category: "Model Lab", size: { w: 6, h: 7 }, minW: 3, symbolBadge: false, group: "green", create(w) {
    const out = el("div");
    w.body.append(out);
    async function load() {
      try {
        const d = await w.api("/v4/research/record");
        const rows = Object.entries(d.by_horizon || {}).flatMap(([h, list]) => list.map(r => ({ h, ...r })));
        out.replaceChildren(rows.length ? table([{ label: "Horizon", cell: r => `${r.h} day${r.h === "1" ? "" : "s"}` }, { label: "Model", cell: r => r.model_version },
          { label: "Graded", num: true, cell: r => String(r.n) }, { label: "Inside band", num: true, cell: r => share(r.coverage) },
          { label: "Direction", num: true, cell: r => (r.leaned ? share(r.hits) : "n/a") }, { label: "Miss", num: true, cell: r => `${num(r.mae)} pts` },
          { label: "No change", num: true, cell: r => `${num(r.nochange_mae)} pts` }], rows)
          : WS.empty("No Research study's forecast has reached its horizon yet. Each study's forecast is graded once, after its target session closes."));
      } catch (e) { if (e !== WS.GONE) out.replaceChildren(el("p", { class: "error", text: e.message })); }
    }
    load();
    return { refresh: load };
  } });

  /* ---------- Discover: today's model opportunities ---------- */
  WS.register("model_ideas", { title: "Model opportunities", category: "Discover", size: { w: 12, h: 8 }, minW: 4, symbolBadge: false, create(w) {
    const out = el("div");
    w.body.append(out);
    async function load() {
      try {
        const d = await w.api("/v4/shadow/opportunities?limit=15");
        if (!d.deployment) { out.replaceChildren(WS.empty("No deployed models yet: open Model Lab to replay and deploy them.")); return; }
        const v = d.validation || {};
        const pick = r => w.set({ symbol: r.symbol, direction: "LONG", horizon: "swing" });
        out.replaceChildren(
          el("p", { class: "small" }, statusTag(...(VALIDATION[v.status] || ["info", v.status])), ` Frozen after the close of ${d.session || "n/a"}. The Discover rank above is unchanged; this is the models' view beside it.`),
          el("h4", { text: "Setups (enter at the next open; target 1.5 daily ranges, stop 1, five sessions)" }),
          d.setups.length ? table([{ label: "Stock", cell: r => r.symbol }, { label: "Setup", cell: r => r.setup },
            { label: "Target first", num: true, cell: r => share(r.p_target) }, { label: "Stop first", num: true, cell: r => share(r.p_stop) },
            { label: "Neither", num: true, cell: r => share(r.p_timeout) }, { label: "Expected net", num: true, cell: r => pct(r.expected_return) },
            { label: "Gate", cell: r => el("span", { title: r.reason || "" }, statusTag(r.take ? "pass" : "info", r.take ? "Take" : "Pass")) }], d.setups, { onRow: pick })
            : WS.empty("No setup qualified after the last close."),
          el("h4", { text: "Leaning hardest tomorrow (against each stock's own volatility)" }),
          d.leaning.length ? table([{ label: "Stock", cell: r => r.symbol }, { label: "Next day", num: true, cell: r => pct(r.pred_return) },
            { label: "80% band", num: true, cell: r => `${pct(r.low_return, 1)} to ${pct(r.high_return, 1)}` }, { label: "Chance up", num: true, cell: r => share(r.p_up) },
            { label: "Model", cell: r => famWord(r.family) }], d.leaning.filter(r => r.horizon === 1).slice(0, 10), { onRow: pick })
            : WS.empty("The deployed next-day model is 'no change': it leans nowhere."));
      } catch (e) { if (e !== WS.GONE) out.replaceChildren(el("p", { class: "error", text: e.message })); }
    }
    load();
    return { refresh: load };
  } });

  /* ---------- Research: the model's forecast for the selected stock ---------- */
  WS.register("model_forecast", { title: "Model forecast", category: "Research", size: { w: 5, h: 9 }, minW: 3, create(w) {
    const out = el("div"), why = el("div");
    w.body.append(out, why);
    async function explain(h) {
      try { const d = await w.api(`/v4/shadow/why?symbol=${enc(w.ctx().symbol)}&horizon=${h}`);
        why.replaceChildren(el("p", { class: "small" }, d.text), d.inputs && d.inputs.length ? table([{ label: "Input", cell: r => r.feature },
          { label: "Before", num: true, cell: r => num(r.before, 4) }, { label: "After", num: true, cell: r => num(r.after, 4) },
          { label: "Moved the forecast", num: true, cell: r => (isNum(r.effect_pct) ? `${r.effect_pct > 0 ? "+" : ""}${r.effect_pct.toFixed(3)} pts` : "n/a") }], d.inputs) : "");
      } catch (e) { if (e !== WS.GONE) why.replaceChildren(el("p", { class: "error", text: e.message })); }
    }
    async function load() {
      const sym = w.ctx().symbol, mine = w.ticket();
      why.replaceChildren();
      if (!sym) { out.replaceChildren(WS.empty("Pick a stock.")); return; }
      try {
        const d = await w.api(`/v4/shadow/latest?symbol=${enc(sym)}`);
        if (!w.current(mine)) return;
        if (!d.deployment) { out.replaceChildren(WS.empty("No deployed models yet: open Model Lab to replay and deploy them.")); return; }
        const preds = d.predictions || [];
        if (!preds.length) { out.replaceChildren(WS.empty(`${sym} has no model forecast for the last session (not in the models' universe, or not yet a year of history).`)); return; }
        const v = d.validation || {};
        const daily = preds.filter(p => p.task === "daily"), trade = preds.find(p => p.task === "trade");
        const kids = [el("p", { class: "small" }, statusTag(...(VALIDATION[v.status] || ["info", v.status])), ` After the close of ${preds[0].session}.`),
          table([{ label: "Horizon", cell: p => `${p.horizon} day${p.horizon === 1 ? "" : "s"}` }, { label: "Forecast", num: true, cell: p => pct(p.pred_return) },
            { label: "80% band", num: true, cell: p => `${pct(p.low_return, 1)} to ${pct(p.high_return, 1)}` }, { label: "Chance up", num: true, cell: p => share(p.p_up) },
            { label: "Model", cell: p => famWord(p.family) },
            { label: "", cell: p => el("button", { class: "ws-btn ws-btn-mini", type: "button", onclick: () => explain(p.horizon) }, "Why did it change?") }], daily)];
        if (trade) kids.push(el("p", { class: "small" }, el("strong", { text: `Setup: ${trade.setup}. ` }),
          `Target first ${share(trade.p_target)}, stop first ${share(trade.p_stop)}, neither ${share(trade.p_timeout)}; expected ${pct(trade.expected_return)} net. `,
          statusTag(trade.take ? "pass" : "info", trade.take ? "The gate would take it" : "The gate passes"), ` ${trade.reason || ""}`));
        const rec = (d.record && d.record.daily && d.record.daily["1"]) || (d.replay_record && d.replay_record.daily && d.replay_record.daily["1"]);
        if (rec && rec.graded) kids.push(el("p", { class: "small muted", text: `On ${sym} so far: ${rec.graded} one-day forecasts graded, ${bp(rec.gain_bp)} against 'no change', band held ${share(rec.coverage)}${d.record && d.record.daily && d.record.daily["1"] ? " (live)" : " (replay)"}.` }));
        out.replaceChildren(...kids);
      } catch (e) { if (e !== WS.GONE) out.replaceChildren(el("p", { class: "error", text: e.message })); }
    }
    return { onContext: (_c, changed) => { if (changed.includes("symbol")) load(); }, refresh: load,
      helpFacts: () => [["Stock", w.ctx().symbol || "none"]] };
  } });

  WS.DEFAULT_LAYOUTS.lab = [
    { type: "lab_runs", x: 0, y: 0, w: 7, h: 10, group: "green" },
    { type: "lab_shadow", x: 7, y: 0, w: 5, h: 10, group: "green" },
    { type: "lab_curve", x: 0, y: 10, w: 7, h: 9, group: "green" },
    { type: "lab_registry", x: 7, y: 10, w: 5, h: 9, group: "green" },
    { type: "lab_errors", x: 0, y: 19, w: 6, h: 9, group: "green" },
    { type: "lab_trades", x: 6, y: 19, w: 6, h: 9, group: "green" },
  ];
  WS.DEFAULT_LAYOUTS.discover.push({ type: "model_ideas", x: 0, y: 18, w: 12, h: 8, group: "blue" });
})();
